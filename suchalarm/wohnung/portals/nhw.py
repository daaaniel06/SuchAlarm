"""NHW (Nassauische Heimstätte), one HTML page for all of Hesse."""

import logging
import re

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    coordinate_pair,
    detect_traits,
    money,
    number,
    rooms,
)
from .common import plain, rescue

log = logging.getLogger('suchalarm')

# One page lists every free flat in Hesse. Each card carries warm and cold rent, size,
# rooms and town in hidden filter fields; the page's map script adds the coordinates.
# The detail page only adds the description and the move-in date.

NHW = 'https://www.nhw.de'
NHW_LIST = f'{NHW}/wohnungsangebote'


def nhw_url(identifier):
    return f'{NHW}/zuhause-finden/immobilie/{identifier}'


def nhw_parse(html):
    spots = {}
    for chunk in html.split('uid: ')[1:]:
        match = re.match(
            r"(\d+),.*?position: \{ lat: parseFloat\('([\d.]+)'\), lng: parseFloat\('([\d.]+)'\)", chunk, re.S
        )
        if match:
            spots[match.group(1)] = coordinate_pair(match.group(2), match.group(3))
    cards = re.split(r'(?=<div class="immo--item" data-itemuid=")', html)[1:]
    if not cards:
        if listings := rescue('NHW', html, r'(?:^|nhw\.de)/zuhause-finden/immobilie/[\w-]+', NHW):
            return listings
        if 'immo--list' in html:
            return []
        raise Unreadable('NHW-Seite enthält keine erkennbaren Angebote (Layout geändert oder gesperrt).')
    listings = []
    for card in cards:
        uid = re.match(r'<div class="immo--item" data-itemuid="(\d+)"', card)
        link = re.search(
            r'immo--item--title[^>]*><a href="/zuhause-finden/immobilie/([\w-]+)[^"]*">(.*?)</a>', card, re.S
        )
        if not uid or not link:
            continue
        facts = dict(re.findall(r'data-filtertype="(\w+)" data-filtervalue="([^"]*)"', card))
        title = plain(link.group(2))[:300] or 'Wohnung'  # a landlord: "… sucht Mieter" is an offer, never a wanted ad
        image = re.search(r'<img class="immo--item--image"[^>]*src="(/fileadmin/[^"]+)"', card)
        location = re.search(r'immo--item--location[^>]*>(.*?)</em>', card, re.S)
        listings.append(
            {
                'url': nhw_url(link.group(1)),
                'title': title,
                'place': plain(location.group(1)).strip(' ,')[:300] if location else '',
                'rent': money(number(facts.get('warmmiete', '').replace('.', ','))),
                'coldRent': money(number(facts.get('nettokaltmiete', '').replace('.', ','))),
                'area': area(number(facts.get('wohnflaeche', '').replace('.', ','))),
                'rooms': rooms(number(facts.get('anzahl_zimmer', '').replace('.', ','))),
                'traits': detect_traits(title + (' WBS' if facts.get('wbs') == '1' else '')),
                'imageSource': NHW + image.group(1) if image else None,
                **spots.get(uid.group(1), {}),
            }
        )
    return listings


def nhw_detail(html):
    rows = {
        plain(label): plain(value)
        for label, value in re.findall(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>', html, re.S)
    }
    if 'Nettokaltmiete' not in rows:
        raise RuntimeError('NHW-Angebot nicht lesbar (vermietet oder Layout geändert).')
    title = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    body = re.search(r'<div class="immo--text">(.*?)(?:<[^>]*id="request"|$)', html, re.S)
    text = (
        (plain(title.group(1)) if title else '')
        + '\n'
        + (plain(re.sub(r'<script.*?</script>', ' ', body.group(1), flags=re.S)) if body else '')
    )
    return {
        'rent': money(number(rows.get('Gesamtmiete (brutto)'))),
        'coldRent': money(number(rows.get('Nettokaltmiete'))),
        'area': area(number(rows.get('Wohnfläche'))),
        'rooms': rooms(number(rows.get('Zimmer'))),
        'traits': detect_traits(text),
        'availableFrom': available_from(rows.get('Verfügbar ab')),
        'text': clip(text),
    }
