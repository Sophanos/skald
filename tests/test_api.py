import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server


def raw_request(server, path, method="GET", body=None, headers=None, peer="127.0.0.1", handler=None):
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    if isinstance(raw, str):
        raw = raw.encode()
    fields = {"Host": "localhost", **(headers or {})}
    if body is not None:
        fields.setdefault("Content-Length", str(len(raw)))
        fields.setdefault("Content-Type", "application/json")
    wire = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items() if v is not None)
    source, output = io.BytesIO(wire.encode() + b"\r\n" + raw), io.BytesIO()
    connection = Mock()
    connection.makefile.return_value = source
    connection.sendall.side_effect = output.write
    (handler or server.ApiHandler)(connection, (peer, 0), Mock())
    response = http.client.HTTPResponse(Mock(makefile=lambda *a, **kw: io.BytesIO(output.getvalue())))
    response.begin()
    return response.status, dict(response.getheaders()), response.read(), connection


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        self.token = self.s.new_client("test-cli", ["read", "decide"])
        self.headers = {"Authorization": "Bearer " + self.token}
        p = patch.dict(os.environ, {"API_LOOPBACK_OPEN": "0"})
        p.start()
        self.addCleanup(p.stop)

    def request(self, path="/api/v1/decide", **kwargs):
        kwargs["headers"] = {**self.headers, **kwargs.get("headers", {})}
        return raw_request(self.s, path, **kwargs)

    def test_index_scopes_and_both_mounts_and_default_off(self):
        for handler in (self.s.ApiHandler, self.s.RemoteHandler):
            with patch.object(self.s, "API_PORT", self.s.REMOTE_PORT):
                code, headers, raw, _ = self.request(handler=handler)
                self.assertEqual(code, 200)
                self.assertEqual([t["type"] for t in json.loads(raw)["types"]], [n for n, s in self.s.DECISIONS.items() if s["api"]])
                self.assertEqual(headers["Cache-Control"], "no-store")
                for header in ("Content-Security-Policy", "Referrer-Policy", "X-Content-Type-Options"):
                    self.assertIn(header, headers)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIsNone(self.s.API_PORT)
        self.assertEqual(self.request(handler=self.s.RemoteHandler)[0], 404)
        self.assertEqual(self.request(handler=self.s.Handler)[0], 404)

    def test_auth_never_trusts_forwarding_and_bad_loopback_token(self):
        code, _, _, _ = raw_request(self.s, "/api/v1/decide", peer="172.18.0.5", headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
        self.assertEqual(code, 401)
        self.assertEqual(self.request(headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(peer="172.18.0.5", headers=self.headers)[0], 200)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(raw_request(self.s, "/api/v1/decide")[0], 401)
        for flag in ("", "0", "true", "1"):
            with patch.dict(os.environ, {"API_LOOPBACK_OPEN": flag}):
                self.assertEqual(raw_request(self.s, "/api/v1/decide")[0], 200 if flag == "1" else 401)
                self.assertEqual(raw_request(self.s, "/api/v1/decide", headers={"Authorization": "Bearer wrong"})[0], 401)

    def test_auth_compares_every_client(self):
        self.s.new_client("other-cli", ["read"])
        with patch.object(self.s.hmac, "compare_digest", wraps=self.s.hmac.compare_digest) as compare:
            self.assertEqual(self.request(headers=self.headers)[0], 200)
            self.assertEqual(compare.call_count, 2)
            self.request(headers={"Authorization": "Bearer wrong"})
            self.assertEqual(compare.call_count, 4)

    def test_client_store_private_reread_and_fail_closed(self):
        file = Path(self.s.DATA, "clients.json")
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, file.read_text())
        self.assertEqual(self.request(headers=self.headers)[0], 200)
        self.s.revoke_client("test-cli")
        self.assertEqual(self.request(headers=self.headers)[0], 401)
        file.write_text('bad json')
        self.assertEqual(self.request(headers=self.headers)[0], 503)
        self.assertEqual(raw_request(self.s, "/api/v1/decide")[0], 401)
        file.unlink()
        self.assertEqual(self.request(headers=self.headers)[0], 503)

    def test_clients_reject_invalid_duplicate_and_accept_events(self):
        for name in ("loopback", "x", "../name", "Upper", "a" * 33):
            with self.assertRaises(ValueError):
                self.s.new_client(name, ["read"])
        with self.assertRaises(ValueError):
            self.s.new_client("test-cli", ["read"])
        for scopes in ([], ["unknown"]):
            with self.assertRaises(ValueError):
                self.s.new_client("new-cli", scopes)
        self.s.new_client("events-cli", ["events"], "ben")
        with self.assertRaises(ValueError):
            self.s.revoke_client("unknown-cli")

    def test_scope_and_person_binding(self):
        read = self.s.new_client("read-cli", ["read"])
        decide = self.s.new_client("decide-cli", ["decide"])
        bound = self.s.new_client("bound-cli", ["read", "decide"], "ben")
        self.assertEqual(self.request(headers={"Authorization": "Bearer " + decide})[0], 403)
        path = "/api/v1/decide/route_query"
        body = {"in": {"query": "new request"}, "no_llm": True}
        self.assertEqual(self.request(path, method="POST", body=body, headers={"Authorization": "Bearer " + read})[0], 403)
        for person, expected in (("ana", 403), ("ben", 200), ("together", 200), (None, 403)):
            self.assertEqual(self.request(path, method="POST", body={**body, "person": person},
                                          headers={"Authorization": "Bearer " + bound})[0], expected)
        self.assertEqual(self.request(path, method="POST", body=body, headers={"Authorization": "Bearer " + bound})[0], 200)

    def test_route_history_and_list_fields_and_compatible_defaults(self):
        path = "/api/v1/decide/route_query"
        for i, choices in enumerate(({}, {"watched": {"choice": "unseen"}, "list": {"choice": "best"}})):
            with patch.object(self.s, "openrouter", return_value={"answers": {
                    "route": {"choice": "request", "confidence": .9}, **choices}}) as model:
                code, _, raw, _ = self.request(path, method="POST", body={"in": {"query": f"synthetic query {i}"}})
            self.assertEqual(code, 200)
            answer = json.loads(raw)["answer"]
            self.assertEqual(answer, {"choice": "request", "confidence": .9,
                                      "watched": "unseen" if choices else "any", "list": "best" if choices else "taste"})
            self.assertEqual(set(model.call_args.args[1]["questions"]), {"route", "watched", "list"})
            model.assert_called_once()
        code, _, raw, _ = self.request(path, method="POST", body={"in": {"query": "synthetic fallback"}, "no_llm": True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["answer"], {"choice": None, "confidence": None, "watched": "any", "list": "taste"})

    def test_guards_and_status_codes(self):
        path = "/api/v1/decide/route_query"
        body = {"in": {"query": "guarded request"}, "no_llm": True}
        cases = [(403, {"Origin": ""}), (400, {"Transfer-Encoding": "chunked"}),
                 (400, {"Content-Length": "-1"}), (411, {"Content-Length": None}),
                 (413, {"Content-Length": "65537"}), (400, {"Content-Length": "100000"}),
                 (415, {"Content-Type": "text/plain"}), (400, {"Content-Length": "2"})]
        for code, headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(path, method="POST", body=body, headers=headers)[0], code)
        self.assertEqual(self.request(body=b"x")[0], 400)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request(path, method="POST", body=b"{")[0], 400)
        self.assertEqual(self.request(path, method="POST", body=body, headers={"Content-Length": "500"})[0], 400)
        self.assertEqual(self.request(path + "-missing", method="POST", body=body)[0], 404)
        self.assertEqual(self.request("/api/v1/other")[0], 404)
        for handler in (self.s.ApiHandler, self.s.RemoteHandler):
            with patch.object(self.s, "API_PORT", self.s.REMOTE_PORT):
                for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "UNKNOWN"):
                    self.assertEqual(self.request(path, method=method, handler=handler)[0], 405)
        self.assertEqual(self.request(method="POST", body=body)[0], 405)

    def test_deadline_and_timeout_reset(self):
        body = {"in": {"query": "timeout request"}, "no_llm": True}
        with patch.object(self.s, "read_remote_body", side_effect=TimeoutError):
            code, _, _, conn = self.request("/api/v1/decide/route_query", method="POST", body=body)
            self.assertEqual(code, 408)
            self.assertEqual(conn.settimeout.call_args.args, (5,))
        original = self.s.decide_many
        def decide(*args, **kwargs):
            return original(*args, **kwargs)
        with patch.object(self.s, "decide_many", side_effect=decide):
            code, _, _, conn = self.request("/api/v1/decide/route_query", method="POST", body=body)
            self.assertEqual(code, 200)
            self.assertEqual(conn.settimeout.call_args.args, (5,))

    def test_bad_input_and_batch_and_receipts_no_credentials(self):
        path = "/api/v1/decide/route_query"
        cases = [{}, [], {"in": {}}, {"in": {"query": 1}}, {"in": {"query": "x"}, "items": []},
                 {"items": "wrong"}, {"items": [{}] * 26}, {"in": {"query": "x"}, "no_llm": 1},
                 {"in": {"query": "x"}, "person": "other"}, {"in": {"query": "x"}, "extra": True}]
        for body in cases:
            self.assertEqual(self.request(path, method="POST", body=body)[0], 400)
        code, _, raw, _ = self.request(path, method="POST", body={"in": {"query": 1}})
        self.assertEqual(json.loads(raw), {"error": "query"})
        for secret in (self.token, hashlib.sha256(self.token.encode()).hexdigest(), "/private/path"):
            code, _, error_raw, _ = self.request(path, method="POST", body={"in": {"query": "x", secret: True}})
            self.assertEqual(code, 400)
            self.assertNotIn(secret, error_raw.decode())
        code, _, raw, _ = self.request(path, method="POST", headers=self.headers,
                                       body={"items": [{"query": "SecretQueryA"}, {"query": "SecretQueryB"}], "no_llm": True})
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(raw)["results"]), 2)
        self.s.flush_decisions()
        receipts = Path(self.s.DATA, "decisions.jsonl").read_text()
        for secret in (self.token, hashlib.sha256(self.token.encode()).hexdigest(), "SecretQueryA", "SecretQueryB"):
            self.assertNotIn(secret, receipts)
            self.assertNotIn(secret, raw.decode())
        self.assertEqual({json.loads(line)["client"] for line in receipts.splitlines()}, {"test-cli"})

    def test_rate_limit_per_client(self):
        for _ in range(60):
            self.assertEqual(self.request(headers=self.headers)[0], 200)
        self.assertEqual(self.request(headers=self.headers)[0], 429)
        with patch.dict(os.environ, {"API_LOOPBACK_OPEN": "1"}):
            self.assertEqual(raw_request(self.s, "/api/v1/decide")[0], 200)
        with patch.object(self.s.time, "monotonic", return_value=self.s.time.monotonic() + 61):
            self.assertEqual(self.request(headers=self.headers)[0], 200)

    def test_path_hygiene(self):
        for path in ("/api/v1/../decide", "/api/v1/%2e/decide", "/api/v1/%2fdecide", "/api/v1//decide", "/stats"):
            self.assertEqual(self.request(path)[0], 404)
            if path.startswith("/api/v1/"):
                with patch.object(self.s, "API_PORT", self.s.REMOTE_PORT):
                    code, _, raw, _ = self.request(path, handler=self.s.RemoteHandler)
                    self.assertEqual(code, 404)
                    self.assertIn("error", json.loads(raw))

    def test_real_port_zero_both_mounts(self):
        for handler in (self.s.ApiHandler, self.s.RemoteHandler):
            try:
                server = self.s.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except PermissionError:
                self.skipTest("sandbox forbids loopback binding; raw HTTP mount tests still run")
            with server, patch.object(self.s, "API_PORT", self.s.REMOTE_PORT):
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    conn.request("GET", "/api/v1/decide", headers=self.headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(len(json.loads(response.read())["types"]), len([n for n, s in self.s.DECISIONS.items() if s["api"]]))
                    conn.close()
                finally:
                    server.shutdown()
                    worker.join(2)
