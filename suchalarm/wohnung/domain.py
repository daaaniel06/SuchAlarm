"""Flat searches: the offer type, the filter rules and the text helpers of the portal parsers.

Pure functions without a database. Money is in euros here, as the portals state it; a search keeps its maximum
warm rent in cents like every search (searches.max_price).
"""

import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

SOURCES = {
    'immoscout24': 'ImmoScout24',
    'kleinanzeigen-wohnung': 'Kleinanzeigen',
    'wg-gesucht': 'WG-Gesucht',
    'immowelt': 'Immowelt',
    'ohne-makler': 'ohne-makler.net',
    'immobilien-de': 'immobilien.de',
    'bauverein': 'bauverein AG',
    'nhw': 'NHW',
}
TRAITS = ('WBS', 'Befristet', 'Tauschwohnung', 'WG-Zimmer')
TEXT_LIMIT = 4000  # title + description kept for the search's own exclusion words


@dataclass(frozen=True)
class Listing:
    source: str
    url: str
    title: str
    place: str = ''
    rent: float | None = None  # warm rent (or total rent as the portal names it), euros
    cold_rent: float | None = None
    area: float | None = None
    rooms: float | None = None
    traits: list[str] = field(default_factory=list)
    available_from: str | None = None  # ISO day
    published_at: str | None = None  # ISO time, ImmoScout24 only
    image: str | None = None  # picture on the portal's image host
    lat: float | None = None
    lon: float | None = None
    text: str = ''  # what the exclusion words are checked against besides the title
    exclusions_known: bool = False  # the description was read (detail page or a portal that lists it)

    @classmethod
    def from_card(cls, source, card):
        """A listing from a parser's card (camelCase keys)."""
        return cls(
            source=source,
            url=card['url'],
            title=card.get('title') or 'Wohnung',
            place=card.get('place') or '',
            rent=card.get('rent'),
            cold_rent=card.get('coldRent'),
            area=card.get('area'),
            rooms=card.get('rooms'),
            traits=sorted(set(card.get('traits') or [])),
            available_from=card.get('availableFrom'),
            published_at=card.get('publishedAt'),
            image=card.get('imageSource'),
            lat=card.get('lat'),
            lon=card.get('lon'),
            text=card.get('text') or '',
        )

    def with_detail(self, detail):
        """The listing completed by its detail page: known values win, traits add up."""
        values = {
            'rent': detail.get('rent'),
            'cold_rent': detail.get('coldRent'),
            'area': detail.get('area'),
            'rooms': detail.get('rooms'),
            'available_from': detail.get('availableFrom'),
            'place': detail.get('place'),
            'text': detail.get('text'),
        }
        return replace(
            self,
            **{key: value for key, value in values.items() if value is not None},
            traits=sorted(set(self.traits) | set(detail.get('traits') or [])),
            exclusions_known=True,
        )


# --- Text helpers of the parsers ------------------------------------------------------


def number(text):
    """German number from text: '1.234,50 €' -> 1234.5, '55,36 m²' -> 55.36."""
    match = re.search(r'\d[\d.,]*', text or '')
    if not match:
        return None
    raw = match.group().rstrip('.,')
    if ',' in raw:
        raw = raw.replace('.', '').replace(',', '.')
    elif raw.count('.') == 1 and len(raw.split('.')[1]) == 3:
        raw = raw.replace('.', '')
    try:
        return float(raw)
    except ValueError:
        return None


def bounded(value, upper):
    return value if value is not None and 0 < value <= upper else None


# Plausible upper limits; anything above is a parsing slip, not an offer.
def money(value):
    return bounded(value, 100000)


def area(value):
    return bounded(value, 10000)


def rooms(value):
    return bounded(value, 100)


def line(text, limit=300):
    """One line of text: a title or address never carries line breaks (they would break mail headers)."""
    return ' '.join(str(text or '').split())[:limit]


def clip(text):
    """Whitespace-normalised text as stored for the exclusion words."""
    return ' '.join(text.split())[:TEXT_LIMIT]


