"""The app's own database: searches, mail history, watch list and settings (DATA_DIR/suchalarm.db).

Each search kind keeps its tables in a file of its own (preis.db, wohnung.db) that it registers here;
db() attaches them, so a query joins them with the shared tables by plain table names and one transaction
covers all. Request budgets, the page cache and the queue live in cadence.db, thumbnails in images/cache.db.
"""

import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from . import migrations


class Search(TypedDict, total=False):
    """A stored search as searches() returns it. Product searches use every column, flat searches query,
    max_price (warm rent in cents), excluded, sources, active, radius and filters (with their place)."""

    id: int
    kind: str  # 'preis' or 'wohnung' (kinds.py)
    query: str
    min_price: int
    max_price: int
    include_shipping: int
    include_fee: int
    excluded: str
    sources: list[str]
    active: int
    category: str
    delivery: str
    plz: str
    radius: int
    negotiable: int
    free: int
    filters: dict[str, Any]
    reference: dict[str, Any] | None  # the idealo new price, added by preis/worker.hits
    learned: list[str]  # words hiding taught the category, added by preis/worker.prepare


# Old per-search timers; the central queue owns all timing. The columns stay for old databases.
LEGACY_COLUMNS = ('interval_minutes', 'last_run', 'next_run')


@dataclass(frozen=True)
class Part:
    """A search kind's own database file. MIGRATIONS run once each (counted by the file's user_version) and
    name their tables with the schema (preis.offers). Foreign keys cannot reach suchalarm.db, so a trigger
    deletes the rows of PER_SEARCH tables along with their search, as ON DELETE CASCADE would."""

    migrations: tuple[Callable[[sqlite3.Connection], None], ...]
    per_search: tuple[str, ...]


PARTS: dict[str, Part] = {}


def register(name, migrations, per_search):
    """DATA_DIR/NAME.db, attached to every connection as schema NAME."""
    PARTS[name] = Part(tuple(migrations), tuple(per_search))


def db_path(name='suchalarm'):
    return Path(os.environ.get('DATA_DIR', '/data')) / f'{name}.db'


# Connections db() holds open in this thread. The app's handlers and its worker share one thread (the event
# loop), so a second connection opened while the first has already written can never get the write lock: the
# first waits for the second, and the whole app stood still for the 20 s timeout ("database is locked" at the
# first mail, #36). Such a second connection gets no wait instead and fails at once, where the code is wrong.
_held = threading.local()


@contextmanager
def db(follow=True):
    """One transaction over suchalarm.db and every kind's file. FOLLOW: delete a search's rows in the kinds'
    files along with it (off only while init() creates the searches table). A function given a connection
    reads and writes through it (reading()); a second one may read, but not write while the first does."""
    open_here = _held.__dict__.setdefault('connections', [])
    writing = any(other.in_transaction for other in open_here)
    connection = sqlite3.connect(db_path(), timeout=0 if writing else 20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    for name, part in PARTS.items():
        connection.execute(f'ATTACH DATABASE ? AS {name}', (str(db_path(name)),))
        if follow:
            # Only a TEMP trigger may reach into another file (by plain table name); it lasts as long as the connection.
            deletes = ' '.join(f'DELETE FROM {table} WHERE search_id=old.id;' for table in part.per_search)
            connection.execute(f'CREATE TEMP TRIGGER {name}_follows AFTER DELETE ON main.searches BEGIN {deletes} END')
    open_here.append(connection)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        open_here.remove(connection)
        connection.close()


def init():
    """Create and migrate every file. The kinds come first: they take their tables out of suchalarm.db
    (migrations.move), so the shared migrations after them find those tables by plain name in the kind's file."""
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with db(follow=False) as conn:
        for schema in ('main', *PARTS):
            conn.execute(f'PRAGMA {schema}.journal_mode=WAL')
        conn.executescript(migrations.SCHEMA)
        for schema, part in PARTS.items():
            migrations.migrate(conn, schema, part.migrations)
        migrations.migrate(conn, 'main', migrations.MIGRATIONS)
        migrations.recover_interrupted_delivery(conn)
        # Signs the links in mails (maillinks.py); made here, as the delivery's open transaction could not.
        conn.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES ('link_secret', ?)", (secrets.token_urlsafe(32),)
        )


# Searches


