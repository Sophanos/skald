"""Local export intake; only public title metadata can reach the decide API."""
from collections import Counter, defaultdict
from contextlib import ExitStack, nullcontext
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import urllib
from unittest.mock import patch

from .members import PEOPLE
from .ingest import EXPLICIT, PRIORITY, clean_title, parse, rating_signal, sources
from .ingest.resolve import TAU, Resolver, event, remote_item, remote_provider


def write_private(path, text):
    path = Path(path)
    from .erase import KEEP, epoch, erased, locked
    data = next((parent for parent in path.parents if any((parent/name).exists() for name in
                 ('skald.json', 'legacy.json', '.skald-format', '.skald-erased'))), None)
    with locked(data) if data is not None else nullcontext():
        for parent in path.parents:
            if erased(parent):
                if path.parent != parent or path.name not in KEEP:
                    raise ValueError('Household erased; run python -m skald setup household')
                break
        generation = epoch(data) if data is not None else None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, name = tempfile.mkstemp(prefix=".intake-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with locked(data) if data is not None else nullcontext():
            if data is not None and epoch(data) != generation:
                raise ValueError('Household erased; run python -m skald setup household')
            os.replace(name, path)
        if os.name == 'posix':
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                pass  # Some platforms cannot open directories for syncing.
            else:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def _mapping(items):
    mapping = {}
    for item in items:
        name, separator, person = item.rpartition("=")
        if not separator or not name or person not in (*PEOPLE, "both"):
            raise ValueError("--profile-map: unknown person or invalid NAME=PERSON")
        mapping[name] = person
    return mapping


def _trusted(server, source):
    if source not in sources():
        raise ValueError("unknown source")
    if source not in server.IMPORT_TRUST:
        raise ValueError("source needs a server trust row")


def _candidates(source, candidates):
    for c in candidates:
        if c.src != source:
            raise ValueError("candidate source differs from adapter")
        clean_title(c.title)
        if c.person not in ("", *PEOPLE, "both"):
            raise ValueError("unknown person")
        if not isinstance(c.year, str) or (c.year and (len(c.year) != 4 or not c.year.isascii() or not c.year.isdigit())):
            raise ValueError("invalid candidate year")
        if c.kind not in ("", "movie", "tv"):
            raise ValueError("invalid candidate kind")
    return candidates


def collect(specs, person, profile_map, *, server, stats=None, temp_dir=None):
    grouped = defaultdict(list)
    for source, path in specs:
        _trusted(server, source)
        grouped[source].append(Path(path))
    out = []
    for source, paths in grouped.items():
        hashes, candidates = [], []
        title_counts = {}
        for path in paths:
            try:
                if not path.exists():
                    raise FileNotFoundError()
                if path.is_dir() and not sources()[source].SOURCE["dir"]:
                    raise ValueError("source needs a file")
                with ExitStack() as stack:
                    input_path = path
                    stream = any(p == Path("/dev/stdin") or (
                        p.parent in (Path("/dev/fd"), Path("/proc/self/fd")) and p.name.isdigit())
                        for p in (path, path.resolve()))
                    try:
                        stream = stream or os.path.samestat(path.stat(), os.fstat(0))
                    except OSError:
                        pass  # Stdin may be closed during a regular-file import.
                    if stream or (not path.is_file() and not path.is_dir()):
                        if temp_dir is not None:
                            Path(temp_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
                        temp = stack.enter_context(tempfile.TemporaryDirectory(prefix="skald-export-", dir=temp_dir))
                        input_path = Path(temp) / path.name
                        h = hashlib.sha256()
                        with path.open("rb") as src, input_path.open("xb") as dest:
                            os.fchmod(dest.fileno(), 0o600)
                            for chunk in iter(lambda: src.read(65536), b""):
                                dest.write(chunk)
                                h.update(chunk)
                        hashes.append(h.hexdigest())
                    else:
                        files = list(path.rglob("*")) if path.is_dir() else [path]
                        if source == "chrome":
                            files += [Path(str(path) + s) for s in ("-wal", "-shm") if Path(str(path) + s).is_file()]
                        for file in files:
                            if file.is_file():
                                h = hashlib.sha256()
                                with file.open("rb") as fh:
                                    for chunk in iter(lambda: fh.read(65536), b""):
                                        h.update(chunk)
                                hashes.append(h.hexdigest())
                    local = {}
                    candidates += parse(source, input_path, profile_map=profile_map, stats=local, temp_dir=temp_dir, title_counts=title_counts)
                    _candidates(source, candidates)
                if stats is not None:
                    source_stats = stats.setdefault("_sources", {}).setdefault(source, Counter())
                    for key, value in local.items():
                        if key.startswith("trakt_"):
                            source_stats[key] = stats[key] = value
                        else:
                            source_stats[key] += value
                            stats[key] = stats.get(key, 0) + value
            except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, OverflowError) as error:
                if isinstance(error, FileNotFoundError):
                    reason = "file not found"
                elif isinstance(error, sqlite3.Error):
                    reason = "not a Chrome History database"
                elif isinstance(error, OSError):
                    reason = "could not read export"
                elif isinstance(error, UnicodeError):
                    reason = "invalid export encoding"
                elif isinstance(error, json.JSONDecodeError):
                    reason = "invalid export JSON"
                elif isinstance(error, ValueError) and str(error).startswith(("invalid ", "unmapped profile ", "unrecognised export date", "rating outside source scale", "no supported ", "source needs ", "candidate source differs")):
                    reason = str(error)
                    if reason.startswith("invalid literal"):
                        reason = "invalid export number"
                else:
                    reason = "invalid export value or structure"
                raise ValueError(f"{source} ({path.name}): {reason}") from None
        _candidates(source, candidates)
        digest = hashlib.sha256(json.dumps(sorted(hashes)).encode()).hexdigest()[:16]
        out.append((source, digest, candidates))
    return out


def _provider(engine, *, required=True):
    from . import remote
    index = remote.request("/api/v1/decide")
    rows = index.get("types", []) if isinstance(index, dict) else []
    if not any(isinstance(x, dict) and x.get("type") == "resolve_title" and x.get("available") is True for x in rows):
        if required:
            raise ValueError("remote resolve_title unavailable")
        return None
    provider = remote_provider()
    def checked(batch, max_usd):
        for item in batch:
            engine._server.check(engine._server.DECISIONS["resolve_title"]["inp"], remote_item(item))
        return provider(batch, max_usd)
    checked.remote = True
    checked.transport = provider
    return checked


def resolve(engine, state, candidates, *, online, remote, max_ai_usd, dry_run, cache_only=False):
    s = engine._server
    with ExitStack() as stack:
        if online and not cache_only:
            if not os.environ.get("TMDB_API"):
                raise ValueError("online resolution requires TMDB_API in the environment")
            for name, value in (("API_KEY", os.environ["TMDB_API"]), ("urllib", urllib), ("tmdb", engine._network_tmdb)):
                stack.enter_context(patch.object(s, name, value))
        resolver = Resolver(s.tmdb, cache=state / "resolve.json", max_ai_usd=max_ai_usd,
                            namespace="tmdb" if online else "server" if remote else "snapshot")
        resolutions = resolver.resolve(candidates, persist=False, cache_only=cache_only)
        if remote and not online and not cache_only:
            from .remote import resolve_titles
            pending = [c for c, r in zip(candidates, resolutions) if r["method"] == "unresolved"]
            for candidate, result in zip(pending, resolve_titles(pending)):
                resolver.cache[resolver.cache_key(candidate)] = result
            resolutions = resolver.resolve(candidates, persist=False)
        if remote and not dry_run and not cache_only and max_ai_usd > 0 and (online or resolver.pending):
            resolver.decide = _provider(engine, required=online)
            resolutions = resolver.resolve(candidates, persist=False, cache_only=cache_only)
    return resolver, resolutions


def plan_hash(person, files, events):
    events = [{k: v for k, v in e.items() if k not in ("t", "id")} for e in events]
    events.sort(key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    raw = {"v": 1, "person": person, "files": dict(files), "events": events}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:16]


def cross_source(events, *, existing=(), profile=(), person):
    if person not in (*PEOPLE, "both"):
        raise ValueError("unknown person")
    counts = dict.fromkeys(("merged", "corroborated", "conflicts_new", "conflicts_existing",
                           "abandon_dropped", "inferred_dropped"), 0)
    # shortcut: new adapters tie at trust 0; add their trust when they join multi-source intake.
    trust = {"trakt": 1., "letterboxd": 1., "imdb": 1., "netflix": .8, "plain": .8, "chrome": .4, "youtube": .4}
    def explicit(e):
        # shortcut: inferred source confidence tops out at .8; carry provenance if a source exceeds TAU.
        return e.get("action") in EXPLICIT and e.get("conf", 1.) >= TAU
    def people(e):
        who = e.get("who", person)
        if who not in (*PEOPLE, "both"):
            raise ValueError("unknown person")
        return set(PEOPLE) if who == "both" else {who}
    def owners(e):
        by = set(e.get("by") or [])
        if e.get("who") in ("both", "tv") or "together" in by:
            by |= set(PEOPLE)
        if e.get("who") in PEOPLE:
            by.add(e["who"])
        return by
    known, positive, imported = defaultdict(list), set(), set()
    for e in existing:
        for who in people(e):
            imported.add((e["key"], who))
            if explicit(e):
                known[(e["key"], who)].append(e["action"])
            if e.get("action") in ("love", "like"):
                positive.add((e["key"], who))
    for e in profile:
        action = {"loved": "love", "liked": "like", "disliked": "not_for_us", "meh": "meh"}.get(e.get("signal"))
        rating = (e.get("item") or {}).get("rating")
        if rating not in (None, "") and e.get("who") != "import":
            action = rating_signal(rating)
        for who in owners(e):
            if action in EXPLICIT and (e.get("who") != "import" or (e["key"], who) not in imported) and e.get("conf", 1.) == 1.:
                known[(e["key"], who)].append(action)
            if e.get("weight", 0) > 0:
                positive.add((e["key"], who))
    events = list(events)
    separate = {e["key"] for e in events if e["who"] != "both"}
    events = [row for e in events for row in
              ([{**e, "who": who} for who in PEOPLE]
               if e["who"] == "both" and e["key"] in separate else [e])]
    grouped = defaultdict(list)
    for e in events:
        pairs = [(e["key"], who) for who in people(e)]
        if e["action"] == "abandon" and any(pair in positive for pair in pairs):
            counts["abandon_dropped"] += 1
            continue
        if not explicit(e) and any(known[pair] for pair in pairs):
            counts["inferred_dropped"] += 1
            continue
        if explicit(e) and any((action == "not_for_us") != (e["action"] == "not_for_us")
                               for pair in pairs for action in known[pair]):
            counts["conflicts_existing"] += 1
        grouped[(e["key"], e["who"])].append(e)
    out = []
    for rows in grouped.values():
        if any(e["action"] in ("seen", "like", "love") for e in rows):
            kept = [e for e in rows if e["action"] != "abandon"]
            counts["abandon_dropped"] += len(rows) - len(kept)
            rows = kept
        counts["merged"] += len(rows) - 1
        rated = [e for e in rows if explicit(e)]
        conflict = len({e["action"] == "not_for_us" for e in rated}) > 1
        counts["conflicts_new"] += conflict
        counts["corroborated"] += len({e["src"] for e in rows}) > 1 and not conflict
        choices = rated or rows
        if conflict:
            winner = max(choices, key=lambda e: (e.get("t", ""), trust.get(e["src"], 0), PRIORITY[e["action"]], e["src"]))
        else:
            winner = max(choices, key=lambda e: (PRIORITY[e["action"]], e["conf"], e.get("t", ""), trust.get(e["src"], 0), e["src"]))
        out.append({**winner, "n": max(e.get("n", 1) for e in rows)})
    return out, counts


def learned(engine, events, counts, person):
    lines = []
    if events:
        react = counts["_react"] if "_react" in counts else engine.react(events, None if person == "both" else person)
        names = engine._model["names"]
        moves = sorted((m for m in react["facets"] if m["delta"] > 0 and m["id"] in names),
                       key=lambda m: (-m["delta"], m["id"]))
        labels = [names[m["id"]] for m in moves[:2]]
        if labels:
            lines.append(f"{person.capitalize()}: {' and '.join(labels)} moved up most")
    abandoned = sum(e["action"] == "abandon" for e in events)
    if abandoned:
        lines.append(f"{abandoned} titles look abandoned from short or early viewing")
    if counts.get("corroborated"):
        noun = "title appears" if counts["corroborated"] == 1 else "titles appear"
        lines.append(f"{counts['corroborated']} {noun} in 2+ sources: counted once")
    if counts.get("conflicts_new"):
        lines.append(f"{counts['conflicts_new']} ratings disagree across exports: newest explicit rating counts")
    if counts.get("conflicts_existing"):
        lines.append(f"{counts['conflicts_existing']} ratings disagree with Skald: the newest explicit rating counts")
    if counts.get("skipped_short"):
        lines.append(f"{counts['skipped_short']} short Netflix previews ignored")
    if counts.get("abandon_dropped"):
        lines.append(f"{counts['abandon_dropped']} inferred abandons ignored for known positive titles")
    if counts.get("inferred_dropped"):
        lines.append(f"{counts['inferred_dropped']} inferred signals ignored where explicit feedback exists")
    if counts.get("skipped_type"):
        lines.append(f"{counts['skipped_type']} unsupported IMDb title types ignored")
    return [line[:90] for line in lines[:6]]


def preview(engine, accepted, person, *, resolutions=(), review=(), remote=None, counts=None):
    signals, methods = defaultdict(Counter), defaultdict(Counter)
    for e in accepted:
        signals[e["src"]][e["action"]] += 1
    for c, r in resolutions:
        methods[c.src][r["method"]] += 1
    react = engine.react(accepted, None if person == "both" else person) if accepted else None
    return {"signals": {s: dict(c) for s, c in signals.items()},
            "methods": {s: dict(c) for s, c in methods.items()},
            "review": dict(Counter(r["src"] for r in review)), "remote": remote,
            "react": {"facets": react["facets"][:8], "rows": react["rows"]} if react else None,
            "learned": learned(engine, accepted, {**(counts or {}), "_react": react}, person)}


def _prompt(text):
    print(text, end=" ", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


def _review(engine, candidates, resolutions, person, resolver, interactive, *, legacy=False):
    accepted, review, later = [], [], False
    for i, (c, r) in enumerate(zip(candidates, resolutions)):
        if not r["high_confidence"] and interactive and not later:
            print(f"Review: {c.title} ({c.year})", file=sys.stderr)
            for choice in r["choices"]:
                print(f"  tmdb:{choice['id']} {choice['title']} ({choice['year']}, {choice['kind']})", file=sys.stderr)
            if legacy:
                print("accept / skip / tmdb:N / later:", end=" ", file=sys.stderr, flush=True)
                reply = input().strip().lower()
            else:
                reply = _prompt("accept / skip / tmdb:N / later:").lower()
            if reply in ("accept", "a") and r["match"]:
                r = {**r, "conf": 1., "high_confidence": True}
            elif reply.startswith("tmdb:"):
                choice = engine._index.get(reply)
                if not choice:
                    raise ValueError("manual id not found in snapshot")
                r = resolver.resolution(choice, 1., "cache", r["choices"])
            elif reply == "later":
                later = True
            if r["high_confidence"]:
                resolver.cache[resolver.cache_key(c)] = r
                resolutions[i] = r
        if r["high_confidence"]:
            accepted.append(engine._server.validate_import(event(c, r, person)))
        else:
            review.append({"title": c.title, "year": c.year, "kind": c.kind, "src": c.src,
                           "person": c.person or person, **r})
    return accepted, review


def _remote(resolver, resolutions, enabled):
    transport = getattr(resolver.decide, "transport", resolver.decide)
    return {"enabled": enabled, "pending": sum(r["method"] == "unresolved" and bool(r["choices"]) for r in resolutions),
            "sent": getattr(transport, "sent", 0), "batches": getattr(transport, "batches", 0), "usd": resolver.spent,
            "receipts": list(dict.fromkeys(r["receipt"] for r in resolutions if r.get("receipt"))),
            "why": dict(Counter(r["why"] for r in resolutions if r.get("why")))}


def import_command(args, engine, state, *, parser=parse):
    _trusted(engine._server, args.source)
    mapping = _mapping(args.profile_map)
    stats, title_counts = {}, {}
    try:
        candidates = [c for path in args.files for c in parser(args.source, path, profile_map=mapping, stats=stats,
                      temp_dir=Path(os.environ.get("DATA_DIR", state)), title_counts=title_counts)]
    except ValueError as error:
        if str(error).startswith("invalid Trakt zip"):
            raise
        raise ValueError("invalid export; check source and profile mapping") from None
    _candidates(args.source, candidates)
    resolver, resolutions = resolve(engine, state, candidates, online=args.resolve_online, remote=args.remote,
                                    max_ai_usd=args.max_ai_usd, dry_run=args.dry_run)
    accepted, review = _review(engine, candidates, resolutions, args.person, resolver,
                               sys.stdin.isatty() and not args.yes, legacy=True)
    accepted, merge = cross_source(accepted, existing=engine._server.read_imports(),
                                   profile=engine._profile["entries"], person=args.person)
    merge.update(stats)
    label = ("TMDB search" if args.resolve_online else "server TMDB search") + " + decide API" if args.remote else \
            "TMDB search" if args.resolve_online else "offline snapshot metadata + cached matches"
    result = {"candidates": len(candidates), "events": accepted, "review": review, "ai_usd": resolver.spent,
              "dry_run": args.dry_run, "resolution": label, "remote": _remote(resolver, resolutions, args.remote),
              "merge": merge, "learned": learned(engine, accepted, merge, args.person)}
    if not args.dry_run:
        _json(state / "resolve.json", resolver.cache)
        pairs = {(args.source, args.person)} | {(c.src, c.person or args.person) for c in candidates}
        pending = _save_review(state, review, pairs)
        result.update(engine.ingest(accepted))
        result["_persist"] = {"log": result["added"] > 0, "undone": []}
        if pending:
            result["review_file"] = "review.json"
    return result


def _state(state):
    path = state / "onboard.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {"v": 1, "people": {}}
        if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("people"), dict):
            raise ValueError()
        for person, records in data["people"].items():
            if person not in (*PEOPLE, "both") or not isinstance(records, dict):
                raise ValueError()
            for source, r in records.items():
                if not isinstance(source, str) or not isinstance(r, dict) or r.get("status") not in ("previewed", "applied", "skipped", "undone") \
                        or any(not isinstance(r.get(k), str) for k in ("file", "at")) \
                        or not isinstance(r.get("counts"), dict) or not isinstance(r.get("added"), list) \
                        or any(not isinstance(i, str) or len(i) != 32 or any(c not in "0123456789abcdef" for c in i) for i in r["added"]) \
                        or any(type(r["counts"].get(k)) is not int or r["counts"][k] < 0 for k in ("candidates", "accepted", "review")) \
                        or not isinstance(r.get("receipts", []), list) or any(not isinstance(i, str) for i in r.get("receipts", [])) \
                        or r.get("plan") is not None and not isinstance(r["plan"], str) \
                        or "events" in r and (not isinstance(r["events"], list) or any(not isinstance(e, dict) or
                            any(not isinstance(e.get(k), str) for k in ("key", "kind", "title", "year", "who", "action", "src")) or
                            type(e.get("conf")) not in (int, float) or not math.isfinite(e["conf"]) or not 0 <= e["conf"] <= 1 or
                            type(e.get("n")) is not int or e["n"] < 1 for e in r["events"])):
                    raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise ValueError("invalid onboard.json; run: skald doctor") from None


def _save_review(state, review, pairs):
    path = state / "review.json"
    old = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(old, list):
        raise ValueError("invalid local review")
    merged = [r for r in old if (r["src"], r["person"]) not in pairs] + review
    _json(path, merged)
    return bool(merged)


def _result(person, mode):
    return {"v": 1, "person": person, "mode": mode, "sources": [],
            "remote": {"enabled": False, "pending": 0, "sent": 0, "batches": 0, "usd": 0., "receipts": [], "why": {}},
            "react": None, "plan": None, "added": 0, "review_file": None, "next": []}


def _source_rows(server):
    from .guide import HOWTO
    rows = [{"name": name, **m.SOURCE, "trust": server.trust("import:" + name),
             "tainted": server.tainted({"src": "import:" + name}), "usable": name in server.IMPORT_TRUST}
            for name, m in sources().items()]
    for row in rows:
        if row["name"] in HOWTO:
            row["howto"] = HOWTO[row["name"]]
    for name in ("utterance", "agent:<client>"):
        rows.append({"name": name, "hint": "later", "dir": False, "profiles": False,
                     "trust": server.trust(name), "tainted": server.tainted({"src": name}), "usable": False})
    return rows


def _wizard(args, server):
    from .guide import quick
    mode = "quick" if getattr(args, "quick", False) else _prompt("Quick / Custom setup [Quick]:").lower() or "quick"
    if mode not in ("quick", "q", "custom", "c"):
        raise ValueError("choose Quick or Custom")
    if mode in ("quick", "q"):
        folder = _prompt("export folder [~/Downloads]:") or "~/Downloads"
        for source, path in quick(folder, server):
            if _prompt(f"use {source} ({path.name})? [y/N]").lower() == "y":
                args.source.append(f"{source}={path}")
    else:
        for row in _source_rows(server):
            if row["usable"]:
                print(f"{row['name']}: {row['hint']}", file=sys.stderr)
        while source := _prompt("source (blank to finish):"):
            _trusted(server, source)
            args.source.append(f"{source}={_prompt('path:')}")
    if not args.person:
        choices = [*server.PEOPLE, "both"]
        args.person = _prompt("who (" + " / ".join(choices) + "):").lower()
        if args.person not in choices:
            raise ValueError("choose " + ", ".join(choices))
    if not args.source:
        raise ValueError("no exports selected; use skald onboard --sources")


def onboard(args, engine, state):
    from . import guide
    interactive = sys.stdin.isatty() and not args.non_interactive
    book = _state(state)
    guide.review(state)
    if args.sources:
        return {**_result(args.person, "sources"), "sources": _source_rows(engine._server)}
    if args.status:
        rows = [{"name": source, "person": p, "file": record["file"], "status": record["status"], **record["counts"]}
                for p, entries in book["people"].items() if not args.person or p == args.person
                for source, record in entries.items()]
        return {**_result(args.person, "status"), "sources": rows}
    if interactive and not args.source and not args.undo and not getattr(args, "reset", False) \
            and (not args.person or getattr(args, "quick", False)):
        _wizard(args, engine._server)
    if not args.person:
        raise ValueError("onboard needs --person")
    previous = book["people"].get(args.person, {})
    if getattr(args, "reset", False):
        if args.dry_run or args.source:
            raise ValueError("--reset cannot use --dry-run or --source; reset first, then onboard")
        if not args.yes and (not interactive or _prompt(f"undo all onboard sources for {args.person}? [y/N]").lower() != "y"):
            raise ValueError("--reset needs confirmation or --yes")
        changed = 0
        for source in previous:
            flags = SimpleNamespace(**{**vars(args), "reset": False, "undo": source})
            changed += onboard(flags, engine, state)["changed"]
        return {**_result(args.person, "reset"), "changed": changed}
    if args.undo:
        if args.dry_run:
            raise ValueError("--undo cannot be combined with --dry-run")
        record = previous.get(args.undo)
        if record is None or record["status"] == "undone":
            return {**_result(args.person, "undo"), "changed": 0}
        from .history import folded_imports
        live_ids = {e["id"] for e in engine._server.read_imports() + folded_imports(engine._server.DATA)}
        ids = [i for i in record["added"] if i in live_ids]
        with patch.object(engine._server, "DATA", str(engine.data_dir)):
            for i in ids:
                engine._server.undo_import(i)
        from .__main__ import persist
        persist(engine, state, {"log": bool(ids), "undone": record["added"]})
        record["status"], record["at"] = "undone", dt.date.today().isoformat()
        _save_review(state, [], {(args.undo, args.person)})
        _json(state / "onboard.json", book)
        return {**_result(args.person, "undo"), "changed": len(ids), "sources": [{"name": args.undo, "file": record["file"],
                "status": "undone", **record["counts"]}]}
    if args.yes and not args.plan and not interactive and not args.dry_run:
        raise ValueError("non-interactive apply needs --yes --plan")
    specs = []
    for spec in args.source:
        source, sep, path = spec.partition("=")
        if not sep or not path:
            raise ValueError("--source must be NAME=PATH")
        specs.append((source, path))
    if not specs and interactive:
        for row in _source_rows(engine._server):
            if row["usable"]:
                print(f"{row['name']}: {row['hint']}", file=sys.stderr)
        while source := _prompt("source (blank to finish):"):
            _trusted(engine._server, source)
            specs.append((source, _prompt("path:")))
    if not specs:
        raise ValueError("onboard needs --source")
    stats = {}
    collected = collect(specs, args.person, _mapping(args.profile_map), server=engine._server, stats=stats,
                        temp_dir=Path(os.environ.get("DATA_DIR", state)))
    skipped = {s for s, h, _ in collected if previous.get(s, {}).get("file") == h
               and previous[s]["status"] in ("applied", "skipped") and not args.again}
    candidates = [c for s, _, cs in collected if s not in skipped for c in cs]
    cache_only = bool(args.yes and args.plan)
    resolver, resolutions = resolve(engine, state, candidates, online=args.resolve_online,
                                    remote=args.remote, max_ai_usd=args.max_ai_usd,
                                    dry_run=args.dry_run or interactive, cache_only=cache_only)
    if interactive and args.remote and not args.dry_run and not cache_only and args.max_ai_usd > 0 and resolver.pending:
        if _prompt(f"send {resolver.pending} titles (title, year, kind only) to the decide API? [y/N]").lower() == "y":
            resolver.decide = _provider(engine, required=args.resolve_online)
            resolutions = resolver.resolve(candidates, persist=False)
    accepted, review = _review(engine, candidates, resolutions, args.person, resolver,
                               interactive and not args.yes and not cache_only)
    if cache_only:
        # Resume the reviewed winners; a partial apply must not promote their duplicate sources.
        replay = []
        for source, digest, _ in collected:
            old = previous.get(source, {})
            rows = [e for e in accepted if e["src"] == source]
            if old.get("file") == digest and old.get("plan") == args.plan and "events" in old:
                fields = ("key", "kind", "title", "year", "action", "conf")
                replay += [e for e in old["events"] if any(
                    (e["who"] == r["who"] or r["who"] == "both" and e["who"] in PEOPLE)
                    and all(e.get(k) == r.get(k) for k in fields) for r in rows)]
            else:
                replay += rows
        accepted = replay
    accepted, merge = cross_source(accepted, existing=engine._server.read_imports(),
                                   profile=engine._profile["entries"], person=args.person)
    merge.update(stats)
    files = {s: h for s, h, _ in collected}
    planned = list(accepted)
    for source, _, cs in collected:
        if source in skipped:
            old = previous[source]
            if "events" in old:
                planned.extend(old["events"])
            else:
                for c in cs:
                    r = resolver.cache.get(resolver.cache_key(c))
                    if r and r["high_confidence"]:
                        planned.append(engine._server.validate_import(event(c, r, args.person)))
    plan = plan_hash(args.person, files, planned)
    if args.plan and plan != args.plan:
        raise ValueError("plan changed; preview again")
    result = _result(args.person, "dry_run" if args.dry_run else "preview")
    result.update(plan=None if args.dry_run else plan, remote=_remote(resolver, resolutions, args.remote))
    summary = preview(engine, accepted, args.person, resolutions=zip(candidates, resolutions), review=review,
                      remote=result["remote"], counts=merge)
    result.update(react=summary["react"], learned=summary["learned"], merge=merge)
    result["maturity"] = {p: guide.member(engine, p)[0] for p in (engine._server.PEOPLE if args.person == "both" else [args.person])}
    result["rated"] = [guide.member(engine, p)[1] for p in result["maturity"]]
    for source, digest, cs in collected:
        es = [e for e in accepted if e["src"] == source]
        counts = {"candidates": len(cs), "accepted": len(es), "review": summary["review"].get(source, 0),
                  "unsupported": sum(stats.get("_sources", {}).get(source, {}).get(k, 0) for k in ("skipped_type", "skipped_trakt")),
                  "signals": summary["signals"].get(source, {}), "methods": summary["methods"].get(source, {})}
        result["sources"].append({"name": source, "file": digest, "status": "skipped" if source in skipped else "previewed", **counts})
    result["next"] = guide.next_actions(engine, state, args.person, pending=len(review), pushed=guide.unpushed(engine, state))
    if interactive:
        from .__main__ import text_output
        text_output(args, result, sys.stderr)
    if args.dry_run:
        return result
    apply = args.yes or (interactive and _prompt(f"write {len(accepted)} events for {args.person}? [y/N]").lower() == "y")
    result["mode"] = "applied" if apply else "preview"
    result["review_file"] = "review.json" if review else None
    _json(state / "resolve.json", resolver.cache)
    pending = _save_review(state, review, {(c.src, c.person or args.person) for c in candidates})
    result["review_file"] = "review.json" if pending else None
    records = book["people"].setdefault(args.person, {})
    for row in result["sources"]:
        source = row["name"]
        old = records.get(source, {})
        added = []
        if apply and source not in skipped:
            added = engine.ingest([e for e in accepted if e["src"] == source])["events"]
        row["status"] = "skipped" if source in skipped else "applied" if apply else "previewed"
        if source in skipped:
            continue
        counts = {k: row[k] for k in ("candidates", "accepted", "review", "unsupported", "signals", "methods")}
        records[source] = {"file": row["file"], "status": row["status"], "at": dt.date.today().isoformat(),
                           "counts": counts,
                           "events": [e for e in accepted if e["src"] == source],
                           "added": list(dict.fromkeys(old.get("added", []) + [e["id"] for e in added])),
                           "receipts": list(dict.fromkeys(old.get("receipts", []) +
                               [r["receipt"] for c, r in zip(candidates, resolutions) if c.src == source and r.get("receipt")]))[-200:], "plan": plan}
        if added:
            # Keep undo ids before saving the log; a crash can safely retry this previewed source.
            records[source]["status"] = "previewed"
            _json(state / "onboard.json", book)
            from .__main__ import persist
            persist(engine, state, {"log": True, "undone": []})
            records[source]["status"] = "applied"
        _json(state / "onboard.json", book)
        result["added"] += len(added)
    result["_persist"] = {"log": False, "undone": []}
    result["next"] = guide.next_actions(engine, state, args.person)
    return result
