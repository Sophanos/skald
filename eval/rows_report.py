#!/usr/bin/env python3
"""Offline home replay: python3.12 eval/rows_report.py --snapshot PATH --before old-server.py."""
import argparse
import bisect
from contextlib import contextmanager, redirect_stdout
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.members import rid
from skald.engine import Engine, heldout
from eval.stop_report import explicit_negative
snapshot = os.environ.get('SKALD_SNAPSHOT')
SNAPSHOT = snapshot if snapshot and snapshot.strip() else None
FILES = ('feeds.json', 'shown.json', 'profile.json', 'emb.json', 'facets.json', 'learn.json', 'taste.json', 'nzb.json')
SEED_ROWS = ('because', 'ana', 'week', 'foryou')
BECAUSE_MARKS = {'tmdb:280': 'Terminator 2', 'tmdb:137113': 'Edge of Tomorrow', 'tmdb:1022787': 'Elio',
                 'tmdb:1022789': 'Inside Out 2', 'tmdb:482': 'Shaft 1971', 'tmdb:173': '20,000 Leagues'}


@contextmanager
def home_server(snapshot, source=ROOT / 'server.py', day='2026-10-11'):
    """Public snapshot inputs only, copied before import; no worker threads or network."""
    def denied(*args, **kwargs):
        raise AssertionError('network forbidden in home replay')

    with tempfile.TemporaryDirectory(prefix='skald-rows-') as temp, \
            patch.dict(os.environ, {'TMDB_API': 'offline', 'DATA_DIR': temp, 'OPENROUTER_API_KEY': ''}), \
            patch('urllib.request.urlopen', denied):
        for name in FILES:
            shutil.copyfile(Path(snapshot) / name, Path(temp) / name)
        for name in ('.skald-format', 'skald.json', 'legacy.json'):
            path = Path(snapshot) / name
            if path.is_file():
                shutil.copyfile(path, Path(temp) / name)
        spec = importlib.util.spec_from_file_location('skald_rows', source)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        setattr(server, "local_now" if hasattr(server, "local_now") else "berlin_now",
                lambda ts=None: datetime.datetime.fromisoformat(day + 'T18:00:00'))
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            setattr(server, name, lambda *args, **kwargs: None)
        try:
            server.load_state()
            p = json.loads(Path(temp, 'profile.json').read_text())
            p['seen'], p['negative'] = set(p['seen']), set(p['negative'])
            server._profiles['household'] = p
            server.tmdb = denied  # build_model tolerates absent offline calibration
            with redirect_stdout(io.StringIO()):
                server._model['m'] = server.build_model(p)
            server.get_model = lambda build=True: server._model['m']
            yield server
        finally:
            for name in ('_pool', '_outer', '_nzb_pool'):
                getattr(server, name).shutdown(wait=True, cancel_futures=True)


def display_rows(server, lang='de-DE', order=None):
    feeds = server.feed_rows(lang)
    names = {cid: name for cid, _, name in feeds}
    requests = [(cid, typ) for cid, types, _ in feeds for typ in (('tv',) if 'tv' in types else types)]
    order = order or os.environ.get('SKALD_ROW_ORDER')
    if order:
        # Player order can split the Top 10 movie and series rows.
        source = Path(order).read_text().split('const ORDER = [', 1)[1].split('];', 1)[0]
        import re
        requests = [(rid(suffix, server.CONFIG), typ)
                    for suffix, typ in re.findall(r"'\w+\.([^:']+):([^']+)'", source)]
    rows = []
    for cid, typ in requests:
        if cid not in names:
            continue
        if server.CONFIG.get('household', {}).get('rows') is not None and not server.row_enabled(cid):
            continue
        name = names[cid]
        if isinstance(name, dict):
            name = name[typ]
        rows.append({'id': cid, 'type': typ, 'name': name, 'metas': server.row_metas(cid, typ, lang)})
    return rows


