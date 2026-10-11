"""Setup publication, failed credential replacement and secret-free JSON."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members, setup, recipes
from skald.__main__ import main, parser


class SetupFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'fake'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self):
        cfg = members.fresh_config(['alex', 'sam'])
        (self.data/'skald.json').write_text(json.dumps(cfg))
        return cfg

    def test_fresh_setup_loses_publication_race_without_overwriting(self):
        winner = members.fresh_config(['eve', 'fin'])
        validate = members.validate_config
        def concurrent(cfg, **kw):
            (self.data/'skald.json').write_text(json.dumps(winner))
            return validate(cfg, **kw)
        with patch.object(members, 'validate_config', side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, 'another setup'):
                setup.household(self.data, ['alex', 'sam'], 'en-US', 'UTC', 'United States')
        self.assertEqual(json.loads((self.data/'skald.json').read_text()), winner)

    def test_credential_replacement_stages_then_probes_or_preserves_old(self):
        cfg = self.config()
        credentials = self.data/'credentials'
        credentials.mkdir(mode=0o700)
        token = credentials/'jellyfin.token'
        token.write_text('old-fake-token')
        token.chmod(0o600)
        cfg['connectors']['jellyfin'] = {'kind': 'player', 'enabled': True, 'auth': 'file:jellyfin.token',
            'options': {'url': 'http://previous', 'users': {'one': 'alex'}}}
        before = json.dumps(cfg)
        (self.data/'skald.json').write_text(before)
        for failure in ('address', 'mapping', 'probe', None):
            with self.subTest(failure=failure), setup.server(self.data) as s:
                args = parser().parse_args(['setup', '--connect', 'jellyfin', '--yes', '--secret-stdin',
                    '--source-url', 'invalid' if failure == 'address' else 'http://replacement',
                    '--map', 'one=unknown' if failure == 'mapping' else 'one=alex'])
                def probe(server, cid):
                    self.assertEqual(token.read_text(), 'old-fake-token')
                    self.assertEqual(server._resolve_auth(server.CONFIG['connectors'][cid]['auth']), 'new-fake-token')
                    return {'ok': failure != 'probe', 'connectors': []}
                with patch('sys.stdin', io.StringIO('new-fake-token\n')), \
                        patch.object(setup.connectors, 'source_users', return_value={'one': 'One'}), \
                        patch.object(setup.connectors, 'test', side_effect=probe):
                    call = lambda: setup.configure_connector(s, args, lambda v, label, default, flag: v or default, [], lambda: None)
                    if failure in ('address', 'mapping'):
                        with self.assertRaises(ValueError):
                            call()
                    else:
                        self.assertEqual(call()['ok'], failure is None)
                self.assertEqual(token.read_text(), 'old-fake-token')
                if failure:
                    self.assertEqual((self.data/'skald.json').read_text(), before)
                    self.assertEqual(list(credentials.iterdir()), [token])
                else:
                    saved = json.loads((self.data/'skald.json').read_text())
                    self.assertNotEqual(saved['connectors']['jellyfin']['auth'], 'file:jellyfin.token')

    def test_directory_sync_failure_keeps_published_credential(self):
        self.config()
        with setup.server(self.data) as s:
            args = parser().parse_args(['setup', '--connect', 'jellyfin', '--yes', '--secret-stdin',
                '--source-url', 'http://replacement', '--map', 'one=alex'])
            fsync = os.fsync
            def sync(fd):
                import stat
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    saved = json.loads((self.data/'skald.json').read_text())
                    if saved['connectors']:
                        raise OSError('directory sync failed')
                return fsync(fd)
            with patch('sys.stdin', io.StringIO('new-fake-token\n')), \
                    patch.object(setup.connectors, 'source_users', return_value={'one': 'One'}), \
                    patch.object(setup.connectors, 'test', return_value={'ok': True, 'connectors': []}), \
                    patch('os.fsync', side_effect=sync):
                with self.assertRaisesRegex(OSError, 'directory sync failed'):
                    setup.configure_connector(s, args, lambda v, label, default, flag: v or default, [], lambda: None)
            saved = json.loads((self.data/'skald.json').read_text())
            reference = saved['connectors']['jellyfin']['auth']
            self.assertEqual(s._resolve_auth(reference), 'new-fake-token')

    def test_json_link_refuses_before_issuing_token(self):
        self.config()
        out = io.StringIO()
        with patch.object(setup, 'link', side_effect=AssertionError('must not issue')):
            self.assertEqual(main(['setup', 'link', '--name', 'laptop', '--base', 'https://api.example', '--json'], out=out), 2)
        self.assertEqual(json.loads(out.getvalue())['error'], 'run without --json; the token is shown once')
        self.assertFalse((self.data/'clients.json').exists())

    def test_json_setup_redacts_addon_token_but_text_shows_it(self):
        with patch.object(setup, 'check_tmdb', return_value=True):
            out = io.StringIO()
            args = ['setup', '--yes', '--members', 'alex,sam', '--base', 'https://tv.example', '--connect', 'files']
            self.assertEqual(main([*args, '--json'], out=out), 0)
            with setup.server(self.data) as s:
                token = s.addon_token()
            self.assertNotIn(token, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertFalse(result['addon_urls_shown'])
            self.assertIn('<addon-token>', out.getvalue())
            out = io.StringIO()
            self.assertEqual(main(['setup', '--yes'], out=out), 0)
            self.assertIn(token, out.getvalue())

    def test_connect_ignores_default_snapshot_env(self):
        with patch.dict(os.environ, {'SKALD_SNAPSHOT': str(self.data), 'SKALD_SNAPSHOT_U': str(self.data), 'ODIN_SNAPSHOT': str(self.data)}), \
                patch('pathlib.Path.home', return_value=self.data), patch('getpass.getpass', return_value='fake-token'):
            out = io.StringIO()
            self.assertEqual(main(['connect', 'https://api.example', '--json'], out=out), 0, out.getvalue())

    def test_watch_examples_include_token(self):
        doc = recipes.watch_doc()
        self.assertIn('http://skald:8080/<addon-token>/<lang>/manifest.json', doc)
        self.assertIn('copy the exact URL that `skald setup --watch aiostreams` prints', doc)

    def test_run_keeps_create_decision_if_another_setup_wins_before_household_step(self):
        winner = members.fresh_config(['eve', 'fin'])
        original = setup.household
        def concurrent(*args, **kwargs):
            (self.data/'skald.json').write_text(json.dumps(winner))
            return original(*args, **kwargs)
        with patch.object(setup, 'check_tmdb', return_value=True), patch.object(setup, 'household', side_effect=concurrent):
            out = io.StringIO()
            self.assertEqual(main(['setup', '--yes', '--members', 'alex,sam', '--base', 'https://tv.example', '--connect', 'files', '--json'], out=out), 2)
            self.assertIn('another setup', out.getvalue())
        self.assertEqual(json.loads((self.data/'skald.json').read_text()), winner)
