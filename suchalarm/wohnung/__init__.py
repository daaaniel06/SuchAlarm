"""Flat searches: ImmoScout24, Kleinanzeigen, WG-Gesucht, Immowelt, ohne-makler.net, immobilien.de, bauverein AG
and NHW."""

from .. import store
from ..core import cadence, images
from . import tables
from .domain import SOURCES, Listing, query_text
from .sources import out_of_area, select

RHYTHMS = {
    'immoscout24': cadence.PORTAL,
    'kleinanzeigen-wohnung': cadence.PORTAL,
    'wg-gesucht': cadence.PORTAL,
    'immowelt': cadence.PORTAL,
    'ohne-makler': cadence.PORTAL,
    'immobilien-de': cadence.PORTAL,
    'bauverein': cadence.LANDLORD,
    'nhw': cadence.LANDLORD,
}
SITES = {'kleinanzeigen-wohnung': 'kleinanzeigen'}  # one budget with the product search on the same site
IMAGE_HOSTS = {
    'immoscout24': r'pictures\.immobilienscout24\.de',
    'kleinanzeigen-wohnung': r'img\.kleinanzeigen\.de',
    'wg-gesucht': r'img\.wg-gesucht\.de',
    'immowelt': r'mms\.immowelt\.de',
    'ohne-makler': r'media\.ohne-makler\.net',
    'immobilien-de': r'immobilien-api\.fly\.dev',
    'bauverein': r'maps\.polyestate\.de',
    'nhw': r'www\.nhw\.de',
}


def _key(source, search):
    """The query text; a regional portal resting for this search gets a key of its own, so its job never
    stands in for the searches that do fetch the shared stock."""
    note = out_of_area(search, source)
    return f'rests: {note}' if note else query_text(source, search)


def _register():
    for source in SOURCES:
        provider = cadence.Provider(
            rhythm=RHYTHMS[source], key=_key, item=Listing, select=select, site=SITES.get(source, '')
        )
        cadence.register(source, provider)
        images.register(source, IMAGE_HOSTS[source])
    store.register('wohnung', tables.MIGRATIONS, tables.PER_SEARCH)


_register()
