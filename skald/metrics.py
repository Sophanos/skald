"""Local, counts-only row cohorts with bounded, disposable attribution evidence."""
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo

DAY = 86400
MAX_PLAY_CREDITS = 10000
DEFAULTS = dict(enabled=False, attribution_days=14, shown_retention_days=30, shown_max_items=10000)
MOMENTS = ('day', 'evening', 'late', 'weekend')
SOURCES = {'history', 'jellyfin', 'plex', 'aiostreams_db', 'trakt', 'trakt_api', 'stremio', 'mdblist',
           'import:netflix', 'import:trakt', 'import:letterboxd'}
_lock = threading.RLock()


def settings(config):
    return {**DEFAULTS, **config.get('metrics', {})}


def _people(config):
    from .members import active_people, guest_people
    return [p for p in active_people(config) if p not in guest_people(config)]


def empty():
    return {'v': 1, 'weeks': {}, 'sources': {}}


def moment(config, at):
    local = dt.datetime.fromtimestamp(at, ZoneInfo(config['region']['tz']))
    hour, day = local.hour, local.weekday()
    bucket = ('late' if hour >= 22 or hour < 4 else
              'weekend' if day == 4 and hour >= 17 or day in (5, 6) else
              'evening' if day < 4 and hour >= 17 else 'day')
    return bucket, local.month


def context(config, at):
    bucket, month = moment(config, at)
    return dict(at=at, moment=bucket, month=month,
                date=dt.datetime.fromtimestamp(at, ZoneInfo(config['region']['tz'])).date().isoformat())


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
                cell.setdefault('extra', {})['restored'] = True
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
    if record['sources'] and record.get('play'):
        plays = cell.setdefault('extra', {}).setdefault('plays', [])
        if len(plays) < MAX_PLAY_CREDITS:
            plays.append(record['play'])
    for source in record['sources']:
        cell['sources'][source] = cell['sources'].get(source, 0) + 1
    if record.get('contexts') or record.get('play_context'):
        extra = cell.setdefault('extra', {})
        contexts = record.get('contexts', {})
        extra['contextual_shown'] = extra.get('contextual_shown', 0) + bool(contexts)
        extra.setdefault('showings', {}).update(contexts)
        extra['days'] = sorted(set(extra.get('days', [])) | {c['date'] for c in contexts.values()})
        buckets = extra.setdefault('moments', {})
        for bucket in {c['moment'] for c in contexts.values()}:
            buckets.setdefault(bucket, {'shown': 0, 'played': 0})['shown'] += 1
        if record.get('play_context') and record['sources']:
            play = record['play_context']
            extra.setdefault('play_contexts', {})[record['play']] = play
            buckets.setdefault(play['moment'], {'shown': 0, 'played': 0})['played'] += 1
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


def forget(data, config, before, person, *, apply=False, erase=False):
    if not any((Path(data)/name).exists() for name in ('metrics.json', 'metrics_shown.json')):
        return 0
    cutoff = dt.datetime.fromisoformat(before).replace(tzinfo=dt.timezone.utc).timestamp()
    with _lock:
        state = _state(data, config, cutoff)
        if erase:
            touched = sum(r['person'] in (person, 'together') for r in state['shown'])
            state['shown'] = [r for r in state['shown'] if r['person'] not in (person, 'together')]
            for rows in state['folded'].values():
                for people in rows.values():
                    for who in (person, 'together'):
                        touched += who in people
                        people.pop(who, None)
            if touched and apply:
                state['sources'] = {}
                _save(data, state)
            return touched
        touched, kept = set(), []
        for record in state['shown']:
            if record['person'] in (person, 'together') and any(t < cutoff for t in record['times']):
                touched.add((record['week'], record['row'], record['person']))
                record['times'] = [t for t in record['times'] if t >= cutoff]
                record['sources'] = []
                record.pop('play', None)
                record.pop('play_context', None)
                if 'contexts' in record:
                    record['contexts'] = {k: c for k, c in record['contexts'].items() if c['at'] >= cutoff}
            if record['times']:
                kept.append(record)
        state['shown'] = kept
        for week, rows in state['folded'].items():
            if week <= _week(cutoff, config):
                touched.update((week, row, who) for row, people in rows.items()
                               for who in people if who in (person, 'together'))
        for week, row, who in touched:
            cell = _cell(state['folded'], week, row, who)
            cell.update(played=0, sources={}, complete=False)
            cell.setdefault('extra', {})['plays'] = []
            cell['extra']['play_contexts'] = {}
            for bucket in cell['extra'].get('moments', {}).values():
                bucket['played'] = 0
        if touched and apply:
            state['sources'] = {}
            _save(data, state)
        return len(touched)


