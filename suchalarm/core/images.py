"""Provider thumbnails: one download at a time, local immutable files, no browser hotlinks."""

import asyncio
import functools
import hashlib
import http.client
import io
import ipaddress
import logging
import os
import re
import socket
import tempfile
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

from . import cadence
from .http import ssl_context

MAX_BYTES, MAX_PIXELS = 2_000_000, 20_000_000
MAX_DISK, MAX_FILES = 128 * 1024 * 1024, 2000
MAX_URL, MAX_PENDING, MAX_ATTEMPTS = 2500, 1000, 3  # failed pictures retry after 6 hours
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
log = logging.getLogger('suchalarm')
# Provider image CDNs, as a host pattern per source; pictures are fetched from nowhere else.
IMAGE_HOSTS: dict[str, re.Pattern] = {}


def register(source, hosts):
    IMAGE_HOSTS[source] = re.compile(hosts)


def root():
    return Path(os.environ.get('DATA_DIR', '/data')) / 'images'


def _setup(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS pictures (key TEXT PRIMARY KEY, source TEXT, remote TEXT, state TEXT, filename TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, seen REAL, bytes INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS offers (url TEXT PRIMARY KEY, key TEXT, seen REAL);
    """)
    if 'priority' not in {r[1] for r in conn.execute('PRAGMA table_info(pictures)')}:
        conn.execute('ALTER TABLE pictures ADD COLUMN priority INTEGER DEFAULT 0')


def db():
    return cadence.transaction('images/cache.db', _setup)


def valid(source, value):
    if not isinstance(value, str) or len(value) > MAX_URL:
        return False
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        pattern = IMAGE_HOSTS.get(source)
        allowed = pattern is not None and pattern.fullmatch(host)
        return bool(
            url.scheme == 'https' and allowed and not url.username and not url.password and url.port in (None, 443)
        )
    except ValueError:
        return False


def remember(source, pictures):
    """PICTURES maps an offer URL to its picture at SOURCE; new pictures are queued for download."""
    now = time.time()
    with db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM pictures WHERE state='pending'").fetchone()[0]
        for url, remote in pictures.items():
            if not valid(source, remote):
                continue
            key = hashlib.sha256((source + ':' + remote).encode()).hexdigest()
            exists = conn.execute('SELECT 1 FROM pictures WHERE key=?', (key,)).fetchone()
            if not exists:
                if pending >= MAX_PENDING:
                    continue
                conn.execute(
                    "INSERT INTO pictures(key,source,remote,state,seen) VALUES (?,?,?,'pending',?)",
                    (key, source, remote, now),
                )
                pending += 1
            else:
                conn.execute('UPDATE pictures SET seen=? WHERE key=?', (now, key))
            conn.execute('INSERT OR REPLACE INTO offers VALUES (?,?,?)', (url, key, now))


def enrich(offers):
    with db() as conn:
        ready = {
            r['url']: r['filename']
            for r in conn.execute(
                "SELECT o.url,p.filename FROM offers o JOIN pictures p ON o.key=p.key WHERE p.state='ready'"
            )
        }
    for offer in offers:
        name = ready.get(offer['url'])
        offer['image_url'] = 'api/images/' + name if name and (root() / name).is_file() else None
    return offers


def fingerprints(urls):
    """url -> 64-bit difference hash (hex) of its stored picture, for comparing photos across providers."""
    if not urls:
        return {}
    marks = ','.join('?' * len(urls))
    with db() as conn:
        rows = conn.execute(
            f"SELECT o.url,p.filename FROM offers o JOIN pictures p ON o.key=p.key WHERE p.state='ready' AND o.url IN ({marks})",
            list(urls),
        ).fetchall()
    found = {r['url']: _fingerprint(r['filename']) for r in rows if file_path(r['filename'] or '')}
    return {url: value for url, value in found.items() if value}


@functools.lru_cache(maxsize=4096)
def _fingerprint(name):
    """Equal for the same photo in another size or quality (Wohnungsbot). A file's name is its content's
    hash, so the result never changes. None for an unreadable or nearly uniform picture (a placeholder)."""
    try:
        with Image.open(root() / name) as picture:
            pixels: list[int] = list(picture.convert('L').resize((9, 8), Image.Resampling.LANCZOS).get_flattened_data())  # type: ignore[arg-type]
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    bits = [pixels[row * 9 + column] > pixels[row * 9 + column + 1] for row in range(8) for column in range(8)]
    if not 8 <= sum(bits) <= 56:
        return None
    return f'{sum(1 << index for index, bit in enumerate(bits) if bit):016x}'


def prioritize(urls):
    """Pictures of offers under the limit (the hit list and the mails) are loaded first."""
    with db() as conn:
        conn.executemany(
            'UPDATE pictures SET priority=1 WHERE key=(SELECT key FROM offers WHERE url=?)', [(u,) for u in urls]
        )


def photos(urls):
    """url -> local file for ready pictures; urls whose picture may still come are listed in 'pending'."""
    if not urls:
        return {}, set()
    marks = ','.join('?' * len(urls))
    with db() as conn:
        rows = conn.execute(
            f'SELECT o.url,p.state,p.filename,p.attempts FROM offers o JOIN pictures p ON o.key=p.key WHERE o.url IN ({marks})',
            list(urls),
        ).fetchall()
    ready = {r['url']: path for r in rows if r['state'] == 'ready' and (path := file_path(r['filename'] or ''))}
    return ready, {r['url'] for r in rows if r['state'] == 'pending' and r['attempts'] < MAX_ATTEMPTS}


def file_path(name):
    if not re.fullmatch(r'[0-9a-f]{64}\.webp', name):
        return None
    path = root() / name
    return path if path.is_file() else None


class ImageFailure(Exception):
    def __init__(self, blocked=False, retry_at=0):
        self.blocked, self.retry_at = blocked, retry_at


IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/webp')
THUMBNAIL = (320, 240)
TIMEOUT = 15


def _connect(host):
    """HTTPS connection pinned to an address checked to be public; TLS still verifies the CDN hostname."""
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ImageFailure()
    context = ssl_context()
    conn = http.client.HTTPSConnection(host, timeout=TIMEOUT, context=context)
    sock = socket.create_connection((str(addresses[0][4][0]), 443), timeout=TIMEOUT)
    try:
        conn.sock = context.wrap_socket(sock, server_hostname=host)
    except Exception:
        sock.close()  # a TLS failure must not leave the connection open
        raise
    return conn


def _fetch(conn, path):
    """The body of a successful image response, at most MAX_BYTES."""
    conn.request(
        'GET',
        path,
        headers={'Accept': 'image/webp,image/jpeg,image/png', 'User-Agent': 'Suchalarm/1.0 (private image cache)'},
    )
    response = conn.getresponse()
    if response.status != 200:
        retry = response.getheader('Retry-After', '')
        retry_at = time.time() + min(int(retry), 86400) if retry.isdigit() else 0
        raise ImageFailure(response.status in (401, 403, 429), retry_at)
    if response.getheader('Content-Type', '').split(';')[0] not in IMAGE_TYPES:
        raise ImageFailure()
    length = response.getheader('Content-Length', '')
    if length.isdigit() and int(length) > MAX_BYTES:
        raise ImageFailure()
    raw = response.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ImageFailure()
    return raw


def _thumbnail(raw):
    """A small WebP; decompression bombs and oversized pictures are refused."""
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ImageFailure()
            image.thumbnail(THUMBNAIL)
            out = io.BytesIO()
            image.convert('RGB').save(out, format='WEBP', quality=78)
    return out.getvalue()


def download(source, url):
    """One provider picture as a local thumbnail (bytes). Raises ImageFailure."""
    if not valid(source, url):
        raise ImageFailure()
    parsed = urlsplit(url)
    conn = _connect(parsed.hostname)
    try:
        raw = _fetch(conn, parsed.path + ('?' + parsed.query if parsed.query else ''))
    finally:
        conn.close()
    return _thumbnail(raw)


async def step():
    now = time.time()
    with db() as conn:
        # Only the most urgent picture per provider: each provider gets at most one try per pass.
        jobs = conn.execute(
            'SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY source ORDER BY priority DESC, seen DESC) AS n '
            "FROM pictures WHERE state='pending' AND attempts<? AND next_at<=?) WHERE n=1 ORDER BY priority DESC, seen DESC",
            (MAX_ATTEMPTS, now),
        ).fetchall()
    for job in jobs:
        try:
            cadence.book(job['source'], 'image')
        except cadence.Deferred:
            continue
        try:
            data = await asyncio.to_thread(download, job['source'], job['remote'])
            name = hashlib.sha256(data).hexdigest() + '.webp'
            fd, tmp = tempfile.mkstemp(dir=root())
            try:
                with os.fdopen(fd, 'wb') as target:
                    target.write(data)
                os.replace(tmp, root() / name)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            with db() as conn:
                conn.execute(
                    "UPDATE pictures SET state='ready',filename=?,bytes=? WHERE key=?", (name, len(data), job['key'])
                )
            cadence.outcome(job['source'], 'image', False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            until = time.time() + 21600
            if isinstance(exc, ImageFailure) and exc.blocked:
                until = cadence.outcome(job['source'], 'image', True, exc.retry_at)
            with db() as conn:
                conn.execute('UPDATE pictures SET attempts=attempts+1,next_at=? WHERE key=?', (until, job['key']))
        return


def cleanup():
    now = time.time()
    with db() as conn:
        conn.execute('DELETE FROM offers WHERE seen<?', (now - 30 * 86400,))
        conn.execute('DELETE FROM pictures WHERE seen<?', (now - 30 * 86400,))
        rows = conn.execute("SELECT key,bytes FROM pictures WHERE state='ready' ORDER BY seen DESC").fetchall()
        total = 0
        for index, row in enumerate(rows):
            total += row['bytes']
            if index >= MAX_FILES or total > MAX_DISK:
                conn.execute('DELETE FROM pictures WHERE key=?', (row['key'],))
        keep = {r[0] for r in conn.execute('SELECT filename FROM pictures WHERE filename IS NOT NULL')}
    for path in root().glob('*.webp'):
        if path.name not in keep:
            path.unlink(missing_ok=True)


async def run():
    clean_at = 0.0
    while True:
        try:
            await step()
            if time.time() >= clean_at:
                await asyncio.to_thread(cleanup)
                clean_at = time.time() + 3600
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Image cache failed')  # a missing thumbnail must never interrupt offers or mail delivery
        await asyncio.sleep(5)
