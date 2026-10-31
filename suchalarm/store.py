"""The app's own database: searches, mail history, watch list and settings (DATA_DIR/suchalarm.db).

Each search kind keeps its tables in a file of its own (preis.db, wohnung.db) that it registers here;
db() attaches them, so a query joins them with the shared tables by plain table names and one transaction
covers all. Request budgets, the page cache and the queue live in cadence.db, thumbnails in images/cache.db.
"""

import json
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY, query TEXT NOT NULL, max_price INTEGER NOT NULL,
  include_shipping INTEGER NOT NULL DEFAULT 1, excluded TEXT NOT NULL DEFAULT '',
  sources TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 60,
  active INTEGER NOT NULL DEFAULT 1, last_run REAL, next_run REAL NOT NULL DEFAULT 0,
  min_price INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS start_offers (
  search_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
  url TEXT NOT NULL, PRIMARY KEY(search_id,url)
);
CREATE TABLE IF NOT EXISTS notifications (
  search_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
  url TEXT NOT NULL, price INTEGER NOT NULL, sent_at REAL NOT NULL,
  PRIMARY KEY(search_id, url)
);
CREATE TABLE IF NOT EXISTS source_status (
  search_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
  source TEXT NOT NULL, checked_at REAL NOT NULL, error TEXT, count INTEGER NOT NULL,
  PRIMARY KEY(search_id, source)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mail_log (
  id INTEGER PRIMARY KEY, search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
  subject TEXT NOT NULL, sent_at REAL NOT NULL, count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  search_id INTEGER REFERENCES searches(id) ON DELETE CASCADE,
  url TEXT NOT NULL, price INTEGER NOT NULL, created_at REAL NOT NULL, batch TEXT,
  PRIMARY KEY(search_id, url)
);
CREATE TABLE IF NOT EXISTS hidden (
  search_id INTEGER REFERENCES searches(id) ON DELETE CASCADE, url TEXT NOT NULL, hidden_at REAL NOT NULL,
  PRIMARY KEY(search_id, url)
);
CREATE TABLE IF NOT EXISTS saved (
  url TEXT PRIMARY KEY, search_id INTEGER, source TEXT NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL,
  shipping INTEGER, seller TEXT NOT NULL, condition TEXT NOT NULL, saved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, recipients TEXT NOT NULL, subject TEXT NOT NULL, count INTEGER NOT NULL,
  status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, sent_at REAL
);
"""


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


@contextmanager
def db(follow=True):
    """One transaction over suchalarm.db and every kind's file. FOLLOW: delete a search's rows in the kinds'
    files along with it (off only while init() creates the searches table)."""
    connection = sqlite3.connect(db_path(), timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    for name, part in PARTS.items():
        connection.execute(f'ATTACH DATABASE ? AS {name}', (str(db_path(name)),))
        if follow:
            # Only a TEMP trigger may reach into another file (by plain table name); it lasts as long as the connection.
            deletes = ' '.join(f'DELETE FROM {table} WHERE search_id=old.id;' for table in part.per_search)
            connection.execute(f'CREATE TEMP TRIGGER {name}_follows AFTER DELETE ON main.searches BEGIN {deletes} END')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# Migrations


def _migrate_legacy(conn):
    """Everything older versions left behind, applied once to databases from before versioning."""
    if 'min_price' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute('ALTER TABLE searches ADD COLUMN min_price INTEGER NOT NULL DEFAULT 0')
    # eBay UI labels in old title snapshots: preis/tables.py cleans them.
    # The idealo new price moved out of the key-value settings into a table that follows its search.
    conn.execute(
        'INSERT OR IGNORE INTO idealo_references SELECT s.id, x.value FROM settings x JOIN searches s '
        "ON x.key = 'reference:' || s.id WHERE x.value != 'null'"
    )
    conn.execute("DELETE FROM settings WHERE key LIKE 'reference:%'")
    # One recipient became a list.
    old = conn.execute("SELECT value FROM settings WHERE key='recipient'").fetchone()
    if old:
        recipients = [old['value']] if old['value'] else []
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('recipients',?)", (json.dumps(recipients),))
        conn.execute("DELETE FROM settings WHERE key='recipient'")
    # The old mail log becomes the delivery history.
    if not conn.execute('SELECT 1 FROM batches LIMIT 1').fetchone():
        conn.execute(
            "INSERT INTO batches SELECT 'log-' || id, '', subject, count, 'sent', NULL, sent_at, sent_at FROM mail_log"
        )


def _add_categories(conn):
    """Category, delivery and its filters. Existing searches become "Sonstiges" with shipping,
    so their provider queries stay exactly the same."""
    existing = {r[1] for r in conn.execute('PRAGMA table_info(searches)')}
    for name, definition in (
        ('category', "TEXT NOT NULL DEFAULT 'sonstiges'"),
        ('delivery', "TEXT NOT NULL DEFAULT 'versand'"),
        ('plz', "TEXT NOT NULL DEFAULT ''"),
        ('radius', 'INTEGER NOT NULL DEFAULT 0'),
        ('negotiable', 'INTEGER NOT NULL DEFAULT 0'),
        ('filters', "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in existing:
            conn.execute(f'ALTER TABLE searches ADD COLUMN {name} {definition}')


def _add_free(conn):
    """Given-away offers ("Zu verschenken"); off for existing searches."""
    if 'free' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute('ALTER TABLE searches ADD COLUMN free INTEGER NOT NULL DEFAULT 0')


def _add_source_failures(conn):
    """Failed attempts in a row per search and source; the main page only reports repeated ones."""
    if 'failures' not in {r[1] for r in conn.execute('PRAGMA table_info(source_status)')}:
        conn.execute('ALTER TABLE source_status ADD COLUMN failures INTEGER NOT NULL DEFAULT 0')


def _add_kind(conn):
    """What a search looks for: 'preis' (products) or 'wohnung' (flats). Every existing search is a product search."""
    if 'kind' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute("ALTER TABLE searches ADD COLUMN kind TEXT NOT NULL DEFAULT 'preis'")


def _moved(conn):
    """Created the tables of a search kind that now live in its own file (wohnung/tables.py)."""


def _add_status_note(conn):
    """Why a source rests for a search (a regional portal outside its area); NULL for a normal result."""
    if 'note' not in {r[1] for r in conn.execute('PRAGMA table_info(source_status)')}:
        conn.execute('ALTER TABLE source_status ADD COLUMN note TEXT')


def _add_saved_detail(conn):
    """Watch-list snapshots of flats get their own columns: the detail line and whether the rent is estimated.
    Snapshots saved before kept them in seller and condition."""
    existing = {r[1] for r in conn.execute('PRAGMA table_info(saved)')}
    for name in ('detail', 'note'):
        if name not in existing:
            conn.execute(f"ALTER TABLE saved ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE saved SET detail=seller, note=condition, seller='', condition='' "
        "WHERE search_id IN (SELECT id FROM searches WHERE kind='wohnung')"
    )


def _add_applied(conn):
    """When the user applied for a saved flat (the application itself happens on the portal)."""
    if 'applied_at' not in {r[1] for r in conn.execute('PRAGMA table_info(saved)')}:
        conn.execute('ALTER TABLE saved ADD COLUMN applied_at REAL')


def _add_fee(conn):
    """Whether a product search counts Vinted's buyer protection; on for existing searches too."""
    if 'include_fee' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute('ALTER TABLE searches ADD COLUMN include_fee INTEGER NOT NULL DEFAULT 1')


def _add_alternatives(conn):
    """Other spellings a product search also looks for, comma-separated like the excluded words."""
    if 'alternatives' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute("ALTER TABLE searches ADD COLUMN alternatives TEXT NOT NULL DEFAULT ''")


def _add_status_started(conn):
    """Whether a source ever delivered a result for a search (status.started): a failure since must not make
    its next result starting stock again. Until now only a clean last result told that."""
    if 'started' not in {r[1] for r in conn.execute('PRAGMA table_info(source_status)')}:
        conn.execute('ALTER TABLE source_status ADD COLUMN started INTEGER NOT NULL DEFAULT 0')
        conn.execute('UPDATE source_status SET started=1 WHERE error IS NULL AND note IS NULL')


# Each entry runs once; PRAGMA user_version counts the applied ones. Only ever append.
MIGRATIONS = (
    _migrate_legacy,
    _add_categories,
    _add_free,
    _add_source_failures,
    _add_kind,
    _moved,  # listings, listing_details
    _add_status_note,
    _add_saved_detail,
    _add_applied,
    _moved,  # more_pages
    _moved,  # separated
    _add_fee,
    _add_alternatives,
    _add_status_started,
)


def move(conn, schema, table):
    """Move TABLE from suchalarm.db into the attached SCHEMA, where the same table already exists."""
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        columns = ','.join(row[1] for row in conn.execute(f'PRAGMA main.table_info({table})'))
        conn.execute(f'INSERT OR IGNORE INTO {schema}.{table}({columns}) SELECT {columns} FROM main.{table}')
        conn.execute(f'DROP TABLE main.{table}')


def _migrate(conn, schema, migrations):
    version = conn.execute(f'PRAGMA {schema}.user_version').fetchone()[0]
    for number, migrate in enumerate(migrations[version:], start=version + 1):
        migrate(conn)
        conn.execute(f'PRAGMA {schema}.user_version={number}')


def _recover_interrupted_delivery(conn):
    """A restart during delivery leaves an uncertain mail: count it as sent so nothing comes twice."""
    conn.execute(
        'INSERT OR REPLACE INTO notifications SELECT search_id, url, price, created_at FROM outbox WHERE batch IS NOT NULL'
    )
    conn.execute('DELETE FROM outbox WHERE batch IS NOT NULL')
    conn.execute(
        "UPDATE batches SET status='unknown', error='Server wurde während des Versands neu gestartet.' "
        "WHERE status='sending'"
    )


def init():
    """Create and migrate every file. The kinds come first: they take their tables out of suchalarm.db
    (store.move), so the shared migrations after them find those tables by plain name in the kind's file."""
    db_path().parent.mkdir(parents=True, exist_ok=True)
    with db(follow=False) as conn:
        for schema in ('main', *PARTS):
            conn.execute(f'PRAGMA {schema}.journal_mode=WAL')
        conn.executescript(SCHEMA)
        for schema, part in PARTS.items():
            _migrate(conn, schema, part.migrations)
        _migrate(conn, 'main', MIGRATIONS)
        _recover_interrupted_delivery(conn)


# Searches


def _search(row) -> Search:
    search = dict(row)
    for column in LEGACY_COLUMNS:
        search.pop(column, None)
    search['sources'] = json.loads(search['sources'])
    search['filters'] = json.loads(search['filters'])
    return Search(**search)


def searches(kind=None) -> list[Search]:
    """All searches, newest first; KIND ('preis' or 'wohnung') limits them to one search kind."""
    with db() as conn:
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
    """Store a new search of KIND; returns its ID."""
    marks = ','.join('?' * len(columns))
    return conn.execute(f'INSERT INTO searches({",".join(columns)},kind) VALUES ({marks},?)', (*values, kind)).lastrowid


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
    conn.execute('INSERT OR REPLACE INTO hidden VALUES (?,?,?)', (search_id, url, now))


def unhide(conn, search_id=None, url=None):
    """Bring back one hidden hit, or all without arguments; returns how many."""
    if search_id is None:
        return conn.execute('DELETE FROM hidden').rowcount
    return conn.execute('DELETE FROM hidden WHERE search_id=? AND url=?', (search_id, url)).rowcount


def statuses(conn, source=None):
    """The last result per search and source, of one SOURCE or all."""
    rows = conn.execute('SELECT * FROM source_status WHERE ?1 IS NULL OR source=?1', (source,))
    return [dict(r) for r in rows]


def saved(conn):
    """The watch list, newest first: snapshots, so a hit stays listed after the provider drops it."""
    return [dict(r) for r in conn.execute('SELECT * FROM saved ORDER BY saved_at DESC')]


def is_saved(conn, url):
    return conn.execute('SELECT 1 FROM saved WHERE url=?', (url,)).fetchone() is not None


def unsave(conn, url=None):
    """Take one hit off the watch list, or all without URL."""
    if url is None:
        conn.execute('DELETE FROM saved')
    else:
        conn.execute('DELETE FROM saved WHERE url=?', (url,))


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
    conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))


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


def backup(keep=7):
    """A consistent copy of suchalarm.db and every kind's file through SQLite's backup API, one per day;
    the newest KEEP of each stay. Returns the copy of suchalarm.db."""
    folder = db_path().parent / 'backups'
    folder.mkdir(exist_ok=True)
    for name in ('suchalarm', *PARTS):
        source, copy = (
            sqlite3.connect(db_path(name), timeout=20),
            sqlite3.connect(folder / time.strftime(f'{name}-%Y-%m-%d.db')),
        )
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        for old in sorted(folder.glob(f'{name}-*.db'))[:-keep]:
            old.unlink()
    return folder / time.strftime('suchalarm-%Y-%m-%d.db')
