"""Generic decision contracts: a new registry block needs only fixtures."""
from collections import Counter
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVING_P95_MS = 10


def load_server(data_dir):
    spec = importlib.util.spec_from_file_location("decide_server", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"TMDB_API": "offline", "DATA_DIR": str(data_dir), "OPENROUTER_API_KEY": "", "API_PORT": ""}), \
            patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
        spec.loader.exec_module(server)
    return server


class ReplayLLM:
    def __init__(self, server):
        self.server, self.calls, self.cost, self.recorded = server, 0, 0., None
        self.bodies = []

    def arm(self, recorded):
        self.recorded = recorded

    def urlopen(self, req, **kwargs):
        assert req.full_url.startswith("https://openrouter.ai/"), "unrecorded host"
        assert self.recorded is not None, "unarmed or second call"
        reply, self.recorded = self.recorded, None
        self.calls += 1
        self.cost += (reply.get("usage") or {}).get("cost") or 0
        self.bodies.append(json.loads(req.data))
        return io.BytesIO(json.dumps(reply).encode())

    def __enter__(self):
        self.key = patch.object(self.server, "OR_KEY", "replay")
        self.network = patch.object(self.server, "_connector_open", self.urlopen)
        self.key.start()
        self.network.start()
        return self

    def __exit__(self, *args):
        # The existing ledger saves asynchronously; finish those writes before temp cleanup.
        for thread in threading.enumerate():
            if getattr(thread, "_target", None) is self.server.save_usage:
                thread.join(2)
        self.network.stop()
        self.key.stop()


def close_server(server):
    server.flush_decisions()
    server._decide_queue.put(False)
    server._decide_queue.join()
    for name in ("_pool", "_outer", "_search_pool", "_nzb_pool", "_connector_pool"):
        pool = getattr(server, name, None)
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def fixture_rows(server, name, fixtures=None):
    path = Path(fixtures or server.DECISIONS[name]["fixtures"])
    return [json.loads(line) for line in (path if path.is_absolute() else ROOT / path).read_text().splitlines()]


def reset(server):
    server.flush_decisions()
    with server._decide_lock:
        server._decide_cache.clear()
    with server._usage_lock:
        server._usage.update(day="", month="", calls={}, month_cost=0, cost=0, tokens=0, month_calls=0)
        server._decide_day = ""
        server._decision_roll()


def run_type(server, name, fixtures=None):
    sources, elapsed, correct = Counter(), [], 0
    rows = fixture_rows(server, name, fixtures)
    counts, hits = Counter(), Counter()
    with ReplayLLM(server) as replay:
        for row in rows:
            if name in SETUP:
                SETUP[name](server)
            replay.arm(row["recorded"])
            result = server.decide(name, row["in"])
            server.check(server.DECISIONS[name]["out"], result["answer"])
            hit = all(result["answer"].get(k) == v for k, v in row["expect"].items())
            group = "synthetic" if row["note"].startswith("Synthetic") else "recorded"
            counts[group] += 1
            hits[group] += hit
            correct += hit
            sources[result["source"]] += 1
            elapsed.append(result["ms"])
    server.flush_decisions()
    return {"n": len(rows), "acc": correct / len(rows), "sources": dict(sources), "llm_calls": replay.calls,
            "cost": replay.cost, "p50_ms": statistics.median(elapsed),
            "p95_ms": sorted(elapsed)[math.ceil(.95 * len(elapsed)) - 1],
            **{group: {"n": counts[group], "acc": hits[group] / counts[group] if counts[group] else None}
               for group in ("recorded", "synthetic")}}


def _free_strings(server, schema, value):
    if schema is str:
        return [value] if isinstance(value, str) and len(value) >= 4 else []
    if isinstance(schema, dict) and isinstance(value, dict):
        return [x for k, s in schema.items() for x in _free_strings(server, s, value.get(k))]
    if isinstance(schema, list) and isinstance(value, list):
        return [x for v in value for x in _free_strings(server, schema[0], v)]
    if isinstance(schema, tuple):
        for member in schema:
            try:
                server.check(member, value)
                return _free_strings(server, member, value)
            except ValueError:
                pass
    return []


