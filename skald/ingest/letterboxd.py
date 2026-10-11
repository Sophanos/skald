from dataclasses import replace
from pathlib import Path
from . import Candidate, EXPLICIT, PRIORITY, clean_title, csv_rows, day, rating_signal


SOURCE = {"hint": "Letterboxd CSV export folder", "dir": True, "profiles": False}

def parse(path, **kwargs):
    path = Path(path)
    files = [path] if path.is_file() else [path / name for name in
             ("ratings.csv", "watched.csv", "diary.csv", "likes/films.csv", "watchlist.csv") if (path / name).is_file()]
    if not files:
        raise ValueError("no supported Letterboxd CSVs")
    grouped = {}
    for file in files:
        for row in csv_rows(file):
            signal = rating_signal(row.get("Rating"), scale=5, liked=file.parent.name == "likes",
                                   default="interest" if file.name == "watchlist.csv" else "seen")
            ids = {"tmdb": int(row["TMDB ID"])} if row.get("TMDB ID") else {}
            c = Candidate(clean_title(row.get("Name") or row.get("Title", "")), row.get("Year", ""),
                          "movie", ids, signal, 1, day(row.get("Watched Date") or row.get("Date")), "letterboxd")
            rows = grouped.setdefault((c.title.casefold(), c.year), [])
            rows.append((c, row, "likes" if file.parent.name == "likes" else file.name))
    out = []
    for rows in grouped.values():
        ratings = [c for c, row, file in rows if file == "ratings.csv" and row.get("Rating") not in (None, "")] or \
                  [c for c, row, file in rows if file == "diary.csv" and row.get("Rating") not in (None, "")]
        c = max(ratings, key=lambda c: c.last_ts) if ratings else max((c for c, _, _ in rows), key=lambda c: PRIORITY[c.signal])
        likes = [c for c, _, file in rows if file == "likes"]
        if likes and c.signal != "not_for_us":
            c = max(likes, key=lambda c: c.last_ts)
        dates = {day(row["Watched Date"]) for _, row, file in rows if file == "diary.csv" and row.get("Watched Date")}
        rated = any(row.get("Rating") not in (None, "") for _, row, _ in rows)
        if not rated and c.signal != "love" and any(file == "diary.csv" and row.get("Rewatch", "").casefold() == "yes"
                                                  for _, row, file in rows):
            c = replace(c, signal="like", conf=.7)
        stamps = [c.last_ts] if ratings or likes else \
                 [x.last_ts for x, _, _ in rows if x.signal == c.signal and x.signal in EXPLICIT]
        out.append(replace(c, n=max(1, len(dates)), last_ts=max(stamps or [x.last_ts for x, _, _ in rows]),
                           ids={k: v for x, _, _ in rows for k, v in x.ids.items()}))
    return out
