"""Opt-in health observations, bounded cache repair and private alert transport."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from .intake import write_private

LOG_LIMIT = 256 * 1024
SERVING_CACHES = frozenset(('feeds.json', 'shown.json', 'shown_scopes.json'))
DEFAULTS = {'enabled': False, 'repair': False, 'cooldown': 1800, 'max_attempts': 3,
            'connector_failures': 3, 'disk_min_bytes': 512 * 1024 * 1024,
            'quiet_hours': '', 'timezone': 'UTC', 'digest': False}


def read_json(path, default):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, UnicodeError):
        return default


def settings(data):
    cfg = {**DEFAULTS, **read_json(Path(data) / 'health-settings.json', {})}
    for field in ('cooldown', 'max_attempts', 'disk_min_bytes', 'connector_failures'):
        if type(cfg[field]) not in (int, float) or not 0 <= cfg[field] < float('inf'):
            cfg[field] = DEFAULTS[field]
    return cfg


def key(kind, value):
    # Row, account and unknown file names never enter logs or notifications.
    return kind + ':' + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def serving_cache(value, *, scoped=False):
    if scoped:
        return isinstance(value, dict) and all(serving_cache(v) for v in value.values())
    return isinstance(value, dict) and all(
        isinstance(k, str) and k.count('|') == 1 and isinstance(v, dict) and
        isinstance(v.get('metas', {}), dict) and isinstance(v.get('names', {}), dict)
        for k, v in value.items())


def send_message(channel, message):
    """The single outbound alert function. Never propagate private request errors."""
    try:
        if 'telegram' in channel:
            cfg = channel['telegram']
            url = 'https://api.telegram.org/bot' + cfg['token'] + '/sendMessage'
            body = {'chat_id': cfg['chat_id'], 'text': message}
        else:
            cfg = channel['webhook']
            url = cfg['url']
            body = {'content': message} if cfg.get('style') == 'discord' else {'message': message, 'title': 'Skald health'}
            if cfg.get('style') == 'discord':
                parsed = urllib.parse.urlsplit(url)
                query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != 'wait']
                url = parsed._replace(query=urllib.parse.urlencode(query + [('wait', 'true')])).geturl()
            if cfg.get('style') == 'ntfy':
                parsed = urllib.parse.urlsplit(url)
                base, _, topic = parsed.path.rstrip('/').rpartition('/')
                body['topic'] = topic
                url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base + '/', parsed.query, ''))
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            if 'webhook' in channel and channel['webhook'].get('style') == 'discord' and not 200 <= response.status < 300:
                return False
            raw = response.read(65536)
            if 'telegram' in channel and not json.loads(raw).get('ok'):
                return False
        return True
    except Exception:
        return False


def configured(data):
    return (Path(data) / 'credentials/alerts.json').is_file()


def view(data):
    value = read_json(Path(data) / 'health-status.json', {})
    return {**value, 'ok': value.get('ok', True), 'enabled': settings(data)['enabled'],
            'checks': value.get('checks', []), 'last_repair': value.get('last_repair'),
            'alert_configured': configured(data)}


def setup_alerts(data, args):
    from . import setup
    cfg = settings(data)
    if args.quiet_hours is not None:
        try:
            if args.quiet_hours:
                start, end = (int(x) for x in args.quiet_hours.split('-'))
                if not 0 <= start <= 23 or not 0 <= end <= 23 or start == end:
                    raise ValueError()
        except ValueError:
            raise ValueError('quiet hours need HH-HH, for example 22-07') from None
        cfg['quiet_hours'] = args.quiet_hours
    if args.timezone:
        try:
            ZoneInfo(args.timezone)
        except (ValueError, KeyError):
            raise ValueError('unknown health timezone') from None
        cfg['timezone'] = args.timezone
    if args.enable_health:
        cfg['enabled'] = True
    for flag in ('repair', 'digest'):
        if getattr(args, flag) is not None:
            cfg[flag] = getattr(args, flag)
    if args.disable_health:
        cfg['enabled'] = False
    if args.telegram and args.webhook:
        raise ValueError('configure one alert channel at a time')
    channels = read_json(Path(data) / 'credentials/alerts.json', {})
    if args.telegram:
        if not args.chat_id:
            raise ValueError('Telegram needs --chat-id')
        token = setup.secret(args.token_stdin, 'Telegram bot token')
        if not token or any(c.isspace() for c in token):
            raise ValueError('invalid Telegram token')
        channels['telegram'] = {'token': token, 'chat_id': args.chat_id}
    if args.webhook:
        url = setup.secret(args.url_stdin, 'Webhook URL')
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError()
        except ValueError:
            raise ValueError('invalid webhook address') from None
        channels['webhook'] = {'url': url, 'style': args.webhook_style}
    if args.telegram or args.webhook:
        write_private(Path(data) / 'credentials/alerts.json', json.dumps(channels) + '\n')
    write_private(Path(data) / 'health-settings.json', json.dumps(cfg) + '\n')
    ok = True
    if args.test:
        if not channels:
            raise ValueError('configure an alert channel before testing')
        ok = Health(data).notify('Skald health test')
    return {'ok': ok, 'alert_configured': configured(data), 'lines': [
        'Alert test sent' if args.test and ok else 'Alert test failed' if args.test else 'Health settings saved; restart the server to apply']}


class Health:
    def __init__(self, data, *, clock=time.time, transport=None, cache_changed=None):
        self.data, self.clock, self.transport = Path(data), clock, transport
        self.cache_changed = cache_changed
        saved = read_json(self.data / 'health-state.json', None)
        self.ledger_valid = saved is not None or not (self.data / 'health-state.json').exists()
        self.state = saved or {}
        self.state.setdefault('checks', {})
        self.state.setdefault('polls', {})
        self.state.setdefault('rows', {})
        self.state.setdefault('digest_at', clock())
        self.file_stamps = {}

    def save_state(self):
        if self.ledger_valid:
            write_private(self.data / 'health-state.json', json.dumps(self.state) + '\n')

    def record(self, check, outcome):
        entry = {'at': self.clock(), 'check': check, 'action': outcome}
        path = self.data / 'health.jsonl'
        raw = (json.dumps(entry) + '\n').encode()
        # One bounded journal, with only complete records retained.
        if path.exists() and path.stat().st_size + len(raw) > LOG_LIMIT:
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - LOG_LIMIT // 2))
                stream.readline()
                tail = stream.read()
            write_private(path, tail.decode('utf-8'))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(raw)
        self.state['last_repair'] = entry

    def notify(self, message, delivered=None):
        delivered = delivered if delivered is not None else []
        channels = read_json(self.data / 'credentials/alerts.json', {})
        for name in ('telegram', 'webhook'):
            if name not in channels or name in delivered:
                continue
            try:
                ok = (self.transport or send_message)({name: channels[name]}, message)
            except Exception:
                ok = False
            if ok:
                delivered.append(name)
        return bool(channels) and all(name in delivered for name in channels)

    def quiet(self, cfg, now):
        if not cfg['quiet_hours']:
            return False
        start, end = map(int, cfg['quiet_hours'].split('-'))
        hour = datetime.datetime.fromtimestamp(now, ZoneInfo(cfg['timezone'])).hour
        return start <= hour < end if start < end else hour >= start or hour < end

    def cache_current(self, path):
        try:
            forgotten = (self.data / 'history-forgotten.json').stat().st_mtime_ns
        except FileNotFoundError:
            return True
        try:
            return path.stat().st_mtime_ns > forgotten
        except FileNotFoundError:
            return False

    def cache_repair(self, name):
        if name not in SERVING_CACHES:
            return False
        path, good = self.data / name, self.data / ('health-cache-' + name)
        # A good copy must still parse and have the serving cache's mapping shape.
        restored = read_json(good, None) if self.cache_current(good) else None
        if restored is None:
            good.unlink(missing_ok=True)
        if serving_cache(restored, scoped=name == 'shown_scopes.json'):
            write_private(path, json.dumps(restored) + '\n')
        else:
            path.unlink(missing_ok=True)
            restored = {}
        if self.cache_changed:
            self.cache_changed(name, restored)
        self.file_stamps.pop(name, None)
        return True

    def json_checks(self, cfg):
        checks, actions = [], {}
        for path in sorted(self.data.glob('*.json')):
            if path.name.startswith('health-cache-') or path.name == 'health-status.json':
                continue
            check = 'json:' + path.name if path.name in SERVING_CACHES else key('json', path.name)
            valid = False
            try:
                stamp = (path.stat().st_mtime_ns, path.stat().st_size)
                if self.file_stamps.get(path.name) == stamp:
                    valid = True
                else:
                    raw = path.read_text()
                    value = json.loads(raw)
                    if path.name in SERVING_CACHES and not serving_cache(value, scoped=path.name == 'shown_scopes.json'):
                        raise ValueError()
                    valid = True
                    if cfg['repair'] and path.name in SERVING_CACHES and self.cache_current(path):
                        write_private(self.data / ('health-cache-' + path.name), raw)
                    self.file_stamps[path.name] = stamp
            except (OSError, ValueError, UnicodeError):
                pass
            checks.append({'id': check, 'state': 'ok' if valid else 'error'})
            if path.name in SERVING_CACHES:
                actions[check] = lambda name=path.name: self.cache_repair(name)
        return checks, actions

    def tick(self, snapshot, actions=None):
        cfg, now = settings(self.data), self.clock()
        if not cfg['enabled']:
            return view(self.data)
        if not self.ledger_valid:
            cfg['repair'] = False
        actions = dict(actions or {})
        checks = []
        def add(check, failed):
            checks.append({'id': check, 'state': 'error' if failed else 'ok'})
        add('worker', not snapshot['worker_alive'])
        for check, feed in snapshot.get('feeds', {}).items():
            add(check, now - feed['built'] > feed['interval'] + 300)
            checks[-1].update(last_success=feed['built'], interval=feed['interval'])
            if feed.get('label'):
                checks[-1]['label'] = feed['label']
        for check, poll in snapshot.get('connectors', {}).items():
            old = self.state['polls'].setdefault(check, {'polls': 0, 'failures': 0})
            if poll['polls'] != old['polls']:
                delta = max(1, poll['polls'] - old['polls'])
                old['failures'] = old['failures'] + delta if poll['failed'] else 0
                old['polls'] = poll['polls']
            failures = old['failures'] = poll.get('failures', old['failures'])
            add(check, failures >= cfg['connector_failures'])
        add('budget', snapshot.get('budget_over', False) or snapshot.get('budget_ratio', 0) >= .9)
        add('aiostreams', snapshot.get('aio_pending', False))
        free = snapshot.get('disk_free')
        if free is None:
            free = shutil.disk_usage(self.data).free
        add('disk', free < cfg['disk_min_bytes'])
        for check, count in snapshot.get('rows', {}).items():
            if count:
                self.state['rows'][check] = True
            add(check, not count and self.state['rows'].get(check, False))
        files, file_actions = self.json_checks(cfg)
        checks += files
        actions.update(file_actions)
        quiet = self.quiet(cfg, now)
        day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()
        present = {c['id'] for c in checks}
        # A removed cache or disabled connector is a recovered check.
        checks += [{'id': check, 'state': 'ok'} for check in self.state['checks'] if check not in present]
        for check in checks:
            cid = check['id']
            rec = self.state['checks'].setdefault(cid, {'day': day, 'attempts': 0, 'at': 0, 'delivered': []})
            if check['state'] == 'ok':
                if rec['delivered'] and not quiet:
                    sent = rec.setdefault('recovery', [])
                    self.notify('Skald recovered: ' + cid, sent)
                    if all(c in sent for c in rec['delivered']):
                        rec['delivered'] = []
                        rec.pop('recovery', None)
                rec.pop('pending', None)
                rec['failed'] = False
                continue
            if not rec.get('failed'):
                rec.pop('recovery', None)
            rec['failed'] = True
            if rec['day'] != day:
                rec.update(day=day, attempts=0)
            rec.pop('pending', None)
            if cfg['repair'] and cid in actions and rec['attempts'] < cfg['max_attempts'] and (not rec['at'] or now - rec['at'] >= cfg['cooldown'] * (2 ** max(0, rec['attempts'] - 1) if cid.startswith('connector:') else 1)):
                rec.update(at=now, attempts=rec['attempts'] + 1)
                # Persist the claim first so a crash cannot reset the daily repair limit.
                self.save_state()
                try:
                    result = actions[cid]()
                except Exception:
                    result = False
                self.record(cid, 'pending' if result == 'pending' else 'repaired' if result is True else 'failed')
                if result == 'pending':
                    rec['pending'] = True
                    continue
                if result is True:
                    rec['pending'] = True
                    continue
            if not quiet:
                self.notify('Skald health: ' + cid + '; repair failed or unavailable', rec['delivered'])
        if cfg['digest'] and now - self.state['digest_at'] >= 86400 and not quiet:
            sent = self.state.setdefault('digest_delivered', [])
            if self.notify('Skald daily health digest: ' + str(sum(c['state'] != 'ok' for c in checks)) + ' failing checks', sent):
                self.state['digest_at'] = now
                self.state.pop('digest_delivered', None)
        result = {'ok': all(c['state'] == 'ok' for c in checks), 'enabled': True, 'at': now,
                  'checks': checks, 'last_repair': self.state.get('last_repair'),
                  'alert_configured': configured(self.data)}
        self.save_state()
        write_private(self.data / 'health-status.json', json.dumps(result) + '\n')
        return result
