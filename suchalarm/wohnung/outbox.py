"""Mail queue: new listings go out once their detail page is read (or cannot be)."""

import json
import time

from .. import store
from . import sources
from .catalog import DETAIL_WINDOW, cents, completed, listings, mailable, offer, twins_by_url
from .domain import Listing, estimated_rent, match, profile

DETAIL_WAIT = 15 * 60  # a matching listing waits this long for its detail page before it is mailed without

QUEUE_SQL = (
    'SELECT l.search_id, l.url, l.body, l.first_seen, d.state, d.body AS detail FROM listings l '
    'LEFT JOIN listing_details d ON d.url=l.url '
    'WHERE l.first_seen>=? '
    'AND NOT EXISTS (SELECT 1 FROM start_offers s WHERE s.search_id=l.search_id AND s.url=l.url) '
    'AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.search_id=l.search_id AND n.url=l.url) '
    'AND NOT EXISTS (SELECT 1 FROM outbox x WHERE x.search_id=l.search_id AND x.url=l.url) '
    'AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.search_id=l.search_id AND h.url=l.url)'
)


def queue_mails(now=None):
    """New listings whose check is complete go to the outbox if they fit. While mails are off or the
    search is paused they only count as known, so switching on later sends no flood."""
    now = time.time() if now is None else now
    mail = store.mail_settings(kind='wohnung')
    mailing = mail['enabled'] and mail['recipients']
    searches = {s['id']: s for s in store.searches('wohnung')}
    followers = {
        (search_id, row['url']) for search_id, search in searches.items() for row in listings(search) if row['copy']
    }
    with store.db() as conn:
        for row in conn.execute(QUEUE_SQL, (now - DETAIL_WINDOW,)).fetchall():
            search = searches.get(row['search_id'])
            if search is None or (row['state'] == 'pending' and row['first_seen'] > now - DETAIL_WAIT):
                continue  # the detail page decides; a paused portal must not hold a clear match for hours
            if (row['search_id'], row['url']) in followers:
                # Another portal's copy of a flat that is mailed through its first listing: only known.
                conn.execute(
                    'INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)', (row['search_id'], row['url'], 0, now)
                )
                continue
            item = completed(row)
            verdict, _ = match(item, profile(search), sources.centre(search))
            if not mailable(item, verdict, row['state']):
                continue  # looked at again while it is recent: a changed search may still want it
            price = cents(item.rent if item.rent is not None else estimated_rent(item)) or 0
            if search['active'] and mailing:
                conn.execute(
                    'INSERT OR IGNORE INTO outbox(search_id,url,price,created_at) VALUES (?,?,?,?)',
                    (search['id'], item.url, price, now),
                )
            else:
                conn.execute(
                    'INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)', (search['id'], item.url, price, now)
                )


def take_pending(conn):
    """Outbox rows of flat searches still worth a mail, as (search, offer, price), oldest first.
    Rows that are not (search gone or paused, listing gone, hidden, no longer fitting, another portal's copy of
    a flat mailed through its first listing) are deleted."""
    searches = {s['id']: s for s in store.searches('wohnung')}
    # A flat queued before its twin on another portal was recognised (its photo came later): only one mail.
    copies, twins = set(), {}
    for sid, flat_search in searches.items():
        rows = listings(flat_search)
        copies |= {(sid, row['url']) for row in rows if row['copy']}
        twins[sid] = twins_by_url(rows)
    entries, dropped = [], []
    rows = conn.execute(
        'SELECT x.search_id, x.url, l.body, d.body AS detail, d.state, h.url AS hidden FROM outbox x '
        'JOIN searches s ON s.id=x.search_id AND s.kind=? '
        'LEFT JOIN listings l ON l.search_id=x.search_id AND l.url=x.url '
        "LEFT JOIN listing_details d ON d.url=x.url AND d.state IN ('done','failed') "
        'LEFT JOIN hidden h ON h.search_id=x.search_id AND h.url=x.url '
        'WHERE x.batch IS NULL ORDER BY x.created_at',
        ('wohnung',),
    ).fetchall()
    for row in rows:
        search = searches.get(row['search_id'])
        if (
            search
            and search['active']
            and row['body']
            and not row['hidden']
            and (search['id'], row['url']) not in copies
        ):
            item = completed(row) if row['state'] == 'done' else Listing(**json.loads(row['body']))
            verdict, reasons = match(item, profile(search), sources.centre(search))
            if mailable(item, verdict, row['state']):
                found = offer(item, verdict, reasons)
                found['twins'] = twins.get(search['id'], {}).get(row['url'], [])
                entries.append((search, found, found['price'] or 0))
                continue
        dropped.append((row['search_id'], row['url']))
    conn.executemany('DELETE FROM outbox WHERE search_id=? AND url=?', dropped)
    return entries
