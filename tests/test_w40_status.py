"""Status and next-step regressions on synthetic household data only."""
from contextlib import redirect_stderr
import errno
import io
import json
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from skald import backup, guide, members, migrate, setup
from skald.__main__ import main


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.data.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'offline'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(members.configure)
        for target in ('socket.create_connection', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def legacy(self):
        (self.data / 'legacy.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        entries = [dict(key=f'tmdb:{i}', id=i, kind='movie', stype='movie', title=f'Synthetic {i}',
                        year='2020', signal='liked', weight=2, age=0, who='remote', by=[person],
                        flags=[], genres=[]) for i, person in enumerate(['ana'] * 4 + ['ben'] * 25, 1)]
        (self.data / 'profile.json').write_text(json.dumps({'entries': entries}))

    def test_completed_status_does_not_request_restart(self):
        (self.data / 'skald.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        (self.data / 'setup.json').write_text(json.dumps(dict(tmdb_checked=True, base='https://tv.example',
                                                           watch='stremio', taste='files')))
        # The server loaded the same saved household after startup.
        with setup.server(self.data) as s:
            self.assertEqual(s.CONFIG, members.load_config(str(self.data), {}, readonly=True)[0])
            setup.record_startup(self.data, s.CONFIG)
            out = io.StringIO()
            self.assertEqual(main(['setup', '--status'], out=out), 0)
        self.assertIn('Next: nothing left\n', out.getvalue())
        self.assertNotIn('Next: restart', out.getvalue())

    def test_setup_status_abbreviation(self):
        self.legacy()
        out = io.StringIO()
        self.assertEqual(main(['setup', '--stat'], out=out), 0)
        self.assertIn('Next:', out.getvalue())

    def test_startup_continues_when_marker_write_fails(self):
        (self.data / 'skald.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        tail = (Path(__file__).resolve().parents[1] / 'server.py').read_text().split('if __name__ == "__main__":', 1)[1]
        code = compile(tail.replace('\n    ', '\n'), 'server-main', 'exec')
        with setup.server(self.data) as s:
            for number in (errno.EROFS, errno.ENOSPC):
                with self.subTest(errno=number):
                    error = OSError(number, os.strerror(number))
                    err = io.StringIO()
                    namespace = {**s.__dict__, 'addon_token': Mock(), 'remote_token': Mock(),
                                 'load_state': Mock(), 'ThreadingHTTPServer': MagicMock()}
                    with patch.object(sys, 'argv', ['server.py']), patch.object(s.threading, 'Thread'), \
                            patch.object(setup, 'write_private', side_effect=error) as write, redirect_stderr(err):
                        exec(code, namespace)
                    write.assert_called_once_with(self.data / '.skald-startup', setup.startup_digest(s.CONFIG) + '\n')
                    namespace['ThreadingHTTPServer'].return_value.__enter__.return_value.serve_forever.assert_called_once()
                    self.assertEqual(err.getvalue(), f'could not record startup marker: {error}; setup --status may ask for a restart\n')

    def test_saved_config_requires_restart_until_loaded(self):
        cfg = members.fresh_config(['ana', 'ben'])
        (self.data / 'skald.json').write_text(json.dumps(cfg))
        (self.data / 'setup.json').write_text(json.dumps(dict(tmdb_checked=True, base='https://tv.example',
                                                           watch='stremio', taste='files')))
        restart = 'restart Skald (docker compose restart engine)'
        self.assertEqual(setup.status(self.data)['next'], restart)
        with setup.server(self.data) as s:
            self.assertFalse((self.data / '.skald-startup').exists())
            setup.record_startup(self.data, s.CONFIG)
            self.assertEqual(setup.status(self.data)['next'], 'nothing left')
            cfg['region']['lang'] = 'de-DE'
            (self.data / 'skald.json').write_text(json.dumps(cfg))
            self.assertEqual(setup.status(self.data)['next'], restart)
            self.assertEqual(s.LANG, 'en-US')
        with setup.server(self.data) as s:
            setup.record_startup(self.data, s.CONFIG)
        self.assertEqual(setup.status(self.data)['next'], 'nothing left')
        # Setup progress is not a server setting and must not trigger a restart.
        saved = setup.state(self.data)
        saved['base'] = 'https://other.example'
        (self.data / 'setup.json').write_text(json.dumps(saved))
        self.assertEqual(setup.status(self.data)['next'], 'nothing left')

    def test_legacy_status_reports_observed_taste_without_writes(self):
        self.legacy()
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        out = io.StringIO()
        self.assertEqual(main(['setup', '--status', '--json'], out=out), 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertTrue(result['legacy_readonly'])
        self.assertEqual(result['missing'], [])
        self.assertIn('taste', result['done'])
        self.assertEqual(result['notes'], ['TMDB key present (not checked)',
            'address: not recorded; run skald setup to record', 'watch: not recorded; run skald setup to record',
            'taste: Ana: 4 rated; Ben: 25 rated'])
        text = io.StringIO()
        self.assertEqual(main(['setup', '--status'], out=text), 0)
        for note in result['notes']:
            self.assertIn('Note: ' + note + '\n', text.getvalue())
        with patch.dict(os.environ, {'TMDB_API': ''}):
            self.assertFalse(setup.status(self.data)['ok'])
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_archive_dispatch_accepts_abbreviated_global_options(self):
        self.legacy()
        archive = backup.create(self.data)
        (self.data / '.skald-format').write_text(json.dumps({'format': migrate.FORMAT + 1, 'release': 'future'}))
        for options in (['--state-d', str(Path(self.temp.name) / 'state')], ['--snap', str(self.data)],
                        ['--state-d=' + str(Path(self.temp.name) / 'state')]):
            with self.subTest(options=options):
                out = io.StringIO()
                self.assertEqual(main([*options, 'backup', 'verify', str(archive), '--json'], out=out), 0, out.getvalue())
                self.assertTrue(json.loads(out.getvalue())['ok'])

    def test_doctor_offers_import_alongside_interview(self):
        self.legacy()
        args = SimpleNamespace(snapshot=str(self.data), live=False, remote=False, fix=False)
        with tempfile.TemporaryDirectory() as state:
            result = guide.doctor(args, Path(state))
        line = next(f['message'] for f in result['findings'] if f['checkId'] == 'maturity.ana')
        self.assertEqual(line, 'Ana: too new — 4 of 20 rated; next: skald interview --person ana '
                              'or import a Netflix or IMDb export (skald import --help)')
