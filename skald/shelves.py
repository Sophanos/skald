"""Fixed addon slots for private standing wishes."""
import copy
from contextlib import contextmanager
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import uuid
from .intake import write_private
LOCK = threading.RLock()
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
def date(value):
    if not isinstance(value, str) or dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError('until: use YYYY-MM-DD')
    return dt.date.fromisoformat(value)

def end_date(s, *, days=None, until=None, no_end=None, clear=False):
    if sum(v is not None and v is not False for v in (days, until, no_end)) > 1:
        raise ValueError('Choose days, until or no end')
    if days is not None and (type(days) is not int or not (0 if clear else 1) <= days <= 30):
        raise ValueError('days must be 1..30'+(' or 0 to clear the end' if clear else ''))
    if no_end or days == 0:
        if not clear:
            raise ValueError('Only a guest update can clear the end')
        return None
    today = s.local_now().date()
    end = today + dt.timedelta(days=days) if days is not None else date(until)
    if not today <= end <= today + dt.timedelta(days=30):
        raise ValueError('until must be today or within 30 days')
    return end.isoformat()

def read(s):
    path = Path(s.DATA) / 'shelves.json'
    return json.loads(path.read_text()) if path.exists() else {'wishes': [], 'plans': {}}

def save(s, doc):
    write_private(Path(s.DATA) / 'shelves.json', json.dumps(doc, ensure_ascii=False) + '\n')

def rules(s):
    """Optional per-shelf filters and months, keyed by wish id; old versions ignore the file."""
    path = Path(s.DATA) / 'shelf_rules.json'
    return json.loads(path.read_text()) if path.exists() else {}

def save_rules(s, doc):
    write_private(Path(s.DATA) / 'shelf_rules.json', json.dumps(doc, ensure_ascii=False) + '\n')

def validate_filters(s, filters):
    if not isinstance(filters, dict) or not filters or set(filters) - {'kind', 'runtime', 'years', 'languages', 'exclude_genres', 'hide'}:
        raise ValueError('invalid shelf filter')
    kind = filters.get('kind', 'movie')
    if kind not in ('movie', 'series'):
        raise ValueError('invalid shelf filter kind')
    prefs = {k: copy.deepcopy(v) for k, v in filters.items() if k in ('years', 'hide')}
    if 'runtime' in filters:
        prefs['runtime'] = {kind: filters['runtime']}
    if 'languages' in filters:
        prefs['focus'] = {'languages': filters['languages']}
    if 'exclude_genres' in filters:
        prefs['excluded_genres'] = {kind: filters['exclude_genres']}
    cfg, person = copy.deepcopy(s.CONFIG), s.PEOPLE[0]
    cfg['taste'].setdefault('people', {}).setdefault(person, {})['prefs'] = prefs
    prefs = s.validate_config(cfg, data=s.DATA)['taste']['people'][person]['prefs']
    return {**filters, **({'languages': prefs['focus']['languages']} if 'languages' in filters else {}),
            **({'exclude_genres': prefs['excluded_genres'][kind]} if 'exclude_genres' in filters else {})}

def rule_ok(s, meta, rule):
    f = (rule or {}).get('filters') or {}
    if not f:
        return True
    facts, kind = s.row_facts(meta), meta['type']
    low, high = f.get('years') or [None, None]
    year = (facts.get('date') or '')[:4]
    if f.get('kind') and kind != f['kind'] or not s.runtime_ok(facts, f.get('runtime') or None):
        return False
    if (low or high) and (not year.isdigit() or low and int(year) < low or high and int(year) > high):
        return False
    if f.get('languages') and facts.get('lang') not in f['languages']:
        return False
    if f.get('exclude_genres') or f.get('hide'):
        names, hide = s.preference_genres(facts, kind), set(f.get('hide') or ())
        if s.genre_excluded(facts, kind, f.get('exclude_genres') or ()) or 'documentaries' in hide and 'documentary' in names:
            return False
        if 'animation' in hide and 'animation' in names or 'anime' in hide and s.is_anime(meta):
            return False
    return True

