"""Household resolution, entrypoints and first-run rows are entirely offline."""
from skald.members import rid
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfo

from decide_kit import close_server, load_server

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.s = load_server(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        close_server(cls.s)
        cls.temp.cleanup()

    def resolve(self, data, env=None):
        return self.s.load_config(str(data), env or {})

    def write(self, data, cfg=None):
        cfg = cfg or self.s.fresh_config(["ana", "ben"])
        Path(data, "skald.json").write_text(json.dumps(cfg))
        return cfg

    def test_file_and_unknown_keys_configured_connectors(self):
        with tempfile.TemporaryDirectory() as data:
            cfg = self.s.fresh_config(["ana", "ben"])
            cfg.update(extra={"keep": True}, connectors={"reserved": {"kind": "api", "enabled": False, "auth": "env:RESERVED"}})
            cfg["household"]["extra"] = "keep"
            self.write(data, cfg)
            actual, source = self.resolve(data)
            self.assertEqual((actual, source), (cfg, "file"))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(self.resolve(data, {"SKALD_MEMBERS": "cy,dex"})[0], cfg)

    def test_override_path(self):
        with tempfile.TemporaryDirectory() as data:
            path = Path(data, "custom.json")
            cfg = self.s.fresh_config(["ana"])
            path.write_text(json.dumps(cfg))
            self.assertEqual(self.resolve(data, {"SKALD_CONFIG": str(path)}), (cfg, "file"))

    def test_five_and_six_members_load_scopes_labels_prompts_and_rotation(self):
        people = ['ana', 'ben', 'cy', 'dex', 'eli', 'flo']
        for count, word in ((5, 'five'), (6, 'six')):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as data:
                self.write(data, self.s.fresh_config(people[:count]))
                s = load_server(data)
                try:
                    self.assertIsNone(s.CONFIG_ERROR)
                    self.assertEqual(s.MEMBER_COUNT, word)
                    self.assertEqual(set(s.SCOPES), set(people[:count]) | {'together'})
                    self.assertEqual(s.PAIR_LABEL, 'Everyone')
                    self.assertTrue(all(s.manifest(s.LANG, p)['catalogs'] for p in (*people[:count], 'together')))
                    self.assertTrue(all('data-who="'+p+'"' in s.REMOTE_HTML for p in people[:count]))
                    self.assertIn('event.by.length === '+str(count), s.REMOTE_JS)
                finally:
                    close_server(s)

    def test_larger_household_profile_text_uses_household_phrasing(self):
        with tempfile.TemporaryDirectory() as data:
            self.write(data, self.s.fresh_config(['ana', 'ben', 'cy', 'dex', 'eli', 'flo']))
            s = load_server(data)
            try:
                profile = {'entries': [{'who': 'tv', 'by': ['together'], 'title': 'Shared movie', 'year': '2020',
                    'signal': 'watched', 'kind': 'movie'}], 'genres': {}, 'searches': []}
                text = s.profile_text(profile)
                self.assertIn('Recently on the shared TV (everyone in the household):', text)
            finally:
                close_server(s)

    def test_legacy_data_config_and_missing_fail_closed(self):
        with tempfile.TemporaryDirectory() as data:
            Path(data, "history.json").write_text('{}')
            with self.assertRaisesRegex(self.s.ConfigError, "legacy.json"):
                self.resolve(data)
            cfg = self.s.fresh_config(["eve", "fin"])
            cfg["household"]["note"] = "Synthetic household"
            path = Path(data, "legacy.json")
            path.write_text(json.dumps(cfg))
            self.assertEqual(self.resolve(data), (cfg, "legacy"))
            s = load_server(data)
            try:
                self.assertEqual(s.PEOPLE, ("eve", "fin"))
                self.assertEqual(s.manifest(s.LANG)["name"], cfg["addon"]["name"])
            finally:
                close_server(s)
            for raw in ('{', '{}', '{"v": NaN}'):
                path.write_text(raw)
                with self.assertRaisesRegex(self.s.ConfigError, "legacy.json"):
                    self.resolve(data)

    def test_legacy_existing_files_in_memory_only(self):
        for name in ("profile", "history", "trakt", "feeds", "taste"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as data:
                Path(data, name + ".json").write_text('{}')
                Path(data, "legacy.json").write_text(json.dumps(self.s.LEGACY))
                cfg, source = self.resolve(data)
                self.assertEqual((cfg, source), (self.s.LEGACY, "legacy"))
                self.assertFalse(Path(data, "skald.json").exists())

    def test_implicit_import_entrypoint_refuses(self):
        with tempfile.TemporaryDirectory() as data:
            cfg, source = self.resolve(data)
            self.assertEqual((cfg, source), (self.s.LEGACY, "implicit"))
            self.assertEqual(list(Path(data).iterdir()), [])
            with self.assertRaisesRegex(ValueError, "SKALD_MEMBERS"):
                self.s.require_config(source)
            s = load_server(data)
            try:
                self.assertEqual(s.CONFIG_SOURCE, "implicit")
            finally:
                close_server(s)
            result = self.serve(data, {"TMDB_API": "offline"})
            self.assertEqual(result.returncode, 2)
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("SKALD_MEMBERS", result.stderr)

    def test_env_once_atomic_permissions_and_no_owner_taste(self):
        with tempfile.TemporaryDirectory() as data:
            env = {"SKALD_MEMBERS": "ana,ben", "SKALD_LANG": "en-US", "SKALD_TZ": "UTC", "SKALD_COUNTRY": "Canada"}
            cfg, source = self.resolve(data, env)
            path = Path(data, "skald.json")
            self.assertEqual(source, "env")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), cfg)
            self.assertEqual(cfg["taste"]["household"], {"likes": "", "rewatch_collections": []})
            self.assertEqual(cfg["household"]["archived"], {})
            self.assertEqual(cfg["household"]["member_rows"], [])
            self.assertEqual(cfg["addon"]["name"], "Skald")
            before = path.read_bytes()
            errors = io.StringIO()
            with redirect_stderr(errors):
                later, source = self.resolve(data, {**env, "SKALD_MEMBERS": "cy,dex", "SKALD_TZ": "Europe/London"})
            self.assertEqual((later, source), (cfg, "file"))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(errors.getvalue().splitlines(), ["skald.json: household environment ignored"])
            self.assertEqual({p.name for p in Path(data).iterdir()}, {"skald.json", ".skald-household"})
            self.assertEqual(Path(data, ".skald-household").read_bytes(), b"")

    def test_invalid_members(self):
        for members in ([], ["a"], ["Ana"], ["ana", "ana"], ["with-dash"], ["a" * 17],
                        ["guest"], ["both"], ["together"], ["household"], ["aa", "bb", "cc", "dd", "ee", "ff", "gg"], [42]):
            with self.subTest(members=members), self.assertRaisesRegex(ValueError, "household.members"):
                self.s.validate_config({"v": 1, "household": {"members": members}})

    def test_file_auth_reserved_names_are_shared_with_config_only_cli(self):
        from skald import members
        expected = {"addon.json", "skald.json", "legacy.json", "clients.json", "remote.json", ".skald-household", ".skald-format"}
        self.assertEqual(set(self.s.RESERVED_DATA_FILES), expected)
        self.assertEqual(set(members.RESERVED_DATA_FILES), expected)
        self.assertIn('legacy.json', self.s.CONNECTOR_DENY_FILES)
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['connectors']['jellyfin'] = {'kind': 'api', 'enabled': True,
            'auth': 'file:legacy.json', 'url': 'http://jellyfin:8096', 'user_id': 'synthetic'}
        with self.assertRaises(self.s.ConfigError):
            self.s.validate_config(cfg)


    def test_invalid_known_keys_without_values(self):
        bad = [("v", 2), ("household.labels", {}), ("household.note", 1), ("household.archived", {"ana": "A"}),
               ("household.trakt_member", "private-value"), ("household.member_rows", ["cy"]),
               ("household.experiences", {"cy": []}), ("taste", []), ("taste.household.likes", []),
               ("taste.household.rewatch_collections", [True]), ("taste.people", {"cy": {}}),
               ("region.lang", "private-value"), ("region.tz", "private-value"), ("region.country", 3),
               ("addon.id", "private value"), ("addon.name", ""), ("addon.version", ""), ("addon.legacy_open", 1)]
        for key, value in bad:
            with self.subTest(key=key):
                cfg = self.s.fresh_config(["ana", "ben"])
                node = cfg
                parts = key.split('.')
                for part in parts[:-1]:
                    node = node[part]
                node[parts[-1]] = value
                with self.assertRaises(ValueError) as err:
                    self.s.validate_config(cfg)
                self.assertEqual(str(err.exception), "invalid config: " + key)

    def test_malformed_json_and_invalid_entrypoints(self):
        with tempfile.TemporaryDirectory() as data:
            Path(data, "skald.json").write_text('{private-value')
            with self.assertRaisesRegex(ValueError, "invalid config: skald.json"):
                self.resolve(data)
            for argv in ([str(ROOT / "server.py")], ["-m", "skald", "rows"]):
                result = self.process(data, argv)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertIn("skald.json", result.stderr)
                self.assertNotIn("private-value", result.stderr)
            cfg = self.s.fresh_config(["ana"])
            cfg["region"]["tz"] = "private-value"
            self.write(data, cfg)
            result = self.serve(data)
            self.assertEqual(result.stderr, "invalid config: region.tz\n")
            Path(data, "skald.json").write_text('{"v": 1, "household": {"members": ["ana"]}, "extra": NaN}')
            self.assertEqual(self.serve(data).stderr, "invalid config: skald.json\n")

    def process(self, data, argv, extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SKALD_") and k != "TMDB_API"}
        env.update(DATA_DIR=str(data), PYTHONDONTWRITEBYTECODE="1", **(extra or {}))
        return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, capture_output=True, text=True)

    def serve(self, data, env=None, flags=()):
        return self.process(data, [str(ROOT / "server.py"), *flags], env)

    def test_missing_api_and_print_config(self):
        with tempfile.TemporaryDirectory() as data:
            cfg = self.write(data)
            result = self.serve(data)
            self.assertEqual((result.returncode, result.stderr), (2, "missing TMDB_API\n"))
            result = self.serve(data, {"TMDB_API": "private-value", "OPENROUTER_API_KEY": "private-value"}, ["--print-config"])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), cfg)
            self.assertNotIn("private-value", result.stdout)

            cfg["api_key"] = "private-value"
            for section in ("household", "region", "addon"):
                cfg[section]["token"] = "private-value"
            cfg["household"]["labels"]["token"] = "private-value"
            cfg["taste"]["people"]["ana"]["token"] = "private-value"
            self.write(data, cfg)
            result = self.serve(data, flags=["--print-config"])
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("private-value", result.stdout)

    def test_direct_cli_never_initializes_or_stamps_household(self):
        for configured in (False, True):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as data:
                if configured:
                    self.write(data)
                before = {p.name: p.read_bytes() for p in Path(data).iterdir()}
                result = self.serve(data, {"SKALD_MEMBERS": "ana,ben"}, ["--print-config"])
                self.assertEqual(result.returncode, 0)
                self.assertEqual({p.name: p.read_bytes() for p in Path(data).iterdir()}, before)

    def test_recovery_commands_report_invalid_config_and_backup_still_runs(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as state:
            for raw, error in (('{bad', 'invalid config: skald.json'), ('{"v":999}', 'invalid config: v')):
                Path(data, "skald.json").write_text(raw)
                result = self.process(data, ["-m", "skald", "--json", "backup", "list", "--data", data])
                self.assertEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["archives"], [])
                self.assertEqual(report["config_error"], error)
                doctor = self.process(data, ["-m", "skald", "--snapshot", data, "--state-dir", state, "--json", "doctor"])
                report = json.loads(doctor.stdout)
                self.assertEqual(doctor.returncode, 1)
                self.assertTrue(any(f["message"] == error for f in report["findings"]))
                login = self.process(data, ["-m", "skald", "--json", "trakt", "login", "--member", "ana"])
                self.assertEqual((login.returncode, json.loads(login.stdout)["error"]), (2, error))
                self.assertFalse(Path(data, ".skald-household").exists())

    def test_legacy_constants(self):
        legacy = self.s.LEGACY
        self.assertEqual(legacy["household"]["members"], ["ana", "ben"])
        self.assertEqual(legacy["household"]["labels"], {"ana": "Ana", "ben": "Ben", "pair": "you both"})
        self.assertEqual(legacy["household"]["trakt_member"], "ben")
        self.assertEqual(legacy["household"]["member_rows"], ["ana"])
        self.assertEqual(legacy["household"]["archived"], {"cal": "Cal"})
        self.assertEqual(legacy["household"]["experiences"], {})
        self.assertEqual(legacy["household"]["note"], "")
        self.assertEqual(legacy["taste"]["household"], {"likes": "", "rewatch_collections": []})
        self.assertEqual(legacy["taste"]["cal"], {"likes": "", "archived": True})
        self.assertEqual(legacy["taste"]["people"], {"ben": {"type": ""}, "ana": {"type": "", "loves": []}})
        self.assertEqual(legacy["region"], self.s.DEFAULT_REGION)
        self.assertEqual(legacy["addon"], {"id": "community.skald.legacy", "name": "TMDB Search", "version": "1.7.3", "legacy_open": True})
        self.assertEqual(self.s.DEFAULT_TASTE, legacy["taste"])
        self.assertEqual(self.s.PEOPLE, tuple(legacy["household"]["members"]))

    def test_memory_why_one_three_four_members(self):
        for count in (1, 2, 3, 4):
            people = ["ana", "ben", "cy", "dex"][:count]
            with self.subTest(count=count), tempfile.TemporaryDirectory() as data:
                self.write(data, self.s.fresh_config(people))
                s = load_server(data)
                try:
                    self.assertEqual(s.PEOPLE, tuple(people))
                    index = {p: {"x:cozy_case": [(1., i, f"f_{p}", 100., 100., "told", True)]} for i, p in enumerate(people)}
                    with patch.object(s, "_memory_read", return_value={"seq": 1}), \
                            patch.object(s, "_memory_index", {"seq": 1, "people": index}), \
                            patch.object(s, "frec", return_value={"x": {"cozy_case": 2}}), \
                            patch.object(s, "get_model", return_value={"names": {"x:cozy_case": "cozy"}}):
                        out = s.memory_why("tmdb:1", "movie", "together", now=100.)
                        self.assertIsNotNone(out)
                        self.assertEqual(out["fact"], "f_ana")
                        self.assertEqual(out["strength"], 1.)
                finally:
                    close_server(s)

    def test_configured_rows_scopes_labels_and_connectors(self):
        with tempfile.TemporaryDirectory() as data:
            cfg = self.s.fresh_config(["ana", "ben", "cy"])
            cfg["household"]["archived"] = {"old": "Old"}
            cfg["household"].update(labels={"ana": "Ana Maria", "ben": "Benny", "cy": "Cee", "pair": "all of you"},
                                    trakt_member="ben", member_rows=["ana", "cy"], experiences={"ana": ["whodunit"]})
            self.write(data, cfg)
            s = load_server(data)
            try:
                self.assertEqual(s.PEOPLE, ("ana", "ben", "cy"))
                self.assertEqual(s.active_profiles(), ["household"])
                self.assertEqual(s.read_taste()["old"], {"likes": "", "archived": True})
                Path(data, "taste.json").write_text(json.dumps({"old": {"archived": False}}))
                self.assertEqual(s.active_profiles(), ["household", "old"])
                self.assertEqual(s.SCOPES, {"ana": "ana", "ben": "ben", "cy": "cy", "together": "both"})
                self.assertEqual(s.ROW_OWNER, {rid('ana', s.CONFIG): "ana", rid('cy', s.CONFIG): "cy"})
                rows = {cid: name for cid, _, name in s.feed_rows()}
                self.assertEqual(rows[rid('week', s.CONFIG)], "✨ This Week for Ana Maria & Benny & Cee")
                self.assertEqual(rows[rid('ana', s.CONFIG)], "For Ana Maria")
                with patch.object(s, "pin_shown"):
                    self.assertEqual(s.manifest(s.LANG, "ana")["name"], "Skald · Ana Maria")
                    self.assertEqual(s.manifest(s.LANG, "together")["name"], "Skald · Together")
                self.assertEqual(s.connector_seeds(1)["trakt"]["person"], "ben")
                self.assertEqual(s.who_label(["ana"]), " (Ana Maria)")
                self.assertEqual(s.who_label(["ben"]), "")
                self.assertNotIn("unused_member", s.REMOTE_JS)
                self.assertIn('"ana": "Ana Maria"', s.REMOTE_JS)
                self.assertEqual(set(s.GUEST_ROW_POLICY), set(s.DEDUPE_ORDER))
                self.assertTrue(set(s.ROW_OWNER) <= s.MIXED & s.REMOTE_ROWS)
                self.assertTrue(s.member_pick({"id": "tmdb:1", "type": "movie", "description": "For Ana Maria: mystery"}, "ana"))
            finally:
                close_server(s)

    def test_config_only_engine_and_cli_default_lang(self):
        from skald.engine import Engine
        from skald.__main__ import main
        from skald import members
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as state:
            cfg = self.write(data)
            with Engine(data) as e:
                self.assertEqual(e._server.CONFIG, cfg)
                self.assertEqual(e.rows()["lang"], "en-US")
            out = io.StringIO()
            try:
                self.assertEqual(main(["--snapshot", data, "--state-dir", state, "rows", "--json"], out), 0)
                self.assertEqual(json.loads(out.getvalue())["lang"], "en-US")
            finally:
                members.configure()

    def test_failed_empty_build_retry_and_partial_success(self):
        s = self.s
        retry, calls = {}, []
        def build(lang):
            calls.append(lang)
            def swallowed():
                for call in (lambda: s.tmdb("synthetic"), lambda: s.anilist("synthetic", {})):
                    try:
                        call()
                    except OSError:
                        pass
            s._outer.submit(lambda: s._pool.submit(swallowed).result()).result()
            self.assertEqual(s._upstream.failures[0], 2)
            return {rid('top10', s.CONFIG): {"movie": [], "series": []}}
        specs = [("top10", [rid('top10', s.CONFIG)], "today", 0, build)]
        with patch.object(s, "refresh_profiles", return_value={}), patch.object(s, "_langs", {"en-US"}), \
                patch.object(s, "_feeds", {}), patch.object(s, "job_specs", return_value=specs), \
                patch.object(s, "pin_decision"), patch.object(s, "save_feeds"), \
                patch("urllib.request.urlopen", side_effect=OSError("offline")), patch("time.time", return_value=1000.):
            s.feed_pass(s.local_now(), retry)
            self.assertEqual(retry, {("top10", "en-US"): 1600.})
            self.assertEqual(s._feeds, {})
            s.feed_pass(s.local_now(), retry)
            self.assertEqual(calls, ["en-US"])
            with patch("time.time", return_value=1600.):
                s.feed_pass(s.local_now(), retry)
            self.assertEqual(calls, ["en-US", "en-US"])
            retry.clear()
            specs[0] = ("top10", [rid('top10', s.CONFIG)], "today", 0,
                        lambda lang: (s.upstream_failed(), {rid('top10', s.CONFIG): {"movie": [{"id": "tmdb:1"}]}})[1])
            s.feed_pass(s.local_now(), retry)
            self.assertEqual(s._feeds[rid('top10', s.CONFIG), "en-US"]["stamp"], "v" + s.STAMP_V + "/today")
            s._feeds.clear()
            specs[0] = ("top10", [rid('top10', s.CONFIG)], "today", 0, lambda lang: {rid('top10', s.CONFIG): {"movie": []}})
            s.feed_pass(s.local_now(), retry)
            self.assertIn((rid('top10', s.CONFIG), "en-US"), s._feeds)
            self.assertIsNone(getattr(s._upstream, "failures", None))

    def test_berlin_rule_zoneinfo_hourly_2026_to_2030(self):
        s = self.s
        zone = ZoneInfo("Europe/Berlin")
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2031, 1, 1, tzinfo=dt.timezone.utc)
        with patch.object(s, "TZ", "Europe/Berlin"):
            while start < end:
                self.assertEqual(s.local_now(start.timestamp()), start.astimezone(zone).replace(tzinfo=None))
                start += dt.timedelta(hours=1)

    def test_other_zone(self):
        at = dt.datetime(2026, 10, 11, 16, tzinfo=dt.timezone.utc)
        for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
            with self.subTest(zone=zone), patch.object(self.s, "TZ", zone):
                self.assertEqual(self.s.local_now(at.timestamp()), at.astimezone(ZoneInfo(zone)).replace(tzinfo=None))

    def test_lost_config_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as data:
            self.resolve(data, {"SKALD_MEMBERS": "ana,ben"})
            Path(data, "skald.json").unlink()
            for env in ({}, {"SKALD_MEMBERS": "ana,ben"}):
                with self.assertRaises(self.s.ConfigError) as err:
                    self.resolve(data, env)
                self.assertEqual(str(err.exception), "skald.json missing: restore it")
            for argv in ([str(ROOT / "server.py")], ["-m", "skald", "rows"]):
                result = self.process(data, argv)
                self.assertEqual((result.returncode, result.stderr), (2, "skald.json missing: restore it\n" if len(argv) == 1 else "skald: skald.json missing: restore it\n"))
            Path(data, ".skald-household").unlink()
            Path(data, "history.json").write_text('{}')
            with self.assertRaisesRegex(self.s.ConfigError, "skald.json missing: restore it"):
                self.resolve(data, {"SKALD_MEMBERS": "ana,ben"})

    def test_file_config_loss_requires_restore_and_owner_migration_is_zero_touch(self):
        with tempfile.TemporaryDirectory() as data:
            history = Path(data, "history.json")
            history.write_text('{}')
            Path(data, 'legacy.json').write_text(json.dumps(self.s.LEGACY))
            before = {p.name: p.read_bytes() for p in Path(data).iterdir()}
            owner = self.serve(data, flags=["--print-config"])
            self.assertEqual(owner.returncode, 0)
            self.assertEqual({p.name: p.read_bytes() for p in Path(data).iterdir()}, before)
            self.write(data)
            configured = self.serve(data, flags=["--print-config"])
            self.assertEqual(configured.returncode, 0)
            stamp = Path(data, ".skald-household")
            self.assertFalse(stamp.exists(), "CLI must not stamp file-configured DATA")
            self.resolve(data)
            self.assertTrue(stamp.is_file(), "explicit configuration stamps the household")
            self.assertEqual(stamp.read_bytes(), b"")
            inode = stamp.stat().st_ino
            self.assertEqual(self.serve(data, flags=["--print-config"]).returncode, 0)
            self.assertEqual(stamp.stat().st_ino, inode)
            Path(data, "skald.json").unlink()
            missing = self.serve(data, flags=["--print-config"])
            self.assertEqual((missing.returncode, missing.stderr), (2, "skald.json missing: restore it\n"))

    def test_interrupted_initialization_has_durable_stamp_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as data:
            stamp = Path(data, ".skald-household")
            synced = []
            fsync, link = os.fsync, os.link
            def sync(fd):
                info = os.fstat(fd)
                if stat.S_ISDIR(info.st_mode) and info.st_ino == Path(data).stat().st_ino:
                    synced.append("directory")
                elif stamp.exists() and info.st_ino == stamp.stat().st_ino:
                    synced.append("stamp")
                return fsync(fd)
            def interrupted(source, target):
                if Path(target).name == "skald.json":
                    self.assertEqual(synced, ["stamp", "directory"], "stamp must be durable before config publication")
                    raise OSError("interrupted initialization")
                return link(source, target)
            with patch("os.fsync", sync), patch("os.link", interrupted):
                with self.assertRaises(self.s.ConfigError):
                    self.resolve(data, {"SKALD_MEMBERS": "ana,ben"})
            self.assertEqual(stamp.read_bytes(), b"")
            self.assertFalse(Path(data, "skald.json").exists())
            with self.assertRaisesRegex(self.s.ConfigError, "skald.json missing: restore it"):
                self.resolve(data)
            self.write(data)
            synced.clear()
            with patch("os.fsync", sync):
                self.assertEqual(self.resolve(data)[1], "file")
            self.assertEqual(synced, ["stamp", "directory"])

    def test_client_commands_require_household(self):
        with tempfile.TemporaryDirectory() as data:
            for flag in ("--new-client", "--revoke-client"):
                result = self.serve(data, flags=[flag, "fixture-client"])
                self.assertEqual(result.returncode, 2)
                self.assertIn("empty DATA", result.stderr)
                self.assertFalse(Path(data, "clients.json").exists())

    def test_snapshot_configure_ignores_household_environment(self):
        from skald import members
        with tempfile.TemporaryDirectory() as data:
            try:
                with patch.dict(os.environ, {"SKALD_MEMBERS": "ana,ben", "SKALD_TZ": "Asia/Tokyo"}):
                    members.configure(data)
                self.assertEqual(members.CONFIG_SOURCE, "implicit")
                self.assertEqual(list(Path(data).iterdir()), [])
            finally:
                members.configure()

    def test_blank_snapshot_configure_honours_live_config_and_writes_stamp(self):
        from skald import members
        from skald.__main__ import parser
        try:
            for snapshot in ("", " \t\n"):
                with self.subTest(snapshot=snapshot), tempfile.TemporaryDirectory() as data:
                    cfg = self.s.fresh_config(["ana"])
                    path = Path(data, "custom.json")
                    path.write_text(json.dumps(cfg))
                    with patch.dict(os.environ, {"DATA_DIR": data, "SKALD_CONFIG": str(path),
                                                 "SKALD_SNAPSHOT": snapshot}, clear=True):
                        members.configure(os.environ["SKALD_SNAPSHOT"])
                        self.assertEqual((members.CONFIG, members.CONFIG_SOURCE), (cfg, "file"))
                        self.assertTrue(Path(data, ".skald-household").is_file())
                        self.assertIsNone(parser().parse_args(["trakt", "login"]).snapshot)
        finally:
            members.configure()

    def test_fresh_cli_startup_never_writes_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as state:
            before = {p.name: p.read_bytes() for p in Path(data).iterdir()}
            result = self.process(data, ["-m", "skald", "--snapshot", data, "--state-dir", state, "rows", "--json"],
                                  {"SKALD_MEMBERS": "ana,ben", "SKALD_TZ": "UTC"})
            self.assertEqual({p.name: p.read_bytes() for p in Path(data).iterdir()}, before)
            self.assertEqual(result.returncode, 2)
            self.assertIn("empty DATA", result.stdout)

    def test_configured_snapshot_cli_reads_never_write_household_stamp(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as state:
            self.write(data)
            Path(data, "profile.json").write_text('{"entries": []}')
            Path(data, "facets.json").write_text('{"items": {}}')
            before = {p.name: p.read_bytes() for p in Path(data).iterdir()}
            commands = (["doctor", "keys"], ["doctor"], ["rows"], ["--help"])
            for command in commands:
                with self.subTest(command=command):
                    result = self.process(data, ["-m", "skald", "--snapshot", data,
                                                "--state-dir", state, "--json", *command])
                    self.assertIn(result.returncode, (0, 1), result.stderr)
                    self.assertEqual({p.name: p.read_bytes() for p in Path(data).iterdir()}, before)
            for command in (["doctor"], ["--help"]):
                result = self.process(data, ["-m", "skald", "--state-dir", state, "--json", *command])
                self.assertIn(result.returncode, (0, 1), result.stderr)
                self.assertEqual({p.name: p.read_bytes() for p in Path(data).iterdir()}, before)

    def test_utc_needs_no_zoneinfo(self):
        cfg = self.s.fresh_config(["ana"])
        with patch.object(self.s, "ZoneInfo", side_effect=AssertionError("tzdata needed")):
            self.assertEqual(self.s.validate_config(cfg), cfg)
            with patch.object(self.s, "TZ", "UTC"):
                self.assertEqual(self.s.local_now(0), dt.datetime(1970, 1, 1))

    def test_region_language_names_and_unknown(self):
        names = dict(de="German", en="English", fr="French", es="Spanish", it="Italian", nl="Dutch",
                     pt="Portuguese", sv="Swedish", da="Danish", nb="Norwegian", fi="Finnish", pl="Polish",
                     ja="Japanese", ko="Korean", zz="local")
        for lang, name in names.items():
            with self.subTest(lang=lang), tempfile.TemporaryDirectory() as data:
                cfg = self.s.fresh_config(["ana"])
                cfg["region"].update(lang=lang + "-US", country="Canary Country")
                self.write(data, cfg)
                s = load_server(data)
                try:
                    self.assertEqual(s.REGION_LANGUAGE, name)
                finally:
                    close_server(s)

    def test_archived_prompt_uses_neutral_pronouns(self):
        p = {"entries": [{"title": "Fixture", "weight": 1}], "likes": "fixture", "seen": set()}
        with patch.object(self.s, "ARCHIVED", {"old": "Old"}), patch.dict(self.s._profiles, {"old": p}), \
                patch.object(self.s, "profile_text", return_value="activity") as profile, \
                patch.object(self.s, "llm_sections", return_value={}) as llm:
            self.s.archived_feed("en-US", "old")
        self.assertIn("Address them", llm.call_args.args[1])
        self.assertIn("their own", llm.call_args.args[1])
        profile.assert_called_once_with(p, "they")


if __name__ == "__main__":
    unittest.main()
