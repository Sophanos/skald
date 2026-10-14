import shutil
"""Tiny synthetic snapshots; no live data or services needed."""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

from eval import replay, variants
from eval.matched import Matched, paired_bootstrap
from eval import cold_start, seed_probes


class ReplayTest(unittest.TestCase):
    def setUp(self):
        root = replay.ROOT / 'eval' / 'out'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.snapshot = Path(self.temp.name) / 'input'
        self.snapshot.mkdir()
        shutil.copyfile(Path(__file__).parent / 'fixtures/events/data/legacy.json', self.snapshot / 'legacy.json')
        # Six training entries: removing target A must leave a pure y centroid.
        vectors = {f'tmdb:{i}': [0., 1.] for i in range(2, 7)}
        vectors.update({'tmdb:1': [1., 0.], 'tmdb:7': [1., 0.],
                        'tmdb:8': [0., 1.], 'tmdb:9': [-1., 0.], 'tmdb:10': [.6, .8]})
        self.vectors = vectors
        entries = [dict(key=f'tmdb:{i}', id=i, kind='movie', stype='movie', title=f'Title {i}',
                        year='2000', signal='loved', weight=2., age=10, who='owner',
                        by=['ben'], flags=[], genres=[], item={}) for i in range(1, 7)]
        self.profile = {'entries': entries, 'seen': [e['key'] for e in entries],
                        'negative': [], 'built_at': 1800000000}
        facets = {k: {'title': f'Title {k[5:]}', 'g': ['Adventure'],
                      'x': {'epic_quest': 3} if int(k[5:]) % 2 else {'cozy_case': 2}}
                  for k in vectors}
        encoded = {k: base64.b64encode(struct.pack('256e', *(v + [0.] * 254))).decode()
                   for k, v in vectors.items()}
        trakt = [{'id': f'tmdb:{i}', 'type': 'movie', 'title': f'Title {i}', 'year': 2000,
                  'signal': 'watched', 'weight': 1., 'at': i*1000, 'backfilled': False}
                 for i in range(1, 11)]
        data = {'profile.json': self.profile,
                'emb.json': {'dim': 256, 'model': 'openai/text-embedding-3-small', 'v': encoded},
                'facets.json': {'items': facets}, 'learn.json': {'hits': [{'key': 'tmdb:1'}]},
                'trakt.json': {'items': trakt}, 'taste.json': {}, 'feeds.json': {}}
        for name, value in data.items():
            (self.snapshot/name).write_text(json.dumps(value), encoding='utf-8')

    def server(self):
        return replay.offline_server(self.snapshot, Path(self.temp.name)/'work')

    def test_loo_math_and_current_parity(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.snapshot.iterdir()}
        with self.server() as server:
            profile = replay.profile_with(self.profile, self.profile['entries'])
            train = replay.leave_out(profile, ['tmdb:1'])
            selected = [variants.REGISTRY['current'], variants.REGISTRY['emb_only']]
            ctx = replay.build_contexts(server, train, selected, replay.SEED)['current']
            self.assertAlmostEqual(ctx.model['vec'][0], 0.)
            self.assertAlmostEqual(ctx.model['vec'][1], 1.)
            self.assertEqual(variants.current(ctx)('tmdb:8'), server.score_key(ctx.model, 'tmdb:8')[0])
            self.assertIn('tmdb:1', server._learn['hits'][0].values())  # Live credit restored after build.
            rows = replay.rank_targets(server, train, [profile['entries'][0]],
                                       sorted(self.vectors), selected, replay.SEED)
            # Candidate y=1, (.6,.8), then two x-axis ties ordered by key, then -x.
            self.assertEqual(rows['emb_only'][0]['rank'], 3)
            self.assertEqual(rows['emb_only'][0]['candidates'], 5)
            self.assertNotIn('tmdb:1', {e['key'] for e in train['entries']})
            self.assertNotIn('tmdb:1', train['seen'])
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.snapshot.iterdir()}
        self.assertEqual(before, after)

    def test_metrics_count_unscorable_as_miss(self):
        m = replay.metrics([{'rank': r} for r in [1, 10, 31, None]])
        self.assertEqual(m['n'], 4)
        self.assertEqual(m['recall@10'], .5)
        self.assertEqual(m['recall@30'], .5)
        self.assertEqual(m['recall@100'], .75)
        self.assertAlmostEqual(m['mrr'], (1 + .1 + 1/31)/4)
        self.assertAlmostEqual(m['ndcg@30'], (1 + 1/math.log2(11))/4)
        self.assertEqual(m['median_rank'], 20.5)
        self.assertIsNone(replay.metrics([])['mrr'])
        self.assertTrue(replay.metrics([{'rank': None}])['median_unbounded'])

    def test_report_explains_local_calibration_without_network_baseline(self):
        text = replay.report({'metadata': {'snapshot': 'synthetic', 'universe_n': 5000, 'seed': 108},
                              'protocols': {}, 'bugs': []}, .1)
        self.assertIn('local known-title calibration', text)
        self.assertNotIn('no match percentages inferred', text)

    def test_guard_precedes_import_and_cannot_be_swallowed(self):
        original = replay.importlib.util.module_from_spec

        def importing(spec):
            with self.assertRaises(replay.OfflineNetworkError):
                urllib.request.urlopen('https://offline.invalid')
            return original(spec)

        with patch.object(replay.importlib.util, 'module_from_spec', importing):
            with self.assertRaises(replay.OfflineNetworkError):
                with self.server():
                    pass
        for name in ('tmdb', 'openrouter', 'anilist'):
            with self.assertRaises(replay.OfflineNetworkError):
                with self.server() as server:
                    getattr(server, name)('unexpected')
        with self.assertRaises(replay.OfflineNetworkError):
            with self.server():
                urllib.request.urlopen('https://offline.invalid')

    def test_leakage_filter_uses_remaining_normalized_vectors(self):
        with self.server() as server:
            profile = replay.profile_with(self.profile, self.profile['entries'])
            near = replay.nearest_remaining(server, 'tmdb:1', replay.leave_out(profile, ['tmdb:1']))
            self.assertEqual(near, 0.)
            contaminated = copy.deepcopy(profile)
            contaminated['entries'].append(dict(profile['entries'][0], key='tmdb:7'))
            near = replay.nearest_remaining(server, 'tmdb:1', replay.leave_out(contaminated, ['tmdb:1']))
            self.assertAlmostEqual(near, 1.)
            selected = [variants.REGISTRY['emb_only']]
            rows = replay.loo(server, contaminated, [profile['entries'][0]],
                              sorted(self.vectors), selected, replay.SEED)['emb_only']
            self.assertFalse(rows[0]['hard'])
            self.assertEqual(replay.summarize(rows)['hard']['n'], 0)

    def test_experience_negative_weight_and_random_order(self):
        with self.server() as server:
            p = replay.profile_with(self.profile, [dict(self.profile['entries'][0], weight=2.),
                                                  dict(self.profile['entries'][1], weight=-2.)])
            ctx = variants.Context(server, None, p, replay.SEED)
            self.assertEqual(ctx.x_profile(), {'epic_quest': 6., 'cozy_case': -2.})
            self.assertAlmostEqual(ctx.x('tmdb:1'), 6/(40**.5))
            floor = variants.random_floor(ctx)
            forward = {k: floor(k) for k in sorted(self.vectors)}
            backward = {k: floor(k) for k in reversed(sorted(self.vectors))}
            self.assertEqual(forward, backward)

    def test_hard_slice_uses_point_eight_and_collection_or_base_title(self):
        with self.server() as server:
            profile = replay.profile_with(self.profile, self.profile['entries'])
            train = replay.leave_out(profile, ['tmdb:1'])
            server._emb['tmdb:1'] = [.6, .8]
            rows = replay.rank_targets(server, train, [profile['entries'][0]], sorted(self.vectors),
                                       [variants.REGISTRY['current']], replay.SEED)['current']
            self.assertFalse(rows[0]['hard'])
            self.assertEqual(replay.summarize(rows)['excluded_cosine_n'], 1)
            server._facets['tmdb:1']['collection'] = 10
            server._facets['tmdb:2']['collection'] = 10
            self.assertTrue(replay.same_franchise(server, 'tmdb:1', train))
            server._facets['tmdb:1'].pop('collection')
            server._facets['tmdb:1']['title'] = 'Saga: First'
            server._facets['tmdb:2']['title'] = 'Saga: Second'
            self.assertTrue(replay.same_franchise(server, 'tmdb:1', train))
            server._facets['tmdb:1']['title'] = 'Other'
            self.assertFalse(replay.same_franchise(server, 'tmdb:1', train))

    def test_tuning_uses_tune_only_and_transform_is_supported(self):
        selected, grid = variants.select('current,blend(1,0,0.12),grid')
        self.assertTrue(grid)
        self.assertEqual(len(selected), 1 + len(variants.GRID))
        first, second = variants.GRID[:2]
        row = dict(key='tmdb:1', signal='loved', kind='movie', anime=False, who='owner',
                   hard=True, nearest_cosine=0., title='target', candidates=10)
        raw = {first.name: [dict(row, split='tune', rank=100, mauc=.8), dict(row, split='test', rank=100, mauc=.1)],
               second.name: [dict(row, split='tune', rank=1, mauc=.2), dict(row, split='test', rank=1, mauc=.9)]}
        block = replay.finish_protocol(raw, [first, second], True)
        self.assertEqual(block['tuning']['selected_variant'], first.name)
        self.assertEqual(block['variants']['grid_selected']['all']['recall@30'], 0.)
        self.assertEqual(block['variants']['grid_selected']['all']['n'], 1)
        explicit = variants.blend(1, 0, .12)
        self.assertEqual(replay.pick_grid({explicit.name: [dict(row, rank=1)]}, [explicit]), explicit.name)
        with self.server() as server:
            def transform(p):
                p['entries'][0]['weight'] = 100.
                return p
            v = variants.Variant('transformed', variants.emb_only, transform)
            ctx = replay.build_contexts(server, self.profile, [v], replay.SEED)['transformed']
            self.assertEqual(ctx.profile['entries'][0]['weight'], 100.)
            self.assertEqual(self.profile['entries'][0]['weight'], 2.)

    def test_together_modality_baselines_use_both_people(self):
        with self.server() as server:
            m = {'pvec': {'ana': [1., 0.], 'ben': [0., 1.]}, 'vec': [1., 0.],
                 'aff': {}, 'paff': {'ana': {'g:Adventure': 1.}, 'ben': {}},
                 'bscale': 1., 'pbscale': {'ana': 1., 'ben': 1.},
                 'x': {}, 'px': {'ana': {'epic_quest': 3}, 'ben': {'cozy_case': 3}}}
            ctx = variants.Context(server, m, self.profile, replay.SEED)
            self.assertAlmostEqual(variants.emb_only(ctx)('tmdb:1', 'together'), .25)
            self.assertAlmostEqual(variants.x_only(ctx)('tmdb:1', 'together'), .25)
            self.assertAlmostEqual(variants.facets_only(ctx)('tmdb:1', 'together'), .25*math.tanh(1))

    def test_temporal_no_future_or_backfill(self):
        items = replay.read_json(self.snapshot/'trakt.json')['items']
        items.append(dict(items[0], id='tmdb:99', backfilled=True, at=999999))
        (self.snapshot/'trakt.json').write_text(json.dumps({'items': items}))
        with self.server() as server:
            train, targets, split = replay.temporal_profile(server, self.snapshot, self.profile)
        self.assertEqual(split['train_n'], 8)
        self.assertEqual({e['key'] for e in targets}, {'tmdb:9', 'tmdb:10'})
        self.assertEqual(train['seen'], {f'tmdb:{i}' for i in range(1, 9)})
        self.assertTrue(train['seen'].isdisjoint({e['key'] for e in targets}))

    def test_output_must_stay_in_worktree(self):
        with self.assertRaises(ValueError):
            replay.workspace_output('/tmp/skald-eval')

    def test_matched_auc_conditions_ties_unknowns_and_richness(self):
        facets = {str(i): {'kind': 'tv', 'k': list(range(i))} for i in range(20)}
        facets.update({'tie': facets['10'], 'movie': {**facets['10'], 'kind': 'movie'},
                       'noemb': facets['10']})
        server = SimpleNamespace(_facets=facets, _emb={k: [1.] for k in facets if k != 'noemb'})
        matching = Matched(server, list(facets))
        self.assertEqual(matching.rich['10'], 10)
        # Exclude other kinds, embedding coverage and richness quintiles; a tie counts half.
        distractors = ['tie', 'movie', 'noemb', '0', '19']
        scores = {k: 0 for k in facets}
        scores['10'] = scores['tie'] = 1
        self.assertEqual(matching.auc('10', scores, distractors), (.5, 1))
        scores['10'] = 2
        self.assertEqual(matching.auc('10', scores, distractors), (1., 1))
        scores['10'] = None
        self.assertEqual(matching.auc('10', scores, distractors), (0., 1))
        self.assertEqual(matching.auc('10', scores, ['movie']), (None, 0))
        self.assertEqual(matching.auc('missing', scores, distractors), (None, 0))

    def test_paired_bootstrap_is_paired_and_reproducible(self):
        rows = [dict(key=str(i), split='test', matched_n=2, mauc=.5) for i in range(3)]
        after = [dict(r, mauc=.6) for r in reversed(rows)]
        result = paired_bootstrap(rows, after)
        self.assertEqual(result, paired_bootstrap(rows, after))
        self.assertAlmostEqual(result['delta'], .1)
        self.assertEqual(result['resamples'], 2000)
        with self.assertRaises(ValueError):
            paired_bootstrap(rows, after[:2])
        with self.assertRaises(ValueError):
            paired_bootstrap(rows, [dict(r, matched_n=3) for r in after])

    def test_seed_probes_validate_identity_and_sign_pairs(self):
        fixture = self.snapshot / 'probes.json'
        probe = {'seed': {'key': 'tmdb:1', 'kind': 'tv', 'title': 'Seed'},
                 'targets': [{'key': 'tmdb:2', 'kind': 'movie', 'title': 'Target'}]}
        fixture.write_text(json.dumps([probe]))
        server = SimpleNamespace(_facets={'tmdb:1': {'title': 'Seed', 'kind': 'tv'},
                                          'tmdb:2': {'title': 'Target', 'kind': 'movie'}})
        self.assertEqual(seed_probes.load_probes(server, fixture), [probe])
        self.assertEqual(replay.resolve_probe(probe['seed'], server, ['tmdb:1']), ['tmdb:1'])
        server._facets['tmdb:1']['kind'] = 'movie'
        with self.assertRaises(ValueError):
            seed_probes.load_probes(server, fixture)
        with self.assertRaises(ValueError):
            replay.resolve_probe(probe['seed'], server, ['tmdb:1'])
        a = [dict(seed='s', key=str(i), rank=2) for i in range(10)]
        b = [dict(r, rank=1) for r in a]
        self.assertLess(seed_probes.sign_test(a, b)['p'], .01)
        with self.assertRaises(ValueError):
            seed_probes.sign_test(a, b[:1])

    def test_cold_start_rejects_insufficient_pool_and_restores_hits(self):
        with self.server() as server:
            hits = server._learn['hits']
            with self.assertRaisesRegex(ValueError, 'cold-start requires'):
                cold_start.run(server, self.profile, sorted(self.vectors))
            self.assertIs(server._learn['hits'], hits)

    def test_protocol_outputs_and_p2_does_not_tune_on_its_targets(self):
        selected, use_grid = variants.select('current,grid')
        with self.server() as server:
            # Legacy hits distinguish this fixture's otherwise identical household and Ben evidence.
            with patch('builtins.print'), patch.object(server, 'HIT_TASTE', True):
                data = replay.run(server, self.snapshot, selected, use_grid,
                                  {'p2', 'p3', 'p4', 'p5'}, replay.SEED)
        ana = data['protocols']['p2']['groups']['ana']
        self.assertEqual(ana['variants']['current']['all']['n'], 0)
        self.assertGreater(ana['tuning']['tune_n'], 0)  # Household trials supplied the tuning.
        self.assertIsNotNone(ana['tuning']['selected_variant'])
        self.assertEqual(data['protocols']['p3']['groups']['temporal']['variants']['current']['all']['n'], 2)
        self.assertEqual(data['protocols']['p5']['n'], 6)
        self.assertLess(data['protocols']['p5']['cosine_to_household']['ben'], 1.)
        self.assertTrue(all(not matches for p in data['protocols']['p4']
                            for matches in p['ranks']['current'].values()))
        self.assertIn('not in universe', replay.report(data, .1))
        json.dumps(data, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
