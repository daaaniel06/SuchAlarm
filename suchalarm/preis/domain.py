import re
import unicodedata
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse, urlunparse

SOURCES = {'ebay': 'eBay', 'kleinanzeigen': 'Kleinanzeigen', 'vinted': 'Vinted', 'idealo': 'idealo · Shops'}
HOSTS = {
    'ebay': {'www.ebay.de', 'ebay.de'},
    'kleinanzeigen': {'www.kleinanzeigen.de', 'kleinanzeigen.de'},
    'vinted': {'www.vinted.de', 'vinted.de'},
    'idealo': {'www.idealo.de', 'idealo.de'},
}
# Only these providers can search around a postcode; pickup searches use them alone.
PICKUP_SOURCES = ('ebay', 'kleinanzeigen')
RADII = (10, 25, 50, 100, 200)
PLATFORMS = {
    'ps5': 'PS5',
    'ps4': 'PS4',
    'switch2': 'Switch 2',
    'switch': 'Switch',
    'xbox-series': 'Xbox Series',
    'xbox-one': 'Xbox One',
    'pc': 'PC',
}
# Vinted files games under one section and filters the platform separately (video_game_platform_ids[]).
# IDs checked live on 29.10.2026; PS5 games are also tagged "PS5 Pro".
VINTED_PLATFORMS = {
    'ps5': ('1281', '8582'),
    'ps4': ('1280',),
    'switch2': ('6478',),
    'switch': ('1273',),
    'xbox-series': ('1291',),
    'xbox-one': ('1290',),
    'pc': ('1276',),
}
# A category presets a new search and narrows the provider search to its own section.
# Section IDs were checked live on 23.10.2026 (eBay _sacat, Kleinanzeigen c…, Vinted catalog[]).
CATEGORIES: dict[str, dict[str, Any]] = {
    'videospiele': dict(
        label='Videospiele',
        hint='PlayStation, Switch, Xbox',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        # Decoration is sold under a game's name and filed as the game itself (Vinted, 29.10.2026).
        excluded='Hülle, nur Cover, Code, Download, Poster, Plakat, Affiche, Mauspad, Mousepad, Tapis de souris, '
        'Schild, Targa, Bilderrahmen, Cadre, Flagge, Drapeau, Figur, Figurine, Sticker',
        negotiable=False,
        free=False,
        radius=0,
        filters=['platform'],
        portal={'ebay': '139973', 'kleinanzeigen': '227', 'vinted': '3026'},
    ),
    'elektronik': dict(
        label='Elektronik',
        hint='Handy, Laptop, Kopfhörer',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        excluded='defekt, Bastler',
        negotiable=False,
        free=False,
        radius=0,
        filters=[],
        portal={'kleinanzeigen': '161', 'vinted': '2994'},
    ),
    'moebel': dict(
        label='Möbel',
        hint='Abholung in deiner Nähe',
        delivery='abholung',
        sources=['ebay', 'kleinanzeigen'],
        excluded='',
        negotiable=True,
        free=True,
        radius=25,
        filters=[],
        portal={'ebay': '3197', 'kleinanzeigen': '80'},
    ),
    'fahrraeder': dict(
        label='Fahrräder',
        hint='Abholung in deiner Nähe',
        delivery='abholung',
        sources=['ebay', 'kleinanzeigen'],
        excluded='defekt',
        negotiable=True,
        free=True,
        radius=50,
        filters=[],
        portal={'ebay': '177831', 'kleinanzeigen': '217'},
    ),
    'autos': dict(
        label='Autos',
        hint='Erstzulassung, Kilometer',
        delivery='abholung',
        sources=['kleinanzeigen'],
        excluded='Unfall, Motorschaden',
        negotiable=True,
        free=False,
        radius=100,
        filters=['year_from', 'km_to'],
        portal={'ebay': '9801', 'kleinanzeigen': '216'},
    ),
    'sonstiges': dict(
        label='Sonstiges',
        hint='Ohne Vorgaben',
        delivery='versand',
        sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'],
        excluded='',
        negotiable=False,
        free=False,
        radius=0,
        filters=[],
        portal={},
    ),
}


MAX_AMOUNT = 10_000_000  # euros; anything above is a parsing accident, not a price


