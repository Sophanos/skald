#!/usr/bin/env python3
"""Offline who's-watching gates; print counts, hashes and milliseconds, never titles."""
import argparse
import os
from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval import profile_golden as golden
from eval.rows_report import home_server


def digest(value):
    return hashlib.sha256(golden.dump(value, True).encode()).hexdigest()


def both_sections(source, snapshot=None):
    original = golden.offline_server
    @contextmanager
    def with_sessions(*args, **kwargs):
        with original(*args, **kwargs) as s:
            # Exercise both attribution on every dated history item, including old windows.
            starts = sorted({it['at']/1000 for it in s.read_history()[:40] if it.get('at') is not None})
            events = [{'t': t, 'id': f'{i+1:032x}', 'who': 'both', 'via': 'phone', 'fix': None}
                      for i, t in enumerate(starts)]
            events.append({'t': max([golden.NOW, *starts])+4*3600, 'id': 'f'*32,
                           'who': 'off', 'via': 'phone', 'fix': None})
            Path(s.DATA, 'sessions.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            yield s
    with patch.object(golden, 'offline_server', with_sessions):
        return golden.sections(source, snapshot)


def rows(s):
    return {cid+'|'+typ: s.row_metas(cid, typ, 'de-DE') for cid, types, _ in s.feed_rows('de-DE') for typ in types}


def mean_score(s, metas):
    values = [s.score_key(s.get_model(False), x['id'], s.PEOPLE[1], 'movie' if x['type'] == 'movie' else 'tv')[0]
              for x in metas[:12]]
    values = [v for v in values if v is not None]
    return sum(values)/len(values) if values else 0


def report(snapshot, before=None):
    out = {}
    if before:
        # Raw replay additionally compares profile/model/rows against the exact base, without regeneration.
        base = golden.sections(before, snapshot)
        no_session = golden.sections(ROOT / 'server.py', snapshot)
        both = both_sections(ROOT / 'server.py', snapshot)
        assert base == no_session == both, 'snapshot golden changed'
        out['golden_sha256'] = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in no_session.items()}
        with redirect_stdout(io.StringIO()), home_server(snapshot, before) as old:
            base_rows = rows(old)
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=golden.NOW), home_server(snapshot) as s:
        baseline = rows(s)
        if before:
            assert baseline == base_rows, 'served rows differ from base'
        out['rows_sha256'] = digest(baseline)
        out['row_count'] = len(baseline)
        out['foryou_mean'] = mean_score(s, baseline[rid('foryou|tv', s.CONFIG)])
        s.write_session({'who': 'both'}, 'phone')
        out['both_rows_identical'] = rows(s) == baseline
        assert out['both_rows_identical'], 'both changed rows'
        out['people'] = {}
        for who in reversed(s.PEOPLE):
            s.write_session({'who': who}, 'phone')
            current = rows(s)
            changed = {}
            for key, metas in current.items():
                if key.split('|')[0] not in s.PERSON_ROWS:
                    continue
                old = [x['id'] for x in baseline[key]]
                new = [x['id'] for x in metas]
                changed[key] = {'moved': sum(i >= len(old) or x != old[i] for i, x in enumerate(new))
                    + max(0, len(old)-len(new)), 'new_first12': len(set(new[:12])-set(old[:12]))}
            out['people'][who] = {'alpha': s.person_alpha(s.get_model(False), who),
                                   'rows': changed, 'foryou_mean': mean_score(s, current[rid('foryou|tv', s.CONFIG)])}
        assert out['people'][s.PEOPLE[1]]['foryou_mean'] >= out['foryou_mean'], 'For You person mean regressed'
        out['p95_ms'] = {}
        for who in ('off', s.PEOPLE[1]):
            s.write_session({'who': who}, 'phone')
            s.row_metas(rid('lane5', s.CONFIG), 'tv', 'de-DE')
            elapsed = []
            for _ in range(30):
                start = time.perf_counter()
                s.row_metas(rid('lane5', s.CONFIG), 'tv', 'de-DE')
                elapsed.append((time.perf_counter()-start)*1000)
            out['p95_ms']['none' if who == 'off' else who] = round(sorted(elapsed)[28], 3)
        assert out['p95_ms'][s.PEOPLE[1]]-out['p95_ms']['none'] <= 25, 'session p95 grew over 25 ms'
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot', type=Path, default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    ap.add_argument('--before', type=Path)
    args = ap.parse_args()
    with redirect_stdout(io.StringIO()):
        result = report(args.snapshot, args.before)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
