from skald.members import rid
import copy
import hashlib
import os
from pathlib import Path
import statistics
import time
import tracemalloc
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM, reset
from test_api_routes import ApiCase


def data_bytes(path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(path).iterdir()
            if p.is_file() and p.name not in ("clients.json", "remote.json", "hydra.json", ".env")}


class ApiViewsTests(ApiCase):
    def test_title_kind_rungs_unknown_and_no_model(self):
        s = self.s
        with patch.object(s, "explain", side_effect=AssertionError("private explain called")):
            view = s.title_view("tmdb:900001", "movie", "ben")
            self.assertEqual(view["rung"], "seed")
            self.assertEqual(s.title_view("tmdb:900005", "movie")["rung"], "facet")
            self.assertIsNone(s.title_view("tmdb:900001", "tv"))
            self.assertIsNone(s.title_view("tmdb:999999", "movie"))
            self.assertIsNone(s.title_view("tmdb:900003", "movie", "ben"))
            with patch.object(s, "get_model", return_value=None):
                view = s.title_view("tmdb:900001", "movie")
                self.assertEqual((view["match"], view["confidence"], view["reasons"], view["rung"]), (None, None, [], None))
                self.assertIsNone(s.title_view("tmdb:900003", "movie", "ben"))
        self.assertLessEqual(len(view["experiences"]), 5)

    def test_profile_counts_facets_and_together_privacy(self):
        code, own = self.request("/api/v1/profile", client="ben")
        self.assertEqual((code, own["titles"]), (200, 2))
        self.assertNotIn("entries", own)
        self.assertEqual(own["signals"], {"loved": 2})
        self.assertEqual(self.request("/api/v1/profile")[1]["titles"], 3)
        for person in ("ben", "together"):
            view = self.request("/api/v1/profile?person=" + person, client="ben")[1]
            self.assertEqual(view["titles"], 2)
            self.assertNotIn("Ana", str(view))
            for kind in ("experiences", "themes", "creators"):
                self.assertLessEqual(len(view[kind]), 10)
                self.assertTrue(all(len(x["anchors"]) <= 3 for x in view[kind]))

    def test_rows_read_only_cached_composer_and_no_bound_fallback(self):
        s = self.s
        learn, files = copy.deepcopy(s._learn), data_bytes(s.DATA)
        metas = [{"id": "tmdb:900001", "name": "Synthetic", "type": "movie", "description": "Private fallback"},
                 {"id": "tmdb:900003", "name": "AnaTitleSentinel", "type": "movie"}]
        with patch.object(s, "note_served", side_effect=AssertionError("learn write")), \
                patch.object(s, "note_exposure", side_effect=AssertionError("online write")), \
                patch.object(s, "row_metas", return_value=metas) as composer:
            code, view = self.request(f"/api/v1/rows?row={rid('foryou', s.CONFIG)}", client="ben")
            self.assertEqual(code, 200)
            composer.assert_called_once_with(rid('foryou', s.CONFIG), "tv", s.LANG, who="ben")
            self.assertEqual(len(view["rows"][0]["items"]), 1)
            with patch.object(s, "title_view", return_value={"reasons": []}):
                self.assertIsNone(self.request(f"/api/v1/rows?row={rid('foryou', s.CONFIG)}", client="ben")[1]["rows"][0]["items"][0]["why"])
                self.assertEqual(self.request(f"/api/v1/rows?row={rid('foryou', s.CONFIG)}")[1]["rows"][0]["items"][0]["why"], "Private fallback")
            ids = {r["id"] for r in self.request("/api/v1/rows", client="ben")[1]["rows"]}
            self.assertNotIn(rid('ana', s.CONFIG), ids)
            self.assertNotIn(rid('week', s.CONFIG), ids)
        self.assertEqual(s._learn, learn)
        self.assertEqual(data_bytes(s.DATA), files)

    def test_row_list_reuses_feed_logic_without_profile_discovery(self):
        with patch.object(self.s, "active_profiles", side_effect=AssertionError("profile discovery wrote defaults")):
            actual = self.s.feed_rows("de-DE", profiles=False)
        with patch.object(self.s, "active_profiles", return_value=["household", "cal"]):
            expected = [r for r in self.s.feed_rows("de-DE") if r[0] != rid('cal', self.s.CONFIG)]
        self.assertEqual(actual, expected)
        self.assertFalse(Path(self.s.DATA, "taste.json").exists())

    def test_row_metas_who_reaches_both_paths_and_cache(self):
        s = self.s
        with patch.object(s, "remote_masks", return_value=(set(), set())), \
                patch.object(s, "build_home", return_value={(rid('foryou', s.CONFIG), "tv"): []}) as build:
            s.row_metas(rid('foryou', s.CONFIG), "tv", "de-DE", who="ben")
            self.assertEqual(build.call_args.args[-1], "ben")
            s.row_metas(rid('foryou', s.CONFIG), "tv", "de-DE", who="ana")
            self.assertEqual(build.call_args.args[-1], "ana")
            self.assertEqual(build.call_count, 2)
        with patch.object(s, "_HOME_CACHE_ENABLED", False), patch.object(s, "served_row", return_value=[]) as serve:
            s.row_metas(rid('foryou', s.CONFIG), "tv", "de-DE", who="ben")
            self.assertEqual(serve.call_args.kwargs["who"], "ben")

    def test_metrics_days_privacy_counts_only_never_stats(self):
        s = self.s
        self.assertEqual(self.request("/api/v1/metrics?days=36")[0], 400)
        report = {"who": {"ben": {"plays": 1}, "ana": {"plays": 2}, "both": {"plays": 3}}, "rows": {}}
        with patch.object(s, "stats", side_effect=AssertionError("private stats called")), \
                patch.object(s, "metrics", side_effect=lambda **kw: copy.deepcopy(report)) as measure:
            code, view = self.request("/api/v1/metrics?days=1", client="ben")
            self.assertEqual(code, 200)
            measure.assert_called_once_with(days=1)
            self.assertEqual(set(view["metrics"]["who"]), {"ben", "both"})
            self.assertNotIn("TitleSentinel", str(view))
            self.assertEqual(set(view["usage"]), {"day", "month_cost", "caps"})

    def test_explain_receipt_privacy_and_25_item_latency(self):
        s = self.s
        inp = {"key": "tmdb:900001", "kind": "movie", "person": "ben"}
        timings = []
        for _ in range(10):
            start = time.perf_counter()
            code, reply = self.request("/api/v1/decide/explain_pick", "POST", {"items": [inp] * 25})
            timings.append((time.perf_counter() - start) * 1000)
            self.assertEqual(code, 200)
            self.assertEqual({r["source"] for r in reply["results"]}, {"rule"})
        s.flush_decisions()
        why = reply["results"][0]["answer"]["why"]
        self.assertNotIn(why, Path(s.DATA, "decisions.jsonl").read_text())
        self.assertFalse(Path(s.DATA, "decide_cache.json").exists())
        self.assertLess(statistics.median(timings), 1000)

    def test_2000_distinct_replays_bounded_memory_and_flights(self):
        s = self.s
        reset(s)
        tracemalloc.start()
        try:
            with ReplayLLM(s) as replay:
                # This gate measures the ladder, not the feed lane's unrelated daily budget.
                with patch.dict(s.AI_CAPS, {"search": 2500}):
                    for i in range(2000):
                        replay.arm({"answers": {"route": {"choice": "title", "confidence": 1.}}, "usage": {"cost": 0}})
                        self.assertEqual(s.decide("route_query", {"query": f"Synthetic memory gate {i}"})["source"], "llm")
                        replay.bodies.clear()  # Measure the ladder, not retained copies of the model prompt.
                s.flush_decisions()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLessEqual(peak, 16 * 1024 * 1024)
        self.assertEqual(s._decide_flights, {})
        self.assertLessEqual(len(s._decide_cache), 5000)

    @unittest.skipUnless(os.environ.get("SKALD_SNAPSHOT"), "set SKALD_SNAPSHOT for snapshot gates")
    def test_snapshot_default_rows_identity_and_latency(self):
        from eval.profile_golden import NOW
        from eval.rows_report import home_server
        with patch("time.time", return_value=NOW), home_server(os.environ["SKALD_SNAPSHOT"]) as current:
            req = {"viewer": None, "query": {}, "client": {}, "tail": None, "body": None}
            expected = current.rows_view(req)
            for row in expected["rows"]:
                self.assertEqual([x["key"] for x in row["items"]],
                                 [x["id"] for x in current.row_metas(row["id"], "tv", "de-DE")[:15]], row["id"])
            with patch.object(current, "_HOME_CACHE_ENABLED", False):
                self.assertEqual(current.rows_view(req), expected)
            before = data_bytes(current.DATA)
            learn = copy.deepcopy(current._learn)
            timings = {"api": [], "remote": []}
            with patch.object(current, "note_served", side_effect=AssertionError("write")), \
                    patch.object(current, "note_exposure", side_effect=AssertionError("write")):
                for _ in range(10):
                    for label, fn in (("api", lambda: current.rows_view(req)), ("remote", lambda: current.remote_rows("de-DE"))):
                        start = time.perf_counter()
                        fn()
                        timings[label].append((time.perf_counter() - start) * 1000)
            self.assertEqual(data_bytes(current.DATA), before)
            self.assertEqual(current._learn, learn)
            self.assertLessEqual(max(timings["api"]), 2 * max(timings["remote"]) + 100)


if __name__ == "__main__":
    unittest.main()
