#!/usr/bin/env python3
"""Embedding dimension experiments on a read-only copy; reports contain private titles."""
import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval import embed_eval, replay
from skald.engine import Engine, OfflineNetworkError


def normalise(vector, dims):
    if not 0 < dims <= len(vector):
        raise ValueError('invalid dimensions')
    vector = vector[:dims]
    norm = math.sqrt(sum(x*x for x in vector))
    if not norm or not math.isfinite(norm):
        raise ValueError('invalid vector norm')
    return [x/norm for x in vector]


def read_vectors(path):
    doc = replay.read_json(path)
    doc['model'] = doc.get('model') or 'openai/text-embedding-3-small'
    dims = doc.get('dims', doc.get('dim'))
    if type(dims) is not int or dims < 1:
        raise ValueError('invalid dimensions')
    doc['dims'] = dims
    doc['vectors'] = {k: list(struct.unpack(f'{dims}e', base64.b64decode(v, validate=True)))
                      for k, v in doc['v'].items()}
    for v in doc['vectors'].values():
        normalise(v, dims)
    return doc


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def destination(data, out, private=False):
    data, out = Path(data).resolve(), Path(out).resolve()
    if out.is_relative_to(data) or (private and out.is_relative_to(ROOT)):
        raise ValueError('output must be outside the data copy and worktree')
    return out


class TextEngine(Engine):
    def _refresh(self):
        pass  # Text extraction must not build models or trigger automatic embedding/enrichment.

    def _title_text(self, kind, tid):  # Stored facets first; TMDB only for titles without them.
        return self._server.title_text(kind, tid)


def embed(args, transport):
    out = destination(args.data, args.out)
    if args.max_dims < 1 or args.limit is not None and args.limit < 1 or args.check_truncation < 0:
        raise ValueError('invalid dimensions or counts')
    if args.check_truncation and args.max_dims != 3072:
        raise ValueError('truncation check requires max-dims 3072')
    key = os.environ.get('OR_KEY') or os.environ.get('OPENROUTER_API_KEY', '')
    if not key:
        raise ValueError('missing embedding key')
    before = time.monotonic()
    source = replay.read_json(Path(args.data) / 'emb.json')
    keys, kinds, texts, skipped, usages = [], {}, [], 0, []
    # Server and provider diagnostics can contain credentials or private title text.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), TextEngine(args.data, offline=False) as engine:
        for k in sorted(source['v'])[:args.limit]:
            kind = (source.get('kinds') or {}).get(k) or engine._server._facets.get(k, {}).get('kind')
            try:
                if kind not in ('movie', 'tv'):
                    raise ValueError('missing kind')
                text = engine._title_text(kind, int(k.split('@')[0][5:]))
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('empty title text')
            except Exception:
                skipped += 1
                continue
            keys.append(k)
            kinds[k] = kind
            texts.append(text)

        def retry(url, payload, secret):
            for attempt in range(3):
                try:
                    response = transport(url, payload, secret)
                    embed_eval.embed(payload['input'], payload['model'], payload['dimensions'],
                                     lambda *args: response, '', '')
                    usages.append(response.get('usage') or {})
                    return response
                except Exception:
                    if attempt == 2:
                        raise RuntimeError('embedding batch failed') from None
                    time.sleep(2**attempt)

        base = os.environ.get('OPENROUTER_BASE', 'https://openrouter.ai')
        vectors, _ = embed_eval.embed(texts, args.model, args.max_dims, retry, key, base)
        check_n = min(args.check_truncation, len(texts))
        direct, _ = embed_eval.embed(texts[:check_n], args.model, 768, retry, key, base) if check_n else ([], {})
    cosine = sum(replay.cosine(v, normalise(full, 768)) for v, full in zip(direct, vectors))/check_n if check_n else None
    costs = [u.get('cost') for u in usages]
    tokens = [u.get('total_tokens', u.get('prompt_tokens')) for u in usages]
    doc = {'model': args.model, 'dims': args.max_dims, 'kinds': kinds,
           'v': {k: base64.b64encode(struct.pack(f'{args.max_dims}e', *v)).decode() for k, v in zip(keys, vectors)},
           'counts': {'source': len(source['v']), 'selected': len(keys)+skipped, 'embedded': len(keys),
                      'skipped': skipped, 'truncation_checked': check_n}, 'truncation_cosine': cosine,
           'total_cost': sum(costs) if costs and all(c is not None for c in costs) else None,
           'tokens': sum(tokens) if tokens and all(t is not None for t in tokens) else None}
    atomic(out, json.dumps(doc, allow_nan=False))
    print(f"embedded={len(keys)} skipped={skipped} cost={doc['total_cost']} tokens={doc['tokens']} seconds={time.monotonic()-before:.1f}")
    if check_n:
        print(f'truncation_checked={check_n} mean_cosine={cosine:.6f}')
    return doc


