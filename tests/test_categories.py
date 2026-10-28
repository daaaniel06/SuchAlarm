import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from suchalarm import store, worker
from suchalarm.app import app
from suchalarm.core import cadence
from suchalarm.preis import sources
from suchalarm.preis.domain import Offer, in_range, select_offers, terms, too_cheap, vehicle
from suchalarm.preis.sources import parse_rows, place_of, search_url


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        self.sofa = dict(
            query='Ecksofa',
            min_price=0,
            max_price=30000,
            include_shipping=False,
            excluded='',
            category='moebel',
            delivery='abholung',
            plz='10115',
            radius=25,
            negotiable=True,
            filters={},
        )
        self.car = dict(
            self.sofa, query='VW Golf', category='autos', radius=100, filters={'year_from': 2012, 'km_to': 150000}
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_kleinanzeigen_searches_section_and_postcode_area(self):
        with self.assertRaises(LookupError):
            search_url('kleinanzeigen', 'Ecksofa', self.sofa)  # never silently nationwide
        cadence.save_location('kleinanzeigen', '10115', '9668')
        self.assertEqual(
            search_url('kleinanzeigen', 'Ecksofa', self.sofa),
            'https://www.kleinanzeigen.de/s-preis::300/Ecksofa/k0c80l9668r25?sortingField=SORTING_PRICE',
        )
        self.assertEqual(
            search_url('kleinanzeigen', 'VW Golf', self.car),
            'https://www.kleinanzeigen.de/s-preis::300/VW-Golf/k0c216l9668r100+autos.ez_i:2012%2C+autos.km_i:%2C150000?sortingField=SORTING_PRICE',
        )

    def test_ebay_searches_around_postcode_instead_of_germany(self):
        params = parse_qs(urlsplit(search_url('ebay', 'Ecksofa', self.sofa)).query)
        self.assertEqual(
            (params['_sacat'], params['_stpos'], params['_sadis'], params['LH_PrefLoc']),
            (['3197'], ['10115'], ['25'], ['99']),
        )
        self.assertNotIn('LH_LocatedIn', params)
        game = dict(
            query='Resident Evil',
            max_price=3500,
            include_shipping=True,
            category='videospiele',
            filters={'platform': 'ps5'},
        )
        params = parse_qs(urlsplit(search_url('ebay', terms(game), game)).query)
        self.assertEqual(
            (params['_sacat'], params['_nkw'], params['LH_LocatedIn']), (['139973'], ['Resident Evil PS5'], ['1'])
        )
        vinted = parse_qs(urlsplit(search_url('vinted', terms(game), game)).query)
        self.assertEqual(vinted['catalog[]'], ['3026'])

    def test_old_searches_keep_their_cache_key(self):
        old = dict(query='Spiel PS5', max_price=3500, include_shipping=True, excluded='')
        new = dict(old, category='sonstiges', delivery='versand', plz='', radius=0, negotiable=False, filters={})
        for source in ('ebay', 'kleinanzeigen', 'vinted', 'idealo'):
            self.assertEqual(cadence.query_key(source, old), cadence.query_key(source, new))
        self.assertNotEqual(cadence.query_key('ebay', self.sofa), cadence.query_key('ebay', dict(self.sofa, radius=50)))
        self.assertNotEqual(
            cadence.query_key('kleinanzeigen', self.car),
            cadence.query_key('kleinanzeigen', dict(self.car, filters={'km_to': 90000})),
        )
        # VB offers are dropped while parsing, so a shared result must not mix both settings.
        for source in ('ebay', 'kleinanzeigen', 'vinted', 'idealo'):
            self.assertNotEqual(cadence.query_key(source, old), cadence.query_key(source, dict(old, negotiable=True)))

    def test_platform_is_required_in_titles(self):
        game = dict(query='Resident Evil Requiem', excluded='', filters={'platform': 'ps5'})
        offers = [
            Offer('ebay', 'https://www.ebay.de/itm/1', 'Resident Evil Requiem PlayStation 5', 3000),
            Offer('ebay', 'https://www.ebay.de/itm/2', 'Resident Evil Requiem Xbox Series X', 3000),
        ]
        self.assertEqual([o.url for o in select_offers(offers, game)], ['https://www.ebay.de/itm/1'])

    def test_negotiable_offers_only_when_allowed(self):
        row = dict(
            title='Ecksofa grau',
            url='https://www.kleinanzeigen.de/s-anzeige/ecksofa/123-88-3331',
            price='150 € VB',
            text='10115 Mitte(2 km) Ecksofa grau 150 € VB',
        )
        self.assertEqual(parse_rows('kleinanzeigen', [row], dict(self.sofa, negotiable=False)), [])
        offer = parse_rows('kleinanzeigen', [row], self.sofa)[0]
        self.assertEqual((offer.price, offer.seller), (15000, 'Mitte · 2 km'))

    def test_cars_outside_stated_limits_drop_out(self):
        self.assertEqual(
            vehicle('1312347 Neukölln(9 km) Motor bei 50.000 km neu 6.990 €104.000 kmEZ 06/2012'), (104000, 2012)
        )
        base = dict(title='VW Golf 7', url='https://www.kleinanzeigen.de/s-anzeige/golf/1-216-3331', price='6.990 €')
        rows = [
            dict(base, text='6.990 €104.000 kmEZ 06/2012'),
            dict(base, url=base['url'].replace('/1-', '/2-'), text='6.990 €190.000 kmEZ 06/2014'),
            dict(base, url=base['url'].replace('/1-', '/3-'), text='6.990 € EZ 2009'),
            dict(base, url=base['url'].replace('/1-', '/4-'), text='6.990 €'),
        ]
        self.assertEqual(
            [o.url[-10:] for o in parse_rows('kleinanzeigen', rows, self.car)],
            [rows[0]['url'][-10:], rows[3]['url'][-10:]],
        )

    def test_ebay_cards_show_distance(self):
        self.assertEqual(
            place_of('ebay', dict(seller='Gebraucht | Privat', text='EUR 100,00 Kostenlose Abholung 15 km von 10115')),
            ('Gebraucht | Privat', '15 km entfernt'),
        )

    def test_given_away_only_when_wanted(self):
        row = dict(
            title='Ecksofa',
            url='https://www.kleinanzeigen.de/s-anzeige/ecksofa/7-88-3331',
            price='Zu verschenken',
            text='10115 Mitte(2 km) Ecksofa Zu verschenken',
        )
        search = dict(self.sofa, min_price=5000)
        self.assertEqual(parse_rows('kleinanzeigen', [row], search), [])
        offer = parse_rows('kleinanzeigen', [row], dict(search, free=True))[0]
        self.assertEqual(offer.price, 0)
        self.assertTrue(in_range(0, dict(search, free=True)))  # the minimum price does not apply
        self.assertFalse(in_range(0, search))
        self.assertFalse(too_cheap(offer, dict(search, free=True)))
        cadence.save_location('kleinanzeigen', '10115', '9668')
        # A lower bound at the provider would hide given-away offers.
        self.assertIn('/s-preis::300/', search_url('kleinanzeigen', 'Ecksofa', dict(search, free=True)))
        self.assertIn('/s-preis:50:300/', search_url('kleinanzeigen', 'Ecksofa', search))

    def test_both_reads_the_area_and_shippable_offers_from_everywhere(self):
        cadence.save_location('kleinanzeigen', '10115', '9668')
        search = dict(self.sofa, delivery='beides')

        def card(n, text):
            url = f'https://www.kleinanzeigen.de/s-anzeige/ecksofa/{n}-88-3331'
            return dict(title='Ecksofa', url=url, price='100 €', text=text)

        pages = {
            search_url('kleinanzeigen', 'Ecksofa', search): [card(1, '10115 Mitte(2 km) Ecksofa 100 €')],
            search_url('kleinanzeigen', 'Ecksofa', dict(search, delivery='versand')): [
                card(2, '80331 München Ecksofa 100 €Versand möglich'),
                card(3, '20095 Hamburg Ecksofa 100 €'),
            ],
        }
        load = AsyncMock(side_effect=lambda page, source, url, follow=False: dict(rows=pages[url], text=''))

        @asynccontextmanager
        async def browser(source):
            yield None

        with (
            patch.object(sources, 'load_page', load),
            patch.object(sources, 'browser_page', browser),
            patch.object(sources.images, 'remember'),
        ):
            offers, _ = asyncio.run(sources.fetch.__wrapped__('kleinanzeigen', search))
        self.assertEqual(
            sorted(o.url.split('/')[-1][0] for o in offers), ['1', '2']
        )  # Hamburg is pickup only and far away
        self.assertTrue(load.call_args_list[1].kwargs['follow'])  # the second page waits for its own slot
        self.assertNotEqual(cadence.query_key('kleinanzeigen', search), cadence.query_key('kleinanzeigen', self.sofa))


class CategoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()
        self.client = TestClient(app)
        worker.progress = worker.Progress()
        self.payload = dict(
            query='Ecksofa',
            max_price=30000,
            sources=['ebay', 'kleinanzeigen'],
            category='moebel',
            delivery='abholung',
            plz='10115',
            radius=25,
            negotiable=True,
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_pickup_rules(self):
        self.assertEqual(
            self.client.post('/api/searches', json=dict(self.payload, sources=['vinted'])).json()['detail'],
            'Vinted und idealo können nicht im Umkreis suchen. Bei Abholung nur eBay und Kleinanzeigen wählen.',
        )
        self.assertEqual(self.client.post('/api/searches', json=dict(self.payload, plz='101')).status_code, 422)
        self.assertEqual(self.client.post('/api/searches', json=dict(self.payload, radius=33)).status_code, 422)
        self.assertEqual(
            self.client.post('/api/searches', json=dict(self.payload, include_shipping=True)).status_code, 201
        )
        search = self.client.get('/api/state').json()['searches'][0]
        self.assertEqual(
            (search['include_shipping'], search['plz'], search['radius'], search['negotiable']), (0, '10115', 25, 1)
        )

    def test_both_keeps_nationwide_sources(self):
        both = dict(
            self.payload,
            delivery='beides',
            sources=['ebay', 'kleinanzeigen', 'vinted'],
            include_shipping=True,
            free=True,
        )
        self.assertEqual(self.client.post('/api/searches', json=both).status_code, 201)
        self.assertEqual(self.client.post('/api/searches', json=dict(both, plz='')).status_code, 422)
        search = self.client.get('/api/state').json()['searches'][0]
        self.assertEqual(
            (search['delivery'], search['sources'], search['include_shipping'], search['free']),
            ('beides', ['ebay', 'kleinanzeigen', 'vinted'], 0, 1),
        )

    def test_filters_belong_to_their_category(self):
        car = dict(
            self.payload,
            query='VW Golf',
            category='autos',
            radius=100,
            filters={'year_from': 2012, 'km_to': 150000, 'platform': 'ps5'},
        )
        self.client.post('/api/searches', json=car)
        game = dict(
            query='Spiel',
            max_price=3500,
            sources=['ebay'],
            category='videospiele',
            plz='10115',
            radius=25,
            filters={'platform': 'ps5', 'km_to': 5000},
        )
        self.client.post('/api/searches', json=game)
        state = self.client.get('/api/state').json()
        self.assertEqual(
            [s['filters'] for s in state['searches']], [{'platform': 'ps5'}, {'year_from': 2012, 'km_to': 150000}]
        )
        self.assertEqual(
            (state['searches'][0]['plz'], state['searches'][0]['radius']), ('', 0)
        )  # shipping searches have no area
        self.assertNotIn('portal', state['choices']['categories']['autos'])

    def test_platform_change_resets_reported_offers(self):
        game = dict(
            query='Spiel', max_price=3500, sources=['ebay'], category='videospiele', filters={'platform': 'ps5'}
        )
        sid = self.client.post('/api/searches', json=game).json()['id']
        with store.db() as conn:
            conn.execute('INSERT INTO notifications VALUES (?,?,?,?)', (sid, 'https://www.ebay.de/itm/1', 2000, 1))
        self.client.put(f'/api/searches/{sid}', json=dict(game, active=False))
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)
        self.client.put(f'/api/searches/{sid}', json=dict(game, filters={'platform': 'ps4'}))
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