def money(value):
    """Convert a EUR amount into integer cents, never binary floats."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace('\xa0', '').replace(' ', '').replace('€', '').replace('EUR', '')
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', text):
        text = text.replace('.', '')
    if ',' in text and '.' in text and text.rfind('.') > text.rfind(','):
        text = text.replace(',', '')
    elif ',' in text:
        text = text.replace('.', '').replace(',', '.')
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
            return None
        return int((amount * 100).quantize(Decimal('1')))
    except InvalidOperation:
        return None


_LABEL = r'(?:versand(?:kosten)?|liefer(?:ung|kosten)|shipping|postage|delivery)'
_NUMBER = r'\d+(?:[.,]\d+)*'
_COST = rf'(?:eur\s*{_NUMBER}|{_NUMBER}\s*(?:€|eur\b))'
_FREE = r'(?:gratis|kostenlos(?:e[rmns]?)?|kostenfrei(?:e[rmns]?)?|free)'
_DAYS = r'(?:\d+\s*(?:[-–]\s*\d+)?\s*(?:tage?|days?)\s*)?'
FREE_SHIPPING = re.compile(
    rf'{_FREE}\s*{_DAYS}{_LABEL}|versandkostenfrei|(?<![a-zäöüß]){_LABEL}\s*:?\s*(?:gratis|kostenlos|kostenfrei|free)\b'
)
# Context that makes a mention useless: "kein kostenloser Versand", "ab 4 €", "Rückversand", ranges.
_FREE_NEGATED = re.compile(r'\b(?:kein[er]?|nicht|ohne)\s*$')
_FREE_QUALIFIED = re.compile(r'\s*(?:ab\b|from\b|(?:für\s+)?rück|returns?\b)')
_COST_VAGUE_BEFORE = re.compile(r'(?:\b(?:ca\.?|etwa|bis|kein|ohne|nicht)|[-–])\s*$')
_COST_VAGUE_AFTER = re.compile(r'\s*(?:[-–]|bis\b|to\b|(?:für\s+)?rück|returns?\b)')
_FROM_BEFORE = re.compile(r'\b(?:ab|from)\s*$')


def _around(text, match):
    return text[max(0, match.start() - 25) : match.start()], text[match.end() : match.end() + 35]


def _cents(value):
    value = value.replace('eur', '').replace('€', '').strip()
    return 0 if re.fullmatch(r'0(?:[,.]0+)?', value) else money(value)


def _free_mentions(text):
    """True if free shipping is stated, False if not mentioned, None if a mention is negated or qualified."""
    found = False
    for match in FREE_SHIPPING.finditer(text):
        before, after = _around(text, match)
        if _FREE_NEGATED.search(before) or _FREE_QUALIFIED.match(after):
            return None
        found = True
    return found


def _charges(text, allow_from):
    """Stated shipping amounts in cents, or None if one is vague (a range, "ca.", a return fee, a minimum)."""
    minimum = r'(?:ab\s*)?' if allow_from else ''
    amounts = set()
    for pattern in (
        rf'(?<!rück)(?<!rueck){_LABEL}\s*[:+]?\s*{minimum}(?P<cost>{_COST})',
        rf'(?<![\w.,])(?P<cost>{_COST})\s*(?:·\s*{_DAYS})?{_LABEL}',  # eBay: "+EUR 6,19 · 2-3 Tage Lieferung"
    ):
        for match in re.finditer(pattern, text):
            before, after = _around(text, match)
            if _COST_VAGUE_BEFORE.search(before) or _COST_VAGUE_AFTER.match(after):
                return None
            if not allow_from and _FROM_BEFORE.search(before):
                return None
            amounts.add(_cents(match.group('cost')))
    return amounts


def shipping(text, *, amount_only=False, allow_from=False):
    """Outbound EUR shipping in cents, or None when unknown or ambiguous. Never guesses 0.

    AMOUNT_ONLY: TEXT is a dedicated shipping field, so a bare amount counts. ALLOW_FROM: a
    provider minimum ("ab 2,49 €", Vinted) is accepted as the amount.
    """
    text = re.sub(r'\s+', ' ', str(text).casefold().replace('\xad', '')).strip()
    values = set()
    # A bare amount is valid only in a dedicated shipping field, never in card text.
    if amount_only and re.fullmatch((r'(?:ab\s*)?' if allow_from else '') + _COST, text):
        values.add(_cents(re.sub(r'^ab\s*', '', text)))
    free = _free_mentions(text)
    if free is None:
        return None
    if free:
        values.add(0)
    charges = _charges(text, allow_from)
    if charges is None:
        return None
    values = (values | charges) - {None}
    return values.pop() if len(values) == 1 else None


def clean_title(source, title):
    """Remove eBay's UI labels at the title boundaries, preserving seller wording."""
    title = title.strip()
    if source == 'ebay':
        title = re.sub(
            r'\s*(?:Wird in neuem Fenster oder Tab geöffnet|Opens in a new window or tab)\s*$', '', title, flags=re.I
        )
        # Old textContent snapshots glued this badge directly to the actual title.
        title = re.sub(r'^(?:Neues Angebot|New Listing)(?=\S)', '', title).strip()
    return title


