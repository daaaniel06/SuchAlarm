"""How a source of a search is shown: its result, a failure or a pause. Shared by every search kind."""

import logging
import time

from . import store
from .core import cadence

log = logging.getLogger('suchalarm')


def clock(stamp):
    return time.strftime('%H:%M', time.localtime(stamp))


STATUS_COLUMNS = 'INSERT OR REPLACE INTO source_status(search_id,source,checked_at,error,count,failures) '


def source_error(search, source, now, reason, failures=None):
    """Show a source as disturbed. Its previous offers stay visible as stale; never a clean empty result.

    FAILURES: failed attempts in a row. None counts this attempt on top of the stored ones."""
    with store.db() as conn:
        if failures is None:
            failures = _failures(conn, search, source) + 1
        conn.execute(STATUS_COLUMNS + 'VALUES (?,?,?,?,0,?)', (search['id'], source, now, reason[:300], failures))


def _failures(conn, search, source):
    row = conn.execute(
        'SELECT failures FROM source_status WHERE search_id=? AND source=?', (search['id'], source)
    ).fetchone()
    return row['failures'] if row else 0


def _shown_failures(search, source):
    """A pause or a pending retry is no new attempt; it shows the error of at least one."""
    with store.db() as conn:
        return max(1, _failures(conn, search, source))


def deferred(search, source, now):
    """The query is not allowed yet. Returns True if that finishes the job for this source.

    A provider pause shows as an error. A plain wait keeps the last result silently, except that
    "Jetzt prüfen" reports a query that is already retrying after a failure.
    """
    until = cadence.blocked_until(source)
    if until:
        reason = cadence.last_error(source, search) or 'Anbieter hat die Abfrage gesperrt.'
        source_error(search, source, now, f'{reason} Pause bis {clock(until)}.', _shown_failures(search, source))
        return True
    state = cadence.query_state(source, search) if cadence.MANUAL.get() else None
    if state and state['state'] == 'retrying':
        reason = cadence.last_error(source, search) or 'Browser-Abfrage fehlgeschlagen. Später erneut versuchen.'
        source_error(search, source, now, reason, _shown_failures(search, source))
        return True
    return False


def failed(search, source, now, exc):
    error = cadence.error_text(exc)
    until = cadence.blocked_until(source)
    source_error(search, source, now, f'{error} Pause bis {clock(until)}.' if until else error)
    # Only the type: exception texts may contain URLs with browser access tokens.
    log.warning('Source %s failed for search %s (%s)', source, search['id'], type(exc).__name__)


SUSPICIOUS_STOCK = 5  # an empty page after at least this many results is treated as a possible layout change


def empty_after_stock(search, source):
    """A source that suddenly lists nothing after a healthy stock usually changed its layout."""
    with store.db() as conn:
        row = conn.execute(
            'SELECT count FROM source_status WHERE search_id=? AND source=? AND error IS NULL', (search['id'], source)
        ).fetchone()
    return bool(row) and row['count'] >= SUSPICIOUS_STOCK


def rest(search, source, note, now):
    """A source with nothing for this search (a regional portal outside its area): shown as resting with the
    reason, never as disturbed."""
    with store.db() as conn:
        conn.execute(STATUS_COLUMNS + 'VALUES (?,?,?,NULL,0,0)', (search['id'], source, now))
        conn.execute('UPDATE source_status SET note=? WHERE search_id=? AND source=?', (note, search['id'], source))
