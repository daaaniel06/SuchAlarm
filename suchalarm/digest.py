"""The digest mails: per search kind one mail for what waits in the outbox, following the kind's rhythm, and a
second try for a recipient refused for now. A mail counts as sent only once the server took it; an uncertain one
is never sent again. The worker (worker.py) calls send_digest() once per pass."""

import asyncio
import email
import email.policy
import logging
import time
import uuid

from . import kinds, mailer, mailserver, scheduler, store
from .core import images
from .kinds import KINDS

log = logging.getLogger('suchalarm')

MAIL_RETRY_AFTER = 600  # after a definite SMTP failure, do not hammer the server
PACKAGE_WAIT = 7200  # "Sofort" with packages: never wait longer than this for a full package
GATHER_WAIT = 300  # while other searches are still being checked, let their offers share the mail
IMAGE_WAIT = 300  # "Sofort" waits this long at most for pictures still being cached
MAIL_LIMIT = 100  # offers per mail; the rest follows with the next one
RECIPIENT_RETRY, RECIPIENT_ATTEMPTS = 900, 4  # a recipient refused for now (4xx): again after 15, 30, 60 minutes
BATCH_HISTORY = 200  # mails kept in the history
INTERVALS = {'Alle 30 Minuten': 30 * 60, 'Täglich': 24 * 3600}


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
    if scheduler.queued_ids() and first > now - GATHER_WAIT:
        return False  # checks are still queued: let their offers share this mail
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
            'SELECT d.batch, d.address, d.attempts, b.message, b.kind FROM deliveries d JOIN batches b ON b.id=d.batch '
            "WHERE d.status='retry' AND d.next_at<=? AND b.message IS NOT NULL ORDER BY d.next_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return
        mail = store.mail_settings(kind=row['kind'] or kinds.DEFAULT)
        if not mail['enabled'] or row['address'] not in mail['recipients']:
            # Taken out of the recipients, or the kind's mails were switched off since: no more tries.
            conn.execute(
                "UPDATE deliveries SET status='cancelled' WHERE batch=? AND address=?", (row['batch'], row['address'])
            )
            _drop_kept_mail(conn, row['batch'])
            return
        # Marked before the handover: a restart in between counts as uncertain, never as a reason to send again.
        conn.execute(
            "UPDATE deliveries SET status='sending' WHERE batch=? AND address=?", (row['batch'], row['address'])
        )
    message = email.message_from_bytes(row['message'], policy=email.policy.default)
    del message['To']
    message['To'] = row['address']
    attempts = row['attempts'] + 1
    try:
        refused = await asyncio.to_thread(mailer.deliver, message)
        code = refused.get(row['address'])
        status = 'accepted' if code is None else ('refused' if code >= 500 else 'retry')
    except mailer.DeliveryError as exc:
        code = exc.refused.get(row['address'])
        if code is not None:  # the one recipient refused: its code decides, as above
            status = 'refused' if code >= 500 else 'retry'
        else:
            status = 'uncertain' if exc.maybe_sent else 'retry'
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
        _drop_kept_mail(conn, row['batch'])


def _drop_kept_mail(conn, batch):
    """The kept mail goes once no recipient waits for another try."""
    if not conn.execute("SELECT 1 FROM deliveries WHERE batch=? AND status='retry'", (batch,)).fetchone():
        conn.execute('UPDATE batches SET message=NULL WHERE id=?', (batch,))


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
