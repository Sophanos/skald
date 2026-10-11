"""One temporary adapter is the entire toy source integration."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import skald.ingest
from skald.__main__ import main
from skald.engine import Engine

from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=True)
ANA, BEN = snapshot_people(SNAPSHOT)

TOY = '''
# toy:begin
from skald.ingest import Candidate, clean_title
SOURCE = {"hint": "Toy title export", "dir": False, "profiles": False}

def parse(path, **kw):
    with open(path, encoding="utf-8") as fh:
        return [Candidate(clean_title(line), "1999", "movie", {"tmdb": 603}, src="toy")
                for line in fh if line.strip()]
# toy:end
'''


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), "set SKALD_SNAPSHOT_U to run snapshot gates")
class ToyIntakeTests(unittest.TestCase):
    def test_one_adapter_with_owner_trust_row(self):
        lines = TOY.split("# toy:begin\n")[1].split("# toy:end")[0].splitlines()
        self.assertEqual(len(lines), 7)
        self.assertLessEqual(len(lines), 25)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "toy.py").write_text(TOY)
            export = root / "synthetic.txt"
            export.write_text("The Matrix\n")
            state = root / "state"
            skald.ingest.__path__.append(temp)
            self.addCleanup(skald.ingest.__path__.remove, temp)
            self.addCleanup(sys.modules.pop, "skald.ingest.toy", None)
            importlib.invalidate_caches()
            def cli(*args, code=0):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    status = main(["--snapshot", str(SNAPSHOT), "--state-dir", str(state), "--json", "onboard", *args])
                self.assertEqual(status, code, out.getvalue())
                return json.loads(out.getvalue())
            def trusted(*args, **kw):
                engine = Engine(*args, **kw)
                engine._server.IMPORT_TRUST = {"toy": .4}
                return engine
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                    patch("urllib.request.build_opener", side_effect=AssertionError("network forbidden")), \
                    patch.object(Engine, "react", return_value={"facets": [], "rows": {}}):
                with patch("skald.__main__.Engine", side_effect=trusted):
                    row = next(r for r in cli("--sources")["sources"] if r["name"] == "toy")
                    self.assertEqual(row, {"name": "toy", "hint": "Toy title export", "dir": False, "profiles": False,
                                           "trust": .4, "tainted": True, "usable": True})
                    flags = ["--source", f"toy={export}", "--person", BEN, "--non-interactive"]
                    staged = cli(*flags)
                    result = cli(*flags, "--yes", "--plan", staged["plan"])
                    self.assertEqual(result["added"], 1)
                    events = [json.loads(line) for line in (state / "imports.jsonl").read_text().splitlines()]
                    self.assertEqual([(e["src"], e["key"], e["action"]) for e in events], [("toy", "tmdb:603", "seen")])
                row = next(r for r in cli("--sources")["sources"] if r["name"] == "toy")
                self.assertFalse(row["usable"])
                self.assertEqual(row["trust"], 0)
                module = skald.ingest.sources()["toy"]
                with patch.object(module, "parse", side_effect=AssertionError("refuse before parsing")):
                    self.assertEqual(cli(*flags, "--dry-run", code=2), {"error": "source needs a server trust row"})
