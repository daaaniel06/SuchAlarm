import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from invisible_playwright._pw.async_api import Error as PlaywrightError

from suchalarm import mailer, store
from suchalarm.core import browser, cadence
from suchalarm.preis import delivery, sources
from suchalarm.preis import worker as products
from suchalarm.preis.domain import HOSTS, Offer, eligible, select_offers, shipping
from suchalarm.preis.sources import load_page, parse_rows, row_shipping


class SourceLoadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Request budgets and the page cache live in DATA_DIR; every test gets its own.
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_follow_up_page_obeys_spacing_and_is_counted_separately(self):
        detail = self.listing()
        await load_page(
            self.page([self.listing()]),
            'idealo',
            'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q=x',
        )
        with self.assertRaises(cadence.Deferred) as waiting:
            await load_page(
                self.page([detail]),
                'idealo',
                'https://www.idealo.de/preisvergleich/OffersOfProduct/1_x.html',
                follow=True,
            )
        self.assertTrue(waiting.exception.continuation)
        with patch('suchalarm.core.cadence.time.time', return_value=waiting.exception.until + 1):
            self.assertEqual(
                await load_page(
                    self.page([detail]),
                    'idealo',
                    'https://www.idealo.de/preisvergleich/OffersOfProduct/1_x.html',
                    follow=True,
                ),
                detail,
            )
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 2)
        with self.assertRaises(cadence.Deferred):
            await load_page(
                self.page([detail]), 'idealo', 'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q=y'
            )

    def page(self, results, status=200):
        return SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=status)),
            evaluate=AsyncMock(side_effect=results),
            wait_for_timeout=AsyncMock(),
        )

    def listing(self):
        return {'rows': [{'title': 'Spiel'}], 'structured': [], 'links': [], 'title': 'Angebote', 'text': 'Spiel'}

    async def test_redirect_and_empty_document_settle_before_reading(self):
        empty = {'rows': [], 'structured': [], 'links': [], 'title': '', 'text': ''}
        result = self.listing()
        page = self.page(
            [PlaywrightError('Execution context was destroyed, most likely because of a navigation'), empty, result]
        )
        self.assertEqual(await load_page(page, 'ebay', 'https://www.ebay.de/'), result)
        self.assertEqual(page.wait_for_timeout.await_count, 2)

    async def test_provider_denial_is_not_reported_as_no_offers(self):
        page = self.page([], status=403)
        with self.assertRaisesRegex(RuntimeError, 'blockiert.*403'):
            await load_page(page, 'ebay', 'https://www.ebay.de/')
        page.evaluate.assert_not_awaited()

    async def test_akamai_challenge_reloads_once_in_the_same_browser(self):
        challenge = SimpleNamespace(status=403, headers={'server': 'AkamaiGHost'})
        page = self.page([self.listing()])
        page.goto.side_effect = [challenge, SimpleNamespace(status=200)]
        self.assertEqual(await load_page(page, 'ebay', 'https://www.ebay.de/'), self.listing())
        self.assertEqual(page.goto.await_count, 2)

    async def test_repeated_akamai_refusal_is_a_block(self):
        challenge = SimpleNamespace(status=403, headers={'server': 'AkamaiGHost'})
        page = self.page([])
        page.goto.side_effect = [challenge, challenge]
        with self.assertRaises(browser.Blocked):
            await load_page(page, 'ebay', 'https://www.ebay.de/')
        self.assertEqual(page.goto.await_count, 2)

    async def test_unauthorized_counts_as_a_provider_block(self):
        with self.assertRaises(browser.Blocked):
            await load_page(self.page([], status=401), 'vinted', 'https://www.vinted.de/items/1')

    async def test_unsettled_vinted_detail_reports_unreadable_page(self):
        lost = PlaywrightError('Execution context was destroyed, most likely because of a navigation')
        page = self.page([lost] * 16)
        with self.assertRaisesRegex(RuntimeError, 'nicht lesbar'):
            await load_page(page, 'vinted', 'https://www.vinted.de/items/1', follow=True)

    def test_unmatched_idealo_term_is_no_provider_refusal(self):
        data = {'structured': [], 'links': [], 'text': 'Angebote'}
        with self.assertRaises(RuntimeError) as error:
            sources.idealo_result(data, None, None, {'query': 'Spiel PS5'})
        self.assertNotIsInstance(error.exception, cadence.Refused)

    def test_idealo_always_counts_its_cheapest_total_with_shipping(self):
        product = {'title': 'Spiel PS5', 'url': 'https://www.idealo.de/preisvergleich/OffersOfProduct/2_x.html'}
        data = {
            'structured': [dict(reference=True, currency='EUR', price='25.00', title='Spiel PS5', url=product['url'])],
            'links': [product],
            'text': 'Spiel PS5',
        }
        detail = {
            'structured': [],
            'rows': [
                dict(title='x', price='30,00 €', seller='Shop A', text='lieferbar 39,00 € inkl. Versand'),
                dict(title='x', price='32,00 €', seller='Shop B', text='lieferbar 32,00 € inkl. Versand'),
            ],
        }
        offers, reference = sources.idealo_result(data, detail, product, {'query': 'Spiel PS5'})
        # The yardstick is the cheapest real total, not idealo's "ab" price without shipping.
        self.assertEqual((reference['price'], reference['shipping_included']), (3200, True))
        # Even a search that leaves shipping out picks and compares the idealo shop by its total.
        search = {'query': 'Spiel PS5', 'include_shipping': False, 'min_price': 0, 'max_price': 3500}
        chosen = select_offers(offers, search)
        self.assertEqual([(o.seller, o.price, o.shipping) for o in chosen], [('Shop B', 3200, 0)])
        self.assertTrue(eligible(chosen[0], search))
        self.assertFalse(eligible(Offer('idealo', product['url'], 'Spiel PS5', 3000, 900), search))
        self.assertFalse(eligible(Offer('idealo', product['url'], 'Spiel PS5', 3000, None), search))
        self.assertTrue(eligible(Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel PS5', 3000, 900), search))
        # Without any known shipping the "ab" price stays, marked as without shipping.
        bare = {'structured': [], 'rows': [dict(title='x', price='30,00 €', seller='Shop A', text='lieferbar')]}
        _, fallback = sources.idealo_result(data, bare, product, {'query': 'Spiel PS5'})
        self.assertEqual((fallback['price'], fallback['shipping_included']), (2500, False))

    def test_idealo_follows_the_first_valid_product_page(self):
        data = {
            'links': [
                {'title': 'Spiel PS5', 'url': 'https://evil.example/preisvergleich/OffersOfProduct/1_x.html'},
                {'title': 'Spiel PS5', 'url': 'https://www.idealo.de/preisvergleich/OffersOfProduct/2_x.html'},
            ]
        }
        self.assertEqual(
            sources.idealo_product(data, {'query': 'Spiel PS5'})['url'],
            'https://www.idealo.de/preisvergleich/OffersOfProduct/2_x.html',
        )
        self.assertIsNone(sources.idealo_product(data, {'query': 'Anderes Spiel'}))

    async def test_challenge_is_not_bypassed(self):
        page = self.page([dict(self.listing(), text='Verify you are human')])
        with self.assertRaisesRegex(RuntimeError, 'Sicherheitsprüfung'):
            await load_page(page, 'ebay', 'https://www.ebay.de/')
        page.wait_for_timeout.assert_not_awaited()

    async def test_empty_results_and_real_browser_errors_are_distinct(self):
        empty = dict(self.listing(), rows=[], text='Keine Ergebnisse gefunden')
        self.assertEqual(await load_page(self.page([empty]), 'ebay', 'https://www.ebay.de/'), empty)
        with cadence.db() as conn:  # a new visit: no cached page, budget available again
            conn.execute('DELETE FROM cache')
            conn.execute('DELETE FROM budget')
        with self.assertRaises(PlaywrightError):
            await load_page(self.page([PlaywrightError('Target closed')]), 'ebay', 'https://www.ebay.de/')

    async def test_loading_is_bounded(self):
        empty = dict(self.listing(), rows=[], text='')
        page = self.page([empty] * 16)
        with self.assertRaisesRegex(RuntimeError, 'nicht lesbar'):
            await load_page(page, 'ebay', 'https://www.ebay.de/')
        self.assertEqual(page.evaluate.await_count, 16)

    async def test_vinted_detail_waits_for_shipping_not_related_cards(self):
        waiting = dict(self.listing(), vintedDetail=True, vintedShipping='')
        ready = dict(waiting, vintedShipping='Versand ab 2,49 €')
        page = self.page([waiting, ready])
        result = await load_page(page, 'vinted', 'https://www.vinted.de/items/12345', follow=True)
        self.assertEqual(result['vintedShipping'], 'Versand ab 2,49 €')
        self.assertEqual(page.evaluate.await_count, 2)

    async def test_vinted_missing_shipping_is_unknown_not_source_failure(self):
        waiting = dict(self.listing(), vintedDetail=True, vintedShipping='')
        result = await load_page(self.page([waiting] * 16), 'vinted', 'https://www.vinted.de/items/12345', follow=True)
        self.assertEqual(result['vintedShipping'], '')


class AvailabilityTests(unittest.TestCase):
    def card(self, title, status=''):
        return dict(
            title=title,
            url='https://www.ebay.de/itm/12345',
            price='35 €',
            text=f'{title} 35 € {status}',
        )

    def test_product_words_are_not_availability_statuses(self):
        for title, status in (
            ('Soldier of Fortune PC', ''),
            ('Soldat Actionfigur', ''),
            ('Spiel PS5', 'Top Angebote'),
            ('Auktionskatalog', ''),
        ):
            with self.subTest(title=title, status=status):
                offers = parse_rows('ebay', [self.card(title, status)], dict(query=title))
                self.assertEqual(len(offers), 1)

    def test_unavailable_and_auction_statuses_still_exclude_offers(self):
        title = 'Spiel PS5'
        for status in (
            'Verkauft',
            'Reserviert',
            'Sold',
            '3 Gebote',
            '1 Gebot',
            'Auktion',
            'Ausverkauft',
            'Gesuch',
            'Beendet',
            'Nicht lieferbar',
            'Nicht verfügbar',
        ):
            with self.subTest(status=status):
                self.assertEqual(parse_rows('ebay', [self.card(title, status)], dict(query=title)), [])


class ShippingTests(unittest.TestCase):
    def test_kleinanzeigen_shipping_never_infers_a_cost_from_availability(self):
        for label, expected in (('Versand möglich', None), ('+ 4,50 € Versand', 450), ('Nur Abholung', None)):
            with self.subTest(label=label):
                self.assertEqual(row_shipping({'shipping': label}, 10000, 'kleinanzeigen'), expected)

    def test_marketplace_shipping_labels_and_real_cached_ebay_cards(self):
        for label, expected in [
            ('+EUR 8,18 Lieferung', 818),
            ('+EUR 6,19 Lieferung', 619),
            ('Gratis Lieferung', 0),
            ('Gratis 2–3 Tage Lieferung', 0),
            ('Kostenlose Lieferung', 0),
            ('Versand: EUR 4,99', 499),
            ('Lieferung: 0,00 €', 0),
            ('Free shipping', 0),
        ]:
            with self.subTest(label=label):
                self.assertEqual(shipping(label), expected)
                row = dict(
                    title='Spiel PS5',
                    price='EUR 40,66',
                    shipping='',
                    text='Spiel PS5Wird in neuem Fenster oder Tab geöffnetGebraucht | PrivatEUR 40,66oder Preisvorschlag'
                    + label
                    + 'mistlbacher 100% positiv (24)',
                )
                self.assertEqual(row_shipping(row, 4066, 'ebay'), expected)
        self.assertEqual(row_shipping({'shipping': '4,99 €'}, 4000, 'ebay'), 499)

    def test_shipping_does_not_read_returns_estimates_or_title_claims(self):
        for label in (
            'Kostenloser Rückversand',
            'Rückversand: 4,99 €',
            'Versand ab 4,99 €',
            'ab 4,99 € Versand',
            'Versand: EUR 4,99–6,99',
            'Versand 4,99 € bis 6,99 €',
            'Versand $4.99',
            'Nicht kostenloser Versand',
            'Kostenloser Versand ab 50 €',
            'Kostenloser Versand für Rücksendungen',
            '+4,99 € Versand +6,99 € Versand',
        ):
            with self.subTest(label=label):
                self.assertIsNone(shipping(label))
        row = dict(
            title='Spiel PS5 Gratis Lieferung',
            price='40,00 €',
            text='Spiel PS5 Gratis Lieferung40,00 €Kostenloser Rückversand',
        )
        self.assertIsNone(row_shipping(row, 4000, 'ebay'))
        self.assertIsNone(row_shipping({'shipping': 'ab 4,99 €', 'text': 'Gratis Lieferung'}, 4000, 'ebay'))

    def test_vinted_accepts_minimum_shipping_but_never_buyer_protection(self):
        self.assertEqual(row_shipping({'shipping': 'Versand ab 2,49 €'}, 4400, 'vinted'), 249)
        self.assertEqual(row_shipping({'shipping': 'ab 2,49 €'}, 4400, 'vinted'), 249)
        self.assertEqual(shipping('Versand\nab 2,49 €', allow_from=True), 249)
        self.assertIsNone(row_shipping({'shipping': '', 'text': '44,00 €46,90 €inkl.'}, 4400, 'vinted'))
        self.assertIsNone(row_shipping({'shipping': '', 'text': '21,70 € inkl. Käuferschutz'}, 2000, 'vinted'))
        offer = Offer('vinted', 'https://www.vinted.de/items/12345', 'Spiel', 4400, 249)
        self.assertTrue(eligible(offer, dict(min_price=0, max_price=4700, include_shipping=True)))
        self.assertFalse(eligible(offer, dict(min_price=0, max_price=4600, include_shipping=True)))
        for included in (True, False):
            entry = (dict(include_shipping=included), dict(source='vinted', shipping=249))
            self.assertEqual(mailer.shipping_text(entry), 'Versand ab 2,49 €')

    def test_total_from_same_card_gives_shipping_in_cents(self):
        self.assertEqual(row_shipping({'text': '49,99 €\n60,97\xa0€ inkl. Versand'}, 4999, 'idealo'), 1098)
        self.assertEqual(row_shipping({'text': '57,49 €\n57,49 € inkl. Versand'}, 5749, 'idealo'), 0)
        self.assertEqual(row_shipping({'text': '1.049,99 €\n1.060,97 € inkl. Versandkosten'}, 104999, 'idealo'), 1098)

    def test_ambiguous_or_missing_shipping_never_becomes_free(self):
        for text in (
            'Rücksendung 14 Tage kostenfrei',
            'Versand ab 4,99 €',
            '40,00 € inkl. Versand',
            '60,97 € inkl. Versand; 62,00 € inkl. Versand',
        ):
            self.assertIsNone(row_shipping({'text': text}, 4999, 'idealo'), text)
        self.assertIsNone(row_shipping({'text': '60,97 € inkl. Versand', 'shippingAmount': '4.99'}, 4999, 'idealo'))
        self.assertEqual(row_shipping({'shippingAmount': '4.99'}, 4999, 'idealo'), 499)

    def test_date_based_delivery_is_an_available_offer(self):
        row = dict(
            title='Spiel PS5',
            url='https://www.idealo.de/preisvergleich/OffersOfProduct/1_spiel.html',
            price='49,99 €',
            text='60,97 € inkl. Versand Lieferung: bis Do. 22.10.',
        )
        offers = parse_rows('idealo', [row], {'query': 'Spiel PS5'})
        self.assertEqual((offers[0].price, offers[0].shipping), (4999, 1098))
        self.assertEqual(
            parse_rows('idealo', [dict(row, text=row['text'] + ' nicht lieferbar')], {'query': 'Spiel PS5'}), []
        )

    def test_shop_selection_always_uses_the_total(self):
        url = 'https://www.idealo.de/preisvergleich/OffersOfProduct/1_spiel.html'
        offers = [
            Offer('idealo', url, 'Spiel PS5', 4999, 1098),
            Offer('idealo', url, 'Spiel PS5', 5749, 0),
            Offer('idealo', url, 'Spiel PS5', 4000, None),
        ]
        search = {'query': 'Spiel PS5', 'max_price': 6000}
        # idealo ignores the shipping switch: the cheapest total wins either way.
        self.assertEqual(select_offers(offers[:2], dict(search, include_shipping=False))[0].price, 5749)
        with_shipping = select_offers(offers, dict(search, include_shipping=True))[0]
        self.assertEqual((with_shipping.price, with_shipping.shipping), (5749, 0))
        self.assertFalse(eligible(offers[-1], dict(search, include_shipping=True)))


class SavedShippingTests(unittest.IsolatedAsyncioTestCase):
    setUp = SourceLoadingTests.setUp
    tearDown = SourceLoadingTests.tearDown
    listing = SourceLoadingTests.listing

    async def test_saved_page_repair_preserves_twelve_hour_schedule(self):
        now = time.time()
        search = dict(query='Spiel PS5', excluded='', include_shipping=False, interval_minutes=60)
        url = 'https://www.idealo.de/preisvergleich/OffersOfProduct/1_spiel.html'
        product = {'url': url, 'title': 'Spiel PS5'}
        detail = dict(
            self.listing(),
            rows=[
                dict(title='Spiel PS5', price='49,99 €', text='60,97 € inkl. Versand Lieferung: bis Do. 22.10.'),
                dict(title='Spiel PS5', price='57,49 €', text='57,49 € inkl. Versand Lieferung: bis Fr. 23.10.'),
                dict(title='', price='49,99 €', text='60,97 € inkl. Versand'),  # nested price-only block
            ],
        )
        cadence.save_page(
            'idealo', sources.search_url('idealo', search['query']), dict(self.listing(), links=[product])
        )
        cadence.save_page('idealo', url, detail)
        value = {'offers': [], 'reference': None}
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries VALUES (?,?,?,?,0,'ok',?)",
                (cadence.query_key('idealo', search), 'idealo', now, now + 43200, json.dumps(value)),
            )
        with patch('suchalarm.core.browser.InvisiblePlaywright') as browser:
            article, _ = await sources.fetch('idealo', search)
            total, _ = await sources.fetch('idealo', dict(search, include_shipping=True))
            browser.assert_not_called()
        # Both settings get the shop with the cheapest total.
        self.assertEqual((article[0].price, article[0].shipping), (5749, 0))
        self.assertEqual((total[0].price, total[0].shipping), (5749, 0))
        with cadence.db() as conn:
            row = conn.execute('SELECT * FROM queries').fetchone()
            self.assertEqual((row['at'], row['next_at']), (now, now + 43200))
            self.assertEqual(len(json.loads(row['value'])['offers']), 2)

    async def test_ebay_cache_repair_adds_shipping_without_a_new_request(self):
        now = time.time()
        search = dict(query='Spiel PS5', min_price=0, max_price=5000, include_shipping=True)
        row = dict(
            title='Spiel PS5',
            url='https://www.ebay.de/itm/12345',
            price='EUR 40,66',
            shipping='',
            text='Spiel PS5EUR 40,66oder Preisvorschlag+EUR 8,18 Lieferung',
        )
        cadence.save_page('ebay', sources.search_url('ebay', search['query'], search), dict(self.listing(), rows=[row]))
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries VALUES (?,?,?,?,0,'ok',?)",
                (
                    cadence.query_key('ebay', search),
                    'ebay',
                    now,
                    now + 1200,
                    json.dumps({'offers': [], 'reference': None, 'version': 2}),
                ),
            )
        with patch('suchalarm.core.browser.InvisiblePlaywright') as browser:
            offers, _ = await sources.fetch('ebay', search)
            browser.assert_not_called()
        self.assertEqual(offers[0].shipping, 818)
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT next_at FROM queries').fetchone()[0], now + 1200)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)

    def test_historical_pages_from_other_visits_are_not_mixed(self):
        cadence.save_page('idealo', 'https://www.idealo.de/example', self.listing())
        self.assertIsNone(cadence.historical_page('idealo', 'https://www.idealo.de/example', time.time() - 1000))


