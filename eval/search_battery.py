#!/usr/bin/env python3
"""Run ordered player searches against live Skald, with concurrent movie/series lanes."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request


AI_MARK = "✨ "
LANES = ("movie", "series")
SESSION = [
    dict(group="searches", q="recommend cozy movies and series for tonight", movie="some", series="some"),
    dict(group="searches", q="Inception", movie="some", series="none"),
    dict(group="searches", q="etwas Spannendes für heute Abend", movie="some", series="some"),
    dict(group="searches", q="Dark", movie="any", series="some"),
    dict(group="searches", q="Arrivl", movie="some", series="any"),
    dict(group="searches", q="Kaulitz", movie="none", series="some"),
    dict(group="searches", q="Tom Hanks", movie="some", series="any"),
    dict(group="searches", q="movies and series I haven't seen yet", movie="some", series="some"),
    dict(group="searches", q="Filme und Serien, die ich schon gesehen habe", movie="any", series="any"),
    dict(group="searches", q="best movies and series of all time", movie="some", series="some"),
    dict(group="searches", q="Preporuči mi nešto opuštajuće za večeras", movie="some", series="some"),
    dict(group="searches", q="Recomiéndame películas y series de misterio", movie="some", series="some"),
    dict(group="searches", q="Quero filmes e séries para relaxar", movie="some", series="some", typing=True),
    dict(group="searches", q="recommend tense movies and series for tonight", movie="some", series="some"),
    dict(group="followups", q="more like the second one", movie="some", series="some"),
    dict(group="followups", q="but less violent", movie="some", series="some"),
    dict(group="followups", q="also some series", movie="any", series="some"),
    dict(group="followups", q="save this as a row", movie="any", series="any"),
    dict(group="followups", q="I like these, save them as my taste", movie="any", series="any"),
    dict(group="searches", q="Interstellar", movie="some", series="none"),
    dict(group="searches", q="something funny for tonight", movie="some", series="some"),
    dict(group="controls", q="help", movie="any", series="any"),
    dict(group="searches", q="recommend thoughtful documentaries", movie="some", series="some"),
    dict(group="controls", q="history", movie="any", series="any"),
    dict(group="searches", q="recommend exciting adventures", movie="some", series="some"),
    dict(group="controls", q="settings", movie="any", series="any"),
    dict(group="searches", q="recommend gentle comedies", movie="some", series="some"),
    dict(group="controls", q="ideas", movie="any", series="any"),
]


def check_query(query):
    text = " ".join(query.casefold().split())
    if re.fullmatch(r"(?:yes|no) \d{2}", text) or text in ("y", "ye", "yes", "ja", "ok") or re.match(
            r"(?:undo|redo|reset|stop|remove|delete|clear|forget|hide|block|unblock|enable|disable|"
            r"rückgängig|bring back|do again|wiederholen|nochmal)\b", text) \
            or text.endswith(" zurücksetzen"):
        raise ValueError(f"refused unsafe query: {query!r}")


def prefixes(step):
    return [step["q"][:n] for n in (3, 8, 15) if n < len(step["q"])] if step.get("typing") else []


def search_catalog(base, lang):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base.rstrip('/')}/{lang}/manifest.json", timeout=40) as response:
        manifest = json.load(response)
    for catalog in manifest['catalogs']:
        if catalog.get('type') in LANES and any(extra.get('name') == 'search' for extra in catalog.get('extra', [])):
            return catalog['id']
    raise ValueError('Manifest has no search catalog')


def fetch(base, lang, lane, query, gate=None, *, catalog):
    check_query(query)
    url = f"{base.rstrip('/')}/{lang}/catalog/{lane}/{urllib.parse.quote(catalog, safe='')}/search={urllib.parse.quote(query, safe='')}.json"
    if gate is not None:
        gate.wait()
    started = time.monotonic()
    names, error = [], None
    try:
        # Keep player traffic direct, including loopback runs on hosts with HTTP proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=40) as response:
            body = json.load(response)
        if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("metas"), list):
            raise ValueError("invalid catalog JSON")
        for meta in body["metas"]:
            if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
                raise ValueError("invalid catalog meta")
        names = [meta["name"] for meta in body["metas"]]
    except (OSError, ValueError, TypeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return dict(wall=time.monotonic() - started, count=len(names), names=names, error=error)


def log_fields(path, query):
    if path is None:
        return {}
    newest = {}
    try:
        with path.open(encoding="utf-8") as log:
            for line in log:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("q") == query:
                    newest = {k: v for k, v in record.items() if k not in ("t", "q")}
    except OSError:
        pass
    return newest


def run_step(args, step, log):
    typed = prefixes(step)
    with ThreadPoolExecutor(max_workers=len(typed) + 2) as pool:
        for query in typed:
            pool.submit(fetch, args.base, args.lang, "movie", query, catalog=args.catalog)
            time.sleep(.15)
        gate = threading.Barrier(2)
        pending = {lane: pool.submit(fetch, args.base, args.lang, lane, step["q"], gate, catalog=args.catalog) for lane in LANES}
        lanes = {lane: future.result() for lane, future in pending.items()}
    for lane, result in lanes.items():
        flags = []
        if result["error"]:
            flags.append("ERR")
        elif step[lane] == "some" and result["count"] == 0:
            flags.append("EMPTY")
        elif step[lane] == "none" and result["count"] > 0:
            flags.append("FILLER")
        if result["wall"] >= args.deadline:
            flags.append("DEADLINE")
        if result["wall"] >= args.slow:
            flags.append("SLOW")
        if step["group"] == "controls" and any(name.startswith(AI_MARK) for name in result["names"]):
            flags.append("CONTROL")
        result["flags"] = flags
    flags = list(dict.fromkeys(flag for result in lanes.values() for flag in result["flags"]))
    if all(step[lane] == "some" for lane in LANES) and \
            ((lanes["movie"]["count"] == 0) != (lanes["series"]["count"] == 0)):
        flags.append("SPLIT")
    return dict(group=step["group"], q=step["q"], expect={lane: step[lane] for lane in LANES},
                lanes=lanes, flags=flags, timing=log_fields(log, step["q"]))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def print_step(row, out):
    parts = [f"{row['group']} {compact(row['q'])}"]
    for lane, result in row["lanes"].items():
        parts.append(f"{lane} {result['wall']:.3f}s count={result['count']} "
                     f"names={compact([name[:40] for name in result['names'][:3]])} flags={compact(result['flags'])}")
        if result["error"]:
            parts.append(f"{lane} error={compact(result['error'])}")
    parts.extend((f"flags={compact(row['flags'])}", f"timing={compact(row['timing'])}"))
    print(" | ".join(parts), file=out, flush=True)


def summarize(results):
    wall = {}
    for lane in LANES:
        values = sorted(row["lanes"][lane]["wall"] for row in results)
        wall[lane] = dict(p50=statistics.median(values) if values else None,
                          p90=values[math.ceil(.9 * len(values)) - 1] if values else None,
                          max=max(values) if values else None)
    return dict(steps=len(results), flags=dict(Counter(flag for row in results for flag in row["flags"])), wall=wall)


def main(argv=None, *, out=None):
    out = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="live Skald base, kept fixed for the session")
    parser.add_argument("--lang", default="de-DE", help="catalog language, kept fixed for the session")
    parser.add_argument("--catalog", help="search catalog id override; otherwise read it once from the target manifest")
    parser.add_argument("--only", choices=("searches", "followups", "controls", "all"), default="all",
                        help="select a group in SESSION order; use all to retain parent context")
    parser.add_argument("--deadline", type=float, default=6.8, help="flag lane wall times at or above this many seconds")
    parser.add_argument("--slow", type=float, default=5, help="warn at or above this many seconds")
    parser.add_argument("--log", type=Path, help="join latest matching searches.jsonl line; default: $DATA_DIR/searches.jsonl")
    parser.add_argument("--json", type=Path, help="save all scored results and summary")
    args = parser.parse_args(argv)
    session = [step for step in SESSION if args.only == "all" or step["group"] == args.only]
    try:
        for step in session:
            for query in (step["q"], *prefixes(step)):
                check_query(query)
    except ValueError as exc:
        print(str(exc), file=out)
        return 1
    if args.catalog is None:
        try:
            args.catalog = search_catalog(args.base, args.lang)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"Cannot read search catalog: {type(exc).__name__}", file=out)
            return 1
    log = args.log
    if log is None and os.environ.get("DATA_DIR"):
        candidate = Path(os.environ["DATA_DIR"]) / "searches.jsonl"
        if candidate.is_file():
            log = candidate
    results = []
    for step in session:
        row = run_step(args, step, log)
        results.append(row)
        print_step(row, out)
    summary = summarize(results)
    print(f"summary steps={summary['steps']} flags={compact(summary['flags'])} wall_seconds={compact(summary['wall'])}", file=out)
    if args.json:
        args.json.write_text(json.dumps(dict(results=results, summary=summary), ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return int(any(flag != "SLOW" for row in results for flag in row["flags"]))


if __name__ == "__main__":
    raise SystemExit(main())
