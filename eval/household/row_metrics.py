"""Opt-in, in-memory row scorecards. No release gate or production integration.

evaluate_rows(scope, week, targets, row_specs, rankings, *, catalog, origin,
served=None, prefix=(), negatives=None, unsafe_titles=None, budget=Budget()).
Scope, week, targets and rankings follow eval.household.metrics. Title ids are
opaque strings. catalog maps title ids to genre sequences. origin is finite
UTC seconds; served is complete prefix exposure evidence [{title, at}], where
at is finite UTC seconds. The preceding window is [origin-28 days, origin).
None evidence means NA; an empty sequence means known empty evidence. Callers
freeze inputs at cutoff; repeat/continuation targets are already timed/eligible.
Specs are the complete frozen ordered manifest, including hidden/empty rows:
{id, type}, type personal/lane/top10/together/rewatch/continuation. Optional
candidates is the full candidate pool; eligible is prior-play eligibility.
Lane specs require definition (nonempty JSON dict, frozen semantics) and
targets (context-conditional reference ids); optional theme_titles is the
frozen theme membership. Lane identity hashes definition, never display slot.
Top10 specs optionally supply chart_available (bool), historical_chart (ordered
ids at cutoff), rank_labels (one historical rank per displayed card).
Together optionally supplies member_targets ({member: reference ids}) and
vetoes ({member: explicit veto ids}). Only verified-group scopes earn group
recovery. Balance is min/max member recall (all zero: 0); satisfaction stays NA.

Returns {scope, week, tasks, rows, summary}. tasks reuse wave-1 Home Recovery;
repeat/continuation rows cannot enter discovery. rows keyed by semantic lane
identity or manifest id include quality (wave-1 Hit/nDCG), @5 sensitivity for
personal rows, candidate recall, leakage, unique fill, genre diversity,
calibration_jsd and specialized contract checks. Suppression is empty-row rate;
incremental hits exclude cards in all preceding rows. Chart fidelity is the
fraction of historical top-ten positions reproduced, including missing cards.
summary retains all target-bearing rows in mean_hit, including suppressed rows;
catalog coverage uses distinct displayed catalog ids / catalog size; cross-row
duplicates count excess row memberships, ignoring within-row repeats. Novelty
is the fraction of distinct displayed ids absent from the prior exposure window.
Diagnostics use all capped rows. calibration(prefix, recommendations, catalog)
returns base-2 Jensen-Shannon divergence in [0,1], or NA for missing/empty genres.
Each title contributes one unit fractionally across its genres. Diversity is mean
pairwise genre Jaccard distance. Calibration is diagnostic, not an objective;
lane interpretation is context-conditional. Historical proportions need not
describe tonight's mixture. Match percentages are not established as calibrated
probabilities. All operations are deterministic and pure.
"""
from collections import Counter
from itertools import combinations
from math import isfinite, log2
from statistics import mean

from eval.household.metrics import Budget, evaluate_week, row_metrics
from eval.household.protocol import protocol_id


ROW_CONTRACTS = {
    'personal': ('Hit/nDCG@12; @5 sensitivity', 'recall, negatives, diversity, fill'),
    'lane': ('conditional Hit/nDCG', 'theme, suppression, incremental hits'),
    'top10': ('historical chart fidelity; secondary Hit/nDCG@10', 'source, labels, safety'),
    'together': ('verified group-choice recovery', 'balance, unknown satisfaction, vetoes'),
    'rewatch': ('timed repeat recovery', 'prior play, separate discovery'),
    'continuation': ('timed continuation recovery', 'prior play, separate discovery'),
}


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _genres(titles, catalog):
    genres = [set(catalog.get(title, ())) for title in titles]
    return genres if genres and all(genres) else None


def calibration(prefix, recommendations, catalog):
    distributions = []
    for titles in (prefix, recommendations):
        genres = _genres(titles, catalog)
        if genres is None:
            return None
        counts = Counter()
        for group in genres:
            for genre in sorted(group):
                counts[genre] += 1 / (len(group) * len(genres))
        distributions.append(counts)
    p, q = distributions
    divergence = 0.0
    for genre in sorted(p.keys() | q.keys()):
        midpoint = (p[genre] + q[genre]) / 2
        for probability in (p[genre], q[genre]):
            if probability:
                divergence += probability * log2(probability / midpoint) / 2
    return min(1.0, max(0.0, divergence))


def _fraction(cards, evidence):
    return _ratio(len(set(cards) & set(evidence)), len(set(cards))) if evidence is not None else None


