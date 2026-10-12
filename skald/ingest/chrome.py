import datetime as dt
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from . import Candidate, clean_title, interests


SOURCE = {"hint": "Chrome history database", "dir": False, "profiles": False}

def url_hint(url):
    try:
        u = urlsplit(url)
        host = (u.hostname or "").removeprefix("www.").lower()
        if u.scheme not in ("http", "https") or u.username or u.password or u.port not in (None, 80, 443):
            return None
        path = unquote(u.path)
    except ValueError:
        return None
    kind, ids, title = "", {}, ""
    if host == "imdb.com" and (m := re.fullmatch(r"/title/(tt\d+)/?", path)):
        ids = {"imdb": m[1]}
    elif host == "themoviedb.org" and (m := re.fullmatch(r"/(movie|tv)/(\d+)(?:-[^/]*)?/?", path)):
        kind, ids = m[1], {"tmdb": int(m[2])}
    elif host == "letterboxd.com" and (m := re.fullmatch(r"/film/([^/]+)/?", path)):
        kind, title = "movie", m[1].replace("-", " ")
    elif host == "trakt.tv" and (m := re.fullmatch(r"/(movies|shows)/([^/]+)/?", path)):
        kind, title = "movie" if m[1] == "movies" else "tv", m[2].replace("-", " ")
    elif host == "netflix.com" and re.fullmatch(r"/title/\d+/?", path):
        pass  # Netflix ids are not TMDB ids; resolve the page's title locally.
    elif re.fullmatch(r"[a-z-]+\.wikipedia\.org", host) and (m := re.fullmatch(r"/wiki/(.+)_\((?:\d{4}_)?(film|TV_series)\)", path)):
        kind, title = "movie" if m[2] == "film" else "tv", m[1].replace("_", " ")
    else:
        return None
    return json.dumps({"kind": kind, "ids": ids, "title": title})


def parse(path, temp_dir=None, **kwargs):
    out = []
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="skald-history-", dir=temp_dir) as temp:
        copy = Path(temp) / "History"
        shutil.copyfile(path, copy)
        # Include uncheckpointed browser writes; temporary copies are deleted on exit.
        for suffix in ("-wal", "-shm"):
            if Path(str(path) + suffix).is_file():
                shutil.copyfile(str(path) + suffix, str(copy) + suffix)
        with sqlite3.connect(copy) as db:
            db.create_function("skald_url", 1, url_hint, deterministic=True)
            visits = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visits'").fetchone()
            days = {}
            if visits:
                for url, stamp in db.execute("SELECT visits.url, visits.visit_time FROM visits JOIN urls ON urls.id=visits.url WHERE skald_url(urls.url) IS NOT NULL"):
                    date = (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=stamp or 0)).date().isoformat()
                    days.setdefault(url, set()).add(date)
            newest_stamp = db.execute("SELECT MAX(last_visit_time) FROM urls").fetchone()[0]
            newest = (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=newest_stamp)).date().isoformat() if newest_stamp else ""
            rows = db.execute("SELECT id, title, visit_count, last_visit_time, skald_url(url) FROM urls WHERE skald_url(url) IS NOT NULL")
            for url, title, n, stamp, hint in rows:
                data = json.loads(hint)
                title = data["title"] or re.sub(r"\s*[-|]\s*(?:IMDb|TMDB|The Movie Database.*|Netflix).*$", "", title, flags=re.I)
                title = re.sub(r"\s*\(\d{4}[^)]*\).*", "", title).removeprefix("Watch ").strip()
                if not title:
                    continue
                date = (dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=stamp or 0)).date().isoformat() if stamp else ""
                out.append(Candidate(clean_title(title), "", data["kind"], data["ids"], "interest", max(1, n or 1), date, "chrome",
                                     conf=.6 if (n or 0) >= 3 and (not visits or len(days.get(url, ())) >= 2) else .3))
    return interests(out, newest)
