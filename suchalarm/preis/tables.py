"""The tables of product searches in DATA_DIR/preis.db (store.register). Each entry runs once; only ever append."""

from .. import store
from .domain import clean_title

TABLES = ('offers', 'shipping_baselines', 'idealo_references')


def _create(conn):
    """Offers per search and URL, Vinted offers waiting for their shipping, the idealo new price per search."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS preis.offers (
        search_id INTEGER, source TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL,
        shipping INTEGER, seller TEXT NOT NULL, condition TEXT NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(search_id, url)
      );
      CREATE TABLE IF NOT EXISTS preis.shipping_baselines (search_id INTEGER, url TEXT NOT NULL, PRIMARY KEY(search_id,url));
      CREATE TABLE IF NOT EXISTS preis.idealo_references (search_id INTEGER PRIMARY KEY, value TEXT NOT NULL);
    """)


def _move(conn):
    """The tables lived in suchalarm.db before."""
    for table in TABLES:
        store.move(conn, 'preis', table)


def _clean_titles(conn):
    """eBay UI labels in old title snapshots; neither timestamps nor notification receipts change."""
    for row in conn.execute("SELECT search_id,url,title FROM preis.offers WHERE source='ebay'").fetchall():
        title = clean_title('ebay', row['title'])
        if title != row['title']:
            conn.execute(
                'UPDATE preis.offers SET title=? WHERE search_id=? AND url=?', (title, row['search_id'], row['url'])
            )


MIGRATIONS = (_create, _move, _clean_titles)
