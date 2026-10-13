"""Opt-in, offline temporal folds for weekly discovery recovery.

Input events: iterable of dicts with member/title (strings), at (occurrence),
available_at (first ingestion/availability), and optional mature_at (outcome
readiness). All times are timezone-aware datetime objects. Each dict represents
an immutable observation, not a retrospectively enriched history record.
Only scalar fields are expected. No I/O or household data loading occurs here.
Normalized events are also accepted: member must be explicit, precision must
be second, and occurred_at/observed_at must be numeric UTC times. Identity is
the canonical title key. Coarse dates and missing availability are rejected.
weekly_folds(events, as_of, *, timezone, outer_weeks=12, development_weeks=8,
outcome_grace=timedelta(days=7)) returns a tuple of frozen Fold objects.
timezone is an IANA household zone string, including 'UTC'. Origins are Monday
05:00 in that zone; targets are [origin, end), the next calendar seven days.
The latest 12 mature weeks are outer, preceded by 8 development weeks plus a
maturity gap. Development labels are cut off at the first outer origin, so
all development weeks mature before parameter selection is frozen.
No minimum history is assumed; callers should report empty prefixes.
outcome_grace is elapsed time after end, at least seven days. Set a longer
grace for completion outcomes, including absent outcomes; mature_at can delay
individual observed outcomes further. Unready labels are censored, not misses.

Fold fields: phase ('development', 'outer', or 'next_choice'), origin/end
(aware datetimes), train/labels/censored (tuples of copied event dicts).
Every member shares the same cutoff: train requires at < origin and both
availability and maturity <= origin. Labels require origin <= at < end and
availability and maturity <= as_of (first outer origin for development).
Earlier outer observations enter later prefixes once ready.
Generate the canonical home once at origin from
train alone, with all other model/catalog inputs also restricted to that time.
This measures weekly discovery recovery, not exact session conversion.

select_parameters(folds, candidates, score) returns a deep copy of one candidate
dict. score(candidate, development_fold) returns a finite numeric utility,
higher is better. Mean development utility selects; ties keep input order.
Call once and reuse the result for every outer fold without retuning.

chronological_last_unique(events, member, as_of) returns a 'next_choice' Fold
or None. It holds out the member's latest first occurrence of a unique title
that is ready by as_of; training uses the household prefix before that event.
Keep this diagnostic separate; never use random title LOO as the main gate.
Do not treat these tasks as equivalent:
protocol choices can change rankings (https://arxiv.org/abs/2507.16289).
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from math import isfinite
from zoneinfo import ZoneInfo

from . import protocol

WEEK = timedelta(days=protocol.OUTCOME_GRACE_DAYS)


@dataclass(frozen=True)
class Fold:
    phase: str
    origin: datetime
    end: datetime
    train: tuple[dict, ...]
    labels: tuple[dict, ...]
    censored: tuple[dict, ...]


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("times must be timezone-aware datetimes")
    return value.astimezone(dt_timezone.utc)


def _events(events):
    rows = [dict(e) for e in events]
    for e in rows:
        if "at" not in e:
            if (e.get("timestamp_precision") != "second"
                    or not isinstance(e.get("member"), str) or not e["member"]):
                raise ValueError("precise times and explicit member required for folds")
            for source, target in (("occurred_at", "at"), ("observed_at", "available_at"),
                                   ("mature_at", "mature_at")):
                if source == "mature_at" and source not in e:
                    continue
                stamp = e.get(source)
                if type(stamp) not in (int, float) or not isfinite(stamp):
                    raise ValueError("finite numeric UTC times required for folds")
                try:
                    e[target] = datetime.fromtimestamp(stamp, dt_timezone.utc)
                except (OverflowError, OSError, ValueError):
                    raise ValueError("invalid fold timestamp") from None
            e["title"] = e["identity"]
        for key in ("at", "available_at", "mature_at"):
            _utc(e.get(key, e["at"]) if key == "mature_at" else e[key])
    return sorted(rows, key=lambda e: (_utc(e["at"]), _utc(e["available_at"]),
                                       e["member"], e["title"]))


def _ready(event, cutoff):
    return max(_utc(event["available_at"]),
               _utc(event.get("mature_at", event["at"]))) <= _utc(cutoff)


def _fold(rows, phase, origin, end, as_of):
    start, stop = _utc(origin), _utc(end)
    train = tuple(dict(e) for e in rows if _utc(e["at"]) < start and _ready(e, origin))
    targets = [e for e in rows if start <= _utc(e["at"]) < stop]
    labels = tuple(dict(e) for e in targets if _ready(e, as_of))
    censored = tuple(dict(e) for e in targets if not _ready(e, as_of))
    return Fold(phase, origin, end, train, labels, censored)


def _boundary(value, zone):
    local = value.astimezone(zone)
    boundary = local.replace(hour=5, minute=0, second=0, microsecond=0)
    boundary -= timedelta(days=boundary.weekday())
    if _utc(boundary) > _utc(value):
        boundary -= WEEK
    return boundary


def weekly_folds(events, as_of, *, timezone, outer_weeks=protocol.MIN_MATURE_WEEKS,
                 development_weeks=protocol.DEVELOPMENT_WEEKS,
                 outcome_grace=WEEK):
    if outer_weeks < 1 or development_weeks < 1 or outcome_grace < WEEK:
        raise ValueError("positive week counts and at least seven days of grace required")
    rows, zone = _events(events), ZoneInfo(timezone)
    outer_end = _boundary(_utc(as_of) - outcome_grace, zone)
    outer_start = outer_end - outer_weeks * WEEK
    dev_end = _boundary(_utc(outer_start) - outcome_grace, zone)
    dev_start = dev_end - development_weeks * WEEK
    development = tuple(_fold(rows, "development", dev_start + i * WEEK,
                              dev_start + (i + 1) * WEEK, outer_start)
                        for i in range(development_weeks))
    outer = tuple(_fold(rows, "outer", outer_start + i * WEEK,
                        outer_start + (i + 1) * WEEK, as_of)
                  for i in range(outer_weeks))
    return development + outer


def select_parameters(folds, candidates, score):
    development = [f for f in folds if f.phase == "development"]
    candidates = list(candidates)
    if not development or not candidates:
        raise ValueError("development folds and candidate parameters required")
    utilities = []
    for parameters in candidates:
        values = [score(parameters, fold) for fold in development]
        if not all(isfinite(v) for v in values):
            raise ValueError("development scores must be finite")
        utilities.append(sum(values) / len(values))
    return deepcopy(candidates[max(range(len(candidates)), key=utilities.__getitem__)])


def chronological_last_unique(events, member, as_of):
    cutoff = _utc(as_of)
    rows = _events(events)
    seen, target = set(), None
    for e in rows:
        if e["member"] != member or _utc(e["at"]) >= cutoff or not _ready(e, as_of):
            continue
        if e["title"] not in seen:
            seen.add(e["title"])
            target = e
    if target is None:
        return None
    train = tuple(dict(e) for e in rows
                  if _utc(e["at"]) < _utc(target["at"]) and _ready(e, target["at"]))
    return Fold("next_choice", target["at"], as_of, train, (dict(target),), ())
