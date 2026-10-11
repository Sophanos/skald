from decide_kit import write_legacy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import check_contract, close_server, load_server, run_type, ROOT, SETUP, synthetic_model
from eval import decisions
from skald.__main__ import main
from skald.engine import Engine
from test_api import raw_request


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        # Scaffold phases reload the same temporary server, without touching real fixtures.
        self.loader = patch.object(decisions, "load_server", return_value=self.s)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.closer = patch.object(decisions, "close_server")
        self.closer.start()
        self.addCleanup(self.closer.stop)

    def test_fast_path(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(decisions.main(["--new", "toy_rank", "--ranker"]), 0)
        block = out.getvalue()
        self.assertLessEqual(len(block.splitlines()), 12)
        exec(block, vars(self.s))
        path = Path(self.temp.name, "toy_rank.jsonl")
        self.s.DECISIONS["toy_rank"]["fixtures"] = str(path)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(decisions.main(["--new", "toy_rank", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"written": str(path), "rows": 20})
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertTrue(all(r["note"].startswith("Synthetic TODO") and not r["expect"] for r in rows))
        with self.assertRaisesRegex(AssertionError, "empty expect"):
            check_contract(self, self.s, "toy_rank")
        rows[0]["expect"] = {"pick": None}
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        with self.assertRaisesRegex(AssertionError, "TODO"):
            check_contract(self, self.s, "toy_rank")

        def hand(inp):
            choices = list({(c["key"], c["kind"]): c for c in inp["choices"]}.values())
            person = inp["person"]
            # Calibrated affinities: cozy 93/70/77, unanchored 70 (household 88), private 96.
            points = {i: (93 if person in (None, "ben") else 77 if person == "together" else 70)
                      for i in range(1, 13)}
            points[5] = 88 if person is None else 70
            points[3] = 96 if person in (None, "ana") else None
            points[2] = points[2] if person in (None, "ben") else None
            if person == "guest":
                points = dict.fromkeys(points)
            scored = [c for c in choices if (n := int(c["key"][5:]) - 900000) in points and points[n] is not None
                      and c["kind"] == ("movie" if n <= 8 else "tv")]
            scored.sort(key=lambda c: -points[int(c["key"][5:]) - 900000])
            return {"pick": next(iter(scored), None)}

        for row in rows:
            row["expect"] = hand(row["in"])
            row["note"] = "Synthetic: hand-derived known movie choices before unknown TV keys"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        with patch.dict(SETUP, {"toy_rank": synthetic_model}):
            check_contract(self, self.s, "toy_rank")
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            decisions.scaffold("toy_rank")
        self.assertEqual(path.read_bytes(), before)
        token = self.s.new_client("scaffold", ["decide"], "ben")
        inp = {"choices": [{"key": "tmdb:900003", "kind": "movie"}, {"key": "tmdb:900001", "kind": "movie"}]}
        code, _, raw, _ = raw_request(self.s, "/api/v1/decide/toy_rank", "POST", {"in": inp},
                                       {"Authorization": "Bearer " + token})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["answer"]["pick"], inp["choices"][1])
        source = Path(self.temp.name, "server.py")
        source.write_text((ROOT / "server.py").read_text() + "\n" + block)
        snapshot = Path(self.temp.name, "snapshot")
        snapshot.mkdir()
        write_legacy(snapshot)
        (snapshot / "profile.json").write_text('{"entries": []}')
        def engine(work):
            result = Engine(work, server_path=source)
            synthetic_model(result._server)
            return result
        with patch("skald.__main__.Engine", side_effect=engine), patch.dict(os.environ, {"SKALD_SNAPSHOT": ""}):
            out = io.StringIO()
            self.assertEqual(main(["--snapshot", str(snapshot), "--state-dir", str(Path(self.temp.name, "state")),
                                   "decide", "toy_rank", "--in",
                                   json.dumps({**inp, "person": "ben"}), "--json"], out=out), 0)
            self.assertEqual(json.loads(out.getvalue())["answer"]["pick"], inp["choices"][1])

    def test_examples_and_plain_block(self):
        for spec in self.s.DECISIONS.values():
            for i in range(20):
                inp = decisions.example_input(spec["inp"], i)
                self.assertEqual(inp, decisions.example_input(spec["inp"], i))
                self.s.check(spec["inp"], inp)
        exec(decisions.scaffold("toy_plain")["block"], vars(self.s))
        self.assertEqual(self.s.decide("toy_plain", {"ok": True})["answer"], {"ok": True})
        for name in ("../bad", "UPPER", "x", "a\nb"):
            with self.assertRaises(ValueError):
                decisions.scaffold(name)

    def test_p95_nearest_rank_and_contract_latency_guard(self):
        path = Path(self.temp.name, "latency.jsonl")
        rows = [{"in": {"ok": True}, "expect": {"ok": True}, "recorded": None, "note": "Synthetic: true"}] * 20
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        exec(decisions.scaffold("toy_latency")["block"], vars(self.s))
        self.s.DECISIONS["toy_latency"]["fixtures"] = str(path)
        answers = [{"answer": {"ok": True}, "source": "rule", "ms": i} for i in range(1, 21)]
        with patch.object(self.s, "decide", side_effect=answers):
            metrics = run_type(self.s, "toy_latency")
        self.assertEqual((metrics["p50_ms"], metrics["p95_ms"]), (10.5, 19))
        with patch("decide_kit.run_type", return_value={"acc": 1, "p95_ms": 11}):
            with self.assertRaises(AssertionError):
                check_contract(self, self.s, "toy_latency")
