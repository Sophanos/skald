"""Synthetic home inspection and household controls, with no upstream calls."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server, ReplayLLM
from skald import commands, members, setup
from test_w108_rows import cards


class RowPlan(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['household']['member_rows'] = ['ana', 'ben']
        setup.publish(self.data, cfg)
        self.s = load_server(self.data)
        self.active_profiles = self.s.active_profiles
        self.explicit_seeds = self.s.explicit_seeds
        self.addCleanup(close_server, self.s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'aio_rows_changed'))
        for name, value in {'get_model': None, 'row_pool': [], 'tired_keys': {}, 'memory_why': None,
                            'remote_masks': (set(), set()), 'veto_keys': set(), 'active_profiles': [],
                            'who_watching': ('both', None), 'explicit_seeds': [], 'week_ok': True,
                            'week_tail': []}.items():
            self.enterContext(patch.object(self.s, name, return_value=value))
        for name in ('row_eligible', 'row_related', 'lane_allowed', 'maker_view'):
            self.enterContext(patch.object(self.s, name, return_value=True))
        self.enterContext(patch.object(self.s, 'polish_row', side_effect=lambda xs, *a, **kw: xs))

    def put(self, key, xs, **names):
        cid = self.s.rid(key)
        self.s._feeds[cid, self.s.LANG] = {'stamp': 'current', 'metas': {'tv': xs}, 'names': names}
        return cid

    def test_season_gate_at_serve_time_and_in_plan_and_why(self):
        import datetime
        from skald.rowplan import why
        s = self.s
        cid = self.put('lane1', cards(20), lane='k:42', title='Synthetic winter theme', months=[12])
        for month, hidden in ((10, True), (12, False), (2, True)):
            with self.subTest(month=month), patch.object(s, 'local_now', return_value=datetime.datetime(2026, month, 14, 18)):
                self.assertEqual(bool(s.served_row(cid, 'tv', s.LANG, {}, set(), (set(), set()))), not hidden)
                row = next(r for r in self.plan() if r['id'] == cid)
                self.assertEqual(row['season'], {'months': [12], 'hidden': hidden})
                self.assertEqual(why(s, 'lane1')['season'], row['season'])
                self.assertEqual(row['rejection'], 'not_today' if hidden else None)
                self.assertEqual(bool(s.source_metas(cid, s.LANG)), not hidden)
        s._feeds[cid, s.LANG]['names']['months'] = 'any'
        with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)):
            self.assertTrue(s.served_row(cid, 'tv', s.LANG, {}, set(), (set(), set())))
        for key in ('mood1', 'leap'):
            row = self.put(key, cards(20), months=[12])
            with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)):
                self.assertEqual(s.served_row(row, 'tv', s.LANG, {}, set(), (set(), set())), [])

    def test_legacy_pin_uses_new_window_only_for_the_same_theme(self):
        import datetime
        s = self.s
        cid = self.put('lane1', cards(20), lane='k:42', title='Synthetic winter theme', months=[12])
        s._shown[cid, s.LANG] = {'metas': {'tv': cards(20)}, 'names': {'lane': 'k:42', 'title': 'Synthetic winter theme'}}
        with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)):
            self.assertEqual(s.row_list(cid, 'tv', s.LANG), [])
            s._shown[cid, s.LANG]['names']['lane'] = 'k:43'
            self.assertTrue(s.row_list(cid, 'tv', s.LANG))

    def test_legacy_winter_pin_resolves_persisted_window_after_slot_replacement(self):
        import datetime
        from skald.rowplan import why
        s = self.s
        theme = {'kind': 'lane', 'name': 'Synthetic winter theme', 'source': ['k:42', 'Synthetic winter']}
        s.theme_months(theme, [12])
        s._learn['seasons'].clear()
        s.load_state()
        cid = self.put('lane1', cards(20, 100), lane='k:43', title='Synthetic everyday theme', months='any')
        s._shown[cid, s.LANG] = {'metas': {'tv': cards(20)},
                                    'names': {'lane': 'k:42', 'title': theme['name']}}
        with patch.object(s, 'get_model', return_value={'names': {'k:42': 'Synthetic winter'}, 'nexp': {}}), \
                patch.object(s, 'openrouter', side_effect=AssertionError('serving must be offline')):
            for month, hidden in ((10, True), (12, False)):
                with self.subTest(month=month), patch.object(s, 'local_now', return_value=datetime.datetime(2026, month, 14, 18)):
                    self.assertEqual(s.row_months(cid, s.LANG), [12])
                    self.assertEqual(bool(s.row_list(cid, 'tv', s.LANG)), not hidden)
                    self.assertEqual(bool(s.served_row(cid, 'tv', s.LANG, {}, set(), (set(), set()))), not hidden)
                    self.assertEqual(why(s, 'lane1')['season'], {'months': [12], 'hidden': hidden})

    def test_deferred_window_invalidates_home_cached_before_season_reply(self):
        import datetime
        s = self.s
        cid = s.rid('lane1')
        theme = {'kind': 'lane', 'key': 'k:42', 'name': 'Synthetic winter theme', 'source': ['k:42', 'Synthetic winter']}
        def build(lang):
            s.theme_months(theme, allow_llm=True)
            return {cid: {'tv': cards(20)}, '_names': {cid: {'title': theme['name'], 'lane': theme['key']}}}
        def reply(*args):
            self.assertEqual(len(s.row_metas(cid, 'tv', s.LANG, who='both')), 20)
            return {'choices': [{'message': {'content': '{"months": [12]}'}}]}
        with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)), \
                patch.object(s, 'OR_KEY', 'fake'), patch.object(s, 'openrouter', side_effect=reply), \
                patch.object(s, '_langs', {s.LANG}), patch.object(s, 'connectors_tick'), \
                patch.object(s, 'job_specs', return_value=[('lanes', [cid], 'synthetic', 0, build)]):
            s.build_feeds(s.local_now(), {}, {}, force=True)
            self.assertEqual(s.row_metas(cid, 'tv', s.LANG, who='both'), [])

    def plan(self, person='household'):
        from skald.rowplan import plan
        return plan(self.s, person)['rows']

    def test_documentary_and_studio_labels_resolve_translation_keys(self):
        from skald.rowplan import label
        s = self.s
        with patch.object(s, 'LANG', 'de-DE'):
            self.assertEqual(label(s, s.rid('wonder'), s.LANG), 'Wunder unserer Welt')
            self.assertEqual(label(s, s.rid('hbo'), s.LANG), 'HBO')

    def test_deferred_lane_season_survives_localized_name(self):
        import datetime
        s = self.s
        cid = s.rid('lane1')
        theme = {'kind': 'lane', 'key': 'k:42', 'name': 'Synthetic winter theme', 'source': ['k:42', 'Synthetic winter theme']}
        def build(lang):
            s.theme_months(theme, allow_llm=True)
            return {cid: {'tv': cards(20)}, '_names': {cid: {'title': 'Erfundene Winterwelt', 'name': theme['name'], 'lane': theme['key']}}}
        with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)), \
                patch.object(s, 'OR_KEY', 'fake'), patch.object(s, 'openrouter', return_value={'choices': [{'message': {'content': '{"months": [12]}'}}]}), \
                patch.object(s, '_langs', {s.LANG}), patch.object(s, 'connectors_tick'), \
                patch.object(s, 'job_specs', return_value=[('lanes', [cid], 'synthetic', 0, build)]):
            s.build_feeds(s.local_now(), {}, {}, force=True)
            self.assertEqual(s._feeds[cid, s.LANG]['names'].get('months'), [12])
            self.assertEqual(s.row_metas(cid, 'tv', s.LANG, who='both'), [])

    def test_legacy_localized_pin_finds_season_by_theme_name(self):
        s = self.s
        theme = {'kind': 'lane', 'name': 'Synthetic winter theme', 'source': ['k:42', 'Synthetic winter theme']}
        s.theme_months(theme, [12])
        cid = self.put('lane1', cards(20), lane='k:43', title='Synthetic everyday theme', months='any')
        s._shown[cid, s.LANG] = {'metas': {'tv': cards(20)}, 'names': {'lane': 'k:42', 'title': 'Erfundene Winterwelt'}}
        with patch.object(s, 'get_model', return_value={'names': {'k:42': theme['name']}}):
            self.assertEqual(s.row_months(cid, s.LANG), [12])

    def test_every_registered_row_and_hidden_codes(self):
        s = self.s
        for i in range(1, 5):
            self.put('mood'+str(i), cards(6, 100*i), mood='cozy')
        self.put('lane1', cards(2, 600), lane='k:42')
        result = {r['id']: r for r in self.plan('ana')}
        self.assertTrue(set(s.DEDUPE_ORDER + s.SHELF_ROWS + s.LANE_BENCH) <= set(result))
        for key, code in (('digital', 'disabled'), ('mood4', 'family_cap'), ('lane1', 'too_few_own'),
                          ('ben', 'unsafe'), ('because', 'not_built')):
            self.assertEqual(result[s.rid(key)]['rejection'], code, key)
        self.assertEqual(result[s.rid('lane1')]['own_cards'], 2)
        self.assertEqual(result[s.rid('mood1')]['position'], 1)
        self.assertNotIn('value', result[s.rid('mood1')])

    def test_overlap_and_daily_rotation(self):
        s = self.s
        self.put('mood1', cards(6))
        self.put('mood2', cards(6))
        for i, key in enumerate(s.DOCS):
            self.put(key.removeprefix(s.rid('')), cards(6, 100+i*10))
        result = {r['id']: r for r in self.plan()}
        self.assertEqual(result[s.rid('mood2')]['rejection'], 'overlap')
        self.assertEqual(sum(result[cid]['rejection'] == 'not_today' for cid in s.DOCS), len(s.DOCS)-1)

    def test_guest_and_other_member_are_unsafe(self):
        self.put('ana', cards(6))
        for person in ('guest', 'ben'):
            result = {r['id']: r for r in self.plan(person)}
            self.assertEqual(result[self.s.rid('ana')]['rejection'], 'unsafe')
            self.assertEqual(result[self.s.rid('ana')]['own_cards'], 0)

    def test_caps_defaults_config_and_explicit_rows(self):
        s = self.s
        for i in range(1, 5):
            self.put('mood'+str(i), cards(6, i*100))
        for i in range(1, 6):
            self.put('lane'+str(i), cards(6, 1000+i*100), lane='k:42')
        s.CONFIG['household']['home'] = {'family_caps': {'mood': 1, 'lane': 2}}
        shown = [r for r in self.plan() if r['position'] is not None]
        self.assertEqual(sum(r['id'] in s.MOODS_ROWS for r in shown), 1)
        self.assertEqual(sum(r['id'] in s.LANES for r in shown), 2)
        s.CONFIG['household']['row_labels'] = {'moods': 'Quiet rows'}
        self.assertEqual(sum(r['id'] in s.MOODS_ROWS and r['position'] is not None for r in self.plan()), 1)
        s.CONFIG['household']['rows'] = ['moods', 'lanes']
        self.assertEqual(sum(r['position'] is not None for r in self.plan()), 9)

    def test_order_or_labels_alone_keep_default_caps(self):
        s = self.s
        for i in range(1, 5):
            self.put('mood'+str(i), cards(6, i*100))
        for key, value in (('row_order', ['foryou']), ('row_labels', {'moods': 'Quiet rows'})):
            with self.subTest(key=key), patch.dict(s.CONFIG['household'], {key: value}):
                self.assertEqual(sum(r['id'] in s.MOODS_ROWS and r['position'] is not None for r in self.plan()), 3)

    def test_commander_counts_raw_borrowed_cards(self):
        s = self.s
        cid = self.put('foryou', cards(1, 100))
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 200)))
        result = commands.preview(s.command_context(), 'synthetic read', 'ana',
                                  answer=dict(op='home_plan', kind=None, values=[cid], person=None))
        row = next(r for r in result['home']['rows'] if r['id'] == cid)
        self.assertEqual((row['own_cards'], row['borrowed_cards']), (1, 11))

    def test_shared_home_plan_normalizes_both(self):
        from skald.rowplan import plan
        s = self.s
        self.put('mood1', cards(6))
        expected = plan(s, 'together')
        self.assertEqual(plan(s, 'both'), expected)
        for scope in (None, 'together'):
            with self.subTest(scope=scope), s.addon_scope(scope), ReplayLLM(s) as replay:
                replay.arm({'choices': [{'message': {'content': json.dumps(
                    dict(op='home_plan', kind=None, values=[], person=None))}}]})
                result = commands.preview(s.command_context(), 'why these rows?', 'both', bound=scope)
                self.assertEqual(result['home'], expected)

    def test_inspection_shelves_uses_cached_facets_only(self):
        from skald import shelves
        from skald.rowplan import why
        s = self.s
        wish = dict(id='synthetic-wish', scope='ana', slot=1, text='quiet mysteries', name='Quiet shelf', when='always')
        shelves.save(s, {'v': 1, 'wishes': [wish], 'plans': {}, 'undo': {}})
        s._feeds[s.rid('shelfraw_ana_1'), s.LANG] = {'wish': wish['id'], 'window': 'always', 'candidates': cards(8)}
        s._shown_scoped['ana'] = {}
        for name in ('ensure_facets', 'ensure_emb', 'tmdb', 'openrouter', 'llm_picks'):
            self.enterContext(patch.object(s, name, side_effect=AssertionError('inspection enrichment: '+name)))
        before = copy.deepcopy((s._feeds, s._facets, s._learn, s._shown, s._shown_scoped))
        dirty = s._facets_dirty.is_set()
        with s.addon_scope('ana'):
            self.assertEqual(len(s.shelf_feed(s.LANG, build=False)[s.rid('shelf_ana_1')]['tv']), 8)
        result = commands.preview(s.command_context(), 'synthetic read', 'ana',
                                  answer=dict(op='home_plan', kind=None, values=[], person=None))
        self.assertEqual(next(r for r in result['home']['rows'] if r['id'] == s.rid('shelf_ana_1'))['own_cards'], 8)
        self.assertEqual(why(s, 'shelf_ana_1', 'ana')['own_cards'], 8)
        self.assertEqual((s._feeds, s._facets, s._learn, s._shown, s._shown_scoped), before)
        self.assertEqual(s._facets_dirty.is_set(), dirty)

    def test_family_and_archive_use_serving_path(self):
        s = self.s
        s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        family = self.put('family', cards(8))
        s._facets.update({x['id']: {'kind': 'movie', 'cr': {'US': 'G'}, 'g': ['Family']} for x in cards(8)})
        row = next(r for r in self.plan('ana') if r['id'] == family)
        self.assertIsNotNone(row['position'])
        self.assertEqual((row['rejection'], row['own_cards']), (None, 8))
        archived = self.put('archive', cards(8, 100))
        with patch.dict(s.ARCHIVED, {'archive': 'Archived member'}), patch.object(s, 'active_profiles', return_value=['archive']), \
                patch.object(s, 'MIXED', s.MIXED | {archived}):
            row = next(r for r in self.plan() if r['id'] == archived)
        self.assertIsNotNone(row['position'])
        self.assertEqual(row['own_cards'], 8)

    def test_hidden_chart_does_not_dedupe_archive(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['people', 'digital']
        archived = self.put('archive', cards(8))
        s._feeds[s.rid('digital'), s.LANG] = {'metas': {'movie': cards(5)}}
        with patch.dict(s.ARCHIVED, {'archive': 'Archived member'}), patch.object(s, 'active_profiles', return_value=['archive']), \
                patch.object(s, 'MIXED', s.MIXED | {archived}):
            row = next(r for r in self.plan() if r['id'] == archived)
        self.assertIsNotNone(row['position'])
        self.assertEqual(row['own_cards'], 8)

    def test_chart_and_family_dropped_cards_are_traced(self):
        from skald.rowplan import why
        s = self.s
        s.CONFIG['household']['rows'] = ['digital', 'toprated', 'family']
        s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        xs = cards(6)
        s._profiles['household'] = {'seen': {x['id'] for x in xs}, 'negative': set()}
        for key in ('digital', 'toprated', 'family'):
            cid = s.rid(key)
            s._feeds[cid, s.LANG] = {'metas': {'movie': xs, 'tv': xs}}
            with self.subTest(key=key):
                result = why(s, cid)
                self.assertEqual(len(result['cards']), 6)
                self.assertTrue(all(x['removed_by'] == 'seen' for x in result['cards']))

    def test_chart_overlap_is_traced(self):
        from skald.rowplan import why
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou', 'digital', 'toprated']
        xs = cards(6)
        self.put('foryou', xs)
        for key in ('digital', 'toprated'):
            cid = s.rid(key)
            s._feeds[cid, s.LANG] = {'metas': {'movie': xs}}
            with self.subTest(key=key):
                result = why(s, cid)
                self.assertEqual(result['rejection'], 'overlap')
                self.assertEqual(len(result['cards']), 6)
                self.assertTrue(all(x['removed_by'] == 'overlap' for x in result['cards']))

    def test_fewer_refuses_empty_or_below_minimum_home(self):
        s = self.s
        self.put('mood1', cards(6))
        cfg = commands.config(s)
        cfg['household']['home'] = {'family_caps': {'mood': 1, 'lane': 0}}
        setup.publish(self.data, cfg, replace=True)
        s.refresh_preferences()
        before = commands.config(s)
        with self.assertRaisesRegex(ValueError, 'populated row'):
            commands.preview(s, 'synthetic', 'ana', answer=dict(op='home_fewer', kind=None, values=['mood'], person=None))
        self.assertEqual(commands.config(s), before)

    def test_cli_fewer_refuses_removing_the_only_populated_row(self):
        from skald.__main__ import main
        from skald.engine import Engine
        class NoRefillEngine(Engine):
            def __enter__(self):
                self._server.row_pool = lambda *a: []
                return self
        self.put('mood1', cards(6))
        self.s.save_feeds()
        cfg = commands.config(self.s)
        cfg['household']['home'] = {'family_caps': {'mood': 1, 'lane': 0}}
        setup.publish(self.data, cfg, replace=True)
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}), patch('skald.__main__.Engine', NoRefillEngine):
            out = io.StringIO()
            self.assertEqual(main(['rows', 'prefer', '--theme', 'mood', '--fewer', '--yes', '--json'], out=out), 2)
        self.assertIn('populated row', json.loads(out.getvalue())['error'])
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_inspection_does_not_create_default_taste_file(self):
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.object(self.s, 'active_profiles', self.active_profiles), patch.object(self.s, 'explicit_seeds', self.explicit_seeds):
            commands.preview(self.s.command_context(), 'synthetic read', 'ana',
                             answer=dict(op='home_plan', kind=None, values=[], person=None))
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_snapshot_inspection_keeps_family_feed(self):
        from skald.engine import Engine
        from skald.rowplan import why
        cfg = commands.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        setup.publish(self.data, cfg, replace=True)
        self.put('family', cards(8))
        self.s.save_feeds()
        (self.data/'facets.json').write_text(json.dumps({'items': {x['id']: {'kind': 'movie', 'cr': {'US': 'G'}, 'g': ['Family']} for x in cards(8)}}))
        with Engine(self.data, offline=True) as engine:
            result = why(engine._server, 'family', 'ana')
        self.assertIsNotNone(result['position'])
        self.assertEqual(result['own_cards'], 8)

    def test_fewer_rechecks_projected_home_on_apply(self):
        s = self.s
        # Weekly cards serve only with this week's stamp.
        week = lambda xs: [{**x, '_week_start': s.week_start(), '_week': {'together': 'Synthetic reason'}} for x in xs]
        fallback = [self.put(key, (week if key == 'week' else list)(cards(6, 100+i*100)))
                    for i, key in enumerate(('foryou', 'week', 'because', 'leap', 'catchup', 'upcoming'))]
        self.put('mood1', cards(6))
        cfg = commands.config(s)
        cfg['household']['home'] = {'family_caps': {'mood': 1, 'lane': 0}}
        setup.publish(self.data, cfg, replace=True)
        s.refresh_preferences()
        proposal = commands.preview(s, 'synthetic', 'ana', answer=dict(op='home_fewer', kind=None, values=['mood'], person=None))
        for cid in fallback:
            s._feeds.pop((cid, s.LANG))
        before = commands.config(s)
        with self.assertRaisesRegex(ValueError, 'populated row'):
            commands.apply(s, proposal)
        self.assertEqual(commands.config(s), before)

    def test_invalid_caps_rejected_and_public_config_keeps_home(self):
        for value in (-1, 9, True, 1.5, '3', None):
            cfg = copy.deepcopy(self.s.CONFIG)
            cfg['household']['home'] = {'family_caps': {'mood': value}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.s.validate_config(cfg)
        cfg = copy.deepcopy(self.s.CONFIG)
        cfg['household']['home'] = {'family_caps': {'mood': 0, 'lane': 8}}
        self.assertEqual(self.s.public_config(self.s.validate_config(cfg))['household']['home'], cfg['household']['home'])

    def test_why_cards_caps_borrowing_seed_and_reason(self):
        from skald.rowplan import why
        s = self.s
        cid = self.put('mood1', cards(10), mood='cozy', seed='tmdb:999')
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 4))
        result = why(s, cid)
        self.assertEqual(result['superhero_removed'], 2)
        self.assertEqual(result['hero_cards'], 2)
        self.assertEqual(result['seed'], 'tmdb:999')
        self.assertEqual(sum(x['removed_by'] == 'cap' for x in result['cards']), 2)
        self.assertFalse(any(x['reason_present'] for x in result['cards']))
        self.put('foryou', [{**cards(1, 100)[0], 'description': 'Fits your quiet wit. · An invented overview.'}])
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 200)))
        row = why(s, 'foryou')
        self.assertEqual(row['own_cards'], 1)
        self.assertEqual(row['borrowed_cards'], 11)
        self.assertTrue(row['cards'][0]['reason_present'])

    def test_plan_no_writes_and_offline_engine_preserves_data(self):
        from skald.engine import Engine
        from skald.rowplan import plan
        cid = self.put('mood1', cards(8), mood='cozy')
        self.s.save_feeds()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with Engine(self.data, offline=True) as engine, \
                patch.object(engine._server, 'pin_shown', side_effect=AssertionError('pin write')), \
                patch.object(engine._server, 'save_learn', side_effect=AssertionError('learn write')), \
                patch.object(engine._server, 'note_exposure', side_effect=AssertionError('exposure write')), \
                patch.object(engine._server, 'save_feeds', side_effect=AssertionError('feed write')):
            result = plan(engine._server, 'ana')
            self.assertIn(cid, {r['id'] for r in result['rows']})
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_prefer_preview_apply_undo_and_stale_preview(self):
        answer = dict(op='home_more', kind=None, values=['mood'], person=None)
        before = commands.config(self.s)
        plan = commands.preview(self.s, 'more mood rows', 'ana', answer=answer)
        self.assertEqual(commands.config(self.s), before)
        commands.apply(self.s, plan)
        self.assertEqual(commands.config(self.s)['household']['home']['family_caps']['mood'], 4)
        with self.assertRaises(ValueError):
            commands.apply(self.s, plan)
        commands.undo(self.s, ['ana'])
        self.assertEqual(commands.config(self.s)['household'], before['household'])

    def test_live_projection_waits_for_profile_without_another_engine(self):
        s = self.s
        (self.data/'feeds.json').write_text('{}')
        (self.data/'profile.json').write_text(json.dumps({'entries': [{'key': 'tmdb:900', 'title': 'Invented Lantern', 'kind': 'movie'}]}))
        (self.data/'facets.json').write_text(json.dumps({'items': {'tmdb:900': {'title': 'Invented Lantern', 'kind': 'movie'}}}))
        with patch.object(s, '_CONFIG_READONLY', False, create=True), \
                patch('skald.engine.Engine', side_effect=AssertionError('second engine')) as engine:
            with self.assertRaisesRegex(ValueError, 'Taste is still loading'):
                commands.preview(s.command_context(), 'more mood rows', 'ana',
                                 answer=dict(op='home_more', kind=None, values=['mood'], person=None))
            engine.assert_not_called()

    def test_refresh_preview_locks_feed_iteration(self):
        s = self.s
        cid = self.put('mood1', cards(6))
        class GrowingFeeds(dict):
            def items(self):
                iterator = iter(super().items())
                yield next(iterator)
                if not s._feeds_lock.locked():
                    self[s.rid('mood2'), s.LANG] = {'stamp': 'new'}
                yield from iterator
        with patch.object(s, '_feeds', GrowingFeeds(s._feeds)):
            proposal = commands.preview(s.command_context(), 'refresh mood1', 'ana',
                                        answer=dict(op='row_refresh', kind=None, values=[cid], person=None))
        self.assertEqual(proposal['refresh']['before'], {s.LANG: {'built': None, 'stamp': 'current'}})

    def test_refresh_only_row_stamp_apply_undo(self):
        a = self.put('mood1', cards(8))
        b = self.put('mood2', cards(8, 100))
        answer = dict(op='row_refresh', kind=None, values=[b], person=None)
        before = copy.deepcopy(self.s._feeds)
        plan = commands.preview(self.s, 'refresh mood2', 'ana', answer=answer)
        self.assertEqual(self.s._feeds, before)
        commands.apply(self.s, plan)
        self.assertEqual(self.s._feeds[a, self.s.LANG], before[a, self.s.LANG])
        self.assertNotIn('stamp', self.s._feeds[b, self.s.LANG])
        saved = json.loads((self.data/'feeds.json').read_text())
        self.assertNotIn('stamp', saved[b+'|'+self.s.LANG])
        commands.undo(self.s, ['ana'])
        self.assertEqual(self.s._feeds, before)

    def test_refresh_later_group_row_triggers_worker(self):
        s = self.s
        for cid in s.MOODS_ROWS:
            self.put(cid.removeprefix(s.rid('')), cards(8))
        stamp = 'v'+s.STAMP_V+'/today'
        for rec in s._feeds.values():
            rec['stamp'] = stamp
        s._feeds[s.MOODS_ROWS[1], s.LANG].pop('stamp')
        builder = self.enterContext(patch.object(s, 'mood_feed', return_value={cid: {'tv': cards(8)} for cid in s.MOODS_ROWS}))
        self.enterContext(patch.object(s, 'job_specs', side_effect=lambda *a: [('moods', s.MOODS_ROWS, 'today', 3600, builder)]))
        self.enterContext(patch.object(s, 'connectors_tick'))
        s.build_feeds(s.local_now(), {}, {})
        self.assertEqual(builder.call_count, 1)

    def test_new_ops_use_decision_transport_and_clarify_this(self):
        self.put('foryou', cards(6, 100))
        for op, text, values in (('home_plan', 'why these rows?', []), ('home_more', 'more mood rows', ['mood']),
                                 ('home_fewer', 'fewer lane rows', ['lane']), ('row_refresh', 'refresh this row', [])):
            with self.subTest(op=op), ReplayLLM(self.s) as replay:
                answer = dict(op=op, kind=None, values=values, person=None)
                replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
                self.assertIsNone(commands.parse_local(text, self.s)['op'])
                plan = commands.preview(self.s, text, 'ana')
                self.assertEqual(replay.calls, 1)
                self.assertTrue(plan)
                if op in ('home_plan', 'row_refresh'):
                    self.assertTrue(plan['read_only'])

    def test_cli_verbs_and_old_rows(self):
        from skald.__main__ import parser, main
        from skald.rowplan import plan
        with patch.object(members, 'CONFIG', self.s.CONFIG), patch.object(members, 'PEOPLE', ['ana', 'ben']):
            p = parser()
            self.assertEqual(p.parse_args(['rows', '--row', 'foryou', '--n', '4']).n, 4)
            self.assertEqual(p.parse_args(['rows', 'plan', '--person', 'ana', '--json']).rows_action, 'plan')
            self.assertEqual(p.parse_args(['rows', 'why', 'mood1', '--json']).rows_action, 'why')
            p.parse_args(['rows', 'prefer', '--theme', 'mood', '--more'])
            p.parse_args(['rows', 'refresh', 'mood1'])
        output = io.StringIO()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}):
            self.assertEqual(main(['rows', 'plan', '--person', 'ana', '--json'], out=output), 0)
        self.assertTrue(json.loads(output.getvalue())['rows'])
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_metric_support_and_franchise_removals(self):
        s = self.s
        self.put('mood1', cards(6, 100, collection=42))
        self.put('mood2', cards(6, 200, collection=42))
        cid = self.put('mood3', cards(6, 300, collection=42))
        s.CONFIG['metrics'] = {'enabled': True}
        report = dict(rows=[dict(row=cid, person='ana', shown=4, played=2)], enabled=True)
        with patch.object(s.row_metrics, 'report', return_value=report):
            result = {r['id']: r for r in self.plan('ana')}
        self.assertEqual(result[cid]['franchise_removed'], 6)
        self.assertEqual(result[cid]['support'], {'shown': 4, 'plays': 2})

    def test_shelf_schedule_and_scope(self):
        from skald import shelves
        s = self.s
        wish = dict(id='synthetic-wish', scope='ana', slot=1, text='quiet mysteries', name='Quiet shelf',
                    when={'days': ['mon'], 'hours': [5, 10]})
        shelves.save(s, {'v': 1, 'wishes': [wish], 'plans': {}, 'undo': {}})
        import datetime as dt
        with patch.object(s, 'local_now', return_value=dt.datetime(2026, 10, 14, 18)):
            result = {r['id']: r for r in self.plan('ana')}
        self.assertEqual(result[s.rid('shelf_ana_1')]['rejection'], 'not_today')
        result = {r['id']: r for r in self.plan('ben')}
        self.assertEqual(result[s.rid('shelf_ana_1')]['rejection'], 'unsafe')

    def test_dropped_filters_and_hidden_card_reasons(self):
        from skald.rowplan import why
        s = self.s
        cid = self.put('mood1', cards(9))
        with patch.object(s, 'row_eligible', side_effect=lambda x, *a, **kw: int(x['id'][5:]) > 2), \
                patch.object(s, 'row_member', side_effect=lambda x, p, field: x['id'] == {'negative': 'tmdb:0', 'seen': 'tmdb:1'}.get(field)), \
                patch.object(s, 'quality_ok', side_effect=lambda x: x['id'] != 'tmdb:2'):
            result = why(s, cid)
        drops = {x['key']: x['removed_by'] for x in result['cards']}
        self.assertEqual([drops['tmdb:'+str(i)] for i in range(3)], ['negative', 'seen', 'quality'])
        self.assertEqual(result['own_cards'], 6)

    def test_source_status_from_fake_decisions(self):
        from skald.rowplan import why
        s = self.s
        cid = self.put('mood1', cards(6))
        theme = dict(kind='mood', key='quiet', name='Quiet stories', source=['calm'])
        pool = cards(2, 900)  # an empty pool now skips the decision
        self.enterContext(patch.object(s, 'taste_hint', return_value='synthetic taste'))
        for why_code, budget in (('BudgetExceeded', True), ('TimeoutError', False)):
            with patch.object(s, 'decide', return_value={'source': 'fallback', 'why': why_code, 'answer': {'months': None}}):
                self.assertIsNone(s.themed_row({}, {'seen': set(), 'negative': set()}, theme, pool, s.LANG, cid))
            status = why(s, cid)['ai_sourcing']
            self.assertEqual(status['skipped_by_budget'], budget)
            self.assertEqual(status['failed'], not budget)

    def test_commander_confirmation_and_localized_home_plan(self):
        s = self.s
        self.put('foryou', cards(6, 100))
        answer = dict(op='home_fewer', kind=None, values=['lane'], person=None)
        with patch.object(commands, 'context', return_value=('rowctl', 'ana', 'ana')), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            commands.special(s, 'a smaller selection of lane rows would help', 'movie', routed=True)
            pending = commands.get_pending(s, 'rowctl')
            self.assertTrue(pending)
            self.assertNotIn('home', commands.config(s)['household'])
            commands.special(s, 'yes '+commands.confirm_code(pending), 'movie')
            self.assertEqual(commands.config(s)['household']['home']['family_caps']['lane'], 2)
            commands.special(s, 'undo', 'movie')
            self.assertNotIn('home', commands.config(s)['household'])
        for lang, fragment in (('en-US', 'rows are selected'), ('de-DE', 'Reihen ausgewählt'), ('sr-RS', 'izabrano je')):
            with patch.object(s, 'LANG', lang):
                plan = commands.preview(s, 'synthetic read', 'ana', answer=dict(op='home_plan', kind=None, values=[], person=None))
            self.assertIn(fragment, plan['description'])

    def test_cap_bounds_unsafe_writes_and_refresh_undo_conflict(self):
        s = self.s
        for op, count in (('home_more', 8), ('home_fewer', 0)):
            cfg = commands.config(s)
            cfg['household']['home'] = {'family_caps': {'mood': count}}
            setup.publish(self.data, cfg, replace=True)
            with self.assertRaises(ValueError):
                commands.preview(s, 'synthetic', 'ana', answer=dict(op=op, kind=None, values=['mood'], person=None))
        for op, values in (('home_more', ['mood']), ('row_refresh', ['mood1'])):
            with self.assertRaises(PermissionError):
                commands.preview(s, 'synthetic', 'guest', answer=dict(op=op, kind=None, values=values, person=None))
        cid = self.put('mood1', cards(6))
        plan = commands.preview(s, 'synthetic', 'ana', answer=dict(op='row_refresh', kind=None, values=[cid], person=None))
        commands.apply(s, plan)
        s._feeds[cid, s.LANG]['stamp'] = 'new-build'
        with self.assertRaises(ValueError):
            commands.undo(s, ['ana'])

    def test_named_row_labels_are_in_decision_context(self):
        s = self.s
        cid = self.put('mood1', cards(6), title='Cozy invented row')
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(dict(op='row_refresh', kind=None, values=[cid], person=None))}}]})
            proposal = commands.preview(s, 'new picks in the cozy row', 'ana')
            prompt = replay.bodies[0]['messages'][1]['content']
        self.assertIn('Cozy invented row', prompt)
        self.assertIn(cid, prompt)
        self.assertEqual(proposal['refresh']['id'], cid)

    def test_sourcing_fresh_reused_and_plan_memory_unchanged(self):
        from skald.rowplan import why
        s = self.s
        cid = self.put('mood1', cards(6))
        theme = dict(kind='mood', key='quiet', name='Quiet stories', source=['calm'])
        pool = cards(2, 900)  # an empty pool now skips the decision
        with patch.object(s, 'taste_hint', return_value='synthetic taste'), \
                patch.object(s, 'decide', return_value={'source': 'llm', 'answer': {'fits_now': False, 'months': 'any'}}):
            self.assertEqual(s.themed_row({}, {}, theme, pool, s.LANG, cid), [])
        self.assertTrue(why(s, cid)['ai_sourcing']['fresh'])
        with patch.object(s, 'rank_by_taste', return_value=[]):
            self.assertEqual(s.themed_row({}, {'seen': set(), 'negative': set()}, theme, pool, s.LANG, cid), [])
        self.assertTrue(why(s, cid)['ai_sourcing']['reused_today'])
        before = copy.deepcopy((s._shown, s._shown_scoped, s._learn, s._feeds))
        self.plan('ana')
        self.assertEqual((s._shown, s._shown_scoped, s._learn, s._feeds), before)
        s._theme_sources.clear()
        with patch.object(s, 'taste_hint', return_value='synthetic taste'), \
                patch.object(s, 'decide', return_value={'source': 'cache', 'answer': {'fits_now': False, 'months': 'any'}}):
            s.themed_row({}, {}, theme, pool, s.LANG, cid)
        self.assertTrue(why(s, cid)['ai_sourcing']['reused_today'])

    def test_offline_source_status_from_feed_names(self):
        from skald.rowplan import why
        cid = self.put('mood1', cards(6), source_date='2026-10-14', source_status='budget')
        self.assertTrue(why(self.s, cid)['ai_sourcing']['skipped_by_budget'])
        self.assertEqual(why(self.s, cid)['ai_sourcing']['decided_date'], '2026-10-14')

    def test_commander_family_explanation(self):
        result = commands.preview(self.s, 'why no mood rows', 'ana',
                                  answer=dict(op='home_plan', kind=None, values=['moods'], person=None))
        self.assertTrue(result['read_only'])
        self.assertIn('No seed', result['description'])

    def test_polish_cap_and_tone_drops(self):
        from skald.rowplan import why
        s = self.s
        cid = self.put('mood1', cards(10, collection=42))
        with patch.object(s, 'polish_row', side_effect=lambda xs, **kw: xs[:6]):
            result = why(s, cid)
        self.assertEqual(sum(x['removed_by'] == 'cap' for x in result['cards']), 4)
        with patch.object(s, 'row_eligible', side_effect=lambda x, *a, **kw: x['id'] != 'tmdb:0'), \
                patch.object(s, 'quality_ok', return_value=True), \
                patch.object(s, 'tone_ok', side_effect=lambda x, *a, **kw: x['id'] != 'tmdb:0'):
            result = why(s, cid)
        self.assertEqual(next(x for x in result['cards'] if x['key'] == 'tmdb:0')['removed_by'], 'tone')

    def test_two_catalog_counts_and_thin_personal_row(self):
        s = self.s
        cid = s.rid('top10')
        s._feeds[cid, s.LANG] = {'metas': {'movie': cards(6), 'series': [{**x, 'type': 'series'} for x in cards(6, 100)]}}
        self.put('foryou', cards(3, 200))
        rows = {r['id']: r for r in self.plan()}
        self.assertEqual(rows[cid]['own_cards'], 12)
        self.assertEqual(rows[s.rid('foryou')]['rejection'], 'too_few_own')

    def test_cli_controls_publish_config_and_queue_refresh_without_feed_writes(self):
        from skald.__main__ import main
        cid = self.put('mood1', cards(6))
        self.s.save_feeds()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}):
            out = io.StringIO()
            self.assertEqual(main(['rows', 'prefer', '--theme', 'mood', '--more', '--json'], out=out), 0)
            preview = json.loads(out.getvalue())
            self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)
            self.assertEqual(main(['rows', 'prefer', '--theme', 'mood', '--more', '--plan', preview['plan'], '--json'], out=io.StringIO()), 0)
            self.s._feeds[cid, self.s.LANG]['built'] = 12345
            self.s.save_feeds()
            feeds = (self.data/'feeds.json').read_bytes()
            out = io.StringIO()
            self.assertEqual(main(['rows', 'refresh', 'mood1', '--yes', '--json'], out=out), 0)
            self.assertIn('queued', json.loads(out.getvalue())['name'].lower())
        self.assertEqual((self.data/'feeds.json').read_bytes(), feeds)
        self.assertEqual(json.loads((self.data/'row_refresh.json').read_text()), [cid])
        self.assertEqual(self.s._feeds[cid, self.s.LANG]['stamp'], 'current')
        self.s.refresh_preferences()
        self.assertEqual(self.s.CONFIG['household']['home']['family_caps']['mood'], 4)

    def test_refresh_request_is_consumed_before_worker_build(self):
        s = self.s
        a = self.put('mood1', cards(8))
        b = self.put('mood2', cards(8, 100))
        s._feeds[a, s.LANG]['stamp'] = 'v'+s.STAMP_V+'/today'
        s._feeds[b, s.LANG]['stamp'] = 'v'+s.STAMP_V+'/today'
        (self.data/'row_refresh.json').write_text(json.dumps([b]))
        builder = self.enterContext(patch.object(s, 'mood_feed', return_value={a: {'tv': cards(8)}, b: {'tv': cards(8, 200)}}))
        self.enterContext(patch.object(s, 'job_specs', return_value=[('moods', [a, b], 'today', 3600, builder)]))
        self.enterContext(patch.object(s, 'refresh_profiles', return_value={}))
        self.enterContext(patch.object(s, 'connectors_tick'))
        self.enterContext(patch.object(s, 'pin_decision'))
        retry = {('moods', s.LANG): s.time.time()+3600}
        s.feed_pass(s.local_now(), retry)
        self.assertEqual(builder.call_count, 1)
        self.assertFalse((self.data/'row_refresh.json').exists())
        self.assertEqual(s._feeds[b, s.LANG]['metas']['tv'], cards(8, 200))

    def test_refresh_requests_merge_and_invalidate_only_named_rows(self):
        from skald.rowplan import queue_refresh, consume_refresh
        s = self.s
        a = self.put('mood1', cards(6))
        b = self.put('shelf_ana_1', cards(6, 100))
        c = self.put('mood2', cards(6, 200))
        raw = s.rid('shelfraw_ana_1')
        s._feeds[raw, s.LANG] = {'window': 'always', 'candidates': cards(6, 100)}
        before = copy.deepcopy(s._feeds)
        queue_refresh(s, a)
        queue_refresh(s, b)
        queue_refresh(s, a)
        self.assertEqual(json.loads((self.data/'row_refresh.json').read_text()), [a, b])
        self.assertEqual(s._feeds, before)
        consume_refresh(s)
        self.assertNotIn('stamp', s._feeds[a, s.LANG])
        self.assertNotIn('stamp', s._feeds[b, s.LANG])
        self.assertNotIn('window', s._feeds[raw, s.LANG])
        self.assertEqual(s._feeds[c, s.LANG], before[c, s.LANG])
        self.assertFalse((self.data/'row_refresh.json').exists())

    def test_refresh_request_bounds_and_invalid_requests(self):
        from skald.rowplan import queue_refresh, consume_refresh
        s = self.s
        cid = self.put('mood1', cards(6))
        with self.assertRaises(ValueError):
            queue_refresh(s, s.rid('missing'))
        before = copy.deepcopy(s._feeds)
        for value in ([cid]*129, [cid, {'title': 'Invented'}], {'rows': [cid]}, [s.rid('missing')]):
            with self.subTest(value=value):
                (self.data/'row_refresh.json').write_text(json.dumps(value))
                consume_refresh(s)
                self.assertEqual(s._feeds, before)
                self.assertFalse((self.data/'row_refresh.json').exists())

    def test_cli_cannot_restore_worker_stamps_when_undoing_commander_refresh(self):
        from skald.__main__ import main
        cid = self.put('mood1', cards(6))
        proposal = commands.preview(self.s, 'synthetic', 'ana', answer=dict(op='row_refresh', kind=None, values=[cid], person=None))
        commands.apply(self.s, proposal)
        before = (self.data/'feeds.json').read_bytes()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}):
            out = io.StringIO()
            self.assertEqual(main(['command', '--person', 'ana', '--undo', '--json'], out=out), 2)
        self.assertIn('Commander', json.loads(out.getvalue())['error'])
        self.assertEqual((self.data/'feeds.json').read_bytes(), before)

    def test_guest_uses_public_row_view(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['digital']
        cid = s.rid('digital')
        xs = cards(8)
        s._feeds[cid, s.LANG] = {'metas': {'movie': xs}}
        s._profiles['household'] = {'seen': {x['id'] for x in xs[:5]}, 'negative': set()}
        with patch.object(s, 'title_view', return_value={'title': 'Invented card'}):
            result = {r['id']: r for r in self.plan('guest')}
        self.assertIsNotNone(result[cid]['position'])
        self.assertEqual(result[cid]['own_cards'], 8)

    def test_shelf_refresh_invalidates_only_its_source_window_and_undoes(self):
        s = self.s
        a = self.put('shelf_ana_1', cards(6))
        b = self.put('shelf_ana_2', cards(6, 100))
        raw_a, raw_b = s.rid('shelfraw_ana_1'), s.rid('shelfraw_ana_2')
        s._feeds[raw_a, s.LANG] = {'window': 'always', 'wish': 'invented-a', 'candidates': cards(6)}
        s._feeds[raw_b, s.LANG] = {'window': 'always', 'wish': 'invented-b', 'candidates': cards(6, 100)}
        before = copy.deepcopy(s._feeds)
        plan = commands.preview(s, 'synthetic', 'ana', answer=dict(op='row_refresh', kind=None, values=[a], person=None))
        commands.apply(s, plan)
        self.assertNotIn('window', s._feeds[raw_a, s.LANG])
        self.assertEqual(s._feeds[raw_b, s.LANG], before[raw_b, s.LANG])
        commands.undo(s, ['ana'])
        self.assertEqual(s._feeds, before)

    def test_cli_controls_refuse_a_household_without_an_adult(self):
        from skald.__main__ import main
        cfg = commands.config(self.s)
        for person in ('ana', 'ben'):
            cfg['taste']['people'][person]['prefs'] = {'kids_age': 8}
        setup.publish(self.data, cfg, replace=True)
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}):
            out = io.StringIO()
            self.assertEqual(main(['rows', 'prefer', '--theme', 'mood', '--more', '--json'], out=out), 2)
            self.assertIn('error', json.loads(out.getvalue()))


if __name__ == '__main__':
    unittest.main()
