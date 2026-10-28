"""Product searches: eBay, Kleinanzeigen, Vinted and shops through idealo."""

from .. import store
from ..core import cadence, images
from . import tables
from .domain import SOURCES, Offer, query_text, select_offers

RHYTHMS = {
    'ebay': cadence.PORTAL,
    'kleinanzeigen': cadence.PORTAL,
    'vinted': cadence.PORTAL,
    'idealo': cadence.REFERENCE,
}
IMAGE_HOSTS = {
    'ebay': r'i\.ebayimg\.com',
    'kleinanzeigen': r'img\.kleinanzeigen\.de|img\.classistatic\.de',
    'idealo': r'cdn\.idealo\.(?:com|de)',
    'vinted': r'images\d*\.vinted\.net',
}


def _register():
    for source in SOURCES:
        provider = cadence.Provider(rhythm=RHYTHMS[source], key=query_text, item=Offer, select=select_offers)
        cadence.register(source, provider)
        images.register(source, IMAGE_HOSTS[source])
    store.register('preis', tables.MIGRATIONS, tables.TABLES)


_register()
