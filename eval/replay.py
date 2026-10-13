#!/usr/bin/env python3
"""Deterministic, stdlib-only offline replay of Skald scoring."""
import argparse
from contextlib import contextmanager, ExitStack, redirect_stdout
import copy
import datetime
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

if __package__:
    from . import variants
    from .matched import Matched, paired_bootstrap, richness
else:
    import variants
    from matched import Matched, paired_bootstrap, richness

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = None
SEED = 20261011
FILES = ('profile.json', 'emb.json', 'facets.json', 'learn.json', 'taste.json',
         'feeds.json')
BUGS = [
    {'path': 'server.py:add_person_loves', 'finding': 'Fixed: explicit love upgrades equal-weight watched evidence; '
     'source-specific opinions retain the other person\'s original signal. Covered in tests/test_engine.py.'},
    {'path': 'server.py:build_profile', 'finding': 'Fixed: duplicate Trakt records stay owner-only; TV/Trakt '
     'overlap retains shared attribution and a separate owner opinion. Covered in tests/test_engine.py.'},
    {'path': 'server.py:score_key', 'finding': 'Fixed: sparse person models use their explicit seeds/facets, '
     'never the household vector. Covered in tests/test_engine.py.'},
]


class OfflineNetworkError(BaseException):
    """BaseException ensures the server's broad except Exception cannot hide a missed path."""


class CalibrationSkipped(Exception):
    pass


def read_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def workspace_output(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / 'eval' / 'out'):
        raise ValueError('--out must be inside this worktree/eval/out (no external writes)')
    return path


@contextmanager
def offline_server(snapshot, work):
    """Copy only public eval inputs; guard urlopen before importing, not just before scoring."""
    snapshot, work = Path(snapshot).resolve(), workspace_output(work)
    if snapshot == work or snapshot.is_relative_to(work):
        raise ValueError('snapshot must not be inside the output directory')
    for name in FILES:
        if not (snapshot / name).is_file():
            raise ValueError(f'missing snapshot file: {name}')
    work.mkdir(parents=True, exist_ok=True)
    attempts = []

    def denied(*args, **kwargs):
        attempts.append('blocked network/API call')
        raise OfflineNetworkError('offline replay blocked a network/API call')

    def offline_tmdb(path, **kwargs):
        if path.startswith('trending/'):
            raise CalibrationSkipped('offline trending calibration skipped')
        return denied()

    with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=work) as temp, ExitStack() as stack:
        for name in FILES:
            shutil.copyfile(snapshot / name, Path(temp) / name)
        for name in ('.skald-format', 'skald.json', 'legacy.json'):
            path = Path(snapshot) / name
            if path.is_file():
                shutil.copyfile(path, Path(temp) / name)
        if (snapshot / 'feedback.jsonl').is_file():
            shutil.copyfile(snapshot / 'feedback.jsonl', Path(temp) / 'feedback.jsonl')
        stack.enter_context(patch('urllib.request.urlopen', denied))
        stack.enter_context(patch.dict(os.environ, {'TMDB_API': 'offline', 'DATA_DIR': temp,
                                                   'OPENROUTER_API_KEY': ''}))
        stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        spec = importlib.util.spec_from_file_location('skald_replay_server', ROOT / 'server.py')
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        server.tmdb, server.openrouter, server.anilist = offline_tmdb, denied, denied
        for name in ('ensure_emb', 'ensure_facets', 'tag_experiences'):
            setattr(server, name, lambda *args, **kwargs: None)
        server.load_emb()
        loaded_emb = len(server._emb)  # Validate decoding before mixed-record repair drops vectors.
        server.load_facets()
        emb = read_json(Path(temp) / 'emb.json')
        if emb.get('dim') != server.EMB_DIM or (emb.get('model') or 'openai/text-embedding-3-small') != server._emb_model:
            raise ValueError('snapshot embedding model/dimension differs from server')
        if loaded_emb != len(emb['v']) or not server._facets:
            raise ValueError('snapshot embeddings/facets failed to load')
        server._emb = dict(sorted(server._emb.items()))
        server._facets = dict(sorted(server._facets.items()))
        server._learn.update(read_json(Path(temp) / 'learn.json'))
        server.offline_attempts = attempts
        try:
            yield server
            if attempts:
                raise OfflineNetworkError('a blocked call was caught; offline run is invalid')
        finally:
            for name in ('_pool', '_outer', '_nzb_pool'):
                getattr(server, name).shutdown(wait=True, cancel_futures=True)


