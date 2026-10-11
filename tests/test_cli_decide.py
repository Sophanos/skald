from decide_kit import write_legacy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import unittest
from unittest.mock import Mock, patch

from skald.__main__ import main
from skald import remote
from skald.engine import Engine
from decide_kit import close_server, load_server, ROOT
from test_api import raw_request
from eval import decisions


class CLIDecideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = Path(self.temp.name, "snapshot")
        self.snapshot.mkdir()
        write_legacy(self.snapshot)
        (self.snapshot / "profile.json").write_text(json.dumps({"entries": [], "built_at": time.time()}))
        self.state = Path(self.temp.name, "state")

    def cli(self, *args, code=0):
        out = io.StringIO()
        result = main([*args, "--json"], out=out)
        self.assertEqual(result, code, out.getvalue())
        return json.loads(out.getvalue())

    def test_local_cannot_spend_even_with_environment_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "must-not-spend"}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")) as network:
            data = self.cli("--snapshot", str(self.snapshot), "--state-dir", str(self.state),
                            "decide", "route_query", "--in", '{"query":"x"}')
            network.assert_not_called()
        self.assertEqual((data["source"], data["why"]), ("fallback", "allow_llm"))
        self.assertFalse(self.state.exists())

    def test_eval_without_snapshot_text_and_json(self):
        with patch.dict(os.environ, {"SKALD_SNAPSHOT": ""}), \
                patch("skald.__main__.Engine", side_effect=AssertionError("eval constructed Engine")), \
                patch("skald.__main__.live.pull", side_effect=AssertionError("eval pulled snapshot")):
            start = time.perf_counter()
            data = self.cli("decide", "--eval")
            self.assertLess(time.perf_counter() - start, 5)
            self.assertEqual(len(data), 6)
            self.assertTrue(all(m["pass"] for m in data.values()))
            out = io.StringIO()
            self.assertEqual(main(["decide", "rank_titles", "--eval"], out=out), 0)
            text = out.getvalue()
            for token in ("rank_titles v1", "acc 1.00 ≥ 1.00", "p50", "p95", "rule 28", "$0", "ok"):
                self.assertIn(token, text)
            for args in (("--in", "{}"), ("--list",), ("--remote",)):
                self.assertIn("--eval cannot use", self.cli("decide", "--eval", *args, code=2)["error"])
            self.assertEqual(self.cli("decide", "unknown", "--eval", code=2), {"error": "unknown type"})

    def test_eval_exit_one_with_broken_fixture_in_temp_copy(self):
        path = Path(self.temp.name, "broken.jsonl")
        server = load_server(self.temp.name)
        self.addCleanup(close_server, server)
        rows = decisions.fixture_rows(server, "rank_titles")
        rows[0]["expect"]["pick"] = None
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        server.DECISIONS["rank_titles"]["fixtures"] = str(path)
        with patch("decide_kit.load_server", return_value=server), patch("decide_kit.close_server"), \
                patch.dict(os.environ, {"SKALD_SNAPSHOT": ""}):
            data = self.cli("decide", "rank_titles", "--eval", code=1)
        self.assertFalse(data["rank_titles"]["pass"])
        self.assertEqual(data["rank_titles"]["acc"], 27 / 28)

    def test_eval_exit_one_over_serving_latency_budget(self):
        metrics = {"n": 28, "acc": 1, "sources": {"rule": 28}, "llm_calls": 0,
                   "cost": 0, "p50_ms": 1, "p95_ms": 11}
        with patch("decide_kit.run_type", return_value=metrics):
            data = self.cli("decide", "rank_titles", "--eval", code=1)
        self.assertFalse(data["rank_titles"]["pass"])

    def test_engine_list_and_batch_and_file_input(self):
        with Engine(self.snapshot) as engine:
            self.assertEqual(len(engine.decisions()["types"]), len([n for n, s in engine._server.DECISIONS.items() if s["api"]]))
            results = engine.decide("route_query", [{"query": "x"}, {"query": "y"}])["results"]
            self.assertEqual([r["why"] for r in results], ["allow_llm", "allow_llm"])
        file = Path(self.temp.name, "batch.json")
        file.write_text('[{"query":"x"},{"query":"y"}]')
        data = self.cli("--snapshot", str(self.snapshot), "--state-dir", str(self.state), "decide", "route_query", "--in", "@" + str(file))
        self.assertEqual(len(data["results"]), 2)
        data = self.cli("--snapshot", str(self.snapshot), "--state-dir", str(self.state), "decide", "--list")
        self.assertEqual(len(data["types"]), len([n for n, s in engine._server.DECISIONS.items() if s["api"]]))

    def test_remote_bypasses_snapshot_and_routes_generic_body(self):
        with patch("skald.__main__.Engine", side_effect=AssertionError("remote constructed Engine")), \
                patch("skald.__main__.live.pull", side_effect=AssertionError("remote pulled snapshot")), \
                patch("skald.remote.request", return_value={"types": []}) as request:
            self.assertEqual(self.cli("--live", "decide", "--list", "--remote"), {"types": []})
            request.assert_called_with("/api/v1/decide")
            self.cli("decide", "route_query", "--in", '[{"query":"x"}]', "--person", "together", "--no-llm", "--remote")
            request.assert_called_with("/api/v1/decide/route_query", {"items": [{"query": "x"}], "person": "together", "no_llm": True})

    def test_remote_token_private_and_http_errors_sanitized(self):
        home = Path(self.temp.name, "home")
        (home / ".config/skald").mkdir(parents=True)
        file = home / ".config/skald/token"
        token = "t" * 43
        file.write_text(token)
        file.chmod(0o644)
        with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {"SKALD_API": "http://127.0.0.1:1", "SKALD_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "token file must be private"):
                remote.request("/api/v1/decide")
            file.chmod(0o600)
            with patch("urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError("private-url", 401, token, {}, None)
                data = self.cli("decide", "--list", "--remote", code=2)
                self.assertEqual(data, {"error": "api 401"})
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.headers["Authorization"], "Bearer " + token)
                self.assertNotIn(token, json.dumps(data))
                self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), json.dumps(data))

    def test_bad_cli_inputs_and_redirect(self):
        with patch("skald.remote.request", side_effect=AssertionError("bad input reached API")):
            for args in (("--list", "route_query"), ("route_query",), ("../bad", "--in", '{}'),
                         ("route_query", "--in", 'bad'), ("route_query", "--in", '1'),
                         ("route_query", "--in", json.dumps([{}] * 26))):
                self.cli("decide", *args, "--remote", code=2)
        with self.assertRaisesRegex(ValueError, "api redirect"):
            remote.NoRedirect().redirect_request(None)

    def test_client_command_before_remote_setup_and_port_refusal(self):
        # Keep the minted capability in captured memory; never print it from the test.
        from skald.members import LEGACY
        Path(self.temp.name, "skald.json").write_text(json.dumps(LEGACY))
        env = {**os.environ, "TMDB_API": "offline", "DATA_DIR": self.temp.name, "API_PORT": ""}
        process = subprocess.run([os.sys.executable, str(ROOT / "server.py"), "--new-client", "cli-test", "--scopes", "read,decide,events", "--person", "ben"],
                                 capture_output=True, text=True, env=env)
        self.assertEqual(process.returncode, 0)
        token = process.stdout.strip()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(process.stdout.splitlines()), 1)
        self.assertEqual(process.stderr, "")
        file = Path(self.temp.name, "clients.json")
        self.assertNotIn(token, file.read_text())
        self.assertFalse(Path(self.temp.name, "remote.json").exists())
        duplicate = subprocess.run([os.sys.executable, str(ROOT / "server.py"), "--new-client", "cli-test"], capture_output=True, env=env)
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(duplicate.stdout, b"")
        revoked = subprocess.run([os.sys.executable, str(ROOT / "server.py"), "--revoke-client", "cli-test"], capture_output=True, env=env)
        self.assertEqual(revoked.returncode, 0)
        self.assertEqual(revoked.stdout, b"")
        refused = subprocess.run([os.sys.executable, str(ROOT / "server.py")], capture_output=True, env={**env, "API_PORT": "8080", "PORT": "8080"})
        self.assertEqual(refused.returncode, 2)
        self.assertFalse(Path(self.temp.name, "remote.json").exists())

    def test_unknown_and_abbreviated_admin_flags_reach_port_guard(self):
        env = {**os.environ, "TMDB_API": "offline", "DATA_DIR": self.temp.name, "API_PORT": "8080", "PORT": "8080"}
        clients = Path(self.temp.name, "clients.json")
        clients.write_text('{"clients": []}')
        for flag in ("--new", "--revoke", "--unknown"):
            with self.subTest(flag=flag):
                process = subprocess.run([os.sys.executable, str(ROOT / "server.py"), flag, "x"],
                                         capture_output=True, env=env, timeout=5)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, b"")
                self.assertEqual(process.stderr.splitlines(), [b"ignoring unknown args: 2", b"API_PORT must differ from PORT"])
                self.assertEqual(clients.read_text(), '{"clients": []}')
                self.assertFalse(Path(self.temp.name, "remote.json").exists())
