"""The rules of AGENTS.md as tests of their own: each one guards a class of errors, not one implementation."""

import ast
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from helpers import AsyncTempDataTest, TempDataTest

from suchalarm import digest, mailer, status, store, worker
from suchalarm.core import browser, cadence
from suchalarm.preis import worker as products
from suchalarm.preis.categories import CATEGORIES
from suchalarm.preis.prices import Offer, eligible
from suchalarm.wohnung import portals
from suchalarm.wohnung.domain import TRAITS, profile
from suchalarm.wohnung.domain import query_text as flat_query

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

    def test_every_module_that_sends_requests_books_them(self):
        """A new adapter must not reach a provider past the budget (core/cadence.py, book)."""
        sending = ('.goto(', 'urlopen(', 'HTTPSConnection(')
        for path in PACKAGE.rglob('*.py'):
            text = path.read_text()
            if any(call in text for call in sending):
                with self.subTest(file=str(path.relative_to(PACKAGE))):
                    self.assertIn('book(', text)

    def test_app_mailer_and_worker_ask_kinds_instead_of_naming_them(self):
        for name in ('app.py', 'mailer.py', 'digest.py', 'worker.py'):
            tree = ast.parse((PACKAGE / name).read_text())
            literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
            with self.subTest(file=name):
                self.assertFalse(literals & {'preis', 'wohnung'})


class SharedResultTests(TempDataTest):
    """Searches with the same query key share one provider request and its cached result: whatever changes the
    request (its URL) must change the key, or a search would get another search's result."""

    def assert_key_follows_request(self, pairs, request, key):
        for label, a, b in pairs:
            with self.subTest(label=label):
                if request(a) != request(b):
                    self.assertNotEqual(key(a), key(b))

    def test_product_searches(self):
        cadence.save_location('kleinanzeigen', '10115', '9668')
        cadence.save_location('kleinanzeigen', '20095', '9409')
        base = dict(
            id=1, kind='preis', query='Golf', alternatives='', min_price=0, max_price=500000, include_shipping=True,
            include_fee=True, excluded='', category='sonstiges', delivery='versand', plz='', radius=0,
            negotiable=False, free=False, filters={}, active=1,
        )  # fmt: skip
        changes = [
            dict(query='Golf 7'), dict(alternatives='Polo'), dict(min_price=10000), dict(max_price=400000),
            dict(include_shipping=False), dict(include_fee=False), dict(excluded='defekt'), dict(negotiable=True),
            dict(free=True), dict(delivery='abholung', plz='10115', radius=50),
            dict(delivery='beides', plz='10115', radius=50), dict(delivery='abholung', plz='20095', radius=100),
            *(dict(filters=f) for f in ({'year_from': 2015}, {'km_to': 100000}, {'fuel': 'diesel'},
                                        {'gearbox': 'manuell'}, {'platform': 'ps5'}, {'bike_type': 'ebike'})),
        ]  # fmt: skip
        for source in ('ebay', 'kleinanzeigen', 'vinted', 'idealo'):
            pairs = [
                (
                    f'{source} {category} {change}',
                    dict(base, category=category),
                    dict(base, category=category, **change),
                )
                for category in CATEGORIES
                for change in changes
            ]
            self.assert_key_follows_request(
                pairs,
                lambda s, source=source: products.search_url(source, s),
                lambda s, source=source: cadence.query_key(source, s),
            )

    def test_flat_searches(self):
        filters = {'min_area': 0, 'min_rooms': 1, 'exclusions': []}
        base = dict(id=1, kind='wohnung', query='Darmstadt', max_price=150000, excluded='', radius=0, filters=filters)
        changes = [
            dict(max_price=120000), dict(radius=10), dict(excluded='souterrain'),
            dict(filters={**filters, 'min_area': 50}), dict(filters={**filters, 'min_rooms': 3}),
            *(dict(filters={**filters, 'exclusions': [trait]}) for trait in TRAITS),
            dict(filters={**filters, 'move_in_from': '2026-12-01'}), dict(filters={**filters, 'move_in_by': '2027-01-01'}),
            dict(filters={**filters, 'excluded_districts': ['Eberstadt']}),
            dict(query='64283', filters={**filters, 'place': {'kind': 'plz', 'label': '64283', 'query': '64283'}}),
        ]  # fmt: skip
        requests = {
            'immoscout24': lambda p: portals.immoscout_url(p, '/de/hessen/darmstadt', (49.87, 8.65)),
            'kleinanzeigen-wohnung': lambda p: portals.kleinanzeigen_url(p, '4881'),
            'immowelt': lambda p: str(portals.immowelt_search('/classified-search?locations=AD08DE6345', p)),
            'wg-gesucht': lambda p: portals.wg_url(['darmstadt', 23], p),
            'ohne-makler': lambda p: portals.om_url('darmstadt', p),
        }
        for source, request in requests.items():
            pairs = [(f'{source} {change}', base, dict(base, **change)) for change in changes]
            self.assert_key_follows_request(
                pairs, lambda s, request=request: request(profile(s)), lambda s, source=source: flat_query(source, s)
            )


class PriceTests(unittest.TestCase):
    def test_unknown_included_shipping_is_no_total(self):
        offer = Offer('ebay', 'https://www.ebay.de/itm/1', 'Spiel', 1000, None)
        self.assertFalse(eligible(offer, dict(min_price=0, max_price=999999, include_shipping=True)))


class FlowTests(AsyncTempDataTest):
    """Find, send, restart: what the mail history says at each step."""

    ENV: ClassVar[dict[str, str]] = {'SMTP_HOST': 'smtp.example', 'SMTP_FROM': 'a@example.com'}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        with store.db() as conn:
            conn.execute("INSERT INTO searches(query,max_price,sources) VALUES ('Spiel',3500,'[\"ebay\"]')")
            conn.execute("INSERT INTO settings(key,value) VALUES ('recipients','[\"a@example.com\"]')")
            conn.execute("INSERT INTO settings(key,value) VALUES ('mail_enabled','1')")
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count,started) VALUES (1,'ebay',1,NULL,0,1)"
            )
        self.search = store.searches()[0]
        self.offer = Offer('ebay', 'https://www.ebay.de/itm/12345', 'Spiel', 3000, 0)

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
            await digest.send_digest()
        self.assertEqual(self.history(), [])
        with patch('suchalarm.mailer.deliver', return_value={}):
            await digest.send_digest(time.time() + digest.MAIL_RETRY_AFTER + 1)
        self.assertEqual(self.history(), [(3000, 'sent')])

    async def test_a_restart_while_sending_never_sends_twice_but_a_lower_price_still_comes(self):
        await self.found(self.offer)
        with store.db() as conn:
            self.assertIsNotNone(digest._claim_batch(conn, store.mail_settings(), time.time(), 'preis'))
        store.init()  # the server restarted before the SMTP answer
        self.assertEqual(self.history(), [(3000, 'uncertain')])
        with patch('suchalarm.mailer.deliver', return_value={}) as deliver:
            await self.found(self.offer)
            await digest.send_digest(time.time() + digest.PACKAGE_WAIT + 1)
            deliver.assert_not_called()
            await self.found(Offer('ebay', self.offer.url, 'Spiel', 2500, 0))
            await digest.send_digest(time.time() + 2 * digest.PACKAGE_WAIT)
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
