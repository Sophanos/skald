import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import members
from skald.__main__ import main


class SuggestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.now = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc).timestamp()
        self.searches = [{"p": "ana", "q": q, "t": self.now - (i * 7 + 2) * 86400 + 8 * 3600}
                         for i, q in enumerate(("Something cozy please", "cozy", "Bitte cozy"))]

    def suggest(self, searches=None, plays=(), existing=(), dismissed=()):
        from skald.suggest import suggest
        return suggest(self.searches if searches is None else searches, plays, "UTC", existing, dismissed, self.now)

    def test_log_search_records_person(self):
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        with patch.object(s, "who_watching", return_value=("ana", "manual")):
            s.log_search("cozy")
            with s.addon_scope("ben"):
                s.log_search("crime")
            with s.addon_scope("ben"):
                s.log_search("cozy")
        with patch.object(s, "who_watching", return_value=("both", "prior")):
            s.log_search("unknown")
        rows = [json.loads(line) for line in (self.data / "searches.jsonl").read_text().splitlines()]
        self.assertEqual([r.get("p") for r in rows], ["ana", "ben", "ben", None])
        self.assertEqual(s.recent_searches(), ["crime", "cozy", "unknown"])

    def test_weekly_search_habit_suggested(self):
        rows = self.suggest()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person"], "ana")
        self.assertEqual(rows[0]["text"], "cozy")
        self.assertEqual(rows[0]["evidence"], {"count": 3, "weeks": 3})
        self.assertEqual(rows[0]["when"]["days"], [5])
        other = [{**r, "p": "ben", "q": "secret crime"} for r in self.searches]
        self.assertTrue(any(r["person"] == "ben" for r in self.suggest(self.searches + other)))
        typed = [{**r, "q": "coz", "t": r["t"] - 10} for r in self.searches]
        self.assertEqual(len(self.suggest([{**r, "q": "cozy"} for r in self.searches] + typed)), 1)
        shifted = [{**r, "t": r["t"] + i * 5 * 3600} for i, r in enumerate(self.searches)]
        self.assertEqual(self.suggest(shifted), [])
        stale = [{**r, "t": r["t"] - 56 * 86400} for r in self.searches]
        self.assertEqual(self.suggest(stale), [])
        weekend = [{**r, "t": r["t"] + (i % 2) * 86400} for i, r in enumerate(self.searches)]
        self.assertEqual(self.suggest(weekend)[0]["when"]["days"], [5, 6])

    def test_two_weeks_not_enough(self):
        self.assertEqual(self.suggest(self.searches[:2] * 5), [])
        many = [{**r, "q": f"theme {j}"} for r in self.searches for j in range(5)]
        self.assertEqual(len(self.suggest(many)), 3)

    def test_play_facet_habit(self):
        plays = [{"person": "ana", "t": r["t"], "genres": ["Documentary"], "kind": "movie"}
                 for r in self.searches]
        plays += [{**p, "t": p["t"] - j * 86400, "genres": ["Drama"]}
                  for p in plays[:] for j in (1, 2, 3)]
        rows = self.suggest([], plays)
        self.assertTrue(any(r["text"] == "documentary" for r in rows))
        self.assertFalse(any(r["text"] == "movie" for r in rows))
        for p in plays:
            p["origin_country"] = ["DE"] if p["genres"] == ["Documentary"] else ["US"]
        self.assertTrue(any(r["text"] == "de" for r in self.suggest([], plays)))
        for i, p in enumerate(plays[:3]):
            p["t"] += i * 5 * 3600
        self.assertTrue(any(r["text"] == "documentary" for r in self.suggest([], plays)))

    def test_existing_wish_and_dismissed_skipped(self):
        row = self.suggest()[0]
        self.assertEqual(self.suggest(existing=[{"scope": "ana", "text": "cozy movies", "when": row["when"]}]), [])
        self.assertEqual(self.suggest(dismissed=[{"person": "ana", "text": "cozy", "t": self.now}]), [])
        self.assertTrue(self.suggest(dismissed=[{"person": "ben", "text": "cozy", "t": self.now}]))
        self.assertTrue(self.suggest(dismissed=[{"person": "ana", "text": "cozy", "t": self.now - 91 * 86400}]))

    def test_child_gets_none(self):
        self.assertEqual(self.suggest([{**r, "kids_age": 10} for r in self.searches]), [])

    def test_cli_suggest_read_only(self):
        cfg = members.fresh_config(["ana", "ben"])
        (self.data / "skald.json").write_text(json.dumps(cfg))
        other = [{**r, "p": "ben", "q": "secret crime"} for r in self.searches]
        (self.data / "searches.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.searches + other))
        (self.data / "suggest-dismissed.json").write_text(json.dumps([{
            "person": "ana", "text": "old", "t": self.now - 91 * 86400}]))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.dict(os.environ, {"DATA_DIR": str(self.data)}), patch("time.time", return_value=self.now), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            out = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ana", "--json"], out), 0, out.getvalue())
            self.assertNotIn("secret", out.getvalue())
            rows = json.loads(out.getvalue())["suggestions"]
            self.assertTrue(all(r["person"] == "ana" for r in rows))
            ben = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ben", "--json"], ben), 0)
            self.assertEqual(json.loads(ben.getvalue())["suggestions"][0]["text"], "secret crime")
            self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)
            self.assertTrue(rows[0]["id"])
            out = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ana", "--dismiss", rows[0]["id"], "--json"], out), 0)
            self.assertEqual((self.data / "suggest-dismissed.json").stat().st_mode & 0o777, 0o600)
            out = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ana", "--json"], out), 0)
            self.assertEqual(json.loads(out.getvalue())["suggestions"], [])
            self.cached_plays()
            before = {p.name: p.read_bytes() for p in self.data.iterdir()}
            out = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ana", "--json"], out), 0)
            self.assertEqual(json.loads(out.getvalue())["suggestions"][0]["evidence"]["count"], 3)
            self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)
            cfg["taste"]["people"] = {"ana": {"prefs": {"kids_age": 10}}}
            (self.data / "skald.json").write_text(json.dumps(cfg))
            out = io.StringIO()
            self.assertEqual(main(["suggest", "--person", "ana", "--json"], out), 0)
            self.assertEqual(json.loads(out.getvalue())["suggestions"], [])

    def cached_plays(self, attributed=True):
        items = [{"id": f"tmdb:{i + 1}", "profile": "ana" if attributed else "household",
                  "type": "movie", "at": r["t"] * 1000, "played": True, "genres": ["Documentary"]}
                 for i, r in enumerate(self.searches)]
        items += [{**item, "id": f"tmdb:{10 + i * 3 + j}", "at": item["at"] - j * 86400000, "genres": ["Drama"]}
                  for i, item in enumerate(items[:]) for j in (1, 2, 3)]
        (self.data / "history.json").write_text(json.dumps({"items": items}))
        return items

    def test_cached_plays_without_profile(self):
        from skald.engine import suggestions
        cfg = members.fresh_config(["ana", "ben"])
        self.cached_plays()
        self.assertFalse((self.data / "profile.json").exists())
        with patch("time.time", return_value=self.now):
            rows = suggestions(self.data, "ana", cfg)["suggestions"]
        self.assertEqual(rows[0]["text"], "documentary")
        self.assertEqual(rows[0]["evidence"], {"count": 3, "weeks": 3})
        items = json.loads((self.data / "history.json").read_text())["items"]
        (self.data / "history.json").unlink()
        for source, cid in (("trakt", "trakt_api"), ("mdblist", "mdblist"), ("stremio", "stremio")):
            cfg["connectors"] = {cid: {"accounts": {"ana": "file:unused.json"}}}
            (self.data / (source + "-ana.history.json")).write_text(json.dumps({"items": [
                {**item, "profile": "me"} for item in items]}))
            with patch("time.time", return_value=self.now):
                self.assertEqual(suggestions(self.data, "ana", cfg)["suggestions"][0]["evidence"]["count"], 3)

    def test_unknown_plays_not_attributed_by_rating(self):
        from skald.engine import suggestions
        cfg = members.fresh_config(["ana", "ben"])
        items = self.cached_plays(attributed=False)
        (self.data / "profile.json").write_text(json.dumps({"entries": [
            {"key": item["id"], "by": ["ana"], "kind": "movie", "genres": item["genres"],
             "signal": "loved", "who": "remote", "flags": ["remote"], "item": item} for item in items]}))
        (self.data / "learn.json").write_text(json.dumps({"ctx": [
            {"key": item["id"], "at": item["at"] / 1000} for item in items]}))
        with patch("time.time", return_value=self.now):
            self.assertEqual(suggestions(self.data, "ana", cfg)["suggestions"], [])

    def test_household_week_boundary(self):
        from skald.suggest import suggest
        def search(date):
            return {"p": "ana", "q": "cozy", "t": dt.datetime.fromisoformat(date).timestamp()}
        dates = [search(d) for d in ("2026-09-27T22:30:00+00:00", "2026-10-04T22:30:00+00:00", "2026-10-11T22:30:00+00:00")]
        row = suggest(dates, [], "Europe/Berlin", [], [], self.now)[0]
        self.assertEqual(row["when"], {"days": [0], "from_hour": 0, "to_hour": 0})
        self.assertEqual(row["evidence"]["weeks"], 3)
        dates[-1] = search("2026-10-05T22:30:00+00:00")
        self.assertEqual(suggest(dates, [], "Europe/Berlin", [], [], self.now), [])
