#!/usr/bin/env python3
"""Frozen reviewer seed probes; no weight selection or network."""
import argparse
from functools import cache
import json
import math
from pathlib import Path

if __package__:
    from . import replay
else:
    import replay


def load_probes(server, path=None):
    probes = replay.read_json(path) if path is not None else replay.probe_data('probes_seed.json')
    if path is None:
        probes = [p for p in probes if all(i['key'] in server._facets for i in [p['seed'], *p['targets']])] if probes else []
    for probe in probes:
        for item in [probe['seed'], *probe['targets']]:
            rec = server._facets.get(item['key'], {})
            if (rec.get('title'), rec.get('kind')) != (item['title'], item['kind']):
                raise ValueError(f"seed probe title/kind mismatch: {item['key']} ({item['title']}, {item['kind']})")
    return probes


def scorers(server, universe):
    pairs = {k: {fid for fid, _ in server.facet_pairs(server._facets.get(k, {}))} for k in universe}
    df = {}
    for rec in server._facets.values():
        for fid, _ in server.facet_pairs(rec):
            df[fid] = df.get(fid, 0) + 1
    n = len(server._facets) + 1
    weights = {fid: server.FACET_W[fid[0]] * max(.05, math.log(n/(count+1))/math.log(n))
               for fid, count in df.items()}

    @cache
    def cosine(a, b):
        with server._emb_lock:
            return replay.cosine(server._emb.get(a), server._emb.get(b))

    def closeness(a, b):
        theme = lambda key: {fid: 1 for fid in pairs[key] if fid[0] == 't'}
        xs = server.xsim(server._facets.get(a, {}).get('x'), server._facets.get(b, {}).get('x'))
        return .55 * (cosine(a, b) or 0) + .45 * max(xs, server.xsim(theme(a), theme(b)))

    def reference(a, b):
        overlap = sum(weights[fid] for fid in sorted(pairs[a] & pairs[b]))
        return .5 * (cosine(a, b) or 0) + .5 * math.tanh(overlap/2)

    # The merged baseline predates the helper; evaluate its frozen specification there.
    return {'cosine': cosine, 'if1_closeness': closeness,
            'seed_similarity': cache(getattr(server, 'seed_similarity', reference))}


def sign_test(before, after):
    ranks = lambda rows: {(r['seed'], r['key']): r['rank'] or math.inf for r in rows}
    a, b = ranks(before), ranks(after)
    if a.keys() != b.keys():
        raise ValueError('sign test requires identical probe pairs')
    wins, losses = sum(b[k] < a[k] for k in a), sum(b[k] > a[k] for k in a)
    n = wins + losses
    p = min(1., 2 * sum(math.comb(n, i) for i in range(min(wins, losses)+1)) / 2**n)
    return {'wins': wins, 'losses': losses, 'ties': len(a)-n, 'p': p}


def run(server, universe, path=None):
    probes = load_probes(server, path)
    if not probes:
        return {'skipped': 'No evaluation probe titles are cached.'}
    result = {'reference_helper': not hasattr(server, 'seed_similarity'), 'scorers': {}}
    for name, score in scorers(server, universe).items():
        rows = []
        for probe in probes:
            seed = probe['seed']['key']
            values = sorted((-value, k) for k in universe if k != seed
                            if (value := score(seed, k)) is not None)
            ranks = {k: i+1 for i, (_, k) in enumerate(values)}
            rows.extend({'seed': seed, **t, 'rank': ranks.get(t['key']),
                         'franchise': probe.get('franchise', False)} for t in probe['targets'])
        analogues = [r for r in rows if not r['franchise']]
        franchises = [r for r in rows if r['franchise']]
        result['scorers'][name] = {**replay.metrics(analogues), 'rows': rows,
            'franchise_hit@10': sum(r['rank'] is not None and r['rank'] <= 10 for r in franchises),
            'franchise_n': len(franchises)}
    base = [r for r in result['scorers']['cosine']['rows'] if not r['franchise']]
    for name, block in result['scorers'].items():
        block['sign_vs_cosine'] = sign_test(base, [r for r in block['rows'] if not r['franchise']])
        print(f"E2 {name}: R@10 {round(block['recall@10']*block['n'])}/{block['n']}, "
              f"R@30 {round(block['recall@30']*block['n'])}, median {block['median_rank']}, "
              f"franchise {block['franchise_hit@10']}/{block['franchise_n']}, "
              f"sign p={block['sign_vs_cosine']['p']:.6g}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default=replay.DEFAULT_SNAPSHOT)
    parser.add_argument('--probes', type=Path, help='seed probe JSON list; defaults to eval/probes.example.json')
    parser.add_argument('--out', default='eval/out/w2b')
    args = parser.parse_args()
    output = replay.workspace_output(args.out)
    with replay.offline_server(Path(args.snapshot), output) as server:
        profile = replay.read_json(Path(args.snapshot) / 'profile.json')
        replay.build_contexts(server, replay.profile_with(profile, profile['entries']),
                              [replay.variants.REGISTRY['current']], replay.SEED)
        result = run(server, sorted(set(server._emb) | set(server._facets)), args.probes)
    (output/'seed_probes.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')


if __name__ == '__main__':
    main()
