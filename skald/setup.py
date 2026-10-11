"""Guided setup on the machine holding DATA; no snapshot is required."""
from contextlib import contextmanager, redirect_stdout
import argparse
import copy
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from . import members, recipes, connectors, remote, guide
from .intake import write_private


@contextmanager
def server(data):
    spec = importlib.util.spec_from_file_location('skald_setup_server', Path(__file__).resolve().parents[1]/'server.py')
    s = importlib.util.module_from_spec(spec)
    s._CONFIG_READONLY = True
    with patch.dict(os.environ, {'DATA_DIR': str(data)}), redirect_stdout(io.StringIO()):
        spec.loader.exec_module(s)
    try:
        yield s
    finally:
        s._decide_queue.put(False)
        s._decide_queue.join()
        for name in ('_pool', '_outer', '_nzb_pool', '_connector_pool'):
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


def publish(data, cfg, *, replace=False, on_publish=None):
    cfg = members.validate_config(cfg, data=str(data))
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if not members.publish_config(str(data), str(path), cfg, replace=replace, on_publish=on_publish):
        raise ValueError('household was created by another setup; run skald setup again')
    return cfg


def member_data(data, person, cfg):
    if cfg.get('taste', {}).get('people', {}).get(person):
        return True
    if person in cfg['connectors'].get('trakt_api', {}).get('accounts', {}):
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
        if path.name in ('skald.json', 'setup.json', 'legacy.json', 'addon.json') or path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith('trakt-' + person + '.'):
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


def household(data, names, lang, timezone, country, *, existing=None):
    data = Path(data)
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    existing = path.exists() if existing is None else existing
    if (data/'legacy.json').exists() and not existing:
        raise ValueError('this existing household is read-only in setup; keep its current configuration')
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
    return publish(data, cfg, replace=existing)


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


def status(data):
    saved = state(data)
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    config = members.validate_config(json.loads(path.read_text()), data=str(data)) if path.exists() else None
    legacy = (data/'legacy.json').exists() and config is None
    steps = {'checks': bool(saved.get('tmdb_checked') and os.environ.get('TMDB_API')),
             'household': bool(config) or legacy, 'address': bool(saved.get('base')),
             'watch': bool(saved.get('watch')), 'taste': not saved.get('connector_failed') and (bool(saved.get('taste')) or bool(config and config['connectors']))}
    missing = [k for k, done in steps.items() if not done]
    commands = {'checks': 'skald setup', 'household': 'skald setup', 'address': 'skald setup --base YOUR_ADDRESS',
                'watch': 'skald setup --watch stremio', 'taste': 'skald setup --connect files'}
    next_command = 'skald connectors test' if saved.get('connector_failed') else commands[missing[0]] if missing else ('docker compose -f compose.stack.example.yaml restart skald' if saved.get('internal_base') else 'docker compose restart engine')
    if 'checks' in missing:
        missing[missing.index('checks')] = ('TMDB_API not set; put it in .env, then docker compose up -d (engine) or docker compose -f compose.stack.example.yaml up -d (skald); without Docker: export TMDB_API=...'
                                            if not os.environ.get('TMDB_API') else 'TMDB key not checked; run skald setup')
    from .migrate import check
    return {'release': members.VERSION, 'data_format': check(data)['format'], 'done': [k for k, done in steps.items() if done], 'missing': missing,
            'next': next_command, 'ok': not missing,
            'legacy_readonly': legacy}


def link(s, name, person, scopes, base):
    base = remote.validate_base(base)
    token = s.new_client(name, scopes, person)
    return {'token': token, 'lines': ['Keep this token private; it is shown once.',
        token, 'The engine needs API_PORT=8081 and /api/v1/ forwarded to port 8081; see docs/SETUP.md.', 'On your laptop, from the Skald checkout:', 'python3 -m skald connect ' + shlex.quote(base),
        'Paste the token at the private prompt.', 'export SKALD_MEMBERS=' + shlex.quote(','.join(s.PEOPLE)), 'python3 -m skald onboard --sources',
        'python3 -m skald push imports --remote',
        'After reviewing the preview: python3 -m skald push imports --remote --apply']}