# Negations such as "kein WBS erforderlich" or "unbefristet" must not exclude an offer.
TRAIT_PATTERNS = {
    'WBS': (
        r'\bWBS\b|Wohnberechtigungsschein',
        r'(?:kein|keine|ohne|nicht)\s+(?:\w+\s+){0,2}(?:WBS|Wohnberechtigungsschein)|(?:WBS|Wohnberechtigungsschein)\s+(?:ist\s+)?(?:nicht|kein)',
    ),
    'Befristet': (
        r'\bbefristet|Zwischenmiete|Untermiete|Zeitmiete|\bauf\s+Zeit\b',
        r'(?:nicht|kein|keine)\s+(?:\w+\s+){0,1}(?:befristet|Zwischenmiete|Untermiete|Zeitmiete)',
    ),
    'Tauschwohnung': (
        r'Tauschwohnung|Wohnungstausch|\bzum\s+Tausch|\bTausch\b|\btauschen\b',
        r'(?:kein|keine|nicht)\s+(?:\w+\s+){0,1}(?:Tausch|tauschen)',
    ),
    'WG-Zimmer': (r'\bWG\b|WG-Zimmer|Mitbewohner(?:in)?', r'(?:keine|kein)\s+(?:\w+\s+){0,1}WG\b'),
}


def detect_traits(text, temporary=False):
    """Traits named in the text; TEMPORARY adds "Befristet" for an end date given elsewhere."""
    traits = []
    for trait, (positive, negated) in TRAIT_PATTERNS.items():
        cleaned = re.sub(negated, ' ', text, flags=re.I)
        if re.search(positive, cleaned, re.I) or (temporary and trait == 'Befristet'):
            traits.append(trait)
    return traits


MONTHS = {
    name: index
    for index, name in enumerate(
        [
            'januar',
            'februar',
            'märz',
            'april',
            'mai',
            'juni',
            'juli',
            'august',
            'september',
            'oktober',
            'november',
            'dezember',
        ],
        1,
    )
}


def available_from(text):
    """Move-in date as ISO day: "sofort" is today, "Oktober 2026" the 1st, "nach Vereinbarung" unknown."""
    text = (text or '').strip().casefold()
    if not text:
        return None
    if 'sofort' in text or text in ('frei', 'bezugsfrei'):
        return date.today().isoformat()
    if match := re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', text):
        day, month, year = (int(part) for part in match.groups())
        year += 2000 if year < 100 else 0
    elif (match := re.search(r'([a-zä]+)\s+(\d{4})', text)) and match.group(1) in MONTHS:
        day, month, year = 1, MONTHS[match.group(1)], int(match.group(2))
    elif match := re.search(r'(\d{1,2})\s*/\s*(\d{4})', text):
        day, month, year = 1, int(match.group(1)), int(match.group(2))
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


_AGE_UNITS = {'sekunde': 1 / 60, 'minute': 1, 'stunde': 60, 'tag': 1440, 'woche': 10080}


def published_at(text, now=None):
    """ISO time from ImmoScout24's "vor 26 Minuten", "vor einem Tag" or "gerade eben"."""
    text = (text or '').casefold()
    now = now or datetime.now(UTC)
    if match := re.search(r'vor\s+(\d+|eine[mnr]?|ein)\s+(sekunde|minute|stunde|tag|woche)', text):
        count = int(match.group(1)) if match.group(1).isdigit() else 1
        minutes = count * _AGE_UNITS[match.group(2)]
    elif re.search(r'gerade|soeben|jetzt|wenigen sekunden', text):
        minutes = 0
    else:
        return None
    return (now - timedelta(minutes=minutes)).replace(second=0, microsecond=0).isoformat()


