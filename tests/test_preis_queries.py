"""Byte-for-byte provider URLs and cache keys captured before splitting preis/domain.py."""

import json
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch

from suchalarm.core import cadence
from suchalarm.preis.categories import BIKE_TYPES, CATEGORIES, PLATFORMS, SOURCES
from suchalarm.preis.queries import query_text
from suchalarm.preis.sources import search_url
from suchalarm.preis.titles import wanted_words


def cases():
    profiles = {
        'plain': {},
        'limits': {'min_price': 2050, 'max_price': 4450, 'include_fee': True},
        'rich': {
            'min_price': 2050,
            'max_price': 4450,
            'include_shipping': True,
            'include_fee': True,
            'alternatives': ' RÉ Requiem , Evil  Requiem ',
            'negotiable': True,
            'free': True,
            'filters': {
                'platform': 'ps5',
                'year_from': 2012,
                'km_to': 104000,
                'fuel': 'benzin',
                'gearbox': 'manuell',
                'bike_type': 'ebike',
            },
        },
    }
    for category, delivery, source, (name, values) in product(
        CATEGORIES, ('versand', 'abholung', 'beides'), SOURCES, profiles.items()
    ):
        search = {
            'query': ' Rési & Evil/2 + ',
            'category': category,
            'delivery': delivery,
            'plz': '64283',
            'radius': 25,
            **values,
        }
        yield f'{category}/{delivery}/{source}/{name}', source, search
    for source, platform in product(SOURCES, PLATFORMS):
        yield (
            f'platform/{source}/{platform}',
            source,
            {
                'query': 'Mario Kart',
                'category': 'videospiele',
                'filters': {'platform': platform},
            },
        )
    for source, bike in product(SOURCES, BIKE_TYPES):
        yield (
            f'bike/{source}/{bike}',
            source,
            {
                'query': 'Rad',
                'category': 'fahrraeder',
                'filters': {'bike_type': bike},
            },
        )
    for source in SOURCES:
        yield f'ebike-words/{source}', source, {'query': 'Pedelec', 'category': 'fahrraeder'}
        for filters in ({'year_from': 2012}, {'km_to': 104000}):
            yield (
                f'car/{source}/{next(iter(filters))}',
                source,
                {
                    'query': 'VW Golf',
                    'category': 'autos',
                    'filters': filters,
                },
            )


class QueryCompatibilityTests(unittest.TestCase):
    def test_urls_and_keys_stay_byte_for_byte_identical(self):
        fixture = Path(__file__).parent / 'fixtures' / 'preis-queries.jsonl'
        expected = dict(json.loads(line) for line in fixture.read_text().splitlines())
        actual = {}
        with patch.object(cadence, 'location', return_value='1234'):
            for name, source, search in cases():
                with self.subTest(case=name):
                    result = [search_url(source, wanted_words(source, search), search), query_text(source, search)]
                    actual[name] = result
                    self.assertEqual(result, expected[name])
        self.assertEqual(actual.keys(), expected.keys())
