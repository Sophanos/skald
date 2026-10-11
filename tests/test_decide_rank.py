import json
import math
import os
from pathlib import Path
import random
import statistics
import time
import unittest
from unittest.mock import patch

from decide_kit import check_contract, fixture_rows, SERVING_P95_MS
from skald.engine import Engine
from test_api_routes import ApiCase


class RankTests(ApiCase):
    def test_hand_derived_fixtures_and_confidence(self):
        metrics = check_contract(self, self.s, "rank_titles")
        self.assertEqual((metrics["n"], metrics["acc"], metrics["sources"]), (28, 1, {"rule": 28}))
        self.assertLessEqual(metrics["p95_ms"], SERVING_P95_MS)
        for row in fixture_rows(self.s, "rank_titles"):
            result = self.s.decide("rank_titles", row["in"])
            self.assertEqual(result["answer"], row["expect"])
            self.assertEqual(result["confidence"], (row["expect"]["match"] or 0) / 100)

    def test_api_privacy_and_batch(self):
        choices = [{"key": f"tmdb:{i}", "kind": "movie"} for i in (900003, 900020, 900005, 900001)]
        code, result = self.request("/api/v1/decide/rank_titles", "POST", {"in": {"choices": choices}}, client="ben")
        self.assertEqual(code, 200)
        self.assertEqual(result["answer"]["pick"], choices[3])
        self.assertEqual(result["answer"]["ranked"], [choices[3], choices[2], choices[0], choices[1]])
        self.assertNotIn("Ana", json.dumps(result))
        code, data = self.request("/api/v1/decide/rank_titles", "POST",
                                  {"items": [{"choices": choices}] * 20}, client="ben")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["results"]), 20)
        self.assertTrue(all(r["answer"] == result["answer"] for r in data["results"]))

    def test_validation_and_fallback(self):
        movie = {"key": "tmdb:900001", "kind": "movie"}
        tv = {**movie, "kind": "tv"}
        self.assertEqual(self.s.rank_norm({"choices": [movie, tv, movie], "person": None})["choices"], [movie, tv])
        self.assertEqual(self.s.decide("rank_titles", {"choices": [movie]})["answer"]["pick"], movie)
        with self.assertRaisesRegex(ValueError, "^choices$"):
            self.s.rank_norm({"choices": [], "person": None})
        for choices in ([], [{"key": "tmdb:900001", "kind": "movie"}] * 26):
            self.assertEqual(self.request("/api/v1/decide/rank_titles", "POST", {"in": {"choices": choices}})[0], 400)
        inp = {"choices": [{"key": "tmdb:900001", "kind": "movie"}], "person": "ben"}
        self.assertEqual(self.s.rank_fallback(inp, ValueError()),
                         ({"ranked": inp["choices"], "pick": None, "why": None, "match": None}, 0))
        with patch.object(self.s, "title_view", return_value={"match": None, "reasons": []}):
            self.assertIsNone(self.s.rank_rule()(inp, {})[0]["pick"])

    def test_custom_score_gets_visible_view_input_and_params(self):
        choices = [{"key": f"tmdb:{i}", "kind": "movie"} for i in (900003, 900001, 900005)]
        inp, calls = {"choices": choices, "person": "ben"}, []
        def score(view, given, params):
            self.assertIs(given, inp)
            calls.append(view["key"])
            return None if view["match"] == 93 else params["bonus"]
        answer, conf = self.s.rank_rule(score)(inp, {"bonus": 100})
        self.assertEqual(calls, ["tmdb:900001", "tmdb:900005"])
        self.assertEqual(answer["ranked"], [choices[2], choices[0], choices[1]])
        self.assertEqual((answer["pick"], conf), (choices[2], .7))

    @unittest.skipUnless(os.environ.get("SKALD_SNAPSHOT"), "snapshot not configured")
    def test_snapshot_latency_25_unseen_choices(self):
        with Engine(Path(os.environ["SKALD_SNAPSHOT"])) as engine:
            s = engine._server
            candidates = sorted((k, rec.get("kind", "movie")) for k, rec in s._facets.items()
                                if "@" not in k and k not in engine._profile["seen"])
            self.assertGreaterEqual(len(candidates), 25)
            choices = [{"key": k, "kind": kind} for k, kind in random.Random(14).sample(candidates, 25)]
            elapsed = []
            for _ in range(200):
                start = time.perf_counter()
                result = engine.decide("rank_titles", {"choices": choices, "person": "together"})
                elapsed.append((time.perf_counter() - start) * 1000)
                self.assertEqual(result["source"], "rule")
            p50, p95 = statistics.median(elapsed), sorted(elapsed)[math.ceil(.95 * len(elapsed)) - 1]
            print(f"rank_titles Gate L: p50 {p50:.3f} ms, p95 {p95:.3f} ms")
            self.assertLessEqual(p95, 50)