def stub_refills(server, lang='de-DE'):
    """Replay finite synthetic TMDB responses through each thin shelf's own source, behind its old pin."""
    s = server
    fixture = json.loads((ROOT / 'tests/fixtures/rows/tmdb.json').read_text())
    titles = fixture.pop('titles')
    cids = [rid('episodes', server.CONFIG), rid('top10', server.CONFIG), rid('leap', server.CONFIG), rid('mood3', server.CONFIG), rid('mood4', server.CONFIG)] + s.LANES
    for slot, cid in enumerate(cids):
        names = s.feed(cid, lang).get('names') or {}
        lane, mood, seed = names.get('lane'), names.get('mood'), s.row_seed(cid, lang)
        xs = s.MOODS[mood][1] if mood else list(s.frec(seed, s.profile_kind(seed)).get('x') or {})
        if lane and lane.startswith('x:'):
            xs = [lane[2:]]
        def response(path, **params):
            if path.startswith('genre/'):
                return {'genres': []}
            kind = 'tv' if '/tv' in path or path.startswith('tv/') else 'movie'
            data = []
            for i, title in enumerate(titles):
                tid = 9900000 + slot * 100 + (50 if kind == 'tv' else 0) + i
                key = f'tmdb:{tid}'
                rec = {'kind': kind, 'y': '2025', 'g': ['Action'], 'x': {x: 2 for x in xs}}
                if lane and lane[0] != 'x':
                    rec[lane[0]] = [[int(lane[2:]), 'marvel cinematic universe (mcu)' if lane == 'k:180547' else lane]]
                s._facets[key] = rec
                data.append({**fixture, 'id': tid, 'title': f'{cid[5:]} {title}', 'name': f'{cid[5:]} {title}',
                             'media_type': kind, 'job': 'Director'})
            return {'results': data, 'cast': [], 'crew': data}
        with patch.object(s, 'tmdb', side_effect=response):
            if lane:
                metas = [x for _, _, x in s.lane_titles(lane, lang)]
                rows = {'tv': metas}
            elif cid == rid('top10', server.CONFIG):
                rows = s.top10_feed(lang)
            elif cid == rid('episodes', server.CONFIG):
                # Episode refills are owned shows; fixture details give every real resume a next episode.
                entries = {e['id']: e for e in s._profiles['household']['entries'] if e['stype'] == 'series'}
                def details(path, **params):
                    if path.startswith('genre/'):
                        return {'genres': []}
                    e = entries[int(path.split('/')[1])]
                    last = e['item'].get('last_episode') or e['item'].get('resume_episode') or [1, 1]
                    return {**fixture, 'id': e['id'], 'name': e['title'],
                            'seasons': [{'season_number': last[0], 'episode_count': last[1] + 1}],
                            'last_episode_to_air': {'season_number': last[0], 'episode_number': last[1] + 1,
                                                    'air_date': '2026-10-10'}}
                with patch.object(s, 'tmdb', side_effect=details):
                    rows = s.episodes_feed(lang)
            else:
                rows = {'tv': [x for _, _, x in s.feeling_pool(s.get_model(False), s._profiles['household'],
                                                             xs, lang, extra=[seed] if seed else [])]}
        s._feeds[(cid, lang)] = {'metas': rows, 'names': names}


def row_metrics(server, row):
    """Diagnostics use the full served row as their local pool; dark means a strongest-three x tag."""
    seeds = [e['key'] for e in server.person_entries(server._profiles['household'], server.PEOPLE[0])
             if e.get('explicit') and e['weight'] > 0]
    scores = {x['id']: server.seed_knn(seeds, x['id'], 1) for x in row['metas']}
    ordered = sorted(v for v in scores.values() if v is not None)
    # The before server predates pool_pct; retain the same averaged rank convention.
    pct = {k: (bisect.bisect_left(ordered, v) + bisect.bisect_right(ordered, v) - 1) / (2 * (len(ordered)-1))
           if v is not None and len(ordered) > 1 else .5 if v is not None else 0 for k, v in scores.items()}
    dark, dread = [], []
    for x in row['metas']:
        tags = server.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv').get('x') or {}
        top = sorted(tags, key=lambda t: (-tags[t], t))[:3]
        dark.append(bool({'dark', 'grim', 'bleak'} & set(top)))
        dread.append('dread' in top)
    return {'ana_seed_pct': pct, 'dark_first10': sum(dark[:10]), 'first10_n': len(dark[:10]),
            'dark_all': sum(dark), 'all_n': len(dark), 'dread_first10': sum(dread[:10]), 'dread_all': sum(dread)}


