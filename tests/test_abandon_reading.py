"""Skald's reading of started-then-stopped titles, from synthetic facets only."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import write_legacy, load_server, close_server


def rec(title, d=(), a=(), k=(), g=(), collection=None):
    return {"title": title, "kind": "movie", "y": "2020", "d": [[i, f"Director {i}"] for i in d],
            "a": [[i, f"Actor {i}"] for i in a], "k": [[i, f"keyword {i}"] for i in k], "g": list(g),
            "collection": collection}


class AbandonReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        write_legacy(Path(temp.name))
        self.s = load_server(Path(temp.name))
        self.addCleanup(close_server, self.s)
        self.ana, self.ben = self.s.PEOPLE[0], self.s.PEOPLE[1]
        desert = dict(k=(1, 2, 3), g=("Science Fiction", "Adventure"))
        self.s._facets.update({
            "tmdb:1": rec("Synthetic desert", d=(7,), a=(70, 71, 72), collection=700, **desert),
            "tmdb:2": rec("Synthetic dunes", d=(8,), a=(80, 72), **desert),
            "tmdb:4": rec("Synthetic favourite", d=(4,), a=(400,), k=(70,), g=("Western",)),
            "tmdb:3": rec("Synthetic sands", d=(9,), a=(90,), **desert),
            "tmdb:10": rec("Same director", d=(7,), a=(10,), k=(40,), g=("Drama",)),
            "tmdb:11": rec("Same saga", d=(11,), a=(11,), k=(41,), g=("Drama",), collection=700),
            "tmdb:12": rec("Lead of one watch", d=(12,), a=(71, 12), k=(42,), g=("Drama",)),
            "tmdb:20": rec("Lead of two watches", d=(20,), a=(72, 20), k=(43,), g=("Drama",)),
            "tmdb:21": rec("Lead of a favourite", d=(21,), a=(400, 21), k=(44,), g=("Drama",)),
            "tmdb:13": rec("Close in taste", d=(13,), a=(13,), **desert),
            "tmdb:14": rec("Far from taste", d=(14,), a=(14,), k=(50, 51), g=("Family",)),
            "tmdb:15": rec("Pressed abandon", d=(7,), a=(15,), **desert),
            "tmdb:17": rec("Stopped twin one", d=(99,), a=(17,), k=(60,), g=("Horror",)),
            "tmdb:18": rec("Stopped twin two", d=(99,), a=(18,), k=(60,), g=("Horror",)),
        })

    def entry(self, n, signal, weight, by=None, who="owner"):
        return {"key": f"tmdb:{n}", "id": n, "kind": "movie", "stype": "movie", "title": f"Synthetic {n}",
                "year": "2020", "signal": signal, "weight": weight, "age": 10, "item": {}, "who": who,
                "by": [by or self.ana], "flags": [], "genres": []}

    def test_stopped_titles_read_by_ties_and_taste(self):
        watched = [self.entry(n, "watched", 2.0) for n in (1, 2, 3)] + [self.entry(4, "loved", 3.0)]
        stopped = {n: self.entry(n, "abandoned", -1.5) for n in (10, 11, 12, 13, 14, 16, 17, 18, 20, 21)}
        pressed = self.entry(15, "abandoned", -1.5, who="remote")
        other = self.entry(19, "abandoned", -1.5, by=self.ben)
        self.s._facets["tmdb:19"] = rec("Other person", d=(7,), a=(19,), **{"k": (1, 2, 3), "g": ("Science Fiction",)})
        entries = watched + list(stopped.values()) + [pressed, other]
        self.s.read_abandoned(entries)
        read = {e["id"]: (e["signal"], e["weight"], e.get("abandon")) for e in entries}
        self.assertEqual(read[10], ("abandoned", 0.0, "related"))
        self.assertEqual(read[11], ("abandoned", 0.0, "related"))
        # A lead actor ties only through two watched titles or one loved title.
        self.assertEqual(read[12], ("abandoned", -.5, "far"))
        self.assertEqual(read[20], ("abandoned", 0.0, "related"))
        self.assertEqual(read[21], ("abandoned", 0.0, "related"))
        self.assertEqual(read[13], ("abandoned", 0.0, "unsure"))
        self.assertEqual(read[14], ("abandoned", -.5, "far"))
        self.assertEqual(read[16], ("abandoned", 0.0, "pending"))  # no facets yet
        # Ties come from watched and loved titles only, so two stopped films never vouch for each other.
        self.assertEqual(read[17], ("abandoned", -.5, "far"))
        self.assertEqual(read[18], ("abandoned", -.5, "far"))
        self.assertEqual(read[15], ("abandoned", -1.5, None))
        # Another member's watches do not explain this member's stop.
        self.assertEqual(read[19], ("abandoned", 0.0, "pending"))
        self.assertEqual([read[n] for n in (1, 2, 3)], [("watched", 2.0, None)] * 3)

    def test_stopped_titles_never_explain_a_pick(self):
        opened = {**self.entry(11, "opened", .2), "opinions": {self.ana: {"signal": "abandoned", "weight": -1.5,
                  "item": {}, "flags": [], "wsrc": "stremio"}}}
        entries = [self.entry(n, "watched", 2.0) for n in (1, 2, 3)] + [opened, self.entry(10, "abandoned", -1.5)]
        self.s.read_abandoned(entries)
        self.assertEqual(entries[-1]["abandon"], "related")
        self.assertEqual(opened["opinions"][self.ana]["abandon"], "related")
        profile = {"entries": entries, "genres": {}, "searches": [], "likes": "", "seen": set(), "negative": set()}
        with patch.object(self.s, "ensure_facets"), patch.object(self.s, "ensure_emb"):
            m = self.s.build_model(profile, enrich=False)
        for key in ("tmdb:10", "tmdb:11"):  # also when only the member's own opinion is the stop
            self.assertNotIn(key, m["ent"])
            self.assertFalse([fid for fid, hs in m["holders"].items() if any(k == key for _, k in hs)])

    def test_member_opinions_on_shared_titles_are_read_per_member(self):
        watched = [self.entry(n, "watched", 2.0, by=self.ben) for n in (1, 2, 3)]
        shared = {**self.entry(10, "watched", 2.0, by=self.ben), "by": [self.ana, self.ben],
                  "opinions": {self.ana: {"signal": "abandoned", "weight": -1.5, "item": {}, "flags": [], "wsrc": "trakt"},
                               self.ben: {"signal": "watched", "weight": 2.0, "item": {}, "flags": [], "wsrc": "trakt"}}}
        self.s.read_abandoned(watched + [shared])
        self.assertEqual(shared["opinions"][self.ana]["weight"], 0)
        self.assertEqual(shared["opinions"][self.ana]["abandon"], "pending")
        self.assertEqual(shared["opinions"][self.ben]["signal"], "watched")

    def test_before_digital_beats_ties_and_taste(self):
        watched = [self.entry(n, "watched", 2) for n in (1, 2, 3)]
        for n in (10, 13, 14):
            with self.subTest(n=n):
                self.s._facets[f"tmdb:{n}"]["dr"] = "2026-10-20"
                stop = self.entry(n, "abandoned", -1.5)
                stop["item"] = {"at": 1791763200000}  # 2026-10-12 UTC
                self.s.read_abandoned(watched + [stop])
                self.assertEqual((stop["signal"], stop["weight"], stop["abandon"]), ("abandoned", 0, "early"))
                self.assertTrue(self.s._stopped(stop))

    def test_missing_or_passed_release_keeps_existing_reading(self):
        for dr, at, expected in ((None, 1791763200000, "related"), ("2026-10-12", 1791763200000, "related"),
                                 ("2026-10-11", 1791763200000, "related"), ("2026-10-20", None, "related")):
            with self.subTest(dr=dr, at=at):
                self.s._facets["tmdb:10"]["dr"] = dr
                stop = self.entry(10, "abandoned", -1.5)
                stop["item"] = {"at": at}
                self.s.read_abandoned([self.entry(1, "watched", 2), stop])
                self.assertEqual(stop["abandon"], expected)

    def test_release_uses_local_watch_date_and_requires_positive_timestamp(self):
        for tz, release, at, expected in (
            ("Europe/Berlin", "2026-10-20", "2026-10-19T21:30:00+00:00", "early"),
            ("Europe/Berlin", "2026-10-20", "2026-10-19T22:30:00+00:00", "pending"),
            ("Europe/Berlin", "2026-01-20", "2026-01-19T23:30:00+00:00", "pending"),
            ("UTC", "2026-10-20", "2026-10-19T22:30:00+00:00", "early"),
            ("Europe/Berlin", "2026-10-20", 0, "pending"),
            ("Europe/Berlin", "2026-10-20", -1, "pending"),
        ):
            with self.subTest(tz=tz, at=at), patch.object(self.s, "TZ", tz):
                self.s._facets["tmdb:14"]["dr"] = release
                stop = self.entry(14, "abandoned", -1.5)
                stop["item"] = {"at": dt.datetime.fromisoformat(at).timestamp() * 1000 if isinstance(at, str) else at}
                self.s.read_abandoned([stop])
                self.assertEqual(stop["abandon"], expected)

    def test_compaction_keeps_broken_streams_and_respects_month_uncertainty(self):
        from skald.history import compact, restored
        now = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc).timestamp()
        for day, release, duration, raw_reading, monthly_reading in (
            (25, "2026-09-20", None, "pending", "pending"),
            (10, "2026-09-20", None, "early", "pending"),
            (25, "2026-10-15", None, "early", "early"),
            (25, "2026-09-20", 2, "early", "early"),
            (None, "2026-10-15", None, "pending", "pending"),
        ):
            with self.subTest(day=day, release=release, duration=duration), patch.object(self.s, "TZ", "Europe/Berlin"):
                self.s._facets["tmdb:14"].update(dr=release, runtime=110)
                raw = {"id": "tmdb:14", "type": "movie", "title": "Synthetic film", "year": 2026,
                       "signal": "abandoned", "weight": -1.5, "progress": .1,
                       "at": dt.datetime(2026, 9, day, 12, tzinfo=dt.timezone.utc).timestamp() * 1000 if day else None}
                if duration:
                    raw["duration_min"] = duration
                before = self.s.trakt_entry(raw, now)
                self.s.read_abandoned([before])
                self.assertEqual(before["abandon"], raw_reading)
                event = next(self.s.trakt_events(now, [raw]))
                summary = compact(event, "trakt.json", "household")
                after = self.s.trakt_entry(restored(summary), now)
                self.s.read_abandoned([after])
                self.assertEqual(after["abandon"], monthly_reading)
                if duration:
                    self.assertEqual(after["item"]["duration_min"], 2)
                    self.assertEqual(after["weight"], 0)
                    self.assertFalse(self.s._abandon_seen(after))

    def test_compacted_month_release_boundary_uses_local_dates(self):
        from skald.history import compact, restored
        at = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc).timestamp()
        raw = {"id": "tmdb:14", "type": "movie", "signal": "abandoned", "weight": -1.5, "at": at * 1000}
        summary = compact(next(self.s.trakt_events(at + 10 * 86400, [raw])), "trakt.json", "household")
        for tz, release, expected in (("UTC", "2026-10-01", "early"),
                                      ("Europe/Berlin", "2026-10-01", "pending"),
                                      ("Europe/Berlin", "2026-10-02", "early")):
            with self.subTest(tz=tz, release=release), patch.object(self.s, "TZ", tz):
                self.s._facets["tmdb:14"]["dr"] = release
                stop = self.s.trakt_entry(restored(summary), at + 10 * 86400)
                self.s.read_abandoned([stop])
                self.assertEqual(stop["abandon"], expected)

    def test_broken_stream_requires_known_short_duration_and_runtime(self):
        for duration, runtime, expected in ((2, 110, "early"), (80, 110, "pending"), (22, 110, "pending"),
                                             (None, 110, "pending"), (0, 110, "pending"), (2, None, "pending"), (2, 0, "pending")):
            with self.subTest(duration=duration, runtime=runtime):
                self.s._facets["tmdb:14"]["runtime"] = runtime
                stop = self.entry(14, "abandoned", -1.5)
                stop["item"] = {"duration_min": duration}
                self.s.read_abandoned([stop])
                self.assertEqual(stop["abandon"], expected)
                if expected == "early":
                    self.assertEqual((stop["signal"], stop["weight"]), ("abandoned", 0))
        stop["kind"] = "tv"
        self.s._facets["tmdb:14"].update(kind="tv", runtime=110, dr="2026-12-01")
        stop["item"] = {"duration_min": 2, "at": 1791763200000}
        self.s.read_abandoned([stop])
        self.assertEqual(stop["abandon"], "pending")

    def test_early_is_read_from_each_members_own_watch(self):
        eva, oskar = self.s.PEOPLE[:2]
        stop = self.entry(14, "abandoned", -1.5)
        stop.update(by=[eva, oskar], opinions={
            eva: {"signal": "abandoned", "weight": -1.5, "item": {"at": 1791763200000}, "wsrc": "stremio"},
            oskar: {"signal": "abandoned", "weight": -1.5, "item": {"at": 1792627200000}, "wsrc": "trakt"}})
        self.s._facets[stop["key"]]["dr"] = "2026-10-20"
        self.s.read_abandoned([stop])
        self.assertEqual(stop["opinions"][eva]["abandon"], "early")
        self.assertEqual(stop["opinions"][eva]["weight"], 0)
        self.assertEqual(stop["opinions"][oskar]["abandon"], "pending")
        self.assertEqual(stop["opinions"][oskar]["weight"], 0)

    def test_explicit_stops_are_untouched_by_early_rule(self):
        self.s._facets["tmdb:14"].update(dr="2026-10-20", runtime=110)
        for who, wsrc in (("remote", None), ("told", None), ("owner", "interview")):
            with self.subTest(who=who, wsrc=wsrc):
                stop = self.entry(14, "abandoned", -1.5, who=who)
                stop.update(item={"at": 1791763200000, "duration_min": 2}, wsrc=wsrc)
                opinion = {"signal": "abandoned", "weight": -1.5, "item": stop["item"], "wsrc": "interview"}
                stop["opinions"] = {self.s.PEOPLE[0]: opinion}
                before = copy.deepcopy(stop)
                self.s.read_abandoned([stop])
                self.assertEqual(stop, before)

    def test_facet_digital_release_uses_only_local_and_us_types_four_five(self):
        def country(code, dates):
            return {"iso_3166_1": code, "release_dates": [{"type": typ, "release_date": date} for typ, date in dates]}
        releases = [country("DE", [(3, "2026-01-01"), (6, "2026-02-01"), (4, "2026-11-01T00:00:00Z"),
                                    (5, "invalid"), (4, "0000-00-00")]),
                    country("US", [(5, "2026-10-20T00:00:00Z"), (4, "2026-10-25")]),
                    country("FR", [(4, "2026-03-01")])]
        with patch.object(self.s, "COUNTRY", "Germany"):
            self.assertEqual(self.s.facet_record("movie", 1, {"release_dates": {"results": releases}})["dr"], "2026-10-20")
            self.assertEqual(self.s.facet_record("movie", 1, {"release_dates": {"results": releases[:1]}})["dr"], "2026-11-01")
            self.assertNotIn("dr", self.s.facet_record("movie", 1, {"release_dates": {"results": releases[2:]}}))
            self.assertNotIn("dr", self.s.facet_record("movie", 1, {}))
            self.assertNotIn("dr", self.s.facet_record("tv", 1, {"release_dates": {"results": releases}}))

    def test_worker_backfills_only_stopped_films_bounded_and_skips_failures(self):
        entries = [self.entry(n, "abandoned", -1.5) for n in range(1000, 1205)]
        for e in entries:
            e["abandon"] = "far"
            self.s._facets[e["key"]] = rec(e["title"])
        self.s._facets["tmdb:1001"]["runtime"] = 95
        self.s._profiles["household"] = {"entries": entries + [self.entry(1, "watched", 2)]}
        self.s._facets_dirty.clear()
        det = {"runtime": 110, "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [
            {"type": 4, "release_date": "2026-10-20"}]}]}}
        with patch.object(self.s, "tmdb", side_effect=[OSError("offline failure")] + [det] * 199) as fetch, \
                patch.object(self.s, "clear_seed_cache") as clear:
            self.s.backfill_abandoned()
        self.assertEqual(fetch.call_count, 200)
        self.assertNotIn("dr", self.s._facets["tmdb:1000"])
        self.assertEqual(self.s._facets["tmdb:1001"]["runtime"], 95)
        self.assertEqual(self.s._facets["tmdb:1002"]["runtime"], 110)
        self.assertEqual(self.s._facets["tmdb:1002"]["dr"], "2026-10-20")
        self.assertNotIn("dr", self.s._facets["tmdb:1200"])
        self.assertNotIn("dr", self.s._facets["tmdb:1"])
        self.assertTrue(self.s._facets_dirty.is_set())
        clear.assert_called_once()

    def test_backfill_retries_do_not_starve_later_stopped_films(self):
        det = {"runtime": 110, "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [
            {"type": 4, "release_date": "2026-10-20"}]}]}}
        for start, failed in ((2000, False), (3000, True)):
            with self.subTest(failed=failed):
                entries = [self.entry(n, "abandoned", -1.5) for n in range(start, start + 205)]
                for e in entries:
                    e["abandon"] = "far"
                    self.s._facets[e["key"]] = rec(e["title"])
                self.s._profiles["household"] = {"entries": entries}
                def fetch(path, **kw):
                    if int(path.split("/")[1]) < start + 200:
                        if failed:
                            raise OSError("synthetic unavailable metadata")
                        return {}
                    return det
                with patch.object(self.s, "tmdb", side_effect=fetch) as tmdb, \
                        patch.object(self.s.time, "time", return_value=1000) as now:
                    self.s.backfill_abandoned()
                    self.assertEqual(tmdb.call_count, 200)
                    self.s.backfill_abandoned()
                    self.assertEqual(tmdb.call_count, 205)
                    self.assertEqual(self.s._facets[f"tmdb:{start + 204}"]["dr"], "2026-10-20")
                    self.s.backfill_abandoned()
                    self.assertEqual(tmdb.call_count, 205)
                    now.return_value = 1000 + 86401
                    self.s.backfill_abandoned()
                    self.assertEqual(tmdb.call_count, 405)

    def test_backfill_unchanged_facts_do_not_dirty_or_invalidate_home(self):
        for n, response in ((14, {"runtime": 110}), (17, {})):
            with self.subTest(response=response):
                stop = self.entry(n, "abandoned", -1.5)
                stop["abandon"] = "far"
                self.s._profiles["household"] = {"entries": [stop]}
                if response:
                    self.s._facets[stop["key"]]["runtime"] = 110
                before = copy.deepcopy(self.s._facets[stop["key"]])
                self.s._facets_dirty.clear()
                with patch.object(self.s, "tmdb", return_value=response) as fetch, \
                        patch.object(self.s, "clear_seed_cache") as clear, \
                        patch.object(self.s.time, "time", return_value=1000) as now:
                    self.s.backfill_abandoned()
                    self.assertEqual(self.s._facets[stop["key"]], before)
                    self.assertFalse(self.s._facets_dirty.is_set())
                    clear.assert_not_called()
                    now.return_value = 1000 + 86401
                    self.s.backfill_abandoned()
                    self.assertEqual(fetch.call_count, 2)
                    self.assertEqual(self.s._facets[stop["key"]], before)
                    self.assertFalse(self.s._facets_dirty.is_set())
                    clear.assert_not_called()

    def test_early_stop_returns_to_rows_after_release_or_broken_stream(self):
        eva = self.s.PEOPLE[0]
        self.s._facets["tmdb:14"].update(dr="2026-10-20", runtime=110)
        meta = {"id": "tmdb:14", "type": "movie", "name": "Synthetic return"}
        local_now = self.s.local_now
        for day, duration, excluded in ((19, None, True), (20, None, False), (21, None, False), (19, 2, False)):
            with self.subTest(day=day, duration=duration):
                stop = self.entry(14, "abandoned", -1.5)
                stop["item"] = {"at": 1791763200000, "duration_min": duration}
                with patch.object(self.s, "fold_events", return_value=([stop], {}, set())), \
                        patch.object(self.s, "event_stages", return_value=[]), \
                        patch.object(self.s, "local_now", side_effect=lambda ts=None:
                                     local_now(ts) if ts is not None else dt.datetime(2026, 10, day)), \
                        patch.object(self.s, "preference_ok", return_value=True):
                    profile = self.s.build_profile([], [], remote=False)
                    self.s._profiles["household"] = profile
                    self.assertEqual("tmdb:14" in profile["seen"], excluded)
                    self.assertNotIn("tmdb:14", profile["negative"])
                    self.assertEqual(self.s.row_eligible(meta, self.s.rid("digital"), self.s.LANG, profile), not excluded)
                    with patch.object(self.s._scoping, "scope", eva, create=True):
                        self.assertEqual("tmdb:14" in self.s.search_profile()["seen"], excluded)

    def test_merges_preserve_cached_early_stop_reason_for_unaffected_member(self):
        from skald.members import fresh_config
        eva, oskar = "eva", "oskar"
        now = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc).timestamp()
        self.s._facets["tmdb:14"].update(dr="2026-10-20", runtime=110)
        meta = {"id": "tmdb:14", "type": "movie", "name": "Synthetic return"}
        with patch.object(self.s, "CONFIG", fresh_config([eva, oskar])), \
                patch.object(self.s, "PEOPLE", (eva, oskar)), \
                patch.object(self.s, "SCOPES", {eva: eva, oskar: oskar}), \
                patch.object(self.s, "local_now", return_value=dt.datetime(2026, 10, 19)), \
                patch.object(self.s, "preference_ok", return_value=True):
            for merge in ("import", "feedback"):
                for reason in ("stream", "release"):
                    with self.subTest(merge=merge, reason=reason):
                        stop = {**self.entry(14, "abandoned", 0, by="together", who="tv"),
                                "abandon": "early", "abandon_reason": reason,
                                "item": {"progress": .3, "at": 1791763200000}}
                        entries, by_key = [stop], {stop["key"]: stop}
                        event = {"id": "a" * 32, "key": stop["key"], "kind": "movie",
                                 "title": stop["title"], "year": "2020", "who": oskar, "action": "love"}
                        if merge == "import":
                            self.s.merge_imports(entries, by_key, now,
                                [{**event, "t": "2026-10-19", "src": "imdb", "conf": 1., "n": 1}])
                        else:
                            self.s.merge_feedback(entries, by_key, now,
                                {(stop["key"], oskar): {**event, "t": now, "reasons": []}})
                        self.s.read_abandoned(entries)
                        self.assertEqual(stop["shared"].get("abandon"), "early")
                        self.assertEqual(stop["shared"].get("abandon_reason"), reason)
                        self.s._profiles["household"] = {"entries": entries}
                        with patch.object(self.s._scoping, "scope", eva, create=True):
                            profile = self.s.search_profile()
                        own = profile["entries"][0]
                        self.assertEqual((own["abandon"], own.get("abandon_reason")), ("early", reason))
                        self.assertEqual(self.s._abandon_seen(own), reason == "release")
                        self.assertEqual(self.s.row_eligible(meta, self.s.rid("digital"), self.s.LANG, profile),
                                         reason == "stream")

    def test_catch_up_with_children_can_offer_an_early_sequel_again(self):
        eva, oskar = self.s.PEOPLE[:2]
        parts = [{"id": 1, "title": "Synthetic first film", "release_date": "2020-01-01"},
                 {"id": 11, "title": "Synthetic sequel", "release_date": "2026-09-01"}]
        responses = {"movie/1": {"belongs_to_collection": {"id": 700}},
                     "collection/700": {"name": "Synthetic Collection", "parts": parts},
                     "genre/movie/list": {"genres": []}}
        now = dt.datetime(2026, 10, 21, 12, tzinfo=dt.timezone.utc).timestamp()
        with patch.dict(self.s.CONFIG["taste"]["people"], {oskar: {"prefs": {"kids_age": 12}}}), \
                patch.object(self.s.time, "time", return_value=now), \
                patch.object(self.s, "tmdb", side_effect=lambda path, **kw: copy.deepcopy(responses[path])):
            self.assertEqual(self.s.adult_people(), (eva,))
            for release, duration, expected in (("2026-10-20", None, ["tmdb:11"]),
                                                ("2026-10-30", 2, ["tmdb:11"]),
                                                ("2026-10-30", None, [])):
                with self.subTest(release=release, duration=duration):
                    self.s._facets["tmdb:11"].update(dr=release, runtime=110)
                    stop = self.entry(11, "abandoned", -1.5, by=eva)
                    stop["item"] = {"at": 1791763200000, "duration_min": duration}
                    entries = [self.entry(1, "watched", 2, by=eva), stop]
                    self.s.read_abandoned(entries)
                    self.s._profiles["household"] = {"entries": entries, "seen": {"tmdb:1"}, "negative": set()}
                    self.assertEqual([m["id"] for m in self.s.catchup_feed(self.s.LANG)["tv"]], expected)

    def test_early_explanation_is_localized_and_per_member(self):
        from skald import commands, members
        from skald.__main__ import parser, text_output
        eva, oskar = self.s.PEOPLE[:2]
        self.s._facets["tmdb:14"].update(dr="2026-10-20", runtime=110)
        for duration, reason in ((None, "release"), (2, "stream")):
            stop = self.entry(14, "abandoned", -1.5)
            stop["item"] = {"at": 1791763200000, "duration_min": duration}
            self.s.read_abandoned([stop])
            profile = {"entries": [stop], "genres": {}, "searches": [], "seen": set(), "negative": set()}
            self.s._profiles["household"] = profile
            with patch.object(self.s, "ensure_facets"), patch.object(self.s, "ensure_emb"):
                self.s._model["m"] = self.s.build_model(profile, enrich=False)
            info = self.s.explain(stop["key"], "movie", person=eva)
            self.assertEqual(info["per_person"][eva]["abandon"], "early")
            self.assertNotIn("abandon", info["per_person"][oskar])
            other = self.s.explain(stop["key"], "movie", person=oskar)
            self.assertNotIn("abandon", other["per_person"][eva])
            for lang, expected in (("en", "Stream was broken" if duration else "Stopped before it was out digitally"),
                                   ("de", "Stream war kaputt" if duration else "Abgebrochen, bevor er digital erschien"),
                                   ("sr-latn", "Strim je bio pokvaren" if duration else "Prekinuto pre digitalnog izdanja"),
                                   ("sr-cyrl", "Стрим је био покварен" if duration else "Прекинуто пре дигиталног издања")):
                with self.subTest(reason=reason, lang=lang), patch.object(self.s, "LANG", lang):
                    text = commands.explain_text(self.s, {"key": stop["key"], "kind": "movie"}, eva, info)
                    self.assertIn(expected, text)
                    out = io.StringIO()
                    with patch.dict(members.CONFIG["region"], {"row_lang": lang}):
                        text_output(parser().parse_args(["explain", stop["key"], "--person", eva]),
                                    {**info, "calibration": "too_new"}, out)
                    self.assertIn(expected, out.getvalue())

    def test_early_stops_do_not_teach_the_ai_taste_prompt(self):
        stop = self.entry(14, "abandoned", -1.5)
        stop["item"] = {"duration_min": 2}
        self.s._facets[stop["key"]]["runtime"] = 110
        self.s.read_abandoned([stop])
        profile = {"entries": [stop], "genres": {}, "searches": [], "likes": ""}
        self.assertNotIn(stop["title"], self.s.profile_text(profile))

    def test_shared_tv_prompt_uses_members_own_early_reading(self):
        eva, oskar = self.s.PEOPLE[:2]
        (Path(self.s.DATA) / "taste.json").write_text(json.dumps({"people": {eva: {}, oskar: {}}}))
        self.s._facets["tmdb:14"]["runtime"] = 110
        for shared_signal, own_signal, should_love in (("abandoned", None, False),
                                                       ("watched", "abandoned", False),
                                                       ("abandoned", "loved", True)):
            with self.subTest(shared=shared_signal, own=own_signal), \
                    patch.object(self.s, "TRAKT_PERSON", eva), patch.dict(self.s.LABELS, {eva: "Eva", oskar: "Oskar"}):
                stop = self.entry(14, shared_signal, -1.5 if shared_signal == "abandoned" else 2, by=oskar, who="tv")
                stop["item"] = {"duration_min": 2}
                if own_signal:
                    stop["opinions"] = {oskar: {"signal": own_signal, "weight": -1.5 if own_signal == "abandoned" else 3,
                                               "item": {"duration_min": 2}, "wsrc": "trakt"}}
                self.s.read_abandoned([stop])
                text = self.s.profile_text({"entries": [stop], "genres": {}, "searches": [], "likes": ""})
                self.assertEqual("Oskar loves: " + stop["title"] in text, should_love)

    def test_member_watch_does_not_inherit_another_members_early_reading(self):
        eva, oskar = self.s.PEOPLE[:2]
        stop = self.entry(14, "abandoned", -1.5)
        stop.update(by=[eva, oskar], item={"duration_min": 2}, opinions={
            oskar: {"signal": "watched", "weight": 2, "item": {}, "wsrc": "trakt"}})
        self.s._facets[stop["key"]]["runtime"] = 110
        self.s.read_abandoned([stop])
        profile = {"entries": [stop]}
        self.assertEqual(self.s.person_entries(profile, eva)[0]["abandon"], "early")
        other = self.s.person_entries(profile, oskar)[0]
        self.assertNotIn("abandon", other)
        self.assertTrue(self.s._abandon_seen(other))
        self.assertTrue(self.s._abandon_seen(stop))

    def test_member_early_opinion_does_not_keep_a_stale_negative_exclusion(self):
        eva = self.s.PEOPLE[0]
        stop = self.entry(14, "abandoned", -1.5)
        stop["opinions"] = {eva: {"signal": "abandoned", "weight": -1.5,
                                  "item": {"duration_min": 2}, "wsrc": "stremio"}}
        self.s._facets[stop["key"]]["runtime"] = 110
        with patch.object(self.s, "fold_events", return_value=([stop], {}, set())), \
                patch.object(self.s, "event_stages", return_value=[]):
            profile = self.s.build_profile([], [], remote=False)
        self.assertNotIn(stop["key"], profile["seen"] | profile["negative"])

    def test_stop_metadata_and_release_day_invalidate_profile_hash(self):
        stop = self.entry(14, "abandoned", -1.5)
        stop["item"] = {"at": 1791763200000}
        self.s.read_abandoned([stop])
        self.s._profiles["household"] = {"entries": [stop]}
        before = self.s.taste_hash("household", [])
        self.s._facets[stop["key"]]["dr"] = "2026-10-20"
        with patch.object(self.s, "local_now", return_value=dt.datetime(2026, 10, 19)):
            self.assertNotEqual(before, self.s.taste_hash("household", []))
            self.s.read_abandoned([stop])
            before_release = self.s.taste_hash("household", [])
        with patch.object(self.s, "local_now", return_value=dt.datetime(2026, 10, 20)):
            self.assertNotEqual(before_release, self.s.taste_hash("household", []))
        stop["by"] = list(self.s.PEOPLE[:2])
        stop["opinions"] = {self.s.PEOPLE[1]: {"signal": "abandoned", "weight": -.5, "abandon": "far"}}
        with patch.object(self.s, "local_now", return_value=dt.datetime(2026, 10, 19)):
            before_release = self.s.taste_hash("household", [])
        with patch.object(self.s, "local_now", return_value=dt.datetime(2026, 10, 20)):
            self.assertNotEqual(before_release, self.s.taste_hash("household", []))

    def test_offline_engine_keeps_released_early_stops_unseen(self):
        from skald.engine import Engine
        stop = self.entry(14, "abandoned", -1.5)
        stop["item"] = {"duration_min": 2}
        self.s._facets[stop["key"]]["runtime"] = 110
        self.s.read_abandoned([stop])
        engine = object.__new__(Engine)
        engine._server = self.s
        profile = engine._profile_with({"entries": [stop], "seen": {stop["key"], "tmdb:99"}})
        self.assertEqual(profile["seen"], {"tmdb:99"})

    def test_offline_engine_preserves_inclusion_with_base_142f6ed(self):
        from eval.route_parity import BaselineUnavailable, baseline_source
        from skald.engine import Engine
        try:
            source = self.enterContext(baseline_source(base_ref="142f6ed"))
        except BaselineUnavailable as error:
            self.skipTest(str(error))
        entries = [self.entry(1, "watched", 2), self.entry(14, "abandoned", -.5),
                   {**self.entry(11, "abandoned", 0), "abandon": "early", "abandon_reason": "stream"}]
        (Path(self.s.DATA) / "profile.json").write_text(json.dumps({"entries": entries, "seen": ["tmdb:99"]}))
        with Engine(self.s.DATA, server_path=source) as engine:
            self.assertFalse(hasattr(engine._server, "_abandon_seen"))
            self.assertEqual(engine._profile["seen"], {"tmdb:1", "tmdb:11", "tmdb:14", "tmdb:99"})
            self.assertEqual(engine._profile["negative"], {"tmdb:14"})

    def test_early_explanation_without_a_model_does_not_write_taste(self):
        eva = self.s.PEOPLE[0]
        stop = self.entry(14, "abandoned", -1.5)
        stop["item"] = {"duration_min": 2}
        self.s._facets[stop["key"]]["runtime"] = 110
        self.s.read_abandoned([stop])
        self.s._profiles["household"] = {"entries": [stop]}
        (Path(self.s.DATA) / "taste.json").unlink(missing_ok=True)
        before = sorted(Path(self.s.DATA).iterdir())
        self.assertEqual(self.s.explain(stop["key"], "movie", person=eva)["per_person"][eva]["abandon"], "early")
        self.assertEqual(sorted(Path(self.s.DATA).iterdir()), before)


if __name__ == "__main__":
    unittest.main()
