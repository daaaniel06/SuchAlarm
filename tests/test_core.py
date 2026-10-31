import asyncio
import json
import os
import smtplib
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from suchalarm import mailer, scheduler, store, worker
from suchalarm.app import app, create, delete, update
from suchalarm.core import browser, cadence, images
from suchalarm.preis import sources
from suchalarm.preis.domain import Offer, canonical, clean_title, eligible, matches, money, select_offers, shipping
from suchalarm.preis.models import Search as ProductSearch
from suchalarm.preis.sources import parse_rows

PAYLOAD = dict(query='Resident Evil Requiem', max_price=3500, sources=['ebay'], include_shipping=False, active=False)


@contextmanager
def without_smtp_env():
    """A clean slate for the SMTP_* variables, whatever the machine running the tests has set."""
    with patch.dict(os.environ):
        for key in [key for key in os.environ if key.startswith('SMTP_')]:
            del os.environ[key]
        yield


class DomainTests(unittest.TestCase):
    def test_price_and_shipping(self):
        self.assertEqual(money('1.234,56 €'), 123456)
        self.assertEqual(money('34.95'), 3495)
        self.assertEqual(money('1.050 €'), 105000)
        self.assertEqual(money('1,234.56'), 123456)
        self.assertEqual(money('1,05 €'), 105)
        self.assertEqual(shipping('+ 4,50 € Versand'), 450)
        self.assertEqual(shipping('Kostenloser Versand'), 0)
        self.assertEqual(shipping('Versand: 5,49 €'), 549)
        self.assertEqual(shipping('+EUR 6,19 · 2-3 Tage Lieferung'), 619)  # eBay cards since October 2026
        self.assertIsNone(shipping('Versand möglich'))
        self.assertIsNone(money('NaN'))
        self.assertIsNone(money('-2'))

    def test_shipping_switch_and_strict_threshold(self):
        offer = Offer('ebay', 'https://www.ebay.de/itm/123', 'Spiel', 3000, 500)
        self.assertFalse(eligible(offer, dict(max_price=3500, include_shipping=True)))
        self.assertTrue(eligible(offer, dict(max_price=3500, include_shipping=False)))
        self.assertFalse(eligible(replace(offer, shipping=None), dict(max_price=3500, include_shipping=True)))
        self.assertTrue(eligible(replace(offer, shipping=None), dict(max_price=3500, include_shipping=False)))

    def test_matching_and_safe_identity(self):
        self.assertTrue(matches('Resident Evil: Requiem – PS5', 'resident evil requiem ps5'))
        self.assertTrue(matches('Résident evil requiem PS5', 'resident evil requiem ps5'))
        self.assertFalse(matches('Resident Evil Requiem Hulle', 'resident evil requiem', 'Hülle'))
        self.assertFalse(matches('Resident Evil Requiem Hülle', 'resident evil requiem', 'Hülle, defekt'))
        self.assertFalse(matches('Resident Evil Requiem PS50', 'resident evil requiem ps5'))
        self.assertTrue(matches('Capcom Resident Evil 3 – Sony PlayStation 5 (PAL)', 'resident evil 3 ps5'))
        self.assertTrue(matches('Resident Evil 3 PS 5', 'resident evil 3 playstation 5'))
        self.assertFalse(matches('Resident Evil 3 PS 4', 'resident evil 3 ps5'))
        self.assertFalse(matches('Resident Evil 3 PlayStation 4', 'resident evil 3', 'ps4'))
        self.assertTrue(matches('Nintendo Switch Produkt Maxi', 'nintendo switch', 'pro, max'))
        self.assertFalse(matches('iPhone 13 Pro Max', 'iphone 13', 'pro max'))
        self.assertFalse(matches('Switch OLED ohne Spiel', 'switch', 'ohne spiel'))

    def test_two_words_written_as_one_still_match(self):
        self.assertTrue(matches("Marvel's Spiderman 2 PS5", 'spider-man 2'))
        self.assertTrue(matches('Marvel Spider-Man 2', 'spiderman 2'))
        self.assertTrue(matches('Apple iPhone13 128GB', 'iphone 13'))
        self.assertTrue(matches('Apple iPhone 13 128GB', 'iphone13'))
        self.assertFalse(matches('Apple iPhone 12 128GB', 'iphone13'))
        self.assertFalse(matches('Marvel Spider 2', 'spider-man 2'))  # one half alone is not enough
        self.assertEqual(
            canonical('ebay', 'https://www.ebay.de/itm/Spiel/12345?tracking=x'), 'https://www.ebay.de/itm/12345'
        )
        self.assertIsNone(canonical('ebay', 'https://evil.example/itm/12345'))
        self.assertIsNone(canonical('vinted', 'javascript:alert(1)'))

    def test_ebay_ui_labels_are_not_seller_titles(self):
        actual = 'Resident Evil Requiem PS5 – gebraucht (OHNE SPIEL)'
        dirty = 'Neues Angebot' + actual + 'Wird in neuem Fenster oder Tab geöffnet'
        self.assertEqual(clean_title('ebay', dirty), actual)
        self.assertEqual(clean_title('ebay', 'Neues Angebot – Spiel PS5'), 'Neues Angebot – Spiel PS5')
        self.assertEqual(clean_title('vinted', dirty), dirty)
        original = Offer('ebay', 'https://www.ebay.de/itm/1', dirty, 3000, 0)
        result = select_offers([original], dict(query='resident evil requiem ps5', excluded='Fenster'))
        self.assertEqual(result[0].title, actual)
        self.assertEqual(original.title, dirty)
        row = dict(title=dirty, url=original.url, price='30 €')
        self.assertEqual(
            parse_rows('ebay', [row], dict(query='resident evil requiem ps5', excluded='Fenster'))[0].title, actual
        )

    def test_source_parsing(self):
        search = dict(query='Spiel PS5', excluded='defekt')
        base = dict(
            title='Spiel PS5', url='https://www.ebay.de/itm/12345', price='24,50 €', shipping='Kostenloser Versand'
        )
        offers = parse_rows('ebay', [base], search)
        self.assertEqual((offers[0].price, offers[0].shipping), (2450, 0))
        self.assertEqual(parse_rows('ebay', [dict(base, price='EUR 24,50')], search)[0].price, 2450)
        self.assertEqual(parse_rows('ebay', [dict(base, price='1.050 €')], search)[0].price, 105000)
        self.assertEqual(parse_rows('ebay', [dict(base, price='EUR 1.050,50')], search)[0].price, 105050)
        for price in ('20 bis 40 €', '25,00 € VB', '20 USD'):
            self.assertEqual(parse_rows('ebay', [dict(base, price=price)], search), [])
        self.assertEqual(parse_rows('ebay', [dict(base, title='Spiel PS5 defekt')], search), [])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
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
            # The source was read before: new offers are news, not the starting point.
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count,started) VALUES (1,'ebay',1,NULL,0,1)"
            )
        self.search = store.searches()[0]
        self.offer = Offer('ebay', 'https://www.ebay.de/itm/12345', 'Spiel', 3000, 0)

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        worker.progress = worker.Progress()

    async def found(self, *offers):
        with patch('suchalarm.preis.sources.fetch', new=AsyncMock(return_value=(list(offers), None))):
            await worker.check(self.search)

    def count(self, table, where=''):
        with store.db() as conn:
            return conn.execute(f'SELECT count(*) FROM {table} {where}').fetchone()[0]

    def setting(self, key, value):
        with store.db() as conn:
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))

    async def test_starting_point_is_not_news(self):
        with store.db() as conn:
            conn.execute('DELETE FROM source_status')
        await self.found(self.offer)
        self.assertEqual((self.count('outbox'), self.count('notifications')), (0, 1))
        await self.found(self.offer, replace(self.offer, url='https://www.ebay.de/itm/2'))
        self.assertEqual(self.count('outbox'), 1)

    async def test_a_queued_offer_pushed_off_the_first_page_still_goes_out(self):
        self.setting('frequency', 'Täglich')
        await self.found(self.offer)
        newer = [replace(self.offer, url=f'https://www.ebay.de/itm/{n}', price=4000) for n in range(3)]
        await self.found(*newer)  # newer offers above the limit push it off the page
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest(time.time() + 86401)
        deliver.assert_called_once()
        self.assertIn(self.offer.url, deliver.call_args.args[0].get_body(('plain',)).get_content())
        await self.found(*newer)  # mailed: no longer kept beside the page
        with store.db() as conn:
            self.assertEqual(
                conn.execute('SELECT count(*) FROM offers WHERE url=?', (self.offer.url,)).fetchone()[0], 0
            )

    async def test_a_queued_offer_survives_an_edit_of_the_search(self):
        self.setting('frequency', 'Täglich')
        await self.found(self.offer)
        await update(1, ProductSearch(query='Spiel', max_price=3500, min_price=100, sources=['ebay']))
        self.search = store.search(1)
        await worker.send_digest()  # a pass before the daily mail is due
        await self.found(self.offer)  # the first result after the edit is a new starting point
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest(time.time() + 86401)
        deliver.assert_called_once()
        self.assertIn(self.offer.url, deliver.call_args.args[0].get_body(('plain',)).get_content())

    async def test_a_queued_offer_with_a_newly_excluded_word_is_not_mailed(self):
        await self.found(replace(self.offer, title='Spiel defekt'))
        await update(1, ProductSearch(query='Spiel', max_price=3500, excluded='defekt', sources=['ebay']))
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
        deliver.assert_not_called()

    async def test_dedup_and_further_drop(self):
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await self.found(self.offer)
            await worker.send_digest()
            await self.found(self.offer)
            await worker.send_digest()
            self.assertEqual(deliver.call_count, 1)
            await self.found(replace(self.offer, price=2900))
            await worker.send_digest()
            self.assertEqual(deliver.call_count, 2)
            self.assertEqual(deliver.call_args[0][0]['Subject'], 'Preisalarm: Spiel für 29,00 €')

    async def test_vinted_hit_waits_for_its_shipping_and_only_hits_are_looked_up(self):
        from suchalarm.preis import delivery

        with store.db() as conn:
            conn.execute('UPDATE searches SET sources=\'["vinted"]\', include_shipping=0')
            conn.execute("UPDATE source_status SET source='vinted'")
        self.search = store.searches()[0]
        hit = Offer('vinted', 'https://www.vinted.de/items/1', 'Spiel', 3000)
        await self.found(hit, replace(hit, url='https://www.vinted.de/items/2', price=3500))
        with delivery.db() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT url FROM deliveries')], [hit.url])
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
            deliver.assert_not_called()
            with patch('time.time', return_value=time.time() + 15 * 60 + 1):
                await worker.send_digest()
        self.assertIn('Versand unbekannt', deliver.call_args[0][0].get_body(('plain',)).get_content())

    async def test_one_mail_for_several_offers(self):
        await self.found(self.offer, replace(self.offer, url='https://www.ebay.de/itm/2', price=2000))
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
        message = deliver.call_args[0][0]
        self.assertEqual(message['Subject'], 'Preisalarm: 2 neue Angebote')
        self.assertIn('https://www.ebay.de/itm/2', message.get_body(('plain',)).get_content())
        self.assertIn('neue Angebote</span>', message.get_body(('html',)).get_content())

    async def test_definite_failure_retries_after_a_pause(self):
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', side_effect=mailer.DeliveryError('Mailserver nicht erreichbar.', False)):
            await worker.send_digest()
        self.assertEqual((self.count('notifications'), self.count('outbox', 'WHERE batch IS NULL')), (0, 1))
        self.assertIn('Mailserver nicht erreichbar.', store.settings()['mail_error'])
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
            deliver.assert_not_called()
            await worker.send_digest(time.time() + 601)
            deliver.assert_called_once()
        self.assertNotIn('mail_error', store.settings())

    async def test_uncertain_outcome_is_never_resent(self):
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', side_effect=mailer.DeliveryError('Verbindung abgebrochen.', True)):
            await worker.send_digest()
        self.assertEqual(
            (self.count('notifications'), self.count('outbox'), self.count('batches', "WHERE status='unknown'")),
            (1, 0, 1),
        )
        await self.found(self.offer)
        self.assertEqual(self.count('outbox'), 0)

    async def test_a_refused_recipient_is_shown_and_the_others_get_nothing_twice(self):
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', return_value={'b@example.com': 550}):
            await worker.send_digest()
        self.assertEqual((self.count('outbox'), self.count('batches', "WHERE status='sent'")), (0, 1))
        self.assertIn('b@example.com nicht angenommen', store.settings()['mail_error'])
        with store.db() as conn:
            self.assertIn('b@example.com', store.mail_log(conn, 1)[0]['error'])

    async def test_a_search_deleted_while_its_mail_goes_out_leaves_the_others_booked(self):
        with store.db() as conn:
            conn.execute("INSERT INTO searches(query,max_price,sources) VALUES ('Konsole',3500,'[\"ebay\"]')")
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count,started) VALUES (2,'ebay',1,NULL,0,1)"
            )
        other = store.search(2)
        await self.found(self.offer)
        with patch(
            'suchalarm.preis.sources.fetch',
            new=AsyncMock(return_value=([replace(self.offer, url='https://www.ebay.de/itm/2', title='Konsole')], None)),
        ):
            await worker.check(other)
        self.assertEqual(self.count('outbox'), 2)

        def delete_while_sending(message):
            with store.db() as conn:
                store.delete_search(conn, 2)
            return []

        with patch('suchalarm.mailer.deliver', side_effect=delete_while_sending):
            await worker.send_digest()
        self.assertEqual((self.count('outbox'), self.count('batches', "WHERE status='sent'")), (0, 1))
        with store.db() as conn:
            self.assertEqual(
                [tuple(r) for r in conn.execute('SELECT search_id, url FROM notifications')], [(1, self.offer.url)]
            )

    async def test_a_reused_search_id_gets_no_receipt_from_the_previous_search(self):
        await self.found(self.offer)

        async def recreate_while_sending(deliver, message):
            await delete(self.search['id'])
            created = await create(ProductSearch(query='Laptop', max_price=5000, sources=['ebay']))
            self.assertEqual(created['id'], self.search['id'])
            return {}

        with patch('suchalarm.worker.asyncio.to_thread', side_effect=recreate_while_sending):
            await worker.send_digest()
        self.assertEqual(store.search(self.search['id'])['query'], 'Laptop')
        self.assertEqual((self.count('notifications'), self.count('outbox')), (0, 0))
        self.assertEqual(self.count('batches', "WHERE status='sent'"), 1)

    async def test_a_price_basis_edit_while_sending_does_not_swallow_a_later_drop(self):
        with store.db() as conn:
            conn.execute('UPDATE searches SET include_shipping=0, max_price=5000 WHERE id=1')
        self.search = store.search(1)
        offer = replace(self.offer, price=1000, shipping=2000)
        await self.found(offer)

        async def edit_while_sending(deliver, message):
            await update(1, ProductSearch(query='Spiel', max_price=5000, sources=['ebay'], include_shipping=True))
            return {}

        with patch('suchalarm.worker.asyncio.to_thread', side_effect=edit_while_sending):
            await worker.send_digest()
        self.assertEqual(self.count('notifications'), 0)
        self.assertEqual(self.count('batches', "WHERE status='sent'"), 1)
        self.search = store.search(1)
        await self.found(offer)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT price FROM notifications').fetchone()[0], 3000)
        await self.found(replace(offer, price=500))
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT price FROM outbox').fetchone()[0], 2500)

    async def test_a_price_basis_edit_before_restart_does_not_restore_the_old_receipt(self):
        await self.found(self.offer)
        with store.db() as conn:
            self.assertIsNotNone(worker._claim_batch(conn, store.mail_settings(), time.time(), 'preis'))
        await update(1, ProductSearch(query='Spiel', max_price=3500, sources=['ebay'], include_shipping=False))
        store.init()
        self.assertEqual((self.count('notifications'), self.count('outbox')), (0, 0))
        self.assertEqual(self.count('batches', "WHERE status='unknown'"), 1)

    async def test_pausing_while_sending_still_records_the_accepted_mail(self):
        await self.found(self.offer)

        async def pause_while_sending(deliver, message):
            await update(1, ProductSearch(query='Spiel', max_price=3500, sources=['ebay'], active=False))
            return {}

        with patch('suchalarm.worker.asyncio.to_thread', side_effect=pause_while_sending):
            await worker.send_digest()
        self.assertEqual((self.count('notifications'), self.count('outbox')), (1, 0))

    async def test_mails_off_means_no_flood_later(self):
        await self.found(self.offer)
        self.setting('mail_enabled', '0')
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
            deliver.assert_not_called()
        await self.found(self.offer, replace(self.offer, url='https://www.ebay.de/itm/2'))
        self.setting('mail_enabled', '1')
        await self.found(self.offer, replace(self.offer, url='https://www.ebay.de/itm/2'))
        self.assertEqual(self.count('outbox'), 0)

    async def test_rhythm_and_packages(self):
        self.setting('batch_size', '2')
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
            deliver.assert_not_called()
            await worker.send_digest(time.time() + 7201)
            deliver.assert_called_once()
        self.setting('frequency', 'Täglich')
        await self.found(replace(self.offer, url='https://www.ebay.de/itm/2'))
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest(time.time() + 3600)
            deliver.assert_not_called()
            await worker.send_digest(time.time() + 86401)
            deliver.assert_called_once()

    async def test_restart_during_delivery_counts_as_sent(self):
        with store.db() as conn:
            conn.execute("INSERT INTO outbox VALUES (1,'https://www.ebay.de/itm/12345',3000,1,'b1')")
            conn.execute("INSERT INTO batches VALUES ('b1','a@example.com','x',1,'sending',NULL,1,NULL)")
        store.init()
        self.assertEqual(
            (self.count('outbox'), self.count('notifications'), self.count('batches', "WHERE status='unknown'")),
            (0, 1, 1),
        )

    async def test_refusal_pause_shows_and_keeps_offers(self):
        await self.found(self.offer)
        cadence.outcome('ebay', 'page', True)
        with patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=cadence.Deferred(time.time() + 1800))):
            await worker.check(self.search)
        with store.db() as conn:
            self.assertIn('Pause bis', conn.execute('SELECT error FROM source_status').fetchone()[0])
            self.assertEqual(conn.execute('SELECT count(*) FROM offers').fetchone()[0], 1)

    async def test_plain_wait_keeps_last_result(self):
        await self.found(self.offer)
        with patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=cadence.Deferred(time.time() + 60))):
            await worker.check(self.search)
        with store.db() as conn:
            self.assertIsNone(conn.execute('SELECT error FROM source_status').fetchone()[0])

    def picture(self, url, state='ready'):
        root = images.root()
        root.mkdir(parents=True, exist_ok=True)
        name = 'a' * 64 + '.webp'
        Image.new('RGB', (32, 24), 'red').save(root / name, format='WEBP')
        with images.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pictures(key,source,remote,state,filename,seen) VALUES ('k','ebay','https://i.ebayimg.com/x.jpg',?,?,1)",
                (state, name if state == 'ready' else None),
            )
            conn.execute("INSERT OR REPLACE INTO offers VALUES (?,'k',1)", (url,))

    async def test_pictures_travel_inside_the_mail(self):
        self.picture(self.offer.url)
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
        message = deliver.call_args[0][0]
        html = message.get_body(('html',)).get_content()
        self.assertIn('src="cid:', html)
        self.assertNotIn('ebayimg', html)
        self.assertEqual(
            [part.get_content_type() for part in message.walk() if part.get_content_maintype() == 'image'],
            ['image/jpeg'],
        )

    async def test_unreadable_picture_never_blocks_the_mail(self):
        self.picture(self.offer.url)
        (images.root() / ('a' * 64 + '.webp')).write_bytes(b'kaputt')
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
        message = deliver.call_args[0][0]
        self.assertNotIn('src="cid:', message.get_body(('html',)).get_content())

    async def test_sofort_waits_briefly_for_a_picture(self):
        self.picture(self.offer.url, state='pending')
        await self.found(self.offer)
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
            deliver.assert_not_called()
            await worker.send_digest(time.time() + 301)
            deliver.assert_called_once()

    async def shop_case(self, shop_price, used_price, shipping=0, include_shipping=True, fresh=True):
        with store.db() as conn:
            conn.execute(
                'UPDATE searches SET sources=?,include_shipping=? WHERE id=1',
                ('["ebay","idealo"]', int(include_shipping)),
            )
            conn.execute(
                "INSERT INTO source_status(search_id,source,checked_at,error,count,started) VALUES (1,'idealo',1,NULL,0,1)"
            )
        self.search = store.searches()[0]
        used = Offer('ebay', 'https://www.ebay.de/itm/7', 'Spiel', used_price, shipping)
        shop = Offer('idealo', 'https://www.idealo.de/preisvergleich/OffersOfProduct/9_x.html', 'Spiel', shop_price, 0)

        async def fetch(source, search):
            return ([used] if source == 'ebay' else [shop]), None

        with patch('suchalarm.preis.sources.fetch', new=fetch):
            await worker.check(self.search)
        if not fresh:
            with store.db() as conn:
                conn.execute("UPDATE source_status SET checked_at=1 WHERE source='ebay'")
                conn.execute("UPDATE offers SET last_seen=1 WHERE source='ebay'")
        with patch('suchalarm.mailer.deliver', return_value=[]) as deliver:
            await worker.send_digest()
        return deliver.call_args[0][0].get_body(('plain',)).get_content() if deliver.called else ''

    async def test_shop_cheaper_than_every_used_offer_says_so(self):
        body = await self.shop_case(2500, 3000, shipping=200)
        self.assertIn('Neu im Shop 7,00 € günstiger als das günstigste Gebrauchtangebot (32,00 €)', body)

    async def test_shop_comparison_needs_known_shipping_and_fresh_results(self):
        body = await self.shop_case(2500, 3000, shipping=None, include_shipping=True)
        self.assertIn('Spiel', body)
        self.assertNotIn('Neu im Shop', body)

    async def test_shop_comparison_ignores_stale_used_results(self):
        body = await self.shop_case(2500, 3000, fresh=False)
        self.assertIn('Spiel', body)
        self.assertNotIn('Neu im Shop', body)

    async def test_shop_not_cheaper_has_no_reason(self):
        body = await self.shop_case(2900, 2800)
        self.assertIn('Spiel', body)
        self.assertNotIn('Neu im Shop', body)

    async def test_price_range_keeps_junk_out(self):
        with store.db() as conn:
            conn.execute('UPDATE searches SET min_price=1000 WHERE id=1')
        self.search = store.searches()[0]
        junk = replace(self.offer, url='https://www.ebay.de/itm/3', price=300)
        await self.found(self.offer, junk)
        with store.db() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT url FROM outbox')], [self.offer.url])
        self.assertFalse(eligible(replace(junk, price=999), self.search))
        self.assertTrue(eligible(replace(junk, price=1000), self.search))

    async def test_hidden_offers_are_never_mailed(self):
        with store.db() as conn:
            conn.execute('INSERT INTO hidden VALUES (1,?,1)', (self.offer.url,))
        await self.found(self.offer)
        self.assertEqual(self.count('outbox'), 0)

    async def test_first_result_of_a_source_is_marked_as_start(self):
        with store.db() as conn:
            conn.execute('DELETE FROM source_status')  # a brand-new search
        await self.found(self.offer)
        later = replace(self.offer, url='https://www.ebay.de/itm/99')
        await self.found(self.offer, later)
        with store.db() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT url FROM start_offers')], [self.offer.url])

    async def test_a_failure_after_an_empty_start_does_not_swallow_the_first_offer(self):
        with store.db() as conn:
            conn.execute('DELETE FROM source_status')  # a brand-new search
        await self.found()  # read fine, nothing there yet
        with patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=RuntimeError('kaputt'))):
            await worker.check(self.search)
        await self.found(self.offer)
        self.assertEqual((self.count('outbox'), self.count('start_offers')), (1, 0))

    async def test_first_seen_survives_rechecks(self):
        with patch('time.time', return_value=1000.0):
            await self.found(self.offer)
        with patch('time.time', return_value=5000.0):
            await self.found(self.offer)
        with store.db() as conn:
            self.assertEqual(
                tuple(conn.execute('SELECT first_seen,last_seen FROM offers').fetchone()), (1000.0, 5000.0)
            )

    async def test_pause_stops_schedule_only(self):
        self.assertIsNone(scheduler.plan([self.search], True)[0])
        scheduler.request(self.search)
        self.assertIsNotNone(scheduler.plan([self.search], True)[0])

    async def test_failed_source_preserves_stale_offers_without_mail(self):
        await self.found(self.offer)
        with patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=RuntimeError('Sicherheitsprüfung'))):
            await worker.check(self.search)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM offers').fetchone()[0], 1)
            self.assertIn('Sicherheitsprüfung', conn.execute('SELECT error FROM source_status').fetchone()[0])

    async def test_failed_attempts_in_a_row_are_counted(self):
        def failures():
            with store.db() as conn:
                return conn.execute('SELECT failures FROM source_status').fetchone()[0]

        failing = patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=RuntimeError('kaputt')))
        with failing:
            await worker.check(self.search)
        self.assertEqual(failures(), 1)
        # A provider pause is no new attempt.
        with (
            patch('suchalarm.preis.sources.fetch', new=AsyncMock(side_effect=cadence.Deferred(time.time() + 60))),
            patch('suchalarm.core.cadence.blocked_until', return_value=time.time() + 60),
        ):
            await worker.check(self.search)
        self.assertEqual(failures(), 1)
        with failing:
            await worker.check(self.search)
        self.assertEqual(failures(), 2)
        await self.found(self.offer)
        self.assertEqual(failures(), 0)

    def restart_old_version(self):
        """A restart of a database from before migrations were versioned."""
        with store.db() as conn:
            for schema in ('main', *store.PARTS):
                conn.execute(f'PRAGMA {schema}.user_version=0')
        store.init()

    async def test_migrations_run_once(self):
        with store.db() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], len(store.MIGRATIONS))
        with patch.object(store, 'MIGRATIONS', (Mock(),) * len(store.MIGRATIONS)) as migrations:
            store.init()
        migrations[0].assert_not_called()

    async def test_restart_cleans_saved_ebay_titles_without_changing_history(self):
        await self.found(replace(self.offer, title='Neues AngebotSpielWird in neuem Fenster oder Tab geöffnet'))
        with store.db() as conn:
            before = dict(conn.execute('SELECT * FROM offers').fetchone())
        self.restart_old_version()
        with store.db() as conn:
            after = dict(conn.execute('SELECT * FROM offers').fetchone())
        self.assertEqual(after, dict(before, title='Spiel'))
        self.assertEqual((self.count('outbox'), self.count('notifications')), (1, 0))

    async def test_idealo_reference_moves_into_its_own_table_and_follows_the_search(self):
        with store.db() as conn:
            conn.execute("INSERT INTO settings VALUES ('reference:1','{\"price\":4000}')")
            conn.execute("INSERT INTO settings VALUES ('reference:99','{\"price\":1}')")  # orphan of a deleted search
        self.restart_old_version()
        with store.db() as conn:
            self.assertEqual(
                [tuple(r) for r in conn.execute('SELECT * FROM idealo_references')], [(1, '{"price":4000}')]
            )
            self.assertIsNone(conn.execute("SELECT 1 FROM settings WHERE key LIKE 'reference:%'").fetchone())
            conn.execute('DELETE FROM searches')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM idealo_references').fetchone()[0], 0)

    async def test_kind_tables_move_out_of_the_shared_file(self):
        await self.found(self.offer)
        with store.db() as conn:  # as before the move: the offers in suchalarm.db
            conn.execute('CREATE TABLE main.offers AS SELECT * FROM preis.offers')
            conn.execute('DELETE FROM preis.offers')
            conn.execute('PRAGMA preis.user_version=1')
        store.init()
        with store.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM main.sqlite_master WHERE name='offers'").fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM preis.offers').fetchone()[0], 1)
            conn.execute('DELETE FROM searches')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0], 0)  # follows its search

    async def test_daily_backup(self):
        worker.backup = worker.BackupClock()
        await worker.daily_backup()
        backups = store.db_path().parent / 'backups'
        for name in ('suchalarm', *store.PARTS):
            self.assertEqual(len(list(backups.glob(f'{name}-*.db'))), 1, name)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()
        self.client = TestClient(app)
        worker.progress = worker.Progress()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_new_and_existing_searches_are_product_searches(self):
        self.client.post('/api/searches', json=dict(query='Spiel PS5', max_price=5000, sources=['ebay']))
        with store.db() as conn:
            conn.execute('PRAGMA user_version=0')  # every migration runs again on an existing database
        store.init()
        self.assertEqual([s['kind'] for s in store.searches()], ['preis'])

    def test_manual_check_spam_is_deduplicated_in_server(self):
        payload = dict(query='Spiel PS5', max_price=5000, sources=['ebay', 'vinted'])
        sid = self.client.post('/api/searches', json=payload).json()['id']
        first = self.client.post(f'/api/searches/{sid}/check', json={}).json()
        self.assertTrue(first['started'])
        for _ in range(40):
            result = self.client.post(f'/api/searches/{sid}/check', json={}).json()
            self.assertFalse(result['started'])
            self.assertTrue(result['already_running'])
        with cadence.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM manual_runs').fetchone()[0], 2)
        scheduler.cancel(sid)
        result = self.client.post(f'/api/searches/{sid}/check', json={}).json()
        self.assertFalse(result['queued'])
        self.assertGreater(result['retry_after'], 0)

    def test_retry_lifts_a_provider_pause_and_checks_only_that_source(self):
        payload = dict(query='Spiel PS5', max_price=5000, sources=['ebay', 'vinted'])
        sid = self.client.post('/api/searches', json=payload).json()['id']
        paused = self.client.post('/api/searches', json=dict(payload, active=False)).json()['id']
        cadence.outcome('ebay', 'page', blocked=True)
        cadence.outcome('ebay', 'page', blocked=True)
        self.assertTrue(cadence.blocked_until('ebay'))
        result = self.client.post('/api/sources/ebay/retry', json={}).json()
        self.assertTrue(result['started'])
        self.assertEqual(cadence.blocked_until('ebay'), 0)
        self.assertEqual(scheduler.queued_sources(), [dict(search_id=sid, source='ebay')])
        self.assertNotIn(paused, scheduler.queued_ids())
        # A further refusal still escalates: the strikes stay.
        until = cadence.outcome('ebay', 'page', blocked=True)
        self.assertGreater(until, time.time() + cadence.BACKOFF[1] + 1)
        self.assertEqual(self.client.post('/api/sources/amazon/retry', json={}).status_code, 404)

    def test_source_details_explain_a_failure(self):
        payload = dict(query='Spiel PS5', max_price=5000, sources=['ebay'])
        sid = self.client.post('/api/searches', json=payload).json()['id']

        async def refused(source, search):
            browser.note(
                stage='Seite auslesen',
                url='https://www.ebay.de/sch/i.html?_nkw=Spiel',
                status=200,
                title='Pardon Our Interruption',
                text='Bitte bestätige, dass du ein Mensch bist',
                cards=0,
            )
            raise browser.Blocked('Anbieter verlangt eine Sicherheitsprüfung. Später erneut versuchen.')

        with patch('suchalarm.preis.sources._visit', refused), self.assertRaises(browser.Blocked):
            asyncio.run(sources.fetch('ebay', store.search(sid)))
        data = self.client.get('/api/sources/ebay/details').json()
        self.assertGreater(data['provider']['blocked_until'], time.time())
        self.assertEqual(data['provider']['strikes'], 1)
        entry = data['searches'][0]
        self.assertEqual((entry['search_id'], entry['query']['failures']), (sid, 1))
        self.assertIn('Sicherheitsprüfung', entry['failure']['error'])
        detail = entry['failure']['detail']
        self.assertEqual(
            (detail['error'], detail['status'], detail['title']), ('Blocked', 200, 'Pardon Our Interruption')
        )
        self.assertTrue(entry['url'].startswith('https://www.ebay.de/sch/i.html?'))
        self.assertEqual(self.client.get('/api/sources/amazon/details').status_code, 404)

    def test_pausing_a_search_keeps_its_hits(self):
        payload = dict(query='Spiel PS5', max_price=5000, sources=['ebay', 'vinted', 'idealo'])
        sid = self.client.post('/api/searches', json=payload).json()['id']
        with store.db() as conn:
            for source, url in (('ebay', 'https://www.ebay.de/itm/1'), ('vinted', 'https://www.vinted.de/items/2')):
                conn.execute("INSERT INTO offers VALUES (?,?,?,'Spiel PS5',3000,0,'','',1,1)", (sid, source, url))
                conn.execute(
                    'INSERT INTO source_status(search_id,source,checked_at,error,count) VALUES (?,?,1,NULL,1)',
                    (sid, source),
                )
            conn.execute('INSERT INTO idealo_references VALUES (?,\'{"price":4000}\')', (sid,))

        def kept():
            state = self.client.get('/api/state').json()
            return (
                sorted(o['source'] for o in state['offers']),
                sorted(s['source'] for s in state['statuses']),
                state['searches'][0]['reference'],
            )

        self.assertEqual(self.client.put(f'/api/searches/{sid}', json=dict(payload, active=False)).status_code, 200)
        self.assertEqual(kept(), (['ebay', 'vinted'], ['ebay', 'vinted'], {'price': 4000}))
        # Dropping a source removes only its own hits and status.
        self.client.put(f'/api/searches/{sid}', json=dict(payload, sources=['ebay', 'idealo']))
        self.assertEqual(kept(), (['ebay'], ['ebay'], {'price': 4000}))
        self.client.put(f'/api/searches/{sid}', json=dict(payload, sources=['ebay']))
        self.assertEqual(kept(), (['ebay'], ['ebay'], None))
        # A changed filter needs a fresh result.
        self.client.put(f'/api/searches/{sid}', json=dict(payload, sources=['ebay'], max_price=4000))
        self.assertEqual(kept(), ([], [], None))

    # Helpers

    def create(self, **changes):
        response = self.client.post('/api/searches', json=dict(PAYLOAD, **changes))
        self.assertEqual(response.status_code, 201)
        return response.json()['id']

    def offer(self, sid, url, price, notified=False):
        with store.db() as conn:
            conn.execute(
                "INSERT INTO offers VALUES (?,'ebay',?,'Resident Evil Requiem',?,0,'','',1,1)", (sid, url, price)
            )
            if notified:
                conn.execute('INSERT INTO notifications VALUES (?,?,?,1)', (sid, url, price))

    def state(self):
        return self.client.get('/api/state').json()

    # Searches

    def test_search_crud(self):
        sid = self.create()
        search = self.state()['searches'][0]
        self.assertEqual(search['include_shipping'], 0)
        self.assertNotIn('interval_minutes', search)
        self.assertEqual(
            self.client.put(f'/api/searches/{sid}', json=dict(PAYLOAD, include_shipping=True)).status_code, 200
        )
        self.assertEqual(self.state()['searches'][0]['include_shipping'], 1)
        self.assertEqual(self.client.delete(f'/api/searches/{sid}').status_code, 200)
        self.assertEqual(self.state()['searches'], [])

    def test_search_validation(self):
        self.assertEqual(self.client.post('/api/searches', json=dict(PAYLOAD, sources=[])).status_code, 422)
        self.assertEqual(self.client.post('/api/searches', json=dict(PAYLOAD, min_price=3500)).status_code, 422)
        self.assertEqual(self.client.post('/api/searches', json=dict(PAYLOAD, query=' x ')).status_code, 422)

    # Request guard

    def test_cross_origin_and_malformed_writes_are_refused(self):
        evil = self.client.post('/api/searches', json=PAYLOAD, headers={'Origin': 'https://evil.example'})
        self.assertEqual(evil.status_code, 403)
        bad_length = self.client.post(
            '/api/searches', content=b'{}', headers={'Content-Type': 'application/json', 'Content-Length': 'abc'}
        )
        self.assertEqual(bad_length.status_code, 400)
        chunked = self.client.post(
            '/api/searches', content=b'{}', headers={'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'}
        )
        self.assertEqual(chunked.status_code, 411)
        form = self.client.post('/api/searches', content=b'a=b', headers={'Content-Type': 'text/plain'})
        self.assertEqual(form.status_code, 415)
        self.assertEqual(self.state()['searches'], [])

    # Mail settings

    def test_mail_settings_validate_and_deduplicate_recipients(self):
        on = {'enabled': True}
        injected = {'recipients': ['a@example.com\r\nBcc:x@example.com'], 'preis': on}
        self.assertEqual(self.client.put('/api/mail', json=injected).status_code, 422)
        self.assertEqual(self.client.put('/api/mail', json={'recipients': [], 'wohnung': on}).status_code, 422)
        settings = {
            'recipients': ['a@example.com', 'A@example.com', 'b@example.com'],
            'preis': {'enabled': True, 'frequency': 'Täglich', 'batch_size': 2},
            'wohnung': {'enabled': True, 'frequency': 'Sofort', 'batch_size': 1},
        }
        self.assertEqual(self.client.put('/api/mail', json=settings).status_code, 200)
        mail = self.state()['mail']
        self.assertEqual(mail['recipients'], ['a@example.com', 'b@example.com'])
        self.assertEqual(mail['preis'], {'enabled': True, 'frequency': 'Täglich', 'batch_size': 2})
        self.assertEqual(mail['wohnung'], {'enabled': True, 'frequency': 'Sofort', 'batch_size': 1})
        self.assertEqual(store.mail_settings(kind='preis')['frequency'], 'Täglich')  # the keys from before flats

    def test_mail_server_from_the_settings_page_never_shows_its_password(self):
        with without_smtp_env():
            self.assertFalse(self.state()['mail']['configured'])
            server = {'sender': 'me@gmail.com', 'password': 'secret-app-pw', 'host': 'SMTP.gmail.com', 'port': 587}
            self.assertEqual(self.client.put('/api/mail/server', json=server).status_code, 200)
            self.client.put('/api/mail/server', json={**server, 'password': None})  # unchanged: kept
            state = self.client.get('/api/state')
            self.assertNotIn('secret-app-pw', state.text)
            mail = state.json()['mail']
            self.assertTrue(mail['configured'])
            self.assertEqual(
                mail['server'],
                {
                    'sender': 'me@gmail.com',
                    'host': 'smtp.gmail.com',
                    'port': 587,
                    'password_set': True,
                    'fixed': False,
                },
            )
            current = mailer.server()
            self.assertEqual((current.user, current.password), ('me@gmail.com', 'secret-app-pw'))
            self.assertEqual(current.sender, 'Suchalarm <me@gmail.com>')

    def test_a_new_mail_server_forgets_the_old_password(self):
        """Otherwise the page could send the saved password to a server of its choosing."""
        with without_smtp_env():
            server = {'sender': 'me@gmail.com', 'password': 'secret', 'host': 'smtp.gmail.com', 'port': 587}
            self.client.put('/api/mail/server', json=server)
            for moved in ({'host': 'evil.example.org'}, {'port': 25}, {'sender': 'other@gmail.com'}):
                self.client.put('/api/mail/server', json=server)
                self.client.put('/api/mail/server', json={**server, 'password': None, **moved})
                self.assertFalse(self.state()['mail']['server']['password_set'], moved)

    def test_mail_server_input_is_validated(self):
        with without_smtp_env():
            for bad in (
                {'sender': 'a@example.com\r\nBcc: x@example.com'},
                {'host': 'smtp.example.org:25'},
                {'host': 'smtp example'},
                {'port': 0},
            ):
                self.assertEqual(self.client.put('/api/mail/server', json=bad).status_code, 422, bad)
            # Half filled in is fine while typing, but not ready to send.
            self.assertEqual(self.client.put('/api/mail/server', json={'sender': 'a@example.com'}).status_code, 200)
            self.assertFalse(self.state()['mail']['configured'])

    def test_mail_server_from_compose_wins_and_cannot_be_changed(self):
        env = {'SMTP_HOST': 'smtp.example.org', 'SMTP_FROM': 'Suchalarm <a@example.org>', 'SMTP_PASSWORD': 'pw'}
        with without_smtp_env(), patch.dict(os.environ, env):
            response = self.client.put('/api/mail/server', json={'sender': 'b@example.com', 'host': 'smtp.other.de'})
            self.assertEqual(response.status_code, 409)
            state = self.client.get('/api/state')
            self.assertNotIn('"pw"', state.text)
            self.assertEqual(
                state.json()['mail']['server'],
                {
                    'sender': 'a@example.org',
                    'host': 'smtp.example.org',
                    'port': 587,
                    'password_set': True,
                    'fixed': True,
                },
            )

    def test_global_pause(self):
        self.assertEqual(self.client.put('/api/settings', json={'paused': True}).status_code, 200)
        self.assertTrue(self.state()['paused'])

    # Offers

    def test_junk_below_minimum_is_not_shown_and_start_is_marked(self):
        sid = self.create(min_price=500)
        self.offer(sid, 'https://www.ebay.de/itm/9', 300)
        self.offer(sid, 'https://www.ebay.de/itm/8', 900)
        with store.db() as conn:
            conn.execute("INSERT INTO start_offers VALUES (?,'https://www.ebay.de/itm/8')", (sid,))
        self.assertEqual([(o['price'], o['start']) for o in self.state()['offers']], [(900, True)])

    def test_watch_list_keeps_a_snapshot(self):
        sid = self.create()
        self.offer(sid, 'https://www.ebay.de/itm/8', 900)
        bookmark = {'search_id': sid, 'url': 'https://www.ebay.de/itm/8', 'saved': True}
        self.assertEqual(self.client.post('/api/saved', json=bookmark).status_code, 200)
        missing = dict(bookmark, url='https://www.ebay.de/itm/404')
        self.assertEqual(self.client.post('/api/saved', json=missing).status_code, 404)
        with store.db() as conn:
            conn.execute('DELETE FROM offers')  # the provider dropped it: the watch list keeps the snapshot
        self.assertEqual([(x['url'], x['price']) for x in self.state()['saved']], [('https://www.ebay.de/itm/8', 900)])
        self.assertEqual(self.client.post('/api/saved', json=dict(bookmark, saved=False)).status_code, 200)
        self.assertEqual(self.state()['saved'], [])

    def test_hiding_and_restoring(self):
        sid = self.create()
        self.offer(sid, 'https://www.ebay.de/itm/7', 900)
        swipe = {'search_id': sid, 'url': 'https://www.ebay.de/itm/7', 'hidden': True}
        self.assertEqual(self.client.post('/api/hidden', json=swipe).status_code, 200)
        state = self.state()
        self.assertEqual((state['hidden'], state['offers']), (1, []))
        self.assertEqual(self.client.post('/api/hidden', json=dict(swipe, hidden=False)).status_code, 200)
        self.assertEqual([o['url'] for o in self.state()['offers']], ['https://www.ebay.de/itm/7'])
        self.client.post('/api/hidden', json=swipe)
        self.assertEqual(self.client.delete('/api/hidden').json(), {'restored': 1})
        self.assertEqual(self.client.post('/api/hidden', json=dict(swipe, search_id=999)).status_code, 404)

    def test_clearing_offers_keeps_the_record_of_reported_ones(self):
        sid = self.create()
        self.offer(sid, 'https://www.ebay.de/itm/7', 900, notified=True)
        self.client.post('/api/saved', json={'search_id': sid, 'url': 'https://www.ebay.de/itm/7', 'saved': True})
        self.assertEqual(self.client.delete('/api/offers').json(), {'deleted': 1})
        state = self.state()
        self.assertEqual((state['offers'], state['saved']), ([], []))
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM notifications').fetchone()[0], 1)


