#!/usr/bin/env python3
"""Offline precedence gates. Output contains only synthetic case names, counts and hashes."""
import argparse
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval import profile_golden as golden
from eval.profile_golden import DAY, FIXTURE, NOW, dump, offline_server, snapshot_stubs
from eval.rows_report import SNAPSHOT
from skald.members import PEOPLE

SOURCE = ROOT / 'server.py'


def event(s, action, age=10, src='import:letterboxd', who='ana', eid='a', **item):
    people = sorted(s.PEOPLE) if who == 'both' else ['together'] if who == 'together' else [who]
    ctx = {'src': src.split(':')[-1]}
    if src == 'tv':
        it = {'id': 'tmdb:1', 'type': 'movie', 'at': (NOW-age*DAY)*1000, **item}
        signal, _ = s.classify(it, NOW)
        action = s.EVENT_ACTION[signal]
        ctx = {'item': it, 'signal': signal, 'genres': [], 'flags': []}
    elif src == 'trakt':
        signal = {'love': 'loved', 'abandon': 'abandoned', 'seen': 'watched'}[action]
        it = {'id': 'tmdb:1', 'type': 'movie', 'at': (NOW-age*DAY)*1000,
              'signal': signal, 'weight': {'love': 3., 'abandon': -1.5, 'seen': 1.}[action]}
        ctx = {'item': it, 'signal': signal, 'genres': [], 'flags': []}
    elif src == 'told':
        ctx = {'item': {'id': 1, 'genre_ids': []}, 'saga': set()}
    return s._event(NOW-age*DAY if src != 'told' else None, eid, 'tmdb:1', 'movie', 'Synthetic', '2020',
                    who, people, action, src, ctx, w0=ctx.get('item', {}).get('weight'))


def cases(s):
    imp = lambda action, age=10, **kw: event(s, action, age, **kw)
    inferred = lambda **kw: {**imp('abandon', src='import:netflix', **kw), 'conf': .4}
    old = lambda action, age=400, **kw: imp(action, age, src='import:imdb', eid='b', **kw)
    tv = lambda **kw: event(s, 'seen', 100, src='tv', who='together', **kw)
    remote = lambda action, who='ana', age=10: event(s, action, age, src='remote', who=who, eid=who)
    trakt = lambda action, age=400: event(s, action, age, src='trakt', who='ben')
    # Expected person signal; None means the person has no slot. All inputs are synthetic.
    rows = [
        ('I1', [old('not_for_us'), imp('love')], 'loved', None),
        ('I2', [old('love'), imp('not_for_us')], 'disliked', None),
        ('I3', [old('love'), imp('like')], 'liked', None),
        ('A1', [old('love'), inferred()], 'loved', None),
        ('A2', [old('abandon'), imp('love')], 'loved', None),
        ('A3', [old('like'), inferred()], 'liked', None),
        ('A4', [old('seen'), inferred()], 'watched', None),
        ('I5', [old('seen', who='both'), inferred()], 'watched', 'watched'),
        ('T1', [old('love', 10), imp('not_for_us')], 'disliked', None),
        ('T2', [imp('love', src='import:netflix'), old('like', 10)], 'liked', None),
        ('T3', [old('abandon', 10), imp('love')], 'loved', None),
        ('S1', [tv(played=True), imp('not_for_us')], 'disliked', 'watched'),
        ('S2', [tv(played=True), imp('love')], 'loved', 'watched'),
        ('S3', [tv(), imp('love')], 'loved', 'opened'),
        ('S4', [tv(progress=.1), imp('love')], 'loved', 'abandoned'),
        ('S5', [tv(favorite=True), imp('not_for_us')], 'disliked', 'loved'),
        ('S6', [tv(played=True), imp('seen')], 'watched', 'watched'),
        ('S7', [tv(progress=.1), imp('seen')], 'watched', 'abandoned'),
        ('S8', [tv(progress=.1), trakt('seen')], 'abandoned', 'watched'),
        ('S9', [tv(progress=.1), imp('like')], 'liked', 'abandoned'),
        ('S10', [tv(played=True), inferred()], 'watched', 'watched'),
        ('K1', [trakt('abandon'), imp('love', who='ben')], None, 'loved'),
        ('K2', [trakt('seen'), inferred(who='ben')], None, 'watched'),
        ('K3', [trakt('love'), imp('not_for_us', who='ben')], None, 'disliked'),
        ('K4', [trakt('love', 10), imp('not_for_us', 2000, who='ben')], None, 'disliked'),
        ('D1', [event(s, 'love', 400, src='told'), imp('not_for_us')], 'loved', None),
        ('R1', [remote('love', 'ben'), remote('not_for_us')], 'disliked', 'loved'),
        ('R2', [imp('love'), remote('not_for_us', 'ben')], 'loved', 'disliked'),
        ('R3', [remote('not_for_us', age=200), imp('love')], 'loved', None),
        ('R4', [trakt('love'), remote('not_for_us')], 'disliked', 'loved'),
        ('R5', [tv(played=True), remote('not_for_us')], 'disliked', 'watched'),
        # Undo cases represent the active adapters after the newest record is removed.
        ('U1', [old('love')], 'loved', None),
        ('U2', [old('love')], 'loved', None),
        ('U3', [tv(played=True)], 'watched', 'watched'),
    ]
    return rows


