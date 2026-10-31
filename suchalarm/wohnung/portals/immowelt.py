"""Immowelt, read in the browser: the result list as JSON, exposés as HTML."""

import logging
import math
import re
from urllib.parse import quote, urlencode, urlsplit

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
from .common import plain

log = logging.getLogger('suchalarm')

# The place's page is found once through its old address /suche/<place>/wohnungen/mieten, which redirects
# to it; its last path part is Immowelt's place ID. The list itself comes from the search Immowelt's own
# result page uses (sources._immowelt): newest first and already cut to the search's limits.

IMMOWELT = 'https://www.immowelt.de'
IMMOWELT_PAGE_SIZE = 100  # offers per search request; the site's own page asks for 30
IMMOWELT_BATCH = 30  # offers per request for their contents: the IDs go into the path, which 403s past about 1 kB


def immowelt_place_url(query):
    return f'{IMMOWELT}/suche/{quote(query.strip().casefold())}/wohnungen/mieten'


def immowelt_location(final_url, city):
    """The path of the place's page that the old address redirected to."""
    path = urlsplit(final_url).path
    if not re.fullmatch(r'/suche/mieten/wohnung/[\w-]+/[\w-]+/ad\w+', path, re.I):
        raise RuntimeError(f'Immowelt kennt den Ort „{city}“ nicht.')
    return path


def immowelt_search(path, profile, page=1):
    """(result page, search request) for the place's PATH and the result PAGE. The page shows the same
    search on the site, so the request is sent from there. priceMax limits the cold rent, so the warm limit is a safe bound;
    the limits are widened to whole numbers, the exact check stays local."""
    place = path.rsplit('/', 1)[-1].upper()  # ".../darmstadt-64283/ad08de2508" -> "AD08DE2508"
    limits = {
        'priceMax': math.ceil(profile['maxRent']),
        'spaceMin': math.floor(profile['minArea']),
        'numberOfRoomsMin': math.floor(profile['minRooms']),
    }
    limits = {name: value for name, value in limits.items() if value}
    limits.update(_excluded(profile.get('exclusions', [])))
    paging = {'page': page} if page > 1 else {}
    address = f'{IMMOWELT}/classified-search?' + urlencode(
        {
            'distributionTypes': 'Rent',
            'estateTypes': 'Apartment',
            'locations': place,
            **{name: ','.join(value) if isinstance(value, list) else value for name, value in limits.items()},
            **paging,
        }
    )
    request = {
        'criteria': {
            'distributionTypes': ['Rent'],
            'estateTypes': ['Apartment'],
            'location': {'placeIds': [place]},
            **limits,
        },
        'paging': {'page': page, 'size': IMMOWELT_PAGE_SIZE, 'order': 'DateDesc'},
    }
    return address, request


# The kinds of offer Immowelt knows (projectTypes); a search naming some leaves out the others.
PROJECT_TYPES = (
    'Flatsharing',
    'Investment',
    'Life_Annuity',
    'New_Build',
    'Projected',
    'Resale',
    'Short_Time_Rental',
    'Stock',
    'Swap_Apartment',
)
EXCLUDED_TYPES = {'Befristet': 'Short_Time_Rental', 'Tauschwohnung': 'Swap_Apartment', 'WG-Zimmer': 'Flatsharing'}


def _excluded(exclusions):
    """The search criteria that leave out the excluded traits (checked live on 31.10.2026)."""
    criteria: dict[str, str | list[str]] = {}
    if 'WBS' in exclusions:
        criteria['certificateOfEligibilityNeeded'] = 'No'
    unwanted = {EXCLUDED_TYPES[trait] for trait in exclusions if trait in EXCLUDED_TYPES}
    if unwanted:
        criteria['projectTypes'] = [kind for kind in PROJECT_TYPES if kind not in unwanted]
    return criteria


def immowelt_cards(offers):
    """Cards from Immowelt's offer contents. Only the labelled prices are read: rawData.price is the cold
    rent of one offer and the warm rent of the next."""
    if not isinstance(offers, list):
        raise Unreadable('Immowelt-Angebote nicht lesbar (Antwort geändert).')
    return [_card(offer) for offer in offers if _EXPOSE.fullmatch(offer.get('url') or '')]


_EXPOSE = re.compile(re.escape(IMMOWELT) + r'/expose/[\w-]+')


