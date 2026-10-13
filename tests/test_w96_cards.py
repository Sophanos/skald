"""Readable command tiles, with temporary synthetic households only."""
import hashlib
import unittest
from unittest.mock import patch

import test_w54_commands as base


class Cards(unittest.TestCase):
    setUp = base.Commands.setUp

    def say(self, query, viewer='both'):
        with patch.object(self.c, 'context', return_value=(None, None, viewer)):
            return self.c.special(self.s, query, 'movie')

    def test_unlinked_about_explains_how_to_name_member(self):
        meta = self.say('what do you know about me')['metas'][0]
        self.assertEqual(meta['name'], 'Who is asking?')
        self.assertIn('Add a member name', meta['description'])
        self.assertIn('what do you know about Ana', meta['description'])
        self.assertIn("Who's watching", meta['description'])

    def test_long_preview_keeps_code_and_full_plan_first(self):
        plan = self.c.preview(self.s, 'hide anime', 'ana')
        plan['name'] = 'Hide documentaries and horror movies for Ana'
        meta = self.c.preview_card(self.s, plan, 'movie')['metas'][0]
        self.assertLessEqual(len(meta['name']), 24)
        self.assertTrue(meta['name'].startswith('yes '+self.c.confirm_code(plan)+': '))
        self.assertEqual(meta['description'].splitlines()[0], plan['name'])
        self.assertIn('Nothing changes until then.', meta['description'])

    def test_long_names_hash_original_and_keep_full_text(self):
        names = ['A very long command result for Ana', 'A very long command result for Sam']
        metas = [self.c.card(self.s, n, 'movie', 'Details.')['metas'][0] for n in names]
        self.assertNotEqual(metas[0]['id'], metas[1]['id'])
        self.assertEqual(metas[0]['name'], metas[1]['name'])
        for name, meta in zip(names, metas):
            self.assertLessEqual(len(meta['name']), 24)
            self.assertEqual(meta['description'], name+'\nDetails.')
            self.assertEqual(meta['id'], self.s.rid('cmd:'+hashlib.sha256(name.encode()).hexdigest()[:12]))

    def test_name_limit_and_whole_words(self):
        for name, expected in [('x'*24, 'x'*24), ('x'*25, '...'),
                               ('One two three four five six', 'One two three four...')]:
            with self.subTest(name=name):
                meta = self.c.card(self.s, name, 'movie')['metas'][0]
                self.assertEqual(meta['name'], expected)
                self.assertLessEqual(len(meta['name']), 24)

    def test_named_about_languages_and_unknown_names(self):
        for query in ('what do you know about Ana', 'was weißt du über Anas',
                      'šta znaš o Anu', 'sta znas o Ana', 'what do you knwo about Ana'):
            with self.subTest(query=query), patch.object(self.s, 'get_model', return_value=None):
                meta = self.say(query)['metas'][0]
                self.assertEqual(meta['name'], 'About Ana')
                self.assertIn('Taste profile is still being built.', meta['description'])
                self.assertNotIn('Top taste:', meta['description'])
                self.assertNotIn('{', meta['description'])
                self.assertNotIn('}', meta['description'])
                self.assertEqual(self.c.parse_local(query, self.s)['person'], 'ana')
        for query in ('what do you know about Dune', 'was weißt du über Niemand', 'šta znaš o Dune'):
            with self.subTest(query=query):
                self.assertIsNone(self.say(query))
                self.assertIsNone(self.c.parse_local(query, self.s)['op'])

    def test_about_plain_preferences_shelves_taste_and_recent_commands(self):
        from pathlib import Path
        from skald import setup
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {
            'hide': ['anime'], 'excluded_genres': {'movie': ['Horror']},
            'focus': {'languages': ['de'], 'countries': ['DE'], 'genres': {'movie': ['Comedy']}},
            'weights': {'genre': {'Thriller': 1, 'Reality': -1}, 'series': -1},
            'runtime': {'movie': 120}, 'years': [1990, 2020], 'discovery': 'gems'}
        setup.publish(Path(self.temp.name), cfg, replace=True)
        self.s.shelf_change('ana', 'add', text='gentle mysteries', name='Mysteries', apply=True)
        self.c.save_journal(self.s, {'ana': [{'name': 'Change '+str(i)} for i in range(4)]})
        model = {'paff': {'ana': {str(i): 10-i for i in range(6)}},
                 'names': {str(i): 'Taste '+str(i) for i in range(6)}}
        with patch.object(self.s, 'get_model', return_value=model):
            meta = self.say('what do you know about Ana')['metas'][0]
        for text in ('anime', 'Horror', 'German', 'Germany', 'Comedy', 'Thriller', 'Reality',
                     'series', '120 minutes', '1990', '2020', 'gems', 'Mysteries', 'gentle mysteries',
                     'Taste 0', 'Taste 4', 'Change 1', 'Change 3'):
            self.assertIn(text, meta['description'])
        for text in ('{', '}', 'Taste 5', 'Change 0'):
            self.assertNotIn(text, meta['description'])

    def test_settings_use_same_about_card(self):
        with patch.object(self.s, 'get_model', return_value=None):
            about = self.say('what do you know about Ana')
            self.assertEqual(self.say('my settings', 'ana'), about)
            self.assertEqual(self.say('settings for Ana'), about)

    def test_settings_keep_shelf_counts_and_household_rows(self):
        from pathlib import Path
        from skald import setup
        self.c.apply(self.s, self.c.preview(self.s, 'show 6 shelves', 'ana'))
        self.c.apply(self.s, self.c.preview(self.s, 'hide For You', 'ana'))
        cfg = self.c.config(self.s)
        cfg['household']['row_labels'] = {'because': 'Mystery Picks'}
        cfg['household']['row_order'] = ['week', 'because']
        setup.publish(Path(self.temp.name), cfg, replace=True)
        with patch.object(self.s, 'get_model', return_value=None):
            detail = self.say('my settings', 'ana')['metas'][0]['description']
        self.assertIn('Shows 6 shelves for Ana.', detail)
        self.assertIn('Household rows: '+', '.join(cfg['household']['rows'])+'.', detail)
        self.assertNotIn('foryou', next(line for line in detail.splitlines() if line.startswith('Household rows:')))
        self.assertIn('Household row labels: because is Mystery Picks.', detail)
        self.assertIn('Household row order: week, because.', detail)
        self.assertNotIn('{', detail)
        self.assertNotIn('}', detail)

    def test_unknown_member_settings_example_works(self):
        import re
        for query in ('settings Nobody', 'settings for Nobody', 'Nobody settings', 'my settings Nobody'):
            with self.subTest(query=query), patch.object(self.s, 'get_model', return_value=None):
                meta = self.say(query)['metas'][0]
                self.assertEqual(meta['name'], 'Who is asking?')
                example = re.search(r'for example "([^"]+)"', meta['description'])[1]
                self.assertEqual(example, 'settings for Ana')
                self.assertEqual(self.say(example)['metas'][0]['name'], 'About Ana')

    def test_about_shows_own_and_shared_wishes_and_schedule(self):
        import json
        from pathlib import Path
        self.s.shelf_change('ana', 'add', text='quiet films', name='Sunday',
                            when={'days': ['sun'], 'hours': [8, 10]}, apply=True)
        wishes = [{'who': 'ana', 'text': 'gentle mysteries'},
                  {'who': 'together', 'text': 'family adventures'},
                  {'who': 'sam', 'text': 'other member wish'}]
        Path(self.temp.name, 'wishes.jsonl').write_text('\n'.join(json.dumps(w) for w in wishes)+'\n')
        with patch.object(self.s, 'get_model', return_value={}):
            detail = self.say('what do you know about Ana')['metas'][0]['description']
        for text in ('quiet films', 'sun from 8 to 10', 'gentle mysteries', 'family adventures'):
            self.assertIn(text, detail)
        for text in ('other member wish', '{', '}', 'Recent commands:', 'Top taste:'):
            self.assertNotIn(text, detail)

    def test_long_member_label_keeps_full_about_title(self):
        label = 'Synthetic Member With A Long Name'
        with patch.dict(self.s.LABELS, {'ana': label}), patch.object(self.s, 'get_model', return_value=None):
            meta = self.say('what do you know about '+label)['metas'][0]
        self.assertLessEqual(len(meta['name']), 24)
        self.assertEqual(meta['description'].splitlines()[0], 'About '+label)

    def test_missing_member_undo_keeps_guidance(self):
        meta = self.say('undo')['metas'][0]
        self.assertEqual(meta['name'], 'Who is asking?')
        self.assertIn('undo for Ana', meta['description'])
        self.assertIn("Who's watching", meta['description'])

    def test_permission_error_keeps_full_message(self):
        meta = self.say('my settings', self.s.GUEST)['metas'][0]
        self.assertEqual(meta['name'], 'Ask a grown-up')
        self.assertIn('Ask a grown-up to change settings', meta['description'])

    def test_error_message_kept_even_when_routed(self):
        message = 'Until when? Say tonight or a date like 2026-12-24'
        with patch.object(self.c, 'context', return_value=(None, None, 'ana')), \
                patch.object(self.c, 'preview', side_effect=ValueError(message)):
            meta = self.c.special(self.s, 'not tonight for Dune', 'movie', routed=True)['metas'][0]
        self.assertLessEqual(len(meta['name']), 24)
        self.assertIn(message, meta['description'])
