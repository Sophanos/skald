"""W12f local front door; these v1 dictionaries can also serve a client."""
import json
import os
from pathlib import Path
import re
import stat
import time
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from . import intake
from .engine import Engine


# Network verification is deliberately unavailable in this worktree.
HOWTO = {
    "trakt": {"steps": ["Open Trakt Settings → Data.", "Choose Export now and download the zip.", "Import the zip as is; no need to unzip."],
              "url": "https://app.trakt.tv/settings/data", "file_glob": ["trakt-export*.zip", "trakt*.json", "watched-*.json", "ratings-*.json", "watchlist-*.json", "history-*.json"], "wait": "minutes", "checked": None},
    "netflix": {"steps": ["Open Account → Profiles → Viewing activity.", "Choose the profile, then Download all.", "For a multi-profile export, add --profile-map NAME=PERSON."],
                "url": "https://help.netflix.com/en/node/101917", "file_glob": ["ViewingActivity*.csv", "NetflixViewingHistory*.csv"], "wait": "minutes", "checked": None},
    "letterboxd": {"steps": ["Open Settings → Import & Export.", "Export your data and unzip the download.", "Use the folder containing ratings.csv and diary.csv."],
                   "url": "https://letterboxd.com/settings/data/", "file_glob": ["letterboxd*", "ratings.csv", "diary.csv", "watched.csv"], "wait": "minutes", "checked": None},
    "imdb": {"steps": ["Open Your ratings or Your watchlist.", "Choose Export and download the CSV.", "Use Custom setup if the filename is simply ratings.csv."],
             "url": "https://help.imdb.com/article/imdb/track-movies-tv/ratings-faq/G67Y87TFYYP6TWAV", "file_glob": ["imdb*.csv", "IMDb*.csv"], "wait": "minutes", "checked": None},
    "youtube": {"steps": ["Open Google Takeout and select YouTube and YouTube Music.", "Select history; JSON or HTML format works.", "Create the export, download it, and unzip watch-history.json or watch-history.html."],
                "url": "https://takeout.google.com/", "file_glob": ["watch-history.json", "watch-history.html"], "wait": "days", "checked": None},
    "chrome": {"steps": ["Close Chrome before copying its History database.", "On macOS: ~/Library/Application Support/Google/Chrome/Default/History.", "On Linux: ~/.config/google-chrome/Default/History.", "On Windows: %LOCALAPPDATA%/Google/Chrome/User Data/Default/History."],
               "url": "https://chromium.googlesource.com/chromium/src/+/main/docs/user_data_dir.md", "file_glob": ["History"], "wait": "minutes", "checked": None},
}


def quick(folder, server):
    """Look at immediate filenames only; callers confirm before collect opens them."""
    from .ingest import sources
    children = sorted(Path(folder).expanduser().iterdir())
    return [(name, path) for name, how in HOWTO.items() if name in sources() and name in server.IMPORT_TRUST
            for path in children if not path.is_symlink() and any(path.match(g) for g in how["file_glob"])]


def review(state):
    path = state / "review.json"
    try:
        rows = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(rows, list) or any(not isinstance(r, dict) or
                any(not isinstance(r.get(k), str) for k in ("src", "person", "title")) for r in rows):
            raise ValueError()
        return rows
    except (ValueError, UnicodeError):
        raise ValueError("invalid review.json; run: skald doctor") from None


def member(engine, person, profile=None):
    profile = profile if profile is not None else engine.profile(person)
    maturity = profile.get("maturity")
    explicit = engine._model.get("nexp", {}).get(person, 0)
    if isinstance(maturity, dict):
        count = maturity.get("explicit", maturity.get("rated", explicit))
        target = maturity.get("need", maturity.get("target", 20))
        label = "too new — " if maturity.get("state") == "too_new" else ""
        line = f"{person.title()}: {label}{count} of {target} rated"
        if label:
            line += f"; next: skald interview --person {person} or import a Netflix or IMDb export (skald import --help)"
    else:
        maturity = None
        line = f"{person.title()}: explicit {explicit}"
    return maturity, line


def unpushed(engine, state):
    path = state / "imports.jsonl"
    if not path.exists():
        return 0
    # shortcut: the push API stores no acknowledgement ledger; compare local ids with the snapshot on a later pull.
    local = {json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()}
    undone = json.loads((state / "undone.json").read_text()) if (state / "undone.json").exists() else []
    return len(local - set(undone) - getattr(engine, "snapshot_import_ids", set()))


def next_actions(engine, state, person, *, pending=None, pushed=None):
    who = list(engine._server.PEOPLE) if person == "both" else [person]
    actions = []
    for p in who:
        maturity, _ = member(engine, p)
        if maturity and maturity.get("state") == "too_new":
            actions.append({"id": "interview", "say": f"Build {p.title()}'s taste with an interview", "cli": f"skald interview --person {p}"})
    pending = sum(r["person"] in (*who, "both") for r in review(state)) if pending is None else pending
    if pending:
        actions.append({"id": "review", "say": f"{pending} titles await review; retry with resolution enabled", "cli": f"skald onboard --person {person} --resolve-online"})
    count = unpushed(engine, state) if pushed is None else pushed
    if count:
        actions.append({"id": "push", "say": "Push resolved local imports", "cli": "skald push imports --apply --remote"})
    return actions


