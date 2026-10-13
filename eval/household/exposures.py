"""Opt-in household-level served-list-to-later-play diagnostics; no integration/I/O.

association_report(online, records, *, as_of, horizon_seconds=604800,
                   historical_popularity=None) consumes an online.json v1 dict:
keys are provider@kind strings, lists map signatures to space-separated key
indices, exp maps day -> row -> signature -> [fetches, hour, UTC_seconds, ...].
No audience attribution is inferred. Row/list/day fetches collapse to the first
recorded time. This log retains 35 days, coarse timing, no confirmed visibility
and no logging propensities. Fetches are counts, never views.
records use eval.household.events input shapes. Only reliable second-precision
playback at <= as_of qualifies; known observation times must also be <= as_of.
Missing observation times assert availability; identity/start duplicates collapse.
All times are finite numeric UTC seconds. A list matures at first time + horizon;
only plays strictly after that time and within the horizon associate with it.
historical_popularity maps (kind, provider) to prior play timestamps. Counts
strictly before each exposure give bands 0, 1-4, 5+; missing histories are unknown.
Previous exposure means earlier recorded lists, not lifetime exposure or views.
Output: diagnostic evidence_level, causal=False, fetches, served_lists,
pending_lists, confirmed_views=None, eligible_plays, unmatched_plays, and separate
rows/position_band/previous_exposure/popularity_band maps. Each cell contains
opportunities, later_plays (binary per distinct list/item), association_rate.
Rows additionally contain fractional_credit, split equally over matching rows
per distinct play. This is bookkeeping, not evidence of which row caused play.

restricted_replay(online, cases, *, as_of, horizon_seconds=604800, k=12) takes
{at: recommendation cutoff, ranking: [(kind, provider), ...], target: identity}.
Only mature cases and lists strictly before cutoff support ranking alignment.
It returns matched case indices with wave-1 metrics on exposed candidates only,
unmatched indices/reasons, support_fraction (matched/all cases), and
candidate_support_fraction (exposed/all recommendation slots in mature cases).
Empty denominators are None. No extrapolation to B's unexposed recommendations;
unbiased=False. Randomized replay design: https://arxiv.org/abs/1003.5956.

release_estimate('ips'|'doubly_robust') always returns unsupported and estimate=None.
A future experiment requires exact action probabilities, audience/context,
positions, candidate eligibility, policy version and attributable outcomes.
Then report w=pi_B/pi_log, ESS=(sum w)^2/sum(w^2), unsupported mass and clipping
sensitivity. Item/slot probabilities need not identify interacting whole-page
slate value; frequency is no propensity: https://proceedings.mlr.press/v48/schnabel16.html.
Outputs remain local diagnostics, not safe sharing documents or release gates.
"""
from math import isfinite

from eval.household.events import normalize_events
from eval.household.metrics import row_metrics
from eval.household.protocol import DIAGNOSTIC


def _time(value):
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("finite numeric UTC seconds required")
    return value


def _window(as_of, horizon):
    _time(as_of)
    if _time(horizon) <= 0:
        raise ValueError("positive horizon required")


def _served(online, as_of):
    # Adapter: wave-1 has no compact online-log decoder; never load server state.
    if type(online.get("v")) is not int or online["v"] != 1:
        raise ValueError("online version must be 1")
    keys, lists, records, fetches = online["keys"], online["lists"], [], 0
    for day, rows in sorted(online["exp"].items()):
        for row, signatures in sorted(rows.items()):
            for signature, rec in sorted(signatures.items()):
                if (len(rec) < 3 or len(rec) % 2 != 1 or
                        type(rec[0]) is not int or rec[0] < 1):
                    raise ValueError("invalid online fetch record")
                times = [_time(t) for t in rec[2::2]]
                if min(times) > as_of:
                    continue
                cards = []
                for raw in lists[signature].split():
                    index = int(raw)
                    if not 0 <= index < len(keys):
                        raise ValueError("invalid online list index")
                    provider, kind = keys[index].rsplit("@", 1)
                    cards.append((kind, provider))
                records.append((min(times), day, row, signature, cards))
                fetches += rec[0]
    return sorted(records), fetches


