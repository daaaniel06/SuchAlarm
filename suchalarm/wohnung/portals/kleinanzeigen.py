"""Kleinanzeigen flats, read in the browser."""

import logging
import re
from html import unescape
from urllib.parse import quote, urlencode

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    detect_traits,
    is_wanted_ad,
    money,
    number,
    rooms,
)
from .common import plain, rescue

log = logging.getLogger('suchalarm')


KLEINANZEIGEN = 'https://www.kleinanzeigen.de'
PRICE_CLASS = r'price|text-title3'  # the card's price line, in the older and the newer layout
PAGE_SIZE = 100  # the most one page holds (25 without it; checked live on 31.10.2026)


def kleinanzeigen_locations_url(query):
    return KLEINANZEIGEN + '/s-ort-empfehlungen.json?' + urlencode({'query': query})


def kleinanzeigen_location(result, city):
    ids = [key.lstrip('_') for key in (result or {}) if key != '_0' and key.lstrip('_').isdigit()]
    if not ids:
        raise RuntimeError(f'Kleinanzeigen kennt den Ort „{city}“ nicht.')
    return ids[0]


def kleinanzeigen_url(profile, location_id, page=1):
    radius = f'r{int(profile["radius"])}' if profile.get('radius') else ''
    filters = ''
    if profile.get('minArea'):
        filters += f'+wohnung_mieten.qm_d:{profile["minArea"]:g},'
    if profile.get('minRooms'):
        filters += f'+wohnung_mieten.zimmer_d:{profile["minRooms"]:g},'
    # No swap filter: swap_s:nein also drops every ad that leaves the field empty, most of them.
    # The price filter is the cold rent; a warm-rent limit is a safe upper bound for it.
    # "anzeige:angebote" leaves out requests from flat seekers.
    paging = f'seite:{page}/' if page > 1 else ''
    path = (
        f'/s-wohnung-mieten/anzeige:angebote/sortierung:neuste/{paging}preis::{int(profile["maxRent"])}'
        f'/c203l{location_id}{radius}{filters}'
    )
    return KLEINANZEIGEN + quote(path, safe='/:+') + f'?pageSize={PAGE_SIZE}'


def kleinanzeigen_parse(html):
    articles = re.findall(r'<article\b[^>]*data-adid="(\d+)"[^>]*data-href="([^"]+)"[^>]*>(.*?)</article>', html, re.S)
    if not articles:
        if re.search(r'keine (?:ergebnisse|treffer|anzeigen)|0 Ergebnisse', html, re.I):
            return []
        link = r'(?:^|kleinanzeigen\.de)/s-anzeige/.+/\d+-\d+-\d+'
        if listings := rescue('Kleinanzeigen', html, link, KLEINANZEIGEN, skip_wanted=True):
            return listings
        if 'srchrslt-content' in html:
            return []
        raise Unreadable('Kleinanzeigen-Seite enthält keine erkennbaren Anzeigen (Layout geändert oder gesperrt).')
    listings = []
    for _ad_id, href, body in articles:
        if '/s-anzeige/' not in href:
            continue
        title_match = re.search(r'<h[23][^>]*>(.*?)</h[23]>', body, re.S)
        title = plain(title_match.group(1)) if title_match else 'Wohnung'
        if is_wanted_ad(title):
            continue
        place_match = re.search(r'<span>(\d{5}\s[^<]+)</span>', body)
        # The card's teaser (start of the description) feeds the exclusion words for free.
        teaser_match = re.search(rf'</h[23]>\s*<p(?![^>]*(?:{PRICE_CLASS}))[^>]*>(.*?)</p>', body, re.S)
        teaser = plain(teaser_match.group(1)) if teaser_match else ''
        facts = _facts(body, title_match, teaser_match)
        price_match = re.search(rf'<p[^>]*class="[^"]*(?:{PRICE_CLASS})[^"]*"[^>]*>(.*?)</p>', body, re.S)
        price = re.search(r'([\d.]+(?:,\d+)?)\s*€', plain(price_match.group(1))) if price_match else None
        size = re.search(r'([\d.,]+)\s*m²', facts)
        count = re.search(r'([\d.,]+)\s*Zi\.', facts)
        picture = re.search(r'<img[^>]+src="(https://img\.kleinanzeigen\.de/[^"]+)"', body)
        listings.append(
            {
                'url': KLEINANZEIGEN + unescape(href),
                'title': title[:300] or 'Wohnung',
                'place': plain(place_match.group(1))[:300] if place_match else '',
                'coldRent': money(number(price.group(1)) if price else None),
                'area': area(number(size.group(1)) if size else None),
                'rooms': rooms(number(count.group(1)) if count else None),
                'traits': detect_traits(title + '\n' + teaser),
                'text': clip(title + ' ' + teaser),
                # rule $_72 is 500x375, the size closest to the ImmoScout24 image.
                'imageSource': re.sub(r'rule=[^&]+', 'rule=$_72.AUTO', unescape(picture.group(1))) if picture else None,
            }
        )
    return listings


def _facts(body, *parts):
    """The card's text without title and teaser: amounts the seller writes there (deposit, parking, a garden's
    m²) are no facts of the flat."""
    for part in sorted((part for part in parts if part), key=lambda part: part.start(), reverse=True):
        body = body[: part.start()] + ' ' + body[part.end() :]
    return plain(re.sub(r'<script.*?</script>', ' ', body, flags=re.S))


def kleinanzeigen_detail(html):
    details = {
        plain(label): plain(value)
        for label, value in re.findall(
            r'<li class="addetailslist--detail">(.*?)<span class="addetailslist--detail--value"\s*>(.*?)</span>',
            html,
            re.S,
        )
    }
    if not details and 'viewad-price' not in html:
        raise RuntimeError('Kleinanzeigen-Anzeige nicht lesbar (gelöscht oder Layout geändert).')
    price = re.search(r'id="viewad-price"[^>]*>(.*?)</h2>', html, re.S)
    description = re.search(r'id="viewad-description-text"[^>]*>(.*?)</p>', html, re.S)
    title = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    text = (unescape(title.group(1)) if title else '') + '\n' + (plain(description.group(1)) if description else '')
    cold = number(plain(price.group(1))) if price else None

    def label(name):
        return next((value for key, value in details.items() if key.casefold().startswith(name.casefold())), None)

    # The page's own tracking data carries the same field, should the list change.
    tracked = re.search(r'"Warmmiete"\s*:\s*"([\d.,]+)"', html)
    warm = number(label('Warmmiete')) or (number(tracked.group(1)) if tracked else None)
    if warm is None and cold is not None and label('Nebenkosten'):
        # Private ads often list cold rent plus costs instead of a warm total.
        warm = cold + (number(label('Nebenkosten')) or 0) + (number(label('Heizkosten')) or 0)
    return {
        'rent': money(warm),
        'coldRent': money(cold),
        'area': area(number(label('Wohnfläche'))),
        'rooms': rooms(number(details.get('Zimmer'))),
        'traits': detect_traits(text),
        'availableFrom': available_from(label('Verfügbar ab')),
        'text': clip(text),
    }
