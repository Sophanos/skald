"""Preview and apply one personal change, shared by search, CLI and API."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import threading
OPS = ('hide', 'show', 'exclude_genre', 'include_genre', 'focus', 'unfocus', 'discovery', 'years', 'like', 'dislike', 'seen', 'less', 'more', 'runtime', 'kids_age', 'settings', 'history', 'undo', 'redo', 'reset', 'shelf_list', 'shelf_add', 'shelf_stop', 'shelf_count', 'row_rename', 'row_hide', 'row_show', 'row_move')
OUT = {'op': (*OPS, None), 'kind': ('movie', 'tv', 'series', 'genre', 'people', 'languages', 'countries', None),
       'values': ([(str, int, None), 10], None), 'person': (str, None)}
NONE = dict(op=None, kind=None, values=[], person=None)

def parse_json(content):
    """Decode the first balanced object, preferring an explicit JSON fence."""
    fence = re.search(r'```json\s*(.*?)```', content, re.S | re.I)
    content = fence[1] if fence else content
    start = content.find('{')
    depth, quoted, escaped = 0, False, False
    for i in range(start, len(content)) if start >= 0 else ():
        char = content[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                answer = json.loads(content[start:i+1])
                return {**dict.fromkeys(OUT), **answer}
    raise json.JSONDecodeError('No balanced JSON object', content, max(start, 0))

def parse_local(text, s=None, viewer=None, bound=None):
    answer = parse_shelf(text, s, viewer, bound)
    if answer['op']:
        return answer
    raw = text.strip()
    q = raw.casefold()
    person = None
    match = re.fullmatch(r'(.+?) (?:for|für) (\w+)', raw, re.I)
    if match and match[2].casefold() != 'you':
        raw, person = match.groups()
        q = raw.casefold()
        person = None if person.casefold() == 'me' else 'together' if person.casefold() in ('us', 'together', 'uns', 'zusammen') else member_name(s, person) if s else person.casefold()
    op, kind, values = None, None, []
    if match := re.fullmatch(r'rename (.+?) to (.+)', raw, re.I):
        op, values = 'row_rename', [row_id(match[1]), match[2]]
    elif match := re.fullmatch(r'call (.+?) row (.+)', raw, re.I):
        op, values = 'row_rename', [row_id(match[1]), match[2]]
    elif match := re.fullmatch(r'(?:hide|remove) (.+)|(.+?) (?:reihe )?ausblenden', q):
        name = match[1] or match[2]
        key = row_id(name)
        if key and name != 'anime':
            op, values = 'row_hide', [key]
    elif match := re.fullmatch(r'(?:show|bring back) (.+)', q):
        key = row_id(match[1])
        if key and match[1] != 'anime':
            op, values = 'row_show', [key]
    elif match := re.fullmatch(r'(?:put|move) (.+?) (?:above|before) (.+)', q):
        op, values = 'row_move', [row_id(match[1]), row_id(match[2])]
    if op:
        if any(v is None for v in values):
            return dict(NONE)
        return dict(op=op, kind=None, values=values, person=person)
    if q in ('undo', 'rückgängig'):
        op = 'undo'
    elif match := re.fullmatch(r'(undo|rückgängig|bring back|redo|do again|wiederholen|nochmal) (.+)', q):
        op, values = ('redo' if match[1] in ('redo', 'do again', 'wiederholen', 'nochmal') else 'undo'), [match[2]]
    elif match := re.fullmatch(r'reset (filters|shelves)|reset (everything)(?: to default)?|(filter|regale|alles)(?: auf (?:standard|standardwerte))? zurücksetzen', q):
        choice = next(v for v in match.groups() if v)
        op, values = 'reset', [{'filter': 'filters', 'regale': 'shelves', 'alles': 'everything'}.get(choice, choice)]
    elif q in ('what did i change', 'history', 'verlauf', 'was habe ich geändert'):
        op = 'history'
    elif q in ('settings', 'my settings', 'what are my settings'):
        op = 'settings'
    elif q in ('show my shelves', 'list shelves', 'shelf list'):
        op = 'shelf_list'
    elif match := re.fullmatch(r'kids age (\d+)', q):
        op, values = 'kids_age', [int(match[1])]
    elif match := re.fullmatch(r'(?:hide|no) horror(?: (movies|series))?', q):
        op, kind, values = 'exclude_genre', {'movies': 'movie', 'series': 'series', None: None}[match[1]], ['Horror']
    elif match := re.fullmatch(r'no (movies|series) longer than (\d+) (minutes|hours)', q):
        op, kind, values = 'runtime', 'movie' if match[1] == 'movies' else 'series', [int(match[2]) * (60 if match[3] == 'hours' else 1)]
    elif match := re.fullmatch(r'(less|more|weniger) (horror|documentaries|comedies|reality-tv)', q):
        op, kind = 'less' if match[1] == 'weniger' else match[1], 'genre'
        values = [{'horror': 'Horror', 'documentaries': 'Documentary', 'comedies': 'Comedy', 'reality-tv': 'Reality'}[match[2]]]
    elif q == 'fewer series please':
        op, kind = 'less', 'series'
    elif match := re.fullmatch(r'I already watched (.+)', raw, re.I):
        op, values = 'seen', [match[1]]
    return dict(op=op, kind=kind, values=values, person=person) if op else dict(NONE)

def row_id(name):
    from .members import ROW_KEYS
    name = re.sub(r'^(?:the )| (?:row|reihe)$', '', name.strip().casefold()).strip()
    aliases = {'top 10': 'top10', 'top rated': 'toprated', 'top rated movies': 'toprated',
               'latest digital releases': 'digital', 'for you': 'foryou',
               'continue watching': 'episodes', 'continue watching & new episodes': 'episodes'}
    return aliases.get(name, name if name in ROW_KEYS else None)

def row_settings(cfg):
    return {key: copy.deepcopy(cfg['household'].get(key)) for key in ('rows', 'row_labels', 'row_order')}

def edit_rows(cfg, change, s):
    from .members import ROW_KEYS
    op, kind, values = change['op'], change['kind'], change['values']
    count = 2 if op in ('row_rename', 'row_move') else 1
    if kind is not None or len(values) != count or values[0] not in ROW_KEYS:
        raise ValueError('row id or values')
    rows = list(cfg['household'].get('rows', [key for key in ROW_KEYS if key not in ('digital', 'toprated')]))
    if op == 'row_rename':
        label = values[1]
        if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
            raise ValueError('row label')
        cfg['household'].setdefault('row_labels', {})[values[0]] = label.strip()
        return
    if op == 'row_hide':
        rows = [key for key in rows if key != values[0]]
    elif op == 'row_show':
        if values[0] not in rows:
            rows.append(values[0])
    else:
        if values[1] not in rows or values[0] not in rows or values[0] == values[1]:
            raise ValueError('move needs two different visible rows')
        rank = {cid: i for i, cid in enumerate(s.DEDUPE_ORDER)}
        groups = s.row_groups()
        order = list(cfg['household'].get('row_order', sorted(ROW_KEYS,
            key=lambda key: min((rank.get(cid, len(rank)) for cid in groups.get(key, [s.rid(key)])), default=len(rank)))))
        order.extend(key for key in ROW_KEYS if key not in order)
        order.remove(values[0])
        order.insert(order.index(values[1]), values[0])
        cfg['household']['row_order'] = order
        return
    cfg['household']['rows'] = rows
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

def shelf_schedule(q):
    from .shelves import DAYS
    days = dict(zip(('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'), DAYS))
    days.update(dict(zip(('montags', 'dienstags', 'mittwochs', 'donnerstags', 'freitags', 'samstags', 'sonntags'), DAYS)))
    days.update({d: d for d in DAYS})
    days.update(dict.fromkeys(('weekday', 'weekdays', 'wochentags', 'unter der woche'), 'weekdays'))
    days.update(dict.fromkeys(('weekend', 'am wochenende'), 'weekend'))
    timing = {'morning': '05-12', 'mornings': '05-12', 'morgens': '05-12',
              'afternoon': '12-18', 'nachmittags': '12-18', 'evening': '18-24', 'evenings': '18-24', 'abends': '18-24'}
    day_pattern = '|'.join(re.escape(d) for d in sorted(days, key=len, reverse=True))
    match = re.fullmatch(r'(?:every )?((?:'+day_pattern+r')(?:\s*(?:,|\band\b|\bund\b)\s*(?:'+day_pattern+r'))*)\s+(.+)', q)
    if not match:
        return None
    day_text, rest = match.groups()
    day_list = [days[d.strip()] for d in re.split(r'\s*(?:,|\band\b|\bund\b)\s*', day_text)]
    if len(day_list) > 1 and any(d not in DAYS for d in day_list):
        return None
    match = re.fullmatch(r'('+ '|'.join(timing) +r')\s+(.+)', rest)
    if match:
        hours, theme = timing[match[1]], match[2]
    else:
        match = re.fullmatch(r'(?:at |um )?(\d{1,2})(?::00)?(?:\s*(?:-|to|bis)\s*(\d{1,2})(?::00)?)?(?: Uhr| uhr)?\s+(.+)', rest)
        if not match:
            return None
        start, end, theme = match.groups()
        # An explicit start without an end lasts until midnight.
        hours = start+'-'+(end or '24')
        theme = re.sub(r'^(?:a|an|the)\s+', '', theme)
    theme = re.sub(r'^(?:einen?|eine)\s+', '', theme)
    theme = {'klassiker': 'classic', 'was kurzes': 'something short'}.get(theme, theme)
    return [theme, ','.join(dict.fromkeys(day_list)), hours]

def parse_shelf(text, s=None, viewer=None, bound=None):
    raw = text.strip()
    q = raw.casefold()
    creation = (r'(?:(?:create|make|add|new)\s+(?:me\s+)?(?:(?:a|an)\s+)?(?:new\s+)?'
                r'(?:catalog|row|shelf|list)\s+(?:about|for|with|of)|'
                r'(?:erstelle|erstell|mache|mach|füge)\s+(?:mir\s+)?(?:(?:ein|eine|einen)\s+)?'
                r'(?:neue[nrms]?\s+)?(?:katalog|reihe|regal|liste)\s+(?:über|für|mit|von)|'
                r'neue[nrms]?\s+(?:katalog|reihe|regal|liste)\s+(?:über|für|mit|von))\s+(.+)')
    person = None
    names = {'me': None}
    if s is not None:
        names.update({name.casefold(): p for p in s.PEOPLE for name in (p, s.LABELS[p])})
    names.update(dict.fromkeys(('us', 'both', 'both of us', 'everyone', 'together', 'uns', 'beide', 'zusammen', 'alle'), 'together'))
    for name in sorted(names, key=len, reverse=True):
        suffix = next((suffix for suffix in (' for '+name, ' für '+name) if q.endswith(suffix)), None)
        if suffix:
            person, q = names[name], q[:-len(suffix)]
            raw = raw[:-len(suffix)]
            break
    else:
        match = re.search(r' for (\w+)$', q)
        if match and not re.fullmatch(creation, raw, re.I):
            person, q = match[1], q[:match.start()]
            raw = raw[:match.start()]
    values, op = [], None
    schedule = shelf_schedule(q)
    if schedule:
        values, op = schedule, 'shelf_add'
    elif match := re.fullmatch(creation, raw, re.I):
        values, op = [match[1], 'always'], 'shelf_add'
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

def command_prompt(s):
    return ('Return JSON with every key always present: op, kind, values, person; null when unknown. Exactly one persistent personal change or op null. '
              'Do not guess person, kind, days or hours unless clearly stated. Title names, questions and ordinary one-off searches return op null, except explicit settings/list requests. '
              'Never choose hide/show for a genre: hide horror movies means exclude_genre Horror kind movie. '
              'Genre exclusion or inclusion without a type uses kind null and applies to movies and series. '
              'Only hide movies/series when asked to remove the whole type. Explicit shelf requests are changes. Ops: ' + ', '.join(OPS) + '. '
              'hide/show values: ' + ', '.join(s.HIDE_CHOICES) + '. Genre values use TMDB genre names; '
              'kind movie or series. focus/unfocus kind languages or countries uses two-letter codes, '
              'or movie/series uses genres. discovery values: balanced/mainstream/gems. years: [from,to], null ends allowed. '
              'focus/unfocus kind people uses explicitly named actors or directors. like/dislike/seen values: one title, kind movie or tv or null. person: member name, everyone or null. '
              'less/more kind genre uses genre names, or movie/series with empty values for a soft type weight, never a ban. '
              'runtime kind movie/series values [maximum minutes]; kids_age kind null values [age 3..17]. '
              'settings, shelf_list and history kind null values []. History includes what did I change and Verlauf. '
              'undo kind null values [] for the latest change or [thing] for a named past change; bring back horror is undo [horror]. '
              'redo / do again kind null values [thing] replay a past change. '
              'reset kind null values [filters], [shelves] or [everything]; reset everything to default and alles auf Standard zurücksetzen are reset [everything]. '
              'reset my taste is unsupported, op null. Never reset learned taste, ratings or viewing history. '
              'Row commands affect the household: row_rename values [row id,new label], row_hide/row_show [row id], row_move [row id,before row id]. '
              'Row ids: episodes,foryou,week,upcoming,catchup,rewatch,because,leap,top10,people,moods,lanes,docs,studios,anime,shelves,digital,toprated. '
              'Top 10 maps to top10, top rated to toprated, Latest Digital Releases to digital, For You to foryou. '
              'shelf_add kind null, values [theme, always] or [theme, days, hours] (comma-separated mon,tue,wed,thu,fri,sat,sun or weekdays/weekend/daily; clock hour window 05-12 or 18-24). '
              'Creating, adding or making a catalog, row, shelf or list about/for/with/of a theme is shelf_add [theme,always]. '
              'German examples: neue Reihe mit, erstelle einen Katalog über. Preserve theme text and typos. Best catalog of crime movies is a one-off search, op null. '
              'German days include montags, freitags, wochentags, am Wochenende; morgens means 05-12, abends means 18-24. '
              'shelf_stop kind null, values [shelf name]; shelf_count kind null, values [count 0..10] or [fewer]. '
              'Shelf person: member name, together for us, or null for me. Title names are not commands. '
              'Unsupported settings return op null. Do not invent an operation.')
def register(s):
    prompt = command_prompt(s)
    model = getattr(s, 'COMMAND_MODEL', s.PICK_MODEL)
    effort = getattr(s, 'COMMAND_EFFORT', 'low')
    fallback_model = getattr(s, 'COMMAND_FALLBACK_MODEL', 'z-ai/glm-5.3-flash')
    fallback_effort = getattr(s, 'COMMAND_FALLBACK_EFFORT', 'low')
    def request(query, model, effort):
        return {'model': model, 'messages': [{'role': 'system', 'content': prompt},
                                           {'role': 'user', 'content': query}],
                'response_format': {'type': 'json_object'}, 'reasoning': {'effort': effort},
                'max_tokens': 200 if effort == 'none' else 1200}
    def parse(reply):
        return parse_json(reply['choices'][0]['message']['content'])
    def rule(inp, params):
        answer = parse_local(inp['query'], s)
        q = inp['query'].strip().casefold()
        return (answer, 1) if answer['op'] or q.startswith('stop ') or q in ('sunday bloody sunday', 'heist', 'yes man', 'reset my taste') or re.fullmatch(r'best (?:catalog|row|shelf|list) (?:of|about|for|with) .+', q) else None
    def fallback_request(batch):
        body = request(batch[0]['query'], fallback_model, fallback_effort)
        body['max_tokens'] = 1200
        return body
    s.decision('parse_command', ver=6, lane='search', cap=None, ttl=30*s.DAY,
        inp={'query': str}, out=OUT, norm=s.route_norm, rule=rule, params={},
        llm={'path': '/api/v1/chat/completions', 'model': model, 'batch': 1, 'timeout': 6,
             'prompt': lambda b: request(b[0]['query'], model, effort),
             'parse': lambda r, b: [(parse(r), 1)],
             'fallback': {'path': '/api/v1/chat/completions', 'model': fallback_model, 'batch': 1, 'timeout': 6,
                          'prompt': fallback_request,
                          'parse': lambda r, b: [(parse(r), 1)]}},
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
    if op in ('less', 'more'):
        if kind not in ('genre', 'movie', 'series') or kind == 'genre' and not values or kind != 'genre' and values:
            raise ValueError('soft weight kind or values')
        weights = prefs.setdefault('weights', {})
        if kind == 'genre':
            genres = weights.setdefault('genre', {})
            for genre in values:
                genres[genre] = -1 if op == 'less' else 1
        else:
            weights[kind] = -1 if op == 'less' else 1
        return
    if op in ('runtime', 'kids_age'):
        if len(values) != 1 or type(values[0]) is not int:
            raise ValueError(op)
        if op == 'runtime':
            if kind not in ('movie', 'series') or not 1 <= values[0] <= 600:
                raise ValueError('runtime kind or minutes (1..600)')
            prefs.setdefault('runtime', {})[kind] = values[0]
        else:
            if kind is not None or not 3 <= values[0] <= 17:
                raise ValueError('kids_age (3..17)')
            prefs['kids_age'] = values[0]
        return
    if op in ('discovery', 'years'):
        if op == 'discovery' and len(values) != 1:
            raise ValueError('discovery')
        prefs[op] = values[0] if op == 'discovery' else values
        return
    target, key = prefs, 'hide'
    if op in ('exclude_genre', 'include_genre'):
        if kind is None:
            for stype in ('movie', 'series'):
                edit(cfg, person, {**change, 'kind': stype})
            return
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
    if change['op'] == 'seen':
        hits = []
        for kind in (('movie', 'tv') if change['kind'] is None else (change['kind'],)):
            hit = s.resolve_pick(kind, title, None, s.LANG)
            if hit:
                item, _ = hit
                hits.append({'key': 'tmdb:'+str(item['id']), 'kind': kind,
                             'title': item.get('title') or item.get('name'),
                             'year': (item.get('release_date') or item.get('first_air_date') or '')[:4]})
        if len(hits) != 1:
            raise ValueError('Unknown or ambiguous title; specify movie or series')
        return hits[0]
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
    local = parse_local(text, s, viewer, bound)
    if text.strip().casefold().startswith('stop ') and not local['op']:
        return None
    change = copy.deepcopy(answer if answer is not None else local if local['op'] else s.decide('parse_command', {'query': text})['answer'])
    change = {**dict.fromkeys(OUT), **change}
    s.check(OUT, change)
    change['values'] = change['values'] or []
    if change['op'] == 'reset':
        return reset_preview(s, text, change, viewer, bound)
    if change['op'] == 'redo' or change['op'] == 'undo' and change['values']:
        return history_preview(s, text, change, viewer, bound)
    if change['op'] in ('settings', 'shelf_list', 'history'):
        if change['kind'] is not None or change['values']:
            raise ValueError('read-only settings command')
        target = shelf_scope(s, change['person'], viewer, bound)
        if change['op'] == 'history':
            details = [relative_time(s.time.time(), e.get('t'))+': '+e['name']+(' (undone)' if e.get('undone') else '')
                       for e in reversed(journal(s).get(target, []))]
            return dict(read_only=True, people=[target], name='History for '+scope_label(s, target),
                        description='\n'.join(details) or 'No changes yet')
        from . import shelves
        details = []
        if change['op'] == 'settings':
            details.append(json.dumps(s.CONFIG['taste']['people'].get(target, {}).get('prefs', {}), ensure_ascii=False))
            details.append('Household rows: '+json.dumps(row_settings(s.CONFIG), ensure_ascii=False))
        details.extend(w['name']+'; '+str(w['when']) for w in shelves.read(s)['wishes'] if w['scope'] in (target, 'together'))
        return dict(read_only=True, people=[target], name=('Settings' if change['op'] == 'settings' else 'Shelves')+' for '+('Together' if target == 'together' else s.LABELS[target]), description='\n'.join(details) or 'No shelves yet')
    if change['op'] and change['op'].startswith('shelf_'):
        return {**shelf_preview(s, change, viewer, bound), 'text': text[:200], 'source': 'search'}
    people(s, viewer=viewer, bound=bound) if bound in (*s.PEOPLE, s.GUEST) or viewer in s.PEOPLE else None
    if change['op'] is None:
        return None
    targets = [shelf_scope(s, change['person'], viewer, bound)] if change['op'] in ('undo', 'kids_age') else people(s, change['person'], viewer, bound)
    cfg = config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    item = None
    if change['op'] in ('like', 'dislike', 'seen'):
        if len(change['values']) != 1 or change['kind'] not in (None, 'movie', 'tv') or not isinstance(change['values'][0], str) or not change['values'][0].strip():
            raise ValueError('title')
        item = resolve(s, change)
    elif change['op'].startswith('row_'):
        if len(targets) != 1:
            raise ValueError('Choose one adult for household rows')
        edit_rows(cfg, change, s)
        s.validate_config(cfg, data=s.DATA)
    elif change['op'] != 'undo':
        if not change['values'] and change['op'] not in ('less', 'more'):
            raise ValueError('values')
        probe = copy.deepcopy(change)
        probe['op'] = {'show': 'hide', 'include_genre': 'exclude_genre', 'unfocus': 'focus'}.get(change['op'], change['op'])
        for p in targets:
            trial = copy.deepcopy(cfg)
            edit(trial, p, probe)
            s.validate_config(trial, data=s.DATA)
            edit(cfg, p, change)
    elif change['kind'] is not None or change['values']:
        raise ValueError('undo')
    op, kind, values = change['op'], change['kind'], change['values']
    display = ', '.join(str(v) for v in values)
    if op in ('exclude_genre', 'include_genre', 'focus', 'unfocus'):
        names = s.LANGUAGE_NAMES if kind == 'languages' else s.COUNTRY_NAMES if kind == 'countries' else {g.casefold(): g for g in s.TMDB_GENRES.get(kind, {}).values()}
        display = ', '.join(names.get(str(v).lower() if kind != 'countries' else str(v).upper(), str(v)) for v in values)
    if op == 'years':
        display = ' to '.join(str(v) if v is not None else 'any' for v in values)
    if item:
        display = item['title']
    if op in ('less', 'more') and not values:
        display = kind
    if op == 'runtime':
        display = kind+' up to '+display+' minutes'
    verb = {'exclude_genre': 'No more', 'include_genre': 'More', 'focus': 'More', 'unfocus': 'Less', 'discovery': 'Discovery:'}.get(op, op.capitalize())
    name = verb+' '+display+' for '+', '.join(s.LABELS[p] for p in targets)
    if op.startswith('row_'):
        if op == 'row_rename':
            name = 'Rename '+values[0]+' to '+values[1]
        elif op == 'row_move':
            name = 'Move '+values[0]+' before '+values[1]
        else:
            name = ('Hide ' if op == 'row_hide' else 'Show ')+values[0]
        name += ' for the household'
    plan = {'people': targets, 'change': change, 'before': before, 'item': item, 'name': name,
            'text': text[:200], 'source': 'search'}
    if op.startswith('row_'):
        plan['rows_before'] = row_settings(config(s))
    if op in ('undo', 'kids_age'):
        plan['actor'] = bound if bound in s.PEOPLE else viewer if viewer in s.PEOPLE else change['person']
        people(s, viewer=plan['actor'])
    if op == 'undo':
        plan['undo_before'] = undo_stamp(journal(s), targets)
    plan['plan'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan
def journal(s):
    path = Path(s.DATA)/'commands.json'
    entries = json.loads(path.read_text()) if path.exists() else {}
    cutoff = s.time.time() - 180*86400
    for rows in entries.values():
        for entry in rows:
            for key, value in dict(t=None, text='', source=None, change=None, after=None).items():
                entry.setdefault(key, value)
    return {p: [e for e in rows if e.get('t') is None or e['t'] >= cutoff][-200:]
            for p, rows in entries.items()}

def scope_label(s, scope):
    return 'Together' if scope == 'together' else s.LABELS[scope]

def relative_time(now, then):
    if then is None:
        return 'Older change (date unknown)'
    seconds = max(0, int(now - then))
    for unit, size in (('year', 365*86400), ('month', 30*86400), ('week', 7*86400),
                       ('day', 86400), ('hour', 3600), ('minute', 60)):
        if seconds >= size:
            n = seconds // size
            return str(n)+' '+unit+('s' if n != 1 else '')+' ago'
    return 'Just now'

def seal(plan):
    plan['plan'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan

def history_match(entries, query, redo=False):
    def words(text):
        return re.findall(r'\w+', str(text).casefold())
    tokens = set(words(query)) - {'the', 'shelf', 'row', 'regal', 'reihe'}
    hits = []
    for i, entry in enumerate(entries):
        if entry.get('undone') and not redo:
            continue
        change = entry.get('change') or {}
        fields = [entry.get('text', ''), entry['name'], str(change.get('op', '')).replace('_', ' '),
                  *change.get('values', [])]
        if change.get('text'):
            fields.append(change['text'])
        found = set(words(' '.join(map(str, fields))))
        if tokens and tokens <= found:
            exact = any(words(query) == words(field) for field in fields)
            hits.append((exact, i, entry))
    if not hits:
        raise ValueError('No matching settings change in history')
    return max(hits, key=lambda hit: hit[:2])[2]

def inverse_prefs(current, before, after, path=()):
    """Invert only this entry's delta; unrelated current values survive."""
    result = copy.deepcopy(current or {})
    before, after = before or {}, after or {}
    for key in before.keys() | after.keys():
        old, new, value = before.get(key), after.get(key), result.get(key)
        if old == new:
            continue
        here = (*path, key)
        if isinstance(old, dict) or isinstance(new, dict):
            restored = inverse_prefs(value, old, new, here)
        elif here[0] in ('hide', 'excluded_genres', 'focus'):
            removed = {str(v).casefold() for v in (new or []) if v not in (old or [])}
            restored = [v for v in (value or []) if str(v).casefold() not in removed]
            restored += [v for v in (old or []) if v not in (new or []) and v not in restored]
        else:
            if value != new:
                raise ValueError('.'.join(here)+' changed since this command; cannot undo it')
            restored = copy.deepcopy(old)
        result.pop(key, None)
        if restored is not None and (restored or key in before):
            result[key] = restored
    return result