class MarkupChangeTests(unittest.IsolatedAsyncioTestCase):
    setUp = SourceLoadingTests.setUp
    tearDown = SourceLoadingTests.tearDown
    listing = SourceLoadingTests.listing

    def card(self, n, price='EUR 15,00'):
        return dict(title=f'Spiel {n}', url=f'https://www.ebay.de/itm/{1000 + n}', price=price)

    def test_cards_without_price_title_or_link_count_as_changed_markup(self):
        ad = dict(title='Shop on eBay', url='https://ebay.com/itm/123456', price='$20.00')
        self.assertFalse(sources.markup_changed('ebay', [ad, ad] + [self.card(n) for n in range(60)]))
        self.assertTrue(sources.markup_changed('ebay', [self.card(n, price='') for n in range(60)]))
        self.assertTrue(sources.markup_changed('ebay', [dict(self.card(n), url='') for n in range(60)]))
        self.assertFalse(sources.markup_changed('ebay', [self.card(n, price='') for n in range(4)]))

    async def test_changed_markup_is_a_retried_failure_not_zero_hits(self):
        search = dict(query='Spiel PS5', min_price=0, max_price=5000, include_shipping=False)
        rows = [self.card(n, price='') for n in range(20)]
        cadence.save_page('ebay', sources.search_url('ebay', search['query'], search), dict(self.listing(), rows=rows))
        page = AsyncMock()
        browser = AsyncMock()
        browser.__aenter__.return_value = page
        with patch('suchalarm.preis.sources.browser_page', return_value=browser), self.assertRaises(cadence.Deferred):
            await sources.fetch('ebay', search)
        self.assertIn('Seitenaufbau des Anbieters geändert', cadence.last_error('ebay', search))


