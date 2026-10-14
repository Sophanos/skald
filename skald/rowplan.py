"""Read-only home explanations and the existing household row control flow."""
import copy
from contextlib import ExitStack
from contextvars import ContextVar
import json
from pathlib import Path
from unittest.mock import patch

_trace = ContextVar('rowplan', default=None)
REFRESH_REQUEST = 'row_refresh.json'
FAMILY_MAX = {'mood': 4, 'lane': 5}
REASONS = {
    'disabled': 'Disabled by the household row selection or default set.',
    'unsafe': 'Unavailable for this viewer under the row scope or safety policy.',
    'not_today': 'Not selected by the schedule or daily rotation today.',
    'too_few_own': 'Too few own picks survive the home filters.',
    'overlap': 'Earlier rows already contain these picks.',
    'family_cap': 'The household limit for this row family has been reached.',
    'not_built': 'No seed or usable source picks have been built yet.',
}


def family_caps(cfg):
    return {'mood': 3, 'lane': 3, **cfg['household'].get('home', {}).get('family_caps', {})}


def capped(cfg):
    return 'rows' not in cfg['household'] or cfg['household'].get('rows_from_default', False)


def validate_home(home):
    if not isinstance(home, dict) or set(home) - {'family_caps'}:
        raise ValueError('household.home')
    caps = home.get('family_caps', {})
    if not isinstance(caps, dict) or set(caps) - {'mood', 'lane'} or any(
            type(v) is not int or not 0 <= v <= 8 for v in caps.values()):
        raise ValueError('household.home.family_caps (integers 0..8)')


def observe(cid, metas=(), code=None, *, typ=None):
    trace = _trace.get()
    if trace is not None:
        rec = trace['rows'].setdefault(cid, {'cards': [], 'dropped': {}, 'code': None})
        rec['cards'] = list(metas)
        rec['code'] = code
        if typ is not None:
            trace['served'][cid, typ] = list(metas)


def ranked(cid, metas, scores):
    trace = _trace.get()
    if trace is not None:
        for i, x in enumerate(metas, 1):
            trace['ranking'][cid, x['id'], x['type']] = {**scores[x['id'], x['type']], 'position': i, 'type': x['type']}


def removed(cid, before, after, rule):
    trace = _trace.get()
    if trace is None:
        return
    rec = trace['rows'].setdefault(cid, {'cards': [], 'dropped': {}, 'code': None})
    kept = {(x['id'], x['type']) for x in after}
    for x in before:
        key = (x['id'], x['type'])
        if key not in kept:
            rec['dropped'].setdefault(key, (x, rule))


def filtered(cid, before, after, p, masks, lang, animation):
    trace = _trace.get()
    if trace is None:
        return
    s = trace['server']
    kept = {(x['id'], x['type']) for x in after}
    for x in before:
        if (x['id'], x['type']) in kept:
            continue
        if s.row_member(x, p, 'negative') or len(masks) > 2 and x['id'] in masks[2]:
            rule = 'negative'
        elif s.row_member(x, p, 'seen') or x['id'] in masks[0]:
            rule = 'seen'
        elif not s.quality_ok(x):
            rule = 'quality'
        elif not s.tone_ok(x, s.row_seed(cid, lang), animation=animation, person=s.ROW_OWNER.get(cid)):
            rule = 'tone'
        else:
            rule = 'quality'
        removed(cid, [x], [], rule)


def polished(cid, before, after, cap):
    trace = _trace.get()
    if trace is None:
        return
    s = trace['server']
    keys = {(x['id'], x['type']) for x in after}
    for x in before:
        if (x['id'], x['type']) not in keys:
            rule = 'franchise_cap' if sum(s.franchise_key(y) == s.franchise_key(x) for y in after) >= cap else 'cap' if s.universe_key(x) else 'quality'
            removed(cid, [x], [], rule)