def _after_forget(config, forgotten, person, at):
    people = _people(config) if person == 'together' else [person]
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
        if person not in (*_people(config), 'together'):
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
            record.setdefault('contexts', {})[str(now)] = context(config, now)
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
            if record['person'] not in (*_people(config), 'together'):
                continue
            index.setdefault((record['key'], record['kind']), []).append(record)
        for play in plays:
            source, at = play['source'], play['at']
            if source not in SOURCES or play['person'] not in (*_people(config), 'together'):
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
            # Connector copies of the same dated play share an opaque credit.
            credit = hashlib.sha256(json.dumps(
                [play['person'], play['key'], play['kind'], float(at)]).encode()).hexdigest()
            for record in index.get((play['key'], play['kind']), ()):
                if record['person'] not in (play['person'], 'together'):
                    continue
                if any(0 <= at - shown <= settings(config)['attribution_days']*DAY for shown in record['times']):
                    if source not in record['sources']:
                        record['sources'].append(source)
                    if 'play' not in record:
                        record['play'] = credit
                        record['play_context'] = context(config, at)
        state['sources'] = coverage
        _prune(state, config, now, daily=cleanup)
        _save(data, state)


def report(data, config, now=None, *, weeks=4, days=None, moment=None):
    now = time.time() if now is None else now
    if days is not None and (type(days) is not int or not 1 <= days <= 35):
        raise ValueError('days must be 1..35')
    if moment is not None and moment not in MOMENTS:
        raise ValueError('unknown moment')
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
                if person not in (*_people(config), 'together'):
                    continue
                if moment is not None:
                    bucket = counts.get('extra', {}).get('moments', {}).get(moment)
                    if bucket is None:
                        continue
                    counts = {**counts, **bucket, 'sources': {}, 'rate': bucket['played']/bucket['shown']
                              if bucket['shown'] and counts['complete'] else None}
                out['rows'].append(dict(week=week, row=row, person=person,
                    shown=counts['shown'], played=counts['played'], sources=counts['sources'],
                    complete=counts['complete'], rate=counts['rate']))
    return out


def row_learning(store, *, scope, now, attribution_days=None, families=None):
    """Return bounded learning from enabled, complete counts in store['weeks'].

    extra carries tz (UTC by default), attribution_days and restored (no raw evidence).
    A caller may also mark individual restored cells with extra.restored.
    extra.plays holds opaque play credits; played counts without these cannot learn.
    Each play shares its credit across eligible cohorts and people in the scope.
    Mood and lane evidence is pooled by family; every slot gets the same L, not a theme claim.
    Kid and guest scopes have no separately recorded cohorts and stay neutral.
    """
    return {row: details['L'] for row, details in row_learning_details(
        store, scope=scope, now=now, attribution_days=attribution_days, families=families).items()}


