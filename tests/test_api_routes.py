"""Registry-wide guard and privacy contracts; adding a route needs only an example."""
from skald.members import rid
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from decide_kit import check_contract, close_server, load_server, synthetic_model
from test_api import raw_request


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        synthetic_model(self.s)
        self.tokens = {name: self.s.new_client("canary-" + name, scopes, person) for name, scopes, person in (
            ("all", ["read", "decide", "events"], None), ("read", ["read"], None),
            ("decide", ["decide"], None), ("events", ["events"], None),
            ("ben", ["read", "decide", "events"], "ben"),
            ("ana", ["read", "decide", "events"], "ana"))}
        p = patch.dict(os.environ, {"API_LOOPBACK_OPEN": "1"})
        p.start()
        self.addCleanup(p.stop)

    def request(self, path, method="GET", body=None, client="all", headers=None, peer="127.0.0.1"):
        self.s._api_rates.clear()
        fields = {"Authorization": "Bearer " + self.tokens[client]} if client else {}
        code, _, raw, _ = raw_request(self.s, path, method, body, {**fields, **(headers or {})}, peer)
        return code, json.loads(raw)

    def example(self, key, spec, method):
        example = spec["example"]
        path = "/api/v1/" + key.replace("*", example.get("tail", ""))
        if example.get("query"):
            path += "?" + urllib.parse.urlencode(example["query"])
        return path, example.get("body") if method == "POST" else None


