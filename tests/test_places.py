"""A portal's own place search: asked once per place, kept for good, an unknown place rests the source."""

from unittest.mock import AsyncMock, patch

from tests.helpers import AsyncTempDataTest

from suchalarm.core import cadence
from suchalarm.wohnung import sources
from suchalarm.wohnung.domain import profile


def flat_search(query='Darmstadt', **filters):
    return dict(id=1, kind='wohnung', query=query, max_price=150000, radius=0, filters=filters)


class ResolvePlaceTests(AsyncTempDataTest):
    async def test_a_found_place_is_asked_once_and_booked_as_a_visit(self):
        p, lookup = profile(flat_search()), AsyncMock(return_value='4868')
        with patch('suchalarm.core.cadence.book') as book:
            self.assertEqual(
                await sources._resolve_place('kleinanzeigen-wohnung', p, lookup, book=True), ('4868', True)
            )
            self.assertEqual(
                await sources._resolve_place('kleinanzeigen-wohnung', p, lookup, book=True), ('4868', False)
            )
        lookup.assert_awaited_once()
        book.assert_called_once_with('kleinanzeigen-wohnung', 'page')
        self.assertEqual(cadence.location('kleinanzeigen', 'darmstadt'), '4868')  # shared with product searches

    async def test_an_unknown_place_fails_once_and_then_rests_the_source(self):
        search = flat_search('Nirgendwo')
        lookup = AsyncMock(return_value=None)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'Immowelt kennt den Ort „Nirgendwo“ nicht'):
                await sources._resolve_place('immowelt', profile(search), lookup)
        lookup.assert_awaited_once()
        self.assertEqual(sources.out_of_area(search, 'immowelt'), 'Kennt diesen Ort nicht')
        self.assertIsNone(sources.out_of_area(search, 'immoscout24'))

    async def test_wg_gesucht_asks_by_the_suggested_label(self):
        place = {'kind': 'city', 'label': 'Frankfurt am Main', 'query': 'Frankfurt'}
        p, lookup = profile(flat_search('Frankfurt am Main', place=place)), AsyncMock(return_value=None)
        with self.assertRaises(RuntimeError):
            await sources._resolve_place('wg-gesucht', p, lookup)
        self.assertEqual(cadence.location('wg-gesucht', 'frankfurt am main'), sources.UNKNOWN_PLACE)
