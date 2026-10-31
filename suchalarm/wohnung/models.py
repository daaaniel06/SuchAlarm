"""Request bodies of flat searches, validated before any handler runs."""

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import SOURCES, TRAITS

FlatSource = Literal[
    'immoscout24', 'kleinanzeigen-wohnung', 'wg-gesucht', 'immowelt', 'ohne-makler', 'immobilien-de', 'bauverein', 'nhw'
]
FLAT_RADII = (0, 2, 5, 10, 15, 20, 30, 50)
DAY = r'^\d{4}-\d{2}-\d{2}$'


class FlatFilters(BaseModel):
    min_area: float = Field(default=0, ge=0, le=10_000)
    min_rooms: float = Field(default=0, ge=0, le=100)
    exclusions: list[Literal[TRAITS]] = Field(default_factory=list, max_length=4)  # type: ignore[valid-type]
    move_in_from: str | None = Field(default=None, pattern=DAY)
    move_in_by: str | None = Field(default=None, pattern=DAY)


class PickedPlace(BaseModel):
    """A place chosen from the suggestions: its search QUERY and ImmoScout24's GEOPATH."""

    kind: Literal['city', 'plz']
    label: str = Field(min_length=2, max_length=120)
    query: str = Field(min_length=2, max_length=120)
    geopath: str | None = Field(default=None, max_length=200, pattern=r'^/[\w/-]+$')


class FlatSearch(BaseModel):
    """A flat search: QUERY is the place (town or postcode), MAX_PRICE the warm rent in cents."""

    kind: Literal['wohnung']
    query: str = Field(min_length=2, max_length=120)
    max_price: int = Field(gt=0, le=10_000_000)
    radius: int = 0
    excluded: str = Field(default='', max_length=400)
    sources: list[FlatSource] = Field(min_length=1, max_length=len(SOURCES))
    active: bool = True
    filters: FlatFilters = Field(default_factory=FlatFilters)
    picked: PickedPlace | None = None  # counts only while the field still shows its label

    def place(self):
        if self.picked and self.picked.label == self.query:
            return self.picked.model_dump()
        kind = 'plz' if re.fullmatch(r'\d{5}', self.query) else 'city'
        return {'kind': kind, 'label': self.query, 'query': self.query}

    def columns(self):
        """(column names, values) of the stored search."""
        return FLAT_COLUMNS, self.row()

    def row(self):
        """Values of FLAT_COLUMNS."""
        filters = {**self.filters.model_dump(), 'place': self.place()}
        filters['exclusions'] = list(dict.fromkeys(filters['exclusions']))
        return (
            self.query,
            self.max_price,
            self.excluded,
            json.dumps(self.sources),
            self.active,
            self.radius,
            json.dumps(filters),
        )

    @field_validator('query')
    @classmethod
    def clean_place(cls, value):
        value = ' '.join(value.split())
        if len(value) < 2:
            raise ValueError('Ort oder Postleitzahl eingeben.')
        return value

    @field_validator('sources')
    @classmethod
    def unique_sources(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode='after')
    def checks(self):
        if self.radius not in FLAT_RADII:
            raise ValueError('Umkreis wählen.')
        window = self.filters
        if window.move_in_from and window.move_in_by and window.move_in_from > window.move_in_by:
            raise ValueError('„Frei ab“ muss vor „spätestens“ liegen.')
        return self


FLAT_COLUMNS = ('query', 'max_price', 'excluded', 'sources', 'active', 'radius', 'filters')


class Separate(BaseModel):
    search_id: int
    url: str = Field(min_length=8, max_length=2000)


class DetailRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