def coordinate_pair(lat, lon):
    """{"lat": .., "lon": ..} for plausible coordinates in Germany, else {}."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {}
    return {'lat': round(lat, 6), 'lon': round(lon, 6)} if 47 <= lat <= 56 and 5 <= lon <= 16 else {}


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


# Requests from flat seekers ("Paar sucht Wohnung", "Wohnung gesucht") posted
# among offers. The Kleinanzeigen search already asks for offers only; this
# title check is the safety net. Titles that look for a person for the flat
# ("Nachmieter gesucht", "Suche Nachmieter") or also offer one ("biete") stay;
# swaps are left to the "Tauschwohnung" exclusion.
_SEEKING = re.compile(r'\b(?:suche|suchen|sucht|gesucht)\b|\bauf\s+der\s+suche\b|\bwohnungssuche\b', re.I)
_OFFER_HINT = re.compile(
    r'\b(?:nach|unter)mieter(?:in|innen)?\b|\bnachfolger(?:in)?\b|\bmitbewohner(?:in|innen)?\b|\bbewohner(?:in|innen)?\b|\bbiete\b|\bzu\s+vermieten\b|tausch'
    r'|\b(?:neue[nr]?|nette[nr]?|zuverlässige[nr]?)?\s*mieter(?:in|innen)?\s+gesucht\b|\bsuchen?\s+(?:eine[nr]?\s+)?(?:neue[nr]?\s+)?mieter',
    re.I,
)


def is_wanted_ad(title):
    return bool(_SEEKING.search(title)) and not _OFFER_HINT.search(title)


# --- A search as the filter rules see it ---------------------------------------------


def profile(search):
    """The flat filters of a stored search (kind 'wohnung').

    The place is the search's query; filters may hold the picked place ({kind, label, query, geopath})
    and min_area, min_rooms, exclusions (TRAITS), move_in_from, move_in_by (ISO days).
    """
    filters = search.get('filters') or {}
    words = [' '.join(word.split()) for word in (search.get('excluded') or '').split(',')]
    return {
        'city': search.get('query') or '',
        'place': filters.get('place'),
        'radius': search.get('radius') or 0,
        'maxRent': (search.get('max_price') or 0) / 100,
        'minArea': filters.get('min_area') or 0,
        'minRooms': filters.get('min_rooms') or 0,
        'exclusions': [trait for trait in filters.get('exclusions') or [] if trait in TRAITS],
        'excludeWords': [word for word in words if word],
        'excludedDistricts': filters.get('excluded_districts') or [],
        'moveInFrom': filters.get('move_in_from'),
        'moveInBy': filters.get('move_in_by'),
    }


def place_query(profile):
    """What the portals' and OpenStreetMap's place searches get: the picked place, else the typed one."""
    return (profile.get('place') or {}).get('query') or profile['city']


def query_text(source, search):
    """The portal request of a flat search; searches with the same text share one request.

    bauverein and NHW always list their whole stock: one request serves every search, the place is
    checked locally (select). Only filters the portal itself applies belong in the text.
    """
    if source in WHOLE_STOCK:
        return 'whole stock'
    p = profile(search)
    values = [place_query(p).casefold(), (p['place'] or {}).get('kind') or '', str(p['radius'])]
    values += [f'{p[limit]:g}' for limit in PORTAL_LIMITS.get(source, ())]
    values += [trait for trait in PORTAL_EXCLUSIONS.get(source, ()) if trait in p['exclusions']]
    return '\n'.join(values)


WHOLE_STOCK = ('bauverein', 'nhw')
ALL_LIMITS = ('maxRent', 'minArea', 'minRooms')
PORTAL_LIMITS = {  # the search's limits each portal applies itself
    'immoscout24': ALL_LIMITS,
    'kleinanzeigen-wohnung': ALL_LIMITS,
    'wg-gesucht': ALL_LIMITS,
    'immowelt': ALL_LIMITS,
    'immobilien-de': ALL_LIMITS,
    'ohne-makler': ('maxRent', 'minArea'),
}
PORTAL_EXCLUSIONS = {  # the excluded traits each portal leaves out itself (checked live 31.10.2026)
    'immoscout24': ('WBS',),
    'wg-gesucht': ('Befristet', 'Tauschwohnung'),
    'immowelt': ('WBS', 'Befristet', 'Tauschwohnung', 'WG-Zimmer'),
}


# --- Filter rules --------------------------------------------------------------------

# Rough monthly running costs incl. heating on top of the cold rent, used when an
# offer names only the cold rent (Darmstadt Betriebskostenspiegel: about 3 €/m²).
RUNNING_COSTS_PER_SQM = 3.0
RUNNING_COSTS_SHARE = 0.25  # when the area is unknown as well


def estimated_rent(listing):
    if listing.rent is not None or listing.cold_rent is None:
        return None
    cold = listing.cold_rent
    return round(cold + (listing.area * RUNNING_COSTS_PER_SQM if listing.area else cold * RUNNING_COSTS_SHARE))


def warm_rent(listing):
    """The warm rent as stated, else estimated from the cold rent; None if the listing names neither."""
    return listing.rent if listing.rent is not None else estimated_rent(listing)


def other_rent(old, new):
    """Whether NEW, a later card of the same listing, asks another rent than OLD."""
    before, now = warm_rent(old), warm_rent(new)
    return before is not None and now is not None and now != before


def cheaper(old, new):
    """Whether NEW, a later card of the same listing, asks less rent than OLD."""
    before, now = warm_rent(old), warm_rent(new)
    return before is not None and now is not None and now < before


def _day(value):
    return datetime.fromisoformat(value).strftime('%d.%m.%Y')


POSTCODE = re.compile(r'\b\d{5}\b')


def home_town(profile):
    """The town of the searched place; for a postcode the town its label names, '' if it names none."""
    place = profile.get('place') or {}
    text = place.get('label') or profile.get('city') or ''
    return re.sub(r'^\d{5}\s*', '', re.split(r'[,(]| - ', text)[0]).strip()


def home_area(profile):
    """The town (or postcode) a search without radius is limited to."""
    query = place_query(profile)
    return query if POSTCODE.fullmatch(query) else home_town(profile)


def outside_home(listing, profile):
    """Portals mix in neighbouring towns; a search without radius keeps what lies in the town itself.

    An address without postcode (NHW names only the town) is judged by the postcode's town: the
    postcode cannot be told, and a flat must not get lost over it."""
    where = listing.place
    if profile.get('radius') or not where:
        return False
    area = home_area(profile)
    if POSTCODE.fullmatch(area) and not POSTCODE.search(where):
        area = home_town(profile)
    return bool(area) and re.search(rf'(?<![\w-]){re.escape(area)}(?![\w])', where, re.I) is None


# A search by distance must never lose a flat in the searched area: only a known
# position counts, with a margin for positions without a house number.
RADIUS_MARGIN_KM = 1.5


def beyond_radius(listing, profile, centre):
    """Distance from the searched place's centre if the offer lies clearly outside the radius."""
    if not profile.get('radius') or not centre or listing.lat is None or listing.lon is None:
        return None
    km = distance_km(centre, (listing.lat, listing.lon))
    return km if km > profile['radius'] + RADIUS_MARGIN_KM else None