def _search(row) -> Search:
    search = dict(row)
    for column in LEGACY_COLUMNS:
        search.pop(column, None)
    search['sources'] = json.loads(search['sources'])
    search['filters'] = json.loads(search['filters'])
    return Search(**search)


@contextmanager
def reading(conn=None):
    """CONN when the caller already holds one, else a connection of its own. A function given a connection must
    never open a second one: once the first writes, the second waits for its lock until it times out."""
    if conn is not None:
        yield conn
        return
    with db() as own:
        yield own


def searches(kind=None) -> list[Search]:
    """All searches, newest first; KIND ('preis' or 'wohnung') limits them to one search kind."""
    with db() as conn:
        return read_searches(conn, kind)


def read_searches(conn, kind=None) -> list[Search]:
    """searches() as CONN sees them."""
    rows = conn.execute('SELECT * FROM searches WHERE ?1 IS NULL OR kind=?1 ORDER BY id DESC', (kind,))
    return [_search(r) for r in rows]


def search(search_id) -> Search | None:
    """One search, or None if it was deleted."""
    with db() as conn:
        return read_search(conn, search_id)


def read_search(conn, search_id) -> Search | None:
    """One search as CONN sees it (also an edit not yet committed), or None."""
    row = conn.execute('SELECT * FROM searches WHERE id=?', (search_id,)).fetchone()
    return _search(row) if row else None


def add_search(conn, kind, columns, values):
    """Store a new search of KIND; returns its ID. An ID is never given twice (SQLite would reuse the last
    one after a deletion), so a link in an old mail cannot reach a newer search."""
    last = conn.execute(
        'SELECT MAX(COALESCE((SELECT MAX(id) FROM searches), 0), '
        "COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key='last_search_id'), 0))"
    ).fetchone()[0]
    marks = ','.join('?' * len(columns))
    conn.execute(f'INSERT INTO searches(id,{",".join(columns)},kind) VALUES (?,{marks},?)', (last + 1, *values, kind))
    put_setting(conn, 'last_search_id', str(last + 1))
    return last + 1


def change_search(conn, search_id, columns, values):
    conn.execute(f'UPDATE searches SET {",".join(f"{c}=?" for c in columns)} WHERE id=?', (*values, search_id))


def delete_search(conn, search_id):
    """The search goes with everything that hangs on it: status, outbox, hits in the kinds' files."""
    conn.execute('DELETE FROM searches WHERE id=?', (search_id,))


# Hidden hits, watch list, source status and delivery history, as the API shows them


def hidden(conn):
    """{(search id, url)} of the hits swiped away."""
    return {(r['search_id'], r['url']) for r in conn.execute('SELECT search_id, url FROM hidden')}


def hide(conn, search_id, url, now):
    conn.execute('INSERT OR REPLACE INTO hidden(search_id,url,hidden_at) VALUES (?,?,?)', (search_id, url, now))


def unhide(conn, search_id=None, url=None):
    """Bring back one hidden hit, or all without arguments; returns how many."""
    if search_id is None:
        return conn.execute('DELETE FROM hidden').rowcount
    return conn.execute('DELETE FROM hidden WHERE search_id=? AND url=?', (search_id, url)).rowcount


def saved(conn):
    """The watch list, newest first: snapshots, so a hit stays listed after the provider drops it."""
    return [dict(r) for r in conn.execute('SELECT * FROM saved ORDER BY saved_at DESC')]


def is_saved(conn, url):
    return conn.execute('SELECT 1 FROM saved WHERE url=?', (url,)).fetchone() is not None


def unsave(conn, url=None):
    """Take one hit off the watch list, or all without URL; returns how many."""
    if url is None:
        return conn.execute('DELETE FROM saved').rowcount
    return conn.execute('DELETE FROM saved WHERE url=?', (url,)).rowcount


def saved_entry(conn, url):
    """One watch-list row as it is stored, or None."""
    row = conn.execute('SELECT * FROM saved WHERE url=?', (url,)).fetchone()
    return dict(row) if row else None


def restore_saved(conn, entry):
    """Put back a row saved_entry() returned."""
    columns = list(entry)
    conn.execute(
        f'INSERT OR REPLACE INTO saved({",".join(columns)}) VALUES ({",".join("?" * len(columns))})',
        [entry[c] for c in columns],
    )


