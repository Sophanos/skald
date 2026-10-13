"""Local, counts-only row cohorts with bounded, disposable attribution evidence."""
import copy
import datetime as dt
import json
import math
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo

DAY = 86400
DEFAULTS = dict(enabled=False, attribution_days=14, shown_retention_days=30, shown_max_items=10000)
SOURCES = {'history', 'jellyfin', 'plex', 'aiostreams_db', 'trakt', 'trakt_api', 'stremio', 'mdblist',
           'import:netflix', 'import:trakt', 'import:letterboxd'}
_lock = threading.RLock()


def settings(config):
    return {**DEFAULTS, **config.get('metrics', {})}


def empty():
    return {'v': 1, 'weeks': {}, 'sources': {}}


def _week(at, config):
    date = dt.datetime.fromtimestamp(at, ZoneInfo(config['region']['tz'])).date()
    return (date - dt.timedelta(days=date.weekday())).isoformat()


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError):
        raise ValueError('invalid local row metrics; restore or remove the metrics files') from None


def _write(path, value):
    from .intake import write_private
    write_private(path, json.dumps(value, allow_nan=False) + '\n')


def disable(data):
    with _lock:
        for name in ('metrics.json', 'metrics_shown.json'):
            (Path(data)/name).unlink(missing_ok=True)


def _state(data, config, now):
    saved = _read(Path(data)/'metrics.json', empty())
    # A restored counts-only backup has no raw attribution evidence.
    state = _read(Path(data)/'metrics_shown.json', None)
    if state is not None:
        if not (isinstance(state, dict) and isinstance(state.get('shown'), list)
                and isinstance(state.get('folded'), dict) and isinstance(state.get('sources'), dict)
                and 'cleaned' in state and (state['cleaned'] is None or isinstance(state['cleaned'], str))):
            raise ValueError('invalid local row metrics; restore or remove the metrics files')
        return state
    today = dt.datetime.fromtimestamp(now, ZoneInfo(config['region']['tz'])).date()
    for week, rows in saved['weeks'].items():
        unfinished = today <= dt.date.fromisoformat(week) + dt.timedelta(days=7+settings(config)['attribution_days'])
        for people in rows.values():
            for cell in people.values():
                cell['closed'] = True
                if unfinished:
                    cell['complete'] = False
    return {'v': 1, 'shown': [], 'folded': saved['weeks'], 'sources': saved['sources'], 'cleaned': None}


def _cell(weeks, week, row, person):
    return weeks.setdefault(week, {}).setdefault(row, {}).setdefault(person,
        {'shown': 0, 'played': 0, 'sources': {}, 'complete': True, 'closed': False})


def _fold(weeks, record):
    cell = _cell(weeks, record['week'], record['row'], record['person'])
    cell['shown'] += 1
    cell['played'] += bool(record['sources'])
    for source in record['sources']:
        cell['sources'][source] = cell['sources'].get(source, 0) + 1
    return cell


def _totals(state):
    weeks = copy.deepcopy(state['folded'])
    for record in state['shown']:
        _fold(weeks, record)
    for rows in weeks.values():
        for people in rows.values():
            for counts in people.values():
                counts['rate'] = counts['played']/counts['shown'] if counts['shown'] and counts['complete'] else None
    return {'v': 1, 'weeks': weeks, 'sources': state['sources']}


def _save(data, state):
    # The evidence and folded counts commit together; the public view is rebuildable.
    _write(Path(data)/'metrics_shown.json', state)
    _write(Path(data)/'metrics.json', _totals(state))


def forget(data, config, before, person, *, apply=False):
    if not any((Path(data)/name).exists() for name in ('metrics.json', 'metrics_shown.json')):
        return 0
    cutoff = dt.datetime.fromisoformat(before).replace(tzinfo=dt.timezone.utc).timestamp()
    with _lock:
        state = _state(data, config, cutoff)
        touched, kept = set(), []
        for record in state['shown']:
            if record['person'] in (person, 'together') and any(t < cutoff for t in record['times']):
                touched.add((record['week'], record['row'], record['person']))
                record['times'] = [t for t in record['times'] if t >= cutoff]
                record['sources'] = []
            if record['times']:
                kept.append(record)
        state['shown'] = kept
        for week, rows in state['folded'].items():
            if week <= _week(cutoff, config):
                touched.update((week, row, who) for row, people in rows.items()
                               for who in people if who in (person, 'together'))
        for week, row, who in touched:
            _cell(state['folded'], week, row, who).update(played=0, sources={}, complete=False)
        if touched and apply:
            state['sources'] = {}
            _save(data, state)
        return len(touched)


