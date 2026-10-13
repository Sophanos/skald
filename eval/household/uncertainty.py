"""Opt-in paired weekly uncertainty; no automatic execution or I/O.

analyze(weeks, metric) takes an ordered, complete calendar frame of week dicts:
  {week: unique hashable label, mature: bool, targets: [target, ...],
   cells: optional iterable of (scope, row) keys, ...opaque caller fields}.
Each target is {id: str, franchise: str or None,
               values: {(scope: str, row: str): (A: float, B: float)}, ...}.
Use empty targets for zero-target calendar weeks. Declare empty rows in cells.
Only mature weeks enter the frame; callers must include every calendar week.
Repeated target ids count once for support. Scope can identify a member.
metric(list_of_weeks) returns (A, B), the FULL macro metric recomputed from
all scopes/rows, or None if undefined. It must be pure, deterministic and
handle repeated and empty weeks. This module does not define the macro metric.
Franchise exclusion filters targets, retaining calendar weeks and other fields.

stationary_bootstrap yields lists of original, intact weekly vectors, jointly
resampling A/B and all scopes/rows with circular contiguous geometric blocks.
Defaults from protocol: 5,000 replicates, seed 7, expected block length 2; analyze also checks
1 and 4. ci95 is the interpolated paired percentile interval for B minus A.
Headline floors: 12 mature weeks, 8 target weeks, 30 distinct discovery targets.
The same floors flag sparse scopes/rows; they receive support, never intervals.
These are abstention rules, not power calculations or finite-sample guarantees.
Stationary-bootstrap validity is asymptotic under weak dependence/stationarity;
12 weeks do not establish those assumptions (Politis/Romano, Statistica Sinica
1994, https://www3.stat.sinica.edu.tw/statistica/j4n2/j4n25/j4n25.htm).

Returns {status, delta, ci95, support, scopes, rows, sensitivity,
         leave_one_week, leave_one_franchise}. Status: INSUFFICIENT,
INCONCLUSIVE, B_BETTER, A_BETTER or NO_CLEAR_DIFFERENCE. Support dicts contain
weeks, target_weeks, targets, status (SUPPORTED/INSUFFICIENT). sensitivity maps
block length to {ci95, verdict}; missing intervals mean undefined replicates.
INCONCLUSIVE means undefined metrics, changed verdict, or an interval endpoint
shift greater than max(0.01, half the length-2 interval width). Scores should
use [0,1] units for this absolute sensitivity threshold. Influence lists hold
{week/franchise, delta, shift}; shift is excluded delta minus full delta.
Influence is descriptive, without confidence claims or repeated support gates.
"""
import math
import random

from . import protocol

def stationary_bootstrap(weeks, block_length=protocol.BOOTSTRAP_BLOCK_LENGTH, *,
                         replicates=protocol.BOOTSTRAP_REPLICATES, seed=protocol.BOOTSTRAP_SEED):
    if block_length < 1 or not math.isfinite(block_length) or replicates < 1:
        raise ValueError('positive replicate count and finite block length >= 1 required')
    if not weeks:
        raise ValueError('nonempty calendar frame required')
    rng = random.Random(seed)
    n = len(weeks)
    for _ in range(replicates):
        index = rng.randrange(n)
        draw = [weeks[index]]
        for _ in range(n - 1):
            index = rng.randrange(n) if rng.random() < 1 / block_length else (index + 1) % n
            draw.append(weeks[index])
        yield draw


def _support(weeks, key=None, scope=False):
    ids, bearing = set(), 0
    for week in weeks:
        selected = [t for t in week['targets'] if key is None or
                    any((cell[0] if scope else cell) == key for cell in t['values'])]
        bearing += bool(selected)
        ids.update(t['id'] for t in selected)
    supported = (len(weeks) >= protocol.MIN_MATURE_WEEKS
                 and bearing >= protocol.MIN_TARGET_WEEKS
                 and len(ids) >= protocol.MIN_DISCOVERY_TARGETS)
    return {'weeks': len(weeks), 'target_weeks': bearing, 'targets': len(ids),
            'status': 'SUPPORTED' if supported else 'INSUFFICIENT'}


def _difference(metric, weeks):
    pair = metric(weeks)
    if pair is None or any(v is None or not math.isfinite(v) for v in pair):
        return None
    delta = pair[1] - pair[0]
    return delta if math.isfinite(delta) else None


def _interval(values):
    if any(v is None for v in values):
        return None
    values.sort()
    def percentile(p):
        index = (len(values) - 1) * p
        low = int(index)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (index - low)
    tail = (1 - protocol.CONFIDENCE_LEVEL) / 2
    return percentile(tail), percentile(1 - tail)


def _verdict(interval):
    if interval is None:
        return 'INCONCLUSIVE'
    if interval[0] > 0:
        return 'B_BETTER'
    if interval[1] < 0:
        return 'A_BETTER'
    return 'NO_CLEAR_DIFFERENCE'


def analyze(weeks, metric):
    weeks = [w for w in weeks if w['mature']]
    if len({w['week'] for w in weeks}) != len(weeks):
        raise ValueError('duplicate calendar week')
    cells = {cell for w in weeks for cell in w.get('cells', ())}
    cells.update(cell for w in weeks for t in w['targets'] for cell in t['values'])
    support = _support(weeks)
    report = {'status': 'INSUFFICIENT', 'delta': None, 'ci95': None, 'support': support,
              'scopes': {s: _support(weeks, s, True) for s in sorted({c[0] for c in cells})},
              'rows': {c: _support(weeks, c) for c in sorted(cells)},
              'sensitivity': {}, 'leave_one_week': [], 'leave_one_franchise': []}
    if support['status'] == 'INSUFFICIENT':
        return report
    delta = report['delta'] = _difference(metric, weeks)
    if delta is None:
        report['status'] = 'INCONCLUSIVE'
        return report
    for length in (protocol.BOOTSTRAP_BLOCK_LENGTH, *protocol.SENSITIVITY_BLOCK_LENGTHS):
        interval = _interval([_difference(metric, draw)
                              for draw in stationary_bootstrap(weeks, length)])
        report['sensitivity'][length] = {'ci95': interval, 'verdict': _verdict(interval)}
    base = report['sensitivity'][protocol.BOOTSTRAP_BLOCK_LENGTH]
    report['ci95'], report['status'] = base['ci95'], base['verdict']
    if base['ci95'] is not None:
        tolerance = max(protocol.SENSITIVITY_TOLERANCE, (base['ci95'][1] - base['ci95'][0]) / 2)
        for check in report['sensitivity'].values():
            if check['verdict'] != base['verdict'] or check['ci95'] is None or any(
                    abs(a - b) > tolerance for a, b in zip(check['ci95'], base['ci95'])):
                report['status'] = 'INCONCLUSIVE'
    def influence(field, label, reduced):
        value = _difference(metric, reduced)
        return {field: label, 'delta': value,
                'shift': None if value is None else value - delta}
    report['leave_one_week'] = [influence('week', w['week'], weeks[:i] + weeks[i + 1:])
                                for i, w in enumerate(weeks)]
    franchises = sorted({t['franchise'] for w in weeks for t in w['targets']
                         if t['franchise'] is not None})
    report['leave_one_franchise'] = [influence('franchise', f, [
        {**w, 'targets': [t for t in w['targets'] if t['franchise'] != f]}
        for w in weeks]) for f in franchises]
    return report