class RouteGuardTests(unittest.IsolatedAsyncioTestCase):
    async def route(self, url, kind='document', navigation=False, address='93.184.216.34'):
        page = SimpleNamespace(main_frame=object())
        request = SimpleNamespace(
            url=url,
            resource_type=kind,
            frame=page.main_frame,
            is_navigation_request=lambda: navigation,
        )
        route = SimpleNamespace(request=request, abort=AsyncMock(), continue_=AsyncMock())
        with patch('socket.getaddrinfo', return_value=[(None, None, None, None, (address, 443))]):
            await browser.route_guard(HOSTS['ebay'], page)(route)
        return 'abort' if route.abort.await_count else 'continue'

    async def test_only_public_provider_pages_load(self):
        self.assertEqual(await self.route('https://www.ebay.de/sch', navigation=True), 'continue')
        self.assertEqual(await self.route('https://cdn.example/app.js', kind='script'), 'continue')
        self.assertEqual(await self.route('https://evil.example/', navigation=True), 'abort')
        self.assertEqual(await self.route('https://i.ebayimg.com/x.jpg', kind='image'), 'abort')
        self.assertEqual(await self.route('file:///etc/passwd'), 'abort')
        self.assertEqual(await self.route('https://router.example/', kind='script', address='192.168.1.1'), 'abort')


