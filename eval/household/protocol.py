"""Versioned policy and evidence gates for local release comparisons.

manifest() returns a detached JSON-compatible dict: version (int), targets
(event names), horizons_days (positive day counts), audience_scopes (member or
household), home_budget (rows and items_per_row), row_families (semantic names,
not catalog ids), exclusions (ineligible candidates), metrics (quality names),
effect_margins (maximum tolerated absolute drop, higher is better), seeds
(named fixed integers). These initial policy values are configurable by callers.
protocol_id(manifest) hashes every field; any policy change invalidates pairing.

Evidence levels are caller assertions about reconstruction, never score labels:
diagnostic: retrospective, present-day cached features or incomplete chronology.
point_in_time_fixed_curation: historical state is correct; editorial/LLM picks
are held fixed, so their generation is outside coverage.
point_in_time_complete_supported_pipeline: all inputs for the compared path are
available at the historical cutoff without network access.

result(manifest, evidence_level, ...) returns a detached dict containing
protocol_id, evidence_level, quality (metric-name to real-history score),
quality_passed (bool, supplied by the metric evaluator), correctness (test-name
to count/status), required_components and supported_components (name tuples),
and coverage_failures (sorted missing names). Required names describe the whole
compared path; supported names assert all of that component's historical inputs
are available offline and its behavior was tested. Fixed external picks do not
cover a changed pick generator. Correctness counts never enter quality scores.
Golden-hash, command and synthetic embedding checks belong in correctness.

compare(baseline, candidate, changed_components=(), enabled=False) takes result
dicts and changed component names. It returns comparable and release_pass bools,
evidence_level (candidate level), failures and coverage_failures (name lists).
Release gating is OFF by default. A pass needs matching protocols and complete
evidence, coverage in both runs, real-history quality, and candidate quality_passed.
Fixed-curation and diagnostic pairs can compare only at their own evidence level.
This module performs no evaluation or export. Existing profile_golden mismatch
printers and replay diagnostics may expose values and are not safe exporters.
"""
import copy
import hashlib
import json

DIAGNOSTIC = 'diagnostic'
FIXED_CURATION = 'point_in_time_fixed_curation'
COMPLETE = 'point_in_time_complete_supported_pipeline'
EVIDENCE_LEVELS = (DIAGNOSTIC, FIXED_CURATION, COMPLETE)

HOME_ROWS = 6
HOME_CARDS = 12
TOP10_CAP = 10
SAMPLING_SEED = 20261013
BOOTSTRAP_SEED = 7
BOOTSTRAP_REPLICATES = 5000
BOOTSTRAP_BLOCK_LENGTH = 2
SENSITIVITY_BLOCK_LENGTHS = (1, 4)
CONFIDENCE_LEVEL = .95
MIN_MATURE_WEEKS = 12
MIN_TARGET_WEEKS = 8
MIN_DISCOVERY_TARGETS = 30
SENSITIVITY_TOLERANCE = .01
DEVELOPMENT_WEEKS = 8
OUTCOME_GRACE_DAYS = 7
PLAYBACK_PROGRESS = .25
COMPLETION_PROGRESS = .90
APPROVAL_RATING_MIN = 7
NEGATIVE_RATING_MAX = 4
MIN_SHARE_CELL = 5
MAX_SHARE_COUNT = 1_000_000
SHARE_DECIMALS = 3


def manifest():
    return {
        'version': 1,
        'targets': ['watched', 'liked', 'loved'],
        'horizons_days': [7, 30, 90],
        'audience_scopes': ['member', 'household'],
        'home_budget': {'rows': HOME_ROWS, 'items_per_row': HOME_CARDS,
                        'top10': TOP10_CAP},
        'row_families': ['personal', 'shared', 'discovery'],
        'exclusions': ['seen', 'blocked', 'unavailable', 'post_cutoff_inputs'],
        'metrics': ['recall@20', 'ndcg@20'],
        'effect_margins': {'recall@20': .01, 'ndcg@20': .01},
        'seeds': {'sampling': SAMPLING_SEED, 'bootstrap': BOOTSTRAP_SEED},
        'event_thresholds': {'playback_progress': PLAYBACK_PROGRESS,
                             'completion_progress': COMPLETION_PROGRESS,
                             'approval_rating_min': APPROVAL_RATING_MIN,
                             'negative_rating_max': NEGATIVE_RATING_MAX},
        'uncertainty': {'replicates': BOOTSTRAP_REPLICATES,
                        'block_length': BOOTSTRAP_BLOCK_LENGTH,
                        'sensitivity_blocks': list(SENSITIVITY_BLOCK_LENGTHS),
                        'confidence_level': CONFIDENCE_LEVEL,
                        'min_weeks': MIN_MATURE_WEEKS,
                        'min_target_weeks': MIN_TARGET_WEEKS,
                        'min_targets': MIN_DISCOVERY_TARGETS,
                        'sensitivity_tolerance': SENSITIVITY_TOLERANCE},
        'splits': {'development_weeks': DEVELOPMENT_WEEKS,
                   'outcome_grace_days': OUTCOME_GRACE_DAYS},
        'export': {'min_cell': MIN_SHARE_CELL, 'max_count': MAX_SHARE_COUNT,
                   'decimals': SHARE_DECIMALS},
    }


def protocol_id(policy):
    encoded = json.dumps(policy, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def result(policy, evidence_level, *, quality=None, quality_passed=False,
           correctness=None, required_components=(), supported_components=()):
    if evidence_level not in EVIDENCE_LEVELS:
        raise ValueError('unknown evidence level')
    if type(quality_passed) is not bool:
        raise ValueError('quality_passed must be a bool')
    required, supported = set(required_components), set(supported_components)
    return {
        'protocol_id': protocol_id(policy), 'evidence_level': evidence_level,
        'quality': copy.deepcopy(quality or {}), 'quality_passed': quality_passed,
        'correctness': copy.deepcopy(correctness or {}),
        'required_components': tuple(sorted(required)),
        'supported_components': tuple(sorted(supported)),
        'coverage_failures': sorted(required - supported),
    }


def compare(baseline, candidate, *, changed_components=(), enabled=False):
    failures = []
    if baseline['protocol_id'] != candidate['protocol_id']:
        failures.append('protocol_changed')
    level = candidate['evidence_level']
    if level != baseline['evidence_level'] or level not in EVIDENCE_LEVELS:
        failures.append('evidence_changed')
    required = (set(baseline['required_components']) |
                set(candidate['required_components']) | set(changed_components))
    supported = (set(baseline['supported_components']) &
                 set(candidate['supported_components']))
    missing = sorted(required - supported)
    if missing or not required:
        failures.append('coverage_failure')
    comparable = not failures
    if level != COMPLETE or baseline['evidence_level'] != COMPLETE:
        failures.append('insufficient_evidence')
    if not baseline['quality'] or not candidate['quality'] or candidate['quality_passed'] is not True:
        failures.append('quality_not_passed')
    if enabled is not True:
        failures.append('disabled')
    return {'comparable': comparable, 'release_pass': not failures,
            'evidence_level': level, 'failures': failures, 'coverage_failures': missing}