def row_learning_details(store, *, scope, now, attribution_days=None, families=None, moment=None):
    """Pure cohort diagnostics behind row_learning; support is mature week count."""
    if not store or not store.get('enabled') or scope == 'guest' or scope.startswith('kid:'):
        return {}
    extra = store.get('extra', {})
    if extra.get('restored'):
        return {}
    families = families or {}
    attribution_days = extra.get('attribution_days', DEFAULTS['attribution_days']) if attribution_days is None else attribution_days
    zone = ZoneInfo(extra.get('tz', 'UTC'))
    today = dt.datetime.fromtimestamp(now, zone).date()
    start = today - dt.timedelta(days=today.weekday(), weeks=12)
    pools, row_pools, plays = {}, {}, {}
    for week, rows in sorted(store.get('weeks', {}).items()):
        date = dt.date.fromisoformat(week)
        end = dt.datetime.combine(date + dt.timedelta(days=7), dt.time(), zone).timestamp()
        mature = dt.datetime.combine(date + dt.timedelta(days=7 + attribution_days), dt.time(), zone).timestamp()
        if date < start or now <= mature:
            continue
        weight = 2 ** (-(now - end) / (28 * DAY))
        for row, people in sorted(rows.items()):
            family = families.get(row)
            if family in ('utility', 'chart', 'shelf'):
                continue
            pool = ('family', family) if family in ('mood', 'lane') else ('row', row)
            for person, cell in sorted(people.items()):
                if (scope != 'household' and person != scope or not cell.get('complete', False)
                        or 'people' in extra and person not in (*extra['people'], 'together')
                        or cell.get('extra', {}).get('restored')):
                    continue
                credits = cell.get('extra', {}).get('plays', [])
                if moment is not None:
                    bucket = cell.get('extra', {}).get('moments', {}).get(moment)
                    if bucket is None:
                        continue
                    cell = {**cell, **bucket}
                    credits = [credit for credit, c in cell.get('extra', {}).get('play_contexts', {}).items()
                               if c['moment'] == moment]
                if not cell['shown'] or len(credits) != cell['played']:
                    continue
                total = pools.setdefault(pool, {'S': 0.0, 'P': 0.0, 'weeks': set()})
                total['S'] += weight * cell['shown']
                for play in credits:
                    plays.setdefault(play, []).append((pool, weight))
                total['weeks'].add(week)
                row_pools[row] = pool
    if not pools:
        return {}
    for credits in plays.values():
        for pool, weight in credits:
            pools[pool]['P'] += weight / len(credits)
    for row, family in sorted(families.items()):
        pool = ('family', family)
        if pool in pools:
            row_pools[row] = pool
    p0 = (sum(r['P'] for r in pools.values()) + 1) / (sum(r['S'] for r in pools.values()) + 1000)
    results = {}
    for pool, counts in pools.items():
        shown, played, support = counts['S'], counts['P'], len(counts['weeks'])
        p = (played + 1000 * p0) / (shown + 1000)
        limit = .01
        prior = 1000 * p0
        limited = min(played, max(0, .05 / (limit * math.log(2)) - prior))
        gain = limit * limited + .05 * math.log2((prior + played) / (prior + limited))
        learning = max(-.05, min(.05, .05 * math.log2(1000 / (shown + 1000)) + gain))
        neutral = 'fewer than two mature weeks' if support < 2 else (
            'fewer than 200 effective showings' if shown < 200 else '')
        results[pool] = dict(S=shown, P=played, p=p, p0=p0, support=support,
                             L=0.0 if neutral else learning, neutral=neutral)
    return {row: dict(results[pool]) for row, pool in sorted(row_pools.items())}


