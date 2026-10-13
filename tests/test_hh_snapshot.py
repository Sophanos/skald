"""Synthetic local snapshot intake and provenance contracts."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from eval.household.protocol import DIAGNOSTIC


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        global snapshot
        from eval.household import snapshot
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def write(self, name, value):
        path = self.source / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def freeze(self, inputs, **kwargs):
        return snapshot.freeze(self.source, self.root / "frozen", inputs=inputs,
                               generation=kwargs.pop("generation", lambda: 1),
                               enabled=True, **kwargs)

    def test_snapshot_generation_change_rejected(self):
        history = self.write("history.json", {"items": []})
        with self.assertRaisesRegex(ValueError, "generation"):
            self.freeze(["history.json"], generation=iter((1, 2)).__next__)
        self.assertFalse((self.root / "frozen").exists())
        calls = []
        def changing_file():
            calls.append(1)
            if len(calls) == 2:
                history.write_text('{"items": [{}]}', encoding="utf-8")
            return 1
        with self.assertRaises(ValueError):
            self.freeze(["history.json"], generation=changing_file)
        self.assertFalse((self.root / "frozen").exists())

    def test_secret_or_link_not_opened(self):
        for name in ("addon.json", "remote.json", "credentials", ".env", "../history.json"):
            with patch.object(snapshot.os, "open") as opened, self.assertRaises(ValueError):
                self.freeze([name])
            opened.assert_not_called()
        with patch.object(snapshot.os, "open") as opened, self.assertRaises(ValueError):
            snapshot.evaluate(self.source / "remote.json")
        opened.assert_not_called()
        link = self.source / "history.json"
        link.symlink_to(self.root / "absent")
        with patch.object(snapshot.os, "open", wraps=os.open) as opened:
            with self.assertRaises(ValueError):
                self.freeze(["history.json"])
            self.assertFalse(any(call.args[0] == "history.json" for call in opened.call_args_list))
        link.unlink()
        config = {"household": {"members": ["ana"], "member_rows": ["ana"],
                               "rows": ["foryou"], "auth": "synthetic"},
                  "taste": {"people": {"ana": {"prefs": {"discovery": "gems",
                              "runtime": {"movie": 90, "auth": "synthetic"},
                              "path": "/synthetic/external"}, "token": "synthetic"}}},
                  "region": {"tz": "UTC", "lang": "en-US", "country": "France"},
                  "connectors": {"file": {"path": "/synthetic/external"}}, "auth": "synthetic"}
        self.write("skald.json", config)
        self.write("taste.json", {"people": {"ana": {"prefs": {"discovery": "gems"}}},
                                  "auth": "synthetic"})
        target = self.freeze(["skald.json", "taste.json"])
        doc = json.loads(target.read_text())
        clean = doc["inputs"]["skald.json"]
        self.assertEqual(clean["household"]["rows"], ["foryou"])
        self.assertEqual(clean["taste"]["people"]["ana"]["prefs"],
                         {"discovery": "gems", "runtime": {"movie": 90}})
        self.assertEqual(clean["region"], config["region"])
        self.assertNotIn("synthetic", json.dumps(clean))
        self.assertEqual(doc["inputs"]["taste.json"],
                         {"people": {"ana": {"prefs": {"discovery": "gems"}}}})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertIsNone(snapshot.freeze(None, None, inputs=None, generation=None))

    def test_missing_history_is_not_empty_history(self):
        target = self.freeze(["history.json"])
        before = target.read_bytes()
        report = snapshot.evaluate(target)
        missing = report["availability"]["current_history"]
        self.assertEqual(missing, {"available": False, "count": None, "unusable": None})
        self.assertEqual(report["evidence_level"], DIAGNOSTIC)
        self.assertEqual(target.read_bytes(), before)
        self.write("history.json", {"items": []})
        target = snapshot.freeze(self.source, self.root / "empty", inputs=["history.json"],
                                 generation=lambda: 1, enabled=True)
        self.assertEqual(snapshot.evaluate(target)["availability"]["current_history"],
                         {"available": True, "count": 0, "unusable": 0})
        self.write("history.json", {"items": [{"at": 100, "played": True, "progress": .9}]})
        event = {"media_kind": "movie", "provider_id": "tmdb:1", "action": "played",
                 "source": "synthetic", "reliable": True, "occurred_at": 100,
                 "observed_at": 110, "timestamp_precision": "second"}
        (self.source / "playback.jsonl").write_text(
            "\n".join(json.dumps(e) for e in (event, dict(event, occurred_at=None), {})))
        target = snapshot.freeze(self.source, self.root / "current",
                                 inputs=["history.json", "playback.jsonl"],
                                 generation=lambda: 1, enabled=True)
        report = snapshot.evaluate(target)
        self.assertEqual(report["availability"]["current_history"]["unusable"], 1)
        self.assertEqual(report["availability"]["dated_playback"]["unusable"], 2)
        self.assertEqual(len(report["targets"]["events"]), 1)
        self.assertFalse(report["availability"]["curation"]["available"])
        self.assertTrue(all(capability in report["availability"]
                            for capability in snapshot.INPUTS.values()))

    def test_bounded_regular_files_and_integrity(self):
        path = self.write("history.json", {"items": []})
        with self.assertRaises(ValueError):
            self.freeze(["history.json"], max_bytes=1)
        path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError):
            self.freeze(["history.json"])
        path.rmdir()
        self.write("history.json", {"items": []})
        target = self.freeze(["history.json"])
        doc = json.loads(target.read_text())
        doc["inputs"]["history.json"]["items"].append({})
        target.chmod(0o600)
        target.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            snapshot.evaluate(target)