def me(engine, state, person):
    profile = engine.profile(person)
    maturity, count = member(engine, person, profile)
    book, pending = intake._state(state), review(state)
    sources = [{"name": name, **{k: r[k] for k in ("file", "status", "at")},
                "counts": {k: r["counts"].get(k, 0) for k in ("candidates", "accepted", "review", "unsupported")}}
               for p, records in book["people"].items() if p in (person, "both") for name, r in records.items()]
    allowed = {e["title"] for e in profile["entries"] if person in e.get("by", []) or "together" in e.get("by", [])}
    top = {group: [{**f, "name": f["name"][:48], "anchors": [a[:60] for a in f["anchors"] if a in allowed][:2]}
                   for f in profile[group][:3]] for group in ("experiences", "themes", "creators")}
    s = engine._server
    facts = s.memory_cards(person, viewer=person, n=3)
    facts = [{**f, "text": f["text"][:140]} for f in facts]
    proposals = sum(f["status"] == "proposed" for f in s.memory_view(person, viewer=person))
    actions = next_actions(engine, state, person)
    actions += [{"id": "forget_source", "say": "Forget an export source", "cli": f"skald me --person {person} forget --source NAME"},
                {"id": "forget_import", "say": "Forget one imported event", "cli": f"skald me --person {person} forget --import ID"}]
    card = {"v": 1, "person": person, "say": f"{person.title()}, Skald has {profile['titles']} titles for you and {proposals} memory proposals."[:140],
            "maturity": maturity, "sources": sources, "top": top, "facts": facts,
            "pending": {"review": sum(r["person"] in (person, "both") for r in pending), "memory": proposals}, "actions": actions}
    # Bound the card without truncating JSON or omitting source/forget information.
    while len(json.dumps(card, ensure_ascii=False).encode()) > 4096:
        groups = [v for v in top.values() if v]
        if groups:
            max(groups, key=len).pop()
        elif facts:
            facts.pop()
        else:
            raise ValueError("personal card exceeds 4 KB")
    return card, count


def me_action(args, engine, state):
    if args.fact:
        if not re.fullmatch(r"m[1-9][0-9]*", args.fact):
            raise ValueError("invalid memory fact id")
        cli = f"skald memory {args.me} --person {args.person} --fact {args.fact}"
        return {"v": 1, "changed": 0, "say": "needs W10c", "cli": cli}, 3
    if args.me == "correct":
        raise ValueError("correct needs --fact")
    if args.source:
        flags = SimpleNamespace(person=args.person, sources=False, status=False, undo=args.source,
                                dry_run=False, non_interactive=True, reset=False)
        result = intake.onboard(flags, engine, state)
        return {"v": 1, "changed": result["changed"], "say": f"Forgot {result['changed']} imported events from {args.source}"}, 0
    if not args.import_id or not re.fullmatch(r"[0-9a-f]{32}", args.import_id):
        raise ValueError("forget needs --source, --import ID or --fact ID")
    target = next((e for e in engine._server.read_imports() if e["id"] == args.import_id), None)
    if target and target["who"] not in (args.person, "both"):
        raise ValueError("import belongs to another person")
    if target:
        with patch.object(engine._server, "DATA", str(engine.data_dir)):
            engine._server.undo_import(args.import_id)
    return {"v": 1, "changed": int(target is not None), "say": f"Forgot {int(target is not None)} imported events",
            "_persist": {"log": target is not None, "undone": [args.import_id] if target else []}}, 0


def connectors(engine, state, person=None, book=None):
    s, now = engine._server, time.time()
    rows = {c["id"]: {**c, "origin": "derived" if s._connectors_derived else "registry"}
            for c in s.connectors_view(now=now)["connectors"]}
    book = intake._state(state) if book is None else book
    for who, records in book.get("people", {}).items():
        for src, rec in records.items():
            cid, at = f"manual:{src}:{who}", s._connector_time(rec["at"])
            c = s._connector(cid, "manual_export", who, now, rec["at"], at, rec["counts"]["accepted"],
                             disabled=rec["status"] in ("skipped", "undone"))
            c.update(last_sync=at, origin="local", auth=None, reminder=s.connector_reminder(c, now))
            if c["reminder"] and c["health"]["state"] == "ok":
                c["health"].update(state="stale", at=now)
            c.pop("cursor")
            rows[cid] = c
    return {"connectors": [c for cid, c in sorted(rows.items()) if person is None or c["person"] in (person, "both")]}


