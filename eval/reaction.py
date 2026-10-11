#!/usr/bin/env python3
"""Offline reaction diagnostics. Threshold failures are measurements, not exit failures."""
import argparse
import copy
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from skald.engine import Engine

CHECKS = ("K1", "K2", "K2b", "K3", "K4", "K5")
NOW = 1791734400.


def kendall(a, b):
    common = set(a) & set(b)
    order = {key: i for i, key in enumerate(b)}
    positions = [order[key] for key in a if key in common]
    pairs = len(positions)*(len(positions)-1)//2
    inversions = sum(x > y for i, x in enumerate(positions) for y in positions[i+1:])
    return 1 - 2*inversions/pairs if pairs else 1.


def percentile(values, q):
    values = sorted(values)
    if not values:
        return 0.
    i = (len(values)-1)*q
    lo = int(i)
    return values[lo] + (values[min(lo+1, len(values)-1)] - values[lo])*(i-lo)


def related_map(engine, keys):
    s = engine._server
    facets = {key: {fid for fid, _ in s.facet_pairs(s.frec(key, engine._index.get(key, {}).get("kind")))
                    if fid[0] in "xktdacn"} for key in keys}
    holders = {}
    for key, fids in facets.items():
        for fid in fids:
            holders.setdefault(fid, set()).add(key)
    out = {}
    for key, fids in facets.items():
        counts = {}
        for fid in fids:
            for other in holders[fid]:
                if other != key:
                    counts[other] = counts.get(other, 0) + 1
        out[key] = {other for other, count in counts.items() if count >= 2}
    return out


def probes(engine, person, n=20, seed=7):
    keys = sorted({k for k in set(engine._server._facets) | set(engine._server._emb) if "@" not in k}
                  - engine._profile["seen"])
    related = related_map(engine, keys)
    order = engine._rank(engine._model, keys, person)
    # Deciles 3 through 8, ascending score; exclude the pre-love top 30 as well.
    eligible = sorted(k for i, k in enumerate(order) if len(order)*.2 <= i < len(order)*.8 and i >= 30
                      and len(related[k]) >= 5)
    return random.Random(seed).sample(eligible, min(n, len(eligible))), related


@contextmanager
def model_view(engine, profile, model, state=None):
    s = engine._server
    original, old_stats = s._profiles.get("household"), s._seed_stats
    s._profiles["household"] = profile
    s.clear_seed_cache()
    try:
        with patch.object(s, "get_model", return_value=model), \
                patch.object(s, "feedback_state", return_value=state or s.feedback_state()):
            yield
    finally:
        s._profiles["household"] = original
        s._seed_stats = old_stats
        s.clear_seed_cache()


def served(engine, person):
    s = engine._server
    return {cid if "tv" in types or len(types) == 1 else cid+":"+typ:
            [meta["id"] for meta in s.row_metas(cid, typ, "de-DE", who=person)]
            for cid, types, _ in s.feed_rows("de-DE", profiles=False)
            for typ in (("tv",) if "tv" in types else types)}


def feedback_model(engine, key, person, action="love", reasons=()):
    s = engine._server
    rec = engine._index[key]
    body = s.validate_feedback({"key": key, "kind": rec["kind"], "title": rec["title"],
                               "year": rec["year"], "who": person, "action": action, "reasons": list(reasons)})
    active, tonight = s.fold_feedback(s.read_feedback() + [{**body, "t": time.time(), "id": "f"*32}])
    state = {"active": active, "tonight": tonight,
             "blocked": {key for (key, _), ev in active.items() if ev["action"] == "not_for_us"}}
    p = copy.deepcopy(engine._profile)
    with patch.object(s, "feedback_state", return_value=state):
        s.merge_feedback(p["entries"], {e["key"]: e for e in p["entries"]}, time.time())
        p = engine._profile_with(p)
        model = engine._build(p)
    return p, model, state


def latency(engine, key, person):
    s = engine._server
    path = engine._work / "feedback.jsonl"
    raw = path.read_bytes() if path.exists() else None
    profiles, model, learn, seed_stats = dict(s._profiles), dict(s._model), copy.deepcopy(s._learn), s._seed_stats
    try:
        s.refresh_profiles()
        s.get_model()
        before = [meta["id"] for meta in s.row_metas(rid('foryou', s.CONFIG), "tv", "de-DE", who=person)]
        rec = engine._index[key]
        s.write_feedback({"key": key, "kind": rec["kind"], "title": rec["title"], "year": rec["year"],
                          "who": person, "action": "love"})
        for passes in range(1, 4):
            s.refresh_profiles()
            s.get_model()
            after = [meta["id"] for meta in s.row_metas(rid('foryou', s.CONFIG), "tv", "de-DE", who=person)]
            if after != before:
                return passes
        return None
    finally:
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)
        s._remote_cache.clear()
        s._profiles.clear(); s._profiles.update(profiles)
        s._model.clear(); s._model.update(model)
        s._learn.clear(); s._learn.update(learn)
        s._seed_stats = seed_stats
        s.clear_seed_cache()


