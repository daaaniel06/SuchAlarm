"""Persisted, shared request budgets."""

import asyncio
import functools
import hashlib
import json
import os
import random
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Rhythm:
    """How often one query of a source is visited."""

    day: int  # minutes between two visits
    night: int  # at least this many minutes at night (NIGHT_HOURS)
    jitter: int  # up to this many seconds are added at random


# Every source belongs to one of these; only the page budget below limits a whole site further.
PORTAL = Rhythm(day=15, night=60, jitter=240)  # marketplaces and big portals: new offers all day
LANDLORD = Rhythm(day=60, night=120, jitter=240)  # one landlord's whole stock on one page; changes rarely
REFERENCE = Rhythm(day=720, night=720, jitter=600)  # shop new prices, a comparison only


@dataclass(frozen=True)
class Provider:
    """What the shared budget needs to know about one source; each search kind registers its own."""

    rhythm: Rhythm
    key: Callable[[str, dict], str]  # (source, search) -> the query text; equal text means one shared request
    item: type  # result dataclass, stored with asdict and rebuilt with item(**values)
    select: Callable[[list, dict], list]  # local filters of one search on the shared result
    site: str = ''  # the page budget is kept per website; sources of one site name it (default: the source)


PROVIDERS: dict[str, Provider] = {}


def register(source, provider):
    PROVIDERS[source] = provider


def site(source):
    """The website whose page and image budget a source uses: shared by every source on that site."""
    provider = PROVIDERS.get(source)
    return provider.site if provider and provider.site else source


# Budget kinds: portal pages, preview pictures, OpenStreetMap (place lookups and map tiles, whose
# policies allow about one request a second) and place suggestions while typing (a few, never a burst).
SPACING = {'page': (40, 70), 'image': (8, 15), 'osm': (1.1, 2), 'suggest': (0.8, 1.2)}
GLOBAL_SPACING = {'page': (5, 10), 'image': (1, 3), 'osm': (0, 0), 'suggest': (0, 0)}
HOURLY = {'page': 60, 'image': 200, 'osm': 300, 'suggest': 40}
BACKOFF = (1800, 3600, 7200, 21600)
RETRIES = (180, 480)
BERLIN = ZoneInfo('Europe/Berlin')
MANUAL = ContextVar('manual_check', default=0)
MANUAL_COOLDOWN = 60
MANUAL_SPACING = (8, 12)
MANUAL_GLOBAL_SPACING = (2, 4)
NIGHT_HOURS = range(1, 6)  # Berlin time: few new offers appear, Rhythm.night applies
REPARSE_TOLERANCE = 120  # a saved page belongs to an observation taken within this many seconds
# What the current fetch saw last (step, address, status, title, text): stored with its failure, so the
# app can show why a source failed. The fetch's own code adds to it with note().
LAST_PAGE: ContextVar[dict | None] = ContextVar('last_page', default=None)


def note(**values):
    seen = LAST_PAGE.get()
    if seen is not None:
        seen.update(values)


@contextmanager
def manual_check(requested_at):
    token = MANUAL.set(requested_at or 0)
    try:
        yield
    finally:
        MANUAL.reset(token)


class Refused(RuntimeError):
    """The provider refuses or hides its page (HTTP 401/403/429, a security check, an unreadable page).
    Pauses the whole provider with growing backoff; any other error only retries this one query."""


GENERIC_ERROR = 'Browser-Abfrage fehlgeschlagen oder Zeitlimit erreicht.'


def error_text(exc):
    """What the app shows for a failed fetch. Only our own RuntimeErrors carry a message meant for people;
    anything else (Playwright, timeouts) may contain URLs with browser access tokens."""
    return str(exc) if isinstance(exc, RuntimeError) else GENERIC_ERROR


class Deferred(RuntimeError):
    """Not allowed yet. CONTINUATION marks a follow-up page of a visit that already started."""

    def __init__(self, until, continuation=False):
        self.until, self.continuation = until, continuation
        super().__init__('Abruf wartet auf den nächsten erlaubten Takt.')


