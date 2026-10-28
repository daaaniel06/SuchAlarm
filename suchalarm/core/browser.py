"""Provider pages in the local stealth Firefox (invisible_playwright): guarded pages, booked visits, settled reads."""

import asyncio
import ipaddress
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from invisible_playwright._pw.async_api import Browser
from invisible_playwright._pw.async_api import Error as PlaywrightError
from invisible_playwright.async_api import InvisiblePlaywright

from . import cadence
from .cadence import note

EMPTY = re.compile(
    r'keine (?:ergebnisse|treffer|anzeigen|artikel|angebote)|0 ergebnisse|nichts gefunden|no results', re.I
)
BLOCK = re.compile(
    r'captcha|zugriff verweigert|access denied|verify you are human|bestätige.{0,30}(?:mensch|kein roboter)|pardon our interruption|sicherheitsüberprüfung',
    re.I,
)


class Blocked(cadence.Refused):
    """The provider refuses the browser (401/403/429 or a security check)."""


class Unreadable(RuntimeError):
    """No offer list in the page: consent wall, login or a page the parser cannot read.

    Not a refusal: a parser that no longer fits must not pause the portal for hours. It retries like
    any other failure and shows the source as disturbed; real blocks are Blocked."""


PAGE_TIMEOUT_MS = 35_000
SETTLE_ATTEMPTS, SETTLE_PAUSE_MS = 16, 750  # up to 12 s for consent pages and client redirects
REFUSAL_STATUSES = (401, 403, 429)
LOST_CONTEXT = ('Execution context was destroyed', 'Cannot find context')


def check_response(response):
    if response is not None and response.status < 400:
        return
    status = response.status if response else 'ohne Antwort'
    if status in REFUSAL_STATUSES:
        raise Blocked(
            f'Anbieter blockiert die Browser-Abfrage (HTTP {status}). Kein Verbindungsfehler; später erneut versuchen.'
        )
    raise RuntimeError(f'Anbieter antwortet mit HTTP {status}.')


DETAIL_TITLE, DETAIL_TEXT = 200, 600  # how much of a page's title and text a failure keeps (cadence.note)


async def load_page(page, source, url, follow=False, *, extract, readable, keep_unready=None):
    """Extracted content of one provider page: from the page cache, or one booked visit.

    EXTRACT is the function expression run in the page with SOURCE as argument. It must return a dict
    with at least 'title' and 'text' (strings, checked for security pages) and 'rows' (list, counted for
    the source details); anything else is the module's own. READABLE(data, follow) says whether the page
    has settled into something the parser can use; KEEP_UNREADY(data, follow) may still accept the last
    read when it never did. FOLLOW marks a follow-up page of the same visit. Pages are cached 15 minutes.
    """
    cached = cadence.cached_page(source, url)
    if cached is not None:
        return cached
    try:
        cadence.book(source, 'page')
    except cadence.Deferred as exc:
        raise cadence.Deferred(exc.until, continuation=follow) from None
    note(stage='Seite laden', url=url, status=None, final_url=None, title=None, text=None, cards=None)
    response = await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
    note(status=getattr(response, 'status', None), final_url=getattr(page, 'url', None))
    check_response(response)
    # Consent pages and client redirects can replace the document immediately
    # after DOMContentLoaded. Read only once the new document has settled.
    data = None
    for _ in range(SETTLE_ATTEMPTS):
        try:
            data = await page.evaluate(extract, source)
        except PlaywrightError as exc:
            if not any(text in str(exc) for text in LOST_CONTEXT):
                raise
            await page.wait_for_timeout(SETTLE_PAUSE_MS)
            continue
        note(
            stage='Seite auslesen',
            title=data['title'][:DETAIL_TITLE],
            text=' '.join(data['text'].split())[:DETAIL_TEXT],
            cards=len(data['rows']),
        )
        if BLOCK.search(data['title'] + '\n' + data['text'][:5000]):
            raise Blocked('Anbieter verlangt eine Sicherheitsprüfung. Später erneut versuchen.')
        if readable(data, follow):
            cadence.save_page(source, url, data)
            return data
        await page.wait_for_timeout(SETTLE_PAUSE_MS)
    if data and keep_unready and keep_unready(data, follow):
        cadence.save_page(source, url, data)
        return data
    raise Unreadable('Angebote nicht lesbar. Einwilligung, Anmeldung oder Seitenaufbau prüfen.')


BLOCKED_RESOURCES = ('image', 'media', 'font')  # pictures come through the image cache, never the browser


def route_guard(hosts, page):
    """Playwright route handler: the page may navigate only to HOSTS, and nothing may load from
    private addresses (the browser service sits inside the home network)."""
    public = {}

    async def is_public(host):
        if host not in public:
            try:
                addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
                public[host] = bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
            except (OSError, ValueError):
                public[host] = False
        return public[host]

    async def guard(route):
        request = route.request
        parsed = urlparse(request.url)
        foreign_navigation = (
            request.is_navigation_request() and request.frame == page.main_frame and parsed.hostname not in hosts
        )
        if (
            parsed.scheme not in ('http', 'https')
            or request.resource_type in BLOCKED_RESOURCES
            or foreign_navigation
            or not await is_public(parsed.hostname)
        ):
            await route.abort()
        else:
            await route.continue_()

    return guard


@asynccontextmanager
async def browser_page(hosts):
    """A fresh page in a freshly started stealth Firefox that may only navigate to HOSTS; closed afterwards.
    Headless runs on a private Xvfb (no Firefox headless mode, which anti-bots recognise)."""
    async with InvisiblePlaywright(headless=True, humanize=False, locale='de-DE', timezone='Europe/Berlin') as browser:
        assert isinstance(browser, Browser)  # without profile_dir it is a Browser, not a persistent context
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
        try:
            page = await context.new_page()
            await page.route('**/*', route_guard(hosts, page))
            yield page
        finally:
            await context.close()
