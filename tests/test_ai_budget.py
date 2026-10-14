"""AI credit and failures use synthetic numbers and fake transports only."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from decide_kit import ROOT, close_server, load_server
from skald import backup, commands, members, migrate, setup
from skald.__main__ import main


class AIBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.config = members.fresh_config(['ana', 'ben'])
        (self.data / 'skald.json').write_text(json.dumps(self.config))
        self.s.CONFIG = copy.deepcopy(self.config)
        self.now = 1791979200
        self.key = 'synthetic-budget-key'
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'OPENROUTER_API_KEY': self.key,
                                          'SKALD_SNAPSHOT': '', 'TMDB_API': 'offline'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def response(self, remaining=3.2, limit=20, reset='monthly', **extra):
        return {'data': {'limit': limit, 'limit_remaining': remaining, 'usage': 16.8,
                         'usage_daily': .4, 'limit_reset': reset, 'label': self.key, **extra}}

    def check(self, remaining=3.2, limit=20, reset='monthly', refresh=True):
        transport = Mock(side_effect=lambda req, **kw: io.BytesIO(json.dumps(self.response(remaining, limit, reset)).encode()))
        with patch.object(self.s, '_connector_open', transport), patch.object(self.s.time, 'time', return_value=self.now):
            self.s.check_ai_credit(self.now, refresh=refresh)
        return transport

    def test_daily_check_survives_restart_and_refresh_bypasses_it(self):
        transport = self.check(refresh=False)
        req = transport.call_args.args[0]
        self.assertEqual(req.full_url, 'https://openrouter.ai/api/v1/key')
        self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(req.get_header('Authorization'), 'Bearer ' + self.key)
        self.s._usage.clear()
        self.s._usage.update(day='', month='', calls={}, cost=0, month_cost=0, tokens=0, month_calls=0)
        self.s.load_usage()
        with patch.object(self.s, '_connector_open', transport):
            self.s.check_ai_credit(self.now + 60)
            self.assertEqual(transport.call_count, 1)
            self.s.check_ai_credit(self.now + 120, refresh=True)
            self.assertEqual(transport.call_count, 2)
            self.s.check_ai_credit(self.now + 86400)
            self.assertEqual(transport.call_count, 3)

    def test_worker_job_and_error_do_not_stop_other_jobs(self):
        job = next(job for job in self.s.DAILY_JOBS if job[0] == 'ai_credit')
        after = Mock(return_value={'done': 1})
        error = urllib.error.HTTPError('https://example.invalid', 503, self.key, {}, io.BytesIO(self.key.encode()))
        with patch.object(self.s, 'DAILY_JOBS', [job, ('after', lambda now: True, after)]), \
                patch.object(self.s, '_connector_open', side_effect=error) as transport, redirect_output() as out:
            self.s.daily_jobs(self.now)
            self.s._daily = None
            self.s.load_usage()
            self.s.daily_jobs(self.now + 60)
        after.assert_called_once()
        transport.assert_called_once()
        self.assertNotIn(self.key, out.getvalue())
        self.assertEqual(self.s.ai_status()['last_failure']['reason'], 'provider_error')

    def test_offline_json_includes_counters_and_does_not_write(self):
        self.check()
        self.s._usage.update(day=self.s.local_now().strftime('%Y-%m-%d'), cost=.4,
                             month=self.s.local_now().strftime('%Y-%m'), month_cost=1.2,
                             calls={'search:synthetic': 7, 'embed:synthetic': 2})
        self.s.save_usage()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        out = io.StringIO()
        self.assertEqual(main(['ai', 'status', '--json'], out=out), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['provider']['limit_remaining'], 3.2)
        self.assertEqual(result['provider']['limit_reset'], 'monthly')
        self.assertEqual((result['spend_today'], result['spend_month']), (.4, 1.2))
        self.assertEqual(result['daily_caps']['search'], {'used': 7, 'cap': self.s.AI_CAPS['search']})
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)
        self.assertNotIn(self.key, out.getvalue())
        self.assertNotIn(self.key, repr(before))

    def test_cli_refresh_makes_one_fake_request(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=lambda req, **kw: io.BytesIO(json.dumps(self.response()).encode())) as transport:
            out = io.StringIO()
            self.assertEqual(main(['ai', 'status', '--refresh', '--json'], out=out), 0)
        transport.assert_called_once()
        self.assertEqual(json.loads(out.getvalue())['provider']['limit'], 20)
        self.assertNotIn(self.key, out.getvalue())

    def test_low_alarm_once_per_day_across_restarts(self):
        with redirect_output() as out:
            self.check()
            self.s.load_usage()
            self.check()
        self.assertEqual(out.getvalue().count('WARNING:'), 1)
        self.assertEqual(self.s.ai_status()['alarm'], 'low_credit')
        self.now += 86400
        with redirect_output() as out:
            self.check()
        self.assertEqual(out.getvalue().count('WARNING:'), 1)

    def test_threshold_strict_or_and_configurable(self):
        for limit, remaining, alarm in ((20, 4, None), (20, 3.99, 'low_credit'),
                                         (2, 1, None), (2, .99, 'low_credit')):
            with self.subTest(limit=limit, remaining=remaining), redirect_output() as out:
                self.s.save_usage({'ai_warning_day': None}, counters=False)
                self.check(remaining, limit)
                self.assertEqual(self.s.ai_status()['alarm'], alarm)
                self.assertEqual(bool(out.getvalue()), alarm is not None)
        self.s.CONFIG['ai'] = {'credit_warn_fraction': .1, 'credit_warn_usd': .5}
        with redirect_output() as out:
            self.check(3.2)
        self.assertEqual(self.s.ai_status()['alarm'], None)
        self.assertEqual(out.getvalue(), '')

    def test_unlimited_and_zero_limit(self):
        from skald.ai import text
        with redirect_output() as out:
            self.check(None, None, None)
        self.assertIn('no limit set', text(self.s.ai_status()))
        self.assertEqual(out.getvalue(), '')
        self.check(0, 0)
        self.assertEqual(self.s.ai_status()['alarm'], 'out_of_credit')

    def test_failures_by_status_or_type_never_message(self):
        errors = [(urllib.error.HTTPError('https://example.invalid', code, self.key, {}, None), reason)
                  for code, reason in ((402, 'out_of_credit'), (403, 'forbidden'), (429, 'rate_limited'),
                                       (500, 'provider_error'), (503, 'provider_error'))]
        errors += [(TimeoutError(self.key), 'timeout'),
                   (urllib.error.URLError(TimeoutError(self.key)), 'timeout')]
        for error, reason in errors:
            with self.subTest(reason=reason), patch.object(self.s, '_connector_open', side_effect=error):
                with self.assertRaises(type(error)):
                    self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'synthetic')
                self.assertEqual(self.s.ai_status()['last_failure']['reason'], reason)
                stored = (self.data / 'usage.json').read_text()
                self.assertNotIn(self.key, stored)
        with patch.dict(self.s.AI_CAPS, {'search': 0}), self.assertRaises(self.s.BudgetExceeded):
            self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'synthetic')
        self.assertEqual(self.s.ai_status()['last_failure']['reason'], 'skald_cap')

    def test_decision_cap_records_failure(self):
        with patch.object(self.s, 'OR_KEY', 'synthetic'), patch.object(self.s, 'AI_MONTHLY_USD', 0):
            self.s.decide('parse_command', {'query': 'How much AI credit is left?'})
        self.assertEqual(self.s.ai_status()['last_failure']['reason'], 'skald_cap')

    def test_provider_failure_logs_once_and_raises_no_private_text(self):
        error = urllib.error.HTTPError('https://example.invalid', 402, self.key, {}, None)
        with patch.object(self.s, '_connector_open', side_effect=error), redirect_output() as out:
            for _ in range(2):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'synthetic')
                self.assertNotIn(self.key, str(raised.exception))
        self.assertEqual(out.getvalue().count('WARNING:'), 1)
        self.assertIn('out of credit', out.getvalue())

    def test_completed_call_saves_reported_spend(self):
        reply = {'usage': {'cost': .25, 'total_tokens': 4}}
        with patch.object(self.s, '_connector_open', return_value=io.BytesIO(json.dumps(reply).encode())), \
                patch.object(self.s, 'save_usage', wraps=self.s.save_usage) as saved:
            self.s.openrouter('/api/v1/embeddings', {}, 1, 'embed', 'synthetic')
        self.assertEqual(saved.call_count, 2)  # One reservation and one completion save.
        stored = json.loads((self.data / 'usage.json').read_text())
        self.assertEqual((stored['cost'], stored['month_cost']), (.25, .25))

    def test_transport_exceptions_are_fresh_and_have_no_private_attributes(self):
        malformed = urllib.error.HTTPError(self.key, self.key, self.key, {'key': self.key}, None)
        errors = [OSError(5, self.key, self.key, None, self.key), TimeoutError(5, self.key, self.key),
                  ValueError(self.key), urllib.error.URLError(self.key),
                  urllib.error.HTTPError(self.key, 503, self.key, {'key': self.key}, io.BytesIO(self.key.encode())), malformed,
                  self.s.UnsafeURL(self.key), self.s.ByteCapExceeded(self.key)]
        for error in errors:
            error.private = self.key
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.s, '_connector_open', side_effect=error), redirect_output() as logs:
                with self.assertRaises(Exception) as raised:
                    self.s.openrouter('/api/v1/embeddings', {}, 1, 'embed', 'synthetic')
                self.assertIsNot(raised.exception, error)
                self.assertNotIn(self.key, str(raised.exception))
                self.assertNotIn(self.key, repr(raised.exception))
                self.assertNotIn(self.key, repr(vars(raised.exception)))
                if isinstance(raised.exception, urllib.error.HTTPError):
                    self.assertIn(raised.exception.code, (502, 503))
                if isinstance(error, (self.s.UnsafeURL, self.s.ByteCapExceeded)):
                    self.assertIsInstance(raised.exception, type(error))
                out = io.StringIO()
                self.assertEqual(main(['ai', 'status', '--json'], out=out), 0)
                self.assertNotIn(self.key, out.getvalue())
                self.assertNotIn(self.key, logs.getvalue())
                self.assertNotIn(self.key, (self.data / 'usage.json').read_text())

    def test_cli_refresh_merges_with_calls_in_another_process(self):
        self.check(10)
        started, resume = self.data / 'refresh-started', self.data / 'refresh-resume'
        script = '''
import io, json, os, sys, time
from pathlib import Path
from unittest.mock import patch
from skald.__main__ import main
settings = json.load(sys.stdin)
def request(req, **kw):
    Path(settings['started']).touch()
    deadline = time.monotonic() + 10
    while not Path(settings['resume']).exists():
        if time.monotonic() > deadline:
            raise AssertionError('fake refresh timed out')
        time.sleep(.01)
    return io.BytesIO(json.dumps(settings['response']).encode())
with patch('urllib.request.OpenerDirector.open', side_effect=request):
    raise SystemExit(main(['ai', 'status', '--refresh', '--json']))
'''
        with subprocess.Popen([sys.executable, '-c', script], cwd=ROOT, env=os.environ.copy(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as cli:
            cli.stdin.write(json.dumps({'started': str(started), 'resume': str(resume), 'response': self.response(18)}))
            cli.stdin.close()
            cli.stdin = None
            deadline = time.monotonic() + 10
            while not started.exists() and cli.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(started.exists(), 'CLI did not reach the fake credit request')
            reply = {'usage': {'cost': .25, 'total_tokens': 4}}
            with patch.object(self.s, '_connector_open', side_effect=lambda *a, **kw: io.BytesIO(json.dumps(reply).encode())):
                self.s.openrouter('/api/v1/embeddings', {}, 1, 'embed', 'synthetic')
                resume.touch()
                stdout, stderr = cli.communicate(timeout=10)
                self.assertEqual(cli.returncode, 0, stderr)
                result = json.loads(stdout)
                self.assertEqual((result['spend_today'], result['spend_month']), (.25, .25))
                self.assertEqual(self.s.ai_status()['provider']['limit_remaining'], 18)
                self.s.openrouter('/api/v1/embeddings', {}, 1, 'embed', 'synthetic')
        stored = json.loads((self.data / 'usage.json').read_text())
        self.assertEqual(stored['ai_credit']['limit_remaining'], 18)
        self.assertEqual((stored['cost'], stored['month_cost'], stored['tokens'], stored['month_calls']), (.5, .5, 8, 2))
        self.assertEqual(stored['calls']['embed:synthetic'], 2)
        self.assertFalse((self.data / 'usage.json.tmp').exists())

    def test_exhaustion_survives_other_failures_and_restart_until_new_check(self):
        from skald.ai import failure, text
        self.check(10)
        failure(self.s, urllib.error.HTTPError('https://example.invalid', 402, self.key, {}, None), self.now + 10)
        for error, reason in ((urllib.error.HTTPError('https://example.invalid', 503, self.key, {}, None), 'provider_error'),
                              (self.s.BudgetExceeded(self.key), 'skald_cap')):
            with self.subTest(reason=reason):
                failure(self.s, error, self.now + 20)
                self.s.load_usage()
                result = self.s.ai_status()
                self.assertEqual(result['last_failure']['reason'], reason)
                self.assertEqual(result['alarm'], 'out_of_credit')
                self.assertIn('out of credit', text(result))
                self.assertIn('out of credit', setup.status(self.data)['ai_credit'])
        self.check(10)  # An older check cannot clear a newer exhaustion event.
        self.assertEqual(self.s.ai_status()['alarm'], 'out_of_credit')
        self.now += 30
        self.check(10)
        self.assertIsNone(self.s.ai_status()['alarm'])

    def test_check_marker_is_durable_before_request_and_survives_crash(self):
        def crash(req, **kw):
            stored = json.loads((self.data / 'usage.json').read_text())
            self.assertEqual(stored['ai_check_at'], self.now)
            self.assertTrue(synced.called)
            raise SystemExit('synthetic crash')
        with patch('skald.intake.os.fsync', wraps=os.fsync) as synced, \
                patch.object(self.s, '_connector_open', side_effect=crash) as transport:
            with self.assertRaises(SystemExit):
                self.s.check_ai_credit(self.now)
            self.s._usage.pop('ai_check_at', None)
            self.s.load_usage()
            self.s.check_ai_credit(self.now + 60)
        transport.assert_called_once()

    def test_failed_check_marker_save_skips_transport_and_logs_once(self):
        with patch('skald.intake.write_private', side_effect=OSError(5, self.key)), \
                patch.object(self.s, '_connector_open') as transport, redirect_output() as logs:
            self.s.check_ai_credit(self.now)
            self.s.check_ai_credit(self.now + 60)
        transport.assert_not_called()
        self.assertEqual(logs.getvalue().count('usage save failed'), 1)
        self.assertNotIn(self.key, logs.getvalue())

    def test_worker_backup_restores_custom_credit_thresholds(self):
        config = {**self.config, 'ai': {'credit_warn_fraction': .1, 'credit_warn_usd': .5}}
        members.validate_config(config)
        (self.data / 'skald.json').write_text(json.dumps(config))
        self.s.backup_state(self.now)
        archive = self.data / 'backups' / f'state-{self.s.online_day(self.now)[0]}.tar.gz'
        with tempfile.TemporaryDirectory() as target:
            self.assertTrue(backup.restore(archive, target)['ok'])
            restored, _ = members.load_config(target, {}, readonly=True)
        self.assertEqual(restored.get('ai'), config['ai'])

    def test_check_error_keeps_snapshot_and_explains_exhaustion(self):
        from skald.ai import text
        self.check(10)
        self.now += 10
        error = urllib.error.HTTPError('https://example.invalid', 402, self.key, {}, None)
        with patch.object(self.s, '_connector_open', side_effect=error), redirect_output() as out:
            self.s.check_ai_credit(self.now, refresh=True)
        result = self.s.ai_status()
        self.assertEqual(result['provider']['limit_remaining'], 10)
        self.assertEqual(result['alarm'], 'out_of_credit')
        self.assertIn('out of credit', text(result))
        self.assertNotIn(self.key, out.getvalue())
        self.check(10)
        self.assertIsNone(self.s.ai_status()['alarm'])

    def test_provider_payload_only_stores_numbers(self):
        self.check()
        stored = self.s._usage['ai_credit']
        self.assertEqual(set(stored), {'limit', 'limit_remaining', 'usage', 'usage_daily', 'reset_days', 'checked_at'})
        self.assertTrue(all(type(v) in (int, float) or v is None for v in stored.values()))
        for payload in ({'data': {'limit': self.key}}, self.response(extra=self.key, limit=True),
                        self.response(limit=float('nan')), self.response(reset=self.key)):
            with patch.object(self.s, '_connector_open', return_value=io.BytesIO(json.dumps(payload).encode())):
                self.s.check_ai_credit(self.now + 1, refresh=True)
            self.assertEqual(self.s._usage['ai_credit'], stored)
            self.assertNotIn(self.key, (self.data / 'usage.json').read_text())

    def test_setup_and_commander_use_stored_numbers_in_four_languages(self):
        self.check()
        result = setup.status(self.data)
        self.assertIn('$3.20', result['ai_credit'])
        self.assertIn('low', result['ai_credit'].lower())
        for language, word in (('en', 'AI credit'), ('de', 'KI-Guthaben'),
                                ('sr-latn', 'AI kredit'), ('sr-cyrl', 'АИ кредит')):
            with self.subTest(language=language), patch.object(self.s, 'decide', return_value={
                    'answer': {'op': 'ai_status', 'kind': None, 'values': [], 'person': None, 'language': language}}):
                plan = commands.preview(self.s, 'How much AI credit is left?', 'ana')
            self.assertTrue(plan['read_only'])
            self.assertIn('$3.20', plan['description'])
            self.assertIn(word, plan['name'])
            self.assertEqual(plan['ai']['provider']['limit'], 20)

    def test_search_routes_credit_to_commander_and_returns_saved_card(self):
        self.check()
        self.assertIn('AI credit', self.s.ROUTE_Q['route']['criteria']['command'])
        answer = {'op': 'ai_status', 'kind': None, 'values': [], 'person': None, 'language': 'en'}
        reply = {'choices': [{'message': {'content': json.dumps(answer)}}]}
        with patch.object(self.s, '_connector_open', return_value=io.BytesIO(json.dumps(reply).encode())), \
                patch.object(self.s, 'OR_KEY', 'synthetic'), patch.object(commands, 'context', return_value=('synthetic', 'ana', 'ana')):
            result = commands.special(self.s, 'How much AI credit is left?', 'movie', routed=True)
        self.assertIn('$3.20', result['metas'][0]['name'])

    def test_config_rejects_invalid_thresholds(self):
        for prefs in ([], {'credit_warn_usd': -1}, {'credit_warn_usd': True},
                      {'credit_warn_fraction': 2}, {'credit_warn_fraction': float('nan')}, {'unknown': 1}):
            with self.subTest(prefs=prefs), self.assertRaises(members.ConfigError):
                members.validate_config({**self.config, 'ai': prefs})
        members.validate_config({**self.config, 'ai': {'credit_warn_fraction': 0, 'credit_warn_usd': 0}})

    def test_format_five_optional_fields_load_in_previous_server_without_migration(self):
        self.assertEqual(migrate.FORMAT, 5)
        self.assertEqual(max(migrate.MIGRATIONS), 5)
        self.check(10)
        self.s._usage.update(day=self.s.local_now().strftime('%Y-%m-%d'), cost=.4,
                             month=self.s.local_now().strftime('%Y-%m'), month_cost=1.2,
                             calls={'search:synthetic': 7})
        from skald.ai import failure
        failure(self.s, urllib.error.HTTPError('https://example.invalid', 402, self.key, {}, None), self.now + 10)
        marker = self.data / '.skald-format'
        marker.write_text(json.dumps({'format': 5, 'release': members.VERSION}))
        before = {name: (self.data / name).read_bytes() for name in ('usage.json', 'skald.json', '.skald-format')}
        source = subprocess.run(['git', 'show', '5fc2a33:server.py'], cwd=ROOT, check=True, capture_output=True, text=True).stdout
        spec = importlib.util.spec_from_file_location('budget_previous_server', ROOT / 'server.py')
        old = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': '', 'API_PORT': ''}), redirect_output():
            exec(compile(source, str(ROOT / 'server.py'), 'exec'), vars(old))
            self.addCleanup(close_server, old)
            old.load_state()
        self.assertFalse(hasattr(old, 'ai_status'))
        stored = json.loads(before['usage.json'])
        self.assertEqual(old._usage, stored)
        self.assertEqual({name: (self.data / name).read_bytes() for name in before}, before)
        self.assertFalse(migrate.rewrite_needed(self.data))


def redirect_output():
    from contextlib import redirect_stdout
    return redirect_stdout(io.StringIO())
