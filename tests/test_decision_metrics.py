import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval import decisions

from decide_kit import close_server, fixture_rows, load_server, run_type


class DecisionMetricsTests(unittest.TestCase):
    def test_replay_rows_do_not_share_daily_call_cap(self):
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            self.addCleanup(close_server, server)
            answer = {"choice": "title", "confidence": .93, "watched": "any", "list": "taste"}
            rows = [{"in": {"query": f"Synthetic independent query {i}"}, "expect": answer,
                     "recorded": {"answers": {"route": {"choice": "title", "confidence": .93},
                                              "watched": {"choice": "any"}, "list": {"choice": "taste"}}},
                     "note": "Synthetic independent decision"} for i in range(3)]
            path = Path(data, "rows.jsonl")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with patch.dict(server.AI_CAPS, {"search": 1}):
                metrics = run_type(server, "route_query", str(path))
            self.assertEqual(metrics["sources"], {"llm": 3})
            self.assertEqual(metrics["llm_calls"], 3)
            self.assertEqual(metrics["acc"], 1)

    def test_live_case_loader_has_accepted_routes_and_optional_fields(self):
        rows = decisions.live_cases()
        self.assertEqual(len(rows), 235)  # Ten AI credit and title-lookalike routing cases.
        self.assertEqual({r['lang'] for r in rows}, {'en', 'de', 'sr', 'es', 'pt'})
        self.assertEqual(next(r for r in rows if r['q'] == 'Dark')['route'], ['title'])
        self.assertEqual(next(r for r in rows if r['q'] == 'Dark series')['route'], ['title', 'request'])
        self.assertTrue(any('watched' not in r and 'list' not in r for r in rows))

    def test_live_skip_without_key_never_loads_server(self):
        with patch.dict(os.environ, {'OR_KEY': '', 'OPENROUTER_API_KEY': ''}), \
                patch.object(decisions, 'load_server', side_effect=AssertionError('no live call')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(decisions.main(['--type', 'route_query', '--live']), 0)
        self.assertIn('skipped', output.getvalue())

    def test_live_scores_each_question_reports_failures_and_threshold(self):
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            rows = [{'q': f'Synthetic query {i}', 'route': ['title', 'request'], 'lang': 'en' if i < 5 else 'sr', 'tag': 'synthetic'}
                    for i in range(10)]
            for failures, expected_code in ((1, 0), (2, 1)):
                answers = [{'answer': {'choice': 'request', 'watched': 'seen' if failures == 2 and i == 1 else 'any',
                                       'list': 'best' if i == 0 else 'taste'}} for i in range(10)]
                with patch.dict(os.environ, {'OR_KEY': 'synthetic-key'}), \
                        patch.object(decisions, 'load_server', return_value=server), \
                        patch.object(decisions, 'close_server'), patch.object(decisions, 'live_cases', return_value=rows, create=True), \
                        patch.object(server, 'decide', side_effect=answers) as decide, \
                        patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                        patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(decisions.main(['--type', 'route_query', '--live', '--json']), expected_code)
                result = json.loads(output.getvalue())['route_query']
                self.assertEqual(result['accuracy'], {'route': 1, 'watched': .9 if failures == 2 else 1, 'list': .9})
                self.assertEqual(result['all_three'], 1 - failures / 10)
                self.assertEqual(result['by_lang'], {'en': {'n': 5, 'all_questions': 1 - failures / 5, 'all_three': 1 - failures / 5},
                                                    'sr': {'n': 5, 'all_questions': 1, 'all_three': 1}})
                self.assertEqual(len(result['failed']), failures)
                self.assertEqual(result['failed'][0]['in'], {'query': rows[0]['q']})
                self.assertEqual(decide.call_count, 10)
                self.assertEqual(server.OR_KEY, 'synthetic-key')
            close_server(server)

    def test_recorded_and_synthetic_accuracy_and_empty_group(self):
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            self.addCleanup(close_server, server)
            rows = fixture_rows(server, "route_query")[:3]
            rows[0]["note"] = "Recorded case"
            for row in rows[1:]:
                row["note"] = "Synthetic case"
            rows[2]["expect"]["choice"] = "wrong"
            path = Path(data, "rows.jsonl")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            metrics = run_type(server, "route_query", str(path))
            self.assertEqual(metrics["recorded"], {"n": 1, "acc": 1})
            self.assertEqual(metrics["synthetic"], {"n": 2, "acc": .5})
            self.assertEqual(metrics["acc"], 2 / 3)
            path.write_text(json.dumps(rows[0]) + "\n")
            self.assertEqual(run_type(server, "route_query", str(path))["synthetic"], {"n": 0, "acc": None})
