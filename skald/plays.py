"""Odin watch state from the mounted AIOStreams database or saved logins."""
from contextlib import closing
from collections import Counter
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import urllib.parse

COLUMNS = ('uuid', 'media_type', 'base_id', 'last_played_at', 'played', 'kind', 'favorite',
           'duration_ms', 'position_ms', 'rating', 'likes', 'dropped', 'season', 'episode')
FIX = 'skald setup aiostreams'
_request_lock = threading.Lock()


class HistoryTruncated(ValueError):
    pass


def settings(cfg):
    return dict(mode='auto', every=900, people={}, live=False) | cfg.get('plays', {})


def validate(cfg, need):
    value = cfg.get('plays', {})
    need(isinstance(value, dict) and set(value) <= {'mode', 'every', 'people', 'live'}, 'plays')
    opts = settings(cfg)
    need(opts['mode'] in ('auto', 'db', 'https', 'off'), 'plays.mode')
    need(type(opts['every']) is int and 300 <= opts['every'] <= 21600, 'plays.every: use 5m to 6h')
    need(opts['live'] is False, 'plays.live: reserved, always off')
    need(isinstance(opts['people'], dict) and all(p in (*cfg['household']['members'], 'together') and
         type(on) is bool for p, on in opts['people'].items()), 'plays.people')


def duration(value):
    if isinstance(value, str) and len(value) > 1 and value[:-1].isascii() and value[:-1].isdigit() and value[-1] in ('m', 'h'):
        seconds = int(value[:-1]) * (60 if value[-1] == 'm' else 3600)
        if 300 <= seconds <= 21600:
            return seconds
    raise ValueError('plays every: use 5m to 6h')


def person_on(cfg, person):
    return settings(cfg)['people'].get('together' if person == 'household' else person, True)


def managed(s):
    return managed_config(s.CONFIG, s.DATA)


def managed_config(cfg, data=None):
    return cfg['history']['source'] == 'aiostreams_db' or 'plays' in cfg or (
        cfg['history']['source'] == 'none' and data is not None and (Path(data)/'credentials/aiostreams.json').exists())


def connector_id(s):
    return 'aiostreams_db' if s.CONFIG['history']['source'] == 'aiostreams_db' else 'player'


def db_options(s):
    return s.CONFIG['connectors'].get('aiostreams_db', {}).get('options')


def person_mapping(s, prefixes=()):
    from . import setup
    from .members import active_people
    claims, paths = {}, {}
    opts = db_options(s) or {}
    def add(prefix, person, path):
        prefix = prefix[:8].lower()
        if len(prefix) != 8 or any(c not in '0123456789abcdef' for c in prefix):
            return
        paths.setdefault(prefix, set()).add(path)
        if person in (*s.CONFIG['household']['members'], 'household', 'together'):
            claims.setdefault(prefix, set()).add('household' if person == 'together' else person)
    for prefix in prefixes:
        add(prefix, None, 'db')
    for prefix, person in opts.get('profiles', {}).items():
        add(prefix, person if prefix not in opts.get('archived', {}) else None, 'db')
    for person, login in setup.aiostreams_logins(s.DATA).items():
        if isinstance(login, dict) and isinstance(login.get('uuid'), str):
            add(login['uuid'], person, 'https')
    result = []
    for prefix in sorted(paths):
        owners = claims.get(prefix, set())
        person = next(iter(owners)) if len(owners) == 1 else None
        status = 'conflict' if len(owners) > 1 else 'mapped' if person in (*active_people(s.CONFIG), 'household') else 'unmapped'
        result.append(dict(login=prefix, person=person if status == 'mapped' else None,
                           paths=sorted(paths[prefix]), status=status))
    return result


def mappings(s, *, read_db=True):
    prefixes = []
    opts = db_options(s)
    if read_db and opts and not s.os.path.islink(opts['path']):
        try:
            uri = 'file:' + urllib.parse.quote(opts['path'], safe='/') + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
                prefixes = [r[0] for r in con.execute('select distinct lower(substr(uuid, 1, 8)) from watch_state') if isinstance(r[0], str)]
        except (OSError, sqlite3.Error):
            pass
    return {'items': person_mapping(s, prefixes)}