def learning_report(data, config, now=None, *, scope='household'):
    """Read local cohorts without saving, cleaning or loading the server."""
    now = time.time() if now is None else now
    out = {'enabled': settings(config)['enabled'], 'scope': scope, 'rows': {}, 'neutral': ''}
    if not out['enabled']:
        out['neutral'] = 'metrics are off'
        return out
    with _lock:
        state = _read(Path(data)/'metrics_shown.json', None)
        if state is None:
            out['neutral'] = 'restored counts have no attribution evidence' if (Path(data)/'metrics.json').exists() else 'metrics are missing'
            return out
        store = {**_totals(_state(data, config, now)), 'enabled': True,
                 'extra': {'tz': config['region']['tz'], 'attribution_days': settings(config)['attribution_days'],
                           'people': _people(config)}}
    from .members import rid
    families = {rid(name, config): family for family, names in (
        ('utility', ('episodes', 'catchup', 'upcoming', 'rewatch', 'airing', 'nextseason', 'interview')),
        ('chart', ('top10', 'digital')),
        ('mood', tuple(f'mood{i}' for i in range(1, 5))),
        ('lane', tuple(f'lane{i}' for i in range(1, 6)) + ('lanebench1', 'lanebench2')),
    ) for name in names}
    for rows in store['weeks'].values():
        for row in rows:
            if row.startswith(rid('shelf_', config)):
                families[row] = 'shelf'
    out['rows'] = row_learning_details(store, scope=scope, now=now, families=families)
    if not out['rows']:
        out['neutral'] = 'no complete mature cohorts for this scope'
    return out


def learning_text(result):
    yield f"Row learning: {result['scope']} (mood and lane slots share family evidence)"
    yield 'Row                             S        P        p       p0  support        L  Neutral reason'
    for row, r in result['rows'].items():
        yield f"{row:<30}  {r['S']:>7.1f}  {r['P']:>7.1f}  {r['p']:>7.4f}  {r['p0']:>7.4f}  {r['support']:>7}  {r['L']:>7.4f}  {r['neutral']}"
    if result['neutral']:
        yield result['neutral']


def collect(server, now):
    """Read existing local inputs only, preserving connector provenance before taste merges."""
    plays, sources = [], set()
    cfg = server.CONFIG
    people = _people(cfg)

    def add(item, person, source):
        if person not in (*people, 'together'):
            return
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
            if person not in people:
                continue
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
        if source not in SOURCES or event['action'] != 'seen' or not event['conf'] or event['who'] not in (*people, 'both', 'together'):
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


def moments_report(data, config, now=None):
    now = time.time() if now is None else now
    from . import i18n
    enabled = settings(config)['enabled']
    out = dict(enabled=enabled, rows=[], undated_imports=0,
               paused_reason=None if enabled else i18n.t('lab.paused', config['region']['lang']))
    if not enabled:
        return out
    with _lock:
        saved = _totals(_state(data, config, now))
    out['undated_imports'] = sum(c['undated'] for source, c in saved['sources'].items() if source.startswith('import:'))
    rows, credits = {}, {}
    for cohorts in saved['weeks'].values():
        for row, people in cohorts.items():
            entry = rows.setdefault(row, dict(row=row, moments={m: dict(showings=0, plays=0) for m in MOMENTS},
                                             unknown=dict(showings=0, plays=0)))
            for cell in people.values():
                extra = cell.get('extra', {})
                entry['unknown']['showings'] += cell['shown'] - extra.get('contextual_shown', 0)
                entry['unknown']['plays'] += cell['played'] - len(extra.get('play_contexts', {}))
                for bucket, counts in extra.get('moments', {}).items():
                    entry['moments'][bucket]['showings'] += counts['shown']
                for credit, context in extra.get('play_contexts', {}).items():
                    if credit not in credits.setdefault(row, set()):
                        entry['moments'][context['moment']]['plays'] += 1
                        credits[row].add(credit)
    out['rows'] = [rows[row] for row in sorted(rows)]
    return out


def moments_text(result, lang='en'):
    from . import i18n
    yield i18n.t('lab.moments', lang)
    if result['paused_reason']:
        yield result['paused_reason']
    for row in result['rows']:
        for bucket, counts in row['moments'].items():
            yield i18n.t('lab.moment_counts', lang, row=row['row'], moment=i18n.t('moment.'+bucket, lang), **counts)
    yield i18n.t('lab.undated', lang, count=result['undated_imports'])
