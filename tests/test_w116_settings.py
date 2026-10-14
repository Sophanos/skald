"""Settings boundaries use synthetic households and offline transports."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

from decide_kit import close_server, load_server
from skald import commands as c, health, members, migrate, rowplan, setup, shelves
from skald.__main__ import main
from test_w108_rows import cards


class SettingsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {'SKALD_CONFIG': '', 'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}))
        setup.publish(self.data, members.fresh_config(['ana', 'ben']))
        migrate.ensure(self.data)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, '_connector_open', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'aio_rows_changed'))
        self.enterContext(patch.object(self.s, 'frec', return_value={}))

    def preview(self, op, values, person='ana', **extra):
        return c.preview(self.s, 'synthetic setting', 'ana', answer=dict(op=op, kind=None, values=values, person=person, **extra))

    def stored(self):
        return {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file() and not p.name.endswith('.lock')}

    def wish(self, scope='ana'):
        c.apply(self.s, self.preview('shelf_add', ['invented stories', 'always'], scope))


class Settings(SettingsFixture):
    def test_invalid_shelf_filters_refused_in_preview_and_apply(self):
        self.wish()
        for filters in ({'runtime': -1}, {'runtime': 0}, {'runtime': 601}, {'runtime': True},
                        {'years': [2020, 1990]}, {'years': [0, 9999]}, {'years': [1, 10000]},
                        {'languages': ['german']}, {'languages': 'de'}, {'exclude_genres': ['Invented']},
                        {'kind': 'invented'}, {'countries': ['german']}):
            for phase in ('preview', 'apply'):
                with self.subTest(filters=filters, phase=phase):
                    before = self.stored()
                    plan = self.preview('shelf_filter', ['Invented Stories'], filters={'runtime': 120})
                    plan['rule']['after']['filters'] = filters
                    with self.assertRaises(ValueError):
                        self.preview('shelf_filter', ['Invented Stories'], filters=filters) if phase == 'preview' else c.apply(self.s, plan)
                    self.assertEqual(self.stored(), before)

    def test_shelf_filter_bounds_and_normalization(self):
        self.wish()
        for filters in ({'runtime': 1}, {'runtime': 600}, {'years': [1, 9999]}, {'years': [9999, 9999]},
                        {'languages': ['DE']}, {'exclude_genres': ['horror']}):
            with self.subTest(filters=filters):
                c.apply(self.s, self.preview('shelf_filter', ['Invented Stories'], filters=filters))
                saved = next(iter(shelves.rules(self.s).values()))['filters']
                expected = {'languages': ['de']} if 'languages' in filters else {'exclude_genres': ['Horror']} if 'exclude_genres' in filters else filters
                self.assertEqual(saved, expected)

    def test_named_horror_on_native_drama_excluded_from_both_rows(self):
        self.wish('ben')
        c.apply(self.s, self.preview('exclude_genre', ['Horror']))
        c.apply(self.s, self.preview('shelf_filter', ['Invented Stories'], 'ben', filters={'exclude_genres': ['Horror']}))
        meta = dict(id='tmdb:9001', type='series', name='Invented Lantern',
                    _row={'genre_ids': [18], 'g': {'Drama', 'Horror'}})
        self.assertFalse(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'))
        self.assertFalse(shelves.rule_ok(self.s, meta, next(iter(shelves.rules(self.s).values()))))
        s, safe = self.s, cards(6, 9100)
        wish = shelves.read(s)['wishes'][0]
        s._feeds[s.rid('foryou'), s.LANG] = {'metas': {'tv': [meta, *safe]}}
        s._feeds[s.rid('shelfraw_ben_1'), s.LANG] = {'wish': wish['id'], 'window': 'always', 'candidates': [meta, *safe]}
        with patch.object(s, 'get_model', return_value=None), patch.object(s, 'search_profile', return_value={}), \
                patch.object(s, 'member_scores', return_value={}), patch.object(s, 'row_eligible', return_value=True), \
                patch.object(s, 'fill_row', side_effect=lambda xs, *a: xs), \
                patch.object(s, 'person_order', side_effect=lambda xs, *a: xs):
            personal = s.served_row(s.rid('foryou'), 'tv', s.LANG, {}, set(), (set(), set()), who='ana')
            with s.addon_scope('ben'):
                shelf = s.shelf_items(s.rid('shelf_ben_1'), 'tv', s.LANG, who='ben')
        for row in (personal, shelf):
            self.assertEqual({x['id'] for x in row}, {x['id'] for x in safe})

    def test_runtime_at_limit_and_unusable_metadata(self):
        for kind in ('movie', 'series'):
            cfg = c.config(self.s)
            cfg['taste']['people']['ana']['prefs'] = {'runtime': {kind: 120}}
            setup.publish(self.data, cfg, replace=True)
            self.s.refresh_preferences()
            for runtime, allowed in ((120, True), (121, False), (None, False), ('120', False), (True, False), (-1, False)):
                with self.subTest(kind=kind, runtime=runtime):
                    meta = dict(id='tmdb:9001', type=kind, _row={'runtime': runtime})
                    self.assertEqual(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'), allowed)
                    self.assertEqual(shelves.rule_ok(self.s, meta, {'filters': {'runtime': 120}}), allowed)

    def test_legacy_zero_shelf_runtime_loads_and_serves_without_a_limit(self):
        self.wish()
        wish = shelves.read(self.s)['wishes'][0]
        xs = cards(6, 9200)
        for x, runtime in zip(xs, (None, 0, '120', True, -1, 700)):
            x['_row']['runtime'] = runtime
        rules_path = self.data / 'shelf_rules.json'
        rules_path.write_text(json.dumps({wish['id']: {'filters': {'runtime': 0}}}))
        raw = {'wish': wish['id'], 'window': 'always', 'candidates': xs}
        (self.data / 'feeds.json').write_text(json.dumps({self.s.rid('shelfraw_ana_1') + '|' + self.s.LANG: raw}))
        before = rules_path.read_bytes()
        other = load_server(self.data)
        try:
            other.load_state()
            with patch.object(other, 'get_model', return_value=None), \
                    patch.object(other, 'search_profile', return_value={}), \
                    patch.object(other, 'member_scores', return_value={}):
                with other.addon_scope('ana'):
                    shown = other.shelf_items(other.rid('shelf_ana_1'), 'tv', other.LANG, who='ana')
            self.assertEqual({x['id'] for x in shown}, {x['id'] for x in xs})
            self.assertEqual(rules_path.read_bytes(), before)
        finally:
            close_server(other)

    def test_personal_config_runtime_year_and_code_bounds(self):
        for prefs, allowed in (({'runtime': {'movie': 600}}, True), ({'runtime': {'movie': 601}}, False),
                               ({'years': [1, 9999]}, True), ({'years': [9999, 9999]}, True),
                               ({'years': [1, 10000]}, False), ({'focus': {'languages': ['DE'], 'countries': ['de']}}, True),
                               ({'focus': {'countries': ['germany']}}, False)):
            with self.subTest(prefs=prefs):
                cfg = c.config(self.s)
                cfg['taste']['people']['ana']['prefs'] = prefs
                if allowed:
                    self.s.validate_config(cfg)
                else:
                    with self.assertRaises(ValueError):
                        self.s.validate_config(cfg)

    def test_shelf_limits_zero_full_and_hidden_wishes_return(self):
        for scope in ('ana', 'together'):
            with self.subTest(scope=scope):
                c.apply(self.s, self.preview('shelf_count', [0], scope))
                before = self.stored()
                with self.assertRaisesRegex(ValueError, 'full'):
                    self.preview('shelf_add', ['invented extra', 'always'], scope)
                self.assertEqual(self.stored(), before)
                c.apply(self.s, self.preview('shelf_count', [10], scope))
                for n in range(10):
                    c.apply(self.s, self.preview('shelf_add', ['invented wish '+str(n), 'always'], scope))
                before = self.stored()
                with self.assertRaisesRegex(ValueError, 'full'):
                    self.preview('shelf_add', ['invented extra', 'always'], scope)
                with self.assertRaisesRegex(ValueError, 'no shelf slot'):
                    self.s.shelf_change(scope, 'add', text='invented extra', apply=True)
                self.assertEqual(self.stored(), before)
                wishes = copy.deepcopy(shelves.read(self.s)['wishes'])
                reply = c.apply(self.s, self.preview('shelf_count', [0], scope))
                self.assertIn('kept', reply['name'])
                self.assertEqual(shelves.read(self.s)['wishes'], wishes)
                self.assertFalse([cid for cid, p, n in shelves.slots(self.s, scope) if p == scope])
                c.apply(self.s, self.preview('shelf_count', [10], scope))
                self.assertEqual(len([cid for cid, p, n in shelves.slots(self.s, scope) if p == scope]), 10)
                self.assertEqual(shelves.read(self.s)['wishes'], wishes)

    def test_seasonal_shelf_logical_month_and_dst(self):
        wish = {'when': 'always'}
        berlin = ZoneInfo('Europe/Berlin')
        for now, months, active in ((dt.datetime(2026, 12, 1, 4, 59), [12], False),
                                    (dt.datetime(2026, 12, 1, 4, 59), [11], True),
                                    (dt.datetime(2026, 12, 1, 5), [12], True),
                                    (dt.datetime(2026, 12, 1, 5), [11], False),
                                    (dt.datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0), [10], True),
                                    (dt.datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=1), [10], True)):
            with self.subTest(now=now):
                self.assertEqual(shelves.window(wish, now, {'months': months}), 'always' if active else None)
        wish = {'when': {'days': ['sun'], 'hours': [5, 12]}}
        with patch.object(self.s, 'TZ', 'Europe/Berlin'):
            for minute, active in ((59, False), (0, True)):
                utc = dt.datetime(2026, 3, 29, 2 if minute else 3, minute, tzinfo=dt.timezone.utc)
                now = self.s.local_now(utc.timestamp())
                self.assertEqual(shelves.window(wish, now, {'months': [3]}), '2026-03-29' if active else None)

    def test_metrics_maxima(self):
        for field, maximum in (('attribution_days', 365), ('shown_retention_days', 365), ('shown_max_items', 100000)):
            for value in (maximum, maximum + 1):
                with self.subTest(field=field, value=value):
                    cfg = c.config(self.s)
                    cfg['metrics'] = {field: value}
                    if value == maximum:
                        self.assertEqual(self.s.validate_config(cfg)['metrics'][field], value)
                    else:
                        with self.assertRaisesRegex(ValueError, 'metrics.'+field):
                            self.s.validate_config(cfg)

    def test_legacy_health_thresholds_load_or_fall_back_without_writes(self):
        self.assertEqual(health.settings(self.data), health.DEFAULTS)
        for field in ('cooldown', 'max_attempts', 'disk_min_bytes', 'connector_failures'):
            for value, accepted in ((0, True), (1800.0, True), (1.5, True), (-1, False), ('3', False),
                                    (True, False), (None, False), (float('nan'), False), (float('inf'), False)):
                with self.subTest(field=field, value=value):
                    path = self.data / 'health-settings.json'
                    path.write_text(json.dumps({field: value}))
                    before = path.read_bytes()
                    expected = value if accepted else health.DEFAULTS[field]
                    self.assertEqual(health.settings(self.data)[field], expected)
                    self.assertFalse(health.Health(self.data, transport=Mock()).tick({})['enabled'])
                    self.assertEqual(path.read_bytes(), before)

    def test_disabled_legacy_health_settings_at_server_startup(self):
        tail = (Path(__file__).resolve().parents[1] / 'server.py').read_text().split('if __name__ == "__main__":', 1)[1]
        for value in (1800.0, -1, None):
            with self.subTest(cooldown=value):
                path = self.data / 'health-settings.json'
                path.write_text(json.dumps({'enabled': False, 'cooldown': value}))
                before = path.read_bytes()
                namespace = {**self.s.__dict__, 'API_PORT': None, 'addon_token': Mock(), 'remote_token': Mock(),
                             'load_state': Mock(), 'ThreadingHTTPServer': MagicMock(), 'start_feed_worker': Mock()}
                with patch('sys.argv', ['server.py']), patch('threading.Thread'):
                    exec('if True:' + tail, namespace)
                namespace['ThreadingHTTPServer'].return_value.__enter__.return_value.serve_forever.assert_called_once()
                self.assertIsNone(namespace['_health'])
                self.assertEqual(path.read_bytes(), before)

    def test_ai_environment_limits_refuse_invalid_values(self):
        settings = ['AI_DAILY_FEED_CALLS', 'AI_DAILY_TAG_CALLS', 'AI_DAILY_SEARCH_CALLS',
                    'AI_DAILY_RESOLVE_CALLS', 'AI_DAILY_EMBED_CALLS', 'AI_MONTHLY_USD', 'API_LANE_SHARE']
        for name in settings:
            values = ['-1', 'nan', 'inf', 'nonsense'] + (['1.01'] if name == 'API_LANE_SHARE' else [])
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        other = load_server(self.data)
                        close_server(other)
        for name in settings:
            for value in ('0', '1'):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    other = load_server(self.data)
                    close_server(other)

    def test_all_ai_lanes_at_daily_cap_boundaries(self):
        s = self.s
        for lane, maximum in s.AI_CAPS.items():
            with self.subTest(lane=lane, cap=0), patch.dict(s.AI_CAPS, {lane: 0}), \
                    patch.object(s, '_usage_roll'), patch.object(s, '_connector_open') as transport:
                s._usage.update(calls={}, month_cost=0)
                with self.assertRaises(s.BudgetExceeded):
                    s._openrouter('/api/v1/chat/completions', {}, 1, lane, 'synthetic')
                transport.assert_not_called()
            for used in (0, maximum - 1, maximum, maximum + 1):
                with self.subTest(lane=lane, used=used), patch.object(s, '_usage_roll'), \
                        patch.object(s, '_connector_open', side_effect=lambda *a, **kw: io.BytesIO(b'{}')) as transport:
                    s._usage.update(calls={lane+':synthetic': used}, month_cost=0)
                    if used < maximum:
                        s._openrouter('/api/v1/chat/completions', {}, 1, lane, 'synthetic')
                        transport.assert_called_once()
                        self.assertEqual(s._usage['calls'][lane+':synthetic'], used + 1)
                    else:
                        with self.assertRaises(s.BudgetExceeded):
                            s._openrouter('/api/v1/chat/completions', {}, 1, lane, 'synthetic')
                        transport.assert_not_called()
                        self.assertEqual(s._usage['calls'][lane+':synthetic'], used)

    def test_row_name_eval_language_tags(self):
        path = Path(__file__).resolve().parents[1] / 'eval/commands_cases.jsonl'
        rows = {r['id']: r for r in map(json.loads, path.read_text().splitlines())}
        for key in ('names-de', 'names-sr', 'names-cyrl', 'names-indirect-de', 'names-indirect-sr',
                    'names-lookalike-de', 'names-lookalike-sr', 'names-rename-de', 'names-rename-sr'):
            with self.subTest(key=key):
                self.assertEqual(rows[key].get('lang'), 'de' if key.endswith('-de') else 'sr')


class HomeSelection(SettingsFixture):
    def setUp(self):
        super().setUp()
        s = self.s
        self.enterContext(patch.object(s, 'get_model', return_value=None))
        self.enterContext(patch.object(s, 'row_masks', return_value=(set(), set())))
        self.enterContext(patch.object(s, 'who_watching', return_value=('both', None)))
        self.enterContext(patch.object(s, 'active_profiles', return_value=[]))
        self.enterContext(patch.object(s, 'feed', return_value={'metas': {}}))
        self.enterContext(patch.object(s, 'pin_shown'))
        self.enterContext(patch.object(s, 'day_index', return_value=1))
        empty = {s.MOODS_ROWS[0], s.LANES[0], s.STUDIO_ROWS[1]}
        def served(cid, typ, *args, **kwargs):
            return [] if cid in empty else [dict(x, type='series' if typ == 'series' else 'movie')
                for x in cards(6, s.DEDUPE_ORDER.index(cid)*100)]
        self.enterContext(patch.object(s, 'served_row', side_effect=served))

    def home_rows(self):
        return self.s.feed_rows()

    def default_home(self):
        s, rows = self.s, self.home_rows()
        ids = {r[0] for r in rows}
        self.assertFalse(ids & set(s.STUDIO_ROWS))
        self.assertEqual(ids & set(s.DOCS), {s.DOCS[1]})
        self.assertEqual(ids & set(s.MOODS_ROWS), set(s.MOODS_ROWS[1:4]))
        self.assertEqual(ids & set(s.LANES), set(s.LANES[1:4]))
        return rows

    def test_hide_from_default_only_removes_top10(self):
        before = self.default_home()
        c.apply(self.s, self.preview('row_hide', ['top10']))
        self.assertEqual(self.home_rows(), [r for r in before if r[0] != self.s.rid('top10')])

    def test_hide_then_show_returns_identical_default_home(self):
        before = self.default_home()
        c.apply(self.s, self.preview('row_hide', ['top10']))
        c.apply(self.s, self.preview('row_show', ['top10']))
        self.assertEqual(self.home_rows(), before)

    def test_show_studios_adds_only_the_populated_studio(self):
        self.default_home()
        before = self.s.manifest(self.s.LANG)['catalogs']
        c.apply(self.s, self.preview('row_show', ['studios']))
        after = self.s.manifest(self.s.LANG)['catalogs']
        self.assertEqual([r for r in after if r['id'] not in self.s.STUDIO_ROWS], before)
        added = [r for r in after if r['id'] in self.s.STUDIO_ROWS]
        self.assertEqual([(r['id'], r['type']) for r in added], [(self.s.STUDIO_ROWS[0], 'tv')])

    def test_explicit_selections_keep_all_selected_rows(self):
        s = self.s
        keys = ['top10', 'moods', 'lanes', 'docs', 'studios']
        expected = {s.rid('top10'), *s.MOODS_ROWS, *s.LANES, *s.DOCS, *s.STUDIO_ROWS}
        for source in ('setup', 'stored', 'false_marker'):
            with self.subTest(source=source):
                cfg = members.fresh_config(['ana', 'ben'])
                if source == 'setup':
                    setup.select_rows(cfg, ','.join(keys))
                else:
                    cfg['household']['rows'] = keys
                    if source == 'false_marker':
                        cfg['household']['rows_from_default'] = False
                setup.publish(self.data, cfg, replace=True)
                s.refresh_preferences()
                before = self.home_rows()
                self.assertEqual({r[0] for r in before}, expected)
                c.apply(s, self.preview('row_hide', ['top10']))
                self.assertEqual(self.home_rows(), [r for r in before if r[0] != s.rid('top10')])
                c.apply(s, self.preview('row_show', ['top10']))
                self.assertEqual(self.home_rows(), before)

    def test_undo_restores_exact_default_config_and_home(self):
        before, cfg = self.default_home(), c.config(self.s)
        for op, key in (('row_hide', 'top10'), ('row_show', 'studios')):
            with self.subTest(op=op):
                c.apply(self.s, self.preview(op, [key]))
                self.assertNotEqual(self.home_rows(), before)
                c.undo(self.s, ['ana'])
                self.assertEqual(c.config(self.s), cfg)
                self.assertEqual(self.home_rows(), before)

    def test_custom_labels_and_order_keep_existing_rows_after_hide(self):
        s = self.s
        for key, value in (('row_labels', {'foryou': 'Invented picks'}),
                           ('row_order', ['top10', 'moods', 'lanes', 'docs', 'studios'])):
            with self.subTest(key=key):
                cfg = members.fresh_config(['ana', 'ben'])
                cfg['household'][key] = value
                setup.publish(self.data, cfg, replace=True)
                s.refresh_preferences()
                before = self.home_rows()
                c.apply(s, self.preview('row_hide', ['top10']))
                self.assertEqual(self.home_rows(), [r for r in before if r[0] != s.rid('top10')])

    def test_child_selection_keeps_existing_nonlean_rows(self):
        s, cfg = self.s, c.config(self.s)
        cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
        setup.publish(self.data, cfg, replace=True)
        s.refresh_preferences()
        with s.addon_scope('ana'):
            before = self.home_rows()
            self.assertTrue(set(s.STUDIO_ROWS + s.DOCS) <= {r[0] for r in before})
            plan = c.preview(s, 'synthetic setting', 'ben', answer=dict(op='row_hide', kind=None, values=['top10'], person='ben'))
            c.apply(s, plan)
            self.assertEqual(self.home_rows(), [r for r in before if r[0] != s.rid('top10')])

    def test_materialization_respects_pending_row_customization(self):
        cfg = c.config(self.s)
        c.edit_rows(cfg, dict(op='row_rename', kind=None, values=['foryou', 'Invented picks']), self.s)
        c.edit_rows(cfg, dict(op='row_hide', kind=None, values=['top10']), self.s)
        self.assertIn('studios', cfg['household']['rows'])


class HomeLimits(SettingsFixture):
    def setUp(self):
        super().setUp()
        for name, value in {'get_model': None, 'row_pool': [], 'tired_keys': {}, 'memory_why': None,
                            'remote_masks': (set(), set()), 'veto_keys': set(), 'active_profiles': [],
                            'who_watching': ('both', None), 'explicit_seeds': [], 'week_ok': True, 'week_tail': []}.items():
            self.enterContext(patch.object(self.s, name, return_value=value))
        for name in ('row_eligible', 'row_related', 'lane_allowed', 'maker_view'):
            self.enterContext(patch.object(self.s, name, return_value=True))
        self.enterContext(patch.object(self.s, 'polish_row', side_effect=lambda xs, *a, **kw: xs))
        for i, cid in enumerate([*self.s.MOODS_ROWS, *self.s.LANES, self.s.rid('foryou')], 1):
            self.s._feeds[cid, self.s.LANG] = {'stamp': 'current', 'metas': {'tv': cards(6, i*100)}, 'names': {'lane': 'k:42'}}

    def shown(self):
        rows = rowplan.plan(self.s)['rows']
        return {family: sum(r['position'] is not None and r['id'] in ids for r in rows)
                for family, ids in (('mood', self.s.MOODS_ROWS), ('lane', self.s.LANES))}

    def test_legacy_caps_load_at_startup(self):
        cfg = c.config(self.s)
        for family in ('mood', 'lane'):
            for value in range(9):
                with self.subTest(family=family, value=value):
                    cfg['household']['home'] = {'family_caps': {family: value}}
                    (self.data / 'skald.json').write_text(json.dumps(cfg))
                    other = load_server(self.data)
                    try:
                        self.assertIsNone(other.CONFIG_ERROR)
                        self.assertEqual(other.CONFIG_SOURCE, 'file')
                        self.assertEqual(other.CONFIG['household']['home'], cfg['household']['home'])
                    finally:
                        close_server(other)

    def test_fewer_then_hide_and_show_never_raise_caps(self):
        c.apply(self.s, self.preview('home_fewer', ['mood']))
        before = self.shown()
        self.assertEqual(before, {'mood': 2, 'lane': 3})
        for op, key in (('row_hide', 'top10'), ('row_show', 'digital'), ('row_hide', 'moods'), ('row_show', 'moods')):
            with self.subTest(op=op, key=key):
                c.apply(self.s, self.preview(op, [key]))
                shown = self.shown()
                self.assertLessEqual(shown['mood'], before['mood'])
                self.assertLessEqual(shown['lane'], before['lane'])

    def test_hide_and_show_preserve_default_caps(self):
        before = self.shown()
        self.assertEqual(before, {'mood': 3, 'lane': 3})
        for op, key in (('row_hide', 'top10'), ('row_show', 'digital')):
            c.apply(self.s, self.preview(op, [key]))
            self.assertEqual(self.shown(), before)

    def test_default_row_selection_reloads_and_undo_restores_it(self):
        c.apply(self.s, self.preview('row_hide', ['top10']))
        self.assertTrue(self.s.public_config(self.s.CONFIG)['household']['rows_from_default'])
        self.assertTrue(c.about_detail(self.s, 'ana'))
        other = load_server(self.data)
        try:
            self.assertIsNone(other.CONFIG_ERROR)
            self.assertTrue(rowplan.capped(other.CONFIG))
            self.assertEqual(rowplan.family_caps(other.CONFIG), {'mood': 3, 'lane': 3})
            self.assertNotIn('home', other.CONFIG['household'])
        finally:
            close_server(other)
        c.undo(self.s, ['ana'])
        self.assertNotIn('rows', self.s.CONFIG['household'])
        self.assertNotIn('rows_from_default', self.s.CONFIG['household'])
        c.apply(self.s, self.preview('row_show', ['digital']))
        cfg = c.config(self.s)
        setup.select_rows(cfg, 'moods,lanes')
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        self.assertEqual(self.shown(), {'mood': 4, 'lane': 5})
        self.assertNotIn('rows_from_default', self.s.CONFIG['household'])

    def test_legacy_row_undo_without_default_marker(self):
        c.apply(self.s, self.preview('row_rename', ['moods', 'Invented moods']))
        entries = c.journal(self.s)
        for state in entries['ana'][-1]['rows'].values():
            state.pop('rows_from_default', None)
        c.save_journal(self.s, entries)
        c.undo(self.s, ['ana'])
        self.assertNotIn('row_labels', c.config(self.s)['household'])

    def test_explicit_full_selection_can_use_all_home_slots(self):
        cfg = c.config(self.s)
        cfg['household']['rows'] = ['moods', 'lanes']
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        self.assertEqual(self.shown(), {'mood': 4, 'lane': 5})
        c.apply(self.s, self.preview('row_hide', ['top10']))
        self.assertEqual(self.shown(), {'mood': 4, 'lane': 5})

    def test_explicit_rows_ignore_stored_caps_after_hide_and_show(self):
        cfg = c.config(self.s)
        cfg['household']['rows'] = ['moods', 'lanes']
        cfg['household']['home'] = {'family_caps': {'mood': 1, 'lane': 2}}
        setup.publish(self.data, cfg, replace=True)
        self.s.refresh_preferences()
        self.assertEqual(self.shown(), {'mood': 4, 'lane': 5})
        for op, key in (('row_hide', 'top10'), ('row_show', 'digital')):
            c.apply(self.s, self.preview(op, [key]))
            self.assertEqual(self.shown(), {'mood': 4, 'lane': 5})
        self.assertEqual(c.config(self.s)['household']['home'], cfg['household']['home'])

    def test_setup_default_rows_drops_hide_show_limits_and_keeps_prefer(self):
        for preferred in (False, True):
            with self.subTest(preferred=preferred):
                cfg = members.fresh_config(['ana', 'ben'])
                if preferred:
                    rowplan.prefer(cfg, 'mood', False)
                cfg['household']['rows'] = ['moods', 'lanes']
                setup.publish(self.data, cfg, replace=True)
                self.s.refresh_preferences()
                c.apply(self.s, self.preview('row_hide', ['top10']))
                with patch.dict(os.environ, {'DATA_DIR': str(self.data)}), \
                        patch('skald.aio_refresh.refresh', return_value=None):
                    self.assertEqual(main(['setup', 'rows', 'all', '--json'], io.StringIO()), 0)
                self.s.refresh_preferences()
                self.assertEqual(self.shown(), {'mood': 2 if preferred else 3, 'lane': 3})
                if not preferred:
                    self.assertNotIn('home', c.config(self.s)['household'])

    def test_home_slot_maxima_config_and_commander(self):
        for family, maximum in (('mood', 4), ('lane', 5)):
            for value in (0, maximum, 8):
                with self.subTest(family=family, value=value):
                    cfg = c.config(self.s)
                    cfg['household']['home'] = {'family_caps': {family: value}}
                    if value == 8:
                        setup.publish(self.data, cfg, replace=True)
                        self.s.refresh_preferences()
                        before = self.stored()
                        for op in ('home_more', 'home_fewer'):
                            with self.assertRaisesRegex(ValueError, f'Home maximum.*{maximum}'):
                                self.preview(op, [family])
                        self.assertEqual(self.stored(), before)
                        continue
                    setup.publish(self.data, cfg, replace=True)
                    self.s.refresh_preferences()
                    self.assertEqual(self.shown()[family], value)
                    before = self.stored()
                    if value == maximum:
                        with self.assertRaisesRegex(ValueError, f'Home maximum.*{maximum}'):
                            self.preview('home_more', [family])
                        self.assertEqual(self.stored(), before)
                    else:
                        c.apply(self.s, self.preview('home_more', [family]))
                        self.assertEqual(self.shown()[family], 1)
