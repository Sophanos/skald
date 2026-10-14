#!/usr/bin/env python3
"""Offline R3 lane gates; emit counts, hashes and timings only."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid, load_config

from eval.home_perf import digest
from eval.profile_golden import NOW
from eval.rows_report import display_rows, home_server, stub_refills


def replay(snapshot, source, stub):
    with home_server(snapshot, source) as s:
        if stub:
            stub_refills(s)
        out = {}
        for who in ('none', *s.PEOPLE, 'both'):
            if who != 'none':
                s.write_session({'who': who}, 'phone')
            rows = display_rows(s)
            requests = [(cid, typ) for cid, types, _ in s.feed_rows('de-DE') for typ in types]
            def identity():
                values = []
                for cid, typ in requests:
                    metas = s.row_metas(cid, typ, 'de-DE')
                    values.append((metas, getattr(metas, 'names', None)))
                return digest(values)
            cached = identity()
            with patch.object(s, '_HOME_CACHE_ENABLED', False):
                plain = identity() if hasattr(s, 'LANE_BENCH') else cached
            above, fronts, lanes = set(), set(), []
            for row in rows:
                cid, metas = row['id'], row['metas']
                if cid in s.LANES:
                    names = getattr(s.row_metas(cid, 'tv', 'de-DE'), 'names', s.feed(cid, 'de-DE').get('names') or {})
                    fid = names.get('lane')
                    coherent = sum(fid in {f for f, _ in s.facet_pairs(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv'))} for x in metas)
                    anime = any(w in names.get('title', '').casefold() for w in ('anime', 'manga'))
                    majority = sum('Animation' in s.row_facts(x)['g'] for x in metas) > len(metas) / 2
                    lanes.append({'slot': cid, 'cards': len(metas), 'coherence_pct': round(100*coherent/len(metas), 1) if metas else None,
                                  'animation_majority': majority, 'anime_allowed': anime, 'overlap': len({x['id'] for x in metas} & fronts)})
                fronts.update(x['id'] for x in metas[:12])
                above = s.mark_above(above, cid, metas)
            out[who] = {'lanes': lanes, 'visible': sum(x['cards'] > 0 for x in lanes), 'gate_a': cached == plain,
                        'rows': {(r['id'], r['type']): digest(r) for r in rows}}
        return out


def supply(snapshot, before):
    with home_server(snapshot, before) as s:
        pool = list(s.row_pool('de-DE'))
        out = {'row_pool': len(pool), 'existing_lanes': []}
        for cid in s.LANES:
            above = set()
            for other in s.DEDUPE_ORDER:
                if other == cid:
                    break
                for typ in ('movie', 'series') if other in (rid('top10', s.CONFIG), rid('toprated', s.CONFIG)) else ('movie',) if other == rid('digital', s.CONFIG) else ('tv',) if other in s.MIXED else ('series',):
                    above = s.mark_above(above, other, s.row_metas(other, typ, 'de-DE'))
            built = s.row_list(cid, 'tv', 'de-DE')
            ids = {x['id'] for x in built}
            eligible = [x for x in pool if x['id'] not in above and s.row_related(x, cid, 'de-DE')
                        and s.row_eligible(x, cid, 'de-DE', s._profiles['household'])]
            out['existing_lanes'].append({'built': len(built), 'built_front_loss': sum(x['id'] in above for x in built),
                                          'eligible_matches': len(eligible), 'eligible_additions': sum(x['id'] not in ids for x in eligible),
                                          'reserve': len((s._feeds.get((cid, 'de-DE')) or {}).get('metas', {}).get('pool', []))})
        # Count the generous supply before applying the builder's affinity floor and lane caps.
        above = set()
        for cid in s.DEDUPE_ORDER:
            if cid in s.LANES:
                break
            for typ in ('movie', 'series') if cid in (rid('top10', s.CONFIG), rid('toprated', s.CONFIG)) else ('movie',) if cid == rid('digital', s.CONFIG) else ('tv',) if cid in s.MIXED else ('series',):
                above = s.mark_above(above, cid, s.row_metas(cid, typ, 'de-DE'))
        candidates = []
        model = s.get_model(False)
        for fid, aff in model['aff'].items():
            if aff <= 0 or fid[0] not in s.LANE_KINDS:
                continue
            ids = {x['id'] for x in pool if x['id'] not in above and s.row_eligible(x, rid('lane1', s.CONFIG), 'de-DE', s._profiles['household'])
                   and fid in {f for f, _ in s.facet_pairs(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv'))}}
            if len(ids) >= 10:
                candidates.append(ids)
        out['ten_card_facets'] = len(candidates)
        with home_server(snapshot) as current, patch.object(current, 'lane_titles', return_value=[]):
            built = current.lanes_feed('de-DE')
            out['cached_build'] = {'primary_cards': [len(built[cid]['tv']) for cid in current.LANES],
                                   'bench_cards': [len(built[cid]['tv']) for cid in current.LANE_BENCH]}
        return out


def report(snapshot, before):
    config = load_config(str(snapshot), {}, readonly=True)[0]
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW):
        capacity = supply(snapshot, before)
        out = {'supply': capacity, 'sessions': {}}
        for stub in (False, True):
            base, current = replay(snapshot, before, stub), replay(snapshot, ROOT / 'server.py', stub)
            out['sessions']['stub_on' if stub else 'stub_off'] = current
            for who, result in current.items():
                changed = [f'{cid}:{typ}' for (cid, typ), sha in result.pop('rows').items() if base[who]['rows'][cid, typ] != sha]
                above = [rid('airing', config), rid('nextseason', config)] + [rid(f'lane{i}', config) for i in range(1, 6)]
                result['changed_rows'] = changed
                result['above_identity'] = all(x.split(':')[0] in above for x in changed)
                result['depth_gate'] = result['visible'] >= 4 and all(10 <= x['cards'] <= 20 for x in result['lanes'] if x['cards'])
                result['coherence_gate'] = all(x['coherence_pct'] is None or x['coherence_pct'] >= 80 for x in result['lanes'])
                result['dedupe_gate'] = all(x['overlap'] == 0 for x in result['lanes'])
                result['animation_gate'] = all(not x['animation_majority'] or x['anime_allowed'] for x in result['lanes'])
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    snapshot = os.environ.get('SKALD_SNAPSHOT')
    ap.add_argument('--snapshot', type=Path, default=snapshot if snapshot and snapshot.strip() else None)
    ap.add_argument('--before', type=Path, required=True)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    if not args.snapshot:
        ap.error('use --snapshot or SKALD_SNAPSHOT')
    result = report(args.snapshot, args.before)
    print(json.dumps(result, sort_keys=True))
    if args.check and not all(all(r[k] for k in ('gate_a', 'above_identity', 'depth_gate', 'coherence_gate', 'dedupe_gate', 'animation_gate'))
                              for mode in result['sessions'].values() for r in mode.values()):
        raise SystemExit(1)
