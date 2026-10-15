"""Guided setup on the machine holding DATA; no snapshot is required."""
from contextlib import contextmanager, redirect_stdout
import argparse
import base64
import copy
import fcntl
import getpass
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import secrets
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from . import members, recipes, connectors, remote, guide
from .intake import write_private


@contextmanager
def server(data, *, report=None):
    spec = importlib.util.spec_from_file_location('skald_setup_server', Path(__file__).resolve().parents[1]/'server.py')
    s = importlib.util.module_from_spec(spec)
    s._CONFIG_READONLY = True
    with patch.dict(os.environ, {'DATA_DIR': str(data)}), redirect_stdout(io.StringIO()):
        spec.loader.exec_module(s)
        s.load_usage()
    try:
        from .aio_refresh import refresh
        changed = False
        def rows_changed():
            nonlocal changed
            changed = True
        s.aio_rows_changed = rows_changed
        yield s
        if changed:
            line = refresh(data)
            if line:
                (report or (lambda line: print(line, file=sys.stderr)))(line)
    finally:
        s._decide_queue.put(False)
        s._decide_queue.join()
        for name in ('_pool', '_outer', '_search_pool', '_nzb_pool', '_connector_pool'):
            getattr(s, name).shutdown(wait=True, cancel_futures=True)


