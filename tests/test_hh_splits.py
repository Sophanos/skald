"""Synthetic household timelines only."""
from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo


WEEK = timedelta(days=7)
ORIGIN = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)


def event(member, title, at, available_at=None, **extra):
    return dict(member=member, title=title, at=at,
                available_at=available_at or at, **extra)


class TemporalFoldsTests(unittest.TestCase):
    def test_normalized_events_preserve_attribution_and_readiness(self):
        from eval.household.events import normalize_events

        at = ORIGIN + timedelta(days=1)
        as_of = ORIGIN + 3 * WEEK
        record = {'media_kind': 'movie', 'provider_id': 'tmdb:1',
                  'member': 'a', 'action': 'played', 'source': 'synthetic',
                  'reliable': True, 'timestamp_precision': 'second',
                  'occurred_at': at.timestamp(), 'observed_at': at.timestamp()}
        delayed = {**record, 'member': 'b', 'media_kind': 'tv',
                   'observed_at': (as_of + timedelta(seconds=1)).timestamp()}
        pending = {**record, 'provider_id': 'tmdb:2',
                   'mature_at': (as_of + WEEK).timestamp()}
        normalized = normalize_events([record, delayed, pending])['events']
        first, second = self.folds(normalized)[1:]
        self.assertEqual(len(first.labels), 1)
        label = first.labels[0]
        self.assertEqual((label['member'], label['title'], label['at'], label['available_at']),
                         ('a', ('movie', 'tmdb:1'), at, at))
        self.assertEqual({e['title'] for e in first.censored},
                         {('tv', 'tmdb:1'), ('movie', 'tmdb:2')})
        self.assertEqual([e['member'] for e in second.train], ['a'])
        self.assertNotIn('at', normalized[0])
        self.assertNotIn('identity', record)
        choice = self.s.chronological_last_unique(normalized, 'a', as_of)
        self.assertEqual(choice.labels[0]['title'], ('movie', 'tmdb:1'))
        for changes in ({'member': None}, {'observed_at': None},
                        {'timestamp_precision': 'day'}, {'occurred_at': None}):
            with self.subTest(fields=tuple(changes)), self.assertRaises(ValueError):
                self.folds(normalize_events([{**record, **changes}])['events'])

    def setUp(self):
        from eval.household import splits
        self.s = splits

    def folds(self, events=(), **kwargs):
        return self.s.weekly_folds(events, ORIGIN + 3 * WEEK, timezone="UTC",
                                   outer_weeks=2, development_weeks=1, **kwargs)

    def test_other_member_future_history_is_excluded(self):
        past = event("a", "past", ORIGIN - 2 * WEEK)
        future = event("b", "future", ORIGIN + timedelta(days=1))
        late = event("b", "late", ORIGIN - WEEK, ORIGIN + timedelta(days=2))
        boundary = event("a", "boundary", ORIGIN)
        folds = self.folds([future, late, boundary, past])
        first, second = [f for f in folds if f.phase == "outer"]
        self.assertEqual(first.origin, ORIGIN)
        self.assertEqual(first.train, (past,))
        self.assertEqual({e["title"] for e in first.labels}, {"future", "boundary"})
        self.assertEqual({e["title"] for e in second.train},
                         {"past", "late", "future", "boundary"})
        self.assertEqual(first.end, second.origin)

    def test_outer_labels_never_select_parameters(self):
        dev = event("a", "development", ORIGIN - WEEK - timedelta(days=1))
        outer = event("a", "outer", ORIGIN + timedelta(days=1))
        calls = []
        candidates = [{"choice": "development"}, {"choice": "outer"}]

        def score(parameters, fold):
            calls.append(fold.phase)
            return sum(e["title"] == parameters["choice"] for e in fold.labels)

        chosen = self.s.select_parameters(self.folds([dev, outer]), candidates, score)
        changed = self.folds([dev, event("b", "changed", outer["at"])])
        self.assertEqual(chosen, self.s.select_parameters(changed, candidates, score))
        self.assertEqual(chosen, {"choice": "development"})
        self.assertEqual(calls, ["development"] * 4)
        candidates[0]["choice"] = "mutated"
        self.assertEqual(chosen, {"choice": "development"})

    def test_unmatured_outcomes_are_censored(self):
        as_of = ORIGIN + 3 * WEEK
        mature = event("a", "mature", ORIGIN + timedelta(days=1))
        pending = event("a", "completion", mature["at"], mature_at=as_of + WEEK)
        delayed = event("b", "delayed", mature["at"], as_of + timedelta(seconds=1))
        fold = self.folds([mature, pending, delayed])[1]
        self.assertEqual(fold.labels, (mature,))
        self.assertEqual({e["title"] for e in fold.censored}, {"completion", "delayed"})
        longer = self.folds(outcome_grace=3 * WEEK)
        self.assertEqual(longer[-1].end + 3 * WEEK, as_of)
        self.assertLessEqual(longer[-1].end, fold.origin)

    def test_latest_twelve_mature_calendar_weeks_follow_eight_development_weeks(self):
        as_of = ORIGIN + timedelta(days=3)
        folds = self.s.weekly_folds([], as_of, timezone="UTC")
        self.assertEqual([f.phase for f in folds], ["development"] * 8 + ["outer"] * 12)
        self.assertEqual(folds[-1].end, ORIGIN - WEEK)
        self.assertEqual(folds[-1].end + WEEK, ORIGIN)
        self.assertEqual(folds[7].end + WEEK, folds[8].origin)
        for group in (folds[:8], folds[8:]):
            self.assertEqual([f.origin for f in group],
                             [group[0].origin + i * WEEK for i in range(len(group))])
        before = self.s.weekly_folds([], ORIGIN - timedelta(microseconds=1), timezone="UTC")
        self.assertEqual(before[-1].end, folds[-1].end - WEEK)

    def test_household_boundary_stays_at_five_across_dst(self):
        local = ZoneInfo("Europe/Berlin")
        as_of = datetime(2026, 4, 13, 5, tzinfo=local)
        folds = self.s.weekly_folds([], as_of, timezone="Europe/Berlin",
                                   outer_weeks=2, development_weeks=1)
        self.assertTrue(all(f.origin.weekday() == 0 and f.origin.hour == 5 for f in folds))
        self.assertEqual(folds[0].origin, datetime(2026, 3, 9, 5, tzinfo=local))
        self.assertEqual(folds[-1].origin.utcoffset(), timedelta(hours=2))
        self.assertEqual(folds[0].origin.utcoffset(), timedelta(hours=1))

    def test_development_labels_are_frozen_before_outer_origin(self):
        at = ORIGIN - 2 * WEEK + timedelta(days=1)
        ready = event("a", "ready", at)
        late = event("a", "late", at, ORIGIN + timedelta(days=1))
        pending = event("a", "pending", at, mature_at=ORIGIN + WEEK)
        folds = self.folds([ready, late, pending])
        self.assertEqual(folds[0].labels, (ready,))
        self.assertEqual({e["title"] for e in folds[0].censored}, {"late", "pending"})
        self.assertEqual(folds[0].end + WEEK, folds[1].origin)

    def test_grace_is_elapsed_time_across_dst(self):
        local = ZoneInfo("Europe/Berlin")
        early = datetime(2026, 3, 30, 5, tzinfo=local)
        folds = self.s.weekly_folds([], early, timezone="Europe/Berlin")
        self.assertEqual(folds[-1].end, datetime(2026, 3, 16, 5, tzinfo=local))
        later = self.s.weekly_folds([], early + timedelta(hours=1), timezone="Europe/Berlin")
        self.assertEqual(later[-1].end, datetime(2026, 3, 23, 5, tzinfo=local))

    def test_folds_are_deterministic_and_do_not_share_event_dicts(self):
        rows = [event("b", "b", ORIGIN - 3 * WEEK),
                event("a", "a", ORIGIN - 3 * WEEK)]
        folds = self.folds(rows)
        self.assertEqual(folds, self.folds(reversed(rows)))
        folds[0].train[0]["title"] = "changed"
        self.assertEqual(rows[1]["title"], "a")
        self.assertEqual(folds[1].train[0]["title"], "a")

    def test_next_choice_uses_last_new_title_and_household_cutoff(self):
        history = [event("a", "old", ORIGIN - 3 * WEEK),
                   event("b", "other", ORIGIN - 2 * WEEK),
                   event("a", "new", ORIGIN - WEEK),
                   event("a", "old", ORIGIN), event("b", "future", ORIGIN)]
        fold = self.s.chronological_last_unique(history, "a", ORIGIN + WEEK)
        self.assertEqual(fold.phase, "next_choice")
        self.assertEqual(fold.labels, (history[2],))
        self.assertEqual(fold.train, tuple(history[:2]))
        self.assertIsNone(self.s.chronological_last_unique([], "a", ORIGIN))

    def test_invalid_dates_and_short_grace_are_rejected(self):
        with self.assertRaises(ValueError):
            self.folds(outcome_grace=timedelta(days=6))
        with self.assertRaises(ValueError):
            self.folds([event("a", "bad", ORIGIN.replace(tzinfo=None))])
        with self.assertRaises(ValueError):
            self.s.select_parameters([], [{"choice": "a"}], lambda p, f: 0)


if __name__ == "__main__":
    unittest.main()
