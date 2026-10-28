import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from suchalarm import scheduler, store, worker
from suchalarm.core import cadence
from suchalarm.preis import sources
from suchalarm.preis.domain import Offer


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        worker.progress = worker.Progress()

    def search(self, query='Spiel PS5', providers=('ebay',), active=True):
        with store.db() as conn:
            sid = conn.execute(
                'INSERT INTO searches(query,max_price,sources,active) VALUES (?,5000,?,?)',
                (query, json.dumps(providers), active),
            ).lastrowid
        return next(s for s in store.searches() if s['id'] == sid)

    def completed(self, job, stamp):
        search, source = job['searches'][0], job['source']
        with cadence.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO queries VALUES (?,?,?,?,0,'ok',?)",
                (
                    job['key'],
                    source,
                    stamp,
                    stamp + cadence.PROVIDERS[source].rhythm.day * 60,
                    json.dumps({'offers': [], 'reference': None}),
                ),
            )
        with store.db() as conn:
            for search in job['searches']:
                conn.execute(
                    'INSERT OR REPLACE INTO source_status(search_id,source,checked_at,error,count) VALUES (?,?,?,NULL,0)',
                    (search['id'], source, stamp),
                )
        scheduler.finish(job, stamp)

    def test_a_query_never_fetched_is_planned_even_if_its_search_has_an_old_status(self):
        # A status from an earlier query (e.g. before an edit) with no cached result for the new one.
        search = self.search()
        with store.db() as conn:
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count) VALUES (?,'ebay',1,NULL,0)",
                (search['id'],),
            )
        job, _ = scheduler.plan([search], False)
        self.assertEqual(job['searches'], [search])

    def test_parallel_clicks_are_atomically_deduplicated(self):
        from concurrent.futures import ThreadPoolExecutor

        search = self.search()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: scheduler.request(search), range(40)))
        self.assertEqual(sum(r['started'] for r in results), 1)
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual').fetchone()[0], 1)

    def test_manual_requests_persist_and_deduplicate(self):
        search = self.search(providers=('ebay', 'idealo'))
        scheduler.request(search)
        scheduler.request(search)
        self.assertEqual(scheduler.queued_ids(), [search['id']])
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual').fetchone()[0], 2)
        self.assertIsNotNone(scheduler.plan(store.searches(), True)[0])
        scheduler.cancel(search['id'], 'ebay')
        self.assertEqual(scheduler.queued_ids(), [search['id']])
        scheduler.cancel(search['id'])
        self.assertEqual(scheduler.queued_ids(), [])
        self.assertIsNone(scheduler.plan(store.searches(), True)[0])

    def test_global_spacing_stops_all_jobs_without_launching_browser(self):
        search = self.search(providers=('ebay', 'vinted'))
        job, _ = scheduler.plan([search], False)
        cadence.book(job['source'])
        self.completed(job, time.time())
        job, next_at = scheduler.plan([search], False)
        self.assertIsNone(job)
        self.assertGreater(next_at, time.time())

    def test_provider_spacing_does_not_block_another_provider(self):
        search = self.search(providers=('ebay', 'vinted'))
        job, _ = scheduler.plan([search], False)
        cadence.book('ebay')
        self.completed(job, time.time())
        future = time.time() + 31
        job, _ = scheduler.plan([search], False, future)
        self.assertEqual(job['source'], 'vinted')

    def test_waiting_jobs_rotate_fairly_and_survive_replanning(self):
        first = self.search('Erstes Spiel')
        second = self.search('Zweites Spiel')
        job, _ = scheduler.plan([first, second], False)
        self.assertEqual(job['searches'][0]['id'], first['id'])
        self.completed(job, time.time())
        # Both become due again; the older unserved job takes the next slot.
        with cadence.db() as conn:
            conn.execute('UPDATE queries SET next_at=0')
        job, _ = scheduler.plan([first, second], False)
        self.assertEqual(job['searches'][0]['id'], second['id'])
        self.assertEqual(scheduler.plan([first, second], False)[0]['key'], job['key'])

    def test_continuation_is_completed_before_starting_another_query(self):
        first = self.search('Erstes Spiel', ('idealo',))
        second = self.search('Zweites Spiel', ('idealo',))
        scheduler.plan([first, second], False)
        with cadence.db() as conn:
            conn.execute(
                "INSERT INTO queries(key,source,next_at,state) VALUES (?,?,0,'continuing')",
                (cadence.query_key('idealo', second), 'idealo'),
            )
        job, _ = scheduler.plan([first, second], False)
        self.assertEqual(job['searches'][0]['id'], second['id'])

    async def test_manual_idealo_refreshes_now_then_keeps_automatic_twelve_hours(self):
        search = self.search(providers=('idealo',))
        actual = AsyncMock(return_value=([], None))
        with (
            patch('suchalarm.preis.sources.fetch', cadence.shared_search(actual)),
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
        ):
            await worker.run_once()
            self.assertEqual(actual.await_count, 1)
            scheduler.request(search)
            await worker.run_once()
            self.assertEqual(actual.await_count, 2)
            self.assertIsNone(scheduler.plan([search], False, time.time() + 3600)[0])
            self.assertGreater(cadence.query_state('idealo', search)['next_at'], time.time() + 43190)

    async def test_spam_creates_one_run_and_one_upstream_request(self):
        first = self.search()
        second = self.search('spiel  ps5')
        for _ in range(100):
            scheduler.request(first)
            scheduler.request(second)
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0], 1)
            started = conn.execute('SELECT requested_at FROM manual_runs').fetchone()[0]
        actual = AsyncMock(return_value=([Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel PS5', 3000, 0)], None))
        with (
            patch('suchalarm.preis.sources.fetch', cadence.shared_search(actual)),
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
        ):
            await worker.run_once()
            for _ in range(100):
                result = scheduler.request(first)
                self.assertFalse(result['started'])
                self.assertGreater(result['retry_after'], 0)
            await worker.run_once()
            self.assertEqual(actual.await_count, 1)
        self.assertEqual(scheduler.queued_ids(), [])
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT requested_at FROM manual_runs').fetchone()[0], started)

    def test_spam_does_not_restart_a_finished_source_while_others_are_running(self):
        search = self.search(providers=('ebay', 'idealo'))
        start = time.time()
        with patch('time.time', return_value=start):
            scheduler.request(search)
            scheduler.cancel(search['id'], 'ebay')
        with patch('time.time', return_value=start + 90):
            self.assertFalse(scheduler.request(search)['started'])
        self.assertEqual(scheduler.queued_sources(), [dict(search_id=search['id'], source='idealo')])

    async def test_manual_transient_error_does_not_add_automatic_retries_to_click(self):
        search = self.search()
        scheduler.request(search)
        actual = AsyncMock(side_effect=RuntimeError('Anbieter antwortet mit HTTP 502.'))
        with (
            patch('suchalarm.preis.sources.fetch', cadence.shared_search(actual)),
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
        ):
            await worker.run_once()
            await worker.run_once()
        self.assertEqual(actual.await_count, 1)
        self.assertEqual(scheduler.queued_ids(), [])
        self.assertIn('HTTP 502', cadence.last_error('ebay', search))

    def test_manual_has_priority_over_automatic_jobs_and_normal_page_gaps(self):
        automatic = self.search('Erstes Spiel')
        manual = self.search('Zweites Spiel')
        cadence.book('ebay')
        scheduler.request(manual)
        job, _ = scheduler.plan([automatic, manual], False, time.time() + 13)
        self.assertEqual(job['searches'][0]['id'], manual['id'])
        self.assertTrue(job['manual_at'])
        self.assertGreater(cadence.ready_at('ebay'), time.time() + 35)

    async def test_blocked_manual_check_finishes_without_browser_or_waiting_list(self):
        search = self.search()
        cadence.outcome('ebay', 'page', True)
        scheduler.request(search)
        with (
            patch('suchalarm.core.browser.InvisiblePlaywright') as browser,
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
        ):
            await worker.run_once()
            browser.assert_not_called()
        self.assertEqual(scheduler.queued_ids(), [])
        with store.db() as conn:
            self.assertIn('Pause bis', conn.execute('SELECT error FROM source_status').fetchone()[0])

    async def test_same_query_runs_once_and_populates_all_searches(self):
        first = self.search('Spiel PS5')
        second = self.search('spiel  ps5')
        actual = AsyncMock(return_value=([Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel PS5', 3000, 0)], None))
        with (
            patch('suchalarm.preis.sources.fetch', cadence.shared_search(actual)),
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
        ):
            await worker.run_once()
        self.assertEqual(actual.await_count, 1)
        with store.db() as conn:
            self.assertEqual({r[0] for r in conn.execute('SELECT search_id FROM offers')}, {first['id'], second['id']})

    async def test_provider_pause_is_checked_before_browser_connection(self):
        search = self.search()
        cadence.outcome('ebay', 'page', True)
        self.assertIsNone(scheduler.plan([search], False)[0])
        with patch('suchalarm.core.browser.InvisiblePlaywright') as browser:
            with self.assertRaises(cadence.Deferred):
                await sources.fetch('ebay', search)
            browser.assert_not_called()

    def test_deleted_and_changed_searches_leave_no_ghost_requests(self):
        search = self.search()
        scheduler.request(search)
        scheduler.plan([], False)
        self.assertEqual(scheduler.queued_ids(), [])
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    async def test_two_step_idealo_visit_resumes_from_cache_after_spacing(self):
        search = self.search(providers=('idealo',))
        scheduler.request(search)
        page = SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            evaluate=AsyncMock(
                return_value={
                    'rows': [{'title': 'Spiel PS5'}],
                    'structured': [],
                    'links': [],
                    'text': 'Spiel PS5',
                    'title': 'Spiel PS5',
                }
            ),
            wait_for_timeout=AsyncMock(),
        )

        async def visit(source, search):
            await sources.load_page(page, source, sources.search_url(source, search['query']))
            await sources.load_page(
                page, source, 'https://www.idealo.de/preisvergleich/OffersOfProduct/1_spiel.html', follow=True
            )
            return [
                Offer(source, 'https://www.idealo.de/preisvergleich/OffersOfProduct/1_spiel.html', 'Spiel PS5', 3000, 0)
            ], None

        start = time.time()
        with (
            patch('suchalarm.preis.sources.fetch', cadence.shared_search(visit)),
            patch('suchalarm.worker.send_digest', new=AsyncMock()),
            patch('suchalarm.worker.daily_backup', new=AsyncMock()),
            patch('time.time', return_value=start) as clock,
        ):
            await worker.run_once()
            self.assertEqual(page.goto.await_count, 1)
            self.assertEqual(cadence.query_state('idealo', search)['state'], 'continuing')
            self.assertEqual(scheduler.queued_ids(), [search['id']])
            job, allowed = scheduler.plan([search], False)
            self.assertIsNone(job)
            self.assertGreaterEqual(allowed - start, 8)
            self.assertLessEqual(allowed - start, 12)
            clock.return_value = allowed + 0.1
            await worker.run_once()
            self.assertEqual(page.goto.await_count, 2)  # search is reused; only the detail page is fetched now
            self.assertGreaterEqual(cadence.query_state('idealo', search)['next_at'], clock.return_value + 43200)
            self.assertEqual(scheduler.queued_ids(), [])
            with cadence.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 2)