class ApiRoutesTests(ApiCase):
    def contract(self):
        for key, spec in self.s.API_ROUTES.items():
            self.assertIsInstance(spec["example"], dict)
            for method, (scope, view, status) in spec["methods"].items():
                with self.subTest(route=key, method=method):
                    self.assertIn(scope, ("read", "decide", "events"))
                    path, body = self.example(key, spec, method)
                    self.assertEqual(self.request(path, method, body, client=None, peer="172.18.0.5")[0], 401)
                    wrong = "events" if scope != "events" else "read"
                    self.assertEqual(self.request(path, method, body, client=wrong)[0], 403)
                    self.assertEqual(self.request(path, "PUT", body)[0], 405)
                    self.assertEqual(self.request(path + ("&" if "?" in path else "?") + "unknown=x", method, body)[0], 400)
                    if spec["query"]:
                        param = next(iter(spec["query"]))
                        query = {**spec["example"].get("query", {}), param: spec["example"].get("query", {}).get(param, "ben")}
                        repeat = path.split("?")[0] + "?" + urllib.parse.urlencode(query) + "&" + param + "=ben"
                    else:
                        repeat = path + "?unknown=x&unknown=x"
                    self.assertEqual(self.request(repeat, method, body)[0], 400)
                    code, _ = self.request(path, method, body)
                    self.assertEqual(code, status)

    def test_registry(self):
        self.assertEqual(len(self.s.API_ROUTES), 12)
        self.contract()

    def test_routes_self_description(self):
        code, data = self.request("/api/v1/routes", client="read")
        self.assertEqual(code, 200)
        self.assertEqual(data, self.s.routes_view(None))
        self.assertEqual([r["path"] for r in data["routes"]], ["/api/v1/" + k for k in sorted(self.s.API_ROUTES)])
        for row in data["routes"]:
            spec = self.s.API_ROUTES[row["path"].removeprefix("/api/v1/")]
            self.assertEqual(set(row), {"path", "methods", "query", "tail", "example"})
            self.assertEqual(row["methods"], {m: s[0] for m, s in spec["methods"].items()})
            self.assertEqual(row["query"], sorted(spec["query"]))
            self.assertEqual(row["tail"], "custom" if callable(spec["tail"]) else spec["tail"])
            self.assertEqual(row["example"], spec["example"])

    def test_toy_read_route_three_lines(self):
        with patch.dict(self.s.API_ROUTES, clear=False):
            self.s.api("toy", get=("read", lambda req: {"ok": True}), example={})
            self.contract()

    def test_toy_decision_four_lines_and_twenty_rows(self):
        path = Path(self.temp.name, "toy.jsonl")
        path.write_text("".join(json.dumps({"in": {"ok": True}, "expect": {"ok": True}, "recorded": None, "note": "toy"}) + "\n" for _ in range(20)))
        with patch.dict(self.s.DECISIONS, clear=False):
            self.s.decision("toy", ver=1, lane=None, cap=None, ttl=self.s.DAY,
                inp={"ok": bool}, out={"ok": bool}, norm=lambda x: x, rule=lambda x, p: (x, 1),
                params={}, llm=None, fallback=lambda x, e: ({"ok": False}, 0),
                requires=(), serving=True, min_acc=1, fixtures=str(path))
            check_contract(self, self.s, "toy")
            self.assertEqual(self.request("/api/v1/decide/toy", "POST", {"in": {"ok": True}})[0], 200)
            self.assertIn("toy", [t["type"] for t in self.request("/api/v1/decide")[1]["types"]])

    def test_query_and_guard_order_and_internal_type(self):
        for query in ("kind=bad", "person=bad&kind=movie", "kind=movie&kind=tv", "kind", "kind=movie&x=y",
                      "kind=movie&" + "x=" + "a" * 256, "&".join("kind=movie" for _ in range(9))):
            self.assertEqual(self.request("/api/v1/titles/tmdb:900001?" + query)[0], 400)
        self.assertEqual(self.request("/api/v1/titles/tmdb:900001")[1], {"error": "kind"})
        self.assertEqual(self.request("/api/v1/titles/tmdb:999999?kind=movie")[0], 404)
        self.assertEqual(self.request("/api/v1/titles/tmdb:bad?kind=movie")[0], 404)
        self.assertEqual(self.request("/api/v1/decide/mood_pick", "GET")[0], 404)
        self.assertEqual(self.request("/api/v1/nope", "POST", {}, client="read")[0], 404)
        self.assertEqual(self.request("/api/v1/decide/route_query", "GET", client="read")[0], 405)
        self.assertEqual(self.request("/api/v1/nope", client=None, peer="172.18.0.5")[0], 401)
        self.assertEqual(self.request("/api/v1/nope", headers={"Origin": ""})[0], 403)

    def test_tail_regex_can_include_slashes(self):
        with patch.dict(self.s.API_ROUTES):
            self.s.api("memory/*", get=("read", lambda req: {"tail": req["tail"]}), tail=r"[a-f0-9]{32}/confirm",
                       example={"tail": "a" * 32 + "/confirm"})
            self.assertEqual(self.request("/api/v1/memory/" + "a" * 32 + "/confirm")[0], 200)
            self.assertEqual(self.request("/api/v1/memory/" + "a" * 32 + "/reject")[0], 404)

    def test_view_bugs_are_internal_and_log_only_route_and_type(self):
        for key, tail in (("broken", None), ("broken/*", "private-tail")):
            with patch.dict(self.s.API_ROUTES), patch("builtins.print") as log:
                self.s.api(key, get=("read", lambda req: {}["private-message"]),
                           query={"secret": str}, example={})
                path = "/api/v1/" + key.replace("*", tail or "") + "?secret=private-query"
                self.assertEqual(self.request(path), (500, {"error": "internal"}))
                log.assert_called_once_with(f"api {key} failed: KeyError", flush=True)
        self.assertEqual(self.request("/api/v1/titles/tmdb:999999?kind=movie"),
                         (404, {"error": "not found"}))

    def test_every_get_privacy_matrix(self):
        s = self.s
        s.write_session({"who": "ana"}, "phone")
        metas = [{"id": e["key"], "type": "movie", "name": e["title"], "description": "AnaTitleSentinel"}
                 for e in s._profiles["household"]["entries"]]
        report = {"who": {"ben": {"plays": 3}, "ana": {"plays": 7}, "both": {"plays": 2}},
                  "rows": {rid('foryou', s.CONFIG): {}, rid('cal', s.CONFIG): {}, rid('ana', s.CONFIG): {}}}
        with patch.object(s, "row_metas", return_value=metas), patch.object(s, "metrics", side_effect=lambda **kw: copy.deepcopy(report)), \
                patch.object(s, "feed_rows", return_value=[(rid('foryou', s.CONFIG), s.MIX, "AnaTitleSentinel"),
                                                          (rid('ana', s.CONFIG), s.MIX, "For Ana"), (rid('cal', s.CONFIG), s.MIX, "Cal")]):
            for key, spec in s.API_ROUTES.items():
                if "GET" not in spec["methods"]:
                    continue
                for client in (None, "read", "ben", "ana"):
                    path, body = self.example(key, spec, "GET")
                    if key == "interview/next" and client in self.s.PEOPLE:
                        path = "/api/v1/interview/next?person=" + client
                    code, data = self.request(path, client=client)
                    self.assertEqual(code, 200, (key, client))
                    text = json.dumps(data).lower()
                    for forbidden in ('"cal"', "household", "canary-"):
                        self.assertNotIn(forbidden, text, (key, client))
                    if client in ("ben", "ana"):
                        other = "Ana" if client == "ben" else "Ben"
                        for sentinel in (other + "TitleSentinel", other + "CreatorSentinel"):
                            self.assertNotIn(sentinel.lower(), text, (key, client))
                        self.assertNotIn('"' + other.lower() + '":', text, (key, client))

    def test_shared_session_get_and_post_hide_private_plays(self):
        last = {"id": "a" * 32, "who": "both", "since": 1, "until": 2, "src": "phone", "plays": 2,
                "titles": ["AnaTitleSentinel", "BenTitleSentinel"]}
        with patch.object(self.s, "session_view", side_effect=lambda *_: {"active": None, "last": copy.deepcopy(last)}), \
                patch.object(self.s, "session_plays", return_value=self.s._profiles["household"]["entries"][:2]):
            for method, body in (("GET", None), ("POST", {"who": "both"})):
                code, data = self.request("/api/v1/session", method, body, client="ben")
                self.assertEqual(code, 200 if method == "GET" else 201)
                view = data if method == "GET" else data["session"]
                self.assertEqual(view["last"]["titles"], ["BenTitleSentinel"])
                self.assertEqual(view["last"]["plays"], 1)

    def test_bound_person_across_views_and_decision_items(self):
        for path in ("titles/tmdb:900001?kind=movie&", "profile?", "rows?"):
            for person, status in (("ana", 403), ("together", 200), ("ben", 200)):
                self.assertEqual(self.request("/api/v1/" + path + "person=" + person, client="ben")[0], status)
        inp = {"key": "tmdb:900001", "kind": "movie"}
        self.assertEqual(self.request("/api/v1/decide/explain_pick", "POST", {"in": {**inp, "person": "ana"}}, client="ben")[0], 403)
        code, reply = self.request("/api/v1/decide/explain_pick", "POST", {"in": inp}, client="ben")
        self.assertEqual(code, 200)
        self.assertEqual(reply["source"], "rule")
        self.assertEqual(self.request("/api/v1/profile", client="ben")[1]["person"], "ben")
        self.assertEqual(self.request("/api/v1/titles/tmdb:900003?kind=movie", client="ben")[0], 404)


if __name__ == "__main__":
    unittest.main()
