"""The background loop: runs queued provider jobs, stores results, mails new offers, backs up.

Everything here runs on the app's event loop, like the API handlers. That is deliberate: a handler
never interleaves with a worker step except at an await, so e.g. editing a search cannot race a
check writing its offers (app.editable refuses while that search is being checked).
"""

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass

from . import backups, digest, scheduler, status, store
from .core import cadence
from .kinds import KINDS

log = logging.getLogger('suchalarm')


@dataclass
class Progress:
    """What the worker is checking right now; shown by /api/state, guards edits (app.editable)."""

    search: int | None = None
    source: str | None = None


@dataclass
class BackupClock:
    due: float = 0.0  # the first backup runs right after the start


progress = Progress()
backup = BackupClock()

FETCH_TIMEOUT = 100  # one provider query, including a follow-up page
IDLE_MAX = 30  # the loop looks again at least this often
ERROR_PAUSE = 30  # after an unexpected error in the loop
BACKUP_EVERY, BACKUP_RETRY = 24 * 3600, 3600


# Checking a search


async def check(search, source_names=None):
    """Fetch the given sources (default: all of the search) and store what they return.

    Returns the sources whose job is finished: a result, a failure, a visible pause or a resting source.
    """
    kind = KINDS[search['kind']]
    progress.search, completed, saved = search['id'], set(), []
    try:
        await kind.prepare(search)
        for source in search['sources'] if source_names is None else source_names:
            progress.source, now = source, time.time()
            if note := kind.rests(search, source):
                status.rest(search, source, note, now)
                completed.add(source)
                continue
            try:
                items, extra = await asyncio.wait_for(kind.fetch(source, search), timeout=FETCH_TIMEOUT)
            except cadence.Deferred:
                if status.deferred(search, source, now):
                    completed.add(source)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                completed.add(source)
                status.failed(search, source, now, exc)
                continue
            completed.add(source)
            if not items and kind.empty_is_suspicious(extra) and status.empty_after_stock(search, source):
                status.failed(
                    search, source, now, RuntimeError('Anbieter liefert plötzlich keine Angebote (Layout geändert?).')
                )
                continue
            saved.append(kind.save(search, source, items, extra, cadence.observation(source, search, now)))
        kind.finish(search, saved)
    finally:
        progress.search = progress.source = None
    return completed


# The loop


async def _serve_followers(job, completed):
    """Other searches sharing the job's query get the same result: from the fresh cache, or the same error."""
    first, source = job['searches'][0], job['source']
    for follower in job['searches'][1:]:
        search = store.search(follower['id'])  # it may have changed while the first one was checked
        if not search or source not in search['sources'] or cadence.query_key(source, search) != job['key']:
            continue
        if not search['active'] and search['id'] not in scheduler.queued_ids():
            continue
        state = cadence.query_state(source, search)
        if status.share_error(first['id'], search, source):
            done = completed
        elif state and state['state'] == 'ok' and state['next_at'] > time.time():
            done = await check(search, [source])
        else:
            done = completed
        if source in done:
            scheduler.cancel(search['id'], source)


async def _run_job(job):
    first, source = job['searches'][0], job['source']
    try:
        with cadence.manual_check(job.get('manual_at')):
            completed = await check(first, [source])
        await _serve_followers(job, completed)
        if source in completed:
            scheduler.cancel(first['id'], source)
    finally:
        # Also after an unexpected error: the job moves to the back instead of being picked again at once.
        scheduler.finish(job)


async def daily_backup():
    """Once a day; after a failure the next attempt waits an hour instead of repeating every pass."""
    if time.time() < backup.due:
        return
    try:
        for kind in KINDS.values():
            kind.cleanup()
        # On the event loop, not in a thread: no API write can land between the copies of two files.
        backups.make()
        backup.due = time.time() + BACKUP_EVERY
    except Exception:
        log.exception('Backup failed')
        backup.due = time.time() + BACKUP_RETRY


async def _guarded(name, step):
    """Run one part of a pass; an unexpected error is logged and must not skip the parts after it (a broken
    provider job never holds back the mail). Returns whether it went through."""
    try:
        await step()
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception('Worker step %s failed', name)
        return False


async def run_once():
    """One step: a due job, or else the kinds' background work; then mail and backup. Returns the idle delay."""
    paused = store.paused()
    job, next_at = scheduler.plan(store.searches(), paused)
    failed = False
    if job:
        failed = not await _guarded('job', lambda: _run_job(job))
    elif not paused:
        for name, kind in KINDS.items():
            # detail pages and shipping lookups, in free slots of the budget
            failed |= not await _guarded(f'{name} idle', kind.idle)

    async def queue_mails():
        for kind in KINDS.values():
            kind.queue_mails()

    failed |= not await _guarded('queue mails', queue_mails)
    failed |= not await _guarded('mail', digest.send_digest)
    await daily_backup()  # has its own retry clock
    if failed:
        return ERROR_PAUSE  # a repeating error must not spin the loop
    return 0 if job else max(0.1, min(IDLE_MAX, next_at - time.time()))


async def run():
    while True:
        try:
            scheduler.wake.clear()
            delay = await run_once()
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(scheduler.wake.wait(), timeout=delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Worker failed; retrying')
            await asyncio.sleep(ERROR_PAUSE)
