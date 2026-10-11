"""Local-only export parsers. Candidates never contain URLs or raw profile names."""

from dataclasses import dataclass, field
import datetime as dt
import csv
import re
import pkgutil
from importlib import import_module


@dataclass(frozen=True)
class Candidate:
    title: str
    year: str = ""
    kind: str = ""
    ids: dict = field(default_factory=dict)
    signal: str = "seen"
    n: int = 1
    last_ts: str = ""
    src: str = "plain"
    person: str = ""
    conf: float = 1.0


# The single parser signal table; ratings are converted to the 10-point scale.
SIGNALS = {"not_for_us": (0, 4.5), "meh": (4.5, 6), "like": (6, 8), "love": (8, 10.0001)}
PRIORITY = {"interest": 0, "seen": 1, "abandon": 2, "meh": 3, "like": 4, "love": 5, "not_for_us": 6}
EXPLICIT = {"love", "like", "meh", "not_for_us"}


def rating_signal(value, scale=10, liked=False, default="seen"):
    if liked:
        return "love"
    if value not in (None, ""):
        rating = float(value) * 10 / scale
        if not 0 <= rating <= 10:
            raise ValueError("rating outside source scale")
        for signal, (low, high) in SIGNALS.items():
            if low <= rating < high:
                return signal
    return default


def day(value):
    if not value:
        return ""
    text = str(value)
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y"):
            try:
                return dt.datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
    raise ValueError("unrecognised export date")


def csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def clean_title(title):
    title = re.sub(r"\s+", " ", str(title)).strip()
    if not title or "://" in title or len(title) > 300 or any(ord(c) < 32 for c in title):
        raise ValueError("invalid export title")
    return title


def combine(candidates):
    out = {}
    for c in candidates:
        key = (c.title.casefold(), c.year, c.kind, tuple(sorted(c.ids.items())), c.person)
        old = out.get(key)
        if not old:
            out[key] = c
            continue
        signal = max((old.signal, c.signal), key=PRIORITY.__getitem__)
        out[key] = Candidate(c.title, c.year, c.kind, c.ids, signal, old.n + c.n,
                             max(old.last_ts, c.last_ts), c.src, c.person,
                             max(x.conf for x in (old, c) if x.signal == signal))
    return list(out.values())


def interests(candidates, newest):
    cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=120)).isoformat() if newest else ""
    candidates = combine(candidates)
    grouped = {}
    for c in sorted(candidates, key=lambda c: c.last_ts, reverse=True):
        rows = grouped.setdefault(c.person, [])
        if c.last_ts and c.last_ts > cutoff and len(rows) < 20:
            rows.append(c)
    kept = {id(c) for rows in grouped.values() for c in rows}
    return [c for c in candidates if id(c) in kept]


RESERVED = {"utterance", "agent", "sync", "told", "tv", "remote", "interview"}


def sources():
    out = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name == "resolve" or info.name.startswith("_") or info.name in RESERVED:
            continue
        module = import_module(__name__ + "." + info.name)
        if callable(getattr(module, "parse", None)) and hasattr(module, "SOURCE"):
            out[info.name] = module
    return out


def parse(source, path, **kwargs):
    module = sources().get(source)
    if module is None:
        raise ValueError("unknown source")
    return module.parse(path, **kwargs)
