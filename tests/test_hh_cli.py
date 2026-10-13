"""Owner CLI contract on synthetic inputs and copied public source trees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from skald.members import fresh_config, rid

ROOT = Path(__file__).resolve().parents[1]
VERDICTS = {'BETTER', 'NON-INFERIOR', 'REGRESSION', 'INCONCLUSIVE',
            'INSUFFICIENT', 'HARD FAIL'}

class HouseholdCliTests(unittest.TestCase):
    def test_synthetic_freeze_compare_and_share(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'synthetic'
            data.mkdir()
            config = fresh_config(['ana', 'ben'])
            config['region']['tz'] = 'UTC'
            config['household']['rows'] = ['moods']
            metas = [{'id': f'tmdb:{i}', 'name': f'HH_PRIVATE_TITLE_SENTINEL {chr(65 + i % 26)} Card',
                      'type': 'movie', 'poster': 'p', 'background': 'b', '_row': {'g': ['Drama'], 'rating': 8,
                                               'votes': 1000, 'date': '2020-01-01'}}
                     for i in range(100, 114)]
            inputs = {'skald.json': config, 'history.json': {'items': []},
                      'facets.json': {'items': {m['id']: {'kind': 'movie', 'g': ['Drama'],
                                                        'x': {'feel_good': 2}} for m in metas}},
                      'emb.json': {'dim': 256, 'v': {}, 'kinds': {}},
                      'feeds.json': {rid('mood1', config) + '|en-US': {'metas': {'tv': metas}, 'names': {'mood': 'cozy'}}}}
            for name, value in inputs.items():
                (data / name).write_text(json.dumps(value))
            record = {'media_kind': 'movie', 'provider_id': 'tmdb:100', 'member': 'ana',
                      'action': 'played', 'source': 'synthetic', 'reliable': True,
                      'occurred_at': int(time.time()) - 18 * 86400,
                      'observed_at': int(time.time()) - 18 * 86400,
                      'timestamp_precision': 'second'}
            (data / 'playback.jsonl').write_text('\n'.join(
                json.dumps({**record, 'provider_id': m['id'], 'member': member})
                for member in ('ana', 'ben') for m in metas) + '\n')
            before = {p.name: p.read_bytes() for p in data.iterdir()}
            for build in ('a', 'b'):
                for source in [ROOT / 'server.py', *sorted((ROOT / 'skald').rglob('*.py'))]:
                    target = root / build / source.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            candidate = root / 'b' / 'server.py'
            candidate.write_text(candidate.read_text().replace('ROW_MIN = 6', 'ROW_MIN = 15'))
            def cli(*args, code=0):
                proc = subprocess.run([sys.executable, '-m', 'eval.household', *map(str, args)],
                                      cwd=ROOT, text=True, capture_output=True, timeout=60,
                                      env={'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'})
                self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                self.assertEqual(proc.stderr, '')
                self.assertNotIn('HH_PRIVATE_TITLE_SENTINEL', proc.stdout)
                self.assertNotIn(temp, proc.stdout)
                return proc.stdout

            cli('freeze', '--data', data, '--out', root / 'frozen')
            for other, output in (('a', 'aa'), ('b', 'ab')):
                text = cli('compare', '--snapshot', root / 'frozen', '--a', root / 'a',
                           '--b', root / other, '--out', root / output, code=2)
                report = json.loads((root / output / 'report.json').read_text())
                self.assertTrue(report['aa_identical'])
                self.assertEqual(report['home']['support']['targets'], 14)
                self.assertEqual(report['recovery'] > 0, other == 'a')
                self.assertEqual(report['ab_identical'], other == 'a')
                self.assertIn(report['verdict'], VERDICTS)
                self.assertIn(report['verdict'], text)
                self.assertEqual(report['evidence_level'], 'diagnostic')
                self.assertFalse(report['release_pass'])
                self.assertEqual((root / output / 'report.json').stat().st_mode & 0o777, 0o600)
                cli('check-share', root / output / 'share.json')
                share = (root / output / 'share.json').read_text()
                self.assertNotIn('HH_PRIVATE_TITLE_SENTINEL', share)
                versions = json.loads(share)['versions']
                self.assertEqual(versions['build_a'] == versions['build_b'], other == 'a')
            self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir()})
            cli('check-share', data / 'history.json', code=1)
            cli('check-share', 'HH_PRIVATE_TITLE_SENTINEL', code=1)
            cli('compare', '--snapshot', root / 'missing', '--a', root / 'a',
                '--b', root / 'b', '--out', root / 'bad', code=1)
            cli('UNKNOWN_PRIVATE_ARGUMENT', code=1)
