"""Checking flat searches: store each portal's listings, then complete new ones from their detail page.

The first successful result of a portal is the search's starting stock (no detail
requests, never "new"). Listings that appear after it and can still fit get their detail page read
once, at most one page per free budget slot, for warm rent, description and move-in date. When nothing
else waits, the starting stock that can still fit gets its detail pages too, slowly (backfill_step).
"""

import logging
import time

from .. import store
from ..core import cadence, images
from . import details, maps, pages, sources
from . import mail as mail  # the kind's mail presentation (kinds.py)
from .catalog import listings
from .domain import SOURCES as SOURCES  # the kind's source labels (kinds.py)
from .domain import TRAITS, place_query, profile, query_text
from .models import FLAT_RADII, FlatSearch
from .outbox import queue_mails as queue_mails  # the kind's mail queue (kinds.py)
from .outbox import take_pending as take_pending

log = logging.getLogger('suchalarm')


# The steps of the shared check loop (worker.check); see preis/worker.py.

EMPTY_IS_SUSPICIOUS = True  # a portal that suddenly lists nothing usually changed its layout
HITS = 'listings'  # key of the hit rows in /api/state
MODEL = FlatSearch  # the request body of a flat search
CHOICES = {'flat_radii': FLAT_RADII, 'traits': TRAITS}  # what the flat editor offers


async def prepare(search):
    """Radius, postcode and regional portals need the place's centre; until it is known, names count."""
    if sources.needs_place(search):
        try:
            await sources.locate(place_query(profile(search)))
        except cadence.Deferred:
            pass
        except Exception:
            log.warning('Place lookup failed for search %s', search['id'])


def rests(search, source):
    return sources.out_of_area(search, source)


async def fetch(source, search):
    return await sources.fetch(source, search)


def save(search, source, listings, listed, observed):
    return pages.save_result(search, source, listings, listed, observed)


def finish(search, saved):
    """New flats are mailed once their detail page is read (queue_mails)."""


async def idle():
    """A further result page after a burst, one detail page and a few map tiles when no search is due."""
    await pages.more_step()
    details.backfill_step()
    await details.detail_step()
    await maps.build_step(store.searches('wohnung'))


def explain(conn, entries, now):
    """Flat mails need no comparison."""


KEEP_LISTINGS = 30 * 86400  # listings unseen this long are forgotten (the watch list keeps its snapshot)
KEEP_DETAILS = 30 * 86400  # detail pages of listings no search lists any more go after this


def cleanup(now=None):
    """Forget listings unseen for a month, detail pages nothing lists any more and unused maps; returns
    how many rows."""
    now = time.time() if now is None else now
    maps.cleanup(store.searches('wohnung'))
    with store.db() as conn:
        gone = conn.execute('DELETE FROM listings WHERE last_seen<?', (now - KEEP_LISTINGS,)).rowcount
        return (
            gone
            + conn.execute(
                'DELETE FROM listing_details WHERE created_at<? AND url NOT IN (SELECT url FROM listings)',
                (now - KEEP_DETAILS,),
            ).rowcount
        )


# The API


def reset(conn, search_id, old, payload):
    """Another place or radius makes the stored flats wrong; other filters are judged per listing. A portal
    that applies a changed limit itself may now list older flats: its stock is read again."""
    moved = old['query'] != payload.query or old['radius'] != payload.radius
    dropped = [source for source in old['sources'] if source not in payload.sources]
    removed = old['sources'] if moved else dropped
    for table in ('source_status', 'more_pages'):
        conn.executemany(f'DELETE FROM {table} WHERE search_id=? AND source=?', [(search_id, s) for s in removed])
    conn.executemany('DELETE FROM listings WHERE search_id=? AND source=?', [(search_id, s) for s in dropped])
    if moved:
        store.keep_waiting_only(conn, 'listings', search_id)
        conn.execute('DELETE FROM start_offers WHERE search_id=?', (search_id,))
        return
    new = store.read_search(conn, search_id)
    kept = [source for source in old['sources'] if source in payload.sources]
    pages.read_stock_again(
        conn, search_id, [source for source in kept if query_text(source, old) != query_text(source, new)]
    )


def search_url(source, search):
    """Portals are read through their own query, not a result page to open."""
    return None


def extras(searches):
    """The ready maps of the flat searches, for the hit list's sheet."""
    return {'maps': maps.areas(searches)}


def hits(searches, hidden):
    """Flats of these searches without hidden ones, in the row shape of the hit list."""
    rows = [row for search in searches for row in listings(search) if (search['id'], row['url']) not in hidden]
    return images.enrich(rows)


def bookmark(conn, search, url):
    """Keep a snapshot on the watch list: price and the detail line stay readable once the portal drops
    the flat. False if the flat is gone."""
    flat = next((row for row in listings(search) if row['url'] == url), None)
    if flat is None:
        return False
    conn.execute(
        'INSERT OR REPLACE INTO saved(url,search_id,source,title,price,shipping,seller,condition,saved_at,detail,note) '
        "VALUES (?,?,?,?,?,NULL,'','',?,?,?)",
        (url, search['id'], flat['source'], flat['title'], flat['price'] or 0, time.time(),
         ' · '.join(mail.details(flat)), 'geschätzt' if flat['estimated'] else ''),
    )  # fmt: skip
    return True


def samples(searches, limit):
    """The first fitting flat of each search, so a test mail shows the real layout."""
    found = []
    for search in searches:
        flat = next((row for row in listings(search) if row['status'] == 'matched'), None)
        if flat:
            found.append((search, flat))
        if len(found) == limit:
            break
    return found


def clear(conn):
    """Forget every listing; flat searches start over with a silent first fetch (the stock is taken without
    mails or detail pages)."""
    count = conn.execute('DELETE FROM listings').rowcount
    conn.execute('DELETE FROM listing_details')
    conn.execute('DELETE FROM more_pages')
    conn.execute('DELETE FROM separated')
    conn.execute("DELETE FROM source_status WHERE search_id IN (SELECT id FROM searches WHERE kind='wohnung')")
    conn.execute("DELETE FROM start_offers WHERE search_id IN (SELECT id FROM searches WHERE kind='wohnung')")
    return count
