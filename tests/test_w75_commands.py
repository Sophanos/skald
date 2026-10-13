"""Settings history uses only synthetic household files."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import commands as c, members, migrate, setup


class SettingsHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'SKALD_CONFIG': ''})
        env.start()
        self.addCleanup(env.stop)
        setup.publish(self.root, members.fresh_config(['ana', 'ben']))
        self.s = load_server(self.root)
        self.addCleanup(close_server, self.s)
        net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)

    def change(self, op, values, kind=None, text='synthetic', person=None):
        return c.preview(self.s, text, 'ana', answer=dict(op=op, kind=kind, values=values, person=person))

    def prefs(self, person='ana'):
        return self.s.CONFIG['taste']['people'][person].get('prefs', {})

    def test_history_metadata_retention_and_migration(self):
        legacy = {'before': None, 'shelf': None, 'event': None, 'name': 'Hide anime'}
        (self.root/'commands.json').write_text(json.dumps({'ana': [legacy]}))
        migrate.ensure(self.root)
        self.assertEqual(migrate.FORMAT, 5)
        self.assertIs(migrate.MIGRATIONS[3], migrate.local_row_metrics)
        self.assertIs(migrate.MIGRATIONS[4], migrate.settings_history_v4)
        old = c.journal(self.s)['ana'][0]
        self.assertIsNone(old['t'])
        self.assertEqual(old['name'], legacy['name'])
        original = 'hide anime ' + 'x' * 230
        plan = self.change('hide', ['anime'], text=original)
        with patch.object(self.s.time, 'time', return_value=2_000_000_000):
            c.apply(self.s, plan)
        entry = c.journal(self.s)['ana'][-1]
        self.assertEqual((entry['t'], entry['text'], entry['source']), (2_000_000_000, original[:200], 'search'))
        self.assertEqual(entry['change']['op'], 'hide')
        self.assertEqual(entry['after'], {'hide': ['anime']})
        with patch.object(self.s.time, 'time', return_value=2_000_000_000 + 181*86400):
            c.apply(self.s, self.change('hide', ['movies']))
        self.assertEqual([e['name'] for e in c.journal(self.s)['ana']][0], 'Hide anime')
        self.assertEqual(len(c.journal(self.s)['ana']), 2)
        entries = {'ana': [legacy] + [dict(entry, t=2_000_000_000 + 181*86400) for _ in range(205)], 'ben': [legacy]}
        c.save_journal(self.s, entries)
        with patch.object(self.s.time, 'time', return_value=2_000_000_000 + 181*86400):
            c.apply(self.s, self.change('hide', ['series']))
        self.assertEqual(len(c.journal(self.s)['ana']), 200)
        self.assertEqual(len(c.journal(self.s)['ben']), 1)

    def test_history_is_read_only_scoped_and_has_relative_time(self):
        now = self.s.time.time()
        with patch.object(self.s.time, 'time', return_value=now - 14*86400):
            c.apply(self.s, self.change('exclude_genre', ['Horror'], text='no horror'))
        c.apply(self.s, self.change('hide', ['anime'], person='ben'))
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        for query in ('what did I change', 'history', 'Verlauf'):
            self.assertEqual(c.parse_local(query)['op'], 'history')
            plan = c.preview(self.s, query, 'ana', 'ana')
            self.assertTrue(plan['read_only'])
            self.assertIn('2 weeks ago', plan['description'])
            self.assertIn('Horror', plan['description'])
            self.assertNotIn('anime', plan['description'])
            self.assertEqual(c.apply(self.s, plan), plan)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'history for ben', 'ana', 'ana')
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'history', self.s.GUEST)
        c.apply(self.s, self.change('kids_age', [10], person='ben'))
        self.assertTrue(c.preview(self.s, 'history', 'ben', 'ben')['read_only'])
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'history for ana', 'ben', 'ben')

    def test_cli_and_api_record_the_original_source(self):
        import io
        from skald.__main__ import main
        self.assertEqual(main(['command', '--person', 'ana', 'no horror', '--yes'], out=io.StringIO()), 0)
        self.assertEqual(c.journal(self.s)['ana'][-1]['source'], 'cli')
        req = dict(viewer='ana', client={'name': 'synthetic'}, body={'in': {'query': 'hide horror movies'}, 'no_llm': True})
        result = c.api_preview(self.s, req)
        req['body'] = {'plan': result['preview']['plan']}
        c.api_apply(self.s, req)
        self.assertEqual(c.journal(self.s)['ana'][-1]['source'], 'api')

    def test_targeted_undo_keeps_newer_changes_and_redo_needs_confirm(self):
        c.apply(self.s, self.change('exclude_genre', ['Horror'], text='no horror'))
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        plan = c.preview(self.s, 'undo no horror', 'ana')
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])
        c.apply(self.s, plan)
        self.assertEqual(self.prefs()['hide'], ['anime'])
        self.assertFalse(self.prefs().get('excluded_genres', {}).get('movie'))
        again = c.preview(self.s, 'redo no horror', 'ana')
        self.assertFalse(self.prefs().get('excluded_genres', {}).get('movie'))
        c.apply(self.s, again)
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])
        self.assertEqual(self.prefs()['hide'], ['anime'])
        c.apply(self.s, c.preview(self.s, 'bring back horror', 'ana'))
        self.assertFalse(self.prefs().get('excluded_genres', {}).get('movie'))
        c.apply(self.s, c.preview(self.s, 'do again no horror', 'ana'))
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])

    def test_scalar_undo_restores_only_its_value_or_refuses(self):
        c.apply(self.s, self.change('runtime', [120], 'movie', text='limit movies to 120 minutes'))
        c.apply(self.s, self.change('runtime', [40], 'series', text='limit series to 40 minutes'))
        c.apply(self.s, c.preview(self.s, 'undo 120 minutes', 'ana'))
        self.assertEqual(self.prefs()['runtime'], {'series': 40})
        c.apply(self.s, self.change('discovery', ['gems'], text='find gems'))
        c.apply(self.s, self.change('discovery', ['mainstream'], text='more mainstream'))
        with self.assertRaisesRegex(ValueError, 'changed since'):
            c.preview(self.s, 'undo find gems', 'ana')
        with patch.object(c, 'context', return_value=('adult', 'ana', 'ana')):
            self.assertIn('changed since', c.special(self.s, 'undo find gems', 'movie', routed=True)['metas'][0]['name'])
        self.assertEqual(self.prefs()['discovery'], 'mainstream')

    def test_plain_undo_keeps_lifo_and_can_be_redone(self):
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        c.apply(self.s, self.change('exclude_genre', ['Horror'], text='no horror'))
        c.undo(self.s, ['ana'])
        self.assertEqual(self.prefs(), {'hide': ['anime']})
        c.apply(self.s, c.preview(self.s, 'redo no horror', 'ana'))
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])
        c.undo(self.s, ['ana'])
        c.undo(self.s, ['ana'])
        self.assertEqual(self.prefs(), {})

    def test_targeted_shelf_undo_preserves_later_shelf_and_redoes(self):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about comedy', 'ana'))
        c.apply(self.s, c.preview(self.s, 'undo the crime shelf', 'ana'))
        self.assertEqual([w['text'] for w in shelves.read(self.s)['wishes']], ['comedy'])
        c.apply(self.s, c.preview(self.s, 'redo the crime shelf', 'ana'))
        self.assertEqual({w['text'] for w in shelves.read(self.s)['wishes']}, {'crime', 'comedy'})
        c.undo(self.s, ['ana'])
        self.assertEqual([w['text'] for w in shelves.read(self.s)['wishes']], ['comedy'])

    def test_reset_preview_confirm_single_entry_undo_and_memory_untouched(self):
        from skald import shelves
        c.apply(self.s, self.change('exclude_genre', ['Horror'], text='no horror'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'show 7 shelves', 'ana'))
        preserved = {'memory.json': '{"synthetic": true}\n', 'history.json': '[]\n',
                     'feedback.jsonl': '', 'imports.jsonl': '', 'taste.json': '{}\n'}
        for name, raw in preserved.items():
            (self.root/name).write_text(raw)
        before_prefs, before_wishes = copy.deepcopy(self.prefs()), shelves.read(self.s)['wishes']
        count = len(c.journal(self.s)['ana'])
        with patch.object(c, 'context', return_value=('reset-test', 'ana', 'ana')):
            card = c.special(self.s, 'reset everything to default', 'movie')['metas'][0]
            self.assertIn('Horror', card['description'])
            self.assertIn('Crime', card['description'])
            self.assertEqual(self.prefs(), before_prefs)
            c.special(self.s, 'yes', 'movie')
            self.assertEqual(self.prefs(), before_prefs)
            plan = c.get_pending(self.s, 'reset-test')
            with patch.object(self.s, 'aio_rows_changed') as changed:
                c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')
                changed.assert_called_once()
        self.assertEqual(self.prefs(), {})
        self.assertEqual(shelves.read(self.s)['wishes'], [])
        self.assertEqual(shelves.slot_count(self.s, 'ana'), 4)
        self.assertEqual(len(c.journal(self.s)['ana']), count + 1)
        with patch.object(self.s, 'aio_rows_changed') as changed:
            c.undo(self.s, ['ana'])
            changed.assert_called_once()
        self.assertEqual(self.prefs(), before_prefs)
        self.assertEqual(shelves.read(self.s)['wishes'], before_wishes)
        c.apply(self.s, c.preview(self.s, 'redo reset everything to default', 'ana'))
        c.apply(self.s, self.change('hide', ['anime']))
        c.apply(self.s, c.preview(self.s, 'undo reset everything to default', 'ana'))
        self.assertEqual(self.prefs()['hide'], ['anime'])
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])
        for name, raw in preserved.items():
            self.assertEqual((self.root/name).read_text(), raw)

    def test_reset_filters_shelves_german_and_together_scopes(self):
        from skald import shelves
        c.apply(self.s, self.change('hide', ['anime']))
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'show 6 shelves', 'ana'))
        c.apply(self.s, c.preview(self.s, 'Filter zurücksetzen', 'ana'))
        self.assertEqual(self.prefs(), {'shelves': 6})
        self.assertEqual(len(shelves.read(self.s)['wishes']), 1)
        c.undo(self.s, ['ana'])
        c.apply(self.s, c.preview(self.s, 'Regale zurücksetzen', 'ana'))
        self.assertEqual(self.prefs(), {'hide': ['anime']})
        self.assertEqual(shelves.read(self.s)['wishes'], [])
        c.undo(self.s, ['ana'])
        c.apply(self.s, c.preview(self.s, 'create a shelf about classics for us', 'ana'))
        c.apply(self.s, c.preview(self.s, 'show 5 shelves for us', 'ana'))
        c.apply(self.s, c.preview(self.s, 'alles auf Standard zurücksetzen für zusammen', 'ana'))
        self.assertEqual(self.prefs(), {'shelves': 6})
        self.assertEqual(shelves.slot_count(self.s, 'together'), 2)
        self.assertEqual([w['scope'] for w in shelves.read(self.s)['wishes']], ['ana'])
        self.assertEqual(c.journal(self.s)['together'][-1]['change']['op'], 'reset')
        c.undo(self.s, ['together'], actor='ana')
        self.assertEqual(self.prefs()['hide'], ['anime'])
        self.assertEqual(shelves.slot_count(self.s, 'together'), 5)

    def test_child_can_manage_own_shelf_history_but_cannot_reset_filters(self):
        from skald import shelves
        c.apply(self.s, self.change('kids_age', [10], person='ben'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about cartoons', 'ben', 'ben'))
        req = dict(viewer='ben', client={'name': 'child'}, body={'in': {'query': 'history'}, 'no_llm': True})
        self.assertTrue(c.api_preview(self.s, req)['preview']['read_only'])
        for query in ('reset filters', 'reset everything to default', 'reset shelves for us', 'undo kids age 10'):
            with self.subTest(query=query), self.assertRaises(PermissionError):
                c.preview(self.s, query, 'ben', 'ben')
        req['body'] = {'in': {'query': 'reset shelves'}, 'no_llm': True}
        plan = c.api_preview(self.s, req)['preview']
        req['body'] = {'plan': plan['plan']}
        c.api_apply(self.s, req)
        self.assertEqual(shelves.read(self.s)['wishes'], [])
        self.assertEqual(self.s.kids_age('ben'), 10)
        c.undo(self.s, ['ben'], actor='ben')
        c.apply(self.s, c.preview(self.s, 'undo cartoons', 'ben', 'ben'))
        self.assertEqual(shelves.read(self.s)['wishes'], [])

    def test_new_decision_rules_and_unsupported_taste_reset(self):
        self.assertEqual(self.s.DECISIONS['parse_command']['ver'], 6)
        cases = {'what did I change': ('history', []), 'Verlauf': ('history', []),
                 'undo no horror': ('undo', ['no horror']), 'undo the crime shelf': ('undo', ['the crime shelf']),
                 'bring back horror': ('undo', ['horror']), 'redo no horror': ('redo', ['no horror']),
                 'do again no horror': ('redo', ['no horror']), 'reset filters': ('reset', ['filters']),
                 'reset shelves': ('reset', ['shelves']), 'reset everything to default': ('reset', ['everything']),
                 'alles auf Standard zurücksetzen': ('reset', ['everything']), 'reset my taste': (None, [])}
        for query, expected in cases.items():
            with self.subTest(query=query):
                answer = self.s.decide('parse_command', {'query': query}, allow_llm=False)['answer']
                self.assertEqual((answer['op'], answer['values']), expected)

    def test_inverse_list_and_scalar_settings_do_not_erase_other_values(self):
        c.apply(self.s, self.change('focus', ['de'], 'languages', text='focus German'))
        c.apply(self.s, self.change('focus', ['en'], 'languages', text='focus English'))
        c.apply(self.s, c.preview(self.s, 'undo focus German', 'ana'))
        self.assertEqual(self.prefs()['focus']['languages'], ['en'])
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        c.apply(self.s, self.change('hide', ['movies'], text='hide movies'))
        c.apply(self.s, c.preview(self.s, 'undo hide anime', 'ana'))
        self.assertEqual(self.prefs()['hide'], ['movies'])
        c.apply(self.s, self.change('years', [1980, 1999], text='eighties and nineties'))
        c.apply(self.s, self.change('years', [2000, None], text='this century'))
        with self.assertRaisesRegex(ValueError, 'changed since'):
            c.preview(self.s, 'undo eighties and nineties', 'ana')

    def test_redo_shelf_add_uses_a_current_free_slot(self):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.undo(self.s, ['ana'])
        c.apply(self.s, c.preview(self.s, 'create a shelf about comedy', 'ana'))
        c.apply(self.s, c.preview(self.s, 'redo crime', 'ana'))
        self.assertEqual({w['text'] for w in shelves.read(self.s)['wishes']}, {'crime', 'comedy'})

    def test_legacy_history_still_supports_plain_undo(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'hide': ['anime']}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        (self.root/'commands.json').write_text(json.dumps({'ana': [dict(before=None, shelf=None, event=None, name='Hide anime')]}))
        plan = c.preview(self.s, 'undo', 'ana')
        c.apply(self.s, plan)
        self.assertEqual(self.prefs(), {})
        self.assertTrue(c.journal(self.s)['ana'][0]['undone'])
        self.assertIn('date unknown', c.preview(self.s, 'history', 'ana')['description'])

    def test_confirmation_rechecks_actor_and_scalar_values(self):
        c.apply(self.s, self.change('kids_age', [10], person='ben'))
        plan = c.preview(self.s, 'undo kids age 10 for ben', 'ana')
        c.apply(self.s, plan)
        self.assertIsNone(self.s.kids_age('ben'))
        c.apply(self.s, self.change('discovery', ['gems'], text='find gems'))
        undo = c.preview(self.s, 'undo find gems', 'ana')
        cfg = c.config(self.s)
        cfg['taste']['people']['ana']['prefs']['discovery'] = 'mainstream'
        setup.publish(self.root, cfg, replace=True)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            c.apply(self.s, undo)
        reset = c.preview(self.s, 'reset everything to default', 'ana')
        cfg['taste']['people']['ana']['prefs']['kids_age'] = 9
        setup.publish(self.root, cfg, replace=True)
        with self.assertRaises(PermissionError):
            c.apply(self.s, reset)

    def test_history_search_and_child_reset_confirmation(self):
        c.apply(self.s, self.change('kids_age', [10], person='ben'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about cartoons', 'ben', 'ben'))
        with patch.object(c, 'context', return_value=('kid', 'ben', 'ben')):
            self.assertIn('Cartoons', c.special(self.s, 'Verlauf', 'movie')['metas'][0]['description'])
            c.special(self.s, 'reset shelves', 'movie')
            plan = c.get_pending(self.s, 'kid')
            self.assertIn('Done', c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')['metas'][0]['name'])

    def test_cli_history_keeps_legacy_files_read_only(self):
        import io
        from skald.__main__ import main
        (self.root/'.skald-format').write_text(json.dumps({'format': 2, 'release': 'synthetic'}))
        path = self.root/'commands.json'
        path.write_text(json.dumps({'ana': [dict(before=None, shelf=None, event=None, name='Old synthetic change')]}))
        before = {name: (self.root/name).read_bytes() for name in ('skald.json', 'commands.json', '.skald-format')}
        out = io.StringIO()
        self.assertEqual(main(['command', '--person', 'ana', 'history'], out=out), 0)
        self.assertIn('Old synthetic change', out.getvalue())
        self.assertEqual(before, {name: (self.root/name).read_bytes() for name in before})

    def test_bound_links_cannot_reset_or_undo_other_adults_filters(self):
        c.apply(self.s, self.change('hide', ['anime'], person='ben'))
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'reset filters for us', 'ana', 'ana')
        c.apply(self.s, c.preview(self.s, 'reset filters for us', 'ana'))
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'undo reset filters for us', 'ana', 'ana')
        self.assertEqual(self.prefs('ben'), {})

    def test_unbound_api_can_confirm_a_named_reset(self):
        req = dict(viewer=None, client={'name': 'adult'}, body={'in': {'query': 'reset filters'}, 'person': 'ana', 'no_llm': True})
        plan = c.api_preview(self.s, req)['preview']
        req['body'] = {'plan': plan['plan']}
        self.assertIn('Done', c.api_apply(self.s, req)['name'])

    def test_redo_stop_refuses_to_stop_a_different_shelf_in_the_slot(self):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'stop Crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about comedy', 'ana'))
        with self.assertRaisesRegex(ValueError, 'Shelf changed'):
            c.preview(self.s, 'redo stop crime', 'ana')
        self.assertEqual([w['text'] for w in shelves.read(self.s)['wishes']], ['comedy'])

    def test_duplicate_matches_pick_newest_and_apply_the_list_inverse(self):
        now = self.s.time.time()
        with patch.object(self.s.time, 'time', return_value=now):
            for _ in range(3):
                c.apply(self.s, c.preview(self.s, 'no horror', 'ana'))
        c.apply(self.s, c.preview(self.s, 'undo no horror', 'ana'))
        entries = c.journal(self.s)['ana']
        self.assertTrue(entries[-1].get('undone'))
        self.assertFalse(entries[-2].get('undone'))
        self.assertFalse(self.prefs().get('excluded_genres', {}).get('movie'))

    def test_bound_search_can_target_a_together_shelf(self):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime for us', 'ana', 'ana'))
        with patch.object(c, 'context', return_value=('member', 'ana', 'ana')):
            c.special(self.s, 'undo crime for us', 'movie')
            plan = c.get_pending(self.s, 'member')
            self.assertIn('Undone', c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')['metas'][0]['name'])
        self.assertEqual(shelves.read(self.s)['wishes'], [])

    def test_redo_replays_other_existing_command_changes(self):
        c.apply(self.s, self.change('row_rename', ['foryou', 'Crime Picks'], text='rename For You to Crime Picks'))
        c.undo(self.s, ['ana'])
        plan = c.preview(self.s, 'redo Crime Picks', 'ana')
        self.assertNotIn('row_labels', c.config(self.s)['household'])
        c.apply(self.s, plan)
        self.assertEqual(c.config(self.s)['household']['row_labels']['foryou'], 'Crime Picks')
        with patch.object(self.s, 'resolve_pick', return_value=({'id': 7, 'title': 'Arrival', 'release_date': '2016-01-01'}, {})):
            c.apply(self.s, self.change('seen', ['Arrival'], 'movie', text='I already watched Arrival'))
            c.undo(self.s, ['ana'])
            again = c.preview(self.s, 'redo Arrival', 'ana')
            self.assertFalse(self.s.active_feedback(self.s.read_feedback()))
            c.apply(self.s, again)
        self.assertEqual(next(iter(self.s.active_feedback(self.s.read_feedback()).values()))['action'], 'seen')

    def test_suggested_shelf_history_keeps_the_original_search_query(self):
        now = self.s.time.time()
        records = [{'p': 'ana', 'q': 'cozy', 't': now-i*7*86400} for i in range(3)]
        (self.root/'searches.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        with patch.object(c, 'context', return_value=('ideas', 'ana', 'ana')):
            card = c.special(self.s, ' SuGgEsT ', 'movie')['metas'][0]
            code = card['description'].split('yes ')[1][:2]
            c.special(self.s, 'yes '+code, 'movie')
        self.assertEqual(c.journal(self.s)['ana'][-1]['text'], ' SuGgEsT ')

    def concurrent_reset(self, start_writer):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        plan = c.preview(self.s, 'reset shelves', 'ana')
        saving, release = threading.Event(), threading.Event()
        errors = []
        save = shelves.save

        def paused_save(s, doc):
            if threading.current_thread() is reset:
                saving.set()
                if not release.wait(5):
                    raise AssertionError('reset was not released')
            save(s, doc)

        def confirm():
            try:
                c.apply(self.s, plan)
            except Exception as error:
                errors.append(error)

        reset = threading.Thread(target=confirm)
        with patch.object(shelves, 'save', side_effect=paused_save):
            reset.start()
            try:
                self.assertTrue(saving.wait(5))
                finish_writer = start_writer()
            finally:
                release.set()
                reset.join(5)
            finish_writer()
        self.assertFalse(reset.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([(w['scope'], w['name']) for w in shelves.read(self.s)['wishes']], [('ben', 'Comedy')])

    def test_reset_preserves_a_concurrent_shelf_api_write(self):
        def start_writer():
            done = threading.Event()
            errors = []

            def add():
                try:
                    self.s.shelf_change('ben', 'add', text='comedy', name='Comedy', apply=True)
                except Exception as error:
                    errors.append(error)
                finally:
                    done.set()

            writer = threading.Thread(target=add)
            writer.start()
            done.wait(1)

            def finish():
                writer.join(5)
                self.assertFalse(writer.is_alive())
                self.assertEqual(errors, [])
            return finish

        self.concurrent_reset(start_writer)

    def test_reset_preserves_a_concurrent_shelf_write_from_another_process(self):
        def start_writer():
            script = ('import sys\nfrom decide_kit import load_server, close_server\n'
                      's = load_server(sys.argv[1])\nprint("ready", flush=True)\n'
                      'try:\n s.shelf_change("ben", "add", text="comedy", name="Comedy", apply=True)\n'
                      'finally:\n close_server(s)\n')
            writer = subprocess.Popen([sys.executable, '-c', script, self.temp.name],
                                      env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent)},
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(writer.stdout.close)
            self.addCleanup(writer.stderr.close)
            self.addCleanup(lambda: writer.kill() if writer.poll() is None else None)
            self.assertEqual(writer.stdout.readline().strip(), 'ready')
            try:
                writer.wait(1)
            except subprocess.TimeoutExpired:
                pass

            def finish():
                stdout, stderr = writer.communicate(timeout=5)
                self.assertEqual(writer.returncode, 0, stdout+stderr)
            return finish

        self.concurrent_reset(start_writer)

    def test_plain_shelf_undo_preserves_targeted_preference_undo(self):
        from skald import shelves
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, c.preview(self.s, 'undo hide anime', 'ana'))
        before = copy.deepcopy(self.prefs())
        c.undo(self.s, ['ana'])
        self.assertEqual(self.prefs(), before)
        self.assertEqual(shelves.read(self.s)['wishes'], [])

    def test_plain_shelves_reset_undo_preserves_targeted_preference_undo(self):
        from skald import shelves
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        c.apply(self.s, c.preview(self.s, 'reset shelves', 'ana'))
        c.apply(self.s, c.preview(self.s, 'undo hide anime', 'ana'))
        before = copy.deepcopy(self.prefs())
        c.undo(self.s, ['ana'])
        self.assertEqual(self.prefs(), before)
        self.assertEqual([w['name'] for w in shelves.read(self.s)['wishes']], ['Crime'])

    def test_new_shelf_undo_keeps_legacy_undo_in_sync(self):
        from skald import shelves
        self.s.shelf_change('ana', 'add', text='crime', name='Crime', apply=True)
        legacy = dict(before=None, shelf=c.shelf_state(self.s, 'ana'), event=None, name='Add Crime')
        c.save_journal(self.s, {'ana': [legacy]})
        c.apply(self.s, c.preview(self.s, 'create a shelf about comedy', 'ana'))
        c.undo(self.s, ['ana'])
        self.assertEqual([w['name'] for w in shelves.read(self.s)['wishes']], ['Crime'])
        c.undo(self.s, ['ana'])
        self.assertEqual(shelves.read(self.s)['wishes'], [])
        self.assertTrue(all(e.get('undone') for e in c.journal(self.s)['ana']))

    def test_cli_reset_preview_prints_what_will_be_cleared(self):
        import io
        from skald.__main__ import main
        c.apply(self.s, self.change('hide', ['anime'], text='hide anime'))
        c.apply(self.s, c.preview(self.s, 'create a shelf about crime', 'ana'))
        before = (self.root/'skald.json').read_bytes(), (self.root/'shelves.json').read_bytes()
        out = io.StringIO()
        self.assertEqual(main(['command', '--person', 'ana', 'reset everything to default'], out=out), 0)
        self.assertIn('Plan:', out.getvalue())
        self.assertIn('Clear settings:', out.getvalue())
        self.assertIn('anime', out.getvalue())
        self.assertIn('Clear shelf: Crime', out.getvalue())
        self.assertEqual(before, ((self.root/'skald.json').read_bytes(), (self.root/'shelves.json').read_bytes()))
