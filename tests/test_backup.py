"""Streamed backups, local restore boundaries and synthetic secret plants."""
from decide_kit import write_legacy
from contextlib import redirect_stdout
import datetime
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from skald import backup
from skald.__main__ import main
from eval import profile_golden

ROOT = Path(__file__).resolve().parents[1]
PY_NOW = datetime.datetime(2026, 10, 11, 6, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('backup_server', ROOT / 'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': ''}):
            spec.loader.exec_module(cls.s)
        for name in ('_pool', '_outer', '_nzb_pool'):
            cls.addClassCleanup(getattr(cls.s, name).shutdown)

    def setUp(self):
        timezone = patch.object(self.s, 'TZ', 'Europe/Berlin')
        timezone.start()
        self.addCleanup(timezone.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        self.s._daily = None
        for p in (patch.object(self.s, 'DATA', self.temp.name),
                  patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))):
            p.start()
            self.addCleanup(p.stop)

    def archive(self, now=PY_NOW):
        out = self.s.backup_state(now)
        return self.data / 'backups' / f'state-{self.s.online_day(now)[0]}.tar.gz', out

    def seed(self):
        for name in self.s.BACKUP_FILES:
            if name in ('skald.json', '.skald-household'):
                continue
            (self.data / name).write_bytes(json.dumps(self.s.LEGACY).encode() if name == 'legacy.json' else b'{"synthetic":1}\n' if name.endswith('.jsonl') else b'{"synthetic":1}')


