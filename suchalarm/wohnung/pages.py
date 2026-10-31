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
from ..status import STATUS_COLUMNS
from . import sources
from .details import FETCH_TIMEOUT, read_again
from .domain import WHOLE_STOCK, Listing, cheaper, could_match, profile

log = logging.getLogger('suchalarm')


FULL_PAGE = 15  # a page of at least this many flats, all new, may have pushed more new ones onto the next

MAX_PAGE = 3

STOCK_PAGES = 50  # a new search takes in the whole stock; this only stops a portal that never runs out

MORE_TRIES = 2


def save_result(search, source, listings, now):
    """Store a successful result; queue detail pages for new listings.

    bauverein and NHW list their whole stock, so a flat missing there is let: it goes. The other portals
    are read on their first page (newest first), after the first result also on the pages behind it: a flat
    missing there may just have moved down, so it stays until cleanup() finds it unseen for KEEP_LISTINGS.
    Returns the URLs of genuinely new listings (not the starting stock)."""
    sid, wanted = search['id'], profile(search)
    with store.db() as conn:
        seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
        succeeded = conn.execute(
            'SELECT 1 FROM source_status WHERE search_id=? AND source=? AND error IS NULL', (sid, source)
        ).fetchone()
        baseline = {listing.url for listing in listings} if not seen and not succeeded else set()
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in baseline])
        new = _store(
            conn, sid, source, listings, seen | dict.fromkeys(baseline, now), wanted, now, sources.centre(search)
        )
        if source in WHOLE_STOCK:
            _drop_let(conn, sid, source, listings)
        conn.execute(STATUS_COLUMNS + 'VALUES (?,?,?,NULL,?,0)', (sid, source, now, len(listings)))
        if baseline:
            _more_after(conn, sid, source, 1, listings, baseline, stock=True)
        elif not _reading_stock(conn, sid, source):
            _more_after(conn, sid, source, 1, listings, new)
    return new


def _reading_stock(conn, sid, source):
    return conn.execute('SELECT 1 FROM more_pages WHERE search_id=? AND source=? AND stock=1', (sid, source)).fetchone()


def _drop_let(conn, sid, source, listings):
    """A landlord's flat missing from its whole stock is let."""
    listed = {item.url for item in listings}
    stored = [row[0] for row in conn.execute('SELECT url FROM listings WHERE search_id=? AND source=?', (sid, source))]
    conn.executemany(
        'DELETE FROM listings WHERE search_id=? AND url=?', [(sid, url) for url in stored if url not in listed]
    )


def _store(conn, sid, source, listings, seen, wanted, now, middle=None):
    """Write listings (SEEN: url -> first_seen of those known); queue detail pages of new ones that can
    still fit. Returns the new URLs."""
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
    new = [item for item in listings if item.url not in seen]
    conn.executemany(
        "INSERT OR IGNORE INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?)",
        [(item.url, source, now) for item in new if source in sources.DETAILS and could_match(item, wanted, middle)],
    )
    return {item.url for item in new}


def _mark_cheaper(conn, sid, source, urls, now):
    """Listings that now ask less rent: their detail page is read (again; it states the rent that counts), and
    the starting stock no longer keeps them out of the mail. outbox.py mails them if the rent is below what
    was last mailed."""
    conn.executemany('UPDATE listings SET cheaper_at=? WHERE search_id=? AND url=?', [(now, sid, url) for url in urls])
    conn.executemany('DELETE FROM start_offers WHERE search_id=? AND url=?', [(sid, url) for url in urls])
    if source in sources.DETAILS:
        read_again(conn, source, urls, now)


def _more_after(conn, sid, source, page, listings, unseen, stock=False):
    """Queue the next page after a full one. While a new search takes in the STOCK, as long as pages bring
    flats not seen yet; otherwise only after a page of nothing but new flats, which may have pushed more down."""
    more = bool(unseen) if stock else len(unseen) == len(listings)
    last = STOCK_PAGES if stock else MAX_PAGE
    if source in sources.PAGED and page < last and len(listings) >= FULL_PAGE and more:
        conn.execute(
            'INSERT OR REPLACE INTO more_pages(search_id,source,page,tries,stock) VALUES (?,?,?,0,?)',
            (sid, source, page + 1, int(stock)),
        )
    else:
        conn.execute('DELETE FROM more_pages WHERE search_id=? AND source=?', (sid, source))


def save_more(search, source, page, listings, now):
    """Store a further result page. Its flats are starting stock while a new search takes in the stock,
    otherwise its new flats count like any others. Returns the URLs of new ones."""
    sid = search['id']
    with store.db() as conn:
        due = conn.execute(
            'SELECT page, stock FROM more_pages WHERE search_id=? AND source=?', (sid, source)
        ).fetchone()
        if due is None or due['page'] != page:
            return set()  # a regular check decided anew in the meantime
        seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
        stock = {item.url for item in listings if item.url not in seen} if due['stock'] else set()
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in stock])
        new = _store(
            conn, sid, source, listings, seen | dict.fromkeys(stock, now), profile(search), now, sources.centre(search)
        )
        _more_after(conn, sid, source, page, listings, stock if due['stock'] else new, bool(due['stock']))
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
