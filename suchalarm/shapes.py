"""Shapes of the values that pass between the core and the search kinds; kinds.py checks the kinds against them.
A module of its own, so the kinds' modules can use them without importing kinds.py (which imports them)."""

from typing import Any, NotRequired, TypedDict

from .store import Search

type Row = dict[str, Any]  # a hit, listing or watch-list row as the API and the mail show it
type MailEntry = tuple[Search, Row, int]  # (search, hit, compared price) waiting for its mail
type MailSample = tuple[Search, Row]


class PageCounts(TypedDict):
    """What a flat portal's page held: offers before the search's own filters, and those its parser missed."""

    listed: int
    unread: int | None
    warning: NotRequired[str | None]