def profile_with(source, entries, hidden=()):
    p = dict(source)
    p['entries'] = list(entries)
    p['seen'] = set(source.get('seen', ())) - set(hidden)
    p['negative'] = {e['key'] for e in entries if e['weight'] < 0}
    return p


def leave_out(profile, keys):
    keys = set(keys)
    return profile_with(profile, [e for e in profile['entries'] if e['key'] not in keys], keys)


def positives(profile):
    return sorted((e for e in profile['entries'] if e['signal'] in ('loved', 'watched')
                   and e['weight'] > 0), key=lambda e: e['key'])


def split_for(key):
    return 'tune' if hashlib.sha256(key.encode()).digest()[0] % 2 == 0 else 'test'


def cosine(a, b):
    if not a or not b:
        return None
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / norm if norm else None


def nearest_remaining(server, key, profile):
    value = server._emb.get(key)
    sims = [cosine(value, server._emb.get(e['key'])) for e in profile['entries']]
    return max((s for s in sims if s is not None), default=None)


def same_franchise(server, key, profile):
    def identity(key, title=''):
        rec = server._facets.get(key, {})
        collection = rec.get('collection') or rec.get('belongs_to_collection')
        if isinstance(collection, dict):
            collection = collection.get('id')
        # Old caches lack collections: strip subtitle, saga suffix and sequel/season numbers.
        title = rec.get('title') or title
        base = server.base_title(re.split(r':| and the ', title, flags=re.I)[0])
        base = re.sub(r'\s+(?:vol(?:ume)?\s*)?(?:\d+|[ivx]+)$', '', base)
        return collection, base
    col, base = identity(key)
    for e in profile['entries']:
        other_col, other_base = identity(e['key'], e['title'])
        if (col and col == other_col) or (base and base == other_base):
            return True
    return False


def metrics(rows):
    ranks = [r['rank'] for r in rows]
    n = len(ranks)
    known = [r for r in ranks if r is not None]
    # Unscorable targets count as misses and infinite ranks, not dropped successes.
    median = statistics.median([r if r is not None else math.inf for r in ranks]) if n else None
    return {'n': n, **{f'recall@{k}': sum(r is not None and r <= k for r in ranks) / n
                      if n else None for k in (10, 30, 100)},
            **{f'ndcg@{k}': sum(1/math.log2(r+1) for r in known if r <= k)/n
               if n else None for k in (10, 30, 100)},
            'mrr': sum(1/r for r in known) / n if n else None,
            'median_rank': median if median is not None and math.isfinite(median) else None,
            'median_unbounded': median is not None and not math.isfinite(median),
            'scorable_n': len(known),
            'mauc': statistics.mean(r['mauc'] if r['mauc'] is not None else 0 for r in rows)
                    if n and all('mauc' in r for r in rows) else None,
            'matched_targets_n': sum(r.get('matched_n', 0) > 0 for r in rows),
            'note': 'empty; no estimate' if not n else ('low-n diagnostic (<30)' if n < 30 else '')}


def summarize(rows):
    out = {'all': metrics(rows), 'test': metrics([r for r in rows if r['split'] == 'test']),
           'hard': metrics([r for r in rows if r['hard']]),
           'hard_test': metrics([r for r in rows if r['hard'] and r['split'] == 'test']),
           'nearest_unknown_n': sum(r['nearest_cosine'] is None for r in rows), 'slices': {}}
    out['excluded_cosine_n'] = sum(r['nearest_cosine'] is not None and r['nearest_cosine'] >= .8 for r in rows)
    out['excluded_franchise_n'] = sum(r.get('same_franchise', False) for r in rows)
    out['excluded_either_n'] = sum(not r['hard'] for r in rows)
    for field in ('signal', 'kind', 'anime', 'who'):
        out['slices'][field] = {str(value): metrics([r for r in rows if r[field] == value])
                               for value in sorted({r[field] for r in rows})}
    return out


def build_contexts(server, profile, selected, seed):
    contexts, by_transform = {}, {}
    for variant in selected:
        transform = variant.transform
        if transform not in by_transform:
            p = transform(copy.deepcopy(profile)) if transform else profile
            saved_hits = server._learn['hits']
            keys = {e['key'] for e in p['entries']}
            server._learn['hits'] = [h for h in saved_hits if h['key'] in keys]
            try:
                with redirect_stdout(io.StringIO()):
                    model = server.build_model(p)
            finally:
                server._learn['hits'] = saved_hits
            by_transform[transform] = variants.Context(server, model, p, seed)
        contexts[variant.name] = by_transform[transform]
    return contexts


