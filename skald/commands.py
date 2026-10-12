"""Preview and apply one personal change, shared by search, CLI and API."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import threading
OPS = ('hide', 'show', 'exclude_genre', 'include_genre', 'focus', 'unfocus', 'discovery', 'years', 'like', 'dislike', 'shelf_add', 'shelf_stop', 'shelf_count')
OUT = {'op': (*OPS, None), 'kind': ('movie', 'tv', 'series', 'languages', 'countries', None),
       'values': [(str, int, None), 10], 'person': (str, None)}
NONE = dict(op=None, kind=None, values=[], person=None)
def member_name(s, name):
    return next((p for p in s.PEOPLE if name.casefold() in (p.casefold(), s.LABELS[p].casefold())), name)

def shelf_hits(s, name, scope):
    from . import shelves
    alias = re.fullmatch(r'the (.+) shelf', name.casefold())
    key = alias[1] if alias else name.casefold()
    return [w for w in shelves.read(s)['wishes']
            if (scope not in (*s.PEOPLE, 'together') or w['scope'] in (scope, 'together'))
            and (w['name'].casefold() == key or alias and key == 'sunday'
                 and w['when'] != 'always' and w['when']['days'] == ['sun'])]

def parse_shelf(text, s=None, viewer=None, bound=None):
    q = text.strip().casefold()
    person = None
    names = {'me': None}
    if s is not None:
        names.update({name.casefold(): p for p in s.PEOPLE for name in (p, s.LABELS[p])})
    names.update(dict.fromkeys(('us', 'both', 'both of us', 'everyone', 'together'), 'together'))
    for name in sorted(names, key=len, reverse=True):
        suffix = ' for '+name
        if q.endswith(suffix):
            person, q = names[name], q[:-len(suffix)]
            break
    else:
        match = re.search(r' for (\w+)$', q)
        if match:
            person, q = match[1], q[:match.start()]
    values, op = [], None
    match = re.fullmatch(r"(?:every )?(sunday|weekday|weekdays) (morning|mornings|evening|evenings) (.+)", q)
    if match:
        day, time, theme = match.groups()
        values, op = [theme, 'sun' if day == 'sunday' else 'weekdays', '05-12' if time.startswith('morning') else '18-24'], 'shelf_add'
    elif q.startswith('make me a row of ') and q[17:].strip():
        values, op = [q[17:], 'always'], 'shelf_add'
    elif re.fullmatch(r'show \d+ shelves', q):
        values, op = [int(q.split()[1])], 'shelf_count'
    elif q == 'fewer shelves':
        values, op = ['fewer'], 'shelf_count'
    elif q.startswith('stop ') and s is not None:
        scope = person or (bound if bound in s.PEOPLE else viewer)
        if len(shelf_hits(s, q[5:], scope)) == 1:
            values, op = [q[5:]], 'shelf_stop'
    return dict(op=op, kind=None, values=values, person=person) if op else dict(NONE)

def shelf_scope(s, named, viewer, bound):
    actor = bound if bound in s.PEOPLE else viewer
    target = named or actor
    if target != 'together':
        target = next((p for p in s.PEOPLE if str(target).casefold() in (p, s.LABELS[p].casefold())), target)
    if actor == s.GUEST or bound == s.GUEST:
        raise PermissionError('Ask a grown-up to change settings')
    if target not in (*s.PEOPLE, 'together'):
        raise ValueError('Add a member name to this command')
    if s.kids_age(actor) is not None and target != actor:
        raise PermissionError('Ask a grown-up to change other shelves')
    if bound in s.PEOPLE and target not in (bound, 'together'):
        raise PermissionError('This plan applies to another member')
    return target

def shelf_preview(s, change, viewer, bound):
    from . import shelves
    scope = shelf_scope(s, change['person'], viewer, bound)
    op, values = change['op'][6:], change['values']
    kwargs = {}
    if change['kind'] is not None or op == 'add' and (len(values) not in (2, 3) or not all(isinstance(v, str) for v in values)) or op in ('stop', 'count') and len(values) != 1:
        raise ValueError('invalid shelf command')
    if op == 'add':
        if len(values) != (2 if values[1] == 'always' else 3):
            raise ValueError('invalid shelf schedule')
        text = values[0]
        when = 'always' if values[1] == 'always' else values[1]+' '+values[2]
        theme = re.sub(r'^(?:a|an|the|some)\s+', '', text, flags=re.I).title()
        day = 'Sunday ' if values[1] == 'sun' else 'Weekday ' if values[1] == 'weekdays' else ''
        name = day + theme
        kwargs = dict(text=text, name=name, when=when)
        if len([w for w in shelves.read(s)['wishes'] if w['scope'] == scope and w['slot'] <= shelves.slot_count(s, scope)]) >= shelves.slot_count(s, scope):
            raise ValueError('Shelves are full; stop a shelf first')
    elif op == 'stop':
        if not isinstance(values[0], str):
            raise ValueError('invalid shelf name')
        name = values[0]
        hits = shelf_hits(s, name, scope)
        if len(hits) != 1:
            raise ValueError('unknown or ambiguous shelf name')
        wish = hits[0]
        scope = shelf_scope(s, wish['scope'], viewer, bound)
        kwargs = dict(slot=wish['slot'], when=wish['when'])
        name = wish['name']
    else:
        count = max(0, shelves.slot_count(s, scope)-1) if values[0] == 'fewer' else values[0]
        kwargs = dict(count=count)
        name = str(count)+' shelves'
    shelf = s.shelf_change(scope, op, **kwargs)
    when = shelf['when']
    timing = 'always on' if when == 'always' else ('weekdays' if when['days'] == shelves.DAYS[:5] else ','.join(when['days']))+' '+str(when['hours'][0])+'-'+str(when['hours'][1])+' '+s.CONFIG['region']['tz']
    label = 'Together' if scope == 'together' else s.LABELS[scope]
    title = 'Add '+day+'shelf: '+theme if op == 'add' else op.capitalize()+' '+name
    return dict(people=[scope], shelf=shelf, plan=shelf['plan'], name=title+' for '+label+'; '+timing)

def register(s):
    prompt = ('Return JSON with op, kind, values, person. Exactly one persistent personal change or op null. '
              'Ordinary one-off searches are not changes. Explicit shelf requests are changes. Ops: ' + ', '.join(OPS) + '. '
              'hide/show values: ' + ', '.join(s.HIDE_CHOICES) + '. Genre values use TMDB genre names; '
              'kind movie or series. focus/unfocus kind languages or countries uses two-letter codes, '
              'or movie/series uses genres. discovery values: balanced/mainstream/gems. years: [from,to], null ends allowed. '
              'like/dislike values: one title, kind movie or tv or null. person: member name, everyone or null. '
              'shelf_add kind null, values [theme, always] or [theme, days, hours] (sun or weekdays, 05-12 or 18-24); '
              'shelf_stop kind null, values [shelf name]; shelf_count kind null, values [count 0..10] or [fewer]. '
              'Shelf person: member name, together for us, or null for me. Title names are not commands. '
              'Unsupported settings return op null. Do not invent an operation.')
    def rule(inp, params):
        answer = parse_shelf(inp['query'], s)
        q = inp['query'].strip().casefold()
        return (answer, 1) if answer['op'] or q.startswith('stop ') or q in ('sunday bloody sunday', 'heist', 'yes man') else None
    s.decision('parse_command', ver=3, lane='search', cap=None, ttl=30*s.DAY,
        inp={'query': str}, out=OUT, norm=s.route_norm, rule=rule, params={},
        llm={'path': '/api/v1/chat/completions', 'model': s.PICK_MODEL, 'batch': 1, 'timeout': 4,
             'prompt': lambda b: {'model': s.PICK_MODEL, 'messages': [{'role': 'system', 'content': prompt},
                                  {'role': 'user', 'content': b[0]['query']}], 'response_format': {'type': 'json_object'},
                                  'reasoning': {'effort': 'none'}, 'max_tokens': 200},
             'parse': lambda r, b: [(json.loads(r['choices'][0]['message']['content']), 1)]},
        fallback=lambda i, e: (dict(NONE), 0), requires=('OR_KEY',), serving=False, min_acc=.95)
def pending(s):
    if not hasattr(s, '_commands_pending'):
        s._commands_pending = {}
    return s._commands_pending
def get_pending(s, scope):
    at, plan = pending(s).get(scope, (0, None))
    if s.time.time() - at >= 600:
        pending(s).pop(scope, None)
        return None
    return plan
def people(s, named=None, viewer=None, bound=None):
    if bound == s.GUEST or viewer == s.GUEST or (s.kids_age(bound) is not None or s.kids_age(viewer) is not None):
        raise PermissionError('Ask a grown-up to change settings')
    who = bound if bound in s.PEOPLE else named or viewer
    who = who.casefold() if isinstance(who, str) else who
    who = next((p for p in s.PEOPLE if who in (p, s.LABELS[p].casefold())), who)
    if who == 'everyone':
        return [p for p in s.PEOPLE if s.kids_age(p) is None]
    if who not in s.PEOPLE:
        raise ValueError('Add a member name to this command')
    if s.kids_age(who) is not None:
        raise PermissionError('Ask a grown-up to change settings')
    return [who]
def config(s):
    return s.validate_config(json.loads(Path(os.environ.get('SKALD_CONFIG') or Path(s.DATA)/'skald.json').read_text()), data=s.DATA)
def edit(cfg, person, change):
    prefs = cfg['taste'].setdefault('people', {}).setdefault(person, {}).setdefault('prefs', {})
    op, kind, values = change['op'], change['kind'], change['values']
    if op in ('discovery', 'years'):
        if op == 'discovery' and len(values) != 1:
            raise ValueError('discovery')
        prefs[op] = values[0] if op == 'discovery' else values
        return
    target, key = prefs, 'hide'
    if op in ('exclude_genre', 'include_genre'):
        target, key = prefs.setdefault('excluded_genres', {}), kind
    elif op in ('focus', 'unfocus'):
        target, key = prefs.setdefault('focus', {}), kind
        if kind in ('movie', 'series'):
            target = target.setdefault('genres', {})
    elif op not in ('hide', 'show'):
        raise ValueError('unsupported command')
    old = target.get(key, [])
    target[key] = list(dict.fromkeys(old + values)) if op in ('hide', 'exclude_genre', 'focus') else [v for v in old if v.casefold() not in {str(x).casefold() for x in values}]
def resolve(s, change):
    title = change['values'][0]
    choices = []
    for kind in (('movie', 'tv') if change['kind'] is None else (change['kind'],)):
        for x in s.tmdb('search/'+kind, query=title).get('results', [])[:4]:
            choices.append({'key': 'tmdb:'+str(x['id']), 'kind': kind, 'title': x.get('title') or x.get('name'),
                            'year': int((x.get('release_date') or x.get('first_air_date') or '0')[:4]) or None,
                            'aliases': [], 'pop': float(x.get('popularity') or 0)})
    pick = s.decide('resolve_title', {'title': title, 'year': None, 'kind': change['kind'], 'choices': choices})['answer']['pick']
    if not pick:
        raise ValueError('Skald cannot resolve this title yet')
    item = next(x for x in choices if x['key'] == pick['key'] and x['kind'] == pick['kind'])
    return {**pick, 'title': item['title'], 'year': str(item['year'] or '')}
def preview(s, text, viewer=None, bound=None, answer=None):
    local = parse_shelf(text, s, viewer, bound)
    if text.strip().casefold().startswith('stop ') and not local['op']:
        return None
    change = copy.deepcopy(answer if answer is not None else local if local['op'] else s.decide('parse_command', {'query': text})['answer'])
    s.check(OUT, change)
    if change['op'] and change['op'].startswith('shelf_'):
        return shelf_preview(s, change, viewer, bound)
    people(s, viewer=viewer, bound=bound) if bound in (*s.PEOPLE, s.GUEST) or viewer in s.PEOPLE else None
    if change['op'] is None:
        return None
    targets = people(s, change['person'], viewer, bound)
    cfg = config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    item = None
    if change['op'] in ('like', 'dislike'):
        if len(change['values']) != 1 or change['kind'] not in (None, 'movie', 'tv'):
            raise ValueError('title')
        item = resolve(s, change)
    else:
        if not change['values']:
            raise ValueError('values')
        probe = copy.deepcopy(change)
        probe['op'] = {'show': 'hide', 'include_genre': 'exclude_genre', 'unfocus': 'focus'}.get(change['op'], change['op'])
        for p in targets:
            trial = copy.deepcopy(cfg)
            edit(trial, p, probe)
            s.validate_config(trial, data=s.DATA)
            edit(cfg, p, change)
    op, kind, values = change['op'], change['kind'], change['values']
    display = ', '.join(str(v) for v in values)
    if op in ('exclude_genre', 'include_genre', 'focus', 'unfocus'):
        names = s.LANGUAGE_NAMES if kind == 'languages' else s.COUNTRY_NAMES if kind == 'countries' else {g.casefold(): g for g in s.TMDB_GENRES.get(kind, {}).values()}
        display = ', '.join(names.get(str(v).lower() if kind != 'countries' else str(v).upper(), str(v)) for v in values)
    if op == 'years':
        display = ' to '.join(str(v) if v is not None else 'any' for v in values)
    if item:
        display = item['title']
    verb = {'exclude_genre': 'No more', 'include_genre': 'More', 'focus': 'More', 'unfocus': 'Less', 'discovery': 'Discovery:'}.get(op, op.capitalize())
    name = verb+' '+display+' for '+', '.join(s.LABELS[p] for p in targets)
    plan = {'people': targets, 'change': change, 'before': before, 'item': item, 'name': name}
    plan['plan'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan
def journal(s):
    path = Path(s.DATA)/'commands.json'
    return json.loads(path.read_text()) if path.exists() else {}
def save_journal(s, entries):
    fd, temp = tempfile.mkstemp(dir=s.DATA, prefix='.commands-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(entries, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, Path(s.DATA)/'commands.json')
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
_LOCK = threading.RLock()
def apply(s, plan, *, cfg=None):
    from .setup import publish
    with _LOCK:
        s.refresh_preferences()
        current, entries = config(s), journal(s)
        if 'shelf' in plan:
            from . import shelves
            shelf = plan['shelf']
            s.shelf_change(shelf['scope'], shelf['op'], plan=shelf['plan'])
            p = shelf['scope']
            after = {'wishes': [w for w in shelves.read(s)['wishes'] if w['scope'] == p], 'count': shelves.slot_count(s, p)}
            entries[p] = (entries.get(p, []) + [{'before': None, 'shelf': after, 'event': None, 'name': plan['name']}])[-20:]
            save_journal(s, entries)
            return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}
        if cfg is None:
            for p in plan['people']:
                people(s, viewer=p)
                if current['taste']['people'].get(p, {}).get('prefs') != plan['before'][p]:
                    raise ValueError('Settings changed; preview again')
            cfg = copy.deepcopy(current)
        if 'change' in plan and not plan.get('item'):
            for p in plan['people']:
                edit(cfg, p, plan['change'])
        cfg = s.validate_config(cfg, data=s.DATA)
        for p in plan['people']:
            event = None
            if plan.get('item'):
                event = s.write_feedback({**plan['item'], 'who': p, 'action': 'love' if plan['change']['op'] == 'like' else 'not_for_us'})
            entries[p] = (entries.get(p, []) + [{'before': current['taste']['people'].get(p, {}).get('prefs'), 'shelf': None, 'event': event, 'name': plan['name']}])[-20:]
        publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
        save_journal(s, entries)
        from .shelves import counts
        if counts(current) != counts(cfg):
            s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+'. Search \'undo\' to revert.'}
def undo(s, targets):
    from .setup import publish
    with _LOCK:
        s.refresh_preferences()
        cfg, entries = config(s), journal(s)
        if not targets:
            raise ValueError('Add a member name to undo')
        for p in targets:
            if p != 'together' and s.kids_age(p) is None:
                people(s, viewer=p)
            if not entries.get(p):
                raise ValueError('No command to undo')
            if s.kids_age(p) is not None and not entries[p][-1].get('shelf'):
                raise PermissionError('Ask a grown-up to change settings')
        skipped = []
        for p in targets:
            old = entries[p].pop()
            if old.get('shelf'):
                s.shelf_change(p, 'undo', apply=True, expected=old.get('shelf'))
                cfg = config(s)
            elif old['event']:
                if any(e['id'] == old['event']['id'] for e in s.active_feedback(s.read_feedback()).values()):
                    s.write_feedback({k: v for k, v in old['event'].items() if k not in ('id', 't', 'undo')} | {'undo': old['event']['id']})
                else:
                    skipped.append(old['name'])
            else:
                traits = cfg['taste']['people'][p]
                traits.pop('prefs', None)
                if old['before'] is not None:
                    traits['prefs'] = old['before']
        from .shelves import counts
        count_changed = counts(config(s)) != counts(cfg)
        publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
        save_journal(s, entries)
        if count_changed:
            s.aio_rows_changed()
    return {'name': 'Undone for '+', '.join('Together' if p == 'together' else s.LABELS[p] for p in targets) + ('. Newer ratings kept; could not be undone: '+ '; '.join(skipped) if skipped else '')}
def card(s, name, stype, description=''):
    return {'metas': [{'id': s.rid('cmd:'+hashlib.sha256(name.encode()).hexdigest()[:12]), 'type': stype,
                       'name': name, 'description': description, 'poster': '/command.svg'}]}
def context(s):
    scope = getattr(s._scoping, 'scope', None)
    return scope, s.SCOPES.get(scope), s._viewer.get() or s.who_watching()[0]
def confirm_code(plan):
    return f"{int(plan['plan'][:8], 16) % 100:02d}"
def preview_card(s, plan, stype):
    return card(s, 'yes '+confirm_code(plan)+': '+plan['name'], stype, 'Type "yes '+confirm_code(plan)+'" to apply. Nothing changes until then.')
def remember_cards(s, metas, scope):
    with _LOCK:
        cache = s.__dict__.setdefault('_command_cards', {})
        now = s.time.time()
        for key in list(cache):
            if now - cache[key][0] >= 1800:
                del cache[key]
        for meta in metas:
            if meta.get('id', '').startswith(s.rid('cmd:')):
                key = (scope, meta['id'])
                cache.pop(key, None)
                cache[key] = (now, copy.deepcopy(meta))
        while len(cache) > 200:
            del cache[next(iter(cache))]
def card_meta(s, cid, stype, scope):
    with _LOCK:
        cache = s.__dict__.setdefault('_command_cards', {})
        at, meta = cache.get((scope, cid), (0, None))
        if meta is not None and s.time.time() - at < 1800:
            return {'meta': {**copy.deepcopy(meta), 'type': stype}}
        cache.pop((scope, cid), None)
        return {'meta': {'id': cid, 'type': stype, 'name': 'Skald command',
                         'description': 'Search again to see this card.', 'poster': '/command.svg'}}
def once(s, scope, query, action):
    with _LOCK:
        now = s.time.time()
        cache = s.__dict__.setdefault('_command_results', {}).setdefault(scope, {})
        for key in list(cache):
            if now - cache[key][0] >= 60:
                del cache[key]
        query = query.strip().casefold()
        if query not in cache:
            result = action()
            if result is None:
                return None
            cache[query] = (now, copy.deepcopy(result))
        return copy.deepcopy(cache[query][1])
def suggestion_cards(s, scope, viewer, bound, stype, dismiss=None):
    from .engine import suggestions
    person = shelf_scope(s, None, viewer, bound)
    plans = s.__dict__.setdefault('_command_suggestions', {}).setdefault(scope, {})
    if dismiss:
        plan = plans.get(dismiss[3:])
        if not plan:
            raise ValueError('Preview suggestions again')
        suggestions(s.DATA, person, s.CONFIG, plan['suggestion'])
        del plans[dismiss[3:]]
        return card(s, 'Suggestion dismissed', stype)
    plans.clear()
    pending(s).pop(scope, None)
    metas = []
    from . import shelves
    for row in suggestions(s.DATA, person, s.CONFIG)['suggestions'][:2]:
        w = row['when']
        parts = []
        start, end = w['from_hour'], w['to_hour']+1
        if start < 5:
            parts.append({'days': [shelves.DAYS[(d-1)%7] for d in w['days']], 'hours': [start+24, min(end, 5)+24]})
        if end > 5:
            parts.append({'days': [shelves.DAYS[d] for d in w['days']], 'hours': [max(start, 5), end]})
        for when in parts:
            if len(metas) == 2:
                break
            if len([w for w in shelves.read(s)['wishes'] if w['scope'] == person and w['slot'] <= shelves.slot_count(s, person)]) >= shelves.slot_count(s, person):
                return card(s, 'Shelves are full; stop a shelf first', stype)
            name = re.sub(r'^(?:a|an|the|some)\s+', '', row['name'], flags=re.I).title()
            shelf = s.shelf_change(person, 'add', text=row['text'], name=name, when=when)
            plan = dict(people=[person], shelf=shelf, plan=shelf['plan'], suggestion=row['id'], at=s.time.time(), name='Add shelf: '+name+' for '+s.LABELS[person]+'; '+str(shelf['when'])+' '+s.CONFIG['region']['tz'])
            code = confirm_code(plan)
            if code in plans:
                continue
            plans[code] = plan
            meta = preview_card(s, plan, stype)['metas'][0]
            meta['description'] += ' Type "no '+code+'" to dismiss.'
            metas.append(meta)
    return {'metas': metas} if metas else card(s, 'No suggestions yet', stype)

def special(s, query, stype, routed=False):
    with _LOCK:
        s.refresh_preferences()
        scope, bound, viewer = context(s)
        q = query.strip().casefold()
        mutate = q in ('undo', 'rückgängig') or bool(re.fullmatch(r'no \d{2}', q)) or (q.startswith('yes ') and len(q) == 6 and q[4:].isdigit())
        action = lambda: _special(s, query, stype, routed)
        result = once(s, scope, q, action) if q in ('help', '?', 'hilfe') or mutate and bound != s.GUEST and viewer != s.GUEST else action()
        if result is not None and stype != 'movie':
            return {'metas': []}
        for meta in (result or {}).get('metas', []):
            meta['type'] = stype
        return result
def _special(s, query, stype, routed=False):
    scope, bound, viewer = context(s)
    q = query.casefold().strip()
    help_q = q in ('help', '?', 'hilfe')
    ideas = q in ('ideas', 'suggest')
    local = parse_shelf(query, s, viewer, bound)['op'] is not None
    if q.startswith('stop ') and not local:
        return None
    dismissing = bool(re.fullmatch(r'no \d{2}', q))
    about = q in ('what do you know about me', 'my settings', 'settings', 'was weißt du über mich')
    named = None
    match = None if about else (re.fullmatch(r'(?:my )?settings(?: for)? (.+)', q) or re.fullmatch(r"(.+?)(?:'s)? (?:my )?settings", q))
    if match:
        about, named = True, member_name(s, match[1])
    confirming = q in ('y', 'ye', 'yes', 'ja', 'ok') or (q.startswith('yes ') and q[4:].isdigit())
    if not (help_q or ideas or dismissing or local or about or routed or confirming or q in ('undo', 'rückgängig')):
        return None
    try:
        if not help_q and (bound == s.GUEST or viewer == s.GUEST):
            return card(s, 'Ask a grown-up to change settings', stype)
        if ideas or dismissing:
            return suggestion_cards(s, scope, viewer, bound, stype, q if dismissing else None)
        if help_q:
            phrases = ('hide anime', 'no more horror', 'every sunday morning a classic for us', 'make me a row of heist films', 'my settings')
            result = card(s, 'Skald help', stype, '; '.join(phrases)+'. Type the exact yes code from the preview to apply; plain yes changes nothing. '+('Applies to '+s.LABELS[bound]+'.' if bound in s.PEOPLE else 'Add a member name, or choose Who\'s watching.'))
            for phrase in phrases:
                result['metas'].extend(card(s, 'Try: '+phrase, stype, 'Type "'+phrase+'" in search.')['metas'])
            return result
        if about:
            targets = [shelf_scope(s, named, viewer, bound)]
            model = s.get_model(False) or {}
            facets = model.get('paff', {}).get(targets[0], {})
            names = model.get('names', {})
            detail = json.dumps({'Together' if p == 'together' else s.LABELS[p]: s.CONFIG['taste']['people'].get(p, {}).get('prefs', {}) for p in targets}, ensure_ascii=False)
            from . import shelves
            for w in shelves.read(s)['wishes']:
                if w['scope'] in (*targets, 'together'):
                    detail += '\n'+w['name']+'; '+str(w['when'])+'; '+('Together' if w['scope'] == 'together' else s.LABELS[w['scope']])
            detail += '\nTop taste: '+', '.join(names.get(k, k) for k, v in sorted(facets.items(), key=lambda kv: -kv[1])[:5])
            detail += '\nRecent commands: '+ '; '.join(e['name'] for p in targets for e in journal(s).get(p, [])[-3:])
            return card(s, 'Settings for '+', '.join('Together' if p == 'together' else s.LABELS[p] for p in targets), stype, detail)
        plan = get_pending(s, scope)
        if confirming and q.startswith('yes '):
            plan = getattr(s, '_command_suggestions', {}).get(scope, {}).get(q[4:], plan)
            if plan and plan.get('suggestion') and s.time.time() - plan['at'] >= 600:
                raise ValueError('Preview suggestions again')
        if confirming:
            if plan is None:
                return None
            if q != 'yes '+confirm_code(plan):
                return preview_card(s, plan, stype)
            if 'shelf' in plan:
                shelf_scope(s, plan['shelf']['scope'], viewer, bound)
            elif bound in s.PEOPLE and plan['people'] != [bound]:
                raise PermissionError('This plan applies to another member')
            if 'shelf' not in plan:
                for target in plan['people']:
                    people(s, viewer=target, bound=bound)
            result = apply(s, plan)
            pending(s).pop(scope, None)
            getattr(s, '_command_suggestions', {}).pop(scope, None)
            s._commands_last = getattr(s, '_commands_last', {}) | {scope: plan['people']}
            return card(s, result['name'], stype)
        if q in ('undo', 'rückgängig'):
            targets = getattr(s, '_commands_last', {}).get(scope) or ([bound] if bound in s.PEOPLE else None) or people(s, viewer=viewer)
            for target in targets:
                shelf_scope(s, target, viewer, bound)
            return card(s, undo(s, targets)['name'], stype)
        pending(s).pop(scope, None)
        getattr(s, '_command_suggestions', {}).pop(scope, None)
        plan = preview(s, query, viewer, bound)
        if plan is None:
            return card(s, 'Skald cannot set this yet', stype)
        pending(s)[scope] = (s.time.time(), plan)
        return preview_card(s, plan, stype)
    except (ValueError, PermissionError) as e:
        return card(s, str(e) if isinstance(e, PermissionError) or 'name' in str(e) or 'shelf' in str(e).lower() or not routed else 'Skald cannot set this yet', stype)
def api_apply(s, req):
    with _LOCK:
        s.refresh_preferences()
        viewer = req['viewer']
        if viewer == s.GUEST:
            raise PermissionError('person')
        scope = 'api:'+req['client']['name']
        if req['body'] == {'undo': True}:
            targets = getattr(s, '_commands_last', {}).get(scope) or ([viewer] if viewer else [])
            for target in targets:
                shelf_scope(s, target, viewer, viewer)
        return once(s, scope, json.dumps(req['body'], sort_keys=True), lambda: _api_apply(s, req))
def _api_apply(s, req):
    viewer, body = req['viewer'], req['body']
    if viewer == s.GUEST:
        raise PermissionError('person')
    scope = 'api:'+req['client']['name']
    if body == {'undo': True}:
        targets = getattr(s, '_commands_last', {}).get(scope) or ([viewer] if viewer else [])
        for target in targets:
            shelf_scope(s, target, viewer, viewer)
        return undo(s, targets)
    plan = get_pending(s, scope)
    if not isinstance(body, dict) or set(body) != {'plan'} or not plan or body['plan'] != plan['plan']:
        raise ValueError('Preview again')
    if 'shelf' in plan:
        shelf_scope(s, plan['shelf']['scope'], viewer, viewer)
    elif s.kids_age(viewer) is not None:
        raise PermissionError('person')
    elif viewer and plan['people'] != [viewer]:
        raise PermissionError('person')
    result = apply(s, plan)
    pending(s).pop(scope, None)
    s._commands_last = getattr(s, '_commands_last', {}) | {scope: plan['people']}
    return result
def api_preview(s, req):
    s.refresh_preferences()
    body, viewer = req['body'], req['viewer']
    if viewer == s.GUEST:
        raise PermissionError('person')
    if not isinstance(body, dict) or set(body) - {'in', 'person', 'no_llm'} or not isinstance(body.get('in'), dict) or set(body['in']) != {'query'}:
        raise ValueError('body')
    person = body.get('person', viewer)
    if viewer and person != viewer:
        raise PermissionError('person')
    if type(body.get('no_llm', False)) is not bool:
        raise ValueError('no_llm')
    s.check({'query': str}, body['in'])
    if s.kids_age(viewer) is not None and not parse_shelf(body['in']['query'], s, person, viewer)['op']:
        raise PermissionError('person')
    scope = 'api:'+req['client']['name']
    pending(s).pop(scope, None)
    local = parse_shelf(body['in']['query'], s, person, viewer)
    answer = local if local['op'] else s.decide('parse_command', body['in'], allow_llm=not body.get('no_llm', False))['answer']
    plan = preview(s, body['in']['query'], person, viewer, answer=answer)
    if plan:
        pending(s)[scope] = (s.time.time(), plan)
    return {'answer': answer, 'preview': plan}
