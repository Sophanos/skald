#!/usr/bin/env python3
"""Ben proxy: hash-tune explicit seeds, hash-test targets, five fixed draws."""
import argparse
import json
from pathlib import Path
import random
import statistics
from unittest.mock import patch

if __package__:
    from . import replay, seed_probes
    from .matched import Matched
else:
    import replay, seed_probes
    from matched import Matched


def curve(server, profile, universe):
    mine = [e for e in server.person_entries(profile, server.PEOPLE[1])
            if e.get('explicit') and e['weight'] > 0 and e['signal'] in ('loved', 'watched')]
    pool = [e for e in mine if replay.split_for(e['key']) == 'tune']
    test = [e for e in mine if replay.split_for(e['key']) == 'test']
    targets = {e['key'] for e in test}
    if len(pool) < 40 or not test:
        raise ValueError('cold-start requires >=40 tune positives and nonempty test positives')
    negatives = sorted(set(universe) - profile['seen'] - targets)
    candidates = sorted(set(negatives) | targets)
    matching = Matched(server, universe)
    similarity = seed_probes.scorers(server, universe)['seed_similarity']
    # Cache the production helper's pair scores once across overlapping draws.
    reference = lambda seeds, key: statistics.mean(sorted((similarity(s, key) for s in seeds), reverse=True)[:3])
    seed_knn = getattr(server, 'seed_knn', reference)
    result = {'pool_n': len(pool), 'test_n': len(test), 'draw_seed': '1000*k + draw', 'k': {},
              'reference_helper': not hasattr(server, 'seed_knn')}
    for k in (5, 10, 20, 40):
        draws = {'person': [], 'seed_knn': [], 'prior': []}
        for draw in range(5):
            sample = random.Random(1000*k + draw).sample(pool, k)
            train = replay.profile_with(profile, sample)
            ctx = replay.build_contexts(server, train, [replay.variants.REGISTRY['current']], replay.SEED)['current']
            # build_model applies the same n_explicit support threshold as production.
            prior_train = replay.profile_with(profile, sample + [e for e in profile['entries']
                if server.PEOPLE[1] not in e.get('by', ()) and e['key'] not in targets])
            prior = replay.build_contexts(server, prior_train, [replay.variants.REGISTRY['current']], replay.SEED)['current']
            seeds = [e['key'] for e in sample]
            for name, score in [('person', lambda key: server.score_key(ctx.model, key, server.PEOPLE[1])[0]),
                                ('seed_knn', lambda key: seed_knn(seeds, key)),
                                ('prior', lambda key: server.person_score(prior.model, key, server.PEOPLE[1])[0])]:
                with patch.object(server, 'seed_similarity', similarity, create=True):
                    scores = {key: score(key) for key in candidates}
                aucs = [matching.auc(e['key'], scores, negatives)[0] for e in test]
                if any(a is None for a in aucs):
                    raise ValueError('cold-start target has no matched distractors')
                draws[name].append(statistics.mean(aucs))
        result['k'][str(k)] = {name: {'mean_mauc': statistics.mean(values), 'min_mauc': min(values),
                                     'draws': values} for name, values in draws.items()}
        for name, block in result['k'][str(k)].items():
            print(f"E3 k={k} {name}: mAUC {block['mean_mauc']:.4f} (min {block['min_mauc']:.4f})", flush=True)
    result['non_inferior'] = all(b['prior']['mean_mauc'] >= b['person']['mean_mauc'] - .01 for b in result['k'].values())
    print('Household prior: ' + ('non-inferior; on' if result['non_inferior'] else 'inferior; off'), flush=True)
    return result


def run(server, profile, universe):
    with patch.dict(server._learn, {'hits': []}):
        return curve(server, profile, universe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default=replay.DEFAULT_SNAPSHOT)
    parser.add_argument('--out', default='eval/out/w2b')
    args = parser.parse_args()
    output = replay.workspace_output(args.out)
    with replay.offline_server(Path(args.snapshot), output) as server:
        profile = replay.read_json(Path(args.snapshot) / 'profile.json')
        profile = replay.profile_with(profile, profile['entries'])
        result = run(server, profile, sorted(set(server._emb) | set(server._facets)))
    result['evidence'] = 'cached-profile'
    print('Evidence: cached-profile')
    (output/'cold_start.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')


if __name__ == '__main__':
    main()
