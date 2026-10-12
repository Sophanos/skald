"""Weekly habits from attributed searches and timestamped plays, without services."""
from collections import defaultdict
from datetime import datetime
import re
from zoneinfo import ZoneInfo

DAY = 86400
FILLER = set("a an the something please show me find for to watch tonight movies movie films film series shows "
             "ein eine einen der die das etwas bitte zeig mir suche für zum schauen heute filme serien".split())


def theme(text):
    return " ".join(w for w in re.findall(r"\w+", text.casefold()) if w not in FILLER)


def facets(play):
    out = {("genre", theme(g)) for g in play.get("genres") or []} | {
        ("country", c.casefold()) for c in play.get("origin_country") or []}
    if play.get("kind"):
        out.add(("kind", play["kind"]))
    return out


def suggest(searches, plays, tz, existing, dismissed, now):
    """Records use p/person, t (epoch seconds), q or genres/origin_country/kind.

    Mark child records with kids_age; the CLI also excludes configured children.
    Days are Monday=0; search windows span at most three inclusive hours.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    children = {r.get("p", r.get("person")) for r in [*searches, *plays] if r.get("kids_age") is not None}
    # Drop keystroke prefixes only within the same person, as recent_searches does.
    ordered = sorted((r for r in searches if r.get("t") is not None), key=lambda r: r["t"])
    searches = [r for i, r in enumerate(ordered) if not any(
        later.get("p", later.get("person")) == r.get("p", r.get("person"))
        and 0 <= later["t"] - r["t"] <= 120 and later.get("q", "").casefold().startswith(r.get("q", "").casefold())
        for later in ordered[i + 1:i + 60])]
    records, totals = defaultdict(list), defaultdict(list)
    for source, rows in (("search", searches), ("play", plays)):
        for r in rows:
            person, at = r.get("p", r.get("person")), r.get("t")
            if not person or person in children or person in ("both", "together", "guest") or at is None or not now - 56 * DAY <= at <= now:
                continue
            d = datetime.fromtimestamp(at, zone)
            if source == "play":
                totals[person].append((d, facets(r)))
            themes = {("search", theme(r.get("q", "")))} if source == "search" else facets(r)
            for key in themes:
                if key[1]:
                    records[person, key].append(d)
    out = defaultdict(list)
    for (person, key), dates in sorted(records.items()):
        candidates = []
        for days in [(d,) for d in range(7)] + [(5, 6), (0, 1, 2, 3, 4)]:
            for hour in range(24) if key[0] == "search" else (0,):
                hits = [d for d in dates if d.weekday() in days and (key[0] != "search" or hour <= d.hour <= min(23, hour + 2))]
                weeks = len({d.date().toordinal() - d.weekday() for d in hits})
                if weeks < 3:
                    continue
                if key[0] != "search":
                    base = [(d, f) for d, f in totals[person] if any(k[0] == key[0] for k in f)]
                    weekday = [f for d, f in base if d.weekday() in days]
                    if sum(key in f for f in weekday) / len(weekday) < 2 * sum(key in f for _, f in base) / len(base):
                        continue
                candidates.append((weeks, len(hits), -len(days), -hour, days, hits))
        if not candidates:
            continue
        weeks, count, _, _, days, hits = max(candidates)
        when = {"days": list(days), "from_hour": min(d.hour for d in hits), "to_hour": max(d.hour for d in hits)}
        def covered(wish):
            if (wish.get("scope", wish.get("person")) not in (person, "both", "together", "household")
                    or not set(key[1].split()) <= set(re.findall(r"\w+", wish.get("text", "").casefold()))):
                return False
            w = wish.get("when") or {}
            if w == 'always':
                return True
            if 'hours' in w:
                from .shelves import DAYS
                covered_hours = {( (DAYS.index(d)+h//24)%7, h%24)
                                 for d in w['days'] for h in range(*w['hours'])}
            else:
                covered_hours = {(d, h) for d in w.get('days', range(7))
                                 for h in range(w.get('from_hour', 0), w.get('to_hour', 23)+1)}
            return all((d, h) in covered_hours for d in days
                       for h in range(when['from_hour'], when['to_hour']+1))
        if any(covered(w) for w in existing) or any(
                r.get("person") == person and now - 90 * DAY <= r.get("t", 0) <= now
                and (r.get("theme") == ":".join(key) or r.get("text", "").casefold() == key[1]
                     or theme(r.get("text", "")) == key[1]) for r in dismissed):
            continue
        out[person].append({"person": person, "name": key[1].title(), "text": key[1], "when": when,
                            "evidence": {"count": count, "weeks": weeks}})
    result = []
    for person in sorted(out):
        ranked = sorted(out[person], key=lambda r: (-r["evidence"]["weeks"], -r["evidence"]["count"], r["text"]))
        unique = {}
        for r in ranked:
            unique.setdefault(r["text"], r)
        result.extend(list(unique.values())[:3])
    return result

