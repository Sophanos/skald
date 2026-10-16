#!/usr/bin/env python3
"""Offline taste-memory diagnostics; JSON contains counts, shares and timings only."""
import argparse
import os
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
import statistics
import sys
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid

from eval import profile_golden as golden, replay
from eval import home_perf
from eval.rows_report import home_server


def empty(s):
    return s._memory_empty()


def model(s, p):
    with redirect_stdout(io.StringIO()):
        return s.build_model(p)


def model_times(old, current):
    samples = {'base_model_ms': [], 'memory_model_ms': []}
    pair = [('base_model_ms', old), ('memory_model_ms', current)]
    for i in range(21):
        for label, s in pair if i % 2 else reversed(pair):
            start = time.process_time()
            model(s, s._profiles['household'])
            samples[label].append((time.process_time()-start)*1000)
    return {label: round(statistics.median(xs), 3) for label, xs in samples.items()}


def shares(s, p):
    weights, total, backfill, dated_march = {}, 0., 0., 0.
    entries = 0
    for e in p['entries']:
        w = e['weight']
        if w <= 0:
            continue
        who = e['who']
        weights[who] = weights.get(who, 0.)+w
        total += w
        entries += 1
        if (e.get('item') or {}).get('backfilled'):
            backfill += w
        t = (e.get('item') or {}).get('at')
        if e['who'] == 'owner' and t and '2025-03-01' <= s.datetime.datetime.fromtimestamp(t/1000, s.datetime.timezone.utc).date().isoformat() < '2025-04-01':
            dated_march += w
    return {'positive_entries': entries, 'positive_weight': total,
            'who': {who: w/(total or 1) for who, w in sorted(weights.items())},
            'backfill': backfill/(total or 1), 'trakt_march_2025': dated_march/(total or 1)}


def supports(s, evts, m):
    candidates = {}
    profile = s._profiles.get('household') or {}
    titles = {(e['key'], e['kind']) for e in profile.get('entries', ())}
    moves = s.title_moves()
    readings = {(p, e['key'], e['kind']): s.entry_weight(e)
                for p in s.PEOPLE for e in s.person_entries(profile, p)} if profile.get('entries') else {}
    for p in s.PEOPLE:
        pairs = sorted(((fid, a) for fid, a in m['paff'].get(p, {}).items()
                        if fid[0] in s.MEMORY_TYPES and fid in m['names'] and s.speaks(fid, m['names'][fid]) and a),
                       key=lambda x: (-abs(x[1]), x[0]))[:s.MEMORY_TOP]
        candidates[p] = {fid: {'kind': 'like' if a > 0 else 'avoid', 'keys': set(), 'families': set(), 'weeks': set()}
                         for fid, a in pairs}
    taint = {}
    for ev in evts:
        if ev['src'] not in taint:
            taint[ev['src']] = s.tainted(ev)
        if taint[ev['src']] or ev['action'] not in s.MEMORY_POS | s.MEMORY_NEG:
            continue
        held = {f for f, _ in s.facet_pairs(s.frec(ev['key'], ev['kind']))}
        kind = 'like' if ev['action'] in s.MEMORY_POS else 'avoid'
        owners = s.watch_owners(ev, moves)
        for p in s.PEOPLE:
            if p not in ev['by'] and p not in owners:
                continue
            weight = readings.get((p, ev['key'], ev['kind']))
            if weight is None:
                weight = (0 if (ev['key'], ev['kind']) in titles or p not in owners or ev['action'] == 'abandon'
                          else ev['conf'] * (1 if kind == 'like' else -1))
            if not weight or (weight > 0) != (kind == 'like'):
                continue
            for fid in held & candidates[p].keys():
                acc = candidates[p][fid]
                if acc['kind'] != kind:
                    continue
                acc['keys'].add(ev['key'])
                acc['families'].add(s.memory_family(ev['src']))
                if ev['t'] is not None:
                    acc['weeks'].add(s.memory_week(ev['t']))
    return candidates


