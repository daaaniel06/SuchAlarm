"""Fetching flat portals: one shared, budgeted request per portal query, place lookups, detail pages.

ImmoScout24 (app API), bauverein (JSON), NHW, WG-Gesucht, ohne-makler.net and immobilien.de (HTML) are
fetched directly; Kleinanzeigen and Immowelt through the remote browser. Every request goes through the
page budget of its site.
"""

import json
import logging
import re
from urllib.parse import urlencode

from ..core import browser, cadence, http, images
from . import portals
from .domain import (
    SOURCES,
    WHOLE_STOCK,
    Listing,
    Profile,
    beyond_radius,
    distance_km,
    home_area,
    inside,
    is_district,
    outside_home,
    place_query,
    profile,
    wanted_ad,
)

log = logging.getLogger('suchalarm')

OSM_UA = 'Suchalarm/1.0 (private single-user apartment search)'
POSTCODE_MIN_RADIUS = 2  # km around a postcode's centre on ImmoScout24
BROWSER_HOSTS = {
    'kleinanzeigen-wohnung': {'www.kleinanzeigen.de', 'kleinanzeigen.de'},
    'immowelt': {'www.immowelt.de', 'immowelt.de'},
}
KLEINANZEIGEN_CARDS = 'article[data-adid]'


def _extract_cards(selector):
    """A function expression for page.evaluate: only the cards, so the page cache stays small."""
    return f"""() => {{
  const cards = [...document.querySelectorAll({json.dumps(selector)})];
  return {{ title: document.title, text: document.body ? document.body.innerText.slice(0, 20000) : '',
           rows: cards.map(() => ({{}})), html: cards.map(card => card.outerHTML).join('\\n') }};
}}"""


EXTRACT_PAGE = """() => ({ title: document.title, text: document.body ? document.body.innerText.slice(0, 20000) : '',
  rows: [], html: document.documentElement.outerHTML })"""


# Offers per result page of each paged portal (the portal modules name how they were checked).
PAGE_SIZES = {
    'immoscout24': portals.immoscout.PAGE_SIZE,
    'kleinanzeigen-wohnung': portals.kleinanzeigen.PAGE_SIZE,
    'wg-gesucht': portals.wg.PAGE_SIZE,
    'immowelt': portals.immowelt.IMMOWELT_PAGE_SIZE,
    'ohne-makler': portals.ohnemakler.PAGE_SIZE,
    'immobilien-de': portals.immobiliende.PAGE_SIZE,
}


# Only fields present on every normal fixture card; photos and move-in dates are optional.
CARD_FIELDS = {
    'immoscout24': ('cold_rent', 'area', 'rooms', 'place'),
    'kleinanzeigen-wohnung': ('cold_rent',),  # sellers may omit size, rooms and place
    'wg-gesucht': ('rent', 'area', 'rooms', 'place'),
    'immowelt': ('area', 'rooms', 'place'),  # some rooms only name warm rent
    'ohne-makler': ('cold_rent', 'area', 'rooms', 'place'),
    'immobilien-de': ('cold_rent', 'place'),  # rooms may lack size and room count
    'bauverein': ('rent', 'area', 'rooms', 'place'),
    'nhw': ('rent', 'cold_rent', 'area', 'rooms', 'place'),
}
FIELD_LABELS = {
    'rent': 'Mietangaben',
    'cold_rent': 'Kaltmieten',
    'area': 'Flächenangaben',
    'rooms': 'Zimmer',
    'place': 'Ortsangaben',
}


FIELD_CHECK_CARDS = 10  # a smaller page may well be five private ads that all say only "VB"


