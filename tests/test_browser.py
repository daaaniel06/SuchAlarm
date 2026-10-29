import time
import unittest
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from suchalarm.app import CHOICES
from suchalarm.preis.domain import SOURCES
from suchalarm.preis.sources import EXTRACT, parse_rows
from suchalarm.wohnung import portals
from suchalarm.wohnung import sources as flat_sources


class BrowserExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rendered_provider_cards(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            fixtures = {
                'ebay': '<li class="s-item"><a class="s-item__link" href="https://www.ebay.de/itm/12345"><h3 class="s-item__title">Resident Evil Requiem PS5</h3></a><span class="s-item__price">29,99 €</span><span class="s-item__shipping">Kostenloser Versand</span></li>',
                'kleinanzeigen': '<article class="aditem"><h2><a class="ellipsis" href="https://www.kleinanzeigen.de/s-anzeige/resident-evil/1234567890-279-1234">Resident Evil Requiem PS5</a></h2><p class="aditem-main--middle--price-shipping--price">25 €</p><p class="aditem-main--middle--price-shipping">25 € + 4,50 € Versand</p></article>',
                'vinted': '<div data-testid="grid-item"><a href="https://www.vinted.de/items/12345-resident-evil" title="Resident Evil Requiem PS5">Resident Evil Requiem PS5</a><p data-testid="product-item-id-12345--price-text">28,00 €</p></div>',
            }
            for source, html in fixtures.items():
                await page.set_content(html)
                data = await page.evaluate(EXTRACT, source)
                offers = parse_rows(source, data['rows'], dict(query='Resident Evil Requiem PS5', excluded=''))
                self.assertEqual(len(offers), 1, source)
                self.assertGreater(offers[0].price, 0)
            for layout in ('s-item', 's-card'):
                await page.set_content(
                    f'<li class="{layout}"><a class="{layout}__link" href="https://www.ebay.de/itm/12345"><h3 class="{layout}__title"><span class="{layout}__title--tagblock">Neues Angebot</span><span>Resident Evil Requiem PS5 – gebraucht</span><span class="clipped">Wird in neuem Fenster oder Tab geöffnet</span></h3></a><span class="{layout}__price">30 €</span></li>'
                )
                data = await page.evaluate(EXTRACT, 'ebay')
                self.assertEqual(data['rows'][0]['title'], 'Resident Evil Requiem PS5 – gebraucht')
            await page.set_content(
                '<li class="s-card"><a class="s-card__link" href="https://www.ebay.de/itm/12345"><h3 class="s-card__title">Resident Evil Requiem PS5 Gratis Lieferung</h3></a><span class="s-card__price">EUR 40,66</span><div class="s-card__attribute-row">+EUR 8,18 Lieferung</div><div>Kostenloser Rückversand</div></li>'
            )
            data = await page.evaluate(EXTRACT, 'ebay')
            self.assertEqual(parse_rows('ebay', data['rows'], dict(query='Resident Evil Requiem PS5'))[0].shipping, 818)
            # Live layout of 27.10.2026: platform and condition are subtitles, the seller is the rating row.
            await page.set_content(
                '<li class="s-card"><a class="s-card__link" href="https://www.ebay.de/itm/12345"><h3 class="s-card__title">Resident Evil Requiem PS5</h3></a><div class="s-card__subtitle-row"><span class="s-card__subtitle">Sony PlayStation 5</span><span class="s-card__subtitle">Gebraucht | Privat</span></div><span class="s-card__price">EUR 44,00</span><div class="s-card__attribute-row">+EUR 6,19 · 2-3 Tage Lieferung</div><div class="s-card__attribute-row">truesior_8 100% positiv (253)</div></li>'
            )
            data = await page.evaluate(EXTRACT, 'ebay')
            offer = parse_rows('ebay', data['rows'], dict(query='Resident Evil Requiem PS5'))[0]
            self.assertEqual((offer.seller, offer.condition, offer.shipping), ('truesior_8', 'Gebraucht', 619))
            await page.set_content(
                '<div data-testid="grid-item"><a href="https://www.vinted.de/items/12345" title="Resident Evil Requiem PS5">Resident Evil Requiem PS5</a><p data-testid="item-price">44,00 €</p><p>46,90 € inkl.</p><p data-testid="item-shipping-price">ab 2,49 €</p></div>'
            )
            data = await page.evaluate(EXTRACT, 'vinted')
            self.assertEqual(
                parse_rows('vinted', data['rows'], dict(query='Resident Evil Requiem PS5'))[0].shipping, 249
            )
            await page.route(
                'https://www.vinted.de/items/12345',
                lambda route: route.fulfill(
                    content_type='text/html; charset=utf-8',
                    body='<h1 data-testid="item-title">Spiel PS5</h1><div data-testid="item-shipping-banner"><h3 data-testid="item-shipping-banner-title">Versand</h3><span data-testid="item-shipping-banner-price">ab 0,00&nbsp;€</span></div><div data-testid="item-description">Versand 99,00 €</div>',
                ),
            )
            await page.goto('https://www.vinted.de/items/12345')
            data = await page.evaluate(EXTRACT, 'vinted')
            self.assertTrue(data['vintedDetail'])
            from suchalarm.preis.domain import shipping

            self.assertEqual(shipping(data['vintedShipping'], allow_from=True), 0)
            await page.set_content(
                '<article data-adid="12345"><h3><a href="https://www.kleinanzeigen.de/s-anzeige/spiel/12345-279-123">Resident Evil Requiem PS5</a></h3><p class="my-xsmall text-title3 font-strong">24 €</p></article>'
            )
            data = await page.evaluate(EXTRACT, 'kleinanzeigen')
            self.assertEqual(
                parse_rows('kleinanzeigen', data['rows'], dict(query='Resident Evil Requiem PS5', excluded=''))[
                    0
                ].price,
                2400,
            )
            for location, count in (
                ('aus Deutschland', 1),
                ('aus Vereinigte Staaten von Amerika', 0),
                ('aus Kanada', 0),
            ):
                await page.set_content(
                    f'<li class="s-item"><a class="s-item__link" href="https://www.ebay.de/itm/12345"><h3 class="s-item__title">Resident Evil Requiem PS5</h3></a><span class="s-item__price">30 €</span><span class="s-item__location">{location}</span></li>'
                )
                data = await page.evaluate(EXTRACT, 'ebay')
                self.assertEqual(data['rows'][0]['location'], location)
                self.assertEqual(
                    len(parse_rows('ebay', data['rows'], dict(query='Resident Evil Requiem PS5', excluded=''))), count
                )
            await page.set_content(
                '<script type="application/ld+json">{"@type":"Product","name":"Resident Evil Requiem PS5","offers":{"@type":"AggregateOffer","lowPrice":"39.95","priceCurrency":"EUR"}}</script>'
            )
            data = await page.evaluate(EXTRACT, 'idealo')
            self.assertTrue(data['structured'][0]['reference'])
            await page.evaluate('document.body.remove()')
            loading = await page.evaluate(EXTRACT, 'ebay')
            self.assertEqual(loading['text'], '')
            # An Immowelt exposé: only its sections, which the parser reads again.
            expose = (Path(__file__).parent / 'fixtures' / 'immowelt' / 'expose_warm.html').read_text()
            await page.set_content(f'<header>Menü</header><main>{expose}</main><footer>Impressum</footer>')
            data = await page.evaluate(flat_sources.IMMOWELT_EXPOSE, 'immowelt')
            self.assertNotIn('Impressum', data['html'])
            self.assertEqual(portals.immowelt_detail(data['html'])['rent'], 770)
            await browser.close()


class QueueInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_editor_has_no_interval_and_waiting_queue_is_not_running(self):
        now = time.time()
        search = dict(
            id=1,
            kind='preis',
            query='Spiel PS5',
            min_price=0,
            max_price=3500,
            include_shipping=1,
            excluded='',
            sources=['ebay'],
            active=1,
            reference=None,
        )
        state = dict(
            searches=[search],
            offers=[],
            listings=[],
            saved=[],
            statuses=[dict(search_id=1, source='ebay', checked_at=now, error=None, count=0)],
            mail_log=[],
            paused=False,
            running=None,
            running_source=None,
            queued=[],
            sources=SOURCES,
            source_kinds=dict.fromkeys(SOURCES, 'preis'),
            choices=CHOICES,
            schedule=[dict(search_id=1, source='ebay', at=now, next_at=now + 1200, state='ok')],
            mail=dict(
                recipients=[],
                configured=False,
                error=None,
                pending=dict(preis=0, wohnung=0),
                preis=dict(enabled=False, frequency='Sofort', batch_size=1),
                wohnung=dict(enabled=False, frequency='Sofort', batch_size=1),
            ),
        )
        submitted, errors, manual_calls = [], [], []
        static = Path(__file__).resolve().parents[1] / 'suchalarm' / 'static'

        async def serve(route):
            path = urlparse(route.request.url).path
            if path == '/api/state':
                await route.fulfill(json=state)
            elif path == '/api/searches/1/check':
                manual_calls.append(path)
                state['queued'] = [1]
                state['queued_jobs'] = [dict(search_id=1, source='ebay')]
                await route.fulfill(json={'queued': True, 'started': True})
            elif path == '/api/searches/1':
                submitted.append(route.request.post_data_json)
                await route.fulfill(json={'ok': True})
            else:
                file = static / (path.removeprefix('/static/') if path.startswith('/static/') else 'index.html')
                types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'}
                await route.fulfill(
                    body=file.read_bytes(), content_type=types.get(file.suffix, 'application/octet-stream')
                )

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('http://suchalarm.test/**', serve)
            await page.add_init_script("localStorage.setItem('suchalarm.area', 'preis')")  # a product search
            await page.goto('http://suchalarm.test/')
            await page.locator('[data-view="searches"]').first.click()
            await page.locator('[data-edit="1"]').click()
            self.assertEqual(await page.locator('#interval').count(), 0)
            await page.locator('#max-price').fill('36')
            await page.locator('#save-search').click()
            await page.wait_for_function('!document.getElementById("editor").open')
            self.assertEqual(submitted[0]['max_price'], 3600)
            self.assertNotIn('interval_minutes', submitted[0])
            self.assertEqual((submitted[0]['category'], submitted[0]['delivery']), ('sonstiges', 'versand'))
            state['queued'] = [1]
            state['queued_jobs'] = [dict(search_id=1, source='ebay')]
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertIn('prüfung startet', (await page.locator('#summary').inner_text()).lower())
            self.assertEqual(await page.locator('#search-list .source-step.waiting').count(), 1)
            # Screenshot regression: an eBay source error must not exclude a
            # matching item when shipping is disabled.
            search.update(min_price=2000, max_price=4500, include_shipping=0)
            state['statuses'][0]['error'] = 'Pause bis 14:45'
            state['offers'] = [
                dict(
                    search_id=1,
                    source='ebay',
                    url='https://www.ebay.de/itm/198728514130',
                    title='Capcom Resident Evil Requiem PS5',
                    price=4066,
                    shipping=None,
                    seller='',
                    condition='',
                    first_seen=now - 3600,
                    last_seen=now - 3600,
                )
            ]
            await page.locator('[data-view="offers"]').first.click()
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 1)
            self.assertEqual(await page.locator('#offer-list .asset-value.over').count(), 0)
            search['include_shipping'] = 1
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 0)
            state['offers'][0]['shipping'] = 818
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 0)  # 48.84 exceeds 45
            search['max_price'] = 5000
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 1)
            state['queued'] = []
            state['queued_jobs'] = []
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            await page.evaluate(
                "const b=document.getElementById('check-all'); b.dispatchEvent(new Event('click')); b.dispatchEvent(new Event('click'));"
            )
            await page.wait_for_function('!document.getElementById("progress").hidden')  # the click is queued
            self.assertEqual(len(manual_calls), 1)
            self.assertTrue(await page.locator('#check-all').is_disabled())
            self.assertEqual(errors, [])
            await browser.close()


class AreaInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_area_at_a_time_with_the_same_rows(self):
        from suchalarm.app import ALL_SOURCES, SOURCE_KINDS

        now = time.time()
        flat_search = dict(id=1, kind='wohnung', query='Darmstadt', max_price=130000, radius=0, excluded='',
                           sources=['immoscout24'], active=1, filters={'min_area': 40, 'min_rooms': 2})  # fmt: skip
        product = dict(id=2, kind='preis', query='Spiel PS5', min_price=0, max_price=3500, include_shipping=1,
                       excluded='', sources=['ebay'], active=1, reference=None)  # fmt: skip
        state = dict(
            searches=[product, flat_search],
            offers=[dict(search_id=2, source='ebay', url='https://www.ebay.de/itm/1', title='Spiel PS5', price=2999,
                         shipping=0, seller='', condition='Gebraucht', first_seen=now, last_seen=now, start=False)],
            listings=[dict(search_id=1, kind='wohnung', source='immoscout24', url='https://www.immobilienscout24.de/expose/1',
                           title='Helle Wohnung', place='Karlstraße 22, 64283 Darmstadt, Kapellplatzviertel', price=101500,
                           estimated=True, area=55, rooms=2, status='matched', reasons=['Alle Filter erfüllt'],
                           first_seen=now, last_seen=now, start=False)],
            saved=[], statuses=[], mail_log=[], paused=False, running=None, running_source=None, queued=[],
            sources=ALL_SOURCES, source_kinds=SOURCE_KINDS, choices=CHOICES, schedule=[],
            mail=dict(recipients=[], configured=False, error=None, pending=dict(preis=0, wohnung=0),
                      server=dict(sender='', host='', port=587, password_set=False, fixed=False),
                      preis=dict(enabled=False, frequency='Sofort', batch_size=1),
                      wohnung=dict(enabled=False, frequency='Sofort', batch_size=1)),
        )  # fmt: skip
        static = Path(__file__).resolve().parents[1] / 'suchalarm' / 'static'
        errors = []

        async def serve(route):
            path = urlparse(route.request.url).path
            if path == '/api/state':
                await route.fulfill(json=state)
                return
            file = static / (path.removeprefix('/static/') if path.startswith('/static/') else 'index.html')
            types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'}
            await route.fulfill(body=file.read_bytes(), content_type=types.get(file.suffix, 'application/octet-stream'))

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('http://suchalarm.test/**', serve)
            await page.goto('http://suchalarm.test/')
            await page.wait_for_function('document.getElementById("offer-count").textContent === "1"')
            self.assertEqual(await page.locator('#offer-unit').inner_text(), 'Wohnung')
            row = await page.locator('#offer-list li').first.inner_text()
            self.assertIn('Kapellplatzviertel · 55 m² · 2 Zi.', row)
            self.assertIn('≈ 1.015,00', row)  # an estimated rent reads "≈ … €" in grey
            self.assertEqual(await page.locator('#offer-list .source-tag').first.inner_text(), 'ImmoScout24')
            await page.keyboard.press('2')
            self.assertEqual(await page.locator('#offer-unit').inner_text(), 'Angebot')
            self.assertEqual(await page.locator('[data-area="preis"]').get_attribute('aria-pressed'), 'true')
            await page.locator('[data-view="searches"]').first.click()
            self.assertEqual(await page.locator('#new-search').get_attribute('aria-label'), 'Neue Preissuche')
            await page.keyboard.press('1')
            self.assertEqual(await page.locator('#new-search').get_attribute('aria-label'), 'Neue Wohnungssuche')
            await page.locator('#new-search').click()
            self.assertTrue(await page.locator('#flat-editor').evaluate('dialog => dialog.open'))
            await page.keyboard.press('2')  # a key in an open dialog never switches the area
            self.assertEqual(await page.locator('[data-area="wohnung"]').get_attribute('aria-pressed'), 'true')
            self.assertEqual(await page.evaluate("localStorage.getItem('suchalarm.area')"), 'wohnung')
            await browser.close()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
