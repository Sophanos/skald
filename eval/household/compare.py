"""Opt-in local release gate; callable only, with no I/O or production hooks.

compare(a, b, inputs, evaluator, metric, *, guardrails, changed_components=(),
enabled=False) runs evaluator(build, shared_inputs) on detached copies. Builds
and inputs are JSON-compatible. inputs requires snapshot, protocol (the wave-1
manifest), eligibility, contexts, external_inputs and panel. panel is {kind:
'frozen'/'newly_matured'/'unused_household', inspections: nonnegative int}.
Only uninspected newly matured evidence or an unused household supports fresh
quality claims. Keep frozen panels for regression detection; record inspections
externally, never describe a reused panel as an untouched test.

Evaluator output: {evidence: protocol.result(...), checks: {privacy: bool,
safety: bool, veto: bool}, weeks: uncertainty.analyze calendar frames except
target.values maps (scope, row_family) to a single recovery score in [0,1]}.
Evidence provenance and pipeline coverage remain caller assertions. All target
and calendar metadata must match exactly. A runs twice before B is inspected.
metric(paired_weeks) is the same pure macro Home Recovery evaluator for all
runs and bootstrap draws; it returns (A, B) or None. On protected projections
it must recompute recovery from the remaining cells, including empty weeks.

guardrails is {members: {scope: margin}, rows: {(scope, row_family): margin}};
either map can be empty. Declare every protected scope and its tolerated decline
before evaluation, normally .05. Sparse or unresolved protection blocks approval.
Policy, builds and input hashes are checked after every evaluator/metric call.
Private input hashes stay local; this result is not a share/export document.

Returns {status, release_pass, fresh_quality, aa_identical, input_hash_before,
input_hash_after, home: uncertainty report, guardrails: nested reports,
failures: fixed reason codes}. Better requires L>.02; non-inferior L>-.01;
regression U<-.01. Statistical non-significance cannot prove non-inferiority.
Non-inferiority or a small gain provides no worthwhile-gain complexity claim.
Pairing and protected-cell projection adapt the existing wave-1 interfaces.
"""
from copy import deepcopy
from math import isfinite

from eval.household import protocol
from eval.household.uncertainty import analyze


def _pair(a, b):
    paired = deepcopy(a)
    for left, right in zip(paired, b, strict=True):
        if {k: v for k, v in left.items() if k != 'targets'} != {
                k: v for k, v in right.items() if k != 'targets'}:
            raise ValueError('calendar_changed')
        for x, y in zip(left['targets'], right['targets'], strict=True):
            if {k: v for k, v in x.items() if k != 'values'} != {
                    k: v for k, v in y.items() if k != 'values'} or x['values'].keys() != y['values'].keys():
                raise ValueError('targets_changed')
            for cell, value in x['values'].items():
                pair = value, y['values'][cell]
                if any(type(v) not in (int, float) or not isfinite(v) or not 0 <= v <= 1 for v in pair):
                    raise ValueError('invalid_recovery')
                x['values'][cell] = pair
    return paired


def _project(weeks, kind, key):
    weeks = deepcopy(weeks)
    for week in weeks:
        match = lambda cell: (cell[0] if kind == 'members' else cell) == key
        week['cells'] = [c for c in week.get('cells', ()) if match(c)]
        for target in week['targets']:
            target['values'] = {c: v for c, v in target['values'].items() if match(c)}
        week['targets'] = [t for t in week['targets'] if t['values']]
    return weeks


def _verdict(report, margin=.01, worthwhile=.02):
    if report['status'] == 'INSUFFICIENT':
        return 'INSUFFICIENT / UNSUPPORTED'
    interval = report['ci95']
    if report['status'] == 'INCONCLUSIVE' or interval is None:
        return 'INCONCLUSIVE'
    low, high = interval
    if not all(isfinite(v) for v in interval) or low > high:
        return 'INCONCLUSIVE'
    if worthwhile is not None and low > worthwhile:
        return 'BETTER'
    if low > -margin:
        return 'NON-INFERIOR'
    return 'REGRESSION' if high < -margin else 'INCONCLUSIVE'