def configure_connector(s, args, ask, lines, show):
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
        result = _configure_connector(s, args, ask, lines, show, stage, published)
        committed = result is None or result['ok']
        return result
    finally:
        if not committed:
            s.CONFIG = previous
            for path in staged:
                path.unlink(missing_ok=True)


def _configure_connector(s, args, ask, lines, show, stage, published):
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
        if old_options.get('archived'):
            opts['archived'] = old_options['archived']
        users = connectors.source_users(s, cid, opts)
        mapping = {**old_options.get('profiles', {}), **parse_mapping(args.map, cfg)}
        for uid in users:
            if uid not in mapping:
                mapping[uid] = ask(None, 'Member for profile ' + uid + ' (or together)', None, '--map')
        opts['profiles'] = {p: 'household' if who == 'together' else who for p, who in mapping.items()}
        c = {'kind': 'player', 'enabled': True, 'auth': None, 'options': opts}
    else:
        instructions = {'trakt_api': 'Create a Trakt API app at https://trakt.tv/oauth/applications (may require VIP). Use its client id and an access token from your authorized account.',
            'jellyfin': 'Create an API key in Jellyfin: Dashboard → Advanced → API Keys → Add.',
            'plex': 'Get a Plex token: open a media item in Plex Web → Get Info → View XML; copy X-Plex-Token privately.'}
        lines.append(instructions[cid])
        show()
        if cid == 'trakt_api':
            person = ask(args.person, 'Trakt member', cfg['household']['trakt_member'], '--person')
            if person not in cfg['household']['members']:
                raise ValueError('choose a household member with --person')
        previous_auth = previous.get('accounts', {}).get(person) if cid == 'trakt_api' and 'accounts' in previous else previous.get('auth')
        if cid == 'trakt_api' and person != cfg['household']['trakt_member'] and 'accounts' not in previous:
            previous_auth = None
        auth = args.auth_env
        if auth:
            auth = 'env:' + auth
        elif previous_auth and not args.secret_stdin:
            auth = previous_auth
        else:
            if args.yes and not args.secret_stdin:
                raise ValueError('secret needed; use --secret-stdin or --auth-env VARIABLE')
            auth = stage(secret(args.secret_stdin, cid + ' secret'), 'trakt-' + person if cid == 'trakt_api' else cid)
        if cid == 'trakt_api':
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
            mapping = parse_mapping(args.map, cfg)
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
        s.CONFIG = publish(Path(s.DATA), cfg, replace=True, on_publish=published)
    return result


def parse_mapping(values, cfg):
    result = {}
    for value in values:
        uid, sep, person = value.rpartition('=')
        if not sep or not uid or person not in (*cfg['household']['members'], 'together'):
            raise ValueError('use --map SOURCE_ID=MEMBER, or SOURCE_ID=together')
        result[uid] = person
    return result


