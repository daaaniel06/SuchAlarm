"""Shared by the portals: plain text of markup, the place check and the fallback parser for
renamed card markup (from Wohnungsbot)."""

import logging
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..domain import (
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


def near(place, radius, centre, address, spot):
    """Radius searches measure the distance; otherwise the town or postcode must match."""
    if radius and centre and spot:
        return distance_km(centre, spot) <= radius
    if re.fullmatch(r'\d{5}', place):
        return re.search(rf'\b{place}\b', address) is not None
    town = re.split(r'[,(-]', place)[0].strip().casefold()
    return bool(town) and town in address.casefold()


# --- Fallback when a portal renames its card markup (from Wohnungsbot) ---------------------
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


PRICE = re.compile(r'(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*(?:€|EUR\b)(.{0,25})', re.I)
AREA = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:m²|m2\b|qm\b)', re.I)
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
    pieces = _strings(card)
    # Some portals put the facts only into the link's title attribute.
    text = ' '.join(([node.attrs['title']] if node.attrs.get('title') else []) + pieces)
    heading = next((n for n in _descendants(card) if n.tag in ('h1', 'h2', 'h3', 'h4')), None)
    fallback_title = node.attrs.get('title') or (pieces[0] if pieces else '')
    title = line(' '.join(_strings(heading)) if heading else fallback_title) or 'Wohnung'
    rent, cold = _card_price(text, warm)
    size, count = AREA.search(text), ROOMS.search(text)
    dates = re.findall(r'\b\d{2}\.\d{2}\.\d{4}\b', text)
    return {
        'url': url,
        'title': title,
        'place': line(next((piece for piece in pieces if PLACE.search(piece)), '')),
        'rent': rent,
        'coldRent': cold,
        'area': area(number(size.group(1))) if size else None,
        'rooms': rooms(number(count.group(1))) if count else None,
        'availableFrom': available_from(dates[0]) if dates else None,
        'traits': detect_traits(title + '\n' + text),
        'text': clip(text),
        'imageSource': _card_picture(card, base),
    }


def _card_price(text, warm):
    """(warm rent, cold rent) of a card: the first price, warm if it says so or WARM and it does not say cold."""
    price = PRICE.search(text)
    if not price:
        return None, None
    cost, note = money(number(price.group(1))), price.group(2)
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
