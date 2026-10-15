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
import fcntl
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
import statistics
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
from functools import lru_cache, partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from types import SimpleNamespace

# --- household config (also loaded alone by skald/members.py) ---
DATA = os.environ.get("DATA_DIR", "/data")
VERSION = "1.7.0"
CONFIG_NAME = "skald.json"
DEFAULT_ROW_PREFIX = "skald"
RESERVED_DATA_FILES = ("addon.json", "skald.json", "legacy.json", "clients.json", "remote.json", ".skald-household", ".skald-format")
ENV_FIRST_RUN = True
MEMBER_COUNTS = (1, 2, 3, 4, 5, 6)
FIRST_RUN_MEMBER_ROWS = False
HIDE_CHOICES = ("movies", "series", "documentaries", "animation", "anime")
TMDB_GENRES = {
    "movie": {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
              18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
              9648: "Mystery", 10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller",
              10752: "War", 37: "Western"},
    "series": {10759: "Action & Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
               18: "Drama", 10751: "Family", 10762: "Kids", 9648: "Mystery", 10763: "News", 10764: "Reality",
               10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics", 37: "Western"},
}
LANG_PATTERN = r"[a-z]{2}-(?:[A-Z]{2}|[A-Z][a-z]{3}(?:-[A-Z]{2})?)"
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
        "experiences": {},
        "weekpicks_added": True
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
        self.key, self.missing = key, missing
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


ROW_KEYS = ('episodes', 'foryou', 'week', 'weekpicks', 'upcoming', 'catchup', 'rewatch', 'ready', 'because', 'leap',
            'top10', 'people', 'moods', 'lanes', 'docs', 'studios', 'anime', 'shelves', 'digital', 'toprated', 'family',
            'newtoyou', 'lab')


def fresh_config(members):
    return {"v": 1, "row_prefix": DEFAULT_ROW_PREFIX, "household": {"members": members,
            "labels": {**{p: p.title() for p in members}, "pair": "you both" if len(members) == 2 else "everyone"},
            "note": "", "archived": {}, "trakt_member": members[0] if members else "",
            "member_rows": list(members) if FIRST_RUN_MEMBER_ROWS else [], "experiences": {}, "weekpicks_added": True},
            "taste": {"household": {"likes": "", "rewatch_collections": []}, "people": {p: {} for p in members}},
            "region": dict(DEFAULT_REGION), "addon": dict(DEFAULT_ADDON),
            "history": {"source": "none"}, "connectors": {}}


def validate_addon_config(cfg):
    if type(cfg["addon"]["legacy_open"]) is not bool:
        raise ConfigError("addon.legacy_open")


def validate_history_connectors(cfg, need, data=None, environ=None):
    history, connectors = cfg["history"], cfg["connectors"]
    need(isinstance(history, dict) and "source" in history and
         set(history) <= {"source", "keep_raw_days", "max_raw_items"}, "history")
    for field in ("keep_raw_days", "max_raw_items"):
        value = history.get(field)
        need(value is None or type(value) is int and value >= 0, "history." + field)
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
        need(isinstance(c, dict) and set(c) <= {"kind", "enabled", "auth", "options", "accounts", "keep_raw"}, key)
        need("keep_raw" not in c or type(c["keep_raw"]) is bool, key + ".keep_raw")
        need("accounts" not in c or cid in ("trakt_api", "mdblist", "stremio", "pmdb"), key + ".accounts")
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
        elif cid in ("trakt_api", "mdblist", "stremio", "pmdb"):
            need(c["kind"] == "api", key)
            if "accounts" in c:
                accounts = c["accounts"]
                need(isinstance(accounts, dict) and bool(accounts), key + ".accounts")
                need(all(person in cfg["household"]["members"] for person in accounts), key + ".accounts")
                need(all(reference(value) and value.startswith("file:") for value in accounts.values()), key + ".accounts")
                need(c.get("auth") is None and len(set(accounts.values())) == len(accounts), key + ".accounts")
            else:
                need(cid == "trakt_api" and reference(c.get("auth")), key)
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
    need(isinstance(members, list) and bool(members), "household.members")
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
    value = hh.get('ai_monthly_usd', 3)
    need(type(value) in (int, float) and 0 <= value < float('inf'), 'household.ai_monthly_usd')
    need(type(hh.get('rows_from_default', False)) is bool, 'household.rows_from_default')
    guests = hh.get('guests', [])
    need(isinstance(guests, list) and all(isinstance(p, str) and p in members for p in guests) and
         len(set(guests)) == len(guests), 'household.guests')
    from datetime import date
    ends = hh.get('guest_until', {})
    try:
        need(isinstance(ends, dict) and all(p in guests and isinstance(value, str) and date.fromisoformat(value).isoformat() == value
             for p, value in ends.items()), 'household.guest_until')
    except (ValueError, TypeError):
        raise ConfigError('household.guest_until (guest: YYYY-MM-DD)') from None
    retired_people = hh.get('retired', [])
    need(isinstance(retired_people, list) and all(p in members for p in retired_people)
         and len(set(retired_people)) == len(retired_people) and len(retired_people) < len(members), 'household.retired')
    away = hh.get('away', [])
    need(isinstance(away, list) and all(p in members and p not in guests and p not in retired_people for p in away)
         and len(set(away)) == len(away), 'household.away')
    ends = hh.get('away_until', {})
    try:
        need(isinstance(ends, dict) and all(p in away and isinstance(value, str) and date.fromisoformat(value).isoformat() == value
             for p, value in ends.items()), 'household.away_until')
    except (ValueError, TypeError):
        raise ConfigError('household.away_until (person: YYYY-MM-DD)') from None
    need(len(members) - len(retired_people) in MEMBER_COUNTS,
         'household.members: active profile limit is '+str(max(MEMBER_COUNTS)))
    ages = hh.get('ages', {})
    need(isinstance(ages, dict) and all(p in members and type(age) is int and 3 <= age <= 120
         for p, age in ages.items()), 'household.ages (3..120)')
    retired = hh.get('retired_rows', [])
    need(isinstance(retired, list) and all(isinstance(cid, str) and cid.startswith(cfg['row_prefix'] + '.')
         for cid in retired), 'household.retired_rows')
    if 'rows' in hh:
        rows = hh['rows']
        need(isinstance(rows, list) and bool(rows) and all(isinstance(k, str) and k in ROW_KEYS for k in rows),
             'household.rows: choose a non-empty list from ' + ', '.join(ROW_KEYS))
    labels = hh.get('row_labels', {})
    need(isinstance(labels, dict) and all(k in ROW_KEYS and isinstance(v, str) and v.strip() and
         len(v) <= 100 and not any(ord(c) < 32 for c in v) for k, v in labels.items()), 'household.row_labels')
    order = hh.get('row_order', [])
    need(isinstance(order, list) and all(isinstance(k, str) and k in ROW_KEYS for k in order) and
         len(set(order)) == len(order), 'household.row_order')
    need(type(hh.get('weekpicks_added', False)) is bool, 'household.weekpicks_added')
    if not doc['household'].get('weekpicks_added', False) and 'row_order' in hh and 'weekpicks' not in order:
        values = list(order)
        values.insert(values.index('week') + 1 if 'week' in values else 0, 'weekpicks')
        hh['row_order'] = values
    hh['weekpicks_added'] = True
    if 'home' in hh:
        from skald.rowplan import validate_home
        try:
            validate_home(hh['home'])
        except ValueError as e:
            raise ConfigError(str(e)) from None
    need(type(hh.get("shelves", 2)) is int and 0 <= hh.get("shelves", 2) <= 10, "household.shelves (0..10)")
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
        prefs = traits.get("prefs", {})
        key = "taste.people." + p + ".prefs"
        need(isinstance(prefs, dict) and set(prefs) <= {"discovery", "years", "excluded_genres", "hide", "focus", "kids_age", "shelves", "weights", "runtime"}, key)
        runtime = prefs.get('runtime', {})
        need(isinstance(runtime, dict) and set(runtime) <= {'movie', 'series'} and
             all(type(v) is int and 1 <= v <= 600 for v in runtime.values()), key + '.runtime (1..600 minutes)')
        weights = prefs.get('weights', {})
        need(isinstance(weights, dict) and set(weights) <= {'genre', 'movie', 'series'}, key + '.weights')
        for kind in ('movie', 'series'):
            need(type(weights.get(kind, 0)) is int and -1 <= weights.get(kind, 0) <= 1, key + '.weights.' + kind)
        genres = weights.get('genre', {})
        genre_names = {name.casefold(): name for names in TMDB_GENRES.values() for name in names.values()}
        need(isinstance(genres, dict) and all(isinstance(g, str) and g.casefold() in genre_names and
             type(v) is int and -1 <= v <= 1 for g, v in genres.items()), key + '.weights.genre')
        if 'genre' in weights:
            weights['genre'] = {genre_names[g.casefold()]: v for g, v in genres.items()}
        need(isinstance(prefs.get("discovery", "balanced"), str) and
             prefs.get("discovery", "balanced") in ("balanced", "mainstream", "gems"), key + ".discovery")
        need(type(prefs.get("shelves", 4)) is int and 0 <= prefs.get("shelves", 4) <= 10, key + ".shelves (0..10)")
        if "kids_age" in prefs:
            need(type(prefs["kids_age"]) is int and 3 <= prefs["kids_age"] <= 17, key + ".kids_age (3..17)")
        years = prefs.get("years", [None, None])
        need(isinstance(years, list) and len(years) == 2 and
             all(y is None or type(y) is int and 1 <= y <= 9999 for y in years), key + ".years")
        need(years[0] is None or years[1] is None or years[0] <= years[1], key + ".years")
        hides = prefs.get("hide", [])
        need(isinstance(hides, list) and all(isinstance(h, str) and h in HIDE_CHOICES for h in hides),
             key + ".hide (" + ", ".join(HIDE_CHOICES) + ")")
        if "hide" in prefs:
            prefs["hide"] = list(dict.fromkeys(hides))
        focus = prefs.get("focus", {})
        need(isinstance(focus, dict) and set(focus) <= {"genres", "languages", "countries", "people"}, key + ".focus")
        names = focus.get('people', [])
        need(isinstance(names, list) and all(isinstance(n, str) and n.strip() and len(n) <= 100 and
             not any(ord(c) < 32 for c in n) for n in names), key + '.focus.people')
        for field, transform in (("languages", str.lower), ("countries", str.upper)):
            values = focus.get(field, [])
            need(isinstance(values, list) and all(isinstance(v, str) and re.fullmatch(r"[a-zA-Z]{2}", v) for v in values),
                 key + ".focus." + field + " (two-letter codes)")
            if field in focus:
                focus[field] = list(dict.fromkeys(transform(v) for v in values))
        for field, genres in (("excluded_genres", prefs.get("excluded_genres", {})),
                              ("focus.genres", focus.get("genres", {}))):
            need(isinstance(genres, dict) and set(genres) <= set(TMDB_GENRES), key + "." + field)
            for kind, values in genres.items():
                supported = TMDB_GENRES.values() if field == 'excluded_genres' else (TMDB_GENRES[kind],)
                names = {name.casefold(): name for table in supported for name in table.values()}
                need(isinstance(values, list) and all(isinstance(g, str) and g.casefold() in names for g in values),
                     key + "." + field + "." + kind + " (TMDB genre names)")
                genres[kind] = list(dict.fromkeys(names[g.casefold()] for g in values))
    for p in archived:
        need(isinstance(taste[p].get("likes", ""), str), "taste." + p + ".likes")
        need(type(taste[p].get("archived", True)) is bool, "taste." + p + ".archived")
    region = cfg["region"]
    need(isinstance(region["lang"], str) and re.fullmatch(LANG_PATTERN, region["lang"]), "region.lang")
    if 'row_lang' in region:
        from skald.i18n import _chain, _STRINGS
        value = region['row_lang']
        need(isinstance(value, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]{4})?(?:-[a-z]{2})?', value, re.I) and
             (value.lower().split('-')[0] == 'en' or set(_chain(value)) & (set(_STRINGS) - {'en'})),
             'region.row_lang: choose a bundled locale (en, de, sr-latn, sr-cyrl)')
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
    from skald.plays import validate as validate_plays
    validate_plays(cfg, need)
    ai_prefs = cfg.get('ai', {})
    need(isinstance(ai_prefs, dict) and set(ai_prefs) <= {'credit_warn_fraction', 'credit_warn_usd'}, 'ai')
    for field, default, maximum in (('credit_warn_fraction', .2, 1), ('credit_warn_usd', 1, float('inf'))):
        value = ai_prefs.get(field, default)
        need(type(value) in (int, float) and 0 <= value <= maximum and value < float('inf'), 'ai.' + field)
    metrics_prefs = cfg.get("metrics", {})
    need(isinstance(metrics_prefs, dict) and set(metrics_prefs) <= {
        "enabled", "attribution_days", "shown_retention_days", "shown_max_items"}, "metrics")
    need(type(metrics_prefs.get("enabled", False)) is bool, "metrics.enabled")
    for field, default, maximum in (("attribution_days", 14, 365), ("shown_retention_days", 30, 365),
                                    ("shown_max_items", 10000, 100000)):
        value = metrics_prefs.get(field, default)
        need(type(value) is int and 1 <= value <= maximum, "metrics." + field)
    lab_prefs = cfg.get('lab', {})
    need(isinstance(lab_prefs, dict) and set(lab_prefs) <= {'enabled', 'trial_showing_days', 'kept_showing_days'}, 'lab')
    need(type(lab_prefs.get('enabled', True)) is bool, 'lab.enabled')
    for field, default in (('trial_showing_days', 10), ('kept_showing_days', 30)):
        need(type(lab_prefs.get(field, default)) is int and 1 <= lab_prefs.get(field, default) <= 365, 'lab.'+field)
    return cfg


def stamp_household(data):
    from skald.erase import erased, locked
    try:
        os.makedirs(data, exist_ok=True)
        with locked(data):
            if not erased(data):
                _stamp_household(data)
    except OSError:
        raise ConfigError("skald.json") from None


def _stamp_household(data):
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


def publish_config(data, path, cfg, *, replace=False, on_publish=None, write_check=None):
    """The stamp precedes atomic publication; first start never overwrites a winner."""
    from skald.erase import locked
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(data, exist_ok=True)
    with locked(data):
        if write_check is not None and write_check(data):
            raise PermissionError('Household erased; run setup and restart Skald')
        os.makedirs(directory, exist_ok=True)
        _stamp_household(data)
        fd, temp = tempfile.mkstemp(prefix=".skald-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
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
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    if on_publish is not None:
        on_publish()
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def load_config(data=None, environ=None, *, readonly=False):
    env = os.environ if environ is None else environ
    data = data or env.get("DATA_DIR", "/data")
    if os.path.exists(os.path.join(data, '.skald-erased')) and not os.path.isfile(os.path.join(data, 'skald.json')):
        return copy.deepcopy(LEGACY), 'implicit'
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


def guest_people(cfg):
    return (*cfg['household'].get('guests', []), *cfg['household'].get('away', []))


def public_config(cfg):
    # Unknown extensions can contain credentials; print only the supported household fields.
    schema = fresh_config(cfg["household"]["members"])
    out = {"v": cfg["v"], "row_prefix": cfg.get("row_prefix", DEFAULT_ROW_PREFIX), **{k: {p: cfg[k][p] for p in schema[k]} for k in ("household", "region", "addon")}}
    out["household"] = {**out["household"], "labels": {p: cfg["household"]["labels"][p] for p in (*cfg["household"]["members"], "pair")}}
    if 'row_lang' in cfg['region']:
        out['region']['row_lang'] = cfg['region']['row_lang']
    for key in ('rows', 'rows_from_default', 'row_labels', 'row_order', 'shelves', 'home', 'retired', 'guests', 'guest_until', 'ages', 'retired_rows', 'away', 'away_until'):
        if key in cfg['household']:
            out['household'][key] = cfg['household'][key]
    out["taste"] = {p: {k: v for k, v in traits.items() if k in ("likes", "archived", "rewatch_collections")}
                    for p, traits in cfg["taste"].items() if p in ("household", *cfg["household"]["archived"])}
    out["taste"]["people"] = {p: {k: v for k, v in traits.items() if k in ("type", "loves", "prefs")}
                               for p, traits in cfg["taste"].get("people", {}).items()}
    out.update(history=cfg["history"], connectors=copy.deepcopy(cfg["connectors"]))
    for key in ("metrics", "ai", "lab", "plays"):
        if key in cfg:
            out[key] = copy.deepcopy(cfg[key])
    return out
# --- end household config ---

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skald.erase import (erased, epoch as erasure_epoch, writing as household_write,
                         writable as household_writable, locked as erasure_lock)
from skald import migrate, shelves, rowplan, row_names, metrics as row_metrics, lab
from skald.history import seen_keys as history_seen
from skald.i18n import t as _translate
_household_epoch = erasure_epoch(DATA)


def household_erased(data):
    return erased(data) or erasure_epoch(data) != _household_epoch

def row_lang():
    return CONFIG['region'].get('row_lang', LANG)

def t(key, lang=None, **fields):
    return _translate(key, lang=lang or row_lang(), **fields)

try:
    if globals().get("__name__") == "__main__" and (not sys.argv[1:] or migrate.rewrite_needed(DATA)):
        migrate.ensure(DATA)
    else:
        migrate.check(DATA)
except (ValueError, OSError) as e:
    if globals().get("__name__") == "__main__":
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None
    raise

class NotConfiguredHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if os.path.exists(os.path.join(DATA, '.skald-erased')):
            try:
                with open(os.path.join(DATA, 'addon.json')) as fh:
                    token = json.load(fh)['token']
                parts = urllib.parse.urlsplit(self.path).path.strip('/').split('/')
                if hmac.compare_digest(parts[0], token):
                    doc = ({'id': 'skald', 'version': VERSION, 'name': 'Skald', 'resources': ['catalog'],
                            'types': ['movie', 'series'], 'catalogs': []} if parts[-1] == 'manifest.json' else {'metas': []})
                    body = json.dumps(doc).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except (OSError, ValueError, KeyError):
                pass
        body = b'{"ok": false, "status": "not-configured"}'
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_for_config(data):
    def read_config():
        try:
            return load_config(data)
        except ConfigError as error:
            if not (error.key == "skald.json" and error.missing
                    and os.path.lexists(os.path.join(data, ".skald-household"))):
                raise
            return copy.deepcopy(LEGACY), "implicit"

    cfg, source = read_config()
    if source != "implicit":
        return cfg, source
    port = int(os.environ.get("PORT", "8080"))
    if os.environ.get("API_PORT") and int(os.environ["API_PORT"]) == port:
        return cfg, source  # Let the CLI report the port conflict before waiting.
    print("empty DATA: set SKALD_MEMBERS=ana,ben or provide skald.json", file=sys.stderr, flush=True)
    with ThreadingHTTPServer(("0.0.0.0", port), NotConfiguredHandler) as health:
        worker = threading.Thread(target=health.serve_forever, daemon=True)
        worker.start()
        try:
            while source == "implicit":
                time.sleep(3)
                cfg, source = read_config()
        finally:
            health.shutdown()
            worker.join()
    return cfg, source


CONFIG_ERROR = None
try:
    if __name__ == "__main__" and not any(arg.split("=", 1)[0] in ("--print-config", "--new-client", "--revoke-client",
            "--addon-url", "--rotate-addon-token", "--help", "-h") for arg in sys.argv[1:]):
        CONFIG, CONFIG_SOURCE = wait_for_config(DATA)
    else:
        CONFIG, CONFIG_SOURCE = load_config(DATA, readonly=globals().get("_CONFIG_READONLY", __name__ != "__main__" or bool(sys.argv[1:])))
except ConfigError as e:
    CONFIG, CONFIG_SOURCE, CONFIG_ERROR = copy.deepcopy(LEGACY), "invalid", e
PEOPLE = tuple(p for p in CONFIG['household']['members'] if p not in CONFIG['household'].get('retired', []))
SCOPES = {**{p: p for p in PEOPLE}, "together": "both"}
LABELS = CONFIG["household"]["labels"]
PAIR_LABEL = LABELS["pair"].removeprefix("you ").title()
ARCHIVED = CONFIG["household"]["archived"]
MEMBER_ROWS = tuple(p for p in CONFIG['household']['member_rows'] if p in PEOPLE)
ROW_OWNER = {rid(p): p for p in MEMBER_ROWS}
TRAKT_PERSON = CONFIG["household"]["trakt_member"]
DEFAULT_TASTE = CONFIG["taste"]
LANG = CONFIG["region"]["lang"]
TZ = CONFIG["region"]["tz"]
COUNTRY = CONFIG["region"]["country"]
EXPERIENCE_TAGS = {p: set(xs) for p, xs in CONFIG["household"]["experiences"].items()}
MEMBER_COUNT = ("one", "two", "three", "four", "five", "six")[len(PEOPLE) - 1]
HOUSEHOLD_KIND = "a household"
MEMBER_NAMES = " and ".join(LABELS[p] for p in PEOPLE)
LANGUAGE_NAMES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
                   "nl": "Dutch", "pt": "Portuguese", "sv": "Swedish", "da": "Danish", "nb": "Norwegian",
                   "fi": "Finnish", "pl": "Polish", "ja": "Japanese", "ko": "Korean"}
REGION_LANGUAGE = LANGUAGE_NAMES.get(LANG[:2], "local")


GUEST = "guest"
PCT_MIN_EXPLICIT = 20
PRIOR_FULL = 40
HOUSEHOLD_PRIOR = True


def who_ok(who, *extra):
    return who in (*PEOPLE, *extra)


API_KEY = os.environ.get("TMDB_API", "")
PORT = int(os.environ.get("PORT", "8080"))
IMG = "https://image.tmdb.org/t/p/"
LANG_RE = re.compile("^(?:" + LANG_PATTERN + ")$")
CACHE_TTL = 600
CACHE_MAX = 4000
_cache = {}
_upstream = threading.local()
_upstream_lock = threading.Lock()


class BuildPool(ThreadPoolExecutor):
    # Builders swallow leaf errors; carry their failure counter through nested pool work.
    def submit(self, fn, /, *args, **kwargs):
        failures = getattr(_upstream, "failures", None)
        deadline = getattr(_upstream, "deadline", None)
        if failures is None and deadline is None:
            return super().submit(fn, *args, **kwargs)
        def run():
            previous = getattr(_upstream, "failures", None)
            previous_deadline = getattr(_upstream, "deadline", None)
            _upstream.failures = failures
            _upstream.deadline = deadline
            try:
                return fn(*args, **kwargs)
            finally:
                _upstream.failures = previous
                _upstream.deadline = previous_deadline
        return super().submit(run)


def upstream_failed():
    failures = getattr(_upstream, "failures", None)
    if failures is not None:
        with _upstream_lock:
            failures[0] += 1


def upstream_timeout(timeout):
    deadline = getattr(_upstream, "deadline", None)
    timeout = min(timeout, deadline - time.monotonic()) if deadline is not None else timeout
    if timeout <= 0:
        raise TimeoutError()
    return timeout


@contextmanager
def search_deadline(deadline):
    previous = getattr(_upstream, "deadline", None)
    _upstream.deadline = deadline
    try:
        yield
    finally:
        _upstream.deadline = previous


_pool = BuildPool(max_workers=32)  # leaf TMDB calls only
_outer = BuildPool(max_workers=16)  # jobs waiting on _pool leaf calls
_search_pool = BuildPool(max_workers=8)  # Stream resolutions must leave routing and title jobs free.
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MARK = "✨ "
AI_MIN_LEN = 4
SEARCH_SETTLE = 0.35  # seconds a non-title query waits for the next keystroke before spending an AI call
AI_SEARCH_TTL = 24 * 60 * 60
SEARCH_HISTORY_LIMIT = 1200  # Prompt only; watched filtering uses the complete scoped history.
SEARCH_WIDE_SIZE = 30
SEARCH_WIDE_TIMEOUT = 12
SEARCH_TIMEOUT = 7
SEARCH_SERVE_AFTER = 2.7
SEARCH_SERVE_MIN = 15
SEARCH_HEAD_START = .5  # Title check overlaps settle; leave time after the pickers for unpaid fallback.
SEARCH_ORDER = "parallel"
SEARCH_CHANNELS = {"title": "tmdb", "command": "command", "request": "picker", "discover": "picker"}
ROUTE_MODEL = os.environ.get("SKALD_ROUTE_MODEL") or "perplexity/pplx-decider-v1.1-27b"
LUNA_DECIDER_MODEL = "openai/gpt-6-luna-decisions"
PICK_MODEL = "openai/gpt-6-luna"
COMMAND_MODEL = os.environ.get("SKALD_COMMAND_MODEL") or "~google/gemini-flash-latest"
COMMAND_EFFORT = os.environ.get("SKALD_COMMAND_EFFORT") or "low"
COMMAND_FALLBACK_MODEL = os.environ.get("SKALD_COMMAND_FALLBACK_MODEL") or "z-ai/glm-5.3-flash"
COMMAND_FALLBACK_EFFORT = os.environ.get("SKALD_COMMAND_FALLBACK_EFFORT") or "low"
ROUTE_Q = {
    "route": {"type": "choice",
        "instructions": "Someone typed this into the search box of a TV app. Decide what kind of answer they want. The text may be in any language or script, with typos, odd spacing or missing apostrophes. Judge the whole text by meaning; no single word decides.",
        "criteria": {
            "title": "The whole text is a name: one movie, series, person or franchise they want to find, possibly misspelled, partial or translated. If any part of the text is not part of that name (a content type, a mood, a comparison, a year, a wish, a verb, a reference to the user), it is not a title. A lone word is a name, even if it could also describe a mood.",
            "request": "The text asks to be shown titles now by describing them rather than naming one: a mood, theme, subject, feeling, genre with a feeling, similarity to something, a ranking, or the user's own viewing history (titles they have or have not watched, or want to watch again). A descriptive word together with a content type, in any language, is a request unless the whole text is itself a known title. It changes nothing saved.",
            "discover": "The text is only a genre and/or a year or decade, with no mood, theme or other wish.",
            "command": "The text asks the app to change or remember something for later: the user's taste, filters, settings, members, or their rows, shelves and catalogs; or it tells the app how they felt about a specific title or that they already saw it; or it asks what the app has learned about a person, when a named household guest's stay ends, or how much AI credit or spending remains. Questions about a named child's watched titles, likes, watching days, usual watching weekdays or when the child is around are commands that read facts. Title names like When Kim Is Around stay title searches. Questions about preparing for a coming saga film or season by watching earlier parts or catching up read the saved get-ready rows and are commands. A title called Get Ready is a title search. Asking to be shown titles from their own history is a request, not a command.",
        }},
    "watched": {"type": "choice",
        "instructions": "Does the user limit results by their own viewing history? Only count wording about the user's (or their household's) own watching. Words that belong to a title name or describe a plot do not count.",
        "criteria": {
            "unseen": "They want only titles they have not watched yet.",
            "seen": "They want only titles they already watched, including wanting to watch something again.",
            "any": "No limit by their history. This is the answer whenever the text does not clearly say unseen or seen.",
        }},
    "list": {"type": "choice",
        "instructions": "Should results follow what is widely regarded as the best, or this household's taste?",
        "criteria": {
            "best": "The user asks for acclaim or rank by broad consensus: the best, greatest, classics, must-see, award-winning, top, in any language or spelling. This holds when combined with other wishes.",
            "taste": "Everything else, including names of titles that contain such words.",
        }},
}
ROUTE_FOLLOWUP_Q = {**ROUTE_Q, "route": {**ROUTE_Q["route"], "instructions": ROUTE_Q["route"]["instructions"] + " When the text builds on the last search, judge what the combined wish asks for."},
    "previous": {"type": "choice",
        "instructions": "The state may show the user's last search and a few of its results, then what they typed now. How does the new text relate to that last search?",
        "criteria": {
            "new": "A fresh search or instruction that stands on its own; it would mean the same with no last search.",
            "refine": "It adjusts the last search: asks for more of it, adds to it, narrows or shifts it, points at one of its results, or keeps it but changes a quality. It only makes sense together with the last search.",
            "act": "It asks to do something lasting with the shown results themselves: keep them as taste, save them as a row or shelf, or hide them.",
        }}}
PICK_SYS = ("You pick movies and series for one household's TV search. Reply with 30 lines and nothing else, one title per line: Title|Year|m or s (m=movie, s=series). No numbering, no commentary. "
            "Use the title as it is known on TMDB (English or original) and the year of first release. "
            "Match the request literally first: mood, theme, genre, era, country and type. 'movies' or 'films' means movies only; 'shows' or 'series' means series only; otherwise give at least 12 movies and 12 series. Anime means Japanese animation. "
            "For best-of or classics requests use broad critical consensus across decades and countries and ignore the household taste. Otherwise use the household taste only to choose between equally good matches. "
            "Avoid more than one entry per franchise unless asked. Mix famous picks with lesser-known titles a fan of the request would respect. "
            "Viewing history follows the request: if it wants titles new to the viewer, never pick a history title; if it asks for titles they watched, pick only history titles, copied exactly, and return fewer lines if needed; otherwise history titles are allowed but new ones come first.")
_ai = {}
_ai_lock = threading.RLock()
LAST_SEARCH_TTL = 30 * 60
_last_search = {}
_search_turns = {}


def search_scope():
    scope = getattr(_scoping, "scope", None)
    return scope, None if scope is not None else _viewer.get() or who_watching()[0]


def last_search():
    with _ai_lock:
        now = time.time()
        for key, value in list(_last_search.items()):
            if now - value["at"] >= LAST_SEARCH_TTL:
                del _last_search[key]
        for key, (expires, _) in list(_ai.items()):
            if expires <= now:
                del _ai[key]
        return copy.deepcopy(_last_search.get(search_scope()))


def search_key(query):
    return " ".join("".join(c for c in query.casefold() if not unicodedata.category(c).startswith("P")).split())


def start_search(query):
    scope, key = search_scope(), search_key(query)
    # Turn registration and command previews must use the same lock.
    with search_commands._LOCK, _ai_lock:
        previous, turn = _search_turns.get(scope, (None, 0))
        if previous != key:
            turn += 1
        _search_turns[scope] = (key, turn)
        if previous != key:
            for cache_key, (_, plan) in list(_ai.items()):
                if cache_key[1] != scope or cache_key[0] == key:
                    continue
                picks = getattr(plan, "search_picks", None)
                if plan.done() and not plan.cancelled() and plan.exception() is None:
                    picks = plan.result().get("picks")
                    if picks is None or picks.done():
                        continue
                stop = getattr(plan, "search_stop", None)
                if stop is not None:
                    stop.set()
                cancel_search_picks(picks)
                plan.cancel()
                del _ai[cache_key]
    return scope, turn


def shown_titles(context):
    return list({(x["key"], x["kind"]): x for row in context["rows"].values() for x in row}.values())


def search_turn(query):
    context = last_search()
    # Movie/series requests and retries for the same text belong to the same turn.
    if context and search_key(context["query"]) == search_key(query):
        context = context.get("prior")
    return {k: v for k, v in context.items() if k != "prior"} if context and time.time() - context["at"] < LAST_SEARCH_TTL else None


def remember_search(query, stype, metas, answer, prior, wish, request):
    titles = [{"key": x["id"], "kind": "movie" if x["type"] == "movie" else "tv",
               "title": x["name"].removeprefix(AI_MARK), "year": (x.get("releaseInfo") or "")[:4] or None}
              for x in metas[:15] if not x["id"].startswith(rid('cmd:'))]
    with _ai_lock:
        scope, turn = request
        if _search_turns.get(scope) != (search_key(query), turn):
            return {"metas": metas}
        old = _last_search.get(scope)
        rows = old["rows"] if old and old["turn"] == turn else {}
        _last_search[scope] = {"at": time.time(), "query": query, "answers": answer or {"choice": "title", "watched": "any", "list": "taste"},
                               "rows": {**rows, stype: titles}, "prior": prior, "wish": wish, "turn": turn}
    return {"metas": metas}


class BudgetExceeded(Exception):
    pass


def tmdb(path, *, _timeout=8, _cache_only=False, **params):
    params["api_key"] = API_KEY
    url = f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(params)}"
    hit = _cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    if _cache_only:
        return {}
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=upstream_timeout(_timeout)) as r:
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

def ai_limit(name, default, convert, maximum=float('inf')):
    try:
        value = convert(os.environ.get(name) or default)
        if math.isfinite(value) and 0 <= value <= maximum:
            return value
    except (ValueError, OverflowError):
        pass
    raise ConfigError(name + (' must be a finite number within 0..1' if maximum == 1 else
                            ' must be a nonnegative integer' if convert is int else ' must be a finite nonnegative number'))


AI_CAPS = {"feed": ai_limit("AI_DAILY_FEED_CALLS", 40, int),
           "tags": ai_limit("AI_DAILY_TAG_CALLS", 60, int),  # batched: 25 titles per call, once per title
           "search": ai_limit("AI_DAILY_SEARCH_CALLS", 300, int),
           "resolve": ai_limit("AI_DAILY_RESOLVE_CALLS", 20, int),
           "embed": ai_limit("AI_DAILY_EMBED_CALLS", 150, int),  # batched: up to 96 titles per call
           "lab": 2}
API_LANE_SHARE = ai_limit("API_LANE_SHARE", .34, float, 1)
AI_MONTHLY_USD = ai_limit("AI_MONTHLY_USD", 3, float)  # hard stop below the OpenRouter key limit
AI_BUDGET_CEILING = AI_MONTHLY_USD if 'AI_MONTHLY_USD' in os.environ else 25
AI_BUDGET_SOURCE = 'env' if 'AI_MONTHLY_USD' in os.environ else 'default'
_usage = {"day": "", "calls": {}, "cost": 0.0, "tokens": 0, "month": "", "month_cost": 0.0, "month_calls": 0}
_usage_lock = threading.Lock()
_ai_credit_lock = threading.Lock()


def load_usage():
    from skald.ai import read_usage
    with _usage_lock:
        _usage.update(read_usage(DATA))


def ai_budget():
    cfg = CONFIG
    path = os.environ.get('SKALD_CONFIG') or os.path.join(DATA, CONFIG_NAME)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f, parse_constant=invalid_json)
    return {'household': cfg['household'].get('ai_monthly_usd'),
            'ceiling': AI_BUDGET_CEILING, 'ceiling_source': AI_BUDGET_SOURCE}


def ai_monthly_usd():
    budget = ai_budget()
    return min(budget['household'] if budget['household'] is not None else AI_MONTHLY_USD, budget['ceiling'])


def ai_status():
    from skald.ai import read_usage, status
    with _usage_lock:
        _usage_roll()
        saved = {k: v for k, v in read_usage(DATA).items() if k.startswith('ai_')}
        return {**status(json.loads(json.dumps({**_usage, **saved})), AI_CAPS, ai_monthly_usd(), CONFIG),
                'budget': ai_budget()}


def check_ai_credit(now=None, *, refresh=False):
    from skald.ai import check_credit
    return check_credit(SimpleNamespace(**globals()), time.time() if now is None else now, refresh=refresh)


def _usage_roll():
    d = local_now()
    if _usage["day"] != d.strftime("%Y-%m-%d"):
        _usage.update(day=d.strftime("%Y-%m-%d"), calls={}, cost=0.0, tokens=0)
    if _usage["month"] != d.strftime("%Y-%m"):
        _usage.update(month=d.strftime("%Y-%m"), month_cost=0.0, month_calls=0)


def save_usage(fields=None, *, counters=True):
    from pathlib import Path
    from skald.ai import read_usage
    from skald.intake import write_private
    with _usage_lock:
        _usage.update(fields or {})
        try:
            with os.fdopen(os.open(os.path.join(DATA, 'usage.lock'), os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                saved = read_usage(DATA) or dict(_usage)
                if counters:
                    saved.update({k: v for k, v in _usage.items() if not k.startswith('ai_')})
                saved.update(fields or {})
                _usage.update({k: v for k, v in saved.items() if k.startswith('ai_')})
                with household_writable(DATA, household_erased) as allowed:
                    if not allowed:
                        return False
                    write_private(Path(DATA) / 'usage.json', json.dumps(saved))
            return True
        except OSError:
            return False


def openrouter(path, body, timeout, lane, label, on_text=None, stop=None):
    try:
        return _openrouter(path, body, timeout, lane, label, on_text, stop)
    except Exception as error:
        from skald.ai import failure
        reason = failure(SimpleNamespace(**globals()), error)
        # Fresh exceptions discard private transport attributes as well as messages.
        if isinstance(error, urllib.error.HTTPError):
            code = error.code if type(error.code) is int and 100 <= error.code <= 599 else 502
            raise urllib.error.HTTPError('https://openrouter.ai', code, reason, None, None) from None
        if isinstance(error, urllib.error.URLError):
            raise urllib.error.URLError(TimeoutError(reason) if reason == 'timeout' else reason) from None
        safe = (BudgetExceeded if isinstance(error, BudgetExceeded) else
                UnsafeURL if isinstance(error, UnsafeURL) else
                ByteCapExceeded if isinstance(error, ByteCapExceeded) else
                TimeoutError if isinstance(error, TimeoutError) else
                OSError if isinstance(error, OSError) else ValueError)
        raise safe(reason) from None


def _openrouter(path, body, timeout, lane, label, on_text=None, stop=None):
    if stop is not None and stop.is_set():
        raise TimeoutError()
    timeout = upstream_timeout(timeout)
    with _usage_lock:
        _usage_roll()
        used = sum(n for k, n in _usage["calls"].items() if k.startswith(lane + ":"))
        if used >= AI_CAPS[lane]:
            raise BudgetExceeded(f"{lane} budget {AI_CAPS[lane]}/day used")
        monthly = ai_monthly_usd()
        if _usage["month_cost"] >= monthly:
            raise BudgetExceeded(f"monthly budget ${monthly} used")
        key = f"{lane}:{label}"
        _usage["calls"][key] = _usage["calls"].get(key, 0) + 1
        _usage["month_calls"] += 1
    save_usage()
    if on_text is not None:
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    deadline = time.monotonic() + timeout
    j = {}
    try:
        req = urllib.request.Request("https://openrouter.ai" + path, json.dumps(body).encode(), {
            "Authorization": "Bearer " + OR_KEY, "Content-Type": "application/json", "X-Title": "Skald"})
        with _connector_open(req, timeout=timeout) as r:
            if on_text is None:
                raw = r.read(CONNECTOR_HTTP_BYTES + 1)
                if len(raw) > CONNECTOR_HTTP_BYTES:
                    raise ByteCapExceeded()
                j = json.loads(raw)
            else:
                content, size, done, pending = [], 0, False, b""
                finished = threading.Event()
                sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
                def expired():
                    return time.monotonic() >= deadline or stop is not None and stop.is_set()
                def guard():
                    while not finished.is_set():
                        if expired():
                            # Shutdown unblocks buffered reads before close takes their lock.
                            if sock is not None:
                                try:
                                    sock.shutdown(2)
                                except OSError:
                                    pass
                            r.close()
                            return
                        finished.wait(min(.01, max(0, deadline - time.monotonic())))
                watcher = threading.Thread(target=guard, daemon=True)
                watcher.start()
                try:
                    while not done:
                        if expired():
                            raise TimeoutError("Picker stream stopped")
                        raw = r.read1(min(4096, CONNECTOR_HTTP_BYTES - size + 1))
                        size += len(raw)
                        if size > CONNECTOR_HTTP_BYTES:
                            raise ByteCapExceeded()
                        if expired():
                            raise TimeoutError("Picker stream stopped")
                        if not raw:
                            break
                        lines = (pending + raw).split(b"\n")
                        pending = lines.pop()
                        for line in lines:
                            if expired():
                                raise TimeoutError("Picker stream stopped")
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                done = True
                                break
                            event = json.loads(data)
                            if event.get("usage"):
                                j["usage"] = event["usage"]
                            if event.get("error"):
                                code = event["error"].get("code")
                                try:
                                    code = int(code)
                                except (ValueError, TypeError):
                                    raise ValueError("Picker stream failed") from None
                                raise urllib.error.HTTPError(req.full_url, code, "Picker stream failed", {}, None)
                            for choice in event.get("choices", []):
                                text = choice.get("delta", {}).get("content") or ""
                                if text:
                                    content.append(text)
                                    on_text(text)
                    if not done:
                        raise TimeoutError("Incomplete picker stream")
                    j["choices"] = [{"message": {"content": "".join(content)}}]
                finally:
                    finished.set()
                    watcher.join()
        return j
    finally:
        usage = j.get("usage") or {}
        with _usage_lock:
            _usage["cost"] += usage.get("cost") or 0
            _usage["month_cost"] += usage.get("cost") or 0
            _usage["tokens"] += usage.get("total_tokens") or 0
        save_usage()


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
        refresh_preferences()
        scored, unscored = [], []
        for choice in inp["choices"]:
            if not title_allowed(choice["key"], choice["kind"], inp["person"]):
                continue
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
    refresh_preferences()
    return {"ranked": [c for c in inp["choices"] if title_allowed(c["key"], c["kind"], inp["person"])], "pick": None, "why": None, "match": None}, 0.0


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
        fields = set("path model batch timeout prompt parse".split())
        assert isinstance(llm, dict) and set(llm) in (fields, fields | {"fallback"})
        assert spec["lane"] in AI_CAPS and not spec["serving"]
        attempts = [llm]
        if "fallback" in llm:
            assert isinstance(llm["fallback"], dict) and set(llm["fallback"]) == fields
            attempts.append(llm["fallback"])
        for attempt in attempts:
            assert isinstance(attempt["path"], str) and attempt["path"].startswith("/api/")
            assert isinstance(attempt["model"], str) and attempt["model"]
            assert type(attempt["batch"]) is int and 1 <= attempt["batch"] <= 25
            assert attempt["batch"] == llm["batch"]
            assert type(attempt["timeout"]) in (int, float) and math.isfinite(attempt["timeout"]) and attempt["timeout"] > 0
            assert callable(attempt["prompt"]) and callable(attempt["parse"])
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
        if name in _decide_over or name in ("theme_source", "theme_season") and _decide_over.intersection(("theme_source", "theme_season")):
            return "over"
        used = _usage["calls"].get(f"{spec['lane']}:{name}", 0)
        if name in ("theme_source", "theme_season"):
            used = sum(_usage["calls"].get(f"feed:{n}", 0) for n in ("theme_source", "theme_season"))
        if spec["cap"] is not None and used >= spec["cap"]:
            return "cap"
        monthly = ai_monthly_usd()
        if monthly == 0 or _usage["month_cost"] + limit > monthly:
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


@household_write
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
                with history_locked(data), household_writable(data, household_erased) as allowed:
                    if not allowed:
                        continue
                    person = rec.get('answer', {}).get('person')
                    if person in history_cutoffs(data) and person not in load_config(data, readonly=True)[0]['household']['members']:
                        continue
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
        if isinstance(err, str) and err in ('over', 'cap', 'month', 'api_share'):
            from skald.ai import failure
            failure(SimpleNamespace(**globals()), BudgetExceeded())
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
        attempts = [llm] if llm else []
        if llm and "fallback" in llm:
            attempts.append(llm["fallback"])
        for offset in range(0, len(pending), llm["batch"] if llm else 1):
            chunk = pending[offset:offset + llm["batch"]]
            cost, over = None, False
            try:
                batch = [inputs[i] for i in chunk]
                for attempt in attempts:
                    try:
                        body = attempt["prompt"](batch)
                        reservation = {}
                        gate = _decision_gate(name, spec, allow_llm, limit, client, reservation)
                        if gate:
                            for i in chunk:
                                fallback(i, gate, cost, over)
                            break
                        try:
                            resp = openrouter(attempt["path"], body, attempt["timeout"], spec["lane"], name)
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
                            cost = (cost or 0) + reported / len(chunk)
                            over = cost * len(chunk) > limit
                            if over:
                                with _usage_lock:
                                    _decision_roll()
                                    _decide_over.add(name)
                        parsed = attempt["parse"](resp, batch)
                        if not isinstance(parsed, list) or len(parsed) != len(chunk):
                            raise ValueError("answers")
                        if attempt is not attempts[-1]:
                            # Validate the whole first attempt before publishing any batch results.
                            parsed = [_decision_answer(spec, pair) for pair in parsed]
                        for i, pair in zip(chunk, parsed):
                            try:
                                pair = _decision_answer(spec, pair)
                            except Exception as err:
                                fallback(i, err, cost, over)
                            else:
                                result(i, pair, "llm", cost=cost, over=over)
                        break
                    except Exception as err:
                        if attempt is not attempts[-1] and not isinstance(err, BudgetExceeded) and not over:
                            continue
                        for i in chunk:
                            if results[i] is None:
                                fallback(i, err, cost, over)
                        break
            finally:
                with _decide_lock:
                    for i in chunk:
                        event = owned.pop(keys[i])
                        if _decide_flights.get(keys[i]) is event:
                            del _decide_flights[keys[i]]
                        event.set()
        for i, event in followers:
            event.wait(sum(attempt["timeout"] for attempt in attempts) + 1)
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
            shared = sum(_usage["calls"].get(f"feed:{n}", 0) for n in ("theme_source", "theme_season")) if name in ("theme_source", "theme_season") else used
            left = 0 if name in _decide_over else max(0, cap - (shared if spec["cap"] is not None else lane_used))
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
    return {"query": query[:200], **{k: inp[k] for k in ("last", "shown", "context_key") if inp.get(k) is not None}}


def route_state(inp):
    if "last" not in inp:
        return inp["query"]
    shown = inp.get("shown") or []
    if isinstance(shown, str):
        shown = shown.split(", ")
    return f"Last search: {inp['last']}\nSome results shown: {', '.join(shown[:3])}\nTyped now: {inp['query']}"


def route_parse(resp, batch):
    answers = resp["answers"]
    a = answers["route"]
    return [({"choice": a["choice"], "confidence": a.get("confidence"),
              "watched": answers.get("watched", {}).get("choice") or "any",
              "list": answers.get("list", {}).get("choice") or "taste",
              **({"previous": answers.get("previous", {}).get("choice") or "new"} if "last" in batch[0] else {})}, a.get("confidence") or 0)]


decision("route_query", ver=9, lane="search", cap=None, ttl=30 * DAY,
    inp={"query": str, "last": (str, None), "shown": (str, [str, 30], None), "context_key": (str, None)}, out={"choice": ("title", "discover", "request", "command", None), "confidence": (float, None),
                           "watched": ("unseen", "seen", "any", None), "list": ("best", "taste", None), "previous": ("new", "refine", "act", None)},
    norm=route_norm, rule=None, params={},
    llm={"path": "/api/alpha/decisions", "model": ROUTE_MODEL, "batch": 1, "timeout": 2.5,
         "prompt": lambda b: {"model": ROUTE_MODEL, "state": route_state(b[0]), "questions": ROUTE_FOLLOWUP_Q if "last" in b[0] else ROUTE_Q}, "parse": route_parse},
    fallback=lambda inp, err: ({"choice": None, "confidence": None, "watched": "any", "list": "taste"}, 0),
    requires=(), serving=False, min_acc=.95)


from skald import commands as search_commands
from types import SimpleNamespace
search_commands.register(SimpleNamespace(**globals()))


def command_context():
    context = globals().setdefault("_command_context", SimpleNamespace())
    context.__dict__.update(globals())
    return context


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
    return {"model": LUNA_DECIDER_MODEL, "state": json.dumps([{k: x.get(k) for k in ("title", "year", "kind")} for x in batch]),
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
    llm={"path": "/api/alpha/decisions", "model": LUNA_DECIDER_MODEL, "batch": 25, "timeout": 20,
         "prompt": resolve_prompt, "parse": resolve_parse},
    fallback=lambda inp, err: ({"pick": None}, 0), requires=("OR_KEY",), serving=False, min_acc=.95)


def search_profile():
    """The scoped member's history, or the union of current members for Together."""
    p = _profiles.get("household") or {}
    scope = getattr(_scoping, "scope", None)
    if scope is None and not guest_people(CONFIG) and not CONFIG['household'].get('retired_rows'):
        return p
    people = member_people() if scope is None or SCOPES[scope] == "both" else (SCOPES[scope],)
    entries = [e for person in people for e in person_entries(p, person)] if p else []
    return {"entries": entries, "seen": {e["key"] for e in entries} | history_seen(DATA),
            "negative": {e["key"] for e in entries if e["weight"] < 0}}


def seen_history(p):
    entries = sorted(p.get("entries", []),
                     key=lambda e: (e["age"] == 999, -e["weight"] if e["age"] == 999 else e["age"]))
    history = {}
    for e in entries:
        it = e["item"]
        watched = (e["signal"] in ("loved", "liked", "watched") or it.get("played") or it.get("completed")
                   or it.get("rating") is not None or (it.get("episodes_played") or 0) > 0
                   or (it.get("progress") or 0) >= .7)
        if watched and e["key"] in p.get("seen", ()) and e["key"] not in p.get("negative", ()):
            history.setdefault((e["key"], e["stype"]), e)
    return list(history.values())


def search_person():
    person = SCOPES.get(getattr(_scoping, "scope", None))
    return person if person in PEOPLE and (_model["m"] or {}).get("paff", {}).get(person) else None


class SearchStream:
    def __init__(self, stype, timing, stop=None):
        self.stype, self.timing = stype, timing
        self.stop = stop if stop is not None else threading.Event()
        self.start = time.monotonic()
        self.changed = threading.Event()
        self.lock = threading.RLock()
        self.jobs, self.resolve, self.finished = {}, None, False
        self.consumed = False

    def add(self, pick):
        if pick[2] != self.stype:
            return
        with self.lock:
            if self.stop.is_set():
                return
            self.timing.setdefault("first_line_ms", round((time.monotonic() - self.start) * 1000))
            if pick not in self.jobs:
                self.jobs[pick] = self.submit(pick) if self.resolve else None
        self.changed.set()

    def submit(self, pick):
        if self.stop.is_set():
            return None
        job = _search_pool.submit(self.resolve, pick)
        def completed(_):
            with self.lock:
                self.timing["resolve_ms"] = round((time.monotonic() - self.resolve_start) * 1000)
                self.release()
            self.changed.set()
        job.add_done_callback(completed)
        return job

    def cancel(self):
        with self.lock:
            self.stop.set()
            for job in list(self.jobs.values()):
                if job is not None:
                    job.cancel()
        self.changed.set()

    def finish(self, _):
        with self.lock:
            self.finished = True
            self.release()
        self.changed.set()

    def release(self):
        if self.consumed and self.finished and self.jobs and all(job is not None and job.done() for job in self.jobs.values()):
            self.timing["resolved"] = len(self.jobs)
            self.jobs.clear()

    def consume(self):
        with self.lock:
            self.consumed = True
            self.release()

    def resolving(self, kind, lang, deadline):
        with self.lock:
            if self.stop.is_set():
                return False
            if self.resolve is None:
                self.lang = lang
                self.resolve_start = time.monotonic()
                def resolve(pick):
                    if self.stop.is_set() or deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError()
                    with search_deadline(deadline):
                        return resolve_pick(kind, pick[0], pick[1], lang)
                self.resolve = resolve
                for pick in self.jobs:
                    self.jobs[pick] = self.submit(pick)
            return self.lang == lang

    def ready(self):
        with self.lock:
            return [p for p, job in self.jobs.items() if job is not None and job.done()]


def search_picks(query, history, listing="taste", stype="movie", person=..., wide=True, deadline=None):
    if person is ...:
        person = search_person()
    m = _model["m"]
    hint = taste_hint(m, person) if m and listing != "best" else "none available"
    taste = f"\nHousehold taste (use only for personal taste requests; ignored for best-of/consensus lists): {hint}" if listing != "best" else ""
    prompt = (query + taste +
              "\nViewing history (if the request wants titles new to the viewer, skip these; "
              "if it asks for titles they watched, choose only from these):\n" + "\n".join(
                  f"{e['title']}|{e.get('year') or ''}|{'m' if e['stype'] == 'movie' else 's'}" for e in history[:SEARCH_HISTORY_LIMIT]))
    titles, code = ("movies", "m") if stype == "movie" else ("series", "s")
    type_instruction = (
        "This catalog shows movies only: every line is a movie, never series. 'shows' or 'series' (in any language) means series only: if the request asks only for series, reply with the single word NONE. Otherwise give 18 movies."
        if stype == "movie" else
        "This catalog shows series only: every line is a series, never movies. 'movies' or 'films' (in any language) means movies only: if the request asks only for movies, reply with the single word NONE. Otherwise give 18 series.")
    system = PICK_SYS.replace("You pick movies and series for", f"You pick {titles} for").replace(
        "Title|Year|m or s (m=movie, s=series)", f"Title|Year|{code}").replace("30 lines", "18 lines").replace(
        "'movies' or 'films' means movies only; 'shows' or 'series' means series only; otherwise give at least 12 movies and 12 series.",
        type_instruction)
    timing, stop = {}, threading.Event()
    stream = SearchStream(stype, timing, stop) if wide else None
    if wide:
        system = system.replace("18 lines", f"{SEARCH_WIDE_SIZE} lines").replace("give 18 ", f"give {SEARCH_WIDE_SIZE} ")
    def pick():
        start = time.monotonic()
        try:
            timeout = SEARCH_WIDE_TIMEOUT if wide else 6
            if deadline is not None:
                timeout = min(timeout, max(0, deadline - start))
            if stop.is_set() or timeout <= 0:
                raise TimeoutError()
            args = (system, prompt, 2000 if wide else 850, timeout, "search", "wide_picks" if wide else "picks")
            picks = llm_picks(*args, stream.add if stream else None, stop)
            if stream:
                for pick in picks:
                    stream.add(pick)
            return picks
        finally:
            timing["ms"] = round((time.monotonic() - start) * 1000)
    rules = _pool.submit(search_rules, query, deadline, stop) if wide else None
    future = _pool.submit(pick)
    future.search_timing = timing
    future.search_wide = wide
    future.search_stream = stream
    if stream:
        future.add_done_callback(stream.finish)
    future.search_rules = rules
    future.search_stop = stop
    future.search_fallback = None
    future.search_args = (query, history, listing, stype, person)
    future.search_deadline = deadline
    if wide:
        def hedge():
            try:
                picks = future.result(timeout=SEARCH_HEAD_START)
                if isinstance(picks, NoPicks) or any(p[2] == stype for p in picks):
                    return
            except Exception:
                pass
            if not stop.is_set():
                search_backup(future)
        future.search_hedge = _outer.submit(hedge)
    return future


def search_backup(picker):
    with _ai_lock:
        stop = getattr(picker, "search_stop", None)
        if stop is not None and stop.is_set():
            return None
        if picker.search_fallback is None:
            picker.search_fallback = search_picks(*picker.search_args, wide=False, deadline=picker.search_deadline)
        return picker.search_fallback


def cancel_search_picks(picker):
    if picker is not None:
        with _ai_lock:
            for job in (picker, getattr(picker, "search_rules", None), getattr(picker, "search_fallback", None)):
                if job is not None:
                    stop = getattr(job, "search_stop", None)
                    if stop is not None:
                        stop.set()
                    stream = getattr(job, "search_stream", None)
                    if isinstance(stream, SearchStream):
                        stream.cancel()
                    job.cancel()


def search_rules(wish, deadline=None, stop=None):
    llm = DECISIONS["parse_command"]["llm"]
    for attempt in (llm, llm["fallback"]):
        try:
            timeout = 4 if deadline is None else min(4, max(0, deadline - time.monotonic()))
            if timeout <= 0 or stop is not None and stop.is_set():
                break
            body = attempt["prompt"]([{"query": wish, "last": None}])
            body.update(usage={"include": True}, messages=[
                {"role": "system", "content": 'Extract only explicit release-year limits and reference titles used as a model for recommendations. '
                 'Return JSON {"from": integer or null, "to": integer or null, "exclude": [title names]}. '
                 'Bounds are inclusive. Convert requested decades to year bounds. Either bound can be null. '
                 'Exclude titles used as reference examples, not titles the user wants to find or rewatch. '
                 'For a combined wish follow the latest refinement and retain earlier rules it does not change. '
                 'Do not invent limits or TMDB ids. No explanation.'},
                {"role": "user", "content": wish}])
            reply = openrouter(attempt["path"], body, timeout, "search", "rules", None, stop)
            value = search_commands.parse_json(reply["choices"][0]["message"]["content"])
            if not isinstance(value, dict):
                raise ValueError("Rules must be an object")
            rules = {"from": value.get("from"), "to": value.get("to"), "exclude": value.get("exclude", [])}
            if any(v is not None and (type(v) is not int or not 1 <= v <= 9999) for v in (rules["from"], rules["to"])):
                raise ValueError("Invalid year bounds")
            if rules["from"] is not None and rules["to"] is not None and rules["from"] > rules["to"]:
                raise ValueError("Reversed year bounds")
            if not isinstance(rules["exclude"], list) or not all(isinstance(v, str) for v in rules["exclude"]):
                raise ValueError("Invalid reference titles")
            return rules
        except Exception as error:
            if isinstance(error, BudgetExceeded):
                break
    return {"from": None, "to": None, "exclude": []}


def wide_rank(metas, rules, person, references=None, deadline=None):
    names = {resolve_normal(name) for name in rules["exclude"]}
    excluded = {pair for name in names for pair in (references or {}).get(name, ())}
    with _facets_lock:
        facets = list(_facets.items())
    excluded.update((slot.split("@")[0], "movie" if rec["kind"] == "movie" else "series")
                    for slot, rec in facets if rec.get("kind") in ("movie", "tv")
                    and resolve_normal(rec.get("title") or "") in names)
    excluded.update((e["key"], e["stype"]) for e in _profiles.get("household", {}).get("entries", [])
                    if resolve_normal(e["title"]) in names)
    allowed = []
    for meta in metas:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        kind = "movie" if meta["type"] == "movie" else "tv"
        year = None
        for value in (frec(meta["id"], kind).get("y"), meta.get("releaseInfo")):
            try:
                parsed = int(str(value)[:4])
                if 1 <= parsed <= 9999:
                    year = parsed
                    break
            except (ValueError, TypeError):
                continue
        if year is not None and (rules["from"] is not None and year < rules["from"] or rules["to"] is not None and year > rules["to"]):
            continue
        if (meta["id"], meta["type"]) not in excluded and resolve_normal(meta["name"].removeprefix(AI_MARK)) not in names:
            allowed.append(meta)
    metas = list({x["id"]: x for x in allowed}.values())
    model = _model["m"]
    tastes = []
    for x in metas:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        tastes.append(score_key(model, x["id"], person, kind="movie" if x["type"] == "movie" else "tv")[0]
                      if model else None)
    known = [v for v in tastes if v is not None]
    low, high = min(known, default=0), max(known, default=0)
    ranked = []
    for i, (meta, score) in enumerate(zip(metas, tastes)):
        taste = (score - low) / (high - low) if score is not None and high > low else .5
        ranked.append((-(.2 * (1 - i / len(metas)) + .8 * taste), meta["id"], meta))
    return [meta for _, _, meta in sorted(ranked, key=lambda x: (x[0], x[1]))][:15]


def ai_plan(query, history=None, context=None, stype="movie", person=..., picks=..., deadline=None, stop=None):
    """Route once; retain a shared picker future for requests and empty title results."""
    if stop is not None and stop.is_set():
        raise TimeoutError()
    if person is ...:
        person = search_person()
    if history is None:
        history = seen_history(search_profile())
    if picks is ...:
        picks = None if context else search_picks(query, history, stype=stype, person=person, deadline=deadline)
    inp = {"query": query}
    if context:
        inp.update(last=context["query"], shown=[x["title"] for x in shown_titles(context)])
        inp["context_key"] = hashlib.sha256(json.dumps({k: context.get(k) for k in ("query", "answers", "rows", "wish")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    start = time.monotonic()
    source = "fallback"
    try:
        routed = decide("route_query", route_norm(inp))
        a, source = routed["answer"], routed.get("source", "llm")
    except Exception:
        a = {"choice": None}
    router_ms = round((time.monotonic() - start) * 1000)
    wish = query
    if context and a.get("previous") == "refine":
        cancel_search_picks(picks)
        picks = None
        wish = (f"Answer the combined wish. Last search: {context.get('wish') or context['query']}\n"
                f"Shown titles (in display order): {json.dumps(shown_titles(context), ensure_ascii=False)}\nTyped now: {query}")
    channel = "command" if a.get("previous") == "act" else "picker" if a.get("previous") == "refine" else SEARCH_CHANNELS.get(a["choice"], "picker")
    if channel == "command" and picks is not None:
        cancel_search_picks(picks)
    if channel == "picker" and picks is None:
        with _ai_lock:
            if stop is not None and stop.is_set():
                raise TimeoutError()
            picks = search_picks(wish, history, a.get("list"), stype, person, deadline=deadline)
    return {**a, "picks": picks, "wish": wish, "router_ms": router_ms, "router_source": source,
            "watched": a.get("watched") or "any", "list": a.get("list") or "taste"}


def taste_rerank(metas, deadline=None, person=...):
    """Search picks: the LLM's order blended with the viewer's match, plus a match note; never blocks on fetches."""
    m = _model["m"]
    if person is ...:
        person = search_person()
    if not m or not m.get("cal") or len(metas) < 2:
        return metas
    missing = [(x["id"], "movie" if x["type"] == "movie" else "tv") for x in metas if not frec(x["id"], "movie" if x["type"] == "movie" else "tv")]
    if missing:
        _outer.submit(ensure_facets, missing)  # known next time
    scored = []
    for i, x in enumerate(metas):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        pct = match_pct(m, score_key(m, x["id"], person, kind="movie" if x["type"] == "movie" else "tv")[0], person)
        taste = (pct - 70) / 30 if pct is not None else 0.0
        scored.append((-(1 - i / len(metas)) * 0.8 - 0.2 * taste, i, with_match(x, why=False, person=person) if pct else x))
    return [x for *_, x in sorted(scored)]


def parse_picks(text):
    picks = []
    for line in text.splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) in (3, 4, 5) and parts[0] and not parts[0].startswith("#"):
            year = parts[1][:4] if parts[1][:4].isdigit() else None
            why = parts[3] if len(parts) >= 4 else ""
            picks.append((parts[0], year, "series" if parts[2].lower().startswith("s") else "movie", why)
                         + ((parts[4],) if len(parts) == 5 else ()))
    return picks


def llm_text(system, prompt, max_tokens, timeout, lane, label, effort="none", on_text=None, stop=None):
    body = {
        "model": PICK_MODEL, "max_tokens": max_tokens, "reasoning": {"effort": effort}, "usage": {"include": True},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    args = ("/api/v1/chat/completions", body, timeout, lane, label)
    j = openrouter(*args, on_text, stop) if stop is not None else openrouter(*args, on_text) if on_text else openrouter(*args)
    return j["choices"][0]["message"].get("content") or ""


class NoPicks(list):
    """An explicit NONE is a valid empty catalog, not a failed picker."""


def llm_picks(system, prompt, max_tokens, timeout, lane, label, on_pick=None, stop=None):
    pending = ""
    def feed(text):
        nonlocal pending
        if stop is not None and stop.is_set():
            raise TimeoutError()
        lines = (pending + text).split("\n")
        pending = lines.pop()
        for line in lines:
            for pick in parse_picks(line):
                on_pick(pick)
    args = (system, prompt, max_tokens, timeout, lane, label)
    text = llm_text(*args, on_text=feed if on_pick else None, stop=stop)
    if text.strip() == "NONE":
        return NoPicks()
    picks = parse_picks(text)
    if text.strip() and not picks:
        raise ValueError("Invalid picker reply")
    return picks


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


def ai_future(query, history=None, stype="movie", timing=None, deadline=None):
    """Share one day's AI answer for equivalent queries within a viewer scope."""
    context = search_turn(query)
    key = search_answer_key(query, stype, context)
    with _ai_lock:
        hit = _ai.get(key)
        if hit and hit[0] > time.time():
            fut = hit[1]
            if not fut.done():
                if timing is not None:
                    timing["picker_ms"] = "cached"
                return fut
            try:
                answer = fut.result()
                picks = answer["picks"]
                if (picks is None or answer.get("previous") == "act" or SEARCH_CHANNELS.get(answer["choice"]) == "command" or not picks.done()
                        or not picks.cancelled() and picks.exception() is None):
                    if timing is not None:
                        timing["picker_ms"] = "cached"
                    return fut
            except Exception:
                pass
        if len(_ai) >= CACHE_MAX:
            _ai.clear()
        if history is None:
            history = seen_history(search_profile())  # Capture the caller's scope before entering the pool.
        person = search_person()
        picks = None if context else search_picks(query, history, stype=stype, person=person, deadline=deadline)
        stop = threading.Event()
        fut = _outer.submit(ai_plan, query, history, context, stype, person, picks, deadline, stop)
        fut.search_stop = stop
        fut.search_picks = picks
        ttl = min(AI_SEARCH_TTL, context["at"] + LAST_SEARCH_TTL - time.time()) if context else AI_SEARCH_TTL
        _ai[key] = (time.time() + ttl, fut)
        return fut


def search_answer_key(query, stype, context):
    return (search_key(query), search_scope(), stype, json.dumps(context, sort_keys=True, ensure_ascii=False) if context else None)


def genres(kind, lang):
    try:
        data = tmdb(f"genre/{kind}/list", language=lang)
        return {g["id"]: g["name"] for g in data.get("genres", [])}
    except Exception:
        return {}


def manifest(lang, scope=None):
    pin_shown(lang, scope)
    extra = [{"name": "search", "isRequired": True}]
    suffix = {"movie": t("catalog.movies"), "series": t("catalog.series"), "tv": ""}
    rows = []
    with addon_scope(scope):
        catalogs = feed_rows(lang)
        for cid, typs, name in catalogs:
            if cid in MIXED and "tv" in typs:  # one mixed row; movie/series copies only duplicate it in Odin
                typs = ["tv"]
            for typ in typs:
                if cid in (rid('top10'), rid('toprated')) and scope in PEOPLE:
                    _, prefs = row_preferences(cid, scope)
                    if ('movies' if typ == 'movie' else 'series') in prefs[0].get('hide', []):
                        continue
                if cid not in SHELF_ROWS and (cid in THEMED_ROWS or cid in READY_ROWS or feed(cid, lang) or (cid, lang) in _feeds_empty or cid == rid('family')) and (cid != rid('top10') or kids_age(safety_subject()) is not None):
                    # Count the cached composition of stored rows, with the catalog's viewer and pins.
                    metas = row_metas(cid, typ, lang, who=SCOPES[scope]) if scope is not None else row_metas(cid, typ, lang)
                    if len(metas) < (1 if cid in (rid('episodes'), rid('weekpicks')) or cid in READY_ROWS else ROW_MIN):
                        continue
                if isinstance(name, dict):
                    rows.append({"type": typ, "id": cid, "name": name[typ]})
                else:
                    rows.append({"type": typ, "id": cid, "name": name + (suffix[typ] if len(typs) > 1 else "")})
    reserved = {row["name"] for row in rows}
    used = set()
    for row in rows:
        name = row["name"]
        if name in used and row["id"] not in SHELF_ROWS:
            n = 2
            while f"{name} · {n}" in reserved:
                n += 1
            row["name"] = f"{name} · {n}"
            reserved.add(row["name"])
        used.add(row["name"])
    rows += [
        {"type": "movie", "id": rid('search'), "name": t("catalog.search"), "extra": extra},
        {"type": "series", "id": rid('search'), "name": t("catalog.search"), "extra": extra},
    ]
    return {
        "id": CONFIG["addon"]["id"] + (f".{scope}" if scope else ""),
        "version": CONFIG["addon"]["version"],
        "name": CONFIG["addon"]["name"] + (f" · {(t("label.together") if scope == "together" else LABELS[scope])}" if scope else ""),
        "description": t("addon.description", language=lang) + (f" · {(t("label.together") if scope == "together" else LABELS[scope])}" if scope else ""),
        "resources": ["catalog", {"name": "meta", "types": ["movie", "series", "tv"], "idPrefixes": [rid('cmd:')]}],
        "types": ["movie", "series", "tv"],
        "idPrefixes": ["tmdb:"],
        "catalogs": rows,
    }


def display_name(it, en, lang=LANG):
    name = it.get("title") or it.get("name") or ""
    original = it.get("original_title" if it.get("title") else "original_name")
    original_language = it.get("original_language") or ""
    if name and ((original and name != original) or
                 (original_language and original_language.split("-")[0] == lang.split("-")[0])):
        return name
    # History cards and offline reconstructions lack provenance; retain the old fallback.
    if not re.search(r"[A-Za-zÀ-ɏ]", name):
        name = en.get("title") or en.get("name") or name
    return name


def to_meta(it, stype, kind, names, en, mark="", note="", lang=LANG):
    date = it.get("release_date") if kind == "movie" else it.get("first_air_date")
    ids = it.get("genre_ids") or [g["id"] for g in it.get("genres") or []]
    overview = it.get("overview") or en.get("overview") or ""
    meta = {
        "id": f"tmdb:{it['id']}",
        "type": stype,
        "name": mark + display_name(it, en, lang),
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
    params["_timeout"] = upstream_timeout(params.get("_timeout", 8))
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


from skald.search import ResolutionCache
_search_resolutions = ResolutionCache(os.path.join(DATA, ".cache", "search-resolve.sqlite3"))


def resolve_pick(kind, title, year, lang):
    key = json.dumps((title.casefold(), str(year or ""), kind, lang), ensure_ascii=False)
    hit = _search_resolutions.get(key)
    if hit is not None:
        return tuple(hit)
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
    hit = best, art.get(best["id"], {})
    _search_resolutions.put(key, hit)
    return hit


def ai_search(stype, kind, picks, lang, names, mark=AI_MARK, history=None, references=None, deadline=None, resolved=None):
    if history is not None:
        allowed = {(e["title"].casefold(), str(e.get("year") or ""), e["stype"]): e for e in history}
        metas, used = [], set()
        for title, year, typ, why, *_ in picks:
            e = allowed.get((title.casefold(), year or "", typ))
            if e and typ == stype:
                if references is not None:
                    references.setdefault(resolve_normal(title), set()).add((e["key"], typ))
                if e["key"] not in used:
                    used.add(e["key"])
                    it = {"id": e["id"], "title": e["title"], "name": e["title"],
                          "release_date": str(e.get("year") or ""), "first_air_date": str(e.get("year") or "")}
                    metas.append(to_meta(it, stype, kind, names, {}, mark, why, lang=lang))
        return metas
    jobs = [(p[0], p[3], (resolved or {}).get(p) or _outer.submit(resolve_pick, kind, p[0], p[1], lang))
            for p in picks if p[2] == stype]
    metas, seen = [], set()
    for title, why, job in jobs:
        try:
            hit = job.result(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
        except Exception:
            continue
        if hit:
            if references is not None:
                references.setdefault(resolve_normal(title), set()).add((f"tmdb:{hit[0]['id']}", stype))
            if hit[0]["id"] not in seen:
                seen.add(hit[0]["id"])
                metas.append(to_meta(hit[0], stype, kind, names, hit[1], mark, why, lang=lang))
    return metas


def looks_like_title(query, lang):
    """An exact TMDB title identity needs no router call."""
    q = query.casefold()
    try:
        res = tmdb("search/multi", query=query, language=lang, include_adult="false", _timeout=upstream_timeout(8)).get("results", [])
    except Exception:
        return False
    if any(it.get("media_type") == "person" and any(person_identity(query) == person_identity(it.get(k) or "")
           for k in ("name", "original_name")) for it in res[:10]):
        return False
    return any(it.get("media_type") != "person" and (it.get(k) or "").casefold() == q
               for it in res[:10] for k in ("title", "name", "original_title", "original_name"))


def person_identity(name):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", name.casefold())
                            if not unicodedata.combining(c)).split())


def person_titles(query, kind, lang):
    persons = tmdb("search/person", query=query, language=lang, include_adult="false").get("results", [])
    person = persons[0] if persons else {}
    if not any(person_identity(query) == person_identity(person.get(k) or "") for k in ("name", "original_name")):
        return None
    credits = tmdb(f"person/{person['id']}/combined_credits", language=lang)
    titles = credits.get("cast", []) + [it for it in credits.get("crew", [])
                                       if it.get("department") == "Directing" or it.get("job") == "Director"]
    return sorted((it for it in titles if it.get("media_type") == kind and not it.get("adult")
                   and (kind != "tv" or not {10767, 10763}.intersection(it.get("genre_ids") or []))),
                  key=lambda it: (kind == "tv" and ((it.get("episode_count") or 0) >= 3 or it in credits.get("crew", [])),
                                  it.get("popularity") or 0), reverse=True)


def superseded(query, wait):
    """Odin searches on every keystroke; skip the AI for a query the user is still typing past."""
    start = time.time()
    time.sleep(wait)
    qc = query.casefold()
    return any(t >= start - 0.1 and q2.casefold() != qc and q2.casefold().startswith(qc) for t, q2 in list(_searches)[-20:])


def search_failure(error):
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, urllib.error.HTTPError):
        return {402: "credit", 403: "denied", 429: "rate"}.get(error.code, "provider")
    if isinstance(error, TimeoutError) or isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError):
        return "timeout"
    return "error"


def search(stype, query, lang, title_only=False):
    with search_deadline(time.monotonic() + SEARCH_TIMEOUT):
        return _search(stype, query, lang, title_only)


def _search(stype, query, lang, title_only=False):
    started = time.monotonic()
    deadline = started + SEARCH_TIMEOUT - min(.05, SEARCH_TIMEOUT / 10)  # Finish filtering and cards inside the deadline.
    picker_deadline = deadline - min(.5, SEARCH_TIMEOUT / 10)  # Leave time for the unpaid fallback.
    local_deadline = deadline - min(.05, SEARCH_TIMEOUT / 20)
    def remaining():
        return max(0, deadline - time.monotonic())
    def picker_remaining():
        return max(0, picker_deadline - time.monotonic())
    def call(fn, *args, _deadline=picker_deadline, **kwargs):
        return _outer.submit(fn, *args, **kwargs).result(timeout=max(0, _deadline - time.monotonic()))
    timing = {}
    _scoping.search_timing = None
    _scoping.private_command = False
    request = start_search(query)
    refresh_preferences()
    kind = "movie" if stype == "movie" else "tv"
    who = safety_subject()
    def filtered(metas):
        if kids_age(who) is not None:
            try:
                call(ensure_facets, [(x["id"], kind) for x in metas])
            except TimeoutError:
                return []
            return [x for x in metas if child_ok(x, who)]
        return metas
    context = search_turn(query) if not title_only else None
    command = (search_commands.special(command_context(), query, stype, results=context) if context else
               search_commands.special(command_context(), query, stype)) if not title_only else None
    if command is not None:
        return command
    plan = None
    complete = True
    p = search_profile()
    history = seen_history(p)
    person = search_person()
    title = True
    settled = None
    with _ai_lock:
        cached = _ai.get(search_answer_key(query, stype, context))
        cached = cached is not None and cached[0] > time.time()
    if not title_only and OR_KEY and (context is not None or len(query) >= AI_MIN_LEN):
        # Settle while TMDB checks identity; exact titles never wait for the settle.
        settled = _outer.submit(superseded, query, min(SEARCH_SETTLE, remaining())) if not cached else None
        title_start = time.monotonic()
        try:
            title = not cached and context is None and _outer.submit(looks_like_title, query, lang).result(timeout=min(.8, picker_remaining()))
        except TimeoutError:
            title = False
        timing["title_ms"] = round((time.monotonic() - title_start) * 1000)
    if not title_only and OR_KEY and not title:
        try:
            complete = settled is None or not settled.result(timeout=picker_remaining())
        except TimeoutError:
            complete = False
        timing["settle_ms"] = round((time.monotonic() - title_start) * 1000) - timing["title_ms"]
        with _ai_lock:
            scope, turn = request
            complete = complete and _search_turns.get(scope) == (search_key(query), turn)
        if complete:
            plan = ai_future(query, history, stype, timing, deadline=picker_deadline)
    q = query.casefold()

    def exact(it):
        return q in {(it.get(k) or "").casefold() for k in ("title", "name", "original_title", "original_name")}

    tmdb_start = time.monotonic()
    title_job = _outer.submit(search_both, kind, query, lang) if plan is None or SEARCH_ORDER == "parallel" else None
    data, art = [], {}
    if plan is None:
        try:
            data, art = title_job.result(timeout=picker_remaining())
        except TimeoutError:
            complete = False
    names_job = _pool.submit(genres, kind, lang)
    timing["resolve_ms"] = round((time.monotonic() - tmdb_start) * 1000)
    picks, answer, channel = None, None, "tmdb"
    fallback_job = None
    fallback_metas = None
    wide_job = None
    references = {}
    resolve_start = None
    failure = None
    stream = None
    def wide_filtered(metas, intent):
        allowed = {e["key"] for e in history if e["stype"] == stype}
        return filtered([x for x in metas if x["id"] not in p.get("negative", ())
                         and (intent != "unseen" or x["id"] not in p.get("seen", ()))
                         and (intent != "seen" or x["id"] in allowed)
                         and not frec(x["id"], kind).get("adult")])

    def wide_metas(picks, names, intent):
        return wide_filtered(ai_search(stype, kind, picks, lang, names, history=history if intent == "seen" else None,
                             references=references, deadline=picker_deadline, resolved=stream.jobs if stream else None), intent)

    def wait_wide(picker, timeout):
        nonlocal stream
        stream = getattr(picker, "search_stream", None)
        intent = answer.get("watched", "any")
        if not isinstance(stream, SearchStream) or intent == "seen":
            stream = None
            return picker.result(timeout=timeout)
        if not stream.resolving(kind, lang, picker_deadline):
            stream = None
            return picker.result(timeout=timeout)
        until = min(picker_deadline, time.monotonic() + timeout)
        serve_at = min(started + (0 if timing.get("picker_ms") == "cached" else SEARCH_SERVE_AFTER), until)
        while True:
            stream.changed.clear()
            now = time.monotonic()
            ready = stream.ready()
            if picker.done():
                picks = picker.result(timeout=0)
                if isinstance(picks, NoPicks) or len(ready) == len(stream.jobs):
                    return picks
            if now >= serve_at and len(ready) >= SEARCH_SERVE_MIN and names_job.done() and picker.search_rules.done():
                metas = wide_metas(ready, names_job.result(), intent)
                if len(call(wide_rank, metas, picker.search_rules.result(), person, references,
                            _deadline=local_deadline, deadline=local_deadline)) >= SEARCH_SERVE_MIN:
                    timing.update(early=True, resolved=len(metas))
                    return ready
            if now >= until:
                return picker.result(timeout=0)
            stream.changed.wait(max(0, min(until, serve_at if now < serve_at else until) - time.monotonic()))

    def fallback_picks():
        nonlocal stream
        if stream is not None:
            stream.consume()
            stream = None
        if getattr(answer["picks"], "search_wide", False) is True:
            picker = answer["picks"]
            answer["picks"] = search_backup(picker)
            picker.cancel()
        return answer["picks"].result(timeout=picker_remaining())

    def resolve_fallback(picker, intent):
        hedge = getattr(picker, "search_hedge", None)
        if hedge is not None:
            hedge.result(timeout=picker_remaining())
        legacy = picker.search_fallback
        if legacy is None:
            return None
        return ai_search(stype, kind, legacy.result(timeout=picker_remaining()), lang, names_job.result(timeout=picker_remaining()),
                         history=history if intent == "seen" else None, deadline=picker_deadline)

    def resolve_wide(picker, intent):
        return ai_search(stype, kind, picker.result(timeout=picker_remaining()), lang, names_job.result(timeout=picker_remaining()),
                         history=history if intent == "seen" else None, references=references, deadline=picker_deadline)

    def failed_search(error):
        nonlocal complete, failure
        failure = failure or search_failure(error)
        waiting = answer.get("picks") if answer else plan
        failed = waiting is not None and (waiting.cancelled() or waiting.done() and waiting.exception() is not None)
        if isinstance(error, TimeoutError) and not failed:
            complete = False
        if channel == "picker":
            timing["picker_ms"] = "timeout" if isinstance(error, TimeoutError) else 0
        if not isinstance(error, TimeoutError) or failed:
            with _ai_lock:
                for key, (_, future) in list(_ai.items()):
                    if future is plan:
                        del _ai[key]

    def served(metas):
        timing["total_ms"] = round((time.monotonic() - started) * 1000)
        if not timing.get("early"):
            cancel_search_picks(getattr(plan, "search_picks", None))
            cancel_search_picks((answer or {}).get("picks"))
        if plan is not None:
            timing.update(router_ms=(answer or {}).get("router_ms", "timeout"),
                          router_source=(answer or {}).get("router_source", "fallback"),
                          resolve_ms=timing["resolve_ms"] + (round((time.monotonic() - resolve_start) * 1000) if resolve_start is not None else 0),
                          channel=channel, rows={stype: len(metas)})
            timing.setdefault("picker_ms", 0)
            picker = (answer or {}).get("picks")
            timing.update({k: v for k, v in getattr(picker, "search_timing", {}).copy().items() if k != "ms"})
            if stream is not None:
                timing.setdefault("resolved", len(stream.ready()))
                timing["picker_state"] = "complete" if picker.done() else "streaming"
                if not picker.done() and timing["picker_ms"] != "cached":
                    timing["picker_ms"] = round((time.monotonic() - stream.start) * 1000)
                stream.consume()
        _scoping.search_timing = timing
        if not complete or channel == "command":
            return {"metas": metas}
        answers = {k: v for k, v in (answer or {}).items() if k not in ("picks", "wish", "router_ms", "router_source")}
        return remember_search(query, stype, metas, answers, context, (answer or {}).get("wish", query), request)
    if plan is not None:
        channel = "picker"
        try:
            try:
                # Leave time for the parallel fallback to resolve if routing is late.
                answer = plan.result(timeout=min(3, picker_remaining()))
            except TimeoutError:
                picker = getattr(plan, 'search_picks', None)
                if picker is None:
                    raise
                answer = dict(choice=None, watched='any', list='taste', picks=picker)
                complete = False
            channel = "command" if answer.get("previous") == "act" else "picker" if answer.get("previous") == "refine" else SEARCH_CHANNELS.get(answer["choice"], "picker")
            if channel != 'command' and getattr(answer['picks'], 'search_wide', False) is True:
                fallback_job = _outer.submit(resolve_fallback, answer['picks'], answer.get('watched', 'any'))
            resolve_start = time.monotonic()
            if answer["choice"] == "title" and answer.get("previous") != "act":
                if getattr(answer['picks'], 'search_wide', False) is True:
                    wide_job = _outer.submit(resolve_wide, answer['picks'], answer.get('watched', 'any'))
                data, art = title_job.result(timeout=picker_remaining()) if title_job else call(search_both, kind, query, lang)
                other_job = _outer.submit(search_both, "tv" if kind == "movie" else "movie", query, lang)
                person_job = _outer.submit(person_titles, query, kind, lang)
                try:
                    other, _ = other_job.result(timeout=picker_remaining())
                except TimeoutError:
                    other = []
                    channel = "tmdb"
                try:
                    credits = person_job.result(timeout=max(0, picker_remaining() - min(.5, SEARCH_TIMEOUT / 10)))
                except Exception:
                    credits = None
                if credits is not None:
                    titles = [it for it in data if exact(it)]
                    seen_ids = {it["id"] for it in titles}
                    for it in credits:
                        if it["id"] not in seen_ids:
                            titles.append(it)
                            seen_ids.add(it["id"])
                    channel = "tmdb"
                    if answer["picks"] is not None:
                        cancel_search_picks(answer["picks"])
                    names = names_job.result(timeout=picker_remaining())
                    return served(filtered([to_meta(it, stype, kind, names, art.get(it["id"], {}), lang=lang)
                                            for it in titles if not it.get("adult")])[:15])
                if any(exact(it) for it in data + other):
                    channel = "tmdb"
                if not data and other:
                    channel = "tmdb"
                    return served([])
            if channel == "tmdb" and not data:
                data, art = title_job.result(timeout=picker_remaining()) if title_job else call(search_both, kind, query, lang)
            if channel == "tmdb" and not data:
                channel = "picker"
            if channel == "picker":
                with _ai_lock:
                    if answer["picks"] is None:
                        answer["picks"] = search_picks(answer.get("wish", query), history, answer["list"], stype, person, deadline=picker_deadline)
                    wide = getattr(answer["picks"], "search_wide", False) is True
                    rules_job = answer["picks"].search_rules if wide else None
                if wide and fallback_job is None:
                    fallback_job = _outer.submit(resolve_fallback, answer['picks'], answer.get('watched', 'any'))
                timeout = min(SEARCH_WIDE_TIMEOUT, picker_remaining()) if wide else picker_remaining()
                timing["resolve_ms"] += round((time.monotonic() - resolve_start) * 1000)
                resolve_start = None
                try:
                    picks = wait_wide(answer["picks"], timeout) if wide else answer["picks"].result(timeout=timeout)
                    if wide and not picks and not isinstance(picks, NoPicks):
                        wide = False
                        picks = None
                        picks = fallback_picks()
                except Exception as error:
                    if not wide:
                        raise
                    failure = failure or search_failure(error)
                    wide = False
                    picks = fallback_picks()
                if timing.get("picker_ms") != "cached":
                    timing["picker_ms"] = getattr(answer["picks"], "search_timing", {}).get("ms", 0)
                resolve_start = time.monotonic()
        except Exception as error:
            failed_search(error)
        if answer is not None and channel == "command":
            with search_commands._LOCK:
                with _ai_lock:
                    scope, turn = request
                    if _search_turns.get(scope) != (search_key(query), turn):
                        return {"metas": []}
                command = (search_commands.special(command_context(), query, stype, routed=True, results=context)
                           if answer.get("previous") == "act" else search_commands.special(command_context(), query, stype, routed=True))
            if command is not None:
                served(command.get("metas", []))
                return command
            answer = None
    try:
        names = names_job.result(timeout=picker_remaining())
    except TimeoutError:
        complete = False
        names = {}
    if picks is not None and wide and not isinstance(picks, NoPicks):
        intent = answer.get("watched", "any")
        if wide_job is not None:
            try:
                metas = wide_job.result(timeout=picker_remaining())
            except Exception:
                metas = []
        else:
            metas = wide_metas(picks, names, intent)
        if wide_job is not None:
            metas = wide_filtered(metas, intent)
        if metas:
            try:
                rules = rules_job.result(timeout=picker_remaining())
            except Exception:
                rules = {"from": None, "to": None, "exclude": []}
            try:
                metas = call(wide_rank, metas, rules, person, references, _deadline=local_deadline, deadline=local_deadline)
            except TimeoutError as error:
                metas = []
                failed_search(error)
        if metas:
            return served(metas)
        try:
            picks = fallback_picks()
        except Exception as error:
            picks = None
            failed_search(error)
        else:
            if timing.get("picker_ms") != "cached":
                timing["picker_ms"] = getattr(answer["picks"], "search_timing", {}).get("ms", 0)
    if isinstance(picks, NoPicks):
        if not failure:
            return served([])
        picks = None
    if picks is not None:
        if fallback_job is not None:
            try:
                fallback_metas = fallback_job.result(timeout=picker_remaining())
            except Exception as error:
                failed_search(error)
        intent = answer.get("watched", "any") if answer is not None else "any"
        if intent == "seen" and picks is not None:
            metas = fallback_metas if fallback_metas is not None else ai_search(stype, kind, picks, lang, names, history=history, deadline=picker_deadline)
            if answer.get("list", "taste") == "taste":
                try:
                    metas = call(taste_rerank, metas, _deadline=local_deadline, deadline=local_deadline, person=person)
                except TimeoutError as error:
                    metas = []
                    failed_search(error)
            metas = filtered(metas)[:15]
            if metas:
                return served(metas)
        if picks or (intent == "unseen" and picks is not None):
            metas = fallback_metas if fallback_metas is not None else ai_search(stype, kind, picks, lang, names, deadline=picker_deadline)
            seen = []
            if p:
                metas = [x for x in metas if x["id"] not in p.get("negative", ())]
                seen = [x for x in metas if x["id"] in p.get("seen", ())]
                metas = [x for x in metas if x["id"] not in p.get("seen", ())]
            if answer.get("list", "taste") == "taste":
                try:
                    metas = call(taste_rerank, metas, _deadline=local_deadline, deadline=local_deadline, person=person)
                except TimeoutError as error:
                    metas = []
                    failed_search(error)
            metas = filtered(metas if intent == "unseen" else metas + seen)
            if intent == "unseen" and len(metas) < 8:
                exclude = set(p.get("seen", ())) | set(p.get("negative", ())) | {x["id"] for x in metas}
                for seed in list(metas):
                    try:
                        more, art = call(with_art, f"{kind}/{seed['id'][5:]}/recommendations", lang, include_adult="false")
                    except Exception:
                        continue
                    more = [it for it in more if f"tmdb:{it['id']}" not in exclude and not it.get("adult")
                            and it.get("media_type", kind) == kind]
                    for meta in filtered([to_meta(it, stype, kind, names, art.get(it["id"], {}), AI_MARK, lang=lang)
                                          for it in more]):
                        if meta["id"] not in exclude:
                            exclude.add(meta["id"])
                            metas.append(meta)
                            if len(metas) >= 15:
                                break
                    if len(metas) >= 15:
                        break
            if metas:
                return served(metas[:15])

    if plan is not None and not data:
        try:
            data, art = title_job.result(timeout=picker_remaining()) if title_job else call(search_both, kind, query, lang)
        except Exception:
            complete = False
    ranked = sorted((it for it in data if not it.get("adult")), key=lambda it: (exact(it), it.get("popularity") or 0), reverse=True)
    metas = [to_meta(it, stype, kind, names, art.get(it["id"], {}), lang=lang) for it in ranked]
    if (plan is not None and channel == "picker") or (not title_only and not OR_KEY):
        with _ai_lock:
            for key, (_, future) in list(_ai.items()):
                if future is plan:
                    del _ai[key]
        if any(exact(it) for it in ranked):
            return served(filtered(metas))
        if not OR_KEY:
            failure = "config"
        intent = (answer or {}).get("watched", "any")
        exclude = set(p.get("negative", ())) | (set(p.get("seen", ())) if intent != "seen" else set())
        allowed = {e["key"] for e in history if e["stype"] == stype}
        used = set()
        def eligible(meta):
            key = meta["id"]
            return (key not in used and key not in exclude and (intent != "seen" or key in allowed)
                    and (kids_age(who) is None or child_ok(meta, who)))
        def append_matches(items):
            for meta in items:
                if remaining() <= 0:
                    break
                key = meta["id"]
                if not eligible(meta):
                    continue
                used.add(key)
                matches.append(meta)
                if len(matches) >= 15:
                    break
        matches = []
        append_matches(metas)
        if len(matches) < 15 and picker_remaining() > 0:
            try:
                keywords = call(tmdb, "search/keyword", query=query, _timeout=picker_remaining()).get("results", [])[:5]
                if keywords and picker_remaining() > 0:
                    items = call(tmdb, f"discover/{kind}", with_keywords="|".join(str(it["id"]) for it in keywords),
                                 language=lang, include_adult="false", _timeout=picker_remaining()).get("results", [])
                    append_matches([to_meta(it, stype, kind, names, {}, lang=lang) for it in items if not it.get("adult")])
            except Exception:
                pass
        if len(matches) < 15:
            pool = [x for x in (list(row_pool(lang)) or list(row_pool(LANG)) or list(row_pool("en-US")))
                    if x.get("type") == stype and eligible(x)]
            model = _model["m"]
            scores = {}
            for x in pool:
                timeout = local_deadline - time.monotonic()
                if timeout <= 0 or not model:
                    break
                try:
                    scores[x["id"]] = _outer.submit(score_key, model, x["id"], person, kind=kind).result(timeout=timeout)[0]
                except TimeoutError:
                    break
            pool.sort(key=lambda x: (scores.get(x["id"]) is None, -(scores.get(x["id"]) or 0)))
            append_matches(pool)
        reason = failure or "empty"
        print(f"search fallback: {reason} ({stype})", flush=True)
        if failure:
            label_lang = CONFIG['region'].get('row_lang', lang)
            notice = t("search.unavailable", lang=label_lang, reason=t("search.reason." + failure, lang=label_lang))
            matches = search_commands.card(command_context(), notice, stype, title=t("search.unavailable.title", lang=label_lang))["metas"] + matches
        complete = True
        return served(matches[:15])
    metas = filtered(metas)
    return served(metas)


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
# The taste model (fingerprints + facets, see below) orders rows and can add short reasons to cards.

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WD_SHORT = ["weekday." + str(i) for i in range(1, 8)]
MONTHS = ["month." + str(i) for i in range(1, 13)]
ANIME_SEASONS = ["WINTER", "SPRING", "SUMMER", "FALL"]
SEASON_SUFFIX = re.compile(r"\s*[:\-]?\s*(season\s*\d+.*|\d+(st|nd|rd|th)\s+season.*|part\s*\d+|cour\s*\d+|final season.*|"
                           r"\b(ii|iii|iv)\b|\b\d+$)\s*$", re.I)
PERSONAL_SYS = ("You curate the personal rows of a household's streaming home screen (movies, TV series, anime). "
                "Reply with exactly these sections and nothing else:\n{sections}\n"
                "Line format: Title|Year|m or s|why|for   (m=movie, s=series). for = {targets} or together. why = a short reason in {language}, max 8 words, "
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
SHELF_ROWS = [rid(f'shelf_{p}_{n}') for p in (*PEOPLE, 'together') for n in range(1, 11)]

from skald.aio_refresh import BackgroundRefresh
_aio_refresh = BackgroundRefresh(DATA, lambda line: print(line, file=sys.stderr, flush=True),
                                 server=lambda: SimpleNamespace(**globals()))


def aio_rows_changed():
    _aio_refresh.schedule()


_health = None
_health_lock = threading.Lock()
_health_at = 0
_feed_thread = None
_feed_thread_lock = threading.Lock()
_health_jobs = set()
_health_poll_runs = {}
_health_poll_backoff = {}
_health_served_rows = {}
_health_row_sources = {}
_health_started = time.time()


def start_feed_worker():
    global _feed_thread
    with _feed_thread_lock:
        if _feed_thread is None or not _feed_thread.is_alive():
            _feed_thread = threading.Thread(target=feed_worker, daemon=True, name='skald-feeds')
            _feed_thread.start()
    return True


def health_poll_result(cid, error, backoff=0):
    if _health is not None:
        from skald.health import key
        with _health_lock:
            prior = _health.state['polls'].get(key('connector', cid), {}).get('failures', 0)
            _health_poll_runs[cid] = _health_poll_runs.get(cid, prior) + 1 if error else 0
            _health_poll_backoff[cid] = backoff


def health_snapshot():
    from skald import health
    hashes = {name: p.get('hash', '') for name, p in _profiles.items()}
    periods = {'episodes': 3 * 3600, 'upcoming': 6 * 3600, 'airing': 6 * 3600,
               'top10': 12 * 3600, 'personal': 12 * 3600, 'week': 7 * 86400, 'weekpicks': 7 * 86400,
               'docsai': 7 * 86400, 'shelves': 60}
    feeds = {}
    with _feeds_lock:
        records = dict(_feeds)
        empty = set(_feeds_empty)
    for job, cids, stamp, gap, build in job_specs(local_now(), hashes):
        for lang in list(_langs):
            for cid in cids:
                if (cid, lang) in empty:
                    continue
                rec = records.get((cid, lang), {})
                feeds[health.key('feed', (job, cid, lang))] = {
                    'built': rec.get('built', _health_started), 'interval': periods.get(job, 86400),
                    'label': cid + ' / ' + lang}
    with _connectors_lock:
        connectors = {health.key('connector', cid): {
            'polls': c['counts']['polls'], 'failed': bool(c['health']['error']),
            'failures': _health_poll_runs.get(cid, _health.state['polls'].get(health.key('connector', cid), {}).get('failures', 0) if _health else 0)}
            for cid, c in _connectors.items() if c['health']['state'] != 'disabled'}
    with _usage_lock:
        _decision_roll()
        monthly = ai_monthly_usd()
        budget = _usage['month_cost'] / monthly if monthly > 0 else 1
        over = bool(_decide_over) or _usage['month_cost'] >= monthly
        for lane, cap in AI_CAPS.items():
            if cap > 0:
                budget = max(budget, sum(n for k, n in _usage['calls'].items() if k.startswith(lane + ':')) / cap)
        for name, spec in DECISIONS.items():
            if spec['cap']:
                budget = max(budget, _usage['calls'].get(f"{spec['lane']}:{name}", 0) / spec['cap'])
    with _health_lock:
        rows = dict(_health_served_rows)
    return {'worker_alive': _feed_thread is not None and _feed_thread.is_alive(),
            'feeds': feeds, 'connectors': connectors, 'budget_ratio': budget,
            'budget_over': over, 'aio_pending': _aio_refresh.failed or _aio_refresh.pending or
                health.read_json(os.path.join(DATA, 'setup.json'), {}).get('aiostreams_pending', False),
            'rows': rows}


def health_actions():
    from skald.health import key
    actions = {'worker': start_feed_worker, 'aiostreams': _aio_refresh.repair}
    hashes = {name: p.get('hash', '') for name, p in _profiles.items()}
    def rebuild(job):
        with _health_lock:
            _health_jobs.add(job)
        return 'pending'
    for job, cids, stamp, gap, build in job_specs(local_now(), hashes):
        for cid in cids:
            for lang in list(_langs):
                actions[key('feed', (job, cid, lang))] = lambda job=job: rebuild(job)
            with _health_lock:
                for row, source in _health_row_sources.items():
                    if source == cid:
                        actions[row] = lambda job=job: rebuild(job)
    def retry(cid):
        global _connectors_generation
        with _connectors_lock:
            c = _connectors.get(cid)
            if not c:
                return False
            c['next_due'] = max(time.time() + 60, c.get('next_due') or 0, _health_poll_backoff.get(cid, 0))
            _connectors_generation += 1
        return 'pending'
    with _connectors_lock:
        for cid in _connectors:
            actions[key('connector', cid)] = lambda cid=cid: retry(cid)
    return actions


_health_tick_lock = threading.Lock()


def health_cache_changed(name, records):
    def unpack(values):
        return {tuple(k.split('|')): v for k, v in values.items()}
    with _pin_lock, _home_lock, _feeds_lock:
        if name == 'shown_scopes.json':
            _shown_scoped.clear()
            _shown_scoped.update({scope: unpack(records[scope]) if scope in records else _shown.copy()
                                  for scope in SCOPES})
        else:
            store = _feeds if name == 'feeds.json' else _shown
            old = dict(store)
            store.clear()
            store.update(unpack(records))
            if name == 'feeds.json':
                _langs.update(lang for cid, lang in store)
            dependents = [*_shown_scoped.values()] + ([_shown] if name == 'feeds.json' else [])
            for dependent in dependents:
                for key, value in list(dependent.items()):
                    if old.get(key) is value:
                        if key in store:
                            dependent[key] = store[key]
                        else:
                            dependent.pop(key)
        _home_cache.clear()
        _pin_cache.clear()


def health_tick():
    global _health_at
    if _health is None or not _health_tick_lock.acquire(blocking=False):
        return
    try:
        if time.time() - _health_at < 300:
            return
        _health_at = time.time()
        _health.tick(health_snapshot(), health_actions())
    except Exception:
        _daily_log('health check failed; check local state permissions')
    finally:
        _health_tick_lock.release()


def health_supervisor():
    while True:
        health_tick()
        time.sleep(60)


def shelf_change(scope, op, **kwargs):
    return shelves.change(SimpleNamespace(**globals()), scope, op, **kwargs)


def shelf_list(scope=None):
    return shelves.listing(SimpleNamespace(**globals()), scope)


def shelf_feed(lang, now=None, build=True):
    return shelves.fill(SimpleNamespace(**globals()), lang, now, build)


def shelf_rows():
    s, doc = SimpleNamespace(**globals()), shelves.read(SimpleNamespace(**globals()))
    return [(cid, ('tv',), next(w['name'] or w['text'][:100] for w in doc['wishes'] if (w['scope'], w['slot']) == (scope, n)))
            for cid, scope, n in shelves.slots(s, getattr(_scoping, 'scope', None), doc=doc)]


def shelf_items(cid, typ, lang, who=None):
    if cid not in {r[0] for r in shelf_rows()}:
        return []
    metas = shelf_feed(lang, build=False).get(cid, {}).get('tv', [])
    hidden = deferred_keys(who_watching()[0] if who is None else who)
    metas = [x for x in metas if (typ == 'tv' or x['type'] == typ) and x['id'] not in hidden and preference_ok(x, cid, safety_subject(who))]
    pinned = _feeds.get((rid('shelfraw_'+cid.removeprefix(rid('shelf_'))), LANG), {}).get('status') == 'pinned'
    return metas if pinned or len(metas) >= ROW_MIN else []

ROW_WINDOW = 12
ROW_MIN = 6
ROW_HERO_MAX = 2
HOME_FRANCHISE_ROWS = 2
DOCS = [rid('doctrend'), rid('wonder'), rid('cosmos'), rid('history'), rid('meaning')]
STUDIO_ROWS = [rid('hbo'), rid('paramount')]
BECAUSE_ROWS = [rid('because'), rid('because2'), rid('because3')]
READY_ROWS = [rid('ready1'), rid('ready2')]
THEMED_ROWS = set(MOODS_ROWS + LANE_ROWS + DOCS + STUDIO_ROWS + BECAUSE_ROWS + [rid('leap')])
# Keep the player catalog order in sync (Top 10 has two catalogs).
DEDUPE_ORDER = ([rid('episodes'), rid('foryou'), rid('week'), rid('weekpicks'), rid('because'), *READY_ROWS, rid('catchup'), rid('upcoming'),
                 rid('leap')] + list(ROW_OWNER) + [rid('rewatch'), rid('top10'), rid('digital'), rid('toprated')] + STUDIO_ROWS + MOODS_ROWS + BECAUSE_ROWS[1:] + DOCS + LANES
                + [rid('airing'), rid('nextseason'), rid('newtoyou')])
LAB_ROWS = []
_lab_visible = ()
_LAB_ORDER = list(DEDUPE_ORDER)
GUEST_ROW_POLICY = {
    rid('episodes'): False,  # Resume household plays.
    rid('foryou'): False,  # Household taste shortlist.
    rid('week'): False,  # Joint member scores.
    rid('weekpicks'): False,  # Personal adult picks.
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
    rid('newtoyou'): False,  # Member taste rotation.
}
GUEST_ROW_POLICY.update(dict.fromkeys([*ROW_OWNER, *BECAUSE_ROWS, *READY_ROWS], False))
GUEST_ROW_POLICY.update({rid('digital'): True, rid('toprated'): True})
GUEST_ROWS = {cid for cid, allowed in GUEST_ROW_POLICY.items() if allowed}
MIXED = ({rid('foryou'), rid('upcoming'), rid('catchup'), rid('rewatch'), rid('because'), rid('week'), rid('weekpicks'),
          rid('leap'), rid('family'), rid('newtoyou')} | set(BECAUSE_ROWS) | set(READY_ROWS) | set(ROW_OWNER) | {rid(p) for p in ARCHIVED} | set(LANES) | set(MOODS_ROWS) | set(SHELF_ROWS) | set(DOCS) | set(STUDIO_ROWS))
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
_erasure_generation = 0
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


def row_groups():
    lab_catalogs()
    return {'people': [*ROW_OWNER, *[rid(p) for p in ARCHIVED]],
              'moods': MOODS_ROWS, 'lanes': LANE_ROWS, 'docs': [*DOCS, rid('docsai')],
              'studios': STUDIO_ROWS, 'anime': [rid('airing'), rid('nextseason')],
              'shelves': SHELF_ROWS, 'lab': LAB_ROWS, 'rewatch': [rid('rewatch'), rid('rewatchai')], 'because': BECAUSE_ROWS, 'ready': READY_ROWS}


def default_rows(cfg=None):
    cfg = CONFIG if cfg is None else cfg
    return (rowplan.capped(cfg) and not any(key in cfg['household'] for key in ('row_order', 'row_labels'))
            and kids_age(safety_subject()) is None)


def row_scope_ok(cid):
    scope = getattr(_scoping, 'scope', None)
    if ROW_OWNER.get(cid) in CONFIG['household'].get('away', []):
        return scope == ROW_OWNER[cid]
    if cid in SHELF_ROWS and CONFIG['household'].get('away'):
        wish = next((w for w in shelves.read(command_context())['wishes'] if rid(f"shelf_{w['scope']}_{w['slot']}") == cid), None)
        if wish and any(p in CONFIG['household'].get('away', []) and scope != p for p in (wish['scope'], wish.get('with_person'))):
            return False
    if cid.startswith(rid('lab_')):
        if kids_age(safety_subject()) is not None:
            return False
        idea = next((i for i in lab.read(DATA)['ideas'] if rid('lab_'+i['id']) == cid), None)
        return bool(idea and (scope in idea['members'] or not set(idea['members']) <= set(CONFIG['household'].get('away', [])))
                    and (scope is None or scope in idea['members'] or 'together' in idea['members']))
    return cid not in ROW_OWNER or scope not in PEOPLE or scope == ROW_OWNER[cid]


def row_enabled(cid):
    if cid in CONFIG['household'].get('retired_rows', []):
        return False
    if (cid == rid('lab') or cid.startswith(rid('lab_'))) and lab.paused(CONFIG):
        return False
    keys = CONFIG['household'].get('rows')
    if cid == rid('lab'):
        return keys is None or 'lab' in keys
    if keys is None:
        return cid not in (rid('digital'), rid('newtoyou')) and (not default_rows() or cid not in STUDIO_ROWS)
    groups = row_groups()
    return any(cid in groups.get(key, [rid(key)]) for key in keys)


def feed_rows(lang=LANG, profiles=True, who=None, *, home=None):
    """Home rows in display order: (catalog id, types, name or {type: name})."""
    if household_erased(DATA):
        return []
    lab_catalogs()
    nxt, nyear = anime_season(local_now(), 1)

    def built(cid, fallback):
        if not row_enabled(cid) or kids_age(safety_subject()) is not None:
            return fallback
        names = feed(cid, lang).get("names") or {}
        return row_names.describe(cid, names, CONFIG, row_lang(), _learn.get("lanes", {}))["label"] if names else fallback

    rows = [
        (rid('episodes'), ("series",), t("row.episodes")),
        (rid('foryou'), MIX, t("row.foryou")),
        (rid('week'), MIX, t("row.week")),
        (rid('weekpicks'), MIX, row_names.describe(rid('weekpicks'), {}, CONFIG, row_lang())['label']),
        (rid('upcoming'), MIX, t("row.upcoming")),
        (rid('catchup'), MIX, t("row.catchup")),
        (rid('rewatch'), MIX, t("row.rewatch")),
        (rid('because'), MIX, built(rid('because'), t("row.because"))),
        (rid('leap'), MIX, built(rid('leap'), t("row.leap"))),
        (rid('top10'), ("movie", "series"), {"movie": t("row.top10.movie"), "series": t("row.top10.series")}),
    ] + [(cid, MIX, t("row.member", person=LABELS[p])) for cid, p in ROW_OWNER.items()] + [(cid, MIX, built(cid, t("row.mood"))) for cid in MOODS_ROWS] \
      + [(cid, MIX, built(cid, t("row.because"))) for cid in BECAUSE_ROWS[1:]] \
      + [(cid, MIX, built(cid, t("row.picked"))) for cid in LANES] \
      + [(cid, MIX, t(DOC_ROWS.get(cid, DOC_TREND)[0])) for cid in DOCS] + [(cid, MIX, t(STUDIOS[cid][0])) for cid in STUDIO_ROWS] + [
        (rid('airing'), ("series",), t("row.airing")),
        (rid('nextseason'), ("series",), t("row.nextseason", season=t("season." + nxt.lower()), year=nyear)),
    ] + [(rid('newtoyou'), MIX, built(rid('newtoyou'), t("row.newtoyou")))] * row_enabled(rid('newtoyou')) \
      + [(rid(p), MIX, t("row.archived", person=label)) for p, label in ARCHIVED.items()
         if p in (active_profiles() if profiles else ())]
    rows += [(cid, MIX, lab.read(DATA)['ideas'][i]['name']) for i, cid in enumerate(LAB_ROWS)]
    from skald import ready
    scope = getattr(_scoping, 'scope', None)
    viewer = SCOPES[scope] if scope is not None else who_watching()[0] if who is None else who
    for cid in READY_ROWS:
        if row_enabled(cid) and ready.slot(SimpleNamespace(**globals()), cid, lang, viewer):
            rows.append((cid, ('tv',), row_name(cid, lang)))
    rows += shelf_rows() if any(row_enabled(cid) for cid in SHELF_ROWS) else []
    rows += [(rid('digital'), ('movie',), t("row.digital"))] * row_enabled(rid('digital'))
    rows += [(rid('toprated'), ('movie', 'series'),
              {'movie': t('row.toprated'), 'series': t('row.toprated.series')})] * row_enabled(rid('toprated'))
    scope = getattr(_scoping, "scope", None)
    if any(kids_age(p) is not None for p in PEOPLE) and (scope not in PEOPLE or kids_age(scope) is not None):
        rows.append((rid('family'), MIX, t("row.family")))
    rank = {cid: i for i, cid in enumerate(DEDUPE_ORDER)}
    keys = CONFIG['household'].get('row_order')
    groups = row_groups()
    if keys is not None:
        rank = {cid: i for i, cid in enumerate(cid for key in keys for cid in groups.get(key, [rid(key)]))}
    labels = {cid: label for key, label in CONFIG['household'].get('row_labels', {}).items()
              for cid in groups.get(key, [rid(key)])}
    rows = [(cid, types, name if cid in LAB_ROWS else labels.get(cid, name)) for cid, types, name in rows]
    rows = sorted((r for r in rows if row_enabled(r[0]) and row_scope_ok(r[0])), key=lambda r: rank.get(r[0], len(rank)))
    if default_rows() or LAB_ROWS:
        who = SCOPES[scope] if scope is not None else who_watching()[0] if who is None else who
        home = _home_rows(lang, who) if home is None else home
        rows = [r for r in rows if r[0] not in (MOODS_ROWS + LANES + DOCS if default_rows() else []) + LAB_ROWS or home.get((r[0], 'tv'))]
    if LAB_ROWS:
        labs = [r for r in rows if r[0] in LAB_ROWS]
        order = {rid('lab_'+i['id']): n for n, i in enumerate(lab.select(DATA, CONFIG, time.time(), scope if scope is not None else who))}
        labs.sort(key=lambda r: order.get(r[0], len(order)))
        rows = [r for r in rows if r[0] not in LAB_ROWS]
        position = max((i+1 for i, r in enumerate(rows) if r[0] in ROW_OWNER), default=min(8, len(rows)))
        rows[position:position] = labs
    return rows


def read_history():
    from skald.history import combine, filter_forgotten
    _refresh_learning()
    return combine(DATA, "history.json", filter_forgotten(DATA, CONFIG, "history.json", _drop_read("history")))


from skald.history import locked as history_locked, cutoffs as history_cutoffs
_history_lock = threading.RLock()


def cleanup_history(now):
    from skald.history import cleanup, enabled
    from types import SimpleNamespace
    if not enabled(CONFIG):
        return {'pruned': 0, 'summaries': 0, 'residual': 0}
    with history_locked(DATA), _history_lock, _imports_lock:
        return cleanup(SimpleNamespace(**globals()), now)


def history_cleanup_due(now):
    from skald.history import enabled
    return enabled(CONFIG)


def active_profiles():
    return [n for n in ("household", *ARCHIVED) if not (read_taste().get(n) or {}).get("archived")]


def read_taste():
    if household_erased(DATA):
        return {}
    path = os.path.join(DATA, "taste.json")
    try:
        with open(path) as fh:
            return {**DEFAULT_TASTE, **json.load(fh)}
    except FileNotFoundError:
        if rowplan._trace.get() is not None:
            return DEFAULT_TASTE
        try:
            with household_writable(DATA, household_erased) as allowed:
                if allowed:
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
    if it.get("src") == "sync:pmdb":
        return bool(it.get("played"))
    rated = e.get("wsrc") == "trakt" and it.get("rating") is not None
    rated = rated or any(opinion.get("wsrc") == "trakt" and (opinion.get("item") or {}).get("rating") is not None
                         for opinion in (e.get("opinions") or {}).values())
    if e.get("who") == "remote" or "remote" in e.get("flags", ()) or rated or it.get("favorite"):  # An opinion is not a play.
        return bool(it.get("played") or (it.get("progress") or 0) >= 0.25 or (it.get("episodes_played") or 0) >= 1)
    return e["signal"] in ("loved", "liked", "watched") or (it.get("progress") or 0) >= 0.25 or (it.get("episodes_played") or 0) >= 1


def _taste_accounts():
    return {p: auth for cid in ("trakt_api", "mdblist", "stremio", "pmdb")
            for p, auth in CONFIG["connectors"].get(cid, {}).get("accounts", {}).items() if p in PEOPLE}


def taste_drop(cid):
    from skald.history import combine, filter_forgotten
    name = os.path.basename(_drop_path(cid))
    owner = cid.split("-", 1)[1] if "-" in cid else None
    return combine(DATA, name, filter_forgotten(DATA, CONFIG, name, _drop_read(cid), owner))


def _taste_rank(value):
    it = value.get("item", value)
    weight = value.get("weight", 0)
    if "item" not in value and it.get("src") == "sync:pmdb":
        weight *= IMPORT_TRUST["pmdb"]
    completion = it.get("completion") or 0
    if it.get("type") == "movie":
        completion = completion or it.get("progress") or 1
    return (it.get("rating") is not None, bool(it.get("played")),
            completion if it.get("played") else 0,
            it.get("episodes_played") or 0,
            bool(it.get("dropped") or value.get("signal") == "abandoned"), weight)


def read_trakt():
    """Account history, attributed by configured file rather than remote item fields."""
    accounts = CONFIG["connectors"].get("trakt_api", {}).get("accounts")
    items = ([{**it, "person": person} for person in accounts if person in PEOPLE for it in taste_drop("trakt-" + person)]
             if accounts is not None else taste_drop("trakt") if TRAKT_PERSON in PEOPLE else [])
    if not any(cid in CONFIG["connectors"] for cid in ("mdblist", "stremio", "pmdb")):
        return items
    if accounts is None:
        items = [{**it, "person": TRAKT_PERSON} for it in items]
    positions = {(it["person"], it["type"], it["id"]): n for n, it in enumerate(items)}
    for source in ("mdblist", "stremio", "pmdb"):
        for person in CONFIG["connectors"].get(source, {}).get("accounts", {}):
            if person not in PEOPLE:
                continue
            for it in taste_drop(source + "-" + person):
                it = {**it, "person": person}
                n = positions.get((person, it["type"], it["id"]))
                if n is None:
                    positions[(person, it["type"], it["id"])] = len(items)
                    items.append(it)
                    continue
                old = items[n]
                if old.get("rating") is not None:
                    continue
                if source == "stremio" and it.get("rating") is not None and old.get("played"):
                    items[n] = {**old, **{f: it[f] for f in ("rating", "signal", "weight")}}
                    continue
                if (_taste_rank(it) > _taste_rank(old) if source == "pmdb" else
                        (bool(it.get("played")), it.get("weight", 0)) > (bool(old.get("played")), old.get("weight", 0))):
                    items[n] = it
    return items


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
    if cid.startswith("pmdb-") and cid[5:] in CONFIG["connectors"].get("pmdb", {}).get("accounts", {}):
        return os.path.join(DATA, cid + ".history.json")
    if cid.startswith("mdblist-") and cid[8:] in CONFIG["connectors"].get("mdblist", {}).get("accounts", {}):
        return os.path.join(DATA, cid + ".history.json")
    if cid.startswith("stremio-") and cid[8:] in CONFIG["connectors"].get("stremio", {}).get("accounts", {}):
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
        if cid in ('trakt', 'trakt_api') and 'accounts' not in cfg:
            enabled &= TRAKT_PERSON in PEOPLE
        out[cid] = _connector(cid, cfg["kind"], TRAKT_PERSON if cid in ("trakt", "trakt_api") else "both", now,
                              auth=cfg.get("auth"), disabled=not enabled)
        if cid in ("trakt_api", "mdblist", "stremio", "pmdb") and "accounts" in cfg:
            out[cid]["person"] = "both"
            out[cid]["accounts"] = {p: _connector(cid + ":" + p, "api", p, now, auth=auth, disabled=not enabled)
                                    for p, auth in cfg["accounts"].items()}
    from skald import plays
    context = SimpleNamespace(**globals())
    if plays.managed(context):
        cid = plays.connector_id(context)
        out[cid] = _connector(cid, "player", "both", now, disabled=plays.settings(CONFIG)['mode'] == 'off')
        out[cid]['interval'] = plays.settings(CONFIG)['every']
        out['history']['next_due'] = None
        out['history']['health']['state'] = 'disabled'
        for other in ('aiostreams_db', 'jellyfin', 'plex'):
            if other != cid and other in out:
                out[other]['next_due'] = None
                out[other]['health']['state'] = 'disabled'
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
        from skald import plays
        context = SimpleNamespace(**globals())
        if not (c['kind'] == 'player' and plays.managed(context) and cid == plays.connector_id(context) and plays.settings(CONFIG)['mode'] != 'off'):
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
        c["accounts"] = {p: _connector_clean(cid.split(":")[0] + ":" + p, account, now) for p, account in c["accounts"].items()
                         if p in CONFIG["connectors"].get(cid.split(":")[0], {}).get("accounts", {})}
    return c


def _connectors_save(rows, generation):
    """Serialized writes, outside the leaf lock; discard a snapshot superseded during IO."""
    if household_erased(DATA):
        return False
    rows = {cid: _connector_clean(cid, c, time.time()) for cid, c in rows.items()}
    with household_writable(DATA, household_erased) as allowed:
        if not allowed:
            return False
        fd, tmp = tempfile.mkstemp(prefix=".connectors-", dir=DATA)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"version": 1, "connectors": rows}, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        with history_locked(DATA):
            with household_writable(DATA, household_erased) as allowed:
                if generation != _connectors_generation or not allowed:
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
    with history_locked(DATA):
        erasure = _erasure_generation
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
        from skald import plays
        context = SimpleNamespace(**globals())
        if plays.managed(context):
            c = rows[plays.connector_id(context)]
            every = plays.settings(CONFIG)['every']
            if c['interval'] != every:
                c['interval'] = every
                if c['health']['state'] != 'disabled':
                    c['next_due'] = c['last_sync']+every if c['last_sync'] else now
            if c['health']['state'] != 'disabled':
                c['next_due'] = max(c['next_due'] or now, plays.state(context).get('backoff_until', 0))
        with history_locked(DATA), _connectors_lock:
            if erasure != _erasure_generation:
                return
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


def forget_connector(person):
    global _connectors_generation, _connectors_path
    with _connectors_lock:
        for cid, connector in list(_connectors.items()):
            if connector.get('person') == person:
                _connectors.pop(cid)
            elif person in connector.get('accounts', {}):
                connector['accounts'].pop(person)
                if not connector['accounts']:
                    _connectors.pop(cid)
        _connectors_generation += 1
        _connectors_path = None


def connector_next(c, new, now):
    from skald import plays
    context = SimpleNamespace(**globals())
    if c['id'] == plays.connector_id(context) and plays.managed(context):
        every = plays.settings(CONFIG)['every']
        return every, now+every
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
    items = [it for it in _drop_read("history", c.get("_stamp")) if it.get('profile') not in CONFIG['household'].get('retired', [])]
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
    content = json.dumps(_trakt_bundle(bundle)) + "\n"
    close_directory = directory is None
    if close_directory:
        directory = _credentials_dir()
    temp = ".trakt-auth-" + secrets.token_hex(16)
    try:
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            try:
                try:
                    os.close(_open_auth_file(name, directory))
                except FileNotFoundError:
                    pass
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(content)
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
    finally:
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


def _http_get(url, headers=None, *, paged=False, body=None, budget=None, text=False):
    """Bound JSON or text transport; a shared budget caps an entire poll, including all endpoints."""
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
            if text:
                return raw.decode('utf-8')
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
    with history_locked(DATA), _history_lock:
        return _connector_write_locked(name, doc, indent=indent, newline=newline)


def _connector_write_locked(name, doc, *, indent=None, newline=False):
    from skald.history import sources, filter_forgotten, clear_connector_caches
    if getattr(_upstream, 'erasure_generation', _erasure_generation) != _erasure_generation:
        return False
    origin = name + ".json"
    if origin not in {filename for filename, _, _ in sources(CONFIG)}:
        return False
    for filename, _, person in sources(CONFIG):
        if filename == origin and "items" in doc:
            items = filter_forgotten(DATA, CONFIG, origin, doc["items"], person)
            if items != doc["items"]:
                doc = {**doc, "items": items}
                clear_connector_caches(doc)
            break
    content = (json.dumps(doc, ensure_ascii=indent is None, indent=indent, allow_nan=False) + ("\n" if newline else "")).encode("utf-8")
    path = os.path.join(DATA, name + ".json")
    try:
        with open(path, "rb") as fh:
            if fh.read() == content:
                return False
    except FileNotFoundError:
        pass
    with household_writable(DATA, household_erased) as allowed:
        if not allowed:
            return False
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


BACKFILL_PLAYS = 5


def _aiostreams_db(options):
    from skald.plays import database
    return database(SimpleNamespace(**globals()), options)


class Unsupported(ValueError):
    pass


def _player_history(records, profile, cid):
    minutes = Counter(at // 60_000 for typ, tid, at, played, favorite, progress, episode in records
                      if typ == "series" and episode is not None and played and at is not None)
    backfill = {minute for minute, count in minutes.items() if count >= BACKFILL_PLAYS}
    items, dropped = {}, 0
    for typ, tid, at, played, favorite, progress, episode in records:
        if type(tid) not in (str, int) or not re.fullmatch(r"[0-9]+", str(tid)) or int(tid) <= 0:
            dropped += 1
            continue
        key = (typ, "tmdb:" + str(int(tid)))
        item = items.setdefault(key, {"profile": profile, "type": typ, "id": key[1], "at": None,
            "played": False, "favorite": False, "progress": 0, "entries": 0, "rating": None,
            "likes": None, "dropped": False})
        if at is not None and at // 60_000 in backfill:
            item["backfilled"] = True
        else:
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
            if profile not in CONFIG['household'].get('retired', [])
            for item in _jellyfin({**shared, "user_id": uid, "profile": profile})["items"]]}
    if options.get('profile') in CONFIG['household'].get('retired', []):
        return {'items': []}
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
    if options.get('profile') in CONFIG['household'].get('retired', []):
        return {'items': []}
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


def player_history_active():
    from skald import plays
    return CONFIG['history']['source'] in HISTORY_ADAPTERS or plays.managed(SimpleNamespace(**globals()))


def _connector_player(c, now):
    from skald import plays
    context = SimpleNamespace(**globals())
    if plays.managed(context) and c['id'] == plays.connector_id(context):
        result = plays.check(context, now)
        if result['path'] == 'not connected':
            return [], c['cursor'], dict(events=c['counts']['events'], error=result.get('error') or 'Unsupported',
                                         backoff_until=result['backoff_until'])
        doc = {'items': read_history()}
    else:
        source = CONFIG["history"]["source"]
        doc = HISTORY_ADAPTERS[source](CONFIG["connectors"][c["id"]]["options"])
        doc['items'] += [it for it in _drop_read('history') if it.get('profile') in CONFIG['household'].get('retired', [])]
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


def _trakt_validate_export(data, *, unknown_dates=False):
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
                if any(type(minute) is not int and not (unknown_dates and minute is None) for minute in plays):
                    raise ValueError(f"{where}.plays must contain integer epoch minutes")
            else:
                total = row.get("eps_total")
                if total is not None and (type(total) is not int or total < 0):
                    raise ValueError(f"{where}.eps_total must be a nonnegative integer or null")
                for episode in plays:
                    if (
                        not isinstance(episode, list) or len(episode) != 4
                        or any(type(value) is not int for value in episode[:2])
                        or type(episode[2]) is not int and not (unknown_dates and episode[2] is None)
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


def _trakt_import_history(data, now, *, unknown_dates=False):
    _trakt_validate_export(data, unknown_dates=unknown_dates)
    minutes = Counter(minute for movie in data["movies"] for minute in movie["plays"])
    minutes.update(episode[2] for show in data["shows"] for episode in show["eps"])
    # shortcut: same-minute batches of >=5 are backfill; add burst detection if imports spread out.
    backfill = {minute for minute, count in minutes.items() if count >= BACKFILL_PLAYS and minute is not None}
    items = []
    for kind in ("movies", "shows"):
        for row in data[kind]:
            series = kind == "shows"
            plays = [episode[2] for episode in row["eps"]] if series else row["plays"]
            real = [minute for minute in plays if minute not in backfill and minute is not None]
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
            if plays and all(minute in backfill for minute in plays):
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



class PMDBKeyRejected(ValueError):
    code = 401


class PMDBRateLimited(ValueError):
    code = 429

    def __init__(self, delay):
        self.headers = {"Retry-After": str(delay)}


class PMDBTimeout(TimeoutError):
    pass


class PMDBBlocked(TimeoutError):
    pass


PMDB_ERRORS = {"PMDBKeyRejected": "PublicMetaDB key rejected; reconnect with skald setup --connect pmdb",
               "PMDBBlocked": "PublicMetaDB blocked this server with a bot check; the key was not checked. "
                              "Ask PublicMetaDB to allow API requests from your server",
               "PMDBRateLimited": "PublicMetaDB rate limit; sync stopped, try later",
               "PMDBTimeout": "PublicMetaDB timeout; try later",
               "SchemaMismatch": "PublicMetaDB malformed response; last good history kept"}


def _pmdb_pages(path, key, budget):
    items, expected = [], None
    for page in range(1, CONNECTOR_HTTP_PAGES + 1):
        try:
            value = _http_get("https://publicmetadb.com/api/external" + path + "?" +
                              urllib.parse.urlencode({"page": page, "perPage": 500}),
                              {"Authorization": "Bearer " + key}, budget=budget)
        except urllib.error.HTTPError as error:
            if error.code == 403 and (error.headers.get("cf-mitigated") or
                    "text/html" in error.headers.get("Content-Type", "").lower()):
                raise PMDBBlocked(PMDB_ERRORS["PMDBBlocked"]) from None
            if error.code in (401, 403):
                raise PMDBKeyRejected() from None
            if error.code == 429:
                try:
                    delay = float(error.headers.get("Retry-After", CONNECTOR_MIN))
                except (TypeError, ValueError):
                    delay = CONNECTOR_MIN
                delay = min(CONNECTOR_IDLE_MAX, max(CONNECTOR_MIN, delay)) if math.isfinite(delay) else CONNECTOR_MIN
                raise PMDBRateLimited(delay) from None
            raise ValueError("PublicMetaDB request failed") from None
        except (TimeoutError, urllib.error.URLError) as error:
            if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                raise PMDBTimeout() from None
            raise ValueError("PublicMetaDB request failed") from None
        except (ByteCapExceeded, PageCapExceeded):
            raise
        except Exception:
            raise SchemaMismatch() from None
        if not isinstance(value, dict) or any(type(value.get(f)) is not int
                for f in ("total", "page", "perPage", "totalPages")):
            raise SchemaMismatch()
        total, size, pages = value["total"], value["perPage"], value["totalPages"]
        if pages > CONNECTOR_HTTP_PAGES:
            raise PageCapExceeded()
        batch = value.get("items")
        if total < 0 or not 1 <= size <= 500 or value["page"] != page or not isinstance(batch, list) or \
                any(not isinstance(row, dict) for row in batch) or \
                pages != (total + size - 1) // size and not (total == 0 and pages == 1) or \
                len(batch) != min(size, max(0, total - (page - 1) * size)) or \
                expected is not None and expected != (total, size, pages):
            raise SchemaMismatch()
        expected = total, size, pages
        items.extend(batch)
        if page >= pages:
            return items
    raise PageCapExceeded()


def _pmdb_time(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaMismatch()
    at = _connector_time(value)
    if at is None or not math.isfinite(at):
        raise SchemaMismatch()
    return at


def _pmdb_items(watched, resume, dropped, watchlist, now, *, readonly=False):
    grouped, states = {"movies": {}, "shows": {}}, {}
    recent, latest = 0, 0
    for source, rows in (("watchlist", watchlist), ("resume", resume), ("watched", watched), ("dropped", dropped)):
        seen = set()
        for row in rows:
            tid, kind, ident = row.get("tmdb_id"), row.get("media_type"), row.get("id")
            if type(tid) is not int or tid <= 0 or kind not in ("movie", "tv") or \
                    type(ident) not in (int, str) or not str(ident) or ident in seen:
                raise SchemaMismatch()
            seen.add(ident)
            episode = [row.get("season"), row.get("episode")]
            if kind == "tv" and source in ("watched", "resume") and any(v is not None for v in episode):
                if any(type(v) is not int for v in episode) or episode[0] < 0 or episode[1] < 1:
                    raise SchemaMismatch()
            elif kind == "tv" and source == "watched":
                raise SchemaMismatch()
            at = _pmdb_time(row.get("watched_at") if source == "watched" else
                            row.get("updated") or row.get("created"))
            latest = max(latest, at or 0)
            bucket = "movies" if kind == "movie" else "shows"
            if tid not in grouped[bucket]:
                details = None if readonly else tmdb_details({"id": "tmdb:" + str(tid),
                                                             "type": "movie" if kind == "movie" else "series"})
                det = details[1] if details else {}
                grouped[bucket][tid] = {"tmdb": tid, "title": det.get("title") or det.get("name") or "Unknown title",
                    "year": int((det.get("release_date") or det.get("first_air_date"))[:4]) if
                            (det.get("release_date") or det.get("first_air_date") or "")[:4].isdigit() else None,
                    "genres": genre_names([g["name"] for g in det.get("genres") or []]),
                    "lang": det.get("original_language"), "status": (det.get("status") or "").lower(),
                    **({"plays": []} if kind == "movie" else {"eps": [], "eps_total": det.get("number_of_episodes")})}
            title = grouped[bucket][tid]
            if source == "watched":
                minute = math.floor(at / 60 + .5) if at is not None else None
                title["plays" if kind == "movie" else "eps"].append(minute if kind == "movie" else [*episode, minute, 0])
                recent += at is not None and now - 30 * DAY <= at <= now
            elif source == "resume":
                progress = row.get("progress")
                if type(progress) not in (int, float) or not math.isfinite(progress) or not 0 <= progress <= 100:
                    raise SchemaMismatch()
                states[(kind, tid)] = {"progress": progress / 100, "at": at * 1000 if at is not None else None,
                    **({"resume_episode": episode} if kind == "tv" and episode[0] is not None else {})}
            elif source == "dropped":
                states.setdefault((kind, tid), {}).update(dropped=True, at=at * 1000 if at is not None else None)
    exported = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    doc = _trakt_import_history({"exported_at": exported,
        **{bucket: list(titles.values()) for bucket, titles in grouped.items()}}, now, unknown_dates=True)
    doc["source"] = "pmdb"
    for it in doc["items"]:
        kind = "movie" if it["type"] == "movie" else "tv"
        state = states.get((kind, int(it["id"][5:])), {})
        if it["played"] and not state.get("dropped"):
            state = {k: v for k, v in state.items() if k != "at"}
        it.update(src="sync:pmdb", **state)
        if state.get("dropped"):
            it.update(signal="abandoned", weight=-1.5)
        elif not it["played"]:
            it.update(signal="interest", weight=1)
    return doc, recent, latest


def _connector_pmdb(c, now, budget=None, *, readonly=False):
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    if "accounts" in c:
        return _connector_accounts(c, now, budget, _connector_pmdb)
    key = _resolve_auth(c["auth"])
    watched, resume, dropped, lists = [_pmdb_pages(path, key, budget)
                                      for path in ("/watched", "/resume", "/dropped", "/lists")]
    watchlist = []
    for row in lists:
        ident = row.get("id")
        if type(ident) not in (int, str) or not str(ident) or row.get("type") not in ("watchlist", "custom"):
            raise SchemaMismatch()
        if row["type"] == "watchlist":
            watchlist.extend(_pmdb_pages("/lists/" + urllib.parse.quote(str(ident), safe="") + "/items", key, budget))
    try:
        doc, recent, latest = _pmdb_items(watched, resume, dropped, watchlist, now, readonly=readonly)
    except Exception:
        raise SchemaMismatch() from None
    if readonly:
        return doc, None, {"plays_30d": recent}
    name = "pmdb-" + c["person"] + ".history"
    try:
        with open(os.path.join(DATA, name + ".json")) as fh:
            old = json.load(fh)
    except FileNotFoundError:
        old = {}
    from skald.history import filter_forgotten
    doc["items"] = filter_forgotten(DATA, CONFIG, name + ".json", doc["items"], c["person"])
    previous = {(it["type"], it["id"]): it for it in old.get("items", [])}
    changed = [it for it in doc["items"] if it != previous.get((it["type"], it["id"]))]
    if doc["items"] != old.get("items"):
        _connector_write(name, doc, indent=2, newline=True)
    events = list(trakt_events(now, [{**it, "person": c["person"]} for it in changed]))
    return events, [latest, len(doc["items"])], {"events": len(doc["items"]), "error": None}


class MDBListKeyRejected(ValueError):
    code = 401

    def __init__(self):
        super().__init__("MDBList key rejected; reconnect with skald setup --connect mdblist")


class MDBListRateLimited(ValueError):
    code = 429
    headers = {}

    def __init__(self):
        super().__init__("MDBList rate limit; sync stopped, try later")


def _mdblist_http(path, key, *, offset=None, budget=None):
    params = {"apikey": key}
    if offset is not None:
        params.update(limit=500, offset=offset)
    try:
        return _http_get("https://api.mdblist.com" + path + "?" + urllib.parse.urlencode(params), budget=budget)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise MDBListKeyRejected() from None
        if error.code == 429:
            raise MDBListRateLimited() from None
        raise ValueError("MDBList request failed") from None
    except Exception:
        # The API key is in the URL; never propagate transport exception text.
        raise ValueError("MDBList request failed") from None


def _mdblist_pages(path, key, buckets, budget=None):
    rows, offset = {bucket: [] for bucket in buckets}, 0
    while True:
        page = _mdblist_http(path, key, offset=offset, budget=budget)
        if not isinstance(page, dict) or not isinstance(page.get("pagination"), dict):
            raise SchemaMismatch()
        for bucket in buckets:
            batch = page.get(bucket, [])
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise SchemaMismatch()
            rows[bucket].extend(batch)
        pagination = page["pagination"]
        if not pagination.get("has_more"):
            return rows
        step = pagination.get("limit", 500)
        if not any(page.get(bucket) for bucket in buckets) or type(step) is not int or step <= 0:
            raise SchemaMismatch()
        offset += step


def _mdblist_title(row, kind):
    title = row.get("movie") or row.get("show") or row
    ids = title.get("ids") or {}
    tid, imdb = ids.get("tmdb"), ids.get("imdb")
    if type(tid) is not int or tid <= 0:
        if not isinstance(imdb, str) or not re.fullmatch(r"tt[0-9]+", imdb):
            return None
        details = tmdb_details({"type": "movie" if kind == "movies" else "series", "id": imdb})
        if not details:
            return None
        tid = details[1]["id"]
    return {"tmdb": tid, "imdb": imdb, "title": title.get("title") or "Unknown title", "year": title.get("year"),
            "genres": [], **({"plays": []} if kind == "movies" else {"eps": [], "eps_total": None})}


def _mdblist_items(rows, now, watched):
    grouped, summaries = {"movies": {}, "shows": {}}, {}
    for bucket, batch in rows.items():
        for row in batch:
            episode = row.get("episode") if bucket == "episodes" else None
            kind = "movies" if bucket == "movies" else "shows"
            if watched and bucket in ("shows", "seasons") and not (row.get("show", {}).get("ids") and row.get("last_watched_at")):
                continue
            title = _mdblist_title({"show": episode.get("show", {})} if episode else row, kind)
            if title is None:
                continue
            if watched and bucket in ("shows", "seasons"):
                at = _connector_time(row["last_watched_at"])
                if at is None:
                    raise SchemaMismatch()
                summary = {**title, "plays": [math.floor(at / 60 + 0.5)]}
                if title["tmdb"] not in summaries or summary["plays"] > summaries[title["tmdb"]]["plays"]:
                    summaries[title["tmdb"]] = summary
                continue
            item = grouped[kind].setdefault(title["tmdb"], title)
            if watched:
                at = _connector_time(row.get("last_watched_at"))
                if at is None:
                    raise SchemaMismatch()
                minute = math.floor(at / 60 + 0.5)
                play = [episode["season"], episode["number"], minute, 0] if episode else [0, 0, minute, 0]
                value = minute if kind == "movies" else play
                field = "plays" if kind == "movies" else "eps"
                if value not in item[field]:
                    item[field].append(value)
    exported = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {"exported_at": exported, **{kind: list(items.values()) for kind, items in grouped.items()}}
    items = _trakt_import_history(doc, now)["items"]
    # Summary timestamps establish a watched show, not an invented episode.
    summary_doc = {"exported_at": exported, "movies": [row for tid, row in summaries.items()
                   if tid not in grouped["shows"]], "shows": []}
    items.extend({**it, "type": "series"} for it in _trakt_import_history(summary_doc, now)["items"])
    return items


def _connector_mdblist(c, now, budget=None, *, readonly=False):
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    if "accounts" in c:
        return _connector_accounts(c, now, budget, _connector_mdblist)
    key = _resolve_auth(c["auth"])
    activities = _mdblist_http("/sync/last_activities", key, budget=budget)
    if not isinstance(activities, dict):
        raise SchemaMismatch()
    path = os.path.join(DATA, "mdblist-" + c["person"] + ".history.json")
    try:
        with open(path) as fh:
            old = json.load(fh)
    except FileNotFoundError:
        old = {}
    doc = {"version": 1, "source": "mdblist", "activities": {}, "watched": [], "watchlist": []}
    for field, bucket, endpoint, buckets in (
            ("watched_at", "watched", "/sync/watched", ("movies", "episodes", "shows", "seasons")),
            ("watchlisted_at", "watchlist", "/watchlist/items", ("movies", "shows"))):
        stamp = activities.get(field)
        if not readonly and stamp is not None and stamp == old.get("activities", {}).get(field):
            doc[bucket] = old[bucket]
        else:
            rows = _mdblist_pages(endpoint, key, buckets, budget)
            doc[bucket] = _mdblist_items(rows, now, bucket == "watched")
        doc["activities"][field] = stamp
    items = {(it["type"], it["id"]): it for it in doc["watchlist"]}
    items.update({(it["type"], it["id"]): it for it in doc["watched"]})
    doc["items"] = list(items.values())
    if readonly:
        return doc, None, {"plays_30d": sum(it.get("plays", 0) for it in doc["watched"]
                                           if now - 30 * DAY <= (it.get("at") or 0) / 1000 <= now)}
    previous = {(it["type"], it["id"]): it for it in old.get("items", [])}
    changed = [it for it in doc["items"] if any(it.get(f) != previous.get((it["type"], it["id"]), {}).get(f)
                                               for f in ("signal", "plays", "at", "episodes_played"))]
    doc["exported_at"] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if doc["activities"] != old.get("activities") or doc["items"] != old.get("items"):
        _connector_write("mdblist-" + c["person"] + ".history", doc, indent=2, newline=True)
    events = list(trakt_events(now, [{**it, "person": c["person"]} for it in changed]))
    cursor = [max((_connector_time(v) or 0 for v in doc["activities"].values()), default=0), len(doc["items"])]
    return events, cursor, {"events": len(doc["items"]), "error": None}


class StremioKeyRejected(ValueError):
    code = 401

    def __init__(self):
        super().__init__("Stremio key rejected; reconnect with skald setup --connect stremio")


class StremioRateLimited(ValueError):
    code = 429
    headers = {}

    def __init__(self):
        super().__init__("Stremio rate limit; sync stopped, try later")


def _stremio_http(url, *, body=None, budget=None, text=False):
    # Stremio's likes and link services refuse requests without a named client.
    headers = {"User-Agent": "Skald/" + VERSION, **({"Content-Type": "application/json"} if body is not None else {})}
    try:
        value = _http_get(url, headers, body=body, budget=budget, **({'text': True} if text else {}))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise StremioKeyRejected() from None
        if error.code == 429:
            raise StremioRateLimited() from None
        raise ValueError("Stremio request failed") from None
    except Exception:
        # Likes URLs contain the key; never propagate transport exception text.
        raise ValueError("Stremio request failed") from None
    if text:
        return value
    if not isinstance(value, dict):
        raise SchemaMismatch()
    if value.get("error"):
        if any(word in json.dumps(value["error"]).lower() for word in ("session", "auth")):
            raise StremioKeyRejected()
        raise ValueError("Stremio request failed")
    return value


def _stremio_login(email, password):
    value = _stremio_http("https://api.strem.io/api/login", body={"email": email, "password": password})
    result = value.get("result")
    key = result.get("authKey") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise StremioKeyRejected()
    return key


STREMIO_RULES = 2  # bump when _stremio_item rules change so cached rows are mapped again
STREMIO_LINK = "https://link.stremio.com/api/v2/"


def _stremio_link_code():
    """Start a device link; the person confirms it in a browser signed in to Stremio."""
    result = _stremio_http(STREMIO_LINK + "create?type=Create").get("result")
    if not isinstance(result, dict) or not all(isinstance(result.get(k), str) and result[k] for k in ("code", "link")):
        raise SchemaMismatch()
    return result["code"], result["link"]


def _stremio_link_key(code, *, tries=100, wait=3):
    """Poll until the link is confirmed (about five minutes); unconfirmed reads report an error."""
    for _ in range(tries):
        time.sleep(wait)
        try:
            result = _stremio_http(STREMIO_LINK + "read?type=Read&code=" + urllib.parse.quote(code, safe="")).get("result")
        except ValueError:
            continue
        key = result.get("authKey") if isinstance(result, dict) else None
        if isinstance(key, str) and key.strip():
            return key
    raise ValueError("Stremio link not confirmed in time; run setup again")


def _stremio_item(row, now):
    raw, kind = row.get("_id"), row.get("type")
    if not isinstance(raw, str) or not re.fullmatch(r"tt[0-9]+", raw) or kind not in ("movie", "series"):
        return None
    state = row.get("state") or {}
    duration, progress = state.get("duration") or 0, state.get("timeWatched") or 0
    watched = (state.get("timesWatched") or 0) > 0 or (state.get("flaggedWatched") or 0) > 0 or duration > 0 and progress / duration >= 0.7
    # Stremio stores titles watched outside the library as removed temporary items.
    if (row.get("removed") or row.get("temp")) and not watched:
        return None
    title = _mdblist_title({"ids": {"imdb": raw}, "title": row.get("name")}, "movies" if kind == "movie" else "shows")
    if title is None:
        return None
    title["plays"] = []
    at = _connector_time(state.get("lastWatched")) if watched else None
    if at is not None:
        title["plays"] = [math.floor(at / 60 + 0.5)]
    exported = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    it = _trakt_import_history({"exported_at": exported, "movies": [title], "shows": []}, now)["items"][0]
    it.update(type=kind, played=watched, plays=max(1, state.get("timesWatched") or 0) if watched else 0,
              signal="watched" if watched else "interest", weight=it["weight"] if at is not None else 2 if watched else 1)
    if kind == "series" and watched:
        episode = re.fullmatch(r"tt[0-9]+:([0-9]+):([0-9]+)", state.get("video_id") or "")
        if episode:
            it.update(episodes_played=1, last_episode=[int(n) for n in episode.groups()])
    return it


def _connector_stremio(c, now, budget=None, *, readonly=False):
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    if "accounts" in c:
        return _connector_accounts(c, now, budget, _connector_stremio)
    key = _resolve_auth(c["auth"])
    library = _stremio_http("https://api.strem.io/api/datastoreGet",
        body={"authKey": key, "collection": "libraryItem", "ids": [], "all": True}, budget=budget).get("result")
    if not isinstance(library, list) or any(not isinstance(row, dict) for row in library):
        raise SchemaMismatch()
    path = os.path.join(DATA, "stremio-" + c["person"] + ".history.json")
    try:
        with open(path) as fh:
            old = json.load(fh)
    except FileNotFoundError:
        old = {}
    records, items = {}, {}
    for row in library:
        raw = row.get("_id")
        if not isinstance(raw, str):
            continue
        previous = old.get("library", {}).get(raw, {}) if old.get("version") == STREMIO_RULES else {}
        it = previous.get("item") if row == previous.get("state") else _stremio_item(row, now)
        records[raw] = {"state": row, "item": it}
        if it:
            items[(it["type"], it["imdb"])] = dict(it)
    likes, cached = {}, old.get("likes", {})
    library_items, likes_available = items.copy(), True
    from skald.ingest import rating_signal
    try:
        addon_key = _stremio_http('https://likes.stremio.com/getAddonKey?key='+urllib.parse.quote(key, safe=''), budget=budget, text=True)
        if isinstance(addon_key, str):
            try:
                addon_key = json.loads(addon_key)
            except json.JSONDecodeError:
                addon_key = addon_key.strip()
        if isinstance(addon_key, dict):
            addon_key = addon_key.get('key')
        if not isinstance(addon_key, str) or not addon_key.strip():
            raise SchemaMismatch()
        for level, rating in (("liked", 8), ("loved", 10)):
            for kind in ("movie", "series"):
                url = "https://likes.stremio.com/addons/" + level + "/movies-shows/user=" + urllib.parse.quote(addon_key, safe="") + "/catalog/" + kind + "/stremio-" + level + "-" + kind + ".json"
                metas = _stremio_http(url, budget=budget).get("metas")
                if not isinstance(metas, list) or any(not isinstance(row, dict) for row in metas):
                    raise SchemaMismatch()
                for meta in metas:
                    raw = meta.get("id")
                    if not isinstance(raw, str) or not re.fullmatch(r"tt[0-9]+", raw) or meta.get("type", kind) != kind:
                        continue
                    cache_key = kind + ":" + raw
                    it = items.get((kind, raw)) or cached.get(cache_key) or _stremio_item({"_id": raw, "type": kind, "name": meta.get("name")}, now)
                    if it is None:
                        continue
                    it = dict(it)
                    signal, weight = IMPORT_SIGNALS[rating_signal(rating)]
                    it.update(signal=signal, weight=weight, rating=rating)
                    items[(kind, raw)] = likes[cache_key] = it
    except StremioRateLimited:
        raise
    except Exception:
        likes_available, items, likes = False, library_items, {}
        for cache_key, it in cached.items():
            ident = it['type'], it['imdb']
            merged = {**it, **items.get(ident, {}), **{field: it[field] for field in ('signal', 'weight', 'rating') if field in it}}
            items[ident] = likes[cache_key] = merged
    doc = {"version": STREMIO_RULES, "source": "stremio", "library": records, "likes": likes, "items": list(items.values())}
    if readonly:
        return doc, None, {"plays_30d": sum(it.get("plays", 0) for it in doc["items"]
            if it.get("played") and now - 30 * DAY <= (it.get("at") or 0) / 1000 <= now), "likes_available": likes_available}
    previous = {(it["type"], it["id"]): it for it in old.get("items", [])}
    changed = [it for it in doc["items"] if any(it.get(f) != previous.get((it["type"], it["id"]), {}).get(f)
        for f in ("signal", "plays", "at", "episodes_played", "last_episode", "rating"))]
    doc["changed_at"] = now if doc["items"] != old.get("items") else old.get("changed_at", 0)
    if any(doc[field] != old.get(field) for field in ("library", "likes", "items")):
        doc["exported_at"] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        _connector_write("stremio-" + c["person"] + ".history", doc, indent=2, newline=True)
    events = list(trakt_events(now, [{**it, "person": c["person"]} for it in changed]))
    cursor = [max(doc["changed_at"], max((_connector_time(row.get("_mtime")) or 0 for row in library), default=0)), len(doc["items"])]
    return events, cursor, {"events": len(doc["items"]), "error": None, "likes_available": likes_available}


def _connector_accounts(c, now, budget, adapter):
    accounts, events = copy.deepcopy(c["accounts"]), []
    for account in accounts.values():
        if account['person'] not in PEOPLE:
            continue
        if "backoff_until" in budget:
            account["last_error"] = "PMDBRateLimited" if adapter == _connector_pmdb else "RateLimited"
            continue
        try:
            new_events, cursor, stats = adapter(account, now, budget)
        except Exception as error:
            account["last_error"] = type(error).__name__
            if adapter in (_connector_mdblist, _connector_stremio, _connector_pmdb):
                account["health"] = {"state": "error", "error": type(error).__name__, "at": now}
            if isinstance(error, MDBListKeyRejected):
                account["health"]["fixHint"] = "MDBList key rejected; run skald setup --connect mdblist --person " + account["person"]
            elif isinstance(error, MDBListRateLimited):
                account["health"]["fixHint"] = "MDBList rate limit; sync stopped, try later"
            if isinstance(error, (StremioKeyRejected, StremioRateLimited)):
                account["health"]["fixHint"] = str(error)
            if adapter == _connector_pmdb:
                account["health"]["fixHint"] = PMDB_ERRORS.get(type(error).__name__, "PublicMetaDB probe failed")
            if isinstance(error, TraktLoginRequired):
                account["health"]["fixHint"] = "re-run skald trakt login --member " + account["person"]
            if getattr(error, "code", None) == 429:
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


def _connector_trakt_api(c, now, budget=None, *, readonly=False):
    cfg = CONFIG["connectors"]["trakt_api"]
    budget = budget if budget is not None else {"bytes": 0, "pages": 0}
    if "accounts" in c:
        return _connector_accounts(c, now, budget, _connector_trakt_api)
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


def _connector_discard_disabled():
    global _connector_pending
    if _connector_pending is not None:
        data, cid, future = _connector_pending
        if (data != DATA or _connectors.get(cid, {}).get('health', {}).get('state') == 'disabled' or
                cid not in _connectors) and (future.cancel() or future.done()):
            _connector_pending = None


def plays_queue_check():
    global _connectors_generation
    now = time.time()
    if _connectors_path != DATA:
        connectors_load(now)
    from skald import plays
    context = SimpleNamespace(**globals())
    backoff = plays.state(context).get('backoff_until', 0)
    with _connectors_lock:
        _connector_discard_disabled()
        c = _connectors[plays.connector_id(context)]
        if c['health']['state'] == 'disabled':
            return
        c['next_due'] = max(now, backoff)
        _connectors_generation += 1
        if _connector_pending is None and c['next_due'] <= now:
            _connector_async(c, now)
        return c['next_due']


CONNECTOR_POLL = {"history": _connector_history, "trakt": _connector_trakt,
                  "player": _connector_player, "aiostreams_db": _connector_player, "jellyfin": _connector_player, "plex": _connector_player,
                  "trakt_api": _connector_trakt_api, "mdblist": _connector_mdblist, "stremio": _connector_stremio, "pmdb": _connector_pmdb}


def poll(c, now):
    with history_locked(DATA):
        generation = _erasure_generation
    _upstream.erasure_generation = generation
    try:
        result = CONNECTOR_POLL[c["id"]](c, now)
        with history_locked(DATA):
            if generation != _erasure_generation:
                return [], c.get('cursor'), {'events': 0, 'error': None}
            return result
    finally:
        del _upstream.erasure_generation


def connectors_demand(now):
    from skald import plays
    context = SimpleNamespace(**globals())
    global _connectors_generation
    with _connectors_lock:
        changed = False
        for c in _connectors.values():
            if plays.managed(context) and c['id'] == plays.connector_id(context):
                continue
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
    from skald import plays
    global _connectors_generation, _connector_import_stamp, _connectors_busy
    with history_locked(DATA):
        erasure = _erasure_generation
    context = SimpleNamespace(**globals())
    waiting = _connectors.get(plays.connector_id(context), {}).get('health', {}).get('state') == 'disabled'
    if _connectors_path != DATA or waiting and plays.managed(context) and plays.settings(CONFIG)['mode'] != 'off':
        connectors_load(now)
    with _connectors_lock:
        if _connectors_busy or _connectors_path != DATA:
            return
        _connector_discard_disabled()
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
                health_poll_result(cid, error or any(a.get('last_error') or a['health']['error'] for a in c.get('accounts', {}).values()), stats.get('backoff_until', 0))
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
                    health_poll_result(cid, error)
                stale_at = at if cid == "history" else c["last_new"]
                state = "error" if c["health"]["error"] else "stale" if stale_at is not None and now - stale_at >= CONNECTOR_STALE[cid] else "ok"
            else:
                continue
            if state != c["health"]["state"]:
                c["health"].update(state=state, at=now)
            changed |= before != _connector_material(c)
        with history_locked(DATA), _connectors_lock:
            if erasure != _erasure_generation:
                return
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
    person = it.get("person", TRAKT_PERSON) if _taste_accounts() else TRAKT_PERSON
    return {"key": it["id"], "id": tid, "kind": kind, "stype": "movie" if kind == "movie" else "series",
            "title": it.get("title") or "", "year": str(it.get("year") or ""), "signal": it.get("signal") or "watched",
            "weight": float(it.get("weight") or 0), "age": age, "item": it, "who": "owner", "by": [person],
            "flags": it.get("flags") or [], "genres": genre_names(it.get("genres") or [])}


# --- events ---
EVENT_ACTION = {"loved": "love", "liked": "like", "watched": "play", "interest": "interest",
                "opened": "open", "abandoned": "abandon", "disliked": "not_for_us"}
EVENT_FAMILY = {"tv": "tv", "trakt": "trakt", "told": "told", "remote": "remote", "interview": "remote", "sync:pmdb": "trakt"}
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
        weight = float(ev["w0"] or 0)
        if src == "sync:pmdb":
            weight *= trust(src)
        return ev["ctx"]["signal"], weight, age
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
        if family == "remote" and signal == "disliked" and (ev.get("ctx") or {}).get("row") == rid('newtoyou'):
            weight *= NEWTOYOU_REJECT  # a deliberate stretch that missed says little about the person
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
    selected, used = [], {}
    for it in items:
        who = attribute(it)[0]
        stream = who if who not in member_people() and who not in ('both', 'together') else 'household'
        if used.get(stream, 0) >= 40:
            continue
        selected.append(it)
        used[stream] = used.get(stream, 0) + it.get("_summary", {}).get("raw_items", 1)
    jobs = [(it, _pool.submit(tmdb_details, it)) for it in selected if "_summary" not in it]
    def resolved_jobs():
        for it, job in jobs:
            try:
                hit = job.result()
            except Exception:
                continue
            if hit:
                yield it, *hit
    has_summaries = any("_summary" in it for it in selected)
    resolved = list(resolved_jobs()) if has_summaries else resolved_jobs()
    raw_keys = {(f"tmdb:{det['id']}", kind, it.get("profile", "household"), who, tuple(sorted(by)))
                for it, kind, det in resolved for who, by, _ in [attribute(it)]} if has_summaries else set()
    for it in selected:
        if "_summary" in it:
            e = it["_summary"]
            if (e["key"], e["kind"], e["profile"], e["who"], tuple(sorted(e["by"]))) in raw_keys:
                continue
            signal, _ = classify(it, now)
            yield _event(it["at"] / 1000 if it.get("at") else None, f"tv:{e['kind']}:{e['key']}",
                         e["key"], e["kind"], e["title"], e["year"], e["who"], e["by"],
                         EVENT_ACTION[signal], "tv", {"item": it, "genres": e["genres"], "flags": e["flags"], "signal": signal}, _ids=ids)
    for it, kind, det in resolved:
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
        person = it.get("person", TRAKT_PERSON) if _taste_accounts() else TRAKT_PERSON
        kind = "movie" if it["type"] == "movie" else "tv"
        signal = it.get("signal") or "watched"
        yield _event(it["at"] / 1000 if it.get("at") else None, f"trakt:{kind}:{it['id']}", it["id"], kind,
                     it.get("title") or "", str(it.get("year") or ""), person, [person],
                     EVENT_ACTION.get(signal, signal), "sync:pmdb" if it.get("src") == "sync:pmdb" else "trakt", {"item": it, "genres": genre_names(it.get("genres") or []),
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
        if e["who"] not in (*PEOPLE, "both"):
            continue
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
        if person not in PEOPLE:
            continue
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
    return (by - {"both", "together"}) | set(member_people()) if by & {"both", "together"} else by


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
        if src in ("tv", "trakt", "sync:pmdb"):
            day = math.floor(item["at"] / 1000 / DAY) if item.get("at") else -math.inf
    explicit = src not in ("trakt", "sync:pmdb") and (action in EXPLICIT if record or "action" in e else
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
        stream = ev['who'] if ev['who'] not in member_people() else 'household'
        grouped.setdefault((ev['key'], stream), []).append(ev)
    entries = []
    for (key, _), reactions in grouped.items():
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
    from skald.history import seen_watches
    forgotten = seen_watches(DATA)
    if forgotten:
        stages = [(stage, [e for e in evts if not (
            (len(e['by']) > 1 or set(e['by']) & {'both', 'together'}) and
            (e.get('t') or 0) < forgotten.get(e['key'], forgotten.get(e['ctx'].get('item', {}).get('id'), 0)))])
            for stage, evts in stages]
    guests = [p for p in PEOPLE if p not in member_people()]
    if not guests and not CONFIG['household'].get('retired'):
        return _fold_events(stages, now)
    stages = [(stage, list(evts)) for stage, evts in stages]
    entries, by_key, extra_seen = _fold_events([(stage, [e for e in evts if e['who'] not in guests and e['who'] not in CONFIG['household'].get('retired', [])]) for stage, evts in stages], now)
    for person in guests:
        separate, _, _ = _fold_events([(stage, [e for e in evts if e['who'] == person]) for stage, evts in stages], now)
        entries.extend(separate)
    return entries, by_key, extra_seen


def _fold_events(stages, now):
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
                by_title = {(e["key"], e["kind"]): e for e in entries}
                for ev in evts:
                    t = trakt_entry(ev["ctx"]["item"], now)
                    t["signal"], t["weight"], t["age"] = event_weight(ev, now)
                    t["wsrc"] = ev["src"]
                    e = by_title.get((t["key"], t["kind"]))
                    if not e:
                        by_key[t["key"]] = t
                        by_title[(t["key"], t["kind"])] = t
                        entries.append(t)
                        continue
                    person = ev["who"]
                    if ev["src"] == "sync:pmdb" and person in _expand_together(e["by"]):
                        old = (e.get("opinions") or {}).get(person) or e.get("shared") or e
                        if _taste_rank(old) >= _taste_rank(t):
                            continue
                    watch_fields = ("age", "genres", "title", "year") if _taste_accounts() else ()
                    if _taste_accounts() and e["who"] not in ("tv", "both"):
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
                            e["wsrc"] = ev["src"]
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
    if player_history_active():
        extra_seen |= {it["id"] for it in items if (it.get("played") or it.get("episodes_played")) and attribute(it)[0] not in (GUEST, *guest_people(CONFIG))}
    entries.sort(key=lambda e: (-(e["weight"] / (1 + min(e["age"], 365) / 60)), e["age"]))
    genre_w = {}
    for e in entries:
        for g in e["genres"]:
            genre_w[g] = genre_w.get(g, 0) + e["weight"]
    shared = [e for e in entries if set(e.get('by', ())) & set(member_people()) or 'together' in e.get('by', ())]
    return {"entries": entries, "genres": genre_w, "searches": searches, "likes": "",
            "seen": {e["key"] for e in shared} | extra_seen | history_seen(DATA), "negative": {e["key"] for e in shared if e["weight"] < 0}}


SEED_WEIGHT = 2.0  # a title someone told us they love: timeless, so no recency
SEED_AGE = 400     # old enough that Watch Again treats it as a favourite, not a saga in progress


def person_loves():
    """[(person, title, year, kind)] from taste.json people.<name>.loves ("Title|Year|m or s")."""
    out = []
    for person, cfg in ((read_taste().get("people") or {}).items()):
        if person not in PEOPLE:
            continue
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
        if person not in guest_people(CONFIG):
            extra |= ev["ctx"]["saga"]
    return extra


def profile_text(p, you="they"):
    def names(signals, who=None, n=10, entries=None):
        hits = [e for e in (p["entries"] if entries is None else entries) if e["signal"] in signals and (who is None or e.get("who") in who)][:n]
        return "; ".join(f"{e['title']} ({e['year']}, {'movie' if e['kind'] == 'movie' else 'series'}"
                         f"{', ' + '/'.join(f for f in e.get('flags') or [] if f in ('binge', 'comfort', 'anime')) if set(e.get('flags') or []) & {'binge', 'comfort', 'anime'} else ''})"
                         for e in hits)
    lines = [f"What {you} told us they love: {p['likes']}"] if p.get("likes") else []
    owner = [e for e in p["entries"] if e.get("who") in ("owner", "both")]
    logs = [(TRAKT_PERSON, owner)]
    if _taste_accounts():
        logs = [(person, [e for e in person_entries(p, person) if e.get("wsrc") == "trakt"])
                for person in _taste_accounts()]
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
        lines.append("Recently on the shared TV account (may include anyone in the household):")
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
    """Changes with taste signals and player activity dates, not progress ticks."""
    now = time.time()
    items = [it for it in items if name != 'household' or attribute(it)[0] in (*member_people(), 'both', 'together')]
    signal = sorted((it['id'], classify(it, now)[0]) for it in items)
    if player_history_active():
        signal.append(sorted((it.get("profile", "household"), it["type"], it["id"], it.get("at") or 0)
                             for it in items))
    from skald.history import read as history_summaries
    summaries = [e for e in history_summaries(DATA) if name != 'household' or
                 set(e.get('by', [e.get('who')])) & {*member_people(), 'both', 'together'}]
    if summaries:
        signal.append(summaries)
    if name == "household":
        if 'guests' in CONFIG['household'] or CONFIG['household'].get('retired_rows'):
            signal.append(member_people())
        signal.append(recent_searches())
        if _taste_accounts():
            signal.append(sorted((it["person"], it["id"], it.get("signal"), it.get("weight")) for it in read_trakt() if it['person'] in member_people()))
        else:
            signal.append(sorted((it["id"], it.get("signal")) for it in read_trakt() if TRAKT_PERSON in member_people()))
        if any(p in member_people() for p in CONFIG['connectors'].get('stremio', {}).get('accounts', {})):
            signal.append(sorted((person, it["id"], it.get("at"), it.get("episodes_played"), it.get("last_episode"))
                for person in CONFIG["connectors"]["stremio"]["accounts"] if person in member_people() for it in _drop_read("stremio-" + person)))
        if any(p in member_people() for p in CONFIG['connectors'].get('pmdb', {}).get('accounts', {})):
            signal.append(sorted((person, it["type"], it["id"], it.get("at"), it.get("episodes_played"),
                                  it.get("last_episode"), it.get("resume_episode"), it.get("progress"))
                for person in CONFIG["connectors"]["pmdb"]["accounts"] if person in member_people() for it in _drop_read("pmdb-" + person)))
    signal.append(read_taste().get(name))
    if name == "household":
        signal.append({p: v for p, v in (read_taste().get('people') or {}).items() if p in member_people()})
        if feedback := feedback_hash(now):  # taste remote: only folded taste weights invalidate stamps.
            signal.append(feedback)
        if imported := import_hash(now):
            signal.append(imported)
        if attributed := attrib_hash(items):
            signal.append(attributed)
    return hashlib.sha1(json.dumps(signal).encode()).hexdigest()[:8]


def episodes_hash(items):
    signal = sorted((it["id"], it.get("last_episode")) for it in items if it["type"] == "series" and attribute(it)[0] in (*member_people(), 'both', 'together'))
    if any(p in member_people() for p in CONFIG['connectors'].get('stremio', {}).get('accounts', {})):
        signal.append(sorted((person, it["id"], it.get("at"), it.get("episodes_played"), it.get("last_episode"))
            for person in CONFIG["connectors"]["stremio"]["accounts"] if person in member_people() for it in _drop_read("stremio-" + person) if it["type"] == "series"))
    if any(p in member_people() for p in CONFIG['connectors'].get('pmdb', {}).get('accounts', {})):
        signal.append(sorted((person, it["id"], it.get("at"), it.get("episodes_played"),
                              it.get("last_episode"), it.get("resume_episode"))
            for person in CONFIG["connectors"]["pmdb"]["accounts"] if person in member_people() for it in _drop_read("pmdb-" + person) if it["type"] == "series"))
    return hashlib.sha1(json.dumps(signal).encode()).hexdigest()[:8]


def refresh_profiles():
    """Rebuild the taste profiles whose inputs changed; returns the current hashes."""
    with _home_lock:
        generation = _erasure_generation
    hist = read_history()
    hashes = {"episodes": episodes_hash([it for it in hist if it.get("profile", "household") == "household"])}
    active = active_profiles()
    for name in ("household", *ARCHIVED):
        if name in active:
            continue
        with _feeds_lock:
            stale = [k for k in _feeds if k[0] == rid(name) and k[0] not in CONFIG['household'].get('retired_rows', [])]
            for key in stale:
                _feeds.pop(key)
        if _profiles.pop(name, None) or stale:
            print(f"profile {name}: archived", flush=True)
    for name in active:
        items = [it for it in hist if it.get("profile", "household") == name or
                 name == "household" and player_history_active() and it.get("profile") in PEOPLE]
        h = taste_hash(name, items)
        hashes[name] = h
        guests = set(guest_people(CONFIG)) & set(PEOPLE) if name == 'household' else set()
        guest_stamp = None
        if guests:
            signal = [it for it in items if attribute(it)[0] in guests]
            signal.append({p: v for p, v in (read_taste().get('people') or {}).items() if p in guests})
            signal.extend(it for it in read_trakt() if it.get('person', TRAKT_PERSON) in guests)
            from skald.history import read as history_summaries
            signal.extend(e for e in history_summaries(DATA) if set(e.get('by', [e.get('who')])) & guests)
            signal += sorted(e['id'] for (_, who), e in active_feedback().items() if who in guests)
            signal += sorted(e['id'] for e in active_imports() if e['who'] in guests)
            guest_stamp = hashlib.sha256(json.dumps(signal, sort_keys=True).encode()).hexdigest()
        if (_profiles.get(name) or {}).get("hash") == h and (_profiles.get(name) or {}).get('guest_stamp') == guest_stamp:
            continue
        prof = build_profile(items, recent_searches() if name == "household" else [],
                             read_trakt() if name == "household" else (), remote=name == "household")
        prof.update(hash=h, likes=(read_taste().get(name) or {}).get("likes", ""))
        if guests:
            prof['guest_stamp'] = guest_stamp
        with _home_lock:
            if generation != _erasure_generation:
                return {}
            _profiles[name] = prof
        print(f"profile {name}: {len(prof['entries'])} titles, hash {h}", flush=True)
    if "household" in _profiles:
        p = adult_profile(_profiles["household"])
        learn_from_plays(p)
        log_play_context(p)
    return hashes


# --- learning from plays (A) ------------------------------------------------------
# Every row title Odin actually fetched counts as shown (first LEARN_VISIBLE positions, once per day).
# A later play on the shared TV credits every row that showed it in the LEARN_WINDOW days before.
# Titles shown on LEARN_TIRED days without a play sink to the end of their row and leave the AI rows.

NEWTOYOU_REJECT = 0.2  # New to You picks change one axis on purpose; a no there counts a fifth of a normal no
LEARN_ROWS = {rid('foryou'), rid('upcoming'), rid('catchup'), rid('rewatch'), rid('because'), rid('top10'), rid('week'), rid('weekpicks'),
              rid('leap')} | set(BECAUSE_ROWS) | set(ROW_OWNER) | set(DOCS) | set(STUDIO_ROWS)
LEARN_VISIBLE = 20
LEARN_WINDOW = 21
LEARN_TIRED = 6
_learn = {"served": {}, "rows": {}, "hits": [], "credited": {}, "lanes": {}, "ctx": [], "moodpicks": {}, "seasons": {}}
CTX_MAX = 500
_learn_lock = threading.RLock()
_learn_dirty = threading.Event()
_learn_forgotten = history_cutoffs(DATA)


def _refresh_learning():
    global _learn_forgotten
    forgotten = history_cutoffs(DATA)
    with _learn_lock:
        if forgotten != _learn_forgotten:
            _learn.clear()
            _learn.update(served={}, rows={}, hits=[], credited={}, lanes={}, ctx=[], moodpicks={}, seasons={})
            _learn_dirty.clear()
            _learn_forgotten = forgotten


def row_name(cid, lang, fallback=None, metas=None, key="title"):
    label = None
    if key == 'title':
        groups = row_groups()
        label = next((name for row, name in CONFIG['household'].get('row_labels', {}).items()
                      if cid in groups.get(row, [rid(row)])), None)
    if cid in SHELF_ROWS and key == "title":
        return label or next((r[2] for r in shelf_rows() if r[0] == cid), fallback)
    if cid in READY_ROWS and key == 'title':
        from skald import ready
        scope = getattr(_scoping, 'scope', None)
        who = SCOPES[scope] if scope is not None else _viewer.get() or who_watching()[0]
        return label or ready.label(SimpleNamespace(**globals()), ready.slot(SimpleNamespace(**globals()), cid, lang, who))
    if key == "title" and kids_age(safety_subject()) is not None:
        return t("row.picked")
    if label:
        return label
    names = row_names.mapping(getattr(metas, "names", feed(cid, lang).get("names")))
    if key == "title" and any(k in names for k in ("label_key", "mood", "lane")):
        return row_names.describe(cid, names, CONFIG, row_lang(), _learn.get("lanes", {}))["label"]
    name = names.get(key) or (names.get("mixed") if key == "title" else None) or fallback
    name = name.get("tv") or name.get("movie") if isinstance(name, dict) else name
    return row_names.text(name) or fallback or (row_names.describe(cid, names, CONFIG, row_lang())["label"] if key == "title" else None)


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
    _refresh_learning()
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
    _refresh_learning()
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
    _refresh_learning()
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
    _refresh_learning()
    played = set(_learn["credited"])
    with _learn_lock:
        return {k: rec["name"] for k, rec in _learn["served"].items() if k not in played and
                max((len(r["days"]) for c, r in rec["rows"].items() if cids is None or c in cids), default=0) >= LEARN_TIRED}


def learning_records(field):
    _refresh_learning()
    records = _learn[field]
    adults = set(adult_people())
    if len(adults) == len(PEOPLE) and not CONFIG['household'].get('retired'):
        return records
    p = _profiles.get("household") or {"entries": []}
    keys = {value["item"]["id"]: e["key"] for e in p["entries"]
            for value in (e, e.get("shared") or {}, *(e.get("opinions") or {}).values())
            if (value.get("item") or {}).get("id")}
    allowed = set()
    for it in read_history():
        if it.get("at"):
            by = attribute(it)[1]
            if adults & set(by) or adults and "together" in by:
                allowed.add((keys.get(it["id"], it["id"]), int(it["at"] / 1000)))
    # shortcut: omit plays without retained attribution; use per-play history if older credit is needed.
    return [r for r in records if (r["key"], r["at"]) in allowed]


def learning_rows():
    _refresh_learning()
    if len(adult_people()) == len(PEOPLE) and not CONFIG['household'].get('retired'):
        return _learn["rows"]
    rows = {k: {**v, "hits": 0} for k, v in _learn["rows"].items()}
    for h in learning_records("hits"):
        if h["row"] in rows:
            rows[h["row"]]["hits"] += h["share"]
    return rows


def hit_titles(n=12):
    with _learn_lock:
        seen, out = set(), []
        for h in reversed(learning_records("hits")):
            if h["key"] not in seen:
                seen.add(h["key"])
                out.append(f"{h['title']} (from {h['row'].split('.')[-1]})")
        return out[:n]


def save_learn():
    _refresh_learning()
    save_online()
    if not _learn_dirty.is_set():
        return True
    _learn_dirty.clear()
    cutoff = (local_now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    with _learn_lock:
        for k in [k for k, rec in _learn["served"].items()
                  if max((r["days"][-1] for r in rec["rows"].values() if r["days"]), default="") < cutoff]:
            _learn["served"].pop(k)
        snap = json.dumps(_learn, ensure_ascii=False)
    try:
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            with open(os.path.join(DATA, "learn.json.tmp"), "w") as fh:
                fh.write(snap)
            os.replace(os.path.join(DATA, "learn.json.tmp"), os.path.join(DATA, "learn.json"))
    except OSError:
        _learn_dirty.set()
        return False
    return True


def learn_stats():
    _refresh_learning()
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
_online_forgotten = history_cutoffs(DATA)


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
    global _online, _online_index, _online_pruned, _online_forgotten
    with _online_lock:
        forgotten = history_cutoffs(DATA)
        if forgotten != _online_forgotten:
            _online = None
            _online_dirty.clear()
            _online_forgotten = forgotten
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
                    with household_writable(DATA, household_erased) as allowed:
                        if allowed:
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
    if not _online_dirty.is_set():
        return
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
            with household_writable(DATA, household_erased) as allowed:
                if not allowed:
                    return False
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

EMB_MODEL = os.environ.get("SKALD_EMBED_MODEL") or "google/gemini-embedding-2"
EMB_DIM = 256
_emb_model = EMB_MODEL
_emb = {}
_emb_kinds = {}
_emb_next = {}
_emb_next_kinds = {}
_emb_job = None
_emb_retry = 0
_emb_lock = threading.RLock()
_emb_dirty = threading.Event()
_emb_error_logged = False


class Embedding(array):
    """Keep the source model on vectors held across an atomic cache switch."""
    def __new__(cls, values, model):
        obj = super().__new__(cls, 'f', values)
        obj.model = model
        return obj


class TasteVector(list):
    """A centroid retains its model without rounding its accumulation."""


def emb_status(pairs=()):
    with _emb_lock:
        needed = _needed_emb(pairs)
        return {"model": _emb_model, "active": sum(femb(k, kind) is not None for k, kind in needed),
                "pending": sum(_emb_get(k, kind, _emb_next, _emb_next_kinds) is not None for k, kind in needed),
                "needed": len(needed)}


def _needed_emb(pairs):
    needed = set(pairs)
    for p in list(_profiles.values()):
        needed.update((e["key"], e["kind"]) for e in p.get("entries", []))
    for store in (_feeds, _shown, *list(_shown_scoped.values())):
        for rec in list(store.values()):
            seed = (rec.get("names") or {}).get("seed")
            if seed:
                kind = (_facets.get(seed) or {}).get("kind") or _emb_kinds.get(seed) or profile_kind(seed)
                if kind in ("movie", "tv"):
                    needed.add((seed, kind))
            for metas in (rec.get("metas") or {}).values():
                if isinstance(metas, list):
                    needed.update((m["id"], "movie" if m["type"] == "movie" else "tv")
                                  for m in metas if isinstance(m, dict) and m.get("id", "").startswith("tmdb:")
                                  and m.get("type") in ("movie", "series", "tv"))
    return needed


def _refresh_emb(requested):
    global _emb_model, _emb_retry
    try:
        with _emb_lock:
            pairs = sorted(_needed_emb(requested))
        failed = _embed_pairs(pairs, EMB_MODEL, _emb_next, _emb_next_kinds)
        with _emb_lock:
            coverage = emb_status(requested)
            pending, active = coverage["pending"], coverage["active"]
            lost = sum(femb(k, kind) is not None for k, kind in failed)
            # Swap once every title that still has text is embedded, if that keeps at least half the old coverage.
            if pending >= active or (pending + lost >= active and 2 * pending >= active):
                _emb.clear()
                _emb.update(_emb_next)
                _emb_kinds.clear()
                _emb_kinds.update(_emb_next_kinds)
                _emb_model = EMB_MODEL
                _emb_next.clear()
                _emb_next_kinds.clear()
                _emb_dirty.set()
        clear_seed_cache()
    except Exception as e:
        # Match the feed worker's retry windows without exposing upstream error text.
        with _emb_lock:
            _emb_retry = time.time() + (3600 if isinstance(e, BudgetExceeded) else 600)


def title_text(kind, tid):
    rec = frec(f"tmdb:{tid}", kind)
    if rec.get("title"):  # Stored facets need no TMDB call; their lists keep TMDB's stored order.
        return (f"{rec['title']} ({rec.get('y') or ''}), {'movie' if kind == 'movie' else 'series'}. "
                f"Genres: {', '.join(rec.get('g') or [])}. Keywords: {', '.join(k[1] for k in (rec.get('k') or [])[:15])}. "
                f"{rec.get('o') or ''}")
    det = tmdb(f"{kind}/{tid}", language="en-US", append_to_response="keywords,credits,release_dates" if kind == "movie" else "keywords,credits,content_ratings")  # same URL as facet_record
    kw = det.get("keywords") or {}
    words = [k["name"] for k in (kw.get("keywords") or kw.get("results") or [])][:15]
    year = (det.get("release_date") or det.get("first_air_date") or "")[:4]
    return (f"{det.get('title') or det.get('name')} ({year}), {'movie' if kind == 'movie' else 'series'}. "
            f"Genres: {', '.join(g['name'] for g in det.get('genres') or [])}. Keywords: {', '.join(words)}. "
            f"{(det.get('overview') or '')[:600]}")


def ensure_emb(pairs):
    """pairs: [(key 'tmdb:1', kind 'movie'|'tv')]; embeds the missing ones. Returns how many are available."""
    global _emb_job
    if not OR_KEY or _defer_enrichment:
        return 0
    pairs = list(pairs)
    with _emb_lock:
        if _emb_model != EMB_MODEL:
            if time.time() >= _emb_retry and (_emb_job is None or _emb_job.done()):
                _emb_job = _outer.submit(_refresh_emb, pairs)
            return sum(femb(k, kind) is not None for k, kind in pairs)
    _embed_pairs(pairs, EMB_MODEL, _emb, _emb_kinds)
    with _emb_lock:
        return sum(femb(k, kind) is not None for k, kind in pairs)


def _embed_pairs(pairs, model, vectors, kinds):
    with _emb_lock:
        missing = list(dict.fromkeys((k, kind) for k, kind in pairs
                                    if _emb_get(k, kind, vectors, kinds) is None))
    texts = [(k, kind, _pool.submit(title_text, kind, k[5:])) for k, kind in missing]
    ready, failed = [], []
    for k, kind, job in texts:
        try:
            ready.append((k, kind, job.result()))
        except Exception:
            failed.append((k, kind))
    for i in range(0, len(ready), 96):
        batch = ready[i:i + 96]
        j = openrouter("/api/v1/embeddings", {"model": model, "input": [t for _, _, t in batch], "dimensions": EMB_DIM},
                       60, "embed", "titles")
        with _emb_lock:
            for d in j["data"]:
                if not isinstance(d["index"], int) or not 0 <= d["index"] < len(batch):
                    raise ValueError("invalid embedding index")
                k, kind, _ = batch[d["index"]]
                v = d["embedding"]
                if len(v) != EMB_DIM or not all(math.isfinite(x) for x in v):
                    raise ValueError("invalid embedding dimensions or values")
                n = sum(x * x for x in v) ** 0.5 or 1.0
                slot = fkey(k, kind)
                if kinds.get(slot) not in (None, kind):
                    slot = f"{k}@{kind}"
                vectors[slot] = Embedding((x / n for x in v), model)
                kinds[slot] = kind
        if vectors is _emb:
            _emb_dirty.set()
            clear_seed_cache()
    return failed


def save_emb():
    if not _emb_dirty.is_set():
        return
    _emb_dirty.clear()
    with _emb_lock:
        snap = {k: base64.b64encode(struct.pack(f"{EMB_DIM}e", *v)).decode() for k, v in _emb.items()}
        kinds = {k: kind for k, kind in _emb_kinds.items() if k in snap}
        model = _emb_model
    try:
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            with open(os.path.join(DATA, "emb.json.tmp"), "w") as fh:
                json.dump({"model": model, "dim": EMB_DIM, "v": snap, "kinds": kinds}, fh)
            os.replace(os.path.join(DATA, "emb.json.tmp"), os.path.join(DATA, "emb.json"))
    except OSError:
        pass


def load_emb():
    global _emb_model
    try:
        with open(os.path.join(DATA, "emb.json")) as fh:
            raw = json.load(fh)
        if raw.get("dim") == EMB_DIM:
            model = raw.get("model") or "openai/text-embedding-3-small"
            vectors = {k: Embedding(struct.unpack(f"{EMB_DIM}e", base64.b64decode(b)), model)
                       for k, b in raw["v"].items()}
            with _emb_lock:
                _emb_model = model
                _emb.clear()
                _emb.update(vectors)
                _emb_kinds.clear()
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


def embedding_failure(e):
    global _emb_error_logged
    if not _emb_error_logged:
        print(f"embeddings: {type(e).__name__}; keeping available vectors", flush=True)
        _emb_error_logged = True


def taste_vector(p, min_titles=5):
    """Weighted mean of their titles' fingerprints; None until enough titles are embedded."""
    try:
        ensure_emb([(k, kind) for k, kind in enrichment_pairs(p) if femb(k, kind) is None][:400])
    except BudgetExceeded:
        pass
    except Exception as e:
        embedding_failure(e)
    hits = {h["key"] for h in _learn["hits"]} if HIT_TASTE and p.get("learned", True) else set()
    acc, n = [0.0] * EMB_DIM, 0
    with _emb_lock:
        model = _emb_model
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
    vec = TasteVector(x / norm for x in acc)
    vec.model = model
    return vec


def fit(vec, key, kind=None):
    with _emb_lock:
        if getattr(vec, "model", _emb_model) != _emb_model:
            return None
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
# Tagging vocabulary stays English; row titles use the interface language.
EXPERIENCES = {
    "cozy_case": (_translate("experience.cozy_case", lang="en"), "row.experience.cozy_case"),
    "banter_duo": (_translate("experience.banter_duo", lang="en"), "row.experience.banter_duo"),
    "will_they": (_translate("experience.will_they", lang="en"), "row.experience.will_they"),
    "slow_romance": (_translate("experience.slow_romance", lang="en"), "row.experience.slow_romance"),
    "period_society": (_translate("experience.period_society", lang="en"), "row.experience.period_society"),
    "forbidden_love": (_translate("experience.forbidden_love", lang="en"), "row.experience.forbidden_love"),
    "found_family": (_translate("experience.found_family", lang="en"), "row.experience.found_family"),
    "mentor_bond": (_translate("experience.mentor_bond", lang="en"), "row.experience.mentor_bond"),
    "second_chance": (_translate("experience.second_chance", lang="en"), "row.experience.second_chance"),
    "tearjerker": (_translate("experience.tearjerker", lang="en"), "row.experience.tearjerker"),
    "grief_healing": (_translate("experience.grief_healing", lang="en"), "row.experience.grief_healing"),
    _translate("experience.heartwarming", lang="en"): ("heartwarming", "row.experience.heartwarming"),
    "coming_of_age": (_translate("experience.coming_of_age", lang="en"), "row.experience.coming_of_age"),
    "underdog": (_translate("experience.underdog", lang="en"), "row.experience.underdog"),
    "rivalry_training": (_translate("experience.rivalry_training", lang="en"), "row.experience.rivalry_training"),
    "epic_quest": (_translate("experience.epic_quest", lang="en"), "row.experience.epic_quest"),
    "magic_wonder": (_translate("experience.magic_wonder", lang="en"), "row.experience.magic_wonder"),
    "team_spectacle": (_translate("experience.team_spectacle", lang="en"), "row.experience.team_spectacle"),
    "hero_burden": (_translate("experience.hero_burden", lang="en"), "row.experience.hero_burden"),
    "dynasty_power": (_translate("experience.dynasty_power", lang="en"), "row.experience.dynasty_power"),
    "frontier_grit": (_translate("experience.frontier_grit", lang="en"), "row.experience.frontier_grit"),
    "political_intrigue": (_translate("experience.political_intrigue", lang="en"), "row.experience.political_intrigue"),
    "war_sacrifice": (_translate("experience.war_sacrifice", lang="en"), "row.experience.war_sacrifice"),
    "antihero": (_translate("experience.antihero", lang="en"), "row.experience.antihero"),
    _translate("experience.revenge", lang="en"): ("revenge", "row.experience.revenge"),
    "heist": (_translate("experience.heist", lang="en"), "row.experience.heist"),
    "whodunit": (_translate("experience.whodunit", lang="en"), "row.experience.whodunit"),
    "mind_bender": (_translate("experience.mind_bender", lang="en"), "row.experience.mind_bender"),
    "dread": (_translate("experience.dread", lang="en"), "row.experience.dread"),
    "survival": (_translate("experience.survival", lang="en"), "row.experience.survival"),
    "competence": (_translate("experience.competence", lang="en"), "row.experience.competence"),
    "courtroom": (_translate("experience.courtroom", lang="en"), "row.experience.courtroom"),
    "family_secrets": (_translate("experience.family_secrets", lang="en"), "row.experience.family_secrets"),
    "big_ideas": (_translate("experience.big_ideas", lang="en"), "row.experience.big_ideas"),
    "time_twist": (_translate("experience.time_twist", lang="en"), "row.experience.time_twist"),
    "dystopia": (_translate("experience.dystopia", lang="en"), "row.experience.dystopia"),
    "feel_good": (_translate("experience.feel_good", lang="en"), "row.experience.feel_good"),
    "dark_comedy": (_translate("experience.dark_comedy", lang="en"), "row.experience.dark_comedy"),
    "slice_of_life": (_translate("experience.slice_of_life", lang="en"), "row.experience.slice_of_life"),
    "true_story": (_translate("experience.true_story", lang="en"), "row.experience.true_story"),
    "passion_art": (_translate("experience.passion_art", lang="en"), "row.experience.passion_art"),
    "sports_glory": (_translate("experience.sports_glory", lang="en"), "row.experience.sports_glory"),
    "nostalgia": (_translate("experience.nostalgia", lang="en"), "row.experience.nostalgia"),
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
_feed_started = False
_defer_enrichment = False


def fkey(key, kind):
    """Bare slot keeps its original kind; collisions use an alias."""
    rec = _facets.get(key) or {}  # Inspect the bare owner to choose a slot.
    owner = rec.get("kind") if rec else _emb_kinds.get(key)
    # Key-only APIs have no request kind and retain the bare slot.
    return f"{key}@{kind}" if kind and owner not in (None, kind) else key


def frec(key, kind):
    return _facets.get(fkey(key, kind)) or {}


def femb(key, kind):
    with _emb_lock:
        return _emb_get(key, kind, _emb, _emb_kinds)


def _emb_get(key, kind, vectors, kinds):
    rec = _facets.get(key) or {}
    owner = rec.get("kind") if rec else kinds.get(key)
    slot = f"{key}@{kind}" if kind and owner not in (None, kind) else key
    if kind and kinds.get(slot) not in (None, kind):
        slot = f"{key}@{kind}"
    return vectors.get(slot) if not kind or kinds.get(slot) in (None, kind) else None


def origin_countries(det):
    return (det.get("origin_country") or [c["iso_3166_1"] for c in det.get("production_countries") or []
                                         if c.get("iso_3166_1")])[:3]


# Named countries used by setup; other households can use their two-letter country code.
COUNTRY_NAMES = {"US": "United States", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
                 "ES": "Spain", "IT": "Italy", "NL": "Netherlands", "AU": "Australia", "CA": "Canada"}
def rating_country(country=None):
    country = COUNTRY if country is None else country
    return {name.lower(): code for code, name in COUNTRY_NAMES.items()}.get(
                country.strip().lower(), country.strip().upper())


def age_ok(rec, kind, age):
    if rec.get("adult"):
        return False
    cr = rec.get("cr") or {}
    for country in dict.fromkeys((rating_country(), "US")):
        cert = (cr.get(country) or "").strip().upper()
        if country == "US":
            table = ({"G": 0, "PG": 8, "PG-13": 13, "R": 17, "NC-17": 18} if kind == "movie" else
                     {"TV-Y": 0, "TV-Y7": 7, "TV-G": 0, "TV-PG": 8, "TV-14": 14, "TV-MA": 17})
        else:
            table = {"U": 0, "PG": 8, "12A": 12, "12": 12, "15": 15, "18": 18} if country == "GB" else {}
        minimum = table.get(cert)
        if minimum is None and re.fullmatch(r"(?:FSK\s*)?(?:[0-9]|1[0-8])", cert):
            minimum = int(re.sub(r"^FSK\s*", "", cert))
        if minimum is not None:
            return age >= minimum
    return False


def certifications(det, kind):
    cr = {}
    field = "release_dates" if kind == "movie" else "content_ratings"
    for result in (det.get(field) or {}).get("results") or []:
        country = result.get("iso_3166_1")
        if country not in (rating_country(), "US"):
            continue
        values = ([r.get("certification") for r in result.get("release_dates") or []] if kind == "movie" else
                  [result.get("rating")])
        for value in values:
            if not value:
                continue
            minimum = next((a for a in range(19) if age_ok({"cr": {country: value}}, kind, a)), -1)
            previous = next((a for a in range(19) if age_ok({"cr": {country: cr.get(country)}}, kind, a)), -1)
            # Different releases can carry different ratings; keep the strictest usable one.
            if country not in cr or minimum > previous:
                cr[country] = value
    return cr


def backfill_ratings():
    """Worker only: retry at most 200 missing rating records when a child lives here."""
    if not any(kids_age(p) is not None for p in PEOPLE):
        return
    with _facets_lock:
        todo = [(k, r["kind"]) for k, r in _facets.items() if "cr" not in r and k.startswith("tmdb:")
                and r.get("kind") in ("movie", "tv")][:200]
    for k, kind in todo:
        try:
            rec = facet_record(kind, k[5:].split('@')[0])
        except Exception:
            continue
        with _facets_lock:
            if k in _facets and _facets[k].get("kind") == kind:
                _facets[k].update(cr=rec["cr"], adult=rec["adult"])
                _facets_dirty.set()
    if todo:
        clear_seed_cache()


def backfill_countries():
    """Worker only: fill optional country facts in at most 200 cached titles per pass."""
    if not any(CONFIG["taste"].get("people", {}).get(p, {}).get("prefs", {}).get("focus", {}).get("countries") for p in PEOPLE):
        return
    with _facets_lock:
        todo = [(k, r.get("kind")) for k, r in _facets.items() if "oc" not in r and k.startswith("tmdb:")
                and r.get("kind") in ("movie", "tv")][:200]
    for k, kind in todo:
        try:
            det = tmdb(f"{kind}/{k[5:].split('@')[0]}", language="en-US")
        except Exception:
            continue  # Leave failed records missing so the next pass retries them.
        with _facets_lock:
            if k in _facets and _facets[k].get("kind") == kind:
                _facets[k]["oc"] = origin_countries(det)
                _facets_dirty.set()
    if todo:
        clear_seed_cache()

def backfill_runtime():
    """Worker only: fill missing runtime when that type has a personal limit."""
    kinds = {('tv' if kind == 'series' else 'movie') for p in PEOPLE
             for kind in CONFIG['taste'].get('people', {}).get(p, {}).get('prefs', {}).get('runtime', {})}
    if not kinds:
        return
    with _facets_lock:
        todo = [(key, rec['kind']) for key, rec in _facets.items() if 'runtime' not in rec
                and key.startswith('tmdb:') and rec.get('kind') in kinds][:200]
    for key, kind in todo:
        try:
            det = tmdb(f"{kind}/{key[5:].split('@')[0]}", language='en-US')
        except Exception:
            continue
        minutes = det.get('runtime') if kind == 'movie' else next(iter(det.get('episode_run_time') or []), None)
        with _facets_lock:
            if key in _facets and _facets[key].get('kind') == kind:
                _facets[key]['runtime'] = minutes
                _facets_dirty.set()
    if todo:
        clear_seed_cache()


def facet_record(kind, tid, det=None):
    if det is None:
        det = tmdb(f"{kind}/{tid}", language="en-US", append_to_response="keywords,credits,release_dates" if kind == "movie" else "keywords,credits,content_ratings")
    kw = det.get("keywords") or {}
    credits = det.get("credits") or {}
    if kind == "movie":
        makers = [c for c in credits.get("crew") or [] if c.get("job") == "Director"][:2]
    else:
        makers = (det.get("created_by") or [])[:3]
    return {**week_release_facts(det, kind), "k": [[k["id"], k["name"]] for k in (kw.get("keywords") or kw.get("results") or [])
                  if k["name"] not in KW_STOP][:20],
            "d": [[c["id"], c["name"]] for c in makers],
            "a": [[c["id"], c["name"]] for c in (credits.get("cast") or [])[:3]],
            "n": [[n["id"], n["name"]] for n in (det.get("networks") or [])[:3]],
            "c": [[c["id"], c["name"]] for c in (det.get("production_companies") or [])[:4]],
            "g": genre_names([g["name"] for g in det.get("genres") or []]),
            "cr": certifications(det, kind), "adult": bool(det.get("adult")),
            "oc": origin_countries(det), "lang": det.get("original_language"), "title": det.get("title") or det.get("name") or "",
            "runtime": det.get('runtime') if kind == 'movie' else next(iter(det.get('episode_run_time') or []), None),
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


def tag_experiences(pairs, max_batches=4, fetch_facets=True):
    """Experience tags for known titles without tags, TAG_BATCH per LLM call. Worker only (seconds per call),
    never on a search. pairs: [(key, kind)] in priority order; returns how many titles got tags."""
    if not OR_KEY or _defer_enrichment:
        return 0
    if fetch_facets:
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
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
    if fid.startswith("x:") and fid[2:] in EXPERIENCES and name == EXPERIENCES[fid[2:]][0]:
        return t("experience." + fid[2:])
    if fid[0] not in "kts":
        return name
    return re.sub(r"\((\w+)\)", lambda g: f"({g[1].upper()})", name[:1].upper() + name[1:])


def person_entries(p, person):
    """Their own titles plus what the household watches together on the TV."""
    seeds = {(title.casefold(), year, kind) for who, title, year, kind in person_loves() if who == person}
    overridden = {e["key"] for e in p["entries"] if e.get("who") == "remote" and person in e.get("by", ())}
    # Title fixes: a moved watch belongs to its new member; an ignored plain watch stays in history with no taste weight.
    moves = title_moves()
    ignored = {r["key"] for r in title_fixes() if r.get("op") == "ignore" and r.get("who") == person}
    out = []
    for e in p["entries"]:
        by = e.get("by", ())
        origin = watch_owners(e, moves).get(person)
        if origin is None and person in by and person in PEOPLE:
            continue
        origin = person if origin is None and person in by else origin
        mover = origin if origin != person else None
        seed = e["key"] not in overridden and any(title == e["title"].casefold() and kind == e["kind"]
                                                and (not year or year == e.get("year")) for title, year, kind in seeds)
        if origin is None and ("together" not in by or person in guest_people(CONFIG)) and not seed:
            continue
        opinions = e.get("opinions") or {}
        own = opinions.get(person) or (opinions.get(mover) if mover else None)
        explicit = person in by or bool(own) or seed or bool(mover)
        value = own or e.get("shared") or {"signal": e["signal"], "weight": e["weight"]}
        if person == "together" and _taste_accounts():
            value = {**value, **{k: e[k] for k in ("age", "genres", "title", "year")}}
        if seed and e.get("who") != "remote":
            value = {"signal": "loved", "weight": max(value["weight"], SEED_WEIGHT), "item": {}, "flags": ["told"]}
        if e["key"] in ignored and value["signal"] == "watched":
            value = {**value, "weight": 0}
        # Shared passive plays are weaker than an opinion explicitly attributed to this person.
        out.append({**e, **value, "weight": value["weight"] * (1 if explicit or person == "together" else .35),
                    "explicit": explicit})
    return out


def enrichment_pairs(p, people=None):
    """Round-robin member histories, positives first within each, one turn per distinct title."""
    queues = []
    people = PEOPLE if people is None else people
    for person in (*people, "together"):
        entries = person_entries(p, person)
        queues.append(deque((e["key"], e["kind"]) for e in
                            [e for e in entries if e["weight"] > 0] + [e for e in entries if e["weight"] <= 0]))
    out, seen = [], set()
    while any(queues):
        for queue in queues:
            while queue and queue[0] in seen:
                queue.popleft()
            if queue:
                pair = queue.popleft()
                seen.add(pair)
                out.append(pair)
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
                npos.setdefault(fid, set()).add((e["key"], e["kind"]))
            else:
                neg[fid] = neg.get(fid, 0) - w
    npos = {fid: len(titles) for fid, titles in npos.items()}
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
CALIBRATION_MAX, CALIBRATION_MIN = 800, 200


def quality_prior(rec):
    """At most .02: votes are a weak prior, never a substitute for taste evidence."""
    if not QUALITY_PRIOR:
        return 0.0
    return .02 * max(0, min(10, (rec or {}).get("vote_average") or 0))/10 * min(1, math.log1p(max(0, (rec or {}).get("vote_count") or 0))/math.log1p(10000))


def calibration_pairs(p, store):
    excluded = p.get("seen", set()) | p.get("negative", set())
    return {(slot.partition("@")[0], rec.get("kind") or slot.partition("@")[2] or "movie")
            for slot, rec in store.items() if rec and not rec.get("adult") and slot.partition("@")[0] not in excluded}


def build_model(p, enrich=True):
    """Taste model of the household: fingerprint centroid, facet affinities (also per person), the loved titles
    behind each facet (for reasons) and a match calibration."""
    global _seed_stats
    house = adult_profile(p)
    house = {**house, "entries": taste_entries(house["entries"])} if house else house
    pos_entries = [e for e in house["entries"] if e["weight"] > 0]
    pairs = enrichment_pairs(p, member_people())
    try:
        ensure_facets([(k, kind) for k, kind in pairs if not frec(k, kind)][:400])
    except Exception as e:
        print(f"facets: {type(e).__name__}: {e}", flush=True)
    if enrich:
        # Facets were fetched above within this build's cap, including kind aliases.
        tag_experiences(enrichment_pairs({"entries": pos_entries}, adult_people()), max_batches=16, fetch_facets=False)
    vec = taste_vector(house) if enrich else None
    hits = {h["key"] for h in learning_records('hits')} if HIT_TASTE else set()
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
    aff, npos = affinities(house["entries"], store, df, n_all, hits)
    holders = {}
    for e in pos_entries:
        for fid, _ in facet_pairs(store.get(fkey(e["key"], e["kind"])) or {}):
            hs = holders.setdefault(fid, {})
            title = (e["key"], e["kind"])
            hs[title] = max(hs.get(title, 0), e["weight"])
    holders = {fid: sorted(((w, fkey(key, kind)) for (key, kind), w in hs.items()), reverse=True)[:40]
               for fid, hs in holders.items() if fid in aff}
    m = {"vec": vec, "aff": aff, "names": names, "npos": npos, "bscale": 1.0, "cal": None, "cal_reference": None, "vbase": 0.0, "bg": [],
         "kinds": {fkey(e["key"], e["kind"]): e["kind"] for e in p["entries"]}, "holders": holders,
         "ent": {fkey(e["key"], e["kind"]): (e["title"], tuple(e.get("by") or ())) for e in pos_entries},
         "paff": {}, "pvec": {}, "px": {}, "pbscale": {}, "pbg": {}, "nexp": {}, "x": experience_profile(house["entries"])}
    active = active_feedback()
    for person in PEOPLE:
        mine = person_entries(p, person) if person not in guest_people(CONFIG) else []
        if _taste_accounts():
            m.setdefault("panchors", {})[person] = {fkey(e["key"], e["kind"]) for e in mine if e["weight"] > 0}
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
            m["pvec"][person] = taste_vector({"entries": seeds, "learned": False}, min_titles=1) if enrich else None
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
    # Rank against known unseen titles; small households still use the trending baseline.
    try:
        excluded = p["seen"] | p.get("negative", set())
        bg = calibration_pairs(p, store)
        bg = sorted(bg, key=lambda pair: hashlib.sha256(f"{pair[0]}@{pair[1]}".encode()).digest())
        if len(bg) >= CALIBRATION_MIN:
            m["cal_reference"] = "known"
        elif enrich:
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
            except Exception as e:
                embedding_failure(e)
            bg = [(k, kind) for k, kind in dict(bg).items() if k not in excluded]
            m["cal_reference"] = "trending"
        else:
            bg = []
        # Missing fingerprints contribute no evidence, matching replay blends; percentile uses the full score.
        m["bg"] = sorted(s for s in (score_key(m, k, kind=kind)[0] for k, kind in bg[:CALIBRATION_MAX]) if s is not None)
        if len(m["bg"]) >= 20:
            m["cal"] = len(m["bg"])
            for person in (*PEOPLE, "together"):
                age = kids_age(person)
                scoped = [(k, kind) for k, kind in bg if age is None or age_ok(frec(k, kind), kind, age)][:CALIBRATION_MAX]
                m["pbg"][person] = sorted(s for k, kind in scoped
                                          if (s := person_score(m, k, person, kind)[0]) is not None)
                if person in m.get("mem0", {}):
                    block = m["mem0"][person]
                    strict = {**m, **{name: {**m[name], person: block[name]} for name in ("paff", "pbscale")}}
                    block["pbg"] = sorted(s for k, kind in scoped
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
    if person == GUEST or person in guest_people(CONFIG):
        return {"state": "guest", "explicit": 0, "need": PCT_MIN_EXPLICIT}
    if person in PEOPLE:
        n = counts.get(person, 0)
    elif "household" in counts and len(adult_people()) == len(PEOPLE):
        n = counts["household"]
    else:
        people = adult_people()
        key = (people, tuple(counts.get(p, 0) for p in people), len((m or {}).get("ent", {})))
        cached = _maturity_house
        if cached[0] is m and cached[1] == key:
            n = cached[2]
        else:
            overlap = sum(max(0, sum(p in by for p in people)-1) for title, by in (m or {}).get("ent", {}).values())
            # A model's evidence is immutable; avoid recounting the same titles for every card.
            n = max(0, sum(key[1])-overlap)
            _maturity_house = (m, key, n)
    state = "too_new" if n < PCT_MIN_EXPLICIT else "learning" if person in PEOPLE and n < PRIOR_FULL else "ready"
    return {"state": state, "explicit": n, "need": PCT_MIN_EXPLICIT}


def person_score(m, key, person=None, kind=None):
    raw, fids = score_key(m, key, person, kind)
    if not HOUSEHOLD_PRIOR or person not in PEOPLE or _taste_accounts():
        return raw, fids
    lam = min(1, (m or {}).get("nexp", {}).get(person, 0)/PRIOR_FULL)
    if lam == 1:
        return raw, fids
    house, house_fids = score_key(m, key, kind=kind)
    if raw is None or house is None:
        return house, house_fids
    return lam*raw + (1-lam)*house, fids


_viewer = contextvars.ContextVar("viewer", default=None)
_safety_context = contextvars.ContextVar("safety_context", default=None)
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


def score_key(m, key, person=None, kind=None, *, rec=None):
    """(score, facet ids worth naming) for a title, for the household or one person; score None when unknown."""
    if not m:
        return None, []
    supplied = {'rec': rec} if rec is not None else {}
    rec = frec(key, kind) if rec is None else rec
    if not rec:
        return None, []
    if person == "together":
        house, house_fids = score_key(m, key, kind=kind, **supplied)
        if house is None:
            return None, []
        adults = adult_people()
        if not adults:
            return None, []
        scores = [score_key(m, key, pp, kind, **supplied) for pp in adults]
        values = []
        for pp, (s, _) in zip(adults, scores):
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
    if kids_age(safety_subject(person)) is not None:
        return None
    best = None
    v = femb(key, kind)
    for w, hk in m["holders"].get(fid, []):
        if hk == fkey(key, kind) or hk in used or hk not in m["ent"]:
            continue
        by = m["ent"][hk][1]
        if person and person not in by and "together" not in by:
            continue
        if person in PEOPLE and _taste_accounts() and hk not in m.get("panchors", {}).get(person, ()):
            continue
        raw = fit(v, hk, m.get("kinds", {}).get(hk))
        if fid[0] not in "xdn" and raw is not None and raw < 0.4:
            continue  # a shared keyword alone ('dragon') makes a silly comparison
        sim = (raw or 0.0) + 0.03 * min(w, 4) + (0.3 if person and person in by else 0)
        if best is None or sim > best[0]:
            best = (sim, hk)
    return (best[1], *m["ent"][best[1]]) if best else None


def reason_details(m, key, fids, n=2, person=None, kind=None):
    """Localized reasons paired with their anchor identity, or None for a facet."""
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
            out.append((t("why.anchor", name=name, title=a[1], person="" if person else who_label(a[2])), a[0]))
        elif fid[0] not in 'kcn':
            out.append((name, None))
    return out


def reasons(m, key, fids, n=2, person=None, kind=None):
    return [text for text, _ in reason_details(m, key, fids, n, person, kind)]


def confidence(m, key, s, fids, person=None):
    """'Strong fit' when the match is high and two loved titles share its main reason, 'Good fit', else 'Wildcard'."""
    m = person_model(m, person)
    pct = match_pct(m, s, person)
    if pct is None:
        return None, None
    support = len(m["holders"].get(fids[0], [])) if fids else 0
    if pct >= 88 and support >= 2:
        return t("match.strong"), pct
    return (t("match.good"), pct) if pct >= 70 else (t("match.wildcard"), pct)


def explain(key, kind=None, person=None):
    """Everything behind one title's note, for /why/<key>.json."""
    top = rowplan.title(SimpleNamespace(**globals()), key, kind, person)
    m = _model["m"]
    if not m:
        return {"key": key, "model": None, **top}
    s, fids = score_key(m, key, kind=kind)
    conf, pct = confidence(m, key, s, fids)
    rec = frec(key, kind)  # /why has no type; internal callers can supply one.
    held, now, memory = {fid for fid, _ in facet_pairs(rec)}, time.time(), memory_load()
    def facts(pp, kind):
        return [{"id": f["id"], "kind": f["kind"], "src": f["src"], "strength": memory_strength(f, now)}
                for f in memory["facts"] if f["person"] == pp and f["status"] == "active"
                and f["kind"] == kind and f["about"] in held]
    allf = sorted(((m["aff"][fid], fid) for fid, _ in facet_pairs(rec) if fid in m["aff"]), reverse=True)
    from skald.reach import title as home_title
    home = home_title(SimpleNamespace(**globals()), key, kind)
    with _feeds_lock:
        catalog_rows = list(dict.fromkeys(cid for store in (_feeds, _shown, *_shown_scoped.values()) for (cid, lang), row in store.items() if lang == LANG
            for xs in (row.get('metas') or {}).values() if isinstance(xs, list) for x in xs
            if isinstance(x, dict) and x['id'] == key and (kind is None or kind == ('movie' if x['type'] == 'movie' else 'tv'))
            and (x.get('_row') or {}).get('feeling_source') == 'catalog'))
    return {"key": key, "title": rec.get("title"), "score": s, "percentile": pct, "confidence": conf,
            **home['household'], **top,
            "feeling_catalog_rows": catalog_rows,
            "fit": fit(m["vec"], key, kind), "reasons": reasons(m, key, fids, 3, kind=kind),
            "experiences": rec.get("x"),
            "per_person": {pp: {"score": person_score(m, key, pp, kind)[0], "n_explicit": m["nexp"].get(pp, 0),
                                "maturity": maturity(m, pp), **home[pp],
                                "percentile": confidence(m, key, *person_score(m, key, pp, kind), pp)[1],
                                "memory": {"likes": facts(pp, "like"), "avoids": facts(pp, "avoid"),
                                           "vetoed": bool(held & veto_abouts(pp, now, memory))}}
                           for pp in PEOPLE},
            "facets": [{"id": fid, "name": m["names"].get(fid), "affinity": round(a, 3),
                        "anchors": [m["ent"][k][0] for _, k in m["holders"].get(fid, [])[:4] if k in m["ent"]]}
                       for a, fid in allf[:12]],
            "archetypes": {who: archetype_view(who) for who in ("household", *adult_people())}}


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


def taste_hint(m, person=None):
    """Search tie-breakers: experiences and themes, without favourite people or studios."""
    aff = m.get("paff", {}).get(person) or m["aff"]
    return taste_line({**m, "aff": {fid: a for fid, a in aff.items() if fid[0] in "xkt"}})


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
    if not build:
        return _model['m'] if _profiles.get('household') else None
    with _home_lock:
        generation, p = _erasure_generation, _profiles.get("household")
    if not p:
        return None
    with _model_lock:
        with _memory_lock:
            seq = _memory_read()["seq"]
        sig = (p.get("hash"), len(_learn["hits"]) if HIT_TASTE else 0, local_now().strftime("%Y-%m-%d"), seq, _emb_model, title_fix_stamp())
        with _facets_lock:
            sig += (len(calibration_pairs(p, _facets)) >= CALIBRATION_MIN,)
        if len(adult_people()) != len(PEOPLE):
            sig += (adult_people(),)
        if _model["sig"] != sig or _model["m"] is None:
            m = build_model(p, enrich=False) if _defer_enrichment else build_model(p)
            with _home_lock:
                if generation != _erasure_generation:
                    return None
                _model.update(m=m, sig=sig)
                print(f"taste model: {len(m['aff'])} facets, cal {m['cal']}, {m['taste_line'][:160]}", flush=True)
                taste_ring_note(m, time.time())
        return _model["m"]


def rank_by_taste(m, items, enrich=True):
    """items: [(key, kind, payload)] -> sorted best first, as [(score, why, key, kind, payload)]; unknowns last."""
    if enrich:
        pairs = [(k, kind) for k, kind, meta in items if not isinstance(meta, dict)
                 or (meta.get('_row') or {}).get('feeling_source') != 'catalog']
        try:
            ensure_facets(pairs)
            ensure_emb(pairs)
        except BudgetExceeded:
            pass
        except Exception as e:
            embedding_failure(e)
        tag_experiences(pairs[:50], max_batches=2)
    scored = [(score_key(m, k, kind=kind), i, k, kind, x) for i, (k, kind, x) in enumerate(items)]
    scored.sort(key=lambda t: (t[0][0] is None, -(t[0][0] or 0), t[1]))
    return [(sw[0], sw[1], k, kind, x) for sw, _, k, kind, x in scored]


def with_match(meta, why=True, floor=65, person=None):
    note = match_note(meta["id"], why, floor, person, "movie" if meta["type"] == "movie" else "tv")
    if not note:
        return meta
    return {**meta, "description": " · ".join(dict.fromkeys(note + ([meta["description"]] if meta.get("description") else [])))}


def taste_order(metas, sort=True, why=True):
    """Metas best match first (stable for unknowns) with optional short reasons; unchanged without a model."""
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
_memory_forget_stamp = None


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


@household_write
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
    with household_writable(DATA, household_erased) as allowed:
        if not allowed:
            return False
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
    global _memory, _memory_forget_stamp
    try:
        marker = os.stat(os.path.join(DATA, "history-forgotten.json"))
        stamp = (DATA, marker.st_mtime_ns, marker.st_size)
    except FileNotFoundError:
        stamp = None
    if stamp != _memory_forget_stamp:
        _memory, _memory_forget_stamp = None, stamp
        _memory_index.clear()
        _memory_veto.clear()
    if _memory is None:
        path = os.path.join(DATA, "memory.json")
        try:
            with open(path) as fh:
                value = json.load(fh)
            if not isinstance(value, dict) or value.get("ver") != 1 or not isinstance(value.get("facts"), list) \
                    or type(value.get("seq")) is not int or value["seq"] < 0 or not isinstance(value.get("forgotten"), dict) \
                    or not math.isfinite(value["at"]) or value["stamp"] is not None and not isinstance(value["stamp"], str) \
                    or any(p not in retained_people() or not math.isfinite(t) for p, t in value["forgotten"].items()):
                raise ValueError("invalid memory store")
            ids = set()
            for f in value["facts"]:
                if f["person"] not in retained_people() or f["kind"] not in ("like", "avoid") \
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
            with household_writable(DATA, household_erased) as allowed:
                if allowed:
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
    with history_locked(DATA), _memory_lock:
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
    sig = (store["seq"], who, int(now//DAY), PEOPLE)
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
    with history_locked(DATA), _memory_lock:
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
        # A plain no to a New to You stretch is not evidence for an avoid memory (an explicit reason still is).
        stretch = kind == "avoid" and (ev.get("ctx") or {}).get("row") == rid('newtoyou')
        family, week = memory_family(src), memory_week(ev["t"]) if ev["t"] is not None else None
        for person in people:
            for fid in held:
                if not stretch and fid in candidates[person] and candidates[person][fid][0] == kind:
                    collect(support, (person, fid, kind), ev, family, week)
                opposite = live.get((person, fid, "avoid" if kind == "like" else "like"))
                if opposite and not stretch and ev["action"] in MEMORY_EXPLICIT and ev["t"] is not None \
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
        if f['person'] not in PEOPLE or not _memory_live(f):
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
    with history_locked(DATA), _memory_lock:
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
    with history_locked(DATA), _memory_lock:
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
    with history_locked(DATA), _memory_lock:
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
            for f in store["facts"] if f['person'] in PEOPLE and _memory_live(f) and (person is None or f["person"] == person)]


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
        if _memory_index.get("seq") != store["seq"] or tuple(_memory_index.get('people', {})) != PEOPLE:
            _memory_index.clear()
            _memory_index.update(seq=store["seq"], people={p: {} for p in PEOPLE})
            for f in store["facts"]:
                if f['person'] in PEOPLE and f["status"] == "active" and f["kind"] == "like":
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
    why = ((t("why.both") if len(PEOPLE) == 2 else t("why.all")) if person == "together" else t("why.told") if told else t("why.like")) + label
    if len(why) > 60 and person != "together":
        why = t("why.like") + label
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
            with household_writable(DATA, household_erased) as allowed:
                if not allowed:
                    return False
                with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    fh.write(json.dumps({day: {p: base64.b64encode(raw).decode() for p, raw in people.items()}
                                         for day, people in _taste_ring.items()}, separators=(",", ":")))
                os.replace(path + ".tmp", path)
        except OSError:
            pass


# --- searches ---------------------------------------------------------------

_last_search_person = None


def log_search(query, deferred=False):
    global _last_search_person
    if len(query) < 3 and not deferred:
        return
    now = time.time()
    person = SCOPES.get(getattr(_scoping, "scope", None))
    if person not in PEOPLE:
        person = _viewer.get() or who_watching()[0]
    person = person if person in PEOPLE else None
    with _ai_lock:
        duplicate = len(query) < 3 or bool(_searches and _searches[-1][1] == query and now - _searches[-1][0] < 30 and person == _last_search_person)
        if not duplicate:
            _searches.append((now, query))
            _last_search_person = person
            del _searches[:-500]
    record = {"t": int(now), "q": query, **({"p": person} if person else {})}
    if deferred:
        return record, duplicate
    if not duplicate:
        write_search(record)


def write_search(record, timing=None):
    if getattr(_scoping, 'private_command', False):
        return
    try:
        with history_locked(DATA), household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            if record.get('p') in history_cutoffs(DATA) and record['p'] not in load_config(DATA, readonly=True)[0]['household']['members']:
                return False
            with open(os.path.join(DATA, "searches.jsonl"), "a") as fh:
                fh.write(json.dumps({**record, **({"timing": timing} if timing else {})}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def private_search(query):
    _scoping.private_command = True
    with _ai_lock:
        _searches[:] = [(at, text) for at, text in _searches if text != query]


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
    """'14 Oct', 'Tue 14 Oct', '3 Feb 2027' (year only when not this year); German '14. Okt'."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    out = t("date.day", day=d.day, month=t(MONTHS[d.month - 1])) + (f" {d.year}" if d.year != local_now().year else "")
    return f"{t(WD_SHORT[d.weekday()])} {out}" if weekday else out


def top_seeds(p, n=3):
    """Strongest recent positive titles; the because row rotates through them day by day."""
    cands = [e for e in p["entries"] if e["weight"] > 0 and e["signal"] in ("loved", "liked", "watched")] \
        or [e for e in p["entries"] if e["weight"] > 0]
    return sorted(cands, key=lambda e: -e["weight"] / (1 + e["age"] / 14))[:n]


def seed_label(e):
    if e["signal"] in ("loved", "liked"):
        return "row.seed." + e["signal"]
    if e["signal"] == "watched":
        return "row.seed.watched" if e["stype"] == "movie" else "row.seed.watch"
    return "row.seed.interest"


def seed_phrase(e):
    return t(seed_label(e), title=e["title"])


def because_feed(lang):
    p = adult_profile(_profiles.get("household"))
    seeds = because_seeds(p)
    m = get_model() if seeds else None
    now = int(time.time())
    positive = {(e["key"], e["stype"]): e for e in (p or {}).get("entries", [])
                if e["weight"] > 0 and e["signal"] in ("loved", "liked", "watched")}
    out, names, anchors = {}, {}, []
    kinds, starts, previous, retained, free = {}, {}, {}, {}, list(BECAUSE_ROWS)
    releasable = set()
    with _feeds_lock:
        for cid in BECAUSE_ROWS:
            records = [(l, rec) for store in (_shown, *_shown_scoped.values(), _feeds)
                       for (row, l), rec in sorted(store.items(), key=lambda x: (x[0][1] != LANG, x[0]))
                       if row == cid and (rec.get("metas") or (rec.get("names") or {}).get("slot_kind"))]
            types = [{x["type"] for typ, xs in (rec.get("metas") or {}).items()
                      if typ in ("movie", "series", "tv") for x in xs} for _, rec in records]
            # Keep the latest kind change across languages, even after seed rotation.
            pin = max((rec for _, rec in records
                       if (rec.get("names") or {}).get("slot_kind") in ("movie", "series")),
                      key=lambda rec: (rec.get("names") or {}).get("slot_since", rec.get("built", now)), default={})
            kinds[cid] = (pin.get("names") or {}).get("slot_kind")
            if kinds[cid] is None and types and len(types[0]) == 1:
                kinds[cid] = next(iter(types[0]))
                pin = records[0][1]
            starts[cid] = (pin.get("names") or {}).get("slot_since", pin.get("built", now))
            previous[cid] = next((rec for (l, rec), ts in sorted(zip(records, types), key=lambda x: x[0][0] != lang)
                                  if l == lang and kinds[cid] and ts <= {kinds[cid]}), {})
    masks = row_masks("both")
    blocked = masks[0] | (masks[2] if len(masks) > 2 else set())
    for cid in free:
        seed = positive.get(((previous[cid].get("names") or {}).get("seed"), kinds[cid]))
        if not seed:
            if kinds[cid]:
                releasable.add(cid)
            previous[cid] = {}
            continue
        metas = [x for typ, xs in (previous[cid].get("metas") or {}).items()
                 if typ in ("movie", "series", "tv") for x in xs]
        if not any(x["id"] not in blocked and preference_ok(x, cid, "both")
                   and row_eligible(x, cid, lang, p, seed=seed["key"]) for x in metas):
            releasable.add(cid)
            previous[cid] = {}
        else:
            retained[cid] = seed
    # Empty slots try all same-kind seeds before taking another kind or filling new slots.
    for release, seed in [(False, e) for e in seeds] + [(True, e) for e in seeds if releasable]:
        stype, kind = seed["stype"], seed["kind"]
        cid = next((cid for cid in free if kinds[cid] == stype and
                    (previous[cid].get("names") or {}).get("seed") == seed["key"]), None) or \
            next((cid for cid in free if kinds[cid] == stype), None) or \
            next((cid for cid in free if kinds[cid] is None and not releasable), None)
        if release:
            cid = next((cid for cid in free if cid in releasable or kinds[cid] is None), None)
        if cid is None:
            continue
        rec = frec(seed["key"], kind)
        anchor = {"id": seed["key"], "type": stype, "name": seed["title"], "_row": {"collection": rec.get("collection")}}
        universe = universe_key(anchor)
        if any(franchise_key(anchor) == franchise_key(a) or universe and universe == universe_key(a) or
               xsim(rec.get("x"), tags) >= BECAUSE_SEED_SIM for a, tags in anchors):
            continue
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
            metas.append(row_meta(it, stype, kind, genre_names, art.get(it["id"], {}), note=t("note.like", title=seed["title"]), lang=lang))
        rank_by_taste(m, [(x["id"], kind, x) for x in metas])
        metas = because_rank([x for x in metas if row_eligible(x, cid, lang, p, seed=seed["key"])], seed["key"])
        if not metas or cid in releasable and not any(
                x["id"] not in blocked and preference_ok(x, cid, "both") for x in metas):
            continue
        anchors.append((anchor, rec.get("x")))
        free.remove(cid)
        out[cid] = {stype: metas[:30]}
        names[cid] = {"title": seed_phrase(seed), "seed": seed["key"],
                      "label_key": seed_label(seed), "fields": {"title": seed["title"]},
                      "slot_kind": stype, "slot_since": now if kinds[cid] != stype else starts[cid]}
        if not free:
            break
    for cid in free:
        if kinds[cid]:
            seed = retained.get(cid)
            if seed:
                rec = frec(seed["key"], seed["kind"])
                anchor = {"id": seed["key"], "type": seed["stype"], "name": seed["title"], "_row": {"collection": rec.get("collection")}}
                universe = universe_key(anchor)
                if any(franchise_key(anchor) == franchise_key(a) or universe and universe == universe_key(a) or
                       xsim(rec.get("x"), tags) >= BECAUSE_SEED_SIM for a, tags in anchors):
                    previous[cid] = {}
                else:
                    anchors.append((anchor, rec.get("x")))
            out[cid] = previous[cid].get("metas") or {}
            names[cid] = {**(previous[cid].get("names") or {}), "slot_kind": kinds[cid], "slot_since": starts[cid]}
    return {**out, "_names": names}


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
            note, rank = t("note.continue", season=resume[0], episode=resume[1]), 1
        elif nxt and nxt == la_se and fresh:
            note, rank = t("note.episode.new", season=nxt[0], episode=nxt[1], date=day_text(la["air_date"], True)), 0
        elif nxt and e["age"] <= 60:
            note, rank = t("note.episode.next", season=nxt[0], episode=nxt[1]), 2
        elif not nxt and na.get("air_date") and 0 <= (datetime.date.fromisoformat(na["air_date"]) - today).days <= 10:
            note, rank = t("note.episode.on", season=na["season_number"], episode=na["episode_number"], date=day_text(na["air_date"], True)), 1
        if note:
            ranked.append((rank, e["age"], row_meta(det, "series", "tv", names, en, note=note, lang=lang)))
    return {"series": [m for _, _, m in sorted(ranked, key=lambda x: (x[0], x[1]))]}


def top10_feed(lang):
    out = {}
    for stype, kind in (("movie", "movie"), ("series", "tv")):
        data, art = with_art(f"trending/{kind}/week", lang)
        names = genres(kind, lang)
        out[stype] = []
        for i, it in enumerate(data, 1):
            out[stype].append(row_meta(it, stype, kind, names, art.get(it["id"], {}),
                                      note=" · ".join([t("note.rank", rank=i)] + match_note(f"tmdb:{it['id']}", kind=kind)), lang=lang))
    return out


def old_movie_premiere(dates):
    cutoff = local_now().date() - datetime.timedelta(days=60+365)
    return any((day := date_or_none(value)) and day < cutoff for value in dates)


def digital_feed(lang):
    today = local_now().date()
    since = today - datetime.timedelta(days=60)
    country_code = rating_country(CONFIG['region']['country'])
    data, art = [], {}
    for page in range(1, 4):
        more, more_art = with_art('discover/movie', lang, page=page, region=country_code, with_release_type=4,
                                 **{'release_date.gte': since.isoformat(), 'release_date.lte': today.isoformat(),
                                    'sort_by': 'popularity.desc', 'include_adult': 'false'})
        data += more
        art.update(more_art)
    data = list({it['id']: it for it in data}.values())
    names, out = genres('movie', lang), []
    for it in sorted(data, key=lambda x: x.get('popularity') or 0, reverse=True):
        if len(out) >= 60:
            break
        if it.get('adult'):
            continue
        releases = tmdb(f"movie/{it['id']}/release_dates").get('results', [])
        if old_movie_premiere((r.get('release_date') or '')[:10] for country in releases
                              for r in country.get('release_dates', []) if r.get('type') in (1, 2, 3)):
            continue
        if any(r.get('type') == 4 and (day := date_or_none((r.get('release_date') or '')[:10]))
               and since <= day <= today for country in releases if country.get('iso_3166_1') == country_code
               for r in country.get('release_dates', [])):
            out.append(row_meta({**it, 'release_dates': {'results': releases}}, 'movie', 'movie', names, art.get(it['id'], {}), lang=lang))
    return {'movie': out}


def toprated_feed(lang):
    jobs = {kind: [_pool.submit(tmdb, f'{kind}/top_rated', language=lang, page=page)
                   for page in range(1, pages+1)] for kind, pages in (('movie', 10), ('tv', 5))}
    out = {}
    for stype, kind in (('movie', 'movie'), ('series', 'tv')):
        data = {}
        for job in jobs[kind]:
            data.update((it['id'], it) for it in job.result().get('results', []))
        names = genres(kind, lang)
        out[stype] = [row_meta(it, stype, kind, names, {}, lang=lang)
                      for it in data.values() if not it.get('adult') and (it.get('vote_count') or 0) >= (200 if kind == 'movie' else 100)
                      and (kind == 'movie' or not NO_TALK & set(it.get('genre_ids') or []))]
    return out


def saga_name(col, en):
    """English collection name without the 'Collection' suffix."""
    name = en.get("name") or col.get("name") or ""
    return re.sub(r"\s*(Collection|Filmreihe|Sammlung)$", "", name).strip()


def details_pair(kind, tid, lang, *, cache_only=False):
    fetch = partial(tmdb, _cache_only=True) if cache_only else tmdb
    jobs = [_pool.submit(fetch, f"{kind}/{tid}", language=l) for l in (lang, "en-US")]
    return jobs[0].result(), jobs[1].result()


def rewatch_feed(lang):
    """Favourites not seen for a while, plus the household's sagas: the next part if one is under way, else part 1."""
    p = adult_profile(_profiles.get("household")) or {"entries": []}
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
        note = t("note.seen.months", months=months) if months < 24 else t("note.unseen.long") if e["item"].get("at") \
            else t("note.seen")
        if e.get("who") == "remote":  # taste remote: feedback time is not watch time.
            note = t("note.seen.marked")
        out[e["stype"]].append(row_meta(det, e["stype"], e["kind"], genres(e["kind"], lang), en, note=note, lang=lang))
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
            note = t("note.collection.next", part=i + 1, count=len(parts), saga=saga)
        else:
            i = 0
            note = t("note.collection.films", saga=saga, count=len(parts)) if len(parts) > 1 else saga
        if f"tmdb:{parts[i]['id']}" in recent and not done:
            continue
        out["movie"].append(row_meta(parts[i], "movie", "movie", names, art.get(parts[i]["id"], {}), note=note, collection=c, lang=lang))
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
        when = None
        if nxt:
            when = local_now(nxt["airingAt"])
            note = t("note.anime.episode", episode=nxt["episode"], weekday=t(WD_SHORT[when.weekday()]), day=when.day, month=t(MONTHS[when.month - 1]), time=f"{when:%H:%M}")
        else:
            sd = m.get("startDate") or {}
            when = datetime.date(sd["year"], sd.get("month") or 12, sd.get("day") or 28) if sd.get("year") else None
            note = (t("note.starts") + (day_text(datetime.date(sd["year"], sd["month"], sd["day"])) if sd.get("day") and sd.get("month")
                                 else f"{t(MONTHS[sd['month'] - 1])} {sd['year']}" if sd.get("month") else str(sd["year"]))
                    if sd.get("year") else t("note.soon"))
        en = dict(hit[1])
        if not re.search(r"[A-Za-z]{2}", display_name(hit[0], en, lang)):
            en["name"] = m["title"].get("english") or m["title"].get("romaji")
        meta = row_meta(hit[0], "series", "tv", names, en, note=note, lang=lang)
        meta["_row"].update(when=when.date().isoformat() if isinstance(when, datetime.datetime) else when.isoformat() if when else None,
                            announced=when is None)
        metas.append(meta)
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
            return 1, sd, row_meta(det, "series", "tv", genres("tv", lang), en, note=t("note.season.from", season=s["season_number"], date=day_text(sd)), lang=lang)
        if today - datetime.timedelta(days=21) <= sd <= today and s["season_number"] > (e["item"].get("last_episode") or [0])[0]:
            return 0, sd, row_meta(det, "series", "tv", genres("tv", lang), en, note=t("note.season.since", season=s["season_number"], date=day_text(sd, True)), lang=lang)
    if nad and na.get("episode_number") == 1 and today < nad <= today + datetime.timedelta(days=180):
        return 1, nad, row_meta(det, "series", "tv", genres("tv", lang), en, note=t("note.season.from", season=na["season_number"], date=day_text(nad)), lang=lang)
    if nad and lad and (nad - lad).days > 60 and 0 <= (nad - today).days <= 14:
        return 1, nad, row_meta(det, "series", "tv", genres("tv", lang), en, note=t("note.back", date=day_text(nad, True)), lang=lang)
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
            out.append((1, d, row_meta(x, "movie", "movie", names, art.get(x["id"], {}), note=t("note.collection.cinemas", date=day_text(d), saga=saga), collection=cid, lang=lang)))
        elif today - datetime.timedelta(days=90) <= d <= today:
            out.append((0, d, row_meta(x, "movie", "movie", names, art.get(x["id"], {}), note=t("note.collection.released", saga=saga), collection=cid, lang=lang)))
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
                    label = f"{t(MONTHS[sd['month'] - 1])} {sd['year']}"
                elif sd.get("year"):
                    label = str(sd["year"])
                else:
                    label = None
                if m["format"] == "MOVIE":
                    hit = resolve_pick("movie", m["title"].get("english") or m["title"].get("romaji"), None, lang)
                    if hit and f"tmdb:{hit[0]['id']}" not in e.get("seen", ()):
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(hit[0], "movie", "movie", genres("movie", lang), hit[1],
                                            note=t("note.anime.film.out" if start and start <= today else "note.anime.film", date=label)
                                            if label else t("note.anime.announced"), lang=lang)))
                elif m["format"] in ("TV", "TV_SHORT", "ONA") and e["kind"] == "tv":
                    note = (t("note.season.announced") if not label else
                            t("note.anime.season.since" if start and start <= today else "note.anime.season.from", date=label))
                    raw = [t for t in (m["title"].get("english"), m["title"].get("romaji")) if t]
                    if base in {base_title(t) for t in raw}:  # "<their show> Season N": same show
                        det, en = details_pair("tv", e["id"], lang)
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(det, "series", "tv", genres("tv", lang), en, note=note, lang=lang)))
                        break
                    # A spin-off or remake ("Dragon Ball Super: Beerus", "NARUTO (2026)") is its own show:
                    # show that show, never pin the news onto the one they watched. No confident match, no card.
                    hit = next((h for h in (resolve_pick("tv", base_title(t), sd.get("year"), lang) for t in raw) if h), None)
                    if hit and str(hit[0]['id']) != str(e["id"]):
                        if f"tmdb:{hit[0]['id']}" not in e.get("seen", ()):
                            note = (t("note.anime.series.since" if start and start <= today else "note.anime.series.from", date=label)
                                    if label else t("note.series.announced")) + t("note.world", title=e["title"])
                        out.append((0 if start and start <= today else 1, when,
                                    row_meta(hit[0], "series", "tv", genres("tv", lang), hit[1], note=note, lang=lang)))
                break
    return out


def movie_collections(p, *, cache_only=False):
    """TMDB collection ids of the sagas they watch: from their movies plus taste.json's rewatch_collections."""
    movies = [e for e in p["entries"] if e["weight"] > 0 and e["stype"] == "movie"][:150]
    cols = {int(c) for c in (read_taste().get("household") or {}).get("rewatch_collections") or []}
    fetch = partial(tmdb, _cache_only=True) if cache_only else tmdb
    for job in [_pool.submit(fetch, f"movie/{e['id']}", language="en-US") for e in movies]:
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
            items.append((key, kind, (d, row_meta(it, stype, kind, names, art.get(it["id"], {}), lang=lang))))
    for meta in ((_feeds.get((rid('nextseason'), lang)) or {}).get("metas") or {}).get("series", []):
        if meta["id"] not in p["seen"] and meta["id"] not in have:
            items.append((meta["id"], "tv", (None, meta)))
    out = []
    for s_, why, key, kind, (d, meta) in rank_by_taste(m, items):
        pct = match_pct(m, s_)
        if pct is None or pct < (65 if m.get("cal_reference") == "known" else 80):
            continue
        original, original_overview = card_parts(meta)
        original = [x for x in original if x not in (t("match.strong"), t("match.good"), t("match.wildcard"))]
        what = (t("note.out", date=day_text(d)) if kind == "movie" else t("note.premieres", date=day_text(d))) if d else \
            (original[0] if original else t("note.soon"))
        note = " · ".join([what] + reasons(m, key, why, kind=kind))
        overview = (meta.get("description") or "") if d else original_overview
        out.append({**meta, "description": " · ".join(x for x in (note, overview) if x)})
    return out[:15]


def upcoming_feed(lang):
    """Coming Soon for You: what is coming next from their shows, sagas and anime, then new releases that fit their taste."""
    p = adult_profile(_profiles.get("household"))
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
        meta = {**meta, "_row": {**(meta.get("_row") or {}), "when": when.isoformat(),
                                         "announced": (meta.get("_row") or {}).get("announced", False)}}
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
        what = t("note.season.out", season=new[0]) if len(new) == 1 else t("note.seasons.out", first=new[0], last=new[-1])
        note = t("note.catchup.new", what=what, count=behind) if behind <= 1 else t("note.catchup.news", what=what, count=behind)
    else:
        note = t("note.catchup.remaining", count=behind, episode=episode_label(en, *nxt)) if behind <= 1 else t("note.catchup.remainings", count=behind, episode=episode_label(en, *nxt))
    meta = row_meta(det, "series", "tv", genres("tv", lang), en, note=note, lang=lang)
    meta["_row"]["season_out"] = finished_season and la["season_number"] > last[0]
    return (0 if fresh else 1, -(lad.toordinal() if fresh else e["weight"]), meta)


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
                            note=t("note.collection.skipped", part=i + 1, count=len(parts), saga=saga), collection=cid, lang=lang)))
        break  # the first one they missed; the next shows up once they watched it
    return out


def catchup_feed(lang):
    p = adult_profile(_profiles.get("household"))
    if not p:
        return {"tv": []}
    if len(adult_people()) != len(PEOPLE):
        p = {**p, "seen": {e["key"] for e in p["entries"]}}
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


def ready_feed(lang):
    from skald import ready
    return ready.build(SimpleNamespace(**globals()), lang)


# --- theme lanes ---------------------------------------------------------------------------
# Three rows built from what their taste is made of: a keyword ("Time Travel", "Based on Manga"), a person
# (director, creator or actor) and a network or studio. Which themes get a row is a small bandit: the
# facet's affinity, times how often its row's titles were actually played (lane:<facet> in learn.json).
# A lane shown for a week without a play fades, the current lanes are sticky for two weeks, and a lane
# that was gone for three weeks starts fresh.

LANE_KINDS = {"x": "feeling", "k": "theme", "d": "person", "a": "person", "n": "brand", "c": "brand"}
LANE_CAP = {"feeling": 2, "theme": 2, "person": 1, "brand": 1}
MAKER_NEED = 8
LANE_TEMPLATE = {"d": "row.lane.from", "a": "row.lane.starring", "n": "row.lane.brand", "c": "row.lane.from"}
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


def lane_allowed(metas, title, legacy=False):
    return legacy or any(row_names.mapping(x.get("_row")).get("anime_lane") is True for x in metas) or sum(
        "Animation" in row_facts(x)["g"] for x in metas) <= len(metas) / 2


class LaneRow(list):
    def __init__(self, metas, names):
        super().__init__(metas)
        self.names = names


THEME_MONTHS = ("any", [tuple(range(1, 13)), 12], None)
THEME_INP = {"kind": ("lane", "mood", "leap"), "theme": str, "source": [str, 40], "months": THEME_MONTHS,
             "date": str, "language": str, "taste": str,
             "candidates": [{"title": str, "year": (int, None), "type": ("movie", "series"),
                             "overview": (str, None)}, 40]}
THEME_OUT = {"anime_lane": (bool, None), "fits_now": bool, "months": THEME_MONTHS, "keep": [int, 40],
             "add": [{"title": str, "year": (int, None), "type": ("movie", "series"), "why": str}, 30],
             "why": {str(i): (str, None) for i in range(40)}}
THEME_SYS = ("Source one themed streaming row. Judge the theme's meaning, not keyword overlap. "
             "When months is unknown, judge once which calendar months (1 to 12) the theme suits, or any for any time. "
             "Return months as a nonempty unique list or \"any\". Reuse supplied months unchanged. "
             "For leap, judge the emotional experience, not the seed title's setting or release date. "
             "fits_now is false only when this theme is wrong for today's date, season or holiday. "
             "anime_lane is true only if the theme explicitly calls for anime or manga adaptations. "
             "Keep only candidates that really fit. Propose up to 30 other fitting movies or series. "
             "For leap, preserve the seed's emotional experience in a different world. "
             "Use household taste to guide suggestions. Order keep and add by fit; keep comes before add. "
             "Return only JSON: {\"fits_now\":true,\"months\":\"any\",\"keep\":[0],\"add\":[{\"title\":\"...\","
             "\"year\":2020,\"type\":\"movie\",\"why\":\"...\"}],\"why\":{\"0\":\"...\"}}. "
             "Indices are zero-based in candidates. Every kept and added title needs a short real reason "
             "in language, one sentence under 90 characters explaining how the story fits. "
             "Never use a bare keyword, tag, facet or theme name as a reason. No markup or dashes.")


def theme_request(batch):
    return {"model": COMMAND_MODEL, "messages": [{"role": "system", "content": THEME_SYS},
            {"role": "user", "content": json.dumps(batch[0], ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "reasoning": {"effort": COMMAND_EFFORT},
            "max_tokens": 2000, "usage": {"include": True}}


def theme_parse(resp, batch):
    answer = json.loads(resp["choices"][0]["message"]["content"])
    try:
        check_theme_months(answer.get("months"))
    except ValueError:
        answer["months"] = None
    check(THEME_OUT, answer)
    inp = batch[0]
    if len(set(answer["keep"])) != len(answer["keep"]) or any(
            not 0 <= i < len(inp["candidates"]) for i in answer["keep"]):
        raise ValueError("keep")
    labels = {x.casefold().strip(" .,!?:;") for x in [inp["theme"], *inp["source"]]}
    for reason in [answer["why"].get(str(i)) for i in answer["keep"]] + [x["why"] for x in answer["add"]]:
        note = clean_note(reason) if reason else ""
        if len(note.split()) < 3 or note.casefold().strip(" .,!?:;") in labels:
            raise ValueError("why")
    return [(answer, 1)]


decision("theme_source", ver=2, lane="feed", cap=12, ttl=DAY, serving=False, requires=("OR_KEY",), api=False,
    inp=THEME_INP, out=THEME_OUT, norm=lambda x: x, rule=None, params={}, min_acc=1,
    llm={"path": "/api/v1/chat/completions", "model": COMMAND_MODEL, "batch": 1, "timeout": 15,
         "prompt": theme_request, "parse": theme_parse},
    fallback=lambda inp, err: ({"fits_now": True, "months": None, "keep": [], "add": [], "why": {}}, 0))


def check_theme_months(months):
    check(THEME_MONTHS, months)
    if months is None or isinstance(months, list) and (not months or len(set(months)) != len(months)):
        raise ValueError("months")


def season_request(batch):
    return {"model": COMMAND_MODEL, "messages": [{"role": "system", "content":
            "Judge a streaming theme's calendar season by meaning, not keyword overlap. "
            "Return only JSON: {\"months\":\"any\"} for any time, or a nonempty unique list of month numbers 1 to 12. "
            "For leap, judge the emotional experience, not the seed title's setting or release date."},
            {"role": "user", "content": json.dumps(batch[0], ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "reasoning": {"effort": COMMAND_EFFORT},
            "max_tokens": 100, "usage": {"include": True}}


def season_parse(resp, batch):
    answer = json.loads(resp["choices"][0]["message"]["content"])
    check({"months": THEME_MONTHS}, answer)
    check_theme_months(answer["months"])
    return [(answer, 1)]


decision("theme_season", ver=1, lane="feed", cap=12, ttl=365 * DAY, serving=False, requires=("OR_KEY",), api=False,
    inp={"kind": ("lane", "mood", "leap"), "name": str, "source": [str, 40]}, out={"months": THEME_MONTHS},
    norm=lambda x: x, rule=None, params={}, min_acc=1,
    llm={"path": "/api/v1/chat/completions", "model": COMMAND_MODEL, "batch": 1, "timeout": 15,
         "prompt": season_request, "parse": season_parse}, fallback=lambda inp, err: ({"months": None}, 0))


_theme_seasons = threading.local()


def theme_months(theme, months=None, allow_llm=False):
    inp = {"kind": theme["kind"], "name": theme["name"][:500], "source": theme["source"][:40]}
    key = hashlib.sha256(json.dumps(inp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    with _learn_lock:
        stored = _learn.get("seasons", {}).get(key)
    if stored is not None:
        return stored
    if months is None and allow_llm:
        pending = getattr(_theme_seasons, "pending", None)
        if pending is not None:
            pending[key] = theme
            return None
        result = decide("theme_season", inp)
        months = result["answer"]["months"]
    if months is not None:
        check_theme_months(months)
        with _learn_lock:
            _learn.setdefault("seasons", {})[key] = months
            _learn_dirty.set()
        save_learn()
        clear_seed_cache()
    return months


def season_fits(months):
    return months is None or months == "any" or local_now().month in months


_theme_sources = {}  # Daily source reuse; the persisted decision cap bounds restart spend.
_row_source_status = {}


def themed_row(m, p, theme, items, lang, cid, person=None, seed=None, exclude=(), allow_llm=True, feeling_report=None):
    """Own picks with bounded vetting; None means the caller must use its failure policy."""
    today = local_now().date().isoformat()
    months = theme_months(theme)
    if not season_fits(months):
        return []
    for key in list(_theme_sources):
        if key[0] != today:
            _theme_sources.pop(key, None)
    cache_key = (today, lang, person, theme["kind"], theme["key"], theme["name"], tuple(theme["source"]))
    counts = rowplan.candidate_sources(items, (feeling_report or {}).get('missing_cards', 0))
    status = partial(rowplan.source_status, SimpleNamespace(**globals()), cid, lang,
                     candidate_sources=counts if feeling_report is not None or any(counts.values()) else None)
    try:
        metas = _theme_sources.get(cache_key)
        vetted = True
        items = list({x["id"]: x for x in items}.values())[:40]
        status(date=today, reused=metas is not None)
        if metas is None and not allow_llm:
            if not season_fits(theme_months(theme, allow_llm=bool(items))):
                return []
            metas, vetted = items, False
            status(date=today, budget=True)
        if metas is None:
            if not items:
                return []
            candidates = []
            for x in items:
                year = str(x.get("releaseInfo") or (x.get("_row") or {}).get("date") or "")[:4]
                overview = (x.get("_row") or {}).get("overview") or card_parts(x)[1]
                candidates.append({"title": x["name"][:500], "year": int(year) if year.isdigit() else None,
                                   "type": x["type"], "overview": overview[:350] or None})
            result = decide("theme_source", {"kind": theme["kind"], "theme": theme["name"][:500],
                            "source": theme["source"][:40], "date": today,
                            "months": months,
                            "language": row_lang(), "taste": taste_hint(m, person)[:500] or "none available",
                            "candidates": candidates})
            if result["source"] == "fallback":
                budget = result.get('why') in ('BudgetExceeded', 'lane', 'month', 'cap', 'over')
                status(date=today, failed=not budget, budget=budget)
                if not season_fits(theme_months(theme, allow_llm=True)):
                    return []
                if result.get("why") != "cap" or theme["kind"] != "lane":
                    return None
                metas, vetted = items, False
            else:
                status(date=today, reused=result['source'] == 'cache')
                answer = result["answer"]
                months = theme_months(theme, answer["months"])
                if not season_fits(months) or not answer["fits_now"]:
                    _theme_sources[cache_key] = []
                    return []
                metas = [{**items[i], "description": " · ".join(x for x in (
                    clean_note(answer["why"][str(i)]), card_parts(items[i])[1]) if x)} for i in answer["keep"]]
                jobs = [(x, _outer.submit(resolve_pick, "movie" if x["type"] == "movie" else "tv",
                                         x["title"], x["year"], lang)) for x in answer["add"]]
                for pick, job in jobs:
                    try:
                        hit = job.result()
                        if hit:
                            kind = "movie" if pick["type"] == "movie" else "tv"
                            metas.append(row_meta(hit[0], pick["type"], kind, genres(kind, lang), hit[1],
                                                  note=clean_note(pick["why"]), lang=lang))
                    except Exception:
                        continue
                metas = [{**x, "_row": {**(x.get("_row") or {}), "anime_lane": bool(answer.get("anime_lane"))}} for x in metas]
        unique = {}
        for x in metas:
            unique.setdefault(x["id"], x)
        metas = list(unique.values())
        ranked = rank_by_taste(m, [(x["id"], "movie" if x["type"] == "movie" else "tv", x) for x in metas])
        raw = {key: person_score(m, key, person, kind)[0] if person else sc for sc, _, key, kind, _ in ranked}
        scores = {key: match_pct(m, sc, person) for key, sc in raw.items()}
        source = metas
        metas = [x for x in metas if x["id"] not in p["seen"] | p["negative"] | set(exclude)
                 and not NO_TALK & set((x.get("_row") or {}).get("genre_ids") or [])
                 and quality_ok(x) and tone_ok(x, seed, animation=theme["kind"] == "leap", person=person)
                 and (theme["kind"] != "lane" or scores[x["id"]] is None or scores[x["id"]] >= 55)
                 and row_eligible(x, cid, lang, p, seed=seed)]
        if not vetted:
            return sorted(metas, key=lambda x: (raw[x["id"]] is None, -(raw[x["id"]] or 0)))
        values = {x["id"]: raw[x["id"]] for x in metas if raw[x["id"]] is not None}
        low, high = min(values.values(), default=0), max(values.values(), default=0)
        blended = [(0.2 * (1 - i / len(metas)) + 0.8 * ((values.get(x["id"], low) - low) / (high - low) if high > low else 0),
                    {**x, "_row": {**(x.get("_row") or {}), "theme": theme["key"]}}) for i, x in enumerate(metas)]
        _theme_sources[cache_key] = source
        return [x for _, x in sorted(blended, key=lambda r: -r[0])]
    except Exception as e:
        status(date=today, failed=True)
        print(f"theme {theme['kind']}: {type(e).__name__}: {e}", flush=True)
        return None


def lane_titles(fid, lang, makers_only=False, feeling_report=None):
    """[(key, kind, meta)] of a theme from TMDB discover or the person's credits, or of a feeling (experience)."""
    t, xid = fid.split(":", 1)
    if t == "x":
        return feeling_pool(get_model(False), _profiles.get("household"), [xid], lang, seeds=10,
                            cid=LANES[0], report=feeling_report)
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
                out.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}), lang=lang)))
    if t in "da":
        jobs = {l: _pool.submit(tmdb, f"person/{xid}/combined_credits", language=l) for l in {lang, "en-US"}}
        data, en = jobs[lang].result(), jobs["en-US"].result()
        art = {(x.get("media_type"), x["id"]): x for x in (en.get("cast") or []) + (en.get("crew") or [])}
        if t == "a":
            xs = [x for x in data.get("cast") or [] if (x.get("order") if x.get("order") is not None else 99) <= 8
                  and (x.get("media_type") == "movie" or (x.get("episode_count") or 0) >= 4)]
        else:
            xs = [x for x in data.get("crew") or [] if x.get("job") in ("Director", "Creator", "Screenplay", "Writer")]
            if makers_only:
                xs = [x for x in xs if (x.get("media_type"), x.get("job")) in (("movie", "Director"), ("tv", "Creator"))]
        xs.sort(key=lambda x: -(x.get("vote_count") or 0))
        for x in xs:
            kind = "movie" if x.get("media_type") == "movie" else "tv"
            key = f"tmdb:{x['id']}"
            if key in seen or NO_TALK & set(x.get("genre_ids") or []) or (x.get("vote_count") or 0) < (100 if kind == "movie" else 30):
                continue
            seen.add(key)
            out.append((key, kind, row_meta(x, "movie" if kind == "movie" else "series", kind, genres(kind, lang),
                                           art.get((x.get("media_type"), x["id"]), {}), lang=lang)))
    return [x for x in out if quality_ok(x[2]) and tone_ok(x[2])]


def lane_title(fid, name, examples):
    if fid[0] == "x":
        return t(EXPERIENCES[fid[2:]][1])
    if fid[0] != "k":
        return t(LANE_TEMPLATE[fid[0]], name=name) if fid[0] in LANE_TEMPLATE else name
    return " ".join(w if w in ("on", "of", "a", "the", "and", "in") else w[:1].upper() + w[1:] for w in name.split())


def lane_names_parse(reply, batch):
    inp = batch[0]
    answer = search_commands.parse_json(reply['choices'][0]['message']['content'])
    names = answer.get('names') if isinstance(answer, dict) else None
    if not isinstance(names, list) or len(names) != len(inp['lanes']):
        raise ValueError('names')
    for name in names:
        if not isinstance(name, str) or not 1 <= len(name.split()) <= 4:
            raise ValueError('name')
    return [({'names': names}, 1)]


def lane_names_request(batch):
    return {'model': COMMAND_MODEL, 'messages': [
        {'role': 'system', 'content': 'Name streaming service rows in the requested household language and script. '
         'Input lanes have a keyword and example card titles. Give each a natural, inviting row name of 1 to 4 words. '
         'No "&" double titles or "for You" suffixes. No quotes inside names, markup or links. '
         'Return JSON only: {"names":["..."]}, in input order.'},
        {'role': 'user', 'content': json.dumps(batch[0], ensure_ascii=False)}],
        'response_format': {'type': 'json_object'}, 'reasoning': {'effort': COMMAND_EFFORT},
        'max_tokens': 600, 'usage': {'include': True}}


decision('name_lanes', ver=1, lane='feed', cap=None, ttl=DAY, serving=False, requires=('OR_KEY',), api=False,
    inp={'language': str, 'lanes': [{'keyword': str, 'examples': [str, 4]}, 7]}, out={'names': [str, 7]},
    norm=lambda x: x, rule=None, params={}, min_acc=.95,
    llm={'path': '/api/v1/chat/completions', 'model': COMMAND_MODEL, 'batch': 1, 'timeout': 10,
         'prompt': lane_names_request, 'parse': lane_names_parse},
    fallback=lambda inp, err: ({'names': []}, 0))


def name_lanes(chosen, model):
    lanes, language = row_names.mapping(_learn.get('lanes')), row_lang().lower()
    _learn['lanes'] = lanes
    titles = {fid: title for fid, st in lanes.items() if (title := row_names.ai_title(st, row_lang()))}
    pending = [(fid, metas) for fid, metas, *_ in chosen if fid.startswith('k:') and fid not in titles]
    if pending:
        result = decide('name_lanes', {'language': row_lang(), 'lanes': [
            {'keyword': model['names'][fid][:500], 'examples': [x['name'][:500] for x in metas[:4]]}
            for fid, metas in pending]})
        if result['source'] != 'fallback':
            for (fid, _), title in zip(pending, result['answer']['names']):
                st = lanes[fid] = row_names.mapping(lanes.get(fid))
                st['titles'] = row_names.mapping(st.get('titles'))
                st['titles'][language] = title
                titles[fid] = title
            _learn_dirty.set()
    return {fid: titles[fid] for fid, *_ in chosen if fid in titles}


def maker_seeds(p, person):
    """Makers with two distinct positive titles, strongest count then newest evidence."""
    makers = {}
    for e in person_entries(p, person):
        if e["weight"] <= 0 or e["signal"] not in ("loved", "liked", "watched"):
            continue
        for mid, name in frec(e["key"], e["kind"]).get("d") or []:
            fid = f"d:{mid}"
            titles, _ = makers.setdefault(fid, ({}, name))
            titles[e["key"], e["kind"]] = e["age"]
    return [(fid, name) for fid, (titles, name) in sorted(makers.items(),
            key=lambda pair: (-len(pair[1][0]), min(pair[1][0].values()), pair[0])) if len(titles) >= 2]


def maker_view(names, p, who):
    names = row_names.mapping(names)
    # Legacy director pins predate the maker flag and keep their owner restriction.
    if names.get("maker") is False or not row_names.text(names.get("lane")).startswith("d:"):
        return True
    owners = {person for person in PEOPLE if names["lane"] in dict(maker_seeds(p, person))}
    if who in (None, "none"):
        return bool(owners)
    return set(PEOPLE) <= owners if who == "both" else who in owners


def lanes_feed(lang):
    p = _profiles.get("household")
    m = get_model()
    if not p or not m:
        return None
    today = local_now().date()
    lanes = row_names.mapping(_learn.get("lanes"))
    _learn["lanes"] = lanes
    with _learn_lock:
        stats = {k: dict(v) for k, v in learning_rows().items() if k.startswith("lane:")}
    cur = {((_feeds.get((cid, lang)) or {}).get("names") or {}).get("lane"): cid for cid in LANES}
    seasonal = {cid: rec["names"] for cid in LANE_ROWS if (rec := _feeds.get((cid, lang)))
                and not season_fits((rec.get("names") or {}).get("months"))}

    def priority(fid, aff):
        st = row_names.mapping(lanes.get(fid))
        last = date_or_none(st.get("last"))
        if last and (today - last).days > 21:  # gone for three weeks: a fresh start
            st, _ = {"titles": st.get("titles", {})}, stats.pop(f"lane:{fid}", None)
            with _learn_lock:
                _learn["rows"].pop(f"lane:{fid}", None)
            lanes[fid] = st
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
    makers, maker_themes = {}, []
    for person in PEOPLE:
        for fid, name in maker_seeds(p, person)[:3]:
            makers[fid] = name
            maker_themes.append((0, fid, person))
    themes = [(pr, fid, None) for pr, fid in themes]
    if makers:
        themes = maker_themes + [(pr, fid, person) for pr, fid, person in themes if not fid.startswith("d:")]
    exclude = p["seen"] | p["negative"]
    front = set()
    moods = 0
    cap = rowplan.family_caps(CONFIG)['mood'] if rowplan.capped(CONFIG) else len(MOODS_ROWS)
    for cid in DEDUPE_ORDER[:DEDUPE_ORDER.index(LANES[0])]:
        if not row_enabled(cid) or cid in MOODS_ROWS and moods >= cap:
            continue
        for typ in (("movie", "series") if cid in (rid('top10'), rid('toprated')) else ("movie",) if cid == rid('digital') else ("tv",) if cid in MIXED else ("series",)):
            metas = row_list(cid, typ, lang, built=True)
            moods += bool(metas) and cid in MOODS_ROWS
            front = mark_above(front, cid, metas)
    chosen, used, tries, vetted, taken, hero_lane = [], {}, 0, 0, set(), False
    maker_members, hidden = set(), remote_masks()[0] if makers else set()
    local = list(row_pool(lang))
    for pr, fid, person in themes:
        if len(chosen) == len(LANE_ROWS) or tries >= 40:
            break
        kind = LANE_KINDS[fid[0]]
        if person in maker_members:
            continue
        if person is not None and any(chosen_fid == fid for chosen_fid, *_ in chosen):
            maker_members.add(person)
            continue
        if fid not in makers and len(chosen) < len(LANES) and used.get(kind, 0) >= LANE_CAP[kind]:
            continue
        tries += 1
        feeling_report = {} if fid[0] == 'x' else None
        try:
            items = [x for x in (lane_titles(fid, lang, feeling_report=feeling_report) if feeling_report is not None else
                                lane_titles(fid, lang, makers_only=True) if fid in makers else lane_titles(fid, lang))
                     if x[0] not in exclude and (fid not in makers or x[0] not in hidden)]
        except Exception as e:
            print(f"lane {fid}: {type(e).__name__}: {e}", flush=True)
            items = []
        cached = [(x["id"], "movie" if x["type"] == "movie" else "tv", x) for x in local
                  if fid not in makers and x["id"] not in exclude and lane_member(x, fid)]
        ids = {x[0] for x in items}
        items += [x for x in cached if x[0] not in ids]
        tired = tired_keys({f"lane:{fid}"})
        items = [x for x in items if x[0] not in tired]
        need = MAKER_NEED if fid in makers else 16  # Snapshot lanes lost 4-6 cards after build; keep ten beyond that loss.
        title = t("row.maker", name=makers[fid]) if fid in makers else lane_title(fid, m["names"][fid], [])
        if fid.startswith("k:"):
            st = row_names.mapping(lanes.get(fid))
            title = row_names.ai_title(st, row_lang()) or row_names.text(st.get("title")) or title
        theme = {"kind": "lane", "name": m["names"][fid] if fid.startswith("k:") else title,
                 "key": fid, "source": [fid, makers.get(fid) or m["names"][fid]]}
        source_cid = LANE_ROWS[len(chosen)]
        previous_names = [(_feeds.get((cid, lang)) or {}).get("names") or {} for cid in LANE_ROWS]
        months = next((n.get("months") for n in previous_names if n.get("lane") == fid and n.get("months") is not None), None)
        if not season_fits(months) or not season_fits(theme_months(theme, months)):
            seasonal.setdefault(cur.get(fid) or source_cid, {"title": title, "lane": fid, "months": months or theme_months(theme),
                                                           "maker": fid in makers, "name": makers.get(fid) or m["names"][fid]})
            continue
        candidates = [x for _, _, x in items if x["id"] not in taken | front]
        metas = themed_row(m, p, theme,
                           candidates, lang, source_cid, person=person, allow_llm=vetted < len(LANE_ROWS) + 1,
                           feeling_report=feeling_report,
                           exclude=taken | front | set(tired) | (hidden if fid in makers else set()))
        status = _row_source_status.get((source_cid, lang))
        vetted += bool(candidates)
        if not season_fits(theme_months(theme)):
            seasonal.setdefault(cur.get(fid) or source_cid, {"title": title, "lane": fid, "months": theme_months(theme),
                                                           "maker": fid in makers, "name": makers.get(fid) or m["names"][fid]})
            continue
        if metas is None:
            previous = [x for cid in LANE_ROWS for x in source_metas(cid, lang, built=True)
                        if ((_feeds.get((cid, lang)) or {}).get("names") or {}).get("lane") == fid
                        and (x.get("_row") or {}).get("theme") == fid]
            metas = list({x["id"]: x for x in previous if x["id"] not in exclude | set(tired) | hidden}.values())
        metas = [x for x in metas if row_eligible(x, LANES[0], lang, p)]
        reserve = list(metas)
        superhero = sum(is_hero(x) for x in metas) > len(metas) / 2
        if len(chosen) < len(LANES) and superhero and hero_lane:
            continue
        fresh = list({x["id"]: x for x in metas if x["id"] not in taken | front}.values())[:ROW_MAX]
        if len(fresh) < need or not lane_allowed(fresh, title):
            print(f"lane {fid}: {len(fresh)} fresh of {len(metas)}", flush=True)
            continue
        hero_lane |= superhero
        used[kind] = used.get(kind, 0) + 1
        if person is not None:
            maker_members.add(person)
        taken |= {x["id"] for x in fresh}
        chosen.append((fid, fresh, reserve, title, status))
    named = name_lanes(chosen, m)
    chosen = [(fid, metas, reserve, named.get(fid, title), status) for fid, metas, reserve, title, status in chosen]
    slots, free = {}, [c for c in LANES]
    for fid, metas, reserve, title, status in chosen[:len(LANES)]:  # a lane keeps its row position while it stays
        if cur.get(fid) in free:
            slots[cur[fid]] = (fid, metas, reserve, title, status)
            free.remove(cur[fid])
    for fid, metas, reserve, title, status in chosen[:len(LANES)]:
        if fid not in [f for f, *_ in slots.values()]:
            slots[free.pop(0)] = (fid, metas, reserve, title, status)
    slots.update(zip(LANE_BENCH, chosen[len(LANES):]))
    out, names = {}, {}
    for cid in LANE_ROWS:
        _row_source_status.pop((cid, lang), None)
        if cid not in slots:
            out[cid] = {"tv": []}
            if cid in seasonal:
                names[cid] = seasonal[cid]
            continue
        fid, metas, reserve, title, status = slots[cid]
        if status:
            _row_source_status[cid, lang] = status
        st = row_names.mapping(lanes.get(fid))
        if not row_names.text(st.get("title")):
            st["title"] = lane_title(fid, m["names"][fid], []) if fid.startswith("k:") else title
            st["since"] = today.isoformat()
        st["last"] = today.isoformat()
        lanes[fid] = st
        _learn_dirty.set()
        out[cid] = {"tv": metas, "pool": reserve}
        names[cid] = {"title": title, "lane": fid, "maker": fid in makers,
                      "name": makers.get(fid) or m["names"][fid], "months": theme_months(
            {"kind": "lane", "name": m["names"][fid] if fid.startswith("k:") else title,
             "source": [fid, makers.get(fid) or m["names"][fid]]})}
    print("lanes: " + ", ".join(f"{n['title']} ({n['lane']})" for n in names.values()), flush=True)
    return {**out, "_names": names}


# --- feeling rows ------------------------------------------------------------------------------
# Same Feeling, New World: one loved title a day (rotating members / together), and titles that give the same
# experience from a different world. For each member: their loves and the shared TV. Four mood shelves: experience bundles,
# chosen once per evening by Luna (what fits tonight: day, season, recent plays, what they respond to), blended
# with taste; without Luna, a taste ranking with a weekday lean (Friday epic, Sunday cozy) rotates daily.

MOODS = {
    "cozy": ("row.mood.cozy", ["cozy_case", "banter_duo", "will_they", "feel_good", "nostalgia"], "0123456"),
    "edge": ("row.mood.edge", ["whodunit", "dread", "mind_bender", "survival", "heist"], "01234"),
    "epic": ("row.mood.epic", ["epic_quest", "magic_wonder", "dynasty_power", "war_sacrifice", "team_spectacle"], "4 5"),
    "heart": ("row.mood.heart", ["tearjerker", "grief_healing", "second_chance", "found_family", "heartwarming"], "6"),
    "romance": ("row.mood.romance", ["slow_romance", "period_society", "will_they", "forbidden_love"], "45"),
    "laugh": ("row.mood.laugh", ["feel_good", "dark_comedy", "slice_of_life", "banter_duo"], "0123"),
    "grit": ("row.mood.grit", ["frontier_grit", "antihero", "revenge", "underdog", "sports_glory"], "56"),
    "ideas": ("row.mood.ideas", ["big_ideas", "time_twist", "dystopia", "mind_bender"], "3456"),
    "power": ("row.mood.power", ["political_intrigue", "dynasty_power", "antihero", "family_secrets"], "0123456"),
    "true": ("row.mood.true", ["true_story", "underdog", "passion_art", "sports_glory", "competence"], "0123456"),
    "home": ("row.mood.home", ["coming_of_age", "found_family", "mentor_bond", "slice_of_life", "heartwarming"], "56"),
    "dark": ("row.mood.dark", ["antihero", "dark_comedy", "revenge", "dread", "family_secrets"], "45"),
}
MOOD_LEVELS = ["wrong for tonight", "could work", "good fit", "perfect for tonight"]


def xsim(a, b):
    """Cosine of two experience-tag dicts."""
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / ((sum(v * v for v in a.values()) * sum(v * v for v in b.values())) ** 0.5 or 1.0)


def feeling_pool(m, p, xs, lang, seeds=6, extra=(), seed=None, *, cid=None, person=None, ranks=None, report=None):
    """Unseen titles carrying xs, alternating ranked stored catalog and loved-title recommendations."""
    if not m or not p:
        return []
    kinds = {fkey(e["key"], e["kind"]): e["kind"] for e in p["entries"]}
    src = []
    for x in xs:
        src += [k for _, k in m["holders"].get(f"x:{x}", [])[:seeds]]
    src = [k for k in dict.fromkeys(list(extra) + src) if k in kinds][:seeds * 2]
    jobs = [(kinds[k], _outer.submit(with_art, f"{kinds[k]}/{k[5:].split('@')[0]}/recommendations", lang)) for k in src]
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
            items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}), lang=lang)))
    try:
        ensure_facets([(k, kind) for k, kind, _ in items])
    except Exception as e:
        print(f"feeling pool: {type(e).__name__}: {e}", flush=True)
    tag_experiences([(k, kind) for k, kind, _ in items], max_batches=3)
    items = [x for x in items if quality_ok(x[2]) and tone_ok(x[2], seed)
             and any(frec(x[0], x[1]).get("x", {}).get(y, 0) >= 2 for y in xs)]
    ranks = ranks if ranks is not None else getattr(_upstream, 'catalog_ranks', None)
    if ranks is None:
        ranks = {}
    if person not in ranks:
        ranks[person] = known_rank(m, p, person)
    with _feeds_lock:
        cached = {(x['id'], 'movie' if x['type'] == 'movie' else 'tv'): x
                  for store in (_feeds, _shown) for row in store.values()
                  for cards in (row.get('metas') or {}).values() if isinstance(cards, list) for x in cards if isinstance(x, dict)}
    exclusions = catalog_exclusions(cid or rid('foryou'), person)
    catalog, scores, missing = [], {}, 0
    for sc, _, key, kind in ranks[person]:
        tags = frec(key, kind).get('x') or {}
        if key in exclude or not any(tags.get(y, 0) >= 2 for y in xs):
            continue
        meta = known_meta(key, kind, lang, cached.get((key, kind)))
        if not meta or not meta.get('poster'):
            missing += 1
            continue
        if NO_TALK & set((meta.get('_row') or {}).get('genre_ids') or []) or not tone_ok(meta, seed, person=person) \
                or catalog_drop(meta, p, cid or rid('foryou'), person, exclusions=exclusions):
            continue
        catalog.append((key, kind, meta))
        scores[key, kind] = sc
    ids = {key for key, _, _ in catalog}
    items = [x for x in items if x[0] not in ids]
    for key, kind, _ in items:
        scores[key, kind] = person_score(m, key, person, kind)[0] if person else score_key(m, key, kind=kind)[0]
    def ranked(source, name):
        for key, kind, meta in source:
            meta.setdefault('_row', {})['feeling_source'] = name
            if name == 'catalog':
                meta['_row']['catalog'] = True
        return sorted(source, key=lambda x: -(0.15 * sum(frec(x[0], x[1]).get('x', {}).get(y, 0) for y in xs)
                      + (match_pct(m, scores[x[0], x[1]], person) or 50) / 100))
    if report is not None:
        report['missing_cards'] = missing
    return interleave(ranked(catalog, 'catalog'), ranked(items, 'recommendations'), key=lambda x: x[0])


def day_index():
    return (local_now() - datetime.timedelta(hours=5)).date().toordinal()


def leap_feed(lang):
    p, m = adult_profile(_profiles.get("household")), get_model()
    if not p or not m:
        return None
    who = (*adult_people(), "together")[day_index() % (len(adult_people()) + 1)]
    cands = [e for e in p["entries"] if e["signal"] == "loved" and frec(e["key"], e["kind"]).get("x")
             and (who in e.get("by", ()))]
    cands = sorted(cands, key=lambda e: -e["weight"])[:7] or \
        sorted((e for e in p["entries"] if e["weight"] > 0 and frec(e["key"], e["kind"]).get("x")), key=lambda e: -e["weight"])[:7]
    if not cands:
        return None
    a = cands[(day_index() // 3) % len(cands)]
    ax = frec(a["key"], a["kind"])["x"]
    top = [x for x, _ in sorted(ax.items(), key=lambda kv: -kv[1])][:3]
    feeling_report = {}
    pool = feeling_pool(m, p, top, lang, seeds=8, extra=[fkey(a["key"], a["kind"])], seed=a["key"],
                        cid=rid('leap'), report=feeling_report)
    pool = [x for x in pool if tone_ok(x[2], a["key"], animation=True)]
    theme = {"kind": "leap", "name": t("row.feeling", title=a["title"]), "key": a["key"],
             "source": [a["title"], *(f"{x}: {v}" for x, v in ax.items())]}
    reserve = themed_row(m, p, theme,
                         [x for _, _, x in pool], lang, rid('leap'), person=who if who in PEOPLE else None, seed=a["key"],
                         feeling_report=feeling_report)
    if reserve is not None:
        metas = polish_row(balance_row(reserve), catalog=True)[:30]
        if len(metas) < 8:
            return {rid('leap'): {"tv": []}}
        return {rid('leap'): {"tv": metas, "pool": reserve},
                "_names": {rid('leap'): {"title": t("row.feeling", title=a["title"]), "seed": a["key"], "months": theme_months(theme),
                                       "label_key": "row.feeling", "fields": {"title": a["title"]}}}}
    av = femb(a["key"], a["kind"])
    try:
        ensure_emb([(k, kind) for k, kind, meta in pool if (meta.get('_row') or {}).get('feeling_source') != 'catalog'])
    except BudgetExceeded:
        pass
    except Exception as e:
        embedding_failure(e)
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
        note = (t("why.feeling", feeling=t("experience." + shared[0])[:1].upper() + t("experience." + shared[0])[1:], title=a["title"]) if shared
                else t("note.feels", title=a["title"]))
        scored.append((same * (0.4 + world) + 0.004 * pct, {**meta, "description": " · ".join(
            x for x in (note + who_label(a.get("by") or ()), meta.get("description")) if x)}))
    reserve = [x for _, x in sorted(scored, key=lambda t: -t[0])]
    metas = polish_row(balance_row(reserve), catalog=True)[:30]
    print(f"leap: {who} -> {a['title']} {top}, pool {len(pool)}, kept {len(metas)}", flush=True)
    if len(metas) < 8:
        return None
    return {rid('leap'): {"tv": metas, "pool": reserve}, "_names": {rid('leap'): {"title": t("row.feeling", title=a["title"]), "seed": a["key"], "months": theme_months(theme),
                                       "label_key": "row.feeling", "fields": {"title": a["title"]}}}}


def newtoyou_plan(m, who):
    """[(keep, change, origin)]: one experience they clearly love kept, one axis moved toward what a neighbour loves
    ("neighbour"), toward the rest of a mood family they already enjoy ("group"), or one family over ("blind").
    Nothing they avoid, nothing they already love. Deterministic for one model."""
    if not m:
        return []
    aff = ((m.get("paff") or {}).get(who) or (m.get("aff") if who == "together" else None)) or {}
    mine = {f[2:]: a for f, a in aff.items() if f[:2] == "x:" and f[2:] in EXPERIENCES}
    strong = [x for x, a in sorted(mine.items(), key=lambda r: (-r[1], r[0])) if a > 0][:3]
    if not strong:
        return []
    fresh = lambda x: x in EXPERIENCES and x not in strong and 0 <= mine.get(x, 0) < .25
    others = [p for p in (*adult_people(), "together") if p != who]
    near = []
    for p in others:
        theirs = (m.get("paff") or {}).get(p) or {}
        near += [f[2:] for f, a in sorted(theirs.items(), key=lambda r: (-r[1], r[0]))
                 if f[:2] == "x:" and a > 0 and fresh(f[2:])][:3]
    plan = []
    for keep in strong:
        families = [k for k, (_, xs, _) in MOODS.items() if keep in xs]
        group = [x for k in families for x in MOODS[k][1] if fresh(x)]
        blind = [x for k, (_, xs, _) in MOODS.items() if k not in families
                 and set(xs) & {y for f in families for y in MOODS[f][1]} for x in xs if fresh(x) and not mine.get(x)]
        for origin, xs in (("neighbour", near), ("group", group), ("blind", blind)):
            plan += [(keep, x, origin) for x in dict.fromkeys(xs) if x != keep]
    out, pairs = [], set()
    for keep, change, origin in plan:
        if (keep, change) not in pairs:
            pairs.add((keep, change))
            out.append((keep, change, origin))
    return out[:12]


def newtoyou_feed(lang):
    """New to You (opt-in): titles that keep one experience someone loves and change one axis. A plain no from this
    row counts lightly (NEWTOYOU_REJECT), so trying something new is cheap."""
    p, m = adult_profile(_profiles.get("household")), get_model()
    if not p or not m:
        return None
    people = adult_people()
    who = (*people, "together")[day_index() % (len(people) + 1)]
    plan = newtoyou_plan(m, who)
    if not plan:
        return None
    person = who if who in PEOPLE else None
    aff = (m.get("paff") or {}).get(who) or m["aff"]
    avoided = {f[2:] for f, a in aff.items() if f[:2] == "x:" and a < 0}
    cid = rid('newtoyou')
    exclude = p["seen"] | p["negative"] | deferred_keys(person) | set(tired_keys({cid}))
    keeps = list(dict.fromkeys(k for k, _, _ in plan))[:2]
    scored, taken, missing = [], set(), 0
    ranks = getattr(_upstream, 'catalog_ranks', None)
    ranks = ranks if ranks is not None else {}
    for keep in keeps:
        feeling_report = {}
        for key, kind, meta in feeling_pool(m, p, [keep], lang, seeds=6, cid=cid, person=person, ranks=ranks, report=feeling_report):
            if key in taken or key in exclude or not quality_ok(meta) or not tone_ok(meta, person=person):
                continue
            tags = frec(key, kind).get("x") or {}
            if any(tags.get(x, 0) >= 2 for x in avoided):
                continue
            pair = next(((k, c) for k, c, _ in plan if k == keep and tags.get(k, 0) >= 2 and tags.get(c, 0) >= 2), None)
            if pair is None:
                continue
            taken.add(key)
            sc = person_score(m, key, person, kind)[0] if person else score_key(m, key, kind=kind)[0]
            label = t("experience." + pair[0])
            note = t("why.newtoyou", keep=label[:1].upper() + label[1:], change=t("experience." + pair[1]))
            scored.append(((tags[pair[0]] + tags[pair[1]]) * 0.15 + (match_pct(m, sc, person) or 50) / 100, key,
                           {**meta, "description": " · ".join(x for x in (note, meta.get("description")) if x)}))
        missing += feeling_report.get('missing_cards', 0)
    reserve = [x for _, _, x in sorted(scored, key=lambda r: -r[0])]
    metas = polish_row(balance_row(reserve), catalog=True)[:30]
    print(f"newtoyou: {who}, plan {len(plan)}, kept {len(metas)}", flush=True)
    if len(metas) < 8:
        return None
    return {cid: {"tv": metas, "pool": reserve},
            "_names": {cid: {"title": t("row.newtoyou.named", name=archetype_view(who)["name"]), "who": who,
                            "label_key": "row.newtoyou.named", "fields": {"name": archetype_view(who)["name"]},
                            "candidate_sources": rowplan.candidate_sources(reserve, missing)}}}


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
            items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}), lang=lang)))
    if items:
        rank_by_taste(m, items)  # facets, fingerprints and tags for the pool
    animation = not any("Animation" in frec(k, profile_kind(k)).get("g", ()) for k in explicit_seeds(person))
    candidates = [x for k, kind, x in items if frec(k, kind).get("x") and quality_ok(x) and tone_ok(x, animation=animation, person=person)]
    scores = member_scores(candidates, person)
    candidates.sort(key=lambda x: scores[x["id"]], reverse=True)
    catalog = catalog_picks(p, m, rid(person), person, lang, ranks=getattr(_upstream, 'catalog_ranks', None))
    ids = {x['id'] for x in catalog}
    candidates = interleave(catalog, [x for x in candidates if x['id'] not in ids][:40-len(catalog)])
    metas = member_balance(polish_row(balance_row([with_match(x, floor=0, person=person) for x in candidates]), catalog=True), person)[:40]
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
    ranks = getattr(_upstream, 'catalog_ranks', None)
    ranks = ranks if ranks is not None else {}
    # a shelf that comes out thin (< 10) gives its slot to the next mood in line
    for mood in list(dict.fromkeys(picks + rule + sorted(fitm, key=lambda k: -fitm[k]))):
        if not free:
            break
        title, xs, _ = MOODS[mood]
        title = t(title)
        feeling_report = {}
        pool = [x for x in feeling_pool(m, p, xs, lang, seeds=6, cid=free[0], ranks=ranks, report=feeling_report)
                if x[0] not in taken]
        tired = set(tired_keys({f"mood:{mood}"}))
        pool = [x for x in pool if x[0] not in tired]
        theme = {"kind": "mood", "name": title, "key": mood, "source": xs}
        reserve = themed_row(m, p, theme,
                             [x for _, _, x in pool], lang, free[0], exclude=taken | tired, feeling_report=feeling_report)
        if reserve is None:
            ranked_pool = rank_by_taste(m, pool)
            scored = []
            for sc, why, key, kind, meta in ranked_pool:
                tags = frec(key, kind).get("x") or {}
                hit = max(xs, key=lambda x: tags.get(x, 0))
                strength = sum(tags.get(x, 0) for x in xs)
                label = t("experience." + hit)
                scored.append((strength * 0.15 + (match_pct(m, sc) or 50) / 100, key,
                               {**meta, "description": " · ".join(y for y in (label[:1].upper() + label[1:], meta.get("description")) if y)}))
            reserve = [x for _, _, x in sorted(scored, key=lambda t: -t[0])]
        metas = reserve[:25]
        if len(metas) < 10:
            continue
        cid = free.pop(0)
        taken |= {x["id"] for x in metas}
        out[cid] = {"tv": metas, "pool": reserve}
        names[cid] = {"title": title, "mood": mood, "months": theme_months(theme)}
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
        ctx = learning_records("ctx")[-10:]
        learned = learning_rows()
        rows = {k: learned.get(f"mood:{k}") or {} for k in MOODS}
        past = [(day, v["picks"]) for day, v in sorted(_learn["moodpicks"].items()) if day < lday.isoformat()][-3:]
    likes = ", ".join(m["names"][f] for _, f in sorted(((a, f) for f, a in m["aff"].items() if f[0] == "x" and a > 0),
                                                       reverse=True)[:8])
    lines = [f"{when_text()} Choosing the mood shelves for {'tonight' if d.hour >= 5 else 'this late night'} "
             f"({lday:%A} evening, {year_season(lday)}). {household_context()}.",
             f"Experiences they love most: {likes}." if likes else ""]
    if ctx:
        lines.append("Recently watched on their TV (when): " + "; ".join(
            f"{c['title']} ({c['kind']}, {c['weekday']} {c['part']})" for c in reversed(ctx)) + ".")
    resp = [f"{t(MOODS[k][0])} shown {r['shown']}x, played {r['hits']:g}" for k, r in rows.items() if r.get("shown")]
    if resp:
        lines.append("How past mood shelves did (days shown, plays from them): " + "; ".join(resp) + ".")
    if past:
        lines.append("Shelves of the last evenings (vary a little): " + "; ".join(
            f"{day}: {', '.join(t(MOODS[k][0]) for k in ks if k in MOODS)}" for day, ks in past) + ".")
    return "\n".join(x for x in lines if x)


MOOD_Q = {k: {"type": "score", "criteria": MOOD_LEVELS,
              "instructions": f"How well does the shelf '{_translate(t, lang=row_lang())}' ({', '.join(EXPERIENCES[x][0] for x in xs)}) "
                              "fit this household tonight?"} for k, (t, xs, _) in MOODS.items()}


def mood_parse(resp, batch):
    answers = resp["answers"]
    scores = {k: float(answers[k]["score"]) if isinstance((answers.get(k) or {}).get("score"), (int, float))
              else None for k in MOODS}
    return [({"scores": scores}, 1.0 if all(v is not None for v in scores.values()) else 0.0)]


decision("mood_pick", ver=1, lane="feed", cap=3, ttl=DAY, serving=False, requires=("OR_KEY",), api=False,
    inp={"state": [str, 16]}, out={"scores": {k: (float, None) for k in MOODS}},
    norm=lambda x: x, rule=None, params={}, min_acc=1,
    llm={"path": "/api/alpha/decisions", "model": LUNA_DECIDER_MODEL, "batch": 1, "timeout": 20,
         "prompt": lambda b: {"model": LUNA_DECIDER_MODEL, "state": "".join(b[0]["state"]), "questions": MOOD_Q,
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
    stamp = (_feeds.get((MOODS_ROWS[0], LANG)) or {}).get("stamp", "")
    marker = next((part for part in stamp.split("/") if part.startswith("children=")), "")
    if marker != together_stamp("").rstrip("/"):
        got = None
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


# --- taste archetypes ---
# A short name and one plain line per adult member and for the household. Written by the command model about once a
# week from facets only (no titles, no names); without a key or on any failure a deterministic label stands in.
# Labels are display text only: nothing here feeds a score or a row order.
ARCHETYPE_DAYS = 7
_archetype_lock = threading.Lock()
ARCHETYPE_INP = {"language": (str, None), "scope": ("member", "household"), "experiences": [tuple(EXPERIENCES), 4], "themes": [str, 4],
                 "groups": [tuple(MOODS), 3]}


def archetype_aff(m, who):
    return (m.get("aff") if who in ("household", "together") else (m.get("paff") or {}).get(who)) or {}


def archetype_groups(aff):
    """Up to three mood families, strongest first, scored by their loved experiences (MOODS order breaks ties)."""
    score = {k: sum(max(0.0, aff.get(f"x:{x}", 0)) for x in xs) for k, (_, xs, _) in MOODS.items()}
    return [k for k in sorted(score, key=lambda k: -score[k]) if score[k] > 0][:3]


def archetype_brief(m, who):
    """Facet-only input for the label: top experiences, a few public themes and the strongest mood families."""
    aff = archetype_aff(m or {}, who)
    names = (m or {}).get("names") or {}
    xs = sorted(((a, f[2:]) for f, a in aff.items() if f[:2] == "x:" and a > 0 and f[2:] in EXPERIENCES),
                key=lambda r: (-r[0], r[1]))
    themes = []
    for a, fid in sorted(((a, f) for f, a in aff.items() if f[:2] in ("k:", "t:") and a > 0), key=lambda r: (-r[0], r[1])):
        name = names.get(fid)
        if isinstance(name, str) and 0 < len(name) <= 40 and speaks(fid, name):
            label = nice_facet(fid, name)
            if label not in themes:
                themes.append(label)
        if len(themes) == 4:
            break
    return {"scope": "household" if who in ("household", "together") else "member",
            "experiences": [x for _, x in xs[:4]], "themes": themes, "groups": archetype_groups(aff)}


def _archetype_clip(text, n):
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:") + "."


def archetype_label(inp):
    """The deterministic label: lead family plus a streak from the second family, and a line from the top facets."""
    groups = [g for g in inp.get("groups") or [] if g in MOODS]
    xs = [x for x in inp.get("experiences") or [] if x in EXPERIENCES]
    if not groups:
        return {"name": t("archetype.new"), "line": t("archetype.line.new")}
    lead = t(f"archetype.lead.{groups[0]}")
    name = t("archetype.name", lead=lead, streak=t(f"archetype.streak.{groups[1]}")) if len(groups) > 1 else lead
    themes = [x for x in inp.get("themes") or [] if isinstance(x, str) and x]
    if len(xs) >= 2 and (themes or len(xs) >= 3):
        key = "archetype.line.household" if inp.get("scope") == "household" else "archetype.line"
        line = t(key, first=t(f"experience.{xs[0]}"), second=t(f"experience.{xs[1]}"),
                 theme=themes[0].lower() if themes else t(f"experience.{xs[2]}"))
    else:
        line = t("archetype.line.new")
    return {"name": _archetype_clip(name, 48), "line": _archetype_clip(line, 140)}


def _archetype_text(value):
    if not isinstance(value, str):
        raise ValueError("type")
    value = " ".join(value.replace("\u2014", "-").replace("\u2013", "-").split()).strip(" \"'")
    if "<" in value or ">" in value or re.search(r"https?:|www\.|://|\.(?:com|net|org|io)\b", value, re.I):
        raise ValueError("markup")
    return value


def archetype_parse(reply):
    """{"name", "line"} from a chat reply; ValueError unless both are short plain text."""
    content = reply["choices"][0]["message"]["content"]
    data = search_commands.parse_json(content) if isinstance(content, str) else None
    if not isinstance(data, dict):
        raise ValueError("json")
    name, line = _archetype_text(data.get("name")), _archetype_text(data.get("line"))
    if not name or len(name) > 48 or len(name.split()) > 6:
        raise ValueError("name")
    if len(line.split()) < 3 or len(line) > 140:
        raise ValueError("line")
    return {"name": name, "line": line}


ARCHETYPE_SYS = ("You name the viewing taste of one person or of a household for a home TV app. Input is JSON: "
                 "experiences they love (ids), a few themes, and mood families, strongest first. Reply with JSON only: "
                 '{"name": "...", "line": "..."}. name: a warm, playful archetype of 2 to 5 words in the requested language and script, no names of '
                 "people or titles, no emoji. line: one plain sentence of 6 to 16 words describing the taste mix. "
                 "For scope household, describe what they share. No links, no markup, no dashes.")


def archetype_request(inp, model, effort):
    return {"model": model, "messages": [{"role": "system", "content": ARCHETYPE_SYS},
                                         {"role": "user", "content": json.dumps({**inp, "language": row_lang()}, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "reasoning": {"effort": effort},
            "max_tokens": 200 if effort == "none" else 600, "usage": {"include": True}}


decision("name_archetype", ver=2, lane="feed", cap=4 * (len(PEOPLE) + 1), ttl=ARCHETYPE_DAYS * DAY, serving=False,
    requires=("OR_KEY",), api=False, inp=ARCHETYPE_INP, out={"name": str, "line": str}, norm=lambda x: {**x, "language": row_lang()},
    rule=lambda inp, params: (archetype_label(inp), 1) if not inp["experiences"] and not inp["themes"] else None,
    params={}, min_acc=.95,
    llm={"path": "/api/v1/chat/completions", "model": COMMAND_MODEL, "batch": 1, "timeout": 10,
         "prompt": lambda b: archetype_request(b[0], COMMAND_MODEL, COMMAND_EFFORT),
         "parse": lambda r, b: [(archetype_parse(r), 1)],
         "fallback": {"path": "/api/v1/chat/completions", "model": COMMAND_FALLBACK_MODEL, "batch": 1, "timeout": 10,
                      "prompt": lambda b: archetype_request(b[0], COMMAND_FALLBACK_MODEL, COMMAND_FALLBACK_EFFORT),
                      "parse": lambda r, b: [(archetype_parse(r), 1)]}},
    fallback=lambda inp, err: (archetype_label(inp), 0))


def archetype_items():
    """Valid cached labels from archetypes.json; a missing or broken file reads as empty."""
    try:
        with open(os.path.join(DATA, "archetypes.json")) as fh:
            data = json.load(fh)
        items = data["items"] if isinstance(data, dict) and data.get("v") == 1 and isinstance(data.get("items"), dict) else {}
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    out = {}
    for who, rec in items.items():
        try:
            rec = row_names.mapping(rec)
            language = row_names.text(rec.get("language")) or "en-US"
            if who in (*PEOPLE, "household") and language.lower() == row_lang().lower() and rec["source"] in ("llm", "rule") and type(rec["at"]) in (int, float) \
                    and math.isfinite(rec["at"]):
                out[who] = {"name": _archetype_text(rec["name"]), "line": _archetype_text(rec["line"]),
                            "source": rec["source"], "at": int(rec["at"]), "language": language}
        except (ValueError, TypeError, KeyError):
            continue
    return out


def archetype_view(who):
    """{"name", "line", "source"} for a member or "household"; never calls a model."""
    who = "household" if who == "together" else who
    rec = archetype_items().get(who) if who in (*adult_people(), "household") else None
    if rec and rec["source"] == "llm" and rec["name"] and rec["line"]:
        return {"name": rec["name"], "line": rec["line"], "source": "llm"}
    m = get_model(False) if who in (*adult_people(), "household") else None
    return {**archetype_label(archetype_brief(m, who) if m else {}), "source": "rule"}


@household_write
def _archetype_save(items):
    path = os.path.join(DATA, "archetypes.json")
    fd, tmp = tempfile.mkstemp(prefix=".archetypes-", dir=DATA)
    try:
        with os.fdopen(fd, "w") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"v": 1, "items": items}, fh, ensure_ascii=False, allow_nan=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def archetypes_due(now):
    return bool(OR_KEY) and get_model(False) is not None


def refresh_archetypes(now=None):
    """Relabel members whose label is missing or a week old. Failures keep the rule label until the next week."""
    now = time.time() if now is None else now
    if not OR_KEY or not _archetype_lock.acquire(blocking=False):
        return {"calls": 0}
    try:
        m = get_model(False)
        if m is None:
            return {"calls": 0}
        before = history_cutoffs(DATA)
        items = archetype_items()
        whos = [*adult_people(), "household"]
        due = [w for w in whos if w not in items or not 0 <= now - items[w]["at"] < ARCHETYPE_DAYS * DAY]
        calls = 0
        for who in due:
            brief = archetype_brief(m, who)
            result = decide("name_archetype", brief)
            calls += result["source"] == "llm"
            items[who] = {**result["answer"], "source": "llm" if result["source"] in ("llm", "cache") else "rule",
                          "at": int(now), "language": row_lang()}
        if due:
            # A forget that landed during the model calls wins: keep its rewrite, drop labels built from forgotten taste.
            with history_locked(DATA):
                after = history_cutoffs(DATA)
                changed = {p for p in {*before, *after} if before.get(p) != after.get(p)}
                if changed:
                    items = {**archetype_items(), **{w: items[w] for w in due if w not in changed and w != "household"}}
                _archetype_save({w: v for w, v in items.items() if w in whos})
        return {"calls": calls, "due": len(due)}
    finally:
        _archetype_lock.release()


def archetypes_job(now):
    """Daily pass: the weekly relabel runs in the background so serving never waits on a model."""
    threading.Thread(target=refresh_archetypes, args=(now,), daemon=True, name="archetypes").start()
    return {"started": 1}


def week_release_facts(det, kind):
    today = local_now().date().isoformat()
    if kind == 'movie':
        releases = (det.get('release_dates') or {}).get('results', [])
        premiere = min((day.isoformat() for country in releases for r in country.get('release_dates', [])
                        if r.get('type') in (1, 2, 3) and (day := date_or_none((r.get('release_date') or '')[:10]))), default=None)
        dates = [(r.get('release_date') or '')[:10] for country in releases
                 if country.get('iso_3166_1') == rating_country() for r in country.get('release_dates', [])
                 if r.get('type') in (4, 6)]
        return {**({'premiere': premiere} if premiere else {}),
                **({'digital': max((d for d in dates if d and d <= today), default=None)} if dates else {})}
    dates = [s.get('air_date') for s in det.get('seasons') or [] if s.get('season_number')]
    return {**({'premiere': det['first_air_date']} if det.get('first_air_date') else {}),
            **({'season': max((d for d in dates if d and d <= today), default=None)} if dates else {})}


def week_candidates(lang, extra=()):
    """Cached watchable releases only; a chart position alone is not a release."""
    today, found = local_now().date(), {}
    with _feeds_lock:
        records = [v for (cid, l), v in sorted(_feeds.items(), key=lambda kv: kv[0][1] != LANG) if cid not in (rid('week'), rid('weekpicks'))]
    records.append({'metas': {'tv': extra}})
    for record in records:
        for xs in (record.get('metas') or {}).values():
            for meta in xs:
                if not isinstance(meta, dict) or meta.get('type') not in ('movie', 'series'):
                    continue
                kind = 'movie' if meta['type'] == 'movie' else 'tv'
                rec = frec(meta['id'], kind)
                facts = {**rec, **(meta.get('_row') or {})}
                if kind == 'movie' and old_movie_premiere((rec.get('premiere'), facts.get('premiere'),
                        facts.get('date') or (meta.get('releaseInfo') or rec.get('y') or '')[:4] + '-01-01')):
                    continue
                dates = [facts.get('digital')] if kind == 'movie' else [facts.get('premiere') or facts.get('date'), facts.get('season')]
                recent = [d for value in dates if (d := date_or_none(value)) and
                          0 <= (today-d).days <= (42 if kind == 'movie' else 28)]
                if recent:
                    found.setdefault((meta['id'], kind), {**meta, '_row': {**(meta.get('_row') or {}),
                                                                         'watchable': max(recent).isoformat(),
                                                                         'when': max(recent).isoformat()}})
    return list(found.values())


def week_digital(lang):
    """Background supplement: one list and at most twelve detail calls, no embeddings or tagging."""
    today = local_now().date()
    try:
        items = tmdb('discover/movie', page=1, region=rating_country(), with_release_type='4|6',
                     **{'release_date.gte': (today-datetime.timedelta(days=42)).isoformat(),
                        'release_date.lte': today.isoformat(), 'sort_by': 'popularity.desc',
                        'include_adult': 'false'}).get('results', [])[:12]
    except Exception:
        return []
    metas = []
    for it in items:
        key = f"tmdb:{it['id']}"
        rec = frec(key, 'movie')
        try:
            if 'digital' not in rec:
                det = tmdb(f"movie/{it['id']}", language=lang, append_to_response='keywords,credits,release_dates')
                rec = facet_record('movie', it['id'], det)
                with _facets_lock:
                    _facets[fkey(key, 'movie')] = rec
                    _facets_dirty.set()
                it = {**it, **det}
            meta = row_meta(it, 'movie', 'movie', TMDB_GENRES['movie'], it, lang=lang)
            metas.append(meta)
        except Exception:
            continue
    return metas


def week_scope(candidates, person):
    p, m = _profiles.get('household') or {}, get_model(False)
    token = _viewer.set('both' if person == 'together' else person)
    try:
        ranked = []
        for meta in candidates:
            if not row_eligible(meta, rid('week'), LANG, p):
                continue
            kind = 'movie' if meta['type'] == 'movie' else 'tv'
            score = person_score(m, meta['id'], person, kind)[0] if m else None
            pct = match_pct(m, score, person) if m else None
            if score is not None and (pct is None or pct >= 70):
                ranked.append((score, {**meta, '_week_pct': pct}))
        ranked.sort(key=lambda x: -x[0])
        # Uncalibrated taste admits only the leading fifth, at most twelve titles.
        limit = min(12, max(3, math.ceil(len(ranked)/5))) if any(x[1]['_week_pct'] is None for x in ranked) else 20
        return [x for _, x in ranked[:limit]]
    finally:
        _viewer.reset(token)


WEEK_SYS = ('Pick up to 3 new and worthwhile titles per scope from its vetted candidates only, using indices. '
            'Return JSON {scope: [{"i": candidate_index, "reason": "one short factual sentence"}]}. '
            'Prefer distinct titles across scopes. Use only the supplied release and taste facts. '
            'Do not invent titles, release dates, availability or connections. Write reasons in {language}.')
_week_lock = threading.Lock()


def week_start(d=None):
    day = (d or local_now()).date()
    return (day - datetime.timedelta(days=day.weekday())).isoformat()


def week_people():
    return [[p, LABELS[p], kids_age(p)] for p in PEOPLE]


def load_week_history():
    for key in ('week', 'weekpicks'):
        load_week_records(key)


def load_week_records(key):
    records = _learn.get(key, [])
    valid = []
    for r in records if isinstance(records, list) else []:
        try:
            if datetime.date.fromisoformat(r['week']).isoformat() != r['week'] or \
                    not isinstance(r['people'], list) or not isinstance(r['picks'], list) or \
                    'cards' in r and not isinstance(r['cards'], list):
                continue
            for p in r['picks']:
                check({'id': str, 'member': str, 'reason': str, 'week': str,
                       'kind': ('movie', 'tv'), 'title': str, **({'anchor': (str, None)} if key == 'weekpicks' else {})}, p)
                if p['week'] != r['week']:
                    raise ValueError('pick week')
            valid.append(r)
        except (ValueError, TypeError, KeyError):
            continue
    if key in _learn:
        _learn[key] = sorted(valid, key=lambda r: r['week'])[-8:]


def relabel_week(record):
    if record.get('row_lang', LANG).lower() == row_lang().lower():
        return False
    reasons = {}
    for x in record.get('cards', []):
        facts = x.get('_row') or {}
        reason = t('week.taste')
        if facts.get('watchable'):
            reason += ' · ' + t('week.release', date=day_text(facts['watchable']))
        reasons[x['id']] = reason
        x['_week'] = {person: reason for person in x.get('_week', {})}
        x['description'] = reason + ' · ' + (facts.get('overview') or '')
    for pick in record['picks']:
        pick['reason'] = reasons.get(pick['id'], t('week.taste'))
    record['row_lang'] = row_lang()
    return True


def week_history(weeks=1, person=None, offset=0, *, week=None):
    _refresh_learning()
    if type(weeks) is not int or type(offset) is not int or not 1 <= weeks <= 8 or not 0 <= offset <= 8-weeks:
        raise ValueError('weeks and offset must fit within the last 8 weeks')
    if person is not None:
        person = next((p for p in (*PEOPLE, 'together') if person.casefold() in (p.casefold(), LABELS.get(p, p).casefold())), None)
        if person is None:
            raise ValueError('unknown person')
    end = datetime.date.fromisoformat(week or week_start()) - datetime.timedelta(weeks=offset)
    start = end - datetime.timedelta(weeks=weeks-1)
    with _learn_lock:
        records = []
        for r in reversed(_learn.get('week', [])):
            if start.isoformat() <= r['week'] <= end.isoformat():
                record = copy.deepcopy(r)
                relabel_week(record)
                records.append({'week': record['week'], 'picks': [p for p in record['picks']
                                if (person is None or p['member'] == person) and title_allowed(p['id'], p['kind'], person)]})
        personal = []
        for r in reversed(_learn.get('weekpicks', [])):
            if start.isoformat() <= r['week'] <= end.isoformat():
                personal.append({'week': r['week'], 'picks': [{**p, 'reason': weekpick_reason(p['member'], p.get('anchor'))}
                    for p in r['picks'] if p['member'] in (*adult_people(), 'together') and
                    (person is None or p['member'] == person) and title_allowed(p['id'], p['kind'], person)]})
    return {'person': person, 'weeks': records, 'personal': personal}


def week_spread(metas):
    kept, franchises, worlds, heroes = [], Counter(), Counter(), 0
    for x in metas:
        franchise, world, hero = franchise_key(x), universe_key(x), is_hero(x)
        if franchises[franchise] >= 2 or world and worlds[world] >= 4 or hero and heroes >= ROW_HERO_MAX:
            continue
        kept.append(x)
        franchises[franchise] += 1
        worlds[world] += 1
        heroes += hero
    return kept


def week_feed(lang):
    """Background only. One attempt per calendar week, shared by languages and restarts."""
    with _week_lock:
        _refresh_learning()
        start, people = week_start(), week_people()
        cached, relabeled = None, False
        with _learn_lock:
            history = _learn.setdefault('week', [])
            previous = next((r for r in history if r['week'] == start), None)
            if previous and previous['people'] == people and 'cards' in previous:
                if relabel_week(previous):
                    _learn_dirty.set()
                    relabeled = True
                cached = copy.deepcopy(previous['cards'])
        if cached is not None:
            if relabeled:
                save_learn()
            return {rid('week'): {'tv': cached}}
        with _learn_lock:
            if not get_model(False):
                return None
            # Persist the reservation before any transport; even a failed attempt cannot spend twice.
            attempt = previous is None
            saved = previous.get('cards', []) if previous else []
            for r in history:
                r.pop('cards', None)
            record = {'week': start, 'people': people, 'picks': []}
            if 'row_lang' in CONFIG['region']:
                record['row_lang'] = row_lang()
            history[:] = sorted([r for r in history if r['week'] != start] + [record], key=lambda r: r['week'])[-8:]
            _learn_dirty.set()
        attempt = save_learn() and attempt
        candidates = week_candidates(lang, saved)
        if attempt and not any(x['type'] == 'movie' for x in candidates):
            candidates = week_candidates(lang, week_digital(lang))
        scopes = {person: week_scope(candidates, person) for person in (*adult_people(), 'together')}
        prompt = {person: [{'i': i, 'title': x['name'], 'release': x['_row']['watchable'],
                            'match': x['_week_pct'], 'genres': sorted(row_facts(x)['g'])}
                           for i, x in enumerate(xs)] for person, xs in scopes.items()}
        answer = {}
        if attempt and OR_KEY and any(scopes.values()):
            try:
                answer = search_commands.parse_json(llm_text(WEEK_SYS.replace('{language}', row_lang()),
                    household_context() + '.\n' + json.dumps(prompt, ensure_ascii=False),
                    4000, 150, 'feed', 'week', effort='medium'))
            except Exception:
                pass
        picks, cards, used = [], {}, set()
        for person, xs in scopes.items():
            chosen = []
            values = answer.get(person)
            if isinstance(values, list):
                for value in values[:3]:
                    if not isinstance(value, dict) or type(value.get('i')) is not int or not 0 <= value['i'] < len(xs):
                        continue
                    reason = value.get('reason')
                    if not isinstance(reason, str) or not reason.strip():
                        continue
                    x = xs[value['i']]
                    if x['id'] not in {m['id'] for m, _ in chosen}:
                        chosen.append((x, ' '.join(reason.split())[:160]))
            if not isinstance(values, list) or values and not chosen:
                for x in week_spread([x for x in xs if x['id'] not in used])[:3]:
                    reason = t('week.match', match=x['_week_pct']) if x['_week_pct'] is not None else t('week.taste')
                    chosen.append((x, reason + ' · ' + t('week.release', date=day_text(x['_row']['watchable']))))
            allowed = {x['id'] for x in week_spread([x for x, _ in chosen])}
            for x, reason in chosen:
                if x['id'] not in allowed:
                    continue
                picks.append({'id': x['id'], 'member': person, 'reason': reason, 'week': start,
                              'kind': 'movie' if x['type'] == 'movie' else 'tv', 'title': x['name'][:160]})
                if x['id'] not in cards:
                    cards[x['id']] = {**x, '_week': {}, '_week_start': start,
                                      'description': reason + ' · ' + (x['_row'].get('overview') or '')}
                cards[x['id']]['_week'][person] = reason
                used.add(x['id'])
        metas = week_spread(list(cards.values()))
        ids = {x['id'] for x in metas}
        with _learn_lock:
            record.update(picks=[p for p in picks if p['id'] in ids], cards=metas)
            _learn_dirty.set()
        save_learn()
        return {rid('week'): {'tv': copy.deepcopy(metas)}}


WEEKPICKS_SYS = ('Pick up to 3 real, unseen titles from any release year for each supplied scope. '
    'Return JSON {scope: [{"title": title, "year": year, "type": "movie" or "series", "anchor": loved_index}]}. '
    'Use only supplied scope keys. Connect each pick to one title from that scope\'s own indexed loved list; '
    'use null if no connection exists. Return results only, no prose. Do not pick seen, abandoned or hidden titles. '
    'Prefer distinct titles across scopes and one great surprise over the obvious sequel.')
_weekpicks_lock = threading.Lock()


def weekpick_reason(person, anchor=None):
    if person == 'together':
        pair = LABELS['pair']
        reason = (t('weekpicks.for', person=pair) if pair not in ('you both', 'everyone') else
                  t('weekpicks.you' if len(adult_people()) == 1 else 'weekpicks.pair' if len(adult_people()) == 2 else 'weekpicks.all'))
    else:
        reason = t('weekpicks.for', person=LABELS[person])
    return t('weekpicks.like', reason=reason, anchor=anchor) if isinstance(anchor, str) and anchor else reason


def weekpick_cards(record):
    cards = []
    for x in copy.deepcopy(record.get('cards', [])):
        scopes = {person: weekpick_reason(person, (x.get('_week_anchors') or {}).get(person))
                  for person in x.get('_week', {}) if person in (*adult_people(), 'together')}
        if scopes:
            cards.append({**x, '_week': scopes, 'description': next(iter(scopes.values())) + ' · ' +
                          ((x.get('_row') or {}).get('overview') or '')})
    return cards


def weekpicks_feed(lang):
    """Reserve one extra weekly call; language and household changes never spend again."""
    with _weekpicks_lock:
        _refresh_learning()
        start = week_start()
        with _learn_lock:
            history = _learn.setdefault('weekpicks', [])
            previous = next((r for r in history if r['week'] == start), None)
            if previous is not None:
                return {rid('weekpicks'): {'tv': weekpick_cards(previous)}}
            p, model = adult_profile(_profiles.get('household')), get_model(False)
            if not p or not model:
                return None
            for r in history:
                r.pop('cards', None)
            record = {'week': start, 'people': week_people(), 'picks': []}
            history[:] = sorted([*history, record], key=lambda r: r['week'])[-8:]
            _learn_dirty.set()
        attempt = save_learn() and _usage.get('weekpicks_week') != start
        if attempt:
            # Forget clears learning, but must not buy a second call in the same week.
            attempt = save_usage({'weekpicks_week': start}, counters=False)
        scopes, answer = {}, {}
        taste = read_taste().get('people') or {}
        for person in (*adult_people(), 'together'):
            mine = [e['title'] for e in sorted(person_entries(p, person), key=lambda e: -e['weight'])
                    if e['weight'] > 0 and person in e.get('by', ())][:12 if person != 'together' else 10]
            affinities = [model.get('names', {}).get(f, f) for f, a in
                          sorted((model.get('paff', {}).get(person) or {}).items(), key=lambda kv: -kv[1])
                          if f.startswith('x') and a > 0][:5]
            scopes[person] = {'loved': [{'i': i, 'title': title} for i, title in enumerate(mine)],
                              'enjoys': affinities, 'type_hint': taste.get(person, {}).get('type')}
        prompt = {'scopes': scopes, 'seen': [e['title'] for e in p['entries'][:60]]}
        if attempt and OR_KEY and adult_people():
            try:
                answer = search_commands.parse_json(llm_text(WEEKPICKS_SYS,
                    when_text() + '\n' + household_context() + '.\n' + json.dumps(prompt, ensure_ascii=False),
                    4000, 150, 'feed', 'weekpicks', effort='medium'))
            except Exception:
                pass
        cards, picks = {}, []
        with _learn_lock:
            recent = {pick['id'] for r in _learn.get('week', []) if r['week'] == start for pick in r['picks']}
        for person, facts in scopes.items():
            values = answer.get(person) if isinstance(answer, dict) else None
            if not isinstance(values, list):
                continue
            who = 'both' if person == 'together' else person
            masks = row_masks(who)
            for value in values[:3]:
                if not isinstance(value, dict) or not isinstance(value.get('title'), str) or not value['title'].strip() or \
                        len(value['title']) > 300 or value.get('type') not in ('movie', 'series') or \
                        type(value.get('year')) is not int or not 1800 <= value['year'] <= local_now().year:
                    continue
                stype, kind = value['type'], 'movie' if value['type'] == 'movie' else 'tv'
                anchor_i = value.get('anchor')
                anchor = facts['loved'][anchor_i]['title'] if type(anchor_i) is int and 0 <= anchor_i < len(facts['loved']) else None
                reason = weekpick_reason(person, anchor)
                got = row_search(stype, kind, [(value['title'], str(value['year']), stype, '')], lang)
                for x in got[:1]:
                    if x['id'] in recent or x['id'] in masks[0] | masks[2] or catalog_drop(x, p, rid('weekpicks'), who):
                        continue
                    if x['id'] in cards and person in cards[x['id']]['_week']:
                        continue
                    picks.append(dict(id=x['id'], member=person, reason=reason, week=start,
                                      kind=kind, title=x['name'][:160], anchor=anchor))
                    if x['id'] not in cards:
                        cards[x['id']] = {**x, '_week': {}, '_week_anchors': {}, '_week_start': start}
                    cards[x['id']]['_week'][person] = reason
                    cards[x['id']]['_week_anchors'][person] = anchor
        with _learn_lock:
            record.update(picks=picks, cards=list(cards.values()))
            _learn_dirty.set()
        save_learn()
        return {rid('weekpicks'): {'tv': weekpick_cards(record)}}


# --- LLM rows ------------------------------------------------------------------

POOL_SEEDS = 16
POOL_PER_SEED = 6
POOL_DETAILS = 50
POOL_CATALOG = 12
POOL_CATALOG_TOL = 1e-6  # Keep previous picks when score noise straddles the cutoff.
POOL_TRENDING = 6
POOL_AREA_SIM = 0.8  # Near-identical experience mixes wait, while overlapping tastes can still seed different areas.


def known_rank(m, p, person=None):
    with _facets_lock:
        pairs = {(slot.partition('@')[0], rec.get('kind') or slot.partition('@')[2] or 'movie')
                 for slot, rec in _facets.items() if rec.get('title') and slot.startswith('tmdb:')
                 and slot.partition('@')[0] not in p.get('seen', ())}
    ranked = [(score, why, key, kind) for key, kind in pairs
              for score, why in [person_score(m, key, person, kind)] if score is not None]
    ranked.sort(key=lambda x: (-x[0], x[2], x[3]))
    return ranked


def known_meta(key, kind, lang=LANG, cached=None):
    rec = frec(key, kind)
    names = TMDB_GENRES['movie' if kind == 'movie' else 'series']
    it = {**rec, 'id': int(key[5:]), 'title': rec.get('title'), 'name': rec.get('title'),
          'release_date': (rec.get('y') or '') + '-01-01', 'first_air_date': (rec.get('y') or '') + '-01-01',
          'genre_ids': [gid for gid, name in names.items() if name in rec.get('g', ())],
          'original_language': rec.get('lang'), 'overview': rec.get('o') or ''}
    meta = row_meta(it, 'movie' if kind == 'movie' else 'series', kind, names, it,
                    collection=rec.get('collection'), lang=lang)
    meta['_row']['g'] = rec.get('g') or []
    for field in ('poster', 'background'):
        if (cached or {}).get(field):
            meta[field] = cached[field]
    return meta


def catalog_exclusions(cid, person=None):
    masks = row_masks('both' if person == 'together' else person or 'both')
    ignored = {r['key'] for r in title_fixes() if r.get('op') == 'ignore'
               and (r.get('who') == person or person is None and r.get('who') not in guest_people(CONFIG))}
    return ignored | masks[0] | masks[2], set(tired_keys({cid}))


def catalog_drop(meta, p, cid, person=None, *, exclusions=None):
    key, kind = meta['id'], 'movie' if meta['type'] == 'movie' else 'tv'
    if key in p.get('seen', ()):
        return 'seen'
    filtered, tired = catalog_exclusions(cid, person) if exclusions is None else exclusions
    if frec(key, kind).get('adult') or key in p.get('negative', ()) or key in filtered:
        return 'filtered'
    if key in tired:
        return 'tired'
    votes = row_facts(meta).get('votes') or 0
    if votes < (200 if meta['type'] == 'movie' else 50) or not quality_ok(meta):
        return 'below floor'
    token = _viewer.set(person)
    try:
        if not title_allowed(key, kind, person) or not row_eligible(meta, cid, LANG, p):
            return 'filtered'
    finally:
        _viewer.reset(token)
    return None


LAB_IDEA = {'id': (str, None), 'name': str, 'members': [str, 20], 'months': ('any', [int, 12]), 'picks': [str, 20]}
LAB_CANDIDATE = {'token': str, 'title': str, 'year': (str, None), 'kind': ('movie', 'tv'), 'runtime': (int, None),
                 'genres': [str, 30], 'experience': [str, 100], 'match': (int, None)}
LAB_INPUT = {'day': str, 'language': str, 'moment': row_metrics.MOMENTS, 'month': int, 'capacity': int,
             'members': [str, 20], 'home_names': [str, 100], 'outcomes': [{'name': str, 'state': ('kept', 'retired'),
             'moments': [str, 4]}, 12], 'taste': [{'member': str, 'experience': [str, 5], 'genres': [str, 5]}, 20], 'refills': [LAB_IDEA, 3], 'candidates': [LAB_CANDIDATE, 250]}


def lab_candidates():
    from itertools import zip_longest
    p, m = _profiles.get('household') or {}, get_model(False)
    if not m:
        return []
    home = set()
    for person in (None, *adult_people(), 'together'):
        with addon_scope(person):
            who = SCOPES[person] if person is not None else 'both'
            rows = build_home(LANG, p, row_masks(who), who)
            home.update((x['id'], 'movie' if x['type'] == 'movie' else 'tv') for xs in rows.values() for x in xs)
    rankings = [known_rank(m, p, person) for person in (*adult_people(), 'together')]
    used, out = set(), []
    for group in zip_longest(*rankings):
        for ranked in group:
            if ranked is None:
                continue
            score, _, key, kind = ranked
            pair = key, kind
            if pair in used or pair in home:
                continue
            used.add(pair)
            meta = known_meta(key, kind)
            if catalog_drop(meta, p, rid('lab'), None):
                continue
            rec = frec(key, kind)
            out.append(dict(token=kind+':'+key[5:], title=rec['title'], year=rec.get('y') or None, kind=kind,
                            runtime=rec.get('runtime'), genres=rec.get('g') or [], experience=list(rec.get('x') or {}),
                            match=match_pct(m, score)))
            if len(out) == 250:
                return out
    return out


def lab_request(batch):
    return {'model': COMMAND_MODEL, 'messages': [{'role': 'system', 'content':
        'Choose results, not prose. Return JSON {"ideas":[{"id":null,"name":"short row name","members":["together"],'
        '"months":"any","picks":["movie:123"]}]}. Propose at most capacity new ideas, plus requested refills. '
        'For a refill use its exact id and name. Choose 8 to 20 distinct picks ONLY from candidates by typed token or zero-based index. '
        'Refills may retain their supplied existing picks. Aim for 12 to 20 unseen picks when refilling. '
        'Never invent or look up titles or facts. Unknown facts stay unknown. Members must come from the supplied list. '
        'Months are any or a nonempty list of month numbers 1..12, judged by meaning. '
        'Names must read like plain everyday Netflix row names in the household language, never translationese, '
        'never "your partner", no punctuation tricks. Approved style examples in both languages: '
        'Cozy Mysteries / Gemütliche Krimis; Gripping Thrillers / Packende Thriller; Mind-Bending / Kopfkino; '
        'Based on True Stories / Nach wahren Begebenheiten; Heartwarming Stories / Herzerwärmende Geschichten; '
        'Zeit für große Sagen; Heute wird gelacht. Learn from recent kept and retired names and their play moments. '
        'Local moments, with exclusive ends and late checked first: late 22:00 to 04:00 any day; '
        'weekend Fri 17:00 to Sun 22:00; evening Mon to Thu 17:00 to 22:00; day everything else.'},
        {'role': 'user', 'content': json.dumps(batch[0], ensure_ascii=False)}],
        'response_format': {'type': 'json_object'}, 'reasoning': {'effort': COMMAND_EFFORT},
        'max_tokens': 2200, 'usage': {'include': True}}


def lab_parse(reply, batch):
    raw = json.loads(reply['choices'][0]['message']['content'])
    inp = batch[0]
    candidates = [c['token'] for c in inp['candidates']]
    refills = {i['id']: i for i in inp['refills']}
    names, used, ideas = {n.casefold() for n in inp['home_names']}, set(), []
    new = 0
    for proposal in raw.get('ideas', []):
        if not isinstance(proposal, dict):
            continue
        identity = proposal.get('id')
        refill = refills.get(identity) if isinstance(identity, str) else None
        if identity is not None and not refill or identity in used or not refill and new >= inp['capacity']:
            continue
        name = refill['name'] if refill else proposal.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or not refill and name.strip().casefold() in names:
            continue
        people, months = (refill['members'], refill['months']) if refill else (proposal.get('members'), proposal.get('months'))
        if not isinstance(people, list) or not people or any(not isinstance(p, str) or p not in inp['members'] for p in people) or len(set(people)) != len(people):
            continue
        if months != 'any' and (not isinstance(months, list) or not months or any(type(m) is not int or not 1 <= m <= 12 for m in months) or len(set(months)) != len(months)):
            continue
        picks = []
        allowed = set(candidates) | set(refill['picks'] if refill else [])
        for pick in proposal.get('picks', []) if isinstance(proposal.get('picks'), list) else []:
            token = candidates[pick] if type(pick) is int and 0 <= pick < len(candidates) else pick
            if isinstance(token, str) and token in allowed and token not in picks:
                picks.append(token)
        if len(picks) < 8:
            continue
        ideas.append(dict(id=identity, name=name.strip(), members=people, months=months, picks=picks[:20]))
        names.add(name.strip().casefold())
        if refill:
            used.add(identity)
        else:
            new += 1
    return [({'ideas': ideas}, 1)]


decision('lab_propose', ver=1, lane='lab', cap=2, ttl=DAY, serving=False, requires=('OR_KEY',), api=False,
    inp=LAB_INPUT, out={'ideas': [LAB_IDEA, 5]}, norm=lambda x: x, rule=None, params={}, min_acc=1,
    llm={'path': '/api/v1/chat/completions', 'model': COMMAND_MODEL, 'batch': 1, 'timeout': 15,
         'prompt': lab_request, 'parse': lab_parse}, fallback=lambda inp, err: ({'ideas': []}, 0))


def lab_job(now):
    try:
        refresh_preferences()
        if lab.paused(CONFIG) or not row_enabled(rid('lab')):
            return
        with history_locked(DATA):
            doc = lab.sync(DATA, CONFIG, now)
            lab.save(DATA, doc)
            day = local_now(now).date().isoformat()
            capacity = max(0, 2-sum(i['state'] == 'trial' for i in doc['ideas']))
            p = _profiles.get('household') or {}
            refills = [{k: i[k] for k in ('id', 'name', 'members', 'months')} |
                       {'picks': [x['kind']+':'+x['key'][5:] for x in i['picks']]}
                       for i in doc['ideas'] if i['state'] == 'kept' and
                       sum(x['key'] not in p.get('seen', ()) for x in i['picks']) < 12]
            current = {i['id'] for i in lab.select(DATA, CONFIG, now, doc=doc)}
            # shortcut: refill three kept ideas per day; enlarge if Home gains more lab slots.
            refills.sort(key=lambda i: i['id'] not in current)
            refills = refills[:3]
            if OR_KEY and doc.get('attempt_day') != day and (capacity or refills):
                doc['attempt_day'] = day
                lab.save(DATA, doc)  # Reserve the local date before any transport, including failed calls.
                candidates = lab_candidates()
                m = get_model(False)
                outcomes = sorted((i for i in doc['ideas'] if i['state'] in ('kept', 'retired')),
                    key=lambda i: max(i.get('retired_at', 0), i.get('last_play') or 0, i.get('manual_keep', 0), i.get('created', 0)), reverse=True)[:12]
                home_names = [name if isinstance(name, str) else ' / '.join(name.values()) for _, _, name in feed_rows(LANG)]
                inp = dict(day=day, language=LANG, moment=row_metrics.moment(CONFIG, now)[0], month=row_metrics.moment(CONFIG, now)[1],
                    capacity=capacity, members=[*adult_people(), 'together'], home_names=home_names,
                    outcomes=[{k: i[k] for k in ('name', 'state', 'moments')} for i in outcomes],
                    taste=[dict(member=who, experience=[x['name'] for x in taste_axes(m, who)['x']],
                                genres=[x['name'] for x in taste_axes(m, who)['g']]) for who in (*adult_people(), 'together')] if m else [],
                    refills=refills, candidates=candidates)
                answer = decide('lab_propose', inp)['answer'] if len(candidates) >= 8 else {'ideas': []}
                for proposal in answer['ideas']:
                    picks = [dict(key='tmdb:'+token.partition(':')[2], kind=token.partition(':')[0]) for token in proposal['picks']]
                    if proposal['id']:
                        next(i for i in doc['ideas'] if i['id'] == proposal['id'])['picks'] = picks
                    else:
                        doc['ideas'].append({**proposal, 'id': uuid.uuid4().hex, 'picks': picks, 'state': 'trial',
                                             'moments': [], 'created': now, 'reason': 'trial'})
                lab.save(DATA, doc)
            lab_catalogs()
    except Exception as e:
        _daily_log('row lab skipped: '+type(e).__name__)


def catalog_picks(p, m, cid, person=None, lang=LANG, ranks=None):
    scope = getattr(_scoping, 'scope', None)
    person = person or (scope if scope in PEOPLE else None)
    viewers = (person,) if person else adult_people()
    if ranks is None:
        ranks = {}
    with _feeds_lock:
        previous = {(x['id'], 'movie' if x['type'] == 'movie' else 'tv')
                    for x in ((_feeds.get((cid, lang)) or {}).get('metas') or {}).get('tv', [])}
        cached = {(x['id'], 'movie' if x['type'] == 'movie' else 'tv'): x
                  for store in (_feeds, _shown) for row in store.values()
                  for xs in (row.get('metas') or {}).values() if isinstance(xs, list) for x in xs if isinstance(x, dict)}
    lists = []
    for who in viewers:
        if who not in ranks:
            ranks[who] = known_rank(m, p, who)
        ranked = sorted(ranks[who], key=lambda x: (-(x[0] + POOL_CATALOG_TOL * ((x[2], x[3]) in previous)),
                                                  x[2], x[3])) if previous else ranks[who]
        lists.append(iter(ranked))
    exclusions = catalog_exclusions(cid, person)
    out, seen = [], set()
    while lists:
        remaining = []
        for ranked in lists:
            for _, _, key, kind in ranked:
                if (key, kind) in seen:
                    continue
                seen.add((key, kind))
                meta = known_meta(key, kind, lang, cached.get((key, kind)))
                if catalog_drop(meta, p, cid, person, exclusions=exclusions):
                    continue
                meta['_row']['catalog'] = True
                out.append(with_match(meta, floor=0, person='together' if scope == 'together' else person))
                remaining.append(ranked)
                break
            if len(out) == POOL_CATALOG:
                return out
        lists = remaining
    return out


def pool_seeds(p, report=None):
    eligible, trace = [], {}
    for e in p['entries']:
        item = e.get('item') or {}
        backfill = (item.get('backfilled') or item.get('_summary', {}).get('origin') == 'history.json') and not item.get('at')
        reason = ('negative' if e['key'] in p.get('negative', ()) or e.get('weight', 0) < 0 else
                  'backfill' if backfill and e.get('signal') != 'loved' else
                  'not positive' if e.get('weight', 0) <= 0 else None)
        identity = (e['key'], e['kind'])
        trace.setdefault(identity, {'key': e['key'], 'kind': e['kind'], 'title': e['title'],
                                    'chosen': False, 'reason': reason or 'limit', 'candidates': 0, 'kept': 0})
        if reason is None:
            eligible.append(e)
    recent = top_seeds({'entries': eligible}, len(eligible))
    head = recent[:POOL_SEEDS]
    tv = top_seeds({'entries': [e for e in eligible if e.get('who') in ('tv', 'both')]}, 3)
    tv = [e for e in tv if e not in head][:2]
    seeded = {(e['key'], e['kind']) for e in head + tv}
    favorites = [e for e in eligible if (e['key'], e['kind']) not in seeded
                 and (e['signal'] in ('loved', 'liked') or
                      e['signal'] == 'watched' and (e.get('item', {}).get('episodes_played') or 0) >= 20)]
    unique = {}
    for e in sorted(favorites, key=lambda e: -e['weight'] / (1 + e['age'] / 365)):
        unique.setdefault((e['key'], e['kind']), e)
    favorites = list(unique.values())[:min(3, max(0, len(head) - 1))]
    cut = max(0, len(head) - len(favorites) - len(tv))
    choices = [(e, 'recent') for e in head[:cut]] + [(e, 'favourite') for e in favorites] + \
              [(e, 'shared TV') for e in tv] + [(e, 'recent') for e in recent[cut:]]
    chosen, franchises, universes, areas, deferred, visited = [], set(), set(), [], [], set()
    for e, source in choices:
        identity = (e['key'], e['kind'])
        if identity in visited:
            continue
        visited.add(identity)
        rec = frec(e['key'], e['kind'])
        meta = {'id': e['key'], 'type': e['stype'], 'name': e['title'], '_row': {'collection': rec.get('collection')}}
        franchise, universe = franchise_key(meta), universe_key(meta)
        area = rec.get('x') or {}
        reason = ('duplicate franchise' if franchise in franchises else
                  'duplicate universe' if universe and universe in universes else
                  'similar area' if any(xsim(area, a) >= POOL_AREA_SIM for a in areas) else None)
        if reason:
            trace[identity]['reason'] = reason
            if reason == 'similar area':
                deferred.append((e, franchise, universe, area))
            continue
        if len(chosen) >= POOL_SEEDS:
            continue
        chosen.append(e)
        franchises.add(franchise)
        if universe:
            universes.add(universe)
        areas.append(area)
        trace[identity].update(chosen=True, reason=source)
    for e, franchise, universe, area in deferred:
        identity = (e['key'], e['kind'])
        if franchise in franchises or universe and universe in universes:
            trace[identity]['reason'] = 'duplicate franchise' if franchise in franchises else 'duplicate universe'
        elif len(chosen) < POOL_SEEDS:
            chosen.append(e)
            franchises.add(franchise)
            if universe:
                universes.add(universe)
            trace[identity].update(chosen=True, reason='similar area fallback')
    if report is not None:
        report['seeds'] = list(trace.values())
    return chosen


def candidate_pool(p, n=40, metas=False, trending_only=None, report=None, catalog=None):
    """Known catalog matches, spread seed recommendations and this week's trending.
    Trending must meet the median recommendation score; without a model it only fills a pool below 20.
    With the taste model, a wider pool is ranked by match; without, the first n in TMDB order."""
    try:
        m = get_model()
    except Exception as e:
        print(f"taste model: {type(e).__name__}: {e}", flush=True)
        m = None
    seeds, per = pool_seeds(p, report), 20 if m else 15
    # Each background personal_feed calls this twice: at most 34 lists and 50 candidate details per invocation.
    jobs = [(e, _pool.submit(tmdb, f"{e['kind']}/{e['id']}/{endpoint}", language="en-US"))
            for e in seeds for endpoint in ('recommendations', 'similar')]
    jobs += [({"kind": k, "title": None}, _pool.submit(tmdb, f"trending/{k}/week", language="en-US")) for k in ("movie", "tv")]
    cands, seen = [], set(p["seen"]) | set(p["negative"]) | set(tired_keys({rid('foryou')}))
    if trending_only is None:
        trending_only = set()
    recommended, unavailable = set(), 0
    for e, job in jobs:
        try:
            data = job.result().get("results", [])
        except Exception:
            unavailable += 1
            continue
        for it in data[:per]:
            key = f"tmdb:{it['id']}"
            if e.get('title'):
                recommended.add((key, e['kind']))
            else:
                trending_only.add((key, e['kind']))
            if key in seen or (it.get("vote_count") or 0) < 100:
                continue
            seen.add(key)
            cands.append((key, e["kind"], it, e))
    trending_only.difference_update(recommended)
    why = {}
    if m:
        ranked = rank_by_taste(m, [(key, kind, (it, e)) for key, kind, it, e in cands], enrich=False)
        ranked.sort(key=lambda x: (x[0] is None, -(x[0] or 0),
                                   -(x[4][0].get('vote_average') or 0) if x[0] is None else 0,
                                   -(x[4][0].get('vote_count') or 0) if x[0] is None else 0))
        missing = [(key, kind) for _, _, key, kind, _ in ranked if not frec(key, kind)][:POOL_DETAILS]
        try:
            if missing:
                ensure_facets(missing)
            known = [(key, kind) for _, _, key, kind, _ in ranked if frec(key, kind).get('title')]
            if known and _emb_model == EMB_MODEL:  # The feed worker starts global embedding migrations separately.
                ensure_emb(known)
            taggable = [(key, kind) for key, kind in known if 'o' in frec(key, kind)][:50]
            if taggable:
                tag_experiences(taggable, max_batches=2, fetch_facets=False)
        except BudgetExceeded:
            pass
        except Exception as e:
            embedding_failure(e)
        ranked = rank_by_taste(m, [(key, kind, payload) for _, _, key, kind, payload in ranked], enrich=False)
        rec_scores = [sc for sc, _, _, _, (_, e) in ranked if e.get('title') and sc is not None]
        floor = statistics.median(rec_scores) if rec_scores else None
        cands = [(key, kind, it, e) for sc, _, key, kind, (it, e) in ranked
                 if e.get('title') or floor is not None and sc is not None and sc >= floor]
        why = {key: (match_pct(m, sc), reasons(m, key, w, kind=kind)) for sc, w, key, kind, _ in ranked}
        n = 50
    counts, kept, spread, trending = Counter(), Counter(), [], 0
    for key, kind, it, e in cands:
        identity = (e.get('key'), kind)
        if e.get('title'):
            counts[identity] += 1
            if kept[identity] >= POOL_PER_SEED:
                continue
            kept[identity] += 1
        else:
            if trending >= POOL_TRENDING:
                continue
            trending += 1
        spread.append((key, kind, it, e))
    cands = interleave([c for c in spread if c[3].get('title')], [c for c in spread if not c[3].get('title')],
                       key=lambda c: (c[0], c[1])) if m else spread
    if not m and sum(bool(e.get('title')) for _, _, _, e in cands) >= 20:
        cands = [c for c in cands if c[3].get('title')]
    catalog = (catalog_picks(p, m, rid('foryou')) if catalog is None else catalog) if m else []
    ids = {x['id'] for x in catalog}
    size = len({(key, kind) for key, kind, _, _ in cands} |
               {(x['id'], 'movie' if x['type'] == 'movie' else 'tv') for x in catalog})
    if report is not None:
        for e in report['seeds']:
            identity = (e['key'], e['kind'])
            e.update(candidates=counts[identity], kept=kept[identity])
        report.update(pool_size=size, distinct_seeds=len(seeds), cap=POOL_PER_SEED,
                      cap_removed=sum(counts.values()) - sum(kept.values()),
                      max_seed_share=max(kept.values(), default=0) / size if size else 0,
                      distinct_franchises=len({franchise_key({'id': key, 'type': 'movie' if kind == 'movie' else 'series',
                                                             'name': it.get('title') or it.get('name'),
                                                             '_row': {**it, 'collection': it.get('collection') or frec(key, kind).get('collection') or
                                                                      (it.get('belongs_to_collection') or {}).get('id')}})
                                               for key, kind, it, _ in cands} | {franchise_key(x) for x in catalog}),
                      unavailable_endpoints=unavailable)
    if metas:
        out = [row_meta(it, "movie" if kind == "movie" else "series", kind, genres(kind, "en-US"), it, lang="en-US")
               for key, kind, it, _ in cands if key not in ids]
        return interleave(catalog, [with_match(x) for x in out if quality_ok(x) and tone_ok(x)])
    out = []
    for key, kind, it, e in cands[:n]:
        if key in ids:
            continue
        year = (it.get("release_date") or it.get("first_air_date") or "")[:4]
        pct, w = why.get(key, (None, []))
        out.append(f"{it.get('title') or it.get('name')} ({year}, {'movie' if kind == 'movie' else 'series'}, "
                   f"{'like ' + e['title'] if e.get('title') else 'trending this week'}"
                   f"{f', match {pct}%' if pct else ''}{', ' + ' / '.join(w) if w else ''})")
    known = [f"{x['name']} ({x.get('releaseInfo', '')}, {x['type']}, "
             f"{' / '.join(match_note(x['id'], floor=0, kind='movie' if x['type'] == 'movie' else 'tv'))})" for x in catalog]
    return interleave(known, out, key=lambda x: x)[:n]


def row_search(stype, kind, picks, lang):
    jobs = [(p[3], p[4] if len(p) == 5 else "", _outer.submit(resolve_pick, kind, p[0], p[1], lang)) for p in picks if p[2] == stype]
    names, metas, seen = genres(kind, lang), [], set()
    for why, target, job in jobs:
        try:
            hit = job.result()
        except Exception:
            continue
        if hit and hit[0]["id"] not in seen:
            seen.add(hit[0]["id"])
            people = list(PEOPLE) if target == "together" else [target] if target in PEOPLE else []
            note = clean_note(why)
            if people and note:
                note = t("row.member", person=" & ".join(LABELS[p] for p in people)) + ": " + note
            meta = row_meta(hit[0], stype, kind, names, hit[1], note=note, lang=lang)
            if people and note:
                meta["_row"].update({"for": people, "personal": True})
            metas.append(meta)
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


def household_context(hint=""):
    if len(adult_people()) != len(PEOPLE):
        return f"Household of {len(adult_people())} adults sharing one TV, " + " & ".join(LABELS[p] for p in adult_people()) + hint
    note = CONFIG["household"]["note"].rstrip(".!? \t\n\r\v\f")
    return (f"Household of {MEMBER_COUNT} sharing one TV, {MEMBER_NAMES}{hint}"
            + ("; " + note if note else ""))


def personal_feed(lang):
    """One reasoning call builds two rows: For You and the AI half of Watch Again."""
    foryou_enabled, rewatch_enabled = row_enabled(rid('foryou')), row_enabled(rid('rewatchai'))
    if not foryou_enabled and not rewatch_enabled:
        return {}
    p = adult_profile(_profiles.get("household"))
    if not p or not (p["entries"] or p["searches"] or p.get("likes")):
        return None
    sections, heads = [], []
    if foryou_enabled:
        sections.append("## FOR YOU\n(24 lines, 12 movies and 12 series incl. a few anime: what they will most likely love next; "
                        "include a few strong new releases and current hits when they fit)")
        heads.append('FOR YOU')
    if rewatch_enabled:
        sections.append("## REWATCH\n(14 lines, mostly movies plus a few series: big, rewatchable favourites and comfort "
                        "classics this household would happily watch again, matching what they love; already-watched titles "
                        "are welcome here; skip the first films of sagas they named, those are covered)")
        heads.append('REWATCH')
    system = PERSONAL_SYS.format(sections='\n'.join(sections), example="a connection to a favourite", language=row_lang(), targets=','.join(adult_people()))
    trending_only = set()
    m = get_model()
    catalog = catalog_picks(p, m, rid('foryou'), lang=lang,
                            ranks=getattr(_upstream, 'catalog_ranks', None)) if foryou_enabled else []
    pool = candidate_pool(p, trending_only=trending_only, catalog=catalog) if foryou_enabled else []
    hits, tired = hit_titles(), tired_keys({rid('foryou')}) if foryou_enabled else {}
    types = {n: (c or {}).get("type") for n, c in (read_taste().get("people") or {}).items() if n in adult_people()}
    hint = (f" (personality types {', '.join(f'{LABELS.get(n, n.title())} {t}' for n, t in types.items() if t)}: a light hint only, "
            "taste comes from what they watch)" if any(types.values()) else "")
    prompt = (f"{when_text()} {household_context(hint)}"
              + f". Pick for {'both' if len(adult_people()) == 2 else 'all'} of them, not one blended person.\n"
              + profile_text(p) +
              (f"\nWhat their favourites have in common (from title metadata): {m['taste_line']}" if m and m.get("taste_line") else "") +
              ("\nEarlier picks from these rows they actually played (find more like these): " + "; ".join(hits) if hits else "") +
              ("\nShown to them for a week, never played (do not suggest again, lean away from them): "
               + "; ".join(list(tired.values())[:20]) if tired else "") +
              ("\nCandidates (known catalog matches, TMDB recommendations and this week's trending, ranked by our taste match; "
               "rerank, use the strongest, add your own ideas): " + "; ".join(pool) if pool else ""))
    sec = llm_sections(system, prompt, 5000, 150, "personal", heads, effort="medium")
    out = {}
    if foryou_enabled:
        exclude = p["seen"] | p["negative"] | set(tired)
        foryou = picks_to_rows(sec.get("FOR YOU", []), lang, exclude)
        mixed = interleave(foryou.get("movie", []), foryou.get("series", []))
        pool_report = {}
        candidates = candidate_pool(p, n=100, metas=True, trending_only=trending_only, report=pool_report, catalog=catalog)
        ids = {x['id'] for x in catalog}
        mixed = interleave(catalog, interleave(mixed, [x for x in candidates if x['id'] not in ids]))
        out[rid('foryou')] = {"tv": polish_row(balance_row([x for x in mixed if quality_ok(x) and tone_ok(x)], foryou=True), catalog=True)[:40]}
        pool_report['seeds'] = [e for e in pool_report.get('seeds', []) if e['chosen']]
        out['_names'] = {rid('foryou'): {'pool': pool_report}}
        # Derived feed provenance also covers AI picks and borrowed refills outside the filtered pool.
        if trending_only:
            out['_names'][rid('foryou')]['trending_only'] = sorted(trending_only)
    if rewatch_enabled:
        recent = {e["key"] for e in p["entries"] if e["age"] < 60}
        out[rid('rewatchai')] = picks_to_rows(sec.get("REWATCH", []), lang, recent)
    return out


def archived_feed(lang, person):
    p = _profiles.get(person)
    label = ARCHIVED[person]
    head = "FOR " + label.upper()
    if not p or not (p["entries"] or p.get("likes")):
        return None
    h = _profiles.get("household")
    system = PERSONAL_SYS.format(sections=f"## {head}\n(24 lines, 12 movies and 12 series: what {label} will most likely love next)", language=row_lang(), targets=person,
                                 example="a connection to a favourite")
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
    rid('wonder'): ("row.docs.wonder", "WONDER",
                    "nature, animals, oceans, our living planet and how life evolved: pure awe",
                    [18330, 9902, 361118, 270, 239155, 5647, 4627, 34152, 311]),
    rid('cosmos'): ("row.docs.cosmos", "MIND & COSMOS",
                    "space and the universe, physics, time, black holes, the brain and consciousness: how reality works",
                    [9882, 15325, 160330, 41608, 6012, 217034, 8056, 4380, 360221, 234133, 161219]),
    rid('history'): ("row.docs.history", "GREAT LIVES",
                     "larger-than-life figures and empires of history (Constantine the Great, Alexander, Caesar, "
                     "Cleopatra, Genghis Khan, Napoleon...), ancient worlds and turning points",
                     [5049, 1405, 15012, 355987, 157894, 208596, 2280, 6165, 2163, 383073]),
    rid('meaning'): ("row.docs.meaning", "BIG QUESTIONS",
                     "spirituality, religions and myths, philosophy, the meaning of life and our place in the world",
                     [10706, 188, 1532, 177146, 2035]),
}
DOC_TREND = ("row.docs.trending",)
# Their favourite makers: BBC, arte, ZDF/Terra X (networks for series, companies for both). Titles from them rank higher.
DOC_FAV = {"with_networks": "4|332|100|1155|1628|662|31|308",
           "with_companies": "34333|8090|9050|80893|201|587|4606|11237"}
DOC_FAV_BONUS = 0.15
DOC_BLOCK = {"without_genres": "80,10402,10763,10764,10767", "without_keywords": "33722"}  # no crime, news, reality, talk
DOCS_SYS = ("You pick documentaries and documentary series for {household} in {country} who love awe, history and big "
            "thinking. Reply with exactly these sections and nothing else:\n{sections}\n"
            "Line format: Title|Year|m or s|why   (m=movie, s=series). why = max 10 words in {ui_language} on what will amaze "
            "or move them. Real, well-made titles only. Their favourite makers are arte, Terra X (ZDF) and the BBC: "
            "lean on them, {language} productions welcome; also National Geographic, PBS, Netflix and other English or "
            "{language} productions; docudrama series count. Classics and hidden gems mixed, best first. "
            "Wonder and uplift over outrage: no true crime, no exposés, no celebrity or music biographies.")


def docs_ai_feed(lang):
    """Once a week: an AI shortlist per documentary shelf (stored as the hidden row <row-prefix>.docsai)."""
    p, m = adult_profile(_profiles.get("household")), get_model()
    if not p:
        return None
    sections = "\n".join(f"## {sec}\n(16 lines: {about})" for _, sec, about, _ in DOC_ROWS.values())
    loved = [e["title"] for e in p["entries"] if e["weight"] > 0][:25]
    prompt = (f"{when_text()} {" and ".join(LABELS[p] for p in adult_people())} share one TV. Titles they love: {'; '.join(loved)}."
              + (f"\nWhat their favourites have in common: {m['taste_line']}" if m and m.get("taste_line") else "")
              + "\nPick documentaries that give the same pull: epic scale, great characters, a sense of wonder.")
    sec = llm_sections(DOCS_SYS.format(sections=sections, household=HOUSEHOLD_KIND, country=COUNTRY, language=REGION_LANGUAGE, ui_language=row_lang()), prompt, 6500, 180, "docs", [v[1] for v in DOC_ROWS.values()],
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
            out[key] = (kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}), lang=lang),
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
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
    rid('hbo'): ("row.hbo", {"tv": {"with_networks": "49|3186"}}),
    rid('paramount'): ("row.paramount", {"tv": {"with_networks": "4330|2076|67"},
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
                items.append((key, kind, row_meta(it, "movie" if kind == "movie" else "series", kind, names, art.get(it["id"], {}), lang=lang)))
        ranked = [x for *_, x in rank_by_taste(m, items)]
        shows, films = [x for x in ranked if x["type"] != "movie"], [x for x in ranked if x["type"] == "movie"]
        mixed = [x for i in range(30) for x in (shows[2 * i:2 * i + 2] + films[i:i + 1])]  # the series lead: two to one
        out[cid] = {"tv": polish_row(balance_row([with_match(x) for x in mixed if quality_ok(x) and tone_ok(x)]))[:40]}
    print("studios: " + ", ".join(f"{c.split('.')[1]}={len(v['tv'])}" for c, v in out.items()), flush=True)
    return out


# --- backups ---
BACKUP_FILES = ("commands.json", "skald.json", "legacy.json", ".skald-household", ".skald-format", "feedback.jsonl", "imports.jsonl", "sessions.jsonl", "taste.json", "trakt.json",
                "history.json", "history-summary.json", "history-forgotten.json", "history-seen.json", "health-settings.json", "learn.json", "online.json", "metrics.json", "lab.json", "decisions.jsonl", "usage.json",
                "searches.jsonl", "emb.json", "facets.json", "memory.json", "memory.log.jsonl", "memory.prev.json", "interview.json", "shelves.json", "undo.jsonl", "wishes.jsonl", "shelf_rules.json",
                "title_fixes.json", "archetypes.json") + (("connectors.json",) if CONNECTOR_BACKUP else ())
BACKUP_SKIP = {"addon.json": "secret, never opened", "remote.json": "secret, never opened", "clients.json": "secret, never opened",
               "credentials": "private credential contents, never read",
               "hydra.json": "secret, never opened", "decide_cache.json": "TTL LLM cache, refilled on demand",
               "feeds.json": "serving cache, rebuilt by feed_worker", "shown.json": "serving cache, rebuilt by feed_worker",
               "shown_scopes.json": "serving cache, starts from legacy pins when absent",
               "metrics_shown.json": "private attribution evidence, never archived",
               "shelves.lock": "shelf writer coordination, recreated on demand",
               "history.lock": "history writer coordination, recreated on demand",
               "usage.lock": "usage writer coordination, recreated on demand",
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
    # Hold the forget lock before reading config or opening any member files.
    with history_locked(DATA):
        return _backup_state(now, archive_name=archive_name)


def _backup_state(now, *, archive_name=None):
    import errno
    import stat
    import tarfile
    from skald.history import sources
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
    with household_writable(DATA, household_erased) as allowed:
        if not allowed:
            return False
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
        for name in dict.fromkeys((*BACKUP_FILES, *(name for name, _, _ in sources(cfg, DATA) if name.endswith('.history.json')))):
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
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


# --- daily jobs ---
DAILY_JOBS = [("backup", backup_due, backup_state)]
DAILY_JOBS.append(("memory", memory_due, memory_job))
DAILY_JOBS.append(("history", history_cleanup_due, cleanup_history))
DAILY_JOBS.append(("row_metrics", lambda now: True, lambda now: update_row_metrics(now, cleanup=True)))
DAILY_JOBS.append(("lab", lambda now: True, lab_job))
DAILY_JOBS.append(("archetypes", archetypes_due, archetypes_job))
DAILY_JOBS.append(("ai_credit", lambda now: bool(os.environ.get('OPENROUTER_API_KEY')), check_ai_credit))
_daily = None
_daily_lock = threading.Lock()


def _daily_save():
    with history_locked(DATA), _daily_lock, household_writable(DATA, household_erased) as allowed:
        if not allowed:
            return False
        out = (_daily or {}).get('memory', {}).get('out', {})
        forgotten = history_cutoffs(DATA)
        if forgotten:
            people = load_config(DATA, readonly=True)[0]['household']['members']
            for person in forgotten:
                if person not in people:
                    out.pop('active_'+person, None)
        snap = json.dumps(_daily)
        fd, tmp = tempfile.mkstemp(prefix=".daily-", suffix=".tmp", dir=DATA)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(snap)
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

def together_stamp(stamp):
    children = sorted(p for p in PEOPLE if kids_age(p) is not None)
    return f"children={','.join(children)}/{stamp}" if children else stamp


def job_specs(d, hashes):
    """(job, row ids, stamp, min seconds between taste-only rebuilds, builder)."""
    # Rows roll over at 05:00, not midnight: the late-night slot stays one build.
    ld = d - datetime.timedelta(hours=5)
    day, hh = ld.strftime("%Y-%m-%d"), hashes.get("household", "")
    specs = [
        ("episodes", [rid('episodes')], f"{day}/{d.hour // 3}/{hh}{hashes.get('episodes', '')}", 1800, lambda l: {rid('episodes'): episodes_feed(l)}),
        ("because", BECAUSE_ROWS, f"r2/{day}/{hh}", 1800, because_feed),
        ("rewatch", [rid('rewatch')], f"{day}/{hh}", 1800, lambda l: {rid('rewatch'): rewatch_feed(l)}),
        ("upcoming", [rid('upcoming')], f"{day}/{d.hour // 6}/{hh}", 1800, lambda l: {rid('upcoming'): upcoming_feed(l)}),
        ("catchup", [rid('catchup')], f"{day}/{hh}{hashes.get('episodes', '')}", 1800, lambda l: {rid('catchup'): catchup_feed(l)}),
        ("ready", READY_ROWS, f"{day}/{hh}{hashes.get('episodes', '')}", 1800, ready_feed),
        ("lanes", LANE_ROWS, f"source1/{day}/{hh}", 3600, lanes_feed),
        ("leap", [rid('leap')], f"source1/{day}/{hh}", 3600, leap_feed),
        *((person, [cid], f"{day}/{hh}", 3600, lambda l, p=person: member_feed(l, p)) for cid, person in ROW_OWNER.items()),
        ("moods", MOODS_ROWS, f"source1/{day}/{hh}", 3600, mood_feed),
        ("docs", DOCS, f"nzb/{day}/{(_feeds.get((rid('docsai'), LANG)) or {}).get('built', 0)}/{hh}", 3600, docs_feed),
        ("studios", STUDIO_ROWS, f"{day}/{hh}", 3600, studios_feed),
        ("top10", [rid('top10')], f"{day}/{d.hour // 12}", 0, lambda l: {rid('top10'): top10_feed(l)}),
        ("airing", [rid('airing')], f"{day}/{d.hour // 6}", 0, lambda l: {rid('airing'): anime_feed(l, False)}),
        ("nextseason", [rid('nextseason')], day, 0, lambda l: {rid('nextseason'): anime_feed(l, True)}),
        ("newtoyou", [rid('newtoyou')], f"{day}/{hh}", 3600, newtoyou_feed),
        ("week", [rid('week')], f"new1/{week_start(d)}/{week_people()}", 0, week_feed),
        ("weekpicks", [rid('weekpicks')], f"{week_start(d)}/{week_people()}", 0, weekpicks_feed),
    ]
    if SHELF_ROWS:
        specs.append(("shelves", SHELF_ROWS, d.strftime("%Y-%m-%d/%H/%M") + hh, 0, shelf_feed))
    if any(kids_age(p) is not None for p in PEOPLE):
        ages = tuple(kids_age(p) for p in PEOPLE)
        specs.append(("family", [rid('family')], f"{day}/{hh}/{ages}", 3600, family_feed))
    if lab_catalogs():
        specs.append(('lab', LAB_ROWS, str(lab_stamp()), 0, lab_feed))
    if OR_KEY:
        specs += [
            ("personal", [cid for cid in (rid('foryou'), rid('rewatchai')) if row_enabled(cid)], f"{day}/{ld.hour // 12}/{hh}", 3600, personal_feed),
            ("docsai", [rid('docsai')], "{}-W{:02d}-{}".format(*ld.isocalendar()[:2], len(DOC_ROWS)), 0, docs_ai_feed),
        ]
        for person in ARCHIVED:
            if person in hashes:
                specs.append((person, [rid(person)], f"{day}/{hashes[person]}", 3 * 3600, lambda l, p=person: archived_feed(l, p)))
    shared = {"because", "rewatch", "upcoming", "catchup", "lanes", "leap", "moods", "docs", "studios", "personal", "week", "docsai",
              "newtoyou", "ready", "weekpicks"}
    for key, builder in (('digital', digital_feed), ('toprated', toprated_feed)):
        if row_enabled(rid(key)):
            specs.append((key, [rid(key)], 'wide1/'+day if key == 'toprated' else day, 0, lambda l, k=key, b=builder: {rid(k): b(l)}))
    label_stamp = 'row_lang=' + row_lang() + '/' if 'row_lang' in CONFIG['region'] else ''
    return [(name, cids, label_stamp + (together_stamp(stamp) if name in shared else stamp), gap, build)
            for name, cids, stamp, gap, build in specs if any(row_enabled(cid) for cid in cids)]


# AIOStreams caches our manifest (with the row names) for hours but fetches rows fresh. Rows whose name changes
# (because, lanes) are therefore served as they were when the manifest was last fetched, so name and content agree.
DYNAMIC = BECAUSE_ROWS + READY_ROWS + [rid('leap'), rid('newtoyou')] + MOODS_ROWS + LANE_ROWS + SHELF_ROWS
_shown = {}
_shown_scoped = {}
_scoping = threading.local()
_scoping.scope = None
_pinning = threading.local()
_pin_cache = {}
_pin_epoch = 0
_pin_lock = threading.RLock()


def forget_person_caches():
    from pathlib import Path
    from skald.health import SERVING_CACHES
    global _erasure_generation
    with _pin_lock, _home_lock, _feeds_lock:
        _erasure_generation += 1
        for store in (_profiles, _feeds, _shown, _shown_scoped, _pin_cache, _home_cache, _feeds_empty):
            store.clear()
        _model.update(sig=None, m=None)
        with _remote_lock, _imports_lock, _person_model_lock, _taste_ring_lock:
            for store in (_remote_cache, _imports_cache, _person_models, _taste_ring):
                store.clear()
        for name in ('profile.json', 'taste_ring.json', 'learn.json', 'online.json', 'memory.json.bad', *SERVING_CACHES,
                     *('health-cache-'+n for n in SERVING_CACHES)):
            Path(DATA, name).unlink(missing_ok=True)


def pin_decision(lang):
    lab_catalogs()
    # Lock order: _pin_lock -> _home_lock -> _feeds_lock; _connectors_lock is a leaf, taken alone; row evaluation holds neither home nor feeds lock.
    with _pin_lock:
        refresh_preferences()
        masks, p, m = remote_masks(), _profiles.get("household"), get_model(False)
        # Each session's served rows apply its own memory veto (W10b); none and both differ there.
        masks_by = {who: row_masks(who) for who in ("none", *PEOPLE, "both")}
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
            if (rid('ready1'), lang) in pins:
                from skald import ready
                record = {**pins[rid('ready1'), lang], 'ready_slots': {
                    who: [[r['key'], r.get('season')] for r in ready.choices(SimpleNamespace(**globals()), lang, who, masks) if r['selected']]
                    for who, masks in masks_by.items()}}
                pins[rid('ready1'), lang] = records[rid('ready1'), lang] = record
            above = {who: set() for who in masks_by}
            moods = {who: 0 for who in above}
            cap = rowplan.family_caps(CONFIG)['mood'] if rowplan.capped(CONFIG) else len(MOODS_ROWS)
            for cid in DEDUPE_ORDER[:DEDUPE_ORDER.index(LANES[0])]:
                if cid in LAB_ROWS or not row_enabled(cid):
                    continue
                for typ in ("movie", "series") if cid in (rid('top10'), rid('toprated')) else ("movie",) if cid == rid('digital') else ("tv",) if cid in MIXED else ("series",):
                    for who in above:
                        if cid in MOODS_ROWS and moods[who] >= cap:
                            continue
                        metas = served_row(cid, typ, lang, p or {}, above[who], masks_by[who], who=who)
                        moods[who] += bool(metas) and cid in MOODS_ROWS
                        above[who] = mark_above(above[who], cid, metas)
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
                    if all(len(row) >= ROW_MIN or not maker_view(candidate.get("names") or {}, p or {"entries": []}, who)
                           for who, row in rows.items()):
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
    token = _safety_context.set(("app", SCOPES[scope] if scope is not None else None))
    try:
        yield
    finally:
        _scoping.scope = previous
        _safety_context.reset(token)


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
            with household_writable(DATA, household_erased) as allowed:
                if not allowed:
                    return False
                with open(path + ".tmp", "w") if scope is None else os.fdopen(
                        os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                    if scope is not None:
                        os.fchmod(fh.fileno(), 0o600)
                    json.dump(snap, fh, ensure_ascii=False)
                os.replace(path + ".tmp", path)
        except OSError:
            pass


def lab_stamp():
    try:
        st = os.stat(os.path.join(DATA, "lab.json"))
        return st.st_ino, st.st_mtime_ns, st.st_size, row_metrics.moment(CONFIG, time.time())
    except FileNotFoundError:
        return None


def lab_catalogs():
    global _lab_visible
    try:
        doc = lab.read(DATA)
        ids = [rid('lab_'+i['id']) for i in doc['ideas']]
    except (OSError, ValueError, KeyError, TypeError):
        doc, ids = {'ideas': []}, []
    if ids != LAB_ROWS:
        MIXED.difference_update(LAB_ROWS)
        THEMED_ROWS.difference_update(LAB_ROWS)
        DYNAMIC[:] = [cid for cid in DYNAMIC if cid not in LAB_ROWS]
        LAB_ROWS[:] = ids
        MIXED.update(ids)
        THEMED_ROWS.update(ids)
        DYNAMIC.extend(ids)
        position = max((_LAB_ORDER.index(cid)+1 for cid in ROW_OWNER), default=7)
        DEDUPE_ORDER[:] = _LAB_ORDER[:position] + ids + _LAB_ORDER[position:]
    visible = tuple((scope, tuple(i['id'] for i in lab.select(DATA, CONFIG, time.time(), scope, doc)))
                    for scope in (None, *PEOPLE, 'together')) if CONFIG['household'].get('rows') is None or 'lab' in CONFIG['household']['rows'] else ()
    visible = tuple(entry for entry in visible if entry[1])
    if visible != _lab_visible:
        _lab_visible = visible
        aio_rows_changed()
    return LAB_ROWS


def lab_feed(lang, cid=None):
    if lab.paused(CONFIG):
        return {}
    out, names = {}, {}
    try:
        for idea in lab.read(DATA)['ideas']:
            if idea['state'] == 'retired' or cid is not None and rid('lab_'+idea['id']) != cid:
                continue
            identity = rid('lab_'+idea['id'])
            out[identity] = {'tv': [known_meta(x['key'], x['kind'], lang) for x in idea['picks']]}
            names[identity] = {'title': idea['name'], 'months': idea['months']}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {**out, '_names': names}


def feed(cid, lang):
    if cid.startswith(rid('lab_')):
        rows = lab_feed(lang, cid)
        return {'metas': rows[cid], 'names': rows['_names'][cid]} if cid in rows else {}
    if hasattr(_pinning, "feeds"):
        return _pinning.feeds.get((cid, lang)) or {}
    scope = getattr(_scoping, "scope", None)
    shown = _shown if scope is None else _shown_scoped.get(scope, {})
    return shown.get((cid, lang)) or _feeds.get((cid, lang)) or {}


def save_feeds():
    if household_erased(DATA):
        return
    with _pin_lock, _feeds_lock:
        generation = _erasure_generation
        snap = {f"{cid}|{lang}": v for (cid, lang), v in _feeds.items()}
    tmp = os.path.join(DATA, "feeds.json." + uuid.uuid4().hex + ".tmp")
    try:
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            fh = open(tmp, "w")
        with fh:
            json.dump(snap, fh, ensure_ascii=False)
        with _pin_lock, household_writable(DATA, household_erased) as allowed:
            if generation == _erasure_generation and allowed:
                os.replace(tmp, os.path.join(DATA, "feeds.json"))
    except OSError:
        pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state():
    if not row_metrics.settings(CONFIG)['enabled']:
        row_metrics.disable(DATA)
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
    load_usage()
    load_decisions()
    try:
        with open(os.path.join(DATA, "learn.json")) as fh:
            _learn.update(json.load(fh))
    except (OSError, ValueError):
        pass
    load_week_history()
    try:
        with open(os.path.join(DATA, "nzb.json")) as fh:
            _nzb.update(json.load(fh))
    except Exception:
        pass
    load_emb()
    load_facets()
    keep = {cid for cid in DEDUPE_ORDER if row_enabled(cid)} | set(SHELF_ROWS) | set(LANE_BENCH) | {rid('family'), rid('rewatchai'), *{rid(p) for p in ARCHIVED}, rid('docsai')}
    keep |= set(CONFIG['household'].get('retired_rows', []))
    keep |= {rid(f"{stem}_{p}_{n}") for stem in ('shelf', 'shelfraw') for p in (*retained_people(), 'together') for n in range(1, 11)}
    for key in [k for k in _feeds if k[0] not in keep]:  # retired rows (gems, season, now, short)
        _feeds.pop(key)
    for store in (_feeds, _shown, *_shown_scoped.values()):
        for key, rec in list(store.items()):
            if key[0] in CONFIG['household'].get('retired_rows', []):
                continue
            language = next((part.removeprefix('row_lang=') for part in rec.get('stamp', '').split('/')
                             if part.startswith('row_lang=')), LANG)
            if ('metas' in rec or 'names' in rec) and language.lower() != row_lang().lower():
                store.pop(key)


def feed_pass(d, retry):
    global _feed_started, _defer_enrichment
    requested = rowplan.consume_refresh(command_context())
    if requested:
        hashes = {name: p.get('hash', '') for name, p in _profiles.items()}
        for job, cids, _, _, _ in job_specs(d, hashes):
            if requested.intersection(cids):
                for key in list(retry):
                    if key[0] == job:
                        retry.pop(key)
    if not _feed_started:
        _feed_started = True
        _defer_enrichment = not bool(_feeds or _profiles)
        if _defer_enrichment:
            build_feeds(d, {}, retry, only={"top10", "toprated"})
    try:
        hashes = refresh_profiles()
        m = _model["m"] or {}
        stale = any(v and getattr(v, "model", _emb_model) != _emb_model
                    for v in (m.get("vec"), *(m.get("pvec") or {}).values()))
        if _defer_enrichment or stale:
            get_model()
    except Exception as e:
        print(f"profiles failed: {type(e).__name__}: {e}", flush=True)
        hashes = {}
    build_feeds(d, hashes, retry)


def build_feeds(d, hashes, retry, only=None, *, force=False):
    with _home_lock:
        generation = _erasure_generation
    _theme_seasons.pending = {}
    ranks = {}
    for lang in list(_langs):
        try:
            for job, cids, stamp, min_gap, build in job_specs(d, hashes):
                if only is not None and job not in only:
                    continue
                try:
                    connectors_tick(time.time())
                except Exception as e:
                    _daily_log(f"connectors tick failed: {type(e).__name__}")
                try:
                    stamp = f"v{STAMP_V}/{stamp}"
                    cur = _feeds.get((cids[0], lang))
                    stale = any((cid, lang) in _feeds and 'stamp' not in _feeds[cid, lang] for cid in cids)
                    if not force and ((cur and cur.get("stamp") == stamp and not stale) or retry.get((job, lang), 0) > time.time()):
                        continue
                    # Same time window, only the taste changed: rebuild at most every min_gap seconds.
                    if not force and not stale and cur and min_gap and cur.get("stamp", "").split("/")[:-1] == stamp.split("/")[:-1] \
                            and time.time() - cur.get("built", 0) < min_gap:
                        continue
                    try:
                        failures = [0]
                        _upstream.failures = failures
                        _upstream.catalog_ranks = ranks
                        try:
                            rows = build(lang)
                        finally:
                            _upstream.failures = None
                            _upstream.catalog_ranks = None
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
                        with _home_lock, _feeds_lock:
                            if generation != _erasure_generation:
                                return
                            for cid in cids:
                                if (cid, lang) not in _feeds:
                                    _feeds_empty.add((cid, lang))
                        retry[(job, lang)] = time.time() + 1800
                        continue
                    names = rows.pop("_names", None) or {}
                    with _home_lock, _feeds_lock:
                        if generation != _erasure_generation:
                            return
                        for cid in cids:
                            rec = {"stamp": stamp, "built": int(time.time()), "metas": rows.get(cid) or {}}
                            if names.get(cid):
                                rec["names"] = names[cid]
                            source = rowplan.source_names(SimpleNamespace(**globals()), cid, lang)
                            if source:
                                rec.setdefault('names', {}).update(source)
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
            if only is None:
                pin_decision(lang)
        except Exception as e:
            _daily_log(f"feed lang {lang} failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
    pending = _theme_seasons.pending
    del _theme_seasons.pending
    for theme in pending.values():
        field = {"lane": "lane", "mood": "mood", "leap": "seed"}[theme["kind"]]
        with _feeds_lock:
            slots = [rec["names"] for rec in _feeds.values() if (rec.get("names") or {}).get(field) == theme["key"]
                     and theme["name"] in (rec["names"].get("title"), rec["names"].get("name"))]
        if not slots:
            continue
        months = theme_months(theme, allow_llm=True)
        if months is not None:
            with _feeds_lock:
                for names in slots:
                    names["months"] = months
    if pending:
        save_feeds()


def finish_cold_enrichment(d, retry):
    """Build or enrich taste after the first feed pass; retain rows until their replacements are ready."""
    global _defer_enrichment
    if not _profiles.get("household") or (not _defer_enrichment and _model["m"] is not None):
        return
    _defer_enrichment = False
    with _model_lock:
        _model["sig"] = None
    try:
        get_model()
    except Exception:
        _defer_enrichment = True
        raise
    finally:
        with _person_model_lock:
            _person_models.clear()
        clear_seed_cache()
        independent = {"top10", "toprated", "digital"}
        hashes = {name: p.get("hash", "") for name, p in _profiles.items()}
        affected = {cid for job, cids, _, _, _ in job_specs(d, hashes) if job not in independent for cid in cids}
        with _feeds_lock:
            for (cid, lang), rec in _feeds.items():
                if cid in affected:
                    rec.pop("stamp", None)
        for key in list(retry):
            if key[0] not in independent:
                retry.pop(key)
        save_feeds()


def feed_worker():
    retry = {}
    while True:
        if household_erased(DATA):
            time.sleep(60)
            continue
        try:
            search_commands.expire(command_context())
        except Exception as e:
            _daily_log(f'expiry tick failed: {type(e).__name__}')
        try:
            connectors_tick(time.time())
        except Exception as e:
            _daily_log(f"connectors tick failed: {type(e).__name__}")
        try:
            feed_pass(local_now(), retry)
            with _health_lock:
                repair_jobs = set(_health_jobs)
                _health_jobs.clear()
            if repair_jobs:
                hashes = {name: p.get('hash', '') for name, p in _profiles.items()}
                build_feeds(local_now(), hashes, {}, only=repair_jobs, force=True)
            finish_cold_enrichment(local_now(), retry)
            refresh_preferences()
            update_row_metrics(time.time())
            backfill_countries()
            backfill_ratings()
            backfill_runtime()
            if _emb_model != EMB_MODEL:
                ensure_emb([])
        except Exception as e:
            _daily_log(f"feed pass failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
        for name, save in (("learn", save_learn), ("nzb", save_nzb), ("emb", save_emb), ("facets", save_facets)):
            try:
                save()
            except Exception as e:
                _daily_log(f"save {name} failed: {type(e).__name__}: {e}".replace("\n", " ").replace("\r", " ")[:240])
        daily_jobs(time.time())
        time.sleep(60)


def update_row_metrics(now, *, cleanup=False):
    if not row_metrics.settings(CONFIG)['enabled']:
        row_metrics.disable(DATA)
        return {"enabled": 0}
    plays, sources = row_metrics.collect(SimpleNamespace(**globals()), now)
    row_metrics.update(DATA, CONFIG, plays, now, sources=sources, cleanup=cleanup)
    if not lab.paused(CONFIG):
        with history_locked(DATA):
            before = lab.read(DATA)
            doc = lab.sync(DATA, CONFIG, now, before)
            if doc != before:
                lab.save(DATA, doc)
                lab_catalogs()
    return {"inputs": len(plays)}


def interleave(*lists, key=lambda m: m["id"]):
    out, seen = [], set()
    for group in zip(*(lst + [None] * (max(map(len, lists)) - len(lst)) for lst in lists)):
        for m in group:
            if m and key(m) not in seen:
                seen.add(key(m))
                out.append(m)
    return out


# Serving facts stay with the feed, so row requests never fetch metadata.
ROW_GENRES = {14: "Fantasy", 16: "Animation", 80: "Crime", 99: "Documentary", 9648: "Mystery", 10402: "Music",
              10751: "Family", 10762: "Kids", 10764: "Reality"}
REASON_STOP = {"ghost", "cop", "las vegas", "thanksgiving", "christmas", "halloween", "scotland", "holiday",
               "holidays", "new york city", "london", "paris", "witch", "coming of age"}
BECAUSE_CUTOFF = 0.28
BECAUSE_SEED_SIM = .65  # Below .65 cosine, shared experience tags leave room for a different taste area.
WEEK_SEED_FLOOR = .64
WEEK_TAIL_FLOOR = .45
WEEK_PENALTY = .25


def row_meta(it, stype, kind, names, en, mark="", note="", collection=None, lang=LANG):
    meta = to_meta(it, stype, kind, names, en, mark, note, lang=lang)
    col = it.get("belongs_to_collection") or en.get("belongs_to_collection") or {}
    releases = [week_release_facts(x, kind) for x in (en, it)]
    meta["_row"] = {**releases[0], **releases[1], "votes": it.get("vote_count"), "rating": it.get("vote_average"),
                    "popularity": it.get("popularity"),
                    "genre_ids": it.get("genre_ids") or [g["id"] for g in it.get("genres") or [] if "id" in g],
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
    if kind == 'movie' and (premieres := [r['premiere'] for r in releases if r.get('premiere')]):
        meta['_row']['premiere'] = min(premieres)
    return meta


def row_facts(meta):
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    facts = dict(row_names.mapping(meta.get("_row")))
    facts["g"] = set(facts.get("g") or ()) | set(rec.get("g") or ()) | set(meta.get("genres") or ())
    facts["lang"] = facts.get("lang") or rec.get("lang")
    facts['runtime'] = facts.get('runtime') or rec.get('runtime')
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


def tone_ok(meta, seed=None, animation=False, person=None):
    if kids_age(person) is not None:
        return True
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
    note = note.rstrip(" .,!?:;·…")
    if row_lang().startswith("en") and re.search(r"\b(my|your|his|her|its|our|their|a|an|the)$", note, re.I):
        return ""
    return note + "." if note else ""


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


def human_cards(metas, person=None):
    child = kids_age(safety_subject(person)) is not None
    counts, out = {}, []
    for i, meta in enumerate(metas):
        parts, overview = card_parts(meta)
        ready_note = (meta.get('_row') or {}).get('ready_note')
        weekpick_note = (meta.get('_row') or {}).get('weekpick_note') if not child else None
        notes = [ready_note] if ready_note else [weekpick_note] if weekpick_note else [meta["_memory"]["why"]] if meta.get("_memory") and not child else []
        for part in (() if child else parts):
            if notes:
                break
            part = part.strip()
            if part in (t("match.strong"), t("match.good"), t("match.wildcard")) or not part:
                continue
            if not reason_ok(part, meta["id"], kind="movie" if meta["type"] == "movie" else "tv"):
                continue
            part = clean_note(part)
            if not part or counts.get(part.casefold(), 0) >= 2 or part in notes:
                continue
            notes.append(part)
            counts[part.casefold()] = counts.get(part.casefold(), 0) + 1
            break  # one warm reason is enough beside an overview
        card = {k: v for k, v in meta.items() if k not in ("_row", "_memory", "_borrowed")}
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
    catalog = any(row_names.mapping(x.get("_row")).get("catalog") is True for x in metas)
    if catalog and len(out) < 10:
        pending = []
        for x in rest:
            if len(out) < 10 and (x["id"] not in bleak_ids or dark < 1):
                out.append(x)
                dark += x["id"] in bleak_ids
            else:
                pending.append(x)
        rest = pending
    for x in rest:
        if len(out) < 20 and x["id"] in bleak_ids and dark >= (1 if catalog and len(out) < 10 else 5):
            tail.append(x)
        else:
            out.append(x)
            if len(out) <= 20:
                dark += x["id"] in bleak_ids
    return out if strict or catalog and len(out) < 20 else out + tail


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


def row_targets(meta):
    targets = row_names.mapping(meta.get("_row")).get("for")
    return targets if isinstance(targets, list) and all(isinstance(p, str) and p in PEOPLE for p in targets) else []


def member_pick(meta, person):
    if person in row_targets(meta):
        return True
    m = person_model(get_model(False), person)
    rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
    if not m or is_hero(meta) or not (procedural(meta) or any(rec.get("x", {}).get(x, 0) >= 2 for x in EXPERIENCE_TAGS.get(person, ()))):
        return False
    s = score_key(m, meta["id"], person, "movie" if meta["type"] == "movie" else "tv")[0]
    pct = match_pct(m, s, person)
    return pct >= (65 if m.get("cal_reference") == "known" else 70) if pct is not None else s is not None and s > 0


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
        warm = [x for x in out if row_names.mapping(x.get("_row")).get("personal") is True][:5]
        hers = [x for x in out if x not in warm and any(member_pick(x, person) for person in MEMBER_ROWS)]
        other = [x for x in out if x not in warm and not any(member_pick(x, person) for person in MEMBER_ROWS)]
        out = list(warm)
        while hers or other:
            source = hers if hers and (len(out) % 3 == 0 or not other) else other
            x = source.pop(0)
            if source is hers:
                if not row_targets(x):
                    person = next(p for p in MEMBER_ROWS if member_pick(x, p))
                    x = {**x, "_row": {**row_names.mapping(x.get("_row")), "for": [person]},
                         "description": " · ".join(s for s in (t("row.member", person=LABELS[person]), x.get("description")) if s)}
            out.append(x)
    return out


def catalog_mix(metas, own_ids=()):
    groups = {}
    for x in metas:
        groups.setdefault(franchise_key(x), []).append(x)
    is_catalog = lambda x: x.get("poster") and row_names.mapping(x.get("_row")).get("catalog") is True
    mixed = interleave([x for x in metas if is_catalog(x)], [x for x in metas if not is_catalog(x)])
    mixed = [groups[franchise_key(x)].pop(0) for x in mixed]
    return sorted(mixed, key=lambda x: x['id'] not in own_ids) if own_ids else mixed


def polish_row(metas, cap=2, anime=False, universe_cap=4, own_ids=(), catalog=False):
    if own_ids:
        metas = sorted(metas, key=lambda x: x['id'] not in own_ids)
    groups = {}
    for meta in metas:
        key = franchise_key(meta)
        groups.setdefault(key, []).append(meta)
    # Own picks reserve space before filler; originals otherwise lead their sequels.
    for key, group in groups.items():
        group.sort(key=lambda x: (x['id'] not in own_ids, row_facts(x).get("date") or "9999"))
        groups[key] = group[:cap]
    kept = [groups[franchise_key(x)].pop(0) for x in metas if groups[franchise_key(x)]]
    if catalog:
        is_catalog = lambda x: x.get("poster") and row_names.mapping(x.get("_row")).get("catalog") is True
        kept = catalog_mix(kept, own_ids)
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
    if catalog:
        # Catalog matches keep taking turns even without stored background art.
        ready = [x for x in rest if art(x) or is_catalog(x)]
        return front + catalog_mix(ready, own_ids) + [x for x in rest if not art(x) and not is_catalog(x)]
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
    return t("note.episode", episode=episode) if not count or episode > count else f"S{season}E{episode}"



def row_months(cid, lang, built=False):
    source = _feeds.get((cid, lang)) or {}
    names = (source if built else feed(cid, lang)).get("names") or {}
    months = names.get("months")
    current = source.get("names") or {}
    if months is None and any(names.get(k) for k in ("seed", "lane", "mood")) and all(
            not names.get(k) or names[k] == current.get(k) for k in ("seed", "lane", "mood")):
        months = current.get("months")
    if months is None and names.get("lane") and names.get("title"):
        fid = names["lane"]
        name = ((get_model(False) or {}).get("names") or {}).get(fid)
        if name:
            months = theme_months({"kind": "lane", "name": names["title"], "source": [fid, name]})
            if months is None and fid.startswith("k:"):
                months = theme_months({"kind": "lane", "name": names.get("name") or name, "source": [fid, name]})
    return months


def row_list(cid, typ, lang, built=False):
    """A row as served. Mixed rows keep one ordered list ('tv', or movies and series interleaved; Watch Again also
    its AI half): type tv serves it whole, movie / series only that kind."""
    if cid in READY_ROWS:
        from skald import ready
        s = SimpleNamespace(**globals())
        who = _viewer.get() or who_watching()[0]
        metas = ready.cards(s, ready.slot(s, cid, lang, who), who)
        return metas if typ == 'tv' else [x for x in metas if x['type'] == typ]
    source = (_feeds.get((cid, lang)) or {}) if built else feed(cid, lang)
    if not season_fits(row_months(cid, lang, built)):
        return []
    rows = source.get("metas") or {}
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


def because_seeds(p):
    p = adult_profile(p)
    if not p:
        return []
    positive = [e for e in p["entries"] if e["weight"] > 0 and e["signal"] in ("loved", "liked", "watched")]
    # Bulk marks (an import or a player backfill with no real play time) seed only when nothing else can.
    positive = [e for e in positive if "backfill_only" not in e.get("flags", ()) and not (
        (e.get("item") or {}).get("backfilled") and not (e.get("item") or {}).get("at"))] or positive
    recent = [e for e in top_seeds(p, 8) if e in positive]
    favourites = sorted((e for e in positive if e["signal"] in ("loved", "liked")), key=lambda e: -e["weight"])[:8]
    pools = []
    for seeds in (recent, favourites):
        start = day_index() % len(seeds) if seeds else 0
        pools.append(seeds[start:] + seeds[:start])
    if day_index() % 2:
        pools.reverse()
    return interleave(*pools, key=lambda e: (e["key"], e["kind"]))


def because_seed(p):
    return next(iter(because_seeds(p)), None)


def soon_date(meta):
    f = row_facts(meta)
    return date_or_none(row_names.text(f.get("when"))) or date_or_none(f.get("date"))


def upcoming_ok(meta):
    when = soon_date(meta)
    if not when:
        return False
    days = (when - local_now().date()).days
    _, overview = card_parts(meta)
    return -120 <= days <= 365 and row_names.mapping(meta.get("_row")).get("announced") is not True \
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


def row_preferences(cid, who):
    person = "both" if cid == rid('family') else ROW_OWNER.get(cid, who)
    people = [person] if person in PEOPLE else member_people() if person in ("both", "together") else []
    settings = CONFIG["taste"].get("people", {})
    return person, [settings.get(p, {}).get("prefs", {}) for p in people]


def preference_genres(facts, kind):
    genre_ids = facts.get("genre_ids")
    return {TMDB_GENRES[kind][g].casefold() for g in genre_ids if g in TMDB_GENRES[kind]} if genre_ids else {
        GENRE_ALIASES.get(g, g).casefold() for g in facts["g"]}


def genre_excluded(facts, kind, genres):
    names = preference_genres(facts, kind)
    if names & {(g if facts.get('genre_ids') else GENRE_ALIASES.get(g, g)).casefold() for g in genres}:
        return True
    # Series have no native Horror id; named facts still support untyped exclusions.
    native = {g.casefold() for g in TMDB_GENRES[kind].values()}
    return bool({g.casefold() for g in facts['g']} & {g.casefold() for g in genres if g.casefold() not in native})


def runtime_ok(facts, limit):
    runtime = facts.get('runtime')
    return limit is None or type(runtime) in (int, float) and 0 < runtime <= limit


def adult_people():
    return tuple(p for p in member_people() if kids_age(p) is None)


def member_people():
    return tuple(p for p in PEOPLE if p not in guest_people(CONFIG))


def retained_people():
    return (*CONFIG['household']['members'], *ARCHIVED)


def adult_profile(p):
    if not p or len(adult_people()) == len(PEOPLE) and not CONFIG['household'].get('retired_rows'):
        return p
    entries, genres_ = [], {}
    for e in p["entries"]:
        by = set(e.get("by", ())) & set(adult_people())
        if not by and "together" not in e.get("by", ()):
            continue
        opinions = {who: value for who, value in (e.get("opinions") or {}).items() if who in by}
        values = list(opinions.values()) + ([e["shared"]] if e.get("shared") and "together" in e.get("by", ()) else [])
        value = max(values, key=lambda v: v["weight"]) if values else e
        entry = {**e, **value, "opinions": opinions, "by": sorted(by | ({"together"} if "together" in e.get("by", ()) else set()))}
        entries.append(entry)
        for g in e.get("genres", ()):
            genres_[g] = genres_.get(g, 0) + entry["weight"]
    return {**p, "entries": entries, "genres": genres_}


def family_feed(lang):
    if not any(kids_age(p) is not None for p in PEOPLE):
        return None
    p, m = _profiles.get("household") or {}, get_model()
    excluded = p.get("seen", set()) | p.get("negative", set())
    metas, seen = [], set(excluded)
    for kind, stype, gs in (("movie", "movie", "10751|16|12"), ("tv", "series", "10751|16|10759")):
        items, art = with_art(f"discover/{kind}", lang, with_genres=gs, include_adult="false", sort_by="popularity.desc")
        ensure_facets([(f"tmdb:{it['id']}", kind) for it in items])
        for it in items:
            key = f"tmdb:{it['id']}"
            if key in seen:
                continue
            seen.add(key)
            meta = row_meta(it, stype, kind, genres(kind, lang), art.get(it["id"], it), lang=lang)
            if quality_ok(meta) and preference_ok(meta, rid('family'), "both"):
                metas.append(meta)
    metas.sort(key=lambda x: score_key(m, x["id"], "together", "movie" if x["type"] == "movie" else "tv")[0] or 0, reverse=True)
    return {rid('family'): {"tv": metas[:ROW_MAX]}}


def kids_age(person):
    return CONFIG["taste"].get("people", {}).get(person, {}).get("prefs", {}).get("kids_age") if person in PEOPLE else None


def safety_subject(person=None, bound=...):
    if bound is ...:
        context = _safety_context.get()
        if context is None:
            return person
        source, bound = context
        if source == "app" and bound is None:
            bound = _viewer.get() or who_watching()[0]
    return person if bound is None else bound


def title_allowed(key, kind, person=None):
    age = kids_age(safety_subject(person))
    return age is None or age_ok(frec(key, kind), kind, age)


def child_ok(meta, who):
    return title_allowed(meta["id"], "movie" if meta["type"] == "movie" else "tv", who)


def preference_ok(meta, cid, who):
    if cid == rid('family'):
        ages = [kids_age(p) for p in PEOPLE if kids_age(p) is not None]
        if not ages or not age_ok(frec(meta["id"], "movie" if meta["type"] == "movie" else "tv"), "movie" if meta["type"] == "movie" else "tv", min(ages)):
            return False
    if not child_ok(meta, who):
        return False
    if cid == rid('episodes'):
        return True
    person, prefs = row_preferences(cid, who)
    if not any(prefs):
        return True
    facts = row_facts(meta)
    kind = meta["type"]
    names = preference_genres(facts, kind)
    hides = {h for pref in prefs for h in pref.get("hide", [])}
    if (person in PEOPLE or cid == rid('family')) and ("movies" if kind == "movie" else "series") in hides:
        return False
    if ("documentaries" in hides and "documentary" in names or
            "animation" in hides and "animation" in names or "anime" in hides and is_anime(meta)):
        return False
    if any(genre_excluded(facts, kind, pref.get('excluded_genres', {}).get(kind, [])) for pref in prefs):
        return False
    if person in PEOPLE:
        limit = prefs[0].get('runtime', {}).get(kind)
        if not runtime_ok(facts, limit):
            return False
        low, high = prefs[0].get("years", [None, None])
        if low is not None or high is not None:
            year = (facts.get("date") or meta.get("releaseInfo") or "")[:4]
            if not year.isdigit() or low is not None and int(year) < low or high is not None and int(year) > high:
                return False
    return True


def preference_order(metas, cid, who):
    person, prefs = row_preferences(cid, who)
    style = prefs[0].get("discovery", "balanced") if person in PEOPLE else "balanced"
    focus = prefs[0].get("focus", {}) if person in PEOPLE else {}
    weights = prefs[0].get('weights', {}) if person in PEOPLE else {}
    if len(metas) < 2 or style == "balanced" and not any(focus.values()) and not weights:
        return metas
    if style != "balanced":
        m = get_model(False)
        facts = [row_facts(x) for x in metas]
        # shortcut: old feed cards lack popularity; use vote counts until their builder refreshes.
        popularity = [f.get("popularity") if f.get("popularity") is not None else math.log1p(f.get("votes") or 0) for f in facts]
        pmax = max(popularity) or 1
        w_quality, w_pop, w_taste = (.2, .5, .3) if style == "mainstream" else (.6, -.1, .3)
        scores = []
        for x, f, pop in zip(metas, facts, popularity):
            score = score_key(m, x["id"], person, "movie" if x["type"] == "movie" else "tv")[0]
            taste = match_pct(m, score, person) if score is not None else None
            scores.append(w_quality * (f.get("rating") or 0) / 10 + w_pop * pop / pmax + w_taste * (taste if taste is not None else 50) / 100)
        metas = [metas[i] for i in sorted(range(len(metas)), key=lambda i: -scores[i])]
    def focus_score(meta):
        facts = row_facts(meta)
        wanted = focus.get("genres", {}).get(meta["type"], [])
        names = {(g if facts.get("genre_ids") else GENRE_ALIASES.get(g, g)).casefold() for g in wanted}
        rec = frec(meta["id"], "movie" if meta["type"] == "movie" else "tv")
        people_names = {n.casefold() for n in focus.get('people', [])}
        credits = {name.casefold() for field in ('d', 'a') for _, name in rec.get(field) or []}
        return (bool(preference_genres(facts, meta["type"]) & names) + bool(people_names & credits) +
                (facts.get("lang") in focus.get("languages", [])) +
                bool(set(rec.get("oc") or []) & set(focus.get("countries", []))))
    if any(focus.values()):
        metas = sorted(metas, key=focus_score, reverse=True)
    if weights:
        def weight_score(meta):
            facts = row_facts(meta)
            names = preference_genres(facts, meta['type'])
            value = weights.get(meta['type'], 0) + sum(v for g, v in weights.get('genre', {}).items()
                if (g if facts.get('genre_ids') else GENRE_ALIASES.get(g, g)).casefold() in names)
            return max(-1, min(1, value))
        # Soft preferences can move a title by at most three places, never remove it.
        metas = [meta for _, meta in sorted(enumerate(metas), key=lambda pair: pair[0] - 2 * weight_score(pair[1]))]
    return metas


def row_eligible(meta, cid, lang, p, animation=None, *, seed=None):
    viewer, kind = _viewer.get(), "movie" if meta["type"] == "movie" else "tv"
    if not preference_ok(meta, cid, viewer):
        return False
    if viewer in PEOPLE and _taste_accounts():
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
    curated = cid in {rid('foryou'), rid('week'), rid('weekpicks'), rid('leap'), rid('newtoyou')} | set(BECAUSE_ROWS) | set(ROW_OWNER) | set(MOODS_ROWS + LANE_ROWS + STUDIO_ROWS)
    if curated:
        if animation is None:
            animation = row_animation(cid)
        if "Documentary" in row_facts(meta)["g"] or not quality_ok(meta) or not tone_ok(meta, (seed or row_seed(cid, lang)) if cid == rid('leap') or cid in BECAUSE_ROWS else None, animation=animation, person=ROW_OWNER.get(cid)):
            return False
        d = soon_date(meta)
        if d and d > local_now().date():
            return False
    if cid in BECAUSE_ROWS:
        return because_ok(meta, seed or row_seed(cid, lang))
    return cid != rid('top10') or quality_ok(meta)


def row_pool(lang):
    pool = {}
    for cid in DEDUPE_ORDER:
        if cid in LAB_ROWS or cid in READY_ROWS and not row_enabled(cid):
            continue
        # A lane's pinned bench must not change the refill supply of rows above it.
        built = cid in LANES and bool(feed(cid, lang).get("_bench"))
        for typ in (("movie", "series") if cid in (rid('top10'), rid('toprated')) else ("series",) if cid in (rid('episodes'), rid('airing'), rid('nextseason')) else ("tv",)):
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
    if cid in MOODS_ROWS + LANE_ROWS + [rid('leap')] and (meta.get("_row") or {}).get("theme") in (
            names.get("lane"), names.get("mood"), names.get("seed")) and (meta.get("_row") or {}).get("theme"):
        if any(x["id"] == meta["id"] for typ, xs in (feed(cid, lang).get("metas") or {}).items()
               if typ in ("movie", "series", "tv", "pool") for x in xs):
            return True
    if cid in (rid('foryou'), rid('week')) or cid in ROW_OWNER:
        if "Documentary" in row_facts(meta)["g"]:
            return False
        return week_ok(meta, seeds) if cid == rid('week') else cid == rid('foryou') or member_pick(meta, ROW_OWNER[cid])
    if cid in BECAUSE_ROWS:
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
    if not season_fits(row_months(cid, lang, built)) or not season_fits((source.get("names") or {}).get("months")):
        return []
    same = all(not names.get(k) or names[k] == (source.get("names") or {}).get(k) for k in ("seed", "lane", "mood", "who"))
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
    candidates = [x for x in own + ([] if cid in THEMED_ROWS else list(row_pool(lang))) if x["id"] not in ids
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
        more = more[:ROW_MAX]  # R1 ranks a bounded refill after the animation guard thins the member's own pool.
    # Own refills keep their context; borrowed cards carry only their overview.
    more = [x if x["id"] in own_ids else {**x, "description": card_parts(x)[1], "_borrowed": True} for x in more]
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
            for person in adult_people() if m["nexp"].get(person, 0) < 20]


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
    # A weaker Week tail must not consume the because and personal shelves.
    because = [cid for cid in BECAUSE_ROWS if row_enabled(cid)]
    protected = {x["id"] for cid in (*because, *ROW_OWNER) for x in row_list(cid, "tv", lang)}
    protected.update(x["id"] for cid in because for x in served_row(cid, "tv", lang, p, ids, masks))
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
        tail.append({**x, "description": card_parts(x)[1], "_borrowed": True})
    return tail


class RowAbove(set):
    def __init__(self, ids):
        super().__init__(ids)
        self.deep = set(ids)
        self.lanes = set()
        self.franchises = {}


def mark_above(above, cid, metas):
    if not isinstance(above, RowAbove):
        above = RowAbove(above)
    above.deep.update(x["id"] for x in metas)
    for key in {franchise_key(x) for x in metas}:
        above.franchises[key] = above.franchises.get(key, 0) + 1
    above.update(x["id"] for x in (metas if cid in MOODS_ROWS + LANES else metas[:ROW_WINDOW]))
    if cid in LANES and isinstance(metas, LaneRow) and metas:
        above.lanes.add(metas.names.get("lane"))
    return above


def row_variety(metas, cid, lang, franchises, who=None, p=None):
    if cid not in THEMED_ROWS | set(ROW_OWNER) | {rid('foryou'), rid('week'), rid('newtoyou'), rid('digital'), rid('toprated'), rid('airing'), rid('nextseason')} or who == GUEST or kids_age(safety_subject(who)) is not None:
        return metas
    source = row_list(cid, 'tv', lang)
    worlds = {universe_key(x) for x in source}
    hero_theme = cid in LANE_ROWS + STUDIO_ROWS and bool(source) and (
        all(is_hero(x) for x in source) or len(worlds) == 1 and None not in worlds)
    heroes, kept = 0, []
    for x in metas:
        if cid in (rid('airing'), rid('nextseason')) and row_member(x, p or {}, 'seen'):
            kept.append(x)
            continue
        hero = is_hero(x)
        if franchises.get(franchise_key(x), 0) >= HOME_FRANCHISE_ROWS or hero and not hero_theme and heroes >= ROW_HERO_MAX:
            rowplan.removed(cid, [x], [], 'franchise_cap' if franchises.get(franchise_key(x), 0) >= HOME_FRANCHISE_ROWS else 'superhero_cap')
            continue
        heroes += hero
        kept.append(x)
    return kept


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
    for person in adult_people():
        n = m["nexp"].get(person, 0)
        own = pool_pct({x["id"]: s if (s := score_key(m, x["id"], person, "movie" if x["type"] == "movie" else "tv")[0]) is not None
                        else house[x["id"]] for x in metas})
        seeds = explicit_seeds(person) if n < 20 else []
        fallback = pool_pct({x["id"]: seed_knn(seeds, x["id"], 1) for x in metas}) if seeds else house_pct
        lam = min(1, n/40)
        people.append({k: lam * own[k] + (1-lam) * fallback[k] for k in house})
    if not people:
        return {k: 0 for k in house}
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


def toprated_scores(metas, who):
    m = get_model(False)
    person = who if who in PEOPLE else 'together'
    scores = {}
    for x in metas if who != GUEST else ():
        kind = 'movie' if x['type'] == 'movie' else 'tv'
        rec = frec(x['id'], kind)
        if not rec:
            # shortcut: new chart titles have only genres, use their full facets once cached.
            rec = {'g': [TMDB_GENRES[x['type']][g] for g in (x.get('_row') or {}).get('genre_ids') or []
                         if g in TMDB_GENRES[x['type']]] or list(row_facts(x)['g'])}
        scores[x['id'], x['type']] = score_key(m, x['id'], person, kind, rec=rec)[0]
    taste = pool_pct(scores)
    out = {}
    for x in metas:
        key = x['id'], x['type']
        rating = row_facts(x).get('rating')
        out[key] = dict(rating=rating, taste_match=100*taste[key] if scores.get(key) is not None else None,
                        blend=.65*taste.get(key, 0) + .35*(rating or 0)/10)
    return out


def served_row(cid, typ, lang, p, above, masks, who=None):
    token = _viewer.set(who) if who is not None else None
    try:
        if not row_scope_ok(cid):
            rowplan.observe(cid, code='unsafe')
            return []
        if cid in READY_ROWS:
            from skald import ready
            s = SimpleNamespace(**globals())
            release = ready.slot(s, cid, lang, who, masks)
            metas = ready.cards(s, release, who)
            metas = metas if typ == 'tv' else [x for x in metas if x['type'] == typ]
            rowplan.observe(cid, metas, None if metas else 'not_today', typ=typ)
            return metas
        names = feed(cid, lang).get("names") or {}
        if cid.startswith(rid('lab_')):
            scope = getattr(_scoping, 'scope', None)
            person = scope if scope is not None else who
            if who == GUEST or kids_age(safety_subject(who)) is not None:
                rowplan.observe(cid, code='unsafe')
                return []
            selected = lab.select(DATA, CONFIG, time.time(), person)
            if cid not in {rid('lab_'+i['id']) for i in selected}:
                rowplan.observe(cid, code='not_today')
                return []
            raw = row_list(cid, typ, lang)
            blocked = masks[0] | (masks[2] if len(masks) > 2 else set())
            corrected = [x for x in raw if x['id'] not in blocked
                         and catalog_drop(x, p, cid, person) is None]
            rowplan.filtered(cid, raw, corrected, p, masks, lang, False)
            metas = [x for x in corrected if x['id'] not in getattr(above, 'deep', above)]
            rowplan.removed(cid, corrected, metas, 'overlap')
            metas = preference_order(metas, cid, who)
            rowplan.observe(cid, metas, None if len(metas) >= ROW_MIN else 'too_few_own', typ=typ)
            return metas[:20] if len(metas) >= ROW_MIN else []
        if not season_fits(row_months(cid, lang)):
            rowplan.observe(cid, code='not_today')
            return LaneRow([], names) if cid in LANE_ROWS else []
        franchises = getattr(above, 'franchises', {})
        if cid in (rid('week'), rid('weekpicks')):
            metas = []
            blocked = masks[0] | (masks[2] if len(masks) > 2 else set())
            for x in row_list(cid, typ, lang):
                scopes = x.get('_week') or {}
                if cid == rid('weekpicks'):
                    scopes = {person: weekpick_reason(person, (x.get('_week_anchors') or {}).get(person))
                              for person in scopes if person in (*adult_people(), 'together') and
                              (who not in PEOPLE or person in (who, 'together')) and
                              (who != GUEST and who not in guest_people(CONFIG) and kids_age(who) is None or person == 'together')}
                if not scopes or x.get('_week_start') != week_start() or who in PEOPLE and not ({who, 'together'} & scopes.keys()):
                    continue
                if x['id'] in blocked or not row_eligible(x, cid, lang, p):
                    continue
                if cid == rid('weekpicks') and (x['id'] in above or catalog_drop(x, p, cid, who) or any(
                        pick['id'] == x['id'] for r in _learn.get('week', []) if r['week'] == week_start() for pick in r['picks'])):
                    continue
                person = who if who in scopes else 'together' if 'together' in scopes else next(iter(scopes))
                card = {**x, '_week': scopes, 'description': scopes[person] + ' · ' + ((x.get('_row') or {}).get('overview') or '')}
                if cid == rid('weekpicks'):
                    card['_week_anchors'] = {person: (x.get('_week_anchors') or {}).get(person) for person in scopes}
                    card['_row'] = {**(x.get('_row') or {}), 'weekpick_note': scopes[person]}
                metas.append(card)
            if cid == rid('weekpicks') and who in adult_people():
                metas.sort(key=lambda x: who not in x['_week'])
            return metas if len(metas) >= (1 if cid == rid('weekpicks') else ROW_MIN) else []
        if cid in (rid('digital'), rid('toprated')):
            raw = row_list(cid, typ, lang)
            if cid == rid('toprated') and who in PEOPLE and p.get('entries'):
                entries = person_entries(p, who)
                known = {e['key'] for e in p['entries']}
                p = {**p, 'entries': entries,
                     'seen': set(p.get('seen', ())) - known | {e['key'] for e in entries},
                     'negative': set(p.get('negative', ())) - known | {e['key'] for e in entries if e['weight'] < 0}}
            blocked = masks[0] | (masks[2] if len(masks) > 2 else set())
            corrected = [x for x in raw if x['id'] not in blocked
                     and not row_member(x, p, 'seen') and not row_member(x, p, 'negative')
                     and quality_ok(x) and preference_ok(x, cid, _viewer.get())]
            rowplan.filtered(cid, raw, corrected, p, masks, lang, False)
            metas = [x for x in corrected if x['id'] not in getattr(above, 'deep', above)]
            rowplan.removed(cid, corrected, metas, 'overlap')
            if cid == rid('toprated'):
                scores = toprated_scores(metas, _viewer.get())
                if _viewer.get() != GUEST:
                    metas = sorted(metas, key=lambda x: -scores[x['id'], x['type']]['blend'])
            metas = preference_order(metas, cid, _viewer.get())
            metas = row_variety(metas, cid, lang, franchises, who, p)
            if cid == rid('toprated'):
                rowplan.ranked(cid, metas[:20] if len(metas) >= ROW_MIN else [], scores)
            rowplan.observe(cid, metas[:20], None if len(metas) >= ROW_MIN else 'too_few_own',
                            typ=typ if len(metas) >= ROW_MIN else None)
            return metas[:20] if len(metas) >= ROW_MIN else []
        if cid == rid('family'):
            scope = getattr(_scoping, "scope", None)
            if scope in PEOPLE and kids_age(scope) is None:
                return []
            raw = row_list(cid, typ, lang)
            metas = [x for x in raw if x["id"] not in masks[0]
                     and (len(masks) < 3 or x["id"] not in masks[2]) and x["id"] not in p.get("seen", ())
                     and x["id"] not in p.get("negative", ()) and preference_ok(x, cid, who)]
            rowplan.filtered(cid, raw, metas, p, masks, lang, False)
            rowplan.observe(cid, metas[:20], None if len(metas) >= ROW_MIN else 'too_few_own', typ=typ)
            return metas[:20] if len(metas) >= ROW_MIN else []
        if len(masks) > 2 and masks[2] and cid not in VETO_EXEMPT: masks = (masks[0] | masks[2], masks[1])
        if cid not in MOODS_ROWS + LANE_ROWS:
            above = getattr(above, "deep", above)
        metas = row_list(cid, typ, lang)
        own_order = {x['id']: i for i, x in enumerate(metas)} if cid == rid('foryou') else {}
        own_ids = set(own_order) | {x['id'] for x in source_metas(cid, lang)} if cid == rid('foryou') else set()
        if cid == rid('rewatch') and metas:
            entries = p.get("entries", [])
            if entries and who in PEOPLE: entries = person_entries(p, who)
            watched = {(e["key"], e["kind"]) for e in entries
                       for value in ((e,) if who in PEOPLE else (e, *(e.get("opinions") or {}).values()))
                       if value["signal"] in ("loved", "liked", "watched") or value["item"].get("played")
                       or value["item"].get("completed") or value["item"].get("rating") is not None
                       or (value["item"].get("episodes_played") or 0) > 0 or (value["item"].get("progress") or 0) >= .7}
            metas = [x for x in metas if (x["id"], "movie" if x["type"] == "movie" else "tv") in watched]
        names = row_names.mapping(feed(cid, lang).get("names"))
        if cid in LANE_ROWS and not maker_view(names, p, who):
            rowplan.observe(cid, code='unsafe')
            return LaneRow([], names)
        animation = row_animation(cid)
        if cid == rid('episodes') and len(metas) < 12:
            catchup = [x for x in row_list(rid('catchup'), "tv", lang) if x["type"] == "series"]
            catchup.sort(key=lambda x: row_names.mapping(x.get("_row")).get("season_out") is not True)
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
                    x = {**x, "name": x["name"] + (t("item.movie") if x["type"] == "movie" else t("item.series"))}
            if row_member(x, p, "seen") and cid in (rid('airing'), rid('nextseason'), rid('upcoming')):
                notes, overview = card_parts(x)
                note = next((n for n in notes if n not in (t("match.strong"), t("match.good"), t("match.wildcard"))), "")
                x = {**x, "description": " · ".join(s for s in (t("note.season.new") + note, overview) if s)}
            if cid in (rid('catchup'), rid('episodes')):
                counts = (x.get("_row") or {}).get("counts") or {}
                x = {**x, "description": re.sub(r"S(\d+)E(\d+)", lambda m: m[0] if int(m[2]) <= counts.get(m[1], 0)
                                                else t("note.episode", episode=m[2]), x.get("description") or "")}
            if x["id"] not in masks[0] and row_eligible(x, cid, lang, p, animation) and (cid not in LANE_ROWS or row_related(x, cid, lang)):
                corrected.append(x)
        # Chart ranks survive; all other shelves use the actual served rows above.
        rowplan.filtered(cid, metas, corrected, p, masks, lang, animation)
        metas = corrected if cid == rid('top10') else [x for x in corrected if x["id"] not in above]
        rowplan.removed(cid, corrected, metas, 'overlap')
        metas = fill_row(metas, cid, lang, p, above, masks)
        if cid not in MIXED | set(LANE_BENCH):
            metas = [x for x in metas if x["type"] == typ]
        if cid == rid('foryou'):
            reserved = {x["id"] for x in row_list(rid('week'), "tv", lang)}
            metas = [x for x in metas if x["id"] not in reserved]
        if cid in BECAUSE_ROWS:
            metas = because_rank(metas, row_seed(cid, lang))
        if cid in ROW_OWNER:
            scores = member_scores(metas, ROW_OWNER[cid])
            metas.sort(key=lambda x: scores[x["id"]], reverse=True)
        if who in PEOPLE and cid in PERSON_ROWS | set(LANE_BENCH): metas = person_order(metas, who)
        metas = preference_order([x for x in metas if preference_ok(x, cid, _viewer.get())], cid, _viewer.get())
        if cid in PERSON_ROWS:
            annotated, n = [], 0
            for x in metas:
                why = memory_why(x["id"], "movie" if x["type"] == "movie" else "tv", "together" if who == "both" else who) if n < MEMORY_WHY_ROW else None
                if why:
                    x, n = {**x, "_memory": why}, n+1
                annotated.append(x)
            metas = annotated
        title = row_names.text(names.get("title")).casefold()
        # Legacy pins stay usable until a background theme pass supplies the animation policy.
        legacy_lane = cid in LANE_ROWS and not any(type(row_names.mapping(x.get("_row")).get("anime_lane")) is bool for x in metas)
        if cid in LANE_ROWS and not lane_allowed(metas, title, legacy_lane):
            metas = []
        anime = (legacy_lane or cid in (rid('airing'), rid('nextseason')) or any(row_names.mapping(x.get("_row")).get("anime_lane") is True for x in metas) or
                 cid in BECAUSE_ROWS and 'Animation' in frec(row_seed(cid, lang), profile_kind(row_seed(cid, lang))).get('g', ()))
        metas = balance_row(metas, anime=anime)
        lid = f"lane:{names['lane']}" if cid in LANE_ROWS and names.get("lane") else None if cid in LANE_ROWS else learn_id(cid, lang)
        tired = tired_keys({lid}) if lid and cid not in (rid('episodes'), rid('upcoming'), rid('catchup')) else {}
        metas = [x for x in metas if x["id"] not in tired] + [x for x in metas if x["id"] in tired]
        # A universe is the point of some lanes; keep the franchise cap within it.
        before_polish = metas
        metas = polish_row(metas, cap=1 if cid == rid('upcoming') else 2, anime=anime,
            universe_cap=None if cid in LANE_ROWS else 4, own_ids=own_ids,
            catalog=cid in set(ROW_OWNER) | PERSON_ROWS | {rid('leap'), rid('newtoyou')})
        rowplan.polished(cid, before_polish, metas, 1 if cid == rid('upcoming') else 2)
        if len(metas) < 5:
            metas = balance_row(metas, anime=anime)
        if cid in BECAUSE_ROWS:
            before_polish = metas
            metas = polish_row(because_balance(metas, row_seed(cid, lang)), anime=anime)
            rowplan.polished(cid, before_polish, metas, 2)
        if cid in ROW_OWNER:
            metas = member_balance(metas, ROW_OWNER[cid], strict=True)
        if who in PEOPLE and cid == rid('foryou'):
            metas = person_animation_sink(memory_reserve(metas, who), who)
        if (cid in MOODS_ROWS or cid == rid('top10') and kids_age(safety_subject(who)) is not None) and len(metas) < ROW_MIN:
            rowplan.observe(cid, metas, 'too_few_own')
            return []
        limit = 10 if cid == rid('top10') else 12 if cid == rid('episodes') else 24 if cid == rid('foryou') else 20
        if cid != rid('foryou'):
            metas = row_variety(metas, cid, lang, franchises, who, p)
            metas = metas[:limit]
        if cid in MOODS_ROWS + LANE_ROWS:
            if not anime:
                cap = int(sum(not is_anime(x) for x in metas) * 2 / 3)
                allowed = {x["id"] for x in [x for x in metas if is_anime(x)][:cap]}
                metas = [x for x in metas if not is_anime(x) or x["id"] in allowed]
            if len(metas) < ROW_MIN and cid not in LANE_ROWS:
                rowplan.observe(cid, metas, 'too_few_own')
                return []
        if cid in LANE_ROWS and not lane_allowed(metas, title, legacy_lane):
            metas = []
        if cid in LANE_ROWS and len(metas) < ROW_MIN:
            rowplan.observe(cid, metas, 'too_few_own')
            return LaneRow([], names)
        if cid in MOODS_ROWS:
            metas = mood_heroes(metas)
        metas = rotate_row(metas, cid)
        metas = remote_order(metas, masks)
        metas = person_animation_sink(metas, who) if who in PEOPLE and cid in PERSON_ROWS | set(LANE_BENCH) else metas
        if cid == rid('foryou'):
            start = 5 if any(row_names.mapping(x.get('_row')).get('catalog') is True for x in row_list(cid, typ, lang)) else 0
            metas[start:] = sorted(metas[start:], key=lambda x: (x['id'] not in own_ids, own_order.get(x['id'], ROW_MAX),
                -(score_key(get_model(False), x['id'], kind='movie' if x['type'] == 'movie' else 'tv')[0] or 0)))
            if start:
                metas[start:] = catalog_mix(metas[start:], own_ids)
            metas = row_variety(metas, cid, lang, franchises, who, p)
            trending_only = {tuple(x) for x in names.get('trending_only', [])}
            kept, trending = [], 0
            for x in metas:
                is_trending = (x['id'], 'movie' if x['type'] == 'movie' else 'tv') in trending_only
                trending += is_trending
                if not is_trending or trending <= 2:
                    kept.append(x)
            head, tail = kept[:start], kept[start:]
            own = remote_order([x for x in tail if x['id'] in own_ids], masks)
            more = remote_order([x for x in tail if x['id'] not in own_ids], masks)
            metas = (head + own + more[:max(0, 12-len(head)-len(own))])[:limit]
        if cid in THEMED_ROWS and len(metas) < ROW_MIN:
            rowplan.observe(cid, metas, 'too_few_own')
            metas = []
        if metas:
            rowplan.observe(cid, metas, typ=typ)
        return LaneRow(metas, names) if cid in LANE_ROWS else metas
    finally:
        if token is not None: _viewer.reset(token)


def _row_metas_uncached(cid, typ, lang, who=None):
    """Reference composition for non-home rows and cache identity checks."""
    who = who_watching()[0] if who is None else who
    if cid in DEDUPE_ORDER:
        return build_home(lang, _profiles.get('household') or {}, row_masks(who), who).get((cid, typ), [])
    masks = row_masks(who)
    p = _profiles.get("household") or {}
    above = set()
    for other in DEDUPE_ORDER:
        if not row_enabled(other):
            continue
        types = ("movie", "series") if other in (rid('top10'), rid('toprated')) else ("movie",) if other == rid('digital') else ("tv",) if other in MIXED else ("series",)
        for ot in types:
            metas = served_row(other, ot, lang, p, above, masks, who=who)
            if other == cid and (ot == typ or other in MIXED):
                selected = metas if typ == "tv" or other not in MIXED else [x for x in metas if x["type"] == typ]
                return human_cards(LaneRow(selected, metas.names) if isinstance(metas, LaneRow) else selected, who)
            above = mark_above(above, other, metas)
    return human_cards(served_row(cid, typ, lang, p, above, masks, who=who), who)


_HOME_CACHE_ENABLED = True
_home_cache = {}
_home_lock = threading.Lock()


def build_home(lang, p, masks, who, *, caps=None):
    lab_catalogs()
    above, rows = set(), {}
    lean, counts, docs = default_rows(), {'moods': 0, 'lanes': 0}, None
    caps = rowplan.family_caps(CONFIG) if caps is None else caps
    capped = rowplan.capped(CONFIG)
    for cid in DEDUPE_ORDER:
        if cid in LAB_ROWS or not row_enabled(cid):
            continue
        types = ("movie", "series") if cid in (rid('top10'), rid('toprated')) else ("movie",) if cid == rid('digital') else ("tv",) if cid in MIXED else ("series",)
        for typ in types:
            group = 'moods' if cid in MOODS_ROWS else 'lanes' if cid in LANES else None
            if lean and cid in DOCS:
                if docs is None:
                    candidates = [(other, served_row(other, 'tv', lang, p, above, masks, who=who)) for other in DOCS if row_enabled(other)]
                    candidates = [(other, xs) for other, xs in candidates if len(xs) >= ROW_MIN]
                    docs = dict([candidates[day_index() % len(candidates)]]) if candidates else {}
                metas = docs.get(cid, [])
                if not metas:
                    rowplan.observe(cid, code='not_today' if cid in dict(candidates) else None)
            elif capped and group and counts[group] >= caps['mood' if group == 'moods' else 'lane']:
                rowplan.observe(cid, code='family_cap')
                metas = []
            else:
                metas = served_row(cid, typ, lang, p, above, masks, who=who)
            if group and metas:
                counts[group] += 1
            if metas:
                rowplan.observe(cid, metas, typ=typ)
            for out_type in MIX if cid in MIXED else (typ,):
                selected = metas if out_type == typ else [x for x in metas if x["type"] == out_type]
                rows[cid, out_type] = human_cards(LaneRow(selected, metas.names) if isinstance(metas, LaneRow) else selected, who)
            above = mark_above(above, cid, metas)
    scope = getattr(_scoping, 'scope', None)
    selected = lab.select(DATA, CONFIG, time.time(), scope if scope is not None else who)
    ids = {rid('lab_'+i['id']) for i in selected}
    for cid in LAB_ROWS:
        if cid not in ids:
            rowplan.observe(cid, code='not_today')
    for idea in selected:
        cid = rid('lab_'+idea['id'])
        if not row_enabled(cid):
            continue
        metas = served_row(cid, 'tv', lang, p, above, masks, who=who)
        for typ in MIX:
            rows[cid, typ] = human_cards(metas if typ == 'tv' else [x for x in metas if x['type'] == typ], who)
        above = mark_above(above, cid, metas)
    return rows


_prefs_stamp = None
_people_settings = {k: copy.deepcopy(CONFIG['household'].get(k))
                    for k in ('members', 'guests', 'member_rows', 'labels', 'ages', 'retired', 'retired_rows', 'away')}


def refresh_preferences():
    global _prefs_stamp, _pin_epoch, _people_settings, PEOPLE, LABELS, MEMBER_ROWS, ROW_OWNER, ARCHIVED, TRAKT_PERSON
    global DEFAULT_TASTE, PAIR_LABEL, EXPERIENCE_TAGS, MEMBER_NAMES, MEMBER_COUNT
    if household_erased(DATA):
        if _prefs_stamp != 'erased':
            forget_person_caches()
            _prefs_stamp = 'erased'
        return
    if CONFIG_SOURCE not in ("file", "env"):
        return
    path = os.environ.get("SKALD_CONFIG") or os.path.join(DATA, CONFIG_NAME)
    plays_before = copy.deepcopy(CONFIG.get('plays'))
    with _home_lock:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            if CONFIG_SOURCE == "env":
                return  # Read-only tools can use an unpublished first-run config.
            raise
        stamp = (path, st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp == _prefs_stamp:
            return
        with open(path, encoding="utf-8") as fh:
            cfg = validate_config(json.load(fh, parse_constant=invalid_json), data=DATA)
        if 'plays' in cfg:
            CONFIG['plays'] = cfg['plays']
        else:
            CONFIG.pop('plays', None)
        if 'lab' in cfg:
            CONFIG['lab'] = cfg['lab']
        else:
            CONFIG.pop('lab', None)
        old_people, old_rows = PEOPLE, set(ROW_OWNER)
        people_changed = any(value != cfg['household'].get(k) for k, value in _people_settings.items())
        if people_changed:
            hh = cfg['household']
            PEOPLE = tuple(p for p in hh['members'] if p not in hh.get('retired', []))
            SCOPES.clear()
            SCOPES.update({**{p: p for p in PEOPLE}, 'together': 'both'})
            LABELS, ARCHIVED = hh['labels'], hh['archived']
            PAIR_LABEL = LABELS['pair'].removeprefix('you ').title()
            MEMBER_ROWS = tuple(p for p in hh['member_rows'] if p in PEOPLE)
            ROW_OWNER = {rid(p): p for p in MEMBER_ROWS}
            TRAKT_PERSON = hh['trakt_member']
            EXPERIENCE_TAGS = {p: set(xs) for p, xs in hh['experiences'].items()}
            MEMBER_NAMES = ' and '.join(LABELS[p] for p in PEOPLE if p not in guest_people(cfg))
            MEMBER_COUNT = ('one', 'two', 'three', 'four', 'five', 'six')[len(PEOPLE)-1]
            SHELF_ROWS[:] = [rid(f'shelf_{p}_{n}') for p in (*PEOPLE, 'together') for n in range(1, 11)]
            DEDUPE_ORDER[:] = [cid for cid in DEDUPE_ORDER if cid not in old_rows]
            index = DEDUPE_ORDER.index(rid('rewatch'))
            DEDUPE_ORDER[index:index] = ROW_OWNER
            for collection in (MIXED, REMOTE_ROWS, API_ROWS):
                collection.difference_update(old_rows)
                collection.update(ROW_OWNER)
            MIXED.update(SHELF_ROWS)
            for cid in old_rows:
                GUEST_ROW_POLICY.pop(cid, None)
            GUEST_ROW_POLICY.update(dict.fromkeys(ROW_OWNER, False))
            def schema_people(schema):
                if isinstance(schema, dict):
                    return {k: schema_people(v) for k, v in schema.items()}
                if isinstance(schema, list):
                    return [schema_people(v) for v in schema]
                if isinstance(schema, tuple):
                    if all(p in schema for p in old_people):
                        return (*PEOPLE, *(v for v in schema if v not in old_people))
                    return tuple(schema_people(v) for v in schema)
                return schema
            for spec in DECISIONS.values():
                for key in ('inp', 'out'):
                    spec[key] = schema_people(spec[key])
            _model.update(sig=None, m=None)
            _remote_cache.clear()
            _person_models.clear()
        CONFIG['household'] = cfg['household']
        CONFIG['household'].setdefault('shelves', 2)
        CONFIG['connectors'] = cfg['connectors']
        CONFIG['taste'] = cfg['taste']
        for person in PEOPLE:
            CONFIG['taste'].setdefault('people', {}).setdefault(person, {})
        DEFAULT_TASTE = cfg['taste']
        if 'metrics' in cfg:
            CONFIG['metrics'] = cfg['metrics']
        else:
            CONFIG.pop('metrics', None)
        if not row_metrics.settings(CONFIG)['enabled']:
            row_metrics.disable(DATA)
        _home_cache.clear()
        _pin_epoch += 1
        _prefs_stamp = stamp
        _people_settings = {k: copy.deepcopy(cfg['household'].get(k)) for k in _people_settings}
        if '_command_context' in globals():
            command_context()
    if plays_before != CONFIG.get('plays'):
        connectors_load(time.time())
    from skald import plays
    if os.path.exists(os.path.join(DATA, plays.WATCHING_DAYS)):
        plays.prune_watching_days(DATA, CONFIG, time.time())


def row_metas(cid, typ, lang, who=None):
    metas = _row_metas_current(cid, typ, lang, who)
    if _health is not None:
        from skald.health import key
        scope = getattr(_scoping, 'scope', None)
        with _health_lock:
            row = key('row', (cid, typ, lang, scope, who))
            _health_served_rows[row] = len(metas)
            _health_row_sources[row] = cid
    return metas


def _row_metas_current(cid, typ, lang, who=None):
    refresh_preferences()
    lab_catalogs()
    if not row_enabled(cid) or cid in THEMED_ROWS and not feed(cid, lang):
        return []
    if cid in SHELF_ROWS:
        return shelf_items(cid, typ, lang, who)
    if not _HOME_CACHE_ENABLED or cid not in DEDUPE_ORDER or typ not in (MIX if cid in MIXED else ("movie", "series") if cid in (rid('top10'), rid('toprated')) else ("movie",) if cid == rid('digital') else ("series",)):
        if who is None:
            return _row_metas_uncached(cid, typ, lang)
        return _row_metas_uncached(cid, typ, lang, who=who)
    who = who_watching()[0] if who is None else who
    return _home_rows(lang, who).get((cid, typ), [])


def _home_rows(lang, who):
    with _home_lock:
        lab_catalogs()
        masks = row_masks(who)
        p, m = _profiles.get("household"), get_model(False)
        scope = getattr(_scoping, "scope", None)
        shown = _shown if scope is None else _shown_scoped.get(scope, {})
        with _feeds_lock:
            generation = tuple((source, k, id(v)) for source, store in enumerate((_feeds, shown))
                               for k, v in sorted(store.items()) if k[1] == lang)
            refs = [v for store in (_feeds, shown) for k, v in store.items() if k[1] == lang]
        key = (lang, who, scope, tuple(frozenset(s) for s in masks), id(m), generation,
               p.get("hash") if p else None, id(p), int(time.time() // 60), lab_stamp(), lab.paused(CONFIG))
        if key not in _home_cache:
            rows = build_home(lang, p or {}, masks, who)
            if len(_home_cache) >= 8:
                _home_cache.pop(next(iter(_home_cache)))
            # Retain source identities so replacements cannot reuse an older cache key.
            _home_cache[key] = (rows, [p, m, *refs])
        return _home_cache[key][0]


def stats():
    with _usage_lock:
        _usage_roll()
        u = json.loads(json.dumps(_usage))
    u["caps"] = {**AI_CAPS, "monthly_usd": ai_monthly_usd()}
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
    u["embedding"] = emb_status()
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
IMPORT_TRUST = {"pmdb": .8, "trakt": 1.0, "letterboxd": 1.0, "imdb": 1.0, "netflix": .8,
                "chrome": .4, "youtube": .4, "plain": .8}
IMPORT_MAX = 20000
IMPORT_STRENGTH = {"opened": 0, "interest": 1, "watched": 2, "liked": 3, "loved": 4, "abandoned": 4, "disliked": 5}
_imports_lock = threading.Lock()
_imports_cache = {}


def validate_import(body, *, stored=False):
    fields = {"t", "id", "key", "kind", "title", "year", "who", "action", "src", "conf", "n"}
    required = fields - {"t", "id", "year", "n"}
    if not isinstance(body, dict) or set(body) - fields or not required <= set(body):
        raise ValueError("invalid import fields")
    e = {"year": "", "n": 1, **body}
    if not isinstance(e["key"], str) or not re.fullmatch(r"tmdb:[0-9]{1,9}", e["key"]) \
            or e["key"] == "tmdb:0":
        raise ValueError("invalid import key")
    for field, allowed in (("kind", ("movie", "tv")), ("who", (*(retained_people() if stored else PEOPLE), "both")),
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
                    e = validate_import(json.loads(line), stored=True)
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
            for name in ("history-summary.json", "history-forgotten.json"):
                try:
                    other = os.stat(os.path.join(DATA, name))
                    stamp += (other.st_mtime_ns, other.st_size)
                except FileNotFoundError:
                    pass
            if _imports_cache.get("stamp") != stamp:
                from skald.history import combine, filter_forgotten
                combined = combine(DATA, "imports.jsonl", filter_forgotten(DATA, CONFIG, "imports.jsonl", read_imports()))
                _imports_cache.update(stamp=stamp, state={"active": active_imports(combined)})
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
    with history_locked(DATA):
        return _save_imports(events)


def _save_imports(events):
    from skald.history import filter_forgotten
    events = filter_forgotten(DATA, CONFIG, "imports.jsonl", events)
    path = os.path.join(DATA, "imports.jsonl")
    tmp = path + ".tmp"
    try:
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
    from skald.history import combine, folded_imports
    incoming = [validate_import(e) for e in bodies]  # validate the whole batch before writing.
    if keep_ids:
        with history_locked(DATA), _imports_lock:
            events = read_imports()
            folded = folded_imports(DATA)
            ids = {e["id"]: e for e in events + folded}
            scoped_ids = {(e["id"], e["who"]): e for e in events + folded}
            folded_claims = {(e["id"], e["who"]) for e in folded}
            latest = {(e["key"], e["who"], e["src"]): e for e in active_imports(combine(DATA, "imports.jsonl", events))}
            added, duplicates, conflicts = [], 0, []
            for i, e in enumerate(incoming):
                old = scoped_ids.get((e.get("id"), e["who"])) or ids.get(e.get("id"))
                e = {"t": old["t"] if old else datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
                     "id": uuid.uuid4().hex, **e}
                if old is not None:
                    comparable = e
                    if (old["id"], old["who"]) in folded_claims:
                        # Folded evidence retains only the month of the original date.
                        comparable = {**e, "t": e["t"][:7] + "-01"}
                    if old == comparable:
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
                scoped_ids[(e["id"], e["who"])] = e
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
    with history_locked(DATA), _imports_lock:
        events = read_imports()
        latest = {(e["key"], e["who"], e["src"]): e for e in active_imports(combine(DATA, "imports.jsonl", events))}
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
    with history_locked(DATA), _imports_lock:
        from skald.history import folded_imports, remove_imports
        events = read_imports()
        target = next((e for e in events + folded_imports(DATA) if e["id"] == event_id), None)
        if target is None:
            raise ValueError("unknown import id")
        save_imports([e for e in events if e["id"] != event_id])
        remove_imports(DATA, {event_id})
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
    for row in sorted((e["key"], person, e["weight"]) for e in entries for person in e["by"] if person in member_people()):
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
                            or not who_ok(e["who"], "both", "off", GUEST, *retained_people()) or e["via"] not in ("phone", "api") \
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
        targets = [bound if body['who'] == 'off' else body['who']]
        if active:
            targets.append(active['who'])
        search_commands.guest_scope(command_context(), bound, {'people': targets})
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
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
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
    if bound == GUEST or kids_age(safety_subject(bound)) is not None:
        return {"active": active, "last": None}
    last = None
    for w in reversed(windows):
        if 0 <= now - w["t"] <= DAY and (plays := session_plays(w)):
            last = {**view(w), "plays": len(plays), "titles": [e["title"] for e in plays[:2]]}
            break
    return {"active": active, "last": last}


def attribute(it):
    if "history_by" in it:
        by = it["history_by"]
        return by[0] if len(by) == 1 else "together", by, {}
    if "_summary" in it:
        e = it["_summary"]
        return e["who"], e["by"], {}
    if player_history_active() and it.get("profile") in retained_people():
        return it["profile"], [it["profile"]], {}
    from skald import plays
    if plays.managed(SimpleNamespace(**globals())):
        profile = it.get('profile')
        if profile in ('household', 'together'):
            return 'together', ['together'], {}
        return GUEST, [GUEST], {}
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
    values = [(it["id"], who, by) for it in items for who, by, _ in [attribute(it)] if by != ["together"] and who in member_people()][:40]
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
    refresh_preferences()
    if kids_age(safety_subject(person)) is not None:
        person = safety_subject(person)
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
        return {**cached[2], "anchors": [a for a in cached[2]["anchors"] if title_allowed(a["key"], a["kind"], person)]}
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
REMOTE_ROWS = set(ROW_OWNER) | set(READY_ROWS) | {rid('foryou'), rid('week'), rid('weekpicks'), rid('upcoming'), rid('because'), rid('leap'),
               rid('rewatch'), rid('interview'), rid('newtoyou')} | set(BECAUSE_ROWS) | set(MOODS_ROWS) | set(LANES) | set(STUDIO_ROWS) | set(DOCS)
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
            with erasure_lock(DATA):
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
                    body = validate_feedback({k: v for k, v in raw.items() if k not in ("id", "t")}, stored=True)
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
        for person in sorted(member_people()) if e["who"] == "both" else (e["who"],):
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
    active = {k: e for k, e in state["active"].items() if k[1] in member_people()}
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
    special = guest_people(CONFIG) or CONFIG['household'].get('retired_rows')
    person = (SCOPES.get(getattr(_scoping, 'scope', None)) or _viewer.get() or who_watching()[0]) if special else None
    people = (person,) if person in PEOPLE and person in guest_people(CONFIG) else member_people()
    blocked = {key for (key, who), e in state['active'].items() if who in people and e['action'] == 'not_for_us'} if special else state['blocked']
    for (key, who), e in state["tonight"].items():
        if who not in people:
            continue
        if now - e["t"] < 18 * 3600:
            tonight.add(key)
    return blocked, tonight


_title_fixes = (None, ())


def title_fixes():
    """Rows from title_fixes.json (defer, ignore, move); reread only when the file changes."""
    global _title_fixes
    path = os.path.join(DATA, "title_fixes.json")
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ()
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _title_fixes[0] != stamp:
        try:
            with open(path) as f:
                rows = tuple(r for r in json.load(f)["fixes"] if isinstance(r, dict) and isinstance(r.get("key"), str))
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"title fixes: {type(e).__name__}: {e}", flush=True)
            rows = ()
        _title_fixes = (stamp, rows)
    return _title_fixes[1]


def deferred_keys(who, now=None):
    """Titles a member put off for now; sessions without one member hide every member's deferrals."""
    now = time.time() if now is None else now
    people = (who,) if who in PEOPLE else member_people()
    return frozenset(r["key"] for r in title_fixes() if r.get("op") == "defer" and r.get("who") in people
                     and isinstance(r.get("until"), (int, float)) and r["until"] > now)


def title_moves():
    """Reattributions per title, oldest first."""
    moves = {}
    for r in sorted((r for r in title_fixes() if r.get("op") == "move"), key=lambda r: r.get("t", 0)):
        moves.setdefault(r["key"], []).append((r.get("who"), r.get("to")))
    return moves


def watch_owners(e, moves=None):
    """Who a watch belongs to after reattributions, as {member: member who first watched it}."""
    owners = {b: b for b in e.get("by", ()) if b in PEOPLE}
    for who, to in (title_moves() if moves is None else moves).get(e["key"], ()):
        if who in owners and to in PEOPLE and to not in owners:
            owners[to] = owners.pop(who)
    return owners


def taste_entries(entries):
    """Household taste evidence; a plain watch loses the share of each member who asked Skald not to learn from it."""
    ignored = {(r["key"], r.get("who")) for r in title_fixes() if r.get("op") == "ignore"
               and r.get("who") not in guest_people(CONFIG)}
    if not ignored:
        return entries
    moves, out = title_moves(), []
    for e in entries:
        owners = watch_owners(e, moves)
        if owners and e.get("signal") == "watched" and any((e["key"], who) in ignored for who in owners):
            e = {**e, "weight": e["weight"] * sum((e["key"], who) not in ignored for who in owners) / len(owners)}
        out.append(e)
    return out


def title_fix_stamp():
    return tuple(sorted((r["op"], r["key"], r.get("who") or "", r.get("to") or "") for r in title_fixes() if r.get("op") in ("ignore", "move")))


def row_masks(who):
    """Row masks for one session: blocked (feedback plus this person's deferrals), tonight, memory veto."""
    blocked, tonight = remote_masks()
    return (blocked | deferred_keys(who), tonight, veto_keys(who))


def remote_order(metas, masks):
    blocked, tonight = masks[:2]
    metas = [m for m in metas if m["id"] not in blocked]
    return [m for m in metas if m["id"] not in tonight] + [m for m in metas if m["id"] in tonight]


def validate_feedback(body, *, stored=False):
    fields = {"key", "kind", "title", "year", "who", "action", "reasons", "row", "undo"}
    if not isinstance(body, dict) or set(body) - fields or not {"key", "kind", "title", "who", "action"} <= set(body):
        raise ValueError("invalid fields")
    if body["who"] == GUEST:
        raise PermissionError("guest is read-only")
    e = {"year": "", "reasons": [], "row": None, "undo": None, **body}
    if not isinstance(e["key"], str) or not re.fullmatch(r"tmdb:\d{1,9}", e["key"], re.ASCII):
        raise ValueError("invalid title key")
    if not isinstance(e["who"], str) or not who_ok(e["who"], "both", *(retained_people() if stored else ())):
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
    if e["row"] is not None and (not isinstance(e["row"], str) or e["row"] not in REMOTE_ROWS | (set(CONFIG["household"].get("retired_rows", [])) if stored else set())):
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
        rewrite = _remote_cache["lines"] >= 5000 or _remote_cache["corrupt"]
        if rewrite:
            events = compact_feedback(events + [e], e["t"])
            if len(events) > 5000:
                raise ValueError("feedback capacity reached")
        with household_writable(DATA, household_erased) as allowed:
            if not allowed:
                return False
            if rewrite:
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
            out.append({"id": rid('interview'), "name": t("row.interview"), "metas": [
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
    if household_erased(DATA):
        code, body = 503, b""
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
    refresh_preferences()
    if viewer is not None:
        if asked not in ((None, viewer) if viewer == GUEST else (None, viewer, "together")):
            raise PermissionError("person")
        selected = viewer if asked is None else asked
        return safety_subject(selected, viewer) if kids_age(viewer) is not None else selected
    if not who_ok(asked, None, "together", GUEST):
        raise ValueError("unknown person")
    return asked


def member_view(req):
    if req["viewer"] == GUEST or req.get("query", {}).get("person") == GUEST:
        raise PermissionError("guest is read-only")


_title_notes = {"model": None, "notes": {}}


def title_view(key, kind, person=None):
    refresh_preferences()
    if kids_age(safety_subject(person)) is not None:
        person = safety_subject(person)
    if not title_allowed(key, kind, person):
        return None
    rec = frec(key, kind)
    if not rec:
        return None
    m = get_model(False)
    slot = fkey(key, kind)
    by = m["ent"][slot][1] if m and slot in m["ent"] else None
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
        stamp = (id(m), key, kind, person, LANG, HOUSEHOLD_PRIOR, maturity(m, person)["explicit"], m.get("cal"), json.dumps(rec, sort_keys=True))
        cached = notes.get(stamp)
        if cached is None:
            s, fids = person_score(m, key, person, kind)
            # Together's household fallback may name a facet belonging to only one person.
            if person == "together":
                fids = [f for f in fids if f in m.get("paff", {}).get(person, {})]
            conf, pct = confidence(m, key, s, fids, person)
            why = reason_details(m, key, fids, 3, person, kind)
            if len(notes) >= 1000:
                notes.clear()
            cached = conf, pct, tuple(why)
            notes[stamp] = cached
        conf, pct, why = cached
        why = list(why)
    if kids_age(safety_subject(person)) is not None:
        why = []
    seed = bool(why and why[0][1])
    return {"key": key, "kind": kind, "title": rec.get("title", ""), "person": person,
            "match": pct, "confidence": conf, "reasons": [text for text, _ in why], "maturity": maturity(m, person),
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
decision("explain_pick", ver=3, lane=None, cap=None, ttl=DAY, serving=True, llm=None, requires=(),
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
    who = viewer or (person if person in PEOPLE else "both")
    rows = []
    for cid, types, name in feed_rows(lang, profiles=False, who=who):
        if cid not in API_ROWS or req["query"].get("row", cid) != cid:
            continue
        if viewer and (cid in (rid('week'), rid('weekpicks')) or ROW_OWNER.get(cid, viewer) != viewer):
            continue
        items = []
        metas = row_metas(cid, "tv", lang, who=who)
        for meta in metas[:15]:
            kind = "movie" if meta.get("type") == "movie" else "tv"
            view = title_view(meta["id"], kind, viewer or person)
            if viewer and view is None:
                continue
            why = next(iter(view["reasons"]), None) if view else None
            if why is None and viewer is None and kids_age(safety_subject(person)) is None:
                why = meta.get("description", "").split(" · ")[0] or None
            items.append({"key": meta["id"], "kind": kind, "title": meta.get("name", ""),
                          "year": meta.get("releaseInfo", ""), "poster": meta.get("poster"), "why": why})
        # Built shelf names can contain household anchor titles.
        rows.append({"id": cid, "name": t("row.picked.private") if viewer or kids_age(person) is not None else row_name(cid, lang, name, metas), "items": items})
    return {"lang": lang, "person": person, "rows": rows}


def guest_rows_view(lang, row=None):
    rows = []
    for cid in DEDUPE_ORDER:
        if not row_enabled(cid) or cid not in GUEST_ROWS or row not in (None, cid):
            continue
        items = []
        for typ in ("movie", "series"):
            for meta in row_list(cid, typ, lang)[:15]:
                kind = "movie" if typ == "movie" else "tv"
                if title_view(meta["id"], kind, GUEST) is None:
                    continue
                items.append({"key": meta["id"], "kind": kind, "title": meta.get("name", ""),
                              "year": meta.get("releaseInfo", ""), "poster": meta.get("poster"), "why": None})
        name = t("row.digital") if cid == rid('digital') else t("row.toprated") if cid == rid('toprated') else t("row.top10.guest")
        rows.append({"id": cid, "name": name, "items": items})
    return {"lang": lang, "person": GUEST, "rows": rows}


def metrics_view(req):
    member_view(req)
    report = metrics(days=req["query"].get("days", LEARN_WINDOW))
    viewer = req["viewer"]
    if viewer:
        if "who" in report:
            report["who"] = {k: v for k, v in report["who"].items() if k in (viewer, "both", "together", "unknown")}
        report["rows"] = {k: v for k, v in report.get("rows", {}).items()
                          if k not in (*{rid(p) for p in ARCHIVED}, rid('week'), rid('weekpicks')) and ROW_OWNER.get(k, viewer) == viewer}
    else:
        report["rows"] = {k: v for k, v in report.get("rows", {}).items() if k not in {rid(p) for p in ARCHIVED}}
    with _usage_lock:
        _usage_roll()
        usage = {"day": _usage["day"], "month_cost": _usage["month_cost"],
                 "caps": {**AI_CAPS, "monthly_usd": ai_monthly_usd()}}
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
    with history_locked(DATA), _imports_lock:
        from skald.history import folded_imports, remove_imports
        stored_events = read_imports()
        by_id = {}
        for stored in stored_events + folded_imports(DATA):
            if not req["viewer"] or stored["who"] == req["viewer"]:
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
            kept = [e for e in stored_events if e["id"] not in removed or
                    req["viewer"] and e["who"] != req["viewer"]]
            save_imports(kept)
            remove_imports(DATA, removed, req["viewer"], {e["id"] for e in kept})
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
                or not who_ok(c["person"], None, GUEST, *retained_people()) \
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
    if household_erased(DATA):
        return None, 503
    header = handler.headers.get("Authorization")
    if header is None and CONFIG["addon"]["legacy_open"] and os.environ.get("API_LOOPBACK_OPEN") == "1" and loopback(handler):
        return {"name": "loopback", "scopes": ["read", "decide"], "person": None}, None
    if header is None:
        return None, 401
    try:
        refresh_preferences()
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
        if hmac.compare_digest(digest, c["sha256"]) and valid and who_ok(c['person'], None, GUEST):
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
    if req["tail"] == "parse_command":
        return search_commands.api_preview(command_context(), req)
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


def api_shelves(req):
    viewer = req['viewer']
    if viewer == GUEST or kids_age(viewer) is not None:
        raise PermissionError('shelves')
    body = req.get('body')
    if body is not None:
        if not isinstance(body, dict) or set(body) != {'plan'} or not isinstance(body['plan'], str):
            raise ValueError('body')
        with shelves.LOCK:
            plan = shelves.read(SimpleNamespace(**globals()))['plans'].get(body['plan'])
            if not plan or viewer is not None and plan['scope'] not in (viewer, 'together'):
                raise PermissionError('shelf scope')
            search_commands.guest_scope(command_context(), viewer, {'people': [plan['scope']], 'shelf': {'scope': plan['scope'], **plan['change']}})
            return shelf_change(plan['scope'], plan['change']['op'], plan=body['plan'])
    result = shelf_list()
    return {**result, 'counts': {p: n for p, n in result['counts'].items() if viewer is None or p in (viewer, 'together')},
            'wishes': [w for w in result['wishes'] if viewer is None or w['scope'] in (viewer, 'together')]}


api("shelves", get=("read", api_shelves), post=("events", api_shelves), example={"body": {"plan": "preview hash"}})
api("decide", get=("read", lambda req: {"types": decide_index(), "person": req["viewer"]}), example={})
api("decide/*", post=("decide", api_decide),
    tail=lambda name: name in DECISIONS and DECISIONS[name]["api"],
    example={"tail": "route_query", "body": {"in": {"query": "synthetic request"}, "no_llm": True}})
api("command", post=("events", lambda req: search_commands.api_apply(command_context(), req)), example={"body": {"plan": "preview hash"}})
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


def child_access(person, path, post=False):
    refresh_preferences()
    root = path.split("/", 1)[0].split(".", 1)[0]
    if kids_age(person) is not None and (root in ("profile", "taste", "feedback", "history", "resolve", "shelves") or
            path == "decide/resolve_title" or root == "session" and not post):
        raise PermissionError("child access")


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
    try:
        child_access(client["person"], path, post)
    except PermissionError:
        return error(403, "person")
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
    token = _safety_context.set(("api", client["person"]))
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
    finally:
        _safety_context.reset(token)

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
        with addon_scope(getattr(_scoping, "scope", None)):
            remote_route(self)

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            return api_route(self, post=True)
        with addon_scope(getattr(_scoping, "scope", None)):
            remote_route(self, post=True)

    def __getattr__(self, name):
        if name.startswith("do_") and self.path.startswith("/api/v1/"):
            return self.do_GET
        raise AttributeError(name)


def remote_route(handler, post=False):
    if household_erased(DATA):
        return remote_send(handler, 503)
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
    try:
        child_access(safety_subject(), route, post)
    except PermissionError:
        return remote_send(handler, 403, {"error": "person"})
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
        with erasure_lock(DATA):
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

    def send_json(self, code, body, max_age=0, cors=False, no_store=False):
        refresh_preferences()
        scope = getattr(_scoping, "scope", None)
        no_store = no_store or cors and (kids_age(SCOPES.get(scope)) is not None or scope is None and any(kids_age(p) is not None for p in PEOPLE))
        token = addon_token().encode("ascii")
        if household_erased(DATA):
            if 'metas' in body:
                body = {'metas': []}
            elif 'meta' in body:
                body = {'meta': None}
            elif 'catalogs' in body:
                body = {**body, 'catalogs': []}
            else:
                code, body = 503, {}
            no_store = True
        raw = re.sub(re.escape(token), b"[redacted]",
                     json.dumps(body, ensure_ascii=False).encode(), flags=re.IGNORECASE)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        elif code >= 400 and self.private_request():
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
        if parts == ["command.svg"]:
            raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450"><rect width="300" height="450" fill="#20242b"/><text x="150" y="230" text-anchor="middle" fill="white" font-size="30">Skald</text></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if parts == ["health"]:
            from skald.health import view
            client, error = _api_auth(self) if self.headers.get('Authorization') else (None, None)
            local = loopback(self) and not any(name in self.headers for name in
                ('X-Forwarded-For', 'X-Real-IP', 'Forwarded'))
            detail = local or client and 'read' in client['scopes'] and client.get('person') is None
            return self.send_json(200, view(DATA) if detail else {"ok": True}, no_store=True)
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
        refresh_preferences()
        if household_erased(DATA):
            if parts and parts[-1] == 'manifest.json':
                return self.send_json(200, {'id': 'skald', 'version': VERSION, 'name': 'Skald',
                    'description': 'Run python -m skald setup household on the server.',
                    'resources': ['catalog'], 'types': ['movie', 'series'], 'catalogs': []}, cors=True, no_store=True)
            return self.send_json(200, {'metas': []}, cors=True, no_store=True)
        if parts == ["health"]:
            from skald.health import view
            return self.send_json(200, view(DATA) if admin else {"ok": True}, no_store=True)
        if parts == ["stats"] and admin:
            return self.send_json(200, stats())
        if len(parts) == 2 and parts[0] == "why" and parts[1].endswith(".json") and admin:
            return self.send_json(200, explain(urllib.parse.unquote(parts[1][:-5])))
        if not parts or not LANG_RE.match(parts[0]):
            return self.send_json(404, {"error": "not found"})
        lang = parts[0]
        scope = None
        if parts[1:2] == ["p"]:
            if len(parts) >= 4 and parts[2] in ARCHIVED and parts[3:4] == ['catalog']:
                return self.send_json(200, {'metas': []}, cors=True)
            if len(parts) < 4 or parts[2] not in SCOPES or self.route_path != "/" + "/".join(parts):
                return self.send_json(404, {"error": "not found"})
            scope, parts = parts[2], [lang, *parts[3:]]
        with addon_scope(scope):
            return self.addon_get(parts, lang, scope)

    def addon_get(self, parts, lang, scope):
        if len(parts) == 4 and parts[1] == 'catalog' and parts[2] in ('movie', 'series', 'tv') and parts[3].endswith('.json') and parts[3][:-5] in CONFIG['household'].get('retired_rows', []):
            return self.send_json(200, {'metas': []}, cors=True)
        if parts[1:] == ["manifest.json"]:
            return self.send_json(200, manifest(lang, scope), 3600, cors=True)
        if len(parts) == 4 and parts[1] == "meta" and parts[2] in ("movie", "series", "tv") and parts[3].endswith(".json"):
            cid = urllib.parse.unquote(parts[3][:-5])
            if cid.startswith(rid('cmd:')):
                return self.send_json(200, search_commands.card_meta(command_context(), cid, parts[2], scope), cors=True, no_store=True)
        if len(parts) == 4 and parts[1] == "catalog" and parts[3].endswith(".json") \
                and (parts[3][:-5], parts[2]) in {(cid, t) for cid, typs, _ in feed_rows(lang) for t in typs}:
            _langs.add(lang)
            metas = row_metas(parts[3][:-5], parts[2], lang, who=SCOPES[scope]) if scope is not None else row_metas(parts[3][:-5], parts[2], lang)
            if CONNECTOR_CATALOG_DEMAND: connectors_demand(time.time())
            note_served(learn_id(parts[3][:-5], lang, metas), metas)
            viewer = SCOPES[scope] if scope is not None else who_watching()[0]
            try:
                row_metrics.record_shown(DATA, CONFIG, parts[3][:-5],
                                         'together' if viewer == 'both' else viewer, metas)
            except Exception:
                _daily_log('row metrics exposure could not be saved')
            return self.send_json(200, {"metas": metas}, shelves.cache_age(SimpleNamespace(**globals()), parts[3][:-5]) if parts[3][:-5] in SHELF_ROWS else 60 if parts[3][:-5] in SESSION_ROWS else 900, cors=True)
        if len(parts) == 4 and parts[1] == "catalog" and parts[2] in MIX \
                and parts[3].startswith(rid('lab_')) and parts[3].endswith(".json"):
            return self.send_json(200, {"metas": []}, cors=True)
        if len(parts) == 5 and parts[1] == "catalog" and parts[2] in ("movie", "series") \
                and parts[3] == rid('search') and parts[4].endswith(".json"):
            extras = urllib.parse.parse_qs(parts[4][:-5])
            query = (extras.get("search") or [""])[0].strip()
            if not query or addon_token().lower() in query.lower():
                return self.send_json(200, {"metas": []}, cors=True, no_store=True)
            record, duplicate = log_search(query, deferred=True)
            _scoping.search_timing = None
            try:
                result = search(parts[2], query, lang)
                for meta in result.get("metas", []):
                    if meta.get("poster") == "/command.svg":
                        meta["poster"] = self.headers.get("X-Forwarded-Proto", "http") + "://" + self.headers.get("Host", "localhost") + "/command.svg"
                search_commands.remember_cards(command_context(), result.get("metas", []), scope)
                return self.send_json(200, result, cors=True, no_store=True)
            except Exception as e:
                return self.send_json(502, {"metas": [], "error": type(e).__name__}, cors=True, no_store=True)
            finally:
                timing = _scoping.search_timing
                if not duplicate:
                    write_search(record, timing)
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
        from skald.health import Health, settings
        if settings(DATA)['enabled']:
            _health = Health(DATA, cache_changed=health_cache_changed)
        start_feed_worker()
        if _health is not None:
            threading.Thread(target=health_supervisor, daemon=True, name='skald-health').start()
        threading.Thread(target=remote.serve_forever, daemon=True).start()
        main.serve_forever()
