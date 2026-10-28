"""Storing result pages: the first one of a portal is the starting stock; a full page of nothing but new
flats may have pushed more onto the next page (more_step)."""

import asyncio
import json
import logging
import time
from dataclasses import asdict

from .. import store
from ..core import cadence
from ..status import STATUS_COLUMNS
from . import sources
from .details import FETCH_TIMEOUT
from .domain import WHOLE_STOCK, could_match, profile

log = logging.getLogger('suchalarm')


FULL_PAGE = 15  # a page of at least this many flats, all new, may have pushed more new ones onto the next

MAX_PAGE = 3

MORE_TRIES = 2


def save_result(search, source, listings, now):
    """Store a successful result; queue detail pages for new listings.

    bauverein and NHW list their whole stock, so a flat missing there is let: it goes. The other portals
    are read on their first page only (newest first): a flat missing there may just have moved down, so
    it stays until cleanup() finds it unseen for KEEP_LISTINGS.
    Returns the URLs of genuinely new listings (not the starting stock)."""
    sid, wanted = search['id'], profile(search)
    with store.db() as conn:
        seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
        succeeded = conn.execute(
            'SELECT 1 FROM source_status WHERE search_id=? AND source=? AND error IS NULL', (sid, source)
        ).fetchone()
        baseline = {listing.url for listing in listings} if not seen and not succeeded else set()
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in baseline])
        if source in WHOLE_STOCK:
            conn.execute('DELETE FROM listings WHERE search_id=? AND source=?', (sid, source))
        new = _store(
            conn, sid, source, listings, seen | dict.fromkeys(baseline, now), wanted, now, sources.centre(search)
        )
        conn.execute(STATUS_COLUMNS + 'VALUES (?,?,?,NULL,?,0)', (sid, source, now, len(listings)))
        _more_after(conn, sid, source, 1, listings, new)
    return new


def _store(conn, sid, source, listings, seen, wanted, now, middle=None):
    """Write listings (SEEN: url -> first_seen of those known); queue detail pages of new ones that can
    still fit. Returns the new URLs."""
    conn.executemany(
        'INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?)',
        [(sid, source, item.url, json.dumps(asdict(item)), seen.get(item.url) or now, now) for item in listings],
    )
    new = [item for item in listings if item.url not in seen]
    conn.executemany(
        "INSERT OR IGNORE INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?)",
        [(item.url, source, now) for item in new if source in sources.DETAILS and could_match(item, wanted, middle)],
    )
    return {item.url for item in new}


def _more_after(conn, sid, source, page, listings, new):
    """Like the Wohnungsbot: a full page of nothing but new flats may have pushed more onto the next page."""
    if source in sources.PAGED and page < MAX_PAGE and len(listings) >= FULL_PAGE and len(new) == len(listings):
        conn.execute('INSERT OR REPLACE INTO more_pages VALUES (?,?,?,0)', (sid, source, page + 1))
    else:
        conn.execute('DELETE FROM more_pages WHERE search_id=? AND source=?', (sid, source))


def save_more(search, source, page, listings, now):
    """Store a further result page; its new flats count like any others. Returns their URLs."""
    sid = search['id']
    with store.db() as conn:
        due = conn.execute('SELECT page FROM more_pages WHERE search_id=? AND source=?', (sid, source)).fetchone()
        if due is None or due['page'] != page:
            return set()  # a regular check decided anew in the meantime
        seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
        new = _store(conn, sid, source, listings, seen, profile(search), now, sources.centre(search))
        _more_after(conn, sid, source, page, listings, new)
    return new


async def more_step(now=None):
    """At most one further result page, of a search whose site may be visited now. Returns the job or None."""
    now = time.time() if now is None else now
    with store.db() as conn:
        rows = conn.execute('SELECT * FROM more_pages').fetchall()
    job = next((row for row in rows if cadence.ready_at(row['source'], now=now) <= now), None)
    if job is None:
        return None
    search = store.search(job['search_id'])
    if not search:
        return None
    where = (job['search_id'], job['source'])
    try:
        listings = await asyncio.wait_for(sources.more(job['source'], search, job['page']), timeout=FETCH_TIMEOUT)
    except cadence.Deferred:
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        refused = isinstance(exc, cadence.Refused)
        if refused:
            cadence.outcome(job['source'], 'page', True)
        with store.db() as conn:
            if refused or job['tries'] + 1 >= MORE_TRIES:
                conn.execute('DELETE FROM more_pages WHERE search_id=? AND source=?', where)
            else:
                conn.execute('UPDATE more_pages SET tries=tries+1 WHERE search_id=? AND source=?', where)
        log.warning('Page %s of %s failed (%s)', job['page'], job['source'], type(exc).__name__)
        return job
    cadence.outcome(job['source'], 'page')
    save_more(search, job['source'], job['page'], listings, time.time())
    return job