def inverse_settings(cfg, person, before, after, change):
    inverse = {'hide': 'show', 'show': 'hide', 'exclude_genre': 'include_genre',
               'include_genre': 'exclude_genre', 'focus': 'unfocus', 'unfocus': 'focus'}
    if change['op'] in inverse:
        edit(cfg, person, {**change, 'op': inverse[change['op']]})
    else:
        current = cfg['taste']['people'].get(person, {}).get('prefs')
        set_prefs(cfg, person, inverse_prefs(current, before, after) or None)

def history_preview(s, text, change, viewer, bound):
    if change['kind'] is not None or len(change['values']) != 1 or not isinstance(change['values'][0], str):
        raise ValueError('Name one settings change to undo or redo')
    scope = shelf_scope(s, change['person'], viewer, bound)
    entries = journal(s)
    entry = history_match(entries.get(scope, []), change['values'][0], change['op'] == 'redo')
    original = entry.get('change')
    if not original:
        raise ValueError('This older command supports plain undo only')
    if bound in s.PEOPLE and any(p != bound for p in entry.get('prefs_before', {})):
        raise PermissionError('This plan applies to another member')
    if change['op'] == 'redo':
        if entry.get('shelf') and original['op'] != 'reset':
            shelf_scope(s, scope, viewer, bound)
            action = {k: original[k] for k in ('text', 'name', 'when', 'slot', 'count')}
            if original['op'] == 'add':
                action['slot'] = None
            elif original['op'] == 'stop':
                removed = [w for w in entry['shelf_before']['wishes'] if w not in entry['shelf']['wishes']]
                hits = [w for w in shelf_state(s, scope)['wishes'] if any(w['id'] == old['id'] for old in removed)]
                if len(hits) != 1:
                    raise ValueError('Shelf changed; cannot redo this stop')
                action['slot'] = hits[0]['slot']
            shelf = s.shelf_change(scope, original['op'], **action)
            return dict(people=[scope], shelf=shelf, plan=shelf['plan'], name='Redo: '+entry['name'], text=text[:200], source='search')
        return preview(s, text, viewer, bound, answer={**original, 'person': scope})
    if entry.get('event') or entry.get('rows'):
        raise ValueError('This command supports plain undo only')
    actor = bound if bound in s.PEOPLE else viewer
    if not entry.get('shelf') or original['op'] == 'reset' and original['values'] != ['shelves']:
        people(s, viewer=actor)
    cfg = config(s)
    affected = entry.get('prefs_before', {scope: entry['before']} if scope in s.PEOPLE else {})
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in affected}
    for p in before:
        inverse_settings(cfg, p, affected[p], entry.get('prefs_after', {scope: entry['after']})[p], original)
    if entry.get('shelf'):
        inverse_shelves(shelf_state(s, scope), entry['shelf_before'], entry['shelf'])
    index = max(i for i, e in enumerate(entries[scope]) if e == entry)
    return seal(dict(people=[scope], change=change, before=before, target=entry, target_index=index,
                     actor=actor, undo_before=undo_stamp(entries, [scope]),
                     name='Undo: '+entry['name'], text=text[:200], source='search'))