def platform(search):
    """The platform a search is limited to, or None."""
    chosen = (search.get('filters') or {}).get('platform')
    return chosen if chosen in PLATFORMS else None


def terms(search):
    """The words of a search: the query plus a chosen platform."""
    chosen = platform(search)
    return f'{search["query"]} {PLATFORMS[chosen]}' if chosen else search['query']


def platform_ids(source, search):
    """The provider's own platform filter for the search; empty where the platform must be in the text."""
    chosen = platform(search)
    return VINTED_PLATFORMS[chosen] if source == 'vinted' and chosen else ()


def wanted_words(source, search):
    """The words a provider is searched for and an offer title must contain. Where the provider filters the
    platform itself, the title need not name it: sellers often leave it out ("Resident Evil Requiem")."""
    return search['query'] if platform_ids(source, search) else terms(search)


def named_platforms(title):
    """The platforms a title names; "Switch 2" is not also "Switch"."""
    text, named = f' {normalized(title)} ', set()
    for key, label in sorted(PLATFORMS.items(), key=lambda item: -len(item[1])):
        phrase = f' {normalized(label)} '
        if phrase in text:
            named.add(key)
            text = text.replace(phrase, ' ')
    return named


def title_fits(source, title, search):
    """Whether an offer title belongs to the search: all its words, nothing excluded, not only other platforms.
    A provider's platform filter fills up with other platforms once its matches run out."""
    chosen, named = platform(search), named_platforms(title)
    return matches(title, wanted_words(source, search), search.get('excluded', '')) and (
        chosen is None or not named or chosen in named
    )


def taught_away(title, search):
    """Whether TITLE carries a word hiding taught the search's category (learning.py), never one of its own words.
    Applied per search after a shared provider result, like the search's own excluded words."""
    own = set(normalized(terms(search)).split())
    return any(word not in own for word in set(normalized(title).split()) & set(search.get('learned', ())))


LEARN_FROM = 2  # hidden titles a word needs before it filters on its own


def learned_words(hidden, kept_titles):
    """Words that mark what gets hidden: in at least LEARN_FROM hidden titles and in more hidden than kept ones.
    HIDDEN holds (title, the words its search asked for); those words say what was wanted, never why it was hidden.
    Numbers never count: they are editions, sizes and years, not a kind of junk."""

    def words(title):
        return set(normalized(title).split())

    hidden_counts = Counter(word for title, wanted in hidden for word in words(title) - words(wanted))
    kept_counts = Counter(word for title in kept_titles for word in words(title))
    return sorted(
        word
        for word, count in hidden_counts.items()
        if count >= LEARN_FROM and count > kept_counts[word] and not word.isdigit()
    )


def section(source, search):
    """The provider's own section for the search's category, or None."""
    return CATEGORIES.get(search.get('category') or 'sonstiges', CATEGORIES['sonstiges'])['portal'].get(source)


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


def lowest_item_price(source, search, compared):
    """The lowest item price whose price before shipping (item + buyer protection) reaches COMPARED."""
    return bisect_left(range(compared), compared, key=lambda price: price + search_fee(source, price, search))


