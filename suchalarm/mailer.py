"""The digest mail: plain text plus an HTML version in the app's look, sent over SMTP.

Pictures travel inside the mail from the local cache (cid), so they stay visible after the
cache or the provider drops them; an offer without a cached picture simply has none.
"""

import io
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from html import escape
from pathlib import Path

from PIL import Image

from . import kinds, mailhtml, maillinks, store

log = logging.getLogger('suchalarm')


class DeliveryError(Exception):
    """A short German reason, never secrets. maybe_sent: the server may have accepted it; never resend."""

    def __init__(self, message, maybe_sent):
        super().__init__(message)
        self.maybe_sent = maybe_sent


@dataclass(frozen=True)
class Server:
    """Where mails go out: from SMTP_* in compose.yml, which wins, or else from the settings page."""

    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str  # the From header
    fixed: bool  # set in compose.yml; the page shows it but cannot change it

    @property
    def address(self):
        return parseaddr(self.sender)[1]


def security_for(port):
    """Port 465 speaks TLS from the start; every other port upgrades with STARTTLS."""
    return 'ssl' if port == 465 else 'starttls'


def server():
    env = os.environ
    if env.get('SMTP_HOST'):
        return Server(
            host=env['SMTP_HOST'],
            port=int(env.get('SMTP_PORT', '587')),
            security=env.get('SMTP_SECURITY', 'starttls'),
            user=env.get('SMTP_USER', ''),
            password=env.get('SMTP_PASSWORD', ''),
            sender=env.get('SMTP_FROM', ''),
            fixed=True,
        )
    saved = store.mail_server()
    return Server(
        host=saved['host'],
        port=saved['port'],
        security=security_for(saved['port']),
        user=saved['sender'],  # the mailbox logs in with its own address
        password=saved['password'],
        sender=formataddr(('Suchalarm', saved['sender'])) if saved['sender'] else '',
        fixed=False,
    )


def configured(current=None):
    current = current or server()
    return bool(current.host and current.sender)


UNREACHABLE = 'Mailserver nicht erreichbar – Host und Port prüfen.'
SMTP_TIMEOUT = 25
# Definite refusals before anything was accepted, checked in this order (subclasses first).
REFUSALS = (
    (smtplib.SMTPAuthenticationError, 'Anmeldung beim Mailserver abgelehnt – Benutzer oder Passwort prüfen.'),
    (smtplib.SMTPRecipientsRefused, 'Der Mailserver lehnt die Empfängeradresse ab.'),
    (smtplib.SMTPSenderRefused, 'Der Mailserver lehnt den Absender ab.'),
)


def _usable_server():
    current = server()
    if not configured(current):
        raise DeliveryError('Der Mailserver ist noch nicht eingerichtet (Einstellungen → Mails).', False)
    if current.security not in ('ssl', 'starttls'):
        raise DeliveryError('SMTP_SECURITY muss ssl oder starttls sein.', False)
    return current


def _explain(error, sending):
    """A DeliveryError for any failure; once the message was being handed over it may have arrived."""
    for kind, text in REFUSALS:
        if isinstance(error, kind):
            return DeliveryError(text, False)
    if isinstance(error, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected):
        return DeliveryError(UNREACHABLE, sending)
    if isinstance(error, smtplib.SMTPResponseException):
        # A definite refusal (size limit, spam filter, rate limit): nothing was accepted.
        return DeliveryError(f'Mailserver lehnt die Nachricht ab (Code {error.smtp_code}).', False)
    if isinstance(error, OSError):
        return DeliveryError(UNREACHABLE, sending)
    return DeliveryError('Versand fehlgeschlagen.', sending)