def field_warning(source, listings):
    """Warn when at least 90% of FIELD_CHECK_CARDS or more cards lack a normally supplied field."""
    if len(listings) < FIELD_CHECK_CARDS:
        return None
    missing = [
        (FIELD_LABELS[field], sum(getattr(listing, field) in (None, '') for listing in listings))
        for field in CARD_FIELDS[source]
    ]
    missing = [(label, count) for label, count in missing if count * 10 >= len(listings) * 9]
    if not missing:
        return None
    extent = 'allen' if all(count == len(listings) for _, count in missing) else 'fast allen'
    return f'Seitenaufbau geändert? {", ".join(label for label, _ in missing)} fehlen bei {extent} Inseraten'


def unread(source, page, read, total):
    """How many offers PAGE should hold by the portal's own count TOTAL (for the whole query) but the parser did
    not READ; None where the portal names no count. A page holds a full page size, the last one the rest."""
    size = PAGE_SIZES.get(source)
    if total is None or size is None:
        return None
    expected = min(size, max(0, total - (page - 1) * size))
    return max(0, expected - read)


# --- Places ------------------------------------------------------------------------------


def _key(query):
    return ' '.join(query.split()).casefold()


async def locate(query, follow=False):
    """(lat, lon, federal state) of a town or postcode from OpenStreetMap; looked up once, kept for good."""
    known = cadence.location('osm', _key(query))
    if known:
        return tuple(json.loads(known))
    params = {'postalcode': query} if re.fullmatch(r'\d{5}', query) else {'q': query}
    url = 'https://nominatim.openstreetmap.org/search?' + urlencode(
        {**params, 'countrycodes': 'de', 'format': 'json', 'limit': 1, 'addressdetails': 1}
    )
    result = await http.get_json('osm', url, {'User-Agent': OSM_UA}, follow=follow, kind='osm')
    if not isinstance(result, list) or not result:
        raise RuntimeError(f'Ort „{query}“ nicht gefunden.')
    value = [float(result[0]['lat']), float(result[0]['lon']), str((result[0].get('address') or {}).get('state') or '')]
    cadence.save_location('osm', _key(query), json.dumps(value))
    return tuple(value)


async def district_area(query, follow=False):
    """The outline of a district (GeoJSON, simplified) from OpenStreetMap; looked up once, kept for good.
    None if OpenStreetMap knows no such boundary."""
    known = cadence.location('osm-area', _key(query))
    if known:
        return json.loads(known)
    url = 'https://nominatim.openstreetmap.org/search?' + urlencode(
        {
            'q': f'Landkreis {query}',
            'countrycodes': 'de',
            'format': 'json',
            'limit': 3,
            'polygon_geojson': 1,
            'polygon_threshold': 0.005,
        }
    )
    result = await http.get_json('osm', url, {'User-Agent': OSM_UA}, follow=follow, kind='osm')
    area = next(
        (
            r['geojson']
            for r in (result if isinstance(result, list) else [])
            if r.get('class') == 'boundary' and (r.get('geojson') or {}).get('type') in ('Polygon', 'MultiPolygon')
        ),
        None,
    )
    cadence.save_location('osm-area', _key(query), json.dumps(area))
    return area


def known_district_area(search):
    """The district outline of a search if looked up already (never a request), else None."""
    known = cadence.location('osm-area', _key(place_query(profile(search))))
    return json.loads(known) if known else None


def centre(search):
    """The searched place's centre if it was looked up already, else None (never a request)."""
    known = cadence.location('osm', _key(place_query(profile(search))))
    return tuple(json.loads(known)[:2]) if known else None


def needs_centre(search):
    p = profile(search)
    return bool(p['radius']) or (p['place'] or {}).get('kind') == 'plz'


def needs_district_area(search):
    """A district search without radius places a landlord's whole stock by the district's outline."""
    p = profile(search)
    return not p['radius'] and is_district(p['place'] or {}) and bool(set(WHOLE_STOCK) & set(search['sources']))


def needs_place(search):
    """Whether the search's place has to be looked up: for its radius or postcode, and for the regional portals."""
    return needs_centre(search) or bool({'bauverein', 'nhw'} & set(search['sources']))


