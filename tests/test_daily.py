"""Daily attempts use the existing Berlin 05:00 rollover, including DST."""
from skald.members import rid
from contextlib import ExitStack, redirect_stdout
import datetime
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def at(wall):
    return datetime.datetime.fromisoformat(wall).replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()


class DailyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('daily_server', ROOT / 'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': ''}):
            spec.loader.exec_module(cls.s)
        for name in ('_pool', '_outer', '_nzb_pool'):
            cls.addClassCleanup(getattr(cls.s, name).shutdown)

    def setUp(self):
        timezone = patch.object(self.s, 'TZ', 'Europe/Berlin')
        timezone.start()
        self.addCleanup(timezone.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.s._daily = None
        self.run_job = Mock(return_value={'count': 1})
        self.due = Mock(return_value=True)
        for p in (patch.object(self.s, 'DATA', self.temp.name),
                  patch.object(self.s, 'DAILY_JOBS', [('synthetic', self.due, self.run_job)]),
                  patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))):
            p.start()
            self.addCleanup(p.stop)

    def jobs(self, wall):
        self.s.daily_jobs(at(wall))

    def test_once_per_berlin_day(self):
        for wall, count in [('2026-10-11T06:00', 1), ('2026-10-11T23:30', 1),
                            ('2026-10-12T04:59', 1), ('2026-10-12T05:01', 2)]:
            self.jobs(wall)
            self.assertEqual(self.run_job.call_count, count)

    def test_dst_rollover(self):
        for wall, count in [('2026-10-24T06:00', 1), ('2026-10-25T02:30', 1),
                            ('2026-10-25T04:59', 1), ('2026-10-25T05:01', 2),
                            ('2026-10-25T23:30', 2), ('2026-10-26T04:59', 2),
                            ('2026-10-26T05:01', 3)]:
            self.jobs(wall)
            self.assertEqual(self.run_job.call_count, count)

    def test_due_false_quiet(self):
        self.due.return_value = False
        with redirect_stdout(io.StringIO()) as out:
            self.jobs('2026-10-11T06:00')
        self.assertEqual(out.getvalue(), '')
        self.run_job.assert_not_called()
        self.assertEqual(self.s._daily['synthetic'], {'day': '2026-10-11', 'skipped': True, 'last_ok': None})
        self.jobs('2026-10-11T23:30')
        self.assertEqual(self.due.call_count, 1)

    def test_failure_next_job_last_ok_and_next_day(self):
        self.jobs('2026-10-10T06:00')
        self.run_job.side_effect = RuntimeError('synthetic ' * 50)
        next_job = Mock(return_value={'count': 2})
        with patch.object(self.s, 'DAILY_JOBS', [('synthetic', self.due, self.run_job), ('next', lambda now: True, next_job)]), \
                redirect_stdout(io.StringIO()) as out:
            self.jobs('2026-10-11T06:00')
            rec = self.s._daily['synthetic']
            self.assertFalse(rec['ok'])
            self.assertTrue(rec['err'].startswith('RuntimeError: '))
            self.assertLessEqual(len(rec['err']), 200)
            self.assertEqual(rec['last_ok'], '2026-10-10')
            self.assertEqual(out.getvalue(), 'daily synthetic failed: ' + rec['err'] + '\n')
            self.assertEqual(next_job.call_count, 1)
            self.jobs('2026-10-11T23:30')
            self.assertEqual(self.run_job.call_count, 2)
            self.jobs('2026-10-12T06:00')
            self.assertEqual(self.run_job.call_count, 3)
            self.assertEqual(next_job.call_count, 2)

    def test_raising_due(self):
        self.due.side_effect = LookupError('synthetic')
        with redirect_stdout(io.StringIO()):
            self.jobs('2026-10-11T06:00')
        self.assertFalse(self.s._daily['synthetic']['ok'])
        self.assertEqual(self.s._daily['synthetic']['err'], 'LookupError: synthetic')
        self.run_job.assert_not_called()

    def test_restart_corrupt_and_modes(self):
        self.jobs('2026-10-11T06:00')
        state = self.data / 'daily.json'
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.s._daily = None
        self.jobs('2026-10-11T23:30')
        self.assertEqual(self.run_job.call_count, 1)
        for raw in ('bad json', '[]', 'null', '{"synthetic": 3}'):
            state.write_text(raw)
            self.s._daily = None
            self.jobs('2026-10-12T06:00')
            self.assertTrue(self.s._daily['synthetic']['ok'])
        self.assertFalse(list(self.data.glob('.daily-*.tmp')))

    def test_bad_and_large_output(self):
        for value in ({'n': 'x' * 1024}, {'nested': {}}, [], {'bool': True}, {'bad': object()}):
            self.s._daily = {}
            self.run_job.return_value = value
            self.jobs('2026-10-11T06:00')
            rec = self.s._daily['synthetic']
            self.assertTrue(rec['ok'])
            self.assertEqual(set(rec['out']), {'too_big'})
            self.assertIsInstance(rec['out']['too_big'], int)
            self.assertLessEqual(len(json.dumps(rec['out'])), 1024)

    def test_save_failure_does_not_rerun_or_stop_next_job(self):
        next_job = Mock(return_value={'n': 1})
        with patch.object(self.s, '_daily_save', side_effect=OSError('synthetic save failure')), \
                patch.object(self.s, 'DAILY_JOBS', [('synthetic', self.due, self.run_job), ('next', self.due, next_job)]), \
                redirect_stdout(io.StringIO()):
            self.jobs('2026-10-11T06:00')
            self.jobs('2026-10-11T23:30')
        self.assertEqual(self.run_job.call_count, 1)
        self.assertEqual(next_job.call_count, 1)

    def test_outer_exception_and_atomic_save_cleanup(self):
        with patch.object(self.s, 'online_day', side_effect=RuntimeError('synthetic')), redirect_stdout(io.StringIO()):
            self.jobs('2026-10-11T06:00')
        self.s._daily = {}
        with patch.object(self.s.os, 'replace', side_effect=OSError('synthetic')), redirect_stdout(io.StringIO()):
            self.jobs('2026-10-11T06:00')
        self.assertFalse(list(self.data.glob('.daily-*.tmp')))

        self.s._daily = {}
        next_job = Mock(return_value={'n': 1})
        with patch('builtins.print', side_effect=OSError('synthetic stdout failure')), \
                patch.object(self.run_job, 'side_effect', RuntimeError('synthetic')), \
                patch.object(self.s, 'DAILY_JOBS', [('synthetic', self.due, self.run_job), ('next', self.due, next_job)]):
            self.jobs('2026-10-12T06:00')
        next_job.assert_called_once()
        with patch('builtins.print', side_effect=OSError('synthetic stdout failure')), \
                patch.object(self.s, 'online_day', side_effect=RuntimeError('synthetic')):
            self.jobs('2026-10-12T06:00')

    def test_feed_worker_save_failures_continue_cycle(self):
        s, now = self.s, at('2026-10-11T06:00')
        jobs = [('a', [rid('synthetic1', s.CONFIG)], 'stamp', 0, lambda lang: {rid('synthetic1', s.CONFIG): {}}),
                ('b', [rid('synthetic2', s.CONFIG)], 'stamp', 0, lambda lang: {rid('synthetic2', s.CONFIG): {}})]
        for failed in (None, 'learn', 'nzb', 'emb', 'facets'):
            with self.subTest(failed=failed), ExitStack() as stack, redirect_stdout(io.StringIO()) as out:
                for name, value in (('_langs', {'de-DE'}), ('_feeds', {})):
                    stack.enter_context(patch.object(s, name, value))
                stack.enter_context(patch.object(s, 'refresh_profiles', return_value={}))
                stack.enter_context(patch.object(s, 'job_specs', return_value=jobs))
                stack.enter_context(patch.object(s.time, 'time', return_value=now))
                calls = Mock()
                for name in ('feeds', 'learn', 'nzb', 'emb', 'facets'):
                    save = stack.enter_context(patch.object(s, 'save_' + name))
                    if failed and name == 'feeds':
                        save.side_effect = [OSError('synthetic disk full'), None]
                    elif name == failed:
                        save.side_effect = ValueError('synthetic bad record\n' + 'x' * 300)
                    calls.attach_mock(save, name)
                calls.attach_mock(stack.enter_context(patch.object(s, 'daily_jobs')), 'daily')
                calls.attach_mock(stack.enter_context(patch.object(s.time, 'sleep', side_effect=RuntimeError('cycle complete'))), 'sleep')
                with self.assertRaisesRegex(RuntimeError, '^cycle complete$'):
                    s.feed_worker()
                self.assertEqual(calls.mock_calls, [call.feeds(), call.feeds(), call.learn(), call.nzb(),
                                                   call.emb(), call.facets(), call.daily(now), call.sleep(60)])
                errors = [line for line in out.getvalue().splitlines() if line.startswith('save ')]
                expected = [] if failed is None else ['save feeds failed: OSError: synthetic disk full',
                                                     f'save {failed} failed: ValueError: synthetic bad record ' + 'x' * 300]
                self.assertEqual(errors, [line[:240] for line in expected])

    def test_hook_and_stats_copy(self):
        source = inspect.getsource(self.s.feed_worker)
        self.assertIn('        daily_jobs(time.time())\n        time.sleep(60)', source)
        self.assertEqual(source.count('daily_jobs(time.time())'), 1)
        self.jobs('2026-10-11T06:00')
        out = self.s.stats()
        self.assertIn('daily', out)
        json.dumps(out)
        out['daily']['synthetic']['out']['count'] = 999
        self.assertEqual(self.s._daily['synthetic']['out']['count'], 1)
        self.assertEqual(out['daily']['backups'], {'count': 0, 'newest': None, 'bytes': 0})


if __name__ == '__main__':
    unittest.main()