def association_report(online, records, *, as_of, horizon_seconds=604800,
                       historical_popularity=None):
    _window(as_of, horizon_seconds)
    lists, fetches = _served(online, as_of)
    events = normalize_events(records)["events"]
    plays = sorted({(e["identity"], e["occurred_at"]) for e in events
                    if e["playback"] and e["timestamp_precision"] == "second"
                    and e["occurred_at"] is not None and e["occurred_at"] <= as_of
                    and (e["observed_at"] is None or
                         type(e["observed_at"]) in (int, float) and e["observed_at"] <= as_of)})
    history = historical_popularity or {}
    for timestamps in history.values():
        for stamp in timestamps:
            _time(stamp)
    out = {"evidence_level": DIAGNOSTIC, "causal": False, "fetches": fetches,
           "limitations": ["visibility_unconfirmed", "coarse_timing", "35_day_retention",
                           "fractional_credit_is_bookkeeping", "propensities_missing"],
           "served_lists": len(lists), "pending_lists": 0, "confirmed_views": None,
           "eligible_plays": len(plays), "unmatched_plays": 0,
           **{name: {} for name in ("rows", "position_band", "previous_exposure", "popularity_band")}}
    prior, matching_rows = {}, {play: set() for play in plays}
    for at, day, row, signature, cards in lists:
        mature = at + horizon_seconds <= as_of
        out["pending_lists"] += not mature
        seen = set()
        for position, item in enumerate(cards, 1):
            if item in seen:
                continue
            seen.add(item)
            previous = "observed" if prior.get(item, at) < at else "none_observed"
            prior.setdefault(item, at)
            if not mature:
                continue
            matched = [play for play in plays if play[0] == item and at < play[1] <= at + horizon_seconds]
            count = sum(t < at for t in history[item]) if item in history else None
            popularity = "unknown" if count is None else "0" if count == 0 else "1-4" if count < 5 else "5+"
            band = "1-5" if position <= 5 else "6-12" if position <= 12 else "13+"
            for group, key in (("rows", row), ("position_band", band),
                               ("previous_exposure", previous), ("popularity_band", popularity)):
                cell = out[group].setdefault(key, {"opportunities": 0, "later_plays": 0})
                cell["opportunities"] += 1
                cell["later_plays"] += bool(matched)
            for play in matched:
                matching_rows[play].add(row)
    for row in out["rows"].values():
        row["fractional_credit"] = 0.0
    for rows in matching_rows.values():
        out["unmatched_plays"] += not rows
        for row in sorted(rows):
            out["rows"][row]["fractional_credit"] += 1 / len(rows)
    for group in ("rows", "position_band", "previous_exposure", "popularity_band"):
        for cell in out[group].values():
            cell["association_rate"] = cell["later_plays"] / cell["opportunities"]
    return out


def restricted_replay(online, cases, *, as_of, horizon_seconds=604800, k=12):
    _window(as_of, horizon_seconds)
    lists, _ = _served(online, as_of)
    cases, matched, unmatched, supported, total = list(cases), [], [], 0, 0
    for index, case in enumerate(cases):
        at = _time(case["at"])
        if at + horizon_seconds > as_of:
            unmatched.append({"index": index, "reason": "immature"})
            continue
        exposed = {item for first, _, _, _, cards in lists if first < at for item in cards}
        ranking = [item for item in case["ranking"] if item in exposed]
        supported += len(ranking)
        total += len(case["ranking"])
        reason = "target_unexposed" if case["target"] not in exposed else "no_exposed_candidates" if not ranking else None
        if reason:
            unmatched.append({"index": index, "reason": reason})
        else:
            matched.append({"index": index, "metrics": row_metrics(ranking, [case["target"]], k)})
    return {"evidence_level": DIAGNOSTIC, "unbiased": False,
            "scope": "previously_exposed_candidates_only",
            "extrapolation": False,
            "support_fraction": len(matched) / len(cases) if cases else None,
            "candidate_support_fraction": supported / total if total else None,
            "matched": matched, "unmatched": unmatched}


def release_estimate(method):
    if method not in ("ips", "doubly_robust"):
        raise ValueError("unsupported estimation method")
    return {"method": method, "status": "unsupported", "estimate": None,
            "reason": "appropriate_randomized_logging_design_required",
            "requires": ["exact_action_probabilities", "audience_context", "positions",
                         "candidate_eligibility", "policy_version", "attributable_outcomes"]}