def because_distribution(server, lang='de-DE'):
    seed = server.row_seed(rid('because', server.CONFIG), lang)
    pool = {x['id']: x for x in server.row_pool(lang)}
    values = sorted(server.seed_similarity(seed, k) for k in pool)
    quantiles = {str(p): round(values[int((len(values)-1)*p)], 3) for p in (0, .1, .25, .5, .75, .9, 1)} if values else {}
    marked = []
    for key, name in BECAUSE_MARKS.items():
        meta = pool.get(key) or {'id': key, 'name': name, 'type': 'movie'}
        marked.append({'name': name, 'score': round(server.seed_similarity(seed, key), 3), 'in_pool': key in pool,
                       'tone_ok': server.tone_ok(meta, seed, animation=True),
                       'year_ok': (server.frec(seed, server.profile_kind(seed)).get('y') or '1980') < '1980' or
                                  (server.row_facts(meta).get('date') or '1980')[:4] >= '1980',
                       'passes': server.because_ok(meta, seed)})
    return {'seed': seed, 'n': len(pool), 'cutoff': server.BECAUSE_CUTOFF,
            'gate_scorer': 'seed_closeness' if hasattr(server, 'seed_closeness') else 'seed_similarity',
            'quantiles': quantiles, 'marked': marked,
            'scores': sorted(({'key': k, 'name': x['name'], 'score': round(server.seed_similarity(seed, k), 3),
                               'passes': server.because_ok(x, seed)} for k, x in pool.items()), key=lambda x: -x['score'])}


def week_distribution(server, lang='de-DE'):
    """Floor calibration over available non-animated candidates with positive absolute together scores."""
    above = {x['id'] for cid, typ in ((rid('episodes', server.CONFIG), 'series'), (rid('foryou', server.CONFIG), 'tv'))
             for x in server.row_metas(cid, typ, lang)}
    p, m = server._profiles['household'], server.get_model(False)
    people = [person for person in server.PEOPLE if m['nexp'][person] < 20]
    shared = [e['key'] for e in p['entries'] if 'together' in e.get('by', ()) and e['weight'] > 0
              and e['signal'] in ('watched', 'liked', 'loved')]
    seeds = {person: server.week_seeds(person) if hasattr(server, 'week_seeds') else
             list(dict.fromkeys([e['key'] for e in server.person_entries(p, person) if e.get('explicit') and e['weight'] > 0] + shared))
             for person in people}
    rows = []
    for x in server.row_pool(lang):
        kind = 'movie' if x['type'] == 'movie' else 'tv'
        joint = server.score_key(m, x['id'], 'together', kind)[0]
        if x['id'] in above or not server.row_eligible(x, rid('week', server.CONFIG), lang, p) or not server.tone_ok(x, animation=True) or (joint or 0) <= 0:
            continue
        rows.append({'key': x['id'], 'name': x['name'], 'hero': server.is_hero(x), 'together': round(joint, 3),
                     'nearest': {person: server.seed_knn(seeds[person], x['id'], 1) or 0 for person in people}})
    distributions = {}
    for person in people:
        values = sorted(x['nearest'][person] for x in rows)
        distributions[person] = {str(q): round(values[int((len(values)-1)*q)], 3) for q in (0, .1, .25, .5, .75, .9, 1)} if values else {}
    floors = {str(floor): sum(all(x['nearest'][person] >= floor for person in people) for x in rows)
              for floor in (.5, .55, .6, .62, .64, .65, .7)}
    curated = server.row_list(rid('week', server.CONFIG), 'tv', lang)
    missing = [x['name'] for x in curated if not server.frec(x['id'], 'movie' if x['type'] == 'movie' else 'tv') and
               server.femb(x['id'], 'movie' if x['type'] == 'movie' else 'tv') is None]
    return {'n': len(rows), 'floor': getattr(server, 'WEEK_SEED_FLOOR', None), 'penalty': getattr(server, 'WEEK_PENALTY', .25),
            'seeds': seeds, 'quantiles': distributions, 'floor_counts': floors, 'curated_without_data': missing, 'scores': rows}


