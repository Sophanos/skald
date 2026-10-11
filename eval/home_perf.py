#!/usr/bin/env python3
"""Offline home identity and CPU gates; emit only counts, hashes and timings."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval.profile_golden import NOW
from eval.rows_report import home_server


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def identity(s):
    requests = [(cid, typ) for cid, types, _ in s.feed_rows('de-DE') for typ in types]
    out = {}
    for who in ('none', 'both', *reversed(s.PEOPLE)):
        if who != 'none':
            s.write_session({'who': who}, 'phone')
        with patch.object(s, '_HOME_CACHE_ENABLED', False):
            before = digest([s.row_metas(cid, typ, 'de-DE') for cid, typ in requests])
        after = digest([s.row_metas(cid, typ, 'de-DE') for cid, typ in requests])
        assert before == after, f'cache identity failed: {who}'
        out[who] = after
    return {'rows_types': len(requests), 'sessions': len(out), 'sha256': out}


def measure(s):
    requests = [(rid(x.split(':')[0], s.CONFIG), x.split(':')[1]) for x in
                'episodes:series foryou:tv week:tv because:tv catchup:tv upcoming:tv leap:tv MEMBER:tv rewatch:tv top10:movie top10:series hbo:tv paramount:tv mood1:tv mood2:tv mood3:tv mood4:tv doctrend:tv wonder:tv cosmos:tv history:tv meaning:tv lane1:tv lane2:tv lane3:tv lane4:tv lane5:tv airing:series nextseason:series'.replace('MEMBER', s.PEOPLE[0]).split()]
    out = {'display_rows': len(requests)}
    for label in ('cold', 'warm'):
        start = time.process_time()
        rows = [s.row_metas(cid, typ, 'de-DE') for cid, typ in requests]
        out[label + '_cpu_ms'] = round((time.process_time() - start) * 1000, 3)
        out[label + '_sha256'] = digest(rows)
    elapsed = []
    for _ in range(30):
        start = time.perf_counter()
        s.row_metas(rid('lane5', s.CONFIG), 'tv', 'de-DE')
        elapsed.append((time.perf_counter() - start) * 1000)
    out['lane5_p95_ms'] = round(sorted(elapsed)[28], 3)
    return out


def report(snapshot, before):
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW):
        with home_server(snapshot, before) as old:
            base = measure(old)
        with home_server(snapshot) as s:
            current = measure(s)
            gate_a = identity(s)
    assert current['cold_sha256'] == current['warm_sha256'], 'warm home identity failed'
    assert current['cold_cpu_ms'] <= base['cold_cpu_ms'] * 1.10, f"home CPU exceeds base +10%: {current['cold_cpu_ms']} vs {base['cold_cpu_ms']} ms"
    return {'base': base, 'current': current, 'gate_a': gate_a}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    ap.add_argument('--snapshot', type=Path, default=snapshot if snapshot and snapshot.strip() else None)
    ap.add_argument('--before', type=Path, required=True)
    args = ap.parse_args()
    if not args.snapshot:
        ap.error('use --snapshot or SKALD_SNAPSHOT')
    print(json.dumps(report(args.snapshot, args.before), sort_keys=True))
