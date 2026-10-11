#!/usr/bin/env python3
"""Offline pop × entropy interview pool; prints keys, never household titles."""
import argparse
import os
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.engine import Engine


def pick(s):
    candidates = []
    for slot, rec in sorted(s._facets.items()):
        key = slot.split("@", 1)[0]
        kind = rec.get("kind") or s.profile_kind(key)
        if kind not in s.KIND or not s.femb(key, kind) or not s.frec(key, kind):
            continue
        for x, strength in (rec.get("x") or {}).items():
            if x in s.EXPERIENCES and strength >= 3:
                candidates.append((key, kind, x, "votes" if rec.get("vote_count") is not None else "proxy"))
    candidates = list(dict.fromkeys(candidates))
    ranked = s.interview_rank(candidates)
    order = {a: i for i, a in enumerate(ranked)}
    selected, used = [], set()
    def take(a):
        selected.append(a)
        used.add(s.franchise_key({"id": a[0], "type": "movie" if a[1] == "movie" else "series"}))
    def available(a):
        return s.franchise_key({"id": a[0], "type": "movie" if a[1] == "movie" else "series"}) not in used
    counts = Counter(a[2] for a in candidates)
    for x in sorted(s.EXPERIENCES, key=lambda x: (counts[x], x)):
        for a in sorted((a for a in candidates if a[2] == x), key=order.get):
            if available(a):
                take(a)
                if sum(a[2] == x for a in selected) == 2:
                    break
    for a in ranked:
        kinds = Counter(a[1] for a in selected)
        if len(selected) >= 86 and all(kinds[k]/len(selected) >= .35 for k in s.KIND):
            break
        if available(a) and (len(selected) < 86 or kinds[a[1]]/len(selected) < .35):
            take(a)
    return tuple(selected)


def validate(s, anchors):
    errors, franchises, decades = [], [], set()
    for key, kind, x, pop in anchors:
        rec = s.frec(key, kind)
        if not rec or s.femb(key, kind) is None or rec.get("x", {}).get(x, 0) < 3:
            errors.append("missing facets, embedding or strength-3 experience")
        franchises.append(s.franchise_key({"id": key, "type": "movie" if kind == "movie" else "series"}))
        year = str(rec.get("y") or "")
        if year[:4].isdigit():
            decades.add(int(year[:4])//10)
    coverage, kinds = Counter(a[2] for a in anchors), Counter(a[1] for a in anchors)
    missing = {x: coverage[x] for x in s.EXPERIENCES if coverage[x] < 2}
    if missing:
        errors.append("experience coverage: " + str(missing))
    if len(anchors) < 86:
        errors.append("fewer than 86 anchors")
    if len(set(franchises)) != len(franchises):
        errors.append("duplicate franchise")
    if any(kinds[k] < .35*len(anchors) for k in s.KIND):
        errors.append("kind balance below 35%")
    if len(decades) < 3:
        errors.append("fewer than three decades")
    return {"anchors": len(anchors), "experiences": len(coverage), "kinds": dict(kinds),
            "decades": len(decades), "proxy_share": sum(a[3] == "proxy" for a in anchors)/max(1, len(anchors)),
            "errors": errors}


def unseen(s, anchors):
    seen = {e["key"] for e in s.person_entries(s._profiles.get("household") or {"entries": []}, s.PEOPLE[1])}
    def first20(pool):
        served, used, rated = [], set(), set()
        for i in range(20):
            if i == 12:
                used.clear()
            left = [a for a in pool if a[2] not in used and a[:2] not in rated]
            if not left:
                break
            a = next((a for a in left if a[1] == s.KIND[(i % 12) % 2]), left[0])
            served.append(a)
            used.add(a[2])
            rated.add(a[:2])
        return sum(a[0] not in seen for a in served)/max(1, len(served))
    baseline = sorted(anchors, key=lambda a: (list(s.EXPERIENCES).index(a[2]), a[0], a[1]))
    return {"pop_entropy": first20(s.interview_rank(anchors)), "experience_order": first20(baseline), "target": .30}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    with Engine(args.snapshot) as e:
        s = e._server
        anchors = s.ANCHORS if args.check else pick(s)
        print("ANCHORS = (")
        for a in anchors:
            print(f"    {a!r},")
        print(")")
        stats = validate(s, anchors)
        print(stats)
        print("unseen_rate:", unseen(s, anchors))
        return int(bool(stats["errors"])) if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
