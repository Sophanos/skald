"""Monthly taste evidence and opt-in raw retention. No connector transports."""
import datetime as dt
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .intake import write_private

SUMMARY = 'history-summary.json'
LOCK = 'history.lock'
_writer_lock = threading.RLock()
_locked_paths = set()
ITEM_FIELDS = ('played', 'plays', 'progress', 'episodes_played', 'last_episode', 'resume_episode', 'rating', 'completed',
               'favorite', 'dropped', 'likes', 'signal', 'weight', 'flags', 'duration_min')


@contextmanager
def locked(data):
    path = Path(data).resolve() / LOCK
    with _writer_lock:
        if path in _locked_paths:
            yield
            return
        with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'a') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            _locked_paths.add(path)
            try:
                yield
            finally:
                _locked_paths.remove(path)
                fcntl.flock(fh, fcntl.LOCK_UN)


def read(data):
    return read_document(data)['items']


def seen_keys(data):
    return set(seen_watches(data))


def seen_watches(data):
    try:
        return json.loads((Path(data) / 'history-seen.json').read_text())
    except FileNotFoundError:
        return {}


def read_document(data):
    try:
        return json.loads((Path(data) / SUMMARY).read_text())
    except FileNotFoundError:
        return {'v': 1, 'items': []}


def enabled(config):
    return any(config['history'].get(k) is not None for k in ('keep_raw_days', 'max_raw_items')) or any(
        c.get('keep_raw') is False for c in config['connectors'].values())


def sources(config, data=None):
    yield 'history.json', config['history']['source'] if config['history']['source'] != 'file' else 'history', None
    yield 'trakt.json', 'trakt', None
    for source, cid in (('trakt', 'trakt_api'), ('mdblist', 'mdblist'), ('stremio', 'stremio'), ('pmdb', 'pmdb')):
        people = set(config['connectors'].get(cid, {}).get('accounts', {}))
        if data is not None:
            people.update(match[1] for path in Path(data).glob(source + '-*.history.json')
                          if (match := re.fullmatch(source + r'-([a-z][a-z0-9]{1,15})\.history\.json', path.name)))
        for person in sorted(people):
            yield source + '-' + person + '.history.json', cid, person
    yield 'imports.jsonl', None, None


def month_time(month):
    return dt.datetime.fromisoformat(month + '-01').replace(tzinfo=dt.timezone.utc).timestamp()


def identity(e):
    return e['origin'], e['key'], e['kind'], e['who'], e['src'], e['month']


def clear_connector_caches(doc):
    fields = {'mdblist': {'watched': [], 'watchlist': [], 'activities': {}},
              'stremio': {'library': {}, 'likes': {}}}.get(doc.get('source'), {})
    changed = any(doc.get(field) for field in fields)
    if changed:
        doc.update(fields)
    return changed


def compact(ev, origin, profile):
    item = ev['ctx'].get('item', {})
    return {**{k: ev[k] for k in ('key', 'kind', 'title', 'year', 'who', 'by', 'action', 'src', 'conf', 'n', 'w0')},
            **({'id': ev['id']} if origin == 'imports.jsonl' else {}),
            'origin': origin, 'profile': profile,
            'month': dt.datetime.fromtimestamp(ev['t'], dt.timezone.utc).strftime('%Y-%m') if ev['t'] is not None else None,
            'play_count': ev['n'] if not item else max(item.get('plays', 0), item.get('episodes_played', 0), int(bool(item.get('played')))),
            'item': {k: item[k] for k in ITEM_FIELDS if k in item},
            'genres': ev['ctx'].get('genres', []), 'flags': ev['ctx'].get('flags', []),
            'signal': ev['ctx'].get('signal')}


def restored(e):
    at = month_time(e['month']) if e['month'] else None
    if e['origin'] == 'imports.jsonl':
        return {**{k: e[k] for k in ('key', 'kind', 'title', 'year', 'who', 'action', 'conf', 'n')},
                't': e['month'] + '-01' if e['month'] else '1970-01-01',
                'src': e['src'].removeprefix('import:'),
                'id': e.get('id') or hashlib.sha256(json.dumps(identity(e)).encode()).hexdigest()[:32]}
    return {**e['item'], **({'src': e['src']} if e['src'] == 'sync:pmdb' else {}), 'id': e['key'], 'type': 'movie' if e['kind'] == 'movie' else 'series',
            'at': at * 1000 if at is not None else None, 'title': e['title'], 'year': e['year'],
            'genres': e['genres'], 'signal': e['signal'], 'profile': e['profile'], 'person': e['who'],
            '_summary': e}


