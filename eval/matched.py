"""Frozen E1 matching and paired uncertainty, shared by replay and cold start."""
import random
import statistics


def richness(rec):
    return sum(len(rec.get(field) or ()) for field in 'kdancgx')


class Matched:
    def __init__(self, server, universe):
        self.rich = {key: richness(server._facets.get(key, {})) for key in universe}
        values = sorted(self.rich.values())
        self.cuts = [values[len(values)*i//5] for i in range(1, 5)] if values else []
        self.bucket = {key: (server._facets.get(key, {}).get('kind'), key in server._emb,
                            sum(value > cut for cut in self.cuts)) for key, value in self.rich.items()}

    def auc(self, key, scores, distractors):
        if key not in self.bucket:
            return None, 0
        target = scores.get(key)
        matched = [d for d in distractors if d != key and self.bucket[d] == self.bucket[key]]
        if not matched:
            return None, 0
        # Unknown targets are misses; unknown distractors rank below known evidence.
        wins = sum(0 if target is None else 1 if scores.get(d) is None or target > scores[d]
                   else .5 if target == scores[d] else 0 for d in matched)
        return wins / len(matched), len(matched)


def paired_bootstrap(before, after):
    before, after = {r['key']: r for r in before}, {r['key']: r for r in after}
    if before.keys() != after.keys():
        raise ValueError('paired baseline must contain the same E1 targets')
    deltas = []
    for key in sorted(before):
        a, b = before[key], after[key]
        if a['split'] != b['split'] or a['matched_n'] != b['matched_n']:
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
