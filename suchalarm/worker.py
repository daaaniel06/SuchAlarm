"""The background loop: runs queued provider jobs, stores results, mails new offers, backs up.

Everything here runs on the app's event loop, like the API handlers. That is deliberate: a handler
never interleaves with a worker step except at an await, so e.g. editing a search cannot race a
check writing its offers (app.editable refuses while that search is being checked).
"""

import asyncio
import email
import email.policy
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass

from . import mailer, mailserver, scheduler, status, store
from .core import cadence, images
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
MAIL_RETRY_AFTER = 600  # after a definite SMTP failure, do not hammer the server
PACKAGE_WAIT = 7200  # "Sofort" with packages: never wait longer than this for a full package
GATHER_WAIT = 300  # while other searches are still being checked, let their offers share the mail
IMAGE_WAIT = 300  # "Sofort" waits this long at most for pictures still being cached
MAIL_LIMIT = 100  # offers per mail; the rest follows with the next one
RECIPIENT_RETRY, RECIPIENT_ATTEMPTS = 900, 4  # a recipient refused for now (4xx): again after 15, 30, 60 minutes
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


# Outbox rows of one search kind: the outbox is shared, each kind has its own rhythm.
OF_KIND = 'search_id IN (SELECT id FROM searches WHERE kind=?)'


def _forget_outbox(conn, kind):
    """Mails are off or unconfigured: pending hits count as known, so enabling later sends no flood."""
    conn.execute(
        'INSERT OR REPLACE INTO notifications(search_id,url,price,sent_at,reason) '
        f"SELECT search_id, url, price, created_at, 'known' FROM outbox WHERE batch IS NULL AND {OF_KIND}",
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
    # The pause after a failure is the kind's own; mails from before kinds were recorded (NULL) count for all.
    last = conn.execute(
        "SELECT status, created_at FROM batches WHERE status IN ('failed','sent') AND (kind=? OR kind IS NULL) "
        'ORDER BY created_at DESC LIMIT 1',
        (kind,),
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
        'INSERT INTO batches(id,recipients,subject,count,status,created_at,kind) VALUES (?,?,?,?,?,?,?)',
        (batch, ', '.join(mail['recipients']), message['Subject'], len(entries), 'sending', now, kind),
    )
    conn.executemany(
        'UPDATE outbox SET batch=? WHERE search_id=? AND url=?',
        [(batch, search['id'], offer['url']) for search, offer, _ in entries],
    )
    return batch, message, entries


def _record_recipients(conn, batch, message, outcome, refused):
    """What each recipient's server said. A temporary refusal (4xx) gets the same mail again later, to that
    address only; the mail is kept until then. Everyone else never gets it twice."""
    status = {'sent': 'accepted', 'unknown': 'uncertain'}[outcome]
    rows: list[tuple] = []
    for address in (a.strip() for a in message['To'].split(',')):
        code = refused.get(address)
        if code is None:
            rows.append((batch, address, status, None, 0))
        else:
            rows.append((batch, address, 'retry' if code < 500 else 'refused', code, time.time() + RECIPIENT_RETRY))
    conn.executemany('INSERT OR REPLACE INTO deliveries(batch,address,status,code,next_at) VALUES (?,?,?,?,?)', rows)
    if any(row[2] == 'retry' for row in rows):
        conn.execute('UPDATE batches SET message=? WHERE id=?', (message.as_bytes(), batch))


def _record_delivery(batch, entries, outcome, error, message=None, refused=None):
    with store.db() as conn:
        sent_at = time.time() if outcome == 'sent' else None
        conn.execute('UPDATE batches SET status=?, error=?, sent_at=? WHERE id=?', (outcome, error, sent_at, batch))
        if message is not None and outcome != 'failed':
            _record_recipients(conn, batch, message, outcome, refused or {})
        if outcome == 'failed':
            # The server definitely did not accept it: keep the offers for a later retry.
            conn.execute('UPDATE outbox SET batch=NULL WHERE batch=?', (batch,))
            store.put_setting(conn, 'mail_error', f'{error} Neuer Versuch in 10 Minuten.')
            return
        # Only the entries still belonging to this batch count; an edit or deletion may have removed them.
        # 'sent' only after the server accepted the mail; an uncertain one is known, so it never comes twice.
        now, reason = time.time(), 'sent' if outcome == 'sent' else 'uncertain'
        conn.executemany(
            'INSERT OR REPLACE INTO notifications(search_id,url,price,sent_at,reason) SELECT search_id,url,?,?,? '
            'FROM outbox WHERE search_id=? AND url=? AND batch=?',
            [(price, now, reason, search['id'], offer['url'], batch) for search, offer, price in entries],
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
    if not mailserver.configured():
        with store.db() as conn:
            for kind in KINDS:
                _forget_outbox(conn, kind)
        return
    for kind in KINDS:
        await _send_kind(kind, now)
    await _retry_recipients(now)


async def _retry_recipients(now):
    """The mail once more to an address its server refused for now (4xx), to that address alone."""
    with store.db() as conn:
        row = conn.execute(
            'SELECT d.batch, d.address, d.attempts, b.message FROM deliveries d JOIN batches b ON b.id=d.batch '
            "WHERE d.status='retry' AND d.next_at<=? AND b.message IS NOT NULL ORDER BY d.next_at LIMIT 1",
            (now,),
        ).fetchone()
    if row is None:
        return
    message = email.message_from_bytes(row['message'], policy=email.policy.default)
    del message['To']
    message['To'] = row['address']
    attempts = row['attempts'] + 1
    try:
        refused = await asyncio.to_thread(mailer.deliver, message)
        code = refused.get(row['address'])
        status = 'accepted' if code is None else ('refused' if code >= 500 else 'retry')
    except mailer.DeliveryError as exc:
        code, status = None, 'uncertain' if exc.maybe_sent else 'retry'
    if status == 'retry' and attempts >= RECIPIENT_ATTEMPTS:
        status = 'failed'
    with store.db() as conn:
        conn.execute(
            'UPDATE deliveries SET status=?, code=?, attempts=?, next_at=? WHERE batch=? AND address=?',
            (status, code, attempts, time.time() + RECIPIENT_RETRY * 2**attempts, row['batch'], row['address']),
        )
        if status == 'failed':
            store.put_setting(
                conn, 'mail_error', f'{row["address"]} hat die Mail auch nach {attempts} Versuchen nicht angenommen.'
            )
        if not conn.execute("SELECT 1 FROM deliveries WHERE batch=? AND status='retry'", (row['batch'],)).fetchone():
            conn.execute('UPDATE batches SET message=NULL WHERE id=?', (row['batch'],))


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
    refused = {}
    try:
        refused = await asyncio.to_thread(mailer.deliver, message)
        # The others got it: never sent again, the refused addresses are shown instead.
        outcome, error = 'sent', (mailer.refusal(refused) if refused else None)
    except mailer.DeliveryError as exc:
        outcome, error = ('unknown' if exc.maybe_sent else 'failed'), str(exc)
        log.warning('Mail %s', outcome)
    _record_delivery(batch, entries, outcome, error, message, refused)


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
        store.backup()
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
    failed |= not await _guarded('mail', send_digest)
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
