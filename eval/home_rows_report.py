#!/usr/bin/env python3
"""Read-only home aggregates: python eval/home_rows_report.py --data PATH."""
import argparse
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skald.engine import Engine


def scope_report(s, lang=None):
    lang = lang or s.LANG
    report = {}
    for scope in (None, *s.SCOPES):
        with s.addon_scope(scope):
            who = s.SCOPES[scope] if scope is not None else s.who_watching()[0]
            with patch.object(s, 'human_cards', side_effect=lambda xs, *a: xs):
                home = s.build_home(lang, s._profiles.get('household') or {}, s.row_masks(who), who)
            rows = []
            for cid, types, _ in s.feed_rows(lang, who=who):
                for typ in (('tv',) if 'tv' in types else types):
                    metas = s.shelf_items(cid, typ, lang, who) if cid in s.SHELF_ROWS else home.get((cid, typ), [])
                    if metas:
                        rows.append(metas)
            cards = [x for xs in rows for x in xs]
            franchises = Counter(key for xs in rows for key in {s.franchise_key(x) for x in xs})
            most, count = max(franchises.items(), key=lambda x: (x[1], x[0]), default=(None, 0))
            report[scope or 'household'] = {
                'rows': len(rows), 'cards': len(cards),
                'borrowed_cards': sum(bool(x.get('_borrowed')) for x in cards),
                'superhero_cards': sum(s.is_hero(x) for x in cards),
                'superhero_rows': sum(any(s.is_hero(x) for x in xs) for xs in rows),
                'most_repeated_franchise': hashlib.sha256(most.encode()).hexdigest()[:12] if most else None,
                'most_repeated_franchise_rows': count,
                'unique_titles': len({(x['id'], x['type']) for x in cards}),
            }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--lang')
    args = parser.parse_args()
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), Engine(args.data, offline=True) as engine:
        if args.lang and not engine._server.LANG_RE.fullmatch(args.lang):
            parser.error('invalid language')
        report = scope_report(engine._server, args.lang)
    for scope, stats in report.items():
        print(scope + ': ' + json.dumps(stats, sort_keys=True))


if __name__ == '__main__':
    main()
