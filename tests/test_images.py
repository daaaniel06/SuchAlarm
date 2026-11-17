import io
import os
import sqlite3
import ssl
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import MagicMock, patch

from PIL import Image

from suchalarm.core import images


def webp():
    out = io.BytesIO()
    Image.new('RGB', (40, 30), 'blue').save(out, format='WEBP')
    return out.getvalue()


class ImageCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def job(self, key, url, priority=0, seen=1.0):
        with images.db() as conn:
            conn.execute(
                "INSERT INTO pictures(key,source,remote,state,seen,priority) VALUES (?,'ebay',?,'queued',?,?)",
                (key, 'https://i.ebayimg.com/' + key + '.jpg', seen, priority),
            )
            conn.execute('INSERT INTO offers VALUES (?,?,?)', (url, key, seen))

    def test_only_provider_cdns_over_https(self):
        self.assertTrue(images.valid('ebay', 'https://i.ebayimg.com/images/g/x/s-l300.jpg'))
        self.assertTrue(images.valid('vinted', 'https://images1.vinted.net/t/x.jpeg'))
        for url in (
            'http://i.ebayimg.com/x.jpg',
            'https://evil.example/x.jpg',
            'https://user:pw@i.ebayimg.com/x.jpg',
            'https://i.ebayimg.com:8443/x.jpg',
            'data:image/png;base64,AA',
        ):
            self.assertFalse(images.valid('ebay', url))

    async def test_hits_first_one_at_a_time(self):
        self.job('old', 'https://www.ebay.de/itm/1', seen=10)
        self.job('hit', 'https://www.ebay.de/itm/2', seen=1)
        images.prioritize(['https://www.ebay.de/itm/2'])
        with (
            patch('suchalarm.core.images.download', return_value=webp()) as download,
            patch('suchalarm.core.cadence.book'),
            patch('suchalarm.core.cadence.outcome'),
        ):
            await images.step()
        self.assertEqual(download.call_count, 1)
        self.assertIn('hit', download.call_args[0][1])
        ready, coming = images.photos(['https://www.ebay.de/itm/1', 'https://www.ebay.de/itm/2'])
        self.assertEqual((list(ready), coming), (['https://www.ebay.de/itm/2'], {'https://www.ebay.de/itm/1'}))

    async def test_failures_retry_later_and_give_up(self):
        self.job('a', 'https://www.ebay.de/itm/1')
        with (
            patch('suchalarm.core.images.download', side_effect=images.ImageFailure()),
            patch('suchalarm.core.cadence.book'),
        ):
            await images.step()
        with images.db() as conn:
            row = conn.execute('SELECT state,attempts,next_at FROM pictures').fetchone()
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_at'], time.time() + 3600)
        self.assertEqual(images.photos(['https://www.ebay.de/itm/1']), ({}, {'https://www.ebay.de/itm/1'}))
        with patch('suchalarm.core.images.download') as download, patch('suchalarm.core.cadence.book'):
            await images.step()
        download.assert_not_called()
        with images.db() as conn:
            conn.execute('UPDATE pictures SET attempts=?,next_at=0', (images.MAX_ATTEMPTS - 1,))
        with (
            patch('suchalarm.core.images.download', side_effect=images.ImageFailure()),
            patch('suchalarm.core.cadence.book'),
        ):
            await images.step()
        with images.db() as conn:
            row = conn.execute('SELECT state,attempts FROM pictures').fetchone()
            conn.execute('UPDATE pictures SET next_at=0')
        self.assertEqual((row['state'], row['attempts']), ('exhausted', images.MAX_ATTEMPTS))
        with patch('suchalarm.core.images.download') as download, patch('suchalarm.core.cadence.book'):
            await images.step()
        download.assert_not_called()
        self.assertEqual(images.photos(['https://www.ebay.de/itm/1']), ({}, set()))

    def test_given_up_pictures_do_not_fill_the_queue(self):
        self.job('a', 'https://www.ebay.de/itm/1')
        with images.db() as conn:
            conn.execute("UPDATE pictures SET state='exhausted',attempts=?", (images.MAX_ATTEMPTS,))
        with patch('suchalarm.core.images.MAX_PENDING', 1):
            images.remember('ebay', {'https://www.ebay.de/itm/2': 'https://i.ebayimg.com/b.jpg'})
        with images.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pictures').fetchone()[0], 2)

    def test_waiting_pictures_fill_the_queue(self):
        self.job('a', 'https://www.ebay.de/itm/1')
        with images.db() as conn:
            conn.execute('UPDATE pictures SET attempts=1,next_at=?', (time.time() + 21600,))
        with patch('suchalarm.core.images.MAX_PENDING', 1):
            images.remember('ebay', {'https://www.ebay.de/itm/2': 'https://i.ebayimg.com/b.jpg'})
        with images.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pictures').fetchone()[0], 1)

    def test_migrates_pending_pictures(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute(
                'CREATE TABLE pictures (key TEXT PRIMARY KEY, source TEXT, remote TEXT, state TEXT, '
                'filename TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0, seen REAL, bytes INTEGER DEFAULT 0)'
            )
            states = [
                ('new', 'pending', 0, 0),
                ('waiting', 'pending', images.MAX_ATTEMPTS - 1, 12345),
                ('limit', 'pending', images.MAX_ATTEMPTS, 0),
                ('over', 'pending', images.MAX_ATTEMPTS + 1, 0),
                ('ready', 'ready', 1, 0),
            ]
            conn.executemany('INSERT INTO pictures(key,state,attempts,next_at) VALUES (?,?,?,?)', states)
            images._setup(conn)
            images._setup(conn)
            self.assertEqual(
                conn.execute('SELECT key,state,attempts,next_at FROM pictures ORDER BY key').fetchall(),
                sorted(
                    (key, 'exhausted' if attempts >= images.MAX_ATTEMPTS else 'queued', attempts, next_at)
                    if state == 'pending'
                    else (key, state, attempts, next_at)
                    for key, state, attempts, next_at in states
                ),
            )

    async def test_budget_pause_does_not_use_an_attempt(self):
        self.job('a', 'https://www.ebay.de/itm/1')
        with (
            patch('suchalarm.core.images.download') as download,
            patch('suchalarm.core.cadence.book', side_effect=images.cadence.Deferred(time.time() + 1800)),
        ):
            await images.step()
        download.assert_not_called()
        with images.db() as conn:
            row = conn.execute('SELECT state,attempts,next_at FROM pictures').fetchone()
        self.assertEqual(tuple(row), ('queued', 0, 0))

    async def test_refusal_pauses_the_image_budget(self):
        self.job('a', 'https://www.ebay.de/itm/1')
        with (
            patch('suchalarm.core.images.download', side_effect=images.ImageFailure(blocked=True)),
            patch('suchalarm.core.cadence.book'),
            patch('suchalarm.core.cadence.outcome', return_value=time.time() + 1800) as outcome,
        ):
            await images.step()
        outcome.assert_called_once_with('ebay', 'image', True, 0)

    def test_cleanup_drops_old_and_orphaned_files(self):
        root = images.root()
        root.mkdir(parents=True, exist_ok=True)
        (root / ('b' * 64 + '.webp')).write_bytes(webp())
        (root / ('c' * 64 + '.webp')).write_bytes(webp())
        with images.db() as conn:
            conn.execute(
                "INSERT INTO pictures(key,source,remote,state,filename,seen,bytes) VALUES ('old','ebay','x','ready',?,?,10)",
                ('b' * 64 + '.webp', time.time() - 31 * 86400),
            )
            conn.execute(
                "INSERT INTO pictures(key,source,remote,state,filename,seen,bytes) VALUES ('new','ebay','y','ready',?,?,10)",
                ('c' * 64 + '.webp', time.time()),
            )
        images.cleanup()
        self.assertEqual(sorted(p.name for p in root.glob('*.webp')), ['c' * 64 + '.webp'])

    def test_tls_failure_closes_the_socket(self):
        sock = MagicMock()
        context = MagicMock()
        context.wrap_socket.side_effect = ssl.SSLError('bad certificate')
        with (
            patch('socket.getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]),
            patch('socket.create_connection', return_value=sock),
            patch('ssl.create_default_context', return_value=context),
            self.assertRaises(ssl.SSLError),
        ):
            images.download('ebay', 'https://i.ebayimg.com/x.jpg')
        sock.close.assert_called_once()

    def test_private_addresses_are_refused(self):
        with (
            patch('socket.getaddrinfo', return_value=[(None, None, None, None, ('192.168.1.5', 443))]),
            self.assertRaises(images.ImageFailure),
        ):
            images.download('ebay', 'https://i.ebayimg.com/x.jpg')


if __name__ == '__main__':
    unittest.main()
