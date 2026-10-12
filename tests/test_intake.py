"""Synthetic exports and offline API stubs exercise the local intake boundary."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from skald.__main__ import main
from skald.engine import Engine
from skald.ingest import Candidate, parse, sources
from skald.ingest.resolve import Resolver, remote_item, remote_provider
from skald.intake import collect, plan_hash, preview, write_private
from skald import remote

from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=True)
ANA, BEN = snapshot_people(SNAPSHOT)
FIXTURES = Path(__file__).parent / "fixtures/ingest"
COUNTS = {"plain": (2, 2, 0), "imdb": (3, 3, 0), "letterboxd": (5, 3, 2), "trakt": (3, 3, 0),
          "youtube": (2, 1, 1), "netflix": (2, 1, 1), "chrome": (5, 3, 2)}
REACT = {"facets": [], "rows": {}}


def fixture_specs(temp):
    root = Path(temp) / "exports"
    shutil.copytree(FIXTURES, root)
    history = root / "History"
    with sqlite3.connect(history) as db:
        db.executescript((root / "chrome.sql").read_text())
    return [("plain", root / "plain.txt"), ("imdb", root / "imdb-ratings.csv"),
            ("imdb", root / "imdb-watchlist.csv"), ("letterboxd", root / "letterboxd"),
            ("trakt", root / "trakt.json"), ("youtube", root / "watch-history.json"),
            ("netflix", root / "ViewingActivity.csv"), ("chrome", history)]


def api_reply(body, *, source="llm", why="llm", cost=.0008):
    return {"results": [{"answer": {"pick": None if source == "fallback" else
                                    {k: inp["choices"][0][k] for k in ("key", "kind")}},
                         "confidence": .99, "source": source, "why": why,
                         "receipt": "d_" + str(i), "cost_usd": cost} for i, inp in enumerate(body["items"])]}


def ambiguous(path, **kw):
    return {"results": [{"id": 1, "title": "The Office", "year": "2001", "kind": "tv"},
                        {"id": 2, "title": "The Office", "year": "2005", "kind": "tv"}]}


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), "set SKALD_SNAPSHOT_U to run snapshot gates")
class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()):
            cls.engine = Engine(SNAPSHOT)
        cls.server = cls.engine._server

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.specs = fixture_specs(self.temp.name)
        self.net = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.opener = patch("urllib.request.build_opener", side_effect=AssertionError("network forbidden"))
        self.opener.start()
        self.addCleanup(self.opener.stop)
        # These snapshot gates isolate paid decisions; the lookup API has synthetic transport tests.
        lookup = patch("skald.remote.resolve_titles", side_effect=lambda candidates: Resolver(
            self.server.tmdb, cache=self.state / "lookup.json").resolve(candidates, persist=False))
        lookup.start()
        self.addCleanup(lookup.stop)
        # Rank simulation itself has coverage in test_cli; these cases check intake/persistence.
        reaction = patch.object(Engine, "react", return_value=REACT)
        self.react = reaction.start()
        self.addCleanup(reaction.stop)

    def cli(self, *args, code=0, capture_stdout=False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            status = main(["--snapshot", str(SNAPSHOT), "--state-dir", str(self.state), "--json", *args],
                          out=None if capture_stdout else out)
        self.assertEqual(status, code, out.getvalue())
        self.assertNotIn("_persist", out.getvalue())
        return json.loads(out.getvalue())

    def onboard(self, *args, specs=None, code=0):
        return self.cli("onboard", "--person", BEN, "--non-interactive", "--profile-map", "Export A=" + BEN,
                        *[v for s, p in (self.specs if specs is None else specs) for v in ("--source", f"{s}={p}")],
                        *args, code=code)

    def test_parity_all_seven_and_multi_file_hash(self):
        result = self.onboard("--dry-run")
        rows = {r["name"]: r for r in result["sources"]}
        for source, expected in COUNTS.items():
            paths = [str(p) for s, p in self.specs if s == source]
            imported = self.cli("import", source, *paths, "--person", BEN, "--profile-map", "Export A=" + BEN, "--dry-run")
            actual = (imported["candidates"], len(imported["events"]), len(imported["review"]))
            self.assertEqual(actual, expected)
            self.assertEqual(tuple(rows[source][k] for k in ("candidates", "review")), (expected[0], expected[2]))
        self.assertEqual({s: r["accepted"] for s, r in rows.items()},
                         {"plain": 0, "imdb": 1, "letterboxd": 2, "trakt": 0, "youtube": 0, "netflix": 0, "chrome": 0})
        self.assertEqual(result["merge"]["merged"], 13)
        reversed_specs = collect(list(reversed(self.specs)), BEN, {"Export A": BEN}, server=self.server)
        self.assertEqual({s: h for s, h, _ in reversed_specs}, {s: r["file"] for s, r in rows.items()})
        self.assertEqual(sum(r["accepted"] for r in rows.values()), 3)
        self.assertIsNone(result["plan"])
        self.assertFalse(self.state.exists())

    def test_dry_run_remote_has_no_paid_preflight_or_writes(self):
        with patch("skald.remote.request", side_effect=AssertionError("remote forbidden")):
            result = self.onboard("--dry-run", "--remote")
            imported = self.cli("import", "plain", str(self.specs[0][1]), "--person", BEN, "--dry-run", "--remote")
        self.assertTrue(result["remote"]["enabled"])
        self.assertGreater(result["remote"]["pending"], 0)
        self.assertEqual(result["remote"]["sent"], 0)
        self.assertEqual(imported["remote"]["sent"], 0)
        self.assertFalse(self.state.exists())

    def test_resume_idempotence_again_and_undo(self):
        staged = self.onboard()
        result = self.onboard("--yes", "--plan", staged["plan"])
        self.assertEqual(result["added"], 3)
        path = self.state / "imports.jsonl"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        book = json.loads((self.state / "onboard.json").read_text())
        lb = set(book["people"][BEN]["letterboxd"]["added"])
        self.assertEqual(len(lb), 2)
        skipped = self.onboard("--yes", "--plan", staged["plan"])
        self.assertTrue(all(r["status"] == "skipped" for r in skipped["sources"]))
        self.assertEqual(skipped["added"], 0)
        self.assertEqual(len(json.loads((self.state / "review.json").read_text())), 6)
        again = self.onboard("--again", "--yes", "--plan", staged["plan"])
        self.assertEqual(again["added"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        before_events = [json.loads(line) for line in path.read_text().splitlines()]
        undone = self.cli("onboard", "--undo", "letterboxd", "--person", BEN)
        self.assertEqual(undone["mode"], "undo")
        self.assertEqual(set(json.loads((self.state / "undone.json").read_text())), lb)
        after = self.cli("imports", "list")["events"]
        self.assertEqual({e["id"] for e in after}, {e["id"] for e in before_events} - lb)
        self.assertTrue(all(e["src"] != "letterboxd" for e in after))
        self.assertEqual(self.cli("onboard", "--status", "--person", BEN)["sources"][2]["status"], "undone")
        for file in self.state.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_interrupt_after_two_sources_keeps_checkpoints(self):
        staged = self.onboard()
        original = Engine.ingest
        count = 0
        def interrupted(engine, events):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic interruption")
            return original(engine, events)
        with patch.object(Engine, "ingest", interrupted):
            self.onboard("--yes", "--plan", staged["plan"], code=2)
        book = json.loads((self.state / "onboard.json").read_text())["people"][BEN]
        self.assertEqual([s for s, r in book.items() if r["status"] == "applied"], ["plain", "imdb"])
        resumed = self.onboard("--yes", "--plan", staged["plan"])
        self.assertEqual([r["name"] for r in resumed["sources"] if r["status"] == "skipped"], ["plain", "imdb"])
        self.assertEqual(resumed["added"], 2)
        self.assertEqual(len(self.cli("imports", "list")["events"]), 3)

    def test_interruption_between_log_and_final_checkpoint_keeps_undo_ids(self):
        from skald.__main__ import persist
        staged = self.onboard(specs=self.specs[:1])
        def interrupted(engine, state, directive):
            persist(engine, state, directive)
            if directive["log"]:
                raise OSError("synthetic checkpoint interruption")
        with patch("skald.__main__.persist", side_effect=interrupted):
            self.onboard("--yes", "--plan", staged["plan"], specs=self.specs[:1], code=2)
        record = json.loads((self.state / "onboard.json").read_text())["people"][BEN]["plain"]
        self.assertEqual(record["status"], "previewed")
        self.assertEqual(len(record["added"]), 2)
        result = self.onboard("--yes", "--plan", staged["plan"], specs=self.specs[:1])
        self.assertEqual(result["added"], 0)
        self.cli("onboard", "--undo", "plain", "--person", BEN)
        self.assertEqual(self.cli("imports", "list")["events"], [])

    def test_subset_preview_keeps_other_sources_review(self):
        self.onboard()
        before = (self.state / "review.json").read_bytes()
        self.onboard(specs=self.specs[:1])
        self.assertEqual((self.state / "review.json").read_bytes(), before)

    def test_plan_binds_bytes_person_and_cached_choices(self):
        staged = self.onboard(specs=self.specs[:1])
        self.assertRegex(staged["plan"], r"^[0-9a-f]{16}$")
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        p = self.specs[0][1]
        p.write_bytes(p.read_bytes() + b"\n")
        refused = self.onboard("--yes", "--plan", staged["plan"], specs=self.specs[:1], code=2)
        self.assertEqual(refused, {"error": "plan changed; preview again"})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})
        self.assertEqual(self.onboard("--yes", specs=self.specs[:1], code=2)["error"], "non-interactive apply needs --yes --plan")
        p.write_bytes(p.read_bytes()[:-1])
        with patch("skald.remote.request", side_effect=AssertionError("no remote on apply")), \
                patch.object(Engine, "_offline_tmdb", side_effect=AssertionError("no search on apply")):
            self.assertEqual(self.onboard("--yes", "--plan", staged["plan"], "--remote", specs=self.specs[:1])["added"], 2)
        e = {"title": "A", "who": BEN, "id": "a" * 32, "t": "2026-10-11"}
        self.assertEqual(plan_hash(BEN, {"plain": "1"}, [e]), plan_hash(BEN, {"plain": "1"}, [{**e, "id": "b" * 32}]))
        self.assertNotEqual(plan_hash(BEN, {"plain": "1"}, [e]), plan_hash(ANA, {"plain": "1"}, [e]))

    def test_real_preview_and_both_person(self):
        events = [{"key": "tmdb:603", "title": "The Matrix", "year": "1999", "kind": "movie", "src": "plain",
                   "who": "both", "action": "love", "conf": 1., "n": 1}]
        preview(self.engine, events, "both")
        self.react.assert_called_with(events, None)
        self.react.reset_mock()
        self.assertIsNone(preview(self.engine, [], "both")["react"])
        self.react.assert_not_called()

    def test_stdout_is_one_document_including_server_noise(self):
        self.cli("import", "plain", str(self.specs[0][1]), "--person", BEN, "--dry-run", capture_stdout=True)
        self.cli("onboard", "--person", "both", "--source", f"plain={self.specs[0][1]}", "--dry-run", capture_stdout=True)
        self.cli("taste", "show", capture_stdout=True)

    def test_privacy_real_client_token_canary(self):
        self.opener.stop()
        bodies = []
        def open_local(req, timeout):
            body = json.loads(req.data) if req.data else None
            if body is not None:
                bodies.append(body)
            reply = {"types": [{"type": "resolve_title", "available": True}]} if body is None else api_reply(body)
            return io.BytesIO(json.dumps(reply).encode())
        token = "SKALD_TOKEN_CANARY_12"
        with patch.dict(os.environ, {"SKALD_API": "https://synthetic.invalid", "SKALD_TOKEN": token}), \
                patch("urllib.request.build_opener", return_value=Mock(open=open_local)):
            result = self.onboard("--remote")
            applied = self.onboard("--remote", "--yes", "--plan", result["plan"])
        self.assertGreater(applied["added"], 0)
        material = json.dumps(result) + "".join(p.read_text() for p in self.state.iterdir()) + json.dumps(bodies)
        for canary in ["Export A", "://", self.temp.name, token] + [p.name for p in (Path(self.temp.name) / "exports").rglob("*") if p.is_file()]:
            self.assertNotIn(canary, material)
        self.assertGreater(len(bodies), 0)
        for body in bodies:
            self.assertEqual(set(body), {"items", "no_llm"})
            self.assertLessEqual(len(json.dumps(body).encode()), 60000)
            for item in body["items"]:
                self.server.check(self.server.DECISIONS["resolve_title"]["inp"], item)

    def test_preflight_failure_zero_budget_and_remote_failure(self):
        with patch("skald.remote.request", return_value={"types": [{"type": "resolve_title", "available": False}]}) as req:
            result = self.onboard("--remote")
            self.assertGreater(result["remote"]["pending"], 0)
            self.assertEqual(result["remote"]["usd"], 0)
            self.assertEqual(req.call_count, 1)
        shutil.rmtree(self.state)
        with patch("skald.remote.request", side_effect=AssertionError("zero budget")):
            self.onboard("--remote", "--max-ai-usd", "0")
        shutil.rmtree(self.state)
        def fail(path, body=None):
            if body is None:
                return {"types": [{"type": "resolve_title", "available": True}]}
            raise ValueError("api 429")
        with patch("skald.remote.request", side_effect=fail):
            result = self.onboard("--remote")
        self.assertEqual(result["remote"]["batches"], 1)
        self.assertIn("api 429", result["remote"]["why"])

    def test_taint_and_server_rejection_pins(self):
        self.assertEqual(self.server.trust("utterance"), .8)
        self.assertTrue(self.server.tainted({"src": "agent:x"}))
        self.assertTrue(self.server.tainted({"src": "import:youtube"}))
        e = {"key": "tmdb:603", "title": "The Matrix", "kind": "movie", "who": BEN, "action": "seen", "conf": 1.}
        for src in ("agent:x", "utterance", "sync:letterboxd"):
            with self.assertRaisesRegex(ValueError, "invalid import src"):
                self.server.validate_import({**e, "src": src})
        rows = self.cli("onboard", "--sources")["sources"]
        self.assertEqual([r["name"] for r in rows if not r["usable"]], ["utterance", "agent:<client>"])

    def test_candidate_source_cannot_claim_other_trust(self):
        with patch("skald.intake.parse", return_value=[Candidate("The Matrix", src="trakt")]):
            with self.assertRaisesRegex(ValueError, "candidate source differs"):
                collect(self.specs[:1], BEN, {}, server=self.server)
        with self.assertRaisesRegex(ValueError, "unknown source"):
            parse("utterance", "unused")

    def test_tty_three_source_dry_run_and_json_prompts(self):
        selections = self.specs[:1] + self.specs[4:5] + self.specs[6:7]
        replies = "".join(f"{s}\n{p}\n" for s, p in selections) + "\nlater\n"
        stdin = io.StringIO(replies)
        with patch("sys.stdin", stdin), patch.object(stdin, "isatty", return_value=True):
            result = self.cli("onboard", "--person", BEN, "--profile-map", "Export A=" + BEN, "--dry-run")
        self.assertEqual([r["name"] for r in result["sources"]], ["plain", "trakt", "netflix"])
        self.assertEqual(sum(r["candidates"] for r in result["sources"]), 7)
        self.assertEqual(sum(r["accepted"] for r in result["sources"]), 3)
        self.assertFalse(self.state.exists())


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), "set SKALD_SNAPSHOT_U to run snapshot gates")
class RemoteResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()):
            cls.engine = Engine(SNAPSHOT)

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "resolve.json"
        self.candidates = [Candidate("The Office", kind="tv", ids={"other": i}) for i in range(26)]
        self.bodies = []
        def request(path, body):
            self.bodies.append(body)
            for inp in body["items"]:
                self.engine._server.check(self.engine._server.DECISIONS["resolve_title"]["inp"], inp)
            return api_reply(body)
        self.request = request

    def resolver(self, maximum=.05, request=None):
        return Resolver(ambiguous, remote_provider(request or self.request), self.cache, max_ai_usd=maximum)

    def test_26_batches_budget_and_fixed_server_reservation(self):
        resolver = self.resolver()
        self.assertEqual(resolver.batch_usd, self.engine._server.DECISIONS["resolve_title"]["max_usd"])
        results = resolver.resolve(self.candidates)
        self.assertEqual([len(b["items"]) for b in self.bodies], [25, 1])
        self.assertAlmostEqual(resolver.spent, .0208)
        self.assertTrue(all(r["method"] == "remote_llm" for r in results))
        self.cache.unlink()
        self.bodies.clear()
        rows = self.resolver(.03).resolve(self.candidates)
        self.assertEqual([len(b["items"]) for b in self.bodies], [25])
        self.assertEqual(rows[-1]["why"], "budget")
        self.cache.unlink()
        self.bodies.clear()
        self.resolver(0).resolve(self.candidates)
        self.assertEqual(self.bodies, [])

    def test_429_stops_all_batches_and_leaves_review(self):
        request = Mock(side_effect=ValueError("api 429"))
        rows = self.resolver(request=request).resolve(self.candidates)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(r["method"] == "unresolved" and r["why"] == "api 429" for r in rows))

    def test_fallback_retry_receipts_and_final_cache(self):
        def fallback(path, body):
            self.bodies.append(body)
            return api_reply(body, source="fallback", why="api_share", cost=0)
        rows = self.resolver(request=fallback).resolve(self.candidates[:1])
        self.assertEqual(rows[0]["method"], "unresolved")
        self.assertEqual(rows[0]["why"], "api_share")
        self.assertEqual(rows[0]["receipt"], "d_0")
        rows = self.resolver().resolve(self.candidates[:1])
        self.assertEqual(len(self.bodies), 2)
        self.assertEqual(rows[0]["method"], "remote_llm")
        final = self.resolver(request=Mock(side_effect=AssertionError("final cache")))
        self.assertEqual(final.resolve(self.candidates[:1]), rows)

    def test_invalid_pick_rejects_whole_batch_and_retries(self):
        def invalid(path, body):
            reply = api_reply(body)
            reply["results"][-1]["answer"]["pick"] = {"key": "tmdb:999", "kind": "tv"}
            return reply
        rows = self.resolver(request=invalid).resolve(self.candidates[:2])
        self.assertTrue(all(r["method"] == "unresolved" and r["why"] == "api response" for r in rows))
        self.assertTrue(all(r["method"] == "remote_llm" for r in self.resolver().resolve(self.candidates[:2])))

    def test_byte_splitting_and_null_cost_charge(self):
        choices = [{"id": i + 1, "title": "界" * 300, "year": "", "kind": "tv", "aliases": ["界" * 300] * 7}
                   for i in range(5)]
        batch = [{"candidate": {"title": "Office", "year": "", "kind": ""}, "choices": choices} for _ in range(25)]
        remote_provider(self.request)(batch, .02)
        self.assertGreater(len(self.bodies), 1)
        self.assertTrue(all(len(json.dumps(b).encode()) <= 60000 for b in self.bodies))
        self.assertTrue(all(len(x["aliases"]) == 5 for b in self.bodies for item in b["items"] for x in item["choices"]))
        def unknown(path, body):
            return api_reply(body, cost=None)
        self.cache.unlink(missing_ok=True)
        resolver = self.resolver(request=unknown)
        resolver.resolve(self.candidates[:2])
        self.assertAlmostEqual(resolver.spent, .04)

    def test_none_is_final_but_failure_is_not(self):
        def none(path, body):
            reply = api_reply(body, source="rule", cost=0)
            for r in reply["results"]:
                r["answer"] = {"pick": None}
            return reply
        rows = self.resolver(request=none).resolve(self.candidates[:1])
        self.assertEqual(rows[0]["method"], "none")
        self.resolver(request=Mock(side_effect=AssertionError("none should be final"))).resolve(self.candidates[:1])


class PrivateWriteTests(unittest.TestCase):
    def test_failed_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state/log"
            write_private(path, "old")
            with patch("skald.intake.os.replace", side_effect=OSError("interruption")):
                with self.assertRaises(OSError):
                    write_private(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(path.parent.iterdir()), [path])
