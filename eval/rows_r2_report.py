#!/usr/bin/env python3
"""Offline R2 targets and same-process CPU comparison; emit counts, hashes and timings only."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval.profile_golden import NOW, sections
from eval.rows_report import display_rows, home_server, stub_refills


def ids(metas):
    return [x['id'] for x in metas]


def rows_diff(before, after):
    a = {(r['id'], r['type']): r['metas'] for r in before}
    b = {(r['id'], r['type']): r['metas'] for r in after}
    changed = [k for k in a.keys() | b.keys() if a.get(k, []) != b.get(k, [])]
    return {'rows_changed': len(changed), 'metas_changed': sum(
        sum(i >= len(a.get(k, [])) or x != a[k][i] for i, x in enumerate(b.get(k, [])))
        + max(0, len(a.get(k, []))-len(b.get(k, []))) for k in changed)}


def metrics(s, rows, no_session, old_week):
    pool = {x['id']: x for x in s.row_pool('de-DE')}
    source = lambda xs: [pool.get(x['id'], x) for x in xs]
    row = {r['id']: source(r['metas']) for r in rows}
    m, seeds = s.get_model(False), s.explicit_seeds('ana')
    fy = row[rid('foryou', s.CONFIG)][:12]
    mean_knn = lambda xs: round(statistics.mean(s.seed_knn(seeds, x['id'], 1) or 0 for x in xs[:12]), 5) if xs else 0
    curated = {rid('foryou', s.CONFIG), rid('week', s.CONFIG), rid('because', s.CONFIG), rid('leap', s.CONFIG), rid('ana', s.CONFIG)} | set(s.MOODS_ROWS + s.LANES + s.STUDIO_ROWS)
    all_metas = [x for r in rows for x in source(r['metas'])]
    return {
        'moods_served': [len(row[c]) for c in s.MOODS_ROWS],
        'lanes_served': [len(row[c]) for c in s.LANES],
        'lanes_visible': sum(bool(row[c]) for c in s.LANES),
        'week_served': len(row[rid('week', s.CONFIG)]),
        'week_overlap_2a89c16': len(set(ids(row[rid('week', s.CONFIG)])) & old_week) if old_week is not None else None,
        'week_heroes': sum(s.is_hero(x) for x in row[rid('week', s.CONFIG)]),
        'week_heroes_first10': sum(s.is_hero(x) for x in row[rid('week', s.CONFIG)][:10]),
        'leap_served': len(row[rid('leap', s.CONFIG)]), 'ana_served': len(row[rid('ana', s.CONFIG)]),
        'because_served': len(row[rid('because', s.CONFIG)]),
        'home_titles': len(all_metas), 'home_heroes': sum(s.is_hero(x) for x in all_metas),
        'home_hero_share': round(sum(s.is_hero(x) for x in all_metas)/max(1, len(all_metas)), 5),
        'rows_majority_heroes': sum(sum(s.is_hero(x) for x in source(r['metas'])) > len(r['metas'])/2 for r in rows),
        'mood_heroes_first10': [sum(s.is_hero(x) for x in row[c][:10]) for c in s.MOODS_ROWS],
        'kid_toned_curated': sum(not s.tone_ok(x) for c in curated for x in row[c]),
        'animation_week_because_leap_ana': [sum('Animation' in s.row_facts(x)['g'] for x in row[c])
                                             for c in (rid('week', s.CONFIG), rid('because', s.CONFIG), rid('leap', s.CONFIG), rid('ana', s.CONFIG))],
        'max_anime_share_non_anime': round(max((sum(s.is_anime(x) for x in source(r['metas']))/len(r['metas'])
                                               for r in rows if r['metas'] and r['id'] not in (rid('airing', s.CONFIG), rid('nextseason', s.CONFIG))
                                               and not any(w in str(r['name']).casefold() for w in ('anime', 'manga'))), default=0), 5),
        'foryou_new_first12': len(set(ids(fy))-set(ids(no_session[rid('foryou', s.CONFIG)][:12]))),
        'foryou_seed_knn_first12': mean_knn(fy),
        'foryou_house_mean_first12': round(statistics.mean(s.score_key(m, x['id'], kind='movie' if x['type'] == 'movie' else 'tv')[0] or 0 for x in fy), 5),
        'person_rows_animation_first12': sum('Animation' in s.row_facts(x)['g'] for c in s.PERSON_ROWS for x in row[c][:12]),
        'foryou_animation_first12': sum('Animation' in s.row_facts(x)['g'] for x in fy),
        'ana_seed_knn_first12': mean_knn(row[rid('ana', s.CONFIG)]),
        'alpha': {p: s.person_alpha(m, p) if hasattr(s, 'person_alpha') else s.PERSON_BLEND*min(1, m['nexp'][p]/s.PERSON_NEXP) for p in s.PEOPLE}}


def sample(s, who):
    s.write_session({'who': 'off' if who == 'none' else who}, 'phone')
    s._home_cache.clear()
    start = time.process_time()
    rows = display_rows(s)
    return rows, (time.process_time()-start)*1000


def report(snapshot, before, base_rows=None, stub=False, gate_a=True):
    old_week = None
    out = {'sessions': {}, 'golden': {}}
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW), \
            home_server(snapshot, before) as old, home_server(snapshot) as new:
        if base_rows:
            dump = json.loads(Path(base_rows).read_text())
            old_week = {x[0] for x in dump[rid('week:tv', new.CONFIG)]}
        if stub:
            stub_refills(old); stub_refills(new)
        servers = {'before': old, 'after': new}
        first, homes, timings = {}, {}, {}
        for label, s in servers.items():
            rows, first[label] = sample(s, 'none')
            homes[label] = {'none': rows}
            timings[label] = {}
        for who in ('none', *new.PEOPLE, 'both'):
            # Warm scoring memos for this session, then alternate versions to limit drift.
            for label, s in servers.items():
                homes[label][who], _ = sample(s, who)
            times = {label: [] for label in servers}
            for _ in range(5):
                for label, s in servers.items():
                    _, ms = sample(s, who)
                    times[label].append(ms)
            item = {}
            for label, s in servers.items():
                none = {r['id']: r['metas'] for r in homes[label]['none']}
                item[label] = metrics(s, homes[label][who], none, old_week)
                timings[label][who] = round(statistics.median(times[label]), 3)
            a = {r['id']: r['metas'] for r in homes['before'][who]}
            b = {r['id']: r['metas'] for r in homes['after'][who]}
            item['foryou_first12_identical_to_before'] = a[rid('foryou', new.CONFIG)][:12] == b[rid('foryou', new.CONFIG)][:12]
            item['because_identical_to_before'] = a[rid('because', new.CONFIG)] == b[rid('because', new.CONFIG)]
            # Compare the head before rotation; the tail can shuffle slots 11-15 after it is attached.
            raw = {}
            for label, s in servers.items():
                with patch.object(s, 'rotate_row', side_effect=lambda xs, cid: xs):
                    raw[label] = s.build_home('de-DE', s._profiles['household'], s.remote_masks(), None if who in ('none', 'both') else who)[rid('week', new.CONFIG), 'tv']
            head = ids(raw['before'])
            curated = set(ids(new.row_list(rid('week', new.CONFIG), 'tv', 'de-DE')))
            tail = {x['id'] for x in b[rid('week', new.CONFIG)] if x['id'] not in curated and new.week_tail_ok(x)}
            item['week_head_identical_before_rotation'] = raw['before'] == raw['after'][:len(head)]
            item['week_tail_in_first10'] = len(tail & set(ids(b[rid('week', new.CONFIG)][:10])))
            item.update(rows_diff(homes['before'][who], homes['after'][who]))
            out['sessions'][who] = item
        out['cpu'] = {'first_build_ms': {k: round(v, 3) for k, v in first.items()}, 'median5_cold_ms': timings,
                      'ratios': {who: round(timings['after'][who]/timings['before'][who], 4) for who in timings['before']}}
        if gate_a:
            from eval.home_perf import identity
            new.write_session({'who': 'off'}, 'phone')
            out['gate_a'] = identity(new)
    a, b = sections(before, snapshot), sections(ROOT / 'server.py', snapshot)
    out['golden']['identical'] = {k: a[k] == b[k] for k in ('household', 'model', 'hashes')}
    out['golden']['sha256'] = {k: hashlib.sha256(b[k].encode()).hexdigest() for k in out['golden']['identical']}
    ar, br = json.loads(a['rows']), json.loads(b['rows'])
    out['golden'].update(rows_diff(ar, br))
    assert all(out['golden']['identical'].values()), 'profile/model/hash changed'
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    snapshot = snapshot if snapshot and snapshot.strip() else None
    ap.add_argument('--snapshot', type=Path, default=snapshot, required=not snapshot)
    ap.add_argument('--before', type=Path, required=True)
    ap.add_argument('--base-rows', type=Path, help='reviewer rows_base.json from 2a89c16')
    ap.add_argument('--stub-tmdb', action='store_true')
    ap.add_argument('--skip-gate-a', action='store_true')
    args = ap.parse_args()
    print(json.dumps(report(args.snapshot, args.before, args.base_rows, args.stub_tmdb, not args.skip_gate_a), sort_keys=True))