def ranking(score, candidates, person=None):
    values = []
    for key in candidates:
        value = score(key, person)
        if value is not None:
            if not math.isfinite(value):
                raise ValueError(f'non-finite score: {key}')
            values.append((-value, key))
    return [key for _, key in sorted(values)]


def rank_targets(server, train, targets, universe, selected, seed, person=None, matching=None):
    contexts = build_contexts(server, train, selected, seed)
    candidates = sorted(set(universe) - train['seen'])
    nearest = {id(ctx): {e['key']: nearest_remaining(server, e['key'], ctx.profile) for e in targets}
               for ctx in {id(ctx): ctx for ctx in contexts.values()}.values()}
    result = {}
    matching = matching or Matched(server, universe)
    for variant in selected:
        context = contexts[variant.name]
        score = variant.factory(context)
        scores = {key: score(key, person) for key in candidates}
        order = ranking(lambda key, person=None: scores[key], candidates)
        ranks = {key: i + 1 for i, key in enumerate(order)}
        rows = []
        for e in targets:
            near = nearest[id(context)][e['key']]
            franchise = same_franchise(server, e['key'], context.profile)
            mauc, matched_n = matching.auc(e['key'], scores, candidates)
            rows.append({'key': e['key'], 'title': e['title'], 'signal': e['signal'],
                         'kind': 'movie' if e['kind'] == 'movie' else 'series',
                         'anime': 'anime' in e.get('flags', ()), 'who': e['who'],
                         'split': split_for(e['key']), 'rank': ranks.get(e['key']),
                         'candidates': len(candidates), 'nearest_cosine': near,
                         'same_franchise': franchise,
                         'mauc': mauc, 'matched_n': matched_n,
                         'hard': not franchise and (near is None or near < .8)})
        result[variant.name] = rows
    return result


def loo(server, profile, targets, universe, selected, seed, person=None):
    result = {v.name: [] for v in selected}
    matching = Matched(server, universe)
    for i, entry in enumerate(targets):
        train = leave_out(profile, [entry['key']])
        rows = rank_targets(server, train, [entry], universe, selected, seed, person, matching)
        for name in result:
            result[name].extend(rows[name])
        if (i + 1) % 50 == 0:
            print(f'  LOO {i+1}/{len(targets)}', flush=True)
    return result


def pick_grid(rows, selected):
    grid_names = {v.name for v in variants.GRID}
    candidates = [v.name for v in selected if v.name in grid_names]
    if not candidates or not any(rows.get(name) for name in candidates):
        return None
    return min(candidates, key=lambda name: (-(metrics(rows[name])['mauc'] or 0), name))


def finish_protocol(raw, selected, use_grid, tune_raw=None, temporal=False):
    output = {}
    for variant in selected:
        rows = raw[variant.name]
        evaluated = rows if temporal or not variant.tunable else [r for r in rows if r['split'] == 'test']
        output[variant.name] = {**summarize(evaluated), 'rows': evaluated,
                                'evaluation_set': 'temporal holdout' if temporal else
                                ('hash test half' if variant.tunable else 'all LOO targets')}
    tuning = tune_raw if tune_raw is not None else {
        name: [r for r in rows if r['split'] == 'tune'] for name, rows in raw.items()}
    winner = pick_grid(tuning, selected) if use_grid else None
    if winner:
        output['grid_selected'] = copy.deepcopy(output[winner])
        output['grid_selected']['selected_variant'] = winner
    return {'variants': output, 'tuning': {'selected_variant': winner, 'objective': 'E1 matched-negative AUC on tune; then variant name',
            'tune_n': len(next(iter(tuning.values()), [])),
            'note': 'Frozen E1 objective. R@k never selects. W2 reused test once.'}}