def targeted_undo(s, plan, current, entries):
    scope = plan['people'][0]
    if undo_stamp(entries, [scope]) != plan['undo_before']:
        raise ValueError('Commands changed; preview undo again')
    entry = entries[scope][plan['target_index']]
    shelf_scope(s, scope, plan['actor'], None)
    if not entry.get('shelf') or entry['change']['op'] == 'reset' and entry['change']['values'] != ['shelves']:
        people(s, viewer=plan['actor'])
    cfg = copy.deepcopy(current)
    old_prefs = entry.get('prefs_before', {scope: entry['before']})
    new_prefs = entry.get('prefs_after', {scope: entry['after']})
    for p in plan['before']:
        inverse_settings(cfg, p, old_prefs[p], new_prefs[p], entry['change'])
    state = inverse_shelves(shelf_state(s, scope), entry['shelf_before'], entry['shelf']) if entry.get('shelf') else None
    if state:
        set_count(cfg, scope, state['count'])
    publish_state(s, cfg, scope, state)
    if entry.get('shelf_before') and entry['change']['op'] != 'reset':
        from .shelves import consume_undo
        consume_undo(s, scope, entry['shelf_before'], entry['change']['op'])
    entry['undone'] = True
    save_journal(s, entries)
    return {'name': 'Undone: '+entry['name']}

