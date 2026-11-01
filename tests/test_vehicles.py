"""Car and bike searches: titles written either way, Kleinanzeigen's own filters, eBay's E-Bike section."""

import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from suchalarm import store
from suchalarm.app import app
from suchalarm.core import cadence
from suchalarm.preis.domain import query_text, title_fits
from suchalarm.preis.sources import parse_rows, search_url


class VehicleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        cadence.save_location('kleinanzeigen', '10115', '9668')
        self.car = dict(
            query='VW Golf 7',
            min_price=0,
            max_price=1500000,
            include_shipping=False,
            excluded='Unfall, Motorschaden',
            category='autos',
            delivery='abholung',
            plz='10115',
            radius=100,
            negotiable=True,
            filters={},
        )
        self.bike = dict(self.car, query='Canyon', excluded='defekt', category='fahrraeder', radius=50)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_car_titles_written_either_way(self):
        # Titles as Kleinanzeigen showed them for "golf" on 01.11.2026.
        for title in (
            'Volkswagen Golf VII Variant Comfortline BMT',
            'Volkswagen Golf 7 Comfortline 1.2 TSI 105 PS Benziner Unfallfrei',
            'Vw golf 7 1,4l 16v',
            'Golf 7 1.4 TSI Highline',  # the make left out inside the car section
            'VW Golf7 GTI',
        ):
            self.assertTrue(title_fits('kleinanzeigen', title, self.car), title)
        for title in ('Volkswagen Golf VI Match', 'VW Polo 7', 'Golf 6 TDI'):
            self.assertFalse(title_fits('kleinanzeigen', title, self.car), title)

    def test_make_alone_stays_required(self):
        bmw = dict(self.car, query='BMW', excluded='')
        self.assertTrue(title_fits('kleinanzeigen', 'BMW 320d Touring', bmw))
        self.assertFalse(title_fits('kleinanzeigen', 'Audi A4 Avant', bmw))
        # Outside the car section the make is a word like any other.
        self.assertFalse(title_fits('ebay', 'Golf 7 Schlägerset', dict(self.car, category='sonstiges')))

    def test_mercedes_benz_is_mercedes(self):
        search = dict(self.car, query='Mercedes-Benz C 200', excluded='')
        self.assertTrue(title_fits('kleinanzeigen', 'Mercedes C 200 T-Modell', search))
        self.assertTrue(title_fits('kleinanzeigen', 'Mercedes Benz C200', search))

    def test_negated_excluded_words_keep_the_offer(self):
        self.assertTrue(title_fits('kleinanzeigen', 'VW Golf 7 TSI kein Unfall, Scheckheft', self.car))
        self.assertTrue(title_fits('kleinanzeigen', 'VW Golf 7 ohne Motorschaden', self.car))
        self.assertFalse(title_fits('kleinanzeigen', 'VW Golf 7 Unfall vorne links', self.car))
        self.assertFalse(title_fits('kleinanzeigen', 'VW Golf 7 kein TÜV, Motorschaden', self.car))

    def test_kleinanzeigen_filters_fuel_and_gearbox(self):
        car = dict(self.car, filters={'year_from': 2012, 'km_to': 150000, 'fuel': 'diesel', 'gearbox': 'automatik'})
        self.assertEqual(
            search_url('kleinanzeigen', 'VW Golf 7', car),
            'https://www.kleinanzeigen.de/s-preis::15000/VW-Golf-7/k0c216l9668r100'
            '+autos.ez_i:2012%2C+autos.km_i:%2C150000+autos.fuel_s:diesel+autos.shift_s:automatik'
            '?sortingField=SORTING_DATE',
        )
        only_fuel = dict(self.car, filters={'fuel': 'elektro'})
        self.assertIn('/k0c216l9668r100+autos.fuel_s:elektro?', search_url('kleinanzeigen', 'Tesla', only_fuel))
        # eBay's car cards know nothing of it; the filters only reach Kleinanzeigen.
        self.assertNotIn('fuel', search_url('ebay', 'VW Golf 7', car))

    def test_new_filters_change_the_shared_result_but_not_the_old_ones(self):
        key = cadence.query_key('kleinanzeigen', self.car)
        self.assertEqual(key, cadence.query_key('kleinanzeigen', dict(self.car, filters={'fuel': None})))
        for filters in ({'fuel': 'diesel'}, {'gearbox': 'manuell'}):
            self.assertNotEqual(key, cadence.query_key('kleinanzeigen', dict(self.car, filters=filters)))
        bike = cadence.query_key('kleinanzeigen', self.bike)
        self.assertNotEqual(bike, cadence.query_key('kleinanzeigen', dict(self.bike, filters={'bike_type': 'rennrad'})))

    def test_a_car_search_never_shares_its_result_with_one_that_needs_the_make(self):
        # Vinted has no car section; a car search there may still miss the make in the title.
        car = dict(self.car, delivery='versand', filters={})
        self.assertNotEqual(query_text('vinted', car), query_text('vinted', dict(car, category='sonstiges')))
        self.assertEqual(
            query_text('vinted', dict(car, category='moebel')), query_text('vinted', dict(car, category='sonstiges'))
        )

    def test_kleinanzeigen_filters_the_kind_of_bike(self):
        rennrad = dict(self.bike, filters={'bike_type': 'rennrad'})
        self.assertIn('/k0c217l9668r50+fahrraeder.type_s:rennrad?', search_url('kleinanzeigen', 'Canyon', rennrad))
        kids = dict(self.bike, filters={'bike_type': 'kinder'})
        self.assertIn('+fahrraeder.art_s:kinder?', search_url('kleinanzeigen', 'Puky', kids))
        self.assertIn('/k0c217l9668r50?', search_url('kleinanzeigen', 'Canyon', self.bike))

    def test_ebay_looks_for_ebikes_in_their_own_section(self):
        def section(search, query):
            return parse_qs(urlsplit(search_url('ebay', query, search)).query)['_sacat'][0]

        self.assertEqual(section(self.bike, 'Canyon'), '177831')
        self.assertEqual(section(dict(self.bike, filters={'bike_type': 'ebike'}), 'Cube'), '74469')
        for query in ('Cube E-Bike', 'Cube EBike', 'Cube Pedelec', 'Cube Elektrofahrrad'):
            self.assertEqual(section(dict(self.bike, query=query), query), '74469', query)
        self.assertEqual(section(dict(self.bike, filters={'bike_type': 'rennrad'}), 'Canyon'), '177831')
        self.assertNotEqual(
            query_text('ebay', dict(self.bike, query='Cube Pedelec')), query_text('ebay', dict(self.bike, query='Cube'))
        )

    def test_bike_titles_written_either_way(self):
        ebike = dict(self.bike, query='Cube E-Bike')
        for title in ('Cube Reaction Hybrid Pedelec', 'Cube EBike 625Wh', 'Cube E Bike Größe L', 'CUBE e-bike'):
            self.assertTrue(title_fits('kleinanzeigen', title, ebike), title)
        mtb = dict(self.bike, query='Canyon Mountainbike')
        for title in ('Canyon Spectral MTB Fully', 'Canyon Mountain Bike Grand Canyon'):
            self.assertTrue(title_fits('kleinanzeigen', title, mtb), title)

    def test_car_cards_parse_like_before(self):
        car = dict(self.car, filters={'year_from': 2012, 'km_to': 200000})
        row = dict(
            title='Volkswagen Golf VII Variant Comfortline BMT',
            url='https://www.kleinanzeigen.de/s-anzeige/golf/3249-216-3331',
            price='6.599 €',
            text='99869 Emleben Heute, 09:46 Volkswagen Golf VII Variant Comfortline BMT VW Golf 7 Variant '
            '6.599 €151.686 kmEZ 09/2013',
        )
        old = dict(row, url=row['url'].replace('3249', '3250'), text='3.899 €181.773 kmEZ 04/2011')
        self.assertEqual([o.price for o in parse_rows('kleinanzeigen', [row, old], car)], [659900])


class VehicleApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()
        self.client = TestClient(app)
        self.base = dict(query='Golf', max_price=1500000, sources=['kleinanzeigen'], plz='10115', radius=100)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_filters_are_validated_and_belong_to_their_category(self):
        filters = {'fuel': 'diesel', 'gearbox': 'manuell', 'bike_type': 'ebike', 'year_from': 2015}
        for category in ('autos', 'fahrraeder', 'moebel'):
            payload = dict(self.base, category=category, delivery='abholung', filters=filters)
            self.assertEqual(self.client.post('/api/searches', json=payload).status_code, 201)
        stored = {s['category']: s['filters'] for s in self.client.get('/api/state').json()['searches']}
        self.assertEqual(
            stored,
            {
                'autos': {'fuel': 'diesel', 'gearbox': 'manuell', 'year_from': 2015},
                'fahrraeder': {'bike_type': 'ebike'},
                'moebel': {},
            },
        )
        wrong = dict(self.base, category='autos', delivery='abholung', filters={'fuel': 'kerosin'})
        self.assertEqual(self.client.post('/api/searches', json=wrong).status_code, 422)

    def test_editor_gets_the_choices(self):
        choices = self.client.get('/api/state').json()['choices']
        self.assertEqual(choices['fuels']['diesel'], 'Diesel')
        self.assertEqual(choices['bike_types']['ebike'], 'E-Bike')
        self.assertIn('gearbox', choices['categories']['autos']['filters'])
        self.assertIn('bike_type', choices['categories']['fahrraeder']['filters'])


if __name__ == '__main__':
    unittest.main()