class BackupTests(ServerCase):
    def test_pre_migration_archive_keeps_shelves_health_and_disconnected_history(self):
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['household']['shelves'] = 7
        (self.data / 'skald.json').write_text(json.dumps(cfg))
        (self.data / '.skald-household').touch()
        retained = {'health-settings.json': b'{"enabled":true,"repair":false}\n'}
        for source in ('trakt', 'mdblist', 'stremio'):
            retained[source + '-ana.history.json'] = b'{"items":[{"id":"tmdb:5","type":"movie"}]}\n'
        for name, raw in retained.items():
            (self.data / name).write_bytes(raw)
        excluded = ('credentials/alerts.json', 'token.json', '.env', 'health-state.json',
                    'health-status.json', 'health-cache-feeds.json', 'feeds.json',
                    'trakt-token.history.json', 'mdblist-auth.history.json')
        for name in excluded:
            path = self.data / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'SYNTHETIC_EXCLUSION_CANARY')
        with patch.dict(os.environ, {'SKALD_CONFIG': ''}), patch.object(self.s, 'CONFIG', cfg):
            path = backup.create(self.data, from_format=2, to_format=5)
        report = backup.verify(path)
        self.assertTrue(report['ok'], report)
        for name in retained:
            with self.subTest(archived=name):
                self.assertIn(name, report['files'])
        restored = self.data / 'restored'
        self.assertTrue(backup.restore(path, restored)['ok'])
        restored_cfg, _ = self.s.load_config(str(restored), {}, readonly=True)
        with self.subTest(setting='shelves'):
            self.assertEqual(restored_cfg['household'].get('shelves'), 7)
        for name, raw in retained.items():
            with self.subTest(restored=name):
                self.assertTrue((restored / name).exists())
                self.assertEqual((restored / name).read_bytes(), raw)
        self.assertFalse(set(excluded) & set(report['files']))
        self.assertNotIn(b'SYNTHETIC_EXCLUSION_CANARY', gzip.decompress(path.read_bytes()))

    def test_legacy_config_roundtrip_keeps_mode_and_is_private(self):
        (self.data / 'history.json').write_text('{"items": []}')
        path, _ = self.archive()
        report = backup.verify(path)
        self.assertIn('legacy.json', report['files'])
        target = self.data / 'legacy-restored'
        self.assertTrue(backup.restore(path, target)['ok'])
        self.assertEqual((target / 'legacy.json').read_bytes(), (self.data / 'legacy.json').read_bytes())
        self.assertFalse((target / '.skald-household').exists())
        self.assertEqual(self.s.load_config(str(target), {}, readonly=True)[1], 'legacy')
        self.assertEqual(stat.S_IMODE((target / 'legacy.json').stat().st_mode), 0o600)

    def test_previous_release_member_archive_restores_with_retained_household(self):
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'ana': 'file:trakt-ana.json'}}
        retained = {'skald.json': json.dumps(cfg).encode(), '.skald-household': b''}
        for name, raw in retained.items():
            (self.data / name).write_bytes(raw)
        name = 'trakt-ana.history.json'
        raw = json.dumps({'items': [{'id': 'tmdb:603', 'type': 'movie',
                                    'title': 'Synthetic title', 'person': 'ben'}]}).encode()
        (self.data / name).write_bytes(raw)
        with patch.object(self.s, 'CONFIG', cfg):
            before = self.s.read_trakt()
        self.assertEqual(before[0]['person'], 'ana')
        manifest = {'v': 1, 'day': '2026-10-11', 'created': 0, 'skipped': {},
                    'files': {name: {'size': len(raw), 'mtime': 0,
                                     'sha256': hashlib.sha256(raw).hexdigest()}}}
        path = self.data / 'previous-release.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            archive.add(self.data / name, arcname=name)
            receipt = json.dumps(manifest).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size = len(receipt)
            archive.addfile(info, io.BytesIO(receipt))
        (self.data / name).unlink()
        restored = self.data / 'restored'
        report = backup.restore(path, restored)
        self.assertTrue(report['ok'], report)
        shutil.copyfile(restored / name, self.data / name)
        self.assertEqual((self.data / name).read_bytes(), raw)
        for name, raw in retained.items():
            self.assertEqual((self.data / name).read_bytes(), raw)
        restored_cfg, source = self.s.load_config(str(self.data), {}, readonly=True)
        self.assertEqual(source, 'file')
        with patch.object(self.s, 'CONFIG', restored_cfg):
            self.assertEqual(self.s.read_trakt(), before)

    def test_household_restore_keeps_config_and_stamp_before_state(self):
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['extension'] = {'secret': 'FAKE_CONFIG_SECRET_CANARY'}
        (self.data / 'skald.json').write_text(json.dumps(cfg))
        (self.data / '.skald-household').touch()
        (self.data / 'taste.json').write_text('{"household":{"likes":"synthetic"}}')
        path, _ = self.archive()
        restored = self.data / 'household-restored'
        original_extract = tarfile.TarFile.extractall
        original_fsync, synced = os.fsync, set()
        def durable(fd):
            st = os.fstat(fd)
            for name in ('skald.json', '.skald-household', ''):
                path = restored / name
                if path.exists() and (st.st_dev, st.st_ino) == (path.stat().st_dev, path.stat().st_ino):
                    synced.add(name)
            return original_fsync(fd)
        def publish(ar, target, **kwargs):
            if any(m.name == 'taste.json' for m in kwargs['members']):
                self.assertTrue((Path(target) / '.skald-household').is_file())
                self.assertEqual(synced, {'skald.json', '.skald-household', ''}, 'household must be durable before state')
                actual, source = self.s.load_config(str(target), {}, readonly=True)
                self.assertEqual(source, 'file')
                self.assertEqual(actual['household']['members'], ['ana', 'ben'])
            return original_extract(ar, target, **kwargs)
        with patch.object(tarfile.TarFile, 'extractall', publish), patch('os.fsync', durable):
            self.assertTrue(backup.restore(path, restored)['ok'])
        self.assertEqual((restored / 'taste.json').read_bytes(), (self.data / 'taste.json').read_bytes())
        self.assertNotIn(b'FAKE_CONFIG_SECRET_CANARY', gzip.decompress(path.read_bytes()))

    def test_backup_refuses_config_aliased_to_credentials_before_reading(self):
        cfg = self.s.fresh_config(['ana'])
        credentials = self.data / 'credentials'
        credentials.mkdir(mode=0o700)
        token = credentials / 'fake.json'
        token.write_text(json.dumps(cfg))
        os.link(token, self.data / 'skald.json')
        import builtins
        original = builtins.open
        def opened(path, *args, **kwargs):
            if not isinstance(path, int) and Path(path) == self.data / 'skald.json':
                raise AssertionError('credential alias read as config')
            return original(path, *args, **kwargs)
        with patch('builtins.open', opened), self.assertRaisesRegex(ValueError, 'credential alias'):
            self.archive()

    def test_backups_owner_chmod_and_unwritable_failure(self):
        root = self.data / 'backups'
        root.mkdir()
        original_stat = os.stat
        uid = os.getuid()
        for owner, writable in ((uid, True), (uid + 1, True), (uid + 1, False)):
            def owned(path, *args, **kwargs):
                st = original_stat(path, *args, **kwargs)
                return SimpleNamespace(st_uid=owner, st_mode=st.st_mode) if os.fspath(path) == str(root) else st
            with self.subTest(owner=owner, writable=writable), patch.object(self.s.os, 'stat', side_effect=owned), \
                    patch.object(self.s.os, 'chmod', wraps=os.chmod) as chmod, \
                    patch.object(self.s.os, 'access', return_value=writable) as access:
                if writable:
                    path, result = self.archive()
                    self.assertTrue(path.is_file())
                    self.assertIn('bytes', result)
                else:
                    with self.assertRaisesRegex(PermissionError, f'^backups dir not owned by uid {uid}: fix ownership$'):
                        self.archive()
                if owner == uid:
                    chmod.assert_called_once_with(str(root), 0o700)
                    access.assert_not_called()
                else:
                    chmod.assert_not_called()
                    access.assert_called_once_with(str(root), os.W_OK)

    def test_secret_plants_never_opened(self):
        self.seed()
        plants = ['addon.json', 'remote.json', 'clients.json', 'hydra.json', '.env', 'prod.env',
                  'trakt_auth.json', 'token.json', 'dbg1/hydra.json']
        for name in plants:
            p = self.data / name
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(b'SYNTHETIC_CANARY_NOT_FOR_ARCHIVE')
        for name in ('daily.json', 'feeds.json'):
            (self.data / name).write_bytes(b'{}')
        import builtins
        original_open, original_os_open = builtins.open, os.open
        def check(fn):
            def wrapped(path, *args, **kwargs):
                if not isinstance(path, int):
                    self.assertNotIn(os.fspath(path), [str(self.data / n) for n in plants])
                    self.assertNotEqual(Path(path), self.data, 'must not list or open DATA')
                return fn(path, *args, **kwargs)
            return wrapped
        original_listdir = os.listdir
        def listdir(path):
            self.assertNotEqual(Path(path), self.data)
            return original_listdir(path)
        with patch('builtins.open', check(original_open)), patch('os.open', check(original_os_open)), patch('os.listdir', listdir):
            path, _ = self.archive()
        with tarfile.open(path) as ar:
            self.assertEqual([m.name for m in ar], [*[n for n in self.s.BACKUP_FILES if n not in ("skald.json", ".skald-household")], 'manifest.json'])
        self.assertNotIn(b'SYNTHETIC_CANARY_NOT_FOR_ARCHIVE', gzip.decompress(path.read_bytes()))

    def test_links_and_nonregular(self):
        (self.data / 'remote.json').write_bytes(b'SYNTHETIC_CANARY')
        (self.data / 'taste.json').symlink_to(self.data / 'remote.json')
        (self.data / 'feedback.jsonl').mkdir()
        os.mkfifo(self.data / 'history.json')
        path, _ = self.archive()
        with tarfile.open(path) as ar:
            manifest = json.load(ar.extractfile('manifest.json'))
        self.assertEqual(manifest['skipped']['taste.json'], 'link')
        for name in ('feedback.jsonl', 'history.json'):
            self.assertEqual(manifest['skipped'][name], 'not a regular file')
        self.assertNotIn(b'SYNTHETIC_CANARY', gzip.decompress(path.read_bytes()))

    def test_classification_and_drift(self):
        import re
        from skald.history import SUMMARY, LOCK
        names = {n for n in re.findall(r'os\.path\.join\(\s*\w+\s*,\s*"([^"/]+)"\s*\)',
                                      (ROOT / 'server.py').read_text()) if not n.endswith('.tmp')}
        names.update(re.findall(r"Path\(s.DATA\) / '([^'/]+)'", (ROOT / 'skald/shelves.py').read_text()))
        names.update(re.findall(r"Path\(data\) / '([^'/]+)'", (ROOT / 'skald/ai.py').read_text()))
        names.add('usage.lock')
        names.update(re.findall(r"Path\(s.DATA\)/'([^'/]+)'", (ROOT / 'skald/commands.py').read_text()))
        names.add(SUMMARY)
        names.add(LOCK)
        names.add('health-settings.json')
        names.update(re.findall(r"Path\(data\)/'([^'/]+)'", (ROOT / 'skald/metrics.py').read_text()))
        self.assertEqual(names | {'skald.json', '.skald-format', 'commands.json'}, set(self.s.BACKUP_FILES) | set(self.s.BACKUP_SKIP))
        self.assertFalse(set(self.s.BACKUP_FILES) & set(self.s.BACKUP_SKIP))
        self.assertTrue(all(self.s.BACKUP_SKIP.values()))
        self.assertTrue({'addon.json', 'remote.json', 'clients.json', 'hydra.json'} <= set(self.s.BACKUP_SKIP))
        self.assertFalse(any(self.s.BACKUP_SECRET.search(n) for n in self.s.BACKUP_FILES))
        self.assertEqual(backup.BACKUP_FILES, self.s.BACKUP_FILES)

    def test_manifest_modes_and_hashes(self):
        self.seed()
        old = os.umask(0)
        try:
            path, out = self.archive()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with tarfile.open(path) as ar:
            members = ar.getmembers()
            self.assertEqual(members[-1].name, 'manifest.json')
            manifest = json.load(ar.extractfile(members[-1]))
            self.assertEqual(manifest['v'], 1)
            for m in members:
                self.assertEqual(m.mode, 0o600)
                self.assertEqual((m.type, m.uid, m.gid, m.uname, m.gname), (tarfile.REGTYPE, 0, 0, '', ''))
                if m.name != 'manifest.json':
                    raw = (self.data / m.name).read_bytes()
                    self.assertEqual(manifest['files'][m.name], {'sha256': hashlib.sha256(raw).hexdigest(),
                                                               'size': len(raw), 'mtime': int((self.data / m.name).stat().st_mtime)})
        self.assertEqual(out['files'], len(self.s.BACKUP_FILES) - 2)
        self.assertFalse(self.s.backup_due(PY_NOW))
        self.assertTrue(backup.verify(path)['ok'])

    def test_torn_jsonl_and_block_boundary(self):
        for raw in (b'complete\nhalf', b'x' * 65540 + b'\n' + b'z' * 65540, b'half', b''):
            with self.subTest(size=len(raw)):
                (self.data / 'feedback.jsonl').write_bytes(raw)
                path, _ = self.archive()
                with tarfile.open(path) as ar:
                    self.assertEqual(ar.extractfile('feedback.jsonl').read(), raw[:raw.rfind(b'\n') + 1])

    def test_atomic_reader_and_verifier_failures(self):
        self.seed()
        for where in ('_BackupReader.read', '_backup_verify'):
            target = self.s._BackupReader if where.startswith('_BackupReader') else self.s
            name = where.split('.')[-1]
            with self.subTest(where=where), patch.object(target, name, side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    self.archive()
            self.assertFalse(list((self.data / 'backups').iterdir()))

    def test_short_read_rejected(self):
        self.seed()
        original = self.s._BackupReader.read
        def truncated(reader, size):
            os.ftruncate(write_fd, 0)
            return original(reader, size)
        with open(self.data / 'feedback.jsonl', 'r+b') as src:
            write_fd = src.fileno()
            with patch.object(self.s._BackupReader, 'read', truncated), self.assertRaises(OSError):
                self.archive()
        self.assertFalse(list((self.data / 'backups').iterdir()))

    def test_stale_tmp_and_strays(self):
        root = self.data / 'backups'
        root.mkdir()
        for name in ('.state-old.tmp', '.state-fresh.tmp', 'notes.txt', 'state-2026-01-01.tar.gz.bak'):
            (root / name).touch()
        os.utime(root / '.state-old.tmp', (PY_NOW - 7200,) * 2)
        os.utime(root / '.state-fresh.tmp', (PY_NOW,) * 2)
        with patch.object(self.s.time, 'time', return_value=PY_NOW):
            self.archive()
        self.assertFalse((root / '.state-old.tmp').exists())
        for name in ('.state-fresh.tmp', 'notes.txt', 'state-2026-01-01.tar.gz.bak'):
            self.assertTrue((root / name).exists())

    def test_disk_guard_and_large_file(self):
        self.seed()
        with patch.object(self.s.os, 'statvfs', return_value=SimpleNamespace(f_bavail=1, f_frsize=4096)):
            with self.assertRaisesRegex(OSError, 'low disk'):
                self.archive()
        self.assertFalse(list((self.data / 'backups').iterdir()))
        with open(self.data / 'emb.json', 'wb') as fh:
            fh.truncate(64 * 1024 * 1024 + 1)
        path, _ = self.archive()
        with tarfile.open(path) as ar:
            self.assertEqual(json.load(ar.extractfile('manifest.json'))['skipped']['emb.json'], 'too large')

    def test_secret_runtime_guard(self):
        with patch.object(self.s, 'BACKUP_FILES', ('token.json',)):
            path, _ = self.archive()
        with tarfile.open(path) as ar:
            self.assertEqual(json.load(ar.extractfile('manifest.json'))['skipped'], {'token.json': 'secret-like name'})

    def test_retention_100_days_and_gap(self):
        start = datetime.date(2026, 1, 1)
        days = [(start + datetime.timedelta(days=i)).isoformat() for i in range(100)]
        for source in (days, days[:35] + days[45:]):
            keep = self.s.backup_retain(source)
            self.assertEqual(len(keep), 22)
            self.assertTrue(set(source[-14:]) <= keep)
            self.assertEqual(keep, backup.retain(source))
            older = sorted(set(source) - set(source[-14:]))
            weeks = {}
            for day in older:
                weeks.setdefault(datetime.date.fromisoformat(day).isocalendar()[:2], day)
            self.assertEqual(keep - set(source[-14:]), set(list(weeks.values())[-8:]))

    def test_retention_40_daily_runs(self):
        start = datetime.date(2026, 9, 2)
        days = []
        (self.data / 'taste.json').write_text('{}')
        root = self.data / 'backups'
        root.mkdir()
        (root / 'notes.txt').write_text('synthetic')
        for i in range(40):
            day = start + datetime.timedelta(days=i)
            now = datetime.datetime.combine(day, datetime.time(6), ZoneInfo('Europe/Berlin')).timestamp()
            days.append(day.isoformat())
            self.s.daily_jobs(now)
            self.s.daily_jobs(now + 60)
            self.assertTrue(self.s._daily['backup']['ok'])
        kept = {m[1] for p in root.iterdir() if (m := self.s._BACKUP_NAME.fullmatch(p.name))}
        self.assertEqual(kept, self.s.backup_retain(days))
        self.assertTrue((root / 'notes.txt').exists())
        rows = backup.list_backups(self.data)['archives']
        self.assertEqual(sum(r['keep'] == 'daily' for r in rows), 14)
        self.assertEqual(sum(r['keep'] == 'weekly' for r in rows), 4)

    def test_fixture_roundtrip_and_golden(self):
        fixture = ROOT / 'tests/fixtures/events/data'
        for name in self.s.BACKUP_FILES:
            if (fixture / name).is_file():
                shutil.copyfile(fixture / name, self.data / name)
        path, _ = self.archive()
        restored = self.data / 'restored'
        self.assertTrue(backup.restore(path, restored)['ok'])
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o700)
        for src in fixture.iterdir():
            self.assertEqual(src.read_bytes(), (restored / src.name).read_bytes())
            self.assertEqual(stat.S_IMODE((restored / src.name).stat().st_mode), 0o600)
        def profile(data):
            with profile_golden.offline_server(ROOT / 'server.py', data=data) as s, redirect_stdout(io.StringIO()):
                return profile_golden.dump(s.build_profile(s.read_history(), ['q1', 'q2'], s.read_trakt(), remote=True))
        self.assertEqual(profile(fixture), profile(restored))
        golden = json.loads((fixture.parent / 'golden.json').read_text())['household']
        self.assertEqual(profile(restored), profile_golden.dump(golden))

    def crafted(self, members, manifest=True, bad_hash=False, household=False):
        path = self.data / 'crafted.tar.gz'
        records = {}
        with tarfile.open(path, 'w:gz') as ar:
            for name, kind, size in members:
                info = tarfile.TarInfo(name)
                info.type, info.size = kind, size
                info.linkname = 'taste.json' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
                ar.addfile(info, io.BytesIO(b'x' * size) if kind == tarfile.REGTYPE else None)
                records[name] = {'size': size, 'sha256': ('0' * 64 if bad_hash else hashlib.sha256(b'x' * size).hexdigest()), 'mtime': 0}
            if manifest:
                raw = json.dumps({'v': 1, 'day': '2026-10-11', 'created': 0, 'files': records, 'skipped': {},
                                  **({'household': True} if household else {})}).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size = len(raw)
                ar.addfile(info, io.BytesIO(raw))
        return path

    def test_household_archive_without_config_and_stamp_is_refused(self):
        path = self.crafted([('taste.json', tarfile.REGTYPE, 1)], household=True)
        target = self.data / 'missing-household'
        report = backup.restore(path, target)
        self.assertFalse(report['ok'])
        self.assertIn('household archive needs skald.json and .skald-household', report['problems'])
        self.assertFalse(target.exists())

    def test_restore_refuses_hostile_archives(self):
        cases = [[(n, tarfile.REGTYPE, 1)] for n in ('/x', '../x', 'a/b', 'remote.json')]
        cases += [[('taste.json', kind, 0)] for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE)]
        cases += [[('taste.json', tarfile.REGTYPE, 1)] * 2]
        for members in cases:
            with self.subTest(members=members):
                path = self.crafted(members)
                self.assertFalse(backup.restore(path, self.data / 'refused')['ok'])
                self.assertFalse((self.data / 'refused').exists())
        for options in ({'manifest': False}, {'bad_hash': True}):
            path = self.crafted([('taste.json', tarfile.REGTYPE, 1)], **options)
            self.assertFalse(backup.restore(path, self.data / 'refused')['ok'])
            self.assertFalse((self.data / 'refused').exists())
        path = self.data / 'oversize.tar.gz'
        with gzip.open(path, 'wb') as ar:
            info = tarfile.TarInfo('taste.json')
            info.size = 64 * 1024 * 1024 + 1
            ar.write(info.tobuf())  # Only a header: the size guard must fire before reading.
        self.assertFalse(backup.restore(path, self.data / 'refused')['ok'])
        self.assertFalse((self.data / 'refused').exists())

    def test_archive_limits_and_invalid_receipts(self):
        path = self.crafted([('taste.json', tarfile.REGTYPE, 1)])
        with patch('skald.backup.os.fstat', return_value=SimpleNamespace(st_size=256 * 1024 * 1024 + 1)):
            self.assertFalse(backup.verify(path)['ok'])
        info = tarfile.TarInfo('taste.json')
        info.size = 1
        with self.assertRaises(ValueError):
            backup._member(info, set(), 512 * 1024 * 1024)
        for value in ([], None, {}, {'v': 1, 'created': 0, 'day': '2026-99-99'},
                      {'v': 1, 'created': 0, 'day': '2026-10-11', 'files': {'taste.json': []}, 'skipped': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backup._manifest(value, {'taste.json': {'size': 1, 'sha256': '0' * 64}})
        path.write_bytes(b'not gzip')
        with self.assertRaisesRegex(ValueError, '^not a valid backup archive$'):
            backup.verify(path)

    def test_manifest_last_and_json_errors(self):
        path = self.crafted([])
        with tarfile.open(path, 'r:gz') as ar:
            manifest = ar.extractfile('manifest.json').read()
        for raw in (manifest, b'bad json'):
            with tarfile.open(path, 'w:gz') as ar:
                info = tarfile.TarInfo('manifest.json')
                info.size = len(raw)
                ar.addfile(info, io.BytesIO(raw))
                if raw == manifest:
                    ar.addfile(tarfile.TarInfo('taste.json'), io.BytesIO())
            if raw == manifest:
                self.assertFalse(backup.verify(path)['ok'])
            else:
                with self.assertRaisesRegex(ValueError, '^not a valid backup archive$'):
                    backup.verify(path)

        path = self.crafted([])
        with patch('skald.backup.json.loads', side_effect=RecursionError), self.assertRaisesRegex(ValueError, '^not a valid backup archive$'):
            backup.verify(path)

    def test_destination_and_dry_run(self):
        self.seed()
        path, _ = self.archive()
        nonempty = self.data / 'nonempty'
        nonempty.mkdir()
        (nonempty / 'notes.txt').touch()
        link = self.data / 'link'
        link.symlink_to(self.data / 'absent')
        for target in (nonempty, link, Path('/data') / 'skald-restore', self.data / 'live' / 'child', self.data / 'snapshot' / 'child'):
            with patch.dict(os.environ, {'DATA_DIR': str(self.data / 'live'), 'SKALD_SNAPSHOT': str(self.data / 'snapshot')}), self.assertRaises(ValueError):
                backup.restore(path, target)
        with self.assertRaises(ValueError):
            backup.restore(path, self.data / 'explicit' / 'child', snapshot=self.data / 'explicit')
        with patch.dict(os.environ, {'DATA_DIR': str(self.data / 'equal')}), self.assertRaises(ValueError):
            backup.restore(path, self.data / 'equal')
        dry = self.data / 'dry'
        self.assertTrue(backup.restore(path, dry, True)['dry_run'])
        self.assertFalse(dry.exists())
        empty = self.data / 'empty'
        empty.mkdir(mode=0o777)
        self.assertTrue(backup.restore(path, empty)['ok'])
        self.assertEqual(stat.S_IMODE(empty.stat().st_mode), 0o700)

    def test_restore_rechecks_second_pass_and_hashes(self):
        self.seed()
        path, _ = self.archive()
        original = backup._verified
        def swap(p):
            result = original(p)
            shutil.copyfile(self.crafted([('../escape', tarfile.REGTYPE, 1)]), path)
            return result
        with patch('skald.backup._verified', swap), self.assertRaises(ValueError):
            backup.restore(path, self.data / 'swap')
        self.assertFalse((self.data / 'escape').exists())
        path, _ = self.archive()
        original_extract = tarfile.TarFile.extractall
        def corrupt(ar, target, **kwargs):
            original_extract(ar, target, **kwargs)
            (Path(target) / 'taste.json').write_bytes(b'synthetic corruption')
        with patch.object(tarfile.TarFile, 'extractall', corrupt), self.assertRaisesRegex(ValueError, 'restored file hash'):
            backup.restore(path, self.data / 'corrupt')
        self.assertTrue((self.data / 'corrupt').exists())

    def test_cli_without_engine_or_snapshot(self):
        self.seed()
        path, _ = self.archive()
        commands = [['backup', 'list', '--data', str(self.data)], ['backup', 'verify', str(path)],
                    ['backup', 'restore', str(path), '--to', str(self.data / 'cli')]]
        with patch('skald.__main__.Engine', side_effect=AssertionError('must not create Engine')), \
                patch('skald.__main__.live.pull', side_effect=AssertionError('must not ssh')):
            for args in commands:
                out = io.StringIO()
                self.assertEqual(main(['--json', *args], out=out), 0)
                json.loads(out.getvalue())
            out = io.StringIO()
            self.assertEqual(main(['--json', '--live', *commands[0]], out=out), 2)
            self.assertEqual(json.loads(out.getvalue()), {'error': 'backup reads local archives only'})
            missing = str(self.data / 'missing-secret-path')
            out = io.StringIO()
            self.assertEqual(main(['--json', 'backup', 'verify', missing], out=out), 2)
            self.assertNotIn(missing, out.getvalue())
        bad = self.crafted([('../x', tarfile.REGTYPE, 1)])
        self.assertEqual(main(['--json', 'backup', 'verify', str(bad)], out=io.StringIO()), 1)
        out = io.StringIO()
        self.assertEqual(main(commands[0], out=out), 0)
        self.assertIn('daily', out.getvalue())

    @unittest.skipUnless(os.environ.get('SKALD_SNAPSHOT'), 'needs owner snapshot')
    def test_snapshot_budget(self):
        snapshot = Path(os.environ['SKALD_SNAPSHOT'])
        for name in self.s.BACKUP_FILES:
            if (snapshot / name).is_file():
                shutil.copyfile(snapshot / name, self.data / name)
        tracemalloc.start()
        start = time.perf_counter()
        try:
            path, out = self.archive()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 3 * 1024 * 1024)
        self.assertLess((time.perf_counter() - start) * 1000, 10000)
        self.assertGreater(out['raw'], 0)
        self.assertTrue(backup.verify(path)['ok'])
