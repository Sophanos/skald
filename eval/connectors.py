#!/usr/bin/env python3
"""Offline connector seeds and timing gates; counts and timings only."""
import argparse
from collections import Counter
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.engine import Engine
from eval.profile_golden import NOW, FIXTURE


def report(snapshot, network_idle=False):
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW), Engine(snapshot) as engine:
        s = engine._server
        if network_idle:
            s.CONFIG['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'auth': 'env:FAKE_TRAKT_TOKEN', 'options': {}}
            s.connectors_load(NOW)
            s._connectors['trakt_api']['next_due'] = NOW + 3600
        seeds = Counter(c['kind'] for c in s.connector_seeds(NOW).values())
        s.connectors_tick(NOW)
        times = []
        for _ in range(100):
            start = time.perf_counter()
            s.connectors_tick(NOW + 1)
            times.append((time.perf_counter() - start) * 1000)
        with s._connectors_lock:
            for c in s._connectors.values():
                if c['kind'] == 'file_drop':
                    c['next_due'] = NOW + 2
        start = time.perf_counter()
        s.connectors_tick(NOW + 2)
        due = (time.perf_counter() - start) * 1000
    p95 = sorted(times)[94]
    assert p95 < 1, f'warm idle p95 {p95:.3f} ms exceeds 1 ms'
    return {'seeds': dict(sorted(seeds.items())), 'manual_seeds': seeds['manual_export'],
            'warm_idle_ms': round(statistics.median(times), 3), 'warm_idle_p95_ms': round(p95, 3),
            'due_file_drop_ms': round(due, 3)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    ap.add_argument('--snapshot', type=Path, default=snapshot if snapshot and snapshot.strip() else None)
    ap.add_argument('--fixture', action='store_true')
    ap.add_argument('--network-idle', action='store_true', help='configured network adapter, not due; transport remains forbidden')
    args = ap.parse_args()
    if not args.snapshot and not args.fixture:
        ap.error('use --snapshot, SKALD_SNAPSHOT or --fixture')
    print(json.dumps(report(FIXTURE / 'data' if args.fixture else args.snapshot, args.network_idle), sort_keys=True))
