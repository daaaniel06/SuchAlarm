"""Building blocks of the digest card in the app's look (mailer.py frames the mail, each kind's mail.card fills it)."""

from html import escape

BUTTON = 'display:inline-block;margin:0 6px 6px 0;padding:12px 22px;border-radius:999px;font-size:15px;font-weight:600;text-decoration:none'
TABLE = 'role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse"'


def photo(url, src, width, colspan=1):
    """The picture, linked to the offer; WIDTH in pixels (it shrinks on narrow screens)."""
    if not src:
        return ''
    span = f' colspan="{colspan}"' if colspan > 1 else ''
    size = 'width:100%;max-width:{0}px' if width > 200 else 'width:{0}px;max-width:100%'
    return (
        f'<tr><td{span} style="padding:0 0 14px"><a href="{escape(url)}"><img src="{escape(src)}" width="{width}" alt="" '
        f'style="display:block;{size.format(width)};height:auto;border:0;border-radius:16px;background:#e9e9ec"></a></td></tr>'
    )


def button(url, label, primary=True):
    colors = 'background:#0a0a0a;color:#ffffff' if primary else 'background:#f4f4f5;color:#0a0a0a'
    return f'<a href="{escape(url)}" style="{BUTTON};{colors}">{escape(label)}</a>'


def card(rows):
    """One offer of the digest: ROWS are the card's table rows."""
    return f'<tr><td style="padding:24px 0;border-bottom:1px solid #ececef">\n<table {TABLE}>{rows}\n</table></td></tr>'
