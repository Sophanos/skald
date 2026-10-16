from dataclasses import replace
import datetime as dt
import json
import io
import sys
import zipfile
from pathlib import Path, PurePosixPath
from . import Candidate, PRIORITY, clean_title, day, rating_signal, iter_json_array


MAX_MEMBER_BYTES = 100 * 1024**2
MAX_TOTAL_BYTES = 300 * 1024**2


SOURCE = {"hint": "Trakt export zip, JSON file or folder", "dir": True, "profiles": False}

def _json_files(path):
    if path.is_file() and path.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if any(member.file_size > MAX_MEMBER_BYTES for member in members):
                    raise ValueError("invalid Trakt zip: member exceeds 100 MB uncompressed")
                if sum(member.file_size for member in members) > MAX_TOTAL_BYTES:
                    raise ValueError("invalid Trakt zip: total exceeds 300 MB uncompressed")
                files = sorted((member for member in members if not member.is_dir()
                                and not PurePosixPath(member.filename).is_absolute()
                                and ".." not in PurePosixPath(member.filename).parts
                                and member.filename.endswith(".json")), key=lambda member: member.filename)
                if not files:
                    raise ValueError("no supported Trakt JSONs")
                total = 0
                for member in files:
                    # ZipExtFile otherwise truncates output at the untrusted declared size.
                    member.file_size = sys.maxsize
                    size = 0
                    with archive.open(member) as raw, io.BytesIO() as buffer:
                        while chunk := raw.read(min(65536, MAX_MEMBER_BYTES - size + 1, MAX_TOTAL_BYTES - total + 1)):
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_MEMBER_BYTES:
                                raise ValueError("invalid Trakt zip: member exceeds 100 MB uncompressed")
                            if total > MAX_TOTAL_BYTES:
                                raise ValueError("invalid Trakt zip: total exceeds 300 MB uncompressed")
                            buffer.write(chunk)
                        buffer.seek(0)
                        with io.TextIOWrapper(buffer, encoding="utf-8") as fh:
                            yield Path(member.filename), fh
        except zipfile.BadZipFile:
            raise ValueError("invalid Trakt zip") from None
    else:
        files = [path] if path.is_file() else sorted(path.rglob("*.json"))
        if not files:
            raise ValueError("no supported Trakt JSONs")
        for file in files:
            with file.open(encoding="utf-8") as fh:
                yield file, fh


def parse(path, stats=None, title_counts=None, **kwargs):
    path = Path(path)
    grouped, stamps = {}, []
    counts = title_counts if title_counts is not None else {}
    for name in ("watched_movies", "watched_series", "watchlist", "ratings", "favorites", "hidden"):
        counts.setdefault(name, set())
    ignored = False
    for file, fh in _json_files(path):
        stem = file.stem.casefold()
        if stem in ("comments", "notes", "likes") or stem.startswith(("collection-", "comments-", "notes-", "likes-")):
            ignored = True
            continue
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
            group = str(group).casefold()
            if group in ("collection", "comments", "notes", "likes") or group.startswith(("collection-", "comments-", "notes-", "likes-")):
                ignored = True
                continue
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
                if row.get("favorite") or row.get("likes") or "favorites" in group or "favorites" in stem:
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
                if "favorites" in group or "favorites" in stem or row.get("favorite"):
                    counts["favorites"].add(key)
                elif watchlist:
                    counts["watchlist"].add(key)
                if row.get("rating") not in (None, ""):
                    counts["ratings"].add(key)
                if entry["hidden"]:
                    counts["hidden"].add(key)
                if any(word in shape for word in ("watched", "history")):
                    counts["watched_movies" if kind == "movie" else "watched_series"].add(key)
    if stats is not None:
        for name, titles in counts.items():
            key = "trakt_" + name
            stats[key] = len(titles)
    if not grouped and not ignored:
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
        elif entry["hidden"]:
            signal, conf = "not_for_us", 1.
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
        out.append(replace(c, signal=signal, conf=conf, n=n, last_ts=entry["t"]))
    return out
