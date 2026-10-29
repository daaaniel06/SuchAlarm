"""WG-Gesucht (HTML, flats and one-room flats, no WG rooms)."""

import logging
import math
import re
import unicodedata
from html import unescape
from urllib.parse import quote, urlencode

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    detect_traits,
    line,
    money,
    number,
    rooms,
)
from .common import attribute, plain, rescue

log = logging.getLogger('suchalarm')

# The result list already carries the total rent ("Gesamtmiete" = rent plus service
# charges), size, rooms, district and move-in date, so most offers never need more.

# The site's own place field; a town it does not know is answered with 404.
WG_CITIES = 'https://www.wg-gesucht.de/api/location/cities/names/'
UMLAUTS = str.maketrans({'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue', 'ß': 'ss'})


def wg_town(place):
    """The town's name as typed into WG-Gesucht's place field: "64283 Frankfurt (Oder), Brandenburg"
    -> "Frankfurt"."""
    return re.split(r'[,(]| - ', _without_postcode(place))[0].strip()


def _without_postcode(place):
    return re.sub(r'^\d{5}\s*', '', place.strip())


def wg_cities_url(place):
    return WG_CITIES + quote(wg_town(place), safe='')


def wg_city(answer, place):
    """(slug, ID) of the place among the place field's suggestions, or None if WG-Gesucht has none.
    They match by prefix: a town of exactly the place's name wins, then one of the town's name
    ("Frankfurt (Oder)", then "Frankfurt"), then the first suggestion."""
    cities = ((answer or {}).get('_embedded') or {}).get('cities') or []
    if not cities:
        return None
    names = [_without_postcode(place).split(',')[0].strip().casefold(), wg_town(place).casefold()]
    exact = (city for name in names for city in cities if city['city_name'].casefold() == name)
    city = next(exact, cities[0])
    return wg_slug(city['city_name']), int(city['city_id'])


def wg_slug(name):
    """The town's name as the site writes it in list addresses: "Frankfurt (Oder)" -> "Frankfurt-Oder".
    Only the ID counts there; a different name is redirected."""
    plain = unicodedata.normalize('NFKD', name.translate(UMLAUTS)).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '-', plain).strip('-')


# Categories in the list address: 1 = 1-Zimmer-Wohnung, 2 = Wohnung (0 WG-Zimmer, 3 Haus). Only "1+2" in the
# path gives one list of both; categories[] in the query is ignored (checked live 29.10.2026).
FLATS = '1+2'


def wg_url(city, profile, page=1):
    """The town's list of flats and one-room flats, already cut to the search's rent, size and rooms.
    rMax is the total rent the cards show; the limits are widened to whole numbers, the exact check stays local."""
    slug, identifier = city
    limits = {
        'rMax': math.ceil(profile['maxRent']),
        'sMin': math.floor(profile['minArea']),
        'rmMin': math.floor(profile['minRooms']),
    }
    query = urlencode({name: value for name, value in limits.items() if value})
    address = f'https://www.wg-gesucht.de/wohnungen-in-{slug}.{identifier}.{FLATS}.1.{page - 1}.html'
    return f'{address}?{query}' if query else address


def wg_place(street, district, postcode=''):
    """ "Darmstadt Eberstadt" becomes "…, Darmstadt, Eberstadt": the district is the name
    the app shows, the town and postcode go with the street."""
    town, _, rest = district.partition(' ')
    parts = [street, f'{postcode} {town}'.strip(), rest] if rest else [street, f'{postcode} {district}'.strip()]
    return ', '.join(part for part in parts if part)[:300]


WG_CARD = re.compile(r'<a\b[^>]*\boffer_list_item\b[^>]*>', re.S)


