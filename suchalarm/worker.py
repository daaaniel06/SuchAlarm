"""The background loop: runs queued provider jobs, stores results, mails new offers, backs up.

Everything here runs on the app's event loop, like the API handlers. That is deliberate: a handler
never interleaves with a worker step except at an await, so e.g. editing a search cannot race a
check writing its offers (app.editable refuses while that search is being checked).
"""

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass

from . import mailer, scheduler, store
from .core import cadence, images
from .kinds import KINDS
from .status import deferred, empty_after_stock, failed, rest, source_error

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
MAIL_RETRY_AFTER = 600  # after a definite SMTP failure, do not hammer the server
PACKAGE_WAIT = 7200  # "Sofort" with packages: never wait longer than this for a full package
GATHER_WAIT = 300  # while other searches are still being checked, let their offers share the mail
IMAGE_WAIT = 300  # "Sofort" waits this long at most for pictures still being cached
MAIL_LIMIT = 100  # offers per mail; the rest follows with the next one
BATCH_HISTORY = 200  # mails kept in the history
INTERVALS = {'Alle 30 Minuten': 30 * 60, 'Täglich': 24 * 3600}
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
                rest(search, source, note, now)
                completed.add(source)
                continue
            try:
                items, extra = await asyncio.wait_for(kind.fetch(source, search), timeout=FETCH_TIMEOUT)
            except cadence.Deferred:
                if deferred(search, source, now):
                    completed.add(source)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                completed.add(source)
                failed(search, source, now, exc)
                continue
            completed.add(source)
            if not items and kind.EMPTY_IS_SUSPICIOUS and empty_after_stock(search, source):
                failed(
                    search, source, now, RuntimeError('Anbieter liefert plötzlich keine Angebote (Layout geändert?).')
                )
                continue
            saved.append(kind.save(search, source, items, extra, cadence.observation(source, search, now)))
        kind.finish(search, saved)
    finally:
        progress.search = progress.source = None
    return completed


# Outbox rows of one search kind: the outbox is shared, each kind has its own rhythm.
OF_KIND = 'search_id IN (SELECT id FROM searches WHERE kind=?)'


def _forget_outbox(conn, kind):
    """Mails are off: what waits only counts as known, so switching them on later sends no flood."""
    conn.execute(
        'INSERT OR REPLACE INTO notifications SELECT search_id, url, price, created_at FROM outbox '
        f'WHERE batch IS NULL AND {OF_KIND}',
        (kind,),
    )
    conn.execute(f'DELETE FROM outbox WHERE batch IS NULL AND {OF_KIND}', (kind,))


