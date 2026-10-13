"""Independent release targets, callable only; no integration is enabled.

normalize_events(records, *, known_titles=()) consumes iterable dicts.
Required: media_kind ('movie' or 'tv'), provider_id (namespaced string,
e.g. 'tmdb:1'), action (string), source (nonempty connector name).
Optional: reliable (bool, default False) declares trustworthy playback evidence;
progress (fraction 0..1); rating (0..10); episode ((season, episode), season >=0,
episode >=1, TV only); rewatch (bool, default False); session_id (string or None).
Canonical identity is (media_kind, provider_id), with a TV series provider_id.
Actions: played, played_episode, progress, completed, like, love, rating, veto.
Other actions, including open/search/favourite, supply no action-based target.
Optional occurred_at and observed_at preserve the supplied values (default None).
timestamp_precision is second/day/month/year/unknown (default unknown).
Second precision requires a finite numeric UTC playback-start time, or None.
Coarse dates remain opaque strings or numeric values, never inferred sessions.
attribution_confidence is 0..1 or None (default None); it is preserved, not scored.
Optional member preserves explicit attribution; mature_at preserves outcome
readiness. weekly_folds accepts precise numeric UTC timestamps with a member
and observed_at; missing attribution or coarse dates cannot define cutoffs.
Caller adapters resolve IDs and declare reliability; no build classifier is used.

Output dict: events (normalized observations with identity and four bools:
playback, completion, approval, negative); discovery (unique identity tuples,
excluding known_titles and explicit rewatches); completion (unique (kind, id, episode_or_None) tuples,
only movies or individual episodes); approval and negative (unique identities).
All target lists sort by identity; events preserve input order and provenance.
Primary playback needs reliable played/played_episode evidence, progress >=.25,
or reliable completion. Completion needs reliable completed or progress >=.90.
TV without an episode cannot complete, regardless of episode counts or progress.
Explicit like/love or rating >=7 approves; veto or rating <=4 is a guardrail.
Opinions are independent of playback reliability. No satisfaction score exists.
protocol reports these fixed choices, not tunable build-dependent thresholds.
coverage maps each source to counts of observations, reliable observations,
playback, completion, approval, negative, precise_playback (before deduplication).

rewatches contains precise sessions after the first play of the same movie or
episode, or sessions explicitly marked rewatch. Each dict contains identity,
episode, occurred_at, session_id, sources (sorted), observation_indices (list).
Shared session_id or identical playback-start times collapse connector reports.
Different episodes never imply rewatches or series completion. Unknown TV
episodes and coarse dates cannot establish distinct genuine rewatch sessions.
No time-window heuristic is used. Results are deterministic and do not mutate
inputs. Invalid field shapes raise ValueError without echoing supplied values.
"""
import math

from . import protocol


def _number(value, low=None, high=None):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (low is None or value >= low)
            and (high is None or value <= high))


