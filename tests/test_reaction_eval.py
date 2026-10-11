from skald.members import rid
import unittest
from unittest.mock import patch

from eval import reaction
from test_taste_report import TasteCase, NOW


class ReactionEvalTests(TasteCase):
    def test_hand_checked_metrics(self):
        self.assertAlmostEqual(reaction.kendall(['a', 'b', 'c'], ['b', 'a', 'c']), 1/3)
        self.assertEqual(reaction.kendall(['a', 'b'], ['b', 'c']), 1)
        self.assertEqual(reaction.percentile([1, 2, 3], .5), 2)
        related = reaction.related_map(self.e, ['tmdb:3', 'tmdb:8', 'tmdb:4'])
        self.assertIn('tmdb:8', related['tmdb:3'])  # same experience and keyword.
        self.assertNotIn('tmdb:4', related['tmdb:3'])  # only experience in common.

    def test_all_checks_and_failing_k3(self):
        s = self.s
        pre = {rid('foryou', s.CONFIG): [f'tmdb:{i}' for i in range(3, 15)]}
        post = {rid('foryou', s.CONFIG): [f'tmdb:{i}' for i in range(5, 17)]}
        m = self.e._model
        m['paff']['ben'] = {'x:cozy_case': .1}
        after = {**m, 'paff': {**m['paff'], 'ben': {'x:cozy_case': 1.8}}}
        p = {**self.e._profile, 'seen': {'tmdb:1', 'tmdb:2', 'tmdb:20'}}
        with patch.object(reaction, 'feedback_model', return_value=(p, after, s.feedback_state())), \
                patch.object(reaction, 'served', side_effect=[post, {rid('foryou', s.CONFIG): []}]), \
                patch.object(self.e, '_rank', side_effect=[['tmdb:3'], ['tmdb:21', 'tmdb:22', 'tmdb:3']]), \
                patch.object(reaction, 'latency', return_value=1):
            r = reaction.measure(self.e, 'tmdb:20', 'ben', {'tmdb:21', 'tmdb:22'}, pre)
        self.assertEqual(r['values'], {'K1': 2, 'K2': 1., 'K2b': 2, 'K3': 1.7, 'K4': 0, 'K5': 1})
        self.assertFalse(r['checks']['K3'])
        self.assertEqual(r['K3_limit'], .05)
        self.assertTrue(all(v for k, v in r['checks'].items() if k != 'K3'))

    def test_real_fixture_k4_and_k5_restore(self):
        stages = [("remote", [self.s._event(NOW-86400, '1', 'tmdb:1', 'movie', 'Fixture 1', '2020',
                   'ben', ['ben'], 'love', 'remote', {'row': None})])]
        pre = {rid('foryou', self.s.CONFIG): ['tmdb:3', 'tmdb:4', 'tmdb:5']}
        with patch('time.time', return_value=NOW), patch.object(reaction, 'served', return_value=pre):
            selected, related = reaction.probes(self.e, 'ben', 20, 7)
            self.assertTrue(selected)
            p, m, state = reaction.feedback_model(self.e, 'tmdb:3', 'ben', 'not_for_us', ['too slow'])
            self.assertIn('tmdb:3', state['blocked'])
            with reaction.model_view(self.e, p, m, state):
                self.assertNotIn('tmdb:3', [v['id'] for v in self.s.remote_order([{'id': 'tmdb:3'}], self.s.remote_masks())])
            with patch.object(self.s, 'refresh_profiles') as refresh, patch.object(self.s, 'get_model'), \
                    patch.object(self.s, 'row_metas', side_effect=[[{'id': 'tmdb:3'}], [{'id': 'tmdb:4'}]]):
                self.assertEqual(reaction.latency(self.e, 'tmdb:20', 'ben'), 1)
                self.assertEqual(refresh.call_count, 2)
        self.assertFalse((self.e._work / 'feedback.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
