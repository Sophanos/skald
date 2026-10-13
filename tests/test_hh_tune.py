"""Synthetic development tuning and frozen outer comparisons."""
from datetime import datetime, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eval.household import metrics, protocol, splits


SCOPE = ('individual', 'member')
BASELINE = (0, 0, .3)


class TuneTests(unittest.TestCase):
    def setUp(self):
        from eval.household import tune
        self.t = tune
        self.server = SimpleNamespace(SCORE_BLEND=BASELINE)
        self.policy = protocol.manifest()
        start = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)
        self.folds = tuple(splits.Fold(phase, start + i * splits.WEEK,
                           start + (i + 1) * splits.WEEK, (), (), ())
                           for i, phase in enumerate(('development', 'development', 'outer')))

    def report(self, fold, hit, passed=True):
        week = metrics.evaluate_week(SCOPE, fold.origin.isoformat(),
               {'discovery': ['target']}, [{'id': 'row'}],
               {'row': ['target' if hit else 'miss']})
        gate = protocol.result(self.policy, protocol.COMPLETE,
               quality={'recall@20': float(hit), 'ndcg@20': float(hit)},
               quality_passed=passed, required_components=('score_blend',),
               supported_components=('score_blend',))
        return {'weeks': [week], 'gate': gate}

    def select(self, evaluate, **kwargs):
        return self.t.select_development(self.server, self.folds, evaluate,
                   [SCOPE], self.policy, enabled=True, **kwargs)

    def test_tuner_cannot_read_outer_results(self):
        calls = []
        def evaluate(fold):
            calls.append((fold.phase, self.server.SCORE_BLEND))
            self.assertEqual(fold.phase, 'development')
            return self.report(fold, self.server.SCORE_BLEND[0] == .1)
        selected = self.select(evaluate)
        self.assertEqual(selected, (.1, 0, .3))
        self.assertEqual(len(calls), 6)
        self.assertEqual(self.server.SCORE_BLEND, BASELINE)
        for winner in (0, .2):
            calls.clear()
            def outer(fold):
                calls.append(self.server.SCORE_BLEND)
                return self.report(fold, self.server.SCORE_BLEND[0] == winner)
            paired = self.t.evaluate_outer(self.server, selected, self.folds,
                                          outer, enabled=True)
            self.assertEqual(calls, [BASELINE, selected])
            self.assertEqual(len(paired), 1)
            self.assertEqual(selected, (.1, 0, .3))

    def test_decay_applied_once(self):
        event = {'action': 'play', 'weight': 2., 'age_days': 90.,
                 'applied_decay': .5, 'source': 'trakt', 'memory': 3, 'fatigue': .2}
        self.assertEqual(self.t.positive_play_decay(event), event)
        for source in ('trakt', 'tv', 'import'):
            event['source'] = source
            decayed = self.t.positive_play_decay(event, half_life_days=90)
            self.assertEqual(decayed['weight'], 2.)
            self.assertEqual(decayed['memory'], 3)
            self.assertEqual(decayed['fatigue'], .2)
            self.assertAlmostEqual(self.t.positive_play_decay(event, half_life_days=180)
                                   ['weight'], 4 * 2 ** (-.5))
            self.assertEqual(self.t.positive_play_decay(decayed, half_life_days=90), decayed)
        veto = {**event, 'action': 'not_for_us', 'weight': -3.}
        self.assertEqual(self.t.positive_play_decay(veto, half_life_days=90), veto)
        self.assertEqual(event['weight'], 2.)

    def test_unused_parameter_cannot_win(self):
        def ignores_blend(fold):
            result = self.report(fold, True)
            result['gate']['correctness']['unused_weight'] = self.server.SCORE_BLEND[0]
            return result
        self.assertEqual(self.select(ignores_blend), BASELINE)
        self.assertEqual(self.t.DEVELOPMENT_GRID, (BASELINE, (.1, 0, .3), (.2, 0, .3)))

    def test_disabled_by_default_and_evidence_roles(self):
        self.assertEqual(self.t.select_development(None, None, None, None, None), BASELINE)
        self.assertEqual(self.t.evaluate_outer(None, None, None, None), ())
        self.assertEqual(self.t.evidence_role(exposed_w2=True), 'development')
        self.assertEqual(self.t.evidence_role(used_for_selection=True), 'development')
        self.assertEqual(self.t.evidence_role(), 'independent_confirmation')

    def test_same_guardrails_veto_improved_recovery(self):
        for failure in ('coverage', 'metric', 'quality', 'protocol'):
            def evaluate(fold):
                challenger = self.server.SCORE_BLEND != BASELINE
                report = self.report(fold, challenger)
                if challenger:
                    gate = report['gate']
                    if failure == 'coverage':
                        gate['supported_components'] = ()
                    elif failure == 'metric':
                        gate['quality']['ndcg@20'] = -.1
                    elif failure == 'quality':
                        gate['quality_passed'] = False
                    else:
                        gate['protocol_id'] = 'different'
                return report
            with self.subTest(failure=failure):
                self.assertEqual(self.select(evaluate), BASELINE)

    def test_close_results_prefer_unchanged_or_simpler(self):
        for scores, expected in (((.98, .985, .99), BASELINE), ((0, .5, .505), (.1, 0, .3))):
            def evaluate(fold):
                index = self.t.DEVELOPMENT_GRID.index(self.server.SCORE_BLEND)
                report = self.report(fold, True)
                targets = [str(i) for i in range(200)]
                report['weeks'] = [metrics.evaluate_week(SCOPE, fold.origin.isoformat(),
                    {'discovery': targets}, [{'id': 'row'}],
                    {'row': targets[:round(scores[index] * 200)]}, metrics.Budget(cards=200))]
                return report
            self.assertEqual(self.select(evaluate), expected)

    def test_production_score_consumes_registered_blend_and_restores_it(self):
        from decide_kit import load_server, close_server
        with tempfile.TemporaryDirectory() as directory:
            server = load_server(directory)
            try:
                model = {'vec': None, 'vbase': 0, 'aff': {}, 'bscale': 1, 'names': {}}
                with patch.object(server, 'frec', return_value={'kind': 'movie'}), \
                     patch.object(server, 'fit', return_value=1.):
                    def score(fold):
                        return server.score_key(model, 'tmdb:1', kind='movie')[0]
                    self.assertEqual(self.t._evaluate(server, (.2, 0, .3), self.folds[0], score), .2)
                    self.assertEqual(server.SCORE_BLEND, BASELINE)
                    self.assertEqual(score(None), 0)
            finally:
                close_server(server)

    def test_callback_failure_restores_production_parameter(self):
        def fails(fold):
            fold.train += ({'synthetic': True},)
        # The fold itself is frozen, and caller state must survive callback errors.
        with self.assertRaises(AttributeError):
            self.select(fails)
        self.assertEqual(self.server.SCORE_BLEND, BASELINE)
        self.assertEqual(self.folds[0].train, ())

    def test_unknown_source_decay_abstains(self):
        event = {'action': 'play', 'weight': 2., 'age_days': 90.}
        with self.assertRaises(ValueError):
            self.t.positive_play_decay(event, half_life_days=90)

    def test_unregistered_parameter_cannot_reach_outer(self):
        with self.assertRaises(ValueError):
            self.t.evaluate_outer(self.server, (.1, .2, .3), self.folds,
                                  lambda fold: self.fail('unregistered blend ran'), enabled=True)

    def test_empty_week_remains_na(self):
        def evaluate(fold):
            report = self.report(fold, self.server.SCORE_BLEND[0] == .1)
            if fold.origin == self.folds[0].origin:
                report['weeks'] = [metrics.evaluate_week(SCOPE, fold.origin.isoformat(), {}, [], {})]
            return report
        self.assertEqual(self.select(evaluate), (.1, 0, .3))


if __name__ == '__main__':
    unittest.main()
