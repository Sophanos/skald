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
    def test_plan_scores_every_step_in_order(self):
        steps = [{'op': 'hide', 'values': ['anime'], 'person': 'ana'},
                 {'op': 'runtime', 'kind': 'movie', 'values': [90]}]
        expect = {'op': 'plan', 'steps': steps}
        self.assertEqual(ev.score(expect, expect), (True, True))
        for actual in (None, {}, [], steps[:1], steps * 2, steps[::-1],
                       [steps[0], {'op': 'runtime', 'kind': 'movie', 'values': [120]}],
                       [steps[0], None]):
            with self.subTest(steps=actual):
                self.assertEqual(ev.score(expect, {'op': 'plan', 'steps': actual}), (True, False))
        self.assertEqual(ev.score(expect, steps[0]), (False, False))

    def test_ask_requires_question_and_correct_slot_without_guessed_change(self):
        expect = {'op': 'ask', 'slot': 'person'}
        answer = {**expect, 'question': 'Für wen soll das gelten?'}
        self.assertEqual(ev.score(expect, answer), (True, True))
        for extra in ({'question': ''}, {'question': '  '}, {'question': None},
                      {'slot': 'title'}, {'values': ['anime']}, {'person': 'ana'},
                      {'kind': 'movie'}, {'steps': [{'op': 'hide'}]}):
            with self.subTest(extra=extra):
                self.assertEqual(ev.score(expect, {**answer, **extra}), (True, False))
        self.assertEqual(ev.score(expect, expect), (True, False))
        self.assertEqual(ev.score(expect, {'op': 'hide', 'values': ['anime']}), (False, False))

    def test_ask_rejects_guessed_date_and_schedule(self):
        cases = {c['id']: c for c in ev.load_cases()}
        for case_id, guessed in (('next-069', {'until': '2027-01-05'}),
                                 ('next-070', {'months': [12]})):
            with self.subTest(id=case_id):
                expect = cases[case_id]['expect']
                answer = {**expect, 'question': 'Which date or schedule do you mean?'}
                self.assertEqual(ev.score(expect, answer), (True, True))
                self.assertEqual(ev.score(expect, {**answer, **guessed}), (True, False))

    def test_future_fields_and_unknown_ops_are_scored_without_runtime_validation(self):
        expect = {'op': 'shelf_filter', 'filters': {'languages': ['DE'], 'runtime': 90}}
        self.assertEqual(ev.score(expect, {'op': 'shelf_filter',
                                         'filters': {'languages': ['de'], 'runtime': 90}}), (True, True))
        self.assertEqual(ev.score(expect, {'op': 'shelf_filter'}), (True, False))
        self.assertEqual(ev.score(expect, {'op': 'shelf_filter',
                                         'filters': {'languages': ['de'], 'runtime': 120}}), (True, False))
        cases = [{'id': 'future', 'group': 'synthetic', 'text': 'synthetic future instruction',
                  'expect': {'op': 'unknown_future_op'}, 'contract': 'next'}]
        report = ev.evaluate(cases)[0]
        self.assertEqual(report['errors'], {})
        self.assertEqual(report['next_count'], 1)
        self.assertEqual(report['next_full_accuracy'], 0)

    def test_case_metadata_languages_and_ids(self):
        cases = [json.loads(line) for line in (ev.ROOT / 'eval/commands_cases.jsonl').read_text().splitlines()]
        self.assertEqual(len({c['id'] for c in cases}), len(cases))
        for case in cases:
            with self.subTest(id=case.get('id')):
                self.assertTrue({'id', 'group', 'text', 'expect', 'contract'} <= case.keys())
                self.assertIn(case['contract'], ('now', 'next'))
                self.assertIn(case.get('lang', 'en'), ('en', 'de', 'sr', 'es', 'pt'))
                self.assertTrue(case['id'] and case['group'] and case['text'])
                self.assertIn('op', case['expect'])
        for lang in ('de', 'sr'):
            self.assertTrue(any(c.get('lang') == lang and c['contract'] == 'now' for c in cases))
        german_ids = {'create-6', 'create-7', 'create-8', 'w75-3', 'w75-4', 'w75-10',
                      'w75-14', 'w75-15', 'w75-16', 'x-ind-6'}
        self.assertTrue(all(c.get('lang') == 'de' for c in cases if c['id'] in german_ids))
        self.assertTrue({'rows', 'schedule', 'filter', 'weights', 'training', 'people', 'meta',
                         'creation', 'settings history'} <= {
                             c['group'] for c in cases if c.get('lang') == 'sr' and c['contract'] == 'now'})

    def test_future_model_reply_keeps_nested_plan_and_question(self):
        cases = [
            {'id': 'plan', 'group': 'synthetic', 'contract': 'next', 'text': 'plan',
             'expect': {'op': 'plan', 'steps': [{'op': 'hide', 'values': ['anime']},
                                             {'op': 'years', 'values': [2000, None]}]}},
            {'id': 'ask', 'group': 'synthetic', 'contract': 'next', 'text': 'ask',
             'expect': {'op': 'ask', 'slot': 'person'}},
            {'id': 'future', 'group': 'synthetic', 'contract': 'next', 'text': 'future',
             'expect': {'op': 'member_create', 'person': 'kim', 'age': 12}},
        ]
        answers = {c['text']: c['expect'] for c in cases}
        answers['ask'] = {**answers['ask'], 'question': 'Za koga?'}
        def transport(url, payload, key, timeout):
            answer = answers[payload['messages'][1]['content']]
            return {'choices': [{'message': {'content': json.dumps(answer)}}]}
        r = ev.evaluate(cases, ['synthetic/model'], transport=transport)[0]
        self.assertEqual(r['errors'], {})
        self.assertEqual((r['next_count'], r['next_op_accuracy'], r['next_full_accuracy']), (3, 1, 1))

    def test_search_routing_target_has_multilingual_synthetic_cases(self):
        rows = [json.loads(line) for line in
                (ev.ROOT / 'eval/fixtures/decisions/route_query.jsonl').read_text().splitlines()]
        for lang in ('en', 'de', 'sr'):
            target = [r for r in rows if r['note'].startswith('Synthetic: ' + lang + ' search target')]
            self.assertGreaterEqual(len(target), 10)
            self.assertEqual({r['expect']['choice'] for r in target}, {'request', 'title'})
            for row in target:
                self.assertEqual(set(row), {'in', 'expect', 'recorded', 'note'})
                self.assertIn(commands.parse_local(row['in']['query'])['op'], (None, 'search'))

    def test_next_columns_do_not_lower_now_accuracy(self):
        cases = [
            {'id': 'now', 'group': 'synthetic', 'contract': 'now', 'text': 'history', 'expect': {'op': 'history'}},
            {'id': 'next-hit', 'group': 'synthetic', 'contract': 'next', 'text': 'Yes Man', 'expect': {'op': None}},
            {'id': 'next-miss', 'group': 'synthetic', 'contract': 'next', 'text': 'unknown', 'expect': {'op': 'ask', 'slot': 'person'}},
        ]
        r = ev.evaluate(cases)[0]
        self.assertEqual((r['now_count'], r['now_op_accuracy'], r['now_full_accuracy']), (1, 1, 1))
        self.assertEqual((r['next_count'], r['next_op_accuracy'], r['next_full_accuracy']), (2, .5, .5))
        out = io.StringIO()
        ev.print_report(r, out)
        self.assertIn('next cases | next op accuracy | next full accuracy', out.getvalue())
        empty = ev.evaluate(cases[:1])[0]
        self.assertEqual((empty['next_count'], empty['next_op_accuracy'], empty['next_full_accuracy']), (0, 0, 0))

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
            self.assertEqual(report['count'], 451)
            self.assertEqual(len(report['results']), 451)
            self.assertEqual(report['no_answer'], 145)
            self.assertEqual(sum(r['op_ok'] for r in report['results']), 300)
            self.assertEqual(sum(r['full_ok'] for r in report['results']), 299)
            self.assertEqual(report['now_count'], 451)
            self.assertEqual(report['now_op_accuracy'], 300 / 451)
            self.assertEqual(report['now_full_accuracy'], 299 / 451)
            self.assertEqual((report['next_count'], report['next_op_accuracy'], report['next_full_accuracy']), (0, 0, 0))
            self.assertEqual(report['by_tier'], {
                'followup': {'now_count': 18, 'op_accuracy': 1, 'full_accuracy': 1},
                'base': {'now_count': 197, 'op_accuracy': 138 / 197, 'full_accuracy': 138 / 197},
                'hard': {'now_count': 236, 'op_accuracy': 144 / 236, 'full_accuracy': 143 / 236},
            })
            self.assertEqual([r['tier'] for r in report['results']],
                             [c.get('tier', 'base') for c in ev.load_cases()])
            self.assertIn('by-tier now accuracy:', out.getvalue())
            self.assertIn('base 197 cases op 70.05% full 70.05%', out.getvalue())
            self.assertIn('hard 236 cases op 61.02% full 60.59%', out.getvalue())
            self.assertEqual([r['contract'] for r in report['results']],
                             [c['contract'] for c in ev.load_cases()])
            self.assertIn('all op accuracy', out.getvalue())
            self.assertIn('now op accuracy', out.getvalue())
            self.assertIn('now full accuracy', out.getvalue())
            self.assertIn('451', out.getvalue())
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