def database(s, options, *, details=False):
    if options != db_options(s) or s.os.path.islink(options['path']):
        raise s.PathMismatch()
    profiles = {r['login']: r['person'] for r in person_mapping(s) if r['status'] == 'mapped' and
                r['login'] not in options.get('archived', {}) and person_on(s.CONFIG, r['person'])}
    uri = 'file:' + urllib.parse.quote(options['path'], safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
        if not set(COLUMNS) <= {row[1] for row in con.execute('pragma table_info(watch_state)')}:
            raise s.SchemaMismatch()
        doc = aggregate(s, con, profiles)
        if details:
            doc['_rows'] = [list(r) for r in con.execute('select '+', '.join(COLUMNS)+' from watch_state')
                            if r[0][:8].lower() in profiles]
        return doc


def aggregate(s, con, profiles, backfills=()):
    con.create_function('burst', 2, lambda prefix, at: (prefix, (at or 0)//60000) in backfills)
    where = ' or '.join(['uuid like ?'] * len(profiles)) or '0'
    rows = con.execute(
        f"with marked as (select *, sum(case when kind = 'episode' and played = 1 then 1 else 0 end) "
        f"over (partition by lower(substr(uuid, 1, 8)), cast(last_played_at / 60000 as integer)) as minute_plays "
        f"from watch_state where ({where})), history as (select *, "
        f"last_played_at is not null and (minute_plays >= ? or burst(lower(substr(uuid, 1, 8)), last_played_at)) as backfill from marked) "
        f"select lower(substr(uuid, 1, 8)), media_type, base_id, "
        f"max(case when not backfill then last_played_at end), max(played), "
        f"sum(case when kind = 'episode' and played = 1 then 1 else 0 end), max(favorite), "
        f"max(case when duration_ms > 0 then 1.0 * position_ms / duration_ms else 0 end), count(*), max(rating), "
        f"max(likes), max(dropped), "
        f"max(case when kind = 'episode' and played = 1 then season * 1000 + episode end), "
        f"max(case when kind = 'episode' and played = 0 and position_ms > 0 then season * 1000 + episode end), "
        f"max(backfill) from history group by 1, 2, 3 order by 4 desc limit 120",
        (*tuple(p + '%' for p in profiles), s.BACKFILL_PLAYS)).fetchall()
    items = []
    for prefix, typ, base, at, played, eps, fav, prog, n, rating, likes, dropped, last, resume, backfill in rows:
        item = {'profile': profiles[prefix], 'type': typ, 'id': base, 'at': at or None, 'played': bool(played),
                'favorite': bool(fav), 'progress': round(prog or 0, 2), 'entries': n, 'rating': rating,
                'likes': likes, 'dropped': bool(dropped)}
        if backfill:
            item['backfilled'] = True
        if typ == 'series':
            item['episodes_played'] = eps or 0
            if last:
                item['last_episode'] = [last // 1000, last % 1000]
            if resume and (not last or resume > last):
                item['resume_episode'] = [resume // 1000, resume % 1000]
        items.append(item)
    return {'items': items}


def aggregate_rows(s, rows, profiles, backfills=()):
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE watch_state ('+', '.join(COLUMNS)+')')
        con.executemany('INSERT INTO watch_state VALUES ('+','.join('?' for _ in COLUMNS)+')', rows)
        return aggregate(s, con, profiles, backfills)


def logins(s):
    from . import setup, remote
    saved = setup.aiostreams_logins(s.DATA)
    base = remote.validate_base(setup.state(Path(s.DATA)).get('aio_url', ''))
    if urllib.parse.urlsplit(base).scheme != 'https':
        raise ValueError('HTTPS address required')
    if not saved:
        raise ValueError('no enabled saved logins')
    mapped = {r['login']: r['person'] for r in person_mapping(s) if r['status'] == 'mapped'}
    selected = {p: login for p, login in saved.items() if isinstance(login, dict) and
                mapped.get(str(login.get('uuid', ''))[:8].lower()) == ('household' if p == 'together' else p) and person_on(s.CONFIG, p)}
    for login in selected.values():
        if not isinstance(login, dict) or not all(isinstance(login.get(k), str) and login[k] for k in ('uuid', 'password')):
            raise ValueError('saved login incomplete')
    return base, selected


def select(s, *, read=False):
    mode = settings(s.CONFIG)['mode']
    if mode == 'off':
        return dict(path='off', reason='Odin plays are off', fix=None)
    reason, db_error = 'database is not configured', None
    opts = db_options(s)
    if mode in ('auto', 'db') and opts:
        try:
            if s.os.path.islink(opts['path']):
                raise s.PathMismatch()
            doc = None
            if read:
                doc = database(s, opts, details=True)
            else:
                uri = 'file:' + urllib.parse.quote(opts['path'], safe='/') + '?mode=ro'
                with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
                    if not set(COLUMNS) <= {row[1] for row in con.execute('pragma table_info(watch_state)')}:
                        raise s.SchemaMismatch()
            return dict(path='db', reason='mounted database has the required schema', fix=None, _doc=doc)
        except (OSError, sqlite3.Error, s.SchemaMismatch, s.PathMismatch) as error:
            reason = 'database unavailable ('+type(error).__name__+')'
            db_error = type(error).__name__
    if mode == 'db':
        return dict(path='not connected', reason=reason, fix='skald setup --connect aiostreams-db', error=db_error)
    try:
        logins(s)
        return dict(path='https', reason=('HTTPS selected' if mode == 'https' else reason+'; using saved AIOStreams logins'), fix=None)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(path='not connected', reason=reason+'; saved HTTPS setup is incomplete', fix=FIX, error=db_error)


def identity(row):
    try:
        raw = bytes.fromhex(row['Id'].replace('-', ''))
    except (ValueError, TypeError, KeyError):
        return None
    if len(raw) != 16 or raw[0] != 161:
        return None
    kind, provider = raw[1] >> 4, raw[1] & 15
    prefixes = {1: 'tt', 2: 'tmdb:', 3: 'tvdb:', 4: 'kitsu:', 5: 'mal:',
                6: 'anilist:', 7: 'anidb:', 8: 'simkl:'}
    types = {1: 'movie', 2: 'series', 3: 'anime', 4: 'tv', 5: 'other', 6: 'channel'}
    if kind not in (1, 2, 4) or provider not in prefixes or raw[2] not in types:
        return None
    number = int.from_bytes(raw[3:9], 'big')
    base = prefixes[provider]+(str(number).zfill(7) if provider == 1 else str(number))
    season, episode = (int.from_bytes(raw[a:a+2], 'big') for a in (9, 11))
    if kind == 4 and (season == 65535 or episode == 65535):
        return None
    return types[raw[2]], base, season if kind == 4 else None, episode if kind == 4 else None


def record(s, row, prefix):
    data = row.get('UserData')
    if not isinstance(data, dict) or not {'Played', 'PlayCount', 'PlaybackPositionTicks', 'IsFavorite'} <= data.keys():
        raise s.Unsupported()
    if any(type(data[k]) is not bool for k in ('Played', 'IsFavorite')) or any(
            type(data[k]) is not int or data[k] < 0 for k in ('PlayCount', 'PlaybackPositionTicks')):
        raise ValueError()
    ticks = row.get('RunTimeTicks', 0)
    if type(ticks) is not int or ticks < 0:
        raise ValueError()
    at = s._connector_time(data.get('LastPlayedDate'))
    if data.get('LastPlayedDate') is not None and (not isinstance(data['LastPlayedDate'], str) or at is None or at < 0):
        raise ValueError()
    key = identity(row)
    if key is None:
        return None, at
    typ, base, season, episode = key
    kind = 'episode' if episode is not None else 'movie' if row['Type'] == 'Movie' else 'series'
    if row.get('Type') != {'episode': 'Episode', 'movie': 'Movie', 'series': 'Series'}[kind] or (
            episode is not None and (row.get('ParentIndexNumber'), row.get('IndexNumber')) != (season, episode)):
        raise ValueError()
    rating, likes = data.get('Rating'), data.get('Likes')
    if rating is not None and (type(rating) not in (int, float) or not 0 <= rating <= 10) or likes is not None and type(likes) is not bool:
        raise ValueError()
    # shortcut: Jellyfin hides original fields and hashed IDs; use the DB until AIOStreams exposes them.
    return [prefix, typ, base, round(at*1000) if at is not None else None, int(data['Played']), kind,
            int(data['IsFavorite']), ticks, data['PlaybackPositionTicks'], rating,
            int(likes) if likes is not None else None, 0, season, episode], at


def row_key(row):
    return row[1], row[2], row[12], row[13]


def https(s, previous=None, now=None):
    base, accounts = logins(s)
    budget, cache, profiles, skipped = {'bytes': 0, 'pages': 0}, {}, {}, set()
    for person, login in accounts.items():
        prefix = login['uuid'][:8].lower()
        profile = 'household' if person == 'together' else person
        if prefix in profiles and profiles[prefix] != profile:
            raise ValueError('duplicate login prefix')
        profiles[prefix] = profile
        fingerprint = hashlib.sha256((base+'|'+login['uuid']).encode()).hexdigest()
        prior = (previous or {}).get(person, {})
        if prior.get('login') != fingerprint:
            prior = {}
        rows = {row_key(row): row[:] for row in prior.get('rows', [])}
        # Re-read the watermark minute so a backfill burst cannot split across polls.
        seen, cutoff = prior.get('seen', 0), prior.get('seen', 0)//60000*60000
        signed = s._http_get(base+'/jellyfin/Users/AuthenticateByName',
            {'Content-Type': 'application/json', 'Authorization': 'MediaBrowser Client="Skald", Device="Skald", DeviceId="skald-plays", Version="1"'},
            body={'Username': login['uuid'], 'Pw': login['password']}, budget=budget)
        if not isinstance(signed, dict) or not isinstance(signed.get('AccessToken'), str) or not signed['AccessToken']:
            raise s.Unsupported()
        uid = signed.get('User', {}).get('Id')
        if not isinstance(uid, str) or not uid:
            raise s.Unsupported()
        headers = {'X-Emby-Token': signed['AccessToken']}
        path = base+'/jellyfin/Users/'+urllib.parse.quote(uid, safe='')+'/Items'
        favorites, active, minutes = set(), set(), {}
        for filtered in ('IsPlayed', 'IsResumable', 'IsFavorite'):
            query = dict(Recursive='true', IncludeItemTypes='Movie,Episode,Series', Fields='ProviderIds',
                         EnableUserData='true', Filters=filtered, SortBy='DatePlayed', SortOrder='Descending', StartIndex=0, Limit=100)
            last_at = float('inf')
            while True:
                page = s._http_get(path+'?'+urllib.parse.urlencode(query), headers, budget=budget)
                if not isinstance(page, dict) or not isinstance(page.get('Items'), list) or type(page.get('TotalRecordCount')) is not int:
                    raise s.Unsupported()
                batch, total = page['Items'], page['TotalRecordCount']
                if total < 0 or len(batch) > 100 or page.get('StartIndex', query['StartIndex']) != query['StartIndex'] or not batch and total > query['StartIndex']:
                    raise ValueError()
                stop = False
                for item in batch:
                    row, at = record(s, item, prefix)
                    millis = round(at*1000) if at is not None else 0
                    if filtered == 'IsPlayed':
                        if millis > last_at:
                            raise ValueError('history is not newest first')
                        last_at = millis
                        if cutoff and millis < cutoff:
                            stop = True
                            break
                    if filtered == 'IsPlayed':
                        seen = max(seen, millis)
                        if item.get('Type') == 'Episode' and item['UserData']['Played'] and at is not None:
                            minutes.setdefault(millis//60000, set()).add(item.get('Id'))
                    if row is None:
                        skipped.add((person, item.get('Id')))
                        continue
                    key = row_key(row)
                    if filtered == 'IsFavorite':
                        favorites.add(key)
                    if filtered != 'IsPlayed':
                        active.add(key)
                    if key not in rows or (row[3] or 0) >= (rows[key][3] or 0):
                        rows[key] = row
                query['StartIndex'] += len(batch)
                # AIOStreams truncates these lists before Jellyfin paging.
                if total >= 500 and (filtered == 'IsFavorite' or filtered == 'IsPlayed' and not stop and query['StartIndex'] >= total):
                    raise HistoryTruncated()
                if stop or query['StartIndex'] >= total and (filtered != 'IsResumable' or len(batch) < 100):
                    break
        rows = {key: row for key, row in rows.items() if row[4] or key in active}
        for key, row in rows.items():
            row[6] = int(key in favorites)
        bursts = set(prior.get('bursts', [])) | {m for m, ids in minutes.items() if len(ids) >= s.BACKFILL_PLAYS}
        cache[person] = dict(login=fingerprint, seen=seen, rows=list(rows.values()), bursts=sorted(bursts))
    prune_cache(s.DATA, s.CONFIG, {'https': cache}, s.time.time() if now is None else now)
    combined = {}
    backfills = set()
    for person, account in cache.items():
        profile = 'household' if person == 'together' else person
        prefix = next(p for p, who in profiles.items() if who == profile)
        backfills.update((prefix, m) for m in account.get('bursts', []))
        for row in account['rows']:
            row = [prefix, *row[1:]]
            key = (profile, *row_key(row))
            if key not in combined or (row[3] or 0) >= (combined[key][3] or 0):
                combined[key] = row
    doc = aggregate_rows(s, combined.values(), profiles, backfills)
    return doc, cache, len(skipped)


def state(s):
    try:
        return json.loads((Path(s.DATA)/'plays.json').read_text())
    except FileNotFoundError:
        return {}


def prune_cache(data, config, saved, now, *, person=None, before=None):
    from . import history
    forgotten = history.cutoffs(data)
    cutoffs = {p: dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp()
               for p, day in forgotten.items()}
    if person is not None:
        cutoffs[person] = max(history.forget_cutoff(before), cutoffs.get(person, 0))
    source = config['history']['source']
    opts = config['connectors'].get('player' if source == 'none' else 'history' if source == 'file' else source, {})
    days, maximum = (config['history'].get(k) for k in ('keep_raw_days', 'max_raw_items'))
    def keep(who, row):
        at = (row[3] or 0)/1000
        owners = list(dict.fromkeys([*config['household']['members'], *cutoffs])) if who in ('household', 'together', 'both') else [who]
        return not (opts.get('keep_raw') is False or days is not None and at < now-days*86400 or
                    any(history.forget_action(owners, p, at, cutoff) != 'keep' for p, cutoff in cutoffs.items()))
    records = [r for r in saved.get('records', []) if keep(r['person'], r['row'])]
    accounts = saved.get('https', {})
    for who, account in accounts.items():
        account['rows'] = [r for r in account.get('rows', []) if keep(who, r)]
        account['bursts'] = [m for m in account.get('bursts', []) if keep(who, [None, None, None, m*60000])]
        if not keep(who, [None, None, None, account.get('seen', 0)]):
            account['seen'] = 0
    if maximum is not None:
        keys = {(r['person'], *row_key(r['row'])): r['row'][3] or 0 for r in records}
        keys.update({('household' if who == 'together' else who, *row_key(r)): r[3] or 0
                     for who, account in accounts.items() for r in account['rows']})
        allowed = set(sorted(keys, key=lambda k: keys[k], reverse=True)[:maximum])
        records = [r for r in records if (r['person'], *row_key(r['row'])) in allowed]
        for who, account in accounts.items():
            account['rows'] = [r for r in account['rows'] if ('household' if who == 'together' else who, *row_key(r)) in allowed]
            account['bursts'] = [m for m in account['bursts'] if any((r[3] or 0)//60000 == m for r in account['rows'])]
    if 'records' in saved:
        saved['records'] = records
    return saved


def purge(data, config, now, *, person=None, before=None, apply=True):
    from . import setup
    path = Path(data)/'plays.json'
    with _request_lock:
        if path.exists():
            saved = json.loads(path.read_text())
            cleaned = prune_cache(data, config, copy.deepcopy(saved), now, person=person, before=before)
            if apply and cleaned != saved:
                setup.write_private(path, json.dumps(cleaned)+'\n')


def status(s, *, checked=None):
    saved = checked if checked is not None else state(s)
    opts = settings(s.CONFIG)
    chosen = {k: saved[k] for k in ('path', 'reason', 'fix', 'error') if k in saved} if checked is not None else select(s)
    chosen.pop('_doc', None)
    if saved.get('error') and saved.get('mode') == settings(s.CONFIG)['mode'] and saved.get('attempted_path') == chosen['path']:
        chosen.update(path='not connected', reason=saved['reason'], fix=saved['fix'], error=saved['error'])
    next_check = max(saved['last_check']+opts['every'], saved.get('backoff_until', 0)) if saved.get('last_check') is not None and opts['mode'] != 'off' else None
    return {**opts, **chosen,
            'last_check': saved.get('last_check'), 'next_check': next_check,
            'new_plays': saved.get('new_plays', dict.fromkeys(s.PEOPLE, 0)),
            'unmatched_ids': saved.get('unmatched_ids', 0),
            'mappings': mappings(s, read_db=chosen['path'] in ('db', 'https'))['items'],
            'backoff_until': saved.get('backoff_until', 0),
            'limitations': ['Jellyfin derives likes and does not expose stored dropped flags, original runtime or inactive rows; opaque hashed IDs are skipped.'] if chosen['path'] == 'https' else []}


def check(s, now=None):
    from . import history, setup
    now = s.time.time() if now is None else now
    chosen = select(s, read=True)
    with history.locked(s.DATA):
        previous = prune_cache(s.DATA, s.CONFIG, state(s), now)
    doc = chosen.pop('_doc', None)
    rows = doc.pop('_rows', []) if doc is not None else []
    result = {**previous, **chosen, 'last_check': now,
              'mode': settings(s.CONFIG)['mode'], 'error': chosen.get('error'), 'backoff_until': 0,
              'new_plays': dict.fromkeys(s.PEOPLE, 0)}
    bursts = set()
    if chosen['path'] == 'https':
        try:
            doc, result['https'], result['unmatched_ids'] = https(s, previous.get('https'), now)
            for who, account in result['https'].items():
                rows.extend(account['rows'])
                bursts.update((r[0], m) for r in account['rows'] for m in account.get('bursts', []))
        except Exception as error:
            result.update(path='not connected', attempted_path='https', error=type(error).__name__,
                          reason='HTTPS read failed ('+type(error).__name__+')', fix=FIX)
            if getattr(error, 'code', None) == 429:
                try:
                    delay = float(error.headers.get('Retry-After', s.CONNECTOR_MIN))
                except (TypeError, ValueError, AttributeError):
                    delay = s.CONNECTOR_MIN
                result['backoff_until'] = now+(max(s.CONNECTOR_MIN, delay) if math.isfinite(delay) else s.CONNECTOR_MIN)
            if isinstance(error, HistoryTruncated):
                result.update(reason='AIOStreams caps this list at 500; a complete HTTPS read is unavailable',
                              fix='skald setup --connect aiostreams-db')
    with history.locked(s.DATA), _request_lock:
        # Forgetting or retention may have run while the transport was reading.
        latest = prune_cache(s.DATA, s.CONFIG, state(s), now)
        if 'requested' in latest:
            result['requested'] = latest['requested']
        result['records'] = latest.get('records', [])
        if doc is not None:
            mapped = {r['login']: r['person'] for r in mappings(s)['items'] if r['status'] == 'mapped'}
            counts = Counter((r[0][:8].lower(), r[3]//60000) for r in rows if r[5] == 'episode' and r[4] and r[3])
            bursts.update(k for k, n in counts.items() if n >= s.BACKFILL_PLAYS)
            records = {(r['person'], *row_key(r['row'])): r for r in result['records']
                       if mapped.get(r['row'][0]) == r['person']}
            old = {(it.get('profile'), it['type'], it['id']): it for it in s.read_history()}
            incoming = {}
            for row in rows:
                person = mapped.get(row[0][:8].lower())
                key = (person, *row_key(row))
                if person is not None and (key not in incoming or (row[3] or 0) > (incoming[key]['row'][3] or 0)):
                    incoming[key] = dict(person=person, row=[row[0][:8].lower(), *row[1:]])
            incoming = list(incoming.values())
            eligible = prune_cache(s.DATA, s.CONFIG, {'records': incoming}, now)['records']
            if len(eligible) != len(incoming) and history.cutoffs(s.DATA):
                doc = aggregate_rows(s, [r['row'] for r in eligible], mapped, bursts)
            incoming = eligible
            for record in incoming:
                person, row = record['person'], record['row']
                key = (person, *row_key(row))
                prior = records.get(key)
                backfill = (row[0], (row[3] or 0)//60000) in bursts
                before = (prior['row'][3] if prior['row'][4] else None) if prior else (
                    old.get(key[:3], {}).get('at') if 'records' not in latest and old.get(key[:3], {}).get('played') else None)
                if row[4] and row[3] and not backfill and row[3]//1000 > (before or 0)//1000:
                    result['new_plays'][person] = result['new_plays'].get(person, 0)+1
                source = chosen['path']
                if prior and (prior['row'][3] or 0) > (row[3] or 0):
                    row[3] = prior['row'][3]
                    source = prior['source']
                records[key] = dict(person=person, row=row, source=source, backfilled=backfill)
            result['records'] = list(records.values())
            prune_cache(s.DATA, s.CONFIG, result, now)
            for item in doc['items']:
                matching = [r for r in result['records'] if (r['person'], *r['row'][1:3]) ==
                            (item['profile'], item['type'], item['id']) and not r['backfilled']]
                item['at'] = max([item['at'] or 0, *((r['row'][3] or 0) for r in matching)], default=0) or None
            doc['items'] += [it for it in s._drop_read('history') if it.get('profile') in s.CONFIG['household'].get('retired', [])]
            s._connector_write('history', doc)
        prune_cache(s.DATA, s.CONFIG, result, now)
        setup.write_private(Path(s.DATA)/'plays.json', json.dumps(result)+'\n')
        return status(s, checked=result)


def compare(s):
    docs, errors = {}, {}
    for path, reader in (('db', lambda: database(s, db_options(s))), ('https', lambda: https(s)[0])):
        try:
            docs[path] = reader()['items']
        except Exception as error:
            errors[path] = type(error).__name__
    differences = {}
    for person in (*s.PEOPLE, 'household'):
        left, right = ({(it['type'], it['id']): it for it in docs.get(path, []) if it.get('profile') == person}
                       for path in ('db', 'https'))
        differences[person] = [dict(type=key[0], id=key[1], db=left.get(key), https=right.get(key))
                               for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key)]
    return dict(equal=not errors and not any(differences.values()), differences=differences, errors=errors)


def changed(cfg, op, value, person=None, *, data=None):
    cfg = copy.deepcopy(cfg)
    opts = settings(cfg)
    if op == 'mode':
        opts['mode'] = value
    elif op == 'every':
        opts['every'] = duration(value)
    elif op == 'person':
        from .members import active_people
        if person not in (*active_people(cfg), 'together'):
            raise ValueError('Choose a current member or together')
        if value not in ('on', 'off'):
            raise ValueError('plays person: use on or off')
        if not managed_config(cfg, data):
            raise ValueError('Odin plays are not set up.')
        opts['people'] = {**opts['people'], person: value == 'on'}
    else:
        raise ValueError('unknown plays setting')
    cfg['plays'] = opts
    return cfg


def change(s, op, value, person=None):
    from . import commands, setup, history
    with history.locked(s.DATA):
        cfg = s.validate_config(changed(commands.config(s), op, value, person, data=s.DATA), data=s.DATA)
        setup.publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
    return status(s)


def text(data):
    if 'items' in data:
        return '\n'.join(json.dumps(it, ensure_ascii=False) for it in data['items']) or 'No plays found'
    if 'differences' in data:
        lines = ['Paths agree' if data['equal'] else 'Paths differ']
        for person, rows in data['differences'].items():
            lines.append(person+': '+str(len(rows))+' differences')
            for row in rows:
                left, right = row['db'], row['https']
                detail = 'HTTPS only' if left is None else 'DB only' if right is None else ', '.join(
                    key+': DB '+str(left.get(key))+', HTTPS '+str(right.get(key))
                    for key in sorted(left.keys() | right.keys()) if left.get(key) != right.get(key))
                lines.append('  '+row['type']+' '+row['id']+': '+detail)
        return '\n'.join(lines+[path+': '+error for path, error in data['errors'].items()])
    return '\n'.join([f"Odin plays: {data['mode']}; {data['path']}", data['reason'],
        f"Last check: {data['last_check']}; next check: {data['next_check']}; every {data['every']//60}m",
        'New plays: '+', '.join(p+' '+str(n) for p, n in data['new_plays'].items())] +
        ['Login '+r['login']+': '+r['status'] for r in data.get('mappings', []) if r['status'] != 'mapped'] +
        ([data['fix']] if data.get('fix') else []) + data.get('limitations', []))


def may_see(s, viewer, item):
    from .members import active_people
    if viewer not in active_people(s.CONFIG):
        return False
    shared = viewer != s.GUEST and viewer in s.adult_people()
    if 'history_by' in item:
        return viewer in item['history_by'] or bool(set(item['history_by']) & {'both', 'together'}) and shared
    profile = item.get('profile', 'household')
    if profile in s.PEOPLE:
        return profile == viewer
    if profile not in ('household', 'together'):
        return False
    return shared


def recent(s, viewer, limit=20, *, title=None):
    if type(limit) is not int or not 1 <= limit <= 120:
        raise ValueError('plays limit: use 1 to 120')
    now = s.time.time()
    profile = s._profiles.get('household')
    if profile is None:
        try:
            profile = json.loads((Path(s.DATA)/'profile.json').read_text())
        except FileNotFoundError:
            profile = {}
    titles = {e['key']: e.get('title', e['key']) for e in profile.get('entries', [])}
    saved = prune_cache(s.DATA, s.CONFIG, state(s), now)
    if 'records' in saved:
        mapped = {r['login']: r['person'] for r in person_mapping(s) if r['status'] == 'mapped'}
        corrections = {(it.get('profile'), it['type'], it['id']): it['history_by']
                       for it in s.read_history() if 'history_by' in it}
        history = [dict(profile=r['person'], type=r['row'][1], id=r['row'][2], at=r['row'][3],
                        played=bool(r['row'][4]), season=r['row'][12], episode=r['row'][13],
                        login=r['row'][0], source=r['source'], backfilled=r['backfilled'],
                        **({'history_by': corrections[(r['person'], *r['row'][1:3])]}
                           if (r['person'], *r['row'][1:3]) in corrections else {}))
                   for r in saved['records'] if mapped.get(r['row'][0]) == r['person']]
    else:
        history = s.read_history()
    items = []
    for it in history:
        if not may_see(s, viewer, it) or not it.get('played'):
            continue
        name = titles.get(it['id'], it.get('title') or it['id'])
        if title is not None:
            if title.casefold() not in (name.casefold(), it['id'].casefold()):
                continue
        elif it.get('backfilled') or it.get('at') is None or not now-7*s.DAY <= it['at']/1000 <= now:
            continue
        items.append(dict(id=it['id'], type=it['type'], title=name, person=it.get('profile', 'household'),
                          at=None if it.get('backfilled') else it.get('at'),
                          season=it.get('season', (it.get('last_episode') or [None, None])[0]),
                          episode=it.get('episode', (it.get('last_episode') or [None, None])[1]),
                          login=it.get('login'), source=it.get('source'), backfilled=bool(it.get('backfilled')),
                          together=it.get('profile', 'household') in ('household', 'together')))
    return dict(items=sorted(items, key=lambda it: -(it['at'] or 0))[:limit])


def explain(s, viewer, title):
    if not isinstance(title, str) or not title.strip():
        raise ValueError('plays why needs a title')
    return recent(s, viewer, 120, title=title)


def run_check(s):
    from . import setup
    with _request_lock:
        now, saved = s.time.time(), state(s)
        if saved.get('requested') is not None and now-saved['requested'] < 120:
            return {**status(s), 'rate_limited': True, 'retry_after': max(0, 120-int(now-saved['requested']))}
        saved['requested'] = now
        setup.write_private(Path(s.DATA)/'plays.json', json.dumps(saved)+'\n')
    next_check = s.plays_queue_check()
    return {**status(s), 'queued': True, 'next_check': next_check}


def preview(s, query, change, viewer, bound, *_):
    from . import commands
    op, values = change['op'].removeprefix('plays_'), change['values']
    if change['kind'] is not None:
        raise ValueError('plays command kind')
    actor = bound if bound in (*s.PEOPLE, s.GUEST) else viewer
    if actor not in s.PEOPLE:
        raise PermissionError('Choose who is watching to read or change plays')
    if op in ('status', 'recent', 'explain', 'check'):
        if (len(values) != 1 if op == 'explain' else bool(values)) or change['person'] not in (None, actor):
            raise PermissionError('Read your own plays')
        data = explain(s, actor, values[0]) if op == 'explain' else recent(s, actor) if op == 'recent' else run_check(s) if op == 'check' else status(s)
        if 'new_plays' in data:
            data['new_plays'] = {actor: data['new_plays'].get(actor, 0)}
            data.pop('people', None)
            data.pop('mappings', None)
        description = text(data) if op in ('recent', 'explain') else 'Checking now, results in a minute' if data.get('queued') else text(data)
        if data.get('queued') and data.get('next_check') is not None and data.get('backoff_until', 0) > s.time.time():
            when = dt.datetime.fromtimestamp(data['next_check'], dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            description = 'Check queued for '+when+' (Retry-After backoff)'
        if data.get('rate_limited'):
            description += '\nCheck already requested; try again in '+str(data['retry_after'])+' seconds'
        return dict(read_only=True, people=[actor], name='Odin plays' if op != 'recent' else 'Your plays this week',
                    description=description, plays=data)
    commands.people(s, viewer=viewer, bound=bound)
    if len(values) != 1 or op not in ('mode', 'every', 'person') or op != 'person' and change['person'] is not None:
        raise ValueError('one plays setting at a time')
    cfg = commands.config(s)
    after = s.validate_config(changed(cfg, op, values[0], change['person'] or actor, data=s.DATA), data=s.DATA)['plays']
    label = ((change['person'] or actor)+' ' if op == 'person' else '')+str(values[0])
    return commands.seal(dict(people=[actor], actor=actor, change=change,
        plays=dict(before=copy.deepcopy(cfg.get('plays')), after=after), text=query[:200], source='search',
        name='Odin plays '+op+': '+label))


def apply(s, plan):
    from . import commands, setup, history
    with history.locked(s.DATA):
        commands.preview(s, plan['text'], plan['actor'], answer=plan['change'])
        cfg, entries = commands.config(s), commands.journal(s)
        if cfg.get('plays') != plan['plays']['before']:
            raise ValueError('Plays settings changed; preview again')
        cfg['plays'] = plan['plays']['after']
        setup.publish(Path(s.DATA), s.validate_config(cfg, data=s.DATA), replace=True)
        s.refresh_preferences()
        actor = plan['actor']
        prefs = cfg['taste']['people'][actor].get('prefs')
        entries[actor] = (entries.get(actor, [])+[commands.history_entry(s, plan, prefs, prefs, plays=plan['plays'])])[-200:]
        commands.save_journal(s, entries)
    return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}


def restore(cfg, before, after):
    if cfg.get('plays') != after:
        raise ValueError('Plays settings changed; cannot undo this command')
    cfg.pop('plays', None)
    if before is not None:
        cfg['plays'] = before
