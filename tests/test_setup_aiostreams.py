"""AIOStreams 2.35.9 setup contract, with no network or private data."""
from contextlib import redirect_stderr
import base64
import copy
import io
import http.client
import json
import os
import secrets
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from skald import members, setup
from skald.__main__ import main


class Reply(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class FakeAIO:
    def __init__(self):
        self.calls = []
        self.configs = {}
        self.protected = False
        self.jellyfin = True
        self.failure = None
        self.meta = [{'name': 'meta', 'idPrefixes': ['tmdb:']}]
        self.catalogs = [{'id': 'other.unrelated.catalog'}, {'id': 'skd1a2b.skald.foryou'}]

    def __call__(self, req, timeout):
        assert timeout == 20
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, req.full_url, body, req.get_header('Authorization')))
        if req.full_url.endswith('/status'):
            value = {'settings': {'baseUrl': 'https://aio.example.org', 'protected': self.protected,
                                  'jellyfin': {'enabled': self.jellyfin}}, 'version': '2.35.9'}
        elif '/stremio/' in req.full_url:
            return Reply({'resources': self.meta, 'catalogs': self.catalogs})
        elif self.failure:
            code, message, http = self.failure
            raise urllib.error.HTTPError(req.full_url, http, 'error', {}, Reply(
                {'success': False, 'error': {'code': code, 'message': message}}))
        elif method == 'POST':
            uid = f'a1b2c3d{len(self.configs) + 1}-fake'
            self.configs[uid] = copy.deepcopy(body['config'])
            value = {'uuid': uid, 'encryptedPassword': 'encrypted'}
        else:
            uid = base64.b64decode(req.get_header('Authorization').split()[1]).decode().split(':')[0]
            if method == 'PUT':
                self.configs[uid] = copy.deepcopy(body['config'])
            value = {'userData': copy.deepcopy(self.configs[uid]), 'encryptedPassword': 'encrypted'}
        return Reply({'success': True, 'data': value})


class AIOStreamsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '',
            'SKALD_SNAPSHOT_U': '', 'SKALD_INTERNAL_BASE': ''}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(members.configure)
        setup.household(self.data, ['alex', 'sam'], 'en-US', 'UTC', 'United States')
        setup.write_private(self.data/'setup.json', json.dumps({'base': 'https://skald.example.org',
            'internal_base': 'http://skald:8080', 'watch': 'jellyfin-app', 'taste': 'files', 'tmdb_checked': True}))
        self.fake = FakeAIO()
        self.transport = patch('urllib.request.OpenerDirector.open', side_effect=self.fake)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        generate = secrets.token_urlsafe
        self.password = patch('secrets.token_urlsafe', side_effect=lambda n: 'fake-password' if n == 18 else generate(n))
        self.password.start()
        self.addCleanup(self.password.stop)

    def run_setup(self, *flags, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdin', io.StringIO(stdin)), redirect_stderr(err):
            code = main(['setup', 'aiostreams', '--yes', *flags], out=out)
        self.assertNotIn('FAKE-DEBRID-KEY', out.getvalue() + err.getvalue())
        return code, out.getvalue()

    def saved(self):
        return json.loads((self.data/'credentials/aiostreams.json').read_text())

    def existing(self, presets=None):
        self.fake.configs['existing'] = {'formatter': {'id': 'other'}, 'sortCriteria': {'global': ['keep']},
            'presets': presets or [{'type': 'debrid', 'instanceId': 'skd', 'options': {'key': 'FAKE-DEBRID-KEY'}}],
            'other': {'keep': [1, 2]}}
        self.fake.meta = []

    def test_new_exact_config_private_credentials_and_rerun(self):
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        body = next(c[2] for c in self.fake.calls if c[0] == 'POST')
        with setup.server(self.data) as s:
            url = s.addon_urls('http://skald:8080')[0]
        self.assertEqual(body, {'password': 'fake-password', 'config': {'formatter': {'id': 'gdrive'},
            'sortCriteria': {'global': []}, 'presets': [
                {'type': 'custom', 'instanceId': 'skd', 'enabled': True, 'options': {'name': 'Skald',
                 'manifestUrl': url, 'timeout': 7000, 'mediaTypes': [], 'libraryAddon': False, 'resultPassthrough': False}},
                {'type': 'tmdb-addon', 'instanceId': 'tmd', 'enabled': True, 'options': {'name': 'The Movie Database',
                 'timeout': 7500, 'resources': ['meta']}}]}})
        self.assertEqual(self.saved(), {'household': {'uuid': 'a1b2c3d1-fake', 'password': 'fake-password', 'member': 'household'}})
        self.assertEqual((self.data/'credentials/aiostreams.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(text.count('server  https://aio.example.org/jellyfin'), 1)
        self.assertEqual(text.count('user    a1b2c3d1-fake'), 1)
        self.assertEqual(text.count('fake-password'), 1)
        self.fake.calls.clear()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertIn('AIOStreams already set up', text)
        self.assertEqual([c[0] for c in self.fake.calls], ['GET', 'GET', 'GET'])
        self.assertNotIn('fake-password', text)

    def test_json_hides_secrets(self):
        code, text = self.run_setup('--json')
        self.assertEqual(code, 0, text)
        self.assertTrue(json.loads(text)['ok'])
        for private in ('fake-password', '/stremio/', 'encrypted'):
            self.assertNotIn(private, text)

    def test_existing_adds_both_and_preserves_config(self):
        self.existing()
        before = copy.deepcopy(self.fake.configs['existing'])
        code, text = self.run_setup('--uuid', 'existing', '--password-stdin', stdin='old-password\n')
        self.assertEqual(code, 0, text)
        after = self.fake.configs['existing']
        self.assertEqual(after['presets'][:1], before['presets'])
        self.assertEqual({k: v for k, v in after.items() if k not in ('presets', 'upstreamCatalogOrder')}, {k: v for k, v in before.items() if k != 'presets'})
        self.assertEqual(len({p['instanceId'] for p in after['presets']}), 3)
        self.assertEqual(after['upstreamCatalogOrder'], ['skd1a2b'])
        self.assertIn('add Skald', text)
        self.assertIn('add The Movie Database (title details)', text)
        self.assertIn('1 other addons kept', text)
        self.assertEqual(self.saved()['household']['uuid'], 'existing')

    def test_url_update_only_and_confirmation(self):
        self.existing([{'type': 'custom', 'instanceId': 'abc', 'enabled': False,
                        'options': {'name': 'Skald', 'manifestUrl': 'https://skald.example.org/old-token/en-US/manifest.json', 'extra': 1}}])
        self.fake.meta = [{'name': 'meta'}]
        self.fake.configs['existing']['upstreamCatalogOrder'] = ['skd1a2b']
        before = copy.deepcopy(self.fake.configs['existing'])
        with patch('builtins.input', return_value='y') as ask, patch('getpass.getpass', return_value='old-password'):
            out = io.StringIO()
            self.assertEqual(main(['setup', 'aiostreams', '--uuid', 'existing'], out=out), 0, out.getvalue())
        ask.assert_called_once_with('Save? [y/N] ')
        before['presets'][0]['options']['manifestUrl'] = self.fake.configs['existing']['presets'][0]['options']['manifestUrl']
        self.assertEqual(self.fake.configs['existing'], before)
        self.assertIn('update Skald address', out.getvalue())

    def test_protected_key_sent_never_stored(self):
        self.fake.protected = True
        code, text = self.run_setup('--secret-stdin', stdin='access-secret\n')
        self.assertEqual(code, 0, text)
        self.assertEqual(self.fake.configs['a1b2c3d1-fake']['accessKey'], 'access-secret')
        self.assertNotIn('access-secret', text)
        for path in (self.data/'setup.json', self.data/'credentials/aiostreams.json'):
            self.assertNotIn('access-secret', path.read_text())
        self.fake.calls.clear()
        code, text = self.run_setup('--secret-stdin', stdin='new-access\n')
        self.assertEqual(code, 0, text)
        self.assertEqual(next(c[2]['config']['accessKey'] for c in self.fake.calls if c[0] == 'PUT'), 'new-access')

    def test_errors_manual_steps_and_no_secrets(self):
        for code, message, http, hint in [('USER_INVALID_CONFIG', 'Failed to fetch manifest for Skald: fetch failed', 400, 'same Docker network? else use --base https address'),
            ('USER_INVALID_DETAILS', 'Invalid UUID or password', 400, 'wrong login'),
            ('ADDON_PASSWORD_INVALID', 'bad key', 403, 'access key'), ('LIMIT', 'too many', 429, 'try again later')]:
            with self.subTest(code=code):
                self.fake.failure = (code, message, http)
                result, text = self.run_setup()
                self.assertNotEqual(result, 0)
                self.assertIn(hint, text)
                self.assertIn('By hand instead', text)
                self.assertIn('The Movie Database', text)
                self.assertFalse((self.data/'credentials/aiostreams.json').exists())

    def test_all_member_urls(self):
        code, text = self.run_setup('--member', 'all')
        self.assertEqual(code, 0, text)
        self.assertEqual(set(self.saved()), {'alex', 'sam', 'together'})
        with setup.server(self.data) as s:
            urls = s.addon_urls('http://skald:8080')[1:]
        self.assertEqual([c['presets'][0]['options']['manifestUrl'] for c in self.fake.configs.values()], urls)
        self.assertEqual(len(self.fake.configs), 3)

    def test_status_and_profile_mapping_default(self):
        with patch.dict(os.environ, {'TMDB_API': 'fake'}):
            self.assertEqual(setup.status(self.data)['next'], 'skald setup aiostreams')
        self.run_setup('--member', 'alex')
        out = io.StringIO()
        with patch.dict(os.environ, {'TMDB_API': 'fake'}):
            self.assertEqual(main(['setup', '--status'], out=out), 0)
        self.assertIn('AIOStreams login: made for alex', out.getvalue())
        with setup.server(self.data) as s:
            args = __import__('skald.__main__', fromlist=['parser']).parser().parse_args(['setup', '--connect', 'aiostreams-db', '--source-path', '/fake'])
            args.connect = 'aiostreams_db'
            answers = []
            def ask(value, label, default, flag):
                answers.append(default)
                return value or default
            with patch('skald.connectors.source_users', return_value={'a1b2c3d1': 'profile'}), patch('skald.connectors.test', return_value={'ok': True}), patch('skald.connectors.text', return_value=[]):
                setup.configure_connector(s, args, ask, [], lambda: None)
            self.assertEqual(answers[-1], 'alex')

    def test_jellyfin_warning_and_public_base(self):
        setup.write_private(self.data/'setup.json', json.dumps({'base': 'https://skald.example.org'}))
        self.fake.jellyfin = False
        code, text = self.run_setup('--aio-url', 'http://aio.test')
        self.assertEqual(code, 0, text)
        self.assertIn('set JELLYFIN_ENABLED=true in .env.aiostreams', text)
        self.assertTrue(self.fake.configs['a1b2c3d1-fake']['presets'][0]['options']['manifestUrl'].startswith('https://skald.example.org/'))
        self.assertEqual(setup.state(self.data)['aio_url'], 'http://aio.test')

    def test_transport_bounds_redirects_and_json_errors(self):
        class Large(io.BytesIO):
            def read(self, size=-1):
                self.size = size
                return b' ' * size
        response = Large()
        with self.assertRaisesRegex(ValueError, 'invalid JSON'):
            setup.aiostreams_request('http://aio.test', transport=lambda req, timeout: response)
        self.assertLessEqual(response.size, 2 * 1024 * 1024)
        for value in ([1], {'success': False, 'error': 'FAKE-DEBRID-KEY'}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'AIOStreams'):
                setup.aiostreams_request('http://aio.test', transport=lambda req, timeout: Reply(value))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = Reply({'success': True, 'data': {}})
            setup.aiostreams_request('http://aio.test')
            self.assertIsInstance(build.call_args.args[0], setup.remote.NoRedirect)

    def test_declined_save_keeps_login_and_config_unsaved(self):
        self.existing()
        before = copy.deepcopy(self.fake.configs)
        with patch('builtins.input', return_value='n'), patch('getpass.getpass', return_value='old-password'):
            out = io.StringIO()
            self.assertEqual(main(['setup', 'aiostreams', '--uuid', 'existing'], out=out), 0)
        self.assertEqual(self.fake.configs, before)
        self.assertFalse((self.data/'credentials/aiostreams.json').exists())

    def test_skald_recognized_by_exact_member_url_without_name(self):
        with setup.server(self.data) as s:
            url = s.addon_urls('https://skald.example.org')[1]
        self.existing([{'type': 'custom', 'instanceId': 'abc', 'options': {'manifestUrl': url}}])
        self.fake.meta = [{'name': 'meta', 'idPrefixes': []}]
        code, text = self.run_setup('--uuid', 'existing', '--password-stdin', stdin='old-password\n')
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.fake.configs['existing']['presets']), 1)
        self.assertIn('update Skald address', text)

    def test_yes_requires_private_stdin_flags(self):
        with patch('getpass.getpass', side_effect=AssertionError('unexpected private prompt')):
            code, text = self.run_setup('--uuid', 'existing')
            self.assertNotEqual(code, 0)
            self.assertIn('--password-stdin', text)
            self.fake.protected = True
            code, text = self.run_setup()
            self.assertNotEqual(code, 0)
            self.assertIn('--secret-stdin', text)

    def test_unrelated_custom_addons_are_kept_byte_equal(self):
        presets = [{'type': 'custom', 'instanceId': 'aaa', 'options': {
            'name': 'Other', 'manifestUrl': 'https://skald.example.org/other/en-US/manifest.json'}},
            {'type': 'custom', 'instanceId': 'bbb', 'options': {
            'name': 'Skald', 'manifestUrl': 'https://unrelated.example/manifest.json'}}]
        self.existing(presets)
        before = json.dumps(presets).encode()
        code, text = self.run_setup('--uuid', 'existing', '--password-stdin', stdin='old-password\n')
        self.assertEqual(code, 0, text)
        self.assertEqual(json.dumps(self.fake.configs['existing']['presets'][:2]).encode(), before)
        self.assertIn('add Skald', text)

    def test_explicit_base_wins_over_saved_and_env_internal(self):
        with patch.dict(os.environ, {'SKALD_INTERNAL_BASE': 'http://another:8080'}):
            code, text = self.run_setup('--base', 'https://remote.example', '--aio-url', 'https://elf.example')
        self.assertEqual(code, 0, text)
        self.assertTrue(self.fake.configs['a1b2c3d1-fake']['presets'][0]['options']['manifestUrl'].startswith('https://remote.example/'))
        self.assertEqual(setup.state(self.data)['internal_base'], 'http://skald:8080')
        self.assertEqual(setup.state(self.data)['base'], 'https://remote.example')

    def test_incomplete_http_response_prints_hint_and_manual_steps(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=http.client.IncompleteRead(b'partial', 10)):
            code, text = self.run_setup()
        self.assertNotEqual(code, 0)
        self.assertIn('AIOStreams unavailable or invalid JSON; check --aio-url and try again', text)
        self.assertIn('By hand instead', text)

    def test_manifest_uses_reachable_aio_url(self):
        self.existing()
        code, text = self.run_setup('--uuid', 'existing', '--password-stdin', '--aio-url', 'http://aio.internal:3000', stdin='old-password\n')
        self.assertEqual(code, 0, text)
        manifests = [c[1] for c in self.fake.calls if '/stremio/' in c[1]]
        self.assertTrue(manifests)
        self.assertTrue(all(url.startswith('http://aio.internal:3000/stremio/') for url in manifests))

    def test_new_login_saves_row_order_and_rerun_does_not_put(self):
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        puts = [c for c in self.fake.calls if c[0] == 'PUT']
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2]['config']['upstreamCatalogOrder'], ['skd1a2b'])
        self.assertEqual(puts[0][2]['config']['presets'], next(c[2]['config']['presets'] for c in self.fake.calls if c[0] == 'POST'))
        self.fake.calls.clear()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertFalse(any(c[0] == 'PUT' for c in self.fake.calls))

    def test_existing_login_keeps_order_entries_and_confirms_order_change(self):
        self.existing()
        self.fake.configs['existing']['upstreamCatalogOrder'] = ['other-addon']
        out = io.StringIO()
        with patch('builtins.input', return_value='y'), patch('getpass.getpass', return_value='old-password'):
            self.assertEqual(main(['setup', 'aiostreams', '--uuid', 'existing'], out=out), 0, out.getvalue())
        self.assertIn("keep Skald's row order", out.getvalue())
        self.assertEqual(self.fake.configs['existing']['upstreamCatalogOrder'], ['other-addon', 'skd1a2b'])

    def scope_entries(self, person='household'):
        with setup.server(self.data) as s, s.addon_scope(None if person == 'household' else person):
            rows = [(cid, typ) for cid, types, _ in s.feed_rows(s.LANG)
                    for typ in (('tv',) if cid in s.MIXED and 'tv' in types else types)]
            rows += [(s.rid('search'), typ) for typ in ('movie', 'series')]
        return [{'id': 'skd1a2b.' + cid, 'type': typ, 'enabled': True} for cid, typ in rows]

    def test_catalog_modifications_refill_slots_and_insert_hidden_rows(self):
        self.run_setup()
        uid = self.saved()['household']['uuid']
        rows = self.scope_entries()
        rows[0].update(enabled=False, extra={'keep': 'FAKE-DEBRID-KEY'})
        others = [{'id': 'other.a', 'type': 'movie', 'enabled': False, 'extra': [1]},
                  {'id': 'other.b', 'type': 'series', 'name': 'keep'},
                  {'id': 'skd1a2bX.unrelated', 'type': 'tv', 'enabled': True}]
        self.fake.configs[uid]['catalogModifications'] = [others[0], rows[2], others[1], rows[0], *rows[3:], others[2]]
        self.fake.calls.clear()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        after = self.fake.configs[uid]['catalogModifications']
        self.assertEqual(after, [others[0], rows[0], rows[1], others[1], *rows[2:], others[2]])
        self.assertEqual(json.dumps([e for e in after if e in others]).encode(), json.dumps(others).encode())
        self.assertIn("keep Skald's rows in place (" + str(len(rows)) + ' rows)', text)
        self.assertNotIn('fake-password', text)
        self.assertNotIn('encrypted', text)
        self.assertEqual(sum(c[0] == 'PUT' for c in self.fake.calls), 1)
        self.fake.calls.clear()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertIn('AIOStreams already set up', text)
        self.assertFalse(any(c[0] == 'PUT' for c in self.fake.calls))

    def test_catalog_modifications_uuid_confirms_and_inserts_first_row(self):
        self.run_setup('--member', 'alex')
        uid = self.saved()['alex']['uuid']
        rows = self.scope_entries('alex')
        other = {'id': 'other.a', 'type': 'movie', 'extra': 'FAKE-DEBRID-KEY'}
        before = [other, *rows[1:]]
        self.fake.configs[uid]['catalogModifications'] = copy.deepcopy(before)
        for answer in ('n', 'y'):
            with self.subTest(answer=answer), patch('builtins.input', return_value=answer) as ask, patch('getpass.getpass', return_value='fake-password'):
                self.fake.calls.clear()
                out = io.StringIO()
                self.assertEqual(main(['setup', 'aiostreams', '--member', 'alex', '--uuid', uid], out=out), 0, out.getvalue())
                ask.assert_called_once_with('Save? [y/N] ')
                self.assertNotIn('FAKE-DEBRID-KEY', out.getvalue())
                self.assertNotIn('fake-password', out.getvalue())
                self.assertEqual(self.fake.configs[uid]['catalogModifications'], [other, *rows] if answer == 'y' else before)
                self.assertEqual(any(c[0] == 'PUT' for c in self.fake.calls), answer == 'y')

    def test_catalog_modifications_without_skald_append_block(self):
        self.run_setup()
        uid = self.saved()['household']['uuid']
        other = {'id': 'other.a', 'type': 'movie', 'enabled': False}
        self.fake.configs[uid]['catalogModifications'] = [other]
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.fake.configs[uid]['catalogModifications'], [other, *self.scope_entries()])

    def test_catalog_modifications_retired_entry_keeps_first_skald_place(self):
        self.run_setup()
        uid = self.saved()['household']['uuid']
        other = {'id': 'other.a', 'type': 'movie'}
        retired = {'id': 'skd1a2b.retired', 'type': 'tv', 'enabled': False, 'extra': 1}
        self.fake.configs[uid]['catalogModifications'] = [other, retired, other]
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.fake.configs[uid]['catalogModifications'],
                         [other, *self.scope_entries(), retired, other])

    def test_new_login_catalog_modifications_from_aio_defaults(self):
        transport = self.fake.__call__
        def defaults(req, timeout):
            reply = transport(req, timeout)
            if req.get_method() == 'POST':
                self.fake.configs['a1b2c3d1-fake']['catalogModifications'] = [{'id': 'other.a', 'type': 'movie'}]
            return reply
        with patch('urllib.request.OpenerDirector.open', side_effect=defaults):
            code, text = self.run_setup('--json')
        self.assertEqual(code, 0, text)
        self.assertEqual(self.fake.configs['a1b2c3d1-fake']['catalogModifications'],
                         [{'id': 'other.a', 'type': 'movie'}, *self.scope_entries()])
        self.assertIn("keep Skald's rows in place", text)
        self.assertNotIn('fake-password', text)

    def test_empty_catalog_modifications_stay_empty(self):
        self.run_setup()
        uid = self.saved()['household']['uuid']
        self.fake.configs[uid]['catalogModifications'] = []
        self.fake.calls.clear()
        code, text = self.run_setup()
        self.assertEqual(code, 0, text)
        self.assertIn('AIOStreams already set up', text)
        self.assertEqual(self.fake.configs[uid]['catalogModifications'], [])
        self.assertFalse(any(c[0] == 'PUT' for c in self.fake.calls))

    def test_status_done_and_missing_taste(self):
        self.run_setup()
        with patch.dict(os.environ, {'TMDB_API': 'fake'}):
            self.assertEqual(setup.status(self.data)['next'], 'restart Skald (docker compose restart engine)')
            with setup.server(self.data) as s:
                setup.record_startup(self.data, s.CONFIG)
            self.assertEqual(setup.status(self.data)['next'], 'nothing left')
            saved = setup.state(self.data)
            saved.pop('taste')
            setup.write_private(self.data/'setup.json', json.dumps(saved))
            self.assertEqual(setup.status(self.data)['next'], 'skald setup --connect files')

    def test_cache_setting_and_new_household_recipe(self):
        from skald import recipes
        root = Path(__file__).resolve().parents[1]
        self.assertIn('MANIFEST_CACHE_TTL=*:21600,skald:600', (root/'.env.aiostreams.example').read_text())
        for name in ('aiostreams', 'jellyfin-app'):
            text = recipes.recipe(name, 'https://skald.example/manifest.json', 'en-US')
            self.assertIn('Rows appear once they have enough titles', text)
            self.assertIn('MANIFEST_CACHE_TTL', text)
