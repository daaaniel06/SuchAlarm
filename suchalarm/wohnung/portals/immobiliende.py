"""immobilien.de (HTML with schema.org data; many offers are copies from ohne-makler.net)."""

import json
import math
import re
from urllib.parse import urlencode

from ...core.browser import Unreadable
from ..domain import area, available_from, clip, detect_traits, line, money, number, rooms
from .common import plain

IMMOBILIEN_DE = 'https://www.immobilien.de'

# A town's list is /mieten/wohnung/<slugToken>/, the token from the site's own place search: Frankfurt am
# Main is "frankfurt2", "frankfurt" is Frankfurt (Oder). The site has no radius: always the whole town.
# Both the list and an offer's page carry schema.org data; only the costs and the full description are
# read from the page itself.


def id_places_url(text):
    return f'{IMMOBILIEN_DE}/api/search/autocomplete?' + urlencode({'query': text, 'limit': 8})


def id_town(answer):
    """The town of a postcode among the place search's answers, or None."""
    zips = (answer or {}).get('zips') or []
    return zips[0].get('city') if zips else None


def id_city(answer, town):
    """The slug token of TOWN among the place search's cities, or None. The full name ("Frankfurt am
    Main") decides; a bare "Frankfurt" means the town with the most offers."""
    cities = [city for city in (answer or {}).get('cities') or [] if city.get('slugToken')]
    wanted = town.casefold().strip()

    def full_name(city):
        return f'{city.get("name") or ""} {city.get("nameAddition") or ""}'.casefold().strip()

    exact = [city for city in cities if full_name(city) == wanted]
    named = sorted(
        (city for city in cities if (city.get('name') or '').casefold() == wanted),
        key=lambda city: -(city.get('listingCount') or 0),
    )
    found = next(iter(exact + named + cities), None)
    return found['slugToken'] if found else None


def id_url(slug, profile, page=1):
    """The town's newest offers within the search's limits. preis_bis limits the cold rent (the warm
    limit is a safe bound); warmmiete_bis would drop every offer without a stated warm rent."""
    limits = {
        'preis_bis': math.ceil(profile['maxRent']),
        'flaeche_von': math.floor(profile['minArea']),
        'zimmer_von': math.floor(profile['minRooms']),
    }
    query = {'sort': 'newest', **{name: value for name, value in limits.items() if value}}
    if page > 1:
        query['page'] = page
    return f'{IMMOBILIEN_DE}/mieten/wohnung/{slug}/?{urlencode(query)}'


def _graph(html):
    """The schema.org objects of a page."""
    nodes: list[dict] = []
    for script in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        nodes += data.get('@graph', [data]) if isinstance(data, dict) else []
    return nodes


def _typed(node, kind):
    types = node.get('@type')
    return kind in (types if isinstance(types, list) else [types])


def id_parse(html):
    """The offers of a result page from its ItemList; a page without hits has none at all."""
    lists = [node for node in _graph(html) if _typed(node, 'ItemList')]
    if not lists:
        if 'Keine Ergebnisse für Deine Suche' in html:
            return []
        raise Unreadable('immobilien.de-Seite enthält keine erkennbaren Angebote (Layout geändert).')
    items = [entry.get('item') or {} for entry in lists[0].get('itemListElement') or []]
    return [_card(item) for item in items if re.fullmatch(rf'{IMMOBILIEN_DE}/expose/\d+', item.get('url') or '')]


def _place(address):
    town = f'{address.get("postalCode") or ""} {address.get("addressLocality") or ""}'.strip()
    return line(', '.join(part for part in (address.get('streetAddress'), town) if part))


def _price(item):
    return number(str(((item.get('offers') or {}).get('priceSpecification') or {}).get('price') or ''))


def _card(item):
    title = line(item.get('name') or 'Wohnung')
    image = item.get('image')
    return {
        'url': item['url'],
        'title': title,
        'place': _place(item.get('address') or {}),
        'coldRent': money(_price(item)),  # without a warm limit in the address the list names the cold rent
        'area': area((item.get('floorSize') or {}).get('value')),
        'rooms': rooms(item.get('numberOfRooms')),
        'traits': detect_traits(title),
        'text': clip(title),
        'imageSource': (image[0] if isinstance(image, list) else image) or None,
    }


_ROW = re.compile(r'<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>', re.S)


def id_detail(html):
    """An offer's page. A stated warm rent counts; otherwise cold rent plus running and heating costs,
    once the heating costs are known (an amount, or included)."""
    listing = next((node for node in _graph(html) if _typed(node, 'RealEstateListing')), None)
    if listing is None:
        raise Unreadable('immobilien.de-Angebot nicht lesbar (gelöscht oder Layout geändert).')
    rows = {plain(label): plain(value) for label, value in _ROW.findall(html)}
    cold = number(rows.get('Kaltmiete') or rows.get('Nettokaltmiete'))
    warm, running = number(rows.get('Warmmiete')), number(rows.get('Nebenkosten'))
    heating_text = rows.get('Heizkosten') or ''
    heating = 0 if 'enthalten' in heating_text else number(heating_text)
    if warm is None and None not in (cold, running, heating):
        warm = cold + running + heating
    description = re.search(r'id="section-beschreibung".*?data-print-flow="true"[^>]*>(.*?)</div>', html, re.S)
    text = plain(description.group(1)) if description else listing.get('description') or ''
    return {
        'rent': money(warm),
        'coldRent': money(cold),
        'area': area(number(rows.get('Wohnfläche'))),
        'rooms': rooms(number(rows.get('Zimmer'))),
        'place': _place(listing.get('address') or {}) or None,
        'traits': detect_traits(text),
        'availableFrom': available_from(rows.get('Verfügbar ab')),
        'text': clip(text),
    }