def registered(s):
    s.lab_catalogs()
    return list(dict.fromkeys([*s.DEDUPE_ORDER, *s.LANE_BENCH, *s.SHELF_ROWS,
                              s.rid('family'), *[s.rid(p) for p in s.ARCHIVED]]))


def unsafe_row(s, cid, who, scope):
    if who == s.GUEST and cid not in s.GUEST_ROWS or not s.row_scope_ok(cid):
        return True
    if cid == s.rid('family'):
        return not any(s.kids_age(p) is not None for p in s.PEOPLE) or scope in s.PEOPLE and s.kids_age(scope) is None
    if cid in s.ROW_OWNER and s.kids_age(s.safety_subject(who)) is not None and s.ROW_OWNER[cid] != who:
        return True
    if cid in s.SHELF_ROWS:
        owner = cid.removeprefix(s.rid('shelf_')).rsplit('_', 1)[0]
        return owner not in ('together', scope) or who == s.GUEST
    return False


def raw_cards(s, cid, lang):
    types = ('movie', 'series') if cid in (s.rid('top10'), s.rid('toprated')) else ('movie',) if cid == s.rid('digital') else ('tv',) if cid in s.MIXED else ('series',)
    return [x for typ in types for x in s.row_list(cid, typ, lang)]


def row_id(s, name, lang=None):
    ids = registered(s)
    cid = name if name.startswith(s.rid('')) else s.rid(name)
    if cid in ids:
        return cid
    hits = [cid for cid in ids if label(s, cid, lang or s.LANG).casefold() == name.casefold()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError('unknown or ambiguous row')


def label(s, cid, lang):
    if cid.startswith(s.rid('lab_')):
        from . import lab
        return next((i['name'] for i in lab.read(s.DATA)['ideas'] if s.rid('lab_'+i['id']) == cid), cid)
    key = cid.removeprefix(s.rid(''))
    if cid in s.ROW_OWNER:
        fallback = s.t('row.member', person=s.LABELS[s.ROW_OWNER[cid]])
    elif cid in s.MOODS_ROWS:
        fallback = s.t('row.mood')
    elif cid in s.LANE_ROWS:
        fallback = s.t('row.picked')
    elif cid in s.DOCS:
        fallback = s.t(s.DOC_ROWS.get(cid, s.DOC_TREND)[0])
    elif cid in s.STUDIO_ROWS:
        fallback = s.t(s.STUDIOS[cid][0])
    else:
        fallback = s.t('row.'+key)
    return s.row_name(cid, lang, fallback=fallback) or key


def source_kind(s, cid):
    if cid.startswith(s.rid('lab_')):
        return 'lab'
    if cid in s.SHELF_ROWS:
        return 'shelf'
    if cid in s.ROW_OWNER or cid in {s.rid(p) for p in s.ARCHIVED}:
        return 'member'
    if cid in s.MOODS_ROWS + s.LANE_ROWS + [s.rid('leap')]:
        return 'ai_sourced'
    if cid in s.BECAUSE_ROWS:
        return 'tmdb_seed'
    if cid in {s.rid(k) for k in ('top10', 'digital', 'toprated', 'doctrend')}:
        return 'chart'
    if cid in {s.rid(k) for k in ('episodes', 'catchup', 'rewatch', 'upcoming', 'airing', 'nextseason')}:
        return 'history'
    return 'curated'


def sourcing(s, cid, lang):
    status = getattr(s, '_row_source_status', {}).get((cid, lang))
    if status:
        return dict(status)
    rec = s.feed(cid, lang)
    names = rec.get('names') or {}
    if names.get('source_status'):
        status, date = names['source_status'], names.get('source_date')
        return dict(decided_date=date, reused_today=status == 'reused' and date == s.local_now().date().isoformat(),
                    fresh=status == 'fresh', failed=status == 'failed', skipped_by_budget=status == 'budget',
                    **({'candidate_sources': names['candidate_sources']} if 'candidate_sources' in names else {}))
    # shortcut: older feeds only have a build date, rebuild them for full sourcing status.
    date = s.local_now(rec['built']).date().isoformat() if rec.get('built') else None
    return {'decided_date': date, 'reused_today': date == s.local_now().date().isoformat(),
            'fresh': False, 'failed': False, 'skipped_by_budget': False}


def card(s, x, removed_by=None):
    notes, _ = s.card_parts(x)
    return {'key': x['id'], 'kind': 'movie' if x['type'] == 'movie' else 'tv', 'title': x.get('name', ''), 'origin': 'borrowed' if x.get('_borrowed') else 'own',
            'reason_present': any(n.strip() for n in notes), 'removed_by': 'cap' if removed_by in ('superhero_cap', 'franchise_cap') else removed_by}


def candidate_sources(metas, missing=0):
    return {**{source: sum((x.get('_row') or {}).get('feeling_source') == source for x in metas)
               for source in ('catalog', 'recommendations')}, 'missing_cards': missing}


def plan(s, person='household', lang=None, *, cards=False, caps=None):
    from . import shelves, lab
    person = 'together' if person == 'both' else person
    lang = lang or s.LANG
    if not s.LANG_RE.fullmatch(lang):
        raise ValueError('invalid language')
    if person not in ('household', 'together', 'guest', *s.PEOPLE):
        raise ValueError('unknown person')
    scope = None if person in ('household', 'guest') else person
    trace = {'server': s, 'rows': {}, 'served': {}, 'ranking': {}}
    token = _trace.set(trace)
    try:
        with ExitStack() as stack:
            if scope is not None and scope not in s._shown_scoped:
                stack.enter_context(patch.dict(s._shown_scoped, {scope: s._shown.copy()}))
            stack.enter_context(s.addon_scope(scope))
            who = s.GUEST if person == 'guest' else s.who_watching()[0] if scope is None else s.SCOPES[scope]
            viewer = s._viewer.set(who)
            stack.callback(s._viewer.reset, viewer)
            p = s._profiles.get('household') or {}
            masks = s.row_masks(who)
            home = s.build_home(lang, p, masks, who, caps=caps)
            feeds = s.feed_rows(lang, who=who, home=home)
            above = set()
            for (cid, typ), xs in trace['served'].items():
                if home.get((cid, typ)):
                    above = s.mark_above(above, cid, xs)
            if who == s.GUEST:
                public = s.guest_rows_view(lang)['rows']
                feeds = [(row['id'], ('tv',), row['name']) for row in public]
                home = {(row['id'], 'tv'): [{'id': x['key'], 'name': x['title'], 'type': 'movie' if x['kind'] == 'movie' else 'series', 'description': ''} for x in row['items']] for row in public}
                for cid, _, _ in feeds:
                    trace['rows'][cid] = {'cards': home[cid, 'tv'], 'dropped': {}, 'code': None}
                    if cid == s.rid('toprated'):
                        scores = s.toprated_scores(raw_cards(s, cid, lang), s.GUEST)
                        for typ in ('movie', 'series'):
                            ranked(cid, [x for x in home[cid, 'tv'] if x['type'] == typ], scores)
            wishes = {s.rid(f"shelf_{w['scope']}_{w['slot']}"): w for w in shelves.read(s)['wishes']}
            rules = shelves.rules(s)
            labels = {cid: name if isinstance(name, str) else ' / '.join(name.values()) for cid, _, name in feeds}
            visible, composed = [], {}
            for cid, types, _ in feeds:
                if unsafe_row(s, cid, who, scope):
                    continue
                if cid in s.SHELF_ROWS:
                    xs = s.shelf_items(cid, 'tv', lang, who)
                elif cid not in s.DEDUPE_ORDER and who != s.GUEST:
                    xs = [x for typ in (('tv',) if 'tv' in types else types) for x in s.served_row(cid, typ, lang, p, above, masks, who=who)]
                    observe(cid, xs)
                else:
                    xs = [x for typ in (('tv',) if 'tv' in types else types)
                          for x in (home.get((cid, typ), []) if who == s.GUEST else trace['served'].get((cid, typ), home.get((cid, typ), [])))]
                minimum = 1 if who == s.GUEST or cid in s.SHELF_ROWS or cid == s.rid('episodes') or cid == s.rid('top10') and s.kids_age(s.safety_subject(who)) is None else s.ROW_MIN
                if len(xs) >= minimum:
                    visible.append(cid)
                    composed[cid] = xs
                    if cid in s.SHELF_ROWS:
                        observe(cid, xs)
            support = s.row_metrics.report(s.DATA, s.CONFIG, s.time.time()) if s.row_metrics.settings(s.CONFIG)['enabled'] else None
            out = []
            for cid in registered(s):
                rec = trace['rows'].get(cid, {'cards': [], 'dropped': {}, 'code': None})
                unsafe = unsafe_row(s, cid, who, scope)
                position = visible.index(cid)+1 if cid in visible and not unsafe else None
                xs = [] if unsafe else composed.get(cid, rec['cards'])
                drops = {} if unsafe else rec['dropped']
                code = None if position is not None else 'unsafe' if unsafe else 'disabled' if not s.row_enabled(cid) else rec['code']
                if code is None and position is None:
                    overlap = any(rule == 'overlap' for _, rule in drops.values())
                    raw = raw_cards(s, cid, lang)
                    code = 'overlap' if overlap and not xs else 'too_few_own' if raw else 'not_built'
                if code == 'too_few_own' and not xs:
                    if any(rule == 'overlap' for _, rule in drops.values()):
                        code = 'overlap'
                    elif not raw_cards(s, cid, lang):
                        code = 'not_built'
                if cid in wishes and not unsafe and not shelves.window(wishes[cid], s.local_now(), rules.get(wishes[cid]['id'])) and code != 'disabled':
                    code = 'not_today'
                kind = source_kind(s, cid)
                reason = REASONS[code] if code else 'Selected for this home.'
                if kind == 'lab':
                    paused = lab.paused(s.CONFIG)
                    if paused:
                        reason = s.t('lab.paused' if paused == 'metrics' else 'lab.disabled')
                    elif code == 'not_today':
                        idea = next(i for i in lab.read(s.DATA)['ideas'] if s.rid('lab_'+i['id']) == cid)
                        reason = s.t('lab.retired' if idea['state'] == 'retired' else 'lab.reason.not_today')
                row = {'id': cid, 'label': labels.get(cid, label(s, cid, lang)), 'position': position,
                       'rejection': code, 'reason': reason, 'source': kind,
                       'own_cards': sum(not x.get('_borrowed') for x in xs), 'borrowed_cards': sum(bool(x.get('_borrowed')) for x in xs),
                       'superhero_removed': sum(rule == 'superhero_cap' for _, rule in drops.values()),
                       'franchise_removed': sum(rule == 'franchise_cap' for _, rule in drops.values()),
                       'hero_cards': sum(s.is_hero(x) for x in xs), 'seed': None if unsafe else s.row_seed(cid, lang)}
                if kind == 'ai_sourced':
                    row['ai_sourcing'] = sourcing(s, cid, lang) if not unsafe else None
                    months = s.row_months(cid, lang) if not unsafe else None
                    row['season'] = {'months': months, 'hidden': not s.season_fits(months)}
                counts = (s.feed(cid, lang).get('names') or {}).get('candidate_sources')
                if counts is not None and not unsafe:
                    row['candidate_sources'] = counts
                if support is not None:
                    rows = [r for r in support['rows'] if r['row'] == cid and (person == 'household' or r['person'] == person)]
                    row['support'] = {'shown': sum(r['shown'] for r in rows), 'plays': sum(r['played'] for r in rows)}
                if cards or cid == s.rid('toprated'):
                    kept = {(x['id'], x['type']) for x in xs}
                    row['cards'] = [{**card(s, x), **trace['ranking'].get((cid, x['id'], x['type']), {})} for x in xs] + [card(s, x, rule) for key, (x, rule) in drops.items() if key not in kept]
                out.append(row)
            out.sort(key=lambda r: (r['position'] is None, r['position'] or 0))
            return {'person': person, 'lang': lang, 'rows': out}
    finally:
        _trace.reset(token)


def why(s, name, person='household', lang=None):
    cid = row_id(s, name, lang)
    return next(r for r in plan(s, person, lang, cards=True)['rows'] if r['id'] == cid)


def title(s, key, kind=None, person=None):
    if not any(x['id'] == key and (kind is None or x['type'] == ('movie' if kind == 'movie' else 'series'))
               for x in raw_cards(s, s.rid('toprated'), s.LANG)):
        return {}
    person = person or getattr(s._scoping, 'scope', None) or 'household'
    picks = [x for x in why(s, 'toprated', person)['cards'] if x['key'] == key and x.get('position')
             and (kind is None or x['type'] == ('movie' if kind == 'movie' else 'series'))]
    return {'toprated': picks[0]} if picks else {}


def text(result):
    for row in result.get('rows', [result]):
        yield f"{row['position'] or '-':>2} {row['id']} | {row['label']} | {row['rejection'] or 'shown'}: {row['reason']} (own {row['own_cards']}, borrowed {row['borrowed_cards']}, hero cap {row['superhero_removed']}, franchise cap {row['franchise_removed']})"
        for x in row.get('cards', []):
            yield f"   {x['key']} | {x['title']} | {x['origin']} | reason {x['reason_present']} | {x['removed_by'] or 'kept'}"
            if 'position' in x:
                yield f"      {x['type']} position {x['position']} | rating {x['rating']} | taste match {x['taste_match']} of 100 | blend {x['blend']:.4f}"


def prefer(cfg, theme, more):
    if theme not in ('mood', 'lane'):
        raise ValueError('theme must be mood or lane')
    count = family_caps(cfg)[theme] + (1 if more else -1)
    if not 0 <= count <= FAMILY_MAX[theme]:
        raise ValueError(f'{theme} rows: Home maximum is {FAMILY_MAX[theme]}; use a limit from 0')
    cfg['household'].setdefault('home', {}).setdefault('family_caps', {})[theme] = count
    return count


def projected_home(s, before, after, more):
    with ExitStack() as stack:
        if not s._feeds and not s._profiles and (Path(s.DATA)/'feeds.json').exists():
            if not getattr(s, '_CONFIG_READONLY', False):
                raise ValueError('Taste is still loading. Try again in a minute.')
            from .engine import Engine
            s = stack.enter_context(Engine(s.DATA, offline=True))._server
        counts = [sum(r['position'] is not None for r in plan(s, caps=family_caps(cfg))['rows']) for cfg in (before, after)]
    minimum = max(1, min(6, counts[0]))
    if not more and counts[1] < minimum:
        raise ValueError(f"Keep at least {minimum} populated rows on Home; this change would leave {counts[1]}")
    return {'before': counts[0], 'after': counts[1], 'minimum': minimum}


def refresh_requests(s):
    path = Path(s.DATA)/REFRESH_REQUEST
    if not path.exists():
        return []
    if path.stat().st_size > 16384:
        raise ValueError('row refresh request too large')
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or len(rows) > 128 or any(not isinstance(cid, str) or cid not in registered(s) for cid in rows):
        raise ValueError('invalid row refresh request')
    return rows


def queue_refresh(s, cid):
    from . import shelves
    from .intake import write_private
    if cid not in registered(s):
        raise ValueError('unknown row')
    with shelves.locked(s):
        rows = list(dict.fromkeys([*refresh_requests(s), cid]))
        if len(rows) > 128:
            raise ValueError('too many row refresh requests')
        write_private(Path(s.DATA)/REFRESH_REQUEST, json.dumps(rows)+'\n')


def consume_refresh(s):
    from . import shelves
    path = Path(s.DATA)/REFRESH_REQUEST
    if not path.exists():
        return set()
    with shelves.locked(s), s._feeds_lock:
        try:
            rows = set(refresh_requests(s))
        except (ValueError, TypeError):
            rows = set()
        for (cid, lang), rec in s._feeds.items():
            if cid in rows:
                rec.pop('stamp', None)
                if cid in s.SHELF_ROWS:
                    raw = s._feeds.get((s.rid('shelfraw_'+cid.removeprefix(s.rid('shelf_'))), lang), {})
                    raw.pop('window', None)
        path.unlink(missing_ok=True)
    return rows


def refresh_state(s, cid):
    if not s._feeds:
        path = Path(s.DATA)/'feeds.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        s._feeds.update({tuple(key.rsplit('|', 1)): rec for key, rec in saved.items()})
    rows = {lang: {'built': rec.get('built'), 'stamp': rec.get('stamp')} for (row, lang), rec in s._feeds.items() if row == cid}
    if cid in s.SHELF_ROWS:
        raw = s.rid('shelfraw_'+cid.removeprefix(s.rid('shelf_')))
        for lang, rec in rows.items():
            rec['window'] = s._feeds.get((raw, lang), {}).get('window')
    return rows


def refresh(s, cid, expected, *, restore=False):
    after = {lang: {**rec, 'stamp': None, **({'window': None} if 'window' in rec else {})} for lang, rec in expected.items()}
    with s._feeds_lock:
        if refresh_state(s, cid) != (after if restore else expected):
            raise ValueError('Row changed; preview again')
        for lang, rec in expected.items():
            feed = s._feeds[cid, lang]
            feed.pop('stamp', None)
            if restore and rec['stamp'] is not None:
                feed['stamp'] = rec['stamp']
            if 'window' in rec:
                raw = s._feeds.get((s.rid('shelfraw_'+cid.removeprefix(s.rid('shelf_'))), lang), {})
                raw.pop('window', None)
                if restore and rec['window'] is not None:
                    raw['window'] = rec['window']
    s.save_feeds()


def preview(s, query, change, viewer, bound, *_):
    from . import commands as c
    op, values = change['op'], change['values']
    if change['kind'] is not None:
        raise ValueError('row control kind')
    if op == 'home_plan':
        if len(values) > 1:
            raise ValueError('one row or the whole home')
        person = bound if bound in (*s.PEOPLE, s.GUEST) else change['person'] or viewer or 'household'
        result = plan(s, person)
        rows = result['rows']
        if values:
            ids = s.row_groups().get(values[0]) or [row_id(s, values[0])]
            rows = [r for r in rows if r['id'] in ids]
        hidden = next((r for r in rows if r['rejection']), None)
        # Preserve legacy Home replies when no label override is configured.
        lang = s.CONFIG['region'].get('row_lang') or {'de': 'de', 'sr': 'sr-latn'}.get(s.LANG[:2], 'en')
        message = s.t('home.selected', lang=lang, n=sum(r['position'] is not None for r in rows))
        if hidden:
            message += ' '+hidden['label']+': '+s.t('home.reason.'+hidden['rejection'], lang=lang).rstrip('.')+'.'
        elif len(rows) == 1:
            row = rows[0]
            message += s.t('home.counts', lang=lang, label=row['label'], own=row['own_cards'], borrowed=row['borrowed_cards'])
        elif rows:
            message += ' '+', '.join(r['label'] for r in rows[:3])+'.'
        if values and any(r['id'] == s.rid('toprated') for r in rows):
            message += '\n'+'\n'.join(text({'rows': [r for r in rows if r['id'] == s.rid('toprated')]}))
        return dict(read_only=True, people=[], name=s.t('home.plan', lang=lang), description=message, home=result)
    targets = c.people(s, change['person'], viewer, bound)
    if len(targets) != 1:
        raise ValueError('Choose one adult for household rows')
    if not values:
        return c.ask_preview(s, query, dict(slot='change', question='Which row or row family do you mean?'))
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError('one row or row family')
    cfg = c.config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    proposal = dict(people=targets, change=change, before=before, item=None, text=query[:200], source='search', actor=targets[0])
    if op in ('home_more', 'home_fewer'):
        trial = copy.deepcopy(cfg)
        count = prefer(trial, values[0], op == 'home_more')
        s.validate_config(trial, data=s.DATA)
        proposal.update(rows_before=c.row_settings(cfg), projection=projected_home(s, cfg, trial, op == 'home_more'),
                        name=f"Set {values[0]} row limit to {count} for the household")
    else:
        cid = row_id(s, values[0])
        with s._feeds_lock:
            state = refresh_state(s, cid)
        if not state:
            raise ValueError('Row has no built feed to refresh')
        proposal.update(refresh={'id': cid, 'before': state}, name='Refresh '+label(s, cid, s.LANG)+' for the household')
    return c.seal(proposal)


def source_status(s, cid, lang, *, date=None, reused=False, failed=False, budget=False, candidate_sources=None):
    if not hasattr(s, '_row_source_status'):
        s._row_source_status = {}
    s._row_source_status[cid, lang] = dict(decided_date=date, reused_today=reused, fresh=not reused and not failed and not budget,
                                         failed=failed, skipped_by_budget=budget)
    if candidate_sources is not None:
        s._row_source_status[cid, lang]['candidate_sources'] = candidate_sources


def source_names(s, cid, lang):
    rec = s._row_source_status.get((cid, lang))
    if not rec:
        return {}
    return {'source_date': rec['decided_date'], 'source_status': 'budget' if rec['skipped_by_budget'] else 'failed' if rec['failed'] else 'reused' if rec['reused_today'] else 'fresh',
            **({'candidate_sources': rec['candidate_sources']} if 'candidate_sources' in rec else {})}


def command_input(s, text, results=None, viewer=None, prior=None):
    from . import lab
    inp = {'query': text, 'rows': [{'id': cid, 'label': label(s, cid, s.LANG)} for cid in registered(s)
                                 if cid not in s.LAB_ROWS and s.row_enabled(cid) and s.row_scope_ok(cid) and cid not in s.SHELF_ROWS]}
    inp['rows'].extend({'id': cid, 'label': name} for cid, _, name in s.shelf_rows())
    doc = lab.read(s.DATA)
    names = {s.rid('lab_'+i['id']): i['name'] for i in doc['ideas']}
    current = [s.rid('lab_'+i['id']) for i in lab.select(s.DATA, s.CONFIG, s.time.time(), doc=doc)]
    recent = list(dict.fromkeys(current + list(reversed(s.LAB_ROWS))))
    # Older ideas remain in rows lab; keep the command model's context within its existing limit.
    inp['rows'].extend({'id': cid, 'label': names[cid]} for cid in recent if s.row_scope_ok(cid))
    inp['rows'] = inp['rows'][:100]
    if results:
        inp.update(last=results['query'], shown=[x['title'] for x in s.shown_titles(results)])
    elif prior and prior.get('member') and prior.get('text'):
        inp['last'] = prior['text']
    else:
        from .commands import journal
        active = [e for e in journal(s).get(viewer, []) if not e.get('undone')]
        if active and active[-1].get('member') and active[-1].get('text'):
            inp['last'] = active[-1]['text']
    inp['people'] = [{'id': p, 'label': s.LABELS[p]} for p in s.PEOPLE]
    return inp