def curate_counts(s, evts, m):
    first = s.curate(iter(evts), m, now=golden.NOW, store=empty(s), dry_run=True)
    second = s.curate(iter(evts), m, now=golden.NOW, store=first['store'], dry_run=True)
    result = {'second_run_ops': len(second['ops']), 'events': first['stats']['events'], 'people': {}}
    for p, candidates in supports(s, evts, m).items():
        result['people'][p] = {'candidates': {kind: sum(c['kind'] == kind for c in candidates.values()) for kind in ('like', 'avoid')},
                              'support3': sum(len(c['keys']) >= s.MEMORY_SUPPORT for c in candidates.values()),
                              'fam2': sum(len(c['families']) >= s.MEMORY_SPREAD for c in candidates.values()),
                              'weeks2': sum(len(c['weeks']) >= s.MEMORY_SPREAD for c in candidates.values()),
                              'proposed': first['stats']['proposed'][p], 'active': first['stats']['active'][p]}
    return result, first


def hit_ab(s, p, evts):
    universe = sorted(k for k in s._facets if '@' not in k and k not in p['seen'])
    def top(m, person):
        scores = [(-score, key) for key in universe if (score := s.score_key(m, key, person)[0]) is not None]
        return {key for _, key in sorted(scores)[:30]}
    def fact_ids(m):
        r = s.curate(iter(evts), m, now=golden.NOW, store=empty(s), dry_run=True)
        return {(f['person'], f['kind'], f['about']) for f in r['store']['facts'] if s._memory_live(f)}
    with patch.dict(s._learn, {'hits': []}), patch.object(s, 'HIT_TASTE', False):
        base = model(s, p)
        base_facts = fact_ids(base)
        before = {person or 'household': top(base, person) for person in (None, s.PEOPLE[1], 'together')}
        positives = sorted(((-score, e['key']) for e in p['entries'] if e['weight'] > 0
                            if (score := s.score_key(base, e['key'], kind=e['kind'])[0]) is not None))
        keys = list(dict.fromkeys(key for _, key in positives))
        out = {}
        for k in (20, 50, 200):
            planted = [{'key': key} for key in keys[:k]]
            out[str(k)] = {}
            for enabled in (True, False):
                with patch.dict(s._learn, {'hits': planted}), patch.object(s, 'HIT_TASTE', enabled):
                    m = model(s, p)
                    out[str(k)]['on' if enabled else 'off'] = {
                        'displaced': {person or 'household': len(before[person or 'household']-top(m, person))
                                      for person in (None, s.PEOPLE[1], 'together')},
                        'new_facts': len(fact_ids(m)-base_facts)}
        return out


def holdout(s, p, evts):
    tune_keys = {e['key'] for e in p['entries'] if replay.split_for(e['key']) == 'tune'}
    tune = replay.profile_with(p, [e for e in p['entries'] if e['key'] in tune_keys])
    m = model(s, tune)
    tune_events = [e for e in evts if e['key'] in tune_keys]
    r = s.curate(iter(tune_events), m, now=golden.NOW, store=empty(s), dry_run=True)
    support = supports(s, tune_events, m)
    universe = [rec for key, rec in s._facets.items() if '@' not in key and key not in p['seen']]
    held_universe = [{f for f, _ in s.facet_pairs(rec)} for rec in universe]
    out = {}
    for person in s.PEOPLE:
        test = [e for e in s.person_entries(p, person) if e.get('explicit') and e['weight'] > 0
                and replay.split_for(e['key']) == 'test']
        held_test = [{f for f, _ in s.facet_pairs(s.frec(e['key'], e['kind']))} for e in test]
        gated = {f['about'] for f in r['store']['facts'] if f['person'] == person and f['kind'] == 'like' and s._memory_live(f)}
        support_only = {fid for fid, acc in support[person].items() if acc['kind'] == 'like' and len(acc['keys']) >= s.MEMORY_SUPPORT}
        out[person] = {}
        for label, fids in (('gated', gated), ('support_only', support_only)):
            lifts = []
            for fid in sorted(fids):
                background = sum(fid in held for held in held_universe)/(len(held_universe) or 1)
                if held_test and background:
                    lifts.append((sum(fid in held for held in held_test)/len(held_test))/background)
            out[person][label] = {'n': len(lifts), 'median': statistics.median(lifts) if lifts else None,
                                  'min': min(lifts) if lifts else None,
                                  'share_gt_1': sum(x > 1 for x in lifts)/len(lifts) if lifts else None}
    return out