def _after_forget(config, forgotten, person, at):
    people = config['household']['members'] if person == 'together' else [person]
    return all(p not in forgotten or at >= dt.datetime.fromisoformat(forgotten[p]).replace(
        tzinfo=dt.timezone.utc).timestamp() for p in people)


def _prune(state, config, now, *, daily=False):
    opts = settings(config)
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    if daily or state['cleaned'] != day:
        cutoff = now - opts['shown_retention_days'] * DAY
        remaining = []
        for record in state['shown']:
            old, record['times'] = record['times'], [t for t in record['times'] if t >= cutoff]
            if record['times']:
                remaining.append(record)
                if any(t < cutoff and now <= t + opts['attribution_days']*DAY for t in old):
                    _cell(state['folded'], record['week'], record['row'], record['person'])['complete'] = False
            else:
                cell = _fold(state['folded'], record)
                cell['closed'] = True
                if record['week'] == _week(now, config) or now <= max(old) + opts['attribution_days']*DAY:
                    cell['complete'] = False
        state['shown'] = remaining
        state['cleaned'] = day
    count = sum(len(r['times']) for r in state['shown'])
    state['shown'].sort(key=lambda r: max(r['times']))
    while count > opts['shown_max_items']:
        record = state['shown'].pop(0)
        count -= len(record['times'])
        cell = _fold(state['folded'], record)
        cell.update(complete=False, closed=True)


def record_shown(data, config, row, person, metas, now=None):
    now = time.time() if now is None else now
    if not settings(config)['enabled']:
        disable(data)
        return
    from .history import locked, cutoffs
    with locked(data), _lock:
        if person not in (*config['household']['members'], 'together'):
            return
        if not _after_forget(config, cutoffs(data), person, now):
            return
        state = _state(data, config, now)
        week = _week(now, config)
        cell = _cell(state['folded'], week, row, person)
        if cell['closed']:
            _prune(state, config, now)
            _save(data, state)
            return
        index = {(r['week'], r['row'], r['person'], r['key'], r['kind']): r for r in state['shown']}
        for meta in metas:
            key, kind = meta.get('id', ''), meta.get('type')
            kind = 'tv' if kind == 'series' else kind
            if not re.fullmatch(r'tmdb:[1-9][0-9]*', key) or kind not in ('movie', 'tv'):
                continue
            identity = (week, row, person, key, kind)
            record = index.get(identity)
            if record is None:
                record = dict(week=week, row=row, person=person, key=key, kind=kind, times=[], sources=[])
                state['shown'].append(record)
                index[identity] = record
            if now not in record['times']:
                record['times'].append(now)
        _prune(state, config, now)
        _save(data, state)


def update(data, config, plays, now=None, *, sources=(), cleanup=False):
    now = time.time() if now is None else now
    if not settings(config)['enabled']:
        disable(data)
        return
    from .history import locked, cutoffs
    with locked(data), _lock:
        state = _state(data, config, now)
        forgotten = cutoffs(data)
        coverage = {s: {'dated': 0, 'undated': 0} for s in sources if s in SOURCES}
        seen = set()
        index = {}
        for record in state['shown']:
            index.setdefault((record['key'], record['kind']), []).append(record)
        for play in plays:
            source, at = play['source'], play['at']
            if source not in SOURCES:
                continue
            dated = type(at) in (int, float) and math.isfinite(at) and 0 < at <= now
            if not dated and at is not None:
                continue
            if dated and not _after_forget(config, forgotten, play['person'], at):
                continue
            identity = (source, play['person'], play['key'], play['kind'], at)
            if identity in seen:
                continue
            seen.add(identity)
            coverage.setdefault(source, {'dated': 0, 'undated': 0})['dated' if dated else 'undated'] += 1
            if not dated:
                continue
            for record in index.get((play['key'], play['kind']), ()):
                if record['person'] not in (play['person'], 'together'):
                    continue
                if any(0 <= at - shown <= settings(config)['attribution_days']*DAY for shown in record['times']):
                    if source not in record['sources']:
                        record['sources'].append(source)
        state['sources'] = coverage
        _prune(state, config, now, daily=cleanup)
        _save(data, state)


