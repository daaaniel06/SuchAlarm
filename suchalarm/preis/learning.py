"""What hiding teaches: titles of hidden offers are kept, and their typical words filter the category's next checks.
A word the user takes back (forget) never filters that category again."""

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


def learned(search, remember=True):
    """The words hiding taught the category of SEARCH, across all product searches of that category.
    Without REMEMBER nothing is written (the state poll), the result is the same."""
    category = search.get('category') or 'sonstiges'
    wanted = {s['id']: all_terms(s) for s in store.searches('preis') if (s.get('category') or 'sonstiges') == category}
    ids = list(wanted)
    marks = ','.join('?' * len(ids))
    with store.db() as conn:
        if remember:
            _remember_hidden(conn)
        hidden = [
            (r['title'], wanted[r['search_id']])
            for r in conn.execute(
                f'SELECT t.search_id, t.url, t.title FROM hidden_titles t JOIN hidden h '
                f'ON h.search_id=t.search_id AND h.url=t.url WHERE t.search_id IN ({marks}) UNION '
                f'SELECT o.search_id, o.url, o.title FROM offers o JOIN hidden h '
                f'ON h.search_id=o.search_id AND h.url=o.url WHERE o.search_id IN ({marks})',
                ids + ids,
            )
        ]
        kept = [
            r[0]
            for r in conn.execute(
                f'SELECT title FROM offers o WHERE search_id IN ({marks}) AND NOT EXISTS '
                '(SELECT 1 FROM hidden h WHERE h.search_id=o.search_id AND h.url=o.url)',
                ids,
            )
        ]
        forgotten = {r[0] for r in conn.execute('SELECT word FROM forgotten_words WHERE category=?', (category,))}
    return [word for word in learned_words(hidden, kept) if word not in forgotten]


def by_category(searches):
    """{category: learned words} for the categories of these product searches, for the editor."""
    categories = {s.get('category') or 'sonstiges' for s in searches}
    return {category: learned({'category': category}, remember=False) for category in sorted(categories)}


def forget(category, word):
    """The user takes a learned word back: it no longer filters CATEGORY, now or later."""
    with store.db() as conn:
        conn.execute('INSERT OR IGNORE INTO forgotten_words VALUES (?,?)', (category, word))
