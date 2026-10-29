import json
import os
import tempfile
import unittest
from unittest.mock import patch

from suchalarm import store
from suchalarm.preis import learning
from suchalarm.preis.domain import CATEGORIES, Offer, learned_words, select_offers, taught_away, title_fits

GAME = 'Resident Evil Requiem'
POSTERS = [f'Affiche encadrée {GAME} {n}' for n in range(3)]
GAMES = [f'{GAME} PS5', 'Résident evil requiem', f'Prix Ferme {GAME}']


class LearnedWordsTests(unittest.TestCase):
    def test_words_of_hidden_titles_beyond_the_search_are_learned(self):
        hidden = [(title, f'{GAME} PS5') for title in POSTERS]
        self.assertEqual(learned_words(hidden, GAMES), ['affiche', 'encadree'])

    def test_one_hidden_title_teaches_nothing(self):
        self.assertEqual(learned_words([(POSTERS[0], GAME)], GAMES), [])

    def test_a_word_kept_as_often_as_hidden_is_no_sign(self):
        hidden = [(f'{GAME} Steelbook', GAME), (f'{GAME} Steelbook', GAME)]
        kept = [f'{GAME} Steelbook Edition', f'{GAME} Steelbook PS5']
        self.assertEqual(learned_words(hidden, kept), [])

    def test_a_search_never_excludes_its_own_words(self):
        search = dict(query='Affiche Resident Evil', excluded='', learned=['affiche', 'encadree'])
        self.assertFalse(taught_away('Affiche Resident Evil Village', search))
        self.assertTrue(taught_away('Affiche encadrée Resident Evil', search))

    def test_learned_words_filter_each_search_after_the_shared_result(self):
        offers = [
            Offer('vinted', f'https://www.vinted.de/items/{n}', t, 4000, None) for n, t in enumerate(POSTERS + GAMES)
        ]
        search = dict(query=GAME, excluded='', max_price=4300, filters={'platform': 'ps5'}, learned=['affiche'])
        self.assertEqual(sorted(o.title for o in select_offers(offers, search)), sorted(GAMES))
        self.assertEqual(len(select_offers(offers, dict(search, learned=[]))), 6)

    def test_games_exclude_decoration_by_default(self):
        excluded = CATEGORIES['videospiele']['excluded']
        search = dict(query=GAME, excluded=excluded, filters={'platform': 'ps5'})
        for junk in ('Tapis de souris XXL', 'Affiche encadrée', 'Targa legno', 'Poster'):
            self.assertFalse(title_fits('vinted', f'{junk} {GAME}', search), junk)
        self.assertTrue(all(title_fits('vinted', title, search) for title in GAMES))


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        store.init()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def search(self, query, category):
        with store.db() as conn:
            return conn.execute(
                'INSERT INTO searches(query,max_price,sources,category) VALUES (?,5000,?,?)',
                (query, json.dumps(['vinted']), category),
            ).lastrowid

    def offers(self, sid, titles, hidden=False):
        with store.db() as conn:
            for n, title in enumerate(titles):
                url = f'https://www.vinted.de/items/{sid}{n}{int(hidden)}'
                conn.execute("INSERT INTO offers VALUES (?,'vinted',?,?,4000,NULL,'','',1,1)", (sid, url, title))
                if hidden:
                    store.hide(conn, sid, url, 1)

    def test_hiding_teaches_the_whole_category_and_survives_the_offer(self):
        games, other = self.search(GAME, 'videospiele'), self.search('Lampe', 'elektronik')
        self.offers(games, POSTERS, hidden=True)
        self.offers(games, GAMES)
        search = {'category': 'videospiele'}
        self.assertEqual(learning.learned(search), ['affiche', 'encadree'])
        with store.db() as conn:
            conn.execute('DELETE FROM offers WHERE search_id=?', (games,))
        self.assertEqual(learning.learned(search), ['affiche', 'encadree'])  # the offers are gone, the lesson stays
        self.assertEqual(learning.learned({'category': 'elektronik', 'id': other}), [])

    def test_restoring_a_hidden_offer_takes_its_lesson_back(self):
        games = self.search(GAME, 'videospiele')
        self.offers(games, POSTERS, hidden=True)
        self.assertEqual(learning.learned({'category': 'videospiele'}), ['affiche', 'encadree'])
        with store.db() as conn:
            store.unhide(conn)
        self.assertEqual(learning.learned({'category': 'videospiele'}), [])
