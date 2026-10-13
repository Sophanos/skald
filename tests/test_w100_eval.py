"""Router comparison metrics and state use offline replies."""
import io
import unittest
from unittest.mock import patch

from eval import decisions, route_compare


class FollowupEvals(unittest.TestCase):
    def test_score_previous_only_when_expected(self):
        row = dict(q='more like this', last='dark movies', shown='One, Two, Three',
                   route=['request'], previous='refine', lang='en')
        answer = dict(choice='request', watched='any', list='taste', previous='new')
        self.assertEqual(decisions.route_input(row), dict(query=row['q'], last=row['last'], shown=row['shown']))
        self.assertEqual(decisions.route_score(row, answer), dict(route=True, watched=True, list=True, previous=False))
        row.pop('previous')
        self.assertNotIn('previous', decisions.route_score(row, answer))

    def test_comparison_state_metrics_and_cost(self):
        cases = [dict(q='more like this', last='dark movies', shown='One, Two, Three',
                      route=['request'], previous='refine', lang='en'),
                 dict(q='Dark', route=['title'], lang='de')]
        questions = {'route': {}, 'watched': {}, 'list': {}, 'previous': {}}
        def post(path, body, key, timeout):
            self.assertEqual(key, 'offline-key')
            if body['state'].startswith('Last search:'):
                self.assertEqual(body['state'], 'Last search: dark movies\nSome results shown: One, Two, Three\nTyped now: more like this')
                answers = dict(route='request', watched='any', list='taste', previous='new')
            else:
                answers = dict(route='title', watched='any', list='taste')
            return dict(answers={k: {'choice': v} for k, v in answers.items()}, usage={'cost': .002})
        with patch.object(route_compare, 'post', side_effect=post):
            result = route_compare.run(('fixture-model', 'followup'), questions, cases, 'offline-key')
        metrics = route_compare.summarize(result['rows'])
        self.assertEqual(metrics['accuracy'], dict(route=1, watched=1, list=1, previous=0))
        self.assertEqual(metrics['question_counts']['previous'], 1)
        self.assertEqual(metrics['all'], .5)
        self.assertEqual(metrics['cost_per_1000'], 2)
        out = io.StringIO()
        route_compare.print_table([result], out)
        for text in ('fixture-model', 'followup', 'previous', 'p50', 'USD/1000', 'en', 'de'):
            self.assertIn(text, out.getvalue())

    def test_no_key_no_network(self):
        with patch.dict('os.environ', {'OR_KEY': '', 'OPENROUTER_API_KEY': ''}), \
                patch.object(route_compare, 'post', side_effect=AssertionError('network forbidden')):
            self.assertEqual(route_compare.main(['--models', 'fixture-model'], out=io.StringIO()), 1)
