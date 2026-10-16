"""Offline separation and context for titles a member stopped watching."""
import argparse
from collections import Counter
from contextlib import redirect_stdout
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import statistics
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skald.engine import Engine, denied


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def median(values, n):
    known = [v for v in values if v is not None]
    return {'median': statistics.median(known) if known else None, 'unknown': n-len(known)}


def count_metrics(values):
    histogram = Counter(v for v in values if v is not None)
    n, scored = len(values), sum(histogram.values())
    high = sum(count for index, count in histogram.items() if index >= 90)
    middle, cumulative, positions = [], 0, ((scored-1)//2, scored//2)
    for index, count in sorted(histogram.items()):
        middle.extend(index for pos in positions if cumulative <= pos < cumulative + count)
        cumulative += count
    med = sum(middle)/2 if scored else None
    interval = None
    if scored:
        z = 1.959963984540054
        share = high/scored
        center = (share + z*z/(2*scored))/(1 + z*z/scored)
        radius = z*math.sqrt(share*(1-share)/scored + z*z/(4*scored*scored))/(1 + z*z/scored)
        interval = [max(0., center-radius), min(1., center+radius)]
    return {'n': n, 'unscored': n-scored, 'coverage': scored/n if n else None,
            'histogram': {str(index): count for index, count in sorted(histogram.items())},
            'median_index': med, 'ge90': high, 'ge90_share': high/scored if scored else None,
            'ge90_bounds': [high/n, (high+n-scored)/n] if n else None, 'wilson95': interval,
            # Retain the owner's original table and target field names.
            'median_percentile': med, 'unknown': n-scored,
            'lt50': sum(count for index, count in histogram.items() if index < 50)}


def pair_accuracy(positives, negatives):
    counts = Counter('W' if p > q else 'L' if p < q else 'T'
                     for p in positives for q in negatives if p is not None and q is not None)
    scored, n = sum(counts.values()), len(positives)*len(negatives)
    return {**{label: counts[label] for label in ('W', 'T', 'L')}, 'n': n, 'unscored': n-scored,
            'accuracy': (counts['W'] + counts['T']/2)/scored if scored else None}


def explicit_negative(e, feedback=None):
    stopped = e['signal'] == 'abandoned' or bool(e.get('abandon'))
    deliberate_drop = feedback and feedback['kind'] == e['kind'] and feedback['action'] == 'abandon'
    return bool(deliberate_drop) or e['signal'] == 'disliked' or stopped and (
        (e.get('item') or {}).get('dropped') is True or e.get('who') == 'import' or e.get('wsrc') == 'import' or
        not e.get('abandon') and (e.get('who') in ('remote', 'told') or e.get('wsrc') in ('remote', 'interview')))


def cohort(e, feedback=None):
    if explicit_negative(e, feedback):
        return 'explicit_negative'
    if e.get('abandon') or e['signal'] == 'abandoned':
        return 'stops_' + (e.get('abandon') or 'pending')
    return {'loved': 'love', 'liked': 'like', 'watched': 'watched'}.get(e['signal'])


def identity(e):
    return hashlib.sha256(f"{e['key']}@{e['kind']}".encode()).hexdigest()


def context(server, entries, library):
    observations = []
    for e in entries:
        rec, item = server.frec(e['key'], e['kind']), e.get('item') or {}
        state = library.get((e['key'], e['kind'])) or library.get((item.get('id'), e['kind']), {})
        runtime = rec.get('runtime')
        runtime = runtime if numeric(runtime) else None
        duration, offset = state.get('duration'), state.get('timeOffset')
        progress = item.get('progress')
        minute = offset/60000 if isinstance(offset, (int, float)) and offset >= 0 else (
            progress*runtime if runtime and isinstance(progress, (int, float)) and 0 <= progress <= 1 else None)
        at = item.get('at')
        at = at/1000 if numeric(at) else server._connector_time(state.get('lastWatched'))
        day = server.local_now(at) if at else None
        try:
            release = datetime.date.fromisoformat(rec.get('dr') or '')
        except (ValueError, TypeError):
            release = None
        observations.append({'runtime': runtime, 'stop_minute': minute,
            'day_part': server.day_part(day) if day else None,
            'year_season': server.year_season(day) if day else None,
            'fri_sun': day.weekday() >= 4 if day else None,
            'short_stream': duration/60000 < runtime/2 if runtime and numeric(duration) else None,
            'before_digital_release': day.date() < release if day and release else None})
    out = {'n': len(entries)}
    for field in ('runtime', 'stop_minute'):
        out[field] = median([o[field] for o in observations], len(entries))
    for field in ('day_part', 'year_season'):
        counts = Counter(o[field] or 'unknown' for o in observations)
        out[field] = {**counts, 'unknown': counts['unknown']}
    for field in ('fri_sun', 'short_stream', 'before_digital_release'):
        out[field] = {label: sum(o[field] is value for o in observations)
                      for label, value in (('yes', True), ('no', False), ('unknown', None))}
    return out


def stop_report(snapshot, person, baseline=None):
    # Engine owns the copied DATA, disabled enrichment and pool shutdown.
    with patch('urllib.request.urlopen', denied), redirect_stdout(io.StringIO()), Engine(snapshot) as engine:
        s, model = engine._server, engine._model
        person = next((p for p in s.PEOPLE if p.casefold() == person.casefold()), person)
        engine._person(person)
        entries = s.person_entries(engine._profile, person)
        feedback = s.active_feedback()
        for e in entries:
            own = (e.get('opinions') or {}).get(person) or e.get('shared')
            if own:
                for field in ('abandon', 'abandon_reason'):
                    e[field] = own.get(field)
        groups = {name: [] for name in ('love', 'like', 'watched', 'explicit_negative',
                  'stops_related', 'stops_unsure', 'stops_far', 'stops_early', 'stops_pending')}
        membership, target_membership, indices, scores = {}, {}, {}, {}
        for e in entries:
            group = cohort(e, feedback.get((e['key'], person)))
            token = identity(e)
            membership[token] = group or 'other'
            target_membership[token] = 'stops_' + e['abandon'] if e.get('abandon') else membership[token]
            score, fids = s.person_score(model, e['key'], person, e['kind'])
            scores[token] = score
            indices[token] = s.confidence(model, e['key'], score, fids, person)[1]
            if group in groups:
                groups[group].append(token)
        cohorts = {group: count_metrics([indices[token] for token in tokens]) for group, tokens in groups.items()}
        def separation(members):
            names = {'loved': ('love',), 'stops_unsure_far': ('stops_unsure', 'stops_far'),
                     'stops_related': ('stops_related',), 'watched': ('watched',), 'dislikes': ('explicit_negative',)}
            return {name: count_metrics([indices.get(token) for token, group in members.items() if group in selected])
                    for name, selected in names.items()}
        fixed = baseline.get('fixed_membership', baseline.get('cohort_membership')) if baseline else target_membership
        if fixed is None or baseline and baseline.get('person') != person:
            raise ValueError('baseline needs cohort membership for the same person')
        # Frozen targets score the same titles even when their stop reading migrates.
        for key, rec in engine._index.items():
            token = identity({'key': key, 'kind': rec['kind']})
            if token in fixed and token not in indices:
                value, fids = s.person_score(model, key, person, rec['kind'])
                indices[token] = s.confidence(model, key, value, fids, person)[1]
        migration = Counter((baseline['cohort_membership'].get(token, 'absent'), membership.get(token, 'absent'))
                            for token in baseline['cohort_membership'].keys() | membership.keys()) if baseline else Counter()
        pairs = pair_accuracy([scores[token] for group in ('love', 'like', 'watched') for token in groups[group]],
                              [scores[token] for token in groups['explicit_negative']])
        name = f'stremio-{person}.history.json'
        source, copied = Path(snapshot)/name, engine._work/name
        if source.is_file() and not copied.exists():
            shutil.copyfile(source, copied)
        library = {}
        raw = json.loads(copied.read_text()) if copied.exists() else {}
        for row in raw.get('library', {}).values():
            item = row.get('item') or {}
            kind = 'movie' if item.get('type') == 'movie' else 'tv'
            library[item.get('id'), kind] = (row.get('state') or {}).get('state') or {}
        films = [e for e in entries if e['kind'] == 'movie']
        return {**engine.provenance(), 'person': person, 'cohorts': cohorts, 'pairs': pairs,
                'separation': separation(target_membership), 'fixed_separation': separation(fixed),
                'cohort_membership': membership, 'fixed_membership': fixed,
                'cohort_migration': [{'from': old, 'to': new, 'n': n} for (old, new), n in sorted(migration.items())],
                'context': {
            'stops': context(s, [e for e in films if e.get('abandon')], library),
            'finished': context(s, [e for e in films if not e.get('abandon') and e['signal'] in ('watched', 'loved')], library)}}


def targets(report):
    loved, stops = (report.get('fixed_separation', report['separation'])[group] for group in ('loved', 'stops_unsure_far'))
    limit = round(stops['n']/6)
    complete = stops['n'] > 0 and stops['unknown'] == 0
    negative = report.get('cohorts', {}).get('explicit_negative', count_metrics([]))
    complete_negative = negative['n'] > 0 and negative['unscored'] == 0
    pairs = report.get('pairs', {})
    return [{'target': 'raw evidence for current rules', 'value': report.get('evidence'),
             'pass': report.get('evidence') == 'raw-evidence'},
            {'target': 'stops unsure+far median <= 75', 'value': stops['median_percentile'],
             'pass': complete and stops['median_percentile'] is not None and stops['median_percentile'] <= 75},
            {'target': f'stops unsure+far >= 90 <= {limit} of {stops["n"]}', 'value': stops['ge90'],
             'pass': complete and stops['ge90'] <= limit},
            {'target': 'loved median >= 90', 'value': loved['median_percentile'],
             'pass': loved['n'] > 0 and loved['unknown'] == 0 and loved['median_percentile'] is not None
                     and loved['median_percentile'] >= 90},
            {'target': 'explicit negative median <= 75', 'value': negative['median_index'],
             'pass': complete_negative and negative['median_index'] <= 75},
            {'target': 'explicit negative >= 90 share <= 1/6', 'value': negative['ge90_bounds'],
             'pass': complete_negative and negative['ge90_bounds'][1] <= 1/6},
            {'target': 'positive versus negative pair accuracy >= .75', 'value': pairs.get('accuracy'),
             'pass': pairs.get('n', 0) > 0 and pairs.get('unscored') == 0 and pairs.get('accuracy', 0) >= .75}]


def text_report(report):
    lines = [f'Separation: {report["person"]} ({report["evidence"]})',
             'group | n | unscored | coverage | median displayed index | >= 90 share | bounds | Wilson 95%']
    for group, row in report['cohorts'].items():
        lines.append(f'{group} | ' + ' | '.join(str(row[k]) for k in
            ('n', 'unscored', 'coverage', 'median_index', 'ge90_share', 'ge90_bounds', 'wilson95')))
    pairs = report['pairs']
    lines.append('Positive versus negative pairs: ' + (str(pairs) if pairs['accuracy'] is not None else 'N/A'))
    lines.extend(f'Cohort migration: {r["from"]} -> {r["to"]}: {r["n"]}' for r in report['cohort_migration'])
    lines.append('Film context (observations only; missing data is unknown)')
    for group, row in report['context'].items():
        lines.append(f'{group}: n {row["n"]}')
        for field, counts in row.items():
            if field != 'n':
                lines.append(f'  {field}: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
    lines.extend(f'{"PASS" if t["pass"] else "FAIL"}: {t["target"]} (value {t["value"]})'
                 for t in report.get('targets', []))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--person', required=True)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check-targets', action='store_true')
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args(argv)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    report = stop_report(args.snapshot, args.person, baseline) if baseline else stop_report(args.snapshot, args.person)
    if args.check_targets:
        report['targets'] = targets(report)
    print(json.dumps(report, ensure_ascii=False) if args.json else text_report(report))
    return int(args.check_targets and not all(t['pass'] for t in report['targets']))


if __name__ == '__main__':
    sys.exit(main())
