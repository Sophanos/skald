#!/usr/bin/env python3
"""Compare embeddings on synthetic profile fixtures using production scoring."""
import argparse
from array import array
from contextlib import redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval import profile_golden, replay

WEIGHTS = (0, .1, .2, .3)


def live_transport(url, payload, key):
    request = Request(url, json.dumps(payload).encode(),
                      {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    # Keep this explicit transport separate from the fixture server's network guard.
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def fixture_transport(url, payload, key):
    data = []
    for i, text in enumerate(payload['input']):
        digest = hashlib.shake_256((payload['model'] + '\0' + text).encode()).digest(2 * payload['dimensions'])
        vector = [int.from_bytes(digest[j:j+2], 'big') / 32767.5 - 1
                  for j in range(0, len(digest), 2)]
        data.append({'index': i, 'embedding': vector})
    return {'data': data, 'usage': {'cost': 0.}}


def embed(texts, model, dims, transport, key, base, clock=time.perf_counter):
    vectors, latencies, costs = [], [], []
    for start in range(0, len(texts), 64):
        batch = texts[start:start+64]
        before = clock()
        response = transport(base.rstrip('/') + '/api/v1/embeddings',
                             {'model': model, 'dimensions': dims, 'input': batch}, key)
        latencies.append((clock() - before) * 1000)
        rows = sorted(response['data'], key=lambda row: row['index'])
        if [r['index'] for r in rows] != list(range(len(batch))):
            raise ValueError('embedding response indices do not match batch')
        for row in rows:
            vector = row['embedding']
            if len(vector) != dims or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
                raise ValueError('embedding response has invalid dimensions or values')
            norm = math.sqrt(sum(x*x for x in vector))
            if not norm or not math.isfinite(norm):
                raise ValueError('embedding response has invalid norm')
            vectors.append(array('f', (x/norm for x in vector)))
        cost = (response.get('usage') or {}).get('cost')
        if cost is not None and (not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0):
            raise ValueError('invalid embedding usage cost')
        costs.append(cost)
    total = sum(costs) if costs and all(c is not None for c in costs) else None
    return vectors, {'total_cost': total,
                     'reported_cost': sum(c for c in costs if c is not None),
                     'cost_per_1000_titles': total * 1000/len(texts) if total is not None else None,
                     'cost_batches_reported': sum(c is not None for c in costs),
                     'batches': len(costs), 'vector_bytes_per_title': dims * 2,
                     'p50_batch_latency_ms': statistics.median(latencies) if latencies else None}


def metrics(ranks):
    n = len(ranks)
    hits = [rank for rank in ranks if rank is not None and rank <= 20]
    return {'n': n, 'recall@20': len(hits)/n if n else None,
            'ndcg@20': sum(1/math.log2(r+1) for r in hits)/n if n else None}


def evaluate(server, profile, universe):
    seed_ranks, blend_ranks = [], {w: {'tune': [], 'test': []} for w in WEIGHTS}
    for target in replay.positives(profile):
        train = replay.leave_out(profile, [target['key']])
        context = replay.build_contexts(server, train, [replay.variants.REGISTRY['current']],
                                        replay.SEED)['current']
        seeds = [e['key'] for e in replay.positives(train)]
        candidates = sorted(set(universe) - train['seen'])

        def rank(score):
            order = replay.ranking(lambda key, person=None: score(key), candidates)
            return order.index(target['key'])+1 if target['key'] in order else None

        seed_ranks.append(rank(lambda key: server.seed_knn(seeds, key)))
        for weight in WEIGHTS:
            server.SCORE_BLEND = (weight, *server.SCORE_BLEND[1:])
            blend_ranks[weight][replay.split_for(target['key'])].append(
                rank(lambda key: server.score_key(context.model, key)[0]))
    blends = {w: {split: metrics(ranks) for split, ranks in groups.items()}
              for w, groups in blend_ranks.items()}
    best = min(WEIGHTS, key=lambda w: (-(blends[w]['tune']['ndcg@20'] or 0), w))
    return {'seed_similarity': metrics(seed_ranks), 'best_embedding_weight': best,
            'best_blend': blends[best]['test'], 'blends': blends,
            'selection': 'highest hash-tune ndcg@20, ties prefer lower weight; best_blend uses hash-test'}


def run(models, transport, key='', base='https://openrouter.ai', fixture=False):
    stubs = replay.read_json(profile_golden.FIXTURE / 'tmdb.json')
    details = {f"{kind}/{det['id']}": det for kind, det in stubs['details'].values()
               if not det.get('raise')}
    details.update({f"{p['kind']}/{p['pick']['id']}": p['pick'] for p in stubs['picks']})
    result = {'mode': 'fixture' if fixture else 'live',
              'vector_storage': 'float16 payload bytes, as in emb.json; excludes JSON/base64 overhead',
              'seed_aggregation': 'production seed_knn: mean of the top three positive-seed seed_similarity scores',
              'protocol': 'replay P1 leave-one-out loved/watched targets; synthetic diagnostic', 'models': []}
    with profile_golden.offline_server(ROOT / 'server.py') as server:
        with redirect_stdout(io.StringIO()):
            items = server.read_history()
            profiles = {'household': server.build_profile(items, ['q1', 'q2'], server.read_trakt(), remote=True),
                        'plain': server.build_profile(items, [], (), remote=False)}
        # All title details come from the existing public fixture, including resolved picks.
        server.tmdb = lambda path, **kwargs: details[path]
        universe, texts = [], []
        for path in sorted(details, key=lambda p: (int(p.split('/')[1]), p)):
            kind, tid = path.split('/')
            title_key = 'tmdb:' + tid
            universe.append(title_key)
            texts.append(server.title_text(kind, tid))
            server._facets[title_key] = server.facet_record(kind, tid)
            server._emb_kinds[title_key] = kind
        server.tmdb = profile_golden.denied
        for model, dims in models:
            vectors, stats = embed(texts, model, dims, transport, key, base)
            if fixture:
                stats['p50_batch_latency_ms'] = 0.  # Fixture runs are deterministic, not timing benchmarks.
            server.EMB_DIM = dims
            server._emb = dict(zip(universe, vectors))
            server.clear_seed_cache()
            block = {'model': model, 'dimensions': dims, 'titles': len(texts), **stats, 'profiles': {}}
            for name, profile in profiles.items():
                block['profiles'][name] = evaluate(server, profile, universe)
            result['models'].append(block)
    return result


def model_specs(value):
    specs = []
    for item in value.split(','):
        try:
            model, dimension = item.rsplit(':', 1)
            dims = int(dimension)
            if not model.strip() or dims < 1:
                raise ValueError
        except ValueError:
            raise argparse.ArgumentTypeError('models must be comma-separated model:positive-dimensions') from None
        specs.append((model.strip(), dims))
    return specs


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True, type=model_specs)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fixture', action='store_true', help='deterministic fake vectors, no network or key')
    mode.add_argument('--live', action='store_true', help='embed public fixture titles through OpenRouter')
    args = parser.parse_args(argv)
    key = '' if args.fixture else os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_KEY', '')
    if args.live and not key:
        print('Live mode requires OPENROUTER_API_KEY or OR_KEY.', file=sys.stderr)
        return 1
    try:
        result = run(args.models, transport or (fixture_transport if args.fixture else live_transport),
                     key, os.environ.get('OPENROUTER_BASE', 'https://openrouter.ai'), args.fixture)
    except Exception:
        # Provider/transport exceptions may contain request headers or credentials.
        print('Embedding comparison failed; check model dimensions, transport and fixture inputs.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
