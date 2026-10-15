"""Named guests change only their own scope, using synthetic data and fake transports."""
import copy
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from skald import commands as c, setup, shelves
from skald.__main__ import main
from test_w116_settings import SettingsFixture


HOUSEHOLD = {
    'household_erase': {},
    'ai_budget': dict(values=[2]),
    'row_rename': dict(values=['foryou', 'Invented picks']),
    'row_hide': dict(values=['top10']), 'row_show': dict(values=['digital']),
    'row_move': dict(values=['foryou', 'episodes']), 'row_lang': dict(values=['en-US']),
    'home_more': dict(values=['mood']), 'home_fewer': dict(values=['lane']),
    'row_refresh': dict(values=['foryou']),
    'lab_keep': dict(values=['invented-row']), 'lab_retire': dict(values=['invented-row']),
    'lab_on': {}, 'lab_off': {},
    'member_create': dict(person='kim', label='Kim', age=30),
    'member_update': dict(person='marko', guest=False),
    'member_remove': dict(person='ben'), 'member_rows': dict(person='marko', own_row=True),
    'member_until': dict(person='marko'),
    'reattribute': dict(person='ben', values=['Invented River']),
    'member_forget': dict(person='ben'), 'member_away': dict(person='ben'), 'member_back': dict(person='ben'),
    'plays_mode': dict(values=['off']), 'plays_every': dict(values=['1h']),
}
READ = {'settings', 'history', 'shelf_list', 'ask', 'search', 'explain', 'taste_drivers',
        'home_reach', 'home_plan', 'ai_status', 'week_picks', 'row_names', 'lab_status', 'lab_moments',
        'plays_status', 'plays_recent', 'plays_explain', 'plays_check', 'plays_around'}
OWN = {'hide', 'show', 'exclude_genre', 'include_genre', 'focus', 'unfocus', 'discovery', 'years',
       'like', 'dislike', 'seen', 'less', 'more', 'runtime', 'kids_age', 'undo', 'redo', 'reset', 'shelf_add',
       'shelf_stop', 'shelf_count', 'plan', 'correct', 'wish', 'shelf_filter', 'shelf_schedule',
       'defer', 'reject', 'ignore_learning', 'like_results', 'shelf_from_results', 'plays_person'}


