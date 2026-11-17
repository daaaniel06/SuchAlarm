"""How a source of a search is shown: its result, a failure or a pause. Shared by every search kind.

This module is the only one that touches the source_status table (suchalarm.db): one row per search and
source, what the app shows. The state of the provider query behind it (cadence.db: when it ran, its cached
result, retries, pauses) is the core's and is shared by every search with the same query; the functions here
turn it into what each search shows. Only preis/worker.py reads the table besides, in two joins that find
recent successful results.
"""

import logging
import time

from . import store
from .core import cadence

log = logging.getLogger('suchalarm')


def clock(stamp):
    return time.strftime('%H:%M', time.localtime(stamp))


def _show(conn, sid, source, now, error=None, count=0, failures=0, note=None):
    """Write what the source shows now. Whether it ever delivered a result (started) stays as it is."""
    conn.execute(
        'INSERT INTO source_status(search_id,source,checked_at,error,count,failures,note) VALUES (?,?,?,?,?,?,?) '
        'ON CONFLICT(search_id,source) DO UPDATE SET checked_at=excluded.checked_at, error=excluded.error, '
        'count=excluded.count, failures=excluded.failures, note=excluded.note',
        (sid, source, now, error, count, failures, note),
    )


# A source that usually brings many offers and suddenly brings almost none has most likely changed its layout in a
# way no single page shows (every card read, but none passes). HISTORY_KEEP results per search and source are kept.
HISTORY_KEEP, HISTORY_NEEDED, USUAL_AT_LEAST, DROP_FACTOR = 20, 5, 10, 10
DROP_WARNING = 'Liefert nur {count} statt sonst etwa {usual} Angebote – Seitenaufbau geändert?'


