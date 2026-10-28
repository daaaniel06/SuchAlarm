"""The same flat on several portals, shown and mailed once.

Rule one: never lose a flat. Merging two different flats would hide one, a missed duplicate only shows a
flat twice. So listings are linked only on clear evidence:

- Only listings of different portals; at most one per portal in a group.
- Area, rooms and rent known and in agreement (area within 1 m², rooms within half a room, rent within 3 %,
  cold with cold where both have it, never an estimate); move-in dates must not lie more than two weeks apart.
- Plus evidence of the place: the same street and house number or the same exact spot (within 50 m), first
  seen at most a week apart (landlords often put a flat on a second portal days later). There a portal
  naming only the cold rent and one naming only the warm rent agree if the warm rent is plausible for the
  cold one. Or a practically identical photo, first seen at most 12 hours apart (photo_hash, at most PHOTO_BITS of 64 bits apart). A photo counts only if it
  shows one flat: landlords such as Vonovia put the same stock picture on different flats, often identical
  units of one estate. A picture that a portal shows for two listings, or that belongs to listings whose
  facts differ, is such a stock picture and proves nothing; neither does a photo of listings that name
  different postcodes.
- Or, also within 12 hours, the landlord's own title word for word (at least 25 characters) with the exact
  area: NHW names only the town, ImmoScout24 only the district, and their photos are cropped differently.
- The street may follow the town ("Darmstadt, Eichbergstraße 12"); a postcode only one side names does not
  contradict.
- A building where one portal itself lists several matching flats within 12 hours has identical units: none
  of them is linked.
- "Nicht dieselbe Wohnung" in the app keeps two listings apart for good (APART pairs).

Nothing is deleted. groups() names each member's first listing; which one the hit list shows is the worker's.
"""

import re
from datetime import date

from .domain import distance_km

WINDOW = 12 * 3600  # photo evidence, and identical units listed by one portal
ADDRESS_WINDOW = 7 * 86400  # street and house number, or the exact spot
AREA_TOLERANCE = 1.0  # m²
RENT_TOLERANCE = 0.03
ROOMS_TOLERANCE = 0.5  # one portal counts the half room, the other does not
SIDE_COSTS = (1.0, 1.6)  # warm rent / cold rent that is plausible for the same flat
MOVE_IN_DAYS = 14
NEAR_KM = 0.05
PHOTO_BITS = 6  # of 64 differing bits: the same picture, resized or recompressed
HOUSE = re.compile(r'(.+?)\s+(\d+)\s*([a-z])?\b.*')
TITLE_CHARS = 25  # a title this long, word for word on two portals, is the landlord's own ad text


def street_key(place):
    """ "heinrichstr12|64283" for "Heinrichstraße 12, 64283 Darmstadt"; the street may come after the town
    ("Darmstadt, Eichbergstraße 12" at NHW). None without a house number."""
    place = place or ''
    for part in place.split(','):
        part = part.strip().casefold().replace('ß', 'ss')
        match = HOUSE.fullmatch(part)
        if not match or re.match(r'\d{5}\b', part):
            continue  # "64283 Darmstadt" is no street
        street = re.sub(r'strasse\b|str\b\.?', 'str', match.group(1))
        street = re.sub(r'[^a-z0-9äöü]', '', street)
        if not re.search(r'[a-zäöü]', street):
            continue
        postcode = re.search(r'\b\d{5}\b', place)
        return f'{street}{match.group(2)}{match.group(3) or ""}|{postcode.group() if postcode else ""}'
    return None


def _same_street(first, second):
    """The same street and house number; a postcode missing on one side does not contradict."""
    if not first or not second:
        return False
    (house, code), (other, other_code) = first.split('|'), second.split('|')
    return house == other and (not code or not other_code or code == other_code)


def _close(a, b, share):
    return abs(a - b) <= max(a, b) * share


def _rents_agree(a, b, mixed):
    """Cold with cold where both have it, else warm with warm. MIXED: one names only the cold rent, the other
    only the warm rent, and the warm one is plausible for the cold one."""
    if a.get('cold_rent') is not None and b.get('cold_rent') is not None:
        return _close(a['cold_rent'], b['cold_rent'], RENT_TOLERANCE)
    if a.get('rent') is not None and b.get('rent') is not None:
        return _close(a['rent'], b['rent'], RENT_TOLERANCE)
    if not mixed:
        return False  # no comparable rent: not enough to tell
    cold, warm = (a, b) if a.get('cold_rent') is not None else (b, a)
    if cold.get('cold_rent') is None or warm.get('rent') is None:
        return False
    low, high = SIDE_COSTS
    return cold['cold_rent'] * low <= warm['rent'] <= cold['cold_rent'] * high


def same_facts(a, b, mixed=False):
    """Area, rooms and rent known on both sides and in agreement; nothing contradicts. MIXED allows a cold
    rent against a warm one (only with the address as evidence)."""
    if None in (a.get('area'), b.get('area'), a.get('rooms'), b.get('rooms')):
        return False
    if abs(a['area'] - b['area']) > AREA_TOLERANCE or abs(a['rooms'] - b['rooms']) > ROOMS_TOLERANCE:
        return False
    if not _rents_agree(a, b, mixed):
        return False
    if a.get('available_from') and b.get('available_from'):
        gap = abs((date.fromisoformat(a['available_from']) - date.fromisoformat(b['available_from'])).days)
        if gap > MOVE_IN_DAYS:
            return False
    return True


