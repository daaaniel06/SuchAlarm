"""One durable, fair queue per provider and normalized query. No per-search timers."""

import asyncio
import time
from dataclasses import dataclass

from . import store
from .core import cadence

MANUAL_RUN_TTL = 600  # a click older than this no longer counts as running
MANUAL_RUN_HISTORY = 86400
wake = asyncio.Event()  # set by the API when something should happen now; the worker loop waits on it


def request(search, only=None):
    """One manual run per actual query; clicks never extend or append a run. ONLY limits it to these sources."""
    now, started, pending, retry = time.time(), 0, False, 0
    chosen = [source for source in search['sources'] if only is None or source in only]
    marks = ','.join('?' * len(chosen))
    with cadence.db() as conn:
        active = conn.execute(
            'SELECT 1 FROM manual m JOIN manual_runs r ON r.key=m.key WHERE m.search_id=? AND r.finished_at=0 '
            f'AND r.requested_at>? AND m.source IN ({marks}) LIMIT 1',
            (search['id'], now - MANUAL_RUN_TTL, *chosen),
        ).fetchone()
        if active:
            return dict(queued=True, started=False, already_running=True, retry_after=0)
        conn.execute('DELETE FROM manual_runs WHERE finished_at>0 AND finished_at<?', (now - MANUAL_RUN_HISTORY,))
        for source in chosen:
            key = cadence.query_key(source, search)
            run = conn.execute('SELECT * FROM manual_runs WHERE key=?', (key,)).fetchone()
            if run and not run['finished_at'] and now - run['requested_at'] < MANUAL_RUN_TTL:
                pending = True
            elif run and run['finished_at'] + cadence.MANUAL_COOLDOWN > now:
                retry = max(retry, run['finished_at'] + cadence.MANUAL_COOLDOWN - now)
                continue
            else:
                conn.execute('INSERT OR REPLACE INTO manual_runs VALUES (?,?,0)', (key, now))
                started += 1
            conn.execute('INSERT OR IGNORE INTO manual VALUES (?,?,?)', (search['id'], source, key))
    return dict(
        queued=bool(started or pending), started=bool(started), already_running=pending, retry_after=int(retry + 0.999)
    )


def _finish_orphaned_runs(conn, now):
    """A manual run is over once no search waits for its query any more."""
    conn.execute(
        'UPDATE manual_runs SET finished_at=? WHERE finished_at=0 AND NOT EXISTS (SELECT 1 FROM manual WHERE manual.key=manual_runs.key)',
        (now,),
    )


def cancel(search_id, source=None):
    with cadence.db() as conn:
        conn.execute(
            'DELETE FROM manual WHERE search_id=?' + (' AND source=?' if source else ''),
            (search_id, source) if source else (search_id,),
        )
        _finish_orphaned_runs(conn, time.time())


def queued_sources():
    with cadence.db() as conn:
        return [dict(r) for r in conn.execute('SELECT search_id,source FROM manual ORDER BY search_id,source')]


def queued_ids():
    with cadence.db() as conn:
        return [r[0] for r in conn.execute('SELECT DISTINCT search_id FROM manual ORDER BY search_id')]


# Order among jobs that may run now; lower first, then by queue position (fair rotation).
PRIORITY_MANUAL, PRIORITY_FROM_CACHE, PRIORITY_CONTINUING, PRIORITY_DUE = -2, -1, 0, 1
IDLE_WAKE = 30


@dataclass(frozen=True)
class QueueView:
    """What one planning pass knows about the queue."""

    manual: dict  # (search id, source) -> query key of a pending click
    manual_runs: dict  # query key -> time of the click
    statuses: dict  # (search id, source) -> stored source status
    now: float


@dataclass(frozen=True)
class Candidate:
    ready: float  # earliest start
    priority: int
    position: int
    job: dict


def _sync_manual(conn, by_id, now):
    """Drop manual requests whose search or query changed; returns the remaining requests and open runs."""
    manual = {(r['search_id'], r['source']): r['key'] for r in conn.execute('SELECT * FROM manual')}
    for (sid, source), key in list(manual.items()):
        search = by_id.get(sid)
        if not search or source not in search['sources'] or cadence.query_key(source, search) != key:
            conn.execute('DELETE FROM manual WHERE search_id=? AND source=?', (sid, source))
            del manual[(sid, source)]
    _finish_orphaned_runs(conn, now)
    # Existing pending requests from an older version become one fresh manual run.
    conn.executemany('INSERT OR IGNORE INTO manual_runs VALUES (?,?,0)', [(key, now) for key in set(manual.values())])
    runs = {r['key']: r['requested_at'] for r in conn.execute('SELECT * FROM manual_runs WHERE finished_at=0')}
    return manual, runs


