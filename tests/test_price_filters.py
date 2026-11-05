import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from suchalarm import scheduler, store
from suchalarm.core import cadence
from suchalarm.preis.prices import Offer, buyer_fee, eligible, portal_price_bounds
from suchalarm.preis.sources import ebay_in_germany, parse_rows, search_url
from suchalarm.preis.worker import judged


class PriceFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        self.search = dict(
            query='Resident Evil Requiem PS5', min_price=2000, max_price=4500, include_shipping=False, excluded=''
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_vinted_and_ebay_receive_exact_item_price_bounds(self):
        vinted = parse_qs(urlsplit(search_url('vinted', self.search['query'], self.search)).query)
        self.assertEqual(
            (vinted['price_from'], vinted['price_to'], vinted['currency']), (['20.00'], ['44.99'], ['EUR'])
        )
        self.assertEqual(vinted['search_text'], [self.search['query']])
        self.assertNotIn('order', vinted)  # keep Vinted's native relevance instead of accessory-first price order
        ebay = parse_qs(urlsplit(search_url('ebay', self.search['query'], self.search)).query)
        self.assertEqual((ebay['_udlo'], ebay['_udhi']), (['20.00'], ['44.99']))
        self.assertEqual(ebay['LH_BIN'], ['1'])
        self.assertEqual(ebay['_sop'], ['12'])  # Best Match, not price order
        self.assertEqual(ebay['_ipg'], ['240'])  # eBay's largest page
        self.assertEqual(ebay['LH_LocatedIn'], ['1'])
        self.assertEqual(ebay['_salic'], ['77'])

    def test_shipping_never_hides_a_cheaper_item_with_valid_total(self):
        search = dict(self.search, include_shipping=True)
        self.assertTrue(eligible(Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel', 1800, 400), search))
        for source, minimum in (('ebay', '_udlo'), ('vinted', 'price_from')):
            query = parse_qs(urlsplit(search_url(source, search['query'], search)).query)
            self.assertNotIn(minimum, query)
        self.assertEqual(portal_price_bounds('vinted', search), (0, 4499))
        self.assertIn('/s-preis::45/', search_url('kleinanzeigen', search['query'], search))

    def test_vinted_buyer_protection_counts_when_the_search_includes_it(self):
        self.assertEqual(buyer_fee('vinted', 4400), 290)  # Vinted's card: 44,00 € -> 46,90 € inkl.
        self.assertEqual(buyer_fee('ebay', 4400), 0)
        offer = Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', 4300, 249)
        with_fee = dict(self.search, include_fee=True)
        self.assertEqual(offer.compared_price(with_fee), 4585)
        self.assertFalse(eligible(offer, with_fee))  # 43 € + 2,85 € protection is over 45 €
        self.assertTrue(eligible(offer, self.search))
        self.assertEqual(offer.compared_price(dict(with_fee, include_shipping=True)), 4834)
        ebay = Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel', 4300, 249)
        self.assertEqual(ebay.compared_price(with_fee), 4300)
        # Vinted filters the item price: the window covers exactly the items whose price with protection fits.
        self.assertEqual(portal_price_bounds('vinted', with_fee), (1838, 4218))  # 20,00 € and 44,99 € inkl.
        self.assertEqual(portal_price_bounds('vinted', dict(with_fee, include_shipping=True)), (0, 4218))
        self.assertEqual(portal_price_bounds('ebay', with_fee), (2000, 4499))

    def test_vinted_price_filter_keeps_every_item_that_fits_with_buyer_protection(self):
        search = dict(self.search, include_fee=True)
        minimum, maximum = portal_price_bounds('vinted', search)
        for price in range(1, 5000):
            fits = eligible(Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', price, 0), search)
            self.assertEqual(minimum <= price <= maximum, fits, price)
        query = parse_qs(urlsplit(search_url('vinted', search['query'], search)).query)
        self.assertEqual((query['price_from'], query['price_to']), (['18.38'], ['42.18']))

    def test_kleinanzeigen_rounds_outwards_not_into_the_price_range(self):
        search = dict(self.search, min_price=2050, max_price=4450)
        self.assertEqual(portal_price_bounds('kleinanzeigen', search), (2000, 4500))
        self.assertIn('/s-preis:20:45/', search_url('kleinanzeigen', search['query'], search))
        self.assertFalse(
            eligible(Offer('kleinanzeigen', 'https://www.kleinanzeigen.de/s-anzeige/x/1-1-1', 'Spiel', 2000, 0), search)
        )

    def test_idealo_reference_is_independent_of_local_price_range(self):
        search = dict(self.search, min_price=3000, max_price=3500, include_shipping=True)
        self.assertEqual(
            search_url('idealo', search['query'], search), search_url('idealo', search['query'], self.search)
        )
        self.assertEqual(cadence.query_key('idealo', search), cadence.query_key('idealo', self.search))

    def test_cache_keys_follow_actual_provider_filters(self):
        other = dict(self.search, min_price=3000)
        for source in ('vinted', 'ebay', 'kleinanzeigen'):
            self.assertNotEqual(cadence.query_key(source, self.search), cadence.query_key(source, other))
        # Local shipping rules/exclusions can share one page if the upstream bounds match.
        first = dict(self.search, include_shipping=True)
        second = dict(other, include_shipping=True, excluded='defekt')
        self.assertEqual(cadence.query_key('vinted', first), cadence.query_key('vinted', second))

    async def test_different_price_windows_cannot_reuse_narrow_result_cache(self):
        actual = AsyncMock(
            return_value=(
                [Offer('vinted', 'https://www.vinted.de/items/1', 'Resident Evil Requiem PS5', 2500, 0)],
                None,
            )
        )
        fetch = cadence.shared_search(actual)
        await fetch('vinted', self.search)
        await fetch('vinted', dict(self.search, min_price=3000))
        await fetch('vinted', dict(self.search, excluded='Hülle'))
        self.assertEqual(actual.await_count, 2)

    def test_scheduler_groups_only_matching_provider_price_windows(self):
        store.init()
        searches = [
            dict(self.search, id=1, sources=['vinted'], active=1),
            dict(self.search, id=2, sources=['vinted'], active=1, min_price=3000),
        ]
        scheduler.plan(searches, False)
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)

    def test_zero_minimum_and_cent_precision(self):
        search = dict(self.search, min_price=0, max_price=1001)
        params = parse_qs(urlsplit(search_url('vinted', search['query'], search)).query)
        self.assertNotIn('price_from', params)
        self.assertEqual(params['price_to'], ['10.00'])

    def test_foreign_ebay_locations_are_not_valid_offers(self):
        row = dict(
            title='Resident Evil Requiem PS5',
            url='https://www.ebay.de/itm/123',
            price='44,57 €',
            text='+EUR 20,56 Lieferungaus Vereinigte Staaten von Amerika',
        )
        self.assertEqual(parse_rows('ebay', [row], self.search), [])
        self.assertFalse(ebay_in_germany(dict(row, text='+EUR 10,52 Lieferungaus Kanada')))
        self.assertFalse(ebay_in_germany(dict(row, text='+EUR 4,00 Lieferungaus Litauen')))
        self.assertTrue(ebay_in_germany(dict(row, text='Gratis Lieferungaus Deutschlandmeinverkaufer')))
        self.assertFalse(ebay_in_germany(dict(row, text='', location='aus USA')))
        self.assertFalse(ebay_in_germany(dict(row, text='', location='aus Österreich')))
        self.assertFalse(ebay_in_germany(dict(row, text='', location='Canada')))
        domestic = dict(row, text='Kostenloser Versand', location='aus Deutschland')
        self.assertEqual(len(parse_rows('ebay', [domestic], self.search)), 1)

    def test_usa_in_seller_title_is_not_an_item_location(self):
        row = dict(
            title='Resident Evil Requiem PS5 Import aus USA',
            text='Resident Evil Requiem PS5 Import aus USA. Kostenloser Versand',
            location='Berlin, Deutschland',
        )
        self.assertTrue(ebay_in_germany(row))

    async def test_unrestricted_ebay_cache_is_not_reused_for_domestic_search(self):
        old_input = 'resident evil requiem ps5\nprice:2000:4499'
        old_key = 'ebay:' + hashlib.sha256(old_input.encode()).hexdigest()
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries VALUES (?,?,?,?,0,'ok',?)",
                (old_key, 'ebay', time.time(), time.time() + 1200, json.dumps({'offers': [], 'reference': None})),
            )
        actual = AsyncMock(
            return_value=([Offer('ebay', 'https://www.ebay.de/itm/1', 'Resident Evil Requiem PS5', 4000, 0)], None)
        )
        offers, _ = await cadence.shared_search(actual)('ebay', self.search)
        actual.assert_awaited_once()
        self.assertEqual(len(offers), 1)
        self.assertNotEqual(cadence.query_key('ebay', self.search), old_key)


class JudgedTests(unittest.TestCase):
    """What the page shows and filters by comes from the server only (preis/worker.judged); the page has no
    price rules of its own."""

    def judged(self, search, price, shipping, source='ebay'):
        return judged({'source': source, 'price': price, 'shipping': shipping}, search)

    def test_unknown_shipping_counts_only_with_the_shipping_switch(self):
        self.assertEqual(self.judged({'include_shipping': 0, 'max_price': 9000}, 4066, None)['compared'], 4066)
        self.assertIsNone(self.judged({'include_shipping': 1, 'max_price': 9000}, 4066, None)['compared'])
        self.assertEqual(self.judged({'include_shipping': 1, 'max_price': 9000}, 4066, 818)['compared'], 4884)

    def test_vinted_counts_its_buyer_protection_when_the_search_includes_it(self):
        def compared(**search):
            return self.judged({'max_price': 9000, **search}, 4400, 249, 'vinted')['compared']

        self.assertEqual(compared(include_shipping=0, include_fee=1), 4690)
        self.assertEqual(compared(include_shipping=1, include_fee=1), 4939)
        self.assertEqual(compared(include_shipping=0, include_fee=0), 4400)
        self.assertEqual(self.judged({'include_fee': 1, 'max_price': 9000}, 4400, 249)['compared'], 4400)

    def test_idealo_shops_always_count_with_shipping(self):
        search = {'include_shipping': 0, 'min_price': 0, 'max_price': 3500}
        shop = self.judged(search, 3000, 900, 'idealo')
        self.assertEqual((shop['compared'], shop['fits'], shop['with_shipping']), (3900, False, True))
        self.assertIsNone(self.judged(search, 3000, None, 'idealo')['compared'])
        used = self.judged(search, 3000, 900)
        self.assertEqual((used['compared'], used['fits'], used['with_shipping']), (3000, True, False))

    def test_fits_follows_the_price_range(self):
        search = {'include_shipping': 1, 'min_price': 2000, 'max_price': 4500}
        self.assertFalse(self.judged(search, 4066, None)['fits'])  # unknown shipping is never 0
        self.assertFalse(self.judged(search, 4066, 818)['fits'])  # 48,84 € is over the limit
        self.assertTrue(self.judged({**search, 'max_price': 5000}, 4066, 818)['fits'])
        self.assertFalse(self.judged(search, 4500, 0)['fits'])  # strictly below the maximum
        self.assertFalse(self.judged(search, 300, 0)['fits'])  # junk below the minimum

    def test_given_away_offers_pass_the_minimum_only_when_the_search_wants_them(self):
        search = {'include_shipping': 0, 'min_price': 2000, 'max_price': 4500}
        self.assertFalse(self.judged(search, 0, None)['fits'])
        self.assertTrue(self.judged({**search, 'free': True}, 0, None)['fits'])
        shipped = {**search, 'include_shipping': 1, 'free': True}
        self.assertTrue(self.judged(shipped, 0, 450)['fits'])  # only the parcel costs
        self.assertFalse(self.judged({**shipped, 'max_price': 400}, 0, 450)['fits'])
        self.assertFalse(self.judged(shipped, 0, None)['fits'])  # unknown shipping is never 0

    def test_a_snapshot_whose_search_is_gone_shows_its_full_price(self):
        snapshot = self.judged(None, 4400, 249, 'vinted')
        self.assertEqual((snapshot['compared'], snapshot['fits']), (4939, True))
        self.assertFalse(self.judged(None, 4400, None, 'vinted')['fits'])
