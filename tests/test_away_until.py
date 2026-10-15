"""Dated adult absences, using synthetic state and recorded model replies."""
from contextlib import ExitStack, nullcontext
import copy
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM
from eval import commands_eval
from skald import commands as c, leave, setup
from skald.__main__ import main
from test_w92_commands import Household


class AwayUntil(Household):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime(2026, 10, 15, 12)
        self.enterContext(patch.object(self.s, 'local_now', side_effect=lambda ts=None: self.now))

    def plan(self, op='member_away', person='ben', **fields):
        plan = c.preview(self.s, '', 'ana', answer=dict(op=op, person=person, **fields))
        if plan.get('forget'):
            plan['source'] = 'cli'
        return plan

    def cli(self, *args):
        out = io.StringIO()
        with patch.object(setup, 'server', return_value=nullcontext(self.s)):
            self.assertEqual(main([*args, '--json'], out=out), 0, out.getvalue())
        return json.loads(out.getvalue())

    def tick(self):
        with ExitStack() as stack:
            for name in ('connectors_tick', 'feed_pass', 'finish_cold_enrichment', 'update_row_metrics',
                         'backfill_countries', 'backfill_ratings', 'backfill_runtime', 'ensure_emb',
                         'save_learn', 'save_nzb', 'save_emb', 'save_facets', 'daily_jobs'):
                stack.enter_context(patch.object(self.s, name))
            stack.enter_context(patch.object(self.s.time, 'sleep', side_effect=SystemExit))
            with self.assertRaises(SystemExit):
                self.s.feed_worker()

    def test_preview_dates_list_public_config_and_undo(self):
        before = c.config(self.s)
        plan = self.plan(until='2026-11-01')
        self.assertEqual(c.config(self.s), before)
        self.assertIn('2026-11-02', plan['name'] + plan['description'])
        c.apply(self.s, plan)
        hh = c.config(self.s)['household']
        self.assertEqual(hh['away_until'], {'ben': '2026-11-01'})
        self.assertEqual(hh['away'], ['ben'])
        self.assertEqual(c.member_list(self.s)['people'][1]['away_until'], '2026-11-01')
        self.assertEqual(self.s.public_config(c.config(self.s))['household']['away_until'], hh['away_until'])
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)

    def test_extend_shorten_and_undo_an_existing_absence(self):
        c.apply(self.s, self.plan())
        for fields, end in ((dict(days=14), '2026-10-29'), (dict(until='2026-10-16'), '2026-10-16')):
            before = c.config(self.s)
            c.apply(self.s, self.plan(**fields))
            self.assertEqual(c.config(self.s)['household']['away_until']['ben'], end)
            c.undo(self.s, ['ana'])
            self.assertEqual(c.config(self.s), before)
        c.apply(self.s, self.plan(days=2))
        before = c.config(self.s)
        c.apply(self.s, self.plan(days=4))
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)
        with self.assertRaisesRegex(ValueError, 'already away'):
            self.plan()

    def test_undated_absence_has_no_expiry(self):
        c.apply(self.s, self.plan())
        self.assertNotIn('away_until', c.config(self.s)['household'])
        self.now += dt.timedelta(days=60)
        c.expire(self.s)
        self.assertEqual(c.config(self.s)['household']['away'], ['ben'])

    def test_date_changes_preserve_other_absences_and_undo_independently(self):
        c.apply(self.s, self.plan('member_create', 'kim', label='Kim'))
        for person in ('ben', 'kim'):
            c.apply(self.s, self.plan(person=person, days=1))
        c.apply(self.s, self.plan(days=3))
        update = c.journal(self.s)['ana'][-1]['id']
        self.assertEqual(c.config(self.s)['household']['away'], ['ben', 'kim'])
        c.apply(self.s, self.plan(person='kim', days=4))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='undo', values=[update])))
        self.assertEqual(c.config(self.s)['household']['away_until'],
                         {'ben': '2026-10-16', 'kim': '2026-10-19'})

    def test_manual_back_and_removal_clear_date_and_undo_restores_it(self):
        c.apply(self.s, self.plan(days=3))
        for op in ('member_back', 'member_remove'):
            with self.subTest(op=op):
                before = c.config(self.s)
                c.apply(self.s, self.plan(op))
                self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
                c.undo(self.s, ['ana'])
                self.assertEqual(c.config(self.s), before)

    def test_forget_and_leave_settings_clear_date(self):
        c.apply(self.s, self.plan(days=3))
        cfg = leave.settings(copy.deepcopy(c.config(self.s)), 'ben')
        self.assertNotIn('ben', cfg['household'].get('away_until', {}))
        c.apply(self.s, self.plan('member_forget'))
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        self.assertNotIn('ben', self.s.PEOPLE)

    def test_worker_returns_after_inclusive_day_once_and_undo_keeps_absence_undated(self):
        c.apply(self.s, self.plan(until='2026-10-18'))
        self.now = dt.datetime(2026, 10, 18, 23, 59, 59)
        self.tick()
        self.assertEqual(c.config(self.s)['household']['away'], ['ben'])
        self.now += dt.timedelta(seconds=1)
        self.tick()
        self.assertNotIn('ben', c.config(self.s)['household'].get('away', []))
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        entry = c.journal(self.s)['ana'][-1]
        self.assertEqual((entry['change']['op'], entry['source'], entry['expired']), ('member_back', 'expiry', True))
        saved = {name: (self.root / name).read_bytes() for name in ('skald.json', 'commands.json')}
        self.tick()
        self.assertEqual(saved, {name: (self.root / name).read_bytes() for name in saved})
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='undo', values=[entry['id']])))
        self.now += dt.timedelta(days=1)
        c.expire(self.s)
        self.assertEqual(c.config(self.s)['household'].get('away', []), ['ben'])
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        self.assertTrue(c.journal(self.s)['ana'][-1]['undone'])
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='redo', values=[entry['id']])))
        self.assertNotIn('ben', c.config(self.s)['household'].get('away', []))
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s)['household']['away'], ['ben'])
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        c.undo(self.s, ['ana'])
        self.assertNotIn('ben', c.config(self.s)['household'].get('away', []))
        self.assertNotIn('ben', c.config(self.s)['household'].get('away_until', {}))
        self.assertTrue(all(e['undone'] for e in c.journal(self.s)['ana']))
        self.assertEqual(c.journal(self.s)['ana'][0]['member']['config_after']['household']['away_until'],
                         {'ben': '2026-10-18'})

    def test_expiry_skips_removed_forgotten_and_manually_returned_people(self):
        for op in ('member_back', 'member_remove', 'member_forget'):
            with self.subTest(op=op):
                c.apply(self.s, self.plan(days=1))
                c.apply(self.s, self.plan(op))
                self.now += dt.timedelta(days=2)
                stale = c.config(self.s)
                stale['household']['away_until'] = {'ben': '2026-10-16'}
                saved = {name: (self.root / name).read_bytes() for name in ('skald.json', 'commands.json')}
                with patch.object(c, 'config', return_value=stale), patch.object(c, 'preview') as preview:
                    c.expire(self.s)
                    preview.assert_not_called()
                self.assertEqual(saved, {name: (self.root / name).read_bytes() for name in saved})
                if op == 'member_remove':
                    c.undo(self.s, ['ana'])
                    c.apply(self.s, self.plan('member_back'))

    def test_expiry_isolates_failures_and_keeps_guest_expiry(self):
        for person, guest in (('kim', False), ('marko', True)):
            c.apply(self.s, self.plan('member_create', person, label=person.title(), guest=guest,
                                      **({'days': 1} if guest else {})))
        for person in ('ben', 'kim'):
            c.apply(self.s, self.plan(person=person, days=1))
        self.now += dt.timedelta(days=2)
        preview = c.preview
        def fail(s, *args, **kwargs):
            if kwargs.get('answer') == dict(op='member_back', person='ben'):
                raise ValueError('synthetic return failure')
            return preview(s, *args, **kwargs)
        with patch.object(c, 'preview', side_effect=fail), patch.object(self.s, '_daily_log') as log:
            c.expire(self.s)
        self.assertEqual(c.config(self.s)['household']['away'], ['ben'])
        self.assertNotIn('marko', self.s.PEOPLE)
        self.assertEqual(log.call_count, 1)
        self.assertIn('ValueError', log.call_args.args[0])

    def test_expiry_keeps_date_extended_before_return_preview(self):
        c.apply(self.s, self.plan(days=1))
        self.now += dt.timedelta(days=2)
        preview = c.preview
        def extend(s, *args, **kwargs):
            if kwargs.get('answer') == dict(op='member_back', person='ben'):
                c.apply(s, preview(s, '', 'ana', answer=dict(op='member_away', person='ben', days=7)))
            return preview(s, *args, **kwargs)
        with patch.object(c, 'preview', side_effect=extend):
            c.expire(self.s)
        self.assertEqual(c.config(self.s)['household']['away_until']['ben'], '2026-10-24')

    def test_invalid_dates_and_unrelated_ops_reject_date_fields(self):
        invalid = [dict(days=n) for n in (-1, 0, 31, True)] + [dict(until=d) for d in
            ('2026-10-14', '2026-11-15', '2026-02-30', '20261018', 'Friday')]
        invalid += [dict(days=2, until='2026-10-18'), dict(no_end=True)]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.plan(**fields)
        for op in ('member_back', 'member_remove', 'member_forget'):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.plan(op, days=3)
        c.apply(self.s, self.plan(until='2026-10-15'))
        c.expire(self.s)
        self.assertEqual(c.config(self.s)['household']['away'], ['ben'])

    def test_config_rejects_invalid_or_inactive_away_dates(self):
        c.apply(self.s, self.plan())
        for dates in ({'ana': '2026-10-18'}, {'absent': '2026-10-18'}, {'ben': '20260230'},
                      {'ben': 3}, [], {'ben': '2026-02-30'}):
            cfg = c.config(self.s)
            cfg['household']['away_until'] = dates
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                self.s.validate_config(cfg)

    def test_changed_date_refuses_stale_plan(self):
        c.apply(self.s, self.plan(days=2))
        plan = self.plan('member_back')
        c.apply(self.s, self.plan(days=4))
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s)['household']['away_until']['ben'], '2026-10-19')

    def test_cli_days_until_preview_apply_update_and_undo(self):
        args = ('members', 'away', 'ben', '--person', 'ana')
        plan = self.cli(*args, '--days', '14')
        self.assertNotIn('away', c.config(self.s)['household'])
        self.cli(*args, '--days', '14', '--plan', plan['plan'])
        self.assertEqual(self.cli('members', 'list')['people'][1]['away_until'], '2026-10-29')
        self.cli(*args, '--until', '2026-11-01', '--yes')
        self.cli('members', '--undo', '--person', 'ana')
        self.assertEqual(c.config(self.s)['household']['away_until']['ben'], '2026-10-29')

    def test_commander_uses_model_date_and_local_today(self):
        answer = dict(op='member_away', kind=None, values=[], person='ben', until='2026-10-16')
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = c.preview(self.s, 'Ben je odsutan do petka', 'ana')
        self.assertIn('2026-10-15', llm.bodies[0]['messages'][1]['content'])
        c.apply(self.s, plan)
        self.assertEqual(c.config(self.s)['household']['away_until']['ben'], '2026-10-16')
        self.assertEqual(self.s.DECISIONS['parse_command']['ver'], 19)

    def test_149_validator_preserves_away_and_ignores_return_date(self):
        c.apply(self.s, self.plan(days=3))
        cfg = c.config(self.s)
        try:
            source = subprocess.run(['git', 'show', 'cc016e7:server.py'], cwd=Path(__file__).resolve().parents[1],
                                    check=True, capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('the 1.4.9 source (cc016e7) is not in this checkout')
        old = dict(self.s.__dict__)
        exec(source[source.index('def validate_config('):source.index('\ndef stamp_household(')], old)
        validated = old['validate_config'](cfg, data=self.s.DATA)
        self.assertEqual(validated, cfg)
        self.assertEqual(validated['household']['away'], ['ben'])
        undated = copy.deepcopy(cfg)
        undated['household'].pop('away_until')
        self.assertEqual(old['validate_config'](undated, data=self.s.DATA)['household']['away'], ['ben'])

    def test_recorded_cases_cover_dates_typos_indirect_wording_and_titles(self):
        cases = [case for case in commands_eval.load_cases() if case['id'].startswith('away-until-')]
        self.assertEqual(len(cases), 18)
        s, spec = commands_eval.synthetic_spec('/synthetic')
        for lang in ('en', 'de', 'sr'):
            rows = [case for case in cases if case['lang'] == lang]
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(case['expect']['op'] is None for case in rows), 1)
        for case in cases:
            self.assertIsNone(spec['rule']({'query': case['text'], 'today': case['today']}, {}), case['id'])
        self.assertTrue(all(row['full_ok'] for row in commands_eval.evaluate(cases)[0]['results']))
