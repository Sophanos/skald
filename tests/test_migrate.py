"""Format gates and migration order, using synthetic DATA only."""
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
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
                         {'format': 1, 'release': members.VERSION})
        self.assertEqual((self.data/'.skald-format').stat().st_mode & 0o777, 0o600)

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
        raw = self.marker(2, '9.0.0')
        (self.data/'legacy.json').write_text('not JSON')
        with self.assertRaisesRegex(ValueError, '9.0.0.*format 2.*upgrade Skald or restore a backup'):
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
        self.assertIn('9.0.0 (format 2); upgrade Skald or restore a backup', result.stderr)

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
