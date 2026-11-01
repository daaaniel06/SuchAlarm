"""Screenshots of every view of the page with sample data, for comparing layouts before and after a change.

    .venv/bin/python scripts/ui_screenshots.py --ref origin/main docs/ui/vorher
    .venv/bin/python scripts/ui_screenshots.py docs/ui/nachher
    .venv/bin/python scripts/ui_screenshots.py --compare docs/ui/vorher docs/ui/nachher docs/ui/index.html \
        --notes docs/ui/notizen.json

Starts the app on its own DATA_DIR (a temporary folder) without worker and image downloads, fills in
searches, hits, watch list, source states and mail history, and photographs both areas on a phone (375 px)
and a desktop with Playwright Chromium (requirements-dev.txt). --ref serves the frontend of another git
state against today's server. No provider, map or other outside address is contacted: the browser only
reaches the local server.
"""

import argparse
import asyncio
import difflib
import fnmatch
import hashlib
import html
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8765
VIEWPORTS = {'handy': dict(width=375, height=812, scale=2), 'desktop': dict(width=1280, height=860, scale=1)}
AREAS = ('wohnung', 'preis')
NOW = time.time()
HOUR = 3600


# Sample data


def _picture(images, seed):
    """A calm two-tone stand-in for a provider's photo; returns its file name."""
    from PIL import Image, ImageDraw

    tones = [(222, 214, 200), (196, 208, 214), (214, 204, 214), (206, 216, 198), (230, 222, 210), (190, 196, 206)]
    back, front = tones[seed % len(tones)], tones[(seed + 2) % len(tones)]
    picture = Image.new('RGB', (480, 360), back)
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 230 + seed % 40, 480, 360), fill=front)
    draw.rectangle((60 + seed * 13 % 160, 70, 220 + seed * 13 % 160, 230 + seed % 40), fill=tuple(c - 18 for c in back))
    name = hashlib.sha256(f'bild-{seed}'.encode()).hexdigest() + '.webp'
    picture.save(images / name, 'WEBP', quality=70)
    return name


def _post(client, path, payload):
    response = client.post(path, json=payload) if path != 'api/mail' else client.put(path, json=payload)
    if response.status_code >= 400:
        raise SystemExit(f'{path}: {response.status_code} {response.text}')
    return response.json()


