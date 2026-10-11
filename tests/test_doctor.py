"""Read-only findings, narrow repairs and metadata-only remote checks."""
import builtins
import json
import os
from pathlib import Path
import stat
import time
import unittest
from unittest.mock import patch

from skald.engine import Engine
from test_onboard_wizard import FrontDoorCase, SNAPSHOT, ANA


class DoctorTests(FrontDoorCase):
    def test_corrupt_state_finding_and_onboard_hint(self):
        self.state.mkdir()
        for name, content in [('onboard.json', '{'), ('onboard.json', '{"v":1,"people":{"ana":{"plain":{}}}}'),
                              ('review.json', '{'), ('review.json', '[{}]')]:
            with self.subTest(name=name, content=content):
                path = self.state / name
                path.write_text(content)
                report, _ = self.cli('doctor', code=1)
                self.assertFalse(report['ok'])
                self.assertIn('state.' + name.split('.')[0], [f['checkId'] for f in report['findings'] if f['severity'] == 'error'])
                result, _ = self.cli('onboard', '--person', 'ana', '--source', f'plain={self.export}', '--dry-run', '--non-interactive', code=2)
                self.assertIn('run: skald doctor', result['error'])
                self.assertEqual(path.read_text(), content)
                path.unlink()
        self.stage()
        path = self.state / 'onboard.json'
        book = json.loads(path.read_text())
        for field, value in [('events', [{}]), ('receipts', {}), ('plan', [])]:
            broken = json.loads(json.dumps(book))
            broken['people']['ana']['plain'][field] = value
            path.write_text(json.dumps(broken))
            self.cli('doctor', code=1)
            result, _ = self.cli('onboard', '--person', 'ana', '--source', f'plain={self.export}', '--dry-run', '--non-interactive', code=2)
            self.assertIn('run: skald doctor', result['error'])

    def test_fix_creates_state_and_repairs_only_modes(self):
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        self.cli('doctor', '--fix', code=1)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        file = self.state / 'review.json'
        file.write_text('[]')
        file.chmod(0o644)
        self.state.chmod(0o755)
        self.cli('doctor', '--fix', code=1)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(file.read_text(), '[]')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})
        self.cli('doctor', '--fix', code=1)

    def test_remote_json_is_never_opened_or_fixed_in_snapshot(self):
        secret = self.data / 'remote.json'
        secret.write_text('SYNTHETIC_SECRET_NOT_TO_BE_OPENED')
        secret.chmod(0o644)
        path_open, builtin_open = Path.open, builtins.open
        def guard_path(path, *args, **kw):
            self.assertNotEqual(path.name, 'remote.json')
            return path_open(path, *args, **kw)
        def guard_builtin(file, *args, **kw):
            if isinstance(file, (str, os.PathLike)):
                self.assertNotEqual(Path(file).name, 'remote.json')
            return builtin_open(file, *args, **kw)
        with patch.object(Path, 'open', guard_path), patch('builtins.open', guard_builtin):
            result, _ = self.cli('doctor', '--fix', code=1)
        finding = next(f for f in result['findings'] if f['checkId'] == 'remote.configured')
        self.assertEqual(finding['severity'], 'warning')
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o644)

    def test_unpushed_partial_review_and_memory_counts(self):
        self.stage()
        result, _ = self.cli('doctor', code=1)
        self.assertIn('1 partial sources', [f['message'] for f in result['findings']])
        self.apply()
        result, _ = self.cli('doctor', code=1)
        self.assertIn('1 unpushed imports', [f['message'] for f in result['findings']])
        self.assertIn('0 pending memory proposals', [f['message'] for f in result['findings']])

    def test_exit_rules_and_legacy_keys(self):
        self.state.mkdir(mode=0o700)
        def ready(*args, **kw):
            engine = Engine(*args, **kw)
            engine._model['nexp'].update(ana=20, ben=20)
            return engine
        with patch('skald.guide.Engine', side_effect=ready):
            result, _ = self.cli('doctor')
        self.assertTrue(result['ok'])
        self.assertEqual(result['v'], 1)
        self.assertEqual(set(result), {'v', 'ok', 'checksRun', 'findings'})
        with patch('skald.guide.Engine', side_effect=RuntimeError('PRIVATE_INTERNAL_CONTENT')):
            result, _ = self.cli('doctor', code=2)
        self.assertNotIn('PRIVATE_INTERNAL_CONTENT', json.dumps(result))
        self.assertEqual(result['findings'][0]['checkId'], 'doctor.internal')
        self.assertIn('records', self.cli('doctor', 'keys')[0])

    def test_live_requires_explicit_remote_opt_in(self):
        with patch('skald.live.pull', side_effect=AssertionError('no implicit pull')):
            result, _ = self.cli('--live', 'doctor', code=1)
        self.assertIn('data.readable', [f['checkId'] for f in result['findings']])
        with patch('skald.live.pull', return_value=self.data) as pull:
            self.cli('--live', 'doctor', '--remote', code=1)
        pull.assert_called_once()
        self.assertNotEqual(pull.call_args.args[0], self.state / 'live')

    @unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), 'set SKALD_SNAPSHOT to run snapshot gates')
    def test_snapshot_findings_and_timing(self):
        started = time.perf_counter()
        result, _ = self.cli('doctor', data=SNAPSHOT, code=1)
        self.assertLess(time.perf_counter() - started, 2)
        findings = {f['checkId']: f for f in result['findings']}
        self.assertEqual(findings['facets.mixed_keys']['severity'], 'warning')
        self.assertEqual(findings['calibration.no_bg']['message'], 'no % offline')
        self.assertEqual(findings['maturity.' + ANA]['severity'], 'warning')
        self.assertIn('skald interview --person ' + ANA, findings['maturity.' + ANA]['fixHint'])


if __name__ == '__main__':
    unittest.main()