def print_rows(server, rows, first):
    seed_rows = tuple(rid(name, server.CONFIG) for name in SEED_ROWS)
    metrics = {}
    for i, row in enumerate(rows):
        if i >= first and row['id'] not in (*seed_rows, rid('', server.CONFIG) + server.PEOPLE[0]):
            continue
        print(f"{row['name']} ({len(row['metas'])}): " + '; '.join(x['name'] for x in row['metas'][:10]))
        if row['id'] in (*seed_rows, rid('', server.CONFIG) + server.PEOPLE[0]):
            stats = metrics[row['id']] = row_metrics(server, row)
            print('  Ana seed_knn pct (served row): ' + '; '.join(
                f"{x['name']}={stats['ana_seed_pct'][x['id']]:.3f}" for x in row['metas'][:10]))
            if row['id'] == rid('', server.CONFIG) + server.PEOPLE[0]:
                print(f"  Dark strongest-three x: first10 {stats['dark_first10']}/{stats['first10_n']}; "
                      f"whole row {stats['dark_all']}/{stats['all_n']}")
                print(f"  Dread strongest-three x (separate proxy): first10 {stats['dread_first10']}/{stats['first10_n']}; "
                      f"whole row {stats['dread_all']}/{stats['all_n']}")
    return metrics


def quality_report(snapshot, person, k=10):
    """Count final slots and leave-one-love-out discovery recovery using production serving."""
    if k < 1:
        raise ValueError('k must be positive')
    with redirect_stdout(io.StringIO()), Engine(snapshot) as engine:
        s = engine._server
        engine._person(person)
        entries = s.person_entries(engine._profile, person)
        feedback = s.active_feedback()
        negatives = {(e['key'], e['kind']) for e in entries
                     if explicit_negative(e, feedback.get((e['key'], person)))}
        loves = sorted({(e['key'], e['kind']) for e in entries if e['signal'] == 'loved' and not e.get('abandon')})
        def final_rows():
            # Empty requested rows still count against fill.
            with s.addon_scope(person), patch.object(s, 'get_model', return_value=engine._model):
                requests = [(cid, typ) for cid, types, _ in s.feed_rows(s.LANG, who=person)
                            for typ in (('tv',) if 'tv' in types else types)]
                return [(cid, s.row_metas(cid, typ, s.LANG, who=person)[:k]) for cid, typ in requests]
        rows = final_rows()
        returned = sum(len(row) for _, row in rows)
        leaked = sum((meta['id'], 'movie' if meta['type'] == 'movie' else 'tv') in negatives for _, row in rows for meta in row)
        recovered = 0
        nondiscovery = {s.rid(name) for name in ('episodes', 'catchup', 'upcoming', 'nextseason', 'rewatch')}
        for key, kind in loves:
            with heldout(engine, {key}):
                recovered += any(meta['id'] == key and ('movie' if meta['type'] == 'movie' else 'tv') == kind
                                 for cid, row in final_rows() if cid not in nondiscovery for meta in row)
        return {**engine.provenance(), 'person': person, 'k': k, 'curation': 'fixed-candidate-pools',
                'rows': len(rows), 'fill': {'returned': returned, 'requested': len(rows)*k},
                'negative_leakage': {'slots': leaked, 'returned': returned, 'known_negatives': len(negatives)},
                'heldout': {'n': len(loves), 'recovered': recovered, 'recall_at_k': recovered/len(loves) if loves else None}}


