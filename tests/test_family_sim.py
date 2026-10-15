"""The owner demo runs real CLI reads and refuses private cross-person reads."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FamilySimTests(unittest.TestCase):
    def test_simulator_calendar_refusals_and_read_only_preview(self):
        start = time.monotonic()
        proc = subprocess.run([sys.executable, '-B', 'eval/family_sim.py', '--json'], cwd=ROOT,
                              text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stderr, '')
        self.assertLess(time.monotonic()-start, 60)
        rows = json.loads(proc.stdout)
        by_command = {r['command']: r for r in rows}
        around = json.loads(by_command['plays around kim --person ana']['output'])
        self.assertTrue(around['around'])
        self.assertIn({'start': '2026-09-08', 'end': '2026-09-17', 'days': 10}, around['stretches'])
        self.assertIn({'start': '2026-09-18', 'end': '2026-09-26', 'days': 9}, around['gaps'])
        self.assertEqual(around['days_28'], 8)
        self.assertEqual(around['last_day'], '2026-10-11')
        taste = json.loads(by_command['taste align kim --person ana']['output'])
        self.assertEqual(taste['person'], 'kim')
        self.assertTrue(taste['drivers'])
        for op in ('plays recent', 'plays around', 'taste align'):
            for actor, target in (('lena', 'kim'), ('kim', 'kim' if op == 'plays around' else 'ana'),
                                  ('ben', 'kim'), ('ana', 'ben')):
                self.assertEqual(by_command[f'{op} {target} --person {actor}']['exit'], 2)
        preview = json.loads(rows[-1]['output'])
        self.assertEqual(rows[-1]['command'], 'members forget kim --person ana')
        self.assertEqual(preview['forget']['counts']['watching_days'], 15)
        self.assertNotIn('applied', preview)

    def test_localized_text_and_cleanup(self):
        from eval import family_sim
        from unittest.mock import patch
        seen = []
        original = family_sim.tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            temp = original(*args, **kwargs)
            seen.append(Path(temp.name))
            return temp
        with patch.object(family_sim.tempfile, 'TemporaryDirectory', temporary):
            rows = family_sim.simulate('de', as_json=False)
        self.assertTrue(seen)
        self.assertTrue(all(not p.exists() for p in seen))
        self.assertIn('Kim', rows[1]['output'])
        self.assertNotIn('last 7 days', rows[1]['output'])
