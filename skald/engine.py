"""A copied snapshot and existing server functions, behind plain JSON dictionaries."""
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import copy
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import random
import tempfile
import time
from types import SimpleNamespace
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = (".skald-format", "skald.json", "legacy.json", "profile.json", "emb.json", "facets.json", "learn.json", "taste.json", "trakt.json",
              "feeds.json", "shown.json", "shown_scopes.json", "history.json", "history-summary.json", "history-forgotten.json", "history-seen.json", "feedback.jsonl", "imports.jsonl", "online.json", "metrics.json", "memory.json", "interview.json", "taste_ring.json", "connectors.json", "shelves.json", "undo.jsonl", "archetypes.json", "title_fixes.json", "lab.json")


def data_files(config):
    return DATA_FILES + tuple(source + "-" + person + ".history.json"
        for source, cid in (("trakt", "trakt_api"), ("mdblist", "mdblist"), ("stremio", "stremio"), ("pmdb", "pmdb"))
        for person in config["connectors"].get(cid, {}).get("accounts", {}))


def doctor_keys(data):
    spec = importlib.util.spec_from_file_location("skald_doctor_server", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    server._CONFIG_READONLY = True
    env = {k: v for k, v in os.environ.items() if not k.startswith("SKALD_")}
    env.update(DATA_DIR=str(data), TMDB_API="offline", OPENROUTER_API_KEY="")
    with patch.dict(os.environ, env, clear=True):
        spec.loader.exec_module(server)
    try:
        facets = json.loads((Path(data) / "facets.json").read_text()).get("items") or {}
        records = server.mixed_keys(data, facets)
        return {"mixed": len(records), "records": records}
    finally:
        for name in ("_pool", "_outer", "_search_pool", "_nzb_pool"):
            getattr(server, name).shutdown(wait=True, cancel_futures=True)


class OfflineNetworkError(BaseException):
    pass


class CalibrationSkipped(Exception):
    pass


def denied(*args, **kwargs):
    raise OfflineNetworkError("offline engine blocked a network call")


def plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                 default=lambda v: sorted(v) if isinstance(v, set) else list(v)))


def facet_view(model, person=None, types="xktd", n=10, keys=None):
    aff = model["paff"].get(person, {}) if person else model["aff"]
    out = []
    for fid, value in sorted(aff.items(), key=lambda pair: (-pair[1], pair[0])):
        if fid[0] not in types or keys is not None and fid not in keys:
            continue
        anchors = [model["ent"][k][0] for _, k in model["holders"].get(fid, [])
                   if k in model["ent"] and (not person or person in model["ent"][k][1] or "together" in model["ent"][k][1])][:3]
        out.append({"id": fid, "name": model["names"].get(fid, fid), "affinity": round(value, 4), "anchors": anchors})
        if len(out) >= n:
            break
    return out


