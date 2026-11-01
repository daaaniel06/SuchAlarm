import os
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from suchalarm import mailer, maillinks, store, worker
from suchalarm.app import app

URL = 'https://www.ebay.de/itm/1?x=1'
OFFER = {'url': URL, 'title': 'iPhone 13', 'price': 30000, 'shipping': 0, 'source': 'ebay', 'seller': 'anna',
         'condition': 'Gebraucht'}  # fmt: skip


class MailLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name, 'APP_URL': 'https://alarm.example/'})
        self.env.start()
        store.init()
        self.client = TestClient(app)
        worker.progress = worker.Progress()
        payload = dict(query='iPhone 13', max_price=40000, sources=['ebay'])
        self.sid = self.client.post('/api/searches', json=payload).json()['id']

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def path(self, link):
        return link.removeprefix('https://alarm.example')

    def test_a_forged_or_broken_link_does_nothing(self):
        token = maillinks.token('hide', s=self.sid, u=URL)
        body, signature = token.split('.')
        forged = maillinks.token('pause', s=self.sid).split('.')[0] + '.' + signature
        for bad in (forged, body, 'x.y', ''):
            self.assertIsNone(maillinks.read(bad), bad)
        self.assertEqual(self.client.post(f'/m/{forged}').status_code, 404)
        self.assertEqual(self.client.get('/m/kaputt').status_code, 404)

    def test_opening_asks_and_only_the_button_hides_with_undo(self):
        path = self.path(maillinks.link('hide', s=self.sid, u=URL))
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Ausblenden?', page.text)
        with store.db() as conn:
            self.assertEqual(store.hidden(conn), set())  # a scanner opening the link changes nothing
        done = self.client.post(path, headers={'origin': 'null'})  # what a form sends under no-referrer
        self.assertIn('Ausgeblendet.', done.text)
        with store.db() as conn:
            self.assertEqual(store.hidden(conn), {(self.sid, URL)})
        undo = re.search(r'action="([^"]+)"', done.text).group(1)
        self.assertIn('Wieder eingeblendet.', self.client.post(f'/m/{undo}').text)
        with store.db() as conn:
            self.assertEqual(store.hidden(conn), set())

    def test_pause_stops_the_search(self):
        self.client.post(self.path(maillinks.link('pause', s=self.sid)))
        self.assertEqual(store.search(self.sid)['active'], 0)
        self.client.post(self.path(maillinks.link('resume', s=self.sid)))
        self.assertEqual(store.search(self.sid)['active'], 1)

    def test_one_click_unsubscribe_turns_the_kinds_mails_off(self):
        with store.db() as conn:
            store.put_setting(conn, 'mail_enabled', '1')
        response = self.client.post(
            self.path(maillinks.link('mute', k='preis')),
            content='List-Unsubscribe=One-Click',
            headers={'content-type': 'application/x-www-form-urlencoded'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(store.mail_settings()['enabled'])

    def test_save_puts_the_hit_on_the_watch_list(self):
        with store.db() as conn:
            conn.execute("INSERT INTO offers VALUES (?,'ebay',?,'iPhone 13',30000,0,'','',1,1)", (self.sid, URL))
        self.assertIn('Gemerkt.', self.client.post(self.path(maillinks.link('save', s=self.sid, u=URL))).text)
        with store.db() as conn:
            self.assertTrue(store.is_saved(conn, URL))

    def test_nothing_to_undo_when_it_was_already_so(self):
        """A hit saved in the app stays saved: no Rückgängig that would take it off the watch list."""
        with store.db() as conn:
            conn.execute("INSERT INTO offers VALUES (?,'ebay',?,'iPhone 13',30000,0,'','',1,1)", (self.sid, URL))
        self.client.post('/api/saved', json={'search_id': self.sid, 'url': URL, 'saved': True})
        page = self.client.post(self.path(maillinks.link('save', s=self.sid, u=URL))).text
        self.assertIn('das war schon so', page)
        self.assertNotIn('Rückgängig', page)
        with store.db() as conn:
            self.assertTrue(store.is_saved(conn, URL))

    def test_a_gone_hit_says_so(self):
        response = self.client.post(self.path(maillinks.link('save', s=self.sid, u=URL)))
        self.assertEqual(response.status_code, 410)
        self.client.delete(f'/api/searches/{self.sid}')
        self.assertEqual(self.client.get(self.path(maillinks.link('pause', s=self.sid))).status_code, 410)

    def test_the_mail_carries_the_links_and_unsubscribe_headers(self):
        search = store.search(self.sid)
        message = mailer.compose_digest([(search, dict(OFFER))], ['a@example.com'], 'b1')
        self.assertTrue(message['List-Unsubscribe'].startswith('<https://alarm.example/m/'))
        self.assertEqual(message['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')
        html = message.get_body(('html',)).get_content()
        for part in ('>Merken<', '>Ausblenden<', '>pausieren<', '>Mails abbestellen<', 'Angebot öffnen ↗'):
            self.assertIn(part, html)
        self.assertIn('Merken: https://alarm.example/m/', message.get_body(('plain',)).get_content())

    def test_without_app_url_the_mail_has_no_links(self):
        with patch.dict(os.environ, {'APP_URL': ''}):
            message = mailer.compose_digest([(store.search(self.sid), dict(OFFER))], ['a@example.com'], 'b1')
        self.assertIsNone(message['List-Unsubscribe'])
        self.assertNotIn('/m/', message.get_body(('html',)).get_content())

    def test_a_flat_links_to_its_sheet(self):
        flat = {'id': 7, 'kind': 'wohnung', 'query': 'Darmstadt', 'max_price': 130000}
        links = maillinks.offer_links(flat, {'url': 'https://a.example/1?x=1'})
        self.assertEqual(links['detail'], 'https://alarm.example/#treffer=7:https%3A%2F%2Fa.example%2F1%3Fx%3D1')


if __name__ == '__main__':
    unittest.main()