def evaluate_rows(scope, week, targets, row_specs, rankings, *, catalog, origin,
                  served=None, prefix=(), negatives=None, unsafe_titles=None,
                  budget=Budget()):
    if isinstance(origin, bool) or not isinstance(origin, (int, float)) or not isfinite(origin):
        raise ValueError('origin must be finite UTC seconds')
    specs, adapted, home_rankings = list(row_specs), [], dict(rankings)
    for spec in specs:
        kind = spec['type']
        if kind not in ROW_CONTRACTS:
            raise ValueError('unknown row type')
        adapted.append({'id': spec['id'], 'task': kind if kind in
                        ('rewatch', 'continuation') else 'discovery', 'top10': kind == 'top10'})
        if kind == 'together' and scope[0] != 'verified-group':
            home_rankings[spec['id']] = []
    home = evaluate_week(scope, week, targets, adapted, home_rankings, budget)
    rows, above, memberships = {}, set(), 0
    for spec, task_spec in zip(specs, adapted):
        kind, row_id = spec['type'], spec['id']
        identity = row_id
        if kind == 'lane':
            if not isinstance(spec.get('definition'), dict) or not spec['definition'] or 'targets' not in spec:
                raise ValueError('lane requires frozen definition and conditional targets')
            identity = 'lane:' + protocol_id(spec['definition'])
        if identity in rows:
            raise ValueError('semantic row identities must be unique')
        k = home['rows'][row_id]['k']
        cards = list(rankings.get(row_id, ()))[:k]
        reference = set(spec['targets'] if kind == 'lane' else targets.get(task_spec['task'], ()))
        if kind == 'together' and scope[0] != 'verified-group':
            reference = set()
        unique, candidates = set(cards), spec.get('candidates')
        genres = _genres(cards, catalog)
        distances = [1 - len(a & b) / len(a | b) for a, b in combinations(genres or (), 2)]
        row = {'quality': row_metrics(cards, reference, k),
               'candidate_recall': _ratio(len(reference & set(candidates)), len(reference))
               if candidates is not None else None,
               'negative_leakage': _fraction(cards, negatives), 'fill': len(unique) / k,
               'diversity': mean(distances) if distances else None,
               'calibration_jsd': calibration(prefix, cards, catalog),
               'calibration_interpretation': 'context_conditional' if kind == 'lane' else 'diagnostic',
               'suppression_rate': int(not cards),
               'incremental_hits': len((unique & reference) - above)}
        if kind == 'personal':
            row['sensitivity_at_5'] = row_metrics(cards, reference, min(5, k))
        elif kind == 'lane':
            row['theme_fidelity'] = _fraction(cards, spec.get('theme_titles'))
        elif kind == 'top10':
            available = spec.get('chart_available', False)
            if type(available) is not bool:
                raise ValueError('chart availability must be boolean')
            chart = list(spec.get('historical_chart', ()))[:10] if available else []
            if len(set(chart)) != len(chart):
                raise ValueError('historical chart titles must be distinct')
            row['historical_source_available'] = available
            row['chart_fidelity'] = _ratio(sum(a == b for a, b in zip(cards, chart)), len(chart))
            labels = spec.get('rank_labels')
            ranks = {title: rank for rank, title in enumerate(chart, 1)}
            row['rank_labels_consistent'] = (len(labels) == len(cards) and all(
                title in ranks and type(label) is int and label == ranks[title]
                for title, label in zip(cards, labels))) if chart and labels is not None else None
            row['unsafe_leakage'] = _fraction(cards, unsafe_titles)
        elif kind == 'together':
            members = spec.get('member_targets', {})
            rates = [_fraction(titles, unique) for titles in members.values()]
            row['member_balance'] = None
            if rates and all(rate is not None for rate in rates):
                row['member_balance'] = min(rates) / max(rates) if max(rates) else 0
            row['unknown_satisfaction'] = None
            vetoes = spec.get('vetoes')
            row['veto_leakage'] = _fraction(cards, set().union(*map(set, vetoes.values()))
                                          if vetoes is not None else None)
        else:
            row['prior_play_eligibility'] = _fraction(cards, spec.get('eligible'))
        rows[identity] = row
        memberships += len(unique)
        above.update(unique)
    recent = None
    if served is not None:
        recent = set()
        for exposure in served:
            at = exposure['at']
            if isinstance(at, bool) or not isinstance(at, (int, float)) or not isfinite(at):
                raise ValueError('exposure times must be finite UTC seconds')
            if origin - 28 * 86400 <= at < origin:
                recent.add(exposure['title'])
    hits = [row['quality']['hit_at_k'] for row in rows.values()
            if row['quality']['hit_at_k'] is not None]
    summary = {'target_rows': len(hits), 'mean_hit': mean(hits) if hits else None,
               'catalog_coverage': _ratio(len(above & catalog.keys()), len(catalog)),
               'cross_row_duplicates': memberships - len(above),
               'not_served_28d': _ratio(len(above - recent), len(above)) if recent is not None else None,
               'suppression_rate': _ratio(sum(row['suppression_rate'] for row in rows.values()), len(rows))}
    return {'scope': scope, 'week': week, 'tasks': home['tasks'], 'rows': rows, 'summary': summary}
