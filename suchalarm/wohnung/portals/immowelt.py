"""Immowelt, read in the browser."""

import logging
import re
from urllib.parse import quote, urlsplit

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    detect_traits,
    money,
    number,
    rooms,
)
from .common import attribute, plain, rescue

log = logging.getLogger('suchalarm')

# The old address /suche/<place>/wohnungen/mieten redirects to the place's page, read in Immowelt's own
# order: sorting by date brings up a captcha (sources._immowelt). Price and size are filtered locally.

IMMOWELT = 'https://www.immowelt.de'


def immowelt_place_url(query):
    return f'{IMMOWELT}/suche/{quote(query.strip().casefold())}/wohnungen/mieten'


def immowelt_location(final_url, city):
    """The path of the place's page that the old address redirected to."""
    path = urlsplit(final_url).path
    if not re.fullmatch(r'/suche/mieten/wohnung/[\w-]+/[\w-]+/ad\w+', path, re.I):
        raise RuntimeError(f'Immowelt kennt den Ort „{city}“ nicht.')
    return path


def immowelt_parse(html):
    cards = re.split(r'(?=<div[^>]*data-testid="serp-core-classified-card-testid")', html)[1:]
    if not cards:
        if re.search(r'keine (?:passenden )?(?:Treffer|Ergebnisse|Angebote)|0 Angebote', html, re.I):
            return []
        if listings := rescue('Immowelt', html, r'^(?:https://www\.immowelt\.de)?/expose/[\w-]+$', IMMOWELT):
            return listings
        raise Unreadable('Immowelt-Seite enthält keine erkennbaren Angebote (Layout geändert oder gesperrt).')
    listings = []
    for card in cards:
        link = re.search(r'<a\b[^>]*data-testid="card-mfe-covering-link-testid"[^>]*>', card)
        if not link:
            continue
        href = urlsplit(attribute(link.group(0), 'href'))
        if href.hostname != 'www.immowelt.de' or not re.fullmatch(r'/expose/[\w-]+', href.path):
            continue
        # "Wohnung zur Miete - Erstbezug - Darmstadt - 835 € - 1 Zimmer, 46,6 m², 4. Geschoss, frei ab 16.11.2026"
        summary = ' '.join(attribute(link.group(0), 'title').split())
        price = re.search(r'data-testid="cardmfe-price-testid"[^>]*>(.*?)</span>', card, re.S)
        price_text = plain(price.group(1)) if price else ''
        address = re.search(r'data-testid="cardmfe-description-box-address"[^>]*>(.*?)</div>', card, re.S)
        teaser = re.search(r'data-testid="cardmfe-description-text-test-id"[^>]*>(.*?)</div></div>', card, re.S)
        teaser_text = plain(teaser.group(1)) if teaser else ''
        gallery = card.split('card-mfe-picture-box-gallery-test-id', 1)[-1]
        picture = re.search(r'<img[^>]+src="(https://mms\.immowelt\.de/[^"?]+)', gallery)
        kind = summary.split(' - ')[0] or 'Wohnung zur Miete'
        title = kind + (' · Erstbezug' if ' - Erstbezug - ' in summary else '')
        size = re.search(r'([\d.,]+)\s*m²', summary)
        count = re.search(r'([\d.,]+)\s*Zimmer', summary)
        free = re.search(r'frei ab (.+)$', summary)
        amount = money(number(price_text))
        warm = 'warmmiete' in price_text.casefold()
        listings.append(
            {
                'url': IMMOWELT + href.path,
                'title': title[:300],
                'place': plain(address.group(1))[:300] if address else '',
                'rent': amount if warm else None,
                'coldRent': None if warm else amount,
                'area': area(number(size.group(1)) if size else None),
                'rooms': rooms(number(count.group(1)) if count else None),
                'traits': detect_traits(summary + '\n' + teaser_text),
                'availableFrom': available_from(free.group(1)) if free else None,
                'text': clip(summary + ' ' + teaser_text),
                'imageSource': picture.group(1) + '?w=480&h=360' if picture else None,
            }
        )
    return listings


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
