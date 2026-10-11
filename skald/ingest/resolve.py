"""Resolution with injected providers; cached records contain only public title metadata."""
import difflib
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from . import clean_title

TAU = .88
MARGIN = .08


def normal(title):
    title = unicodedata.normalize("NFKD", title.casefold())
    title = "".join(c for c in title if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", title))


def match_score(candidate, choice):
    aliases = [choice.get(k) for k in ("title", "name", "original_title", "original_name")]
    aliases += choice.get("aliases") or []
    alt = choice.get("alternative_titles") or {}
    aliases += [x.get("title") for x in alt.get("titles", alt.get("results", []))]
    similarity = max((difflib.SequenceMatcher(None, normal(candidate.title), normal(t)).ratio()
                      for t in aliases if t), default=0)
    year = str(choice.get("year") or choice.get("release_date") or choice.get("first_air_date") or "")[:4]
    year_fit = .5
    if candidate.year and year:
        delta = abs(int(candidate.year) - int(year))
        year_fit = 1 if delta == 0 else .8 if delta == 1 else 0
    kind = choice.get("kind") or choice.get("media_type") or ("tv" if choice.get("name") else "movie")
    kind_fit = 1 if candidate.kind == kind else .5 if not candidate.kind else 0
    popularity = min(1, math.log1p(max(0, float(choice.get("popularity") or 0))) / 8)
    return round(.72 * similarity + .18 * year_fit + .08 * kind_fit + .02 * popularity, 6)


def public_choice(choice, kind=None):
    kind = kind or choice.get("kind") or choice.get("media_type") or ("tv" if choice.get("name") else "movie")
    if kind not in ("movie", "tv") or not re.fullmatch(r"[1-9][0-9]{0,8}", str(choice.get("id", ""))):
        raise ValueError("invalid resolver choice")
    year = str(choice.get("year") or choice.get("release_date") or choice.get("first_air_date") or "")[:4]
    try:
        popularity = float(choice.get("popularity") or 0)
    except (ValueError, TypeError):
        raise ValueError("invalid resolver choice") from None
    if not re.fullmatch(r"(?:[0-9]{4})?", year) or not math.isfinite(popularity):
        raise ValueError("invalid resolver choice")
    return {"id": int(choice["id"]), "title": clean_title(choice.get("title") or choice.get("name") or ""),
            "year": year, "kind": kind, "popularity": popularity,
            "aliases": [clean_title(t) for t in ([choice.get("original_title"), choice.get("original_name")] + (choice.get("aliases") or [])) if t][:5]}


def remote_item(item):
    c = item["candidate"]
    return {"title": clean_title(c["title"]), "year": int(c["year"]) if c.get("year") else None,
            "kind": c.get("kind") or None,
            "choices": [{"key": f"tmdb:{x['id']}", "kind": x["kind"], "title": clean_title(x["title"]),
                         "aliases": [clean_title(t) for t in x.get("aliases", []) if t][:5],
                         "year": int(x["year"]) if x.get("year") else None,
                         "pop": float(x.get("popularity") or 0)} for x in item["choices"] if x.get("title")][:8]}


def remote_groups(batch):
    group = []
    for item in batch:
        trial = group + [item]
        size = len(json.dumps({"items": [remote_item(x) for x in trial], "no_llm": False}, allow_nan=False).encode())
        if group and (len(trial) > 25 or size > 60000):
            yield group
            group = []
        group.append(item)
        if len(json.dumps({"items": [remote_item(x) for x in group], "no_llm": False}, allow_nan=False).encode()) > 60000:
            raise ValueError("remote item too large")
    if group:
        yield group


def remote_provider(request=None):
    if request is None:
        from ..remote import request

    def decide(batch, max_usd):
        answers, receipts, why = {}, {}, {}
        cost, unknown, offset = 0., False, 0
        decide.sources, decide.kinds = {}, {}
        for group in remote_groups(batch):
            body = {"items": [remote_item(x) for x in group], "no_llm": False}
            decide.batches += 1
            decide.sent += len(group)
            reply = request("/api/v1/decide/resolve_title", body)
            rows = reply.get("results") if isinstance(reply, dict) else None
            if not isinstance(rows, list) or len(rows) != len(group):
                raise ValueError("api response")
            for i, (row, inp) in enumerate(zip(rows, body["items"])):
                if not isinstance(row, dict) or row.get("source") not in ("rule", "cache", "llm", "fallback"):
                    raise ValueError("api response")
                source, charge = row["source"], row.get("cost_usd")
                if charge is not None:
                    if type(charge) not in (int, float) or not math.isfinite(charge) or charge < 0:
                        raise ValueError("api response")
                    cost += charge
                elif source == "llm":
                    cost += max_usd
                    unknown = True
                key = str(offset + i)
                receipt = row.get("receipt")
                if receipt is not None:
                    if not isinstance(receipt, str) or not re.fullmatch(r"d_[a-zA-Z0-9_]{1,198}", receipt):
                        raise ValueError("api response")
                    receipts[key] = receipt
                reason = row.get("why") or source
                why[key] = reason if isinstance(reason, str) and re.fullmatch(r"[a-zA-Z0-9_ .:-]{1,80}", reason) else "api response"
                if source == "fallback":
                    answers[key] = {"why": why[key]}
                    continue
                answer = row.get("answer")
                if not isinstance(answer, dict) or set(answer) != {"pick"}:
                    raise ValueError("api response")
                pick = answer["pick"]
                if pick is not None and pick not in [{"key": x["key"], "kind": x["kind"]} for x in inp["choices"]]:
                    raise ValueError("api response")
                confidence = row.get("confidence")
                if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("api response")
                answers[key] = {"choice": pick["key"][5:] if pick else "none", "confidence": confidence}
                decide.sources[key], decide.kinds[key] = source, pick["kind"] if pick else None
            offset += len(group)
        decide.charged = cost
        return {"answers": answers, "usage": {"cost": cost if not unknown else None},
                "receipts": receipts, "why": why}

    decide.remote, decide.sent, decide.batches = True, 0, 0
    return decide


class Resolver:
    def __init__(self, tmdb, decide=None, cache=None, max_ai_usd=.05, tau=TAU, margin=MARGIN, namespace="tmdb", batch_usd=.02):
        if not math.isfinite(max_ai_usd) or not 0 <= max_ai_usd <= 10:
            raise ValueError("--max-ai-usd must be between 0 and 10")
        self.tmdb, self.decide = tmdb, decide
        self.path = Path(cache or Path.home() / ".cache/skald/resolve.json")
        self.max_ai_usd, self.spent = max_ai_usd, 0.0
        self.batch_usd = batch_usd
        self.pending = 0
        self.tau, self.margin = tau, margin
        self.namespace = namespace
        self.cache = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def cache_key(self, candidate):
        value = {"v": 1, "namespace": self.namespace, "title": normal(candidate.title), "year": candidate.year,
                 "kind": candidate.kind, "ids": candidate.ids}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def exact(self, candidate):
        ids = candidate.ids
        if "tmdb" in ids:
            tid = str(ids["tmdb"])
            if not re.fullmatch(r"[1-9][0-9]{0,8}", tid):
                raise ValueError("invalid exported TMDB id")
            kinds = [candidate.kind] if candidate.kind else ["movie", "tv"]
            hits = []
            for kind in kinds:
                try:
                    hit = self.tmdb(f"{kind}/{tid}", language="en-US")
                except LookupError:
                    continue
                if hit and hit.get("id"):
                    if str(hit["id"]) != tid:
                        raise ValueError("TMDB response id does not match the exact id")
                    hits.append(public_choice(hit, kind))
            if len(hits) == 1:
                return hits[0]
            if hits:
                hits.sort(key=lambda c: -match_score(candidate, c))
                if match_score(candidate, hits[0]) - match_score(candidate, hits[1]) + 1e-9 >= self.margin:
                    return hits[0]
        if "imdb" in ids:
            if not re.fullmatch(r"tt[0-9]{1,12}", str(ids["imdb"])):
                raise ValueError("invalid exported IMDb id")
            found = self.tmdb(f"find/{ids['imdb']}", external_source="imdb_id", language="en-US")
            hits = [public_choice(c, kind) for kind, field in (("movie", "movie_results"), ("tv", "tv_results"))
                    for c in found.get(field, []) if not candidate.kind or candidate.kind == kind]
            if len(hits) == 1:
                return hits[0]
        return None

    def resolution(self, choice, confidence, method, choices=()):
        return {"match": {"key": f"tmdb:{choice['id']}", "title": choice["title"], "year": choice["year"],
                          "kind": choice["kind"]} if choice else None,
                "conf": confidence, "method": method, "high_confidence": bool(choice and confidence >= self.tau),
                "choices": list(choices)}

    def resolve(self, candidates, persist=True, cache_only=False):
        results, ambiguous = [], []
        for candidate in candidates:
            key = self.cache_key(candidate)
            result = self.cache.get(key)
            if result is None and cache_only:
                result = self.resolution(None, 0, "unresolved")
            if result is None:
                exact = self.exact(candidate)
                if exact:
                    result = self.resolution(exact, 1., "id")
                else:
                    found = self.tmdb("search/multi", query=candidate.title, language="en-US")
                    choices = [public_choice(c) for c in found.get("results", [])
                               if c.get("title", c.get("name")) and c.get("media_type", c.get("kind", "movie")) in ("movie", "tv")]
                    ranked = sorted(((match_score(candidate, c), c) for c in choices), key=lambda x: -x[0])
                    top = ranked[0][0] if ranked else 0
                    margin = top - (ranked[1][0] if len(ranked) > 1 else 0)
                    if top >= self.tau and margin + 1e-9 >= self.margin:
                        result = self.resolution(ranked[0][1], top, "search")
                    else:
                        result = self.resolution(None, top, "unresolved", [c for _, c in ranked[:5]])
            results.append(dict(result))
            self.cache[key] = results[-1]
            if result["method"] == "unresolved" and result["choices"]:
                ambiguous.append((len(results) - 1, key, candidate, result["choices"]))
        self.pending = len(ambiguous)
        is_remote = getattr(self.decide, "remote", False) is True
        batches = []
        if is_remote:
            batch = [{"candidate": {"title": c.title, "year": c.year, "kind": c.kind}, "choices": choices}
                     for _, _, c, choices in ambiguous]
            start = 0
            for group in remote_groups(batch):
                batches.append(ambiguous[start:start + len(group)])
                start += len(group)
        else:
            batches = [ambiguous[i:i + 25] for i in range(0, len(ambiguous), 25)]
        failure = None
        for group in batches:
            remaining = self.max_ai_usd - self.spent
            reservation = self.batch_usd if is_remote else remaining
            if cache_only or not self.decide or remaining + 1e-9 < reservation or remaining <= 0 or failure:
                for position, _, _, _ in group:
                    if failure or (self.decide and not cache_only):
                        results[position]["why"] = failure or "budget"
                continue
            self.spent += reservation
            batch = [{"candidate": {"title": c.title, "year": c.year, "kind": c.kind}, "choices": choices}
                     for _, _, c, choices in group]
            try:
                reply = self.decide(batch, max_usd=reservation)
                usage = reply.get("usage") or {}
                provider = getattr(self.decide, "transport", self.decide)
                cost = provider.charged if is_remote else usage.get("cost")
                if cost is not None:
                    if not math.isfinite(cost) or cost < 0 or (not is_remote and cost > reservation):
                        raise ValueError("decisions provider exceeded its reserved cost limit")
                    self.spent += cost - reservation
                answers = reply.get("answers") or {}
                for i, (position, key, _, choices) in enumerate(group):
                    answer = answers.get(str(i)) or {}
                    if "choice" not in answer:
                        results[position]["why"] = reply.get("why", {}).get(str(i), "api response")
                        if str(i) in reply.get("receipts", {}):
                            results[position]["receipt"] = reply["receipts"][str(i)]
                        continue
                    selected = next((c for c in choices if str(c["id"]) == str(answer["choice"])
                                     and (not is_remote or not provider.kinds.get(str(i)) or c["kind"] == provider.kinds[str(i)])), None)
                    conf = answer.get("confidence") or 0
                    if type(conf) not in (int, float) or not math.isfinite(conf) or not 0 <= conf <= 1:
                        raise ValueError("invalid decision confidence")
                    source = provider.sources.get(str(i), "llm") if is_remote else "llm"
                    method = "remote_" + source if is_remote and selected else "ai" if selected else "none"
                    result = self.resolution(selected, conf, method, choices)
                    if str(i) in reply.get("receipts", {}):
                        result["receipt"] = reply["receipts"][str(i)]
                    result["why"] = reply.get("why", {}).get(str(i), source)
                    results[position] = self.cache[key] = result
            except ValueError as error:
                if not is_remote:
                    raise
                failure = str(error)
                for position, _, _, _ in group:
                    results[position]["why"] = failure
        if persist:
            from ..intake import write_private
            write_private(self.path, json.dumps(self.cache, ensure_ascii=False, allow_nan=False) + "\n")
        return results


def event(candidate, resolution, person):
    if not resolution["match"]:
        raise ValueError("cannot import an unresolved candidate")
    result = {**resolution["match"], "who": candidate.person or person, "action": candidate.signal,
              "src": candidate.src, "conf": round(resolution["conf"] * candidate.conf, 3), "n": candidate.n}
    if candidate.signal == "abandon" and (candidate.kind or "movie") != resolution["match"]["kind"]:
        result["action"] = "seen"
    if candidate.last_ts:
        result["t"] = candidate.last_ts
    return result
