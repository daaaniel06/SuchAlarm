"""HTTP API and static files. Access protection is the private tailnet; see README "Betrieb".

Handlers are async and use SQLite directly on the event loop, like the worker. Queries take
milliseconds, and sharing the loop keeps handlers and worker steps from interleaving (see worker.py).
"""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import mailer, scheduler, store, worker
from .core import cadence, images
from .kinds import KINDS, ROUTERS
from .models import AnySearch, Applied, Bookmark, Hide, MailServer, MailSettings, Preferences, Template

STATIC = Path(__file__).with_name('static')
# What the search editors offer, from every kind.
CHOICES = {key: value for kind in KINDS.values() for key, value in kind.CHOICES.items()}
# Every source of every kind; Kleinanzeigen appears in two under its own name for each.
ALL_SOURCES = {source: label for kind in KINDS.values() for source, label in kind.SOURCES.items()}
SOURCE_KINDS = {source: name for name, kind in KINDS.items() for source in kind.SOURCES}
MAX_BODY = 16 * 1024
MAIL_LOG_SHOWN = 50
CSP = '; '.join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    )
)


@asynccontextmanager
async def lifespan(app):
    store.init()
    tasks = [asyncio.create_task(worker.run()), asyncio.create_task(images.run())]
    yield
    for task in reversed(tasks):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
for router in ROUTERS:
    app.include_router(router)


@app.exception_handler(RequestValidationError)
async def invalid(request: Request, exc: RequestValidationError):
    """One readable sentence for the form instead of a list of field errors."""
    errors = exc.errors()
    own = errors and errors[0].get('type') == 'value_error'
    text = (
        str(errors[0].get('msg', '')).removeprefix('Value error, ') if own else 'Eingaben prüfen und erneut versuchen.'
    )
    return JSONResponse({'detail': text}, status_code=422)


def _refuse(request):
    """Why a writing request is refused, or None. Blocks cross-origin writes and oversized bodies."""
    origin = request.headers.get('origin')
    if origin and urlparse(origin).netloc != request.headers.get('host'):
        return 403, 'Fremder Ursprung abgewiesen.'
    if 'transfer-encoding' in request.headers:
        return 411, 'Länge der Anfrage fehlt.'  # a chunked body would bypass the size limit
    try:
        length = int(request.headers.get('content-length', '0'))
    except ValueError:
        return 400, 'Ungültige Anfrage.'
    if length > MAX_BODY:
        return 413, 'Anfrage zu groß.'
    if (
        request.method in ('POST', 'PUT')
        and request.headers.get('content-type', '').split(';')[0] != 'application/json'
    ):
        return 415, 'JSON erforderlich.'  # also forces a CORS preflight for any cross-site form
    return None


