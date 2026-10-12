"""Bounded W60 regressions using temporary synthetic households."""
import datetime as dt
import unittest
from unittest.mock import patch
import test_w54_commands as base
from decide_kit import fixture_rows

class Fixes(unittest.TestCase):
    setUp = base.Commands.setUp

    def child(self):
        from skald import setup
        from pathlib import Path
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        setup.publish(Path(self.temp.name), cfg, replace=True)
        self.s.refresh_preferences()

    def rows(self, when=None):
        return [{'person': 'ana', 'id': theme, 'text': theme, 'name': theme.title(),
                 'when': when or {'days': [6], 'from_hour': 8, 'to_hour': 9}}
                for theme in ('cozy', 'crime')]

    def test_stop_requires_existing_exact_shelf(self):
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            for q in ('Stop M', 'Stop Making', 'Stop Making Sense', 'stop absent'):
                self.assertIsNone(self.c.special(self.s, q, 'movie'))
            self.s.shelf_change('ana', 'add', text='classic', name='Classics', apply=True)
            for q in ('stop CLASSICS', 'stop the classics shelf'):
                self.assertIn('Stop Classics', self.c.special(self.s, q, 'movie')['metas'][0]['name'])
            self.assertIsNone(self.c.special(self.s, 'stop classics for sam', 'movie'))

    def test_full_member_label_sets_scope(self):
        with patch.dict(self.s.LABELS, {'sam': 'Mary Ann', 'ana': 'Ann'}):
            plan = self.c.preview(self.s, 'weekday evenings something light for MARY ANN', 'ana')
            self.assertEqual(plan['people'], ['sam'])
            self.assertEqual(plan['shelf']['text'], 'something light')

    def test_child_paired_undo_runs_once(self):
        self.child()
        for theme in ('heist', 'cozy'):
            self.c.apply(self.s, self.c.preview(self.s, 'make me a row of '+theme, 'ana', 'ana'))
        with patch.object(self.c, 'context', return_value=('child', 'ana', 'ana')):
            self.c.special(self.s, 'undo', 'movie')
            self.c.special(self.s, ' UNDO ', 'series')
        self.assertEqual(len(self.s.shelf_list('ana')['wishes']), 1)

    def test_api_undo_uses_last_together_scope(self):
        plan = self.c.preview(self.s, 'every sunday morning a classic for us', 'ana')
        self.c.pending(self.s)['api:adult'] = (self.s.time.time(), plan)
        req = {'viewer': 'ana', 'client': {'name': 'adult'}, 'body': {'plan': plan['plan']}}
        self.c.api_apply(self.s, req)
        req['body'] = {'undo': True}
        self.c.api_apply(self.s, req)
        self.assertFalse(self.s.shelf_list('together')['wishes'])
        self.child()
        self.s._commands_last['api:adult'] = ['together']
        with self.assertRaises(PermissionError):
            self.c.api_apply(self.s, req)

    def test_dismiss_keeps_other_suggestion_code(self):
        with patch('skald.engine.suggestions', return_value={'suggestions': self.rows()}) as suggestions, \
                patch.object(self.s, 'who_watching', return_value=('ana', None)):
            cards = self.c.special(self.s, 'ideas', 'movie')['metas']
            codes = [c['description'].split('yes ')[1][:2] for c in cards]
            self.assertEqual(len(codes), 2)
            self.c.special(self.s, 'no '+codes[0], 'movie')
            self.assertIn('Done:', self.c.special(self.s, 'yes '+codes[1], 'movie')['metas'][0]['name'])
            self.assertEqual(self.s.shelf_list('ana')['wishes'][0]['text'], 'crime')

    def test_suggestion_splits_logical_day_boundary(self):
        row = self.rows({'days': [6], 'from_hour': 4, 'to_hour': 5})[:1]
        with patch('skald.engine.suggestions', return_value={'suggestions': row}), \
                patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.c.special(self.s, 'ideas', 'movie')
        plans = self.s._command_suggestions[None].values()
        self.assertEqual({(tuple(p['shelf']['when']['days']), tuple(p['shelf']['when']['hours'])) for p in plans},
                         {(('sat',), (28, 29)), (('sun',), (5, 6))})

    def test_existing_shelf_covers_both_midnight_portions(self):
        from skald.suggest import suggest
        now = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc).timestamp()
        existing = [{'scope': 'ana', 'text': 'cozy', 'when': {'days': ['sat'], 'hours': [22, 26]}}]
        for day, hour in ((10, 23), (11, 1)):
            at = dt.datetime(2026, 10, day, hour, tzinfo=dt.timezone.utc).timestamp()
            searches = [{'p': 'ana', 'q': 'cozy', 't': at-i*7*86400} for i in range(3)]
            self.assertEqual(suggest(searches, [], 'UTC', existing, [], now), [])

    def test_registered_decisions_use_model_for_endpoint(self):
        for name, spec in self.s.DECISIONS.items():
            llm = spec['llm']
            if not llm:
                continue
            with self.subTest(name=name):
                body = llm['prompt']([fixture_rows(self.s, name)[0]['in']])
                if llm['path'] == '/api/alpha/decisions':
                    self.assertEqual(llm['model'], self.s.ROUTE_MODEL)
                    self.assertEqual(body['model'], self.s.ROUTE_MODEL)
                elif llm['path'] == '/api/v1/chat/completions':
                    self.assertNotEqual(llm['model'], self.s.ROUTE_MODEL)
                    self.assertNotEqual(body['model'], self.s.ROUTE_MODEL)
        spec = self.s.DECISIONS['parse_command']
        body = spec['llm']['prompt']([{'query': 'hide anime'}])
        self.assertEqual(body['reasoning'], {'effort': 'none'})
        self.assertLessEqual(body['max_tokens'], 200)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertGreater(spec['ver'], 2)

    def test_command_cards_only_in_movie_results(self):
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            for q in ('help', 'my settings', 'ideas', 'make me a row of heist films'):
                self.assertTrue(self.c.special(self.s, q, 'movie')['metas'])
                self.assertEqual(self.c.special(self.s, q, 'series'), {'metas': []})
            plan = self.c.get_pending(self.s, None)
            q = 'yes '+self.c.confirm_code(plan)
            self.assertEqual(self.c.special(self.s, q, 'series'), {'metas': []})
            self.assertIn('Done:', self.c.special(self.s, q, 'movie')['metas'][0]['name'])
            self.assertEqual(len(self.s.shelf_list('ana')['wishes']), 1)
            self.assertEqual(self.c.special(self.s, 'undo', 'series'), {'metas': []})
            self.assertIn('Undone', self.c.special(self.s, 'undo', 'movie')['metas'][0]['name'])

    def test_settings_accepts_member_phrase_forms(self):
        with patch.dict(self.s.LABELS, {'sam': 'Mary Ann'}), \
                patch.object(self.c, 'context', return_value=(None, 'both', 'both')):
            for name in ('sam', 'MARY ANN'):
                for q in ('my settings for '+name, name+' my settings', name+' settings',
                          name+"'s settings", 'settings '+name, 'settings for '+name, 'my settings '+name):
                    with self.subTest(q=q):
                        result = self.c.special(self.s, q, 'movie')
                        self.assertIsNotNone(result)
                        self.assertEqual(result['metas'][0]['name'], 'Settings for Mary Ann')
            for q in ('settings Nobody', 'Nobody settings'):
                self.assertEqual(self.c.special(self.s, q, 'movie')['metas'][0]['name'], 'Add a member name to this command')
