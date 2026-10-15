"""Explicit device login; share the server's bounded transport without starting the engine."""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from . import members
from .members import DATA, AuthMissing, _auth_reference
from .erase import erased as household_erased, writable as household_writable

_path = Path(__file__).resolve().parents[1] / "server.py"
_source = _path.read_text().split("# --- connector transport (also loaded alone by skald/trakt.py) ---", 1)[1].split("# --- end connector transport ---", 1)[0]
exec(compile(_source, str(_path), "exec"))


def login(out, person=None):
    global DATA
    DATA = os.environ.get("DATA_DIR", "/data")
    if person is not None and person not in members.PEOPLE:
        raise ValueError("unknown person")
    cfg = members.CONFIG["connectors"].get("trakt_api", {})
    if "accounts" not in cfg and person is not None and person != members.CONFIG["household"]["trakt_member"]:
        raise ValueError("person has no configured Trakt account")
    if "accounts" in cfg and person not in cfg["accounts"]:
        raise ValueError("trakt login needs --person with a configured Trakt account")
    reference = cfg.get("accounts", {}).get(person) if "accounts" in cfg else cfg.get("auth")
    if not isinstance(reference, str) or not reference.startswith("file:"):
        raise ValueError("trakt login needs connectors.trakt_api.auth = file:NAME")
    directory = None
    try:
        _, name = _auth_reference(reference, DATA)
        directory = _credentials_dir(create=True)
        try:
            os.close(_open_auth_file(name, directory))
        except FileNotFoundError:
            pass
        opts = cfg.get("options", {})
        client_id = _resolve_auth(opts.get("client_id", "env:TRAKT_CLIENT_ID"))
        client_secret = _resolve_auth(opts.get("client_secret", "env:TRAKT_CLIENT_SECRET"))
        headers = {"Content-Type": "application/json"}
        device = _http_get(TRAKT_URL + "/oauth/device/code", headers, body={"client_id": client_id})
        code, user = device["device_code"], device["user_code"]
        interval, expires = device["interval"], device["expires_in"]
        if not isinstance(code, str) or not code or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,32}", user):
            raise ValueError()
        if type(interval) is not int or type(expires) is not int or not 1 <= interval <= 60 or not interval <= expires <= 3600:
            raise ValueError()
        url = device["verification_url"]
        if url not in ("https://trakt.tv/activate", "https://www.trakt.tv/activate"):
            raise UnsafeURL()
        print(f"Open {url} and enter {user}", file=out, flush=True)
        deadline = time.monotonic() + expires
        while time.monotonic() < deadline:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            try:
                token = _http_get(TRAKT_URL + "/oauth/device/token", headers,
                    body={"code": code, "client_id": client_id, "client_secret": client_secret})
            except urllib.error.HTTPError as error:
                if error.code == 400:  # authorization pending
                    continue
                if error.code == 429:
                    interval = min(60, interval + 5)
                    continue
                raise
            _write_trakt_auth(reference, token, directory)
            print("Trakt token saved (0600)", file=out)
            return
        raise TimeoutError()
    except Exception as error:
        raise ValueError("trakt login: " + type(error).__name__) from None
    finally:
        if directory is not None:
            os.close(directory)
