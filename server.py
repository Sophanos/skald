"""Minimal Stremio search addon over the TMDB API, with optional AI recommendations and home rows.

Title searches hit TMDB directly. With OPENROUTER_API_KEY set, a decision model routes
non-title queries; free-text requests ("best of heist", "wie Dark", "80s action")
go to an LLM whose picks are resolved via TMDB and marked with a sparkle.

Home rows are prebuilt in the background (see the Home rows section): serving a row never
calls an LLM, deterministic rows (TMDB/AniList) cost nothing, and the few LLM rows run on a
daily call budget, rebuilding only when their stamp (taste, day) changes. Rows that mix movies and
series are offered three ways: type tv (both kinds interleaved), movie (movies only), series (series
only); AIOStreams' Jellyfin mode files a movie-typed catalog under Movies, which hides its series.

Routes (language in the path, e.g. en-US or fr-FR):
  /{lang}/manifest.json
  /{lang}/catalog/{movie|series}/<row-prefix>.search/search={query}.json
  /{lang}/catalog/{movie|series|tv}/<row-prefix>.<row>.json
  /stats  (AI call counts and cost, row build times)
IDs are emitted as tmdb:{id} so the tmdb-addon meta resource resolves details.
"""
import base64
import copy
import _thread
import queue
import sys
import tempfile
from array import array
import bisect
import contextvars
import datetime
import difflib
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import random
import re
import secrets
import sqlite3
import stat
import struct
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- household config (also loaded alone by skald/members.py) ---
DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "1.0.4"
CONFIG_NAME = "skald.json"
DEFAULT_ROW_PREFIX = "skald"
RESERVED_DATA_FILES = ("addon.json", "skald.json", "legacy.json", "clients.json", "remote.json", ".skald-household", ".skald-format")
ENV_FIRST_RUN = True
MEMBER_COUNTS = (1, 2, 3, 4, 5, 6)
FIRST_RUN_MEMBER_ROWS = False
DEFAULT_REGION = {"lang": "en-US", "tz": "UTC", "country": "United States"}
DEFAULT_ADDON = {"id": "community.skald", "name": "Skald", "version": VERSION, "legacy_open": False}
# Generic fallback; existing households are loaded from DATA/legacy.json.
LEGACY = {
    "v": 1,
    "row_prefix": DEFAULT_ROW_PREFIX,
    "household": {
        "members": [
            "ana",
            "ben"
        ],
        "labels": {
            "ana": "Ana",
            "ben": "Ben",
            "pair": "you both"
        },
        "note": "",
        "archived": {
            "cal": "Cal"
        },
        "trakt_member": "ben",
        "member_rows": [
            "ana"
        ],
        "experiences": {}
    },
    "taste": {
        "household": {
            "likes": "",
            "rewatch_collections": []
        },
        "cal": {
            "likes": "",
            "archived": True
        },
        "people": {
            "ben": {
                "type": ""
            },
            "ana": {
                "type": "",
                "loves": []
            }
        }
    },
    "region": {
        "lang": "en-US",
        "tz": "UTC",
        "country": "United States"
    },
    "addon": {
        "id": "community.skald.legacy",
        "name": "TMDB Search",
        "version": "1.7.3",
        "legacy_open": True
    },
    "history": {
        "source": "file"
    },
    "connectors": {}
}


class ConfigError(ValueError):
    def __init__(self, key, *, missing=False):
        super().__init__(key + " missing: restore it" if missing else "invalid config: " + key)


CONNECTOR_DENY_FILES = set(RESERVED_DATA_FILES) | {"hydra.json"}
_CONNECTOR_AUTH = re.compile(r"^(env|file):[A-Za-z0-9_.-]+$")


class AuthMissing(ValueError):
    pass


def _auth_reference(reference, data=None, environ=None):
    if not isinstance(reference, str) or not _CONNECTOR_AUTH.fullmatch(reference):
        raise AuthMissing() from None
    kind, name = reference.split(":", 1)
    if name in (".", ".."):
        raise AuthMissing() from None
    if kind == "file":
        env = os.environ if environ is None else environ
        data = data or env.get("DATA_DIR") or globals().get("DATA", "/data")
        path = os.path.join(data, "credentials", name)
        active = env.get("SKALD_CONFIG") or os.path.join(data, CONFIG_NAME)
        active_target = os.path.realpath(path) == os.path.realpath(active)
        try:
            active_target |= os.path.samefile(path, active)
        except OSError:
            pass
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}\.(json|token|txt)", name) or name in CONNECTOR_DENY_FILES or active_target or (
                os.path.lexists(os.path.join(data, name)) and not os.path.lexists(path)):
            raise AuthMissing("file auth: move the file into DATA/credentials/ and use file:NAME; "
                              "NAME must match [a-z0-9][a-z0-9_-]{0,63}.(json|token|txt)") from None
    return kind, name


def invalid_json(_):
    raise ConfigError("skald.json")


def fresh_config(members):
    return {"v": 1, "row_prefix": DEFAULT_ROW_PREFIX, "household": {"members": members,
            "labels": {**{p: p.title() for p in members}, "pair": "you both" if len(members) == 2 else "everyone"},
            "note": "", "archived": {}, "trakt_member": members[0] if members else "",
            "member_rows": list(members) if FIRST_RUN_MEMBER_ROWS else [], "experiences": {}},
            "taste": {"household": {"likes": "", "rewatch_collections": []}, "people": {p: {} for p in members}},
            "region": dict(DEFAULT_REGION), "addon": dict(DEFAULT_ADDON),
            "history": {"source": "none"}, "connectors": {}}


def validate_addon_config(cfg):
    if type(cfg["addon"]["legacy_open"]) is not bool:
        raise ConfigError("addon.legacy_open")


def validate_history_connectors(cfg, need, data=None, environ=None):
    history, connectors = cfg["history"], cfg["connectors"]
    need(isinstance(history, dict) and set(history) == {"source"}, "history")
    source = history["source"]
    need(source in ("file", "aiostreams_db", "jellyfin", "plex", "none"), "history.source")
    need(isinstance(connectors, dict), "connectors")
    connectors = cfg["connectors"] = copy.deepcopy(connectors)
    def reference(value):
        try:
            _auth_reference(value, data, environ)
            return True
        except AuthMissing as error:
            if str(error):
                raise ConfigError("connectors.auth: " + str(error)) from None
            return False
    for cid, c in connectors.items():
        need(isinstance(cid, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", cid), "connectors.id")
        key = "connectors." + cid
        need(isinstance(c, dict) and set(c) <= {"kind", "enabled", "auth", "options", "accounts"}, key)
        need("accounts" not in c or cid == "trakt_api", key + ".accounts")
        need(c.get("kind") in ("file_drop", "manual_export", "api", "player", "availability"), key + ".kind")
        need(type(c.get("enabled")) is bool, key + ".enabled")
        need(c.get("auth") is None or cid == "nzbhydra" and c.get("auth") == "file:hydra.json" or
             reference(c["auth"]), key + ".auth")
        opts = c.get("options", {})
        need(isinstance(opts, dict), key + ".options")
        allowed = {"aiostreams_db": {"path", "profiles", "archived"},
                   "jellyfin": {"url", "user_id", "section", "profile", "users"},
                   "plex": {"url", "section", "profile"},
                   "trakt_api": {"client_id", "client_secret"}}.get(cid, set())
        need(set(opts) <= allowed, key + ".options")
        if cid == "aiostreams_db":
            need(c["kind"] == "player" and c.get("auth") is None, key)
            need(isinstance(opts.get("path"), str) and os.path.isabs(opts["path"]) and "\x00" not in opts["path"], key + ".options.path")
            profiles, archived = opts.get("profiles"), opts.get("archived", {})
            need(isinstance(profiles, dict) and bool(profiles) and isinstance(archived, dict), key + ".options.profiles")
            for mapping in (profiles, archived):
                need(all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9-]{8}", p) and
                         name in (*cfg["household"]["members"], *cfg["household"]["archived"], "household")
                         for p, name in mapping.items()), key + ".options.profiles")
            prefixes = [p.lower() for mapping in (profiles, archived) for p in mapping]
            need(len(set(prefixes)) == len(prefixes), key + ".options.profiles")
            opts["profiles"] = {p.lower(): name for p, name in profiles.items()}
            opts["archived"] = {p.lower(): name for p, name in archived.items()}
        elif cid in ("jellyfin", "plex"):
            from urllib.parse import urlsplit
            need(c["kind"] == "player" and reference(c.get("auth")), key)
            need(isinstance(opts.get("url"), str) and opts["url"].strip() and
                 not any(ord(ch) < 32 for ch in opts["url"]), key + ".options.url")
            try:
                url = urlsplit(opts["url"])
                valid_url = url.scheme in ("http", "https") and url.hostname and not (
                    url.username is not None or url.password is not None or url.query or url.fragment)
            except ValueError:
                valid_url = False
            need(valid_url, key + ".options.url")
            if cid == "jellyfin" and "users" in opts:
                need("user_id" not in opts and "profile" not in opts, key + ".options.users")
                need(isinstance(opts["users"], dict) and bool(opts["users"]) and all(
                    isinstance(uid, str) and uid.strip() and not any(ord(ch) < 32 for ch in uid) and
                    person in (*cfg["household"]["members"], "household")
                    for uid, person in opts["users"].items()), key + ".options.users")
            for field in (("user_id",) if cid == "jellyfin" and "users" not in opts else ()) + (("section",) if "section" in opts else ()):
                need(isinstance(opts.get(field), str) and opts[field].strip() and
                     not any(ord(ch) < 32 for ch in opts[field]), key + ".options." + field)
            need(opts.get("profile", "household") in (*cfg["household"]["members"], "household"), key + ".options.profile")
        elif cid == "trakt_api":
            need(c["kind"] == "api", key)
            if "accounts" in c:
                accounts = c["accounts"]
                need(isinstance(accounts, dict) and bool(accounts), key + ".accounts")
                need(all(person in cfg["household"]["members"] for person in accounts), key + ".accounts")
                need(all(reference(value) and value.startswith("file:") for value in accounts.values()), key + ".accounts")
                need(c.get("auth") is None and len(set(accounts.values())) == len(accounts), key + ".accounts")
            else:
                need(reference(c.get("auth")), key)
            need(all(reference(value) for value in opts.values()), key + ".options")
        elif cid == "nzbhydra":
            need(c["kind"] == "availability" and c.get("auth") == "file:hydra.json", key)
        else:
            need(not c["enabled"] or cid in ("history", "trakt") and c["kind"] == "file_drop", key + ": not built yet")
    if source in ("aiostreams_db", "jellyfin", "plex"):
        need(connectors.get(source, {}).get("enabled") is True, "history.source")


def validate_config(doc, *, data=None, environ=None):
    def need(ok, key):
        if not ok:
            raise ConfigError(key)
    need(isinstance(doc, dict), "skald.json")
    need(type(doc.get("v")) is int and doc["v"] == 1, "v")
    hh = doc.get("household")
    need(isinstance(hh, dict), "household")
    members = hh.get("members")
    need(isinstance(members, list) and len(members) in MEMBER_COUNTS, "household.members")
    need(all(isinstance(p, str) and re.fullmatch(r"[a-z][a-z0-9]{1,15}", p) and
             p not in ("guest", "both", "together", "household") for p in members), "household.members")
    need(len(set(members)) == len(members), "household.members")
    cfg = fresh_config(members)
    for section in ("household", "region", "addon"):
        value = doc.get(section, {})
        need(isinstance(value, dict), section)
        cfg[section].update(value)
    cfg.update({k: v for k, v in doc.items() if k not in ("household", "region", "addon")})
    need(isinstance(cfg["row_prefix"], str) and re.fullmatch(r"[a-z][a-z0-9]{1,15}", cfg["row_prefix"]), "row_prefix")
    hh = cfg["household"]
    labels = hh["labels"]
    need(isinstance(labels, dict), "household.labels")
    need(all(isinstance(labels.get(p), str) and labels[p].strip() and
             not any(ord(c) < 32 for c in labels[p]) for p in (*members, "pair")), "household.labels")
    need(isinstance(hh["note"], str), "household.note")
    archived = hh["archived"]
    need(isinstance(archived, dict), "household.archived")
    need(all(isinstance(p, str) and re.fullmatch(r"[a-z][a-z0-9]{1,15}", p) and
             p not in (*members, "guest", "both", "together", "household") and isinstance(label, str) and
             label.strip() and not any(ord(c) < 32 for c in label) for p, label in archived.items()), "household.archived")
    need(hh["trakt_member"] in members, "household.trakt_member")
    rows = hh["member_rows"]
    need(isinstance(rows, list) and all(p in members for p in rows) and len(set(rows)) == len(rows), "household.member_rows")
    xs = hh["experiences"]
    need(isinstance(xs, dict) and all(p in members and isinstance(tags, list) and
             all(isinstance(x, str) for x in tags) for p, tags in xs.items()), "household.experiences")
    taste = cfg["taste"]
    need(isinstance(taste, dict), "taste")
    taste = cfg["taste"] = copy.deepcopy(taste)
    for p in archived:
        taste.setdefault(p, {"likes": "", "archived": True})
    need(all(isinstance(taste.get(k, {}), dict) for k in ("household", "people", *archived)), "taste")
    need(isinstance(taste.get("household", {}).get("likes", ""), str), "taste.household.likes")
    ids = taste.get("household", {}).get("rewatch_collections", [])
    need(isinstance(ids, list) and all(type(i) is int and i > 0 for i in ids), "taste.household.rewatch_collections")
    need(all(p in members and isinstance(v, dict) for p, v in taste.get("people", {}).items()), "taste.people")
    for p, traits in taste.get("people", {}).items():
        need(isinstance(traits.get("type", ""), str), "taste.people." + p + ".type")
        loves = traits.get("loves", [])
        need(isinstance(loves, list) and all(isinstance(t, str) for t in loves), "taste.people." + p + ".loves")
    for p in archived:
        need(isinstance(taste[p].get("likes", ""), str), "taste." + p + ".likes")
        need(type(taste[p].get("archived", True)) is bool, "taste." + p + ".archived")
    region = cfg["region"]
    need(isinstance(region["lang"], str) and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", region["lang"]), "region.lang")
    need(isinstance(region["country"], str) and region["country"].strip() and
         not any(ord(c) < 32 for c in region["country"]), "region.country")
    need(isinstance(region["tz"], str), "region.tz")
    try:
        if region["tz"] not in ("UTC", "Europe/Berlin"):
            ZoneInfo(region["tz"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("region.tz") from None
    addon = cfg["addon"]
    need(isinstance(addon["id"], str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", addon["id"]), "addon.id")
    for key in ("name", "version"):
        need(isinstance(addon[key], str) and addon[key].strip() and
             not any(ord(c) < 32 for c in addon[key]), "addon." + key)
    validate_addon_config(cfg)
    validate_history_connectors(cfg, need, data, environ)
    return cfg


def stamp_household(data):
    path = os.path.join(data, ".skald-household")
    try:
        if not os.path.lexists(path):
            os.makedirs(data, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".skald-", dir=data)
            os.close(fd)
            try:
                os.link(temp, path)
            except FileExistsError:
                pass
            finally:
                os.unlink(temp)
        with open(path, "rb") as stamp:
            os.fsync(stamp.fileno())
        directory = os.open(data, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise ConfigError("skald.json") from None


def publish_config(data, path, cfg, *, replace=False, on_publish=None):
    """The stamp precedes atomic publication; first start never overwrites a winner."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    stamp_household(data)
    fd, temp = tempfile.mkstemp(prefix=".skald-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o644)
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if replace:
            os.replace(temp, path)
        else:
            try:
                os.link(temp, path)
            except FileExistsError:
                return False
        if on_publish is not None:
            on_publish()
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_config(data=None, environ=None, *, readonly=False):
    env = os.environ if environ is None else environ
    data = data or env.get("DATA_DIR", "/data")
    path = env.get("SKALD_CONFIG") or os.path.join(data, CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = validate_config(json.load(fh, parse_constant=invalid_json), data=data, environ=env)
    except FileNotFoundError:
        stamp = os.path.join(data, ".skald-household")
        existing = any(os.path.isfile(os.path.join(data, name)) for name in
                       ("legacy.json", "profile.json", "history.json", "trakt.json", "feeds.json", "taste.json",
                        "imports.jsonl", "feedback.jsonl")) or any(
                            name.endswith(".history.json") and os.path.isfile(os.path.join(data, name))
                            for name in (os.listdir(data) if os.path.isdir(data) else ()))
        if os.path.lexists(stamp) or existing and "SKALD_MEMBERS" in env:
            raise ConfigError("skald.json", missing=True)
        if existing:
            try:
                with open(os.path.join(data, "legacy.json"), encoding="utf-8") as fh:
                    cfg = validate_config(json.load(fh, parse_constant=invalid_json), data=data, environ=env)
            except FileNotFoundError:
                raise ConfigError("legacy.json", missing=True) from None
            except (ConfigError, OSError, UnicodeError, json.JSONDecodeError, RecursionError):
                raise ConfigError("legacy.json") from None
            return cfg, "legacy"
        if not ENV_FIRST_RUN or "SKALD_MEMBERS" not in env:
            return copy.deepcopy(LEGACY), "implicit"
        cfg = fresh_config(env["SKALD_MEMBERS"].split(","))
        cfg["region"].update({key: env[name] for key, name in
            (("lang", "SKALD_LANG"), ("tz", "SKALD_TZ"), ("country", "SKALD_COUNTRY")) if name in env})
        cfg = validate_config(cfg, data=data, environ=env)
        if readonly:
            return cfg, "env"
        try:
            if not publish_config(data, path, cfg):
                return load_config(data, env)
        except OSError:
            raise ConfigError("skald.json") from None
        return cfg, "env"
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConfigError("skald.json") from None
    if not readonly:
        stamp_household(data)
    changed = "SKALD_MEMBERS" in env and env["SKALD_MEMBERS"].split(",") != cfg["household"]["members"]
    changed |= any(name in env and env[name] != cfg["region"][key] for key, name in
        (("lang", "SKALD_LANG"), ("tz", "SKALD_TZ"), ("country", "SKALD_COUNTRY")))
    if changed:
        print("skald.json: household environment ignored", file=sys.stderr, flush=True)
    return cfg, "file"


def require_config(source, error=None):
    if error:
        raise error
    if source == "implicit":
        raise ValueError("empty DATA: set SKALD_MEMBERS=ana,ben or provide skald.json")


def rid(name, config=None):
    return (CONFIG if config is None else config).get("row_prefix", DEFAULT_ROW_PREFIX) + "." + name


def public_config(cfg):
    # Unknown extensions can contain credentials; print only the supported household fields.
    schema = fresh_config(cfg["household"]["members"])
    out = {"v": cfg["v"], "row_prefix": cfg.get("row_prefix", DEFAULT_ROW_PREFIX), **{k: {p: cfg[k][p] for p in schema[k]} for k in ("household", "region", "addon")}}
    out["household"] = {**out["household"], "labels": {p: cfg["household"]["labels"][p] for p in (*cfg["household"]["members"], "pair")}}
    out["taste"] = {p: {k: v for k, v in traits.items() if k in ("likes", "archived", "rewatch_collections")}
                    for p, traits in cfg["taste"].items() if p in ("household", *cfg["household"]["archived"])}
    out["taste"]["people"] = {p: {k: v for k, v in traits.items() if k in ("type", "loves")}
                               for p, traits in cfg["taste"].get("people", {}).items()}
    out.update(history=cfg["history"], connectors=copy.deepcopy(cfg["connectors"]))
    return out
# --- end household config ---

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skald import migrate
try:
    if globals().get("__name__") == "__main__" and (not sys.argv[1:] or migrate.check(DATA)["format"] < migrate.FORMAT):
        migrate.ensure(DATA)
    else:
        migrate.check(DATA)
except (ValueError, OSError) as e:
    if globals().get("__name__") == "__main__":
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None
    raise

CONFIG_ERROR = None
try:
    CONFIG, CONFIG_SOURCE = load_config(DATA, readonly=globals().get("_CONFIG_READONLY", __name__ != "__main__" or bool(sys.argv[1:])))
except ConfigError as e:
    CONFIG, CONFIG_SOURCE, CONFIG_ERROR = copy.deepcopy(LEGACY), "invalid", e
PEOPLE = tuple(CONFIG["household"]["members"])
SCOPES = {**{p: p for p in PEOPLE}, "together": "both"}
LABELS = CONFIG["household"]["labels"]
PAIR_LABEL = LABELS["pair"].removeprefix("you ").title()
ARCHIVED = CONFIG["household"]["archived"]
MEMBER_ROWS = tuple(CONFIG["household"]["member_rows"])
ROW_OWNER = {rid(p): p for p in MEMBER_ROWS}
TRAKT_PERSON = CONFIG["household"]["trakt_member"]
DEFAULT_TASTE = CONFIG["taste"]
LANG = CONFIG["region"]["lang"]
TZ = CONFIG["region"]["tz"]
COUNTRY = CONFIG["region"]["country"]
EXPERIENCE_TAGS = {p: set(xs) for p, xs in CONFIG["household"]["experiences"].items()}
MEMBER_COUNT = ("one", "two", "three", "four", "five", "six")[len(PEOPLE) - 1]
HOUSEHOLD_KIND = "a couple" if len(PEOPLE) == 2 else "a household"
MEMBER_NAMES = " and ".join(LABELS[p] for p in PEOPLE)
REGION_LANGUAGE = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
                   "nl": "Dutch", "pt": "Portuguese", "sv": "Swedish", "da": "Danish", "nb": "Norwegian",
                   "fi": "Finnish", "pl": "Polish", "ja": "Japanese", "ko": "Korean"}.get(LANG[:2], "local")
MEMBER_PICK_RE = {p: re.compile(r"For (" + re.escape(LABELS[p]) + "|" + re.escape(LABELS["pair"]) + ")", re.I) for p in PEOPLE}
WARM_RE = re.compile(r"For (" + "|".join(re.escape(LABELS[p]) for p in ("pair", *PEOPLE)) + "):")


GUEST = "guest"
PCT_MIN_EXPLICIT = 20
PRIOR_FULL = 40
HOUSEHOLD_PRIOR = True


def who_ok(who, *extra):
    return who in (*PEOPLE, *extra)


API_KEY = os.environ.get("TMDB_API", "")
PORT = int(os.environ.get("PORT", "8080"))
IMG = "https://image.tmdb.org/t/p/"
LANG_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
CACHE_TTL = 600
CACHE_MAX = 4000
_cache = {}
_upstream = threading.local()
_upstream_lock = threading.Lock()


class BuildPool(ThreadPoolExecutor):
    # Builders swallow leaf errors; carry their failure counter through nested pool work.
    def submit(self, fn, /, *args, **kwargs):
        failures = getattr(_upstream, "failures", None)
        if failures is None:
            return super().submit(fn, *args, **kwargs)
        def run():
            previous = getattr(_upstream, "failures", None)
            _upstream.failures = failures
            try:
                return fn(*args, **kwargs)
            finally:
                _upstream.failures = previous
        return super().submit(run)


def upstream_failed():
    failures = getattr(_upstream, "failures", None)
    if failures is not None:
        with _upstream_lock:
            failures[0] += 1


_pool = BuildPool(max_workers=32)  # leaf TMDB calls only
_outer = BuildPool(max_workers=16)  # jobs waiting on _pool leaf calls
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MARK = "✨ "
AI_MIN_LEN = 4
SEARCH_SETTLE = 0.35  # seconds a non-title query waits for the next keystroke before spending an AI call
ROUTE_MODEL = "openai/gpt-6-luna-decisions"
PICK_MODEL = "openai/gpt-6-luna"
ROUTE_Q = {"route": {"type": "choice",
    "instructions": "A user typed this into the search box of a movie/TV streaming app. Classify what they want.",
    "criteria": {
        "title": "The name (or partial name) of a specific movie, series, person or franchise, e.g. 'Paddington', 'dark', 'Avengers', 'Haus des Geldes', 'Tom Hanks'.",
        "discover": "Only a genre and/or year/decade filter, e.g. '80s action', 'Komödie 1995', 'horror 2023', 'Thriller der 90er'.",
        "request": "A free-text wish, mood, theme, similarity or curated list, e.g. 'best of heist', 'like Dark', 'filme über zeitreisen', 'something cozy for sunday', 'playlist sad anime'."}}}
PICK_SYS = ("You recommend movies and TV series for a streaming search box. Reply with exactly 20 lines, nothing else, "
            "format: Title|Year|m or s  (m=movie, s=series). Real, well-known titles that best match the request, best first. "
            "For 'best/greatest' requests, use broad critical consensus, not one person's taste, across decades and countries. "
            "'All time' means across decades, including older classics. Include at least 7 series and 7 movies unless "
            "the request clearly asks for only one type (movies/films or series/shows/anime); then use only that type.")
_ai = {}
_ai_lock = threading.Lock()


class BudgetExceeded(Exception):
    pass


def tmdb(path, *, _timeout=8, **params):
    params["api_key"] = API_KEY
    url = f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(params)}"
    hit = _cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            data = json.load(r)
    except Exception:
        upstream_failed()
        raise
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[url] = (time.time() + CACHE_TTL, data)
    return data


# ---------------------------------------------------------------------------
# AI budget: every OpenRouter call is counted per lane and day; a lane over its daily cap
# raises BudgetExceeded and callers fall back (plain TMDB search, last built row).

AI_CAPS = {"feed": int(os.environ.get("AI_DAILY_FEED_CALLS", "40")),
           "tags": int(os.environ.get("AI_DAILY_TAG_CALLS", "60")),  # batched: 25 titles per call, once per title
           "search": int(os.environ.get("AI_DAILY_SEARCH_CALLS", "300")),
           "resolve": int(os.environ.get("AI_DAILY_RESOLVE_CALLS") or 20),
           "embed": int(os.environ.get("AI_DAILY_EMBED_CALLS", "150"))}  # batched: up to 96 titles per call
API_LANE_SHARE = float(os.environ.get("API_LANE_SHARE") or .34)
AI_MONTHLY_USD = float(os.environ.get("AI_MONTHLY_USD", "3"))  # hard stop below the OpenRouter key limit
_usage = {"day": "", "calls": {}, "cost": 0.0, "tokens": 0, "month": "", "month_cost": 0.0, "month_calls": 0}
_usage_lock = threading.Lock()


def _usage_roll():
    d = local_now()
    if _usage["day"] != d.strftime("%Y-%m-%d"):
        _usage.update(day=d.strftime("%Y-%m-%d"), calls={}, cost=0.0, tokens=0)
    if _usage["month"] != d.strftime("%Y-%m"):
        _usage.update(month=d.strftime("%Y-%m"), month_cost=0.0, month_calls=0)


def save_usage():
    with _usage_lock:
        snap = json.dumps(_usage)
    try:
        with open(os.path.join(DATA, "usage.json.tmp"), "w") as fh:
            fh.write(snap)
        os.replace(os.path.join(DATA, "usage.json.tmp"), os.path.join(DATA, "usage.json"))
    except OSError:
        pass


def openrouter(path, body, timeout, lane, label):
    with _usage_lock:
        _usage_roll()
        used = sum(n for k, n in _usage["calls"].items() if k.startswith(lane + ":"))
        if used >= AI_CAPS[lane]:
            raise BudgetExceeded(f"{lane} budget {AI_CAPS[lane]}/day used")
        if _usage["month_cost"] >= AI_MONTHLY_USD:
            raise BudgetExceeded(f"monthly budget ${AI_MONTHLY_USD} used")
        key = f"{lane}:{label}"
        _usage["calls"][key] = _usage["calls"].get(key, 0) + 1
        _usage["month_calls"] += 1
    req = urllib.request.Request("https://openrouter.ai" + path, json.dumps(body).encode(), {
        "Authorization": "Bearer " + OR_KEY, "Content-Type": "application/json", "X-Title": "Skald"})
    with _connector_open(req, timeout=timeout) as r:
        raw = r.read(CONNECTOR_HTTP_BYTES + 1)
        if len(raw) > CONNECTOR_HTTP_BYTES:
            raise ByteCapExceeded()
        j = json.loads(raw)
    usage = j.get("usage") or {}
    with _usage_lock:
        _usage["cost"] += usage.get("cost") or 0
        _usage["month_cost"] += usage.get("cost") or 0
        _usage["tokens"] += usage.get("total_tokens") or 0
    threading.Thread(target=save_usage, daemon=True).start()
    return j


# --- decisions ---
# Add a type: 1 copy this block; 2 set budget/schema; 3 write norm/rule/fallback
# (and prompt/parse); 4 add >=20 fixtures; 5 run the generic contract test.
# decision("copy_me", ver=1, lane=None, cap=None, ttl=DAY,
#     inp={"mood": ("cozy", "dark")}, out={"ok": bool}, norm=lambda x: x,
#     rule=lambda x, p: ({"ok": True}, 1), params={}, llm=None,
#     fallback=lambda x, err: ({"ok": False}, 0), requires=(), serving=True,
#     min_acc=1)  # max_usd and fixtures have defaults.
TKEY = "tmdb:N"
KIND = ("movie", "tv")
TITLE = {"key": TKEY, "kind": KIND}
RANK_INP = {"choices": [TITLE, 25], "person": (*PEOPLE, "together", GUEST, None)}
RANK_OUT = {"ranked": [TITLE, 25], "pick": (TITLE, None), "why": (str, None), "match": (int, None)}
DAY = 86400
DECIDE_MAX_USD = float(os.environ.get("DECIDE_MAX_USD") or .02)
assert math.isfinite(DECIDE_MAX_USD) and DECIDE_MAX_USD >= 0
DECISIONS = {}


def rank_norm(inp):
    choices = list({(c["key"], c["kind"]): c for c in inp["choices"]}.values())
    if not choices:
        raise ValueError("choices")
    return {**inp, "choices": choices, "person": inp.get("person")}


def rank_rule(score=None):
    def rule(inp, params):
        scored, unscored = [], []
        for choice in inp["choices"]:
            view = title_view(choice["key"], choice["kind"], inp["person"])
            value = (score(view, inp, params) if score is not None else view["match"]) if view else None
            if value is None:
                unscored.append(choice)
            else:
                scored.append((value, choice, view))
        scored.sort(key=lambda row: -row[0])
        pick, view = (scored[0][1], scored[0][2]) if scored else (None, None)
        match = view["match"] if view else None
        return {"ranked": [row[1] for row in scored] + unscored, "pick": pick,
                "why": next(iter(view["reasons"]), None) if view else None, "match": match}, \
            match / 100 if match is not None else 0.0
    return rule


def rank_fallback(inp, err):
    return {"ranked": inp["choices"], "pick": None, "why": None, "match": None}, 0.0


def check(schema, value, path=""):
    if isinstance(schema, tuple):
        for member in schema:
            try:
                check(member, value, path)
                return
            except ValueError:
                pass
    elif isinstance(schema, dict):
        if isinstance(value, dict):
            for key in value:
                if key not in schema:
                    raise ValueError(f"{path}.{key}".strip("."))
            for key, member in schema.items():
                check(member, value.get(key), f"{path}.{key}".strip("."))
            return
    elif isinstance(schema, list):
        if isinstance(value, list) and len(value) <= (schema[1] if len(schema) == 2 else 25):
            for i, item in enumerate(value):
                check(schema[0], item, f"{path}[{i}]")
            return
    elif schema is str:
        if isinstance(value, str) and 0 < len(value) <= 500:
            return
    elif schema is int or schema is float:
        if type(value) in ((int,) if schema is int else (int, float)):
            try:
                if math.isfinite(value):
                    return
            except OverflowError:
                pass
    elif schema is bool:
        if type(value) is bool:
            return
    elif schema == TKEY:
        if isinstance(value, str) and re.fullmatch(r"tmdb:[1-9][0-9]{0,8}", value):
            return
    elif type(schema) is type(value) and schema == value:
        return
    raise ValueError(path)


def _decision_schema(schema):
    if isinstance(schema, dict):
        assert all(isinstance(k, str) for k in schema)
        for key, member in schema.items():
            if member == TKEY:
                assert key == "key" and schema.get("kind") == KIND
            else:
                _decision_schema(member)
    elif isinstance(schema, list):
        assert len(schema) in (1, 2)
        assert len(schema) == 1 or type(schema[1]) is int and schema[1] >= 0
        _decision_schema(schema[0])
    elif isinstance(schema, tuple):
        assert schema
        for member in schema:
            _decision_schema(member)
    else:
        assert schema != TKEY
        assert schema in (str, int, float, bool, None) or type(schema) in (str, int, float, bool)


def decision(name, **spec):
    assert isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name) and name not in DECISIONS
    fields = set("ver lane cap ttl max_usd inp out norm rule params llm fallback requires serving min_acc fixtures api".split())
    assert not set(spec) - fields
    spec.setdefault("max_usd", DECIDE_MAX_USD)
    spec.setdefault("fixtures", f"eval/fixtures/decisions/{name}.jsonl")
    spec.setdefault("lane", None)
    spec.setdefault("api", True)
    assert set(spec) == fields
    assert type(spec["ver"]) is int and spec["ver"] >= 1
    assert spec["lane"] is None or isinstance(spec["lane"], str) and spec["lane"] in AI_CAPS
    assert spec["cap"] is None or type(spec["cap"]) is int and spec["cap"] >= 0
    for field in ("ttl", "max_usd", "min_acc"):
        assert type(spec[field]) in (int, float) and math.isfinite(spec[field]) and spec[field] >= 0
    assert spec["min_acc"] <= 1 and type(spec["serving"]) is bool and type(spec["api"]) is bool
    assert isinstance(spec["params"], dict) and isinstance(spec["fixtures"], str)
    assert isinstance(spec["requires"], tuple) and all(isinstance(x, str) for x in spec["requires"])
    assert callable(spec["norm"]) and callable(spec["fallback"])
    assert spec["rule"] is None or callable(spec["rule"])
    assert isinstance(spec["inp"], dict) and isinstance(spec["out"], dict)
    _decision_schema(spec["inp"])
    _decision_schema(spec["out"])
    llm = spec["llm"]
    if llm is not None:
        assert isinstance(llm, dict) and set(llm) == set("path model batch timeout prompt parse".split())
        assert spec["lane"] in AI_CAPS and not spec["serving"]
        assert isinstance(llm["path"], str) and llm["path"].startswith("/api/")
        assert isinstance(llm["model"], str) and llm["model"]
        assert type(llm["batch"]) is int and 1 <= llm["batch"] <= 25
        assert type(llm["timeout"]) in (int, float) and math.isfinite(llm["timeout"]) and llm["timeout"] > 0
        assert callable(llm["prompt"]) and callable(llm["parse"])
    spec["max_usd"] = min(spec["max_usd"], DECIDE_MAX_USD)
    DECISIONS[name] = spec
    return spec


def _decision_free(schema):
    if schema is str:
        return True
    if isinstance(schema, dict):
        return any(_decision_free(s) for s in schema.values())
    if isinstance(schema, (tuple, list)):
        return any(_decision_free(s) for s in (schema[:1] if isinstance(schema, list) else schema))
    return False


def _decision_answer(spec, pair):
    answer, conf = pair
    check(spec["out"], answer)
    check(float, conf)
    if not 0 <= conf <= 1:
        raise ValueError("confidence")
    return answer, conf


_decide_lock = threading.Lock()
_decide_cache = {}
_decide_flights = {}
_decide_day = ""
_decide_counts = {}
_decide_over = set()
_decide_queue = queue.Queue()


def _decision_roll():
    global _decide_day
    _usage_roll()
    if _decide_day != _usage["day"]:
        _decide_day = _usage["day"]
        _decide_counts.clear()
        _decide_over.clear()


def _decision_gate(name, spec, allow_llm, limit, client=None, reservation=None):
    if spec["serving"]:
        return "serving"
    if not allow_llm:
        return "allow_llm"
    if any(not globals().get(x) for x in spec["requires"]):
        return "requires"
    with _usage_lock:
        _decision_roll()
        if name in _decide_over:
            return "over"
        used = _usage["calls"].get(f"{spec['lane']}:{name}", 0)
        if spec["cap"] is not None and used >= spec["cap"]:
            return "cap"
        if _usage["month_cost"] + limit > AI_MONTHLY_USD:
            return "month"
        if spec["llm"] is None:
            return "llm"
        if client is not None and reservation is not None:
            key = f"api:{spec['lane']}"
            used = _usage["calls"].get(key, 0)
            if used >= int(AI_CAPS[spec["lane"]] * API_LANE_SHARE):
                return "api_share"
            # Reserve once per new batch; cache hits and in-flight followers spend nothing.
            _usage["calls"][key] = used + 1
            reservation["day"] = _usage["day"]
    # Concurrent pre-checks may pass together: overshoot <= max_usd * concurrency.
    return None


def _decision_hit(key):
    hit = _decide_cache.get(key)
    return hit if hit and hit["expires"] > time.time() else None


def _decision_put(key, spec, answer, conf):
    with _decide_lock:
        if key not in _decide_cache and len(_decide_cache) >= 5000:
            for old in list(_decide_cache)[:2500]:
                del _decide_cache[old]
        _decide_cache[key] = {"expires": time.time() + spec["ttl"], "answer": answer, "confidence": conf}


def _decision_cache_snapshot():
    with _decide_lock:
        return {k: v for k, v in _decide_cache.items() if v["expires"] > time.time()
                and (s := DECISIONS.get(k.split("|")[0])) and s["ttl"] >= DAY and not _decision_free(s["out"])}


def _decision_write_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".decide-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(value, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _decision_writer():
    lines, dirty, last_save, logged = {}, None, 0, False
    while True:
        try:
            task = _decide_queue.get(timeout=max(.01, 10 - (time.monotonic() - last_save)) if dirty else None)
        except queue.Empty:
            task = None
        try:
            if task is False:
                return
            if task and not isinstance(task, threading.Event):
                data, rec, persist = task
                path = os.path.join(data, "decisions.jsonl")
                if path not in lines:
                    try:
                        with open(path) as fh:
                            lines[path] = sum(1 for _ in fh)
                    except FileNotFoundError:
                        lines[path] = 0
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
                lines[path] += 1
                if lines[path] > 5000:
                    with open(path) as fh:
                        keep = list(deque(fh, maxlen=4000))
                    fd, tmp = tempfile.mkstemp(prefix=".decisions-", dir=data)
                    try:
                        with os.fdopen(fd, "w") as fh:
                            fh.writelines(keep)
                            fh.flush()
                            os.fsync(fh.fileno())
                        os.replace(tmp, path)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    lines[path] = len(keep)
                if persist:
                    dirty = data
            if dirty and time.monotonic() - last_save >= 10:
                _decision_write_json(os.path.join(dirty, "decide_cache.json"), _decision_cache_snapshot())
                dirty, last_save = None, time.monotonic()
        except Exception as err:
            if not logged:
                print(f"decision writer: {type(err).__name__}", file=sys.stderr)
                logged = True
            dirty = None
        finally:
            if isinstance(task, threading.Event):
                task.set()
            if task is not None:
                _decide_queue.task_done()


# Low-level threads are daemons; import starts only this idle persistence worker.
_thread.start_new_thread(_decision_writer, ())


def flush_decisions(timeout=2):
    done = threading.Event()
    _decide_queue.put(done)
    return done.wait(timeout)


def load_decisions():
    try:
        with open(os.path.join(DATA, "decide_cache.json")) as fh:
            cached = json.load(fh)
        with _decide_lock:
            for key, value in list(cached.items())[-5000:]:
                try:
                    name, ver, digest = key.split("|")
                    spec = DECISIONS.get(name)
                    if not spec or ver != str(spec["ver"]) or spec["ttl"] < DAY or _decision_free(spec["out"]):
                        continue
                    if not re.fullmatch(r"[0-9a-f]{32}", digest):
                        continue
                    if not isinstance(value, dict) or set(value) != {"expires", "answer", "confidence"}:
                        continue
                    _decision_answer(spec, (value["answer"], value["confidence"]))
                    if type(value["expires"]) in (int, float) and math.isfinite(value["expires"]) and value["expires"] > time.time():
                        if key not in _decide_cache and len(_decide_cache) >= 5000:
                            for old in list(_decide_cache)[:2500]:
                                del _decide_cache[old]
                        _decide_cache[key] = value
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    try:
        with open(os.path.join(DATA, "decisions.jsonl")) as fh:
            records = list(deque(fh, maxlen=5000))
        with _usage_lock:
            _decision_roll()
            for raw in records:
                try:
                    rec = json.loads(raw)
                    if local_now(rec["t"]).strftime("%Y-%m-%d") != _decide_day:
                        continue
                    name = rec["type"]
                    if name not in DECISIONS:
                        continue
                    src, cost = rec["src"], rec.get("cost") or 0
                    if src not in ("rule", "cache", "llm", "fallback"):
                        continue
                    check(float, cost, "cost")
                    counts = _decide_counts.setdefault(name, {"sources": {}, "cost": 0.0})
                    mix = counts["sources"]
                    mix[src] = mix.get(src, 0) + 1
                    counts["cost"] += cost
                    if rec.get("over"):
                        _decide_over.add(name)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    continue
    except (OSError, ValueError, TypeError, KeyError):
        pass


def decide(name, inp, *, person=None, ctx=None, allow_llm=True, max_usd=None, client=None):
    return decide_many(name, [inp], person=person, ctx=ctx, allow_llm=allow_llm, max_usd=max_usd, client=client)[0]


def decide_many(name, items, *, person=None, ctx=None, allow_llm=True, max_usd=None, client=None):
    spec = DECISIONS[name]
    limit = spec["max_usd"]
    if max_usd is not None:
        check(float, max_usd, "max_usd")
        if max_usd < 0:
            raise ValueError("max_usd")
        limit = min(limit, max_usd)
    if client is not None and client != "loopback" and not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", client):
        raise ValueError("client")
    start = time.monotonic()
    pending, owned, followers, results, inputs, keys, hashes = [], {}, [], [None] * len(items), [], [], []
    # Validate the entire batch before registering flights (bad input cannot strand a waiter).
    for inp in items:
        check(spec["inp"], inp)
        inp = spec["norm"](inp)
        check(spec["inp"], inp)
        digest = hashlib.sha256(json.dumps(inp, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                           allow_nan=False).encode()).hexdigest()[:32]
        inputs.append(inp)
        hashes.append(digest)
        keys.append(f"{name}|{spec['ver']}|{digest}")

    def result(i, pair, source, why=None, cost=None, over=False):
        answer, conf = _decision_answer(spec, pair)
        results[i] = {"type": name, "ver": spec["ver"], "answer": answer, "confidence": conf,
                      "source": source, "why": why, "receipt": "d_" + secrets.token_hex(6),
                      "cost_usd": cost, "ms": round((time.monotonic() - start) * 1000, 3)}
        if source == "llm":
            _decision_put(keys[i], spec, answer, conf)
        rec = {"t": time.time(), "id": results[i]["receipt"], "type": name, "ver": spec["ver"],
               "in": hashes[i], "src": source, "why": why, "conf": conf, "cost": cost, "ms": results[i]["ms"],
               "answer": {k: answer[k] for k, s in spec["out"].items() if k in answer and not _decision_free(s)}}
        if client is not None:
            rec["client"] = client
        if over:
            rec["over"] = True
        with _usage_lock:
            _decision_roll()
            counts = _decide_counts.setdefault(name, {"sources": {}, "cost": 0.0})
            mix = counts["sources"]
            mix[source] = mix.get(source, 0) + 1
            counts["cost"] += cost or 0
        _decide_queue.put((DATA, rec, source == "llm" and spec["ttl"] >= DAY and not _decision_free(spec["out"])))

    def fallback(i, err, cost=None, over=False):
        result(i, spec["fallback"](inputs[i], err), "fallback", err if isinstance(err, str) else type(err).__name__, cost, over)

    try:
        for i, inp in enumerate(inputs):
            pair = spec["rule"](inp, spec["params"]) if spec["rule"] else None
            if pair is not None:
                result(i, pair, "rule")
                continue
            with _decide_lock:
                hit = _decision_hit(keys[i])
            if hit:
                result(i, (hit["answer"], hit["confidence"]), "cache")
                continue
            gate = _decision_gate(name, spec, allow_llm, limit, client)
            if gate:
                fallback(i, gate)
                continue
            with _decide_lock:
                hit = _decision_hit(keys[i])
                event = _decide_flights.get(keys[i])
                if not hit and event is None:
                    event = _decide_flights[keys[i]] = threading.Event()
                    pending.append(i)
                    owned[keys[i]] = event
                elif not hit:
                    followers.append((i, event))
            if hit:
                result(i, (hit["answer"], hit["confidence"]), "cache")
        llm = spec["llm"]
        for offset in range(0, len(pending), llm["batch"] if llm else 1):
            chunk = pending[offset:offset + llm["batch"]]
            cost, over = None, False
            try:
                batch = [inputs[i] for i in chunk]
                body = llm["prompt"](batch)
                reservation = {}
                gate = _decision_gate(name, spec, allow_llm, limit, client, reservation)
                if gate:
                    for i in chunk:
                        fallback(i, gate)
                    continue
                try:
                    resp = openrouter(llm["path"], body, llm["timeout"], spec["lane"], name)
                except BudgetExceeded:
                    if client is not None:
                        with _usage_lock:
                            if _usage["day"] == reservation["day"]:
                                _usage["calls"][f"api:{spec['lane']}"] -= 1
                    raise
                reported = (resp.get("usage") or {}).get("cost")
                if reported is not None:
                    check(float, reported, "cost")
                    if reported < 0:
                        raise ValueError("cost")
                    over = reported > limit
                    cost = reported / len(chunk)
                    if over:
                        with _usage_lock:
                            _decision_roll()
                            _decide_over.add(name)
                parsed = llm["parse"](resp, batch)
                if not isinstance(parsed, list) or len(parsed) != len(chunk):
                    raise ValueError("answers")
                for i, pair in zip(chunk, parsed):
                    try:
                        pair = _decision_answer(spec, pair)
                    except Exception as err:
                        fallback(i, err, cost, over)
                    else:
                        result(i, pair, "llm", cost=cost, over=over)
            except Exception as err:
                for i in chunk:
                    if results[i] is None:
                        fallback(i, err, cost, over)
            finally:
                with _decide_lock:
                    for i in chunk:
                        event = owned.pop(keys[i])
                        if _decide_flights.get(keys[i]) is event:
                            del _decide_flights[keys[i]]
                        event.set()
        for i, event in followers:
            event.wait(llm["timeout"] + 1)
            with _decide_lock:
                hit = _decision_hit(keys[i])
            if hit:
                result(i, (hit["answer"], hit["confidence"]), "cache")
            else:
                fallback(i, "singleflight")
        return results
    finally:
        with _decide_lock:
            for key, event in owned.items():
                if _decide_flights.get(key) is event:
                    del _decide_flights[key]
                event.set()


def decide_stats():
    with _usage_lock:
        _decision_roll()
        out = {}
        for name, spec in DECISIONS.items():
            counts = _decide_counts.get(name, {"sources": {}, "cost": 0.0})
            used = _usage["calls"].get(f"{spec['lane']}:{name}", 0)
            lane_used = sum(n for k, n in _usage["calls"].items() if k.startswith(f"{spec['lane']}:"))
            cap = spec["cap"] if spec["cap"] is not None else AI_CAPS.get(spec["lane"], 0)
            left = 0 if name in _decide_over else max(0, cap - (used if spec["cap"] is not None else lane_used))
            out[name] = {"sources": dict(counts["sources"]), "llm_calls": used, "cost": counts["cost"],
                         "cap": left, "over": name in _decide_over}
        return out


def _decision_json_schema(schema):
    if isinstance(schema, dict):
        return {k: _decision_json_schema(s) for k, s in schema.items()}
    if isinstance(schema, (tuple, list)):
        return [_decision_json_schema(s) for s in schema]
    return schema.__name__ if isinstance(schema, type) else schema


def decide_index():
    counts = decide_stats()
    return [{"type": name, "ver": s["ver"], "lane": s["lane"], "cap": s["cap"],
             "used_today": counts[name]["llm_calls"], "serving": s["serving"],
             "available": all(bool(globals().get(x)) for x in s["requires"]),
             "inp": _decision_json_schema(s["inp"]), "out": _decision_json_schema(s["out"])}
            for name, s in DECISIONS.items() if s["api"]]


def route_norm(inp):
    query = " ".join(inp["query"].split())
    if not query:
        raise ValueError("query")
    return {"query": query[:200]}


def route_parse(resp, batch):
    a = resp["answers"]["route"]
    return [({"choice": a["choice"], "confidence": a.get("confidence")}, a.get("confidence") or 0)]


decision("route_query", ver=1, lane="search", cap=None, ttl=30 * DAY,
    inp={"query": str}, out={"choice": ("title", "discover", "request", None), "confidence": (float, None)},
    norm=route_norm, rule=None, params={},
    llm={"path": "/api/alpha/decisions", "model": ROUTE_MODEL, "batch": 1, "timeout": 4,
         "prompt": lambda b: {"model": ROUTE_MODEL, "state": b[0]["query"], "questions": ROUTE_Q}, "parse": route_parse},
    fallback=lambda inp, err: ({"choice": None, "confidence": None}, 0), requires=(), serving=False, min_acc=.95)


def resolve_normal(title):
    title = unicodedata.normalize("NFKD", title[:120].casefold())
    title = "".join(c for c in title if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", title))


def resolve_score(candidate, choice):
    aliases = [choice.get(k) for k in ("title", "name", "original_title", "original_name")]
    aliases += choice.get("aliases") or []
    alt = choice.get("alternative_titles") or {}
    aliases += [x.get("title") for x in alt.get("titles", alt.get("results", []))]
    similarity = max((difflib.SequenceMatcher(None, resolve_normal(candidate["title"]), resolve_normal(t)).ratio()
                      for t in aliases if t), default=0)
    year = str(choice.get("year") or choice.get("release_date") or choice.get("first_air_date") or "")[:4]
    year_fit = .5
    if candidate.get("year") and year:
        delta = abs(int(candidate["year"]) - int(year))
        year_fit = 1 if delta == 0 else .8 if delta == 1 else 0
    kind = choice.get("kind") or choice.get("media_type") or ("tv" if choice.get("name") else "movie")
    kind_fit = 1 if candidate.get("kind") == kind else .5 if not candidate.get("kind") else 0
    popularity = min(1, math.log1p(max(0, float(choice.get("popularity", choice.get("pop")) or 0))) / 8)
    return round(.72 * similarity + .18 * year_fit + .08 * kind_fit + .02 * popularity, 6)


def resolve_rule(inp, params):
    ranked = sorted(((resolve_score(inp, c), c) for c in inp["choices"]), key=lambda x: -x[0])
    if ranked and ranked[0][0] >= params["tau"] and ranked[0][0] - (ranked[1][0] if len(ranked) > 1 else 0) + 1e-9 >= params["margin"]:
        conf, choice = ranked[0]
        return ({"pick": {k: choice[k] for k in TITLE}}, conf)
    return None


def resolve_prompt(batch):
    questions = {}
    for i, inp in enumerate(batch):
        choices = {f"c{j}": f"{c['title']} ({c.get('year')}, {c['kind']})" for j, c in enumerate(inp["choices"])}
        choices["none"] = "None of these titles matches"
        questions[str(i)] = {"type": "choice", "instructions": "Resolve the exported title to exactly one choice, or none.",
                             "criteria": choices}
    return {"model": ROUTE_MODEL, "state": json.dumps([{k: x.get(k) for k in ("title", "year", "kind")} for x in batch]),
            "questions": questions, "usage": {"include": True}}


def resolve_parse(resp, batch):
    out = []
    for i, inp in enumerate(batch):
        a = resp["answers"].get(str(i)) or {}
        c = next((c for j, c in enumerate(inp["choices"]) if a.get("choice") == f"c{j}"), None)
        conf = a.get("confidence")
        out.append(({"pick": {k: c[k] for k in TITLE} if c else None}, 0 if conf is None else conf))
    return out


decision("resolve_title", ver=1, lane="resolve", cap=20, ttl=180 * DAY, max_usd=.02,
    inp={"title": str, "year": (int, None), "kind": (*KIND, None),
         "choices": [{**TITLE, "title": str, "aliases": [str, 5], "year": (int, None), "pop": (float, None)}, 8]},
    out={"pick": (TITLE, None)}, norm=lambda inp: inp, rule=resolve_rule, params={"tau": .88, "margin": .08},
    llm={"path": "/api/alpha/decisions", "model": ROUTE_MODEL, "batch": 25, "timeout": 20,
         "prompt": resolve_prompt, "parse": resolve_parse},
    fallback=lambda inp, err: ({"pick": None}, 0), requires=("OR_KEY",), serving=False, min_acc=.95)


def search_intent(query):
    """Consensus lists use canon; moods and similarities use a light household hint."""
    q = re.sub(r"[-_]+", " ", query.casefold())
    return "canon" if re.search(r"\b(best(?:e[nmrs]?)?|bset|bets|greatest|graetest|greatset|top|all\s+times?|"
                                r"classics?|clasics?|masterpieces?|masterpeices?|acclaimed|must\s+see|"
                                r"oscars?|award\s+winning|highest\s+rated|aller\s+zeiten|klassiker|meisterwerke?|"
                                r"najbolj\w*|svih\s+vremena|klasici|klasik\w*)\b", q) else "taste"


def ai_plan(query):
    """None for title searches, else the LLM's picks as [(title, year, 'movie'|'series', why)]."""
    m = _model["m"]
    hint = taste_hint(m) if m and search_intent(query) == "taste" else ""
    taste = f"\n(Household taste, only to break ties between equally good matches: {hint})" if hint else ""
    picks = _pool.submit(llm_picks, PICK_SYS, query + taste, 700, 6, "search", "picks")  # in parallel with the router
    try:
        a = decide("route_query", route_norm({"query": query}))["answer"]
    except ValueError:
        picks.cancel()
        return None
    if a["choice"] is None or (a["choice"] == "title" and (a["confidence"] or 0) >= .5):
        picks.cancel()
        return None
    return picks.result()


def taste_rerank(metas):
    """Search picks: the LLM's order blended with the household's match, plus a match note; never blocks on fetches."""
    m = _model["m"]
    if not m or not m.get("cal") or len(metas) < 2:
        return metas
    missing = [(x["id"], "movie" if x["type"] == "movie" else "tv") for x in metas if not frec(x["id"], "movie" if x["type"] == "movie" else "tv")]
    if missing:
        _outer.submit(ensure_facets, missing)  # known next time
    scored = []
    for i, x in enumerate(metas):
        pct = match_pct(m, score_key(m, x["id"], kind="movie" if x["type"] == "movie" else "tv")[0])
        taste = (pct - 70) / 30 if pct is not None else 0.0
        scored.append((-(1 - i / len(metas)) * 0.8 - 0.2 * taste, i, with_match(x, why=False) if pct else x))
    return [x for *_, x in sorted(scored)]


def parse_picks(text):
    picks = []
    for line in text.splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) in (3, 4) and parts[0] and not parts[0].startswith("#"):
            year = parts[1][:4] if parts[1][:4].isdigit() else None
            why = parts[3] if len(parts) == 4 else ""
            picks.append((parts[0], year, "series" if parts[2].lower().startswith("s") else "movie", why))
    return picks


def llm_text(system, prompt, max_tokens, timeout, lane, label, effort="none"):
    j = openrouter("/api/v1/chat/completions", {
        "model": PICK_MODEL, "max_tokens": max_tokens, "reasoning": {"effort": effort}, "usage": {"include": True},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}, timeout, lane, label)
    return j["choices"][0]["message"].get("content") or ""


def llm_picks(system, prompt, max_tokens, timeout, lane, label):
    return parse_picks(llm_text(system, prompt, max_tokens, timeout, lane, label))


def llm_sections(system, prompt, max_tokens, timeout, label, names, effort="none"):
    """One call, several rows: a header line per section name, followed by its pick lines."""
    out, cur = {n: [] for n in names}, None
    for line in llm_text(system, prompt, max_tokens, timeout, "feed", label, effort).splitlines():
        head = re.sub(r"[#*:_\s]+", " ", line).strip().upper()
        if head in out:
            cur = head
        elif cur:
            out[cur] += parse_picks(line)
    return out


def ai_future(query):
    """One shared AI call per query: the movie and series catalogs are requested together."""
    key = query.casefold()
    with _ai_lock:
        hit = _ai.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if len(_ai) >= CACHE_MAX:
            _ai.clear()
        fut = _outer.submit(ai_plan, query)  # ai_plan waits on _pool leaf calls
        _ai[key] = (time.time() + CACHE_TTL, fut)
        return fut


def genres(kind, lang):
    try:
        data = tmdb(f"genre/{kind}/list", language=lang)
        return {g["id"]: g["name"] for g in data.get("genres", [])}
    except Exception:
        return {}


def manifest(lang, scope=None):
    pin_shown(lang, scope)
    extra = [{"name": "search", "isRequired": True}]
    suffix = {"movie": " · Movies", "series": " · Series", "tv": ""}
    rows = []
    with addon_scope(scope):
        catalogs = feed_rows(lang)
        for cid, typs, name in catalogs:
            if cid in MIXED and "tv" in typs:  # one mixed row; movie/series copies only duplicate it in Odin
                typs = ["tv"]
            for typ in typs:
                if (feed(cid, lang) or (cid, lang) in _feeds_empty) and cid != rid('top10'):
                    # Count the cached composition of stored rows, with the catalog's viewer and pins.
                    metas = row_metas(cid, typ, lang, who=SCOPES[scope]) if scope is not None else row_metas(cid, typ, lang)
                    if len(metas) < (1 if cid == rid('episodes') else ROW_MIN):
                        continue
                if isinstance(name, dict):
                    rows.append({"type": typ, "id": cid, "name": name[typ]})
                else:
                    rows.append({"type": typ, "id": cid, "name": name + (suffix[typ] if len(typs) > 1 else "")})
    reserved = {row["name"] for row in rows}
    used = set()
    for row in rows:
        name = row["name"]
        if name in used:
            n = 2
            while f"{name} · {n}" in reserved:
                n += 1
            row["name"] = f"{name} · {n}"
            reserved.add(row["name"])
        used.add(row["name"])
    rows += [
        {"type": "movie", "id": rid('search'), "name": "Search", "extra": extra},
        {"type": "series", "id": rid('search'), "name": "Search", "extra": extra},
    ]
    return {
        "id": CONFIG["addon"]["id"] + (f".{scope}" if scope else ""),
        "version": CONFIG["addon"]["version"],
        "name": CONFIG["addon"]["name"] + (f" · {("Together" if scope == "together" else LABELS[scope])}" if scope else ""),
        "description": f"Fast TMDB title search and home rows ({lang})" + (f" · {("Together" if scope == "together" else LABELS[scope])}" if scope else ""),
        "resources": ["catalog"],
        "types": ["movie", "series", "tv"],
        "idPrefixes": ["tmdb:"],
        "catalogs": rows,
    }


def display_name(it, en):
    # No German translation -> TMDB returns the original script (e.g. Chinese); prefer English then.
    name = it.get("title") or it.get("name") or ""
    if not re.search(r"[A-Za-zÀ-ɏ]", name):
        name = en.get("title") or en.get("name") or name
    return name


def to_meta(it, stype, kind, names, en, mark="", note=""):
    date = it.get("release_date") if kind == "movie" else it.get("first_air_date")
    ids = it.get("genre_ids") or [g["id"] for g in it.get("genres") or []]
    overview = it.get("overview") or en.get("overview") or ""
    meta = {
        "id": f"tmdb:{it['id']}",
        "type": stype,
        "name": mark + display_name(it, en),
        "description": " · ".join(x for x in (note, overview) if x) or None,
        "releaseInfo": (date or "")[:4] or None,
        "genres": [names[g] for g in ids if g in names] or None,
    }
    poster = en.get("poster_path") or it.get("poster_path")
    backdrop = en.get("backdrop_path") or it.get("backdrop_path")
    if poster:
        meta["poster"] = IMG + "w500" + poster
    if backdrop:
        meta["background"] = IMG + "w1280" + backdrop
    if (it.get("vote_count") or 0) >= 50:
        meta["imdbRating"] = f"{it.get('vote_average', 0):.1f}"
    return {k: v for k, v in meta.items() if v is not None}


def search_both(kind, query, lang, **params):
    # Localised text, but artwork from the English search: localised searches fall
    # back to arbitrary-language posters (e.g. Chinese) when no local poster exists.
    jobs = {l: _pool.submit(tmdb, f"search/{kind}", query=query, language=l, include_adult="false", **params)
            for l in {lang, "en-US"}}
    data = jobs[lang].result().get("results", [])
    try:
        art = {it["id"]: it for it in jobs["en-US"].result().get("results", [])}
    except Exception:
        art = {}
    return data, art


def match_score(it, en, title):
    """How well a TMDB result matches an LLM pick's title (0..1), over local, English and original names."""
    want = re.sub(r"\W+", " ", title.casefold()).strip()
    names = {it.get(k) for k in ("title", "name", "original_title", "original_name")} | {en.get("title"), en.get("name")}
    return max((difflib.SequenceMatcher(None, want, re.sub(r"\W+", " ", n.casefold()).strip()).ratio()
                for n in names if n), default=0)


def resolve_pick(kind, title, year, lang):
    year_param = {("year" if kind == "movie" else "first_air_date_year"): year} if year else {}
    data, art = search_both(kind, title, lang, **year_param)
    if not data and year_param:
        data, art = search_both(kind, title, lang)
    # Fuzzy search returns *something* for any string; reject results that aren't the picked title.
    scored = [(match_score(it, art.get(it["id"], {}), title), i, it) for i, it in enumerate(data[:5])]
    scored = [x for x in scored if x[0] >= 0.75]
    if not scored:
        return None
    _, _, best = max(scored, key=lambda x: (round(x[0], 1), -x[1]))
    return best, art.get(best["id"], {})


def ai_search(stype, kind, picks, lang, names, mark=AI_MARK):
    jobs = [(p[3], _outer.submit(resolve_pick, kind, p[0], p[1], lang)) for p in picks if p[2] == stype]
    metas, seen = [], set()
    for why, job in jobs:
        try:
            hit = job.result()
        except Exception:
            continue
        if hit and hit[0]["id"] not in seen:
            seen.add(hit[0]["id"])
            metas.append(to_meta(hit[0], stype, kind, names, hit[1], mark, why))
    return metas


def looks_like_title(query, lang):
    """A TMDB title or person starting with the query means a title search: no AI call needed."""
    q = query.casefold()
    try:
        res = tmdb("search/multi", query=query, language=lang, include_adult="false").get("results", [])
    except Exception:
        return False
    return any((it.get(k) or "").casefold().startswith(q)
               for it in res[:10] for k in ("title", "name", "original_title", "original_name"))


def superseded(query, wait):
    """Odin searches on every keystroke; skip the AI for a query the user is still typing past."""
    start = time.time()
    time.sleep(wait)
    qc = query.casefold()
    return any(t >= start - 0.1 and q2.casefold() != qc and q2.casefold().startswith(qc) for t, q2 in list(_searches)[-20:])


def search(stype, query, lang, title_only=False):
    kind = "movie" if stype == "movie" else "tv"
    plan = None
    if not title_only and OR_KEY and len(query) >= AI_MIN_LEN and not looks_like_title(query, lang) \
            and not superseded(query, SEARCH_SETTLE):
        plan = ai_future(query)
    data, art = search_both(kind, query, lang)
    names = genres(kind, lang)
    q = query.casefold()

    def exact(it):
        return q in {(it.get(k) or "").casefold() for k in ("title", "name", "original_title", "original_name")}

    if plan is not None and not any(exact(it) for it in data):
        try:
            picks = plan.result(timeout=7)
        except Exception:
            picks = None  # router/LLM failed, slow or over budget: plain TMDB results below
        if picks:
            metas = ai_search(stype, kind, picks, lang, names)
            p = _profiles.get("household")
            seen = []
            if p:
                metas = [x for x in metas if x["id"] not in p["negative"]]
                seen = [x for x in metas if x["id"] in p["seen"]]
                metas = [x for x in metas if x["id"] not in p["seen"]]
            if search_intent(query) == "taste":
                metas = taste_rerank(metas)
            return {"metas": metas + seen}

    ranked = sorted(data, key=lambda it: (exact(it), it.get("popularity") or 0), reverse=True)
    return {"metas": [to_meta(it, stype, kind, names, art.get(it["id"], {})) for it in ranked]}


# ---------------------------------------------------------------------------
# Home rows. The worker keeps a taste profile per person (from history.json, written by the
# host's export_history.py cron, plus logged searches) and rebuilds each row job when its
# stamp changes. Rows persist in DATA/feeds.json so restarts don't re-spend calls.
#
#   job        rows                    source                         rebuild
#   personal   foryou + rewatchai      1 LLM call (taste-ranked pool)  taste change (>=1h apart) or 2x/day
#   archived   personal archive row    1 LLM call                      taste change (>=3h) or daily
#   because    because                 TMDB recommendations            seed change or daily
#   episodes   episodes                TMDB tv details                 every 3h or taste change
#   upcoming   upcoming                TMDB + AniList + taste model    4x/day or taste change
#   catchup    catchup                 TMDB tv details + collections   daily or taste/episodes change
#   lanes      lane1..3                taste facets + TMDB discover    daily or taste change (bandit picks themes)
#   rewatch    rewatch                 history + taste.json sagas      daily or taste change
#   top10      top10                   TMDB trending                   2x/day
#   airing     airing                  AniList + TMDB                  4x/day
#   nextseason nextseason              AniList + TMDB                  daily
# The taste model (fingerprints + facets, see below) orders every non-LLM row and writes the "% match" notes.

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WD_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ANIME_SEASONS = ["WINTER", "SPRING", "SUMMER", "FALL"]
SEASON_SUFFIX = re.compile(r"\s*[:\-]?\s*(season\s*\d+.*|\d+(st|nd|rd|th)\s+season.*|part\s*\d+|cour\s*\d+|final season.*|"
                           r"\b(ii|iii|iv)\b|\b\d+$)\s*$", re.I)
PERSONAL_SYS = ("You curate the personal rows of a household's streaming home screen (movies, TV series, anime). "
                "Reply with exactly these sections and nothing else:\n{sections}\n"
                "Line format: Title|Year|m or s|why   (m=movie, s=series). why = a short English reason, max 8 words, "
                "naming what of theirs it connects to, e.g. '{example}'. Real titles only (English or original title), "
                "best first, mix movies and series. Never suggest anything they gave up on or disliked, and nothing "
                "they already watched (except in REWATCH).")
GENRE_ALIASES = {"Sci-Fi": "Science Fiction", "Kids": "Family", "War & Politics": "War", "Soap": "Drama",
                 "Action & Adventure": "Action", "Sci-Fi & Fantasy": "Science Fiction"}
# "opened" = clicked, never really started: a hint, not taste.
SIGNAL_WEIGHT = {"loved": 3.0, "liked": 2.0, "watched": 2.0, "interest": 1.0, "opened": 0.2, "abandoned": -1.5, "disliked": -3.0}
LANES = [rid('lane1'), rid('lane2'), rid('lane3'), rid('lane4'), rid('lane5')]
LANE_BENCH = [rid('lanebench1'), rid('lanebench2')]
LANE_ROWS = LANES + LANE_BENCH
MOODS_ROWS = [rid('mood1'), rid('mood2'), rid('mood3'), rid('mood4')]
ROW_WINDOW = 12
ROW_MIN = 6
DOCS = [rid('doctrend'), rid('wonder'), rid('cosmos'), rid('history'), rid('meaning')]
STUDIO_ROWS = [rid('hbo'), rid('paramount')]
# Keep the player catalog order in sync (Top 10 has two catalogs).
DEDUPE_ORDER = ([rid('episodes'), rid('foryou'), rid('week'), rid('because'), rid('catchup'), rid('upcoming'),
                 rid('leap')] + list(ROW_OWNER) + [rid('rewatch'), rid('top10')] + STUDIO_ROWS + MOODS_ROWS + DOCS + LANES
                + [rid('airing'), rid('nextseason')])
GUEST_ROW_POLICY = {
    rid('episodes'): False,  # Resume household plays.
    rid('foryou'): False,  # Household taste shortlist.
    rid('week'): False,  # Joint member scores.
    rid('because'): False,  # Household seed and shelf name.
    rid('catchup'): False,  # Household seen mask and taste.
    rid('upcoming'): False,  # Household taste and history.
    rid('leap'): False,  # Member taste rotation.
    rid('rewatch'): False,  # Household favourites.
    rid('top10'): True,  # Raw public trending order; annotations removed for guests.
    rid('hbo'): False,  # Household-ranked studio shortlist.
    rid('paramount'): False,  # Household-ranked studio shortlist.
    rid('mood1'): False,  # Household taste and seeds.
    rid('mood2'): False,  # Household taste and seeds.
    rid('mood3'): False,  # Household taste and seeds.
    rid('mood4'): False,  # Household taste and seeds.
    rid('doctrend'): False,  # Household-ranked documentaries.
    rid('wonder'): False,  # Household-informed documentary shortlist.
    rid('cosmos'): False,  # Household-informed documentary shortlist.
    rid('history'): False,  # Household-informed documentary shortlist.
    rid('meaning'): False,  # Household-informed documentary shortlist.
    rid('lane1'): False,  # Household affinity and seeds.
    rid('lane2'): False,  # Household affinity and seeds.
    rid('lane3'): False,  # Household affinity and seeds.
    rid('lane4'): False,  # Household affinity and seeds.
    rid('lane5'): False,  # Household affinity and seeds.
    rid('airing'): False,  # Household history masks.
    rid('nextseason'): False,  # Household history masks.
}
GUEST_ROW_POLICY.update(dict.fromkeys(ROW_OWNER, False))
GUEST_ROWS = {cid for cid, allowed in GUEST_ROW_POLICY.items() if allowed}
MIXED = ({rid('foryou'), rid('upcoming'), rid('catchup'), rid('rewatch'), rid('because'), rid('week'),
          rid('leap')} | set(ROW_OWNER) | {rid(p) for p in ARCHIVED} | set(LANES) | set(MOODS_ROWS) | set(DOCS) | set(STUDIO_ROWS))
MIX = ("tv", "movie", "series")  # tv = both kinds in one row; movie / series = one kind only
STAMP_V = "17.3"  # bump to rebuild every row once after a format change
ROW_MAX = 40
DAY = 86400
_feeds = {}
_feeds_empty = set()  # Checked without taste signal; forgotten on restart.
_feeds_lock = threading.Lock()
_langs = {LANG}
_searches = []
_profiles = {}
_idmap = {}  # imdb id -> tmdb id
_trakt = {}
TRAKT_GENRES = {"science-fiction": "Science Fiction", "anime": "Anime", "superhero": "Superhero"}


def local_now(ts=None):
    """Europe/Berlin wall clock without tzdata (alpine): CEST between the last Sundays of March and October."""
    now = datetime.datetime.fromtimestamp(time.time() if ts is None else ts, datetime.timezone.utc)

    if TZ != "Europe/Berlin":
        return now.astimezone(datetime.timezone.utc if TZ == "UTC" else ZoneInfo(TZ)).replace(tzinfo=None)

    def last_sunday(month):
        d = datetime.datetime(now.year, month + 1, 1, 1, tzinfo=datetime.timezone.utc) - datetime.timedelta(days=1)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)

    summer = last_sunday(3) <= now < last_sunday(10)
    return (now + datetime.timedelta(hours=2 if summer else 1)).replace(tzinfo=None)


def anime_season(d, offset=0):
    i = (d.month - 1) // 3 + offset
    return ANIME_SEASONS[i % 4], d.year + i // 4


def feed_rows(lang=LANG, profiles=True):
    """Home rows in display order: (catalog id, types, name or {type: name})."""
    nxt, nyear = anime_season(local_now(), 1)

    def built(cid, fallback):
        names = feed(cid, lang).get("names") or {}
        title = names.get("title") or names.get("mixed") or fallback
        return title

    rows = [
        (rid('episodes'), ("series",), "Continue Watching & New Episodes"),
        (rid('foryou'), MIX, "✨ For You"),
        (rid('week'), MIX, "✨ This Week for " + " & ".join(LABELS[p] for p in PEOPLE)),
        (rid('upcoming'), MIX, "Coming Soon for You"),
        (rid('catchup'), MIX, "Catch Up"),
        (rid('rewatch'), MIX, "Watch Again"),
        (rid('because'), MIX, built(rid('because'), "More Like Your Favourites")),
        (rid('leap'), MIX, built(rid('leap'), "Same Feeling, New World")),
        (rid('top10'), ("movie", "series"), {"movie": "Top 10 Movies This Week", "series": "Top 10 Series This Week"}),
    ] + [(cid, MIX, "For " + LABELS[p]) for cid, p in ROW_OWNER.items()] + [(cid, MIX, built(cid, "Tonight's Mood")) for cid in MOODS_ROWS] \
      + [(cid, MIX, built(cid, "Picked for You")) for cid in LANES] \
      + [(cid, MIX, DOC_ROWS.get(cid, DOC_TREND)[0]) for cid in DOCS] + [(cid, MIX, STUDIOS[cid][0]) for cid in STUDIO_ROWS] + [
        (rid('airing'), ("series",), "Anime · New This Week"),
        (rid('nextseason'), ("series",), f"Anime · Next Season ({nxt.title()} {nyear})"),
    ] + [(rid(p), MIX, "✨ For " + label) for p, label in ARCHIVED.items()
         if p in (active_profiles() if profiles else ())]
    rank = {cid: i for i, cid in enumerate(DEDUPE_ORDER)}
    return sorted(rows, key=lambda r: rank.get(r[0], len(rank)))


def read_history():
    return _drop_read("history")


def active_profiles():
    return [n for n in ("household", *ARCHIVED) if not (read_taste().get(n) or {}).get("archived")]


def read_taste():
    path = os.path.join(DATA, "taste.json")
    try:
        with open(path) as fh:
            return {**DEFAULT_TASTE, **json.load(fh)}
    except FileNotFoundError:
        try:
            with open(path, "w") as fh:
                json.dump(DEFAULT_TASTE, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass
    except Exception as e:
        print(f"taste.json unreadable, using defaults: {e}", flush=True)
    return DEFAULT_TASTE


def tmdb_details(item):
    """(kind, en-US details) for a history item id (tmdb:123 or an imdb tt id)."""
    kind = "movie" if item["type"] == "movie" else "tv"
    raw = item["id"]
    if raw.startswith("tmdb:"):
        tid = raw[5:]
    else:
        tid = _idmap.get(raw)
        if tid is None:
            found = tmdb(f"find/{raw}", external_source="imdb_id", language="en-US")
            res = found.get("movie_results" if kind == "movie" else "tv_results") or []
            if not res:
                return None
            tid = _idmap[raw] = str(res[0]["id"])
    return kind, tmdb(f"{kind}/{tid}", language="en-US")


def classify(item, now):
    age = (now - (item.get("at") or 0) / 1000) / DAY if item.get("at") else 999
    rating, likes = item.get("rating"), item.get("likes")
    if item.get("dropped") or (rating is not None and rating <= 4):
        return "disliked", age
    if item.get("favorite") or (rating is not None and rating >= 7) or (likes or 0) > 0:
        return "loved", age
    prog = item.get("progress") or 0
    if item["type"] == "movie":
        if item.get("played") or prog >= 0.7:
            return "watched", age
        if 0.03 <= prog < 0.3 and age > 2:
            return "abandoned", age
        return "interest" if prog >= 0.03 else "opened", age
    eps = item.get("episodes_played") or 0
    if eps >= 2:
        return "watched", age
    if (eps == 1 or 0.03 <= prog < 0.5) and age > 14:
        return "abandoned", age
    return "interest" if eps or prog >= 0.03 else "opened", age


def real_play(e):
    """Actually watched (not just opened): what may credit a row or count as a play."""
    it = e["item"]
    rated = e.get("wsrc") == "trakt" and it.get("rating") is not None
    rated = rated or any(opinion.get("wsrc") == "trakt" and (opinion.get("item") or {}).get("rating") is not None
                         for opinion in (e.get("opinions") or {}).values())
    if e.get("who") == "remote" or "remote" in e.get("flags", ()) or rated or it.get("favorite"):  # An opinion is not a play.
        return bool(it.get("played") or (it.get("progress") or 0) >= 0.25 or (it.get("episodes_played") or 0) >= 1)
    return e["signal"] in ("loved", "liked", "watched") or (it.get("progress") or 0) >= 0.25 or (it.get("episodes_played") or 0) >= 1


def read_trakt():
    """Account history, attributed by configured file rather than remote item fields."""
    accounts = CONFIG["connectors"].get("trakt_api", {}).get("accounts")
    if accounts is not None:
        return [{**it, "person": person} for person in accounts for it in _drop_read("trakt-" + person)]
    return _drop_read("trakt")


# --- connectors: references, local drops and bounded live adapters ---
CONNECTOR_MIN = 3600
CONNECTOR_MAX = DAY
CONNECTOR_IDLE_AFTER = 30 * DAY
CONNECTOR_IDLE_MAX = 7 * DAY
CONNECTOR_DAILY = ("api", "player")
CONNECTOR_REMIND = 30 * DAY
CONNECTOR_DEMAND_GAP = 300
CONNECTOR_CATALOG_DEMAND = True
CONNECTOR_HISTORY_LAST_GOOD = False
CONNECTOR_SEPARATE_ROUTE = True
CONNECTOR_BACKUP = os.environ.get("SKALD_CONNECTOR_BACKUP", "1") != "0"
CONNECTOR_STALE = {"history": 2 * 3600, "trakt": 30 * DAY}
CONNECTOR_REMINDER_WARNING = True
CONNECTOR_REMINDER = "{src} export for {who} is {days} days old; export again, then `skald import <file>`"
_connectors_lock = threading.Lock()  # leaf; no IO or other locks while held
_connectors_busy = False  # one IO worker; claimed under the leaf lock, then released before IO
_connectors = {}
_connectors_path = None
_connectors_generation = 0
_connectors_derived = True
_connectors_saved = {}
_connector_import_stamp = None
_connector_stamps = {}
_drop_status = {}
_history_good = {}
_trakt_accounts = {}


def _drop_path(cid):
    if cid.startswith("trakt-") and cid[6:] in CONFIG["connectors"].get("trakt_api", {}).get("accounts", {}):
        return os.path.join(DATA, cid + ".history.json")
    return {"history": os.path.join(DATA, "history.json"), "trakt": os.path.join(DATA, "trakt.json")}[cid]


def _drop_read(cid, stamp=None):
    """The sole drop parser; history is deliberately uncached and Trakt keeps its last good list."""
    path = _drop_path(cid)
    trakt = _trakt if cid == "trakt" else _trakt_accounts.setdefault(path, {})
    try:
        if stamp is None:
            st = os.stat(path)
            stamp = (path, st.st_mtime_ns, st.st_size)
    except OSError as e:
        _drop_status[cid] = {"error": type(e).__name__}
        return json.loads(_history_good.get(path, "[]")) if cid == "history" and CONNECTOR_HISTORY_LAST_GOOD else []
    if cid != "history" and trakt.get("stamp") == stamp:
        _drop_status[cid] = {"error": None}
        return trakt["items"]
    try:
        with open(path) as fh:
            doc = json.load(fh)
        items = doc["items"] if cid == "history" else [it for it in doc.get("items", []) if it.get("id", "").startswith("tmdb:")]
        if cid != "history":
            trakt.update(stamp=stamp, items=items, exported=doc.get("exported_at") or doc.get("generated_at"))
        elif CONNECTOR_HISTORY_LAST_GOOD:
            _history_good[path] = json.dumps(items)
        _drop_status[cid] = {"error": None}
        return items
    except Exception as e:
        _drop_status[cid] = {"error": type(e).__name__}
        if cid != "history":
            print(f"{cid}.json unreadable: {type(e).__name__}", flush=True)
            return trakt.get("items") or []
        return json.loads(_history_good.get(path, "[]")) if CONNECTOR_HISTORY_LAST_GOOD else []


def _connector_time(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    if not value:
        return None
    try:
        date = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (date if date.tzinfo else date.replace(tzinfo=datetime.timezone.utc)).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _connector(cid, kind, person, now, cursor=None, last_new=None, events=0, auth=None, disabled=False):
    return {"id": cid, "kind": kind, "person": person, "auth": auth, "cursor": cursor,
            "last_sync": None, "last_new": last_new, "next_due": None if disabled or kind in ("manual_export", "availability") else now,
            "interval": CONNECTOR_MIN, "health": {"state": "disabled" if disabled else "ok", "error": None, "at": now},
            "counts": {"events": events, "new_last": 0, "polls": 0}}


def connector_seeds(now):
    per_person = "accounts" in CONFIG["connectors"].get("trakt_api", {})
    items, trakt = read_history(), [] if per_person else read_trakt()
    items = items if isinstance(items, list) else []
    cursor = max((it["at"] for it in items if isinstance(it, dict) and type(it.get("at")) in (int, float) and math.isfinite(it["at"])), default=0)
    exported = _trakt.get("exported") if not _drop_status.get("trakt", {}).get("error") else None
    out = {"history": _connector("history", "file_drop", "both", now, cursor, cursor / 1000 or None, len(items),
                                disabled=CONFIG["history"]["source"] != "file"),
           "trakt": _connector("trakt", "file_drop", TRAKT_PERSON, now, [exported, len(trakt)],
                               _connector_time(exported) or max(((it.get("at") or 0) / 1000 for it in trakt), default=0) or None, len(trakt),
                               disabled=per_person or CONFIG["history"]["source"] != "file" and not os.path.isfile(_drop_path("trakt")))}
    out.update(_connector_manual(read_imports(), now))
    for cid, kind, auth in (("trakt_api", "api", "env:TRAKT_TOKEN"), ("letterboxd_rss", "api", None),
                            ("player", "player", None), ("aiostreams_db", "player", None)):
        out[cid] = _connector(cid, kind, TRAKT_PERSON if cid == "trakt_api" else "both", now, auth=auth, disabled=True)
    out["nzbhydra"] = _connector("nzbhydra", "availability", "both", now, auth="file:hydra.json", disabled=hydra_cfg() is None)
    for cid, cfg in CONFIG["connectors"].items():
        enabled = cfg["enabled"] and (cid not in ("aiostreams_db", "jellyfin", "plex") or CONFIG["history"]["source"] == cid)
        if cid == "history":
            enabled &= CONFIG["history"]["source"] == "file"
        if cid == "nzbhydra":
            enabled &= hydra_cfg() is not None
        if cid == "trakt":
            enabled &= not per_person
        out[cid] = _connector(cid, cfg["kind"], TRAKT_PERSON if cid in ("trakt", "trakt_api") else "both", now,
                              auth=cfg.get("auth"), disabled=not enabled)
        if cid == "trakt_api" and "accounts" in cfg:
            out[cid]["person"] = "both"
            out[cid]["accounts"] = {p: _connector("trakt_api:" + p, "api", p, now, auth=auth, disabled=not enabled)
                                    for p, auth in cfg["accounts"].items()}
    return out


def _connector_manual(records, now):
    out = {}
    for e in records:
        cid = f"manual:{e['src']}:{e['who']}"
        c = out.setdefault(cid, _connector(cid, "manual_export", e["who"], now))
        c["counts"]["events"] += 1
        # shortcut: event dates approximate export dates, use a receipt timestamp when imports carry one.
        if c["cursor"] is None or e["t"] > c["cursor"]:
            c["cursor"], c["last_new"] = e["t"], _connector_time(e["t"])
    return out


def _connector_clean(cid, c, now):
    c = {**_connector(cid, c.get("kind"), c.get("person"), now), **c, "id": cid}
    c["health"], c["counts"] = dict(c["health"]), dict(c["counts"])
    if c["person"] not in (*PEOPLE, "both"):
        raise ValueError("connector person")
    if c["kind"] not in ("file_drop", "manual_export", "api", "player", "availability") and c["health"]["state"] != "disabled":
        c["health"] = {"state": "disabled", "error": None, "at": now}
    if c["kind"] in ("api", "player") and c["health"]["state"] != "disabled" and not CONFIG["connectors"].get(cid.split(":")[0], {}).get("enabled"):
        c["health"] = {"state": "disabled", "error": None, "at": now}
    if c["auth"] is not None and (not isinstance(c["auth"], str) or not _CONNECTOR_AUTH.fullmatch(c["auth"])):
        c["auth"] = None
        c["health"] = {"state": "error", "error": "AuthNotReference", "at": now}
        _daily_log(f"connector {cid}")
    if c["kind"] in ("manual_export", "availability") or c["health"]["state"] == "disabled":
        c["next_due"] = None
    if c["health"]["state"] not in ("ok", "stale", "error", "disabled") or type(c["interval"]) not in (int, float) or not math.isfinite(c["interval"]) or c["interval"] <= 0:
        raise ValueError("connector state")
    if any(type(c["counts"].get(k)) is not int or c["counts"][k] < 0 for k in ("events", "new_last", "polls")):
        raise ValueError("connector counts")
    for error in (c["health"].get("error"), c.get("last_error")):
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error)):
            raise ValueError("connector error class")
    for field in ("last_sync", "last_new", "next_due"):
        if c[field] is not None and (type(c[field]) not in (int, float) or not math.isfinite(c[field])):
            raise ValueError("connector time")
    if "accounts" in c:
        c["accounts"] = {p: _connector_clean("trakt_api:" + p, account, now) for p, account in c["accounts"].items()
                         if p in CONFIG["connectors"].get("trakt_api", {}).get("accounts", {})}
    return c


def _connectors_save(rows, generation):
    """Serialized writes, outside the leaf lock; discard a snapshot superseded during IO."""
    rows = {cid: _connector_clean(cid, c, time.time()) for cid, c in rows.items()}
    fd, tmp = tempfile.mkstemp(prefix=".connectors-", dir=DATA)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"version": 1, "connectors": rows}, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        if generation != _connectors_generation:
            return False
        os.replace(tmp, os.path.join(DATA, "connectors.json"))
        _connectors_saved.clear()
        _connectors_saved.update({cid: _connector_material(c) for cid, c in rows.items()})
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def connectors_load(now=None):
    global _connectors_path, _connectors_generation, _connectors_derived, _connector_import_stamp, _connectors_busy
    now = time.time() if now is None else now
    with _connectors_lock:
        if _connectors_busy:
            return
        _connectors_busy = True
    try:
        rows, original = {}, None
        try:
            with open(os.path.join(DATA, "connectors.json")) as fh:
                doc = json.load(fh, parse_constant=int)
            if type(doc.get("version")) is not int or doc["version"] != 1 or not isinstance(doc.get("connectors"), dict):
                raise ValueError("connector version")
            original = doc["connectors"]
            rows = {cid: _connector_clean(cid, c, now) for cid, c in original.items()}
        except Exception as e:
            rows, original = {}, None
            print(f"connectors load: {type(e).__name__}", flush=True)
        seeds = connector_seeds(now)
        for cid, c in seeds.items():
            rows.setdefault(cid, c)
            if cid in CONFIG["connectors"] or cid in ("history", "trakt", "trakt_api", "letterboxd_rss", "player", "aiostreams_db", "nzbhydra"):
                old = rows[cid]
                old.update(kind=c["kind"], person=c["person"], auth=c["auth"])
                if "accounts" in c:
                    configured_accounts = c["accounts"]
                    previous = old.get("accounts", {})
                    old["accounts"] = {}
                    for p, account in configured_accounts.items():
                        prior = previous.get(p, account)
                        prior.update(id=account["id"], person=p, kind="api", auth=account["auth"])
                        if account["health"]["state"] == "disabled" or prior["health"]["state"] == "disabled":
                            prior.update(health=account["health"], next_due=account["next_due"])
                        old["accounts"][p] = prior
                else:
                    old.pop("accounts", None)
                if c["health"]["state"] == "disabled" or cid == "nzbhydra":
                    if (old["health"]["state"], old["health"]["error"]) != (c["health"]["state"], c["health"]["error"]):
                        old["health"] = c["health"]
                    old["next_due"] = c["next_due"]
                elif old["health"]["state"] == "disabled" or (cid in CONFIG["connectors"] and old["health"]["error"] == "AuthNotReference"):
                    old.update(health=c["health"], next_due=now)
        with _connectors_lock:
            _connectors.clear()
            _connectors.update(rows)
            _connectors_path = DATA
            _connectors_generation += 1
            generation = _connectors_generation
        _connectors_derived = original is None
        _connector_stamps.clear()
        _connector_import_stamp = _connector_imports_stamp()
        _connectors_saved.clear()
        if rows != original:
            try:
                _connectors_save(rows, generation)
            except Exception as e:
                _daily_log(f"connectors save failed: {type(e).__name__}")
        else:
            _connectors_saved.update({cid: _connector_material(c) for cid, c in rows.items()})
        return rows
    finally:
        with _connectors_lock:
            _connectors_busy = False


def connector_next(c, new, now):
    if c["kind"] == "manual_export":
        return c["interval"], None
    idle = not new and now - (c.get("last_new") or 0) >= CONNECTOR_IDLE_AFTER
    ceiling = min(DAY, CONNECTOR_MAX) if c["kind"] in CONNECTOR_DAILY else CONNECTOR_IDLE_MAX if idle else CONNECTOR_MAX
    interval = max(CONNECTOR_MIN, c["interval"] / 2) if new else min(ceiling, max(CONNECTOR_MIN, c["interval"] * 2))
    if c["kind"] in CONNECTOR_DAILY:
        interval = min(DAY, interval)
    return interval, now + interval


def connector_reminder(c, now):
    if c["kind"] != "manual_export" or c["health"]["state"] == "disabled" or c["last_new"] is None or now - c["last_new"] < CONNECTOR_REMIND:
        return None
    return CONNECTOR_REMINDER.format(src=c["id"].removeprefix("manual:").rsplit(":", 1)[0], who=c["person"], days=int((now - c["last_new"]) / DAY))


def _connector_history(c, now):
    items = _drop_read("history", c.get("_stamp"))
    cursor = max((it.get("at") or 0 for it in items), default=0)
    # Use spine metadata already in memory; polling must never resolve a title over the network.
    entries = {it["id"]: e for p in list(_profiles.values()) for e in p.get("entries", [])
               for it in (e.get("item") or {}, (e.get("shared") or {}).get("item") or {}) if it.get("id")}
    events, ids, pending = [], {}, False
    for it in items[:40]:
        e = entries.get(it["id"])
        if e is None:
            pending |= (it.get("at") or 0) > (c["cursor"] or 0)
            continue  # shortcut: retry unknown TV metadata after the spine refreshes; use drop metadata when available.
        kind, signal = "movie" if it["type"] == "movie" else "tv", classify(it, now)[0]
        who, by, ctx = attribute(it)
        ev = _event(it["at"] / 1000 if it.get("at") else None, f"tv:{kind}:{it['id']}", e["key"], kind, e["title"], e["year"], who, by,
            EVENT_ACTION[signal], "tv", {"item": it, "genres": list(e["genres"]), "flags": [], "signal": signal, **ctx}, _ids=ids)
        if (it.get("at") or 0) > (c["cursor"] or 0):
            events.append(ev)
    return events, c["cursor"] if pending else max(c["cursor"] or 0, cursor), {**_drop_status["history"], "events": len(items)}


def _connector_trakt(c, now):
    items = _drop_read("trakt", c.get("_stamp"))
    cursor = [_trakt.get("exported"), len(items)]
    old_time = _connector_time((c["cursor"] or [None, 0])[0]) or 0
    events = [e for e in trakt_events(now, items) if (e["t"] or 0) > old_time]
    return events, cursor, {**_drop_status["trakt"], "events": len(items)}


# Live adapters share output files with the existing spine; credentials exist only during a poll.
# --- connector transport (also loaded alone by skald/trakt.py) ---
CONNECTOR_HTTP_BYTES = 20 * 1024 * 1024
CONNECTOR_HTTP_PAGES = 100
TRAKT_URL = "https://api.trakt.tv"


class SchemaMismatch(ValueError):
    pass


class PathMismatch(ValueError):
    pass


class UnsafeURL(ValueError):
    pass


class ByteCapExceeded(ValueError):
    pass


class PageCapExceeded(ValueError):
    pass


class CredentialsDirectoryUnsafe(AuthMissing):
    pass


class TraktLoginRequired(ValueError):
    pass


def _credentials_dir(create=False):
    path = os.path.join(DATA, "credentials")
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise CredentialsDirectoryUnsafe("Set DATA/credentials/ owner to the engine uid and chmod 0700.")
        if create:
            os.fchmod(fd, 0o700)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_auth_file(name, directory=None):
    close_directory = directory is None
    if close_directory:
        directory = _credentials_dir()
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    finally:
        if close_directory:
            os.close(directory)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise AuthMissing()
        return fd
    except Exception:
        os.close(fd)
        raise


def _resolve_auth(reference):
    kind, name = _auth_reference(reference, DATA)
    if kind == "env":
        value = os.environ.get(name, "")
    else:
        fd = _open_auth_file(name)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            value = fh.read(65537).strip()
    if not value or len(value) > 65536 or any(ord(c) < 32 for c in value):
        raise AuthMissing() from None
    return value


def _trakt_bundle(value):
    if not isinstance(value, dict):
        raise ValueError()
    for field in ("access_token", "refresh_token"):
        token = value.get(field)
        if not isinstance(token, str) or not token or any(ord(c) < 32 for c in token):
            raise ValueError()
    if type(value.get("created_at")) is not int or value["created_at"] < 0 or \
            type(value.get("expires_in")) is not int or value["expires_in"] <= 0:
        raise ValueError()
    bundle = {k: value[k] for k in ("access_token", "refresh_token", "created_at", "expires_in")}
    if len(json.dumps(bundle)) > 65536:
        raise ValueError()
    return bundle


def _write_trakt_auth(reference, bundle, directory=None):
    _, name = _auth_reference(reference, DATA)
    close_directory = directory is None
    if close_directory:
        directory = _credentials_dir()
    temp = ".trakt-auth-" + secrets.token_hex(16)
    try:
        try:
            os.close(_open_auth_file(name, directory))
        except FileNotFoundError:
            pass
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(_trakt_bundle(bundle), fh)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _auth_reference(reference, DATA)
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass
        if close_directory:
            os.close(directory)


def _read_trakt_auth(reference):
    kind, name = _auth_reference(reference, DATA)
    if kind == "env":
        return {"access_token": _resolve_auth(reference)}
    with os.fdopen(_open_auth_file(name), "r", encoding="utf-8") as fh:
        raw = fh.read(65537).strip()
    if not raw or len(raw) > 65536:
        raise AuthMissing()
    if raw.startswith("{"):
        return _trakt_bundle(json.loads(raw))
    if any(ord(c) < 32 for c in raw):
        raise AuthMissing()
    return {"access_token": raw}


def _renew_trakt_auth(reference, bundle, cfg, budget):
    opts = cfg.get("options", {})
    try:
        token = _http_get(TRAKT_URL + "/oauth/token", {"Content-Type": "application/json"}, body={
            "grant_type": "refresh_token", "refresh_token": bundle["refresh_token"],
            "client_id": _resolve_auth(opts.get("client_id", "env:TRAKT_CLIENT_ID")),
            "client_secret": _resolve_auth(opts.get("client_secret", "env:TRAKT_CLIENT_SECRET")),
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob"}, budget=budget)
        token = _trakt_bundle(token)
        _write_trakt_auth(reference, token)
    except Exception as error:
        failure = TraktLoginRequired()
        if isinstance(error, urllib.error.HTTPError):
            failure.code, failure.headers = error.code, error.headers
        raise failure from None
    return token


class _ConnectorNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UnsafeURL() from None


def _connector_open(req, timeout):
    return urllib.request.build_opener(_ConnectorNoRedirect()).open(req, timeout=timeout)


def _connector_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise UnsafeURL()
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(host)
            private = address.is_loopback
            if address.version == 4:
                private |= any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
            else:
                private |= address.is_link_local or address in ipaddress.ip_network("fc00::/7")
        except ValueError:
            private = host == "localhost" or bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", host))
            private |= bool(re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:local|home\.arpa|lan)", host))
        if private:
            return
    raise UnsafeURL()


def _http_get(url, headers=None, *, paged=False, body=None, budget=None):
    """Bound JSON transport; a shared budget caps an entire poll, including all endpoints."""
    _connector_url(url)
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    items, page, expected_pages, expected_total = [], 1, None, None
    while True:
        if budget["pages"] >= CONNECTOR_HTTP_PAGES:
            raise PageCapExceeded()
        budget["pages"] += 1
        target = url
        if paged:
            target += ("&" if "?" in url else "?") + urllib.parse.urlencode({"page": page, "limit": 100})
        request = urllib.request.Request(target, headers=headers or {},
            data=json.dumps(body).encode() if body is not None else None)
        with _connector_open(request, timeout=10) as response:
            remaining = CONNECTOR_HTTP_BYTES - budget["bytes"]
            raw = response.read(remaining + 1)
            budget["bytes"] += len(raw)
            if budget["bytes"] > CONNECTOR_HTTP_BYTES:
                raise ByteCapExceeded()
            value = json.loads(raw)
            if not paged:
                return value
            if not isinstance(value, list):
                raise ValueError()
            items.extend(value)
            raw_pages = response.headers.get("X-Pagination-Page-Count")
            raw_total = response.headers.get("X-Pagination-Item-Count")
            pages = int(raw_pages) if raw_pages is not None else (expected_pages if expected_pages is not None else 1)
            total = int(raw_total) if raw_total is not None else expected_total
            if pages < 0 or pages > CONNECTOR_HTTP_PAGES:
                raise PageCapExceeded()
            if (expected_pages is not None and pages != expected_pages or
                    expected_total is not None and total != expected_total or
                    total is not None and total < 0):
                raise ValueError()
            if raw_pages is not None:
                expected_pages = pages
            if raw_total is not None:
                expected_total = total
            if pages == 0 and (total not in (None, 0) or value) or pages > 1 and not value:
                raise ValueError()
            if expected_pages is not None and total is None and (len(value) > 100 or page < pages and len(value) != 100):
                raise ValueError()
            if expected_pages is not None and total is not None:
                if pages != (total+99)//100 and not (pages == 1 and total == 0):
                    raise ValueError()
                if len(value) != min(100, max(0, total - (page-1)*100)):
                    raise ValueError()
        if page >= pages:
            if expected_total is not None and len(items) != expected_total:
                raise ValueError()
            return items
        page += 1
# --- end connector transport ---

_connector_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="skald-connector")
_connector_pending = None


def _connector_write(name, doc, *, indent=None, newline=False):
    content = (json.dumps(doc, ensure_ascii=indent is None, indent=indent, allow_nan=False) + ("\n" if newline else "")).encode("utf-8")
    path = os.path.join(DATA, name + ".json")
    try:
        with open(path, "rb") as fh:
            if fh.read() == content:
                return False
    except FileNotFoundError:
        pass
    fd, temp = tempfile.mkstemp(prefix="." + name + "-", dir=DATA)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o644)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def _aiostreams_db(options):
    configured = CONFIG["connectors"]["aiostreams_db"]["options"]
    if options != configured or os.path.islink(options["path"]):
        raise PathMismatch()
    profiles = {p: name for p, name in options["profiles"].items() if p not in options.get("archived", {})}
    required = {"uuid", "media_type", "base_id", "last_played_at", "played", "kind", "favorite", "duration_ms",
                "position_ms", "rating", "likes", "dropped", "season", "episode"}
    uri = "file:" + urllib.parse.quote(options["path"], safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
        if not required <= {row[1] for row in con.execute("pragma table_info(watch_state)")}:
            raise SchemaMismatch()
        where = " or ".join(["uuid like ?"] * len(profiles)) or "0"
        rows = con.execute(
            f"select lower(substr(uuid, 1, 8)), media_type, base_id, max(coalesce(last_played_at, 0)), max(played), "
            f"sum(case when kind = 'episode' and played = 1 then 1 else 0 end), max(favorite), "
            f"max(case when duration_ms > 0 then 1.0 * position_ms / duration_ms else 0 end), count(*), max(rating), "
            f"max(likes), max(dropped), "
            f"max(case when kind = 'episode' and played = 1 then season * 1000 + episode end), "
            f"max(case when kind = 'episode' and played = 0 and position_ms > 0 then season * 1000 + episode end) "
            f"from watch_state where ({where}) group by 1, 2, 3 order by 4 desc limit 120",
            tuple(p + "%" for p in profiles)).fetchall()
    items = []
    for prefix, typ, base, at, played, eps, fav, prog, n, rating, likes, dropped, last, resume in rows:
        item = {"profile": profiles[prefix], "type": typ, "id": base, "at": at or None, "played": bool(played),
                "favorite": bool(fav), "progress": round(prog or 0, 2), "entries": n, "rating": rating,
                "likes": likes, "dropped": bool(dropped)}
        if typ == "series":
            item["episodes_played"] = eps or 0
            if last:
                item["last_episode"] = [last // 1000, last % 1000]
            if resume and (not last or resume > last):
                item["resume_episode"] = [resume // 1000, resume % 1000]
        items.append(item)
    return {"items": items}


class Unsupported(ValueError):
    pass


def _player_history(records, profile, cid):
    items, dropped = {}, 0
    for typ, tid, at, played, favorite, progress, episode in records:
        if type(tid) not in (str, int) or not re.fullmatch(r"[0-9]+", str(tid)) or int(tid) <= 0:
            dropped += 1
            continue
        key = (typ, "tmdb:" + str(int(tid)))
        item = items.setdefault(key, {"profile": profile, "type": typ, "id": key[1], "at": None,
            "played": False, "favorite": False, "progress": 0, "entries": 0, "rating": None,
            "likes": None, "dropped": False})
        item["at"] = max(item["at"] or 0, at or 0) or None
        item["played"] |= played
        item["favorite"] |= favorite
        item["progress"] = max(item["progress"], round(progress, 2))
        item["entries"] += 1
        if typ == "series":
            item["episodes_played"] = item.get("episodes_played", 0) + int(played)
            if played:
                if episode != [0, 0]: item["last_episode"] = max(item.get("last_episode", [0, 0]), episode)
            elif progress > 0:
                item["resume_episode"] = max(item.get("resume_episode", [0, 0]), episode)
    for item in items.values():
        if "resume_episode" in item and item["resume_episode"] <= item.get("last_episode", [0, 0]):
            del item["resume_episode"]
    print(f"connector {cid} dropped_idless={dropped}", flush=True)
    return {"items": sorted(items.values(), key=lambda item: (-(item["at"] or 0), item["type"], item["id"]))[:120]}


def _jellyfin(options):
    if "users" in options:
        shared = {k: v for k, v in options.items() if k != "users"}
        return {"items": [item for uid, profile in options["users"].items()
            for item in _jellyfin({**shared, "user_id": uid, "profile": profile})["items"]]}
    base = options["url"].rstrip("/")
    headers = {"X-Emby-Token": _resolve_auth(CONFIG["connectors"]["jellyfin"]["auth"])}
    budget, rows = {"bytes": 0, "pages": 0}, {}
    path = "/Users/" + urllib.parse.quote(options["user_id"], safe="") + "/Items"
    for filtered in ("IsPlayed", "IsResumable", "IsFavorite"):
        query = {"Recursive": "true", "IncludeItemTypes": "Movie,Episode,Series" if filtered == "IsFavorite" else "Movie,Episode", "Fields": "ProviderIds",
                 "EnableUserData": "true", "Filters": filtered, "StartIndex": 0, "Limit": 100}
        if "section" in options:
            query["ParentId"] = options["section"]
        total = None
        while True:
            page = _http_get(base + path + "?" + urllib.parse.urlencode(query), headers, budget=budget)
            if not isinstance(page, dict) or "Items" not in page or "TotalRecordCount" not in page:
                raise Unsupported()
            count = page["TotalRecordCount"]
            if (not isinstance(page["Items"], list) or type(count) is not int or count < 0 or
                    total is not None and count != total or page.get("StartIndex", query["StartIndex"]) != query["StartIndex"] or
                    len(page["Items"]) != min(100, max(0, count - query["StartIndex"]))):
                raise ValueError()
            total = count
            rows.update((row["Id"], row) for row in page["Items"])
            query["StartIndex"] += len(page["Items"])
            if query["StartIndex"] >= page["TotalRecordCount"]:
                break
    records = []
    series = {row["Id"]: row for row in rows.values() if row["Type"] == "Series"}
    episode_series = {row["SeriesId"] for row in rows.values() if row["Type"] == "Episode"}
    for row in rows.values():
        if not isinstance(row.get("UserData"), dict) or not {"Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite"} <= row["UserData"].keys():
            raise Unsupported()
        data = row["UserData"]
        if any(type(data[field]) is not bool for field in ("Played", "IsFavorite")) or any(
                type(data[field]) is not int or data[field] < 0 for field in ("PlayCount", "PlaybackPositionTicks")):
            raise ValueError()
        at = _connector_time(data.get("LastPlayedDate"))
        if data.get("LastPlayedDate") is not None and (not isinstance(data["LastPlayedDate"], str) or at is None or at < 0):
            raise ValueError()
        duration = row.get("RunTimeTicks") or 0
        if type(duration) is not int or duration < 0:
            raise ValueError()
        episode = row["Type"] == "Episode"
        if row["Type"] not in ("Movie", "Episode", "Series") or episode and any(
                type(row.get(field)) is not int or row[field] < 0 for field in ("ParentIndexNumber", "IndexNumber")):
            raise ValueError()
        if row["Type"] == "Series" and row["Id"] in episode_series:
            continue
        title = row
        if episode:
            sid = row["SeriesId"]
            if sid not in series:
                series[sid] = _http_get(base + path + "/" + urllib.parse.quote(sid, safe="") + "?Fields=ProviderIds&EnableUserData=true",
                                        headers, budget=budget)
            title = series[sid]
        records.append(("movie" if row["Type"] == "Movie" else "series", title.get("ProviderIds", {}).get("Tmdb"),
            int(at * 1000) if at else None, row["Type"] != "Series" and (data["Played"] or data.get("PlayCount", 0) > 0),
            data["IsFavorite"] or title.get("UserData", {}).get("IsFavorite", False),
            data.get("PlaybackPositionTicks", 0) / duration if duration and row["Type"] != "Series" else 0,
            [row["ParentIndexNumber"], row["IndexNumber"]] if episode else None))
    return _player_history(records, options.get("profile", "household"), "jellyfin")


def _plex(options):
    base = options["url"].rstrip("/")
    headers = {"X-Plex-Token": _resolve_auth(CONFIG["connectors"]["plex"]["auth"]), "Accept": "application/json"}
    budget, records, seen = {"bytes": 0, "pages": 0}, [], set()
    def pages(path, field):
        rows, start, total = [], 0, None
        while True:
            query = {"X-Plex-Container-Start": start, "X-Plex-Container-Size": 100, "includeGuids": 1}
            page = _http_get(base + path + "?" + urllib.parse.urlencode(query), headers, budget=budget)["MediaContainer"]
            batch = page.get(field, [])
            count = page.get("totalSize", total if total is not None else page["size"])
            if (not isinstance(batch, list) or type(count) is not int or count < 0 or
                    total is not None and count != total or page.get("offset", start) != start or
                    type(page["size"]) is not int or page["size"] != len(batch) or
                    len(batch) != min(100, max(0, count - start))):
                raise ValueError()
            total = count
            rows.extend(batch)
            start += len(batch)
            if start >= total:
                return rows
    sections = pages("/library/sections", "Directory")
    if "section" in options and not any(section["key"] == options["section"] and section["type"] in ("movie", "show")
                                        for section in sections):
        raise ValueError()
    for section in sections:
        if section["type"] not in ("movie", "show") or "section" in options and section["key"] != options["section"]:
            continue
        path = "/library/sections/" + urllib.parse.quote(section["key"], safe="") + "/all"
        rows = pages(path, "Metadata")
        for title in rows:
            if title["type"] != section["type"]:
                raise ValueError()
            tid = next((match[1] for guid in title.get("Guid", [])
                        if (match := re.fullmatch(r"tmdb://([1-9][0-9]*)", guid["id"]))), None)
            episode = section["type"] == "show"
            if episode and not (title.get("viewedLeafCount", 0) > 0 or title.get("viewOffset", 0) > 0 or title.get("lastViewedAt", 0) > 0):
                continue
            leaves = pages("/library/metadata/" + urllib.parse.quote(title["ratingKey"], safe="") +
                "/allLeaves", "Metadata") if episode else [title]
            for row in leaves:
                if row["type"] != ("episode" if episode else "movie") or episode and any(
                        type(row.get(field)) is not int or row[field] < 0 for field in ("parentIndex", "index")):
                    raise ValueError()
                if any(row.get(field) is not None and (type(row[field]) is not int or row[field] < 0)
                       for field in ("viewCount", "viewOffset", "lastViewedAt", "duration")):
                    raise ValueError()
                key = ("series" if episode else "movie", row["ratingKey"])
                if key in seen:
                    continue
                seen.add(key)
                if not (row.get("viewCount") or row.get("viewOffset") or row.get("lastViewedAt")):
                    continue
                duration = row.get("duration") or 0
                records.append(("series" if episode else "movie", tid, (row.get("lastViewedAt") or 0) * 1000 or None,
                    row.get("viewCount", 0) > 0, False, row.get("viewOffset", 0) / duration if duration else 0,
                    [row["parentIndex"], row["index"]] if episode else None))
    return _player_history(records, options.get("profile", "household"), "plex")


HISTORY_ADAPTERS = {"aiostreams_db": _aiostreams_db, "jellyfin": _jellyfin, "plex": _plex}


def _connector_player(c, now):
    source = CONFIG["history"]["source"]
    doc = HISTORY_ADAPTERS[source](CONFIG["connectors"][c["id"]]["options"])
    _connector_write("history", doc)
    events, cursor, stats = _connector_history({**c, "cursor": (c["cursor"] or [0, 0])[0]}, now)
    return events, [cursor, len(doc["items"])], stats


def _trakt_export(history, movies, shows, ratings, now):
    """REST pages to the web-cache export consumed by the unchanged taste transform."""
    def metadata(row, series):
        ids = row.get("ids") or {}
        return {"tmdb": ids.get("tmdb"), "imdb": ids.get("imdb"), "title": row.get("title"), "year": row.get("year"),
                "genres": row.get("genres") or [], "lang": row.get("language"), "status": row.get("status"),
                **({"eps_total": row.get("aired_episodes"), "eps": []} if series else {"plays": []})}
    grouped = {"movie": {}, "show": {}}
    for kind, watched in (("movie", movies), ("show", shows)):
        for row in watched:
            title = row[kind]
            key = title["ids"]["tmdb"]
            item = grouped[kind].setdefault(key, metadata(title, kind == "show"))
            if kind == "show" and row.get("aired_episodes") is not None:
                item["eps_total"] = row["aired_episodes"]
    seen, plays = set(), Counter()
    for row in history:
        if row.get("id") is not None:
            if row["id"] in seen:
                raise ValueError()
            seen.add(row["id"])
        kind = "movie" if "movie" in row else "show"
        title = row[kind]
        item = grouped[kind].setdefault(title["ids"]["tmdb"], metadata(title, kind == "show"))
        at = _connector_time(row.get("watched_at"))
        if at is None:
            raise ValueError()
        minute = math.floor(at / 60 + 0.5)
        if kind == "movie":
            item["plays"].append(minute)
            plays[(kind, title["ids"]["tmdb"])] += 1
        else:
            episode = row["episode"]
            item["eps"].append([episode["season"], episode["number"], minute, episode.get("runtime") or 0])
            plays[(kind, title["ids"]["tmdb"], episode["season"], episode["number"])] += 1
    expected = {}
    for row in movies:
        key = ("movie", row["movie"]["ids"]["tmdb"])
        if key in expected:
            raise ValueError()
        expected[key] = row["plays"]
    watched_shows = set()
    for row in shows:
        tid = row["show"]["ids"]["tmdb"]
        if tid in watched_shows or not row["seasons"]:
            raise ValueError()
        watched_shows.add(tid)
        for season in row["seasons"]:
            if not season["episodes"]:
                raise ValueError()
            for episode in season["episodes"]:
                key = ("show", tid, season["number"], episode["number"])
                if key in expected:
                    raise ValueError()
                expected[key] = episode["plays"]
    if any(type(count) is not int or count < 1 for count in expected.values()) or expected != plays:
        raise ValueError()
    for row in ratings:
        kind = "movie" if "movie" in row else "show" if "show" in row else None
        if kind and not row.get("episode"):
            title = row[kind]
            grouped[kind].setdefault(title["ids"]["tmdb"], metadata(title, kind == "show"))
    return {"exported_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "movies": list(grouped["movie"].values()), "shows": list(grouped["show"].values())}


# Pure offline history transform; reference parity tests guard changes.
TRAKT_BASE_WEIGHT = {"loved": 3, "watched": 2, "interest": 1, "abandoned": -1.5}


def _trakt_validate_export(data):
    """Reject malformed history before replacing an existing output file."""
    if not isinstance(data, dict) or not isinstance(data.get("exported_at"), str):
        raise ValueError("export must be an object with an exported_at string")
    for kind in ("movies", "shows"):
        rows = data.get(kind)
        if not isinstance(rows, list):
            raise ValueError(f"{kind} must be an array")
        for index, row in enumerate(rows):
            where = f"{kind}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{where} must be an object")
            if type(row.get("tmdb")) is not int or row["tmdb"] <= 0:
                raise ValueError(f"{where}.tmdb must be a positive integer")
            if not isinstance(row.get("title"), str) or not row["title"].strip():
                raise ValueError(f"{where}.title must be a nonempty string")
            if row.get("year") is not None and type(row["year"]) is not int:
                raise ValueError(f"{where}.year must be an integer or null")
            for key in ("imdb", "lang", "status"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    raise ValueError(f"{where}.{key} must be a string or null")
            genres = row.get("genres")
            if genres is not None and (
                not isinstance(genres, list) or any(not isinstance(g, str) for g in genres)
            ):
                raise ValueError(f"{where}.genres must be an array of strings or null")
            key = "plays" if kind == "movies" else "eps"
            plays = row.get(key)
            if not isinstance(plays, list):
                raise ValueError(f"{where}.{key} must be an array")
            if kind == "movies":
                if any(type(minute) is not int for minute in plays):
                    raise ValueError(f"{where}.plays must contain integer epoch minutes")
            else:
                total = row.get("eps_total")
                if total is not None and (type(total) is not int or total < 0):
                    raise ValueError(f"{where}.eps_total must be a nonnegative integer or null")
                for episode in plays:
                    if (
                        not isinstance(episode, list) or len(episode) != 4
                        or any(type(value) is not int for value in episode[:3])
                        or episode[0] < 0 or episode[1] < 0
                    ):
                        raise ValueError(f"{where}.eps needs [season, number, minute, runtime] rows")


def _trakt_max_session_eps(minutes):
    largest = current = 0
    previous = None
    for minute in sorted(minutes):
        current = current + 1 if previous is not None and minute - previous <= 180 else 1
        largest = max(largest, current)
        previous = minute
    return largest


def _trakt_import_history(data, now):
    _trakt_validate_export(data)
    minutes = Counter(minute for movie in data["movies"] for minute in movie["plays"])
    minutes.update(episode[2] for show in data["shows"] for episode in show["eps"])
    # shortcut: same-minute batches of >=5 are backfill; add burst detection if imports spread out.
    backfill = {minute for minute, count in minutes.items() if count >= 5}
    items = []
    for kind in ("movies", "shows"):
        for row in data[kind]:
            series = kind == "shows"
            plays = [episode[2] for episode in row["eps"]] if series else row["plays"]
            real = [minute for minute in plays if minute not in backfill]
            at = max(real) * 60_000 if real else None
            item = {
                "profile": "me", "type": "series" if series else "movie",
                "id": f"tmdb:{row['tmdb']}", "imdb": row.get("imdb"),
                "title": row["title"], "year": row.get("year"),
                "genres": row.get("genres") or [], "lang": row.get("lang"),
                "at": at, "first_at": min(real) * 60_000 if real else None,
                "backfilled": any(minute in backfill for minute in plays),
                "played": bool(plays), "plays": len(plays),
            }
            flags = []
            if series:
                episodes = {tuple(episode[:2]) for episode in row["eps"]}
                count = len(episodes)
                total = row.get("eps_total")
                completion = min(count / total, 1.0) if total else None
                rewatch = len(plays) - count
                session = _trakt_max_session_eps(real)
                item.update({
                    "episodes_played": count, "episodes_total": total,
                    "completion": completion,
                    "last_episode": list(max(episodes)) if episodes else None,
                    "max_session_eps": session,
                })
                signal = "interest"
                # Returning shows are caught up against aired totals, not future seasons.
                caught_up = row.get("status") == "returning series" and completion == 1.0
                if caught_up or (completion is not None and completion >= 0.9):
                    flags.append("completed")
                    signal = "loved" if count >= 50 or rewatch >= 1 else "watched"
                elif count >= 3 and (completion is None or completion >= 0.25):
                    signal = "watched"
                elif (
                    completion is not None and completion < 0.25 and 0 < count <= 3
                    and at is not None and now - at / 1000 > 60 * DAY
                ):
                    signal = "abandoned"
                    flags.append("dropped_early")
                if session >= 4:
                    flags.append("binge")
                if rewatch >= 3:
                    flags.append("comfort")
            else:
                rewatch = max(0, len(plays) - 1)
                signal = "loved" if len(plays) >= 2 else "watched" if plays else "interest"
                if len(plays) >= 2:
                    flags.append("comfort")
            if rewatch >= 1:
                flags.append("rewatch")
            genres = {genre.lower() for genre in item["genres"]}
            if "anime" in genres or (item["lang"] == "ja" and "animation" in genres):
                flags.append("anime")
            if plays and not real:
                flags.append("backfill_only")
            age = (now - at / 1000) / DAY if at is not None else float("inf")
            recency = 1.0 if age <= 30 else 0.7 if age <= 120 else 0.5 if age <= 365 else 0.35
            weight = TRAKT_BASE_WEIGHT[signal] * recency + 0.5 * ("binge" in flags) + 0.5 * ("comfort" in flags)
            item.update({"rewatch": rewatch, "signal": signal, "flags": flags, "weight": round(weight, 3)})
            items.append(item)
    items.sort(key=lambda item: (-item["weight"], -(item["at"] if item["at"] is not None else float("-inf"))))
    backfill_plays = sum(minutes[minute] for minute in backfill)
    return {
        "version": 1, "source": "trakt", "exported_at": data["exported_at"],
        "generated_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "stats": {
            "movies": len(data["movies"]), "shows": len(data["shows"]),
            "plays": sum(minutes.values()), "backfill_plays": backfill_plays,
            "real_plays": sum(minutes.values()) - backfill_plays,
        },
        "items": items,
    }



def _connector_trakt_api(c, now, budget=None, *, readonly=False):
    cfg = CONFIG["connectors"]["trakt_api"]
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    if "accounts" in c:
        accounts, events = copy.deepcopy(c["accounts"]), []
        for account in accounts.values():
            if "backoff_until" in budget:
                account["last_error"] = "RateLimited"
                continue
            try:
                new_events, cursor, stats = _connector_trakt_api(account, now, budget)
            except Exception as error:
                account["last_error"] = type(error).__name__
                if isinstance(error, TraktLoginRequired):
                    account["health"]["fixHint"] = "re-run skald trakt login --member " + account["person"]
                if isinstance(error, (urllib.error.HTTPError, TraktLoginRequired)) and getattr(error, "code", None) == 429:
                    try:
                        delay = float(error.headers.get("Retry-After", CONNECTOR_MIN))
                    except (TypeError, ValueError):
                        delay = CONNECTOR_MIN
                    budget["backoff_until"] = now + (max(CONNECTOR_MIN, delay) if math.isfinite(delay) else CONNECTOR_MIN)
                continue
            account.pop("last_error", None)
            new = _connector_advanced(account, cursor)
            account.update(cursor=cursor, last_sync=now, health={"state": "ok", "error": None, "at": now})
            if new:
                account["last_new"] = now
            account["counts"].update(events=stats["events"], new_last=len(new_events) if new else 0,
                                     polls=account["counts"]["polls"] + 1)
            account["interval"], account["next_due"] = connector_next(account, new, now)
            events.extend(new_events)
        cursor = [max(((a["cursor"] or [0, 0])[0] for a in accounts.values()), default=0),
                  sum(a["counts"]["events"] for a in accounts.values())]
        return events, cursor, {"events": cursor[1], "error": None, "accounts": accounts,
                                "backoff_until": budget.get("backoff_until", 0)}
    reference = c["auth"] if c["id"].startswith("trakt_api:") else cfg["auth"]
    bundle = _read_trakt_auth(reference)
    renewed = False
    if not readonly and "refresh_token" in bundle and bundle["created_at"] + bundle["expires_in"] <= now + 60:
        bundle = _renew_trakt_auth(reference, bundle, cfg, budget)
        renewed = True
    headers = {"trakt-api-version": "2", "trakt-api-key": _resolve_auth(cfg.get("options", {}).get("client_id", "env:TRAKT_CLIENT_ID")),
               "Authorization": "Bearer " + bundle["access_token"]}
    for attempt in range(2):
        try:
            history, movies, shows, ratings = [_http_get(TRAKT_URL + path + "?extended=full", headers, paged=True, budget=budget)
                for path in ("/sync/history", "/sync/watched/movies", "/sync/watched/shows", "/sync/ratings")]
            break
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            if readonly or renewed or "refresh_token" not in bundle:
                raise TraktLoginRequired() from None
            bundle = _renew_trakt_auth(reference, bundle, cfg, budget)
            headers["Authorization"] = "Bearer " + bundle["access_token"]
            renewed = True
    doc = _trakt_import_history(_trakt_export(history, movies, shows, ratings, now), now)
    from skald.ingest import rating_signal
    by_key = {(it["type"], it["id"]): it for it in doc["items"]}
    for row in ratings:
        kind = "movie" if "movie" in row else "show" if "show" in row else None
        if kind is None or row.get("episode"):
            continue
        signal, weight = IMPORT_SIGNALS[rating_signal(row["rating"])]
        it = by_key[("movie" if kind == "movie" else "series", "tmdb:" + str(row[kind]["ids"]["tmdb"]))]
        it.update(signal=signal, weight=weight, rating=row["rating"], rated_at=row.get("rated_at"))
    if readonly:
        return doc, None, {"plays_30d": sum(now - 30 * DAY <= (_connector_time(row.get("watched_at")) or 0) <= now
                                               for row in history)}
    if c["id"].startswith("trakt_api:"):
        _connector_write("trakt-" + c["person"] + ".history", doc, indent=2, newline=True)
        old_time = _connector_time((c["cursor"] or [None, 0])[0]) or 0
        events = [e for e in trakt_events(now, [{**it, "person": c["person"]} for it in doc["items"]]) if (e["t"] or 0) > old_time]
        stats = {"events": len(doc["items"]), "error": None}
    else:
        _connector_write("trakt", doc, indent=2, newline=True)
        events, _, stats = _connector_trakt(c, now)
    latest = max((_connector_time(row.get(field)) or 0 for rows in (history, movies, shows, ratings)
                  for row in rows for field in ("watched_at", "last_watched_at", "rated_at")), default=0)
    return events, [latest, len(doc["items"])], stats


def _connector_job(c, now):
    try:
        return poll(c, now)
    except Exception as error:
        return [], c["cursor"], {"events": c["counts"]["events"], "error": type(error).__name__}


def _connector_async(c, now):
    global _connector_pending
    if _connector_pending is None:
        _connector_pending = (DATA, c["id"], _connector_pool.submit(_connector_job, c, now))
        return None
    data, cid, future = _connector_pending
    if data != DATA or cid != c["id"] or not future.done():
        return None
    _connector_pending = None
    return future.result()


CONNECTOR_POLL = {"history": _connector_history, "trakt": _connector_trakt,
                  "aiostreams_db": _connector_player, "jellyfin": _connector_player, "plex": _connector_player,
                  "trakt_api": _connector_trakt_api}


def poll(c, now):
    return CONNECTOR_POLL[c["id"]](c, now)


def connectors_demand(now):
    global _connectors_generation
    with _connectors_lock:
        changed = False
        for c in _connectors.values():
            if c["kind"] not in ("manual_export", "availability") and c["health"]["state"] != "disabled" and (c["last_sync"] is None or now - c["last_sync"] >= CONNECTOR_DEMAND_GAP):
                if c["next_due"] != now:
                    c["next_due"], changed = now, True
        if changed:
            _connectors_generation += 1


def _connector_advanced(c, cursor):
    if c["id"] == "history":
        return cursor > (c["cursor"] or 0)
    old = c["cursor"] or [None, 0]
    return cursor[1] > old[1] or (_connector_time(cursor[0]) or 0) > (_connector_time(old[0]) or 0)


def _connector_material(c):
    material = c["cursor"], c["health"]["state"], c["last_new"], c["interval"]
    return material + (c["accounts"],) if "accounts" in c else material


def _connector_imports_stamp():
    try:
        st = os.stat(os.path.join(DATA, "imports.jsonl"))
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def connectors_tick(now):
    global _connectors_generation, _connector_import_stamp, _connectors_busy
    if _connectors_path != DATA:
        connectors_load(now)
    with _connectors_lock:
        if _connectors_busy or _connectors_path != DATA:
            return
        _connectors_busy = True
        generation = _connectors_generation
        rows = {cid: {**c, "health": dict(c["health"]), "counts": dict(c["counts"])} for cid, c in _connectors.items()}
    try:
        changed = False
        stamp = _connector_imports_stamp()
        if stamp != _connector_import_stamp:
            _connector_import_stamp = stamp
            for cid, seed in _connector_manual(read_imports(), now).items():
                old = rows.get(cid)
                if old is None:
                    rows[cid], changed = seed, True
                else:
                    old["counts"]["events"] = seed["counts"]["events"]
                    if seed["cursor"] > (old["cursor"] or ""):
                        old.update(cursor=seed["cursor"], last_new=seed["last_new"])
                        changed = True
        for cid, c in rows.items():
            before = _connector_material(c)
            if c["health"]["state"] == "disabled" or c["health"]["error"] == "AuthNotReference":
                continue
            if c["kind"] == "manual_export":
                state = "stale" if connector_reminder(c, now) else "ok"
            elif c["kind"] in ("api", "player") and cid in CONNECTOR_POLL:
                if c["next_due"] is None or c["next_due"] > now:
                    continue
                stats = {}
                try:
                    result = _connector_async(c, now)
                    if result is None:
                        continue
                    events, cursor, stats = result
                    if "accounts" in stats:
                        c["accounts"] = stats["accounts"]
                    error = stats["error"]
                    new = not error and _connector_advanced(c, cursor)
                    if not error:
                        c["cursor"] = cursor
                        if new:
                            c["last_new"] = now
                    c["counts"].update(events=stats["events"], new_last=len(events) if new else 0)
                except Exception as e:
                    error, new = type(e).__name__, False
                    c["counts"]["new_last"] = 0
                c["counts"]["polls"] += 1
                c["last_sync"] = now
                c["interval"], c["next_due"] = connector_next(c, new, now)
                c["next_due"] = max(c["next_due"], stats.get("backoff_until", 0))
                for account in c.get("accounts", {}).values():
                    account.update(interval=c["interval"], next_due=c["next_due"])
                c["health"]["error"] = error
                if error == "CredentialsDirectoryUnsafe":
                    c["health"]["fixHint"] = "Set DATA/credentials/ owner to the engine uid and chmod 0700."
                elif error == "TraktLoginRequired":
                    c["health"]["fixHint"] = "re-run skald trakt login --member " + c["person"]
                else:
                    c["health"].pop("fixHint", None)
                state = "error" if error else "ok"
            elif c["kind"] == "file_drop" and cid in CONNECTOR_POLL:
                path = _drop_path(cid)
                try:
                    st = os.stat(path)
                    stamp = (path, st.st_mtime_ns, st.st_size)
                    error = None
                except OSError as e:
                    stamp, error = (path, None, None), type(e).__name__
                prev, at = _connector_stamps.get(cid, (None, now))
                if prev != stamp:
                    at = min(now, st.st_mtime) if prev is None and error is None else now
                _connector_stamps[cid] = stamp, at
                due = c["next_due"] is not None and c["next_due"] <= now
                if prev != stamp or due:
                    try:
                        events, cursor, stats = poll({**c, "_stamp": stamp}, now)
                        error = error or stats["error"]
                        new = not error and _connector_advanced(c, cursor)
                        if not error:
                            c["cursor"] = cursor
                            if new:
                                c["last_new"] = now
                        c["counts"] = {"events": stats["events"], "new_last": len(events) if new else 0, "polls": c["counts"]["polls"] + 1}
                    except Exception as e:
                        error, new = type(e).__name__, False
                        c["counts"].update(new_last=0, polls=c["counts"]["polls"] + 1)
                    c["last_sync"] = now
                    c["interval"], c["next_due"] = connector_next(c, new, now)
                    c["health"]["error"] = error
                stale_at = at if cid == "history" else c["last_new"]
                state = "error" if c["health"]["error"] else "stale" if stale_at is not None and now - stale_at >= CONNECTOR_STALE[cid] else "ok"
            else:
                continue
            if state != c["health"]["state"]:
                c["health"].update(state=state, at=now)
            changed |= before != _connector_material(c)
        with _connectors_lock:
            if generation != _connectors_generation:
                # Demand only changes next_due; retain an unconsumed demand while publishing poll results.
                for cid, c in rows.items():
                    current = _connectors.get(cid, c)
                    if current["next_due"] is not None and (c["last_sync"] is None or current["next_due"] > c["last_sync"]):
                        c["next_due"] = min(current["next_due"], c["next_due"]) if c["next_due"] is not None else current["next_due"]
            _connectors.update(rows)
            _connectors_generation += 1
            generation = _connectors_generation
        if changed or _connectors_saved != {cid: _connector_material(c) for cid, c in rows.items()}:
            _connectors_save(rows, generation)
    finally:
        with _connectors_lock:
            _connectors_busy = False


def connectors_view(req=None, now=None):
    if _connectors_path != DATA:
        connectors_load(now)
    now = time.time() if now is None else now
    viewer = req.get("viewer") if req else None
    with _connectors_lock:
        accounts = [account for c in _connectors.values() for account in (c.get("accounts", {}).values() if "accounts" in c else [c])]
        rows = [{k: c[k] for k in ("id", "kind", "person", "last_sync", "last_new", "next_due", "interval")}
                | {"health": dict(c["health"]), "counts": dict(c["counts"]), "auth": "ref" if c["auth"] else None,
                   "reminder": connector_reminder(c, now)} | ({"last_error": c["last_error"]} if "last_error" in c else {})
                for c in accounts if viewer is None or c["person"] in (viewer, "both")]
    for c in rows:
        if c["reminder"] and c["health"]["state"] == "ok":
            c["health"].update(state="stale", at=now)
    return {"connectors": sorted(rows, key=lambda c: c["id"])}


def genre_names(raw):
    out = []
    for g in raw:
        g = TRAKT_GENRES.get(g, g.replace("-", " ").title()) if g.islower() else g
        for name in [GENRE_ALIASES.get(g, g)] + ([GENRE_ALIASES.get(x, x) for x in g.split(" & ")] if " & " in g else []):
            if name not in out:
                out.append(name)
    return out


def trakt_entry(it, now):
    tid = int(it["id"][5:])
    age = (now - it["at"] / 1000) / DAY if it.get("at") else 999
    kind = "movie" if it["type"] == "movie" else "tv"
    person = it["person"] if "accounts" in CONFIG["connectors"].get("trakt_api", {}) else TRAKT_PERSON
    return {"key": it["id"], "id": tid, "kind": kind, "stype": "movie" if kind == "movie" else "series",
            "title": it.get("title") or "", "year": str(it.get("year") or ""), "signal": it.get("signal") or "watched",
            "weight": float(it.get("weight") or 0), "age": age, "item": it, "who": "owner", "by": [person],
            "flags": it.get("flags") or [], "genres": genre_names(it.get("genres") or [])}


# --- events ---
EVENT_ACTION = {"loved": "love", "liked": "like", "watched": "play", "interest": "interest",
                "opened": "open", "abandoned": "abandon", "disliked": "not_for_us"}
EVENT_FAMILY = {"tv": "tv", "trakt": "trakt", "told": "told", "remote": "remote", "interview": "remote"}
ACTION_W = {}  # populated on first use, after the source tables below are defined.
OBSERVER = {"self": 1.0, "other": 1.0}
_event_stats = {"unknown_src": 0}


def trust(src):
    if src.startswith(("import:", "sync:")):
        value = IMPORT_TRUST.get(src[7:] if src.startswith("import:") else src[5:])
        if value is not None:
            return value
    elif src in ("tv", "trakt", "told", "remote", "interview"):
        return 1.0
    elif src.startswith("agent:") or src == "utterance":
        return .8
    _event_stats["unknown_src"] += 1
    return 0.0


def observer(ev):
    if ev["who"] in ("both", "together"):
        return "self" if set(ev["by"]) >= set(PEOPLE) else "other"
    return "self" if ev["who"] in ev["by"] else "other"


def tainted(ev):
    return ev["src"].startswith("agent:") or ev["src"] == "utterance" or trust(ev["src"]) < .8


def event_weight(ev, now):
    if not ACTION_W:
        weights = {(family, action): (signal, SIGNAL_WEIGHT[signal])
                   for family in ("tv", "trakt") for signal, action in EVENT_ACTION.items()}
        weights.update({("import", action): pair for action, pair in IMPORT_SIGNALS.items()})
        weights.update({("remote", action): pair for action, pair in {
            "love": ("loved", 3.0), "like": ("liked", 2.0), "meh": ("watched", 0.3),
            "seen": ("watched", 1.0), "not_for_us": ("disliked", -3.0)}.items()})
        weights[("told", "love")] = ("loved", SEED_WEIGHT)
        ACTION_W.update(weights)
    src = ev["src"]
    family = EVENT_FAMILY.get(src, "import")
    if family == "trakt":
        age = (now - ev["t"]) / DAY if ev["t"] is not None else 999
        return ev["ctx"]["signal"], float(ev["w0"] or 0), age
    signal, weight = ACTION_W[(family, ev["action"])]
    if family == "import":
        age = max(0, int((now - ev["t"]) / DAY)) if ev["t"] is not None else 999
        decay = 1 - age / 120 if signal == "interest" else 1.0 if age <= 30 else .5 if age <= 120 else .25
    elif family == "told":
        age, decay = SEED_AGE, 1.0
    else:
        age = (now - ev["t"]) / DAY if ev["t"] is not None else 999
        if family == "remote":
            age = max(0, age)
        decay_age = 0 if family == "remote" and ev["action"] == "love" and age <= 90 else age
        decay = 1.0 if decay_age <= 30 else 0.5 if decay_age <= 120 else 0.25
    return signal, weight * ev["conf"] * trust(src) * OBSERVER[observer(ev)] * decay, age


def _event(t, eid, key, kind, title, year, who, by, action, src, ctx, conf=1.0, n=1, reasons=None, w0=None, _ids=None):
    if _ids is not None:
        count = _ids[eid] = _ids.get(eid, 0) + 1
        if count > 1:
            eid = f"{eid}#{count}"
    return {"t": t, "id": eid, "key": key, "kind": kind, "title": title, "year": year,
            "who": who, "by": by, "action": action, "src": src, "conf": conf, "n": n,
            "reasons": [] if reasons is None else reasons, "w0": w0, "ctx": ctx}


def tv_events(now, items, _ids=None):
    ids = {} if _ids is None else _ids
    jobs = [(it, _pool.submit(tmdb_details, it)) for it in items[:40]]
    for it, job in jobs:
        try:
            hit = job.result()
        except Exception:
            continue
        if not hit:
            continue
        kind, det = hit
        signal, _ = classify(it, now)
        who, by, ctx = attribute(it)
        yield _event(it["at"] / 1000 if it.get("at") else None, f"tv:{kind}:{it['id']}",
                     f"tmdb:{det['id']}", kind, det.get("title") or det.get("name") or "",
                     (det.get("release_date") or det.get("first_air_date") or "")[:4], who, by,
                     EVENT_ACTION[signal], "tv", {"item": it, "genres": genre_names([g["name"] for g in det.get("genres") or []]),
                                                  "flags": [], "signal": signal, **ctx}, _ids=ids)


def trakt_events(now, trakt, _ids=None):
    ids = {} if _ids is None else _ids
    for it in trakt:
        person = it["person"] if "accounts" in CONFIG["connectors"].get("trakt_api", {}) else TRAKT_PERSON
        kind = "movie" if it["type"] == "movie" else "tv"
        signal = it.get("signal") or "watched"
        yield _event(it["at"] / 1000 if it.get("at") else None, f"trakt:{kind}:{it['id']}", it["id"], kind,
                     it.get("title") or "", str(it.get("year") or ""), person, [person],
                     EVENT_ACTION.get(signal, signal), "trakt", {"item": it, "genres": genre_names(it.get("genres") or []),
                     "flags": it.get("flags") or [], "signal": signal}, w0=it.get("weight"), _ids=ids)


def told_events(_ids=None):
    ids = {} if _ids is None else _ids
    jobs = [(person, title, kind, _outer.submit(resolve_pick, kind, title, year, "en-US"))
            for person, title, year, kind in person_loves()]
    for person, title, kind, job in jobs:
        try:
            hit = job.result()
        except Exception as e:
            online_error(e)
            hit = None
        if not hit:
            print(f"loves: no TMDB match for {person}: {title}", flush=True)
            continue
        it = hit[0]
        saga = set()
        if kind == "movie":
            try:
                col = tmdb(f"movie/{it['id']}", language="en-US").get("belongs_to_collection")
                if col:
                    saga = {f"tmdb:{x['id']}" for x in tmdb(f"collection/{col['id']}", language="en-US").get("parts") or []}
            except Exception:
                pass
        key = f"tmdb:{it['id']}"
        yield _event(None, f"told:{person}:{kind}:{key}", key, kind, it.get("title") or it.get("name") or title,
                     (it.get("release_date") or it.get("first_air_date") or "")[:4], person, [person], "love", "told",
                     {"saga": saga, "item": it}, _ids=ids)


def import_events(now, active=None, _ids=None):
    ids = {} if _ids is None else _ids
    # shortcut: 20k folds exceed the 1.2x CPU target; optimise before bulk intake.
    dates, sources = {}, {}
    for e in reversed(active_imports(active)):
        at = dates.get(e["t"])
        if at is None:
            at = dates[e["t"]] = datetime.datetime.combine(datetime.date.fromisoformat(e["t"]), datetime.time(), datetime.timezone.utc).timestamp()
        people = sorted(_expand_together([e["who"]]))
        src = sources.get(e["src"])
        if src is None:
            src = sources[e["src"]] = e["src"] if ":" in e["src"] else "import:" + e["src"]
        eid = e["id"]
        count = ids[eid] = ids.get(eid, 0) + 1
        if count > 1:
            eid = f"{eid}#{count}"
        yield {"t": at, "id": eid, "key": e["key"], "kind": e["kind"], "title": e["title"], "year": e["year"],
               "who": e["who"], "by": people, "action": e["action"], "src": src, "conf": e["conf"], "n": e["n"],
               "reasons": [], "w0": None, "ctx": {"src": e["src"]}}


def remote_events(now, active=None, _ids=None):
    ids = {} if _ids is None else _ids
    for (key, person), e in (feedback_state()["active"] if active is None else active).items():
        if e["action"] == "not_tonight":
            continue
        eid = e["id"] + ":" + person if e["who"] == "both" else e["id"]
        yield _event(e["t"], eid, key, e["kind"], e["title"], e["year"], person, [person], e["action"],
                     "interview" if e.get("row") == rid('interview') else "remote", {"row": e.get("row")}, reasons=e["reasons"], _ids=ids)


def event_stages(now, items, trakt=(), remote=True):
    counts = {}
    stages = (("tv", tv_events(now, items, counts)), ("trakt", trakt_events(now, trakt, counts)), ("told", told_events(counts)))
    if remote:
        stages += (("import", import_events(now, _ids=counts)), ("remote", remote_events(now, _ids=counts)))
    return stages


def events(now, items, trakt=(), remote=True):
    for _, evts in event_stages(now, items, trakt, remote):
        yield from evts


def _import_event_entries(now, evts):
    interests = {}
    for ev in evts:
        if not ev["conf"]:
            continue
        signal, base = IMPORT_SIGNALS[ev["action"]]
        people = ev["by"]
        if signal == "interest":
            age = max(0, int((now - ev["t"]) / DAY)) if ev["t"] is not None else 999
            if age >= 120 or any(interests.get(person, 0) >= 20 for person in people) or not base:
                continue
        signal, weight, age = event_weight(ev, now)
        if not weight:
            continue
        if signal == "interest":
            for person in people:
                interests[person] = interests.get(person, 0) + 1
        yield {"key": ev["key"], "id": int(ev["key"][5:]), "kind": ev["kind"],
               "stype": "movie" if ev["kind"] == "movie" else "series", "title": ev["title"], "year": ev["year"],
               "signal": signal, "weight": weight, "age": age, "who": "import", "by": people,
               "flags": ["import", ev["ctx"]["src"]], "genres": [],
               "item": {"episodes_played": ev["n"] if ev["kind"] == "tv" and ev["action"] == "seen" else 0}, "n": ev["n"]}


# Owner decisions, in brief order.
IMPORT_TOLD_LOCKED = True
IMPORT_REMOTE_NEWEST = True
IMPORT_COMPLETION_FIRST = True
IMPORT_EXPLICIT_FIRST = True
IMPORT_SHARED_OPINIONS = True
OPINION_TRUST_FIRST = True
REMOTE_PER_PERSON = True
_claim_records = threading.local()  # Entry identities, never persisted profile fields.


def _expand_together(by):
    by = set(by)
    return (by - {"both", "together"}) | set(PEOPLE) if by & {"both", "together"} else by


def _claim_record(value):
    pair = getattr(_claim_records, "entries", {}).get(id(value))
    return pair[1] if pair and pair[0] is value else None


def _claim_rank(e, now, person=None, newest=False):
    from skald.ingest import EXPLICIT, PRIORITY
    value = (e.get("opinions") or {}).get(person) or e.get("shared") or e
    record = _claim_record(value)
    if record and len(record) > 5 and person in record[5]:
        record = record[5][person]
    if "action" in e:  # A validated batch row, before it has an entry.
        src, action, who, eid = "import:" + e["src"], e["action"], e["who"], e.get("id", "")
        day = datetime.date.fromisoformat(e["t"]).toordinal() - datetime.date(1970, 1, 1).toordinal() if e.get("t") else -math.inf
        signal = IMPORT_SIGNALS[action][0]
    elif record:
        at, src, action, who, eid = record[:5]
        day = math.floor(at / DAY) if at is not None else -math.inf
        signal = value["signal"]
    else:
        flags = value.get("flags", e.get("flags", ()))
        src = "remote" if "remote" in flags else value.get("wsrc") or (
              "import" if "import" in flags or e.get("who") == "import" else
              "told" if "told" in flags or e.get("who") == "told" else
              "remote" if e.get("who") == "remote" else "trakt" if e.get("who") == "owner" else "tv")
        if src == "import":
            src = flags[1] if len(flags) > 1 else "plain"
            if ":" not in src: src = "import:" + src
        signal = value["signal"]
        action = EVENT_ACTION.get(signal, "open")
        if action == "play": action = "seen"
        if src in ("remote", "interview") and signal == "watched" and value["weight"] in (.3, .15, .075): action = "meh"
        who = "both" if len(e.get("by", ())) > 1 or "together" in e.get("by", ()) else person
        eid = ""
        item = value.get("item", e.get("item", {}))
        day = math.floor(now / DAY - e.get("age", 999))
        if src in ("tv", "trakt"):
            day = math.floor(item["at"] / 1000 / DAY) if item.get("at") else -math.inf
    explicit = src != "trakt" and (action in EXPLICIT if record or "action" in e else
               action in EXPLICIT if src in ("remote", "interview") else signal in ("loved", "liked", "disliked"))
    if not IMPORT_COMPLETION_FIRST and signal in ("watched", "abandoned"): explicit = True
    inferred = {"opened": 0, "interest": 1, "abandoned": 2, "watched": 3, "loved": 4}.get(signal, 0)
    tie = (-trust(src), -({"remote": 2, "interview": 2, "tv": 1, "trakt": 1}.get(src, 0)),
           -PRIORITY.get(action, -1), -(who not in ("both", "together")), src, eid)
    if not OPINION_TRUST_FIRST:
        tie = (tie[2], tie[0], tie[1], *tie[3:])
    if newest or not IMPORT_EXPLICIT_FIRST: return (-day, *tie)
    return (-(explicit and IMPORT_EXPLICIT_FIRST),
            -(inferred if not explicit and IMPORT_COMPLETION_FIRST else 0), -day, *tie)


def _merge_import_events(entries, by_key, now, evts):
    _claim_records.entries = {}
    grouped, candidates = {}, {}
    for old in entries:
        grouped.setdefault(old["key"], []).append(old)
    current = None
    def tracked():
        nonlocal current
        for current in evts:
            yield current
    for imported in _import_event_entries(now, tracked()):
        _claim_records.entries[id(imported)] = (imported, (current["t"], current["src"], current["action"], current["who"], current["id"]))
        key = imported["key"]
        old = candidates.get(key)
        if old is None:
            candidates[key] = imported
            continue
        choices = old if isinstance(old, list) else [old]
        winners = {p: e for e in choices for p in e["by"]}
        rank = _claim_rank(imported, now)
        for person in imported["by"]:
            old = winners.get(person)
            if old is None or rank < _claim_rank(old, now):
                winners[person] = imported
        kept = list({id(winners[p]): winners[p] for p in sorted(winners)}.values())
        for e in [*choices, imported]:
            if not any(e is winner for winner in kept):
                _claim_records.entries.pop(id(e), None)
        for e in kept:
            e["by"] = sorted(p for p in winners if winners[p] is e)
        candidates[key] = kept[0] if len(kept) == 1 else kept
    for key, choices in candidates.items():
        if not isinstance(choices, list): choices = [choices]
        for imported in choices:
            remaining = set(imported["by"])
            for old in tuple(grouped.get(key, ())):
                by = _expand_together(old.get("by") or [])
                overlap = by & remaining
                if not overlap:
                    continue
                protected = old.get("who") in ("remote", "told") or old.get("who") in ("tv", "both") and real_play(old)
                won = set()
                for person in sorted(overlap):
                    value = (old.get("opinions") or {}).get(person) or old.get("shared") or old
                    if value["signal"] == imported["signal"]:
                        if value.get("wsrc") == "import":
                            _claim_records.entries[id(value)] = (value, (*_claim_record(imported), imported))
                        continue
                    if (old.get("who") == "told" or "told" in value.get("flags", ())) and IMPORT_TOLD_LOCKED:
                        continue
                    if old.get("who") == "remote" and not IMPORT_REMOTE_NEWEST:
                        continue
                    if _claim_rank(imported, now) < _claim_rank(old, now, person):
                        won.add(person)
                remaining -= overlap - won
                if protected:
                    remaining -= won
                    if won and IMPORT_SHARED_OPINIONS:
                        if "together" in old["by"]:
                            old.setdefault("shared", {**{k: old[k] for k in ("signal", "weight", "item", "flags")}, "wsrc": old.get("wsrc")})
                        for person in sorted(won):
                            value = {"signal": imported["signal"], "weight": imported["weight"], "item": {},
                                     "flags": imported["flags"], "wsrc": "import"}
                            old.setdefault("opinions", {})[person] = value
                            _claim_records.entries[id(value)] = (value, (*_claim_record(imported), imported))
                    continue
                if not won:
                    continue
                kept = by - won
                if kept:
                    old["by"] = sorted(kept)
                else:
                    entries.remove(old)
                    grouped[key].remove(old)
                imported["genres"] = old["genres"]
            if remaining:
                imported["by"] = sorted(remaining)
                entries.append(imported)
                by_key[key] = imported
            # Keep metadata only when age/signal cannot recover the claim for the remote stage.
            record = _claim_record(imported)
            if imported["signal"] != "watched" and record and record[0] is not None and record[0] <= now:
                _claim_records.entries.pop(id(imported), None)


def _feedback_event_entries(now, evts, records=None):
    records = {} if records is None else records
    grouped = {}
    for ev in evts:
        grouped.setdefault(ev["key"], []).append(ev)
    entries = []
    for key, reactions in grouped.items():
        if not REMOTE_PER_PERSON or len({ev["action"] for ev in reactions}) == 1:
            # Equal actions retain the original latest-event bytes and combined people.
            latest = reactions[0]
            for ev in reactions[1:]:
                if (ev["t"] if ev["t"] is not None else -math.inf) >= (latest["t"] if latest["t"] is not None else -math.inf): latest = ev
            claims = [(latest, sorted(ev["who"] for ev in reactions))]
        else:
            claims = [(ev, [ev["who"]]) for ev in sorted(reactions, key=lambda ev: ev["who"])]
        for ev, by in claims:
            signal, weight, age = event_weight(ev, now)
            e = {"key": key, "id": int(key[5:]), "kind": ev["kind"],
                 "stype": "movie" if ev["kind"] == "movie" else "series", "title": ev["title"], "year": ev["year"],
                 "signal": signal, "weight": weight, "age": age, "item": {}, "who": "remote", "by": by,
                 "flags": ["remote"], "genres": []}
            record = (ev["t"], ev["src"], ev["action"], ev["who"], ev["id"])
            if len(by) > 1:
                record = (*record, {r["who"]: (r["t"], r["src"], r["action"], r["who"], r["id"]) for r in reactions})
            records[id(e)] = (e, record)
            entries.append(e)
    return entries


def _merge_feedback_events(entries, by_key, now, evts):
    if not hasattr(_claim_records, "entries"): _claim_records.entries = {}
    grouped = {}
    for old in entries:
        grouped.setdefault(old["key"], []).append(old)
    removed, added, updated, revived = set(), [], {}, {}
    for e in _feedback_event_entries(now, evts, _claim_records.entries):
        key, reactors = e["key"], set(e["by"])
        olds = grouped.get(key, [])
        for old in olds:
            by = _expand_together(old["by"])
            for person in sorted(reactors & by):
                value = (old.get("opinions") or {}).get(person) or old.get("shared") or old
                if value.get("wsrc") == "import" or old.get("who") == "import":
                    if IMPORT_REMOTE_NEWEST and _claim_rank(old, now, person) < _claim_rank(e, now, person):
                        reactors.discard(person)
                        record = _claim_record(value)
                        if old.get("who") == "remote" and record and len(record) > 5:
                            imported = record[5]
                            restored = revived.get(id(imported))
                            if restored is None:
                                restored = imported if any(imported is x for x in entries) else {**imported, "by": []}
                                revived[id(imported)] = restored
                                if restored is not imported: added.append(restored)
                            restored["by"] = sorted(set(restored["by"]) | {person})
                            by_key[key] = restored
                            by.discard(person)
                            old["by"] = sorted(by)
                            if not old["by"]: removed.add(id(old))
        if not reactors:
            continue
        e["by"] = sorted(reactors)
        play = None
        for old in olds:
            by = _expand_together(old["by"])
            covered = bool(by & reactors) and (reactors <= by or by <= reactors)
            if old.get("who") in ("tv", "both", "owner") and real_play(old) and covered:
                play = old
        for old in olds:
            if old is play or id(old) in removed: continue
            by = _expand_together(old["by"])
            if by & reactors:
                old["by"] = sorted(by - reactors)
                if not old["by"]: removed.add(id(old))
        if play:
            by = _expand_together(play["by"])
            if by - reactors or play.get("opinions"):
                play.setdefault("shared", {**{k: play[k] for k in ("signal", "weight", "item", "flags")}, "wsrc": play.get("wsrc")})
                for person in sorted(reactors):
                    play.setdefault("opinions", {})[person] = {"signal": e["signal"], "weight": e["weight"],
                        "item": {}, "flags": ["remote"], "wsrc": "remote"}
            # Keep TV provenance, watch age and resume data for learning and rewatch cards.
            play.update(by=list(dict.fromkeys(play["by"] + e["by"])),
                        flags=list(dict.fromkeys(play["flags"] + ["remote"])))
            at = _claim_record(e)[0]
            at = at if at is not None else -math.inf
            if id(play) not in updated or at >= updated[id(play)]:
                play.update(signal=e["signal"], weight=e["weight"])
                updated[id(play)] = at
            by_key[key] = play
        else:
            added.append(e)
            grouped.setdefault(key, []).append(e)
            by_key[key] = e
    entries[:] = [x for x in entries if id(x) not in removed] + [x for x in added if id(x) not in removed]
    _claim_records.entries.clear()


def fold_events(stages, now):
    _claim_records.entries = {}
    try:
        entries, by_key, extra_seen = [], {}, set()
        order, previous = ("tv", "trakt", "told", "import", "remote"), -1
        for stage, evts in stages:
            position = order.index(stage)
            if position <= previous:
                raise ValueError("event stages out of order")
            previous = position
            if stage == "told":
                extra_seen |= add_person_loves(entries, by_key, evts)
            elif stage == "import":
                _merge_import_events(entries, by_key, now, evts)
            elif stage == "remote":
                _merge_feedback_events(entries, by_key, now, evts)
            elif stage == "tv":
                for ev in evts:
                    if ev["who"] == GUEST:
                        continue
                    signal, weight, age = event_weight(ev, now)
                    kind, key, ctx = ev["kind"], ev["key"], ev["ctx"]
                    e = {"key": key, "id": int(key[5:]), "kind": kind, "stype": "movie" if kind == "movie" else "series",
                         "title": ev["title"], "year": ev["year"], "signal": signal, "weight": weight, "age": age,
                         "item": ctx["item"], "who": "tv", "by": ev["by"], "flags": ctx["flags"], "genres": ctx["genres"]}
                    by_key[key] = e
                    entries.append(e)
            elif stage == "trakt":
                for ev in evts:
                    t = trakt_entry(ev["ctx"]["item"], now)
                    t["signal"], t["weight"], t["age"] = event_weight(ev, now)
                    t["wsrc"] = "trakt"
                    e = by_key.get(t["key"])
                    if not e:
                        by_key[t["key"]] = t
                        entries.append(t)
                        continue
                    person = ev["who"]
                    watch_fields = ("age", "genres", "title", "year") if "accounts" in CONFIG["connectors"].get("trakt_api", {}) else ()
                    if "accounts" in CONFIG["connectors"].get("trakt_api", {}) and e["who"] not in ("tv", "both"):
                        old = {k: e[k] for k in ("signal", "weight", "item", "flags") + watch_fields}
                        old["wsrc"] = e.get("wsrc")
                        if "together" in e["by"]:
                            e.setdefault("shared", old)
                        for other in e["by"]:
                            if other in PEOPLE:
                                e.setdefault("opinions", {}).setdefault(other, old)
                        e.setdefault("opinions", {})[person] = {k: t[k] for k in ("signal", "weight", "item", "flags", "wsrc") + watch_fields}
                    # Keep account opinions separate from shared TV signals.
                    if e["who"] in ("tv", "both"):
                        if "together" not in e["by"]: e["by"] = sorted(set(e["by"]) | {person})
                        e.setdefault("shared", {**{k: e[k] for k in ("signal", "weight", "item", "flags") + watch_fields}, "wsrc": e.get("wsrc")})
                        e["who"] = "both"
                        e.setdefault("opinions", {})[person] = {k: t[k] for k in ("signal", "weight", "item", "flags", "wsrc") + watch_fields}
                    else:
                        e["by"] = list(dict.fromkeys(e["by"] + t["by"]))
                    e["flags"] = list(dict.fromkeys(e["flags"] + t["flags"]))
                    if t["signal"] == "loved" or (e["signal"] != "loved" and t["weight"] > e["weight"]):
                        if t["weight"] >= e["weight"]:
                            e["wsrc"] = "trakt"
                        e.update(signal=t["signal"], weight=max(e["weight"], t["weight"]))
                    e["age"] = min(e["age"], t["age"])
                    if t["item"].get("last_episode") and tuple(t["item"]["last_episode"]) > tuple(e["item"].get("last_episode") or ()):
                        e["item"] = {**e["item"], "last_episode": t["item"]["last_episode"]}
        return entries, by_key, extra_seen
    finally:
        _claim_records.entries.clear()


def build_profile(items, searches, trakt=(), remote=True):
    """Household TV history plus the owner's Trakt history, folded in source precedence order."""
    now = time.time()
    entries, by_key, extra_seen = fold_events(event_stages(now, items, trakt, remote), now)
    entries.sort(key=lambda e: (-(e["weight"] / (1 + min(e["age"], 365) / 60)), e["age"]))
    genre_w = {}
    for e in entries:
        for g in e["genres"]:
            genre_w[g] = genre_w.get(g, 0) + e["weight"]
    return {"entries": entries, "genres": genre_w, "searches": searches, "likes": "",
            "seen": {e["key"] for e in entries} | extra_seen, "negative": {e["key"] for e in entries if e["weight"] < 0}}


SEED_WEIGHT = 2.0  # a title someone told us they love: timeless, so no recency
SEED_AGE = 400     # old enough that Watch Again treats it as a favourite, not a saga in progress


def person_loves():
    """[(person, title, year, kind)] from taste.json people.<name>.loves ("Title|Year|m or s")."""
    out = []
    for person, cfg in ((read_taste().get("people") or {}).items()):
        for raw in (cfg or {}).get("loves") or []:
            parts = [x.strip() for x in str(raw).split("|")]
            if parts and parts[0]:
                year = parts[1][:4] if len(parts) > 1 and parts[1][:4].isdigit() else None
                kind = "tv" if len(parts) > 2 and parts[2].lower().startswith("s") else "movie"
                out.append((person, parts[0], year, kind))
    return out


def add_person_loves(entries, by_key, evts=None):
    """Adds each person's loved titles; a saga film marks the whole saga as seen. Returns those extra seen keys."""
    extra = set()
    for ev in told_events() if evts is None else evts:
        person, kind, key = ev["who"], ev["kind"], ev["key"]
        signal, weight, age = event_weight(ev, 0)
        e = by_key.get(key)
        if e:
            old = dict(signal=e["signal"], weight=e["weight"], item=e.get("item") or {}, flags=e.get("flags") or [])
            if "together" in e["by"]:
                e.setdefault("shared", {**old, "wsrc": e.get("wsrc")})
            opinions = e.setdefault("opinions", {})
            for other in e["by"]:
                if other in PEOPLE:
                    opinions.setdefault(other, {**old, "wsrc": e.get("wsrc")})
            if person not in e["by"]:
                e["by"] = e["by"] + [person]
            e.setdefault("opinions", {})[person] = {"signal": signal, "weight": weight, "item": {}, "flags": ["told"]}
            e.update(signal=signal, weight=max(e["weight"], weight))
        else:
            it = ev["ctx"]["item"]
            try:
                gs = genre_names([g for g in (genres(kind, "en-US").get(i) for i in it.get("genre_ids") or []) if g])
            except Exception as err:
                online_error(err)
                continue
            e = {"key": key, "id": it["id"], "kind": kind, "stype": "movie" if kind == "movie" else "series",
                 "title": ev["title"], "year": ev["year"], "signal": signal,
                 "weight": weight, "age": age, "item": {}, "who": "told", "by": [person], "flags": ["told"],
                 "genres": gs}
            by_key[key] = e
            entries.append(e)
        extra |= ev["ctx"]["saga"]
    return extra


def profile_text(p, you="they"):
    def names(signals, who=None, n=10, entries=None):
        hits = [e for e in (p["entries"] if entries is None else entries) if e["signal"] in signals and (who is None or e.get("who") in who)][:n]
        return "; ".join(f"{e['title']} ({e['year']}, {'movie' if e['kind'] == 'movie' else 'series'}"
                         f"{', ' + '/'.join(f for f in e.get('flags') or [] if f in ('binge', 'comfort', 'anime')) if set(e.get('flags') or []) & {'binge', 'comfort', 'anime'} else ''})"
                         for e in hits)
    lines = [f"What {you} told us {'they' if you == 'they' else 'he'} love: {p['likes']}"] if p.get("likes") else []
    owner = [e for e in p["entries"] if e.get("who") in ("owner", "both")]
    logs = [(TRAKT_PERSON, owner)]
    if "accounts" in CONFIG["connectors"].get("trakt_api", {}):
        logs = [(person, [e for e in person_entries(p, person) if e.get("wsrc") == "trakt"])
                for person in CONFIG["connectors"]["trakt_api"]["accounts"]]
    for person, owner in logs:
        if not owner:
            continue
        n_series = sum(e["stype"] == "series" for e in owner)
        n_anime = sum("anime" in (e.get("flags") or []) for e in owner)
        lines.append(f"{LABELS[person]} keeps a long-term watch log ({len(owner) - n_series} movies, {n_series} series, "
                     f"{n_anime} of them anime). From that log:")
        for label, sig in (("  Loved (finished long runs, rewatched)", ("loved",)), ("  Liked", ("liked",)), ("  Watched", ("watched",)),
                           ("  Gave up early (avoid similar)", ("abandoned",))):
            if names(sig, ("owner", "both"), 14, owner):
                lines.append(f"{label}: {names(sig, ('owner', 'both'), 14, owner)}")
        runs = sorted((e for e in owner if e["stype"] == "series" and e["signal"] == "loved"),
                      key=lambda e: -(e["item"].get("episodes_played") or 0))[:12]
        if runs:
            lines.append("  Longest runs (episodes watched): " + "; ".join(
                f"{e['title']} ({e['item'].get('episodes_played')}{', anime' if 'anime' in e['flags'] else ''})" for e in runs))
    for person, cfg in (read_taste().get("people") or {}).items():
        told = [e["title"] for e in p["entries"] if person in (e.get("by") or []) and "told" in (e.get("flags") or [])
                or (person in (e.get("by") or []) and e.get("who") in ("tv", "both") and person != TRAKT_PERSON)]
        if told:
            lines.append(f"{LABELS[person]} loves: " + "; ".join(told[:12]))
    tv = [e for e in p["entries"] if e.get("who") in ("tv", "both")]
    if tv:
        lines.append("Recently on the shared TV (both of them, incl. the partner's own shows):" if len(PEOPLE) == 2 else
                     "Recently on the shared TV (everyone in the household):")
    for label, sig in (("  Loved (favourites)", ("loved",)), ("  Liked", ("liked",)), ("  Watched", ("watched",)),
                       ("  Started recently (not finished)", ("interest",)), ("  Only opened, never started", ("opened",)),
                       (f"  Gave up on ({you} lost interest, avoid similar)", ("abandoned",)),
                       ("  Disliked (thumbs down, avoid similar)", ("disliked",))):
        if tv and names(sig, ("tv", "both")):
            lines.append(f"{label}: {names(sig, ('tv', 'both'))}")
    remote = [e for e in p["entries"] if e.get("who") == "remote"]
    for person in PEOPLE:
        for signal in ("loved", "liked", "watched", "disliked"):
            titles = [e["title"] for e in remote if person in e["by"] and e["signal"] == signal]
            if titles:
                lines.append(f"{LABELS[person]} explicitly {signal}: " + "; ".join(titles[:15]))
    top = sorted(((w, g) for g, w in p["genres"].items() if w > 0), reverse=True)[:6]
    if top:
        lines.append("Strongest genres: " + ", ".join(g for _, g in top))
    if p["searches"]:
        lines.append("Recent searches (what they looked for): " + ", ".join(p["searches"][-10:]))
    return "\n".join(lines)


def taste_hash(name, items):
    """Changes only when a title's taste signal changes (not on every progress tick while watching)."""
    now = time.time()
    signal = sorted((it["id"], classify(it, now)[0]) for it in items if name != "household" or attribute(it)[0] != GUEST)
    if name == "household":
        signal.append(recent_searches())
        if "accounts" in CONFIG["connectors"].get("trakt_api", {}):
            signal.append(sorted((it["person"], it["id"], it.get("signal"), it.get("weight")) for it in read_trakt()))
        else:
            signal.append(sorted((it["id"], it.get("signal")) for it in read_trakt()))
    signal.append(read_taste().get(name))
    if name == "household":
        signal.append(read_taste().get("people"))
        if feedback := feedback_hash(now):  # taste remote: only folded taste weights invalidate stamps.
            signal.append(feedback)
        if imported := import_hash(now):
            signal.append(imported)
        if attributed := attrib_hash(items):
            signal.append(attributed)
    return hashlib.sha1(json.dumps(signal).encode()).hexdigest()[:8]


def episodes_hash(items):
    return hashlib.sha1(json.dumps(sorted((it["id"], it.get("last_episode")) for it in items
                                          if it["type"] == "series")).encode()).hexdigest()[:8]


def refresh_profiles():
    """Rebuild the taste profiles whose inputs changed; returns the current hashes."""
    hist = read_history()
    hashes = {"episodes": episodes_hash([it for it in hist if it.get("profile", "household") == "household"])}
    active = active_profiles()
    for name in ("household", *ARCHIVED):
        if name in active:
            continue
        with _feeds_lock:
            stale = [k for k in _feeds if k[0] == rid(name)]
            for key in stale:
                _feeds.pop(key)
        if _profiles.pop(name, None) or stale:
            print(f"profile {name}: archived", flush=True)
    for name in active:
        items = [it for it in hist if it.get("profile", "household") == name or
                 name == "household" and CONFIG["history"]["source"] in HISTORY_ADAPTERS and it.get("profile") in PEOPLE]
        h = taste_hash(name, items)
        hashes[name] = h
        if (_profiles.get(name) or {}).get("hash") == h:
            continue
        prof = build_profile(items, recent_searches() if name == "household" else [],
                             read_trakt() if name == "household" else (), remote=name == "household")
        prof.update(hash=h, likes=(read_taste().get(name) or {}).get("likes", ""))
        _profiles[name] = prof
        print(f"profile {name}: {len(prof['entries'])} titles, hash {h}", flush=True)
    if "household" in _profiles:
        learn_from_plays(_profiles["household"])
        log_play_context(_profiles["household"])
    return hashes


# --- learning from plays (A) ------------------------------------------------------
# Every row title Odin actually fetched counts as shown (first LEARN_VISIBLE positions, once per day).
# A later play on the shared TV credits every row that showed it in the LEARN_WINDOW days before.
# Titles shown on LEARN_TIRED days without a play sink to the end of their row and leave the AI rows.

LEARN_ROWS = {rid('foryou'), rid('upcoming'), rid('catchup'), rid('rewatch'), rid('because'), rid('top10'), rid('week'),
              rid('leap')} | set(ROW_OWNER) | set(DOCS) | set(STUDIO_ROWS)
LEARN_VISIBLE = 20
LEARN_WINDOW = 21
LEARN_TIRED = 6
_learn = {"served": {}, "rows": {}, "hits": [], "credited": {}, "lanes": {}, "ctx": [], "moodpicks": {}}
CTX_MAX = 500
_learn_lock = threading.Lock()
_learn_dirty = threading.Event()


def row_name(cid, lang, fallback=None, metas=None, key="title"):
    names = getattr(metas, "names", feed(cid, lang).get("names") or {})
    name = names.get(key) or (names.get("mixed") if key == "title" else None) or fallback
    return name.get("tv") or name.get("movie") or fallback if isinstance(name, dict) else name


def learn_id(cid, lang, metas=None):
    """Learning key of a served row: lanes learn per theme ('lane:k:9840'), not per slot."""
    if cid in LANES:
        lane = row_name(cid, lang, metas=metas, key="lane")
        return f"lane:{lane}" if lane else None
    if cid in MOODS_ROWS:  # moods learn per mood, whichever shelf shows it
        mood = (feed(cid, lang).get("names") or {}).get("mood")
        return f"mood:{mood}" if mood else None
    return cid if cid in LEARN_ROWS else None


def note_served(cid, metas):
    if not cid:
        return
    day = local_now().strftime("%Y-%m-%d")
    with _learn_lock:
        for pos, m in enumerate(metas[:LEARN_VISIBLE]):
            rec = _learn["served"].setdefault(m["id"], {"name": m.get("name", ""), "rows": {}})
            r = rec["rows"].setdefault(cid, {"first": time.time(), "days": []})
            if day not in r["days"]:
                r["days"] = (r["days"] + [day])[-30:]
                row = _learn["rows"].setdefault(cid, {"shown": 0, "hits": 0})
                row["shown"] += 1
                _learn_dirty.set()
    note_exposure(cid, metas)


def learn_from_plays(p):
    """Credit rows (once per title) for titles really played on the shared TV within LEARN_WINDOW days after a row
    showed them; when several rows showed it, they share the credit (1/k each)."""
    note_tv(p)
    with _learn_lock:
        for e in p["entries"]:
            at = (e["item"].get("at") or 0) / 1000 if e.get("who") in ("tv", "both") and real_play(e) else 0
            rec = _learn["served"].get(e["key"])
            if not at or not rec or e["key"] in _learn["credited"]:
                continue
            day = local_now(at).date()
            rows = [cid for cid, r in rec["rows"].items()
                    if any(0 <= (day - datetime.date.fromisoformat(d)).days <= LEARN_WINDOW for d in r["days"])]
            if not rows:
                continue
            _learn["credited"][e["key"]] = at
            share = round(1 / len(rows), 3)
            for cid in rows:
                _learn["rows"].setdefault(cid, {"shown": 0, "hits": 0})["hits"] += share
                _learn["hits"] = (_learn["hits"] + [{"key": e["key"], "title": e["title"], "row": cid, "at": int(at),
                                                     "share": share, "days_shown": len(rec["rows"][cid]["days"])}])[-200:]
            print(f"learn: {e['title']} played after {', '.join(rows)}", flush=True)
            _learn_dirty.set()


def day_part(d):
    h = d.hour
    return "late night" if h < 5 or h >= 22 else "morning" if h < 12 else "afternoon" if h < 17 else "evening"


def year_season(d):
    return ("winter", "spring", "summer", "autumn")[d.month % 12 // 3]


def log_play_context(p):
    """Append every real shared-TV play once (title + time it was last played): weekday, part of day, season,
    the rows that showed it and that evening's mood shelves. Free, and the raw material for learning 'when'."""
    with _learn_lock:
        have = {(c["key"], c["at"]) for c in _learn["ctx"]}
        for e in p["entries"]:
            at = int((e["item"].get("at") or 0) / 1000) if e.get("who") in ("tv", "both") and real_play(e) else 0
            if not at or (e["key"], at) in have:
                continue
            d = local_now(at)
            lday = (d - datetime.timedelta(hours=5)).date()
            rec = _learn["served"].get(e["key"]) or {"rows": {}}
            rows = sorted(cid for cid, r in rec["rows"].items()
                          if any(0 <= (lday - datetime.date.fromisoformat(x)).days <= LEARN_WINDOW for x in r["days"]))
            tags = frec(e["key"], e["kind"]).get("x") or {}
            _learn["ctx"].append({
                "key": e["key"], "title": e["title"], "kind": e["kind"], "at": at, "weekday": lday.strftime("%A"),
                "hour": d.hour, "part": day_part(d), "season": year_season(d), "rows": rows,
                "moods": (lambda r: r.get("shown") or r.get("picks", []))(_learn["moodpicks"].get(lday.isoformat()) or {}),
                "x": [x for x, _ in sorted(tags.items(), key=lambda kv: -kv[1])][:3]})
            have.add((e["key"], at))
            _learn_dirty.set()
        _learn["ctx"] = sorted(_learn["ctx"], key=lambda c: c["at"])[-CTX_MAX:]


def tired_keys(cids=None):
    """Titles shown on many days without being played: the household is not interested (right now)."""
    played = set(_learn["credited"])
    with _learn_lock:
        return {k: rec["name"] for k, rec in _learn["served"].items() if k not in played and
                max((len(r["days"]) for c, r in rec["rows"].items() if cids is None or c in cids), default=0) >= LEARN_TIRED}


def hit_titles(n=12):
    with _learn_lock:
        seen, out = set(), []
        for h in reversed(_learn["hits"]):
            if h["key"] not in seen:
                seen.add(h["key"])
                out.append(f"{h['title']} (from {h['row'].split('.')[-1]})")
        return out[:n]


def save_learn():
    save_online()
    if not _learn_dirty.is_set():
        return
    _learn_dirty.clear()
    cutoff = (local_now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    with _learn_lock:
        for k in [k for k, rec in _learn["served"].items()
                  if max((r["days"][-1] for r in rec["rows"].values() if r["days"]), default="") < cutoff]:
            _learn["served"].pop(k)
        snap = json.dumps(_learn, ensure_ascii=False)
    try:
        with open(os.path.join(DATA, "learn.json.tmp"), "w") as fh:
            fh.write(snap)
        os.replace(os.path.join(DATA, "learn.json.tmp"), os.path.join(DATA, "learn.json"))
    except OSError:
        pass


def learn_stats():
    with _learn_lock:
        rows = {cid: {**r, "hit_rate": round((r["hits"] + 1) / (r["shown"] + 2), 3)} for cid, r in _learn["rows"].items()}
        hits = _learn["hits"][-10:]
        ctx = _learn["ctx"][-8:]
        picks = dict(sorted(_learn["moodpicks"].items())[-5:])
        n_ctx = len(_learn["ctx"])
    return {"rows": rows, "recent_hits": hits, "tracked": len(_learn["served"]), "tired": len(tired_keys()),
            "plays_logged": n_ctx, "recent_plays": ctx, "moodpicks": picks, "online": online_summary()}


# --- online metrics (W11a) ------------------------------------------------------------

ONLINE_DAYS = 35
ONLINE_TOP = 12
ONLINE_ACTIVE_H = 3
ONLINE_ABANDON_H = 48
ONLINE_TV_MAX = 3000
_online = None
_online_lock = threading.RLock()
_online_save_lock = threading.Lock()
_online_dirty = threading.Event()
_online_meta = {"errors": 0, "corrupt": False, "skipped": 0, "unmapped": 0}
_online_index = {}
_online_pruned = None
_online_error_at = None


def online_day(t):
    d = local_now(t) - datetime.timedelta(hours=5)
    return d.date().isoformat(), d.hour


def okey(key, kind):
    return key + "@" + kind


def online_error(e):
    global _online_error_at
    with _online_lock:
        _online_meta["errors"] += 1
        now = time.time()
        if _online_error_at is None or now - _online_error_at >= 3600:
            _online_error_at = now
            print(f"online metrics: {type(e).__name__}", flush=True)


def online_valid(s):
    """Validate the persisted indices and shapes before any serving hook uses them."""
    def stamp(t):
        return type(t) in (int, float) and math.isfinite(t) and t >= 0
    if s["v"] != 1 or (s["since"] is not None and not stamp(s["since"])):
        raise ValueError("online version/since")
    if "obs_floor" in s and not stamp(s["obs_floor"]):
        raise ValueError("online observation floor")
    if not isinstance(s["keys"], list) or len(set(s["keys"])) != len(s["keys"]):
        raise ValueError("online keys")
    for k in s["keys"] + [r[1] for r in s["tv"]]:
        if not isinstance(k, str) or not re.fullmatch(r"[^@\s]+@(movie|tv|unknown)", k):
            raise ValueError("online key")
    for sig, raw in s["lists"].items():
        if not re.fullmatch(r"[0-9a-f]{16}", sig) or not isinstance(raw, str):
            raise ValueError("online list")
        ix = [int(i) for i in raw.split()]
        if len(ix) > LEARN_VISIBLE or any(i < 0 or i >= len(s["keys"]) for i in ix):
            raise ValueError("online index")
    for ld, rows in s["exp"].items():
        datetime.date.fromisoformat(ld)
        for lid, lists in rows.items():
            if not isinstance(lid, str):
                raise ValueError("online row")
            for sig, rec in lists.items():
                if sig not in s["lists"] or not isinstance(rec, list) or len(rec) < 3 or len(rec) % 2 != 1:
                    raise ValueError("online exposure")
                if type(rec[0]) is not int or rec[0] < 1 or len(set(rec[1::2])) != len(rec[1::2]):
                    raise ValueError("online fetches")
                if any(type(h) is not int or not 0 <= h < 24 for h in rec[1::2]) or not all(map(stamp, rec[2::2])):
                    raise ValueError("online hour/time")
    for ld, rec in s["act"].items():
        datetime.date.fromisoformat(ld)
        if len(rec) != 3 or type(rec[0]) is not int or not 0 < rec[0] < 1 << 24 or not all(map(stamp, rec[1:])) or rec[1] > rec[2]:
            raise ValueError("online activity")
    for r in s["tv"]:
        if len(r) != 3 or not stamp(r[0]) or type(r[2]) is not int or r[2] not in (0, 1):
            raise ValueError("online observation")
    for iid, r in s["obs"].items():
        if not isinstance(iid, str) or len(r) != 4 or not stamp(r[0]) or type(r[1]) is not bool or not all(map(stamp, r[2:])):
            raise ValueError("online baseline")


def online_state():
    global _online, _online_index, _online_pruned
    with _online_lock:
        if _online is None:
            path = os.path.join(DATA, "online.json")
            try:
                with open(path) as fh:
                    loaded = json.load(fh)
                online_valid(loaded)
                _online = loaded
            except FileNotFoundError:
                pass
            except Exception as e:
                _online_meta["corrupt"] = True
                online_error(e)
                try:
                    os.replace(path, path + ".bad")
                except OSError as e:
                    online_error(e)
            if _online is None:
                _online = {"v": 1, "since": None, "keys": [], "lists": {}, "exp": {}, "act": {}, "tv": [], "obs": {}}
            _online_index = {k: i for i, k in enumerate(_online["keys"])}
            _online_pruned = None
        return _online


def note_exposure(lid, metas, t=None):
    try:
        if not lid:
            return
        t = time.time() if t is None else t
        ld, lh = online_day(t)
        keys = [okey(m["id"], "unknown" if m["type"] == "unknown" else "movie" if m["type"] == "movie" else "tv")
                for m in metas[:LEARN_VISIBLE]]
        sig = hashlib.blake2s("\n".join(keys).encode(), digest_size=8).hexdigest()
        with _online_lock:
            s = online_state()
            if s["since"] is None:
                s["since"] = t
            if sig not in s["lists"]:
                for k in keys:
                    if k not in _online_index:
                        _online_index[k] = len(s["keys"])
                        s["keys"].append(k)
                s["lists"][sig] = " ".join(str(_online_index[k]) for k in keys)
            rec = s["exp"].setdefault(ld, {}).setdefault(lid, {}).setdefault(sig, [0])
            rec[0] += 1
            if lh not in rec[1::2]:
                rec.extend((lh, int(t)))
            _online_dirty.set()
    except Exception as e:
        online_error(e)


def online_activity(s, t):
    ld, lh = online_day(t)
    rec = s["act"].setdefault(ld, [0, int(t), int(t)])
    rec[0] |= 1 << lh
    rec[1] = min(rec[1], int(t))
    rec[2] = max(rec[2], int(t))


def note_tv(p, items=None, now=None):
    try:
        now = time.time() if now is None else now
        items = read_history() if items is None else items
        mapped = {e["item"]["id"]: (e["key"], e["kind"]) for e in p["entries"]
                  if "profile" in e.get("item", {})}
        times = [t for t, _ in list(_searches)]
        # Folded feedback is a lower bound: undone/superseded actions are no longer available.
        feedback = feedback_state()
        times.extend(e["t"] for field in ("active", "tonight") for e in feedback[field].values())
        windows = globals().get("session_windows")
        if callable(windows):
            times.extend(w["t"] for w in windows())
        with _online_lock:
            s = online_state()
            for it in items:
                if it.get("profile", "household") != "household":
                    continue
                if not it.get("at"):
                    _online_meta["skipped"] += 1
                    continue
                at = it["at"] / 1000
                if at > now:
                    continue
                iid = it["id"]
                prior = s["obs"].get(iid)
                if prior and at <= prior[0]:
                    continue
                if prior is None and "obs_floor" in s and at <= s["obs_floor"]:
                    _online_meta["skipped"] += 1
                    continue
                pair = mapped.get(iid)
                if pair is None and iid.startswith("tmdb:"):
                    pair = iid, "movie" if it["type"] == "movie" else "tv"
                if pair is None:
                    _online_meta["unmapped"] += 1
                    continue
                played, eps, progress = bool(it.get("played")), it.get("episodes_played") or 0, it.get("progress") or 0
                if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in (at, eps, progress)):
                    raise ValueError("online TV numbers")
                old = prior or [0, False, 0, 0]
                real = (played and not old[1]) or (eps > old[2] if prior else eps >= 1) or (progress >= .25 and progress > old[3])
                s["tv"].append([at, okey(*pair), int(real)])
                s["obs"][iid] = [at, played, eps, progress]
                times.append(at)
            for t in times:
                if t <= now:
                    online_activity(s, t)
            if times or s["tv"]:
                _online_dirty.set()
    except Exception as e:
        online_error(e)


def save_online():
    global _online_pruned, _online_index
    try:
        with _online_save_lock:
            with _online_lock:
                if not _online_dirty.is_set():
                    return
                s = online_state()
                now = time.time()
                ld = online_day(now)[0]
                if _online_pruned != ld:
                    cutoff = (datetime.date.fromisoformat(ld) - datetime.timedelta(days=ONLINE_DAYS - 1)).isoformat()
                    for field in ("exp", "act"):
                        s[field] = {d: r for d, r in s[field].items() if d >= cutoff}
                    s["tv"] = sorted((r for r in s["tv"] if r[0] >= now - 120 * DAY), key=lambda r: r[0])[-ONLINE_TV_MAX:]
                    obs = sorted(s["obs"].items(), key=lambda kv: kv[1][0])
                    if len(obs) > 2000:
                        s["obs_floor"] = max(s.get("obs_floor", 0), obs[-2001][1][0])
                    s["obs"] = dict(obs[-2000:])
                    latest = {}
                    for rows in s["exp"].values():
                        for lists in rows.values():
                            for sig, rec in lists.items():
                                latest[sig] = max(latest.get(sig, 0), *rec[2::2])
                    used = set(sorted(latest, key=latest.get)[-2000:])
                    for rows in s["exp"].values():
                        for lid, lists in rows.items():
                            rows[lid] = {sig: r for sig, r in lists.items() if sig in used}
                    old_keys, keys, index, lists = s["keys"], [], {}, {}
                    for sig in used:
                        ix = []
                        for i in s["lists"][sig].split():
                            k = old_keys[int(i)]
                            if k not in index:
                                index[k] = len(keys)
                                keys.append(k)
                            ix.append(str(index[k]))
                        lists[sig] = " ".join(ix)
                    s.update(keys=keys, lists=lists)
                    _online_index, _online_pruned = index, ld
                snap = json.dumps(s, separators=(",", ":"), allow_nan=False)
                _online_dirty.clear()
            path = os.path.join(DATA, "online.json")
            with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(snap)
            os.replace(path + ".tmp", path)
    except Exception as e:
        _online_dirty.set()
        online_error(e)


def online_wilson(k, n):
    # Repeat-day credit can exceed title-days; that ratio has no binomial interval.
    if not n or not 0 <= k <= n:
        return None
    p, z2 = k / n, 1.96 ** 2
    mid = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = 1.96 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return [round(max(0, mid - half), 3), round(min(1, mid + half), 3)]


def online_quantiles(values):
    if not values:
        return None
    v = sorted(values)
    return {"n": len(v), **{name: round(v[math.ceil(q * len(v)) - 1], 1)
                            for name, q in (("p25", .25), ("p50", .5), ("p75", .75))}}


def online_cell(t):
    d = local_now(t)
    part = day_part(d)
    wd = (d - datetime.timedelta(hours=5)).weekday()
    weekend = wd in ((4, 5) if part in ("evening", "late night") else (5, 6))
    return ("weekend" if weekend else "weeknight" if part in ("evening", "late night") else "weekday") + "/" + part


def metrics(now=None, days=LEARN_WINDOW):
    """Counts and Wilson 95% intervals; time-to-play is an upper bound (export lag ≤30 min)."""
    if type(days) is not int or days < 1 or days > ONLINE_DAYS:
        raise ValueError(f"days must be 1..{ONLINE_DAYS}")
    now = time.time() if now is None else now
    end = online_day(now)[0]
    start = (datetime.date.fromisoformat(end) - datetime.timedelta(days=days - 1)).isoformat()
    before = (datetime.date.fromisoformat(start) - datetime.timedelta(days=LEARN_WINDOW)).isoformat()
    with _online_lock:
        s = online_state()
        since, keys, lists = s["since"], list(s["keys"]), dict(s["lists"])
        exp = {ld: {lid: {sig: list(r) for sig, r in ls.items()} for lid, ls in rows.items()}
               for ld, rows in s["exp"].items() if before <= ld <= end}
        act = {ld: list(r) for ld, r in s["act"].items() if before <= ld <= end}
        tv, meta = [list(r) for r in s["tv"]], dict(_online_meta)
    out = {"window": {"days": days, "from": start, "to": end,
                       "since": local_now(since).strftime("%Y-%m-%d %H:%M") if since is not None else None},
           "fetches": 0, "exposures": 0, "impressions": 0, "title_days": 0, "titles": 0,
           "tv": {"real": 0, "opens": 0}, "plays": {"n": 0, "pre_since": 0, "credited": 0, "hit": {}},
           "rows": {}, "clicks": 0, "ttp_min": None, "abandon": {"k": 0, "n": 0, "pending": 0, "ci": None},
           "context": {}, "days": {}, "meta": meta}
    who = globals().get("who_watching") if session_windows() else None
    if callable(who):
        out["who"] = {}
    def cell(group, name):
        return out[group].setdefault(name, {"impressions": 0, "plays": 0, "credited": 0})
    def row(lid):
        return out["rows"].setdefault(lid, {"impressions": 0, "title_days": 0, "credit": 0, "ci": None})
    def count(t, field):
        cell("context", online_cell(t))[field] += 1
        if callable(who):
            cell("who", who(t)[0])[field] += 1
    parsed = {sig: [keys[int(i)] for i in raw.split()] for sig, raw in lists.items()}
    shown, title_days = {}, set()
    for ld, rows in exp.items():
        mask = act.get(ld, [0])[0]
        for lid, ls in rows.items():
            for sig, r in ls.items():
                pairs = [(h, t) for h, t in zip(r[1::2], r[2::2]) if t <= now]
                if start <= ld:
                    out["fetches"] += r[0]
                    out["exposures"] += bool(pairs)
                qualified = [t for h, t in pairs if any(mask & (1 << a) for a in
                             range(max(0, h - ONLINE_ACTIVE_H), min(24, h + ONLINE_ACTIVE_H + 1)))]
                if not qualified:
                    continue
                t = min(qualified)
                if start <= ld:
                    out["impressions"] += 1
                    row(lid)["impressions"] += 1
                    count(t, "impressions")
                    title_days.update((ld, lid, k) for k in parsed[sig][:ONLINE_TOP])
                for pos, k in enumerate(parsed[sig]):
                    shown.setdefault(k, []).append((ld, lid, pos, t))
    for ld, lid, k in title_days:
        row(lid)["title_days"] += 1
    out["title_days"], out["titles"] = len(title_days), len({k for _, _, k in title_days})
    observations, real_times = {}, {}
    for at, k, real in tv:
        if at > now:
            continue
        ld = online_day(at)[0]
        if real:
            real_times.setdefault(k, []).append(at)
        if start <= ld <= end:
            token = k, ld, real
            observations[token] = min(at, observations.get(token, at))
    hits, ttp = {k: 0 for k in (5, 12, 20)}, []
    for (k, ld, real), at in observations.items():
        out["tv"]["real" if real else "opens"] += 1
        if real:
            count(at, "plays")
        eligible = since is not None and at >= since
        if real and not eligible:
            out["plays"]["pre_since"] += 1
        if real and eligible:
            out["plays"]["n"] += 1
        matches = [(d, lid, pos, t) for d, lid, pos, t in shown.get(k, []) if t <= at and
                   0 <= (datetime.date.fromisoformat(ld) - datetime.date.fromisoformat(d)).days <= LEARN_WINDOW] if eligible else []
        credited = {lid for _, lid, pos, _ in matches if pos < ONLINE_TOP}
        if real and eligible:
            for depth in hits:
                hits[depth] += any(pos < depth for _, _, pos, _ in matches)
            if credited:
                out["plays"]["credited"] += 1
                count(at, "credited")
                for lid in credited:
                    row(lid)["credit"] += 1 / len(credited)
        elif not real and credited:
            out["clicks"] += 1
        same_day = [t for d, _, pos, t in matches if d == ld and pos < ONLINE_TOP]
        if credited and same_day:
            ttp.append((at - min(same_day)) / 60)
        if not real:
            abandoned = out["abandon"]
            if now - at < ONLINE_ABANDON_H * 3600:
                abandoned["pending"] += 1
            else:
                abandoned["n"] += 1
                abandoned["k"] += not any(at < t <= at + ONLINE_ABANDON_H * 3600 for t in real_times.get(k, []))
    for depth, k in hits.items():
        n = out["plays"]["n"]
        out["plays"]["hit"][str(depth)] = [k, n, *online_wilson(k, n)] if n else None
    for r in out["rows"].values():
        r["credit"] = round(r["credit"], 3)
        r["ci"] = online_wilson(r["credit"], r["title_days"])
    out["ttp_min"] = online_quantiles(ttp)
    a = out["abandon"]
    a["ci"] = online_wilson(a["k"], a["n"])
    active = [r for ld, r in act.items() if start <= ld <= end and r[1] <= now]
    out["days"] = {"active": len(active), "span_min": online_quantiles([(min(now, r[2]) - r[1]) / 60 for r in active])}
    windows = globals().get("session_windows")
    if callable(who) and callable(windows):
        minutes = [(min(now, w["until"]) - w["t"]) / 60 for w in windows()
                   if start <= online_day(w["t"])[0] <= end and w["t"] <= now]
        q = online_quantiles(minutes)
        out["who"]["sessions"] = {"n": len(minutes), "min": {k: v for k, v in q.items() if k != "n"} if q else None}
    return out


def online_summary():
    try:
        with _online_lock:
            s = online_state()
            return {"since": online_day(s["since"])[0] if s["since"] is not None else None,
                    "days": len(s["exp"]), "fetches": sum(r[0] for rows in s["exp"].values() for ls in rows.values() for r in ls.values()),
                    "exposures": sum(len(ls) for rows in s["exp"].values() for ls in rows.values()),
                    "tv": {"real": sum(r[2] for r in s["tv"]), "opens": sum(not r[2] for r in s["tv"])},
                    "errors": _online_meta["errors"], "corrupt": _online_meta["corrupt"]}
    except Exception as e:
        online_error(e)
        return {"since": None, "days": 0, "fetches": 0, "exposures": 0, "tv": {"real": 0, "opens": 0},
                "errors": _online_meta["errors"], "corrupt": _online_meta["corrupt"]}


# --- taste fingerprints (B) -----------------------------------------------------------
# One small embedding per title (plot, genres, keywords), computed once and kept in /data/emb.json.
# The household's taste vector is the weighted mean of what they watched (minus what they dropped,
# plus what they played from a row); candidates are ranked by similarity before any LLM sees them.

EMB_MODEL = "openai/text-embedding-3-small"
EMB_DIM = 256
_emb = {}
_emb_kinds = {}
_emb_lock = threading.Lock()
_emb_dirty = threading.Event()


def title_text(kind, tid):
    det = tmdb(f"{kind}/{tid}", language="en-US", append_to_response="keywords,credits")  # same URL as facet_record
    kw = det.get("keywords") or {}
    words = [k["name"] for k in (kw.get("keywords") or kw.get("results") or [])][:15]
    year = (det.get("release_date") or det.get("first_air_date") or "")[:4]
    return (f"{det.get('title') or det.get('name')} ({year}), {'movie' if kind == 'movie' else 'series'}. "
            f"Genres: {', '.join(g['name'] for g in det.get('genres') or [])}. Keywords: {', '.join(words)}. "
            f"{(det.get('overview') or '')[:600]}")


def ensure_emb(pairs):
    """pairs: [(key 'tmdb:1', kind 'movie'|'tv')]; embeds the missing ones. Returns how many are available."""
    if not OR_KEY:
        return 0
    with _emb_lock:
        missing = list(dict.fromkeys((k, kind) for k, kind in pairs if femb(k, kind) is None))
    texts = [(k, kind, _pool.submit(title_text, kind, k[5:])) for k, kind in missing]
    ready = []
    for k, kind, job in texts:
        try:
            ready.append((k, kind, job.result()))
        except Exception:
            continue
    for i in range(0, len(ready), 96):
        batch = ready[i:i + 96]
        j = openrouter("/api/v1/embeddings", {"model": EMB_MODEL, "input": [t for _, _, t in batch], "dimensions": EMB_DIM},
                       60, "embed", "titles")
        with _emb_lock:
            for (k, kind, _), d in zip(batch, sorted(j["data"], key=lambda d: d["index"])):
                v = d["embedding"]
                n = sum(x * x for x in v) ** 0.5 or 1.0
                slot = fkey(k, kind)
                if _emb_kinds.get(slot) not in (None, kind):
                    slot = f"{k}@{kind}"
                _emb[slot] = array('f', (x / n for x in v))
                _emb_kinds[slot] = kind
        _emb_dirty.set()
        clear_seed_cache()
    with _emb_lock:
        return sum(femb(k, kind) is not None for k, kind in pairs)


def save_emb():
    if not _emb_dirty.is_set():
        return
    _emb_dirty.clear()
    with _emb_lock:
        snap = {k: base64.b64encode(struct.pack(f"{EMB_DIM}e", *v)).decode() for k, v in _emb.items()}
        kinds = {k: kind for k, kind in _emb_kinds.items() if k in snap}
    try:
        with open(os.path.join(DATA, "emb.json.tmp"), "w") as fh:
            json.dump({"model": EMB_MODEL, "dim": EMB_DIM, "v": snap, "kinds": kinds}, fh)
        os.replace(os.path.join(DATA, "emb.json.tmp"), os.path.join(DATA, "emb.json"))
    except OSError:
        pass


def load_emb():
    try:
        with open(os.path.join(DATA, "emb.json")) as fh:
            raw = json.load(fh)
        if raw.get("model") == EMB_MODEL and raw.get("dim") == EMB_DIM:
            _emb.update({k: array('f', struct.unpack(f"{EMB_DIM}e", base64.b64decode(b))) for k, b in raw["v"].items()})
            for k in raw["v"]:
                kind = (raw.get("kinds") or {}).get(k) or (_facets.get(k) or {}).get("kind")  # Stored slots have no request kind.
                if kind in ("movie", "tv"):
                    _emb_kinds[k] = kind
    except Exception:
        pass
    finally:
        clear_seed_cache()


def entry_weight(e):
    """One title's evidence; long runs replace flat binge/comfort boosts with logarithmic growth."""
    w = e["weight"]
    eps = e.get("item", {}).get("episodes_played") or 0
    if w > 0 and eps > 0:
        if e.get("who") == "owner" or e.get("wsrc") == "trakt":
            w = max(.05, w - .5 * sum(f in e.get("flags", ()) for f in ("binge", "comfort")))
        w *= 1 + .1 * math.log1p(eps)
    return w


def taste_vector(p, min_titles=5):
    """Weighted mean of their titles' fingerprints; None until enough titles are embedded."""
    try:
        ensure_emb([(e["key"], e["kind"]) for e in p["entries"][:400]])
    except BudgetExceeded:
        pass
    hits = {h["key"] for h in _learn["hits"]} if HIT_TASTE and p.get("learned", True) else set()
    acc, n = [0.0] * EMB_DIM, 0
    with _emb_lock:
        for e in p["entries"]:
            v = femb(e["key"], e["kind"])
            if not v:
                continue
            # e["weight"] already carries recency (build_profile / trakt import): no second decay here
            w = entry_weight(e) * (0.5 if e["weight"] < 0 else 1.0) + (2.0 if e["key"] in hits else 0.0)
            acc = [a + w * x for a, x in zip(acc, v)]
            n += e["weight"] > 0
    if n < min_titles:
        return None
    norm = sum(x * x for x in acc) ** 0.5 or 1.0
    return [x / norm for x in acc]


def fit(vec, key, kind=None):
    v = femb(key, kind)
    return sum(a * b for a, b in zip(vec, v)) if vec and v else None


# --- taste facets (C) -------------------------------------------------------------------
# What a title is made of, beyond its plot: TMDB keywords ("based on true story", "time travel"),
# directors/creators, the top cast, networks and studios, genres, and for anime the AniList tags and
# source. Kept per title in /data/facets.json. Affinity per facet = how much more often it shows up in
# what they loved than in everything we know (with minimum support), so one-off actors stay noise.
# Score weights are selected by offline replay; creators and studios rise through loved titles' facets.

FACET_W = {"k": 1.0, "d": 1.3, "a": 0.8, "n": 0.9, "c": 0.6, "g": 0.25, "t": 0.8, "s": 0.4, "x": 1.2}
FACET_SUPPORT = {"k": 2, "d": 2, "a": 3, "n": 2, "c": 3, "g": 3, "t": 2, "s": 3, "x": 2}
X_PRIOR = 20
# Experiences: what it feels like to watch a title, not what it is about. One batched LLM call tags 25 titles
# (once per title, kept in facets.json as "x"); they then count like any other facet. id: (reason, row title).
EXPERIENCES = {
    "cozy_case": ("cozy case of the week", "Cozy Case-of-the-Week"),
    "banter_duo": ("witty banter duo", "Witty Banter Duos"),
    "will_they": ("will-they-won't-they", "Will They, Won't They"),
    "slow_romance": ("slow-burn romance", "Slow-Burn Romance"),
    "period_society": ("period romance & society", "Ballrooms & Period Romance"),
    "forbidden_love": ("love against the odds", "Love Against the Odds"),
    "found_family": ("found family", "Found Family"),
    "mentor_bond": ("unlikely mentor bond", "Unlikely Mentors"),
    "second_chance": ("second chances", "Second Chances"),
    "tearjerker": ("a good cry", "For a Good Cry"),
    "grief_healing": ("grief & healing", "Grief & Healing"),
    "heartwarming": ("heartwarming", "Heartwarming Picks"),
    "coming_of_age": ("coming of age", "Coming of Age"),
    "underdog": ("underdog rising", "Underdogs Rising"),
    "rivalry_training": ("rivalry & training arc", "Rivals & Training Arcs"),
    "epic_quest": ("epic quest", "Epic Quests"),
    "magic_wonder": ("magic & wonder", "Magic & Wonder"),
    "team_spectacle": ("team-up spectacle", "Team-Up Spectacle"),
    "hero_burden": ("the hero's burden", "Heroes with a Burden"),
    "dynasty_power": ("family dynasty & power", "Dynasties & Power"),
    "frontier_grit": ("frontier grit", "Frontier Grit"),
    "political_intrigue": ("political intrigue", "Political Intrigue"),
    "war_sacrifice": ("war & sacrifice", "War & Sacrifice"),
    "antihero": ("antihero's descent", "Antiheroes"),
    "revenge": ("revenge", "Roads to Revenge"),
    "heist": ("heist & caper", "Heists & Capers"),
    "whodunit": ("twisty whodunit", "Twisty Whodunits"),
    "mind_bender": ("mind-bending puzzle", "Mind-Benders"),
    "dread": ("slow-burn dread", "Slow-Burn Dread"),
    "survival": ("survival against the odds", "Survival Stories"),
    "competence": ("experts at work", "Experts at Work"),
    "courtroom": ("courtroom drama", "Courtroom Drama"),
    "family_secrets": ("family secrets", "Family Secrets"),
    "big_ideas": ("big sci-fi ideas", "Big Sci-Fi Ideas"),
    "time_twist": ("time loops & travel", "Time Twists"),
    "dystopia": ("rebellion in a dark future", "Dark Futures"),
    "feel_good": ("feel-good laughs", "Feel-Good Laughs"),
    "dark_comedy": ("dark comedy", "Dark Comedy"),
    "slice_of_life": ("slice of life", "Slice of Life"),
    "true_story": ("inspiring true story", "Inspiring True Stories"),
    "passion_art": ("passion for the craft", "Passion for the Craft"),
    "sports_glory": ("sports glory", "Sports Glory"),
    "nostalgia": ("nostalgic comfort", "Nostalgic Comfort"),
}
TAG_SYS = ("You tag what it FEELS like to watch movies and TV series (the experience a viewer gets), not their plot topics. "
           "Allowed tags (id: meaning):\n{vocab}\n"
           "For each numbered title reply with exactly one line: N|id:strength,id:strength,... using 2 to 5 allowed ids, "
           "strength 3 = defines the title, 2 = clearly there, 1 = a touch. Judge the whole title as you know it; "
           "if you don't know it, judge from the description. No other text.")
TAG_BATCH = 25
KW_STOP = {"duringcreditsstinger", "aftercreditsstinger", "woman director", "sequel", "prequel", "anime", "3d",
           "live action and animation", "based on novel or book", "remake", "duringcreditsstinger ", "short film",
           "adult animation", "shounen", "seinen", "aftercreditsstinger "}
ANILIST_TAGS = ("{alias}:Page(perPage:1){{media(search:{q},type:ANIME,sort:[SEARCH_MATCH]){{title{{english romaji}} "
                "source tags{{name rank isGeneralSpoiler isMediaSpoiler}}}}}}")
# TMDB tone tags and broad keywords: they count for the score, but never name a reason or a lane.
KW_TONE = set("""absurd admiring aggressive amused angry anxious appreciative approving arrogant assertive audacious awestruck
bewildered bold calm cautionary celebratory cheerful comforting compassionate complex condescending defiant depressing
desperate didactic disheartening dramatic earnest embarrassed empathetic energetic enthusiastic excited exhilarated factual
familiar frantic frightened grim harsh hilarious hopeful horrified humorous informative inspirational intense ironic joyful
joyous lighthearted melancholy mischievous mysterious nostalgic optimistic playful powerful provocative reflective
respectful romantic sarcastic serious sincere suspenseful sympathetic tense thoughtful vibrant whimsical witty wry zany
sad tragic shocking""".split()) | {"critical", "enchant"}
KW_BROAD = {"miniseries", "3d animation", "new york city", "friendship", "revenge", "villain", "hero", "family", "love",
            "murder", "adventure", "fantasy", "tragedy", "loss of loved one", "coming of age", "dystopia", "magic", "death",
            "father son relationship", "mother daughter relationship", "husband wife relationship", "based on comic",
            "duringcreditsstinger", "violence", "teenager", "high school", "female protagonist", "male protagonist",
            "remake", "action hero", "post-apocalyptic future", "superhero", "super power", "fantasy world", "supernatural",
            "organized crime", "secret identity", "training", "journey", "travel", "future", "king", "orphan", "amnesia",
            "creature", "rivalry", "battle", "competition", "friends", "school", "student", "sequel", "spin off",
            "christianity", "religion", "god", "dying and death", "shootout", "psychopath", "mission", "military", "space",
            "spacecraft", "obsession", "philosophical", "child hero"}
_facets = {}
_facets_lock = threading.Lock()
_facets_dirty = threading.Event()
_model = {"sig": None, "m": None}
_model_lock = threading.Lock()


def fkey(key, kind):
    """Bare slot keeps its original kind; collisions use an alias."""
    rec = _facets.get(key) or {}  # Inspect the bare owner to choose a slot.
    owner = rec.get("kind") if rec else _emb_kinds.get(key)
    # Key-only APIs have no request kind and retain the bare slot.
    return f"{key}@{kind}" if kind and owner not in (None, kind) else key


def frec(key, kind):
    return _facets.get(fkey(key, kind)) or {}


def femb(key, kind):
    slot = fkey(key, kind)
    if kind and _emb_kinds.get(slot) not in (None, kind):
        slot = f"{key}@{kind}"
    return _emb.get(slot) if not kind or _emb_kinds.get(slot) in (None, kind) else None


def facet_record(kind, tid):
    det = tmdb(f"{kind}/{tid}", language="en-US", append_to_response="keywords,credits")
    kw = det.get("keywords") or {}
    credits = det.get("credits") or {}
    if kind == "movie":
        makers = [c for c in credits.get("crew") or [] if c.get("job") == "Director"][:2]
    else:
        makers = (det.get("created_by") or [])[:3]
    return {"k": [[k["id"], k["name"]] for k in (kw.get("keywords") or kw.get("results") or [])
                  if k["name"] not in KW_STOP][:20],
            "d": [[c["id"], c["name"]] for c in makers],
            "a": [[c["id"], c["name"]] for c in (credits.get("cast") or [])[:3]],
            "n": [[n["id"], n["name"]] for n in (det.get("networks") or [])[:3]],
            "c": [[c["id"], c["name"]] for c in (det.get("production_companies") or [])[:4]],
            "g": genre_names([g["name"] for g in det.get("genres") or []]),
            "lang": det.get("original_language"), "title": det.get("title") or det.get("name") or "",
            "y": (det.get("release_date") or det.get("first_air_date") or "")[:4], "kind": kind,
            "vote_average": det.get("vote_average"), "vote_count": det.get("vote_count"),
            "collection": (det.get("belongs_to_collection") or {}).get("id"),
            "o": (det.get("overview") or "")[:280]}


def anilist_tags(recs):
    """AniList tags and source for anime records, matched by title (10 per request); marks each as looked up."""
    for i in range(0, len(recs), 10):
        chunk = recs[i:i + 10]
        q = "{" + " ".join(ANILIST_TAGS.format(alias=f"a{j}", q=json.dumps(base_title(r["title"])))
                           for j, r in enumerate(chunk)) + "}"
        req = urllib.request.Request("https://graphql.anilist.co", json.dumps({"query": q}).encode(),
                                     {"Content-Type": "application/json", "Accept": "application/json",
                                      "User-Agent": "skald/" + VERSION})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)["data"]
        except Exception as e:
            print(f"anilist tags: {type(e).__name__}: {e}", flush=True)
            return
        for j, rec in enumerate(chunk):
            rec["al"] = 1
            base = base_title(rec["title"])
            for m in (data.get(f"a{j}") or {}).get("media") or []:
                names = [norm_title(m["title"].get(k)) for k in ("english", "romaji")]
                if len(base) < 3 or not any(base in n or n in base for n in names if n):
                    continue
                rec["t"] = [t["name"] for t in m.get("tags") or []
                            if (t.get("rank") or 0) >= 60 and not t.get("isGeneralSpoiler") and not t.get("isMediaSpoiler")][:8]
                if m.get("source"):
                    rec["s"] = m["source"]


def ensure_facets(pairs):
    """pairs: [(key 'tmdb:1', kind 'movie'|'tv')]; fetches the missing ones."""
    with _facets_lock:
        missing = list(dict.fromkeys((k, kind) for k, kind in pairs if not frec(k, kind)))
    jobs = [(k, kind, _pool.submit(facet_record, kind, k[5:])) for k, kind in missing]
    got = {}
    for k, kind, job in jobs:
        try:
            got[k, kind] = job.result()
        except Exception:
            continue
    anime = [r for r in got.values() if r.get("lang") in ("ja", "ko", "zh") and "Animation" in r["g"]]
    if anime:
        anilist_tags(anime)
    if got:
        with _facets_lock:
            for (k, kind), rec in got.items():
                _facets[fkey(k, kind)] = rec
        _facets_dirty.set()
        clear_seed_cache()
    return len(got)


_tagging = set()


def tag_experiences(pairs, max_batches=4):
    """Experience tags for known titles without tags, TAG_BATCH per LLM call. Worker only (seconds per call),
    never on a search. pairs: [(key, kind)], strongest first; returns how many titles got tags."""
    if not OR_KEY:
        return 0
    ensure_facets([(k, kind) for k, kind in pairs if fkey(k, kind) != k and not frec(k, kind)])
    with _facets_lock:
        todo = [(fkey(k, kind), kind) for k, kind in dict.fromkeys(pairs) if frec(k, kind) and "x" not in frec(k, kind)
                and fkey(k, kind) not in _tagging][:TAG_BATCH * max_batches]
        _tagging.update(k for k, _ in todo)
    done = 0
    try:
        old = [(k, kind) for k, kind in todo if "o" not in _facets[k]]  # records from before overviews were kept
        jobs = [(k, kind, _pool.submit(tmdb, f"{kind}/{k[5:].split('@')[0]}", language="en-US")) for k, kind in old]
        for k, kind, job in jobs:
            try:
                det = job.result()
            except Exception:
                det = {}
            with _facets_lock:
                if _facets[k].get("kind") in (None, kind):
                    _facets[k].update({"o": (det.get("overview") or "")[:280], "kind": kind,
                                       "y": (det.get("release_date") or det.get("first_air_date") or "")[:4]})
            clear_seed_cache()
        vocab = "\n".join(f"{x}: {d[0]}" for x, d in EXPERIENCES.items())
        for i in range(0, len(todo), TAG_BATCH):
            batch = [k for k, _ in todo[i:i + TAG_BATCH]]
            lines = []
            for j, k in enumerate(batch, 1):
                rec = _facets[k]
                kws = ", ".join(n for _, n in (rec.get("k") or [])[:8])
                extra = f"; tags: {', '.join(rec['t'][:5])}" if rec.get("t") else ""
                lines.append(f"{j}. {rec.get('title')} ({rec.get('y') or '?'}, {'movie' if rec.get('kind') == 'movie' else 'series'}; "
                             f"{'/'.join(rec.get('g') or [])}) keywords: {kws}{extra} | {rec.get('o') or ''}")
            text = llm_text(TAG_SYS.format(vocab=vocab), "\n".join(lines), 60 * len(batch) + 100, 90, "tags", "experience")
            got = {}
            for line in text.splitlines():
                mm = re.match(r"\s*(\d+)\s*[|.:)]\s*(.*)", line)
                if not mm or not 1 <= int(mm[1]) <= len(batch):
                    continue
                tags = {}
                for part in mm[2].split(","):
                    xs = [x.strip() for x in part.split(":")]
                    if len(xs) == 2 and xs[0] in EXPERIENCES and xs[1][:1] in ("1", "2", "3"):
                        tags[xs[0]] = int(xs[1][:1])
                if tags:
                    got[batch[int(mm[1]) - 1]] = tags
            with _facets_lock:
                for k, tags in got.items():  # a title the reply skipped is retried next time
                    _facets[k]["x"] = tags
            _facets_dirty.set()
            clear_seed_cache()
            done += len(got)
            print(f"tags: {len(got)}/{len(batch)} titles tagged", flush=True)
    except BudgetExceeded as e:
        print(f"tags: {e}", flush=True)
    except Exception as e:
        print(f"tags: {type(e).__name__}: {e}", flush=True)
    finally:
        with _facets_lock:
            _tagging.difference_update(k for k, _ in todo)
    return done


def facet_pairs(rec):
    """[(facet id, display name)] of one title."""
    out = [(f"{t}:{i}", n) for t in ("k", "d", "a", "n", "c") for i, n in rec.get(t) or []]
    out += [(f"g:{g}", g) for g in rec.get("g") or []]
    out += [(f"t:{t}", t) for t in rec.get("t") or []]
    if rec.get("s"):
        out.append((f"s:{rec['s']}", rec["s"].replace("_", " ").lower()))
    out += [(f"x:{x}", EXPERIENCES[x][0]) for x, st in (rec.get("x") or {}).items() if x in EXPERIENCES and st >= 2]
    return out


def save_facets():
    if not _facets_dirty.is_set():
        return
    _facets_dirty.clear()
    with _facets_lock:
        snap = json.dumps({"v": 1, "items": _facets}, ensure_ascii=False, separators=(",", ":"))
    try:
        with open(os.path.join(DATA, "facets.json.tmp"), "w") as fh:
            fh.write(snap)
        os.replace(os.path.join(DATA, "facets.json.tmp"), os.path.join(DATA, "facets.json"))
    except OSError:
        pass


def mixed_keys(data, facets):
    kinds = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            key = value.get('key') or value.get('id')
            kind = value.get('kind') or value.get('type')
            if isinstance(key, str) and key.startswith('tmdb:') and kind in ('movie', 'tv', 'series'):
                kinds.setdefault(key, set()).add('movie' if kind == 'movie' else 'tv')
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    for name in ('feeds', 'shown', 'history', 'trakt', 'profile'):
        try:
            with open(os.path.join(data, name + '.json')) as fh:
                visit(json.load(fh))
        except (OSError, ValueError):
            continue
    return [{'key': key, 'title': rec.get('title', ''), 'stored_kind': rec['kind'],
             'served_kinds': sorted(kinds[key])}
            for key, rec in sorted(facets.items()) if rec.get('kind') in ('movie', 'tv') and
            # shortcut: both-kind evidence cannot prove a mixture; add identity checks if needed.
            key in kinds and rec['kind'] not in kinds[key] and any(kind != rec['kind'] and
            facets.get(f'{key}@{kind}', {}).get('kind') != kind for kind in kinds[key])]


def load_facets():
    try:
        with open(os.path.join(DATA, "facets.json")) as fh:
            _facets.update(json.load(fh).get("items") or {})
        mixed = mixed_keys(DATA, _facets)
        for rec in mixed:
            k = rec["key"]
            _facets.pop(k, None)
            _emb.pop(k, None)
            _emb_kinds.pop(k, None)
        for k in _emb:
            kind = (_facets.get(k) or {}).get("kind")  # Stored slots have no request kind.
            if k not in _emb_kinds and kind in ("movie", "tv"):
                _emb_kinds[k] = kind
        if mixed:
            _facets_dirty.set()
            _emb_dirty.set()
            print(f"keys: dropped {len(mixed)} mixed facet records and their embeddings", flush=True)
    except Exception:
        pass
    finally:
        clear_seed_cache()


def speaks(fid, name):
    """Whether a facet can name a reason ('Christopher Nolan', 'Based on manga'), unlike tone tags or places."""
    return fid[0] != "k" or (name not in KW_TONE and name not in KW_BROAD and ", " not in name)


def nice_facet(fid, name):
    if fid[0] not in "kts":
        return name
    return re.sub(r"\((\w+)\)", lambda g: f"({g[1].upper()})", name[:1].upper() + name[1:])


def person_entries(p, person):
    """Their own titles plus what the household watches together on the TV."""
    seeds = {(title.casefold(), year, kind) for who, title, year, kind in person_loves() if who == person}
    overridden = {e["key"] for e in p["entries"] if e.get("who") == "remote" and person in e.get("by", ())}
    out = []
    for e in p["entries"]:
        by = e.get("by", ())
        seed = e["key"] not in overridden and any(title == e["title"].casefold() and kind == e["kind"]
                                                and (not year or year == e.get("year")) for title, year, kind in seeds)
        if person not in by and "together" not in by and not seed:
            continue
        own = (e.get("opinions") or {}).get(person)
        explicit = person in by or bool(own) or seed
        value = own or e.get("shared") or {"signal": e["signal"], "weight": e["weight"]}
        if person == "together" and "accounts" in CONFIG["connectors"].get("trakt_api", {}):
            value = {**value, **{k: e[k] for k in ("age", "genres", "title", "year")}}
        if seed and e.get("who") != "remote":
            value = {"signal": "loved", "weight": max(value["weight"], SEED_WEIGHT), "item": {}, "flags": ["told"]}
        # Shared passive plays are weaker than an opinion explicitly attributed to this person.
        out.append({**e, **value, "weight": value["weight"] * (1 if explicit or person == "together" else .35),
                    "explicit": explicit})
    return out


def facet_value(fid, p_, n_, npos, xtot, df, n_x, n_all, loose=False, person=False):
    """One facet's affinity; person evidence enters gradually against the catalog prior."""
    if npos < (1 if loose else FACET_SUPPORT[fid[0]]) and not n_:
        return None
    if fid[0] == "x":
        base = df / n_x
        share = (p_ + X_PRIOR * base) / (xtot + X_PRIOR) if person else p_ / (xtot or 1)
        lift = (share + 0.01) / (base + 0.01)
        if loose:
            return FACET_W["x"] * (max(0, min(1.5, math.log(lift))) * min(1, p_/2) - .5*n_/(p_+n_+1))
        return FACET_W["x"] * (max(-1.0, min(1.5, math.log(lift))) * min(1.0, npos / 4)
                               - 0.5 * n_ / (p_ + n_ + 1.0))
    idf = math.log(n_all / (df + 1)) / math.log(n_all)
    value = (p_ - n_) / (p_ + n_ + 1.0) * math.log1p(max(1, npos)) * max(idf, 0.05) * FACET_W[fid[0]]
    return value * min(1, npos / (2 * FACET_SUPPORT[fid[0]])) if person and not loose else value


def affinities(entries, store, df, n_all, hits, loose=False, person=False):
    """(facet affinity, positive count) over entries; loose lowers the support bar for a person's smaller history."""
    pos, neg, npos = {}, {}, {}
    xtot = 0.0
    for e in entries:
        rec = store.get(fkey(e["key"], e["kind"]))
        if not rec:
            continue
        w = entry_weight(e) + (1.5 if e["key"] in hits and not loose else 0)
        if w > 0 and rec.get("x"):
            xtot += w
        for fid, _ in facet_pairs(rec):
            if w > 0:
                pos[fid] = pos.get(fid, 0) + w
                npos[fid] = npos.get(fid, 0) + 1
            else:
                neg[fid] = neg.get(fid, 0) - w
    aff = {}
    n_x = sum(1 for r in store.values() if r.get("x")) or 1
    for fid in set(pos) | set(neg):
        value = facet_value(fid, pos.get(fid, 0), neg.get(fid, 0), npos.get(fid, 0), xtot,
                            df.get(fid, 0), n_x, n_all, loose, person)
        if value is not None:
            aff[fid] = value
    return aff, npos


REASON_FACETS = {"too dark": ("dark", "grim", "bleak", "dread", "dystopia", "antihero", "dark_comedy"),
                "too slow": ("slow", "slow_burn", "slow_romance", "dread", "slice_of_life"),
                "too violent": ("violent", "gore", "brutal", "revenge", "war_sacrifice", "frontier_grit"),
                "too silly": ("silly", "slapstick", "absurd", "feel_good")}


def experience_profile(entries):
    out = {}
    for e in entries:
        w = entry_weight(e) * (.5 if e["weight"] < 0 else 1)
        for tag, strength in (frec(e["key"], e["kind"]).get("x") or {}).items():
            out[tag] = out.get(tag, 0) + w * strength
    return out


QUALITY_PRIOR = False


def quality_prior(rec):
    """At most .02: votes are a weak prior, never a substitute for taste evidence."""
    if not QUALITY_PRIOR:
        return 0.0
    return .02 * max(0, min(10, (rec or {}).get("vote_average") or 0))/10 * min(1, math.log1p(max(0, (rec or {}).get("vote_count") or 0))/math.log1p(10000))


def build_model(p):
    """Taste model of the household: fingerprint centroid, facet affinities (also per person), the loved titles
    behind each facet (for reasons) and a match calibration."""
    global _seed_stats
    pos_entries = [e for e in p["entries"] if e["weight"] > 0]
    try:
        ensure_facets([(e["key"], e["kind"]) for e in p["entries"][:400]])
    except Exception as e:
        print(f"facets: {type(e).__name__}: {e}", flush=True)
    tag_experiences([(e["key"], e["kind"]) for e in pos_entries[:400]], max_batches=16)
    vec = taste_vector(p)
    hits = {h["key"] for h in _learn["hits"]} if HIT_TASTE else set()
    with _facets_lock:
        store = dict(_facets)
    df, names = {}, {}
    for rec in store.values():
        for fid, name in facet_pairs(rec):
            df[fid] = df.get(fid, 0) + 1
            names[fid] = name
    n_all = len(store) + 1
    _seed_stats = (df, n_all, {k: {fid for fid, _ in facet_pairs(rec)} for k, rec in store.items()})
    clear_seed_cache()
    aff, npos = affinities(p["entries"], store, df, n_all, hits)
    holders = {}
    for e in pos_entries:
        for fid, _ in facet_pairs(store.get(fkey(e["key"], e["kind"])) or {}):
            holders.setdefault(fid, []).append((e["weight"], e["key"]))
    holders = {fid: sorted(hs, reverse=True)[:40] for fid, hs in holders.items() if fid in aff}
    m = {"vec": vec, "aff": aff, "names": names, "npos": npos, "bscale": 1.0, "cal": None, "vbase": 0.0, "bg": [],
         "kinds": {e["key"]: e["kind"] for e in p["entries"]}, "holders": holders, "ent": {e["key"]: (e["title"], tuple(e.get("by") or ())) for e in pos_entries},
         "paff": {}, "pvec": {}, "px": {}, "pbscale": {}, "pbg": {}, "nexp": {}, "x": experience_profile(p["entries"])}
    active = active_feedback()
    for person in PEOPLE:
        mine = person_entries(p, person)
        if "accounts" in CONFIG["connectors"].get("trakt_api", {}):
            m.setdefault("panchors", {})[person] = {e["key"] for e in mine if e["weight"] > 0}
        m["nexp"][person] = sum(bool(e.get("explicit")) and e["weight"] > 0 for e in mine)
        m["paff"][person] = affinities(mine, store, df, n_all, hits, loose=m["nexp"][person] < 40, person=True)[0]
        m["px"][person] = experience_profile(mine)
        reason_aff, reason_x = {}, {}
        for e in mine:
            if e["weight"] >= 0:
                continue
            event = active.get((e["key"], person))
            reasons_ = event["reasons"] if event else ()
            rec = store.get(fkey(e["key"], e["kind"]), {})
            for reason in reasons_:
                fids = reason_fids(reason, rec)
                for fid in fids:
                    reason_aff[fid] = max(-.6, reason_aff.get(fid, 0) - .6 * min(1, abs(e["weight"])/3))
                    if fid.startswith("x:"):
                        reason_x[fid[2:]] = max(-3, reason_x.get(fid[2:], 0) - abs(e["weight"]))
        for fid, delta in reason_aff.items():
            m["paff"][person][fid] = m["paff"][person].get(fid, 0) + delta
        for tag, delta in reason_x.items():
            m["px"][person][tag] = m["px"][person].get(tag, 0) + delta
        memory = memory_load()
        if memory["facts"]:
            m.setdefault("paff0", {})[person] = dict(m["paff"][person])
            m["paff"][person] = memory_pseudo(person, m["paff"][person], time.time(), memory)
        with _emb_lock:
            embedded = sum(e["weight"] > 0 and femb(e["key"], e["kind"]) is not None for e in mine)
        seeds = mine if embedded >= 5 else [e for e in mine if e.get("explicit")]
        try:
            m["pvec"][person] = taste_vector({"entries": seeds, "learned": False}, min_titles=1)
        except Exception as e:
            print(f"pvec {person}: {type(e).__name__}: {e}", flush=True)
            m["pvec"][person] = None
        own, own0 = [], []
        paff0 = m.get("paff0", {}).get(person)
        changed = {fid for fid, a in m["paff"][person].items() if a != paff0.get(fid)} if paff0 is not None else set()
        for e in mine:
            if e["weight"] <= 0:
                continue
            rec = store.get(fkey(e["key"], e["kind"]))
            b = facet_bonus(m, rec, m["paff"][person])[0]
            if b > 0: own.append(b)
            if paff0 is not None:
                affected = changed and (any(fid in changed for fid, _ in facet_pairs(rec or {})) or
                                       any(f"x:{x}" in changed for x in (rec or {}).get("x") or {}))
                b0 = facet_bonus(m, rec, paff0)[0] if affected else b
                if b0 > 0: own0.append(b0)
        own.sort()
        m["pbscale"][person] = own[len(own)//2] if own else 1.0
        if paff0 is not None:
            own0.sort()
            m.setdefault("mem0", {})[person] = {"paff": paff0, "pbscale": own0[len(own0)//2] if own0 else 1.0, "pbg": []}

    own = [b for b in (facet_bonus(m, store.get(fkey(e["key"], e["kind"])))[0] for e in pos_entries[:150]) if b > 0]
    if own:
        m["bscale"] = sorted(own)[len(own) // 2] or 1.0
    # Calibration: the percentile among this week's ~80 trending titles (median ~70%, top ones 95+).
    try:
        bg = []
        for kind in ("movie", "tv"):
            for page in (1, 2):
                bg += [(f"tmdb:{it['id']}", kind) for it in tmdb(f"trending/{kind}/week", language="en-US", page=page).get("results", [])]
        ensure_facets(bg)
        tag_experiences(bg, max_batches=4)
        try:
            ensure_emb(bg)
        except BudgetExceeded:
            pass
        # Missing fingerprints contribute no evidence, matching replay blends; percentile uses the full score.
        m["bg"] = sorted(s for s in (score_key(m, k, kind=kind)[0] for k, kind in dict(bg).items() if k not in p["seen"]) if s is not None)
        if len(m["bg"]) >= 20:
            m["cal"] = len(m["bg"])
            for person in (*PEOPLE, "together"):
                m["pbg"][person] = sorted(s for k, kind in dict(bg).items() if k not in p["seen"]
                                          if (s := person_score(m, k, person, kind)[0]) is not None)
                if person in m.get("mem0", {}):
                    block = m["mem0"][person]
                    strict = {**m, **{name: {**m[name], person: block[name]} for name in ("paff", "pbscale")}}
                    block["pbg"] = sorted(s for k, kind in dict(bg).items() if k not in p["seen"]
                                          if (s := person_score(strict, k, person, kind)[0]) is not None)
    except Exception as e:
        print(f"calibration: {type(e).__name__}: {e}", flush=True)
    m["taste_line"] = taste_line(m)
    return m


def facet_bonus(m, rec, aff=None):
    """(bonus, [(contribution, facet id)]) of one title's facets under the model (or one person's affinities)."""
    if not rec:
        return 0.0, []
    aff = m["aff"] if aff is None else aff
    fids = {fid for fid, _ in facet_pairs(rec)}
    fids |= {f"x:{x}" for x in rec.get("x") or {} if aff.get(f"x:{x}", 0) < 0}
    parts = sorted(((aff[fid], fid) for fid in fids if fid in aff), reverse=True)
    good = [x for x in parts if x[0] > 0][:6]
    bad = [x for x in parts if x[0] < 0]
    return sum(a for a, _ in good) + sum(a for a, _ in bad), good


SCORE_BLEND = (0, 0, .3)  # frozen W2b facet score; E1 matched-negative evaluation in eval/README.md
_maturity_house = (None, None, 0)


def maturity(m, person=None):
    global _maturity_house
    counts = (m or {}).get("nexp", {})
    if person == GUEST:
        return {"state": "guest", "explicit": 0, "need": PCT_MIN_EXPLICIT}
    if person in PEOPLE:
        n = counts.get(person, 0)
    elif "household" in counts:
        n = counts["household"]
    else:
        key = (PEOPLE, tuple(counts.get(p, 0) for p in PEOPLE), len((m or {}).get("ent", {})))
        cached = _maturity_house
        if cached[0] is m and cached[1] == key:
            n = cached[2]
        else:
            overlap = sum(max(0, sum(p in by for p in PEOPLE)-1) for title, by in (m or {}).get("ent", {}).values())
            # A model's evidence is immutable; avoid recounting the same titles for every card.
            n = max(0, sum(key[1])-overlap)
            _maturity_house = (m, key, n)
    state = "too_new" if n < PCT_MIN_EXPLICIT else "learning" if person in PEOPLE and n < PRIOR_FULL else "ready"
    return {"state": state, "explicit": n, "need": PCT_MIN_EXPLICIT}


def person_score(m, key, person=None, kind=None):
    raw, fids = score_key(m, key, person, kind)
    if not HOUSEHOLD_PRIOR or person not in PEOPLE or "accounts" in CONFIG["connectors"].get("trakt_api", {}):
        return raw, fids
    lam = min(1, (m or {}).get("nexp", {}).get(person, 0)/PRIOR_FULL)
    if lam == 1:
        return raw, fids
    house, house_fids = score_key(m, key, kind=kind)
    if raw is None or house is None:
        return house, house_fids
    return lam*raw + (1-lam)*house, fids


_viewer = contextvars.ContextVar("viewer", default=None)
_person_models = {}
_person_model_lock = threading.Lock()


def person_model(m, person):
    # shortcut: member_feed/week_feed build candidates from both memories; strict per serve until feed isolation.
    if not _person_models and not (m or {}).get("mem0"): return m
    with _person_model_lock:
        if _person_models:
            base = next(iter(_person_models.values()))[0]
            if any(m is view for _, view in _person_models.values()):
                m = base
            elif m is not base:
                _person_models.clear()
        viewer = _viewer.get()
        if viewer not in PEOPLE or person not in PEOPLE or person == viewer or person not in (m or {}).get("mem0", {}):
            return m
        key = (id(m), person)
        if key not in _person_models:
            block = m["mem0"][person]
            view = {**m, **{name: {**m.get(name, {}), person: block[name]} for name in ("paff", "pbscale", "pbg")}}
            _person_models[key] = (m, view)
        return _person_models[key][1]


def score_key(m, key, person=None, kind=None):
    """(score, facet ids worth naming) for a title, for the household or one person; score None when unknown."""
    if not m:
        return None, []
    rec = frec(key, kind)
    if not rec:
        return None, []
    if person == "together":
        house, house_fids = score_key(m, key, kind=kind)
        if house is None:
            return None, []
        scores = [score_key(m, key, pp, kind) for pp in PEOPLE]
        values = []
        for pp, (s, _) in zip(PEOPLE, scores):
            lam = min(1, m["nexp"].get(pp, 0)/40)
            values.append(lam * (s if s is not None else house) + (1-lam)*house)
        fids = [fid for fid in scores[0][1] if all(fid in fs for _, fs in scores[1:])]
        return sum(values)/len(values) - .25*(max(values)-min(values)), fids or house_fids
    if person: m = person_model(m, person)
    vec = (m.get("pvec") or {}).get(person) if person else m["vec"]
    v = fit(vec, key, kind)
    bonus, good = facet_bonus(m, rec, (m.get("paff") or {}).get(person, {}) if person else None)
    scale = m.get("pbscale", {}).get(person, 1.0) if person else m["bscale"]
    xp = m.get("px", {}).get(person) if person else m.get("x")
    a, b, c = SCORE_BLEND
    s = a * (v if v is not None else m["vbase"]) + b * xsim((rec or {}).get("x"), xp) + c * math.tanh(bonus/scale) + quality_prior(rec)
    why = [fid for _, fid in good if fid[0] in "xkdatn" and speaks(fid, m["names"].get(fid, ""))]
    why.sort(key=lambda fid: fid[0] != "x")  # the experience first: it says most about why
    return s, why[:3]


_seed_stats = ({}, 1, {})


def clear_seed_cache():
    global _pin_epoch
    with _home_lock:
        seed_similarity.cache_clear()
        _home_cache.clear()
        _pin_epoch += 1


@lru_cache(maxsize=65536)
def seed_similarity(a, b):
    """Equal shares of fingerprint cosine and rare shared facets; independent of household taste."""
    df, n, pairs = _seed_stats
    overlap = sum(FACET_W[fid[0]] * max(.05, math.log(n/(df[fid]+1))/math.log(n))
                  for fid in sorted(pairs.get(a, set()) & pairs.get(b, set())))
    with _emb_lock:
        va, vb = femb(a, None), femb(b, None)  # seed_similarity accepts bare keys without kinds.
        norm = math.sqrt(sum(x*x for x in va) * sum(x*x for x in vb)) if va and vb else 0
        cosine = sum(x*y for x, y in zip(va, vb))/norm if norm else 0
    return .5*cosine + .5*math.tanh(overlap/2)


def seed_knn(seeds, key, k=3):
    """Mean of the closest k seed matches; None without seeds."""
    if k < 1:
        raise ValueError("k must be positive")
    scores = sorted((seed_similarity(seed, key) for seed in seeds), reverse=True)[:k]
    return sum(scores)/len(scores) if scores else None


def mmr(keys, lam=.7):
    """Diversify ranked keys without replacing relevance. O(n²) `fit` calls; keep n ≤ 60."""
    if not 0 <= lam <= 1:
        raise ValueError("lam must be between 0 and 1")
    # This key-only evaluation helper has no candidate kinds.
    left, chosen = list(dict.fromkeys(keys)), []
    relevance = {k: 1-i/max(1, len(left)) for i, k in enumerate(left)}
    while left:
        with _emb_lock:
            key = max(left, key=lambda k: lam*relevance[k] - (1-lam)*max(
                (max(0, fit(femb(k, None), other) or 0) for other in chosen), default=0))
        chosen.append(key)
        left.remove(key)
    return chosen


def who_label(by):
    """Label an anchor belonging to one member other than the watch-log owner."""
    mine = [p for p in PEOPLE if p in by]
    return " (" + LABELS[mine[0]] + ")" if len(mine) == 1 and mine[0] != TRAKT_PERSON and "together" not in by else ""


def anchor(m, key, fid, used=(), person=None, kind=None):
    """The loved title behind a facet that is closest to this one by fingerprint: (key, title, by) or None.
    For one person, their own titles first."""
    best = None
    v = femb(key, kind)
    for w, hk in m["holders"].get(fid, []):
        if hk == key or hk in used or hk not in m["ent"]:
            continue
        by = m["ent"][hk][1]
        if person and person not in by and "together" not in by:
            continue
        if person in PEOPLE and "accounts" in CONFIG["connectors"].get("trakt_api", {}) and hk not in m.get("panchors", {}).get(person, ()):
            continue
        raw = fit(v, hk, m.get("kinds", {}).get(hk))
        if fid[0] not in "xdn" and raw is not None and raw < 0.4:
            continue  # a shared keyword alone ('dragon') makes a silly comparison
        sim = (raw or 0.0) + 0.03 * min(w, 4) + (0.3 if person and person in by else 0)
        if best is None or sim > best[0]:
            best = (sim, hk)
    return (best[1], *m["ent"][best[1]]) if best else None


def reasons(m, key, fids, n=2, person=None, kind=None):
    """['Found family like Firefly', 'Witty banter duo like Moonlighting'] for a card."""
    out, used = [], set()
    for fid in fids:
        if len(out) >= n:
            break
        a = anchor(m, key, fid, used, person, kind)
        name = nice_facet(fid, m["names"].get(fid, ""))
        if not reason_ok(name, key, fid, kind):
            continue
        if fid[0] == "x":
            name = name[:1].upper() + name[1:]
        if a:
            used.add(a[0])
            out.append(f"{name} like {a[1]}{'' if person else who_label(a[2])}")
        else:
            out.append(name)
    return out


def confidence(m, key, s, fids, person=None):
    """'Strong fit' when the match is high and two loved titles share its main reason, 'Good fit', else 'Wildcard'."""
    m = person_model(m, person)
    pct = match_pct(m, s, person)
    if pct is None:
        return None, None
    support = len(m["holders"].get(fids[0], [])) if fids else 0
    if pct >= 88 and support >= 2:
        return "Strong fit", pct
    return ("Good fit", pct) if pct >= 70 else ("Wildcard", pct)


def explain(key, kind=None):
    """Everything behind one title's note, for /why/<key>.json."""
    m = _model["m"]
    if not m:
        return {"key": key, "model": None}
    s, fids = score_key(m, key, kind=kind)
    conf, pct = confidence(m, key, s, fids)
    rec = frec(key, kind)  # /why has no type; internal callers can supply one.
    held, now, memory = {fid for fid, _ in facet_pairs(rec)}, time.time(), memory_load()
    def facts(pp, kind):
        return [{"id": f["id"], "kind": f["kind"], "src": f["src"], "strength": memory_strength(f, now)}
                for f in memory["facts"] if f["person"] == pp and f["status"] == "active"
                and f["kind"] == kind and f["about"] in held]
    allf = sorted(((m["aff"][fid], fid) for fid, _ in facet_pairs(rec) if fid in m["aff"]), reverse=True)
    return {"key": key, "title": rec.get("title"), "score": s, "percentile": pct, "confidence": conf,
            "fit": fit(m["vec"], key, kind), "reasons": reasons(m, key, fids, 3, kind=kind),
            "experiences": rec.get("x"),
            "per_person": {pp: {"score": person_score(m, key, pp, kind)[0], "n_explicit": m["nexp"].get(pp, 0),
                                "maturity": maturity(m, pp),
                                "percentile": confidence(m, key, *person_score(m, key, pp, kind), pp)[1],
                                "memory": {"likes": facts(pp, "like"), "avoids": facts(pp, "avoid"),
                                           "vetoed": bool(held & veto_abouts(pp, now, memory))}}
                           for pp in PEOPLE},
            "facets": [{"id": fid, "name": m["names"].get(fid), "affinity": round(a, 3),
                        "anchors": [m["ent"][k][0] for _, k in m["holders"].get(fid, [])[:4] if k in m["ent"]]}
                       for a, fid in allf[:12]]}


def match_pct(m, s, person=None):
    if maturity(m, person)["state"] in ("too_new", "guest"):
        return None
    if s is None or not m or not m.get("cal"):
        return None
    bg = m.get("pbg", {}).get(person, []) if person else m["bg"]
    if len(bg) < 20:
        return None
    rank = (bisect.bisect_left(bg, s) + bisect.bisect_right(bg, s)) / 2
    return max(40, min(99, round(40 + 59 * rank / len(bg))))


def match_note(key, why=True, floor=65, person=None, kind=None):
    """Human reasons for a card; confidence belongs in /why, not on every poster."""
    m = _model["m"]
    s, fids = person_score(m, key, person, kind)
    conf, pct = confidence(m, key, s, fids, person)
    if pct is None or pct < floor:
        return []
    return reasons(m, key, fids, person=person, kind=kind) if why else []


def taste_hint(m):
    """Search tie-breakers: experiences and themes, without favourite people or studios."""
    return taste_line({**m, "aff": {fid: a for fid, a in m["aff"].items() if fid[0] in "xkt"}})


def taste_line(m):
    def top(types, n):
        xs = sorted(((a, fid) for fid, a in m["aff"].items() if fid[0] in types and a > 0
                     and speaks(fid, m["names"][fid])), reverse=True)[:n]
        return ", ".join(nice_facet(fid, m["names"][fid]) for _, fid in xs)
    parts = [("experiences", top("x", 6)), ("themes", top("kt", 8)), ("creators", top("d", 4)), ("actors", top("a", 4)), ("networks/studios", top("nc", 4)),
             ("source", top("s", 2))]
    return "; ".join(f"{k}: {v}" for k, v in parts if v)


def get_model(build=True):
    """The current taste model; rebuilt (in the worker only) when the profile or our knowledge changed."""
    p = _profiles.get("household")
    if not p:
        return None
    if not build:
        return _model["m"]
    with _model_lock:
        with _memory_lock:
            seq = _memory_read()["seq"]
        sig = (p.get("hash"), len(_learn["hits"]) if HIT_TASTE else 0, local_now().strftime("%Y-%m-%d"), seq)
        if _model["sig"] != sig or _model["m"] is None:
            _model["m"] = build_model(p)
            _model["sig"] = sig
            m = _model["m"]
            print(f"taste model: {len(m['aff'])} facets, cal {m['cal']}, {m['taste_line'][:160]}", flush=True)
            taste_ring_note(m, time.time())
        return _model["m"]


def rank_by_taste(m, items):
    """items: [(key, kind, payload)] -> sorted best first, as [(score, why, key, kind, payload)]; unknowns last."""
    try:
        ensure_facets([(k, kind) for k, kind, _ in items])
        ensure_emb([(k, kind) for k, kind, _ in items])
    except Exception as e:
        print(f"rank: {type(e).__name__}: {e}", flush=True)
    tag_experiences([(k, kind) for k, kind, _ in items[:50]], max_batches=2)
    scored = [(score_key(m, k, kind=kind), i, k, kind, x) for i, (k, kind, x) in enumerate(items)]
    scored.sort(key=lambda t: (t[0][0] is None, -(t[0][0] or 0), t[1]))
    return [(sw[0], sw[1], k, kind, x) for sw, _, k, kind, x in scored]


def with_match(meta, why=True, floor=65, person=None):
    note = match_note(meta["id"], why, floor, person, "movie" if meta["type"] == "movie" else "tv")
    if not note:
        return meta
    return {**meta, "description": " · ".join(dict.fromkeys(note + ([meta["description"]] if meta.get("description") else [])))}


def taste_order(metas, sort=True, why=True):
    """Metas best match first (stable for unknowns) with a '91% match' note; unchanged without a model."""
    m = get_model()
    if not m or not metas:
        return metas
    items = [(x["id"], "movie" if x["type"] == "movie" else "tv", x) for x in metas]
    ranked = [x for *_, x in rank_by_taste(m, items)] if sort else metas
    return [with_match(x, why) for x in ranked]


# --- taste memory (W10a) ---
HIT_TASTE = False  # Row hits stay logged, but never move taste.
MEMORY_CAP, MEMORY_MERGE_AT = 40, 32
MEMORY_TOP, MEMORY_TYPES = 15, "xktgd"
MEMORY_SUPPORT, MEMORY_SPREAD = 3, 2
MEMORY_REASON = 2
MEMORY_CONTRARY = (2, 30)
MEMORY_HALF_LIFE, MEMORY_FLOOR = 180, .15
MEMORY_KEEP = .75
MEMORY_COOLDOWN = 90
MEMORY_EVIDENCE, MEMORY_ENDED = 12, 40
MEMORY_LOG_MAX = (2000, 1600)
MEMORY_POS = {"love", "like", "play", "seen"}
MEMORY_NEG = {"not_for_us", "abandon"}
MEMORY_EXPLICIT = {"love", "like", "not_for_us"}
MEMORY_PSEUDO = 1.75
MEMORY_VETO_DAYS, MEMORY_VETO_MAX_SHARE = 180, .3
MEMORY_WHY_MIN, MEMORY_WHY_ROW = .4, 2
VETO_EXEMPT = {rid('episodes'), rid('catchup'), rid('rewatch')}
_memory, _memory_lock, _memory_index = None, threading.Lock(), {}
_memory_veto = {}


class MemoryFull(ValueError):
    pass


def reason_fids(reason, rec):
    fids = [f"x:{x}" for x in REASON_FACETS.get(reason, ())]
    if reason == "not my genre":
        fids = [f"g:{g}" for g in rec.get("g", ())]
    elif reason == "seen enough of these":
        fids = [f"x:{x}" for x, st in (rec.get("x") or {}).items() if st >= 2]
    return fids


def memory_family(src):
    return EVENT_FAMILY.get(src) or (src.split(":", 1)[1] if src.startswith(("import:", "sync:")) else src)


def memory_week(t):
    return tuple(datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isocalendar()[:2])


def _memory_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _memory_empty():
    return {"ver": 1, "seq": 0, "stamp": None, "at": 0, "forgotten": {}, "facts": []}


def _memory_atomic(path, data):
    fd, tmp = tempfile.mkstemp(prefix=".memory-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _memory_logs():
    try:
        with open(os.path.join(DATA, "memory.log.jsonl")) as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def _memory_log(ops):
    if not ops:
        return
    path = os.path.join(DATA, "memory.log.jsonl")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as fh:
        os.fchmod(fh.fileno(), 0o600)
        for op in ops:
            fh.write(json.dumps(op, ensure_ascii=False, allow_nan=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    with open(path, "rb") as fh:
        lines = deque(fh, maxlen=MEMORY_LOG_MAX[0])
    if len(lines) >= MEMORY_LOG_MAX[0]:
        _memory_atomic(path, b"".join(list(lines)[-MEMORY_LOG_MAX[1]:]))


def _memory_read():
    global _memory
    if _memory is None:
        path = os.path.join(DATA, "memory.json")
        try:
            with open(path) as fh:
                value = json.load(fh)
            if not isinstance(value, dict) or value.get("ver") != 1 or not isinstance(value.get("facts"), list) \
                    or type(value.get("seq")) is not int or value["seq"] < 0 or not isinstance(value.get("forgotten"), dict) \
                    or not math.isfinite(value["at"]) or value["stamp"] is not None and not isinstance(value["stamp"], str) \
                    or any(p not in PEOPLE or not math.isfinite(t) for p, t in value["forgotten"].items()):
                raise ValueError("invalid memory store")
            ids = set()
            for f in value["facts"]:
                if f["person"] not in PEOPLE or f["kind"] not in ("like", "avoid") \
                        or f["status"] not in ("proposed", "active", "rejected", "superseded", "decayed", "capped") \
                        or not re.fullmatch(r"m[1-9][0-9]*", f["id"]) or not math.isfinite(f["strength"]) \
                        or not 0 <= f["strength"] <= 1 or not isinstance(f["evidence"], list) \
                        or len(f["evidence"]) > MEMORY_EVIDENCE or any(not isinstance(e, str) for e in f["evidence"]) \
                        or f["status"] in ("proposed", "active") and not f["evidence"] \
                        or not re.fullmatch(r"[xktgd]:[^\x00-\x1f]{1,80}", f["about"]) \
                        or not isinstance(f["text"], str) or len(f["text"]) > 60 \
                        or f["src"] not in ("implicit", "reason", "told") or not isinstance(f["via"], str) \
                        or not isinstance(f["gates"], dict) or type(f["n"]) is not int or f["n"] < 1 \
                        or not all(math.isfinite(f[k]) for k in ("created", "seen")) \
                        or f["confirmed"] is not None and not math.isfinite(f["confirmed"]) \
                        or not _memory_live(f) and not math.isfinite(f["end"]) \
                        or f["into"] is not None and not re.fullmatch(r"m[1-9][0-9]*", f["into"]) \
                        or f["id"] in ids or _memory_seq(f) > value["seq"]:
                    raise ValueError("invalid memory fact")
                ids.add(f["id"])
            _memory = value
        except FileNotFoundError:
            _memory = _memory_empty()
        except (ValueError, TypeError, KeyError):
            os.chmod(path, 0o600)
            os.replace(path, os.path.join(DATA, "memory.json.bad"))
            _memory = _memory_empty()
            _memory_index.clear()
            _memory_log([{"t": time.time(), "op": "corrupt", "n": 1}])
    return _memory


def memory_load():
    with _memory_lock:
        return _memory_copy(_memory_read())


def _memory_save(store, ops, forget=False):
    global _memory
    path = os.path.join(DATA, "memory.json")
    if forget:
        _decision_write_json(os.path.join(DATA, "memory.prev.json"), store)
    else:
        try:
            with open(path, "rb") as fh:
                old = fh.read()
        except FileNotFoundError:
            old = json.dumps(_memory_empty()).encode()
        _memory_atomic(os.path.join(DATA, "memory.prev.json"), old)
    _decision_write_json(path, store)
    _memory_log(ops)
    _memory = _memory_copy(store)
    _memory_index.clear()
    _memory_veto.clear()


def memory_save(store, ops):
    with _memory_lock:
        _memory_save(store, ops)


def memory_strength(f, now):
    if f["src"] == "told":
        return f["strength"]
    days = max(0, now - max(f["seen"], f.get("confirmed") or 0)) / DAY
    return f["strength"] * .5 ** (days / MEMORY_HALF_LIFE)


def memory_pseudo(person, paff, now, store=None):
    store = memory_load() if store is None else store
    out = dict(paff)
    positive = sorted(a for a in paff.values() if a > 0)
    q99 = positive[int(.99*(len(positive)-1))] if positive else 1.0
    for f in store["facts"]:
        if f["person"] != person:
            continue
        about, strength = f["about"], memory_strength(f, now)
        if f["status"] == "active" and (f["src"] == "told" or f["confirmed"] is not None) and strength:
            floor = MEMORY_PSEUDO * strength * q99
            out[about] = max(out.get(about, 0), floor) if f["kind"] == "like" else min(out.get(about, 0), -floor)
    for f in store["facts"]:
        if f["person"] == person and f["status"] == "rejected" and 0 <= now-f["end"] < MEMORY_COOLDOWN*DAY and f["about"] in out:
            out[f["about"]] *= 1 - .5*memory_strength(f, now)
    return out


def _memory_breadth(now=None):
    p = _profiles.get("household") or {}
    now = time.time() if now is None else now
    sig = (len(_facets), id(p), p.get("hash"), int(now//DAY))
    if _memory_veto.get("breadth_sig") != sig:
        unseen = {}
        with _facets_lock:
            for key, rec in _facets.items():
                key = key.split("@", 1)[0]
                if key not in p.get("seen", ()):
                    unseen.setdefault(key, set()).update(fid for fid, _ in facet_pairs(rec))
        counts = Counter(fid for held in unseen.values() for fid in held)
        _memory_veto.update(breadth_sig=sig, breadth={fid: n/(len(unseen) or 1) for fid, n in counts.items()})
    return _memory_veto["breadth"]


def _memory_veto_state(f, now):
    if f["status"] != "active" or f["kind"] != "avoid" or f["strength"] < .8:
        return None
    if f["src"] != "told":
        if f["confirmed"] is None:
            return None
        if now-f["confirmed"] > MEMORY_VETO_DAYS*DAY:
            return "lapsed"
    return "too_broad" if _memory_breadth(now).get(f["about"], 0) > MEMORY_VETO_MAX_SHARE else "on"


def veto_abouts(who, now, store=None):
    if who not in (*PEOPLE, "both"):
        return frozenset()
    store = memory_load() if store is None else store
    sig = (store["seq"], who, int(now//DAY))
    cache = _memory_veto.setdefault("abouts", {})
    result = cache.get(sig)
    if result is None:
        people = PEOPLE if who == "both" else (who,)
        result = frozenset(f["about"] for f in store["facts"] if f["person"] in people and _memory_veto_state(f, now) == "on")
        if len(cache) >= 8:
            cache.clear()
        cache[sig] = result
    return result


def veto_keys(who, now=None):
    now = time.time() if now is None else now
    with _memory_lock:
        store = _memory_read()
        sig = (store["seq"], who, len(_facets), int(now//DAY))
        cache = _memory_veto.setdefault("keys", {})
        result = cache.get(sig)
        if result is None:
            if _memory_veto.get("facet_count") != len(_facets):
                _memory_veto.pop("abouts", None)
                _memory_veto["facet_count"] = len(_facets)
            abouts = veto_abouts(who, now, store)
            with _facets_lock:
                result = frozenset(key.split("@", 1)[0] for key, rec in _facets.items()
                                   if any(fid in abouts for fid, _ in facet_pairs(rec))) if abouts else frozenset()
            if len(cache) >= 8:
                cache.clear()
            cache[sig] = result
        return result


def memory_text(kind, about, names):
    label = nice_facet(about, names.get(about) or about[2:].replace("_", " "))
    return (("likes " if kind == "like" else "avoids ") + label)[:60]


def memory_about_ok(about, names):
    return isinstance(about, str) and bool(re.fullmatch(r"[xktgd]:[^\x00-\x1f]{1,80}", about)) and \
        (about in names or about[2:] in EXPERIENCES or about in {f"x:{x}" for xs in REASON_FACETS.values() for x in xs})


def _memory_seq(f):
    return int(f["id"][1:])


def _memory_live(f):
    return f["status"] in ("proposed", "active")


def _memory_op(store, ops, op, f, now, before=None, reason=None):
    store["seq"] += 1
    ops.append({"t": now, "op": op, "id": f["id"], "person": f["person"], "kind": f["kind"],
                "about": f["about"], "from": before, "to": f["status"], "n": f["n"], "reason": reason})


def _memory_end(store, ops, op, f, now, status, into=None):
    before = f["status"]
    f.update(status=status, end=now, evidence=[], into=into)
    _memory_op(store, ops, op, f, now, before)


def _memory_new(store, person, kind, about, now, names, **fields):
    f = {"id": f"m{store['seq']+1}", "person": person, "kind": kind, "about": about,
         "text": memory_text(kind, about, names), "strength": 0, "n": 0, "evidence": [],
         "src": "implicit", "via": "curate", "status": "proposed", "gates": {},
         "created": now, "confirmed": None, "seen": now, "end": None, "into": None, **fields}
    store["facts"].append(f)
    return f


def _memory_trim(store, ops, now):
    for person in PEOPLE:
        ended = sorted((f for f in store["facts"] if f["person"] == person and not _memory_live(f)),
                       key=lambda f: (f["end"], _memory_seq(f)))
        for f in ended[:-MEMORY_ENDED]:
            store["facts"].remove(f)
            _memory_op(store, ops, "trim", f, now, f["status"])
    store["facts"].sort(key=_memory_seq)


def curate(evts, m, now=None, store=None, stamp=None, dry_run=False, facets=None):
    now = time.time() if now is None else now
    if dry_run:
        return _memory_curate(evts, m, now, memory_load() if store is None else _memory_copy(store), stamp, facets)
    with _memory_lock:
        result = _memory_curate(evts, m, now, _memory_copy(_memory_read() if store is None else store), stamp, facets)
        if result["refused"]:
            _memory_log([{"t": now, "op": "refused", "n": sum(r["ending"] for r in result["refused"])}])
        elif result["ops"]:
            _memory_save(result["store"], result["ops"])
        return result


def _memory_curate(evts, m, now, store, stamp, facets):
    start = time.perf_counter()
    prior = _memory_copy(store)
    if facets is None:
        with _facets_lock:
            facets = dict(_facets)
    names = m["names"]
    candidates, support, reasons, contrary, taint = {}, {}, {}, {}, {}
    live = {(f["person"], f["about"], f["kind"]): f for f in store["facts"] if _memory_live(f)}
    for person in PEOPLE:
        ranked = sorted(((fid, a) for fid, a in m.get("paff0", m["paff"]).get(person, {}).items()
                         if fid[0] in MEMORY_TYPES and fid in names and speaks(fid, names[fid]) and a),
                        key=lambda pair: (-abs(pair[1]), pair[0]))[:MEMORY_TOP]
        candidates[person] = {fid: ("like" if a > 0 else "avoid", rank) for rank, (fid, a) in enumerate(ranked, 1)}

    def collect(table, slot, ev, family, week):
        acc = table.setdefault(slot, {"keys": set(), "families": set(), "weeks": set(), "ids": [], "seen": None, "conf": 1.0})
        acc["keys"].add(ev["key"])
        acc["families"].add(family)
        if week is not None:
            acc["weeks"].add(week)
        t = ev["t"]
        if t is not None:
            acc["seen"] = max(acc["seen"] or t, t)
        acc["conf"] = min(acc["conf"], ev["conf"])
        pair = (t if t is not None else -math.inf, ev["id"])
        if pair not in acc["ids"]:
            acc["ids"].append(pair)
            acc["ids"].sort(key=lambda x: (-x[0], x[1]))
            del acc["ids"][MEMORY_EVIDENCE:]

    stats = {"events": 0, "used": 0, "tainted": 0, "candidates": {p: len(candidates[p]) for p in PEOPLE}}
    for ev in evts:
        stats["events"] += 1
        src = ev["src"]
        if src not in taint:
            taint[src] = tainted(ev)
        if taint[src]:
            stats["tainted"] += 1
            continue
        if ev["action"] not in MEMORY_POS | MEMORY_NEG:
            continue
        people = [p for p in PEOPLE if p in ev["by"] and
                  (p not in store["forgotten"] or ev["t"] is not None and ev["t"] > store["forgotten"][p])]
        if not people:
            continue
        stats["used"] += 1
        rec = facets.get(fkey(ev["key"], ev["kind"])) or {}
        held = {fid for fid, _ in facet_pairs(rec)}
        kind = "like" if ev["action"] in MEMORY_POS else "avoid"
        family, week = memory_family(src), memory_week(ev["t"]) if ev["t"] is not None else None
        for person in people:
            for fid in held:
                if fid in candidates[person] and candidates[person][fid][0] == kind:
                    collect(support, (person, fid, kind), ev, family, week)
                opposite = live.get((person, fid, "avoid" if kind == "like" else "like"))
                if opposite and ev["action"] in MEMORY_EXPLICIT and ev["t"] is not None \
                        and ev["t"] > (opposite["confirmed"] or opposite["created"]):
                    contrary.setdefault(opposite["id"], {})[ev["id"]] = ev["t"]
            if ev["action"] == "not_for_us":
                for reason in set(ev["reasons"]) & set(REMOTE_REASONS):
                    for fid in set(reason_fids(reason, rec)) & held:
                        if memory_about_ok(fid, names):
                            collect(reasons, (person, fid, reason), ev, family, week)

    offers = {}
    for (person, fid, kind), acc in support.items():
        n = len(acc["keys"])
        if n >= MEMORY_SUPPORT and (len(acc["families"]) >= MEMORY_SPREAD or len(acc["weeks"]) >= MEMORY_SPREAD):
            offers[(person, fid, kind)] = {"n": n, "evidence": [eid for _, eid in acc["ids"]],
                "strength": min(.7, round(.1*n, 2)), "seen": acc["seen"], "src": "implicit", "via": "curate",
                "status": "proposed", "gates": {"support": n, "families": len(acc["families"]),
                    "weeks": len(acc["weeks"]), "rank": candidates[person][fid][1]}}
    for (person, fid, reason), acc in sorted(reasons.items()):
        n = len(acc["keys"])
        if n < MEMORY_REASON:
            continue
        offer = {"n": n, "evidence": [eid for _, eid in acc["ids"]], "strength": min(.7, .35*n)*acc["conf"],
                 "seen": acc["seen"], "src": "reason", "via": "reason:"+reason, "status": "active",
                 "gates": {"support": n, "reason": reason}}
        slot = (person, fid, "avoid")
        if slot not in offers or offers[slot]["src"] == "implicit" or n > offers[slot]["n"]:
            offers[slot] = offer
    ops = []
    for (person, fid, kind), offer in sorted(offers.items()):
        f = live.get((person, fid, kind))
        if f:
            if f["src"] == "told" or (f["n"] == offer["n"] and f["evidence"] == offer["evidence"]
                    and (offer["src"] != "reason" or f["src"] == "reason" and f["gates"] == offer["gates"] and f["status"] == "active")):
                continue
            before = f["status"]
            strength = max(f["strength"], offer["strength"]) if f["confirmed"] else offer["strength"]
            status = "active" if f["status"] == "active" else offer["status"]
            f.update({**offer, "strength": strength, "status": status, "seen": offer["seen"] if offer["seen"] is not None else f["created"]})
            _memory_op(store, ops, "reinforce", f, now, before)
            continue
        ended = [f for f in store["facts"] if (f["person"], f["about"], f["kind"]) == (person, fid, kind) and not _memory_live(f)]
        if any((now-f["end"] < MEMORY_COOLDOWN*DAY or offer["n"] < 2*f["n"]) if f["status"] == "rejected"
               else (now-f["end"] < MEMORY_COOLDOWN*DAY and offer["n"] < 2*f["n"]) for f in ended):
            continue
        f = _memory_new(store, person, kind, fid, now, names, **{**offer, "seen": offer["seen"] if offer["seen"] is not None else now})
        live[(person, fid, kind)] = f
        _memory_op(store, ops, "add", f, now)
    for f in sorted(store["facts"], key=lambda f: (f["person"], f["about"], f["kind"], _memory_seq(f))):
        if not _memory_live(f):
            continue
        ts = sorted(contrary.get(f["id"], {}).values())
        count, days = MEMORY_CONTRARY
        if any(ts[i+count-1]-t <= days*DAY for i, t in enumerate(ts[:len(ts)-count+1])):
            _memory_end(store, ops, "supersede", f, now, "superseded")
        elif memory_strength(f, now) < MEMORY_FLOOR:
            _memory_end(store, ops, "decay", f, now, "decayed")
    for person in PEOPLE:
        facts = [f for f in store["facts"] if f["person"] == person and _memory_live(f)]
        while len(facts) > MEMORY_MERGE_AT:
            pair = None
            ordered = sorted(facts, key=lambda f: (-memory_strength(f, now), _memory_seq(f)))
            for i, a in enumerate(ordered):
                for b in ordered[i+1:]:
                    ea, eb = set(a["evidence"]), set(b["evidence"])
                    if a["kind"] == b["kind"] and len(ea & eb)/len(ea | eb) >= .7:
                        pair = a, b
                        break
                if pair:
                    break
            if not pair:
                break
            a, b = pair
            _memory_end(store, ops, "merge", b, now, "superseded", a["id"])
            facts.remove(b)
        while len(facts) > MEMORY_CAP:
            proposed = [f for f in facts if f["status"] == "proposed"]
            if not proposed:
                break
            f = min(proposed, key=lambda f: (memory_strength(f, now), f["n"], -_memory_seq(f)))
            _memory_end(store, ops, "cap", f, now, "capped")
            facts.remove(f)
    _memory_trim(store, ops, now)
    refused = []
    for person in PEOPLE:
        active = {f["id"] for f in prior["facts"] if f["person"] == person and f["status"] == "active"}
        surviving = {f["id"] for f in store["facts"] if f["status"] == "active"}
        ending = len(active-surviving)
        if ending > max(1, int((1-MEMORY_KEEP)*len(active))):
            refused.append({"person": person, "active_before": len(active), "ending": ending})
        elif sum(f["person"] == person and _memory_live(f) for f in store["facts"]) > MEMORY_CAP:
            refused.append({"person": person, "active_before": len(active), "ending": ending, "reason": "cap"})
    if ops and not refused:
        store.update(stamp=stamp, at=now)
    if refused:
        store = prior
    stats.update(proposed={p: sum(f["person"] == p and f["status"] == "proposed" for f in store["facts"]) for p in PEOPLE},
                 active={p: sum(f["person"] == p and f["status"] == "active" for f in store["facts"]) for p in PEOPLE},
                 ms=round((time.perf_counter()-start)*1000, 3))
    ops.sort(key=lambda op: (op["person"], op["about"], op["kind"]))
    return {"ops": ops, "store": store, "refused": refused, "stats": stats}


def memory_tell(person, kind, about, src="told:cli", conf=1.0, now=None, names=None):
    now = time.time() if now is None else now
    names = (get_model(False) or {}).get("names", {}) if names is None else names
    if person not in PEOPLE or kind not in ("like", "avoid") or not memory_about_ok(about, names) \
            or not isinstance(conf, (int, float)) or not math.isfinite(conf) or conf < 0:
        raise ValueError("invalid memory tell")
    if not isinstance(src, str) or not re.fullmatch(r"(?:told:(?:cli|phone)|agent:[a-z][a-z0-9_-]{0,31}|utterance|(?:import|sync):[a-z]+|tv|trakt|told|remote|interview)", src):
        raise ValueError("invalid memory source")
    trusted = not tainted({"src": "told" if src.startswith("told:") else src})
    with _memory_lock:
        store, ops = _memory_copy(_memory_read()), []
        live = [f for f in store["facts"] if f["person"] == person and _memory_live(f)]
        same = next((f for f in live if f["about"] == about and f["kind"] == kind), None)
        opposites = [f for f in live if f["about"] == about and f["kind"] != kind] if trusted else []
        if same is None and len(live)-len(opposites) >= MEMORY_CAP:
            proposed = [f for f in live if f["status"] == "proposed"]
            if not proposed:
                raise MemoryFull("all memory slots are active")
            f = min(proposed, key=lambda f: (memory_strength(f, now), f["n"], -_memory_seq(f)))
            _memory_end(store, ops, "cap", f, now, "capped")
        if same:
            f, before = same, same["status"]
            f.update(strength=max(f["strength"], min(1, conf)), n=f["n"]+1)
            if trusted:
                f.update(src="told", via=src, status="active", confirmed=now, seen=now, gates={})
            _memory_op(store, ops, "reinforce", f, now, before)
            f["evidence"] = [f"op:{store['seq']}"] + f["evidence"][:MEMORY_EVIDENCE-1]
        else:
            f = _memory_new(store, person, kind, about, now, names, src="told", via=src, n=1,
                            strength=min(1, conf), status="active" if trusted else "proposed", confirmed=now if trusted else None,
                            evidence=[f"op:{store['seq']+1}"])
        _memory_op(store, ops, "tell", f, now, before if same else None)
        for opposite in opposites:
            _memory_end(store, ops, "supersede", opposite, now, "superseded", f["id"])
        _memory_trim(store, ops, now)
        store["at"] = now
        _memory_save(store, ops)
        return _memory_copy(f)


def _memory_viewer(person, viewer):
    if viewer not in (None, *PEOPLE) or viewer is not None and person is not None and person != viewer:
        raise PermissionError("memory belongs to another person")
    if person is not None and person not in PEOPLE:
        raise ValueError("person must be " + " or ".join(reversed(PEOPLE)))


def _memory_find(store, fid, viewer):
    f = next((f for f in store["facts"] if f["id"] == fid), None)
    if f is None:
        raise ValueError("unknown memory id")
    _memory_viewer(f["person"], viewer)
    return f


def _memory_decide(fid, viewer, now, reject):
    now = time.time() if now is None else now
    with _memory_lock:
        store, ops = _memory_copy(_memory_read()), []
        f = _memory_find(store, fid, viewer)
        if not _memory_live(f):
            raise ValueError("memory fact has ended")
        if reject:
            _memory_end(store, ops, "reject", f, now, "rejected")
        else:
            before = f["status"]
            f.update(status="active", strength=max(f["strength"], .8), confirmed=now)
            _memory_op(store, ops, "confirm", f, now, before)
        _memory_trim(store, ops, now)
        store["at"] = now
        _memory_save(store, ops)
        return _memory_copy(f)


def memory_confirm(fid, viewer=None, now=None):
    return _memory_decide(fid, viewer, now, False)


def memory_reject(fid, viewer=None, now=None):
    return _memory_decide(fid, viewer, now, True)


def memory_forget(person, now=None):
    if person not in PEOPLE:
        raise ValueError("person must be " + " or ".join(reversed(PEOPLE)))
    now = time.time() if now is None else now
    with _memory_lock:
        store = _memory_copy(_memory_read())
        n = sum(f["person"] == person for f in store["facts"])
        store["facts"] = [f for f in store["facts"] if f["person"] != person]
        store["forgotten"][person] = now
        store["seq"] += 1
        store["at"] = now
        logs = [op for op in _memory_logs() if op.get("person") != person]
        _memory_atomic(os.path.join(DATA, "memory.log.jsonl"),
                       "".join(json.dumps(op, ensure_ascii=False)+"\n" for op in logs).encode())
        _memory_save(store, [{"t": now, "op": "forget", "person": person, "n": n}], forget=True)
        return n


def memory_forgotten():
    with _memory_lock:
        return dict(_memory_read()["forgotten"])


def memory_view(person=None, viewer=None, now=None, store=None):
    _memory_viewer(person, viewer)
    now = time.time() if now is None else now
    person = person or viewer
    store = memory_load() if store is None else store
    fields = ("id", "person", "kind", "about", "text", "strength", "status", "src", "n", "created", "confirmed")
    return [{**{k: f[k] for k in fields}, "strength": round(memory_strength(f, now), 2),
             "veto": _memory_veto_state(f, now),
             "veto_until": f["confirmed"]+MEMORY_VETO_DAYS*DAY if f["kind"] == "avoid" and f["src"] != "told" and f["confirmed"] is not None else None,
             "fades_in_days": max(0, math.ceil(MEMORY_HALF_LIFE*math.log2(max(memory_strength(f, now), MEMORY_FLOOR)/MEMORY_FLOOR))) if f["src"] != "told" else None}
            for f in store["facts"] if _memory_live(f) and (person is None or f["person"] == person)]


def memory_explain(fid, viewer=None, store=None):
    with _memory_lock:
        f = _memory_copy(_memory_find(_memory_read() if store is None else store, fid, viewer))
        f["log"] = [op for op in _memory_logs() if op.get("id") == fid][-20:]
        return f


def memory_cards(person, viewer=None, n=3, now=None):
    _memory_viewer(person, viewer)
    now = time.time() if now is None else now
    facts = [f for f in memory_load()["facts"] if f["person"] == person and f["status"] == "proposed"]
    facts.sort(key=lambda f: (-memory_strength(f, now), -f["n"], f["id"]))
    return [{"id": f["id"], "text": f["text"], "about": f["about"], "n": f["n"],
             "strength": round(memory_strength(f, now), 2)} for f in facts[:max(0, n)]]


def memory_why(key, kind, person, now=None):
    if person not in (*PEOPLE, "together"):
        return None
    now = time.time() if now is None else now
    with _memory_lock:
        store = _memory_read()
        if _memory_index.get("seq") != store["seq"]:
            _memory_index.clear()
            _memory_index.update(seq=store["seq"], people={p: {} for p in PEOPLE})
            for f in store["facts"]:
                if f["status"] == "active" and f["kind"] == "like":
                    _memory_index["people"][f["person"]].setdefault(f["about"], []).append(
                        (f["strength"], _memory_seq(f), f["id"], f["seen"], f["confirmed"] or 0, f["src"],
                         f["src"] == "told" or f["confirmed"] is not None))
        people = PEOPLE if person == "together" else (person,)
        if any(not _memory_index["people"][p] for p in people):
            return None
        held = {fid for fid, _ in facet_pairs(frec(key, kind) or {})}
        def best(p, about):
            options = _memory_index["people"][p].get(about, ())
            return max(((strength if src == "told" else strength*.5**(max(0, now-max(seen, confirmed))/DAY/MEMORY_HALF_LIFE),
                         -seq, fid, told) for strength, seq, fid, seen, confirmed, src, told in options), default=None)
        choices = []
        for about in held:
            if person == "together":
                matches = [best(p, about) for p in PEOPLE]
                if all(matches):
                    newest = max(matches, key=lambda a: a[1])
                    choices.append((min(a[0] for a in matches), newest[1], newest[2], newest[3], about))
            elif (a := best(person, about)):
                choices.append((*a, about))
        if not choices:
            return None
        strength, _, fid, told, about = max(choices, key=lambda c: (c[0], c[1], c[-1]))
        if strength < MEMORY_WHY_MIN:
            return None
    label = nice_facet(about, (get_model(False) or {}).get("names", {}).get(about) or about[2:].replace("_", " "))
    why = (("You both like " if len(PEOPLE) == 2 else "You all like ") if person == "together" else "Because you told me you like " if told else "You like ") + label
    if len(why) > 60 and person != "together":
        why = "You like " + label
    return {"why": why[:60], "fact": fid, "rung": "memory", "told": told, "strength": strength}


def memory_summary(since, now=None):
    now = time.time() if now is None else now
    with _memory_lock:
        store = _memory_read()
        out = {status: {p: sum(f["person"] == p and f["status"] == status for f in store["facts"]) for p in PEOPLE}
               for status in ("active", "proposed")}
        counts = dict.fromkeys(("proposed", "confirmed", "rejected", "ended", "refused"), 0)
        for op in _memory_logs():
            if not since <= op["t"] <= now:
                continue
            if op["op"] in ("add", "tell") and op.get("to") == "proposed":
                counts["proposed"] += 1
            elif op["op"] == "confirm":
                counts["confirmed"] += 1
            elif op["op"] == "reject":
                counts["rejected"] += 1
            elif op["op"] in ("supersede", "decay", "merge", "cap"):
                counts["ended"] += 1
            elif op["op"] == "refused":
                counts["refused"] += 1
        return {**out, "since": counts}


def memory_due(now):
    # The daily runner deduplicates nights; a tell must not postpone curate.
    return bool(_profiles.get("household"))


def memory_daily(now, m=None, dry_run=False):
    p, m = _profiles.get("household"), m or get_model(False)
    if not p or not m:
        return {"skipped": "no model"}
    evts = events(now, [it for it in read_history() if it.get("profile", "household") == "household"], read_trakt())
    r = curate(evts, m, now, stamp=p.get("hash"), dry_run=dry_run)
    counts = {}
    for op in r["ops"]:
        counts[op["op"]] = counts.get(op["op"], 0)+1
    return {"ops": counts, "refused": len(r["refused"]),
            "live": {p: r["stats"]["active"][p]+r["stats"]["proposed"][p] for p in PEOPLE},
            "active": r["stats"]["active"], "ms": r["stats"]["ms"], **({"store": r["store"]} if dry_run else {})}


def memory_job(now):
    r = memory_daily(now, dry_run=True)
    if not r.get("refused") and "skipped" not in r:
        r = memory_daily(now)
    if "skipped" in r:
        return r
    return {"ops": sum(r["ops"].values()), **r["ops"], "refused": r["refused"],
            **{f"active_{p}": r["active"][p] for p in PEOPLE}, "ms": int(r["ms"])}


# --- taste report ---
TASTE_RING_DAYS = 8
_taste_ring = {}
_taste_ring_lock = threading.Lock()


def taste_axes(m, person, n=5):
    aff = (m or {}).get("paff", {}).get(person, {}) if person else (m or {}).get("aff", {})
    return {types: [{"id": fid, "name": m["names"].get(fid, fid), "affinity": round(value, 4)}
                    for fid, value in sorted(aff.items(), key=lambda pair: (-pair[1], pair[0]))
                    if fid[0] in types][:n] for types in ("x", "kt", "d", "a", "g")}


def taste_ring_note(m, now):
    import zlib
    day = local_now(now).date().isoformat()
    with _taste_ring_lock:
        # Compressed numeric snapshots keep eight days below 20 KB without retaining models.
        _taste_ring[day] = {person: zlib.compress(json.dumps({fid: round(aff[fid], 4) for fid in
            sorted(sorted(aff), key=aff.get, reverse=True)[:40]}, separators=(",", ":")).encode())
            for person, aff in {"household": m["aff"], **{p: m["paff"].get(p, {}) for p in PEOPLE}}.items()}
        for old in sorted(_taste_ring)[:-TASTE_RING_DAYS]:
            del _taste_ring[old]
        path = os.path.join(DATA, "taste_ring.json")
        try:
            with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(json.dumps({day: {p: base64.b64encode(raw).decode() for p, raw in people.items()}
                                     for day, people in _taste_ring.items()}, separators=(",", ":")))
            os.replace(path + ".tmp", path)
        except OSError:
            pass


# --- searches ---------------------------------------------------------------

def log_search(query):
    if len(query) < 3:
        return
    now = time.time()
    if _searches and _searches[-1][1] == query and now - _searches[-1][0] < 30:
        return  # movie + series catalog requests of the same keystroke
    _searches.append((now, query))
    del _searches[:-500]
    try:
        with open(os.path.join(DATA, "searches.jsonl"), "a") as fh:
            fh.write(json.dumps({"t": int(now), "q": query}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_searches(n=15):
    """Final queries only: Odin searches per keystroke, so drop prefixes of a query typed right after."""
    items = list(_searches)
    out = []
    for i, (t, q) in enumerate(items):
        qc = q.casefold()
        if any(t2 - t <= 120 and q2.casefold().startswith(qc) for t2, q2 in items[i + 1:i + 60]):
            continue
        if qc not in (x.casefold() for x in out):
            out.append(q)
    return out[-n:]


# --- deterministic rows (no LLM) ---------------------------------------------

def with_art(path, lang, **params):
    """TMDB list endpoint in the UI language, plus English artwork by id."""
    jobs = {l: _pool.submit(tmdb, path, language=l, **params) for l in {lang, "en-US"}}
    data = jobs[lang].result().get("results", [])
    try:
        art = {it["id"]: it for it in jobs["en-US"].result().get("results", [])}
    except Exception:
        art = {}
    return data, art


def day_text(d, weekday=False):
    """'14 Oct', 'Tue 14 Oct', '3 Feb 2027' (year only when not this year)."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    out = f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if d.year != local_now().year else "")
    return f"{WD_SHORT[d.weekday()]} {out}" if weekday else out


def top_seeds(p, n=3):
    """Strongest recent positive titles; the because row rotates through them day by day."""
    cands = [e for e in p["entries"] if e["weight"] > 0 and e["signal"] in ("loved", "liked", "watched")] \
        or [e for e in p["entries"] if e["weight"] > 0]
    return sorted(cands, key=lambda e: -e["weight"] / (1 + e["age"] / 14))[:n]


def seed_phrase(e):
    if e["signal"] == "loved":
        return f"Because You Love {e['title']}"
    if e["signal"] == "liked":
        return f"Because You Like {e['title']}"
    if e["signal"] == "watched":
        return f"Because You {'Watched' if e['stype'] == 'movie' else 'Watch'} {e['title']}"
    return f"Because {e['title']} Caught Your Eye"


def because_feed(lang):
    p = _profiles.get("household")
    seeds = top_seeds(p, 8) if p else []
    if not seeds:
        return None
    seed = because_seed(p)
    stype, kind = seed["stype"], seed["kind"]
    genre_names = genres(kind, lang)
    data, art = [], {}
    for path in (f"{kind}/{seed['id']}/recommendations", f"{kind}/{seed['id']}/similar"):
        more, more_art = with_art(path, lang)
        data += more
        art.update(more_art)
    metas, seen = [], set()
    for it in data:
        key = f"tmdb:{it['id']}"
        if key in p["seen"] or key in p["negative"] or key in seen:
            continue
        seen.add(key)
        metas.append(row_meta(it, stype, kind, genre_names, art.get(it["id"], {}), note=f"Like {seed['title']}"))
    rank_by_taste(get_model(), [(x["id"], kind, x) for x in metas])
    metas = because_rank([x for x in metas if because_ok(x, seed["key"])], seed["key"])
    return {rid('because'): {stype: metas[:30]}, "_names": {rid('because'): {"title": seed_phrase(seed), "seed": seed["key"]}}}


def next_episode(det, last):
    """The first episode after last=(season, episode) that has aired, else None."""
    s, e = last
    counts = {x["season_number"]: x.get("episode_count") or 0 for x in det.get("seasons") or [] if x.get("season_number")}
    la = det.get("last_episode_to_air") or {}
    cand = (s, e + 1) if e < counts.get(s, 0) else (s + 1, 1) if counts.get(s + 1) else None
    if not cand or not la or cand > (la.get("season_number", 0), la.get("episode_number", 0)):
        return None
    return cand


def episodes_feed(lang):
    p = _profiles.get("household")
    if not p:
        return {"series": []}
    today = local_now().date()
    names = genres("tv", lang)
    shows = [e for e in p["entries"] if e["stype"] == "series" and e["signal"] in ("loved", "liked", "watched", "interest")
             and e["age"] <= 120]
    jobs = [(e, _pool.submit(tmdb, f"tv/{e['id']}", language=lang), _pool.submit(tmdb, f"tv/{e['id']}", language="en-US"))
            for e in shows]
    ranked = []
    for e, job, en_job in jobs:
        try:
            det, en = job.result(), en_job.result()
        except Exception:
            continue
        last = tuple(e["item"].get("last_episode") or ()) or None
        resume = tuple(e["item"].get("resume_episode") or ()) or None
        if not (last or resume):
            continue  # only opened, never watched: not "their" show yet
        la, na = en.get("last_episode_to_air") or {}, en.get("next_episode_to_air") or {}
        la_se = (la.get("season_number", 0), la.get("episode_number", 0))
        nxt = next_episode(en, last) if last else None
        fresh = la.get("air_date") and (today - datetime.date.fromisoformat(la["air_date"])).days <= 21
        note, rank = None, None
        if resume:
            note, rank = f"Continue S{resume[0]}E{resume[1]}", 1
        elif nxt and nxt == la_se and fresh:
            note, rank = f"New: S{nxt[0]}E{nxt[1]} since {day_text(la['air_date'], True)}", 0
        elif nxt and e["age"] <= 60:
            note, rank = f"Up next: S{nxt[0]}E{nxt[1]}", 2
        elif not nxt and na.get("air_date") and 0 <= (datetime.date.fromisoformat(na["air_date"]) - today).days <= 10:
            note, rank = f"New episode S{na['season_number']}E{na['episode_number']} on {day_text(na['air_date'], True)}", 1
        if note:
            ranked.append((rank, e["age"], row_meta(det, "series", "tv", names, en, note=note)))
    return {"series": [m for _, _, m in sorted(ranked, key=lambda x: (x[0], x[1]))]}


def top10_feed(lang):
    out = {}
    for stype, kind in (("movie", "movie"), ("series", "tv")):
        data, art = with_art(f"trending/{kind}/week", lang)
        names = genres(kind, lang)
        out[stype] = []
        for i, it in enumerate(data, 1):
            out[stype].append(row_meta(it, stype, kind, names, art.get(it["id"], {}),
                                      note=" · ".join([f"#{i} this week"] + match_note(f"tmdb:{it['id']}", kind=kind))))
    return out


def saga_name(col, en):
    """English collection name without the 'Collection' suffix."""
    name = en.get("name") or col.get("name") or ""
    return re.sub(r"\s*(Collection|Filmreihe|Sammlung)$", "", name).strip()


def details_pair(kind, tid, lang):
    jobs = [_pool.submit(tmdb, f"{kind}/{tid}", language=l) for l in (lang, "en-US")]
    return jobs[0].result(), jobs[1].result()


def rewatch_feed(lang):
    """Favourites not seen for a while, plus the household's sagas: the next part if one is under way, else part 1."""
    p = _profiles.get("household") or {"entries": []}
    today = local_now().date()
    played = {e["id"]: e for e in p["entries"] if e["signal"] in ("loved", "liked", "watched")}
    recent = {e["key"] for e in p["entries"] if e["age"] < 60}
    out = {"movie": [], "series": []}
    old = sorted((e for e in played.values() if e["age"] >= 90 and (e.get("who") == "tv" or e["kind"] == "movie"
                                                                     or {"comfort", "rewatch"} & set(e["flags"]))),
                 key=lambda e: (e["signal"] != "loved", -e["weight"], -e["age"]))[:36]
    if len(old) > 12:
        k = (today.toordinal() * 12) % len(old)
        old = old[k:] + old[:k]  # a different dozen of old favourites every day
    for e, job in [(e, _outer.submit(details_pair, e["kind"], e["id"], lang)) for e in old[:12]]:
        try:
            det, en = job.result()
        except Exception:
            continue
        months = int(e["age"] // 30)
        note = f"Last seen {months} months ago" if months < 24 else "Not seen in ages" if e["item"].get("at") \
            else "Seen before"
        if e.get("who") == "remote":  # taste remote: feedback time is not watch time.
            note = "Marked seen"
        out[e["stype"]].append(row_meta(det, e["stype"], e["kind"], genres(e["kind"], lang), en, note=note))
    cols = list((read_taste().get("household") or {}).get("rewatch_collections") or [])
    if cols:
        k = today.toordinal() % len(cols)
        cols = cols[k:] + cols[:k]  # a different saga leads every day
    jobs = [(c, _pool.submit(tmdb, f"collection/{c}", language=lang), _pool.submit(tmdb, f"collection/{c}", language="en-US"))
            for c in cols]
    names = genres("movie", lang)
    out["sagas"] = []  # every part of the named sagas: the AI half must not repeat them
    for c, job, en_job in jobs:
        try:
            col, en = job.result(), en_job.result()
        except Exception:
            continue
        parts = sorted((x for x in col.get("parts") or [] if x.get("release_date") and x["release_date"] <= today.isoformat()),
                       key=lambda x: x["release_date"])
        if not parts:
            continue
        art = {x["id"]: x for x in en.get("parts") or []}
        out["sagas"] += [f"tmdb:{x['id']}" for x in parts]
        saga = saga_name(col, en)
        done = [i for i, x in enumerate(parts) if x["id"] in played and played[x["id"]]["age"] <= 180]
        if done and done[-1] + 1 < len(parts):
            i = done[-1] + 1
            note = f"Next: part {i + 1} of {len(parts)} · {saga}"
        else:
            i = 0
            note = f"{saga} · {len(parts)} films" if len(parts) > 1 else saga
        if f"tmdb:{parts[i]['id']}" in recent and not done:
            continue
        out["movie"].append(row_meta(parts[i], "movie", "movie", names, art.get(parts[i]["id"], {}), note=note, collection=c))
    return out


def anilist(query, variables):
    req = urllib.request.Request("https://graphql.anilist.co", json.dumps({"query": query, "variables": variables}).encode(),
                                 {"Content-Type": "application/json", "Accept": "application/json",
                                  "User-Agent": "skald/" + VERSION})  # Cloudflare blocks urllib's default UA
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["data"]["Page"]["media"]
    except Exception:
        upstream_failed()
        raise


ANILIST_Q = """query($st:MediaStatus,$s:MediaSeason,$y:Int,$sort:[MediaSort]){Page(perPage:40){media(type:ANIME,status:$st,
season:$s,seasonYear:$y,sort:$sort,isAdult:false,format_in:[TV,TV_SHORT,ONA]){title{english romaji}
startDate{year month day} nextAiringEpisode{episode airingAt}}}}"""


def resolve_anime(titles, lang):
    for title in titles:
        if not title:
            continue
        clean = title
        while True:
            stripped = SEASON_SUFFIX.sub("", clean).strip()
            if stripped == clean or not stripped:
                break
            clean = stripped
        data, art = search_both("tv", clean, lang)
        anime = [it for it in data if 16 in it.get("genre_ids", [])]
        if anime:
            return anime[0], art.get(anime[0]["id"], {})
    return None


def anime_feed(lang, upcoming):
    d = local_now()
    if upcoming:
        s, y = anime_season(d, 1)
        media = anilist(ANILIST_Q, {"st": "NOT_YET_RELEASED", "s": s, "y": y, "sort": ["POPULARITY_DESC"]})
    else:
        media = anilist(ANILIST_Q, {"st": "RELEASING", "sort": ["TRENDING_DESC"]})
        media = [m for m in media if m.get("nextAiringEpisode")]
    names = genres("tv", lang)
    jobs = [(m, _outer.submit(resolve_anime, [m["title"].get("english"), m["title"].get("romaji")], lang)) for m in media]
    metas, seen = [], set()
    for m, job in jobs:
        try:
            hit = job.result()
        except Exception:
            continue
        if not hit or hit[0]["id"] in seen:
            continue
        seen.add(hit[0]["id"])
        nxt = m.get("nextAiringEpisode")
        if nxt:
            when = local_now(nxt["airingAt"])
            note = f"Ep {nxt['episode']} · {WD_SHORT[when.weekday()]} {when.day} {MONTHS[when.month - 1]}, {when:%H:%M}"
        else:
            sd = m.get("startDate") or {}
            note = ("Starts " + (day_text(datetime.date(sd["year"], sd["month"], sd["day"])) if sd.get("day") and sd.get("month")
                                 else f"{MONTHS[sd['month'] - 1]} {sd['year']}" if sd.get("month") else str(sd["year"]))
                    if sd.get("year") else "Coming soon")
        en = dict(hit[1])
        if not re.search(r"[A-Za-z]{2}", display_name(hit[0], en)):
            en["name"] = m["title"].get("english") or m["title"].get("romaji")
        metas.append(row_meta(hit[0], "series", "tv", names, en, note=note))
    return {"series": taste_order(metas[:30], why=False)}


# --- coming up for them ---------------------------------------------------------------
# New seasons of their shows, the next part of their sagas and new anime seasons, dated.

ANILIST_SEARCH = ("{alias}:Page(perPage:4){{media(search:{q},type:ANIME,isAdult:false,status_in:[NOT_YET_RELEASED,RELEASING],"
                  "sort:[SEARCH_MATCH]){{format status title{{english romaji}} startDate{{year month day}} "
                  "nextAiringEpisode{{episode airingAt}}}}}}")


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def base_title(s):
    while True:
        stripped = SEASON_SUFFIX.sub("", s).strip()
        if stripped == s or not stripped:
            return norm_title(s)
        s = stripped


def date_or_none(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def show_upcoming(e, lang, today):
    det, en = details_pair("tv", e["id"], lang)
    if (en.get("status") or "") in ("Ended", "Canceled"):
        return None
    na, la = en.get("next_episode_to_air") or {}, en.get("last_episode_to_air") or {}
    nad, lad = date_or_none(na.get("air_date")), date_or_none(la.get("air_date"))
    for s in sorted(en.get("seasons") or [], key=lambda x: x.get("season_number") or 0):
        sd = date_or_none(s.get("air_date"))
        if not s.get("season_number") or not sd:
            continue
        if today < sd <= today + datetime.timedelta(days=180):
            return 1, sd, row_meta(det, "series", "tv", genres("tv", lang), en, note=f"Season {s['season_number']} from {day_text(sd)}")
        if today - datetime.timedelta(days=21) <= sd <= today and s["season_number"] > (e["item"].get("last_episode") or [0])[0]:
            return 0, sd, row_meta(det, "series", "tv", genres("tv", lang), en, note=f"New season {s['season_number']} since {day_text(sd, True)}")
    if nad and na.get("episode_number") == 1 and today < nad <= today + datetime.timedelta(days=180):
        return 1, nad, row_meta(det, "series", "tv", genres("tv", lang), en, note=f"Season {na['season_number']} from {day_text(nad)}")
    if nad and lad and (nad - lad).days > 60 and 0 <= (nad - today).days <= 14:
        return 1, nad, row_meta(det, "series", "tv", genres("tv", lang), en, note=f"Back on {day_text(nad, True)}")
    return None


def saga_upcoming(cid, lang, today, seen):
    col, en = (_pool.submit(tmdb, f"collection/{cid}", language=l) for l in (lang, "en-US"))
    col, en = col.result(), en.result()
    saga = saga_name(col, en)
    art, names, out = {x["id"]: x for x in en.get("parts") or []}, genres("movie", lang), []
    for x in col.get("parts") or []:
        d = date_or_none(x.get("release_date"))
        if f"tmdb:{x['id']}" in seen or not d:
            continue
        if today < d <= today + datetime.timedelta(days=365):
            out.append((1, d, row_meta(x, "movie", "movie", names, art.get(x["id"], {}), note=f"In cinemas {day_text(d)} · {saga}", collection=cid)))
        elif today - datetime.timedelta(days=90) <= d <= today:
            out.append((0, d, row_meta(x, "movie", "movie", names, art.get(x["id"], {}), note=f"Just released · {saga}", collection=cid)))
    return out


def anime_upcoming(entries, lang, today):
    """New seasons and films of their anime, found by title on AniList (one batched query per 10 titles)."""
    out = []
    for i in range(0, len(entries), 10):
        chunk = entries[i:i + 10]
        q = "{" + " ".join(ANILIST_SEARCH.format(alias=f"a{j}", q=json.dumps(base_title(e["title"])))
                           for j, e in enumerate(chunk)) + "}"
        req = urllib.request.Request("https://graphql.anilist.co", json.dumps({"query": q}).encode(),
                                     {"Content-Type": "application/json", "Accept": "application/json",
                                      "User-Agent": "skald/" + VERSION})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)["data"]
        for j, e in enumerate(chunk):
            base = base_title(e["title"])
            for m in (data.get(f"a{j}") or {}).get("media") or []:
                names = [norm_title(m["title"].get(k)) for k in ("english", "romaji")]
                if len(base) < 4 or not any(base in n for n in names if n):
                    continue
                sd = m.get("startDate") or {}
                start = datetime.date(sd["year"], sd.get("month") or 12, sd.get("day") or 28) if sd.get("year") else None
                if m["status"] == "RELEASING" and (not start or (today - start).days > 45):
                    continue  # long-running and weekly: the episodes row covers it
                when = start or today + datetime.timedelta(days=365)
                if sd.get("day") and sd.get("month"):
                    label = day_text(start)
                elif sd.get("month"):
                    label = f"{MONTHS[sd['month'] - 1]} {sd['year']}"
                elif sd.get("year"):
                    label = str(sd["year"])
                else:
                    label = None
                if m["format"] == "MOVIE":
                    hit = resolve_pick("movie", m["title"].get("english") or m["title"].get("romaji"), None, lang)
                    if hit and f"tmdb:{hit[0]['id']}" not in e.get("seen", ()):
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(hit[0], "movie", "movie", genres("movie", lang), hit[1],
                                            note=f"Anime film · {'out ' if start and start <= today else ''}{label}"
                                            if label else "Anime film announced")))
                elif m["format"] in ("TV", "TV_SHORT", "ONA") and e["kind"] == "tv":
                    note = ("New season announced" if not label else
                            f"New season {'airing since ' if start and start <= today else 'from '}{label}")
                    raw = [t for t in (m["title"].get("english"), m["title"].get("romaji")) if t]
                    if base in {base_title(t) for t in raw}:  # "<their show> Season N": same show
                        det, en = details_pair("tv", e["id"], lang)
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(det, "series", "tv", genres("tv", lang), en, note=note)))
                        break
                    # A spin-off or remake ("Dragon Ball Super: Beerus", "NARUTO (2026)") is its own show:
                    # show that show, never pin the news onto the one they watched. No confident match, no card.
                    hit = next((h for h in (resolve_pick("tv", base_title(t), sd.get("year"), lang) for t in raw) if h), None)
                    if hit and str(hit[0]['id']) != str(e["id"]):
                        if f"tmdb:{hit[0]['id']}" not in e.get("seen", ()):
                            note = (f"New series {'airing since ' if start and start <= today else 'from '}{label}"
                                    if label else "New series announced") + f" · from the {e['title']} world"
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(hit[0], "series", "tv", genres("tv", lang), hit[1], note=note)))
                break
    return out


def movie_collections(p):
    """TMDB collection ids of the sagas they watch: from their movies plus taste.json's rewatch_collections."""
    movies = [e for e in p["entries"] if e["weight"] > 0 and e["stype"] == "movie"][:150]
    cols = {int(c) for c in (read_taste().get("household") or {}).get("rewatch_collections") or []}
    for job in [_pool.submit(tmdb, f"movie/{e['id']}", language="en-US") for e in movies]:
        try:
            c = job.result().get("belongs_to_collection")
        except Exception:
            continue
        if c:
            cols.add(c["id"])
    return cols


def soon_for_taste(p, lang, today, have):
    """Releases of the next 90 days nobody of theirs is attached to, kept when they fit the taste model."""
    m = get_model()
    if not m or not m.get("cal"):
        return []
    end = (today + datetime.timedelta(days=90)).isoformat()
    start = (today + datetime.timedelta(days=1)).isoformat()
    srcs = [("movie", "movie", _outer.submit(with_art, "discover/movie", lang, sort_by="popularity.desc",
                                            **{"primary_release_date.gte": start, "primary_release_date.lte": end,
                                               "with_release_type": "2|3|4", "include_adult": "false"})),
            ("series", "tv", _outer.submit(with_art, "discover/tv", lang, sort_by="popularity.desc",
                                          **{"first_air_date.gte": start, "first_air_date.lte": end,
                                             "include_adult": "false", "without_genres": "10763,10764,10767"}))]
    items = []
    for stype, kind, job in srcs:
        try:
            data, art = job.result()
        except Exception:
            continue
        names = genres(kind, lang)
        for it in data[:20]:
            key = f"tmdb:{it['id']}"
            d = date_or_none(it.get("release_date") if kind == "movie" else it.get("first_air_date"))
            if key in p["seen"] or key in have or not d:
                continue
            items.append((key, kind, (d, row_meta(it, stype, kind, names, art.get(it["id"], {})))))
    for meta in ((_feeds.get((rid('nextseason'), lang)) or {}).get("metas") or {}).get("series", []):
        if meta["id"] not in p["seen"] and meta["id"] not in have:
            items.append((meta["id"], "tv", (None, meta)))
    out = []
    for s_, why, key, kind, (d, meta) in rank_by_taste(m, items):
        pct = match_pct(m, s_)
        if pct is None or pct < 80:
            continue
        original, original_overview = card_parts(meta)
        original = [x for x in original if x not in ("Strong fit", "Good fit", "Wildcard")]
        what = (f"Out {day_text(d)}" if kind == "movie" else f"Premieres {day_text(d)}") if d else \
            (original[0] if original else "Coming soon")
        note = " · ".join([what] + reasons(m, key, why, kind=kind))
        overview = (meta.get("description") or "") if d else original_overview
        out.append({**meta, "description": " · ".join(x for x in (note, overview) if x)})
    return out[:15]


def upcoming_feed(lang):
    """Coming Soon for You: what is coming next from their shows, sagas and anime, then new releases that fit their taste."""
    p = _profiles.get("household")
    if not p:
        return {"tv": []}
    today = local_now().date()
    pos = [e for e in p["entries"] if e["weight"] > 0 and e["signal"] in ("loved", "liked", "watched")]
    shows = [e for e in pos if e["stype"] == "series" and "anime" not in e["flags"]][:80]
    anime = [{**e, "seen": p["seen"]} for e in pos if "anime" in e["flags"] or
             (e["stype"] == "series" and "Animation" in e["genres"] and e.get("who") == "tv")][:30]
    found = []
    jobs = [_outer.submit(show_upcoming, e, lang, today) for e in shows]
    jobs += [_outer.submit(saga_upcoming, c, lang, today, p["seen"]) for c in movie_collections(p)]
    jobs.append(_outer.submit(anime_upcoming, anime, lang, today))
    for job in jobs:
        try:
            r = job.result()
        except Exception as e:
            print(f"upcoming: {type(e).__name__}: {e}", flush=True)
            continue
        found += [r] if isinstance(r, tuple) else r or []
    best = {}
    for bucket, when, meta in found:  # one entry per title: the soonest / freshest wins
        meta = {**meta, "_row": {**(meta.get("_row") or {}), "when": when.isoformat()}}
        if not upcoming_ok(meta):
            continue
        cur = best.get(meta["id"])
        if not cur or (bucket, when) < cur[:2]:
            best[meta["id"]] = (bucket, when, meta)
    ranked = sorted(best.values(), key=lambda x: (x[0], -x[1].toordinal() if x[0] == 0 else x[1].toordinal()))
    own_now = [m for b, _, m in ranked if b == 0]
    own_soon = [m for b, _, m in ranked if b == 1]
    try:
        picks = soon_for_taste(p, lang, today, set(best))
    except Exception as e:
        print(f"upcoming taste: {type(e).__name__}: {e}", flush=True)
        picks = []
    # Theirs first (just out), then their announced titles alternating with new releases picked for their taste.
    return {"tv": polish_row([x for x in own_now + interleave(own_soon, picks) if upcoming_ok(x)], cap=1)[:30]}


# --- catch up --------------------------------------------------------------------------
# Shows they liked that moved on without them (new seasons, or stopped halfway) and saga films they skipped.
# Shows they are actively watching (last 60 days) belong to the episodes row, not here.

def ep_index(det, season, episode):
    counts = {x["season_number"]: x.get("episode_count") or 0 for x in det.get("seasons") or [] if x.get("season_number")}
    if season not in counts:
        return None
    return sum(c for n, c in counts.items() if n < season) + episode


def show_catchup(e, lang, today):
    det, en = details_pair("tv", e["id"], lang)
    last = tuple(e["item"].get("last_episode") or ())
    la = en.get("last_episode_to_air") or {}
    if len(last) != 2 or not la.get("season_number"):
        return None
    seen_i, aired_i = ep_index(en, *last), ep_index(en, la["season_number"], la.get("episode_number") or 0)
    if seen_i is None or aired_i is None:
        return None
    behind = aired_i - seen_i
    played = e["item"].get("episodes_played")
    if behind <= 0 or behind > 400 or (played and seen_i > 1.5 * played + 30):
        return None  # caught up, an endless show, or their numbering does not match TMDB's
    counts = {x["season_number"]: x.get("episode_count") or 0 for x in en.get("seasons") or [] if x.get("season_number")}
    absolute = last[0] == 1 and last[1] > counts.get(1, 0)
    nxt = (1, last[1] + 1) if absolute else next_episode(en, last)
    if not nxt:
        return None
    lad = date_or_none(la.get("air_date"))
    fresh = any(s.get("season_number") and date_or_none(s.get("air_date")) and
                0 <= (today - date_or_none(s["air_date"])).days <= 90 for s in en.get("seasons") or [])
    finished_season = not absolute and last[1] >= counts.get(last[0], 0)
    if finished_season and la["season_number"] > last[0]:
        new = list(range(last[0] + 1, la["season_number"] + 1))
        what = f"Season {new[0]} is out" if len(new) == 1 else f"Seasons {new[0]}–{new[-1]} are out"
        note = f"{what} · {behind} new episode{'s' if behind > 1 else ''}"
    else:
        note = f"{behind} episode{'s' if behind > 1 else ''} to go · from {episode_label(en, *nxt)}"
    return (0 if fresh else 1, -(lad.toordinal() if fresh else e["weight"]),
            row_meta(det, "series", "tv", genres("tv", lang), en, note=note))


def saga_catchup(cid, lang, today, p):
    col, en = (_pool.submit(tmdb, f"collection/{cid}", language=l) for l in (lang, "en-US"))
    col, en = col.result(), en.result()
    parts = sorted((x for x in col.get("parts") or [] if date_or_none(x.get("release_date"))
                    and date_or_none(x["release_date"]) <= today), key=lambda x: x["release_date"])
    seen = [i for i, x in enumerate(parts) if f"tmdb:{x['id']}" in p["seen"] and f"tmdb:{x['id']}" not in p["negative"]]
    if not seen:
        return []
    saga, art, names, out = saga_name(col, en), {x["id"]: x for x in en.get("parts") or []}, genres("movie", lang), []
    for i, x in enumerate(parts):
        if i <= seen[0] or f"tmdb:{x['id']}" in p["seen"]:
            continue
        d = date_or_none(x["release_date"])
        fresh = (today - d).days <= 365
        out.append((0 if fresh else 1, -d.toordinal() if fresh else i,
                    row_meta(x, "movie", "movie", names, art.get(x["id"], {}),
                            note=f"You skipped part {i + 1} of {len(parts)} · {saga}", collection=cid)))
        break  # the first one they missed; the next shows up once they watched it
    return out


def catchup_feed(lang):
    p = _profiles.get("household")
    if not p:
        return {"tv": []}
    today = local_now().date()
    shows = [e for e in p["entries"] if e["stype"] == "series" and e["signal"] in ("loved", "liked", "watched") and e["weight"] > 0
             and e["item"].get("last_episode") and e["age"] > 60][:120]
    jobs = [_outer.submit(show_catchup, e, lang, today) for e in shows]
    jobs += [_outer.submit(saga_catchup, c, lang, today, p) for c in movie_collections(p)]
    found = []
    for job in jobs:
        try:
            r = job.result()
        except Exception as e:
            print(f"catchup: {type(e).__name__}: {e}", flush=True)
            continue
        found += [r] if isinstance(r, tuple) else r or []
    found.sort(key=lambda x: (x[0], x[1]))
    return {"tv": [m for *_, m in found][:20]}


# --- theme lanes ---------------------------------------------------------------------------
# Three rows built from what their taste is made of: a keyword ("Time Travel", "Based on Manga"), a person
# (director, creator or actor) and a network or studio. Which themes get a row is a small bandit: the
# facet's affinity, times how often its row's titles were actually played (lane:<facet> in learn.json).
# A lane shown for a week without a play fades, the current lanes are sticky for two weeks, and a lane
# that was gone for three weeks starts fresh.

LANE_KINDS = {"x": "feeling", "k": "theme", "d": "person", "a": "person", "n": "brand", "c": "brand"}
LANE_CAP = {"feeling": 2, "theme": 2, "person": 1, "brand": 1}
LANE_TEMPLATE = {"d": "From {}", "a": "Starring {}", "n": "{} Picks for You", "c": "From {}"}
LANE_SYS = ("You name rows on a streaming home screen. The row holds movies and series sharing one TMDB keyword. "
            "Reply with only the row title: 2 to 5 English words that name that theme plainly, Title Case, no quotes, "
            "no emoji. Examples: 'time travel' -> Time Travel Stories; 'based on manga' -> Manga Adaptations; "
            "'heist' -> Heists & Big Scores; 'marvel cinematic universe (mcu)' -> More from the MCU.")
# Brand rows only for names people choose by; a local broadcaster (MBS, Fuji TV) is a reason, not a row.
LANE_BRANDS = {"HBO", "HBO Max", "Netflix", "Apple TV+", "Disney+", "Prime Video", "Amazon", "Hulu", "FX", "AMC",
               "Showtime", "Paramount+", "Peacock", "Crunchyroll", "BBC One", "BBC Two", "Starz", "A24", "Pixar",
               "Studio Ghibli", "Marvel Studios", "Lucasfilm Ltd.", "Legendary Pictures", "Blumhouse Productions",
               "DC Studios", "Toho", "MAPPA", "ufotable", "Kyoto Animation", "Wit Studio", "Bones", "Madhouse",
               "Production I.G", "CloverWorks", "Studio Trigger", "Toei Animation", "Sunrise", "Science SARU"}
NO_TALK = {10763, 10764, 10767, 99}  # news, reality, talk, documentary


def lane_member(meta, fid):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    return fid in {f for f, _ in facet_pairs(rec)}


def lane_allowed(metas, title):
    return any(w in title.casefold() for w in ("anime", "manga")) or sum(
        "Animation" in row_facts(x)["g"] for x in metas) <= len(metas) / 2


class LaneRow(list):
    def __init__(self, metas, names):
        super().__init__(metas)
        self.names = names


def lane_titles(fid, lang):
    """[(key, kind, meta)] of a theme from TMDB discover or the person's credits, or of a feeling (experience)."""
    t, xid = fid.split(":", 1)
    if t == "x":
        return feeling_pool(get_model(False), _profiles.get("household"), [xid], lang, seeds=10)
    pairs = []
    if t in "kc":
        f = {"with_keywords": xid} if t == "k" else {"with_companies": xid}
        for kind, votes in (("movie", 200), ("tv", 50)):
            for sort in ("popularity.desc", "vote_count.desc"):
                for page in (1, 2):
                    pairs.append((kind, _outer.submit(with_art, f"discover/{kind}", lang, sort_by=sort, page=page,
                                                      **{**f, "vote_count.gte": votes, "include_adult": "false"})))
    elif t == "n":
        for sort in ("popularity.desc", "vote_count.desc"):
            pairs.append(("tv", _outer.submit(with_art, "discover/tv", lang, sort_by=sort,
                                             with_networks=xid, **{"vote_count.gte": 40})))
    out, seen = [], set()
    for kind, job in pairs:
        try:
            data, art = job.result()
        except Exception:
            continue
        names = genres(kind, lang)
        for it in data:
            key = f"tmdb:{it['id']}"
            if key not in seen and not NO_TALK & set(it.get("genre_ids") or []):
                seen.add(key)
                out.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}))))
    if t in "da":
        jobs = {l: _pool.submit(tmdb, f"person/{xid}/combined_credits", language=l) for l in {lang, "en-US"}}
        data, en = jobs[lang].result(), jobs["en-US"].result()
        art = {(x.get("media_type"), x["id"]): x for x in (en.get("cast") or []) + (en.get("crew") or [])}
        if t == "a":
            xs = [x for x in data.get("cast") or [] if (x.get("order") if x.get("order") is not None else 99) <= 8
                  and (x.get("media_type") == "movie" or (x.get("episode_count") or 0) >= 4)]
        else:
            xs = [x for x in data.get("crew") or [] if x.get("job") in ("Director", "Creator", "Screenplay", "Writer")]
        xs.sort(key=lambda x: -(x.get("vote_count") or 0))
        for x in xs:
            kind = "movie" if x.get("media_type") == "movie" else "tv"
            key = f"tmdb:{x['id']}"
            if key in seen or NO_TALK & set(x.get("genre_ids") or []) or (x.get("vote_count") or 0) < (100 if kind == "movie" else 30):
                continue
            seen.add(key)
            out.append((key, kind, row_meta(x, "movie" if kind == "movie" else "series", kind, genres(kind, lang),
                                           art.get((x.get("media_type"), x["id"]), {}))))
    return [x for x in out if quality_ok(x[2]) and tone_ok(x[2])]


def lane_title(fid, name, examples):
    if fid[0] == "x":
        return EXPERIENCES[fid[2:]][1]
    if fid[0] != "k" or not OR_KEY:
        return LANE_TEMPLATE.get(fid[0], "{}").format(name)
    try:
        raw = llm_text(LANE_SYS, f"Keyword: '{name}'", 30, 20, "feed", "lane-title").strip().strip('"\'').splitlines()[0].strip()
        if 3 <= len(raw) <= 40:
            return raw
    except Exception as e:
        print(f"lane title: {type(e).__name__}: {e}", flush=True)
    return " ".join(w if w in ("on", "of", "a", "the", "and", "in") else w[:1].upper() + w[1:] for w in name.split())


def lanes_feed(lang):
    p = _profiles.get("household")
    m = get_model()
    if not p or not m:
        return None
    today = local_now().date()
    lanes = _learn.setdefault("lanes", {})
    with _learn_lock:
        stats = {k: dict(v) for k, v in _learn["rows"].items() if k.startswith("lane:")}
    cur = {((_feeds.get((cid, lang)) or {}).get("names") or {}).get("lane"): cid for cid in LANES}

    def priority(fid, aff):
        st = lanes.get(fid) or {}
        last = date_or_none(st.get("last"))
        if last and (today - last).days > 21:  # gone for three weeks: a fresh start
            st, _ = {}, stats.pop(f"lane:{fid}", None)
            with _learn_lock:
                _learn["rows"].pop(f"lane:{fid}", None)
            lanes.pop(fid, None)
        since = date_or_none(st.get("since"))
        days = (today - since).days if since else 0
        hits = (stats.get(f"lane:{fid}") or {}).get("hits", 0)
        pr = aff * (1 + 4 * (hits + 0.5) / (days + 5))
        if days >= 7 and not hits:
            pr *= 0.3
        if fid in cur and days < (14 if hits else 7):
            pr *= 1.25
        return pr

    seed = because_seed(p)
    seed_facets = {f for f, _ in facet_pairs(frec(seed["key"], seed["kind"]))} if seed else set()
    def skip_lane(fid):
        name = m["names"].get(fid, "").casefold()
        return name in ("animated", "animation") or (fid in seed_facets and
               ("superhero" in name or "cinematic universe" in name or fid.startswith("collection:")))

    cands = [(fid, a) for fid, a in m["aff"].items() if a > 0 and fid[0] in LANE_KINDS
             and not skip_lane(fid) and speaks(fid, m["names"][fid]) and (fid[0] not in "nc" or m["names"][fid] in LANE_BRANDS)]
    # Experience affinity is a lift on its own scale: stretch it so the strongest feeling meets the strongest theme.
    top_x = max((a for fid, a in cands if fid[0] == "x"), default=0)
    top_o = max((a for fid, a in cands if fid[0] != "x"), default=0)
    xscale = top_o / top_x if top_x and top_o else 1.0
    themes = sorted(((priority(fid, a * xscale if fid[0] == "x" else a), fid) for fid, a in cands), reverse=True)
    exclude = p["seen"] | p["negative"]
    front = set()
    for cid in DEDUPE_ORDER[:DEDUPE_ORDER.index(LANES[0])]:
        for typ in (("movie", "series") if cid == rid('top10') else ("tv",) if cid in MIXED else ("series",)):
            front = mark_above(front, cid, row_list(cid, typ, lang, built=True))
    chosen, used, tries, taken, hero_lane = [], {}, 0, set(), False
    local = list(row_pool(lang))
    for pr, fid in themes:
        if len(chosen) == len(LANE_ROWS) or tries >= 40:
            break
        kind = LANE_KINDS[fid[0]]
        if len(chosen) < len(LANES) and used.get(kind, 0) >= LANE_CAP[kind]:
            continue
        tries += 1
        try:
            items = [x for x in lane_titles(fid, lang) if x[0] not in exclude]
        except Exception as e:
            print(f"lane {fid}: {type(e).__name__}: {e}", flush=True)
            items = []
        cached = [(x["id"], "movie" if x["type"] == "movie" else "tv", x) for x in local
                  if x["id"] not in exclude and lane_member(x, fid)]
        ids = {x[0] for x in items}
        items += [x for x in cached if x[0] not in ids]
        tired = tired_keys({f"lane:{fid}"})
        items = [x for x in items if x[0] not in tired]
        need = 16  # Snapshot lanes lost 4-6 cards after build; keep ten beyond that loss.
        if len(items) < need:
            print(f"lane {fid}: only {len(items)} unseen", flush=True)
            continue
        ranked = [(sc, x) for sc, _, _, _, x in rank_by_taste(m, items)]
        metas = [with_match(x) for sc, x in ranked if (sc is None or match_pct(m, sc) is None or match_pct(m, sc) >= 55)
                 and lane_member(x, fid) and row_eligible(x, LANES[0], lang, p)]
        reserve = list(metas)
        superhero = sum(is_hero(x) for x in metas) > len(metas) / 2
        if len(chosen) < len(LANES) and superhero and hero_lane:
            continue
        fresh = list({x["id"]: x for x in metas if x["id"] not in taken | front}.values())[:ROW_MAX]
        title = (lanes.get(fid) or {}).get("title") or lane_title(fid, m["names"][fid], [x["name"] for x in fresh])
        if len(fresh) < need or not lane_allowed(fresh, title):
            print(f"lane {fid}: {len(fresh)} fresh of {len(metas)}", flush=True)
            continue
        hero_lane |= superhero
        used[kind] = used.get(kind, 0) + 1
        taken |= {x["id"] for x in fresh}
        chosen.append((fid, fresh, reserve, title))
    slots, free = {}, [c for c in LANES]
    for fid, metas, reserve, title in chosen[:len(LANES)]:  # a lane keeps its row position while it stays
        if cur.get(fid) in free:
            slots[cur[fid]] = (fid, metas, reserve, title)
            free.remove(cur[fid])
    for fid, metas, reserve, title in chosen[:len(LANES)]:
        if fid not in [f for f, *_ in slots.values()]:
            slots[free.pop(0)] = (fid, metas, reserve, title)
    slots.update(zip(LANE_BENCH, chosen[len(LANES):]))
    out, names = {}, {}
    for cid in LANE_ROWS:
        if cid not in slots:
            out[cid] = {"tv": []}
            continue
        fid, metas, reserve, title = slots[cid]
        st = lanes.get(fid) or {}
        if not st.get("title"):
            st["title"] = title
            st["since"] = today.isoformat()
        st["last"] = today.isoformat()
        lanes[fid] = st
        _learn_dirty.set()
        out[cid] = {"tv": metas, "pool": reserve}
        names[cid] = {"title": st["title"], "lane": fid}
    print("lanes: " + ", ".join(f"{n['title']} ({n['lane']})" for n in names.values()), flush=True)
    return {**out, "_names": names}


# --- feeling rows (experience tags, no LLM beyond the one-off tagging) ---------------------------------------
# Same Feeling, New World: one loved title a day (rotating members / together), and titles that give the same
# experience from a different world. For each member: their loves and the shared TV. Four mood shelves: experience bundles,
# chosen once per evening by Luna (what fits tonight: day, season, recent plays, what they respond to), blended
# with taste; without Luna, a taste ranking with a weekday lean (Friday epic, Sunday cozy) rotates daily.

MOODS = {
    "cozy": ("Cozy Case Night", ["cozy_case", "banter_duo", "will_they", "feel_good", "nostalgia"], "0123456"),
    "edge": ("Edge of Your Seat", ["whodunit", "dread", "mind_bender", "survival", "heist"], "01234"),
    "epic": ("Epic Saga Night", ["epic_quest", "magic_wonder", "dynasty_power", "war_sacrifice", "team_spectacle"], "4 5"),
    "heart": ("Feel Everything", ["tearjerker", "grief_healing", "second_chance", "found_family", "heartwarming"], "6"),
    "romance": ("Date Night Romance", ["slow_romance", "period_society", "will_they", "forbidden_love"], "45"),
    "laugh": ("Just Laugh Tonight", ["feel_good", "dark_comedy", "slice_of_life", "banter_duo"], "0123"),
    "grit": ("Grit & Glory", ["frontier_grit", "antihero", "revenge", "underdog", "sports_glory"], "56"),
    "ideas": ("Big Ideas Night", ["big_ideas", "time_twist", "dystopia", "mind_bender"], "3456"),
    "power": ("Power & Intrigue", ["political_intrigue", "dynasty_power", "antihero", "family_secrets"], "0123456"),
    "true": ("True Stories That Inspire", ["true_story", "underdog", "passion_art", "sports_glory", "competence"], "0123456"),
    "home": ("Growing Up & Found Family", ["coming_of_age", "found_family", "mentor_bond", "slice_of_life", "heartwarming"], "56"),
    "dark": ("Dark & Twisted", ["antihero", "dark_comedy", "revenge", "dread", "family_secrets"], "45"),
}
MOOD_LEVELS = ["wrong for tonight", "could work", "good fit", "perfect for tonight"]


def xsim(a, b):
    """Cosine of two experience-tag dicts."""
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / ((sum(v * v for v in a.values()) * sum(v * v for v in b.values())) ** 0.5 or 1.0)


def feeling_pool(m, p, xs, lang, seeds=6, extra=(), seed=None):
    """[(key, kind, meta)] of unseen titles that clearly carry one of the experiences xs, from TMDB recommendations
    of the loved titles carrying them (and of extra seed keys)."""
    if not m or not p:
        return []
    kinds = {e["key"]: e["kind"] for e in p["entries"]}
    src = []
    for x in xs:
        src += [k for _, k in m["holders"].get(f"x:{x}", [])[:seeds]]
    src = [k for k in dict.fromkeys(list(extra) + src) if k in kinds][:seeds * 2]
    jobs = [(kinds[k], _outer.submit(with_art, f"{kinds[k]}/{k[5:]}/recommendations", lang)) for k in src]
    exclude = p["seen"] | p["negative"]
    items, seen = [], set()
    for kind, job in jobs:
        try:
            data, art = job.result()
        except Exception:
            continue
        names = genres(kind, lang)
        for it in data:
            key = f"tmdb:{it['id']}"
            if key in seen or key in exclude or NO_TALK & set(it.get("genre_ids") or []) \
                    or (it.get("vote_count") or 0) < (200 if kind == "movie" else 50):
                continue
            seen.add(key)
            items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}))))
    try:
        ensure_facets([(k, kind) for k, kind, _ in items])
    except Exception as e:
        print(f"feeling pool: {type(e).__name__}: {e}", flush=True)
    tag_experiences([(k, kind) for k, kind, _ in items], max_batches=3)
    return [x for x in items if quality_ok(x[2]) and tone_ok(x[2], seed)
            and any(frec(x[0], x[1]).get("x", {}).get(y, 0) >= 2 for y in xs)]


def day_index():
    return (local_now() - datetime.timedelta(hours=5)).date().toordinal()


def leap_feed(lang):
    p, m = _profiles.get("household"), get_model()
    if not p or not m:
        return None
    who = (*PEOPLE, "together")[day_index() % (len(PEOPLE) + 1)]
    cands = [e for e in p["entries"] if e["signal"] == "loved" and frec(e["key"], e["kind"]).get("x")
             and (who in e.get("by", ()))]
    cands = sorted(cands, key=lambda e: -e["weight"])[:7] or \
        sorted((e for e in p["entries"] if e["weight"] > 0 and frec(e["key"], e["kind"]).get("x")), key=lambda e: -e["weight"])[:7]
    if not cands:
        return None
    a = cands[(day_index() // 3) % len(cands)]
    ax = frec(a["key"], a["kind"])["x"]
    top = [x for x, _ in sorted(ax.items(), key=lambda kv: -kv[1])][:3]
    pool = feeling_pool(m, p, top, lang, seeds=8, extra=[a["key"]], seed=a["key"])
    pool = [x for x in pool if tone_ok(x[2], a["key"], animation=True)]
    av = femb(a["key"], a["kind"])
    try:
        ensure_emb([(k, kind) for k, kind, _ in pool])
    except BudgetExceeded:
        pass
    scored = []
    for key, kind, meta in pool:
        xs = frec(key, kind).get("x") or {}
        same = xsim(ax, xs)
        world = 1 - (fit(av, key, kind) or 0.6)  # far by fingerprint = a different world
        s, _ = score_key(m, key, kind=kind)
        pct = match_pct(m, s) or 50
        if same < 0.35:
            continue
        shared = [x for x in top if xs.get(x, 0) >= 2][:1]
        note = (f"{EXPERIENCES[shared[0]][0][:1].upper() + EXPERIENCES[shared[0]][0][1:]} like {a['title']}" if shared
                else f"Feels like {a['title']}")
        scored.append((same * (0.4 + world) + 0.004 * pct, {**meta, "description": " · ".join(
            x for x in (note + who_label(a.get("by") or ()), meta.get("description")) if x)}))
    reserve = [x for _, x in sorted(scored, key=lambda t: -t[0])]
    metas = polish_row(balance_row(reserve))[:30]
    print(f"leap: {who} -> {a['title']} {top}, pool {len(pool)}, kept {len(metas)}", flush=True)
    if len(metas) < 8:
        return None
    return {rid('leap'): {"tv": metas, "pool": reserve}, "_names": {rid('leap'): {"title": f"Same Feeling as {a['title']}", "seed": a["key"]}}}


def member_feed(lang, person):
    p, m = _profiles.get("household"), get_model()
    if not p or not m:
        return None
    mine = sorted((e for e in person_entries(p, person) if e["weight"] > 0),
                  key=lambda e: (-(person in e.get("by", ())), -e["weight"]))[:8]
    kinds = {e["key"]: e["kind"] for e in mine}
    jobs = [(kinds[k], _outer.submit(with_art, f"{kinds[k]}/{k[5:]}/recommendations", lang)) for k in kinds]
    exclude, items, seen = p["seen"] | p["negative"] | set(tired_keys({rid(person)})), [], set()
    for kind, job in jobs:
        try:
            data, art = job.result()
        except Exception:
            continue
        names = genres(kind, lang)
        for it in data:
            key = f"tmdb:{it['id']}"
            if key in seen or key in exclude or NO_TALK & set(it.get("genre_ids") or []) \
                    or (it.get("vote_count") or 0) < (200 if kind == "movie" else 50):
                continue
            seen.add(key)
            items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}))))
    rank_by_taste(m, items)  # facets, fingerprints and tags for the pool
    animation = not any("Animation" in frec(k, profile_kind(k)).get("g", ()) for k in explicit_seeds(person))
    candidates = [x for k, kind, x in items if frec(k, kind).get("x") and quality_ok(x) and tone_ok(x, animation=animation)]
    scores = member_scores(candidates, person)
    candidates.sort(key=lambda x: scores[x["id"]], reverse=True)
    metas = member_balance(polish_row(balance_row([with_match(x, floor=0, person=person) for x in candidates])), person)[:40]
    return {rid(person): {"tv": metas}} if len(metas) >= 8 else None


def mood_feed(lang):
    p, m = _profiles.get("household"), get_model()
    if not p or not m:
        return None
    wd = str((local_now() - datetime.timedelta(hours=5)).weekday())
    fitm = {k: sum(max(0.0, m["aff"].get(f"x:{x}", 0)) for x in xs) + (0.15 if wd in days else 0)
            for k, (_, xs, days) in MOODS.items()}
    ranked = sorted(fitm, key=lambda k: -fitm[k])[:7]
    i = day_index()
    rule = list(dict.fromkeys(ranked[(i + 2 * k) % len(ranked)] for k in range(len(ranked))))[:len(MOODS_ROWS)]
    picks = pick_moods(m, fitm, rule)
    out, names, taken = {cid: {"tv": []} for cid in MOODS_ROWS}, {}, set()
    free = list(MOODS_ROWS)
    # a shelf that comes out thin (< 10) gives its slot to the next mood in line
    for mood in list(dict.fromkeys(picks + rule + sorted(fitm, key=lambda k: -fitm[k]))):
        if not free:
            break
        title, xs, _ = MOODS[mood]
        pool = [x for x in feeling_pool(m, p, xs, lang, seeds=6) if x[0] not in taken]
        pool = [x for x in pool if x[0] not in tired_keys({f"mood:{mood}"})]
        ranked_pool = rank_by_taste(m, pool)
        scored = []
        for sc, why, key, kind, meta in ranked_pool:
            tags = frec(key, kind).get("x") or {}
            hit = max(xs, key=lambda x: tags.get(x, 0))
            strength = sum(tags.get(x, 0) for x in xs)
            label = EXPERIENCES[hit][0]
            scored.append((strength * 0.15 + (match_pct(m, sc) or 50) / 100, key,
                           {**meta, "description": " · ".join(y for y in (label[:1].upper() + label[1:], meta.get("description")) if y)}))
        reserve = [x for _, _, x in sorted(scored, key=lambda t: -t[0])]
        metas = reserve[:25]
        if len(metas) < 10:
            continue
        cid = free.pop(0)
        taken |= {x["id"] for x in metas}
        out[cid] = {"tv": metas, "pool": reserve}
        names[cid] = {"title": title, "mood": mood}
    shown = [n["mood"] for n in names.values()]
    lday = (local_now() - datetime.timedelta(hours=5)).date().isoformat()
    with _learn_lock:
        if lday in _learn["moodpicks"] and _learn["moodpicks"][lday].get("shown") != shown:
            _learn["moodpicks"][lday]["shown"] = shown
            _learn_dirty.set()
    print("moods: " + ", ".join(f"{n['title']} ({len(out[c]['tv'])})" for c, n in names.items()), flush=True)
    return {**out, "_names": names}


def mood_state(m):
    """What Luna sees when choosing tonight's shelves (no titles beyond what they played)."""
    d = local_now()
    lday = (d - datetime.timedelta(hours=5)).date()
    with _learn_lock:
        ctx = _learn["ctx"][-10:]
        rows = {k: _learn["rows"].get(f"mood:{k}") or {} for k in MOODS}
        past = [(day, v["picks"]) for day, v in sorted(_learn["moodpicks"].items()) if day < lday.isoformat()][-3:]
    likes = ", ".join(m["names"][f] for _, f in sorted(((a, f) for f, a in m["aff"].items() if f[0] == "x" and a > 0),
                                                       reverse=True)[:8])
    lines = [f"{when_text()} Choosing the mood shelves for {'tonight' if d.hour >= 5 else 'this late night'} "
             f"({lday:%A} evening, {year_season(lday)}) for {MEMBER_NAMES}, {HOUSEHOLD_KIND} sharing one TV.",
             f"Experiences they love most: {likes}." if likes else ""]
    if ctx:
        lines.append("Recently watched on their TV (when): " + "; ".join(
            f"{c['title']} ({c['kind']}, {c['weekday']} {c['part']})" for c in reversed(ctx)) + ".")
    resp = [f"{MOODS[k][0]} shown {r['shown']}x, played {r['hits']:g}" for k, r in rows.items() if r.get("shown")]
    if resp:
        lines.append("How past mood shelves did (days shown, plays from them): " + "; ".join(resp) + ".")
    if past:
        lines.append("Shelves of the last evenings (vary a little): " + "; ".join(
            f"{day}: {', '.join(MOODS[k][0] for k in ks if k in MOODS)}" for day, ks in past) + ".")
    return "\n".join(x for x in lines if x)


MOOD_Q = {k: {"type": "score", "criteria": MOOD_LEVELS,
              "instructions": f"How well does the shelf '{t}' ({', '.join(EXPERIENCES[x][0] for x in xs)}) "
                              f"fit this {'couple' if len(PEOPLE) == 2 else 'household'} tonight?"} for k, (t, xs, _) in MOODS.items()}


def mood_parse(resp, batch):
    answers = resp["answers"]
    scores = {k: float(answers[k]["score"]) if isinstance((answers.get(k) or {}).get("score"), (int, float))
              else None for k in MOODS}
    return [({"scores": scores}, 1.0 if all(v is not None for v in scores.values()) else 0.0)]


decision("mood_pick", ver=1, lane="feed", cap=3, ttl=DAY, serving=False, requires=("OR_KEY",), api=False,
    inp={"state": [str, 16]}, out={"scores": {k: (float, None) for k in MOODS}},
    norm=lambda x: x, rule=None, params={}, min_acc=1,
    llm={"path": "/api/alpha/decisions", "model": ROUTE_MODEL, "batch": 1, "timeout": 20,
         "prompt": lambda b: {"model": ROUTE_MODEL, "state": "".join(b[0]["state"]), "questions": MOOD_Q,
                              "usage": {"include": True}}, "parse": mood_parse},
    fallback=lambda inp, err: ({"scores": {k: None for k in MOODS}}, 0))


def pick_moods(m, fitm, rule):
    """Tonight's mood shelves, decided once per evening: Luna scores every shelf for tonight (0-3), blended with the
    household's taste fit and a small penalty for repeating yesterday. Falls back to the rule rotation."""
    lday = (local_now() - datetime.timedelta(hours=5)).date().isoformat()
    with _learn_lock:
        got = _learn["moodpicks"].get(lday)
        yday = (_learn["moodpicks"].get((datetime.date.fromisoformat(lday) - datetime.timedelta(days=1)).isoformat())
                or {}).get("picks", [])
    n = len(MOODS_ROWS)
    if got and len(got.get("picks", [])) == n and all(k in MOODS for k in got["picks"]):
        return got["picks"]
    rec = {"picks": rule, "source": "rule", "at": int(time.time())}
    if OR_KEY:
        try:
            state = mood_state(m)
            result = decide("mood_pick", {"state": [state[i:i+500] for i in range(0, len(state), 500)]})
            if result["source"] == "fallback":
                print(f"moodpick: {result['why']}", flush=True)
            top = max(fitm.values()) or 1.0
            luna = {k: v for k, v in result["answer"]["scores"].items() if v is not None}
            if len(luna) == len(MOODS) and max(luna.values()) - min(luna.values()) >= 0.3:
                blend = {k: 0.65 * luna[k] / 3 + 0.35 * max(0.0, fitm[k]) / top - (0.1 if k in yday else 0) for k in MOODS}
                rec = {"picks": sorted(blend, key=lambda k: -blend[k])[:n], "source": "luna", "at": int(time.time()),
                       "luna": {k: round(v, 2) for k, v in luna.items()}}
        except Exception as e:  # BudgetExceeded, network, unexpected answer
            print(f"moodpick: {type(e).__name__}: {e}", flush=True)
    with _learn_lock:
        _learn["moodpicks"][lday] = rec
        for day in sorted(_learn["moodpicks"])[:-60]:
            _learn["moodpicks"].pop(day)
        _learn_dirty.set()
    print(f"moodpick {lday} ({rec['source']}): {', '.join(rec['picks'])}", flush=True)
    return rec["picks"]


WEEK_SYS = (f"You are the personal film friend of {HOUSEHOLD_KIND} sharing one TV. Once a week you pick a few titles for them. "
            "Reply with exactly these sections and nothing else:\n" +
            "".join(f"## FOR {LABELS[p].upper()}\n(3 lines)\n" for p in PEOPLE) +
            f"## TOGETHER\n(3 lines: what {'both' if len(PEOPLE) == 2 else 'all'} will enjoy on the sofa)\n"
            "Line format: Title|Year|m or s|why   (m=movie, s=series). why = max 10 English words, warm and specific, "
            "naming one title of theirs it connects to. Real titles only, nothing they already watched or gave up on. "
            "Prefer one great surprise over the obvious sequel.")


def week_feed(lang):
    """One call a week: three picks per member and together, with a personal why."""
    p, m = _profiles.get("household"), get_model()
    if not p:
        return None
    people = read_taste().get("people") or {}

    def person_line(name):
        cfg = people.get(name) or {}
        mine = [e["title"] for e in sorted(person_entries(p, name), key=lambda e: -e["weight"])
                if e["weight"] > 0 and name in e.get("by", ())][:12]
        x = ", ".join(m["names"][f] for _, f in sorted(((a, f) for f, a in (m["paff"].get(name) or {}).items()
                                                         if f[0] == "x" and a > 0), reverse=True)[:5]) if m else ""
        return (f"{LABELS[name]}{' (' + cfg['type'] + ', a light hint only)' if cfg.get('type') else ''}: loves "
                f"{'; '.join(mine) or 'see together'}{'. Enjoys: ' + x if x else ''}.")

    together = [e["title"] for e in p["entries"] if "together" in e.get("by", ()) and e["weight"] > 0][:10]
    prompt = (when_text() + "\n" + "\n".join(person_line(p) for p in PEOPLE) + "\n"
              f"Watching together on the TV lately: {'; '.join(together)}.\n"
              f"Already seen, do not pick: {'; '.join(e['title'] for e in p['entries'][:60])}.")
    sec = llm_sections(WEEK_SYS, prompt, 4000, 150, "week", [*("FOR " + LABELS[p].upper() for p in PEOPLE), "TOGETHER"], effort="medium")
    metas = []
    for head, who in [("TOGETHER", "For " + LABELS["pair"]), *(("FOR " + LABELS[p].upper(), "For " + LABELS[p]) for p in PEOPLE)]:
        for t, y, st, why in sec.get(head, [])[:3]:
            kind = "movie" if st == "movie" else "tv"
            got = row_search(st, kind, [(t, y, st, f"{who}: {why}" if why else who)], lang)
            metas += [x for x in got if x["id"] not in p["seen"] and x["id"] not in {y_["id"] for y_ in metas}]
    return {rid('week'): {"tv": metas}} if metas else None


# --- LLM rows ------------------------------------------------------------------

def candidate_pool(p, n=40, metas=False):
    """TMDB recommendations of their strongest titles plus this week's trending: real candidates the LLM can rerank.
    With the taste model, a wider pool is ranked by match (fingerprint + facets); without, the first n in TMDB order."""
    try:
        m = get_model()
    except Exception as e:
        print(f"taste model: {type(e).__name__}: {e}", flush=True)
        m = None
    vec = m and m["vec"]
    seeds, per = (top_seeds(p, 10), 20) if m else (top_seeds(p, 4), 15)
    tv = top_seeds({"entries": [e for e in p["entries"] if e.get("who") in ("tv", "both")]}, 3)
    seeds += [e for e in tv if e not in seeds][:2]  # the shared TV (incl. the partner's shows) always seeds the pool
    jobs = [(e, _pool.submit(tmdb, f"{e['kind']}/{e['id']}/recommendations", language="en-US")) for e in seeds]
    jobs += [({"kind": k, "title": None}, _pool.submit(tmdb, f"trending/{k}/week", language="en-US")) for k in ("movie", "tv")]
    cands, seen = [], set(p["seen"]) | set(p["negative"]) | set(tired_keys({rid('foryou')}))
    for e, job in jobs:
        try:
            data = job.result().get("results", [])
        except Exception:
            continue
        for it in data[:per]:
            key = f"tmdb:{it['id']}"
            if key in seen or (it.get("vote_count") or 0) < 100:
                continue
            seen.add(key)
            cands.append((key, e["kind"], it, e))
    why = {}
    if m:
        ranked = rank_by_taste(m, [(key, kind, (it, e)) for key, kind, it, e in cands])
        cands = [(key, kind, it, e) for _, _, key, kind, (it, e) in ranked]
        why = {key: (match_pct(m, sc), reasons(m, key, w, kind=kind)) for sc, w, key, kind, _ in ranked}
        n = 50
    if metas:
        out = [row_meta(it, "movie" if kind == "movie" else "series", kind, genres(kind, "en-US"), it)
               for _, kind, it, _ in cands]
        return [with_match(x) for x in out if quality_ok(x) and tone_ok(x)]
    out = []
    for key, kind, it, e in cands[:n]:
        year = (it.get("release_date") or it.get("first_air_date") or "")[:4]
        pct, w = why.get(key, (None, []))
        out.append(f"{it.get('title') or it.get('name')} ({year}, {'movie' if kind == 'movie' else 'series'}, "
                   f"{'like ' + e['title'] if e.get('title') else 'trending this week'}"
                   f"{f', match {pct}%' if pct else ''}{', ' + ' / '.join(w) if w else ''})")
    return out


def row_search(stype, kind, picks, lang):
    jobs = [(p[3], _outer.submit(resolve_pick, kind, p[0], p[1], lang)) for p in picks if p[2] == stype]
    names, metas, seen = genres(kind, lang), [], set()
    for why, job in jobs:
        try:
            hit = job.result()
        except Exception:
            continue
        if hit and hit[0]["id"] not in seen:
            seen.add(hit[0]["id"])
            metas.append(row_meta(hit[0], stype, kind, names, hit[1], note=clean_note(why)))
    return metas


def picks_to_rows(picks, lang, exclude):
    out = {}
    for stype, kind in (("movie", "movie"), ("series", "tv")):
        metas = row_search(stype, kind, picks, lang)
        out[stype] = [m for m in metas if m["id"] not in exclude]
    return out


def when_text():
    d = local_now()
    return f"It is {d:%A}, {d:%d %B %Y}, {d:%H:%M} in {COUNTRY}."


def personal_feed(lang):
    """One reasoning call builds two rows: For You and the AI half of Watch Again."""
    p = _profiles.get("household")
    if not p or not (p["entries"] or p["searches"] or p.get("likes")):
        return None
    system = PERSONAL_SYS.format(
        sections="## FOR YOU\n(24 lines, 12 movies and 12 series incl. a few anime: what they will most likely love next; "
                 "include a few strong new releases and current hits when they fit)\n"
                 "## REWATCH\n(14 lines, mostly movies plus a few series: big, rewatchable favourites and comfort "
                 "classics this household would happily watch again, matching what they love; already-watched titles "
                 "are welcome here; skip the first films of sagas they named, those are covered)",
        example="Because you loved Paddington 2")
    pool = candidate_pool(p)
    hits, tired = hit_titles(), tired_keys({rid('foryou')})
    m = get_model()
    types = {n: (c or {}).get("type") for n, c in (read_taste().get("people") or {}).items()}
    prompt = (f"{when_text()} Household of {MEMBER_COUNT} sharing one TV, {MEMBER_NAMES}"
              + (f" (personality types {', '.join(f'{LABELS.get(n, n.title())} {t}' for n, t in types.items() if t)}: a light hint only, "
                 "taste comes from what they watch)" if any(types.values()) else "")
              + ("; " + CONFIG["household"]["note"] if CONFIG["household"]["note"] else "")
              + f". Pick for {'both' if len(PEOPLE) == 2 else 'all'} of them, not one blended person.\n"
              + profile_text(p) +
              (f"\nWhat their favourites have in common (from title metadata): {m['taste_line']}" if m and m.get("taste_line") else "") +
              ("\nEarlier picks from these rows they actually played (find more like these): " + "; ".join(hits) if hits else "") +
              ("\nShown to them for a week, never played (do not suggest again, lean away from them): "
               + "; ".join(list(tired.values())[:20]) if tired else "") +
              ("\nCandidates (TMDB recommendations of their titles and this week's trending, ranked by our taste match; "
               "rerank, use the strongest, add your own ideas): " + "; ".join(pool) if pool else ""))
    sec = llm_sections(system, prompt, 5000, 150, "personal", ["FOR YOU", "REWATCH"], effort="medium")
    exclude = p["seen"] | p["negative"] | set(tired)
    recent = {e["key"] for e in p["entries"] if e["age"] < 60}
    foryou = picks_to_rows(sec.get("FOR YOU", []), lang, exclude)
    mixed = interleave(foryou.get("movie", []), foryou.get("series", []))
    mixed = interleave(mixed, candidate_pool(p, n=100, metas=True))
    mixed = polish_row(balance_row([x for x in mixed if quality_ok(x) and tone_ok(x)], foryou=True))[:40]
    return {rid('foryou'): {"tv": mixed},
            rid('rewatchai'): picks_to_rows(sec.get("REWATCH", []), lang, recent)}


def archived_feed(lang, person):
    p = _profiles.get(person)
    label = ARCHIVED[person]
    head = "FOR " + label.upper()
    if not p or not (p["entries"] or p.get("likes")):
        return None
    h = _profiles.get("household")
    system = PERSONAL_SYS.format(sections=f"## {head}\n(24 lines, 12 movies and 12 series: what {label} will most likely love next)",
                                 example="Because you loved Dune")
    prompt = (f"{when_text()} Personal row for {label} (one of the {MEMBER_COUNT} people in the household). Address them as 'you' in why, "
              "and base why on their own titles or genres, not the household's.\n"
              f"{label}'s own activity:\n{profile_text(p, 'they')}" +
              ("\nWhat the household watches together (light hint only): " +
               "; ".join([e["title"] for e in h["entries"] if e["weight"] > 0][:8]) if h and h["entries"] else ""))
    sec = llm_sections(system, prompt, 4000, 120, person, [head], effort="low")
    return {rid(person): picks_to_rows(sec.get(head, []), lang, p["seen"])}


# --- wonder rows: documentaries that leave you in awe or thinking ----------------------------------------------
# Three shelves: Earth, life & cosmos; great lives & empires; big questions. Each mixes a weekly AI shortlist (it
# knows the classics and the gems: Attenborough, Cosmos, Terra X, arte) with TMDB documentaries on the shelf's
# keywords, ranked by rating and taste. Documentaries stay out of every other row (NO_TALK).

DOC_ROWS = {  # cid: (row title, AI section, what the shelf is about, TMDB keyword ids)
    rid('wonder'): ("Wonder of Our World", "WONDER",
                    "nature, animals, oceans, our living planet and how life evolved: pure awe",
                    [18330, 9902, 361118, 270, 239155, 5647, 4627, 34152, 311]),
    rid('cosmos'): ("Mind & Cosmos", "MIND & COSMOS",
                    "space and the universe, physics, time, black holes, the brain and consciousness: how reality works",
                    [9882, 15325, 160330, 41608, 6012, 217034, 8056, 4380, 360221, 234133, 161219]),
    rid('history'): ("Great Lives & Empires", "GREAT LIVES",
                     "larger-than-life figures and empires of history (Constantine the Great, Alexander, Caesar, "
                     "Cleopatra, Genghis Khan, Napoleon...), ancient worlds and turning points",
                     [5049, 1405, 15012, 355987, 157894, 208596, 2280, 6165, 2163, 383073]),
    rid('meaning'): ("Big Questions", "BIG QUESTIONS",
                     "spirituality, religions and myths, philosophy, the meaning of life and our place in the world",
                     [10706, 188, 1532, 177146, 2035]),
}
DOC_TREND = ("Trending in Documentaries",)
# Their favourite makers: BBC, arte, ZDF/Terra X (networks for series, companies for both). Titles from them rank higher.
DOC_FAV = {"with_networks": "4|332|100|1155|1628|662|31|308",
           "with_companies": "34333|8090|9050|80893|201|587|4606|11237"}
DOC_FAV_BONUS = 0.15
DOC_BLOCK = {"without_genres": "80,10402,10763,10764,10767", "without_keywords": "33722"}  # no crime, news, reality, talk
DOCS_SYS = ("You pick documentaries and documentary series for {household} in {country} who love awe, history and big "
            "thinking. Reply with exactly these sections and nothing else:\n{sections}\n"
            "Line format: Title|Year|m or s|why   (m=movie, s=series). why = max 10 English words on what will amaze "
            "or move them. Real, well-made titles only. Their favourite makers are arte, Terra X (ZDF) and the BBC: "
            "lean on them, {language} productions welcome; also National Geographic, PBS, Netflix and other English or "
            "{language} productions; docudrama series count. Classics and hidden gems mixed, best first. "
            "Wonder and uplift over outrage: no true crime, no exposés, no celebrity or music biographies.")


def docs_ai_feed(lang):
    """Once a week: an AI shortlist per documentary shelf (stored as the hidden row <row-prefix>.docsai)."""
    p, m = _profiles.get("household"), get_model()
    if not p:
        return None
    sections = "\n".join(f"## {sec}\n(16 lines: {about})" for _, sec, about, _ in DOC_ROWS.values())
    loved = [e["title"] for e in p["entries"] if e["weight"] > 0][:25]
    prompt = (f"{when_text()} {MEMBER_NAMES} share one TV. Titles they love: {'; '.join(loved)}."
              + (f"\nWhat their favourites have in common: {m['taste_line']}" if m and m.get("taste_line") else "")
              + "\nPick documentaries that give the same pull: epic scale, great characters, a sense of wonder.")
    sec = llm_sections(DOCS_SYS.format(sections=sections, household=HOUSEHOLD_KIND, country=COUNTRY, language=REGION_LANGUAGE), prompt, 6500, 180, "docs", [v[1] for v in DOC_ROWS.values()],
                       effort="low")
    out = {}
    for cid, (_, name, _, _) in DOC_ROWS.items():
        rows = picks_to_rows(sec.get(name, []), lang, p["seen"])
        out[cid] = interleave(rows["movie"], rows["series"])
    return {rid('docsai'): out}


def _doc_pool(lang, exclude, taken, queries):
    """Run discover queries; returns {key: (kind, meta, quality, popularity, favourite maker)}."""
    jobs = [(kind, fav, _outer.submit(with_art, f"discover/{kind}", lang, with_genres=99, include_adult="false",
                                      **DOC_BLOCK, **q)) for kind, fav, q in queries]
    out = {}
    for kind, fav, job in jobs:
        try:
            data, art = job.result()
        except Exception:
            continue
        names = genres(kind, lang)
        for it in data:
            key = f"tmdb:{it['id']}"
            if key in taken or key in exclude:
                continue
            if key in out:
                if fav:
                    out[key] = out[key][:4] + (True,)
                continue
            q = (it.get("vote_average") or 0) * math.log10(max(10, it.get("vote_count") or 0))
            out[key] = (kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {})),
                        q, it.get("popularity") or 0, fav)
    return {k: v for k, v in out.items() if doc_ok(v[1], rid('doctrend'))}


def _doc_rank(m, pool, w_quality, w_pop, w_taste):
    """Blend quality, popularity and taste (each 0..1), plus a bonus for their favourite makers."""
    if not pool:
        return []
    qmax = max(v[2] for v in pool.values()) or 1
    pmax = max(v[3] for v in pool.values()) or 1
    top = sorted(pool, key=lambda k: -(w_quality * pool[k][2] / qmax + w_pop * pool[k][3] / pmax))[:70]
    ranked = rank_by_taste(m, [(k, pool[k][0], pool[k][1]) for k in top])
    scored = []
    for sc, _, k, _, x in ranked:
        kind, _, q, pop, fav = pool[k]
        scored.append((w_quality * q / qmax + w_pop * pop / pmax + w_taste * ((match_pct(m, sc) or 50) / 100)
                       + (DOC_FAV_BONUS if fav else 0), k, x))
    return [(k, x) for _, k, x in sorted(scored, key=lambda t: -t[0])]


# --- NZB availability: documentary shelves only offer what can be played -------------------------------------------
# The only stream source is Usenet via NZBHydra (/data/hydra.json: {"url": ".../api", "key": ...}, readable by the
# addon only). Each title is searched once by id (IMDb / TVDB), with a strict name match as fallback; "found" is kept
# NZB_OK_DAYS, "nothing" NZB_MISS_DAYS (new releases appear). Unchecked titles (budget, Hydra down) stay in.
NZB_OK_DAYS, NZB_MISS_DAYS, NZB_DAILY = 14, 3, 1500
_nzb = {"titles": {}, "day": "", "used": 0}
_nzb_lock = threading.Lock()
_nzb_dirty = threading.Event()
_nzb_pool = BuildPool(4)
_hydra = {}


def hydra_cfg():
    path = os.path.join(DATA, "hydra.json")
    try:
        mtime = os.path.getmtime(path)
        if _hydra.get("mtime") != mtime:
            with open(path) as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), str) and cfg[k].strip() for k in ("url", "key")):
                raise ValueError()
            _connector_url(cfg["url"])
            _hydra.update(mtime=mtime, url=cfg["url"], key=cfg["key"])
        return _hydra["url"], _hydra["key"]
    except (OSError, ValueError, KeyError):
        return None


def nzb_words(s):
    s = (s or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", s)


def nzb_search(cfg, **q):
    with _nzb_lock:
        day = local_now().strftime("%Y-%m-%d")
        if _nzb["day"] != day:
            _nzb.update(day=day, used=0)
        if _nzb["used"] >= NZB_DAILY:
            raise BudgetExceeded("nzb budget")
        _nzb["used"] += 1
    url = cfg[0] + "?" + urllib.parse.urlencode({**q, "apikey": cfg[1]})
    with _connector_open(url, timeout=20) as r:
        raw = r.read(CONNECTOR_HTTP_BYTES + 1)
        if len(raw) > CONNECTOR_HTTP_BYTES:
            raise ByteCapExceeded()
        x = raw.decode("utf-8", "replace")
    if "<error" in x[:400]:
        raise RuntimeError(re.search(r'description="([^"]*)"', x).group(1) if 'description="' in x else "hydra error")
    return re.findall(r"<title>([^<]*)</title>", x)[1:]  # [0] is the channel title


NZB_TAGS = {"german", "deutsch", "dl", "doku", "dokumentation", "complete", "web", "webrip", "webdl", "hdtv",
            "bluray", "uhd", "x264", "h264", "x265", "h265", "hevc", "multi", "dubbed", "internal", "proper", "repack",
            "staffel", "teil", "part", "mini", "miniseries", "dokuserie", "english", "eng", "ac3", "dd51", "remastered"}


def nzb_name_hit(name, year, releases):
    """A release named exactly like the title: its words in a row, first or after one tag ('BBC.'), then the end, a
    year, an episode marker or a release tag (so 'Erde' does not match 'Erde.aus.dem.All'), and the year when a
    film's release names one. Titles under three words ('Kingdom', 'Evolution') need the year in the release."""
    w = nzb_words(name)
    if not w or (len(w) == 1 and len(w[0]) < 4):
        return False
    for rel in releases:
        r = nzb_words(rel)
        for i in (0, 1):
            if r[i:i + len(w)] != w:
                continue
            nxt = r[i + len(w)] if len(r) > i + len(w) else ""
            if nxt and not (re.fullmatch(r"(19|20)\d\d|s\d+(e\d+)?|e\d+|\d{3,4}p", nxt) or nxt in NZB_TAGS):
                continue
            yrs = [int(t) for t in r[i + len(w):] if len(t) == 4 and t[:2] in ("19", "20")]
            if len(w) < 3 and not (year and yrs):
                continue
            if not year or not yrs or abs(yrs[0] - year) <= 1:
                return True
    return False


def nzb_check(key, kind, local_name):
    cfg = hydra_cfg()
    if not cfg:
        return None
    tid = key[5:]
    ext = tmdb(f"{kind}/{tid}/external_ids")
    imdb, tvdb = ext.get("imdb_id"), ext.get("tvdb_id")
    if kind == "movie" and imdb:
        found = nzb_search(cfg, t="movie", imdbid=imdb)
    elif kind == "tv" and (tvdb or imdb):
        found = nzb_search(cfg, t="tvsearch", **({"tvdbid": tvdb} if tvdb else {"imdbid": imdb}))
    else:
        found = []
    if found:
        return True, "id"
    det = tmdb(f"{kind}/{tid}", language="en-US")
    year = int((det.get("release_date") or det.get("first_air_date") or "0")[:4] or 0)
    names = list(dict.fromkeys(n for n in (det.get("original_title") or det.get("original_name"),
                                           det.get("title") or det.get("name"), local_name) if n))[:2]
    for n in names:
        if nzb_name_hit(n, year, nzb_search(cfg, t="search", q=n)):
            return True, "name"
    return False, "none"


def nzb_state(key):
    rec = _nzb["titles"].get(key)
    if not rec:
        return None
    fresh = time.time() - rec["at"] < (NZB_OK_DAYS if rec["ok"] else NZB_MISS_DAYS) * 86400
    return rec["ok"] if fresh else None


def nzb_keep(metas, need):
    """metas in shelf order -> the first `need` that are (or may be) playable, checking only what is unknown."""
    if not hydra_cfg():
        return metas[:need]
    out, i, dropped = [], 0, []
    while len(out) < need and i < len(metas):
        batch = metas[i:i + need - len(out) + 4]
        i += len(batch)
        todo = [x for x in batch if nzb_state(x["id"]) is None]
        jobs = {x["id"]: _nzb_pool.submit(nzb_check, x["id"], "movie" if x["type"] == "movie" else "tv", x.get("name"))
                for x in todo}
        for k, job in jobs.items():
            try:
                res = job.result()
            except Exception as e:  # budget, Hydra or TMDB down: leave unknown (kept)
                print(f"nzb {k}: {type(e).__name__}", flush=True)
                continue
            if res:
                with _nzb_lock:
                    _nzb["titles"][k] = {"ok": res[0], "via": res[1], "at": int(time.time())}
                _nzb_dirty.set()
        for x in batch:
            if nzb_state(x["id"]) is False:
                dropped.append(x["name"])
            else:
                out.append(x)
    if dropped:
        print(f"nzb: not on usenet, skipped {', '.join(dropped)}", flush=True)
    return out[:need]


def save_nzb():
    if not _nzb_dirty.is_set():
        return
    _nzb_dirty.clear()
    with _nzb_lock:
        cut = time.time() - 60 * 86400
        _nzb["titles"] = {k: v for k, v in _nzb["titles"].items() if v["at"] > cut}
        snap = json.dumps(_nzb)
    try:
        with open(os.path.join(DATA, "nzb.json.tmp"), "w") as fh:
            fh.write(snap)
        os.replace(os.path.join(DATA, "nzb.json.tmp"), os.path.join(DATA, "nzb.json"))
    except OSError:
        pass


def nzb_stats():
    with _nzb_lock:
        t = list(_nzb["titles"].values())
        return {"known": len(t), "found": sum(v["ok"] for v in t), "by_name": sum(v.get("via") == "name" for v in t),
                "missing": sum(not v["ok"] for v in t), "searches_today": _nzb["used"], "cap": NZB_DAILY,
                "hydra": bool(hydra_cfg())}


def docs_feed(lang):
    p, m = _profiles.get("household"), get_model()
    if not p or not m:
        return None
    ai = (_feeds.get((rid('docsai'), lang)) or {}).get("metas") or {}
    exclude = p["seen"] | p["negative"]
    out, taken = {}, set()
    # Trending: popular documentaries of the last two years on the shelves' themes (plain trending is celebrity,
    # sport and music), lightly ranked by taste.
    since = (local_now() - datetime.timedelta(days=730)).strftime("%Y-%m-%d")
    themes = "|".join(str(k) for v in DOC_ROWS.values() for k in v[3]) + "|1956"  # + world war ii
    def trend_queries(div):
        queries = []
        for kind, votes, date in (("movie", 15, "primary_release_date.gte"), ("tv", 5, "first_air_date.gte")):
            for page in (1, 2, 3):
                queries.append((kind, False, {"sort_by": "popularity.desc", "page": page,
                                              "vote_count.gte": max(2, votes // div), date: since,
                                              "with_keywords": themes}))
            queries.append((kind, True, {"sort_by": "popularity.desc", "vote_count.gte": max(2, votes // 3 // div),
                                         date: since, "with_keywords": themes,
                                         **({"with_networks": DOC_FAV["with_networks"]} if kind == "tv"
                                            else {"with_companies": DOC_FAV["with_companies"]})}))
        return queries

    def split(ranked):
        return interleave([x for _, x in ranked if x["type"] == "movie"], [x for _, x in ranked if x["type"] != "movie"])

    pool = _doc_pool(lang, exclude, taken, trend_queries(1))
    metas = nzb_keep(split(_doc_rank(m, pool, 0.15, 0.55, 0.3)), 30)
    if len(metas) < 30:  # titles not on usenet: top up from less-voted documentaries on the same themes
        more = _doc_pool(lang, exclude, taken | set(pool), trend_queries(3))
        metas += nzb_keep(split(_doc_rank(m, more, 0.15, 0.55, 0.3)), 30 - len(metas))
    taken |= {x["id"] for x in metas}
    out[rid('doctrend')] = {"tv": metas}
    for cid, (_, _, _, kws) in DOC_ROWS.items():
        def shelf_queries(div):
            queries = []
            for kind, votes in (("movie", 50 // div), ("tv", 15 // div)):
                base = {"with_keywords": "|".join(map(str, kws)), "vote_count.gte": votes}
                for sort, page in (("vote_count.desc", 1), ("vote_count.desc", 2), ("popularity.desc", 1)):
                    queries.append((kind, False, {**base, "sort_by": sort, "page": page}))
                favs = [{"with_companies": DOC_FAV["with_companies"]}]
                if kind == "tv":
                    favs.append({"with_networks": DOC_FAV["with_networks"]})
                for f in favs:
                    queries.append((kind, True, {**base, "vote_count.gte": max(5, votes // 5),
                                                 "sort_by": "vote_count.desc", **f}))
            return queries

        pool = _doc_pool(lang, exclude, taken, shelf_queries(1))
        ranked = _doc_rank(m, pool, 0.6, 0, 0.4)
        picks = [x for x in ai.get(cid, []) if x["id"] not in taken and x["id"] not in exclude and doc_ok(x, cid)][:16]
        ids = {x["id"] for x in picks}
        metas = nzb_keep(picks + [x for k, x in ranked if k not in ids and doc_ok(x, cid)], 40)  # the curated picks first, then the shelf
        if len(metas) < 30:  # titles not on usenet: top up from less-voted documentaries on the same themes
            more = _doc_pool(lang, exclude, taken | set(pool) | ids, shelf_queries(3))
            metas += nzb_keep([x for _, x in _doc_rank(m, more, 0.6, 0, 0.4)], 30 - len(metas))
        taken |= {x["id"] for x in metas}
        out[cid] = {"tv": metas}
    print("docs: " + ", ".join(f"{c.split('.')[1]}={len(v['tv'])}" for c, v in out.items()), flush=True)
    return out


# --- studio rows: the brands they choose by, ranked by their taste ----------------------------------------------
STUDIOS = {  # cid: (row title, TMDB discover filter per kind)
    rid('hbo'): ("HBO for You", {"tv": {"with_networks": "49|3186"}}),
    rid('paramount'): ("Paramount+ for You", {"tv": {"with_networks": "4330|2076|67"},
                                              "movie": {"with_companies": "4", "vote_average.gte": 7}}),
}


def studios_feed(lang):
    p, m = _profiles.get("household"), get_model()
    if not p or not m:
        return None
    exclude = p["seen"] | p["negative"]
    out = {}
    for cid, (title, filters) in STUDIOS.items():
        jobs = []
        for kind, f in filters.items():
            for sort, page in (("popularity.desc", 1), ("vote_count.desc", 1), ("vote_count.desc", 2)):
                jobs.append((kind, _outer.submit(with_art, f"discover/{kind}", lang, sort_by=sort, page=page, **f,
                                                 **{"vote_count.gte": 1000 if kind == "movie" else 40})))
        items, seen = [], set()
        for kind, job in jobs:
            try:
                data, art = job.result()
            except Exception:
                continue
            names = genres(kind, lang)
            for it in data:
                key = f"tmdb:{it['id']}"
                if key in seen or key in exclude or NO_TALK & set(it.get("genre_ids") or []):
                    continue
                seen.add(key)
                items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}))))
        ranked = [x for *_, x in rank_by_taste(m, items)]
        shows, films = [x for x in ranked if x["type"] != "movie"], [x for x in ranked if x["type"] == "movie"]
        mixed = [x for i in range(30) for x in (shows[2 * i:2 * i + 2] + films[i:i + 1])]  # the series lead: two to one
        out[cid] = {"tv": polish_row(balance_row([with_match(x) for x in mixed if quality_ok(x) and tone_ok(x)]))[:40]}
    print("studios: " + ", ".join(f"{c.split('.')[1]}={len(v['tv'])}" for c, v in out.items()), flush=True)
    return out


# --- backups ---
BACKUP_FILES = ("skald.json", "legacy.json", ".skald-household", ".skald-format", "feedback.jsonl", "imports.jsonl", "sessions.jsonl", "taste.json", "trakt.json",
                "history.json", "learn.json", "online.json", "decisions.jsonl", "usage.json",
                "searches.jsonl", "emb.json", "facets.json", "memory.json", "memory.log.jsonl", "memory.prev.json", "interview.json") + (("connectors.json",) if CONNECTOR_BACKUP else ())
BACKUP_SKIP = {"addon.json": "secret, never opened", "remote.json": "secret, never opened", "clients.json": "secret, never opened",
               "credentials": "private credential contents, never read",
               "hydra.json": "secret, never opened", "decide_cache.json": "TTL LLM cache, refilled on demand",
               "feeds.json": "serving cache, rebuilt by feed_worker", "shown.json": "serving cache, rebuilt by feed_worker",
               "shown_scopes.json": "serving cache, starts from legacy pins when absent",
               "nzb.json": "serving cache, rebuilt from Hydra", "daily.json": "this runner's own state",
               "taste_ring.json": "bounded taste report cache, starts empty when absent",
               "memory.json.bad": "quarantined corrupt bytes, not usable state",
               "backups": "the archive dir itself"}
if not CONNECTOR_BACKUP:
    BACKUP_SKIP["connectors.json"] = "derived connector state, reseeded on load"
BACKUP_SECRET = re.compile(r"(?i)(token|secret|auth|cred|passw|\.env$|^\.env|^addon\.|^remote\.|^clients\.|^hydra\.)")
_BACKUP_NAME = re.compile(r"state-(\d{4}-\d{2}-\d{2})\.tar\.gz")


def backup_retain(days):
    days = sorted(set(days), reverse=True)
    keep = set(days[:14])
    weeks = {}
    for day in days[14:]:
        week = datetime.date.fromisoformat(day).isocalendar()[:2]
        if week in weeks or len(weeks) < 8:
            weeks[week] = day
    return keep | set(weeks.values())


def _backup_days(backups):
    days = {}
    for name in os.listdir(backups):
        match = _BACKUP_NAME.fullmatch(name)
        if match:
            try:
                datetime.date.fromisoformat(match[1])
            except ValueError:
                continue
            days[match[1]] = name
    return days


def _backup_clean(backups, now):
    import stat
    for name in os.listdir(backups):
        if name.startswith(".state-") and name.endswith(".tmp"):
            path = os.path.join(backups, name)
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > 3600:
                os.unlink(path)


def backup_prune(backups, today):
    days = _backup_days(backups)
    keep = backup_retain([*days, today]) | {today}
    deleted = 0
    for day, name in days.items():
        if day not in keep:
            os.unlink(os.path.join(backups, name))
            deleted += 1
    from skald.backup import migration_archives
    migrations = migration_archives(backups)
    for name in migrations[3:]:
        os.unlink(os.path.join(backups, name))
        deleted += 1
    _backup_clean(backups, time.time())
    return {"kept": len(days) + len(migrations) - deleted, "deleted": deleted}


class _BackupReader:
    def __init__(self, fd, size):
        self.fd, self.left = fd, size
        self.sha = hashlib.sha256()

    def read(self, size):
        raw = os.read(self.fd, min(size, self.left, 65536))
        if not raw and self.left:
            raise OSError("short backup read")
        self.left -= len(raw)
        self.sha.update(raw)
        return raw


def _backup_verify(path, manifest):
    import tarfile
    seen = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if member.name == "manifest.json":
                if json.loads(archive.extractfile(member).read(65536)) != manifest or archive.next() is not None:
                    raise ValueError("backup manifest mismatch")
                break
            rec = manifest["files"].get(member.name)
            if not rec or member.name in seen or member.type != tarfile.REGTYPE or member.size != rec["size"]:
                raise ValueError("backup member mismatch")
            sha = hashlib.sha256()
            with archive.extractfile(member) as src:
                while raw := src.read(65536):
                    sha.update(raw)
            if sha.hexdigest() != rec["sha256"]:
                raise ValueError("backup hash mismatch")
            seen.add(member.name)
        else:
            raise ValueError("backup manifest missing")
    if seen != set(manifest["files"]):
        raise ValueError("backup member missing")


def backup_due(now):
    day = online_day(now)[0]
    return not os.path.exists(os.path.join(DATA, "backups", f"state-{day}.tar.gz"))


def _backup_credential_inodes():
    from skald.backup import credential_inodes
    return credential_inodes(DATA)


def backup_state(now, *, archive_name=None):
    import errno
    import stat
    import tarfile
    day = online_day(now)[0]
    credential_inodes = _backup_credential_inodes()
    try:
        st = os.stat(os.environ.get("SKALD_CONFIG") or os.path.join(DATA, CONFIG_NAME))
    except FileNotFoundError:
        pass
    else:
        if (st.st_dev, st.st_ino) in credential_inodes:
            raise ValueError("config is a credential alias")
    cfg, source = load_config(DATA, readonly=True)
    household = source in ("file", "env")
    backups = os.path.join(DATA, "backups")
    os.makedirs(backups, mode=0o700, exist_ok=True)
    uid = os.getuid()
    if os.stat(backups).st_uid == uid:
        os.chmod(backups, 0o700)
    elif not os.access(backups, os.W_OK):
        raise PermissionError(f"backups dir not owned by uid {uid}: fix ownership")
    _backup_clean(backups, now)
    opened, skipped, files = {}, {}, {}
    tmp = None
    try:
        for name in (*BACKUP_FILES, *("trakt-" + p + ".history.json" for p in CONFIG["connectors"].get("trakt_api", {}).get("accounts", {}))):
            if name in ("skald.json", ".skald-household"):
                continue
            if BACKUP_SECRET.search(name):
                skipped[name] = "secret-like name"
                continue
            try:
                # NONBLOCK prevents a planted FIFO from stalling the worker before fstat.
                fd = os.open(os.path.join(DATA, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            except OSError as e:
                if e.errno not in (errno.ENOENT, errno.ELOOP):
                    raise
                skipped[name] = "link" if e.errno == errno.ELOOP else "missing"
                continue
            try:
                opened[name] = (fd, os.fstat(fd))
            except Exception:
                os.close(fd)
                raise
        raw_size = sum(st.st_size for _, st in opened.values())
        disk = os.statvfs(backups)
        if disk.f_bavail * disk.f_frsize < max(64 * 1024 * 1024, 3 * raw_size):
            raise OSError("low disk")
        fd, tmp = tempfile.mkstemp(prefix=f".state-{day}.", suffix=".tmp", dir=backups)
        with os.fdopen(fd, "wb") as fh:
            with tarfile.open(fileobj=fh, mode="w:gz", compresslevel=6) as archive:
                if household:
                    for name, raw in (("skald.json", (json.dumps(public_config(cfg), ensure_ascii=False, indent=1) + "\n").encode()),
                                      (".skald-household", b"")):
                        info = tarfile.TarInfo(name)
                        info.size, info.mode, info.mtime = len(raw), 0o600, int(now)
                        with tempfile.TemporaryFile(dir=backups) as receipt:
                            receipt.write(raw)
                            receipt.seek(0)
                            archive.addfile(info, receipt)
                        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "mtime": int(now)}
                for name, (source, st) in opened.items():
                    if not stat.S_ISREG(st.st_mode):
                        skipped[name] = "not a regular file"
                        continue
                    if (st.st_dev, st.st_ino) in credential_inodes:
                        skipped[name] = "credential alias"
                        continue
                    size = st.st_size
                    if size > 64 * 1024 * 1024:
                        skipped[name] = "too large"
                        continue
                    if name.endswith(".jsonl"):
                        end = size
                        size = 0
                        while end:
                            start = max(0, end - 65536)
                            os.lseek(source, start, os.SEEK_SET)
                            block = os.read(source, end - start)
                            if len(block) != end - start:
                                raise OSError("short backup read")
                            newline = block.rfind(b"\n")
                            if newline >= 0:
                                size = start + newline + 1
                                break
                            end = start
                    os.lseek(source, 0, os.SEEK_SET)
                    reader = _BackupReader(source, size)
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = size, 0o600, int(st.st_mtime)
                    info.type = tarfile.REGTYPE
                    archive.addfile(info, reader)
                    files[name] = {"sha256": reader.sha.hexdigest(), "size": size, "mtime": info.mtime}
                manifest = {"v": 1, "day": day, "created": int(now), "files": files, "skipped": skipped}
                if household:
                    manifest["household"] = True
                raw = json.dumps(manifest, separators=(",", ":")).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode, info.mtime = len(raw), 0o600, int(now)
                with tempfile.TemporaryFile(dir=backups) as receipt:
                    receipt.write(raw)
                    receipt.seek(0)
                    archive.addfile(info, receipt)
            fh.flush()
            os.fsync(fh.fileno())
        _backup_verify(tmp, manifest)
        size = os.stat(tmp).st_size
        os.replace(tmp, os.path.join(backups, archive_name or f"state-{day}.tar.gz"))
        tmp = None
        try:
            fd = os.open(backups, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        return {"files": len(files), "raw": sum(r["size"] for r in files.values()), "bytes": size,
                "skipped": len(skipped), **backup_prune(backups, day)}
    finally:
        for fd, _ in opened.values():
            os.close(fd)
        if tmp is not None:
            os.unlink(tmp)


# --- daily jobs ---
DAILY_JOBS = [("backup", backup_due, backup_state)]
DAILY_JOBS.append(("memory", memory_due, memory_job))
_daily = None
_daily_lock = threading.Lock()


def _daily_save():
    fd, tmp = tempfile.mkstemp(prefix=".daily-", suffix=".tmp", dir=DATA)
    try:
        with os.fdopen(fd, "w") as fh:
            with _daily_lock:
                json.dump(_daily, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(DATA, "daily.json"))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _daily_log(message):
    try:
        print(message, flush=True)
    except Exception:
        pass


def daily_jobs(now):
    global _daily
    try:
        today = online_day(now)[0]
        with _daily_lock:
            if _daily is None:
                try:
                    with open(os.path.join(DATA, "daily.json")) as fh:
                        state = json.load(fh)
                    _daily = {k: v for k, v in state.items() if isinstance(v, dict)}
                except (OSError, ValueError, AttributeError):
                    _daily = {}
        for name, due, run in DAILY_JOBS:
            with _daily_lock:
                previous = dict(_daily.get(name, {}))
            if previous.get("day") == today:
                continue
            start = time.monotonic()
            last_ok = previous.get("last_ok")
            try:
                if not due(now):
                    rec = {"day": today, "skipped": True, "last_ok": last_ok}
                else:
                    out = run(now)
                    try:
                        n = len(json.dumps(out, allow_nan=False).encode())
                    except (TypeError, ValueError):
                        n = 0
                    if not isinstance(out, dict) or any(type(k) is not str or type(v) not in (int, str)
                                                        for k, v in out.items()) or not n or n > 1024:
                        out = {"too_big": n}
                    rec = {"day": today, "ok": True, "ms": int((time.monotonic() - start) * 1000),
                           "out": out, "last_ok": today}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
                rec = {"day": today, "ok": False, "ms": int((time.monotonic() - start) * 1000),
                       "err": err, "last_ok": last_ok}
                _daily_log(f"daily {name} failed: {err}")
            with _daily_lock:
                _daily[name] = rec
            try:
                _daily_save()
            except Exception as e:
                _daily_log(f"daily state failed: {type(e).__name__}: {e}"[:240])
    except Exception as e:
        _daily_log(f"daily failed: {type(e).__name__}: {e}"[:240])


def daily_stats():
    with _daily_lock:
        out = json.loads(json.dumps(_daily or {}))
    sizes = {}
    try:
        backups = os.path.join(DATA, "backups")
        for day, name in _backup_days(backups).items():
            sizes[day] = os.stat(os.path.join(backups, name), follow_symlinks=False).st_size
    except OSError:
        pass
    out["backups"] = {"count": len(sizes), "newest": max(sizes, default=None), "bytes": sum(sizes.values())}
    return out


# --- jobs ------------------------------------------------------------------------

def job_specs(d, hashes):
    """(job, row ids, stamp, min seconds between taste-only rebuilds, builder)."""
    # Rows roll over at 05:00, not midnight: the late-night slot stays one build.
    ld = d - datetime.timedelta(hours=5)
    day, hh = ld.strftime("%Y-%m-%d"), hashes.get("household", "")
    specs = [
        ("episodes", [rid('episodes')], f"{day}/{d.hour // 3}/{hh}{hashes.get('episodes', '')}", 1800, lambda l: {rid('episodes'): episodes_feed(l)}),
        ("because", [rid('because')], f"{day}/{hh}", 1800, because_feed),
        ("rewatch", [rid('rewatch')], f"{day}/{hh}", 1800, lambda l: {rid('rewatch'): rewatch_feed(l)}),
        ("upcoming", [rid('upcoming')], f"{day}/{d.hour // 6}/{hh}", 1800, lambda l: {rid('upcoming'): upcoming_feed(l)}),
        ("catchup", [rid('catchup')], f"{day}/{hh}{hashes.get('episodes', '')}", 1800, lambda l: {rid('catchup'): catchup_feed(l)}),
        ("lanes", LANE_ROWS, f"r3/{day}/{hh}", 3600, lanes_feed),
        ("leap", [rid('leap')], f"{day}/{hh}", 3600, leap_feed),
        *((person, [cid], f"{day}/{hh}", 3600, lambda l, p=person: member_feed(l, p)) for cid, person in ROW_OWNER.items()),
        ("moods", MOODS_ROWS, f"{day}/{hh}", 3600, mood_feed),
        ("docs", DOCS, f"nzb/{day}/{(_feeds.get((rid('docsai'), LANG)) or {}).get('built', 0)}/{hh}", 3600, docs_feed),
        ("studios", STUDIO_ROWS, f"{day}/{hh}", 3600, studios_feed),
        ("top10", [rid('top10')], f"{day}/{d.hour // 12}", 0, lambda l: {rid('top10'): top10_feed(l)}),
        ("airing", [rid('airing')], f"{day}/{d.hour // 6}", 0, lambda l: {rid('airing'): anime_feed(l, False)}),
        ("nextseason", [rid('nextseason')], day, 0, lambda l: {rid('nextseason'): anime_feed(l, True)}),
    ]
    if OR_KEY:
        specs += [
            ("personal", [rid('foryou'), rid('rewatchai')], f"{day}/{ld.hour // 12}/{hh}", 3600, personal_feed),
            # once a week (Monday 05:00 rollover), whatever the taste does in between
            ("week", [rid('week')], "{}-W{:02d}".format(*ld.isocalendar()[:2]), 0, week_feed),
            ("docsai", [rid('docsai')], "{}-W{:02d}-{}".format(*ld.isocalendar()[:2], len(DOC_ROWS)), 0, docs_ai_feed),
        ]
        for person in ARCHIVED:
            if person in hashes:
                specs.append((person, [rid(person)], f"{day}/{hashes[person]}", 3 * 3600, lambda l, p=person: archived_feed(l, p)))
    return specs


# AIOStreams caches our manifest (with the row names) for hours but fetches rows fresh. Rows whose name changes
# (because, lanes) are therefore served as they were when the manifest was last fetched, so name and content agree.
DYNAMIC = [rid('because'), rid('leap')] + MOODS_ROWS + LANE_ROWS
_shown = {}
_shown_scoped = {}
_scoping = threading.local()
_scoping.scope = None
_pinning = threading.local()
_pin_cache = {}
_pin_epoch = 0
_pin_lock = threading.RLock()


def pin_decision(lang):
    # Lock order: _pin_lock -> _home_lock -> _feeds_lock; _connectors_lock is a leaf, taken alone; row evaluation holds neither home nor feeds lock.
    with _pin_lock:
        masks, p, m = remote_masks(), _profiles.get("household"), get_model(False)
        # Each session's served rows apply its own memory veto (W10b); none and both differ there.
        masks_by = {who: (*masks, veto_keys(who)) for who in ("none", *PEOPLE, "both")}
        with _feeds_lock:
            generation = tuple((0, k, id(v)) for k, v in sorted(_feeds.items()) if k[1] == lang)
            key = (lang, generation, id(p), p.get("hash") if p else None,
                   tuple(frozenset(s) for who in masks_by for s in masks_by[who]), local_now().date(), day_index(), id(m), _pin_epoch)
            cached = _pin_cache.get(lang)
            if cached and cached[0] == key:
                return cached[1]
            refs = [v for k, v in _feeds.items() if k[1] == lang]
            records = {**_feeds, **_shown}
            pins = {(cid, lang): _feeds[cid, lang] for cid in DYNAMIC if _feeds.get((cid, lang))}
        records.update(pins)
        _pinning.feeds = records
        try:
            above = {who: set() for who in masks_by}
            for cid in DEDUPE_ORDER[:DEDUPE_ORDER.index(LANES[0])]:
                for typ in ("movie", "series") if cid == rid('top10') else ("tv",) if cid in MIXED else ("series",):
                    for who in above:
                        above[who] = mark_above(above[who], cid, served_row(cid, typ, lang, p or {}, above[who], masks_by[who], who=who))
            used = set()
            for cid in LANES:
                primary = records.get((cid, lang)) or {}
                for bench, candidate in [(None, primary)] + [(b, records.get((b, lang)) or {}) for b in LANE_BENCH]:
                    fid = (candidate.get("names") or {}).get("lane")
                    if bench and (not fid or fid in used):
                        continue
                    records[cid, lang] = {**candidate, "_bench": bench} if bench else candidate
                    rows = {who: served_row(cid, "tv", lang, p or {}, ids, masks_by[who], who=who) for who, ids in above.items()}
                    if not bench:
                        primary_rows = rows
                    if all(len(row) >= ROW_MIN for row in rows.values()):
                        break
                else:
                    records[cid, lang] = primary
                    rows = primary_rows
                pins[cid, lang] = records[cid, lang]
                used.add((records[cid, lang].get("names") or {}).get("lane"))
                for who, row in rows.items():
                    above[who] = mark_above(above[who], cid, row)
        finally:
            del _pinning.feeds
        _pin_cache[lang] = (key, pins, refs, p, m)  # Retain identities until this generation is replaced.
        return pins


@contextmanager
def addon_scope(scope):
    if scope is not None:
        if scope not in SCOPES:
            raise ValueError("unknown addon scope")
        if scope not in _shown_scoped:
            with _home_lock, _feeds_lock:
                _shown_scoped.setdefault(scope, _shown.copy())
    previous = getattr(_scoping, "scope", None)
    _scoping.scope = scope
    try:
        yield
    finally:
        _scoping.scope = previous


def pin_shown(lang, scope=None):
    if scope is not None and scope not in SCOPES:
        raise ValueError("unknown addon scope")
    with _pin_lock:
        pins = pin_decision(lang)
        with _home_lock, _feeds_lock:
            shown = _shown if scope is None else _shown_scoped.setdefault(scope, _shown.copy())
            shown.update(pins)
            snap = {f"{cid}|{l}": v for (cid, l), v in _shown.items()} if scope is None else {
                s: {f"{cid}|{l}": v for (cid, l), v in store.items()} for s, store in _shown_scoped.items()}
        path = os.path.join(DATA, "shown.json" if scope is None else "shown_scopes.json")
        try:
            with open(path + ".tmp", "w") if scope is None else os.fdopen(
                    os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                if scope is not None:
                    os.fchmod(fh.fileno(), 0o600)
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


def feed(cid, lang):
    if hasattr(_pinning, "feeds"):
        return _pinning.feeds.get((cid, lang)) or {}
    scope = getattr(_scoping, "scope", None)
    shown = _shown if scope is None else _shown_scoped[scope]
    return shown.get((cid, lang)) or _feeds.get((cid, lang)) or {}


def save_feeds():
    with _feeds_lock:
        snap = {f"{cid}|{lang}": v for (cid, lang), v in _feeds.items()}
    tmp = os.path.join(DATA, "feeds.json.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(snap, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(DATA, "feeds.json"))
    except OSError:
        pass


def load_state():
    connectors_load()
    import zlib
    ring = {}
    try:
        with open(os.path.join(DATA, "taste_ring.json")) as fh:
            stored = json.load(fh)
        for day, people in stored.items():
            datetime.date.fromisoformat(day)
            ring[day] = {p: base64.b64decode(people[p], validate=True) for p in ("household", *PEOPLE)}
            for raw in ring[day].values():
                values = json.loads(zlib.decompress(raw))
                if not isinstance(values, dict) or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values.values()):
                    raise ValueError("invalid taste ring")
        ring = {day: ring[day] for day in sorted(ring)[-TASTE_RING_DAYS:]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, zlib.error):
        ring = {}
    with _taste_ring_lock:
        _taste_ring.clear()
        _taste_ring.update(ring)
    try:
        with open(os.path.join(DATA, "shown.json")) as fh:
            for key, v in json.load(fh).items():
                cid, lang = key.split("|", 1)
                _shown[(cid, lang)] = v
    except Exception:
        pass
    scoped = {scope: _shown.copy() for scope in SCOPES}
    try:
        with open(os.path.join(DATA, "shown_scopes.json")) as fh:
            stored = json.load(fh)
        if not isinstance(stored, dict):
            raise ValueError("invalid scope pins")
        for scope in SCOPES:
            if scope not in stored:
                continue
            records = {}
            for key, v in stored[scope].items():
                cid, lang = key.split("|")
                if not cid or not LANG_RE.fullmatch(lang) or not isinstance(v, dict) or any(
                        not isinstance(v.get(k, {}), dict) for k in ("metas", "names")):
                    raise ValueError("invalid scope pins")
                records[cid, lang] = v
            scoped[scope] = records
    except (OSError, ValueError, TypeError, AttributeError):
        scoped = {scope: _shown.copy() for scope in SCOPES}
    with _home_lock, _feeds_lock:
        _shown_scoped.clear()
        _shown_scoped.update(scoped)
    try:
        with open(os.path.join(DATA, "feeds.json")) as fh:
            for key, v in json.load(fh).items():
                cid, lang = key.split("|", 1)
                _feeds[(cid, lang)] = v
                _langs.add(lang)
    except Exception:
        pass
    try:
        with open(os.path.join(DATA, "searches.jsonl")) as fh:
            for line in fh.readlines()[-500:]:
                rec = json.loads(line)
                _searches.append((rec["t"], rec["q"]))
    except Exception:
        pass
    try:
        with open(os.path.join(DATA, "usage.json")) as fh:
            _usage.update(json.load(fh))
    except Exception:
        pass
    load_decisions()
    try:
        with open(os.path.join(DATA, "learn.json")) as fh:
            _learn.update(json.load(fh))
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(DATA, "nzb.json")) as fh:
            _nzb.update(json.load(fh))
    except Exception:
        pass
    load_emb()
    load_facets()
    keep = {cid for cid, _, _ in feed_rows()} | set(LANE_BENCH) | {rid('rewatchai'), *{rid(p) for p in ARCHIVED}, rid('docsai')}
    for key in [k for k in _feeds if k[0] not in keep]:  # retired rows (gems, season, now, short)
        _feeds.pop(key)


def feed_pass(d, retry):
    try:
        hashes = refresh_profiles()
    except Exception as e:
        print(f"profiles failed: {type(e).__name__}: {e}", flush=True)
        hashes = {}
    for lang in list(_langs):
        try:
            for job, cids, stamp, min_gap, build in job_specs(d, hashes):
                try:
                    stamp = f"v{STAMP_V}/{stamp}"
                    cur = _feeds.get((cids[0], lang))
                    if (cur and cur.get("stamp") == stamp) or retry.get((job, lang), 0) > time.time():
                        continue
                    # Same time window, only the taste changed: rebuild at most every min_gap seconds.
                    if cur and min_gap and cur.get("stamp", "").split("/")[:-1] == stamp.split("/")[:-1] \
                            and time.time() - cur.get("built", 0) < min_gap:
                        continue
                    try:
                        failures = [0]
                        _upstream.failures = failures
                        try:
                            rows = build(lang)
                        finally:
                            _upstream.failures = None
                    except BudgetExceeded as e:
                        print(f"job {job} {lang} skipped: {e}", flush=True)
                        retry[(job, lang)] = time.time() + 3600
                        continue
                    except Exception as e:
                        print(f"job {job} {lang} failed: {type(e).__name__}: {e}", flush=True)
                        retry[(job, lang)] = time.time() + 600
                        continue
                    if failures[0] and not any(v for cid in cids for k, v in ((rows or {}).get(cid) or {}).items() if k != "pool"):
                        retry[(job, lang)] = time.time() + 600
                        continue
                    if rows is None:  # no taste signal yet: keep the old row, check again later
                        with _feeds_lock:
                            for cid in cids:
                                if (cid, lang) not in _feeds:
                                    _feeds_empty.add((cid, lang))
                        retry[(job, lang)] = time.time() + 1800
                        continue
                    names = rows.pop("_names", None) or {}
                    with _feeds_lock:
                        for cid in cids:
                            rec = {"stamp": stamp, "built": int(time.time()), "metas": rows.get(cid) or {}}
                            if names.get(cid):
                                rec["names"] = names[cid]
                            _feeds[(cid, lang)] = rec
                            _feeds_empty.discard((cid, lang))
                    print(f"job {job} {lang} built: " + ", ".join(
                        f"{cid.split('.')[1]}.{k}={len(v)}" for cid in cids for k, v in (rows.get(cid) or {}).items()), flush=True)
                    try:
                        save_feeds()
                    except Exception as e:
                        _daily_log(f"save feeds failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
                except Exception as e:
                    _daily_log(f"job {job} {lang} failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
            pin_decision(lang)
        except Exception as e:
            _daily_log(f"feed lang {lang} failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])


def feed_worker():
    retry = {}
    while True:
        try:
            connectors_tick(time.time())
        except Exception as e:
            _daily_log(f"connectors tick failed: {type(e).__name__}")
        try:
            feed_pass(local_now(), retry)
        except Exception as e:
            _daily_log(f"feed pass failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
        for name, save in (("learn", save_learn), ("nzb", save_nzb), ("emb", save_emb), ("facets", save_facets)):
            try:
                save()
            except Exception as e:
                _daily_log(f"save {name} failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
        daily_jobs(time.time())
        time.sleep(60)


def interleave(*lists):
    out, seen = [], set()
    for group in zip(*(lst + [None] * (max(map(len, lists)) - len(lst)) for lst in lists)):
        for m in group:
            if m and m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    return out


# Serving facts stay with the feed, so row requests never fetch metadata.
ROW_GENRES = {14: "Fantasy", 16: "Animation", 80: "Crime", 99: "Documentary", 9648: "Mystery", 10402: "Music",
              10751: "Family", 10762: "Kids", 10764: "Reality"}
REASON_STOP = {"ghost", "cop", "las vegas", "thanksgiving", "christmas", "halloween", "scotland", "holiday",
               "holidays", "new york city", "london", "paris", "witch", "coming of age"}
BECAUSE_CUTOFF = 0.28
WEEK_SEED_FLOOR = .64
WEEK_TAIL_FLOOR = .45
WEEK_PENALTY = .25


def row_meta(it, stype, kind, names, en, mark="", note="", collection=None):
    meta = to_meta(it, stype, kind, names, en, mark, note)
    col = it.get("belongs_to_collection") or en.get("belongs_to_collection") or {}
    meta["_row"] = {"votes": it.get("vote_count"), "rating": it.get("vote_average"),
                    "date": it.get("release_date") or it.get("first_air_date"), "collection": collection or col.get("id"),
                    "collection_name": col.get("name"),
                    "c": en.get("production_companies") or it.get("production_companies") or [],
                    "k": (en.get("keywords") or it.get("keywords") or {}).get("keywords") or
                         (en.get("keywords") or it.get("keywords") or {}).get("results") or [],
                    "g": [g.get("name") for g in en.get("genres") or it.get("genres") or []] +
                         [ROW_GENRES[g] for g in (it.get("genre_ids") or [g["id"] for g in it.get("genres") or []]) if g in ROW_GENRES],
                    "lang": it.get("original_language") or en.get("original_language"), "english": en.get("title") or en.get("name"),
                    "certification": it.get("certification") or en.get("certification"),
                    "status": it.get("status") or en.get("status"),
                    "runtime": it.get("runtime") or en.get("runtime"),
                    "overview": it.get("overview") or en.get("overview") or "",
                    "counts": {str(s["season_number"]): s.get("episode_count") or 0 for s in en.get("seasons") or []}}
    return meta


def row_facts(meta):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    facts = dict(meta.get("_row") or {})
    facts["g"] = set(facts.get("g") or ()) | set(rec.get("g") or ()) | set(meta.get("genres") or ())
    facts["lang"] = facts.get("lang") or rec.get("lang")
    facts.setdefault("rating", float(meta["imdbRating"]) if meta.get("imdbRating") else None)
    facts.setdefault("date", (meta.get("releaseInfo") or rec.get("y") or "")[:4] + "-01-01")
    return facts


def franchise_key(meta):
    col = row_facts(meta).get("collection")
    if col:
        return f"collection:{col}"
    name = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv").get("title") or meta.get("name") or ""
    name = re.sub(r"\s*[:–—].*$", "", name)
    name = re.sub(r"\s+(?:(?:Part|Vol\.?|Book)\s*)?(?:\d+|[IVX]+)\s*$", "", name, flags=re.I)
    return base_title(name) or meta["id"]


def universe_key(meta):
    rec, facts = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv"), row_facts(meta)
    words = [n for field in ("c", "k") for _, n in rec.get(field) or []]
    words += [x.get("name", "") for field in ("c", "k") for x in facts.get(field) or []]
    words += [facts.get("collection_name") or ""]
    text = " | ".join(words).casefold()
    name = (rec.get("title") or facts.get("english") or meta.get("name") or "").casefold()
    for key, pattern, prefix in (
            ("marvel", r"marvel (?:studios|entertainment|cinematic universe)", r"^marvel(?:['’]s)?\s"),
            ("dc", r"\bdc(?: films| studios| entertainment| universe| extended universe)?\b", r"^dc(?:['’]s)?\s"),
            ("starwars", r"star wars|lucasfilm", r"^star wars\b"),
            ("wizarding", r"wizarding world|harry potter|fantastic beasts", r"^(?:harry potter|fantastic beasts)\b")):
        if re.search(pattern, text) or re.search(prefix, name):
            return key
    return None


def row_seed(cid, lang):
    names = feed(cid, lang).get("names") or {}
    key = names.get("seed")
    if key:
        return key
    title = re.sub(r"^(Because You (?:Love|Like|Watched|Watch) |Same Feeling as )", "", names.get("title") or "")
    return next((e["key"] for e in (_profiles.get("household") or {}).get("entries", [])
                 if norm_title(e["title"]) == norm_title(title)), None)


def tone_ok(meta, seed=None, animation=False):
    g = row_facts(meta)["g"]
    allowed = set(frec(seed, profile_kind(seed)).get("g") or ())
    blocked = {"Family", "Kids"} | ({"Animation"} if animation else set())
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    words = " ".join(n for _, n in rec.get("k") or [])
    cert = (meta.get("_row") or {}).get("certification") or ""
    kids = cert in ("G", "TV-Y", "TV-Y7") or bool(re.search(r"\b(children|pre.teen|young adult|teen fantasy)\b", words, re.I))
    kids |= bool("Fantasy" in g and re.search(r"\bteenage girl\b", words, re.I) and
                 re.search(r"\b1[0-3][ -]year[ -]old\b", rec.get("o") or "", re.I))
    return not (g & blocked - allowed) and not (kids and not allowed & {"Family", "Kids"})


def quality_ok(meta, docs=False):
    f = row_facts(meta)
    rating, votes = f.get("rating"), f.get("votes")
    if docs:
        return (rating is None or rating >= 6) and (votes is None or votes >= 20)
    d = date_or_none(f.get("date"))
    if d and 0 <= (local_now().date() - d).days <= 120:
        return True
    return (rating is None or rating >= 6.5) and (votes is None or votes >= (200 if meta["type"] == "movie" else 50))


def doc_ok(meta, cid):
    if not quality_ok(meta, docs=True) or "Music" in row_facts(meta)["g"]:
        return False
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    facts = row_facts(meta)
    if facts.get("runtime") and facts["runtime"] < 30 and facts["g"] & {"Self-help", "Self Help", "Reality"}:
        return False
    words = " ".join(n for _, n in rec.get("k") or []) + " " + meta.get("name", "")
    if re.search(r"\b(how.to|self.help|meditation|headspace|guide to)\b", words, re.I):
        return False
    return cid not in (rid('history'), rid('meaning')) or not re.search(
        r"\b(celebrity|actor|actress|singer|musician|music|concert|hollywood|film director)\b", words, re.I)


def is_anime(meta):
    f = row_facts(meta)
    return "Animation" in f["g"] and f.get("lang") == "ja"


def is_hero(meta):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    return any("superhero" in n.casefold() or "cinematic universe" in n.casefold() for _, n in rec.get("k") or [])


def reason_ok(name, key, fid="", kind=None):
    rec = frec(key, kind)
    return bool(name) and norm_title(name) not in REASON_STOP and norm_title(name) != norm_title(rec.get("title")) \
        and not (fid[:1] in "kt" and any(norm_title(name) == norm_title(n) for _, n in rec.get("a") or []))


def clean_note(note):
    note = re.sub(r"(?<=[A-Za-z])\.[a-z_]{3,}\b", "", note).strip()
    if len(note) > 90:
        note = note[:91].rsplit(" ", 1)[0].rstrip(" ,;:·")
    return note.rstrip(" .") + "." if note else ""


def card_parts(meta):
    desc = meta.get("description") or ""
    overview = (meta.get("_row") or {}).get("overview")
    parts = desc.split(" · ")
    if overview is not None:
        note = desc[:-len(overview)].rstrip(" ·") if overview and desc.endswith(overview) else desc
        return note.split(" · "), overview
    # Old pinned feeds have no serving facts; their final segment is the overview.
    if len(parts) > 1 or len(desc) > 90:
        return parts[:-1], parts[-1]
    return parts, ""


def human_cards(metas):
    counts, out = {}, []
    for i, meta in enumerate(metas):
        parts, overview = card_parts(meta)
        notes = [meta["_memory"]["why"]] if meta.get("_memory") else []
        title = norm_title(frec(meta["id"], "movie" if meta["type"] == "movie" else "tv").get("title") or meta.get("name"))
        for part in parts:
            if notes:
                break
            part = part.strip()
            if part in ("Strong fit", "Good fit", "Wildcard") or not part:
                continue
            a = re.search(r"^(.*?)\b(?:like|loved|love)\s+(.+)$", part, re.I)
            if a:
                head, a = a[1].strip(), a[2]
                if head and not reason_ok(head, meta["id"], kind="movie" if meta["type"] == "movie" else "tv"):
                    continue
                if norm_title(re.sub(r"\s*\([^)]*\)$", "", a)) == title:
                    continue
                if i >= 2:
                    part = head
                    if re.search(r"\bbecause\b", part, re.I):
                        continue
            elif i >= 2 and re.search(r"\b(because|loved|feels like)\b", part, re.I):
                continue
            if not reason_ok(part, meta["id"], kind="movie" if meta["type"] == "movie" else "tv"):
                continue
            part = clean_note(part)
            if counts.get(part.casefold(), 0) >= 2 or part in notes:
                continue
            notes.append(part)
            counts[part.casefold()] = counts.get(part.casefold(), 0) + 1
            break  # one warm reason is enough beside an overview
        card = {k: v for k, v in meta.items() if k not in ("_row", "_memory")}
        # shortcut: pre-upgrade pins lack vote counts; omit their rating until their builder refreshes.
        if ((meta.get("_row") or {}).get("votes") or 0) < 50:
            card.pop("imdbRating", None)
        desc = " · ".join(x for x in (" · ".join(notes), overview) if x)
        if desc:
            card["description"] = desc
        else:
            card.pop("description", None)
        out.append(card)
    return LaneRow(out, metas.names) if isinstance(metas, LaneRow) else out


def pool_pct(values):
    """Keyed row scores to [0, 1] ranks; ties average, missing evidence stays zero."""
    ordered = sorted(v for v in values.values() if v is not None)
    ranks = {v: (bisect.bisect_left(ordered, v) + bisect.bisect_right(ordered, v) - 1) / (2 * (len(ordered) - 1))
             if len(ordered) > 1 else .5 for v in set(ordered)}
    return {k: ranks[v] if v is not None else 0 for k, v in values.items()}


def explicit_seeds(person):
    p = _profiles.get("household") or {"entries": []}
    return list(dict.fromkeys(e["key"] for e in person_entries(p, person) if e.get("explicit") and e["weight"] > 0))


def member_weight(n):
    return .8 if n < 20 else .5 if n < 40 else .2


def member_scores(metas, person):
    m = get_model(False)
    seeds = explicit_seeds(person)
    knn = pool_pct({x["id"]: seed_knn(seeds, x["id"], 1) for x in metas})
    taste = pool_pct({x["id"]: score_key(m, x["id"], person, "movie" if x["type"] == "movie" else "tv")[0] for x in metas})
    w = member_weight((m or {}).get("nexp", {}).get(person, 0))
    return {k: w * knn[k] + (1-w) * taste[k] for k in knn}


def member_balance(metas, person, strict=False):
    seeds = explicit_seeds(person)
    if not seeds:
        return metas
    counts, front, rest, dark, bleak_ids = {}, [], [], 0, set()
    for x in metas:
        seed = max(seeds, key=lambda k: seed_similarity(k, x["id"]))
        tags = frec(x["id"], "movie" if x["type"] == "movie" else "tv").get("x") or {}
        bleak = bool({"dark", "grim", "bleak", "dread"} & set(sorted(tags, key=lambda t: (-tags[t], t))[:3]))
        if bleak:
            bleak_ids.add(x["id"])
        if len(front) < 10 and counts.get(seed, 0) < 4 and (not bleak or dark < 1):
            front.append(x)
            counts[seed] = counts.get(seed, 0) + 1
            dark += bleak
        else:
            rest.append(x)
    # Preserve the previous shelf's one dread pick in ten and five in twenty.
    out, tail = list(front), []
    for x in rest:
        if len(out) < 20 and x["id"] in bleak_ids and dark >= 5:
            tail.append(x)
        else:
            out.append(x)
            if len(out) <= 20:
                dark += x["id"] in bleak_ids
    return out if strict else out + tail


def because_ok(meta, seed):
    if not seed or not tone_ok(meta, seed, animation=True) or not quality_ok(meta):
        return False
    year = frec(seed, profile_kind(seed)).get("y") or "1980"
    date = row_facts(meta).get("date") or "1980"
    if year >= "1980" and date[:4] < "1980":
        return False
    rec = frec(seed, profile_kind(seed))
    anchor = {"id": seed, "type": "movie" if profile_kind(seed) != "tv" else "series", "name": rec.get("title") or seed}
    if not set(rec.get("g") or ()) & row_facts(meta)["g"] and not (franchise_key(meta) == franchise_key(anchor) or
            (universe := universe_key(anchor)) and universe == universe_key(meta)):
        return False
    return seed_similarity(seed, meta["id"]) >= BECAUSE_CUTOFF


def because_rank(metas, seed):
    m = get_model(False)
    return sorted(metas, key=lambda x: (seed_similarity(seed, x["id"]), score_key(m, x["id"], kind="movie" if x["type"] == "movie" else "tv")[0] or 0), reverse=True)


def because_balance(metas, seed, limit=20):
    kind = frec(seed, profile_kind(seed)).get("kind")
    kind = "series" if kind == "tv" else kind
    if not kind:
        kind = next((e["stype"] for e in (_profiles.get("household") or {}).get("entries", []) if e["key"] == seed), "movie")
    own = [x for x in metas if x["type"] == kind]
    other = [x for x in metas if x["type"] != kind]
    if own:
        other = other[:len(own) // 2]
    # Keep the seed's medium in two of every three slots, as far as the pool allows.
    out = []
    while own or other:
        source = other if (other and len(out) % 3 == 2) or not own else own
        out.append(source.pop(0))
    return out[:limit]


def member_pick(meta, person):
    notes, _ = card_parts(meta)
    if any(MEMBER_PICK_RE[person].search(s) for s in notes):
        return True
    m = person_model(get_model(False), person)
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    if not m or is_hero(meta) or not (procedural(meta) or any(rec.get("x", {}).get(x, 0) >= 2 for x in EXPERIENCE_TAGS.get(person, ()))):
        return False
    s = score_key(m, meta["id"], person, "movie" if meta["type"] == "movie" else "tv")[0]
    pct = match_pct(m, s, person)
    return pct >= 70 if pct is not None else s is not None and s > 0


def procedural(meta):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    return "Crime" in row_facts(meta)["g"] and any(re.search(r"detective|investigation|police procedural", n, re.I)
                                                  for _, n in rec.get("k") or [])


def balance_row(metas, foryou=False, anime=False):
    # Select the whole shelf before polishing its first five.
    out, tail, heroes = [], [], 0
    cap = max(1, min(20, len(metas)) // 3)
    for meta in metas:
        if (foryou and is_hero(meta) and heroes >= cap) or (not anime and is_anime(meta)):
            tail.append(meta)
            continue
        out.append(meta)
        heroes += is_hero(meta)
    limit = int(len(out) * 2 / 3)  # anime <= 40% after adding it to the non-anime pool
    out += [x for x in tail if is_anime(x) and (not foryou or not is_hero(x))][:limit] if not anime else tail
    if foryou:
        warm = [x for x in out if any(WARM_RE.search(n) for n in card_parts(x)[0])][:5]
        hers = [x for x in out if x not in warm and any(member_pick(x, person) for person in MEMBER_ROWS)]
        other = [x for x in out if x not in warm and not any(member_pick(x, person) for person in MEMBER_ROWS)]
        out = list(warm)
        while hers or other:
            source = hers if hers and (len(out) % 3 == 0 or not other) else other
            x = source.pop(0)
            if source is hers:
                notes, overview = card_parts(x)
                if not any("For " in s for s in notes):
                    x = {**x, "description": " · ".join(s for s in ("For " + LABELS[next(p for p in MEMBER_ROWS if member_pick(x, p))], x.get("description")) if s)}
            out.append(x)
    return out


def polish_row(metas, cap=2, anime=False, universe_cap=4):
    groups = {}
    for meta in metas:
        key = franchise_key(meta)
        groups.setdefault(key, []).append(meta)
    # Originals lead their sequels even when taste ranked the sequel higher.
    for key, group in groups.items():
        group.sort(key=lambda x: row_facts(x).get("date") or "9999")
        groups[key] = group[:cap]
    kept = [groups[franchise_key(x)].pop(0) for x in metas if groups[franchise_key(x)]]
    art = lambda x: bool(x.get("poster") and x.get("background"))
    strong = lambda x: art(x) and (row_facts(x).get("rating") is None or row_facts(x)["rating"] >= 6.8)
    front, rest, used, animated, universes = [], [], set(), 0, {}
    counts = {}
    for meta in kept:
        key = franchise_key(meta)
        universe = universe_key(meta)
        if universe and universe_cap is not None and counts.get(universe, 0) >= universe_cap:
            continue
        if universe:
            counts[universe] = counts.get(universe, 0) + 1
        if len(front) < 5 and strong(meta) and key not in used and (universe_cap is None or universes.get(universe, 0) < 2) and (anime or not is_anime(meta) or animated < 2):
            front.append(meta)
            used.add(key)
            animated += is_anime(meta)
            if universe:
                universes[universe] = universes.get(universe, 0) + 1
        else:
            rest.append(meta)
    if len(front) < 5 and _profiles.get("household"):
        return front
    return front + [x for x in rest if art(x)] + [x for x in rest if not art(x)]


def rotate_row(metas, cid):
    if cid in (rid('top10'), rid('episodes'), rid('catchup')):
        return metas
    out = list(metas)
    rng = random.Random(f"{day_index()}:{cid}")
    illustrated = next((i for i, x in enumerate(out) if not (x.get("poster") and x.get("background"))), len(out))
    for start in (5, 10, 15):
        end = max(start, min(start + 5, illustrated))
        if cid == rid('foryou') and start == 15:
            end = min(end, 18)  # keep the household balance of the first 18 through rotation.
        block = out[start:end]
        rng.shuffle(block)
        out[start:end] = block
    positions = {}
    for i, x in enumerate(out):
        positions.setdefault(franchise_key(x), []).append(i)
    for slots in positions.values():
        ordered = sorted((out[i] for i in slots), key=lambda x: row_facts(x).get("date") or "9999")
        for i, x in zip(slots, ordered):
            out[i] = x
    return out


def episode_label(det, season, episode):
    count = next((s.get("episode_count") or 0 for s in det.get("seasons") or [] if s["season_number"] == season), 0)
    return f"Episode {episode}" if not count or episode > count else f"S{season}E{episode}"



def row_list(cid, typ, lang, built=False):
    """A row as served. Mixed rows keep one ordered list ('tv', or movies and series interleaved; Watch Again also
    its AI half): type tv serves it whole, movie / series only that kind."""
    rows = ((_feeds.get((cid, lang)) or {}) if built else feed(cid, lang)).get("metas") or {}
    if cid not in MIXED:
        return rows.get(typ, [])
    if "tv" in rows:
        full = rows["tv"]
    else:
        lists = [rows.get("movie", []), rows.get("series", [])]
        if cid == rid('rewatch'):
            ai = (_feeds.get((rid('rewatchai'), lang)) or {}).get("metas") or {}
            sagas = set(rows.get("sagas") or [])
            lists = [interleave(lists[0], lists[1]),
                     [m for m in interleave(ai.get("movie", []), ai.get("series", [])) if m["id"] not in sagas]]
        full = interleave(*lists)
    return (full if typ == "tv" else [m for m in full if m["type"] == typ])[:ROW_MAX]


def because_seed(p):
    seeds = top_seeds(p, 8) if p else []
    if not seeds:
        return None
    rng = random.Random(f"{day_index()}:because")
    return rng.choices(seeds, weights=[max(.1, e["weight"]) / (1 + e["age"] / 90) for e in seeds])[0]


def soon_date(meta):
    f = row_facts(meta)
    if f.get("when"):
        return date_or_none(f["when"])
    notes, _ = card_parts(meta)
    text = " ".join(notes)
    match = re.search(r"\b(\d{1,2}) (" + "|".join(MONTHS) + r")(?: (20\d{2}))?\b", text)
    if match:
        try:
            return datetime.date(int(match[3] or local_now().year), MONTHS.index(match[2]) + 1, int(match[1]))
        except ValueError:
            return None
    match = re.search(r"\b(20\d{2})\b", text)
    if match:
        month = re.search(r"\b(" + "|".join(MONTHS) + r") (20\d{2})\b", text)
        return datetime.date(int(month[2]), MONTHS.index(month[1]) + 1, 1) if month else datetime.date(int(match[1]), 12, 31)
    return date_or_none(f.get("date"))


def upcoming_ok(meta):
    when = soon_date(meta)
    if not when:
        return False
    days = (when - local_now().date()).days
    _, overview = card_parts(meta)
    return -120 <= days <= 365 and not re.search(r"\b(TBA|to be announced)\b", meta.get("description") or "", re.I) \
        and (days <= 180 or (meta.get("background") and overview.strip()))


_row_kinds = {"profile": None, "keys": {}}


def profile_kinds(p):
    if _row_kinds["profile"] is not p:
        keys = {}
        for e in p.get("entries", []):
            seen, negative = keys.setdefault(e["key"], (set(), set()))
            kind = e.get("kind") or ("movie" if e.get("stype") == "movie" else "tv" if e.get("stype") == "series" else None)
            if kind:
                seen.add(kind)
                if e.get("weight", 0) < 0:
                    negative.add(kind)
        _row_kinds.update(profile=p, keys=keys)
    return _row_kinds["keys"]


def profile_kind(key):
    kinds = profile_kinds(_profiles.get("household") or {}).get(key, ((), ()))[0]
    return next(iter(kinds)) if len(kinds) == 1 else None


def row_member(meta, p, field):
    if meta["id"] not in p.get(field, ()):
        return False
    kinds = profile_kinds(p).get(meta["id"])
    return kinds is None or not kinds[0] or ("movie" if meta["type"] == "movie" else "tv") in kinds[field == "negative"]


def row_animation(cid):
    return cid == rid('leap') or cid in ROW_OWNER and not any(
        "Animation" in frec(k, profile_kind(k)).get("g", ()) for k in explicit_seeds(ROW_OWNER[cid]))


def row_eligible(meta, cid, lang, p, animation=None):
    viewer, kind = _viewer.get(), "movie" if meta["type"] == "movie" else "tv"
    if viewer in PEOPLE and "accounts" in CONFIG["connectors"].get("trakt_api", {}):
        known = any(e["key"] == meta["id"] and e["kind"] == kind for e in p.get("entries", []))
        if known and not any(e["key"] == meta["id"] and e["kind"] == kind for e in person_entries(p, viewer)):
            return False
    if row_member(meta, p, "negative"):
        return False
    name = meta.get("name") or ""
    if not re.search(r"[A-Za-zÀ-ɏ]{2}", name) and re.search(r"[^\W\d_]", name):
        return False
    own = cid in (rid('episodes'), rid('catchup'), rid('rewatch'))
    if row_member(meta, p, "seen") and not own:
        if cid not in (rid('airing'), rid('nextseason'), rid('upcoming')) or meta["type"] != "series":
            return False
        entries = [e for e in p.get("entries", []) if e["key"] == meta["id"] and e.get("kind", "movie" if meta["type"] == "movie" else "tv") == ("movie" if meta["type"] == "movie" else "tv")]
        if not entries or row_facts(meta).get("status") in ("Ended", "Canceled") or any(e.get("item", {}).get("completed") for e in entries):
            return False
    if cid == rid('upcoming'):
        return upcoming_ok(meta)
    if cid in DOCS:
        return doc_ok(meta, cid)
    curated = cid in {rid('foryou'), rid('week'), rid('because'), rid('leap')} | set(ROW_OWNER) | set(MOODS_ROWS + LANE_ROWS + STUDIO_ROWS)
    if curated:
        if animation is None:
            animation = row_animation(cid)
        if "Documentary" in row_facts(meta)["g"] or not quality_ok(meta) or not tone_ok(meta, row_seed(cid, lang) if cid == rid('leap') else None, animation=animation):
            return False
        d = soon_date(meta)
        if d and d > local_now().date():
            return False
    if cid == rid('because'):
        return because_ok(meta, row_seed(cid, lang))
    return cid != rid('top10') or quality_ok(meta)


def row_pool(lang):
    pool = {}
    for cid in DEDUPE_ORDER:
        # A lane's pinned bench must not change the refill supply of rows above it.
        built = cid in LANES and bool(feed(cid, lang).get("_bench"))
        for typ in (("movie", "series") if cid == rid('top10') else ("series",) if cid in (rid('episodes'), rid('airing'), rid('nextseason')) else ("tv",)):
            for x in row_list(cid, typ, lang, built=built):
                pool.setdefault(x["id"], x)
        for x in source_metas(cid, lang, built=built):
            pool.setdefault(x["id"], x)
    for xs in ((_feeds.get((rid('docsai'), lang)) or {}).get("metas") or {}).values():
        for x in xs:
            pool.setdefault(x["id"], x)
    return pool.values()


def row_related(meta, cid, lang, seeds=None):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    tags = rec.get("x") or {}
    names = feed(cid, lang).get("names") or {}
    if cid in (rid('foryou'), rid('week')) or cid in ROW_OWNER:
        if "Documentary" in row_facts(meta)["g"]:
            return False
        return week_ok(meta, seeds) if cid == rid('week') else cid == rid('foryou') or member_pick(meta, ROW_OWNER[cid])
    if cid == rid('because'):
        return because_ok(meta, row_seed(cid, lang))
    if cid == rid('leap'):
        seed = row_seed(cid, lang)
        xs = frec(seed, profile_kind(seed)).get("x") or {}
        shared = [x for x, v in xs.items() if v >= 2 and x != "coming_of_age"]
        return xsim(xs, tags) >= .35 and any(tags.get(x, 0) >= 2 for x in shared)
    if cid in MOODS_ROWS:
        mood = names.get("mood")
        return mood in MOODS and any(tags.get(x, 0) >= 2 for x in MOODS[mood][1])
    if cid in LANE_ROWS:
        return lane_member(meta, names.get("lane"))
    if cid in STUDIO_ROWS:
        filters = STUDIOS[cid][1].get("movie" if meta["type"] == "movie" else "tv") or {}
        return any(str(i) in str(filters.get(param, "")).split("|") for param, field in
                   (("with_networks", "n"), ("with_companies", "c")) for i, _ in rec.get(field) or [])
    if cid in DOCS:
        kws = set(DOC_ROWS[cid][3]) if cid in DOC_ROWS else {k for v in DOC_ROWS.values() for k in v[3]}
        return "Documentary" in row_facts(meta)["g"] and any(k in kws for k, _ in rec.get("k") or [])
    return False


def source_metas(cid, lang, built=False):
    source = _feeds.get((cid, lang)) or {}
    names = (source if built else feed(cid, lang)).get("names") or {}
    same = all(not names.get(k) or names[k] == (source.get("names") or {}).get(k) for k in ("seed", "lane", "mood"))
    return [x for typ, xs in (source.get("metas") or {}).items()
            if same and typ in ("movie", "series", "tv", "pool") for x in xs]


def lane_sibling(cid, lang):
    metas = row_list(cid, "tv", lang)
    worlds = {universe_key(x) for x in metas}
    if len(metas) < 12 or len(worlds) != 1 or None in worlds:
        return None
    def experiences(xs):
        tags = {}
        for x in xs:
            for tag, value in (frec(x["id"], "movie" if x["type"] == "movie" else "tv").get("x") or {}).items():
                tags[tag] = tags.get(tag, 0) + value
        return tags
    own = experiences(metas)
    candidates = [(xsim(own, experiences(row_list(other, "tv", lang))), other) for other in LANES if other != cid
                  and ((feed(other, lang).get("names") or {}).get("lane") or "").startswith("x:")]
    score, other = max(candidates, default=(0, None))
    return other if score >= .35 else None


def fill_row(metas, cid, lang, p, above, masks, tail=None):
    if cid not in MIXED | set(LANE_BENCH) | {rid('top10'), rid('episodes')} or cid in (rid('catchup'), rid('upcoming'), rid('rewatch')):
        return metas
    ids = {x["id"] for x in metas} | above | masks[0]
    own = source_metas(cid, lang)
    if cid in LANE_ROWS:
        own += (feed(cid, lang).get("metas") or {}).get("pool") or []
    own_ids = {x["id"] for x in own}
    animation = row_animation(cid)
    seeds = {person: week_seeds(person) for person in PEOPLE} if cid == rid('week') else None
    candidates = [x for x in own + list(row_pool(lang)) if x["id"] not in ids
                  and not row_member(x, p, "negative") and (cid == rid('episodes') or not row_member(x, p, "seen"))
                  and (cid == rid('week') or x["id"] in own_ids or cid in ROW_OWNER and not is_hero(x) or row_related(x, cid, lang))
                  and (cid not in LANE_ROWS or row_related(x, cid, lang))
                  and row_eligible(x, cid, lang, p, animation)]
    more = []
    for x in candidates:
        if x["id"] in ids:
            continue
        if cid == rid('week') and x["id"] not in own_ids and tail is not None:
            values = week_values(x, seeds)
            if values is None or any(v < WEEK_SEED_FLOOR for v in values):
                if values is not None and all(v >= WEEK_TAIL_FLOOR for v in values):
                    tail.append(x)
                continue
        elif cid == rid('week') and x["id"] not in own_ids and not week_ok(x, seeds):
            continue
        ids.add(x["id"])
        more.append(x)
    m = get_model(False)
    if cid != rid('top10'):
        more.sort(key=lambda x: (member_pick(x, ROW_OWNER[cid]) if cid in ROW_OWNER else False,
                                  score_key(m, x["id"], ROW_OWNER.get(cid), "movie" if x["type"] == "movie" else "tv")[0] or 0), reverse=True)
    if cid in ROW_OWNER:
        more = more[:ROW_MAX]  # R1 ranks a bounded refill after the animation guard thins her own pool.
    # Own refills keep their context; borrowed cards carry only their overview.
    more = [x if x["id"] in own_ids else {**x, "description": card_parts(x)[1]} for x in more]
    return metas + more


def week_seeds(person):
    p = _profiles.get("household") or {"entries": []}
    shared = [e["key"] for e in p["entries"] if "together" in e.get("by", ()) and e["weight"] > 0
              and e["signal"] in ("watched", "liked", "loved")]
    return list(dict.fromkeys(explicit_seeds(person) + shared))


def week_values(meta, seeds=None):
    m = get_model(False)
    if not m or not tone_ok(meta, animation=True) or (score_key(m, meta["id"], "together", "movie" if meta["type"] == "movie" else "tv")[0] or 0) <= 0:
        return None
    return [(seed_knn(seeds[person] if seeds is not None else week_seeds(person), meta["id"], 1) or 0)
            for person in PEOPLE if m["nexp"].get(person, 0) < 20]


def week_ok(meta, seeds=None):
    values = week_values(meta, seeds)
    return values is not None and all(v >= WEEK_SEED_FLOOR for v in values)


def week_tail_ok(meta, seeds=None):
    values = week_values(meta, seeds)
    return values is not None and all(v >= WEEK_TAIL_FLOOR for v in values) and any(v < WEEK_SEED_FLOOR for v in values)


def week_tail(metas, lang, p, above, masks, candidates=None):
    if len(metas) < 10:
        return []
    ids = {x["id"] for x in metas} | above | masks[0]
    if candidates is None:
        seeds = {person: week_seeds(person) for person in PEOPLE}
        candidates = [x for x in row_pool(lang) if x["id"] not in ids and row_eligible(x, rid('week'), lang, p)
                      and week_tail_ok(x, seeds)]
    # A weaker Week tail must not consume the two established personal shelves.
    protected = {x["id"] for cid in (rid('because'), *ROW_OWNER) for x in row_list(cid, "tv", lang)}
    protected.update(x["id"] for x in served_row(rid('because'), "tv", lang, p, ids, masks))
    candidates = [x for x in candidates if x["id"] not in ids | protected and not any(member_pick(x, person) for person in MEMBER_ROWS)]
    scores = week_scores(candidates)
    candidates.sort(key=lambda x: week_score(x, scores)[1], reverse=True)
    tail, heroes = [], int(sum(is_hero(x) for x in metas) >= 2)
    for x in candidates:
        if len(tail) >= min(6, 20-len(metas)):
            break
        if x["id"] in ids:
            continue
        if is_hero(x) and heroes:
            continue
        # Tail cards retain the same franchise and universe limits as the head.
        if sum(franchise_key(y) == franchise_key(x) for y in metas + tail) >= 2:
            continue
        if any(franchise_key(y) == franchise_key(x) and (row_facts(y).get("date") or "9999") >
               (row_facts(x).get("date") or "9999") for y in metas):
            continue
        universe = universe_key(x)
        if universe and sum(universe_key(y) == universe for y in metas + tail) >= 4:
            continue
        heroes += is_hero(x)
        ids.add(x["id"])
        tail.append({**x, "description": card_parts(x)[1]})
    return tail


class RowAbove(set):
    def __init__(self, ids):
        super().__init__(ids)
        self.deep = set(ids)
        self.lanes = set()


def mark_above(above, cid, metas):
    if not isinstance(above, RowAbove):
        above = RowAbove(above)
    above.deep.update(x["id"] for x in metas)
    above.update(x["id"] for x in (metas if cid in MOODS_ROWS + LANES else metas[:ROW_WINDOW]))
    if cid in LANES and isinstance(metas, LaneRow) and metas:
        above.lanes.add(metas.names.get("lane"))
    return above


def mood_heroes(metas):
    front, tail, heroes = [], [], 0
    for x in metas:
        if len(front) < 10 and is_hero(x) and heroes >= 3:
            tail.append(x)
        else:
            heroes += is_hero(x)
            front.append(x)
    return front + tail


def week_scores(metas):
    m = get_model(False)
    if not m:
        return {x["id"]: 0 for x in metas}
    house = {x["id"]: score_key(m, x["id"], kind="movie" if x["type"] == "movie" else "tv")[0] for x in metas}
    house_pct = pool_pct(house)
    people = []
    for person in PEOPLE:
        n = m["nexp"].get(person, 0)
        own = pool_pct({x["id"]: s if (s := score_key(m, x["id"], person, "movie" if x["type"] == "movie" else "tv")[0]) is not None
                        else house[x["id"]] for x in metas})
        seeds = explicit_seeds(person) if n < 20 else []
        fallback = pool_pct({x["id"]: seed_knn(seeds, x["id"], 1) for x in metas}) if seeds else house_pct
        lam = min(1, n/40)
        people.append({k: lam * own[k] + (1-lam) * fallback[k] for k in house})
    return {k: sum(p[k] for p in people)/len(people) - WEEK_PENALTY*(max(p[k] for p in people)-min(p[k] for p in people))
            if house[k] is not None else 0 for k in house}


def week_score(meta, scores):
    joint = scores.get(meta["id"], 0)
    date = date_or_none(row_facts(meta).get("date"))
    fresh = bool(date and 0 <= (local_now().date() - date).days <= 730 or
                 row_facts(meta).get("status") in ("Returning Series", "In Production"))
    return joint, joint + .08 * fresh


def memory_reserve(metas, who):
    facts = [f for f in memory_load()["facts"] if f["person"] == who and f["status"] == "active"
             and f["kind"] == "like" and f["src"] == "told" and f["strength"] >= .8]
    if not facts:
        return metas
    breadth = _memory_breadth()
    out, moved = list(metas), 0
    held = {x["id"]: {fid for fid, _ in facet_pairs(frec(x["id"], "movie" if x["type"] == "movie" else "tv"))} for x in metas}
    for f in facts:
        about = f["about"]
        if breadth.get(about, 0) > MEMORY_VETO_MAX_SHARE:
            continue
        need = 2 - sum(about in held[x["id"]] for x in out[:10])
        for x in out[10:]:
            if need <= 0 or moved >= 5:
                break
            if about in held[x["id"]] and not is_anime(x):
                out.remove(x)
                out.insert(5 + moved, x)
                moved, need = moved+1, need-1
    return out if moved else metas


def served_row(cid, typ, lang, p, above, masks, who=None):
    token = _viewer.set(who) if who is not None else None
    try:
        if len(masks) > 2 and masks[2] and cid not in VETO_EXEMPT: masks = (masks[0] | masks[2], masks[1])
        if cid not in MOODS_ROWS + LANE_ROWS:
            above = getattr(above, "deep", above)
        metas = row_list(cid, typ, lang)
        if cid == rid('rewatch') and metas:
            entries = p.get("entries", [])
            if entries and who in PEOPLE: entries = person_entries(p, who)
            watched = {(e["key"], e["kind"]) for e in entries
                       for value in ((e,) if who in PEOPLE else (e, *(e.get("opinions") or {}).values()))
                       if value["signal"] in ("loved", "liked", "watched") or value["item"].get("played")
                       or value["item"].get("completed") or value["item"].get("rating") is not None
                       or (value["item"].get("episodes_played") or 0) > 0 or (value["item"].get("progress") or 0) >= .7}
            metas = [x for x in metas if (x["id"], "movie" if x["type"] == "movie" else "tv") in watched]
        names = feed(cid, lang).get("names") or {}
        animation = row_animation(cid)
        if cid == rid('episodes') and len(metas) < 12:
            catchup = [x for x in row_list(rid('catchup'), "tv", lang) if x["type"] == "series"]
            catchup.sort(key=lambda x: not re.search(r"\bSeason\w* .* (?:is|are) out", x.get("description", "")))
            metas = interleave(metas, catchup)[:12]
        if cid == rid('foryou'):
            week = [x for x in row_list(rid('week'), "tv", lang) if x["id"] not in masks[0] and row_eligible(x, rid('week'), lang, p)]
            metas = [x for x in metas if x["id"] not in {w["id"] for w in week}]
        corrected = []
        for x in metas:
            if not re.search(r"[A-Za-zÀ-ɏ]{2}", x.get("name", "")):
                en = (x.get("_row") or {}).get("english") or frec(x["id"], "movie" if x["type"] == "movie" else "tv").get("title")
                if en and re.search(r"[A-Za-z]{2}", en):
                    x = {**x, "name": en}
                elif x.get("name", "").isdigit() and p:
                    x = {**x, "name": x["name"] + (" (Movie)" if x["type"] == "movie" else " (Series)")}
            if row_member(x, p, "seen") and cid in (rid('airing'), rid('nextseason'), rid('upcoming')):
                notes, overview = card_parts(x)
                note = next((n for n in notes if n not in ("Strong fit", "Good fit", "Wildcard")), "")
                x = {**x, "description": " · ".join(s for s in ("New season: " + note, overview) if s)}
            if cid in (rid('catchup'), rid('episodes')):
                counts = (x.get("_row") or {}).get("counts") or {}
                x = {**x, "description": re.sub(r"S(\d+)E(\d+)", lambda m: m[0] if int(m[2]) <= counts.get(m[1], 0)
                                                else f"Episode {m[2]}", x.get("description") or "")}
            if x["id"] not in masks[0] and row_eligible(x, cid, lang, p, animation) and (cid not in LANE_ROWS or row_related(x, cid, lang)):
                corrected.append(x)
        # Chart ranks survive; all other shelves use the actual served rows above.
        metas = corrected if cid == rid('top10') else [x for x in corrected if x["id"] not in above]
        tail_candidates = []
        metas = fill_row(metas, cid, lang, p, above, masks, tail_candidates) if cid == rid('week') else fill_row(metas, cid, lang, p, above, masks)
        if cid not in MIXED | set(LANE_BENCH):
            metas = [x for x in metas if x["type"] == typ]
        if cid == rid('foryou'):
            reserved = {x["id"] for x in row_list(rid('week'), "tv", lang)}
            metas = [x for x in metas if x["id"] not in reserved]
            metas.sort(key=lambda x: score_key(get_model(False), x["id"], kind="movie" if x["type"] == "movie" else "tv")[0] or 0, reverse=True)
        if cid == rid('because'):
            metas = because_rank(metas, row_seed(cid, lang))
        if cid in ROW_OWNER:
            scores = member_scores(metas, ROW_OWNER[cid])
            metas.sort(key=lambda x: scores[x["id"]], reverse=True)
        if cid == rid('week'):
            curated = {x["id"] for x in row_list(cid, "tv", lang)}
            scores = week_scores(metas)
            metas.sort(key=lambda x: (x["id"] in curated, week_score(x, scores)[1]), reverse=True)
        if who in PEOPLE and cid in PERSON_ROWS | set(LANE_BENCH): metas = person_order(metas, who)
        if cid in PERSON_ROWS:
            annotated, n = [], 0
            for x in metas:
                why = memory_why(x["id"], "movie" if x["type"] == "movie" else "tv", "together" if who == "both" else who) if n < MEMORY_WHY_ROW else None
                if why:
                    x, n = {**x, "_memory": why}, n+1
                annotated.append(x)
            metas = annotated
        title = names.get("title", "").casefold()
        if cid in LANE_ROWS and not lane_allowed(metas, title):
            metas = []
        anime = cid in (rid('airing'), rid('nextseason')) or any(w in title for w in ("anime", "manga"))
        metas = balance_row(metas, foryou=cid == rid('foryou'), anime=anime)
        lid = f"lane:{names['lane']}" if cid in LANE_ROWS and names.get("lane") else None if cid in LANE_ROWS else learn_id(cid, lang)
        tired = tired_keys({lid}) if lid and cid not in (rid('episodes'), rid('upcoming'), rid('catchup')) else {}
        metas = [x for x in metas if x["id"] not in tired] + [x for x in metas if x["id"] in tired]
        # A universe is the point of some lanes; keep the franchise cap within it.
        metas = polish_row(metas, anime=anime, universe_cap=None) if cid in LANE_ROWS else polish_row(
            metas, cap=1 if cid == rid('upcoming') else 2, anime=anime)
        if len(metas) < 5:
            metas = balance_row(metas, anime=anime)
        if cid == rid('foryou'):
            metas = polish_row(balance_row(metas, foryou=True))
        if cid == rid('because'):
            metas = polish_row(because_balance(metas, row_seed(cid, lang)))
        if cid in ROW_OWNER:
            metas = member_balance(metas, ROW_OWNER[cid], strict=True)
        if who in PEOPLE and cid == rid('foryou'):
            metas = person_animation_sink(memory_reserve(metas, who), who)
        if cid in MOODS_ROWS and len(metas) < ROW_MIN:
            return []
        limit = 10 if cid == rid('top10') else 12 if cid == rid('episodes') else 24 if cid == rid('foryou') else 20
        metas = metas[:limit]
        if cid in MOODS_ROWS + LANE_ROWS:
            if not anime:
                cap = int(sum(not is_anime(x) for x in metas) * 2 / 3)
                allowed = {x["id"] for x in [x for x in metas if is_anime(x)][:cap]}
                metas = [x for x in metas if not is_anime(x) or x["id"] in allowed]
            if len(metas) < ROW_MIN and cid not in LANE_ROWS:
                return []
        if cid in LANE_ROWS and not lane_allowed(metas, title):
            metas = []
        if cid in LANE_ROWS and len(metas) < ROW_MIN:
            return LaneRow([], names)
        if cid in MOODS_ROWS:
            metas = mood_heroes(metas)
        if cid == rid('week'):
            tail = week_tail(metas, lang, p, above, masks, tail_candidates)
            head_ids = {x["id"] for x in metas}
            rotated = rotate_row(metas + tail, cid)
            # Franchise ordering during rotation must not pull a tail original into the head.
            return remote_order([x for x in rotated if x["id"] in head_ids], masks) + remote_order(
                [x for x in rotated if x["id"] not in head_ids], masks)
        else:
            metas = rotate_row(metas, cid)
        metas = remote_order(metas, masks)
        metas = person_animation_sink(metas, who) if who in PEOPLE and cid in PERSON_ROWS | set(LANE_BENCH) else metas
        return LaneRow(metas, names) if cid in LANE_ROWS else metas
    finally:
        if token is not None: _viewer.reset(token)


def _row_metas_uncached(cid, typ, lang, who=None):
    """Reference composition for non-home rows and cache identity checks."""
    who = who_watching()[0] if who is None else who
    masks = (*remote_masks(), veto_keys(who))
    p = _profiles.get("household") or {}
    above = set()
    for other in DEDUPE_ORDER:
        types = ("movie", "series") if other == rid('top10') else ("tv",) if other in MIXED else ("series",)
        for ot in types:
            metas = served_row(other, ot, lang, p, above, masks, who=who)
            if other == cid and (ot == typ or other in MIXED):
                selected = metas if typ == "tv" or other not in MIXED else [x for x in metas if x["type"] == typ]
                return human_cards(LaneRow(selected, metas.names) if isinstance(metas, LaneRow) else selected)
            above = mark_above(above, other, metas)
    return human_cards(served_row(cid, typ, lang, p, above, masks, who=who))


_HOME_CACHE_ENABLED = True
_home_cache = {}
_home_lock = threading.Lock()


def build_home(lang, p, masks, who):
    above, rows = set(), {}
    for cid in DEDUPE_ORDER:
        types = ("movie", "series") if cid == rid('top10') else ("tv",) if cid in MIXED else ("series",)
        for typ in types:
            metas = served_row(cid, typ, lang, p, above, masks, who=who)
            for out_type in MIX if cid in MIXED else (typ,):
                selected = metas if out_type == typ else [x for x in metas if x["type"] == out_type]
                rows[cid, out_type] = human_cards(LaneRow(selected, metas.names) if isinstance(metas, LaneRow) else selected)
            above = mark_above(above, cid, metas)
    return rows


def row_metas(cid, typ, lang, who=None):
    if not _HOME_CACHE_ENABLED or cid not in DEDUPE_ORDER or typ not in (MIX if cid in MIXED else ("movie", "series") if cid == rid('top10') else ("series",)):
        if who is None:
            return _row_metas_uncached(cid, typ, lang)
        return _row_metas_uncached(cid, typ, lang, who=who)
    with _home_lock:
        who = who_watching()[0] if who is None else who
        masks = (*remote_masks(), veto_keys(who))
        p, m = _profiles.get("household"), get_model(False)
        scope = getattr(_scoping, "scope", None)
        shown = _shown if scope is None else _shown_scoped[scope]
        with _feeds_lock:
            generation = tuple((source, k, id(v)) for source, store in enumerate((_feeds, shown))
                               for k, v in sorted(store.items()) if k[1] == lang)
            refs = [v for store in (_feeds, shown) for k, v in store.items() if k[1] == lang]
        key = (lang, who, scope, tuple(frozenset(s) for s in masks), id(m), generation,
               p.get("hash") if p else None, id(p), int(time.time() // 60))
        if key not in _home_cache:
            rows = build_home(lang, p or {}, masks, who)
            if len(_home_cache) >= 8:
                _home_cache.pop(next(iter(_home_cache)))
            # Retain source identities so replacements cannot reuse an older cache key.
            _home_cache[key] = (rows, [p, m, *refs])
        return _home_cache[key][0][cid, typ]


def stats():
    with _usage_lock:
        _usage_roll()
        u = json.loads(json.dumps(_usage))
    u["caps"] = {**AI_CAPS, "monthly_usd": AI_MONTHLY_USD}
    u["rows"] = {f"{cid}|{lang}": {"built": v.get("built"), "stamp": v.get("stamp"),
                                   "count": {k: len(x) for k, x in (v.get("metas") or {}).items()}}
                 for (cid, lang), v in sorted(_feeds.items())}
    u["profiles"] = {k: {"titles": len(v["entries"]), "hash": v.get("hash"),
                         "signals": {s: sum(e["signal"] == s for e in v["entries"]) for s in SIGNAL_WEIGHT}}
                     for k, v in _profiles.items()}
    u["decide"] = decide_stats()
    u["daily"] = daily_stats()
    u["learn"] = learn_stats()
    u["fingerprints"] = len(_emb)
    u["facets"] = len(_facets)
    m = _model["m"]
    if m:
        def top(aff, types, n=8):
            return [f"{m['names'].get(f)} {a:.2f}" for a, f in sorted(((a, f) for f, a in aff.items() if f[0] in types), reverse=True)[:n]]
        u["taste"] = {"facets": len(m["aff"]), "background": m["cal"], "line": m["taste_line"],
                      "experiences": top(m["aff"], "x", 10), "networks": top(m["aff"], "nc", 6), "creators": top(m["aff"], "d", 6),
                      "people": {pp: {"experiences": top(a, "x", 8), "themes": top(a, "kt", 6)} for pp, a in m["paff"].items()}}
        with _facets_lock:
            u["tagged"] = sum("x" in r for r in _facets.values())
    u["lanes"] = _learn.get("lanes") or {}
    u["nzb"] = nzb_stats()
    u["trakt_titles"] = len(read_trakt())
    u["who"] = who_stats()
    return u


# --- onboarding imports ---
IMPORT_SIGNALS = {"love": ("loved", 3.0), "like": ("liked", 2.0), "seen": ("watched", 1.0),
                  "not_for_us": ("disliked", -3.0), "interest": ("interest", .2),
                  "abandon": ("abandoned", -1.5), "meh": ("watched", .3), "not_tonight": ("interest", 0.0)}
IMPORT_TRUST = {"trakt": 1.0, "letterboxd": 1.0, "imdb": 1.0, "netflix": .8,
                "chrome": .4, "youtube": .4, "plain": .8}
IMPORT_MAX = 20000
IMPORT_STRENGTH = {"opened": 0, "interest": 1, "watched": 2, "liked": 3, "loved": 4, "abandoned": 4, "disliked": 5}
_imports_lock = threading.Lock()
_imports_cache = {}


def validate_import(body):
    fields = {"t", "id", "key", "kind", "title", "year", "who", "action", "src", "conf", "n"}
    required = fields - {"t", "id", "year", "n"}
    if not isinstance(body, dict) or set(body) - fields or not required <= set(body):
        raise ValueError("invalid import fields")
    e = {"year": "", "n": 1, **body}
    if not isinstance(e["key"], str) or not re.fullmatch(r"tmdb:[0-9]{1,9}", e["key"]) \
            or e["key"] == "tmdb:0":
        raise ValueError("invalid import key")
    for field, allowed in (("kind", ("movie", "tv")), ("who", (*PEOPLE, "both")),
                           ("action", IMPORT_SIGNALS), ("src", IMPORT_TRUST)):
        if not isinstance(e[field], str) or e[field] not in allowed:
            raise ValueError("unknown person" if field == "who" else "invalid import " + field)
    if not isinstance(e["title"], str) or not e["title"].strip() or len(e["title"]) > 300 \
            or any(ord(c) < 32 for c in e["title"]) or "://" in e["title"]:
        raise ValueError("invalid import title")
    if not isinstance(e["year"], str) or not re.fullmatch(r"(?:[0-9]{4})?", e["year"]):
        raise ValueError("invalid import year")
    if type(e["conf"]) not in (int, float) or not math.isfinite(e["conf"]) or not 0 <= e["conf"] <= 1 \
            or type(e["n"]) is not int or not 1 <= e["n"] <= 100000:
        raise ValueError("invalid import confidence/count")
    if "id" in e and (not isinstance(e["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", e["id"])):
        raise ValueError("invalid import id")
    if "t" in e:
        if not isinstance(e["t"], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", e["t"]):
            raise ValueError("invalid import day")
        datetime.date.fromisoformat(e["t"])
    return e


def read_imports():
    events = []
    try:
        with open(os.path.join(DATA, "imports.jsonl"), "rb") as fh:
            while len(events) < IMPORT_MAX:
                line = fh.readline(4097)
                if not line:
                    break
                if len(line) > 4096:
                    while line and not line.endswith(b"\n"):
                        line = fh.readline(4097)
                    continue
                try:
                    e = validate_import(json.loads(line))
                    if {"id", "t"} <= set(e):
                        events.append(e)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    continue
    except OSError:
        pass
    return events


def import_state():
    path = os.path.join(DATA, "imports.jsonl")
    with _imports_lock:
        try:
            st = os.stat(path)
            stamp = (path, st.st_mtime_ns, st.st_size)
            if _imports_cache.get("stamp") != stamp:
                _imports_cache.update(stamp=stamp, state={"active": active_imports(read_imports())})
            return _imports_cache["state"]
        except OSError:
            _imports_cache.clear()
            return None


def active_imports(events=None):
    if events is None:
        return (import_state() or {}).get("active", [])
    latest = {}
    for e in events:
        latest[(e["key"], e["who"], e["src"])] = e
    return list(latest.values())


def save_imports(events):
    path = os.path.join(DATA, "imports.jsonl")
    tmp = path + ".tmp"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_imports(bodies, keep_ids=False):
    incoming = [validate_import(e) for e in bodies]  # validate the whole batch before writing.
    if keep_ids:
        with _imports_lock:
            events = read_imports()
            ids = {e["id"]: e for e in events}
            latest = {(e["key"], e["who"], e["src"]): e for e in active_imports(events)}
            added, duplicates, conflicts = [], 0, []
            for i, e in enumerate(incoming):
                old = ids.get(e.get("id"))
                e = {"t": old["t"] if old else datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
                     "id": uuid.uuid4().hex, **e}
                if old is not None:
                    if old == e:
                        duplicates += 1
                    else:
                        conflicts.append(i)
                    continue
                key = (e["key"], e["who"], e["src"])
                old = latest.get(key)
                if old and all(e[k] == old[k] for k in ("action", "conf", "n", "title", "year", "kind")):
                    duplicates += 1
                    continue
                ids[e["id"]] = latest[key] = e
                added.append(e)
            if len(events) + len(added) > IMPORT_MAX:
                raise ValueError("imports limit")
            if added:
                save_imports(events + added)
            return added, duplicates, conflicts
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    incoming = [{"t": today, **e} for e in incoming]
    batch = {}
    for e in incoming:
        key = (e["key"], e["who"], e["src"])
        old = batch.get(key)
        if old:
            newest = min((old, e), key=lambda item: _claim_rank(item, 0, newest=True))
            e = {**newest, "n": max(old["n"], e["n"])}
        batch[key] = e
    with _imports_lock:
        events = read_imports()
        latest = {(e["key"], e["who"], e["src"]): e for e in active_imports(events)}
        added = []
        for e in batch.values():
            e = {**e, "id": uuid.uuid4().hex}
            key = (e["key"], e["who"], e["src"])
            old = latest.get(key)
            if old and all(e[k] == old[k] for k in ("action", "conf", "n", "title", "year", "kind")):
                continue
            latest[key] = e
            added.append(e)
        if len(events) + len(added) > IMPORT_MAX:
            raise ValueError("imports limit is 20000 events; undo old imports first")
        if added:
            save_imports(events + added)
        return added


def undo_import(event_id):
    with _imports_lock:
        events = read_imports()
        target = next((e for e in events if e["id"] == event_id), None)
        if target is None:
            raise ValueError("unknown import id")
        save_imports([e for e in events if e["id"] != event_id])
        return target


def import_entries(now, events=None):
    return list(_import_event_entries(now, import_events(now, events)))


def merge_imports(entries, by_key, now, events=None):
    try:
        _merge_import_events(entries, by_key, now, import_events(now, events))
        return _claim_records.entries.copy()  # Explicit handoff to this fold's remote stage.
    finally:
        _claim_records.entries = {}


def import_hash(now):
    state = import_state()
    if state is None:
        return None
    day = int(now // DAY)
    if state.get("day") == day:
        return state["hash"]
    entries = []
    merge_imports(entries, {}, now, state["active"])
    h = hashlib.sha256()
    for row in sorted((e["key"], person, e["weight"]) for e in entries for person in e["by"]):
        h.update(json.dumps(row).encode())
    state.update(day=day, hash=h.hexdigest())
    return state["hash"]


# --- who's watching ---
SESSION_HOURS = 4
PERSON_ROWS = {rid('foryou')} | set(MOODS_ROWS) | set(LANES)
SESSION_ROWS = set(DEDUPE_ORDER[next(i for i, cid in enumerate(DEDUPE_ORDER) if cid in PERSON_ROWS):]) - {rid('top10')}
PERSON_BLEND = .5
PERSON_FLOOR = .5
PERSON_NEXP = 20
ANIM_SINK_MIN = 2
_sessions_lock = threading.RLock()
_sessions_cache = {}
_person_scores = {}
_person_lock = threading.Lock()


def read_sessions():
    events, lines, corrupt = [], 0, False
    try:
        with open(os.path.join(DATA, "sessions.jsonl"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines += 1
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict) or set(e) != {"t", "id", "who", "via", "fix"} \
                            or type(e["t"]) not in (int, float) or not math.isfinite(e["t"]) or e["t"] < 0 \
                            or not isinstance(e["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", e["id"]) \
                            or not who_ok(e["who"], "both", "off", GUEST) or e["via"] not in ("phone", "api") \
                            or (e["fix"] is not None and (e["who"] == "off" or not isinstance(e["fix"], str)
                                or not re.fullmatch(r"[0-9a-f]{32}", e["fix"]))):
                        raise ValueError("invalid session")
                    events.append(e)
                except (ValueError, TypeError, RecursionError):
                    corrupt = True
    except FileNotFoundError:
        pass
    _sessions_cache.update(lines=lines, corrupt=corrupt)
    return events


def session_windows():
    path = os.path.join(DATA, "sessions.jsonl")
    with _sessions_lock:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            _sessions_cache.clear()
            return []
        stamp = (path, st.st_mtime_ns, st.st_size)
        if _sessions_cache.get("stamp") != stamp:
            events = read_sessions()
            fixes = {e["fix"]: e for e in events if e["fix"]}
            windows = []
            for e in events:
                if e["fix"]:
                    continue
                if windows:
                    windows[-1]["until"] = min(windows[-1]["until"], e["t"])
                if e["who"] != "off":
                    fix = fixes.get(e["id"])
                    windows.append({"id": e["id"], "who": fix["who"] if fix else e["who"], "t": e["t"],
                                    "until": e["t"] + SESSION_HOURS * 3600, "src": "fix" if fix else "tap"})
            _sessions_cache.update(stamp=stamp, events=events, windows=windows)
        return _sessions_cache["windows"]


class SessionConflict(Exception):
    pass


def validate_session(body, bound=None):
    if not isinstance(body, dict) or set(body) - {"who", "fix"} or "who" not in body:
        raise ValueError("invalid session fields")
    if bound and (body["who"] not in ((bound, "off") if bound == GUEST else (bound, "both", "off")) or "fix" in body):
        raise PermissionError("session person")
    if not who_ok(body["who"], "both", "off", GUEST):
        raise ValueError("unknown person")
    fix = body.get("fix")
    if "fix" in body and (body["who"] == "off" or not isinstance(fix, str)
                         or not any(w["id"] == fix for w in session_windows())):
        raise ValueError("invalid session fix")
    if bound:
        active = session_at(time.time())
        if active and active["who"] not in (bound, "both") or body["who"] == "off" and not active:
            raise SessionConflict("session person")
    return {"who": body["who"], "fix": fix}


def write_session(body, via, bound=None):
    if via not in ("phone", "api"):
        raise ValueError("invalid session source")
    with _sessions_lock:
        e = validate_session(body, bound)
        events = read_sessions()
        e = {"t": time.time(), "id": uuid.uuid4().hex, **e, "via": via}
        events.append(e)
        path = os.path.join(DATA, "sessions.jsonl")
        rewrite = _sessions_cache["lines"] + 1 >= 2000 or _sessions_cache["corrupt"]
        if rewrite:
            events = events[-1000:]
            starts = {x["id"] for x in events if x["fix"] is None and x["who"] != "off"}
            events = [x for x in events if x["fix"] is None or x["fix"] in starts]
        with os.fdopen(os.open(path + ".tmp" if rewrite else path,
                              os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if rewrite else os.O_APPEND), 0o600), "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            for old in events if rewrite else [e]:
                fh.write(json.dumps(old) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if rewrite:
            os.replace(path + ".tmp", path)
        _sessions_cache.clear()
        return session_view(bound)


def session_at(t):
    return next((w for w in reversed(session_windows()) if w["t"] <= t < w["until"]), None)


def who_watching(t=None):
    w = session_at(time.time() if t is None else t)
    return (w["who"], w["src"]) if w else ("both", "prior")


def session_plays(w):
    return [e for e in (_profiles.get("household") or {}).get("entries", [])
            if e.get("who") in ("tv", "both") and real_play(e)
            and (at := (e.get("item") or {}).get("at")) is not None and w["t"] <= at / 1000 < w["until"]]


def session_view(bound=None):
    now = time.time()
    if bound is None and who_watching(now)[0] == GUEST:
        bound = GUEST
    windows = [w for w in session_windows() if not bound or w["who"] in ((GUEST,) if bound == GUEST else (bound, "both"))]
    def view(w):
        return {"id": w["id"], "who": w["who"], "since": w["t"], "until": w["until"], "src": w["src"]}
    active = next((view(w) for w in reversed(windows) if w["t"] <= now < w["until"]), None)
    if bound == GUEST:
        return {"active": active, "last": None}
    last = None
    for w in reversed(windows):
        if 0 <= now - w["t"] <= DAY and (plays := session_plays(w)):
            last = {**view(w), "plays": len(plays), "titles": [e["title"] for e in plays[:2]]}
            break
    return {"active": active, "last": last}


def attribute(it):
    if CONFIG["history"]["source"] in HISTORY_ADAPTERS and it.get("profile") in PEOPLE:
        return it["profile"], [it["profile"]], {}
    at = it.get("at")
    w = session_at(at / 1000) if at is not None else None
    if not w:
        return "together", ["together"], {}
    who = w["who"]
    if who == GUEST or not who_ok(who, "both"):
        return GUEST, [GUEST], {"src_who": w["src"]}
    by = [who] if who in PEOPLE else ["together"]
    return who if who in PEOPLE else "together", by, {"src_who": w["src"]}


def attrib_hash(items):
    values = [(it["id"], who, by) for it in items[:40] for who, by, _ in [attribute(it)] if by != ["together"] and who != GUEST]
    return hashlib.sha1(json.dumps(sorted(values)).encode()).hexdigest() if values else None


def person_alpha(m, who, seeds=None):
    if not m or who not in PEOPLE or not (explicit_seeds(who) if seeds is None else seeds):
        return 0
    return max(PERSON_FLOOR, PERSON_BLEND * min(1, m.get("nexp", {}).get(who, 0) / PERSON_NEXP))


def person_animation_sink(metas, who, seeds=None):
    m = get_model(False)
    if not m or m.get("nexp", {}).get(who, 0) >= 20:
        return metas
    if seeds is None:
        seeds = explicit_seeds(who)
    if seeds and sum("Animation" in frec(k, profile_kind(k)).get("g", ()) for k in seeds) < ANIM_SINK_MIN:
        return sorted(metas, key=lambda x: "Animation" in row_facts(x)["g"])
    return metas


def person_order(metas, who):
    m = get_model(False)
    seeds = explicit_seeds(who) if m and who in PEOPLE else []
    alpha = person_alpha(m, who, seeds)
    if not alpha or len(metas) < 2:
        return metas
    with _person_lock:
        if _person_scores.get("model") is not m:
            _person_scores.clear()
            _person_scores["model"] = m
        scores = _person_scores.setdefault(who, {})
        n = m.get("nexp", {}).get(who, 0)
        knn = _person_scores.setdefault((who, "knn"), {})
        values = []
        for x in metas:
            key = (x["id"], "movie" if x["type"] == "movie" else "tv")
            if key not in scores:
                scores[key] = score_key(m, key[0], who, key[1])[0]
            if n < 20 and key not in knn:
                knn[key] = seed_knn(seeds, key[0], 1)
            values.append(scores[key])
        if n < 20:
            taste = pool_pct({x["id"]: v for x, v in zip(metas, values)})
            seed = pool_pct({x["id"]: knn[x["id"], "movie" if x["type"] == "movie" else "tv"] for x in metas})
            w = member_weight(n)
            values = [w * seed[x["id"]] + (1-w) * taste[x["id"]] for x in metas]
    order = sorted(range(len(metas)), key=lambda i: (values[i] is None, -(values[i] or 0), i))
    ranks = {old: rank for rank, old in enumerate(order)}
    out = [metas[i] for i in sorted(range(len(metas)), key=lambda i: ((1-alpha)*i/(len(metas)-1)
                        + alpha*ranks[i]/(len(metas)-1), i))]
    return person_animation_sink(out, who, seeds)


def who_stats():
    now = time.time()
    windows = session_windows()
    active = session_at(now)
    counts = {who: sum(len(session_plays(w)) for w in windows if w["who"] == who) for who in (*PEOPLE, "both")}
    return {"active": int(active is not None), "minutes_left": math.ceil((active["until"]-now)/60) if active else 0,
            "windows": len(windows), "attributed": counts, "labelled": sum(counts.values())}


# --- taste remote ---
# shortcut: snapshot lacks strength-3 nostalgia anchors; fill coverage after facet refresh.
ANCHORS = (
    ('tmdb:3476', 'tv', 'cozy_case', 'proxy'),
    ('tmdb:1167', 'tv', 'cozy_case', 'proxy'),
    ('tmdb:94664', 'tv', 'second_chance', 'proxy'),
    ('tmdb:1567605', 'movie', 'second_chance', 'proxy'),
    ('tmdb:597', 'movie', 'forbidden_love', 'proxy'),
    ('tmdb:277', 'movie', 'forbidden_love', 'proxy'),
    ('tmdb:64684', 'tv', 'whodunit', 'proxy'),
    ('tmdb:157372', 'tv', 'whodunit', 'proxy'),
    ('tmdb:114466', 'tv', 'heartwarming', 'proxy'),
    ('tmdb:1184918', 'movie', 'heartwarming', 'proxy'),
    ('tmdb:13643', 'tv', 'slow_romance', 'proxy'),
    ('tmdb:313068', 'tv', 'slow_romance', 'proxy'),
    ('tmdb:11443', 'movie', 'heist', 'proxy'),
    ('tmdb:298', 'movie', 'heist', 'proxy'),
    ('tmdb:1366', 'movie', 'sports_glory', 'proxy'),
    ('tmdb:921', 'movie', 'sports_glory', 'proxy'),
    ('tmdb:258994', 'tv', 'will_they', 'proxy'),
    ('tmdb:3078', 'movie', 'will_they', 'proxy'),
    ('tmdb:116799', 'tv', 'courtroom', 'proxy'),
    ('tmdb:66276', 'tv', 'courtroom', 'proxy'),
    ('tmdb:92830', 'tv', 'hero_burden', 'proxy'),
    ('tmdb:320288', 'movie', 'hero_burden', 'proxy'),
    ('tmdb:489', 'movie', 'mentor_bond', 'proxy'),
    ('tmdb:9475', 'movie', 'mentor_bond', 'proxy'),
    ('tmdb:80537', 'tv', 'rivalry_training', 'proxy'),
    ('tmdb:27845', 'tv', 'rivalry_training', 'proxy'),
    ('tmdb:497', 'movie', 'tearjerker', 'proxy'),
    ('tmdb:158', 'movie', 'tearjerker', 'proxy'),
    ('tmdb:1419', 'tv', 'banter_duo', 'proxy'),
    ('tmdb:11688', 'movie', 'banter_duo', 'proxy'),
    ('tmdb:70453', 'tv', 'family_secrets', 'proxy'),
    ('tmdb:46738', 'movie', 'family_secrets', 'proxy'),
    ('tmdb:283938', 'tv', 'slice_of_life', 'proxy'),
    ('tmdb:82728', 'tv', 'slice_of_life', 'proxy'),
    ('tmdb:87108', 'tv', 'true_story', 'proxy'),
    ('tmdb:1317149', 'movie', 'true_story', 'proxy'),
    ('tmdb:75214', 'tv', 'grief_healing', 'proxy'),
    ('tmdb:7980', 'movie', 'grief_healing', 'proxy'),
    ('tmdb:67676', 'tv', 'feel_good', 'proxy'),
    ('tmdb:9602', 'movie', 'feel_good', 'proxy'),
    ('tmdb:73586', 'tv', 'frontier_grit', 'proxy'),
    ('tmdb:157744', 'tv', 'frontier_grit', 'proxy'),
    ('tmdb:365222', 'movie', 'competence', 'proxy'),
    ('tmdb:228305', 'tv', 'competence', 'proxy'),
    ('tmdb:125988', 'tv', 'dystopia', 'proxy'),
    ('tmdb:1300968', 'movie', 'dystopia', 'proxy'),
    ('tmdb:4421', 'movie', 'underdog', 'proxy'),
    ('tmdb:270603', 'tv', 'underdog', 'proxy'),
    ('tmdb:244786', 'movie', 'passion_art', 'proxy'),
    ('tmdb:998846', 'movie', 'passion_art', 'proxy'),
    ('tmdb:1541', 'movie', 'found_family', 'proxy'),
    ('tmdb:67466', 'tv', 'found_family', 'proxy'),
    ('tmdb:65494', 'tv', 'dynasty_power', 'proxy'),
    ('tmdb:76331', 'tv', 'dynasty_power', 'proxy'),
    ('tmdb:62084', 'tv', 'period_society', 'proxy'),
    ('tmdb:81723', 'tv', 'period_society', 'proxy'),
    ('tmdb:225180', 'tv', 'revenge', 'proxy'),
    ('tmdb:43016', 'tv', 'revenge', 'proxy'),
    ('tmdb:70523', 'tv', 'time_twist', 'proxy'),
    ('tmdb:42509', 'tv', 'time_twist', 'proxy'),
    ('tmdb:94028', 'tv', 'antihero', 'proxy'),
    ('tmdb:475557', 'movie', 'antihero', 'proxy'),
    ('tmdb:9343', 'tv', 'mind_bender', 'proxy'),
    ('tmdb:264660', 'movie', 'mind_bender', 'proxy'),
    ('tmdb:110492', 'tv', 'dark_comedy', 'proxy'),
    ('tmdb:9275', 'movie', 'dark_comedy', 'proxy'),
    ('tmdb:13674', 'tv', 'political_intrigue', 'proxy'),
    ('tmdb:64353', 'tv', 'political_intrigue', 'proxy'),
    ('tmdb:207347', 'tv', 'coming_of_age', 'proxy'),
    ('tmdb:10534', 'movie', 'coming_of_age', 'proxy'),
    ('tmdb:21731', 'tv', 'war_sacrifice', 'proxy'),
    ('tmdb:855', 'movie', 'war_sacrifice', 'proxy'),
    ('tmdb:63247', 'tv', 'big_ideas', 'proxy'),
    ('tmdb:687163', 'movie', 'big_ideas', 'proxy'),
    ('tmdb:136342', 'tv', 'magic_wonder', 'proxy'),
    ('tmdb:8204', 'movie', 'magic_wonder', 'proxy'),
    ('tmdb:1576', 'movie', 'survival', 'proxy'),
    ('tmdb:6278', 'movie', 'survival', 'proxy'),
    ('tmdb:49529', 'movie', 'epic_quest', 'proxy'),
    ('tmdb:85', 'movie', 'epic_quest', 'proxy'),
    ('tmdb:1212763', 'movie', 'dread', 'proxy'),
    ('tmdb:1339713', 'movie', 'dread', 'proxy'),
    ('tmdb:893723', 'movie', 'team_spectacle', 'proxy'),
    ('tmdb:575264', 'movie', 'team_spectacle', 'proxy'),
    ('tmdb:94305', 'tv', 'survival', 'proxy'),
    ('tmdb:89456', 'tv', 'survival', 'proxy'),
)


def interview_rank(anchors, person=None, m=None):
    m = get_model(False) if m is None else m
    scores = {(key, kind): person_score(m, key, person, kind)[0] for key, kind, _, _ in anchors}
    pcts = pool_pct(scores)
    # Rashid et al. 2002 pop×entropy adapted to one household, without cross-user rating spread.
    def rank(a):
        key, kind, _, _ = a
        p = pcts[key, kind]
        entropy = -p*math.log2(p) - (1-p)*math.log2(1-p) if 0 < p < 1 else 0
        votes = frec(key, kind).get("vote_count")
        popularity = math.log1p(votes) if votes is not None else 1.0  # D5 neutral offline proxy.
        return -popularity*entropy, key, kind, a[2]
    return sorted(anchors, key=rank)


_interview_lock = threading.Lock()
_interview_cache = {}


def interview_skips():
    try:
        with open(os.path.join(DATA, "interview.json")) as fh:
            rows = json.load(fh)
        if not isinstance(rows, list) or any(not isinstance(e, dict) or set(e) != {"key", "kind", "who"}
                or not who_ok(e["who"]) or e["kind"] not in KIND or not isinstance(e["key"], str)
                or not re.fullmatch(r"tmdb:[0-9]{1,9}", e["key"]) for e in rows):
            raise ValueError("invalid interview skips")
        return rows[-500:]
    except FileNotFoundError:
        return []


def interview_next(person):
    if not who_ok(person):
        raise PermissionError("guest is read-only") if person == GUEST else ValueError("unknown person")
    m = get_model(False)
    stamps = []
    for name in ("feedback.jsonl", "interview.json"):
        path = os.path.join(DATA, name)
        try:
            st = os.stat(path)
            stamps.append((path, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            stamps.append((path, None, None))
    cached = _interview_cache.get(person)
    if cached and cached[0] is m and cached[1] == stamps:
        return cached[2]
    model = m
    p = _profiles.get("household") or {"entries": []}
    active = active_feedback()
    entries = [dict(e) for e in p["entries"]]
    merge_feedback(entries, {e["key"]: e for e in entries}, time.time(), active)
    mine = [e for e in person_entries({"entries": entries}, person) if e.get("explicit")]
    if m and any(e.get("row") == rid('interview') for (key, who), e in active.items() if who == person):
        # Preview the frozen facet scorer from cached metadata; the worker rebuilds the complete model.
        n = sum(e["weight"] > 0 for e in mine)
        df, n_all, _ = _seed_stats
        aff = affinities(person_entries({"entries": entries}, person), _facets, df, n_all, set(), loose=n < 40, person=True)[0]
        preview = {**m, "paff": {**m["paff"], person: aff}, "nexp": {**m["nexp"], person: n}}
        own = sorted(b for e in mine if e["weight"] > 0
                     if (b := facet_bonus(preview, frec(e["key"], e["kind"]), aff)[0]) > 0)
        m = {**preview, "pbscale": {**m.get("pbscale", {}), person: own[len(own)//2] if own else 1.0}}
    positives = sum(e["signal"] in ("loved", "liked") for e in mine)
    negatives = sum(e["action"] == "not_for_us" and bool(e["reasons"]) for (key, who), e in active.items() if who == person)
    member = maturity(m, person)
    done_reason = "ready" if member["state"] == "ready" else "answers" if positives >= 20 and negatives >= 5 else None
    done = done_reason is not None
    rated = {(e["key"], e["kind"]) for e in mine}
    rated |= {(key, e["kind"]) for (key, who), e in active.items() if who == person}
    rated |= {(e["key"], e["kind"]) for e in interview_skips() if e["who"] == person}
    eligible = []
    for key, kind, x, pop in interview_rank(() if done else ANCHORS, person if mine else None, m):
        if done or (key, kind) in rated:
            continue
        view = title_view(key, kind, person)
        if view is None:
            continue
        rec = frec(key, kind)
        eligible.append({"key": key, "kind": kind, "title": view["title"], "year": str(rec.get("y") or ""), "experience": x})
    out, experiences = [], set()
    for i in range(12):
        left = [a for a in eligible if a["experience"] not in experiences]
        if not left:
            break
        a = next((a for a in left if a["kind"] == KIND[i % 2]), left[0])
        out.append(a)
        experiences.add(a["experience"])
    result = {"v": 2, "person": person, "maturity": member,
            "progress": {"positives": positives, "need": 20, "negatives_with_reason": negatives, "need_neg": 5},
            "done": done, "done_reason": done_reason, "anchors": out}
    _interview_cache[person] = (model, stamps, result)
    return result


def interview_answer(body):
    if not isinstance(body, dict) or not who_ok(body.get("who")):
        raise PermissionError("guest is read-only") if isinstance(body, dict) and body.get("who") == GUEST else ValueError("unknown person")
    if body.get("action") != "skip":
        if body.get("action") not in ("love", "like", "meh", "not_for_us"):
            raise ValueError("interview action")
        return write_feedback({**body, "row": rid('interview')})
    if set(body) - {"key", "kind", "who", "action", "row", "title", "year", "reasons", "undo"} or body.get("reasons") or body.get("undo") is not None:
        raise ValueError("invalid skip")
    e = {k: body.get(k) for k in ("key", "kind", "who")}
    if not isinstance(e["key"], str) or not re.fullmatch(r"tmdb:[0-9]{1,9}", e["key"]) or e["kind"] not in KIND:
        raise ValueError("invalid skip")
    with _interview_lock:
        _decision_write_json(os.path.join(DATA, "interview.json"), (interview_skips() + [e])[-500:])
    return e


REMOTE_PORT = int(os.environ.get("REMOTE_PORT", "8081"))
REMOTE_ROWS = set(ROW_OWNER) | {rid('foryou'), rid('week'), rid('upcoming'), rid('because'), rid('leap'),
               rid('rewatch'), rid('interview')} | set(MOODS_ROWS) | set(LANES) | set(STUDIO_ROWS) | set(DOCS)
REMOTE_ACTIONS = {"love", "like", "meh", "not_for_us", "not_tonight", "seen"}
REMOTE_REASONS = ("too dark", "too slow", "too violent", "too silly", "not my genre", "seen enough of these")
REMOTE_CSP = ("default-src 'none'; img-src https://image.tmdb.org data:; style-src 'self'; "
              "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
_remote_lock = threading.Lock()
_remote_posts = deque()
_remote_cache = {}


def remote_token():
    path = os.path.join(DATA, "remote.json")
    with _remote_lock:
        try:
            with open(path) as fh:
                stored = json.load(fh)
            token = stored.get("token") if isinstance(stored, dict) else None
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError("invalid remote capability")
            return token
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"token": token}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            return token


def read_feedback():
    # Caller holds _remote_lock; an interrupted append must not break catalogs.
    events, lines, corrupt = [], 0, False
    try:
        with open(os.path.join(DATA, "feedback.jsonl"), "rb") as fh:
            for line in fh:
                lines += 1
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) \
                            or not re.fullmatch(r"[0-9a-f]{32}", raw["id"]) \
                            or type(raw.get("t")) not in (int, float) or not math.isfinite(raw["t"]) or raw["t"] < 0:
                        raise ValueError("invalid event")
                    body = validate_feedback({k: v for k, v in raw.items() if k not in ("id", "t")})
                    events.append({"id": raw["id"], "t": raw["t"], **body})
                except (ValueError, PermissionError, TypeError, OverflowError, RecursionError):
                    corrupt = True
    except FileNotFoundError:
        pass
    _remote_cache.update(lines=lines, corrupt=corrupt)
    return events


def active_feedback(events=None):
    if events is None:
        return feedback_state()["active"]
    return fold_feedback(events)[0]


def fold_feedback(events):
    undone = {e["undo"] for e in events if e.get("undo")}
    latest, tonight = {}, {}
    for e in events:
        if e.get("undo") or e["id"] in undone:
            continue
        for person in sorted(PEOPLE) if e["who"] == "both" else (e["who"],):
            target = tonight if e["action"] == "not_tonight" else latest
            target[(e["key"], person)] = e
    return latest, tonight


def feedback_state():
    path = os.path.join(DATA, "feedback.jsonl")
    with _remote_lock:
        try:
            st = os.stat(path)
            stamp = (path, st.st_mtime_ns, st.st_size)
            if _remote_cache.get("stamp") != stamp:
                active, tonight = fold_feedback(read_feedback())
                _remote_cache.update(stamp=stamp, state={"active": active, "tonight": tonight,
                    "blocked": {key for (key, _), e in active.items() if e["action"] == "not_for_us"}})
            return _remote_cache["state"]
        except OSError:
            _remote_cache.clear()
            return {"active": {}, "tonight": {}, "blocked": set()}


def feedback_weight(e, now):
    ev = _event(e["t"], e.get("id"), e.get("key"), e.get("kind"), e.get("title"), e.get("year"),
                e.get("who", "unknown"), [], e["action"], "interview" if e.get("row") == rid('interview') else "remote",
                {"row": e.get("row")})
    return event_weight(ev, now)


def feedback_entries(now, active=None):
    return _feedback_event_entries(now, remote_events(now, active))


def merge_feedback(entries, by_key, now, active=None, records=None):
    _claim_records.entries = {} if records is None else records
    try:
        _merge_feedback_events(entries, by_key, now, remote_events(now, active))
    finally:
        _claim_records.entries.clear()


def feedback_hash(now):
    state = feedback_state()
    active = state["active"]
    if not active:
        return None
    cached = state.get("hash")
    if cached and cached[0] <= now <= cached[1]:
        return cached[2]
    h = hashlib.sha256()
    until = math.inf
    for (key, person), e in sorted(active.items()):
        signal, weight, _ = feedback_weight(e, now)
        h.update(json.dumps((key, person, signal, weight)).encode())
        for days in ((90, 120) if e["action"] == "love" else (30, 120)):
            boundary = e["t"] + days * DAY
            if boundary >= now:
                until = min(until, boundary)
    value = h.hexdigest()
    state["hash"] = (now, until, value)
    return value


def feedback_reasons():
    out = {}
    for (key, _), e in active_feedback().items():
        if e["action"] == "not_for_us":
            out[key] = list(dict.fromkeys(out.get(key, []) + e["reasons"]))
    return out


def remote_masks():
    tonight = set()
    now = time.time()
    state = feedback_state()
    blocked = state["blocked"]
    for (key, _), e in state["tonight"].items():
        if now - e["t"] < 18 * 3600:
            tonight.add(key)
    return blocked, tonight


def remote_order(metas, masks):
    blocked, tonight = masks[:2]
    metas = [m for m in metas if m["id"] not in blocked]
    return [m for m in metas if m["id"] not in tonight] + [m for m in metas if m["id"] in tonight]


def validate_feedback(body):
    fields = {"key", "kind", "title", "year", "who", "action", "reasons", "row", "undo"}
    if not isinstance(body, dict) or set(body) - fields or not {"key", "kind", "title", "who", "action"} <= set(body):
        raise ValueError("invalid fields")
    if body["who"] == GUEST:
        raise PermissionError("guest is read-only")
    e = {"year": "", "reasons": [], "row": None, "undo": None, **body}
    if not isinstance(e["key"], str) or not re.fullmatch(r"tmdb:\d{1,9}", e["key"], re.ASCII):
        raise ValueError("invalid title key")
    if not isinstance(e["who"], str) or not who_ok(e["who"], "both"):
        raise ValueError("unknown person")
    if not isinstance(e["kind"], str) or e["kind"] not in ("movie", "tv") \
            or not isinstance(e["action"], str) or e["action"] not in REMOTE_ACTIONS:
        raise ValueError("invalid kind or action")
    if not isinstance(e["title"], str) or not e["title"].strip() or len(e["title"]) > 300 \
            or any(ord(c) < 32 for c in e["title"]):
        raise ValueError("invalid title")
    if not isinstance(e["year"], str) or not re.fullmatch(r"(?:[0-9]{4})?", e["year"]):
        raise ValueError("invalid year")
    if not isinstance(e["reasons"], list) or len(e["reasons"]) > len(REMOTE_REASONS) \
            or any(not isinstance(r, str) or r not in REMOTE_REASONS for r in e["reasons"]) \
            or len(set(e["reasons"])) != len(e["reasons"]) or (e["reasons"] and e["action"] != "not_for_us"):
        raise ValueError("invalid reasons")
    if e["row"] is not None and (not isinstance(e["row"], str) or e["row"] not in REMOTE_ROWS):
        raise ValueError("invalid row")
    if e["undo"] is not None and (not isinstance(e["undo"], str) or not re.fullmatch(r"[0-9a-f]{32}", e["undo"])):
        raise ValueError("invalid undo")
    return e


def write_feedback(body):
    e = validate_feedback(body)
    with _remote_lock:
        events = read_feedback()
        if e["undo"]:
            active, tonight = fold_feedback(events)
            target = next((x for x in (*active.values(), *tonight.values()) if x["id"] == e["undo"]), None)
            if not target \
                    or any(e[k] != target[k] for k in ("key", "kind", "title", "year", "who", "action", "reasons", "row")):
                raise ValueError("invalid undo target")
        e = {"t": time.time(), "id": uuid.uuid4().hex, **e}
        path = os.path.join(DATA, "feedback.jsonl")
        raw = json.dumps(e, ensure_ascii=False) + "\n"
        if _remote_cache["lines"] >= 5000 or _remote_cache["corrupt"]:
            events = compact_feedback(events + [e], e["t"])
            if len(events) > 5000:
                raise ValueError("feedback capacity reached")
            tmp = path + ".tmp"
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                for old in events:
                    fh.write(json.dumps(old, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        else:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "w") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
        _remote_cache.clear()
        return e


def compact_feedback(events, now):
    active, tonight = fold_feedback(events)
    kept, people = {}, {}
    for (key, person), e in (*active.items(), *tonight.items()):
        if e["action"] == "not_tonight" and now - e["t"] >= 18 * 3600:
            continue
        kept[e["id"]] = e
        people.setdefault(e["id"], set()).add(person)
    order = {e["id"]: i for i, e in enumerate(events)}
    return [{**kept[id], "who": "both" if people[id] >= set(PEOPLE) else next(iter(people[id]))}
            for id in sorted(kept, key=order.get)]


def remote_history():
    state = feedback_state()
    by_id = {}
    for (_, person), e in (*state["active"].items(), *state["tonight"].items()):
        if e["action"] == "not_tonight" and time.time() - e["t"] >= 18 * 3600:
            continue
        by_id.setdefault(e["id"], {**e, "by": []})["by"].append(person)
    events = sorted(by_id.values(), key=lambda e: e["t"], reverse=True)
    m = _model["m"]
    summary = {}
    for person in PEOPLE:
        aff = (m.get("paff") or {}).get(person, {}) if m else {}
        top = sorted(((a, f) for f, a in aff.items() if f.startswith("x:") and a > 0), reverse=True)[:3]
        summary[person] = ", ".join(m["names"].get(f, f[2:]) for _, f in top) if top else "Your next favourite starts with you."
    return {"events": events[:120],
            "summary": summary}


def remote_rows(lang):
    out = []
    person = who_watching()[0]
    if person in PEOPLE:
        interview = interview_next(person)
        if not interview["done"]:
            out.append({"id": rid('interview'), "name": "Find your taste", "metas": [
                {"id": a["key"], "type": "movie" if a["kind"] == "movie" else "series", "name": a["title"],
                 "releaseInfo": a["year"], "poster": IMG + "w300" + frec(a["key"], a["kind"]).get("poster", "")}
                for a in interview["anchors"]]})
    for cid, _, name in feed_rows(lang):
        if cid not in REMOTE_ROWS:
            continue
        metas = row_metas(cid, "tv", lang)
        out.append({"id": cid, "name": row_name(cid, lang, name, metas), "metas": metas[:15]})
    return {"rows": out}


def remote_send(handler, code, body=b"", content_type="application/json; charset=utf-8"):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode()
    elif isinstance(body, str):
        body = body.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def read_remote_body(handler, length):
    deadline = time.monotonic() + handler.timeout
    body = bytearray()
    while len(body) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("body deadline")
        handler.connection.settimeout(remaining)
        chunk = handler.rfile.read1(length - len(body))
        if not chunk:
            raise ValueError("incomplete body")
        body.extend(chunk)
    if time.monotonic() >= deadline:
        raise TimeoutError("body deadline")
    return body


# --- api views ---
def visible_by(by, viewer):
    return viewer != GUEST and (viewer is None or viewer in by or "together" in by)


def api_person(asked, viewer):
    if viewer is not None:
        if asked not in ((None, viewer) if viewer == GUEST else (None, viewer, "together")):
            raise PermissionError("person")
        return viewer if asked is None else asked
    if not who_ok(asked, None, "together", GUEST):
        raise ValueError("unknown person")
    return asked


def member_view(req):
    if req["viewer"] == GUEST or req.get("query", {}).get("person") == GUEST:
        raise PermissionError("guest is read-only")


_title_notes = {"model": None, "notes": {}}


def title_view(key, kind, person=None):
    rec = frec(key, kind)
    if not rec:
        return None
    m = get_model(False)
    by = m["ent"][key][1] if m and key in m["ent"] else None
    if by is None:
        entries = [e for e in (_profiles.get("household") or {}).get("entries", [])
                   if e["key"] == key and e["kind"] == kind]
        by = [who for e in entries for who in e.get("by", ())] if entries else None
    if by is not None and not visible_by(by, person):
        return None
    conf, pct, why = None, None, []
    if m and person != GUEST:
        if _title_notes["model"] is not m:
            _title_notes.update(model=m, notes={})
        notes = _title_notes["notes"]
        stamp = (id(m), key, kind, person, HOUSEHOLD_PRIOR, maturity(m, person)["explicit"], m.get("cal"), json.dumps(rec, sort_keys=True))
        cached = notes.get(stamp)
        if cached is None:
            s, fids = person_score(m, key, person, kind)
            # Together's household fallback may name a facet belonging to only one person.
            if person == "together":
                fids = [f for f in fids if f in m.get("paff", {}).get(person, {})]
            conf, pct = confidence(m, key, s, fids, person)
            why = reasons(m, key, fids, 3, person, kind)
            if len(notes) >= 1000:
                notes.clear()
            cached = conf, pct, tuple(why)
            notes[stamp] = cached
        conf, pct, why = cached
        why = list(why)
    seed = bool(why and m and any(why[0].endswith(" like " + title + (who_label(by) if person is None else ""))
                                 for title, by in m["ent"].values() if visible_by(by, person)))
    return {"key": key, "kind": kind, "title": rec.get("title", ""), "person": person,
            "match": pct, "confidence": conf, "reasons": why, "maturity": maturity(m, person),
            "rung": ("seed" if seed else "facet") if why else None,
            "experiences": [] if person == GUEST else [EXPERIENCES.get(k, (k,))[0] for k, v in
                            sorted((rec.get("x") or {}).items(), key=lambda x: (-x[1], x[0]))[:5]]}


def explain_pick_rule(inp, params):
    view = title_view(inp["key"], inp["kind"], inp["person"])
    if view is None:
        return {"why": None, "reasons": [], "match": None, "confidence": None, "rung": None,
                "maturity": maturity(get_model(False), inp["person"])}, 0.0
    return {"why": next(iter(view["reasons"]), None), **{k: view[k] for k in ("reasons", "match", "confidence", "rung", "maturity")}}, \
        view["match"] / 100 if view["match"] is not None else 0.0


MATURITY = {"state": ("too_new", "learning", "ready", "guest"), "explicit": int, "need": int}
INTERVIEW_OUT = {"v": int, "person": PEOPLE, "maturity": MATURITY,
    "progress": {"positives": int, "need": int, "negatives_with_reason": int, "need_neg": int},
    "done": bool, "done_reason": ("ready", "answers", None),
    "anchors": [{"key": TKEY, "kind": KIND, "title": str, "year": (str, ""), "experience": tuple(EXPERIENCES)}, 12]}
decision("interview_next", ver=2, lane=None, cap=None, ttl=DAY, serving=True,
    llm=None, requires=(), min_acc=1, api=True, inp={"person": PEOPLE}, out=INTERVIEW_OUT,
    norm=lambda x: x, rule=lambda x, p: (interview_next(x["person"]), 1), params={},
    fallback=lambda x, err: ({"v": 2, "person": x["person"], "maturity": (member := maturity(get_model(False), x["person"])),
        "progress": {"positives": 0, "need": 20, "negatives_with_reason": 0, "need_neg": 5},
        "done": member["state"] == "ready", "done_reason": "ready" if member["state"] == "ready" else None, "anchors": []}, 0))
decision("explain_pick", ver=2, lane=None, cap=None, ttl=DAY, serving=True, llm=None, requires=(),
    min_acc=1, api=True, inp={"key": TKEY, "kind": KIND, "person": (*PEOPLE, "together", GUEST, None)},
    out={"why": (str, None), "match": (int, None), "confidence": ("Strong fit", "Good fit", "Wildcard", None),
         "rung": ("seed", "facet", None), "reasons": [str, 3], "maturity": MATURITY}, norm=lambda x: x, rule=explain_pick_rule, params={},
    fallback=lambda inp, err: ({"why": None, "reasons": [], "match": None, "confidence": None, "rung": None,
                               "maturity": maturity(get_model(False), inp["person"])}, 0.0))


decision("rank_titles", ver=1, lane=None, cap=None, ttl=DAY, serving=True, llm=None, requires=(), min_acc=1,
         api=True, inp=RANK_INP, out=RANK_OUT, norm=rank_norm, rule=rank_rule(), params={}, fallback=rank_fallback)


def api_title(req):
    if "kind" not in req["query"]:
        raise ValueError("kind")
    person = api_person(req["query"].get("person"), req["viewer"])
    view = title_view(req["tail"], req["query"]["kind"], req["viewer"] or person)
    if view is None:
        raise ApiError(404, "not found")
    return {**view, "person": person}


def profile_view(req):
    member_view(req)
    viewer = req["viewer"]
    person = api_person(req["query"].get("person"), viewer)
    p = _profiles.get("household") or {"entries": []}
    entries = person_entries(p, person) if person in PEOPLE else p["entries"]
    entries = [e for e in entries if visible_by(e.get("by", ()), viewer)]
    signals, sources = {}, {}
    for e in entries:
        signal = e["signal"]
        signals[signal] = signals.get(signal, 0) + 1
        src = next((f for f in e.get("flags", []) if f in IMPORT_TRUST), "import") if e["who"] == "import" else e["who"]
        src = src if src in ("tv", "trakt", "told", "remote", "import", *IMPORT_TRUST) else "import"
        sources[src] = sources.get(src, 0) + 1
    m = get_model(False)
    def facets(types):
        if not m:
            return []
        own = viewer or (person if person in PEOPLE else None)
        aff = m["paff"].get(own, {}) if own else m["aff"]
        out = []
        for fid, value in sorted(aff.items(), key=lambda pair: (-pair[1], pair[0])):
            if fid[0] not in types:
                continue
            anchors = [m["ent"][k][0] for _, k in m["holders"].get(fid, []) if k in m["ent"]
                       and visible_by(m["ent"][k][1], viewer or own)][:3]
            out.append({"id": fid, "name": m["names"].get(fid, fid), "affinity": round(value, 4), "anchors": anchors})
            if len(out) == 10:
                break
        return out
    return {"person": person, "maturity": maturity(m, viewer or person), "titles": len(entries), "signals": signals, "sources": sources,
            **{name: facets(types) for name, types in (("experiences", "x"), ("themes", "kt"), ("creators", "d"))}}


def taste_report_view(req):
    member_view(req)
    import zlib
    viewer = req["viewer"]
    person = api_person(req["query"].get("person"), viewer)
    own = viewer or (person if person in PEOPLE else None)
    m = get_model(False)
    axes = taste_axes(m, own)
    for facets in axes.values():
        for facet in facets:
            facet["anchors"] = [m["ent"][k][0] for _, k in m["holders"].get(facet["id"], ())
                if k in m["ent"] and visible_by(m["ent"][k][1], own)][:3]
    movers = {"warming_up": True}
    with _taste_ring_lock:
        ring = dict(_taste_ring)
    if m and ring:
        newest = max(ring)
        cutoff = (datetime.date.fromisoformat(newest) - datetime.timedelta(days=min(req["query"].get("days", 7), TASTE_RING_DAYS - 1))).isoformat()
        older = [day for day in ring if day <= cutoff]
        if older:
            old = max(older)
            who = own or "household"
            before = json.loads(zlib.decompress(ring[old][who]))
            after = json.loads(zlib.decompress(ring[newest][who]))
            facets = [{"id": fid, "name": m["names"].get(fid, fid), "before": before.get(fid, 0),
                       "after": after.get(fid, 0), "delta": round(after.get(fid, 0) - before.get(fid, 0), 4)}
                      for fid in before.keys() | after.keys() if abs(after.get(fid, 0) - before.get(fid, 0)) > 1e-6]
            # shortcut: omitted top-40 affinities count as zero; use CLI replay for exact movers.
            movers = {"from": old, "to": newest, "approximate": True,
                      "facets": sorted(facets, key=lambda f: (-abs(f["delta"]), f["id"]))[:10]}
    result = {"person": person, "maturity": maturity(m, own), "axes": axes, "movers": movers,
              "calibration": {"bg_n": len((m or {}).get("bg", [])),
                              "pbg_n": len((m or {}).get("pbg", {}).get(own, [])),
                              "explicit": (m or {}).get("nexp", {}).get(own, 0)}}
    # Bound even unusually long imported names and anchors.
    while len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        anchored = [f for fs in axes.values() for f in fs if f["anchors"]]
        if anchored:
            max(anchored, key=lambda f: len(json.dumps(f["anchors"]).encode()))["anchors"].pop()
        elif movers.get("facets"):
            movers["facets"].pop()
        else:
            max(axes.values(), key=lambda fs: len(json.dumps(fs).encode())).pop()
    return result


API_ROWS = REMOTE_ROWS


def rows_view(req):
    if req["viewer"] == GUEST or req["query"].get("person") == GUEST:
        api_person(req["query"].get("person"), req["viewer"])
        return guest_rows_view(req["query"].get("lang", LANG), req["query"].get("row"))
    lang = req["query"].get("lang", LANG)
    viewer = req["viewer"]
    person = api_person(req["query"].get("person"), viewer)
    who = viewer or (person if person in PEOPLE else None)
    rows = []
    for cid, types, name in feed_rows(lang, profiles=False):
        if cid not in API_ROWS or req["query"].get("row", cid) != cid:
            continue
        if viewer and (cid == rid('week') or ROW_OWNER.get(cid, viewer) != viewer):
            continue
        items = []
        metas = row_metas(cid, "tv", lang, who=who)
        for meta in metas[:15]:
            kind = "movie" if meta.get("type") == "movie" else "tv"
            view = title_view(meta["id"], kind, who or person)
            if viewer and view is None:
                continue
            why = next(iter(view["reasons"]), None) if view else None
            if why is None and viewer is None:
                why = meta.get("description", "").split(" · ")[0] or None
            items.append({"key": meta["id"], "kind": kind, "title": meta.get("name", ""),
                          "year": meta.get("releaseInfo", ""), "poster": meta.get("poster"), "why": why})
        # Built shelf names can contain household anchor titles.
        rows.append({"id": cid, "name": "Picked for you" if viewer else row_name(cid, lang, name, metas), "items": items})
    return {"lang": lang, "person": person, "rows": rows}


def guest_rows_view(lang, row=None):
    rows = []
    for cid in DEDUPE_ORDER:
        if cid not in GUEST_ROWS or row not in (None, cid):
            continue
        items = []
        for typ in ("movie", "series"):
            for meta in row_list(cid, typ, lang)[:15]:
                kind = "movie" if typ == "movie" else "tv"
                if title_view(meta["id"], kind, GUEST) is None:
                    continue
                items.append({"key": meta["id"], "kind": kind, "title": meta.get("name", ""),
                              "year": meta.get("releaseInfo", ""), "poster": meta.get("poster"), "why": None})
        rows.append({"id": cid, "name": "Top 10 this week", "items": items})
    return {"lang": lang, "person": GUEST, "rows": rows}


def metrics_view(req):
    member_view(req)
    report = metrics(days=req["query"].get("days", LEARN_WINDOW))
    viewer = req["viewer"]
    if viewer:
        if "who" in report:
            report["who"] = {k: v for k, v in report["who"].items() if k in (viewer, "both", "together", "unknown")}
        report["rows"] = {k: v for k, v in report.get("rows", {}).items()
                          if k not in (*{rid(p) for p in ARCHIVED}, rid('week')) and ROW_OWNER.get(k, viewer) == viewer}
    else:
        report["rows"] = {k: v for k, v in report.get("rows", {}).items() if k not in {rid(p) for p in ARCHIVED}}
    with _usage_lock:
        _usage_roll()
        usage = {"day": _usage["day"], "month_cost": _usage["month_cost"],
                 "caps": {**AI_CAPS, "monthly_usd": AI_MONTHLY_USD}}
    return {"metrics": report, "decide": decide_stats(), "usage": usage}


def imports_check(events, req):
    for e in events:
        if not isinstance(e, dict):
            continue
        src = e.get("src")
        if isinstance(src, str) and (src.startswith(("agent:", "sync:")) or src == "utterance"):
            raise ApiError(403, "src")
        if req["viewer"] and e.get("who") != req["viewer"]:
            raise PermissionError("person")


def imports_write(events, req):
    valid, indices, rejected = [], [], []
    for i, e in enumerate(events):
        try:
            valid.append(validate_import(e))
            indices.append(i)
        except (ValueError, TypeError, OverflowError):
            rejected.append({"i": i, "error": "input"})
    try:
        added, duplicates, conflicts = write_imports(valid, keep_ids=True)
    except ValueError:
        raise ApiError(409, "limit") from None
    rejected.extend({"i": indices[i], "error": "conflict"} for i in conflicts)
    return {"accepted": len(added), "duplicates": duplicates, "rejected": sorted(rejected, key=lambda x: x["i"])}


def undo_write(events, req):
    accepted, rejected = 0, []
    with _imports_lock:
        stored_events = read_imports()
        by_id = {}
        for stored in stored_events:
            by_id.setdefault(stored["id"], stored)
        removed = set()
        for i, e in enumerate(events):
            if not isinstance(e, dict) or set(e) != {"id"} or not isinstance(e["id"], str) \
                    or not re.fullmatch(r"[0-9a-f]{32}", e["id"]):
                rejected.append({"i": i, "error": "id"})
                continue
            stored = by_id.get(e["id"])
            if stored is None or req["viewer"] and stored["who"] != req["viewer"]:
                rejected.append({"i": i, "error": "id"})
                continue
            del by_id[e["id"]]
            removed.add(e["id"])
            accepted += 1
        if accepted:
            save_imports([e for e in stored_events if e["id"] not in removed])
    return {"accepted": accepted, "duplicates": 0, "rejected": rejected}


def interview_check(events, req):
    member_view(req)
    for e in events:
        if not isinstance(e, dict):
            continue
        person = api_person(e.get("who"), req["viewer"])
        if person not in PEOPLE:
            raise ValueError("unknown person")
        if e.get("who") != person:
            raise PermissionError("person")
        if not isinstance(e.get("key"), str) or e.get("kind") not in KIND:
            continue
        if title_view(e.get("key"), e.get("kind"), person) is None:
            raise PermissionError("person")


def interview_write(events, req):
    accepted, rejected = 0, []
    for i, e in enumerate(events):
        try:
            interview_answer(e)
            accepted += 1
        except (ValueError, TypeError):
            rejected.append({"i": i, "error": "input"})
    return {"accepted": accepted, "duplicates": 0, "rejected": rejected}


EVENT_STREAMS = {
    "imports": {"check": imports_check, "write": imports_write, "example": []},
    "imports_undo": {"check": lambda events, req: None, "write": undo_write, "example": []},
    "interview": {"check": interview_check, "write": interview_write, "example": []},
}


def intake(stream, events, req):
    member_view(req)
    if not isinstance(stream, str) or stream not in EVENT_STREAMS:
        raise ValueError("stream")
    if not isinstance(events, list) or len(events) > 500:
        raise ValueError("events")
    spec = EVENT_STREAMS[stream]
    spec["check"](events, req)
    return spec["write"](events, req)


def api_events(req):
    body = req["body"]
    if not isinstance(body, dict) or set(body) != {"stream", "events"}:
        raise ValueError("body")
    return intake(body["stream"], body["events"], req)


# --- api v1 ---
# 1. eval/decisions.py --new NAME [--ranker]: paste the printed decision block.
# 2. Rerun --new NAME to write 20 synthetic rows (+ decide_kit.SETUP for model rules).
# 3. Fill expect and replace TODO notes; the contract fails until both are done.
# 4. skald decide NAME --eval; eval/decisions.py --skill --write refreshes API docs.
API_PORT = int(os.environ["API_PORT"]) if os.environ.get("API_PORT") else None
# API_LOOPBACK_OPEN=1 permits tokenless read/decide on loopback; default off.
_api_lock = threading.Lock()
_api_clients = {}
_api_rates = {}
if not CONFIG["addon"]["legacy_open"] and os.environ.get("API_LOOPBACK_OPEN") == "1":
    print("API_LOOPBACK_OPEN ignored: addon.legacy_open is false", file=sys.stderr, flush=True)


def safe_path(handler, prefixes, *, addon=False):
    path = handler.raw_requestline.decode("iso-8859-1").split()[1].split("?", 1)[0]
    parts = path.split("/")
    if addon and len(parts) >= 6 and parts[-4] == "catalog" and parts[-3] in ("movie", "series") \
            and parts[-2] == rid('search') and parts[-1].endswith(".json"):
        # Search extras keep the existing HTTP request-line cap (64 KiB), not structural path rules.
        path = "/".join(parts[:-1])
    return len(path) <= 256 and path.isascii() and urllib.parse.unquote(path).isascii() \
        and not any(x in path.lower() for x in ("%2f", "%5c", "%2e", "..", "//", "\\")) \
        and path.startswith(prefixes)


def _clients_read(missing=False):
    path = os.path.join(DATA, "clients.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        if missing:
            return []
        raise
    if not isinstance(data, dict) or set(data) != {"clients"} or not isinstance(data["clients"], list):
        raise ValueError("clients")
    names = set()
    for c in data["clients"]:
        if not isinstance(c, dict) or set(c) != set("name sha256 scopes person created".split()) \
                or not isinstance(c["name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", c["name"]) \
                or c["name"] == "loopback" or c["name"] in names \
                or not isinstance(c["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", c["sha256"]) \
                or not isinstance(c["scopes"], list) or not c["scopes"] \
                or any(s not in ("read", "decide", "events") for s in c["scopes"]) \
                or not who_ok(c["person"], None, GUEST) \
                or c["person"] == GUEST and "events" in c["scopes"] \
                or type(c["created"]) not in (int, float) or not math.isfinite(c["created"]):
            raise ValueError("clients")
        names.add(c["name"])
    return data["clients"]


def new_client(name, scopes, person=None):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name) or name == "loopback":
        raise ValueError("invalid client name")
    if not scopes or any(s not in ("read", "decide", "events") for s in scopes) or not who_ok(person, None, GUEST):
        raise ValueError("unknown person" if not who_ok(person, None, GUEST) else "invalid client scopes")
    if person == GUEST and "events" in scopes:
        raise ValueError("guest is read-only")
    with _api_lock:
        clients = _clients_read(missing=True)
        if any(c["name"] == name for c in clients):
            raise ValueError("duplicate client")
        token = secrets.token_urlsafe(32)
        clients.append({"name": name, "sha256": hashlib.sha256(token.encode()).hexdigest(), "scopes": list(scopes),
                        "person": person, "created": time.time()})
        _decision_write_json(os.path.join(DATA, "clients.json"), {"clients": clients})
    return token


def revoke_client(name):
    with _api_lock:
        clients = _clients_read()
        kept = [c for c in clients if c["name"] != name]
        if len(kept) == len(clients):
            raise ValueError("unknown client")
        _decision_write_json(os.path.join(DATA, "clients.json"), {"clients": kept})


def _api_auth(handler):
    header = handler.headers.get("Authorization")
    if header is None and CONFIG["addon"]["legacy_open"] and os.environ.get("API_LOOPBACK_OPEN") == "1" and loopback(handler):
        return {"name": "loopback", "scopes": ["read", "decide"], "person": None}, None
    if header is None:
        return None, 401
    try:
        path = os.path.join(DATA, "clients.json")
        stamp = os.stat(path).st_mtime_ns
        with _api_lock:
            if _api_clients.get("path") != path or _api_clients.get("mtime") != stamp:
                clients = _clients_read()
                _api_clients.update(path=path, mtime=stamp, clients=clients)
            clients = _api_clients["clients"]
    except (OSError, ValueError, TypeError, RecursionError):
        return None, 503
    valid = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", header)
    digest = hashlib.sha256((valid[1] if valid else "").encode()).hexdigest()
    found = None
    for c in clients:
        if hmac.compare_digest(digest, c["sha256"]) and valid:
            found = c
    return found, None if found else 401


class ApiError(Exception):
    def __init__(self, code, text):
        self.code, self.text = code, text


API_ROUTES = {}


def api(key, get=None, post=None, query=None, tail=None, example=None):
    assert re.fullmatch(r"[a-z][a-z0-9_]*(?:/(?:[a-z][a-z0-9_]*|\*))?", key) and key not in API_ROUTES
    assert example is not None and isinstance(example, dict)
    methods = {}
    for method, spec in (("GET", get), ("POST", post)):
        if spec is not None:
            assert len(spec) in (2, 3) and spec[0] in ("read", "decide", "events") and callable(spec[1])
            methods[method] = (*spec, 200) if len(spec) == 2 else spec
    assert methods
    API_ROUTES[key] = {"methods": methods, "query": query or {}, "tail": tail, "example": example}
    return API_ROUTES[key]


def api_decide(req):
    connectors_demand(time.time())
    body, client, name = req["body"], req["client"], req["tail"]
    if not isinstance(body, dict) or set(body) - {"in", "items", "person", "no_llm"} \
            or ("in" in body) == ("items" in body):
        raise ApiError(400, "body")
    person = body.get("person", req["viewer"])
    if not who_ok(person, None, "together", GUEST):
        raise ApiError(400, "person")
    if req["viewer"] and person not in ((GUEST,) if req["viewer"] == GUEST else (req["viewer"], "together")):
        raise PermissionError("person")
    person = api_person(person, req["viewer"])
    if person == GUEST and name not in ("explain_pick", "rank_titles"):
        raise PermissionError("guest is read-only")
    if type(body.get("no_llm", False)) is not bool:
        raise ApiError(400, "no_llm")
    items = body.get("items") if "items" in body else [body["in"]]
    if not isinstance(items, list) or len(items) > 25:
        raise ApiError(400, "items")
    if "person" in DECISIONS[name]["inp"]:
        items = [{**item, "person": api_person(item.get("person", person), req["viewer"])}
                 if isinstance(item, dict) else item for item in items]
    try:
        results = decide_many(name, items, person=person, allow_llm=not body.get("no_llm", False), client=client["name"])
        return {"results": results} if "items" in body else results[0]
    except ValueError as err:
        fields, schemas = set(), [DECISIONS[name]["inp"]]
        while schemas:
            schema = schemas.pop()
            if isinstance(schema, dict):
                fields.update(schema)
                schemas.extend(schema.values())
            elif isinstance(schema, (list, tuple)):
                schemas.extend(schema)
        path = str(err)
        # Unknown keys can themselves contain secrets; expose only registered field paths.
        valid = re.fullmatch(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*|\[[0-9]+\])*", path)
        raise ApiError(400, path if valid and set(re.findall(r"[a-z_][a-z0-9_]*", path)) <= fields else "input") from None


def api_resolve(req):
    from pathlib import Path
    from skald.ingest import Candidate, clean_title
    from skald.ingest.resolve import Resolver
    body = req["body"]
    if not isinstance(body, dict) or set(body) != {"items"} or not isinstance(body["items"], list) or len(body["items"]) > 100:
        raise ValueError("body")
    candidates = []
    for item in body["items"]:
        if not isinstance(item, dict) or set(item) != {"title", "year", "kind", "ids"}:
            raise ValueError("body")
        if not isinstance(item["title"], str) or not isinstance(item["year"], str) or not re.fullmatch(r"(?:[0-9]{4})?", item["year"]) \
                or item["kind"] not in ("", "movie", "tv") or not isinstance(item["ids"], dict):
            raise ValueError("body")
        for key, value in item["ids"].items():
            pattern = {"imdb": r"tt[0-9]{1,12}", "tmdb": r"[1-9][0-9]{0,8}"}.get(key)
            if pattern is None or not isinstance(value, (str, int)) or not re.fullmatch(pattern, str(value)):
                raise ValueError("body")
        candidates.append(Candidate(clean_title(item["title"]), item["year"], item["kind"], item["ids"]))
    if not API_KEY:
        raise ApiError(503, "Server has no TMDB key; use --resolve-online with TMDB_API on your laptop.")
    deadline = time.monotonic() + 20
    def lookup(path, **params):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("lookup deadline")
        return tmdb(path, _timeout=min(8, remaining), **params)
    # Only TMDB's normal cache survives; lookup requests never write household data.
    with tempfile.TemporaryDirectory() as folder:
        resolver = Resolver(lookup, cache=Path(folder) / "resolve.json", max_ai_usd=0)
        results = []
        for candidate in candidates:
            try:
                results.extend(resolver.resolve([candidate], persist=False))
            except (OSError, urllib.error.URLError):
                results.append(resolver.resolution(None, 0, "unresolved"))
    return {"results": results}


def api_session(req):
    member_view(req)
    view = session_view(req["viewer"])
    if req["viewer"] and view["last"]:
        last = view["last"]
        plays = [e for e in session_plays({"t": last["since"], "until": last["until"]})
                 if visible_by(e.get("by", ()), req["viewer"])]
        view["last"] = {**last, "plays": len(plays), "titles": [e["title"] for e in plays[:2]]} if plays else None
    return view


def api_write_session(req):
    try:
        write_session(req["body"], "api", req["viewer"])
        return {"session": api_session(req)}
    except SessionConflict:
        raise ApiError(409, "session") from None
    except ValueError:
        raise ApiError(400, "session") from None


def api_kind(value):
    if value not in KIND:
        raise ValueError
    return value


def api_lang(value):
    if not LANG_RE.fullmatch(value):
        raise ValueError
    return value


def api_row(value):
    if value not in API_ROWS | GUEST_ROWS:
        raise ValueError
    return value


def api_days(value):
    if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= ONLINE_DAYS:
        raise ValueError
    return int(value)


def routes_view(req):
    return {"routes": [{"path": "/api/v1/" + key,
                        "methods": {method: spec[0] for method, spec in route["methods"].items()},
                        "query": sorted(route["query"]),
                        "tail": "custom" if callable(route["tail"]) else route["tail"],
                        "example": route["example"]} for key, route in sorted(API_ROUTES.items())]}


api("decide", get=("read", lambda req: {"types": decide_index(), "person": req["viewer"]}), example={})
api("decide/*", post=("decide", api_decide),
    tail=lambda name: name in DECISIONS and DECISIONS[name]["api"],
    example={"tail": "route_query", "body": {"in": {"query": "synthetic request"}, "no_llm": True}})
api("resolve", post=("read", api_resolve), example={"body": {"items": []}})
api("session", get=("read", api_session), post=("events", api_write_session, 201), example={"body": {"who": "both"}})
api("titles/*", get=("read", api_title), tail=r"tmdb:[0-9]{1,9}",
    query={"kind": api_kind, "person": lambda p: api_person(p, None)},
    example={"tail": "tmdb:900001", "query": {"kind": "movie"}})
api("profile", get=("read", profile_view), query={"person": lambda p: api_person(p, None)}, example={})
api("taste", get=("read", taste_report_view), query={"person": lambda p: api_person(p, None), "days": api_days}, example={})
api("rows", get=("read", rows_view), query={"lang": api_lang, "person": lambda p: api_person(p, None), "row": api_row}, example={})
api("metrics", get=("read", metrics_view if CONNECTOR_SEPARATE_ROUTE else lambda req: metrics_view(req) | connectors_view(req)),
    query={"days": api_days}, example={})
if CONNECTOR_SEPARATE_ROUTE:
    api("connectors", get=("read", connectors_view), example={})
api("events", get=("events", lambda req: {"person": req["viewer"]}),
    post=("events", api_events), example={"body": {"stream": "imports", "events": []}})
api("routes", get=("read", routes_view), example={})
api("interview/next", get=("read", lambda req: interview_next(api_person(req["query"].get("person"), req["viewer"]))),
    query={"person": lambda p: api_person(p, None)}, example={"query": {"person": PEOPLE[0]}})


def api_route(handler, post=False):
    def error(code, text):
        return remote_send(handler, code, {"error": text})
    if "Origin" in handler.headers:
        return error(403, "origin")
    client, code = _api_auth(handler)
    if code:
        return error(code, "unavailable" if code == 503 else "unauthorized")
    with _api_lock:
        now = time.monotonic()
        rates = _api_rates.setdefault(client["name"], deque())
        while rates and rates[0] <= now - 60:
            rates.popleft()
        if len(rates) >= 60:
            return error(429, "rate")
        rates.append(now)
    url = urllib.parse.urlsplit(handler.path)
    path = url.path[len("/api/v1/"):]
    if client["person"] == GUEST and path.split("/", 1)[0] in ("profile", "taste", "memory", "session", "metrics", "interview", "connectors"):
        return error(403, "person")
    route_key = path
    tail, route = None, API_ROUTES.get(path) if not path.endswith("/*") else None
    if route is None:
        for key, spec in API_ROUTES.items():
            if key.endswith("/*") and path.startswith(key[:-1]):
                value = path[len(key)-1:]
                pattern = spec["tail"]
                if (pattern(value) if callable(pattern) else re.fullmatch(pattern or r"[^/]+", value)):
                    route_key, tail, route = key, value, spec
                    break
    if route is None:
        return error(404, "not found")
    method = route["methods"].get(handler.command)
    if method is None:
        return error(405, "method")
    scope, view, status = method
    if scope not in client["scopes"]:
        return error(403, "scope")
    if handler.headers.get_all("Transfer-Encoding"):
        return error(400, "size")
    lengths = handler.headers.get_all("Content-Length") or []
    if len(lengths) > 1:
        return error(400, "size")
    length = lengths[0] if lengths else None
    body = None
    if handler.command == "GET":
        if length is not None and (not re.fullmatch(r"[0-9]{1,5}", length) or int(length) != 0):
            return error(400, "body")
    else:
        if length is None:
            return error(411, "length")
        if not re.fullmatch(r"[0-9]{1,5}", length):
            return error(400, "size")
        if int(length) > 65536:
            return error(413, "size")
        if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return error(415, "json")
        try:
            raw = read_remote_body(handler, int(length))
        except TimeoutError:
            return error(408, "deadline")
        except (ValueError, OSError):
            return error(400, "body")
        finally:
            handler.connection.settimeout(handler.timeout)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return error(400, "json")
    if len(url.query) > 256:
        return error(400, "query")
    try:
        params = urllib.parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except ValueError:
        return error(400, "query")
    query = {}
    for key, values in params.items():
        if key not in route["query"]:
            return error(400, "query")
        if len(values) != 1:
            return error(400, key)
        try:
            query[key] = route["query"][key](values[0])
        except (ValueError, TypeError):
            return error(400, key)
    req = {"client": client, "viewer": client["person"], "query": query, "tail": tail, "body": body}
    try:
        return remote_send(handler, status, view(req))
    except PermissionError:
        return error(403, "person")
    except ApiError as err:
        return error(err.code, err.text)
    except ValueError as err:
        field = str(err)
        return error(400, field if field in {*route["query"], "body", "stream", "events"} else "input")
    except OSError:
        return error(503, "unavailable")
    except Exception as e:
        print(f"api {route_key} failed: {type(e).__name__}", flush=True)
        return error(500, "internal")

class ApiHandler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Content-Security-Policy", REMOTE_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        remote_send(self, code, {"error": "request"})

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.request_version == "HTTP/0.9":
            self.send_error(400)
            return False
        if not safe_path(self, ("/api/v1/",)):
            self.send_error(404)
            return False
        return True

    def do_GET(self):
        api_route(self)

    def do_POST(self):
        api_route(self, post=True)

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.do_GET
        raise AttributeError(name)


class RemoteHandler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Content-Security-Policy", REMOTE_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        remote_send(self, code)

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.request_version == "HTTP/0.9":
            self.send_error(400)
            return False
        prefixes = ("/taste/", "/api/v1/") if API_PORT == REMOTE_PORT else ("/taste/",)
        if not safe_path(self, prefixes):
            if API_PORT == REMOTE_PORT and self.path.startswith("/api/v1/"):
                remote_send(self, 404, {"error": "request"})
            else:
                self.send_error(404)
            return False
        return True

    def do_GET(self):
        if self.path.startswith("/api/v1/"):
            return api_route(self)
        remote_route(self)

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            return api_route(self, post=True)
        remote_route(self, post=True)

    def __getattr__(self, name):
        if name.startswith("do_") and self.path.startswith("/api/v1/"):
            return self.do_GET
        raise AttributeError(name)


def remote_route(handler, post=False):
    url = urllib.parse.urlsplit(handler.path)
    parts = url.path.split("/")
    token = parts[2] if len(parts) > 2 else ""
    try:
        valid = hmac.compare_digest(token.encode(), remote_token().encode())
    except (OSError, ValueError):
        return remote_send(handler, 503)
    if not valid or len(parts) != 4 or parts[1] != "taste":
        return remote_send(handler, 404)
    route = parts[3]
    if post:
        if route not in ("feedback.json", "session.json"):
            return remote_send(handler, 404)
        now = time.monotonic()
        with _remote_lock:
            while _remote_posts and _remote_posts[0] <= now - 60:
                _remote_posts.popleft()
            if len(_remote_posts) >= 60:
                return remote_send(handler, 429, {"error": "Try again in a minute."})
            _remote_posts.append(now)
        origin = handler.headers.get("Origin")
        try:
            cross_origin = origin and urllib.parse.urlsplit(origin).netloc != handler.headers.get("Host")
        except ValueError:
            cross_origin = True
        if handler.headers.get("Sec-Fetch-Site") == "cross-site" or cross_origin:
            return remote_send(handler, 403, {"error": "Use this page to send feedback."})
        length = handler.headers.get("Content-Length", "")
        if handler.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,5}", length):
            return remote_send(handler, 400, {"error": "Invalid request size."})
        if int(length) > (512 if route == "session.json" else 4096):
            return remote_send(handler, 413, {"error": "Feedback is too large."})
        if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return remote_send(handler, 415, {"error": "Send JSON feedback."})
        try:
            body = json.loads(read_remote_body(handler, int(length)))
            if route == "session.json":
                guest = who_watching()[0] == GUEST
                view = write_session(body, "phone")
                if guest:
                    view["last"] = None
                return remote_send(handler, 201, {"session": view})
            if who_watching()[0] == GUEST:
                raise PermissionError("guest is read-only")
            event = interview_answer(body) if isinstance(body, dict) and body.get("row") == rid('interview') else write_feedback(body)
            return remote_send(handler, 201, {"event": event})
        except PermissionError:
            return remote_send(handler, 403, {"error": "Guest is read-only."})
        except TimeoutError:
            return remote_send(handler, 408, {"error": "Feedback timed out. Try again."})
        except (ValueError, UnicodeError, RecursionError):
            return remote_send(handler, 400, {"error": "Invalid feedback."})
        except OSError:
            return remote_send(handler, 503, {"error": "Could not save. Try again."})
    if route == "":
        return remote_send(handler, 200, REMOTE_HTML, "text/html; charset=utf-8")
    if route == "app.js":
        return remote_send(handler, 200, REMOTE_JS, "text/javascript; charset=utf-8")
    if route == "style.css":
        return remote_send(handler, 200, REMOTE_CSS, "text/css; charset=utf-8")
    if route == "feedback.json":
        if who_watching()[0] == GUEST:
            return remote_send(handler, 403, {"error": "Guest is read-only."})
        return remote_send(handler, 200, remote_history())
    if route == "session.json":
        return remote_send(handler, 200, session_view())
    query = urllib.parse.parse_qs(url.query)
    lang = query.get("lang", [LANG])[0]
    if not LANG_RE.fullmatch(lang):
        return remote_send(handler, 400, {"error": "Invalid language."})
    if route == "rows.json":
        if who_watching()[0] == GUEST:
            view = guest_rows_view(lang)
            return remote_send(handler, 200, {"rows": [{"id": row["id"], "name": row["name"], "metas": [
                {"id": item["key"], "type": "movie" if item["kind"] == "movie" else "series",
                 "name": item["title"], "releaseInfo": item["year"], "poster": item["poster"]} for item in row["items"]]}
                for row in view["rows"]]})
        return remote_send(handler, 200, remote_rows(lang))
    if route == "search.json":
        if who_watching()[0] == GUEST:
            return remote_send(handler, 403, {"error": "Guest is read-only."})
        q = query.get("q", [""])[0].strip()
        if not 2 <= len(q) <= 100:
            return remote_send(handler, 400, {"error": "Enter a title (2–100 characters)."})
        try:
            metas = search("movie", q, lang, title_only=True)["metas"] \
                + search("series", q, lang, title_only=True)["metas"]
            return remote_send(handler, 200, {"metas": metas[:30]})
        except Exception:
            return remote_send(handler, 502, {"error": "Search is unavailable. Try again."})
    return remote_send(handler, 404)


REMOTE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Skald · Your taste</title><link rel="stylesheet" href="style.css"><script src="app.js" defer></script></head>
<body><header class="top"><a class="brand" href="./" aria-label="Skald home">SKALD<span>TASTE REMOTE</span></a>
<div class="watching"><span id="watch-label">Who's watching?</span><div class="switch" role="group" aria-labelledby="watch-label">
{who_buttons}</div><button id="end-session" hidden>End</button></div><div id="last-session" class="session-fix" hidden></div></header>
<main><section id="browse"><div class="intro"><p class="eyebrow">YOUR EVENING, YOUR WAY</p><h1>Tonight starts<br>with you.</h1><p>A little more of what you love.</p></div>
<div id="hero"></div><section class="favourite"><div><p class="eyebrow">MAKE IT PERSONAL</p><h2>Add a favourite</h2><p>The films and series that feel like you.</p></div>
<form id="search-form" role="search"><label class="sr" for="query">Search films and series by title</label><input id="query" type="search" placeholder="Find a film or series" minlength="2" maxlength="100" required autocomplete="off"><button type="submit" aria-label="Search titles">Search</button></form>
<p id="search-status" role="status"></p><div id="results" class="strip"></div></section>
<div id="rows" aria-busy="true"><p class="loading">Finding your evening…</p></div></section>
<section id="taste" hidden><div class="intro"><p class="eyebrow">A TASTE THAT'S YOURS</p><h1>Your taste.</h1><p>Every opinion makes the next pick better.</p></div><div id="history"><p class="loading">Loading your feedback…</p></div></section></main>
<nav class="tabs" aria-label="Main navigation"><button data-tab="browse" aria-current="page"><span aria-hidden="true">▦</span>Discover</button><button data-tab="taste"><span aria-hidden="true">♡</span>Your taste</button></nav>
<dialog id="sheet" aria-labelledby="sheet-title"><div class="sheet-art"><img id="sheet-image" alt=""><button id="close" class="close" aria-label="Close title">×</button></div>
<div class="sheet-body"><p id="sheet-context" class="eyebrow"></p><h2 id="sheet-title"></h2><p id="sheet-year" class="muted"></p><p id="sheet-why" class="why"></p>
<div id="actions" class="actions"></div><section id="options" hidden><h3 id="options-title"></h3><div id="chips" class="chips"></div><button id="save-options" class="primary">Save feedback</button></section><p id="sheet-status" role="status"></p></div></dialog>
<p id="toast" class="toast" role="status" aria-live="polite" hidden></p><noscript><p class="intro">Enable JavaScript to share your taste.</p></noscript></body></html>""".replace("{who_buttons}", "".join(f'<button data-who="{who}" aria-pressed="{str(who == "both").lower()}">{html.escape(PAIR_LABEL if who == "both" else LABELS.get(who, who.title()))}</button>'
                                     for who in (*reversed(PEOPLE), "both", GUEST)))

REMOTE_CSS = """
.top,.watching{flex-wrap:wrap}.switch button,#end-session,.session-fix button{min-height:44px;min-width:44px}#end-session,.session-fix button{background:#21232a;border-radius:20px;padding:10px 14px;font-size:12px}.session-fix{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap;width:100%}
:root{color-scheme:dark;--bg:#0b0c0f;--panel:#191b20;--muted:#9fa1ac;--ink:#f5f2ee;--accent:#ed575d;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}button,input{font:inherit}button,a,input{-webkit-tap-highlight-color:transparent}button{color:inherit;cursor:pointer;border:0}button:disabled{opacity:.5;cursor:wait}button:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:4px}a{color:inherit;text-decoration:none}[hidden]{display:none!important}
.top{padding:22px 22px 18px;background:rgba(11,12,15,.96);border-bottom:1px solid #22242a;position:sticky;top:0;z-index:2}.brand{font-size:23px;letter-spacing:.22em;font-weight:800;display:flex;align-items:center;gap:16px}.brand span{font-size:9px;letter-spacing:.18em;font-weight:600;color:var(--muted)}.watching{margin-top:24px;display:flex;align-items:center;justify-content:space-between;gap:12px}.watching>span{font-size:12px;color:var(--muted)}.switch{background:#21232a;border-radius:24px;padding:4px;display:flex;gap:2px}.switch button{background:transparent;border-radius:20px;padding:9px 15px;font-size:12px;font-weight:600}.switch button[aria-pressed=true]{background:var(--ink);color:#111318}
main{max-width:1100px;margin:auto;padding-bottom:110px}.intro{padding:34px 22px 24px}.eyebrow{font-size:10px;font-weight:700;letter-spacing:.16em;color:var(--accent);margin:0 0 14px}h1{font-size:42px;line-height:1.03;letter-spacing:-.05em;margin:0 0 16px;font-weight:750}h2{font-size:21px;letter-spacing:-.025em;margin:0 0 8px}h3{font-size:15px}.intro>p:last-child,.favourite p:not(.eyebrow){color:var(--muted);font-size:14px;line-height:1.5;margin:0}
.hero{position:relative;isolation:isolate;height:260px;margin:0 22px 30px;border:1px solid #303139;border-radius:15px;overflow:hidden;background:var(--panel);width:calc(100% - 44px);display:flex;align-items:flex-end;text-align:left;padding:24px}.hero>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:-2}.hero:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,rgba(9,10,13,.98),rgba(9,10,13,.03));z-index:-1}.hero h2{font-size:27px;margin:0 0 8px;max-width:280px}.hero .eyebrow{color:#f1b8b9}.hero .hint{font-size:12px;color:#ddd}.hero .hint:after{content:"  →";color:var(--accent)}
.favourite{margin:0 22px 34px;padding:23px 20px 0;border-radius:15px;background:linear-gradient(135deg,#22202a,#16171b);border:1px solid #2e2c34;overflow:hidden}.favourite .eyebrow{color:#c7b9e5}.favourite h2{font-size:23px}.favourite form{display:flex;margin-top:20px;padding:5px;background:#0e0f13;border:1px solid #383843;border-radius:9px;gap:5px}.favourite input{width:100%;min-width:0;background:transparent;border:0;color:var(--ink);padding:10px;font-size:14px}.favourite form button{padding:10px 14px;border-radius:6px;background:#33303e;font-size:12px;font-weight:700}.favourite #search-status{font-size:12px;margin-top:12px}.favourite .strip{margin-top:14px;padding-bottom:22px}.favourite .poster{width:105px}
.row{margin:0 0 28px}.row-heading{padding:0 22px;display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}.row-heading h2{font-size:18px;margin:0;max-width:90%}.row-heading span{color:#696d78;font-size:10px;letter-spacing:.1em}.strip{display:flex;overflow-x:auto;gap:12px;padding:0 22px 8px;scroll-snap-type:x proximity;scrollbar-width:none}.strip::-webkit-scrollbar{display:none}.poster{flex:0 0 auto;width:122px;background:transparent;padding:0;text-align:left;scroll-snap-align:start;border-radius:9px}.poster-art{aspect-ratio:2/3;width:100%;border-radius:8px;overflow:hidden;background:#21232a;position:relative;display:grid;place-items:center}.poster-art>span{color:#727583;font-size:30px}.poster img{height:100%;width:100%;object-fit:cover;position:absolute;inset:0}.poster .name{font-size:12px;line-height:1.4;margin:9px 0 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.poster .year{font-size:10px;color:var(--muted)}.empty,.loading{color:var(--muted);padding:0 22px;font-size:13px;line-height:1.7}
.tabs{position:fixed;bottom:0;left:0;right:0;display:flex;justify-content:center;background:rgba(17,18,22,.98);border-top:1px solid #2a2c33;padding:10px 20px calc(10px + env(safe-area-inset-bottom));z-index:3;gap:65px}.tabs button{background:transparent;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:10px;color:var(--muted);min-width:70px;min-height:44px}.tabs span{font-size:24px;line-height:1.2}.tabs button[aria-current=page]{color:var(--ink)}
.person{margin:0 22px 28px}.person-head{border-bottom:1px solid #282a32;padding-bottom:20px;margin-bottom:6px}.person-head h2{font-size:26px}.person-head p{color:var(--muted);font-size:13px;line-height:1.6;margin:0}.feedback{padding:18px 0;border-bottom:1px solid #23252c;display:flex;align-items:center;gap:16px}.feedback-info{flex:1;min-width:0}.feedback h3{margin:0 0 6px;font-size:14px}.feedback p{font-size:11px;line-height:1.6;color:var(--muted);margin:0}.feedback button{background:#22242b;border-radius:7px;padding:12px 15px;font-size:12px}
dialog{margin:auto auto 0;border:1px solid #34363e;border-bottom:0;border-radius:20px 20px 0 0;padding:0;width:min(100%,520px);max-height:92dvh;color:var(--ink);background:#15161b;overscroll-behavior:contain}dialog::backdrop{background:rgba(0,0,0,.78)}.sheet-art{height:190px;position:relative;background:linear-gradient(135deg,#38333c,#16171b)}.sheet-art:after{content:"";position:absolute;inset:0;background:linear-gradient(0deg,#15161b,transparent 80%);pointer-events:none}.sheet-art img{width:100%;height:100%;object-fit:cover}.close{position:absolute;top:16px;right:16px;width:40px;height:40px;border-radius:50%;background:rgba(0,0,0,.7);font-size:27px;z-index:1}.sheet-body{padding:0 24px calc(24px + env(safe-area-inset-bottom))}.sheet-body h2{font-size:29px;line-height:1.1}.muted{color:var(--muted);font-size:12px}.why{font-size:13px;color:#c1c2cc;line-height:1.7;white-space:pre-line}.actions{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:24px}.actions button,.primary{border-radius:9px;padding:16px 10px;font-size:13px;font-weight:650;background:#282a32;min-height:48px}.actions button:first-child,.primary{background:var(--ink);color:#15161b}.actions .negative{color:#ef979c;background:#362126}.actions button:last-child:nth-child(odd){grid-column:span 2}.chips{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.chips button{padding:12px 14px;border:1px solid #444650;border-radius:24px;background:transparent;font-size:12px}.chips button[aria-pressed=true]{background:var(--ink);color:#16171b;border-color:var(--ink)}.primary{width:100%;margin-top:6px}#sheet-status{font-size:12px;color:#f0aaae;line-height:1.5}.toast{position:fixed;z-index:5;bottom:100px;left:50%;transform:translateX(-50%);max-width:calc(100% - 40px);width:max-content;padding:14px 20px;background:var(--ink);color:#17181d;border-radius:10px;box-shadow:0 8px 35px #0008;font-size:13px}.sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
@media(min-width:700px){.top{padding:24px max(32px,calc((100vw - 1056px)/2));display:flex;justify-content:space-between;align-items:center}.watching{margin:0}.intro{padding-top:48px}h1{font-size:58px}.hero{height:380px}.hero h2{font-size:40px;max-width:600px}.poster{width:155px}.favourite{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding-bottom:22px}.favourite form{width:42%;margin-top:0}.favourite #search-status,.favourite .strip{width:100%}.favourite .strip:empty{display:none}dialog{margin:auto;border-radius:20px;border-bottom:1px solid #34363e}.sheet-art{height:230px}.person{max-width:720px}}
@media(prefers-reduced-motion:no-preference){button{transition:opacity .15s,background .15s}.poster:hover{opacity:.85}dialog[open]{animation:rise .18s ease-out}@keyframes rise{from{transform:translateY(25px);opacity:0}to{transform:translateY(0);opacity:1}}}
"""

REMOTE_JS = r"""
'use strict';
const $ = id => document.getElementById(id);
const make = (tag, text, cls) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; };
const people = __PEOPLE__;
const labels = {love:'Love it', like:'Like', meh:'Meh', not_for_us:'Not for us', not_tonight:'Not tonight', seen:'Already seen', skip:'Skip'};
const reasons = ['too dark','too slow','too violent','too silly','not my genre','seen enough of these'];
let who = 'both', selected = null, pending = false, toastTimer, historyData = null;
try { const saved = localStorage.getItem('skald-taste-who'); if (Object.hasOwn(people, saved)) who = saved; } catch (_) {}
function choose(person) {
  who = person;
  document.querySelectorAll('[data-who]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.who === who)));
  try { localStorage.setItem('skald-taste-who', who); } catch (_) {}
  if (selected) $('sheet-context').textContent = `FOR ${people[who].toUpperCase()} · ${selected.favourite ? 'YOUR FAVOURITES' : 'YOUR EVENING'}`;
}
document.querySelectorAll('[data-who]').forEach(b => b.addEventListener('click', () => saveSession({who:b.dataset.who})));
choose(who);
let sessionPending = false, sessionReady = false;
function sessionControls(disabled) {
  document.querySelectorAll('[data-who],#end-session,.session-fix button').forEach(b => {b.disabled = disabled;});
}
function showSession(view) {
  if (view.active) choose(view.active.who);
  $('watch-label').textContent = view.active ? `Watching · until ${new Date(view.active.until*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}` : "Who's watching?";
  $('end-session').hidden = !view.active;
  const line = $('last-session'); line.replaceChildren(); line.hidden = !view.last?.plays;
  if (view.last?.plays) {
    const last = view.last;
    line.append(make('span', `${people[last.who]} · ${last.plays} ${last.plays === 1 ? 'play':'plays'} — Wrong person?`));
    Object.keys(people).filter(person => person !== last.who).forEach(person => {
      const b = make('button', people[person]); b.type = 'button';
      b.addEventListener('click', () => saveSession({who:person, fix:last.id})); line.append(b);
    });
  }
}
async function saveSession(body) {
  if (sessionPending || !sessionReady) return;
  sessionPending = true; sessionControls(true);
  try {
    const data = await api('session.json', body); showSession(data.session);
    if (body.who !== 'off' && !body.fix) choose(body.who);
    if (body.fix) toast('Fixed. Rows update in a minute.');
    loadRows().catch(e => toast(e.message));
  } catch (error) { toast(error.message); }
  finally { sessionPending = false; sessionControls(false); }
}
$('end-session').addEventListener('click', () => saveSession({who:'off'}));
async function loadSession() {
  sessionControls(true);
  try { showSession(await api('session.json')); }
  catch (error) { toast(error.message); }
  finally { sessionReady = true; sessionControls(false); }
}
async function api(path, body, signal) {
  const r = await fetch(path, {method:body ? 'POST':'GET', headers:body ? {'Content-Type':'application/json'}:{}, body:body ? JSON.stringify(body):undefined, signal, cache:'no-store'});
  let data; try { data = await r.json(); } catch (_) { throw Error('This page is unavailable. Reopen your private link.'); }
  if (!r.ok) throw Error(data.error || 'Could not save. Try again.');
  return data;
}
function toast(text) { clearTimeout(toastTimer); $('toast').textContent = text; $('toast').hidden = false; toastTimer = setTimeout(() => {$('toast').hidden = true;}, 3500); }
function artwork(src, size) {
  if (typeof src !== 'string' || !/^https:\/\/image\.tmdb\.org\/t\/p\/[A-Za-z0-9]+\/[A-Za-z0-9_.-]+$/.test(src)) return '';
  return src.replace(/\/t\/p\/[^/]+\//, `/t/p/${size}/`);
}
function picture(src, size) {
  const img = make('img'); img.alt = ''; img.loading = 'lazy'; img.decoding = 'async'; img.src = artwork(src, size);
  img.addEventListener('error', () => img.remove(), {once:true}); return img;
}
function poster(meta, row, favourite=false) {
  const b = make('button', null, 'poster'); b.type = 'button'; b.setAttribute('aria-label', `${meta.name}, ${meta.releaseInfo || ''}, share feedback`);
  const art = make('div', null, 'poster-art'); art.append(make('span', 'O'));
  if (artwork(meta.poster, 'w342')) art.append(picture(meta.poster, 'w342'));
  b.append(art, make('p', meta.name, 'name'), make('p', [meta.releaseInfo, meta.type === 'movie' ? 'Film':'Series'].filter(Boolean).join(' · '), 'year'));
  b.addEventListener('click', () => openTitle(meta, row, favourite)); return b;
}
async function loadRows() {
  const data = await api('rows.json');
  const scroll = window.scrollY, strips = new Map([...$('rows').querySelectorAll('.strip')].map(el => [el.dataset.row, el.scrollLeft]));
  $('rows').replaceChildren(); $('hero').replaceChildren();
  let hero = null, total = 0;
  data.rows.forEach(row => {
    if (!row.metas.length) return;
    total += row.metas.length; hero ||= {meta:row.metas[0], row:row.id};
    const section = make('section', null, 'row'), heading = make('div', null, 'row-heading');
    heading.append(make('h2', row.name), make('span', 'FOR YOU'));
    const strip = make('div', null, 'strip'); strip.dataset.row = row.id; strip.setAttribute('aria-label', row.name);
    row.metas.forEach(meta => strip.append(poster(meta, row.id))); section.append(heading, strip); $('rows').append(section);
    strip.scrollLeft = strips.get(row.id) || 0;
  });
  if (hero) {
    const b = make('button', null, 'hero'), text = make('div'); b.type = 'button';
    if (artwork(hero.meta.background, 'w780')) b.append(picture(hero.meta.background, 'w780'));
    text.append(make('p', 'A LITTLE INSPIRATION', 'eyebrow'), make('h2', hero.meta.name), make('span', 'Tell us how it feels', 'hint'));
    b.append(text); b.addEventListener('click', () => openTitle(hero.meta, hero.row)); $('hero').append(b);
  }
  if (!total) $('rows').append(make('p', 'Your shelves are warming up. Add a favourite to get started.', 'empty'));
  $('rows').setAttribute('aria-busy','false');
  window.scrollTo(0, scroll);
}
function showRowsError(error) { $('rows').setAttribute('aria-busy','false'); $('rows').replaceChildren(make('p', error.message, 'empty')); }
function openTitle(meta, row, favourite=false) {
  if (pending) return;
  selected = {meta, row, favourite}; $('options').hidden = true; $('sheet-status').textContent = '';
  const src = artwork(meta.background || meta.poster, 'w780'); $('sheet-image').hidden = !src;
  if (src) $('sheet-image').src = src; else $('sheet-image').removeAttribute('src');
  $('sheet-title').textContent = meta.name;
  $('sheet-year').textContent = [meta.releaseInfo, meta.type === 'movie' ? 'Film':'Series'].filter(Boolean).join(' · ');
  $('sheet-why').textContent = who === 'guest' ? '' : meta.description || 'One for your own story.';
  choose(who); $('actions').replaceChildren();
  (who === 'guest' ? [] : row === '__INTERVIEW_ROW__' ? ['love','like','meh','not_for_us','skip'] : favourite ? ['love','not_for_us'] : ['love','like','not_for_us','not_tonight','seen']).forEach(action => {
    const b = make('button', `${action === 'love' ? '♡  ':''}${labels[action]}`, action === 'not_for_us' ? 'negative':''); b.type = 'button';
    b.addEventListener('click', () => action === 'not_for_us' || action === 'seen' ? options(action) : save(action)); $('actions').append(b);
  });
  $('sheet').showModal();
}
$('close').addEventListener('click', () => {if (!pending) $('sheet').close();});
$('sheet').addEventListener('click', e => {if (e.target === $('sheet') && !pending) {const r = $('sheet').getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) $('sheet').close();}});
$('sheet').addEventListener('cancel', e => {if (pending) e.preventDefault();});
$('sheet').addEventListener('close', () => {selected = null;});
function options(action) {
  $('options').hidden = false; $('options-title').textContent = action === 'seen' ? 'How did it feel? (optional)' : 'What missed the mark? (optional)';
  $('chips').replaceChildren(); const chosen = new Set(); let feeling = 'seen';
  const values = action === 'seen' ? ['love','like','meh'] : reasons;
  values.forEach(value => {
    const b = make('button', action === 'seen' ? labels[value] : value); b.type = 'button'; b.setAttribute('aria-pressed','false');
    b.addEventListener('click', () => {
      if (action === 'seen') {feeling = feeling === value ? 'seen' : value; $('chips').querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b && feeling === value)));}
      else {if (chosen.has(value)) chosen.delete(value); else chosen.add(value); b.setAttribute('aria-pressed', String(chosen.has(value)));}
    }); $('chips').append(b);
  });
  $('save-options').onclick = () => save(action === 'seen' ? feeling : action, [...chosen]);
  $('save-options').textContent = action === 'seen' ? 'Save as seen' : 'Save feedback';
  $('options').scrollIntoView({block:'nearest'});
}
async function save(action, reasonList=[]) {
  if (pending || !selected) return;
  const {meta, row} = selected;
  const body = {key:meta.id, kind:meta.type === 'movie' ? 'movie':'tv', title:meta.name, year:(meta.releaseInfo || '').slice(0,4), who, action, reasons:reasonList, row, undo:null};
  pending = true; $('sheet').querySelectorAll('button').forEach(b => b.disabled = true); $('sheet-status').textContent = '';
  let saved = false;
  try {
    await api('feedback.json', body); saved = true;
  } catch (error) { $('sheet-status').textContent = error.message; }
  finally {pending = false; $('sheet').querySelectorAll('button').forEach(b => b.disabled = false);}
  if (saved) {
    $('sheet').close(); toast(action === 'not_tonight' ? 'Moved aside for tonight.' : `Saved for ${people[body.who]}.`);
    void Promise.allSettled([loadRows().catch(e => toast(e.message)), loadHistory().catch(e => toast(e.message))]);
  }
}
async function loadHistory() {
  historyData = await api('feedback.json'); const scroll = window.scrollY; $('history').replaceChildren();
  __MEMBERS__.forEach(person => {
    const section = make('section', null, 'person'), head = make('div', null, 'person-head');
    head.append(make('h2', people[person]), make('p', historyData.summary[person])); section.append(head);
    const mine = historyData.events.filter(e => e.by.includes(person));
    if (!mine.length) section.append(make('p', 'No feedback yet. Start with a favourite.', 'muted'));
    mine.forEach(event => {
      const entry = make('div', null, 'feedback'), info = make('div', null, 'feedback-info');
      info.append(make('h3', event.title), make('p', `${labels[event.action]}__SHARED_NOTE__ · ${new Date(event.t*1000).toLocaleDateString(undefined,{month:'short',day:'numeric'})}`));
      if (event.reasons.length) info.append(make('p', event.reasons.join(' · ')));
      const undo = make('button', 'Undo'); undo.type = 'button'; undo.setAttribute('aria-label', `Undo ${labels[event.action]} for ${event.title}`);
      undo.addEventListener('click', async () => {
        undo.disabled = true;
        try { const {id, t, by, ...body} = event; await api('feedback.json', {...body, undo:id}); toast('Feedback undone.'); void Promise.allSettled([loadHistory().catch(e => toast(e.message)), loadRows().catch(e => toast(e.message))]); }
        catch (error) {toast(error.message); undo.disabled = false;}
      }); entry.append(info, undo); section.append(entry);
    }); $('history').append(section);
  });
  window.scrollTo(0, scroll);
}
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => {
  const tab = b.dataset.tab; $('browse').hidden = tab !== 'browse'; $('taste').hidden = tab !== 'taste';
  document.querySelectorAll('[data-tab]').forEach(x => {if(x === b) x.setAttribute('aria-current','page'); else x.removeAttribute('aria-current');});
  window.scrollTo(0,0); if (tab === 'taste') loadHistory().catch(e => toast(e.message));
}));
let searchController;
$('search-form').addEventListener('submit', async e => {
  e.preventDefault(); const q = $('query').value.trim(); if (q.length < 2) return;
  if (searchController) searchController.abort(); searchController = new AbortController();
  $('search-status').textContent = 'Searching titles…'; $('results').replaceChildren();
  try {
    const data = await api(`search.json?q=${encodeURIComponent(q)}`, null, searchController.signal);
    $('search-status').textContent = data.metas.length ? 'Pick a title. Make it yours.' : 'No titles found. Try another name.';
    data.metas.forEach(meta => $('results').append(poster(meta, null, true)));
  } catch (error) {if (error.name !== 'AbortError') $('search-status').textContent = error.message;}
});
loadSession().then(() => loadRows().catch(showRowsError));
loadHistory().catch(e => {$('history').replaceChildren(make('p', e.message, 'empty'));});
""".replace("__INTERVIEW_ROW__", rid("interview")).replace("__PEOPLE__", json.dumps({**{p: LABELS[p] for p in PEOPLE}, "both": PAIR_LABEL, GUEST: "Guest"})).replace("__MEMBERS__", "[" + ",".join(repr(p) for p in reversed(PEOPLE)) + "]").replace("__SHARED_NOTE__",
    "${event.by.length === " + str(len(PEOPLE)) + " ? '" + json.dumps(" · " + PAIR_LABEL, ensure_ascii=False)[1:-1].replace("'", "\\'") + "':''}")


def loopback(handler):
    return handler.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


_addon_lock = threading.Lock()


def addon_token(*, rotate=False):
    path = os.path.join(DATA, "addon.json")
    with _addon_lock:
        if not rotate:
            try:
                with open(path) as fh:
                    stored = json.load(fh)
                token = stored.get("token") if isinstance(stored, dict) else None
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                    raise ValueError("invalid addon capability")
                return token
            except FileNotFoundError:
                pass
        token = secrets.token_urlsafe(32)
        if rotate:
            fd, temp = tempfile.mkstemp(prefix=".addon-", dir=DATA)
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump({"token": token}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            if rotate:
                os.replace(temp, path)
        finally:
            if rotate and os.path.exists(temp):
                os.unlink(temp)
        return token


def addon_urls(base=None, *, rotate=False):
    require_config(CONFIG_SOURCE, CONFIG_ERROR)
    base = (base or f"http://localhost:{PORT}").rstrip("/")
    url = urllib.parse.urlsplit(base)
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment or url.username or url.password:
        raise ValueError("base must be an HTTP(S) URL without credentials, query or fragment")
    root = f"{base}/{addon_token(rotate=rotate)}/{LANG}"
    return [root + "/manifest.json", *(f"{root}/p/{scope}/manifest.json" for scope in SCOPES)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def private_request(self):
        return not CONFIG["addon"]["legacy_open"] or addon_token().encode("ascii").lower() \
            in urllib.parse.unquote_to_bytes(self.raw_requestline).lower()

    def send_error(self, code, message=None, explain=None):
        if self.private_request():
            self.close_connection = True
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            return self.send_json(code, {"error": "request failed"})
        return super().send_error(code, message, explain)

    def send_json(self, code, body, max_age=0, cors=False):
        raw = re.sub(re.escape(addon_token().encode("ascii")), b"[redacted]",
                     json.dumps(body, ensure_ascii=False).encode(), flags=re.IGNORECASE)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if code >= 400 and self.private_request():
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Referrer-Policy", "no-referrer")
        elif max_age or not CONFIG["addon"]["legacy_open"]:
            policy = "public" if CONFIG["addon"]["legacy_open"] else "private"
            self.send_header("Cache-Control", f"{policy}, max-age={max_age}")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        self.route_path = urllib.parse.urlsplit(self.path).path
        parts = self.route_path.strip("/").split("/")
        if parts == ["health"]:
            return self.send_json(200, {"ok": True})
        legacy = CONFIG["addon"]["legacy_open"]
        if LANG_RE.match(parts[0]) or parts[0] in ("stats", "why"):
            if legacy:
                return self.route(parts, loopback(self))
        else:
            candidate = parts[0].encode("utf-8")
            token = addon_token().encode("ascii")
            valid = hmac.compare_digest(candidate if len(candidate) == len(token) else b"\0" * len(token), token)
            if valid and len(candidate) == len(token) and safe_path(self, ("/",), addon=True):
                self.route_path = self.route_path[len(parts[0]) + 1:]
                return self.route(parts[1:], True)
        return self.send_json(404, {"error": "not found"})

    def route(self, parts, admin):
        if parts == ["health"]:
            return self.send_json(200, {"ok": True})
        if parts == ["stats"] and admin:
            return self.send_json(200, stats())
        if len(parts) == 2 and parts[0] == "why" and parts[1].endswith(".json") and admin:
            return self.send_json(200, explain(urllib.parse.unquote(parts[1][:-5])))
        if not parts or not LANG_RE.match(parts[0]):
            return self.send_json(404, {"error": "not found"})
        lang = parts[0]
        scope = None
        if parts[1:2] == ["p"]:
            if len(parts) < 4 or parts[2] not in SCOPES or self.route_path != "/" + "/".join(parts):
                return self.send_json(404, {"error": "not found"})
            scope, parts = parts[2], [lang, *parts[3:]]
        with addon_scope(scope):
            return self.addon_get(parts, lang, scope)

    def addon_get(self, parts, lang, scope):
        if parts[1:] == ["manifest.json"]:
            return self.send_json(200, manifest(lang, scope), 3600, cors=True)
        if len(parts) == 4 and parts[1] == "catalog" and parts[3].endswith(".json") \
                and (parts[3][:-5], parts[2]) in {(cid, t) for cid, typs, _ in feed_rows(lang) for t in typs}:
            _langs.add(lang)
            metas = row_metas(parts[3][:-5], parts[2], lang, who=SCOPES[scope]) if scope is not None else row_metas(parts[3][:-5], parts[2], lang)
            if CONNECTOR_CATALOG_DEMAND: connectors_demand(time.time())
            note_served(learn_id(parts[3][:-5], lang, metas), metas)
            return self.send_json(200, {"metas": metas}, 60 if parts[3][:-5] in SESSION_ROWS else 900, cors=True)
        if len(parts) == 5 and parts[1] == "catalog" and parts[2] in ("movie", "series") \
                and parts[3] == rid('search') and parts[4].endswith(".json"):
            extras = urllib.parse.parse_qs(parts[4][:-5])
            query = (extras.get("search") or [""])[0].strip()
            if not query or addon_token().lower() in query.lower():
                return self.send_json(200, {"metas": []}, cors=True)
            log_search(query)
            try:
                return self.send_json(200, search(parts[2], query, lang), 600, cors=True)
            except Exception as e:
                return self.send_json(502, {"metas": [], "error": type(e).__name__}, cors=True)
        return self.send_json(404, {"error": "not found"})


if __name__ == "__main__":
    import argparse
    from contextlib import ExitStack
    cli = argparse.ArgumentParser(allow_abbrev=False)
    if CONFIG_ERROR:
        cli.exit(2, str(CONFIG_ERROR) + "\n")
    cli.add_argument("--print-config", action="store_true")
    action = cli.add_mutually_exclusive_group()
    action.add_argument("--new-client")
    action.add_argument("--revoke-client")
    action.add_argument("--addon-url", action="store_true")
    action.add_argument("--rotate-addon-token", action="store_true")
    cli.add_argument("--base")
    cli.add_argument("--scopes", default="read,decide")
    cli.add_argument("--person", choices=(*PEOPLE, GUEST))
    args, unknown = cli.parse_known_args()
    try:
        if args.print_config or args.new_client or args.revoke_client or args.addon_url or args.rotate_addon_token:
            require_config(CONFIG_SOURCE)
    except ValueError as e:
        cli.exit(2, str(e) + "\n")
    if args.print_config:
        print(json.dumps(public_config(CONFIG), ensure_ascii=False, indent=1))
        raise SystemExit(0)
    if args.addon_url or args.rotate_addon_token:
        try:
            print("\n".join(addon_urls(args.base, rotate=args.rotate_addon_token)))
        except (OSError, ValueError):
            cli.exit(2, "could not create addon URLs\n")
        raise SystemExit(0)
    if unknown:
        print(f"ignoring unknown args: {len(unknown)}", file=sys.stderr, flush=True)
    if args.new_client or args.revoke_client:
        try:
            if args.new_client:
                print(new_client(args.new_client, args.scopes.split(","), args.person))
            else:
                revoke_client(args.revoke_client)
        except (OSError, ValueError):
            cli.exit(2, "could not update clients\n")
        raise SystemExit(0)
    if API_PORT == PORT:
        cli.exit(2, "API_PORT must differ from PORT\n")
    try:
        require_config(CONFIG_SOURCE)
    except ValueError as e:
        cli.exit(2, str(e) + "\n")
    if not API_KEY:
        cli.exit(2, "missing TMDB_API\n")
    addon_token()  # Create private capabilities before accepting requests.
    remote_token()
    load_state()
    with ExitStack() as stack:
        main = stack.enter_context(ThreadingHTTPServer(("0.0.0.0", PORT), Handler))
        remote = stack.enter_context(ThreadingHTTPServer(("0.0.0.0", REMOTE_PORT), RemoteHandler))
        if API_PORT is not None and API_PORT != REMOTE_PORT:
            api = stack.enter_context(ThreadingHTTPServer((os.environ.get("API_BIND") or "0.0.0.0", API_PORT), ApiHandler))
            threading.Thread(target=api.serve_forever, daemon=True).start()
        from skald.setup import record_startup
        record_startup(DATA, CONFIG)
        threading.Thread(target=feed_worker, daemon=True).start()
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        main.serve_forever()