def _card(offer):
    description = offer.get('mainDescription') or {}
    facts = offer.get('hardFacts') or {}
    raw = offer.get('rawData') or {}
    address = (offer.get('location') or {}).get('address') or {}
    prices = {price.get('label'): number(price.get('value')) for price in facts.get('prices') or []}
    free = next((fact.get('value') for fact in facts.get('facts') or [] if fact.get('type') == 'availability'), None)
    text = f'{description.get("headline") or ""}\n{description.get("description") or ""}'
    traits = detect_traits(text)
    if raw.get('propertySubType') == 'FLATSHARING_ROOM' and 'WG-Zimmer' not in traits:
        traits.append('WG-Zimmer')
    city, district = address.get('city') or '', address.get('district') or ''
    place = [address.get('street'), f'{address.get("zipCode") or ""} {city}'.strip()]
    place += [district] if district != city else []
    picture = next(
        (
            image['url']
            for image in (offer.get('gallery') or {}).get('images') or []
            if image.get('url') and (image.get('classification') or {}).get('name') != 'LOGO'
        ),
        None,
    )
    return {
        'url': offer['url'],
        'title': line(description.get('headline') or facts.get('title') or 'Wohnung zur Miete'),
        'place': line(', '.join(part for part in place if part)),
        'rent': money(prices.get('Warmmiete')),
        'coldRent': money(prices.get('Kaltmiete')),
        'area': area((raw.get('surface') or {}).get('main')),
        'rooms': rooms(raw.get('nbroom')),
        'traits': traits,
        'availableFrom': available_from(free),
        'text': clip(text),
        'imageSource': picture + '?w=480&h=360' if picture else None,
    }


# The parts of an exposé that sources reads in the browser, joined by line breaks in page order.
IMMOWELT_SECTIONS = (
    'cdp-hardfacts-keyfacts',
    'cdp-price',
    'cdp-main-description-expandable-text',
    'cdp-additional-description-expandable-text',
    'cdp-features',
)


def immowelt_detail(html):
    """An exposé's sections (IMMOWELT_SECTIONS). Only a stated warm rent counts: "geschätzte Warmmiete"
    is Immowelt's own guess. Without one, cold rent plus running and heating costs is the warm rent once
    the heating costs are known (an amount, or included in the others)."""
    html = re.sub(r'<svg.*?</svg>', ' ', html, flags=re.S)
    sections = {
        found.group(1): part
        for part in re.split(r'\n(?=<\w+\b[^>]*\bdata-testid="cdp-)', html)
        if (found := re.match(r'<\w+\b[^>]*\bdata-testid="([\w-]+)"', part))
    }
    if 'cdp-price' not in sections:
        raise Unreadable('Immowelt-Angebot nicht lesbar (gelöscht oder Layout geändert).')
    # "Warmmiete 770 € Kaltmiete 9,82 €/m² 540 € Nebenkosten 230 € Heizkosten in Nebenkosten enthalten"
    costs = re.split(r'Zusätzliche Kosten|Kaution', plain(sections['cdp-price']))[0]

    def cost(label):
        found = re.search(rf'(?<!geschätzte ){label} (?:[\d.,]+ €/m² )?([\d.]+(?:,\d+)?) €', costs)
        return number(found.group(1)) if found else None

    warm, cold, running = cost('Warmmiete'), cost('Kaltmiete'), cost('Nebenkosten')
    heating = 0 if re.search(r'Heizkosten in (?:Nebenkosten|Warmmiete) enthalten', costs) else cost('Heizkosten')
    if warm is None and None not in (cold, running, heating):
        warm = cold + running + heating
    facts = plain(sections.get('cdp-hardfacts-keyfacts', ''))  # "2 Zimmer • 55 m² • Geschoss 1/2 • frei ab 01.12.2026"
    size = re.search(r'([\d.,]+) m²', facts)
    count = re.search(r'([\d.,]+) Zimmer', facts)
    free = re.search(r'frei ab ([^•]+)', facts)
    text = '\n'.join(plain(sections.get(name, '')) for name in IMMOWELT_SECTIONS[2:])
    return {
        'rent': money(warm),
        'coldRent': money(cold),
        'area': area(number(size.group(1)) if size else None),
        'rooms': rooms(number(count.group(1)) if count else None),
        'traits': detect_traits(text),
        'availableFrom': available_from(free.group(1)) if free else None,
        'text': clip(text),
    }