def _spot(listing):
    """Coordinates that belong to the house: only with a street and house number in the place."""
    if listing.get('lat') is None or listing.get('lon') is None or not street_key(listing.get('place')):
        return None
    return listing['lat'], listing['lon']


def same_place(a, b):
    if _same_street(street_key(a.get('place')), street_key(b.get('place'))):
        return True
    spot, other = _spot(a), _spot(b)
    return bool(spot and other) and distance_km(spot, other) <= NEAR_KM


def _photos_close(first, second):
    return bool(first and second) and bin(int(first, 16) ^ int(second, 16)).count('1') <= PHOTO_BITS


def same_photo(a, b):
    return _photos_close(a.get('photo_hash'), b.get('photo_hash'))


def stock_photos(items):
    """Photos that show more than one flat: one portal lists two offers with it, or offers with it differ in
    their facts. Returns their hashes."""
    shown = [x for x in items if x.get('photo_hash')]
    stock = set()
    for i, a in enumerate(shown):
        for b in shown[i + 1 :]:
            if a['url'] != b['url'] and same_photo(a, b) and (a['source'] == b['source'] or not same_facts(a, b)):
                stock |= {a['photo_hash'], b['photo_hash']}
    return stock


def _postcode(listing):
    found = re.search(r'\b\d{5}\b', listing.get('place') or '')
    return found.group() if found else None


def photo_proof(a, b, stock):
    """The same photo, of one flat only, and no other postcode."""
    if not same_photo(a, b) or any(_photos_close(x['photo_hash'], s) for x in (a, b) for s in stock):
        return False
    first, second = _postcode(a), _postcode(b)
    return not (first and second and first != second)


def _title(listing):
    return ' '.join(re.findall(r'\w+', (listing.get('title') or '').casefold()))


def title_proof(a, b):
    """The landlord's own long title word for word, the exact area and no other postcode: one ad on two
    portals (NHW names only the town, ImmoScout24 the district, the photos are cropped differently)."""
    title = _title(a)
    if len(title) < TITLE_CHARS or title != _title(b) or abs(a['area'] - b['area']) > 0.1:
        return False
    first, second = _postcode(a), _postcode(b)
    return not (first and second and first != second)


def _twin(a, b, stock=frozenset(), apart=frozenset()):
    """The same flat: its facts agree, and the address or (within WINDOW) the photo or the title proves it."""
    if frozenset((a['url'], b['url'])) in apart:
        return False  # "Nicht dieselbe Wohnung"
    gap = abs(a['first_seen'] - b['first_seen'])
    if gap <= ADDRESS_WINDOW and same_place(a, b) and same_facts(a, b, mixed=True):
        return True
    return gap <= WINDOW and same_facts(a, b) and (photo_proof(a, b, stock) or title_proof(a, b))


def groups(listings, apart=frozenset()):
    """{url: url of the group's first listing} for listings that are the same flat; singles are left out.

    LISTINGS are dicts with url, source, first_seen and the facts of domain.Listing. APART holds
    frozensets of two URLs the user separated."""
    items = sorted(listings, key=lambda x: (x['first_seen'], x['url']))
    stock = stock_photos(items)
    pairs = _close_pairs(items)
    ambiguous = _ambiguous(pairs, stock)
    members = {x['url']: [x] for x in items}
    for a, b in pairs:
        if a['source'] == b['source'] or a['url'] in ambiguous or b['url'] in ambiguous:
            continue
        if not _twin(a, b, stock, apart):
            continue
        joined = members[a['url']] + [x for x in members[b['url']] if x not in members[a['url']]]
        if members[a['url']] is members[b['url']] or len({x['source'] for x in joined}) < len(joined):
            continue  # already together, or a portal would appear twice
        if any(not _twin(x, y, stock, apart) for x in joined for y in joined if x is not y):
            continue  # every member must match every other, not just a neighbour
        for x in joined:
            members[x['url']] = joined
    result = {}
    for url, group in members.items():
        if len(group) > 1:
            result[url] = min(group, key=lambda x: (x['first_seen'], x['url']))['url']
    return result


def _close_pairs(items):
    """Pairs of different listings found within ADDRESS_WINDOW of each other; ITEMS sorted by first_seen."""
    pairs = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if b['first_seen'] - a['first_seen'] > ADDRESS_WINDOW:
                break  # sorted: every later one is further apart
            if a['url'] != b['url']:
                pairs.append((a, b))
    return pairs


def _ambiguous(pairs, stock):
    """URLs of a portal listing several matching flats in one place at once: identical units, linked to none."""
    return {
        x['url']
        for a, b in pairs
        if a['source'] == b['source'] and b['first_seen'] - a['first_seen'] <= WINDOW and _twin(a, b, stock)
        for x in (a, b)
    }