def combine(data, origin, raw):
    doc = read_document(data)
    summaries = [e for e in doc['items'] if e['origin'] == origin]
    if not summaries:
        return raw
    if origin == 'imports.jsonl':
        # Corrections follow log order, even when their watch dates go backwards.
        items = {(it['id'], it['who']): it for it in [*(restored(e) for e in summaries), *raw]}
        order = {eid: i for i, eid in enumerate(doc.get('import_order', [it['id'] for it in items.values()]))}
        return sorted(items.values(), key=lambda it: order.get(it['id'], len(order)))
    # Remaining raw title state wins over older monthly state for the same source/person.
    existing = {(it['id'], 'movie' if it['type'] == 'movie' else 'tv', it.get('profile', 'household')) for it in raw}
    latest = {}
    for e in summaries:
        it = restored(e)
        k = it['id'], e['kind'], it['profile']
        if origin != 'history.json' and k in existing:
            continue
        if origin == 'history.json':
            k += (e['who'], tuple(sorted(e['by'])))
        if k not in latest or (it.get('at') or 0) > (latest[k].get('at') or 0):
            latest[k] = it
    return raw + list(latest.values())


def cleanup(s, now, *, apply=True):
    if not enabled(s.CONFIG):
        return {'pruned': 0, 'summaries': 0, 'residual': 0}
    if not apply:
        return _cleanup(s, now, apply=False)
    with locked(s.DATA):
        return _cleanup(s, now, apply=True)


def _cleanup(s, now, *, apply):
    data = Path(s.DATA)
    summary_doc = read_document(data)
    summaries = summary_doc['items']
    folded = {identity(e): e for e in summaries}
    docs, rows = {}, []
    for name, cid, person in sources(s.CONFIG, data):
        path = data / name
        if not path.exists():
            continue
        raw = path.read_text()
        doc = [json.loads(line) for line in raw.splitlines() if line.strip()] if cid is None else json.loads(raw)
        items = doc if cid is None else doc['items']
        docs[name] = doc
        for i, it in enumerate(items):
            at = dt.datetime.fromisoformat(it['t']).replace(tzinfo=dt.timezone.utc).timestamp() if cid is None else (it.get('at') or 0) / 1000
            connector = cid or it['src']
            opts = s.CONFIG['connectors'].get(connector, s.CONFIG['connectors'].get('trakt_api', {}) if connector == 'trakt' else {})
            days = s.CONFIG['history'].get('keep_raw_days')
            prune = opts.get('keep_raw') is False or days is not None and at < now - days * 86400
            rows.append((at, name, i, it, person, prune, opts.get('keep_raw') is False))
    maximum = s.CONFIG['history'].get('max_raw_items')
    excess = max(0, len(rows) - maximum) if maximum is not None else 0
    by_age = sorted(rows, key=lambda row: row[:3])
    selected = {(name, i) for _, name, i, _, _, _, _ in by_age[:excess]}
    removed, batch_counts, updated_tv, tv_times = {}, {}, {}, {}
    residual = 0
    for at, name, i, it, person, prune, no_raw in rows:
        if not prune and (name, i) not in selected:
            continue
        # Apply exact cutoffs before dates become monthly. Forget removes stored summaries itself.
        eligible = filter_forgotten(data, s.CONFIG, name, [it], person)
        if not eligible:
            removed.setdefault(name, set()).add(i)
            continue
        if name == 'imports.jsonl':
            evts = list(s.import_events(now, eligible))
        elif name == 'history.json':
            evts = list(s.tv_events(now, eligible))
        else:
            evts = list(s.trakt_events(now, [{**item, **({'person': person} if person else {})} for item in eligible]))
        if not evts:
            if maximum == 0 or no_raw:
                removed.setdefault(name, set()).add(i)
            else:
                residual += 1
            continue
        for event in evts:
            e = compact(event, name, it.get('profile', 'household'))
            batch_counts[identity(e)] = batch_counts.get(identity(e), 0) + 1
            old = folded.get(identity(e))
            if old:
                e['play_count'] = max(old['play_count'], e['play_count'])
            if name == 'history.json':
                if identity(e) in tv_times and at <= tv_times[identity(e)]:
                    e = {**old, 'play_count': e['play_count']}
                else:
                    tv_times[identity(e)] = at
            e['raw_items'] = max(batch_counts[identity(e)], (old or {}).get('raw_items', 0))
            if name == 'imports.jsonl':
                folded.pop(identity(e), None)
            folded[identity(e)] = e
            if name == 'history.json':
                updated_tv[identity(e)] = e
        removed.setdefault(name, set()).add(i)
    if removed and apply:
        items = list(updated_tv.values()) + [e for k, e in folded.items() if k not in updated_tv]
        imports = docs.get('imports.jsonl', [])
        if 'imports.jsonl' in removed or 'import_order' in summary_doc:
            previous = summary_doc.get('import_order', [restored(e)['id'] for e in summaries if e['origin'] == 'imports.jsonl'])
            order = dict.fromkeys([*previous, *(e['id'] for e in imports)])
            live_ids = {e['id'] for i, e in enumerate(imports) if i not in removed.get('imports.jsonl', ())}
            live_ids.update(restored(e)['id'] for e in items if e['origin'] == 'imports.jsonl')
            summary_doc['import_order'] = [eid for eid in order if eid in live_ids]
        summary_doc['items'] = items
        write_private(data / SUMMARY, json.dumps(summary_doc) + '\n')
        for name, indices in removed.items():
            doc = docs[name]
            items = doc if name == 'imports.jsonl' else doc['items']
            kept = [it for i, it in enumerate(items) if i not in indices]
            if name == 'imports.jsonl':
                content = ''.join(json.dumps(it) + '\n' for it in kept)
            else:
                doc['items'] = kept
                clear_connector_caches(doc)
                content = json.dumps(doc) + '\n'
            write_private(data / name, content)
    for name, doc in docs.items():
        if apply and name != 'imports.jsonl' and clear_connector_caches(doc):
            if not removed:
                write_private(data / SUMMARY, json.dumps(summary_doc) + '\n')
            write_private(data / name, json.dumps(doc) + '\n')
    from . import plays
    plays.purge(data, s.CONFIG, now, apply=apply)
    return {'pruned': sum(map(len, removed.values())), 'summaries': len(folded), 'residual': residual}


