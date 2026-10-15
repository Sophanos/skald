from concurrent.futures import ThreadPoolExecutor
import copy
import datetime
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from decide_kit import close_server, fixture_rows, load_server, ReplayLLM, reset


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = load_server(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(close_server, self.s)

    def route(self, query="unresolved query", **kwargs):
        return self.s.decide("route_query", {"query": query}, **kwargs)

    def reply(self, cost=None):
        response = {"answers": {"route": {"choice": "request", "confidence": .9}}}
        if cost is not None:
            response["usage"] = {"cost": cost}
        return response

    def test_route_model_default_and_environment_override_preserve_other_models(self):
        for override, expected in (('', 'perplexity/pplx-decider-v1.1-27b'), ('synthetic/router', 'synthetic/router')):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as data, \
                    patch.dict(os.environ, {'SKALD_ROUTE_MODEL': override}):
                s = load_server(data)
                try:
                    spec = s.DECISIONS['route_query']
                    self.assertEqual(s.ROUTE_MODEL, expected)
                    self.assertEqual(spec['llm']['model'], expected)
                    self.assertEqual(spec['llm']['path'], '/api/alpha/decisions')
                    self.assertEqual(spec['llm']['prompt']([{'query': 'synthetic query'}]),
                                     {'model': expected, 'state': 'synthetic query', 'questions': s.ROUTE_Q})
                    self.assertEqual(s.PICK_MODEL, 'openai/gpt-6-luna')
                    for name in ('resolve_title', 'mood_pick'):
                        self.assertEqual(s.DECISIONS[name]['llm']['model'], 'openai/gpt-6-luna-decisions')
                finally:
                    close_server(s)

    def test_schema_edges_and_paths(self):
        s = self.s
        for schema, values in ((str, ["", "x" * 501, None]), (int, [True, 1., math.inf]),
                               (float, [True, math.nan, math.inf, 10 ** 400]), (bool, [0, "true"]),
                               (s.TITLE, ["tmdb:1", {"key": "tmdb:0", "kind": "movie"},
                                          {"key": "tmdb:1", "kind": "series"}])):
            for value in values:
                with self.assertRaises(ValueError):
                    s.check(schema, value)
        for schema, value in (((int, None), None), ((s.TITLE, None), None), (float, 1),
                              ([int], list(range(25))), ([str, 1], ["x"]), (s.TITLE, {"key": "tmdb:999999999", "kind": "tv"})):
            s.check(schema, value)
        with self.assertRaisesRegex(ValueError, r"^choices\[3\]\.kind$"):
            s.check({"choices": [s.TITLE]}, {"choices": [{"key": "tmdb:1", "kind": "movie"}] * 3 +
                                                         [{"key": "tmdb:2", "kind": "bad secret"}]})
        for schema, value in (([int], list(range(26))), ([str, 1], ["x", "y"]), ({"a": int}, {}), ({"a": int}, {"a": 1, "b": 2})):
            with self.assertRaises(ValueError):
                s.check(schema, value)

    def test_registration_assertions(self):
        spec = dict(self.s.DECISIONS["route_query"])
        for change in ({"bad": 1}, {"ver": False}, {"lane": "unknown"}, {"serving": True},
                       {"inp": {"key": self.s.TKEY}}, {"out": {"pick": self.s.TKEY}},
                       {"llm": {**spec["llm"], "batch": 0}}, {"fallback": None},
                       {"llm": {**spec["llm"], "fallback": {**spec["llm"], "timeout": 0}}},
                       {"llm": {**spec["llm"], "fallback": {**spec["llm"], "batch": 2}}},
                       {"llm": {**spec["llm"], "fallback": {**spec["llm"], "unexpected": True}}}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                self.s.decision("bad_spec", **{**spec, **change})
        with self.assertRaises(AssertionError):
            self.s.decision("missing_spec", ver=1)
        with self.assertRaises(AssertionError):
            self.s.decision("route_query", **spec)
        with self.assertRaises(KeyError):
            self.s.decide("missing", {})
        spec.pop("max_usd")
        spec.pop("fixtures")
        registered = self.s.decision("good_spec", **spec)
        self.assertEqual(registered["max_usd"], self.s.DECIDE_MAX_USD)
        self.assertEqual(registered["fixtures"], "eval/fixtures/decisions/good_spec.jsonl")

    def test_normalization_and_route_body(self):
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            first = self.route("  Hello   World  ")
            second = self.route("Hello World")
        self.assertEqual((first["source"], second["source"]), ("llm", "cache"))
        self.assertEqual(first["cost_usd"], None)
        self.assertEqual(replay.bodies[0], {"model": self.s.ROUTE_MODEL, "state": "Hello World", "questions": self.s.ROUTE_Q})
        for query in (" " * 20, "a" * 501):
            with self.assertRaisesRegex(ValueError, "^query$"):
                self.route(query)

    def test_long_queries_keep_full_picks_and_truncate_router(self):
        self.s._model["m"] = None
        for length in (225, 600):
            query = str(length) + "x" * (length - 3)
            picks = Mock()
            picks.result.return_value = [("A title", 2026, "movie", "a reason")]
            with self.subTest(length=length), patch.object(self.s, "search_picks", return_value=picks) as picker, ReplayLLM(self.s) as replay:
                replay.arm(self.reply())
                self.assertIs(self.s.ai_plan(query)["picks"], picks)
                self.assertEqual(picker.call_args.args[0], query)
                self.assertEqual(replay.bodies[0]["state"], query[:200])
                picks.cancel.assert_not_called()
        self.assertEqual(self.s.route_norm({"query": " " + "X" * 225 + " "}), {"query": "X" * 200})

    def test_plan_router_error_keeps_picks(self):
        self.s._model["m"] = None
        with patch.object(self.s, "_pool") as pool, patch.object(self.s, "decide", side_effect=ValueError("query")):
            pool.submit.return_value.result.return_value = []
            self.assertIs(self.s.ai_plan("query")["picks"], pool.submit.return_value)
            pool.submit.return_value.cancel.assert_not_called()

    def test_rule_precedes_cache_and_gates(self):
        row = fixture_rows(self.s, "resolve_title")[0]
        result = self.s.decide("resolve_title", row["in"], allow_llm=False)
        self.assertEqual(result["source"], "rule")
        self.assertEqual(result["answer"], row["expect"])

    def test_gates_order_and_call_time_requires(self):
        row = next(r for r in fixture_rows(self.s, "resolve_title") if r["recorded"])
        self.assertEqual(self.s.decide("resolve_title", row["in"], allow_llm=False)["why"], "allow_llm")
        self.assertEqual(self.s.decide("resolve_title", row["in"])["why"], "requires")
        with ReplayLLM(self.s) as replay:
            replay.arm(row["recorded"])
            self.assertEqual(self.s.decide("resolve_title", row["in"])["source"], "llm")
        with patch.dict(self.s.DECISIONS["route_query"], {"serving": True, "llm": None}):
            self.assertEqual(self.route(allow_llm=False)["why"], "serving")

    def test_ledger_and_cost_and_over_rollover(self):
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply(.03))
            first = self.route()
            second = self.route("different query")
            self.assertEqual(first["cost_usd"], .03)
            self.assertEqual(second["why"], "over")
            self.assertEqual(self.s._usage["calls"], {"search:route_query": 1})
            self.assertEqual(self.s._usage["month_cost"], .03)
            counts = self.s.decide_stats()["route_query"]
            self.assertEqual((counts["cap"], counts["over"], counts["llm_calls"]), (0, True, 1))
            self.s.flush_decisions()
            records = [json.loads(x) for x in Path(self.s.DATA, "decisions.jsonl").read_text().splitlines()]
            self.assertTrue(records[0]["over"])
            future = self.s.local_now() + datetime.timedelta(days=1)
            with patch.object(self.s, "local_now", return_value=future):
                replay.arm(self.reply(.001))
                self.assertEqual(self.route("next day")["source"], "llm")
        self.assertFalse(self.s.decide_stats()["route_query"]["over"])

    def test_month_reservation_and_max_usd_clamp(self):
        self.s._usage_roll()
        self.s._usage["month_cost"] = self.s.ai_monthly_usd() - .01
        with ReplayLLM(self.s) as replay:
            self.assertEqual(self.route(max_usd=20)["why"], "month")
            replay.arm(self.reply(.003))
            result = self.route(max_usd=.002)
            self.assertEqual(result["source"], "llm")
            self.assertTrue(self.s.decide_stats()["route_query"]["over"])
        with self.assertRaises(ValueError):
            self.route(max_usd=-1)

    def test_batch_cost_partial_validation(self):
        rows = [r for r in fixture_rows(self.s, "resolve_title") if r["recorded"]][:3]
        response = {"answers": {"0": {"choice": "c1", "confidence": .9},
                                 "1": {"choice": "c1", "confidence": 4},
                                 "2": {"choice": "outside", "confidence": .7}}, "usage": {"cost": .006}}
        with ReplayLLM(self.s) as replay:
            replay.arm(response)
            results = self.s.decide_many("resolve_title", [r["in"] for r in rows])
        self.assertEqual(replay.calls, 1)
        self.assertEqual([r["cost_usd"] for r in results], [.002] * 3)
        self.assertEqual([r["source"] for r in results], ["llm", "fallback", "llm"])
        self.assertIsNone(results[2]["answer"]["pick"])
        self.assertEqual(list(replay.bodies[0]["questions"]["0"]["criteria"]), ["c0", "c1", "none"])
        self.assertEqual(replay.bodies[0]["usage"], {"include": True})
        self.assertEqual(self.s._usage["month_cost"], .006)

    def test_llm_confidence_rejects_bools_and_strings(self):
        row = next(r for r in fixture_rows(self.s, "resolve_title") if r["recorded"])
        for conf in (False, True, "", "0.9", -1, 2):
            with ReplayLLM(self.s) as replay:
                replay.arm({"answers": {"0": {"choice": "c0", "confidence": conf}}})
                result = self.s.decide("resolve_title", row["in"])
                self.assertEqual((result["source"], result["why"]), ("fallback", "ValueError"))

    def test_singleflight_ten_threads_ten_receipts(self):
        entered, release = threading.Event(), threading.Event()
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply(.001))
            original = replay.urlopen
            def slow(req, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                return original(req, **kwargs)
            with patch.object(self.s, "_connector_open", slow), ThreadPoolExecutor(max_workers=10) as pool:
                jobs = [pool.submit(self.route) for _ in range(10)]
                self.assertTrue(entered.wait(1))
                release.set()
                results = [f.result(3) for f in jobs]
        self.assertEqual(replay.calls, 1)
        self.assertEqual(sum(r["source"] == "llm" for r in results), 1)
        self.assertEqual(sum(r["source"] == "cache" for r in results), 9)
        self.s.flush_decisions()
        self.assertEqual(len(Path(self.s.DATA, "decisions.jsonl").read_text().splitlines()), 10)
        self.assertEqual(len({r["receipt"] for r in results}), 10)

    def test_follower_timeout_and_invalid_batch_leaves_no_flights(self):
        key = f"route_query|{self.s.DECISIONS['route_query']['ver']}|" + self.s.hashlib.sha256(b'{"query":"waiting"}').hexdigest()[:32]
        with self.s._decide_lock:
            self.s._decide_flights[key] = threading.Event()
        with patch.dict(self.s.DECISIONS["route_query"]["llm"], {"timeout": .001}), \
                patch.object(threading.Event, "wait", return_value=False):
            self.assertEqual(self.route("waiting")["why"], "singleflight")
        self.s._decide_flights.clear()
        with self.assertRaises(ValueError):
            self.s.decide_many("route_query", [{"query": "valid"}, {"query": False}])
        self.assertEqual(self.s._decide_flights, {})

    def test_exception_fallback_and_baseexception_escapes(self):
        with patch.object(self.s, "openrouter", side_effect=TimeoutError("private query")):
            result = self.route()
        self.assertEqual((result["source"], result["why"]), ("fallback", "TimeoutError"))
        for cost in (True, -1, math.nan, math.inf):
            with patch.object(self.s, "openrouter", return_value=self.reply(cost)):
                result = self.route()
            self.assertEqual((result["source"], result["why"], result["cost_usd"]), ("fallback", "ValueError", None))
        class Stop(BaseException):
            pass
        with patch.object(self.s, "openrouter", side_effect=Stop), self.assertRaises(Stop):
            self.route()
        self.assertEqual(self.s._decide_flights, {})
        with patch.dict(self.s.DECISIONS["route_query"], {"fallback": lambda x, e: ({"choice": "invalid"}, 0)}), \
                self.assertRaises(ValueError):
            self.route(allow_llm=False)

    def test_only_llm_answers_cached_and_ttl_expires(self):
        self.route(allow_llm=False)
        self.assertFalse(self.s._decide_cache)
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            self.route()
            self.assertEqual(self.route()["source"], "cache")
            for hit in self.s._decide_cache.values():
                hit["expires"] = time.time() - 1
            replay.arm(self.reply())
            self.assertEqual(self.route()["source"], "llm")

    def test_receipt_rotation_cache_bounds_12k(self):
        with patch.object(self.s, "openrouter", return_value=self.reply()):
            for i in range(12000):
                self.route(f"query {i}")
        # Generous bound: the writer drains slowly under suite load, but a stuck writer still fails.
        self.assertTrue(self.s.flush_decisions(timeout=60))
        lines = Path(self.s.DATA, "decisions.jsonl").read_text().splitlines()
        self.assertLessEqual(len(lines), 5000)
        self.assertLessEqual(len(self.s._decide_cache), 5000)
        self.assertEqual(json.loads(lines[-1])["in"], self.s.hashlib.sha256(b'{"query":"query 11999"}').hexdigest()[:32])
        self.assertEqual(Path(self.s.DATA, "decisions.jsonl").stat().st_mode & 0o777, 0o600)

    def test_cache_load_closed_answers_and_receipt_privacy(self):
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            result = self.route("NeverStoreThisQuery", client="test-cli")
        self.s.flush_decisions()
        cache = Path(self.s.DATA, "decide_cache.json")
        self.assertTrue(cache.exists())
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        text = cache.read_text() + Path(self.s.DATA, "decisions.jsonl").read_text()
        self.assertNotIn("NeverStoreThisQuery", text)
        self.assertNotIn("replay", text)
        server = load_server(self.s.DATA)
        self.addCleanup(close_server, server)
        server.load_decisions()
        self.assertEqual(server.decide("route_query", {"query": "NeverStoreThisQuery"}, allow_llm=False)["source"], "cache")
        server.flush_decisions()
        rec = json.loads(Path(self.s.DATA, "decisions.jsonl").read_text().splitlines()[0])
        self.assertEqual(set(rec), set("t id type ver in src why conf cost ms answer client".split()))
        self.assertEqual(rec["client"], "test-cli")
        self.assertEqual(rec["id"], result["receipt"])

    def test_load_skips_bad_middle_receipt_and_cache_entries(self):
        key = lambda n: f"route_query|{self.s.DECISIONS['route_query']['ver']}|" + str(n) * 32
        hit = {"expires": time.time() + self.s.DAY, "answer": self.reply()["answers"]["route"], "confidence": .9}
        cache = {key(1): hit, "torn-key": hit, key(2): {**hit, "confidence": "bad"}, key(3): hit}
        Path(self.s.DATA, "decide_cache.json").write_text(json.dumps(cache))
        receipt = {"t": time.time(), "type": "route_query", "src": "llm", "cost": .001}
        Path(self.s.DATA, "decisions.jsonl").write_text(
            json.dumps(receipt) + '\n{"t":\n' + json.dumps({**receipt, "over": True}) + "\n")
        self.s.load_decisions()
        self.assertEqual(set(self.s._decide_cache), {key(1), key(3)})
        counts = self.s.decide_stats()["route_query"]
        self.assertEqual(counts["sources"], {"llm": 2})
        self.assertEqual(counts["cost"], .002)
        self.assertTrue(counts["over"])
        self.assertEqual(self.route("new query")["why"], "over")

    def test_api_share_is_per_lane_and_preserves_live_search_and_cache(self):
        self.s.decision("other_route", **self.s.DECISIONS["route_query"])
        with patch.dict(self.s.AI_CAPS, {"search": 3}), patch.object(self.s, "API_LANE_SHARE", .34), ReplayLLM(self.s) as replay:
            replay.arm(self.reply(.001))
            self.assertEqual(self.route("first", client="client-one")["source"], "llm")
            self.assertEqual(self.route("first", client="client-two")["source"], "cache")
            refused = self.s.decide("other_route", {"query": "second"}, client="client-two")
            self.assertEqual((refused["source"], refused["why"]), ("fallback", "api_share"))
            self.assertTrue(refused["receipt"])
            replay.arm(self.reply(.001))
            self.assertEqual(self.route("live query")["source"], "llm")
            self.assertEqual(self.s._usage["calls"], {"api:search": 1, "search:route_query": 2})
            future = self.s.local_now() + datetime.timedelta(days=1)
            with patch.object(self.s, "local_now", return_value=future):
                replay.arm(self.reply(.001))
                self.assertEqual(self.route("next day", client="client-one")["source"], "llm")
                self.assertEqual(self.s._usage["calls"]["api:search"], 1)

    def test_api_share_reserves_concurrent_batches(self):
        entered, release = threading.Event(), threading.Event()
        with patch.dict(self.s.AI_CAPS, {"search": 3}), patch.object(self.s, "API_LANE_SHARE", .34), ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            original = replay.urlopen
            def slow(req, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                return original(req, **kwargs)
            with patch.object(self.s, "_connector_open", slow), ThreadPoolExecutor(max_workers=3) as pool:
                leader = pool.submit(self.route, "first", client="client-one")
                self.assertTrue(entered.wait(1))
                follower = pool.submit(self.route, "first", client="client-two")
                self.assertEqual(pool.submit(self.route, "second", client="client-two").result(1)["why"], "api_share")
                release.set()
                self.assertEqual(leader.result(2)["source"], "llm")
                self.assertEqual(follower.result(2)["source"], "cache")
            self.assertEqual(replay.calls, 1)

    def test_api_share_counts_batches_and_refunds_unspent_budget_refusal(self):
        rows = [r for r in fixture_rows(self.s, "resolve_title") if r["recorded"]][:3]
        with ReplayLLM(self.s) as replay:
            replay.arm({"answers": {str(i): {"choice": "none"} for i in range(3)}})
            self.assertEqual(len(self.s.decide_many("resolve_title", [r["in"] for r in rows], client="client-one")), 3)
            self.assertEqual(self.s._usage["calls"]["api:resolve"], 1)
            self.s._usage["calls"]["search:picks"] = self.s.AI_CAPS["search"]
            result = self.route("blocked lane", client="client-one")
            self.assertEqual(result["why"], "BudgetExceeded")
            self.assertEqual(self.s._usage["calls"]["api:search"], 0)

    def test_free_text_out_memory_only_and_writer_failure(self):
        spec = {**self.s.DECISIONS["route_query"], "out": {"text": str, "flag": bool},
                "llm": {**self.s.DECISIONS["route_query"]["llm"],
                        "parse": lambda r, b: [({"text": "PrivateAnswer", "flag": True}, 1)]}}
        self.s.decision("text_answer", **spec)
        with ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            self.s.decide("text_answer", {"query": "PrivateInput"})
        self.s.flush_decisions()
        self.assertFalse(Path(self.s.DATA, "decide_cache.json").exists())
        rec = json.loads(Path(self.s.DATA, "decisions.jsonl").read_text())
        self.assertEqual(rec["answer"], {"flag": True})
        with patch.object(self.s, "DATA", self.s.DATA + "/missing"), patch.object(self.s, "print", create=True) as log:
            self.route(allow_llm=False)
            self.s.flush_decisions()
            self.route(allow_llm=False)
            self.s.flush_decisions()
            self.assertEqual(log.call_count, 1)

    def test_writer_io_stays_off_caller_and_cache_save_is_throttled(self):
        caller = threading.get_ident()
        original_open = self.s.os.open
        def checked_open(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), caller)
            return original_open(*args, **kwargs)
        with patch.object(self.s.os, "open", checked_open), \
                patch.object(self.s, "openrouter", return_value=self.reply()), \
                patch.object(self.s, "_decision_write_json", wraps=self.s._decision_write_json) as save, \
                patch("threading.Thread.start", side_effect=AssertionError("caller started thread")):
            self.route("cache first")
            self.s.flush_decisions()
            self.route("cache second")
            self.s.flush_decisions()
            self.assertEqual(save.call_count, 1)
            with patch.object(self.s.time, "monotonic", return_value=time.monotonic() + 11):
                self.route("cache third")
                self.s.flush_decisions()
            self.assertEqual(save.call_count, 2)
        with patch.dict(self.s.DECISIONS["route_query"], {"ttl": 1}), ReplayLLM(self.s) as replay:
            replay.arm(self.reply())
            self.route("short ttl")
            self.s.flush_decisions()
        self.assertNotIn("short ttl", Path(self.s.DATA, "decide_cache.json").read_text())

    def test_resolve_parity(self):
        from skald.ingest.resolve import TAU, MARGIN, match_score
        self.assertEqual(self.s.DECISIONS["resolve_title"]["params"], {"tau": TAU, "margin": MARGIN})
        for row in fixture_rows(self.s, "resolve_title"):
            inp = row["in"]
            for choice in inp["choices"]:
                original = {**choice, "popularity": choice["pop"]}
                self.assertEqual(self.s.resolve_score(inp, choice), match_score(SimpleNamespace(**inp), original))

    def test_route_fixtures_match_original_answers(self):
        for row in fixture_rows(self.s, "route_query"):
            if row["expect"]["choice"] is not None:
                answer = row["expect"]
                self.assertEqual(self.s.route_parse(row["recorded"], [row["in"]])[0][0], answer)
