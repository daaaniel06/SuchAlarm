"""The rules of AGENTS.md as tests of their own: each one guards a class of errors, not one implementation."""

import ast
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from suchalarm import mailer, status, store, worker
from suchalarm.core import browser
from suchalarm.preis.prices import Offer, eligible

PACKAGE = Path(__file__).resolve().parent.parent / 'suchalarm'


def imported_modules(path):
    """Every module a file imports, relative ones resolved against the suchalarm package."""
    tree = ast.parse(path.read_text())
    parts = path.relative_to(PACKAGE.parent).with_suffix('').parts
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = '.'.join(parts[: len(parts) - node.level]) if node.level else ''
            module = '.'.join(filter(None, (base, node.module)))
            names += [module] + [f'{module}.{alias.name}' for alias in node.names]
    return names


class StructureTests(unittest.TestCase):
    def test_core_knows_no_search_kind(self):
        for path in (PACKAGE / 'core').glob('*.py'):
            for name in imported_modules(path):
                with self.subTest(file=path.name, name=name):
                    self.assertFalse(name.startswith(('suchalarm.preis', 'suchalarm.wohnung')))

    def test_app_mailer_and_worker_ask_kinds_instead_of_naming_them(self):
        for name in ('app.py', 'mailer.py', 'worker.py'):
            tree = ast.parse((PACKAGE / name).read_text())
            literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
            with self.subTest(file=name):
                self.assertFalse(literals & {'preis', 'wohnung'})


class PriceTests(unittest.TestCase):
    def test_unknown_included_shipping_is_no_total(self):
        offer = Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel', 1000, None)
        self.assertFalse(eligible(offer, dict(min_price=0, max_price=999999, include_shipping=True)))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    """Find, send, restart: what the mail history says at each step."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ, {'DATA_DIR': self.tmp.name, 'SMTP_HOST': 'smtp.example', 'SMTP_FROM': 'a@example.com'}
        )
        self.env.start()
        store.init()
        with store.db() as conn:
            conn.execute("INSERT INTO searches(query,max_price,sources) VALUES ('Spiel',3500,'[\"ebay\"]')")
            conn.execute("INSERT INTO settings VALUES ('recipients','[\"a@example.com\"]')")
            conn.execute("INSERT INTO settings VALUES ('mail_enabled','1')")
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count,started) VALUES (1,'ebay',1,NULL,0,1)"
            )
        self.search = store.searches()[0]
        self.offer = Offer('ebay', 'https://www.ebay.de/itm/12345', 'Spiel', 3000, 0)

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def found(self, *offers, error=None):
        fetch = AsyncMock(side_effect=error) if error else AsyncMock(return_value=(list(offers), None))
        with patch('suchalarm.preis.sources.fetch', new=fetch):
            await worker.check(self.search)

    def history(self):
        with store.db() as conn:
            return [tuple(r) for r in conn.execute('SELECT price, reason FROM notifications ORDER BY price')]

    async def test_no_history_without_the_servers_yes(self):
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', side_effect=mailer.DeliveryError('Abgelehnt.', False)):
            await worker.send_digest()
        self.assertEqual(self.history(), [])
        with patch('suchalarm.mailer.deliver', return_value={}):
            await worker.send_digest(time.time() + worker.MAIL_RETRY_AFTER + 1)
        self.assertEqual(self.history(), [(3000, 'sent')])

    async def test_a_restart_while_sending_never_sends_twice_but_a_lower_price_still_comes(self):
        await self.found(self.offer)
        with store.db() as conn:
            self.assertIsNotNone(worker._claim_batch(conn, store.mail_settings(), time.time(), 'preis'))
        store.init()  # the server restarted before the SMTP answer
        self.assertEqual(self.history(), [(3000, 'uncertain')])
        with patch('suchalarm.mailer.deliver', return_value={}) as deliver:
            await self.found(self.offer)
            await worker.send_digest(time.time() + worker.PACKAGE_WAIT + 1)
            deliver.assert_not_called()
            await self.found(Offer('ebay', self.offer.url, 'Spiel', 2500, 0))
            await worker.send_digest(time.time() + 2 * worker.PACKAGE_WAIT)
            deliver.assert_called_once()
        self.assertEqual(self.history(), [(2500, 'sent')])

    async def test_a_blocked_source_is_disturbed_never_an_empty_result(self):
        await self.found(self.offer)
        await self.found(error=browser.Blocked('Anbieter blockiert die Browser-Abfrage (HTTP 403).'))
        with store.db() as conn:
            shown = status.of(conn, self.search['id'], 'ebay')
            kept = conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0]
        self.assertIn('HTTP 403', shown['error'])
        self.assertEqual((shown['count'], kept), (1, 1))  # the last result stays, shown as stale
