"""Shelf manifest refreshes use synthetic DATA and a fake transport only."""
import base64
from contextlib import redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import commands, members, setup
from skald.__main__ import main


class FakeAIO:
    def __init__(self):
        self.calls = []
        self.configs = {'synthetic-uuid-ana': {'addons': [{'url': 'https://synthetic.invalid/manifest.json'}],
                        'nested': {'keep': [1, None, False]}}, 'synthetic-uuid-sam': {'other': 'unchanged'}}
        self.failure = None

    def __call__(self, req, timeout):
        assert timeout == 20
        assert req.full_url == 'https://aio.invalid/api/v1/user'
        uid = base64.b64decode(req.get_header('Authorization').split()[1]).decode().split(':')[0]
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), uid, body))
        if req.get_method() == 'PUT' and self.failure:
            status, code = self.failure
            raise urllib.error.HTTPError(req.full_url, status, 'failed', {}, io.BytesIO(json.dumps(
                {'success': False, 'error': {'code': code, 'message': 'synthetic-uuid-ana synthetic-password'}}).encode()))
        return io.BytesIO(json.dumps({'success': True, 'data': {'userData': self.configs[uid]}}).encode())


class AIORefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_CONFIG': ''})
        env.start()
        self.addCleanup(env.stop)
        setup.publish(self.data, members.fresh_config(['ana', 'sam']))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        (self.data / 'setup.json').write_text(json.dumps({'aio_url': 'https://aio.invalid'}))
        (self.data / 'credentials').mkdir()
        (self.data / 'credentials/aiostreams.json').write_text(json.dumps({p:
            {'uuid': 'synthetic-uuid-' + p, 'password': 'synthetic-password', 'member': p} for p in ('ana', 'sam')}))
        self.fake = FakeAIO()
        self.lines = []

    def background(self):
        from skald.aio_refresh import BackgroundRefresh
        self.s._aio_refresh = BackgroundRefresh(self.data, self.lines.append, transport=self.fake)
        self.addCleanup(self.join)
        return self.s._aio_refresh

    def join(self):
        thread = self.s._aio_refresh.thread
        if thread:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def add(self):
        return self.s.shelf_change('ana', 'add', text='genre:drama', name='Drama', apply=True)

    def search_apply(self):
        with self.s.addon_scope('ana'):
            self.s.search('movie', 'make me a row of heist films', 'en-US')
            plan = commands.get_pending(self.s.command_context(), 'ana')
            self.assertIsNotNone(plan)
            return self.s.search('movie', 'yes ' + commands.confirm_code(plan), 'en-US')

    def test_search_add_get_put_unchanged_per_login(self):
        self.background()
        with patch('skald.aio_refresh.time.sleep'):
            self.assertIn('Done:', self.search_apply()['metas'][0]['name'])
            self.join()
        self.assertEqual([c[0] for c in self.fake.calls], ['GET', 'PUT', 'GET', 'PUT'])
        for method, uid, body in self.fake.calls:
            if method == 'PUT':
                self.assertEqual(body, {'config': self.fake.configs[uid]})
        self.assertEqual(self.lines, ['AIOStreams rows refreshed (2 logins)'])

    def test_burst_coalesces_and_changes_during_run_queue_once(self):
        bg = self.background()
        waiting, release, fetching, finish = [threading.Event() for _ in range(4)]
        def sleep(seconds):
            self.assertEqual(seconds, 45)
            waiting.set()
            self.assertTrue(release.wait(3))
        def transport(req, timeout):
            if not fetching.is_set():
                fetching.set()
                self.assertTrue(finish.wait(3))
            return self.fake(req, timeout)
        bg.transport = transport
        with patch('skald.aio_refresh.time.sleep', side_effect=sleep):
            try:
                self.add()
                self.assertTrue(waiting.wait(3))
                self.s.shelf_change('ana', 'replace', slot=1, text='genre:comedy', apply=True)
                self.s.shelf_change('ana', 'count', count=6, apply=True)
                self.assertFalse(self.fake.calls)
                self.assertTrue(bg.thread.daemon)
                release.set()
                self.assertTrue(fetching.wait(3))
                self.s.shelf_change('ana', 'stop', slot=1, apply=True)
                self.s.shelf_change('ana', 'undo', apply=True)
            finally:
                release.set()
                finish.set()
            self.join()
        self.assertEqual(len(self.fake.calls), 8)
        self.assertEqual(len(self.lines), 2)

    def test_no_setup_url_or_logins_sends_nothing(self):
        from skald.aio_refresh import refresh
        for name, value in [('setup.json', None), ('setup.json', {}), ('setup.json', {'aio_url': 'https://aio.invalid'}),
                            ('credentials/aiostreams.json', {}), ('credentials/aiostreams.json', None)]:
            path = self.data / name
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(value))
            if name.startswith('credentials') or not value or 'aio_url' not in value:
                self.assertIsNone(refresh(self.data, transport=self.fake))
        self.assertFalse(self.fake.calls)

    def test_successful_refresh_preserves_setup_changes_during_request(self):
        from skald.aio_refresh import refresh
        def transport(req, timeout):
            if req.get_method() == 'PUT':
                saved = setup.state(self.data)
                saved.update(aio_url='https://new-aio.invalid', base='https://new-skald.invalid',
                             taste='files', aiostreams_pending=True, aiostreams_hint='old hint')
                setup.write_private(self.data / 'setup.json', json.dumps(saved))
            return self.fake(req, timeout)
        self.assertIn('refreshed', refresh(self.data, transport=transport))
        saved = setup.state(self.data)
        self.assertEqual(saved['aio_url'], 'https://new-aio.invalid')
        self.assertEqual(saved['base'], 'https://new-skald.invalid')
        self.assertEqual(saved['taste'], 'files')
        self.assertFalse(saved['aiostreams_pending'])
        self.assertNotIn('aiostreams_hint', saved)

    def test_failed_put_keeps_change_and_redacts_log(self):
        self.background()
        for failure, hint in [((500, None), 'rejected setup'), ((401, 'ADDON_PASSWORD_INVALID'), 'instance access key')]:
            with self.subTest(failure=failure), patch('skald.aio_refresh.time.sleep'):
                self.fake.failure = failure
                self.fake.calls.clear()
                self.s.shelf_change('ana', 'add' if not self.s.shelf_list('ana')['wishes'] else 'replace',
                                    slot=1, text='genre:drama', name='Kept', apply=True)
                self.join()
                self.assertEqual(self.s.shelf_list('ana')['wishes'][0]['name'], 'Kept')
                self.assertEqual(len(self.fake.calls), 2)
                self.assertIn(hint, self.lines[-1])
                saved = setup.state(self.data)
                self.assertTrue(saved['aiostreams_pending'])
                self.assertEqual(saved['aiostreams_hint'], self.lines[-1])
                status = setup.status(self.data)
                self.assertIn('aiostreams', status['missing'])
                self.assertIn(self.lines[-1], status['notes'])
                for secret in ('synthetic-uuid', 'synthetic-password', 'Authorization', '/api/', 'addons', 'https://'):
                    self.assertNotIn(secret, self.lines[-1])

    def test_refresh_adds_and_removes_catalogs_using_setup_order(self):
        from skald.aio_refresh import refresh
        from test_setup_aiostreams import FakeAIO as CatalogAIO
        aio = CatalogAIO()
        cid = 'skd1a2b.' + self.s.rid('shelf_ana_1')
        other = {'id': 'other.keep', 'type': 'movie', 'enabled': False}
        for login in setup.aiostreams_logins(self.data).values():
            aio.configs[login['uuid']] = {'presets': [{'type': 'custom', 'instanceId': 'skd'}],
                'accessKey': 'synthetic-access-key', 'catalogModifications': [other.copy()],
                'upstreamCatalogOrder': ['other']}
        with patch.object(self.s, 'aio_rows_changed'):
            self.add()
        self.assertIn('refreshed', refresh(self.data, transport=aio, server=self.s))
        ana = aio.configs['synthetic-uuid-ana']
        sam = aio.configs['synthetic-uuid-sam']
        self.assertIn(cid, [r['id'] for r in ana['catalogModifications']])
        self.assertNotIn(cid, [r['id'] for r in sam['catalogModifications']])
        self.assertLess(next(i for i, r in enumerate(ana['catalogModifications']) if r['id'] == cid),
                        ana['catalogModifications'].index(other))
        self.assertIn('skd1a2b', ana['upstreamCatalogOrder'])
        with patch.object(self.s, 'aio_rows_changed'):
            self.s.shelf_change('ana', 'stop', slot=1, apply=True)
        self.assertIn('refreshed', refresh(self.data, transport=aio, server=self.s))
        self.assertNotIn(cid, [r['id'] for r in aio.configs['synthetic-uuid-ana']['catalogModifications']])
        self.assertIn(other, aio.configs['synthetic-uuid-ana']['catalogModifications'])
        self.assertEqual(ana['accessKey'], 'synthetic-access-key')
        saved = setup.state(self.data)
        self.assertFalse(saved['aiostreams_pending'])
        self.assertNotIn('aiostreams_hint', saved)

    def test_empty_saved_logins_do_not_start_background_thread(self):
        bg = self.background()
        (self.data / 'credentials/aiostreams.json').write_text('{}')
        self.add()
        self.assertIsNone(bg.thread)
        self.assertFalse(self.fake.calls)

    def test_search_preview_dismiss_and_non_shelf_prefs_send_nothing(self):
        self.background()
        with self.s.addon_scope('ana'), patch.object(self.s, 'search_both', return_value=([], {})):
            self.s.search('movie', 'Synthetic Film', 'en-US', title_only=True)
            self.s.search('movie', 'make me a row of heist films', 'en-US')
            plan = commands.preview(self.s, 'hide anime', 'ana', answer={'op': 'hide', 'kind': None,
                                   'values': ['anime'], 'person': None})
            commands.apply(self.s, plan)
            suggestion = {'id': 'synthetic', 'text': 'genre:drama', 'name': 'Drama', 'when':
                          {'days': [6], 'from_hour': 5, 'to_hour': 11}, 'evidence': {'weeks': 3}}
            with patch('skald.engine.suggestions', return_value={'suggestions': [suggestion]}):
                commands.suggestion_cards(self.s, 'ana', 'ana', 'ana', 'movie')
                code = next(iter(self.s._command_suggestions['ana']))
                commands.suggestion_cards(self.s, 'ana', 'ana', 'ana', 'movie', dismiss='no ' + code)
        self.assertIsNone(self.s._aio_refresh.thread)
        self.assertFalse(self.fake.calls)

    def test_each_commit_and_person_together_counts_refresh(self):
        self.background()
        changes = [('ana', 'add', {'text': 'genre:drama'}), ('ana', 'replace', {'slot': 1, 'text': 'genre:comedy'}),
                   ('ana', 'stop', {'slot': 1}), ('ana', 'undo', {}), ('ana', 'count', {'count': 6}),
                   ('together', 'count', {'count': 3})]
        with patch('skald.aio_refresh.time.sleep'):
            for scope, op, kwargs in changes:
                with self.subTest(op=op, scope=scope):
                    self.fake.calls.clear()
                    self.s.shelf_change(scope, op, apply=True, **kwargs)
                    self.join()
                    self.assertEqual(len(self.fake.calls), 4)

    def test_cli_add_refreshes_synchronously_and_prints_line(self):
        original = setup.aiostreams_request
        def request(url, **kwargs):
            self.assertEqual(threading.current_thread(), threading.main_thread())
            kwargs['transport'] = self.fake
            return original(url, **kwargs)
        out = io.StringIO()
        with patch.object(setup, 'aiostreams_request', side_effect=request):
            self.assertEqual(main(['shelf', 'add', '--person', 'ana', '--yes', 'genre:drama'], out=out), 0)
        self.assertEqual(len(self.fake.calls), 4)
        self.assertIn('AIOStreams rows refreshed (2 logins)', out.getvalue().splitlines())
        self.assertEqual(out.getvalue().splitlines()[0], 'AIOStreams rows refreshed (2 logins)')

    def test_cli_context_releases_locks_and_coalesces_two_changes(self):
        from skald import shelves
        original = setup.aiostreams_request
        probes = []
        def transport(req, timeout):
            with (self.data / 'shelves.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def probe():
                for lock in (shelves.LOCK, commands._LOCK):
                    acquired = lock.acquire(blocking=False)
                    probes.append(acquired)
                    if acquired:
                        lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(probes))
            return self.fake(req, timeout)
        def request(url, **kwargs):
            kwargs['transport'] = transport
            return original(url, **kwargs)
        with patch.object(setup, 'aiostreams_request', side_effect=request):
            with setup.server(self.data, report=self.lines.append) as s:
                s.shelf_change('ana', 'add', text='genre:drama', apply=True)
                commands.apply(s, commands.preview(s, 'show 3 shelves', 'ana'))
                self.assertFalse(self.fake.calls)
                self.assertFalse(self.lines)
        self.assertEqual([c[0] for c in self.fake.calls], ['GET', 'PUT', 'GET', 'PUT'])
        self.assertEqual(probes, [True] * 8)
        self.assertEqual(self.lines, ['AIOStreams rows refreshed (2 logins)'])

    def test_cli_context_exception_skips_refresh(self):
        original = setup.aiostreams_request
        def request(url, **kwargs):
            kwargs['transport'] = self.fake
            return original(url, **kwargs)
        with patch.object(setup, 'aiostreams_request', side_effect=request):
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                with setup.server(self.data, report=self.lines.append) as s:
                    s.shelf_change('ana', 'add', text='genre:drama', apply=True)
                    raise ValueError('synthetic failure')
        self.assertFalse(self.fake.calls)
        self.assertFalse(self.lines)

    def test_cli_json_keeps_refresh_line_on_stderr(self):
        original = setup.aiostreams_request
        def request(url, **kwargs):
            kwargs['transport'] = self.fake
            return original(url, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(setup, 'aiostreams_request', side_effect=request), redirect_stderr(err):
            self.assertEqual(main(['shelf', 'add', '--person', 'ana', '--yes', '--json', 'genre:drama'], out=out), 0)
        self.assertEqual(len(json.loads(out.getvalue())['wishes']), 1)
        self.assertEqual(err.getvalue(), 'AIOStreams rows refreshed (2 logins)\n')

    def test_count_change_card_omits_manual_refresh_hint(self):
        plan = commands.preview(self.s, 'show 3 shelves', 'ana')
        self.assertEqual(plan['name'], 'Count 3 shelves for ' + self.s.LABELS['ana'] + '; always on; hidden wishes are kept')

    def test_clear_prefs_and_undo_refresh_only_when_count_changes(self):
        self.background()
        with patch('skald.aio_refresh.time.sleep'):
            self.s.shelf_change('ana', 'count', count=6, apply=True)
            self.join()
        self.fake.calls.clear()
        original = setup.aiostreams_request
        out = io.StringIO()
        def request(url, **kwargs):
            kwargs['transport'] = self.fake
            return original(url, **kwargs)
        with patch.object(setup, 'aiostreams_request', side_effect=request), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['setup', 'prefs', '--person', 'ana', '--clear'], out=out), 0)
        self.assertEqual(len(self.fake.calls), 4)
        with patch('skald.aio_refresh.time.sleep'):
            commands.undo(self.s, ['ana'])
            self.join()
        self.assertEqual(len(self.fake.calls), 8)

    def test_safe_hints_and_unexpected_error_stop_first_login(self):
        from skald.aio_refresh import refresh
        for status, code, hint in [(401, 'USER_INVALID_DETAILS', 'wrong login'), (429, None, 'rate limit')]:
            self.fake.failure = status, code
            self.fake.calls.clear()
            self.assertIn(hint, refresh(self.data, transport=self.fake))
            self.assertEqual(len(self.fake.calls), 2)
        for error in (OSError('synthetic-password /private/path'), RuntimeError('synthetic-uuid-ana')):
            with self.subTest(error=type(error).__name__):
                calls = []
                def transport(req, timeout):
                    calls.append(req.get_method())
                    raise error
                line = refresh(self.data, transport=transport)
                self.assertEqual(calls, ['GET'])
                self.assertNotIn('synthetic-', line)
                self.assertNotIn('/private/path', line)

    def test_docs_explain_automatic_names_and_catalog_delay(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('docs/CLI.md', 'docs/SETUP.md', 'skills/skald-taste/SKILL.md', 'docs/SEARCH.md'):
            text = (root / name).read_text()
            self.assertNotIn('apps may need one addon refresh', text)
            self.assertIn('skald setup aiostreams', text)
            self.assertIn('reload row names', text)
        self.assertIn('CATALOG_CACHE_TTL', (root / 'docs/SETUP.md').read_text())
        self.assertLessEqual(len((root / 'README.md').read_text().splitlines()), 260)

