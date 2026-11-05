"""Checking product searches: store each provider's offers, queue new ones for the mail, Vinted shipping.

The steps the shared check loop (worker.check) asks of a search kind: prepare, rests, fetch, save, finish;
then take_pending and explain for the mail, idle, queue_mails and cleanup for the loop.
"""

import json
import time
from collections.abc import Collection, Mapping
from sqlite3 import Connection
from typing import Any

from .. import mailserver, status, store
from ..core import images
from ..shapes import MailEntry, MailSample, Row
from . import delivery, learning, sources
from . import mail as mail  # the kind's mail presentation (kinds.py)
from .categories import BIKE_TYPES, CATEGORIES, FUELS, GEARBOXES, PICKUP_SOURCES, PLATFORMS, RADII
from .categories import SOURCES as SOURCES
from .models import SEARCH_FILTERS, Search
from .prices import Offer, compared_price, eligible, free, in_range, search_fee, too_cheap, with_shipping
from .titles import all_terms, title_fits, wanted_words

FRESH_USED = 3 * 3600  # a shop comparison only uses second-hand results this recent
SHIPPING_WAIT = 15 * 60  # a Vinted hit waits this long for its shipping lookup before it is mailed without
HITS = 'offers'  # key of the hit rows in /api/state
MODEL = Search  # the request body of a product search
# What the search editor offers; the providers' section IDs stay on the server.
CHOICES = {
    'categories': {key: {k: v for k, v in value.items() if k != 'portal'} for key, value in CATEGORIES.items()},
    'platforms': PLATFORMS,
    'fuels': FUELS,
    'gearboxes': GEARBOXES,
    'bike_types': BIKE_TYPES,
    'radii': RADII,
    'pickup_sources': PICKUP_SOURCES,
}


def compared(offer, search):
    """compared_price for a stored offer row."""
    return compared_price(
        offer['price'],
        offer['shipping'],
        with_shipping(offer['source'], search),
        search_fee(offer['source'], offer['price'], search),
    )


# A snapshot on the watch list whose search is gone: shown with shipping and fee, within no limit.
ANY_SEARCH = {'include_shipping': 1, 'include_fee': 1, 'min_price': 0, 'max_price': float('inf')}


def judged(row, search):
    """ROW (an offer or watch-list snapshot) with what the page shows and filters by, so the price rules live
    here only: COMPARED (None with unknown shipping that counts), FITS the search, WITH_SHIPPING for the note."""
    search = search or ANY_SEARCH
    price = compared(row, search)
    return dict(
        row,
        compared=price,
        fits=in_range(price, search, free(row['price'], search)),
        with_shipping=with_shipping(row['source'], search),
    )


# Checking a search


async def prepare(search: store.Search) -> None:
    """What hiding taught the search's category filters this check too (titles.taught_away)."""
    search['learned'] = learning.learned(search)


def rests(search: store.Search, source: str) -> str | None:
    """Product providers never rest for a search."""
    return None


async def fetch(source: str, search: store.Search) -> tuple[list[Offer], Row | None]:
    return await sources.fetch(source, search)


def empty_is_suspicious(reference: Row | None) -> bool:
    """An empty product result is ordinary; a changed layout is caught by markup_changed."""
    return False


def save(
    search: store.Search, source: str, offers: list[Offer], reference: Row | None, observed: float
) -> tuple[list[Offer], set[str]]:
    """Store a successful result; returns (offers, the starting point's URLs) for finish()."""
    return offers, save_result(search, source, offers, reference, observed)


def finish(search: store.Search, saved: list[tuple[list[Offer], set[str]]]) -> None:
    """After all sources: remember Vinted offers without shipping, queue what is new for the mail."""
    found = {offer.url: offer for offers, _ in saved for offer in offers}
    baseline = set().union(*(urls for _, urls in saved)) if saved else set()
    # Shipping only adds to the item price: an item already at the limit can never become a hit.
    delivery.remember(offer for offer in found.values() if offer.price < search['max_price'])
    enqueue(search, found, baseline)


