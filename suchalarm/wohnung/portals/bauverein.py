"""bauverein AG Darmstadt through its polyEstate map widget (JSON)."""

import json
import logging
import re
from urllib.parse import quote

from ...core.browser import Unreadable
from ..domain import (
    area,
    available_from,
    clip,
    coordinate_pair,
    detect_traits,
    line,
    money,
    number,
    rooms,
)

log = logging.getLogger('suchalarm')


# bauvereinag.de/kunde-werden/mietwohnungen-suchen embeds this widget with a
# public key; one request returns the landlord's complete rental list (10–30 flats).

BAUVEREIN_CLIENT = 845981
BAUVEREIN_KEY = 'iusngpsiugnpiubnjmpynioxpgo8i4ztp987zps9etmhsz7nt4o87enDOEUZGFOZB'
POLYESTATE = 'https://maps.polyestate.de/'
BAUVEREIN_WIDGET = f'{POLYESTATE}widget/latest/wrapper/?clientId={BAUVEREIN_CLIENT}&apiKey={BAUVEREIN_KEY}'
BAUVEREIN_HEADERS = {'Origin': 'https://maps.polyestate.de', 'Content-Type': 'text/plain;charset=UTF-8'}
BAUVEREIN_MARKERS = {
    'orderHeuristic': 'SORT_DESC',
    'orderField': 'id',
    'mieten': 1,
    'kaufen': 0,
    'wohnen': 1,
    'parken': 0,
    'gewerbe': 0,
    'balkon': 0,
    'garten': 0,
    'kautionsfrei': 0,
    'fahrstuhl_personen': 0,
    'unterkellert_keller': 0,
    'stellplatz': 0,
    'wbs': 0,
    'zimmerMin': 0,
    'preisMax': 10000,
}
DARMSTADT = (49.8728, 8.6512)
BAUVEREIN_REACH = 25  # km from Darmstadt's centre that the bauverein's stock covers


def polyestate(endpoint, body):
    """(url, payload) of one request to the widget's API."""
    payload = {'anbieterId': BAUVEREIN_CLIENT, 'apiKey': BAUVEREIN_KEY, 'requestSource': 'MAPS', **body}
    return f'{POLYESTATE}web/index.php/real-estate/{endpoint}', json.dumps(payload).encode()


def bauverein_url(reference, identifier):
    """Opens the offer in the landlord's widget: title, prices and "Direkt anfragen"."""
    return f'{BAUVEREIN_WIDGET}#/profile/{BAUVEREIN_CLIENT}__{reference.replace("/", "~")}/{identifier}'


def bauverein_parse(data):
    """The landlord's whole stock of flats; the place is checked per search (near)."""
    objects = data.get('objekte') if isinstance(data, dict) else None
    if not isinstance(objects, list):
        raise Unreadable('bauverein-Liste nicht lesbar (Schnittstelle geändert).')
    listings = []
    for item in objects:
        if (
            not isinstance(item, dict)
            or item.get('nutzungsart') != 'WOHNEN'
            or item.get('vermarktungsart') != 'MIETE_PACHT'
        ):
            continue
        picture = str((item.get('titelbild') or {}).get('pfad') or '')
        address = line(item.get('adresse'))
        district = line(item.get('regionaler_zusatz'))
        listings.append(
            {
                # The widget links by reference number; the picture name starts with it. Without a
                # picture the caller asks for it (bauverein_reference) and fills in the URL.
                'url': bauverein_url(re.sub(r'-\d+\.\w+$', '', picture), item['id']) if picture else None,
                'bauvereinId': item['id'],
                'title': line(item.get('objekttitel')) or 'Wohnung',
                'place': ', '.join(part for part in (address, district) if part)[:300],
                'rent': money(number(str(item.get('gesamtpreis') or ''))),
                'area': area(number(str(item.get('wohnflaeche') or ''))),
                'rooms': rooms(number(str(item.get('anzahl_zimmer') or ''))),
                'traits': detect_traits(str(item.get('objekttitel') or '')),
                'availableFrom': available_from(item.get('verfuegbar_ab')),
                'imageSource': f'{POLYESTATE}web/media/{int(item.get("anbieterId") or 1)}/{quote(picture)}'
                if picture
                else None,
                **coordinate_pair(item.get('lat'), item.get('lon')),
            }
        )
    return listings


def _bauverein_object(data):
    try:
        return data['openimmo']['anbieter'][0]['immobilie'][0]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError('bauverein-Angebot nicht lesbar (vermietet oder Schnittstelle geändert).') from None


def bauverein_reference(data):
    """The reference number from an object-details answer."""
    reference = (_bauverein_object(data).get('verwaltung_techn') or {}).get('objektnr_extern')
    if not reference:
        raise RuntimeError('bauverein-Angebot ohne Referenznummer.')
    return str(reference)


def bauverein_identifier(url):
    return int(url.rsplit('/', 1)[-1])


def bauverein_detail(data):
    offer = _bauverein_object(data)
    prices, areas, texts = offer.get('preise') or {}, offer.get('flaechen') or {}, offer.get('freitexte') or {}
    text = '\n'.join(
        str(texts.get(key) or '')
        for key in ('objekttitel', 'objektbeschreibung', 'ausstatt_beschr', 'sonstige_angaben')
    )
    cold = number(str(prices.get('nettokaltmiete') or prices.get('kaltmiete') or ''))
    warm = number(str(prices.get('warmmiete') or ''))
    if warm is None:
        # "Gesamtmiete" is cold rent plus service charges; heating comes on top unless it is included.
        total = number(str(prices.get('gesamtmiete') or prices.get('gesamtpreis') or ''))
        included = str(prices.get('heizkosten_enthalten')).casefold() in ('true', '1')
        heating = 0 if included else number(str(prices.get('heizkosten') or '')) or 0
        warm = None if total is None else total + heating
    return {
        'rent': money(warm),
        'coldRent': money(cold),
        'area': area(number(str(areas.get('wohnflaeche') or ''))),
        'rooms': rooms(number(str(areas.get('anzahl_zimmer') or ''))),
        'traits': detect_traits(text),
        'availableFrom': available_from((offer.get('verwaltung_objekt') or {}).get('verfuegbar_ab')),
        'text': clip(text),
    }