def temporal_profile(server, snapshot, profile):
    real = [it for it in read_json(snapshot / 'trakt.json')['items']
            if it.get('at') and not it.get('backfilled')]
    unique = {}
    for it in sorted(real, key=lambda it: (it['at'], it['id'])):
        unique[it['id']] = it
    ordered = sorted(unique.values(), key=lambda it: (it['at'], it['id']))
    cut = int(len(ordered) * .8)
    older, newer = ordered[:cut], ordered[cut:]
    now = profile.get('built_at')
    if not isinstance(now, (int, float)):
        raise ValueError('profile.built_at must be a snapshot epoch timestamp')
    entries = [server.trakt_entry(it, now) for it in older]
    train = profile_with({}, entries)
    train['seen'] = {e['key'] for e in entries}
    targets = positives({'entries': [server.trakt_entry(it, now) for it in newer]})
    return train, targets, {'real_titles_n': len(ordered), 'train_n': len(older),
                            'newest_n': len(newer), 'positive_test_n': len(targets),
                            'cutoff_at': older[-1]['at'] if older else None,
                            'note': 'Low-n diagnostic. Only older real Trakt observations train; '
                            'no backfill, shared-TV data, told seeds or learned hits. Newest nonpositive '
                            'observations are not relevance targets. Snapshot weights/metadata are frozen.'}


def resolve_probe(item, server, universe):
    key = item['key']
    if key not in universe:
        return []
    if server._facets.get(key, {}).get('kind') != item['kind']:
        raise ValueError(f'P4 probe kind mismatch: {key}')
    return [key]


def probe_data(name, path=None):
    if path is not None:
        return read_json(path)
    return read_json(ROOT / 'eval' / 'probes.example.json')['seed_probes' if name == 'probes_seed.json' else 'probes']


def probes(server, profile, universe, selected, seed, path=None):
    out = []
    for probe in probe_data('probes.json', path):
        hidden = sorted({key for item in probe['hide'] for key in resolve_probe(item, server, universe)})
        expected = {item['title']: resolve_probe(item, server, universe) for item in probe['expect']}
        # Probes ask about known analogues too: remove expectations from training and seen.
        expected_keys = {key for keys in expected.values() for key in keys}
        train = leave_out(profile, set(hidden) | expected_keys)
        contexts = build_contexts(server, train, selected, seed)
        candidates = sorted(set(universe) - train['seen'])
        ranks = {}
        for variant in selected:
            order = ranking(variant.factory(contexts[variant.name]), candidates)
            lookup = {key: i+1 for i, key in enumerate(order)}
            ranks[variant.name] = {title: [{'key': key, 'rank': lookup.get(key)} for key in keys]
                                   for title, keys in expected.items()}
        out.append({**probe, 'n': len(expected_keys), 'hidden_keys': hidden, 'ranks': ranks,
                    'note': 'Qualitative low-n probe; expectations also withheld and unmasked. '
                    'Resolved by frozen key+kind, never title.'})
    return out


def attribution(server, profile, universe, seed):
    positive = [e for e in profile['entries'] if e['weight'] > 0]
    total = sum(e['weight'] for e in positive)
    who, membership, split_credit = {}, {}, {}
    for e in positive:
        who[e['who']] = who.get(e['who'], 0.0) + e['weight']
        by = sorted(set(e.get('by', ())))
        for person in by:
            membership[person] = membership.get(person, 0.0) + e['weight']
            split_credit[person] = split_credit.get(person, 0.0) + e['weight'] / len(by)
    groups = {'owner': [e for e in profile['entries'] if e['who'] in ('owner', 'both')],
              'tv': [e for e in profile['entries'] if e['who'] in ('tv', 'both')],
              **{p: server.person_entries(profile, p) for p in server.PEOPLE}}
    context = build_contexts(server, profile, [variants.REGISTRY['current']], seed)['current']
    candidates = sorted(set(universe) - profile['seen'])
    household = set(ranking(variants.current(context), candidates)[:30])
    cosines, counts, overlap = {}, {}, {}
    for name, entries in groups.items():
        with redirect_stdout(io.StringIO()):
            vec = context.model['pvec'].get(name) if name in server.PEOPLE else server.taste_vector({'entries': entries})
        cosines[name] = cosine(context.model['vec'], vec)
        counts[name] = len(entries)
        if name in server.PEOPLE:
            other = set(ranking(variants.current(context), candidates, name)[:30])
            overlap[name] = {'intersection_n': len(household & other),
                             'share_of_household_top30': len(household & other) / len(household) if household else None}
    her = context.model['paff'][server.PEOPLE[0]]
    negative = sum((server.score_key(context.model, key, server.PEOPLE[0])[0] or 0) < 0 for key in candidates)
    return {'n': len(positive), 'positive_weight': total,
            'ana_negative_entries': sum(e['weight'] < 0 for e in groups[server.PEOPLE[0]]),
            'ana_negative_x': sum(v < 0 for fid, v in her.items() if fid.startswith('x:')),
            'ana_negative_scores': negative, 'candidates_n': len(candidates),
            'ana_negative_share': negative / len(candidates) if candidates else None,
            'weight_share_who': {k: v/total for k, v in sorted(who.items())},
            'weight_share_by_membership': {k: v/total for k, v in sorted(membership.items())},
            'weight_share_by_split_credit': {k: v/total for k, v in sorted(split_credit.items())},
            'cosine_to_household': cosines, 'subprofile_n': counts, 'top30_overlap': overlap,
            'note': 'by membership overlaps; split credit divides each title equally among unique by values. '
            'Ana/Ben include together via live person_entries; owner/TV include both. '
            'Duplicates in by are counted once; source labels remain as snapshotted. '
            'Raw Trakt/history inputs are excluded; backfill attribution is unavailable.'}