def out_of_area(search, source):
    """Why a regional portal has nothing for this search, or None to fetch it. Never a request: an unknown
    place counts as inside, so the portal is asked as before."""
    p = profile(search)
    place = p['place'] or {}
    if source == 'wg-gesucht' and place.get('kind') == 'plz':
        return 'Sucht nur nach Ortsnamen, nicht nach Postleitzahl'
    if cadence.location(_place_key(source), _key(_place_query(source, p))) == UNKNOWN_PLACE:
        return 'Kennt diesen Ort nicht'
    if source in WHOLE_STOCK and needs_district_area(search):
        # Without the outline every flat of the stock would be left out, and that empty result would become the
        # search's starting stock: the flats then found would be mailed as new. The source waits instead.
        outline = cadence.location('osm-area', _key(place_query(p)))
        if outline is None:
            return 'Wartet auf den Umriss des Kreises'
        if json.loads(outline) is None:
            return 'Kreis auf der Karte nicht gefunden'
    known = cadence.location('osm', _key(place_query(p)))
    if known and source == 'bauverein':
        lat, lon, _ = json.loads(known)
        if distance_km((lat, lon), portals.DARMSTADT) > portals.BAUVEREIN_REACH + p['radius']:
            return 'Nur Darmstadt und Umgebung'
    if known and source == 'nhw' and not p['radius']:  # a radius may reach into Hesse; select() measures it
        state = json.loads(known)[2]
        if state and state != 'Hessen':
            return 'Nur in Hessen'
    return None


_suggestions: dict[str, list] = {}  # typed text -> places; every prefix is a query, so the size is capped


async def suggest(text):
    """Places for the field while typing, from ImmoScout24; [] while its small budget asks to wait."""
    text = ' '.join(text.split())[:60]
    if len(text) < 3:
        return []
    key = text.casefold()
    if key in _suggestions:
        return _suggestions[key]
    try:
        entries = await http.get_json('immoscout24', portals.immoscout_locations_url(text), kind='suggest')
    except cadence.Refused:
        cadence.outcome('immoscout24', 'suggest', True)
        return []
    except cadence.Deferred:
        return []  # the field asks again at the next pause
    except Exception as exc:
        log.warning('Place suggestions failed (%s)', type(exc).__name__)
        return []
    places = portals.immoscout_places(entries)
    if len(_suggestions) >= 500:
        del _suggestions[next(iter(_suggestions))]
    _suggestions[key] = places
    return places


# A portal's own place search is asked once per place; what it found is kept for good (cadence.location).
UNKNOWN_PLACE = ''  # kept for a place the portal does not know: asked once, then the source rests (out_of_area)
PLACE_KEYS = {'kleinanzeigen-wohnung': 'kleinanzeigen'}  # Kleinanzeigen keeps flats' and products' places together


def _place_key(source):
    return PLACE_KEYS.get(source, source)


def _place_query(source, p):
    """What the portal's place search is asked: WG-Gesucht takes the place as the suggestion named it."""
    if source == 'wg-gesucht':
        return (p['place'] or {}).get('label') or place_query(p)
    return place_query(p)


async def _resolve_place(source, p, lookup, book=False):
    """(the portal's own name or ID for the search's place, whether it was looked up just now). LOOKUP asks the
    portal and returns None for a place it does not know; BOOK: the lookup is a page visit of its own in the
    budget. An unknown place fails the fetch and rests the source from then on."""
    query = _place_query(source, p)
    known = cadence.location(_place_key(source), _key(query))
    looked_up = known is None
    if looked_up:
        if book:
            cadence.book(source, 'page')
        found = await lookup()
        known = UNKNOWN_PLACE if found is None else found
        cadence.save_location(_place_key(source), _key(query), known)
    if known == UNKNOWN_PLACE:
        raise RuntimeError(f'{SOURCES[source]} kennt den Ort „{query}“ nicht.')
    return known, looked_up


