"""Synthetic row contracts and diagnostics, with no household inputs."""
import importlib
import unittest


class RowMetricsTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('eval.household.row_metrics')

    def report(self, specs, rankings, targets=None, **kwargs):
        return self.m.evaluate_rows(('individual', 'synthetic'), 'w1',
                                    targets or {'discovery': ['x']}, specs,
                                    rankings, catalog={'x': ['drama'],
                                    'y': ['comedy']}, origin=30 * 86400, **kwargs)

    def test_hidden_row_not_removed_from_denominator(self):
        specs = [{'id': 'a', 'type': 'personal'},
                 {'id': 'b', 'type': 'personal'}]
        result = self.report(specs, {'a': ['x']})
        self.assertEqual(result['summary']['target_rows'], 2)
        self.assertEqual(result['summary']['mean_hit'], .5)
        self.assertEqual(result['rows']['b']['quality']['hit_at_k'], 0)
        self.assertEqual(result['rows']['b']['fill'], 0)
        self.assertEqual(result['tasks']['discovery']['score'], 1)

    def test_lane_identity_survives_slot_change(self):
        lane = {'type': 'lane', 'definition': {'theme': 'quiet', 'version': 1},
                'targets': ['x', 'absent'], 'theme_titles': ['x']}
        a = self.report([dict(lane, id='slot1')], {'slot1': ['x']})
        b = self.report([dict(lane, id='slot7')], {'slot7': ['x']})
        self.assertEqual(a['rows'], b['rows'])
        row = next(iter(a['rows'].values()))
        self.assertEqual(row['quality']['targets'], 2)
        self.assertEqual(row['theme_fidelity'], 1)
        self.assertEqual(row['calibration_interpretation'], 'context_conditional')
        c = self.report([dict(lane, id='slot1', definition={'theme': 'loud'})],
                        {'slot1': ['x']})
        self.assertNotEqual(set(a['rows']), set(c['rows']))

    def test_top10_chart_fidelity_checked(self):
        spec = {'id': 'chart', 'type': 'top10', 'historical_chart': ['x', 'y'],
                'chart_available': True, 'rank_labels': [2, 1]}
        row = self.report([spec], {'chart': ['y', 'x']})['rows']['chart']
        self.assertEqual(row['quality']['k'], 10)
        self.assertEqual(row['quality']['hit_at_k'], 1)
        self.assertEqual(row['chart_fidelity'], 0)
        self.assertTrue(row['rank_labels_consistent'])
        bad = self.report([dict(spec, rank_labels=[1, 2])], {'chart': ['y', 'x']})
        self.assertFalse(bad['rows']['chart']['rank_labels_consistent'])
        missing = self.report([dict(spec, chart_available=False)], {'chart': ['x']})
        self.assertIsNone(missing['rows']['chart']['chart_fidelity'])

    def test_coverage_recall_duplicates_and_prior_exposure(self):
        specs = [{'id': 'a', 'type': 'personal', 'candidates': ['x']},
                 {'id': 'b', 'type': 'personal'}]
        report = self.report(specs, {'a': ['x', 'y'], 'b': ['x']},
                             {'discovery': ['x', 'absent']}, prefix=['x'],
                             negatives=['y'], served=[{'title': 'x', 'at': 2 * 86400},
                             {'title': 'y', 'at': 30 * 86400}])
        self.assertEqual(report['summary']['catalog_coverage'], 1)
        self.assertEqual(report['summary']['cross_row_duplicates'], 1)
        self.assertEqual(report['summary']['not_served_28d'], .5)
        row = report['rows']['a']
        self.assertEqual(row['candidate_recall'], .5)
        self.assertEqual(row['negative_leakage'], .5)
        self.assertEqual(row['diversity'], 1)
        self.assertEqual(row['sensitivity_at_5']['k'], 5)

    def test_calibration_is_bounded_fractional_and_diagnostic(self):
        catalog = {'a': ['drama', 'comedy', 'drama'], 'b': ['drama'],
                   'c': ['comedy'], 'd': ['horror']}
        self.assertAlmostEqual(self.m.calibration(['a'], ['b', 'c'], catalog), 0)
        self.assertEqual(self.m.calibration(['b'], ['d'], catalog), 1)
        self.assertIsNone(self.m.calibration([], ['a'], catalog))
        self.assertIsNone(self.m.calibration(['unknown'], ['a'], catalog))

    def test_group_evidence_and_repeat_tasks_stay_separate(self):
        specs = [{'id': 'g', 'type': 'together', 'member_targets':
                  {'m1': ['x'], 'm2': ['y']}, 'vetoes': {'m2': ['x']}},
                 {'id': 'r', 'type': 'rewatch', 'eligible': ['x']}]
        targets = {'discovery': ['y'], 'rewatch': ['x', 'absent']}
        report = self.m.evaluate_rows(('verified-group', 'group'), 'w1', targets,
                 specs, {'g': ['x'], 'r': ['x', 'y']}, catalog={'x': ['drama'],
                 'y': ['comedy']}, origin=0)
        self.assertEqual(report['rows']['g']['member_balance'], 0)
        self.assertEqual(report['rows']['g']['veto_leakage'], 1)
        self.assertIsNone(report['rows']['g']['unknown_satisfaction'])
        self.assertEqual(report['rows']['r']['prior_play_eligibility'], .5)
        self.assertEqual(report['tasks']['discovery']['score'], 0)
        self.assertEqual(report['tasks']['rewatch']['score'], .5)

    def test_lane_suppression_incremental_hits_and_missing_inputs(self):
        lane = {'id': 'l', 'type': 'lane', 'definition': {'theme': 'quiet'},
                'targets': ['x', 'y'], 'theme_titles': ['x'],
                'candidates': ['x', 'y']}
        specs = [{'id': 'a', 'type': 'personal'}, lane]
        result = self.report(specs, {'a': ['x'], 'l': ['x', 'y']})
        row = list(result['rows'].values())[1]
        self.assertEqual(row['incremental_hits'], 1)
        self.assertEqual(row['theme_fidelity'], .5)
        hidden = self.report(specs, {'a': ['x']})
        self.assertEqual(hidden['summary']['suppression_rate'], .5)
        self.assertEqual(list(hidden['rows'].values())[1]['suppression_rate'], 1)
        self.assertIsNone(result['rows']['a']['candidate_recall'])
        with self.assertRaises(ValueError):
            self.report([dict(lane, definition=None)], {})
        with self.assertRaises(ValueError):
            self.report([lane, dict(lane, id='other')], {})

    def test_empty_group_evidence_and_unknown_scope_abstain(self):
        spec = {'id': 'g', 'type': 'together'}
        for members in ({}, {'m1': []}, {'m1': ['x'], 'm2': []}):
            with self.subTest(members=members):
                report = self.report([dict(spec, member_targets=members)], {'g': ['x']})
                self.assertIsNone(report['rows']['g']['member_balance'])
                self.assertIsNone(report['rows']['g']['quality']['hit_at_k'])
                self.assertEqual(report['tasks']['discovery']['score'], 0)
        self.assertIsNone(self.report([spec], {})['summary']['not_served_28d'])


if __name__ == '__main__':
    unittest.main()
