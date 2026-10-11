from contextlib import redirect_stdout
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from eval import decisions


class SkillDocTests(unittest.TestCase):
    def saved(self):
        text = (decisions.ROOT / "skills/skald-taste/SKILL.md").read_text()
        self.assertEqual(text.count(decisions.BEGIN), 1)
        self.assertEqual(text.count(decisions.END), 1)
        return text[text.index(decisions.BEGIN):text.index(decisions.END) + len(decisions.END)]

    def assert_current(self):
        self.assertEqual(self.saved(), decisions.skill_block())

    def test_current_deterministic_synthetic_block(self):
        self.assert_current()
        block = decisions.skill_block()
        self.assertEqual(block, decisions.skill_block())
        self.assertLessEqual(len(block.splitlines()), 120)
        keys = re.findall(r"tmdb:([0-9]+)", block)
        self.assertTrue(keys)
        self.assertTrue(all(re.fullmatch(r"9000[0-9]{2}", k) for k in keys))
        self.assertNotIn("Dune", block)
        self.assertNotIn("Castle", block)
        self.assertNotIn("mood_pick", block)

    def test_schema_drift_fails(self):
        with tempfile.TemporaryDirectory() as data:
            s = load_server(data)
            try:
                out = s.DECISIONS["explain_pick"]["out"]
                with patch.dict(out, {"rung": ("seed", "facet", "memory", None)}), \
                        patch.object(decisions, "load_server", return_value=s), patch.object(decisions, "close_server"):
                    with self.assertRaises(AssertionError):
                        self.assert_current()
            finally:
                close_server(s)

    def test_examples_never_use_recorded_rows(self):
        with tempfile.TemporaryDirectory() as data:
            s = load_server(data)
            try:
                path = Path(data, "rows.jsonl")
                path.write_text('{"in":{"query":"RecordedTextSentinel"},"note":"Recorded"}\n'
                                '{"in":{"query":"Synthetic text sentinel"},"note":"Synthetic: safe"}\n')
                with patch.dict(s.DECISIONS["route_query"], {"fixtures": str(path)}), \
                        patch.object(decisions, "load_server", return_value=s), patch.object(decisions, "close_server"):
                    block = decisions.skill_block()
                    self.assertNotIn("RecordedTextSentinel", block)
                    self.assertIn("Synthetic text sentinel", block)
            finally:
                close_server(s)

    def test_write_only_replaces_marked_block(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "skills/skald-taste/SKILL.md")
            path.parent.mkdir(parents=True)
            original = "frontmatter and intake\n\n" + decisions.BEGIN + "\nold\n" + decisions.END + "\nfooter\n"
            path.write_text(original)
            with patch.object(decisions, "ROOT", Path(root)), patch.object(decisions, "skill_block", return_value=decisions.BEGIN + "\nnew\n" + decisions.END):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(decisions.main(["--skill", "--write"]), 0)
            self.assertEqual(path.read_text(), original.replace("\nold\n", "\nnew\n"))
