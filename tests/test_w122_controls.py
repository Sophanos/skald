"""Household controls use synthetic people and offline transports."""
import copy
import io
import json
import os
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald import commands as c, setup, shelves
from skald.__main__ import main
from eval import commands_eval
from test_w116_settings import SettingsFixture


class Controls(SettingsFixture):
    def setUp(self):
        super().setUp()
        cfg = c.config(self.s)
        cfg['household']['members'] += ['kim', 'oskar']
        cfg['household']['labels'].update(kim='Kim', oskar='Oskar')
        cfg['household']['guests'] = ['oskar']
        cfg['household']['ages'] = {'kim': 9}
        cfg['taste']['people'].update(kim={'prefs': {'kids_age': 9}}, oskar={})
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data)}))

    def plan(self, op, values=(), actor='ana', person=None, **extra):
        return c.preview(self.s, 'synthetic control', actor, answer=dict(
            op=op, kind=None, values=list(values), person=person, **extra))

    def change(self, op, values=(), actor='ana', person=None):
        c.apply(self.s, self.plan(op, values, actor, person))
        return c.changes(self.s, actor)['changes'][0]

    def cli(self, *args):
        out = io.StringIO()
        code = main([*args, '--json'], out=out)
        return code, json.loads(out.getvalue())

    def test_budget_preview_set_read_live_and_undo(self):
        before = self.stored()
        plan = self.plan('ai_budget', [5.5], currency='EUR')
        self.assertIn('USD', plan['name'])
        self.assertIn('5.5', plan['name'])
        self.assertEqual(self.stored(), before)
        c.apply(self.s, plan)
        self.assertEqual(self.s.ai_monthly_usd(), 5.5)
        self.assertEqual(self.s.ai_status()['budget'], {
            'household': 5.5, 'ceiling': 25, 'ceiling_source': 'default'})
        self.assertEqual(self.cli('ai', 'budget')[1]['household'], 5.5)
        other = load_server(self.data)
        self.addCleanup(close_server, other)
        c.apply(other, c.preview(other, '', 'ana', answer=dict(op='ai_budget', values=[0])))
        self.assertEqual(self.s.ai_monthly_usd(), 0)
        c.undo(other, ['ana'], actor='ana')
        self.assertEqual(self.s.ai_monthly_usd(), 5.5)
        c.undo(self.s, ['ana'], actor='ana')
        self.assertEqual(self.s.ai_monthly_usd(), 3)

    def test_env_is_ceiling_and_default_is_clipped(self):
        with patch.dict(os.environ, {'AI_MONTHLY_USD': '2'}):
            s = load_server(self.data)
        self.addCleanup(close_server, s)
        self.assertEqual(s.ai_monthly_usd(), 2)
        self.assertEqual(s.ai_status()['budget']['ceiling_source'], 'env')
        before = self.stored()
        with self.assertRaisesRegex(ValueError, 'ceiling.*2.*USD'):
            c.preview(s, '', 'ana', answer=dict(op='ai_budget', values=[3]))
        self.assertEqual(self.stored(), before)
        for value in (-1, True, float('inf'), '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plan('ai_budget', [value])

    def test_unsaved_budget_preserves_existing_environment_cap(self):
        with patch.dict(os.environ, {'AI_MONTHLY_USD': '10'}):
            s = load_server(self.data)
        self.addCleanup(close_server, s)
        self.assertEqual(s.ai_monthly_usd(), 10)
        self.assertIsNone(s.ai_budget()['household'])
        c.apply(s, c.preview(s, '', 'ana', answer=dict(op='ai_budget', values=[5])))
        self.assertEqual(s.ai_monthly_usd(), 5)
        c.undo(s, ['ana'], actor='ana')
        self.assertEqual(s.ai_monthly_usd(), 10)

    def test_budget_roles_and_stale_plan_write_nothing(self):
        for actor in ('kim', 'oskar', 'guest'):
            before = self.stored()
            with self.subTest(actor=actor), self.assertRaisesRegex(PermissionError, 'Ask a household member'):
                self.plan('ai_budget', [0], actor)
            self.assertEqual(self.stored(), before)
        before = self.stored()
        with self.assertRaisesRegex(PermissionError, 'Ask a household member'):
            c.preview(self.s, '', 'ana', 'guest', answer=dict(op='ai_budget', values=[0]))
        self.assertEqual(self.stored(), before)
        plan = self.plan('ai_budget', [2], 'ben')
        cfg = c.config(self.s)
        cfg['household']['guests'].append('ben')
        setup.publish(self.data, cfg, replace=True)
        before = self.stored()
        with self.assertRaises(PermissionError):
            c.apply(self.s, plan)
        self.assertEqual(self.stored(), before)

    def test_zero_blocks_transport_and_keeps_fallbacks(self):
        self.change('ai_budget', [0])
        with patch.object(self.s, 'OR_KEY', 'synthetic'):
            result = self.s.decide('parse_command', {'query': 'synthetic unrecognized control'})
            self.assertEqual((result['source'], result['why']), ('fallback', 'month'))
            with self.assertRaises(self.s.BudgetExceeded):
                self.s._openrouter('/unused', {}, 1, 'search', 'synthetic')

    def test_saved_config_passes_cc016e7_budget_rules(self):
        before = c.config(self.s)
        self.change('ai_budget', [5])
        saved = json.loads((self.data / 'skald.json').read_text())
        # cc016e7 merges household keys without a whitelist; ai has this closed rule.
        merged = copy.deepcopy(before)
        merged['household'].update(saved['household'])
        self.assertEqual(merged, saved)
        ai = saved.get('ai', {})
        self.assertIsInstance(ai, dict)
        self.assertLessEqual(set(ai), {'credit_warn_fraction', 'credit_warn_usd'})
        for field, default, maximum in (('credit_warn_fraction', .2, 1), ('credit_warn_usd', 1, float('inf'))):
            value = ai.get(field, default)
            self.assertTrue(type(value) in (int, float) and 0 <= value <= maximum and value < float('inf'))
        saved['household'].pop('ai_monthly_usd')
        self.assertEqual(saved, before)

    def test_actors_and_viewer_filtering_since_and_old_entries(self):
        for actor in ('ana', 'kim', 'oskar'):
            self.change('shelf_count', [2], actor)
        all_rows = c.changes(self.s)['changes']
        self.assertEqual({r['by'] for r in all_rows}, {'ana', 'kim', 'oskar'})
        self.assertEqual(c.changes(self.s, 'ben')['changes'], all_rows)
        for actor in ('kim', 'oskar'):
            rows = c.changes(self.s, actor)['changes']
            self.assertEqual([r['scope'] for r in rows], [actor])
        entries = c.journal(self.s)
        entries['ana'][0]['t'] -= 8 * 86400
        entries['ana'][0].pop('by')
        c.save_journal(self.s, entries)
        self.assertEqual(len(c.changes(self.s, 'ana', '7d')['changes']), 2)
        self.assertEqual(c.changes(self.s)['changes'][-1]['by'], 'unknown')
        plan = self.plan('history', since='7d', person='household')
        self.assertTrue(plan['read_only'])
        self.assertEqual(plan['changes'], c.changes(self.s, 'ana', '7d')['changes'])

    def test_adult_targeted_guest_undo_and_kid_refusal(self):
        guest = self.change('shelf_count', [2], 'oskar')
        adult = self.change('ai_budget', [5])
        before = self.stored()
        plan = self.plan('undo', [guest['id']])
        self.assertEqual(self.stored(), before)
        c.apply(self.s, plan)
        self.assertEqual(shelves.slot_count(self.s, 'oskar'), 4)
        before = self.stored()
        for actor in ('kim', 'oskar'):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.plan('undo', [adult['id']], actor)
            self.assertEqual(self.stored(), before)

    def test_out_of_order_undo_and_redo_refuse_without_writes(self):
        old = self.change('ai_budget', [5])
        latest = self.change('ai_budget', [7], 'ben')
        before = self.stored()
        with self.assertRaisesRegex(ValueError, 'later change first'):
            self.plan('undo', [old['id']])
        self.assertEqual(self.stored(), before)
        c.apply(self.s, self.plan('undo', [latest['id']]))
        c.apply(self.s, self.plan('undo', [old['id']]))
        self.change('ai_budget', [9], 'ben')
        before = self.stored()
        with self.assertRaisesRegex(ValueError, 'later change first'):
            self.plan('redo', [old['id']])
        self.assertEqual(self.stored(), before)

    def test_independent_targeted_row_undo_by_name_and_id(self):
        first = self.change('row_rename', ['foryou', 'Invented picks'])
        self.change('shelf_count', [2], 'oskar')
        plan = self.plan('undo', ['row rename'], 'ben')
        self.assertEqual(plan['target']['id'], first['id'])
        c.apply(self.s, plan)
        self.assertNotIn('foryou', c.config(self.s)['household'].get('row_labels', {}))

    def test_targeted_compound_row_undo_keeps_later_runtime(self):
        c.apply(self.s, self.plan('plan', steps=[
            dict(op='row_rename', kind=None, values=['foryou', 'Invented picks'], person=None),
            dict(op='hide', kind=None, values=['anime'], person=None)]))
        first = c.changes(self.s)['changes'][0]
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='runtime', kind='movie', values=[90])))
        c.apply(self.s, self.plan('undo', [first['id']]))
        cfg = c.config(self.s)
        self.assertEqual(cfg['taste']['people']['ana']['prefs'], {'runtime': {'movie': 90}})
        self.assertNotIn('foryou', cfg['household'].get('row_labels', {}))

    def test_targeted_undo_prefers_exact_shelf_name_across_scopes(self):
        first = self.change('shelf_add', ['Comedy', 'always'])
        self.change('shelf_add', ['Dark Comedy', 'always'], 'ben')
        before = self.stored()
        plan = self.plan('undo', ['comedy'])
        self.assertEqual(self.stored(), before)
        self.assertEqual(plan['target']['id'], first['id'])
        c.apply(self.s, plan)
        self.assertEqual([w['name'] for w in shelves.read(self.s)['wishes']], ['Dark Comedy'])

    def test_targeted_undo_asks_when_matches_are_equally_good(self):
        for actor, name in (('ana', 'Comedy'), ('ben', 'Mystery')):
            first = self.change('shelf_add', [name, 'always'])
            second = self.change('shelf_add', [name, 'always'], actor)
            for query in (name.lower(), 'always'):
                with self.subTest(actor=actor, query=query):
                    before = self.stored()
                    with self.assertRaisesRegex(ValueError, 'Which change') as error:
                        self.plan('undo', [query])
                    self.assertIn(first['id'], str(error.exception))
                    self.assertIn(second['id'], str(error.exception))
                    self.assertEqual(self.stored(), before)

    def test_ids_survive_reload_and_pruning(self):
        first = self.change('shelf_count', [2])
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        self.assertEqual(c.changes(s)['changes'][0]['id'], first['id'])
        entries = c.journal(s)
        entries['ana'][0]['t'] -= 181 * 86400
        c.save_journal(s, entries)
        self.assertEqual(c.changes(s)['changes'], [])
        latest = self.change('shelf_count', [3])
        self.assertNotEqual(first['id'], latest['id'])

    def test_commander_confirmed_cross_scope_undo_and_guest_list(self):
        self.change('shelf_count', [2], 'oskar')
        with patch.object(self.s, 'decide', return_value={'answer': dict(op='undo', kind=None, values=['shelf count'], person='oskar')}), \
                patch.object(c, 'context', return_value=('ben', 'ben', 'ben')):
            c.special(self.s, 'synthetic undo request', 'movie', routed=True)
            plan = c.get_pending(self.s, 'ben')
            self.assertIsNotNone(plan)
            c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')
        self.assertEqual(shelves.slot_count(self.s, 'oskar'), 4)
        plan = self.plan('history', actor='oskar', since='7d', person='household')
        self.assertEqual({r['scope'] for r in plan['changes']}, {'oskar'})

    def test_shared_link_confirms_targeted_undo_as_adult_viewer(self):
        entry = self.change('ai_budget', [5])
        with patch.object(self.s, 'decide', return_value={'answer': dict(op='undo', values=[entry['id']])}), \
                patch.object(c, 'context', return_value=('together', 'both', 'ben')):
            c.special(self.s, 'synthetic undo request', 'movie', routed=True)
            plan = c.get_pending(self.s, 'together')
            self.assertEqual(plan['actor'], 'ben')
            c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')
        self.assertEqual(self.s.ai_monthly_usd(), 3)
        self.assertTrue(c.changes(self.s)['changes'][0]['undone'])

    def test_stale_redo_and_cross_scope_overlap_refuse_without_writes(self):
        old = self.change('row_rename', ['foryou', 'Invented first'])
        c.apply(self.s, self.plan('undo', [old['id']]))
        redo = self.plan('redo', [old['id']])
        self.change('row_rename', ['foryou', 'Invented second'], 'ben')
        before = self.stored()
        for action in (lambda: self.plan('redo', [old['id']]), lambda: c.apply(self.s, redo)):
            with self.assertRaisesRegex(ValueError, 'later change first'):
                action()
            self.assertEqual(self.stored(), before)

    def test_old_journal_ids_are_stable_and_by_unknown(self):
        (self.data / 'commands.json').write_text(json.dumps({'ana': [dict(
            before=None, after={'hide': ['anime']}, name='Old change')]}))
        first = c.changes(self.s)['changes'][0]
        self.assertEqual(first['by'], 'unknown')
        self.assertIsNone(first['time'])
        self.assertIn('by: unknown', self.plan('history')['description'])
        self.assertEqual(c.changes(self.s)['changes'][0]['id'], first['id'])
        self.change('shelf_count', [2], 'ben')
        self.assertEqual(c.changes(self.s)['changes'][-1]['id'], first['id'])

    def test_shared_link_uses_the_adult_viewer_and_children_undo_own_entries(self):
        for actor in ('kim', 'oskar'):
            entry = self.change('shelf_count', [2], actor)
            c.apply(self.s, self.plan('undo', [entry['id']], actor))
            self.assertEqual(shelves.slot_count(self.s, actor), 4)
        self.change('ai_budget', [5])
        plan = c.preview(self.s, '', 'ana', 'both', answer=dict(op='history', person='household', since='7d'))
        self.assertEqual(len(plan['changes']), 3)
        plan = c.preview(self.s, '', 'ben', 'both', answer=dict(op='undo', values=['AI budget']))
        c.apply(self.s, plan)
        self.assertEqual(self.s.ai_monthly_usd(), 3)

    def test_child_plain_undo_keeps_adult_authored_shelf_permissions(self):
        self.change('shelf_add', ['Cartoons', 'always'], 'kim')
        for op, values in (('shelf_count', [2]), ('shelf_add', ['Invented stories', 'always']),
                           ('shelf_stop', ['Cartoons']), ('reset', ['shelves'])):
            with self.subTest(op=op):
                before = c.shelf_state(self.s, 'kim')
                self.change(op, values, person='kim')
                with patch.object(c, 'context', return_value=('kim:'+op, 'kim', 'kim')):
                    c.special(self.s, 'undo', 'movie')
                self.assertEqual(c.shelf_state(self.s, 'kim'), before)
                self.assertEqual(self.s.kids_age('kim'), 9)

    def test_anonymous_actor_and_default_ceiling(self):
        entry = c.history_entry(self.s, {'name': 'Synthetic guest change', 'actor': 'guest'}, None, None)
        self.assertEqual(entry['by'], 'guest')
        self.assertEqual(self.s.ai_budget(), {'household': None, 'ceiling': 25, 'ceiling_source': 'default'})
        before = self.stored()
        with self.assertRaisesRegex(ValueError, 'ceiling.*25.*USD'):
            self.plan('ai_budget', [26])
        self.assertEqual(self.stored(), before)

    def test_multilingual_recorded_control_cases(self):
        cases = [case for case in commands_eval.load_cases() if case['id'].startswith('w122-')]
        self.assertEqual(len(cases), 30)
        for lang in ('en', 'de', 'sr'):
            rows = [case for case in cases if case['lang'] == lang]
            self.assertEqual(len(rows), 10)
            self.assertEqual({case['expect']['op'] for case in rows}, {'ai_budget', 'history', None})
        report = commands_eval.evaluate(cases)[0]
        self.assertEqual(report['full_accuracy'], 1)

    def test_cli_budget_and_changes_preview_yes_undo(self):
        before = self.stored()
        code, plan = self.cli('ai', 'budget', '5', '--person', 'ana')
        self.assertEqual(code, 0)
        self.assertIn('plan', plan)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.cli('ai', 'budget', '5', '--person', 'ana', '--yes')[0], 0)
        self.assertEqual(self.cli('ai', 'status')[1]['budget']['household'], 5)
        code, rows = self.cli('changes', '--since', '7d', '--person', 'ben')
        self.assertEqual(code, 0)
        entry = rows['changes'][0]
        before = self.stored()
        self.assertEqual(self.cli('changes', 'undo', entry['id'], '--person', 'ben')[0], 0)
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.cli('changes', 'undo', entry['id'], '--person', 'ben', '--yes')[0], 0)
        self.assertEqual(self.s.ai_monthly_usd(), 3)
