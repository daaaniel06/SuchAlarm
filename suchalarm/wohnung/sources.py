"""Fetching flat portals: one shared, budgeted request per portal query, place lookups, detail pages.

ImmoScout24 (app API), bauverein (JSON), NHW and WG-Gesucht (HTML) are fetched directly; Kleinanzeigen
and Immowelt through the remote browser. Every request goes through the page budget of its site.
"""

import json
import logging
import re
from urllib.parse import urlencode

from ..core import browser, cadence, http, images
from . import portals
from .domain import Listing, beyond_radius, distance_km, is_wanted_ad, outside_home, place_query, profile

log = logging.getLogger('suchalarm')

OSM_UA = 'Suchalarm/1.0 (private single-user apartment search)'
POSTCODE_MIN_RADIUS = 2  # km around a postcode's centre on ImmoScout24 and NHW
BROWSER_HOSTS = {
    'kleinanzeigen-wohnung': {'www.kleinanzeigen.de', 'kleinanzeigen.de'},
    'immowelt': {'www.immowelt.de', 'immowelt.de'},
}
CARDS = {
    'kleinanzeigen-wohnung': 'article[data-adid]',
    'immowelt': '[data-testid="serp-core-classified-card-testid"]',
}
DETAILS = ('immoscout24', 'kleinanzeigen-wohnung', 'wg-gesucht', 'immowelt', 'bauverein', 'nhw')


def _extract_cards(selector):
    """A function expression for page.evaluate: only the cards, so the page cache stays small."""
    return f"""() => {{
  const cards = [...document.querySelectorAll({json.dumps(selector)})];
  return {{ title: document.title, text: document.body ? document.body.innerText.slice(0, 20000) : '',
           rows: cards.map(() => ({{}})), html: cards.map(card => card.outerHTML).join('\\n') }};
}}"""


EXTRACT_PAGE = """() => ({ title: document.title, text: document.body ? document.body.innerText.slice(0, 20000) : '',
  rows: [], html: document.documentElement.outerHTML })"""


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


def centre(search):
    """The searched place's centre if it was looked up already, else None (never a request)."""
    known = cadence.location('osm', _key(place_query(profile(search))))
    return tuple(json.loads(known)[:2]) if known else None


def needs_centre(search):
    p = profile(search)
    return bool(p['radius']) or (p['place'] or {}).get('kind') == 'plz'


def needs_place(search):
    """Whether the search's place has to be looked up: for its radius or postcode, and for the regional portals."""
    return needs_centre(search) or bool({'bauverein', 'nhw'} & set(search['sources']))


def out_of_area(search, source):
    """Why a regional portal has nothing for this search, or None to fetch it. Never a request: an unknown
    place counts as inside, so the portal is asked as before."""
    p = profile(search)
    place = p['place'] or {}
    if source == 'wg-gesucht':
        if place.get('kind') == 'plz':
            return 'Sucht nur nach Ortsnamen, nicht nach Postleitzahl'
        try:
            portals.wg_town(place.get('label') or place_query(p))
        except RuntimeError:
            return 'Nur Darmstadt und Umgebung'
    known = cadence.location('osm', _key(place_query(p)))
    if known and source == 'bauverein':
        lat, lon, _ = json.loads(known)
        if distance_km((lat, lon), portals.DARMSTADT) > portals.BAUVEREIN_REACH + p['radius']:
            return 'Nur Darmstadt und Umgebung'
    if known and source == 'nhw':
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


async def _location(source, query, resolve):
    known = cadence.location(source, _key(query))
    if known:
        return known
    value = await resolve()
    cadence.save_location(source, _key(query), value)
    return value


# --- Result lists -----------------------------------------------------------------------


async def _immoscout(search, p, page=1):
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

    looked_up = not place.get('geopath') and cadence.location('immoscout24', _key(query)) is None
    geopath = place.get('geopath') or await _location('immoscout24', query, region)
    coords = (await locate(query))[:2] if p['radius'] else None
    url = portals.immoscout_url(p, geopath, coords, page)
    return await http.get_json('immoscout24', url, headers, portals.IMMOSCOUT_BODY, follow=looked_up)


def _readable_cards(source):
    def readable(data, follow):
        return (
            bool(data['rows'])
            or browser.EMPTY.search(data['text'])
            or (source == 'kleinanzeigen-wohnung' and 'Leider keine' in data['text'])
        )

    return readable


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
            return portals.kleinanzeigen_location(found, query)

        # The place lookup is a visit of its own in the site's budget; the list then waits for the next slot.
        looked_up = cadence.location('kleinanzeigen', _key(query)) is None
        if looked_up:
            cadence.book(source, 'page')
        identifier = await _location('kleinanzeigen', query, location)
        data = await browser.load_page(
            page,
            source,
            portals.kleinanzeigen_url(p, identifier, number),  # the result page's number
            looked_up,
            extract=_extract_cards(CARDS[source]),
            readable=_readable_cards(source),
        )
    return portals.kleinanzeigen_parse(data['html']) if data['rows'] else []  # an empty page passed on its text


