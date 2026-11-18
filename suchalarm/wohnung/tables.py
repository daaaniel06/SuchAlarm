"""The tables of flat searches in DATA_DIR/wohnung.db (store.register). Each entry runs once; only ever append."""

from .. import migrations

TABLES = ('listings', 'listing_details', 'more_pages', 'separated')

# listing_details.state: queued (also while retrying), read, given up after details.DETAIL_ATTEMPTS, or gone (the
# portal answers 404: it deleted the listing).
PENDING, DONE, FAILED, GONE = 'pending', 'done', 'failed', 'gone'
PER_SEARCH = ('listings', 'more_pages', 'separated')  # detail pages are per URL and go with cleanup()


def _create(conn):
    """Flats per search and URL, their detail pages (per URL), further result pages to read after a page of
    nothing but new flats, and pairs the user called "Nicht dieselbe Wohnung" (duplicates.py)."""
    conn.executescript(f"""
      CREATE TABLE IF NOT EXISTS wohnung.listings (
        search_id INTEGER, source TEXT NOT NULL, url TEXT NOT NULL,
        body TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(search_id, url)
      );
      CREATE TABLE IF NOT EXISTS wohnung.listing_details (
        url TEXT PRIMARY KEY, source TEXT NOT NULL, body TEXT, state TEXT NOT NULL DEFAULT '{PENDING}',
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
        migrations.move(conn, 'wohnung', table)


def _cheaper_at(conn):
    """When a listing last asked less rent than before: it may be mailed again (outbox.py)."""
    if 'cheaper_at' not in {r[1] for r in conn.execute('PRAGMA wohnung.table_info(listings)')}:
        conn.execute('ALTER TABLE wohnung.listings ADD COLUMN cheaper_at REAL')


def _stock_pages(conn):
    """Further pages of a new search's whole stock (pages.py): their flats are starting stock, not new."""
    if 'stock' not in {r[1] for r in conn.execute('PRAGMA wohnung.table_info(more_pages)')}:
        conn.execute('ALTER TABLE wohnung.more_pages ADD COLUMN stock INTEGER NOT NULL DEFAULT 0')


def _page_jobs(conn):
    """An id per further-page job (pages.py): a page that loaded for a job replaced meanwhile is dropped.
    Jobs from before get one each too."""
    if 'job' not in {r[1] for r in conn.execute('PRAGMA wohnung.table_info(more_pages)')}:
        conn.execute("ALTER TABLE wohnung.more_pages ADD COLUMN job TEXT NOT NULL DEFAULT ''")
        conn.execute('UPDATE wohnung.more_pages SET job=lower(hex(randomblob(16)))')


def _detail_requests(conn):
    """Whether the user asked for a detail page (details.request_detail): it is read even for a paused search."""
    if 'requested' not in {r[1] for r in conn.execute('PRAGMA wohnung.table_info(listing_details)')}:
        conn.execute('ALTER TABLE wohnung.listing_details ADD COLUMN requested INTEGER NOT NULL DEFAULT 0')


def _restart_district_stock(conn):
    """A district search without radius could not place NHW's and bauverein's whole stock before 11/2026 and
    stored an empty first result. Now the district's outline places it: that first result starts over, so the
    stock found then is starting stock, not new flats to mail."""
    if 'kind' not in {r[1] for r in conn.execute('PRAGMA main.table_info(searches)')}:
        return  # from before flat searches: none to restart
    conn.execute(
        "DELETE FROM main.source_status WHERE source IN ('nhw', 'bauverein') AND search_id IN ("
        "SELECT id FROM main.searches WHERE kind='wohnung' AND COALESCE(radius, 0)=0 AND ("
        "json_extract(filters, '$.place.kind')='district' OR json_extract(filters, '$.place.label') LIKE '%(Kreis)'))"
    )


def _mail_context(conn):
    columns = {row[1] for row in conn.execute('PRAGMA wohnung.table_info(listings)')}
    if 'copy_url' not in columns:
        conn.execute('ALTER TABLE wohnung.listings ADD COLUMN copy_url TEXT')
    if 'awaiting_area' not in columns:
        conn.execute('ALTER TABLE wohnung.listings ADD COLUMN awaiting_area INTEGER NOT NULL DEFAULT 0')


MIGRATIONS = (
    _create,
    _move,
    _cheaper_at,
    _stock_pages,
    _page_jobs,
    _detail_requests,
    _restart_district_stock,
    _mail_context,
)
