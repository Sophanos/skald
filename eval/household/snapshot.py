"""Opt-in owner-run local freeze and read-only provenance evaluation.

freeze(source, destination, inputs=..., generation=..., enabled=False) returns
None while disabled, otherwise destination/snapshot.json in a new 0700 directory
with a 0400 file. inputs is an explicit sequence of names from INPUTS; no directory
scan or backup machinery is used. Missing selected files stay missing.
generation() must return a nonnegative integer identifying committed state. The
owner must pause writers or fence all writes with that generation; file stamps
are also checked before and after intake. There is no production generation hook.
Each regular input with one hard link is bounded by max_bytes (cap 8 MiB); the
total is capped at 64 MiB. JSONL inputs are lists of JSON objects; JSON inputs
are objects. skald.json and taste.json retain only supported configuration
fields, audience structure, rows, preferences and region, without auth or paths.
Other allowlisted state remains private. Errors never echo paths or contents.

The file contains {schema: 1, generation: int, requested: [name], inputs:
{name: sanitized JSON object or JSONL list}, digest: SHA256 of those fields}.
This private digest detects accidental edits, not an authenticity signature.
evaluate(path) verifies the file and returns {evidence_level, availability,
targets, generation}. availability maps capability to {available: bool,
count: int or None, unusable: int or None}; count None is missing, zero is empty.
unusable counts playback chronology failures; None means not assessed.
targets uses eval.household.events.normalize_events on immutable playback.jsonl
records in that module's input shape, with numeric occurred_at and observed_at
and second precision. Invalid or undated records count as unusable, not targets.
No dates or plays are inferred from current history items, cumulative progress,
latest timestamps, session windows, feedback receipts or import receipts.

A present-day freeze is always protocol.DIAGNOSTIC, even with dated playback.
Metadata, embeddings, candidate responses, learned state, feeds (curation) and
shown (pins) are current inputs, not historical state. Backups omit feeds and
shown and cannot guarantee historical rows. This module does not score releases.
"""
import json
import math
import os
from pathlib import Path
import stat

from eval.household.events import normalize_events
from eval.household.protocol import DIAGNOSTIC, protocol_id

INPUTS = dict(zip(
    ("skald.json", "taste.json", "history.json", "playback.jsonl", "imports.jsonl",
     "feedback.jsonl", "sessions.jsonl", "facets.json", "emb.json", "decide_cache.json",
     "feeds.json", "shown.json", "shown_scopes.json", "learn.json", "online.json",
     "trakt.json", "cache.json"),
    ("configuration", "preferences", "current_history", "dated_playback", "imports",
     "feedback", "sessions", "metadata", "embeddings", "candidate_responses",
     "curation", "pins", "scoped_pins", "learned_state", "online_state",
     "trakt_history", "replay_cache")))
MAX_BYTES, MAX_TOTAL = 8 * 1024 * 1024, 64 * 1024 * 1024


def _clean(value, schema):
    if isinstance(schema, type):
        if type(value) is not schema:
            raise ValueError("invalid configuration shape")
        return value
    if isinstance(schema, list):
        if type(value) is not list:
            raise ValueError("invalid configuration shape")
        return [_clean(v, schema[0]) if v is not None else None for v in value]
    if type(value) is not dict:
        raise ValueError("invalid configuration shape")
    return {k: _clean(v, schema.get(k, schema.get("*"))) for k, v in value.items()
            if k in schema or "*" in schema}


def _sanitize(doc, *, config=False, members=()):
    hh = doc.get("household", {}) if config else {}
    members = hh.get("members", []) if config else list(members)
    _clean(members, [str])
    genres = {"movie": [str], "series": [str]}
    prefs = {"discovery": str, "years": [int], "hide": [str], "kids_age": int,
             "shelves": int, "excluded_genres": genres,
             "focus": {"genres": genres, "languages": [str], "countries": [str], "people": [str]},
             "runtime": {"movie": int, "series": int},
             "weights": {"movie": int, "series": int, "genre": {"*": int}}}
    taste = {"household": {"likes": str, "rewatch_collections": [int]},
             "people": {p: {"type": str, "loves": [str], "prefs": prefs} for p in members}}
    schema = {"v": int, "row_prefix": str, "taste": taste,
              "region": {"lang": str, "tz": str, "country": str},
              "household": {"members": [str], "labels": {p: str for p in (*members, "pair")},
                            "member_rows": [str], "rows": [str], "row_order": [str],
                            "row_labels": {r: str for r in hh.get("rows", []) + hh.get("row_order", [])},
                            "shelves": int, "archived": {"*": str}, "trakt_member": str,
                            "experiences": {p: [str] for p in members}}}
    return _clean(doc, schema if config else taste)