def save_result(search, source, offers, reference, now):
    """Replace a source's stored offers with a successful result. Returns the URLs that form its
    starting point: the first readable result of a source is not news and is never mailed.

    An offer waiting for the mail stays until it is sent: only the first result page is read, so newer
    offers may just have pushed it off, which does not mean it is gone."""
    sid = search['id']
    with store.db() as conn:
        seen = dict(conn.execute('SELECT url, first_seen FROM offers WHERE search_id=? AND source=?', (sid, source)))
        waiting = {r[0] for r in conn.execute('SELECT url FROM outbox WHERE search_id=? AND batch IS NULL', (sid,))}
        current = {offer.url for offer in offers}
        # An offer already waiting for the mail is news, also in a starting point after an edit.
        baseline = current - waiting if not (seen.keys() - waiting) and not status.started(conn, sid, source) else set()
        # The app shows no "new" dot for this starting point either.
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in baseline])
        sent = dict(conn.execute('SELECT url, sent_at FROM notifications WHERE search_id=?', (sid,)))
        conn.executemany(
            'DELETE FROM offers WHERE search_id=? AND source=? AND url=?',
            [(sid, source, url) for url in seen if url in current or url not in waiting],
        )
        conn.executemany(
            'INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?)',
            [
                (
                    *(sid, o.source, o.url, o.title, o.price, o.shipping, o.seller, o.condition),
                    seen.get(o.url) or sent.get(o.url) or now,
                    now,
                )
                for o in offers
            ],
        )
        conn.executemany(
            'INSERT OR IGNORE INTO negotiable_offers VALUES (?,?)',
            [(sid, o.url) for o in offers if o.negotiable],
        )
        # Vinted shipping still unknown in the starting point: a later shipping lookup must not mail it either.
        conn.executemany(
            'INSERT OR IGNORE INTO shipping_baselines VALUES (?,?)',
            [(sid, o.url) for o in offers if o.source == 'vinted' and o.shipping is None and o.url in baseline],
        )
        status.source_ok(conn, sid, source, now, len(offers))
        # The shared fetch leaves exclusions out; its reference is judged per search here.
        if source == 'idealo' and reference and title_fits(source, reference['title'], search):
            value = json.dumps(dict(reference, checked_at=now))
            conn.execute('INSERT OR REPLACE INTO idealo_references VALUES (?,?)', (sid, value))
        elif source == 'idealo':
            conn.execute('DELETE FROM idealo_references WHERE search_id=?', (sid,))
    return baseline


def enqueue(search, found, baseline):
    """New offers under the limit go to the outbox. The starting point, and anything found while mails
    are off, no mailserver is configured or the search is paused, only counts as known,
    so switching on later sends no flood."""
    mail = store.mail_settings()
    mailing = search['active'] and mail['enabled'] and mail['recipients'] and mailserver.configured()
    sid, now = search['id'], time.time()
    wanted = [offer for offer in found.values() if eligible(offer, search)]
    images.prioritize([offer.url for offer in wanted])
    with store.db() as conn:
        hidden = {r[0] for r in conn.execute('SELECT url FROM hidden WHERE search_id=?', (sid,))}
        reported = dict(conn.execute('SELECT url, price FROM notifications WHERE search_id=?', (sid,)))
        for offer in wanted:
            price = offer.compared_price(search)
            if offer.url in hidden:
                continue  # hidden by a swipe: never mailed
            if offer.url in reported and price >= reported[offer.url]:
                continue  # already reported at this price or cheaper
            if offer.url in baseline or not mailing:
                store.remember_reported(conn, sid, offer.url, price, now)
            else:
                store.queue(conn, sid, offer.url, price, now)


# Mail


def take_pending(conn: Connection) -> list[MailEntry]:
    """Outbox rows still worth a mail, as (search, offer, price), oldest first. Rows that are not
    (search gone or paused, offer gone, hidden, out of range, already reported) are deleted."""
    searches = {s['id']: s for s in store.read_searches(conn, 'preis')}
    entries, dropped = [], []
    rows = conn.execute(
        'SELECT x.search_id AS outbox_search, x.url AS outbox_url, x.created_at AS queued, o.*, '
        'n.price AS reported, h.url AS hidden '
        'FROM outbox x LEFT JOIN offers o ON o.search_id=x.search_id AND o.url=x.url '
        'LEFT JOIN notifications n ON n.search_id=x.search_id AND n.url=x.url '
        'LEFT JOIN hidden h ON h.search_id=x.search_id AND h.url=x.url '
        "WHERE x.batch IS NULL AND x.search_id IN (SELECT id FROM searches WHERE kind='preis') ORDER BY x.created_at"
    ).fetchall()
    now = time.time()
    for row in rows:
        search = searches.get(row['outbox_search'])
        if row['source'] == 'vinted' and row['shipping'] is None and row['queued'] > now - SHIPPING_WAIT:
            continue  # the shipping lookup comes within a minute or two; the mail should name it
        price = compared(row, search) if row['url'] and search else None
        if (
            search
            and search['active']
            and not row['hidden']
            and price is not None  # unknown shipping that counts: no comparable price
            and in_range(price, search, free(row['price'], search))
            and title_fits(row['source'], row['title'], search)
            and not (row['reported'] is not None and price >= row['reported'])
        ):
            offer = dict(row)
            for column in ('outbox_search', 'outbox_url', 'queued', 'reported', 'hidden'):
                del offer[column]
            entries.append((search, offer, price))
        else:
            dropped.append((row['outbox_search'], row['outbox_url']))
    store.drop_queued(conn, dropped)
    return entries


