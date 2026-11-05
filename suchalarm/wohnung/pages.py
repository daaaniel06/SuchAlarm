"""Storing result pages: the first one of a portal is the starting stock, and a new search reads the
portal's further pages into it too, one per free slot (more_step). Later, a full page of nothing but new
flats may have pushed more onto the next page."""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass

from .. import store
from ..core import cadence
from ..status import more_unread, source_ok, started
from . import sources
from .details import FETCH_TIMEOUT, GONE_RECHECK, read_again
from .domain import WHOLE_STOCK, Listing, cheaper, could_match, other_rent, profile
from .tables import GONE

log = logging.getLogger('suchalarm')


FULL_PAGE = 15  # fallback for portals without a known page size

MAX_PAGE = 3

STOCK_PAGES = 50  # a new search takes in the whole stock; this only stops a portal that never runs out

MORE_TRIES = 2

# more_pages.stock: 0 a page behind a burst of new flats, 1 the starting stock, REREAD the stock read again after
# the portal's own limits changed: there every full page counts, since a page of known flats proves no end.
REREAD = 2


@dataclass(frozen=True)
class Page:
    """One result page of a portal for a search: NUMBER (1 the newest), its LISTINGS after the search's own place
    filter, how many offers the portal's page held before it (LISTED) and how many of the portal's own count its
    parser missed (UNREAD, None where the portal names no count)."""

    source: str
    number: int
    listings: list[Listing]
    listed: int
    unread: int | None = None


def save_result(search, source, listings, listed, now, unread=None, warning=None):
    """Store a successful result (LISTED: how many offers the portal's page held before select(); UNREAD: how many
    of the portal's own count the parser missed, sources.unread); queue
    detail pages for new listings.

    bauverein and NHW list their whole stock, so a flat missing there is let: it goes. The other portals
    are read on their first page (newest first), after the first result also on the pages behind it: a flat
    missing there may just have moved down, so it stays until cleanup() finds it unseen for KEEP_LISTINGS.
    The first result, and a page 1 that read_stock_again() asked for, is starting stock.
    Returns the URLs of genuinely new listings (not the starting stock)."""
    sid, page = search['id'], Page(source, 1, listings, listed, unread)
    with store.db() as conn:
        job = _job(conn, sid, source)
        reading_stock = job is not None and bool(job['stock'])
        stock = _first_result(conn, sid, source) or (reading_stock and job['page'] == 1)
        unseen = _save_page(conn, search, page, now, stock)
        if source in WHOLE_STOCK:
            _drop_let(conn, sid, source, listings)
        source_ok(conn, sid, source, now, len(listings), unread, warning)
        if stock or not reading_stock:  # the reading of the stock goes on behind a regular check
            _queue_next(conn, sid, page, unseen, stock, reread=bool(job) and job['stock'] == REREAD)
    return set() if stock else unseen


def save_more(search, job, listings, listed, now, unread=None, warning=None):
    """Store the further result page that JOB (a more_pages row) asked for. Its unseen flats are starting
    stock while a search takes in the stock, otherwise they count like any new ones. Returns the URLs of
    new ones."""
    sid, page = search['id'], Page(job['source'], job['page'], listings, listed, unread)
    with store.db() as conn:
        if not _current(conn, job):
            return set()  # a regular check or an edit decided anew while the page loaded
        more_unread(conn, sid, page.source, unread, warning)
        stock = bool(job['stock'])
        unseen = _save_page(conn, search, page, now, stock)
        _queue_next(conn, sid, page, unseen, stock, reread=job['stock'] == REREAD)
    return set() if stock else unseen


def read_stock_again(conn, sid, changed):
    """The portal request of the CHANGED sources (other limits): it may list older flats not seen yet. The next
    result reads the stock again from page 1, so they join the starting stock instead of being mailed."""
    for source in changed:
        if source in sources.PAGED:
            _queue(conn, sid, source, 1, stock=REREAD)


def _queue(conn, sid, source, page, stock):
    """Ask for PAGE, replacing the source's job. The new job id tells a page still loading for the old one."""
    conn.execute(
        'INSERT OR REPLACE INTO more_pages(search_id,source,page,tries,stock,job) VALUES (?,?,?,0,?,?)',
        (sid, source, page, int(stock), uuid.uuid4().hex),
    )


def _job(conn, sid, source):
    return conn.execute('SELECT page, stock FROM more_pages WHERE search_id=? AND source=?', (sid, source)).fetchone()


def _current(conn, job):
    """Whether JOB is still the one its search and source wait for."""
    return conn.execute('SELECT 1 FROM more_pages WHERE job=?', (job['job'],)).fetchone() is not None


def _first_result(conn, sid, source):
    """Whether this is the source's first result: a flat kept only for its mail (reset) does not count."""
    known = conn.execute(
        'SELECT 1 FROM listings l WHERE search_id=? AND source=? AND NOT EXISTS '
        '(SELECT 1 FROM outbox x WHERE x.search_id=l.search_id AND x.url=l.url AND x.batch IS NULL)',
        (sid, source),
    ).fetchone()
    return not known and not started(conn, sid, source)


