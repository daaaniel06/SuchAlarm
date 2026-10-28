"""The endpoints only flat searches have: area maps, place suggestions, a flat's position, detail pages on
request and "Nicht dieselbe Wohnung". app.py includes ROUTER through kinds.ROUTERS."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import scheduler, store
from ..core import cadence
from . import catalog, details, maps, sources
from .models import DetailRequest, Separate

ROUTER = APIRouter()


@ROUTER.get('/api/maps/{name}.webp')
async def area_map(name: str):
    """A searched place's map; its name changes with the place, so the browser keeps it for good."""
    file = maps.picture(name)
    if file is None:
        raise HTTPException(404, 'Karte nicht gefunden.')
    return FileResponse(
        file, media_type='image/webp', headers={'Cache-Control': 'private, max-age=31536000, immutable'}
    )


@ROUTER.get('/api/location')
async def location(search_id: int, url: str):
    """Where a flat lies, for the map in its sheet; looked up by address once if the portal gives no
    coordinates. {"wait": true} while the OpenStreetMap budget asks to wait a moment."""
    search = store.search(search_id)
    if not search:
        raise HTTPException(404, 'Suche nicht gefunden.')
    rows = catalog.listings(search) if search['kind'] == 'wohnung' else []
    flat = next((row for row in rows if row['url'] == url), None)
    if flat is None:
        raise HTTPException(404, 'Inserat nicht mehr vorhanden.')
    try:
        found = await maps.locate(flat)
    except cadence.Deferred:
        return {'wait': True}
    except Exception:
        raise HTTPException(502, 'Position konnte nicht geladen werden.') from None
    if found is None:
        raise HTTPException(404, 'Adresse nicht gefunden.')
    return found


@ROUTER.get('/api/places')
async def places(q: str = ''):
    """Place suggestions for the flat editor."""
    return await sources.suggest(q)


@ROUTER.post('/api/details')
async def request_detail(payload: DetailRequest):
    """Read a flat's detail page on request (within the portal's budget, so it takes a few minutes)."""
    with store.db() as conn:
        if not details.request_detail(conn, payload.url):
            raise HTTPException(404, 'Für dieses Inserat gibt es keine Detailseite.')
    scheduler.wake.set()
    return {'ok': True}


@ROUTER.post('/api/separate')
async def separate(payload: Separate):
    """ "Nicht dieselbe Wohnung": the flat leaves its group of listings on other portals for good."""
    with store.db() as conn:
        if not catalog.separate(conn, payload.search_id, payload.url):
            raise HTTPException(404, 'Treffer nicht mehr vorhanden.')
    return {'ok': True}
