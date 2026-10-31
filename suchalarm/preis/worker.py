"""Checking product searches: store each provider's offers, queue new ones for the mail, Vinted shipping.

The steps the shared check loop (worker.check) asks of a search kind: prepare, rests, fetch, save, finish;
then take_pending and explain for the mail, idle, queue_mails and cleanup for the loop.
"""

import json
import time

from .. import store
from ..core import images
from ..status import source_ok, started
from . import delivery, learning, sources
from . import mail as mail  # the kind's mail presentation (kinds.py)
from .domain import (
    CATEGORIES,
    PICKUP_SOURCES,
    PLATFORMS,
    RADII,
    Offer,
    all_terms,
    compared_price,
    eligible,
    free,
    in_range,
    search_fee,
    title_fits,
    too_cheap,
    wanted_words,
    with_shipping,
)
from .domain import SOURCES as SOURCES  # the kind's source labels (kinds.py)
from .models import SEARCH_FILTERS, Search

FRESH_USED = 3 * 3600  # a shop comparison only uses second-hand results this recent
SHIPPING_WAIT = 15 * 60  # a Vinted hit waits this long for its shipping lookup before it is mailed without
HITS = 'offers'  # key of the hit rows in /api/state
MODEL = Search  # the request body of a product search
EMPTY_IS_SUSPICIOUS = False  # an empty product result is ordinary; a changed layout is caught by markup_changed
# What the search editor offers; the providers' section IDs stay on the server.
CHOICES = {
    'categories': {key: {k: v for k, v in value.items() if k != 'portal'} for key, value in CATEGORIES.items()},
    'platforms': PLATFORMS,
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


# Checking a search


async def prepare(search):
    """What hiding taught the search's category filters this check too (domain.excluded_words)."""
    search['learned'] = learning.learned(search)


def rests(search, source):
    """Product providers never rest for a search."""
    return None


async def fetch(source, search):
    return await sources.fetch(source, search)


def save(search, source, offers, reference, observed):
    """Store a successful result; returns (offers, the starting point's URLs) for finish()."""
    return offers, save_result(search, source, offers, reference, observed)


def finish(search, saved):
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
        baseline = current - waiting if not (seen.keys() - waiting) and not started(conn, sid, source) else set()
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
        # Vinted shipping still unknown in the starting point: a later shipping lookup must not mail it either.
        conn.executemany(
            'INSERT OR IGNORE INTO shipping_baselines VALUES (?,?)',
            [(sid, o.url) for o in offers if o.source == 'vinted' and o.shipping is None and o.url in baseline],
        )
        source_ok(conn, sid, source, now, len(offers))
        if source == 'idealo' and reference:
            value = json.dumps(dict(reference, checked_at=now))
            conn.execute('INSERT OR REPLACE INTO idealo_references VALUES (?,?)', (sid, value))
        elif source == 'idealo':
            conn.execute('DELETE FROM idealo_references WHERE search_id=?', (sid,))
    return baseline


def enqueue(search, found, baseline):
    """New offers under the limit go to the outbox. The starting point, and anything found while mails
    are off or the search is paused, only counts as known, so switching on later sends no flood."""
    mail = store.mail_settings()
    mailing = search['active'] and mail['enabled'] and mail['recipients']
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
                conn.execute('INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)', (sid, offer.url, price, now))
                continue
            # A waiting entry only follows a further drop; one already being sent stays as it is.
            conn.execute(
                'INSERT INTO outbox(search_id,url,price,created_at) VALUES (?,?,?,?) '
                'ON CONFLICT(search_id,url) DO UPDATE SET price=excluded.price '
                'WHERE outbox.batch IS NULL AND excluded.price < outbox.price',
                (sid, offer.url, price, now),
            )


# Mail


def take_pending(conn):
    """Outbox rows still worth a mail, as (search, offer, price), oldest first. Rows that are not
    (search gone or paused, offer gone, hidden, out of range, already reported) are deleted."""
    searches = {s['id']: s for s in store.searches('preis')}
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
            and row['url']
            and title_fits(row['source'], row['title'], search)
            and in_range(price, search, free(row['price'], search))
            and not (row['reported'] is not None and price >= row['reported'])
        ):
            offer = dict(row)
            for column in ('outbox_search', 'outbox_url', 'queued', 'reported', 'hidden'):
                del offer[column]
            entries.append((search, offer, price))
        else:
            dropped.append((row['outbox_search'], row['outbox_url']))
    conn.executemany('DELETE FROM outbox WHERE search_id=? AND url=?', dropped)
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