class GuestGuard(SettingsFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data)}))
        cfg = c.config(self.s)
        cfg['household']['shelves'] = 2
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(
            op='member_create', person='marko', label='Marko', age=30, guest=True)))
        self.item = dict(key='tmdb:990001', kind='movie', title='Invented River', year='2026')
        self.enterContext(patch.object(self.s, 'ensure_emb', side_effect=AssertionError('embedding forbidden')))
        self.enterContext(patch.object(c, 'resolve', return_value=self.item))
        self.enterContext(patch.object(c, 'find_title', return_value=self.item))
        self.s._profiles['household'] = {'entries': [dict(self.item, by=['marko'], signal='watched', weight=1)],
                                         'seen': set(), 'negative': set()}

    def guest_plan(self, op, actor='marko', bound=None, **fields):
        return c.preview(self.s, 'synthetic change', actor, bound,
                         answer={'op': op, 'kind': None, 'values': [], 'person': None, **fields})

    def refused(self, action):
        before = self.stored()
        with self.assertRaisesRegex(PermissionError, 'Ask a household member to change'):
            action()
        self.s.flush_decisions()
        self.assertEqual(self.stored(), before)

    def demote(self, person='ben'):
        cfg = c.config(self.s)
        cfg['household']['guests'].append(person)
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()

    def test_every_op_has_a_scope(self):
        self.assertEqual(set(c.REGISTRY), set(HOUSEHOLD) | READ | OWN)
        for op, spec in c.REGISTRY.items():
            with self.subTest(op=op):
                self.assertEqual(spec['scope'], 'household' if op in HOUSEHOLD else 'read' if op in READ else 'own')

    def test_every_household_op_refuses_preview_and_bound_without_writes(self):
        for op, fields in HOUSEHOLD.items():
            for bound in (None, 'marko'):
                with self.subTest(op=op, bound=bound):
                    self.refused(lambda: self.guest_plan(op, bound=bound, **fields))

    def test_other_people_and_together_refused_even_when_bound(self):
        changes = [dict(op='exclude_genre', values=['Horror']), dict(op='kids_age', values=[12]),
                   dict(op='like', values=['Invented River']),
                   dict(op='shelf_add', values=['invented stories', 'always']),
                   dict(op='shelf_count', values=[2]), dict(op='shelf_schedule', values=['invented seasons'], months=[12]),
                   dict(op='shelf_filter', values=['Invented stories'], filters={'runtime': 90}),
                   dict(op='shelf_stop', values=['Invented stories']), dict(op='wish', values=['invented stories']),
                   dict(op='reset', values=['everything']), dict(op='ignore_learning', values=['Invented River']),
                   dict(op='defer', values=['Invented River'], until='tonight'),
                   dict(op='undo'), dict(op='redo', values=['invented'])]
        for change in changes:
            for person in ('ana', 'ben', 'Ana', 'together', 'everyone', 'both'):
                for bound in (None, 'marko'):
                    with self.subTest(change=change, person=person, bound=bound):
                        self.refused(lambda: self.guest_plan(bound=bound, person=person, **change))

    def test_own_filters_feedback_shelves_and_undo_stay_personal(self):
        before = c.config(self.s)
        household_hash = self.s.taste_hash('household', [])
        for op, fields in (
            ('exclude_genre', dict(values=['Horror'], person='marko')),
            ('like', dict(values=['Invented River'])), ('dislike', dict(values=['Invented River'])),
            ('seen', dict(values=['Invented River'])), ('defer', dict(values=['Invented River'], until='tonight')),
            ('reject', dict(values=['Invented River'], reason='invented reason')),
            ('ignore_learning', dict(values=['Invented River'])),
            ('shelf_add', dict(values=['invented stories', 'always'])),
            ('shelf_filter', dict(values=['Invented Stories'], filters={'runtime': 90})),
            ('shelf_count', dict(values=[3])), ('wish', dict(values=['invented stories'])),
        ):
            with self.subTest(op=op):
                c.apply(self.s, self.guest_plan(op, **fields))
                cfg = c.config(self.s)
                self.assertEqual(cfg['household'], before['household'])
                self.assertEqual(cfg['region'], before['region'])
                for p in ('ana', 'ben'):
                    self.assertEqual(cfg['taste']['people'][p], before['taste']['people'][p])
                self.assertEqual(self.s.taste_hash('household', []), household_hash)
        self.assertTrue(all(e['who'] == 'marko' for e in self.s.read_feedback()))
        self.assertEqual({w['scope'] for w in shelves.read(self.s)['wishes']}, {'marko'})
        self.assertEqual(set(c.journal(self.s)) - {'ana'}, {'marko'})
        c.apply(self.s, self.guest_plan('undo'))
        self.assertEqual(shelves.slot_count(self.s, 'marko'), 4)
        c.undo(self.s, ['marko'], actor='marko')
        c.apply(self.s, self.guest_plan('reset', values=['everything']))
        c.apply(self.s, self.guest_plan('undo'))
        self.assertEqual(c.config(self.s)['household'], before['household'])

    def test_own_age_filter_keeps_household_and_kid_rules(self):
        before = c.config(self.s)
        c.apply(self.s, self.guest_plan('kids_age', values=[12]))
        self.assertEqual(c.config(self.s)['household'], before['household'])
        for p in ('ana', 'ben'):
            self.assertEqual(c.config(self.s)['taste']['people'][p], before['taste']['people'][p])
        self.assertEqual(self.s.kids_age('marko'), 12)
        with self.assertRaisesRegex(PermissionError, 'Ask a grown-up'):
            self.guest_plan('exclude_genre', values=['Horror'])

    def catalog(self, scope):
        s = self.s
        meta = dict(id=self.item['key'], type='movie', name=self.item['title'], _row={'votes': 1000})
        with s.addon_scope(scope), \
                patch.object(s, 'known_rank', return_value=[(1, None, self.item['key'], 'movie')]), \
                patch.object(s, 'known_meta', return_value=meta), \
                patch.object(s, 'with_match', side_effect=lambda x, **kw: x), \
                patch.object(s, 'row_eligible', return_value=True):
            return [x['id'] for x in s.catalog_picks(s._profiles['household'], {}, s.rid('foryou'))]

    def test_guest_ignore_excludes_only_own_catalog(self):
        self.s._profiles['household']['entries'][0]['by'] = ['together']
        before = {scope: self.catalog(scope) for scope in (None, 'together', 'ana', 'ben', 'marko')}
        self.assertTrue(all(ids == [self.item['key']] for ids in before.values()))
        c.apply(self.s, self.guest_plan('ignore_learning', values=[self.item['title']]))
        for scope, ids in before.items():
            with self.subTest(scope=scope):
                self.assertEqual(self.catalog(scope), [] if scope == 'marko' else ids)

    def test_guest_ignore_keeps_shared_watches_and_learning(self):
        p = self.s._profiles['household']
        p['entries'] = [dict(p['entries'][0], by=by) for by in
                        (['together'], ['ana'], ['ana', 'marko'], ['marko'])]
        before = copy.deepcopy(p['entries'])
        ana = self.s.person_entries(p, 'ana')
        together = self.s.person_entries(p, 'together')
        c.apply(self.s, self.guest_plan('ignore_learning', values=[self.item['title']]))
        self.assertEqual(p['entries'], before)
        self.assertEqual(self.s.taste_entries(p['entries']), before)
        self.assertEqual(self.s.person_entries(p, 'ana'), ana)
        self.assertEqual(self.s.person_entries(p, 'together'), together)
        self.assertEqual([e['weight'] for e in self.s.person_entries(p, 'marko')], [0, 0])

    def test_adult_ignore_keeps_household_catalog_effect(self):
        self.s._profiles['household']['entries'][0]['by'] = ['together']
        c.apply(self.s, self.guest_plan('ignore_learning', actor='ana', values=[self.item['title']]))
        for scope in (None, 'together', 'ana', 'ben', 'marko'):
            with self.subTest(scope=scope):
                self.assertEqual(self.catalog(scope), [] if scope in (None, 'together', 'ana') else [self.item['key']])

    def test_adult_plan_keeps_pre_guard_id_and_cli_apply(self):
        with patch.object(self.s, 'decide', return_value={'answer': dict(c.NONE, op='hide', values=['anime'])}), \
                patch.object(setup, 'server', return_value=nullcontext(self.s)):
            plan = c.preview(self.s, 'hide anime', 'ana')
            expected = c.seal({k: v for k, v in plan.items() if k not in ('actor', 'plan')})['plan']
            out = io.StringIO()
            self.assertEqual(main(['command', 'hide anime', '--person', 'ana', '--plan', expected, '--json'], out=out),
                             0, out.getvalue())
        self.assertEqual(plan['plan'], expected)
        self.assertEqual(c.config(self.s)['taste']['people']['ana']['prefs']['hide'], ['anime'])

    def test_kid_plan_keeps_pre_guard_id(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ben']['prefs'] = {'kids_age': 12}
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        plan = self.guest_plan('wish', actor='ben', values=['invented stories'])
        expected = c.seal({k: v for k, v in plan.items() if k not in ('actor', 'plan')})['plan']
        self.assertEqual(plan['plan'], expected)

    def test_adult_correction_keeps_pre_guard_id(self):
        prior = self.guest_plan('hide', actor='ana', values=['anime'])
        answer = dict(op='correct', values=['anime', 'documentaries'])
        with patch.object(c, 'preview', c._preview):
            expected = c.preview(self.s, 'synthetic correction', 'ana', answer=answer, prior=prior)['plan']
        plan = c.preview(self.s, 'synthetic correction', 'ana', answer=answer, prior=prior)
        self.assertEqual(plan['plan'], expected)

    def test_guest_can_read_household_status_and_own_picks(self):
        self.s._profiles['household']['entries'] = []
        self.enterContext(patch.object(self.s, 'explain', return_value={'model': None}))
        self.enterContext(patch.object(self.s, 'week_history', return_value={'weeks': []}))
        self.enterContext(patch.object(self.s, 'get_model', return_value=None))
        self.enterContext(patch.object(self.s, 'active_profiles', return_value=[]))
        for op, fields in (('home_plan', {}), ('row_names', {}), ('lab_status', {}), ('lab_moments', {}),
                           ('week_picks', dict(values=[1, 0])), ('ai_status', {}),
                           ('explain', dict(values=['Invented River'])), ('settings', {}), ('history', {}),
                           ('shelf_list', dict(person='together')), ('taste_drivers', {}), ('home_reach', {}),
                           ('ask', dict(slot='change', question='Which change?')),
                           ('search', dict(kind='mood', values=['invented cozy']))):
            with self.subTest(op=op):
                self.assertTrue(self.guest_plan(op, **fields)['read_only'])

    def test_stale_member_plan_refused_before_apply_writes(self):
        self.enterContext(patch('skald.rowplan.projected_home', return_value={}))
        plans = [self.guest_plan(op, actor='ben', **fields) for op, fields in (
            ('row_rename', dict(values=['foryou', 'Invented picks'])), ('row_lang', dict(values=['en-US'])),
            ('lab_off', {}), ('exclude_genre', dict(values=['Horror'], person='ana')),
            ('shelf_add', dict(values=['invented stories', 'always'], person='together')),
            ('home_more', dict(values=['mood'], person='ana')))]
        self.demote()
        for plan in plans:
            with self.subTest(op=plan.get('change', plan.get('shelf'))):
                self.refused(lambda: c.apply(self.s, plan))

    def test_preview_rechecks_role_from_config_before_saving_shelf_plans(self):
        cfg = c.config(self.s)
        cfg['household']['guests'].append('ben')
        setup.publish(self.data, cfg, replace=True)
        self.refused(lambda: self.guest_plan('shelf_add', actor='ben', person='together',
                                           values=['invented stories', 'always']))

    def test_undo_rechecks_household_entry_and_stale_undo_plan(self):
        c.apply(self.s, self.guest_plan('row_rename', actor='ben', values=['foryou', 'Invented picks']))
        plan = self.guest_plan('undo', actor='ben')
        self.demote()
        self.refused(lambda: self.guest_plan('undo', actor='ben'))
        self.refused(lambda: c.apply(self.s, plan))
        self.refused(lambda: c.undo(self.s, ['ben'], actor='ben'))
        self.refused(lambda: self.guest_plan('redo', actor='ben', values=['Invented picks']))

    def test_plans_corrections_and_targeted_undo_recheck_scope(self):
        steps = [dict(op='hide', kind=None, values=['anime'], person=None),
                 dict(op='row_rename', kind=None, values=['foryou', 'Invented picks'], person=None)]
        self.refused(lambda: self.guest_plan('plan', steps=steps))
        prior = c.preview(self.s, '', 'ana', answer=dict(op='row_rename', values=['foryou', 'Invented picks']))
        self.refused(lambda: c.preview(self.s, '', 'marko', answer=dict(op='correct', values=['Invented picks', 'Other invented picks']), prior=prior))
        c.apply(self.s, self.guest_plan('exclude_genre', values=['Horror']))
        c.apply(self.s, self.guest_plan('undo', values=['Horror']))
        c.apply(self.s, self.guest_plan('redo', values=['Horror']))
        self.assertEqual(c.config(self.s)['taste']['people']['marko']['prefs']['excluded_genres']['movie'], ['Horror'])

    def test_correction_cannot_undo_another_person(self):
        c.apply(self.s, self.guest_plan('exclude_genre', actor='ana', values=['Horror']))
        self.s._commands_last = {'synthetic': ['ana']}
        self.refused(lambda: c.preview(self.s, '', 'marko', scope='synthetic',
                                      answer=dict(op='correct', values=['Horror', 'Thriller'])))

    def test_commander_and_api_refuse_current_and_stale_guest_changes(self):
        answer = dict(op='row_rename', kind=None, values=['foryou', 'Invented picks'], person=None)
        with patch.object(self.s, 'decide', return_value={'answer': answer}), \
                patch.object(c, 'context', return_value=('guestguard', None, 'marko')):
            before = self.stored()
            card = c.special(self.s, 'rename my row', 'movie', routed=True)['metas'][0]
            self.assertIn('Ask a household member to change', card['description'])
            self.assertEqual(self.stored(), before)
            req = dict(viewer='marko', client={'name': 'synthetic'}, body={'in': {'query': 'rename my row'}})
            self.refused(lambda: c.api_preview(self.s, req))
        for api in (False, True):
            plan = self.guest_plan('row_rename', actor='ben', values=['foryou', 'Invented picks'])
            scope = 'api:synthetic' if api else 'guestguard'
            c.pending(self.s)[scope] = (self.s.time.time(), plan)
        self.demote()
        plan = c.get_pending(self.s, 'api:synthetic')
        self.refused(lambda: c.api_apply(self.s, dict(viewer='ben', client={'name': 'synthetic'}, body={'plan': plan['plan']})))
        with patch.object(c, 'context', return_value=('guestguard', None, 'ben')):
            before = self.stored()
            card = c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')['metas'][0]
            self.assertIn('Ask a household member to change', card['description'])
            self.assertEqual(self.stored(), before)

    def test_cli_guest_refusal_and_personal_apply(self):
        out = io.StringIO()
        before = self.stored()
        self.assertEqual(main(['command', 'rename For You to Invented picks', '--person', 'marko', '--yes', '--json'], out=out), 2)
        self.assertEqual(self.stored(), before)
        self.assertEqual(main(['command', 'no horror', '--person', 'marko', '--yes', '--json'], out=io.StringIO()), 0)

    def test_household_cli_selects_a_member_when_first_person_is_a_guest(self):
        self.demote('ana')
        for args in (['rows', 'prefer', '--theme', 'mood', '--more'], ['rows', 'lab', '--off']):
            with self.subTest(args=args):
                out = io.StringIO()
                self.assertEqual(main([*args, '--yes', '--json'], out=out), 0, out.getvalue())

    def test_shelves_api_refuses_together_plan(self):
        plan = self.s.shelf_change('together', 'add', text='invented stories', name='Invented Stories')
        self.refused(lambda: self.s.api_shelves(dict(viewer='marko', body={'plan': plan['plan']})))

    def test_bound_guest_sessions_change_only_their_own_attribution(self):
        before = c.config(self.s)
        self.s.write_session({'who': 'marko'}, 'api', 'marko')
        self.assertEqual(self.s.who_watching()[0], 'marko')
        self.s.write_session({'who': 'off'}, 'api', 'marko')
        self.assertEqual(c.config(self.s), before)
        self.refused(lambda: self.s.write_session({'who': 'both'}, 'api', 'marko'))
        with patch.object(self.s, 'session_at', return_value={'who': 'both'}):
            self.refused(lambda: self.s.write_session({'who': 'marko'}, 'api', 'marko'))
            self.refused(lambda: self.s.write_session({'who': 'off'}, 'api', 'marko'))

    def test_recorded_multilingual_commands_obey_guest_scope(self):
        path = Path(__file__).resolve().parents[1] / 'eval/commands_cases.jsonl'
        cases = [json.loads(line) for line in path.read_text().splitlines()]
        for case in (row for row in cases if row['id'].startswith('w121-')):
            with self.subTest(case=case['id']):
                answer = c.parse_json(case['recorded']['choices'][0]['message']['content'])
                if case['guest_allowed']:
                    plan = c.preview(self.s, case['text'], 'marko', answer=answer)
                    if plan and not plan.get('read_only'):
                        c.apply(self.s, plan)
                else:
                    self.refused(lambda: c.preview(self.s, case['text'], 'marko', answer=answer))

    def test_own_shelf_name_cannot_resolve_to_a_together_shelf(self):
        c.apply(self.s, self.guest_plan('shelf_add', actor='ana', person='together', values=['invented shared', 'always']))
        self.refused(lambda: self.guest_plan('shelf_stop', values=['Invented Shared']))
        self.refused(lambda: self.guest_plan('shelf_filter', values=['Invented Shared'], filters={'runtime': 90}))

    def test_results_and_personal_plan_stay_in_own_scope(self):
        results = {'query': 'invented rivers', 'rows': {'invented': [self.item]}}
        for op in ('like_results', 'shelf_from_results'):
            c.apply(self.s, c.preview(self.s, '', 'marko', 'marko', answer=dict(op=op), results=results))
        steps = [dict(op='exclude_genre', kind=None, values=['Horror'], person=None),
                 dict(op='runtime', kind='movie', values=[90], person=None)]
        c.apply(self.s, self.guest_plan('plan', steps=steps))
        c.undo(self.s, ['marko'], actor='marko')
        self.assertEqual({e['who'] for e in self.s.read_feedback()}, {'marko'})
        self.assertEqual({w['scope'] for w in shelves.read(self.s)['wishes']}, {'marko'})
        self.assertFalse(c.config(self.s)['taste']['people']['ana'].get('prefs'))
        self.assertFalse(c.config(self.s)['taste']['people']['ben'].get('prefs'))