def percentile(samples, p=.95):
    return sorted(samples)[int(p*(len(samples)-1))]


def budget(s, evts, m):
    tracemalloc.start()
    r = s.curate(iter(evts), m, now=golden.NOW, store=empty(s), dry_run=True)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    elapsed = []
    for _ in range(3):
        start = time.perf_counter()
        s.curate(iter(evts), m, now=golden.NOW, store=empty(s), dry_run=True)
        elapsed.append((time.perf_counter()-start)*1000)
    store = empty(s)
    for person in s.PEOPLE:
        for i in range(s.MEMORY_CAP+s.MEMORY_ENDED):
            ended = i >= s.MEMORY_CAP
            f = s._memory_new(store, person, 'like', f'g:Synthetic {i}', golden.NOW, {},
                              strength=.6, n=12, status='decayed' if ended else 'active',
                              evidence=[] if ended else [f'trakt:movie:tmdb:{j+1000000}#{i}' for j in range(s.MEMORY_EVIDENCE)],
                              gates={'support': 12, 'families': 2, 'weeks': 3, 'rank': 1},
                              end=golden.NOW if ended else None)
            store['seq'] += 1
    s.memory_save(store, [])
    size = (Path(s.DATA)/'memory.json').stat().st_size
    s._memory = None
    tracemalloc.start()
    s.memory_load()
    load_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    s._facets['tmdb:9999999'] = {'kind': 'movie', 'g': ['Synthetic 0']}
    why = []
    for _ in range(1000):
        start = time.perf_counter()
        s.memory_why('tmdb:9999999', 'movie', 'together', now=golden.NOW)
        why.append((time.perf_counter()-start)*1000)
    due = []
    with patch('builtins.open', side_effect=AssertionError('memory_due reads a file')):
        for _ in range(1000):
            start = time.perf_counter()
            s.memory_due(golden.NOW)
            due.append((time.perf_counter()-start)*1000)
    return {'events': r['stats']['events'], 'curate_ms': statistics.median(elapsed), 'curate_peak_bytes': peak,
            'memory_bytes': size, 'load_peak_bytes': load_peak, 'why_p95_ms': percentile(why), 'due_p95_ms': percentile(due)}


def synthetic(s, p, m):
    """Fixed-seed facts; report any infeasible rank constraint in the diagnostics."""
    rng, chosen = random.Random(replay.SEED), {}
    universe = {k: {fid for fid, _ in s.facet_pairs(rec)} for k, rec in s._facets.items()
                if '@' not in k and k not in p['seen']}
    for person in s.PEOPLE:
        ranked = sorted(m['paff'][person], key=lambda f: (-m['paff'][person][f], f))
        eligible = [fid for fid in ranked if fid[0] in 'xgk' and sum(fid in held for held in universe.values()) >= 30]
        pool = [fid for fid in ranked[19:40] if fid in eligible]
        fallback = len(pool) < 4
        if fallback:
            pool = eligible[19:40]
        avoids = [fid for fid in pool if sum(fid in held for held in universe.values())/len(universe) <= s.MEMORY_VETO_MAX_SHARE]
        assert len(pool) >= 4 and avoids, f'not enough synthetic facets: {person}'
        avoid = rng.choice(avoids)
        likes = rng.sample([fid for fid in pool if fid != avoid], 3)
        chosen[person] = {'likes': likes, 'avoid': avoid, 'rank_pool': 'coverage30' if fallback else 'all',
                          'ranks': {fid: ranked.index(fid)+1 for fid in likes+[avoid]}}
        for kind, fids in (('like', likes), ('avoid', [avoid])):
            for fid in fids:
                s.memory_tell(person, kind, fid, now=golden.NOW)
    return chosen


