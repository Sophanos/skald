"""Opt-in local development tuning; no execution, printing or I/O on import.

select_development(server, folds, evaluate, scopes, policy, enabled=False)
returns a frozen SCORE_BLEND tuple. server is an isolated offline production
server module, initially SCORE_BLEND=(0, 0, .3). Never pass a running service.
folds are splits.weekly_folds outputs; only development folds reach evaluate.
evaluate(fold) runs real Home paths under the temporarily applied SCORE_BLEND
and returns {weeks: [metrics.evaluate_week results], gate: protocol.result}.
weeks must cover exactly the registered scopes for that fold's origin.isoformat().
Keep eligibility, target sets, row manifests and all other inputs fixed in A/B.
gate quality uses the policy's effect_margins metrics; missing values abstain.
Home Recovery is recomputed by metrics.summarize, never supplied as a utility.
Comparability, coverage, quality_passed and the same metric margins guard each
development fold. Candidate Home Recovery cannot regress in any supported scope.
The registered grid varies only the actual score_key embedding coefficient;
zero does not disable seed-similarity or discovery embeddings elsewhere.
Require >.01 absolute macro recovery improvement, then prefer smaller weights
within .01 of the best. This is a development selection rule, not significance
or a release pass. No synthetic correctness count can improve the objective.

evaluate_outer(server, selected, folds, evaluate, enabled=False) returns a tuple
of {week, baseline, challenger} evaluation pairs, only for outer folds. Select
once beforehand; this function has no selection or grid leaderboard. Disabled
selection returns baseline; disabled outer evaluation returns (). Neither calls
evaluate. W2's exposed test remains DEVELOPMENT evidence, never a clean outer
test. evidence_role marks confirmation independent only when not used to select.

positive_play_decay(event, half_life_days=None) is an unregistered adapter,
returning a copied {action, weight, age_days, applied_decay, ...} event. For a
positive play only, divide the already-decayed weight by its recorded source
factor, then apply 90/180-day decay once and replace applied_decay. None keeps
the existing schedule. Never infer one source's factor from another's age.
Vetoes, opinions, memory and fatigue fields are unchanged. Production exposes
no unified play half-life parameter, so recency is NOT in the tuning grid.
"""
from copy import deepcopy
from math import isfinite
from unittest.mock import patch

from eval.household.metrics import summarize
from eval.household.protocol import compare, protocol_id
from eval.household.splits import select_parameters


BASELINE = (0, 0, .3)
DEVELOPMENT_GRID = (BASELINE, (.1, 0, .3), (.2, 0, .3))
MIN_GAIN = .01


def _evaluate(server, blend, fold, evaluate):
    with patch.object(server, 'SCORE_BLEND', blend):
        return deepcopy(evaluate(deepcopy(fold)))


def _recovery(report, fold, scopes):
    weeks = report['weeks']
    if (len(weeks) != len(scopes) or {w['scope'] for w in weeks} != set(scopes)
            or any(w['week'] != fold.origin.isoformat() for w in weeks)):
        raise ValueError('scope-week coverage changed')
    return summarize(weeks, scopes)['tasks']['discovery']


def _guarded(baseline, candidate, policy):
    a, b = baseline['gate'], candidate['gate']
    if a['protocol_id'] != protocol_id(policy):
        return False
    verdict = compare(a, b, changed_components=('score_blend',), enabled=True)
    if not verdict['comparable'] or b['quality_passed'] is not True:
        return False
    for metric, margin in policy['effect_margins'].items():
        values = a['quality'].get(metric), b['quality'].get(metric), margin
        if any(type(v) not in (int, float) or not isfinite(v) for v in values):
            return False
        if margin < 0 or values[1] < values[0] - margin:
            return False
    return True


def _no_drop(a, b):
    return a == b if a is None or b is None else b >= a


def select_development(server, folds, evaluate, scopes, policy, *, enabled=False):
    if enabled is not True:
        return BASELINE
    if server.SCORE_BLEND != BASELINE:
        raise ValueError('unexpected inspected baseline')
    development = [f for f in folds if f.phase == 'development']
    scopes = tuple(scopes)
    if not development or not scopes:
        raise ValueError('development folds and registered scopes required')
    reports = {blend: [_evaluate(server, blend, f, evaluate) for f in development]
               for blend in DEVELOPMENT_GRID}
    recoveries = {blend: [_recovery(r, f, scopes) for r, f in zip(rows, development)]
                  for blend, rows in reports.items()}
    totals = {b: summarize([w for r in rows for w in r['weeks']], scopes)
              ['tasks']['discovery']['score'] for b, rows in reports.items()}
    base = recoveries[BASELINE]
    if totals[BASELINE] is None:
        return BASELINE
    scores = {BASELINE: totals[BASELINE]}
    for blend in DEVELOPMENT_GRID[1:]:
        candidate = recoveries[blend]
        safe = all(_guarded(a, b, policy) for a, b in zip(reports[BASELINE], reports[blend]))
        safe &= all(_no_drop(a['scopes'][s]['score'], b['scopes'][s]['score'])
                    for a, b in zip(base, candidate) for s in scopes)
        if safe:
            scores[blend] = totals[blend]
    best = max(scores.values())
    if best - scores[BASELINE] <= MIN_GAIN + 1e-12:
        return BASELINE
    # Reuse the temporal selector; utilities are frozen before any outer call.
    candidates = [{'blend': b} for b in DEVELOPMENT_GRID if b in scores]
    chosen = select_parameters(development, candidates,
             lambda p, f: int(best - scores[p['blend']] <= MIN_GAIN + 1e-12))
    return chosen['blend']


def evaluate_outer(server, selected, folds, evaluate, *, enabled=False):
    if enabled is not True:
        return ()
    if selected not in DEVELOPMENT_GRID or server.SCORE_BLEND != BASELINE:
        raise ValueError('unregistered blend or unexpected baseline')
    pairs = []
    for fold in folds:
        if fold.phase == 'outer':
            baseline = _evaluate(server, BASELINE, fold, evaluate)
            challenger = (deepcopy(baseline) if selected == BASELINE else
                          _evaluate(server, selected, fold, evaluate))
            pairs.append({'week': fold.origin.isoformat(), 'baseline': baseline,
                          'challenger': challenger})
    return tuple(pairs)


def evidence_role(*, exposed_w2=False, used_for_selection=False):
    return 'development' if exposed_w2 or used_for_selection else 'independent_confirmation'


def positive_play_decay(event, *, half_life_days=None):
    result = deepcopy(event)
    if half_life_days is None or event['action'] != 'play' or event['weight'] <= 0:
        return result
    if half_life_days not in (90, 180):
        raise ValueError('unregistered play decay experiment')
    age, applied, weight = event.get('age_days'), event.get('applied_decay'), event['weight']
    if (any(type(v) not in (int, float) or not isfinite(v) for v in (age, applied, weight))
            or age < 0 or not 0 < applied <= 1):
        raise ValueError('known age and source decay factor required')
    factor = 2 ** (-age / half_life_days)
    result.update(weight=weight / applied * factor, applied_decay=factor)
    return result