def _closed_strings(schema):
    if isinstance(schema, str):
        return {schema}
    members = schema.values() if isinstance(schema, dict) else schema if isinstance(schema, (list, tuple)) else ()
    return {value for member in members for value in _closed_strings(member)}


def check_contract(testcase, server, name, fixtures=None):
    spec = server.DECISIONS[name]
    rows = fixture_rows(server, name, fixtures)
    testcase.assertGreaterEqual(len(rows), 20)
    for row in rows:
        testcase.assertEqual(set(row), {"in", "expect", "recorded", "note"})
        testcase.assertTrue(row["expect"], "empty expect")
        testcase.assertNotIn("TODO", row["note"])
        testcase.assertTrue(row["recorded"] is None or isinstance(row["recorded"], dict))
        server.check(spec["inp"], row["in"])
    reset(server)
    if name in SETUP:
        SETUP[name](server)
    metrics = run_type(server, name, fixtures)
    testcase.assertGreaterEqual(metrics["acc"], spec["min_acc"], metrics)
    if spec["serving"]:
        testcase.assertLessEqual(metrics["p95_ms"], SERVING_P95_MS, metrics)
    for key, schema in spec["inp"].items():
        for wrong in (None, False, [], {}, 0, ""):
            try:
                server.check(schema, wrong)
            except ValueError:
                with testcase.assertRaises(ValueError):
                    server.decide(name, {**rows[0]["in"], key: wrong})
                break
    server.flush_decisions()
    text = "".join(p.read_text() for p in (Path(server.DATA) / "decisions.jsonl", Path(server.DATA) / "decide_cache.json") if p.exists())
    for row in rows:
        for schema, value in ((spec["inp"], row["in"]), (spec["out"], row["expect"])):
            for string in _free_strings(server, schema, value):
                # Public op suffixes such as fewer can also be literal command values.
                enums = _closed_strings(spec['out'])
                if string not in enums and not any(e.endswith('_'+string) for e in enums):
                    testcase.assertNotIn(string, text)
    if spec["serving"]:
        reset(server)
        with patch("urllib.request.urlopen", side_effect=AssertionError("serving spent")) as network, \
                patch.object(server, "_connector_open", side_effect=AssertionError("serving spent")) as outbound:
            for row in rows:
                server.decide(name, row["in"])
            network.assert_not_called()
            outbound.assert_not_called()
    if spec["llm"]:
        row = next(r for r in rows if r["recorded"] and
                   (not spec["rule"] or spec["rule"](spec["norm"](r["in"]), spec["params"]) is None))
        for gate in ("lane", "month", "cap", "over"):
            reset(server)
            with ReplayLLM(server) as replay:
                replay.arm(row["recorded"])
                with server._usage_lock:
                    if gate == "lane":
                        server._usage["calls"][spec["lane"] + ":other"] = server.AI_CAPS[spec["lane"]]
                    elif gate == "month":
                        server._usage["month_cost"] = server.AI_MONTHLY_USD
                    elif gate == "cap":
                        if spec["cap"] is None:
                            continue
                        server._usage["calls"][spec["lane"] + ":" + name] = spec["cap"]
                    else:
                        server._decide_over.add(name)
                result = server.decide(name, row["in"])
                testcase.assertEqual(result["source"], "fallback")
                testcase.assertEqual(result["why"], {"lane": "BudgetExceeded"}.get(gate, gate))
                testcase.assertEqual(replay.calls, 0)
                testcase.assertTrue(server.flush_decisions())
                testcase.assertIn(result["receipt"], Path(server.DATA, "decisions.jsonl").read_text())
        reset(server)
        with ReplayLLM(server) as replay:
            replay.arm(row["recorded"])
            first = server.decide(name, row["in"])
            testcase.assertEqual(first["source"], "llm")
            testcase.assertEqual(server.decide(name, row["in"])["source"], "cache")
            with patch.dict(spec, {"ver": spec["ver"] + 1}):
                replay.arm(row["recorded"])
                testcase.assertEqual(server.decide(name, row["in"])["source"], "llm")
            testcase.assertEqual(replay.calls, 2)
    server.flush_decisions()
    return metrics