def report(data, config, now=None, *, weeks=4, days=None):
    now = time.time() if now is None else now
    if days is not None and (type(days) is not int or not 1 <= days <= 35):
        raise ValueError('days must be 1..35')
    end = _week(now, config)
    start = (dt.date.fromisoformat(end) - dt.timedelta(weeks=weeks-1)).isoformat()
    if days is not None:
        start = _week(now - (days-1)*DAY, config)
    out = {'enabled': settings(config)['enabled'], 'from': start, 'to': end, 'rows': [], 'sources': {}}
    if not out['enabled']:
        return out
    with _lock:
        saved = _totals(_state(data, config, now))
    out['sources'] = saved['sources']
    for week, rows in sorted(saved['weeks'].items()):
        if not start <= week <= end:
            continue
        for row, people in sorted(rows.items()):
            for person, counts in sorted(people.items()):
                out['rows'].append(dict(week=week, row=row, person=person,
                    shown=counts['shown'], played=counts['played'], sources=counts['sources'],
                    complete=counts['complete'], rate=counts['rate']))
    return out


def collect(server, now):
    """Read existing local inputs only, preserving connector provenance before taste merges."""
    plays, sources = [], set()
    cfg = server.CONFIG
    people = cfg['household']['members']

    def add(item, person, source):
        key = item.get('id', '')
        kind = 'movie' if item.get('type') == 'movie' else 'tv'
        if not key.startswith('tmdb:'):
            tid = server._idmap.get(key)
            if tid is None:
                return
            key = 'tmdb:' + str(tid)
        at = None if '_summary' in item else item.get('at')
        plays.append(dict(key=key, kind=kind, person=person, source=source,
                          at=at/1000 if type(at) in (int, float) else None))

    def played(item):
        return bool(item.get('played') or (item.get('episodes_played') or 0) > 0 or
                    (item.get('progress') or 0) >= .25)

    history = cfg['history']['source']
    if history != 'none' and cfg['connectors'].get('history' if history == 'file' else history, {}).get('enabled', True):
        source = 'history' if history == 'file' else history
        sources.add(source)
        for item in server.read_history():
            if played(item):
                who = server.attribute(item)[0]
                if who in (*people, 'together'):
                    add(item, who, source)
    for source in ('trakt_api', 'mdblist', 'stremio'):
        connector = cfg['connectors'].get(source, {})
        if not connector.get('enabled'):
            continue
        sources.add(source)
        accounts = connector.get('accounts', {cfg['household']['trakt_member']: None})
        for person in accounts:
            name = ('trakt-' + person if 'accounts' in connector else 'trakt') if source == 'trakt_api' else source+'-'+person
            for item in server._drop_read(name):
                if played(item):
                    add(item, person, source)
    if not cfg['connectors'].get('trakt_api', {}).get('enabled') and cfg['connectors'].get('trakt', {}).get('enabled',
            (Path(server.DATA)/'trakt.json').is_file()):
        sources.add('trakt')
        for item in server._drop_read('trakt'):
            if played(item) or item.get('signal') == 'watched' and item.get('rating') is None:
                add(item, cfg['household']['trakt_member'], 'trakt')
    for event in server.active_imports():
        source = 'import:' + event['src']
        if source not in SOURCES or event['action'] != 'seen' or not event['conf']:
            continue
        sources.add(source)
        # Stored taste imports cannot distinguish a viewing date from a default import date.
        plays.append(dict(key=event['key'], kind=event['kind'], at=None,
                          person='together' if event['who'] == 'both' else event['who'], source=source))
    return plays, sources


def text(result):
    if not result['enabled']:
        yield 'Row metrics are off.'
        return
    yield f"Row weeks: {result['from']} to {result['to']} (week starts, household timezone)"
    yield 'Week        Person           Row                             Shown  Played  Rate'
    for row in result['rows']:
        rate = f"{row['rate']:.1%}" if row['rate'] is not None else 'n/a'
        yield f"{row['week']}  {row['person']:<15}  {row['row']:<30}  {row['shown']:>5}  {row['played']:>6}  {rate}"
        if row['sources']:
            yield '  Credited by source: ' + ', '.join(f'{s} {n}' for s, n in sorted(row['sources'].items()))
    if any(not r['complete'] for r in result['rows']):
        yield 'n/a: raw evidence was capped or removed early; these counts are incomplete.'
    yield 'Play sources (dated titles / titles without a usable play time in current local inputs):'
    for source, counts in sorted(result['sources'].items()):
        yield f"  {source}: {counts['dated']} / {counts['undated']}"
    if not result['sources']:
        yield '  none observed'
