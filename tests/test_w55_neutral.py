import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald.members import fresh_config


class NeutralPromptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cfg = fresh_config(["alex", "sam"])
        cfg["household"]["note"] = "Two flatmates who enjoy mysteries"
        Path(temp.name, "skald.json").write_text(json.dumps(cfg))
        self.s = load_server(temp.name)
        self.addCleanup(close_server, self.s)
        self.profile = {"likes": "mysteries", "entries": [{
            "who": "tv", "by": ["together"], "title": "Synthetic mystery", "year": "2020",
            "signal": "watched", "kind": "movie", "weight": 1, "key": "tmdb:1", "age": 1}],
            "genres": {}, "searches": [], "seen": set(), "negative": set()}
        for name, value in (("read_taste", {"people": {"alex": {"type": "INTP"}}}),
                            ("get_model", None), ("candidate_pool", []),
                            ("hit_titles", []), ("tired_keys", {})):
            stub = patch.object(self.s, name, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)
        network = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.s._profiles["household"] = self.profile

    def prompts(self):
        s = self.s
        candidate = {'id': 'tmdb:2', 'name': 'Invented Harbor', 'type': 'movie', '_week_pct': 80,
                     '_row': {'watchable': '2026-10-01', 'date': '2026-10-01'}}
        s._learn.pop('week', None)
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'get_model', return_value={'cal': True}), \
                patch.object(s, 'week_candidates', return_value=[candidate]), \
                patch.object(s, 'week_scope', return_value=[candidate]), patch.object(s, 'llm_text', return_value='{}') as llm:
            s.week_feed('en-US')
            week = '\n'.join(llm.call_args.args[:2])
        with patch.object(s, "llm_sections", return_value={}) as llm:
            s.personal_feed("en-US")
            personal = "\n".join(llm.call_args.args[:2])
        body = s.DECISIONS["mood_pick"]["llm"]["prompt"]([{"state": [s.mood_state({"names": {}, "aff": {}})]}])
        mood = body["state"] + "\n" + "\n".join(q["instructions"] for q in body["questions"].values())
        return {"week": week, "personal": personal, "mood": mood}

    def test_two_members_do_not_imply_couple(self):
        self.assertEqual(len(self.s.PEOPLE), 2)
        for name, text in self.prompts().items():
            with self.subTest(prompt=name):
                self.assertNotRegex(text.lower(), r"\b(couple|partner)\b")
                self.assertIn("Household of two sharing one TV, Alex and Sam", text)

    def test_shared_tv_text_does_not_claim_everyone_watched(self):
        for people in (("alex",), ("alex", "sam"), ("alex", "sam", "lee")):
            with self.subTest(people=people), patch.object(self.s, "PEOPLE", people):
                text = self.s.profile_text(self.profile)
                self.assertIn("Recently on the shared TV account (may include anyone in the household):", text)
                self.assertNotRegex(text, r"both of them|everyone in the household|partner")

    def test_profile_text_has_no_gendered_default(self):
        for subject in ("they", "Alex"):
            with self.subTest(subject=subject):
                text = self.s.profile_text(self.profile, subject)
                self.assertNotRegex(text.lower(), r"\b(he|she|his|her)\b")
                self.assertIn("mysteries", text)

    def test_household_note_reaches_week_and_mood_prompts(self):
        for note in ("Two flatmates who enjoy mysteries", "Two flatmates who enjoy mysteries.",
                     "Two flatmates who enjoy mysteries! ? \t\n", ""):
            with self.subTest(note=note), patch.dict(self.s.CONFIG["household"], note=note):
                prompts = self.prompts()
                for name, text in prompts.items():
                    with self.subTest(prompt=name):
                        self.assertEqual(text.count("Two flatmates who enjoy mysteries"), int(bool(note)))
                        self.assertNotIn("; .", text)
                        self.assertNotIn("..", text)
                        if note:
                            self.assertIn("Two flatmates who enjoy mysteries.", text)
                self.assertIn("personality types Alex INTP: a light hint only, taste comes from what they watch",
                              prompts["personal"])
                if note:
                    self.assertIn("taste comes from what they watch); Two flatmates who enjoy mysteries.",
                                  prompts["personal"])


if __name__ == "__main__":
    unittest.main()