class DeliveryQueueTests(unittest.IsolatedAsyncioTestCase):
    setUp = SourceLoadingTests.setUp
    tearDown = SourceLoadingTests.tearDown

    async def test_one_detail_per_step_reuses_global_page_budget_and_cache(self):
        offers = [Offer('vinted', f'https://www.vinted.de/items/{i}', 'Spiel PS5', 3000 + i) for i in (1, 2)]
        delivery.remember(offers)

        async def visit(url):
            cadence.book('vinted', 'page')
            return 249

        fetch = AsyncMock(side_effect=visit)
        self.assertEqual(await delivery.step({o.url for o in offers}, fetch), (offers[0].url, 249))
        self.assertIsNone(await delivery.step({o.url for o in offers}, fetch))
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(delivery.cached(offers[0].url), 249)
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        self.assertIsNone(delivery.cached(offers[1].url))

    async def test_no_longer_active_offers_never_launch_browser(self):
        delivery.remember([Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', 3000)])
        fetch = AsyncMock()
        self.assertIsNone(await delivery.step(set(), fetch))
        self.assertIsNone(await delivery.step({'https://www.vinted.de/items/2'}, fetch))
        fetch.assert_not_called()

    async def test_missing_shipping_is_cached_and_refusals_pause_source(self):
        offer = Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', 3000)
        delivery.remember([offer])
        fetch = AsyncMock(return_value=None)
        self.assertEqual(await delivery.step({offer.url}, fetch), (offer.url, None))
        delivery.remember([offer])
        self.assertIsNone(await delivery.step({offer.url}, fetch))
        self.assertEqual(fetch.await_count, 1)
        with delivery.db() as conn:
            conn.execute('UPDATE deliveries SET next_at=0')
        self.assertIsNone(await delivery.step({offer.url}, AsyncMock(side_effect=browser.Blocked('HTTP 403'))))
        self.assertGreater(cadence.blocked_until('vinted'), time.time() + 1700)

    async def test_shipping_updates_query_cache_without_moving_search_schedule(self):
        now = time.time()
        offer = Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', 3000)
        delivery.remember([offer])
        value = {
            'offers': [dict(source='vinted', url=offer.url, title='Spiel', price=3000, shipping=None)],
            'reference': None,
            'version': 3,
        }
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries VALUES ('test','vinted',?,?,0,'ok',?)", (now, now + 1800, json.dumps(value))
            )
        await delivery.step({offer.url}, AsyncMock(return_value=249))
        with cadence.db() as conn:
            row = conn.execute("SELECT * FROM queries WHERE key='test'").fetchone()
            self.assertEqual((row['at'], row['next_at']), (now, now + 1800))
            self.assertEqual(json.loads(row['value'])['offers'][0]['shipping'], 249)
        self.assertEqual(delivery.cached(offer.url), 249)
        with patch('suchalarm.preis.delivery.time.time', return_value=now + delivery.TTL + 10):
            self.assertIsNone(delivery.cached(offer.url))

    async def test_detail_arrival_keeps_initial_baseline_and_enqueues_new_matches(self):
        store.init()
        with store.db() as conn:
            conn.execute("INSERT INTO searches(id,query,max_price,sources) VALUES (1,'Spiel',4000,'[\"vinted\"]')")
            for i in (1, 2):
                conn.execute(
                    "INSERT INTO offers VALUES (1,'vinted',?,'Spiel',3000,NULL,'','',1,1)",
                    (f'https://www.vinted.de/items/{i}',),
                )
            conn.execute("INSERT INTO shipping_baselines VALUES (1,'https://www.vinted.de/items/1')")
            conn.execute("INSERT INTO settings VALUES ('mail_enabled','1')")
            conn.execute("INSERT INTO settings VALUES ('recipients','[\"test@example.com\"]')")
        for i in (1, 2):
            with patch('suchalarm.preis.delivery.step', return_value=(f'https://www.vinted.de/items/{i}', 249)):
                await products.update_shipping()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
            self.assertEqual(
                conn.execute('SELECT url,price FROM outbox').fetchone()[:],
                ('https://www.vinted.de/items/2', 3469),  # 30 € + 2,20 € protection + 2,49 €
            )
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM shipping_baselines').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
