"""The flat portals: request URLs and parsers for result lists and detail pages, one module per portal.
No network here.

Parsers return cards with camelCase keys (domain.Listing.from_card). A page without recognisable offers is
browser.Unreadable (shown as disturbed, retried like a failure), every other problem a RuntimeError with a short
German text. sources.py reaches every portal through this package.
"""

from .bauverein import (  # noqa: F401
    BAUVEREIN_CLIENT,
    BAUVEREIN_HEADERS,
    BAUVEREIN_KEY,
    BAUVEREIN_MARKERS,
    BAUVEREIN_REACH,
    BAUVEREIN_WIDGET,
    DARMSTADT,
    POLYESTATE,
    bauverein_detail,
    bauverein_identifier,
    bauverein_parse,
    bauverein_reference,
    bauverein_url,
    polyestate,
)
from .common import (  # noqa: F401
    AREA,
    PLACE,
    PRICE,
    ROOMS,
    SELF_CLOSING,
    VOID_TAGS,
    attribute,
    fallback_cards,
    near,
    plain,
    rescue,
)
from .immoscout import (  # noqa: F401
    IMMOSCOUT_BODY,
    IMMOSCOUT_LIST,
    IMMOSCOUT_UA,
    immoscout_detail,
    immoscout_detail_url,
    immoscout_locations_url,
    immoscout_parse,
    immoscout_places,
    immoscout_region,
    immoscout_url,
)
from .immowelt import (  # noqa: F401
    IMMOWELT,
    IMMOWELT_SECTIONS,
    immowelt_cards,
    immowelt_detail,
    immowelt_location,
    immowelt_place_url,
    immowelt_search,
)
from .kleinanzeigen import (  # noqa: F401
    KLEINANZEIGEN,
    kleinanzeigen_detail,
    kleinanzeigen_location,
    kleinanzeigen_locations_url,
    kleinanzeigen_parse,
    kleinanzeigen_url,
)
from .nhw import NHW, NHW_LIST, nhw_detail, nhw_parse, nhw_url  # noqa: F401
from .wg import (  # noqa: F401
    WG_CARD,
    wg_cities_url,
    wg_city,
    wg_detail,
    wg_parse,
    wg_place,
    wg_slug,
    wg_town,
    wg_url,
)