def explain(conn, entries, now):
    """A real shop offer cheaper than every current second-hand hit says so, with the saving."""
    for search, offer, price in entries:
        floor = used_floor(conn, search, now) if offer['source'] == 'idealo' else None
        if floor is not None and price < floor:
            offer['compare'] = {'used': floor, 'saving': floor - price}


# Background work of the loop


async def idle():
    """One Vinted shipping lookup when no search is due."""
    await update_shipping()


def queue_mails():
    """Product offers are queued as soon as they are found (finish)."""


def cleanup():
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


def reset(conn, search_id, old, payload):
    """Drop what an edit makes invalid. Pausing a search or dropping a source keeps the remaining hits:
    a paused search is not checked again on its own, so wiping them would empty its list."""
    changed = any(old[name] != payload.comparable(name) for name in SEARCH_FILTERS)
    dropped = [source for source in old['sources'] if source not in payload.sources]
    # A different query or price basis makes earlier reports incomparable.
    if (
        old['include_shipping'] != payload.include_shipping
        or old['include_fee'] != payload.include_fee
        or all_terms(old) != payload.terms()
    ):
        conn.execute('DELETE FROM notifications WHERE search_id=?', (search_id,))
        conn.execute('DELETE FROM outbox WHERE search_id=?', (search_id,))
    conn.executemany('DELETE FROM offers WHERE search_id=? AND source=?', [(search_id, source) for source in dropped])
    if changed:
        # Offers waiting for the mail stay until it goes out; take_pending judges them by the new filters.
        conn.execute(
            'DELETE FROM offers WHERE search_id=? AND url NOT IN '
            '(SELECT url FROM outbox WHERE search_id=? AND batch IS NULL)',
            (search_id, search_id),
        )
    removed = old['sources'] if changed else dropped
    conn.executemany('DELETE FROM source_status WHERE search_id=? AND source=?', [(search_id, s) for s in removed])
    if 'idealo' in removed:
        conn.execute('DELETE FROM idealo_references WHERE search_id=?', (search_id,))


def search_url(source, search):
    """The provider's result page of a search, for the settings; None while Kleinanzeigen lacks the location ID."""
    try:
        return sources.search_url(source, wanted_words(source, search), search)
    except LookupError:
        return None


def extras(searches):
    """Product searches add nothing of their own to /api/state."""
    return {}


def hits(searches, hidden):
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
                offers.append(dict(row, start=key in start))
    for search in searches:
        search.setdefault('reference', None)
    return images.enrich(offers)


def bookmark(conn, search, url):
    """Keep a snapshot on the watch list; False if the offer is gone."""
    return bool(
        conn.execute(
            'INSERT OR REPLACE INTO saved(url,search_id,source,title,price,shipping,seller,condition,saved_at) '
            'SELECT url,search_id,source,title,price,shipping,seller,condition,? FROM offers WHERE search_id=? AND url=?',
            (time.time(), search['id'], url),
        ).rowcount
    )


def samples(searches, limit):
    """Recent offers that match their search, so a test mail shows the real layout."""
    by_id = {s['id']: s for s in searches}
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM offers ORDER BY last_seen DESC LIMIT 200')]
    found = []
    for offer in rows:
        search = by_id.get(offer['search_id'])
        if search and in_range(compared(offer, search), search, free(offer['price'], search)):
            found.append((search, offer))
            if len(found) == limit:
                break
    return found


def clear(conn):
    """Forget every stored offer; the record of reported offers stays, so nothing is mailed twice."""
    return conn.execute('DELETE FROM offers').rowcount