def excluded_district(place, districts):
    return next((name for name in districts if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', place, re.I)), None)


def match(listing, profile, centre=None):
    """("matched" | "unclear" | "excluded", reasons) of one listing for one search.

    The portal search applies place, radius and its own filters; the radius is checked again here for
    portals that ignore it. CENTRE is the searched place's centre, None if not looked up yet.
    """
    if is_wanted_ad(listing.title):
        return 'excluded', ['Gesuch, kein Angebot']
    if outside_home(listing, profile):
        return 'excluded', [f'Nicht in {home_area(profile)}']
    if (km := beyond_radius(listing, profile, centre)) is not None:
        return 'excluded', [f'{km:.1f} km von der Mitte (bis {profile["radius"]} km)'.replace('.', ',')]
    if district := excluded_district(listing.place, profile.get('excludedDistricts', [])):
        return 'excluded', [f'Ortsteil „{district}“ ausgeschlossen']
    excluded = set(profile['exclusions']) & set(listing.traits)
    if excluded:
        return 'excluded', sorted(excluded)
    text = f'{listing.title}\n{listing.text}'.casefold()
    word = next((word for word in profile.get('excludeWords', []) if word.casefold() in text), None)
    if word:
        return 'excluded', [f'Enthält „{word}“']
    available = listing.available_from
    if available and profile.get('moveInBy') and available > profile['moveInBy']:
        return 'excluded', [f'Frei erst ab {_day(available)}']
    if available and profile.get('moveInFrom') and available < profile['moveInFrom']:
        return 'excluded', [f'Schon ab {_day(available)} frei']
    missing = []
    rent = warm_rent(listing)
    if rent is not None and rent > profile['maxRent']:
        return 'excluded', ['Warmmiete zu hoch' if listing.rent is not None else 'Warmmiete (geschätzt) zu hoch']
    if rent is None:
        missing.append('Miete fehlt')
    for value, minimum, label, reason in (
        (listing.area, profile['minArea'], 'Fläche', 'Fläche zu klein'),
        (listing.rooms, profile['minRooms'], 'Zimmer', 'Zu wenige Zimmer'),
    ):
        if value is None:
            missing.append(label + ' fehlt')
        elif value < minimum:
            return 'excluded', [reason]
    if (profile['exclusions'] or profile.get('excludeWords')) and not listing.exclusions_known:
        missing.append('Beschreibung noch nicht geprüft')
    return ('unclear', missing) if missing else ('matched', ['Alle Filter erfüllt'])


def could_match(listing, profile, centre=None):
    """Whether a detail page is worth a request: nothing on the card already rules the listing out.

    The same rules as match(), except that an estimated warm rent never rules out (the detail page may
    state a lower one); a cold rent above the limit does, since the warm rent can only be higher.
    """
    if listing.cold_rent is not None and listing.cold_rent > profile['maxRent']:
        return False
    verdict, _ = match(replace(listing, cold_rent=None), profile, centre)
    return verdict != 'excluded'