def mark_applied(conn, url, when):
    """When the user applied for a saved flat; None takes the mark back."""
    conn.execute('UPDATE saved SET applied_at=? WHERE url=?', (when, url))


def mail_log(conn, limit):
    """The latest deliveries, newest first."""
    rows = conn.execute(
        'SELECT subject, count, status, error, created_at, sent_at FROM batches ORDER BY created_at DESC LIMIT ?',
        (limit,),
    )
    return [dict(r) for r in rows]


def keep_waiting_only(conn, table, search_id):
    """Delete a search's rows of a kind's TABLE (offers, listings) except those waiting in the outbox: an edit
    starts over, but what is due for the mail stays until it goes out (take_pending judges it anew)."""
    conn.execute(
        f'DELETE FROM {table} WHERE search_id=?1 AND url NOT IN '
        '(SELECT url FROM outbox WHERE search_id=?1 AND batch IS NULL)',
        (search_id,),
    )


# Why a hit counts as known at its price; only a lower price mails it again. Only 'sent' is a mail the
# server accepted: the history of notifications. 'known': starting stock, another portal's copy, or found
# while mails were off. 'uncertain': the server may have taken the mail; never sent twice.
REASONS = ('sent', 'known', 'uncertain')


def remember_reported(conn, search_id, url, price, when, reason='known'):
    """A hit counts as known at PRICE for REASON (REASONS)."""
    conn.execute(
        'INSERT OR REPLACE INTO notifications(search_id,url,price,sent_at,reason) VALUES (?,?,?,?,?)',
        (search_id, url, price, when, reason),
    )


def queue(conn, search_id, url, price, when):
    """A hit waits in the outbox for its mail. One already waiting only follows a further drop of its price;
    one being sent stays as it is."""
    conn.execute(
        'INSERT INTO outbox(search_id,url,price,created_at) VALUES (?,?,?,?) '
        'ON CONFLICT(search_id,url) DO UPDATE SET price=excluded.price '
        'WHERE outbox.batch IS NULL AND excluded.price < outbox.price',
        (search_id, url, price, when),
    )


def forget_reports(conn, search_id):
    """A search changed what it compares: its earlier reports and waiting hits no longer say anything."""
    conn.execute('DELETE FROM notifications WHERE search_id=?', (search_id,))
    conn.execute('DELETE FROM outbox WHERE search_id=?', (search_id,))


def drop_queued(conn, pairs):
    """Take waiting hits, (search id, url) PAIRS, out of the outbox: no longer worth a mail."""
    conn.executemany('DELETE FROM outbox WHERE search_id=? AND url=?', pairs)


def pending(conn, kind):
    """Hits of KIND waiting in the outbox for their mail."""
    return conn.execute(
        'SELECT COUNT(*) FROM outbox WHERE batch IS NULL AND search_id IN (SELECT id FROM searches WHERE kind=?)',
        (kind,),
    ).fetchone()[0]


# Settings: a small key-value table; these helpers are the only place that knows its keys.


def settings():
    with db() as conn:
        return {r['key']: r['value'] for r in conn.execute('SELECT * FROM settings')}


def put_setting(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', (key, value))


def delete_setting(conn, key):
    conn.execute('DELETE FROM settings WHERE key=?', (key,))


def paused(values=None):
    """The global pause of automatic checks ("Jetzt prüfen" still works)."""
    return (settings() if values is None else values).get('paused') == '1'


def mail_key(name, kind):
    """Setting key of a per-kind mail option; product searches keep the keys from before flats existed."""
    return name if kind == 'preis' else f'{name}:{kind}'


def mail_settings(values=None, kind='preis'):
    """The mail part of settings() for one search kind, typed. Recipients are shared by every kind."""
    values = settings() if values is None else values
    return {
        'recipients': json.loads(values.get('recipients', '[]')),
        'enabled': values.get(mail_key('mail_enabled', kind)) == '1',
        'frequency': values.get(mail_key('frequency', kind), 'Sofort'),
        'batch_size': int(values.get(mail_key('batch_size', kind), '1')),
    }


MAIL_SERVER_DEFAULT = {'sender': '', 'password': '', 'host': '', 'port': 587}


def mail_server(values=None):
    """The mail server entered on the settings page; the password never leaves the server."""
    values = settings() if values is None else values
    return {**MAIL_SERVER_DEFAULT, **json.loads(values.get('mail_server', '{}'))}
