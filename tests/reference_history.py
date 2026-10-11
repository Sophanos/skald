"""Sanitized frozen transforms for adapter parity; synthetic inputs only."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile

DAY = 86_400
BASE_WEIGHT = {"loved": 3, "watched": 2, "interest": 1, "abandoned": -1.5}


def validate_export(data):
    """Reject malformed history before replacing an existing output file."""
    if not isinstance(data, dict) or not isinstance(data.get("exported_at"), str):
        raise ValueError("export must be an object with an exported_at string")
    for kind in ("movies", "shows"):
        rows = data.get(kind)
        if not isinstance(rows, list):
            raise ValueError(f"{kind} must be an array")
        for index, row in enumerate(rows):
            where = f"{kind}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{where} must be an object")
            if type(row.get("tmdb")) is not int or row["tmdb"] <= 0:
                raise ValueError(f"{where}.tmdb must be a positive integer")
            if not isinstance(row.get("title"), str) or not row["title"].strip():
                raise ValueError(f"{where}.title must be a nonempty string")
            if row.get("year") is not None and type(row["year"]) is not int:
                raise ValueError(f"{where}.year must be an integer or null")
            for key in ("imdb", "lang", "status"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    raise ValueError(f"{where}.{key} must be a string or null")
            genres = row.get("genres")
            if genres is not None and (
                not isinstance(genres, list) or any(not isinstance(g, str) for g in genres)
            ):
                raise ValueError(f"{where}.genres must be an array of strings or null")
            key = "plays" if kind == "movies" else "eps"
            plays = row.get(key)
            if not isinstance(plays, list):
                raise ValueError(f"{where}.{key} must be an array")
            if kind == "movies":
                if any(type(minute) is not int for minute in plays):
                    raise ValueError(f"{where}.plays must contain integer epoch minutes")
            else:
                total = row.get("eps_total")
                if total is not None and (type(total) is not int or total < 0):
                    raise ValueError(f"{where}.eps_total must be a nonnegative integer or null")
                for episode in plays:
                    if (
                        not isinstance(episode, list) or len(episode) != 4
                        or any(type(value) is not int for value in episode[:3])
                        or episode[0] < 0 or episode[1] < 0
                    ):
                        raise ValueError(f"{where}.eps needs [season, number, minute, runtime] rows")


def max_session_eps(minutes):
    largest = current = 0
    previous = None
    for minute in sorted(minutes):
        current = current + 1 if previous is not None and minute - previous <= 180 else 1
        largest = max(largest, current)
        previous = minute
    return largest


def import_history(data, now):
    validate_export(data)
    minutes = Counter(minute for movie in data["movies"] for minute in movie["plays"])
    minutes.update(episode[2] for show in data["shows"] for episode in show["eps"])
    # shortcut: same-minute batches of >=5 are backfill; add burst detection if imports spread out.
    backfill = {minute for minute, count in minutes.items() if count >= 5}
    items = []
    for kind in ("movies", "shows"):
        for row in data[kind]:
            series = kind == "shows"
            plays = [episode[2] for episode in row["eps"]] if series else row["plays"]
            real = [minute for minute in plays if minute not in backfill]
            at = max(real) * 60_000 if real else None
            item = {
                "profile": "me", "type": "series" if series else "movie",
                "id": f"tmdb:{row['tmdb']}", "imdb": row.get("imdb"),
                "title": row["title"], "year": row.get("year"),
                "genres": row.get("genres") or [], "lang": row.get("lang"),
                "at": at, "first_at": min(real) * 60_000 if real else None,
                "backfilled": any(minute in backfill for minute in plays),
                "played": bool(plays), "plays": len(plays),
            }
            flags = []
            if series:
                episodes = {tuple(episode[:2]) for episode in row["eps"]}
                count = len(episodes)
                total = row.get("eps_total")
                completion = min(count / total, 1.0) if total else None
                rewatch = len(plays) - count
                session = max_session_eps(real)
                item.update({
                    "episodes_played": count, "episodes_total": total,
                    "completion": completion,
                    "last_episode": list(max(episodes)) if episodes else None,
                    "max_session_eps": session,
                })
                signal = "interest"
                # Returning shows are caught up against aired totals, not future seasons.
                caught_up = row.get("status") == "returning series" and completion == 1.0
                if caught_up or (completion is not None and completion >= 0.9):
                    flags.append("completed")
                    signal = "loved" if count >= 50 or rewatch >= 1 else "watched"
                elif count >= 3 and (completion is None or completion >= 0.25):
                    signal = "watched"
                elif (
                    completion is not None and completion < 0.25 and 0 < count <= 3
                    and at is not None and now - at / 1000 > 60 * DAY
                ):
                    signal = "abandoned"
                    flags.append("dropped_early")
                if session >= 4:
                    flags.append("binge")
                if rewatch >= 3:
                    flags.append("comfort")
            else:
                rewatch = max(0, len(plays) - 1)
                signal = "loved" if len(plays) >= 2 else "watched" if plays else "interest"
                if len(plays) >= 2:
                    flags.append("comfort")
            if rewatch >= 1:
                flags.append("rewatch")
            genres = {genre.lower() for genre in item["genres"]}
            if "anime" in genres or (item["lang"] == "ja" and "animation" in genres):
                flags.append("anime")
            if plays and not real:
                flags.append("backfill_only")
            age = (now - at / 1000) / DAY if at is not None else float("inf")
            recency = 1.0 if age <= 30 else 0.7 if age <= 120 else 0.5 if age <= 365 else 0.35
            weight = BASE_WEIGHT[signal] * recency + 0.5 * ("binge" in flags) + 0.5 * ("comfort" in flags)
            item.update({"rewatch": rewatch, "signal": signal, "flags": flags, "weight": round(weight, 3)})
            items.append(item)
    items.sort(key=lambda item: (-item["weight"], -(item["at"] if item["at"] is not None else float("-inf"))))
    backfill_plays = sum(minutes[minute] for minute in backfill)
    return {
        "version": 1, "source": "trakt", "exported_at": data["exported_at"],
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
        "stats": {
            "movies": len(data["movies"]), "shows": len(data["shows"]),
            "plays": sum(minutes.values()), "backfill_plays": backfill_plays,
            "real_plays": sum(minutes.values()) - backfill_plays,
        },
        "items": items,
    }


def write_atomic(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)



def export_history(path, profiles):
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=10) as con:
        where = " or ".join(["uuid like ?"] * len(profiles))
        args = tuple(p + "%" for p in profiles)
        rows = con.execute(
            f"select substr(uuid, 1, 8), media_type, base_id, max(coalesce(last_played_at, 0)), max(played), "
            f"sum(case when kind = 'episode' and played = 1 then 1 else 0 end), max(favorite), "
            f"max(case when duration_ms > 0 then 1.0 * position_ms / duration_ms else 0 end), count(*), max(rating), "
            f"max(likes), max(dropped), "
            f"max(case when kind = 'episode' and played = 1 then season * 1000 + episode end), "
            f"max(case when kind = 'episode' and played = 0 and position_ms > 0 then season * 1000 + episode end) "
            f"from watch_state where ({where}) group by 1, 2, 3 order by 4 desc limit 120",
            args).fetchall()
        items = []
        for prefix, typ, base, at, played, eps, fav, prog, n, rating, likes, dropped, last, resume in rows:
            item = {"profile": profiles[prefix], "type": typ, "id": base, "at": at or None, "played": bool(played),
                    "favorite": bool(fav), "progress": round(prog or 0, 2), "entries": n, "rating": rating,
                    "likes": likes, "dropped": bool(dropped)}
            if typ == "series":
                item["episodes_played"] = eps or 0
                if last:
                    item["last_episode"] = [last // 1000, last % 1000]  # furthest episode played to the end
                if resume and (not last or resume > last):
                    item["resume_episode"] = [resume // 1000, resume % 1000]  # started, not finished
            items.append(item)
        return json.dumps({"items": items}).encode()