async def _wg_city(place):
    """What WG-Gesucht's place field suggests for the place, as stored ('[slug, ID]'), or None."""
    try:
        answer = await http.get_json('wg-gesucht', portals.wg_cities_url(place))
    except http.NotFound:
        return None
    city = portals.wg_city(answer, place)
    return json.dumps(city) if city else None


# --- Result lists -----------------------------------------------------------------------


async def _immoscout(p: Profile, page=1):
    query, place = place_query(p), p['place'] or {}
    headers = {'User-Agent': portals.IMMOSCOUT_UA, 'Content-Type': 'application/json'}
    if place.get('kind') == 'plz':
        # ImmoScout24 has no postcode region: search around the postcode's centre.
        lat, lon, _ = await locate(query)
        url = portals.immoscout_url({**p, 'radius': max(p['radius'], POSTCODE_MIN_RADIUS)}, '', (lat, lon), page)
        return await http.get_json('immoscout24', url, headers, portals.IMMOSCOUT_BODY)

    async def region():
        entries = await http.get_json('immoscout24', portals.immoscout_locations_url(query))
        return portals.immoscout_region(entries, query)

    geopath, looked_up = (
        (place['geopath'], False) if place.get('geopath') else await _resolve_place('immoscout24', p, region)
    )
    coords = (await locate(query))[:2] if p['radius'] else None
    url = portals.immoscout_url(p, geopath, coords, page)
    return await http.get_json('immoscout24', url, headers, portals.IMMOSCOUT_BODY, follow=looked_up)


def _kleinanzeigen_readable(data, follow):
    return bool(data['rows']) or browser.EMPTY.search(data['text']) or 'Leider keine' in data['text']


async def _kleinanzeigen(p, number=1):
    source, query = 'kleinanzeigen-wohnung', place_query(p)
    async with browser.browser_page(BROWSER_HOSTS[source]) as page:

        async def location():
            response = await page.goto(portals.kleinanzeigen_locations_url(query), timeout=browser.PAGE_TIMEOUT_MS)
            browser.check_response(response)
            try:
                found = json.loads(await response.text())  # Firefox shows JSON in its viewer, not as body text
            except ValueError:
                raise browser.Unreadable('Ortssuche von Kleinanzeigen nicht lesbar.') from None
            return portals.kleinanzeigen_location(found, query, exact=is_district(p['place'] or {}))

        # The place lookup is a visit of its own in the site's budget; the list then waits for the next slot.
        identifier, looked_up = await _resolve_place(source, p, location, book=True)
        data = await browser.load_page(
            page,
            source,
            portals.kleinanzeigen_url(p, identifier, number),  # the result page's number
            looked_up,
            extract=_extract_cards(KLEINANZEIGEN_CARDS),
            readable=_kleinanzeigen_readable,
        )
    # An empty page passed on its text.
    return portals.kleinanzeigen_parse(data['html']) if data['rows'] else [], portals.kleinanzeigen_total(data['text'])


def _immowelt_list(request):
    """A function expression for page.evaluate: the offers of REQUEST (portals.immowelt_search), asked for
    from inside Immowelt's own result page, then their contents in batches of portals.IMMOWELT_BATCH. A
    refusal reads as a blocked page; any other failed answer leaves 'total' empty, so the page counts as
    unreadable."""
    return f"""async () => {{
  const page = {{ title: document.title, text: document.body ? document.body.innerText.slice(0, 20000) : '',
                 rows: [], total: null }};
  const read = async response => {{
    if ({json.dumps(browser.REFUSAL_STATUSES)}.includes(response.status)) page.text = 'Zugriff verweigert\\n' + page.text;
    return response.ok ? response.json() : null;
  }};
  const found = await read(await fetch('/serp-bff/search', {{
    method: 'POST', headers: {{ 'content-type': 'application/json' }}, body: JSON.stringify({json.dumps(request)}) }}));
  if (!found) return page;
  const ids = found.classifieds.map(offer => offer.id);
  for (let start = 0; start < ids.length; start += {portals.IMMOWELT_BATCH}) {{
    const batch = ids.slice(start, start + {portals.IMMOWELT_BATCH});
    const offers = await read(await fetch('/classifiedList/' + batch.join(','), {{ headers: {{ 'x-language': 'de' }} }}));
    if (!offers) return page;
    page.rows.push(...offers);
  }}
  page.total = found.totalCount;
  return page;
}}"""


