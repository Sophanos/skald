"""Home composition and feed regressions, stdlib only; SKALD_SNAPSHOT selects the real input."""
from skald.members import rid
from collections import Counter
import datetime
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from snapshot_support import require_snapshot
from eval.rows_report import home_server, display_rows, stub_refills, row_metrics, SNAPSHOT


class SnapshotRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snapshot = require_snapshot()
        if not snapshot.is_dir():
            raise unittest.SkipTest('set SKALD_SNAPSHOT to a home snapshot')
        cls.context = home_server(snapshot)
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)
        cls.rows = display_rows(cls.s)
        cls.source = {x['id']: x for x in cls.s.row_pool('de-DE')}

    def test_display_order_and_first_six_dedupe(self):
        s = self.s
        actual = list(dict.fromkeys(r['id'] for r in self.rows))
        self.assertEqual(actual, s.DEDUPE_ORDER)
        seen = set()
        for row in self.rows:
            ids = {x['id'] for x in row['metas']}
            if row['id'] not in (rid('rewatch', s.CONFIG), rid('top10', s.CONFIG)):
                blocked = seen if row['id'] in s.MOODS_ROWS + s.LANES else getattr(seen, 'deep', seen)
                self.assertFalse(ids & blocked, row['id'])
            seen = s.mark_above(seen, row['id'], row['metas'])
        ids = [x['id'] for r in self.rows[:6] for x in r['metas']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_negative_seen_and_names(self):
        p = self.s._profiles['household']
        for row in self.rows:
            for x in row['metas']:
                self.assertNotIn(x['id'], p['negative'])
                self.assertRegex(x['name'], '[A-Za-z]')
                self.assertNotIn('_row', x)
                if row['id'] == rid('top10', self.s.CONFIG):
                    self.assertNotIn(x['id'], p['seen'])

    def test_franchises_art_and_originals(self):
        for row in self.rows:
            keys = [self.s.franchise_key(x) for x in row['metas']]
            self.assertTrue(all(n <= 2 for n in Counter(keys).values()), row['id'])
            self.assertEqual(len(keys[:5]), len(set(keys[:5])), row['id'])
            found_missing = False
            for i, x in enumerate(row['metas']):
                art = bool(x.get('poster') and x.get('background'))
                if i < 5:
                    self.assertTrue(art, (row['id'], x['name']))
                    rating = self.s.row_facts(self.source[x['id']]).get('rating')
                    self.assertTrue(rating is None or rating >= 6.8, (row['id'], x['name']))
                if not art:
                    found_missing = True
                else:
                    self.assertFalse(found_missing, row['id'])
            for key, count in Counter(keys).items():
                if count > 1:
                    dates = [self.s.row_facts(self.source[x['id']]).get('date') for x in row['metas'] if self.s.franchise_key(x) == key]
                    self.assertEqual(dates, sorted(dates), (row['id'], key))

    def test_human_text(self):
        for row in self.rows:
            heads = []
            anchors = 0
            for x in row['metas']:
                desc = x.get('description', '')
                self.assertNotRegex(desc, r'(Strong fit|Good fit|Wildcard|[a-z]\.[a-z]{3,})')
                source = self.source[x['id']]
                overview = self.s.card_parts(source)[1]
                why = desc[:-len(overview)].rstrip(' ·') if overview and desc.endswith(overview) else ''
                if why:
                    heads.append(why)
                    anchors += bool(re.search(r'\b(like|loved|Because)\b', why))
                    title = (self.s._facets.get(x['id']) or {}).get('title') or x['name']
                    self.assertNotIn(' like ' + title, why)
                    self.assertEqual(len(why.split(' · ')), len(set(why.split(' · '))))
            self.assertTrue(all(n <= 2 for n in Counter(heads).values()), row['id'])
            self.assertLessEqual(anchors, 2, row['id'])

    def test_quality_tone_and_balance(self):
        curated = {rid('foryou', self.s.CONFIG), rid('week', self.s.CONFIG), rid('because', self.s.CONFIG), rid('leap', self.s.CONFIG), rid('', self.s.CONFIG) + self.s.PEOPLE[0]} | set(self.s.LANES + self.s.MOODS_ROWS + self.s.STUDIO_ROWS)
        hero_rows = 0
        for row in self.rows:
            metas = row['metas']
            source = [self.source[x['id']] for x in metas]
            if row['id'] in curated:
                for x in source:
                    self.assertTrue(self.s.quality_ok(x), (row['id'], x['name']))
                    seed = self.s.row_seed(row['id'], 'de-DE') if row['id'] in (rid('because', self.s.CONFIG), rid('leap', self.s.CONFIG)) else None
                    self.assertTrue(self.s.tone_ok(x, seed, animation=row['id'] == rid('because', self.s.CONFIG)), (row['id'], x['name']))
            if row['id'] in self.s.DOCS:
                self.assertTrue(all(self.s.doc_ok(x, row['id']) for x in source), row['id'])
            if row['id'] not in (rid('airing', self.s.CONFIG), rid('nextseason', self.s.CONFIG)):
                self.assertLessEqual(sum(self.s.is_anime(x) for x in source), len(source) * .4, row['id'])
            if row in self.rows[:16] and sum(self.s.is_hero(x) for x in source) > len(source) / 2:
                hero_rows += 1
        self.assertLessEqual(hero_rows, 2)
        foryou = self.rows[1]['metas']
        self.assertGreaterEqual(len(foryou), 20)
        self.assertGreaterEqual(sum(self.s.member_pick(self.source[x['id']], self.s.PEOPLE[0]) for x in foryou[:18]), 6)
        self.assertLessEqual(sum(self.s.is_hero(x) for x in foryou[:18]), 6)
        self.assertGreaterEqual(len(self.rows[0]['metas']), 6)
        self.assertGreaterEqual(len(self.rows[2]['metas']), 12)

    def test_because_cutoff_and_upcoming(self):
        row = next(r for r in self.rows if r['id'] == rid('because', self.s.CONFIG))
        seed = self.s.row_seed(rid('because', self.s.CONFIG), 'de-DE')
        for x in row['metas']:
            self.assertTrue(self.s.because_ok(self.source[x['id']], seed), x['name'])
            self.assertNotIn(x['name'], ('Elio', 'Shaft', 'Alles steht Kopf 2', '20.000 Meilen unter dem Meer'))
        row = next(r for r in self.rows if r['id'] == rid('upcoming', self.s.CONFIG))
        for x in row['metas']:
            self.assertNotIn('TBA', x.get('description', ''))
            self.assertTrue(self.s.upcoming_ok(self.source[x['id']]), x['name'])
        self.assertLessEqual(len(next(r for r in self.rows if r['id'] == rid('catchup', self.s.CONFIG))['metas']), 20)

    def test_because_medium_universe_and_week_intent(self):
        s = self.s
        row = next(r for r in self.rows if r['id'] == rid('because', s.CONFIG))['metas']
        self.assertGreaterEqual(len(row), 12)
        self.assertGreaterEqual(sum(x['type'] == 'movie' for x in row), len(row) * 2 / 3)
        for key, count in Counter(s.universe_key(x) for x in row if s.universe_key(x)).items():
            self.assertLessEqual(count, 4, key)
            self.assertLessEqual(sum(s.universe_key(x) == key for x in row[:5]), 2, key)
        self.assertTrue(s.because_ok(self.source['tmdb:280'], s.row_seed(rid('because', s.CONFIG), 'de-DE')))
        week = next(r for r in self.rows if r['id'] == rid('week', s.CONFIG))['metas']
        foryou = next(r for r in self.rows if r['id'] == rid('foryou', s.CONFIG))['metas']
        curated = {x['id'] for x in s.row_list(rid('week', s.CONFIG), 'tv', 'de-DE')}
        self.assertEqual({x['id'] for x in week[:5]}, curated)
        self.assertFalse({x['id'] for x in foryou[:10]} & {x['id'] for x in week[:10]})

    def test_refill_from_stubbed_sources_behind_pins(self):
        with home_server(require_snapshot()) as s:
            stub_refills(s)
            rows = display_rows(s)
            counts = {(r['id'], r['type']): len(r['metas']) for r in rows}
            for cid in s.LANES + [rid('leap', s.CONFIG), rid('mood3', s.CONFIG), rid('mood4', s.CONFIG)]:
                self.assertGreaterEqual(counts[(cid, 'tv')], 12, cid)
            for typ in ('movie', 'series'):
                self.assertEqual(counts[(rid('top10', s.CONFIG), typ)], 10)
            self.assertEqual(counts[(rid('episodes', s.CONFIG), 'series')], 11)
            self.assertEqual(s.lane_sibling(rid('lane1', s.CONFIG), 'de-DE'), rid('lane3', s.CONFIG))
            seen = set()
            for row in rows:
                ids = [x['id'] for x in row['metas']]
                self.assertEqual(len(ids), len(set(ids)), row['id'])
                if row['id'] not in (rid('rewatch', s.CONFIG), rid('top10', s.CONFIG)):
                    blocked = seen if row['id'] in s.MOODS_ROWS + s.LANES else getattr(seen, 'deep', seen)
                    self.assertFalse(set(ids) & blocked, row['id'])
                seen = s.mark_above(seen, row['id'], row['metas'])

    def test_review_targets_with_stub(self):
        with home_server(require_snapshot()) as s:
            stub_refills(s)
            week = s.row_metas(rid('week', s.CONFIG), 'tv', 'de-DE')
            ana = s.row_metas(rid('', s.CONFIG) + self.s.PEOPLE[0], 'tv', 'de-DE')
            because = s.row_metas(rid('because', s.CONFIG), 'tv', 'de-DE')
            foryou = s.row_metas(rid('foryou', s.CONFIG), 'tv', 'de-DE')
            self.assertGreaterEqual(len(week), 12)
            self.assertLessEqual(sum(s.is_hero(x) for x in week[:10]), 2)
            self.assertTrue(all(s.tone_ok(x, animation=True) for x in week[:10]))
            seeds = s.explicit_seeds(self.s.PEOPLE[0])
            curated_ids = {x['id'] for x in s.row_list(rid('week', s.CONFIG), 'tv', 'de-DE')}
            head = [x for x in week if x['id'] in curated_ids or s.week_ok(x)]
            pct = s.pool_pct({x['id']: s.seed_knn(seeds, x['id'], 1) for x in head})
            self.assertGreaterEqual(sum(pct[x['id']] >= .6 for x in week[:10]), 3)
            self.assertFalse({x['id'] for x in week[:10]} & {x['id'] for x in foryou[:10]})
            curated = s.row_list(rid('week', s.CONFIG), 'tv', 'de-DE')
            self.assertEqual([x['id'] for x in week[:5]], [x['id'] for x in curated])
            self.assertTrue(all(not s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv') and
                                s.femb(x['id'], 'movie' if x['type'] == 'movie' else 'tv') is None for x in curated))
            self.assertTrue(all(s.week_ok(x) for x in head[5:]))
            tail = [x for x in week if x not in head]
            self.assertTrue(all(s.week_tail_ok(x) for x in tail))
            self.assertLessEqual(sum(s.is_hero(x) for x in tail), 1)
            self.assertFalse({x['id'] for x in tail} & {x['id'] for x in week[:10]})
            self.assertGreaterEqual(sum(s.procedural(x) for x in ana[:10]), 2)
            self.assertLessEqual(sum(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv').get('x', {}).get('period_society', 0) >= 2
                                     for x in ana[:10]), 5)
            nearest = [max(seeds, key=lambda k: s.seed_similarity(k, x['id'])) for x in ana[:10]]
            self.assertTrue(all(n <= 4 for n in Counter(nearest).values()))
            metrics = row_metrics(s, {'metas': ana})
            self.assertEqual(metrics['dark_first10'], 0)
            self.assertLessEqual(metrics['dread_first10'], 1)
            self.assertLessEqual(metrics['dread_all'], 5)
            self.assertGreaterEqual(len(because), 12)
            self.assertGreaterEqual(sum(x['type'] == 'movie' for x in because), 6)
            self.assertTrue(all(x['name'] not in ('Hacks', 'Mr. Inbetween') for x in because))

    def test_daily_rotation_and_seed_variety(self):
        metas = [dict(id=f'tmdb:{i}', name=f'Film {i}', type='movie', poster='p', background='b') for i in range(20)]
        with patch.object(self.s, 'day_index', return_value=100):
            a = self.s.rotate_row(metas, rid('foryou', self.s.CONFIG))
            self.assertEqual(a, self.s.rotate_row(metas, rid('foryou', self.s.CONFIG)))
        with patch.object(self.s, 'day_index', return_value=101):
            b = self.s.rotate_row(metas, rid('foryou', self.s.CONFIG))
        self.assertEqual(a[:5], b[:5])
        self.assertGreaterEqual(sum(x != y for x, y in zip(a[5:], b[5:])), 5)
        for i, x in enumerate(a):
            self.assertLessEqual(abs(i - metas.index(x)), 5)
        seeds = set()
        for day in range(14):
            with patch.object(self.s, 'day_index', return_value=datetime.date(2026, 10, 11).toordinal() + day):
                seeds.add(self.s.because_seed(self.s._profiles['household'])['key'])
        self.assertGreaterEqual(len(seeds), 6)


class BuilderRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        spec = importlib.util.spec_from_file_location('row_builders', Path(__file__).resolve().parents[1] / 'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'DATA_DIR': temp.name, 'OPENROUTER_API_KEY': ''}), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            spec.loader.exec_module(cls.s)
        cls.s.local_now = lambda ts=None: datetime.datetime(2026, 10, 11, 18)
        cls.s.get_model = lambda build=True: None
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            setattr(cls.s, name, lambda *args, **kwargs: None)
        cls.s._profiles['household'] = {'entries': [dict(key='tmdb:1726', id=1726, title='Iron Man', weight=3,
                                                       age=1, signal='loved', stype='movie', kind='movie')],
                                       'seen': set(), 'negative': set()}
        cls.s._emb['tmdb:1726'] = [1., 0.]
        cls.s._facets['tmdb:1726'] = {'k': [[9715, 'superhero']], 'g': ['Action'], 'y': '2008'}
        for name in ('_pool', '_outer', '_nzb_pool'):
            cls.addClassCleanup(getattr(cls.s, name).shutdown)

    def setUp(self):
        p = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        p.start()
        self.addCleanup(p.stop)

    def item(self, tid=990001, **kw):
        return dict(id=tid, title='Original', name='Original', release_date='2000-01-01', first_air_date='2000-01-01',
                    genre_ids=[28], vote_count=500, vote_average=7.5, poster_path='/p', backdrop_path='/b',
                    overview='An ordinary overview.', original_language='en', **kw)

    def test_honest_rating_and_collection_cap(self):
        it = self.item(); it['vote_count'] = 49
        self.assertNotIn('imdbRating', self.s.to_meta(it, 'movie', 'movie', {}, it))
        it['vote_count'] = 50
        self.assertEqual(self.s.to_meta(it, 'movie', 'movie', {}, it)['imdbRating'], '7.5')
        metas = []
        for i in range(4):
            it = self.item(990001 + i); it['belongs_to_collection'] = {'id': 123}; it['release_date'] = f'200{i}-01-01'
            metas.append(self.s.row_meta(it, 'movie', 'movie', {}, it))
        extras = [self.s.row_meta({**self.item(990010 + i), 'title': title}, 'movie', 'movie', {}, {})
                  for i, title in enumerate(('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon'))]
        served = self.s.polish_row(list(reversed(metas)) + extras)
        self.assertEqual(sum(self.s.franchise_key(x) == 'collection:123' for x in served), 2)
        self.assertEqual(served[0]['id'], 'tmdb:990001')
        self.assertEqual(len({self.s.franchise_key(x) for x in served[:5]}), 5)
        self.assertEqual(self.s.franchise_key(metas[0]), 'collection:123')

    def test_because_builder_prefers_seed_similarity(self):
        s = self.s
        seed = s._profiles['household']['entries'][0]
        close, far, kids = self.item(), self.item(990002), self.item(990003)
        kids['genre_ids'] = [16, 10751]
        pairs = {'k:9715', 't:responsibility', 'd:123'}
        stats = ({fid: 2 for fid in pairs}, 100, {seed['key']: pairs, 'tmdb:990001': pairs})
        with patch.object(s, '_seed_stats', stats), \
                patch.object(s, 'because_seed', return_value=seed), patch.object(s, 'genres', return_value={}), \
                patch.object(s, 'tmdb', return_value={'results': [far, close, kids]}), \
                patch.dict(s._emb, {'tmdb:990001': [.2, (1 - .2 ** 2) ** .5], 'tmdb:990002': [.6, .8],
                                    'tmdb:990003': s._emb[seed['key']]}), \
                patch.dict(s._facets, {f'tmdb:{i}': {'g': ['Action'], 'k': [[9715, 'superhero']]} for i in (990001, 990002, 990003)}):
            self.assertLess(s.fit(s._emb[seed['key']], 'tmdb:990001'), s.fit(s._emb[seed['key']], 'tmdb:990002'))
            self.assertGreater(s.seed_similarity(seed['key'], 'tmdb:990001'), s.seed_similarity(seed['key'], 'tmdb:990002'))
            built = s.because_feed('en-US')[rid('because', s.CONFIG)]['movie']
        self.assertEqual([x['id'] for x in built], ['tmdb:990001', 'tmdb:990002'])

    def test_seed_similarity_cutoff_and_guards(self):
        s = self.s
        seed = 'tmdb:1726'
        meta = s.row_meta(self.item(), 'movie', 'movie', {}, {})
        with patch.object(s, 'seed_similarity', return_value=s.BECAUSE_CUTOFF) as similarity, \
                patch.dict(s._facets, {seed: {'y': '2008', 'g': ['Action'], 't': ['responsibility']},
                                      meta['id']: {'g': ['Action'], 't': ['responsibility']}}):
            self.assertTrue(s.because_ok(meta, seed))
            similarity.assert_called_once_with(seed, meta['id'])
            similarity.return_value = s.BECAUSE_CUTOFF - .001
            self.assertFalse(s.because_ok(meta, seed))
            similarity.return_value = 1
            meta['_row']['date'] = '1954-01-01'
            self.assertFalse(s.because_ok(meta, seed))
            meta['_row']['date'], meta['_row']['g'] = '2000-01-01', ['Family']
            self.assertFalse(s.because_ok(meta, seed))

    def test_because_requires_genre_franchise_or_universe_anchor(self):
        s = self.s
        meta = s.row_meta(self.item(), 'movie', 'movie', {}, {})
        seed = 'tmdb:1726'
        rec = {'g': ['Drama'], 'k': [[325778, 'bold']], 'title': 'Mood-only Match'}
        with patch.object(s, 'seed_similarity', return_value=1), \
                patch.dict(s._facets, {seed: {'kind': 'movie', 'y': '2008', 'title': 'Iron Man', 'g': ['Action'],
                                             'k': [[325778, 'bold']], 'c': [[420, 'Marvel Studios']]}, meta['id']: rec}):
            self.assertFalse(s.because_ok(meta, seed))
            rec['g'] = ['Action']
            self.assertTrue(s.because_ok(meta, seed))
            rec['g'], rec['c'] = ['Drama'], [[420, 'Marvel Studios']]
            self.assertTrue(s.because_ok(meta, seed))
            rec['c'], rec['title'] = [], 'Iron Man 4'
            self.assertTrue(s.because_ok(meta, seed))

    def test_pool_pct_ties_and_edges(self):
        pct = self.s.pool_pct
        self.assertEqual(pct({}), {})
        self.assertEqual(pct({'a': 5}), {'a': .5})
        self.assertEqual(pct({'a': 5, 'b': 5}), {'a': .5, 'b': .5})
        self.assertEqual(pct({'a': -10, 'b': 0, 'c': 0, 'd': 40}), {'a': 0, 'b': .5, 'c': .5, 'd': 1})
        self.assertEqual(pct({'a': 0, 'b': 0, 'c': 10}), {'a': .25, 'b': .25, 'c': 1})
        self.assertEqual(pct({'a': None, 'b': None}), {'a': 0, 'b': 0})
        self.assertEqual(pct({'a': None, 'b': -3, 'c': 7}), {'a': 0, 'b': 0, 'c': 1})

    def test_member_weight_steps(self):
        for n, weight in ((0, .8), (19, .8), (20, .5), (39, .5), (40, .2), (80, .2)):
            with self.subTest(n=n):
                self.assertEqual(self.s.member_weight(n), weight)

    def test_explicit_positive_seeds_and_member_percentile_blend(self):
        s = self.s
        entries = [dict(key=key, weight=weight, explicit=explicit)
                   for key, weight, explicit in (('loved', 3, True), ('shared', 2, False), ('negative', -3, True), ('loved', 3, True))]
        metas = [dict(id=key, type='movie') for key in ('a', 'b', 'c')]
        with patch.object(s, 'person_entries', return_value=entries), \
                patch.object(s, 'get_model', return_value={'nexp': {'ana': 4}}), \
                patch.object(s, 'seed_knn', side_effect=lambda seeds, key, k: {'a': 0, 'b': .9, 'c': .1}[key]) as knn, \
                patch.object(s, 'score_key', side_effect=lambda m, key, person, kind: ({'a': 100, 'b': 0, 'c': 50}[key], [])):
            self.assertEqual(s.explicit_seeds('ana'), ['loved'])
            scores = s.member_scores(metas, "ana")
            self.assertAlmostEqual(scores['a'], .2)
            self.assertAlmostEqual(scores['b'], .8)
            self.assertAlmostEqual(scores['c'], .5)
            self.assertTrue(all(call.args[0] == ['loved'] and call.args[2] == 1 for call in knn.call_args_list))

    def test_member_first10_seed_quota_and_dark_share(self):
        s = self.s
        metas = [dict(id=f'{seed}{i}', type='movie') for seed in 'abc' for i in range(8)]
        with patch.object(s, 'explicit_seeds', return_value=list('abc')), \
                patch.object(s, 'seed_similarity', side_effect=lambda seed, key: .9 if key.startswith(seed) else .1), \
                patch.object(s, 'frec', return_value={}):
            row = s.member_balance(metas, "ana")
            self.assertEqual([x['id'] for x in row[:4]], ['a0', 'a1', 'a2', 'a3'])
            self.assertTrue(all(n <= 4 for n in Counter(x['id'][0] for x in row[:10]).values()))
            self.assertEqual({x['id'] for x in row}, {x['id'] for x in metas})
            self.assertEqual(s.member_balance(metas[:8], "ana"), metas[:8])  # No other seed represented in this pool.
        with patch.object(s, 'explicit_seeds', return_value=list('abc')), \
                patch.object(s, 'seed_similarity', side_effect=lambda seed, key: .9 if key.startswith(seed) else .1), \
                patch.object(s, 'frec', side_effect=lambda key, kind: {'x': {'dread': 3}} if key.endswith('0') else {}):
            row = s.member_balance(metas, "ana")
            self.assertEqual(sum(x['id'].endswith('0') for x in row[:10]), 1)
        metas = [dict(id=f'{seed}{i}', type='movie') for seed in 'abc' for i in range(14)]
        with patch.object(s, 'explicit_seeds', return_value=list('abc')), \
                patch.object(s, 'seed_similarity', side_effect=lambda seed, key: .9 if key.startswith(seed) else .1), \
                patch.object(s, 'frec', side_effect=lambda key, kind: {'x': {'dread': 3}} if int(key[1:]) < 5 else {}):
            row = s.member_balance(metas, "ana")
            self.assertLessEqual(sum(int(x['id'][1:]) < 5 for x in row[:20]), 5)
            self.assertEqual(len(row), len(metas))

    def test_week_absolute_gate_and_shared_seed_floor(self):
        s = self.s
        meta = s.row_meta(self.item(), 'movie', 'movie', {}, {})
        model = {'nexp': {'ana': 4, 'ben': 40}}
        entries = [dict(key=k, by=by, weight=w, signal=signal) for k, by, w, signal in
                   [('shared', ['together'], 1, 'watched'), ('loved', ['together'], 3, 'loved'),
                    ('planned', ['together'], .2, 'interest'), ('negative', ['together'], -3, 'disliked'),
                    ('personal', ['ben'], 3, 'loved')]]
        with patch.dict(s._profiles, {'household': {'entries': entries}}), \
                patch.object(s, 'explicit_seeds', return_value=['own', 'shared']), \
                patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'score_key', return_value=(.001, [])) as score, \
                patch.object(s, 'seed_knn', return_value=s.WEEK_SEED_FLOOR) as knn:
            self.assertEqual(s.week_seeds('ana'), ['own', 'shared', 'loved'])
            self.assertTrue(s.week_ok(meta))
            score.assert_called_once_with(model, meta['id'], 'together', 'movie')
            knn.assert_called_once_with(['own', 'shared', 'loved'], meta['id'], 1)
            knn.return_value = s.WEEK_SEED_FLOOR - .001
            self.assertFalse(s.week_ok(meta))
            knn.return_value = 1
            score.return_value = (0, [])
            self.assertFalse(s.week_ok(meta))
            score.return_value = (.1, [])
            meta['_row']['g'] = ['Animation']
            self.assertFalse(s.week_ok(meta))
            meta['_row']['g'] = []
            model['nexp']['ana'] = 20
            knn.reset_mock()
            self.assertTrue(s.week_ok(meta))
            knn.assert_not_called()
            with self.assertRaises(TypeError):
                s.week_score(meta)

    def test_week_seed_fallback_household_and_freshness(self):
        s = self.s
        metas = [s.row_meta(self.item(990500 + i), 'movie', 'movie', {}, {}) for i in range(3)]
        keys = [x['id'] for x in metas]
        house = dict(zip(keys, (100, 0, 50)))
        ben = dict(zip(keys, (0, 100, 50)))
        seeds = dict(zip(keys, (0, .9, .1)))
        model = {'nexp': {'ana': 4, 'ben': 40}}
        def score(m, key, person=None, kind=None):
            self.assertNotEqual(person, 'together')
            self.assertEqual(kind, 'movie')
            return (ben if person == 'ben' else house)[key], []
        with patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'explicit_seeds', return_value=['loved']) as explicit, \
                patch.object(s, 'seed_knn', side_effect=lambda ss, key, k: seeds[key]) as knn, \
                patch.object(s, 'score_key', side_effect=score):
            low = s.week_scores(metas)
            self.assertAlmostEqual(s.week_score(metas[0], low)[0], .025)
            self.assertAlmostEqual(s.week_score(metas[1], low)[0], .925)
            self.assertAlmostEqual(s.week_score(metas[2], low)[0], .5)
            explicit.assert_called_once_with('ana')
            self.assertEqual(knn.call_count, 3)
            metas[1]['_row']['date'] = '2026-10-01'
            joint, fresh = s.week_score(metas[1], low)
            self.assertAlmostEqual(fresh, joint + .08)
            model['nexp']['ana'] = 20
            knn.reset_mock()
            rich = s.week_scores(metas)
            self.assertAlmostEqual(rich[keys[0]], .25)
            self.assertAlmostEqual(rich[keys[1]], .25)
            self.assertAlmostEqual(rich[keys[2]], .5)
            knn.assert_not_called()

    def test_report_seed_percentiles_and_dark_share(self):
        s = self.s
        metas = [dict(id=key, type='movie') for key in ('a', 'b', 'c')]
        with patch.object(s, 'person_entries', return_value=[dict(key='loved', explicit=True, weight=3)]), \
                patch.object(s, 'seed_knn', side_effect=lambda seeds, key, k: {'a': .1, 'b': .1, 'c': .9}[key]), \
                patch.object(s, 'frec', side_effect=lambda key, kind: {'x': {'dark': 3} if key == 'a' else {'dread': 3} if key == 'b' else {'banter_duo': 3}}):
            stats = row_metrics(s, {'metas': metas})
            self.assertEqual(stats['ana_seed_pct'], {'a': .25, 'b': .25, 'c': 1})
            self.assertEqual((stats['dark_first10'], stats['first10_n'], stats['dark_all'], stats['all_n']), (1, 3, 1, 3))
            self.assertEqual((stats['dread_first10'], stats['dread_all']), (1, 1))

    def test_universe_caps_metadata_and_prefixes(self):
        s = self.s
        cases = [('marvel', 'Marvel’s Sentinel', {}), ('dc', 'DC Sentinel', {}),
                 ('starwars', 'Star Wars: Sentinel', {}), ('wizarding', 'Fantastic Beasts: Sentinel', {}),
                 ('marvel', 'Sentinel', {'production_companies': [{'id': 420, 'name': 'Marvel Studios'}]}),
                 ('dc', 'Sentinel', {'keywords': {'results': [{'id': 1, 'name': 'dc universe (dcu)'}]}}),
                 ('starwars', 'Sentinel', {'belongs_to_collection': {'id': 1, 'name': 'Star Wars Collection'}}),
                 ('wizarding', 'Sentinel', {'keywords': {'keywords': [{'id': 2, 'name': 'wizarding world'}]}})]
        for expected, title, fields in cases:
            it = {**self.item(), 'title': title, **fields}
            self.assertEqual(s.universe_key(s.row_meta(it, 'movie', 'movie', {}, it)), expected)
        names = ['Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta', 'Eta', 'Theta', 'Iota', 'Kappa']
        metas = [s.row_meta({**self.item(990100 + i), 'title': name,
                            'production_companies': [{'id': 420, 'name': 'Marvel Studios'}] if i < 6 else []},
                           'movie', 'movie', {}, {}) for i, name in enumerate(names)]
        served = s.polish_row(metas)
        self.assertEqual(sum(s.universe_key(x) == 'marvel' for x in served), 4)
        self.assertLessEqual(sum(s.universe_key(x) == 'marvel' for x in served[:5]), 2)

    def test_because_balance_for_movie_and_series_seeds(self):
        s = self.s
        metas = [dict(id=f'tmdb:{i}', type='movie' if i < 9 else 'series') for i in range(18)]
        for kind, typ in [('movie', 'movie'), ('tv', 'series')]:
            with patch.dict(s._facets, {'tmdb:1': {'kind': kind}}):
                row = s.because_balance(metas, 'tmdb:1')
                self.assertGreaterEqual(sum(x['type'] == typ for x in row), len(row) * 2 / 3)

    def test_saga_horizon_and_catchup_numbering(self):
        today = datetime.date(2026, 10, 11)
        soon, late = self.item(), self.item(990002)
        soon['release_date'], late['release_date'] = '2026-12-11', '2028-01-01'
        with patch.object(self.s, 'tmdb', return_value={'name': 'Saga', 'parts': [late, soon]}), \
                patch.object(self.s, 'genres', return_value={}):
            self.assertEqual(len(self.s.saga_upcoming(123, 'en-US', today, set())), 1)
        det = self.item()
        det.update(seasons=[{'season_number': 1, 'episode_count': 25}, {'season_number': 2, 'episode_count': 25},
                            {'season_number': 3, 'episode_count': 30, 'air_date': '2026-10-01'}],
                   last_episode_to_air={'season_number': 3, 'episode_number': 30, 'air_date': '2026-10-10'})
        e = dict(id=990001, item={'last_episode': [1, 63], 'episodes_played': 63}, weight=2)
        with patch.object(self.s, 'tmdb', return_value=det):
            got = self.s.show_catchup(e, 'en-US', today)
        self.assertIn('Episode 64', got[2]['description'])
        self.assertNotIn('S1E64', got[2]['description'])
        self.assertEqual(got[0], 0)

    def test_confidence_dedup_and_leaks(self):
        it = self.item()
        cards = [self.s.row_meta({**it, 'id': 990001 + i}, 'movie', 'movie', {}, it,
                                note='Strong fit · Strong fit · comfort anime.favorite') for i in range(4)]
        served = self.s.human_cards(cards)
        self.assertEqual(sum('comfort anime.' in x['description'] for x in served), 2)
        self.assertTrue(all('fit' not in x['description'] and 'favorite' not in x['description'] for x in served))

    def test_lane_builder_tone_and_quality(self):
        good, kids, low, scant = [self.item(990100 + i) for i in range(4)]
        kids['genre_ids'], low['vote_average'], scant['vote_count'] = [10751], 5.9, 10
        with patch.object(self.s, 'tmdb', return_value={'results': [kids, low, scant, good]}):
            got = self.s.lane_titles('k:123', 'en-US')
        self.assertEqual([x[0] for x in got], ['tmdb:990100'])
        fresh = self.s.row_meta({**scant, 'release_date': '2026-10-01'}, 'movie', 'movie', {}, scant)
        self.assertTrue(self.s.quality_ok(fresh))

    def test_doc_builder_floor_and_music(self):
        good, music, low = [self.item(990200 + i) for i in range(3)]
        good['genre_ids'], music['genre_ids'], low['genre_ids'] = [99], [99, 10402], [99]
        low['vote_average'] = 2.6
        with patch.object(self.s, 'tmdb', return_value={'results': [low, music, good]}):
            got = self.s._doc_pool('en-US', set(), set(), [('movie', False, {})])
        self.assertEqual(list(got), ['tmdb:990200'])

    def test_seen_new_season_exception_and_negatives(self):
        s = self.s
        cards = [s.row_meta(self.item(990400 + i), 'series', 'tv', {}, {}, note='Starts 20 Oct') for i in range(4)]
        cards[1]['_row']['status'] = 'Ended'
        p = {'seen': {x['id'] for x in cards}, 'negative': {'tmdb:990403'},
             'entries': [{'key': x['id'], 'title': x['name'], 'item': {'completed': i == 2}} for i, x in enumerate(cards)]}
        with patch.dict(s._profiles, {'household': p}), \
                patch.dict(s._feeds, {(rid('upcoming', s.CONFIG), 'en-US'): {'metas': {'tv': cards}}}):
            got = s.row_metas(rid('upcoming', s.CONFIG), 'tv', 'en-US')
        self.assertEqual([x['id'] for x in got], ['tmdb:990400'])
        self.assertTrue(got[0]['description'].startswith('New season: Starts 20 Oct.'))

    def test_anime_builder_uses_anilist_english(self):
        it = {**self.item(990300), 'name': '死して生きる', 'title': '', 'genre_ids': [16], 'original_language': 'ja'}
        media = {'title': {'english': 'English Name', 'romaji': 'Romaji Name'}, 'startDate': {'year': 2027, 'month': 1, 'day': 1}}
        with patch.object(self.s, 'anilist', return_value=[media]), patch.object(self.s, 'resolve_anime', return_value=(it, {})), \
                patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'taste_order', side_effect=lambda xs, **kw: xs):
            got = self.s.anime_feed('en-US', True)
        self.assertEqual(got['series'][0]['name'], 'English Name')

    def test_soon_for_taste_does_not_repeat_confidence(self):
        s = self.s
        meta = self.s.row_meta(self.item(), 'series', 'tv', {}, self.item(), note='Strong fit · Starts 20 Oct')
        with patch.object(s, 'get_model', return_value={'cal': 10}), \
                patch.object(s, 'tmdb', return_value={'results': []}), \
                patch.dict(s._feeds, {(rid('nextseason', s.CONFIG), 'en-US'): {'metas': {'series': [meta]}}}), \
                patch.object(s, 'rank_by_taste', side_effect=lambda m, xs: [(1, [], *x) for x in xs]), \
                patch.object(s, 'match_pct', return_value=90), patch.object(s, 'reasons', return_value=[]):
            got = s.soon_for_taste({'seen': set()}, 'en-US', datetime.date(2026, 10, 11), set())
        self.assertEqual(got[0]['description'], 'Starts 20 Oct · An ordinary overview.')


if __name__ == '__main__':
    unittest.main()