def shelf_state(s, scope):
    from . import shelves
    return dict(wishes=copy.deepcopy([w for w in shelves.read(s)['wishes'] if w['scope'] == scope]),
                count=shelves.slot_count(s, scope))

def inverse_shelves(current, before, after):
    result = copy.deepcopy(current)
    if before['count'] != after['count']:
        if current['count'] != after['count']:
            raise ValueError('Shelf count changed since this command; cannot undo it')
        result['count'] = before['count']
    old = {w['id']: w for w in before['wishes']}
    new = {w['id']: w for w in after['wishes']}
    result['wishes'] = [w for w in result['wishes'] if w['id'] not in new or w['id'] in old]
    for key, wish in old.items():
        if key not in new and not any(w['id'] == key for w in result['wishes']):
            if any(w['slot'] == wish['slot'] for w in result['wishes']) or wish['slot'] > result['count']:
                raise ValueError('Shelf slot changed since this command; cannot undo it')
            result['wishes'].append(wish)
    return result

def set_prefs(cfg, person, prefs):
    traits = cfg['taste']['people'].setdefault(person, {})
    traits.pop('prefs', None)
    if prefs is not None:
        traits['prefs'] = copy.deepcopy(prefs)

def set_count(cfg, scope, count):
    if scope == 'together':
        if cfg['household'].get('shelves', 2) != count:
            cfg['household']['shelves'] = count
    elif cfg['taste']['people'].get(scope, {}).get('prefs', {}).get('shelves', 4) != count:
        cfg['taste']['people'].setdefault(scope, {}).setdefault('prefs', {})['shelves'] = count