def seed():
    """Searches through the real API (its validation), hits and states straight into the tables."""
    from fastapi.testclient import TestClient

    from suchalarm import app as web
    from suchalarm import store

    store.init()
    client = TestClient(web.app)  # without "with": no lifespan, so no worker

    flats = [
        store.search(_post(client, 'api/searches', payload)['id'])
        for payload in (
            dict(
                kind='wohnung', query='Darmstadt', max_price=130000, radius=5,
                sources=['immoscout24', 'kleinanzeigen-wohnung', 'wg-gesucht', 'immowelt', 'bauverein'],
                filters=dict(min_area=45, min_rooms=2, exclusions=['WBS', 'Tauschwohnung']),
            ),
            dict(
                kind='wohnung', query='64283', max_price=95000, radius=0, active=False,
                sources=['immoscout24', 'ohne-makler'], filters=dict(min_area=30, min_rooms=1),
            ),
        )
    ]  # fmt: skip
    products = [
        store.search(_post(client, 'api/searches', payload)['id'])
        for payload in (
            dict(
                query='Resident Evil Requiem', max_price=3500, category='videospiele',
                sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo'], filters=dict(platform='ps5'),
            ),
            dict(query='iPhone 15 128 GB', max_price=42000, min_price=20000, category='elektronik',
                 sources=['ebay', 'kleinanzeigen', 'vinted', 'idealo']),
            dict(query='Ecksofa', max_price=15000, category='moebel', delivery='abholung', plz='64283', radius=25,
                 sources=['kleinanzeigen'], negotiable=True),
            dict(query='Cube Rennrad', max_price=60000, category='fahrraeder', sources=['ebay', 'kleinanzeigen'],
                 active=False),
        )
    ]  # fmt: skip

    flat_rows = [
        # source, title, place, warm, cold, m², rooms, hours ago, traits, available from
        ('immoscout24', 'Helle 3-Zimmer-Wohnung mit Balkon', 'Heinrichstraße 12, 64283 Darmstadt',
         1180, 940, 74, 3, 2, [], '2026-12-01'),
        ('immowelt', 'Helle 3-Zimmer-Wohnung mit Balkon', 'Heinrichstr. 12, 64283 Darmstadt',
         1180, 940, 74, 3, 5, [], '2026-12-01'),
        ('kleinanzeigen-wohnung', 'Gemütliche 2-Zimmer-Altbauwohnung', 'Bessungen, 64285 Darmstadt',
         None, 760, 58, 2, 4, [], '2026-11-15'),
        ('wg-gesucht', 'Ruhige Wohnung nahe Herrngarten', '64289 Darmstadt',
         990, 800, 52, 2, 9, [], None),
        ('bauverein', 'Modernisierte Wohnung im Martinsviertel', 'Liebfrauenstraße 40, 64289 Darmstadt',
         1090, 870, 63, 2.5, 30, [], '2027-01-01'),
        ('immoscout24', 'Familienwohnung mit Garten', 'Arheilgen, 64291 Darmstadt',
         1290, 1040, 88, 4, 50, [], None),
        ('immoscout24', 'Dachgeschoss mit Ausblick, WBS erforderlich', 'Kranichstein, 64289 Darmstadt',
         720, 560, 61, 2, 20, ['WBS'], None),
        ('kleinanzeigen-wohnung', 'Penthouse am Luisenplatz', '64283 Darmstadt',
         2400, 2000, 120, 4, 70, [], None),
        ('immowelt', 'Kleine Wohnung, Tausch gesucht', 'Eberstadt, 64297 Darmstadt',
         650, 500, 41, 1.5, 100, ['Tauschwohnung'], None),
        ('ohne-makler', 'Zwei Zimmer am Riegerplatz', 'Riegerplatz 3, 64289 Darmstadt',
         870, 690, 48, 2, 6, [], '2026-12-15'),
    ]  # fmt: skip
    offer_rows = [
        # search, source, title, price, shipping, seller, condition, hours ago
        (0, 'ebay', 'Resident Evil Requiem PS5 neu OVP', 2899, 0, 'gamestore_24', 'Neu', 1),
        (0, 'kleinanzeigen', 'Resident Evil Requiem PlayStation 5', 2500, 450, 'Privat', 'Sehr gut', 3),
        (0, 'vinted', 'Resident Evil Requiem (PS5)', 2200, 349, 'lena_k', 'Sehr gut', 8),
        (0, 'ebay', 'Resident Evil Requiem PS5 Steelbook Edition', 4499, 0, 'retro-games', 'Neu', 20),
        (0, 'kleinanzeigen', 'RE Requiem PS5 wie neu', 3000, None, 'Privat', 'Gut', 30),
        (1, 'ebay', 'Apple iPhone 15 128GB Schwarz', 41900, 0, 'smartphone-outlet', 'Generalüberholt', 2),
        (1, 'kleinanzeigen', 'iPhone 15 128 GB blau, Akku 96 %', 39000, 690, 'Privat', 'Sehr gut', 5),
        (1, 'vinted', 'iPhone 15 128GB rosé', 38500, 549, 'mia.b', 'Gut', 26),
        (1, 'ebay', 'iPhone 15 128 GB Displayschaden', 18000, 0, 'handyankauf', 'Defekt', 40),
        (2, 'kleinanzeigen', 'Ecksofa grau mit Schlaffunktion', 12000, None, 'Privat', 'Gut', 4),
        (2, 'kleinanzeigen', 'Ecksofa Cord beige, Abholung Darmstadt', 0, None, 'Privat', 'Gebraucht', 12),
        (2, 'kleinanzeigen', 'Großes Ecksofa Leder', 25000, None, 'Privat', 'Gut', 50),
    ]  # fmt: skip

    from suchalarm.core import images

    pictures = images.root()
    pictures.mkdir(parents=True, exist_ok=True)
    with images.db() as conn:
        for number in range(len(flat_rows) + len(offer_rows)):
            key = f'k{number}'
            conn.execute(
                "INSERT INTO pictures(key,source,remote,state,filename,seen) VALUES (?,?,?,'ready',?,?)",
                (key, 'ebay', '', _picture(pictures, number), NOW),
            )

    def picture_for(conn_images, url, number):
        if number % 4 != 3:  # some rows keep the source tile
            conn_images.execute('INSERT INTO offers(url,key,seen) VALUES (?,?,?)', (url, f'k{number}', NOW))

    from suchalarm.wohnung.domain import Listing

    with store.db() as conn, images.db() as conn_images:
        for number, (source, title, place, warm, cold, area, rooms, ago, traits, free) in enumerate(flat_rows):
            url = f'https://example.invalid/wohnung/{number}'
            listing = Listing(
                source, url, title, place=place, rent=warm, cold_rent=cold, area=area, rooms=rooms,
                traits=traits, available_from=free, lat=49.87 + number / 500, lon=8.65 + number / 500,
                exclusions_known=True,
            )  # fmt: skip
            seen = NOW - ago * HOUR
            conn.execute(
                'INSERT INTO wohnung.listings(search_id,source,url,body,first_seen,last_seen) VALUES (?,?,?,?,?,?)',
                (flats[0]['id'], source, url, json.dumps(listing.__dict__), seen, NOW - HOUR),
            )
            conn.execute(
                "INSERT INTO wohnung.listing_details(url,source,body,state,created_at) VALUES (?,?,'{}','done',?)",
                (url, source, seen),
            )
            picture_for(conn_images, url, number)
        for number, (search, source, title, price, shipping, seller, condition, ago) in enumerate(offer_rows):
            url = f'https://example.invalid/angebot/{number}'
            seen = NOW - ago * HOUR
            conn.execute(
                'INSERT INTO preis.offers(search_id,source,url,title,price,shipping,seller,condition,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (products[search]['id'], source, url, title, price, shipping, seller, condition, seen, NOW - HOUR),
            )
            picture_for(conn_images, url, len(flat_rows) + number)
        conn.execute(
            'INSERT INTO preis.idealo_references VALUES (?,?)',
            (products[0]['id'], json.dumps(dict(price=3999, url='https://example.invalid/idealo', shipping_included=True))),
        )  # fmt: skip
        # Source states: everything fine, except Vinted blocked and WG-Gesucht failing twice.
        for search in flats + products:
            for source in search['sources']:
                error = {'vinted': 'Seite gesperrt (Captcha)', 'wg-gesucht': 'Zeitüberschreitung'}.get(source)
                conn.execute(
                    'INSERT INTO source_status(search_id,source,checked_at,error,count,failures,started) '
                    'VALUES (?,?,?,?,?,?,1)',
                    (search['id'], source, NOW - 25 * 60, error, 0 if error else 24, 2 if error else 0),
                )
        for number, (subject, ago, count, status) in enumerate(
            (
                ('2 neue Wohnungen in Darmstadt', 2, 2, 'sent'),
                ('Resident Evil Requiem für 25,00 €', 3, 1, 'sent'),
                ('iPhone 15 128 GB für 390,00 €', 26, 1, 'failed'),
            )
        ):
            sent = NOW - ago * HOUR
            conn.execute(
                'INSERT INTO batches(id,recipients,subject,count,status,error,created_at,sent_at) VALUES (?,?,?,?,?,?,?,?)',
                (f'b{number}', '[]', subject, count, status, 'Anmeldung abgelehnt' if status == 'failed' else None,
                 sent, sent if status == 'sent' else None),
            )  # fmt: skip
        store.put_setting(conn, 'application_template', 'Hallo,\n\nich interessiere mich für die Wohnung …')

    for search, url in (
        (flats[0], 'https://example.invalid/wohnung/2'),
        (flats[0], 'https://example.invalid/wohnung/4'),
        (products[0], 'https://example.invalid/angebot/1'),
        (products[1], 'https://example.invalid/angebot/6'),
    ):
        _post(client, 'api/saved', dict(search_id=search['id'], url=url, saved=True))
    _post(client, 'api/applied', dict(search_id=flats[0]['id'], url='https://example.invalid/wohnung/4', applied=True))
    _post(
        client,
        'api/mail',
        dict(
            recipients=['du@example.invalid', 'mitbewohner@example.invalid'],
            wohnung=dict(enabled=True, frequency='Sofort', batch_size=2),
            preis=dict(enabled=True, frequency='Täglich', batch_size=1),
        ),
    )


