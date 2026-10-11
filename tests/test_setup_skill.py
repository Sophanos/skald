from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupSkillTests(unittest.TestCase):
    def test_setup_loop_and_reviewed_push(self):
        text = (ROOT/'skills/skald-taste/SKILL.md').read_text()
        self.assertIn('Set up Skald for a household', text)
        self.assertIn('skald setup --status', text)
        self.assertIn('printed next command', text)
        self.assertIn('dry-run', text)
        self.assertNotIn('Never run `push`', text)
        self.assertNotIn('Help Ben or Ana', text)
