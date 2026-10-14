"""One complete initial config, using only synthetic SQLite and HTTP inputs."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from history_kit import watch_db
from test_setup_aiostreams import FakeAIO
from skald import connectors, members, setup
from skald.__main__ import main


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root/'data'
        self.data.mkdir(mode=0o700)
        self.db = root/'aio.db'
        watch_db(self.db)
        self.uid = 'a1b2c3d4-1111-4111-8111-111111111111'
        with sqlite3.connect(self.db) as con:
            con.execute('update watch_state set uuid=? where uuid=?', (self.uid, 'a1b2c3d4-device'))
            con.executescript('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by TEXT);')
            con.execute('insert into settings (key,value) values (?,?)', ('api.configAccessKey', json.dumps('PRIVATE-ACCESS')))
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'fake'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(members.configure)
        self.fake = FakeAIO()
        self.fake.protected = True
        self.fake.configs[self.uid] = {'presets': [], 'upstreamCatalogOrder': []}
        self.flags = ['setup', 'household', '--member', 'sam', '--lang', 'de-DE', '--timezone', 'Europe/Berlin',
                      '--country', 'DE', '--aio-login', 'a1b2c3d4', '--source-path', str(self.db),
                      '--internal-base', 'http://skald:8080', '--aio-url', 'http://aio:3000', '--password-stdin']

    def run_setup(self, *extra, ready=True):
        out = io.StringIO()
        def acknowledge(data, cfg, base):
            self.assertEqual(json.loads((data/'skald.json').read_text()), cfg)
            self.assertIn('aiostreams_db', cfg['connectors'])
            if not ready:
                raise ValueError('Engine not ready; retry with skald setup aiostreams')
        with patch.object(setup, 'check_tmdb', return_value=True), \
                patch.object(setup, 'wait_household_ready', side_effect=acknowledge), \
                patch('urllib.request.OpenerDirector.open', side_effect=self.fake), \
                patch('sys.stdin', io.StringIO('PRIVATE-PASSWORD\n')), \
                patch('builtins.input', side_effect=AssertionError('unexpected prompt')), redirect_stderr(out):
            code = main([*self.flags, *extra], out=out)
        for value in ('PRIVATE-PASSWORD', 'PRIVATE-ACCESS', '/stremio/', 'manifest.json', self.uid):
            self.assertNotIn(value, out.getvalue())
        return code, out.getvalue()

    def test_complete_single_publication_and_private_credentials(self):
        before = self.db.read_bytes()
        with patch.object(setup, 'publish', wraps=setup.publish) as publish:
            code, output = self.run_setup('--rows', 'foryou,week')
        self.assertEqual(code, 0, output)
        self.assertEqual(publish.call_count, 1)
        cfg = json.loads((self.data/'skald.json').read_text())
        self.assertEqual(cfg['household']['members'], ['sam'])
        self.assertEqual(cfg['household']['rows'], ['foryou', 'week'])
        opts = cfg['connectors']['aiostreams_db']['options']
        self.assertEqual(opts['profiles'], {'a1b2c3d4': 'sam'})
        self.assertEqual(opts['archived'], {'b1b2c3d4': 'household', 'c1b2c3d4': 'household'})
        self.assertEqual(self.db.read_bytes(), before)
        creds = self.data/'credentials/aiostreams.json'
        self.assertEqual(creds.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(creds.read_text())['sam']['uuid'], self.uid)
        self.assertTrue(any(c[2] and c[2]['config']['accessKey'] == 'PRIVATE-ACCESS' for c in self.fake.calls if c[0] == 'PUT'))

    def test_row_language_is_part_of_initial_household_config(self):
        self.flags[self.flags.index('--member') + 1] = 'ana'
        code, output = self.run_setup('--row-lang', 'en-US')
        self.assertEqual(code, 0, output)
        region = json.loads((self.data/'skald.json').read_text())['region']
        self.assertEqual(region['lang'], 'de-DE')
        self.assertEqual(region.get('row_lang'), 'en-US')

    def test_defaults_and_configured_refusal(self):
        self.assertEqual(self.run_setup()[0], 0)
        before = (self.data/'skald.json').read_bytes()
        self.assertNotIn('rows', json.loads(before)['household'])
        code, output = self.run_setup()
        self.assertNotEqual(code, 0)
        self.assertIn('existing setup commands', output)
        self.assertEqual((self.data/'skald.json').read_bytes(), before)

    def test_failure_keeps_retryable_progress_and_hides_raw_exception(self):
        self.fake.failure = ('USER_INVALID_CONFIG', 'fetch manifest PRIVATE-PASSWORD', 400)
        code, output = self.run_setup()
        self.assertNotEqual(code, 0)
        self.assertIn('skald setup aiostreams', output)
        self.assertTrue((self.data/'skald.json').exists())
        self.assertTrue((self.data/'credentials/aiostreams.json').exists())
        self.assertNotIn('POST', [c[0] for c in self.fake.calls])

    def test_readiness_failure_never_calls_aio(self):
        self.assertNotEqual(self.run_setup(ready=False)[0], 0)
        self.assertEqual(self.fake.calls, [])

    def test_collision_and_invalid_rows_do_not_publish(self):
        with sqlite3.connect(self.db) as con:
            con.execute('update watch_state set uuid=? where uuid=?', ('a1b2c3d4-2222-4222-8222-222222222222', 'b1b2c3d4-device'))
        code, output = self.run_setup()
        self.assertNotEqual(code, 0)
        self.assertIn('ambiguous', output)
        self.assertFalse((self.data/'skald.json').exists())
        with sqlite3.connect(self.db) as con:
            con.execute('delete from watch_state where uuid != ?', (self.uid,))
        self.assertNotEqual(self.run_setup('--rows', 'invalid')[0], 0)
        self.assertFalse((self.data/'skald.json').exists())

    def test_settings_schema_and_private_no_history_uuid(self):
        self.assertEqual(connectors.aiostreams_access_key(str(self.db)), 'PRIVATE-ACCESS')
        with sqlite3.connect(self.db) as con:
            con.execute('delete from watch_state where uuid=?', (self.uid,))
        with patch.object(setup, 'secret', side_effect=[self.uid, 'PRIVATE-PASSWORD']):
            self.assertEqual(self.run_setup('--uuid-stdin')[0], 0)
        with sqlite3.connect(self.db) as con:
            con.execute('update settings set value=?', ('PRIVATE-INVALID',))
        with self.assertRaisesRegex(ValueError, 'settings'):
            connectors.aiostreams_access_key(str(self.db))

    def test_retry_uses_saved_password_and_hides_secret_exception(self):
        self.fake.failure = ('USER_INVALID_DETAILS', 'PRIVATE-PASSWORD', 401)
        self.assertNotEqual(self.run_setup()[0], 0)
        self.fake.failure = None
        out = io.StringIO()
        with patch.object(setup, 'wait_household_ready'), \
                patch('urllib.request.OpenerDirector.open', side_effect=self.fake), \
                patch.object(setup, 'secret', side_effect=AssertionError('password requested again')):
            self.assertEqual(main(['setup', 'aiostreams'], out=out), 0, out.getvalue())
        for value in ('PRIVATE-PASSWORD', 'PRIVATE-ACCESS', self.uid, '/stremio/'):
            self.assertNotIn(value, out.getvalue())
        with patch.object(setup, 'wait_household_ready'), \
                patch('urllib.request.OpenerDirector.open', side_effect=RuntimeError('PRIVATE-PASSWORD PRIVATE-ACCESS')):
            self.assertNotEqual(main(['setup', 'aiostreams'], out=out), 0)
        self.assertNotIn('PRIVATE-PASSWORD', out.getvalue())
        self.assertNotIn('PRIVATE-ACCESS', out.getvalue())

    def test_full_prefix_still_rejects_eight_digit_collision(self):
        with sqlite3.connect(self.db) as con:
            con.execute('update watch_state set uuid=? where uuid=?', ('a1b2c3d4-2222-4222-8222-222222222222', 'b1b2c3d4-device'))
        with self.assertRaisesRegex(ValueError, 'eight-character'):
            connectors.aiostreams_login(str(self.db), self.uid)
        with self.assertRaisesRegex(ValueError, 'no history'):
            connectors.aiostreams_login(str(self.db), 'ffffffff')

    def test_ready_requires_config_acknowledgement_and_health(self):
        from history_kit import Reply
        cfg = members.validate_config(members.fresh_config(['sam']))
        transport = unittest.mock.Mock(return_value=Reply({'ok': True}))
        with self.assertRaisesRegex(ValueError, 'Engine not ready'):
            setup.wait_household_ready(self.data, cfg, 'http://skald:8080', timeout=0, transport=transport)
        transport.assert_not_called()
        setup.record_startup(self.data, cfg)
        setup.wait_household_ready(self.data, cfg, 'http://skald:8080', timeout=0, transport=transport)
        self.assertEqual(transport.call_args.args[0].full_url, 'http://skald:8080/health')
        with self.assertRaisesRegex(ValueError, 'Engine not ready'):
            setup.wait_household_ready(self.data, cfg, 'http://skald:8080', timeout=0,
                                       transport=lambda *a, **kw: Reply({'ok': False}))

    def test_saved_addresses_and_missing_settings(self):
        setup.write_private(self.data/'setup.json', json.dumps({'internal_base': 'http://skald:8080', 'aio_url': 'http://aio:3000'}))
        self.flags = self.flags[:-5] + ['--password-stdin']
        self.assertEqual(self.run_setup()[0], 0)
        with sqlite3.connect(self.db) as con:
            con.execute('drop table settings')
        with self.assertRaisesRegex(ValueError, 'settings schema'):
            connectors.aiostreams_access_key(str(self.db))

    def test_internal_address_success_has_complete_status(self):
        self.assertEqual(self.run_setup()[0], 0)
        cfg = json.loads((self.data/'skald.json').read_text())
        setup.record_startup(self.data, cfg)
        out = io.StringIO()
        self.assertEqual(main(['setup', '--status', '--json'], out=out), 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertNotIn('address', result['missing'])
        self.assertEqual(result['next'], 'nothing left')
        self.assertEqual(setup.state(self.data)['base'], 'http://skald:8080')

    def test_failed_aio_status_stays_pending_until_successful_retry(self):
        address_flag = self.flags.index('--internal-base')
        self.flags[address_flag:address_flag + 2] = ['--base', 'https://skald.example.org']
        self.fake.failure = ('USER_INVALID_DETAILS', 'PRIVATE-PASSWORD', 401)
        self.assertNotEqual(self.run_setup()[0], 0)
        cfg = json.loads((self.data/'skald.json').read_text())
        setup.record_startup(self.data, cfg)
        self.assertTrue((self.data/'credentials/aiostreams.json').exists())
        out = io.StringIO()
        self.assertNotEqual(main(['setup', '--status', '--json'], out=out), 0)
        result = json.loads(out.getvalue())
        self.assertIn('aiostreams', result['missing'])
        self.assertEqual(result['next'], 'skald setup aiostreams')
        with patch.object(setup, 'wait_household_ready'), \
                patch('urllib.request.OpenerDirector.open', side_effect=self.fake), \
                patch.object(setup, 'secret', side_effect=AssertionError('password requested again')):
            self.assertNotEqual(main(['setup', 'aiostreams'], out=io.StringIO()), 0)
            self.assertFalse(setup.status(self.data)['ok'])
            self.fake.failure = None
            self.assertEqual(main(['setup', 'aiostreams'], out=io.StringIO()), 0)
        result = setup.status(self.data)
        self.assertTrue(result['ok'])
        self.assertNotIn('aiostreams', result['missing'])
        self.assertEqual(result['next'], 'nothing left')
