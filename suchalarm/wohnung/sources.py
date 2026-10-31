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
    Listing,
    beyond_radius,
    distance_km,
    home_area,
    is_wanted_ad,
    outside_home,
    place_query,
    profile,
)

log = logging.getLogger('suchalarm')

OSM_UA = 'Suchalarm/1.0 (private single-user apartment search)'
POSTCODE_MIN_RADIUS = 2  # km around a postcode's centre on ImmoScout24 and NHW
BROWSER_HOSTS = {
    'kleinanzeigen-wohnung': {'www.kleinanzeigen.de', 'kleinanzeigen.de'},
    'immowelt': {'www.immowelt.de', 'immowelt.de'},
}
KLEINANZEIGEN_CARDS = 'article[data-adid]'
DETAILS = (
    'immoscout24',
    'kleinanzeigen-wohnung',
    'wg-gesucht',
    'immowelt',
    'ohne-makler',
    'immobilien-de',
    'bauverein',
    'nhw',
)


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
        if cadence.location('wg-gesucht', _key(_wg_place(p))) == UNKNOWN_PLACE:
            return 'Kennt diesen Ort nicht'
    if source in ('ohne-makler', 'immobilien-de') and cadence.location(source, _key(place_query(p))) == UNKNOWN_PLACE:
        return 'Kennt diesen Ort nicht'
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


UNKNOWN_PLACE = ''  # kept for a town a portal's place search does not know, so it is asked only once


def _wg_place(p):
    return (p['place'] or {}).get('label') or place_query(p)


async def _wg_city(place):
    """What WG-Gesucht's place field suggests for the place, as stored: '[slug, ID]' or UNKNOWN_PLACE."""
    try:
        answer = await http.get_json('wg-gesucht', portals.wg_cities_url(place))
    except http.NotFound:
        return UNKNOWN_PLACE
    city = portals.wg_city(answer, place)
    return json.dumps(city) if city else UNKNOWN_PLACE


async def _location(source, query, resolve):
    known = cadence.location(source, _key(query))
    if known is not None:
        return known
    value = await resolve()
    cadence.save_location(source, _key(query), value)
    return value


# --- Result lists -----------------------------------------------------------------------


async def _immoscout(p, page=1):
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
            extract=_extract_cards(KLEINANZEIGEN_CARDS),
            readable=_kleinanzeigen_readable,
        )
    return portals.kleinanzeigen_parse(data['html']) if data['rows'] else []  # an empty page passed on its text