def _stamp(directory, name, limit):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("expected bounded regular unlinked file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(directory, name, limit):
    before = _stamp(directory, name, limit)
    if before is None:
        return None
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != before[:2]:
            raise ValueError("snapshot input changed")
        raw = stream.read(limit + 1)
    if len(raw) > limit or _stamp(directory, name, limit) != before:
        raise ValueError("snapshot input changed")
    def invalid(_):
        raise ValueError("invalid snapshot JSON")
    if name.endswith(".jsonl"):
        doc = [json.loads(line, parse_constant=invalid) for line in raw.splitlines() if line.strip()]
        if any(type(row) is not dict for row in doc):
            raise ValueError("invalid snapshot records")
    else:
        doc = json.loads(raw, parse_constant=invalid)
        if type(doc) is not dict:
            raise ValueError("invalid snapshot object")
        if name == "history.json" and (type(doc.get("items")) is not list
                or any(type(row) is not dict for row in doc["items"])):
            raise ValueError("invalid current history items")
    return doc


def freeze(source, destination, *, inputs, generation, enabled=False, max_bytes=MAX_BYTES):
    if enabled is not True:
        return None
    if (not isinstance(inputs, (tuple, list)) or any(type(n) is not str or n not in INPUTS for n in inputs)
            or len(set(inputs)) != len(inputs) or not callable(generation)
            or type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES):
        raise ValueError("invalid explicit snapshot inputs")
    directory = None
    try:
        directory = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        start = generation()
        if type(start) is not int or start < 0:
            raise ValueError("invalid snapshot generation")
        names = sorted(inputs)
        stamps = {n: _stamp(directory, n, max_bytes) for n in names}
        if sum(s[2] for s in stamps.values() if s is not None) > MAX_TOTAL:
            raise ValueError("snapshot size exceeds bound")
        data = {n: _read(directory, n, max_bytes) for n in names if stamps[n] is not None}
        end = generation()
        if type(end) is not int or end != start:
            raise ValueError("snapshot generation changed")
        if stamps != {n: _stamp(directory, n, max_bytes) for n in names}:
            raise ValueError("snapshot input changed")
        if "skald.json" in data:
            data["skald.json"] = _sanitize(data["skald.json"], config=True)
        if "taste.json" in data:
            members = data.get("skald.json", {}).get("household", {}).get(
                "members", list(data["taste.json"].get("people", {})))
            data["taste.json"] = _sanitize(data["taste.json"], members=members)
        doc = {"schema": 1, "generation": start, "requested": names, "inputs": data}
        doc["digest"] = protocol_id(doc)
        encoded = json.dumps(doc, sort_keys=True, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_TOTAL:
            raise ValueError("snapshot size exceeds bound")
        destination = Path(destination)
        destination.mkdir(mode=0o700)
        target = destination / "snapshot.json"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                os.fchmod(stream.fileno(), 0o400)
        except Exception:
            target.unlink(missing_ok=True)
            destination.rmdir()
            raise
        return target
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError("snapshot freeze rejected: inputs or generation changed") from None
    finally:
        if directory is not None:
            os.close(directory)


def load(path):
    """Return the verified snapshot envelope for evaluation or worker staging."""
    directory = None
    try:
        path = Path(path)
        if path.name != "snapshot.json":
            raise ValueError("expected snapshot file")
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        doc = _read(directory, path.name, MAX_TOTAL)
        digest = doc.pop("digest")
        if (set(doc) != {"schema", "generation", "requested", "inputs"} or doc["schema"] != 1
                or type(doc["generation"]) is not int or doc["generation"] < 0
                or digest != protocol_id(doc) or set(doc["inputs"]) - set(doc["requested"])
                or set(doc["requested"]) - set(INPUTS)):
            raise ValueError("invalid snapshot provenance")
        return {**doc, "digest": digest}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("snapshot evaluation rejected") from None
    finally:
        if directory is not None:
            os.close(directory)


def evaluate(path):
    try:
        doc = load(path)
        availability, usable = {}, []
        for name, capability in INPUTS.items():
            payload = doc["inputs"].get(name)
            records = payload.get("items") if name == "history.json" and payload is not None else payload
            count = None if payload is None else len(records)
            unusable = count if name == "history.json" else None
            if name == "playback.jsonl" and payload is not None:
                unusable = 0
                for record in records:
                    try:
                        event = normalize_events([record])["events"][0]
                        dated = all(type(event[k]) in (int, float) and math.isfinite(event[k])
                                    for k in ("occurred_at", "observed_at"))
                        if event["playback"] and event["timestamp_precision"] == "second" and dated:
                            usable.append(record)
                        else:
                            unusable += 1
                    except (ValueError, TypeError, KeyError):
                        unusable += 1
            availability[capability] = {"available": payload is not None, "count": count, "unusable": unusable}
        return {"evidence_level": DIAGNOSTIC, "availability": availability,
                "targets": normalize_events(usable), "generation": doc["generation"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("snapshot evaluation rejected") from None
