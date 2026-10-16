"""Synthetic interviews: diverse sittings, adaptation, progress and bounded skips."""
from skald.members import rid
from decide_kit import write_legacy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald.engine import Engine
from skald.__main__ import interview_command, main, parser
from decide_kit import check_contract, close_server, load_server
from test_api import raw_request


class InterviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name)
        write_legacy(data)
        (data / "profile.json").write_text('{"entries": []}')
        (data / "taste.json").write_text('{"people": {}}')
        with redirect_stdout(io.StringIO()):
            self.e = Engine(data)
        self.addCleanup(self.e.close)
        self.s = self.e._server
        facets, anchors = {}, []
        for i, x in enumerate(self.s.EXPERIENCES):
            for j in range(6):
                key = f"tmdb:{900100+i*6+j}"
                kind = "tv" if j % 2 else "movie"
                facets[key] = {"title": f"Synthetic anchor {i} {j}", "kind": kind, "y": str(1970+i%5*10), "x": {x: 3}, "g": []}
                anchors.append((key, kind, x, "proxy"))
        self.s._facets.update(facets)
        self.s.ANCHORS = tuple(anchors)
        self.e._index = self.e._title_index()
        self.e._refresh()

    def answer(self, a, action="love", reasons=None):
        return {"key": a[0], "kind": a[1], "title": self.s.frec(a[0], a[1])["title"], "year": "2020",
                "action": action, "reasons": reasons or []}

    def test_too_new_keeps_diverse_interview_without_percentages(self):
        view = self.e.interview("ana")
        self.assertEqual(len(view["anchors"]), 12)
        self.assertEqual(len({a["experience"] for a in view["anchors"]}), 12)
        self.assertEqual({a["kind"] for a in view["anchors"]}, {"movie", "tv"})
        self.assertNotIn('"match"', json.dumps(view))
        self.assertNotIn('"percentile"', json.dumps(view))
        self.assertEqual(view["maturity"]["state"], "too_new")
        self.assertFalse(view["done"])
        self.assertIsNone(view["done_reason"])

    def test_learning_keeps_interview_without_reasoned_negatives(self):
        for a in self.s.ANCHORS[:20]:
            view = self.e.interview("ana", self.answer(a))
        self.assertEqual(view["maturity"]["state"], "learning")
        self.assertFalse(view["done"])
        self.assertIsNone(view["done_reason"])
        self.assertTrue(view["anchors"])

    def test_ready_finishes_without_reasoned_negatives(self):
        for a in self.s.ANCHORS[:40]:
            view = self.e.interview("ana", self.answer(a))
        self.assertEqual(view["maturity"]["state"], "ready")
        self.assertTrue(view["done"])
        self.assertEqual(view["done_reason"], "ready")
        self.assertEqual(view["progress"]["negatives_with_reason"], 0)
        self.assertEqual(view["anchors"], [])
        with patch.object(self.s, "who_watching", return_value=("ana", "phone")), \
                patch.object(self.s, "feed_rows", return_value=[]):
            self.assertEqual(self.s.remote_rows("de-DE"), {"rows": []})

    def test_unsure_stops_come_first_until_answered_even_when_ready(self):
        for a in self.s.ANCHORS[:40]:
            self.e.interview("ana", self.answer(a))
        key = "tmdb:900999"
        self.s._facets[key] = {"title": "Synthetic stop", "kind": "movie", "y": "2021", "g": []}
        stop = {"key": key, "id": 900999, "kind": "movie", "stype": "movie", "title": "Synthetic stop", "year": "2021",
                "signal": "abandoned", "weight": 0.0, "age": 5, "item": {}, "who": "owner", "by": ["ana"],
                "flags": [], "genres": [], "abandon": "unsure"}
        profile = self.s._profiles["household"]
        self.s._profiles["household"] = {**profile, "entries": profile["entries"] + [stop]}
        self.s._interview_cache.clear()
        view = self.s.interview_next("ana")
        self.assertFalse(view["done"])
        self.assertEqual([(a["key"], a["experience"]) for a in view["anchors"]], [(key, "abandoned")])
        self.s.interview_answer({"key": key, "kind": "movie", "title": "Synthetic stop", "year": "2021",
                                 "action": "not_for_us", "reasons": [], "who": "ana"})
        view = self.s.interview_next("ana")
        self.assertTrue(view["done"])
        self.assertEqual(view["anchors"], [])

    def test_early_stops_are_not_asked(self):
        from test_abandon_reading import rec
        key = "tmdb:900999"
        self.s._facets[key] = {**rec("Synthetic broken stream"), "runtime": 110}
        stop = {"key": key, "id": 900999, "kind": "movie", "stype": "movie", "title": "Synthetic broken stream",
                "year": "2020", "signal": "abandoned", "weight": -1.5, "age": 5,
                "item": {"duration_min": 2}, "who": "owner", "by": [self.s.PEOPLE[0]], "flags": [], "genres": []}
        self.s.read_abandoned([stop])
        self.assertEqual(stop["abandon"], "early")
        profile = self.s._profiles["household"]
        self.s._profiles["household"] = {**profile, "entries": profile["entries"] + [stop]}
        self.s._interview_cache.clear()
        self.assertNotIn(key, [a["key"] for a in self.s.interview_next(self.s.PEOPLE[0])["anchors"]])

    def test_five_loves_learn_experience_and_progress(self):
        x = next(iter(self.s.EXPERIENCES))
        for a in [a for a in self.s.ANCHORS if a[2] == x][:5]:
            view = self.e.interview("ana", self.answer(a))
        self.assertEqual(view["progress"]["positives"], 5)
        axes = self.s.taste_axes(self.e._model, "ana")
        self.assertIn("x:"+x, [a["id"] for a in axes["x"][:3]])
        self.assertFalse(view["done"])
        self.assertEqual(view["maturity"]["explicit"], 5)

    def test_done_needs_twenty_positives_and_five_reasoned_negatives(self):
        for a in self.s.ANCHORS[:20]:
            self.e.interview("ana", self.answer(a))
        self.assertFalse(self.e.interview("ana")["done"])
        for a in self.s.ANCHORS[20:25]:
            self.e.interview("ana", self.answer(a, "not_for_us", ["too slow"]))
        view = self.e.interview("ana")
        self.assertTrue(view["done"])
        self.assertEqual(view["maturity"]["state"], "learning")
        self.assertEqual(view["done_reason"], "answers")
        self.assertEqual(view["anchors"], [])

    def test_cache_tracks_person_model_feedback_and_skips(self):
        s = self.s
        with patch.object(s, "interview_rank", wraps=s.interview_rank) as rank:
            first = s.interview_next("ana")
            self.assertEqual(s.interview_next("ana"), first)
            self.assertEqual(rank.call_count, 1)
            s.interview_next("ben")
            self.assertEqual(rank.call_count, 2)
            s.interview_answer({**self.answer(s.ANCHORS[0]), "who": "ana"})
            self.assertEqual(s.interview_next("ana")["progress"]["positives"], 1)
            self.assertEqual(rank.call_count, 3)
            s.interview_answer({**self.answer(s.ANCHORS[1], "skip"), "who": "ana"})
            self.assertNotIn(s.ANCHORS[1][0], [a["key"] for a in s.interview_next("ana")["anchors"]])
            self.assertEqual(rank.call_count, 4)
            self.e._refresh()
            s.interview_next("ana")
            self.assertEqual(rank.call_count, 5)

    def test_cli_done_message_and_exit_zero(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for reason, why in (("ready", "member is ready"), ("answers", "20 positives and 5 reasoned negatives")):
            with self.subTest(reason=reason):
                view = {**self.e.interview("ana"), "done": True, "done_reason": reason, "anchors": []}
                for remote in (False, True):
                    out = io.StringIO()
                    argv = ["--snapshot", self.temp.name, "--state-dir", state.name, "interview", "--person", "ana"]
                    with patch("skald.__main__.remote.request", return_value=view), \
                            patch.object(Engine, "interview", return_value=view), patch("builtins.input") as read:
                        self.assertEqual(main(argv + (["--remote"] if remote else []), out), 0)
                    read.assert_not_called()
                    self.assertEqual(out.getvalue(), f"Interview complete: {why}.\n")

    def test_skip_is_private_bounded_and_does_not_teach(self):
        before = self.e.profile("ana")
        a = self.s.ANCHORS[0]
        view = self.e.interview("ana", self.answer(a, "skip"))
        self.assertNotIn(a[0], [a["key"] for a in view["anchors"]])
        self.assertEqual(before, self.e.profile("ana"))
        self.assertEqual(os.stat(self.e._work / "interview.json").st_mode & 0o777, 0o600)
        self.assertFalse((Path(self.temp.name) / "interview.json").exists())
        for i in range(501):
            self.s.interview_answer({"key": f"tmdb:{i+1}", "kind": "tv", "who": "ana", "action": "skip"})
        self.assertEqual(len(self.s.interview_skips()), 500)

    def test_popularity_entropy_and_neutral_proxy(self):
        anchors = self.s.ANCHORS[:3]
        with patch.object(self.s, "person_score", side_effect=[(0., []), (.5, []), (1., [])]):
            ranked = self.s.interview_rank(anchors, "ana")
        self.assertEqual(ranked[0], anchors[1])
        self.assertTrue(all(a[3] == "proxy" for a in ranked))
        self.s.frec(*anchors[1][:2])["vote_count"] = 0
        with patch.object(self.s, "person_score", side_effect=[(0., []), (.5, []), (1., [])]):
            self.assertEqual(self.s.interview_rank(anchors, "ana")[0], anchors[0])

    def test_bound_api_answers_and_immediate_progress(self):
        s = self.s
        token = s.new_client("interview-member", ["read", "events"], "ana")
        headers = {"Authorization": "Bearer "+token}
        a = s.ANCHORS[0]
        body = {"stream": "interview", "events": [{**self.answer(a), "who": "ana"}]}
        code, _, raw, _ = raw_request(s, "/api/v1/events", "POST", body, headers)
        self.assertEqual((code, json.loads(raw)["accepted"]), (200, 1))
        code, _, raw, _ = raw_request(s, "/api/v1/interview/next", headers=headers)
        self.assertEqual((code, json.loads(raw)["progress"]["positives"]), (200, 1))
        body["events"][0]["who"] = "ben"
        self.assertEqual(raw_request(s, "/api/v1/events", "POST", body, headers)[0], 403)
        guest = s.new_client("interview-guest", ["read", "decide"], "guest")
        self.assertEqual(raw_request(s, "/api/v1/events", "POST", body, {"Authorization": "Bearer "+guest})[0], 403)

    def test_phone_skip_and_interview_source(self):
        s = self.s
        a = s.ANCHORS[0]
        with patch.object(s, "remote_token", return_value="fixture-token"):
            body = {**self.answer(a, "skip"), "who": "ana", "row": rid('interview', s.CONFIG), "undo": None}
            code, _, _, _ = raw_request(s, "/taste/fixture-token/feedback.json", "POST", body, handler=s.RemoteHandler)
            self.assertEqual(code, 201)
            body["action"] = "love"
            self.assertEqual(raw_request(s, "/taste/fixture-token/feedback.json", "POST", body, handler=s.RemoteHandler)[0], 201)
        events = list(s.remote_events(s.time.time()))
        self.assertEqual(events[0]["src"], "interview")

    def test_offline_cli_one_sitting_and_quit(self):
        args = parser().parse_args(["interview", "--person", "ana"])
        out = io.StringIO()
        with patch("builtins.input", side_effect=["l", "s", "q"]):
            result = interview_command(args, self.e, out)
        self.assertIn("temporary copy", out.getvalue())
        self.assertEqual(result["progress"]["positives"], 1)
        self.assertEqual(len(self.s.interview_skips()), 1)

    def test_decision_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            s = load_server(temp)
            try:
                check_contract(self, s, "interview_next")
            finally:
                close_server(s)


if __name__ == "__main__":
    unittest.main()
