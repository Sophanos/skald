"""Offline privacy contracts for the public addon listener."""
from skald.members import rid
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class PrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.data.cleanup)
        spec = importlib.util.spec_from_file_location("privacy_server", Path(__file__).resolve().parents[1] / "server.py")
        cls.s = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"TMDB_API": "offline", "DATA_DIR": cls.data.name,
                                          "OPENROUTER_API_KEY": ""}), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            spec.loader.exec_module(cls.s)
        cls.addClassCleanup(cls.s._pool.shutdown)
        cls.addClassCleanup(cls.s._outer.shutdown)

    def setUp(self):
        patch = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, path, address="203.0.113.9", forwarded=None):
        wire = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n"
        if forwarded:
            wire += f"X-Forwarded-For: {forwarded}\r\n"
        connection = mock.Mock()
        connection.makefile.return_value = io.BytesIO((wire + "\r\n").encode())
        output = io.BytesIO()
        connection.sendall.side_effect = output.write
        self.s.Handler(connection, (address, 0), mock.Mock())
        response = http.client.HTTPResponse(mock.Mock(makefile=lambda *a, **kw: io.BytesIO(output.getvalue())))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def test_private_routes_reject_non_loopback_and_forwarded_headers(self):
        with mock.patch.object(self.s, "stats") as stats, mock.patch.object(self.s, "explain") as explain:
            for address in ("172.18.0.5", "203.0.113.9", "127.0.0.2", "::ffff:203.0.113.9"):
                for forwarded in (None, "127.0.0.1"):
                    unknown = self.request("/unknown", address, forwarded)
                    for path in ("/stats", "/why/x.json"):
                        with self.subTest(address=address, forwarded=forwarded, path=path):
                            status, headers, body = self.request(path, address, forwarded)
                            self.assertEqual((status, body), (404, unknown[2]))
                            self.assertNotIn("Access-Control-Allow-Origin", headers)
            stats.assert_not_called()
            explain.assert_not_called()

    def test_loopback_keeps_full_payload_without_cors(self):
        payload = {"profiles": {"person": {"titles": 12}}, "taste": {"experiences": ["cozy"]}}
        with mock.patch.object(self.s, "stats", return_value=payload) as stats, \
                mock.patch.object(self.s, "explain", return_value=payload) as explain:
            for address in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                for path in ("/stats", "/why/x.json"):
                    with self.subTest(address=address, path=path):
                        status, headers, body = self.request(path, address, "203.0.113.9")
                        self.assertEqual(status, 200)
                        self.assertEqual(json.loads(body), payload)
                        self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertEqual(stats.call_count, 3)
            self.assertEqual(explain.call_args_list, [mock.call("x")] * 3)

    def test_public_health_and_unknown_routes_have_no_cors(self):
        for path, expected in (("/health", 200), ("/unknown", 404), ("/de-DE/meta/movie/x.json", 404)):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, expected)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                if path == "/health":
                    self.assertEqual(json.loads(body), {"ok": True})

    def test_stremio_routes_keep_cors(self):
        with mock.patch.object(self.s, "manifest", return_value={}), \
                mock.patch.object(self.s, "feed_rows", return_value=[(rid('foryou', self.s.CONFIG), ["tv"], "For you")]), \
                mock.patch.object(self.s, "row_metas", return_value=[]), \
                mock.patch.object(self.s, "note_served"), mock.patch.object(self.s, "log_search"), \
                mock.patch.object(self.s, "search", return_value={"metas": []}) as search:
            for path in ("/de-DE/manifest.json", f"/de-DE/catalog/tv/{rid('foryou', self.s.CONFIG)}.json",
                         f"/de-DE/catalog/movie/{rid('search', self.s.CONFIG)}/search=.json",
                         f"/de-DE/catalog/movie/{rid('search', self.s.CONFIG)}/search=x.json"):
                with self.subTest(path=path):
                    status, headers, _ = self.request(path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            search.side_effect = RuntimeError("offline failure")
            status, headers, _ = self.request(f"/de-DE/catalog/movie/{rid('search', self.s.CONFIG)}/search=x.json")
            self.assertEqual(status, 502)
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")


if __name__ == "__main__":
    unittest.main()