def _save_page(conn, search, page, now, stock):
    """Store one result PAGE; returns the URLs not seen before. With STOCK they join the starting stock,
    ranked below everything known in the portal's order (newest first), so the stock read page by page
    lists the freshest flats first."""
    sid, source, listings = search['id'], page.source, page.listings
    seen = dict(conn.execute('SELECT url, first_seen FROM listings WHERE search_id=? AND source=?', (sid, source)))
    unseen = {item.url for item in listings if item.url not in seen}
    if stock:
        conn.executemany('INSERT OR IGNORE INTO start_offers VALUES (?,?)', [(sid, url) for url in unseen])
        oldest = min(seen.values(), default=now)
        ranked = [url for url in dict.fromkeys(item.url for item in listings) if url in unseen]
        seen |= {url: oldest - 1 - position for position, url in enumerate(ranked)}
    _store(conn, search, page, seen, now)
    return unseen


def _drop_let(conn, sid, source, listings):
    """A landlord's flat missing from its whole stock is let."""
    listed = {item.url for item in listings}
    stored = [row[0] for row in conn.execute('SELECT url FROM listings WHERE search_id=? AND source=?', (sid, source))]
    conn.executemany(
        'DELETE FROM listings WHERE search_id=? AND url=?', [(sid, url) for url in stored if url not in listed]
    )


def _store(conn, search, page, seen, now):
    """Write the PAGE's listings (SEEN: url -> first_seen); queue missing details of fitting listings beyond the
    stock."""
    sid, source, listings = search['id'], page.source, page.listings
    wanted, middle = profile(search), sources.centre(search)
    before = {
        url: Listing(**json.loads(body))
        for url, body in conn.execute('SELECT url, body FROM listings WHERE search_id=? AND source=?', (sid, source))
    }
    conn.executemany(
        'INSERT INTO listings(search_id,source,url,body,first_seen,last_seen) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(search_id,url) DO UPDATE SET body=excluded.body, last_seen=excluded.last_seen',
        [(sid, source, item.url, json.dumps(asdict(item)), seen.get(item.url) or now, now) for item in listings],
    )
    moved = [item for item in listings if item.url in before and other_rent(before[item.url], item)]
    _mark_cheaper(conn, sid, [item.url for item in moved if cheaper(before[item.url], item)], now)
    if source in sources.DETAILS:
        # The detail page states the rent that counts; one read before the change no longer does. A page that
        # was gone but is listed again a day later may have been taken down only for a while.
        gone = {
            row[0]
            for row in conn.execute(
                'SELECT url FROM listing_details WHERE source=? AND state=? AND created_at<?',
                (source, GONE, now - GONE_RECHECK),
            )
        }
        read_again(
            conn, source, [item.url for item in moved] + [item.url for item in listings if item.url in gone], now
        )
    stock = {row[0] for row in conn.execute('SELECT url FROM start_offers WHERE search_id=?', (sid,))}
    conn.executemany(
        "INSERT OR IGNORE INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?)",
        [
            (item.url, source, now)
            for item in listings
            if item.url not in stock and source in sources.DETAILS and could_match(item, wanted, middle)
        ],
    )


def _mark_cheaper(conn, sid, urls, now):
    """Listings that now ask less rent: the starting stock no longer keeps them out of the mail. outbox.py
    mails them if the rent is below what was last mailed."""
    conn.executemany('UPDATE listings SET cheaper_at=? WHERE search_id=? AND url=?', [(now, sid, url) for url in urls])
    conn.executemany('DELETE FROM start_offers WHERE search_id=? AND url=?', [(sid, url) for url in urls])


def _queue_next(conn, sid, page, unseen, stock, reread=False):
    """Queue the page after PAGE, or end the reading. Only a page that was full (its listed offers before the
    search's own place filter, plus the unread ones) can have one behind it. While a search takes in its STOCK,
    reading goes on as long as pages bring flats not seen yet (a page of only other places' offers says nothing
    about the end), and to the last full page when it is REREAD; otherwise only after a page of nothing but new
    flats, which may have pushed more down."""
    source, listings = page.source, page.listings
    size = sources.PAGE_SIZES.get(source, FULL_PAGE)
    full = source in sources.PAGED and page.listed + (page.unread or 0) >= size
    if stock:
        more, last = full and (bool(unseen) or not listings or reread), STOCK_PAGES
    else:
        more, last = full and bool(listings) and unseen == {item.url for item in listings}, MAX_PAGE
    if more and page.number < last:
        _queue(conn, sid, source, page.number + 1, REREAD if reread else stock)
        return
    if more and stock:
        log.warning('Reading the stock of %s stopped after %s pages; narrow the search to see the rest', source, last)
    conn.execute('DELETE FROM more_pages WHERE search_id=? AND source=?', (sid, source))


async def more_step(now=None):
    """At most one further result page, of an active search whose site may be visited now. A paused search's
    jobs wait until it runs again. Returns the job or None."""
    now = time.time() if now is None else now
    with store.db() as conn:
        rows = conn.execute(
            'SELECT m.* FROM more_pages m JOIN searches s ON s.id=m.search_id WHERE s.active'
        ).fetchall()
    job = next((row for row in rows if cadence.ready_at(row['source'], now=now) <= now), None)
    if job is None:
        return None
    search = store.search(job['search_id'])
    if not search:
        return None
    try:
        found, counts = await asyncio.wait_for(sources.more(job['source'], search, job['page']), timeout=FETCH_TIMEOUT)
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
                conn.execute('DELETE FROM more_pages WHERE job=?', (job['job'],))
            else:
                conn.execute('UPDATE more_pages SET tries=tries+1 WHERE job=?', (job['job'],))
        log.warning('Page %s of %s failed (%s)', job['page'], job['source'], type(exc).__name__)
        return job
    cadence.outcome(job['source'], 'page')
    save_more(search, job, found, counts['listed'], time.time(), counts['unread'], counts.get('warning'))
    return job