@app.middleware('http')
async def guard(request: Request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and (refusal := _refuse(request)):
        status, detail = refusal
        return JSONResponse({'detail': detail}, status_code=status)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = CSP
    response.headers.setdefault('Cache-Control', 'no-store' if request.url.path.startswith('/api/') else 'no-cache')
    return response


def existing(search_id):
    search = store.search(search_id)
    if not search:
        raise HTTPException(404, 'Suche nicht gefunden.')
    return search


def editable(search_id):
    """A search that may be changed now: not while the worker is writing its results."""
    search = existing(search_id)
    if worker.progress.search == search_id:
        raise HTTPException(409, 'Suche wird gerade geprüft. Danach erneut versuchen.')
    return search


# Reading


@app.get('/api/images/{filename}')
async def image_file(filename: str):
    path = images.file_path(filename)
    if path is None:
        raise HTTPException(404, 'Vorschaubild noch nicht verfügbar.')
    return FileResponse(
        path, media_type='image/webp', headers={'Cache-Control': 'private, max-age=31536000, immutable'}
    )


@app.get('/api/health')
async def health():
    store.settings()  # the database answers
    return {'ok': True}


def _area_mail(settings):
    return {key: settings[key] for key in ('enabled', 'frequency', 'batch_size')}


def _mail_server():
    """What the page shows of the mail server; whether a password is saved, never the password."""
    current = mailer.server()
    return {
        'sender': current.address,
        'host': current.host,
        'port': current.port,
        'password_set': bool(current.password),
        'fixed': current.fixed,
    }


@app.get('/api/state')
async def state():
    """Everything the page shows, in one response; polled every few seconds."""
    settings = store.settings()
    searches = store.searches()
    with store.db() as conn:
        hidden = store.hidden(conn)
        statuses = store.statuses(conn)
        saved = images.enrich(store.saved(conn))
        logs = store.mail_log(conn, MAIL_LOG_SHOWN)
        pending = {kind: store.pending(conn, kind) for kind in KINDS}
    own = {name: [s for s in searches if s['kind'] == name] for name in KINDS}
    hits = {kind.HITS: kind.hits(own[name], hidden) for name, kind in KINDS.items()}
    extras = {key: value for name, kind in KINDS.items() for key, value in kind.extras(own[name]).items()}
    mail = {
        'recipients': store.mail_settings(settings)['recipients'],
        'configured': mailer.configured(),
        'server': _mail_server(),
        'error': settings.get('mail_error'),
        'pending': pending,
        **{kind: _area_mail(store.mail_settings(settings, kind)) for kind in KINDS},
    }
    return {
        'searches': searches,
        **hits,
        'saved': saved,
        'hidden': len(hidden),
        'statuses': statuses,
        'mail_log': logs,
        'paused': store.paused(settings),
        'running': worker.progress.search,
        'queued': scheduler.queued_ids(),
        'queued_jobs': scheduler.queued_sources(),
        'running_source': worker.progress.source,
        'sources': ALL_SOURCES,
        'source_kinds': SOURCE_KINDS,
        'choices': CHOICES,
        'schedule': cadence.describe(searches),
        'mail': mail,
        'template': settings.get('application_template', ''),
        **extras,
    }


# Searches


@app.post('/api/searches', status_code=201)
async def create(payload: AnySearch):
    columns, values = payload.columns()
    with store.db() as conn:
        search_id = store.add_search(conn, payload.kind, columns, values)
    scheduler.wake.set()
    return {'id': search_id}


@app.put('/api/searches/{search_id}')
async def update(search_id: int, payload: AnySearch):
    old = editable(search_id)
    if old['kind'] != payload.kind:
        raise HTTPException(422, 'Die Art einer Suche lässt sich nicht ändern.')
    scheduler.cancel(search_id)
    columns, values = payload.columns()
    with store.db() as conn:
        store.change_search(conn, search_id, columns, values)
        KINDS[payload.kind].reset(conn, search_id, old, payload)
    scheduler.wake.set()
    return {'ok': True}


@app.delete('/api/searches/{search_id}')
async def delete(search_id: int):
    editable(search_id)
    scheduler.cancel(search_id)
    with store.db() as conn:
        store.delete_search(conn, search_id)
    return {'ok': True}


@app.post('/api/searches/{search_id}/check', status_code=202)
async def check(search_id: int):
    result = scheduler.request(existing(search_id))
    if result['queued']:
        scheduler.wake.set()
    return result


@app.post('/api/sources/{source}/retry', status_code=202)
async def retry_source(source: str):
    """A disturbed provider by hand: lift its pause and check it now for every active search using it."""
    if source not in ALL_SOURCES:
        raise HTTPException(404, 'Quelle nicht gefunden.')
    cadence.release(source)
    results = [scheduler.request(s, [source]) for s in store.searches() if s['active'] and source in s['sources']]
    if any(r['queued'] for r in results):
        scheduler.wake.set()
    return {
        'started': any(r['started'] for r in results),
        'already_running': any(r['already_running'] for r in results),
        'retry_after': max((r['retry_after'] for r in results), default=0),
    }


@app.get('/api/sources/{source}/details')
async def source_details(source: str):
    """Why a source fails, for the settings: provider pause and budget, and per search its query and last error."""
    if source not in ALL_SOURCES:
        raise HTTPException(404, 'Quelle nicht gefunden.')
    queued = set(scheduler.queued_ids())
    searches = [s for s in store.searches() if source in s['sources'] and (s['active'] or s['id'] in queued)]
    provider, queries = cadence.diagnose(source, searches)
    with store.db() as conn:
        statuses = {status['search_id']: status for status in store.statuses(conn, source)}
    kind = KINDS[SOURCE_KINDS[source]]
    result = [
        dict(
            queries[search['id']],
            search_id=search['id'],
            status=statuses.get(search['id']),
            url=kind.search_url(source, search),
        )
        for search in searches
    ]
    return {'provider': provider, 'searches': result}


# Offers


@app.post('/api/hidden')
async def hide(payload: Hide):
    """A swipe to the left: gone from the hits and from mails; "Rückgängig" or the settings bring it back."""
    if payload.hidden:
        existing(payload.search_id)
    with store.db() as conn:
        if payload.hidden:
            store.hide(conn, payload.search_id, payload.url, time.time())
        else:
            store.unhide(conn, payload.search_id, payload.url)
    return {'ok': True}


@app.delete('/api/hidden')
async def restore_hidden():
    with store.db() as conn:
        count = store.unhide(conn)
    return {'restored': count}


@app.delete('/api/offers')
async def clear_offers():
    """Searches, mail settings and the record of reported offers stay, so nothing is mailed twice."""
    with store.db() as conn:
        count = sum(kind.clear(conn) for kind in KINDS.values())
        store.unsave(conn)
    return {'deleted': count}


@app.post('/api/saved')
async def bookmark(payload: Bookmark):
    with store.db() as conn:
        if not payload.saved:
            store.unsave(conn, payload.url)
            return {'ok': True}
        search = store.search(payload.search_id)
        if not search or not KINDS[search['kind']].bookmark(conn, search, payload.url):
            raise HTTPException(404, 'Treffer nicht mehr vorhanden.')
    return {'ok': True}


@app.post('/api/applied')
async def applied(payload: Applied):
    """Marking a flat as applied for keeps it on the watch list ("Beworben"); unmarking keeps it saved."""
    with store.db() as conn:
        if payload.applied and not store.is_saved(conn, payload.url):
            search = store.search(payload.search_id)
            if not search or not KINDS[search['kind']].bookmark(conn, search, payload.url):
                raise HTTPException(404, 'Treffer nicht mehr vorhanden.')
        store.mark_applied(conn, payload.url, time.time() if payload.applied else None)
    return {'ok': True}


@app.put('/api/template')
async def template(payload: Template):
    with store.db() as conn:
        store.put_setting(conn, 'application_template', payload.text)
    return {'ok': True}


# Settings and mail


@app.put('/api/settings')
async def preferences(payload: Preferences):
    with store.db() as conn:
        store.put_setting(conn, 'paused', '1' if payload.paused else '0')
    scheduler.wake.set()
    return {'ok': True}


@app.put('/api/mail')
async def mail_settings(payload: MailSettings):
    if any(getattr(payload, kind).enabled for kind in KINDS) and not payload.recipients:
        raise HTTPException(422, 'Empfänger fehlt.')
    with store.db() as conn:
        store.put_setting(conn, 'recipients', json.dumps(payload.recipients))
        for kind in KINDS:
            area = getattr(payload, kind)
            store.put_setting(conn, store.mail_key('mail_enabled', kind), '1' if area.enabled else '0')
            store.put_setting(conn, store.mail_key('frequency', kind), area.frequency)
            store.put_setting(conn, store.mail_key('batch_size', kind), str(area.batch_size))
    scheduler.wake.set()
    return {'ok': True}


@app.put('/api/mail/server')
async def mail_server(payload: MailServer):
    if mailer.server().fixed:
        raise HTTPException(409, 'Der Mailserver ist in der compose.yml festgelegt.')
    saved = store.mail_server()
    # The saved password only ever goes to the server and mailbox it was entered for; otherwise anyone
    # reaching the page could point the server elsewhere and catch it at the login.
    moved = (payload.sender, payload.host, payload.port) != (saved['sender'], saved['host'], saved['port'])
    if payload.password is not None or moved:
        saved['password'] = payload.password or ''
    saved.update(sender=payload.sender, host=payload.host, port=payload.port)
    with store.db() as conn:
        store.put_setting(conn, 'mail_server', json.dumps(saved))
    return {'ok': True}


def _sample_entries(limit=2):
    """Recent offers that match their search, so a test mail shows the real layout."""
    samples: list[tuple[dict, dict]] = []
    for name, kind in KINDS.items():
        if len(samples) < limit:
            samples += kind.samples(store.searches(name), limit - len(samples))
    ready, _ = images.photos([offer['url'] for _, offer in samples])
    for _, offer in samples:
        offer['photo'] = ready.get(offer['url'])
    return samples


@app.post('/api/mail/test')
async def mail_test():
    recipients = store.mail_settings()['recipients']
    if not recipients:
        raise HTTPException(422, 'Zuerst eine Empfängeradresse speichern.')
    message = mailer.test_message(recipients, _sample_entries(), uuid.uuid4().hex)
    try:
        refused = await asyncio.to_thread(mailer.deliver, message)
    except mailer.DeliveryError as exc:
        raise HTTPException(502, str(exc)) from None
    if refused:
        raise HTTPException(502, mailer.refusal(refused))
    return {'ok': True}


@app.get('/')
async def index():
    return FileResponse(STATIC / 'index.html')


app.mount('/static', StaticFiles(directory=STATIC), name='static')
