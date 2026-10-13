"""Opt-in, in-memory reconstruction; no loading, serving or release gating.

reconstruct(fold, dependencies) takes a splits.Fold and an exact mapping of
DEPENDENCIES to immutable version journals (empty means known empty).
Each version is {id: str, at: aware datetime, available_at: aware datetime,
value: detached complete payload, deleted: optional bool=False}.
at is occurrence/effective time; available_at is observation time, including
for corrections. Payloads must reflect that observation, never today's state.
Use the same id for replacement/deletion; feedback and undo retain separate
journals so builders can replay their original order and domain semantics.
Sessions carry corrected attribution in value; later fixes never alter train.
Metadata, popularity, facets and embeddings also need historical versions.
Absence of a journal is an error, not an assertion of historical support.

Output: {origin: UTC datetime, train: tuple of detached Fold.train records,
journals: {component: tuple of permitted versions},
state: {component: {id: latest surviving value}}}. Serving requires BOTH
at and available_at strictly before origin. Revisions sort by observation,
occurrence, id; duplicate revision coordinates are errors. No title-membership
filter is used for learned credits or other dependencies. No current model
is accepted. Builders derive profiles, models, statistics and decay from this
prefix, using origin as their clock and no unsupplied caches or global state.
session_labels(versions, as_of) returns a detached latest session-value map;
observation <= as_of permits later corrections for labels only, never serving.
Callers restrict these labels to the Fold's target window and maturity rules.
freeze_reference(policy, inputs) requires protocol extensions reference_catalog
(unique string ids) and reference_required_components (unique catalog-feature
names, including metadata). Eligibility versions are {eligible: bool, reason:
'unavailable' or 'ineligible' when false}. Returns origin, protocol_id, eligible
(sorted tuple), excluded (reason to tuple), exclusion_counts (reason to int).
Missing historical eligibility/features count as historically_unverifiable.
This catalog is protocol-defined independently of either build's retrieval.
reference_targets(reference, targets) accepts catalog ids or normalized event
identities and returns distinct eligible catalog ids plus
exclusion_counts, including outside_reference. Never pass a retrieval pool.
rebuild_pair(fold, dependencies, policy, build_a, build_b) freezes reference
first, then calls each builder on its own identical detached inputs. Builders
must construct and return fresh {profile: ..., model: ..., ...}; returns
{A: builder result, B: builder result, reference: frozen reference}.
Wave-1 Fold/hash are reused; journals adapt the missing reconstruction API.
"""
from copy import deepcopy
from datetime import datetime, timezone

from eval.household.protocol import protocol_id
from eval.household.splits import Fold


FEATURES = ('metadata', 'popularity', 'facets', 'embeddings')
DEPENDENCIES = ('searches', 'feedback', 'undo', 'sessions', 'seeds', 'fatigue',
                'credits', 'memory', 'candidate_pools', 'pinned_rows',
                *FEATURES, 'eligibility')
EXCLUSIONS = ('unavailable', 'ineligible', 'historically_unverifiable')


def _time(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('historical times must be timezone-aware datetimes')
    return value.astimezone(timezone.utc)


def _versions(records, cutoff, *, labels=False):
    cutoff, coordinates, permitted = _time(cutoff), set(), []
    for row in records:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'] or type(row.get('deleted', False)) is not bool
                or ('value' not in row and not row.get('deleted'))):
            raise ValueError('invalid historical version')
        at, observed = _time(row.get('at')), _time(row.get('available_at'))
        coordinate = (row['id'], at, observed)
        if coordinate in coordinates:
            raise ValueError('ambiguous historical revision')
        coordinates.add(coordinate)
        if at < cutoff and (observed <= cutoff if labels else observed < cutoff):
            permitted.append(deepcopy(row))
    return tuple(sorted(permitted, key=lambda r: (_time(r['available_at']),
                                                 _time(r['at']), r['id'])))


def _state(versions):
    current = {}
    for row in versions:
        if row.get('deleted', False):
            current.pop(row['id'], None)
        else:
            current[row['id']] = deepcopy(row['value'])
    return current


def reconstruct(fold, dependencies):
    if not isinstance(fold, Fold) or set(dependencies) != set(DEPENDENCIES):
        raise ValueError('a Fold and every historical dependency are required')
    origin = _time(fold.origin)
    train = []
    for row in fold.train:
        at, observed = _time(row.get('at')), _time(row.get('available_at'))
        mature = _time(row.get('mature_at', row.get('at')))
        if at < origin and observed < origin and mature <= origin:
            train.append(deepcopy(row))
    journals = {name: _versions(dependencies[name], origin) for name in DEPENDENCIES}
    return {'origin': origin, 'train': tuple(train), 'journals': journals,
            'state': {name: _state(rows) for name, rows in journals.items()}}


def session_labels(versions, as_of):
    return _state(_versions(versions, as_of, labels=True))


def freeze_reference(policy, inputs):
    catalog = policy.get('reference_catalog')
    required = policy.get('reference_required_components')
    if (not isinstance(catalog, (list, tuple))
            or any(not isinstance(t, str) or not t for t in catalog)
            or len(set(catalog)) != len(catalog)
            or not isinstance(required, (list, tuple))
            or any(c not in FEATURES for c in required)
            or len(set(required)) != len(required) or 'metadata' not in required):
        raise ValueError('protocol-defined reference catalog and features required')
    state, eligible = inputs['state'], []
    excluded = {reason: [] for reason in EXCLUSIONS}
    for title in sorted(catalog):
        eligibility = state['eligibility'].get(title)
        if eligibility is None:
            reason = 'historically_unverifiable'
        elif (not isinstance(eligibility, dict)
              or type(eligibility.get('eligible')) is not bool):
            raise ValueError('invalid historical eligibility')
        elif not eligibility['eligible']:
            reason = eligibility.get('reason')
            if reason not in ('unavailable', 'ineligible'):
                raise ValueError('historical exclusion reason required')
        elif any(state[c].get(title) is None for c in required):
            reason = 'historically_unverifiable'
        else:
            eligible.append(title)
            continue
        excluded[reason].append(title)
    return {'origin': inputs['origin'], 'protocol_id': protocol_id(policy),
            'eligible': tuple(eligible),
            'excluded': {k: tuple(v) for k, v in excluded.items()},
            'exclusion_counts': {k: len(v) for k, v in excluded.items()}}


def reference_targets(reference, targets):
    targets = {title if isinstance(title, str) else title[1] for title in targets}
    eligible = tuple(sorted(targets.intersection(reference['eligible'])))
    counts = {reason: len(targets.intersection(ids))
              for reason, ids in reference['excluded'].items()}
    counts['outside_reference'] = len(targets) - len(eligible) - sum(counts.values())
    return {'targets': eligible, 'exclusion_counts': counts}


def rebuild_pair(fold, dependencies, policy, build_a, build_b):
    inputs = reconstruct(fold, dependencies)
    reference = freeze_reference(policy, inputs)
    results = {}
    for name, build in (('A', build_a), ('B', build_b)):
        result = build(deepcopy(inputs))
        if not isinstance(result, dict) or not {'profile', 'model'} <= result.keys():
            raise ValueError('builders must rebuild both profile and model')
        results[name] = deepcopy(result)
    return {**results, 'reference': reference}