def publish_state(s, cfg, scope, state=None):
    from . import shelves
    from .setup import publish
    changed = shelves.counts(config(s)) != shelves.counts(cfg)
    cfg = s.validate_config(cfg, data=s.DATA)
    if state is not None:
        doc = shelves.read(s)
        changed |= shelf_state(s, scope) != state
        doc['wishes'] = [w for w in doc['wishes'] if w['scope'] != scope] + state['wishes']
        doc['plans'].clear()
        shelves.save(s, doc)
    publish(Path(s.DATA), cfg, replace=True)
    s.refresh_preferences()
    if changed:
        s.aio_rows_changed()

def reset_preview(s, text, change, viewer, bound):
    if change['kind'] is not None or change['values'] not in (['filters'], ['shelves'], ['everything']):
        raise ValueError('Reset filters, shelves or everything to default')
    scope = shelf_scope(s, change['person'], viewer, bound)
    actor = bound if bound in s.PEOPLE else viewer
    choice = change['values'][0]
    if bound in s.PEOPLE and scope == 'together' and choice != 'shelves':
        raise PermissionError('Use a shared link to reset Together filters')
    if choice != 'shelves':
        people(s, viewer=actor)
    targets = [p for p in s.PEOPLE if s.kids_age(p) is None] if scope == 'together' else [scope]
    cfg = config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    after, details = {}, []
    for p, prefs in before.items():
        kept = copy.deepcopy(prefs or {})
        removed = {k: v for k, v in kept.items() if (k == 'shelves') == (choice == 'shelves') or choice == 'everything'}
        if scope == 'together':
            removed.pop('shelves', None)
        for key in removed:
            kept.pop(key)
        after[p] = kept or None
        if removed:
            details.append(scope_label(s, p)+': '+json.dumps(removed, ensure_ascii=False))
    # Together shelves have their own count; personal shelf settings stay personal.
    if scope == 'together' and choice == 'shelves':
        before, after = {}, {}
    plan = dict(people=[scope], change=change, before=before, prefs_after=after, actor=actor,
                name='Reset '+choice+' to default for '+scope_label(s, scope), text=text[:200], source='search', reset=True)
    if choice != 'filters':
        state = shelf_state(s, scope)
        plan.update(shelf_before=state, shelf_after=dict(wishes=[], count=2 if scope == 'together' else 4))
        details.extend('Clear shelf: '+w['name'] for w in state['wishes'])
        details.append('Shelf count: '+str(state['count'])+' to '+str(plan['shelf_after']['count']))
        if scope == 'together':
            plan['household_shelves_before'] = cfg['household'].get('shelves')
    plan['description'] = 'Clear settings:\n'+'\n'.join(details) if details else 'Already at default'
    return seal(plan)

