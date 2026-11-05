"""The search kinds. Each has its worker module (preis/worker.py, wohnung/worker.py) with the same steps
and names, so the check loop, the mail and the API never branch on the kind themselves.

A kind's module offers:
- SOURCES (labels), HITS (key of its hit rows in /api/state)
- prepare, rests, fetch, save, finish, EMPTY_IS_SUSPICIOUS: one check (worker.check)
- take_pending, explain, mail (presentation, see preis/mail.py): the digest
- idle, queue_mails, cleanup: the loop
- hits, saved (watch-list snapshots), bookmark, samples, clear, reset (after an edit), search_url, extras: the API
- MODEL (the pydantic search body) and CHOICES (what its editor offers): models.py and /api/state

A kind's own endpoints (preis/api.py, wohnung/api.py) are an APIRouter in ROUTERS.
"""

from collections.abc import Collection
from sqlite3 import Connection
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from . import store
from .preis import api as preis_api
from .preis import worker as preis
from .preis.models import Search
from .preis.prices import Offer
from .shapes import MailEntry, MailSample, PageCounts, Row
from .wohnung import api as wohnung_api
from .wohnung import worker as wohnung
from .wohnung.domain import Listing
from .wohnung.models import FlatSearch


@runtime_checkable
class Mail(Protocol):
    WORDS: dict[str, str]

    def price(self, search: store.Search, offer: Row, /) -> int: ...
    def price_text(self, search: store.Search, offer: Row, /) -> str: ...
    def note(self, search: store.Search, offer: Row, /) -> str: ...
    def facts(self, offer: Row, /) -> list[str]: ...
    def price_range(self, search: store.Search, /) -> str: ...
    def reason(self, offer: Row, /) -> str: ...
    def subject(self, entries: list[MailSample], title: str, /) -> str: ...
    def card(self, search: store.Search, offer: Row, photo_src: str, links: dict[str, str], /) -> str: ...


@runtime_checkable
class Kind[Item, Extra, Saved, Payload: BaseModel](Protocol):
    """One worker module; fetch, save and finish share their kind's result types."""

    SOURCES: dict[str, str]
    HITS: str
    MODEL: type[Payload]
    CHOICES: dict[str, Any]
    EMPTY_IS_SUSPICIOUS: bool

    @property
    def mail(self) -> Mail: ...

    async def prepare(self, search: store.Search, /) -> None: ...
    def rests(self, search: store.Search, source: str, /) -> str | None: ...
    async def fetch(self, source: str, search: store.Search, /) -> tuple[list[Item], Extra]: ...
    def save(self, search: store.Search, source: str, items: list[Item], extra: Extra, observed: float, /) -> Saved: ...
    def finish(self, search: store.Search, saved: list[Saved], /) -> None: ...
    def take_pending(self, conn: Connection, /) -> list[MailEntry]: ...
    def explain(self, conn: Connection, entries: list[MailEntry], now: float, /) -> None: ...
    async def idle(self) -> None: ...
    def queue_mails(self) -> None: ...
    def cleanup(self) -> int: ...
    def hits(self, searches: list[store.Search], hidden: Collection[tuple[int, str]], /) -> list[Row]: ...
    def saved(self, rows: list[Row], searches: list[store.Search], /) -> list[Row]: ...
    def bookmark(self, conn: Connection, search: store.Search, url: str, /) -> bool: ...
    def samples(self, searches: list[store.Search], limit: int, /) -> list[MailSample]: ...
    def clear(self, conn: Connection, /) -> int: ...
    def reset(self, conn: Connection, search_id: int, old: store.Search, payload: Payload, /) -> None: ...
    def search_url(self, source: str, search: store.Search, /) -> str | None: ...
    def extras(self, searches: list[store.Search], /) -> dict[str, Any]: ...


ASYNC = ('prepare', 'fetch', 'idle')

# These assignments check each worker against its concrete fetch/save/finish and request-body types.
_preis: Kind[Offer, Row | None, tuple[list[Offer], set[str]], Search] = preis
_wohnung: Kind[Listing, PageCounts, set[str], FlatSearch] = wohnung
KINDS: dict[str, Kind[Any, Any, Any, Any]] = {'preis': _preis, 'wohnung': _wohnung}
DEFAULT = 'preis'  # a search without kind is a product search, as before flats existed
ROUTERS = (preis_api.ROUTER, wohnung_api.ROUTER)


def of(search):
    """The kind module of a search."""
    return KINDS[search.get('kind') or DEFAULT]
