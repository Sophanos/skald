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
import time
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
                        "report": "summarize recent learning", "align": "show taste drivers and correction previews", "calibration": "inspect local match calibration", "explain": "trace a title's score", "rows": "preview recommendation rows",
                        "react": "simulate a reaction", "learn": "inspect learning signals", "metrics": "show local weekly row play counts",
                        "connectors": "show input health and sync cadence", "health": "show saved server checks, repairs and alert availability", "memory": "inspect memory proposals", "doctor": "check local setup and state", "keys": "inspect mixed title keys",
                        "import": "import one local export", "imports": "list or undo imports", "list": "list saved items", "undo": "undo a saved import",
                        "onboard": "set up and resume local exports", "me": "show what Skald knows about you", "forget": "forget imported signals",
                        "correct": "correct a memory fact", "push": "preview or push resolved imports", "decide": "run a structured decision", "interview": "rate interview anchors to start a taste",
                        "backup": "inspect or restore backups", "verify": "verify an archive", "restore": "restore an archive", "eval": "run offline evaluation",
                        "suggest": "show weekly habits that could become shelves",
                        "addon-url": "print private addon URLs for local DATA_DIR",
                        "trakt": "authorize the configured Trakt connector", "login": "sign in with a Trakt device code"}
        c = parent.add_parser(name, help=descriptions[name], description=descriptions[name])
        c.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return c

    def person(c, required=False):
        c.add_argument("--person", choices=PEOPLE if not required else (*PEOPLE, "both"), required=required)

    shelf = commands.add_parser("shelf", help="edit fixed standing wish slots").add_subparsers(dest="shelf_op", required=True, help="shelf operation to run")
    for op in ("add", "replace", "list", "stop", "count"):
        c = shelf.add_parser(op, help="edit shelf " + op)
        c.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        scope = c.add_mutually_exclusive_group(required=op != "list")
        scope.add_argument("--person", choices=PEOPLE)
        scope.add_argument("--together", action="store_true", help="use the shared Together slots")
        if op in ("add", "replace"):
            c.add_argument("text", help="free text viewing wish")
            c.add_argument("--name", default="")
            c.add_argument("--when", default="always", help="always, or weekdays and logical hours, e.g. sun 05-12")
            c.add_argument("--yes", action="store_true")
            c.add_argument("--plan")
            c.add_argument("--slot", type=int, required=op == "replace", help="select a fixed slot number")
        elif op == "stop":
            c.add_argument("slot", help="shelf name or slot number")
        elif op == "count":
            c.add_argument("count", type=int, help="number of slots, 0..10")

    from . import setup
    setup.add_parser(commands)
    command = commands.add_parser("command", help="preview, apply or undo a personal change")
    command.add_argument("text", nargs="?", default="", help="one personal change; repeat text with --plan")
    command.add_argument("--person", required=True)
    command.add_argument("--yes", action="store_true", help="apply the preview")
    command.add_argument("--plan", help="apply only this preview hash")
    command.add_argument("--undo", action="store_true", help="revert the last command")
    command.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    connect = commands.add_parser("connect", help="link this laptop to the API")
    connect.add_argument("url")
    connect.add_argument("--token-stdin", action="store_true")
    connect.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    addon = cmd(commands, "addon-url")
    addon.add_argument("--base")
    addon.add_argument("--rotate-addon-token", action="store_true")
    taste = cmd(commands, "taste").add_subparsers(dest="taste", required=True)
    person(cmd(taste, "show"))
    align = cmd(taste, "align")
    align.add_argument("--person", choices=(*PEOPLE, "household"), help="member to inspect; omit for the household")
    align.add_argument("--limit", type=int, default=10, help="maximum drivers and facets per kind, 1..100")
    cmd(taste, "calibration").add_argument("--person", choices=(*PEOPLE, "together", "household"))
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
    row_actions = rows.add_subparsers(dest="rows_action")
    row_actions.add_parser("pool", help="inspect pool seeds and cached counts").add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for op in ('plan', 'why', 'prefer', 'refresh'):
        c = row_actions.add_parser(op, help={'plan': 'explain every home row', 'why': 'explain one row and its cards',
                                       'prefer': 'change a household row family cap', 'refresh': 'queue a row refresh for the worker'}[op])
        c.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        if op in ('plan', 'why'):
            c.add_argument('--person', choices=('household', 'together', *PEOPLE, 'guest'), default='household')
            c.add_argument('--lang', default=argparse.SUPPRESS)
        else:
            c.add_argument('--yes', action='store_true', help='apply the preview')
            c.add_argument('--plan', help='apply only if the preview hash still matches')
        if op in ('why', 'refresh'):
            c.add_argument('row_name', metavar='ROW', help='registered row id or label')
        if op == 'prefer':
            c.add_argument('--theme', choices=('mood', 'lane'), required=True, help='row family to change')
            direction = c.add_mutually_exclusive_group(required=True)
            direction.add_argument('--more', action='store_true', help='raise the row limit by one')
            direction.add_argument('--fewer', action='store_true', help='lower the row limit by one')
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
    cmd(commands, "health")
    metrics = cmd(commands, "metrics")
    metrics.add_argument("--days", type=int, help="include row weeks touching the last 1..35 days (default: 4 weeks)")
    learning = metrics.add_subparsers(dest="metrics_action").add_parser("learning", help="show mature row learning without writing")
    person(learning)
    learning.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    suggestions = cmd(commands, "suggest")
    suggestions.add_argument("--person", choices=PEOPLE, required=True)
    suggestions.add_argument("--dismiss", help="dismiss one suggestion id for 90 days")
    history = commands.add_parser("history", help="preview cleanup or forget old evidence")
    action = history.add_mutually_exclusive_group(required=True)
    action.add_argument("--forget-before", metavar="DATE", help="forget evidence before YYYY-MM-DD")
    action.add_argument("--cleanup", action="store_true", help="run the configured raw retention cleanup")
    history.add_argument("--person", choices=PEOPLE, help="household member whose evidence to forget")
    mode = history.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show counts without writing (default)")
    mode.add_argument("--yes", action="store_true", help="apply after reviewing the dry-run counts")
    history.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
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
        if args.taste == "align":
            return engine.align(args.person, args.limit)
        if args.taste == "report":
            return engine.report(args.person, args.since, args.before)
        if args.taste == "calibration":
            return engine.calibration(args.person)
        key = title_key(engine, args.title)
        if args.trace:
            return engine.trace(key, args.person)
        return {**engine.explain(key), **engine.score(key, args.person)}
    if args.command == "explain":
        return engine.trace(title_key(engine, args.title), args.person)
    if args.command == "rows":
        if args.rows_action == "pool":
            return engine.pool()
        if args.rows_action in ('plan', 'why'):
            from . import rowplan
            return rowplan.plan(engine._server, args.person, args.lang) if args.rows_action == 'plan' else rowplan.why(engine._server, args.row_name, args.person, args.lang)
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
        if args.metrics_action == 'learning':
            from .metrics import learning_report
            return learning_report(engine._work, engine._server.CONFIG, scope=args.person or 'household')
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
    def taste_line(arch):
        if isinstance(arch, dict) and arch.get("name") and arch.get("line"):
            line(f"Taste: {arch['name']}. {arch['line']}")
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
            taste_line(data.get("archetype"))
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
    elif args.command == "taste" and args.taste == "align":
        from .alignment import text
        line(text(data))
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
    elif args.command == "taste" and args.taste == "calibration":
        ref = data['reference']
        line(f"{data['person'] or 'Household'} · reference {ref['kind'] or 'unavailable'} · {ref['size']} titles")
        line("Scores: " + " · ".join(f"{p} {number(value)}" for p, value in data['scores'].items()))
        for cid, row in data['rows'].items():
            line(cid + ": " + " · ".join(f"{band}% {n}" for band, n in row['bands'].items()) + f" · unknown {row['unknown']}")
    elif args.command == "taste" and args.taste == "report":
        name = (data["person"] or "Household").title()
        w, cal = data["movers"]["window"], data["calibration"]
        line(f"{name} · {w['since']} days · {w['events']} window events · {w['undated']} undated")
        for adult, coverage in data["coverage"].items():
            line(f"{adult.title()}: Learned from {coverage['learned']} of {coverage['total']} titles")
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
        arch = data.get("archetypes") or {}
        taste_line(arch.get(getattr(args, "person", None)) or arch.get("household"))
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
        if args.rows_action == "pool":
            line(f"Pool: {data['pool_size']} candidates")
            if data.get('counts_built_at') is not None:
                line(f"Counts from background build: {data['counts_built_at']}; selection matches: {data['selection_matches_build']}")
            if data.get('unavailable_endpoints'):
                line(f"Unavailable offline endpoints: {data['unavailable_endpoints']}")
            for e in data['seeds']:
                line(f"  {e['title']} ({e['key']}, {e['kind']}): {'chosen' if e['chosen'] else 'skipped'}, {e['reason']}; "
                     f"candidates {e['candidates']}, kept {e['kept']}")
            line(f"Cap: {data.get('cap', 6)} per seed; removed {data.get('cap_removed', 0)}")
            return
        if args.rows_action in ('plan', 'why'):
            from .rowplan import text
            for value in text(data):
                line(value)
            return
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
        from .metrics import text, learning_text
        for message in (learning_text(data) if getattr(args, 'metrics_action', None) == 'learning' else text(data)):
            line(message)
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
            if args.source == "trakt" and data["dry_run"]:
                line("Trakt: " + ", ".join(f"{name.replace('_', ' ')} {data['merge'].get('trakt_' + name, 0)}"
                                         for name in ("watched_movies", "watched_series", "watchlist", "ratings", "favorites", "hidden")))
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
    pre = argparse.ArgumentParser(add_help=False)
    snapshot = os.environ.get("SKALD_SNAPSHOT")
    pre.add_argument("--snapshot", default=snapshot if snapshot and snapshot.strip() else None)
    pre.add_argument("--live", action="store_true")
    pre.add_argument("--remote", action="store_true")
    pre.add_argument("--person")
    pre.add_argument("--json", action="store_true")
    pre.add_argument("--state-dir")
    pre.add_argument("command", nargs=argparse.REMAINDER)
    known, _ = pre.parse_known_args(argv)
    # Stop global abbreviations at the command; remote/person belong to subcommands.
    tail = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    tail.add_argument("--remote", action="store_true")
    tail.add_argument("--person")
    tail.add_argument("--json", action="store_true")
    sub, _ = tail.parse_known_args(known.command[1:])
    known.remote, known.person = sub.remote, sub.person
    known.json = known.json or sub.json
    calibration = [arg for arg in known.command if arg != "--json"][:2] == ["taste", "calibration"]
    known.command = known.command[0] if known.command else None
    args = None
    try:
        if known.command == "health":
            args = parser().parse_args(argv)
            if args.live:
                raise ValueError("health reads local state only; omit --live")
            from .health import view
            data = view(args.snapshot or os.environ.get("DATA_DIR", "/data"))
            if args.json:
                print(json.dumps(data), file=out)
            else:
                print("Health checks: " + ("on" if data['enabled'] else "off"), file=out)
                print("Alert channel configured: " + ("yes" if data['alert_configured'] else "no"), file=out)
                for check in data['checks']:
                    print(check.get('label', check['id']) + ": " + check['state'], file=out)
                repair = data['last_repair']
                print("Last repair: " + (json.dumps(repair) if repair else "none"), file=out)
            return 0 if data['ok'] else 1
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
        from . import migrate
        cli_argv = sys.argv[1:] if argv is None else argv
        readonly_align = known.command == "taste" and "align" in cli_argv
        row_control = known.command == 'rows' and any(v in ('plan', 'why', 'prefer', 'refresh') for v in cli_argv)
        if not known.snapshot and not known.live and not known.remote and known.command != "suggest" and not readonly_align and not row_control and not calibration and not (
                known.command == "metrics" and "learning" in cli_argv) and not any(
                flag in cli_argv for flag in ('--help', '-h')):
            local_data = os.environ.get("DATA_DIR", "/data")
            # Additive settings/history formats need no writes for read-only commands.
            if migrate.rewrite_needed(local_data):
                migrate.ensure(local_data)
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
        if args.command == "taste" and args.taste == "calibration":
            if args.live:
                raise ValueError("taste calibration reads local data only; omit --live")
            with Engine(args.snapshot or os.environ.get("DATA_DIR", "/data")) as engine:
                data = engine.calibration(args.person)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        if args.command is None:
            p.print_help(file=out)
            if not (state / "onboard.json").exists():
                print("start with: skald onboard", file=out)
            return 0
        if args.command == "taste" and args.taste == "align":
            if args.live:
                raise ValueError('taste align reads local DATA_DIR or --snapshot only; omit --live')
            with redirect_stdout(sys.stderr), Engine(args.snapshot or os.environ.get('DATA_DIR', '/data')) as engine:
                data = engine.align(args.person, args.limit)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        if args.command == "history":
            from .history import forget
            if args.live or args.snapshot and args.yes:
                raise ValueError("history writes local DATA_DIR only; omit --live and --snapshot")
            directory = args.snapshot or os.environ.get("DATA_DIR", "/data")
            if args.cleanup:
                from .history import cleanup, enabled
                if args.yes and enabled(members.CONFIG):
                    migrate.ensure(directory)
                with Engine(directory) as engine, patch.object(engine._server, 'DATA', str(directory)):
                    data = {**cleanup(engine._server, time.time(), apply=args.yes), 'dry_run': not args.yes}
                if args.json:
                    print(json.dumps(data), file=out)
                else:
                    print(f"{'Would prune' if data['dry_run'] else 'Pruned'} {data['pruned']} raw items; "
                          f"{data['summaries']} monthly summaries.", file=out)
                    print(f"Residual raw records: {data['residual']} (no taste event).", file=out)
                return 0
            if not args.person:
                raise ValueError('forget requires --person')
            data = forget(directory, members.CONFIG, args.forget_before, args.person)
            if args.yes:
                from .migrate import ensure
                ensure(directory)
                data = forget(directory, members.CONFIG, args.forget_before, args.person, apply=True)
            if args.json:
                print(json.dumps(data), file=out)
            else:
                print(f"{'Would forget' if data['dry_run'] else 'Forgot'} {data['raw']} raw items, "
                      f"{data['summaries']} monthly summaries, {data['facts']} inferred memory facts; "
                      f"{data['metrics']} metric cohorts invalidated.", file=out)
                if data['dry_run']:
                    print("Repeat with --yes to apply these counts.", file=out)
            return 0
        if args.command == "metrics":
            from .metrics import report, settings, disable, learning_report
            if args.live:
                raise ValueError('metrics reads local DATA_DIR or --snapshot only; omit --live')
            members.require_config(members.CONFIG_SOURCE, members.CONFIG_ERROR)
            if not args.snapshot and args.metrics_action != 'learning' and not settings(members.CONFIG)['enabled']:
                disable(os.environ.get('DATA_DIR', '/data'))
            if args.metrics_action == 'learning':
                data = learning_report(args.snapshot or os.environ.get('DATA_DIR', '/data'), members.CONFIG,
                                       scope=args.person or 'household')
            else:
                data = report(args.snapshot or os.environ.get('DATA_DIR', '/data'), members.CONFIG, days=args.days)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, allow_nan=False), file=out)
            else:
                text_output(args, data, out)
            return 0
        if args.command == "suggest":
            from .engine import suggestions
            if args.live:
                raise ValueError("suggest reads local data only; omit --live")
            if args.snapshot and args.dismiss:
                raise ValueError("dismiss uses local DATA_DIR; omit --snapshot")
            data = suggestions(args.snapshot or os.environ.get("DATA_DIR", "/data"), args.person, members.CONFIG, args.dismiss)
            if args.json:
                print(json.dumps(data, ensure_ascii=False), file=out)
            else:
                for row in data["suggestions"]:
                    print(f"{row['id']}  {row['name']}: {row['text']} ({row['evidence']['weeks']} weeks; {row['when']})", file=out)
                if not data["suggestions"]:
                    print("No suggestions.", file=out)
            return 0
        if args.command == "shelf":
            if args.live or args.snapshot:
                raise ValueError("shelf uses local DATA_DIR; omit --snapshot and --live")
            from . import setup
            with setup.server(Path(os.environ.get("DATA_DIR", "/data")),
                              report=lambda line: print(line, file=sys.stderr if args.json else out)) as s:
                s.require_config(s.CONFIG_SOURCE, s.CONFIG_ERROR)
                scope = "together" if args.together else args.person
                if args.shelf_op == "list":
                    data = s.shelf_list(scope)
                else:
                    kwargs = {k: getattr(args, k) for k in ("text", "name", "when", "slot", "count", "plan") if hasattr(args, k)}
                    data = s.shelf_change(scope, args.shelf_op, apply=getattr(args, "yes", False) or args.shelf_op in ("stop", "count"), **kwargs)
            print(json.dumps(data, ensure_ascii=False, indent=None if args.json else 2), file=out)
            return 0
        if args.command == 'rows' and args.rows_action in ('plan', 'why'):
            from . import rowplan
            if args.live:
                raise ValueError('rows plan and why use local DATA or --snapshot')
            with Engine(args.snapshot or os.environ.get('DATA_DIR', '/data'), offline=True) as engine:
                data = rowplan.plan(engine._server, args.person, args.lang) if args.rows_action == 'plan' else rowplan.why(engine._server, args.row_name, args.person, args.lang)
            print(json.dumps(data, ensure_ascii=False) if args.json else '\n'.join(rowplan.text(data)), file=out)
            return 0
        if args.command == 'rows' and args.rows_action in ('prefer', 'refresh'):
            from . import commands
            if args.live or args.snapshot:
                raise ValueError('row changes use local DATA_DIR')
            data_dir = Path(os.environ.get('DATA_DIR', '/data'))
            with Engine(data_dir, offline=True) as engine:
                s = engine._server
                # Household controls do not need a player or network refresh.
                s.aio_rows_changed = lambda: None
                if args.rows_action == 'refresh':
                    answer = dict(op='row_refresh', kind=None, values=[args.row_name], person=None)
                else:
                    answer = dict(op='home_more' if args.more else 'home_fewer', kind=None, values=[args.theme], person=None)
                data = commands.preview(s, '', next((p for p in s.PEOPLE if s.kids_age(p) is None), None), answer=answer)
                data['source'] = 'cli'
                if args.plan and args.plan != data['plan']:
                    raise ValueError('Plan changed; preview again')
                if args.yes or args.plan:
                    with patch.object(s, 'DATA', str(data_dir)):
                        data = commands.apply(s, data)
            print(json.dumps(data, ensure_ascii=False) if args.json else data['name'] + ('? Plan: '+data['plan'] if 'plan' in data else ''), file=out)
            return 0
        if args.command == "command":
            from . import commands, setup
            with setup.server(Path(os.environ.get("DATA_DIR", "/data")),
                              report=lambda line: print(line, file=sys.stderr if args.json else out)) as s:
                if args.undo:
                    data = commands.undo(s, commands.people(s, viewer=args.person), source='cli')
                else:
                    data = commands.preview(s, args.text, args.person)
                    if data is None:
                        raise ValueError("Skald cannot set this yet")
                    if not data.get('read_only'):
                        data['source'] = 'cli'
                    if args.plan and args.plan != data.get("plan"):
                        raise ValueError("Plan changed; preview again")
                    if args.yes or args.plan:
                        data = commands.apply(s, data)
            print(json.dumps(data, ensure_ascii=False) if args.json else data["name"] + ("? Plan: " + data["plan"] if "plan" in data else ""), file=out)
            if not args.json and data.get('description'):
                print(data['description'], file=out)
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
                for note in data.get("notes", []):
                    print("Note: " + note, file=out)
                for line in data.get("prefs", []):
                    print(line, file=out)
                for cid, label in (("mdblist", "MDBList"), ("stremio", "Stremio")):
                    for person, connected in data.get("connections", {}).get(cid, {}).items():
                        print(label + " " + person + ": " + ("connected" if connected else "not connected"), file=out)
                print("AIOStreams login: " + data["aiostreams_login"], file=out)
                print("Next: " + data["next"], file=out)
            elif args.setup_action in ("household", "link", "adopt", "prefs", "rows", "alerts") or args.disconnect:
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
            from .history import folded_imports, remove_imports
            snapshot_ids.update(e['id'] for e in folded_imports(work))
            if undone:
                remove_imports(work, set(undone))
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
