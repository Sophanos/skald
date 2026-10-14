"""Search commands use synthetic households only."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from decide_kit import load_server, close_server
from skald import members, setup
from skald.__main__ import main
class Commands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'SKALD_CONFIG': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        setup.publish(Path(self.temp.name), members.fresh_config(['ana', 'sam']))
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        self.c = __import__('skald.commands', fromlist=['preview'])
        self.change = {'op': 'hide', 'kind': None, 'values': ['anime'], 'person': None}
        self.parse = patch.object(self.s, 'decide', return_value={'answer': self.change})
        self.parse.start()
        self.addCleanup(self.parse.stop)
    def preview(self, person='ana', bound=None):
        return self.c.preview(self.s, 'hide anime', person, bound)
    def test_route_command_vs_title_guard(self):
        self.assertIn('command', self.s.ROUTE_Q['route']['criteria'])
        for title in ('No Country for Old Men', 'Never Have I Ever', 'Yes Man'):
            self.s._last_search.clear()
            with patch.object(self.s, 'search_both', return_value=([{'id': 1, 'title': title}], {})), patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'to_meta', return_value={'id': 'tmdb:1', 'type': 'movie', 'name': title}), patch.object(self.s, 'OR_KEY', 'stub'), patch.object(self.s, 'looks_like_title', return_value=True):
                self.assertEqual(self.s.search('movie', title, 'en-US')['metas'][0]['id'], 'tmdb:1')
    def test_command_preview_saves_nothing(self):
        before = {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir() if p.is_file()}
        plan = self.preview()
        self.assertEqual(plan['plan'], self.preview()['plan'])
        from concurrent.futures import Future
        future = Future()
        future.set_result({'choice': 'command', 'picks': None})
        with patch.object(self.s, 'OR_KEY', 'stub'), patch.object(self.s, 'looks_like_title', return_value=False), patch.object(self.s, 'superseded', return_value=False), patch.object(self.s, 'ai_future', return_value=future), patch.object(self.s, 'search_both', return_value=([], {})), patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.assertIn('Nothing changes', self.s.search('movie', 'hide anime', 'en-US')['metas'][0]['description'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.temp.name).iterdir() if p.is_file()})
    def test_yes_applies_and_undo_reverts(self):
        plan = self.preview()
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.c.pending(self.s)[None] = (self.s.time.time(), plan)
            self.assertIn('Done:', self.s.search('movie', 'yes '+self.c.confirm_code(plan), 'en-US')['metas'][0]['name'])
        self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['hide'], ['anime'])
        self.c.undo(self.s, ['ana'])
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])

    def test_member_link_changes_only_that_member(self):
        self.change['person'] = 'sam'
        self.assertEqual(self.preview(bound='ana')['people'], ['ana'])

    def test_shared_link_needs_a_person(self):
        with self.assertRaisesRegex(ValueError, 'name'):
            self.preview(None)
        self.change['person'] = 'everyone'
        self.assertEqual(self.preview(None)['people'], ['ana', 'sam'])

    def test_child_cannot_command(self):
        self.s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        with self.assertRaises(PermissionError):
            self.preview(bound='ana')

    def test_like_command_writes_taste_event(self):
        self.change.update(op='like', kind='tv', values=['Dark'])
        with patch.object(self.c, 'resolve', return_value={'key': 'tmdb:1', 'kind': 'tv', 'title': 'Dark', 'year': '2017'}):
            self.c.apply(self.s, self.preview())
        self.assertEqual(self.s.read_feedback()[0]['action'], 'love')
        self.c.undo(self.s, ['ana'])
        self.assertFalse(self.s.active_feedback(self.s.read_feedback()))

    def test_cli_command_plan_hash(self):
        with patch('skald.commands.preview', side_effect=lambda s, text, person, bound=None: {'plan': 'abc', 'name': 'Hide anime?', 'people': [person]}):
            out = io.StringIO()
            self.assertEqual(main(['command', '--person', 'ana', 'hide anime'], out=out), 0)
            self.assertIn('abc', out.getvalue())

    def test_api_command_bound_token_own_person(self):
        plan = self.preview('sam')
        self.c.pending(self.s)['api:token'] = (self.s.time.time(), plan)
        req = {'viewer': 'ana', 'client': {'name': 'token'}, 'body': {'plan': plan['plan']}}
        with self.assertRaises(PermissionError):
            self.c.api_apply(self.s, req)

    def test_one_off_wish_never_saved(self):
        self.change.update(op=None, values=[])
        self.assertIsNone(self.preview())

    def test_pending_expires(self):
        self.c.pending(self.s)['x'] = (0, self.preview())
        self.assertIsNone(self.c.get_pending(self.s, 'x'))

    def test_help_card(self):
        self.assertIn('hide anime', self.c.special(self.s, 'help', 'movie')['metas'][0]['description'])

    def test_about_me_card_reads_only(self):
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.assertIn('ana', self.c.special(self.s, 'my settings', 'movie')['metas'][0]['name'].lower())
        self.assertFalse((Path(self.temp.name) / 'commands.json').exists())

    def test_confirmation_requires_plan_code(self):
        plan = self.preview()
        self.c.pending(self.s)[None] = (self.s.time.time(), plan)
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            for query in ('y', 'ye', 'yes', 'yes ', 'yes 0', 'yes 99', 'yes man'):
                self.c.special(self.s, query, 'movie')
                self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])
            code = self.c.confirm_code(plan)
            self.assertIn('yes '+code, self.c.special(self.s, 'yes', 'movie')['metas'][0]['description'])
            self.assertIn('Done:', self.c.special(self.s, ' YES '+code+' ', 'movie')['metas'][0]['name'])
        other = {**plan, 'plan': 'ffffffffffffffff'}
        self.assertNotEqual(code, self.c.confirm_code(other))

    def test_duplicate_confirm_apply_and_undo(self):
        first = self.preview()
        self.c.apply(self.s, first)
        self.change['values'] = ['movies']
        plan = self.preview()
        self.c.pending(self.s)[None] = (self.s.time.time(), plan)
        self.c.apply(self.s, plan)
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            a = self.c.special(self.s, 'undo', 'movie')
            b = self.c.special(self.s, ' UNDO ', 'series')
            self.assertTrue(a['metas'])
            self.assertEqual(b, {'metas': []})
            self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['hide'], ['anime'])
            plan = self.preview()
            self.c.pending(self.s)[None] = (self.s.time.time(), plan)
            query = 'yes '+self.c.confirm_code(plan)
            a = self.c.special(self.s, query, 'movie')
            b = self.c.special(self.s, query.upper(), 'series')
            self.assertTrue(a['metas'])
            self.assertEqual(b, {'metas': []})
        self.assertEqual(sum(not e.get('undone') for e in self.c.journal(self.s)['ana']), 2)
        req = {'viewer': 'ana', 'client': {'name': 'synthetic'}, 'body': {'plan': self.preview()['plan']}}
        self.c.pending(self.s)['api:synthetic'] = (self.s.time.time(), self.preview())
        self.assertEqual(self.c.api_apply(self.s, req), self.c.api_apply(self.s, req))
        req['body'] = {'undo': True}
        self.assertEqual(self.c.api_apply(self.s, req), self.c.api_apply(self.s, req))
        self.assertEqual(sum(not e.get('undone') for e in self.c.journal(self.s)['ana']), 2)
        later = self.s.time.time() + 61
        with patch.object(self.s.time, 'time', return_value=later):
            self.c.api_apply(self.s, req)
        self.assertEqual(sum(not e.get('undone') for e in self.c.journal(self.s)['ana']), 1)

    def test_shared_confirmation_uses_stored_targets(self):
        for named in ('ana', 'everyone'):
            with self.subTest(named=named), patch.object(self.s, 'who_watching', return_value=('both', None)):
                self.change['person'] = named
                plan = self.preview(None)
                self.c.pending(self.s)[named] = (self.s.time.time(), plan)
                with patch.object(self.c, 'context', return_value=(named, 'both', 'guest')):
                    self.assertIn('Ask a grown-up', self.c.special(self.s, 'yes '+self.c.confirm_code(plan), 'movie')['metas'][0]['name'])
                with patch.object(self.c, 'context', return_value=(named, 'both', 'both')):
                    self.assertIn('Done:', self.c.special(self.s, 'yes '+self.c.confirm_code(plan), 'movie')['metas'][0]['name'])

    def test_api_preview_refreshes_child_restriction(self):
        cfg = self.c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        setup.publish(Path(self.temp.name), cfg, replace=True)
        req = {'viewer': 'ana', 'client': {'name': 'synthetic'}, 'body': {'in': {'query': 'hide anime'}}}
        with self.assertRaises(PermissionError):
            self.c.api_preview(self.s, req)
        self.s.decide.assert_called_once()
        (name, inp), kwargs = self.s.decide.call_args
        self.assertEqual((name, inp['query'], kwargs), ('parse_command', 'hide anime', {'allow_llm': True}))
        self.assertNotIn('api:synthetic', self.c.pending(self.s))

    def test_undo_consumes_superseded_taste_event(self):
        self.change.update(op='like', kind='tv', values=['Dark'])
        item = {'key': 'tmdb:1', 'kind': 'tv', 'title': 'Dark', 'year': '2017'}
        with patch.object(self.c, 'resolve', return_value=item):
            self.c.apply(self.s, self.preview())
        newer = self.s.write_feedback({**item, 'who': 'ana', 'action': 'meh'})
        result = self.c.undo(self.s, ['ana'])
        self.assertIn('could not be undone', result['name'])
        self.assertTrue(all(e.get('undone') for e in self.c.journal(self.s)['ana']))
        self.assertEqual(self.s.active_feedback(self.s.read_feedback())[('tmdb:1', 'ana')]['id'], newer['id'])
