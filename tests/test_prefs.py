"""Per-member preferences, using only synthetic households and cards."""
from contextlib import ExitStack
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members, setup
from skald.__main__ import main
from eval.profile_golden import ROOT, FIXTURE, offline_server
import test_rows_r2
from test_rows_r2 import cards


class PrefsConfig(unittest.TestCase):
    def test_validation(self):
        for prefs in ({'discovery': 'surprise'}, {'years': [2000, 1990]},
                      {'years': [True, None]}, {'years': ['1990', None]},
                      {'years': [1990]}, {'years': [-1, None]},
                      {'excluded_genres': {'movie': ['Reality']}},
                      {'excluded_genres': {'series': ['invented']}}, {'discovery': []}):
            with self.subTest(prefs=prefs):
                cfg = members.fresh_config(['alex', 'sam'])
                cfg['taste']['people']['alex']['prefs'] = prefs
                with self.assertRaises(members.ConfigError):
                    members.validate_config(cfg)

    def test_case_insensitive_genres_and_public_config(self):
        cfg = members.fresh_config(['alex'])
        cfg['taste']['people']['alex']['prefs'] = {'discovery': 'gems', 'years': [1990, None],
            'excluded_genres': {'movie': ['hORror'], 'series': ['reality', 'TALK']}}
        cfg = members.validate_config(cfg)
        prefs = cfg['taste']['people']['alex']['prefs']
        self.assertEqual(prefs['excluded_genres']['movie'], ['Horror'])
        self.assertEqual(prefs['excluded_genres']['series'], ['Reality', 'Talk'])
        self.assertEqual(members.public_config(cfg)['taste']['people']['alex']['prefs'], prefs)


class PrefsRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    served = test_rows_r2.RowsR2.served

    def prefs(self, **people):
        cfg = copy.deepcopy(self.s.CONFIG)
        for person, prefs in people.items():
            cfg['taste']['people'].setdefault(person, {})['prefs'] = prefs
        return patch.object(self.s, 'CONFIG', cfg)

    def test_absent_and_balanced_are_byte_identical(self):
        stubs = json.loads((FIXTURE / 'tmdb.json').read_text())
        xs = [self.s.row_meta(item, 'movie' if kind == 'movie' else 'series', kind, self.s.genres(kind, 'en-US'), {})
              for kind, item in stubs['details'].values() if not item.get('raise')]
        cid = self.s.rid('leap')
        before = json.dumps(self.served(cid, xs, who='ana'))
        with self.prefs(ana={'discovery': 'balanced', 'years': [None, None], 'excluded_genres': {}}):
            self.assertEqual(json.dumps(self.served(cid, xs, who='ana')), before)

    def test_genres_years_personal_rows_and_search(self):
        s = self.s
        xs = [{**x, 'releaseInfo': str(1980 if i == 0 else 2000),
               'genres': ['Horror' if i == 1 else 'Drama']} for i, x in enumerate(cards(9))]
        prefs = {'years': [1990, 2010], 'excluded_genres': {'movie': ['Horror']}}
        with self.prefs(ana=prefs), patch.object(s, 'frec', return_value={}):
            for cid in [s.rid('leap'), s.rid('because'), *s.MOODS_ROWS, *s.LANE_ROWS, *s.STUDIO_ROWS]:
                with self.subTest(cid=cid), patch.object(s, 'because_rank', side_effect=lambda xs, *a: xs), \
                        patch.object(s, 'because_balance', side_effect=lambda xs, *a: xs):
                    result = self.served(cid, xs, who='ana')
                    self.assertEqual({x['id'] for x in result}, {x['id'] for x in xs[2:]})
            with patch.object(s, 'search_both', return_value=([{'id': 50, 'title': 'Synthetic Horror',
                    'release_date': '1980-01-01', 'genre_ids': [27]}], {})), \
                    patch.object(s, 'genres', return_value={27: 'Horror'}):
                self.assertTrue(s.search('movie', 'Synthetic Horror', 'en-US', title_only=True))

    def test_together_union_ignores_years_discovery_and_guest(self):
        s = self.s
        xs = [{**x, 'releaseInfo': '1980', 'genres': [g]} for x, g in zip(cards(3), ['Horror', 'Comedy', 'Drama'])]
        with self.prefs(ana={'years': [2000, None], 'discovery': 'gems', 'excluded_genres': {'movie': ['Horror']}},
                        ben={'excluded_genres': {'movie': ['Comedy']}}), patch.object(s, 'frec', return_value={}):
            self.assertEqual(self.served(s.rid('leap'), xs, who='both'), xs[2:])
            self.assertEqual(self.served(s.rid('leap'), xs, who=s.GUEST), xs)

    def test_discovery_reorders_and_filters_before_minimum(self):
        s = self.s
        xs = [{**x, '_row': {'rating': rating, 'votes': votes, 'popularity': pop, 'g': ['Drama']}}
              for x, rating, votes, pop in zip(cards(2), [8.8, 7.0], [100, 10000], [2, 100])]
        with patch.object(s, 'frec', return_value={}):
            for style, expected in [('mainstream', xs[::-1]), ('gems', xs)]:
                with self.prefs(ana={'discovery': style}):
                    self.assertEqual(self.served(s.rid('leap'), xs, who='ana'), expected)
            with self.prefs(ana={'excluded_genres': {'movie': ['Drama']}}):
                self.assertEqual(self.served(s.MOODS_ROWS[0], xs * 4, who='ana'), [])

    def test_kind_specific_exclusions_unknown_year_and_owner(self):
        s = self.s
        xs = [{**x, 'type': kind, 'genres': ['Comedy'], 'releaseInfo': year}
              for x, kind, year in zip(cards(4), ['movie', 'series', 'series', 'movie'], ['2000', '2000', '', ''])]
        with self.prefs(ana={'years': [1990, None], 'excluded_genres': {'series': ['Comedy']}}), \
                patch.object(s, 'frec', return_value={}):
            self.assertEqual([s.preference_ok(x, s.rid('leap'), 'ana') for x in xs], [True, False, False, False])
            self.assertEqual([s.preference_ok(x, s.rid('ana'), 'both') for x in xs], [True, False, False, False])
            self.assertEqual([s.preference_ok(x, s.rid('leap'), 'both') for x in xs], [True, False, False, True])

    def test_tmdb_ids_filter_localized_genres(self):
        s = self.s
        x = {**cards(1)[0], 'genres': ['Liebesfilm'], '_row': {'genre_ids': [10749]}}
        with self.prefs(ana={'excluded_genres': {'movie': ['Romance']}}), patch.object(s, 'frec', return_value={}):
            self.assertFalse(s.preference_ok(x, s.rid('leap'), 'ana'))

    def test_old_localized_series_uses_normalized_genres_but_ids_are_exact(self):
        s = self.s
        x = {**cards(1)[0], 'type': 'series', 'genres': ['Sci-Fi & Fantasy auf Deutsch'],
             '_row': {'votes': 100, 'rating': 8.0}}
        with self.prefs(ana={'excluded_genres': {'series': ['Sci-Fi & Fantasy']}}), \
                patch.object(s, 'frec', return_value={'g': ['Science Fiction', 'Fantasy']}):
            self.assertFalse(s.preference_ok(x, s.rid('leap'), 'ana'))
            x['_row']['genre_ids'] = [10765]
            self.assertFalse(s.preference_ok(x, s.rid('leap'), 'ana'))
            x['_row']['genre_ids'] = [18]
            self.assertTrue(s.preference_ok(x, s.rid('leap'), 'ana'))

    def test_unpublished_first_run_keeps_rows_working(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SKALD_CONFIG': str(Path(temp)/'skald.json')}), \
                patch.object(self.s, 'CONFIG_SOURCE', 'env'):
            self.s.refresh_preferences()

    def test_refills_and_week_tail_cannot_escape(self):
        s = self.s
        xs = [{**x, 'releaseInfo': '2000', 'genres': ['Drama']} for x in cards(10)]
        blocked = {**cards(1, 50)[0], 'releaseInfo': '1980', 'genres': ['Horror']}
        with self.prefs(ana={'years': [1990, None], 'excluded_genres': {'movie': ['Horror']}}), ExitStack() as stack:
            for name, value in {'row_eligible': True, 'row_related': True, 'row_member': False, 'get_model': None,
                                'feed': {}, 'tired_keys': {}, 'memory_why': None, 'frec': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            for name in ('balance_row', 'polish_row', 'rotate_row', 'remote_order', 'person_order',
                         'person_animation_sink', 'memory_reserve'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            stack.enter_context(patch.object(s, 'week_scores', return_value={}))
            stack.enter_context(patch.object(s, 'week_score', return_value=(0, 0)))
            stack.enter_context(patch.object(s, 'fill_row', side_effect=lambda xs, *a, **kw: xs + [blocked]))
            stack.enter_context(patch.object(s, 'week_tail', return_value=[blocked]))
            for suffix in ('week', 'foryou'):
                cid = s.rid(suffix)
                with patch.object(s, 'row_list', side_effect=lambda c, *a: xs.copy() if c == cid else []):
                    result = s.served_row(cid, 'tv', 'en-US', {}, set(), (set(), set()), who='ana')
                    self.assertEqual(result, xs)

    def test_live_config_change_invalidates_home_cache(self):
        s = self.s
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'skald.json'
            cfg = copy.deepcopy(s.CONFIG)
            path.write_text(json.dumps(cfg))
            with patch.dict(os.environ, {'SKALD_CONFIG': str(path)}), patch.object(s, 'CONFIG_SOURCE', 'file'), \
                    patch.object(s, 'CONFIG', cfg), patch.object(s, 'build_home', return_value={(s.rid('foryou'), 'tv'): []}) as build, \
                    patch.object(s, 'get_model', return_value=None), patch.object(s, 'remote_masks', return_value=(set(), set())), \
                    patch.object(s, 'veto_keys', return_value=set()):
                s.row_metas(s.rid('foryou'), 'tv', 'en-US', who='ana')
                cfg = copy.deepcopy(cfg)
                cfg['taste']['people']['ana']['prefs'] = {'discovery': 'gems'}
                setup.publish(Path(temp), cfg, replace=True)
                s.row_metas(s.rid('foryou'), 'tv', 'en-US', who='ana')
                self.assertEqual(build.call_count, 2)
                self.assertEqual(s.CONFIG['taste']['people']['ana']['prefs']['discovery'], 'gems')


class PrefsCLI(unittest.TestCase):
    def test_invalid_updates_preserve_config_and_partial_updates(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            data = Path(temp)
            cfg = setup.publish(data, members.fresh_config(['alex']))
            before = (data/'skald.json').read_bytes()
            for flags in (['--years', '2000-1990'], ['--years', 'bad'], ['--exclude-movie-genres', 'reality'],
                          ['--person', 'unknown', '--clear'], ['--clear', '--discovery', 'gems'], []):
                with self.subTest(flags=flags):
                    self.assertEqual(main(['setup', 'prefs', '--person', 'alex', '--json', *flags], out=io.StringIO()), 2)
                    self.assertEqual((data/'skald.json').read_bytes(), before)
            digest = setup.startup_digest(cfg)
            for flags in (['--discovery', 'gems'], ['--years=-2000'], ['--exclude-series-genres', 'talk']):
                self.assertEqual(main(['setup', 'prefs', '--person', 'alex', *flags], out=io.StringIO()), 0)
            cfg = members.validate_config(json.loads((data/'skald.json').read_text()))
            self.assertEqual(setup.startup_digest(cfg), digest)
            self.assertEqual(cfg['taste']['people']['alex']['prefs'], {'discovery': 'gems', 'years': [None, 2000],
                                                                    'excluded_genres': {'series': ['Talk']}})
            self.assertEqual((data/'skald.json').stat().st_mode & 0o777, 0o600)


    def test_set_clear_status_and_legacy_refusal(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            data = Path(temp)
            setup.publish(data, members.fresh_config(['alex', 'sam']))
            out = io.StringIO()
            self.assertEqual(main(['setup', 'prefs', '--person', 'alex', '--discovery', 'gems', '--years', '1990-',
                '--exclude-movie-genres', 'horror', '--exclude-series-genres', 'reality,talk'], out=out), 0)
            self.assertIn('gems', out.getvalue())
            cfg = json.loads((data/'skald.json').read_text())
            self.assertEqual(cfg['taste']['people']['alex']['prefs']['years'], [1990, None])
            out = io.StringIO()
            main(['setup', '--status'], out=out)
            self.assertIn('Prefs alex: gems; years 1990-', out.getvalue())
            self.assertIn('Prefs sam: balanced', out.getvalue())
            self.assertEqual(main(['setup', 'prefs', '--person', 'alex', '--clear'], out=io.StringIO()), 0)
            cfg = json.loads((data/'skald.json').read_text())
            self.assertNotIn('prefs', cfg['taste']['people']['alex'])
            (data/'legacy.json').write_text(json.dumps(cfg))
            (data/'skald.json').unlink()
            (data/'.skald-household').unlink()
            out = io.StringIO()
            self.assertEqual(main(['setup', 'prefs', '--person', 'alex', '--clear', '--json'], out=out), 2)
            self.assertIn('run skald setup adopt', out.getvalue())
