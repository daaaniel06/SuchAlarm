"""What hiding teaches: titles of hidden offers are kept, and their typical words filter the category's next checks."""

from .. import store
from .domain import all_terms, learned_words


def _remember_hidden(conn):
    """Keep the titles of hidden offers while they are still stored; a restored offer teaches nothing."""
    conn.execute(
        'INSERT OR IGNORE INTO hidden_titles SELECT o.search_id, o.url, o.title FROM offers o '
        'JOIN hidden h ON h.search_id=o.search_id AND h.url=o.url'
    )
    conn.execute(
        'DELETE FROM hidden_titles WHERE NOT EXISTS '
        '(SELECT 1 FROM hidden h WHERE h.search_id=hidden_titles.search_id AND h.url=hidden_titles.url)'
    )


def learned(search):
    """The words hiding taught the category of SEARCH, across all product searches of that category."""
    category = search.get('category') or 'sonstiges'
    wanted = {s['id']: all_terms(s) for s in store.searches('preis') if (s.get('category') or 'sonstiges') == category}
    ids = list(wanted)
    marks = ','.join('?' * len(ids))
    with store.db() as conn:
        _remember_hidden(conn)
        hidden = [
            (r['title'], wanted[r['search_id']])
            for r in conn.execute(f'SELECT search_id, title FROM hidden_titles WHERE search_id IN ({marks})', ids)
        ]
        kept = [
            r[0]
            for r in conn.execute(
                f'SELECT title FROM offers o WHERE search_id IN ({marks}) AND NOT EXISTS '
                '(SELECT 1 FROM hidden h WHERE h.search_id=o.search_id AND h.url=o.url)',
                ids,
            )
        ]
    return learned_words(hidden, kept)