def model_report(snapshot, before=None):
    with patch('time.time', return_value=golden.NOW), redirect_stdout(io.StringIO()):
        with home_server(snapshot, before or ROOT/'server.py') as old:
            baseline_time = home_perf.measure(old)
            requests = [(cid, typ) for cid, types, _ in old.feed_rows('de-DE') for typ in types]
            empty_before = {who: home_perf.digest([old.row_metas(cid, typ, 'de-DE', who=who) for cid, typ in requests])
                            for who in ('none', *old.PEOPLE, 'both')}
        with home_server(snapshot) as s:
            p, base = s._profiles['household'], s.get_model(False)
            requests = [(cid, typ) for cid, types, _ in s.feed_rows('de-DE') for typ in types]
            def rows(who):
                return {(cid, typ): s.row_metas(cid, typ, 'de-DE', who=who) for cid, typ in requests}
            def foryou_pool(person):
                captured, order = [], s.person_order
                def capture(metas, who):
                    captured.append(metas)
                    return order(metas, who)
                with patch.object(s, 'person_order', side_effect=capture), patch.object(s, '_HOME_CACHE_ENABLED', False):
                    s.row_metas(rid('foryou', s.CONFIG), 'tv', 'de-DE', who=person)
                return captured[-1]
            before_rows = {who: rows(who) for who in (*s.PEOPLE, 'both')}
            empty_after = {who: home_perf.digest(list(rows(who).values())) for who in ('none', *s.PEOPLE, 'both')}
            assert empty_before == empty_after, 'P0: empty memory changed a session'
            before_pool = {who: foryou_pool(who) for who in s.PEOPLE}
            chosen = synthetic(s, p, base)
            synthetic_store = s.memory_load()
            m = model(s, p)
            s._model['m'] = m
            universe = {key: {fid for fid, _ in s.facet_pairs(rec)} for key, rec in s._facets.items()
                        if '@' not in key and key not in p['seen']}
            def top(m, person):
                return sorted(universe, key=lambda k: (-(s.score_key(m, k, person)[0] or 0), k))[:12]
            def stats(aff):
                xs = [a for a in aff.values() if a > 0]
                return {f'p{q}': percentile(xs, q/100) if xs else 0 for q in (50, 90, 99)}
            after_rows = {who: rows(who) for who in (*s.PEOPLE, 'both')}
            reserve = {person: {'likes': {}, 'days': []} for person in s.PEOPLE}
            with home_server(snapshot, before or ROOT/'server.py') as old:
                old_base = old.get_model(False)
                old.memory_save(synthetic_store, [])
                old_memory = model(old, old._profiles['household'])
                old._model['m'] = old_memory
                today_rows = {who: {(cid, typ): old.row_metas(cid, typ, 'de-DE', who=who) for cid, typ in requests}
                              for who in (*s.PEOPLE, 'both')}
                both_identical = home_perf.digest(list(today_rows['both'].values())) == home_perf.digest(list(after_rows['both'].values()))
                assert both_identical, 'P0: both changed with memory'
                for person in s.PEOPLE:
                    fids = set(chosen[person]['likes'])
                    count = lambda metas, fids: sum(bool(universe.get(x['id'], set()) & fids) for x in metas[:12])
                    triple = (before_rows[person], today_rows[person], after_rows[person])
                    for fid in chosen[person]['likes']:
                        reserve[person]['likes'][fid] = [count(r[rid('foryou', s.CONFIG), 'tv'], {fid}) for r in triple]
                    reserve[person]['any'] = [count(r[rid('foryou', s.CONFIG), 'tv'], fids) for r in triple]
                    reserve[person]['rows_moved'] = sum(a != after_rows[person][key] for key, a in today_rows[person].items())
                for d in range(7):
                    now = golden.NOW+d*s.DAY
                    day = s.datetime.datetime(2026, 10, 11, 18)+s.datetime.timedelta(days=d)
                    with patch('time.time', return_value=now), patch.object(s, 'local_now' if hasattr(s, 'local_now') else 'berlin_now', return_value=day), \
                            patch.object(old, 'local_now' if hasattr(old, 'local_now') else 'berlin_now', return_value=day):
                        old._model['m'] = old_base
                        old.memory_save(empty(old), [])
                        a = {pp: old.row_metas(rid('foryou', s.CONFIG), 'tv', 'de-DE', who=pp) for pp in s.PEOPLE}
                        for pp in s.PEOPLE:
                            b = s.row_metas(rid('foryou', s.CONFIG), 'tv', 'de-DE', who=pp)
                            pool = foryou_pool(pp)
                            eligible = {x['id'] for x in pool if not s.is_anime(x)}
                            like_counts = {fid: sum(fid in universe.get(x['id'], ()) for x in b[:12]) for fid in chosen[pp]['likes']}
                            fids = set(chosen[pp]['likes'])
                            any_count = lambda metas: sum(bool(universe.get(x['id'], set()) & fids) for x in metas[:12])
                            delta = any_count(b)-any_count(a[pp])
                            covered = all(like_counts[fid] >= 1 for fid in fids if any(fid in universe.get(k, ()) for k in eligible))
                            reserve[pp]['days'].append({'day': d, 'any': [any_count(a[pp]), any_count(b)], 'delta': delta,
                                                        'covered': covered, 'likes': like_counts})
                for pp in s.PEOPLE:
                    reserve[pp]['min_delta'] = min(day['delta'] for day in reserve[pp]['days'])
                    reserve[pp]['min_any'] = min(day['any'][1] for day in reserve[pp]['days'])
                    reserve[pp]['passed'] = reserve[pp]['min_delta'] >= 2 and all(day['covered'] for day in reserve[pp]['days'])
            floor2 = sum(v[-1] >= 2 for pp in reserve.values() for v in pp['likes'].values())
            plus2 = sum(v[-1]-v[0] >= 2 for pp in reserve.values() for v in pp['likes'].values())
            persona = {}
            for person in s.PEOPLE:
                a, b = top(base, person), top(m, person)
                served_a = before_rows[person][rid('foryou', s.CONFIG), 'tv'][:12]
                served_b = after_rows[person][rid('foryou', s.CONFIG), 'tv'][:12]
                pool_a, pool_b = before_pool[person], foryou_pool(person)
                persona[person] = {'q99': stats(base['paff'][person])['p99'], 'facts_applied': 4,
                                   'before': stats(base['paff'][person]), 'after': stats(m['paff'][person]), 'likes': {}}
                for fid in chosen[person]['likes']:
                    count = lambda keys: sum(fid in universe.get(k, ()) for k in keys)
                    persona[person]['likes'][fid] = {'pool': count(universe), 'top12': [count(a), count(b)],
                                                    'served_pool': [count(x['id'] for x in pool_a), count(x['id'] for x in pool_b)],
                                                    'served12': [count(x['id'] for x in served_a), count(x['id'] for x in served_b)]}
            # Measure a fresh home before diagnostics warm its row caches.
            with home_server(snapshot) as timed:
                timed.memory_save(synthetic_store, [])
                timed._model['m'] = model(timed, p)
                elapsed = home_perf.measure(timed)
                with home_server(snapshot, before or ROOT/'server.py') as old:
                    old.memory_save(synthetic_store, [])
                    build_times = model_times(old, timed)
            identity = home_perf.identity(s)
            s._memory_veto.clear()
            start = time.perf_counter()
            s.veto_keys('both', golden.NOW)
            cold = (time.perf_counter()-start)*1000
            warm = []
            for _ in range(100):
                start = time.perf_counter()
                s.veto_keys('both', golden.NOW)
                warm.append((time.perf_counter()-start)*1000)
            notes = {}
            for who in (*s.PEOPLE, 'both'):
                counts = []
                for metas in rows(who).values():
                    assert not any('_memory' in x for x in metas), 'memory marker leaked'
                    n = sum(x.get('description', '').startswith(('Because you told me you like ', 'You both like ')) for x in metas)
                    assert n <= s.MEMORY_WHY_ROW, 'row note cap exceeded'
                    counts.append(n)
                notes[who] = {'rows': len(counts), 'max': max(counts), 'total': sum(counts)}
            # One avoid, then three likes, isolate the partner's taste from the viewer.
            veto, isolation = {}, {}
            for person in s.PEOPLE:
                s.memory_save(empty(s), [])
                s._model['m'] = base
                baseline = {who: rows(who) for who in (*s.PEOPLE, 'both')}
                s.memory_tell(person, 'avoid', chosen[person]['avoid'], now=golden.NOW)
                s._model['m'] = model(s, p)
                other = next(pp for pp in s.PEOPLE if pp != person)
                other_rows = rows(other)
                other_changed = [cid+':'+typ for cid, typ in baseline[other]
                                 if home_perf.digest(baseline[other][cid, typ]) != home_perf.digest(other_rows[cid, typ])]
                assert not other_changed, f'V1 avoid changed {other}'
                isolation[person] = {'avoid': len(other_changed)}
                sessions = {}
                for who in (person, 'both'):
                    current = rows(who)
                    shrank, thin, excluded, exempt_changes, added_n, lost_n = {}, {}, 0, 0, 0, 0
                    for (cid, typ), metas in current.items():
                        label = cid+':'+typ
                        previous = baseline[who][cid, typ]
                        if cid in s.VETO_EXEMPT:
                            exempt_changes += metas != previous
                            old_ids, new_ids = {x['id'] for x in previous}, {x['id'] for x in metas}
                            added, lost = new_ids-old_ids, old_ids-new_ids
                            added_n, lost_n = added_n+len(added), lost_n+len(lost)
                            above = s.DEDUPE_ORDER[:s.DEDUPE_ORDER.index(cid)]
                            prior = lambda r: {x['id'] for (c, t), xs in r.items() if c in above for x in xs}
                            assert added <= prior(baseline[who])-prior(current), 'V3: addition did not leave a row above'
                            assert all(chosen[person]['avoid'] in {fid for fid, _ in s.facet_pairs(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv'))}
                                       for x in metas if x['id'] in added), 'V3: addition carries no veto'
                            full_a, full_b = baseline[who].get((cid, 'tv'), previous), current.get((cid, 'tv'), metas)
                            full_added = {x['id'] for x in full_b}-{x['id'] for x in full_a}
                            limit = 12 if cid == rid('episodes', s.CONFIG) else 20
                            assert not lost or full_added and len(full_b) == limit and lost <= {x['id'] for x in full_a[-len(full_added):]}, 'V3: loss beyond limit'
                        else:
                            excluded += sum(chosen[person]['avoid'] in {fid for fid, _ in s.facet_pairs(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv'))} for x in metas)
                            if len(metas) < len(previous):
                                shrank[label] = len(previous)-len(metas)
                            if len(metas) < 6:
                                thin[label] = len(metas)
                    assert excluded == 0, 'veto leaked'
                    sessions[who] = {'rows': len(current), 'remaining_matches': excluded, 'exempt_changes': exempt_changes,
                                     'exempt_added': added_n, 'exempt_lost': lost_n,
                                     'shrank': shrank, 'under6': thin}
                veto[person] = {'facet': chosen[person]['avoid'], 'sessions': sessions,
                                'other_identical': not other_changed, 'other_changed': other_changed}
                s.memory_save(empty(s), [])
                for fid in chosen[person]['likes']:
                    s.memory_tell(person, 'like', fid, now=golden.NOW)
                s._model['m'] = model(s, p)
                other_rows = rows(other)
                changes = sum(home_perf.digest(a) != home_perf.digest(other_rows[key]) for key, a in baseline[other].items())
                assert not changes, f'V1 likes changed {other}'
                isolation[person]['likes'] = changes
            breadth = s._memory_breadth()
            # A single fact checks the visible wording independently of the synthetic mix.
            s.memory_save(empty(s), [])
            s._model['m'] = base
            held = [fid for x in before_rows[s.PEOPLE[1]][rid('foryou', s.CONFIG), 'tv'][:12]
                    for fid, _ in s.facet_pairs(s.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv')) if fid[0] in 'xgk']
            assert held, 'no eligible For You note facet'
            s.memory_tell(s.PEOPLE[1], 'like', held[0], now=golden.NOW)
            s._model['m'] = model(s, p)
            one = rows(s.PEOPLE[1])[rid('foryou', s.CONFIG), 'tv']
            notes['single_told_foryou'] = sum(x.get('description', '').startswith('Because you told me you like ') for x in one)
            assert notes['single_told_foryou'] >= 1, 'single told fact has no note'
            s.memory_tell(s.PEOPLE[0], 'like', held[0], now=golden.NOW)
            s._model['m'] = model(s, p)
            together = {who: sum(x.get('description', '').startswith('You both like ') for x in rows(who)[rid('foryou', s.CONFIG), 'tv'])
                        for who in (*s.PEOPLE, 'both')}
            assert together['both'] >= 1 and not together[s.PEOPLE[1]] and not together[s.PEOPLE[0]], 'together note scope'
            notes['together_foryou'] = together
            faded = s.memory_load()
            for f in faded['facts']:
                f.update(src='implicit', strength=.78, seen=golden.NOW-180*s.DAY, confirmed=None)
            s.memory_save(faded, [])
            s._model['m'] = model(s, p)
            notes['implicit_039_foryou'] = sum(x.get('description', '').startswith('You like ') for x in rows(s.PEOPLE[1])[rid('foryou', s.CONFIG), 'tv'])
            assert not notes['implicit_039_foryou'], 'decayed fact still has a note'
            s.memory_save(empty(s), [])
            s.memory_tell(s.PEOPLE[1], 'avoid', 'g:Drama', now=golden.NOW)
            assert not s.veto_keys('both', golden.NOW), 'broad veto hid titles'
            assert s.memory_view(now=golden.NOW)[0]['veto'] == 'too_broad'
            s.memory_save(synthetic_store, [])
            return {'pseudo': s.MEMORY_PSEUDO, 'selection': chosen, 'persona': persona, 'veto': veto, 'notes': notes,
                    'reserve': reserve, 'floor2': floor2, 'per_like_plus2': plus2, 'isolation': isolation,
                    'p0': {'empty_identical': empty_before == empty_after, 'both_memory_identical': both_identical},
                    'breadth_cap': {fid: share for fid, share in breadth.items() if share > s.MEMORY_VETO_MAX_SHARE},
                    'cache': identity, 'time': {'base': baseline_time, 'memory': elapsed,
                                               **build_times,
                                               'veto_cold_ms': cold, 'veto_warm_p95_ms': percentile(warm)}}


def report(snapshot, overlay=None, with_model=False, before=None):
    stubs, seed = golden.snapshot_stubs(snapshot)
    with golden.offline_server(ROOT/'server.py', snapshot, stubs) as s, redirect_stdout(io.StringIO()):
        p = s.build_profile(s.read_history(), [], s.read_trakt())
        s._profiles['household'] = p
        m = model(s, p)
        s._model['m'] = m
        evts = list(s.events(golden.NOW, s.read_history(), s.read_trakt()))
        counts, _ = curate_counts(s, evts, m)
        out = {'shares': shares(s, p), 'hit_ab': hit_ab(s, p, evts), 'curate': counts,
               'holdout': holdout(s, p, evts), 'budget': budget(s, evts, m)}
    if overlay is not None:
        with golden.offline_server(ROOT/'server.py', snapshot, stubs, overlay, seed) as s, redirect_stdout(io.StringIO()):
            p = s.build_profile(s.read_history(), [], s.read_trakt())
            s._profiles['household'] = p
            m = model(s, p)
            s._model['m'] = m
            evts = list(s.events(golden.NOW, s.read_history(), s.read_trakt()))
            out['budget'] = {**budget(s, evts, m), 'overlay': overlay}
    if with_model:
        out['model'] = model_report(snapshot, before)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot', type=Path, default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    ap.add_argument('--overlay', type=int)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--model', action='store_true')
    ap.add_argument('--before', type=Path)
    args = ap.parse_args()
    if args.overlay is not None and args.overlay < 0:
        ap.error('--overlay must be nonnegative')
    output = replay.workspace_output(args.out) if args.out is not None else None
    result = report(args.snapshot, args.overlay, args.model, args.before)
    raw = json.dumps(result, sort_keys=True, indent=2, allow_nan=False)+'\n'
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output/'memory_report.json').write_text(raw)
    print(raw, end='')
    if args.model:
        assert all(p['passed'] for p in result['model']['reserve'].values()), 'P′: seven-day gate failed (see reserve table)'
        timing = result['model']['time']
        assert timing['memory_model_ms'] <= timing['base_model_ms'] * 1.10, 'model build exceeds base +10%'


if __name__ == '__main__':
    main()
