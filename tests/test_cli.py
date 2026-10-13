from skald.members import rid
from skald import members as row_config
import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import tarfile
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from skald.engine import DATA_FILES
from skald import Engine
from skald.__main__ import main, text_output
from skald import live
from skald.ingest import Candidate

from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=True)
ANA, BEN = snapshot_people(SNAPSHOT)
FIXTURES = Path(__file__).parent / "fixtures/ingest"


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), "set SKALD_SNAPSHOT_U to run snapshot gates")
class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.config = row_config.load_config(str(SNAPSHOT), {}, readonly=True)[0]
        self.before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for name in DATA_FILES if (p := SNAPSHOT / name).is_file()}
        self.net = patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.addCleanup(self.assert_snapshot_unchanged)

    def assert_snapshot_unchanged(self):
        self.assertEqual(self.before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for name in DATA_FILES if (p := SNAPSHOT / name).is_file()})

    def cli(self, *args, code=0):
        out = io.StringIO()
        status = main(["--snapshot", str(SNAPSHOT), "--state-dir", str(self.state), *args], out=out)
        self.assertEqual(status, code, out.getvalue())
        return json.loads(out.getvalue())

    def loved_title(self):
        with Engine(SNAPSHOT) as engine:
            entries = engine.profile()['entries']
            return next(e for e in entries if ANA in e.get('by', []) and e.get('signal') == 'loved' and engine._server._facets.get(e['key']))

    def test_show_why_rows_learn(self):
        profile = self.cli("taste", "show", "--person", ANA, "--json")
        self.assertGreater(profile["titles"], 5)
        self.assertTrue(profile["experiences"])
        self.assertEqual(sum(sum(c.values()) for c in profile["source_signals"].values()), profile["titles"])
        title = self.loved_title()
        why = self.cli("taste", "why", title["key"], "--person", ANA, "--json")
        self.assertEqual(why["key"], title["key"])
        self.assertTrue(why["reasons"])
        self.assertIn(BEN, why["per_person"])
        self.assertIn("anchors", why["facets"][0])
        self.assertIsNone(why["percentile"])
        rows = self.cli("rows", "--row", ANA, "--n", "3", "--json")
        self.assertEqual(rows["rows"][0]["id"], rid('', self.config) + ANA)
        self.assertEqual(len(rows["rows"][0]["titles"]), 3)
        self.assertIn("why", rows["rows"][0]["titles"][0])
        learn = self.cli("learn", "--json")
        self.assertGreater(learn["plays_logged"], 0)
        self.assertIn("hit_rate", next(iter(learn["rows"].values())))
        raw = json.dumps(learn)
        self.assertNotIn('"hour"', raw)
        self.assertNotIn('"at": 17', raw)
        self.assertFalse(self.state.exists())

    def test_react_loaded_title_and_no_writes(self):
        title = self.loved_title()
        data = self.cli("react", "loves", title['key'], "--person", ANA, "--json")
        self.assertFalse(data['writes'])
        self.assertTrue(data['facets'])
        self.assertFalse(self.state.exists())

    def test_react_reasons_and_validation(self):
        title = self.loved_title()['key']
        self.cli("react", "not_for_us", title, "--person", ANA, "--reason", "too slow", "--json")
        self.cli("react", "love", title, "--reason", "too slow", "--json", code=2)
        self.cli("rows", "--n", "0", "--json", code=2)
        self.cli("taste", "why", "No Such Synthetic Movie", "--json", code=2)
        rows = self.cli("rows", "--row", ANA, "--n", "1", "--json")
        key = rows["rows"][0]["titles"][0]["key"]
        disliked = self.cli("react", "not_for_us", key, "--person", ANA, "--json")
        removed = next(e for moves in disliked["rows"].values() for e in moves if e["key"] == key)
        self.assertIsNone(removed["after"])

    def test_import_dry_run_list_dedupe_undo_and_push_preview(self):
        data = self.cli("import", "trakt", str(FIXTURES / "trakt.json"), "--person", ANA, "--yes", "--dry-run", "--json")
        self.assertTrue(data["dry_run"])
        self.assertEqual(len(data["events"]), 3)
        self.assertFalse(self.state.exists())
        data = self.cli("import", "trakt", str(FIXTURES / "trakt.json"), "--person", ANA, "--yes", "--json")
        self.assertEqual(data["added"], 3)
        self.assertTrue((self.state / "imports.jsonl").exists())
        self.assertNotIn("http", (self.state / "imports.jsonl").read_text())
        self.assertEqual(self.cli("import", "trakt", str(FIXTURES / "trakt.json"), "--person", ANA, "--yes", "--json")["added"], 0)
        listed = self.cli("imports", "list", "--json")
        self.assertEqual(len(listed["events"]), 3)
        with patch.dict(os.environ, {"SKALD_API": "https://api.example", "SKALD_TOKEN": "fake"}), patch("subprocess.run", side_effect=AssertionError("preview must not SSH")):
            preview = self.cli("push", "imports", "--remote", "--json")
        self.assertEqual(preview["requests"], 0)
        self.assertEqual(preview["count"], 3)
        undone = self.cli("imports", "undo", listed["events"][0]["id"], "--json")
        self.assertEqual(undone["undone"]["id"], listed["events"][0]["id"])
        self.assertEqual(len(self.cli("imports", "list", "--json")["events"]), 2)

    def test_yes_skips_low_confidence_and_writes_private_review(self):
        candidates = [Candidate("The Office remake", "", "tv")]
        with patch("skald.__main__.parse", return_value=candidates):
            result = self.cli("import", "plain", "unused", "--person", ANA, "--yes", "--json")
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["review"]), 1)
        self.assertTrue((self.state / "review.json").is_file())

    def test_terminal_review_can_fix_to_an_exact_cached_id(self):
        with patch("skald.__main__.parse", return_value=[Candidate("The Matrix Remake", "1999", "movie")]), \
                patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="tmdb:603"), \
                redirect_stderr(io.StringIO()):
            result = self.cli("import", "plain", "unused", "--person", ANA, "--json")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["events"][0]["key"], "tmdb:603")
        self.assertEqual(result["events"][0]["conf"], 1)

    def test_netflix_person_map_does_not_escape(self):
        result = self.cli("import", "netflix", str(FIXTURES / "ViewingActivity.csv"), "--person", BEN, "--profile-map", "Export A=" + ANA, "--yes", "--json")
        self.assertTrue(all(e["who"] == ANA for e in result["events"]))
        self.assertTrue(any(e["title"] == "Castle" and e["n"] == 3 for e in result["events"]))
        self.assertNotIn("Export A", json.dumps(result))
        self.assertNotIn("Export A", (self.state / "resolve.json").read_text())

    def test_eval_wrap_and_global_json(self):
        report = self.cli("--json", "eval", "--protocols", "p4", "--variants", "current")
        self.assertIn("p4", report["protocols"])
        self.assertTrue(report["metadata"]["universe_n"] > 1000)

    def test_facade_contract_and_no_internal_types(self):
        with Engine(SNAPSHOT) as engine:
            before = copy.deepcopy(engine.profile())
            detached = engine.profile()
            detached["entries"][0]["by"].append("untrusted")
            self.assertEqual(before, engine.profile())
            title = self.loved_title()
            for value in (engine.profile(BEN), engine.score(title["key"], ANA), engine.explain(title["key"]),
                          engine.rows(), engine.learn()):
                self.assertIsInstance(value, dict)
                json.dumps(value, allow_nan=False)
            engine.react([{**{k: title[k] for k in ("key", "kind", "title", "year")}, "action": "love"}], ANA)
            self.assertEqual(before, engine.profile())

    def test_facade_ingest_and_live_history_fallback(self):
        work = Path(self.temp.name) / "snapshot"
        work.mkdir()
        for name in live.DATA_FILES:
            if (SNAPSHOT / name).is_file() and name != "profile.json":
                shutil.copyfile(SNAPSHOT / name, work / name)
        with Engine(work) as engine:
            self.assertGreater(engine.profile()["titles"], 300)
            self.assertTrue(any(ANA in e["by"] for e in engine.profile()["entries"]))
            result = engine.ingest([{"key": "tmdb:1415", "kind": "tv", "title": "Elementary", "year": "2012",
                                    "who": ANA, "action": "love", "src": "trakt", "conf": 1, "n": 1}])
            self.assertEqual(result["added"], 1)
            self.assertTrue((work / "imports.jsonl").is_file())
            self.assertTrue(any(e["key"] == "tmdb:1415" for e in engine.profile(ANA)["entries"]))
        self.assertFalse((work / "profile.json").exists())

    def test_online_import_is_explicit_and_provider_stubbed(self):
        response = json.dumps({"results": [{"id": 603, "title": "The Matrix", "release_date": "1999-01-01", "media_type": "movie", "popularity": 100}]}).encode()
        with patch.dict("os.environ", {"TMDB_API": "synthetic-key", "OPENROUTER_API_KEY": ""}), \
                patch("urllib.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(response)) as transport:
            result = self.cli("import", "plain", str(FIXTURES / "plain.txt"), "--person", BEN, "--yes", "--resolve-online", "--dry-run", "--json")
        self.assertEqual(result["resolution"], "TMDB search")
        self.assertTrue(transport.called)
        self.assertEqual(result["events"][0]["key"], "tmdb:603")
        self.assertFalse(self.state.exists())


class TextTests(unittest.TestCase):
    def test_scores_are_three_decimals_without_changing_json(self):
        value = .12345678
        facet = {"name": "Comfort", "affinity": value, "anchors": [], "delta": value}
        cases = [
            ("taste", "show", {"person": None, "titles": 1, "sources": {}, "experiences": [facet],
                               "themes": [], "creators": [], "signals": {}, "source_signals": {}}),
            ("taste", "why", {"title": "Castle", "score": value, "percentile": None, "confidence": None,
                              "calibration": "snapshot", "reasons": [], "facets": [facet],
                              "per_person": {"ana": {"score": value, "percentile": None}}}),
            ("react", None, {"simulation": "preview", "rows": {}, "top50": [], "facets": [facet]}),
            ("learn", None, {"plays_logged": 1, "tired": 0, "tired_titles": [],
                             "rows": {rid('week', row_config.CONFIG): {"shown": 1, "hits": value, "hit_rate": value}},
                             "recent_hits": [{"title": "Castle", "row": rid('week', row_config.CONFIG), "share": value}]})]
        for command, taste, data in cases:
            before, out = json.dumps(data), io.StringIO()
            text_output(SimpleNamespace(command=command, taste=taste), data, out)
            self.assertIn("0.123", out.getvalue())
            self.assertNotIn(str(value), out.getvalue())
            self.assertEqual(json.dumps(data), before)
            if taste == "why":
                self.assertIn("percentile n/a (offline)", out.getvalue())


class LiveTests(unittest.TestCase):
    def setUp(self):
        host = patch.dict(os.environ, {'SKALD_SSH_HOST': 'synthetic-host.test'})
        host.start()
        self.addCleanup(host.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def archive(self, members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, content in members.items():
                raw = content.encode()
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        return buffer.getvalue()

    def test_fixed_commands_and_snapshot_pull(self):
        command = live.command("snapshot")
        self.assertEqual(command[:4], ["ssh", "-o", "BatchMode=yes", "synthetic-host.test"])
        self.assertNotIn("hydra.json", command[-1])
        self.assertNotIn("remote.json", command[-1])
        self.assertNotIn(".env", command[-1])
        with self.assertRaises(ValueError):
            live.command("ls /secrets")
        members = {name: "{}" for name in ("profile.json", "emb.json", "facets.json")}
        run = Mock(return_value=subprocess.CompletedProcess(command, 0, self.archive(members), b""))
        destination = live.pull(Path(self.temp.name) / "live", run=run)
        self.assertEqual((destination / "profile.json").read_text(), "{}")
        run.assert_called_once_with(command, check=True, capture_output=True, timeout=60)

    def test_archive_rejects_secrets_traversal_and_links(self):
        for name in ("hydra.json", "remote.json", "../profile.json"):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, self.archive({name: "private"}), b""))
            with self.assertRaises(ValueError):
                live.pull(Path(self.temp.name) / "live", run=run)
        self.assertFalse((Path(self.temp.name) / "profile.json").exists())

    def test_snapshot_pull_preserves_member_outputs_and_removes_stale_ones(self):
        files = {name: '{}' for name in ('profile.json', 'emb.json', 'facets.json',
                                         'trakt-ana.history.json', 'trakt-ben.history.json')}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, self.archive(files), b''))
        destination = live.pull(Path(self.temp.name)/'live', run=run)
        self.assertEqual((destination/'trakt-ben.history.json').read_text(), '{}')
        del files['trakt-ben.history.json']
        run.return_value = subprocess.CompletedProcess([], 0, self.archive(files), b'')
        live.pull(destination, run=run)
        self.assertFalse((destination/'trakt-ben.history.json').exists())
        self.assertIn('trakt-*.history.json', live.command('snapshot')[-1])

    def test_snapshot_pull_accepts_personal_history_without_legacy_history(self):
        files = {name: '{}' for name in ('skald.json', 'emb.json', 'facets.json', 'trakt-ben.history.json')}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, self.archive(files), b''))
        destination = live.pull(Path(self.temp.name)/'live', run=run)
        self.assertTrue((destination/'trakt-ben.history.json').is_file())

    def test_snapshot_export_excludes_member_output_credential_aliases_and_symlinks(self):
        data = Path(self.temp.name)/'data'
        data.mkdir()
        credentials = data/'credentials'
        credentials.mkdir(mode=0o700)
        credential = credentials/'private.token'
        credential.write_text('FAKE_SNAPSHOT_CREDENTIAL_CANARY')
        os.link(credential, data/'trakt-ben.history.json')
        for name in (*live.DATA_FILES, 'trakt-cy.history.json'):
            (data/name).write_text('{}')
        (data/'.skald-format').write_text(json.dumps({'format': 1, 'release': 'test'}))
        (data/'skald.json').write_text(json.dumps({'v': 1, 'household': {'members': ['ana', 'ben', 'cy']}}))
        def local_export(command, **kwargs):
            self.assertEqual(command[0], 'ssh')
            script = command[-1].replace('/opt/skald/app', str(Path(__file__).resolve().parents[1]))
            script = script.replace('python3', sys.executable).replace('--ignore-failed-read', '')
            return subprocess.run(['sh', '-c', script], env={**os.environ, 'COPYFILE_DISABLE': '1'}, **kwargs)
        with patch.object(live, 'REMOTE_DATA', str(data)):
            destination = live.pull(Path(self.temp.name)/'live', run=local_export)
        self.assertFalse((destination/'trakt-ben.history.json').exists(), 'credential alias exported')
        self.assertTrue((destination/'trakt-cy.history.json').is_file())
        (data/'trakt-ana.history.json').symlink_to(credential)
        with patch.object(live, 'REMOTE_DATA', str(data)):
            live.pull(destination, run=local_export)
        self.assertFalse((destination/'trakt-ana.history.json').exists(), 'symlink exported')

    def test_push_preview_and_merge_with_stubbed_subprocess(self):
        validate = lambda e: e
        event = {"id": "a" * 32, "t": "2026-10-11", "key": "tmdb:1419", "kind": "tv", "title": "Castle", "year": "2009", "who": "ana", "action": "love", "src": "trakt", "conf": 1, "n": 1}
        other = {**event, "id": "b" * 32, "key": "tmdb:603"}
        run = Mock(side_effect=[subprocess.CompletedProcess([], 0, (json.dumps(other) + "\n").encode(), b""), subprocess.CompletedProcess([], 0, b"", b"")])
        self.assertEqual(live.push([event], validate, run=run)["count"], 1)
        run.assert_not_called()
        with patch("subprocess.run", run):
            result = live.push([event], validate, apply=True)
        self.assertEqual(result["uploaded"], 2)
        command = run.call_args.args[0]
        self.assertEqual(command, live.command("push"))
        self.assertIn("#65534", command[-1])
        self.assertIn("chmod 600", command[-1])
        self.assertIn("imports.jsonl.bak-", command[-1])
        self.assertNotIn("docker", command[-1])
        upload = run.call_args.kwargs["input"]
        self.assertIn(b'"key": "tmdb:1419"', upload)

    def test_push_failure_does_not_report_success(self):
        run = Mock(side_effect=subprocess.CalledProcessError(1, ["ssh"]))
        with self.assertRaises(subprocess.CalledProcessError):
            live.push([], lambda e: e, apply=True, run=run)

    def test_push_undo_does_not_resurrect_remote_event(self):
        event = {"id": "a" * 32, "t": "2026-10-11"}
        run = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(event).encode(), b""), subprocess.CompletedProcess([], 0, b"", b"")])
        result = live.push([], lambda e: e, apply=True, run=run, undone=[event["id"]])
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"imports": [], "undo_ids": [event["id"]]})


if __name__ == "__main__":
    unittest.main()