def fold_case(s, events, reverse=False):
    order = ('tv', 'trakt', 'told', 'import', 'remote')
    grouped = {stage: [] for stage in order}
    for ev in reversed(events) if reverse else events:
        grouped['import' if ':' in ev['src'] else 'remote' if ev['src'] == 'interview' else ev['src']].append(copy.deepcopy(ev))
    entries, _, _ = s.fold_events(((stage, iter(grouped[stage])) for stage in order), NOW)
    return entries


def views(s, entries):
    return {p: s.person_entries({'entries': entries}, p) for p in s.PEOPLE}


def signals(s, entries):
    return {p: [e['signal'] for e in es] for p, es in views(s, entries).items()}


def table(before=None):
    results, failed = [], 0
    with offline_server(SOURCE) as s:
        for name, evts, ana, ben in cases(s):
            expected = {'ana': [] if ana is None else [ana], 'ben': [] if ben is None else [ben]}
            actual = [signals(s, fold_case(s, evts, rev)) for rev in (False, True)]
            failed += sum(a != expected for a in actual)
            results.append({'case': name, 'W17': actual, 'expected': expected})
    if before:
        with offline_server(before) as s:
            for result, (_, evts, _, _) in zip(results, cases(s)):
                result['today'] = [signals(s, fold_case(s, evts, rev)) for rev in (False, True)]
    else:
        with tempfile.TemporaryDirectory(prefix='skald-precedence-base-') as temp:
            source = Path(temp) / 'server.py'
            source.write_bytes(subprocess.run(['git', 'show', 'bd0b6c4:server.py'], cwd=ROOT, check=True, capture_output=True).stdout)
            with offline_server(source) as s:
                for result, (_, evts, _, _) in zip(results, cases(s)):
                    result['today'] = [signals(s, fold_case(s, evts, rev)) for rev in (False, True)]
    for result in results:
        print(dump(result))
    changed = sum(r['today'] != r['W17'] for r in results)
    print(dump({'rows': len(results), 'changed': changed, 'mismatches': failed}))
    return not failed


def people(e):
    by = set(e['by'])
    return (by - {'together'}) | set(PEOPLE) if 'together' in by else by


def scope(s):
    stages = [(name, list(evts)) for name, evts in s.event_stages(NOW, s.read_history(), s.read_trakt())]
    base, _, _ = s.fold_events(((name, iter(evts)) for name, evts in stages[:3]), NOW)
    # Scope counts active nonzero records, including interests outside the serving cap/age.
    imported = [{'key': ev['key'], 'by': ev['by'], 'signal': s.IMPORT_SIGNALS[ev['action']][0]}
                for ev in dict(stages)['import'] if ev['conf'] and s.IMPORT_SIGNALS[ev['action']][1]]
    reactions = dict(stages)['remote']
    remote = [e for ev in reactions for e in s._feedback_event_entries(NOW, iter([ev]))]
    slots, existing, remote_slots = {}, {}, {}
    for e in base:
        for p in people(e):
            value = (e.get('opinions') or {}).get(p) or e.get('shared') or e
            existing.setdefault((e['key'], p), []).append(value['signal'])
    for e in imported + remote:
        for p in people(e):
            slots.setdefault((e['key'], p), []).append(e['signal'])
    for e in remote:
        for p in people(e): remote_slots.setdefault(e['key'], set()).add(p)
    scoped = {key for (key, p), claims in slots.items()
              if len(claims) >= 2 or any(claim != signal for claim in claims for signal in existing.get((key, p), []))}
    for key, reactors in remote_slots.items():
        holders = {p for k, p in existing if k == key} | {p for e in imported if e['key'] == key for p in people(e)}
        actions = {ev['action'] for ev in reactions if ev['key'] == key}
        if holders - reactors or len(actions) > 1: scoped.add(key)
    return scoped


