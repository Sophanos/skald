"""Live probe clients use fake HTTP and read-only SQLite, never DATA writes."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from history_kit import NOW, Reply, watch_db, trakt_pages
from player_kit import player_fixture, jellyfin_transport, plex_transport
from decide_kit import load_server, close_server
from skald import members
from skald.__main__ import main


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.s.CONFIG = self.s.fresh_config(['ana', 'ben'])
        self.s.CONFIG_SOURCE = 'file'
        folder = self.data/'credentials'
        folder.mkdir(mode=0o700)
        (folder/'fake.token').write_text('fake')
        (folder/'fake.token').chmod(0o600)

    def configure(self, cid, options):
        self.s.CONFIG['connectors'][cid] = {'kind': 'player' if cid != 'trakt_api' else 'api',
            'enabled': True, 'auth': None if cid == 'aiostreams_db' else 'file:fake.token', 'options': options}

    def probe(self, cid, transport=None, show=False):
        from skald import connectors
        before = {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()}
        result = connectors.test(self.s, cid, transport=transport, now=NOW, show=show)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file()})
        self.assertNotIn('fake.token', json.dumps(result))
        return result['connectors'][0]

    def test_jellyfin_probe_mapping_and_count(self):
        self.configure('jellyfin', {'url': 'http://jellyfin', 'user_id': 'one', 'profile': 'ana'})
        client = jellyfin_transport(player_fixture('jellyfin'))
        def transport(req, timeout):
            if urllib.parse.urlsplit(req.full_url).path == '/Users':
                return Reply([{'Id': 'one', 'Name': 'PrivateOne'}, {'Id': 'two', 'Name': 'PrivateTwo'}])
            return client(req, timeout)
        result = self.probe('jellyfin', transport)
        self.assertEqual(result['unmapped'], 1)
        self.assertTrue(result['auth_ok'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['plays_30d'], 3)
        self.assertNotIn('PrivateTwo', json.dumps(result))
        self.assertIn('PrivateTwo', self.probe('jellyfin', transport, show=True)['unmapped_names'])
        self.assertGreater(result['plays_30d'], 0)

    def test_jellyfin_all_users_map_without_extending_other_clients(self):
        self.configure('jellyfin', {'url': 'http://jellyfin', 'users': {'one': 'ana', 'two': 'household'}})
        cfg = self.s.validate_config(self.s.CONFIG)
        self.s.CONFIG = cfg
        client = jellyfin_transport(player_fixture('jellyfin'))
        def transport(req, timeout):
            if urllib.parse.urlsplit(req.full_url).path == '/Users':
                return Reply([{'Id': 'one', 'Name': 'One'}, {'Id': 'two', 'Name': 'Two'}])
            return client(req, timeout)
        self.assertTrue(self.probe('jellyfin', transport)['ok'])

    def test_plex_probe(self):
        self.configure('plex', {'url': 'http://plex', 'profile': 'household'})
        client = plex_transport(player_fixture('plex'))
        def transport(req, timeout):
            if urllib.parse.urlsplit(req.full_url).path == '/myplex/account':
                return Reply({'MyPlex': {'username': 'PrivateAccount'}})
            return client(req, timeout)
        result = self.probe('plex', transport)
        self.assertTrue(result['ok'])
        self.assertEqual(result['unmapped'], 0)
        self.assertEqual(result['plays_30d'], 3)

    def test_sqlite_probe_counts_unmapped_and_does_not_modify_db(self):
        options = watch_db(self.data/'aio.db')
        options['archived'] = {}
        self.configure('aiostreams_db', options)
        raw = (self.data/'aio.db').read_bytes()
        result = self.probe('aiostreams_db')
        self.assertEqual(result['unmapped'], 1)
        self.assertEqual((self.data/'aio.db').read_bytes(), raw)
        from skald import connectors
        self.assertEqual(connectors.test(self.s, 'aiostreams-db', now=NOW)['connectors'][0]['name'], 'aiostreams_db')

    def test_trakt_probe_expired_auth_never_refreshes_or_writes(self):
        self.configure('trakt_api', {})
        pages = trakt_pages()
        def transport(req, timeout):
            self.assertIsNone(req.data)
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), \
                patch.object(self.s, '_read_trakt_auth', return_value={'access_token': 'fake', 'refresh_token': 'fake',
                    'created_at': 0, 'expires_in': 1}), \
                patch.object(self.s, '_connector_write', side_effect=AssertionError('probe write')), \
                patch.object(self.s, '_renew_trakt_auth', side_effect=AssertionError('probe refresh')):
            result = self.probe('trakt_api', transport)
        self.assertTrue(result['ok'])
        self.assertGreater(result['plays_30d'], 0)

    def test_failed_auth_is_nonzero_json_without_exception_details(self):
        from skald import setup
        self.configure('jellyfin', {'url': 'http://jellyfin', 'user_id': 'one'})
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': ''}), \
                patch.object(setup, 'server') as server:
            server.return_value.__enter__.return_value = self.s
            self.s._connector_open = lambda *a, **kw: (_ for _ in ()).throw(ValueError('secret-canary'))
            out = io.StringIO()
            self.assertEqual(main(['connectors', 'test', 'jellyfin', '--json'], out=out), 1)
        self.assertNotIn('secret-canary', out.getvalue())
        members.configure()


class ConnectorSetupTests(unittest.TestCase):
    def test_setup_bad_user_response_has_one_line_fix(self):
        from contextlib import contextmanager
        from skald import setup
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data/'skald.json').write_text(json.dumps(members.fresh_config(['alex', 'sam'])))
            original = setup.server
            @contextmanager
            def fake_server(path):
                with original(path) as s, patch.object(s, '_connector_open', return_value=Reply({})):
                    yield s
            with patch.dict(os.environ, {'DATA_DIR': folder, 'SKALD_SNAPSHOT': '', 'TMDB_API': 'fake', 'FAKE_KEY': 'secret-canary'}), \
                    patch.object(setup, 'server', fake_server), patch.object(setup, 'check_tmdb', return_value=True):
                out = io.StringIO()
                code = main(['setup', '--yes', '--base', 'https://tv.example', '--connect', 'jellyfin',
                    '--source-url', 'http://jellyfin', '--auth-env', 'FAKE_KEY', '--json'], out=out)
            self.assertEqual(code, 2)
            self.assertIn('check', json.loads(out.getvalue())['error'].lower())
            self.assertNotIn('secret-canary', out.getvalue())
        members.configure()

    def test_agent_setup_sources_private_credentials_and_mappings(self):
        from contextlib import contextmanager
        from skald import setup
        for cid in ('jellyfin', 'plex', 'trakt', 'aiostreams-db'):
            with self.subTest(cid=cid), tempfile.TemporaryDirectory() as folder:
                data = Path(folder)
                cfg = members.fresh_config(['alex', 'sam'])
                (data/'skald.json').write_text(json.dumps(cfg))
                (data/'setup.json').write_text(json.dumps({'base': 'https://tv.example', 'watch': 'stremio', 'taste': 'files'}))
                flags = ['--person', 'alex', '--secret-stdin']
                http = None
                if cid == 'jellyfin':
                    flags += ['--source-url', 'http://jellyfin', '--map', 'one=alex', '--map', 'two=together']
                    client = jellyfin_transport(player_fixture('jellyfin'))
                    def http(req, timeout):
                        return Reply([{'Id': 'one', 'Name': 'One'}, {'Id': 'two', 'Name': 'Two'}]) if urllib.parse.urlsplit(req.full_url).path == '/Users' else client(req, timeout)
                elif cid == 'plex':
                    flags += ['--source-url', 'http://plex']
                    client = plex_transport(player_fixture('plex'))
                    def http(req, timeout):
                        return Reply({'MyPlex': {'username': 'SyntheticAccount'}}) if urllib.parse.urlsplit(req.full_url).path == '/myplex/account' else client(req, timeout)
                elif cid == 'trakt':
                    pages = trakt_pages()
                    def http(req, timeout):
                        return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
                else:
                    watch_db(data/'aio.db')
                    flags = ['--source-path', str(data/'aio.db'), '--map', 'a1b2c3d4=together',
                             '--map', 'b1b2c3d4=alex', '--map', 'c1b2c3d4=sam']
                original = setup.server
                @contextmanager
                def fake_server(path):
                    with original(path) as s, patch.object(s, '_connector_open', http or s._connector_open):
                        yield s
                with patch.dict(os.environ, {'DATA_DIR': folder, 'SKALD_SNAPSHOT': '', 'TMDB_API': 'fake'}), \
                        patch.object(setup, 'server', fake_server), patch.object(setup, 'check_tmdb', return_value=True), \
                        patch('sys.stdin', io.StringIO('secret-canary\nclient-canary\n')):
                    out = io.StringIO()
                    self.assertEqual(main(['setup', '--yes', '--connect', cid, '--json', *flags], out=out), 0, out.getvalue())
                    before = json.loads((data/'skald.json').read_text())
                    if cid == 'plex':
                        before['connectors']['plex']['options']['section'] = 'movies'
                    if cid == 'trakt':
                        previous = before['connectors']['trakt_api']
                        previous['accounts'] = {'alex': previous['auth']}
                        previous['auth'] = None
                    (data/'skald.json').write_text(json.dumps(before))
                    self.assertEqual(main(['setup', '--yes', '--connect', cid, '--json'], out=io.StringIO()), 0)
                    self.assertEqual(json.loads((data/'skald.json').read_text()), before)
                    if cid == 'trakt':
                        second = io.StringIO()
                        with patch.dict(os.environ, {'SECOND_TRAKT': 'second-secret-canary'}):
                            self.assertEqual(main(['setup', '--yes', '--connect', 'trakt', '--person', 'sam',
                                '--auth-env', 'SECOND_TRAKT', '--json'], out=second), 0)
                        accounts = json.loads((data/'skald.json').read_text())['connectors']['trakt_api']['accounts']
                        self.assertEqual(set(accounts), {'alex', 'sam'})
                        self.assertEqual(accounts['alex'], before['connectors']['trakt_api']['accounts']['alex'])
                        self.assertTrue(accounts['sam'].startswith('file:'))
                        self.assertNotIn('second-secret-canary', second.getvalue())
                self.assertNotIn('secret-canary', out.getvalue())
                saved = json.loads((data/'skald.json').read_text())
                self.assertIn(cid.replace('aiostreams-db', 'aiostreams_db').replace('trakt', 'trakt_api'), saved['connectors'])
                if (data/'credentials').exists():
                    self.assertEqual((data/'credentials').stat().st_mode & 0o777, 0o700)
                    for credential in (data/'credentials').iterdir():
                        self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
        members.configure()
