"""Opt-in, aggregate-only sharing. No household modules or import-time I/O.

build_share(report) accepts a dict with status, versions and metrics; all other
keys, including nested diagnostics, are ignored. Status is ok, error,
golden_mismatch or worker_crash; unknown values become error. Non-ok reports
export no metrics. versions maps evaluator, build_a, build_b and protocol to
lowercase SHA-256 hex digests of PUBLIC code/protocol artifacts only. Callers
must establish provenance: a digest cannot prove its input was public.
Dataset hashes and per-item hashes are never accepted as additional fields.
Titles, catalog IDs, row labels, names, dates, paths and embeddings are excluded.

metrics maps the fixed names hit_rate, coverage_rate and regression_rate to
{count: int, events: int}, meaning aggregate binary trials and positive events.
home_recovery also accepts {count, events, score}: the preregistered macro
score survives instead of being replaced by a pooled binary rate. These counts
are only for suppression; Wilson bounds do not apply to a macro score.
home_recovery_delta (or any accepted metric name) accepts analyze's aggregate
{delta, ci95, support}, preserving the signed paired bootstrap interval.
Only aggregate support counts survive; sparse or undefined pairs are suppressed.
The same protocol must define these trials for both households. Counts are
bounded by 1,000,000. No member, calendar, row or item breakdown is supported.
Output is {schema: 1, status: code, versions: {...}, metrics: {...}}. Each cell
is {status: suppressed} if either events or count-events is below five; no
count, rate or bound survives, so the complement cannot be derived. Otherwise
it is {status: ok, count: int, events: int, rate: float, confidence: [lo, hi]}.
Rates round to three decimals; 95% Wilson bounds round outward to three.
validate_share(document) rejects any extra key or inconsistent value, returns
None, and raises ValueError with a constant message on invalid documents.
write_share(directory, report, enabled=False) does nothing unless enabled is
True, then validates and writes share.json in an existing directory; returns
its Path. It never serializes exceptions, golden differences or crash details.

A second household downloads the same evaluator, A/B builds and protocol,
evaluates its own read-only snapshot locally and shares only this validated
document. compare_households([document, ...]) requires at least two documents
with identical versions. It returns {households: int, status: replication_only,
rates: {metric: float}} with equal household weight, using only metrics present
and unsuppressed in every household. Two households are useful replication,
not a population-level effectiveness estimate. No comparison identifier is used.
Paired cells are averaged separately under deltas, without pooling intervals.
"""
import json
import math
from pathlib import Path
import re
from statistics import NormalDist

from . import protocol

METRICS = ("hit_rate", "coverage_rate", "regression_rate", "home_recovery", "home_recovery_delta")
VERSIONS = ("evaluator", "build_a", "build_b", "protocol")
STATUSES = ("ok", "error", "golden_mismatch", "worker_crash")


def _versions(values):
    if type(values) is not dict:
        raise ValueError("invalid public version hashes")
    result = {key: values.get(key) for key in VERSIONS}
    if any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in result.values()):
        raise ValueError("invalid public version hashes")
    return result


def _number(value, low, high):
    return (type(value) in (int, float) and math.isfinite(value)
            and low <= value <= high)


def _paired_cell(values):
    support = values.get("support")
    if type(support) is not dict:
        raise ValueError("invalid aggregate support")
    counts = {key: support.get(key) for key in ("weeks", "target_weeks", "targets")}
    if (any(type(n) is not int or not 0 <= n <= protocol.MAX_SHARE_COUNT
            for n in counts.values()) or counts["target_weeks"] > counts["weeks"]):
        raise ValueError("invalid aggregate support")
    if (counts["weeks"] < protocol.MIN_MATURE_WEEKS
            or counts["target_weeks"] < protocol.MIN_TARGET_WEEKS
            or counts["targets"] < protocol.MIN_DISCOVERY_TARGETS):
        return {"status": "suppressed"}
    delta, bounds = values.get("delta"), values.get("ci95")
    if delta is None or bounds is None:
        return {"status": "suppressed"}
    if (not _number(delta, -1, 1) or type(bounds) not in (tuple, list) or len(bounds) != 2
            or any(not _number(value, -1, 1) for value in bounds) or bounds[0] > bounds[1]):
        raise ValueError("invalid paired aggregate")
    return {"status": "ok", "delta": float(delta),
            "ci95": [float(value) for value in bounds], "support": counts}


