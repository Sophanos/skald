"""Read-only probes using the server's existing bounded clients."""
from contextlib import closing, redirect_stdout
import io
import sqlite3
import time
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
            try:
                if cid in ('trakt_api', 'mdblist', 'stremio'):
                    refs = cfg.get('accounts', {s.CONFIG['household']['trakt_member']: cfg.get('auth')})
                    for person, auth in refs.items():
                        _, _, stats = s.CONNECTOR_POLL[cid]({'id': cid + ':' + person, 'person': person,
                            'auth': auth, 'cursor': None}, now, readonly=True)
                        row['plays_30d'] += stats['plays_30d']
                    unmapped = []
                elif cid in ('jellyfin', 'plex', 'aiostreams_db'):
                    opts = cfg['options']
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
               f"{r['unmapped']} unmapped; {r['plays_30d']} plays in the last 30 days")
        if r.get('unmapped_names'):
            yield 'Unmapped: ' + ', '.join(r['unmapped_names'])
        if r.get('error'):
            yield r['error']
        if r.get('fix'):
            yield r['fix']
