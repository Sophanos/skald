"""Synthetic release metric checks; no household inputs."""
import importlib
import math
import unittest


class HouseholdMetricsTests(unittest.TestCase):
    def test_resampled_weeks_recompute_scope_macro_with_multiplicity(self):
        other = ('individual', 'ben')
        specs = [{'id': 'a'}]
        hit = self.week({'discovery': ['x']}, specs, {'a': ['x']})
        miss = self.week({'discovery': ['x']}, specs, week='w2')
        other_hit = self.week({'discovery': ['x']}, specs, {'a': ['x']}, scope=other)
        empty = self.week({}, specs, week='w3')
        draw = [hit, other_hit, miss, empty, miss]
        result = self.m.summarize(draw, [self.scope, other], resampled=True)
        discovery = result['tasks']['discovery']
        self.assertAlmostEqual(discovery['score'], 2 / 3)
        self.assertEqual(discovery['scopes'][self.scope],
                         {'score': 1 / 3, 'target_weeks': 3, 'observed_weeks': 4})
        conflicting = self.week({'discovery': ['x']}, specs)
        with self.assertRaises(ValueError):
            self.m.summarize([hit, conflicting], [self.scope], resampled=True)

    def setUp(self):
        self.m = importlib.import_module('eval.household.metrics')
        self.scope = ('individual', 'ana')

    def week(self, targets, specs=(), rankings=None, scope=None, week='w1'):
        return self.m.evaluate_week(scope or self.scope, week, targets,
                                    specs, rankings or {})

    def test_duplicate_across_rows_gets_one_home_hit(self):
        specs = [{'id': 'a'}, {'id': 'b'}]
        result = self.week({'discovery': ['x', 'y', 'x']}, specs,
                           {'a': ['x', 'x'], 'b': ['x']})
        self.assertEqual(result['tasks']['discovery'],
                         {'hits': 1, 'targets': 2, 'score': .5})
        self.assertLessEqual(result['rows']['a']['ndcg_at_k'], 1)

    def test_empty_target_week_is_na(self):
        result = self.week({}, [{'id': 'a'}], {'a': ['x']})
        self.assertIsNone(result['tasks']['discovery']['score'])
        self.assertIsNone(result['rows']['a']['hit_at_k'])
        self.assertIsNone(result['rows']['a']['ndcg_at_k'])
        summary = self.m.summarize([result], [self.scope])
        self.assertIsNone(summary['tasks']['discovery']['score'])
        self.assertEqual(summary['tasks']['discovery']['missing_scopes'], [self.scope])

    def test_missing_candidate_remains_a_miss(self):
        result = self.week({'discovery': ['absent']}, [{'id': 'a'}])
        self.assertEqual(result['tasks']['discovery']['score'], 0)
        self.assertEqual(result['rows']['a']['hit_at_k'], 0)
        self.assertEqual(result['rows']['a']['ndcg_at_k'], 0)

    def test_frozen_footprint_and_separate_tasks(self):
        specs = [{'id': 'continue', 'task': 'continuation'},
                 {'id': 'top', 'top10': True}] + [{'id': str(i)} for i in range(6)]
        rankings = {'continue': ['c'], 'top': [str(i) for i in range(10)] + ['t']}
        rankings['0'] = [str(i) for i in range(12)] + ['z']
        rankings['5'] = ['late']
        result = self.week({'discovery': ['t', 'z', 'late'],
                            'continuation': ['c'], 'upcoming': ['u'], 'rewatch': ['r']},
                           specs, rankings)
        self.assertEqual(result['tasks']['discovery']['score'], 0)
        self.assertEqual(result['tasks']['continuation']['score'], 1)
        self.assertEqual(result['tasks']['upcoming']['score'], 0)
        self.assertEqual(result['tasks']['rewatch']['score'], 0)
        self.assertEqual(result['rows']['top']['k'], 10)
        self.assertEqual(result['rows']['5']['hit_at_k'], 1)
        self.assertEqual(result, self.week(
            {'discovery': ['t', 'z', 'late'], 'continuation': ['c'],
             'upcoming': ['u'], 'rewatch': ['r']}, specs, rankings))

    def test_binary_ndcg_uses_reference_targets_and_original_ranks(self):
        targets = ['x', 'y', 'unavailable']
        a = self.m.row_metrics(['x', 'x', 'y'], targets, 3)
        b = self.m.row_metrics(['y'], targets, 3)
        ideal = 1 + 1 / math.log2(3) + .5
        self.assertAlmostEqual(a['ndcg_at_k'], 1.5 / ideal)
        self.assertAlmostEqual(b['ndcg_at_k'], 1 / ideal)
        self.assertEqual(a['hit_at_k'], 1)
        self.assertEqual(a['hits'], 2)

    def test_scope_means_are_equal_weight_and_attribution_stays_separate(self):
        shared = ('shared-unknown', 'household')
        group = ('verified-group', 'ana+ben')
        specs = [{'id': 'a'}]
        weeks = [self.week({'discovery': ['x']}, specs, {'a': ['x']}),
                 self.week({'discovery': ['x', 'y']}, specs, week='w2'),
                 self.week({}, specs, week='w3'),
                 self.week({'discovery': ['x']}, specs, scope=shared),
                 self.week({'discovery': ['x']}, specs, scope=group)]
        result = self.m.summarize(weeks, [self.scope, shared, group])['tasks']['discovery']
        self.assertAlmostEqual(result['score'], 1 / 6)
        self.assertEqual(result['scopes'][self.scope],
                         {'score': .5, 'target_weeks': 2, 'observed_weeks': 3})
        missing = self.m.summarize(weeks[:3], [self.scope, shared])['tasks']['discovery']
        self.assertIsNone(missing['score'])
        self.assertEqual(missing['missing_scopes'], [shared])
        self.assertEqual(self.m.summarize(weeks, [self.scope])['unregistered_scopes'],
                         [shared, group])

    def test_invalid_or_duplicate_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.m.row_metrics([], ['x'], 0)
        with self.assertRaises(ValueError):
            self.week({}, [{'id': 'a'}, {'id': 'a'}])
        with self.assertRaises(ValueError):
            self.week({}, [{'id': 'a', 'task': 'typo'}])
        result = self.week({})
        with self.assertRaises(ValueError):
            self.m.summarize([result, result], [self.scope])


if __name__ == '__main__':
    unittest.main()
