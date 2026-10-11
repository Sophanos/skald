#!/usr/bin/env python3
"""Exact offline raw-input replay; snapshot output is hashes/counts or stdout mismatches only."""
import argparse
from array import array
from collections import Counter
from contextlib import contextmanager, redirect_stdout
import datetime
import gc
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval.rows_report import FILES, display_rows

NOW = 1791734400.0
DAY = 86400
FIXTURE = ROOT / 'tests/fixtures/events'
# No nondeterministic fields have been found; callables are excluded by the dump contract.
EXCLUDE = []


def canon(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, (array, tuple)):
        return list(value)
    raise TypeError(type(value).__name__)


def dump(value, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=True,
                      default=canon, sort_keys=sort_keys)


def denied(*args, **kwargs):
    raise AssertionError('network forbidden in profile replay')


def snapshot_stubs(snapshot):
    profile = json.loads((Path(snapshot) / 'profile.json').read_text())
    stubs = {'details': {}, 'picks': [], 'genres': {}, 'collections': {}}
    for e in profile['entries']:
        gids = []
        for genre in e['genres']:
            gid = next((k for k, v in stubs['genres'].items() if v == genre), None)
            if gid is None:
                gid = str(len(stubs['genres']) + 1)
                stubs['genres'][gid] = genre
            gids.append(int(gid))
        kind = e['kind']
        det = {'id': int(e['key'][5:]), 'title' if kind == 'movie' else 'name': e['title'],
               'release_date' if kind == 'movie' else 'first_air_date': e['year'] + '-01-01' if e['year'] else '',
               'genres': [{'name': g} for g in e['genres']], 'genre_ids': gids}
        stubs['details'][e['key']] = [kind, det]
        if (e.get('item') or {}).get('id'):
            stubs['details'][e['item']['id']] = [kind, det]
        stubs['picks'].append({'title': e['title'], 'kind': kind, 'pick': det})
    return stubs, profile