async def _immowelt(p):
    """The place's page as Immowelt shows it (order "Relevanz").

    Sorting by date is not possible: Immowelt answers the sorted search, also when chosen in its own menu,
    with a DataDome captcha, and no captcha is worked around. The place's page is found once through its
    old address, which redirects to it; that visit then waits for the next slot like any second page."""
    source, query = 'immowelt', place_query(p)
    async with browser.browser_page(BROWSER_HOSTS[source]) as page:

        async def location():
            response = await page.goto(portals.immowelt_place_url(query), timeout=browser.PAGE_TIMEOUT_MS)
            browser.check_response(response)
            return portals.immowelt_location(page.url, query)

        looked_up = cadence.location(source, _key(query)) is None
        if looked_up:
            cadence.book(source, 'page')
        path = await _location(source, query, location)
        data = await browser.load_page(
            page,
            source,
            portals.IMMOWELT + path,
            looked_up,
            extract=_extract_cards(CARDS[source]),
            readable=_readable_cards(source),
        )
    return portals.immowelt_parse(data['html']) if data['rows'] else []


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


PAGED = ('immoscout24', 'kleinanzeigen-wohnung', 'wg-gesucht')  # the others list everything on one page


async def cards(source, search, page=1):
    """The cards of one portal query, newest first; PAGE counts only on the PAGED portals."""
    p = profile(search)
    if source == 'immoscout24':
        return portals.immoscout_parse(await _immoscout(search, p, page))
    if source == 'kleinanzeigen-wohnung':
        return await _kleinanzeigen(p, page)
    if source == 'immowelt':
        return await _immowelt(p)
    if source == 'wg-gesucht':
        place = p['place'] or {}
        if place.get('kind') == 'plz':
            raise RuntimeError('WG-Gesucht sucht nur nach Ortsnamen, nicht nach Postleitzahl.')
        return portals.wg_parse(await http.get_html(source, portals.wg_url(place.get('label') or place_query(p), page)))
    if source == 'bauverein':
        return await _bauverein()
    if source == 'nhw':
        return portals.nhw_parse(await http.get_html(source, portals.NHW_LIST))
    raise LookupError(source)


@cadence.shared_search(cache_version=1)
async def fetch(source, search):
    """(listings, None) of one portal query; the decorator shares, caches and times it, select() filters
    per search."""
    listings = [Listing.from_card(source, card) for card in await cards(source, search)]
    images.remember(source, {listing.url: listing.image for listing in listings if listing.image})
    return listings, None


async def more(source, search, page):
    """A further result page of one search after a burst of new flats: not shared or cached, only booked."""
    listings = [Listing.from_card(source, card) for card in await cards(source, search, page)]
    images.remember(source, {listing.url: listing.image for listing in listings if listing.image})
    return select(listings, search)


def select(listings, search):
    """The listings of a shared result that belong to this search: in its place, and offers only.

    Everything else (rent, size, traits, words, move-in) is judged per listing by domain.match, also after
    the detail page; a listing ruled out by an estimated rent may still turn out to fit.
    """
    p, middle = profile(search), centre(search)
    kept = []
    for listing in listings:
        if is_wanted_ad(listing.title) or beyond_radius(listing, p, middle) is not None:
            continue
        if listing.source in ('bauverein', 'nhw'):
            radius = p['radius']
            if listing.source == 'nhw' and (p['place'] or {}).get('kind') == 'plz':
                radius = max(radius, POSTCODE_MIN_RADIUS)  # its cards name the town, not the postcode
            spot = (listing.lat, listing.lon) if listing.lat is not None else None
            if not portals.near(place_query(p), radius, middle, listing.place, spot):
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


async def detail(source, url):
    """What the detail page of a listing adds (Wohnungsbot's camelCase keys); one booked visit."""
    if source == 'immoscout24':
        data = await http.get_json(source, portals.immoscout_detail_url(url), {'User-Agent': portals.IMMOSCOUT_UA})
        return portals.immoscout_detail(data)
    if source == 'kleinanzeigen-wohnung':
        return portals.kleinanzeigen_detail(await _browser_page(source, url))
    if source == 'immowelt':
        return await _immowelt_detail(url)
    if source == 'wg-gesucht':
        return portals.wg_detail(await http.get_html(source, url))
    if source == 'bauverein':
        endpoint, payload = portals.polyestate('object-details', {'id': portals.bauverein_identifier(url)})
        return portals.bauverein_detail(await http.get_json(source, endpoint, portals.BAUVEREIN_HEADERS, payload))
    if source == 'nhw':
        return portals.nhw_detail(await http.get_html(source, url))
    raise LookupError(source)