def cutoffs(data):
    try:
        return json.loads((Path(data) / 'history-forgotten.json').read_text())
    except FileNotFoundError:
        return {}


def forget_cutoff(before):
    return dt.datetime.combine(dt.date.fromisoformat(before) + dt.timedelta(days=1),
                               dt.time(), dt.timezone.utc).timestamp()


def forget_action(people, person, at, cutoff):
    if person not in people:
        return 'keep'
    if len(people) == 1:
        return 'erase'
    return 'shared' if (at or 0) < cutoff else 'keep'


def folded_imports(data):
    return [restored(e) for e in read(data) if e['origin'] == 'imports.jsonl']


def remove_imports(data, ids, person=None, raw_ids=()):
    with locked(data):
        path = Path(data) / SUMMARY
        try:
            doc = json.loads(path.read_text())
        except FileNotFoundError:
            return
        kept = [e for e in doc['items'] if e['origin'] != 'imports.jsonl' or restored(e)['id'] not in ids
                or person is not None and e['who'] != person]
        if len(kept) != len(doc['items']):
            doc['items'] = kept
            if 'import_order' in doc:
                remaining = set(raw_ids) | {restored(e)['id'] for e in kept if e['origin'] == 'imports.jsonl'}
                doc['import_order'] = [eid for eid in doc['import_order'] if eid not in ids or eid in remaining]
            write_private(path, json.dumps(doc) + '\n')


