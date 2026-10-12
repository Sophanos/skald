"""Readable command cards, using synthetic households and in-memory HTTP."""
import json
import unittest
from unittest.mock import patch

import test_w54_commands as base
from test_api import raw_request


class Odin(unittest.TestCase):
    setUp = base.Commands.setUp

    def request(self, tail, scope=None):
        path = '/synthetic-capability/'+self.s.LANG+'/' + ('p/'+scope+'/' if scope else '') + tail
        with patch.object(self.s, 'addon_token', return_value='synthetic-capability'), \
                patch.object(self.s, 'log_search'), \
                patch.object(self.s, 'who_watching', return_value=('ana', None)):
            status, headers, raw, _ = raw_request(self.s, path, handler=self.s.Handler)
        return status, headers, json.loads(raw)

    def test_codes_first_in_preview_and_suggestions(self):
        plan = self.c.preview(self.s, 'hide anime', 'ana')
        code = self.c.confirm_code(plan)
        card = self.c.preview_card(self.s, plan, 'movie')['metas'][0]
        self.assertEqual(card['name'], 'yes '+code+': Hide anime for Ana')
        self.assertIn('yes '+code, card['description'])
        rows = [{'person': 'ana', 'id': 'cozy', 'text': 'cozy', 'name': 'Cozy',
                 'when': {'days': [6], 'from_hour': 8, 'to_hour': 9}}]
        with patch('skald.engine.suggestions', return_value={'suggestions': rows}):
            cards = self.c.suggestion_cards(self.s, None, 'ana', None, 'movie')['metas']
        self.assertTrue(cards)
        for card in cards:
            self.assertRegex(card['name'], r'^yes \d{2}: ')
            self.assertIn(card['name'][:6], card['description'])
            self.assertIn(': Add shelf: Cozy for Ana', card['name'])

    def test_friendly_plan_names_and_labels(self):
        with patch.dict(self.s.LABELS, {'ana': 'Alex'}):
            cases = [('exclude_genre', 'movie', ['horror'], 'No more Horror'),
                     ('include_genre', 'movie', ['horror'], 'More Horror'),
                     ('hide', None, ['anime'], 'Hide anime'),
                     ('show', None, ['anime'], 'Show anime'),
                     ('focus', 'languages', ['ko'], 'More Korean'),
                     ('unfocus', 'languages', ['ko'], 'Less Korean'),
                     ('focus', 'countries', ['de'], 'More Germany'),
                     ('unfocus', 'countries', ['DE'], 'Less Germany'),
                     ('focus', 'movie', ['horror'], 'More Horror'),
                     ('discovery', None, ['gems'], 'Discovery: gems'),
                     ('years', None, [1990, 2000], 'Years 1990 to 2000'),
                     ('like', 'tv', ['dark'], 'Like Dark'),
                     ('dislike', 'tv', ['dark'], 'Dislike Dark')]
            with patch.object(self.c, 'resolve', return_value={'key': 'tmdb:1', 'kind': 'tv', 'title': 'Dark', 'year': '2017'}):
                for op, kind, values, name in cases:
                    with self.subTest(op=op, kind=kind):
                        answer = dict(op=op, kind=kind, values=values, person=None)
                        plan = self.c.preview(self.s, '', 'ana', answer=answer)
                        self.assertEqual(plan['name'], name+' for Alex')
            with patch.object(self.s, 'who_watching', return_value=('ana', None)):
                card = self.c.special(self.s, 'my settings', 'movie')['metas'][0]
                self.assertEqual(card['name'], 'Settings for Alex')
                self.assertIn('"Alex"', card['description'])
            self.c.apply(self.s, self.c.preview(self.s, 'hide anime', 'ana'))
            self.assertEqual(self.c.undo(self.s, ['ana'])['name'], 'Undone for Alex')
        for article in ('a', 'an', 'the', 'some'):
            plan = self.c.preview(self.s, 'every sunday morning '+article+' classic for us', 'ana')
            self.assertEqual(plan['shelf']['name'], 'Sunday Classic')
            self.assertTrue(plan['name'].startswith('Add Sunday shelf: Classic for Together'))

    def test_scoped_command_meta_and_manifest(self):
        with patch.object(self.s, 'shelf_rows', return_value=[]):
            manifest = self.s.manifest(self.s.LANG)
        self.assertIn('catalog', manifest['resources'])
        self.assertEqual(manifest['idPrefixes'], ['tmdb:'])
        self.assertIn({'name': 'meta', 'types': ['movie', 'series', 'tv'],
                       'idPrefixes': [self.s.rid('cmd:')]}, manifest['resources'])
        for scope in (None, 'ana', 'sam'):
            status, _, result = self.request('catalog/movie/'+self.s.rid('search')+'/search=my%20settings.json', scope)
            self.assertEqual(status, 200)
            card = result['metas'][0]
            for typ in ('movie', 'series', 'tv'):
                status, headers, result = self.request('meta/'+typ+'/'+card['id']+'.json', scope)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(result['meta'], {**card, 'type': typ})
            other = 'sam' if scope != 'sam' else 'ana'
            self.assertEqual(self.request('meta/movie/'+card['id']+'.json', other)[2]['meta']['name'], 'Skald command')
        status, _, _ = self.request('meta/movie/tmdb:1.json')
        self.assertEqual(status, 404)

    def test_command_meta_latest_expiry_and_bound(self):
        card = {'id': self.s.rid('cmd:same'), 'type': 'movie', 'name': 'First', 'description': 'One'}
        with patch.object(self.s, 'search', return_value={'metas': [card]}):
            self.request('catalog/movie/'+self.s.rid('search')+'/search=fixture.json')
            card['description'] = 'Latest'
            self.request('catalog/movie/'+self.s.rid('search')+'/search=fixture.json')
        tail = 'meta/movie/'+card['id']+'.json'
        self.assertEqual(self.request(tail)[2]['meta']['description'], 'Latest')
        later = self.s.time.time()+1800
        with patch.object(self.s.time, 'time', return_value=later):
            self.assertEqual(self.request(tail)[2]['meta']['description'], 'Search again to see this card.')
        cards = [{**card, 'id': self.s.rid('cmd:'+str(i))} for i in range(201)]
        with patch.object(self.s, 'search', return_value={'metas': cards}):
            self.request('catalog/movie/'+self.s.rid('search')+'/search=fixture.json')
        self.assertEqual(self.request('meta/movie/'+cards[0]['id']+'.json')[2]['meta']['name'], 'Skald command')
        self.assertEqual(self.request('meta/movie/'+cards[-1]['id']+'.json')[2]['meta']['name'], 'First')

    def test_help_examples_movie_only_once(self):
        phrases = ['hide anime', 'no more horror', 'every sunday morning a classic for us',
                   'make me a row of heist films', 'my settings']
        with patch.object(self.s, 'who_watching', return_value=('ana', None)), \
                patch.object(self.c, '_special', wraps=self.c._special) as action:
            result = self.c.special(self.s, 'help', 'movie')
            self.assertEqual(result['metas'][0]['name'], 'Skald help')
            self.assertEqual([m['name'] for m in result['metas'][1:]], ['Try: '+p for p in phrases])
            for meta, phrase in zip(result['metas'][1:], phrases):
                self.assertIn('Type "'+phrase+'" in search.', meta['description'])
            self.assertEqual(self.c.special(self.s, 'HELP', 'series'), {'metas': []})
            self.assertEqual(action.call_count, 1)
        with patch.object(self.c, 'context', return_value=('guest', 'guest', 'guest')):
            self.assertEqual(self.c.special(self.s, 'help', 'movie')['metas'][0]['name'], 'Skald help')

    def test_together_shelf_phrases(self):
        for phrase in ('us', 'both', 'both of us', 'everyone', 'together'):
            for text in ('every sunday morning a classic', 'make me a row of heist films'):
                with self.subTest(phrase=phrase, text=text):
                    plan = self.c.preview(self.s, text+' for '+phrase, 'ana', 'ana')
                    self.assertEqual(plan['people'], ['together'])
                    self.assertNotIn(' for '+phrase, plan['shelf']['text'])

    def test_together_phrases_override_member_labels(self):
        phrases = ('us', 'both', 'both of us', 'everyone', 'together')
        for label in ('Us', 'Both', 'Both of Us', 'Everyone', 'Together'):
            with patch.dict(self.s.LABELS, {'ana': label}):
                for phrase in (*phrases, 'ana'):
                    with self.subTest(label=label, phrase=phrase):
                        plan = self.c.preview(self.s, 'make me a row of heist films for '+phrase, 'ana', 'ana')
                        self.assertEqual(plan['people'], ['ana' if phrase == 'ana' else 'together'])
