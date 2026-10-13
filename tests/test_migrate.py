"""Format gates and migration order, using synthetic DATA only."""
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

from skald import members, migrate
from skald.__main__ import main


class Migrations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data/'legacy.json').write_text(json.dumps(members.LEGACY))
        (self.data/'history.json').write_text('{"entries": []}\n')

    def marker(self, version, release='previous'):
        raw = json.dumps({'format': version, 'release': release})
        (self.data/'.skald-format').write_text(raw)
        return raw

    def test_baseline_and_atomic_marker(self):
        self.assertEqual(migrate.check(self.data)['format'], 1)
        migrate.ensure(self.data)
        self.assertEqual(json.loads((self.data/'.skald-format').read_text()),
                         {'format': migrate.FORMAT, 'release': members.VERSION})
        self.assertEqual((self.data/'.skald-format').stat().st_mode & 0o777, 0o600)

    def test_external_config_migrates_without_publishing_local_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'household.json'
            cfg = members.fresh_config(['ana', 'ben'])
            cfg['metrics'] = {'enabled': True}
            config.write_text(json.dumps(cfg))
            before = config.read_bytes()
            (self.data / '.skald-household').touch()
            self.marker(2)
            with patch.dict(os.environ, {'SKALD_CONFIG': str(config)}, clear=True), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                self.assertEqual(migrate.ensure(self.data)['format'], migrate.FORMAT)
                self.assertEqual(migrate.ensure(self.data)['format'], migrate.FORMAT)
            self.assertTrue((self.data / 'metrics.json').exists())
            self.assertFalse((self.data / 'skald.json').exists())
            self.assertEqual(config.read_bytes(), before)

    def test_second_process_waits_and_skips_completed_migration(self):
        self.marker(1)
        script = textwrap.dedent('''\
            import sys
            from pathlib import Path
            from unittest.mock import patch
            from skald import backup, migrate
            data, role = Path(sys.argv[1]), sys.argv[2]
            create = backup.create
            def archive(*args, **kwargs):
                with (data/'calls').open('a') as out:
                    out.write('backup\\n')
                return create(*args, **kwargs)
            def step(staged):
                with (data/'calls').open('a') as out:
                    out.write('step\\n')
                if role == 'first':
                    print('staged', flush=True)
                    sys.stdin.readline()
                (staged/'taste.json').write_text('{}\\n')
            with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: step}), \\
                    patch.object(backup, 'create', archive), \\
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                print('starting', flush=True)
                print(migrate.ensure(data)['format'], flush=True)
            ''')
        env = {k: v for k, v in os.environ.items() if not k.startswith('SKALD_')}
        def launch(role):
            proc = subprocess.Popen([sys.executable, '-c', script, str(self.data), role],
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            def cleanup():
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
            self.addCleanup(cleanup)
            return proc
        first = launch('first')
        self.assertEqual(first.stdout.readline().strip(), 'starting')
        self.assertEqual(first.stdout.readline().strip(), 'staged')
        second = launch('second')
        self.assertEqual(second.stdout.readline().strip(), 'starting')
        with self.assertRaises(subprocess.TimeoutExpired):
            second.wait(timeout=.3)
        self.assertEqual((self.data/'calls').read_text(), 'backup\nstep\n')
        out, err = first.communicate('\n', timeout=10)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(out.strip(), '2')
        out, err = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(out.strip(), '2')
        self.assertEqual((self.data/'calls').read_text(), 'backup\nstep\n')
        self.assertEqual(len(list((self.data/'backups').glob('pre-migrate-*'))), 1)

    def test_migration_lock_timeout_leaves_state_untouched(self):
        import fcntl
        self.marker(1)
        lock = self.data / '.skald-migrate.lock'
        with lock.open('w') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            before = {p.name: p.read_bytes() for p in self.data.iterdir()}
            with patch.object(migrate, 'LOCK_TIMEOUT', .05, create=True), \
                    self.assertRaisesRegex(migrate.FormatError, 'timed out.*migration'):
                migrate.ensure(self.data)
            self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_additive_migrations_leave_read_only_status_unchanged(self):
        self.assertEqual(migrate.MIGRATIONS[2], migrate.settings_v2)
        self.assertEqual(migrate.MIGRATIONS[3], migrate.local_row_metrics)
        self.assertEqual(migrate.MIGRATIONS[4], migrate.settings_history_v4)
        self.assertEqual(migrate.MIGRATIONS[5], migrate.monthly_history)
        for enabled in (None, False, True):
            for version in (1, 2, 3, 4, 5):
                with self.subTest(enabled=enabled, version=version):
                    config = members.fresh_config(['ana', 'ben'])
                    if enabled is not None:
                        config['metrics'] = {'enabled': enabled}
                    (self.data/'skald.json').write_text(json.dumps(config))
                    self.marker(version)
                    before = {p.name: p.read_bytes() for p in self.data.iterdir()}
                    self.assertFalse(migrate.rewrite_needed(self.data))
                    with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True):
                        out = io.StringIO()
                        self.assertIn(main(['setup', '--status', '--json'], out=out), (0, 1))
                        self.assertEqual(json.loads(out.getvalue())['data_format'], version)
                    self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_default_off_metrics_migration_creates_no_metric_files(self):
        config = members.fresh_config(['ana', 'ben'])
        (self.data/'skald.json').write_text(json.dumps(config))
        before = (self.data/'skald.json').read_bytes()
        migrate.MIGRATIONS[3](self.data)
        self.assertFalse((self.data/'metrics.json').exists())
        self.assertFalse((self.data/'metrics_shown.json').exists())
        migrate.ensure(self.data)
        self.assertEqual(migrate.check(self.data)['format'], migrate.FORMAT)
        self.assertFalse((self.data/'metrics.json').exists())
        self.assertEqual((self.data/'skald.json').read_bytes(), before)

    def test_backup_first_order_idempotence_and_verified_archive(self):
        self.marker(1)
        calls = []
        def first(data):
            calls.append('first')
            value = json.loads((data/'history.json').read_text())
            value['migrated'] = True
            (data/'history.json').write_text(json.dumps(value) + '\n')
        def second(data):
            calls.append('second')
            self.assertTrue(json.loads((data/'history.json').read_text())['migrated'])
            (data/'taste.json').write_text('{}\n')
        from skald import backup
        create = backup.create
        def verified_backup(data, **kwargs):
            calls.append('backup')
            self.assertEqual((data/'history.json').read_text(), '{"entries": []}\n')
            archive = create(data, **kwargs)
            self.assertTrue(backup.verify(archive)['ok'])
            return archive
        with patch.object(migrate, 'FORMAT', 3), patch.object(migrate, 'MIGRATIONS', {2: first, 3: second}), \
                patch.object(backup, 'create', side_effect=verified_backup):
            migrate.ensure(self.data)
            self.assertEqual(calls, ['backup', 'first', 'second'])
            first_bytes = (self.data/'history.json').read_bytes()
            migrate.ensure(self.data)
            self.assertEqual(calls, ['backup', 'first', 'second'])
            first(self.data)
            second(self.data)
            self.assertEqual((self.data/'history.json').read_bytes(), first_bytes)
            self.assertEqual(json.loads((self.data/'.skald-format').read_text())['format'], 3)

    def test_failure_leaves_data_and_format_untouched(self):
        raw = self.marker(1)
        before = (self.data/'history.json').read_bytes()
        def fail(data):
            (data/'history.json').write_text('changed')
            (data/'new.json').write_text('{}')
            raise ValueError('migration failed')
        with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: fail}):
            with self.assertRaisesRegex(ValueError, 'migration failed'):
                migrate.ensure(self.data)
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertFalse((self.data/'new.json').exists())
        self.assertEqual((self.data/'.skald-format').read_text(), raw)

    def test_marker_write_failure_rolls_back_files(self):
        raw = self.marker(1)
        before = (self.data/'history.json').read_bytes()
        def change(data):
            (data/'history.json').write_text('changed')
        with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: change}), \
                patch('skald.intake.write_private', side_effect=OSError('write failed')):
            with self.assertRaisesRegex(OSError, 'write failed'):
                migrate.ensure(self.data)
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertEqual((self.data/'.skald-format').read_text(), raw)

    def test_publication_failure_rolls_back_changed_new_and_deleted_files(self):
        raw = self.marker(1)
        before = (self.data/'history.json').read_bytes()
        legacy = (self.data/'legacy.json').read_bytes()
        def change(data):
            (data/'history.json').write_text('changed')
            (data/'taste.json').write_text('{}')
            (data/'legacy.json').unlink()
        replace = os.replace
        def fail_marker(source, dest):
            if Path(dest) == self.data/'.skald-format':
                raise OSError('publish failed')
            return replace(source, dest)
        with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: change}), \
                patch('os.replace', side_effect=fail_marker):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                migrate.ensure(self.data)
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertEqual((self.data/'legacy.json').read_bytes(), legacy)
        self.assertFalse((self.data/'taste.json').exists())
        self.assertEqual((self.data/'.skald-format').read_text(), raw)

    def test_local_cli_migrates_before_status(self):
        self.marker(1)
        def change(data):
            (data/'taste.json').write_text('{}')
        with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: change}), \
                patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True):
            out = io.StringIO()
            main(['setup', '--status', '--json'], out=out)
            self.assertEqual(json.loads(out.getvalue())['data_format'], 2)
        self.assertTrue((self.data/'taste.json').exists())

    def test_newer_refuses_before_reading_household(self):
        raw = self.marker(migrate.FORMAT + 1, '9.0.0')
        (self.data/'legacy.json').write_text('not JSON')
        with self.assertRaisesRegex(ValueError, rf'9.0.0.*format {migrate.FORMAT + 1}.*upgrade Skald or restore a backup'):
            migrate.ensure(self.data)
        self.assertEqual((self.data/'.skald-format').read_text(), raw)
        self.assertFalse((self.data/'backups').exists())
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True):
            out = io.StringIO()
            self.assertEqual(main(['setup', '--status', '--json'], out=out), 2)
            self.assertIn('upgrade Skald or restore a backup', json.loads(out.getvalue())['error'])

        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'server.py')],
                                env={**os.environ, 'DATA_DIR': str(self.data)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(f'9.0.0 (format {migrate.FORMAT + 1}); upgrade Skald or restore a backup', result.stderr)

    def test_setup_status_release_and_format(self):
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True):
            for json_output in (False, True):
                out = io.StringIO()
                main(['setup', '--status', *(['--json'] if json_output else [])], out=out)
                if json_output:
                    result = json.loads(out.getvalue())
                    self.assertEqual(result['release'], members.VERSION)
                    self.assertEqual(result['data_format'], 1)
                else:
                    self.assertIn('Release: ' + members.VERSION, out.getvalue())
                    self.assertIn('Data format: 1', out.getvalue())
