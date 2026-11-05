import asyncio
import json
import subprocess
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from suchalarm.app import CHOICES
from suchalarm.preis.categories import SOURCES
from suchalarm.preis.sources import EXTRACT, parse_rows, search_url
from suchalarm.preis.worker import judged
from suchalarm.wohnung import portals
from suchalarm.wohnung import sources as flat_sources


class BrowserExtractionTests(unittest.IsolatedAsyncioTestCase):
    def test_idealo_shipping_uses_the_german_destination(self):
        def detail(country, rate):
            row = dict(shippingRate=dict(currency='EUR', value=rate))
            if country:
                row['shippingDestination'] = dict(addressCountry=country)
            return row

        cases = (
            ([detail('AT', '0'), detail('DE', '4.99')], '4.99'),
            ([detail('AT', '0'), detail('CH', '5')], None),
            ([detail(None, '0'), detail('AT', '5')], None),
            ([detail(None, '3.99')], '3.99'),
            ([detail('AT', '0')], None),
        )
        products = [
            {
                '@type': 'Product',
                'name': 'Spiel',
                'offers': dict(
                    price='30', priceCurrency='EUR', availability='https://schema.org/InStock', shippingDetails=details
                ),
            }
            for details, _ in cases
        ]
        # This branch reads JSON-LD only: no layout or browser is needed to exercise the real extractor.
        script = """
            const extract = eval(process.argv[1]);
            globalThis.location = {href: 'https://www.idealo.de/'};
            const results = JSON.parse(process.argv[2]).map(product => {
                globalThis.document = {
                    querySelectorAll: selector => selector === 'script[type="application/ld+json"]'
                        ? [{textContent: JSON.stringify(product)}] : [],
                    querySelector: () => null,
                };
                return extract('idealo').structured[0].shippingAmount;
            });
            process.stdout.write(JSON.stringify(results));
        """
        result = subprocess.run(
            ['node', '-e', script, EXTRACT, json.dumps(products)], check=True, capture_output=True, text=True
        )
        self.assertEqual(json.loads(result.stdout), [expected for _, expected in cases])

    async def test_ebay_extracts_the_whole_requested_page_including_its_cheapest_offer(self):
        page_size = int(parse_qs(urlparse(search_url('ebay', 'Spiel')).query)['_ipg'][0])
        self.assertEqual(page_size, 240)
        cards = ''.join(
            '<li class="s-card"><a class="s-card__link" '
            f'href="https://www.ebay.de/itm/{100000 + position}"><h3 class="s-card__title">Spiel</h3></a>'
            f'<span class="s-card__price">{20 if position == page_size - 1 else 35} €</span></li>'
            for position in range(page_size)
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(cards)
            data = await page.evaluate(EXTRACT, 'ebay')
            self.assertEqual(len(data['rows']), page_size)
            offers = parse_rows('ebay', data['rows'], dict(query='Spiel'))
            self.assertEqual(min(offer.price for offer in offers), 2000)
            self.assertEqual(offers[-1].url, f'https://www.ebay.de/itm/{100000 + page_size - 1}')
            await browser.close()

    async def test_kleinanzeigen_shipping_container_excludes_the_item_price(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            for label, expected in (
                ('Versand möglich', None),
                ('+ 4,50 € Versand', 450),
                ('Nur Abholung', None),
                ('', None),
            ):
                with self.subTest(label=label):
                    await page.set_content(
                        '<article class="aditem"><h3><a class="ellipsis" '
                        'href="https://www.kleinanzeigen.de/s-anzeige/ecksofa/12345-88-3331">Ecksofa</a></h3>'
                        '<div class="aditem-main--middle--price-shipping">'
                        '<p class="aditem-main--middle--price-shipping--price">100 €</p>'
                        f'<span>{label}</span></div></article>'
                    )
                    data = await page.evaluate(EXTRACT, 'kleinanzeigen')
                    self.assertEqual(data['rows'][0]['shipping'], label)
                    offer = parse_rows('kleinanzeigen', data['rows'], dict(query='Ecksofa', delivery='abholung'))[0]
                    self.assertEqual((offer.price, offer.shipping), (10000, expected))
            await browser.close()

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
            # Live layout of 29.10.2026: the owner only in the embedded page data, the condition as subtitle.
            await page.set_content(
                '<div data-testid="grid-item"><div data-testid="product-item-id-10580057914"><a href="https://www.vinted.de/items/10580057914-resident-evil-requiem" title="Résident evil requiem"></a><p data-testid="product-item-id-10580057914--description-title">Résident evil requiem</p><p data-testid="product-item-id-10580057914--description-subtitle">Sehr gut</p><p data-testid="product-item-id-10580057914--price-text">40,00&nbsp;€</p></div></div>'
                r'<script>self.__next_f.push([1,"{\"badge\":\"$undefined\",\"itemId\":\"10580057914\"}},\"catalogTracking\":{\"contentSource\":\"search\",\"ownerId\":57997699,\"searchScore\":1}"])</script>'
            )
            data = await page.evaluate(EXTRACT, 'vinted')
            offer = parse_rows('vinted', data['rows'], dict(query='Resident Evil Requiem'))[0]
            self.assertEqual((offer.owner, offer.condition), ('57997699', 'Sehr gut'))
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
            from suchalarm.preis.money import shipping

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
            await page.set_content(
                '<article class="aditem"><h2><a class="ellipsis" href="https://www.kleinanzeigen.de/s-anzeige/spiel/12345-279-123">Resident Evil Requiem PS5 verkauft</a></h2><p class="aditem-main--middle--description">Verkauft wird mein Spiel. Festpreis, kein VB.</p><p class="aditem-main--middle--price-shipping--price">24 €</p><span class="simpletag">Reserviert</span></article>'
            )
            data = await page.evaluate(EXTRACT, 'kleinanzeigen')
            self.assertNotIn('Verkauft wird', data['rows'][0]['status'])
            self.assertIn('Reserviert', data['rows'][0]['status'])
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
            await self.immowelt_list(page)
            await browser.close()

    async def immowelt_list(self, page):
        """Immowelt's list, asked for from its result page: the search, then the offers' contents."""
        offers = (Path(__file__).parent / 'fixtures' / 'immowelt' / 'list.json').read_text()
        asked, refuse = [], []
        ids = [f'26A{n:09d}' for n in range(31)]  # one more than a request for their contents carries

        async def answer(route):
            request = route.request
            asked.append((urlparse(request.url).path, request.post_data, request.headers.get('x-language')))
            if request.url.endswith('/serp-bff/search'):
                if refuse:
                    await route.fulfill(status=403, body='')
                else:
                    await route.fulfill(json={'totalCount': 31, 'classifieds': [{'id': i} for i in ids]})
            elif '/classifiedList/' in request.url:
                await route.fulfill(content_type='application/json', body=offers)
            else:
                await route.fulfill(content_type='text/html', body='<title>Wohnungen zur Miete</title><p>Liste</p>')

        await page.route('https://www.immowelt.de/**', answer)
        path = '/suche/mieten/wohnung/hessen/darmstadt-64283/ad08de2508'
        address, request = portals.immowelt_search(path, {'maxRent': 900, 'minArea': 40, 'minRooms': 2})
        await page.goto(address)
        data = await page.evaluate(flat_sources._immowelt_list(request), 'immowelt')
        self.assertEqual(data['total'], 31)
        self.assertEqual(len(portals.immowelt_cards(data['rows'])), 6)  # the fixture's 3 offers per answer
        search, *contents = asked[1:]
        self.assertEqual(json.loads(search[1]), request)
        self.assertEqual(
            contents,
            [('/classifiedList/' + ','.join(ids[:30]), None, 'de'), ('/classifiedList/' + ids[30], None, 'de')],
        )
        refuse.append(True)
        data = await page.evaluate(flat_sources._immowelt_list(request), 'immowelt')
        self.assertIsNone(data['total'])
        self.assertRegex(data['text'], flat_sources.browser.BLOCK)


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
            offer = dict(
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

            def judge():  # the server judges each offer by its search (preis/worker.judged)
                state['offers'] = [judged(offer, search)]

            judge()
            await page.locator('[data-view="offers"]').first.click()
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 1)
            self.assertEqual(await page.locator('#offer-list .asset-value.over').count(), 0)
            search['include_shipping'] = 1
            judge()
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 0)
            offer['shipping'] = 818
            judge()
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_timeout(300)
            self.assertEqual(await page.locator('#offer-list .asset-row').count(), 0)  # 48.84 exceeds 45
            search['max_price'] = 5000
            judge()
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
                         shipping=0, seller='', condition='Gebraucht', first_seen=now, last_seen=now, start=False,
                         compared=2999, fits=True, with_shipping=True)],
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
        template_started, template_release = asyncio.Event(), asyncio.Event()

        async def serve(route):
            path = urlparse(route.request.url).path
            if path == '/api/state':
                await route.fulfill(json=state)
                return
            if path == '/api/template':
                template_started.set()
                await template_release.wait()
                state['template'] = route.request.post_data_json['text']
                await route.fulfill(json={'ok': True})
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
            await page.keyboard.press('Escape')
            await page.locator('[data-view="settings"]').first.click()
            await page.locator('[data-settings-tab="data"]').click()
            await page.locator('#template-edit').click()
            await page.locator('#template-text').fill('Meine Bewerbung')
            await page.locator('#template-save').click()
            await asyncio.wait_for(template_started.wait(), timeout=5)
            self.assertTrue(await page.locator('#template-text').is_disabled())
            await page.keyboard.press('Escape')
            try:
                self.assertTrue(await page.locator('#template-edit').is_disabled())
            finally:
                template_release.set()
            await page.wait_for_function(
                '!document.getElementById("template-edit").disabled && document.getElementById("template-edit").textContent === "Bearbeiten"'
            )
            await page.locator('#template-edit').click()
            self.assertEqual(await page.locator('#template-text').input_value(), 'Meine Bewerbung')
            self.assertTrue(await page.locator('#template-text').is_enabled())
            await browser.close()
        self.assertEqual(errors, [])


class FrontendRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from suchalarm.app import ALL_SOURCES, SOURCE_KINDS

        now = time.time()
        self.listing = dict(
            search_id=1,
            kind='wohnung',
            source='immoscout24',
            url='https://www.immobilienscout24.de/expose/1',
            title='Helle Wohnung',
            place='Darmstadt',
            price=100000,
            area=55,
            rooms=2,
            status='matched',
            reasons=['Alle Filter erfüllt'],
            traits=[],
            first_seen=now,
            last_seen=now,
            start=False,
        )
        self.state = dict(
            searches=[dict(id=1, kind='wohnung', query='Darmstadt', max_price=130000, radius=0, excluded='',
                           sources=['immoscout24'], active=1, filters={})],
            offers=[], listings=[self.listing], saved=[], statuses=[], mail_log=[], paused=False,
            running=None, running_source=None, queued=[], schedule=[], sources=ALL_SOURCES,
            source_kinds=SOURCE_KINDS, choices=CHOICES,
            mail=dict(recipients=['a@example.org', 'b@example.org'], configured=True, error=None,
                      pending=dict(preis=0, wohnung=0),
                      server=dict(sender='', host='', port=587, password_set=False, fixed=False),
                      preis=dict(enabled=True, frequency='Sofort', batch_size=1),
                      wohnung=dict(enabled=True, frequency='Sofort', batch_size=1)),
        )  # fmt: skip
        self.actions, self.errors = [], []
        static = Path(__file__).resolve().parents[1] / 'suchalarm' / 'static'

        async def serve(route):
            path = urlparse(route.request.url).path
            if path == '/api/state':
                await route.fulfill(json=self.state)
            elif path == '/api/location':
                await route.fulfill(body='null', content_type='application/json')
            elif path in ('/api/hidden', '/api/saved'):
                self.actions.append((path, route.request.method, route.request.post_data_json))
                await route.fulfill(json={'ok': True})
            else:
                file = static / (path.removeprefix('/static/') if path.startswith('/static/') else 'index.html')
                types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'}
                await route.fulfill(
                    body=file.read_bytes(), content_type=types.get(file.suffix, 'application/octet-stream')
                )

        playwright = await async_playwright().start()
        self.addAsyncCleanup(playwright.stop)
        browser = await playwright.chromium.launch()
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page(has_touch=True)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        await self.page.route('http://suchalarm.test/**', serve)
        await self.page.add_init_script("localStorage.setItem('suchalarm.area', 'wohnung')")
        await self.page.goto('http://suchalarm.test/')
        await self.page.locator('#offer-list .asset-row').wait_for()

    async def asyncTearDown(self):
        self.assertEqual(self.errors, [])

    async def test_pointercancel_resets_swipe_but_pointerup_commits_in_both_directions(self):
        page = self.page
        touch = await page.context.new_cdp_session(page)

        async def drag(direction):
            box = await page.locator('#offer-list .asset-row').bounding_box()
            x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            await touch.send('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=[dict(x=x, y=y)]))
            await touch.send(
                'Input.dispatchTouchEvent', dict(type='touchMove', touchPoints=[dict(x=x + direction * 160, y=y)])
            )
            # Real touch input gives setPointerCapture a live pointer; 160 px exceeds SWIPE_ARM.
            await page.wait_for_function("document.querySelector('#offer-list li').classList.contains('armed')")
            self.assertTrue(await page.locator('#offer-list li').evaluate("li => li.classList.contains('swiping')"))
            self.assertGreater(
                await page.locator('#offer-list .asset-row').evaluate(
                    'row => Math.abs(new DOMMatrix(getComputedStyle(row).transform).m41)'
                ),
                80,
            )

        for direction, path, flag in ((-1, '/api/hidden', 'hidden'), (1, '/api/saved', 'saved')):
            with self.subTest(direction=direction):
                await page.reload()
                await page.locator('#offer-list .asset-row').wait_for()
                self.actions.clear()
                await drag(direction)
                await touch.send('Input.dispatchTouchEvent', dict(type='touchCancel', touchPoints=[]))
                # Hiding posts after 200 + 220 ms, saving after 260 ms: observe beyond both timers.
                await page.wait_for_timeout(600)
                self.assertEqual(self.actions, [])
                self.assertEqual(await page.locator('#offer-list .asset-row').count(), 1)
                self.assertEqual(
                    await page.locator('#offer-list .asset-row').evaluate('row => row.style.transform'), ''
                )
                self.assertEqual(
                    await page.locator('#offer-list .asset-row').evaluate(
                        'row => new DOMMatrix(getComputedStyle(row).transform).m41'
                    ),
                    0,
                )
                self.assertFalse(
                    await page.locator('#offer-list li').evaluate(
                        "li => ['swiping', 'armed', 'to-left', 'to-right', 'leaving'].some(c => li.classList.contains(c))"
                    )
                )
                await drag(direction)
                async with page.expect_request(f'http://suchalarm.test{path}') as requested:
                    await touch.send('Input.dispatchTouchEvent', dict(type='touchEnd', touchPoints=[]))
                request = await requested.value
                self.assertEqual(request.method, 'POST')
                self.assertEqual(request.post_data_json, dict(search_id=1, url=self.listing['url'], **{flag: True}))
                await page.wait_for_timeout(600)
                self.assertEqual(len(self.actions), 1)

    async def test_application_mark_only_confirms_success_after_a_successful_post(self):
        page = self.page
        outcome = 'http'
        submitted = []

        async def applied(route):
            submitted.append((route.request.method, route.request.post_data_json))
            if outcome == 'http':
                await route.fulfill(status=500, json={'detail': 'Bewerbung konnte nicht gespeichert werden.'})
            elif outcome == 'network':
                await route.abort('failed')
            else:
                self.state['saved'] = [dict(self.listing, saved_at=time.time(), applied_at=time.time())]
                await route.fulfill(json={'ok': True})

        await page.route('http://suchalarm.test/api/applied', applied)
        for failure in ('http', 'network'):
            with self.subTest(failure=failure):
                outcome = failure
                self.state['saved'] = []
                await page.reload()
                await page.locator('#offer-list .asset-row').click()
                await page.locator('#detail [data-sheet="apply"]').click()
                feedback = page.locator('#application-feedback')
                before = await feedback.inner_text()
                await page.locator('#application-mark').click()
                await page.wait_for_function(
                    "!document.getElementById('message').hidden && document.getElementById('message').classList.contains('down')"
                )
                # Let mark() resume after act() has caught the error before checking the negative assertion.
                await page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
                self.assertEqual(await feedback.inner_text(), before)
                self.assertNotIn('Als beworben markiert und gemerkt.', await feedback.inner_text())
                self.assertEqual(submitted[-1], ('POST', dict(search_id=1, url=self.listing['url'], applied=True)))
                self.assertTrue(await page.locator('#application-mark').is_visible())
                self.assertTrue(await page.locator('#application-applied').is_hidden())

                outcome = 'success'
                await page.locator('#application-mark').click()
                await page.wait_for_function(
                    "document.getElementById('application-feedback').textContent === 'Als beworben markiert und gemerkt.'"
                )
                self.assertTrue(await page.locator('#application-applied').is_visible())
                self.assertTrue(await page.locator('#application-mark').is_hidden())

    async def test_stale_state_after_mail_save_cannot_restore_a_removed_recipient(self):
        page = self.page
        await page.locator('[data-view="settings"]').first.click()
        old_started, fresh_started = asyncio.Event(), asyncio.Event()
        held = []
        submitted = []

        async def state(route):
            if len(held) < 2:
                # Snapshot at request time: the old poll sees a+b, the post-save refresh sees a.
                held.append((route, json.dumps(self.state)))
                (old_started if len(held) == 1 else fresh_started).set()
            else:
                await route.fulfill(json=self.state)

        async def mail(route):
            self.assertEqual(route.request.method, 'PUT')
            body = route.request.post_data_json
            submitted.append(body)
            self.state['mail'].update(body)
            await route.fulfill(json={'ok': True})

        await page.route('http://suchalarm.test/api/state', state)
        await page.route('http://suchalarm.test/api/mail', mail)
        # Start the real refresh, but do not await its deliberately held response yet.
        await page.evaluate("() => { window.oldPoll = import('/static/page.js').then(m => m.refresh()); }")
        await asyncio.wait_for(old_started.wait(), timeout=5)
        await page.get_by_role('button', name='b@example.org entfernen', exact=True).click()
        await asyncio.wait_for(fresh_started.wait(), timeout=5)
        self.assertEqual(submitted[0]['recipients'], ['a@example.org'])

        # PUT has completed. Deliver the pre-save poll while the subsequent refresh is still in flight.
        old_route, old_body = held[0]
        await old_route.fulfill(body=old_body, content_type='application/json')
        await page.evaluate('() => window.oldPoll')  # includes parsing and rendering the stale state
        async with page.expect_request('http://suchalarm.test/api/mail') as requested:
            await page.locator('[data-mail-area="wohnung"] [data-mail="frequency"]').select_option('Täglich')
        request = await requested.value
        self.assertEqual(request.method, 'PUT')
        self.assertEqual(request.post_data_json['wohnung']['frequency'], 'Täglich')
        self.assertEqual(request.post_data_json['recipients'], ['a@example.org'])
        self.assertEqual(await page.locator('#recipient-items [data-remove]').count(), 1)
        await page.wait_for_function("!document.getElementById('mail-form').dataset.saving")
        fresh_route, fresh_body = held[1]
        await fresh_route.fulfill(body=fresh_body, content_type='application/json')


if __name__ == '__main__':
    unittest.main()