def apply_reset(s, plan, current, entries):
    scope = plan['people'][0]
    shelf_scope(s, scope, plan['actor'], None)
    if plan['change']['values'] != ['shelves']:
        people(s, viewer=plan['actor'])
    cfg = copy.deepcopy(current)
    for p, before in plan['before'].items():
        if current['taste']['people'].get(p, {}).get('prefs') != before:
            raise ValueError('Settings changed; preview reset again')
        set_prefs(cfg, p, plan['prefs_after'][p])
    if 'shelf_before' in plan and shelf_state(s, scope) != plan['shelf_before']:
        raise ValueError('Shelves changed; preview reset again')
    if 'household_shelves_before' in plan:
        cfg['household'].pop('shelves', None)
    entry = history_entry(s, plan, plan['before'].get(scope), plan['prefs_after'].get(scope))
    entry.update(prefs_before=plan['before'], prefs_after=plan['prefs_after'])
    if 'shelf_before' in plan:
        entry.update(shelf_before=plan['shelf_before'], shelf=plan['shelf_after'])
    if 'household_shelves_before' in plan:
        entry['household_shelves_before'] = plan['household_shelves_before']
    publish_state(s, cfg, scope, plan.get('shelf_after'))
    entries[scope] = (entries.get(scope, []) + [entry])[-200:]
    save_journal(s, entries)
    return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}

