"""Opt-in, in-memory release metrics. None represents NA, never zero.

evaluate_week(scope, week, targets, row_specs, rankings, budget=Budget()):
  scope is (kind, opaque_id), kind individual/verified-group/shared-unknown.
  week is a string. Never expand group or unknown plays into individual scopes.
  targets maps task to title-id sequences: discovery, continuation, upcoming,
  rewatch. Discovery contains eligible next-week new-title playback targets.
  Eligibility and attribution are supplied by the caller, not inferred here.
  row_specs is the frozen ordered list of {id: str, task: str='discovery',
  top10: bool=False}; rankings maps those row ids to ranked title-id lists.
  Title ids are opaque strings; use the same reference targets and manifest
  for A and B. Missing rankings are empty and retain their manifest positions.
  Returns {scope, week, tasks: {task: {hits, targets, score}}, rows: {id:
  {k, hits, targets, hit_at_k, ndcg_at_k}}}. Targets are distinct title counts.
  Discovery score is Home Recovery: distinct recovered / distinct targets.
  Default budget: first six discovery rows, twelve cards each; Top 10 has at
  most ten. It is an evaluation budget, not actual viewport visibility.
  Other tasks are separate, using all their rows with the same card caps.
  Row reports include every manifest row, even outside the home footprint.

row_metrics(cards, targets, k=12) returns the row report above. Hit@k is binary
  any-target recovery. Binary nDCG uses sum(1/log2(rank+1)) and an ideal of
  min(k, distinct reference targets). Repeated cards consume ranks but gain
  only once. Absent candidates remain misses, including targets absent from
  the local pool. No-target rows are NA; missing target-bearing rows are zero.

summarize(weeks, scopes, *, resampled=False) takes evaluate_week results and preregistered scope
  tuples. Returns {tasks: {task: {score, scopes: {scope: {score, target_weeks,
  observed_weeks}}, missing_scopes}}, unregistered_scopes}. Average only
  target-bearing weeks per scope, then equally over registered scopes.
  Any unsupported registered scope makes the aggregate NA and is reported;
  unregistered scopes are reported, not folded into the headline.
  Duplicate scope-week observations are errors unless resampled=True, which
  counts identical bootstrap repetitions with multiplicity. Keep support for A/B.

Callers should rank the complete frozen eligible local pool where feasible.
No random negative sampling occurs here. Full-pool evaluation avoids sampling
distortion but does not remove exposure bias (arXiv:2508.05398).
"""
from dataclasses import dataclass
from math import log2
from statistics import mean

from . import protocol

TASKS = ('discovery', 'continuation', 'upcoming', 'rewatch')
SCOPE_KINDS = ('individual', 'verified-group', 'shared-unknown')


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError('metric budget must be a positive integer')


@dataclass(frozen=True)
class Budget:
    rows: int = protocol.HOME_ROWS
    cards: int = protocol.HOME_CARDS
    top10: int = protocol.TOP10_CAP

    def __post_init__(self):
        for value in (self.rows, self.cards, self.top10):
            _positive(value)
        if self.top10 > protocol.TOP10_CAP:
            raise ValueError('Top 10 budget cannot exceed ten')


def _scope(scope):
    if (not isinstance(scope, tuple) or len(scope) != 2
            or scope[0] not in SCOPE_KINDS or not isinstance(scope[1], str)):
        raise ValueError('scope must be a (kind, opaque_id) tuple')


def row_metrics(cards, targets, k=protocol.HOME_CARDS):
    _positive(k)
    reference, seen = set(targets), set()
    gain, hits = 0.0, 0
    for rank, title in enumerate(cards[:k], 1):
        if title in reference and title not in seen:
            hits += 1
            gain += 1 / log2(rank + 1)
        seen.add(title)
    ideal = sum(1 / log2(rank + 1) for rank in range(1, min(k, len(reference)) + 1))
    return {'k': k, 'hits': hits, 'targets': len(reference),
            'hit_at_k': int(hits > 0) if reference else None,
            'ndcg_at_k': gain / ideal if ideal else None}


def evaluate_week(scope, week, targets, row_specs, rankings, budget=Budget()):
    _scope(scope)
    if not isinstance(week, str) or set(targets) - set(TASKS):
        raise ValueError('week must be a string and targets must use known tasks')
    reference = {task: set(targets.get(task, ())) for task in TASKS}
    footprint = {task: set() for task in TASKS}
    rows, discovery_rows = {}, 0
    for spec in row_specs:
        row_id, task = spec['id'], spec.get('task', 'discovery')
        if row_id in rows or task not in TASKS:
            raise ValueError('row ids must be unique and tasks known')
        k = min(budget.cards, budget.top10) if spec.get('top10', False) else budget.cards
        cards = rankings.get(row_id, ())
        rows[row_id] = row_metrics(cards, reference[task], k)
        if task == 'discovery':
            discovery_rows += 1
            if discovery_rows > budget.rows:
                continue
        footprint[task].update(cards[:k])
    tasks = {}
    for task in TASKS:
        count = len(reference[task])
        hits = len(reference[task] & footprint[task])
        tasks[task] = {'hits': hits, 'targets': count,
                       'score': hits / count if count else None}
    return {'scope': scope, 'week': week, 'tasks': tasks, 'rows': rows}


def summarize(weeks, scopes, *, resampled=False):
    scopes, weeks = list(scopes), list(weeks)
    for scope in scopes:
        _scope(scope)
    if len(set(scopes)) != len(scopes):
        raise ValueError('preregistered scopes must be unique')
    observations, unregistered = {}, []
    for observation in weeks:
        scope, week = observation['scope'], observation['week']
        _scope(scope)
        key = scope, week
        if key in observations and (not resampled or observation != observations[key]):
            raise ValueError('duplicate scope-week observation')
        observations[key] = observation
        if scope not in scopes and scope not in unregistered:
            unregistered.append(scope)
    tasks = {}
    for task in TASKS:
        support = {}
        for scope in scopes:
            scoped = [row for row in weeks if row['scope'] == scope]
            scores = [row['tasks'][task]['score'] for row in scoped
                      if row['tasks'][task]['score'] is not None]
            support[scope] = {'score': mean(scores) if scores else None,
                              'target_weeks': len(scores), 'observed_weeks': len(scoped)}
        missing = [scope for scope in scopes if support[scope]['score'] is None]
        tasks[task] = {'score': mean(support[s]['score'] for s in scopes)
                       if scopes and not missing else None,
                       'scopes': support, 'missing_scopes': missing}
    return {'tasks': tasks, 'unregistered_scopes': unregistered}
