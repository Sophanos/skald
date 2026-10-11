#!/usr/bin/env python3
"""Name-literal census by checked-in ownership kind; no data files are read."""
import argparse
import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# This map is deliberately checked in: changing a validator cannot silently relabel it.
KIND_MAP = {
    "validator-enum": {"RANK_INP", "validate_import", "read_sessions", "validate_session", "validate_feedback",
        "api_person", "_clients_read", "new_client", "api_decide", "_person", "_mapping", "_candidates", "person", "parser", "parse"},
    "both-fold": {"import_events", "_merge_import_events", "fold_feedback", "compact_feedback", "cross_source", "observer"},
    "registry": {"PEOPLE"},
    "third-member": {"member_feed", "member_scores", "explicit_seeds", "member_balance", "leap_feed", "ROW_OWNER"},
    "labels": {"who_label"},
    "phone": {"REMOTE_HTML", "REMOTE_JS"},
    "legacy": {"LEGACY"},
    "config": {"DEFAULT_TASTE", "TRAKT_PERSON"},
    "prompt": {"brief", "personal_feed", "week_feed", "docs_ai_feed", "person_line"},
}


def census(path):
    text = path.read_text()
    spans = []
    for node in ast.walk(ast.parse(text)):
        name = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
        if isinstance(node, ast.Assign):
            name = next((n.id for n in node.targets if isinstance(n, ast.Name)), name)
        if name and (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or any(name in names for names in KIND_MAP.values())):
            kind = next((k for k, names in KIND_MAP.items() if name in names), "other")
            spans.append((node.lineno, node.end_lineno, kind))
    counts = {}
    for i, line in enumerate(text.splitlines(), 1):
        hits = {name: int(name in line) for name in ("ben", "ana")}
        hits["PEOPLE"] = int("PEOPLE" in line)
        if not any(hits.values()):
            continue
        scope = sorted((b-a, k) for a, b, k in spans if a <= i <= b)
        kind = scope[0][1] if scope else "other"
        if ('inp=' in line or '"person": (' in line or 'choices=' in line) and (hits["ben"] or hits["ana"]):
            kind = "validator-enum"
        counts.setdefault(kind, Counter()).update(hits)
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--server", type=Path, help="compare a before-server copy")
    args = p.parse_args()
    print("area | kind | ben | ana | PEOPLE")
    for area in ("server.py", "skald", "eval", "tests"):
        paths = [args.server or args.root/area] if area.endswith(".py") else sorted((args.root/area).rglob("*.py"))
        counts = {}
        for path in paths:
            for kind, values in census(path).items():
                counts.setdefault(kind, Counter()).update(values)
        for kind, values in sorted(counts.items()):
            print(f"{area} | {kind} | {values['ben']} | {values['ana']} | {values['PEOPLE']}")
        total = sum(counts.values(), Counter())
        outside = sum((v for k, v in counts.items() if k != "legacy"), Counter())
        print(f"{area} | outside-legacy | {outside['ben']} | {outside['ana']} | {outside['PEOPLE']}")
        print(f"{area} | total | {total['ben']} | {total['ana']} | {total['PEOPLE']}")


if __name__ == "__main__":
    main()