def quality_targets(report):
    return [{'target': 'raw evidence for current rules', 'value': report['evidence'], 'pass': report['evidence'] == 'raw-evidence'},
            {'target': 'every requested slot filled', 'value': report['fill'],
             'pass': report['fill']['requested'] > 0 and report['fill']['returned'] == report['fill']['requested']},
            {'target': 'no known negative leakage', 'value': report['negative_leakage'],
             'pass': report['negative_leakage']['known_negatives'] > 0 and report['negative_leakage']['slots'] == 0},
            {'target': 'held-out love recovered', 'value': report['heldout'],
             'pass': report['heldout']['n'] > 0 and report['heldout']['recovered'] > 0}]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default=SNAPSHOT)
    parser.add_argument('--order', type=Path, help='player order file; otherwise SKALD_ROW_ORDER or the server display order')
    parser.add_argument('--before', type=Path)
    parser.add_argument('--day', default='2026-10-11')
    parser.add_argument('--first', type=int, default=8)
    parser.add_argument('--json', nargs='?', const=Path('-'), type=Path, help='optional report artifact, or stdout without a path')
    parser.add_argument('--quality', action='store_true', help='count final rows and held-out discovery recovery')
    parser.add_argument('--person')
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--check-targets', action='store_true')
    parser.add_argument('--stub-tmdb', action='store_true', help='also replay synthetic refill fixtures')
    args = parser.parse_args(argv)
    if not args.snapshot:
        parser.error('--snapshot is required')
    if args.quality:
        if not args.person or args.k < 1:
            parser.error('--quality needs --person and positive --k')
        report = quality_report(args.snapshot, args.person, args.k)
        report['targets'] = quality_targets(report) if args.check_targets else []
        text = json.dumps(report, ensure_ascii=False)
        if args.json and str(args.json) != '-':
            args.json.write_text(text + '\n')
        else:
            print(text if args.json else '\n'.join(f'{key}: {value}' for key, value in report.items()))
        return int(args.check_targets and not all(t['pass'] for t in report['targets']))
    report = {'evidence': 'cached-profile'}
    print('Evidence: cached-profile')
    for label, source in ([('before', args.before)] if args.before else []) + [('after', ROOT / 'server.py')]:
        with home_server(args.snapshot, source, args.day) as server:
            rows = display_rows(server, order=args.order)
            report[label] = rows
            print(label.upper())
            report[label + '_metrics'] = print_rows(server, rows, args.first)
            print('Below 12: ' + ', '.join(f"{r['id']}:{r['type']}={len(r['metas'])}" for r in rows if len(r['metas']) < 12))
            distribution = report[label + '_because_similarity'] = because_distribution(server)
            print('Because snapshot pool: ' + json.dumps({k: v for k, v in distribution.items() if k != 'scores'}, ensure_ascii=False))
            week = report[label + '_week_floor'] = week_distribution(server)
            print('This Week floor pool: ' + json.dumps({k: v for k, v in week.items() if k != 'scores'}, ensure_ascii=False))
            if args.stub_tmdb:
                stub_refills(server)
                report['stub_' + label] = display_rows(server, order=args.order)
                if label == 'after':
                    report['stub'] = report['stub_after']
                print('Thin source fixtures (offline -> stub): ' + ', '.join(
                    f"{r['id']}:{r['type']}={len(r['metas'])}->{len(stub['metas'])}"
                    for r, stub in zip(rows, report['stub_' + label]) if len(r['metas']) < 12))
                print('STUB ' + label.upper())
                report['stub_' + label + '_metrics'] = print_rows(server, report['stub_' + label], 0)
                week = report['stub_' + label + '_week_floor'] = week_distribution(server)
                print('Stub This Week floor pool: ' + json.dumps({k: v for k, v in week.items() if k != 'scores'}, ensure_ascii=False))
    if args.before:
        passes = {x['key']: x['passes'] for x in report['after_because_similarity']['scores']}
        before = next(r for r in report['before'] if r['id'] == rid('because', server.CONFIG))
        dropped = report['because_dropped'] = [x['name'] for x in before['metas'] if not passes.get(x['id'], False)]
        print('Because anchor drops from the old row: ' + '; '.join(dropped))
    if args.json:
        if str(args.json) == '-':
            print(json.dumps(report, ensure_ascii=False))
        else:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
