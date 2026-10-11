#!/usr/bin/env python3
"""Live-calibration simulation, offline: counts and hashes only."""
import argparse
import os
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.engine import Engine


def simulation(snapshot, source):
    with Engine(snapshot, server_path=source) as e:
        s, m = e._server, e._model
        legacy = not hasattr(s, "maturity")
        if legacy:
            s.person_score = s.score_key
            s.maturity = lambda model, person: {"state": "unguarded", "explicit": model["nexp"].get(person, 0), "need": 20}
        unseen = sorted(k for k, rec in s._facets.items() if "@" not in k and k not in e._profile["seen"] and rec.get("kind") in s.KIND)
        bg = random.Random(7).sample(unseen, 80)
        m["bg"] = sorted(s.score_key(m, k, kind=s._facets[k].get("kind"))[0] for k in bg)
        for p in s.PEOPLE:
            scorer = getattr(s, "person_score", s.score_key)
            m["pbg"][p] = sorted(scorer(m, k, p, s._facets[k].get("kind"))[0] for k in bg)
        m["cal"] = len(m["bg"])
        probe = random.Random(11).sample(unseen, 300)
        result = {}
        for p in s.PEOPLE:
            views = [s.title_view(k, s._facets[k].get("kind"), p) for k in probe]
            picks = [s.explain_pick_rule({"key": k, "kind": s._facets[k].get("kind"), "person": p}, {})[0] for k in probe]
            scores = [e.score(k, p) for k in probe]
            matched = [(v["match"], v["confidence"]) for v in views]
            result[p] = {"explicit": m["nexp"][p], "state": s.maturity(m, p)["state"] if hasattr(s, "maturity") else "unguarded",
                "title_view": sum(v["match"] is not None for v in views), "explain_pick": sum(v["match"] is not None for v in picks),
                "match_note": sum(bool(s.match_note(k, person=p, kind=s._facets[k].get("kind"))) for k in probe),
                "engine_score": sum(v["percentile"] is not None for v in scores),
                "median": statistics.median(v[0] for v in matched if v[0] is not None) if any(v[0] is not None for v in matched) else None,
                "match_confidence_sha256": hashlib.sha256(json.dumps(matched).encode()).hexdigest()}
            if not legacy:
                assert all(v["maturity"] == s.maturity(m, p) for v in (*views, *picks, *scores))
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot", default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    p.add_argument("--before", type=Path, required=True)
    args = p.parse_args()
    before, after = simulation(args.snapshot, args.before), simulation(args.snapshot, ROOT / "server.py")
    print(json.dumps({"before": before, "after": after}, indent=2))
    for person, block in after.items():
        if block["explicit"] < 20:
            assert all(block[k] == 0 for k in ("title_view", "explain_pick", "match_note", "engine_score"))
        if block["explicit"] >= 40:
            assert block["match_confidence_sha256"] == before[person]["match_confidence_sha256"]


if __name__ == "__main__":
    main()
