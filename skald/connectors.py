"""Read-only probes using the server's existing bounded clients."""
from contextlib import closing, redirect_stdout
import io
import json
import os
import sqlite3
import time
import uuid
import urllib.parse
from unittest.mock import patch


def source_users(s, cid, options, auth=None):
    if cid == 'aiostreams_db':
        if s.os.path.islink(options['path']):
            raise ValueError('database must be a regular mounted file')
        uri = 'file:' + urllib.parse.quote(options['path'], safe='/') + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as con:
            return {row[0]: row[0] for row in con.execute('select distinct lower(substr(uuid,1,8)) from watch_state')}
    token = s._resolve_auth(auth or s.CONFIG['connectors'][cid]['auth'])
    base = options['url'].rstrip('/')
    if cid == 'jellyfin':
        rows = s._http_get(base + '/Users', {'X-Emby-Token': token})
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) and all(
                isinstance(row.get(field), str) and row[field].strip() for field in ('Id', 'Name')) for row in rows):
            raise ValueError('no readable Jellyfin users; check the source address, API key and server users')
        return {row['Id']: row['Name'] for row in rows}
    if cid == 'plex':
        response = s._http_get(base + '/myplex/account', {'X-Plex-Token': token, 'Accept': 'application/json'})
        account = response.get('MyPlex') if isinstance(response, dict) else None
        if not isinstance(account, dict) or not isinstance(account.get('username'), str) or not account['username'].strip():
            raise ValueError('no readable Plex account; check the source address and token')
        return {'account': account['username']}
    return {}


def aiostreams_login(path, prefix, private_uuid=None):
    """Resolve a login without conflating accounts sharing the reader's eight digits."""
    if not isinstance(prefix, str) or not prefix or any(c not in '0123456789abcdef-' for c in prefix.lower()):
        raise ValueError('supply an AIOStreams login prefix with --aio-login')
    prefix = prefix.lower()
    try:
        if private_uuid is not None:
            private_uuid = str(uuid.UUID(private_uuid))
        uri = 'file:' + urllib.parse.quote(path, safe='/') + '?mode=ro'
        if os.path.islink(path):
            raise ValueError()
        with closing(sqlite3.connect(uri, uri=True)) as con:
            users = {row[0].lower() for row in con.execute('select distinct uuid from watch_state')}
    except (ValueError, sqlite3.Error, AttributeError):
        raise ValueError('cannot resolve login; check the read-only database and private UUID') from None
    matches = {u for u in users if u.startswith(prefix)}
    if private_uuid is not None:
        if not private_uuid.startswith(prefix):
            raise ValueError('private UUID does not match --aio-login')
        matches.add(private_uuid)
    if not matches:
        raise ValueError('login has no history; supply its full UUID privately with --uuid-stdin')
    if len(matches) != 1:
        raise ValueError('ambiguous AIOStreams login prefix')
    selected = matches.pop()
    if any(u != selected and u[:8] == selected[:8] for u in users):
        raise ValueError('ambiguous eight-character AIOStreams login prefix')
    return selected


def aiostreams_access_key(path):
    """AIOStreams settings stores dotted keys and JSON-encoded values."""
    try:
        if os.path.islink(path):
            raise ValueError()
        uri = 'file:' + urllib.parse.quote(path, safe='/') + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as con:
            columns = {row[1]: row for row in con.execute('pragma table_info(settings)')}
            if not {'key', 'value', 'updated_at', 'updated_by'} <= columns.keys() or not columns['key'][5]:
                raise ValueError()
            row = con.execute('select value from settings where key=?', ('api.configAccessKey',)).fetchone()
        value = json.loads(row[0]) if row else None
        if value is not None and (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)):
            raise ValueError()
        return value
    except (sqlite3.Error, ValueError, TypeError):
        raise ValueError('cannot read AIOStreams settings; check the settings schema and read-only database mount') from None


