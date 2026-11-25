"""Shared by the portals: plain text of markup, the place check, the fallback parser for renamed card
markup and the fallback reader that fills fields a portal's own reading missed."""

import logging
import re
from collections import Counter
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..domain import (
    FROM_TEXT,
    area,
    available_from,
    clip,
    detect_traits,
    distance_km,
    is_wanted_ad,
    line,
    money,
    number,
    rooms,
)

log = logging.getLogger('suchalarm')


def plain(fragment):
    return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', fragment)).split())


def attribute(tag, name):
    match = re.search(rf'\b{name}="([^"]*)"', tag)
    return unescape(match.group(1)) if match else ''


_NOT_INCLUDED = re.compile(r'\b(?:nicht|nein|keine?|zzgl|zuzüglich|exkl|exklusive|extra|separat)\b')
_INCLUDED = re.compile(r'enthalten|inkl|inklusive|inbegriffen')


def heating_included(text):
    """Whether a portal's heating line says the heating is paid with the other costs: "in Nebenkosten
    enthalten" is, "nicht in Nebenkosten enthalten" is not."""
    text = (text or '').casefold()
    return bool(_INCLUDED.search(text)) and not _NOT_INCLUDED.search(text)


def warm_from_parts(cold, running, heating, included=False):
    """The warm rent from its parts, for an offer that names no total: only with every part known, the heating
    INCLUDED in the running costs (an amount then is already in them) or as an amount of its own. Otherwise None,
    so the warm rent is estimated and marked so (domain.estimated_rent) instead of passing a total without the
    heating as stated."""
    if cold is None or running is None or (heating is None and not included):
        return None
    return cold + running + (0 if included else heating)


def near(place, radius, centre, address, spot):
    """A radius search measures the distance where both the search's centre and the flat's spot are known.
    Otherwise the town or postcode must match: a landlord's whole stock (all of Hessen at NHW) must not pass
    for want of a position, and a flat elsewhere without coordinates would otherwise be mailed."""
    if radius and centre and spot:
        return distance_km(centre, spot) <= radius
    if re.fullmatch(r'\d{5}', place):
        return re.search(rf'\b{place}\b', address) is not None
    town = re.split(r'[,(-]', place)[0].strip().casefold()
    return bool(town) and town in address.casefold()


# --- Fallback when a portal renames its card markup ---------------------
# Offers are found by their detail links, which rarely change, and read from the card text by pattern.

VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
SELF_CLOSING = {'p', 'li', 'option', 'tr', 'td', 'dt', 'dd'}  # an opening tag closes an open sibling


class _Node:
    __slots__ = ('attrs', 'children', 'end', 'parent', 'start', 'tag')

    def __init__(self, tag, attrs, parent, start):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []
        self.start = self.end = start  # range of detail links inside this node


class _Cards(HTMLParser):
    """Minimal DOM that records every detail link (href or data-href matching `link`)."""

    def __init__(self, link, base):
        super().__init__(convert_charrefs=True)
        self.link, self.base = link, base
        self.root = self.current = _Node('root', {}, None, 0)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in SELF_CLOSING and self.current.tag == tag:
            self._close(self.current)
        node = _Node(tag, dict(attrs), self.current, len(self.links))
        self.current.children.append(node)
        for name in ('href', 'data-href'):
            href = (node.attrs.get(name) or '').split('#')[0].split('?')[0]
            if href and self.link.search(href):
                self.links.append((node, urljoin(self.base, href)))
                break
        if tag in VOID_TAGS:
            node.end = len(self.links)
        else:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self._close(node)

    def _close(self, node):
        while self.current is not node.parent:
            self.current.end = len(self.links)
            self.current = self.current.parent

    def handle_data(self, data):
        if self.current.tag not in ('script', 'style', 'noscript', 'template'):
            self.current.children.append(data)

    def close(self):
        super().close()
        while self.current is not None:  # unclosed elements end with the document
            self.current.end = len(self.links)
            self.current = self.current.parent


