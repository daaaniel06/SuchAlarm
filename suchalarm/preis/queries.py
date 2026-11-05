"""Provider sections, vehicle filters and shared-result cache keys."""

import re
from urllib.parse import urlparse, urlunparse

from .categories import BIKE_TYPES, CATEGORIES, EBAY_EBIKES, HOSTS, KLEINANZEIGEN_BIKES, PICKUP_SOURCES
from .prices import portal_price_bounds
from .titles import all_terms, normalized, phrases, platform_ids, terms


def bike_type(search):
    """The kind of bike a search is limited to, or None."""
    chosen = (search.get('filters') or {}).get('bike_type')
    return chosen if chosen in BIKE_TYPES else None


def wants_ebike(search):
    """A bike search for E-Bikes: chosen as its type or named in its words ("E-Bike", "Pedelec")."""
    return bike_type(search) == 'ebike' or 'ebike' in normalized(all_terms(search)).split()


def section(source, search):
    """The provider's own section for the search's category, or None."""
    category = search.get('category') or 'sonstiges'
    if source == 'ebay' and category == 'fahrraeder' and wants_ebike(search):
        return EBAY_EBIKES
    return CATEGORIES.get(category, CATEGORIES['sonstiges'])['portal'].get(source)


def kleinanzeigen_attributes(search):
    """Kleinanzeigen's own filters after the section: car limits, fuel and gearbox, the kind of bike."""
    filters, parts = search.get('filters') or {}, []
    category = search.get('category')
    if category == 'autos':
        if filters.get('year_from') or filters.get('km_to'):
            parts.append(f'autos.ez_i:{filters.get("year_from") or ""}%2C')
            parts.append(f'autos.km_i:%2C{filters.get("km_to") or ""}')
        if filters.get('fuel'):
            parts.append(f'autos.fuel_s:{filters["fuel"]}')
        if filters.get('gearbox'):
            parts.append(f'autos.shift_s:{filters["gearbox"]}')
    if category == 'fahrraeder' and bike_type(search):
        parts.append(KLEINANZEIGEN_BIKES[bike_type(search)])
    return ''.join('+' + part for part in parts)


def around(source, search):
    """(postcode, km) for a search with pickup at a provider that supports it, else None.

    "beides" (pickup or shipping) also searches this area; the nationwide part follows separately."""
    pickup = search.get('delivery') in ('abholung', 'beides')
    if pickup and source in PICKUP_SOURCES and search.get('plz') and search.get('radius'):
        return search['plz'], search['radius']
    return None


def vehicle(text):
    """Mileage and first registration year from a car card ("104.000 km", "EZ 06/2012"); None if absent."""
    text = text.replace('\xa0', ' ')
    # Only the card's own tags ("104.000 km" right before "EZ"), never a number from the description.
    km = re.findall(r'(?<![\d.])(\d{1,3}(?:\.\d{3})+|\d{1,7})\s*km\s*(?=EZ\b)', text)
    year = re.findall(r'EZ\s*(?:\d{1,2}/)?((?:19|20)\d{2})\b', text)
    return (int(km[-1].replace('.', '')) if km else None), (int(year[-1]) if year else None)


def vehicle_fits(text, search):
    """Unknown values pass: the provider already filtered; only stated values outside the range drop out."""
    filters = search.get('filters') or {}
    km, year = vehicle(text)
    if filters.get('km_to') and km is not None and km > filters['km_to']:
        return False
    return not (filters.get('year_from') and year is not None and year < filters['year_from'])


def canonical(source, url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS[source] or parsed.username or parsed.password:
        return None
    patterns = {
        'ebay': r'/itm/(?:[^/]+/)?(\d+)',
        'kleinanzeigen': r'/s-anzeige/.+/(\d+)-\d+-\d+',
        'vinted': r'/items/(\d+)',
        'idealo': r'/preisvergleich/OffersOfProduct/(\d+)',
    }
    match = re.search(patterns[source], parsed.path)
    if not match:
        return None
    if source == 'ebay':
        return f'https://www.ebay.de/itm/{match.group(1)}'
    return urlunparse(('https', parsed.netloc, parsed.path, '', '', ''))


def _query_words(source, search):
    query = ' '.join(terms(search).casefold().split())
    if others := phrases(source, search)[1:]:
        query += '\nalso:' + '|'.join(' '.join(phrase.casefold().split()) for phrase in others)
    return query


def _query_area(search, category, place):
    parts: list[str] = []
    if category:
        parts.append(f'section:{category}')
    if place:
        parts.append('around:{}:{}'.format(*place))
    if search.get('category') == 'autos' and not category:
        parts.append('cars')  # titles may leave out the make, so the shared result differs
    return parts


def _query_filters(source, search, category):
    filters = search.get('filters') or {}
    parts: list[str] = []
    if source != 'kleinanzeigen' or not category:
        return parts
    if category == '216' and (filters.get('year_from') or filters.get('km_to')):
        parts.append(f'vehicle:{filters.get("year_from") or ""}:{filters.get("km_to") or ""}')
    # eBay's E-Bike section is already in "section".
    parts.extend(f'{name}:{filters[name]}' for name in ('fuel', 'gearbox', 'bike_type') if filters.get(name))
    return parts


def _query_options(source, search, place):
    parts: list[str] = []
    # VB offers are dropped while parsing at every provider.
    if search.get('negotiable'):
        parts.append('negotiable')
    if search.get('free') and source == 'kleinanzeigen':
        parts.append('free')
    if search.get('delivery') == 'beides' and place:
        parts.append('and-nationwide-shipping')
    if platform_ids(source, search):
        parts.append('platform-filter')
    return parts


def _query_prices(source, search):
    minimum, maximum = portal_price_bounds(source, search)
    return [f'price:{minimum}:{maximum}'] if minimum or maximum is not None else []


def query_text(source, search):
    """The normalized provider query; order and spelling preserve existing shared-result cache keys."""
    category, place = section(source, search), around(source, search)
    parts = [
        _query_words(source, search),
        *_query_area(search, category, place),
        *_query_filters(source, search, category),
        *_query_options(source, search, place),
    ]
    if source == 'ebay':
        parts.append('item-location:DE')
    if source == 'vinted':
        parts.append('order:relevance')
    return '\n'.join([*parts, *_query_prices(source, search)])