def fmt(value):
    return '—' if value is None else f'{value:.4f}' if isinstance(value, float) else str(value)


def metric_table(items):
    lines = ['| Variant / slice | n | mAUC | R@10 | R@30 | NDCG@30 | R@100 | MRR | Median rank | Note |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for label, m in items:
        median = '∞' if m['median_unbounded'] else fmt(m['median_rank'])
        lines.append(f"| {label} | {m['n']} | {fmt(m['mauc'])} | {fmt(m['recall@10'])} | {fmt(m['recall@30'])} | "
                     f"{fmt(m['ndcg@30'])} | {fmt(m['recall@100'])} | {fmt(m['mrr'])} | {median} | {m['note']} |")
    return lines


def report(data, elapsed):
    meta = data['metadata']
    lines = ['# Skald offline replay', '',
             f"Snapshot: {meta['snapshot']}; universe n={meta['universe_n']}; seed={meta['seed']}.",
             f'Runtime: {elapsed:.1f}s. No network; trending calibration skipped; no match percentages inferred.',
             'Ranking ties use ascending key. Unscorable targets count as misses. All numbers are fractions.',
             'The universe is previously collected titles, not uniform distractors; watched ≠ loved. '
             'Static embeddings, facets and snapshot recency weights can contain future information.', '']
    for name, p in data['protocols'].items():
        if name in ('p4', 'p5'):
            continue
        lines.extend([f'## {name.upper()}', '', p['note'], ''])
        if 'split' in p:
            lines.extend([f"Temporal split: {json.dumps(p['split'], sort_keys=True)}", ''])
        for subname, block in p['groups'].items():
            lines.extend([f'### {subname}', '',
                          f"Tuning: {json.dumps(block['tuning'], sort_keys=True)}", ''])
            lines.extend(metric_table([(f"{v} ({d['evaluation_set']})", d['all'])
                                       for v, d in block['variants'].items()]))
            lines.append('')
            if name == 'p1':
                lines.extend(['#### Hard slice (no remaining franchise, nearest cosine < 0.8)', ''])
                lines.extend(metric_table([(v, d['hard']) for v, d in block['variants'].items()]))
                counts = {v: {k: d[k] for k in ('excluded_cosine_n', 'excluded_franchise_n', 'excluded_either_n')}
                          for v, d in block['variants'].items()}
                lines.extend(['', f'Targets excluded (overlapping counts): {counts}.', '',
                              'Unknown nearest cosine stays in hard; count in metrics.json. '
                              'This removes the easy target, not its saga neighbours from training.', '',
                              '#### Common hash-test comparison (including fixed baselines)', ''])
                lines.extend(metric_table([(v, d['test']) for v, d in block['variants'].items()]))
                lines.append('')
                for v, d in block['variants'].items():
                    lines.extend([f'#### {v} slices', ''])
                    lines.extend(metric_table([(f'{field}={value}', m)
                        for field, slices in d['slices'].items() for value, m in slices.items()]))
                    lines.append('')
    if 'p4' in data['protocols']:
        lines.extend(['## P4 named probes', '',
                      'Low-n qualitative diagnostics, not optimisation targets. Expected titles are also '
                      'withheld, including ones already seen. Empty lookup = not in universe. '
                      'Only fixed baselines are shown: probe expectations can overlap weight-tuning targets.', ''])
        for probe in data['protocols']['p4']:
            lines.extend([f"### {probe['name']} (n={probe['n']})", '',
                          f"Hidden: {', '.join(probe['hidden_keys']) or 'NOT FOUND'}", '',
                          '| Variant | Expected title | Rank (key) |', '|---|---|---|'])
            for v, expected in probe['ranks'].items():
                for title, matches in expected.items():
                    ranks = ', '.join(f"{fmt(r['rank'])} ({r['key']})" for r in matches) or 'not in universe'
                    lines.append(f'| {v} | {title} | {ranks} |')
            lines.append('')
    if 'p5' in data['protocols']:
        lines.extend(['## P5 source attribution', '', chr(96)*3 + 'json',
                      json.dumps(data['protocols']['p5'], indent=2, sort_keys=True), chr(96)*3, ''])
    if 'paired' in data:
        lines.extend(['## E1 paired bootstrap vs merged baseline', '',
                      json.dumps(data['paired'], sort_keys=True), ''])
    if 'anecdote' in data:
        lines.extend(['## Together anecdote (n<10)', '', '| Target | Rank |', '|---|---:|'])
        lines.extend(f"| {r['title']} | {fmt(r['rank'])} |" for r in data['anecdote']['rows'])
        lines.extend(['', f"Median rank: {fmt(data['anecdote']['median_rank'])}.", ''])
    lines.extend(['## Profile fixes (frozen snapshot labels are retained in replay)', ''])
    lines.extend(f"- {b['path']}: {b['finding']}" for b in data['bugs'])
    return '\n'.join(lines) + '\n'


def run(server, snapshot, selected, use_grid, protocols, seed, probes_path=None):
    profile = read_json(snapshot / 'profile.json')
    if (snapshot / 'feedback.jsonl').is_file():
        server.merge_feedback(profile['entries'], {e['key']: e for e in profile['entries']}, profile['built_at'])
        profile['seen'] = set(profile['seen']) | {e['key'] for e in profile['entries']}
    profile = profile_with(profile, profile['entries'])
    universe = sorted(set(server._emb) | set(server._facets))
    data = {'metadata': {'snapshot': str(snapshot), 'seed': seed, 'universe_n': len(universe),
                        'profile_n': len(profile['entries']), 'positive_targets_n': len(positives(profile)),
                        'server_sha256': hashlib.sha256((ROOT/'server.py').read_bytes()).hexdigest(),
                        'snapshot_sha256': {name: hashlib.sha256((snapshot/name).read_bytes()).hexdigest()
                                            for name in FILES}},
            'protocols': {}, 'bugs': BUGS}
    selected_for_probes = [v for v in selected if not v.tunable]
    household_tune = None
    if 'p1' in protocols:
        print('P1 household LOO', flush=True)
        raw = loo(server, profile, positives(profile), universe, selected, seed)
        household_tune = {name: [r for r in rows if r['split'] == 'tune'] for name, rows in raw.items()}
        block = finish_protocol(raw, selected, use_grid)
        data['protocols']['p1'] = {'note': 'All loved/watched positive targets. Full LOO; no sampling. '
            'Free-weight blends use stable SHA-256 tune/test halves. Fixed baselines also have test-half '
            'metrics for a fair comparison.', 'groups': {'household': block}}
    if 'p2' in protocols:
        print('P2 person/shared diagnostics', flush=True)
        if use_grid and household_tune is None:
            tune_targets = [e for e in positives(profile) if split_for(e['key']) == 'tune']
            household_tune = loo(server, profile, tune_targets, universe,
                                  [v for v in selected if v.tunable], seed)
        groups = {}
        for name in (server.PEOPLE[0], 'together'):
            targets = [e for e in positives(profile) if (e['who'] in ('tv', 'both') if name == 'together'
                                                        else name in e.get('by', ()))]
            raw = loo(server, profile, targets, universe, selected, seed, name)
            groups[name] = finish_protocol(raw, selected, use_grid, tune_raw=household_tune or {})
            groups[name]['tuning']['source'] = 'household hash-tune targets; P2 never selects weights'
            if name == 'together' and 'current' in raw:
                data['anecdote'] = {**metrics(raw['current']), 'rows': raw['current']}
        data['protocols']['p2'] = {'note': 'anecdote (n<10). Ana targets her explicit '
            'by/told positives, trained with person_entries (her titles + together) and scored with paff/pvec. '
            'Together targets who=tv/both, trains on both histories '
            'and uses score_key(person="together"). Household seen exclusions remain in all pools. '
            'Grid weights come from household tuning; P2 reports blends on hash-test targets only.', 'groups': groups}
    if 'p3' in protocols:
        print('P3 temporal', flush=True)
        train, targets, split = temporal_profile(server, snapshot, profile)
        saved_hits = server._learn['hits']
        server._learn['hits'] = []
        try:
            tune_targets = [e for e in positives(train) if split_for(e['key']) == 'tune']
            tunable = [v for v in selected if v.tunable]
            tune_raw = loo(server, train, tune_targets, universe, tunable, seed) if use_grid else {}
            raw = rank_targets(server, train, targets, universe, selected, seed)
        finally:
            server._learn['hits'] = saved_hits
        block = finish_protocol(raw, selected, use_grid, tune_raw=tune_raw, temporal=True)
        data['protocols']['p3'] = {'note': split['note'], 'split': split, 'groups': {'temporal': block}}
    if 'p4' in protocols:
        print('P4 named probes', flush=True)
        data['protocols']['p4'] = probes(server, profile, universe, selected_for_probes, seed, probes_path)
    if 'p5' in protocols:
        print('P5 attribution', flush=True)
        data['protocols']['p5'] = attribution(server, profile, universe, seed)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    parser.add_argument('--snapshot', default=snapshot if snapshot and snapshot.strip() else DEFAULT_SNAPSHOT, required=not snapshot)
    parser.add_argument('--variants', default='current,emb_only,facets_only,x_only,x_facets,random,grid')
    parser.add_argument('--protocols', default='p1,p2,p4,p5', help='comma-separated p1..p5; p3 needs raw input')
    parser.add_argument('--out', default=str(ROOT / 'eval' / 'out'), help='root inside eval/out; stamp is appended')
    parser.add_argument('--probes', type=Path, help='probe JSON list; defaults to eval/probes.example.json')
    parser.add_argument('--baseline', type=Path, help='merged baseline metrics.json for paired E1 bootstrap')
    args = parser.parse_args()
    try:
        selected, use_grid = variants.select(args.variants)
        protocols = {p.strip().lower() for p in args.protocols.split(',')}
        if not protocols or protocols - {'p1', 'p2', 'p3', 'p4', 'p5'}:
            raise ValueError('protocols must be a comma-separated subset of p1,p2,p3,p4,p5')
        output = workspace_output(args.out)
        snapshot = Path(args.snapshot).resolve()
        started = time.monotonic()
        with offline_server(snapshot, output) as server:
            selected += [variants.Variant('richness', lambda ctx: lambda key, person=None:
                                         richness(ctx.server._facets.get(key, {})))]
            data = run(server, snapshot, selected, use_grid, protocols, SEED, args.probes)
        if args.baseline:
            baseline = read_json(args.baseline)
            if data['metadata']['snapshot_sha256'] != baseline['metadata']['snapshot_sha256']:
                raise ValueError('paired baseline must use the same snapshot inputs')
            rows = lambda d: d['protocols']['p1']['groups']['household']['variants']['current']['rows']
            before, after = rows(baseline), rows(data)
            data['paired'] = {label: paired_bootstrap([r for r in before if label == 'all' or r['split'] == 'test'],
                                                     [r for r in after if label == 'all' or r['split'] == 'test'])
                              for label in ('all', 'test')}
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        destination = output / stamp
        destination.mkdir()
        (destination/'metrics.json').write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n',
                                              encoding='utf-8')
        elapsed = time.monotonic() - started
        (destination/'report.md').write_text(report(data, elapsed), encoding='utf-8')
        if 'p1' in data['protocols']:
            block = data['protocols']['p1']['groups']['household']['variants']
            for name in ('current', 'richness'):
                for label in ('all', 'test'):
                    m = block[name][label]
                    print(f"E1 {name} {label}: n={m['n']} mAUC={fmt(m['mauc'])} "
                          f"R@100={round((m['recall@100'] or 0)*m['n'])}", flush=True)
        if 'paired' in data:
            print('E1 paired: ' + json.dumps(data['paired'], sort_keys=True), flush=True)
        print(f'Report: {destination}/report.md ({elapsed:.1f}s)', flush=True)
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
