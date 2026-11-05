"""What the page and the links in the mail both do to hits, searches and mails, in one place.

Each returns whether something changed, so a mail link that finds it already done offers nothing to undo.
Gone (a short German reason) when the search or hit has left.
"""

import time

from . import kinds, scheduler, store


class Gone(Exception):
    pass


def _search(search_id):
    search = store.search(search_id)
    if not search:
        raise Gone('Suche nicht gefunden.')
    return search


def bookmark(conn, search_id, url):
    """Put a hit on the watch list; a saved one stays as it is (with its "Beworben")."""
    if store.is_saved(conn, url):
        return False
    search = store.read_search(conn, search_id)
    if not search or not kinds.of(search).bookmark(conn, search, url):
        raise Gone('Treffer nicht mehr vorhanden.')
    return True


UNDO_WINDOW = 10 * 60
_removed: dict = {}  # the watch-list entry taken off last, with its time: undoing brings it back whole


def set_saved(search_id, url, saved):
    """Saving again right after taking a hit off restores its snapshot and "Beworben", even once the hit left."""
    with store.db() as conn:
        if not saved:
            entry = store.saved_entry(conn, url)
            _removed.clear()
            if entry:
                _removed.update(entry=entry, at=time.time())
            return bool(store.unsave(conn, url))
        entry = _removed.get('entry')
        if entry and entry['url'] == url and time.time() - _removed['at'] < UNDO_WINDOW:
            _removed.clear()
            if store.is_saved(conn, url):
                return False  # saved again meanwhile (another tab, "Beworben"): that one is newer
            store.restore_saved(conn, entry)
            return True
        return bookmark(conn, search_id, url)


def set_hidden(search_id, url, hidden):
    """Gone from the hits and from mails; unhiding brings it back."""
    with store.db() as conn:
        if not hidden:
            return bool(store.unhide(conn, search_id, url))
        _search(search_id)
        if (search_id, url) in store.hidden(conn):
            return False
        store.hide(conn, search_id, url, time.time())
        return True


def set_active(search_id, active):
    """Pause a search (a check waiting for it is dropped) or let it run again."""
    if bool(_search(search_id)['active']) == active:
        return False
    with store.db() as conn:
        store.change_search(conn, search_id, ['active'], [int(active)])
    if not active:
        scheduler.cancel(search_id)
    scheduler.wake.set()
    return True


def set_mails(kind, enabled):
    """The mails of one search kind on or off."""
    if kind not in kinds.KINDS:
        raise Gone('Unbekannte Suchart.')
    if store.mail_settings(kind=kind)['enabled'] == enabled:
        return False
    with store.db() as conn:
        store.put_setting(conn, store.mail_key('mail_enabled', kind), '1' if enabled else '0')
    scheduler.wake.set()
    return True
