"""Large-export regressions use generated data only."""
from html.parser import HTMLParser
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from skald.ingest import Candidate, clean_title, day, interests, parse
from skald.ingest import youtube
from skald.intake import collect, import_command, onboard


class ChunkedText(io.StringIO):
    def read(self, size=-1):
        if size < 0:
            raise AssertionError("whole-file read")
        return super().read(min(size, 71))


def old_youtube(content):
    class History(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.parts, self.link = [], None, False

        def handle_starttag(self, tag, attrs):
            if tag == "div" and set(dict(attrs).get("class", "").split()) == {
                    "content-cell", "mdl-cell", "mdl-cell--6-col", "mdl-typography--body-1"}:
                self.parts, self.link = [], False
            elif self.parts is not None:
                if tag == "br":
                    self.parts.append("\n")
                elif tag == "a":
                    self.link = True

        def handle_data(self, data):
            if self.parts is not None:
                self.parts.append(data)

        def handle_endtag(self, tag):
            if tag == "div" and self.parts is not None:
                text = "".join(self.parts).translate(str.maketrans("\u00a0\u202f\u2009", "   "))
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if self.link and lines and re.match(r"^Watched\s", lines[0]):
                    self.rows.append({"title": lines[0], "time": youtube.html_day(lines[-1])})
                self.parts = None

    if content.lstrip().startswith("<"):
        history = History()
        history.feed(content)
        rows = history.rows
    else:
        rows = json.loads(content)
    out = []
    for row in rows:
        text = re.sub(r"^Watched\s+", "", row.get("title", ""), flags=re.I)
        match = re.search(r"\b(?:trailer|teaser|official\s+clip)\b", text, re.I)
        if not match:
            continue
        title = re.sub(r"\b(?:official|final|new|full)\s*$", "", text[:match.start()], flags=re.I).strip(" -–—|:")
        year_match = re.search(r"\((\d{4})\)", text)
        year = year_match[1] if year_match else ""
        title = re.sub(r"\(\d{4}\)", "", title).strip()
        if title:
            out.append(Candidate(clean_title(title), year, "", {}, "interest", 1, day(row.get("time")), "youtube", conf=.5))
    return interests(out, max((day(r.get("time")) for r in rows), default=""))


class LargeExportTests(unittest.TestCase):
    def test_regular_stdin_fingerprint_and_candidates(self):
        server = SimpleNamespace(IMPORT_TRUST={"youtube": True})
        script = '''
from dataclasses import asdict
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from skald.intake import collect
fd = os.dup(0)
path = sys.argv[1].replace("{fd}", str(fd))
if not Path(path).exists():
    sys.exit(77)
source, digest, candidates = collect(
    [("youtube", path)], "ana", {},
    server=SimpleNamespace(IMPORT_TRUST={"youtube": True}))[0]
print(json.dumps([source, digest, [asdict(c) for c in candidates]]))
'''
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "export.json"
            file.write_text(json.dumps([{"title": "Watched Arrival (2016) Official Trailer",
                                         "time": "2026-10-11"}]))
            alias = Path(temp) / "stdin-alias"
            alias.symlink_to("/dev/stdin")
            source, digest, candidates = collect([("youtube", file)], "ana", {}, server=server)[0]
            self.assertTrue(candidates)
            expected = [source, digest, [asdict(c) for c in candidates]]
            for path in ("/dev/stdin", "/dev/fd/{fd}", "/proc/self/fd/{fd}", str(alias), str(file)):
                with self.subTest(path=path), file.open("rb") as stdin:
                    result = subprocess.run([sys.executable, "-c", script, path], stdin=stdin,
                                            capture_output=True, text=True, timeout=10)
                    if result.returncode == 77:
                        continue  # /proc/self/fd is unavailable on macOS.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)

    def test_youtube_streaming_matches_old(self):
        entries = []
        rows = []
        dates = ("11 Oct 2026, 01:15:02 CEST", "Oct 10, 2026, 1:15:02 AM", "09.10.2026, 01:15:02", "unknown date")
        for i in range(90):
            space = ("\u00a0", "\u202f", "\u2009")[i % 3]
            promotion = ("Official Trailer", "Teaser", "Official Clip")[i % 5 % 3]
            title = f"Film {i % 9} (2026) {promotion}" if i % 3 else "cooking " + "x" * 1000
            date = dates[i % 4].replace(" ", space)
            entries.append(f'<div class="outer-cell"><div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Watched{space}<a href="https://example.test/watch">{title}</a><br><a href="https://example.test/channel">Channel</a><br>{date}</div></div>')
            rows.append({"title": "Watched" + space + title, "time": youtube.html_day(date.replace(space, " ")), "padding": "x" * 1000})
        # Newest non-promotional entry determines the cutoff.
        rows.append({"title": "cooking", "time": "2027-01-01"})
        entries.append('<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Watched <a>cooking</a><br>01 Jan 2027, 01:00:00</div>')
        for content in (" \n" + "".join(entries), json.dumps(rows, ensure_ascii=False), "[]",
                        json.dumps([{"title": "cooking", "time": "2026-10-11"}]),
                        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Watched <a>cooking</a><br>11 Oct 2026, 01:00:00</div>'):
            with self.subTest(format=content.lstrip()[:1]), patch("builtins.open", return_value=ChunkedText(content)):
                self.assertEqual(youtube.parse("synthetic"), old_youtube(content))

    def test_youtube_non_array_error_classes(self):
        for content in ('123', 'null', 'true', '{"title": "Film"}', '"Film"', 'not JSON'):
            with self.subTest(content=content):
                try:
                    old_youtube(content)
                except (TypeError, AttributeError, ValueError) as error:
                    expected = type(error)
                with patch("builtins.open", return_value=ChunkedText(content)), self.assertRaises(expected):
                    youtube.parse("synthetic")

    def test_youtube_empty_object_and_string(self):
        for content in ('{}', '""'):
            with self.subTest(content=content), patch("builtins.open", return_value=ChunkedText(content)):
                self.assertEqual(youtube.parse("synthetic"), [])

    def test_youtube_json_rejects_non_json_leading_whitespace(self):
        for content in ('\u2009[]', ' ' * 80 + '\u2009[]'):
            with self.subTest(content=content), patch("builtins.open", return_value=ChunkedText(content)):
                with self.assertRaises(json.JSONDecodeError):
                    youtube.parse("synthetic")

    def test_json_array_boundaries_and_errors(self):
        from skald.ingest import iter_json_array
        for size in (1, 2, 3):
            class TinyChunks(ChunkedText):
                def read(self, requested=-1):
                    return super().read(min(requested, size))
            numeric = '[12345, -1.23e+45, 0.12345, true, null]'
            self.assertEqual(list(iter_json_array(TinyChunks(numeric))), json.loads(numeric))
        rows = [None, True, False, 12345, -1.23e45, {"text": "escaped \\" * 30000}, [1, 2]]
        self.assertEqual(list(iter_json_array(ChunkedText(json.dumps(rows)))), rows)
        for text in ("", "[", "[1", "[1,]", "[1 2]", "[] trailing", "[falseX]", "[1e]", "[01]"):
            with self.subTest(text=text), self.assertRaises(json.JSONDecodeError):
                list(iter_json_array(ChunkedText(text)))
        with self.assertRaises(TypeError):
            list(iter_json_array(ChunkedText("123")))

    def test_trakt_array_streaming_matches_grouped(self):
        rows = [{"movie": {"title": f"Film {i % 3}", "ids": {"tmdb": i % 3 + 1}},
                 "watched_at": f"2026-10-{i % 10 + 1:02}", "padding": "x" * 900} for i in range(90)]
        rows += [None, {"unusable": True}]
        with tempfile.TemporaryDirectory() as temp:
            array = Path(temp) / "history.json"
            grouped = Path(temp) / "grouped.json"
            array.write_text(json.dumps(rows))
            grouped.write_text(json.dumps({"history": rows}))
            expected_stats = {}
            expected = parse("trakt", grouped, stats=expected_stats)
            stats = {}
            with patch.object(Path, "open", return_value=ChunkedText(array.read_text())):
                self.assertEqual(parse("trakt", array, stats=stats), expected)
            self.assertEqual(stats, expected_stats)

    def test_chrome_disk_copy_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "History"
            data = root / "data"
            data.mkdir()
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE urls (id, url, title, visit_count, last_visit_time)")
            from skald.ingest import chrome
            original = chrome.shutil.copyfile
            def check_copy(src, dest):
                self.assertEqual(Path(dest).parent.parent, data)
                self.assertEqual(stat.S_IMODE(Path(dest).parent.stat().st_mode), 0o700)
                return original(src, dest)
            with patch.object(chrome.shutil, "copyfile", side_effect=check_copy):
                self.assertEqual(parse("chrome", path, temp_dir=data), [])
            self.assertEqual(list(data.iterdir()), [])
            path.write_text("not sqlite")
            with self.assertRaises(sqlite3.Error):
                parse("chrome", path, temp_dir=data)
            self.assertEqual(list(data.iterdir()), [])

    def test_pipe_fingerprint_candidates_and_cleanup(self):
        server = SimpleNamespace(IMPORT_TRUST={"plain": True})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            file = root / "export.txt"
            fifo = root / "pipe"
            os.mkfifo(fifo)
            original_parse = parse
            def check_parse(source, path, **kwargs):
                self.assertEqual(Path(path).parent.parent, data)
                self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(Path(path).parent.stat().st_mode), 0o700)
                return original_parse(source, path, **kwargs)
            results = []
            for content in (b"Arrival (2016)\n", b"Castle (2009)\n"):
                file.write_bytes(content)
                expected = collect([("plain", file)], "ana", {}, server=server, temp_dir=data)
                with patch.object(Path, "open", autospec=True) as opened, patch("skald.intake.parse", side_effect=check_parse):
                    opened.side_effect = lambda path, *args, **kwargs: io.BytesIO(content) if path == fifo else io.open(path, *args, **kwargs)
                    actual = collect([("plain", fifo)], "ana", {}, server=server, temp_dir=data)
                self.assertEqual(actual, expected)
                self.assertEqual(list(data.iterdir()), [])
                results.append(actual[0][1])
            previous = {"plain": {"file": results[0], "status": "applied"}}
            self.assertNotEqual(previous["plain"]["file"], results[1])
            with patch.object(Path, "open", autospec=True) as opened:
                opened.side_effect = lambda path, *args, **kwargs: io.BytesIO(b"https://invalid.test\n") if path == fifo else io.open(path, *args, **kwargs)
                with self.assertRaisesRegex(ValueError, "invalid export title"):
                    collect([("plain", fifo)], "ana", {}, server=server, temp_dir=data)
            self.assertEqual(list(data.iterdir()), [])

    def test_new_pipe_is_not_skipped(self):
        server = SimpleNamespace(IMPORT_TRUST={"plain": True})
        args = SimpleNamespace(non_interactive=True, sources=False, status=False, source=["plain=/dev/stdin"],
                               person="ana", undo=None, yes=False, plan=None, profile_map=[], again=False,
                               resolve_online=False, remote=False, max_ai_usd=0, dry_run=True)
        engine = SimpleNamespace(_server=server)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"DATA_DIR": temp}):
            state = Path(temp) / "state"
            original = Path.open
            def piped(content):
                return lambda path, *a, **kw: io.BytesIO(content) if str(path) == "/dev/stdin" else original(path, *a, **kw)
            with patch.object(Path, "open", autospec=True, side_effect=piped(b"Arrival\n")):
                digest = collect([("plain", "/dev/stdin")], "ana", {}, server=server, temp_dir=temp)[0][1]
            book = {"people": {"ana": {"plain": {"file": digest, "status": "applied"}}}}
            for content, titles in ((b"Arrival\n", []), (b"Castle\n", ["Castle"])):
                with patch("skald.intake._state", return_value=book), patch("skald.guide.review"), \
                        patch.object(Path, "open", autospec=True, side_effect=piped(content)), \
                        patch("skald.intake.resolve", side_effect=RuntimeError("stop after skip check")) as resolved:
                    with self.assertRaisesRegex(RuntimeError, "stop after skip check"):
                        onboard(args, engine, state)
                    self.assertEqual([c.title for c in resolved.call_args.args[2]], titles)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_import_uses_disk_directory(self):
        engine = SimpleNamespace(_server=SimpleNamespace(IMPORT_TRUST={"plain": True}))
        args = SimpleNamespace(source="plain", files=["unused"], profile_map=[], resolve_online=False,
                               remote=False, max_ai_usd=0, dry_run=True)
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            data = Path(temp) / "data"
            for household in (None, data):
                with patch.dict(os.environ), patch("skald.intake.resolve", side_effect=RuntimeError("stop")):
                    os.environ.pop("DATA_DIR", None)
                    if household:
                        os.environ["DATA_DIR"] = str(household)
                    parser = unittest.mock.Mock(return_value=[])
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        import_command(args, engine, state, parser=parser)
                    self.assertEqual(parser.call_args.kwargs["temp_dir"], household or state)
