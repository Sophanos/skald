"""Synthetic player traffic, with HTTP restricted to an ephemeral loopback server."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from eval import search_battery as battery


def step(q, movie="some", series="some", group="searches", **extra):
    return dict(group=group, q=q, movie=movie, series=series, **extra)


@contextmanager
def fake_skald(reply, catalog='skald.search'):
    arrivals = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.endswith('/manifest.json'):
                body = {'catalogs': [{'id': catalog, 'type': lane, 'extra': [{'name': 'search'}]} for lane in battery.LANES]}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            parts = self.path.split("/")
            if parts[-2] != catalog:
                self.send_error(404)
                return
            lane = parts[-3]
            query = unquote(parts[-1][len("search="):-len(".json")])
            with lock:
                record = dict(lane=lane, q=query, path=self.path, start=time.monotonic())
                arrivals.append(record)
            status, body = reply(lane, query, arrivals)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            record["end"] = time.monotonic()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", arrivals
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def picks(lane, query, arrivals):
    return 200, {"metas": [{"name": "Synthetic " + lane}]}


class SearchBatteryTests(unittest.TestCase):
    def test_discovers_search_catalog_once_from_same_manifest(self):
        for catalog in ('skald.search', 'custom.find'):
            urls = []
            def open_fake(url, **kwargs):
                urls.append(url)
                if url.endswith('/manifest.json'):
                    body = {'catalogs': [{'id': 'another.row', 'type': 'movie'},
                                         {'id': catalog, 'type': 'movie', 'extra': [{'name': 'search'}]},
                                         {'id': catalog, 'type': 'series', 'extra': [{'name': 'search'}]}]}
                else:
                    body = {'metas': [{'name': 'Synthetic pick'}]}
                return io.BytesIO(json.dumps(body).encode())
            with self.subTest(catalog=catalog), patch.object(battery.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = open_fake
                code, _, _ = self.run_battery('http://synthetic.invalid/p/ana', [step('Synthetic one'), step('Synthetic two')])
            self.assertEqual(code, 0)
            self.assertEqual(urls[0], 'http://synthetic.invalid/p/ana/de-DE/manifest.json')
            self.assertEqual(sum(url.endswith('/manifest.json') for url in urls), 1)
            self.assertTrue(all('/'+catalog+'/search=' in url for url in urls[1:]))

    def test_catalog_override_needs_no_manifest(self):
        with patch.object(battery.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(b'{"metas":[]}')
            code, _, _ = self.run_battery('http://synthetic.invalid', [step('Synthetic', 'any', 'any')], '--catalog', 'chosen.search')
            self.assertEqual(code, 0)
            self.assertEqual(build.return_value.open.call_count, 2)
            self.assertTrue(all('/chosen.search/search=' in call.args[0] for call in build.return_value.open.call_args_list))

    def run_battery(self, base, session, *args):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(battery, "SESSION", session), \
                patch.dict(os.environ, {"DATA_DIR": directory}):
            report = Path(directory) / "report.json"
            code = battery.main(["--base", base, "--json", str(report), *args], out=output)
            return code, json.loads(report.read_text()), output.getvalue()

    def test_lanes_overlap_and_url_quotes_one_query(self):
        meeting = threading.Barrier(2)

        def reply(lane, query, arrivals):
            meeting.wait(timeout=2)
            time.sleep(.03)
            return picks(lane, query, arrivals)

        query = "Synthetic / café & ? + #"
        with fake_skald(reply) as (base, arrivals):
            code, report, output = self.run_battery(base, [step(query)])
        self.assertEqual(code, 0)
        self.assertEqual(len(arrivals), 2)
        self.assertEqual({r["q"] for r in arrivals}, {query})
        self.assertLess(max(r["start"] for r in arrivals), min(r["end"] for r in arrivals))
        self.assertTrue(all(r["path"].startswith("/de-DE/catalog/") for r in arrivals))
        self.assertTrue(all("/skald.search/search=" in r["path"] for r in arrivals))
        self.assertEqual(report["summary"]["steps"], 1)
        self.assertIn("movie", output)
        self.assertIn("series", output)

    def test_fixed_session_order_and_followup_parents(self):
        session = battery.SESSION
        with fake_skald(picks) as (base, arrivals):
            _, report, _ = self.run_battery(base, session)
        self.assertEqual([r["q"] for r in report["results"]], [s["q"] for s in session])
        full = [r for r in arrivals if r["q"] in {s["q"] for s in session}]
        self.assertEqual([r["q"] for r in full[::2]], [s["q"] for s in session])
        for previous, current in zip(session, session[1:]):
            before = [r for r in full if r["q"] == previous["q"]]
            after = [r for r in full if r["q"] == current["q"]]
            self.assertLessEqual(max(r["end"] for r in before), min(r["start"] for r in after))
        queries = [s["q"] for s in session]
        first = queries.index("more like the second one")
        self.assertEqual(session[first - 1]["group"], "searches")
        self.assertEqual(queries[first:first + 5], ["more like the second one", "but less violent",
                         "also some series", "save this as a row", "I like these, save them as my taste"])
        self.assertEqual(session[first + 5]["group"], "searches")
        for index, item in enumerate(session):
            if item["group"] == "controls":
                self.assertEqual(session[index - 1]["group"], "searches")
        self.assertEqual({s["q"] for s in session if s["group"] == "controls"},
                         {"help", "history", "settings", "ideas"})

    def test_only_filters_without_reordering(self):
        with fake_skald(picks) as (base, arrivals):
            for group in ("searches", "followups", "controls", "all"):
                with self.subTest(group=group):
                    _, report, _ = self.run_battery(base, battery.SESSION, "--only", group)
                    expected = [s["q"] for s in battery.SESSION if group == "all" or s["group"] == group]
                    self.assertEqual([r["q"] for r in report["results"]], expected)

    def test_warm_movie_stall_is_not_hidden_by_series(self):
        session = [step("Synthetic cold mood"), step("Synthetic warm mood")]

        def reply(lane, query, arrivals):
            if query == session[1]["q"] and lane == "movie" and any(
                    r["q"] == session[0]["q"] for r in arrivals):
                time.sleep(.12)
                return 200, {"metas": []}
            return picks(lane, query, arrivals)

        with fake_skald(reply) as (base, arrivals):
            code, report, output = self.run_battery(base, session, "--deadline", ".08", "--slow", ".05")
        cold, warm = report["results"]
        self.assertEqual(cold["flags"], [])
        self.assertTrue({"EMPTY", "SPLIT", "DEADLINE", "SLOW"} <= set(warm["flags"]))
        self.assertIn("DEADLINE", warm["lanes"]["movie"]["flags"])
        self.assertEqual(warm["lanes"]["series"]["flags"], [])
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"]["flags"]["SPLIT"], 1)
        self.assertGreater(report["summary"]["wall"]["movie"]["max"], .08)
        self.assertIn("p90", output)

    def test_empty_filler_control_and_http_json_errors(self):
        session = [step("Synthetic empty"), step("Synthetic filler", "none", "any"),
                   step("help", "any", "any", "controls"), step("Synthetic 500"),
                   step("Synthetic bad JSON"), step("Synthetic bad shape"), step("Synthetic body error")]

        def reply(lane, query, arrivals):
            if query == "Synthetic empty":
                return 200, {"metas": []}
            if query == "help":
                return 200, {"metas": [{"name": "Read only card"}, {"name": battery.AI_MARK + "AI pick"}]}
            if query == "Synthetic 500":
                return 500, {"error": "synthetic"}
            if query == "Synthetic bad JSON":
                return 200, b"{"
            if query == "Synthetic bad shape":
                return 200, {"metas": "wrong"}
            if query == "Synthetic body error":
                return 200, {"metas": [], "error": "synthetic"}
            return picks(lane, query, arrivals)

        with fake_skald(reply) as (base, arrivals):
            code, report, _ = self.run_battery(base, session)
        self.assertEqual(code, 1)
        for row, flag in zip(report["results"], ("EMPTY", "FILLER", "CONTROL", "ERR", "ERR", "ERR", "ERR")):
            self.assertIn(flag, row["flags"])
        self.assertNotIn("SPLIT", report["results"][0]["flags"])

    def test_slow_alone_does_not_fail_and_any_can_be_empty(self):
        def reply(lane, query, arrivals):
            time.sleep(.03)
            return 200, {"metas": []}

        with fake_skald(reply) as (base, arrivals):
            code, report, _ = self.run_battery(base, [step("Synthetic any", "any", "any")],
                                               "--slow", ".01", "--deadline", "1")
        self.assertEqual(code, 0)
        self.assertEqual(report["results"][0]["flags"], ["SLOW"])

    def test_unsafe_queries_are_refused_before_any_request(self):
        queries = ("yes 12", "no 00", " YES   99 ", "No\t42", "yes １２", "yes", "ok", "undo!", "undo", "redo", "reset filters",
                   "stop a row", "rückgängig", "bring back horror", "do again horror", "alles zurücksetzen")
        with fake_skald(picks) as (base, arrivals):
            for query in queries:
                with self.subTest(query=query), patch.object(battery, "SESSION", [step("Synthetic safe"), step(query)]):
                    output = io.StringIO()
                    self.assertEqual(battery.main(["--base", base], out=output), 1)
                    self.assertIn("refused", output.getvalue().lower())
            self.assertEqual(arrivals, [])
        for query in ("Yes Man", "No Country for Old Men"):
            battery.check_query(query)

    def test_typing_prefixes_overlap_and_only_full_query_is_scored(self):
        query = "Synthetic typed search"
        full_arrived = threading.Event()

        def reply(lane, text, arrivals):
            if text != query:
                self.assertEqual(lane, "movie")
                self.assertTrue(full_arrived.wait(timeout=3))
                return 500, {"error": "prefix ignored"}
            full_arrived.set()
            return picks(lane, text, arrivals)

        with fake_skald(reply) as (base, arrivals):
            code, report, _ = self.run_battery(base, [step(query, typing=True)])
        prefixes = [r for r in arrivals if r["q"] != query]
        self.assertGreaterEqual(len(prefixes), 2)
        self.assertTrue(all(query.startswith(r["q"]) for r in prefixes))
        for earlier, later in zip(prefixes, prefixes[1:]):
            self.assertAlmostEqual(later["start"] - earlier["start"], .15, delta=.08)
        self.assertLess(next(r["start"] for r in arrivals if r["q"] == query), min(r["end"] for r in prefixes))
        self.assertEqual(code, 0)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["summary"]["steps"], 1)

    def test_log_join_keeps_newest_query_and_arbitrary_fields(self):
        query = "Synthetic log query"
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "searches.jsonl"
            records = [dict(t=1, q=query, timing={"router": 99}), dict(t=2, q="other", wrong=True),
                       dict(t=3, q=query, timing={"router": .3, "picker": .4, "resolve": .1}, extra="future")]
            log.write_text("\n".join(json.dumps(r) for r in records) + "\n{", encoding="utf-8")
            with fake_skald(picks) as (base, arrivals):
                code, report, output = self.run_battery(base, [step(query)], "--log", str(log))
                with patch.object(battery, "SESSION", [step(query)]), patch.dict(os.environ, {"DATA_DIR": directory}):
                    default_output = io.StringIO()
                    self.assertEqual(battery.main(["--base", base], out=default_output), 0)
            self.assertEqual(code, 0)
            self.assertEqual(report["results"][0]["timing"], {k: v for k, v in records[-1].items() if k not in ("t", "q")})
            for text in ('"router":0.3', '"picker":0.4', '"resolve":0.1', '"extra":"future"'):
                self.assertIn(text, output)
                self.assertIn(text, default_output.getvalue())

    def test_names_are_limited_but_control_checks_all_names(self):
        def reply(lane, query, arrivals):
            return 200, {"metas": [{"name": "x" * 60}] * 3 + [{"name": battery.AI_MARK + "fourth"}]}

        with fake_skald(reply) as (base, arrivals):
            code, report, output = self.run_battery(base, [step("settings", "any", "any", "controls")])
        self.assertEqual(code, 1)
        self.assertIn("CONTROL", report["results"][0]["flags"])
        self.assertNotIn("x" * 41, output)
        self.assertNotIn("fourth", output)
        self.assertEqual(report["results"][0]["lanes"]["movie"]["count"], 4)

    def test_stdin_and_checkout_entrypoints(self):
        script = Path(__file__).resolve().parents[1] / "eval/search_battery.py"
        for args, source in (([str(script), "--help"], None), (["-", "--help"], script.read_text())):
            process = subprocess.run([sys.executable, *args], input=source, text=True,
                                     capture_output=True, timeout=5, cwd=script.parent.parent)
            self.assertEqual(process.returncode, 0, process.stderr)
            for flag in ("--base", "--lang", "--catalog", "--only", "--deadline", "--slow", "--log", "--json"):
                self.assertIn(flag, process.stdout)

    def test_safety_preflight_without_transport(self):
        for query in ("yes 12", "NO 42", "yes １２", "yes", "ok", "undo!", "undo", "redo", "reset filters", "stop a row"):
            with self.subTest(query=query), patch.object(battery, "SESSION", [step("Synthetic safe"), step(query)]), \
                    patch.object(battery, "fetch", side_effect=AssertionError("request sent")):
                self.assertEqual(battery.main([], out=io.StringIO()), 1)

    def test_deadline_boundary_and_timeout_without_transport(self):
        response = io.StringIO('{"metas":[{"name":"Synthetic pick"}]}')
        with patch.object(battery.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = response
            result = battery.fetch("http://127.0.0.1:8080", "de-DE", "movie", "Synthetic pick", catalog='skald.search')
            self.assertEqual(result["count"], 1)
            self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 40})

        def reply(base, lang, lane, query, gate=None, *, catalog):
            if gate is not None:
                gate.wait(timeout=2)
            warm_movie = query == "Synthetic warm" and lane == "movie"
            return dict(wall=6.8 if warm_movie else .01, count=0 if warm_movie else 1,
                        names=[] if warm_movie else ["Synthetic pick"], error=None)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(battery, "SESSION", [step("Synthetic cold"), step("Synthetic warm")]), \
                patch.object(battery, "fetch", side_effect=reply), \
                patch.dict(os.environ, {"DATA_DIR": directory}):
            path = Path(directory) / "result.json"
            self.assertEqual(battery.main(["--json", str(path), '--catalog', 'skald.search'], out=io.StringIO()), 1)
            report = json.loads(path.read_text())
        self.assertEqual(report["results"][0]["flags"], [])
        self.assertEqual(set(report["results"][1]["flags"]), {"EMPTY", "DEADLINE", "SLOW", "SPLIT"})
        self.assertEqual(report["summary"]["wall"]["movie"]["max"], 6.8)


if __name__ == "__main__":
    unittest.main()
