"""Request bodies of the API, validated before any handler runs. Each search kind brings its own search model
(preis/models.py, wohnung/models.py); AnySearch and MailSettings are built from the kinds."""

import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, Discriminator, Field, Tag, create_model, field_validator

from .kinds import DEFAULT, KINDS

EMAIL = re.compile(r'[^\s@<>\r\n,;]+@[^\s@<>\r\n,;]+\.[^\s@<>\r\n,;]+')  # no header injection, no lists


class Bookmark(BaseModel):
    search_id: int
    url: str = Field(min_length=8, max_length=2000)
    saved: bool


class Hide(BaseModel):
    search_id: int
    url: str = Field(min_length=8, max_length=2000)
    hidden: bool


class Applied(BaseModel):
    search_id: int
    url: str = Field(min_length=8, max_length=2000)
    applied: bool


class Template(BaseModel):
    text: str = Field(max_length=20000)


class Preferences(BaseModel):
    paused: bool


class AreaMail(BaseModel):
    """Mails of one search kind."""

    enabled: bool = False
    frequency: Literal['Sofort', 'Alle 30 Minuten', 'Täglich'] = 'Sofort'
    batch_size: int = Field(default=1, ge=1, le=3)


class Recipients(BaseModel):
    recipients: list[str] = Field(default_factory=list, max_length=5)

    @field_validator('recipients')
    @classmethod
    def emails(cls, values):
        result: list[str] = []
        for value in values:
            value = value.strip()
            if len(value) > 254 or not EMAIL.fullmatch(value):
                raise ValueError('Gültige Mailadresse eingeben.')
            if value.casefold() not in (item.casefold() for item in result):
                result.append(value)
        return result


def _kind(value):
    return (value.get('kind') if isinstance(value, dict) else getattr(value, 'kind', None)) or DEFAULT


# Both are built from the registered kinds at import time, which a type checker cannot follow.
if TYPE_CHECKING:
    MailSettings = Any
    AnySearch = Any
else:
    # The shared recipients and one AreaMail per search kind, named like the kind.
    MailSettings = create_model(
        'MailSettings', __base__=Recipients, **{name: (AreaMail, Field(default_factory=AreaMail)) for name in KINDS}
    )
    # A search of any kind; a body without "kind" is a product search, as before flats existed.
    AnySearch = Annotated[
        Union[tuple(Annotated[kind.MODEL, Tag(name)] for name, kind in KINDS.items())],  # noqa: UP007
        Discriminator(_kind),
    ]
