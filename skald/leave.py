"""Permanent person erasure, shared by Commander and the agent CLI."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path

from . import history, metrics, plays, shelves
from .intake import write_private


def settings(cfg, person):
    hh = cfg['household']
    for key in ('members', 'member_rows', 'guests', 'retired', 'away'):
        if key in hh:
            hh[key] = [p for p in hh[key] if p != person]
    for key in ('labels', 'ages', 'experiences', 'guest_until', 'away_until'):
        hh.get(key, {}).pop(person, None)
    if hh['trakt_member'] == person:
        hh['trakt_member'] = hh['members'][0]
        connector = cfg['connectors'].get('trakt_api', {})
        if 'accounts' not in connector:
            cfg['connectors'].pop('trakt_api', None)
    cfg['taste']['people'].pop(person, None)
    cfg.get('plays', {}).get('people', {}).pop(person, None)
    for cid, connector in list(cfg['connectors'].items()):
        connector.get('accounts', {}).pop(person, None)
        if 'accounts' in connector and not connector['accounts']:
            cfg['connectors'].pop(cid)
            continue
        opts = connector.get('options', {})
        if opts.get('profile') == person:
            cfg['connectors'].pop(cid)
            continue
        for key in ('profiles', 'users', 'archived'):
            if key in opts:
                opts[key] = {k: v for k, v in opts[key].items() if v != person}
                if not opts[key] and key != 'archived':
                    cfg['connectors'].pop(cid, None)
    if cfg['history']['source'] in ('aiostreams_db', 'jellyfin', 'plex') and cfg['history']['source'] not in cfg['connectors']:
        cfg['history']['source'] = 'none'
    return cfg


def contains(value, person):
    if isinstance(value, dict):
        return person in value or any(contains(v, person) for v in value.values())
    if isinstance(value, list):
        return any(contains(v, person) for v in value)
    return value == person


def without_person(value, person):
    if isinstance(value, dict):
        return {k: without_person(v, person) for k, v in value.items() if k != person and v != person}
    if isinstance(value, list):
        return [without_person(v, person) for v in value if v != person]
    return value


def prepare(s, cfg, person, before, shared='keep'):
    if shared not in ('keep', 'drop'):
        raise ValueError('Shared watches need keep or drop')
    data, changes, removed = Path(s.DATA), {}, []
    links = []
    for cid, connector in cfg['connectors'].items():
        if person in connector.get('accounts', {}):
            links.append((cid, connector['accounts'][person]))
        opts = connector.get('options', {})
        if opts.get('profile') == person:
            links.append((cid, 'profile'))
        links.extend((cid, key, source) for key in ('profiles', 'users', 'archived')
                     for source, who in opts.get(key, {}).items() if who == person)
    if cfg['household']['trakt_member'] == person:
        links.append(('trakt_member', person))
    counts = dict(feedback=0, history=0, imports=0, summaries=0, shelves=0, ideas=0,
                  preferences=len(cfg['taste']['people'].get(person, {}).get('prefs', {})), labels=1, links=len(links),
                  shared={p: 0 for p in s.PEOPLE if p != person})
    seen = history.seen_watches(data)
    windows = history.session_windows(data)
    cutoff = history.forget_cutoff(before)
    counted_history = set()

    def save(name, doc):
        changes[name] = json.dumps(doc, ensure_ascii=False) + '\n'

    def evidence(rows, owner, field, origin):
        kept = []
        for row in rows:
            by = row.get('by')
            people = history.owners({'history_by': by}, cfg) if by is not None else history.owners(
                row, cfg, owner, windows if origin == 'history.json' else (), data=data, origin=origin)
            if set(people) & {'together', 'both', 'household'}:
                people = cfg['household']['members']
            at = history.month_time(row['month']) if row.get('month') else (
                dt.datetime.fromisoformat(row['t']).replace(tzinfo=dt.timezone.utc).timestamp()
                if isinstance(row.get('t'), str) else row.get('t', (row.get('at') or 0) / 1000))
            action = history.forget_action(people, person, at, cutoff)
            if action == 'shared':
                for p in counts['shared']:
                    if p in people and (origin != 'plays.json' or (row.get('profile', 'household'), row.get('type'), row.get('id')) not in counted_history):
                        counts['shared'][p] += 1
                if shared == 'keep':
                    remaining = [p for p in people if p != person]
                    row = copy.deepcopy(row)
                    for field_name in ('who', 'person', 'profile'):
                        if row.get(field_name) == person:
                            row[field_name] = remaining[0] if len(remaining) == 1 else 'household'
                    if row.get('_summary', {}).get('who') == person:
                        row['_summary']['who'] = remaining[0]
                    kept.append(without_person(row, person))
                    if origin == 'history.json':
                        counted_history.add((row.get('profile', 'household'), row.get('type'), row.get('id')))
                    continue
            if action != 'keep':
                key = (row.get('profile', 'household'), row.get('type'), row.get('id'))
                counted = origin != 'plays.json' or key not in counted_history
                if counted:
                    counts[field] += 1
                if origin in ('history.json', 'plays.json'):
                    counted_history.add(key)
                removed.append(row)
                if action == 'shared':
                    key = row.get('key') or row.get('id')
                    if isinstance(key, str):
                        seen[key] = max(cutoff, seen.get(key, 0))
                        if not key.startswith('tmdb:') and 'type' in row:
                            hit = s.tmdb_details(row)
                            if hit:
                                canonical = 'tmdb:'+str(hit[1]['id'])
                                seen[canonical] = max(cutoff, seen.get(canonical, 0))
                continue
            kept.append(without_person(row, person))
        return kept

    for name, cid, owner in history.sources(cfg, data):
        path = data / name
        if not path.exists():
            continue
        raw = path.read_text()
        doc = [json.loads(line) for line in raw.splitlines() if line.strip()] if cid is None else json.loads(raw)
        rows = doc if cid is None else doc['items']
        kept = evidence(rows, owner or (cfg['household']['trakt_member'] if name == 'trakt.json' else None),
                        'imports' if cid is None else 'history', name)
        if owner == person:
            changes[name] = None
        elif kept != rows:
            if cid is None:
                changes[name] = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in kept)
            else:
                doc['items'] = kept
                save(name, doc)
    cached = plays.state(s)
    rows = [(r['person'], r['row']) for r in cached.get('records', [])]
    rows += [(who, row) for who, account in cached.get('https', {}).items() for row in account.get('rows', [])]
    evidence([dict(profile='household' if who == 'together' else who, type=row[1], id=row[2], at=row[3])
              for who, row in rows], None, 'history', 'plays.json')
    cleaned = plays.prune_cache(data, cfg, copy.deepcopy(cached), s.time.time(), person=person, before=before, shared=shared)
    cleaned = without_person(cleaned, person)
    if cleaned != cached:
        save('plays.json', cleaned)
    path = data / history.SUMMARY
    if path.exists():
        doc = json.loads(path.read_text())
        kept = evidence(doc['items'], None, 'summaries', history.SUMMARY)
        if kept != doc['items']:
            doc['items'] = kept
            if 'import_order' in doc:
                ids = {r.get('id') for r in removed if r.get('origin') == 'imports.jsonl'}
                doc['import_order'] = [i for i in doc['import_order'] if i not in ids]
            save(path.name, doc)
    for name, field in (('feedback.jsonl', 'feedback'), ('wishes.jsonl', None), ('sessions.jsonl', None), ('undo.jsonl', None), ('memory.log.jsonl', None)):
        path = data / name
        if not path.exists():
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if field:
            kept = evidence(rows, None, field, name)
        else:
            kept = [r for r in rows if not contains(r, person)]
            removed.extend(r for r in rows if r not in kept)
        if kept != rows:
            changes[name] = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in kept)
    doc = shelves.read(s)
    gone = [w for w in doc['wishes'] if person in (w['scope'], w.get('with_person'))]
    plans = {k: v for k, v in doc.get('plans', {}).items() if not contains(v, person)}
    if gone or plans != doc.get('plans', {}):
        counts['shelves'] = len(gone)
        removed.extend(gone)
        doc['wishes'] = [w for w in doc['wishes'] if w not in gone]
        doc['plans'] = plans
        save('shelves.json', doc)
        rules = shelves.rules(s)
        save('shelf_rules.json', {k: v for k, v in rules.items() if k not in {w['id'] for w in gone}})
    path = data / 'lab.json'
    if path.exists():
        doc = json.loads(path.read_text())
        gone = [i for i in doc['ideas'] if i['members'] == [person]]
        counts['ideas'] = len(gone)
        removed.extend(gone)
        doc['ideas'] = [{**i, 'members': [p for p in i['members'] if p != person]} for i in doc['ideas'] if i not in gone]
        if gone or any(person in i['members'] for i in json.loads(path.read_text())['ideas']):
            save(path.name, doc)
    path = data / 'taste.json'
    if path.exists():
        doc = json.loads(path.read_text())
        if person in doc.get('people', {}):
            removed.append(doc['people'].pop(person))
            save(path.name, doc)
    path = data / 'commands.json'
    if path.exists():
        doc = json.loads(path.read_text())
        kept = {p: [r for r in rows if not contains(r, person)] for p, rows in doc.items() if p != person}
        if kept != doc:
            save(path.name, kept)
    from .setup import aiostreams_logins, state
    logins = aiostreams_logins(data)
    odin = person in logins or any(who == person for connector in cfg['connectors'].values()
                                  for key in ('profiles', 'archived') for who in connector.get('options', {}).get(key, {}).values())
    if person in logins:
        logins.pop(person)
        save('credentials/aiostreams.json', logins)
    saved = state(data)
    if contains(saved, person):
        save('setup.json', without_person(saved, person))
    path = data / 'connectors.json'
    if path.exists():
        doc = json.loads(path.read_text())
        kept = {}
        for cid, connector in doc['connectors'].items():
            if connector.get('person') == person:
                continue
            if person in connector.get('accounts', {}):
                connector['accounts'].pop(person)
                if not connector['accounts']:
                    continue
            kept[cid] = connector
        doc['connectors'] = kept
        save(path.name, doc)
    for name, field in (('title_fixes.json', 'fixes'), ('memory.json', 'facts'), ('memory.prev.json', 'facts')):
        path = data / name
        if not path.exists():
            continue
        doc = json.loads(path.read_text())
        kept = [r for r in doc[field] if not contains(r, person)]
        if kept != doc[field] or person in doc.get('forgotten', {}):
            removed.extend(r for r in doc[field] if r not in kept)
            doc[field] = kept
            if field == 'facts':
                doc['stamp'] = None
                doc['forgotten'].pop(person, None)
            save(path.name, doc)
    for name, field in (('interview.json', None), ('clients.json', 'clients'), ('archetypes.json', 'items')):
        path = data / name
        if not path.exists():
            continue
        doc = json.loads(path.read_text())
        rows = doc[field] if field else doc
        kept = ({k: v for k, v in rows.items() if k not in (person, 'household')} if name == 'archetypes.json' else
                [r for r in rows if not contains(r, person)])
        if kept != rows:
            if field:
                doc[field] = kept
            else:
                doc = kept
            save(name, doc)
    if seen != history.seen_watches(data):
        save('history-seen.json', seen)
    forgotten = history.cutoffs(data)
    forgotten[person] = max(forgotten.get(person, before), (dt.date.fromisoformat(before) + dt.timedelta(days=1)).isoformat())
    save('history-forgotten.json', forgotten)
    def affected(old, new):
        if isinstance(old, dict) and isinstance(new, dict):
            return {k: affected(v, new.get(k)) for k, v in old.items() if v != new.get(k)}
        if isinstance(old, list) and isinstance(new, list):
            return [r for r in old if r not in new]
        return old
    targets = {}
    for name, content in changes.items():
        path = data / name
        if not path.exists():
            continue
        raw = path.read_text()
        old = [json.loads(line) for line in raw.splitlines() if line.strip()] if name.endswith('.jsonl') else json.loads(raw)
        new = ([json.loads(line) for line in content.splitlines() if line.strip()] if name.endswith('.jsonl') else json.loads(content)) if content is not None else None
        if old != new:
            targets[name] = affected(old, new)
    counts['metrics'] = metrics.forget(data, cfg, before, person, erase=True)
    for name, field in (('metrics_shown.json', 'folded'), ('metrics.json', 'weeks')):
        path = data / name
        if path.exists():
            doc = json.loads(path.read_text())
            targets[name] = {'shown': [r for r in doc.get('shown', []) if r['person'] in (person, 'together')],
                field: {week: {row: {p: cell for p, cell in people.items() if p in (person, 'together')}
                               for row, people in rows.items() if set(people) & {person, 'together'}}
                        for week, rows in doc[field].items() if any(set(people) & {person, 'together'} for people in rows.values())}}
    stamp = hashlib.sha256(json.dumps([targets, links], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return changes, dict(before=before, shared=shared, odin=odin, counts=counts, stamp=stamp)


def preview(s, cfg, person, shared='keep'):
    return prepare(s, cfg, person, s.local_now().date().isoformat(), shared)[1]


def odin_reminder(label):
    return label+"'s Odin login and its watch history still exist in Odin and AIOStreams. Remove them there if "+label+' should lose access.'


def apply(s, plan):
    from . import commands as c
    from .backup import before_people
    from .setup import publish
    with shelves.locked(s), history.locked(s.DATA), plays._request_lock:
        s.refresh_preferences()
        cfg = c.config(s)
        person = plan['member']['name']
        with metrics._lock:
            checked = c.member_preview(s, '', plan['change'], plan['actor'], None)
            changes, state = prepare(s, cfg, person, plan['forget']['before'], plan['forget']['shared'])
            if checked.get('read_only') or state != plan['forget'] or c.member_state(cfg, person) != plan['member']['before']:
                raise ValueError('Person data changed; preview again')
            before_people(s, cfg)
            metrics.forget(s.DATA, cfg, plan['forget']['before'], person, apply=True, erase=True)
        for name, content in changes.items():
            path = Path(s.DATA) / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write_private(path, content)
        publish(Path(s.DATA), checked['member']['config_after'], replace=True)
        new_cfg = checked['member']['config_after']
        old_refs = {r for cid, connector in cfg['connectors'].items() for r in
                    [connector.get('accounts', {}).get(person), connector.get('auth') if cid not in new_cfg['connectors'] else None] if r}
        if cfg['household']['trakt_member'] == person and 'accounts' not in cfg['connectors'].get('trakt_api', {}):
            old_refs.add(cfg['connectors'].get('trakt_api', {}).get('auth'))
        for ref in old_refs:
            if ref and ref.startswith('file:') and not contains(new_cfg['connectors'], ref):
                (Path(s.DATA)/'credentials'/ref[5:]).unlink(missing_ok=True)
        s.forget_person_caches()
        s.forget_connector(person)
        s.refresh_preferences()
        c.pending(s).clear()
    s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+'. This cannot be undone.',
            'description': odin_reminder(plan['member']['config_before']['household']['labels'][person]) if plan['forget']['odin'] else ''}