def session_windows(data):
    try:
        events = [json.loads(line) for line in (Path(data) / 'sessions.jsonl').read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    fixes = {e['fix']: e for e in events if e.get('fix')}
    windows = []
    for e in events:
        if e.get('fix'):
            continue
        if windows:
            windows[-1]['until'] = min(windows[-1]['until'], e['t'])
        if e['who'] != 'off':
            windows.append({'t': e['t'], 'until': e['t'] + 4 * 3600,
                            'who': fixes.get(e['id'], e)['who']})
    return windows


def owners(it, config, person=None, windows=(), *, data=None, origin=None):
    from . import plays
    if person:
        return [person]
    if 'history_by' in it:
        return it['history_by']
    if '_summary' in it:
        by = it['_summary']['by']
        return list(config['household']['members']) if 'together' in by or 'both' in by else by
    if origin == 'history.json' and plays.managed_config(config, data) and 'who' not in it and 'person' not in it:
        profile = it.get('profile')
        if profile in ('household', 'together'):
            return list(config['household']['members'])
        return [profile] if profile in config['household']['members'] else []
    if windows and 'who' not in it and 'person' not in it and not (
            (config['history']['source'] in ('aiostreams_db', 'jellyfin', 'plex', 'none') or 'plays' in config) and
            it.get('profile') in config['household']['members']):
        at = (it.get('at') or 0) / 1000
        match = next((w for w in reversed(windows) if w['t'] <= at < w['until']), None)
        if match and match['who'] not in ('both', 'together'):
            return [match['who']]
    who = it.get('who') or it.get('person') or it.get('profile', 'household')
    if who in ('household', 'both', 'together'):
        return list(config['household']['members'])
    return [who]


def forget(data, config, before, person, *, apply=False):
    day = dt.date.fromisoformat(before)
    if day.isoformat() != before or person not in config['household']['members']:
        raise ValueError('use YYYY-MM-DD and a household member')
    if not apply:
        return _forget(data, config, before, person, apply=False)
    with locked(data):
        return _forget(data, config, before, person, apply=True)


def _forget(data, config, before, person, *, apply):
    """Preview by default; monthly evidence intersecting the boundary month is removed."""
    day = dt.date.fromisoformat(before)
    if day.isoformat() != before or person not in config['household']['members']:
        raise ValueError('use YYYY-MM-DD and a household member')
    cutoff = dt.datetime.combine(day, dt.time(), dt.timezone.utc).timestamp()
    data = Path(data)
    changes = {}
    from . import plays
    saved_days = plays.days_state(data)
    kept_days = {p: [d for d in days if p != person or d >= before] for p, days in saved_days.items()}
    kept_days = {p: days for p, days in kept_days.items() if days}
    if kept_days != saved_days:
        changes[plays.WATCHING_DAYS] = json.dumps(kept_days)+'\n'
    windows = session_windows(data)
    count = {'raw': 0, 'summaries': 0, 'facts': 0, 'dry_run': not apply}
    count['watching_days'] = len(saved_days.get(person, []))-len(kept_days.get(person, []))
    raw_import_ids = set()
    for name, cid, owner in sources(config, data):
        path = data / name
        if not path.exists():
            continue
        doc = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if cid is None else json.loads(path.read_text())
        items = doc if cid is None else doc['items']
        kept = []
        changed = False
        for it in items:
            at = dt.datetime.fromisoformat(it['t']).replace(tzinfo=dt.timezone.utc).timestamp() if cid is None else (it.get('at') or 0) / 1000
            people = owners(it, config, owner or (config['household']['trakt_member'] if name == 'trakt.json' else None),
                            windows if name == 'history.json' else (), data=data, origin=name)
            if at < cutoff and person in people:
                count['raw'] += 1
                changed = True
                remaining = [p for p in people if p != person]
                if not remaining:
                    continue
                if cid is None:
                    kept.extend({**it, 'who': p} for p in remaining)
                    continue
                it = {**it, 'history_by': remaining}
            kept.append(it)
        if cid is None:
            raw_import_ids = {it['id'] for it in kept}
        if cid is not None and (changed or owner == person):
            changed = clear_connector_caches(doc) or changed
        if changed:
            if cid is None:
                changes[name] = ''.join(json.dumps(it) + '\n' for it in kept)
            else:
                doc['items'] = kept
                changes[name] = json.dumps(doc) + '\n'
    kept = []
    summary_doc = read_document(data)
    for e in summary_doc['items']:
        people = list(config['household']['members']) if 'together' in e['by'] or 'both' in e['by'] else e['by']
        old = e['month'] is None or e['month'] < before[:7] or day.day > 1 and e['month'] == before[:7]
        if old and person in people:
            count['summaries'] += 1
            remaining = [p for p in people if p != person]
            if not remaining:
                continue
            # Split shared claims so the forgotten member cannot regain an opinion.
            kept.extend({**e, 'who': p, 'by': [p]} for p in remaining)
        else:
            kept.append(e)
    old_order = summary_doc.get('import_order')
    if old_order is not None:
        live_ids = raw_import_ids | {restored(e)['id'] for e in kept if e['origin'] == 'imports.jsonl'}
        summary_doc['import_order'] = [eid for eid in old_order if eid in live_ids]
    if count['summaries'] or summary_doc.get('import_order') != old_order:
        summary_doc['items'] = kept
        changes[SUMMARY] = json.dumps(summary_doc) + '\n'
    wishes = data / 'wishes.jsonl'
    if wishes.exists():
        rows = [json.loads(line) for line in wishes.read_text().splitlines() if line.strip()]
        kept = [w for w in rows if not (w.get('who') == person and w.get('t', 0) < cutoff)]
        if len(kept) != len(rows):
            changes[wishes.name] = ''.join(json.dumps(w, ensure_ascii=False) + '\n' for w in kept)
    fixes = data / 'title_fixes.json'
    if fixes.exists():
        rows = json.loads(fixes.read_text())['fixes']
        # A moved watch belongs to the member it moved to, so only forgetting that member removes the row.
        kept = [r for r in rows if not ((r.get('to') if r.get('op') == 'move' else r.get('who')) == person and r.get('t', 0) < cutoff)]
        if len(kept) != len(rows):
            changes[fixes.name] = json.dumps({'fixes': kept}, ensure_ascii=False, indent=1) + '\n'
    forgotten_facts = set()
    from .metrics import forget as forget_metrics
    count['metrics'] = forget_metrics(data, config, before, person, apply=apply)
    for name in ('memory.json', 'memory.prev.json'):
        path = data / name
        if not path.exists():
            continue
        store = json.loads(path.read_text())
        facts = store['facts']
        store['facts'] = [f for f in facts if not (f['person'] == person and f.get('src') != 'told' and
                          f.get('confirmed') is None and f.get('seen', f.get('created', 0)) < cutoff)]
        if len(facts) != len(store['facts']):
            if name == 'memory.json':
                count['facts'] = len(facts) - len(store['facts'])
            store['stamp'] = None
            forgotten_facts.update(f['id'] for f in facts if f not in store['facts'])
        store['forgotten'][person] = max(cutoff, store['forgotten'].get(person, 0))
        store['seq'] += 1
        changes[name] = json.dumps(store) + '\n'
    log = data / 'memory.log.jsonl'
    if forgotten_facts and log.exists():
        ops = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        changes[log.name] = ''.join(json.dumps(op) + '\n' for op in ops if op.get('id') not in forgotten_facts)
    if apply:
        from .health import SERVING_CACHES
        from . import plays
        plays.purge(data, config, cutoff, person=person, before=before)
        forgotten = cutoffs(data)
        forgotten[person] = max(before, forgotten.get(person, before))
        changes['history-forgotten.json'] = json.dumps(forgotten) + '\n'
        # Only this member's taste label and the shared one go; other members keep theirs and their weekly limit.
        labels = data / 'archetypes.json'
        try:
            doc = json.loads(labels.read_text())
            doc['items'] = {k: v for k, v in doc['items'].items() if k not in (person, 'household')}
            changes[labels.name] = json.dumps(doc, ensure_ascii=False, sort_keys=True) + '\n'
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, KeyError, AttributeError):
            labels.unlink()
        # Serving and copied profile caches must rebuild from the remaining evidence.
        for name in ('profile.json', 'taste_ring.json', 'learn.json', 'online.json', *SERVING_CACHES,
                     *('health-cache-' + name for name in SERVING_CACHES)):
            (data / name).unlink(missing_ok=True)
        for name, content in changes.items():
            write_private(data / name, content)
    return count


