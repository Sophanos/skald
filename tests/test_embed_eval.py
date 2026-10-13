"""Synthetic embedding comparisons, including the live transport contract."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import math
import unittest
from unittest.mock import patch

from eval import embed_eval


class EmbedEvalTests(unittest.TestCase):
    def test_imdb_fixture_uses_resolved_target_id_and_ranks_it_first(self):
        evaluate = embed_eval.evaluate
        checked_targets = []

        def check_target(server, profile, universe):
            target = next(e for e in profile['entries'] if e['title'] == 'Fixture 6')
            if target['signal'] != 'loved':
                return evaluate(server, profile, universe)
            self.assertEqual(target['key'], 'tmdb:6')
            self.assertIn(target['key'], universe)
            self.assertEqual(server._facets[target['key']]['title'], target['title'])
            self.assertNotIn('tmdb:0006', universe)
            checked_targets.append(target['key'])
            seed = next(e for e in embed_eval.replay.positives(profile) if e['key'] == 'tmdb:1')
            return evaluate(server, embed_eval.replay.profile_with(profile, [seed, target]), universe)

        def transport(url, payload, key):
            # The loved target and its seed match exactly; every distractor is opposite.
            return {'data': [{'index': i, 'embedding': [1., 0.] if text.startswith(
                ('Fixture 1 (', 'Fixture 6 (')) else [-1., 0.]}
                for i, text in enumerate(payload['input'])], 'usage': {'cost': 0.}}

        with patch.object(embed_eval, 'evaluate', side_effect=check_target):
            report = embed_eval.run([('synthetic', 2)], transport, fixture=True)
        self.assertTrue(checked_targets)
        metrics = report['models'][0]['profiles']['plain']['seed_similarity']
        self.assertEqual(metrics, {'n': 2, 'recall@20': 1., 'ndcg@20': 1.})

    def test_cutoff_math_counts_missing_targets(self):
        result = embed_eval.metrics([1, 20, 21, None])
        self.assertEqual(result['recall@20'], .5)
        self.assertAlmostEqual(result['ndcg@20'], (1 + 1/math.log2(21))/4)
        self.assertIsNone(embed_eval.metrics([])['recall@20'])

    def test_batches_dimensions_indices_cost_and_latency(self):
        calls = []

        def transport(url, payload, key):
            calls.append((url, payload, key))
            return {'data': [{'index': i, 'embedding': [i+1.] + [1.] * (payload['dimensions']-1)}
                             for i in reversed(range(len(payload['input'])))],
                    'usage': {'cost': .25}}

        vectors, stats = embed_eval.embed([str(i) for i in range(130)], 'm1', 7,
                                         transport, 'synthetic-key', 'https://example.invalid',
                                         clock=iter([0., 1., 2., 5., 6., 8.]).__next__)
        self.assertEqual([len(c[1]['input']) for c in calls], [64, 64, 2])
        self.assertTrue(all(c[0] == 'https://example.invalid/api/v1/embeddings' for c in calls))
        self.assertTrue(all(len(v) == 7 for v in vectors))
        self.assertAlmostEqual(sum(x*x for x in vectors[0]), 1., places=6)
        self.assertAlmostEqual(vectors[0][0], 1/math.sqrt(7))
        self.assertAlmostEqual(vectors[1][0], 2/math.sqrt(10))
        self.assertEqual(stats['total_cost'], .75)
        self.assertEqual(stats['cost_per_1000_titles'], .75 * 1000/130)
        self.assertEqual(stats['p50_batch_latency_ms'], 2000.)
        self.assertEqual(stats['vector_bytes_per_title'], 14)

    def test_live_pipeline_with_fake_transport_never_prints_key(self):
        output, errors, calls = io.StringIO(), io.StringIO(), []

        def transport(url, payload, key):
            calls.append(payload)
            self.assertEqual(key, 'synthetic-secret')
            return embed_eval.fixture_transport(url, payload, key)

        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-secret'}), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(embed_eval.main(['--models', 'm1:7,m2:11', '--live'], transport), 0)
        self.assertNotIn('synthetic-secret', output.getvalue() + errors.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual([c['dimensions'] for c in calls], [7, 11])
        self.assertIn('Fixture 1 (2020), movie. Genres:', calls[0]['input'][0])
        for model in report['models']:
            for profile in model['profiles'].values():
                self.assertGreater(profile['seed_similarity']['n'], 0)
                self.assertGreaterEqual(profile['seed_similarity']['recall@20'], 0)
                self.assertLessEqual(profile['seed_similarity']['ndcg@20'], 1)
                self.assertIn(profile['best_embedding_weight'], (0, .1, .2, .3))

    def test_fixture_determinism_and_invalid_dimensions(self):
        payload = {'model': 'm', 'dimensions': 9, 'input': ['title']}
        self.assertEqual(embed_eval.fixture_transport('', payload, ''),
                         embed_eval.fixture_transport('', payload, ''))
        with self.assertRaises(ValueError):
            embed_eval.embed(['title'], 'm', 9,
                             lambda *args: {'data': [{'index': 0, 'embedding': [1.]}]}, '', '')

    def test_transport_errors_are_sanitized(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'synthetic-secret'}), \
                redirect_stdout(output), redirect_stderr(errors):
            code = embed_eval.main(['--models', 'm:7', '--live'],
                                   lambda *args: (_ for _ in ()).throw(ValueError('synthetic-secret')))
        self.assertEqual(code, 1)
        self.assertNotIn('synthetic-secret', output.getvalue() + errors.getvalue())

    def test_unknown_cost_is_not_reported_as_free(self):
        def transport(url, payload, key):
            response = embed_eval.fixture_transport(url, payload, key)
            del response['usage']
            return response

        _, stats = embed_eval.embed(['title'], 'm', 7, transport, '', '')
        self.assertIsNone(stats['total_cost'])
        self.assertIsNone(stats['cost_per_1000_titles'])

    def test_fixture_pipeline_is_deterministic_and_selects_only_on_tune(self):
        report = embed_eval.run([('m1', 7), ('m2', 11)], embed_eval.fixture_transport, fixture=True)
        self.assertEqual(report, embed_eval.run([('m1', 7), ('m2', 11)],
                                               embed_eval.fixture_transport, fixture=True))
        for model in report['models']:
            for profile in model['profiles'].values():
                blends = profile['blends']
                best = min(embed_eval.WEIGHTS,
                           key=lambda w: (-(blends[w]['tune']['ndcg@20'] or 0), w))
                self.assertEqual(profile['best_embedding_weight'], best)
                self.assertEqual(profile['best_blend'], blends[best]['test'])
                self.assertEqual(profile['seed_similarity']['n'],
                                 blends[best]['test']['n'] + blends[best]['tune']['n'])