def compare(a, b, inputs, evaluator, metric, *, guardrails,
            changed_components=(), enabled=False):
    if not {'snapshot', 'protocol', 'eligibility', 'contexts', 'external_inputs', 'panel'} <= inputs.keys():
        raise ValueError('shared inputs required')
    if set(guardrails) - {'members', 'rows'} or any(
            type(m) not in (int, float) or not isfinite(m) or not 0 <= m <= 1
            for values in guardrails.values() for m in values.values()):
        raise ValueError('predeclared guardrail margins required')
    frozen = {'inputs': inputs, 'builds': [a, b], 'guardrails': {
        k: list(v.items()) for k, v in guardrails.items()}, 'changed': list(changed_components)}
    before = protocol.protocol_id(frozen)
    policy, guards = deepcopy(inputs['protocol']), deepcopy(guardrails)
    out = dict(status='HARD FAIL', release_pass=False, fresh_quality=False,
               aa_identical=False, input_hash_before=before, input_hash_after=before,
               home=None, guardrails={}, failures=[])

    def integrity():
        out['input_hash_after'] = protocol.protocol_id({**frozen, 'guardrails': {
            k: list(v.items()) for k, v in guardrails.items()}})
        if out['input_hash_after'] != before:
            raise ValueError('input_integrity')

    def run(build):
        build, shared = deepcopy(build), deepcopy(inputs)
        digest = protocol.protocol_id([build, shared])
        result = deepcopy(evaluator(build, shared))
        integrity()
        if protocol.protocol_id([build, shared]) != digest:
            raise ValueError('input_integrity')
        return result

    def score(frame):
        original = deepcopy(frame)
        value = metric(frame)
        integrity()
        if original != frame:
            raise ValueError('metric_mutation')
        return value

    try:
        left, repeat = run(a), run(a)
        aa = _pair(left['weeks'], repeat['weeks'])
        aa_score = score(aa)
        if left != repeat or aa_score != score(aa) or (aa_score is not None and aa_score[0] != aa_score[1]):
            raise ValueError('repeatability')
        out['aa_identical'] = True
        right = run(b)
        weeks = _pair(left['weeks'], right['weeks'])
        if any(r['checks'].get(k) is not True for r in (left, repeat, right)
               for k in ('privacy', 'safety', 'veto')):
            raise ValueError('hard_checks')
        if any(r['evidence']['protocol_id'] != protocol.protocol_id(policy) for r in (left, right)):
            raise ValueError('protocol_changed')
        gate = protocol.compare(left['evidence'], right['evidence'],
                                changed_components=frozen['changed'], enabled=True)
        out['failures'] = gate['failures']
        if any(f != 'quality_not_passed' for f in gate['failures']):
            out['status'] = 'INSUFFICIENT / UNSUPPORTED'
            return out
        out['home'] = analyze(weeks, score)
        statuses = [_verdict(out['home'])]
        for kind, values in guards.items():
            out['guardrails'][kind] = {}
            for key, margin in values.items():
                report = analyze(_project(weeks, kind, key), score)
                report = {**report, 'status': _verdict(report, margin, None)}
                out['guardrails'][kind][key] = report
                statuses.append(report['status'])
        out['status'] = next((s for s in ('REGRESSION', 'INSUFFICIENT / UNSUPPORTED', 'INCONCLUSIVE')
                              if s in statuses), statuses[0])
        panel = inputs['panel']
        out['fresh_quality'] = (panel.get('kind') in ('newly_matured', 'unused_household')
                                and type(panel.get('inspections')) is int and panel['inspections'] == 0)
        out['release_pass'] = (enabled is True and gate['release_pass'] and out['fresh_quality']
                               and out['status'] in ('BETTER', 'NON-INFERIOR'))
        if not out['fresh_quality']:
            out['failures'] = out['failures'] + ['fresh_evidence_required']
        integrity()
    except Exception:
        out.update(status='HARD FAIL', release_pass=False, failures=['reproducibility_or_integrity'])
    return out