def install(server, arm, dims, truncate=False):
    server.EMB_DIM, server._emb_model = dims, arm['model']
    server._emb = {k: server.Embedding(normalise(v, dims) if truncate else v, arm['model'])
                   for k, v in arm['vectors'].items()}
    server._emb_kinds = {k: (arm.get('kinds') or {}).get(k) or server._facets.get(k, {}).get('kind')
                         for k in server._emb}
    server.clear_seed_cache()


def seed_variant():
    def factory(ctx):
        seeds = [e['key'] for e in replay.positives(ctx.profile)]
        return lambda key, person=None: ctx.server.seed_knn(seeds, key)
    return replay.variants.Variant('byw', factory)


def deltas(before, after):
    if not before:
        return {}
    result = {}
    for name, value in [('recall@10', lambda r: float(r is not None and r <= 10)),
                        ('recall@30', lambda r: float(r is not None and r <= 30)),
                        ('mrr', lambda r: 1/r if r is not None else 0.)]:
        # Reuse the paired bootstrap on per-target metric contributions instead of mAUC.
        contribution = lambda rows: [dict(key=r['key'], split=r['split'], matched_n=1,
                                           mauc=value(r['rank'])) for r in rows]
        result[name] = replay.paired_bootstrap(contribution(before), contribution(after))
    return result


def screen(server, profile, universe):
    model = replay.build_contexts(server, profile, [replay.variants.REGISTRY['current']], replay.SEED)['current'].model
    unseen = sorted(set(universe) - profile['seen'])
    views = {}
    for person in (*server.adult_people(), 'together'):
        entries = server.person_entries(profile, person)
        seeds = sorted(replay.positives({'entries': entries}), key=lambda e: (-server.entry_weight(e), e['key']))[:2]
        views[person] = {'top15': replay.ranking(lambda k, p=None: server.score_key(model, k, person)[0], unseen)[:15],
                         'because': {e['key']: replay.ranking(lambda k, p=None: server.seed_knn([e['key']], k), unseen)[:10]
                                     for e in seeds}}
    return views


def table(result):
    lines = ['| Arm | Dims | Weight | R@10 | R@30 | MRR | Median | Hard R@30 | BYW R@30 | Delta R@30 (95%) | MB |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|']
    def fmt(value):
        return '-' if value is None else f'{value:.4f}'
    for block in result['arms']:
        m, d = block['all'], block['delta'].get('recall@30')
        delta = '-' if not d else f"{d['delta']:.4f} [{d['ci95'][0]:.4f}, {d['ci95'][1]:.4f}]"
        lines.append('| ' + ' | '.join([block['arm'] + ': ' + block['model'], str(block['dims']), str(block['weight']),
            *[fmt(m[k]) for k in ('recall@10', 'recall@30', 'mrr')], 'infinity' if m['median_unbounded'] else fmt(m['median_rank']),
            fmt(block['hard']['recall@30']), fmt(block['byw']['recall@30']), delta, fmt(block['storage_mb'])]) + ' |')
    return lines


def report(result, titles):
    lines = ['# Embedding dimensions', '', 'Evidence: ' + result['evidence'],
             'P1 is leave-one-out over the cached universe. Scores are fractions.',
             'Best weights use hash-tune mAUC, ties prefer lower weight; screen lists are diagnostic score rankings.',
             'Static cached features can contain future information. Hard slices differ by arm. Storage excludes JSON/base64.',
             'Intervals resample paired targets 2000 times, seed 0; they do not capture tuning or shared training uncertainty.',
             '', *table(result), '', 'Per-person recall@30:', '']
    for block in result['arms']:
        values = ', '.join(f"{p}: {m['recall@30']} (n={m['n']})" for p, m in block['people'].items())
        lines.extend([f"{block['model']} / {block['dims']} / {block['weight']}: {values}", ''])
    lines.extend(['', '## Screen view', ''])
    baseline = result['screen']['baseline']
    def label(key):
        return titles.get(key, key).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    def columns(left, right):
        lines.extend(['| Baseline | Candidate |', '|---|---|'])
        for i in range(max(len(left), len(right))):
            a, b = left[i:i+1], right[i:i+1]
            lines.append('| ' + (label(a[0]) + (' [dropped]' if a[0] not in right else '') if a else '') +
                         ' | ' + (label(b[0]) + (' [entered]' if b[0] not in left else '') if b else '') + ' |')
        lines.append('')
    for name, views in result['screen'].items():
        if name == 'baseline':
            continue
        for person, view in views.items():
            lines.extend([f'### {person}: {name}', ''])
            columns(baseline[person]['top15'], view['top15'])
            for seed, keys in view['because'].items():
                lines.extend([f'Because you watched {label(seed)}', ''])
                columns(baseline[person]['because'][seed], keys)
    return '\n'.join(lines) + '\n'


