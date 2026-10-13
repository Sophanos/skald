"""Private addon routes and local URL commands; all capabilities are synthetic."""
from skald.members import rid
from contextlib import redirect_stderr, redirect_stdout
import gzip
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import MagicMock, Mock, patch

from decide_kit import ROOT, close_server, load_server
from eval.profile_golden import NOW
from eval.route_parity import BaselineUnavailable, baseline_source
from skald import members
from test_api import raw_request

CANARY = "CanaryAddonUrlSecret" + "x" * 23


class AddonTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.cfg = members.fresh_config(["ana", "ben"])
        (self.data / "skald.json").write_text(json.dumps(self.cfg))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SKALD_")}
        self.env.update(DATA_DIR=str(self.data), PYTHONDONTWRITEBYTECODE="1", API_LOOPBACK_OPEN="0")
        p = patch.dict(os.environ, self.env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.logs = io.StringIO()
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.token = self.s.addon_token()
        self.s.manifest = lambda lang, scope=None: {"lang": lang, "scope": scope}
        self.s.feed_rows = lambda lang: [(rid('foryou', self.s.CONFIG), ["tv"], "For You")]
        self.s.row_metas = lambda *args, **kw: [{"id": "tmdb:1", "name": "Fixture"}]
        self.s.stats = lambda: {"private": True}
        self.s.explain = lambda key: {"key": key}
        self.s.note_served = lambda *a: None

    def request(self, path, **kw):
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            return raw_request(self.s, path, handler=self.s.Handler, **kw)[:3]

    def process(self, *args):
        return subprocess.run([sys.executable, *args], cwd=ROOT, env=self.env, capture_output=True, text=True)

    def test_fresh_routes_and_admin_require_token_on_every_peer(self):
        paths = ["/en-US/manifest.json", "/en-US/p/ana/manifest.json", f"/en-US/catalog/tv/{rid('foryou', self.s.CONFIG)}.json",
                 f"/en-US/p/ana/catalog/tv/{rid('foryou', self.s.CONFIG)}.json", "/stats", "/why/tmdb:1.json"]
        for peer in ("127.0.0.1", "203.0.113.1"):
            for path in paths:
                with self.subTest(path=path, peer=peer):
                    self.assertEqual(self.request(path, peer=peer)[0], 404)
                    response = self.request("/" + self.token + path, peer=peer)
                    self.assertEqual(response[0], 200)
                    self.assertTrue(response[1]["Cache-Control"].startswith("private,"))
        self.assertEqual(self.request("/" + self.token + "/api/v1/decide")[0], 404)
        self.assertEqual(self.request("/" + self.token + "/new-client")[0], 404)

    def test_wrong_missing_malformed_tokens_have_identical_404(self):
        expected = self.request("/unknown")
        bad = ["/" + "x" * 43 + "/en-US/manifest.json", "/en-US/manifest.json",
               "/" + self.token.swapcase() + "/en-US/manifest.json",
               "/" + self.token + "%2f/en-US/manifest.json", "/" + self.token + "/%2e%2e/en-US/manifest.json",
               "/" + self.token + "//en-US/manifest.json", "//" + self.token + "/en-US/manifest.json",
               "/" + self.token + f"/en-US%2fcatalog/tv/{rid('foryou', self.s.CONFIG)}.json",
               "/%" + format(ord(self.token[0]), "02x") + self.token[1:] + "/en-US/manifest.json"]
        for path in bad:
            with self.subTest(case=bad.index(path)), patch.object(self.s.hmac, "compare_digest", wraps=self.s.hmac.compare_digest) as compare:
                actual = self.request(path)
                self.assertEqual(actual, expected)
                for call in compare.call_args_list:
                    a, b = call.args
                    self.assertIsInstance(a, bytes)
                    self.assertIsInstance(b, bytes)
                    self.assertEqual(len(a), len(b))

    def test_cache_headers_private_fresh_public_legacy(self):
        path = f"/en-US/catalog/tv/{rid('foryou', self.s.CONFIG)}.json"
        fresh = self.request("/" + self.token + path)
        self.assertEqual(fresh[1]["Cache-Control"], "private, max-age=60")
        self.assertEqual(fresh[1]["Access-Control-Allow-Origin"], "*")
        self.s.CONFIG["addon"]["legacy_open"] = True
        public = self.request(path)
        self.assertEqual(public, self.request("/" + self.token + path))
        self.assertEqual(public[1]["Cache-Control"], "public, max-age=60")
        self.assertEqual(self.request("/stats", peer="203.0.113.1")[0], 404)

    def test_unicode_and_long_searches_match_legacy_through_token(self):
        self.s.CONFIG["addon"]["legacy_open"] = True
        self.s.search = lambda kind, query, lang: {"metas": [{"name": query}]}
        for query in ("Amélie", "千と千尋の神隠し", "a" * 180):
            for scope in ("", "p/ana/"):
                for kind in ("movie", "series"):
                    path = f"/en-US/{scope}catalog/{kind}/{rid('search', self.s.CONFIG)}/search={urllib.parse.quote(query)}.json"
                    with self.subTest(query_chars=len(query), scope=scope, kind=kind):
                        legacy = self.request(path)
                        token = self.request("/" + self.token + path)
                        self.assertEqual(legacy[0], 200)
                        self.assertEqual(token, legacy)
                        self.assertEqual(json.loads(token[2])["metas"], [{"name": query}])

    def test_api_loopback_open_ignored_once_and_bearer_still_works(self):
        with patch.dict(os.environ, {"API_LOOPBACK_OPEN": "1"}), redirect_stderr(self.logs):
            s = load_server(self.data)
        self.addCleanup(close_server, s)
        with patch.dict(os.environ, {"API_LOOPBACK_OPEN": "1"}):
            for handler in (s.ApiHandler, s.RemoteHandler):
                with patch.object(s, "API_PORT", s.REMOTE_PORT):
                    self.assertEqual(raw_request(s, "/api/v1/decide", handler=handler)[0], 401)
                    self.assertEqual(raw_request(s, "/api/v1/decide", "POST", {}, handler=handler)[0], 401)
            token = s.new_client("fixture-client", ["read", "decide"], "ana")
            self.assertEqual(raw_request(s, "/api/v1/decide", headers={"Authorization": "Bearer " + token})[0], 200)
        self.assertEqual(self.logs.getvalue().splitlines(), ["API_LOOPBACK_OPEN ignored: addon.legacy_open is false"])

    def test_token_permissions_backup_and_no_canary_leaks(self):
        (self.data / "addon.json").write_text(json.dumps({"token": CANARY}))
        self.assertEqual(len(CANARY), 43)
        self.assertEqual(self.s.addon_token(), CANARY)
        self.assertEqual(stat.S_IMODE((self.data / "addon.json").stat().st_mode), 0o600)
        self.assertEqual(self.s.BACKUP_SKIP["addon.json"], "secret, never opened")
        self.assertTrue(self.s.BACKUP_SECRET.search("addon.json"))
        self.request("/" + CANARY + "/en-US/manifest.json")
        self.request("/" + CANARY + f"/en-US/catalog/tv/{rid('foryou', self.s.CONFIG)}.json")
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            tail = (ROOT / "server.py").read_text().split('if __name__ == "__main__":', 1)[1]
            namespace = {**self.s.__dict__, "remote_token": Mock(), "load_state": Mock(), "ThreadingHTTPServer": MagicMock()}
            with patch.object(sys, "argv", ["server.py"]), patch.object(self.s.threading, "Thread"):
                exec(compile(tail.replace("\n    ", "\n"), "server-main", "exec"), namespace)
            self.s.save_feeds()
            self.s.backup_state(NOW)
        output = self.process(str(ROOT / "server.py"), "--print-config")
        self.assertEqual(output.returncode, 0)
        self.assertNotIn(CANARY, self.logs.getvalue() + output.stdout + output.stderr)
        self.assertNotIn(CANARY, (self.data / "feeds.json").read_text())
        for archive in (self.data / "backups").glob("*.tar.gz"):
            self.assertNotIn(CANARY.encode(), gzip.decompress(archive.read_bytes()))

    def test_rotation_atomic_invalidates_old_token_without_restart(self):
        path = "/en-US/p/ana/manifest.json"
        with patch.object(self.s.os, "replace", wraps=os.replace) as replace:
            new = self.s.addon_token(rotate=True)
        replace.assert_called_once()
        self.assertNotEqual(self.token, new)
        self.assertEqual(self.request("/" + self.token + path)[0], 404)
        self.assertEqual(self.request("/" + new + path)[0], 200)
        self.assertEqual(stat.S_IMODE((self.data / "addon.json").stat().st_mode), 0o600)
        self.assertFalse(list(self.data.glob(".addon-*")))

    def test_health_leaks_nothing(self):
        status, _, raw = self.request("/health", peer="203.0.113.1")
        self.assertEqual((status, raw), (200, b'{"ok": true}'))
        local = json.loads(self.request("/health")[2])
        self.assertEqual(local, {"ok": True, "enabled": False, "checks": [],
                                 "last_repair": None, "alert_configured": False})
        self.assertEqual(json.loads(self.request("/" + self.token + "/health")[2]), local)

    def test_malformed_get_never_echoes_token_and_has_privacy_headers(self):
        (self.data / "addon.json").write_text(json.dumps({"token": CANARY}))
        variants = [CANARY, CANARY + "%2f", CANARY + "x", CANARY.swapcase(), CANARY + "?junk",
                    "%" + format(ord(CANARY[0]), "02x") + CANARY[1:]]
        for legacy in (False, True):
            self.s.CONFIG["addon"]["legacy_open"] = legacy
            for variant in variants:
                with self.subTest(legacy=legacy, variant=variants.index(variant)):
                    source = io.BytesIO(f"GET /{variant}/en-US/manifest.json extra HTTP/1.0\r\n\r\n".encode())
                    output = io.BytesIO()
                    connection = Mock()
                    connection.makefile.return_value = source
                    connection.sendall.side_effect = output.write
                    with redirect_stdout(self.logs), redirect_stderr(self.logs):
                        self.s.Handler(connection, ("127.0.0.1", 0), Mock())
                    self.assertNotIn(CANARY.encode().lower(), output.getvalue().lower())
                    response = http.client.HTTPResponse(Mock(makefile=lambda *a, **kw: io.BytesIO(output.getvalue())))
                    response.begin()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(response.getheader("Cache-Control"), "private, no-store")
                    self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
            status, headers, _ = self.request(f"/{CANARY}/unknown")
            self.assertEqual(status, 404)
            self.assertEqual(headers["Cache-Control"], "private, no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            _, _, _, connection = raw_request(self.s, f"/{CANARY}/why/{CANARY}.json", handler=self.s.Handler)
            wire = b"".join(call.args[0] for call in connection.sendall.call_args_list)
            self.assertNotIn(CANARY.encode(), wire)
        self.assertNotIn(CANARY.lower(), self.logs.getvalue().lower())

    def test_unrelated_legacy_errors_keep_base_wire_bytes(self):
        try:
            source = self.enterContext(baseline_source())
        except BaselineUnavailable as error:
            self.skipTest(str(error))
        spec = importlib.util.spec_from_file_location("addon_error_base", source)
        base = importlib.util.module_from_spec(spec)
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            spec.loader.exec_module(base)
        self.addCleanup(close_server, base)
        self.s.CONFIG["addon"]["legacy_open"] = True
        for path in ("/unknown", "/en-US/nope", "/plain extra", "/" + "a" * 43 + "/nope extra"):
            with self.subTest(path=path):
                old = raw_request(base, path, handler=base.Handler)[3]
                new = raw_request(self.s, path, handler=self.s.Handler)[3]
                wire = lambda connection: b"".join(call.args[0] for call in connection.sendall.call_args_list)
                self.assertEqual(wire(new), wire(old))

    def test_configured_token_never_enters_search_log_or_response(self):
        (self.data / "addon.json").write_text(json.dumps({"token": CANARY}))
        self.s.CONFIG["addon"]["legacy_open"] = True
        with patch.object(self.s, "search", return_value={"metas": [{"name": "upstream"}]}) as search:
            for query in (CANARY, CANARY.swapcase(), "junk " + CANARY + " trailing"):
                for scope in ("", "p/ana/", "p/together/"):
                    for kind in ("movie", "series"):
                        path = f"/en-US/{scope}catalog/{kind}/{rid('search', self.s.CONFIG)}/search=" + urllib.parse.quote(query) + ".json"
                        for prefix in ("", "/" + CANARY):
                            response = self.request(prefix + path)
                            self.assertEqual(response[0], 200)
                            search.assert_not_called()
                            self.assertEqual(json.loads(response[2]), {"metas": []})
        log = self.data / "searches.jsonl"
        self.assertNotIn(CANARY.lower(), (log.read_text() if log.exists() else "").lower())
        self.assertNotIn(CANARY.lower(), self.logs.getvalue().lower())

    def test_invalid_stored_capability_is_not_replaced(self):
        path = self.data / "addon.json"
        for raw in ('{}', '{"token":"short"}', '[]'):
            path.write_text(raw)
            with self.assertRaisesRegex(ValueError, "invalid addon capability"):
                self.s.addon_token()
            self.assertEqual(path.read_text(), raw)

    def test_url_commands_and_rotation(self):
        expected = self.s.addon_urls("https://host.example")
        self.assertEqual(len(expected), 4)
        for command in ([str(ROOT / "server.py"), "--addon-url"], ["-m", "skald", "addon-url"]):
            output = self.process(*command, "--base", "https://host.example")
            self.assertEqual((output.returncode, output.stderr), (0, ""))
            self.assertEqual(output.stdout.splitlines(), expected)
        rotated = self.process(str(ROOT / "server.py"), "--rotate-addon-token", "--base", "https://host.example")
        self.assertEqual(rotated.returncode, 0)
        self.assertNotEqual(rotated.stdout.splitlines(), expected)
        self.assertEqual(rotated.stdout.splitlines(), self.s.addon_urls("https://host.example"))
        self.assertEqual(self.process("-m", "skald", "--snapshot", str(self.data), "addon-url").returncode, 2)
        before = (self.data / "addon.json").read_bytes()
        self.assertEqual(self.process(str(ROOT / "server.py"), "--rotate-addon-token", "--base", "https://user:pass@host").returncode, 2)
        self.assertEqual((self.data / "addon.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