def filter_forgotten(data, config, origin, items, owner=None):
    forgotten = cutoffs(data)
    if not forgotten:
        return items
    seen = seen_watches(data)
    windows = session_windows(data) if origin == 'history.json' else ()
    kept = []
    for it in items:
        people = owners(it, config, owner or (config['household']['trakt_member'] if origin == 'trakt.json' else None), windows, data=data, origin=origin)
        date = it.get('t') if origin == 'imports.jsonl' else (
            dt.datetime.fromtimestamp((it.get('at') or 0) / 1000, dt.timezone.utc).date().isoformat())
        at = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp() if origin == 'imports.jsonl' else (it.get('at') or 0) / 1000
        if (len(people) > 1 or 'history_by' not in it and it.get('profile') in ('both', 'together', 'household') or
                set(people) & {'both', 'together', 'household'} or
                origin == 'imports.jsonl' and it.get('who') == 'both') and at < seen.get(it.get('key') or it.get('id'), 0):
            continue
        remaining = [p for p in people if p not in forgotten or date >= forgotten[p]]
        if remaining == people:
            kept.append(it)
        elif remaining:
            if origin == 'imports.jsonl':
                kept.extend({**it, 'who': p} for p in remaining)
            else:
                e = it.get('_summary')
                kept.append({**it, 'history_by': remaining, **({'_summary': {**e, 'who': remaining[0], 'by': remaining}} if e else {})})
    return kept