async def _immowelt(p, number=1):
    """The place's newest offers, already cut to the search's limits.

    The place's page is found once through its old address, which redirects to it; that visit then waits
    for the next slot like any second page. The list is asked for from the search's own result page,
    which keys the page cache."""
    source, query = 'immowelt', place_query(p)
    async with browser.browser_page(BROWSER_HOSTS[source]) as page:

        async def location():
            response = await page.goto(portals.immowelt_place_url(query), timeout=browser.PAGE_TIMEOUT_MS)
            try:
                browser.check_response(response)
            except browser.NotFound:
                return None  # no page for this place ("Darmstadt-Dieburg" answers 410)
            return portals.immowelt_location(page.url, query)

        place, looked_up = await _resolve_place(source, p, location, book=True)
        address, request = portals.immowelt_search(place, p, number)
        data = await browser.load_page(
            page,
            source,
            address,
            looked_up,
            extract=_immowelt_list(request),
            readable=lambda data, follow: data['total'] is not None,
        )
    return portals.immowelt_cards(data['rows']), data['total']


async def _wg_gesucht(p, page=1):
    """The town's list; its ID is found once through the site's place field, which takes a visit of
    its own in the budget, so the list then follows as a further request of the same fetch."""
    source = 'wg-gesucht'
    if (p['place'] or {}).get('kind') == 'plz':
        raise RuntimeError('WG-Gesucht sucht nur nach Ortsnamen, nicht nach Postleitzahl.')
    city, looked_up = await _resolve_place(source, p, lambda: _wg_city(_place_query(source, p)))
    html = await http.get_html(source, portals.wg_url(json.loads(city), p, page), follow=looked_up)
    return portals.wg_parse(html), portals.wg_total(html)


async def _result_page(source, url, page, follow):
    """A result page's HTML; past the last page the portals answer 404, which means no further offers."""
    try:
        return await http.get_html(source, url, follow=follow)
    except http.NotFound:
        if page > 1:
            return None
        raise


async def _ohne_makler(p, page=1):
    """The newest offers of a postcode, or of a town: its path is found once through the site's place
    search and the state from OpenStreetMap, then the list follows as a further request."""
    source = 'ohne-makler'
    if (p['place'] or {}).get('kind') == 'plz':
        place, looked_up = place_query(p), False
    else:
        place, looked_up = await _resolve_place(source, p, lambda: _om_path(p))
    html = await _result_page(source, portals.om_url(place, p, page), page, looked_up)
    return (portals.om_parse(html), portals.om_total(html)) if html else ([], None)


async def _om_path(p):
    town = home_area(p)
    name = portals.om_town(await http.get_json('ohne-makler', portals.om_places_url(town)), town)
    state = (await locate(place_query(p), follow=True))[2] if name else ''
    return portals.om_path(state, name) if state else None


async def _immobilien_de(p, page=1):
    """The town's newest offers (a postcode stands for its town: the site has no radius). The town's
    token is found once through the site's place search, then the list follows as a further request."""
    source = 'immobilien-de'
    slug, looked_up = await _resolve_place(source, p, lambda: _id_slug(p))
    html = await _result_page(source, portals.id_url(slug, p, page), page, looked_up)
    return (portals.id_parse(html), portals.id_total(html)) if html else ([], None)


