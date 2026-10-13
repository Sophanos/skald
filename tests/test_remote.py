"""Offline contract tests for the phone taste remote: python3 -m unittest discover -s tests -v."""
from skald.members import rid
import concurrent.futures
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock


class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("remote_server", Path(__file__).resolve().parents[1] / "server.py")
        cls.s = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"TMDB_API": "offline", "OPENROUTER_API_KEY": ""}), \
                mock.patch("threading.Thread.start") as start, \
                mock.patch("http.server.ThreadingHTTPServer.__init__", side_effect=AssertionError("listener on import")) as listener, \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            spec.loader.exec_module(cls.s)
        start.assert_not_called()
        listener.assert_not_called()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s.DATA = self.tmp.name
        self.s._remote_posts.clear()
        self.s._remote_cache.clear()
        self.s._learn.update(served={}, rows={}, hits=[], credited={}, ctx=[])
        self.s._feeds.clear()
        self.s._shown.clear()
        self.s._trakt.clear()
        self.s._profiles.clear()
        self.s._model["m"] = None
        for name in ("tmdb", "openrouter"):
            patch = mock.patch.object(self.s, name, side_effect=AssertionError("network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        patch.start()
        self.addCleanup(patch.stop)

    def body(self, **changes):
        return {"key": "tmdb:123", "kind": "tv", "title": "Castle", "year": "2009", "who": "ben",
                "action": "love", "reasons": [], "row": rid('foryou', self.s.CONFIG), "undo": None, **changes}

    def write(self, **changes):
        return self.s.write_feedback(self.body(**changes))

    def undo(self, event):
        return self.s.write_feedback({k: v for k, v in {**event, "undo": event["id"]}.items() if k not in ("id", "t")})

    def http_server(self, require_socket=False, handler=None):
        try:
            server = self.s.ThreadingHTTPServer(("127.0.0.1", 0), handler or self.s.RemoteHandler)
        except PermissionError:
            if require_socket:
                self.skipTest("sandbox forbids loopback binding; real port-0 round trip needs a permitted host")
            return None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(cleanup)
        return server.server_address[1]

    def request(self, port, path, method="GET", body=None, headers=None, handler=None):
        if port is None:
            # Exercise the real HTTP parser/handler when the execution sandbox forbids sockets.
            raw = (body or "").encode()
            request_headers = {"Host": "localhost", "Content-Length": str(len(raw)), **(headers or {})}
            wire = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items())
            return self.raw_request(wire.encode() + b"\r\n" + raw, handler)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def raw_request(self, wire, handler=None):
        source, output = io.BytesIO(wire), io.BytesIO()
        connection = mock.Mock()
        connection.makefile.return_value = source
        connection.sendall.side_effect = output.write
        (handler or self.s.RemoteHandler)(connection, ("127.0.0.1", 0), mock.Mock())
        response_wire = output.getvalue()
        response = http.client.HTTPResponse(mock.Mock(makefile=lambda *a, **kw: io.BytesIO(response_wire)))
        response.begin()
        return response.status, dict(response.getheaders()), response.read()

    def test_token_persists_and_is_private(self):
        token = self.s.remote_token()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(self.s.remote_token(), token)
        path = Path(self.tmp.name, "remote.json")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.chmod(0o644)
        with mock.patch.object(self.s.os, "chmod", side_effect=PermissionError("foreign owner")):
            self.assertEqual(self.s.remote_token(), token)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_token_gate_and_route_isolation(self):
        port = self.http_server()
        token = self.s.remote_token()
        paths = ["/taste", "/taste/", "/taste/wrong/", "/taste/stats", "/taste/wrong/app.js",
                 f"/taste/{token}", f"/taste/{token}/stats", f"/taste/{token}/why/tmdb:1.json",
                 f"/taste/{token}/en-US/manifest.json", f"/taste/{token}/../stats"]
        with mock.patch.object(self.s.hmac, "compare_digest", wraps=self.s.hmac.compare_digest) as compare:
            for path in paths:
                status, headers, raw = self.request(port, path)
                self.assertEqual(status, 404)
                self.assertEqual(raw, b"")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertEqual(compare.call_count, len(paths) - 2)  # prefix/path rejection precedes token lookup.
        status, _, raw = self.request(port, "/taste/wrong/feedback.json", "POST", "{}")
        self.assertEqual((status, raw), (404, b""))

    def test_http_round_trip_on_port_zero(self):
        port = self.http_server(require_socket=True)
        self.check_round_trip(port)

    def test_http_handler_round_trip_and_assets(self):
        self.check_round_trip(None)

    def check_round_trip(self, port):
        root = "/taste/" + self.s.remote_token() + "/"
        for path, mime in (("", "text/html"), ("app.js", "text/javascript"), ("style.css", "text/css")):
            status, headers, raw = self.request(port, root + path)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(mime))
            self.assertEqual(headers["Content-Security-Policy"], self.s.REMOTE_CSP)
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertTrue(raw)
        status, _, raw = self.request(port, root + "feedback.json", "POST", json.dumps(self.body()),
                                      {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        event = json.loads(raw)["event"]
        self.assertRegex(event["id"], r"^[0-9a-f]{32}$")
        status, _, raw = self.request(port, root + "feedback.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["events"][0], {**event, "by": ["ben"]})
        stored = json.loads(Path(self.tmp.name, "feedback.jsonl").read_text())
        self.assertEqual(stored, event)
        undo = {k: v for k, v in {**event, "undo": event["id"]}.items() if k not in ("id", "t")}
        status, _, _ = self.request(port, root + "feedback.json", "POST", json.dumps(undo),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        _, _, raw = self.request(port, root + "feedback.json")
        self.assertEqual(json.loads(raw)["events"], [])

    def test_validation_rejects(self):
        bad = [None, [], {}, self.body(extra=True), self.body(id="forged"), self.body(t=0)]
        for field, values in {"key": [123, "tmdb:1234567890", "tmdb:١", "tmdb:1\n", "tt1"],
                              "kind": ["series", []], "who": ["together", []], "action": ["loved", {}],
                              "title": ["", " ", "a" * 301, "x\ny", []],
                              "year": [2009, "09", "2009\n", None],
                              "reasons": ["too slow", ["unknown"], [[]], ["too slow", "too slow"]],
                              "row": [rid('top10', self.s.CONFIG), [], "../stats"], "undo": [True, "missing"]}.items():
            bad += [self.body(**{field: value}) for value in values]
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.s.validate_feedback(body)
        self.s.validate_feedback(self.body(action="not_for_us", reasons=list(self.s.REMOTE_REASONS)))
        with self.assertRaises(ValueError):
            self.s.validate_feedback(self.body(reasons=["too slow"]))

    def test_undo_validation(self):
        event = self.write()
        with self.assertRaises(ValueError):
            self.s.write_feedback(self.body(undo="a" * 32))
        with self.assertRaises(ValueError):
            self.s.write_feedback(self.body(undo=event["id"], who="ana"))
        self.undo(event)
        with self.assertRaises(ValueError):
            self.undo(event)
        undo_event = self.undo(self.write(action="like"))
        with self.assertRaises(ValueError):
            self.s.write_feedback(self.body(undo=undo_event["id"]))

    def test_latest_wins_and_undo_restores_previous(self):
        love = self.write()
        like = self.write(action="like")
        self.assertEqual(self.s.feedback_entries(time.time())[0]["signal"], "liked")
        self.undo(like)
        self.assertEqual(self.s.feedback_entries(time.time())[0]["signal"], "loved")
        self.undo(love)
        self.assertEqual(self.s.feedback_entries(time.time()), [])

    def test_both_splits_and_individual_override_is_isolated(self):
        event = self.write(who="both")
        entries = self.s.feedback_entries(time.time())
        self.assertEqual(entries[0]["by"], ["ana", "ben"])
        dislike = self.write(who="ana", action="not_for_us", reasons=["too dark"])
        entries = self.s.feedback_entries(time.time())
        # Each person's latest action wins only their own slot.
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["by"] for e in entries], [["ana"], ["ben"]])
        self.assertEqual([e["signal"] for e in entries], ["disliked", "loved"])
        active = self.s.active_feedback()
        self.assertEqual(active[("tmdb:123", "ben")]["action"], "love")
        self.assertEqual(active[("tmdb:123", "ana")]["action"], "not_for_us")
        self.undo(event)
        self.assertEqual(self.s.feedback_entries(time.time())[0]["by"], ["ana"])
        self.undo(dislike)
        self.assertEqual(self.s.feedback_entries(time.time()), [])

    def test_signal_weights_decay_and_not_tonight(self):
        now = time.time()
        for action, signal, weight in (("love", "loved", 3), ("like", "liked", 2), ("meh", "watched", .3),
                                       ("seen", "watched", 1), ("not_for_us", "disliked", -3)):
            with mock.patch.object(self.s.time, "time", return_value=now):
                self.write(action=action)
            entry = self.s.feedback_entries(now)[0]
            self.assertEqual((entry["signal"], entry["weight"]), (signal, weight))
            self.assertEqual(entry["who"], "remote")
            self.assertEqual(entry["flags"], ["remote"])
            self.assertFalse(self.s.real_play(entry))
        self.write(action="love")
        self.assertEqual(self.s.feedback_entries(now + 89 * self.s.DAY)[0]["weight"], 3)
        self.assertEqual(self.s.feedback_entries(now + 91 * self.s.DAY)[0]["weight"], 1.5)
        self.assertEqual(self.s.feedback_entries(now + 121 * self.s.DAY)[0]["weight"], .75)
        self.write(action="not_tonight")
        self.assertEqual(self.s.feedback_entries(now)[0]["signal"], "loved")
        self.write(key="tmdb:456", action="not_tonight")
        with mock.patch.object(self.s, "add_person_loves", return_value=set()):
            self.assertNotIn("tmdb:456", self.s.build_profile([], [])["seen"])

    def test_profile_override_merges_attribution_preserves_resume(self):
        now = time.time()
        implicit = {"id": "tmdb:123", "type": "series", "title": "Castle", "year": "2009", "at": now * 1000,
                    "signal": "loved", "weight": 9, "last_episode": [2, 3], "genres": ["drama"]}
        self.write(action="not_for_us")
        with mock.patch.object(self.s, "add_person_loves", return_value=set()):
            p = self.s.build_profile([], [], [implicit])
        entry = p["entries"][0]
        self.assertEqual(entry["signal"], "disliked")
        self.assertEqual(entry["weight"], -3)
        self.assertEqual(entry["item"]["last_episode"], [2, 3])
        self.assertEqual(entry["genres"], ["Drama"])
        self.assertIn("tmdb:123", p["seen"])
        self.assertIn("tmdb:123", p["negative"])
        self.assertEqual(entry["who"], "owner")
        self.assertIn("remote", entry["flags"])
        with mock.patch.object(self.s, "add_person_loves", return_value=set()):
            other = self.s.build_profile([], [], [implicit], remote=False)
        self.assertEqual(other["entries"][0]["signal"], "loved")

    def test_shared_history_and_two_remote_opinions(self):
        item = {"id": "tmdb:123", "type": "series", "episodes_played": 3, "at": time.time() * 1000,
                "last_episode": [1, 3]}
        detail = {"id": 123, "name": "Castle", "first_air_date": "2009-01-01", "genres": []}
        self.write(who="ana", action="not_for_us")
        with mock.patch.object(self.s, "tmdb_details", return_value=("tv", detail)), \
                mock.patch.object(self.s, "add_person_loves", return_value=set()):
            p = self.s.build_profile([item], [])
        self.assertEqual(p["entries"][0]["signal"], "disliked")
        self.assertEqual(p["entries"][0]["by"], ["together", "ana"])
        self.write(who="ben", action="love")
        with mock.patch.object(self.s, "tmdb_details", return_value=("tv", detail)), \
                mock.patch.object(self.s, "add_person_loves", return_value=set()):
            p = self.s.build_profile([item], [])
        self.assertEqual(len(p["entries"]), 1)
        self.assertEqual(p["entries"][0]["signal"], "loved")
        self.assertEqual(p["entries"][0]["by"], ["together", "ana", "ben"])
        self.assertTrue(all(e["item"]["last_episode"] == [1, 3] for e in p["entries"]))

    def test_reasons_only_active_negatives(self):
        event = self.write(who="both", action="not_for_us", reasons=["too slow"])
        self.write(who="ana", action="not_for_us", reasons=["too dark", "too slow"])
        self.assertEqual(self.s.feedback_reasons(), {"tmdb:123": ["too dark", "too slow"]})
        self.write(action="love")
        self.assertEqual(self.s.feedback_reasons(), {"tmdb:123": ["too dark", "too slow"]})
        with self.assertRaises(ValueError):
            self.undo(event)  # Neither person's latest opinion is this superseded event.
        self.assertEqual(self.s.feedback_reasons(), {"tmdb:123": ["too dark", "too slow"]})

    def test_instant_row_filter_undo_and_18_hour_expiry(self):
        metas = [{"id": f"tmdb:{i}", "name": str(i), "type": "series"} for i in range(1, 17)]
        self.s._feeds[(rid('foryou', self.s.CONFIG), "de-DE")] = {"metas": {"tv": metas}}
        event = self.write(key="tmdb:1", who="ana", action="not_for_us")
        now = time.time()
        with mock.patch.object(self.s.time, "time", return_value=now - 17 * 3600):
            self.write(key="tmdb:2", action="not_tonight")
        with mock.patch.object(self.s, "tired_keys", return_value={}):
            served = self.s.row_metas(rid('foryou', self.s.CONFIG), "tv", "de-DE")
            self.assertEqual(served[-1]["id"], "tmdb:2")
            self.assertNotIn("tmdb:1", [m["id"] for m in served])
            self.assertEqual(metas[0]["id"], "tmdb:1")
            self.undo(event)
            served = self.s.row_metas(rid('foryou', self.s.CONFIG), "tv", "de-DE")
            self.assertEqual(served[0]["id"], "tmdb:1")
            with mock.patch.object(self.s.time, "time", return_value=now + 3601):
                served = self.s.row_metas(rid('foryou', self.s.CONFIG), "tv", "de-DE")
            self.assertEqual(served[1]["id"], "tmdb:2")
        self.s._feeds[(rid('top10', self.s.CONFIG), "de-DE")] = {"metas": {"series": metas}}
        self.write(key="tmdb:3", action="not_for_us")
        self.assertNotIn("tmdb:3", [m["id"] for m in self.s.row_metas(rid('top10', self.s.CONFIG), "series", "de-DE")])

    def test_hash_changes_on_feedback_and_undo(self):
        before = self.s.taste_hash("household", [])
        other = self.s.taste_hash("cal", [])
        event = self.write()
        after = self.s.taste_hash("household", [])
        self.assertNotEqual(before, after)
        self.assertEqual(other, self.s.taste_hash("cal", []))
        self.undo(event)
        self.assertNotEqual(after, self.s.taste_hash("household", []))

    def test_refresh_profiles_rebuilds_after_feedback_and_undo(self):
        with mock.patch.object(self.s, "active_profiles", return_value=["household"]), \
                mock.patch.object(self.s, "read_history", return_value=[]), \
                mock.patch.object(self.s, "add_person_loves", return_value=set()), \
                mock.patch.object(self.s, "learn_from_plays"), \
                mock.patch.object(self.s, "log_play_context"), mock.patch("builtins.print"):
            self.s.refresh_profiles()
            self.assertEqual(self.s._profiles["household"]["entries"], [])
            event = self.write(action="like")
            self.s.refresh_profiles()
            p = self.s._profiles["household"]
            self.assertEqual(p["entries"][0]["signal"], "liked")
            self.assertEqual(self.s.top_seeds(p)[0]["key"], "tmdb:123")
            self.assertEqual(self.s.seed_phrase(p["entries"][0]), "Because You Like Castle")
            self.assertIn("Ben explicitly liked: Castle", self.s.profile_text(p))
            self.undo(event)
            self.s.refresh_profiles()
            self.assertEqual(self.s._profiles["household"]["entries"], [])

    def test_hash_changes_when_remote_grace_expires(self):
        now = time.time()
        with mock.patch.object(self.s.time, "time", return_value=now):
            self.write()
        with mock.patch.object(self.s.time, "time", return_value=now + 89 * self.s.DAY):
            before = self.s.taste_hash("household", [])
        with mock.patch.object(self.s.time, "time", return_value=now + 91 * self.s.DAY):
            self.assertNotEqual(before, self.s.taste_hash("household", []))

    def test_compaction_failure_preserves_original(self):
        event = self.write()
        path = Path(self.tmp.name, "feedback.jsonl")
        raw = (json.dumps(event) + "\n") * 5000
        path.write_text(raw)
        with mock.patch.object(self.s.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.write(action="like")
        self.assertEqual(path.read_text(), raw)

    def test_compaction_is_bounded_and_atomic(self):
        event = self.write()
        path = Path(self.tmp.name, "feedback.jsonl")
        line = json.dumps(event) + "\n"
        path.write_text(line * 5000)
        with mock.patch.object(self.s.os, "replace", wraps=os.replace) as replace:
            newest = self.write(action="like")
            replace.assert_called_once()
        events = [json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1], newest)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_concurrent_append(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            events = list(pool.map(lambda i: self.write(key=f"tmdb:{i}"), range(24)))
        lines = Path(self.tmp.name, "feedback.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 24)
        self.assertEqual({json.loads(x)["id"] for x in lines}, {e["id"] for e in events})

    def test_http_validation_size_rate_limit_and_origin(self):
        port = self.http_server()
        root = "/taste/" + self.s.remote_token() + "/"
        for body, headers, expected in (("[]", {"Content-Type": "application/json"}, 400),
                                        ("not json", {"Content-Type": "application/json"}, 400),
                                        ("x" * 4097, {"Content-Type": "application/json"}, 413),
                                        ("{}", {}, 415),
                                        ("{}", {"Content-Type": "application/json", "Origin": "https://attacker.example"}, 403),
                                        ("{}", {"Content-Type": "application/json", "Origin": "https://["}, 403),
                                        ("{}", {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403)):
            status, headers, _ = self.request(port, root + "feedback.json", "POST", body, headers)
            self.assertEqual(status, expected)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.s._remote_posts.clear()
        with mock.patch.object(self.s.time, "monotonic", return_value=1000):
            for _ in range(60):
                status, _, _ = self.request(port, root + "feedback.json", "POST", "{}", {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
            status, _, _ = self.request(port, root + "feedback.json", "POST", "{}", {"Content-Type": "application/json"})
            self.assertEqual(status, 429)
        with mock.patch.object(self.s.time, "monotonic", return_value=1061):
            status, _, _ = self.request(port, root + "feedback.json", "POST", json.dumps(self.body()), {"Content-Type": "application/json"})
            self.assertEqual(status, 201)

    def test_search_reuses_search_without_ai(self):
        port = self.http_server()
        root = "/taste/" + self.s.remote_token() + "/"
        self.s.OR_KEY = "offline"
        self.addCleanup(setattr, self.s, "OR_KEY", "")
        item = {"id": 1, "title": "Castle", "release_date": "2009-01-01", "popularity": 10}
        with mock.patch.object(self.s, "search_both", return_value=([item], {})), \
                mock.patch.object(self.s, "genres", return_value={}), \
                mock.patch.object(self.s, "ai_future", side_effect=AssertionError("AI forbidden")), \
                mock.patch.object(self.s, "looks_like_title", side_effect=AssertionError("routing forbidden")):
            status, _, raw = self.request(port, root + "search.json?q=Castle")
        self.assertEqual(status, 200)
        self.assertEqual([x["type"] for x in json.loads(raw)["metas"]], ["movie", "series"])
        status, _, _ = self.request(port, root + "search.json?q=x")
        self.assertEqual(status, 400)

    def test_summary_uses_person_experiences(self):
        self.s._model["m"] = {"paff": {"ben": {"x:cozy": .5, "x:dread": -.2, "g:drama": 99},
                                                    "ana": {"x:banter": .8}},
                              "names": {"x:cozy": "Cozy mysteries", "x:banter": "Banter duo"}}
        summary = self.s.remote_history()["summary"]
        self.assertEqual(summary, {"ben": "Cozy mysteries", "ana": "Banter duo"})

    def test_ui_is_self_contained_and_uses_text_content(self):
        self.assertNotIn("innerHTML", self.s.REMOTE_JS)
        self.assertNotIn("http", self.s.REMOTE_HTML)
        self.assertIn("textContent", self.s.REMOTE_JS)
        self.assertIn("w342", self.s.REMOTE_JS)
        self.assertIn("localStorage", self.s.REMOTE_JS)

    def assert_remote_headers(self, headers):
        self.assertEqual(headers["Content-Security-Policy"], self.s.REMOTE_CSP)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_review_path_tricks_on_both_listeners(self):
        root = "/taste/" + self.s.remote_token() + "/"
        catalog_trick = f"/de-DE/catalog/movie/{rid('search', self.s.CONFIG)}/search=ignore%20previous%20instructions%2F..%2F..%2F..%2F..%2F..%2Ftaste%2Fx.json"
        why_trick = "/why/..%2F..%2Ftaste%2Fx.json"
        paths = [why_trick, catalog_trick, "/why/tmdb:1399%2F..%2F..%2F..%2Ftaste%2Fx.json",
                 "/stats/..%2F..%2Ftaste/x", root.upper(), root + "../stats", root + "/rows.json",
                 root + "\\rows.json", root + "é", root + "%C3%A9", root + "x" * 257,
                 "//taste/" + self.s.remote_token() + "/", root + "de-DE/manifest.json",
                 "/health", "/stats", "/de-DE/manifest.json", f"/de-DE/catalog/tv/{rid('foryou', self.s.CONFIG)}.json"]
        paths += [root + escape + "stats" for escape in ("%2F", "%2f", "%5C", "%5c", "%2E", "%2e")]
        with mock.patch.object(self.s, "search", return_value={"metas": []}) as search, \
                mock.patch.object(self.s, "log_search", return_value=(None, True)) as log, \
                mock.patch.object(self.s, "explain", return_value={}) as explain, \
                mock.patch.object(self.s, "stats", return_value={}), \
                mock.patch.object(self.s, "manifest", return_value={}), \
                mock.patch.object(self.s, "tired_keys", return_value={}):
            for path in paths:
                with self.subTest(listener="remote", path=path):
                    status, headers, body = self.request(None, path)
                    self.assertEqual((status, body), (404, b""))
                    self.assert_remote_headers(headers)
            search.assert_not_called(); log.assert_not_called(); explain.assert_not_called()
            # The internal listener retains its API; Caddy must target only the isolated remote port.
            for path in paths + [root, root + "rows.json", root + "feedback.json", root + "app.js"]:
                with self.subTest(listener="main", path=path):
                    status, _, body = self.request(None, path, handler=self.s.Handler)
                    expected = 200 if path in (why_trick, catalog_trick, paths[2], "/health", "/stats",
                                               "/de-DE/manifest.json", f"/de-DE/catalog/tv/{rid('foryou', self.s.CONFIG)}.json") else 404
                    self.assertEqual(status, expected)
                    self.assertNotIn(b"<!doctype", body)
            self.assertEqual(log.call_count, 1)
        for handler, path, expected in ((self.s.Handler, "/health", 200),
                                       (self.s.Handler, root, 404), (self.s.RemoteHandler, "/health", 404)):
            port = self.http_server(handler=handler)
            self.assertEqual(self.request(port, path, handler=handler)[0], expected)

    def test_headers_on_remote_errors_and_unsupported_methods(self):
        root = "/taste/" + self.s.remote_token() + "/"
        for method in ("HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"):
            for path, expected in ((root, 501), ("/health", 404), (root + "../stats", 404)):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(None, path, method)
                    self.assertEqual((status, body), (expected, b""))
                    self.assert_remote_headers(headers)
        for method, path, body, extra, expected in (
                ("GET", root + "rows.json?lang=no", None, {}, 400),
                ("POST", root + "feedback.json", "{}", {}, 415),
                ("POST", root + "feedback.json", "{}", {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, 400)):
            status, headers, _ = self.request(None, path, method, body, extra)
            self.assertEqual(status, expected)
            self.assert_remote_headers(headers)
        with mock.patch.object(self.s, "remote_rows", side_effect=OSError("unreadable")):
            # Parser-generated errors also pass through the remote header hook.
            status, headers, _ = self.request(None, root, method="GET", headers={"X-Long": "x" * 65537})
            self.assertEqual(status, 431)
            self.assert_remote_headers(headers)
        with mock.patch.object(self.s, "remote_token", side_effect=OSError("unreadable")):
            status, headers, _ = self.request(None, root)
            self.assertEqual(status, 503)
            self.assert_remote_headers(headers)
        status, headers, _ = self.request(None, root + "search.json?q=Castle")
        self.assertEqual(status, 502)
        self.assert_remote_headers(headers)
        for wire, expected in ((f"GET {root}\r\n".encode(), 400),
                               (b"GET /taste/x/ HTTP/nope\r\n\r\n", 400),
                               (b"broken\r\n", 400),
                               (b"GET /" + b"x" * 65537 + b" HTTP/1.0\r\n\r\n", 414)):
            status, headers, body = self.raw_request(wire)
            self.assertEqual((status, body), (expected, b""))
            self.assert_remote_headers(headers)

    def test_corrupt_line_matrix_keeps_catalogs_hashes_and_posts_working(self):
        event = self.write(action="not_for_us")
        path = Path(self.tmp.name, "feedback.jsonl")
        good = json.dumps(event).encode() + b"\n"
        bad = [b'{"t": 1, "id": "ab', b"", b"[]", b"null", b"\xff", b"[" * 1100,
               json.dumps({"t": 1, "id": "c" * 32, "key": "tmdb:2"}).encode()]
        for changes in ({"id": []}, {"id": "bad"}, {"t": True}, {"t": "now"}, {"t": float("nan")},
                        {"t": float("inf")}, {"t": -1}, {"key": []}, {"key": "tmdb:١"}, {"kind": "series"},
                        {"who": "unknown"}, {"action": []}, {"reasons": [[]]}, {"extra": 1}):
            bad.append(json.dumps({**event, **changes}).encode())
        root = "/taste/" + self.s.remote_token() + "/"
        metas = [{"id": "tmdb:123", "name": "Castle", "type": "series"},
                 {"id": "tmdb:2", "name": "Other", "type": "series"}]
        self.s._feeds[(rid('foryou', self.s.CONFIG), "de-DE")] = {"metas": {"tv": metas}}
        expected_hash = self.s.taste_hash("household", [])
        with mock.patch.object(self.s, "tired_keys", return_value={}):
            for extra in bad:
                with self.subTest(line=extra[:80]):
                    path.write_bytes(good + extra + b"\n")
                    self.assertEqual(self.s.remote_masks(), ({"tmdb:123"}, set()))
                    self.assertEqual(self.s.taste_hash("household", []), expected_hash)
                    self.assertEqual(self.s.row_metas(rid('foryou', self.s.CONFIG), "tv", "de-DE"), metas[1:])
                    self.assertEqual(self.request(None, f"/de-DE/catalog/tv/{rid('foryou', self.s.CONFIG)}.json", handler=self.s.Handler)[0], 200)
                    self.assertEqual(self.request(None, root + "rows.json")[0], 200)
                    status, headers, _ = self.request(None, root + "feedback.json", "POST",
                        json.dumps(self.body(key="tmdb:3")), {"Content-Type": "application/json"})
                    self.assertEqual(status, 201)
                    self.assert_remote_headers(headers)
                    self.assertEqual(len(path.read_text().splitlines()), 2)  # Repair on compaction.

    def test_unreadable_feedback_falls_back_safely(self):
        self.write()
        self.s._remote_cache.clear()
        with mock.patch("builtins.open", side_effect=PermissionError("unreadable feedback")), \
                mock.patch.object(self.s, "tired_keys", return_value={}):
            self.assertEqual(self.s.remote_masks(), (set(), set()))
            self.assertIsNone(self.s.feedback_hash(time.time()))
            self.assertEqual(self.s.row_metas(rid('foryou', self.s.CONFIG), "tv", "de-DE"), [])

    def test_fold_cache_invalidates_on_write_mtime_and_size(self):
        event = self.write()
        with mock.patch.object(self.s, "read_feedback", wraps=self.s.read_feedback) as read:
            state = self.s.feedback_state()
            for _ in range(5):
                self.s.remote_masks(); self.s.feedback_hash(time.time()); self.s.remote_history()
            self.assertEqual(read.call_count, 1)
            self.write(action="like")
            self.assertEqual(self.s.feedback_entries(time.time())[0]["signal"], "liked")
            self.assertEqual(read.call_count, 3)  # Write validation and one cache refill.
            self.assertEqual(state["active"][(event["key"], "ben")]["action"], "love")
            path = Path(self.tmp.name, "feedback.jsonl")
            st = path.stat()
            path.write_text(json.dumps({**event, "action": "meh"}) + "\n")
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # Size alone invalidates.
            self.assertEqual(self.s.feedback_entries(time.time())[0]["weight"], .3)
            st = path.stat()
            path.write_text(path.read_text().replace('"meh"', '"seen"').rstrip() + "\n")
            self.s.feedback_state()
            st = path.stat()
            path.write_text(path.read_text().replace('"seen"', '"love"'))  # Same size, new mtime.
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
            self.assertEqual(self.s.feedback_entries(time.time())[0]["weight"], 3)
            path.unlink()
            self.assertIsNone(self.s.feedback_hash(time.time()))

    def test_hash_invariance_no_file_not_tonight_undo_and_cancelling_toggles(self):
        signal = [self.s.recent_searches(), sorted((it["id"], it.get("signal")) for it in self.s.read_trakt()),
                  self.s.read_taste().get("household"), self.s.read_taste().get("people")]
        before = hashlib.sha1(json.dumps(signal).encode()).hexdigest()[:8]
        self.assertEqual(self.s.taste_hash("household", []), before)
        tonight = self.write(action="not_tonight")
        self.assertEqual(self.s.taste_hash("household", []), before)
        self.undo(tonight)
        self.assertEqual(self.s.taste_hash("household", []), before)
        love = self.write()
        loved = self.s.taste_hash("household", [])
        tonight = self.write(action="not_tonight")
        self.assertEqual(self.s.taste_hash("household", []), loved)
        self.undo(tonight)
        like = self.write(action="like")
        self.assertNotEqual(self.s.taste_hash("household", []), loved)
        self.undo(like)
        self.assertEqual(self.s.taste_hash("household", []), loved)
        self.write(action="like"); self.write(action="love")
        self.assertEqual(self.s.taste_hash("household", []), loved)
        with self.assertRaises(ValueError):
            self.undo(love)

    def test_fold_one_entry_per_title_and_preserves_tv_attribution(self):
        item = {"id": "tmdb:123", "type": "series", "episodes_played": 1,
                "at": (time.time() - 20 * self.s.DAY) * 1000}
        detail = {"id": 123, "name": "Castle", "genres": []}
        self.write()
        with mock.patch.object(self.s, "tmdb_details", return_value=("tv", detail)), \
                mock.patch.object(self.s, "add_person_loves", return_value=set()):
            p = self.s.build_profile([item], [])
        e = p["entries"][0]
        self.assertEqual((e["who"], e["by"]), ("tv", ["together", "ben"]))
        self.assertIs(e["item"], item)
        self.assertNotIn("Ana loves: Castle", self.s.profile_text(p))
        self.write(key="tmdb:7", kind="movie", title="Arrival")
        self.write(key="tmdb:7", kind="movie", title="Arrival", who="ana")
        with mock.patch.object(self.s, "add_person_loves", return_value=set()):
            p = self.s.build_profile([], [])
        self.assertEqual([e["title"] for e in self.s.top_seeds(p)].count("Arrival"), 1)

    def test_phone_opinion_preserves_tv_row_learning_and_context(self):
        now = time.time()
        detail = {"id": 77, "title": "Arrival", "genres": []}
        for action in (None, "love", "not_for_us"):
            with self.subTest(action=action):
                self.s._learn.update(served={}, rows={}, hits=[], credited={}, ctx=[])
                self.s._learn["served"]["tmdb:77"] = {"name": "Arrival", "rows": {rid('foryou', self.s.CONFIG): {
                    "first": now - 2 * self.s.DAY, "days": [self.s.local_now(now - 2 * self.s.DAY).strftime("%Y-%m-%d")]}}}
                if action:
                    self.write(key="tmdb:77", kind="movie", title="Arrival", who="both", action=action)
                item = {"id": "tmdb:77", "type": "movie", "progress": .95, "played": True, "at": (now - 3600) * 1000}
                with mock.patch.object(self.s, "tmdb_details", return_value=("movie", detail)), \
                        mock.patch.object(self.s, "add_person_loves", return_value=set()), mock.patch("builtins.print"):
                    p = self.s.build_profile([item], [])
                    self.s.learn_from_plays(p); self.s.log_play_context(p)
                self.assertEqual(set(self.s._learn["credited"]), {"tmdb:77"})
                self.assertEqual(len(self.s._learn["ctx"]), 1)
                # Opening a title with a phone love must not fabricate a play.
                if action:
                    item.update(progress=0, played=False)
                    self.assertFalse(self.s.real_play(p["entries"][0]))

    def test_compaction_keeps_folded_opinions_partial_both_and_live_tonight(self):
        now = time.time()
        with mock.patch.object(self.s.time, "time", return_value=now - 20 * 3600):
            expired = self.write(key="tmdb:2", action="not_tonight")
        both = self.write(who="both")
        separate = self.write(who="ana", action="like")
        undone = self.write(key="tmdb:3")
        self.undo(undone)
        live = self.write(key="tmdb:4", action="not_tonight")
        path = Path(self.tmp.name, "feedback.jsonl")
        path.write_text(path.read_text() + (json.dumps(expired) + "\n") * 5000)
        newest = self.write(key="tmdb:5")
        stored = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual({e["id"] for e in stored}, {both["id"], separate["id"], live["id"], newest["id"]})
        self.assertEqual(next(e for e in stored if e["id"] == both["id"])["who"], "ben")
        self.assertEqual(self.s.remote_masks()[1], {"tmdb:4"})
        history = self.s.remote_history()["events"]
        event = next(e for e in history if e["id"] == both["id"])
        self.undo({k: v for k, v in event.items() if k != "by"})
        self.assertNotIn((both["key"], "ben"), self.s.active_feedback())
        self.assertEqual(self.s.active_feedback()[(both["key"], "ana")]["action"], "like")

    def test_history_contains_only_current_people_and_rejects_stale_undo(self):
        first = self.write()
        both = self.write(who="both", action="like")
        newest = self.write(who="ana", action="not_for_us")
        history = self.s.remote_history()["events"]
        self.assertEqual({e["id"] for e in history}, {both["id"], newest["id"]})
        self.assertEqual(next(e for e in history if e["id"] == both["id"])["by"], ["ben"])
        with self.assertRaises(ValueError):
            self.undo(first)
        self.undo(newest)
        self.assertEqual(self.s.active_feedback()[("tmdb:123", "ana")]["action"], "like")

    def test_body_deadline_bounds_total_time_and_returns_headers(self):
        handler = mock.Mock(timeout=5, rfile=mock.Mock())
        handler.rfile.read1.side_effect = [b"a", b"b", b"c"]
        with mock.patch.object(self.s.time, "monotonic", side_effect=[0, 0, 2, 5.1]):
            with self.assertRaises(TimeoutError):
                self.s.read_remote_body(handler, 3)
        self.assertEqual(handler.rfile.read1.call_count, 2)
        self.assertEqual(handler.connection.settimeout.call_args_list, [mock.call(5), mock.call(3)])
        handler.rfile.read1.side_effect = [b"abc"]
        with mock.patch.object(self.s.time, "monotonic", side_effect=[0, 0, 6]):
            with self.assertRaises(TimeoutError):
                self.s.read_remote_body(handler, 3)
        root = "/taste/" + self.s.remote_token() + "/feedback.json"
        with mock.patch.object(self.s, "read_remote_body", side_effect=TimeoutError):
            status, headers, _ = self.request(None, root, "POST", "{}", {"Content-Type": "application/json"})
            self.assertEqual(status, 408)
            self.assert_remote_headers(headers)
        self.assertEqual(self.s.RemoteHandler.timeout, 5)

    def test_remote_seen_card_does_not_claim_watch_date(self):
        now = time.time()
        with mock.patch.object(self.s.time, "time", return_value=now - 100 * self.s.DAY):
            self.write(kind="movie", action="seen")
        with mock.patch.object(self.s, "add_person_loves", return_value=set()):
            self.s._profiles["household"] = self.s.build_profile([], [])
        detail = {"id": 123, "title": "Castle", "release_date": "2009-01-01", "genre_ids": []}
        with mock.patch.object(self.s, "details_pair", return_value=(detail, detail)), \
                mock.patch.object(self.s, "genres", return_value={}), \
                mock.patch.object(self.s, "read_taste", return_value={}):
            row = self.s.rewatch_feed("de-DE")
        description = row["movie"][0]["description"]
        self.assertIn("Marked seen", description)
        self.assertNotIn("Last seen", description)

    def test_startup_uses_two_listeners_and_remote_port_env(self):
        import runpy
        Path(self.tmp.name, "skald.json").write_text(json.dumps(self.s.LEGACY))
        with mock.patch.dict(os.environ, {"TMDB_API": "offline", "DATA_DIR": self.tmp.name,
                                          "PORT": "8080", "REMOTE_PORT": "8091", "OPENROUTER_API_KEY": ""}), \
                mock.patch("sys.argv", ["server.py"]), \
                mock.patch("http.server.ThreadingHTTPServer") as listeners, \
                mock.patch("threading.Thread.start"), mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "server.py"), run_name="__main__")
        self.assertEqual([call.args[0] for call in listeners.call_args_list], [("0.0.0.0", 8080), ("0.0.0.0", 8091)])
        self.assertEqual([call.args[1].__name__ for call in listeners.call_args_list], ["Handler", "RemoteHandler"])

    def test_5000_event_performance_reports_before_and_cached_after(self):
        from collections import deque

        path = Path(self.tmp.name, "feedback.jsonl")
        now = time.time()

        def legacy_active():
            with path.open() as fh:
                events = list(deque((json.loads(line) for line in fh), maxlen=5000))
            return self.s.active_feedback(events)

        def legacy_masks():
            blocked, tonight = set(), set()
            for (key, _), e in legacy_active().items():
                if e["action"] == "not_for_us":
                    blocked.add(key)
                elif e["action"] == "not_tonight" and now - e["t"] < 18 * 3600:
                    tonight.add(key)
            return blocked, tonight

        def legacy_hash():
            with path.open("rb") as fh:
                h = hashlib.file_digest(fh, "sha256")
            entries = {}
            for (key, person), e in legacy_active().items():
                if e["id"] in entries:
                    entries[e["id"]]["by"].append(person)
                    continue
                signal, weight, age = self.s.feedback_weight(e, now)
                entries[e["id"]] = {"key": key, "id": int(key[5:]), "kind": e["kind"],
                    "stype": "movie" if e["kind"] == "movie" else "series", "title": e["title"], "year": e["year"],
                    "signal": signal, "weight": weight, "age": age, "item": {}, "who": "remote", "by": [person],
                    "flags": ["remote"], "genres": []}
            h.update(json.dumps([(e["key"], e["by"], e["weight"]) for e in entries.values()]).encode())
            return h.hexdigest()

        def measure(fn):
            start = time.perf_counter()
            for _ in range(5):
                fn()
            ms = (time.perf_counter() - start) * 200
            tracemalloc.start()
            try:
                fn()
                peak = tracemalloc.get_traced_memory()[1] / 2**20
            finally:
                tracemalloc.stop()
            return ms, peak

        for mode in ("typical", "worst"):
            with self.subTest(mode=mode):
                with path.open("w") as fh:
                    for i in range(5000):
                        event = {**self.body(key=f"tmdb:{100000000 + i}",
                            title="😀" * 300 if mode == "worst" else f"Some Title {i}",
                            who="both" if mode == "worst" else "ben",
                            action="not_for_us" if mode == "worst" else "love",
                            reasons=list(self.s.REMOTE_REASONS) if mode == "worst" else []), "t": now - i, "id": f"{i:032x}"}
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self.s._remote_cache.clear()
                start = time.perf_counter()
                self.s.feedback_state()
                cold_ms = (time.perf_counter() - start) * 1000
                self.s._remote_cache.clear()
                tracemalloc.start()
                self.s.feedback_state()
                retained, cold_peak = (n / 2**20 for n in tracemalloc.get_traced_memory())
                tracemalloc.stop()
                self.s.feedback_hash(now)  # Warm the weight hash until the next decay boundary.
                for label, old, new in (("masks", legacy_masks, self.s.remote_masks),
                                         ("hash", legacy_hash, lambda: self.s.feedback_hash(now))):
                    before, after = measure(old), measure(new)
                    print(f"\n5000 {mode} {label}: before {before[0]:.2f} ms / {before[1]:.2f} MiB; "
                          f"cached after {after[0]:.3f} ms / {after[1]:.3f} MiB")
                print(f"5000 {mode} cache: cold {cold_ms:.2f} ms; retained {retained:.2f} MiB; cold peak {cold_peak:.2f} MiB")
                self.assertEqual(len(self.s.active_feedback()), 10000 if mode == "worst" else 5000)

    def test_compaction_at_capacity_preserves_active_opinions(self):
        path = Path(self.tmp.name, "feedback.jsonl")
        now = time.time()
        with path.open("w") as fh:
            for i in range(5000):
                fh.write(json.dumps({**self.body(key=f"tmdb:{i}"), "t": now, "id": f"{i:032x}"}) + "\n")
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.write(key="tmdb:5001")
        self.assertEqual(path.read_bytes(), original)
        self.write(key="tmdb:1", action="like")
        self.assertEqual(len(path.read_text().splitlines()), 5000)
        self.assertEqual(self.s.active_feedback()[("tmdb:1", "ben")]["action"], "like")

    def test_snapshot_rows_on_temporary_copy(self):
        from snapshot_support import require_snapshot
        snapshot = require_snapshot()
        if not snapshot.is_dir():
            self.skipTest("local snapshot unavailable")
        for name in ("feeds.json", "shown.json", "taste.json", "legacy.json", "skald.json", ".skald-format"):
            if (snapshot/name).is_file():
                shutil.copy2(snapshot/name, Path(self.tmp.name, name))
        from skald import setup
        context = setup.server(Path(self.tmp.name))
        s = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.enterContext(mock.patch.object(self, 's', s))

        for name, store in (("feeds.json", self.s._feeds), ("shown.json", self.s._shown)):
            for key, value in json.loads(Path(self.tmp.name, name).read_text()).items():
                cid, lang = key.split("|", 1)
                store[(cid, lang)] = value
        rows = self.s.remote_rows("de-DE")["rows"]
        expected = [cid for cid, _, _ in self.s.feed_rows("de-DE") if cid in self.s.REMOTE_ROWS]
        self.assertEqual([row["id"] for row in rows], expected)
        self.assertGreater(sum(len(row["metas"]) for row in rows), 100)
        self.assertTrue(all(len(row["metas"]) <= 15 for row in rows))
        for row in rows:
            names = self.s.feed(row["id"], "de-DE").get("names") or {}
            dynamic = names.get("title") or names.get("mixed")
            if dynamic:
                self.assertEqual(row["name"], dynamic)
        port = self.http_server()
        root = "/taste/" + self.s.remote_token() + "/"
        status, _, raw = self.request(port, root + "rows.json")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["rows"]), len(rows))


if __name__ == "__main__":
    unittest.main()