def _rhythm_allows(conn, mail, now, kind):
    """Whether the chosen rhythm lets a mail go now; pictures are looked at later."""
    first, waiting = conn.execute(
        f'SELECT MIN(created_at), COUNT(*) FROM outbox WHERE batch IS NULL AND {OF_KIND}', (kind,)
    ).fetchone()
    if not first:
        return False
    last = conn.execute(
        "SELECT status, created_at FROM batches WHERE status IN ('failed','sent') ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if last and last['status'] == 'failed' and last['created_at'] > now - MAIL_RETRY_AFTER:
        return False
    if mail['frequency'] != 'Sofort':
        return first <= now - INTERVALS.get(mail['frequency'], 0)
    if (progress.search or scheduler.queued_ids()) and first > now - GATHER_WAIT:
        return False  # other checks are running: let their offers share this mail
    return waiting >= mail['batch_size'] or first <= now - PACKAGE_WAIT


def _claim_batch(conn, mail, now, kind):
    """Compose the next mail of a search kind and mark its offers as being sent. None if nothing is due yet."""
    if not _rhythm_allows(conn, mail, now, kind):
        return None
    first = conn.execute(f'SELECT MIN(created_at) FROM outbox WHERE batch IS NULL AND {OF_KIND}', (kind,)).fetchone()[0]
    entries = sorted(KINDS[kind].take_pending(conn), key=lambda entry: entry[2])[:MAIL_LIMIT]
    if not entries:
        return None
    ready, coming = images.photos([offer['url'] for _, offer, _ in entries])
    if mail['frequency'] == 'Sofort' and coming and first > now - IMAGE_WAIT:
        return None
    for _, offer, _ in entries:
        offer['photo'] = ready.get(offer['url'])
    KINDS[kind].explain(conn, entries, now)
    batch = uuid.uuid4().hex
    message = mailer.compose_digest([(search, offer) for search, offer, _ in entries], mail['recipients'], batch)
    conn.execute(
        "INSERT INTO batches VALUES (?,?,?,?,'sending',NULL,?,NULL)",
        (batch, ', '.join(mail['recipients']), message['Subject'], len(entries), now),
    )
    conn.executemany(
        'UPDATE outbox SET batch=? WHERE search_id=? AND url=?',
        [(batch, search['id'], offer['url']) for search, offer, _ in entries],
    )
    return batch, message, entries


def _record_delivery(batch, entries, outcome, error):
    with store.db() as conn:
        sent_at = time.time() if outcome == 'sent' else None
        conn.execute('UPDATE batches SET status=?, error=?, sent_at=? WHERE id=?', (outcome, error, sent_at, batch))
        if outcome == 'failed':
            # The server definitely did not accept it: keep the offers for a later retry.
            conn.execute('UPDATE outbox SET batch=NULL WHERE batch=?', (batch,))
            store.put_setting(conn, 'mail_error', f'{error} Neuer Versuch in 10 Minuten.')
            return
        # Sent, or perhaps sent: record it so the same offer never comes twice.
        now = time.time()
        conn.executemany(
            'INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)',
            [(search['id'], offer['url'], price, now) for search, offer, price in entries],
        )
        conn.execute('DELETE FROM outbox WHERE batch=?', (batch,))
        if outcome == 'sent' and error is None:
            store.delete_setting(conn, 'mail_error')
        elif outcome == 'sent':
            store.put_setting(conn, 'mail_error', error)
        else:
            store.put_setting(
                conn, 'mail_error', f'{error} Die Mail kam vielleicht trotzdem an und wird nicht erneut gesendet.'
            )
        conn.execute(
            'DELETE FROM batches WHERE id NOT IN (SELECT id FROM batches ORDER BY created_at DESC LIMIT ?)',
            (BATCH_HISTORY,),
        )


async def send_digest(now=None):
    """Per search kind one mail for everything pending, following its rhythm; never resent after an
    uncertain outcome."""
    now = time.time() if now is None else now
    if not mailer.configured():
        return
    for kind in KINDS:
        await _send_kind(kind, now)


async def _send_kind(kind, now):
    mail = store.mail_settings(kind=kind)
    with store.db() as conn:
        if not mail['enabled'] or not mail['recipients']:
            _forget_outbox(conn, kind)
            return
        claimed = _claim_batch(conn, mail, now, kind)
    if claimed is None:
        return
    batch, message, entries = claimed
    try:
        refused = await asyncio.to_thread(mailer.deliver, message)
        # The others got it: never sent again, the refused addresses are shown instead.
        outcome, error = 'sent', (mailer.refusal(refused) if refused else None)
    except mailer.DeliveryError as exc:
        outcome, error = ('unknown' if exc.maybe_sent else 'failed'), str(exc)
        log.warning('Mail %s', outcome)
    _record_delivery(batch, entries, outcome, error)


# The loop


async def _serve_followers(job, completed):
    """Other searches sharing the job's query get the same result: from the fresh cache, or the same error."""
    first, source = job['searches'][0], job['source']
    with store.db() as conn:
        first_status = conn.execute(
            'SELECT * FROM source_status WHERE search_id=? AND source=?', (first['id'], source)
        ).fetchone()
    for follower in job['searches'][1:]:
        search = store.search(follower['id'])  # it may have changed while the first one was checked
        if not search or source not in search['sources'] or cadence.query_key(source, search) != job['key']:
            continue
        if not search['active'] and search['id'] not in scheduler.queued_ids():
            continue
        state = cadence.query_state(source, search)
        if first_status and first_status['error']:
            source_error(search, source, first_status['checked_at'], first_status['error'], first_status['failures'])
            done = completed
        elif state and state['state'] == 'ok' and state['next_at'] > time.time():
            done = await check(search, [source])
        else:
            done = completed
        if source in done:
            scheduler.cancel(search['id'], source)


async def _run_job(job):
    first, source = job['searches'][0], job['source']
    with cadence.manual_check(job.get('manual_at')):
        completed = await check(first, [source])
    await _serve_followers(job, completed)
    if source in completed:
        scheduler.cancel(first['id'], source)
    scheduler.finish(job)


async def daily_backup():
    """Once a day; after a failure the next attempt waits an hour instead of repeating every pass."""
    if time.time() < backup.due:
        return
    try:
        for kind in KINDS.values():
            kind.cleanup()
        await asyncio.to_thread(store.backup)
        backup.due = time.time() + BACKUP_EVERY
    except Exception:
        log.exception('Backup failed')
        backup.due = time.time() + BACKUP_RETRY


async def run_once():
    """One step: a due job, or else the kinds' background work; then mail and backup. Returns the idle delay."""
    paused = store.paused()
    job, next_at = scheduler.plan(store.searches(), paused)
    if job:
        await _run_job(job)
    elif not paused:
        for kind in KINDS.values():
            await kind.idle()  # detail pages and shipping lookups, in free slots of the budget
    for kind in KINDS.values():
        kind.queue_mails()
    await send_digest()
    await daily_backup()
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
