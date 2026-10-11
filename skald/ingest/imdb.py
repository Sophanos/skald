import re
from . import Candidate, clean_title, combine, csv_rows, day, rating_signal


SOURCE = {"hint": "Ratings and watchlist CSV", "dir": False, "profiles": False}

def parse(path, stats=None, **kwargs):
    out = []
    for row in csv_rows(path):
        title_type = re.sub(r"[\s_-]", "", row.get("Title Type", "")).casefold()
        if title_type in ("videogame", "podcastseries", "podcastepisode", "musicvideo"):
            if stats is not None:
                stats["skipped_type"] = stats.get("skipped_type", 0) + 1
            continue
        rating = row.get("Your Rating")
        signal = rating_signal(rating, default="interest")
        kind = "tv" if title_type in ("tvseries", "tvminiseries", "tvepisode") else "movie"
        ids = {"imdb": row["Const"]} if row.get("Const") else {}
        out.append(Candidate(clean_title(row.get("Title", "")), row.get("Year", ""), kind, ids, signal,
                             1, day(row.get("Date Rated") or row.get("Created")), "imdb"))
    return combine(out)
