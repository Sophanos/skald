"""Synthetic household people, guests, shared filters and reversible removal."""
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from test_w92_commands import Household
from decide_kit import ReplayLLM
from skald import commands as c, setup, shelves
from skald.__main__ import main


class People(Household):
    def plan(self, op, person, **fields):
        if op == 'member_create':
            fields.setdefault('label', person.title())
        return c.preview(self.s, 'synthetic people change', 'ana', answer=dict(op=op, person=person, **fields))

    def test_guest_create_preview_limit_and_exact_undo(self):
        before = c.config(self.s)
        plan = self.plan('member_create', 'oskar', guest=True, label='Oskar', age=30)
        self.assertEqual(c.config(self.s), before)
        c.apply(self.s, plan)
        cfg = c.config(self.s)
        self.assertEqual(cfg['household']['guests'], ['oskar'])
        self.assertNotIn('oskar', cfg['household']['member_rows'])
        self.assertEqual(cfg['household']['labels']['pair'], 'you both')
        self.assertEqual(cfg['household']['ages']['oskar'], 30)
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)
        for name in ('kim', 'oskar', 'kim2', 'oskar2'):
            c.apply(self.s, self.plan('member_create', name, guest=True))
        with self.assertRaisesRegex(ValueError, 'household.members'):
            self.plan('member_create', 'ben2', guest=True)

    def test_promote_demote_restore_rows_and_labels(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True, own_row=True))
        before = c.config(self.s)
        c.apply(self.s, self.plan('member_update', 'oskar', guest=False))
        self.assertEqual(c.config(self.s)['household']['labels']['pair'], 'everyone')
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)
        c.apply(self.s, self.plan('member_update', 'ben', guest=True))
        self.assertNotIn('ben', self.s.adult_people())
        c.undo(self.s, ['ana'])
        self.assertIn('ben', self.s.adult_people())

    def test_remove_preview_preserves_files_and_restores_exact_config(self):
        cfg = c.config(self.s)
        cfg['household']['member_rows'] = ['ana', 'ben']
        cfg['household']['trakt_member'] = 'ben'
        cfg['household']['experiences']['ben'] = ['gentle']
        cfg['household']['labels']['pair'] = 'Ana and Ben'
        cfg['taste']['people']['ben'] = {'prefs': {'hide': ['anime']}}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, c.preview(self.s, 'add a shelf with invented heists', 'ben', answer=dict(op='shelf_add', values=['invented heists', 'always'])))
        evidence = self.root / 'imports.jsonl'
        evidence.write_text('{"who":"ben","title":"Invented Heist"}\n')
        before, raw = c.config(self.s), evidence.read_bytes()
        plan = self.plan('member_remove', 'ben')
        for text in ('Invented Heists', 'likes', 'hides', 'Ana and Ben', 'trakt_member', self.s.rid('ben'), 'stay saved'):
            self.assertIn(text.casefold(), plan['description'].casefold())
        c.apply(self.s, plan)
        self.assertEqual(self.s.PEOPLE, ('ana',))
        self.assertEqual(c.config(self.s)['household']['retired'], ['ben'])
        self.assertEqual(c.config(self.s)['household']['trakt_member'], 'ben')
        self.assertEqual(evidence.read_bytes(), raw)
        self.assertTrue(list((self.root / 'backups').glob('pre-people-*')))
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)
        self.assertEqual(evidence.read_bytes(), raw)

    def test_last_adult_kids_and_anonymous_guest_denied(self):
        c.apply(self.s, self.plan('member_update', 'ben', age=12))
        with self.assertRaisesRegex(ValueError, 'last adult'):
            self.plan('member_remove', 'ana')
        for viewer in ('ben', self.s.GUEST):
            with self.subTest(viewer=viewer), self.assertRaises(PermissionError):
                c.preview(self.s, 'x', viewer, answer=dict(op='member_remove', person='ana'))

    def test_stale_apply_and_conflicting_undo_are_atomic(self):
        plan = self.plan('member_remove', 'ben')
        c.apply(self.s, self.plan('member_update', 'ben', label='Kim'))
        before = c.config(self.s)
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)
        c.apply(self.s, self.plan('member_remove', 'ben'))
        cfg = c.config(self.s)
        cfg['household']['labels']['pair'] = 'Synthetic pair'
        setup.publish(self.root, cfg, replace=True)
        with self.assertRaisesRegex(ValueError, 'changed.*cannot undo'):
            c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), cfg)

    def test_guest_taste_does_not_change_household(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True))
        p = {'entries': [dict(key='tmdb:990001', kind='movie', title='Invented Heist', weight=3, by=['oskar'], genres=['Comedy'])]}
        self.assertEqual(self.s.adult_people(), ('ana', 'ben'))
        self.assertEqual(self.s.adult_profile(p)['entries'], [])
        self.assertEqual(self.s.maturity({'nexp': {'oskar': 100}}, 'oskar')['state'], 'guest')
        with patch.object(self.s, '_profiles', {'household': p}), self.s.addon_scope('together'):
            self.assertEqual(self.s.search_profile()['seen'], set())

    def test_shared_shelf_uses_two_people_filters(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True))
        answer = dict(op='shelf_add', values=['invented heists', 'always'], with_person='oskar')
        c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=answer))
        wish = shelves.read(self.s)['wishes'][-1]
        self.assertEqual((wish['scope'], wish['with_person']), ('ana', 'oskar'))
        self.assertIn(self.s.rid('shelf_ana_1'), [r[0] for r in shelves.slots(self.s, 'ana')])
        self.assertNotIn(self.s.rid('shelf_ana_1'), [r[0] for r in shelves.slots(self.s, 'ben')])
        metas = [dict(id='tmdb:990001', type='movie'), dict(id='tmdb:990002', type='movie'), dict(id='tmdb:990003', type='movie')]
        key = (self.s.rid('shelfraw_ana_1'), self.s.LANG)
        self.s._feeds[key] = dict(wish=wish['id'], window='always', candidates=metas, status='pinned')
        def profile():
            who = getattr(self.s._scoping, 'scope', None)
            return {'seen': {'tmdb:990001'} if who == 'oskar' else set(), 'negative': {'tmdb:990002'} if who == 'ana' else set()}
        with patch.object(self.s, 'search_profile', side_effect=profile), patch.object(self.s, 'preference_ok', side_effect=lambda x, cid, who: who != 'oskar' or x['id'] != 'tmdb:990003'):
            self.assertEqual(shelves.fill(self.s, self.s.LANG, build=False)[self.s.rid('shelf_ana_1')]['tv'], [])
        c.undo(self.s, ['ana'])
        self.assertEqual(shelves.read(self.s)['wishes'], [])

    def test_cli_list_preview_apply_and_undo_json(self):
        def run(*args):
            out = io.StringIO()
            self.assertEqual(main(['members', *args, '--json'], out=out), 0, out.getvalue())
            return json.loads(out.getvalue())
        preview = run('add', 'oskar', '--guest', '--age', '30', '--person', 'ana')
        self.assertNotIn('oskar', c.config(self.s)['household']['members'])
        run('add', 'oskar', '--guest', '--age', '30', '--person', 'ana', '--plan', preview['plan'])
        rows = run('list')['people']
        self.assertEqual(rows[-1], dict(id='oskar', name='Oskar', age=30, guest=True, own_row=False, shelves=0))
        run('remove', 'oskar', '--person', 'ana', '--yes')
        run('--undo', '--person', 'ana')
        self.assertIn('oskar', c.config(self.s)['household']['members'])

    def test_retired_row_ids_return_empty_after_reload(self):
        c.apply(self.s, self.plan('member_remove', 'ben'))
        with setup.server(self.root) as s:
            handler = SimpleNamespace(send_json=lambda status, body, **kw: (status, body))
            for cid in (s.rid('ben'), s.rid('shelf_ben_1')):
                self.assertEqual(s.Handler.addon_get(handler, ['en-US', 'catalog', 'tv', cid+'.json'], 'en-US', None),
                                 (200, {'metas': []}))
                self.assertFalse(s.row_enabled(cid))

    def test_guest_feedback_masks_and_hashes_are_isolated(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True))
        s = self.s
        before = s.taste_hash('household', [])
        with patch.object(s, 'attribute', return_value=('oskar', ['oskar'], {})):
            self.assertEqual(s.taste_hash('household', [dict(id='tmdb:990003', type='movie', played=True)]), before)
        s.write_feedback(dict(who='oskar', key='tmdb:990001', kind='movie', title='Invented Heist', action='not_for_us'))
        with s.addon_scope('ana'):
            self.assertNotIn('tmdb:990001', s.remote_masks()[0])
        with s.addon_scope('oskar'):
            self.assertIn('tmdb:990001', s.remote_masks()[0])
        self.assertEqual(s.taste_hash('household', []), before)
        p = s.build_profile([], [], remote=True)
        self.assertNotIn('tmdb:990001', p['seen'])
        self.assertNotIn('tmdb:990001', p['negative'])
        both = dict(id='a'*32, t=1, who='both', key='tmdb:990002', action='love')
        self.assertNotIn(('tmdb:990002', 'oskar'), s.fold_feedback([both])[0])

    def test_llm_decides_guest_shared_scope_pronouns_and_undo(self):
        s = self.s
        cases = [
            ('Add my best friend Oskar as a guest', dict(op='member_create', person='oskar', label='Oskar', guest=True)),
            ('Add a catalog for Oskar and me with invented heists', dict(op='shelf_add', values=['invented heists', 'always'], with_person='oskar')),
            ('Make him a full member', dict(op='member_update', person='oskar', guest=False)),
            ('Undo that', dict(op='undo')),
        ]
        with ReplayLLM(s) as llm:
            for text, answer in cases:
                llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
                plan = c.preview(s, text, 'ana')
                self.assertIsNotNone(plan)
                c.apply(s, plan)
            self.assertEqual(llm.calls, 4)
        self.assertIn('oskar', c.config(s)['household']['guests'])

    def test_remove_self_is_refused(self):
        before = c.config(self.s)
        with self.assertRaisesRegex(ValueError, 'last adult|[Aa]sk another adult'):
            c.preview(self.s, 'remove me', 'ana', answer=dict(op='member_remove', person='ana'))
        self.assertEqual(c.config(self.s), before)

    def test_removed_evidence_survives_reads_and_new_writes(self):
        s = self.s
        s.write_feedback(dict(who='ben', key='tmdb:990001', kind='movie', title='Invented Heist', action='love'))
        event = dict(who='ben', key='tmdb:990002', kind='movie', title='Invented River', action='like', src='plain', conf=1)
        s.write_imports([event])
        c.apply(s, self.plan('member_remove', 'ben'))
        self.assertEqual(len(s.read_feedback()), 1)
        self.assertEqual(len(s.read_imports()), 1)
        s.write_feedback(dict(who='ana', key='tmdb:990003', kind='movie', title='Invented Night', action='love'))
        s.write_imports([{**event, 'who': 'ana', 'key': 'tmdb:990004'}])
        self.assertEqual(len(s.read_feedback()), 2)
        self.assertEqual(len(s.read_imports()), 2)
        c.undo(s, ['ana'])
        self.assertEqual(len(s.read_feedback()), 2)
        self.assertEqual(len(s.read_imports()), 2)

    def test_connector_owner_retention_and_unrelated_change_undo(self):
        cfg = c.config(self.s)
        cfg['connectors']['jellyfin'] = dict(enabled=False, kind='player', auth='file:jellyfin.json', options={'url': 'http://example.invalid', 'users': {'synthetic-user': 'ben'}})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        plan = self.plan('member_remove', 'ben')
        self.assertIn('jellyfin', plan['description'])
        c.apply(self.s, plan)
        self.assertEqual(c.config(self.s)['connectors']['jellyfin']['options']['users'], {'synthetic-user': 'ben'})
        changed = c.config(self.s)
        changed['household']['note'] = 'Unrelated synthetic change'
        setup.publish(self.root, changed, replace=True)
        c.undo(self.s, ['ana'])
        cfg['household']['note'] = 'Unrelated synthetic change'
        self.assertEqual(c.config(self.s), cfg)

    def test_shared_shelf_keeps_the_guest_child_filter(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True, age=12))
        c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=dict(op='shelf_add', values=['invented heists', 'always'], with_person='oskar')))
        wish = shelves.read(self.s)['wishes'][-1]
        self.s._feeds[self.s.rid('shelfraw_ana_1'), self.s.LANG] = dict(wish=wish['id'], window='always', status='pinned', candidates=[dict(id='tmdb:990001', type='movie')])
        with patch.object(self.s, 'search_profile', return_value={}), patch.object(self.s, 'preference_ok', side_effect=lambda x, cid, who: self.s.kids_age(self.s.safety_subject(who)) is None):
            self.assertEqual(shelves.fill(self.s, self.s.LANG, build=False)[self.s.rid('shelf_ana_1')]['tv'], [])

    def test_remove_shared_shelf_retires_owner_id_and_refuses_stale_shelves(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True))
        c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=dict(op='shelf_add', values=['invented heists', 'always'], with_person='oskar')))
        plan = self.plan('member_remove', 'oskar')
        self.assertIn(self.s.rid('shelf_ana_1'), plan['description'])
        c.apply(self.s, c.preview(self.s, 'add a shelf with invented rivers', 'oskar', answer=dict(op='shelf_add', values=['invented rivers', 'always'])))
        before = c.config(self.s)
        with self.assertRaisesRegex(ValueError, 'Shelves changed'):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)
        c.apply(self.s, self.plan('member_remove', 'oskar'))
        self.assertIn(self.s.rid('shelf_ana_1'), c.config(self.s)['household']['retired_rows'])

    def test_guest_filters_stay_personal_and_refresh_without_taste_changes(self):
        c.apply(self.s, self.plan('member_create', 'oskar', guest=True))
        c.apply(self.s, c.preview(self.s, 'hide anime', 'oskar', answer=dict(op='hide', values=['anime'])))
        self.assertNotIn({'hide': ['anime']}, self.s.row_preferences(self.s.rid('foryou'), 'both')[1])
        s = self.s
        with patch.object(s, 'read_history', return_value=[]), patch.object(s, 'read_trakt', return_value=[]), patch.object(s, 'learn_from_plays'), patch.object(s, 'log_play_context'):
            s.refresh_profiles()
            before = s._profiles['household']['hash']
            s.write_feedback(dict(who='oskar', key='tmdb:990001', kind='movie', title='Invented Heist', action='seen'))
            s.refresh_profiles()
            self.assertEqual(s._profiles['household']['hash'], before)
            with s.addon_scope('oskar'):
                self.assertIn('tmdb:990001', s.search_profile()['seen'])

    def test_guest_reactions_leave_scores_reach_and_calibration_unchanged(self):
        s = self.s
        for i in range(990001, 990222):
            s._facets['tmdb:'+str(i)] = dict(title='Invented Heist '+str(i), kind='movie', g=['Comedy'], x={'cozy_case': 2}, k=[[1, 'Invented theme']])
        s.write_feedback(dict(who='ana', key='tmdb:990001', kind='movie', title='Invented Heist', action='love'))
        with patch.object(s, 'ensure_facets'), patch.object(s, 'ensure_emb'):
            before = s.build_profile([], [], remote=True)
            model = s.build_model(before, enrich=False)
            c.apply(s, self.plan('member_create', 'oskar', guest=True))
            s.write_feedback(dict(who='oskar', key='tmdb:990002', kind='movie', title='Invented River', action='not_for_us'))
            after = s.build_profile([], [], remote=True)
            changed = s.build_model(after, enrich=False)
        for field in ('aff', 'x', 'bg', 'cal', 'cal_reference', 'taste_line'):
            self.assertEqual(changed[field], model[field], field)
        for person in (None, 'ana', 'ben', 'together'):
            self.assertEqual(s.score_key(changed, 'tmdb:990003', person, 'movie'), s.score_key(model, 'tmdb:990003', person, 'movie'))
            self.assertEqual(s.known_rank(changed, after, person), s.known_rank(model, before, person))

    def test_pending_person_context_reaches_the_model(self):
        first = c.preview(self.s, 'Remove Ben from the household', 'ana', answer=dict(op='member_remove', person='ben'))
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': '{"op":"member_remove","person":"ben"}'}}]})
            plan = c.preview(self.s, 'Remove him', 'ana', prior=first)
            self.assertIn('Last search: Remove Ben from the household', llm.bodies[0]['messages'][1]['content'])
            self.assertEqual(plan['member']['name'], 'ben')
