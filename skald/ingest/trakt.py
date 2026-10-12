from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
from . import Candidate, PRIORITY, clean_title, day, rating_signal, iter_json_array


SOURCE = {"hint": "Trakt JSON export file or folder", "dir": True, "profiles": False}

def parse(path, stats=None, **kwargs):
    path = Path(path)
    files = [path] if path.is_file() else sorted(path.rglob("*.json"))
    if not files:
        raise ValueError("no supported Trakt JSONs")
    grouped, stamps = {}, []
    for file in files:
        with file.open(encoding="utf-8") as fh:
            first = fh.read(1)
            while first and first.isspace():
                first = fh.read(1)
            fh.seek(0)
            if first == "[":
                groups = {file.stem.casefold(): iter_json_array(fh)}
            else:
                data = json.load(fh)
                groups = data if isinstance(data, dict) else {file.stem.casefold(): data}
            for group, rows in groups.items():
                if first != "[" and not isinstance(rows, list):
                    if stats is not None:
                        stats["skipped_trakt"] = stats.get("skipped_trakt", 0) + 1
                    continue
                for row in rows:
                    item = (row.get("movie") or row.get("show") or row) if isinstance(row, dict) else None
                    if not isinstance(item, dict) or not item.get("title"):
                        if stats is not None:
                            stats["skipped_trakt"] = stats.get("skipped_trakt", 0) + 1
                        continue
                    kind = "movie" if row.get("movie") or row.get("type") == "movie" else "tv"
                    ids = {k: v for k, v in (item.get("ids") or {}).items() if k in ("tmdb", "imdb") and v}
                    if str(row.get("id", "")).startswith("tmdb:"):
                        ids["tmdb"] = int(row["id"][5:])
                    c = Candidate(clean_title(item.get("title", "")), str(item.get("year") or ""), kind, ids, src="trakt")
                    key = (kind, str(ids.get("tmdb") or ids.get("imdb") or c.title.casefold()), c.year)
                    entry = grouped.setdefault(key, {"c": c, "explicit": [], "ratings": [], "episodes": set(),
                                                      "history": 0, "watched": 0, "plays": 0, "last": "", "t": "",
                                                      "aired": None, "hidden": False, "interest": True})
                    entry["c"] = replace(entry["c"], ids={**entry["c"].ids, **ids})
                    stamp = day(row.get("watched_at") or row.get("rated_at") or row.get("listed_at") or row.get("last_watched_at"))
                    stamps.append(stamp)
                    entry["t"] = max(entry["t"], stamp)
                    # Row shape takes precedence over filenames used by different exporters.
                    group = str(group).casefold()
                    shape = "history" if row.get("watched_at") else "ratings" if "rating" in row and row.get("rated_at") else \
                            "watchlist" if row.get("listed_at") else "watched" if "seasons" in row else group
                    watchlist = "watchlist" in shape
                    episode = row.get("episode") or row.get("type") == "episode"
                    if row.get("rating") not in (None, ""):
                        if episode:
                            entry["ratings"].append(float(row["rating"]))
                            rating_signal(row["rating"])
                        else:
                            entry["explicit"].append((rating_signal(row["rating"]), stamp))
                    if row.get("favorite") or row.get("likes"):
                        entry["explicit"].append(("love", stamp))
                    if "history" in shape or row.get("watched_at"):
                        entry["history"] += int(row.get("episodes_played") or 1)
                        ep = row.get("episode")
                        if isinstance(ep, dict) and ep.get("number") is not None:
                            entry["episodes"].add((ep.get("season"), ep["number"]))
                        entry["last"] = max(entry["last"], stamp)
                    for season in row.get("seasons", []):
                        for ep in season.get("episodes", []):
                            entry["episodes"].add((season.get("number"), ep.get("number")))
                            last = day(ep.get("last_watched_at"))
                            stamps.append(last)
                            entry["last"] = max(entry["last"], last)
                            entry["t"] = max(entry["t"], last)
                            entry["plays"] = max(entry["plays"], int(ep.get("plays") or 1))
                    entry["watched"] = max(entry["watched"], int(row.get("episodes_played") or 0))
                    if kind == "movie":
                        entry["plays"] = max(entry["plays"], int(row.get("plays") or 0))
                    entry["last"] = max(entry["last"], day(row.get("last_watched_at")))
                    aired = row.get("aired_episodes", item.get("aired_episodes"))
                    if aired is not None:
                        entry["aired"] = int(aired)
                    entry["hidden"] |= any(word in group or word in file.stem.casefold() for word in ("hidden", "dropped"))
                    entry["interest"] &= watchlist
    if not grouped:
        raise ValueError("no supported Trakt rows")
    newest = max(stamps, default="")
    cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=60)).isoformat() if newest else ""
    out = []
    for entry in grouped.values():
        c = entry["c"]
        n = max(1, len(entry["episodes"]), entry["watched"], entry["history"],
                entry["plays"] if c.kind == "movie" else 0)
        signal, conf = ("interest" if entry["interest"] else "seen"), 1.
        if entry["explicit"]:
            signal, stamp = max(entry["explicit"], key=lambda r: (PRIORITY[r[0]], r[1]))
            entry["t"] = stamp
        elif len(entry["ratings"]) >= 3:
            signal, conf = rating_signal(sum(entry["ratings"]) / len(entry["ratings"])), .7
        else:
            if entry["plays"] >= 2:
                signal, conf = "like", .7
            stale = bool(entry["last"] and entry["last"] < cutoff)
            watched = len(entry["episodes"]) or max(entry["watched"], entry["history"])
            if c.kind == "tv" and stale and entry["aired"] is not None:
                if entry["aired"] > 0 and watched / entry["aired"] < .25:
                    signal, conf = "abandon", .5
            elif c.kind == "tv" and stale and 0 < watched <= 2:
                signal, conf = "abandon", .4
            if signal != "abandon" and entry["hidden"]:
                signal, conf = "abandon", .8
        out.append(replace(c, signal=signal, conf=conf, n=n, last_ts=entry["t"]))
    return out
