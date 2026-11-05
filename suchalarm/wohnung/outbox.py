"""Mail queue: new listings go out once their detail page is read (or cannot be)."""

import time
from sqlite3 import Connection

from .. import mailserver, store
from ..shapes import MailEntry
from . import sources
from .catalog import DETAIL_WINDOW, cents, completed, listings, mailable, offer, twins_by_url
from .domain import match, profile, warm_rent

DETAIL_WAIT = 15 * 60  # a matching listing waits this long for its detail page before it is mailed without

QUEUE_SQL = (
    'SELECT l.search_id, l.url, l.body, MAX(l.first_seen, COALESCE(l.cheaper_at, 0)) AS seen, d.state, '
    'd.body AS detail, n.price AS reported FROM listings l '
    'LEFT JOIN listing_details d ON d.url=l.url '
    'LEFT JOIN notifications n ON n.search_id=l.search_id AND n.url=l.url '
    'WHERE (l.first_seen>=? OR l.cheaper_at>=?) AND (n.url IS NULL OR l.cheaper_at>n.sent_at) '
    'AND NOT EXISTS (SELECT 1 FROM start_offers s WHERE s.search_id=l.search_id AND s.url=l.url) '
    'AND NOT EXISTS (SELECT 1 FROM outbox x WHERE x.search_id=l.search_id AND x.url=l.url) '
    'AND NOT EXISTS (SELECT 1 FROM hidden h WHERE h.search_id=l.search_id AND h.url=l.url)'
)


def _wanted(searches):
    """search id -> (profile, centre) for domain.match, looked up once per search instead of once per listing."""
    return {sid: (profile(search), sources.centre(search)) for sid, search in searches.items()}


def queue_mails(now: float | None = None) -> None:
    """New listings whose check is complete go to the outbox if they fit, and so do known ones whose rent fell
    below what was last mailed. While mails are off, no mailserver is configured or the search is paused,
    they only count as known, so switching on later sends no flood."""
    now = time.time() if now is None else now
    mail = store.mail_settings(kind='wohnung')
    mailing = mail['enabled'] and mail['recipients'] and mailserver.configured()
    searches = {s['id']: s for s in store.searches('wohnung')}
    wanted = _wanted(searches)
    followers = {
        (search_id, row['url']) for search_id, search in searches.items() for row in listings(search) if row['copy']
    }
    with store.db() as conn:
        recent = now - DETAIL_WINDOW
        for row in conn.execute(QUEUE_SQL, (recent, recent)).fetchall():
            search = searches.get(row['search_id'])
            if search is None or (row['state'] == 'pending' and row['seen'] > now - DETAIL_WAIT):
                continue  # the detail page decides; a paused portal must not hold a clear match for hours
            if (row['search_id'], row['url']) in followers:
                # Another portal's copy of a flat that is mailed through its fitting listing: only known.
                store.remember_reported(conn, row['search_id'], row['url'], 0, now)
                continue
            item = completed(row)
            verdict, _ = match(item, *wanted[search['id']])
            if not mailable(item, verdict, row['state']):
                continue  # looked at again while it is recent: a changed search may still want it
            price = cents(warm_rent(item)) or 0
            if row['reported'] is not None and price >= row['reported']:
                continue  # already mailed at this rent or a lower one
            if search['active'] and mailing:
                store.queue(conn, search['id'], item.url, price, now)
            else:
                store.remember_reported(conn, search['id'], item.url, price, now)


def take_pending(conn: Connection) -> list[MailEntry]:
    """Outbox rows of flat searches still worth a mail, as (search, offer, price), oldest first.
    Rows that are not (search gone or paused, listing gone, hidden, no longer fitting, a fallen rent that rose
    again, another portal's copy of a flat mailed through its fitting listing) are deleted."""
    searches = {s['id']: s for s in store.read_searches(conn, 'wohnung')}
    wanted = _wanted(searches)
    # A flat queued before its twin on another portal was recognised (its photo came later): only one mail.
    copies, twins = set(), {}
    for sid, flat_search in searches.items():
        rows = listings(flat_search, conn)
        copies |= {(sid, row['url']) for row in rows if row['copy']}
        twins[sid] = twins_by_url(rows)
    entries, dropped = [], []
    rows = conn.execute(
        'SELECT x.search_id, x.url, l.body, d.body AS detail, d.state, h.url AS hidden, n.price AS reported '
        'FROM outbox x '
        'JOIN searches s ON s.id=x.search_id AND s.kind=? '
        'LEFT JOIN listings l ON l.search_id=x.search_id AND l.url=x.url '
        "LEFT JOIN listing_details d ON d.url=x.url AND d.state IN ('done','failed') "
        'LEFT JOIN hidden h ON h.search_id=x.search_id AND h.url=x.url '
        'LEFT JOIN notifications n ON n.search_id=x.search_id AND n.url=x.url '
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
            item = completed(row)
            verdict, reasons = match(item, *wanted[search['id']])
            # A fall in rent queued for the digest is no news once the rent rose again before it went out.
            dearer = row['reported'] is not None and (cents(warm_rent(item)) or 0) >= row['reported']
            if mailable(item, verdict, row['state']) and not dearer:
                found = offer(item, verdict, reasons)
                found['twins'] = twins.get(search['id'], {}).get(row['url'], [])
                entries.append((search, found, found['price'] or 0))
                continue
        dropped.append((row['search_id'], row['url']))
    store.drop_queued(conn, dropped)
    return entries
