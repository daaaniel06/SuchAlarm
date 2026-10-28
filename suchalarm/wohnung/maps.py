"""Where a flat lies, and one map of each searched place to draw it on.

Positions: ImmoScout24, bauverein and mostly NHW list coordinates. Other flats are looked up by their
address with OpenStreetMap's Nominatim when their sheet opens, once; the answer is kept for good.

The map: the worker fetches the OpenStreetMap tiles around a search's place in free moments and stitches
them into a light and a dark greyscale picture in DATA_DIR/maps. Every sheet shows that picture (the
browser keeps it) and only places the dot. Both kinds of request use the 'osm' budget (core/cadence.py).
"""

import asyncio
import io
import json
import logging
import math
import re
import time
from urllib.parse import urlencode

from PIL import Image, ImageOps

from ..core import cadence, http
from . import sources

ZOOM = 13  # street names readable when the picture is shown at about a third of its size
AREA_KM = {
    'west': 8.0,
    'east': 8.0,
    'north': 6.0,
    'south': 7.5,
}  # around the centre: Darmstadt from Arheilgen to Eberstadt
VERSION = 1  # raise to rebuild every stored map
TILES_PER_STEP = 10  # tiles fetched per idle step; the 'osm' budget spaces them about a second apart
SHORT_WAIT = 3  # seconds a tile may wait for its slot within one step
TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'

log = logging.getLogger('suchalarm')


def map_dir():
    return cadence.data_dir() / 'maps'


def tile_dir():
    return cadence.data_dir() / 'tiles'


# Positions


def has_street(place):
    """A house number before the postcode or town means the spot is exact."""
    first = place.split(',')[0]
    return bool(re.search(r'[A-Za-zäöüß.]\s+\d+\s*[a-zA-Z]?\b', first)) and 'unvollständig' not in place


def queries(place):
    """Address first, then the postcode, then the town: the best match that exists wins."""
    parts = [part.strip() for part in re.sub(r'\(.*?\)', '', place).split(',') if part.strip()]
    postcode = re.search(r'\b\d{5}\b', place)
    # The town follows the postcode; without one it is the second of three parts or the last one.
    after_code = re.search(r'\b\d{5}\s+([^\s,]+)', place)
    if after_code:
        town = after_code.group(1)
    elif len(parts) >= 3:
        town = parts[1].split()[0]
    elif parts and (len(parts) > 1 or not has_street(place)):
        town = parts[-1].split()[0]
    else:
        town = ''
    street_like = bool(parts) and bool(
        re.search(r'str\.|straße|strasse|weg\b|platz|allee|gasse|ring\b|damm\b|pfad\b', parts[0], re.I)
    )
    candidates = []
    if parts and (has_street(place) or street_like):
        candidates.append(f'{parts[0]}, {postcode.group() + " " if postcode else ""}{town}'.strip(', '))
    if postcode:
        candidates.append(f'{postcode.group()} {town}'.strip())
    if town:
        candidates.append(town)
    return list(dict.fromkeys(candidates))


async def geocode(query):
    """[lat, lon] of an address from Nominatim, None if it is not found; each query is asked once."""
    key = query.casefold()
    known = cadence.location('osm-address', key)
    if known is not None:
        return json.loads(known)
    url = 'https://nominatim.openstreetmap.org/search?' + urlencode(
        {'q': query, 'countrycodes': 'de', 'format': 'json', 'limit': 1}
    )
    result = await http.get_json('osm', url, {'User-Agent': sources.OSM_UA}, kind='osm')
    value = [float(result[0]['lat']), float(result[0]['lon'])] if isinstance(result, list) and result else None
    cadence.save_location('osm-address', key, json.dumps(value))
    return value


async def locate(flat):
    """{lat, lon, approx} of a flat (a row of catalog.listings), None without a findable place.
    Raises cadence.Deferred while the budget asks to wait."""
    place = flat.get('place') or ''
    if flat.get('lat') is not None and flat.get('lon') is not None:
        return {'lat': flat['lat'], 'lon': flat['lon'], 'approx': not has_street(place)}
    candidates = queries(place)
    for query in candidates:
        found = await geocode(query)
        if found:
            return {'lat': found[0], 'lon': found[1], 'approx': not (has_street(place) and query == candidates[0])}
    return None


# The map of a searched place