SCHEMA = """
      CREATE TABLE IF NOT EXISTS budget (key TEXT PRIMARY KEY, next_at REAL DEFAULT 0, blocked_until REAL DEFAULT 0, strikes INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS requests (key TEXT, at REAL);
      CREATE INDEX IF NOT EXISTS request_times ON requests(key,at);
      CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, at REAL, expires REAL);
      CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, source TEXT, position INTEGER, not_before REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS manual (search_id INTEGER, source TEXT, key TEXT, PRIMARY KEY(search_id,source));
      CREATE TABLE IF NOT EXISTS manual_runs (key TEXT PRIMARY KEY, requested_at REAL, finished_at REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS failures (key TEXT PRIMARY KEY, error TEXT, at REAL, detail TEXT);
      CREATE TABLE IF NOT EXISTS queries (key TEXT PRIMARY KEY, source TEXT, at REAL DEFAULT 0, next_at REAL DEFAULT 0, failures INTEGER DEFAULT 0, state TEXT, value TEXT);
      CREATE TABLE IF NOT EXISTS places (source TEXT, query TEXT, value TEXT NOT NULL, PRIMARY KEY(source, query));
"""
_ready_paths: set[Path] = set()


def connect(path, setup):
    """Open a database file; SETUP (schema and migrations) runs once per process and file, not on every call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = path not in _ready_paths or not path.exists()
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    if fresh:
        try:
            setup(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        _ready_paths.add(path)
    return conn


def _setup(conn):
    conn.executescript(SCHEMA)
    if 'detail' not in {r[1] for r in conn.execute('PRAGMA table_info(failures)')}:
        conn.execute('ALTER TABLE failures ADD COLUMN detail TEXT')
    # Older versions kept Kleinanzeigen location IDs by postcode only; they become places of that source.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='locations'").fetchone():
        conn.execute("INSERT OR IGNORE INTO places SELECT 'kleinanzeigen', postcode, id FROM locations")
        conn.execute('DROP TABLE locations')
    # Older versions' Vinted shipping cache moved to the product module's delivery.db; it refills within hours.
    conn.execute('DROP TABLE IF EXISTS deliveries')


def data_dir():
    return Path(os.environ.get('DATA_DIR', '/data'))


@contextmanager
def transaction(path, setup):
    """One immediate transaction on a database file of DATA_DIR; SETUP creates its tables."""
    conn = connect(data_dir() / path, setup)
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db():
    return transaction('cadence.db', _setup)


def query_key(source, search):
    query = PROVIDERS[source].key(source, search)
    return source + ':' + hashlib.sha256(query.encode()).hexdigest()


def interval(source, stamp):
    rhythm = PROVIDERS[source].rhythm
    minutes = rhythm.night if datetime.fromtimestamp(stamp, BERLIN).hour in NIGHT_HOURS else rhythm.day
    return minutes * 60 + random.uniform(0, rhythm.jitter)


def _pause_key(source, kind):
    # Spacing and hourly budget are kept per website; a refusal pauses only the source that met it.
    return f'pause:{source}:{kind}'


def _ready(conn, source, kind, now):
    key = f'{site(source)}:{kind}'
    paused = _blocked_until(conn, source, kind)
    manual = kind == 'page' and MANUAL.get()
    keys = (f'manual:{key}', 'manual:all:page') if manual else (key, f'all:{kind}')
    rows = conn.execute('SELECT next_at FROM budget WHERE key IN (?,?)', keys).fetchall()
    until = max([now, paused] + [r['next_at'] for r in rows])
    used = conn.execute('SELECT at FROM requests WHERE key=? AND at>? ORDER BY at', (key, now - 3600)).fetchall()
    if len(used) >= HOURLY[kind]:
        until = max(until, used[-HOURLY[kind]]['at'] + 3600)
    return until


def ready_at(source, kind='page', now=None):
    with db() as conn:
        return _ready(conn, source, kind, time.time() if now is None else now)


def book(source, kind='page'):
    now = time.time()
    key, global_key = f'{site(source)}:{kind}', f'all:{kind}'
    with db() as conn:
        until = _ready(conn, source, kind, now)
        if until > now:
            raise Deferred(until)
        for name, gap in ((key, SPACING[kind]), (global_key, GLOBAL_SPACING[kind])):
            conn.execute(
                'INSERT INTO budget(key,next_at) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at',
                (name, now + random.uniform(*gap)),
            )
        # Every real visit also sets the short manual gap. A manual visit sets the
        # normal background gap too, so automation cannot follow it immediately.
        if kind == 'page':
            for name, gap in ((f'manual:{key}', MANUAL_SPACING), ('manual:all:page', MANUAL_GLOBAL_SPACING)):
                conn.execute(
                    'INSERT INTO budget(key,next_at) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at',
                    (name, now + random.uniform(*gap)),
                )
        conn.execute('INSERT INTO requests VALUES (?,?)', (key, now))
        conn.execute('DELETE FROM requests WHERE at<?', (now - 86400,))


def outcome(source, kind, blocked=False, retry_after=0):
    key, now = _pause_key(source, kind), time.time()
    with db() as conn:
        row = conn.execute('SELECT strikes FROM budget WHERE key=?', (key,)).fetchone()
        strikes = (row['strikes'] if row else 0) + 1 if blocked else 0
        until = max(now + BACKOFF[min(strikes, 4) - 1], retry_after) if blocked else 0
        conn.execute(
            'INSERT INTO budget(key,strikes,blocked_until) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET strikes=excluded.strikes,blocked_until=excluded.blocked_until',
            (key, strikes, until),
        )
    return until


def release(source, kind='page'):
    """Lift a provider pause by hand. The strikes stay, so a further refusal pauses even longer."""
    with db() as conn:
        conn.execute('UPDATE budget SET blocked_until=0 WHERE key=?', (_pause_key(source, kind),))


def _blocked_until(conn, source, kind='page'):
    row = conn.execute('SELECT blocked_until FROM budget WHERE key=?', (_pause_key(source, kind),)).fetchone()
    return row['blocked_until'] if row and row['blocked_until'] > time.time() else 0


def blocked_until(source, kind='page'):
    """End of the current refusal pause of a provider, or 0."""
    with db() as conn:
        return _blocked_until(conn, source, kind)


def cached_page(source, url):
    key = 'page:' + source + ':' + url
    with db() as conn:
        row = conn.execute(
            'SELECT value FROM cache WHERE key=? AND expires>? AND at>=?', (key, time.time(), MANUAL.get())
        ).fetchone()
    return json.loads(row['value']) if row else None


def historical_page(source, url, observed_at):
    """Reparse an already observed page after a parser fix; never claim it is a fresh visit."""
    with db() as conn:
        row = conn.execute('SELECT value,at FROM cache WHERE key=?', ('page:' + source + ':' + url,)).fetchone()
    return json.loads(row['value']) if row and abs(row['at'] - observed_at) < REPARSE_TOLERANCE else None


def save_page(source, url, value):
    now = time.time()
    with db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',
            ('page:' + source + ':' + url, json.dumps(value), now, now + 900),
        )
        conn.execute('DELETE FROM cache WHERE expires<?', (now - 86400,))
        conn.execute('DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY at DESC LIMIT 200)')


def location(source, query):
    """A provider's stored place ID for a place query (postcode, town), or None. Kept for good.

    A plain read without a write lock: query keys may look places up while a caller holds db()."""
    conn = connect(data_dir() / 'cadence.db', _setup)
    try:
        row = conn.execute('SELECT value FROM places WHERE source=? AND query=?', (source, query)).fetchone()
    finally:
        conn.close()
    return row['value'] if row else None


def save_location(source, query, value):
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO places VALUES (?,?,?)', (source, query, value))


def edit_results(source, needle, edit):
    """Change stored results of SOURCE that contain NEEDLE in place, without touching their observation
    or cadence. EDIT(value) changes the stored dict ({'offers': [...], ...}) and returns whether it did."""
    with db() as conn:
        rows = conn.execute(
            'SELECT key,value FROM queries WHERE source=? AND value IS NOT NULL AND instr(value,?)', (source, needle)
        ).fetchall()
        for row in rows:
            value = json.loads(row['value'])
            if edit(value):
                conn.execute('UPDATE queries SET value=? WHERE key=?', (json.dumps(value), row['key']))


def observation(source, search, fallback):
    with db() as conn:
        row = conn.execute("SELECT at FROM queries WHERE key=? AND state='ok'", (query_key(source, search),)).fetchone()
    return row['at'] if row else fallback


def _query_state(conn, source, search):
    row = conn.execute('SELECT * FROM queries WHERE key=?', (query_key(source, search),)).fetchone()
    return dict(row) if row else None


def query_state(source, search):
    with db() as conn:
        return _query_state(conn, source, search)


def query_due(source, row, now):
    if not row:
        return now
    due = row['next_at']
    if row['state'] == 'ok' and datetime.fromtimestamp(now, BERLIN).hour in NIGHT_HOURS:
        due = max(due, row['at'] + PROVIDERS[source].rhythm.night * 60)
    return due


def describe(searches):
    now, result = time.time(), []
    with db() as conn:  # polled every few seconds by the app: one connection for all rows
        for search in searches:
            for source in search['sources']:
                row = _query_state(conn, source, search)
                result.append(
                    dict(
                        search_id=search['id'],
                        source=source,
                        at=row['at'] if row else 0,
                        next_at=max(query_due(source, row, now), _ready(conn, source, 'page', now)),
                        state='blocked' if _blocked_until(conn, source) else row['state'] if row else 'waiting',
                    )
                )
    return result


QUERY_CACHE_SIZE = 500
RETRY_SILENTLY_WITHIN = 2700  # a retried query with a result this recent keeps showing it without an error


def _cached_result(source, search, row, cache_version, cached_parser):
    """The stored result of a query, reparsed from the saved pages when the parser changed since."""
    value = json.loads(row['value'])
    if value.get('version', 1) != cache_version and cached_parser:
        repaired = cached_parser(source, dict(search, excluded=''), value, row['at'])
        if repaired is not None:
            value = dict(repaired, version=cache_version)
            with db() as conn:
                conn.execute('UPDATE queries SET value=? WHERE key=?', (json.dumps(value), row['key']))
    item: Any = PROVIDERS[source].item
    return [item(**o) for o in value['offers']], value['reference']


def _record_wait(key, source, exc):
    with db() as conn:
        conn.execute(
            'INSERT INTO queries(key,source,next_at,state) VALUES (?,?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at,state=excluded.state',
            (key, source, exc.until, 'continuing' if exc.continuation else 'waiting'),
        )


def _record_failure(key, source, row, exc, now, detail=None):
    """Remember a failed query and plan its retry. Returns (until, retry_silently).

    A refusal pauses the whole provider with growing backoff. Other errors retry this query after
    3 and 8 minutes, then at the normal interval; the first retries stay silent while the last
    result is recent.
    """
    failures = (row['failures'] if row else 0) + 1
    refused = isinstance(exc, Refused)
    if refused:
        until = outcome(source, 'page', True)
    else:
        until = now + (RETRIES[failures - 1] if failures <= len(RETRIES) else interval(source, now))
    with db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO failures(key,error,at,detail) VALUES (?,?,?,?)',
            (key, error_text(exc)[:500], now, json.dumps(detail) if detail else None),
        )
        conn.execute(
            'INSERT INTO queries(key,source,next_at,failures,state) VALUES (?,?,?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET next_at=excluded.next_at,failures=excluded.failures,state=excluded.state',
            (key, source, until, failures, 'blocked' if refused else 'retrying'),
        )
    recent = not row or not row['at'] or now - row['at'] < RETRY_SILENTLY_WITHIN
    return until, not refused and failures <= len(RETRIES) and recent


def _record_success(key, source, offers, reference, cache_version):
    now = time.time()
    value = json.dumps({'offers': [asdict(o) for o in offers], 'reference': reference, 'version': cache_version})
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO queries VALUES (?,?,?,?,0,'ok',?)",
            (key, source, now, now + interval(source, now), value),
        )
        conn.execute(
            'DELETE FROM queries WHERE key NOT IN (SELECT key FROM queries ORDER BY at DESC LIMIT ?)',
            (QUERY_CACHE_SIZE,),
        )
        conn.execute('DELETE FROM failures WHERE key=?', (key,))
    outcome(source, 'page', False)


def shared_search(call=None, *, cache_version=1, cached_parser=None):
    """Decorator for a provider fetch: one portal request per normalized query (Provider.key), shared by
    every search with the same query. Each search's own filters are applied locally (Provider.select).

    CALL(source, search) returns (items, reference): a list of Provider.item and one extra value per query
    that the core stores and hands back unchanged (product searches: the idealo new price; else None).

    Within the query's interval the stored result is served; "Jetzt prüfen" (MANUAL) forces a fresh
    request unless the provider is paused. Waits and failures are recorded for the scheduler.
    """
    if call is None:
        return lambda inner: shared_search(inner, cache_version=cache_version, cached_parser=cached_parser)

    @functools.wraps(call)
    async def wrapped(source, search):
        key, now = query_key(source, search), time.time()
        with db() as conn:
            row = conn.execute('SELECT * FROM queries WHERE key=?', (key,)).fetchone()
        requested = MANUAL.get()
        if requested and (paused := blocked_until(source)):
            raise Deferred(paused)
        force = requested and (not row or row['at'] < requested)
        if row and row['next_at'] > now and not force:
            if row['state'] != 'ok' or not row['value']:
                raise Deferred(row['next_at'])
            offers, reference = _cached_result(source, search, row, cache_version, cached_parser)
            return PROVIDERS[source].select(offers, search), reference
        seen = {'stage': 'Abruf vorbereiten'}
        token = LAST_PAGE.set(seen)
        try:
            # Waiting for the next allowed slot never starts a browser or opens a connection.
            if (ready := ready_at(source)) > time.time():
                raise Deferred(ready)
            offers, reference = await call(source, dict(search, excluded=''))
        except asyncio.CancelledError:
            raise
        except Deferred as exc:
            _record_wait(key, source, exc)
            raise
        except Exception as exc:
            until, silently = _record_failure(key, source, row, exc, now, dict(seen, error=type(exc).__name__))
            if silently:
                raise Deferred(until) from None
            raise
        finally:
            LAST_PAGE.reset(token)
        _record_success(key, source, offers, reference, cache_version)
        return PROVIDERS[source].select(offers, search), reference

    return wrapped


def diagnose(source, searches):
    """Everything known about a provider and the queries of SEARCHES, for the app's "Details"."""
    now, key = time.time(), f'{site(source)}:page'
    with db() as conn:
        row = conn.execute('SELECT strikes FROM budget WHERE key=?', (_pause_key(source, 'page'),)).fetchone()
        used = conn.execute('SELECT COUNT(*) FROM requests WHERE key=? AND at>?', (key, now - 3600)).fetchone()[0]
        provider = dict(
            blocked_until=_blocked_until(conn, source),
            strikes=row['strikes'] if row else 0,
            ready_at=_ready(conn, source, 'page', now),
            pages_last_hour=used,
            pages_per_hour=HOURLY['page'],
        )
        queries = {}
        for search in searches:
            query = query_key(source, search)
            state = conn.execute('SELECT at,next_at,failures,state FROM queries WHERE key=?', (query,)).fetchone()
            failure = conn.execute('SELECT error,at,detail FROM failures WHERE key=?', (query,)).fetchone()
            queries[search['id']] = dict(
                query=dict(state) if state else None,
                failure=dict(failure, detail=json.loads(failure['detail'] or 'null')) if failure else None,
            )
    return provider, queries


def last_error(source, search):
    with db() as conn:
        row = conn.execute('SELECT error FROM failures WHERE key=?', (query_key(source, search),)).fetchone()
    return row['error'] if row else ''
