"""ImmoScout24 through its mobile app API (the website refuses the browser)."""

import json
import logging
import re
from urllib.parse import urlencode

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    coordinate_pair,
    detect_traits,
    is_wanted_ad,
    line,
    money,
    number,
    published_at,
    rooms,
)

log = logging.getLogger('suchalarm')


IMMOSCOUT_UA = 'ImmoScout_28.1_26.5.2_._'
IMMOSCOUT_LIST = 'https://api.mobile.immobilienscout24.de/search/list?'
IMMOSCOUT_BODY = json.dumps({'supportedResultListTypes': [], 'userData': {}}).encode()


def immoscout_locations_url(query):
    return 'https://www.immobilienscout24.de/geoautocomplete/v3/locations.json?' + urlencode({'i': query})


def immoscout_region(entries, city):
    """Geo path such as '/de/berlin/berlin' for a city name, from the location suggestions."""
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f'ImmoScout24 kennt den Ort „{city}“ nicht.')
    entities = [entry.get('entity') or {} for entry in entries if isinstance(entry, dict)]
    best = next((e for e in entities if e.get('type') == 'city'), None) or entities[0]
    uri = (best.get('geopath') or {}).get('uri')
    if not uri:
        raise RuntimeError(f'ImmoScout24 kennt den Ort „{city}“ nicht.')
    return uri


def immoscout_places(entries):
    """Suggestions for the place field: towns, districts and postcodes."""
    result, seen = [], set()
    for entry in entries if isinstance(entries, list) else []:
        entity = (entry or {}).get('entity') or {}
        label, kind = str(entity.get('label') or ''), entity.get('type')
        geopath = ((entity.get('geopath') or {}).get('uri') or '').split('?')[0]
        if kind in ('city', 'district') and geopath:
            place = {'kind': 'city', 'label': label, 'query': re.sub(r'\s*\(Kreis\)$', '', label), 'geopath': geopath}
        elif kind in ('postcode', 'postcodeWithQuarter') and re.match(r'\d{5}\s', label):
            # "64283 Darmstadt - Bessungen": one entry per postcode.
            code, town = label[:5], label[6:].split(' - ')[0]
            place = {'kind': 'plz', 'label': f'{code} {town}', 'query': code, 'geopath': None}
        else:
            continue  # quarters: Kleinanzeigen has no matching place
        if place['label'] not in seen:
            seen.add(place['label'])
            result.append(place)
    return result[:8]


def immoscout_url(profile, region, coords, page=1):
    params = {
        'realestatetype': 'apartmentrent',
        'pricetype': 'calculatedtotalrent',  # filter on warm rent, results still show cold rent
        'price': f'-{profile["maxRent"]:.1f}',
        'sorting': '-firstactivation',
        'pagenumber': str(page),
    }
    if 'Tauschwohnung' in profile.get('exclusions', []):
        params['exclusioncriteria'] = 'swapflat'
    if profile.get('minArea'):
        params['livingspace'] = f'{profile["minArea"]:.1f}-'
    if profile.get('minRooms'):
        params['numberofrooms'] = f'{profile["minRooms"]:.1f}-'
    if profile.get('radius') and coords:
        params.update(
            searchType='radius', geocoordinates=f'{coords[0]:.5f};{coords[1]:.5f};{float(profile["radius"]):.1f}'
        )
    else:
        params.update(searchType='region', geocodes=region)
    return IMMOSCOUT_LIST + urlencode(params)


def immoscout_parse(data):
    if not isinstance(data, dict) or not isinstance(data.get('resultListItems'), list):
        raise Unreadable('ImmoScout24-Ergebnisse haben ein unbekanntes Format.')
    listings = (_immoscout_listing(entry) for entry in data['resultListItems'] if isinstance(entry, dict))
    return [listing for listing in listings if listing]


def _immoscout_listing(entry):
    """One flat of the result list, or None for ads, wanted ads and entries without an ID."""
    item = entry.get('item') or {}
    identifier = str(item.get('id', ''))
    title = line(item.get('title')) or 'Wohnung'
    if entry.get('type') != 'EXPOSE_RESULT' or not identifier.isdigit() or is_wanted_ad(title):
        return None
    values = [str(attr.get('value') or '') for attr in item.get('attributes') or [] if isinstance(attr, dict)]
    address = item.get('address') or {}

    def fact(found):
        return next((number(v) for v in values if found(v)), None)

    return {
        'url': f'https://www.immobilienscout24.de/expose/{identifier}',
        'title': title,
        'place': line(address.get('line'))[:300],
        'coldRent': money(fact(lambda v: '€' in v)),
        'area': area(fact(lambda v: 'm²' in v)),
        'rooms': rooms(fact(lambda v: re.search(r'\bZi(?:\.|mmer)', v))),
        'traits': detect_traits(title),
        'imageSource': _immoscout_image(item),
        'publishedAt': published_at(item.get('published')),
        **coordinate_pair(address.get('lat'), address.get('lon')),
    }


def _immoscout_image(item):
    """480x360 is enough for the list and the sheet; the template carries format and quality."""
    pictures = [p for p in item.get('pictures') or [] if isinstance(p, dict) and p.get('urlScaleAndCrop')]
    if pictures:
        return pictures[0]['urlScaleAndCrop'].replace('%WIDTH%x%HEIGHT%', '480x360')
    return str((item.get('titlePicture') or {}).get('preview') or '') or None


def _warm(cold, costs, heating):
    """Warm rent from its parts when an offer names no total: only with the service charges and the
    heating known (an amount, or "in Nebenkosten enthalten")."""
    costs, heat = number(costs), number(heating)
    if cold is None or costs is None or (heat is None and 'enthalten' not in (heating or '').casefold()):
        return None
    return cold + costs + (heat or 0)


def immoscout_detail_url(url):
    return 'https://api.mobile.immobilienscout24.de/expose/' + url.rstrip('/').rsplit('/', 1)[-1]


def immoscout_detail(data):
    attributes: dict[str, str] = {}
    texts: list[str] = []
    for section in data.get('sections') or [] if isinstance(data, dict) else []:
        if not isinstance(section, dict):
            continue
        for attr in section.get('attributes') or []:
            if isinstance(attr, dict) and attr.get('label'):
                attributes.setdefault(str(attr['label']).rstrip(':').strip(), str(attr.get('text') or ''))
        if section.get('type') in ('TITLE', 'TEXT_AREA'):
            texts.append(str(section.get('title') or '') + '\n' + str(section.get('text') or ''))

    def label(*names):
        return next((attributes[k] for k in attributes if any(k.startswith(n) for n in names)), None)

    if not attributes:
        raise Unreadable('ImmoScout24-Exposé nicht lesbar (gelöscht oder Schnittstelle geändert).')
    extra = (
        ' '.join(texts)
        + ' '
        + ' '.join(f'{k} {v}' for k, v in attributes.items() if 'WBS' in k or 'Wohnberechtigungsschein' in k)
    )
    cold = number(label('Kaltmiete'))
    return {
        'rent': money(
            number(label('Warmmiete', 'Gesamtmiete')) or _warm(cold, label('Nebenkosten'), label('Heizkosten'))
        ),
        'coldRent': money(cold),
        'area': area(number(label('Wohnfläche'))),
        'rooms': rooms(number(label('Zimmer'))),
        'traits': detect_traits(extra),
        'availableFrom': available_from(label('Bezugsfrei ab')),
        'text': clip(extra),
    }