def _immowelt_list(request):
    """A function expression for page.evaluate: the offers of REQUEST (portals.immowelt_search), asked for
    from inside Immowelt's own result page, then their contents in one further request. A refusal reads
    as a blocked page; any other failed answer leaves 'total' empty, so the page counts as unreadable."""
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
  if (ids.length) {{
    const offers = await read(await fetch('/classifiedList/' + ids.join(','), {{ headers: {{ 'x-language': 'de' }} }}));
    if (!offers) return page;
    page.rows = offers;
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
            browser.check_response(response)
            return portals.immowelt_location(page.url, query)

        looked_up = cadence.location(source, _key(query)) is None
        if looked_up:
            cadence.book(source, 'page')
        address, request = portals.immowelt_search(await _location(source, query, location), p, number)
        data = await browser.load_page(
            page,
            source,
            address,
            looked_up,
            extract=_immowelt_list(request),
            readable=lambda data, follow: data['total'] is not None,
        )
    return portals.immowelt_cards(data['rows'])


async def _wg_gesucht(p, page=1):
    """The town's list; its ID is found once through the site's place field, which takes a visit of
    its own in the budget, so the list then follows as a further request of the same fetch."""
    source, place = 'wg-gesucht', _wg_place(p)
    if (p['place'] or {}).get('kind') == 'plz':
        raise RuntimeError('WG-Gesucht sucht nur nach Ortsnamen, nicht nach Postleitzahl.')
    looked_up = cadence.location(source, _key(place)) is None
    city = await _location(source, place, lambda: _wg_city(place))
    if city == UNKNOWN_PLACE:
        raise RuntimeError(f'WG-Gesucht kennt den Ort „{place}“ nicht.')
    html = await http.get_html(source, portals.wg_url(json.loads(city), p, page), follow=looked_up)
    return portals.wg_parse(html)


async def _listed_place(source, p, resolve):
    """(place, looked up now) of a portal whose place is found once through its own place search."""
    query = place_query(p)
    looked_up = cadence.location(source, _key(query)) is None
    place = await _location(source, query, resolve)
    if place == UNKNOWN_PLACE:
        raise RuntimeError(f'{SOURCES[source]} kennt den Ort „{query}“ nicht.')
    return place, looked_up


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
        place, looked_up = await _listed_place(source, p, lambda: _om_path(p))
    html = await _result_page(source, portals.om_url(place, p, page), page, looked_up)
    return portals.om_parse(html) if html else []


async def _om_path(p):
    town = home_area(p)
    name = portals.om_town(await http.get_json('ohne-makler', portals.om_places_url(town)), town)
    state = (await locate(place_query(p), follow=True))[2] if name else ''
    return portals.om_path(state, name) if state else UNKNOWN_PLACE


async def _immobilien_de(p, page=1):
    """The town's newest offers (a postcode stands for its town: the site has no radius). The town's
    token is found once through the site's place search, then the list follows as a further request."""
    source = 'immobilien-de'
    slug, looked_up = await _listed_place(source, p, lambda: _id_slug(p))
    html = await _result_page(source, portals.id_url(slug, p, page), page, looked_up)
    return portals.id_parse(html) if html else []


async def _id_slug(p):
    town = home_area(p)
    if (p['place'] or {}).get('kind') == 'plz':
        town = portals.id_town(await http.get_json('immobilien-de', portals.id_places_url(town)))
        if not town:
            return UNKNOWN_PLACE
    answer = await http.get_json('immobilien-de', portals.id_places_url(town), follow=True)
    return portals.id_city(answer, town) or UNKNOWN_PLACE


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


async def cards(source, search, page=1):
    """The cards of one portal query, newest first; PAGE counts only on the PAGED portals."""
    p = profile(search)
    if source == 'immoscout24':
        return portals.immoscout_parse(await _immoscout(p, page))
    if source == 'kleinanzeigen-wohnung':
        return await _kleinanzeigen(p, page)
    if source == 'immowelt':
        return await _immowelt(p, page)
    if source == 'wg-gesucht':
        return await _wg_gesucht(p, page)
    if source == 'ohne-makler':
        return await _ohne_makler(p, page)
    if source == 'immobilien-de':
        return await _immobilien_de(p, page)
    if source == 'bauverein':
        return await _bauverein()
    if source == 'nhw':
        return portals.nhw_parse(await http.get_html(source, portals.NHW_LIST))
    raise LookupError(source)


@cadence.shared_search(cache_version=2)
async def fetch(source, search):
    """(listings, how many the page held) of one portal query; the decorator shares, caches and times it,
    select() filters per search. The count tells pages.py whether the portal's page was full."""
    listings = await _page(source, search)
    return listings, len(listings)


async def more(source, search, page):
    """(listings of this search, how many the page held) of a further result page, while a search takes
    in its stock or after a burst of new flats: not shared or cached, only booked."""
    listings = await _page(source, search, page)
    return select(listings, search), len(listings)


async def _page(source, search, page=1):
    listings = [Listing.from_card(source, card) for card in await cards(source, search, page)]
    images.remember(source, {listing.url: listing.image for listing in listings if listing.image})
    return listings


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
    """What the detail page of a listing adds (camelCase keys); one booked visit."""
    if source == 'immoscout24':
        data = await http.get_json(source, portals.immoscout_detail_url(url), {'User-Agent': portals.IMMOSCOUT_UA})
        return portals.immoscout_detail(data)
    if source == 'kleinanzeigen-wohnung':
        return portals.kleinanzeigen_detail(await _browser_page(source, url))
    if source == 'immowelt':
        return await _immowelt_detail(url)
    if source == 'wg-gesucht':
        return portals.wg_detail(await http.get_html(source, url))
    if source == 'ohne-makler':
        return portals.om_detail(await http.get_html(source, url))
    if source == 'immobilien-de':
        return portals.id_detail(await http.get_html(source, url))
    if source == 'bauverein':
        endpoint, payload = portals.polyestate('object-details', {'id': portals.bauverein_identifier(url)})
        return portals.bauverein_detail(await http.get_json(source, endpoint, portals.BAUVEREIN_HEADERS, payload))
    if source == 'nhw':
        return portals.nhw_detail(await http.get_html(source, url))
    raise LookupError(source)
