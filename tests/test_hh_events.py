"""Synthetic contracts for independent household evaluation targets."""
import unittest

from eval.household.events import normalize_events


def event(action="played", **changes):
    return {"media_kind": "movie", "provider_id": "tmdb:1",
            "action": action, "source": "player", "reliable": True,
            "occurred_at": 100, "observed_at": 110,
            "timestamp_precision": "second", "attribution_confidence": .8,
            **changes}


class HouseholdEventsTests(unittest.TestCase):
    def test_explicit_rewatch_is_not_discovery(self):
        result = normalize_events([event(rewatch=True)])
        self.assertEqual(result['discovery'], [])
        self.assertEqual(len(result['rewatches']), 1)
        coarse = event(rewatch=True, occurred_at='2026', timestamp_precision='year')
        self.assertEqual(normalize_events([coarse])['discovery'], [])
        first = event(occurred_at=50)
        self.assertEqual(normalize_events([first, event(rewatch=True)])['discovery'],
                         [('movie', 'tmdb:1')])

    def test_rating_without_play_is_not_playback(self):
        for action in ("rating", "open", "search", "favourite", "like", "love", "veto"):
            with self.subTest(action=action):
                result = normalize_events([event(action, rating=9)])
                self.assertEqual(result["discovery"], [])
                self.assertFalse(result["events"][0]["playback"])
        self.assertEqual(normalize_events([event("rating", rating=7)])["approval"],
                         [("movie", "tmdb:1")])
        for record in (event("rating", rating=4), event("veto")):
            self.assertEqual(normalize_events([record])["negative"], [("movie", "tmdb:1")])

    def test_cross_connector_duplicate_counts_once(self):
        records = [event(session_id="first"), event(source="trakt", session_id="first")]
        result = normalize_events(records)
        self.assertEqual(result["discovery"], [("movie", "tmdb:1")])
        self.assertEqual(result["rewatches"], [])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(set(result["coverage"]), {"player", "trakt"})
        self.assertEqual(result["coverage"]["trakt"]["playback"], 1)

    def test_movie_and_tv_ids_do_not_collide(self):
        result = normalize_events([event(), event("played_episode", media_kind="tv", episode=(1, 1))])
        self.assertEqual(result["discovery"], [("movie", "tmdb:1"), ("tv", "tmdb:1")])

    def test_thresholds_and_reliability_are_protocol_choices(self):
        for progress, expected in ((.249, False), (.25, True), (.899, True), (.90, True)):
            result = normalize_events([event("progress", progress=progress)])
            self.assertEqual(result["events"][0]["playback"], expected)
            self.assertEqual(bool(result["completion"]), progress >= .90)
        for record in (event(reliable=False), event("progress", progress=.95, reliable=False)):
            self.assertEqual(normalize_events([record])["discovery"], [])
        self.assertEqual(normalize_events([])["protocol"]["playback_progress"], .25)
        self.assertEqual(normalize_events([])["protocol"]["completion_progress"], .90)

    def test_episodes_do_not_complete_series(self):
        records = [event("played_episode", media_kind="tv", episode=episode)
                   for episode in ((1, 1), (1, 1), (1, 2))]
        result = normalize_events(records)
        self.assertEqual(result["discovery"], [("tv", "tmdb:1")])
        self.assertEqual(result["completion"], [])
        self.assertEqual(result["rewatches"], [])
        completed = normalize_events([event("completed", media_kind="tv", episode=(1, 1)),
                                      event("completed", media_kind="tv")])
        self.assertEqual(completed["completion"], [("tv", "tmdb:1", (1, 1))])

    def test_rewatches_and_coarse_dates_preserve_evidence(self):
        first = event()
        second = event(occurred_at=200, observed_at=220)
        coarse = event(occurred_at="2026-10-01", observed_at=300, timestamp_precision="day")
        result = normalize_events([second, first, dict(second, source="trakt"), coarse])
        self.assertEqual(len(result["rewatches"]), 1)
        self.assertEqual(result["rewatches"][0]["occurred_at"], 200)
        self.assertEqual(result["rewatches"][0]["sources"], ["player", "trakt"])
        self.assertEqual(result["events"][3]["occurred_at"], "2026-10-01")
        self.assertEqual(result["events"][3]["timestamp_precision"], "day")
        self.assertEqual(result["events"][3]["attribution_confidence"], .8)
        self.assertEqual(result["events"][3]["observed_at"], 300)
        self.assertEqual(normalize_events([coarse, dict(coarse, observed_at=400)])["rewatches"], [])
        self.assertEqual(normalize_events([first], known_titles=[("movie", "tmdb:1")])["discovery"], [])

    def test_coverage_and_empty_input(self):
        result = normalize_events([event(), event("rating", rating=8), event(reliable=False)])
        self.assertEqual(result["coverage"]["player"],
                         {"observations": 3, "reliable": 2, "playback": 1,
                          "completion": 0, "approval": 1, "negative": 0, "precise_playback": 1})
        self.assertEqual(normalize_events([])["coverage"], {})

    def test_bad_input_is_rejected_without_mutating_records(self):
        record = event()
        normalize_events([record])
        self.assertNotIn("identity", record)
        for changes in ({"media_kind": "unknown"}, {"provider_id": ""}, {"progress": 25},
                        {"attribution_confidence": float("nan")}, {"episode": (1, 0)},
                        {"timestamp_precision": "second", "occurred_at": "2026-10-01"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_events([event(**changes)])

    def test_session_evidence_merges_connector_clock_differences(self):
        records = [event(), event(source="trakt", occurred_at=101, session_id="first"),
                   event(session_id="first"), event(occurred_at=200, session_id="second")]
        result = normalize_events(records)
        self.assertEqual(len(result["rewatches"]), 1)
        self.assertEqual(result["rewatches"][0]["occurred_at"], 200)

    def test_explicit_rewatch_requires_a_precise_movie_or_episode(self):
        self.assertEqual(len(normalize_events([event(rewatch=True)])["rewatches"]), 1)
        for record in (event(rewatch=True, occurred_at="2026", timestamp_precision="year"),
                       event("played_episode", media_kind="tv", rewatch=True),
                       event(rewatch=True, reliable=False)):
            self.assertEqual(normalize_events([record])["rewatches"], [])
