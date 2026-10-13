import copy
import unittest

from eval.household.uncertainty import analyze, stationary_bootstrap


def fixture():
    return [{'week': i, 'mature': True, 'targets': [
        {'id': f'{i}-{j}', 'franchise': f'f{j}', 'values': {
            ('alice', 'main'): (i, i + 1),
            ('bob', 'main'): (-i, -i + 1)}} for j in range(3)]}
        for i in range(12)]


def macro(weeks):
    cells = {}
    for week in weeks:
        for target in week['targets']:
            for key, pair in target['values'].items():
                cells.setdefault(key, []).append(pair)
    scopes = {}
    for (scope, row), pairs in cells.items():
        scopes.setdefault(scope, []).append(tuple(
            sum(p[i] for p in pairs) / len(pairs) for i in (0, 1)))
    return tuple(sum(sum(p[i] for p in rows) / len(rows)
                     for rows in scopes.values()) / len(scopes) for i in (0, 1))


class UncertaintyTests(unittest.TestCase):
    def test_bootstrap_keeps_ab_and_members_paired(self):
        weeks = fixture()
        original = copy.deepcopy(weeks)
        draws = list(stationary_bootstrap(weeks, 2, replicates=20))
        self.assertEqual(draws, list(stationary_bootstrap(weeks, 2, replicates=20)))
        self.assertTrue(any(len({w['week'] for w in d}) < 12 for d in draws))
        for draw in draws:
            self.assertEqual(len(draw), 12)
            for week in draw:
                self.assertIs(week, weeks[week['week']])
        calls = []
        def checked(draw):
            calls.append(len(draw))
            for week in draw:
                for t in week['targets']:
                    a, b = t['values'][('alice', 'main')]
                    c, d = t['values'][('bob', 'main')]
                    self.assertEqual((a + c, b - a, d - c), (0, 1, 1))
            return macro(draw)
        report = analyze(weeks, checked)
        self.assertEqual(calls.count(12), 15004)
        self.assertEqual(report['status'], 'B_BETTER')
        for endpoint in report['ci95']:
            self.assertAlmostEqual(endpoint, 1)
        self.assertEqual(len(report['leave_one_week']), 12)
        self.assertEqual(len(report['leave_one_franchise']), 3)
        self.assertTrue(all(abs(d['shift']) < 1e-12
                            for d in report['leave_one_franchise']))
        self.assertEqual(weeks, original)

    def test_zero_target_weeks_remain_in_resampling(self):
        weeks = fixture()
        for w in weeks[8:]:
            w['targets'] = []
        for w in weeks[:8]:
            w['targets'].append(copy.deepcopy(w['targets'][0]))
            w['targets'][-1]['id'] += '-extra'
        draws = list(stationary_bootstrap(weeks, 2, replicates=30))
        self.assertTrue(any(not w['targets'] for d in draws for w in d))
        def calendar_metric(draw):
            return (0, sum(bool(w['targets']) for w in draw) / len(draw))
        report = analyze(weeks, calendar_metric)
        self.assertEqual(report['support']['weeks'], 12)
        self.assertEqual(report['support']['target_weeks'], 8)
        self.assertEqual(report['delta'], 8 / 12)
        self.assertLess(report['ci95'][0], report['delta'])

    def test_sparse_support_returns_insufficient(self):
        for weeks in ([], fixture()[:11], fixture()[:7] + [
                {'week': i, 'mature': True, 'targets': []} for i in range(7, 12)]):
            report = analyze(weeks, lambda _: self.fail('unsupported metric called'))
            self.assertEqual(report['status'], 'INSUFFICIENT')
            self.assertIsNone(report['ci95'])
        weeks = fixture()
        for w in weeks:
            for t in w['targets']:
                t['id'] = t['id'][-1]
        self.assertEqual(analyze(weeks, macro)['status'], 'INSUFFICIENT')
        weeks = fixture()
        weeks[0]['targets'][0]['values'][('rare', 'rare')] = (0, 100)
        report = analyze(weeks, macro)
        self.assertEqual(report['scopes']['rare']['status'], 'INSUFFICIENT')
        self.assertEqual(report['rows'][('rare', 'rare')]['status'], 'INSUFFICIENT')
        weeks[0]['cells'] = [('empty', 'empty')]
        self.assertEqual(analyze(weeks, macro)['scopes']['empty']['status'], 'INSUFFICIENT')
        weeks[-1]['mature'] = False
        self.assertEqual(analyze(weeks, macro)['status'], 'INSUFFICIENT')

    def test_block_sensitivity_and_undefined_replicates_abstain(self):
        from unittest.mock import patch
        weeks = fixture()
        def draws(frame, block_length, **kwargs):
            yield [frame[0 if block_length == 1 else -1]] * len(frame)
        with patch('eval.household.uncertainty.stationary_bootstrap', draws):
            self.assertEqual(analyze(weeks, lambda w: (0, w[0]['week'] - 5))
                             ['status'], 'INCONCLUSIVE')
            self.assertEqual(analyze(weeks, lambda w: (0, w[0]['week'] + 1))
                             ['status'], 'INCONCLUSIVE')
        self.assertEqual(analyze(weeks, lambda _: (0, float('nan')))
                         ['status'], 'INCONCLUSIVE')
        def incomplete(draw):
            return (0, 1) if len({w['week'] for w in draw}) == 12 else None
        report = analyze(weeks, incomplete)
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertIsNone(report['ci95'])

    def test_recomputes_macro_and_exclusions(self):
        weeks = fixture()
        for week in weeks:
            for target in week['targets']:
                target['values'][('bob', 'main')] = (0, 1)
        def metric(frame):
            return (0, macro(frame)[1] ** 2)
        report = analyze(weeks, metric)
        self.assertEqual(report['delta'], metric(weeks)[1])
        self.assertNotEqual(report['delta'], sum(metric([w])[1] for w in weeks) / 12)
        for i, diagnostic in enumerate(report['leave_one_week']):
            expected = metric(weeks[:i] + weeks[i + 1:])[1]
            self.assertEqual(diagnostic['delta'], expected)
            self.assertEqual(diagnostic['shift'], expected - report['delta'])
        for diagnostic in report['leave_one_franchise']:
            reduced = [{**w, 'targets': [t for t in w['targets']
                        if t['franchise'] != diagnostic['franchise']]} for w in weeks]
            self.assertEqual(diagnostic['delta'], metric(reduced)[1])