def portal_price_bounds(source, search):
    """Conservative item-price window; never drop a valid total-price match because of shipping."""
    if source == 'idealo':
        return 0, None  # the independent new-price reference must survive even above the alarm limit
    # Buyer protection counts on top of the item price, so the window shifts down to the item prices that fit.
    # Shipping is unknown until read, so with shipping the item itself may cost anything below the maximum.
    minimum = 0 if search.get('include_shipping') else lowest_item_price(source, search, search.get('min_price', 0))
    maximum = (
        lowest_item_price(source, search, search['max_price']) - 1 if search.get('max_price') is not None else None
    )
    if source == 'kleinanzeigen':
        # "Zu verschenken" has no price: any lower bound would hide it at the provider.
        if search.get('free'):
            minimum = 0
        # Kleinanzeigen accepts whole euros: widen outwards, then enforce exact cents locally.
        minimum = minimum // 100 * 100
        maximum = ((maximum + 99) // 100) * 100 if maximum is not None else None
    return minimum, maximum


def select_offers(offers, search):
    """One destination per result; choose its cheapest shop using this search's price basis."""
    result: dict[str, Offer] = {}

    def rank(offer):
        value = offer.compared_price(search)
        return (
            value is None,
            value if value is not None else offer.price,
            offer.price,
            offer.shipping if offer.shipping is not None else float('inf'),
        )

    for offer in offers:
        offer = replace(offer, title=clean_title(offer.source, offer.title))
        if not title_fits(offer.source, offer.title, search) or taught_away(offer.title, search):
            continue
        if offer.url not in result or rank(offer) < rank(result[offer.url]):
            result[offer.url] = offer
    return list(result.values())


@lru_cache(maxsize=4096)  # the same titles and search words come back with every check
def normalized(text):
    # Accents are spelled either way ("Résident Evil", "Hulle"); compare the base letters.
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    text = re.sub(r'[^\w]+', ' ', text).strip()
    # Sellers write PS5, PS 5 and PlayStation 5 interchangeably.
    return re.sub(r'\b(?:playstation|ps) ?([1-5])\b', r'ps\1', text)


def matches(title, query, excluded=''):
    text = normalized(title)
    words = set(text.split())
    if not all(w in words for w in normalized(query).split()):
        return False
    padded = f' {text} '
    return not any(term and f' {term} ' in padded for term in map(normalized, excluded.split(',')))


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


def with_shipping(source, search):
    """Whether an offer compares with shipping: idealo shops always by their full price, others as the search says."""
    return source == 'idealo' or bool(search.get('include_shipping'))


def buyer_fee(source, price):
    """Vinted's buyer protection on an item: 5 % of the price plus 0,70 €. Checked 28.10.2026, the "inkl."
    price on Vinted's cards says the same (44,00 € -> 46,90 €). Other providers add nothing."""
    return (price * 5 + 50) // 100 + 70 if source == 'vinted' and price > 0 else 0


def search_fee(source, price, search):
    """The buyer protection a search adds to an item price: only on Vinted and only while it includes it."""
    return buyer_fee(source, price) if search.get('include_fee') else 0


def compared_price(price, shipping, include_shipping, fee=0):
    """The price a search compares: item price (+ buyer protection FEE), or that + known shipping.
    Unknown shipping is never 0."""
    if not include_shipping:
        return price + fee
    return None if shipping is None else price + fee + shipping


@dataclass(frozen=True)
class Offer:
    source: str
    url: str
    title: str
    price: int
    shipping: int | None = None
    seller: str = ''
    condition: str = ''

    @classmethod
    def from_row(cls, row):
        """An offer from a stored row (offers or saved table)."""
        return cls(
            row['source'], row['url'], row['title'], row['price'], row['shipping'], row['seller'], row['condition']
        )

    def compared_price(self, search):
        return compared_price(
            self.price, self.shipping, with_shipping(self.source, search), search_fee(self.source, self.price, search)
        )


def free(price, search):
    """A given-away offer (price 0) in a search that wants them; the minimum price does not apply."""
    return price == 0 and bool(search.get('free'))


def in_range(price, search):
    """The price range of a search: from min_price (junk such as 3 € listings stays out) up to below max_price."""
    return (
        price is not None
        and (search.get('min_price', 0) <= price or free(price, search))
        and price < search['max_price']
    )


def too_cheap(offer, search):
    # Without a known shipping price the item price alone decides; a 3 € listing is junk either way.
    if free(offer.price, search):
        return False
    price = offer.compared_price(search)
    return (price if price is not None else offer.price) < search.get('min_price', 0)


def eligible(offer, search):
    return in_range(offer.compared_price(search), search)


def query_text(source, search):
    """The normalized provider query of a search; searches with the same text share one request."""
    query = ' '.join(terms(search).casefold().split())
    category, place = section(source, search), around(source, search)
    # Section, place, vehicle limits and VB change the provider's answer; without them the key stays as before.
    if category:
        query += f'\nsection:{category}'
    if place:
        query += '\naround:{}:{}'.format(*place)
    filters = search.get('filters') or {}
    if source == 'kleinanzeigen' and category == '216' and (filters.get('year_from') or filters.get('km_to')):
        query += f'\nvehicle:{filters.get("year_from") or ""}:{filters.get("km_to") or ""}'
    # VB offers are dropped while parsing at every provider, so the shared result depends on it everywhere.
    if search.get('negotiable'):
        query += '\nnegotiable'
    if search.get('free') and source == 'kleinanzeigen':
        query += '\nfree'
    if search.get('delivery') == 'beides' and place:
        query += '\nand-nationwide-shipping'
    if source == 'ebay':
        query += '\nitem-location:DE'
    if source == 'vinted':
        query += '\norder:relevance'
    minimum, maximum = portal_price_bounds(source, search)
    if minimum or maximum is not None:
        query += f'\nprice:{minimum}:{maximum}'
    return query