def history_entry(s, plan, before, after, **fields):
    return dict(before=copy.deepcopy(before), after=copy.deepcopy(after), shelf=None, event=None,
                name=plan['name'], t=int(s.time.time()), text=plan.get('text', '')[:200],
                source=plan.get('source', 'search'), change=copy.deepcopy(plan.get('change') or plan.get('shelf')),
                **fields)

def undo_stamp(entries, targets):
    return hashlib.sha256(json.dumps({p: entries.get(p, []) for p in targets}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
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
    if plan.get('read_only'):
        return plan
    with _LOCK:
        from .migrate import ensure
        ensure(s.DATA)
        s.refresh_preferences()
        if plan.get('target') or plan.get('reset'):
            from . import shelves
            with shelves.locked(s):
                s.refresh_preferences()
                current, entries = config(s), journal(s)
                return targeted_undo(s, plan, current, entries) if plan.get('target') else apply_reset(s, plan, current, entries)
        current, entries = config(s), journal(s)
        if plan.get('change', {}).get('op') == 'undo':
            people(s, viewer=plan['actor'])
            if undo_stamp(entries, plan['people']) != plan['undo_before']:
                raise ValueError('Commands changed; preview undo again')
            return undo(s, plan['people'], actor=plan['actor'])
        if 'shelf' in plan:
            from . import shelves
            shelf = plan['shelf']
            before_shelf = shelf_state(s, shelf['scope'])
            s.shelf_change(shelf['scope'], shelf['op'], plan=shelf['plan'])
            p = shelf['scope']
            after = {'wishes': [w for w in shelves.read(s)['wishes'] if w['scope'] == p], 'count': shelves.slot_count(s, p)}
            entry = history_entry(s, plan, current['taste']['people'].get(p, {}).get('prefs'),
                                  config(s)['taste']['people'].get(p, {}).get('prefs'))
            entry['shelf'] = after
            entry['shelf_before'] = before_shelf
            entries[p] = (entries.get(p, []) + [entry])[-200:]
            save_journal(s, entries)
            return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}
        if cfg is None:
            if 'rows_before' in plan and row_settings(current) != plan['rows_before']:
                raise ValueError('Rows changed; preview again')
            for p in plan['people']:
                people(s, viewer=plan['actor'] if plan.get('change', {}).get('op') == 'kids_age' else p)
                if current['taste']['people'].get(p, {}).get('prefs') != plan['before'][p]:
                    raise ValueError('Settings changed; preview again')
            cfg = copy.deepcopy(current)
        if 'change' in plan and not plan.get('item'):
            if 'rows_before' in plan:
                edit_rows(cfg, plan['change'], s)
            else:
                for p in plan['people']:
                    edit(cfg, p, plan['change'])
        cfg = s.validate_config(cfg, data=s.DATA)
        for p in plan['people']:
            event = None
            if plan.get('item'):
                event = s.write_feedback({**plan['item'], 'who': p, 'action': {'like': 'love', 'dislike': 'not_for_us', 'seen': 'seen'}[plan['change']['op']]})
            entry = history_entry(s, plan, current['taste']['people'].get(p, {}).get('prefs'),
                                  cfg['taste']['people'].get(p, {}).get('prefs'))
            entry['event'] = event
            if plan.get('change', {}).get('op') == 'kids_age':
                entry['kids_age'] = True
            if 'rows_before' in plan:
                entry['rows'] = {'before': row_settings(current), 'after': row_settings(cfg)}
            entries[p] = (entries.get(p, []) + [entry])[-200:]
        publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
        save_journal(s, entries)
        from .shelves import counts
        if counts(current) != counts(cfg) or row_settings(current) != row_settings(cfg):
            s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+'. Search \'undo\' to revert.'}