def slot_count(s, scope):
    return s.CONFIG['household'].get('shelves', 2) if scope == 'together' else s.CONFIG['taste'].get('people', {}).get(scope, {}).get('prefs', {}).get('shelves', 4)

def counts(cfg):
    return [cfg['household'].get('shelves', 2), *[cfg['taste'].get('people', {}).get(p, {}).get('prefs', {}).get('shelves', 4)
            for p in cfg['household']['members']]]

def slots(s, scope=None, *, doc=None):
    scopes = [scope, 'together'] if scope in s.PEOPLE else ['together']
    occupied = {(w['scope'], w['slot']) for w in (read(s) if doc is None else doc)['wishes']
                if (w.get('with_person') is None or w['with_person'] in s.PEOPLE) and
                not any(p in s.CONFIG['household'].get('away', []) and scope != p for p in (w['scope'], w.get('with_person')))}
    return [(s.rid(f'shelf_{p}_{n}'), p, n) for p in scopes for n in range(1, slot_count(s, p) + 1)
            if (p, n) in occupied]

def schedule(value):
    if value == 'always':
        return value
    if isinstance(value, str):
        match = re.fullmatch(r'([a-z,]+) (\d{1,2})-(\d{1,2})', value.lower())
        if not match:
            raise ValueError('when: use always or sun 05-12')
        days, start, end = match.groups()
        days = {'daily': DAYS, 'weekdays': DAYS[:5], 'weekend': DAYS[5:]}.get(days, days.split(','))
        value = {'days': days, 'hours': [int(start), int(end)]}
    if not isinstance(value, dict) or set(value) != {'days', 'hours'}:
        raise ValueError('invalid schedule')
    days, hours = value['days'], value['hours']
    if not isinstance(days, list) or not days or any(d not in DAYS for d in days) or not isinstance(hours, list) or len(hours) != 2 or any(type(h) is not int for h in hours) or not 5 <= hours[0] < hours[1] <= 29:
        raise ValueError('schedule hours must be within the logical day, 05..29')
    return {'days': list(dict.fromkeys(days)), 'hours': hours}

def window(wish, now, rule=None):
    if (rule or {}).get('months') and (now - dt.timedelta(hours=5)).month not in rule['months']:
        return None
    when = wish['when']
    if when == 'always':
        return 'always'
    day = (now - dt.timedelta(hours=5)).date()
    hour = now.hour + (24 if now.hour < 5 else 0)
    return day.isoformat() if DAYS[day.weekday()] in when['days'] and when['hours'][0] <= hour < when['hours'][1] else None

def cache_age(s, cid):
    wish = next((w for w in read(s)['wishes'] if s.rid(f"shelf_{w['scope']}_{w['slot']}") == cid), None)
    if not wish or wish['when'] == 'always':
        return 900
    now = s.local_now()
    day = (now - dt.timedelta(hours=5)).date()
    boundaries = [dt.datetime.combine(day + dt.timedelta(days=i), dt.time()) + dt.timedelta(hours=h)
                  for i in range(8) if DAYS[(day + dt.timedelta(days=i)).weekday()] in wish['when']['days']
                  for h in wish['when']['hours']]
    seconds = min((int((b - now).total_seconds()) for b in boundaries if b > now), default=900)
    return max(60, min(900, seconds))

def listing(s, scope=None):
    doc = read(s)
    scopes = [scope] if scope else [*s.PEOPLE, 'together']
    wishes = []
    for w in doc['wishes']:
        if w['scope'] in scopes:
            raw = s._feeds.get((s.rid(f"shelfraw_{w['scope']}_{w['slot']}"), s.LANG), {})
            active = window(w, s.local_now(), rules(s).get(w['id']))
            status = raw.get('status', 'unfilled') if (raw.get('wish'), raw.get('window')) == (w['id'], active) else 'unfilled'
            wishes.append({**w, 'status': status if active else 'outside window'})
    return {'counts': {p: slot_count(s, p) for p in scopes}, 'wishes': wishes}