def doctor(args, state):
    with tempfile.TemporaryDirectory(prefix="skald-doctor-") as temp:
        findings, checks = [], []
        def finding(cid, severity, message, hint=None):
            findings.append({"checkId": cid, "severity": severity, "message": message, "fixHint": hint})
        checks.append("data.readable")
        snapshot = Path(args.snapshot).resolve() if args.snapshot else None
        if args.live:
            from . import live
            if args.remote:
                snapshot = live.pull(Path(temp) / "live")
            else:
                finding("data.readable", "warning", "live reachability was not checked locally", "use --remote to opt into a live check")
                snapshot = state / "live"
        if snapshot is None or not snapshot.is_dir() or not any((snapshot / n).is_file() and os.access(snapshot / n, os.R_OK) for n in ("skald.json", "profile.json", "history.json", "trakt.json")):
            finding("data.readable", "error", "snapshot data is not readable", "set --snapshot DIR or SKALD_SNAPSHOT")
            snapshot = None
        if snapshot and (state == snapshot or state.is_relative_to(snapshot)):
            raise ValueError("state directory must be outside the read-only snapshot")
        checks.append("state.modes")
        if args.fix and not state.exists():
            state.mkdir(parents=True, mode=0o700)
        if not state.exists():
            finding("state.modes", "warning", "state directory is missing", "skald doctor --fix")
        else:
            for path in [state, *sorted(state.iterdir())]:
                if path.is_symlink() or not (path.is_dir() if path == state else path.is_file()):
                    continue
                target = 0o700 if path == state else 0o600
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode != target:
                    if args.fix:
                        os.chmod(path, target)
                    else:
                        finding("state.modes", "warning", f"{'state directory' if path == state else 'state file'} mode is {mode:04o}", "skald doctor --fix")
        book, pending = None, None
        for name, load in (("onboard", intake._state), ("review", review)):
            checks.append("state." + name)
            try:
                value = load(state)
                if name == "onboard":
                    book = value
                else:
                    pending = value
            except ValueError:
                finding("state." + name, "error", f"invalid {name}.json", "restore a valid local backup; doctor never deletes state")
        if book is not None:
            partial = sum(r["status"] == "previewed" for records in book["people"].values() for r in records.values())
            finding("state.onboard", "warning" if partial else "info", f"{partial} partial sources", "skald onboard --status" if partial else None)
        if pending is not None:
            finding("state.review", "warning" if pending else "info", f"{len(pending)} pending review", "skald onboard --resolve-online" if pending else None)
        checks.append("remote.configured")
        # Metadata only: never read remote.json, including in --fix.
        config = (snapshot or state) / "remote.json"
        try:
            mode = stat.S_IMODE(os.stat(config).st_mode)
            finding("remote.configured", "warning" if mode & 0o077 else "info", "remote config is present" + (" but not private" if mode & 0o077 else ""),
                    "make remote.json private (mode 0600)" if mode & 0o077 else None)
        except FileNotFoundError:
            finding("remote.configured", "info", "remote config is absent", None)
        if snapshot:
            with Engine(snapshot) as engine:
                checks.append("connectors.health")
                rows = connectors(engine, state, book=book or {"people": {}})["connectors"]
                errors = sum(c["health"]["state"] == "error" or bool(c.get("last_error")) for c in rows)
                reminders = [c["reminder"] for c in rows if c["reminder"]]
                warning = errors or (reminders and engine._server.CONNECTOR_REMINDER_WARNING)
                message = f"{errors} connector errors; {len(reminders)} export reminders"
                hint = "; ".join(reminders) if reminders else None
                if not any(c["kind"] != "availability" and c["health"]["state"] != "disabled" for c in rows):
                    hint = "Connect Trakt, drop a CSV export or paste titles; see docs/CONNECTORS.md"
                finding("connectors.health", "warning" if warning else "info", message,
                        hint)
                engine.snapshot_import_ids = {e["id"] for e in engine._server.read_imports()}
                checks.extend(["imports.unpushed", "facets.mixed_keys", "calibration.no_bg", "memory.pending"])
                count = unpushed(engine, state)
                finding("imports.unpushed", "warning" if count else "info", f"{count} unpushed imports", "skald push imports --apply --remote" if count else None)
                # Use the captured count because the loader has already removed mixed records.
                count = sum(int(m[1]) for line in engine.diagnostics if (m := re.search(r"keys: dropped (\d+) mixed", line)))
                finding("facets.mixed_keys", "warning" if count else "info", f"{count} mixed facet records ignored", "refresh snapshot facets" if count else None)
                finding("calibration.no_bg", "info", "no % offline", None)
                for person in engine._server.PEOPLE:
                    cid = "maturity." + person
                    checks.append(cid)
                    maturity, line = member(engine, person)
                    too_new = maturity.get("state") == "too_new" if maturity else engine._model.get("nexp", {}).get(person, 0) < 20
                    finding(cid, "warning" if too_new else "info", line, f"skald interview --person {person}" if too_new else None)
                count = sum(f["status"] == "proposed" for f in engine._server.memory_view())
                finding("memory.pending", "warning" if count else "info", f"{count} pending memory proposals", "skald memory" if count else None)
        return {"v": 1, "ok": not any(f["severity"] in ("warning", "error") for f in findings), "checksRun": checks, "findings": findings}
