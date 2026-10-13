"""Command measurement uses synthetic cases and transports only."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from eval import commands_eval as ev
from skald import commands
from decide_kit import load_server, close_server


class CommandEval(unittest.TestCase):
    def test_theme_half_words_and_exact_schedule(self):
        expect = {'op': 'shelf_add', 'values': ['short cozy classic movies', 'sun', '05-12']}
        answer = {'op': 'shelf_add', 'values': [' COZY movies please ', 'SUN', '05-12']}
        self.assertEqual(ev.score(expect, answer), (True, True))
        answer['values'][0] = 'cozy'
        self.assertEqual(ev.score(expect, answer), (True, False))
        answer['values'][0] = 'cozy movies'
        for days, hours in (('sun', '12-05'), ('sunday', '05-12'), (['sun'], '05-12'), ('sun', [5, 12])):
            answer['values'][1:] = [days, hours]
            self.assertEqual(ev.score(expect, answer), (True, False))
        self.assertEqual(ev.score({'op': 'shelf_add', 'values': ['movies', ['sun'], [5, 12]]},
                                  {'op': 'shelf_add', 'values': ['movies', ['sun'], [5, 12]]}), (True, False))
        self.assertEqual(ev.score({'op': 'shelf_stop', 'values': ['friday comedy shelf']},
                                  {'op': 'shelf_stop', 'values': ['comedy friday']}), (True, True))
        self.assertEqual(ev.score({'op': 'like', 'values': ['Dark']},
                                  {'op': 'like', 'values': ['dark movies']}), (True, False))

    def test_null_person_kind_and_numbers(self):
        self.assertEqual(ev.score({'op': None}, {'op': None}), (True, True))
        self.assertEqual(ev.score({'op': None}, None), (False, False))
        self.assertEqual(ev.score({'op': None}, {}), (False, False))
        expect = {'op': 'years', 'person': 'Ana', 'kind': 'MOVIE', 'values': [1990, None]}
        answer = {'op': 'years', 'person': 'ana', 'kind': 'movie', 'values': [1990.0, None]}
        self.assertEqual(ev.score(expect, answer), (True, True))
        answer['person'] = 'ben'
        self.assertEqual(ev.score(expect, answer), (True, False))
        answer['person'], answer['kind'] = 'ana', 'tv'
        self.assertEqual(ev.score(expect, answer), (True, False))
        self.assertEqual(ev.score({'op': 'hide', 'values': [' Anime ']},
                                  {'op': 'hide', 'values': ['anime']}), (True, True))

    def test_fixture_count_no_network_and_json(self):
        with tempfile.TemporaryDirectory() as data, patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            dest = Path(data) / 'results.json'
            out = io.StringIO()
            self.assertEqual(ev.main(['--fixture', '--json', str(dest)], out=out), 0)
            reports = json.loads(dest.read_text())
            report = reports[0]
            self.assertEqual(report['count'], 129)
            self.assertEqual(len(report['results']), 129)
            self.assertEqual(report['no_answer'], 67)
            self.assertEqual(sum(r['op_ok'] for r in report['results']), 61)
            self.assertEqual(sum(r['full_ok'] for r in report['results']), 61)
            self.assertEqual(report['now_count'], 129)
            self.assertEqual(report['now_op_accuracy'], 61 / 129)
            self.assertEqual(report['now_full_accuracy'], 61 / 129)
            self.assertEqual(report['by_tier'], {
                'base': {'now_count': 85, 'op_accuracy': 57 / 85, 'full_accuracy': 57 / 85},
                'hard': {'now_count': 44, 'op_accuracy': 4 / 44, 'full_accuracy': 4 / 44},
            })
            self.assertEqual([r['tier'] for r in report['results']],
                             [c.get('tier', 'base') for c in ev.load_cases()])
            self.assertIn('by-tier now accuracy:', out.getvalue())
            self.assertIn('base 85 cases op 67.06% full 67.06%', out.getvalue())
            self.assertIn('hard 44 cases op 9.09% full 9.09%', out.getvalue())
            self.assertEqual([r['contract'] for r in report['results']],
                             [c['contract'] for c in ev.load_cases()])
            self.assertIn('all op accuracy', out.getvalue())
            self.assertIn('now op accuracy', out.getvalue())
            self.assertIn('now full accuracy', out.getvalue())
            self.assertIn('129', out.getvalue())
            self.assertEqual(ev.evaluate(ev.load_cases()), reports)

    def test_live_metrics_request_and_secret_safe_output(self):
        cases = [{'id': str(i), 'group': 'synthetic', 'contract': 'now' if i == 3 else 'next', 'text': str(i), 'expect': {'op': None}}
                 for i in range(4)]
        ticks = threading.local()
        def clock():
            return getattr(ticks, 'now', 0)
        def transport(url, payload, key, timeout):
            self.assertEqual(url, 'https://example.invalid/api/v1/chat/completions')
            self.assertEqual(key, 'synthetic-key')
            self.assertEqual(timeout, 4)
            self.assertEqual(payload['usage'], {'include': True})
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertEqual(payload['model'], 'synthetic/model')
            i = int(payload['messages'][1]['content'])
            ticks.now = clock() + (i + 1) / 10
            return {'choices': [{'message': {'content': json.dumps({'op': None if i < 3 else 'hide'})}}],
                    'usage': {'cost': .001}}
        reports = ev.evaluate(cases, ['synthetic/model'], transport=transport, key='synthetic-key',
                              base='https://example.invalid/', clock=clock)
        r = reports[0]
        self.assertEqual((r['op_accuracy'], r['full_accuracy']), (.75, .75))
        self.assertEqual((r['now_count'], r['now_op_accuracy'], r['now_full_accuracy']), (1, 0, 0))
        self.assertEqual([row['contract'] for row in r['results']], ['next', 'next', 'next', 'now'])
        self.assertAlmostEqual(r['p50_ms'], 250)
        self.assertAlmostEqual(r['p95_ms'], 385)
        self.assertAlmostEqual(r['total_cost'], .004)
        self.assertAlmostEqual(r['cost_per_1000'], 1)
        self.assertEqual(r['failed_ids'], ['3'])
        out = io.StringIO()
        ev.print_report(r, out)
        self.assertNotIn('synthetic-key', out.getvalue())

    def test_model_effort_request_budget_and_reporting(self):
        cases = [{'id': 'synthetic', 'group': 'synthetic', 'contract': 'now',
                  'text': 'Yes Man', 'expect': {'op': None}}]
        requests = []
        def transport(url, payload, key, timeout):
            requests.append(payload)
            return {'choices': [{'message': {'content': '{"op": null}'}}]}
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-key'}), \
                patch.object(ev, 'load_cases', return_value=cases):
            out = io.StringIO()
            dest = Path(data) / 'reports.json'
            self.assertEqual(ev.main(['--live', '--models',
                                      'synthetic/model@low,synthetic/model,synthetic/model@none',
                                      '--json', str(dest)], transport=transport, out=out), 0)
            self.assertEqual([p['model'] for p in requests], ['synthetic/model'] * 3)
            self.assertEqual([p['reasoning']['effort'] for p in requests], ['low', 'none', 'none'])
            self.assertEqual([p['max_tokens'] for p in requests], [1200, 200, 200])
            self.assertEqual([r['model'] for r in json.loads(dest.read_text())],
                             ['synthetic/model@low', 'synthetic/model@none', 'synthetic/model@none'])
            self.assertIn('synthetic/model@low |', out.getvalue())
            self.assertIn('synthetic/model@none |', out.getvalue())
            self.assertNotIn('synthetic-key', out.getvalue())

    def test_timeout_and_error_are_misses_even_for_null(self):
        cases = [{'id': 'timeout', 'group': 'synthetic', 'contract': 'now', 'text': 'sensitive text', 'expect': {'op': None}}]
        def transport(*args):
            raise TimeoutError('synthetic-key raw model output')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'synthetic-key'}), patch.object(ev, 'load_cases', return_value=cases):
            out = io.StringIO()
            self.assertEqual(ev.main(['--live', '--models', 'synthetic'], transport=transport, out=out), 0)
            self.assertIn('TimeoutError', out.getvalue())
            for secret in ('synthetic-key', 'raw model output', 'sensitive text'):
                self.assertNotIn(secret, out.getvalue())
        r = ev.evaluate(cases, ['synthetic'], transport=transport)[0]
        self.assertEqual((r['timeouts'], r['full_accuracy'], r['op_accuracy']), (1, 0, 0))
        self.assertEqual(r['results'][0]['answer_op'], None)
        def broken(*args):
            return {'choices': [{'message': {'content': 'raw model output'}}], 'usage': {'cost': .003}}
        r = ev.evaluate(cases, ['synthetic'], transport=broken)[0]
        self.assertEqual(r['errors'], {'JSONDecodeError': 1})
        self.assertEqual(r['full_accuracy'], 0)
        self.assertEqual(r['total_cost'], .003)
        def wrapped_timeout(*args):
            raise urllib.error.URLError(TimeoutError('synthetic-key'))
        self.assertEqual(ev.evaluate(cases, ['synthetic'], transport=wrapped_timeout)[0]['timeouts'], 1)

    def test_now_subset_separates_op_and_full_and_handles_empty(self):
        cases = [
            {'id': 'partial', 'group': 'synthetic', 'contract': 'now', 'text': 'weekday mornings short',
             'expect': {'op': 'shelf_add', 'values': ['short', 'sun', '05-12']}},
            {'id': 'miss', 'group': 'synthetic', 'contract': 'now', 'text': 'unsupported', 'expect': {'op': 'hide'}},
            {'id': 'next', 'group': 'synthetic', 'contract': 'next', 'text': 'Yes Man', 'expect': {'op': None}},
        ]
        report = ev.evaluate(cases)[0]
        self.assertEqual((report['now_count'], report['now_op_accuracy'], report['now_full_accuracy']), (2, .5, 0))
        self.assertEqual((report['op_accuracy'], report['full_accuracy']), (2 / 3, 1 / 3))
        empty = ev.evaluate(cases[2:])[0]
        self.assertEqual((empty['now_count'], empty['now_op_accuracy'], empty['now_full_accuracy']), (0, 0, 0))

    def test_tier_accuracy_counts_only_now_and_defaults_to_base(self):
        cases = [
            {'id': 'partial', 'group': 'synthetic', 'contract': 'now', 'text': 'weekday mornings short',
             'expect': {'op': 'shelf_add', 'values': ['short', 'sun', '05-12']}},
            {'id': 'base-miss', 'group': 'synthetic', 'contract': 'now', 'text': 'no horror', 'expect': {'op': 'hide'}},
            {'id': 'base-next', 'group': 'synthetic', 'contract': 'next', 'text': 'Yes Man', 'expect': {'op': None}},
            {'id': 'hard-now', 'group': 'synthetic', 'contract': 'now', 'tier': 'hard', 'text': 'Yes Man', 'expect': {'op': None}},
            {'id': 'hard-next', 'group': 'synthetic', 'contract': 'next', 'tier': 'hard', 'text': 'no horror', 'expect': {'op': 'hide'}},
            {'id': 'other-next', 'group': 'synthetic', 'contract': 'next', 'tier': 'other', 'text': 'Yes Man', 'expect': {'op': None}},
        ]
        report = ev.evaluate(cases)[0]
        self.assertEqual(report['by_tier'], {
            'base': {'now_count': 2, 'op_accuracy': .5, 'full_accuracy': 0},
            'hard': {'now_count': 1, 'op_accuracy': 1, 'full_accuracy': 1},
            'other': {'now_count': 0, 'op_accuracy': 0, 'full_accuracy': 0},
        })
        self.assertEqual(ev.evaluate([])[0]['by_tier'], {})

    def test_command_model_env_and_prompt(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'SKALD_COMMAND_MODEL': 'synthetic/override', 'SKALD_COMMAND_EFFORT': 'none'}):
            s = load_server(data)
            try:
                spec = s.DECISIONS['parse_command']['llm']
                self.assertEqual(s.COMMAND_MODEL, 'synthetic/override')
                self.assertEqual(spec['model'], s.COMMAND_MODEL)
                body = spec['prompt']([{'query': 'hide anime'}])
                self.assertEqual(body['model'], s.COMMAND_MODEL)
                self.assertEqual(s.COMMAND_EFFORT, 'none')
                self.assertEqual(body['reasoning'], {'effort': 'none'})
                self.assertEqual(body['max_tokens'], 200)
                self.assertEqual(spec['timeout'], 6)
                self.assertEqual(body['messages'][0]['content'], commands.command_prompt(s))
                synthetic, _ = ev.synthetic_spec(data)
                self.assertEqual(commands.command_prompt(synthetic), commands.command_prompt(s))
            finally:
                close_server(s)
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'SKALD_COMMAND_MODEL': '', 'SKALD_COMMAND_EFFORT': ''}):
            s = load_server(data)
            try:
                self.assertEqual(s.COMMAND_MODEL, '~google/gemini-flash-latest')
                self.assertEqual(s.COMMAND_EFFORT, 'low')
                spec = s.DECISIONS['parse_command']['llm']
                body = spec['prompt']([{'query': 'hide anime'}])
                self.assertEqual(body['reasoning'], {'effort': 'low'})
                self.assertEqual(body['max_tokens'], 1200)
                self.assertEqual(spec['timeout'], 6)
            finally:
                close_server(s)
