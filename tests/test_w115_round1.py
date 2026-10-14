"""People review regressions, using synthetic state and offline transports."""
import base64
import copy
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import types
from unittest.mock import patch

from decide_kit import close_server, load_server, ReplayLLM
from test_w92_commands import Household
from skald import aio_refresh, commands as c, setup, shelves
from skald.__main__ import main


class RoundOne(Household):
    def plan(self, op, person, actor='ana', **fields):
        if op == 'member_create':
            fields.setdefault('label', person.title())
        return c.preview(self.s, '', actor, answer=dict(op=op, person=person, **fields))

    def add_guest(self):
        c.apply(self.s, self.plan('member_create', 'kim', guest=True))

    def test_retired_clients_are_preserved_and_refused_individually(self):
        tokens = {p: self.s.new_client(p, ['read'], p) for p in ('ana', 'ben')}
        raw = (self.root / 'clients.json').read_bytes()
        def auth(s, p):
            return s._api_auth(SimpleNamespace(headers={'Authorization': 'Bearer '+tokens[p]}))
        self.assertIsNone(auth(self.s, 'ben')[1])
        c.apply(self.s, self.plan('member_remove', 'ben'))
        self.assertEqual(auth(self.s, 'ben')[1], 401)
        with setup.server(self.root) as s:
            self.assertIsNone(auth(s, 'ana')[1])
            self.assertEqual(auth(s, 'ben')[1], 401)
        self.assertEqual((self.root / 'clients.json').read_bytes(), raw)
        c.undo(self.s, ['ana'])
        self.assertIsNone(auth(self.s, 'ben')[1])

    def test_auth_reloads_retirement_before_using_cached_tokens(self):
        token = self.s.new_client('ben', ['read'], 'ben')
        handler = SimpleNamespace(headers={'Authorization': 'Bearer '+token})
        self.assertIsNone(self.s._api_auth(handler)[1])
        cfg = c.config(self.s)
        cfg['household']['retired'] = ['ben']
        cfg['household']['retired_rows'] = [self.s.rid('ben')]
        setup.publish(self.root, cfg, replace=True)
        self.assertEqual(self.s._api_auth(handler)[1], 401)

    def test_retired_shelf_journal_survives_later_writes(self):
        self.s.shelf_change('ben', 'add', text='genre:comedy', name='Invented Ben', apply=True)
        before = (self.root / 'undo.jsonl').read_bytes()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        self.s.shelf_change('ana', 'add', text='genre:comedy', name='Invented Ana', apply=True)
        self.assertIn(before, (self.root / 'undo.jsonl').read_bytes())

    def test_retired_state_survives_restart_save_and_undo(self):
        self.removal_roundtrip('ben')

    def test_retired_shared_state_survives_restart_save_and_undo(self):
        self.removal_roundtrip('marko')

    def removal_roundtrip(self, person):
        self.s.shelf_change('ben', 'add', text='genre:comedy', name='Invented Ben', apply=True)
        c.apply(self.s, self.plan('member_create', 'marko', guest=True))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person='marko')))
        for wish in shelves.read(self.s)['wishes']:
            for stem in ('shelf', 'shelfraw'):
                self.s._feeds[self.s.rid(f"{stem}_{wish['scope']}_{wish['slot']}"), self.s.LANG] = dict(
                    wish=wish['id'], window='always', status='pinned', candidates=[dict(id='tmdb:990001', type='movie')])
        self.s.save_feeds()
        self.s.new_client('ben', ['read'], 'ben')
        (self.root / 'credentials').mkdir(exist_ok=True)
        (self.root / 'credentials/aiostreams.json').write_text(json.dumps({'ben': {'uuid': 'synthetic-ben', 'password': 'synthetic'}}))
        (self.root / 'catalog-modifications.json').write_text(json.dumps({'catalogModifications': [
            dict(id='synthetic.'+self.s.rid('shelf_ben_1'), type='tv', enabled=False, name='Invented Ben')]}))
        self.s.load_state()
        self.s.read_taste()
        def snapshot():
            return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before = snapshot()
        c.apply(self.s, self.plan('member_remove', person))
        with setup.server(self.root) as s:
            s.load_state()
            with patch.object(s, 'read_history', return_value=[]), patch.object(s, 'read_trakt', return_value=[]), patch.object(s, 'learn_from_plays'), patch.object(s, 'log_play_context'):
                s.refresh_profiles()
            s.save_feeds()
            c.undo(s, ['ana'])
        after = snapshot()
        # Removal keeps its backup and an undone command, the only added audit state.
        added = after.keys() - before.keys()
        self.assertTrue(all(p.startswith('backups/pre-people-') for p in added), added)
        old_commands, new_commands = json.loads(before.pop('commands.json')), json.loads(after.pop('commands.json'))
        removed = new_commands['ana'].pop()
        self.assertTrue(removed['undone'])
        self.assertEqual(old_commands, new_commands)
        for name, raw in before.items():
            if name == 'skald.json':
                self.assertEqual(json.loads(after[name]), json.loads(raw), name)
            else:
                self.assertEqual(after[name], raw, name)

    def test_catalog_modifications_for_retired_shared_shelf_stay_exact(self):
        c.apply(self.s, self.plan('member_create', 'marko', guest=True))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person='marko')))
        cid = 'synthetic.'+self.s.rid('shelf_ana_1')
        cfg = {'catalogModifications': [dict(id=cid, type='tv', enabled=False, name='Invented private label')]}
        manifest = {'catalogs': [dict(id='synthetic.'+self.s.rid('search'))]}
        c.apply(self.s, self.plan('member_remove', 'marko'))
        setup.keep_catalog_places(self.s, cfg, manifest, 'ana')
        self.assertIn(dict(id=cid, type='tv', enabled=False, name='Invented private label'), cfg['catalogModifications'])

    def test_refresh_skips_retired_login_and_continues(self):
        (self.root / 'setup.json').write_text(json.dumps({'aio_url': 'https://synthetic.invalid'}))
        (self.root / 'credentials').mkdir(exist_ok=True)
        logins = {p: dict(uuid=p, password='synthetic') for p in ('ben', 'ana')}
        path = self.root / 'credentials/aiostreams.json'
        path.write_text(json.dumps(logins))
        raw, called = path.read_bytes(), []
        def transport(req, timeout):
            person = base64.b64decode(req.get_header('Authorization').split()[1]).decode().split(':')[0]
            called.append(person)
            return io.BytesIO(json.dumps({'success': True, 'data': {'userData': {}}}).encode())
        c.apply(self.s, self.plan('member_remove', 'ben'))
        result = aio_refresh.refresh(self.root, transport=transport, server=self.s)
        self.assertEqual(called, ['ana', 'ana'])
        self.assertIn('(1 logins)', result)
        self.assertEqual(path.read_bytes(), raw)

    def test_guest_equal_reactions_do_not_change_member_weight_or_age(self):
        self.add_guest()
        s, now = self.s, self.s.time.time()
        with patch.object(s.time, 'time', return_value=now-200*s.DAY):
            s.write_feedback(dict(who='ana', key='tmdb:990001', kind='movie', title='Invented River', action='like'))
        with patch.object(s.time, 'time', return_value=now):
            before = s.adult_profile(s.build_profile([], []))
            s.write_feedback(dict(who='kim', key='tmdb:990001', kind='movie', title='Invented River', action='like'))
            after = s.adult_profile(s.build_profile([], []))
        self.assertEqual(after, before)
        self.assertEqual([(e['weight'], e['age']) for e in after['entries']], [(0.5, 200)])

    def test_guest_plays_have_an_independent_history_cap(self):
        self.add_guest()
        s = self.s
        def item(person, n):
            return dict(id='synthetic-'+str(n), type='movie', profile=person, played=True)
        member = item('ana', 990001)
        guests = [item('kim', n) for n in range(990002, 990042)]
        with patch.object(s, 'attribute', side_effect=lambda it: (it['profile'], [it['profile']], {})), patch.object(s, 'tmdb_details', side_effect=lambda it: ('movie', dict(id=int(it['id'].split('-')[1]), title='Invented River', genres=[]))):
            events = list(s.tv_events(s.time.time(), [*guests, member]))
            self.assertIn('ana', [e['who'] for e in events])
            self.assertEqual(s.attrib_hash([*guests, member]), s.attrib_hash([member]))

    def test_household_hash_ignores_all_guest_sources(self):
        self.add_guest()
        s = self.s
        before = s.taste_hash('household', [])
        cfg = c.config(s)
        cfg['taste']['people']['kim']['loves'] = ['Invented River (2025)']
        setup.publish(self.root, cfg, replace=True)
        s.refresh_preferences()
        self.assertEqual(s.taste_hash('household', []), before, 'configured loves')
        for source in ('stremio', 'pmdb'):
            with self.subTest(source=source), patch.dict(s.CONFIG['connectors'], {source: {'accounts': {'kim': {}}}}), patch.object(s, '_drop_read', side_effect=lambda name: [dict(id='tmdb:990001', type='movie', at=1)] if name == source+'-kim' else []):
                self.assertEqual(s.taste_hash('household', []), before, source)
        with patch.object(s, '_taste_accounts', return_value={'kim': {}}), patch.object(s, 'read_trakt', return_value=[dict(person='kim', id='tmdb:990001')]):
            self.assertEqual(s.taste_hash('household', []), before, 'trakt')
        (self.root / 'history-summary.json').write_text(json.dumps({'v': 1, 'items': [dict(origin='history', who='kim', by=['kim'], key='tmdb:990001')]}))
        self.assertEqual(s.taste_hash('household', []), before, 'history summaries')

    def test_guest_plays_do_not_change_episode_feed_hash(self):
        self.add_guest()
        s = self.s
        before = s.episodes_hash([])
        guest = dict(id='tmdb:990001', type='series', profile='kim', at=1, last_episode=[1, 2])
        with patch.object(s, 'attribute', return_value=('kim', ['kim'], {})):
            self.assertEqual(s.episodes_hash([guest]), before)
        for source in ('stremio', 'pmdb'):
            with self.subTest(source=source), patch.dict(s.CONFIG['connectors'], {source: {'accounts': {'kim': {}}}}), patch.object(s, '_drop_read', return_value=[guest]):
                self.assertEqual(s.episodes_hash([]), before)

    def test_retired_player_and_loves_are_not_household_inputs(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ben']['loves'] = ['Invented River|2025|m']
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        with patch.dict(self.s.CONFIG['history'], source='jellyfin'):
            self.assertEqual(self.s.attribute(dict(profile='ben', id='tmdb:990001', type='movie'))[0], 'ben')
        self.assertEqual(self.s.person_loves(), [])

    def test_retired_command_journal_does_not_expire_on_later_writes(self):
        c.apply(self.s, c.preview(self.s, '', 'ben', answer=dict(op='hide', values=['anime'])))
        path = self.root / 'commands.json'
        entries = json.loads(path.read_text())
        entries['ben'][0]['t'] = int(self.s.time.time()-200*self.s.DAY)
        path.write_text(json.dumps(entries))
        c.apply(self.s, self.plan('member_remove', 'ben'))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='hide', values=['anime'])))
        self.assertEqual(json.loads(path.read_text())['ben'], entries['ben'])

    def test_apply_rechecks_guest_actor(self):
        c.apply(self.s, self.plan('member_create', 'kim'))
        c.apply(self.s, self.plan('member_create', 'marko'))
        plan = self.plan('member_remove', 'ben')
        c.apply(self.s, self.plan('member_update', 'ana', actor='marko', guest=True))
        before = c.config(self.s)
        with self.assertRaises(PermissionError):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)

    def test_apply_rechecks_child_actor_and_current_limit(self):
        c.apply(self.s, self.plan('member_create', 'kim'))
        plan = self.plan('member_remove', 'ben')
        c.apply(self.s, self.plan('member_update', 'ana', actor='kim', age=12))
        before = c.config(self.s)
        with self.assertRaises(PermissionError):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)
        with patch.object(self.s, 'MEMBER_COUNTS', (1, 2, 3)):
            with self.assertRaisesRegex(ValueError, 'household.members'):
                self.plan('member_create', 'marko', actor='kim')

    def test_apply_rechecks_limit_after_a_concurrent_addition(self):
        with patch.object(self.s, 'MEMBER_COUNTS', (1, 2, 3)):
            plan = self.plan('member_create', 'kim')
            c.apply(self.s, self.plan('member_create', 'marko'))
            before = c.config(self.s)
            with self.assertRaisesRegex(ValueError, 'household.members'):
                c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)

    def test_apply_rechecks_last_adult_after_self_demotion_preview(self):
        plan = self.plan('member_update', 'ana', guest=True)
        c.apply(self.s, self.plan('member_update', 'ben', actor='ben', guest=True))
        before = c.config(self.s)
        with self.assertRaisesRegex(ValueError, 'last adult'):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)
    def test_member_rows_preview_apply_undo_and_cli(self):
        self.add_guest()
        before = c.config(self.s)
        plan = self.plan('member_rows', 'kim', own_row=True)
        self.assertEqual(c.config(self.s), before)
        self.assertIn('own Home row', plan['name'])
        c.apply(self.s, plan)
        self.assertIn('kim', c.config(self.s)['household']['member_rows'])
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)
        out = io.StringIO()
        self.assertEqual(main(['members', 'rows', 'kim', 'on', '--person', 'ana', '--yes', '--json'], out=out), 0, out.getvalue())
        self.assertIn('kim', c.config(self.s)['household']['member_rows'])
        c.apply(self.s, self.plan('member_rows', 'kim', own_row=False))
        self.assertNotIn('kim', c.config(self.s)['household']['member_rows'])

    def test_member_rows_search_yes_and_undo(self):
        self.add_guest()
        before = c.config(self.s)
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(dict(op='member_rows', person='kim', own_row=True))}}]})
            c.special(self.s, 'Give Kim her own row', 'movie', routed=True)
        plan = c.get_pending(self.s, 'w92')
        self.assertEqual(c.config(self.s), before)
        c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')
        self.assertIn('kim', c.config(self.s)['household']['member_rows'])
        c.special(self.s, 'undo', 'movie')
        self.assertEqual(c.config(self.s), before)

    def test_retired_guest_session_cannot_mask_household_rows(self):
        self.add_guest()
        self.s.write_feedback(dict(who='kim', key='tmdb:990001', kind='movie', title='Invented River', action='not_for_us'))
        c.apply(self.s, self.plan('member_remove', 'kim'))
        with patch.object(self.s, 'who_watching', return_value=('kim', 'phone')):
            self.assertEqual(self.s.remote_masks(), (set(), set()))
        with patch.object(self.s, 'read_history', return_value=[]), patch.object(self.s, 'read_trakt', return_value=[]), patch.object(self.s, 'learn_from_plays'), patch.object(self.s, 'log_play_context'):
            self.s.refresh_profiles()
        self.assertNotIn('guest_stamp', self.s._profiles['household'])

    def test_unresolved_people_ask_with_candidates_without_writes(self):
        self.add_guest()
        c.apply(self.s, self.plan('member_create', 'marko', guest=True))
        for op, person, fields in [('member_create', 'her', {'guest': True}), ('member_create', None, {'label': 'Kim'}), ('member_remove', 'her', {}), ('member_rows', 'her', {'own_row': False}), ('member_update', 'unknown', {'label': 'Kim'})]:
            with self.subTest(op=op, person=person):
                before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name not in ('usage.json', 'decisions.jsonl')}
                with ReplayLLM(self.s) as llm:
                    llm.arm({'choices': [{'message': {'content': json.dumps(dict(op=op, person=person, **fields))}}]})
                    plan = c.preview(self.s, 'synthetic ambiguous '+op+' '+str(person), 'ana', prior={'text': 'Kim and Marko', 'member': {'name': 'kim'}})
                self.assertTrue(plan.get('read_only'), plan)
                self.assertIn('Kim', plan['name'])
                self.assertIn('Marko', plan['name'])
                self.assertTrue(plan['name'].endswith('?'))
                c.apply(self.s, plan)
                for path, raw in before.items():
                    self.assertEqual(Path(path).read_bytes(), raw)

    def test_model_person_question_lists_current_candidates(self):
        self.add_guest()
        c.apply(self.s, self.plan('member_create', 'marko', guest=True))
        plan = c.preview(self.s, '', 'ana', answer=dict(op='ask', slot='person', question='Which person do you mean?'))
        self.assertTrue(plan['read_only'])
        self.assertIn('Kim', plan['name'])
        self.assertIn('Marko', plan['name'])

    def test_local_api_creation_keeps_the_explicit_new_name(self):
        result = c.api_preview(self.s, dict(body={'in': {'query': 'Add a household member named Kim, age 12'}, 'no_llm': True}, viewer='ana', client={'name': 'ana'}))
        plan = result['preview']
        self.assertFalse(plan.get('read_only'), plan)
        c.apply(self.s, plan)
        self.assertIn('kim', self.s.PEOPLE)

    def test_current_display_names_and_ambiguous_labels(self):
        c.apply(self.s, self.plan('member_update', 'ben', label='Kim Jones'))
        plan = self.plan('member_remove', 'Kim Jones')
        self.assertEqual(plan['member']['name'], 'ben')
        self.assertEqual(self.plan('member_rows', 'Kim Jones', own_row=True)['member']['name'], 'ben')
        c.apply(self.s, self.plan('member_create', 'kim', label='Kim Jones', guest=True))
        before = c.config(self.s)
        plan = self.plan('member_remove', 'Kim Jones')
        self.assertTrue(plan['read_only'])
        c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)

    def test_shared_shelf_applies_both_tonight_masks(self):
        self.add_guest()
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person='kim')))
        wish = shelves.read(self.s)['wishes'][-1]
        metas = [dict(id='tmdb:'+str(n), type='movie') for n in (990001, 990002, 990003)]
        self.s._feeds[self.s.rid('shelfraw_ana_1'), self.s.LANG] = dict(wish=wish['id'], window='always', candidates=metas, status='pinned')
        for who, n in [('ana', 990002), ('kim', 990001)]:
            self.s.write_feedback(dict(who=who, key='tmdb:'+str(n), kind='movie', title='Invented River', action='not_tonight'))
        with patch.object(self.s, 'search_profile', return_value={}), patch.object(self.s, 'preference_ok', return_value=True), patch.object(self.s, 'member_scores', return_value={}):
            result = shelves.fill(self.s, self.s.LANG, build=False)[self.s.rid('shelf_ana_1')]['tv']
        self.assertEqual([m['id'] for m in result], ['tmdb:990003', 'tmdb:990001', 'tmdb:990002'])

    def test_old_pending_member_preview_requests_new_preview(self):
        plan = self.plan('member_remove', 'ben')
        plan['member'].pop('config_after')
        before = c.config(self.s)
        with self.assertRaisesRegex(ValueError, 'preview again'):
            c.apply(self.s, plan)
        self.assertEqual(c.config(self.s), before)

    def test_guest_connector_and_told_signals_never_merge_member_evidence(self):
        self.add_guest()
        s, now = self.s, self.s.time.time()
        member = s._event(now-200*s.DAY, 'synthetic-ana', 'tmdb:990001', 'movie', 'Invented River', '', 'ana', ['ana'], 'like', 'remote', {})
        guest = s._event(now, 'synthetic-kim', 'tmdb:990001', 'movie', 'Invented River', '', 'kim', ['kim'], 'play', 'tv', dict(item={}, genres=[], flags=[], signal='watched'))
        with patch.object(s.time, 'time', return_value=now), patch.object(s, 'event_stages', return_value=[('tv', []), ('remote', [member])]):
            before = s.adult_profile(s.build_profile([], []))
        with patch.object(s.time, 'time', return_value=now), patch.object(s, 'event_stages', return_value=[('tv', [guest]), ('remote', [member])]):
            after = s.adult_profile(s.build_profile([], []))
        self.assertEqual(after, before)
        member = s._event(now-200*s.DAY, 'synthetic-ana', 'tmdb:990001', 'movie', 'Invented River', '', 'ana', ['ana'], 'play', 'tv', dict(item={}, genres=[], flags=[], signal='watched'))
        item = dict(id='tmdb:990001', type='movie', title='Invented River', person='kim', signal='loved', weight=8, at=now*1000)
        guest = s._event(now, 'synthetic-kim', 'tmdb:990001', 'movie', 'Invented River', '', 'kim', ['kim'], 'love', 'trakt', dict(item=item, genres=[], flags=[], signal='loved'))
        with patch.object(s.time, 'time', return_value=now), patch.object(s, '_taste_accounts', return_value={'kim': {}}):
            with patch.object(s, 'event_stages', return_value=[('tv', [member])]):
                before = s.adult_profile(s.build_profile([], []))
            with patch.object(s, 'event_stages', return_value=[('tv', [member]), ('trakt', [guest])]):
                after = s.adult_profile(s.build_profile([], []))
        self.assertEqual(after, before)

    def test_df66a3f_loads_retirement_and_unchanged_client_shape(self):
        self.add_guest()
        c.apply(self.s, self.plan('member_update', 'kim', age=30))
        c.apply(self.s, self.plan('member_rows', 'kim', own_row=True))
        c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person='kim')))
        self.s.new_client('kim', ['read'], 'kim')
        self.s.memory_tell('kim', 'like', 'x:found_family', names={'x:found_family': 'found family'})
        memory = self.s.memory_load()
        memory['forgotten'] = {'kim': 0}
        self.s.memory_save(memory, [])
        raw = (self.root / 'memory.json').read_bytes()
        c.apply(self.s, self.plan('member_remove', 'kim'))
        root = Path(__file__).resolve().parents[1]
        source = subprocess.run(['git', 'show', 'df66a3f:server.py'], cwd=root, check=True, capture_output=True, text=True).stdout
        old = types.ModuleType('rollback_server')
        old.__file__, old._CONFIG_READONLY = str(root / 'server.py'), True
        exec(compile(source, old.__file__, 'exec'), old.__dict__)
        self.addCleanup(close_server, old)
        self.assertIsNone(old.CONFIG_ERROR)
        self.assertEqual(old._clients_read(), self.s._clients_read())
        old.load_state()
        self.assertEqual(old.memory_load(), memory)
        self.assertEqual((self.root / 'memory.json').read_bytes(), raw)
        self.assertIn('kim', old.PEOPLE)
        self.assertIn('kim', old.adult_people())
        shelf_source = subprocess.run(['git', 'show', 'df66a3f:skald/shelves.py'], cwd=root, check=True, capture_output=True, text=True).stdout
        old_shelves = types.ModuleType('skald.rollback_shelves')
        old_shelves.__package__ = 'skald'
        exec(compile(shelf_source, 'rollback_shelves.py', 'exec'), old_shelves.__dict__)
        self.assertEqual(old_shelves.read(old), shelves.read(self.s))
