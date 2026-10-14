"""Bounded engine regressions using synthetic data and mocked transports."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server


class EngineFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)

    def test_digital_and_toprated_dedupe_and_refill(self):
        s = self.s
        cards = [dict(id=f'tmdb:{n}', type='movie', name=f'Synthetic {n} Card') for n in range(40)]
        above = s.mark_above(set(), s.rid('top10'), cards[:10])
        above = s.mark_above(above, s.rid('foryou'), cards[10:30])
        with patch.object(s, 'row_list', return_value=cards), \
                patch.object(s, 'quality_ok', return_value=True), \
                patch.object(s, 'preference_ok', return_value=True), \
                patch.object(s, 'preference_order', side_effect=lambda xs, *a: xs):
            for row in ('digital', 'toprated'):
                with self.subTest(row=row):
                    result = s.served_row(s.rid(row), 'movie', s.LANG, {}, above, (set(), set()))
                    self.assertEqual(result, cards[30:])
                    result = s.served_row(s.rid(row), 'movie', s.LANG, {}, {x['id'] for x in cards[:10]},
                                          ({cards[10]['id']}, set(), {cards[11]['id']}))
                    self.assertEqual(result, cards[12:32])
                    result = s.served_row(s.rid(row), 'movie', s.LANG, {}, {x['id'] for x in cards[:-5]},
                                          (set(), set()))
                    self.assertEqual(result, [])

    def test_due_history_read_between_feed_jobs(self):
        s = self.s
        read = Mock(return_value=([], 0, {'events': 0, 'error': None}))
        def first(lang):
            read.reset_mock()
            (self.data / 'history.json').write_text(json.dumps({'items': []}))
            s.connectors_load(1000)
            return {}
        def last(lang):
            read.assert_called_once()
            return {}
        jobs = [('first', [s.rid('top10')], 'today', 0, first),
                ('last', [s.rid('digital')], 'today', 0, last)]
        with ExitStack() as stack:
            for name, value in {'refresh_profiles': {}, 'job_specs': jobs}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            for name in ('save_feeds', 'pin_decision', '_daily_log'):
                stack.enter_context(patch.object(s, name))
            stack.enter_context(patch.object(s, '_langs', {s.LANG}))
            stack.enter_context(patch.dict(s.CONNECTOR_POLL, {'history': read}))
            stack.enter_context(patch.object(s.time, 'time', return_value=1000))
            stack.enter_context(redirect_stdout(io.StringIO()))
            s.feed_pass(s.local_now(), {})
        read.assert_called_once()
        self.assertIn((s.rid('digital'), s.LANG), s._feeds)

    def test_empty_data_waits_with_unhealthy_health_then_loads_config(self):
        s = self.s
        out = io.StringIO()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'PORT': '8080'}, clear=True), \
                patch.object(s, 'ThreadingHTTPServer') as server, \
                patch.object(s.time, 'sleep') as sleep, redirect_stderr(out):
            def ready(seconds):
                self.assertEqual(seconds, 3)
                if sleep.call_count == 2:
                    (self.data / 'skald.json').write_text(json.dumps(s.fresh_config(['eve', 'fin'])))
            sleep.side_effect = ready
            cfg, source = s.wait_for_config(str(self.data))
            self.assertEqual((cfg['household']['members'], source), (['eve', 'fin'], 'file'))
            handler_class = server.call_args.args[1]
            for path, status in (('/health', 503), ('/en-US/manifest.json', 503)):
                handler = handler_class.__new__(handler_class)
                handler.path = path
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = io.BytesIO()
                handler.do_GET()
                handler.send_response.assert_called_once_with(status)
                self.assertEqual(json.loads(handler.wfile.getvalue()), {'ok': False, 'status': 'not-configured'})
            server.return_value.__enter__.return_value.shutdown.assert_called_once()
        self.assertEqual(out.getvalue().splitlines(), ['empty DATA: set SKALD_MEMBERS=ana,ben or provide skald.json'])

    def test_wait_keeps_real_config_errors(self):
        s = self.s
        with patch.dict(os.environ, {}, clear=True), patch.object(s, 'ThreadingHTTPServer') as server:
            (self.data / 'skald.json').write_text('{')
            with self.assertRaises(s.ConfigError):
                s.wait_for_config(str(self.data))
            server.assert_not_called()

    def test_wait_survives_real_setup_marker_before_config_publication(self):
        from skald import setup
        s = self.s
        for already_publishing in (False, True):
            with self.subTest(already_publishing=already_publishing), tempfile.TemporaryDirectory() as folder:
                data = Path(folder)
                marker_ready, release, published = threading.Event(), threading.Event(), threading.Event()
                errors = []
                link = os.link
                def delayed_link(source, target):
                    if Path(target) == data / 'skald.json':
                        self.assertTrue((data / '.skald-household').is_file())
                        marker_ready.set()
                        if not release.wait(2):
                            raise AssertionError('publication was not released')
                    return link(source, target)
                def publish():
                    try:
                        setup.publish(data, s.fresh_config(['eve', 'fin']))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        published.set()
                publisher = threading.Thread(target=publish)
                with patch.dict(os.environ, {'DATA_DIR': folder}, clear=True), \
                        patch('os.link', side_effect=delayed_link), \
                        patch.object(s, 'ThreadingHTTPServer') as server, \
                        patch.object(s.time, 'sleep') as sleep, redirect_stderr(io.StringIO()):
                    def ready(seconds):
                        self.assertEqual(seconds, 3)
                        if sleep.call_count == 1:
                            if not already_publishing:
                                publisher.start()
                            self.assertTrue(marker_ready.wait(1))
                            self.assertFalse((data / 'skald.json').exists())
                            server.return_value.__enter__.return_value.shutdown.assert_not_called()
                            handler = server.call_args.args[1].__new__(server.call_args.args[1])
                            handler.path = '/health'
                            handler.send_response = Mock()
                            handler.send_header = Mock()
                            handler.end_headers = Mock()
                            handler.wfile = io.BytesIO()
                            handler.do_GET()
                            handler.send_response.assert_called_once_with(503)
                        elif sleep.call_count == 2:
                            release.set()
                            self.assertTrue(published.wait(1))
                        else:
                            self.fail('config publication did not finish startup')
                    sleep.side_effect = ready
                    try:
                        if already_publishing:
                            publisher.start()
                            self.assertTrue(marker_ready.wait(1))
                        cfg, source = s.wait_for_config(folder)
                    finally:
                        release.set()
                        if publisher.ident is not None:
                            publisher.join(2)
                    self.assertEqual(errors, [])
                    self.assertEqual((cfg['household']['members'], source), (['eve', 'fin'], 'file'))
                    self.assertEqual(sleep.call_count, 2)
                    server.return_value.__enter__.return_value.shutdown.assert_called_once()

    def test_invalid_config_published_during_wait_still_fails(self):
        s = self.s
        with patch.dict(os.environ, {}, clear=True), patch.object(s, 'ThreadingHTTPServer') as server, \
                patch.object(s.time, 'sleep') as sleep, redirect_stderr(io.StringIO()):
            def ready(seconds):
                s.stamp_household(str(self.data))
                if sleep.call_count == 2:
                    (self.data / 'skald.json').write_text('{')
            sleep.side_effect = ready
            with self.assertRaisesRegex(s.ConfigError, 'invalid config: skald.json'):
                s.wait_for_config(str(self.data))
            self.assertEqual(sleep.call_count, 2)
            server.return_value.__enter__.return_value.shutdown.assert_called_once()

    def test_server_startup_waits_then_reaches_normal_api_validation(self):
        from skald import members, migrate
        # This startup test disables threads; migrations have their own backup tests.
        (self.data / '.skald-format').write_text(json.dumps({'format': migrate.FORMAT, 'release': members.VERSION}))
        out = io.StringIO()
        def ready(seconds):
            (self.data / 'skald.json').write_text(json.dumps(self.s.fresh_config(['eve', 'fin'])))
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True), \
                patch.object(sys, 'argv', ['server.py']), \
                patch('_thread.start_new_thread'), \
                patch('http.server.ThreadingHTTPServer') as health, \
                patch('time.sleep', side_effect=ready) as sleep, \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                redirect_stderr(out), self.assertRaises(SystemExit) as exit:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'server.py'), run_name='__main__')
        self.assertEqual(exit.exception.code, 2)
        sleep.assert_called_once_with(3)
        health.assert_called_once()
        self.assertEqual(out.getvalue().splitlines(), [
            'empty DATA: set SKALD_MEMBERS=ana,ben or provide skald.json', 'missing TMDB_API'])


if __name__ == '__main__':
    unittest.main()
