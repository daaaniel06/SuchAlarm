"""How a flat reads in the digest mail (mailer.py builds the mail around it)."""

from datetime import date
from html import escape

from .. import mailhtml
from ..fmt import amount, number
from .domain import SOURCES

WORDS = {
    'one': 'neue Wohnung',
    'many': 'neue Wohnungen',
    'tagline': 'passend zu deiner Wohnungssuche',
    'button': 'Inserat öffnen',
    'footer': 'Miete, Lage und Verfügbarkeit im Inserat prüfen; eine geschätzte Warmmiete ist Kaltmiete plus etwa 3 € je m².',
    'footer_plain': 'Miete, Lage und Verfügbarkeit im Inserat prüfen.',
}


def price(search, offer):
    """The warm rent in cents (estimated or stated); 0 when not known."""
    return offer['price'] or 0


def price_text(search, offer):
    """A flat without a known rent says so: never "Zu verschenken"."""
    value = price(search, offer)
    return amount(value) if value else 'Miete offen'


def note(search, offer):
    if not offer.get('price'):
        return 'Miete im Inserat'
    return 'Warmmiete geschätzt' if offer.get('estimated') else 'Warmmiete'


def details(offer):
    """Place, size, rooms and move-in: the line a flat keeps on the watch list too."""
    day = offer.get('available_from')
    return [
        part
        for part in (
            offer.get('place'),
            f'{number(offer["area"])} m²' if offer.get('area') else '',
            f'{number(offer["rooms"])} Zi.' if offer.get('rooms') else '',
            f'frei ab {day[8:10]}.{day[5:7]}.{day[:4]}' if day else '',
        )
        if part
    ]


def facts(offer):
    return [*details(offer), SOURCES.get(offer['source'], '')]


def price_range(search):
    return f'bis {amount(search["max_price"])} warm'


def reason(offer):
    return ''


def subject(entries, title):
    if len(entries) > 1:
        return f'Wohnungen: {len(entries)} neue'
    value = price(*entries[0])
    return f'Wohnung: {title} für {amount(value)} warm' if value else f'Wohnung: {title}'


def place_name(offer):
    """Like the app: district or town large, street and postcode small."""
    raw = (offer.get('place') or '').replace('(unvollständige Adresse)', '')
    parts = [part.strip() for part in raw.split(',') if part.strip()]
    if len(parts) >= 3:
        return parts[-1], ', '.join(parts[:-1])
    if parts and parts[-1][:5].isdigit() and ' ' in parts[-1]:
        code, town = parts[-1].split(' ', 1)
        return town, ', '.join([*parts[:-1], code])
    if len(parts) == 2:
        return parts[1], parts[0]
    return (parts[0] if parts else 'Ort offen'), ''


def _card_facts(offer):
    """Size, rooms, move-in and portal; the place stands above."""
    day = offer.get('available_from')
    free = (
        ('sofort frei' if date.fromisoformat(day) <= date.today() else f'frei ab {day[8:10]}.{day[5:7]}.{day[:4]}')
        if day
        else ''
    )
    return [
        part
        for part in (
            f'{number(offer["area"])} m²' if offer.get('area') else '',
            f'{number(offer["rooms"])} Zi.' if offer.get('rooms') else '',
            free,
            SOURCES.get(offer['source'], ''),
        )
        if part
    ]


def _buttons(offer):
    """One button per portal when the same flat is on several: the reader picks where to go."""
    twins = offer.get('twins') or []
    if not twins:
        return mailhtml.button(offer['url'], WORDS['button'])
    return ''.join(
        mailhtml.button(x['url'], f'Bei {SOURCES.get(x["source"], x["source"])} öffnen', primary=index == 0)
        for index, x in enumerate([offer, *twins])
    )


def card(search, offer, photo_src):
    """The Wohnungsbot card: wide photo, district and rent, address, title, facts, a button per portal."""
    name, address = place_name(offer)
    estimated = bool(offer.get('price') and offer.get('estimated'))
    rent = f'≈ {price_text(search, offer)}' if estimated else price_text(search, offer)
    label = 'Warmmiete geschätzt' if estimated else 'warm' if offer.get('price') else ''
    return mailhtml.card(f"""{mailhtml.photo(offer['url'], photo_src, 520)}
<tr><td><table {mailhtml.TABLE}><tr>
<td style="vertical-align:top;font-size:19px;font-weight:700;letter-spacing:-0.02em;color:#0a0a0a">{escape(name)}</td>
<td align="right" style="vertical-align:top;white-space:nowrap;padding-left:12px;font-size:22px;font-weight:700;letter-spacing:-0.03em;color:{'#636369' if estimated else '#0a0a0a'}">{escape(rent)}</td>
</tr><tr>
<td style="padding-top:2px;font-size:13px;color:#707077">{escape(address)}</td>
<td align="right" style="padding-top:2px;white-space:nowrap;font-size:13px;color:#707077">{label}</td>
</tr></table></td></tr>
<tr><td style="padding-top:10px;font-size:14px;line-height:1.45;color:#636369">{escape(offer.get('title') or '')}</td></tr>
<tr><td style="padding-top:6px;font-size:13px;font-weight:600;color:#0a0a0a">{escape(' · '.join(_card_facts(offer)))}</td></tr>
<tr><td style="padding-top:16px">{_buttons(offer)}</td></tr>""")