@contextmanager
def locked(s):
    """Serialize shelf state changes across threads and processes."""
    with LOCK, os.fdopen(os.open(Path(s.DATA) / 'shelves.lock', os.O_RDWR | os.O_CREAT, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield

def change(s, scope, op, **kwargs):
    with locked(s):
        return _change(s, scope, op, **kwargs)

def consume_undo(s, scope, before, op):
    """Remove the corresponding old-stack entry while the shelf lock is held."""
    journal = Path(s.DATA) / 'undo.jsonl'
    events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    index = next((i for i in range(len(events)-1, -1, -1)
                  if events[i] == {'scope': scope, 'op': op, 'before': before}), None)
    if index is not None:
        events.pop(index)
        write_private(journal, ''.join(json.dumps(e) + '\n' for e in events))

def _change(s, scope, op, *, text='', name='', when='always', slot=None, count=None, apply=False, plan=None, expected=None, with_person=None, days=None, until=None):
    with LOCK:
        if scope not in (*s.PEOPLE, 'together'):
            raise ValueError('unknown shelf scope')
        if with_person is not None and (scope not in s.PEOPLE or with_person not in s.PEOPLE or with_person == scope):
            raise ValueError('Name two different people for this shelf')
        s.refresh_preferences()
        doc = read(s)
        before = {'wishes': copy.deepcopy([w for w in doc['wishes'] if w['scope'] == scope]), 'count': slot_count(s, scope)}
        if plan:
            saved = doc['plans'].get(plan)
            if not saved or saved['scope'] != scope or saved['before'] != before:
                raise ValueError('shelf plan changed; preview again')
            return _change(s, scope, apply=True, **saved['change'])
        if op == 'undo':
            if expected is not None and expected != before:
                raise ValueError('Shelves changed; cannot undo this command')
            journal = Path(s.DATA) / 'undo.jsonl'
            events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            index = next((i for i in range(len(events)-1, -1, -1) if events[i]['scope'] == scope), None)
            if index is None:
                raise ValueError('No shelf change to undo')
            old = events.pop(index)['before']
            cfg = copy.deepcopy(s.CONFIG)
            if scope == 'together':
                cfg['household']['shelves'] = old['count']
            else:
                cfg['taste'].setdefault('people', {}).setdefault(scope, {}).setdefault('prefs', {})['shelves'] = old['count']
            cfg = s.validate_config(cfg, data=s.DATA)
            write_private(Path(os.environ.get('SKALD_CONFIG') or Path(s.DATA) / 'skald.json'), json.dumps(cfg) + '\n')
            s.CONFIG.update(cfg)
            doc['wishes'] = [w for w in doc['wishes'] if w['scope'] != scope] + old['wishes']
            doc['plans'].clear()
            save(s, doc)
            write_private(journal, ''.join(json.dumps(e) + '\n' for e in events))
            s.aio_rows_changed()
            return listing(s, scope)
        own = [w for w in doc['wishes'] if w['scope'] == scope]
        if isinstance(slot, str):
            hits = [w['slot'] for w in own if w['name'].casefold() == slot.casefold() or str(w['slot']) == slot]
            if len(hits) != 1:
                raise ValueError('unknown or ambiguous shelf')
            slot = hits[0]
        if op == 'count':
            if type(count) is not int or not 0 <= count <= 10:
                raise ValueError('shelves count must be 0..10')
        elif op in ('add', 'replace', 'stop'):
            if slot is None:
                slot = next((n for n in range(1, before['count'] + 1) if all(w['slot'] != n for w in own)), None)
            exists = any(w['slot'] == slot for w in own)
            if type(slot) is not int or slot < 1 or slot > before['count'] and not (op == 'stop' and exists):
                raise ValueError('no shelf slot available')
            if op == 'add' and exists or op in ('replace', 'stop') and not exists:
                raise ValueError('shelf slot is occupied or missing')
            if op != 'stop':
                if not isinstance(text, str) or not text.strip() or len(text) > 1000 or not isinstance(name, str) or len(name) > 100 or any(ord(c) < 32 for c in name):
                    raise ValueError('invalid shelf wish')
                when = schedule(when)
                if days is not None or until is not None:
                    until = end_date(s, days=days, until=until)
        else:
            raise ValueError('unknown shelf operation')
        action = dict(op=op, text=text, name=name, when=when, slot=slot, count=count)
        if with_person is not None:
            action['with_person'] = with_person
        if until is not None:
            action['until'] = until
        digest = hashlib.sha256(json.dumps([scope, before, action], sort_keys=True).encode()).hexdigest()[:20]
        if not apply:
            doc['plans'][digest] = {'scope': scope, 'before': before, 'change': action}
            doc['plans'] = dict(list(doc['plans'].items())[-20:])
            save(s, doc)
            return {'plan': digest, 'scope': scope, **action}
        if op == 'count':
            cfg = copy.deepcopy(s.CONFIG)
            if scope == 'together':
                cfg['household']['shelves'] = count
            else:
                cfg['taste'].setdefault('people', {}).setdefault(scope, {}).setdefault('prefs', {})['shelves'] = count
            cfg = s.validate_config(cfg, data=s.DATA)
            write_private(Path(os.environ.get('SKALD_CONFIG') or Path(s.DATA) / 'skald.json'), json.dumps(cfg) + '\n')
            s.CONFIG.update(cfg)
        else:
            doc['wishes'] = [w for w in doc['wishes'] if (w['scope'], w['slot']) != (scope, slot)]
            if op != 'stop':
                doc['wishes'].append(dict(id=uuid.uuid4().hex, scope=scope, slot=slot, text=text.strip(), name=name, when=when,
                                         **({'with_person': with_person} if with_person is not None else {}),
                                         **({'until': until} if until is not None else {})))
        doc['plans'].clear()
        save(s, doc)
        journal = Path(s.DATA) / 'undo.jsonl'
        events = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
        events.append({'scope': scope, 'op': op, 'before': before})
        active = (*s.PEOPLE, 'together')
        kept = [e for i, e in enumerate(events) if e['scope'] not in active or sum(x['scope'] == e['scope'] for x in events[i+1:]) < 20]
        write_private(journal, ''.join(json.dumps(e) + '\n' for e in kept))
        s.aio_rows_changed()
        return listing(s, scope)

def discover(s, text, kind):
    params = {}
    for word in text.split():
        key, sep, value = word.partition(':')
        if not sep:
            return None
        if key == 'genre':
            gid = next((i for i, n in s.TMDB_GENRES[kind].items() if n.casefold() == value.replace('_', ' ').casefold()), None)
            if gid is None:
                return None
            params['with_genres'] = gid
        elif key == 'year' and re.fullmatch(r'\d{4}(?:-\d{4})?', value):
            low, _, high = value.partition('-')
            field = 'primary_release_date' if kind == 'movie' else 'first_air_date'
            params.update({field + '.gte': low + '-01-01', field + '.lte': (high or low) + '-12-31'})
        elif key == 'country' and re.fullmatch(r'[A-Za-z]{2}', value):
            params['with_origin_country'] = value.upper()
        else:
            return None
    return params or None

def fill(s, lang, now=None, build=True):
    with LOCK:
        now = now or s.local_now()
        doc, ruled = read(s), rules(s)
        out = {'_names': {}}
        for scope in (*s.PEOPLE, 'together'):
            for cid, owner, n in slots(s, scope, doc=doc):
                if owner != scope:
                    continue
                wish = next((w for w in doc['wishes'] if (w['scope'], w['slot']) == (scope, n)), None)
                out[cid] = {'tv': []}
                out['_names'][cid] = {'title': wish['name'] or wish['text'][:100]}
                rule = ruled.get(wish['id']) if wish else None
                win = window(wish, now, rule) if wish else None
                if not win:
                    continue
                key = (s.rid(f'shelfraw_{scope}_{n}'), s.LANG)
                raw = s._feeds.get(key, {})
                if (raw.get('wish'), raw.get('window')) != (wish['id'], win):
                    if not build:
                        continue
                    raw = {'wish': wish['id'], 'window': win, 'candidates': [], 'status': 'filled'}
                    s._feeds[key] = raw  # Reserve the window even if upstream fails.
                    s.save_feeds()
                    try:
                        picks = s.llm_picks(s.PICK_SYS, wish['text'], 700, 6, 'search', 'picks') if s.OR_KEY else None
                        for typ, kind in [('movie', 'movie'), ('series', 'tv')]:
                            if picks is not None:
                                names = s.genres(kind, s.LANG)
                                raw['candidates'] += s.ai_search(typ, kind, picks, s.LANG, names)
                            else:
                                filters = discover(s, wish['text'], kind)
                                if filters:
                                    names = s.genres(kind, s.LANG)
                                    items = s.tmdb('discover/' + kind, language=s.LANG, include_adult='false', **filters).get('results', [])
                                    raw['candidates'] += [s.to_meta(it, typ, kind, names, {}, lang=s.LANG) for it in items]
                                else:
                                    raw['status'] = 'needs OpenRouter'
                    except Exception:
                        raw['status'] = 'fill failed'
                    s.save_feeds()
                who = 'both' if scope == 'together' else scope
                pinned = raw.get('status') == 'pinned'
                token = s._safety_context.set(('shelf', who))
                try:
                    with s.addon_scope(scope):
                        profile = s.search_profile()
                        tonight = set(s.remote_masks()[1])
                    partner = wish.get('with_person')
                    if build:
                        s.ensure_facets([(x['id'], 'movie' if x['type'] == 'movie' else 'tv') for x in raw['candidates']])
                    metas = list({x['id']: x for x in raw['candidates'] if (pinned and partner is None or x['id'] not in profile.get('seen', ())) and x['id'] not in profile.get('negative', ()) and s.preference_ok(x, cid, who) and rule_ok(s, x, rule)}.values())
                    if partner is not None:
                        shared_token = s._safety_context.set(('shelf', partner))
                        try:
                            with s.addon_scope(partner):
                                other = s.search_profile()
                                tonight.update(s.remote_masks()[1])
                            excluded = set(other.get('seen', ())) | set(other.get('negative', ())) | s.deferred_keys(partner)
                            metas = [x for x in metas if x['id'] not in excluded and s.preference_ok(x, cid, partner)]
                        finally:
                            s._safety_context.reset(shared_token)
                    scores = s.member_scores(metas, who) if who in s.PEOPLE else s.week_scores(metas)
                    rated = [e for e in other.get('entries', []) if e.get('signal') in ('loved', 'liked', 'disliked')
                             or (e.get('item') or {}).get('rating') is not None] if partner in s.CONFIG['household'].get('guests', []) else []
                    if rated:
                        seeds = list(dict.fromkeys(e['key'] for e in rated if e.get('explicit') and e['weight'] > 0))
                        guest_scores = s.pool_pct({x['id']: s.seed_knn(seeds, x['id'], 1) for x in metas})
                        scores = {x['id']: (scores.get(x['id'], 0) + guest_scores.get(x['id'], 0))/2 for x in metas}
                    metas.sort(key=lambda x: scores.get(x['id'], 0), reverse=True)
                    metas = s.preference_order(metas, cid, who)[:s.ROW_MAX]
                    if pinned:
                        order = {x['id']: i for i, x in enumerate(raw['candidates'])}
                        metas.sort(key=lambda x: order[x['id']])
                    metas = s.remote_order(metas, (set(), tonight))
                    out[cid]['tv'] = metas if pinned or len(metas) >= s.ROW_MIN else []
                finally:
                    s._safety_context.reset(token)
        return out