def overlay(data, profile, n, feedback=True):
    rng = random.Random(7)
    entries = profile['entries']
    actions = ('love', 'like', 'seen', 'not_for_us', 'interest', 'meh', 'not_tonight')
    sources = ('trakt', 'letterboxd', 'imdb', 'netflix', 'chrome', 'youtube', 'plain')
    from skald.members import load_config
    config = load_config(str(data), {}, readonly=True)[0]
    members = config['household']['members']
    people = (members[1], members[0], 'both')
    imports = []
    for i in range(n):
        e = rng.choice(entries) if i % 2 == 0 and entries else {
            'key': f'tmdb:{8000000+i}', 'kind': 'movie' if i % 3 else 'tv',
            'title': f'Synthetic {i}', 'year': '2020'}
        age = rng.randrange(401)
        imports.append({'t': datetime.datetime.fromtimestamp(NOW-age*DAY, datetime.timezone.utc).date().isoformat(),
                        'id': f'{i+1:032x}', **{k: e[k] for k in ('key', 'kind', 'title', 'year')},
                        'title': f'Synthetic {i}', 'who': people[i % 3], 'action': actions[i % 7], 'src': sources[(i // 7) % 7],
                        'conf': rng.choice((0.0, .57, 1.0)), 'n': rng.randrange(1, 10)})
    (Path(data) / 'imports.jsonl').write_text(''.join(dump(e)+'\n' for e in imports))
    if feedback:
        remote = []
        for i in range(60):
            e = rng.choice(entries) if i % 2 == 0 and entries else {
                'key': f'tmdb:{9000000+i}', 'kind': 'tv' if i % 3 else 'movie',
                'title': f'Synthetic remote {i}', 'year': '2020'}
            action = ('love', 'like', 'meh', 'seen', 'not_for_us', 'not_tonight')[i % 6]
            remote.append({'id': f'{100000+i:032x}', 't': NOW-rng.randrange(401)*DAY,
                           **{k: e[k] for k in ('key', 'kind', 'title', 'year')}, 'who': people[(i // 6) % 3],
                           'title': f'Synthetic remote {i}', 'action': action, 'reasons': ['too slow'] if action == 'not_for_us' else [],
                           'row': rid('foryou', config), 'undo': None})
        (Path(data) / 'feedback.jsonl').write_text(''.join(dump(e)+'\n' for e in remote))


@contextmanager
def offline_server(source, data=FIXTURE / 'data', stubs=None, n=None, profile=None):
    with tempfile.TemporaryDirectory(prefix='skald-profile-') as temp, \
            patch.dict(os.environ, {'TMDB_API': 'offline', 'DATA_DIR': temp, 'OPENROUTER_API_KEY': ''}), \
            patch('urllib.request.urlopen', denied), patch('time.time', return_value=NOW):
        for name in dict.fromkeys((*FILES, '.skald-format', 'legacy.json', 'skald.json', 'history.json', 'trakt.json', 'imports.jsonl', 'feedback.jsonl')):
            path = Path(data) / name
            if path.is_file():
                shutil.copyfile(path, Path(temp) / name)
        if n is not None:
            overlay(temp, profile, n)
        if stubs is None:
            stubs = json.loads((FIXTURE / 'tmdb.json').read_text())
        spec = importlib.util.spec_from_file_location('skald_profile_golden', source)
        s = importlib.util.module_from_spec(spec)
        try:
            with redirect_stdout(io.StringIO()):
                spec.loader.exec_module(s)
            setattr(s, "local_now" if hasattr(s, "local_now") else "berlin_now",
                    lambda ts=None: datetime.datetime(2026, 10, 11, 18))
            def details(it):
                hit = stubs['details'].get(it['id'])
                if hit and hit[1].get('raise'):
                    raise LookupError('synthetic failed resolution')
                return hit
            picks = {(e['title'].casefold(), e['kind']): e['pick'] for e in stubs['picks']}
            s.tmdb_details = details
            s.resolve_pick = lambda kind, title, year, lang: (picks[(title.casefold(), kind)], {}) if (title.casefold(), kind) in picks else None
            s.genres = lambda *args, **kwargs: {int(k): v for k, v in stubs['genres'].items()}
            def tmdb(path, **kwargs):
                if path not in stubs['collections']:
                    return denied(path)
                return stubs['collections'][path]
            s.tmdb = tmdb
            for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
                setattr(s, name, lambda *args, **kwargs: None)
            with redirect_stdout(io.StringIO()):
                s.load_state()
            yield s
        finally:
            for name in ('_pool', '_outer', '_nzb_pool', '_connector_pool'):
                pool = getattr(s, name, None)
                if pool is not None:
                    pool.shutdown(wait=True, cancel_futures=True)


def sections(source, snapshot=None, n=300):
    stubs, seed = snapshot_stubs(snapshot) if snapshot else (None, None)
    with offline_server(source, snapshot or FIXTURE / 'data', stubs, n if snapshot else None, seed) as s, \
            redirect_stdout(io.StringIO()):
        items = s.read_history()
        p = s.build_profile(items, ['q1', 'q2'] if not snapshot else [], s.read_trakt(), remote=True)
        out = {'household': dump(p)}
        if snapshot:
            s._profiles['household'] = p
            s.tmdb = denied
            m = s.build_model(p)
            s._model['m'] = m
            s.get_model = lambda build=True: m
            out['model'] = dump({**{k: v for k, v in m.items() if not callable(v) and k not in EXCLUDE},
                                 '_seed_stats': s._seed_stats}, True)
            out['rows'] = dump(display_rows(s), True)
        else:
            out['plain'] = dump(s.build_profile(items, [], (), remote=False))
        out['hashes'] = dump({'taste': s.taste_hash('household', items),
                              'import': s.import_hash(NOW), 'feedback': s.feedback_hash(NOW)}, True)
        return out


def difference(a, b, path='$'):
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        if list(a) != list(b):
            return path + '.keys', list(a), list(b)
        for k in a:
            diff = difference(a[k], b[k], path + '.' + k)
            if diff:
                return diff
    elif isinstance(a, list):
        if len(a) != len(b):
            return path + '.length', len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            diff = difference(x, y, f'{path}[{i}]')
            if diff:
                return diff
    elif dump(a) != dump(b):
        return path, a, b


def compare(before, after):
    for name in before:
        if before[name] != after[name]:
            diff = difference(json.loads(before[name]), json.loads(after[name]), '$.' + name)
            print('Mismatch:', *(repr(x) for x in (diff or (name, before[name], after[name]))))
            return False
    return True


def summary(parts):
    counts = {'entries_by_who': dict(Counter(e['who'] for e in json.loads(parts['household'])['entries']))}
    if 'model' in parts:
        counts['model_keys'] = len(json.loads(parts['model'])) - 1
        rows = json.loads(parts['rows'])
        counts.update(rows=len(rows), metas=sum(len(r['metas']) for r in rows))
    return {'sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in parts.items()}, 'counts': counts}


def memory(source, n):
    peaks, elapsed = [], []
    # Use the same half-existing/half-fresh overlay as snapshot replay, with synthetic fixture keys.
    seed = json.loads((FIXTURE / 'golden.json').read_text())['household']
    # The base has no import-strength row for the fixture's deliberately unknown Trakt signal.
    seed['entries'] = [e for e in seed['entries'] if e['signal'] != 'mystery']
    with offline_server(source, n=n, profile=seed) as s, redirect_stdout(io.StringIO()):
        s.import_state(); s.feedback_state()
        items, trakt = s.read_history(), s.read_trakt()
        s.build_profile(items, [], trakt)  # warm threads and caches before measuring the hot path.
        for _ in range(3):
            gc.collect()
            tracemalloc.start()
            p = s.build_profile(items, [], trakt)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            del p
        for _ in range(7):
            gc.collect()
            start = time.perf_counter()
            p = s.build_profile(items, [], trakt)
            elapsed.append((time.perf_counter() - start) * 1000)
            if len(elapsed) == 7:
                profile_sha256 = hashlib.sha256(dump(p).encode()).hexdigest()
                entries = len(p['entries'])
            del p
    return {'profile_sha256': profile_sha256, 'entries': entries, 'peak_bytes': max(peaks), 'wall_ms': round(statistics.median(elapsed), 3), 'imports': n,
            'existing_fraction': .5, 'timing': 'without tracemalloc'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fixture', action='store_true')
    mode.add_argument('--snapshot', type=Path)
    mode.add_argument('--memory', type=int)
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--write', action='store_true')
    action.add_argument('--check', action='store_true')
    ap.add_argument('--source', type=Path, default=ROOT / 'server.py')
    ap.add_argument('--before', type=Path)
    ap.add_argument('--after', type=Path, default=ROOT / 'server.py')
    ap.add_argument('--overlay', type=int, default=300)
    args = ap.parse_args()
    if args.fixture:
        if not (args.write or args.check):
            ap.error('--fixture requires --write or --check')
        actual = sections(args.source)
        if not compare(actual, sections(args.source)):
            raise SystemExit(1)
        path = FIXTURE / 'golden.json'
        if args.write:
            path.write_text(dump({k: json.loads(v) for k, v in actual.items()}) + '\n')
        else:
            saved = json.loads(path.read_text())
            if not compare({k: dump(v, k == 'hashes') for k, v in saved.items()}, actual):
                raise SystemExit(1)
        print('Fixture identical:', dump(summary(actual)))
    else:
        if not args.before:
            ap.error('--before is required')
        if args.memory is not None:
            before, after = memory(args.before, args.memory), memory(args.after, args.memory)
            print(dump({'before': before, 'after': after}))
            if before['profile_sha256'] != after['profile_sha256'] or after['peak_bytes'] > before['peak_bytes'] + 1024*1024 or after['wall_ms'] > 1.2 * before['wall_ms']:
                raise SystemExit(1)
        else:
            before = sections(args.before, args.snapshot, args.overlay)
            if not compare(before, sections(args.before, args.snapshot, args.overlay)):
                raise SystemExit(1)
            after = sections(args.after, args.snapshot, args.overlay)
            print(dump({'before': summary(before), 'after': summary(after), 'overlay': args.overlay, 'EXCLUDE': EXCLUDE}))
            if not compare(before, after):
                raise SystemExit(1)
            print('Snapshot identical')


if __name__ == '__main__':
    main()