def _consumers(searches, paused, manual):
    """query key -> (source, searches using it). Paused searches only take part with a manual request."""
    consumers: dict[str, tuple[str, list[dict]]] = {}
    for search in searches:
        for source in search['sources']:
            if (search['active'] and not paused) or (search['id'], source) in manual:
                consumers.setdefault(cadence.query_key(source, search), (source, []))[1].append(search)
    return consumers


def _sync_jobs(conn, consumers):
    """One persistent queue entry per needed query; new ones go to the back, unused ones are dropped."""
    jobs = {r['key']: dict(r) for r in conn.execute('SELECT * FROM jobs')}
    position = max((j['position'] for j in jobs.values()), default=0)
    for key, (source, _) in consumers.items():
        if key not in jobs:
            position += 1
            conn.execute('INSERT INTO jobs VALUES (?,?,?,0)', (key, source, position))
            jobs[key] = dict(key=key, source=source, position=position, not_before=0)
    conn.executemany('DELETE FROM jobs WHERE key=?', [(key,) for key in jobs.keys() - consumers.keys()])
    return jobs


def _candidate(key, source, consumers, job, view):
    """When and how urgently this query's job could run, and for which searches."""
    manual, statuses, now = view.manual, view.statuses, view.now
    row = cadence.query_state(source, consumers[0])
    clicked = [s for s in consumers if (s['id'], source) in manual]
    fresh_cache = bool(row and row['state'] == 'ok' and row['next_at'] > now)
    blocked = cadence.blocked_until(source)
    manual_at = view.manual_runs.get(key, 0) if clicked else 0

    def result(ready, priority, searches):
        return Candidate(
            ready, priority, job['position'], dict(key=key, source=source, searches=searches, manual_at=manual_at)
        )

    if manual_at:
        # A click runs with the short manual spacing. A refusal or an already fresh result
        # finishes it at once with a visible pause or the cached result; nothing waits in a queue.
        with cadence.manual_check(manual_at):
            allowed = cadence.ready_at(source, now=now)
        satisfied = fresh_cache and row['at'] >= manual_at
        return result(now if blocked or satisfied else allowed, PRIORITY_MANUAL, consumers)
    # Searches that have not seen the current cached result yet get it without a portal request.
    behind = fresh_cache and [
        s
        for s in consumers
        if (s['id'], source) in manual
        or (s['id'], source) not in statuses
        or statuses[(s['id'], source)]['checked_at'] != row['at']
    ]
    if behind:
        return result(max(now, job['not_before']), PRIORITY_FROM_CACHE, behind)
    if clicked and blocked and row and row['next_at'] > now:
        return result(max(now, job['not_before']), PRIORITY_FROM_CACHE, clicked)
    ready = max(cadence.query_due(source, row, now), cadence.ready_at(source, now=now), job['not_before'])
    priority = PRIORITY_CONTINUING if row and row['state'] == 'continuing' else PRIORITY_DUE
    return result(ready, priority, consumers)


def plan(searches, paused, now=None):
    """Return the first ready job and the next wake time. Waiting never launches a browser.

    Manual requests go first. Among jobs allowed now, a visit that already started finishes first,
    then the queue rotates fairly by position.
    """
    now = time.time() if now is None else now
    with store.db() as conn:
        statuses = {(r['search_id'], r['source']): dict(r) for r in conn.execute('SELECT * FROM source_status')}
    with cadence.db() as conn:
        manual, manual_runs = _sync_manual(conn, {s['id']: s for s in searches}, now)
        consumers = _consumers(searches, paused, manual)
        jobs = _sync_jobs(conn, consumers)
    view = QueueView(manual, manual_runs, statuses, now)
    candidates = [_candidate(key, source, users, jobs[key], view) for key, (source, users) in consumers.items()]
    candidates = [c for c in candidates if c.job['manual_at']] or candidates
    if not candidates:
        return None, now + IDLE_WAKE
    ready = [c for c in candidates if c.ready <= now]
    if ready:
        return min(ready, key=lambda c: (c.priority, c.position)).job, now
    return None, min(c.ready for c in candidates)


def finish(job, now=None):
    now = time.time() if now is None else now
    row = cadence.query_state(job['source'], job['searches'][0])
    # A timeout outside the fetch decorator must not cause an immediate repeated request.
    with store.db() as conn:
        status = conn.execute(
            'SELECT error FROM source_status WHERE search_id=? AND source=?', (job['searches'][0]['id'], job['source'])
        ).fetchone()
    fallback = now + 180 if not row or row['next_at'] <= now or (status and status['error']) else 0
    with cadence.db() as conn:
        position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM jobs').fetchone()[0]
        conn.execute('UPDATE jobs SET position=?,not_before=? WHERE key=?', (position, fallback, job['key']))
