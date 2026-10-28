"""The search kinds. Each has its worker module (preis/worker.py, wohnung/worker.py) with the same steps
and names, so the check loop, the mail and the API never branch on the kind themselves.

A kind's module offers:
- SOURCES (labels), HITS (key of its hit rows in /api/state)
- prepare, rests, fetch, save, finish, EMPTY_IS_SUSPICIOUS: one check (worker.check)
- take_pending, explain, mail (presentation, see preis/mail.py): the digest
- idle, queue_mails, cleanup: the loop
- hits, bookmark, samples, clear, reset (after an edit), search_url, extras: the API
- MODEL (the pydantic search body) and CHOICES (what its editor offers): models.py and /api/state

A kind's own endpoints (wohnung/api.py) are an APIRouter in ROUTERS.
"""

from .preis import worker as preis
from .wohnung import api as wohnung_api
from .wohnung import worker as wohnung

# What every kind's module offers (tests/test_kinds.py checks it); the async ones are awaited.
INTERFACE = (
    'SOURCES',
    'HITS',
    'MODEL',
    'CHOICES',
    'EMPTY_IS_SUSPICIOUS',
    'prepare',
    'rests',
    'fetch',
    'save',
    'finish',
    'take_pending',
    'explain',
    'mail',
    'idle',
    'queue_mails',
    'cleanup',
    'hits',
    'bookmark',
    'samples',
    'clear',
    'reset',
    'search_url',
    'extras',
)
ASYNC = ('prepare', 'fetch', 'idle')
MAIL = ('WORDS', 'price', 'price_text', 'note', 'facts', 'price_range', 'reason', 'subject', 'card')

KINDS = {'preis': preis, 'wohnung': wohnung}
DEFAULT = 'preis'  # a search without kind is a product search, as before flats existed
ROUTERS = (wohnung_api.ROUTER,)


def of(search):
    """The kind module of a search."""
    return KINDS[search.get('kind') or DEFAULT]
