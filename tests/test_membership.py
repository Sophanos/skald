"""Data-driven members, n-ary scoring, maturity and read-only guests."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, synthetic_model
from test_api_routes import ApiCase
from test_api import raw_request

ROOT = Path(__file__).resolve().parents[1]


class MembershipTests(ApiCase):
    def test_registry_and_nary(self):
        s = self.s
        self.assertTrue(all(s.who_ok(p) for p in s.PEOPLE))
        self.assertFalse(s.who_ok(s.GUEST))
        self.assertTrue(s.who_ok(s.GUEST, s.GUEST))
        self.assertEqual(set(s.GUEST_ROW_POLICY), set(s.DEDUPE_ORDER))
        m = s.get_model(False)
        for key in ("tmdb:900001", "tmdb:900005"):
            values = [s.score_key(m, key, p, "movie")[0] for p in s.PEOPLE]
            self.assertEqual(s.score_key(m, key, "together", "movie")[0], sum(values)/2-.25*abs(values[0]-values[1]))
        with patch.object(s, "PEOPLE", (*s.PEOPLE, "third")):
            m["nexp"]["third"] = 0
            self.assertIsNotNone(s.score_key(m, "tmdb:900001", "together", "movie")[0])
            self.assertIn("tmdb:900001", s.week_scores([{"id": "tmdb:900001", "type": "movie"}]))
            self.assertEqual(s.observer({"who": "both", "by": list(s.PEOPLE)}), "self")
            self.assertEqual(s.observer({"who": "both", "by": list(s.PEOPLE[:2])}), "other")
        from eval.names_census import census
        for path in [ROOT / "server.py", *sorted((ROOT / "skald").rglob("*.py"))]:
            counts = census(path).get("validator-enum", {})
            self.assertEqual(counts.get("ben", 0)+counts.get("ana", 0), 0, path.name)

    def test_third_member_at_all_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(ROOT / "skald", root / "skald", ignore=shutil.ignore_patterns("__pycache__"))
            source = (ROOT / "server.py").read_text()
            (root / "server.py").write_text(source)
            data = root / "data"
            data.mkdir()
            (data / "profile.json").write_text('{"entries": []}')
            config = copy.deepcopy(self.s.LEGACY)
            config["household"]["members"].append("third")
            config["household"]["labels"]["third"] = "Third"
            (data / "skald.json").write_text(json.dumps(config))
            code = '''from skald.engine import Engine
from skald.__main__ import parser
from skald import intake
from skald.ingest import Candidate
from skald.ingest.netflix import parse
from pathlib import Path
with Engine("data") as e:
 s=e._server
 assert "third" in s.PEOPLE
 assert s.validate_session({"who":"third"})["who"] == "third"
 body={"who":"third","key":"tmdb:1","kind":"movie","title":"Synthetic","action":"love"}
 s.validate_feedback(body)
 s.new_client("third-client", ["read","decide","events"], "third")
 assert s.api_person("third",None) == "third"
 s.check(s.DECISIONS["explain_pick"]["inp"], {"key":"tmdb:1","kind":"movie","person":"third"})
 s.api_decide({"body":{"in":{"key":"tmdb:1","kind":"movie"},"person":"third"},"client":{"name":"third-client"},"viewer":"third","tail":"explain_pick"})
 assert parser().parse_args(["decide","explain_pick","--person","third"]).person == "third"
 assert parser().parse_args(["onboard","--person","third"]).person == "third"
 assert parser().parse_args(["taste","show","--person","third"]).person == "third"
 assert intake._mapping(["local=third"])["local"] == "third"
 intake._candidates("plain",[Candidate("Synthetic", person="third")])
 assert intake.cross_source([],person="third")[0] == []
 Path("data/netflix.csv").write_text("Profile Name,Title,Date\\nLocal,Synthetic,2026-10-11\\n")
 assert parse("data/netflix.csv", profile_map={"Local":"third"})[0].person == "third"
 e._person("third")
 s.score_key(e._model,"tmdb:1","together","movie")
 s.week_scores([])
'''
            result = subprocess.run([sys.executable, "-c", code], cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "DATA_DIR": str(data)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_maturity_and_single_percentile_gate(self):
        s, m = self.s, self.s.get_model(False)
        for n, state in ((0, "too_new"), (4, "too_new"), (19, "too_new"), (20, "learning"), (39, "learning"), (40, "ready")):
            m["nexp"]["ana"] = n
            self.assertEqual(s.maturity(m, "ana"), {"state": state, "explicit": n, "need": 20})
            view = s.title_view("tmdb:900001", "movie", "ana")
            self.assertEqual(view["match"] is None, n < 20)
            self.assertEqual(s.match_pct(m, .1, "ana") is None, n < 20)
        m["nexp"] = {"ben": 19, "ana": 0}
        self.assertEqual(s.maturity(m)["state"], "too_new")
        m["nexp"]["ben"] = 20
        self.assertEqual(s.maturity(m, "together")["state"], "ready")
        self.assertIsNone(s.match_pct(m, 1, s.GUEST))
        shared = {"nexp": {p: 15 for p in s.PEOPLE}, "ent": {str(i): ("Synthetic", s.PEOPLE) for i in range(15)}}
        self.assertEqual(s.maturity(shared)["explicit"], 15)
        self.assertEqual(s.maturity(shared)["state"], "too_new")

    def test_person_score_limits_and_switch(self):
        s, m = self.s, self.s.get_model(False)
        house = s.score_key(m, "tmdb:900001", kind="movie")[0]
        raw = s.score_key(m, "tmdb:900001", "ana", "movie")[0]
        for n in (0, 10, 20, 40, 309):
            m["nexp"]["ana"] = n
            self.assertEqual(s.person_score(m, "tmdb:900001", "ana", "movie")[0], raw)
        m["paff"]["ana"] = {}
        raw = s.score_key(m, "tmdb:900001", "ana", "movie")[0]
        for n in (0, 10, 20, 40, 309):
            m["nexp"]["ana"] = n
            lam = min(1, n/40)
            self.assertEqual(s.person_score(m, "tmdb:900001", "ana", "movie")[0],
                             raw if lam == 1 else lam*raw+(1-lam)*house)
        with patch.object(s, "HOUSEHOLD_PRIOR", False):
            self.assertEqual(s.person_score(m, "tmdb:900001", "ana", "movie"), s.score_key(m, "tmdb:900001", "ana", "movie"))

    def test_display_cache_isolated_and_invalidated(self):
        s = self.s
        first = s.title_view("tmdb:900001", "movie", "ana")
        first["reasons"].append("Private mutation")
        self.assertNotIn("Private mutation", s.title_view("tmdb:900001", "movie", "ana")["reasons"])
        s.get_model(False)["nexp"]["ana"] = 4
        self.assertIsNone(s.title_view("tmdb:900001", "movie", "ana")["match"])
        s._facets["tmdb:900001"]["title"] = "New synthetic title"
        self.assertEqual(s.title_view("tmdb:900001", "movie", "ana")["title"], "New synthetic title")

    def test_guest_clients_views_and_denials(self):
        s = self.s
        self.tokens["guest"] = s.new_client("guest-read", ["read", "decide"], "guest")
        with self.assertRaises(ValueError):
            s.new_client("guest-events", ["events"], "guest")
        self.assertIn("guest", [c["person"] for c in s._clients_read()])
        view = self.request("/api/v1/titles/tmdb:900001?kind=movie", client="guest")[1]
        self.assertEqual((view["match"], view["confidence"], view["reasons"], view["experiences"]), (None, None, [], []))
        self.assertEqual(view["maturity"]["state"], "guest")
        self.assertEqual(self.request("/api/v1/titles/tmdb:900004?kind=movie", client="guest")[0], 404)
        for route in ("profile", "taste", "memory", "session", "metrics", "interview/next?person=ben"):
            self.assertEqual(self.request("/api/v1/"+route, client="guest")[0], 403, route)
        with self.assertRaises(PermissionError):
            s.validate_feedback({"who": "guest", "key": "tmdb:1", "kind": "movie", "title": "Synthetic", "action": "love"})
        with self.assertRaises(PermissionError):
            s.intake("interview", [], {"viewer": "guest", "query": {}})

    def test_guest_sessions_teach_nothing(self):
        s = self.s
        before = copy.deepcopy(s._learn)
        s.write_session({"who": "guest"}, "phone")
        active = s.session_at(s.time.time())
        items = [{"id": f"tmdb:{i}", "type": "series", "at": int((active["t"]+1)*1000), "episodes_played": 20} for i in (1, 2, 3)]
        self.assertEqual(s.attribute(items[0])[:2], ("guest", ["guest"]))
        with patch.object(s, "tmdb_details", side_effect=lambda it: ("tv", {"id": int(it["id"][5:]), "name": "Guest play"})):
            events = list(s.tv_events(s.time.time(), items))
        entries, _, seen = s.fold_events([("tv", events)], s.time.time())
        self.assertEqual((entries, seen), ([], set()))
        self.assertEqual(s.taste_hash("household", items), s.taste_hash("household", []))
        self.assertEqual(s._learn, before)
        self.assertEqual(s.validate_session({"who": "guest"})["who"], "guest")

    def test_phone_guest_feedback_and_read_only_rows(self):
        s = self.s
        with patch.object(s, "remote_token", return_value="fixture-token"):
            root = "/taste/fixture-token/"
            s.write_session({"who": "both"}, "phone")
            with patch.object(s, "session_plays", return_value=[{"title": "Private together title"}]):
                code, _, raw, _ = raw_request(s, root+"session.json", "POST", {"who": "guest"}, handler=s.RemoteHandler)
                self.assertEqual(code, 201)
                self.assertIsNone(json.loads(raw)["session"]["last"])
                code, _, raw, _ = raw_request(s, root+"session.json", handler=s.RemoteHandler)
                self.assertEqual(code, 200)
                self.assertIsNone(json.loads(raw)["last"])
                self.assertNotIn("Private together title", raw.decode())
                code, _, raw, _ = raw_request(s, root+"session.json", "POST", {"who": "off"}, handler=s.RemoteHandler)
                self.assertEqual(code, 201)
                self.assertIsNone(json.loads(raw)["session"]["last"])
            s.write_session({"who": "guest"}, "phone")
            body = {"who": "guest", "key": "tmdb:900001", "kind": "movie", "title": "Synthetic", "action": "love"}
            self.assertEqual(raw_request(s, root+"feedback.json", "POST", body, handler=s.RemoteHandler)[0], 403)
            self.assertEqual(raw_request(s, root+"feedback.json", handler=s.RemoteHandler)[0], 403)
            self.assertEqual(raw_request(s, root+"search.json?q=synthetic", handler=s.RemoteHandler)[0], 403)
            with patch.object(s, "row_list", return_value=[]):
                code, _, raw, _ = raw_request(s, root+"rows.json", handler=s.RemoteHandler)
            self.assertEqual(code, 200)
            self.assertLessEqual({r["id"] for r in json.loads(raw)["rows"]}, s.GUEST_ROWS)


if __name__ == "__main__":
    unittest.main()
