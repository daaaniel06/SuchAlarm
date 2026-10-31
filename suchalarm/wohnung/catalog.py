"""The stored listings of a flat search as rows of the hit list and the mail: completed by their detail
page, judged, and grouped with the same flat on other portals."""

import json
import time
from dataclasses import asdict

from .. import store
from ..core import images
from . import duplicates, sources
from .domain import SOURCES, Listing, match, profile, warm_rent

STALE = 86400  # a listing unseen this long is shown as "zuletzt gesehen" (rows.js); its twin leads instead

DETAIL_WINDOW = 3 * 86400  # older listings are not worth a detail request any more


def cents(euros):
    return None if euros is None else round(euros * 100)


def offer(item, verdict, reasons):
    """A listing as a row of the hit list and the mail: the same keys for both kinds where they mean the same.

    PRICE is the warm rent in cents, ESTIMATED when only the cold rent was known."""
    return dict(
        asdict(item),
        kind='wohnung',
        price=cents(warm_rent(item)),
        estimated=item.rent is None and item.cold_rent is not None,
        status=verdict,
        reasons=reasons,
    )


def mailable(item, verdict, detail_state):
    """Matched, or unclear only because no detail page can tell more about the description."""
    if verdict == 'matched':
        return True
    return verdict == 'unclear' and (item.source not in sources.DETAILS or detail_state == 'failed')


def listings(search):
    """The stored listings of a flat search, completed by their detail pages, each with its verdict.

    Returns dicts: the listing's fields plus status, reasons, first_seen, last_seen and start."""
    wanted, middle = profile(search), sources.centre(search)
    with store.db() as conn:
        rows = conn.execute(
            'SELECT l.*, d.body AS detail, d.state AS detail_state, d.created_at AS asked_at, s.url AS start '
            'FROM listings l LEFT JOIN listing_details d ON d.url=l.url '
            'LEFT JOIN start_offers s ON s.search_id=l.search_id AND s.url=l.url '
            'WHERE l.search_id=? ORDER BY l.first_seen DESC',
            (search['id'],),
        ).fetchall()
    result, now = [], time.time()
    for row in rows:
        item = Listing(**json.loads(row['body']))
        if row['detail_state'] == 'done':
            item = item.with_detail(json.loads(row['detail']))
        verdict, reasons = match(item, wanted, middle)
        result.append(
            dict(
                offer(item, verdict, reasons),
                search_id=search['id'],
                first_seen=row['first_seen'],
                last_seen=row['last_seen'],
                start=bool(row['start']),
                details=_detail_state(row, item.source, now),
            )
        )
    # The same flat on several portals: the first one that fits leads and names the others.
    photos = images.fingerprints([row['url'] for row in result])
    for row in result:
        row['photo_hash'] = photos.get(row['url'])
    firsts = duplicates.groups(result, apart(search['id']))
    members: dict[str, list[dict]] = {}
    for row in result:
        if row['url'] in firsts:
            members.setdefault(firsts[row['url']], []).append(row)
    for group in members.values():
        # The hit list shows a listing the portals still show; the mail goes out with the first that fits.
        recent = [x for x in group if x['last_seen'] >= now - STALE] or group
        leader, mailed = min(recent, key=_fitting_first), min(group, key=_fitting_first)
        for row in group:
            twins = sorted((x for x in group if x is not row), key=lambda x: x['source'])
            row['same_as'] = None if row is leader else leader['url']
            row['copy'] = row is not mailed
            row['also'] = [x['source'] for x in twins] if row is leader else []
            row['twins'] = [_twin(x) for x in twins] if row is leader else []
    for row in result:
        row.setdefault('same_as', None)
        row.setdefault('copy', False)
        row.setdefault('also', [])
        row.setdefault('twins', [])
    return result


def _fitting_first(row):
    """Order within a group of the same flat: a listing the search may want before one it rules out (one
    portal may ask a few euros more), then the first found."""
    return (row['status'] == 'excluded', row['first_seen'], row['url'])


def _twin(row):
    return {'source': row['source'], 'url': row['url'], 'name': SOURCES.get(row['source'], row['source'])}


def twins_by_url(rows):
    """url -> the same flat on the other portals, for the mail's buttons."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row['same_as'] or row['url'], []).append(row)
    return {
        row['url']: [_twin(x) for x in sorted(group, key=lambda x: x['source']) if x is not row]
        for group in groups.values()
        for row in group
    }


def apart(search_id):
    """Pairs of listings the user called "Nicht dieselbe Wohnung", as frozensets of two URLs."""
    with store.db() as conn:
        rows = conn.execute('SELECT url, other FROM separated WHERE search_id=?', (search_id,)).fetchall()
    return {frozenset((row['url'], row['other'])) for row in rows}


def separate(conn, search_id, url):
    """ "Nicht dieselbe Wohnung": the listing leaves its group for good. False if the search does not list it."""
    search = store.search(search_id)
    rows = listings(search) if search and search['kind'] == 'wohnung' else []
    flat = next((row for row in rows if row['url'] == url), None)
    if flat is None:
        return False
    leader = flat['same_as'] or flat['url']
    group = [row for row in rows if row['url'] == leader or row['same_as'] == leader]
    conn.executemany(
        'INSERT OR IGNORE INTO separated VALUES (?,?,?)',
        [(search_id, url, row['url']) for row in group if row['url'] != url],
    )
    return True


def _detail_state(row, source, now):
    """done, pending (being read), failed, available (can be asked for) or none (the portal has no detail page)."""
    if source not in sources.DETAILS:
        return 'none'
    state = row['detail_state']
    if state == 'pending' and row['asked_at'] < now - DETAIL_WINDOW:
        return 'available'  # too old for detail_step: asking again queues it anew
    return state or 'available'


def completed(row):
    """The listing of ROW, completed by its detail page once that is read. A page queued to be read again
    (another rent) no longer counts: until then the card does."""
    item = Listing(**json.loads(row['body']))
    return item.with_detail(json.loads(row['detail'])) if row['state'] == 'done' else item