def replay(source, snapshot=None, n=300, excluded=(), render=False, shuffle=None):
    stubs, seed = snapshot_stubs(snapshot) if snapshot else (None, None)
    with offline_server(source, snapshot or FIXTURE / 'data', stubs, n if snapshot else None, seed) as s, redirect_stdout(io.StringIO()):
        if excluded or shuffle is not None:
            raw = s.read_imports()
            if shuffle is not None:
                accepted = {e['key'] for e in s._import_event_entries(NOW, s.import_events(NOW, raw)) if e['signal'] == 'interest'}
                raw = [e for e in raw if e['action'] != 'interest' or e['key'] in accepted]
                groups = {}
                for e in raw: groups.setdefault((e['key'], e['who'], e['src']), []).append(e)
                rng, positions, shuffled = random.Random(shuffle), {k: 0 for k in groups}, []
                keys = list(groups)
                while keys:
                    key = rng.choice(keys)
                    shuffled.append(groups[key][positions[key]])
                    positions[key] += 1
                    if positions[key] == len(groups[key]): keys.remove(key)
                raw = shuffled
            raw = [e for e in raw if e['key'] not in excluded]
            Path(s.DATA, 'imports.jsonl').write_text(''.join(dump(e)+'\n' for e in raw))
            remote = [e for e in s.read_feedback() if e['key'] not in excluded]
            Path(s.DATA, 'feedback.jsonl').write_text(''.join(dump(e)+'\n' for e in remote))
            s._imports_cache.clear(); s._remote_cache.clear()
        keys = scope(s) if not excluded else set()
        universe = {ev['key'] for stage, evts in s.event_stages(NOW, s.read_history(), s.read_trakt())
                    for ev in evts if stage != 'import' or ev['conf'] and s.IMPORT_SIGNALS[ev['action']][1]}
        items = s.read_history()
        p = s.build_profile(items, [] if snapshot else ['q1', 'q2'], s.read_trakt())
        out = {'household': dump(p)}
        pv = views(s, p['entries'])
        if render:
            s._profiles['household'] = p
            s.tmdb = golden.denied
            m = s.build_model(p); s._model['m'] = m
            s.get_model = lambda build=True: m
            out['model'] = dump({**{k: v for k, v in m.items() if not callable(v) and k not in golden.EXCLUDE},
                                 '_seed_stats': s._seed_stats}, True)
            out['rows'] = dump(golden.display_rows(s), True)
        out['hashes'] = dump({'taste': s.taste_hash('household', items), 'import': s.import_hash(NOW), 'feedback': s.feedback_hash(NOW)}, True)
        out['scope_counts'] = dump({'keys': len(universe), 'scope': len(keys), 'out_of_scope': len(universe-keys)})
        return out, pv, keys


def entry_groups(entries, excluded=()):
    grouped = {}
    for e in entries:
        if e['key'] not in excluded: grouped.setdefault(e['key'], []).append(e)
    return {k: dump(v) for k, v in grouped.items()}


