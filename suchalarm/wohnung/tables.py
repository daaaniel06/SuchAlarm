"""The tables of flat searches in DATA_DIR/wohnung.db (store.register). Each entry runs once; only ever append."""

from .. import store

TABLES = ('listings', 'listing_details', 'more_pages', 'separated')
PER_SEARCH = ('listings', 'more_pages', 'separated')  # detail pages are per URL and go with cleanup()


def _create(conn):
    """Flats per search and URL, their detail pages (per URL), further result pages to read after a page of
    nothing but new flats, and pairs the user called "Nicht dieselbe Wohnung" (duplicates.py)."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS wohnung.listings (
        search_id INTEGER, source TEXT NOT NULL, url TEXT NOT NULL,
        body TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(search_id, url)
      );
      CREATE TABLE IF NOT EXISTS wohnung.listing_details (
        url TEXT PRIMARY KEY, source TEXT NOT NULL, body TEXT, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS wohnung.more_pages (
        search_id INTEGER, source TEXT NOT NULL, page INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(search_id, source)
      );
      CREATE TABLE IF NOT EXISTS wohnung.separated (
        search_id INTEGER, url TEXT NOT NULL, other TEXT NOT NULL, PRIMARY KEY(search_id, url, other)
      );
    """)


def _move(conn):
    """The tables lived in suchalarm.db before."""
    for table in TABLES:
        store.move(conn, 'wohnung', table)


def _cheaper_at(conn):
    """When a listing last asked less rent than before: it may be mailed again (outbox.py)."""
    if 'cheaper_at' not in {r[1] for r in conn.execute('PRAGMA wohnung.table_info(listings)')}:
        conn.execute('ALTER TABLE wohnung.listings ADD COLUMN cheaper_at REAL')


MIGRATIONS = (_create, _move, _cheaper_at)