async def _id_slug(p):
    town = home_area(p)
    if (p['place'] or {}).get('kind') == 'plz':
        town = portals.id_town(await http.get_json('immobilien-de', portals.id_places_url(town)))
        if not town:
            return None
    answer = await http.get_json('immobilien-de', portals.id_places_url(town), follow=True)
    return portals.id_city(answer, town) or None


async def _bauverein():
    """The landlord's stock. A flat without a picture needs one more request for its reference number;
    the stock is kept in the page cache and found references for good, so a fetch that has to wait for
    the next slot continues where it stopped instead of starting over."""
    url, payload = portals.polyestate('get-marker', portals.BAUVEREIN_MARKERS)
    data = cadence.cached_page('bauverein', url)
    if data is None:
        data = await http.get_json('bauverein', url, portals.BAUVEREIN_HEADERS, payload)
        cadence.save_page('bauverein', url, data)
    cards = portals.bauverein_parse(data)
    for card in cards:
        if card['url'] is not None:
            continue
        identifier = int(card['bauvereinId'])
        reference = cadence.location('bauverein', f'ref:{identifier}')
        if reference is None:
            details, body = portals.polyestate('object-details', {'id': identifier})
            answer = await http.get_json('bauverein', details, portals.BAUVEREIN_HEADERS, body, follow=True)
            reference = portals.bauverein_reference(answer)
            cadence.save_location('bauverein', f'ref:{identifier}', reference)
        card['url'] = portals.bauverein_url(reference, identifier)
    return cards


PAGED = (
    'immoscout24',
    'kleinanzeigen-wohnung',
    'wg-gesucht',
    'ohne-makler',
    'immobilien-de',
    'immowelt',
)  # the others list everything on one page


async def _immoscout_cards(p, page):
    data = await _immoscout(p, page)
    return portals.immoscout_parse(data), portals.immoscout_total(data)


async def _bauverein_cards(p, page):
    return await _bauverein(), None  # a landlord's whole stock: no count to check


async def _nhw_cards(p, page):
    return portals.nhw_parse(await http.get_html('nhw', portals.NHW_LIST)), None


# How each portal's result list is read: (search profile, page) -> (cards, the portal's count for the whole query
# or None where it names none).
LISTS = {
    'immoscout24': _immoscout_cards,
    'kleinanzeigen-wohnung': _kleinanzeigen,
    'immowelt': _immowelt,
    'wg-gesucht': _wg_gesucht,
    'ohne-makler': _ohne_makler,
    'immobilien-de': _immobilien_de,
    'bauverein': _bauverein_cards,
    'nhw': _nhw_cards,
}


async def cards(source, search, page=1):
    """(cards, total) of one portal query, newest first; PAGE counts only on the PAGED portals. TOTAL: how many
    offers the portal says the whole query has, None where it does not say (unread())."""
    return await LISTS[source](profile(search), page)


@cadence.shared_search(cache_version=4)  # 4: field warnings alongside the page counts
async def fetch(source, search):
    """(listings, counts) of one portal query; the decorator shares, caches and times it, select() filters per
    search. COUNTS: how many the page held ('listed', tells pages.py whether it was full) and how many of the
    portal's own count the parser missed ('unread', None where the portal names none)."""
    return await _page(source, search)


async def more(source, search, page):
    """(listings of this search, counts as fetch()) of a further result page, while a search takes in its stock
    or after a burst of new flats: not shared or cached, only booked."""
    listings, counts = await _page(source, search, page)
    return select(listings, search), counts


async def _page(source, search, page=1):
    found, total = await cards(source, search, page)
    listings = [Listing.from_card(source, card) for card in found]
    images.remember(source, {listing.url: listing.image for listing in listings if listing.image})
    missed = unread(source, page, len(found), total)
    if missed:
        log.warning('%s: page %s lists %s offers the parser could not read', source, page, missed)
    warning = field_warning(source, [Listing.from_card(source, portals.own_reading(card)) for card in found])
    if warning:
        log.warning('%s: page %s: %s', source, page, warning)
    return listings, {'listed': len(listings), 'unread': missed, 'warning': warning}