def undo(s, targets, actor=None):
    from . import shelves
    from .setup import publish
    with _LOCK, shelves.locked(s):
        from .migrate import ensure
        ensure(s.DATA)
        s.refresh_preferences()
        cfg, entries = config(s), journal(s)
        if not targets:
            raise ValueError('Add a member name to undo')
        for p in targets:
            if p != 'together' and s.kids_age(p) is None:
                people(s, viewer=p)
            active = [e for e in entries.get(p, []) if not e.get('undone')]
            if not active:
                raise ValueError('No command to undo')
            rows = active[-1].get('rows')
            if rows and row_settings(cfg) != rows['after']:
                raise ValueError('Rows changed; cannot undo an older row command')
            settings = not active[-1].get('shelf') or (active[-1].get('change') or {}).get('op') == 'reset' and active[-1]['change']['values'] != ['shelves']
            if s.kids_age(p) is not None and settings and not (actor in s.PEOPLE and s.kids_age(actor) is None and (active[-1].get('kids_age') or active[-1].get('prefs_before') is not None)):
                raise PermissionError('Ask a grown-up to change settings')
        skipped = []
        for p in targets:
            old = next(e for e in reversed(entries[p]) if not e.get('undone'))
            if old.get('prefs_before') is not None or old.get('shelf_before'):
                if old.get('shelf') and shelf_state(s, p) != old['shelf']:
                    raise ValueError('Shelves changed; cannot undo this command')
                for target, prefs in old.get('prefs_before', {p: old['before']} if p in s.PEOPLE else {}).items():
                    if 'prefs_before' not in old or old['change'].get('values') == ['shelves']:
                        after = old.get('prefs_after', {p: old['after']})[target]
                        prefs = inverse_prefs(cfg['taste']['people'].get(target, {}).get('prefs'), prefs, after) or None
                    set_prefs(cfg, target, prefs)
                if 'household_shelves_before' in old:
                    cfg['household'].pop('shelves', None)
                    if old['household_shelves_before'] is not None:
                        cfg['household']['shelves'] = old['household_shelves_before']
                elif old.get('shelf_before'):
                    set_count(cfg, p, old['shelf_before']['count'])
                publish_state(s, cfg, p, old.get('shelf_before'))
                if old.get('shelf_before') and old['change']['op'] != 'reset':
                    shelves.consume_undo(s, p, old['shelf_before'], old['change']['op'])
                cfg = config(s)
            elif old.get('shelf'):
                shelves._change(s, p, 'undo', apply=True, expected=old.get('shelf'))
                cfg = config(s)
            elif old.get('rows'):
                for key, value in old['rows']['before'].items():
                    cfg['household'].pop(key, None)
                    if value is not None:
                        cfg['household'][key] = value
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
            old['undone'] = True
        from .shelves import counts
        count_changed = counts(config(s)) != counts(cfg) or row_settings(config(s)) != row_settings(cfg)
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
    return card(s, 'yes '+confirm_code(plan)+': '+plan['name'], stype, 'Type "yes '+confirm_code(plan)+'" to apply. Nothing changes until then.'+('\n'+plan['description'] if plan.get('description') else ''))
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
def suggestion_cards(s, scope, viewer, bound, stype, dismiss=None, *, text=''):
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
            plan = dict(people=[person], shelf=shelf, plan=shelf['plan'], suggestion=row['id'], at=s.time.time(), text=text[:200], name='Add shelf: '+name+' for '+s.LABELS[person]+'; '+str(shelf['when'])+' '+s.CONFIG['region']['tz'])
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
    local = parse_local(query, s, viewer, bound)['op'] is not None
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
            return suggestion_cards(s, scope, viewer, bound, stype, q if dismissing else None, text=query)
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
            detail += '\nHousehold rows: '+json.dumps(row_settings(s.CONFIG), ensure_ascii=False)
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
            elif bound in s.PEOPLE and plan['people'] != [bound] and not (plan.get('reset') or plan.get('target')):
                raise PermissionError('This plan applies to another member')
            if 'shelf' not in plan:
                if plan.get('reset') or plan.get('target'):
                    for target in plan['people']:
                        shelf_scope(s, target, viewer, bound)
                    if bound in s.PEOPLE and any(p != bound for p in plan['before']):
                        raise PermissionError('This plan applies to another member')
                    plan['actor'] = bound if bound in s.PEOPLE else viewer
                elif plan.get('change', {}).get('op') in ('undo', 'kids_age'):
                    people(s, viewer=viewer, bound=bound)
                    for target in plan['people']:
                        shelf_scope(s, target, viewer, bound)
                else:
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
            return card(s, undo(s, targets, actor=viewer)['name'], stype)
        pending(s).pop(scope, None)
        getattr(s, '_command_suggestions', {}).pop(scope, None)
        plan = preview(s, query, viewer, bound)
        if plan is None:
            return card(s, 'Skald cannot set this yet', stype)
        if plan.get('read_only'):
            return card(s, plan['name'], stype, plan['description'])
        pending(s)[scope] = (s.time.time(), plan)
        return preview_card(s, plan, stype)
    except (ValueError, PermissionError) as e:
        return card(s, str(e) if isinstance(e, PermissionError) or 'name' in str(e) or 'shelf' in str(e).lower() or 'changed since' in str(e) or not routed else 'Skald cannot set this yet', stype)
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
    elif plan.get('reset') or plan.get('target'):
        for target in plan['people']:
            shelf_scope(s, target, viewer, viewer)
        if viewer in s.PEOPLE and any(p != viewer for p in plan['before']):
            raise PermissionError('person')
        plan['actor'] = viewer or plan['actor']
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
    local = parse_local(body['in']['query'], s, person, viewer)
    if s.kids_age(viewer) is not None and local['op'] not in ('history', 'settings', 'shelf_list', 'undo', 'redo', 'reset', 'shelf_add', 'shelf_stop', 'shelf_count'):
        raise PermissionError('person')
    scope = 'api:'+req['client']['name']
    pending(s).pop(scope, None)
    answer = local if local['op'] else s.decide('parse_command', body['in'], allow_llm=not body.get('no_llm', False))['answer']
    plan = preview(s, body['in']['query'], person, viewer, answer=answer)
    if plan and not plan.get('read_only'):
        plan['source'] = 'api'
        pending(s)[scope] = (s.time.time(), plan)
    return {'answer': answer, 'preview': plan}
