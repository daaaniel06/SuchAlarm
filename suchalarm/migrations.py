"""The schema of suchalarm.db and its migrations. Each entry of MIGRATIONS runs once; only ever append.
A kind's own file has its own list (store.register); move() takes its tables out of suchalarm.db."""

import json

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
  url TEXT NOT NULL, price INTEGER NOT NULL, sent_at REAL NOT NULL, reason TEXT,
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


def _migrate_legacy(conn):
    """Everything older versions left behind, applied once to databases from before versioning."""
    if 'min_price' not in {r[1] for r in conn.execute('PRAGMA table_info(searches)')}:
        conn.execute('ALTER TABLE searches ADD COLUMN min_price INTEGER NOT NULL DEFAULT 0')
    # eBay UI labels in old title snapshots: preis/tables.py cleans them.
    # The idealo new price moved out of the key-value settings into a table that follows its search.
    conn.execute(
        'INSERT OR IGNORE INTO idealo_references(search_id,value) SELECT s.id, x.value FROM settings x JOIN searches s '
        "ON x.key = 'reference:' || s.id WHERE x.value != 'null'"
    )
    conn.execute("DELETE FROM settings WHERE key LIKE 'reference:%'")
    # One recipient became a list.
    old = conn.execute("SELECT value FROM settings WHERE key='recipient'").fetchone()
    if old:
        recipients = [old['value']] if old['value'] else []
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('recipients',?)", (json.dumps(recipients),))
        conn.execute("DELETE FROM settings WHERE key='recipient'")
    # The old mail log becomes the delivery history.
    if not conn.execute('SELECT 1 FROM batches LIMIT 1').fetchone():
        conn.execute(
            'INSERT INTO batches(id,recipients,subject,count,status,error,created_at,sent_at) '
            "SELECT 'log-' || id, '', subject, count, 'sent', NULL, sent_at, sent_at FROM mail_log"
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


def _add_status_unread(conn):
    """How many offers of the portal's own count the parser could not read on the source's last page; NULL where
    the portal names no count."""
    if 'unread' not in {r[1] for r in conn.execute('PRAGMA table_info(source_status)')}:
        conn.execute('ALTER TABLE source_status ADD COLUMN unread INTEGER')


def _add_status_warning(conn):
    """A layout hint beside a successful source result, without marking its offers stale."""
    if 'warning' not in {r[1] for r in conn.execute('PRAGMA table_info(source_status)')}:
        conn.execute('ALTER TABLE source_status ADD COLUMN warning TEXT')


def _add_deliveries(conn):
    """Which search kind a mail was for (its rhythm and pause after a failure are the kind's own), the mail itself
    while a recipient still waits for a retry, and what each recipient's server said."""
    columns = {r[1] for r in conn.execute('PRAGMA table_info(batches)')}
    if 'kind' not in columns:
        conn.execute('ALTER TABLE batches ADD COLUMN kind TEXT')
    if 'message' not in columns:
        conn.execute('ALTER TABLE batches ADD COLUMN message BLOB')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS deliveries (batch TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE, '
        'address TEXT NOT NULL, status TEXT NOT NULL, code INTEGER, attempts INTEGER NOT NULL DEFAULT 0, '
        'next_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(batch, address))'
    )


def _add_report_reason(conn):
    """Why a hit counts as known (REASONS). Rows from before stay NULL: their reason was not recorded."""
    if 'reason' not in {r[1] for r in conn.execute('PRAGMA table_info(notifications)')}:
        conn.execute('ALTER TABLE notifications ADD COLUMN reason TEXT')


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
    _add_status_unread,
    _add_status_warning,
    _add_report_reason,
    _add_deliveries,
)


def move(conn, schema, table):
    """Move TABLE from suchalarm.db into the attached SCHEMA, where the same table already exists."""
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        columns = ','.join(row[1] for row in conn.execute(f'PRAGMA main.table_info({table})'))
        conn.execute(f'INSERT OR IGNORE INTO {schema}.{table}({columns}) SELECT {columns} FROM main.{table}')
        conn.execute(f'DROP TABLE main.{table}')


def migrate(conn, schema, migrations):
    """Run the MIGRATIONS of SCHEMA not applied yet; PRAGMA user_version counts the applied ones."""
    version = conn.execute(f'PRAGMA {schema}.user_version').fetchone()[0]
    for number, migrate in enumerate(migrations[version:], start=version + 1):
        migrate(conn)
        conn.execute(f'PRAGMA {schema}.user_version={number}')


def recover_interrupted_delivery(conn):
    """A restart during delivery leaves an uncertain mail: known, so nothing comes twice, but not as sent."""
    conn.execute(
        'INSERT OR REPLACE INTO notifications(search_id,url,price,sent_at,reason) '
        "SELECT search_id, url, price, created_at, 'uncertain' FROM outbox WHERE batch IS NOT NULL"
    )
    conn.execute('DELETE FROM outbox WHERE batch IS NOT NULL')
    conn.execute(
        "UPDATE batches SET status='unknown', error='Server wurde während des Versands neu gestartet.' "
        "WHERE status='sending'"
    )
    conn.execute("UPDATE deliveries SET status='uncertain' WHERE status='sending'")  # a retry cut off the same way