def run(args):
    data = Path(os.environ.get('DATA_DIR', '/data'))
    if args.status:
        return status(data)
    config_path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if (data/'legacy.json').exists() and not config_path.exists():
        raise ValueError('this existing household is read-only in setup; keep its current configuration')
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
    check_tmdb(data)
    saved['tmdb_checked'] = True
    write_private(data/'setup.json', json.dumps(saved) + '\n')
    lines.extend(['Checks: DATA writable and private; TMDB key valid.',
        'OpenRouter adds AI picks, natural-language search, mood shelves and weekly picks; rows work without it.' +
        (' Key present.' if os.environ.get('OPENROUTER_API_KEY') else ' Optional key absent.')])
    path = Path(os.environ.get('SKALD_CONFIG') or data/'skald.json')
    if (data/'legacy.json').exists() and not path.exists():
        raise ValueError('this existing household is read-only in setup; keep its current configuration')
    existing = path.exists()
    names = [name.strip().lower() for name in args.members.split(',')] if args.members is not None else None
    if not existing:
        raw = ask(args.members, 'Household names, 1–6, comma-separated (lowercase)', None, '--members')
        names = [n.strip().lower() for n in raw.split(',')]
    lang = args.lang if existing else ask(args.lang, 'Language', 'en-US', '--lang')
    tz = args.timezone if existing else ask(args.timezone, 'Timezone', 'UTC', '--timezone')
    country = args.country if existing else ask(args.country, 'Country', 'United States', '--country')
    cfg = household(data, names, lang, tz, country, existing=existing) if not existing or names is not None or any((lang, tz, country)) else members.validate_config(json.loads(path.read_text()), data=str(data))
    lines.append('Household: ' + ', '.join(cfg['household']['members']) + '; ' + cfg['region']['lang'] + '; ' + cfg['region']['tz'] + '; ' + cfg['region']['country'] + '.')
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
        if args.connect or not saved.get('taste') and not cfg['connectors']:
            args.connect = ask(args.connect, 'Taste source: trakt, jellyfin, plex, aiostreams-db, files', 'files', '--connect')
            if args.connect == 'aiostreams-db':
                args.connect = 'aiostreams_db'
            if args.connect not in ('trakt', 'jellyfin', 'plex', 'aiostreams_db', 'files'):
                raise ValueError('choose a supported --connect source')
            result = configure_connector(s, args, ask, lines, show)
            saved['taste'] = args.connect
            if result and not result['ok']:
                saved.pop('taste', None)
                saved['connector_failed'] = True
            else:
                saved.pop('connector_failed', None)
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
    p.add_argument('setup_action', nargs='?', choices=['link'], help='link creates a private token for a laptop')
    helps = {
        'yes': 'use supplied values and defaults without prompting',
        'json': 'print a JSON result with addon tokens hidden',
        'status': 'show completed steps, missing settings and one next command',
        'check': 'fetch <base>/<lang>/manifest.json and say if your apps can reach it',
        'secret-stdin': 'read the secret from stdin instead of a prompt',
        'members': 'comma-separated household member names, e.g. alex,sam',
        'lang': 'language for titles, e.g. en-US',
        'timezone': 'timezone for dates, e.g. Europe/Berlin',
        'country': 'country for AI context, e.g. United States',
        'base': 'the address your watching apps use, e.g. https://skald.example.org',
        'internal-base': 'Skald address inside Docker for AIOStreams, e.g. http://skald:8080',
        'watch': 'watching app: stremio, aiostreams or jellyfin-app',
        'connect': 'taste source: files, trakt, jellyfin, plex or aiostreams-db',
        'name': 'name for the laptop token created by link',
        'person': 'household member whose source or laptop token to use',
        'source-url': 'address of the Jellyfin or Plex server',
        'source-path': 'path to the AIOStreams database inside the engine',
        'user-id': 'Jellyfin user id to map to a member',
        'auth-env': 'environment variable holding the source token or API key',
        'client-id-env': 'environment variable holding the Trakt client id',
        'scopes': 'comma-separated laptop permissions (default: read,decide,events)',
        'map': 'map a source account to a member: SOURCE_ID=MEMBER or SOURCE_ID=together (repeatable)',
    }
    for name in ('yes', 'json', 'status', 'check', 'secret-stdin'):
        p.add_argument('--' + name, action='store_true', default=False if name != 'json' else argparse.SUPPRESS, help=helps[name])
    for name in ('members', 'lang', 'timezone', 'country', 'base', 'internal-base', 'watch', 'connect', 'name', 'person',
                 'source-url', 'source-path', 'user-id', 'auth-env', 'client-id-env'):
        p.add_argument('--' + name, help=helps[name])
    p.add_argument('--scopes', default='read,decide,events', help=helps['scopes'])
    p.add_argument('--map', action='append', default=[], help=helps['map'])
    return p
