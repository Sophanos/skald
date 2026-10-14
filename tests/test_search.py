import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Event
from unittest.mock import patch

from decide_kit import close_server


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.data.cleanup)
        spec = importlib.util.spec_from_file_location("search_server", Path(__file__).resolve().parents[1] / "server.py")
        cls.server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"TMDB_API": "offline", "DATA_DIR": cls.data.name}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            spec.loader.exec_module(cls.server)
        cls.addClassCleanup(close_server, cls.server)

    def setUp(self):
        self.s = self.server
        self.s._ai.clear()
        self.s._last_search.clear()
        self.s._profiles.clear()
        self.s._facets.clear()
        self.model = {"aff": {"x:cozy": 3, "k:detective": 2, "t:hope": 1,
                              "d:1": 5, "a:2": 4, "n:3": 3, "c:4": 2, "s:novel": 1},
                      "names": {"x:cozy": "Cozy comfort", "k:detective": "detective", "t:hope": "hope",
                                "d:1": "Peter Jackson", "a:2": "Elijah Wood", "n:3": "HBO",
                                "c:4": "Marvel Studios", "s:novel": "novel"}, "cal": None}
        self.model["taste_line"] = self.s.taste_line(self.model)
        self.s._model["m"] = self.model
        for name, kwargs in [("urllib.request.urlopen", {"side_effect": AssertionError("network forbidden")}),
                             ("openrouter", {"return_value": {"answers": {"route": {"choice": "request"}}}}),
                             ("search_rules", {"return_value": {"from": None, "to": None, "exclude": []}}),
                             ("ensure_facets", {}),
                             ("llm_text", {"return_value": ""}), ("tmdb", {"return_value": {"results": []}})]:
            mock = patch(name, **kwargs) if name.startswith("urllib.") else patch.object(self.s, name, **kwargs)
            setattr(self, name.rsplit(".", 1)[-1], mock.start())
            self.addCleanup(mock.stop)

    def legacy_picker(self):
        legacy = patch.object(self.s, "search_picks", partial(self.s.search_picks, wide=False))
        legacy.start()
        self.addCleanup(legacy.stop)

    def route(self, watched="any", listing="taste"):
        self.openrouter.return_value = {"answers": {"route": {"choice": "request"},
                                                   "watched": {"choice": watched}, "list": {"choice": listing}}}

    def test_consensus_prompt_labels_household_hint(self):
        self.legacy_picker()
        self.s.ai_plan("best all times")["picks"].result(timeout=2)
        system, prompt, tokens, *_ = self.llm_text.call_args.args
        self.assertTrue(prompt.startswith("best all times\nHousehold taste"))
        self.assertIn("ignored for best-of/consensus lists", prompt)
        self.assertNotIn("Peter Jackson", system + prompt)
        self.assertIn("across decades and countries", system)
        self.assertIn("across decades and countries", system)
        self.assertIn("18 lines", system)
        self.assertIn("movies only", system)
        self.assertIn("reply with the single word NONE", system)
        self.assertGreaterEqual(tokens, 600)

    def test_per_type_prompt_replacements_match_shared_prompt(self):
        self.legacy_picker()
        for text in (
                "You pick movies and series for", "30 lines",
                "Title|Year|m or s (m=movie, s=series)",
                "'movies' or 'films' means movies only; 'shows' or 'series' means series only; otherwise give at least 12 movies and 12 series."):
            self.assertEqual(self.s.PICK_SYS.count(text), 1, f"PICK_SYS replacement drift: {text}")
        for stype, plural, code, other in (("movie", "movies", "m", "series"),
                                           ("series", "series", "s", "movies")):
            with self.subTest(stype=stype):
                self.s.search_picks("something funny for tonight", [], stype=stype).result(timeout=2)
                system = self.llm_text.call_args.args[0]
                self.assertTrue(system.startswith(f"You pick {plural} for"))
                self.assertIn(f"Title|Year|{code}.", system)
                self.assertEqual(system.count("Title|Year|"), 1)
                self.assertNotIn(f"Title|Year|{'s' if code == 'm' else 'm'}", system)
                self.assertIn(f"This catalog shows {plural} only: every line is a {stype}, never {other}.", system)
                self.assertIn(f"(in any language) means {other} only:", system)
                self.assertIn(f"if the request asks only for {other}, reply with the single word NONE.", system)
                self.assertIn(f"Otherwise give 18 {plural}.", system)
                self.assertIn("18 lines", system)
                for old in ("You pick movies and series for", "30 lines", "m or s", "return no lines", "at least 12"):
                    self.assertNotIn(old, system)

    def test_per_type_picker_fixture_replies(self):
        path = Path(__file__).resolve().parents[1] / "eval/fixtures/decisions/search_picks.jsonl"
        history = self.s.seen_history(self.imported_history())
        for line in path.read_text().splitlines():
            row = json.loads(line)
            with self.subTest(query=row["in"]["query"], stype=row["in"]["stype"]):
                if "person" in row["in"]:
                    self.opposite_tastes()
                self.llm_text.return_value = row["recorded"]
                picks = self.s.search_picks(**row["in"], history=history).result(timeout=2)
                self.assertEqual([list(p) for p in picks], row["expect"])
                system, prompt, *_ = self.llm_text.call_args.args
                self.assertTrue(prompt.startswith(row["in"]["query"] + "\n"))
                self.assertIn("Title 500|2000|m", prompt)
                if "person" in row["in"]:
                    self.assertIn("Romance" if row["in"]["person"] == "ben" else "Action", prompt)
                    self.assertNotIn("Action" if row["in"]["person"] == "ben" else "Romance", prompt)
                self.assertIn("Title|Year|m." if row["in"]["stype"] == "movie" else "Title|Year|s.", system)
                self.assertTrue(all(p[2] == row["in"]["stype"] for p in picks))

    def test_none_picker_is_cached_without_fallback_or_retry(self):
        self.legacy_picker()
        self.llm_text.return_value = " \nNONE\n "
        self.assertEqual(self.s.llm_picks(self.s.PICK_SYS, "dark movies", 850, 6, "search", "picks"), [])
        for order in ("parallel", "router_first"):
            for stype in ("movie", "series"):
                self.s._ai.clear()
                self.s._last_search.clear()
                self.llm_text.reset_mock()
                with self.subTest(order=order, stype=stype), \
                        patch.object(self.s, "SEARCH_ORDER", order), \
                        patch.object(self.s, "OR_KEY", "offline"), \
                        patch.object(self.s, "looks_like_title", return_value=False), \
                        patch.object(self.s, "superseded", return_value=False), \
                        patch.object(self.s, "search_both", return_value=([{"id": 9, "title": "Unrelated"}], {})) as titles, \
                        patch.object(self.s, "resolve_pick", side_effect=AssertionError("no fallback")):
                    for _ in range(2):
                        self.assertEqual(self.s.search(stype, "dark movies", "en-US"), {"metas": []})
                    self.assertTrue(self.s._ai)
                    self.llm_text.assert_called_once()
                    if order == "router_first":
                        titles.assert_not_called()

    def test_sentence_picker_reply_still_raises(self):
        for reply in ("No series match this request.", "none", "NONE\nNo series match."):
            with self.subTest(reply=reply):
                self.llm_text.return_value = reply
                with self.assertRaisesRegex(ValueError, "Invalid picker reply"):
                    self.s.llm_picks(self.s.PICK_SYS, "dark movies", 850, 6, "search", "picks")

    def test_person_tv_credits_filter_and_rank_recurring_roles_and_crew(self):
        credits = {"cast": [
            {"id": 1, "media_type": "tv", "genre_ids": [18, 10767], "episode_count": 30, "popularity": 1000},
            {"id": 2, "media_type": "tv", "genre_ids": [10763], "episode_count": 4, "popularity": 900},
            {"id": 3, "media_type": "tv", "episode_count": 1, "popularity": 100},
            {"id": 4, "media_type": "tv", "episode_count": 2, "popularity": 90},
            {"id": 5, "media_type": "tv", "episode_count": 3, "popularity": 10},
            {"id": 6, "media_type": "tv", "episode_count": 30, "popularity": 5},
            {"id": 7, "media_type": "tv", "popularity": 110},
            {"id": 8, "media_type": "tv", "episode_count": None, "genre_ids": None},
            {"id": 9, "media_type": "tv", "episode_count": 8, "adult": True, "popularity": 800},
            {"id": 10, "media_type": "movie", "genre_ids": [10767], "popularity": 200},
            {"id": 11, "media_type": "movie", "genre_ids": [10763], "popularity": 150}],
            "crew": [
                {"id": 12, "media_type": "tv", "job": "Director", "episode_count": 1, "popularity": 12},
                {"id": 13, "media_type": "tv", "department": "Directing", "popularity": 8},
                {"id": 14, "media_type": "tv", "job": "Director", "genre_ids": [10767], "popularity": 700},
                {"id": 15, "media_type": "movie", "job": "Director", "popularity": 175},
                {"id": 16, "media_type": "movie", "job": "Writer", "popularity": 600}]}
        original = json.dumps(credits, sort_keys=True)
        for kind, expected in (("tv", [12, 5, 13, 6, 7, 3, 4, 8]), ("movie", [10, 15, 11])):
            with self.subTest(kind=kind):
                self.tmdb.side_effect = [{"results": [{"id": 42, "name": "Synthetic Actor"}]}, credits]
                titles = self.s.person_titles("Synthetic Actor", kind, "en-US")
                self.assertEqual([it["id"] for it in titles], expected)
                self.assertEqual(json.dumps(credits, sort_keys=True), original)

    def test_taste_prompt_only_has_experiences_and_themes(self):
        self.s.ai_plan("something cozy for tonight")["picks"].result(timeout=2)
        system, prompt, *_ = self.llm_text.call_args.args
        self.assertTrue(prompt.startswith("something cozy for tonight\nHousehold taste"))
        self.assertIn("experiences: Cozy comfort; themes: Detective, Hope", prompt)
        self.assertIn("personal taste", prompt)
        for text in ("Peter Jackson", "Elijah Wood", "HBO", "Marvel Studios", "novel", "creators:", "actors:"):
            self.assertNotIn(text, system + prompt)

    def test_no_model_means_no_hint(self):
        self.s._model["m"] = None
        self.s.ai_plan("something cozy")["picks"].result(timeout=2)
        self.assertIn("none available", self.llm_text.call_args.args[1])

    def opposite_tastes(self):
        action = {"x:hero_burden": 4, "k:hero": 3, "x:slow_romance": -4}
        romance = {"x:slow_romance": 4, "k:banter": 3, "t:period": 2, "x:hero_burden": -4}
        self.model.update(aff=action, paff={"ana": action, "ben": romance},
                          names={"x:hero_burden": "Action", "k:hero": "Hero",
                                 "x:slow_romance": "Romance", "k:banter": "Banter", "t:period": "Period drama"})

    def test_scoped_picker_taste_uses_member_affinities(self):
        self.opposite_tastes()
        with patch.object(self.s, "PEOPLE", ("ana", "ben")), \
                patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "together": "both"}), \
                self.s.addon_scope("ben"):
            self.s.ai_future("something cozy for tonight").result(timeout=2)["picks"].result(timeout=2)
        prompt = self.llm_text.call_args.args[1]
        self.assertIn("experiences: Romance; themes: Banter, Period drama", prompt)
        self.assertNotIn("Action", prompt)
        self.assertNotIn("Hero", prompt)

    def test_picker_taste_household_fallbacks_and_cache_isolation(self):
        self.opposite_tastes()
        self.model["paff"]["dana"] = {}
        ben_plans = {}
        with patch.object(self.s, "PEOPLE", ("ana", "ben", "ciro", "dana")), \
                patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "ciro": "ciro", "dana": "dana", "together": "both"}):
            for scope in ("ben", "ana", "together", None, "ciro", "dana"):
                for stype in ("movie", "series"):
                    with self.subTest(scope=scope, stype=stype), self.s.addon_scope(scope):
                        plan = self.s.ai_future("something cozy for tonight", stype=stype)
                        picks = plan.result(timeout=2)["picks"]
                        picks.result(timeout=2)
                        self.s.search_backup(picks).result(timeout=2)
                        self.assertIs(plan, self.s.ai_future("something cozy for tonight", stype=stype))
                        prompt = self.llm_text.call_args.args[1]
                        self.assertIn("Romance" if scope == "ben" else "Action", prompt)
                        self.assertNotIn("Action" if scope == "ben" else "Romance", prompt)
                        if scope == "ben":
                            ben_plans[stype] = plan
            with self.s.addon_scope("ben"):
                for stype, plan in ben_plans.items():
                    self.assertIs(plan, self.s.ai_future("something cozy for tonight", stype=stype))
        self.assertEqual(self.llm_text.call_count, 24)

    def test_scoped_taste_rerank_uses_personal_scores_and_calibration(self):
        self.opposite_tastes()
        bg = [-.25 + i * .025 for i in range(20)]
        self.model.update(cal=1, vec=None, vbase=0, bscale=1, bg=bg, holders={},
                          pbg={"ana": bg, "ben": bg}, nexp={"household": 60, "ana": 60, "ben": 60})
        metas = [{"id": f"tmdb:{i}", "type": "movie"} for i in (9101, 9102, 9103)]
        facets = {meta["id"]: {"kind": "movie", "x": {tag: 4} if tag else {}}
                  for meta, tag in zip(metas, ("hero_burden", "slow_romance", None))}
        with patch.dict(self.s._facets, facets), patch.object(self.s, "PEOPLE", ("ana", "ben", "ciro")), \
                patch.object(self.s, "match_note", wraps=self.s.match_note) as notes, \
                patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "ciro": "ciro", "together": "both"}):
            for scope, ids in (("ana", [9101, 9102, 9103]), ("ben", [9102, 9101, 9103]),
                               ("together", [9101, 9102, 9103]), (None, [9101, 9102, 9103]),
                               ("ciro", [9101, 9102, 9103])):
                with self.subTest(scope=scope), self.s.addon_scope(scope):
                    notes.reset_mock()
                    self.assertEqual([m["id"] for m in self.s.taste_rerank(metas)], [f"tmdb:{i}" for i in ids])
                    notes.assert_any_call("tmdb:9102", False, 65, scope if scope in ("ana", "ben") else None, "movie")

    def test_picker_captures_person_before_pool_and_refinement(self):
        self.opposite_tastes()
        with patch.object(self.s, "PEOPLE", ("ana", "ben")), \
                patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "together": "both"}):
            for previous in ("new", "refine"):
                context = {"query": "something cozy", "rows": {}, "at": self.s.time.time()}
                self.openrouter.return_value["answers"]["previous"] = {"choice": previous}
                release = Event()
                with self.subTest(previous=previous), ThreadPoolExecutor(max_workers=1) as pool, \
                        patch.object(self.s._outer, "submit", side_effect=pool.submit), \
                        patch.object(self.s, "search_turn", return_value=context):
                    pool.submit(release.wait, 2)
                    with self.s.addon_scope("ben"):
                        plan = self.s.ai_future("more like this " + previous)
                    with self.s.addon_scope("ana"):
                        release.set()
                        picks = plan.result(timeout=2)["picks"]
                        picks.result(timeout=2)
                        self.s.search_backup(picks).result(timeout=2)
                    prompt = self.llm_text.call_args.args[1]
                    self.assertIn("Romance", prompt)
                    self.assertNotIn("Action", prompt)

    def test_router_title_still_skips_picks(self):
        self.legacy_picker()
        self.openrouter.return_value = {"answers": {"route": {"choice": "title", "confidence": 0.9}}}
        with patch.object(self.s, "_pool") as pool:
            picks = pool.submit.return_value
            self.assertEqual(self.s.ai_plan("Castle")["choice"], "title")
            pool.submit.assert_called_once()
            picks.cancel.assert_not_called()
            picks.result.assert_not_called()
        self.llm_text.assert_not_called()

    def test_same_type_shares_eighteen_line_picker(self):
        self.legacy_picker()
        self.llm_text.return_value = "\n".join(f"Title {i}|2000|m" for i in range(18))
        first = self.s.ai_future("best all times")
        self.assertIs(first, self.s.ai_future("BEST ALL TIMES"))
        picks = first.result(timeout=2)["picks"].result(timeout=2)
        self.assertEqual(len(picks), 18)
        self.assertTrue(all(p[2] == "movie" for p in picks))
        self.llm_text.assert_called_once()

    def search_results(self, query, stype, watched="any", listing="taste"):
        self.route(watched, listing)
        self.llm_text.return_value = "\n".join(f"Title {i}|2000|{'m' if i < 6 else 's'}" for i in range(1, 11))

        def results(path, **params):
            title = params.get("query", "")
            if title.startswith("Title "):
                return {"results": [{"id": int(title.split()[-1]), "title": title, "name": title,
                                     "release_date": "2000-01-01", "first_air_date": "2000-01-01"}]}
            return {"results": []}

        self.tmdb.side_effect = results
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False):
            return self.s.search(stype, query, "en-US")["metas"]

    def test_seen_and_negative_for_both_intents_and_catalogs(self):
        self.legacy_picker()
        self.s._profiles["household"] = {"seen": {"tmdb:1", "tmdb:3", "tmdb:4", "tmdb:6", "tmdb:8", "tmdb:9"},
                                         "negative": {"tmdb:3", "tmdb:8"}}
        for query in ("best all times", "something cozy"):
            for stype, ids in [("movie", [2, 5, 1, 4]), ("series", [7, 10, 6, 9])]:
                with self.subTest(query=query, stype=stype):
                    self.assertEqual([m["id"] for m in self.search_results(query, stype)], [f"tmdb:{i}" for i in ids])

    def test_canon_skips_rerank_and_taste_only_reranks_unseen(self):
        self.legacy_picker()
        self.s._profiles["household"] = {"seen": {"tmdb:1", "tmdb:4"}, "negative": {"tmdb:3"}}
        with patch.object(self.s, "taste_rerank", side_effect=lambda metas, **kw: metas[::-1]) as rerank:
            self.assertEqual([m["id"] for m in self.search_results("best all times", "movie", listing="best")],
                             ["tmdb:2", "tmdb:5", "tmdb:1", "tmdb:4"])
            rerank.assert_not_called()
            self.assertEqual([m["id"] for m in self.search_results("something cozy", "movie")],
                             ["tmdb:5", "tmdb:2", "tmdb:1", "tmdb:4"])
            self.assertEqual([m["id"] for m in rerank.call_args.args[0]], ["tmdb:2", "tmdb:5"])

    def test_no_profile_preserves_order(self):
        self.assertEqual([m["id"] for m in self.search_results("best all times", "movie", listing="best")],
                         [f"tmdb:{i}" for i in range(1, 6)])

    def test_taste_rerank_uses_twenty_percent(self):
        self.model["cal"] = 1
        metas = [{"id": f"tmdb:{i}", "type": "movie"} for i in range(4)]
        with patch.dict(self.s._facets, {m["id"]: {} for m in metas}), \
                patch.object(self.s, "score_key", return_value=(0, [])), \
                patch.object(self.s, "match_pct", side_effect=[70, 70, 99, 70]), \
                patch.object(self.s, "with_match", side_effect=lambda meta, **kw: meta):
            self.assertEqual(self.s.taste_rerank(metas), metas)

    def history(self):
        entries = [dict(key=f"tmdb:{i}", id=i, title=f"Title {i}", year="2000", kind="movie",
                        stype="movie", age=i, weight=-1 if i == 3 else 1, signal="watched",
                        by=["ana" if i != 4 else "ben"], item={}) for i in (1, 3, 4)]
        self.s._profiles["household"] = {"entries": entries, "seen": {e["key"] for e in entries},
                                         "negative": {"tmdb:3"}}
        return entries

    def test_seen_history_excludes_watchlist_and_interest(self):
        entries = self.history()
        self.route("seen")
        entries.extend({**entries[0], "key": f"tmdb:{i}", "id": i, "title": f"Title {i}",
                        "signal": signal, "item": item} for i, signal, item in (
                            (6, "watchlist", {}), (7, "interest", {}), (8, "interest", {"played": True}),
                            (9, "interest", {"rating": 8})))
        self.s._profiles["household"]["seen"].update(e["key"] for e in entries)
        self.llm_text.return_value = "Title 6|2000|m|Listed\nTitle 7|2000|m|Interested\nTitle 8|2000|m|Played\nTitle 9|2000|m|Rated"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False), \
                patch.object(self.s, "SCOPES", {"ana": "ana", "together": "both"}), \
                patch.object(self.s, "PEOPLE", ("ana", "ben")):
            for scope in (None, "ana", "together"):
                with self.subTest(scope=scope), self.s.addon_scope(scope):
                    metas = self.s.search("movie", "anime I have watched", "en-US")["metas"]
                    self.assertEqual([m["id"] for m in metas], ["tmdb:8", "tmdb:9"])
                    prompt = self.llm_text.call_args.args[1]
                    self.assertNotIn("Title 6|", prompt)
                    self.assertNotIn("Title 7|", prompt)

    def test_unseen_removes_seen_and_negative(self):
        self.history()
        self.assertEqual([m["id"] for m in self.search_results("anime which i have not watched", "movie", watched="unseen")],
                         ["tmdb:2", "tmdb:5"])
        self.assertIn("not watched", self.llm_text.call_args.args[1])

    def seen_results(self, scope=None):
        self.route("seen", "best")
        self.llm_text.return_value = "Title 4|2000|m|Again\nInvented|2000|m|Oops\nTitle 1|2000|m|Favorite\nTitle 3|2000|m|Bad"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False), \
                patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "together": "both"}), \
                patch.object(self.s, "PEOPLE", ("ana", "ben")), self.s.addon_scope(scope), \
                patch.object(self.s, "resolve_pick", side_effect=AssertionError("no title resolution")), \
                patch.object(self.s, "taste_rerank", side_effect=AssertionError("keep LLM order")):
            return self.s.search("movie", "anime which i have watched", "en-US")["metas"]

    def test_seen_history_order_prompt_and_no_title_resolution(self):
        self.history()
        metas = self.seen_results()
        self.assertEqual([m["id"] for m in metas], ["tmdb:4", "tmdb:1"])
        self.assertEqual(metas[0]["name"], "✨ Title 4")
        self.assertIn("Again", metas[0]["description"])
        system, prompt, *_ = self.llm_text.call_args.args
        self.assertIn("Title 1|2000|m", prompt)
        self.assertIn("Title 4|2000|m", prompt)
        self.assertLess(prompt.index("Title 1"), prompt.index("Title 4"))
        self.assertIn("have watched", prompt)
        self.assertIn("only", system)

    def test_scoped_history_and_cache_isolation(self):
        self.history()
        for scope, ids in [("ana", [1]), ("ben", [4]), ("together", [4, 1]), (None, [4, 1])]:
            with self.subTest(scope=scope):
                self.assertEqual([m["id"] for m in self.seen_results(scope)], [f"tmdb:{i}" for i in ids])
        self.assertEqual(self.llm_text.call_count, 4)
        self.seen_results("ana")
        self.assertEqual(self.llm_text.call_count, 4)

    def test_scoped_unseen_and_neutral(self):
        self.legacy_picker()
        self.history()
        with patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "together": "both"}), \
                patch.object(self.s, "PEOPLE", ("ana", "ben")):
            for scope, ids, neutral in [("ana", [2, 4, 5], [2, 4, 5, 1]), ("ben", [1, 2, 3, 5], [1, 2, 3, 5, 4]), ("together", [2, 5], [2, 5, 1, 4])]:
                with self.s.addon_scope(scope):
                    self.assertEqual([m["id"] for m in self.search_results("anime I have not watched", "movie", watched="unseen")],
                                     [f"tmdb:{i}" for i in ids])
                    self.assertEqual([m["id"] for m in self.search_results("best all times", "movie", listing="best")],
                                     [f"tmdb:{i}" for i in neutral])

    def test_seen_empty_llm_stays_empty(self):
        self.history()
        self.route("seen")
        self.tmdb.return_value = {"results": [{"id": 9, "title": "Unrelated"}]}
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "looks_like_title", return_value=False), \
                patch.object(self.s, "superseded", return_value=False):
            self.assertEqual(self.s.search("movie", "anime I have already seen", "en-US"), {"metas": []})

    def test_seen_history_cap_recency_and_series(self):
        self.route("seen")
        entries = [dict(key=f"tmdb:{i}", id=i, title=f"Title {i}", year=2000, kind="tv",
                        stype="series", age=305-i, weight=1, signal="watched", by=["ana"], item={})
                   for i in range(1, 306)]
        self.s._profiles["household"] = {"entries": entries, "seen": {e["key"] for e in entries}, "negative": set()}
        self.llm_text.return_value = "Title 305|2000|s|Recent\nTitle 1|2000|s|Too old"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False), \
                patch.object(self.s, "resolve_pick", side_effect=AssertionError("no title resolution")):
            metas = self.s.search("series", "anime I have already seen", "en-US")["metas"]
        self.assertEqual([m["id"] for m in metas], ["tmdb:305", "tmdb:1"])
        prompt = self.llm_text.call_args.args[1]
        self.assertEqual(len(prompt.splitlines()[3:]), 305)
        self.assertEqual(prompt.splitlines()[3], "Title 305|2000|s")
        self.assertIn("Title 1|2000|s", prompt)

    def imported_history(self, count=640):
        entries = [dict(key=f"tmdb:{i}", id=i, title=f"Title {i}", year=2000, kind="movie",
                        stype="movie", age=40-i if i <= 40 else 999,
                        weight=3 if i in (500, 501) else 1,
                        signal="loved" if i in (500, 501) else "watched", by=["ana"], item={})
                   for i in range(1, count + 1)]
        profile = {"entries": entries, "seen": {e["key"] for e in entries}, "negative": set()}
        self.s._profiles["household"] = profile
        return profile

    def test_imported_history_prompt_is_complete_and_ordered(self):
        profile = self.imported_history()
        profile["entries"][0]["age"] = 1500
        self.s.search_picks("anime I have watched", self.s.seen_history(profile)).result(timeout=2)
        lines = self.llm_text.call_args.args[1].splitlines()[3:]
        self.assertEqual(len(lines), 640)
        self.assertEqual(lines[:40], [f"Title {i}|2000|m" for i in range(40, 0, -1)])
        self.assertEqual(lines[40:43], ["Title 500|2000|m", "Title 501|2000|m", "Title 41|2000|m"])
        self.assertEqual(lines[-1], "Title 640|2000|m")

    def test_imported_history_seen_can_return_title_500(self):
        self.imported_history()
        self.route("seen")
        self.llm_text.return_value = "Title 500|2000|m|Favorite"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False), \
                patch.object(self.s, "resolve_pick", side_effect=AssertionError("no title resolution")):
            metas = self.s.search("movie", "anime I have watched", "en-US")["metas"]
        self.assertEqual([m["id"] for m in metas], ["tmdb:500"])

    def test_history_prompt_cap_does_not_cap_seen_or_unseen_filtering(self):
        profile = self.imported_history(1205)
        self.route("seen")
        self.llm_text.return_value = "Title 1205|2000|m|Old watch"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False):
            metas = self.s.search("movie", "anime I have watched", "en-US")["metas"]
            self.assertEqual([m["id"] for m in metas], ["tmdb:1205"])
            lines = self.llm_text.call_args.args[1].splitlines()[3:]
            self.assertEqual(len(lines), 1200)
            self.assertEqual(self.s.SEARCH_HISTORY_LIMIT, 1200)
            self.assertNotIn("Title 1205|2000|m", lines)
            self.assertEqual(len(self.s.seen_history(profile)), 1205)
            self.route("unseen")
            with patch.object(self.s, "resolve_pick", return_value=({"id": 1205, "title": "Title 1205"}, {})) as resolve:
                self.assertEqual(self.s.search("movie", "anime I haven't watched", "en-US")["metas"], [])
                self.assertEqual(resolve.call_count, 2)

    def test_seen_title_paths(self):
        self.history()
        query = "anime I have already seen"
        for mode in ("title_only", "exact", "router_title"):
            self.s._ai.clear()
            self.tmdb.return_value = {"results": [{"id": 1, "title": query if mode == "exact" else "Other"}]}
            with patch.object(self.s, "OR_KEY", "offline"), \
                    patch.object(self.s, "looks_like_title", return_value=False), \
                    patch.object(self.s, "superseded", return_value=False), \
                    patch.object(self.s, "decide", return_value={"answer": {"choice": "title", "confidence": .9}}):
                self.assertEqual(self.s.search("movie", query, "en-US", title_only=mode == "title_only")
                                 ["metas"][0]["id"], "tmdb:1")
                for _, plan in self.s._ai.values():
                    plan.result(timeout=2)["picks"].result(timeout=2)

    def test_plain_title_search_keeps_seen_and_negative(self):
        self.s._profiles["household"] = {"seen": {"tmdb:1"}, "negative": {"tmdb:1"}}
        self.tmdb.return_value = {"results": [{"id": 1, "title": "Castle"}]}
        with patch.object(self.s, "OR_KEY", "offline"):
            self.assertEqual(self.s.search("movie", "Castle", "en-US")["metas"][0]["id"], "tmdb:1")
        self.openrouter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
