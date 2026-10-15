"""Temporary guests and host shelves, with synthetic state and fake transports."""
from contextlib import ExitStack, nullcontext
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM, reset, run_type
from eval import commands_eval
from skald import commands as c, setup, shelves
from skald.__main__ import main
from test_w92_commands import Household


class GuestDays(Household):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime(2026, 10, 15, 12)
        self.enterContext(patch.object(self.s, 'local_now', side_effect=lambda ts=None: self.now))

    def plan(self, op, person='kim', actor='ana', **fields):
        if op == 'member_create':
            fields.setdefault('label', person.title())
        return c.preview(self.s, 'synthetic change', actor, answer=dict(op=op, person=person, **fields))

    def guest(self, **fields):
        c.apply(self.s, self.plan('member_create', guest=True, **fields))

    def test_multi_person_plan_keeps_guest_dates_and_child_age(self):
        answer = dict(op='plan', kind=None, values=[], person=None, steps=[
            dict(op='member_create', kind=None, values=[], person='kim', label='Kim',
                 guest=True, role='child', age=12, days=3),
            dict(op='member_create', kind=None, values=[], person='marko', label='Marko',
                 guest=True, until='2026-10-18')])
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            parsed = self.s.decide('parse_command', {'query': 'Add Kim and Marko as temporary guests',
                                                    'today': '2026-10-15'})
        self.assertEqual(parsed['answer'], answer)
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=parsed['answer']))
        self.assertEqual(c.config(self.s)['household']['guest_until'],
                         {'kim': '2026-10-18', 'marko': '2026-10-18'})
        self.assertEqual(self.s.kids_age('kim'), 12)
        self.assertIsNone(self.s.kids_age('marko'))

    def test_multi_person_undo_checks_every_person_for_evidence(self):
        answer = dict(op='plan', steps=[
            dict(op='member_create', kind=None, values=[], person=name, label=name.title(), guest=True)
            for name in ('kim', 'marko')])
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=answer))
        self.s.write_feedback(dict(who='marko', key='tmdb:990001', kind='movie',
                                   title='Invented Heist', action='love'))
        before = (self.root / 'skald.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'has data'):
            c.undo(self.s, ['ana'])
        self.assertEqual((self.root / 'skald.json').read_bytes(), before)

    def test_expiry_undo_keeps_unrelated_same_name_shelves_ambiguous(self):
        c.apply(self.s, self.shelf(days=1))
        c.apply(self.s, self.shelf(person='ben'))
        self.now += dt.timedelta(days=2)
        c.expire(self.s)
        with self.assertRaisesRegex(ValueError, 'Which change'):
            c.preview(self.s, '', 'ana', answer=dict(op='undo', values=['Invented Heists']))

    def tick(self):
        with ExitStack() as stack:
            for name in ('connectors_tick', 'feed_pass', 'finish_cold_enrichment', 'update_row_metrics',
                         'backfill_countries', 'backfill_ratings', 'backfill_runtime', 'ensure_emb',
                         'save_learn', 'save_nzb', 'save_emb', 'save_facets', 'daily_jobs'):
                stack.enter_context(patch.object(self.s, name))
            stack.enter_context(patch.object(self.s.time, 'sleep', side_effect=SystemExit))
            with self.assertRaises(SystemExit):
                self.s.feed_worker()

    def cli(self, *args, status=0):
        out = io.StringIO()
        with patch.object(setup, 'server', return_value=nullcontext(self.s)):
            self.assertEqual(main([*args, '--json'], out=out), status, out.getvalue())
        return json.loads(out.getvalue()) if status == 0 else out.getvalue()

    def shelf(self, **fields):
        return c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented heists', 'always'], **fields))

    def test_guest_end_preview_tick_once_and_undo_keeps_evidence(self):
        plan = self.plan('member_create', guest=True, days=3, own_row=True)
        self.assertNotIn('kim', c.config(self.s)['household']['members'])
        c.apply(self.s, plan)
        self.assertEqual(c.member_list(self.s)['people'][-1]['guest_until'], '2026-10-18')
        c.apply(self.s, self.shelf(with_person='kim'))
        self.s.write_feedback(dict(who='kim', key='tmdb:990001', kind='movie', title='Invented Heist', action='love'))
        raw = (self.root / 'feedback.jsonl').read_bytes()
        self.now = dt.datetime(2026, 10, 18, 23, 59, 59)
        self.tick()
        self.assertIn('kim', self.s.PEOPLE)
        self.now += dt.timedelta(seconds=1)
        self.tick()
        self.assertEqual(self.s.PEOPLE, ('ana', 'ben'))
        self.assertEqual(c.config(self.s)['household']['retired'], ['kim'])
        self.assertFalse(shelves.slots(self.s, 'ana'))
        self.assertNotIn('kim', self.s.MEMBER_ROWS)
        self.assertEqual((self.root / 'feedback.jsonl').read_bytes(), raw)
        saved = {name: (self.root / name).read_bytes() for name in ('skald.json', 'commands.json')}
        self.tick()
        self.assertEqual(saved, {name: (self.root / name).read_bytes() for name in saved})
        c.undo(self.s, ['ana'])
        self.tick()
        self.assertIn('kim', self.s.PEOPLE)
        self.assertTrue(shelves.slots(self.s, 'ana'))
        self.assertNotIn('kim', c.config(self.s)['household'].get('guest_until', {}))
        self.assertEqual((self.root / 'feedback.jsonl').read_bytes(), raw)

    def test_bring_guest_back_undoes_named_expiry_after_other_change(self):
        self.guest(days=1)
        self.now += dt.timedelta(days=2)
        self.tick()
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='hide', values=['anime'])))
        plan = c.preview(self.s, '', 'ana', answer=dict(op='undo', values=['Kim']))
        c.apply(self.s, plan)
        self.tick()
        self.assertIn('kim', self.s.PEOPLE)
        self.assertEqual(c.config(self.s)['taste']['people']['ana']['prefs']['hide'], ['anime'])

    def test_bound_member_undo_confirms_in_search_and_command_api(self):
        self.guest(person='marko', days=1)
        self.now += dt.timedelta(days=2)
        c.expire(self.s)
        for transport in ('search', 'api'):
            with self.subTest(transport=transport), ReplayLLM(self.s) as llm:
                llm.arm({'choices': [{'message': {'content': json.dumps(dict(op='undo', values=['Marko']))}}]})
                if transport == 'search':
                    with patch.object(c, 'context', return_value=('w92', 'ana', 'ana')):
                        c.special(self.s, 'bring Marko back', 'movie', routed=True)
                        self.confirm()
                else:
                    req = dict(viewer='ana', client={'name': 'round1'}, body={'in': {'query': 'bring Marko back'}})
                    plan = c.api_preview(self.s, req)['preview']
                    c.api_apply(self.s, {**req, 'body': {'plan': plan['plan']}})
                self.assertIn('marko', self.s.PEOPLE)
                self.assertNotIn('marko', c.config(self.s)['household'].get('guest_until', {}))
                c.apply(self.s, self.plan('member_remove', 'marko'))

    def test_update_extend_shorten_clear_promote_and_undo(self):
        self.guest(days=2)
        for fields, expected in ((dict(days=4), '2026-10-19'), (dict(until='2026-10-16'), '2026-10-16'),
                                 (dict(days=0), None), (dict(days=30), '2026-11-14'), (dict(no_end=True), None)):
            before = c.config(self.s)
            plan = self.plan('member_update', **fields)
            self.assertEqual(c.config(self.s), before)
            c.apply(self.s, plan)
            self.assertEqual(c.config(self.s)['household'].get('guest_until', {}).get('kim'), expected)
            c.undo(self.s, ['ana'])
            self.assertEqual(c.config(self.s), before)
        c.apply(self.s, self.plan('member_update', guest=False))
        self.assertNotIn('kim', c.config(self.s)['household'].get('guest_until', {}))

    def test_extended_guest_refuses_stale_removal(self):
        self.guest(days=1)
        plan = self.plan('member_remove')
        c.apply(self.s, self.plan('member_update', days=4))
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(self.s, plan)
        self.now += dt.timedelta(days=2)
        self.tick()
        self.assertIn('kim', self.s.PEOPLE)

    def test_guest_and_partner_shelf_end_together_and_undo_separately(self):
        self.guest(days=1)
        c.apply(self.s, self.shelf(with_person='kim', days=1))
        self.now += dt.timedelta(days=2)
        self.tick()
        self.assertNotIn('kim', self.s.PEOPLE)
        self.assertFalse(shelves.read(self.s)['wishes'])
        c.undo(self.s, ['ana'])
        self.assertIn('kim', self.s.PEOPLE)
        c.undo(self.s, ['ana'])
        self.tick()
        self.assertTrue(shelves.slots(self.s, 'ana'))

    def test_full_member_refuses_any_end(self):
        for fields in (dict(days=3), dict(until='2026-10-18'), dict(days=0), dict(no_end=True)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.plan('member_update', 'ben', **fields)
        with self.assertRaises(ValueError):
            self.plan('member_create', days=3)

    def test_end_ranges_and_conflicting_inputs(self):
        self.guest()
        invalid = [dict(days=n) for n in (-1, 31, True)] + [dict(until=d) for d in
            ('2026-10-14', '2026-11-15', '2026-02-30', '20261018', 'Sunday')]
        invalid += [dict(days=2, until='2026-10-18'), dict(days=2, no_end=True)]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.plan('member_update', **fields)
        with self.assertRaises(ValueError):
            self.plan('member_create', 'newguest', guest=True, days=0)
        for fields in [dict(days=0), *invalid]:
            with self.subTest(shelf=fields), self.assertRaises(ValueError):
                self.shelf(**fields)

    def test_config_validates_optional_guest_dates(self):
        self.guest()
        for dates in ({'ben': '2026-10-18'}, {'kim': '20260230'}, {'kim': 3}, [], {'kim': '2026-02-30'}):
            cfg = c.config(self.s)
            cfg['household']['guest_until'] = dates
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                self.s.validate_config(cfg)

    def test_shelf_until_tick_and_plain_undo(self):
        c.apply(self.s, self.shelf(until='2026-10-18'))
        self.assertEqual(shelves.read(self.s)['wishes'][0]['until'], '2026-10-18')
        self.now = dt.datetime(2026, 10, 18, 23, 59, 59)
        self.tick()
        self.assertTrue(shelves.slots(self.s, 'ana'))
        self.now += dt.timedelta(seconds=1)
        self.tick()
        self.assertFalse(shelves.read(self.s)['wishes'])
        saved = (self.root / 'commands.json').read_bytes()
        self.tick()
        self.assertEqual((self.root / 'commands.json').read_bytes(), saved)
        c.undo(self.s, ['ana'])
        self.tick()
        self.assertTrue(shelves.slots(self.s, 'ana'))
        self.assertNotIn('until', shelves.read(self.s)['wishes'][0])

    def test_expiry_removes_only_due_shelves_and_named_undo(self):
        c.apply(self.s, self.shelf(days=1))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'])))
        self.now += dt.timedelta(days=2)
        self.tick()
        self.assertEqual([w['text'] for w in shelves.read(self.s)['wishes']], ['invented rivers'])
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='undo', values=['Invented Heists'])))
        self.tick()
        self.assertEqual(len(shelves.read(self.s)['wishes']), 2)

    def test_hidden_due_shelf_expires_before_guest(self):
        self.guest(days=1)
        self.s.shelf_change('ana', 'add', slot=4, text='invented heists', days=1, apply=True)
        self.s.shelf_change('ana', 'count', count=3, apply=True)
        self.now += dt.timedelta(days=2)
        c.expire(self.s)
        self.assertFalse(shelves.read(self.s)['wishes'])
        self.assertNotIn('kim', self.s.PEOPLE)

    def test_expiry_logs_individual_failures_and_continues(self):
        self.guest(days=1)
        self.guest(person='marko', days=1)
        c.apply(self.s, self.shelf(days=1))
        c.apply(self.s, self.shelf(person='ben', days=1))
        self.now += dt.timedelta(days=2)
        change, preview = self.s.shelf_change, c.preview
        def fail_shelf(scope, op, **kwargs):
            if scope == 'ana' and op == 'stop':
                raise OSError('synthetic shelf failure')
            return change(scope, op, **kwargs)
        def fail_guest(s, *args, **kwargs):
            if kwargs.get('answer') == dict(op='member_remove', person='kim'):
                raise ValueError('synthetic guest failure')
            return preview(s, *args, **kwargs)
        with patch.object(self.s, 'shelf_change', side_effect=fail_shelf), patch.object(c, 'preview', side_effect=fail_guest), patch.object(self.s, '_daily_log') as log:
            c.expire(self.s)
        self.assertEqual([w['scope'] for w in shelves.read(self.s)['wishes']], ['ana'])
        self.assertIn('kim', self.s.PEOPLE)
        self.assertNotIn('marko', self.s.PEOPLE)
        self.assertEqual(log.call_count, 2)
        self.assertIn('OSError', log.call_args_list[0].args[0])
        self.assertIn('ValueError', log.call_args_list[1].args[0])

    def test_expiry_keeps_replaced_or_extended_shelf_before_stop_preview(self):
        change = self.s.shelf_change
        for edit in ('replace', 'extend'):
            with self.subTest(edit=edit):
                c.apply(self.s, self.shelf(days=1))
                wish = shelves.read(self.s)['wishes'][-1]
                self.now += dt.timedelta(days=2)
                end = (self.now.date() + dt.timedelta(days=7)).isoformat()
                def interleave(scope, op, **kwargs):
                    if op == 'stop' and not kwargs.get('plan'):
                        if edit == 'replace':
                            change(scope, 'replace', slot=wish['slot'], text='invented replacement', until=end, apply=True)
                        else:
                            doc = shelves.read(self.s)
                            next(w for w in doc['wishes'] if w['id'] == wish['id'])['until'] = end
                            shelves.save(self.s, doc)
                    return change(scope, op, **kwargs)
                with patch.object(self.s, 'shelf_change', side_effect=interleave):
                    c.expire(self.s)
                current = next(w for w in shelves.read(self.s)['wishes'] if w['slot'] == wish['slot'])
                self.assertEqual(current['until'], end)
                self.assertEqual(current['id'] == wish['id'], edit == 'extend')

    def test_plain_undo_restores_two_expiries_without_changing_other_shelves(self):
        c.apply(self.s, self.shelf(days=1))
        c.apply(self.s, self.shelf(days=1))
        c.apply(self.s, self.shelf(days=7))
        original = shelves.read(self.s)['wishes']
        self.now += dt.timedelta(days=2)
        c.expire(self.s)
        for _ in range(2):
            c.undo(self.s, ['ana'])
        current = {w['id']: w for w in shelves.read(self.s)['wishes']}
        self.assertEqual(current[original[2]['id']], original[2])
        for wish in original[:2]:
            self.assertEqual(current[wish['id']], {k: v for k, v in wish.items() if k != 'until'})
        c.expire(self.s)
        self.assertEqual(len(shelves.read(self.s)['wishes']), 3)

    def test_timed_shelves_and_guest_shelves_refuse_kids_and_guests(self):
        self.guest()
        c.apply(self.s, self.plan('member_update', 'ben', age=12))
        for actor in ('ben', 'kim', self.s.GUEST):
            for op, fields in (('member_update', dict(person='kim', days=3)),
                               ('member_until', dict(person='kim')),
                               ('shelf_add', dict(values=['invented heists', 'always'], days=3)),
                               ('shelf_add', dict(values=['invented heists', 'always'], with_person='kim'))):
                with self.subTest(actor=actor, op=op, fields=fields), self.assertRaises(PermissionError):
                    c.preview(self.s, '', actor, answer=dict(op=op, **fields))
        self.assertEqual(c.REGISTRY['member_until']['scope'], 'household')
        plan = self.shelf(days=2)
        cfg = c.config(self.s)
        cfg['household']['guests'].append('ana')
        cfg['household']['guests'].remove('kim')
        setup.publish(self.root, cfg, replace=True)
        with self.assertRaises(PermissionError):
            c.apply(self.s, plan)

    def test_preview_rechecks_child_role_and_resolved_guest_before_saving(self):
        self.guest()
        self.guest(person='kim2')
        cfg = c.config(self.s)
        cfg['taste']['people']['ben']['prefs'] = {'kids_age': 12}
        setup.publish(self.root, cfg, replace=True)
        before = shelves.read(self.s)
        for actor, fields in (('ben', dict(days=3)), ('kim2', dict(with_person='Kim'))):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                c.preview(self.s, '', actor, answer=dict(op='shelf_add', values=['invented heists', 'always'], **fields))
            self.assertEqual(shelves.read(self.s), before)

    def test_cli_guest_dates_preview_apply_list_update_and_undo(self):
        args = ('members', 'add', 'kim', '--guest', '--days', '3', '--person', 'ana')
        plan = self.cli(*args)
        self.assertNotIn('kim', self.s.PEOPLE)
        self.cli(*args, '--plan', plan['plan'])
        self.assertEqual(self.cli('members', 'list')['people'][-1]['guest_until'], '2026-10-18')
        self.cli('members', 'update', 'kim', '--no-end', '--person', 'ana', '--yes')
        self.cli('members', '--undo', '--person', 'ana')
        self.assertEqual(c.config(self.s)['household']['guest_until']['kim'], '2026-10-18')

    def test_cli_timed_shared_shelf_preview_apply_and_commander_undo(self):
        self.guest()
        args = ('shelf', 'add', '--person', 'ana', '--with-person', 'kim', '--days', '3', 'invented heists')
        plan = self.cli(*args)
        self.assertFalse(shelves.read(self.s)['wishes'])
        self.cli(*args, '--plan', plan['plan'])
        self.assertEqual(shelves.read(self.s)['wishes'][0]['until'], '2026-10-18')
        self.cli('command', '--undo', '--person', 'ana')
        self.assertFalse(shelves.read(self.s)['wishes'])

    def test_shelf_api_rechecks_guest_on_a_saved_timed_plan(self):
        self.guest()
        plan = self.s.shelf_change('kim', 'add', text='invented heists', days=3)
        with self.assertRaises(PermissionError):
            self.s.api_shelves(dict(viewer='kim', body={'plan': plan['plan']}))
        self.assertFalse(shelves.read(self.s)['wishes'])

    def test_commander_guest_until_is_read_only(self):
        self.guest(days=3)
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        plan = self.plan('member_until')
        self.assertTrue(plan['read_only'])
        self.assertIn('2026-10-18', plan['name'] + plan.get('description', ''))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(dict(op='member_until', person='kim'))}}]})
            card = c.special(self.s, 'When does Kim leave?', 'movie', routed=True)['metas'][0]
        self.assertIn('2026-10-18', card['description'])

    def test_llm_relative_date_context_and_host_scope(self):
        self.guest()
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(dict(op='shelf_add', values=['invented heists', 'always'],
                person=None, with_person='kim', until='2026-10-18'))}}]})
            plan = c.preview(self.s, 'Katalog za Kim i mene do nedelje', 'ana')
            self.assertEqual((plan['shelf']['scope'], plan['shelf']['with_person'], plan['shelf']['until']),
                             ('ana', 'kim', '2026-10-18'))
            self.assertIn('2026-10-15', llm.bodies[0]['messages'][1]['content'])
            self.assertEqual(llm.calls, 1)

    def test_guest_blend_only_with_ratings_and_full_member_bytes_unchanged(self):
        self.guest()
        s = self.s
        cards = [dict(id=f'tmdb:{990010+i}', type='movie', name=f'Invented Film {i}') for i in range(9)]
        for partner in ('kim', 'ben', None):
            c.apply(s, self.shelf(with_person=partner))
        for wish in shelves.read(s)['wishes']:
            s._feeds[s.rid(f"shelfraw_ana_{wish['slot']}"), s.LANG] = dict(wish=wish['id'], window='always', candidates=cards, status='filled')
        def entry(n, who, signal, weight, **item):
            return dict(key=f'tmdb:{990100+n}', title=f'Invented Evidence {n}', kind='movie', stype='movie',
                        by=[who], who='remote', signal=signal, weight=weight, age=0, genres=[], item=item)
        watched = [entry(i, 'kim', 'watched', 2) for i in (0, 1)]
        rated = entry(2, 'kim', 'liked', 2)
        foreign = entry(3, 'ben', 'liked', 2)
        for i, key in enumerate([x['id'] for x in cards] + [e['key'] for e in [*watched, rated, foreign]]):
            drama = i in (4, 5, 6, 7, 8, 11)
            s._facets[key] = dict(kind='movie', title='Invented Film', g=['Drama' if drama else 'Action'])
            s._emb[key] = [0., 1.] if drama else [1., 0.]
        s._emb[rated['key']] = [.25, 1.]
        def profile(entries):
            s._profiles['household'] = dict(entries=entries, seen={e['key'] for e in entries}, negative=set())
            s._model['m'] = s.build_model(s._profiles['household'], enrich=False)
        def feed():
            return [shelves.fill(s, s.LANG, build=False)[s.rid(f'shelf_ana_{n}')]['tv'] for n in (1, 2, 3)]
        def adult_bytes():
            return json.dumps([s.member_scores(cards, p) for p in ('ana', 'ben')], sort_keys=True).encode()
        profile([*watched, foreign])
        baseline = json.dumps(feed(), sort_keys=True).encode()
        self.assertEqual(feed()[0], feed()[2])
        for signal, weight, item in (('liked', 2, {}), ('loved', 3, {}), ('watched', .3, {'rating': 5})):
            with self.subTest(signal=signal):
                rated.update(signal=signal, weight=weight, item=item)
                profile([rated, foreign])
                expected = feed()
                adults = adult_bytes()
                self.assertNotEqual(expected[0], expected[2])
                self.assertEqual(expected[1], expected[2])
                profile([*watched, rated, foreign])
                self.assertEqual(feed(), expected)
                self.assertEqual(adult_bytes(), adults)
                self.assertEqual(json.dumps(feed()[1:], sort_keys=True).encode(),
                                 json.dumps(json.loads(baseline)[1:], sort_keys=True).encode())

    def test_old_source_unavailable_skips_for_missing_git_or_either_file(self):
        source = 'def validate_config(cfg, data=None):\n    return cfg\n\ndef stamp_household():\n    pass\n'
        reason = 'the 1.4.9 source (cc016e7) is not in this checkout'
        for error in (OSError('git unavailable'), subprocess.CalledProcessError(128, ['git', 'show'])):
            for available in (0, 1):
                with self.subTest(error=type(error).__name__, available=available):
                    result = unittest.TestResult()
                    replies = [types.SimpleNamespace(stdout=source)] * available + [error]
                    with patch.object(subprocess, 'run', side_effect=replies):
                        type(self)('test_cc016e7_saved_extensions_pass_original_rules').run(result)
                    self.assertFalse(result.errors, result.errors)
                    self.assertEqual([why for _, why in result.skipped], [reason])

    def test_cc016e7_saved_extensions_pass_original_rules(self):
        self.guest(days=3)
        c.apply(self.s, self.shelf(with_person='kim', days=3))
        cfg, doc = c.config(self.s), shelves.read(self.s)
        root = Path(__file__).resolve().parents[1]
        try:
            source = subprocess.run(['git', 'show', 'cc016e7:server.py'], cwd=root, check=True, capture_output=True, text=True).stdout
            shelf_source = subprocess.run(['git', 'show', 'cc016e7:skald/shelves.py'], cwd=root, check=True, capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('the 1.4.9 source (cc016e7) is not in this checkout')
        # Copy the original validator's rule, including its permissive household merge.
        start, end = source.index('def validate_config('), source.index('\ndef stamp_household(')
        old = dict(self.s.__dict__)
        exec(source[start:end], old)
        self.assertEqual(old['validate_config'](cfg, data=self.s.DATA), cfg)
        old_shelves = types.ModuleType('skald.old_shelves')
        old_shelves.__package__ = 'skald'
        exec(compile(shelf_source, 'old_shelves.py', 'exec'), old_shelves.__dict__)
        self.assertEqual(old_shelves.read(self.s), doc)
        self.now += dt.timedelta(days=5)
        self.assertEqual(old_shelves.window(doc['wishes'][0], self.now), 'always')
        self.assertIn('kim', cfg['household']['members'])

    def test_multilingual_recorded_guestdays_cases(self):
        cases = [case for case in commands_eval.load_cases() if case['id'].startswith('w125-')]
        self.assertGreaterEqual(len(cases), 27)
        self.assertEqual({case['lang'] for case in cases}, {'en', 'de', 'sr'})
        self.assertTrue(all(row['full_ok'] for row in commands_eval.evaluate(cases)[0]['results']))

    def test_expanded_decision_fixtures_do_not_exhaust_the_fake_daily_budget(self):
        reset(self.s)
        self.assertEqual(run_type(self.s, 'parse_command')['acc'], 1)
