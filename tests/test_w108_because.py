"""Distinct daily because shelves, synthetic titles and offline transports."""
import ast
import copy
import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server


class BecauseRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        s = self.s
        self.rows = [s.rid(key) for key in ('because', 'because2', 'because3')]
        self.p = {'entries': [], 'seen': set(), 'negative': set(), 'searches': []}
        self.recs, self.similar, self.facets = {}, {}, {}
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(s, 'CONFIG', copy.deepcopy(s.CONFIG)))
        self.enterContext(patch.dict(s._profiles, {'household': self.p}, clear=True))
        self.enterContext(patch.object(s, '_facets', self.facets))
        self.enterContext(patch.object(s, '_feeds', {}))
        self.enterContext(patch.object(s, '_shown', {}))
        self.enterContext(patch.object(s, '_shown_scoped', {}))
        self.enterContext(patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)))
        self.enterContext(patch.object(s, 'day_index', return_value=0))
        self.clock = self.enterContext(patch.object(s, 'time', wraps=s.time))
        self.clock.time.return_value = 1792000000
        self.enterContext(patch.object(s, 'get_model', return_value=None))
        self.enterContext(patch.object(s, 'genres', return_value={18: 'Drama', 16: 'Animation', 99: 'Documentary'}))
        self.enterContext(patch.object(s, 'seed_similarity', return_value=.8))
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            self.enterContext(patch.object(s, name, return_value=None))
        self.tmdb = self.enterContext(patch.object(s, 'tmdb', side_effect=self.transport))

    def transport(self, path, **params):
        kind, tid, endpoint = path.split('/')
        self.assertIn(kind, ('movie', 'tv'))
        self.assertIn(endpoint, ('recommendations', 'similar'))
        return {'results': (self.recs if endpoint == 'recommendations' else self.similar).get(int(tid), [])}

    def seed(self, i, signal='watched', age=1, weight=1, kind='movie', **facets):
        e = {'key': f'tmdb:{i}', 'id': i, 'title': f'Synthetic Area {i} Seed', 'kind': kind,
             'stype': 'movie' if kind == 'movie' else 'series', 'signal': signal, 'age': age, 'weight': weight,
             'by': ['ana'], 'item': {}, 'flags': [], 'genres': ['Drama']}
        self.p['entries'].append(e)
        self.p['seen'].add(e['key'])
        self.facets[self.s.fkey(e['key'], kind)] = {'title': e['title'], 'kind': kind, 'g': ['Drama'],
                                 'y': '2020', 'x': {tuple(self.s.EXPERIENCES)[i % len(self.s.EXPERIENCES)]: 3}, **facets}
        self.recs[i] = [self.item(i * 100 + n) for n in range(1, 21)]
        return e

    def item(self, i, **fields):
        return {'id': i, 'title': f'Synthetic {i} Card', 'genre_ids': [18],
                'vote_count': 500, 'vote_average': 8, 'poster_path': '/synthetic.jpg',
                'backdrop_path': '/synthetic.jpg', 'release_date': '2020-01-01', **fields}

    def built(self):
        return self.s.because_feed('en-US') or {}

    def stored(self, built):
        self.s._feeds.update({(cid, 'en-US'): {'metas': built.get(cid, {}),
                                             'names': built.get('_names', {}).get(cid, {}),
                                             'built': int(self.s.time.time())}
                              for cid in self.rows})

    def test_kind_retention_survives_seven_days_across_rebuilds(self):
        for i in range(1, 4):
            self.seed(i)
        first = self.built()
        self.stored(first)
        self.s._shown.update(copy.deepcopy(self.s._feeds))
        self.s._shown_scoped['synthetic'] = copy.deepcopy(self.s._feeds)
        series = self.seed(4, kind='tv')
        start = self.s.time.time()
        with patch.object(self.s, 'because_seeds', return_value=[series]):
            for day in range(1, 7):
                self.clock.time.return_value = start + day * self.s.DAY
                built = self.built()
                self.assertEqual(built[self.rows[0]], first[self.rows[0]])
                self.stored(built)
            self.clock.time.return_value = start + 7 * self.s.DAY
            built = self.built()
        self.assertEqual(built[self.rows[0]], first[self.rows[0]])
        self.assertEqual(self.s._feeds[self.rows[0], 'en-US']['names']['slot_since'], start)
        self.assertEqual(built['_names'][self.rows[0]]['slot_since'], start)

    def test_expired_slot_keeps_kind_when_series_seed_ranks_first(self):
        self.seed(1)
        first = self.built()
        self.stored(first)
        start = self.s.time.time()
        self.clock.time.return_value = start + 8 * self.s.DAY
        series = self.seed(2, kind='tv')
        movie = self.seed(3)
        with patch.object(self.s, 'because_seeds', return_value=[series, movie]):
            built = self.built()
        self.assertEqual(set(built[self.rows[0]]), {'movie'})
        self.assertEqual(built['_names'][self.rows[0]]['seed'], movie['key'])
        self.assertEqual(built['_names'][self.rows[0]]['slot_since'], start)

    def test_expired_empty_slot_can_change_kind_after_movie_seeds_fail(self):
        movie = self.seed(1)
        first = self.built()
        self.stored(first)
        self.s._shown.update(copy.deepcopy(self.s._feeds))
        self.p['seen'].update(x['id'] for x in first[self.rows[0]]['movie'])
        self.recs[1] = []
        self.clock.time.return_value += 8 * self.s.DAY
        series = self.seed(2, kind='tv')
        with patch.object(self.s, 'because_seeds', return_value=[series, movie]):
            built = self.built()
        self.assertEqual(set(built[self.rows[0]]), {'series'})
        self.assertEqual(built['_names'][self.rows[0]]['slot_since'], self.s.time.time())
        self.stored(built)
        new_movie = self.seed(3)
        with patch.object(self.s, 'because_seeds', return_value=[new_movie, series]):
            rebuilt = self.built()
        self.assertEqual(set(rebuilt[self.rows[0]]), {'series'})
        self.assertEqual(rebuilt['_names'][self.rows[0]]['slot_since'], self.s.time.time())

    def test_expired_mixed_slots_keep_kinds_during_simultaneous_rotation(self):
        for kinds in (('movie', 'tv', 'movie'), ('tv', 'movie', 'tv')):
            with self.subTest(kinds=kinds):
                self.p['entries'].clear()
                self.s._feeds.clear()
                for i, kind in enumerate(kinds, 1):
                    self.seed(i, kind=kind)
                first = self.built()
                self.stored(first)
                start = self.s.time.time()
                self.clock.time.return_value = start + 7 * self.s.DAY
                seeds = [self.seed(i, kind=kind) for i, kind in
                         enumerate((kinds[1], kinds[0], kinds[2]), 4)]
                with patch.object(self.s, 'because_seeds', return_value=seeds):
                    built = self.built()
                for cid in self.rows:
                    self.assertEqual(set(built[cid]), set(first[cid]))
                    self.assertIn(built['_names'][cid]['seed'], {e['key'] for e in seeds})
                    self.assertEqual(built['_names'][cid]['slot_since'], start)

    def test_fully_filtered_slot_tries_same_kind_seeds_before_changing(self):
        for days in (1, 8):
            with self.subTest(days=days):
                self.p['entries'].clear()
                self.p['seen'].clear()
                self.s._feeds.clear()
                self.seed(1)
                first = self.built()
                self.stored(first)
                start = self.s.time.time()
                self.p['seen'].update(x['id'] for x in first[self.rows[0]]['movie'])
                self.clock.time.return_value = start + days * self.s.DAY
                series = self.seed(2, kind='tv')
                movie = self.seed(3)
                with patch.object(self.s, 'because_seeds', return_value=[series, movie]):
                    built = self.built()
                self.assertEqual(set(built[self.rows[0]]), {'movie'})
                self.assertEqual(built['_names'][self.rows[0]]['seed'], movie['key'])
                self.assertEqual(built['_names'][self.rows[0]]['slot_since'], start)

    def test_fully_filtered_retained_cards_release_kind_before_deadline(self):
        for field in ('seen', 'negative', 'blocked', 'veto', 'preference', 'empty'):
            with self.subTest(field=field), patch.object(self.s, 'CONFIG', copy.deepcopy(self.s.CONFIG)):
                self.s._feeds.clear()
                self.p['entries'].clear()
                self.p['seen'].clear()
                self.p['negative'].clear()
                for i in range(1, 4):
                    self.seed(i)
                first = self.built()
                self.stored(first)
                ids = {x['id'] for cid in self.rows for x in first[cid]['movie']}
                masks = (set(), set(), set())
                if field in ('seen', 'negative'):
                    self.p[field].update(ids)
                elif field in ('blocked', 'veto'):
                    masks[0 if field == 'blocked' else 2].update(ids)
                elif field == 'preference':
                    prefs = self.s.CONFIG['taste']['people'].setdefault('ana', {}).setdefault('prefs', {})
                    prefs['excluded_genres'] = {'movie': ['Drama']}
                else:
                    for cid in self.rows:
                        self.s._feeds[cid, 'en-US']['metas'] = {}
                with patch.object(self.s, 'fill_row', side_effect=lambda xs, *a: xs):
                    self.assertFalse(self.s.served_row(self.rows[0], 'tv', 'en-US', self.p,
                                                       set(), masks, who='both'))
                series = self.seed(4, kind='tv')
                with patch.object(self.s, 'because_seeds', return_value=[series]), \
                        patch.object(self.s, 'row_masks', return_value=masks):
                    built = self.built()
                self.assertEqual(set(built[self.rows[0]]), {'series'})
                self.stored(built)
                self.assertTrue(self.s.served_row(self.rows[0], 'tv', 'en-US', self.p,
                                                  set(), masks, who='both'))

    def test_nonpositive_seed_releases_slot_to_series(self):
        movie = self.seed(1)
        self.stored(self.built())
        movie['weight'] = 0
        series = self.seed(2, kind='tv')
        with patch.object(self.s, 'because_seeds', return_value=[series]):
            built = self.built()
        self.assertEqual(set(built[self.rows[0]]), {'series'})
        self.assertEqual(built['_names'][self.rows[0]]['seed'], series['key'])

    def test_nonpositive_seed_tries_same_kind_before_series(self):
        previous = self.seed(1)
        self.stored(self.built())
        start = self.s.time.time()
        previous['weight'] = 0
        series = self.seed(2, kind='tv')
        movie = self.seed(3)
        with patch.object(self.s, 'because_seeds', return_value=[series, movie]):
            built = self.built()
        self.assertEqual(set(built[self.rows[0]]), {'movie'})
        self.assertEqual(built['_names'][self.rows[0]]['seed'], movie['key'])
        self.assertEqual(built['_names'][self.rows[0]]['slot_since'], start)

    def test_empty_language_slot_releases_without_copying_another_languages_cards(self):
        self.seed(1)
        first = self.built()
        self.stored(first)
        self.recs[1] = []
        series = self.seed(2, kind='tv')
        built = self.s.because_feed('de-DE')
        self.assertEqual(set(built[self.rows[0]]), {'series'})
        self.assertEqual(built['_names'][self.rows[0]]['seed'], series['key'])
        self.assertTrue({x['id'] for x in first[self.rows[0]]['movie']}.isdisjoint(
            x['id'] for x in built[self.rows[0]]['series']))

    def test_rejected_retained_seed_clears_cards_and_labels_but_keeps_kind(self):
        for signal, weight in (('disliked', -3), ('interest', .2), ('watched', 0)):
            with self.subTest(signal=signal, weight=weight):
                self.s._feeds.clear()
                self.p['entries'].clear()
                seed = self.seed(1)
                self.stored(self.built())
                seed.update(signal=signal, weight=weight)
                self.recs[1] = []
                built = self.built()
                self.assertEqual(built[self.rows[0]], {})
                self.assertEqual(built['_names'][self.rows[0]]['slot_kind'], 'movie')
                self.assertFalse({'seed', 'title', 'label_key', 'fields'} & built['_names'][self.rows[0]].keys())
                self.stored(built)
                self.assertIsNone(self.s.row_seed(self.rows[0], 'en-US'))

    def test_rebuilt_seed_prefers_its_previous_same_kind_slot(self):
        seeds = [self.seed(i) for i in range(1, 4)]
        self.stored(self.built())
        with patch.object(self.s, 'because_seeds', return_value=[seeds[2]]):
            built = self.built()
        self.assertEqual([built['_names'][cid]['seed'] for cid in self.rows],
                         [e['key'] for e in seeds])
        self.stored(built)
        self.s.CONFIG['household']['rows'] = ['because']
        home = self.s.build_home('en-US', self.p, (set(), set()), 'both')
        self.assertTrue(all(home[cid, 'tv'] for cid in self.rows))

    def test_retained_anchors_must_be_diverse_from_rebuilt_and_retained_rows(self):
        for facets in ({'collection': 90}, {'c': [[90, 'Marvel Studios']]}, {'x': {'dread': 3}}):
            for rebuild in (True, False):
                with self.subTest(facets=facets, rebuild=rebuild):
                    self.s._feeds.clear()
                    self.p['entries'].clear()
                    self.seed(1)
                    self.seed(2)
                    self.seed(3)
                    self.stored(self.built())
                    for i in (1, 2):
                        self.facets[f'tmdb:{i}'].update(facets)
                    with patch.object(self.s, 'because_seeds', return_value=self.p['entries'][:1] if rebuild else []):
                        built = self.built()
                    self.assertTrue(built[self.rows[0]])
                    self.assertEqual(built[self.rows[1]], {})
                    self.assertFalse({'seed', 'title', 'label_key', 'fields'} & built['_names'][self.rows[1]].keys())
                    self.assertTrue(built[self.rows[2]])

    def test_old_function_skips_when_149_source_is_unavailable(self):
        for error in (OSError('git missing'), subprocess.CalledProcessError(128, ['git', 'show'])):
            with self.subTest(error=type(error).__name__), patch.object(subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(unittest.SkipTest, r'the 1\.4\.9 source \(cc016e7\) is not in this checkout'):
                    self.old_function('because_feed')

    def test_three_areas_build_three_named_rows_from_both_endpoints(self):
        seeds = [self.seed(i, signal=signal) for i, signal in enumerate(('loved', 'liked', 'watched'), 1)]
        for e in seeds:
            self.similar[e['id']] = [self.item(e['id'] * 100 + 99), self.recs[e['id']][0]]
        built = self.built()
        self.assertEqual(set(built) - {'_names'}, set(self.rows))
        names = built['_names']
        self.assertEqual({n['seed'] for n in names.values()}, {e['key'] for e in seeds})
        for cid in self.rows:
            e = next(e for e in seeds if e['key'] == names[cid]['seed'])
            self.assertEqual(names[cid]['title'], self.s.seed_phrase(e))
            ids = [x['id'] for x in built[cid]['movie']]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertIn(f"tmdb:{e['id'] * 100 + 99}", ids)
        self.assertEqual({c.args[0] for c in self.tmdb.call_args_list},
                         {f"movie/{e['id']}/{endpoint}" for e in seeds for endpoint in ('recommendations', 'similar')})

    def test_same_franchise_universe_or_experience_does_not_take_two_slots(self):
        for facets in (({'collection': 90}, {'collection': 90}),
                       ({'c': [[90, 'Marvel Studios']]}, {'c': [[91, 'Marvel Entertainment']]}),
                       ({'x': {'dread': 3, 'whodunit': 2}}, {'x': {'dread': 3, 'whodunit': 1}})):
            with self.subTest(facets=facets):
                self.p['entries'].clear()
                self.seed(1, **facets[0])
                self.seed(2, **facets[1])
                self.seed(3)
                built = self.built()
                self.assertEqual(len(set(built) - {'_names'}), 2)
                chosen = {n['seed'] for n in built['_names'].values()}
                self.assertEqual(len(chosen & {'tmdb:1', 'tmdb:2'}), 1)
                self.assertIn('tmdb:3', chosen)

    def test_rotation_is_stable_within_day_and_moves_each_slot(self):
        seeds = [self.seed(i) for i in range(1, 7)]
        first = self.built()
        self.assertEqual(first, self.built())
        self.assertEqual(self.s.because_seed(self.p)['key'], first['_names'][self.rows[0]]['seed'])
        with patch.object(self.s, 'day_index', return_value=1):
            second = self.built()
        for cid in self.rows:
            self.assertNotEqual(first['_names'][cid]['seed'], second['_names'][cid]['seed'])
        used = set()
        for day in range(6):
            with patch.object(self.s, 'day_index', return_value=day):
                used.add(self.built()['_names'][self.rows[0]]['seed'])
        self.assertEqual(used, {e['key'] for e in seeds})

    def test_model_enriches_seed_facets_before_diversity_check(self):
        for i in range(1, 4):
            self.seed(i)
        def enrich(*args):
            for i in (1, 2):
                self.facets[f'tmdb:{i}'] = {**self.facets[f'tmdb:{i}'], 'collection': 90,
                                          'x': {'dread': 3}}
        with patch.object(self.s, 'get_model', side_effect=enrich):
            self.assertEqual(len(set(self.built()) - {'_names'}), 2)

    def test_recent_watches_mix_with_old_favourites_outside_recent_top_eight(self):
        recent = [self.seed(i) for i in range(1, 10)]
        favourite = self.seed(10, signal='loved', age=900, weight=3)
        chosen = {n['seed'] for n in self.built()['_names'].values()}
        self.assertIn(favourite['key'], chosen)
        self.assertTrue(chosen & {e['key'] for e in recent})
        first_slot = next(cid for cid, n in self.built()['_names'].items() if n['seed'] == favourite['key'])
        with patch.object(self.s, 'day_index', return_value=1):
            second_slot = next(cid for cid, n in self.built()['_names'].items() if n['seed'] == favourite['key'])
        self.assertNotEqual(first_slot, second_slot)

    def test_only_positive_signals_can_seed(self):
        self.seed(1, signal='interest', weight=5)
        self.seed(2, signal='opened', weight=2)
        self.seed(3, signal='disliked', weight=-3)
        self.seed(4, weight=0)
        self.assertFalse(set(self.built()) - {'_names'})
        self.assertIsNone(self.s.because_seed(self.p))
        self.tmdb.assert_not_called()

    def test_backfill_favourite_cannot_seed_when_real_watch_exists(self):
        real = self.seed(1)
        backfill = self.seed(2, signal='loved', age=float('inf'), weight=3)
        backfill['flags'] = ['backfill_only']
        with patch.object(self.s, 'top_seeds', return_value=[real]):
            self.assertEqual(self.s.because_seeds(self.p), [real])
        self.assertEqual(self.s.because_seeds(self.p), [real])
        self.p['entries'].remove(real)
        self.assertEqual(self.s.because_seeds(self.p), [backfill])

    def test_seed_identity_includes_kind(self):
        movie = self.seed(1, signal='loved')
        series = {**movie, 'kind': 'tv', 'stype': 'series', 'title': 'Synthetic Series Seed'}
        self.p['entries'].append(series)
        self.facets['tmdb:1@tv'] = {**self.facets[movie['key']], 'kind': 'tv', 'title': series['title'],
                                   'x': {'war_sacrifice': 3}}
        other = self.seed(2)
        seeds = self.s.because_seeds(self.p)
        self.assertEqual([(e['key'], e['kind']) for e in seeds],
                         [(movie['key'], 'movie'), (series['key'], 'tv'), (other['key'], 'movie')])
        built = self.built()
        self.assertEqual(set(built) - {'_names'}, set(self.rows))
        self.assertEqual([set(built[cid]) for cid in self.rows], [{'movie'}, {'series'}, {'movie'}])

    def test_seed_similarity_threshold_excludes_at_or_above_cutoff(self):
        for i in range(1, 4):
            self.seed(i)
        for similarity, count in ((.64, 3), (.65, 1), (.66, 1)):
            with self.subTest(similarity=similarity), patch.object(self.s, 'xsim', return_value=similarity):
                self.assertEqual(len(set(self.built()) - {'_names'}), count)

    def test_unusable_sixteen_seeds_bound_endpoint_calls_per_language(self):
        for i in range(1, 17):
            self.seed(i, signal='watched' if i <= 8 else 'loved', age=1 if i <= 8 else 900)
            self.recs[i] = []
        self.assertEqual(len(self.s.because_seeds(self.p)), 16)
        for lang in ('en-US', 'de-DE'):
            with self.subTest(lang=lang):
                self.tmdb.reset_mock()
                self.assertEqual(self.s.because_feed(lang), {'_names': {}})
                for language in {lang, 'en-US'}:
                    self.assertEqual(sum(c.kwargs['language'] == language for c in self.tmdb.call_args_list), 32)
                self.assertEqual(self.tmdb.call_count, 32 if lang == 'en-US' else 64)

    def test_unusable_candidates_skip_seed_and_refill_slot(self):
        for fields in ({'genre_ids': [16]}, {'genre_ids': [99]},
                       {'release_date': '2030-01-01'}, {'vote_average': 2}, {}):
            with self.subTest(fields=fields):
                self.p['entries'].clear()
                self.p['negative'].clear()
                # An animated seed may lead to animated titles, so only a non-animated seed tests the animation block.
                bad = self.seed(1, g=['Drama', 'Documentary'] if fields.get('genre_ids') == [16] else ['Drama', 'Animation', 'Documentary'])
                good = self.seed(2)
                self.recs[1] = [self.item(101, **fields)]
                if not fields:
                    self.p['negative'].add('tmdb:101')
                with patch.object(self.s, 'because_seed', return_value=bad), \
                        patch.object(self.s, 'row_animation', return_value=True):
                    built = self.built()
                self.assertEqual(set(built) - {'_names'}, {self.rows[0]})
                self.assertEqual(built['_names'][self.rows[0]]['seed'], good['key'])
                self.assertNotIn(bad['key'], {n['seed'] for n in built['_names'].values()})

    def test_no_usable_seed_clears_slots_and_manifest_hides_them(self):
        self.seed(1)
        self.recs[1] = []
        built = self.built()
        self.assertIsNotNone(self.s.because_feed('en-US'))
        self.assertFalse(set(built) - {'_names'})
        self.stored(built)
        self.s.CONFIG['household']['rows'] = ['because']
        with patch.object(self.s, 'pin_shown'), patch.object(self.s, 'fill_row', side_effect=lambda xs, *a: xs):
            catalogs = self.s.manifest('en-US')['catalogs']
        self.assertFalse(set(self.rows) & {c['id'] for c in catalogs})

    def test_registration_and_later_home_placement(self):
        s = self.s
        self.assertEqual(s.BECAUSE_ROWS, self.rows)
        for registry in (s.DEDUPE_ORDER, s.MIXED, s.LEARN_ROWS, s.DYNAMIC, s.REMOTE_ROWS):
            self.assertTrue(set(self.rows) <= set(registry))
        self.assertEqual(len(s.DEDUPE_ORDER), len(set(s.DEDUPE_ORDER)))
        self.assertLess(s.DEDUPE_ORDER.index(self.rows[0]), s.DEDUPE_ORDER.index(s.MOODS_ROWS[0]))
        self.assertGreater(s.DEDUPE_ORDER.index(self.rows[1]), s.DEDUPE_ORDER.index(s.MOODS_ROWS[-1]))
        self.assertEqual(next(job[1] for job in s.job_specs(s.local_now(), {}) if job[0] == 'because'), self.rows)
        self.assertEqual(s.row_groups()['because'], self.rows)
        self.assertTrue(set(self.rows) <= {r[0] for r in s.feed_rows('en-US')})

    def test_old_daily_stamp_rebuilds_all_slots_and_keeps_unused_ones(self):
        s = self.s
        for i in range(1, 4):
            self.seed(i)
        day = (s.local_now() - datetime.timedelta(hours=5)).strftime('%Y-%m-%d')
        stamp = f"v{s.STAMP_V}/{s.together_stamp(day + '/synthetic')}"
        s._feeds[self.rows[0], 'en-US'] = {'stamp': stamp, 'metas': {}}
        with patch.object(s, '_langs', {'en-US'}), patch.object(s, 'connectors_tick'), patch.object(s, 'save_feeds'):
            s.build_feeds(s.local_now(), {'household': 'synthetic'}, {}, only={'because'})
            self.assertTrue(all(s._feeds.get((cid, 'en-US'), {}).get('names', {}).get('seed') for cid in self.rows))
            previous = copy.deepcopy(s._feeds)
            with patch.object(s, 'because_seeds', return_value=self.p['entries'][:1]):
                s.build_feeds(s.local_now(), {'household': 'synthetic'}, {}, only={'because'}, force=True)
        for cid in self.rows[1:]:
            self.assertEqual(s._feeds[cid, 'en-US']['metas'], previous[cid, 'en-US']['metas'])
            self.assertEqual(s._feeds[cid, 'en-US']['names'], previous[cid, 'en-US']['names'])

    def old_function(self, name, namespace=None):
        try:
            source = subprocess.run(['git', 'show', 'cc016e7:server.py'], check=True,
                                    capture_output=True, text=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('the 1.4.9 source (cc016e7) is not in this checkout')
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace = self.s.__dict__.copy() if namespace is None else namespace
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'cc016e7:server.py', 'exec'), namespace)
        return namespace[name]

    def test_single_kind_slots_swap_seeds_instead_of_flipping(self):
        for kinds in (('movie', 'tv', 'movie'), ('tv', 'movie', 'tv')):
            with self.subTest(kinds=kinds):
                self.p['entries'].clear()
                self.s._feeds.clear()
                seeds = [self.seed(i, kind=kind) for i, kind in enumerate(kinds, 1)]
                previous = self.built()
                self.stored(previous)
                self.p['entries'] = [seeds[1], seeds[0], seeds[2]]
                built = self.built()
                self.assertEqual([set(built[cid]) for cid in self.rows],
                                 [set(previous[cid]) for cid in self.rows])
                self.assertEqual(built['_names'][self.rows[1]]['seed'], seeds[1]['key'])

    def test_no_same_kind_seed_keeps_previous_row_and_serving_filters(self):
        for i in range(1, 4):
            self.seed(i)
        previous = self.built()
        self.stored(previous)
        series = self.seed(4, kind='tv')
        with patch.object(self.s, 'because_seeds', return_value=[series]):
            built = self.built()
        for cid in self.rows:
            self.assertEqual(built[cid], previous[cid])
            self.assertEqual(built['_names'][cid]['seed'], previous['_names'][cid]['seed'])
            self.assertEqual(built['_names'][cid]['slot_kind'], 'movie')
        self.stored(built)
        removed = {built[self.rows[0]]['movie'][i]['id'] for i in (0, 1)}
        self.p['seen'].add(next(iter(removed)))
        self.p['negative'].update(removed)
        served = self.s.served_row(self.rows[0], 'tv', 'en-US', self.p, set(), (set(), set()), who='none')
        self.assertTrue(served)
        self.assertFalse(removed & {x['id'] for x in served})

    def test_unusable_same_kind_seed_keeps_previous_row(self):
        self.seed(1)
        previous = self.built()
        self.stored(previous)
        self.recs[1] = []
        built = self.built()
        self.assertEqual(built[self.rows[0]], previous[self.rows[0]])
        self.assertEqual(built['_names'][self.rows[0]]['seed'], 'tmdb:1')

    def test_first_build_matches_149_except_optional_kind(self):
        for i, kind in enumerate(('tv', 'movie', 'tv'), 1):
            self.seed(i, kind=kind)
        previous = self.old_function('because_feed')('en-US')
        built = self.built()
        for cid in self.rows:
            self.assertEqual(built['_names'][cid].pop('slot_kind'), next(iter(built[cid])))
            built['_names'][cid].pop('slot_since')
        self.assertEqual(built, previous)

    def test_mixed_slot_is_free_but_matching_slot_gets_seed_first(self):
        self.seed(1, kind='tv')
        self.seed(2)
        previous = self.built()
        # The first slot used to be mixed, the second used to be series-only.
        mixed = {'tv': previous[self.rows[0]]['series'] + previous[self.rows[1]]['movie']}
        previous[self.rows[1]] = previous[self.rows[0]]
        previous[self.rows[0]] = mixed
        previous['_names'] = {cid: {} for cid in self.rows}
        self.stored(previous)
        built = self.built()
        self.assertEqual(set(built[self.rows[0]]), {'movie'})
        self.assertEqual(set(built[self.rows[1]]), {'series'})

    def test_slot_kind_agrees_across_languages_and_survives_empty_cache(self):
        seeds = [self.seed(1), self.seed(2, kind='tv'), self.seed(3)]
        self.stored(self.built())
        self.p['entries'] = [seeds[1], seeds[0], seeds[2]]
        for lang in ('de-DE', 'en-US'):
            built = self.s.because_feed(lang)
            self.assertEqual([set(built[cid]) for cid in self.rows], [{'movie'}, {'series'}, {'movie'}])
        for cid, stype in zip(self.rows, ('movie', 'series', 'movie')):
            self.s._feeds[cid, 'en-US'] = {'metas': {}, 'names': {'slot_kind': stype}}
        self.assertEqual([set(self.built()[cid]) for cid in self.rows], [{'movie'}, {'series'}, {'movie'}])

    def test_served_pin_teaches_kind_before_an_unserved_build(self):
        self.seed(1)
        previous = self.built()
        self.stored(previous)
        self.s._shown.update(copy.deepcopy(self.s._feeds))
        series = self.seed(2, kind='tv')
        self.s._feeds[self.rows[0], 'en-US'] = {'metas': {'series': [{'type': 'series'}]}}
        with patch.object(self.s, 'because_seeds', return_value=[series]):
            built = self.built()
        self.assertEqual(built[self.rows[0]], previous[self.rows[0]])

    def test_149_loader_accepts_optional_slot_kind_and_restart_keeps_it(self):
        s = self.s
        self.seed(1)
        self.stored(self.built())
        s.save_feeds()
        stored = json.loads(Path(s.DATA, 'feeds.json').read_text())
        self.assertEqual(stored[self.rows[0] + '|en-US']['names']['slot_kind'], 'movie')
        self.assertEqual(stored[self.rows[0] + '|en-US']['names']['slot_since'], s.time.time())
        namespace = s.__dict__.copy()
        namespace.update(_feeds={}, _shown={}, _shown_scoped={}, _langs=set(), _learn=copy.deepcopy(s._learn))
        for name in ('connectors_load', 'load_usage', 'load_decisions', 'load_week_history', 'load_emb', 'load_facets'):
            namespace[name] = lambda: None
        self.old_function('load_state', namespace)()
        self.assertEqual(namespace['_feeds'], s._feeds)
        with patch.object(s, '_feeds', namespace['_feeds']):
            series = self.seed(2, kind='tv')
            with patch.object(s, 'because_seeds', return_value=[series]):
                self.assertEqual(self.built()[self.rows[0]], stored[self.rows[0] + '|en-US']['metas'])

    def test_non_because_fixture_feeds_are_byte_identical_to_149(self):
        s = self.s
        fixture = {s.rid('top10'): {'movie': [{'id': 'tmdb:9001', 'type': 'movie', 'name': 'Synthetic Chart'}]},
                   s.rid('rewatch'): {'series': [{'id': 'tmdb:9002', 'type': 'series', 'name': 'Synthetic Return'}]}}
        results = []
        with patch.object(s, '_langs', {'en-US'}), patch.object(s, 'connectors_tick'), \
                patch.object(s, 'top10_feed', return_value=fixture[s.rid('top10')]), \
                patch.object(s, 'rewatch_feed', return_value=fixture[s.rid('rewatch')]), \
                patch.object(s, 'time', wraps=s.time) as clock:
            clock.time.return_value = 1792000000
            for builder in (self.old_function('build_feeds'), s.build_feeds):
                s._feeds.clear()
                builder(s.local_now(), {}, {}, only={'top10', 'rewatch'}, force=True)
                results.append(Path(s.DATA, 'feeds.json').read_bytes())
        self.assertEqual(results[0], results[1])

    def test_explicit_rows_hide_new_slots_without_because_group(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou', 'unknown']
        self.assertTrue(all(not s.row_enabled(cid) for cid in self.rows))
        self.assertFalse(set(self.rows) & {r[0] for r in s.feed_rows('en-US')})
        s.CONFIG['household']['rows'] = ['because']
        self.assertTrue(all(s.row_enabled(cid) for cid in self.rows))
        self.assertEqual({r[0] for r in s.feed_rows('en-US')}, set(self.rows))

    def test_guests_never_receive_because_rows(self):
        s = self.s
        for cid in self.rows:
            self.assertIs(s.GUEST_ROW_POLICY[cid], False)
            self.assertNotIn(cid, s.GUEST_ROWS)
        with patch.object(s, 'row_list', return_value=[]):
            self.assertFalse(set(self.rows) & {r['id'] for r in s.guest_rows_view('en-US')['rows']})

    def test_all_slots_share_filters_rank_balance_and_full_home_dedupe(self):
        s = self.s
        for i in range(1, 4):
            self.seed(i)
        built = self.built()
        self.assertEqual(set(built) - {'_names'}, set(self.rows))
        shared = s.row_meta(self.item(9999), 'movie', 'movie', {18: 'Drama'}, {})
        for cid in self.rows:
            built[cid]['movie'][-1] = shared
        self.stored(built)
        s.CONFIG['household']['rows'] = ['because']
        for cid in self.rows:
            with patch.object(s, 'because_ok', return_value=False):
                self.assertFalse(s.row_eligible(shared, cid, 'en-US', self.p))
                self.assertFalse(s.row_related(shared, cid, 'en-US'))
        for name in ('polish_row', 'balance_row', 'rotate_row', 'remote_order', 'preference_order'):
            self.enterContext(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
        self.enterContext(patch.object(s, 'fill_row', side_effect=lambda xs, *a, **kw: xs))
        self.enterContext(patch.object(s, 'tired_keys', return_value={}))
        with patch.object(s, 'because_rank', wraps=s.because_rank) as rank, \
                patch.object(s, 'because_balance', wraps=s.because_balance) as balance:
            home = s.build_home('en-US', self.p, (set(), set()), 'both')
        ids = [x['id'] for cid in self.rows for x in home[cid, 'tv']]
        self.assertTrue(all(home[cid, 'tv'] for cid in self.rows))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids.count(shared['id']), 1)
        seeds = {n['seed'] for n in built['_names'].values()}
        self.assertEqual({c.args[1] for c in rank.call_args_list}, seeds)
        self.assertEqual({c.args[1] for c in balance.call_args_list}, seeds)

    def test_week_tail_protects_new_because_sources_and_refills(self):
        s = self.s
        head = [s.row_meta(self.item(8000 + i), 'movie', 'movie', {18: 'Drama'}, {}) for i in range(10)]
        protected = s.row_meta(self.item(9000), 'movie', 'movie', {18: 'Drama'}, {})
        for cid in self.rows[1:]:
            for source in ('built', 'refill'):
                with self.subTest(cid=cid, source=source), \
                        patch.object(s, 'row_list', side_effect=lambda row, *a: [protected] if row == cid and source == 'built' else []), \
                        patch.object(s, 'served_row', side_effect=lambda row, *a: [protected] if row == cid and source == 'refill' else []), \
                        patch.object(s, 'member_pick', return_value=False), patch.object(s, 'week_scores', return_value={}):
                    self.assertEqual(s.week_tail(head, 'en-US', self.p, set(), (set(), set()), [protected]), [])

    def test_week_tail_keeps_candidates_from_disabled_because_rows(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['week']
        head = [s.row_meta(self.item(8000 + i), 'movie', 'movie', {18: 'Drama'}, {}) for i in range(10)]
        candidate = s.row_meta(self.item(9000), 'movie', 'movie', {18: 'Drama'}, {})
        for cid in self.rows:
            for source in ('built', 'refill'):
                with self.subTest(cid=cid, source=source), \
                        patch.object(s, 'row_list', side_effect=lambda row, *a: [candidate] if row == cid and source == 'built' else []), \
                        patch.object(s, 'served_row', side_effect=lambda row, *a: [candidate] if row == cid and source == 'refill' else []), \
                        patch.object(s, 'member_pick', return_value=False), patch.object(s, 'week_scores', return_value={}):
                    tail = s.week_tail(head, 'en-US', self.p, set(), (set(), set()), [candidate])
                    self.assertEqual([x['id'] for x in tail], [candidate['id']])


if __name__ == '__main__':
    unittest.main()
