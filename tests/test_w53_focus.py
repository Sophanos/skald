"""W53 preferences and country cache backfill; synthetic inputs only."""
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
import test_prefs
from test_rows_r2 import cards


class FocusConfig(unittest.TestCase):
    def test_normalization_and_bad_shapes(self):
        cfg = members.fresh_config(['alex'])
        prefs = {'hide': ['anime', 'anime'], 'focus': {'genres': {'movie': ['horror', 'HORROR'],
                 'series': ['sci-fi & fantasy']}, 'languages': ['ES', 'es'], 'countries': ['es', 'MX']}}
        cfg['taste']['people']['alex']['prefs'] = prefs
        normalized = members.validate_config(cfg)['taste']['people']['alex']['prefs']
        self.assertEqual(normalized['hide'], ['anime'])
        self.assertEqual(normalized['focus'], {'genres': {'movie': ['Horror'], 'series': ['Sci-Fi & Fantasy']},
                                              'languages': ['es'], 'countries': ['ES', 'MX']})
        self.assertEqual(members.public_config(members.validate_config(cfg))['taste']['people']['alex']['prefs'], normalized)
        for bad in ({'anime': 'hide'}, {'hide': 'anime'}, {'hide': [True]}, {'hide': ['unknown']}, {'focus': []}, {'focus': {'other': []}},
                    {'focus': {'genres': []}}, {'focus': {'genres': {'tv': []}}},
                    {'focus': {'genres': {'movie': ['Reality']}}}, {'focus': {'languages': 'es'}},
                    {'focus': {'languages': ['spa']}}, {'focus': {'countries': [True]}},
                    {'focus': {'countries': ['E1']}}):
            with self.subTest(bad=bad):
                cfg['taste']['people']['alex']['prefs'] = bad
                with self.assertRaises(members.ConfigError):
                    members.validate_config(cfg)

    def test_cli_partial_clear_and_invalid_preserve(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            path = Path(temp) / 'skald.json'
            setup.publish(Path(temp), members.fresh_config(['alex']))
            def run(*flags):
                return main(['setup', 'prefs', '--person', 'alex', *flags], out=io.StringIO())
            self.assertEqual(run('--hide', 'anime', '--focus-movie-genres', 'horror,drama',
                                 '--focus-series-genres', 'talk', '--focus-languages', 'ES', '--focus-countries', 'es,mx'), 0)
            self.assertEqual(run('--hide', 'documentaries,anime'), 0)
            before = path.read_bytes()
            for flags in (('--focus-movie-genres', 'invented'), ('--focus-languages', 'spa'),
                          ('--focus-countries', 'E1'), ('--hide', 'films'), ('--show', 'cartoons'), ('--clear', '--show', 'anime')):
                self.assertEqual(run(*flags, '--json'), 2)
                self.assertEqual(path.read_bytes(), before)
            out = io.StringIO()
            self.assertEqual(main(['setup', 'prefs', '--person', 'alex', '--hide', 'films', '--json'], out=out), 2)
            self.assertIn('movies, series, documentaries, animation, anime', out.getvalue())
            self.assertEqual(run('--focus-languages', '', '--show', 'anime'), 0)
            cfg = json.loads(path.read_text())
            prefs = cfg['taste']['people']['alex']['prefs']
            self.assertEqual(prefs['hide'], ['documentaries'])
            self.assertIn('hide documentaries', setup.prefs_line(cfg, 'alex'))
            self.assertEqual(prefs['focus']['languages'], [])
            self.assertEqual(prefs['focus']['countries'], ['ES', 'MX'])
            line = setup.prefs_line(cfg, 'alex')
            for part in ('Horror, Drama', 'Talk', 'ES, MX'):
                self.assertIn(part, line)
            self.assertNotIn('focus languages', line)
            for flag in ('--focus-movie-genres', '--focus-series-genres', '--focus-countries'):
                self.assertEqual(run(flag, ''), 0)
            self.assertEqual(run('--clear'), 0)
            self.assertNotIn('prefs', json.loads(path.read_text())['taste']['people']['alex'])


class FocusRows(unittest.TestCase):
    setUpClass = classmethod(test_prefs.PrefsRows.setUpClass.__func__)
    prefs = test_prefs.PrefsRows.prefs
    served = test_prefs.PrefsRows.served

    def test_anime_hide_keeps_cartoons_and_together_union(self):
        s = self.s
        xs = [{**x, '_row': {'g': ['Animation'], 'lang': lang}} for x, lang in zip(cards(3), ['ja', 'en', 'ko'])]
        with self.prefs(ana={'hide': ['anime']}), patch.object(s, 'frec', return_value={}):
            for who, expected in [('ana', xs[1:]), ('both', xs[1:]), ('ben', xs), (s.GUEST, xs)]:
                self.assertEqual(self.served(s.rid('leap'), xs, who=who), expected)
            self.assertFalse(s.preference_ok(xs[0], s.rid('ana'), 'both'))
        with self.prefs(ana={'hide': []}), patch.object(s, 'frec', return_value={}):
            self.assertEqual(self.served(s.rid('leap'), xs, who='ana'), xs)

    def test_hide_categories_kinds_and_together(self):
        s = self.s
        xs = [{**x, 'type': kind, '_row': {'genre_ids': [genre], 'g': [name], 'lang': lang}}
              for x, (kind, genre, name, lang) in zip(cards(6), [
                  ('movie', 99, 'Documentary', 'en'), ('series', 99, 'Documentary', 'en'),
                  ('movie', 16, 'Animation', 'ja'), ('series', 16, 'Animation', 'en'),
                  ('movie', 18, 'Drama', 'en'), ('series', 18, 'Drama', 'en')])]
        with patch.object(s, 'frec', return_value={}):
            for hide, dropped in [('documentaries', {0, 1}), ('animation', {2, 3}),
                                  ('anime', {2}), ('movies', {0, 2, 4}), ('series', {1, 3, 5})]:
                with self.subTest(hide=hide), self.prefs(ana={'hide': [hide]}):
                    expected = [x for i, x in enumerate(xs) if i not in dropped]
                    self.assertEqual(self.served(s.rid('leap'), xs, who='ana'), expected)
                    self.assertEqual([x for x in xs if s.preference_ok(x, s.rid('ana'), 'both')], expected)
                    self.assertEqual(self.served(s.rid('leap'), xs, who='both'),
                                     xs if hide in ('movies', 'series') else expected)
                    self.assertEqual(self.served(s.rid('leap'), xs, who='ben'), xs)
                    self.assertEqual(self.served(s.rid('leap'), xs, who=s.GUEST), xs)
            with self.prefs(ana={'hide': ['movies', 'series']}, ben={'hide': ['animation']}):
                self.assertEqual(self.served(s.rid('leap'), xs, who='ana'), [])
                self.assertEqual(self.served(s.rid('leap'), xs, who='both'), [xs[i] for i in [0, 1, 4, 5]])

    def test_started_rows_skip_all_preference_filters(self):
        s = self.s
        xs = [{**x, 'type': 'series', 'releaseInfo': '1980', 'description': '',
               '_row': {'g': ['Animation', 'Documentary'], 'lang': 'ja'}} for x in cards(6)]
        with self.prefs(ana={'hide': ['movies', 'series', 'documentaries', 'animation', 'anime'],
                            'years': [2000, None], 'excluded_genres': {'series': ['Animation']}}):
            cid = s.rid('episodes')
            for who in ('ana', 'both'):
                self.assertTrue(all(s.preference_ok(x, cid, who) for x in xs))
                self.assertEqual(self.served(cid, xs, who=who, typ='series'), xs)
            with patch.object(s, 'preference_genres', side_effect=AssertionError('must skip filters')):
                self.assertTrue(s.row_eligible(xs[0], cid, 'en-US', {'entries': []}))
            self.assertFalse(s.preference_ok(xs[0], s.rid('rewatch'), 'ana'))

    def test_catchup_keeps_preference_filters(self):
        s = self.s
        xs = [{**x, 'description': '', 'genres': ['Horror']} for x in cards(1)]
        for prefs in ({'hide': ['movies']}, {'excluded_genres': {'movie': ['Horror']}}):
            with self.subTest(prefs=prefs), self.prefs(ana=prefs), patch.object(s, 'frec', return_value={}):
                self.assertFalse(s.preference_ok(xs[0], s.rid('catchup'), 'ana'))
                self.assertEqual(self.served(s.rid('catchup'), xs, who='ana'), [])

    def test_hide_series_drops_member_top10_catalog(self):
        s = self.s
        cid = s.rid('top10')
        with patch.object(s, 'pin_shown'), patch.object(s, 'feed', return_value={}), \
                patch.object(s, 'feed_rows', return_value=[(cid, ['movie', 'series'], 'Top 10')]):
            for hide, kept in [('series', 'movie'), ('movies', 'series')]:
                with self.subTest(hide=hide), self.prefs(ana={'hide': [hide]}):
                    for scope in ('ana', 'ben', 'together', None):
                        kinds = [row['type'] for row in s.manifest('en-US', scope)['catalogs'] if row['id'] == cid]
                        self.assertEqual(kinds, [kept] if scope == 'ana' else ['movie', 'series'])

    def test_hide_series_removes_member_manifest_row(self):
        s = self.s
        cid = s.rid('airing')
        xs = [{**x, 'type': 'series'} for x in cards(s.ROW_MIN)]
        with self.prefs(ana={'hide': ['series']}), patch.object(s, 'pin_shown'), \
                patch.object(s, 'feed_rows', return_value=[(cid, ['series'], 'Synthetic row')]), \
                patch.object(s, 'feed', return_value={'synthetic': True}), \
                patch.object(s, 'row_metas', side_effect=lambda c, t, l, who=None: self.served(c, xs, who=who, typ=t)):
            for scope, visible in [('ana', False), ('ben', True), ('together', True)]:
                self.assertEqual(any(row['id'] == cid for row in s.manifest('en-US', scope)['catalogs']), visible)

    def test_focus_groups_stable_personal_only_and_old_countries(self):
        s = self.s
        xs = [{**x, '_row': {'genre_ids': gs, 'g': [], 'lang': lang}}
              for x, gs, lang in zip(cards(5), [[18], [27, 18], [27], [18], [18]], ['en', 'es', 'es', 'es', 'en'])]
        recs = {xs[1]['id']: {'oc': ['ES', 'MX']}, xs[4]['id']: {'oc': ['MX']}}
        focus = {'genres': {'movie': ['Horror', 'Drama']}, 'languages': ['es'], 'countries': ['ES', 'MX']}
        with self.prefs(ana={'focus': focus}), patch.object(s, 'frec', side_effect=lambda key, kind: recs.get(key, {})), \
                patch.object(s, 'tmdb') as tmdb:
            self.assertEqual(s.preference_order(xs, s.rid('leap'), 'ana'), [xs[i] for i in [1, 2, 3, 4, 0]])
            self.assertTrue(all(s.preference_ok(x, s.rid('leap'), 'ana') for x in xs))
            self.assertEqual(s.preference_order(xs, s.rid('leap'), 'both'), xs)
            self.assertEqual(s.preference_order(xs, s.rid('leap'), s.GUEST), xs)
            self.assertEqual(s.preference_order(xs, s.rid('ana'), 'both')[0], xs[1])
            localized = [{**x, 'type': 'series', '_row': {}, 'genres': ['localized']} for x in xs[:2]]
            with self.prefs(ana={'focus': {'genres': {'series': ['Sci-Fi & Fantasy']}}}), \
                    patch.object(s, 'frec', side_effect=lambda key, kind: {'g': ['Science Fiction', 'Fantasy']} if key == xs[1]['id'] else {}):
                self.assertEqual(s.preference_order(localized, s.rid('leap'), 'ana'), localized[::-1])
            tmdb.assert_not_called()
            series = [{**x, 'type': 'series'} for x in xs]
            self.assertEqual(s.preference_order(series, s.rid('leap'), 'ana'), [series[i] for i in [1, 2, 3, 4, 0]])

    def test_focus_after_discovery_and_hot_reload(self):
        s = self.s
        xs = [{**x, '_row': {'g': ['Drama'], 'lang': lang, 'rating': rating, 'popularity': pop}}
              for x, lang, rating, pop in zip(cards(3), ['en', 'es', 'es'], [9, 7, 8], [100, 50, 10])]
        with patch.object(s, 'frec', return_value={}), patch.object(s, 'get_model', return_value=None), \
                patch.object(s, 'score_key', return_value=(None,)), patch.object(s, 'match_pct', return_value=None):
            for style in ('mainstream', 'gems'):
                with self.prefs(ana={'discovery': style}):
                    base = s.preference_order(xs, s.rid('leap'), 'ana')
                with self.prefs(ana={'discovery': style, 'focus': {'languages': ['es']}}):
                    self.assertEqual(s.preference_order(xs, s.rid('leap'), 'ana'),
                                     [x for x in base if x['_row']['lang'] == 'es'] + [xs[0]])
        with tempfile.TemporaryDirectory() as temp:
            cfg = copy.deepcopy(s.CONFIG)
            cfg['taste']['people']['ana']['prefs'] = {'hide': ['anime'], 'focus': {'countries': ['es']}}
            path = Path(temp) / 'skald.json'
            path.write_text(json.dumps(cfg))
            with patch.dict(os.environ, {'SKALD_CONFIG': str(path)}), patch.object(s, 'CONFIG_SOURCE', 'file'), \
                    patch.object(s, 'CONFIG', copy.deepcopy(s.CONFIG)), patch.object(s, '_prefs_stamp', None):
                s.refresh_preferences()
                self.assertEqual(s.CONFIG['taste']['people']['ana']['prefs'], {'hide': ['anime'], 'focus': {'countries': ['ES']}})

    def test_country_facets_fallback_and_bounded_retry(self):
        s = self.s
        for detail, expected in [({'origin_country': ['ES', 'MX', 'AR', 'CL'], 'production_countries': [{'iso_3166_1': 'US'}]}, ['ES', 'MX', 'AR']),
                                 ({'production_countries': [{'iso_3166_1': 'US'}]}, ['US']), ({}, [])]:
            with patch.object(s, 'tmdb', return_value=detail):
                self.assertEqual(s.facet_record('movie', '1')['oc'], expected)
        facets = {f'tmdb:{i}': {'kind': 'movie'} for i in range(205)}
        facets['tmdb:1@tv'] = {'kind': 'tv'}
        with patch.object(s, '_facets', facets), patch.object(s, 'tmdb', return_value={'origin_country': ['ES']}) as tmdb, \
                patch.object(s, 'clear_seed_cache'):
            with self.prefs(ana={}):
                s.backfill_countries()
                tmdb.assert_not_called()
            with self.prefs(ana={'focus': {'countries': ['ES']}}):
                tmdb.side_effect = [OSError('synthetic failure')] + [{'origin_country': ['ES']}] * 199
                s.backfill_countries()
                self.assertEqual(tmdb.call_count, 200)
                self.assertNotIn('oc', facets['tmdb:0'])
                tmdb.side_effect = None
                tmdb.reset_mock()
                s.backfill_countries()
                self.assertEqual(tmdb.call_count, 7)
                self.assertEqual(facets['tmdb:0']['oc'], ['ES'])
                self.assertIn(unittest.mock.call('tv/1', language='en-US'), tmdb.call_args_list)
                self.assertTrue(s._facets_dirty.is_set())
        with patch.object(s, 'backfill_countries') as backfill, patch.object(s, 'refresh_preferences'), \
                patch.object(s, 'connectors_tick'), patch.object(s, 'feed_pass'), patch.object(s, 'daily_jobs'), \
                patch.object(s.time, 'sleep', side_effect=InterruptedError), patch.object(s, 'save_facets'), \
                patch.object(s, 'save_emb'), patch.object(s, 'save_learn'), patch.object(s, 'save_nzb'):
            with self.assertRaises(InterruptedError):
                s.feed_worker()
            backfill.assert_called_once_with()
