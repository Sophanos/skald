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
        raise ValueError(f"api {err.code}") from None
    except (urllib.error.URLError, OSError):
        raise ValueError("api unavailable") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("api response") from None


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
    if not apply:
        return {"count": len(events), "undone": len(undone), "chunks": len(bodies),
                "bytes": sum(len(json.dumps(b, allow_nan=False).encode()) for b in bodies),
                "host": base.hostname, "requests": 0}
    out = {"accepted": 0, "duplicates": 0, "rejected": [], "undone": 0, "already_absent": 0, "requests": 0}
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
