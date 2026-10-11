#!/usr/bin/env python3
"""Reaction diagnostics with feasible entrants, row coverage and a frozen base taste limit."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import statistics
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval import reaction
from skald.engine import Engine

CHECKS = ('K1', 'K1feas', 'K1dir', 'R90', 'K2', 'K2b', 'K3F', 'K4', 'K5')


def row_changes(before, after, related):
    return {cid: {'tau': reaction.kendall(keys, after[cid]),
                  'departures': len(set(keys[:12]) - set(after[cid][:12]))}
            for cid, keys in before.items() if not set(keys[:24]) & related}


def measure(engine, key, person, related, pre_rows, limit):
    ranks, post_rows = [], []
    rank, served = engine._rank, reaction.served
    def ranked(*args):
        out = rank(*args)
        ranks.append(out)
        return out
    def rows(*args):
        out = served(*args)
        post_rows.append(out)
        return out
    with patch.object(engine, '_rank', side_effect=ranked), patch.object(reaction, 'served', side_effect=rows):
        result = reaction.measure(engine, key, person, related, pre_rows)
    before, after = ({k: i+1 for i, k in enumerate(keys)} for keys in ranks[:2])
    feasible = len(set(ranks[0][30:100]) & related) >= 2
    gains = [before[k]-after[k] for k in related & before.keys() & after.keys()]
    gain = statistics.median(gains) if gains else 0
    changes = row_changes(pre_rows, post_rows[0], related)
    coverage = sum(v['tau'] >= .70 and v['departures'] <= 3 for v in changes.values()) / len(changes) if changes else 1.
    result['checks'].pop('K3')
    result['checks'].update(K1feas=result['checks']['K1'] if feasible else None, K1dir=gain > 0,
                            R90=coverage >= .90, K3F=result['values']['K3'] <= limit)
    result['values'].update(K1feas=result['values']['K1'] if feasible else None, K1dir=gain,
                            R90=coverage, K3F=result['values']['K3'])
    result.update(K3_limit=limit, feasible=feasible, rows=changes,
                  worst_row=min(changes, key=lambda cid: (changes[cid]['tau'] >= .70 and changes[cid]['departures'] <= 3,
                                                         changes[cid]['tau'], -changes[cid]['departures'], cid), default=None))
    result['failed'] = [k for k in CHECKS if result['checks'][k] is False]
    result.pop('pass')
    return result


def run(snapshot, person=None, n=20, seed=7, base=None):
    with patch('time.time', return_value=reaction.NOW), redirect_stdout(io.StringIO()):
        limits = {}
        if base:
            with Engine(snapshot, server_path=base) as old:
                limits = {who: .5*reaction.percentile([v for v in old._model['paff'][who].values() if v > 0], .99)
                          for who in old._server.PEOPLE}
        with Engine(snapshot) as engine:
            out = {}
            for who in (person,) if person else engine._server.PEOPLE:
                selected, related = reaction.probes(engine, who, n, seed)
                with reaction.model_view(engine, engine._profile, engine._model):
                    rows = reaction.served(engine, who)
                limit = limits.get(who, .5*reaction.percentile([v for v in engine._model['paff'][who].values() if v > 0], .99))
                results = [measure(engine, key, who, related[key], rows, limit) for key in selected]
                checks = {}
                for check in CHECKS:
                    eligible = [r for r in results if r['checks'][check] is not None]
                    values = [r['values'][check] for r in eligible if r['values'][check] is not None]
                    checks[check] = {'n': len(eligible),
                                     'pass_rate': sum(r['checks'][check] for r in eligible)/len(eligible) if eligible else None,
                                     'median': statistics.median(values) if values else None}
                out[who] = {'n': len(results), 'checks': checks, 'K3_limit': limit,
                            'K3_median': statistics.median(r['values']['K3'] for r in results) if results else None,
                            'probes': results}
    return {'seed': seed, 'people': out}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    snapshot = snapshot if snapshot and snapshot.strip() else None
    ap.add_argument('--snapshot', type=Path, default=snapshot, required=not snapshot)
    ap.add_argument('--person')
    ap.add_argument('--n', type=int, default=20)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--base', type=Path)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if not 1 <= args.n <= 100:
        ap.error('--n must be between 1 and 100')
    result = run(args.snapshot, args.person, args.n, args.seed, args.base)
    if args.json:
        print(json.dumps(result, allow_nan=False))
    else:
        for who, report in result['people'].items():
            print(who, 'n', report['n'], 'K3 median', report['K3_median'], 'base limit', report['K3_limit'])
            for check, values in report['checks'].items():
                print(check, 'pass_rate', values['pass_rate'], 'median', values['median'], 'n', values['n'])
            for r in report['probes']:
                if r['failed']:
                    print(r['key'], 'failed', ','.join(r['failed']), 'worst row', r['worst_row'], r['rows'].get(r['worst_row']))


if __name__ == '__main__':
    main()