class Engine:
    def __init__(self, data_dir, server_path=None, offline=True):
        from .migrate import check
        self.data_dir = Path(data_dir).resolve()
        check(self.data_dir)
        if not any((self.data_dir / n).is_file() for n in ("skald.json", "profile.json", "history.json", "trakt.json")):
            raise ValueError("snapshot must contain skald.json, a profile or watch history")
        self._temp = tempfile.TemporaryDirectory(prefix="skald-engine-")
        self._work = Path(self._temp.name)
        try:
            for name in (*DATA_FILES, "metrics_shown.json"):
                if (self.data_dir / name).is_file():
                    shutil.copyfile(self.data_dir / name, self._work / name)
            from .members import load_config
            from .backup import credential_inodes
            config, _ = load_config(str(self._work), {}, readonly=True)
            member_files = data_files(config)[len(DATA_FILES):]
            blocked = credential_inodes(self.data_dir) if member_files else set()
            for name in member_files:
                source = self.data_dir/name
                if source.is_file() and not source.is_symlink():
                    st = source.stat()
                    if (st.st_dev, st.st_ino) not in blocked:
                        shutil.copyfile(source, self._work/name)
            from .migrate import ensure
            ensure(self._work)
            env = {k: v for k, v in os.environ.items() if not k.startswith("SKALD_")}
            env.update(DATA_DIR=str(self._work), TMDB_API="offline" if offline else os.environ.get("TMDB_API", ""),
                       OPENROUTER_API_KEY="" if offline else os.environ.get("OPENROUTER_API_KEY", ""))
            with patch.dict(os.environ, env, clear=True), \
                    patch("urllib.request.urlopen", denied) if offline else patch.dict(os.environ, {}), \
                    patch.object(__import__("sys"), "dont_write_bytecode", True):
                spec = importlib.util.spec_from_file_location("skald_engine_server", server_path or ROOT / "server.py")
                self._server = importlib.util.module_from_spec(spec)
                self._server._CONFIG_READONLY = True
                spec.loader.exec_module(self._server)
            s = self._server
            if getattr(s, "CONFIG_ERROR", None):
                raise ValueError(str(s.CONFIG_ERROR))
            # Older sources remain usable by the existing --before replay callers.
            s.LANG = getattr(s, "LANG", s.feed_rows.__defaults__[0])
            if not hasattr(s, "local_now"):
                s.local_now = s.berlin_now
            self._network_tmdb, self._network_openrouter = s.tmdb, s.openrouter
            if offline:
                s._connector_open = denied
                s.urllib = SimpleNamespace(parse=s.urllib.parse, request=SimpleNamespace(urlopen=denied, Request=urllib.request.Request))
                s.tmdb, s.openrouter, s.anilist = self._offline_tmdb, denied, denied
                for name in ("ensure_emb", "ensure_facets", "tag_experiences"):
                    setattr(s, name, lambda *args, **kwargs: None)
            noise = io.StringIO()
            with redirect_stdout(noise), redirect_stderr(noise):
                s.load_state()
            self.diagnostics = noise.getvalue().splitlines()
            profile_path = self._work / "profile.json"
            self._base = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {"entries": []}
            self._index = self._title_index()
            if not profile_path.exists():
                with redirect_stdout(io.StringIO()):
                    self._base = s.build_profile(s.read_history(), [], s.read_trakt())
                self._base["built_at"] = time.time()
                profile_path.write_text(json.dumps(plain(self._base)), encoding="utf-8")
                self._index = self._title_index()
            self._refresh()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if hasattr(self, "_server"):
            if hasattr(self._server, "_decide_queue"):
                self._server.flush_decisions()
                self._server._decide_queue.put(False)
            for name in ("_pool", "_outer", "_search_pool", "_nzb_pool", "_connector_pool"):
                pool = getattr(self._server, name, None)
                if pool:
                    pool.shutdown(wait=True, cancel_futures=True)
        self._temp.cleanup()

    def decisions(self):
        return {"types": self._server.decide_index()}

    def decide(self, name, inp, person=None):
        s = self._server
        if isinstance(inp, list):
            result = {"results": s.decide_many(name, inp, person=person, allow_llm=False)}
        else:
            result = s.decide(name, inp, person=person, allow_llm=False)
        s.flush_decisions()
        return result

    def _title_index(self):
        out = {}
        for key, rec in self._server._facets.items():
            if "@" in key:
                continue  # The key-only CLI index retains the bare title; slots are internal.
            out[key] = {"id": int(key[5:]), "title": rec.get("title", ""), "year": rec.get("y", ""),
                        "kind": rec.get("kind", "movie"), "aliases": []}
        for e in self._base["entries"]:
            out.setdefault(e["key"], {}).update(id=e["id"], title=e["title"], year=e["year"], kind=e["kind"],
                                              imdb=(e.get("item") or {}).get("imdb"))
        for item in self._server.read_trakt():
            key = item["id"]
            out.setdefault(key, {"id": int(key[5:]), "title": item.get("title", ""), "year": str(item.get("year") or ""),
                                 "kind": "movie" if item.get("type") == "movie" else "tv", "aliases": []})
            if item.get("imdb"):
                out[key]["imdb"] = item["imdb"]
        for rec in self._server._feeds.values():
            for metas in rec.get("metas", {}).values():
                if not isinstance(metas, list):
                    continue
                for meta in metas:
                    if not isinstance(meta, dict) or not str(meta.get("id", "")).startswith("tmdb:"):
                        continue
                    key = meta["id"]
                    entry = out.setdefault(key, {"id": int(key[5:]), "title": meta.get("name", ""),
                                                "kind": "tv" if meta.get("type") == "series" else "movie",
                                                "year": str(meta.get("releaseInfo") or "")[:4]})
                    aliases = entry.setdefault("aliases", [])
                    if meta.get("name") and meta["name"] != entry["title"] and meta["name"] not in aliases:
                        aliases.append(meta["name"])
        return out

    def _offline_tmdb(self, path, **kwargs):
        if path.startswith("trending/"):
            raise CalibrationSkipped("no trending calibration in snapshot")
        if path in ("search/multi", "search/movie", "search/tv"):
            from .ingest.resolve import normal
            import difflib
            query = normal(kwargs["query"])
            matches = [c for c in self._index.values() if max((difflib.SequenceMatcher(None, query, normal(t)).ratio()
                       for t in [c["title"]] + c.get("aliases", [])), default=0) >= .55]
            kind = path[7:]
            year = kwargs.get("year") or kwargs.get("first_air_date_year")
            return {"results": [self._tmdb_shape(c) for c in matches if (kind == "multi" or c["kind"] == kind)
                                and (not year or c["year"] == str(year))]}
        if path.startswith("genre/"):
            return {"genres": []}
        if path.startswith("find/"):
            imdb = path[5:]
            return {field: [c for c in self._index.values() if c.get("imdb") == imdb and c["kind"] == kind]
                    for kind, field in (("movie", "movie_results"), ("tv", "tv_results"))}
        kind, _, tid = path.partition("/")
        hit = self._index.get("tmdb:" + tid)
        if kind in ("movie", "tv") and hit and hit["kind"] == kind:
            return self._tmdb_shape(hit)
        raise LookupError("title metadata unavailable offline")

    def _tmdb_shape(self, record):
        title, year = record["title"], record["year"]
        rec = self._server.frec(f"tmdb:{record['id']}", record["kind"])
        return {**copy.deepcopy(record), "media_type": record["kind"],
                "title" if record["kind"] == "movie" else "name": title,
                "release_date" if record["kind"] == "movie" else "first_air_date": year + "-01-01" if year else "",
                "genres": [{"name": g} for g in rec.get("g", [])]}

    def _profile_with(self, source):
        p = copy.deepcopy(source)
        p.setdefault("searches", [])
        p.setdefault("likes", "")
        p["seen"] = set(p.get("seen", [])) | {e["key"] for e in p["entries"]}
        p["negative"] = {e["key"] for e in p["entries"] if e["weight"] < 0}
        genres = Counter()
        for e in p["entries"]:
            for g in e.get("genres", []):
                genres[g] += e["weight"]
        p["genres"] = dict(genres)
        return p

    def _build(self, p):
        with redirect_stdout(io.StringIO()):
            return self._server.build_model(p)

    def _refresh(self):
        s = self._server
        p = copy.deepcopy(self._base)
        if (self._work / "imports.jsonl").exists():
            p["entries"] = [e for e in p["entries"] if e.get("who") != "import"]
            for e in p["entries"]:
                if "opinions" in e:
                    e["opinions"] = {person: value for person, value in e["opinions"].items() if value.get("wsrc") != "import"}
                if (e.get("shared") or {}).get("wsrc") == "import":
                    del e["shared"]
                if "opinions" in e and not e["opinions"]:
                    del e["opinions"]
                    e.update({k: v for k, v in e.pop("shared", {}).items() if v is not None})
        by_key = {e["key"]: e for e in p["entries"]}
        records = s.merge_imports(p["entries"], by_key, time.time())
        s.merge_feedback(p["entries"], by_key, time.time(), records=records)
        self._profile = self._profile_with(p)
        for e in self._profile["entries"]:
            self._index.setdefault(e["key"], {"id": e["id"], "title": e["title"], "year": e["year"], "kind": e["kind"], "aliases": []})
        self._model = self._build(self._profile)
        s._profiles["household"] = self._profile
        s._model["m"] = self._model

    def _person(self, person):
        if person is not None and person not in self._server.PEOPLE:
            raise ValueError("unknown person")

    def _facets(self, model, person=None, types="xktd", n=10, keys=None):
        return facet_view(model, person, types, n, keys)

    def align(self, person=None, limit=10):
        from .alignment import view
        return view(self._server, self._profile, self._model, person, limit)

    def profile(self, person=None):
        self._person(person)
        s = self._server
        entries = s.person_entries(self._profile, person) if person else self._profile["entries"]
        clean = [{k: e[k] for k in ("key", "title", "year", "kind", "signal", "weight", "by", "who", "flags")} for e in entries]
        sources = Counter()
        source_signals = {}
        for e in entries:
            src = next((f for f in e.get("flags", []) if f in s.IMPORT_TRUST), "import") if e["who"] == "import" else e["who"]
            sources[src] += 1
            counts = source_signals.setdefault(src, Counter())
            counts[e["signal"]] += 1
        return plain({"person": person, "maturity": s.maturity(self._model, person), "titles": len(entries), "entries": clean, "sources": dict(sources),
                "source_signals": source_signals,
                "signals": dict(Counter(e["signal"] for e in entries)),
                **{name: self._facets(self._model, person, types) for name, types in
                   (("experiences", "x"), ("themes", "kt"), ("creators", "d"))}})

    def score(self, key, person=None):
        self._person(person)
        s = self._server
        kind = self._index.get(key, {}).get("kind")
        value, fids = s.person_score(self._model, key, person, kind)
        confidence, percentile = s.confidence(self._model, key, value, fids, person)
        present = {fid for fid, _ in s.facet_pairs(s.frec(key, kind))}
        facets = self._facets(self._model, person, "xktdangcs", 12, present)
        return {"key": key, "title": self._index.get(key, {}).get("title"), "person": person, "score": value,
                "percentile": percentile, "confidence": confidence, "maturity": s.maturity(self._model, person),
                "reasons": s.reasons(self._model, key, fids, 3, person, kind),
                "facets": facets,
                "calibration": self._model.get("cal_reference") or "snapshot has no reference"}

    def calibration(self, person=None):
        from . import shelves
        person = None if person == "household" else person
        self._person(None if person == "together" else person)
        s, m = self._server, self._model
        bg = m.get("pbg", {}).get(person, []) if person else m["bg"]
        scores = {f"p{p}": bg[round((len(bg)-1)*p/100)] if bg else None for p in (0, 10, 25, 50, 75, 90, 100)}
        owners = {**s.ROW_OWNER, **{s.rid(f"shelf_{w['scope']}_{w['slot']}"): w['scope'] for w in shelves.read(s)['wishes']}}
        rows = {}
        for (cid, lang), rec in sorted(s._feeds.items()):
            if lang != s.LANG or not s.row_enabled(cid):
                continue
            who = owners.get(cid, person)
            for typ, metas in (rec.get("metas") or {}).items():
                if typ not in ("tv", "movie", "series") or not isinstance(metas, list):
                    continue
                bands, unknown = {f"{d}-{d+9}": 0 for d in range(40, 100, 10)}, 0
                for meta in metas:
                    kind = "movie" if meta.get("type") == "movie" else "tv"
                    score = s.person_score(m, meta["id"], who, kind)[0]
                    pct = s.match_pct(m, score, who)
                    if pct is None:
                        unknown += 1
                    else:
                        bands[f"{pct//10*10}-{pct//10*10+9}"] += 1
                row = cid if typ == "tv" else cid + ":" + typ
                rows[row] = {"person": who, "bands": bands, "unknown": unknown}
        return {"person": person, "reference": {"kind": m.get("cal_reference"), "size": len(bg)},
                "scores": scores, "rows": rows}

    def explain(self, key, person=None):
        return plain(self._server.explain(key, self._index.get(key, {}).get("kind"), person=person))

    def interview(self, person, answer=None):
        self._person(person)
        if person is None:
            raise ValueError("unknown person")
        if answer is not None:
            self._server.interview_answer({**answer, "who": person})
            self._refresh()
        return self._server.interview_next(person)

    def _report_events(self):
        s = self._server
        now = self._base.get("built_at", time.time())
        # Resolve raw TV ids and told seeds from snapshot metadata, never a live lookup.
        details, picks = {}, {}
        for e in self._base["entries"]:
            rec = self._tmdb_shape(self._index[e["key"]])
            rec["first_air_date"] = e["year"] + "-01-01" if e["year"] else ""
            details[e["key"]] = details[(e.get("item") or {}).get("id", e["key"])] = (e["kind"], rec)
            picks[(e["title"].casefold(), e["kind"])] = rec
        with patch.object(s, "tmdb_details", side_effect=lambda it: details.get(it["id"])), \
                patch.object(s, "resolve_pick", side_effect=lambda kind, title, year, lang:
                    (picks[(title.casefold(), kind)], {}) if (title.casefold(), kind) in picks else None), \
                redirect_stdout(io.StringIO()):
            stages = [(stage, list(evts)) for stage, evts in s.event_stages(now,
                [it for it in s.read_history() if it.get("profile", "household") == "household"], s.read_trakt())]
        return now, stages

    def _events_model(self, stages, now):
        s = self._server
        stages = copy.deepcopy(stages)
        entries, _, seen = s.fold_events(stages, now)
        p = self._profile_with({"entries": entries, "seen": seen})
        active = {(ev["key"], ev["who"]): ev for stage, evts in stages if stage == "remote" for ev in evts}
        with patch.object(s, "active_feedback", return_value=active):
            return self._build(p)

    def trace(self, key, person=None):
        self._person(person)
        s, m = self._server, self._model
        kind = self._index.get(key, {}).get("kind")
        rec = s.frec(key, kind)
        vec = m.get("pvec", {}).get(person) if person else m["vec"]
        v = s.fit(vec, key, kind)
        bonus, good = s.facet_bonus(m, rec, m["paff"].get(person, {}) if person else None)
        scale = m.get("pbscale", {}).get(person, 1.) if person else m["bscale"]
        xp = m.get("px", {}).get(person) if person else m.get("x")
        a, b, c = s.SCORE_BLEND
        parts = {"fit": a * (v if v is not None else m["vbase"]),
                 "xsim": b * s.xsim(rec.get("x"), xp), "facets": c * math.tanh(bonus/scale),
                 "quality": s.quality_prior(rec)} if rec else None
        if parts is not None and s.HOUSEHOLD_PRIOR and person in s.PEOPLE and m["nexp"].get(person, 0) < s.PRIOR_FULL:
            parts["household_prior"] = s.person_score(m, key, person, kind)[0] - sum(parts.values())
        now, stages = self._report_events()
        top = {fid for _, fid in good[:5]}
        evidence = self._evidence([ev for _, evts in stages for ev in evts], top, person, now, 5)
        view = s.title_view(key, kind, person)
        explanation = self.explain(key, person)
        return {**explanation, **self.score(key, person), "parts": parts, "events": evidence,
                "rung": (view or {}).get("rung"),
                "bg_n": len(m.get("pbg", {}).get(person, []) if person else m["bg"]),
                **({"memory": explanation["memory"]} if "memory" in explanation else {})}

    def _evidence(self, events, fids, person, now, n=3):
        s = self._server
        relevant = [ev for ev in events if (not person or s.visible_by(ev["by"], person))
                    and fids & {fid for fid, _ in s.facet_pairs(s.frec(ev["key"], ev["kind"]))}]
        relevant.sort(key=lambda ev: (-s.event_weight(ev, now)[1], ev["id"]))
        return [{"key": ev["key"], "title": ev["title"], "action": ev["action"], "src": ev["src"],
                 "day": s.local_now(ev["t"]).date().isoformat() if ev["t"] is not None else None,
                 "weight": s.event_weight(ev, now)[1]} for ev in relevant[:n]]

    def report(self, person=None, since=7, before=None):
        self._person(person)
        if type(since) is not int or since < 1 or since > 36500:
            raise ValueError("--since must be between 1 and 36500")
        s, m = self._server, self._model
        now, stages = self._report_events()
        events = [ev for _, evts in stages for ev in evts]
        dated = [ev["t"] for ev in events if ev["t"] is not None]
        cutoff = max(dated) - since*s.DAY if dated else None
        kept = [(stage, [ev for ev in evts if ev["t"] is None or ev["t"] <= cutoff]) for stage, evts in stages]
        window = [ev for ev in events if ev["t"] is not None and ev["t"] > cutoff]
        seed_stats = s._seed_stats
        try:
            full = self._events_model(stages, now)
            old = self._events_model(kept, now)
            drift = self._events_model(stages, now - since*s.DAY)
            def changes(a, b, evidence=()):
                aa = a["paff"].get(person, {}) if person else a["aff"]
                bb = b["paff"].get(person, {}) if person else b["aff"]
                out = [{"id": fid, "name": b["names"].get(fid, fid), "before": aa.get(fid, 0),
                        "after": bb.get(fid, 0), "delta": bb.get(fid, 0)-aa.get(fid, 0),
                        "events": self._evidence(evidence, {fid}, person, now)}
                       for fid in aa.keys() | bb.keys() if abs(bb.get(fid, 0)-aa.get(fid, 0)) > 1e-9]
                return sorted(out, key=lambda f: (-abs(f["delta"]), f["id"]))
            # A window reaching past all dated history has no observed baseline.
            facets = changes(old, full, window) if dated and cutoff >= min(dated) else []
            decay = changes(drift, full)
            loved = sorted({e["key"] for e in (s.person_entries(self._profile, person) if person else self._profile["entries"])
                            if e["signal"] == "loved"})
            selected = random.Random(7).sample(loved, min(20, len(loved)))
            held = []
            for key in selected:
                p = copy.deepcopy(self._profile)
                p["entries"] = [e for e in p["entries"] if e["key"] != key]
                p["seen"] = set(p["seen"]) - {key}
                model = self._build(self._profile_with(p))
                value, fids = s.person_score(model, key, person, self._index[key]["kind"])
                label, _ = s.confidence(model, key, value, fids, person)
                held.append({"key": key, "label": label})
        finally:
            s._seed_stats = seed_stats
            s.clear_seed_cache()
        rows, cards = {}, []
        for cid, types, _ in s.feed_rows(s.LANG, profiles=False):
            who = s.ROW_OWNER.get(cid, person)
            for typ in (("tv",) if "tv" in types else types):
                metas = s.row_metas(cid, typ, s.LANG, who=who)
                views = [s.title_view(meta["id"], "movie" if meta.get("type") == "movie" else "tv", who) for meta in metas]
                counts = Counter((v or {}).get("rung") or "none" for v in views)
                row = cid if "tv" in types or len(types) == 1 else cid+":"+typ
                rows[row] = {rung: counts[rung]/len(views) if views else 0. for rung in ("memory", "seed", "facet", "none")}
                if cid in s.PERSON_ROWS:
                    cards.extend(views)
        hist = {str(d): 0 for d in range(40, 100, 10)}
        labels = Counter((v or {}).get("confidence") or "None" for v in cards)
        for v in cards:
            if v and v["match"] is not None:
                hist[str(min(90, v["match"]//10*10))] += 1
        evaluated = [v for v in held if v["label"] is not None]
        calibration = {"bg_n": len(m["bg"]), "pbg_n": {p: len(bg) for p, bg in m["pbg"].items()},
            "hist": hist, "labels": {label: labels[label]/len(cards) if cards else 0.
                for label in ("Strong fit", "Good fit", "Wildcard", "None")},
            "loved_wildcard": {"share": sum(v["label"] == "Wildcard" for v in evaluated)/len(evaluated) if evaluated else None,
                               "n": len(evaluated), "titles": held}}
        if before:
            from eval.reaction import kendall
            with Engine(before) as previous:
                old_keys = {k for k in set(previous._server._facets) | set(previous._server._emb) if "@" not in k} - previous._profile["seen"]
                keys = {k for k in set(s._facets) | set(s._emb) if "@" not in k} - self._profile["seen"]
                a = previous._rank(previous._model, old_keys, person, display=True)[:30]
                b = self._rank(m, keys, person, display=True)[:30]
                calibration["overlap"] = {"jaccard": len(set(a) & set(b))/len(set(a) | set(b)) if a or b else 1.,
                                          "tau": kendall(a, b), "intersection": len(set(a) & set(b))}
        coverage = {}
        for adult in s.adult_people():
            titles = {(e["key"], e["kind"]) for e in s.person_entries(self._profile, adult) if e["weight"] > 0}
            coverage[adult] = {"learned": sum(bool(s.frec(k, kind)) for k, kind in titles), "total": len(titles)}
        return plain({"person": person, "maturity": s.maturity(m, person), "axes": s.taste_axes(m, person),
            "coverage": coverage,
            "movers": {"window": {"since": since, "cutoff": cutoff, "events": len(window),
                                  "undated": sum(ev["t"] is None for ev in events),
                                  "baseline": bool(dated and cutoff >= min(dated))}, "facets": facets, "decay": decay},
            "rows": rows, "calibration": calibration})

    def pool(self, tmdb=None):
        """Inspect today's seeds and cached build counts, or replay with a supplied offline TMDB."""
        s = self._server
        p = s.adult_profile(s._profiles.get('household')) or {'entries': [], 'seen': set(), 'negative': set()}
        feed = s.feed(s.rid('foryou'), s.LANG)
        cached = (feed.get('names') or {}).get('pool')
        report = {}
        if cached is not None and tmdb is None:
            report = copy.deepcopy(cached)
            s.pool_seeds(p, report)
            counts = {(e['key'], e['kind']): e for e in cached['seeds']}
            for e in report['seeds']:
                old = counts.get((e['key'], e['kind']))
                e.update({k: old[k] if old else None for k in ('candidates', 'kept')})
            report['counts_built_at'] = feed.get('built')
            report['selection_matches_build'] = {(e['key'], e['kind']) for e in report['seeds'] if e['chosen']} == \
                                                {(e['key'], e['kind']) for e in cached['seeds'] if e['chosen']}
        else:
            with patch.object(s, 'tmdb', tmdb or s.tmdb):
                s.candidate_pool(p, metas=True, report=report)
            report['counts_built_at'] = None
            report['selection_matches_build'] = True
        cards = feed.get('metas', {}).get('tv') or []
        report['unique_foryou_titles'] = len({(x['id'], x['type']) for x in cards})
        return report

    def lab(self):
        from .lab import view
        return view(self._work, self._server.CONFIG)

    def moments(self):
        from .metrics import moments_report
        return moments_report(self._work, self._server.CONFIG)

    def week(self, weeks=1, person=None):
        s = self._server
        latest = max((r['week'] for r in s._learn.get('week', [])), default=None)
        return s.week_history(weeks, person, week=latest)

    def rows(self, lang=None):
        s = self._server
        lang = lang or s.LANG
        if not s.LANG_RE.fullmatch(lang):
            raise ValueError("invalid language")
        rows = []
        for cid, types, name in s.feed_rows(lang):
            metas = s.row_metas(cid, "tv" if "tv" in types else types[0], lang)
            name = s.row_name(cid, lang, name, metas)
            rows.append({"id": cid, "name": name, "titles": [{"key": m["id"], "title": m.get("name", ""),
                         "kind": "movie" if m.get("type") == "movie" else "tv", "why": self.score(m["id"])["reasons"] or
                         [m.get("description", "").split(" · ")[0]]} for m in metas]})
        return {"lang": lang, "rows": rows}

    def reach(self, person=None, top=20):
        from .reach import view
        return view(self._server, person, top)

    def _rank(self, model, keys, person=None, display=False):
        score = self._server.person_score if display else self._server.score_key
        values = [(score(model, k, person, self._index.get(k, {}).get("kind"))[0], k) for k in keys]
        return [k for _, k in sorted(values, key=lambda v: (v[0] is None, -(v[0] or 0), v[1]))]

    def _moves(self, before, after):
        old, new = {k: i + 1 for i, k in enumerate(before)}, {k: i + 1 for i, k in enumerate(after)}
        keys = set(before[:50]) | set(after[:50])
        return sorted([{"key": k, "title": self._index.get(k, {}).get("title", k), "before": old.get(k),
                        "after": new.get(k), "move": old[k] - new[k] if k in old and k in new else None}
                       for k in keys if old.get(k) != new.get(k)],
                      key=lambda v: (-(abs(v["move"] or 0)), v["key"]))

    def react(self, events, person=None, facts=()):
        facts = tuple(facts)
        people = {f[0] for f in facts}
        if person is None and len(people) == 1:
            person = next(iter(people))
        self._person(person)
        s = self._server
        p = copy.deepcopy(self._profile)
        by_key = {e["key"]: e for e in p["entries"]}
        feedback, imports = [], []
        for event in events:
            event = {"who": person or "both", **event}
            if "src" in event:
                imports.append({"t": time.strftime("%Y-%m-%d", time.gmtime()), "id": "0" * 32, **s.validate_import(event)})
            else:
                validated = s.validate_feedback(event)
                feedback.append({**validated, "t": time.time(), "id": __import__("uuid").uuid4().hex})
        if imports:
            s.merge_imports(p["entries"], by_key, time.time(), imports)
        if feedback:
            with s._remote_lock:
                existing = s.read_feedback()
            active, tonight = s.fold_feedback(existing + feedback)
            state = {"active": active, "tonight": tonight,
                     "blocked": {key for (key, _), e in active.items() if e["action"] == "not_for_us"}}
            with patch.object(s, "feedback_state", return_value=state):
                s.merge_feedback(p["entries"], by_key, time.time())
                after_masks = s.remote_masks()
        else:
            after_masks = s.remote_masks()
        before_masks = s.remote_masks()
        if facts:
            with patch.object(s, "who_watching", return_value=(person or "both", "preview")):
                pools = self.rows()
        else:
            pools = self.rows()
        from .history import LOCK
        paths = [self._work / n for n in ("memory.json", "memory.prev.json", "memory.log.jsonl", LOCK)] if facts else []
        saved = {path: path.read_bytes() if path.exists() else None for path in paths}
        memory = copy.deepcopy(s._memory)
        try:
            for who, kind, about in facts:
                s.memory_tell(who, kind, about)
            p = self._profile_with(p)
            model = self._build(p)
            rows, vetoed, notes = {}, {}, {}
            hidden = s.veto_keys(person or "both")
            note_person = person or "together"
            score_person = person or ("together" if facts else None)
            for row in pools["rows"]:
                keys = [item["key"] for item in row["titles"]]
                who = s.ROW_OWNER.get(row["id"], score_person)
                before = [e["id"] for e in s.remote_order([{"id": k} for k in self._rank(self._model, keys, who)], before_masks)]
                after = [e["id"] for e in s.remote_order([{"id": k} for k in self._rank(model, keys, who)], after_masks)]
                cid = row["id"]
                removed = set(after) & hidden if cid not in s.VETO_EXEMPT else set()
                after = [k for k in after if k not in removed]
                vetoed[cid] = len(removed)
                notes[cid] = min(s.MEMORY_WHY_ROW, sum(s.memory_why(k, self._index.get(k, {}).get("kind"), note_person) is not None
                                                      for k in after)) if cid in s.PERSON_ROWS else 0
                rows[cid] = self._moves(before, after)
            universe = {k for k in set(s._emb) | set(s._facets) if "@" not in k}  # Internal slots are not Skald ids.
            keys = universe - p["seen"]
            before, after = self._rank(self._model, keys, score_person), self._rank(model, keys, score_person)
            scopes = (person,) if person else tuple(sorted(people)) if facts else (None,)
            deltas = []
            for pp in scopes:
                old_aff = self._model["paff"].get(pp, {}) if pp else self._model["aff"]
                aff = model["paff"].get(pp, {}) if pp else model["aff"]
                deltas += [{"id": f, "name": model["names"].get(f, f), "before": old_aff.get(f, 0),
                            "after": aff.get(f, 0), "delta": aff.get(f, 0) - old_aff.get(f, 0),
                            **({"person": pp} if len(scopes) > 1 else {})} for f in set(old_aff) | set(aff)]
            return plain({"person": person, "rows": rows, "top50": self._moves(before, after),
                          "facets": sorted([d for d in deltas if abs(d["delta"]) > 1e-6], key=lambda d: -abs(d["delta"])),
                          "vetoed": vetoed, "notes": notes,
                          "memory": s.memory_view() if facts else [],
                          "simulation": "taste reranking and memory vetoes of fixed served pools" if facts else
                                        "taste reranking of fixed served pools; feed membership is unchanged", "writes": False})
        finally:
            if facts:
                for path, data in saved.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(data)
                s._memory = memory
                s._memory_index.clear()
                s._memory_veto.clear()

    def ingest(self, events):
        s = self._server
        # Persistence targets the caller's data directory; scoring always uses a private copy.
        with patch.object(s, "DATA", str(self.data_dir)):
            added = s.write_imports(events)
        if (self.data_dir / "imports.jsonl").exists():
            shutil.copyfile(self.data_dir / "imports.jsonl", self._work / "imports.jsonl")
        self._refresh()
        return {"events": added, "added": len(added)}

    def metrics(self, days=None):
        from .metrics import report
        return report(self._work, self._server.CONFIG, days=days)

    def memory(self, person=None, explain=None, now=None):
        self._person(person)
        s = self._server
        r = s.memory_daily(time.time() if now is None else now, m=self._model, dry_run=True)
        if explain:
            return {"fact": s.memory_explain(explain, store=r["store"])}
        return {"dry_run": True, "ops": r["ops"], "refused": r["refused"],
                "facts": s.memory_view(person, now=now, store=r["store"])}

    def learn(self):
        s = self._server
        stats = s.learn_stats()
        for field in ("recent_hits", "recent_plays"):
            stats[field] = [{k: (time.strftime("%Y-%m-%d", time.gmtime(v)) if k == "at" else v)
                             for k, v in e.items() if k not in ("hour", "part")} for e in stats[field]]
        stats["moodpicks"] = {day: {k: v for k, v in pick.items() if k != "at"} for day, pick in stats["moodpicks"].items()}
        stats["tired_titles"] = [{"key": k, "title": title} for k, title in sorted(s.tired_keys().items())]
        stats["credited"] = {k: time.strftime("%Y-%m-%d", time.gmtime(at)) for k, at in s._learn["credited"].items()}
        return plain(stats)


def suggestions(data, person, config, dismiss=None):
    """Read local cached inputs; only an explicit dismissal writes a private file."""
    from .intake import write_private
    from .suggest import DAY, suggest
    import hashlib
    data = Path(data)
    def read(name, default):
        path = data / name
        return json.loads(path.read_text()) if path.exists() else default
    children = {p for p, prefs in config["taste"].get("people", {}).items() if prefs.get("prefs", {}).get("kids_age") is not None}
    searches = [json.loads(line) for line in (data / "searches.jsonl").read_text().splitlines() if line.strip()] if (data / "searches.jsonl").exists() else []
    cache = read("facets.json", {}).get("items", {})
    sources = [("history.json", None)] + [(source + "-" + person + ".history.json", person)
        for source, cid in (("trakt", "trakt_api"), ("mdblist", "mdblist"), ("stremio", "stremio"), ("pmdb", "pmdb"))
        if person in config["connectors"].get(cid, {}).get("accounts", {})]
    plays = []
    for filename, owner in sources:
        for item in read(filename, {}).get("items", []):
            if (owner or item.get("person") or item.get("profile")) != person or not item.get("at"):
                continue
            if item.get("src") == "sync:pmdb" and not item.get("played"):
                continue
            if not (item.get("played") or (item.get("progress") or 0) >= .25 or (item.get("episodes_played") or 0) >= 1):
                continue
            key = item.get("id")
            kind = "movie" if item.get("type") == "movie" else "tv" if item.get("type") == "series" else None
            facts = cache.get(key, {})
            plays.append({"key": key, "person": person, "t": item["at"] / 1000, "kind": kind,
                          "genres": item.get("genres") or facts.get("g") or [],
                          "origin_country": item.get("origin_country") or facts.get("oc") or []})
    plays = list({(r["person"], r["t"], r.get("key")): r for r in plays}.values())
    now = time.time()
    dismissed = read("suggest-dismissed.json", [])
    rows = [] if person in children else suggest([r for r in searches if r.get("p") == person], plays,
        config["region"]["tz"], read("wishes.json", []) + read("shelves.json", {}).get("wishes", []), dismissed, now)
    for r in rows:
        r["id"] = hashlib.sha256(json.dumps([person, r["text"]], ensure_ascii=False).encode()).hexdigest()[:8]
    if dismiss:
        hit = next((r for r in rows if r["id"] == dismiss), None)
        if hit is None:
            raise ValueError("unknown suggestion id for this person")
        dismissed = [r for r in dismissed if r.get("t", 0) >= now - 90 * DAY]
        dismissed.append({"person": person, "text": hit["text"], "t": now})
        write_private(data / "suggest-dismissed.json", json.dumps(dismissed, ensure_ascii=False) + "\n")
        rows = [r for r in rows if r is not hit]
    return {"person": person, "suggestions": rows}