# Server


def static_from(ref, target):
    """suchalarm/static as it was at the git REF (for "before" shots of the frontend), unpacked into TARGET."""
    archive = subprocess.run(
        ['git', 'archive', ref, 'suchalarm/static'], cwd=ROOT, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter='data')
    return target / 'suchalarm' / 'static'


def serve(data, static=None):
    from fastapi.staticfiles import StaticFiles

    data.mkdir()
    os.environ['DATA_DIR'] = str(data)
    for name in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_FROM'):
        os.environ.pop(name, None)
    from suchalarm import app as web
    from suchalarm import store

    @asynccontextmanager
    async def quiet(app):  # the real lifespan without worker and image downloads
        store.init()
        yield

    web.app.router.lifespan_context = quiet
    if static:  # another frontend against today's server
        web.STATIC = static
        mount = next(route for route in web.app.routes if getattr(route, 'name', '') == 'static')
        mount.app = StaticFiles(directory=static)  # type: ignore[attr-defined]
    seed()
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(web.app, host='127.0.0.1', port=PORT, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server


# Screenshots


async def shoot(out):
    from playwright.async_api import async_playwright

    base = f'http://127.0.0.1:{PORT}/'
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for device, size in VIEWPORTS.items():
            for area in AREAS:
                context = await browser.new_context(
                    viewport=dict(width=size['width'], height=size['height']),
                    device_scale_factor=size['scale'],
                    color_scheme='light',
                    locale='de-DE',
                    timezone_id='Europe/Berlin',
                    is_mobile=device == 'handy',
                    has_touch=device == 'handy',
                )
                # Nothing leaves this machine: only the local server answers.
                await context.route(
                    '**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort()
                )
                # A visit five hours ago, so newer hits carry their green dot.
                seen = json.dumps(dict(since=NOW - 5 * HOUR, left=NOW - 5 * HOUR, opened=[]))
                await context.add_init_script(
                    f'localStorage.setItem("suchalarm.area", {json.dumps(area)});'
                    f'if (!sessionStorage.getItem("shot")) {{ localStorage.setItem("suchalarm.seen", {json.dumps(seen)});'
                    'sessionStorage.setItem("shot", "1"); }'
                )
                page = await context.new_page()
                await page.goto(base)
                await page.wait_for_selector('#offer-list li')
                await page.evaluate('document.fonts.ready')
                for name, steps in views(area):
                    await page.goto(base)
                    await page.wait_for_selector('#offer-list li')
                    for step in steps:
                        await step(page)
                    await page.wait_for_timeout(250)
                    await capture(page, out / f'{device}-{area}-{name}.png', size)
                    print(f'{device}-{area}-{name}')
                await context.close()
        await browser.close()


def click(selector):
    async def run(page):
        await page.locator(selector).first.click()
        await page.wait_for_timeout(150)

    return run


def nav(view):
    async def run(page):
        target = page.locator(f'nav [data-view="{view}"]')
        await target.first.click()
        await page.wait_for_timeout(150)

    return run


def views(area):
    flats = area == 'wohnung'
    shots = [
        ('treffer', []),
        ('treffer-alle', [click('#tab-all')]),
        ('merkliste', [nav('saved')]),
        ('suchen', [nav('searches')]),
        ('editor-neu', [nav('searches'), click('#new-search')]),
        ('editor-bearbeiten', [nav('searches'), click('[data-edit]'), click('dialog[open] details.more summary')]),
    ]
    if flats:
        shots += [
            ('merkliste-beworben', [nav('saved'), click('#tab-applied')]),
            ('detail', [click('#offer-list .asset-row')]),
            ('bewerbung', [click('#offer-list .asset-row'), click('[data-sheet="apply"]')]),
        ]
    else:  # the settings are the same in both areas
        shots += [
            ('einstellungen-mails', [nav('settings')]),
            ('einstellungen-quellen', [nav('settings'), click('[data-settings-tab="sources"]')]),
            ('einstellungen-daten', [nav('settings'), click('[data-settings-tab="data"]')]),
            ('mailserver', [nav('settings'), click('#server-edit')]),
        ]
    return shots


async def capture(page, path, size):
    """The whole page in a window as tall as its content (an open dialog: as tall as the dialog), so the
    fixed navigation and sheets sit where they would at the end of the scroll."""
    dialog = page.locator('dialog[open]')
    if await dialog.count():
        height = await dialog.evaluate('d => { d.scrollTop = 0; return d.scrollHeight; }') + 160
    else:
        height = await page.evaluate('document.documentElement.scrollHeight')
    await page.set_viewport_size(dict(width=size['width'], height=max(size['height'], height)))
    await page.wait_for_timeout(200)
    await page.screenshot(path=path, animations='disabled')  # the new dot's pop would differ from shot to shot
    await page.set_viewport_size(dict(width=size['width'], height=size['height']))


# Comparison page


def changed_bands(first, second):
    """Where two screenshots differ: (top, bottom, left, right) boxes in pixels for each of them.

    Rows are matched like lines in a text diff, so content that only moved up or down counts as unchanged.
    A row that still differs is compared once more, softened and within 2 px up or down of where it moved:
    text that lands half a pixel apart after a shift is the same text, and stays unmarked."""
    from PIL import Image, ImageChops, ImageFilter

    a, b = Image.open(first).convert('RGB'), Image.open(second).convert('RGB')
    rows = lambda image: [image.crop((0, y, image.width, y + 1)).tobytes() for y in range(image.height)]  # noqa: E731
    soft = [image.convert('L').filter(ImageFilter.GaussianBlur(1)) for image in (a, b)]

    def differs(side, y, shift):
        """The column span where row Y of one side differs from the other side near Y + SHIFT, or None."""
        mine, other = soft[side], soft[1 - side]
        row, best = mine.crop((0, y, mine.width, y + 1)), None
        for there in range(y + shift - 2, y + shift + 3):
            if not 0 <= there < other.height or other.width != mine.width:
                continue
            diff = ImageChops.difference(row, other.crop((0, there, other.width, there + 1))).point(
                lambda v: v > 40 and 255
            )
            box = diff.getbbox()
            if box is None or sum(diff.histogram()[255:]) <= 5:
                return None
            best = box if best is None or box[2] - box[0] < best[2] - best[0] else best
        return (best[0], best[2]) if best else (0, mine.width)

    marks: tuple[list, list] = ([], [])
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, rows(a), rows(b), autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        for side, top, bottom, shift in ((0, i1, i2, j1 - i1), (1, j1, j2, i1 - j1)):
            found = marks[side]
            for y in range(top, bottom):
                span = differs(side, y, shift)
                if span is None:
                    continue
                if found and y - found[-1][1] < 24:  # close rows read as one change
                    last = found.pop()
                    found.append((last[0], y + 1, min(span[0], last[2]), max(span[1], last[3])))
                else:
                    found.append((y, y + 1, *span))
    return marks, (a.size, b.size)


def compare(before, after, target, notes=None):
    """A local page with each screenshot before and after side by side: changed regions boxed, views without
    any change marked as such, and the notes (JSON: {"zusammenfassung": [...], "ansichten": [{"muster":
    ["handy-*-treffer"], "text": "..."}]}) above the views they name."""
    notes = json.loads(notes.read_text(encoding='utf-8')) if notes else {}
    names = sorted({p.name for p in before.glob('*.png')} | {p.name for p in after.glob('*.png')})
    rel = lambda p: os.path.relpath(p, target.parent)  # noqa: E731
    sections, links = [], []
    for name in names:
        view = name[:-4]
        texts = [n['text'] for n in notes.get('ansichten', []) if any(fnmatch.fnmatch(view, m) for m in n['muster'])]
        both = (before / name).exists() and (after / name).exists()
        marks, sizes = changed_bands(before / name, after / name) if both else (([], []), ((1, 1), (1, 1)))
        same = both and not marks[0] and not marks[1]
        cells = ''
        for label, folder, found, (width, height) in zip(
            ('Vorher', 'Nachher'), (before, after), marks, sizes, strict=True
        ):
            boxes = ''.join(
                f'<i style="top:{top / height:.2%};height:{(bottom - top) / height:.2%};'
                f'left:{left / width:.2%};width:{(right - left) / width:.2%}"></i>'
                for top, bottom, left, right in found
            )
            image = (
                f'<div class="shot"><img src="{html.escape(rel(folder / name))}" loading="lazy" alt="">{boxes}</div>'
                if (folder / name).exists()
                else '<p>–</p>'
            )
            cells += f'<figure><figcaption>{label}</figcaption>{image}</figure>'
        status = (
            '<p class="same">Keine sichtbare Änderung.</p>'
            if same
            else ''.join(f'<p class="note">{html.escape(t)}</p>' for t in texts)
        )
        sections.append(
            f'<section id="{view}" class="{"same" if same else "changed"}"><h2>{html.escape(view)}</h2>'
            f'{status}<div class="pair">{cells}</div></section>'
        )
        links.append(f'<a href="#{view}" class="{"same" if same else "changed"}">{html.escape(view)}</a>')
    summary = ''.join(f'<li>{html.escape(t)}</li>' for t in notes.get('zusammenfassung', []))
    changed = sum('class="changed"' in s for s in sections)
    target.write_text(
        '<!doctype html><html lang="de"><meta charset="utf-8"><title>Suchalarm: Vorher/Nachher</title>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><style>'
        'body{font:15px/1.5 system-ui,sans-serif;margin:0 auto;padding:24px;max-width:1800px;background:#f4f4f5;color:#0a0a0a}'
        'nav{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:13px;margin:16px 0 24px}a{color:#1a47b8}'
        'nav a.same{color:#707077}nav a.changed::before{content:"● ";color:#1a47b8}'
        '.summary{background:#fff;border-radius:16px;padding:16px 24px;max-width:900px}.summary li{margin:4px 0}'
        'h2{font-size:15px;margin:40px 0 8px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}'
        '.note{margin:4px 0;padding:6px 12px;border-left:3px solid #1a47b8;background:#e7efff;border-radius:4px;max-width:900px}'
        '.same{color:#707077}figure{margin:0}figcaption{font-size:13px;font-weight:600;color:#636369;margin-bottom:4px}'
        '.shot{position:relative}.shot i{position:absolute;outline:2px solid #1a47b8;outline-offset:1px;'
        'background:rgb(26 71 184/.08);border-radius:4px;pointer-events:none}'
        'img{display:block;width:100%;border:1px solid #e9e9ec;border-radius:8px;background:#fff}'
        'section[id^=handy] .pair{grid-template-columns:repeat(2,minmax(0,420px))}'
        'body:has(#only:checked) section.same,body:has(#only:checked) nav a.same{display:none}'
        'body:has(#boxes:not(:checked)) .shot i{display:none}</style>'
        '<h1>Suchalarm – Vorher / Nachher</h1>'
        + (f'<ul class="summary">{summary}</ul>' if summary else '')
        + f'<p>{changed} von {len(names)} Ansichten geändert. Blaue Rahmen zeigen, wo sich etwas verändert hat '
        '(nur verschobene Inhalte zählen nicht).</p>'
        '<label><input type="checkbox" id="only"> Nur geänderte Ansichten</label> &nbsp; '
        '<label><input type="checkbox" id="boxes" checked> Rahmen zeigen</label>'
        f'<nav>{"".join(links)}</nav>{"".join(sections)}</html>\n',
        encoding='utf-8',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path, nargs='?', help='folder for the screenshots')
    parser.add_argument('--ref', help='frontend (suchalarm/static) from this git ref, e.g. origin/main')
    parser.add_argument('--compare', nargs=3, type=Path, metavar=('VORHER', 'NACHHER', 'HTML'))
    parser.add_argument('--notes', type=Path, help='with --compare: JSON with a summary and notes per view')
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare, notes=args.notes)
        return
    if not args.out:
        parser.error('Zielordner fehlt')
    with tempfile.TemporaryDirectory() as data:
        static = static_from(args.ref, Path(data) / 'ref') if args.ref else None
        server = serve(Path(data) / 'data', static)
        try:
            asyncio.run(shoot(args.out.resolve()))
        finally:
            server.should_exit = True


if __name__ == '__main__':
    main()