def synthetic_model(server):
    """Calibrated, synthetic household with private-title and creator canaries."""
    s = server
    s.person_loves = lambda: []
    entries = []
    for i, (title, by) in enumerate((("BenTitleSentinel", ["ben"]),
                                     ("AnaTitleSentinel", ["ana"]),
                                     ("SharedTitleSentinel", ["together"])), 900002):
        entries.append({"key": f"tmdb:{i}", "kind": "movie", "title": title, "year": "2020",
                        "by": by, "who": "told", "signal": "loved", "weight": 4, "flags": []})
    s._profiles["household"] = {"entries": entries, "seen": set(), "negative": set()}
    s._profiles["cal"] = {"entries": [], "seen": set(), "negative": set()}
    shared = {"x:cozy_case": .8, "k:9001": .5}
    paff = {"ben": {**shared, "d:9001": .9},
            "ana": {"x:whodunit": .8, "d:9002": .9}, "together": shared}
    aff = {**paff["ben"], **paff["ana"], "k:9005": .7}
    names = {"x:cozy_case": "Cozy puzzle", "x:whodunit": "Private puzzle", "k:9001": "Shared theme",
             "k:9005": "Unanchored theme", "d:9001": "BenCreatorSentinel", "d:9002": "AnaCreatorSentinel"}
    holders = {"x:cozy_case": [(4, "tmdb:900004"), (4, "tmdb:900002")],
               "k:9001": [(4, "tmdb:900004")], "d:9001": [(4, "tmdb:900002")],
               "x:whodunit": [(4, "tmdb:900003")], "d:9002": [(4, "tmdb:900003")]}
    bg = [i / 100 for i in range(-30, 31, 3)]
    m = {"aff": aff, "paff": paff, "names": names, "holders": holders,
         "ent": {e["key"]: (e["title"], e["by"]) for e in entries},
         "kinds": {e["key"]: e["kind"] for e in entries}, "vec": None, "pvec": {}, "vbase": 0.,
         "bscale": 1., "pbscale": {}, "x": {}, "px": {}, "cal": len(bg), "bg": bg,
         "pbg": {p: bg for p in (*s.PEOPLE, "together")}, "nexp": {p: 40 for p in s.PEOPLE}}
    s._model["m"] = m
    s.get_model = lambda build=True: m
    for i in range(900001, 900013):
        kind = "tv" if i >= 900009 else "movie"
        s._facets[f"tmdb:{i}"] = {"title": f"Synthetic Pick {i}", "kind": kind,
                                  "x": {"cozy_case": 2}, "k": [[9001, names["k:9001"]]], "d": []}
    for e in entries:
        s._facets[e["key"]]["title"] = e["title"]
    s._facets["tmdb:900003"].update(x={"whodunit": 2}, k=[], d=[[9002, names["d:9002"]]])
    s._facets["tmdb:900005"].update(x={}, k=[[9005, names["k:9005"]]])
    return m


SETUP = {"explain_pick": synthetic_model, "mood_pick": reset, "theme_source": reset, "theme_season": reset, "rank_titles": synthetic_model}


def write_legacy(data):
    """Synthetic tests explicitly opt into legacy DATA mode."""
    from skald import members
    members.configure(str(data), readonly=True)
    Path(data, 'legacy.json').write_bytes((ROOT / 'tests/fixtures/events/data/legacy.json').read_bytes())
