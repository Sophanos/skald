from dataclasses import replace
import datetime as dt
import re
from ..members import PEOPLE
from . import Candidate, clean_title, combine, csv_rows, day


SOURCE = {"hint": "Viewing activity CSV", "dir": False, "profiles": True}

def parse(path, profile_map=None, stats=None, **kwargs):
    rows = csv_rows(path)
    duration = bool(rows and "Duration" in rows[0])
    newest = max((day(r.get("Start Time") or r.get("Date")) for r in rows), default="")
    cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=60)).isoformat() if newest else ""
    out, plays = [], {}
    for row in rows:
        person = ""
        if row.get("Profile Name"):
            person = (profile_map or {}).get(row["Profile Name"])
            if person not in (*PEOPLE, "both"):
                name = ascii(row["Profile Name"][:80])
                raise ValueError(f"unmapped profile {name} — add --profile-map {ascii(row['Profile Name'][:80] + '=' + PEOPLE[0])}")
        seconds = None
        if duration and row.get("Duration"):
            if not re.fullmatch(r"\d+:\d{2}:\d{2}", row["Duration"]):
                raise ValueError("invalid Netflix duration")
            h, m, s = map(int, row["Duration"].split(":"))
            if m >= 60 or s >= 60:
                raise ValueError("invalid Netflix duration")
            seconds = h * 3600 + m * 60 + s
            if seconds < 120 or seconds < 300 and "autoplayed" in row.get("Attributes", "").casefold():
                if stats is not None:
                    stats["skipped_short"] = stats.get("skipped_short", 0) + 1
                continue
        raw = row.get("Title", "")
        episodic = re.search(r":\s*(?:Season|Series|Staffel|Part)\s+\d+\s*:", raw, re.I)
        title = raw[:episodic.start()] if episodic else raw
        supplemental = (row.get("Supplemental Video Type") or "").strip()
        signal = "interest" if supplemental else "seen"
        c = Candidate(clean_title(title), "", "tv" if episodic else "", {}, signal, 1,
                      day(row.get("Start Time") or row.get("Date")), "netflix", person)
        out.append(c)
        plays.setdefault((c.title.casefold(), c.kind, person), []).append((raw, seconds, c.last_ts, signal))
    out = combine(out)
    if not duration:
        return out
    for i, c in enumerate(out):
        watched = [p for p in plays[(c.title.casefold(), c.kind, c.person)] if p[3] == "seen"]
        if not watched or any(p[1] is None for p in watched):
            continue
        if not c.kind and sum(p[1] for p in watched) < 1800:
            out[i] = replace(c, kind="movie", signal="abandon", conf=.4)
        elif c.kind == "tv" and len({p[0] for p in watched}) <= 2 and max(p[2] for p in watched) < cutoff:
            out[i] = replace(c, signal="abandon", conf=.5)
    return out
