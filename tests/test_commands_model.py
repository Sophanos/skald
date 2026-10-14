"""Production command routing and fallback use synthetic transport replies."""
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald.commands import NONE, OUT


class CommandModel(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {'SKALD_COMMAND_MODEL': '', 'SKALD_COMMAND_EFFORT': ''}):
            self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        self.answer = {'op': 'hide', 'kind': None, 'values': ['anime'], 'person': None}
        self.reply = {'choices': [{'message': {'content': json.dumps(self.answer)}}]}
        self.requests = []
        self.key = patch.object(self.s, 'OR_KEY', 'synthetic-key')
        self.key.start()
        self.addCleanup(self.key.stop)
        self.guard = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.addCleanup(self.join_usage)

    def join_usage(self):
        for thread in threading.enumerate():
            if getattr(thread, '_target', None) is self.s.save_usage:
                thread.join(2)

    def run_command(self, replies, query='hide anime', **kwargs):
        replies = iter(replies)
        def transport(request, *, timeout):
            self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/chat/completions')
            self.requests.append((json.loads(request.data), timeout))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        with patch.object(self.s, '_connector_open', side_effect=transport):
            return self.s.decide('parse_command', {'query': query}, **kwargs)

    def assert_requests(self, fallback=False):
        expected = [('~google/gemini-flash-latest', 'low', 1200, 6)]
        if fallback:
            expected.append(('z-ai/glm-5.3-flash', 'low', 1200, 6))
        self.assertEqual([(p['model'], p['reasoning']['effort'], p['max_tokens'], t)
                          for p, t in self.requests], expected)
        if fallback:
            self.assertEqual(self.requests[0][0]['messages'], self.requests[1][0]['messages'])
        for payload, _ in self.requests:
            self.assertEqual(payload['response_format'], {'type': 'json_object'})

    def test_version_eight_ignores_fresh_version_seven_cache(self):
        spec = self.s.DECISIONS['parse_command']
        self.assertEqual(spec['ver'], 15)
        self.assertEqual(spec['ttl'], 30 * self.s.DAY)
        inp = self.s.route_norm({'query': 'hide anime'})
        digest = hashlib.sha256(json.dumps(inp, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:32]
        self.s._decision_put('parse_command|7|' + digest, {**spec, 'ver': 7},
                             {**self.answer, 'op': 'show'}, 1)
        result = self.run_command([self.reply])
        self.assertEqual(result['ver'], 15)
        self.assertEqual(result['source'], 'llm')
        self.assertEqual(result['answer'], self.answer)
        self.assert_requests()

    def test_fallback_uses_real_server_openrouter_and_check_names(self):
        inspect.signature(self.s.openrouter).bind('/api/v1/chat/completions', {}, 4, 'search', 'parse_command')
        inspect.signature(self.s.check).bind(OUT, self.answer)
        with (patch.object(self.s, 'openrouter', wraps=self.s.openrouter) as router,
              patch.object(self.s, 'check', wraps=self.s.check) as validate):
            result = self.run_command([TimeoutError('synthetic'), self.reply])
            self.assertEqual(router.call_count, 2)
            validate.assert_any_call(OUT, self.answer)
        self.assertEqual(result['answer'], self.answer)
        self.assert_requests(fallback=True)

    def test_primary_success_does_not_call_glm(self):
        result = self.run_command([self.reply])
        self.assertEqual(result['answer'], self.answer)
        self.assertEqual(result['source'], 'llm')
        self.assert_requests()

    def test_primary_failures_try_glm_once(self):
        failures = [urllib.error.HTTPError('https://example.invalid', 503, 'synthetic', {}, None),
                    TimeoutError('synthetic'),
                    {'choices': [{'message': {'content': '{bad json'}}]},
                    {'choices': [{'message': {'content': '{"op":"invented","values":[]}'}}]}]
        for i, failure in enumerate(failures):
            with self.subTest(failure=i):
                self.requests.clear()
                result = self.run_command([failure, self.reply], query=f'hide anime synthetic {i}')
                self.assertEqual(result['answer'], self.answer)
                self.assert_requests(fallback=True)

    def test_glm_failure_returns_none_without_third_attempt(self):
        for i, failure in enumerate([TimeoutError('synthetic'),
                                     urllib.error.HTTPError('https://example.invalid', 503, 'synthetic', {}, None),
                                     {'choices': [{'message': {'content': '{bad json'}}]},
                                     {'choices': [{'message': {'content': '{"op":"invented","values":[]}'}}]}]):
            with self.subTest(failure=i):
                self.requests.clear()
                result = self.run_command([TimeoutError('synthetic'), failure], query=f'hide anime failure {i}')
                self.assertEqual(result['answer'], NONE)
                self.assert_requests(fallback=True)

    def assert_accounting(self, result, cost, over=False):
        self.assertAlmostEqual(result['cost_usd'], cost)
        self.assertTrue(self.s.flush_decisions())
        receipts = [json.loads(line) for line in Path(self.s.DATA, 'decisions.jsonl').read_text().splitlines()]
        receipt = next(r for r in receipts if r['id'] == result['receipt'])
        self.assertAlmostEqual(receipt['cost'], cost)
        self.assertEqual(receipt.get('over', False), over)
        stats = self.s.decide_stats()['parse_command']
        self.assertAlmostEqual(stats['cost'], cost)
        self.assertEqual(stats['over'], over)
        self.assertEqual(stats['llm_calls'], len(self.requests))
        self.assertAlmostEqual(self.s._usage['month_cost'], cost)

    def test_timeout_fallback_cost_is_recorded(self):
        result = self.run_command([TimeoutError('synthetic'), {**self.reply, 'usage': {'cost': .003}}])
        self.assertEqual(result['answer'], self.answer)
        self.assert_accounting(result, .003)
        self.assert_requests(fallback=True)

    def test_over_budget_primary_does_not_spend_fallback(self):
        primary = {'choices': [{'message': {'content': '{bad json'}}], 'usage': {'cost': .03}}
        result = self.run_command([primary, self.reply])
        self.assertEqual(result['answer'], NONE)
        self.assert_accounting(result, .03, over=True)
        self.assert_requests()

    def test_fallback_over_budget_is_flagged_and_blocks_next_call(self):
        result = self.run_command([TimeoutError('synthetic'), {**self.reply, 'usage': {'cost': .03}}])
        self.assertEqual(result['answer'], self.answer)
        self.assert_accounting(result, .03, over=True)
        self.assert_requests(fallback=True)
        refused = self.run_command([], query='hide anime another synthetic')
        self.assertEqual(refused['answer'], NONE)
        self.assertEqual(refused['why'], 'over')
        self.assert_requests(fallback=True)

    def test_cost_sums_both_attempts_and_flags_total_over_limit(self):
        primary = {'choices': [{'message': {'content': '{bad json'}}], 'usage': {'cost': .0015}}
        result = self.run_command([primary, {**self.reply, 'usage': {'cost': .0015}}], max_usd=.002)
        self.assertEqual(result['answer'], self.answer)
        self.assert_accounting(result, .003, over=True)
        self.assert_requests(fallback=True)

    def test_bad_json_fallback_still_records_its_cost(self):
        secondary = {'choices': [{'message': {'content': '{bad json'}}], 'usage': {'cost': .003}}
        result = self.run_command([TimeoutError('synthetic'), secondary])
        self.assertEqual(result['answer'], NONE)
        self.assert_accounting(result, .003)
        self.assert_requests(fallback=True)

    def test_fallback_rechecks_decision_cap(self):
        with patch.dict(self.s.DECISIONS['parse_command'], {'cap': 1}):
            result = self.run_command([TimeoutError('synthetic'), self.reply])
        self.assertEqual(result['answer'], NONE)
        self.assertEqual(result['why'], 'cap')
        self.assert_requests()

    def test_fallback_rechecks_monthly_gate(self):
        primary = {'choices': [{'message': {'content': '{bad json'}}], 'usage': {'cost': .008}}
        with (patch.object(self.s, 'AI_MONTHLY_USD', .015),
              patch.dict(self.s.DECISIONS['parse_command'], {'max_usd': .01})):
            result = self.run_command([primary, self.reply])
        self.assertEqual(result['answer'], NONE)
        self.assertEqual(result['why'], 'month')
        self.assert_accounting(result, .008)
        self.assert_requests()

    def test_fallback_rechecks_api_share(self):
        with patch.dict(self.s.AI_CAPS, {'search': 4}), patch.object(self.s, 'API_LANE_SHARE', .25):
            result = self.run_command([TimeoutError('synthetic'), self.reply], client='client-one')
        self.assertEqual(result['answer'], NONE)
        self.assertEqual(result['why'], 'api_share')
        self.assertEqual(self.s._usage['calls']['api:search'], 1)
        self.assert_requests()

    def test_rules_and_disabled_llm_do_not_call_models(self):
        self.assertEqual(self.run_command([], allow_llm=False)['answer'], NONE)
        self.assertEqual(self.run_command([], query='make me a row of heist films', allow_llm=False)['answer'], NONE)
        self.assertEqual(self.requests, [])

    def test_budget_refusal_does_not_try_glm(self):
        with self.s._usage_lock:
            self.s._usage_roll()
            self.s._usage['calls']['search:other'] = self.s.AI_CAPS['search']
        result = self.run_command([])
        self.assertEqual(result['answer'], NONE)
        self.assertEqual(result['why'], 'BudgetExceeded')
        self.assertEqual(self.requests, [])
