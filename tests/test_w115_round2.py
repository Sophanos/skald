"""Last people review regressions, with synthetic state and offline transports."""
import copy
import datetime as dt
import io
import json
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from decide_kit import ReplayLLM
from test_w92_commands import Household
from skald import backup, commands as c, members, metrics, setup
from skald.__main__ import parser
from skald.engine import Engine


class RoundTwo(Household):
    def plan(self, op, person, actor='ana', **fields):
        if op == 'member_create':
            fields.setdefault('label', person.title())
        return c.preview(self.s, '', actor, answer=dict(op=op, person=person, **fields))

    def guest(self):
        c.apply(self.s, self.plan('member_create', 'kim', guest=True))

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_retired_memory_survives_restart_and_undo(self):
        s = self.s
        names = {'x:found_family': 'found family'}
        facts = [s.memory_tell(p, 'like', 'x:found_family', names=names) for p in ('ana', 'ben')]
        store = s.memory_load()
        store['forgotten'] = {'ben': 0}
        s.memory_save(store, [])
        raw = (self.root / 'memory.json').read_bytes()
        c.apply(s, self.plan('member_remove', 'ben'))
        with setup.server(self.root) as restarted:
            self.assertEqual(restarted.memory_load(), store)
            self.assertFalse((self.root / 'memory.json.bad').exists())
            self.assertEqual([f['id'] for f in restarted.memory_view()], [facts[0]['id']])
            with patch.object(restarted, 'frec', return_value={'x': {'found_family': 2}}), patch.object(restarted, 'get_model', return_value={'names': names}):
                self.assertEqual(restarted.memory_why('tmdb:990001', 'movie', 'ana')['fact'], facts[0]['id'])
                self.assertIsNone(restarted.memory_why('tmdb:990001', 'movie', 'ben'))
            c.undo(restarted, ['ana'])
            self.assertEqual(restarted.memory_load(), store)
            self.assertEqual([f['id'] for f in restarted.memory_view()], [f['id'] for f in facts])
        self.assertEqual((self.root / 'memory.json').read_bytes(), raw)

    def test_memory_why_refreshes_active_index_on_retirement(self):
        s = self.s
        names = {'x:found_family': 'found family'}
        for p in ('ana', 'ben'):
            s.memory_tell(p, 'like', 'x:found_family', names=names)
        with patch.object(s, 'frec', return_value={'x': {'found_family': 2}}), patch.object(s, 'get_model', return_value={'names': names}):
            self.assertIsNotNone(s.memory_why('tmdb:990001', 'movie', 'together'))
            c.apply(s, self.plan('member_remove', 'ben'))
            self.assertIsNotNone(s.memory_why('tmdb:990001', 'movie', 'ana'))
            self.assertEqual(set(s._memory_index['people']), {'ana'})

    def test_retired_memory_is_not_decayed_by_curation(self):
        s, now = self.s, self.s.time.time()
        s.memory_tell('ben', 'like', 'x:found_family', names={'x:found_family': 'found family'}, now=now)
        store = s.memory_load()
        store['facts'][0].update(src='implicit', strength=.01)
        s.memory_save(store, [])
        c.apply(s, self.plan('member_remove', 'ben'))
        result = s.curate([], {'paff': {'ana': {}}, 'names': {}}, now=now+s.DAY, dry_run=True)
        self.assertEqual(result['store']['facts'], store['facts'])

    def test_retirement_invalidates_cached_memory_vetoes(self):
        s, now = self.s, self.s.time.time()
        s.memory_tell('ben', 'avoid', 'x:found_family', names={'x:found_family': 'found family'}, now=now)
        with patch.object(s, '_memory_breadth', return_value={}):
            self.assertEqual(s.veto_abouts('both', now), {'x:found_family'})
            c.apply(s, self.plan('member_remove', 'ben'))
            self.assertEqual(s.veto_abouts('both', now), set())

    def test_backup_restore_keeps_retirement_and_guest_fields(self):
        self.guest()
        c.apply(self.s, self.plan('member_update', 'kim', age=30))
        c.apply(self.s, self.plan('member_remove', 'ben'))
        cfg = c.config(self.s)
        path = backup.create(self.root)
        with tempfile.TemporaryDirectory() as target:
            restored = self.root.__class__(target) / 'restored'
            self.assertTrue(backup.restore(path, restored)['ok'])
            saved, _ = members.load_config(str(restored), {}, readonly=True)
        for field in ('retired', 'guests', 'ages', 'retired_rows'):
            self.assertEqual(saved['household'].get(field), cfg['household'][field], field)

    def test_guest_mood_play_leaves_shared_credit_tired_keys_and_context_alone(self):
        self.guest()
        s, now = self.s, self.s.time.time()
        key, row = 'tmdb:990001', 'mood:cozy'
        days = [(s.local_now(now)-dt.timedelta(days=n)).date().isoformat() for n in range(6)]
        s._learn['served'] = {key: {'name': 'Invented River', 'rows': {row: {'first': now-5*s.DAY, 'days': days}}}}
        s._learn['rows'] = {row: {'shown': 6, 'hits': 0}}
        item = dict(id=key, type='movie', profile='kim', played=True, at=now*1000)
        event = s._event(now, 'synthetic-kim', key, 'movie', 'Invented River', '', 'kim', ['kim'], 'play', 'tv', dict(item=item, genres=[], flags=[], signal='watched'))
        before = copy.deepcopy(s._learn)
        with patch.object(s, 'read_history', return_value=[item]), patch.object(s, 'read_trakt', return_value=[]), patch.object(s, 'event_stages', return_value=[('tv', [event])]), patch.object(s, 'note_tv'), patch.object(s, 'frec', return_value={}):
            s.refresh_profiles()
        self.assertEqual(s._learn, before)
        self.assertEqual(s.tired_keys(), {key: 'Invented River'})

    def test_removed_connector_links_keep_the_owner_and_skip_sync(self):
        self.guest()
        cfg = c.config(self.s)
        cfg['history']['source'] = 'jellyfin'
        cfg['connectors']['jellyfin'] = dict(kind='player', enabled=True, auth='file:synthetic.json',
            options=dict(url='https://synthetic.invalid', users={'synthetic-user': 'kim'}))
        cfg['connectors']['mdblist'] = dict(kind='api', enabled=True, accounts={'kim': 'file:synthetic-kim.json'})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        old = dict(id='tmdb:990001', type='movie', profile='kim', played=True, at=1)
        self.s._connector_write('history', {'items': [old]})
        c.apply(self.s, self.plan('member_remove', 'kim'))
        self.assertEqual(c.config(self.s)['connectors'], cfg['connectors'])
        with patch.object(self.s, '_http_get', side_effect=AssertionError('retired input fetched')):
            self.assertEqual(self.s._jellyfin(c.config(self.s)['connectors']['jellyfin']['options']), {'items': []})
        adapter = Mock(return_value={'items': []})
        with patch.dict(self.s.HISTORY_ADAPTERS, jellyfin=adapter), patch.object(self.s, 'CONFIG', c.config(self.s)), patch.object(self.s, '_connector_history', return_value=([], 1, {})):
            self.s._connector_player({'id': 'jellyfin', 'cursor': None}, self.s.time.time())
        self.assertIn(old, self.s._drop_read('history'))
        with patch.object(self.s, 'CONFIG', c.config(self.s)):
            seeds = self.s.connector_seeds(self.s.time.time())
        sync = Mock(return_value=([], [0, 0], {'events': 0}))
        self.s._connector_accounts(seeds['mdblist'], self.s.time.time(), {}, sync)
        sync.assert_not_called()

    def test_removed_single_player_link_skips_transport(self):
        for source in ('jellyfin', 'plex'):
            with self.subTest(source=source):
                opts = dict(url='https://synthetic.invalid', profile='ben', user_id='synthetic-user')
                c.apply(self.s, self.plan('member_remove', 'ben'))
                with patch.object(self.s, '_resolve_auth', return_value='synthetic'), patch.dict(self.s.CONFIG['connectors'], {source: {'auth': 'file:synthetic.json'}}), patch.object(self.s, '_http_get', return_value={}) as transport:
                    self.assertEqual(self.s.HISTORY_ADAPTERS[source](opts), {'items': []})
                    transport.assert_not_called()
                c.undo(self.s, ['ana'])

    def test_retired_aiostreams_mapping_is_retained_but_not_read(self):
        path = self.root / 'synthetic-aio.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE watch_state (uuid, media_type, base_id, last_played_at, played, kind, favorite, duration_ms, position_ms, rating, likes, dropped, season, episode)')
            db.execute('INSERT INTO watch_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       ('12345678-synthetic', 'movie', 'tmdb:990001', 1000, 1, 'movie', 1, 100, 100, None, None, 0, 0, 0))
        options = dict(path=str(path), profiles={'12345678': 'ben'})
        raw = path.read_bytes()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        with patch.dict(self.s.CONFIG['connectors'], aiostreams_db={'options': options}):
            self.assertEqual(self.s._aiostreams_db(options), {'items': []})
        self.assertEqual(options['profiles'], {'12345678': 'ben'})
        self.assertEqual(path.read_bytes(), raw)

    def test_metrics_collects_only_active_household_owners(self):
        self.guest()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        cfg = self.s.CONFIG
        cfg['connectors']['stremio'] = dict(enabled=True, accounts={p: None for p in ('ana', 'ben', 'kim')})
        items = [dict(id='tmdb:990001', type='movie', profile=p, played=True, at=1000) for p in ('ana', 'ben', 'kim')]
        imports = [dict(key='tmdb:990001', kind='movie', who=p, src='netflix', action='seen', conf=1) for p in ('ana', 'ben', 'kim')]
        with patch.object(self.s, 'read_history', return_value=items), patch.object(self.s, 'attribute', side_effect=lambda it: (it['profile'], [it['profile']], {})), patch.object(self.s, '_drop_read', return_value=items[:1]), patch.object(self.s, 'active_imports', return_value=imports):
            plays, _ = metrics.collect(self.s, self.s.time.time())
        self.assertEqual({p['person'] for p in plays}, {'ana'})

    def test_metrics_rejects_guest_and_retired_attribution_without_deleting_records(self):
        self.guest()
        cfg, now = c.config(self.s), self.s.time.time()
        cfg['metrics'] = {'enabled': True}
        row = self.s.rid('foryou')
        for person in ('ana', 'ben', 'together'):
            metrics.record_shown(self.root, cfg, row, person, [dict(id='tmdb:990001', type='movie')], now-60)
        cfg['household']['retired'] = ['ben']
        plays = [dict(key='tmdb:990001', kind='movie', person=p, source='history', at=now) for p in ('ben', 'kim')]
        metrics.update(self.root, cfg, plays, now)
        state = json.loads((self.root / 'metrics_shown.json').read_text())
        self.assertEqual({r['person'] for r in state['shown']}, {'ana', 'ben', 'together'})
        self.assertTrue(all(not r['sources'] for r in state['shown']))
        self.assertEqual(metrics.report(self.root, cfg, now)['sources'], {})

    def test_metrics_shown_and_report_skip_inactive_cohorts(self):
        self.guest()
        cfg, now = c.config(self.s), self.s.time.time()
        cfg['metrics'] = {'enabled': True}
        row = self.s.rid('foryou')
        meta = [dict(id='tmdb:990001', type='movie')]
        metrics.record_shown(self.root, cfg, row, 'ben', meta, now)
        cfg['household']['retired'] = ['ben']
        for person in ('ana', 'ben', 'kim', 'together'):
            metrics.record_shown(self.root, cfg, row, person, meta, now)
        saved = json.loads((self.root / 'metrics_shown.json').read_text())
        self.assertEqual({r['person'] for r in saved['shown']}, {'ana', 'ben', 'together'})
        self.assertEqual({r['person'] for r in metrics.report(self.root, cfg, now)['rows']}, {'ana', 'together'})

    def test_retired_trakt_selector_keeps_ownership_and_disables_sync(self):
        cfg = c.config(self.s)
        cfg['household']['trakt_member'] = 'ben'
        cfg['connectors']['trakt_api'] = dict(kind='api', enabled=True, auth='file:synthetic.json')
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        self.assertEqual(self.s.TRAKT_PERSON, 'ben')
        self.assertEqual(self.s.connector_seeds(self.s.time.time())['trakt_api']['health']['state'], 'disabled')

    def test_retired_connector_inputs_do_not_enter_read_trakt(self):
        self.guest()
        cfg = c.config(self.s)
        for source in ('trakt_api', 'mdblist', 'stremio', 'pmdb'):
            cfg['connectors'][source] = dict(kind='api', enabled=True,
                accounts={p: 'file:'+source+'-'+p+'.json' for p in ('ana', 'ben', 'kim')})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        with patch.object(self.s, 'taste_drop', return_value=[dict(id='tmdb:990001', type='movie')]):
            self.assertEqual({it['person'] for it in self.s.read_trakt()}, {'ana', 'kim'})

    def test_retired_player_records_do_not_emit_sync_events_or_hold_cursor(self):
        s, now = self.s, self.s.time.time()
        items = [dict(id='tmdb:'+str(990001+n), type='movie', profile=p, played=True, at=(now-600+n*60)*1000) for n, p in enumerate(('ana', 'ben'))]
        s._connector_write('history', {'items': items})
        raw = (self.root / 'history.json').read_bytes()
        s._profiles['household'] = {'entries': [dict(item=it, key=it['id'], title='Invented River', year='', genres=[]) for it in items[:1]]}
        c.apply(s, self.plan('member_remove', 'ben'))
        with patch.object(s, 'attribute', side_effect=lambda it: (it['profile'], [it['profile']], {})):
            events, cursor, stats = s._connector_history({'cursor': 0}, now)
        self.assertEqual([e['who'] for e in events], ['ana'])
        self.assertEqual(cursor, items[0]['at'])
        self.assertEqual(stats['events'], 1)
        self.assertEqual((self.root / 'history.json').read_bytes(), raw)

    def test_metrics_shared_forget_gate_uses_active_household_owners(self):
        self.guest()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        cutoff = '2099-01-01'
        self.assertTrue(metrics._after_forget(c.config(self.s), {'ben': cutoff, 'kim': cutoff}, 'together', self.s.time.time()))

    def test_metrics_household_aggregation_skips_retired_and_guest_cohorts(self):
        self.guest()
        cfg, now = c.config(self.s), self.s.time.time()
        cfg['metrics'] = {'enabled': True}
        row = self.s.rid('foryou')
        weeks = {}
        for days in (35, 42):
            week = metrics._week(now-days*self.s.DAY, cfg)
            weeks[week] = {row: {p: dict(shown=300, played=1, sources={'history': 1}, complete=True,
                closed=True, extra={'plays': [p+week]}) for p in ('ana', 'ben', 'kim')}}
        state = dict(v=1, shown=[], folded=weeks, sources={}, cleaned=None)
        (self.root / 'metrics_shown.json').write_text(json.dumps(state))
        cfg['household']['retired'] = ['ben']
        mixed = metrics.learning_report(self.root, cfg, now)['rows']
        clean = copy.deepcopy(state)
        for rows in clean['folded'].values():
            rows[row] = {'ana': rows[row]['ana']}
        (self.root / 'metrics_shown.json').write_text(json.dumps(clean))
        self.assertEqual(mixed, metrics.learning_report(self.root, cfg, now)['rows'])

    def test_retired_hits_are_filtered_when_remaining_people_are_adults(self):
        s, now = self.s, int(self.s.time.time())
        row, key = s.rid('foryou'), 'tmdb:990001'
        s._learn['hits'] = [dict(key=key, title='Invented Ben River', row=row, at=now, share=1)]
        s._learn['rows'] = {row: dict(shown=1, hits=1)}
        s._learn['ctx'] = [dict(key=key, at=now)]
        retained = copy.deepcopy(s._learn)
        c.apply(s, self.plan('member_remove', 'ben'))
        with patch.object(s, 'read_history', return_value=[dict(id=key, profile='ben', at=now*1000)]), patch.object(s, 'attribute', return_value=('ben', ['ben'], {})):
            self.assertEqual(s.hit_titles(), [])
            self.assertEqual(s.learning_records('ctx'), [])
            self.assertEqual(s.learning_rows()[row]['hits'], 0)
        self.assertEqual(s._learn, retained)

    def test_personal_prompt_uses_only_adult_personality_hints(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ana']['type'] = 'ENFP'
        cfg['taste']['people']['ben']['type'] = 'INTJ'
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, self.plan('member_remove', 'ben'))
        self.s._profiles['household'] = dict(entries=[], searches=['invented rivers'], likes='', seen=set(), negative=set())
        llm = Mock(return_value={})
        with patch.object(self.s, 'get_model', return_value={}), patch.object(self.s, 'catalog_picks', return_value=[]), patch.object(self.s, 'candidate_pool', return_value=[]), patch.object(self.s, 'llm_sections', llm):
            self.s.personal_feed(self.s.LANG)
        prompt = llm.call_args.args[1]
        self.assertIn('Ana ENFP', prompt)
        self.assertNotIn('Ben INTJ', prompt)

    def test_active_limit_allows_replacement_and_refuses_removal_undo_before_writes(self):
        self.assertEqual(max(self.s.MEMBER_COUNTS), 6)
        c.apply(self.s, self.plan('member_create', 'kim'))
        c.apply(self.s, self.plan('member_remove', 'ben'))
        with patch.object(self.s, 'MEMBER_COUNTS', (1, 2, 3)), patch.object(members, 'MEMBER_COUNTS', (1, 2, 3)):
            c.apply(self.s, self.plan('member_create', 'marko', actor='kim'))
            before = self.files()
            with self.assertRaisesRegex(ValueError, 'limit.*3|3.*limit'):
                c.undo(self.s, ['ana'])
            self.assertEqual(self.files(), before)
        self.assertEqual(self.s.PEOPLE, ('ana', 'kim', 'marko'))

    def test_labeled_unresolved_creation_asks_for_name_without_writes(self):
        before = self.files()
        answer = dict(op='member_create', person='her', label='Her', guest=True)
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = c.preview(self.s, 'Add her as a guest', 'ana')
        self.assertTrue(plan.get('read_only'), plan)
        self.assertTrue(plan['name'].endswith('?'))
        c.apply(self.s, plan)
        for name, raw in before.items():
            self.assertEqual((self.root / name).read_bytes(), raw)
        self.assertNotIn('her', self.s.PEOPLE)

    def test_shared_shelf_requires_one_partner_match(self):
        c.apply(self.s, self.plan('member_update', 'ben', label='Kim Jones'))
        c.apply(self.s, self.plan('member_create', 'kim', label='Kim Jones', guest=True))
        before = self.files()
        for partner in ('Kim Jones', 'unknown'):
            with self.subTest(partner=partner):
                plan = c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person=partner))
                self.assertTrue(plan.get('read_only'), plan)
                self.assertTrue(plan['name'].endswith('?'))
                c.apply(self.s, plan)
        self.assertEqual(self.files(), before)
        plan = c.preview(self.s, '', 'ana', answer=dict(op='shelf_add', values=['invented rivers', 'always'], with_person='Kimu'))
        self.assertEqual(plan['shelf']['with_person'], 'kim')

    def test_cli_and_setup_status_list_active_identities_only(self):
        c.apply(self.s, self.plan('member_remove', 'ben'))
        members.configure(str(self.root), readonly=True)
        self.assertEqual(members.PEOPLE, ['ana'])
        with self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
            parser().parse_args(['calibration', '--person', 'ben'])
        status = setup.status(self.root)
        self.assertTrue(all('ben' not in people for people in status['connections'].values()))
        self.assertTrue(all('Ben' not in line for line in status['prefs']))

    def test_setup_household_summary_omits_retired_identity(self):
        c.apply(self.s, self.plan('member_remove', 'ben'))
        args = parser().parse_args(['setup', '--yes', '--json', '--base', 'https://synthetic.invalid'])
        with patch.object(setup, 'check_tmdb'), patch.object(setup.guide, 'doctor', return_value={'findings': []}), patch.object(setup, 'restart_required', return_value=False):
            result = setup.run(args)
        self.assertNotIn('Household: ana, ben;', '\n'.join(result.get('lines', [])))
        self.assertIn('Household: ana;', '\n'.join(result.get('lines', [])))

    def test_calibration_omits_disabled_retained_shelf_rows(self):
        self.s.shelf_change('ben', 'add', text='genre:comedy', name='Invented Ben', apply=True)
        row = self.s.rid('shelf_ben_1')
        self.s._feeds[row, self.s.LANG] = dict(metas={'tv': [dict(id='tmdb:990001', type='movie')]})
        c.apply(self.s, self.plan('member_remove', 'ben'))
        engine = SimpleNamespace(_server=self.s, _model={'bg': [], 'pbg': {}}, _person=lambda _: None)
        with patch.object(self.s, 'person_score', return_value=(0, [])), patch.object(self.s, 'match_pct', return_value=None):
            self.assertNotIn(row, Engine.calibration(engine)['rows'])
        self.assertIn((row, self.s.LANG), self.s._feeds)

    def test_shelf_theme_member_words_do_not_override_structured_answer(self):
        text = 'Add a shelf about Ana and invented rivers'
        answer = dict(op='shelf_add', values=['Ana and invented rivers', 'always'], person=None)
        self.assertEqual(c.parse_shelf(text, self.s)['values'], answer['values'])
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = c.preview(self.s, text, 'ana')
            self.assertEqual(llm.calls, 1)
        self.assertEqual(plan['shelf']['text'], 'Ana and invented rivers')

    def test_api_shared_shelf_uses_model_participants(self):
        self.guest()
        answer = dict(op='shelf_add', values=['invented rivers', 'always'], person=None, with_person='kim')
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            result = c.api_preview(self.s, dict(body={'in': {'query': 'Add a catalog for Kim and me with invented rivers'}}, viewer='ana', client={'name': 'ana'}))
            self.assertEqual(result['preview']['shelf'].get('with_person'), 'kim')
            self.assertEqual(result['preview']['shelf']['text'], 'invented rivers')
            self.assertEqual(llm.calls, 1)