def used_floor(conn, search, now):
    """Cheapest matching second-hand offer of a search, from successful and recent checks only."""
    rows = conn.execute(
        'SELECT o.* FROM offers o JOIN source_status s ON s.search_id=o.search_id AND s.source=o.source '
        "WHERE o.search_id=? AND o.source!='idealo' AND s.error IS NULL AND s.checked_at>=? AND o.last_seen>=s.checked_at",
        (search['id'], now - FRESH_USED),
    ).fetchall()
    prices = (compared(row, search) for row in rows)
    return min((p for p in prices if p is not None and p >= search.get('min_price', 0)), default=None)


def explain(conn: Connection, entries: list[MailEntry], now: float) -> None:
    """A real shop offer cheaper than every current second-hand hit says so, with the saving."""
    for search, offer, price in entries:
        floor = used_floor(conn, search, now) if offer['source'] == 'idealo' else None
        if floor is not None and price < floor:
            offer['compare'] = {'used': floor, 'saving': floor - price}


# Background work of the loop


async def idle() -> None:
    """One Vinted shipping lookup when no search is due."""
    await update_shipping()


def queue_mails() -> None:
    """Product offers are queued as soon as they are found (finish)."""


def cleanup() -> int:
    """Nothing kept beyond the stored results."""
    return 0


def _vinted_searches():
    return {s['id']: s for s in store.searches('preis') if 'vinted' in s['sources']}


def _fresh_vinted_urls(searches):
    """Vinted offers from recent successful checks of active searches; only these get a detail lookup."""
    with store.db() as conn:
        rows = conn.execute(
            'SELECT o.search_id,o.url FROM offers o JOIN source_status s ON s.search_id=o.search_id AND s.source=o.source '
            "WHERE o.source='vinted' AND s.error IS NULL AND s.checked_at>=? AND o.last_seen>=s.checked_at",
            (time.time() - FRESH_USED,),
        )
        return {r['url'] for r in rows if r['search_id'] in searches and searches[r['search_id']]['active']}


def _apply_shipping(url, amount):
    """Share the shipping price with paused searches too; enqueue() only counts their offers as known."""
    searches = _vinted_searches()
    with store.db() as conn:
        rows = [
            r
            for r in conn.execute("SELECT * FROM offers WHERE source='vinted' AND url=?", (url,)).fetchall()
            if r['search_id'] in searches and title_fits('vinted', r['title'], searches[r['search_id']])
        ]
        baselines = {r[0] for r in conn.execute('SELECT search_id FROM shipping_baselines WHERE url=?', (url,))}
        if amount is not None:
            conn.executemany(
                'DELETE FROM shipping_baselines WHERE search_id=? AND url=?', [(r['search_id'], url) for r in rows]
            )
        conn.executemany(
            'UPDATE offers SET shipping=? WHERE search_id=? AND url=?', [(amount, r['search_id'], url) for r in rows]
        )
        conn.execute("UPDATE saved SET shipping=? WHERE source='vinted' AND url=?", (amount, url))
    if amount is None:
        return
    for row in rows:
        offer = Offer.from_row(dict(row, shipping=amount))
        enqueue(searches[row['search_id']], {url: offer}, {url} if row['search_id'] in baselines else set())


async def update_shipping():
    """One Vinted detail lookup, in a free slot of the shared page budget."""
    result = await delivery.step(_fresh_vinted_urls(_vinted_searches()), sources.fetch_shipping)
    if result is not None and not store.paused():
        _apply_shipping(*result)


