"""Opt-in, in-memory session evaluation; no production integration or I/O.

evaluate_session(session, slate, records=(), member_preferences=None, *, k=12)
accepts session {id: nonempty str, participants: optional sequence of unique
member IDs}. Participants are a caller assertion of independently confirmed
attendance. Never derive them from Together attribution, a shared prior, or
the household roster. Extra session fields (who/by/ctx/source) confer no proof.
No participants means shared-unknown; one means individual; multiple means
verified-group. Group scope IDs encode the sorted participants without aliases.

slate is one ranked sequence of (media_kind, namespaced provider_id) identities.
records are wave-1 events.normalize_events inputs belonging to this one session,
plus optional member (the independently identified author of feedback).
Supplied session_id must match session.id. Caller must establish session bounds
for records without session_id. Unattributed feedback never approves for others.
member_preferences maps each participant to independently observed held-out
event records, separate from session records. Positive preferences use wave-1
approval evidence; missing or empty positive targets yield None, never dislike.
Caller is responsible for chronological holdout and independent observation.

Output: scope tuple; group_choices and group_completion row_metrics reports
(None unless verified-group); viewing_events/completion_events unit counts
(None for shared-unknown). A movie or TV episode counts once in this session,
not once per participant or connector. TV plays with unknown episode count
one unidentified unit but never establish episode or series completion.
Completion metrics recover distinct title identities with supported completion.
member_balance reports label 'member-balance proxies', metric 'ndcg_at_k',
members (ID to score/None), missing_members, mean, minimum, spread (max-min).
Aggregates are None unless all participants have support. Every member uses
the SAME slate and k; these proxies are not measured joint satisfaction.
joint_approval maps each slate identity to {status, missing_members}. Approved
requires session approval from every participant, and no relevant negative.
Absent feedback is unknown; any relevant negative is rejected. veto_violations
lists slate identities with participant negatives from session or held-out
evidence (wave-1 veto/low-rating guardrails); hard_failure is their presence,
regardless of proxy averages or k. Guardrails inspect the whole supplied slate.
The module selects no aggregation rule and makes no satisfaction guarantee.
"""
import json
from statistics import mean

from eval.household.events import normalize_events
from eval.household.metrics import row_metrics


def evaluate_session(session, slate, records=(), member_preferences=None, *, k=12):
    """Score one supplied slate using confirmed attendance and separate evidence."""
    sid = session.get('id')
    participants = session.get('participants', ())
    if (not isinstance(sid, str) or not sid.strip()
            or not isinstance(participants, (list, tuple))
            or any(not isinstance(p, str) or not p.strip()
                   or p in ('together', 'both', 'household') for p in participants)
            or len(set(participants)) != len(participants)):
        raise ValueError('invalid session or confirmed participants')
    participants = sorted(participants)
    if not participants:
        scope = ('shared-unknown', 'household')
    elif len(participants) == 1:
        scope = ('individual', participants[0])
    else:
        scope = ('verified-group', json.dumps(participants, separators=(',', ':')))
    slate, records = list(slate), list(records)
    evidence = normalize_events(records)
    if any(e['session_id'] not in (None, sid) for e in evidence['events']):
        raise ValueError('records must belong to the supplied session')
    preferences = {} if member_preferences is None else member_preferences
    if not isinstance(preferences, dict):
        raise ValueError('member preferences must be a mapping')
    approvals = {p: set() for p in participants}
    negatives = {p: set() for p in participants}
    for record, event in zip(records, evidence['events']):
        member = record.get('member')
        if member not in participants:
            continue
        if event['approval']:
            approvals[member].add(event['identity'])
        if event['negative']:
            negatives[member].add(event['identity'])
    scores = {}
    for member in participants:
        held_out = normalize_events(preferences.get(member, ()))
        scores[member] = row_metrics(slate, held_out['approval'], k)['ndcg_at_k']
        negatives[member].update(held_out['negative'])
    # Validate the budget even when attendance and member targets are unknown.
    choices = row_metrics(slate, evidence['discovery'], k)
    completed = row_metrics(slate, [unit[:2] for unit in evidence['completion']], k)
    missing = [p for p in participants if scores[p] is None]
    supported = bool(participants) and not missing
    balance = {'label': 'member-balance proxies', 'metric': 'ndcg_at_k',
               'members': scores, 'missing_members': missing,
               'mean': mean(scores.values()) if supported else None,
               'minimum': min(scores.values()) if supported else None,
               'spread': max(scores.values()) - min(scores.values()) if supported else None}
    joint, violations = {}, []
    for identity in dict.fromkeys(slate):
        absent = [p for p in participants if identity not in approvals[p]]
        vetoed = any(identity in negatives[p] for p in participants)
        joint[identity] = {'status': 'rejected' if vetoed else
                           'unknown' if absent or not participants else 'approved',
                           'missing_members': absent}
        if vetoed:
            violations.append(identity)
    viewing = {(e['identity'], e['episode']) for e in evidence['events'] if e['playback']}
    return {'scope': scope,
            'group_choices': choices if scope[0] == 'verified-group' else None,
            'group_completion': completed if scope[0] == 'verified-group' else None,
            'viewing_events': len(viewing) if participants else None,
            'completion_events': len(evidence['completion']) if participants else None,
            'member_balance': balance, 'joint_approval': joint,
            'veto_violations': violations, 'hard_failure': bool(violations)}