class DeliveryOutcomeTests(unittest.TestCase):
    """Whether a failed mail may have arrived decides between a retry and never sending it again."""

    def setUp(self):
        self.env = patch.dict(os.environ, {'SMTP_HOST': 'smtp.example', 'SMTP_FROM': 'a@example.com', 'SMTP_USER': 'u'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def outcome(self, connect=None, login=None, send=None, refused=None):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.login.side_effect = login
        client.send_message.side_effect = send
        client.send_message.return_value = refused or {}
        with patch('smtplib.SMTP', side_effect=connect, return_value=client):
            try:
                refused_now = mailer.deliver(mailer.EmailMessage())
            except mailer.DeliveryError as error:
                return str(error), error.maybe_sent
        return ('sent', refused_now) if refused_now else 'sent'

    def test_success(self):
        self.assertEqual(self.outcome(), 'sent')

    def test_recipients_refused_while_others_got_it(self):
        refused = {'c@example.com': (550, b'no'), 'b@example.com': (450, b'later')}
        self.assertEqual(self.outcome(refused=refused), ('sent', {'b@example.com': 450, 'c@example.com': 550}))

    def test_a_temporary_refusal_does_not_blame_the_address(self):
        text = mailer.refusal({'b@example.com': 450, 'c@example.com': 550})
        self.assertIn('c@example.com nicht angenommen – Adresse prüfen', text)
        self.assertIn('b@example.com vorübergehend abgelehnt', text)
        self.assertNotIn('b@example.com nicht angenommen', text)

    def test_refusals_before_sending_are_retried(self):
        self.assertEqual(self.outcome(connect=ConnectionRefusedError())[1], False)
        self.assertEqual(
            self.outcome(login=smtplib.SMTPAuthenticationError(535, b'no')),
            ('Anmeldung beim Mailserver abgelehnt – Benutzer oder Passwort prüfen.', False),
        )
        self.assertEqual(self.outcome(send=smtplib.SMTPRecipientsRefused({}))[1], False)
        self.assertEqual(self.outcome(send=smtplib.SMTPSenderRefused(550, b'no', 'a'))[1], False)
        self.assertEqual(
            self.outcome(send=smtplib.SMTPDataError(552, b'too big')),
            ('Mailserver lehnt die Nachricht ab (Code 552).', False),
        )

    def test_a_broken_handover_may_have_arrived(self):
        self.assertEqual(self.outcome(send=smtplib.SMTPServerDisconnected()), (mailer.UNREACHABLE, True))
        self.assertEqual(self.outcome(send=TimeoutError())[1], True)
        self.assertEqual(self.outcome(send=ValueError())[1], True)
        self.assertEqual(self.outcome(login=smtplib.SMTPServerDisconnected())[1], False)

    def test_saved_server_logs_in_with_its_address(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with without_smtp_env(), patch.dict(os.environ, {'DATA_DIR': tmp.name}):
            store.init()
            saved = {'sender': 'me@t-online.de', 'password': 'pw', 'host': 'securesmtp.t-online.de', 'port': 465}
            with store.db() as conn:
                store.put_setting(conn, 'mail_server', json.dumps(saved))
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.send_message.return_value = {}
            with patch('smtplib.SMTP_SSL', return_value=client) as connect:
                mailer.deliver(mailer.EmailMessage())
            self.assertEqual(connect.call_args.args, ('securesmtp.t-online.de', 465))
            client.starttls.assert_not_called()
            client.login.assert_called_once_with('me@t-online.de', 'pw')

    def test_missing_configuration(self):
        with patch.dict(os.environ, {'SMTP_SECURITY': 'plain'}):
            self.assertEqual(self.outcome()[1], False)


if __name__ == '__main__':
    unittest.main()