def wg_parse(html):
    """The result list (layout of 10/2026: each offer is an <a class="wgg-list-card offer_list_item">)."""
    tags = list(WG_CARD.finditer(html))
    if not tags:
        if re.search(r'keine\s+(?:passenden\s+)?(?:Angebote|Anzeigen|Ergebnisse)', html, re.I):
            return []
        link = r'^(?:https://www\.wg-gesucht\.de)?/(?:1-zimmer-)?wohnungen-in-[\w-]+\.\d+\.html$'
        if listings := rescue('WG-Gesucht', html, link, 'https://www.wg-gesucht.de', warm=True):
            return listings
        raise Unreadable('WG-Gesucht-Seite enthält keine erkennbaren Anzeigen (Layout geändert oder gesperrt).')
    listings = []
    for index, tag in enumerate(tags):
        card = html[tag.end() : tags[index + 1].start() if index + 1 < len(tags) else len(html)]
        href = re.search(r'\bhref="(/[^"]+\.\d+\.html)"', tag.group(0))
        if not href:
            continue
        title = line(attribute(tag.group(0), 'aria-label')) or 'Wohnung'
        heading = re.search(r'<div[^>]*ad-subtitle[^>]*>', card)
        subtitle = attribute(heading.group(0), 'title') if heading else ''
        kind, district, street = ([' '.join(part.split()) for part in subtitle.split('·')] + ['', '', ''])[:3]
        district = ' '.join(dict.fromkeys(district.split()))  # "Darmstadt Darmstadt" -> "Darmstadt"
        price = re.search(r'class="card-main-text">\s*([^<]*?(?:&euro;|€))', card)
        size = re.search(r'class="size-value">([^<]*)<', card)
        middle = card.split('middle-row', 1)[-1].split('bottom-card', 1)[0] if 'middle-row' in card else ''
        dates = re.findall(r'\d{2}\.\d{2}\.\d{4}', middle)
        count = re.match(r'([\d,.]+)-Zimmer', kind)
        picture = re.search(r'<img[^>]+class="top-image-card[^"]*"[^>]+src="(https://img\.wg-gesucht\.de/[^"]+)"', card)
        listings.append(
            {
                'url': 'https://www.wg-gesucht.de' + unescape(href.group(1)),
                'title': title[:300],
                'place': wg_place(street, district),
                'rent': money(number(unescape(price.group(1)))) if price else None,
                'area': area(number(unescape(size.group(1)))) if size else None,
                'rooms': rooms(number(count.group(1)) if count else 1.0 if kind.startswith('1-Zimmer') else None),
                'traits': detect_traits(title, temporary=len(dates) > 1),  # "01.10.2026 - 31.03.2027"
                'availableFrom': available_from(dates[0]) if dates else None,
                'imageSource': unescape(picture.group(1)) if picture else None,
            }
        )
    return listings


def wg_detail(html):
    # A label pairs with the next value only if no other label comes first (the address has none).
    pairs = re.findall(
        r'class="section_panel_detail"[^>]*>(.*?)</span>((?:(?!section_panel_detail).)*?)class="section_panel_value"[^>]*>(.*?)</span>',
        html,
        re.S,
    )
    values = {plain(label).rstrip(':').strip(): plain(value) for label, _, value in pairs}
    labels = [plain(label) for label in re.findall(r'class="section_panel_detail"[^>]*>(.*?)</span>', html, re.S)]
    if 'Miete' not in values:
        raise RuntimeError('WG-Gesucht-Anzeige nicht lesbar (gelöscht oder Layout geändert).')
    cold = number(values.get('Miete'))
    extra = [number(values.get(key)) for key in ('Nebenkosten', 'Sonstige Kosten')]
    warm = cold + sum(value or 0 for value in extra) if cold is not None and any(v is not None for v in extra) else None
    # The address block runs into the next heading ("Verfügbarkeit"); keep the address.
    address = next((label.split(' Verfügbarkeit')[0] for label in labels if re.search(r'\b\d{5}\b', label)), '')
    free = {
        kind: re.search(rf'frei {kind}:\s*</span>.*?section_panel_value[^>]*>\s*([\d.]+)', html, re.S)
        for kind in ('ab', 'bis')
    }
    title = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    description = re.search(r'id="ad_description_text"[^>]*>(.*?)(?:<div class="panel|<h2)', html, re.S)
    text = (
        (plain(title.group(1)) if title else '')
        + '\n'
        + (
            plain(re.sub(r'<script.*?</script>|<!--.*?-->', ' ', description.group(1), flags=re.S))
            if description
            else ''
        )
    )
    street = re.match(r'(.*?)\s+(\d{5})\s+(.*)$', address)
    return {
        'rent': money(warm),
        'coldRent': money(cold),
        'place': wg_place(street.group(1), ' '.join(dict.fromkeys(street.group(3).split())), street.group(2))
        if street
        else None,
        'traits': detect_traits(text, temporary=bool(free['bis'])),
        'availableFrom': available_from(free['ab'].group(1)) if free['ab'] else None,
        'text': clip(text),
    }
