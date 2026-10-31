"""The map in a flat's sheet: address lookups, the stitched picture of a searched place, the endpoints."""

import asyncio
import io
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from suchalarm.core import cadence
from suchalarm.wohnung import maps, pages
from suchalarm.wohnung.domain import Listing
from tests.test_wohnung import TempData

DARMSTADT = [49.8728, 8.6512, 'Hessen']


def png():
    buffer = io.BytesIO()
    Image.new('RGB', (256, 256), (200, 220, 240)).save(buffer, 'PNG')
    return buffer.getvalue()


class QueryTests(unittest.TestCase):
    def test_address_then_postcode_then_town(self):
        self.assertEqual(
            maps.queries('Karlstraße 22, 64283 Darmstadt, Kapellplatzviertel'),
            ['Karlstraße 22, 64283 Darmstadt', '64283 Darmstadt', 'Darmstadt'],
        )
        self.assertEqual(maps.queries('Darmstadt, Bessungen'), ['Bessungen'])
        self.assertEqual(maps.queries(''), [])
        self.assertTrue(maps.has_street('Karlstraße 22, 64283 Darmstadt'))
        self.assertFalse(maps.has_street('64283 Darmstadt'))


class MapTests(TempData):
    def setUp(self):
        super().setUp()
        cadence.save_location('osm', 'darmstadt', json.dumps(DARMSTADT))
        self.search = self.stored_search(query='Darmstadt', sources=['wg-gesucht'])

    def build(self):
        with patch.object(maps.http, 'get', new=AsyncMock(return_value=png())) as get:
            while not maps.info(DARMSTADT[:2]):
                self.assertGreater(asyncio.run(maps.build_step([self.search])), 0)
        return get

    def test_the_place_is_stitched_once_from_its_tiles(self):
        get = self.build()
        self.assertEqual(get.await_count, len(maps._tiles(DARMSTADT[:2])))
        area = maps.areas([self.search])[self.search['id']]
        self.assertGreater(area['width'], 1000)
        self.assertLess(area['left'], area['centre_x'])
        self.assertTrue(maps.picture(area['key'] + '-dark'))
        self.assertFalse(list(maps.tile_dir().glob('*/*/*.png')))  # the picture holds them now
        self.assertEqual(asyncio.run(maps.build_step([self.search])), 0)
        self.assertIsNone(maps.picture('../suchalarm'))

    def test_a_deferred_tile_waits_for_the_next_step(self):
        with patch.object(maps.http, 'get', new=AsyncMock(side_effect=cadence.Deferred(time.time() + 60))):
            self.assertEqual(asyncio.run(maps.build_step([self.search])), 0)
        later = [cadence.Deferred(time.time() + 0.1), png(), cadence.Deferred(time.time() + 60)]
        with patch.object(maps.http, 'get', new=AsyncMock(side_effect=later)):  # the short wait is waited out
            self.assertEqual(asyncio.run(maps.build_step([self.search])), 1)
        self.assertIsNone(maps.info(DARMSTADT[:2]))

    def test_maps_of_places_no_search_uses_go(self):
        self.build()
        maps.cleanup([])
        self.assertFalse(list(maps.map_dir().glob('*.webp')))

    def test_a_flat_without_coordinates_is_looked_up_by_address_once(self):
        from fastapi.testclient import TestClient

        from suchalarm.app import app

        client = TestClient(app)
        flat = Listing.from_card(
            'wg-gesucht',
            {'url': 'https://www.wg-gesucht.de/1.html', 'title': 'W', 'place': 'Karlstraße 22, 64283 Darmstadt'},
        )
        pages.save_result(self.search, 'wg-gesucht', [flat], len([flat]), time.time())
        answer = [{'lat': '49.866', 'lon': '8.654'}]
        params = {'search_id': self.search['id'], 'url': flat.url}
        with patch.object(maps.http, 'get_json', new=AsyncMock(return_value=answer)) as get:
            self.assertEqual(
                client.get('/api/location', params=params).json(), {'lat': 49.866, 'lon': 8.654, 'approx': False}
            )
            client.get('/api/location', params=params)
        self.assertEqual(get.await_count, 1)
        with patch.object(maps.http, 'get_json', new=AsyncMock(side_effect=cadence.Deferred(time.time() + 1))):
            cadence.save_location('osm-address', 'Karlstraße 22, 64283 Darmstadt'.casefold(), 'null')  # not found
            self.assertEqual(client.get('/api/location', params=params).json(), {'wait': True})
        self.assertEqual(client.get('/api/location', params=dict(params, url='https://x.de/2')).status_code, 404)
        self.assertIn('maps', client.get('/api/state').json())


if __name__ == '__main__':
    unittest.main()
