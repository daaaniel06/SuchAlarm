"""Storing result pages: the first one of a portal is the starting stock, and a new search reads the
portal's further pages into it too, one per free slot (more_step). Later, a full page of nothing but new
flats may have pushed more onto the next page."""

import asyncio
import json
import logging
import time
from dataclasses import asdict

from .. import store
from ..core import cadence
from ..status import source_ok, started
from . import sources
from .details import FETCH_TIMEOUT, read_again
from .domain import WHOLE_STOCK, Listing, cheaper, could_match, profile

log = logging.getLogger('suchalarm')


FULL_PAGE = 15  # a portal page that held at least this many offers may have more behind it

MAX_PAGE = 3

STOCK_PAGES = 50  # a new search takes in the whole stock; this only stops a portal that never runs out

MORE_TRIES = 2


def save_result(search, source, listings, listed, now):
    """Store a successful result (LISTED: how many offers the portal's page held before select()); queue
    detail pages for new listings.

    bauverein and NHW list their whole stock, so a flat missing there is let: it goes. The other portals
    are read on their first page (newest first), after the first result also on the pages behind it: a flat
    missing there may just have moved down, so it stays until cleanup() finds it unseen for KEEP_LISTINGS.
    The first result, and a page 1 that read_stock_again() asked for, is starting stock.
    Returns the URLs of genuinely new listings (not the starting stock)."""
    sid = search['id']
    with store.db() as conn:
        job = _job(conn, sid, source)
        reading_stock = job is not None and bool(job['stock'])
        stock = _first_result(conn, sid, source) or (reading_stock and job['page'] == 1)
        unseen = _save_page(conn, search, source, listings, now, stock)
        if source in WHOLE_STOCK:
            _drop_let(conn, sid, source, listings)
        source_ok(conn, sid, source, now, len(listings))
        if stock or not reading_stock:  # the reading of the stock goes on behind a regular check
            _queue_next(conn, sid, source, 1, listings, listed, unseen, stock)
    return set() if stock else unseen


def save_more(search, source, page, listings, listed, now):
    """Store a further result page. Its unseen flats are starting stock while a search takes in the stock,
    otherwise they count like any new ones. Returns the URLs of new ones."""
    sid = search['id']
    with store.db() as conn:
        job = _job(conn, sid, source)
        if job is None or job['page'] != page:
            return set()  # a regular check decided anew in the meantime
        stock = bool(job['stock'])
        unseen = _save_page(conn, search, source, listings, now, stock)
        _queue_next(conn, sid, source, page, listings, listed, unseen, stock)
    return set() if stock else unseen


def read_stock_again(conn, sid, changed):
    """The portal request of the CHANGED sources (other limits): it may list older flats not seen yet. The next
    result reads the stock again from page 1, so they join the starting stock instead of being mailed."""
    conn.executemany(
        'INSERT OR REPLACE INTO more_pages(search_id,source,page,tries,stock) VALUES (?,?,1,0,1)',
        [(sid, source) for source in changed if source in sources.PAGED],
    )


def _job(conn, sid, source):
    return conn.execute('SELECT page, stock FROM more_pages WHERE search_id=? AND source=?', (sid, source)).fetchone()


def _first_result(conn, sid, source):
    known = conn.execute('SELECT 1 FROM listings WHERE search_id=? AND source=?', (sid, source)).fetchone()
    return not known and not started(conn, sid, source)


def _save_page(conn, search, source, listings, now, stock):
    """Store one result page; returns the URLs not seen before. With STOCK they join the starting stock,
    ranked below everything known in the portal's order (newest first), so the stock read page by page
    lists the freshest flats first."""
    sid = search['id']
    seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
    unseen = {item.url for item in listings if item.url not in seen}
    if stock:
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in unseen])
        oldest = min(seen.values(), default=now)
        ranked = [url for url in dict.fromkeys(item.url for item in listings) if url in unseen]
        seen |= {url: oldest - 1 - position for position, url in enumerate(ranked)}
    _store(conn, sid, source, listings, seen, profile(search), now, sources.centre(search))
    return unseen


def _drop_let(conn, sid, source, listings):
    """A landlord's flat missing from its whole stock is let."""
    listed = {item.url for item in listings}
    stored = [row[0] for row in conn.execute('SELECT url FROM listings WHERE search_id=? AND source=?', (sid, source))]
    conn.executemany(
        'DELETE FROM listings WHERE search_id=? AND url=?', [(sid, url) for url in stored if url not in listed]
    )


def _store(conn, sid, source, listings, seen, wanted, now, middle=None):
    """Write listings (SEEN: url -> first_seen of those known); queue detail pages of new ones that can
    still fit."""
    before = {
        url: Listing(**json.loads(body))
        for url, body in conn.execute('SELECT url, body FROM listings WHERE search_id=? AND source=?', (sid, source))
    }
    conn.executemany(
        'INSERT INTO listings(search_id,source,url,body,first_seen,last_seen) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(search_id,url) DO UPDATE SET body=excluded.body, last_seen=excluded.last_seen',
        [(sid, source, item.url, json.dumps(asdict(item)), seen.get(item.url) or now, now) for item in listings],
    )
    dropped = [item.url for item in listings if item.url in before and cheaper(before[item.url], item)]
    _mark_cheaper(conn, sid, source, dropped, now)
    conn.executemany(
        "INSERT OR IGNORE INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?)",
        [
            (item.url, source, now)
            for item in listings
            if item.url not in seen and source in sources.DETAILS and could_match(item, wanted, middle)
        ],
    )


def _mark_cheaper(conn, sid, source, urls, now):
    """Listings that now ask less rent: their detail page is read (again; it states the rent that counts), and
    the starting stock no longer keeps them out of the mail. outbox.py mails them if the rent is below what
    was last mailed."""
    conn.executemany('UPDATE listings SET cheaper_at=? WHERE search_id=? AND url=?', [(now, sid, url) for url in urls])
    conn.executemany('DELETE FROM start_offers WHERE search_id=? AND url=?', [(sid, url) for url in urls])
    if source in sources.DETAILS:
        read_again(conn, source, urls, now)


def _queue_next(conn, sid, source, page, listings, listed, unseen, stock):
    """Queue the page after PAGE, or end the reading. Only a page that was full (LISTED offers before the
    search's own place filter) can have one behind it. While a search takes in its STOCK, reading goes on as
    long as pages bring flats not seen yet (a page of only other places' offers says nothing about the end);
    otherwise only after a page of nothing but new flats, which may have pushed more down."""
    full = source in sources.PAGED and listed >= FULL_PAGE
    if stock:
        more, last = full and (bool(unseen) or not listings), STOCK_PAGES
    else:
        more, last = full and bool(listings) and unseen == {item.url for item in listings}, MAX_PAGE
    if more and page < last:
        conn.execute(
            'INSERT OR REPLACE INTO more_pages(search_id,source,page,tries,stock) VALUES (?,?,?,0,?)',
            (sid, source, page + 1, int(stock)),
        )
        return
    if more and stock:
        log.warning('Reading the stock of %s stopped after %s pages; narrow the search to see the rest', source, last)
    conn.execute('DELETE FROM more_pages WHERE search_id=? AND source=?', (sid, source))


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
        found, listed = await asyncio.wait_for(sources.more(job['source'], search, job['page']), timeout=FETCH_TIMEOUT)
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
    save_more(search, job['source'], job['page'], found, listed, time.time())
    return job
