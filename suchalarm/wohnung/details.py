"""Detail pages: one per free budget slot for new listings that can still fit (detail_step), the starting
stock slowly when nothing else waits (backfill_step), and any listing on request."""

import asyncio
import json
import logging
import time

from .. import store
from ..core import cadence
from . import sources
from .catalog import DETAIL_WINDOW, STALE
from .domain import Listing, could_match, profile

log = logging.getLogger('suchalarm')


FETCH_TIMEOUT = 100

DETAIL_ATTEMPTS = 3

DETAIL_RETRY = 600  # a failed detail page is tried again after 10, then 20 minutes

BACKFILL_GAP = 15 * 60  # at most one detail page of the starting stock per portal in this time


async def detail_step(now=None):
    """At most one detail page: the listing waiting longest whose site may be visited now. Returns its URL."""
    now = time.time() if now is None else now
    with store.db() as conn:
        rows = conn.execute(
            "SELECT * FROM listing_details WHERE state='pending' AND next_at<=? AND created_at>=? ORDER BY created_at",
            (now, now - DETAIL_WINDOW),
        ).fetchall()
    job = next((row for row in rows if cadence.ready_at(row['source'], now=now) <= now), None)
    if job is None:
        return None
    try:
        detail = await asyncio.wait_for(sources.detail(job['source'], job['url']), timeout=FETCH_TIMEOUT)
    except cadence.Deferred:
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        attempts, until = job['attempts'] + 1, time.time() + DETAIL_RETRY * 2 ** job['attempts']
        if isinstance(exc, cadence.Refused):
            # A refusal says nothing about the listing: pause the site, keep the attempts.
            attempts, until = job['attempts'], cadence.outcome(job['source'], 'page', True)
        with store.db() as conn:
            conn.execute(
                'UPDATE listing_details SET attempts=?, next_at=?, error=?, state=? WHERE url=?',
                (
                    attempts,
                    until,
                    cadence.error_text(exc)[:300],
                    'failed' if attempts >= DETAIL_ATTEMPTS else 'pending',
                    job['url'],
                ),
            )
        log.warning('Detail page of %s failed (%s)', job['source'], type(exc).__name__)
        return job['url']
    cadence.outcome(job['source'], 'page')
    with store.db() as conn:
        conn.execute(
            "UPDATE listing_details SET body=?, state='done', error=NULL WHERE url=?", (json.dumps(detail), job['url'])
        )
    return job['url']


_backfilled: dict[str, float] = {}  # portal -> when it last got a detail page of the starting stock


def backfill_step(now=None):
    """Queue one detail page of the starting stock when no other detail page waits: the listing of an
    active search that the portal still lists and that can still fit, newest first, at most one per
    portal every BACKFILL_GAP. It is read like any other (detail_step) and never mailed: the starting
    stock stays known. Returns the URL or None."""
    now = time.time() if now is None else now
    searches = {s['id']: s for s in store.searches('wohnung') if s['active']}
    with store.db() as conn:
        waiting = conn.execute(
            "SELECT 1 FROM listing_details WHERE state='pending' AND created_at>=? LIMIT 1", (now - DETAIL_WINDOW,)
        ).fetchone()
        if waiting or not searches:
            return None
        rows = conn.execute(
            'SELECT l.search_id, l.source, l.url, l.body FROM listings l '
            'JOIN start_offers s ON s.search_id=l.search_id AND s.url=l.url '
            'LEFT JOIN listing_details d ON d.url=l.url '
            'LEFT JOIN hidden h ON h.search_id=l.search_id AND h.url=l.url '
            'WHERE d.url IS NULL AND h.url IS NULL AND l.last_seen>=? ORDER BY l.first_seen DESC, l.rowid',
            (now - STALE,),
        ).fetchall()
        for row in rows:
            source, search = row['source'], searches.get(row['search_id'])
            if search is None or source not in sources.DETAILS or now - _backfilled.get(source, 0) < BACKFILL_GAP:
                continue
            if not could_match(Listing(**json.loads(row['body'])), profile(search), sources.centre(search)):
                continue
            if cadence.ready_at(source, now=now) > now:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?)",
                (row['url'], source, now),
            )
            _backfilled[source] = now
            return row['url']
    return None


def read_again(conn, source, urls, now):
    """Queue the detail pages of URLS (of SOURCE) like new ones, also those read before."""
    conn.executemany(
        "INSERT INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?) ON CONFLICT(url) DO "
        "UPDATE SET state='pending', attempts=0, next_at=0, error=NULL, created_at=excluded.created_at "
        "WHERE state!='pending'",
        [(url, source, now) for url in urls],
    )


def request_detail(conn, url, now=None):
    """The user asks for a listing's detail page (the starting stock has none): it is queued like a new one.
    False if no search lists the URL or its portal has no detail page."""
    now = time.time() if now is None else now
    row = conn.execute('SELECT source FROM listings WHERE url=? LIMIT 1', (url,)).fetchone()
    if row is None or row['source'] not in sources.DETAILS:
        return False
    conn.execute(
        "INSERT INTO listing_details(url,source,state,created_at) VALUES (?,?,'pending',?) ON CONFLICT(url) DO "
        "UPDATE SET state='pending', attempts=0, next_at=0, error=NULL, created_at=excluded.created_at "
        "WHERE state!='done'",
        (url, row['source'], now),
    )
    return True
