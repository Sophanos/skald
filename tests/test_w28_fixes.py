"""Bounded W28 review regressions on synthetic DATA."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skald import backup, members, migrate, setup
from skald.engine import Engine
from skald.__main__ import main
from eval import rows_report, online_report, replay
from eval.profile_golden import FIXTURE, ROOT


class W28Fixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)/'data'
        self.data.mkdir(mode=0o700)
        (self.data/'legacy.json').write_text(json.dumps(members.LEGACY))
        (self.data/'.skald-format').write_text(json.dumps({'format': 1, 'release': members.VERSION}))

    def test_omitted_destination_refused_before_migration(self):
        outside = Path(self.temp.name)/'original.json'
        outside.write_text('original bytes')
        (self.data/'taste.json').symlink_to(outside)
        originals = {p.name: p.read_bytes() for p in self.data.iterdir()}
        def change(data):
            (data/'taste.json').write_text('replacement')
        replace = os.replace
        def fail_marker(source, dest):
            if Path(dest) == self.data/'.skald-format':
                raise OSError('marker publication failed')
            return replace(source, dest)
        with patch.object(migrate, 'FORMAT', 2), patch.object(migrate, 'MIGRATIONS', {2: change}), patch('os.replace', side_effect=fail_marker):
            with self.assertRaisesRegex(ValueError, 'not in.*backup'):
                migrate.ensure(self.data)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()
                          if p.is_file() and p.name != migrate.LOCK}, originals)
        self.assertTrue((self.data/'taste.json').is_symlink())
        self.assertEqual(outside.read_text(), 'original bytes')

    def test_archive_restore_ignores_newer_live_data(self):
        archive = backup.create(self.data)
        (self.data/'.skald-format').write_text('{"format": 99, "release": "future"}')
        target = Path(self.temp.name)/'restored'
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True):
            out = io.StringIO()
            self.assertEqual(main(['backup', 'restore', str(archive), '--to', str(target), '--json'], out=out), 0, out.getvalue())
        self.assertTrue((target/'legacy.json').is_file())

    def test_migration_archives_are_unique_and_keep_daily(self):
        with setup.server(self.data) as s:
            s.backup_state(1791734400.)
            daily = next((self.data/'backups').glob('state-*.tar.gz'))
            original = daily.read_bytes()
        with patch('time.time', return_value=1791734400.):
            first, second = backup.create(self.data), backup.create(self.data)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(daily.read_bytes(), original)
        self.assertTrue(backup.verify(first)['ok'])

    def test_backup_title_loads_snapshot_person_before_parse(self):
        from skald import __main__ as cli
        (self.data/'skald.json').write_text(json.dumps(members.fresh_config(['alex'])))
        with patch.object(cli, 'Engine'), patch.object(cli, 'execute', return_value={}) as execute, \
                patch.object(cli, 'persist'), patch.object(cli, 'text_output'):
            self.assertEqual(main(['--snapshot', str(self.data), 'taste', 'why', 'backup', '--person', 'alex'], out=io.StringIO()), 0)
        args = execute.call_args.args[0]
        self.assertEqual(args.person, 'alex')

    def test_pre_migration_list_and_separate_retention(self):
        root = self.data/'backups'
        root.mkdir()
        daily = root/'state-2026-10-11.tar.gz'
        daily.write_bytes(b'daily')
        archives = []
        for i in range(5):
            path = root/f'pre-migrate-1-2-{1791734400000000000+i}-{i:032x}.tar.gz'
            path.write_bytes(b'migration')
            archives.append(path)
        listing = backup.list_backups(self.data)
        rows = [r for r in listing['archives'] if r.get('kind') == 'pre-migration']
        self.assertEqual([r['name'] for r in rows], [p.name for p in reversed(archives)])
        self.assertEqual([r['keep'] for r in rows], ['pre-migration']*3 + ['prune']*2)
        self.assertEqual(sum('pre-migration' in line for line in backup.text(listing)), 5)
        with setup.server(self.data) as s:
            s.backup_prune(str(root), '2026-10-11')
            self.assertEqual([p.exists() for p in archives], [False, False, True, True, True])
            self.assertEqual(daily.read_bytes(), b'daily')
            s.backup_prune(str(root), '2027-10-11')
            self.assertTrue(all(p.exists() for p in archives[2:]))

    def test_engine_copies_member_history_before_migration(self):
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'auth': None,
            'accounts': {'ana': 'file:ana.token'}, 'options': {'client_id': 'env:FAKE_CLIENT'}}
        (self.data/'skald.json').write_text(json.dumps(cfg))
        (self.data/'trakt-ana.history.json').write_text('{"items": []}')
        def ensure(data):
            self.assertTrue((data/'trakt-ana.history.json').is_file())
            raise ValueError('copied first')
        with patch.object(migrate, 'ensure', side_effect=ensure):
            with self.assertRaisesRegex(ValueError, 'copied first'):
                Engine(self.data)

    def test_nondefault_fixture_rows_map_player_order(self):
        shutil.copytree(FIXTURE/'data', self.data, dirs_exist_ok=True)
        cfg = json.loads((self.data/'legacy.json').read_text())
        cfg['row_prefix'] = 'test'
        cfg['household']['row_order'] = []
        (self.data/'legacy.json').write_text(json.dumps(cfg))
        order = Path(self.temp.name)/'order.mjs'
        with setup.server(self.data) as s:
            requests = [(cid, typ) for cid in s.DEDUPE_ORDER if s.row_enabled(cid) and cid not in s.READY_ROWS for typ in
                        (('movie', 'series') if cid in (s.rid('top10'), s.rid('toprated')) else ('series',) if cid in (s.rid('episodes'), s.rid('airing'), s.rid('nextseason')) else ('tv',))]
            order.write_text('const ORDER = [' + ','.join(repr('player.' + cid.split('.', 1)[1] + ':' + typ) for cid, typ in requests) + '];')
            with patch.object(s, 'row_metas', return_value=[]):
                rows = rows_report.display_rows(s, order=order)
            self.assertEqual([(r['id'], r['type']) for r in rows], requests)
            shared = {'id': 'tmdb:1', 'type': 'movie'}
            for row in rows:
                if row['id'] in (s.rid('foryou'), s.rid('rewatch')):
                    row['metas'] = [shared]
            from test_rows import SnapshotRows
            check = SnapshotRows('test_display_order_and_first_six_dedupe')
            check.s, check.rows = s, rows
            with self.assertRaises(AssertionError):
                check.test_display_order_and_first_six_dedupe()
            from test_rows_r2 import SnapshotR2
            check = SnapshotR2('test_snapshot_window_rule')
            check.s, check.rows = s, rows
            with patch('test_rows_r2.display_rows', return_value=rows):
                with self.assertRaises(AssertionError):
                    check.test_snapshot_window_rule()
                for row in rows:
                    if row['id'] == s.rid('rewatch'):
                        row['metas'] = []
                    elif row['id'] == s.rid('top10') and row['type'] == 'movie':
                        row['metas'] = [shared]
                    ids = [x['id'] for x in row['metas']]
                    self.assertEqual(len(ids), len(set(ids)), row['id'])
                check.test_snapshot_window_rule()

    def test_copy_helpers_keep_format_gate(self):
        output = Path(self.temp.name)/'output'
        self.assertFalse(self.data.is_relative_to(output))
        (self.data/'.skald-format').write_text('{"format": 99, "release": "future"}')
        for name in rows_report.FILES:
            (self.data/name).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'format 99'):
            online_report.online_report(self.data, '2026-10-11T18:00:00')
        with patch.object(replay, 'workspace_output', side_effect=lambda p: Path(p)), self.assertRaisesRegex(ValueError, 'format 99'):
            with replay.offline_server(self.data, output):
                pass

    def test_decision_skill_loads_server_by_path(self):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH' and not k.startswith('SKALD_SNAPSHOT')}
        env['DATA_DIR'] = str(self.data)
        result = subprocess.run([sys.executable, 'eval/decisions.py', '--skill'], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