def measure(engine, key, person, related, pre_rows=None, with_latency=True):
    s, model = engine._server, engine._model
    if pre_rows is None:
        with model_view(engine, engine._profile, model):
            pre_rows = served(engine, person)
    p, after_model, state = feedback_model(engine, key, person)
    universe = {k for k in set(s._facets) | set(s._emb) if "@" not in k} - p["seen"]
    before = engine._rank(model, universe, person)[:30]
    after = engine._rank(after_model, universe, person)[:30]
    entrants = len((set(after) - set(before)) & related)
    with model_view(engine, p, after_model, state):
        post_rows = served(engine, person)
    unrelated = [cid for cid, keys in pre_rows.items() if not set(keys[:24]) & related]
    taus = [kendall(pre_rows[cid], post_rows[cid]) for cid in unrelated]
    leaving = [len(set(pre_rows[cid][:12]) - set(post_rows[cid][:12])) for cid in unrelated]
    old_aff, new_aff = model["paff"][person], after_model["paff"][person]
    largest = max((abs(new_aff.get(fid, 0)-old_aff.get(fid, 0)) for fid in old_aff.keys() | new_aff.keys()), default=0.)
    limit = .5*percentile([v for v in old_aff.values() if v > 0], .99)
    target = next((k for keys in pre_rows.values() for k in keys if k in engine._index), None)
    appearances = None
    if target:
        blocked_p, blocked_model, blocked_state = feedback_model(engine, target, person, "not_for_us", ["too slow"])
        with model_view(engine, blocked_p, blocked_model, blocked_state):
            appearances = sum(keys.count(target) for keys in served(engine, person).values())
    passes = latency(engine, key, person) if with_latency else None
    values = {"K1": entrants, "K2": min(taus, default=1.), "K2b": max(leaving, default=0),
              "K3": largest, "K4": appearances, "K5": passes}
    checks = {"K1": 2 <= entrants <= 10, "K2": all(t >= .70 for t in taus),
              "K2b": all(n <= 3 for n in leaving), "K3": largest <= limit,
              "K4": appearances == 0, "K5": passes is not None and passes <= 1}
    return {"key": key, "values": values, "checks": checks, "K3_limit": limit,
            "unrelated_rows": len(unrelated), "pass": all(checks.values())}


def run(snapshot, person=None, n=20, seed=7, timing=False):
    started = time.perf_counter()
    with patch("time.time", return_value=NOW), redirect_stdout(io.StringIO()), Engine(snapshot) as engine:
        out = {}
        for who in (person,) if person else engine._server.PEOPLE:
            selected, related = probes(engine, who, n, seed)
            with model_view(engine, engine._profile, engine._model):
                rows = served(engine, who)
            results = [measure(engine, key, who, related[key], rows) for key in selected]
            count = len(results)
            rate = sum(r["pass"] for r in results)/count if count else None
            summary = {check: {"pass_rate": sum(r["checks"][check] for r in results)/count if count else None,
                "median": statistics.median([r["values"][check] for r in results if r["values"][check] is not None])
                if any(r["values"][check] is not None for r in results) else None} for check in CHECKS}
            out[who] = {"n": count, "pass_rate": rate, "pass": rate is not None and rate >= .8,
                        "checks": summary, "worst": [r["key"] for r in sorted(results,
                            key=lambda r: (sum(r["checks"].values()), -r["values"]["K3"], r["key"]))[:3]], "probes": results}
    result = {"seed": seed, "people": out}
    if timing:
        result["seconds"] = round(time.perf_counter()-started, 3)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    snapshot = snapshot if snapshot and snapshot.strip() else None
    ap.add_argument("--snapshot", type=Path, default=snapshot, required=not snapshot)
    ap.add_argument("--person")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--latency", action="store_true", help="include total elapsed seconds; K5 is always measured")
    args = ap.parse_args()
    if not 1 <= args.n <= 100:
        ap.error("--n must be between 1 and 100")
    result = run(args.snapshot, args.person, args.n, args.seed, args.latency)
    if args.json:
        print(json.dumps(result, allow_nan=False))
    else:
        for who, report in result["people"].items():
            print(who, "n", report["n"], "pass_rate", report["pass_rate"])
            for check, scores in report["checks"].items():
                print(check, "pass_rate", scores["pass_rate"], "median", scores["median"])
            print("worst", " ".join(report["worst"]))
        if args.latency:
            print("seconds", result["seconds"])


if __name__ == "__main__":
    main()