def _normalize(record):
    kind, provider = record.get("media_kind"), record.get("provider_id")
    if kind not in ("movie", "tv") or not isinstance(provider, str) or ":" not in provider:
        raise ValueError("Expected media_kind and namespaced provider_id")
    namespace, identifier = provider.split(":", 1)
    if not namespace or not identifier or provider != provider.strip():
        raise ValueError("Invalid provider_id")
    for field in ("action", "source"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError("Expected action and source strings")
    for field in ("reliable", "rewatch"):
        if not isinstance(record.get(field, False), bool):
            raise ValueError("Expected boolean reliability and rewatch flags")
    for field, high in (("progress", 1), ("rating", 10), ("attribution_confidence", 1)):
        if record.get(field) is not None and not _number(record[field], 0, high):
            raise ValueError("Invalid numeric evidence")
    episode = record.get("episode")
    if episode is not None:
        if (kind != "tv" or not isinstance(episode, (tuple, list)) or len(episode) != 2
                or any(type(n) is not int for n in episode) or episode[0] < 0 or episode[1] < 1):
            raise ValueError("Expected a TV season and episode pair")
        episode = tuple(episode)
    precision = record.get("timestamp_precision", "unknown")
    if precision not in ("second", "day", "month", "year", "unknown"):
        raise ValueError("Invalid timestamp precision")
    for field in ("occurred_at", "observed_at", "mature_at"):
        stamp = record.get(field)
        if stamp is not None and not (isinstance(stamp, str) or _number(stamp)):
            raise ValueError("Invalid timestamp")
    occurred = record.get("occurred_at")
    if precision == "second" and occurred is not None and not _number(occurred):
        raise ValueError("Second precision requires numeric UTC time")
    session_id = record.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("Invalid session identifier")
    action, reliable = record["action"], record.get("reliable", False)
    progress, rating = record.get("progress"), record.get("rating")
    unit = kind == "movie" or episode is not None
    completion = reliable and unit and (action == "completed" or
                                        (progress is not None and progress >= protocol.COMPLETION_PROGRESS))
    playback = reliable and (action == "played" or
                             (kind == "tv" and action == "played_episode") or
                             (progress is not None and progress >= protocol.PLAYBACK_PROGRESS) or completion)
    return {"identity": (kind, provider), "episode": episode,
            "action": action, "source": record["source"], "reliable": reliable,
            "occurred_at": occurred, "observed_at": record.get("observed_at"),
            "timestamp_precision": precision,
            "attribution_confidence": record.get("attribution_confidence"),
            "member": record.get("member"),
            **({"mature_at": record["mature_at"]} if record.get("mature_at") is not None else {}),
            "progress": progress, "rating": rating, "session_id": session_id,
            "rewatch": record.get("rewatch", False), "playback": playback,
            "completion": completion,
            "approval": action in ("like", "love") or
                        (rating is not None and rating >= protocol.APPROVAL_RATING_MIN),
            "negative": action == "veto" or
                        (rating is not None and rating <= protocol.NEGATIVE_RATING_MAX)}


def normalize_events(records, *, known_titles=()):
    """Return separate protocol targets and per-source evidence coverage."""
    events = [_normalize(record) for record in records]
    coverage, sessions, aliases = {}, [], {}
    discovery, completion, approval, negative = set(), set(), set(), set()
    known = set(known_titles)
    for index, event in enumerate(events):
        identity, episode = event["identity"], event["episode"]
        precise = (event["playback"] and event["timestamp_precision"] == "second"
                   and event["occurred_at"] is not None)
        counts = coverage.setdefault(event["source"], dict.fromkeys(
            ("observations", "reliable", "playback", "completion", "approval",
             "negative", "precise_playback"), 0))
        counts["observations"] += 1
        counts["precise_playback"] += int(precise)
        for target in ("reliable", "playback", "completion", "approval", "negative"):
            counts[target] += int(event[target])
        if event["playback"] and not event["rewatch"] and identity not in known:
            discovery.add(identity)
        if event["completion"]:
            completion.add((*identity, episode))
        if event["approval"]:
            approval.add(identity)
        if event["negative"]:
            negative.add(identity)
        if not precise or (identity[0] == "tv" and episode is None):
            continue
        unit = (identity, episode)
        keys = [(unit, "time", event["occurred_at"])]
        if event["session_id"] is not None:
            keys.insert(0, (unit, "session", event["session_id"]))
        session = next((aliases[key] for key in keys if key in aliases), None)
        if session is None:
            session = {"identity": identity, "episode": episode,
                       "occurred_at": event["occurred_at"], "session_id": event["session_id"],
                       "sources": set(), "observation_indices": [], "rewatch": False}
            sessions.append(session)
        for key in keys:
            other = aliases.get(key)
            if other is not None and other is not session:
                session["sources"].update(other["sources"])
                session["observation_indices"].extend(other["observation_indices"])
                session["occurred_at"] = min(session["occurred_at"], other["occurred_at"])
                session["rewatch"] |= other["rewatch"]
                sessions.remove(other)
                for alias, target in list(aliases.items()):
                    if target is other:
                        aliases[alias] = session
        for key in keys:
            aliases[key] = session
        if session["session_id"] is None:
            session["session_id"] = event["session_id"]
        session["occurred_at"] = min(session["occurred_at"], event["occurred_at"])
        session["rewatch"] |= event["rewatch"]
        session["sources"].add(event["source"])
        session["observation_indices"].append(index)
    seen, rewatches = set(), []
    for session in sorted(sessions, key=lambda s: s["occurred_at"]):
        unit = (session["identity"], session["episode"])
        if unit in seen or session["rewatch"]:
            rewatches.append({k: sorted(v) if k in ("sources", "observation_indices") else v
                              for k, v in session.items() if k != "rewatch"})
        seen.add(unit)
    return {"events": events, "discovery": sorted(discovery),
            "completion": sorted(completion), "approval": sorted(approval),
            "negative": sorted(negative), "rewatches": rewatches,
            "coverage": dict(sorted(coverage.items())),
            "protocol": protocol.manifest()["event_thresholds"]}
