from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM, reset
from test_api_routes import ApiCase

NOW = 1791734400.


class MoodPickTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.fitm = {k: (i+1)/12 for i, k in enumerate(self.s.MOODS)}
        self.rule = list(self.s.MOODS)[:4]

    def pick(self):
        with patch.object(self.s.time, "time", return_value=NOW), redirect_stdout(io.StringIO()):
            return self.s.pick_moods({}, self.fitm, self.rule)

    def test_three_prechange_records_and_outbound_body(self):
        s = self.s
        for mode in ("spread", "flat", "missing"):
            reset(s)
            s._learn["moodpicks"] = {}
            scores = {k: float(i%4) if mode != "flat" else 1. for i, k in enumerate(s.MOODS)}
            answers = {k: {"score": v} for k, v in scores.items()}
            if mode == "missing":
                answers.pop("dark")
            state = "Synthetic tonight " * 70
            with ReplayLLM(s) as replay, patch.object(s, "mood_state", return_value=state):
                replay.arm({"answers": answers, "usage": {"cost": .001}})
                self.pick()
                self.assertEqual(replay.calls, 1)
                self.assertEqual(replay.bodies[0], {"model": s.ROUTE_MODEL, "state": state, "questions": s.MOOD_Q,
                                                   "usage": {"include": True}})
                self.assertEqual(s._usage["calls"]["feed:mood_pick"], 1)
                self.assertNotIn("feed:moodpick", s._usage["calls"])
            # Recorded from c9a080f before replacing the paid call (frozen time and synthetic reply).
            expected = {"picks": ["dark", "ideas", "heart", "home"] if mode == "spread" else self.rule,
                        "source": "luna" if mode == "spread" else "rule", "at": int(NOW)}
            if mode == "spread":
                expected["luna"] = scores
            self.assertEqual(s._learn["moodpicks"]["2026-10-11"], expected)

    def test_cached_day_and_no_key_never_decide(self):
        s = self.s
        with patch.object(s, "decide", side_effect=AssertionError("unexpected decide")):
            s._learn["moodpicks"] = {"2026-10-11": {"picks": self.rule}}
            self.assertEqual(self.pick(), self.rule)
            s._learn["moodpicks"] = {}
            with patch.object(s, "OR_KEY", ""):
                self.assertEqual(self.pick(), self.rule)
        self.assertFalse(Path(s.DATA, "decisions.jsonl").exists())

    def test_cap_budget_and_internal_index(self):
        s = self.s
        for gate in ("cap", "budget"):
            reset(s)
            s._learn["moodpicks"] = {}
            with patch.object(s, "OR_KEY", "replay"), patch.object(s, "mood_state", return_value="Synthetic tonight"), \
                    patch.object(s, "openrouter", side_effect=s.BudgetExceeded) as llm:
                if gate == "cap":
                    s._usage["calls"]["feed:mood_pick"] = 3
                self.assertEqual(self.pick(), self.rule)
                self.assertEqual(llm.call_count, 0 if gate == "cap" else 1)
        self.assertNotIn("mood_pick", [x["type"] for x in s.decide_index()])
        self.assertEqual(s.DECISIONS["mood_pick"]["cap"], 3)


if __name__ == "__main__":
    unittest.main()
