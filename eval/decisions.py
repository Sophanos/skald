#!/usr/bin/env python3
"""Offline decision replay, opt-in live routing checks and generated CLI/API skill documentation."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from decide_kit import close_server, fixture_rows, load_server, reset, run_type, SERVING_P95_MS

BEGIN = "<!-- skald:decide-api:begin -->"
END = "<!-- skald:decide-api:end -->"


def live_cases():
    return [json.loads(line) for line in (ROOT / "eval/fixtures/decisions/route_query_live.jsonl").read_text().splitlines()]


def route_input(row):
    return {"query": row["q"], **{k: row[k] for k in ("last", "shown") if k in row}}


def route_score(row, answer):
    expect = {"route": row["route"], "watched": [row.get("watched", "any")], "list": [row.get("list", "taste")]}
    if "previous" in row:
        expect["previous"] = [row["previous"]]
    return {q: answer.get("choice" if q == "route" else q) in choices for q, choices in expect.items()}


def live_report():
    key = os.environ.get("OR_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"skipped": "OR_KEY not set", "pass": True}
    with tempfile.TemporaryDirectory() as data:
        server = load_server(data)
        try:
            server.OR_KEY = key
            rows = live_cases()
            correct, counts, failed, langs = {}, {}, [], {}
            for row in rows:
                inp = route_input(row)
                answer = server.decide("route_query", inp)["answer"]
                scores = route_score(row, answer)
                misses = [q for q, hit in scores.items() if not hit]
                for q, hit in scores.items():
                    counts[q] = counts.get(q, 0) + 1
                    correct[q] = correct.get(q, 0) + hit
                lang = langs.setdefault(row["lang"], [0, 0])
                lang[0] += 1
                lang[1] += not misses
                if misses:
                    failed.append({"in": inp, "questions": misses, "answer": answer})
            accuracy = {q: n / counts[q] for q, n in correct.items()}
            all_three = 1 - len(failed) / len(rows)
            return {"n": len(rows), "accuracy": accuracy, "failed": failed,
                    "all_questions": all_three, "all_three": all_three,
                    "question_counts": counts, "by_lang": {l: {"n": n, "all_questions": hits / n, "all_three": hits / n} for l, (n, hits) in langs.items()},
                    "pass": all_three >= .9}
        finally:
            close_server(server)


def report(kind=None):
    with tempfile.TemporaryDirectory() as data:
        server = load_server(data)
        try:
            if kind is not None and kind not in server.DECISIONS:
                raise ValueError("unknown type")
            out = {}
            for name in ([kind] if kind else server.DECISIONS):
                reset(server)
                spec = server.DECISIONS[name]
                tracemalloc.start()
                try:
                    m = run_type(server, name)
                    m.update(peak_bytes=tracemalloc.get_traced_memory()[1], ver=spec["ver"],
                             min_acc=spec["min_acc"], serving=spec["serving"])
                    m["pass"] = (m["acc"] >= spec["min_acc"] and
                                 (not spec["serving"] or m["p95_ms"] <= SERVING_P95_MS) and
                                 all(r["expect"] and "TODO" not in r["note"] for r in fixture_rows(server, name)))
                    out[name] = m
                finally:
                    tracemalloc.stop()
            return out
        finally:
            close_server(server)


def example_input(schema, i):
    if isinstance(schema, dict):
        return {k: example_input(s, i) for k, s in schema.items()}
    if isinstance(schema, list):
        return [example_input(schema[0], 2 * i + j) for j in range(min(2, schema[1] if len(schema) == 2 else 25))]
    if isinstance(schema, tuple):
        return example_input(schema[i % len(schema)], i)
    if schema == "tmdb:N":
        return f"tmdb:{900001 + i % 99}"
    if schema is str:
        return f"Synthetic text {i}"
    if schema in (int, float, bool):
        return schema(i)
    return schema


def scaffold(name, ranker=False):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        raise ValueError("invalid decision type")
    with tempfile.TemporaryDirectory() as data:
        server = load_server(data)
        try:
            if name not in server.DECISIONS:
                if ranker:
                    fields = "inp=RANK_INP, out=RANK_OUT, norm=rank_norm, rule=rank_rule(), params={}, fallback=rank_fallback)"
                else:
                    fields = ('inp={"ok": bool}, out={"ok": bool}, norm=lambda x: x,\n'
                              '         rule=lambda x, p: (x, 1), params={}, fallback=lambda x, err: ({"ok": False}, 0))')
                return {"block": f'decision("{name}", ver=1, lane=None, cap=None, ttl=DAY, serving=True,\n'
                                 f'         llm=None, requires=(), min_acc=1, api=True,\n         {fields}'}
            spec = server.DECISIONS[name]
            path = Path(spec["fixtures"])
            if not path.is_absolute():
                path = ROOT / path
            rows = [{"in": example_input(spec["inp"], i), "expect": {}, "recorded": None,
                     "note": "Synthetic TODO: fill expect and explain the case"} for i in range(20)]
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x") as file:
                file.writelines(json.dumps(row) + "\n" for row in rows)
            return {"written": str(path), "rows": len(rows)}
        finally:
            close_server(server)


def skill_block():
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with tempfile.TemporaryDirectory() as data:
        server = load_server(data)
        try:
            lines = [BEGIN, "", "## Skald decisions and taste API", "",
                     "A bound token sees its own person + together. Use only the person's authorized CLI or API.",
                     'Search command previews require the exact `yes NN` code shown first in the card name. Open a card to read its details. Plain `yes` and prefixes save nothing. Identical mutation requests within 60 seconds return their first result.', ""]
            for name, spec in sorted(server.DECISIONS.items()):
                if not spec["api"]:
                    continue
                path = Path(spec["fixtures"])
                path = path if path.is_absolute() else ROOT / path
                rows = fixture_rows(server, name) if path.exists() else []
                inp = next((r["in"] for r in rows if r["note"].startswith("Synthetic")), example_input(spec["inp"], 0))
                lines.extend([f"### {name} ({'serving' if spec['serving'] else 'LLM'})",
                              f"- inp: `{compact(server._decision_json_schema(spec['inp']))}`",
                              f"- out: `{compact(server._decision_json_schema(spec['out']))}`",
                              f"- CLI: `python3 -m skald decide {name} --in '{compact(inp)}'`",
                              f"- API: `python3 -m skald decide {name} --remote --in '{compact(inp)}'`", ""])
            for route in server.routes_view(None)["routes"]:
                methods = ", ".join(f"{m} → {scope}" for m, scope in sorted(route["methods"].items()))
                lines.append(f"- `{route['path']}`: {methods}; query {compact(route['query'])}; "
                             f"tail {compact(route['tail'])}; example `{compact(route['example'])}`")
            return "\n".join([*lines, "", END])
        finally:
            close_server(server)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--type")
    mode.add_argument("--new", metavar="NAME")
    mode.add_argument("--skill", action="store_true")
    p.add_argument("--ranker", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--live", action="store_true", help="Check route_query with the real decider; skip without OR_KEY")
    args = p.parse_args(argv)
    if args.ranker and not args.new or args.write and not args.skill:
        p.error("--ranker needs --new; --write needs --skill")
    if args.live and args.type != "route_query":
        p.error("--live needs --type route_query")
    if args.skill:
        block = skill_block()
        if args.write:
            path = ROOT / "skills/skald-taste/SKILL.md"
            text = path.read_text()
            if BEGIN not in text and END not in text:
                text = text.rstrip() + "\n\n" + block + "\n"
            elif text.count(BEGIN) == text.count(END) == 1 and text.index(BEGIN) < text.index(END):
                text = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]
            else:
                raise ValueError("invalid skill markers")
            path.write_text(text)
        print(block)
        return 0
    data = {"route_query": live_report()} if args.live else scaffold(args.new, args.ranker) if args.new else report(args.type)
    if args.json:
        print(json.dumps(data, sort_keys=True))
    elif args.new:
        print(data.get("block") or json.dumps(data, sort_keys=True))
    else:
        for name, metrics in data.items():
            print(name + ": " + json.dumps(metrics, sort_keys=True))
    return 0 if args.new else int(any(not m["pass"] for m in data.values()))


if __name__ == "__main__":
    raise SystemExit(main())