def test(s, name=None, *, transport=None, now=None, show=False):
    now = time.time() if now is None else now
    name = {'trakt': 'trakt_api', 'aiostreams-db': 'aiostreams_db'}.get(name, name)
    configured = s.CONFIG['connectors']
    if name and name not in configured:
        raise ValueError('connector is not configured; run skald setup --connect ' + name.removesuffix('_api'))
    results = []
    with patch.object(s, '_connector_open', transport or s._connector_open), redirect_stdout(io.StringIO()):
        for cid, cfg in configured.items():
            if name and cid != name or not name and not cfg['enabled']:
                continue
            row = {'name': cid, 'reachable': False, 'auth_ok': False, 'unmapped': 0,
                   'mapping_complete': False, 'plays_30d': 0, 'ok': False}
            if cid == 'pmdb':
                row['accounts'] = []
                for person, auth in cfg['accounts'].items():
                    account = {'person': person, 'ok': False, 'plays_30d': 0}
                    try:
                        _, _, stats = s._connector_pmdb({'id': 'pmdb:' + person, 'person': person,
                            'auth': auth, 'cursor': None}, now, readonly=True)
                        account.update(ok=True, plays_30d=stats['plays_30d'])
                    except Exception as error:
                        account['error'] = s.PMDB_ERRORS.get(type(error).__name__, 'PublicMetaDB probe failed')
                    row['accounts'].append(account)
                row.update(ok=all(a['ok'] for a in row['accounts']),
                           plays_30d=sum(a['plays_30d'] for a in row['accounts']))
                row.update(reachable=row['ok'], auth_ok=row['ok'], mapping_complete=True)
                if not row['ok']:
                    row['error'] = '; '.join(a['person'] + ': ' + a['error'] for a in row['accounts'] if not a['ok'])
                results.append(row)
                continue
            try:
                if cid in ('trakt_api', 'mdblist', 'stremio'):
                    refs = cfg.get('accounts', {s.CONFIG['household']['trakt_member']: cfg.get('auth')})
                    for person, auth in refs.items():
                        _, _, stats = s.CONNECTOR_POLL[cid]({'id': cid + ':' + person, 'person': person,
                            'auth': auth, 'cursor': None}, now, readonly=True)
                        row['plays_30d'] += stats['plays_30d']
                        if cid == 'stremio':
                            row['likes_available'] = row.get('likes_available', True) and stats['likes_available']
                    unmapped = []
                elif cid in ('jellyfin', 'plex', 'aiostreams_db'):
                    opts = cfg['options']
                    if cid == 'aiostreams_db':
                        row['skipped'] = len(opts.get('archived', {}))
                    users = source_users(s, cid, opts)
                    row.update(reachable=True, auth_ok=True)
                    selected = (opts['profiles'].keys() | opts.get('archived', {}).keys()) if cid == 'aiostreams_db' else \
                        opts.get('users', [opts.get('user_id')]) if cid == 'jellyfin' else ['account']
                    unmapped = [label for uid, label in users.items() if uid not in selected]
                    items = getattr(s, '_' + cid)(opts)['items']
                    row['plays_30d'] = sum(it.get('episodes_played', 1) for it in items
                        if it.get('played') and now - 30 * s.DAY <= (it.get('at') or 0) / 1000 <= now)
                elif cid in ('history', 'trakt'):
                    items = s._drop_read(cid)
                    unmapped = []
                    row['plays_30d'] = sum(now - 30*s.DAY <= (it.get('at') or 0)/1000 <= now for it in items)
                else:
                    raise ValueError('unsupported connector')
                row.update(reachable=True, auth_ok=True, unmapped=len(unmapped),
                           mapping_complete=not unmapped, ok=not unmapped)
                if show:
                    row['unmapped_names'] = unmapped
                if unmapped:
                    row['fix'] = 'Map the remaining users with skald setup --connect ' + cid
            except Exception as error:
                code = getattr(error, 'code', None)
                row['reachable'] |= code in (401, 403) or isinstance(error, s.TraktLoginRequired)
                row['fix'] = 'Check the address, credential and database mount; run skald setup --connect ' + cid.removesuffix('_api')
                row['error'] = 'authentication failed' if code in (401, 403) else 'connector probe failed'
                if cid in ('mdblist', 'stremio'):
                    label = 'Stremio' if cid == 'stremio' else 'MDBList'
                    row['error'] = {401: label + ' key rejected', 403: label + ' key rejected',
                                    429: label + ' rate limit; sync stopped, try later'}.get(code, 'connector probe failed')
            results.append(row)
    return {'ok': bool(results) and all(r['ok'] for r in results), 'connectors': results}


def text(result):
    for r in result['connectors']:
        yield (f"{r['name']}: reachable {'yes' if r['reachable'] else 'no'}; auth {'OK' if r['auth_ok'] else 'not OK'}; "
               f"{r['unmapped']} unmapped; {r['plays_30d']} plays in the last 30 days" +
               (f"; {r['skipped']} profiles skipped" if 'skipped' in r else ''))
        if 'likes_available' in r:
            yield 'Stremio likes '+('available' if r['likes_available'] else 'unavailable; library still syncs')
        for account in r.get('accounts', []):
            yield f"{r['name']}:{account['person']}: {account['plays_30d']} plays in the last 30 days; " + ('OK' if account['ok'] else account['error'])
        if r.get('unmapped_names'):
            yield 'Unmapped: ' + ', '.join(r['unmapped_names'])
        if r.get('error'):
            yield r['error']
        if r.get('fix'):
            yield r['fix']
