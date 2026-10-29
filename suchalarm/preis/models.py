"""Request body of a product search, validated before any handler runs."""

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import CATEGORIES, MAX_ALTERNATIVES, PICKUP_SOURCES, PLATFORMS, RADII, all_terms, alternatives

Source = Literal['ebay', 'kleinanzeigen', 'vinted', 'idealo']
# Changing one of these needs a fresh result; active and sources do not.
SEARCH_FILTERS = (
    'query',
    'alternatives',
    'min_price',
    'max_price',
    'include_shipping',
    'include_fee',
    'excluded',
    'category',
    'delivery',
    'plz',
    'radius',
    'negotiable',
    'free',
    'filters',
)
SEARCH_COLUMNS = (
    'query',
    'alternatives',
    'min_price',
    'max_price',
    'include_shipping',
    'include_fee',
    'excluded',
    'sources',
    'active',
    'category',
    'delivery',
    'plz',
    'radius',
    'negotiable',
    'filters',
    'free',
)


class Filters(BaseModel):
    """Category filters; a search keeps only those its category offers."""

    platform: Literal[tuple(PLATFORMS)] | None = None  # type: ignore[valid-type]  # built from domain.py
    year_from: int | None = Field(default=None, ge=1950, le=2100)
    km_to: int | None = Field(default=None, ge=1000, le=2_000_000)

    def stored(self):
        return {key: value for key, value in self.model_dump().items() if value is not None}


class Search(BaseModel):
    kind: Literal['preis'] = 'preis'
    query: str = Field(min_length=2, max_length=180)
    alternatives: str = Field(default='', max_length=200)  # other spellings, comma-separated
    min_price: int = Field(default=0, ge=0, le=100_000_000)
    max_price: int = Field(gt=0, le=100_000_000)
    include_shipping: bool = True
    include_fee: bool = True  # Vinted's buyer protection on top of the item price
    excluded: str = Field(default='', max_length=400)
    sources: list[Source] = Field(min_length=1, max_length=4)
    active: bool = True
    category: Literal[tuple(CATEGORIES)] = 'sonstiges'  # type: ignore[valid-type]
    delivery: Literal['versand', 'abholung', 'beides'] = 'versand'
    plz: str = Field(default='', max_length=5)
    radius: int = 0
    negotiable: bool = False
    free: bool = False
    filters: Filters = Field(default_factory=Filters)

    def columns(self):
        """(column names, values) of the stored search."""
        return SEARCH_COLUMNS, self.row()

    def row(self):
        """Column values in the order of SEARCH_COLUMNS."""
        return (
            self.query,
            self.alternatives,
            self.min_price,
            self.max_price,
            self.include_shipping,
            self.include_fee,
            self.excluded,
            json.dumps(self.sources),
            self.active,
            self.category,
            self.delivery,
            self.plz,
            self.radius,
            self.negotiable,
            json.dumps(self.filters.stored()),
            self.free,
        )

    def comparable(self, name):
        """A field as the stored search has it, so an edit can tell what changed."""
        return self.filters.stored() if name == 'filters' else getattr(self, name)

    def terms(self):
        return all_terms({'query': self.query, 'alternatives': self.alternatives, 'filters': self.filters.stored()})

    @field_validator('query')
    @classmethod
    def clean_query(cls, value):
        value = ' '.join(value.split())
        if len(value) < 2:
            raise ValueError('Suchbegriff ist zu kurz.')
        return value

    @field_validator('alternatives')
    @classmethod
    def clean_alternatives(cls, value):
        spellings = list(dict.fromkeys(alternatives({'alternatives': value})))
        if len(spellings) > MAX_ALTERNATIVES:
            raise ValueError(f'Höchstens {MAX_ALTERNATIVES} andere Schreibweisen.')
        if any(len(spelling) < 2 for spelling in spellings):
            raise ValueError('Eine andere Schreibweise ist zu kurz.')
        return ', '.join(spellings)

    @field_validator('sources')
    @classmethod
    def unique_sources(cls, value):
        return list(dict.fromkeys(value))

    @model_validator(mode='after')
    def price_range(self):
        if self.min_price >= self.max_price:
            raise ValueError('Mindestpreis muss unter dem Höchstpreis liegen.')
        return self

    @model_validator(mode='after')
    def pickup(self):
        if self.delivery in ('abholung', 'beides'):
            if not re.fullmatch(r'\d{5}', self.plz):
                raise ValueError('Für Abholung eine fünfstellige PLZ eingeben.')
            if self.radius not in RADII:
                raise ValueError('Umkreis wählen.')
            # With "beides" Vinted and idealo still take part, nationwide with shipping.
            if self.delivery == 'abholung' and any(source not in PICKUP_SOURCES for source in self.sources):
                raise ValueError(
                    'Vinted und idealo können nicht im Umkreis suchen. Bei Abholung nur eBay und Kleinanzeigen wählen.'
                )
            # Picked-up offers have no shipping, so all offers compare by item price alone.
            self.include_shipping = False
        else:
            self.plz, self.radius = '', 0
        allowed = CATEGORIES[self.category]['filters']
        self.filters = Filters(**{key: value for key, value in self.filters.model_dump().items() if key in allowed})
        return self