def tile_xy(lat, lon, zoom=ZOOM):
    """Web Mercator position in tile units."""
    n = 2**zoom
    sin = math.sin(math.radians(lat))
    return (lon + 180) / 360 * n, (0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * n


def area_pixels(centre):
    """The area's corners in tile units: x0, y0 (north-west), x1, y1 (south-east)."""
    lat, lon = centre
    per_lon = 111.0 * math.cos(math.radians(lat))
    north, south = lat + AREA_KM['north'] / 111.0, lat - AREA_KM['south'] / 111.0
    west, east = lon - AREA_KM['west'] / per_lon, lon + AREA_KM['east'] / per_lon
    return (*tile_xy(north, west), *tile_xy(south, east))


def key(centre):
    return f'v{VERSION}-z{ZOOM}-{centre[0]:.4f}-{centre[1]:.4f}'.replace('.', '_')


def info(centre):
    """What the page needs to place a dot: the picture's name and its exact Mercator bounds; None until built."""
    name = key(centre)
    if not all((map_dir() / f'{name}{suffix}.webp').is_file() for suffix in ('', '-dark')):
        return None
    x0, y0, x1, y1 = area_pixels(centre)
    cx, cy = tile_xy(*centre)
    left, top = int(x0 * 256), int(y0 * 256)
    return {
        'key': name,
        'zoom': ZOOM,
        'left': left,
        'top': top,
        'width': int(x1 * 256) - left,
        'height': int(y1 * 256) - top,
        'centre_x': round(cx * 256),
        'centre_y': round(cy * 256),
    }


def areas(searches):
    """{search id: info} of the flat searches whose map is ready."""
    found = {}
    for search in searches:
        centre = sources.centre(search) if search['kind'] == 'wohnung' else None
        if centre and (area := info(centre)):
            found[search['id']] = area
    return found


def _tiles(centre):
    x0, y0, x1, y1 = area_pixels(centre)
    return [(x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]


def _tile_file(x, y):
    return tile_dir() / str(ZOOM) / str(x) / f'{y}.png'


async def _tile(x, y):
    """One tile; the second or so the 'osm' budget asks between two is waited out here."""
    while True:
        try:
            return await http.get(
                'osm-tiles', TILE_URL.format(z=ZOOM, x=x, y=y), {'User-Agent': sources.OSM_UA}, kind='osm'
            )
        except cadence.Deferred as exc:
            wait = exc.until - time.time()
            if wait > SHORT_WAIT:
                raise
            await asyncio.sleep(max(wait, 0.05))


async def build_step(searches):
    """Fetch a few missing tiles of the next place without a map; stitch it once all are there.
    Returns the number of tiles fetched. Raises nothing: a failed tile is tried again next time."""
    for centre in dict.fromkeys(sources.centre(s) for s in searches if s['kind'] == 'wohnung'):
        if not centre or info(centre):
            continue
        fetched = 0
        for x, y in _tiles(centre):
            file = _tile_file(x, y)
            if file.is_file():
                continue
            if fetched == TILES_PER_STEP:
                return fetched
            try:
                raw = await _tile(x, y)
            except cadence.Deferred:
                return fetched  # the budget asks to wait: next idle step
            except Exception as exc:
                log.warning('Map tile %s/%s failed (%s)', x, y, type(exc).__name__)
                return fetched
            file.parent.mkdir(parents=True, exist_ok=True)
            file.with_suffix('.partial').write_bytes(raw)
            file.with_suffix('.partial').replace(file)
            fetched += 1
        try:
            _stitch(centre)
        except OSError:  # an unreadable tile: fetched again on the next step
            log.warning('Map of %s could not be stitched', key(centre))
            for x, y in _tiles(centre):
                _tile_file(x, y).unlink(missing_ok=True)
        return fetched
    return 0


def _stitch(centre):
    x0, y0, x1, y1 = area_pixels(centre)
    left, top = int(x0 * 256), int(y0 * 256)
    canvas = Image.new('L', (int(x1 * 256) - left, int(y1 * 256) - top), 235)
    for x, y in _tiles(centre):
        with Image.open(_tile_file(x, y)) as tile:
            canvas.paste(ImageOps.grayscale(tile), (x * 256 - left, y * 256 - top))
    # Quieter than OSM's own contrast: the map is background, the dot is the point. Dark mode gets its
    # own picture: no CSS filter in the browser (Safari paints large filtered layers piece by piece).
    light = canvas.point(lambda value: int(64 + value * 0.74))
    dark = canvas.point(lambda value: int(12 + (255 - value) * 0.62))
    map_dir().mkdir(parents=True, exist_ok=True)
    name = key(centre)
    for picture, suffix in ((light, ''), (dark, '-dark')):
        target = map_dir() / f'{name}{suffix}.webp'
        partial = target.with_suffix('.partial')
        buffer = io.BytesIO()
        picture.save(buffer, 'WEBP', quality=75, method=6)
        partial.write_bytes(buffer.getvalue())
        partial.replace(target)
    for x, y in _tiles(centre):  # the picture holds them now
        _tile_file(x, y).unlink(missing_ok=True)


def cleanup(searches):
    """Maps of places no flat search uses any more go."""
    used = {key(c) for c in (sources.centre(s) for s in searches if s['kind'] == 'wohnung') if c}
    for file in map_dir().glob('*.webp') if map_dir().is_dir() else []:
        if file.stem.removesuffix('-dark') not in used:
            file.unlink(missing_ok=True)


def picture(name):
    """Path of a stored map picture by its name (key, optionally with -dark), None if unknown."""
    if not re.fullmatch(r'[\w-]+', name):
        return None
    file = map_dir() / f'{name}.webp'
    return file if file.is_file() else None
