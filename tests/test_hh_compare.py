"""Synthetic release comparison checks, without household inputs."""
import copy
import unittest
from statistics import mean
from unittest.mock import patch

from eval.household import compare, protocol


def fixture():
    return {'snapshot': {'weeks': list(range(12))}, 'protocol': protocol.manifest(),
            'eligibility': ['synthetic'], 'contexts': [], 'external_inputs': [],
            'panel': {'kind': 'newly_matured', 'inspections': 0}}


def evaluator(build, inputs):
    return {'evidence': protocol.result(inputs['protocol'], protocol.COMPLETE,
                quality={'home_recovery': .5}, quality_passed=True,
                required_components=('ranking',), supported_components=('ranking',)),
            'checks': dict.fromkeys(('privacy', 'safety', 'veto'), True),
            'weeks': [{'week': i, 'mature': True, 'targets': [
                {'id': f'{i}-{j}', 'franchise': None, 'values': {
                    ('one', 'main'): .5 + build.get('gain', 0),
                    ('two', 'main'): .5 + build.get('gain', 0) - build.get('loss', 0)}}
                for j in range(3)]} for i in inputs['snapshot']['weeks']]}


def metric(weeks):
    cells = {}
    for week in weeks:
        for target in week['targets']:
            for cell, pair in target['values'].items():
                cells.setdefault(cell, []).append(pair)
    return tuple(mean(mean(p[i] for p in pairs) for pairs in cells.values())
                 for i in (0, 1)) if cells else None


class CompareTests(unittest.TestCase):
    def run_pair(self, build=None, **kwargs):
        return compare.compare({}, build or {}, fixture(), evaluator, metric,
                               guardrails={'members': {'one': .05, 'two': .05},
                                           'rows': {('one', 'main'): .05}}, **kwargs)

    def test_non_significance_is_not_noninferiority(self):
        for interval, status in (((-.03, .03), 'INCONCLUSIVE'),
                                 ((-.005, .015), 'NON-INFERIOR'),
                                 ((.005, .015), 'NON-INFERIOR'),
                                 ((.021, .04), 'BETTER'),
                                 ((-.04, -.011), 'REGRESSION'),
                                 ((-.01, .02), 'INCONCLUSIVE')):
            with self.subTest(interval=interval), patch.object(compare, 'analyze',
                    return_value={'status': 'NO_CLEAR_DIFFERENCE', 'ci95': interval}):
                # No protected cells in this threshold-only check.
                result = compare.compare({}, {}, fixture(), evaluator, metric,
                                         guardrails={}, enabled=True)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['release_pass'], status in ('BETTER', 'NON-INFERIOR'))
                historical = fixture()
                historical['panel']['inspections'] = 1
                self.assertFalse(compare.compare({}, {}, historical, evaluator, metric,
                                                 guardrails={}, enabled=True)['release_pass'])
        self.assertFalse(self.run_pair({'gain': .03})['release_pass'])

    def test_supported_member_regression_blocks_gain(self):
        result = self.run_pair({'gain': .2, 'loss': .3}, enabled=True)
        self.assertGreater(result['home']['ci95'][0], .02)
        self.assertEqual(result['status'], 'REGRESSION')
        self.assertFalse(result['release_pass'])
        self.assertEqual(result['guardrails']['members']['two']['status'], 'REGRESSION')
        with patch.object(compare, 'analyze', side_effect=[
                {'status': 'B_BETTER', 'ci95': (.04, .08)},
                {'status': 'NO_CLEAR_DIFFERENCE', 'ci95': (-.06, .01)}]):
            result = compare.compare({}, {}, fixture(), evaluator, metric,
                                     guardrails={'members': {'one': .05}}, enabled=True)
            self.assertEqual(result['status'], 'INCONCLUSIVE')
            self.assertFalse(result['release_pass'])

    def test_aa_is_identical_and_inputs_unchanged(self):
        inputs = fixture()
        original = copy.deepcopy(inputs)
        result = compare.compare({}, {}, inputs, evaluator, metric, guardrails={}, enabled=True)
        self.assertEqual(result['home']['ci95'], (0, 0))
        self.assertTrue(result['aa_identical'])
        self.assertEqual(result['input_hash_before'], result['input_hash_after'])
        self.assertEqual(inputs, original)
        for mode in ('nondeterministic', 'mutated', 'privacy', 'pairing', 'unsupported', 'temporal'):
            calls = []
            def broken(build, shared):
                calls.append(None)
                out = evaluator(build, shared)
                if mode == 'nondeterministic':
                    out['weeks'][0]['targets'][0]['values'][('one', 'main')] += len(calls) / 100
                if mode == 'mutated':
                    shared['contexts'].append('changed')
                if mode == 'privacy':
                    out['checks']['privacy'] = False
                if mode == 'pairing' and len(calls) == 3:
                    out['weeks'][0]['targets'][0]['id'] = 'other'
                if mode == 'unsupported':
                    out['evidence']['supported_components'] = ()
                if mode == 'temporal':
                    out['evidence']['evidence_level'] = protocol.DIAGNOSTIC
                return out
            with self.subTest(mode=mode):
                out = compare.compare({}, {}, inputs, broken, metric, guardrails={}, enabled=True)
                self.assertEqual(out['status'], 'INSUFFICIENT / UNSUPPORTED'
                                 if mode in ('unsupported', 'temporal') else 'HARD FAIL')
                self.assertFalse(out['release_pass'])
                self.assertEqual(inputs, original)
