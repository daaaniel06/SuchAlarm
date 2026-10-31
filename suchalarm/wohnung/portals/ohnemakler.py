"""ohne-makler.net (HTML, private landlords only)."""

import math
import re
import unicodedata
from urllib.parse import quote, urlencode

from ...core.browser import Unreadable
from ..domain import area, available_from, clip, detect_traits, line, money, number, rooms
from .common import attribute, plain

OHNE_MAKLER = 'https://www.ohne-makler.net'
OM_LIST = f'{OHNE_MAKLER}/immobilien/wohnung-mieten/'
OM_RADII = (10, 25, 50, 100)  # the steps of the site's radius field, in km

# A town's list sits at /immobilien/wohnung-mieten/<state>/<town>/, both named the way the site spells
# them: "Frankfurt (Main)" in Hessen is .../hessen/frankfurt-main/. A postcode needs no path (?zip=), but
# the site ignores the radius there. Without a radius a town's own offers come first; what follows
# id="similar-listings" lies outside the search (other towns, other prices) and is never read.


def om_places_url(town):
    return f'{OHNE_MAKLER}/geodb/cities/find/?' + urlencode({'term': town, 'format': 'objects', 'iso2': 'DE'})


def om_town(answer, town):
    """The site's name of TOWN among its place suggestions, or None. "Frankfurt am Main" is
    "Frankfurt (Main)" there: names are compared without brackets and filler words."""
    names = [entry['name'] for entry in answer or [] if entry.get('model') == 'CITY' and entry.get('name')]
    wanted = _bare(town)
    return next((name for name in names if _bare(name) == wanted), names[0] if names else None)


def _bare(name):
    words = re.sub(r'[()]', ' ', name.casefold()).split()
    return ' '.join(word for word in words if word not in ('am', 'an', 'der', 'die', 'dem', 'vor', 'im', 'in'))


def om_slug(name):
    """ "Bad Homburg (Höhe)" -> "bad-homburg-hohe": umlauts lose their dots, brackets go."""
    ascii_name = unicodedata.normalize('NFKD', name.casefold().replace('ß', 'ss')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', ascii_name).strip('-')


def om_url(place, profile, page=1):
    """The newest offers of PLACE (a town's path, or a postcode) within the search's rent and area.
    price_max limits the cold rent, so the warm limit is a safe bound; rooms the site cannot filter."""
    limits = {'price_max': math.ceil(profile['maxRent']), 'area_min': math.floor(profile['minArea'])}
    query = {'sort': 'date_desc', **{name: value for name, value in limits.items() if value}}
    if re.fullmatch(r'\d{5}', place):
        address, query = OM_LIST, {'zip': place, **query}
    else:
        address = f'{OHNE_MAKLER}{quote(place)}'
        radius = next((step for step in OM_RADII if step >= profile['radius']), OM_RADII[-1])
        query['radius'] = radius if profile['radius'] else 0
    if page > 1:
        query['page'] = page
    return f'{address}?{urlencode(query)}'


def om_path(state, town):
    return f'/immobilien/wohnung-mieten/{om_slug(state)}/{om_slug(town)}/'


_COUNT = re.compile(r'<p class="md:text-lg[^"]*empty:hidden">(.*?)</p>', re.S)
_CARD = re.compile(r'(?=<div\b[^>]*\bid="bookmark_\d+")')


def om_parse(html):
    """The offers of a result page: only the cards before the site's "similar offers" block. An empty
    count line means no offer matched; the cards shown then are only similar ones."""
    count = _COUNT.search(html)
    if count is None:
        raise Unreadable('ohne-makler-Seite enthält keine erkennbaren Angebote (Layout geändert).')
    if not plain(count.group(1)):
        return []
    found = html.split('id="similar-listings"', 1)[0]
    return [card for part in _CARD.split(found)[1:] if (card := _card(part))]


def _card(part):
    link = re.search(r'<a\b[^>]*href="(/immobilie/\d+/)"', part)
    if not link:
        return None
    title = re.search(r'<h4\b[^>]*>(.*?)</h4>', part, re.S)
    price = re.search(r'<span class="block font-semibold[^"]*">(.*?)</span>', part, re.S)
    place = re.search(r'</h4>.*?<span>(.*?)</span>', part, re.S)
    picture = re.search(r'<img\b[^>]*>', part)

    def fact(name):  # <div class="flex items-center" title="Zimmer"> <i>…</i> <span …> 2 </span>
        found = re.search(rf'title="{name}">.*?<span\b[^>]*>(.*?)</span>', part, re.S)
        return number(plain(found.group(1))) if found else None

    heading = plain(title.group(1)) if title else ''
    source = attribute(picture.group(0), 'src') if picture else ''
    return {
        'url': OHNE_MAKLER + link.group(1),
        'title': line(heading or 'Wohnung'),
        'place': line(plain(place.group(1))) if place else '',
        'coldRent': money(number(plain(price.group(1)))) if price else None,
        'area': area(fact('Wohnfläche')),
        'rooms': rooms(fact('Zimmer')),
        'traits': detect_traits(heading),
        'text': clip(heading),
        'imageSource': source or None,
    }


def om_detail(html):
    """An offer's page. Its rent is the cold rent; the warm rent only follows from the sum of running
    and heating costs, which many offers leave out."""
    heading = re.search(r'<h1\b[^>]*>(.*?)</h1>', html, re.S)
    if heading is None:
        raise Unreadable('ohne-makler-Angebot nicht lesbar (gelöscht oder Layout geändert).')
    address = re.search(r'<a href="#ort"[^>]*>.*?<span\b[^>]*>(.*?)</span>', html, re.S)
    rows = {
        plain(label): plain(value)
        for label, value in re.findall(r'<tr\b[^>]*>\s*<td\b[^>]*>(.*?)</td>\s*<td\b[^>]*>(.*?)</td>', html, re.S)
    }
    cold, costs = number(rows.get('Kaltmiete')), number(rows.get('Summe Nebenkosten/Heizkosten'))
    text = '\n'.join(plain(part) for part in re.findall(r'<div class="prose\b[^"]*">(.*?)</div>', html, re.S))

    def headline(label):  # <span class="block text-lg …">2</span> </div> <span …>Zimmer</span>
        found = re.search(
            rf'<span class="block text-lg[^"]*">([^<]*)</span>\s*</div>\s*<span[^>]*>{label}</span>', html
        )
        return number(found.group(1)) if found else None

    return {
        'rent': money(cold + costs) if None not in (cold, costs) else None,
        'coldRent': money(cold),
        'area': area(headline('Wohnfläche')),
        'rooms': rooms(headline('Zimmer')),
        # "Kirchstraße 21, 64283 Darmstadt – Hessen"
        'place': line(re.split(r'\s+[–-]\s+', plain(address.group(1)))[0]) if address else None,
        'traits': detect_traits(text),
        'availableFrom': available_from(rows.get('Übernahme ab')),
        'text': clip(text),
    }
