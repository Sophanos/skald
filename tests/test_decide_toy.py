import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from decide_kit import close_server, check_contract, load_server, ReplayLLM, reset
from skald.__main__ import main
from test_api import raw_request

# toy:begin
def toy(s, path):
    s.decision("toy_pick", ver=1, lane="search", cap=None, ttl=s.DAY,
        inp={"choices": [s.TITLE, 2], "mood": ("cozy", "dark")}, out={"pick": (s.TITLE, None)},
        norm=lambda x: x, rule=lambda x, p: ({"pick": x["choices"][0]}, 1) if len(x["choices"]) == 1 else None,
        params={}, llm={"path": "/api/alpha/decisions", "model": s.ROUTE_MODEL, "batch": 25, "timeout": 4,
            "prompt": lambda b: {"model": s.ROUTE_MODEL, "state": json.dumps(b), "questions": {
                str(i): {"type": "choice", "criteria": {"c0": "first", "c1": "second", "none": "none"}} for i in range(len(b))}},
            "parse": lambda r, b: [({"pick": x["choices"][0] if r["answers"][str(i)]["choice"] == "c0" else None},
                                    r["answers"][str(i)]["confidence"]) for i, x in enumerate(b)]},
        fallback=lambda x, err: ({"pick": None}, 0), requires=(), serving=False, min_acc=1, fixtures=str(path))
    rows = [{"in": {"choices": [{"key": f"tmdb:{i + 1}", "kind": "movie"}, {"key": f"tmdb:{i + 1}", "kind": "tv"}], "mood": "cozy"},
             "expect": {"pick": {"key": f"tmdb:{i + 1}", "kind": "movie"}},
             "recorded": {"answers": {"0": {"choice": "c0", "confidence": .9}}, "usage": {"cost": .001}}, "note": "toy"} for i in range(20)]
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))
    return rows
# toy:end


class ToyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        self.rows = toy(self.s, Path(self.temp.name, "toy.jsonl"))
        self.headers = {"Authorization": "Bearer " + self.s.new_client("toy-api", ["read", "decide"])}

    def test_one_block_contract_api_cli_and_batch(self):
        source = Path(__file__).read_text().split("# toy:begin\n", 1)[1].split("# toy:end", 1)[0]
        self.assertLess(len(source.splitlines()), 30)
        metrics = check_contract(self, self.s, "toy_pick")
        self.assertEqual(metrics["acc"], 1)
        reset(self.s)
        code, _, raw, _ = raw_request(self.s, "/api/v1/decide", headers=self.headers)
        self.assertEqual(code, 200)
        self.assertIn("toy_pick", [t["type"] for t in json.loads(raw)["types"]])
        before = len(Path(self.s.DATA, "decisions.jsonl").read_text().splitlines())
        with ReplayLLM(self.s) as replay:
            replay.arm({"answers": {str(i): {"choice": "c0", "confidence": .9} for i in range(20)}, "usage": {"cost": .002}})
            code, _, raw, _ = raw_request(self.s, "/api/v1/decide/toy_pick", method="POST", headers=self.headers, body={"items": [r["in"] for r in self.rows]})
        self.assertEqual(code, 200)
        self.assertEqual(replay.calls, 1)
        self.assertEqual(len(json.loads(raw)["results"]), 20)
        self.s.flush_decisions()
        self.assertEqual(len(Path(self.s.DATA, "decisions.jsonl").read_text().splitlines()) - before, 20)
        token = self.s.new_client("toy-cli", ["read", "decide"])
        def wire(req, **kwargs):
            code, _, raw, _ = raw_request(self.s, req.selector, headers=dict(req.header_items()))
            self.assertEqual(code, 200)
            return io.BytesIO(raw)
        with patch.dict(os.environ, {"SKALD_API": "http://127.0.0.1:1", "SKALD_TOKEN": token, "SKALD_SNAPSHOT": ""}), \
                patch("urllib.request.build_opener", return_value=Mock(open=wire)):
            out = io.StringIO()
            self.assertEqual(main(["decide", "--list", "--remote", "--json"], out=out), 0)
            self.assertIn("toy_pick", [t["type"] for t in json.loads(out.getvalue())["types"]])
            self.assertNotIn(token, out.getvalue())

    def test_toy_cli_real_port_zero(self):
        try:
            server = self.s.ThreadingHTTPServer(("127.0.0.1", 0), self.s.ApiHandler)
        except PermissionError:
            self.skipTest("sandbox forbids loopback binding; toy CLI HTTP transport test still runs")
        with server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            token = self.s.new_client("toy-cli", ["read", "decide"])
            try:
                with patch.dict(os.environ, {"SKALD_API": f"http://127.0.0.1:{server.server_port}", "SKALD_TOKEN": token, "SKALD_SNAPSHOT": ""}):
                    out = io.StringIO()
                    self.assertEqual(main(["decide", "--list", "--remote", "--json"], out=out), 0)
                    self.assertIn("toy_pick", [t["type"] for t in json.loads(out.getvalue())["types"]])
            finally:
                server.shutdown()
                worker.join(2)
