#!/usr/bin/env python3
"""Offline loopback wire parity for LEGACY routes and every W20 scope."""
import argparse
from contextlib import ExitStack, contextmanager, redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval.profile_golden import NOW
from eval.rows_report import home_server

BASE_REF = "60989fb"


class BaselineUnavailable(RuntimeError):
    """Git or the requested baseline ref is unavailable."""


@contextmanager
def baseline_source(base=None, base_ref=BASE_REF):
    if base is not None:
        yield Path(base)
        return
    try:
        source = subprocess.run(["git", "show", "--end-of-options", f"{base_ref}:server.py"], cwd=ROOT,
                                capture_output=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise BaselineUnavailable(f"git or baseline ref {base_ref} unavailable: {error}") from error
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp, "server.py")
        path.write_bytes(source)
        yield path


@contextmanager
def loopback_server(s):
    with s.ThreadingHTTPServer(("127.0.0.1", 0), s.Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def get(path):
            with socket.create_connection(server.server_address, timeout=10) as connection:
                connection.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode("ascii"))
                with connection.makefile("rb") as stream:
                    wire = stream.read()
            response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *a, **kw: io.BytesIO(wire)))
            response.begin()
            return response.status, dict(response.getheaders()), response.read(), wire
        try:
            yield get
        finally:
            server.shutdown()
            thread.join()


def report(snapshot, base=None, base_ref=BASE_REF):
    with baseline_source(base, base_ref) as source, redirect_stdout(io.StringIO()), patch("time.time", return_value=NOW), \
            home_server(snapshot, source) as old, home_server(snapshot) as s, ExitStack() as cleanup:
        for server in (old, s):
            cleanup.callback(server._decide_queue.join)
            cleanup.callback(server._decide_queue.put, False)
            cleanup.callback(server.flush_decisions)
        assert s.CONFIG["addon"]["legacy_open"], "route parity requires LEGACY"
        token = s.addon_token()
        # Search alone needs an upstream stub; catalogs use the copied offline state.
        def search(kind, query, lang):
            if query == "offline-error":
                raise ValueError("offline fixture")
            return {"metas": [{"id": "tmdb:1", "type": kind, "name": query}]}
        for server in (old, s):
            server.search = search
            server.log_search = lambda query: None
        catalogs = [(cid, typ) for cid, types, _ in s.feed_rows(s.LANG) for typ in types]
        sessions = [None, "both", *s.PEOPLE]
        checks, hashes = 0, []
        with loopback_server(old) as base_get, loopback_server(s) as get:
            for session in sessions:
                if session is not None:
                    old.write_session({"who": session}, "phone")
                    s.write_session({"who": session}, "phone")
                routes = ["/health", "/stats", "/why/tmdb:1.json"]
                for scope in (None, *s.SCOPES):
                    root = f"/{s.LANG}/" + (f"p/{scope}/" if scope is not None else "")
                    routes.append(root + "manifest.json")
                    routes.extend(root + f"catalog/{typ}/{cid}.json" for cid, typ in catalogs)
                    routes.extend(root + f"catalog/{typ}/{rid('search', s.CONFIG)}/search={query}.json"
                                  for typ in ("movie", "series")
                                  for query in ("fixture", "Am%C3%A9lie", "%E6%97%A5%E6%9C%AC%E8%AA%9E", "a" * 180))
                session_hash = hashlib.sha256()
                for index, route in enumerate(routes):
                    base, current, protected = base_get(route), get(route), get("/" + token + route)
                    assert base[0] == current[0] == 200, f"route failed: session {len(hashes)}, route {index}"
                    assert base == current, f"base wire parity failed: session {len(hashes)}, route {index}"
                    assert current == protected, f"token wire parity failed: session {len(hashes)}, route {index}"
                    assert base_get(route) == protected, f"base wire parity failed: session {len(hashes)}, route {index}"
                    digest = hashlib.sha256(current[2]).digest()
                    session_hash.update(digest)
                    checks += 1
                hashes.append(session_hash.hexdigest())
            errors = [("/unknown", 404), ("/api/v1/decide", 404),
                      (f"/{s.LANG}/p/unknown/manifest.json", 404),
                      (f"/{s.LANG}/catalog/tv/{rid('unknown', s.CONFIG)}.json", 404),
                      ("/plain extra", 400), ("/" + "a" * 43 + "/nope extra", 400),
                      ("/" + "B" * 43 + "/nope extra", 400),
                      ("/" + "a" * 43 + "%2f/nope extra", 400),
                      ("/" + "a" * 43 + "x/nope extra", 400),
                      (f"/{s.LANG}/catalog/movie/{rid('search', s.CONFIG)}/search=offline-error.json", 502)]
            error_hash = hashlib.sha256()
            for index, (route, status) in enumerate(errors):
                base, current = base_get(route), get(route)
                assert base[0] == current[0] == status, f"error route failed: route {index}"
                assert base == current, f"base error wire parity failed: route {index}"
                error_hash.update(hashlib.sha256(current[3]).digest())
    return {"catalogs": len(catalogs), "sessions": len(sessions), "scopes": len(s.SCOPES),
            "pairs": checks, "base_pairs": checks, "error_pairs": len(errors),
            "error_sha256": error_hash.hexdigest(), "requests": checks * 4 + len(errors) * 2, "sha256": hashes,
            "aggregate_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    ap.add_argument("--snapshot", type=Path, default=snapshot if snapshot and snapshot.strip() else None)
    baseline = ap.add_mutually_exclusive_group()
    baseline.add_argument("--base", type=Path, help="explicit baseline server.py")
    baseline.add_argument("--base-ref", default=BASE_REF, help=f"git baseline ref (default: {BASE_REF})")
    args = ap.parse_args()
    if not args.snapshot:
        ap.error("use --snapshot or SKALD_SNAPSHOT")
    try:
        result = report(args.snapshot, args.base, args.base_ref)
    except BaselineUnavailable as error:
        ap.error(f"baseline unavailable: {error}; use --base with a local server.py")
    print(json.dumps(result, sort_keys=True))
