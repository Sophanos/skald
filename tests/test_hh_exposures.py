"""Synthetic contracts for served-list association, never causal estimates."""
import copy
import importlib
import unittest


DAY = 86400
ITEM = ("movie", "tmdb:1")
OTHER = ("movie", "tmdb:2")


def online(at=100, fetches=1):
    return {"v": 1, "keys": ["tmdb:1@movie", "tmdb:2@movie"],
            "lists": {"a": "0 1"},
            "exp": {"2026-10-01": {"personal": {"a": [fetches, 0, at]}}}}


def play(at=200, **changes):
    return {"media_kind": "movie", "provider_id": "tmdb:1", "action": "played",
            "source": "synthetic", "reliable": True, "occurred_at": at,
            "timestamp_precision": "second", **changes}


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module("eval.household.exposures")

    def test_fetch_not_counted_as_view(self):
        data = online(fetches=50)
        data["exp"]["2026-10-01"]["personal"]["a"].extend([1, 3700])
        report = self.api.association_report(data, [play()], as_of=8 * DAY)
        self.assertEqual(report["fetches"], 50)
        self.assertEqual(report["served_lists"], 1)
        self.assertIsNone(report["confirmed_views"])
        self.assertEqual(report["rows"]["personal"]["opportunities"], 2)
        self.assertEqual(report["rows"]["personal"]["later_plays"], 1)
        self.assertFalse(report["causal"])

    def test_post_play_exposure_gets_no_credit(self):
        for at in (200, 300):
            report = self.api.association_report(online(at), [play()], as_of=8 * DAY)
            self.assertEqual(report["rows"]["personal"]["fractional_credit"], 0)
            self.assertEqual(report["rows"]["personal"]["later_plays"], 0)
            self.assertEqual(report["unmatched_plays"], 1)

    def test_ips_without_propensity_is_unsupported(self):
        for method in ("ips", "doubly_robust"):
            estimate = self.api.release_estimate(method)
            self.assertEqual(estimate["status"], "unsupported")
            self.assertIsNone(estimate["estimate"])
            self.assertIn("exact_action_probabilities", estimate["requires"])
            self.assertIn("policy_version", estimate["requires"])

    def test_maturity_and_horizon_exclude_early_and_late_credit(self):
        immature = self.api.association_report(online(), [play()], as_of=DAY)
        self.assertEqual(immature["pending_lists"], 1)
        self.assertEqual(immature["rows"], {})
        late = self.api.association_report(online(), [play(8 * DAY)], as_of=9 * DAY)
        self.assertEqual(late["rows"]["personal"]["later_plays"], 0)
        boundary = self.api.association_report(online(), [play(100 + 7 * DAY)],
                                      as_of=100 + 7 * DAY)
        self.assertEqual(boundary["rows"]["personal"]["later_plays"], 1)

    def test_strata_use_only_prior_observations(self):
        data = online()
        data["exp"]["2026-10-02"] = {"shared": {"a": [1, 0, 300]}}
        report = self.api.association_report(data, [play(400)], as_of=8 * DAY,
                                    historical_popularity={ITEM: [0, 500], OTHER: []})
        self.assertEqual(report["previous_exposure"]["none_observed"]["opportunities"], 2)
        self.assertEqual(report["previous_exposure"]["observed"]["opportunities"], 2)
        self.assertEqual(report["popularity_band"]["1-4"]["opportunities"], 2)
        self.assertEqual(report["position_band"]["1-5"]["opportunities"], 4)
        self.assertEqual(report["rows"]["personal"]["fractional_credit"], .5)
        self.assertEqual(report["rows"]["shared"]["fractional_credit"], .5)

    def test_open_coarse_unreliable_and_future_plays_supply_no_credit(self):
        records = [play(action="open"), play(reliable=False),
                   play(timestamp_precision="day"), play(10 * DAY),
                   play(observed_at=10 * DAY)]
        report = self.api.association_report(online(), records, as_of=8 * DAY)
        self.assertEqual(report["eligible_plays"], 0)
        self.assertEqual(report["rows"]["personal"]["fractional_credit"], 0)
        duplicate = self.api.association_report(online(), [play(), play(source="other")], as_of=8 * DAY)
        self.assertEqual(duplicate["eligible_plays"], 1)
        self.assertEqual(duplicate["rows"]["personal"]["fractional_credit"], 1)

    def test_position_bands_duplicate_cards_and_kind_boundaries(self):
        data = online()
        data["keys"] += [f"tmdb:{n}@movie" for n in range(3, 15)]
        data["lists"]["a"] = "0 " + " ".join(str(n) for n in range(14))
        records = [play(media_kind="tv", action="played_episode", episode=(1, 1))]
        report = self.api.association_report(data, records, as_of=8 * DAY)
        self.assertEqual(report["rows"]["personal"]["opportunities"], 14)
        self.assertEqual(report["position_band"]["6-12"]["opportunities"], 7)
        self.assertEqual(report["position_band"]["13+"]["opportunities"], 3)
        self.assertEqual(report["rows"]["personal"]["fractional_credit"], 0)

    def test_replay_censors_immature_cases_and_keeps_supported_misses(self):
        cases = [{"at": 300, "ranking": [OTHER], "target": ITEM},
                 {"at": 8 * DAY, "ranking": [ITEM], "target": ITEM},
                 {"at": 300, "ranking": [], "target": ITEM}]
        report = self.api.restricted_replay(online(), cases, as_of=8 * DAY)
        self.assertEqual(report["matched"][0]["metrics"]["hits"], 0)
        self.assertEqual([c["reason"] for c in report["unmatched"]],
                         ["immature", "no_exposed_candidates"])
        self.assertFalse(report["extrapolation"])

    def test_replay_reports_support_and_unmatched_without_extrapolation(self):
        absent = ("movie", "tmdb:3")
        cases = [{"at": 300, "ranking": [absent, OTHER, ITEM], "target": ITEM},
                 {"at": 300, "ranking": [absent], "target": absent},
                 {"at": 50, "ranking": [ITEM], "target": ITEM}]
        report = self.api.restricted_replay(online(), cases, as_of=8 * DAY)
        self.assertEqual(report["support_fraction"], 1 / 3)
        self.assertEqual(report["candidate_support_fraction"], 2 / 5)
        self.assertEqual(len(report["unmatched"]), 2)
        self.assertEqual(report["matched"][0]["metrics"]["ndcg_at_k"], 1 / 1.584962500721156)
        self.assertFalse(report["unbiased"])
        self.assertEqual(report["evidence_level"], "diagnostic")

    def test_empty_inputs_are_na_and_inputs_are_detached(self):
        data, records = online(), [play()]
        before = copy.deepcopy((data, records))
        self.api.association_report(data, records, as_of=8 * DAY)
        self.assertEqual((data, records), before)
        self.assertIsNone(self.api.restricted_replay(data, [], as_of=8 * DAY)["support_fraction"])
        for kwargs in ({"as_of": float("nan")}, {"as_of": 100, "horizon_seconds": 0}):
            with self.assertRaises(ValueError):
                self.api.association_report(data, records, **kwargs)