def select(listings, search):
    """The listings of a shared result that belong to this search: in its place, and offers only.

    Everything else (rent, size, traits, words, move-in) is judged per listing by domain.match, also after
    the detail page; a listing ruled out by an estimated rent may still turn out to fit.
    """
    p, middle = profile(search), centre(search)
    district = not p['radius'] and is_district(p['place'] or {})
    area = known_district_area(search) if district else None
    kept = []
    for listing in listings:
        if wanted_ad(listing) or beyond_radius(listing, p, middle) is not None:
            continue
        # A landlord's whole stock is not searched by the district: only a flat inside its outline counts.
        if (
            district
            and listing.source in WHOLE_STOCK
            and not (area and listing.lat is not None and inside(area, listing.lat, listing.lon))
        ):
            continue
        if listing.source in WHOLE_STOCK and p['radius']:
            spot = (listing.lat, listing.lon) if listing.lat is not None else None
            if not portals.near(place_query(p), p['radius'], middle, listing.place, spot):
                continue
        elif outside_home(listing, p):
            continue
        kept.append(listing)
    return kept


# --- Detail pages -----------------------------------------------------------------------


async def _browser_page(source, url, extract=EXTRACT_PAGE, readable=lambda data, follow: bool(data['html'])):
    async with browser.browser_page(BROWSER_HOSTS[source]) as page:
        data = await browser.load_page(page, source, url, extract=extract, readable=readable)
    return data['html']


# Only the exposé's sections: a whole Immowelt page (about 700 kB) would crowd the page cache.
IMMOWELT_EXPOSE = _extract_cards(', '.join(f'[data-testid="{name}"]' for name in portals.IMMOWELT_SECTIONS))


async def _immowelt_detail(url):
    try:
        html = await _browser_page(
            'immowelt', url, IMMOWELT_EXPOSE, lambda data, follow: 'data-testid="cdp-price"' in data['html']
        )
    except browser.Blocked as exc:
        # A refused exposé skips only this listing; whether Immowelt blocks shows in its result list.
        raise RuntimeError(str(exc)) from None
    return portals.immowelt_detail(html)


async def _immoscout_detail(url):
    data = await http.get_json('immoscout24', portals.immoscout_detail_url(url), {'User-Agent': portals.IMMOSCOUT_UA})
    return portals.immoscout_detail(data)


async def _kleinanzeigen_detail(url):
    return portals.kleinanzeigen_detail(await _browser_page('kleinanzeigen-wohnung', url))


async def _bauverein_detail(url):
    endpoint, payload = portals.polyestate('object-details', {'id': portals.bauverein_identifier(url)})
    return portals.bauverein_detail(await http.get_json('bauverein', endpoint, portals.BAUVEREIN_HEADERS, payload))


# Portals whose detail page is plain HTML: its parser.
HTML_DETAILS = {
    'wg-gesucht': portals.wg_detail,
    'ohne-makler': portals.om_detail,
    'immobilien-de': portals.id_detail,
    'nhw': portals.nhw_detail,
}
DETAIL_PAGES = {
    'immoscout24': _immoscout_detail,
    'kleinanzeigen-wohnung': _kleinanzeigen_detail,
    'immowelt': _immowelt_detail,
    'bauverein': _bauverein_detail,
}


DETAILS = (*HTML_DETAILS, *DETAIL_PAGES)  # the portals with a detail page to read


async def detail(source, url):
    """What the detail page of a listing adds (camelCase keys); one booked visit."""
    if source in HTML_DETAILS:
        return HTML_DETAILS[source](await http.get_html(source, url))
    return await DETAIL_PAGES[source](url)
