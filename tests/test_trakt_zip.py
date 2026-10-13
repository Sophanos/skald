"""Synthetic Trakt exports; no account data or network."""
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from skald import guide, intake
from skald.__main__ import text_output
from skald.ingest import parse, sources
from skald.ingest import trakt
from skald.members import PEOPLE


def title(name, kind="movie"):
    return {kind: {"title": name, "year": 2020, "ids": {"imdb": "tt" + name}}}


def export(root):
    watched = {**title("Watched"), "plays": 1, "last_watched_at": "2026-10-01"}
    show = {**title("Series", "show"), "seasons": [{"number": 1, "episodes": [{"number": 1}]}]}
    files = {
        "watched-movies.json": [watched, watched],
        "watched-shows.json": [show],
        "watched-history-1.json": [{**title("Series", "show"), "watched_at": "2026-10-01"}],
        "ratings-movies.json": [{**title("Rated"), "rating": 9, "rated_at": "2026-10-01"}],
        "lists-watchlist.json": [{**title("Wish"), "listed_at": "2026-10-01"}],
        "lists-favorites.json": [{**title("Favorite"), "listed_at": "2026-10-01"}],
        "collection-movies.json": [title("Owned"), title("Watched")],
        "hidden-shows.json": [title("Hidden", "show"), title("Hidden", "show")],
        "user-profile.json": {"username": "synthetic"},
        "network-followers.json": [{"username": "synthetic"}],
    }
    folder = root / "folder"
    folder.mkdir()
    archive = root / "trakt-export.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, rows in files.items():
            content = json.dumps(rows)
            (folder / name).write_text(content)
            z.writestr(name, content)
    return folder, archive


