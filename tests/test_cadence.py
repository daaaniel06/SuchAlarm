import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from suchalarm.core import cadence
from suchalarm.preis.domain import SOURCES, Offer
from suchalarm.wohnung.domain import SOURCES as FLAT_SOURCES


def berlin(hour):
    return datetime(2026, 10, 20, hour, 0, tzinfo=cadence.BERLIN).timestamp()


class CadenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_importing_the_core_alone_registers_every_source(self):
        code = (
            'from suchalarm.core import cadence, images; print(sorted(cadence.PROVIDERS), sorted(images.IMAGE_HOSTS))'
        )
        root = cadence.Path(__file__).resolve().parents[1]
        out = subprocess.run([sys.executable, '-c', code], cwd=root, capture_output=True, text=True, check=True).stdout
        every = sorted([*SOURCES, *FLAT_SOURCES])
        self.assertEqual(out.strip(), f'{every} {every}')

    def test_sources_of_one_site_share_its_budget_but_pause_alone(self):
        flats = cadence.Provider(
            cadence.PORTAL, lambda source, search: '', Offer, lambda items, search: items, 'kleinanzeigen'
        )
        cadence.register('kleinanzeigen-test', flats)
        self.addCleanup(cadence.PROVIDERS.pop, 'kleinanzeigen-test')
        cadence.book('kleinanzeigen')
        self.assertGreater(cadence.ready_at('kleinanzeigen-test'), time.time() + 35)
        self.assertLessEqual(cadence.ready_at('ebay'), time.time() + 30)  # another site only waits the global gap
        cadence.outcome('kleinanzeigen-test', 'page', blocked=True)
        self.assertGreater(cadence.blocked_until('kleinanzeigen-test'), time.time())
        self.assertEqual(cadence.blocked_until('kleinanzeigen'), 0)  # a flat portal's pause leaves prices alone

    def test_places_are_kept_per_source_and_old_location_ids_carry_over(self):
        old = cadence.sqlite3.connect(cadence.Path(self.tmp.name) / 'cadence.db')
        old.execute('CREATE TABLE locations (postcode TEXT PRIMARY KEY, id TEXT NOT NULL)')
        old.execute("INSERT INTO locations VALUES ('10115', '9668')")
        old.commit()
        old.close()
        self.assertEqual(cadence.location('kleinanzeigen', '10115'), '9668')
        self.assertIsNone(cadence.location('immowelt', '10115'))
        cadence.save_location('immowelt', '10115', 'ad08de8634')
        self.assertEqual(cadence.location('immowelt', '10115'), 'ad08de8634')
        self.assertEqual(cadence.location('kleinanzeigen', '10115'), '9668')

    def test_schema_is_created_once_and_again_after_the_file_is_gone(self):
        calls = []

        def setup(conn):
            calls.append(1)
            conn.execute('CREATE TABLE IF NOT EXISTS t (x)')

        path = cadence.Path(self.tmp.name) / 'once.db'
        for _ in range(3):
            cadence.connect(path, setup).close()
        self.assertEqual(len(calls), 1)
        path.unlink()
        conn = cadence.connect(path, setup)
        conn.execute('SELECT * FROM t')
        conn.close()
        self.assertEqual(len(calls), 2)

    def test_spacing_and_hourly_limit_persist(self):
        cadence.book('ebay')
        with self.assertRaises(cadence.Deferred):
            cadence.book('ebay')  # every db() call is a fresh connection: the limit lives on disk
        with self.assertRaises(cadence.Deferred):
            cadence.book('vinted')  # the shared spacing across providers applies too
        with cadence.db() as conn:
            conn.execute('DELETE FROM budget')
            conn.executemany(
                'INSERT INTO requests VALUES (?,?)', [('vinted:page', time.time() - 60)] * cadence.HOURLY['page']
            )
        with self.assertRaises(cadence.Deferred):
            cadence.book('vinted')

    def test_manual_short_gaps_keep_background_spacing_and_hourly_cap(self):
        start = time.time()
        with patch('suchalarm.core.cadence.time.time', return_value=start):
            cadence.book('ebay')
        with cadence.manual_check(start + 1):
            self.assertLessEqual(cadence.ready_at('ebay', now=start + 1), start + 12)
            self.assertLessEqual(cadence.ready_at('vinted', now=start + 1), start + 4)
            with patch('suchalarm.core.cadence.time.time', return_value=start + 13):
                cadence.book('ebay')
            self.assertGreaterEqual(cadence.ready_at('ebay', now=start + 13), start + 21)
        self.assertGreaterEqual(cadence.ready_at('ebay', now=start + 13), start + 53)
        with cadence.db() as conn:
            conn.executemany('INSERT INTO requests VALUES (?,?)', [('ebay:page', start)] * cadence.HOURLY['page'])
        with cadence.manual_check(start + 1):
            self.assertGreaterEqual(cadence.ready_at('ebay', now=start + 30), start + 3600)

    async def test_manual_ignores_old_query_and_page_cache_but_reuses_own_pages(self):
        start = time.time()
        url = 'https://www.ebay.de/sch/i.html?q=Spiel'
        with patch('suchalarm.core.cadence.time.time', return_value=start):
            cadence.save_page('ebay', url, {'old': True})
        actual = AsyncMock(return_value=([], None))
        fetch = cadence.shared_search(actual)
        search = {'query': 'Spiel'}
        await fetch('ebay', search)
        with cadence.manual_check(time.time() + 0.001):
            self.assertIsNone(cadence.cached_page('ebay', url))
        requested = time.time()
        with cadence.manual_check(requested):
            await fetch('ebay', search)
            await fetch('ebay', search)
            cadence.save_page('ebay', url, {'fresh': True})
            self.assertEqual(cadence.cached_page('ebay', url), {'fresh': True})
        self.assertEqual(actual.await_count, 2)
        self.assertFalse(cadence.MANUAL.get())

    def test_day_night_and_idealo_intervals(self):
        self.assertTrue(15 * 60 <= cadence.interval('ebay', berlin(12)) <= 19 * 60)
        self.assertTrue(15 * 60 <= cadence.interval('vinted', berlin(12)) <= 19 * 60)
        self.assertTrue(60 * 60 <= cadence.interval('kleinanzeigen', berlin(3)) <= 64 * 60)
        self.assertGreaterEqual(cadence.interval('idealo', berlin(12)), 12 * 3600)
        self.assertLess(cadence.interval('ebay', berlin(6)), 20 * 60)  # 06:00 is day again

    def test_refusals_pause_longer_each_time(self):
        first = cadence.outcome('ebay', 'page', True)
        self.assertAlmostEqual(first - time.time(), 1800, delta=5)
        second = cadence.outcome('ebay', 'page', True)
        self.assertAlmostEqual(second - time.time(), 3600, delta=5)
        self.assertTrue(cadence.blocked_until('ebay'))
        with self.assertRaises(cadence.Deferred):
            cadence.book('ebay')
        cadence.outcome('ebay', 'page', False)
        self.assertEqual(cadence.blocked_until('ebay'), 0)

    async def test_identical_queries_share_one_request(self):
        offers = [
            Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel PS5', 2000),
            Offer('ebay', 'https://www.ebay.de/itm/2', 'Spiel PS5 defekt', 900),
        ]
        inner = AsyncMock(return_value=(offers, None))
        fetch = cadence.shared_search(inner)
        first, _ = await fetch('ebay', {'query': 'Spiel  PS5', 'excluded': '', 'interval_minutes': 60})
        second, _ = await fetch('ebay', {'query': 'spiel ps5', 'excluded': 'defekt', 'interval_minutes': 60})
        self.assertEqual(inner.await_count, 1)  # "Jetzt prüfen" or a second search inside the interval asks nobody
        self.assertEqual(inner.await_args[0][1]['excluded'], '')
        self.assertEqual((len(first), len(second)), (2, 1))

    async def test_refusal_is_remembered_for_the_query(self):
        inner = AsyncMock(side_effect=cadence.Refused('Anbieter blockiert die Browser-Abfrage (HTTP 403).'))
        fetch = cadence.shared_search(inner)
        search = {'query': 'Spiel', 'excluded': '', 'interval_minutes': 60}
        with self.assertRaises(RuntimeError):
            await fetch('ebay', search)
        with self.assertRaises(cadence.Deferred):
            await fetch('ebay', search)
        self.assertEqual(inner.await_count, 1)
        self.assertTrue(cadence.blocked_until('ebay'))

    async def test_only_a_refusal_pauses_the_provider(self):
        # The error type decides, not its wording: this used to pause idealo for every search.
        fetch = cadence.shared_search(
            AsyncMock(
                side_effect=RuntimeError(
                    'idealo-Vergleich nicht lesbar oder keine eindeutig passende Variante gefunden.'
                )
            )
        )
        with self.assertRaises(cadence.Deferred):
            await fetch('idealo', {'query': 'Spiel', 'excluded': ''})
        self.assertFalse(cadence.blocked_until('idealo'))

    async def test_unreadable_page_retries_without_pausing_the_provider(self):
        from suchalarm.core.browser import Unreadable

        # A parser that no longer fits is a failure to fix, not a block that silences the portal for hours.
        fetch = cadence.shared_search(AsyncMock(side_effect=Unreadable('Ortssuche von Kleinanzeigen nicht lesbar.')))
        with self.assertRaises(cadence.Deferred):
            await fetch('kleinanzeigen', {'query': 'Spiel', 'excluded': ''})
        self.assertFalse(cadence.blocked_until('kleinanzeigen'))

    def test_legacy_search_interval_does_not_override_central_cadence(self):
        search = {'query': 'Spiel', 'sources': ['ebay'], 'interval_minutes': 180}
        now = time.time()
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries VALUES (?,?,?,?,0,'ok','{}')",
                (cadence.query_key('ebay', search), 'ebay', now, now + 1200),
            )
        self.assertAlmostEqual(cadence.query_due('ebay', cadence.query_state('ebay', search), now), now + 1200, delta=1)


if __name__ == '__main__':
    unittest.main()
