"""python -m skald: inspect, simulate and import taste signals."""
import argparse
from contextlib import redirect_stdout
import io
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
import datetime
import sqlite3
import subprocess
from unittest.mock import patch

from .engine import Engine, data_files
from . import members
from .members import PEOPLE
from .ingest import parse, sources
from .ingest.resolve import normal
from . import intake, guide
from . import live, remote


def parser():
    p = argparse.ArgumentParser(prog="skald")
    source = p.add_mutually_exclusive_group()
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    source.add_argument("--snapshot", default=snapshot if snapshot and snapshot.strip() else None)
    source.add_argument("--live", action="store_true")
    p.add_argument("--state-dir", default=str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "skald"), help="resolved local imports and resolution cache")
    p.add_argument("--json", action="store_true")
    commands = p.add_subparsers(dest="command", help="command to run")

    def cmd(parent, name):
        descriptions = {"taste": "inspect taste and learning", "show": "show the taste profile", "why": "explain a title's fit",
                        "report": "summarize recent learning", "explain": "trace a title's score", "rows": "preview recommendation rows",
                        "react": "simulate a reaction", "learn": "inspect learning signals", "metrics": "show engine metrics",
                        "connectors": "show input health and sync cadence", "memory": "inspect memory proposals", "doctor": "check local setup and state", "keys": "inspect mixed title keys",
                        "import": "import one local export", "imports": "list or undo imports", "list": "list saved items", "undo": "undo a saved import",
                        "onboard": "set up and resume local exports", "me": "show what Skald knows about you", "forget": "forget imported signals",
                        "correct": "correct a memory fact", "push": "preview or push resolved imports", "decide": "run a structured decision", "interview": "rate interview anchors to start a taste",
                        "backup": "inspect or restore backups", "verify": "verify an archive", "restore": "restore an archive", "eval": "run offline evaluation",
                        "addon-url": "print private addon URLs for local DATA_DIR",
                        "trakt": "authorize the configured Trakt connector", "login": "sign in with a Trakt device code"}
        c = parent.add_parser(name, help=descriptions[name], description=descriptions[name])
        c.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return c

    def person(c, required=False):
        c.add_argument("--person", choices=PEOPLE if not required else (*PEOPLE, "both"), required=required)

    from . import setup
    setup.add_parser(commands)
    connect = commands.add_parser("connect", help="link this laptop to the API")
    connect.add_argument("url")
    connect.add_argument("--token-stdin", action="store_true")
    connect.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    addon = cmd(commands, "addon-url")
    addon.add_argument("--base")
    addon.add_argument("--rotate-addon-token", action="store_true")
    taste = cmd(commands, "taste").add_subparsers(dest="taste", required=True)
    person(cmd(taste, "show"))
    why = cmd(taste, "why")
    why.add_argument("title", nargs="+")
    person(why)
    why.add_argument("--trace", action="store_true")
    report = cmd(taste, "report")
    person(report)
    report.add_argument("--since", type=int, default=7)
    report.add_argument("--before")
    explain = cmd(commands, "explain")
    explain.add_argument("title", nargs="+")
    person(explain)
    rows = cmd(commands, "rows")
    rows.add_argument("--row")
    rows.add_argument("--n", type=int, default=10)
    rows.add_argument("--lang", default=members.CONFIG["region"]["lang"])
    react = cmd(commands, "react")
    react.add_argument("action", nargs="?", choices=("love", "loves", "like", "likes", "meh", "seen", "not_for_us", "not_tonight", "interest"))
    react.add_argument("title", nargs="*")
    react.add_argument("--tell", action="append", default=[], metavar="PERSON:like|avoid:FACET_ID")
    react.add_argument("--reason", action="append", default=[])
    person(react)
    connector = cmd(commands, "connectors")
    person(connector)
    probes = connector.add_subparsers(dest="connector_action")
    probe = probes.add_parser("test", help="read-only live connector check")
    probe.add_argument("name", nargs="?")
    probe.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    probe.add_argument("--show", action="store_true")
    cmd(cmd(commands, "trakt").add_subparsers(dest="trakt", required=True), "login").add_argument(
        "--person", "--member", dest="person", choices=PEOPLE if not members.CONFIG_ERROR else None)
    cmd(cmd(commands, "login").add_subparsers(dest="login", required=True), "trakt").add_argument(
        "--person", "--member", dest="person", choices=PEOPLE if not members.CONFIG_ERROR else None)
    cmd(commands, "learn")
    cmd(commands, "metrics").add_argument("--days", type=int, default=21)
    memory = cmd(commands, "memory")
    person(memory)
    memory.add_argument("--explain")
    check = cmd(commands, "doctor")
    check.add_argument("--fix", action="store_true", help="create state directory and repair local modes only")
    check.add_argument("--remote", action="store_true", help="allow an explicit live reachability check")
    doctor = check.add_subparsers(dest="doctor", help="optional diagnostic detail")
    cmd(doctor, "keys")
    me = cmd(commands, "me")
    person(me)
    me.add_argument("--remote", action="store_true", help="allow an explicit live snapshot pull")
    actions = me.add_subparsers(dest="me", help="optional correction or forget action")
    for name in ("forget", "correct"):
        action = cmd(actions, name)
        person(action)
        action._option_string_actions["--person"].default = argparse.SUPPRESS
        target = action.add_mutually_exclusive_group(required=True)
        target.add_argument("--source", help="onboard source to forget")
        target.add_argument("--import", dest="import_id", help="import event id to forget")
        target.add_argument("--fact", help="memory fact id; changes are not implemented yet")
    imp = cmd(commands, "import")
    imp.add_argument("source", choices=tuple(sources()))
    imp.add_argument("files", nargs="+")
    person(imp, required=True)
    imp.add_argument("--yes", action="store_true")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--profile-map", action="append", default=[], metavar="NAME=PERSON")
    imp.add_argument("--max-ai-usd", type=float, default=.05)
    imp.add_argument("--resolve-online", action="store_true", help="opt-in: TMDB search; exports stay local")
    imp.add_argument("--remote", action="store_true", help="resolve titles through the linked server")
    imports = cmd(commands, "imports").add_subparsers(dest="imports", required=True)
    cmd(imports, "list")
    undo = cmd(imports, "undo")
    undo.add_argument("id")
    on = cmd(commands, "onboard")
    on.add_argument("--person", choices=(*PEOPLE, "both"))
    mode = on.add_mutually_exclusive_group()
    mode.add_argument("--sources", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--undo", metavar="SOURCE")
    mode.add_argument("--reset", action="store_true", help="undo this person's onboard sources after confirmation")
    on.add_argument("--quick", action="store_true", help="scan one folder by known export filenames, confirming every match")
    on.add_argument("--verbose", action="store_true", help="show rank moves, facet deltas and loader diagnostics")
    on.add_argument("--source", action="append", default=[], metavar="NAME=PATH")
    on.add_argument("--remote", action="store_true")
    on.add_argument("--resolve-online", action="store_true")
    on.add_argument("--dry-run", action="store_true")
    on.add_argument("--yes", action="store_true", help="apply a reviewed import plan; scripted runs also need --plan")
    on.add_argument("--plan")
    on.add_argument("--non-interactive", action="store_true")
    on.add_argument("--again", action="store_true")
    on.add_argument("--profile-map", action="append", default=[], metavar="NAME=PERSON")
    on.add_argument("--max-ai-usd", type=float, default=.05)
    push = cmd(commands, "push")
    push.add_argument("target", choices=("imports",))
    push.add_argument("--apply", action="store_true")
    push.add_argument("--remote", action="store_true")
    dec = cmd(commands, "decide")
    dec.add_argument("type", nargs="?")
    dec.add_argument("--list", action="store_true")
    dec.add_argument("--eval", action="store_true")
    dec.add_argument("--in", dest="input")
    dec.add_argument("--person", choices=(*PEOPLE, "together"))
    dec.add_argument("--no-llm", action="store_true")
    dec.add_argument("--remote", action="store_true")
    interview = cmd(commands, "interview")
    interview.add_argument("--person", choices=PEOPLE, required=True)
    interview.add_argument("--remote", action="store_true")
    bk = cmd(commands, "backup").add_subparsers(dest="backup", required=True)
    cmd(bk, "list").add_argument("--data", required=True)
    cmd(bk, "verify").add_argument("archive")
    rs = cmd(bk, "restore")
    rs.add_argument("archive")
    rs.add_argument("--to", required=True)
    rs.add_argument("--dry-run", action="store_true")
    ev = cmd(commands, "eval")
    ev.add_argument("--protocols", default="p1,p2,p3,p4,p5")
    ev.add_argument("--variants", default="current,emb_only,facets_only")
    helps = {"snapshot": "read a local snapshot directory", "live": "use live data (front-door commands need --remote to pull)",
             "json": "print a JSON result", "title": "title words or tmdb:N", "trace": "include score details", "since": "look back this many days",
             "before": "compare with a prior snapshot", "row": "select a recommendation row", "n": "maximum titles per row", "lang": "title language",
             "action": "reaction to simulate", "reason": "reason for the reaction (repeatable)", "days": "metric window in days",
             "explain": "explain one memory fact id", "source": "export source (onboard: NAME=PATH, repeatable)", "files": "local export files",
             "yes": "confirm writes without prompting", "dry_run": "preview without writing local state",
             "profile_map": "map a local export profile NAME=PERSON (repeatable)", "max_ai_usd": "maximum paid resolution budget in USD",
             "resolve_online": "search TMDB to resolve titles; exports stay local", "remote": "opt into the existing remote API", "id": "saved import event id", "sources": "show export instructions",
             "status": "show per-source onboarding status", "undo": "undo one onboard source", "plan": "apply only this preview plan hash",
             "non_interactive": "disable terminal prompts", "again": "reconsider unchanged applied sources", "target": "data stream to push",
             "apply": "apply the previewed push", "type": "decision type", "list": "list available decision types", "eval": "evaluate decision fixtures",
             "input": "decision JSON or @FILE", "no_llm": "disable paid decision models", "data": "backup data directory", "archive": "backup archive path",
             "to": "restore destination directory", "protocols": "comma-separated evaluation protocols", "variants": "comma-separated evaluation variants",
             "person": "household member whose taste or history to use", "url": "Skald API address, e.g. https://skald.example.org",
             "token_stdin": "read the API token from stdin instead of a prompt", "base": "address your watching apps use",
             "rotate_addon_token": "replace all private addon links; reinstall them in your apps",
             "tell": "add a member's facet preference: PERSON:like|avoid:FACET_ID (repeatable)",
             "name": "connector name to check; omit to check all", "show": "show account ids for mapping"}
    def add_help(parser):
        for action in parser._actions:
            if not action.help:
                action.help = helps.get(action.dest)
            if isinstance(action, argparse._SubParsersAction):
                action.help = action.help or "choose a task"
                for child in action.choices.values():
                    add_help(child)
    add_help(p)
    return p


def title_key(engine, title):
    title = " ".join(title)
    if title.startswith("tmdb:"):
        if not engine._server.re.fullmatch(r"tmdb:[0-9]{1,9}", title):
            raise ValueError("invalid title key")
        return title
    matches = [k for k, c in engine._index.items() if normal(title) in
               {normal(t) for t in [c["title"]] + c.get("aliases", [])}]
    known = {e["key"] for e in engine._profile["entries"]} & set(matches)
    if len(known) == 1:
        return next(iter(known))
    if len(matches) != 1:
        raise ValueError("title is unknown or ambiguous in this snapshot; use tmdb:N")
    return matches[0]


def decide_command(args, engine=None):
    if args.eval:
        if args.input is not None or args.list or args.remote:
            raise ValueError("--eval cannot use --in, --list or --remote")
        spec = importlib.util.spec_from_file_location("skald_decision_eval", Path(__file__).resolve().parents[1] / "eval/decisions.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.report(args.type)
    if args.list:
        if args.type or args.input is not None:
            raise ValueError("--list takes no type or input")
        return remote.request("/api/v1/decide") if args.remote else engine.decisions()
    if not args.type or args.input is None:
        raise ValueError("decide needs TYPE and --in")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", args.type):
        raise ValueError("invalid decision type")
    try:
        raw = Path(args.input[1:]).read_text() if args.input.startswith("@") else args.input
        inp = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid decision JSON") from None
    if not isinstance(inp, (dict, list)):
        raise ValueError("decision input must be an object or array")
    if isinstance(inp, list) and len(inp) > 25:
        raise ValueError("items")
    if args.remote:
        body = {"items" if isinstance(inp, list) else "in": inp, "no_llm": args.no_llm}
        if args.person:
            body["person"] = args.person
        return remote.request("/api/v1/decide/" + args.type, body)
    try:
        return engine.decide(args.type, inp, args.person)
    except KeyError:
        raise ValueError("unknown decision type") from None


def interview_command(args, engine=None, out=None):
    out = out or sys.stdout
    def next_anchor(answer=None):
        if not args.remote:
            return engine.interview(args.person, answer)
        if answer is not None:
            result = remote.request("/api/v1/events", {"stream": "interview", "events": [{**answer, "who": args.person}]})
            if result["rejected"]:
                raise ValueError("interview answer rejected")
        return remote.request("/api/v1/interview/next?person=" + args.person)
    data = next_anchor()
    if args.json or data["done"]:
        return data
    if not args.remote:
        print("Offline: answers live only in this engine's temporary copy.", file=out)
    used = set()
    for _ in range(12):
        a = next((a for a in data["anchors"] if a["experience"] not in used), None)
        if data["done"] or a is None:
            break
        print(f"{a['key']} | {a['kind']} | {a['title']} ({a['year']}) · l love / k like / m meh / n no / s skip / q quit", file=out)
        try:
            key = input().strip().lower()
        except EOFError:
            break
        if key == "q":
            break
        if key not in "lkmns" or len(key) != 1:
            raise ValueError("use l/k/m/n/s/q")
        reasons = []
        if key == "n":
            chips = ("too dark", "too slow", "too violent", "too silly", "not my genre", "seen enough of these")
            print("Reason (optional): " + " · ".join(f"{i+1} {r}" for i, r in enumerate(chips)), file=out)
            try:
                reason = input().strip()
            except EOFError:
                reason = ""
            if reason:
                if reason not in tuple(str(i) for i in range(1, 7)):
                    raise ValueError("unknown reason chip")
                reasons = [chips[int(reason)-1]]
        used.add(a["experience"])
        data = next_anchor({**{k: a[k] for k in ("key", "kind", "title", "year")},
                           "action": {"l": "love", "k": "like", "m": "meh", "n": "not_for_us", "s": "skip"}[key],
                           "reasons": reasons})
    return data


def execute(args, engine, state, snapshot, out=None):
    if args.command == "decide":
        return decide_command(args, engine)
    if args.command == "interview":
        return interview_command(args, engine, out)
    if args.command == "taste":
        if args.taste == "show":
            return engine.profile(args.person)
        if args.taste == "report":
            return engine.report(args.person, args.since, args.before)
        key = title_key(engine, args.title)
        if args.trace:
            return engine.trace(key, args.person)
        return {**engine.explain(key), **engine.score(key, args.person)}
    if args.command == "explain":
        return engine.trace(title_key(engine, args.title), args.person)
    if args.command == "rows":
        if not 1 <= args.n <= 100:
            raise ValueError("--n must be between 1 and 100")
        data = engine.rows(args.lang)
        if args.row:
            cid = args.row if args.row.startswith(members.rid('', engine._server.CONFIG)) else members.rid(args.row, engine._server.CONFIG)
            data["rows"] = [r for r in data["rows"] if r["id"] == cid]
            if not data["rows"]:
                raise ValueError("unknown row")
        for row in data["rows"]:
            row["titles"] = row["titles"][:args.n]
        return data
    if args.command == "react":
        facts = [tuple(f.split(":", 2)) for f in args.tell]
        if any(len(f) != 3 for f in facts):
            raise ValueError("--tell needs PERSON:like|avoid:FACET_ID")
        if args.action is None and not args.title and facts and not args.reason:
            return engine.react([], args.person, facts=facts)
        if not args.action or not args.title:
            raise ValueError("react needs an action and title, or --tell")
        key = title_key(engine, args.title)
        choice = engine._index.get(key)
        if not choice:
            raise ValueError("reaction title unavailable in snapshot")
        action = {"loves": "love", "likes": "like"}.get(args.action, args.action)
        e = {"key": key, "title": choice["title"], "kind": choice["kind"], "year": choice["year"], "action": action}
        if action == "interest":
            if args.reason:
                raise ValueError("interest has no reasons")
            e.update(src="plain", conf=1, n=1)
        else:
            e["reasons"] = args.reason
        return engine.react([e], args.person, facts=facts)
    if args.command == "connectors":
        return guide.connectors(engine, state, args.person)
    if args.command == "learn":
        return engine.learn()
    if args.command == "metrics":
        return engine.metrics(days=args.days)
    if args.command == "memory":
        return engine.memory(args.person, args.explain)
    if args.command == "import":
        return intake.import_command(args, engine, state, parser=parse)
    if args.command == "onboard":
        return intake.onboard(args, engine, state)
    if args.command == "me":
        if not args.person:
            raise ValueError("me needs --person")
        if args.me:
            data, code = guide.me_action(args, engine, state)
            return {**data, "_exit": code}
        data, count = guide.me(engine, state, args.person)
        return {**data, "_count": count}
    if args.command == "imports":
        if args.imports == "list":
            return {"events": engine._server.active_imports()}
        with patch.object(engine._server, "DATA", str(engine.data_dir)):
            return {"undone": engine._server.undo_import(args.id), "_persist": {"log": True, "undone": [args.id]}}
    if args.command == "push":
        undone = json.loads((state / "undone.json").read_text()) if (state / "undone.json").exists() else []
        return live.push(engine._server.read_imports(), engine._server.validate_import, apply=args.apply, undone=undone)
    if args.command == "eval":
        from eval import replay, variants
        selected, use_grid = variants.select(args.variants)
        protocols = {p.strip().lower() for p in args.protocols.split(",")}
        if not protocols or protocols - {"p1", "p2", "p3", "p4", "p5"}:
            raise ValueError("invalid eval protocols")
        with redirect_stdout(io.StringIO()):
            report = replay.run(engine._server, engine._work, selected, use_grid, protocols, replay.SEED)
        report["metadata"]["snapshot"] = str(snapshot)
        split = report["protocols"].get("p3", {}).get("split", {})
        if split.get("cutoff_at") is not None:
            split["cutoff_at"] = datetime.datetime.fromtimestamp(split["cutoff_at"] / 1000, datetime.timezone.utc).date().isoformat()
        return report
    raise ValueError("unknown command")


def text_output(args, data, out):
    color = out.isatty()
    def number(value):
        return f"{value:.3f}" if value is not None else "n/a (offline)"
    def line(value):
        print(value, file=out)
    if args.command == "interview" and data["done"]:
        why = "member is ready" if data["done_reason"] == "ready" else "20 positives and 5 reasoned negatives"
        line(f"Interview complete: {why}.")
        return
    def moves(items):
        for e in items[:10]:
            move = e["move"]
            arrow = "↑" if (move or 0) > 0 else "↓"
            marker = f"{arrow}{abs(move)}" if move is not None else "in/out"
            if color:
                marker = f"\033[{'32' if (move or 0) > 0 else '31'}m{marker}\033[0m"
            line(f"  {marker:>5}  {e['title'][:40]:40} {e['before']} → {e['after']}")
    mat = data.get("maturity", {})
    if mat.get("state") == "too_new":
        line(f"too new · {mat['explicit']} of {mat['need']} rated · % hidden")
    if args.command == "doctor":
        if "findings" in data:
            for finding in data["findings"]:
                line(f"{finding['severity']}: {finding['checkId']}: {finding['message']}" + (f"; {finding['fixHint']}" if finding['fixHint'] else ""))
        else:
            for rec in data["records"]:
                line(f"{rec['key']} | {rec['title']} | stored {rec['stored_kind']} | served {', '.join(rec['served_kinds'])}")
            line(f"{data['mixed']} mixed records")
    elif args.command == "me":
        if "top" not in data:
            line(data["say"])
            if data.get("cli"):
                line(data["cli"])
        else:
            line(data["say"])
            line(data.get("_count", args.person.title()))
            for source in data["sources"][:7]:
                c = source["counts"]
                line(f"{source['name']}: {source['status']} · {c['accepted']} accepted · {c['review']} review")
            for group, facets in data["top"].items():
                line(group + ": " + ", ".join(f['name'] + (f" ({', '.join(f['anchors'])})" if f['anchors'] else "") for f in facets))
            if data["facts"]:
                line("Facts: " + "; ".join(f"{f['id']}: {f['text']}" for f in data["facts"]))
            line(f"Pending: {data['pending']['review']} review · {data['pending']['memory']} memory")
            for action in data["actions"]:
                line(action["cli"])
    elif args.command == "taste" and args.taste == "show":
        line(f"{data['person'] or 'Household'} · {data['titles']} titles · " + ", ".join(f"{k} {v}" for k, v in data["sources"].items()))
        for group in ("experiences", "themes", "creators"):
            line(group.title())
            for facet in data[group][:8]:
                value = facet["affinity"]
                bar = ("+" if value >= 0 else "−") + "█" * min(12, round(abs(value) * 8))
                line(f"  {facet['name'][:34]:34} {bar:13} {value:6.3f}  {', '.join(facet['anchors'][:2])}")
        line("Signals: " + ", ".join(f"{k} {v}" for k, v in data["signals"].items()))
        for source, counts in data["source_signals"].items():
            line(f"  {source:10} " + ", ".join(f"{signal} {n}" for signal, n in counts.items()))
    elif args.command == "taste" and args.taste == "report":
        name = (data["person"] or "Household").title()
        w, cal = data["movers"]["window"], data["calibration"]
        line(f"{name} · {w['since']} days · {w['events']} window events · {w['undated']} undated")
        for group, facets in data["axes"].items():
            line(group + ": " + ", ".join(f"{f['name']} {f['affinity']:+.2f}" for f in facets[:3]))
        for f in data["movers"]["facets"][:3]:
            evidence = ", ".join(e["title"] + " " + e["action"] for e in f["events"])
            line(f"{name} {'↑' if f['delta'] > 0 else '↓'} {f['name']} {f['delta']:+.2f}" + (f" ({evidence})" if evidence else ""))
        line(f"Decay: {len(data['movers']['decay'])} facets · baseline {'available' if w['baseline'] else 'unavailable'}")
        line(f"Calibration: bg {cal['bg_n']} · loved Wildcard {number(cal['loved_wildcard']['share'])}")
        if "overlap" in cal:
            line(f"Top 30: overlap {cal['overlap']['jaccard']:.3f} · τ {cal['overlap']['tau']:.3f}")
    elif args.command in ("taste", "explain"):
        line(f"{data['title'] or data['key']} · score {number(data['score'])} · percentile {number(data['percentile'])} · {data['confidence'] or data['calibration']}")
        for reason in data["reasons"]:
            line("  " + reason)
        for facet in data.get("facets", [])[:6]:
            line(f"  {facet['name'][:34]:34} {facet['affinity']:6.3f}  {', '.join(facet['anchors'][:2])}")
        for person, split in data.get("per_person", {}).items():
            line(f"  {person:8} {number(split['score'])} / {number(split['percentile'])}")
        if "parts" in data:
            line("  Score parts: " + ", ".join(f"{k} {v:+.6f}" for k, v in (data["parts"] or {}).items()))
            line(f"  Rung: {data['rung'] or 'none'} · bg {data['bg_n']}")
            for e in data["events"]:
                line(f"  {e['src']} {e['day'] or 'undated'} {e['weight']:+.3f} · {e['title']}")
    elif args.command == "rows":
        for row in data["rows"]:
            line(f"{row['id']} · {row['name']}")
            for i, title in enumerate(row["titles"], 1):
                line(f"  {i:2} {title['title'][:38]:38} {'; '.join(title['why'])}")
    elif args.command == "react":
        line(data["simulation"])
        for cid in (members.rid('foryou', members.CONFIG), members.rid('week', members.CONFIG), members.rid('because', members.CONFIG), *(members.rid(p, members.CONFIG) for p in members.CONFIG["household"]["member_rows"])):
            line(cid)
            moves(data["rows"].get(cid, []))
        line("Top 50")
        moves(data["top50"])
        line("Facet changes")
        for facet in data["facets"][:8]:
            name = f"{facet['person']}: {facet['name']}" if facet.get("person") else facet['name']
            line(f"  {name[:38]:38} {facet['delta']:+.3f}")
        for cid, n in data.get("vetoed", {}).items():
            if n or data["notes"].get(cid):
                line(f"  {cid}: {n} vetoed · {data['notes'].get(cid, 0)} memory notes")
    elif args.command == "connectors":
        def date(value):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat(timespec="seconds") if value is not None else "—"
        line("id | kind | person | health | last new | next due | reminder")
        for c in data["connectors"]:
            origin = f" ({c['origin']})" if c["origin"] != "registry" else ""
            health = c['health']['state'] + (f" (last poll: {c['last_error']})" if c.get('last_error') else "")
            if c['health'].get('fixHint'):
                health += "; " + c['health']['fixHint']
            line(f"{c['id']}{origin} | {c['kind']} | {c['person']} | {health} | {date(c['last_new'])} | {date(c['next_due'])} | {c['reminder'] or '—'}")
    elif args.command == "metrics":
        def rate(k, n, ci):
            return f"{number(k)}/{n} [{number(ci[0])}–{number(ci[1])}]" if n and ci else "n/a"
        w, plays = data["window"], data["plays"]
        line(f"{w['from']}…{w['to']} · since {w['since'] or 'n/a'}")
        line(f"{data['fetches']} fetches · {data['exposures']} exposures · {data['impressions']} impressions")
        line(f"{data['title_days']} title-days · {data['titles']} titles covered")
        line(f"TV: {data['tv']['real']} real · {data['tv']['opens']} opens · {plays['pre_since']} pre-since")
        hit12 = plays['hit']['12']
        line(f"Credited plays: {rate(plays['credited'], plays['n'], hit12[2:] if hit12 else None)} · {data['clicks']} clicks")
        for depth, hit in plays["hit"].items():
            line(f"hit@{depth}: " + (rate(hit[0], hit[1], hit[2:]) if hit else "n/a"))
        q = data["ttp_min"]
        line(f"Time-to-play upper bound: ≤{q['p50']:.1f} min median (n={q['n']}, p25={q['p25']:.1f}, p75={q['p75']:.1f})" if q else "Time-to-play upper bound: n/a")
        a = data["abandon"]
        line(f"Abandon: {rate(a['k'], a['n'], a['ci'])} · {a['pending']} pending")
        for lid, row in sorted(data["rows"].items(), key=lambda p: -p[1]["credit"])[:10]:
            line(f"{lid}: {row['impressions']} impressions · credit {rate(row['credit'], row['title_days'], row['ci'])}")
    elif args.command == "memory":
        facts = [data["fact"]] if "fact" in data else data["facts"]
        proposed = sum(f["status"] == "proposed" for f in facts)
        active = sum(f["status"] == "active" for f in facts)
        ops = " ".join(f"{op} {n}" for op, n in data.get("ops", {}).items()) or "0"
        line(f"memory (dry run, nothing written) · {proposed} proposed · {active} active · ops {ops}")
        for f in facts:
            line(f"{f['person']} {f['status']} {f['strength']:.2f} {f['text']} {f['id']} n={f['n']}")
    elif args.command == "learn":
        line(f"{data['plays_logged']} plays · {len(data['recent_hits'])} recent hits · {data['tired']} tired titles")
        line("Row                              Shown  Credit   Rate")
        for cid, row in sorted(data["rows"].items(), key=lambda p: -p[1]["hits"])[:12]:
            line(f"{cid[:32]:32} {row['shown']:5} {row['hits']:7.3f} {row['hit_rate']:6.3f}")
        for hit in data["recent_hits"]:
            line(f"  hit: {hit['title']} ← {hit['row']} ({number(hit['share'])})")
        for tired in data["tired_titles"][:5]:
            line("  tired: " + tired["title"])
    elif args.command == "decide":
        if args.eval:
            for name, m in data.items():
                sources = ", ".join(f"{k} {v}" for k, v in sorted(m["sources"].items()))
                line(f"{name} v{m['ver']} · acc {m['acc']:.2f} ≥ {m['min_acc']:.2f} · "
                     f"p50 {m['p50_ms']:.1f} ms · p95 {m['p95_ms']:.1f} ms · {sources} · "
                     f"${m['cost']:g} · {'ok' if m['pass'] else 'fail'}")
        elif "types" in data:
            for item in data["types"]:
                line(f"{item['type']} v{item['ver']} · {item['lane'] or 'rule'} · {'available' if item['available'] else 'unavailable'}")
        else:
            for item in data.get("results", [data]):
                line(f"{item['type']} · {item['source']} · " + json.dumps(item["answer"], ensure_ascii=False))
    elif args.command == "interview":
        progress = data["progress"]
        line(f"{progress['positives']} of 20 positives · {progress['negatives_with_reason']} of 5 reasoned negatives · learning")
    elif args.command == "onboard":
        for row in data["sources"]:
            if data["mode"] == "sources":
                line(f"{row['name']}: {row['hint']} · trust {row['trust']} · {'usable' if row['usable'] else 'later/unusable'}")
                if row.get("howto"):
                    how = row["howto"]
                    for step in how["steps"]:
                        line("  " + step)
                    line(f"  {how['url']} · wait {how['wait']}" + (" (unverified)" if how["checked"] is None else ""))
            else:
                paths = [Path(spec.partition('=')[2]).name for spec in getattr(args, "source", []) if spec.partition('=')[0] == row['name']]
                label = f" ({', '.join(paths)})" if paths else ""
                line(f"{row['name']}{label}: {row['candidates']} candidates · {row['accepted']} accepted · {row['review']} review · {row.get('unsupported', 0)} unsupported")
                if row['review'] and not getattr(args, "remote", False) and not getattr(args, "resolve_online", False):
                    line("  Offline matches need review; use --resolve-online or --remote to resolve.")
                if row['name'] == 'imdb' and row.get('unsupported'):
                    line(f"  {row['unsupported']} unsupported title types (videoGame, podcastSeries, podcastEpisode, musicVideo) ignored")
        if data["mode"] == "sources":
            return
        if data.get("learned"):
            line("What I learned:")
            for note in data["learned"]:
                line("  " + note)
        line(f"{data['mode']} · added {data['added']}")
        for count in data.get("rated", []):
            line(count)
        if "changed" in data:
            line(f"changed {data['changed']} events")
        if data["react"] and getattr(args, "verbose", False):
            for facet in data["react"]["facets"]:
                line(f"  {facet['name']}: {facet['delta']:+.3f}")
            for row, items in data["react"]["rows"].items():
                line(row)
                moves(items)
        if data["plan"]:
            line("plan " + data["plan"])
        for action in data.get("next") or []:
            line(action["cli"])
    elif args.command in ("import", "imports", "push"):
        for e in data.get("events", []):
            line(f"{e.get('id', 'preview'):32} {e['who']:7} {e['action']:10} {e['src']:10} {e['title']} ({e['conf']:.2f}, n={e['n']})")
        if args.command == "import":
            line(f"{data['candidates']} candidates · {len(data['events'])} accepted · {len(data['review'])} to review · {'dry run' if data['dry_run'] else 'saved'}")
        elif args.command == "push":
            line(f"{data['count']} local events · " + (f"uploaded {data['uploaded']} merged events" if data["apply"] else "preview; use --apply to upload"))
        elif "undone" in data:
            line("Undone " + data["undone"]["id"])
    else:
        line("Replay protocols: " + ", ".join(data["protocols"]))
        for pid, protocol in data["protocols"].items():
            if isinstance(protocol, dict):
                for name, group in protocol.get("groups", {}).items():
                    for variant, metrics in group["variants"].items():
                        line(f"  {pid} {name} {variant}: recall@30={metrics['all']['recall@30']}")


def persist(engine, state, directive):
    if directive["log"]:
        path = engine.data_dir / "imports.jsonl"
        intake.write_private(state / "imports.jsonl", path.read_text(encoding="utf-8"))
    if directive["undone"]:
        path = state / "undone.json"
        previous = json.loads(path.read_text()) if path.exists() else []
        intake.write_private(path, json.dumps(sorted(set(previous) | set(directive["undone"]))) + "\n")


def main(argv=None, out=None):
    out = out or sys.stdout
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    pre.add_argument("--snapshot", default=snapshot if snapshot and snapshot.strip() else None)
    pre.add_argument("--live", action="store_true")
    pre.add_argument("--remote", action="store_true")
    pre.add_argument("--person")
    pre.add_argument("--json", action="store_true")
    pre.add_argument("--state-dir")
    pre.add_argument("command", nargs="?")
    known, _ = pre.parse_known_args(argv)
    args = None
    try:
        if known.command == "backup":
            candidate = parser().parse_args(argv)
            if candidate.command == "backup":
                args = candidate
                from . import backup
                if args.live:
                    raise ValueError("backup reads local archives only")
                data, ok = backup.run(args)
                if args.backup == "list" and members.CONFIG_ERROR:
                    data["config_error"] = str(members.CONFIG_ERROR)
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
                else:
                    print("\n".join(backup.text(data)), file=out)
                return 0 if ok else 1
        from .migrate import check, ensure, FORMAT
        if not known.snapshot and not known.live:
            local_data = os.environ.get("DATA_DIR", "/data")
            if check(local_data)["format"] < FORMAT:
                ensure(local_data)
        members.configure(known.snapshot if not known.live else None, readonly=True)
        if known.remote and not known.snapshot and members.CONFIG_SOURCE == "implicit" and known.person not in (None, "both", "together", "guest"):
            members.CONFIG = members.validate_config(members.fresh_config([known.person]))
            members.PEOPLE[:] = [known.person]
            members.CONFIG_SOURCE = "env"
        # Read-only CLI help and remote commands can use the implicit registry.
        p = parser()
        args = p.parse_args(argv)
        from .migrate import FormatError
        if members.CONFIG_ERROR and (isinstance(members.CONFIG_ERROR, FormatError) or args.command not in ("backup", "doctor", "connect")):
            raise members.CONFIG_ERROR
        state = Path(args.state_dir).resolve()
        if args.command is None:
            p.print_help(file=out)
            if not (state / "onboard.json").exists():
                print("start with: skald onboard", file=out)
            return 0
        if args.command in ("setup", "connect") or args.command == "connectors" and args.connector_action == "test":
            from . import setup, connectors
            if args.live or args.snapshot and args.command != "connect":
                raise ValueError("setup and connector tests use local DATA_DIR; omit --snapshot and --live")
            if args.command == "setup":
                if args.setup_action == "link" and not args.name:
                    raise ValueError("link needs --name NAME")
                with redirect_stdout(out):
                    data = setup.run(args)
            elif args.command == "connect":
                data = remote.connect(args.url, setup.secret(args.token_stdin, "API token"))
            else:
                with setup.server(Path(os.environ.get("DATA_DIR", "/data"))) as s:
                    s.require_config(s.CONFIG_SOURCE, s.CONFIG_ERROR)
                    data = connectors.test(s, args.name, show=args.show)
            if args.json:
                print(json.dumps(data, ensure_ascii=False), file=out)
            elif args.command == "connectors":
                print("\n".join(connectors.text(data)), file=out)
            elif args.command == "connect":
                print(data["message"], file=out)
            elif args.status:
                print("Release: " + data["release"], file=out)
                print("Data format: " + str(data["data_format"]), file=out)
                print("Done: " + (", ".join(data["done"]) or "nothing yet"), file=out)
                print("Missing: " + (", ".join(data["missing"]) or "nothing"), file=out)
                print("AIOStreams login: " + data["aiostreams_login"], file=out)
                print("Next: " + data["next"], file=out)
            elif args.setup_action == "link":
                print("\n".join(data["lines"]), file=out)
            return 0 if data.get("ok", True) else 1
        if args.command == "onboard" and args.sources and not args.snapshot and not args.live:
            from . import setup
            with tempfile.TemporaryDirectory(prefix="skald-sources-") as temp, setup.server(Path(temp)) as s:
                data = {**intake._result(args.person, "sources"), "sources": intake._source_rows(s)}
            if args.json:
                print(json.dumps(data, ensure_ascii=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        if args.command == "addon-url":
            if args.snapshot or args.live:
                raise ValueError("addon-url uses local DATA_DIR; omit --snapshot and --live")
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / "server.py"),
                       "--rotate-addon-token" if args.rotate_addon_token else "--addon-url"]
            if args.base:
                command.extend(["--base", args.base])
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "could not create addon URLs")
            print(json.dumps({"urls": result.stdout.splitlines()}) if args.json else result.stdout.rstrip("\n"), file=out)
            return 0
        if args.command in ("trakt", "login"):
            if args.snapshot or args.live:
                raise ValueError("trakt login uses local DATA_DIR; omit --snapshot and --live")
            members.require_config(members.CONFIG_SOURCE, members.CONFIG_ERROR)
            from .trakt import login
            login(out, args.person)
            return 0
        if args.command == "doctor" and args.doctor is None:
            try:
                data = {"v": 1, "ok": False, "checksRun": ["config.valid"], "findings": [
                    {"checkId": "config.valid", "severity": "error", "message": str(members.CONFIG_ERROR),
                     "fixHint": "restore a valid skald.json or use an image supporting its config version"}]} \
                    if members.CONFIG_ERROR else guide.doctor(args, state)
            except Exception:
                data = {"v": 1, "ok": False, "checksRun": [], "findings": [{"checkId": "doctor.internal", "severity": "error",
                        "message": "could not complete local checks", "fixHint": "check snapshot and state readability"}]}
                code = 2
            else:
                code = 0 if data["ok"] else 1
            if args.json:
                print(json.dumps(data, ensure_ascii=False), file=out)
            else:
                text_output(args, data, out)
            return code
        if args.command == "interview" and args.remote:
            data = interview_command(args, out=out)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        if args.command == "decide" and (args.remote or args.eval):
            data = decide_command(args)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            else:
                text_output(args, data, out)
            return int(args.eval and any(not m["pass"] for m in data.values()))
        if args.command == "push" and args.remote:
            from .remote import push_imports
            data = push_imports(args.state_dir, apply=args.apply)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            elif args.apply:
                print(f"{data['accepted']} accepted · {data['duplicates']} duplicates · {len(data['rejected'])} rejected · "
                      f"{data['undone']} undone · {data['already_absent']} already absent · {data['requests']} requests", file=out)
            else:
                print(f"{data['count']} events · {data['undone']} undo ids · {data['chunks']} chunks · "
                      f"{data['bytes']} bytes → {data['host']} · preview; use --apply", file=out)
            if not args.json:
                for message in data.get("messages", []):
                    print(message, file=out)
            return 1 if args.apply and data["rejected"] else 0
        if args.command in ("onboard", "me"):
            intake._state(state)
            guide.review(state)
        snapshot = (state / "live" if args.command in ("onboard", "me") and not args.remote else live.pull(state / "live")) \
            if args.live else Path(args.snapshot).resolve() if args.snapshot else None
        if snapshot is None and not (args.command in ("import", "onboard") and args.remote):
            members.require_config(members.CONFIG_SOURCE, members.CONFIG_ERROR)
            raise ValueError("use --snapshot DIR, $SKALD_SNAPSHOT, or --live")
        if args.live:
            members.configure(str(snapshot), readonly=True)
        members.require_config(members.CONFIG_SOURCE, members.CONFIG_ERROR)
        if snapshot is not None and (snapshot == state or state.is_relative_to(snapshot)):
            raise ValueError("state directory must be outside the read-only snapshot")
        if args.command == "doctor":
            from .engine import doctor_keys
            data = doctor_keys(snapshot)
            if args.json:
                print(json.dumps(data, ensure_ascii=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        undone = json.loads((state / "undone.json").read_text()) if (state / "undone.json").exists() else []
        if not isinstance(undone, list) or any(not isinstance(i, str) or len(i) != 32 or any(c not in "0123456789abcdef" for c in i) for i in undone):
            raise ValueError("invalid local undo log")
        with tempfile.TemporaryDirectory(prefix="skald-cli-") as temp:
            work = Path(temp)
            if snapshot is None:
                (work / "skald.json").write_text(json.dumps(members.CONFIG), encoding="utf-8")
            else:
                for name in data_files(members.CONFIG):
                    if (snapshot / name).is_file():
                        shutil.copyfile(snapshot / name, work / name)
            snapshot_ids = {json.loads(line)["id"] for line in (work / "imports.jsonl").read_text().splitlines() if line.strip()} \
                if (work / "imports.jsonl").exists() else set()
            # Merge a pulled remote log with local events, preserving event ids.
            if (state / "imports.jsonl").exists() or undone:
                remote_log = (work / "imports.jsonl").read_text(encoding="utf-8") if (work / "imports.jsonl").exists() else ""
                local = (state / "imports.jsonl").read_text(encoding="utf-8") if (state / "imports.jsonl").exists() else ""
                merged = {e["id"]: e for e in [json.loads(line) for line in
                          (remote_log + "\n" + local).splitlines() if line] if e["id"] not in undone}
                (work / "imports.jsonl").write_text("".join(json.dumps(e) + "\n" for e in merged.values()), encoding="utf-8")
            with redirect_stdout(sys.stderr), Engine(work) as engine:
                engine.snapshot_import_ids = snapshot_ids
                if getattr(args, "verbose", False):
                    for diagnostic in engine.diagnostics:
                        print(diagnostic, file=sys.stderr)
                data = execute(args, engine, state, snapshot, out)
                persist(engine, state, data.pop("_persist", {"log": False, "undone": []}))
        code = data.pop("_exit", 0)
        if args.json:
            data.pop("_count", None)
            print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
        else:
            text_output(args, data, out)
        return code
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Export failures carry a local basename and fixed reason, never a raw row.
        message = str(error) if isinstance(error, ValueError) else "could not read or write local data"
        if args is not None and args.command == "setup" and message.startswith("invalid config:"):
            message += "; check the household names, region and connector mappings, then rerun setup"
        if args is not None and args.command == "setup" and (not message or not isinstance(error, ValueError)):
            message = "setup could not read or write its data; check DATA ownership, credentials and the source address"
        if args.json if args is not None else known.json:
            print(json.dumps({"error": message}), file=out)
        else:
            print("skald: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