class TraktZipTests(unittest.TestCase):
    def test_zip_folder_and_intake_parity(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, archive = export(Path(temp))
            self.assertEqual(parse("trakt", archive), parse("trakt", folder))
            server = SimpleNamespace(IMPORT_TRUST={"trakt": 1})
            cs = intake.collect([("trakt", archive)], PEOPLE[0], {}, server=server)[0][2]
            self.assertEqual(cs, parse("trakt", folder))
            self.assertIn("zip", sources()["trakt"].SOURCE["hint"])

    def test_zip_member_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "export.zip"
            with zipfile.ZipFile(archive, "w") as z:
                for name in ("../bad.json", "/bad.json", "nested/../bad.json", "ignore.txt"):
                    z.writestr(name, json.dumps([title("Bad")]))
                z.writestr("directory.json/", "")
                z.writestr("nested/watched-movies.json", json.dumps([title("Good")]))
            self.assertEqual([c.title for c in parse("trakt", archive)], ["Good"])
            self.assertEqual(list(Path(temp).iterdir()), [archive])

    def test_zip_size_limits(self):
        # Patch central-directory sizes of tiny synthetic archives, without allocating huge data.
        for sizes, message in (([100 * 1024**2 + 1], "100 MB"), ([80 * 1024**2] * 4, "300 MB")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "export.zip"
                with zipfile.ZipFile(archive, "w") as z:
                    for i in range(len(sizes)):
                        z.writestr(f"watched-{i}.json", "[]")
                data = bytearray(archive.read_bytes())
                offset = 0
                for size in sizes:
                    offset = data.index(b"PK\x01\x02", offset)
                    struct.pack_into("<I", data, offset + 24, size)
                    offset += 46
                archive.write_bytes(data)
                with self.assertRaisesRegex(ValueError, message):
                    parse("trakt", archive)
                with self.assertRaisesRegex(ValueError, message):
                    intake.collect([("trakt", archive)], PEOPLE[0], {}, server=SimpleNamespace(IMPORT_TRUST={"trakt": 1}))

    def test_zip_actual_decompressed_limits(self):
        for member_cap, total_cap, names, message in (
                (200, 1000, ["watched-movies.json"], "100 MB"),
                (1000, 500, ["watched-movies.json", "watched-history-1.json"], "300 MB")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "export.zip"
                content = json.dumps([title("Synthetic")]) + " " * 300
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    for name in names:
                        z.writestr(name, content)
                data = bytearray(archive.read_bytes())
                # Both headers understate the size; CRC still covers the full payload.
                for signature, size_offset in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
                    offset = 0
                    for _ in names:
                        offset = data.index(signature, offset)
                        struct.pack_into("<I", data, offset + size_offset, 1)
                        offset += size_offset + 4
                archive.write_bytes(data)
                with patch.object(trakt, "MAX_MEMBER_BYTES", member_cap, create=True), \
                        patch.object(trakt, "MAX_TOTAL_BYTES", total_cap, create=True):
                    with self.assertRaisesRegex(ValueError, message):
                        parse("trakt", archive)

    def test_bare_and_prefixed_annotations_add_nothing(self):
        for name in ("comments", "notes", "likes", "comments-movies", "notes-movies", "likes-movies"):
            for wrapped in (False, True):
                with self.subTest(name=name, wrapped=wrapped), tempfile.TemporaryDirectory() as temp:
                    rows = [{**title("Ignored"), "likes": True}]
                    path = Path(temp) / ("export.json" if wrapped else name + ".json")
                    path.write_text(json.dumps({name: rows} if wrapped else rows))
                    self.assertEqual(parse("trakt", path), [])

    def test_dry_run_counts_across_input_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            watched = root / "watched-movies.json"
            history = root / "watched-history-1.json"
            watched.write_text(json.dumps([{**title("Shared"), "plays": 1}]))
            history.write_text(json.dumps([{**title("Shared"), "watched_at": "2026-10-01"}]))
            args = SimpleNamespace(command="import", source="trakt", files=[watched, history], profile_map=[],
                                   person=PEOPLE[0], resolve_online=False, remote=True, max_ai_usd=0,
                                   dry_run=True, yes=True)
            server = SimpleNamespace(IMPORT_TRUST={"trakt": 1}, read_imports=lambda: [])
            engine = SimpleNamespace(_server=server, _profile={"entries": []})
            with patch("skald.intake.resolve", return_value=(SimpleNamespace(spent=0, decide=None), [])) as resolve:
                result = intake.import_command(args, engine, root / "state")
            self.assertEqual(resolve.call_args.args[2], parse("trakt", watched) + parse("trakt", history))
            out = io.StringIO()
            text_output(args, result, out)
            self.assertIn("Trakt: watched movies 1, watched series 0, watchlist 0, ratings 0, favorites 0, hidden 0\n", out.getvalue())
            stats = {}
            intake.collect([("trakt", watched), ("trakt", history)], PEOPLE[0], {}, server=server, stats=stats)
            self.assertEqual(stats["trakt_watched_movies"], 1)
            self.assertEqual(stats["_sources"]["trakt"]["trakt_watched_movies"], 1)

    def test_favorites_are_love(self):
        for name, data in (("lists-favorites.json", [title("Favorite")]),
                           ("favorites-movies.json", [title("Favorite")]),
                           ("export.json", {"favorites": [title("Favorite")]})):
            rows = data if isinstance(data, list) else data["favorites"]
            rows[0]["listed_at"] = "2026-10-01"
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / name
                path.write_text(json.dumps(data))
                self.assertEqual(parse("trakt", path)[0].signal, "love")

    def test_collection_and_annotations_add_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, _ = export(Path(temp))
            (folder / "playback-movies.json").write_text(json.dumps([title("Playback")]))
            for prefix in ("comments", "notes", "likes"):
                (folder / f"{prefix}-movies.json").write_text(json.dumps([{**title(prefix), "likes": True}]))
            rows = {c.title: c.signal for c in parse("trakt", folder)}
            self.assertEqual(rows, {"Watched": "seen", "Series": "seen", "Rated": "love", "Wish": "interest", "Favorite": "love", "Hidden": "abandon", "Playback": "seen"})
            only = folder / "collection-movies.json"
            self.assertEqual(parse("trakt", only), [])

    def test_dry_run_count_line(self):
        with tempfile.TemporaryDirectory() as temp:
            _, archive = export(Path(temp))
            args = SimpleNamespace(command="import", source="trakt", files=[archive], profile_map=[],
                                   person=PEOPLE[0], resolve_online=False, remote=True, max_ai_usd=0,
                                   dry_run=True, yes=True)
            engine = SimpleNamespace(_server=SimpleNamespace(IMPORT_TRUST={"trakt": 1}, read_imports=lambda: []),
                                     _profile={"entries": []})
            resolver = SimpleNamespace(spent=0, decide=None)
            with patch("skald.intake.resolve", return_value=(resolver, [])) as resolve:
                result = intake.import_command(args, engine, Path(temp) / "state")
            self.assertTrue(resolve.call_args.kwargs["remote"])
            out = io.StringIO()
            text_output(args, result, out)
            lines = out.getvalue().splitlines()
            self.assertEqual([line for line in lines if line.startswith("Trakt:")],
                             ["Trakt: watched movies 1, watched series 1, watchlist 1, ratings 1, favorites 1, hidden 1"])
            self.assertFalse((Path(temp) / "state").exists())

    def test_howto_and_quick_zip(self):
        how = guide.HOWTO["trakt"]
        self.assertEqual(how["url"], "https://app.trakt.tv/settings/data")
        self.assertEqual(how["steps"], ["Open Trakt Settings → Data.", "Choose Export now and download the zip.", "Import the zip as is; no need to unzip."])
        with tempfile.TemporaryDirectory() as temp:
            _, archive = export(Path(temp))
            self.assertIn(("trakt", archive), guide.quick(temp, SimpleNamespace(IMPORT_TRUST={"trakt": 1})))

    def test_export_documentation(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("docs/CONNECTORS.md",):
            text = (root / name).read_text()
            for link in ("[Trakt Settings → Data](https://app.trakt.tv/settings/data)",
                         "[Viewing activity](https://help.netflix.com/en/node/101917)"):
                self.assertIn(link, text)
            self.assertIn('"Export now"', text)
            self.assertIn('"Download all"', text)
            self.assertIn("import trakt trakt-export.zip", text)
        self.assertLessEqual(len((root / "README.md").read_text().splitlines()), 220)

    def test_existing_fixture_folder_unchanged(self):
        fixture = Path(__file__).parent / "fixtures/ingest/trakt.json"
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "trakt.json").write_text(" \n" + fixture.read_text())
            rows = parse("trakt", temp)
            self.assertEqual(rows, parse("trakt", fixture))
            self.assertEqual([(c.title, c.signal, c.n) for c in rows],
                             [("Castle", "seen", 2), ("The Matrix", "love", 1), ("Arrival", "interest", 1)])
