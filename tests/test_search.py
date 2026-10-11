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

    def test_plain_title_search_keeps_seen_and_negative(self):
        self.s._profiles["household"] = {"seen": {"tmdb:1"}, "negative": {"tmdb:1"}}
        self.tmdb.return_value = {"results": [{"id": 1, "title": "Castle"}]}
        with patch.object(self.s, "OR_KEY", "offline"):
            self.assertEqual(self.s.search("movie", "Castle", "en-US")["metas"][0]["id"], "tmdb:1")
        self.openrouter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