def _cell(values):
    if type(values) is not dict:
        raise ValueError("invalid aggregate counts")
    if "delta" in values:
        return _paired_cell(values)
    n, k = values.get("count"), values.get("events")
    if type(n) is not int or type(k) is not int or not 0 <= k <= n <= protocol.MAX_SHARE_COUNT:
        raise ValueError("invalid aggregate counts")
    if "score" in values and not _number(values["score"], 0, 1):
        raise ValueError("invalid aggregate score")
    if min(k, n - k) < protocol.MIN_SHARE_CELL:
        return {"status": "suppressed"}
    if "score" in values:
        return {"status": "ok", "count": n, "events": k,
                "score": round(float(values["score"]), protocol.SHARE_DECIMALS)}
    p, z = k / n, NormalDist().inv_cdf((1 + protocol.CONFIDENCE_LEVEL) / 2)
    scale = 1 + z * z / n
    center = (p + z * z / (2 * n)) / scale
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / scale
    rounding = 10 ** protocol.SHARE_DECIMALS
    return {"status": "ok", "count": n, "events": k,
            "rate": round(p, protocol.SHARE_DECIMALS),
            "confidence": [math.floor((center - radius) * rounding) / rounding,
                           math.ceil((center + radius) * rounding) / rounding]}


def build_share(report):
    if type(report) is not dict:
        raise ValueError("invalid aggregate report")
    status = report.get("status")
    if type(status) is not str or status not in STATUSES:
        status = "error"
    result = {"schema": 1, "status": status,
              "versions": _versions(report.get("versions")), "metrics": {}}
    if status == "ok":
        cells = report.get("metrics", {})
        if type(cells) is not dict:
            raise ValueError("invalid aggregate counts")
        result["metrics"] = {key: _cell(cells[key]) for key in METRICS if key in cells}
    return result


def validate_share(document):
    try:
        if (type(document) is not dict or set(document) != {"schema", "status", "versions", "metrics"}
                or type(document["schema"]) is not int or document["schema"] != 1
                or type(document["status"]) is not str or document["status"] not in STATUSES
                or document["versions"] != _versions(document["versions"])
                or type(document["metrics"]) is not dict
                or set(document["metrics"]) - set(METRICS)
                or (document["status"] != "ok" and document["metrics"])):
            raise ValueError
        for cell in document["metrics"].values():
            if type(cell) is not dict:
                raise ValueError
            if cell == {"status": "suppressed"}:
                continue
            if "delta" in cell:
                bounds = cell.get("ci95")
                valid = (type(cell["delta"]) is float and type(bounds) is list
                         and len(bounds) == 2 and all(type(value) is float for value in bounds))
            elif "score" in cell:
                valid = type(cell["score"]) is float
            else:
                bounds = cell.get("confidence")
                valid = (type(cell.get("rate")) is float and type(bounds) is list
                         and len(bounds) == 2 and all(type(value) is float for value in bounds))
            if not valid or cell != _cell(cell):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid aggregate document") from None


def write_share(directory, report, *, enabled=False):
    if enabled is not True:
        return None
    document = build_share(report)
    validate_share(document)
    target = Path(directory) / "share.json"
    target.write_text(json.dumps(document, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return target


def compare_households(documents):
    if type(documents) is not list or len(documents) < 2:
        raise ValueError("at least two aggregate documents required")
    for document in documents:
        validate_share(document)
    if any(doc["versions"] != documents[0]["versions"] for doc in documents):
        raise ValueError("public versions must match")
    rates, deltas = {}, {}
    for key in METRICS:
        if all(doc["status"] == "ok" and doc["metrics"].get(key, {}).get("status") == "ok"
               for doc in documents):
            cells = [doc["metrics"][key] for doc in documents]
            if all("delta" in cell for cell in cells):
                deltas[key] = round(sum(cell["delta"] for cell in cells) / len(cells),
                                    protocol.SHARE_DECIMALS)
            elif all("score" in cell for cell in cells):
                rates[key] = round(sum(cell["score"] for cell in cells) / len(cells),
                                   protocol.SHARE_DECIMALS)
            elif all("rate" in cell for cell in cells):
                rates[key] = round(sum(cell["events"] / cell["count"] for cell in cells) / len(cells),
                                   protocol.SHARE_DECIMALS)
    return {"households": len(documents), "status": "replication_only", "rates": rates,
            **({"deltas": deltas} if deltas else {})}
