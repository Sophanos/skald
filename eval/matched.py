"""Frozen E1 matching and paired uncertainty, shared by replay and cold start."""
import random
import statistics
import hashlib
import json


def richness(rec):
    return sum(len(rec.get(field) or ()) for field in 'kdancgx')


class Matched:
    def __init__(self, server, universe):
        self.rich = {key: richness(server._facets.get(key, {})) for key in universe}
        values = sorted(self.rich.values())
        self.cuts = [values[len(values)*i//5] for i in range(1, 5)] if values else []
        self.bucket = {key: (server._facets.get(key, {}).get('kind'), key in server._emb,
                            sum(value > cut for cut in self.cuts)) for key, value in self.rich.items()}

    def distractors(self, key, candidates):
        if key not in self.bucket:
            return ()
        return tuple(sorted(d for d in candidates if d != key and self.bucket.get(d) == self.bucket[key]))

    def identity(self, key, candidates):
        return hashlib.sha256(json.dumps(self.distractors(key, candidates), separators=(',', ':')).encode()).hexdigest()

    def auc(self, key, scores, distractors):
        if key not in self.bucket:
            return None, 0
        target = scores.get(key)
        matched = self.distractors(key, distractors)
        if not matched:
            return None, 0
        # Unknown targets are misses; unknown distractors rank below known evidence.
        wins = sum(0 if target is None else 1 if scores.get(d) is None or target > scores[d]
                   else .5 if target == scores[d] else 0 for d in matched)
        return wins / len(matched), len(matched)


def paired_bootstrap(before, after):
    indexed = [{r.get('target_id', r['key']): r for r in rows} for rows in (before, after)]
    if any(len(index) != len(rows) for index, rows in zip(indexed, (before, after))):
        raise ValueError('duplicate E1 target identity')
    before, after = indexed
    if before.keys() != after.keys():
        raise ValueError('paired baseline must contain the same E1 targets')
    deltas = []
    for key in sorted(before):
        a, b = before[key], after[key]
        if any(a.get(field) != b.get(field) for field in ('key', 'kind', 'split', 'matched_n', 'matched_sha256', 'hard', 'signal')):
            raise ValueError(f'paired baseline matching differs: {key}')
        if a['mauc'] is None or b['mauc'] is None:
            raise ValueError(f'paired baseline has an unmatched target: {key}')
        deltas.append(b['mauc'] - a['mauc'])
    if not deltas:
        raise ValueError('paired baseline is empty')
    rng = random.Random(0)
    draws = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
    return {'n': len(deltas), 'delta': statistics.mean(deltas), 'ci95': [draws[50], draws[1949]],
            'resamples': 2000, 'seed': 0}


def coverage_gate(before, after):
    paired, checks = {}, []
    for label, select in (('all', lambda r: True), ('test', lambda r: r['split'] == 'test'),
                          ('hard', lambda r: r['hard']), ('loved', lambda r: r['signal'] == 'loved')):
        a, b = [r for r in before if select(r)], [r for r in after if select(r)]
        supported = label in ('all', 'test') or len(a) >= 30
        paired[label] = {**paired_bootstrap(a, b), 'supported': supported} if a else {
            'n': 0, 'delta': None, 'ci95': None, 'supported': supported}
        if supported:
            checks.append({'slice': label, 'pass': paired[label]['delta'] is not None and paired[label]['delta'] >= 0})
    return {'paired': paired, 'checks': checks, 'pass': all(c['pass'] for c in checks)}


def coverage_report(server, profile, overlay):
    from eval import replay, variants
    universe = sorted(set(server._facets) | set(server._emb))
    matching = Matched(server, universe)
    targets = replay.positives(profile)
    selected = [variants.REGISTRY['current']]
    rows = {}
    for arm, tags in (('original', {}), ('expanded', overlay)):
        with variants.experience_overlay(server, tags):
            rows[arm] = replay.loo(server, profile, targets, universe, selected, replay.SEED,
                                   matching=matching)['current']
    gate = coverage_gate(rows['original'], rows['expanded'])
    return {'scorer': list(server.SCORE_BLEND), 'universe_n': len(universe), 'overlay_n': len(overlay),
            'matching_frozen': True, 'arms': {arm: {label: replay.metrics([r for r in values if select(r)])
                for label, select in (('all', lambda r: True), ('test', lambda r: r['split'] == 'test'),
                    ('hard', lambda r: r['hard']), ('loved', lambda r: r['signal'] == 'loved'))}
                for arm, values in rows.items()}, **gate}


def snapshot_coverage(snapshot, overlay):
    from contextlib import redirect_stdout
    import io
    from pathlib import Path
    from unittest.mock import patch
    from skald.engine import Engine, denied
    with patch('urllib.request.urlopen', denied), redirect_stdout(io.StringIO()), Engine(snapshot) as engine:
        return {**coverage_report(engine._server, engine._profile, json.loads(Path(overlay).read_text())), **engine.provenance()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Frozen E1 original vs expanded experience coverage.')
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--overlay', required=True, help='JSON mapping facet slots to {kind, x}; missing tags only')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = snapshot_coverage(args.snapshot, args.overlay)
    if args.json:
        print(json.dumps(report, sort_keys=True, allow_nan=False))
    else:
        print('Evidence:', report['evidence'])
        print('slice | n | original mAUC | expanded mAUC | paired delta | 95% interval | check')
        for label, paired in report['paired'].items():
            print(f"{label} | {paired['n']} | {report['arms']['original'][label]['mauc']} | "
                  f"{report['arms']['expanded'][label]['mauc']} | {paired['delta']} | {paired['ci95']} | "
                  f"{('PASS' if paired['delta'] is not None and paired['delta'] >= 0 else 'FAIL') if paired['supported'] else 'low-n diagnostic'}")
        print('PASS' if report['pass'] else 'FAIL')
    return int(not report['pass'])


if __name__ == '__main__':
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