def scoped_diff(before, snapshot, n):
    a, av, scoped = replay(before, snapshot, n)
    b, bv, _ = replay(SOURCE, snapshot, n)
    ap, bp = json.loads(a['household']), json.loads(b['household'])
    ag, bg = entry_groups(ap['entries']), entry_groups(bp['entries'])
    changed = {k for k in ag.keys() | bg.keys() if ag.get(k) != bg.get(k)}
    outer = changed - scoped
    order = [e['key'] for e in ap['entries'] if e['key'] not in scoped] == [e['key'] for e in bp['entries'] if e['key'] not in scoped]
    pv = {p: dump([e for e in av[p] if e['key'] not in scoped]) == dump([e for e in bv[p] if e['key'] not in scoped]) for p in av}
    genres = max((abs(ap['genres'].get(g, 0)-bp['genres'].get(g, 0)) for g in ap['genres'].keys() | bp['genres'].keys()), default=0)
    result = {**json.loads(a['scope_counts']), 'entries': len(ap['entries']), 'served_unique_keys': len(ag), 'changed': len(changed), 'out_of_scope_changed': len(outer),
              'person_views_identical': pv, 'relative_order_identical': order,
              'negative_added': len(set(bp['negative'])-set(ap['negative'])),
              'negative_removed': len(set(ap['negative'])-set(bp['negative'])), 'max_genres_delta': genres}
    if snapshot is None: result['changed_fixture_keys'] = sorted(changed)
    print(dump(result))
    return not outer and order and all(pv.values()) and len(changed) <= len(scoped)


def no_conflict(before, snapshot, n):
    _, _, scoped = replay(before, snapshot, n)
    a, _, _ = replay(before, snapshot, n, scoped, True)
    b, _, _ = replay(SOURCE, snapshot, n, scoped, True)
    a.pop('scope_counts'); b.pop('scope_counts')
    with redirect_stdout(io.StringIO()): identical = golden.compare(a, b)
    print(dump({'scope_removed': len(scoped), 'identical': identical,
                'parts_identical': {k: a[k] == b[k] for k in a},
                'before_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in a.items()},
                'after_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in b.items()}}))
    return identical


def shuffle_gate(n, snapshot=None):
    datasets = [None] + ([snapshot] if snapshot else [])
    failures = 0
    for data in datasets:
        first, pv, _ = replay(SOURCE, data, shuffle=0)
        canonical = lambda p: dump(sorted(p, key=lambda e: e['key']))
        household = canonical(json.loads(first['household'])['entries'])
        personal = {p: canonical(es) for p, es in pv.items()}
        for seed in range(1, n+1):
            parts, views_, _ = replay(SOURCE, data, shuffle=seed)
            failures += household != canonical(json.loads(parts['household'])['entries']) or any(personal[p] != canonical(es) for p, es in views_.items())
    print(dump({'datasets': len(datasets), 'shuffles': n, 'mismatches': failures}))
    return not failures


def memory(before, n, repeat):
    runs = {'before': [], 'after': []}
    for _ in range(repeat):
        for name, source in (('before', before), ('after', SOURCE)):
            result = golden.memory(source, n)
            runs[name].append(result)
            print(dump({'run': len(runs[name]), 'source': name, 'peak_bytes': result['peak_bytes'], 'wall_ms': result['wall_ms']}), flush=True)
    summary = {name: {'peak_bytes': max(r['peak_bytes'] for r in rs), 'median_wall_ms': statistics.median(r['wall_ms'] for r in rs)} for name, rs in runs.items()}
    ok = summary['after']['peak_bytes'] <= summary['before']['peak_bytes'] + 1024*1024 and summary['after']['median_wall_ms'] <= 1.2*summary['before']['median_wall_ms']
    print(dump({'summary': summary, 'passed': ok}))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--table', action='store_true')
    ap.add_argument('--shuffle', type=int)
    ap.add_argument('--snapshot', type=Path)
    ap.add_argument('--before', type=Path)
    ap.add_argument('--overlay', type=int, default=300)
    ap.add_argument('--no-conflict', action='store_true')
    ap.add_argument('--memory', type=int)
    ap.add_argument('--repeat', type=int, default=5)
    args = ap.parse_args()
    if args.table: ok = table(args.before)
    elif args.shuffle is not None:
        ok = shuffle_gate(args.shuffle, args.snapshot or Path(SNAPSHOT))
    elif args.before is None: ap.error('--before is required')
    elif args.memory is not None: ok = memory(args.before, args.memory, args.repeat)
    elif args.no_conflict: ok = no_conflict(args.before, args.snapshot, args.overlay)
    else: ok = scoped_diff(args.before, args.snapshot, args.overlay)
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