def compare(data, vectors, dims, weights, out):
    out = destination(data, out, private=True)
    baseline, candidate = read_vectors(Path(data)/'emb.json'), read_vectors(vectors)
    if not dims or any(d < 1 or d > candidate['dims'] for d in dims) or not weights or any(not math.isfinite(w) or w < 0 for w in weights):
        raise ValueError('invalid dimensions or weights')
    result = {'arms': [], 'screen': {}, 'selection': 'hash-tune mAUC; lower weight breaks ties',
              'storage': {str(d): 2*d*len(candidate['vectors'])/1_000_000 for d in dims}}
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), Engine(data) as engine:
        result.update(engine.provenance())
        s, profile = engine._server, engine._profile
        universe = sorted(k for k in set(baseline['vectors']) | set(s._facets) if '@' not in k)
        live = s.SCORE_BLEND
        reference = None
        for arm, dimension in [(baseline, baseline['dims']), *[(candidate, d) for d in dims]]:
            install(s, arm, dimension, truncate=arm is candidate)
            blocks = []
            for weight in sorted(set([0., *weights])):
                s.SCORE_BLEND = (weight, *live[1:])
                raw = replay.loo(s, profile, replay.positives(profile), universe,
                                 [replay.variants.REGISTRY['current'], seed_variant()], replay.SEED)
                rows = raw['current']
                if reference is None:
                    reference = raw
                people = {p: replay.metrics(replay.loo(s, profile,
                    replay.positives({'entries': s.person_entries(profile, p)}), universe,
                    [replay.variants.REGISTRY['current']], replay.SEED, p)['current']) for p in s.adult_people()}
                block = {'arm': 'baseline' if arm is baseline else 'candidate',
                         'model': arm['model'], 'dims': dimension, 'weight': weight, 'titles': len(arm['vectors']),
                         **replay.summarize(rows), 'people': people, 'rows': rows, 'byw': replay.metrics(raw['byw']),
                         'delta': deltas(reference['current'], rows), 'byw_delta': deltas(reference['byw'], raw['byw']),
                         'storage_mb': dimension*2*len(arm['vectors'])/1_000_000}
                result['arms'].append(block)
                blocks.append(block)
                if arm is baseline and weight == 0:
                    result['screen']['baseline'] = screen(s, profile, universe)
            if arm is candidate:
                best = min(blocks, key=lambda b: (-(replay.metrics([r for r in b['rows'] if r['split'] == 'tune'])['mauc'] or 0), b['weight']))
                s.SCORE_BLEND = (best['weight'], *live[1:])
                result['screen'][f"{dimension} dims, weight {best['weight']}"] = screen(s, profile, universe)
        titles = {k: v.get('title') or k for k, v in s._facets.items()}
        titles.update({k: v['title'] for k, v in engine._index.items()})
        result['titles'] = titles
    atomic(out/'metrics.json', json.dumps(result, indent=2, allow_nan=False))
    atomic(out/'report.md', report(result, titles))
    return result


def main(argv=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('embed', 'compare'):
        p = commands.add_parser(name)
        p.add_argument('--data', required=True, type=Path)
        p.add_argument('--out', required=True, type=Path)
        if name == 'embed':
            p.add_argument('--model', default='google/gemini-embedding-2')
            p.add_argument('--max-dims', type=int, default=3072)
            p.add_argument('--limit', type=int)
            p.add_argument('--check-truncation', type=int, default=0)
        else:
            p.add_argument('--vectors', type=Path, required=True)
            p.add_argument('--dims', default='256,768,1536,3072')
            p.add_argument('--weights', default='0,.15,.3')
            p.add_argument('--network', choices=['none'], default='none')
    args = parser.parse_args(argv)
    try:
        if args.command == 'embed':
            embed(args, transport or embed_eval.live_transport)
        else:
            result = compare(args.data, args.vectors, [int(d) for d in args.dims.split(',')],
                             [float(w) for w in args.weights.split(',')], args.out)
            print('\n'.join(table(result)))
            for dim, mb in result['storage'].items():
                print(f'float16 storage dims={dim}: {mb:.4f} MB')
    except (Exception, OfflineNetworkError):
        print('Embedding experiment failed; check inputs, output location and transport.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
