import json
import re
from . import Candidate, clean_title, day, interests


SOURCE = {"hint": "Takeout watch history JSON", "dir": False, "profiles": False}

def parse(path, **kwargs):
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    out = []
    for row in rows:
        text = re.sub(r"^Watched\s+", "", row.get("title", ""), flags=re.I)
        match = re.search(r"\b(?:trailer|teaser|official\s+clip)\b", text, re.I)
        if not match:
            continue
        title = re.sub(r"\b(?:official|final|new|full)\s*$", "", text[:match.start()], flags=re.I)
        title = title.strip(" -–—|:")
        year_match = re.search(r"\((\d{4})\)", text)
        year = year_match[1] if year_match else ""
        title = re.sub(r"\(\d{4}\)", "", title).strip()
        if title:
            out.append(Candidate(clean_title(title), year, "", {}, "interest", 1, day(row.get("time")), "youtube", conf=.5))
    return interests(out, max((day(r.get("time")) for r in rows), default=""))
