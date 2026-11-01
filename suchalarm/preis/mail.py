"""How a product offer reads in the digest mail (mailer.py builds the mail around it)."""

from html import escape

from .. import mailhtml
from ..fmt import amount
from .domain import SOURCES, compared_price, search_fee, with_shipping

WORDS = {
    'one': 'neues Angebot',
    'many': 'neue Angebote',
    'tagline': 'unter deinem Höchstpreis',
    'button': 'Angebot öffnen ↗',
    'footer': 'Preise und Verfügbarkeit beim Anbieter prüfen.',
    'footer_plain': 'Preise und Verfügbarkeit beim Anbieter prüfen.',
}


def price(search, offer):
    """The compared price; with unknown shipping the item price, labelled "Versand unbekannt"."""
    value = compared_price(
        offer['price'],
        offer['shipping'],
        with_shipping(offer['source'], search),
        search_fee(offer['source'], offer['price'], search),
    )
    return offer['price'] if value is None else value


def price_text(search, offer):
    """0 only ever comes from a "Zu verschenken" listing."""
    value = price(search, offer)
    return 'Zu verschenken' if value == 0 else amount(value)


def note(search, offer):
    """The shipping under the price."""
    if offer['shipping'] is None:
        return 'Versand unbekannt'
    if offer['source'] == 'vinted':
        return f'Versand ab {amount(offer["shipping"])}'
    if offer['shipping'] == 0:
        return 'Versand frei'
    sign = 'inkl.' if with_shipping(offer['source'], search) else '+'
    return f'{sign} {amount(offer["shipping"])} Versand'


def facts(offer):
    return [part for part in (offer['seller'], offer['condition'], SOURCES[offer['source']].split(' · ')[0]) if part]


def price_range(search):
    if search.get('min_price'):
        return f'{amount(search["min_price"])} bis unter {amount(search["max_price"])}'
    return f'unter {amount(search["max_price"])}'


def reason(offer):
    compare = offer.get('compare')
    if not compare:
        return ''
    return f'Neu im Shop {amount(compare["saving"])} günstiger als das günstigste Gebrauchtangebot ({amount(compare["used"])})'


def subject(entries, title):
    if len(entries) > 1:
        return f'Preisalarm: {len(entries)} neue Angebote'
    value = price(*entries[0])
    return f'Preisalarm: {title} ' + ('zu verschenken' if value == 0 else f'für {amount(value)}')


def card(search, offer, photo_src, links):
    """A row like in the app: picture, title and price, provider and facts, why it fits, the search, buttons."""
    why = reason(offer)
    why_row = (
        f'<tr><td colspan="2" style="padding-top:8px;font-size:13px;font-weight:600;color:{mailhtml.GREEN}">{escape(why)}</td></tr>'
        if why
        else ''
    )
    provider = SOURCES[offer['source']].split(' · ')[0]  # first and green, as in the app's row foot
    foot = ' · '.join([mailhtml.source(provider), *(escape(part) for part in facts(offer)[:-1])])
    text = f"""<table {mailhtml.TABLE}><tr>
<td style="vertical-align:top;font-size:15px;font-weight:600;line-height:1.35;letter-spacing:-0.01em;color:{mailhtml.INK}">{escape(offer['title'])}</td>
<td align="right" style="vertical-align:top;white-space:nowrap;padding-left:12px;font-size:20px;font-weight:700;letter-spacing:-0.03em;color:{mailhtml.INK}">{escape(price_text(search, offer))}</td>
</tr><tr>
<td style="padding-top:6px;font-size:13px;color:{mailhtml.MUTED}">{foot}</td>
<td align="right" style="padding-top:6px;padding-left:12px;white-space:nowrap;font-size:13px;color:{mailhtml.MUTED}">{escape(note(search, offer))}</td>
</tr></table>"""
    head = (
        f'<td width="72" style="vertical-align:top;width:72px;padding-right:14px">{mailhtml.image(offer["url"], photo_src, 72, fixed=True)}</td><td style="vertical-align:top">{text}</td>'
        if photo_src
        else f'<td colspan="2">{text}</td>'
    )
    return mailhtml.card(f"""<tr>{head}</tr>{why_row}
<tr><td colspan="2" style="padding-top:10px;font-size:13px;color:{mailhtml.SECONDARY}">{escape(search['query'])} · {escape(price_range(search))}</td></tr>
<tr><td colspan="2" style="padding-top:16px">{mailhtml.button(offer['url'], WORDS['button'])}{mailhtml.actions(links)}</td></tr>""")
