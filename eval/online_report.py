"""Replay fetch bursts and raw TV history, without a worker, network or snapshot writes."""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def online_report(snapshot, now, days=21):
    snapshot = Path(snapshot)
    wall = datetime.datetime.fromisoformat(now)
    at = wall.replace(tzinfo=ZoneInfo("Europe/Berlin")).timestamp() if wall.tzinfo is None else wall.timestamp()
    with tempfile.TemporaryDirectory(prefix="skald-online-") as work, \
            patch.dict(os.environ, {"DATA_DIR": work, "TMDB_API": "offline", "OPENROUTER_API_KEY": ""}), \
            patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
        for name in ("learn.json", "feeds.json", "shown.json", "facets.json", "profile.json", "history.json",
                     "feedback.jsonl", "searches.jsonl", "sessions.jsonl", ".skald-format", "skald.json", "legacy.json"):
            if (snapshot / name).is_file():
                shutil.copyfile(snapshot / name, Path(work) / name)
        spec = importlib.util.spec_from_file_location("online_report_server", ROOT / "server.py")
        s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(s)
        try:
            def read(name, default):
                path = Path(work) / (name + ".json")
                return json.loads(path.read_text()) if path.exists() else default
            learn = read("learn", {"served": {}, "rows": {}, "hits": [], "ctx": []})
            kinds = {}
            def metas(value):
                if isinstance(value, dict):
                    if "id" in value and "type" in value:
                        kinds.setdefault(value["id"], "movie" if value["type"] == "movie" else "tv")
                    for child in value.values():
                        metas(child)
                elif isinstance(value, list):
                    for child in value:
                        metas(child)
            metas(read("feeds", {}))
            metas(read("shown", {}))
            facets = read("facets", {}).get("items", {})
            for slot, rec in facets.items():
                if rec.get("kind") in ("movie", "tv"):
                    kinds.setdefault(slot.split("@")[0], rec["kind"])
            by_row = {}
            for key, rec in learn["served"].items():
                for lid, r in rec["rows"].items():
                    by_row.setdefault(lid, []).append((r["first"], key))
            fetches = []
            for lid, records in by_row.items():
                records.sort(key=lambda r: r[0])
                group, first, last = [], None, None
                for t, key in records:
                    if last is not None and t - last >= .1:
                        fetches.append((first, lid, group))
                        group, first = [], None
                    if first is None:
                        first = t
                    group.append({"id": key, "type": kinds.get(key, "unknown")})
                    last = t
                if group:
                    fetches.append((first, lid, group))
            for t, lid, group in sorted(fetches, key=lambda r: r[0]):
                s.note_exposure(lid, group, t=t)
            path = Path(work) / "searches.jsonl"
            if path.exists():
                s._searches = [(r["t"], r["q"]) for r in map(json.loads, path.read_text().splitlines())]
            s.note_tv(read("profile", {"entries": []}), now=at)
            state = s.online_state()
            return {"legacy": {"shown": sum(r["shown"] for r in learn["rows"].values()),
                               "learn_ids": len(learn["rows"]), "titles": len(learn["served"]),
                               "hits": len(learn["hits"]), "ctx": len(learn["ctx"])},
                    "replay": {"observations": len(state["tv"]),
                               "unknown_keys": sum(k.endswith("@unknown") for k in state["keys"])},
                    "online": s.metrics(now=at, days=days)}
        finally:
            for name in ("_pool", "_outer", "_nzb_pool"):
                getattr(s, name).shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    p.add_argument("--snapshot", default=snapshot if snapshot and snapshot.strip() else None)
    p.add_argument("--now", required=True, help="ISO Berlin wall time")
    p.add_argument("--days", type=int, default=21)
    args = p.parse_args()
    if not args.snapshot:
        p.error("use --snapshot or SKALD_SNAPSHOT")
    print(json.dumps(online_report(args.snapshot, args.now, args.days), separators=(",", ":")))
