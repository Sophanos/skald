import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


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
        cls.addClassCleanup(cls.server._pool.shutdown)
        cls.addClassCleanup(cls.server._outer.shutdown)

    def setUp(self):
        self.s = self.server
        self.s._ai.clear()
        self.s._profiles.clear()
        self.model = {"aff": {"x:cozy": 3, "k:detective": 2, "t:hope": 1,
                              "d:1": 5, "a:2": 4, "n:3": 3, "c:4": 2, "s:novel": 1},
                      "names": {"x:cozy": "Cozy comfort", "k:detective": "detective", "t:hope": "hope",
                                "d:1": "Peter Jackson", "a:2": "Elijah Wood", "n:3": "HBO",
                                "c:4": "Marvel Studios", "s:novel": "novel"}, "cal": None}
        self.model["taste_line"] = self.s.taste_line(self.model)
        self.s._model["m"] = self.model
        for name, kwargs in [("urllib.request.urlopen", {"side_effect": AssertionError("network forbidden")}),
                             ("openrouter", {"return_value": {"answers": {"route": {"choice": "request"}}}}),
                             ("llm_text", {"return_value": ""}), ("tmdb", {"return_value": {"results": []}})]:
            mock = patch(name, **kwargs) if name.startswith("urllib.") else patch.object(self.s, name, **kwargs)
            setattr(self, name.rsplit(".", 1)[-1], mock.start())
            self.addCleanup(mock.stop)

    def test_intents(self):
        canon = ["best all times", "Best of all time", "best all-time films", "top movies ever",
                 "greatest series", "top 10 movies ever", "classics", "masterpiece", "critically acclaimed",
                 "must-see movies", "oscar winning films", "award-winning shows", "IMDb top", "highest rated",
                 "beste filme aller zeiten", "besten Serien", "Klassiker", "Meisterwerk", "Meisterwerke",
                 "najbolji filmovi", "najbolje serije svih vremena", "klasici", "bset movies", "bets films",
                 "graetest shows", "clasics", "masterpeice"]
        taste = ["something cozy", "like Castle", "dark nordic crime", "something cozy for tonight",
                 "something like Dark", "feel good tonight", "gemütliche Krimis", "nesto kao Castle",
                 "cozy crime", "crime with a great cast", "films about a classical musician"]
        for intent, queries in [("canon", canon), ("taste", taste)]:
            for query in queries:
                with self.subTest(query=query):
                    self.assertEqual(self.s.search_intent(query), intent)

    def test_canon_prompt_has_no_household_hint(self):
        self.s.ai_plan("best all times")
        system, prompt, tokens, *_ = self.llm_text.call_args.args
        self.assertEqual(prompt, "best all times")
        self.assertNotIn(self.model["taste_line"], system + prompt)
        self.assertNotIn("Peter Jackson", system + prompt)
        self.assertIn("across decades and countries", system)
        self.assertIn("including older classics", system)
        self.assertIn("exactly 20 lines", system)
        self.assertIn("at least 7 series and 7 movies", system)
        self.assertIn("then use only that type", system)
        self.assertGreaterEqual(tokens, 600)

    def test_taste_prompt_only_has_experiences_and_themes(self):
        self.s.ai_plan("something cozy for tonight")
        system, prompt, *_ = self.llm_text.call_args.args
        self.assertEqual(prompt, "something cozy for tonight\n(Household taste, only to break ties between equally good "
                         "matches: experiences: Cozy comfort; themes: Detective, Hope)")
        for text in ("Peter Jackson", "Elijah Wood", "HBO", "Marvel Studios", "novel", "creators:", "actors:"):
            self.assertNotIn(text, system + prompt)

    def test_no_model_means_no_hint(self):
        self.s._model["m"] = None
        self.s.ai_plan("something cozy")
        self.assertEqual(self.llm_text.call_args.args[1], "something cozy")

    def test_router_title_still_skips_picks(self):
        from decide_kit import close_server
        self.addClassCleanup(close_server, self.s)
        self.openrouter.return_value = {"answers": {"route": {"choice": "title", "confidence": 0.9}}}
        with patch.object(self.s, "_pool") as pool:
            picks = pool.submit.return_value
            self.assertIsNone(self.s.ai_plan("Castle"))
            pool.submit.assert_called_once()
            picks.cancel.assert_called_once()
            picks.result.assert_not_called()
        self.llm_text.assert_not_called()

    def test_shared_pick_call_keeps_20_lines_and_both_types(self):
        self.llm_text.return_value = "\n".join(f"Title {i}|2000|{'m' if i < 10 else 's'}" for i in range(20))
        first = self.s.ai_future("best all times")
        self.assertIs(first, self.s.ai_future("BEST ALL TIMES"))
        picks = first.result(timeout=2)
        self.assertEqual(len(picks), 20)
        self.assertEqual(sum(p[2] == "series" for p in picks), 10)
        self.llm_text.assert_called_once()

    def search_results(self, query, stype):
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
        self.s._profiles["household"] = {"seen": {"tmdb:1", "tmdb:3", "tmdb:4", "tmdb:6", "tmdb:8", "tmdb:9"},
                                         "negative": {"tmdb:3", "tmdb:8"}}
        for query in ("best all times", "something cozy"):
            for stype, ids in [("movie", [2, 5, 1, 4]), ("series", [7, 10, 6, 9])]:
                with self.subTest(query=query, stype=stype):
                    self.assertEqual([m["id"] for m in self.search_results(query, stype)], [f"tmdb:{i}" for i in ids])

    def test_canon_skips_rerank_and_taste_only_reranks_unseen(self):
        self.s._profiles["household"] = {"seen": {"tmdb:1", "tmdb:4"}, "negative": {"tmdb:3"}}
        with patch.object(self.s, "taste_rerank", side_effect=lambda metas: metas[::-1]) as rerank:
            self.assertEqual([m["id"] for m in self.search_results("best all times", "movie")],
                             ["tmdb:2", "tmdb:5", "tmdb:1", "tmdb:4"])
            rerank.assert_not_called()
            self.assertEqual([m["id"] for m in self.search_results("something cozy", "movie")],
                             ["tmdb:5", "tmdb:2", "tmdb:1", "tmdb:4"])
            self.assertEqual([m["id"] for m in rerank.call_args.args[0]], ["tmdb:2", "tmdb:5"])

    def test_no_profile_preserves_order(self):
        self.assertEqual([m["id"] for m in self.search_results("best all times", "movie")],
                         [f"tmdb:{i}" for i in range(1, 6)])

    def test_taste_rerank_uses_twenty_percent(self):
        self.model["cal"] = 1
        metas = [{"id": f"tmdb:{i}", "type": "movie"} for i in range(4)]
        with patch.dict(self.s._facets, {m["id"]: {} for m in metas}), \
                patch.object(self.s, "score_key", return_value=(0, [])), \
                patch.object(self.s, "match_pct", side_effect=[70, 70, 99, 70]), \
                patch.object(self.s, "with_match", side_effect=lambda meta, **kw: meta):
            self.assertEqual(self.s.taste_rerank(metas), metas)

    def test_seen_intent_table(self):
        for phrase in ("I have not watched", "I haven't seen", "I have never seen", "new to me", "unwatched",
                       "ich habe noch nicht gesehen", "ich habe nie gesehen", "nisam gledao", "nisam gledala",
                       "which i have not watched", "I haven't watched", "I've never seen"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.s.seen_intent("anime " + phrase), ("unseen", "anime"))
        for phrase in ("I have watched", "I've seen", "I have already seen", "rewatch", "ich habe schon gesehen",
                       "gledao sam", "gledala sam", "which i have watched"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.s.seen_intent("anime " + phrase), ("seen", "anime"))
        for query in ("watchmen", "seen from space", "Castle"):
            self.assertEqual(self.s.seen_intent(query), (None, query))

    def history(self):
        entries = [dict(key=f"tmdb:{i}", id=i, title=f"Title {i}", year="2000", kind="movie",
                        stype="movie", age=i, weight=-1 if i == 3 else 1, signal="watched",
                        by=["ana" if i != 4 else "ben"], item={}) for i in (1, 3, 4)]
        self.s._profiles["household"] = {"entries": entries, "seen": {e["key"] for e in entries},
                                         "negative": {"tmdb:3"}}
        return entries

    def test_seen_intent_requires_viewer(self):
        for query in ("movies about people who have never seen the ocean",
                      "movies about a woman who has already seen the future",
                      "filmovi o ljudima koji su gledao more"):
            with self.subTest(query=query):
                self.assertEqual(self.s.seen_intent(query), (None, query))
        self.history()
        query = "movies about people who have never seen the ocean"
        metas = self.search_results(query, "movie")
        self.assertEqual([m["id"] for m in metas], ["tmdb:2", "tmdb:5", "tmdb:1", "tmdb:4"])
        self.assertIn(query, self.llm_text.call_args.args[1])
        for phrase in ("we have watched", "watched by me", "our watched", "wir haben gesehen",
                       "gledao sam", "gledala sam", "unwatched", "not seen yet"):
            with self.subTest(phrase=phrase):
                intent = "unseen" if phrase in ("unwatched", "not seen yet") else "seen"
                self.assertEqual(self.s.seen_intent("anime " + phrase), (intent, "anime"))

    def test_seen_intent_extended_negations(self):
        for phrase in ("I have not already seen", "nisam nikad gledao", "nisam nikada gledala",
                       "nismo nikad gledali", "ich habe noch nie gesehen", "we have never watched"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.s.seen_intent("anime " + phrase), ("unseen", "anime"))

    def test_seen_history_excludes_watchlist_and_interest(self):
        entries = self.history()
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
        self.assertEqual([m["id"] for m in self.search_results("anime which i have not watched", "movie")],
                         ["tmdb:2", "tmdb:5"])
        self.assertNotIn("not watched", self.llm_text.call_args.args[1])

    def seen_results(self, scope=None):
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
        self.assertNotIn("have watched", prompt)
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
        self.history()
        with patch.object(self.s, "SCOPES", {"ana": "ana", "ben": "ben", "together": "both"}), \
                patch.object(self.s, "PEOPLE", ("ana", "ben")):
            for scope, ids in [("ana", [2, 4, 5]), ("ben", [1, 2, 3, 5]), ("together", [2, 5])]:
                with self.s.addon_scope(scope):
                    self.assertEqual([m["id"] for m in self.search_results("anime I have not watched", "movie")],
                                     [f"tmdb:{i}" for i in ids])
                    self.assertEqual([m["id"] for m in self.search_results("best all times", "movie")],
                                     ["tmdb:2", "tmdb:5", "tmdb:1", "tmdb:4"])

    def test_seen_empty_llm_stays_empty(self):
        self.history()
        self.tmdb.return_value = {"results": [{"id": 9, "title": "Unrelated"}]}
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "looks_like_title", return_value=False), \
                patch.object(self.s, "superseded", return_value=False):
            self.assertEqual(self.s.search("movie", "anime I have already seen", "en-US"), {"metas": []})

    def test_seen_history_cap_recency_and_series(self):
        entries = [dict(key=f"tmdb:{i}", id=i, title=f"Title {i}", year=2000, kind="tv",
                        stype="series", age=305-i, weight=1, signal="watched", by=["ana"], item={})
                   for i in range(1, 306)]
        self.s._profiles["household"] = {"entries": entries, "seen": {e["key"] for e in entries}, "negative": set()}
        self.llm_text.return_value = "Title 305|2000|s|Recent\nTitle 1|2000|s|Too old"
        with patch.object(self.s, "OR_KEY", "offline"), patch.object(self.s, "superseded", return_value=False), \
                patch.object(self.s, "resolve_pick", side_effect=AssertionError("no title resolution")):
            metas = self.s.search("series", "anime I have already seen", "en-US")["metas"]
        self.assertEqual([m["id"] for m in metas], ["tmdb:305"])
        prompt = self.llm_text.call_args.args[1]
        self.assertEqual(len(prompt.splitlines()[2:]), 300)
        self.assertEqual(prompt.splitlines()[2], "Title 305|2000|s")
        self.assertNotIn("Title 1|", prompt)

    def test_seen_plain_fallbacks(self):
        self.history()
        query = "anime I have already seen"
        for mode in ("no_key", "title_only", "exact", "router_title"):
            self.s._ai.clear()
            self.tmdb.return_value = {"results": [{"id": 1, "title": query if mode == "exact" else "Other"}]}
            with patch.object(self.s, "OR_KEY", "" if mode == "no_key" else "offline"), \
                    patch.object(self.s, "looks_like_title", return_value=False), \
                    patch.object(self.s, "superseded", return_value=False), \
                    patch.object(self.s, "decide", return_value={"answer": {"choice": "title", "confidence": .9}}):
                self.assertEqual(self.s.search("movie", query, "en-US", title_only=mode == "title_only")
                                 ["metas"][0]["id"], "tmdb:1")

    def test_plain_title_search_keeps_seen_and_negative(self):
        self.s._profiles["household"] = {"seen": {"tmdb:1"}, "negative": {"tmdb:1"}}
        self.tmdb.return_value = {"results": [{"id": 1, "title": "Castle"}]}
        with patch.object(self.s, "OR_KEY", "offline"):
            self.assertEqual(self.s.search("movie", "Castle", "en-US")["metas"][0]["id"], "tmdb:1")
        self.openrouter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