# The API


def reset(conn: Connection, search_id: int, old: Mapping[str, Any], payload: Search) -> None:
    """Drop what an edit makes invalid. Pausing a search or dropping a source keeps the remaining hits:
    a paused search is not checked again on its own, so wiping them would empty its list."""
    changed = any(old[name] != payload.comparable(name) for name in SEARCH_FILTERS)
    dropped = [source for source in old['sources'] if source not in payload.sources]
    if old['negotiable'] and not payload.negotiable:
        conn.execute(
            'DELETE FROM outbox WHERE search_id=? AND batch IS NULL '
            'AND url IN (SELECT url FROM negotiable_offers WHERE search_id=?)',
            (search_id, search_id),
        )
    # A different query or price basis makes earlier reports incomparable.
    if (
        old['include_shipping'] != payload.include_shipping
        or old['include_fee'] != payload.include_fee
        or all_terms(old) != payload.terms()
    ):
        store.forget_reports(conn, search_id)
    conn.executemany('DELETE FROM offers WHERE search_id=? AND source=?', [(search_id, source) for source in dropped])
    if changed:
        store.keep_waiting_only(conn, 'offers', search_id)
    removed = old['sources'] if changed else dropped
    status.forget(conn, search_id, removed)
    if 'idealo' in removed:
        conn.execute('DELETE FROM idealo_references WHERE search_id=?', (search_id,))


def search_url(source: str, search: store.Search) -> str | None:
    """The provider's result page of a search, for the settings; None while Kleinanzeigen lacks the location ID."""
    try:
        return sources.search_url(source, wanted_words(source, search), search)
    except LookupError:
        return None


def extras(searches: list[store.Search]) -> dict[str, Any]:
    """The words hiding taught each category, shown in the editor to take back."""
    return {'learned': learning.by_category(searches)}


def hits(searches: list[store.Search], hidden: Collection[tuple[int, str]]) -> list[Row]:
    """Stored offers of these searches without junk below a search's minimum price and without hidden ones.
    Each search gets its idealo new price as 'reference' (None without one)."""
    by_id = {s['id']: s for s in searches}
    with store.db() as conn:
        for row in conn.execute('SELECT * FROM idealo_references'):
            if row['search_id'] in by_id:
                by_id[row['search_id']]['reference'] = json.loads(row['value'])
        start = {(r['search_id'], r['url']) for r in conn.execute('SELECT search_id, url FROM start_offers')}
        offers = []
        for row in conn.execute('SELECT * FROM offers ORDER BY price'):
            key = (row['search_id'], row['url'])
            search = by_id.get(row['search_id'])
            if search and key not in hidden and not too_cheap(Offer.from_row(row), search):
                offers.append(judged(dict(row, start=key in start), search))
    for search in searches:
        search.setdefault('reference', None)
    return images.enrich(offers)


def saved(rows: list[Row], searches: list[store.Search]) -> list[Row]:
    """The kind's watch-list snapshots as the page shows them, judged by their search like hits."""
    by_id = {s['id']: s for s in searches}
    return [judged(row, by_id.get(row['search_id'])) for row in rows]


def bookmark(conn: Connection, search: store.Search, url: str) -> bool:
    """Keep a snapshot on the watch list; False if the offer is gone."""
    return bool(
        conn.execute(
            'INSERT OR REPLACE INTO saved(url,search_id,source,title,price,shipping,seller,condition,saved_at) '
            'SELECT url,search_id,source,title,price,shipping,seller,condition,? FROM offers WHERE search_id=? AND url=?',
            (time.time(), search['id'], url),
        ).rowcount
    )


def samples(searches: list[store.Search], limit: int) -> list[MailSample]:
    """Recent offers that match their search, so a test mail shows the real layout."""
    by_id = {s['id']: s for s in searches}
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM offers ORDER BY last_seen DESC LIMIT 200')]
    found: list[MailSample] = []
    for offer in rows:
        search = by_id.get(offer['search_id'])
        if search and in_range(compared(offer, search), search, free(offer['price'], search)):
            found.append((search, offer))
            if len(found) == limit:
                break
    return found


def clear(conn: Connection) -> int:
    """Forget every stored offer; the record of reported offers stays, so nothing is mailed twice."""
    return conn.execute('DELETE FROM offers').rowcount
