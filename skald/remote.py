"""Bearer API client; remote decisions spend on the server's ledger."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("api redirect")


def settings():
    folder = Path.home() / ".config/skald"
    def saved(name):
        try:
            with (folder / name).open() as fh:
                if os.fstat(fh.fileno()).st_mode & 0o077:
                    raise ValueError(name + " file must be private; run skald connect again")
                return fh.read().strip()
        except FileNotFoundError:
            return ""
    base = os.environ.get("SKALD_API") or saved("api")
    token = os.environ.get("SKALD_TOKEN") or saved("token")
    return base.rstrip("/"), token


def validate_base(base):
    url = urllib.parse.urlsplit(base)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("use an HTTP(S) API base URL without credentials")
    return base.rstrip("/")


def connect(base, token):
    from .intake import write_private
    base = validate_base(base)
    if not token or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in token):
        raise ValueError("invalid API token; copy the token from skald setup link")
    folder = Path.home() / ".config/skald"
    write_private(folder / "api", base + "\n")
    write_private(folder / "token", token + "\n")
    return {"ok": True, "message": "Laptop connected. Next: skald push imports --remote"}


def request(path, body=None):
    base, token = settings()
    validate_base(base)
    headers = {"Accept": "application/json"}
    if token:
        if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in token):
            raise ValueError("invalid API token")
        headers["Authorization"] = "Bearer " + token
    raw = json.dumps(body, allow_nan=False).encode() if body is not None else None
    if raw is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, raw, headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        if err.code in (403, 503):
            try:
                reason = json.load(err).get("error")
            except (ValueError, OSError, AttributeError):
                reason = None
            if reason in ("person", "scope", "src", "origin") or reason == "Server has no TMDB key; use --resolve-online with TMDB_API on your laptop.":
                raise ValueError(reason if err.code == 503 else "api 403: " + reason) from None
        raise ValueError(f"api {err.code}") from None
    except (urllib.error.URLError, OSError):
        raise ValueError("api unavailable") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("api response") from None


def resolve_titles(candidates):
    from .ingest.resolve import public_choice
    results = []
    start = 0
    while start < len(candidates):
        group = candidates[start:start + 100]
        body = {"items": [{"title": c.title, "year": c.year, "kind": c.kind,
                           "ids": {k: v for k, v in c.ids.items() if k in ("imdb", "tmdb")}} for c in group]}
        while len(json.dumps(body, allow_nan=False).encode()) > 65536:
            if len(group) == 1:
                raise ValueError("title exceeds API body limit")
            group.pop()
            body["items"].pop()
        start += len(group)
        reply = request("/api/v1/resolve", body)
        rows = reply.get("results") if isinstance(reply, dict) else None
        if not isinstance(rows, list) or len(rows) != len(group):
            raise ValueError("api response")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"match", "conf", "method", "high_confidence", "choices"} \
                    or type(row["conf"]) not in (int, float) or not 0 <= row["conf"] <= 1 \
                    or row["method"] not in ("id", "search", "unresolved") or not isinstance(row["choices"], list):
                raise ValueError("api response")
            match = row["match"]
            if match is not None:
                if not isinstance(match, dict) or set(match) != {"key", "title", "year", "kind"} or not isinstance(match["key"], str) or not match["key"].startswith("tmdb:"):
                    raise ValueError("api response")
                public_choice({**match, "id": match["key"][5:]})
            results.append(row)
    return results


def push_imports(state, apply=False, *, clock=None, sleep=None):
    import re
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    state = Path(state)
    path = state / "imports.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    undone = json.loads((state / "undone.json").read_text()) if (state / "undone.json").exists() else []
    if not isinstance(undone, list) or any(not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{32}", i) for i in undone):
        raise ValueError("invalid local undo log")
    if any(not isinstance(e, dict) or not isinstance(e.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", e["id"])
           or not isinstance(e.get("t"), str) for e in events):
        raise ValueError("invalid local import log")
    base = urllib.parse.urlsplit(validate_base(settings()[0]))
    if base.scheme not in ("http", "https") or not base.netloc or base.username or base.password or base.query or base.fragment:
        raise ValueError("set SKALD_API to an API base URL")

    def chunks(stream, items):
        batch = []
        for item in items:
            candidate = {"stream": stream, "events": [*batch, item]}
            if len(batch) == 500 or len(json.dumps(candidate, allow_nan=False).encode()) > 60000:
                if not batch:
                    raise ValueError("event exceeds API body limit")
                yield {"stream": stream, "events": batch}
                batch = []
            batch.append(item)
            if len(json.dumps({"stream": stream, "events": batch}, allow_nan=False).encode()) > 60000:
                raise ValueError("event exceeds API body limit")
        if batch:
            yield {"stream": stream, "events": batch}

    bodies = [*chunks("imports", events), *chunks("imports_undo", [{"id": i} for i in undone])]
    refusal_reasons = {"scope": "this laptop token lacks events scope", "person": "these titles are outside this laptop token's person",
                       "src": "this import source is not allowed", "origin": "the server refused the request origin"}
    try:
        index = request("/api/v1/events")
    except ValueError as err:
        if str(err).startswith("api 403"):
            reason = refusal_reasons.get(str(err).removeprefix("api 403: "), "this laptop token cannot write these titles")
            raise ValueError(f"Refused import push: {reason}.") from None
        raise
    if not isinstance(index, dict) or "person" not in index or index["person"] is not None and not isinstance(index["person"], str):
        raise ValueError("api response; reconnect to an updated Skald server")
    person, messages = index["person"], []
    if person:
        from collections import Counter
        skipped = Counter(e.get("who", "unknown") for e in events if e.get("who") != person)
        messages = [f"Skipped {n} titles for {who}: this laptop token is for {person} only. "
                    "Make a household token with `skald setup link --name laptop` (no --person)."
                    for who, n in sorted(skipped.items())]
        events = [e for e in events if e.get("who") == person]
        bodies = [*chunks("imports", events), *chunks("imports_undo", [{"id": i} for i in undone])]
    if not apply:
        return {"count": len(events), "undone": len(undone), "chunks": len(bodies),
                "bytes": sum(len(json.dumps(b, allow_nan=False).encode()) for b in bodies),
                "host": base.hostname, "requests": 0, "messages": messages}
    out = {"accepted": 0, "duplicates": 0, "rejected": [], "undone": 0, "already_absent": 0, "requests": 0, "messages": messages}
    last, offset = None, 0
    for body in bodies:
        for attempt in range(2):
            if last is not None:
                sleep(max(0, 1.1 - (clock() - last)))
            last = clock()
            out["requests"] += 1
            try:
                reply = request("/api/v1/events", body)
                break
            except ValueError as err:
                if str(err).startswith("api 403"):
                    reason = refusal_reasons.get(str(err).removeprefix("api 403: "), "this laptop token cannot write these titles")
                    raise ValueError(f"Refused {len(body['events'])} {body['stream']} titles: {reason}.") from None
                if str(err) != "api 429":
                    raise
                if attempt:
                    raise ValueError("rate limited; re-run (idempotent)") from None
                sleep(61)
        if not isinstance(reply, dict) or set(reply) != {"accepted", "duplicates", "rejected"} \
                or any(type(reply[k]) is not int or reply[k] < 0 for k in ("accepted", "duplicates")) \
                or not isinstance(reply["rejected"], list):
            raise ValueError("api response")
        rejected = reply["rejected"]
        if any(not isinstance(r, dict) or set(r) != {"i", "error"} or type(r["i"]) is not int
               or not 0 <= r["i"] < len(body["events"]) or r["error"] not in ("input", "conflict", "id") for r in rejected):
            raise ValueError("api response")
        if reply["accepted"] + reply["duplicates"] + len(rejected) != len(body["events"]) \
                or len({r["i"] for r in rejected}) != len(rejected):
            raise ValueError("api response")
        undo = body["stream"] == "imports_undo"
        out["undone" if undo else "accepted"] += reply["accepted"]
        out["duplicates"] += reply["duplicates"]
        for rejected in reply["rejected"]:
            if undo and rejected["error"] == "id":
                out["already_absent"] += 1
            else:
                out["rejected"].append({"i": offset + rejected["i"], "error": rejected["error"]})
        offset += len(body["events"])
    return out
