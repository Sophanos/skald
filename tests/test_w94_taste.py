"""W94: taste archetypes and the opt-in New to You row (synthetic data, no network)."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, synthetic_model
from skald import backup, commands as c, engine, history, members, setup
from skald.__main__ import text_output

ROOT = Path(__file__).resolve().parents[1]
NOW = 1791734400.
DAY = 86400


def reply(name, line, cost=.0001):
    return {"choices": [{"message": {"content": json.dumps({"name": name, "line": line})}}], "usage": {"cost": cost}}


class FakeOpenRouter:
    """Counts model calls; answers every call with the same reply (or raises)."""
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls, self.bodies = answer, error, 0, []

    def __call__(self, req, **kwargs):
        assert req.full_url.startswith("https://openrouter.ai/"), "unexpected host"
        self.calls += 1
        self.bodies.append(json.loads(req.data))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"DATA_DIR": self.temp.name, "SKALD_CONFIG": ""})
        env.start()
        self.addCleanup(env.stop)
        setup.publish(self.root, members.fresh_config(["ana", "ben"]))
        self.s = load_server(self.root)
        self.addCleanup(close_server, self.s)
        net = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        net.start()
        self.addCleanup(net.stop)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        s = self.s
        self.m = m = synthetic_model(s)
        m["paff"]["ana"].update({"x:whodunit": .8, "x:dread": .5, "x:mind_bender": .3, "x:heist": -.4})
        m["paff"]["ben"].update({"x:cozy_case": .8, "x:found_family": .6, "x:magic_wonder": .3})
        m["paff"]["together"].update({"x:cozy_case": .6, "x:found_family": .4})
        m["aff"].update({"x:whodunit": .5, "x:cozy_case": .7, "x:found_family": .5, "x:dread": .2, "x:magic_wonder": .2})
        for fid in [f for f in {**m["aff"], **m["paff"]["ana"], **m["paff"]["ben"]} if f.startswith("x:")]:
            m["names"][fid] = s.EXPERIENCES[fid[2:]][0]

    def write(self, items):
        Path(self.s.DATA, "archetypes.json").write_text(json.dumps({"v": 1, "items": items}))

    def keyed(self, fake):
        key = patch.object(self.s, "OR_KEY", "synthetic")
        net = patch.object(self.s, "_connector_open", fake)
        key.start()
        net.start()
        self.addCleanup(net.stop)
        self.addCleanup(key.stop)


class ArchetypeLabels(Base):
    def test_rule_label_is_deterministic_plain_and_short(self):
        s = self.s
        for who in ("ana", "ben", "household"):
            with self.subTest(who=who):
                brief = s.archetype_brief(self.m, who)
                self.assertEqual(brief["scope"], "household" if who == "household" else "member")
                one, two = s.archetype_label(brief), s.archetype_label(json.loads(json.dumps(brief)))
                self.assertEqual(one, two)
                self.assertTrue(0 < len(one["name"]) <= 48 and len(one["name"].split()) <= 6)
                self.assertTrue(0 < len(one["line"]) <= 140)
                self.assertNotIn("\u2014", one["name"] + one["line"])
        self.assertNotEqual(s.archetype_label(s.archetype_brief(self.m, "ana"))["name"],
                            s.archetype_label(s.archetype_brief(self.m, "ben"))["name"])
        empty = s.archetype_label({"scope": "member", "experiences": [], "themes": [], "groups": []})
        self.assertEqual(empty["name"], s.t("archetype.new"))

    def test_brief_carries_facets_only(self):
        s = self.s
        brief = s.archetype_brief(self.m, "ana")
        text = json.dumps(brief)
        self.assertNotIn("Sentinel", text)
        self.assertNotIn("ana", text.casefold().replace("banter", ""))
        self.assertIn("whodunit", brief["experiences"])
        self.assertNotIn("heist", brief["experiences"])
        self.assertTrue(set(brief["groups"]) <= set(s.MOODS))
        s.check(s.DECISIONS["name_archetype"]["inp"], brief)

    def test_parse_validates_answers(self):
        s = self.s
        good = s.archetype_parse(reply("Slow-burn detective", "Twisty cases with a worldbuilder streak."))
        self.assertEqual(good, {"name": "Slow-burn detective", "line": "Twisty cases with a worldbuilder streak."})
        dashed = s.archetype_parse(reply("Puzzle chaser \u2014 night owl", "Clever plots – and slow dread."))
        self.assertNotIn("\u2014", dashed["name"])
        self.assertNotIn("–", dashed["line"])
        bad = [reply("x" * 49, "A fine line here."), reply("One two three four five six seven", "A fine line here."),
               reply("Puzzle fan", "see https://example.com now"), reply("<b>Fan</b>", "A fine line here."),
               reply("Puzzle fan", "Short."), reply("Puzzle fan", "y " * 80), reply("", "A fine line here."),
               {"choices": [{"message": {"content": "not json"}}]},
               {"choices": [{"message": {"content": json.dumps({"name": "Fan"})}}]},
               {"choices": [{"message": {"content": json.dumps({"name": 3, "line": "A fine line here."})}}]}]
        for answer in bad:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                s.archetype_parse(answer)

    def test_strings_live_in_english_locale(self):
        strings = json.loads((ROOT / "skald/locales/en.json").read_text())
        for mood in self.s.MOODS:
            self.assertIn("archetype.lead." + mood, strings)
            self.assertIn("archetype.streak." + mood, strings)
        for key in ("archetype.name", "archetype.new", "archetype.line", "archetype.line.household",
                    "archetype.line.new", "row.newtoyou", "row.newtoyou.named", "why.newtoyou"):
            self.assertIn(key, strings)
        self.assertFalse([k for k, v in strings.items() if "\u2014" in v and k.startswith(("archetype.", "row.newtoyou", "why.newtoyou"))])


class ArchetypeRefresh(Base):
    def test_no_key_falls_back_without_network_or_file(self):
        s = self.s
        with patch.object(s, "OR_KEY", ""), patch.object(s, "_connector_open", side_effect=AssertionError("spent")):
            self.assertIn("archetypes", [name for name, _, _ in s.DAILY_JOBS])
            due = next(d for name, d, _ in s.DAILY_JOBS if name == "archetypes")
            self.assertFalse(due(NOW))
            out = s.refresh_archetypes(NOW)
            self.assertEqual(out.get("calls", 0), 0)
            view = s.archetype_view("ana")
        self.assertFalse(Path(s.DATA, "archetypes.json").exists())
        self.assertEqual(view["source"], "rule")
        self.assertEqual(view["name"], s.archetype_label(s.archetype_brief(self.m, "ana"))["name"])

    def test_refresh_uses_command_model_and_runs_at_most_weekly(self):
        s = self.s
        fake = FakeOpenRouter(reply("Twisty puzzle chaser", "Clever cases with a streak of slow dread."))
        self.keyed(fake)
        s.refresh_archetypes(NOW)
        self.assertEqual(fake.calls, 3)  # ana, ben, household
        body = fake.bodies[0]
        self.assertEqual(body["model"], s.COMMAND_MODEL)
        self.assertEqual(body["reasoning"], {"effort": s.COMMAND_EFFORT})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("Sentinel", json.dumps(fake.bodies))
        items = json.loads(Path(s.DATA, "archetypes.json").read_text())["items"]
        self.assertEqual(set(items), {"ana", "ben", "household"})
        self.assertEqual(items["ana"]["source"], "llm")
        self.assertEqual(s.archetype_view("ana")["name"], "Twisty puzzle chaser")
        for day in range(1, 7):
            s.refresh_archetypes(NOW + day * DAY)
        self.assertEqual(fake.calls, 3)
        with patch.dict(s._decide_cache, clear=True):
            s._decide_cache.clear()
            s.refresh_archetypes(NOW + 7 * DAY)
        self.assertEqual(fake.calls, 6)
        for day in range(8, 30):
            s.refresh_archetypes(NOW + day * DAY)
        self.assertLessEqual(fake.calls, 3 * 5)

    def test_failed_or_invalid_answer_keeps_rule_label_for_a_week(self):
        s = self.s
        for fake in (FakeOpenRouter(error=OSError("synthetic outage")),
                     FakeOpenRouter(reply("Visit https://spam.example", "Buy now at https://spam.example today."))):
            with self.subTest(fake=fake.error or "invalid"):
                Path(s.DATA, "archetypes.json").unlink(missing_ok=True)
                s._decide_cache.clear()
                with patch.object(s, "OR_KEY", "synthetic"), patch.object(s, "_connector_open", fake):
                    s.refresh_archetypes(NOW)
                    first = fake.calls
                    self.assertGreaterEqual(first, 3)
                    view = s.archetype_view("ana")
                    self.assertEqual(view["source"], "rule")
                    self.assertEqual(view["name"], s.archetype_label(s.archetype_brief(self.m, "ana"))["name"])
                    s.refresh_archetypes(NOW + 3 * DAY)
                    self.assertEqual(fake.calls, first)

    def test_job_runs_in_background_and_skips_without_profile(self):
        s = self.s
        fake = FakeOpenRouter(reply("Twisty puzzle chaser", "Clever cases with a streak of slow dread."))
        self.keyed(fake)
        name, due, run = next(j for j in s.DAILY_JOBS if j[0] == "archetypes")
        self.assertTrue(due(NOW))
        with patch.object(s.threading, "Thread") as thread:
            out = run(NOW)
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs.get("daemon"))
        self.assertEqual(fake.calls, 0)
        self.assertTrue(all(type(v) in (int, str) for v in out.values()))
        with patch.object(s, "get_model", lambda build=True: None):
            self.assertFalse(due(NOW))
            self.assertEqual(s.refresh_archetypes(NOW).get("calls", 0), 0)
        self.assertEqual(fake.calls, 0)

    def test_view_reads_file_and_ignores_unknown_or_broken_entries(self):
        s = self.s
        self.write({"ana": {"name": "Cached name", "line": "Cached line for this person.", "source": "llm", "at": int(NOW)},
                    "cal": {"name": "Old member", "line": "Should never be shown.", "source": "llm", "at": int(NOW)}})
        with patch.object(s, "_connector_open", side_effect=AssertionError("spent")):
            self.assertEqual(s.archetype_view("ana")["name"], "Cached name")
            self.assertEqual(s.archetype_view("cal")["name"], s.t("archetype.new"))
            self.assertEqual(s.archetype_view("ben")["source"], "rule")
            Path(s.DATA, "archetypes.json").write_text("{broken")
            self.assertEqual(s.archetype_view("ana")["source"], "rule")

    def test_archetypes_never_change_scores(self):
        s = self.s
        keys = [f"tmdb:{i}" for i in range(900001, 900013)]
        before = {k: (s.score_key(self.m, k, kind="movie"), s.explain(k, "movie")) for k in keys}
        self.write({who: {"name": "Cached name", "line": "Cached line for everyone here.", "source": "llm", "at": int(NOW)}
                    for who in ("ana", "ben", "household")})
        after = {k: (s.score_key(self.m, k, kind="movie"), s.explain(k, "movie")) for k in keys}
        for k in keys:
            self.assertEqual(before[k][0], after[k][0])
            self.assertEqual({x: v for x, v in before[k][1].items() if x != "archetypes"},
                             {x: v for x, v in after[k][1].items() if x != "archetypes"})
            self.assertEqual(after[k][1]["archetypes"]["ana"]["name"], "Cached name")
            self.assertEqual(set(after[k][1]["archetypes"]), {"household", "ana", "ben"})


class ArchetypeData(Base):
    def test_backup_lists_and_snapshot_files(self):
        self.assertIn("archetypes.json", backup.BACKUP_FILES)
        self.assertIn("archetypes.json", self.s.BACKUP_FILES)
        self.assertIn("archetypes.json", engine.DATA_FILES)

    def test_forget_removes_archetypes(self):
        self.write({"ana": {"name": "Cached name", "line": "Cached line for this person.", "source": "llm", "at": int(NOW)}})
        cfg = c.config(self.s)
        history.forget(self.root, cfg, "2100-01-01", "ana")
        self.assertTrue(Path(self.s.DATA, "archetypes.json").exists())
        history.forget(self.root, cfg, "2100-01-01", "ana", apply=True)
        self.assertNotIn("ana", json.loads(Path(self.s.DATA, "archetypes.json").read_text())["items"])


class NewToYouRow(Base):
    def cids(self):
        return [cid for cid, _, _ in self.s.feed_rows()]

    def test_opt_in_and_existing_order_unchanged(self):
        s = self.s
        cid = s.rid("newtoyou")
        self.assertIn("newtoyou", members.ROW_KEYS)
        self.assertEqual(members.ROW_KEYS[-2:], ("newtoyou", "lab"))
        self.assertFalse(s.row_enabled(cid))
        with patch.dict(s.CONFIG['household'], row_labels={}):
            before = self.cids()
        self.assertNotIn(cid, before)
        day = dt.datetime(2026, 10, 13, 20)
        self.assertNotIn("newtoyou", [name for name, *_ in s.job_specs(day, {})])
        rows = [k for k in members.ROW_KEYS if k not in ("digital", "newtoyou")]
        with patch.dict(s.CONFIG["household"], rows=rows + ["newtoyou"]):
            self.assertTrue(s.row_enabled(cid))
            after = self.cids()
            specs = {name: spec for name, *spec in s.job_specs(day, {})}
        self.assertIn(cid, after)
        self.assertEqual([x for x in after if x != cid], before)
        self.assertEqual(specs["newtoyou"][0], [cid])
        self.assertEqual(s.DEDUPE_ORDER[-1], cid)
        self.assertFalse(s.GUEST_ROW_POLICY[cid])
        self.assertIn(cid, s.REMOTE_ROWS)
        self.assertNotIn(cid, s.LEARN_ROWS)
        self.assertIn(cid, s.MIXED)

    def test_add_row_command(self):
        s = self.s
        for text in ("add the New to You row", "show new to you", "bring back the New to You row"):
            with self.subTest(text=text):
                self.assertEqual(c.parse_local(text, s)["op"], "row_show")
                self.assertEqual(c.parse_local(text, s)["values"], ["newtoyou"])
        self.assertEqual(c.parse_local("hide the New to You row", s)["values"], ["newtoyou"])
        self.assertEqual(c.parse_local("add a row with new movies", s)["op"], "shelf_add")
        cfg = members.fresh_config(["ana", "ben"])
        c.edit_rows(cfg, {"op": "row_show", "kind": None, "values": ["newtoyou"]}, s)
        self.assertEqual(cfg["household"]["rows"][-1], "newtoyou")
        self.assertNotIn("digital", cfg["household"]["rows"])
        cfg = members.fresh_config(["ana", "ben"])
        c.edit_rows(cfg, {"op": "row_hide", "kind": None, "values": ["top10"]}, s)
        self.assertNotIn("newtoyou", cfg["household"]["rows"])
        self.assertIn("newtoyou", c.command_prompt(s))


class NewToYouCandidates(Base):
    def setUp(self):
        super().setUp()
        s = self.s
        self.p = s._profiles["household"]
        self.pool = []
        for i in range(910001, 910031):
            tags = {"whodunit": 3, "cozy_case": 2} if i < 910013 else {"whodunit": 3, "found_family": 2} if i < 910020 else {"whodunit": 3}
            s._facets[f"tmdb:{i}"] = {"title": f"Synthetic {i} Candidate", "kind": "movie", "x": tags, "k": [], "d": []}
            self.pool.append((f"tmdb:{i}", "movie", self.meta(i)))
        self.bad = {}
        for i, why in ((910101, "seen"), (910102, "negative"), (910103, "deferred"), (910104, "avoided"),
                       (910105, "quality"), (910106, "tone")):
            tags = {"whodunit": 3, "cozy_case": 2, **({"heist": 3} if why == "avoided" else {})}
            s._facets[f"tmdb:{i}"] = {"title": f"Synthetic {i} Bad", "kind": "movie", "x": tags, "k": [], "d": []}
            meta = self.meta(i, rating=5.1 if why == "quality" else 7.6, genres=["Family"] if why == "tone" else ["Drama"])
            self.pool.append((f"tmdb:{i}", "movie", meta))
            self.bad[f"tmdb:{i}"] = why
        self.p["seen"] = {"tmdb:910101"}
        self.p["negative"] = {"tmdb:910102"}

    @staticmethod
    def meta(i, rating=7.6, genres=("Drama",)):
        return {"id": f"tmdb:{i}", "type": "movie", "name": f"Synthetic {i} Candidate", "poster": "p", "background": "b",
                "description": "", "genres": list(genres),
                "_row": {"votes": 900, "rating": rating, "g": list(genres), "date": "2015-01-01", "k": [], "c": [],
                         "genre_ids": []}}

    def test_plan_keeps_one_strong_facet_and_changes_one_axis(self):
        s = self.s
        plan = s.newtoyou_plan(self.m, "ana")
        self.assertTrue(plan)
        strong = {"whodunit", "dread", "mind_bender"}
        for keep, change, origin in plan:
            self.assertIn(keep, strong)
            self.assertNotEqual(keep, change)
            self.assertNotIn(change, strong)
            self.assertNotEqual(change, "heist")
            self.assertIn(origin, ("group", "neighbour", "blind"))
        self.assertIn(("whodunit", "cozy_case", "neighbour"), plan)
        self.assertEqual(plan, s.newtoyou_plan(self.m, "ana"))
        self.assertTrue(s.newtoyou_plan(self.m, "together"))

    def test_feed_filters_and_needs_both_axes(self):
        s = self.s
        calls = []
        def pool(m, p, xs, lang, seeds=6, extra=(), seed=None, **kwargs):
            calls.append(list(xs))
            return list(self.pool)
        with patch.object(s, "feeling_pool", pool), patch.object(s, "day_index", return_value=0), \
                patch.object(s, "deferred_keys", return_value=frozenset({"tmdb:910103"})), \
                patch.object(s, "tired_keys", return_value={}):
            out = s.newtoyou_feed("en-US")
        cid = s.rid("newtoyou")
        self.assertIsNotNone(out)
        ids = [x["id"] for x in out[cid]["tv"]]
        self.assertGreaterEqual(len(ids), 8)
        self.assertFalse(set(ids) & set(self.bad))
        self.assertNotIn("tmdb:910025", ids)  # keeps the facet but changes nothing
        for x in out[cid]["tv"]:
            tags = s.frec(x["id"], "movie")["x"]
            self.assertGreaterEqual(tags["whodunit"], 2)
            self.assertTrue(tags.get("cozy_case", 0) >= 2 or tags.get("found_family", 0) >= 2)
        self.assertTrue(all(xs[0] in ("whodunit", "dread", "mind_bender") for xs in calls))
        name = s.archetype_view("ana")["name"]
        self.assertEqual(out["_names"][cid]["title"], s.t("row.newtoyou.named", name=name))
        self.assertIn(s.t("experience.cozy_case"), out[cid]["tv"][0]["description"])

    def test_feed_is_quiet_without_taste(self):
        s = self.s
        with patch.object(s, "get_model", lambda build=True: None):
            self.assertIsNone(s.newtoyou_feed("en-US"))


class LightRejection(Base):
    def event(self, action, row):
        return {"t": NOW - DAY, "id": "e1", "key": "tmdb:1", "kind": "movie", "title": "Synthetic", "year": "2020",
                "who": "ana", "action": action, "row": row}

    def test_rejecting_a_new_to_you_pick_counts_lightly(self):
        s = self.s
        new, other = s.rid("newtoyou"), s.rid("foryou")
        light = s.feedback_weight(self.event("not_for_us", new), NOW)
        full = s.feedback_weight(self.event("not_for_us", other), NOW)
        self.assertEqual(light[0], full[0])
        self.assertLess(light[1], 0)
        self.assertLessEqual(abs(light[1]), .25 * abs(full[1]))
        self.assertEqual(full[1], s.feedback_weight(self.event("not_for_us", None), NOW)[1])
        for action in ("love", "like", "meh", "seen"):
            self.assertEqual(s.feedback_weight(self.event(action, new), NOW), s.feedback_weight(self.event(action, other), NOW))


class Surfaces(Base):
    def test_cli_me_and_explain_show_the_archetype(self):
        patch.stopall()
        out = io.StringIO()
        data = {"say": "Ana, hello.", "_count": "Ana", "sources": [], "top": {}, "facts": [],
                "pending": {"review": 0, "memory": 0}, "actions": [],
                "archetype": {"name": "Twisty puzzle chaser", "line": "Clever cases with slow dread.", "source": "llm"}}
        text_output(SimpleNamespace(command="me", person="ana"), data, out)
        self.assertIn("Taste: Twisty puzzle chaser. Clever cases with slow dread.", out.getvalue())
        out = io.StringIO()
        why = {"key": "tmdb:1", "title": "Synthetic", "score": .1, "percentile": 50, "confidence": "Good fit", "reasons": [],
               "archetypes": {"household": {"name": "Cozy crew", "line": "Shared cozy cases.", "source": "rule"}}}
        text_output(SimpleNamespace(command="explain", taste=None), why, out)
        self.assertIn("Taste: Cozy crew. Shared cozy cases.", out.getvalue())

    def test_docs_and_changelog(self):
        cli = (ROOT / "docs/CLI.md").read_text()
        self.assertIn("newtoyou", cli)
        head = (ROOT / "CHANGELOG.md").read_text().split("## 1.4.1\n", 1)[1].split("**Data changes:** none.", 1)[0]
        self.assertIn("New to You", head)
        self.assertIn("archetypes.json", head)
        self.assertLessEqual(len((ROOT / "README.md").read_text().splitlines()), 260)
        self.assertLessEqual(len((ROOT / "AGENTS.md").read_text().splitlines()), 110)


class ReviewFixes(Base):
    def test_refresh_drops_a_label_for_someone_forgotten_mid_call(self):
        s = self.s
        fake = FakeOpenRouter(reply("Twisty puzzle chaser", "Clever cases with a streak of slow dread."))
        self.write({"ben": {"name": "Old ben", "line": "Old line.", "source": "llm", "at": int(NOW - 9 * DAY)}})

        def forget_during_call(req, **kwargs):
            if fake.calls == 0:
                # A forget of ana lands while her label is being named.
                Path(s.DATA, "history-forgotten.json").write_text(json.dumps({"ana": "2026-10-01"}))
            return fake(req, **kwargs)
        self.keyed(forget_during_call)
        s.refresh_archetypes(NOW)
        items = json.loads(Path(s.DATA, "archetypes.json").read_text())["items"]
        self.assertNotIn("ana", items)
        self.assertNotIn("household", items)
        self.assertEqual(items["ben"]["name"], "Twisty puzzle chaser")

    def curate_with(self, ctx):
        s = self.s
        s._memory = None
        s._memory_index.clear()
        names = {"x:found_family": "found family"}
        m = {"paff": {"ben": {"x:found_family": 1.}, "ana": {}}, "names": names, "nexp": {}}
        facets = {}
        evts = []
        for i, age in enumerate((10, 20)):
            key = f"tmdb:{i + 1}"
            facets[key] = {"kind": "movie", "x": {"found_family": 2}, "g": ["Horror"]}
            evts.append(s._event(NOW - age * DAY, f"remote:movie:{key}", key, "movie", "Synthetic", "2020", "ben", ["ben"],
                                 "not_for_us", "remote", ctx))
        fact = {"id": "m1", "person": "ben", "kind": "like", "about": "x:found_family", "text": "likes found family",
                "strength": .6, "n": 3, "evidence": ["e1"], "src": "implicit", "via": "curate", "status": "active",
                "gates": {}, "created": NOW - 100 * DAY, "confirmed": None, "seen": NOW, "end": None, "into": None}
        store = {**s._memory_empty(), "seq": 1, "facts": [fact]}
        with patch.object(s, "_facets", facets), patch.dict(s._model, {"m": m}):
            return s.curate(iter(evts), m, now=NOW, store=store, dry_run=True)["store"]["facts"][0]["status"]

    def test_light_rejections_do_not_supersede_a_like_memory(self):
        self.assertEqual(self.curate_with({}), "superseded")
        self.assertEqual(self.curate_with({"row": self.s.rid("newtoyou")}), "active")

    def test_pinned_new_to_you_refill_needs_the_same_member(self):
        s = self.s
        cid = s.rid("newtoyou")
        metas = {"tv": [{"id": "tmdb:1", "type": "movie"}]}
        with patch.dict(s._feeds, {(cid, "en-US"): {"names": {"who": "ben"}, "metas": metas}}):
            with patch.object(s, "feed", lambda c, lang: {"names": {"who": "ana"}}):
                self.assertEqual(s.source_metas(cid, "en-US"), [])
            with patch.object(s, "feed", lambda c, lang: {"names": {"who": "ben"}}):
                self.assertEqual(s.source_metas(cid, "en-US"), metas["tv"])

    def test_forgetting_one_member_keeps_other_labels(self):
        cfg = c.config(self.s)
        rec = {"name": "Synthetic label", "line": "Synthetic line.", "source": "llm", "at": int(NOW)}
        self.write({"ana": rec, "ben": rec, "household": rec})
        history.forget(self.root, cfg, "2026-10-01", "ana", apply=True)
        items = json.loads(Path(self.s.DATA, "archetypes.json").read_text())["items"]
        self.assertEqual(set(items), {"ben"})
        self.assertEqual(items["ben"]["at"], int(NOW))

    def test_parse_command_version_bumped(self):
        self.assertEqual(self.s.DECISIONS["parse_command"]["ver"], 19)


if __name__ == "__main__":
    unittest.main()