def deliver(message):
    """Send over SMTP. Returns the recipients the server refused while it took the others, each with its SMTP
    code (address -> code, sorted). Raises DeliveryError with a short German reason, never secrets."""
    current = _usable_server()
    del message['From']  # the mailbox that sends it, whatever the server is set to now
    message['From'] = current.sender
    context = ssl.create_default_context()
    sending = False
    client: smtplib.SMTP
    try:
        if current.security == 'ssl':
            client = smtplib.SMTP_SSL(current.host, current.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(current.host, current.port, timeout=SMTP_TIMEOUT)
        with client:
            if current.security == 'starttls':
                client.starttls(context=context)
            if current.user:
                client.login(current.user, current.password)
            sending = True
            refused = client.send_message(message)
    except Exception as error:
        raise _explain(error, sending) from None
    return {address: refused[address][0] for address in sorted(refused)}


def refusal(refused):
    """What to show when the server took a mail but refused some of its recipients (address -> SMTP code).
    A 4xx code is temporary (RFC 5321): the address is fine, that copy just did not arrive. Nothing is resent
    either way: the others already have the mail."""
    lasting = [address for address, code in refused.items() if code >= 500]
    passing = [address for address, code in refused.items() if code < 500]
    parts = []
    if lasting:
        parts.append(f'Der Mailserver hat {", ".join(lasting)} nicht angenommen – Adresse prüfen.')
    if passing:
        parts.append(f'Der Mailserver hat {", ".join(passing)} vorübergehend abgelehnt; dort kam diese Mail nicht an.')
    return ' '.join(parts)


def _mail(search):
    """The presentation module of a search's kind (preis/mail.py, wohnung/mail.py)."""
    return kinds.of(search).mail


def entry_price(entry):
    return _mail(entry[0]).price_text(*entry)


def shipping_text(entry):
    """The note under the price: shipping for products, what the rent is for flats."""
    return _mail(entry[0]).note(*entry)


def facts(entry):
    return _mail(entry[0]).facts(entry[1])


def price_range(search):
    return _mail(search).price_range(search)


def reason(entry):
    return _mail(entry[0]).reason(entry[1])


def plain_entry(entry):
    search, offer = entry
    links = maillinks.offer_links(search, offer)
    lines = [
        offer['title'],
        f'{entry_price(entry)} · {shipping_text(entry)} · ' + ' · '.join(facts(entry)),
        reason(entry),
        f'Suche: {search["query"]} · {price_range(search)}',
        offer['url'],
        *(f'Auch bei {twin["name"]}: {twin["url"]}' for twin in offer.get('twins') or []),
        *(f'{label}: {links[key]}' for key, label in (('save', 'Merken'), ('hide', 'Ausblenden')) if key in links),
    ]
    return '\n'.join(line for line in lines if line)


def _card(entry, photo_src=None):
    return _mail(entry[0]).card(*entry, photo_src, maillinks.offer_links(*entry))


def _kind_name(entries):
    return (entries[0][0].get('kind') if entries else None) or kinds.DEFAULT


def _footer(entries, words):
    """The way into the app, a pause link per search in this mail, and the unsubscribe link."""
    base = maillinks.app_url()
    link = f'color:{mailhtml.INK};font-weight:600'
    lines = [escape(words['footer'])]
    if base:
        searches = {search['id']: search for search, _ in entries if search.get('id') is not None}
        pauses = ' · '.join(
            f'Suche „{escape(search["query"])}“ <a href="{escape(maillinks.link("pause", s=sid))}" style="{link}">pausieren</a>'
            for sid, search in searches.items()
        )
        lines += [
            f'<a href="{escape(base)}/" style="{link}">In der App öffnen</a> · '
            f'<a href="{escape(maillinks.link("mute", k=_kind_name(entries)))}" style="{link}">Mails abbestellen</a>',
            *([pauses] if pauses else []),
        ]
    lines.append('Rhythmus und Empfänger stellst du in der App unter „Einstellungen“ ein.')
    return ''.join(f'<p style="margin:0 0 8px">{line}</p>' for line in lines)


def _preheader(entries):
    """The line inbox lists show under the subject; hidden in the mail itself."""
    if not entries:
        return ''
    first = entries[0]
    text = f'{" ".join(first[1]["title"].split())[:90]} · {entry_price(first)}'
    if len(entries) > 1:
        text += f' und {len(entries) - 1} weitere'
    return f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{escape(text)}</div>'


def digest_html(entries, photos=None):
    count = len(entries)
    words = _mail(entries[0][0] if entries else {}).WORDS  # one mail is one kind (worker._send_kind)
    noun, tagline = words['one' if count == 1 else 'many'], words['tagline']
    cards = ''.join(_card(entry, photo) for entry, photo in zip(entries, photos or [None] * count, strict=True))
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:{mailhtml.FILL};font-family:Geist,-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{mailhtml.INK};-webkit-text-size-adjust:100%">
{_preheader(entries)}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:{mailhtml.FILL}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;max-width:568px;background:#ffffff;border-radius:24px">
<tr><td style="padding:24px 24px 0;font-size:13px;font-weight:700;letter-spacing:-0.01em;color:{mailhtml.INK}">suchalarm</td></tr>
<tr><td style="padding:24px 24px 0;line-height:1"><span style="font-size:56px;font-weight:700;letter-spacing:-0.045em">{count}</span><span style="font-size:22px;font-weight:600;letter-spacing:-0.02em">&nbsp;{noun}</span></td></tr>
<tr><td style="padding:10px 24px 16px;font-size:14px;font-weight:600;color:{mailhtml.GREEN}">{tagline}</td></tr>
<tr><td style="padding:0 24px 4px"><table {mailhtml.TABLE}>{cards}</table></td></tr>
</table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;max-width:568px">
<tr><td style="padding:20px 24px 8px;font-size:12px;line-height:1.55;color:{mailhtml.MUTED}">{_footer(entries, words)}</td></tr>
</table></td></tr></table></body></html>"""


def compose_digest(entries, recipients, batch):
    """entries: (search, offer) pairs, both plain dicts."""
    message = EmailMessage()
    message['To'] = ', '.join(recipients)
    kind = _mail(entries[0][0])
    title = ' '.join(entries[0][1]['title'].split())[:80]  # headers may not contain line breaks
    message['Subject'] = kind.subject(entries, title)
    message['Message-ID'] = f'<{batch}@suchalarm.local>'
    if unsubscribe := maillinks.link('mute', k=_kind_name(entries)):
        message['List-Unsubscribe'] = f'<{unsubscribe}>'
        message['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'  # RFC 8058: the client POSTs to it
    lines = '\n\n'.join(plain_entry(entry) for entry in entries)
    message.set_content(f'{lines}\n\n{kind.WORDS["footer_plain"]}\n{maillinks.app_url()}'.strip())
    attached: list[tuple[bytes, str]] = []
    sources: list[str | None] = []
    for _, offer in entries:
        data = jpeg(offer['photo']) if offer.get('photo') else None
        if data:
            cid = make_msgid(domain='suchalarm.local')
            attached.append((data, cid))
            sources.append('cid:' + cid[1:-1])
        else:
            sources.append(None)
    message.add_alternative(digest_html(entries, sources), subtype='html')
    html_part = message.get_body(('html',))
    assert html_part is not None
    for data, cid in attached:
        html_part.add_related(data, maintype='image', subtype='jpeg', cid=cid)
    return message


def jpeg(path):
    """The cache keeps WebP; mail clients such as Outlook only show JPEG reliably.
    None if the file is gone or unreadable: a broken picture must never block the mail."""
    try:
        with Image.open(path) as picture:
            out = io.BytesIO()
            picture.convert('RGB').save(out, format='JPEG', quality=82)
            return out.getvalue()
    except Exception as exc:
        log.warning('Mail picture %s unreadable (%s)', Path(path).name, type(exc).__name__)
        return None


def test_message(recipients, entries, batch):
    """Proves delivery; with current matches it shows the real layout."""
    if entries:
        message = compose_digest(entries, recipients, batch)
        del message['Subject']
        message['Subject'] = 'Suchalarm: Testmail – so sehen Benachrichtigungen aus'
        return message
    message = EmailMessage()
    message['To'] = ', '.join(recipients)
    message['Subject'] = 'Suchalarm: Testmail'
    message.set_content(
        'Der Mailversand funktioniert. Neue passende Angebote und Wohnungen kommen ab jetzt an diese Adresse(n).'
    )
    return message