def check_data(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = data.stat()
    if data.is_symlink() or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError('DATA needs your ownership and mode 0700; fix its owner and run chmod 700 DATA')
    fd, path = tempfile.mkstemp(prefix='.setup-check-', dir=data)
    os.close(fd)
    os.unlink(path)


def check_tmdb(data, *, transport=None):
    if not os.environ.get('TMDB_API'):
        raise ValueError('TMDB_API not set; put it in .env, then docker compose up -d (engine) or docker compose -f compose.stack.example.yaml up -d (skald); without Docker: export TMDB_API=...')
    with server(data) as s, patch('urllib.request.urlopen', transport or s.urllib.request.urlopen):
        try:
            result = s.tmdb('configuration')
            if not isinstance(result, dict) or 'images' not in result:
                raise ValueError()
        except Exception:
            raise ValueError('TMDB key could not be checked; check TMDB_API and the engine internet connection') from None
    return True


def publish(data, cfg, *, replace=False, on_publish=None, write_check=None):
    cfg = members.validate_config(cfg, data=str(data))
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if not members.publish_config(str(data), str(path), cfg, replace=replace, on_publish=on_publish,
                                  write_check=write_check):
        raise ValueError('household was created by another setup; run skald setup again')
    return cfg


def member_data(data, person, cfg, skip=()):
    if cfg.get('taste', {}).get('people', {}).get(person):
        return True
    if any(person in cfg['connectors'].get(cid, {}).get('accounts', {}) for cid in ('trakt_api', 'mdblist', 'stremio', 'pmdb')):
        return True
    for c in cfg['connectors'].values():
        opts = c.get('options', {})
        if opts.get('profile') == person or person in opts.get('profiles', {}).values() or person in opts.get('users', {}).values():
            return True
    def contains(value):
        if isinstance(value, dict):
            return person in value or any(contains(v) for v in value.values())
        if isinstance(value, list):
            return any(contains(v) for v in value)
        return value in (person, 'both')
    for path in data.iterdir():
        if path.name in ('skald.json', 'setup.json', 'legacy.json', 'addon.json', *skip) or path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith(('trakt-' + person + '.', 'mdblist-' + person + '.', 'stremio-' + person + '.', 'pmdb-' + person + '.')):
            return True
        if path.suffix not in ('.json', '.jsonl'):
            continue
        try:
            values = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.suffix == '.jsonl' else [json.loads(path.read_text())]
        except (ValueError, UnicodeError):
            raise ValueError('cannot check member data; repair ' + path.name + ' before changing members') from None
        if any(contains(v) for v in values):
            return True
    return False


def household(data, names, lang, timezone, country, *, existing=None, prepare=False, row_lang=None):
    data = Path(data)
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    existing = path.exists() if existing is None else existing
    if (data/'legacy.json').exists() and not existing:
        raise ValueError('this existing household is read-only in setup; run skald setup adopt to make it editable')
    if not existing:
        members.load_config(str(data), {}, readonly=True)  # Refuse persisted events without their household config.
    cfg = members.validate_config(json.loads(path.read_text()), data=str(data)) if existing else members.fresh_config(names)
    if names is not None:
        old = cfg['household']['members']
        for p in set(old) - set(names):
            if member_data(data, p, cfg):
                raise ValueError(p + ' has data; keep their name or use the household archive tools')
        hh = cfg['household']
        hh['members'] = names
        hh['labels'] = {**{p: hh['labels'].get(p, p.title()) for p in names}, 'pair': 'you both' if len(names) == 2 else 'everyone'}
        hh['member_rows'] = [p for p in hh['member_rows'] if p in names]
        hh['experiences'] = {p: v for p, v in hh['experiences'].items() if p in names}
        if hh['trakt_member'] not in names:
            hh['trakt_member'] = names[0] if names else ''
        cfg['taste']['people'] = {p: cfg['taste']['people'].get(p, {}) for p in names}
    cfg['region'].update({k: v for k, v in {'lang': lang, 'tz': timezone, 'country': country}.items() if v is not None})
    if row_lang == 'same':
        cfg['region'].pop('row_lang', None)
    elif row_lang is not None:
        cfg['region']['row_lang'] = row_lang
    return members.validate_config(cfg, data=str(data)) if prepare else publish(data, cfg, replace=existing)


def secret(stdin=False, label='Secret'):
    value = sys.stdin.readline().strip() if stdin else getpass.getpass(label + ': ').strip()
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError('missing secret; supply it through the private prompt or --secret-stdin')
    return value


def state(data):
    value = json.loads((data/'setup.json').read_text()) if (data/'setup.json').exists() else {}
    if not isinstance(value, dict):
        raise ValueError('invalid setup.json; restore a valid copy and rerun setup')
    return value


def aiostreams_logins(data):
    path = Path(data)/'credentials/aiostreams.json'
    return json.loads(path.read_text()) if path.exists() else {}


def aiostreams_request(url, *, method='GET', body=None, login=None, transport=None, manifest=None):
    headers = {'Accept': 'application/json'}
    if login:
        headers['Authorization'] = 'Basic ' + base64.b64encode(
            (login['uuid'] + ':' + login['password']).encode()).decode()
    raw = json.dumps(body).encode() if body is not None else None
    if raw is not None:
        headers['Content-Type'] = 'application/json'
    req = remote.urllib.request.Request(url, raw, headers, method=method)
    http_status = None
    try:
        try:
            response = (transport or remote.urllib.request.build_opener(remote.NoRedirect()).open)(req, timeout=20)
        except remote.urllib.error.HTTPError as err:
            http_status, response = err.code, err
        with response:
            raw = response.read(2 * 1024 * 1024)
        if len(raw) >= 2 * 1024 * 1024:
            raise ValueError()
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            raise ValueError()
    except (OSError, ValueError, http.client.HTTPException):
        raise ValueError('AIOStreams unavailable or invalid JSON; check --aio-url and try again') from None
    if http_status or reply.get('success') is False:
        error = reply.get('error', {})
        error = error if isinstance(error, dict) else {}
        code = error.get('code')
        if http_status == 429:
            hint = 'AIOStreams rate limit; try again later'
        elif code == 'USER_INVALID_DETAILS':
            hint = 'AIOStreams wrong login; check UUID and password'
        elif code == 'ADDON_PASSWORD_INVALID':
            hint = 'AIOStreams protected; check the instance access key and try again'
        elif code == 'USER_INVALID_CONFIG' and 'fetch manifest' in str(error.get('message', '')).lower():
            hint = 'AIOStreams cannot reach Skald; same Docker network? else use --base https address'
        else:
            hint = 'AIOStreams rejected setup; check its settings or use the manual steps'
        raise ValueError(hint)
    return reply


def catalog_prefix(s, manifest):
    own_catalogs = {cid for cid, _, _ in s.feed_rows(s.LANG)} | set(s.SHELF_ROWS) | {s.rid('search')}
    for catalog in manifest.get('catalogs', []):
        prefix, _, cid = catalog['id'].partition('.')
        if cid in own_catalogs:
            return prefix
    return None


def keep_catalog_places(s, config, manifest, person, *, first_added=False):
    modifications = config.get('catalogModifications') or []
    prefix = catalog_prefix(s, manifest)
    if not prefix or not modifications and not first_added:
        return None
    prefix += '.'
    with s.addon_scope(None if person == 'household' else person):
        rows = [(prefix + cid, typ) for cid, types, _ in s.feed_rows(s.LANG)
                for typ in (('tv',) if cid in s.MIXED and 'tv' in types else types)]
    home_rows = set(rows)
    rows += [(prefix + s.rid('search'), typ) for typ in ('movie', 'series')]
    original = modifications
    shelf_ids = {prefix + cid for cid in s.SHELF_ROWS}
    retired_ids = {prefix + cid for cid in s.CONFIG['household'].get('retired_rows', [])}
    modifications = [entry for entry in modifications
                     if entry['id'] in retired_ids or entry['id'] not in shelf_ids and not entry['id'].startswith(prefix+s.rid('lab_'))
                     or (entry['id'], entry['type']) in home_rows]
    existing = {(entry['id'], entry['type']): entry for entry in modifications
                if entry['id'].startswith(prefix)}
    groups, leading = [], []
    for key in rows:
        if key in existing:
            groups.append([*leading, existing[key]])
            leading = []
        else:
            entry = {'id': key[0], 'type': key[1], 'enabled': True}
            (groups[-1] if groups else leading).append(entry)
    # Keep retired non-shelf catalogs and their hidden settings.
    groups.extend([entry] for key, entry in existing.items() if key not in rows)
    if leading and groups:
        groups[0][:0] = leading
        leading = []
    slots = iter(groups)
    ordered = []
    for entry in modifications:
        if entry['id'].startswith(prefix):
            ordered.extend(next(slots))
        else:
            ordered.append(entry)
    ordered.extend(leading)
    promote = (first_added or not home_rows.intersection(existing) or
               any(i >= 40 and (entry['id'], entry['type']) in home_rows
                   for i, entry in enumerate(ordered)))
    if promote:
        ordered = [existing.get(key, {'id': key[0], 'type': key[1], 'enabled': True})
                   for key in rows if key in home_rows]
        ordered += [entry for entry in modifications if (entry['id'], entry['type']) not in home_rows]
        ordered += [{'id': key[0], 'type': key[1], 'enabled': True}
                    for key in rows if key not in home_rows and key not in existing]
        if ordered != original:
            config['catalogModifications'] = ordered
            return "move Skald's rows to the top (Odin shows the first 40)"
        return None
    if ordered != original:
        config['catalogModifications'] = ordered
        return "keep Skald's rows in place (" + str(len(rows)) + ' rows)'
    return None


def setup_aiostreams(s, args, saved, ask, lines, show, *, transport=None, secret_input=None, private=False):
    url = None
    def request(address, **kwargs):
        return aiostreams_request(address, transport=transport, manifest=url, **kwargs)
    try:
        internal = args.internal_base or saved.get('internal_base') or os.environ.get('SKALD_INTERNAL_BASE')
        base = remote.validate_base(args.base or internal or saved.get('base') or '')
        if (args.base or not internal) and remote.urllib.parse.urlsplit(base).scheme != 'https':
            raise ValueError('use --base https address so AIOStreams can reach Skald')
        urls = dict(zip(['household', *s.SCOPES], s.addon_urls(base)))
        selected = args.member or 'household'
        if selected not in (*urls, 'all'):
            raise ValueError('choose --member ' + '|'.join([*s.PEOPLE, 'together', 'all']))
        people = list(s.SCOPES) if selected == 'all' else [selected]
        if args.uuid and selected == 'all':
            raise ValueError('use --uuid with one member at a time')
        aio = remote.validate_base(ask(args.aio_url or saved.get('aio_url') or ('http://aiostreams:3000' if internal else None),
                                      'AIOStreams address', None, '--aio-url'))
        roots = {remote.validate_base(b) for b in (base, internal, saved.get('internal_base'),
                 saved.get('base'), os.environ.get('SKALD_INTERNAL_BASE')) if b}
        known_urls = {url for root in roots for url in s.addon_urls(root)}
        saved['aio_url'] = aio
        if internal:
            saved['internal_base'] = remote.validate_base(internal)
        if args.base:
            saved['base'] = base
        write_private(Path(s.DATA)/'setup.json', json.dumps(saved) + '\n')
        settings = request(aio + '/api/v1/status')['data']['settings']
        public = remote.validate_base(settings['baseUrl'])
        if not settings.get('jellyfin', {}).get('enabled'):
            lines.append('Jellyfin mode is off; set JELLYFIN_ENABLED=true in .env.aiostreams')
        if args.yes and settings.get('protected') and not args.secret_stdin and secret_input is None:
            raise ValueError('protected AIOStreams needs --secret-stdin for the instance access key')
        if args.yes and args.uuid and not args.password_stdin:
            raise ValueError('existing AIOStreams login needs --password-stdin')
        access = secret_input if secret_input is not None else secret(args.secret_stdin, 'AIOStreams instance access key') if settings.get('protected') else None
        logins = aiostreams_logins(s.DATA)
        for person in people:
            url = urls[person]
            login = {'uuid': args.uuid, 'password': secret(args.password_stdin, 'AIOStreams password'), 'member': person} if args.uuid else logins.get(person)
            new = login is None
            first_added = new
            skald = {'type': 'custom', 'instanceId': 'skd', 'enabled': True, 'options': {
                'name': 'Skald', 'manifestUrl': url, 'timeout': 7000, 'mediaTypes': [],
                'libraryAddon': False, 'resultPassthrough': False}}
            tmdb = {'type': 'tmdb-addon', 'instanceId': 'tmd', 'enabled': True, 'options': {
                'name': 'The Movie Database', 'timeout': 7500, 'resources': ['meta']}}
            if new:
                password = secret(True, 'AIOStreams password') if args.password_stdin else secrets.token_urlsafe(18)
                config = {'formatter': {'id': 'gdrive'}, 'sortCriteria': {'global': []}, 'presets': [skald, tmdb]}
            else:
                reply = request(aio + '/api/v1/user', login=login)['data']
                config = reply['userData']
                encrypted = reply['encryptedPassword']
                presets = config['presets']
                found = None
                for preset in presets:
                    options = preset.get('options', {})
                    address = options.get('manifestUrl', '')
                    if preset.get('type') == 'custom' and (address in known_urls or
                        options.get('name') == 'Skald' and any(address.startswith(root + '/') for root in roots)):
                        found = preset
                        break
                replacing_other = False
                if found is None and args.uuid:
                    found = next((preset for preset in presets if preset.get('type') == 'custom'
                                  and preset.get('options', {}).get('name') == 'Skald'), None)
                    replacing_other = found is not None
                changes = []
                def unused(preferred):
                    used = {p.get('instanceId') for p in presets}
                    if preferred not in used:
                        return preferred
                    return next(f'{i:03x}' for i in range(4096) if f'{i:03x}' not in used)
                kept = len(presets) - (found is not None)
                if found is None:
                    first_added = True
                    skald['instanceId'] = unused('skd')
                    presets.append(skald)
                    changes.append('add Skald')
                elif found['options']['manifestUrl'] != url:
                    found['options']['manifestUrl'] = url
                    changes.append('replace other Skald address' if replacing_other else 'update Skald address')
                manifest = request(aio + '/stremio/' + login['uuid'] + '/' + encrypted + '/manifest.json')
                details = any(isinstance(r, dict) and r.get('name') == 'meta' and
                              (not r.get('idPrefixes') or 'tmdb:' in r['idPrefixes']) for r in manifest.get('resources', []))
                if not details:
                    tmdb['instanceId'] = unused('tmd')
                    presets.append(tmdb)
                    changes.append('add The Movie Database (title details)')
                if access is not None and config.get('accessKey') != access:
                    changes.append('update instance access key')
                catalog_change = keep_catalog_places(s, config, manifest, person, first_added=first_added)
                if catalog_change:
                    changes.append(catalog_change)
                save_config = bool(changes)
                order_id = catalog_prefix(s, manifest)
                order_needed = not order_id or order_id not in config.get('upstreamCatalogOrder', [])
                if order_needed:
                    changes.append("keep Skald's row order")
                if changes:
                    lines.extend([person + ':', *changes, str(kept) + ' other addons kept'])
                    show()
                    if not args.yes:
                        if args.json:
                            raise ValueError('use --yes with --json to save changes')
                        if input('Save? [y/N] ').strip().lower() != 'y':
                            lines.append('Not saved')
                            continue
                else:
                    lines.append('AIOStreams already set up')
            if access is not None:
                config['accessKey'] = access
            if new:
                reply = request(aio + '/api/v1/user', method='POST', body={'config': config, 'password': password})['data']
                login = {'uuid': reply['uuid'], 'password': password, 'member': person}
                encrypted = reply['encryptedPassword']
            elif save_config:
                request(aio + '/api/v1/user', method='PUT', body={'config': config}, login=login)
            if new or args.uuid:
                logins[person] = login
                os.close(s._credentials_dir(create=True))
                write_private(Path(s.DATA)/'credentials/aiostreams.json', json.dumps(logins) + '\n')
            if new or save_config or order_needed:
                if new or save_config:
                    manifest = request(aio + '/stremio/' + login['uuid'] + '/' + encrypted + '/manifest.json')
                order_id = catalog_prefix(s, manifest)
                if not order_id:
                    raise ValueError('AIOStreams has no Skald catalogs yet; run skald setup aiostreams again when rows appear')
                config = request(aio + '/api/v1/user', login=login)['data']['userData']
                order = config.setdefault('upstreamCatalogOrder', [])
                order_changed = order_id not in order
                if order_changed:
                    order.append(order_id)
                catalog_change = keep_catalog_places(s, config, manifest, person, first_added=first_added)
                if catalog_change:
                    lines.append(catalog_change)
                if order_changed or catalog_change:
                    if access is not None:
                        config['accessKey'] = access
                    request(aio + '/api/v1/user', method='PUT', body={'config': config}, login=login)
            if new:
                if selected == 'all':
                    lines.append(person + ':')
                lines.extend(['Odin / Jellyfin app:  server  ' + public + '/jellyfin',
                              '                      user    ' + login['uuid']])
                if not args.json and not private:
                    stremio = public + '/stremio/' + login['uuid'] + '/' + encrypted + '/manifest.json'
                    lines.extend(['                      password ' + login['password'] + '   (shown once; also saved privately in DATA)',
                                  'Stremio (optional):   ' + stremio])
                    links = recipes.stremio_links(stremio)
                    if links:
                        lines.append('                      one click  ' + links['app'])
        result = {'ok': True, 'lines': lines}
    except Exception as error:
        hint = 'AIOStreams setup failed; check its address, access key and login, then retry with skald setup aiostreams' if private else str(error) if isinstance(error, ValueError) else 'AIOStreams response incomplete; check its settings'
        if args.json and 'urls' in locals():
            hint = hint.replace(s.addon_token(), '<addon-token>')
        lines.append(hint)
        manual_url = url or 'YOUR_PRINTED_MANIFEST_URL'
        if args.json:
            manual_url = manual_url.replace(s.addon_token(), '<addon-token>')
        if not private:
            lines.extend(recipes.aiostreams_manual(manual_url).splitlines())
        result = {'ok': False, 'lines': lines}
    if private and saved.get('household_member') or 'aiostreams_pending' in saved:
        saved['aiostreams_pending'] = not result['ok']
        if result['ok']:
            saved.pop('aiostreams_hint', None)
        write_private(Path(s.DATA)/'setup.json', json.dumps(saved) + '\n')
    show()
    return result


RESTART = 'restart Skald (docker compose restart engine)'


def startup_digest(config):
    config = copy.deepcopy(config)
    config.pop('metrics', None)  # Metrics preferences reload when rows are served.
    # Preference refresh makes this implicit default explicit in the loaded config.
    config['household'].setdefault('shelves', 2)
    people = config['taste'].setdefault('people', {})
    for person in config['household']['members']:
        people.setdefault(person, {})
    for traits in config.get('taste', {}).get('people', {}).values():
        traits.pop('prefs', None)  # Preferences reload when rows are served.
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def record_startup(data, config):
    try:
        write_private(Path(data)/'.skald-startup', startup_digest(config) + '\n')
    except OSError as error:
        print(f'could not record startup marker: {error}; setup --status may ask for a restart', file=sys.stderr)


def restart_required(data, config):
    marker = Path(data)/'.skald-startup'
    # CONFIG is loaded once; setup.json and credential contents are not cached there.
    return not marker.exists() or marker.read_text().strip() != startup_digest(config)


def status(data):
    saved = state(data)
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    config = members.validate_config(json.loads(path.read_text()), data=str(data)) if path.exists() else None
    people = members.active_people(config) if config else []
    legacy = (data/'legacy.json').exists() and config is None
    steps = {'checks': bool(saved.get('tmdb_checked') and os.environ.get('TMDB_API')),
             'household': bool(config) or legacy, 'address': bool(saved.get('base')),
             'watch': bool(saved.get('watch')), 'taste': not saved.get('connector_failed') and (bool(saved.get('taste')) or bool(config and config['connectors']))}
    if saved.get('household_member') or 'aiostreams_pending' in saved:
        steps['aiostreams'] = not saved.get('aiostreams_pending')
    notes = [saved['aiostreams_hint']] if saved.get('aiostreams_hint') else []
    if config and 'aiostreams_db' in config['connectors']:
        skipped = len(config['connectors']['aiostreams_db']['options'].get('archived', {}))
        notes.append('AIOStreams: ' + str(skipped) + ' profiles skipped')
    if legacy:
        config, _ = members.load_config(str(data), readonly=True)
        people = members.active_people(config)
        if os.environ.get('TMDB_API') and not saved.get('tmdb_checked'):
            steps.pop('checks')
            notes.append('TMDB key present (not checked)')
        for step in ('address', 'watch'):
            if not steps[step]:
                steps.pop(step)
                notes.append(step + ': not recorded; run skald setup to record')
        rated = {person: 0 for person in config['household']['members']}
        if any((data/name).is_file() for name in ('profile.json', 'history.json', 'trakt.json')):
            from .engine import Engine
            with Engine(data) as engine:
                rated.update(engine._model['nexp'])
        steps['taste'] = not saved.get('connector_failed') and (steps['taste'] or bool(config['connectors']) or any(rated.values()))
        notes.append('taste: ' + '; '.join(config['household']['labels'][p] + ': ' + str(n) + ' rated' for p, n in rated.items()))
    connections = {cid: {p: bool(config['connectors'].get(cid, {}).get('enabled') and
                                     p in config['connectors'].get(cid, {}).get('accounts', {}))
                         for p in people}
                   for cid in ('mdblist', 'stremio', 'pmdb')}
    missing = [k for k, done in steps.items() if not done]
    commands = {'checks': 'skald setup', 'household': 'skald setup', 'address': 'skald setup --base YOUR_ADDRESS',
                'watch': 'skald setup --watch stremio', 'taste': 'skald setup --connect files',
                'aiostreams': 'skald setup aiostreams'}
    next_command = 'skald connectors test' if saved.get('connector_failed') else commands[missing[0]] if missing else 'nothing left'
    if 'checks' in missing:
        missing[missing.index('checks')] = ('TMDB_API not set; put it in .env, then docker compose up -d (engine) or docker compose -f compose.stack.example.yaml up -d (skald); without Docker: export TMDB_API=...'
                                            if not os.environ.get('TMDB_API') else 'TMDB key not checked; run skald setup')
    logins = aiostreams_logins(data)
    if config:
        logins = {p: login for p, login in logins.items() if p in (*people, 'household')}
    if saved.get('watch') in ('aiostreams', 'jellyfin-app') and not logins and all(steps.get(k, False) for k in ('checks', 'household', 'address', 'watch')):
        next_command = 'skald setup aiostreams'
    if config and not legacy and restart_required(data, config):
        if next_command == 'nothing left':
            next_command = RESTART
        else:
            notes.append(RESTART)
    from .migrate import check
    from .ai import read_usage, status as ai_status, text as ai_text
    return {'release': members.VERSION, 'data_format': check(data)['format'], 'done': [k for k, done in steps.items() if done], 'missing': missing,
            'next': next_command, 'ok': not missing,
            'legacy_readonly': legacy, 'aiostreams_login': 'made for ' + ', '.join(logins) if logins else 'not made',
            'notes': notes, 'connections': connections,
            'lang': (config or members.CONFIG)['region']['lang'],
            'row_lang': (config or members.CONFIG)['region'].get('row_lang', (config or members.CONFIG)['region']['lang']),
            'ai_credit': ai_text(ai_status(read_usage(data), {}, 0, config or {}), (config or members.CONFIG)['region']['lang']),
            'prefs': [prefs_line(config, p) for p in people] if config else []}


def link(s, name, person, scopes, base):
    base = remote.validate_base(base)
    token = s.new_client(name, scopes, person)
    return {'token': token, 'lines': ['Keep this token private; it is shown once.',
        token, 'The engine needs API_PORT=8081 and /api/v1/ forwarded to port 8081; see docs/SETUP.md.', 'On your laptop, from the Skald checkout:', 'python3 -m skald connect ' + shlex.quote(base),
        'Paste the token at the private prompt.', 'export SKALD_MEMBERS=' + shlex.quote(','.join(s.PEOPLE)), 'python3 -m skald onboard --sources',
        'python3 -m skald push imports --remote',
        'After reviewing the preview: python3 -m skald push imports --remote --apply']}


def configure_connector(s, args, ask, lines, show, *, initial=False):
    staged = []
    previous = s.CONFIG
    committed = False
    def published():
        nonlocal committed
        committed = True
    def stage(value, label):
        directory = s._credentials_dir(create=True)
        os.close(directory)
        fd, name = tempfile.mkstemp(prefix=label + '-', suffix='.token', dir=Path(s.DATA)/'credentials')
        os.close(fd)
        path = Path(name)
        staged.append(path)
        write_private(path, value)
        return 'file:' + path.name
    try:
        result = _configure_connector(s, args, ask, lines, show, stage, published, initial=initial)
        committed = result is None or result['ok']
        return result
    finally:
        if not committed:
            s.CONFIG = previous
            for path in staged:
                path.unlink(missing_ok=True)


def _configure_connector(s, args, ask, lines, show, stage, published, *, initial=False):
    cid = 'trakt_api' if args.connect == 'trakt' else args.connect
    if cid == 'files':
        lines.extend(recipes.FILES.format(person=s.PEOPLE[0]).splitlines())
        return None
    cfg = copy.deepcopy(s.CONFIG)
    previous = cfg['connectors'].get(cid, {})
    old_options = previous.get('options', {})
    if cid == 'aiostreams_db':
        lines.append('Mount the AIOStreams SQLite database read-only into the engine, including its WAL files.')
        opts = {'path': ask(args.source_path, 'AIOStreams database path inside the engine', old_options.get('path'), '--source-path')}
        archived = dict(old_options.get('archived', {}))
        opts['profiles'] = dict(old_options.get('profiles', {}))
        users = connectors.source_users(s, cid, opts)
        mapping = {p.lower(): who for p, who in parse_mapping(args.map, cfg, cid).items()}
        for uid in users:
            if uid not in mapping and uid not in opts['profiles'] and uid not in archived:
                default = next((v['member'] for v in aiostreams_logins(s.DATA).values() if v['uuid'].startswith(uid) or uid.startswith(v['uuid'][:8])), None)
                who = ask(None, 'Member for profile ' + uid + ' (or together or skip)', 'together' if default == 'household' else default, '--map')
                mapping.update(parse_mapping([uid + '=' + who], cfg, cid))
        for p, who in mapping.items():
            if who == 'skip':
                opts['profiles'].pop(p, None)
                archived[p] = 'household'
            else:
                archived.pop(p, None)
                opts['profiles'][p] = 'household' if who == 'together' else who
        if not opts['profiles']:
            raise ValueError('map at least one profile to a member or together; not all profiles can be skipped')
        if archived:
            opts['archived'] = archived
        c = {'kind': 'player', 'enabled': True, 'auth': None, 'options': opts}
    else:
        instructions = {'pmdb': 'Create a personal PublicMetaDB key at https://publicmetadb.com: Settings → API → Create. Paste it privately.',
            'stremio': 'Paste your Stremio auth key privately, or leave it blank to sign in with email and password.',
            'mdblist': 'Get a free API key at https://mdblist.com/preferences. Paste it privately.',
            'trakt_api': 'Create a Trakt API app at https://trakt.tv/oauth/applications (may require VIP). Use its client id and an access token from your authorized account.',
            'jellyfin': 'Create an API key in Jellyfin: Dashboard → Advanced → API Keys → Add.',
            'plex': 'Get a Plex token: open a media item in Plex Web → Get Info → View XML; copy X-Plex-Token privately.'}
        lines.append(instructions[cid])
        show()
        if cid in ('trakt_api', 'mdblist', 'stremio', 'pmdb'):
            person = ask(args.person, 'Source member', cfg['household']['trakt_member'], '--person')
            if person not in cfg['household']['members']:
                raise ValueError('choose a household member with --person')
        previous_auth = previous.get('accounts', {}).get(person) if cid in ('trakt_api', 'mdblist', 'stremio', 'pmdb') and 'accounts' in previous else previous.get('auth')
        if cid == 'trakt_api' and person != cfg['household']['trakt_member'] and 'accounts' not in previous:
            previous_auth = None
        if cid in ('mdblist', 'stremio', 'pmdb') and args.auth_env:
            raise ValueError('Keys use private files; use --key-stdin')
        auth = args.auth_env
        if auth:
            auth = 'env:' + auth
        elif previous_auth and not args.secret_stdin:
            auth = previous_auth
        else:
            if args.yes and not args.secret_stdin:
                raise ValueError('secret needed; use --secret-stdin or --auth-env VARIABLE')
            key = secret(args.secret_stdin, cid + ' secret') if cid != 'stremio' or args.secret_stdin else getpass.getpass('Stremio auth key (blank for email login): ').strip()
            if cid == 'stremio' and not key:
                key = s._stremio_login(secret(False, 'Stremio email'), secret(False, 'Stremio password'))
            auth = stage(key, cid.removesuffix('_api') + '-' + person if cid in ('trakt_api', 'mdblist', 'stremio', 'pmdb') else cid)
        if cid in ('mdblist', 'stremio', 'pmdb'):
            accounts = dict(previous.get('accounts', {}))
            accounts[person] = auth
            c = {'kind': 'api', 'enabled': True, 'auth': None, 'accounts': accounts, 'options': {}}
        elif cid == 'trakt_api':
            if args.client_id_env:
                client = 'env:' + args.client_id_env
            elif previous.get('options', {}).get('client_id') and not args.secret_stdin:
                client = previous['options']['client_id']
            else:
                if args.yes and not args.secret_stdin:
                    raise ValueError('client id needed; use --client-id-env VARIABLE')
                client = stage(secret(args.secret_stdin, 'Trakt client id'), 'trakt-client')
            accounts = dict(previous.get('accounts', {}))
            if previous.get('auth') and person != cfg['household']['trakt_member']:
                accounts[cfg['household']['trakt_member']] = previous['auth']
            c = {'kind': 'api', 'enabled': True, 'auth': auth, 'options': {**old_options, 'client_id': client}}
            if accounts:
                accounts[person] = auth
                for member, reference in accounts.items():
                    if reference.startswith('env:'):
                        accounts[member] = stage(s._resolve_auth(reference), 'trakt-' + member)
                c.update(auth=None, accounts=accounts)
            else:
                cfg['household']['trakt_member'] = person
        else:
            opts = {'url': remote.validate_base(ask(args.source_url, 'Source server address', old_options.get('url'), '--source-url'))}
            if 'section' in old_options:
                opts['section'] = old_options['section']
            users = connectors.source_users(s, cid, opts, auth)
            mapping = parse_mapping(args.map, cfg, cid)
            if cid == 'jellyfin':
                prior_users = old_options.get('users', {old_options.get('user_id'): old_options.get('profile', 'household')})
                mapping = {**{uid: person for uid, person in prior_users.items() if uid in users}, **mapping}
                if args.user_id:
                    if args.user_id not in users:
                        raise ValueError('unknown Jellyfin user; choose a listed --user-id')
                    mapping[args.user_id] = args.person or mapping.get(args.user_id) or ask(None, 'Member for selected user (or together)', None, '--person')
                for uid, label in users.items():
                    if uid not in mapping:
                        mapping[uid] = ask(None, 'Member for Jellyfin user ' + label + ' (' + uid + '), or together', None, '--map')
                if set(mapping) - users.keys():
                    raise ValueError('unknown Jellyfin user in --map; use ids listed by Jellyfin')
                opts['users'] = {uid: 'household' if person == 'together' else person for uid, person in mapping.items()}
            else:
                uid = 'account'
                lines.append('Plex account found: ' + users[uid])
                profile = mapping.get(uid) or ask(args.person, 'Member for this login (or together)', old_options.get('profile'), '--person')
                opts['profile'] = 'household' if profile == 'together' else profile
            c = {'kind': 'player', 'enabled': True, 'auth': auth, 'options': opts}
    cfg['connectors'][cid] = c
    if cid in ('jellyfin', 'plex', 'aiostreams_db'):
        cfg['history']['source'] = cid
    s.CONFIG = members.validate_config(cfg, data=s.DATA)
    result = connectors.test(s, cid)
    lines.extend(connectors.text(result))
    if result['ok']:
        history = None
        if cid == 'aiostreams_db' and old_options and any(old_options.get(k) != opts[k] for k in ('path', 'profiles')):
            history = s._aiostreams_db(opts)
        s.CONFIG = publish(Path(s.DATA), cfg, replace=not initial, on_publish=published)
        if history is not None:
            # Changed history inputs invalidate the hashes checked by refresh_profiles().
            s._connector_write('history', history)
    return result


def parse_mapping(values, cfg, cid=None):
    result = {}
    for value in values:
        uid, sep, person = value.rpartition('=')
        if cid == 'aiostreams_db' and person == 'skip' and 'skip' in cfg['household']['members']:
            raise ValueError('cannot use skip with a household member named skip; rename that member first')
        if not sep or not uid or person not in (*cfg['household']['members'], 'together') and not (cid == 'aiostreams_db' and person == 'skip'):
            raise ValueError('use --map SOURCE_ID=MEMBER, or SOURCE_ID=together, or SOURCE_ID=skip for aiostreams-db')
        result[uid] = person
    return result


def prefs_line(cfg, person):
    prefs = cfg['taste'].get('people', {}).get(person, {}).get('prefs', {})
    years = '-'.join(str(y) if y is not None else '' for y in prefs.get('years', [None, None]))
    excluded = prefs.get('excluded_genres', {})
    extra = ['hide ' + ', '.join(prefs['hide'])] if prefs.get('hide') else []
    if 'kids_age' in prefs:
        extra.append('child, age ' + str(prefs['kids_age']))
    focus = prefs.get('focus', {})
    for label, values in [('movie genres', focus.get('genres', {}).get('movie', [])),
                          ('series genres', focus.get('genres', {}).get('series', [])),
                          ('languages', focus.get('languages', [])), ('countries', focus.get('countries', []))]:
        if values:
            extra.append('focus ' + label + ' ' + ', '.join(values))
    return ('Prefs ' + person + ': ' + prefs.get('discovery', 'balanced') + '; years ' + years +
            '; exclude movies ' + (', '.join(excluded.get('movie', [])) or 'none') +
            '; exclude series ' + (', '.join(excluded.get('series', [])) or 'none') + ''.join('; ' + x for x in extra))


def select_rows(cfg, keys):
    cfg['household'].pop('rows_from_default', None)
    if keys == 'all':
        cfg['household'].pop('rows', None)
    else:
        cfg['household']['rows'] = [k.strip() for k in (keys or '').split(',')]


def wait_household_ready(data, cfg, base, *, timeout=30, transport=None):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not restart_required(data, cfg):
                req = remote.urllib.request.Request(base + '/health')
                with (transport or remote.urllib.request.build_opener(remote.NoRedirect()).open)(req, timeout=2) as response:
                    if json.loads(response.read(4096)).get('ok') is True:
                        return
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            raise ValueError('Engine not ready with the complete config; check the engine, then run skald setup aiostreams')
        time.sleep(0.25)


def setup_household(data, args):
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if path.exists() or (data/'legacy.json').exists() or (data/'.skald-household').exists():
        raise ValueError('household already configured; use the existing setup commands: skald setup rows, skald setup --connect, or skald setup aiostreams')
    check_data(data)
    from .migrate import ensure
    ensure(data)
    saved = state(data)
    if not args.member or args.member in ('all', 'household', 'together'):
        raise ValueError('supply one household member with --member')
    cfg = household(data, [args.member], args.lang or 'en-US', args.timezone or 'UTC', args.country or 'United States', prepare=True, row_lang=args.row_lang)
    if args.rows is not None:
        select_rows(cfg, args.rows)
        cfg = members.validate_config(cfg, data=str(data))
    source = args.source_path or saved.get('source_path')
    if not source:
        raise ValueError('supply the mounted database with --source-path')
    internal = args.internal_base or saved.get('internal_base') or os.environ.get('SKALD_INTERNAL_BASE')
    base = remote.validate_base(internal or args.base or saved.get('base') or '')
    aio = remote.validate_base(args.aio_url or saved.get('aio_url') or ('http://aiostreams:3000' if internal else ''))
    private_uuid = secret(True, 'AIOStreams full UUID') if args.uuid_stdin else None
    login = aiostreams_logins(data).get(args.member)
    uid = connectors.aiostreams_login(source, args.aio_login, private_uuid or (login or {}).get('uuid'))
    access = connectors.aiostreams_access_key(source)
    if login is None or login['uuid'] != uid:
        login = {'uuid': uid, 'password': secret(args.password_stdin, 'AIOStreams password'), 'member': args.member}
    check_tmdb(data)
    args = copy.copy(args)
    args.yes = True
    args.uuid = None
    args.connect = 'aiostreams_db'
    args.source_path = source
    args.internal_base = internal
    args.aio_url = aio
    lines = ['Checks: DATA private; TMDB valid.']
    def ask(value, label, default, flag):
        if value is not None:
            return value
        if default is not None:
            return default
        raise ValueError('missing ' + flag)
    with server(data) as s:
        users = connectors.source_users(s, 'aiostreams_db', {'path': source})
        args.map = [p + '=skip' for p in users if p != uid[:8]] + [uid[:8] + '=' + args.member]
        s.CONFIG = cfg
        result = configure_connector(s, args, ask, lines, lambda: None, initial=True)
        if not result['ok']:
            return {'ok': False, 'lines': lines}
        cfg = s.CONFIG
        os.close(s._credentials_dir(create=True))
        logins = aiostreams_logins(data)
        logins[args.member] = login
        write_private(data/'credentials/aiostreams.json', json.dumps(logins) + '\n')
    saved.update(tmdb_checked=True, taste='aiostreams_db', watch='jellyfin-app', aio_url=aio, source_path=source,
                 household_member=args.member, aiostreams_pending=True)
    saved['base'] = remote.validate_base(args.base or base)
    if internal:
        saved['internal_base'] = base
    write_private(data/'setup.json', json.dumps(saved) + '\n')
    try:
        wait_household_ready(data, cfg, base)
    except ValueError:
        return {'ok': False, 'lines': lines + ['Engine not ready; check the engine, then retry with skald setup aiostreams.']}
    with server(data) as s:
        result = setup_aiostreams(s, args, saved, ask, lines, lambda: None, secret_input=access, private=True)
    if result['ok']:
        lines.append('Household ready. Use your existing player login.')
    return result


def run(args):
    data = Path(os.environ.get('DATA_DIR', '/data'))
    if args.setup_action == 'alerts':
        from .health import setup_alerts
        return setup_alerts(data, args)
    if args.setup_action == 'household':
        return setup_household(data, args)
    if args.status:
        return status(data)
    if args.disconnect:
        if args.disconnect not in ('mdblist', 'stremio', 'pmdb'):
            raise ValueError('--disconnect supports mdblist, stremio or pmdb')
        path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
        cfg = members.validate_config(json.loads(path.read_text()), data=str(data))
        person = args.person
        if person not in cfg['household']['members']:
            raise ValueError('choose a household member with --person')
        connector = cfg['connectors'].get(args.disconnect, {})
        reference = connector.get('accounts', {}).pop(person, None)
        if not connector.get('accounts'):
            cfg['connectors'].pop(args.disconnect, None)
        publish(data, cfg, replace=True)
        if reference:
            _, name = members._auth_reference(reference, str(data))
            (data/'credentials'/name).unlink(missing_ok=True)
        return {'ok': True, 'lines': [{'mdblist': 'MDBList', 'stremio': 'Stremio', 'pmdb': 'PublicMetaDB'}[args.disconnect] + ' ' + person + ': disconnected']}
    config_path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if args.setup_action == 'adopt':
        if config_path.exists():
            raise ValueError(str(config_path) + ' already exists; household cannot be adopted')
        legacy = data/'legacy.json'
        if not legacy.is_file():
            raise ValueError('no legacy.json household to adopt')
        cfg = json.loads(legacy.read_text())
        members.validate_config(cfg, data=str(data))
        if not args.yes:
            if args.json:
                raise ValueError('use --yes with --json to adopt')
            if input('Adopt this household? [y/N] ').strip().lower() != 'y':
                raise ValueError('household adoption cancelled')
        check_data(data)
        stamp = data/'.skald-household'
        stamped = os.path.lexists(stamp)
        try:
            if not members.publish_config(str(data), str(config_path), cfg):
                raise ValueError(str(config_path) + ' already exists; household cannot be adopted')
        except (OSError, members.ConfigError):
            if not stamped and not config_path.exists():
                stamp.unlink(missing_ok=True)
            raise
        return {'lines': ['Adopted household into ' + str(config_path) + '.',
                          'Now use skald setup link, skald setup --connect, and imports.',
                          'Rollback: remove ' + str(config_path) + ' and DATA/.skald-household; legacy.json still holds the old config.']}
    if (data/'legacy.json').exists() and not config_path.exists():
        raise ValueError('this existing household is read-only in setup; run skald setup adopt to make it editable')
    if args.setup_action == 'rows':
        if not config_path.exists():
            raise ValueError('set up a household before choosing rows')
        check_data(data)
        from . import commands, shelves
        with server(data) as s:
            with commands._LOCK, shelves.LOCK, os.fdopen(os.open(data/'shelves.lock', os.O_RDWR | os.O_CREAT, 0o600), 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                cfg = commands.config(s)
                before = cfg['household'].get('rows')
                select_rows(cfg, args.row_keys)
                cfg = publish(data, cfg, replace=True)
            if before != cfg['household'].get('rows'):
                s.aio_rows_changed()
        return {'ok': True, 'lines': ['Rows: ' + (', '.join(cfg['household']['rows']) if 'rows' in cfg['household'] else 'all default rows') + '.']}
    if args.setup_action == 'prefs':
        if not config_path.exists():
            raise ValueError('set up a household before changing preferences')
        cfg = members.validate_config(json.loads(config_path.read_text()), data=str(data))
        if args.person not in cfg['household']['members']:
            raise ValueError('choose a household member with --person')
        values = (args.discovery, args.years, args.exclude_movie_genres, args.exclude_series_genres,
                  args.kids_age, args.hide, args.show, args.focus_movie_genres, args.focus_series_genres, args.focus_languages, args.focus_countries)
        if args.clear and any(v is not None for v in values):
            raise ValueError('use --clear without preference flags')
        if not args.clear and all(v is None for v in values):
            raise ValueError('supply a preference flag or --clear')
        traits = cfg['taste'].setdefault('people', {}).setdefault(args.person, {})
        if args.clear:
            traits.pop('prefs', None)
        else:
            prefs = traits.setdefault('prefs', {})
            if args.kids_age is not None:
                if args.kids_age == 'off':
                    prefs.pop('kids_age', None)
                elif args.kids_age.isdigit() and 3 <= int(args.kids_age) <= 17:
                    prefs['kids_age'] = int(args.kids_age)
                else:
                    raise ValueError('--kids-age needs an age from 3 to 17, or off')
            for action, raw in (('hide', args.hide), ('show', args.show)):
                if raw is not None:
                    words = [v.strip() for v in raw.split(',')]
                    if any(v not in members.HIDE_CHOICES for v in words):
                        raise ValueError('--' + action + ' accepts: ' + ', '.join(members.HIDE_CHOICES))
                    hides = prefs.setdefault('hide', [])
                    prefs['hide'] = list(dict.fromkeys(hides + words)) if action == 'hide' else [h for h in hides if h not in words]
            for field, raw in (('movie', args.focus_movie_genres), ('series', args.focus_series_genres),
                               ('languages', args.focus_languages), ('countries', args.focus_countries)):
                if raw is not None:
                    target = prefs.setdefault('focus', {})
                    if field in ('movie', 'series'):
                        target = target.setdefault('genres', {})
                    target[field] = [v.strip() for v in raw.split(',')] if raw.strip() else []
            if args.discovery is not None:
                prefs['discovery'] = args.discovery
            if args.years is not None:
                if not re.fullmatch(r'(?:[0-9]{4})?-(?:[0-9]{4})?', args.years):
                    raise ValueError('--years needs FROM-TO, FROM- or -TO (four-digit years)')
                prefs['years'] = [int(y) if y else None for y in args.years.split('-')]
            for kind, raw in (('movie', args.exclude_movie_genres), ('series', args.exclude_series_genres)):
                if raw is not None:
                    prefs.setdefault('excluded_genres', {})[kind] = [g.strip() for g in raw.split(',')] if raw.strip() else []
        check_data(data)
        from . import commands
        lines = [prefs_line(cfg, args.person)]
        with server(data, report=lines.append) as s:
            commands.apply(s, {'people': [args.person], 'name': 'Setup prefs for '+args.person}, cfg=cfg)
        return {'ok': True, 'lines': lines}
    check_data(data)
    from .migrate import ensure
    ensure(data)
    saved = state(data)
    lines = []
    shown = 0
    def show():
        nonlocal shown
        if not args.json and len(lines) > shown:
            print('\n'.join(lines[shown:]))
            shown = len(lines)
    if args.setup_action == 'link':
        if args.json:
            raise ValueError('run without --json; the token is shown once')
        with server(data) as s:
            s.require_config(s.CONFIG_SOURCE, s.CONFIG_ERROR)
            return link(s, args.name, args.person, args.scopes.split(','), args.base or saved.get('base') or '')
    def ask(value, label, default, flag):
        show()
        if value is not None:
            return value
        if args.yes:
            if default is not None:
                return default
            raise ValueError('missing ' + flag + '; supply it and run skald setup again')
        if args.json:
            raise ValueError('use --yes and flags with --json')
        answer = input(label + (f' [{default}]' if default else '') + ': ').strip()
        return answer or default
    if args.setup_action == 'aiostreams':
        with server(data) as s:
            s.require_config(s.CONFIG_SOURCE, s.CONFIG_ERROR)
            if saved.get('household_member'):
                args = copy.copy(args)
                args.member = args.member or saved['household_member']
                args.yes = True
                cfg = members.validate_config(json.loads(config_path.read_text()), data=str(data))
                wait_household_ready(data, cfg, args.internal_base or saved.get('internal_base') or args.base or saved.get('base'))
                access = connectors.aiostreams_access_key(saved['source_path'])
                return setup_aiostreams(s, args, saved, ask, lines, show, secret_input=access, private=True)
            return setup_aiostreams(s, args, saved, ask, lines, show)
    if args.connect and config_path.exists():
        if args.connect == 'aiostreams-db':
            args.connect = 'aiostreams_db'
        if args.connect not in ('trakt', 'mdblist', 'stremio', 'pmdb', 'jellyfin', 'plex', 'aiostreams_db', 'files'):
            raise ValueError('choose a supported --connect source')
        with server(data) as s:
            s.require_config(s.CONFIG_SOURCE, s.CONFIG_ERROR)
            probe = configure_connector(s, args, ask, lines, show)
            saved['taste'] = args.connect
            if probe and not probe['ok']:
                saved.pop('taste', None)
                saved['connector_failed'] = True
            else:
                saved.pop('connector_failed', None)
                if restart_required(data, s.CONFIG):
                    lines.append(RESTART + ' now so it starts reading.')
        write_private(data/'setup.json', json.dumps(saved) + '\n')
        result = status(data)
        result.update(ok=probe is None or probe['ok'], addon_urls_shown=False,
                      lines=lines + ['Next: ' + result['next']])
        lines[:] = result['lines']
        show()
        return result
    check_tmdb(data)
    saved['tmdb_checked'] = True
    write_private(data/'setup.json', json.dumps(saved) + '\n')
    lines.extend(['Checks: DATA writable and private; TMDB key valid.',
        'OpenRouter adds AI picks, natural-language search, mood shelves and weekly picks; rows work without it.' +
        (' Key present.' if os.environ.get('OPENROUTER_API_KEY') else ' Optional key absent.')])
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if (data/'legacy.json').exists() and not path.exists():
        raise ValueError('this existing household is read-only in setup; run skald setup adopt to make it editable')
    existing = path.exists()
    names = [name.strip().lower() for name in args.members.split(',')] if args.members is not None else None
    if not existing:
        raw = ask(args.members, 'Household names, 1–6, comma-separated (lowercase)', None, '--members')
        names = [n.strip().lower() for n in raw.split(',')]
    lang = args.lang if existing else ask(args.lang, 'Language', 'en-US', '--lang')
    tz = args.timezone if existing else ask(args.timezone, 'Timezone', 'UTC', '--timezone')
    country = args.country if existing else ask(args.country, 'Country', 'United States', '--country')
    cfg = household(data, names, lang, tz, country, existing=existing, row_lang=args.row_lang) if not existing or names is not None or args.row_lang is not None or any((lang, tz, country)) else members.validate_config(json.loads(path.read_text()), data=str(data))
    if not args.yes and not args.json and not saved.get('children_asked'):
        if ask(None, 'Any children watching?', 'no', '').lower() in ('y', 'yes'):
            person = ask(None, 'Which member is the child?', None, '--person')
            if person not in cfg['household']['members']:
                raise ValueError('choose a household member')
            age = ask(None, 'Child age (3..17)', None, '--kids-age')
            if not age.isdigit() or not 3 <= int(age) <= 17:
                raise ValueError('child age must be from 3 to 17')
            cfg['taste'].setdefault('people', {}).setdefault(person, {}).setdefault('prefs', {})['kids_age'] = int(age)
            cfg = publish(data, cfg, replace=True)
        saved['children_asked'] = True
        write_private(data/'setup.json', json.dumps(saved) + '\n')
    lines.append('Household: ' + ', '.join(members.active_people(cfg)) + '; ' + cfg['region']['lang'] + '; ' + cfg['region']['tz'] + '; ' + cfg['region']['country'] + '.')
    if args.row_lang is not None:
        lines.append('Row names and card labels: ' + cfg['region'].get('row_lang', cfg['region']['lang']) + '; ' + RESTART + ' to apply.')
    internal_base = args.internal_base or saved.get('internal_base') or os.environ.get('SKALD_INTERNAL_BASE')
    if internal_base:
        internal_base = remote.validate_base(internal_base)
        saved['internal_base'] = internal_base
    watch_choice = args.watch or saved.get('watch') or ('jellyfin-app' if internal_base else None)
    base = remote.validate_base(ask(args.base or saved.get('base') or (internal_base if watch_choice in ('aiostreams', 'jellyfin-app') else None), 'Skald address reachable from your player', None, '--base'))
    saved['base'] = base
    write_private(data/'setup.json', json.dumps(saved) + '\n')
    with server(data) as s:
        token = s.addon_token()
        urls = [url.replace(token, '<addon-token>') if args.json else url for url in s.addon_urls(base)]
        lines.append('Household: ' + urls[0])
        lines.extend(('Together' if p == 'together' else cfg['household']['labels'][p]) + ': ' + url for p, url in zip(s.SCOPES, urls[1:]))
        if args.check:
            try:
                req = s.urllib.request.Request(base + '/' + cfg['region']['lang'] + '/manifest.json')
                with s.urllib.request.build_opener(remote.NoRedirect()).open(req, timeout=10) as response:
                    manifest = json.load(response)
                if not isinstance(manifest, dict) or 'catalogs' not in manifest:
                    raise ValueError()
                lines.append('Address: reachable.')
            except Exception:
                lines.append('Address: not reachable; check the proxy, address and private addon token path.')
                saved['address_reachable'] = False
            else:
                saved['address_reachable'] = True
        watch = ask(watch_choice, 'Where you watch: stremio, aiostreams, jellyfin-app', 'stremio', '--watch')
        if watch not in recipes.RECIPES:
            raise ValueError('choose --watch stremio, aiostreams or jellyfin-app')
        saved['watch'] = watch
        write_private(data/'setup.json', json.dumps(saved) + '\n')
        lines.extend(recipes.recipe(watch, urls[0], cfg['region']['lang'], s.addon_urls(internal_base)[0].replace(token, '<addon-token>') if internal_base and args.json else s.addon_urls(internal_base)[0] if internal_base else None).splitlines())
        if watch == 'stremio' and recipes.stremio_links(base):
            lines.append('Install with one click: open your link on the device that has Stremio.')
            lines.extend(('Household' if p == 'household' else 'Together' if p == 'together' else cfg['household']['labels'][p])
                         + ': ' + recipes.stremio_links(url)['app'] for p, url in zip(['household', *s.SCOPES], urls))
            lines.append('QR code for a phone: skald addon-url --stremio --qr --person NAME --base ' + base)
        if args.connect or not saved.get('taste') and not cfg['connectors']:
            args.connect = ask(args.connect, 'Taste source: trakt, mdblist, stremio, pmdb, jellyfin, plex, aiostreams-db, files', 'files', '--connect')
            if args.connect == 'aiostreams-db':
                args.connect = 'aiostreams_db'
            if args.connect not in ('trakt', 'mdblist', 'stremio', 'pmdb', 'jellyfin', 'plex', 'aiostreams_db', 'files'):
                raise ValueError('choose a supported --connect source')
            result = configure_connector(s, args, ask, lines, show)
            saved['taste'] = args.connect
            if result and not result['ok']:
                saved.pop('taste', None)
                saved['connector_failed'] = True
            else:
                saved.pop('connector_failed', None)
                if (data/'.skald-startup').exists() and restart_required(data, s.CONFIG):
                    lines.append(RESTART + ' now so it starts reading.')
    write_private(data/'setup.json', json.dumps(saved) + '\n')
    result = status(data)
    with tempfile.TemporaryDirectory(prefix='skald-setup-doctor-') as temp:
        result['doctor'] = guide.doctor(SimpleNamespace(snapshot=str(data), live=False, remote=False, fix=False), Path(temp))
    errors = sum(f['severity'] == 'error' for f in result['doctor']['findings'])
    notes = sum(f['severity'] == 'warning' for f in result['doctor']['findings'])
    result['addon_urls_shown'] = not args.json
    result['lines'] = lines + ['Laptop link (optional): skald setup link --name laptop',
        f'Doctor: {errors} errors; {notes} notes. Household valid; DATA private; TMDB valid.' + (' Connector needs attention.' if saved.get('connector_failed') else ''),
        'Next: ' + result['next']]
    lines[:] = result['lines']
    show()
    result['ok'] &= not errors and not saved.get('connector_failed') and (not args.check or saved.get('address_reachable', False))
    return result


def add_parser(commands):
    p = commands.add_parser('setup', help='set up a household on the machine holding DATA')
    p.add_argument('setup_action', nargs='?', choices=['household', 'link', 'aiostreams', 'adopt', 'prefs', 'rows', 'alerts'], help='create a household, link a laptop, configure AIOStreams, preferences, rows or health alerts')
    for flag, help_text in (
            ('telegram', 'with alerts: configure Telegram privately'),
            ('token-stdin', 'with alerts: read the Telegram token from stdin'),
            ('webhook', 'with alerts: configure a JSON POST webhook'),
            ('url-stdin', 'with alerts: read the webhook URL from stdin'),
            ('test', 'with alerts: send one test message per configured channel'),
            ('enable-health', 'with alerts: enable health checks after server restart'),
            ('disable-health', 'with alerts: disable health checks after server restart'),
            ('repair', 'with alerts: opt into bounded self-repair'),
            ('digest', 'with alerts: opt into a daily health digest')):
        p.add_argument('--' + flag, action=argparse.BooleanOptionalAction if flag in ('repair', 'digest') else 'store_true',
                       default=None if flag in ('repair', 'digest') else False, help=help_text)
    p.add_argument('--chat-id', help='with alerts: private Telegram chat id; never echoed')
    p.add_argument('--webhook-style', choices=['ntfy', 'gotify', 'discord'], default='ntfy', help='with alerts: webhook JSON format')
    p.add_argument('--quiet-hours', help='with alerts: silent local hours, e.g. 22-07; uses --timezone or UTC')
    p.add_argument('--aio-login', help='with household: AIOStreams login prefix to map to --member')
    p.add_argument('--uuid-stdin', action='store_true', help='with household: read a full login UUID privately before the password, for a login without history')
    p.add_argument('--rows', help='with household: comma-separated home row keys; all uses default rows')
    p.add_argument('row_keys', nargs='?', help='with rows: comma-separated keys (' + ', '.join(members.ROW_KEYS) + '), or all for default rows')
    helps = {
        'aio-url': 'address where Skald reaches AIOStreams',
        'uuid': 'existing AIOStreams login UUID',
        'member': 'AIOStreams login for NAME, together or all; default: household',
        'password-stdin': 'read the AIOStreams login password from stdin',
        'yes': 'use supplied values and defaults without prompting',
        'json': 'print JSON with addon tokens, AIOStreams passwords and Stremio login URLs hidden',
        'status': 'show completed steps, missing settings and one next command',
        'check': 'fetch <base>/<lang>/manifest.json and say if your apps can reach it',
        'secret-stdin': 'read the secret from stdin instead of a prompt',
        'members': 'comma-separated household member names, e.g. alex,sam',
        'lang': 'language for titles, e.g. en-US',
        'row-lang': 'language for row names and card labels, e.g. en-US; same follows --lang',
        'timezone': 'timezone for dates, e.g. Europe/Berlin',
        'country': 'country for AI context, e.g. United States',
        'base': 'the address your watching apps use, e.g. https://skald.example.org',
        'internal-base': 'Skald address inside Docker for AIOStreams, e.g. http://skald:8080',
        'watch': 'watching app: stremio, aiostreams or jellyfin-app',
        'disconnect': 'remove a member MDBList, Stremio or PublicMetaDB connection and its private key',
        'connect': 'taste source: files, trakt, mdblist, stremio, pmdb, jellyfin, plex or aiostreams-db',
        'name': 'name for the laptop token created by link',
        'person': 'household member whose source or laptop token to use',
        'source-url': 'address of the Jellyfin or Plex server',
        'source-path': 'path to the AIOStreams database inside the engine',
        'user-id': 'Jellyfin user id to map to a member',
        'auth-env': 'environment variable holding the source token or API key',
        'client-id-env': 'environment variable holding the Trakt client id',
        'scopes': 'comma-separated laptop permissions (default: read,decide,events)',
        'map': 'map a source account: SOURCE_ID=MEMBER, SOURCE_ID=together or SOURCE_ID=skip for aiostreams-db (repeatable)',
    }
    for name in ('yes', 'json', 'status', 'check', 'secret-stdin', 'password-stdin'):
        p.add_argument('--' + name, action='store_true', default=False if name != 'json' else argparse.SUPPRESS, help=helps[name])
    for name in ('members', 'lang', 'row-lang', 'timezone', 'country', 'base', 'internal-base', 'watch', 'connect', 'disconnect', 'name', 'person',
                 'source-url', 'source-path', 'user-id', 'auth-env', 'client-id-env', 'aio-url', 'uuid', 'member'):
        p.add_argument('--' + name, help=helps[name])
    p.add_argument('--key-stdin', dest='secret_stdin', action='store_true', help='read the source key from stdin (same as --secret-stdin)')
    p.add_argument('--discovery', choices=['balanced', 'mainstream', 'gems'], help='member discovery style (default: balanced)')
    p.add_argument('--kids-age', help='limit child rows and search by age 3..17; off clears the limit')
    p.add_argument('--years', help='release years: FROM-TO, FROM- or -TO; use --years=-2000 for an upper bound')
    p.add_argument('--exclude-movie-genres', help='comma-separated TMDB movie genre names; empty clears exclusions')
    p.add_argument('--exclude-series-genres', help='comma-separated TMDB series genre names; empty clears exclusions')
    for action in ('hide', 'show'):
        p.add_argument('--' + action, help=action + ' comma-separated categories: ' + ', '.join(members.HIDE_CHOICES))
    for flag, label in (('movie-genres', 'TMDB movie genre names'), ('series-genres', 'TMDB series genre names'),
                        ('languages', 'two-letter language codes'), ('countries', 'two-letter country codes')):
        p.add_argument('--focus-' + flag, help='rank rows by comma-separated ' + label + '; empty clears focus')
    p.add_argument('--clear', action='store_true', help='remove all preferences for --person')
    p.add_argument('--scopes', default='read,decide,events', help=helps['scopes'])
    p.add_argument('--map', action='append', default=[], help=helps['map'])
    return p
