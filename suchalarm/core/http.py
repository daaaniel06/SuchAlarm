"""Direct HTTPS requests for sources that work without the browser (see AGENTS.md).

Every request is booked in the same page budget as a browser visit and fails the same way: a refusal
(401/403/429) is browser.Blocked and pauses the site, anything else retries this one query.
"""

import asyncio
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import cadence
from .browser import REFUSAL_STATUSES, Blocked, Unreadable, note

BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/140.0.0.0 Safari/537.36'
)
TIMEOUT = 20
MAX_BYTES = 3_000_000


def ssl_context():
    """The system's certificates; certifi's where installed (Python from python.org on macOS has none)."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


_SSL = ssl_context()


def _download(url, headers, data):
    request = Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    try:
        with urlopen(request, timeout=TIMEOUT, context=_SSL) as response:
            note(status=response.status)
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        note(status=error.code)
        if error.code in REFUSAL_STATUSES:
            raise Blocked(
                f'Anbieter blockiert den Abruf (HTTP {error.code}). Kein Verbindungsfehler; später erneut versuchen.'
            ) from None
        raise RuntimeError(f'Anbieter antwortet mit HTTP {error.code}.') from None
    except (URLError, TimeoutError, OSError) as error:
        note(text=f'{type(error).__name__}: {getattr(error, "reason", error)}'[:300])  # shown under "Details"
        raise RuntimeError('Anbieter derzeit nicht erreichbar.') from None
    if len(raw) > MAX_BYTES:
        raise RuntimeError('Antwort des Anbieters ist zu groß.')
    return raw


async def get(source, url, headers=None, data=None, follow=False, kind='page'):
    """The body of one booked request to SOURCE's site, from its KIND of budget. FOLLOW marks a further
    request of the same fetch."""
    try:
        cadence.book(source, kind)
    except cadence.Deferred as exc:
        raise cadence.Deferred(exc.until, continuation=follow) from None
    note(stage='Seite laden', url=url, status=None, final_url=None, title=None, text=None, cards=None)
    return await asyncio.to_thread(_download, url, {'User-Agent': BROWSER_UA, **(headers or {})}, data)


async def get_json(source, url, headers=None, data=None, follow=False, kind='page'):
    raw = await get(source, url, {'Accept': 'application/json', **(headers or {})}, data, follow, kind)
    try:
        return json.loads(raw)
    except ValueError:
        raise Unreadable('Antwort des Anbieters nicht lesbar (Schnittstelle geändert?).') from None


async def get_html(source, url, follow=False):
    headers = {'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'de-DE,de;q=0.9'}
    raw = await get(source, url, headers, None, follow)
    return raw.decode('utf-8', errors='replace')