def _strings(node):
    """Text pieces of a node in document order, whitespace-normalised."""
    pieces, stack = [], [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if item.split():
                pieces.append(' '.join(item.split()))
        else:
            stack.extend(reversed(item.children))
    return pieces


def _descendants(node):
    stack = list(reversed(node.children))
    while stack:
        item = stack.pop()
        if isinstance(item, _Node):
            yield item
            stack.extend(reversed(item.children))


# An amount in euros ("1.250 €", "950,50 EUR"), never a price per m² ("9,82 €/m²").
MONEY = r'(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*(?:€|EUR\b)(?!\s*/\s*m)'
SIZE = r'(\d+(?:[.,]\d+)?)\s*(?:m²|m2\b|qm\b)'
PRICE = re.compile(MONEY + r'(?=(.{0,25}))', re.I)  # the words after it, which the next amount may share
AREA = re.compile(SIZE, re.I)
ROOMS = re.compile(r'(\d+(?:[.,]5)?)\s*-?\s*(?:Zi\.|Zimmer)')
PLACE = re.compile(r'^\d{5}\s+\D|\(\d{5}\)')


def fallback_cards(html, link, base='', skip_wanted=False, warm=False):
    """Offers read without portal-specific markup: one card per distinct detail link.

    `warm`: the card price is the total rent unless it says otherwise."""
    tree = _Cards(re.compile(link), base)
    tree.feed(html)
    tree.close()
    listings, seen = [], set()
    for node, url in tree.links:
        if url in seen:
            continue
        seen.add(url)
        listing = _fallback_card(node, _card_around(tree, node, url), url, base, warm)
        if not (skip_wanted and is_wanted_ad(listing['title'])):
            listings.append(listing)
    return listings


def _card_around(tree, node, url):
    """The largest element around the link that holds no other offer."""
    card = node
    while card.parent.tag not in ('root', 'html', 'body') and all(
        other == url for _, other in tree.links[card.parent.start : card.parent.end]
    ):
        card = card.parent
    return card


def _fallback_card(node, card, url, base, warm):
    # Some portals put the facts only into the link's title attribute.
    pieces = ([node.attrs['title']] if node.attrs.get('title') else []) + _strings(card)
    text = ' '.join(pieces)
    heading = next((n for n in _descendants(card) if n.tag in ('h1', 'h2', 'h3', 'h4')), None)
    title = line(' '.join(_strings(heading)) if heading else (pieces[0] if pieces else '')) or 'Wohnung'
    return {
        'url': url,
        'title': title,
        **text_facts(pieces, warm),
        'traits': detect_traits(title + '\n' + text),
        'text': clip(text),
        'imageSource': _card_picture(card, base),
    }


def text_facts(pieces, warm=False):
    """What the text pieces of a card say, read by pattern alone: place, rents, size, rooms and move-in date.
    `warm`: the card price is the total rent unless it says otherwise."""
    text = ' '.join(pieces)
    rent, cold = _card_price(text, warm)
    size, count = AREA.search(text), ROOMS.search(text)
    date = re.search(r'\b\d{2}\.\d{2}\.\d{4}\b', text)
    return {
        'place': line(next((piece for piece in pieces if PLACE.search(piece)), '')),
        'rent': rent,
        'coldRent': cold,
        'area': area(number(size.group(1))) if size else None,
        'rooms': rooms(number(count.group(1))) if count else None,
        'availableFrom': available_from(date.group(0)) if date else None,
    }


PRICE_LABEL = re.compile(r'(\w*(?:warm|kalt)\w*)\W{0,3}$', re.I)  # "Warmmiete 950 €", "Kaltmiete: 700 €"
# Amounts that are no rent: "Kaution 450 € · Warmmiete 1.250 €" asks 1.250 €.
OTHER_COST = re.compile(
    r'\b(?:kaution|nebenkosten|nk|heizkosten|betriebskosten|provision|stellplatz|garage|ablöse)\W{0,3}$', re.I
)


def _card_price(text, warm):
    """(warm rent, cold rent) of a card: the first amount that is no other cost, warm if it says so or WARM and it
    does not say cold. A label right before the amount says it; otherwise the words after it ("950 € warm")."""
    price = next((found for found in PRICE.finditer(text) if not OTHER_COST.search(text[: found.start()])), None)
    if not price:
        return None, None
    label = PRICE_LABEL.search(text[: price.start()])
    cost, note = money(number(price.group(1))), label.group(1) if label else price.group(2)
    total = bool(re.search(r'warm', note, re.I)) or (warm and not re.search(r'kalt', note, re.I))
    return (cost, None) if total else (None, cost)


def _card_picture(card, base):
    """The first https picture of the card that is no avatar or logo."""
    for node in _descendants(card):
        if node.tag != 'img' or re.search(r'avatar|logo', ' '.join(map(str, node.attrs.values())), re.I):
            continue
        source = urljoin(base, node.attrs.get('src') or node.attrs.get('data-src') or '')
        if source.startswith('https://'):
            return source
    return None


def rescue(portal, html, link, base='', skip_wanted=False, warm=False):
    listings = fallback_cards(html, link, base, skip_wanted, warm)
    if listings:
        log.warning('%s: Kartenlayout unbekannt, Ausweich-Parser las %d Angebote', portal, len(listings))
    return listings


# --- Fallback for single fields ------------------------------------------
# A portal's own reading stays first. Where it misses a field, mostly after a renamed class, the card's text
# fills it (text_facts), as the fallback parser does for whole cards, and a detail page's text its labelled
# values (page_facts).


def own_reading(listing):
    """The listing as the portal's own reading left it, without the fields filled from its card's text: what
    the field check (sources.field_warning) judges, so a changed layout is still reported."""
    return {**listing, **dict.fromkeys(listing.get(FROM_TEXT, ()))}


def html_pieces(fragment):
    """Text pieces of markup in document order, without scripts and styles."""
    fragment = re.sub(r'<(script|style)\b.*?</\1>', ' ', fragment, flags=re.S | re.I)
    return [piece for part in re.split(r'<[^>]+>', fragment) if (piece := ' '.join(unescape(part).split()))]


def json_pieces(value):
    """The text values of decoded JSON in order, for portals that answer with data instead of markup."""
    if isinstance(value, str):
        return [piece] if (piece := ' '.join(value.split())) else []
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [piece for item in items for piece in json_pieces(item)]


def complete(portal, cards, warm=False):
    """The listings of (listing, text pieces of its card) pairs, each field the portal reads but found empty
    taken from the card's text (text_facts)."""
    filled = Counter(field for listing, pieces in cards for field in _fill(listing, text_facts(pieces, warm)))
    _log_filled(portal, 'Kartentext', filled)
    return [listing for listing, _ in cards]


def complete_detail(portal, detail, pieces):
    """A detail page as the portal's parser read it, each field it reads but found empty taken from the labelled
    values of the page's text pieces (page_facts)."""
    _log_filled(portal, 'Seitentext', Counter(_fill(detail, page_facts(pieces))))
    return detail


def _fill(own, facts):
    """The fields of the portal's own reading OWN that it reads but found empty, taken from FACTS read from the
    text. Fields the portal does not read stay out: a cold-rent portal gets no warm rent. OWN names them under
    FROM_TEXT (see own_reading and Listing.with_detail)."""
    filled = [
        field for field, value in facts.items() if field in own and own[field] in (None, '') and value not in (None, '')
    ]
    for field in filled:
        own[field] = facts[field]
    if filled:
        own.setdefault(FROM_TEXT, []).extend(filled)
    return filled


def _log_filled(portal, origin, filled):
    """A hint that the portal's markup changed."""
    if filled:
        found = ', '.join(f'{field} {count}×' for field, count in sorted(filled.items()))
        log.warning('%s: Felder aus dem %s ergänzt (Layout geändert?): %s', portal, origin, found)


# The labels of a detail page, each with its value right behind it ("Wohnfläche ca. 55 m²", "Zimmer: 2").
# Immowelt also names the price per m² first ("Kaltmiete 9,82 €/m² 540 €"). The first labelled value counts: the
# offer's own facts come before what a page adds further down.
_LABEL_GAP = r'\W{0,3}(?:ca\.\s*)?'
PAGE_LABELS = {
    'rent': (r'Warmmiete|Gesamtmiete', rf'(?:[\d.,]+\s*€\s*/\s*m²\s*)?{MONEY}'),
    'coldRent': (r'Nettokaltmiete|Kaltmiete', rf'(?:[\d.,]+\s*€\s*/\s*m²\s*)?{MONEY}'),
    'area': (r'Wohnfläche', SIZE),
    'rooms': (r'Zimmeranzahl|Anzahl Zimmer|Zimmer', r'(\d+(?:,5)?)(?![\d.,]|\s*(?:m²|€))'),  # not "1. OG"
    'availableFrom': (r'Verfügbar ab|Bezugsfrei ab|frei ab', r'(\d{2}\.\d{2}\.\d{4}|sofort)'),
}
# A portal's own estimate ("geschätzte Warmmiete", "geschätzte monatliche Warmmiete") is no stated value.
ESTIMATE = re.compile(r'geschätzt\w*\s+(?:\w+\s+)?$', re.I)
_READERS = {
    'rent': lambda value: money(number(value)),
    'coldRent': lambda value: money(number(value)),
    'area': lambda value: area(number(value)),
    'rooms': lambda value: rooms(number(value)),
    'availableFrom': available_from,
}


def page_facts(pieces):
    """What the labelled values among a detail page's text pieces say: rents, size, rooms and move-in date."""
    text = ' '.join(pieces)
    facts = {}
    for field, (labels, value) in PAGE_LABELS.items():
        stated = (
            found
            for found in re.finditer(rf'\b(?:{labels})\b{_LABEL_GAP}{value}', text, re.I)
            if not ESTIMATE.search(text[: found.start()])
        )
        found = next(stated, None)
        facts[field] = _READERS[field](found.group(1)) if found else None
    return facts
