"""Building blocks of the digest card in the app's look (mailer.py frames the mail, each kind's mail.card fills it).

Mail clients ignore stylesheets, so every style is inline; colours are the light values of DESIGN.md."""

from html import escape

INK, SECONDARY, MUTED, HAIR, FILL, GREEN = '#0a0a0a', '#636369', '#707077', '#ececef', '#f4f4f5', '#09703b'
BUTTON = 'display:inline-block;margin:0 6px 8px 0;border-radius:999px;font-weight:600;text-decoration:none;white-space:nowrap'
TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse"'


def image(url, src, width, fixed=False):
    """The picture, linked to the offer: FIXED keeps WIDTH (a thumbnail), otherwise it shrinks on narrow screens."""
    size = f'width:{width}px' if fixed else 'width:100%'
    return (
        f'<a href="{escape(url)}"><img src="{escape(src)}" width="{width}" alt="" style="display:block;{size};'
        f'max-width:{width}px;height:auto;border:0;border-radius:16px;background:#e9e9ec"></a>'
    )


def photo(url, src, width, colspan=1):
    """The picture as a row of its own above the text."""
    if not src:
        return ''
    span = f' colspan="{colspan}"' if colspan > 1 else ''
    return f'<tr><td{span} style="padding:0 0 16px">{image(url, src, width)}</td></tr>'


def button(url, label, primary=True, small=False):
    """A pill; one leading to a page elsewhere carries ↗ (DESIGN.md, Components). SMALL: the quieter kind
    for what the app does with a hit (merken, ausblenden)."""
    colors = f'background:{INK};color:#ffffff' if primary else f'background:{FILL};color:{INK}'
    size = 'padding:9px 16px;font-size:14px' if small else 'padding:12px 20px;font-size:15px'
    return f'<a href="{escape(url)}" style="{BUTTON};{size};{colors}">{escape(label)}</a>'


def actions(links, detail=False):
    """Merken and Ausblenden from the mail, and with DETAIL the flat's sheet in the app; empty without APP_URL.
    A row of their own, so they fit on a phone."""
    if not links:
        return ''
    pills = [('save', 'Merken'), ('hide', 'Ausblenden'), *([('detail', 'Details')] if detail else [])]
    return (
        '<div style="padding-top:2px">'
        + ''.join(button(links[key], label, False, True) for key, label in pills)
        + '</div>'
    )


def source(label):
    """The provider, as the small green label of a row in the app."""
    return f'<span style="color:{GREEN};font-weight:600">{escape(label)}</span>'


def card(rows):
    """One offer of the digest: ROWS are the card's table rows."""
    return f'<tr><td style="padding:24px 0;border-top:1px solid {HAIR}">\n<table {TABLE}>{rows}\n</table></td></tr>'
