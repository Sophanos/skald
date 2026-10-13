"""Synthetic protocol and evidence gates, independent of household inputs."""
import copy
import unittest

from eval.household import protocol as p


class ProtocolTests(unittest.TestCase):
    def test_manifest_matches_evaluator_defaults(self):
        import inspect
        from eval.household import events, metrics, splits, uncertainty

        policy = p.manifest()
        budget = metrics.Budget()
        self.assertEqual(policy['home_budget'],
                         {'rows': 6, 'items_per_row': 12, 'top10': 10})
        self.assertEqual((budget.rows, budget.cards, budget.top10), (6, 12, 10))
        self.assertEqual(inspect.signature(metrics.row_metrics).parameters['k'].default,
                         policy['home_budget']['items_per_row'])
        bootstrap = inspect.signature(uncertainty.stationary_bootstrap).parameters
        self.assertEqual(bootstrap['seed'].default, policy['seeds']['bootstrap'])
        self.assertEqual(bootstrap['replicates'].default, policy['uncertainty']['replicates'])
        self.assertEqual(events.normalize_events([])['protocol'], policy['event_thresholds'])
        self.assertEqual(inspect.signature(splits.weekly_folds).parameters['outer_weeks'].default,
                         policy['uncertainty']['min_weeks'])

    def result(self, evidence=None, **kwargs):
        return p.result(p.manifest(), evidence or p.COMPLETE,
                        quality={'recall@20': .8}, quality_passed=True,
                        required_components=('ranking',),
                        supported_components=('ranking',), **kwargs)

    def test_diagnostic_cannot_claim_release_pass(self):
        diagnostic = self.result(p.DIAGNOSTIC)
        self.assertEqual(diagnostic['evidence_level'], p.DIAGNOSTIC)
        verdict = p.compare(diagnostic, diagnostic, enabled=True)
        self.assertFalse(verdict['release_pass'])
        self.assertIn('insufficient_evidence', verdict['failures'])

    def test_protocol_change_invalidates_comparison(self):
        baseline = self.result()
        for key, value in p.manifest().items():
            with self.subTest(field=key):
                changed = p.manifest()
                changed[key] = ['changed', value]
                candidate = p.result(changed, p.COMPLETE)
                verdict = p.compare(baseline, candidate, enabled=True)
                self.assertFalse(verdict['comparable'])
                self.assertFalse(verdict['release_pass'])
                self.assertIn('protocol_changed', verdict['failures'])

    def test_fixed_curation_can_compare_but_cannot_pass_release(self):
        fixed = self.result(p.FIXED_CURATION)
        verdict = p.compare(fixed, fixed, enabled=True)
        self.assertTrue(verdict['comparable'])
        self.assertFalse(verdict['release_pass'])
        self.assertFalse(p.compare(fixed, self.result())['comparable'])

    def test_changed_unsupported_component_fails_coverage(self):
        baseline = self.result()
        verdict = p.compare(baseline, baseline, enabled=True,
                            changed_components=('editorial',))
        self.assertFalse(verdict['release_pass'])
        self.assertEqual(verdict['coverage_failures'], ['editorial'])
        self.assertIn('coverage_failure', verdict['failures'])
        candidate = copy.deepcopy(baseline)
        candidate['required_components'] += ('external_picks',)
        self.assertEqual(p.compare(baseline, candidate, enabled=True)
                         ['coverage_failures'], ['external_picks'])

    def test_complete_quality_is_opt_in_and_correctness_stays_separate(self):
        complete = self.result(correctness={'golden_hash': 12, 'command': 4,
                                           'synthetic_embedding': 7})
        self.assertFalse(p.compare(complete, complete)['release_pass'])
        self.assertTrue(p.compare(complete, complete, enabled=True)['release_pass'])
        self.assertEqual(complete['quality'], {'recall@20': .8})
        for field, value in (('quality', {}), ('quality_passed', False)):
            candidate = copy.deepcopy(complete)
            candidate[field] = value
            self.assertFalse(p.compare(complete, candidate, enabled=True)['release_pass'])

    def test_empty_or_one_sided_coverage_cannot_pass(self):
        complete = self.result()
        for field in ('required_components', 'supported_components'):
            incomplete = copy.deepcopy(complete)
            incomplete[field] = ()
            verdict = p.compare(incomplete, incomplete, enabled=True)
            self.assertFalse(verdict['release_pass'])
            self.assertIn('coverage_failure', verdict['failures'])
        incomplete['required_components'] = ('ranking',)
        self.assertFalse(p.compare(incomplete, complete, enabled=True)['release_pass'])

    def test_manifest_and_results_are_detached_and_identity_is_stable(self):
        manifest = p.manifest()
        original = p.protocol_id(manifest)
        self.assertEqual(original, p.protocol_id(dict(reversed(list(manifest.items())))))
        quality = {'recall@20': .7}
        recorded = p.result(manifest, p.DIAGNOSTIC, quality=quality)
        quality['recall@20'] = 0
        manifest['home_budget']['rows'] += 1
        self.assertEqual(recorded['protocol_id'], original)
        self.assertEqual(recorded['quality']['recall@20'], .7)
        self.assertEqual(p.protocol_id(p.manifest()), original)

    def test_unknown_evidence_and_nonfinite_protocol_are_rejected(self):
        with self.assertRaises(ValueError):
            p.result(p.manifest(), 'unknown')
        manifest = p.manifest()
        manifest['effect_margins']['recall@20'] = float('nan')
        with self.assertRaises(ValueError):
            p.protocol_id(manifest)
