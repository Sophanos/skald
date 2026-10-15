"""Opt-in actual home serving replay, using synthetic state only.

replay_home(server, now=UTC_seconds, lang='en-US', scope=None, who=None,
            order=None, enabled=False, details=False) returns None unless explicitly enabled.
server is an isolated, initialized production module with synthetic CONFIG,
profiles, model, facets, embeddings, learning, taste/memory/feedback readers.
Callers must supply offline readers; this module never loads household inputs.
No workers may be running on this module. Replay is single-threaded.
order is an optional sequence of (catalog_id, type), otherwise feed_rows order.
who defaults to the scoped member, or 'both' for a shared scope.
Mixed movie/series/tv aliases collapse to one canonical tv slate at their first
position; genuine split catalogs such as Top 10 remain separate.

Output: rows [{id, type, name, metas}], row_specs [{id, task, top10}], rankings
{spec_id: [title_id]}, candidate_inputs {(catalog_id, type): stages}.
Stages: curated (row_list), own_source (source_metas), pool (row_pool),
backfilled (fill_row output), ranked (preference_order output), served (before
human_cards). Missing stages are empty lists. All contain detached meta dicts.
Rows are the final row_metas outputs, including empty minimum-size failures.
details=True counts denied external calls per row as network_attempts and
uses manifest visibility and labels, retaining blocked rows with an empty slate
without candidate-stage probes or repinning the frozen shown stores.
Candidates include ineligible items and items beyond the served limit; they
must never replace final rows in quality metrics. Pool is refill availability,
not a claim that every item was retrieved specifically for this row.
row_specs/rankings can be passed directly to eval.household.metrics.

Shown stores are current overwritten pins, never historical impressions.
Records stay intact, including names, metas and bench identity. Scoped stores
replace legacy pins; missing scopes inherit them, exactly as addon_scope does.
State and serving caches are detached for replay and restored afterwards.

personal_request(server, candidate_text, now, lang) captures personal_feed's
actual llm_sections request with a supplied ordered list of candidate strings.
It returns {args, kwargs, model, lang}; no LLM is called or picks resolved.
curation_evidence(cached_request, current_request, complete=False,
                  supported_request_hashes=()) returns evidence_level,
request_hash, request_changed. Cache reuse supports fixed curation only.
A complete claim requires independent offline support for the current exact
request hash, including changed candidate text. Unsupported claims raise.
Requests and diagnostics are local only; never pass them to a share exporter.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from unittest.mock import patch

from eval.household.protocol import COMPLETE, FIXED_CURATION, protocol_id


def _denied(*args, **kwargs):
    raise AssertionError('external generation forbidden in home replay')


@contextmanager
def _offline(server, now, denied=_denied):
    with ExitStack() as stack:
        stack.enter_context(patch('time.time', return_value=now))
        stack.enter_context(patch('urllib.request.urlopen', denied))
        # In-memory preferences are the frozen input, not the current config file.
        stack.enter_context(patch.object(server, 'refresh_preferences', lambda: None))
        stack.enter_context(patch.object(server, 'get_model', return_value=server._model['m']))
        for name in ('tmdb', 'openrouter', '_connector_open', 'llm_sections'):
            stack.enter_context(patch.object(server, name, denied))
        for name in ('CONFIG', '_feeds', '_shown', '_shown_scoped', '_profiles'):
            stack.enter_context(patch.object(server, name, deepcopy(getattr(server, name))))
        for name, value in (('_home_cache', {}), ('_person_scores', {}),
                            ('_memory_index', {}), ('_memory_veto', {}),
                            ('_row_kinds', {'profile': None, 'keys': {}}),
                            ('_health', None), ('_HOME_CACHE_ENABLED', True)):
            stack.enter_context(patch.object(server, name, value))
        yield


def replay_home(server, *, now, lang='en-US', scope=None, who=None,
                order=None, enabled=False, details=False):
    if enabled is not True:
        return None
    s, inputs, rows = server, {}, []
    who = (scope if scope in s.PEOPLE else 'both') if who is None else who
    original_serve, original_fill = s.served_row, s.fill_row
    original_order = s.preference_order
    original_metas = s.row_metas
    current = None
    attempts = {}

    def denied(*args, **kwargs):
        attempts[current] = attempts.get(current, 0) + 1
        raise _RowNetworkBlocked()

    def detailed_serve(cid, typ, language, *args, **kwargs):
        nonlocal current
        previous, current = current, (cid, typ)
        try:
            return original_serve(cid, typ, language, *args, **kwargs)
        except _RowNetworkBlocked:
            return []
        finally:
            current = previous

    def manifest_metas(cid, typ, language, *args, **kwargs):
        nonlocal current
        current = cid, typ
        try:
            metas = original_metas(cid, typ, language, *args, **kwargs)
        except _RowNetworkBlocked:
            metas = []
        # Unknown sizes must not let the manifest hide a blocked row.
        return [None] * s.ROW_MIN if attempts.get((cid, typ)) else metas

    def fill(metas, *args, **kwargs):
        result = original_fill(metas, *args, **kwargs)
        inputs[current]['backfilled'] = deepcopy(result)
        return result

    def rank(metas, *args, **kwargs):
        result = original_order(metas, *args, **kwargs)
        inputs[current]['ranked'] = deepcopy(result)
        return result

    def serve(cid, typ, language, *args, **kwargs):
        nonlocal current
        current = cid, typ
        inputs[current] = {'curated': deepcopy(s.row_list(cid, typ, language)),
                           'own_source': deepcopy(s.source_metas(cid, language)),
                           'pool': deepcopy(list(s.row_pool(language))),
                           'backfilled': [], 'ranked': [], 'served': []}
        result = original_serve(cid, typ, language, *args, **kwargs)
        inputs[current]['served'] = deepcopy(result)
        return result

    with _offline(s, now, denied if details else _denied), s.addon_scope(scope), ExitStack() as stack:
        stack.enter_context(patch.object(s, 'served_row', detailed_serve if details else serve))
        if details:
            stack.enter_context(patch.object(s.urllib.request, 'urlopen', denied))
            stack.enter_context(patch.object(s, 'anilist', denied))
        else:
            stack.enter_context(patch.object(s, 'fill_row', fill))
            stack.enter_context(patch.object(s, 'preference_order', rank))
        feeds = s.feed_rows(lang)
        if details and attempts:
            # Keep blocked rows visible when Home normally hides empty slates.
            available = {key: [True] for key in attempts if key is not None}
            available.update({(cid, typ): [True] for cid, types, _ in feeds for typ in types})
            feeds = s.feed_rows(lang, home=available)
        if details:
            with patch.object(s, 'pin_shown', lambda *a, **kw: None), \
                    patch.object(s, 'feed_rows', return_value=feeds), \
                    patch.object(s, 'row_metas', manifest_metas):
                catalogs = [r for r in s.manifest(lang, scope)['catalogs'] if r['id'] != s.rid('search')]
            names = {}
            for row in catalogs:
                names.setdefault(row['id'], {})[row['type']] = row['name']
            requests = [(r['id'], r['type']) for r in catalogs]
        else:
            names = {cid: name for cid, _, name in feeds}
            requests = [(cid, typ) for cid, types, _ in feeds
                        for typ in (('tv',) if 'tv' in types else types)]
        requests = order if order is not None else requests
        seen = set()
        for cid, typ in requests:
            if cid not in names or not s.row_enabled(cid):
                continue
            typ = 'tv' if cid in s.MIXED else typ
            if details and typ not in names[cid]:
                continue
            if (cid, typ) in seen:
                continue
            seen.add((cid, typ))
            name = names[cid]
            if details:
                current = cid, typ
            try:
                metas = deepcopy(s.row_metas(cid, typ, lang, who=who))
            except _RowNetworkBlocked:
                metas = []
            rows.append({'id': cid, 'type': typ,
                         'name': name[typ] if isinstance(name, dict) else name,
                         'metas': metas})
        if details:
            for row in rows:
                row['network_attempts'] = attempts.get((row['id'], row['type']), 0)
    specs, rankings = [], {}
    for row in rows:
        key = row['id'] + ':' + row['type']
        cid = row['id']
        task = ('continuation' if cid in (s.rid('episodes'), s.rid('catchup')) else
                'upcoming' if cid in (s.rid('upcoming'), s.rid('nextseason')) else
                'rewatch' if cid == s.rid('rewatch') else 'discovery')
        specs.append({'id': key, 'task': task, 'top10': cid == s.rid('top10')})
        rankings[key] = [meta['id'] for meta in row['metas']]
    return {'rows': rows, 'row_specs': specs, 'rankings': rankings,
            'candidate_inputs': inputs}


class _Captured(Exception):
    pass


class _RowNetworkBlocked(AssertionError):
    pass


def personal_request(server, candidate_text, *, now, lang='en-US'):
    request = {}

    def capture(*args, **kwargs):
        request.update(args=deepcopy(list(args)), kwargs=deepcopy(kwargs),
                       model=server.PICK_MODEL, lang=lang)
        raise _Captured

    with _offline(server, now), patch.object(server, 'llm_sections', capture), \
            patch.object(server, 'candidate_pool', return_value=list(candidate_text)):
        try:
            server.personal_feed(lang)
        except _Captured:
            return request
    raise ValueError('personal request unavailable')


def curation_evidence(cached_request, current_request, *, complete=False,
                      supported_request_hashes=()):
    current_hash = protocol_id(current_request)
    if complete and current_hash not in supported_request_hashes:
        raise ValueError('unsupported personal request')
    return {'evidence_level': COMPLETE if complete else FIXED_CURATION,
            'request_hash': current_hash,
            'request_changed': protocol_id(cached_request) != current_hash}