def _drop_warning(conn, sid, source, count):
    """A warning when COUNT is far below what the source usually brought for this search, else None."""
    earlier = sorted(
        r[0]
        for r in conn.execute(
            'SELECT count FROM source_history WHERE search_id=? AND source=? ORDER BY checked_at DESC LIMIT ?',
            (sid, source, HISTORY_KEEP),
        )
    )
    if len(earlier) < HISTORY_NEEDED:
        return None
    usual = earlier[len(earlier) // 2]  # the median: one odd result does not move it
    if usual < USUAL_AT_LEAST or count * DROP_FACTOR > usual:
        return None
    return DROP_WARNING.format(count=count, usual=usual)


def forget_counts(conn, search_id):
    """An edited search finds something else: what its sources usually brought says nothing about it any more
    (a lower rent limit is no broken page)."""
    conn.execute('DELETE FROM source_history WHERE search_id=?', (search_id,))


def _remember_count(conn, sid, source, now, count):
    conn.execute(
        'INSERT INTO source_history(search_id,source,checked_at,count) VALUES (?,?,?,?)', (sid, source, now, count)
    )
    conn.execute(
        'DELETE FROM source_history WHERE search_id=?1 AND source=?2 AND rowid NOT IN (SELECT rowid FROM '
        'source_history WHERE search_id=?1 AND source=?2 ORDER BY checked_at DESC LIMIT ?3)',
        (sid, source, HISTORY_KEEP),
    )


def source_ok(conn, sid, source, now, count, unread=None, warning=None):
    """A readable result with COUNT offers. From now on the source has started for the search (started()).
    UNREAD: offers the parser missed. WARNING: a layout hint beside the successful result; a sudden drop against
    the source's usual count for this search is one too (_drop_warning)."""
    drop = _drop_warning(conn, sid, source, count)
    warning = warning or drop
    if drop is None:
        # A drop stays out of the usual count: a page that stays broken keeps its warning instead of becoming the
        # new normal. It ends when the counts come back or the search is edited.
        _remember_count(conn, sid, source, now, count)
    _show(conn, sid, source, now, count=count)
    conn.execute(
        'UPDATE source_status SET started=1, unread=?, warning=? WHERE search_id=? AND source=?',
        (unread, warning, sid, source),
    )


def more_unread(conn, sid, source, unread, warning=None):
    """Keep unread counts and the first layout hint of further pages until the next regular result."""
    if unread:
        conn.execute(
            'UPDATE source_status SET unread=COALESCE(unread, 0) + ? WHERE search_id=? AND source=?',
            (unread, sid, source),
        )
    if warning:
        conn.execute(
            'UPDATE source_status SET warning=COALESCE(warning, ?) WHERE search_id=? AND source=?',
            (warning, sid, source),
        )


def started(conn, sid, source):
    """Whether the source ever delivered a result for the search. Its first one is the starting stock; a
    failure since does not change that, only an edit that drops the status (the kinds' reset())."""
    row = conn.execute('SELECT started FROM source_status WHERE search_id=? AND source=?', (sid, source)).fetchone()
    return bool(row and row['started'])


def source_error(search, source, now, reason, failures=None):
    """Show a source as disturbed. Its previous offers stay visible as stale; never a clean empty result.

    FAILURES: failed attempts in a row. None counts this attempt on top of the stored ones. The count of the
    last result stays, so an empty page after the failure still looks suspicious (empty_after_stock)."""
    with store.db() as conn:
        row = _row(conn, search, source)
        if failures is None:
            failures = (row['failures'] if row else 0) + 1
        count = row['count'] if row else 0
        _show(conn, search['id'], source, now, error=reason[:300], count=count, failures=failures)


def _row(conn, search, source):
    return of(conn, search['id'], source)


def of(conn, search_id, source):
    """The row a search shows for a source, or None before its first check (or after an edit dropped it)."""
    row = conn.execute('SELECT * FROM source_status WHERE search_id=? AND source=?', (search_id, source)).fetchone()
    return dict(row) if row else None


def table(conn, source=None):
    """Every search's row, of one SOURCE or all, as the API shows them."""
    rows = conn.execute('SELECT * FROM source_status WHERE ?1 IS NULL OR source=?1', (source,))
    return [dict(r) for r in rows]


def by_search_and_source(conn):
    """{(search id, source): row}, for the scheduler."""
    return {(row['search_id'], row['source']): row for row in table(conn)}


def forget(conn, search_id, sources):
    """An edit makes these sources' results invalid: their next result is a new starting stock (started()), and
    what they usually brought says nothing about the changed search."""
    pairs = [(search_id, s) for s in sources]
    conn.executemany('DELETE FROM source_status WHERE search_id=? AND source=?', pairs)
    conn.executemany('DELETE FROM source_history WHERE search_id=? AND source=?', pairs)


def forget_kind(conn, kind):
    """Every row of one search kind's searches (the kind's offers were all deleted)."""
    conn.execute('DELETE FROM source_status WHERE search_id IN (SELECT id FROM searches WHERE kind=?)', (kind,))
    conn.execute('DELETE FROM source_history WHERE search_id IN (SELECT id FROM searches WHERE kind=?)', (kind,))


def share_error(first_id, follower, source):
    """A search sharing the query of FIRST_ID shows the same failure. Returns whether there was one."""
    with store.db() as conn:
        first = of(conn, first_id, source)
    if not first or not first['error']:
        return False
    source_error(follower, source, first['checked_at'], first['error'], first['failures'])
    return True


def _failures(conn, search, source):
    row = _row(conn, search, source)
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
    if state and state['state'] == cadence.RETRYING:
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
    """A source that suddenly lists nothing after a healthy stock usually changed its layout. It stays
    disturbed while it keeps listing nothing, also after a failure in between: source_error keeps the count."""
    with store.db() as conn:
        row = _row(conn, search, source)
    return bool(row) and row['count'] >= SUSPICIOUS_STOCK


def rest(search, source, note, now):
    """A source with nothing for this search (a regional portal outside its area): shown as resting with the
    reason, never as disturbed."""
    with store.db() as conn:
        _show(conn, search['id'], source, now, note=note)
