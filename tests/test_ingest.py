import copy
from dataclasses import asdict
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from skald.engine import Engine
from skald.ingest import Candidate, parse, rating_signal
from skald.ingest.chrome import url_hint
from skald.ingest.resolve import Resolver, match_score, remote_provider

FIXTURES = Path(__file__).parent / "fixtures/ingest"
from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=True)
ANA, BEN = snapshot_people(SNAPSHOT)


class ParserTests(unittest.TestCase):
    def test_trakt(self):
        rows = parse("trakt", FIXTURES / "trakt.json")
        self.assertEqual([(c.title, c.signal, c.n) for c in rows],
                         [("Castle", "seen", 2), ("The Matrix", "love", 1), ("Arrival", "interest", 1)])
        self.assertEqual(rows[0].ids["tmdb"], 1419)
        self.assertEqual(rows[0].last_ts, "2026-10-11")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchlist.json"
            path.write_text(json.dumps([{"movie": {"title": "Arrival", "year": 2016, "ids": {"tmdb": 329865}}}]))
            self.assertEqual(parse("trakt", path)[0].signal, "interest")

    def test_netflix_both_formats_and_person_map(self):
        with self.assertRaisesRegex(ValueError, "unmapped profile 'Export A' — add --profile-map 'Export A=ana'"):
            parse("netflix", FIXTURES / "ViewingActivity.csv")
        rows = parse("netflix", FIXTURES / "ViewingActivity.csv", profile_map={"Export A": "ana"})
        self.assertEqual((rows[0].title, rows[0].kind, rows[0].n, rows[0].person), ("Castle", "tv", 3, "ana"))
        self.assertEqual(rows[1].signal, "interest")
        self.assertNotIn("Export A", json.dumps([asdict(c) for c in rows]))
        legacy = parse("netflix", FIXTURES / "NetflixViewingHistory.csv")
        self.assertEqual(legacy[0].last_ts, "2026-10-10")
        self.assertEqual(legacy[1].title, "Castle")

    def test_youtube_only_promotional_titles(self):
        rows = parse("youtube", FIXTURES / "watch-history.json")
        self.assertEqual([(r.title, r.year, r.signal) for r in rows], [("Arrival", "2016", "interest"), ("Castle", "", "interest")])
        raw = json.dumps([asdict(c) for c in rows])
        self.assertNotIn("http", raw)
        self.assertNotIn("cooking", raw)

    def test_letterboxd_all_exports(self):
        rows = parse("letterboxd", FIXTURES / "letterboxd")
        self.assertEqual({r.title: r.signal for r in rows}, {"The Matrix": "love", "Bad Film": "not_for_us", "Arrival": "seen", "Castle": "love", "Future Film": "interest"})
        self.assertEqual(next(c for c in rows if c.title == "Arrival").n, 1)
        self.assertEqual(len(parse("letterboxd", FIXTURES / "letterboxd/ratings.csv")), 2)

    def test_imdb_ratings_watchlist(self):
        rows = parse("imdb", FIXTURES / "imdb-ratings.csv")
        self.assertEqual((rows[0].signal, rows[0].ids), ("love", {"imdb": "tt0133093"}))
        self.assertEqual((rows[1].kind, rows[1].signal), ("tv", "like"))
        self.assertEqual(parse("imdb", FIXTURES / "imdb-watchlist.csv")[0].signal, "interest")

    def test_plain(self):
        rows = parse("plain", FIXTURES / "plain.txt")
        self.assertEqual([(r.title, r.year) for r in rows], [("Castle", "2009"), ("The Matrix", "1999")])

    def test_chrome_copy_and_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "History"
            with sqlite3.connect(path) as db:
                db.executescript((FIXTURES / "chrome.sql").read_text())
            before = path.read_bytes()
            rows = parse("chrome", path)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(rows), 5)  # Letterboxd and Wikipedia visits to Arrival coalesce.
        self.assertEqual(rows[0].ids, {"imdb": "tt0133093"})
        self.assertEqual(rows[1].ids, {"tmdb": 1419})
        raw = json.dumps([asdict(c) for c in rows])
        self.assertNotIn("http", raw)
        self.assertNotIn("DO NOT", raw)
        self.assertTrue(all(len(c.last_ts) == 10 for c in rows))
        for url in ("https://imdb.com@evil.com/title/tt1", "file:///title/tt1", "https://evil.com/imdb.com/title/tt1", "https://imdb.com/title/tt1/secret", "https://en.wikipedia.org/wiki/Private_hobby"):
            self.assertIsNone(url_hint(url))
        self.assertIsNotNone(url_hint("https://de.wikipedia.org/wiki/Castle_(TV_series)"))

    def test_signal_table_boundaries(self):
        expected = {0: "not_for_us", 4: "not_for_us", 5: "meh", 6: "like", 7.5: "like", 8: "love", 10: "love"}
        for rating, signal in expected.items():
            self.assertEqual(rating_signal(rating), signal)
            self.assertEqual(rating_signal(rating / 2, scale=5), signal)
        self.assertEqual(rating_signal(None, liked=True), "love")
        self.assertEqual(rating_signal(None, default="interest"), "interest")
        for bad in (-1, 11, float("nan")):
            with self.assertRaises(ValueError):
                rating_signal(bad)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "resolve.json"
        self.movie = {"id": 603, "title": "The Matrix", "year": "1999", "kind": "movie", "popularity": 100}

    def test_match_remakes_year_and_office(self):
        candidate = Candidate("The Thing", "1982", "movie")
        original = {"id": 1, "title": "The Thing", "year": "1982", "kind": "movie"}
        remake = {**original, "id": 2, "year": "2011"}
        self.assertGreaterEqual(match_score(candidate, original), .88)
        self.assertLess(match_score(candidate, remake), .88)
        self.assertGreaterEqual(match_score(candidate, {**original, "year": "1983"}), .88)
        office = Candidate("The Office", "2005", "tv")
        self.assertGreater(match_score(office, {"name": "The Office", "first_air_date": "2005-01-01"}),
                           match_score(office, {"name": "The Office", "first_air_date": "2001-01-01"}) + .08)

    def test_anime_romaji_english_alias(self):
        anime = Candidate("Shingeki no Kyojin", "2013", "tv")
        choice = {"name": "Attack on Titan", "original_name": "進撃の巨人", "aliases": ["Shingeki no Kyojin"], "year": "2013", "kind": "tv"}
        self.assertGreaterEqual(match_score(anime, choice), .88)
        self.assertLess(match_score(anime, {**choice, "aliases": []}), .88)

    def test_exact_order_and_cache_free_rerun(self):
        calls = []
        def tmdb(path, **kwargs):
            calls.append(path)
            if path == "movie/603":
                return self.movie
            if path.startswith("find/"):
                return {"movie_results": [self.movie]}
            return {"results": [self.movie]}
        candidates = [Candidate("Wrong export spelling", "", "movie", {"tmdb": 603}),
                      Candidate("The Matrix", "1999", "movie", {"imdb": "tt0133093"}),
                      Candidate("The Matrix", "1999", "movie")]
        decide = Mock(side_effect=AssertionError("exact/search should not call AI"))
        rows = Resolver(tmdb, decide, self.cache).resolve(candidates)
        self.assertEqual([r["method"] for r in rows], ["id", "id", "search"])
        self.assertEqual(calls, ["movie/603", "find/tt0133093", "search/multi"])
        self.assertTrue(all(r["high_confidence"] for r in rows))
        cached = Resolver(Mock(side_effect=AssertionError("cache missed")), decide, self.cache).resolve(candidates)
        self.assertEqual(rows, cached)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)

    def ambiguous_tmdb(self, path, **kwargs):
        return {"results": [{"id": 1, "title": "The Office", "year": "2001", "kind": "tv"},
                            {"id": 2, "title": "The Office", "year": "2005", "kind": "tv"}]}

    def test_margin_none_batch_and_hard_cost_cap(self):
        calls = []
        def decide(batch, max_usd):
            calls.append((len(batch), max_usd))
            return {"answers": {str(i): {"choice": "none", "confidence": .9} for i in range(len(batch))}, "usage": {"cost": max_usd}}
        candidates = [Candidate("The Office" + " " * i, "", "tv", {"other": i}) for i in range(26)]
        resolver = Resolver(self.ambiguous_tmdb, decide, self.cache, max_ai_usd=.005)
        rows = resolver.resolve(candidates)
        self.assertEqual(calls, [(25, .005)])
        self.assertEqual(resolver.spent, .005)
        self.assertEqual(rows[-1]["method"], "unresolved")
        self.assertTrue(all(not r["match"] for r in rows))
        self.assertEqual(Resolver(self.ambiguous_tmdb, decide, self.cache, max_ai_usd=0).resolve(candidates), rows)

    def test_batches_25_and_cap_violation(self):
        sizes = []
        def decide(batch, max_usd):
            sizes.append(len(batch))
            return {"answers": {str(i): {"choice": "2", "confidence": .99} for i in range(len(batch))}, "usage": {"cost": .001}}
        candidates = [Candidate("The Office", "", "tv", {"other": i}) for i in range(26)]
        rows = Resolver(self.ambiguous_tmdb, decide, self.cache).resolve(candidates)
        self.assertEqual(sizes, [25, 1])
        self.assertTrue(all(r["method"] == "ai" and r["high_confidence"] for r in rows))
        with self.assertRaisesRegex(ValueError, "exceeded"):
            Resolver(self.ambiguous_tmdb, lambda *a, **k: {"usage": {"cost": 1}}, Path(self.temp.name) / "other.json", max_ai_usd=.001).resolve([Candidate("The Office")])

    def test_remote_adapter_uses_public_choices(self):
        request = Mock(return_value={"results": [{"answer": {"pick": {"key": "tmdb:2", "kind": "tv"}},
                                                  "confidence": .99, "source": "llm", "receipt": "d_toy", "cost_usd": .0008}]})
        reply = remote_provider(request)([{"candidate": {"title": "The Office", "year": "", "kind": "tv"},
                                           "choices": [{"id": 2, "title": "The Office", "year": "2005", "kind": "tv"}]}], .02)
        path, body = request.call_args.args
        self.assertEqual(path, "/api/v1/decide/resolve_title")
        self.assertEqual(set(body), {"items", "no_llm"})
        self.assertEqual(body["items"][0]["choices"][0]["key"], "tmdb:2")
        self.assertIsNone(body["items"][0]["year"])
        self.assertEqual(reply["answers"]["0"]["choice"], "2")
        self.assertEqual(reply["receipts"], {"0": "d_toy"})
        self.assertEqual(reply["usage"]["cost"], .0008)


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), "set SKALD_SNAPSHOT_U to run snapshot gates")
class FoldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(SNAPSHOT)
        cls.server = cls.engine._server

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_patch = patch.object(self.server, "DATA", self.temp.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc).timestamp()

    def event(self, **kwargs):
        return {"key": "tmdb:1", "kind": "tv", "title": "Synthetic", "year": "2000", "who": ANA, "action": "love", "src": "netflix", "conf": 1., "n": 3, "t": "2026-10-11", **kwargs}

    def old(self, **kwargs):
        return {"key": "tmdb:1", "id": 1, "kind": "tv", "stype": "series", "title": "Synthetic", "year": "2000",
                "by": [ANA], "who": "owner", "flags": [], "genres": [], "signal": "watched", "weight": 1., "age": 0, "item": {}, **kwargs}

    def fold(self, entry):
        entries = [entry]
        self.server.merge_imports(entries, {entry["key"]: entry}, self.now)
        return entries

    def test_weaker_raises_and_people_do_not_bleed(self):
        self.server.write_imports([self.event()])
        entries = self.fold(self.old(by=[BEN, ANA]))
        self.assertEqual([(e["by"], e["signal"]) for e in entries], [([BEN], "watched"), ([ANA], "loved")])
        self.assertEqual(entries[1]["who"], "import")
        self.assertEqual(entries[1]["flags"], ["import", "netflix"])
        self.assertAlmostEqual(entries[1]["weight"], 2.4)

    def test_tv_remote_and_told_are_protected(self):
        self.server.write_imports([self.event()])
        for who in ("tv", "both", "remote", "told"):
            old = self.old(who=who, item={"last_episode": [2, 3]})
            result = self.fold(old)
            self.assertEqual(result, [old])
            self.assertEqual(old["signal"], "watched")
            self.assertEqual(old["item"]["last_episode"], [2, 3])
        entries = self.fold(self.old(who="tv", signal="opened", weight=.2))
        self.assertEqual(entries[0]["who"], "import")

    def test_hook_precedes_remote_and_hash_changes(self):
        before = self.server.taste_hash("household", [])
        added = self.server.write_imports([self.event()])
        after = self.server.taste_hash("household", [])
        self.assertNotEqual(before, after)
        self.server.write_feedback({"key": "tmdb:1", "kind": "tv", "title": "Synthetic", "year": "2000", "who": ANA, "action": "not_for_us"})
        with patch.object(self.server, "add_person_loves", return_value=set()):
            p = self.server.build_profile([], [])
        self.assertEqual(p["entries"][0]["signal"], "disliked")
        self.assertEqual(p["entries"][0]["who"], "remote")
        self.server.undo_import(added[0]["id"])
        self.assertNotEqual(after, self.server.taste_hash("household", []))

    def test_trust_interest_cap_depth_and_decay(self):
        events = [self.event(key=f"tmdb:{i}", action="interest", src="chrome") for i in range(1, 31)]
        self.server.write_imports(events)
        entries = self.server.import_entries(self.now)
        self.assertEqual(len(entries), 20)
        self.assertAlmostEqual(entries[0]["weight"], .08)
        self.assertEqual(self.server.import_hash(self.now), self.server.import_hash(self.now + 3600))
        self.assertLess(self.server.import_entries(self.now + 60 * self.server.DAY)[0]["weight"], .08)
        self.assertEqual(self.server.import_entries(self.now + 120 * self.server.DAY), [])
        self.server.write_imports([self.event(key="tmdb:999", action="seen", n=58)])
        entry = self.server.import_entries(self.now)[0]
        self.assertEqual(entry["n"], 58)
        self.assertEqual(entry["item"]["episodes_played"], 58)
        self.assertAlmostEqual(entry["weight"], .8)

    def test_remote_replaces_imports_but_preserves_real_plays(self):
        s = self.server
        s.write_imports([self.event(who="both"), self.event(src="imdb", who=BEN)])
        s.write_feedback({"key": "tmdb:1", "kind": "tv", "title": "Synthetic", "year": "2000",
                          "who": ANA, "action": "like"})
        for who in ("import", "tv", "both", "owner"):
            old = self.old(who=who, by=[BEN] if who == "owner" else [ANA], flags=["import"] if who == "import" else [],
                           item={"last_episode": [2, 3], "episodes_played": 3})
            entries, by_key = [old], {old["key"]: old}
            s.merge_feedback(entries, by_key, self.now)
            # Another person's owner play survives beside the reactor's opinion.
            if who == "owner":
                self.assertEqual(entries, [old, *s.feedback_entries(self.now)])
                self.assertEqual((old["by"], old["signal"], old["weight"]), ([BEN], "watched", 1.))
                continue
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0]["signal"], entries[0]["weight"]), ("liked", 2.))
            if who == "import":
                self.assertEqual(entries, s.feedback_entries(self.now))
            else:
                self.assertIs(entries[0], old)
                self.assertEqual(old["who"], who)
                self.assertEqual(old["item"]["last_episode"], [2, 3])
        with patch.object(s, "add_person_loves", return_value=set()), patch.object(s.time, "time", return_value=self.now):
            entries = s.build_profile([], [])["entries"]
        # Ana's reaction removes only her import slot; Ben keeps his rating.
        self.assertEqual([(e["by"], e["signal"]) for e in entries], [([BEN], "loved"), ([ANA], "liked")])
        self.assertEqual(entries[1:], s.feedback_entries(self.now))
        self.assertEqual(entries[0], s.import_entries(self.now)[0])

    def test_corrupt_imports_do_not_break_profile_or_hash(self):
        s = self.server
        event = s.write_imports([self.event()])[0]
        path = Path(self.temp.name, "imports.jsonl")
        good = json.dumps(event).encode() + b"\n"
        bad = [b"null", b"[]", b"\xff", b'{"broken":', b"[" * 1100,
               b"x" * 10000, json.dumps({**event, "t": "2026-02-30"}).encode(),
               json.dumps({**event, "id": []}).encode()]
        expected = s.import_hash(self.now)
        for line in bad:
            path.write_bytes(good + line + b"\n" + good)
            self.assertEqual(s.import_hash(self.now), expected)
            with patch.object(s, "add_person_loves", return_value=set()):
                self.assertEqual(len(s.build_profile([], [])["entries"]), 1)
            s.taste_hash("household", [])
        with patch.object(s, "IMPORT_MAX", 2):
            self.assertEqual(len(s.read_imports()), 2)
        with patch("builtins.open", side_effect=PermissionError):
            self.assertEqual(s.read_imports(), [])

    def test_import_hash_folds_weights_and_caches_file_stamp(self):
        s = self.server
        self.assertIsNone(s.import_hash(self.now))
        event = s.write_imports([self.event()])[0]
        with patch.object(s, "read_imports", wraps=s.read_imports) as read:
            with patch.object(s.json, "dumps", wraps=json.dumps) as dump:
                first = s.import_hash(self.now)
                self.assertTrue(dump.called)
                self.assertTrue(all(isinstance(call.args[0], tuple) and len(call.args[0]) == 3 for call in dump.call_args_list))
            for _ in range(3):
                self.assertEqual(s.import_hash(self.now + 3600), first)
                s.import_entries(self.now)
            self.assertEqual(read.call_count, 1)
            path = Path(self.temp.name, "imports.jsonl")
            path.write_text(json.dumps({**event, "id": "a" * 32, "n": 500, "title": "Renamed"}) + "\n")
            self.assertEqual(s.import_hash(self.now), first)
            self.assertEqual(read.call_count, 2)
            path.write_text(json.dumps({**event, "action": "like"}) + "\n")
            self.assertNotEqual(s.import_hash(self.now), first)
            self.assertEqual(read.call_count, 3)
            stamp = path.stat()
            liked = s.import_hash(self.now)
            path.write_text(path.read_text().replace('"like"', '"love"'))
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
            self.assertEqual(s.import_hash(self.now), first)  # mtime alone invalidates.
            self.assertEqual(read.call_count, 4)
            path.write_text(json.dumps({**event, "action": "like", "n": 500}) + "\n")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
            self.assertEqual(s.import_hash(self.now), liked)  # size alone invalidates.
            self.assertEqual(read.call_count, 5)
            path.write_text(json.dumps({**event, "conf": 2 / 3}) + "\n")
            self.assertEqual(s.import_hash(self.now), liked)  # only folded weights, not action labels.
        path.unlink()
        self.assertIsNone(s.import_hash(self.now))

    def test_interest_both_respects_person_cap(self):
        events = [self.event(key=f"tmdb:{i}", action="interest", who="both", src="chrome") for i in range(1, 21)]
        events += [self.event(key=f"tmdb:{i}", action="interest", src="youtube") for i in range(21, 41)]
        self.server.write_imports(events)
        entries = self.server.import_entries(self.now)
        self.assertEqual(sum(ANA in e["by"] for e in entries), 20)

    def test_dedupe_undo_atomic_failure_and_cap(self):
        event = self.event()
        added = self.server.write_imports([event, event])
        self.assertEqual(len(added), 1)
        self.assertEqual(self.server.write_imports([event]), [])
        updated = self.server.write_imports([self.event(action="like")])[0]
        self.assertEqual(self.server.active_imports()[0]["action"], "like")
        self.server.undo_import(updated["id"])
        self.assertEqual(self.server.active_imports()[0]["action"], "love")
        path = Path(self.temp.name) / "imports.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        with patch.object(self.server.os, "replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.server.write_imports([self.event(action="seen")])
        self.assertEqual(path.read_bytes(), before)
        with patch.object(self.server, "IMPORT_MAX", 1):
            with self.assertRaisesRegex(ValueError, "limit"):
                self.server.write_imports([self.event(key="tmdb:2")])
        self.assertEqual(path.read_bytes(), before)

    def test_batch_preserves_rating_over_watched_and_zero_conf_has_no_signal(self):
        events = self.server.write_imports([self.event(action="love", n=1), self.event(action="seen", n=58)])
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["action"], events[0]["n"]), ("love", 58))
        self.server.write_imports([self.event(key="tmdb:2", conf=0)])
        self.assertEqual([e["key"] for e in self.server.import_entries(self.now)], ["tmdb:1"])

    def test_boundary_validation_no_partial_batch(self):
        for changes in ({"key": "tt1"}, {"who": "Export A"}, {"conf": float("nan")}, {"conf": True}, {"n": 0},
                        {"t": "2026-10-11T21:00:00Z"}, {"src": "random"}, {"url": "private"}, {"title": "http://private"}):
            with self.assertRaises(ValueError):
                self.server.write_imports([self.event(), self.event(**changes)])
            self.assertFalse((Path(self.temp.name) / "imports.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
