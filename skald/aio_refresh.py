"""Reload saved AIOStreams catalogs using setup's row ordering."""
from contextlib import nullcontext
import json
from pathlib import Path
import threading
import time

from . import setup


FAILURE = 'AIOStreams refresh failed; check its settings'
SAFE_HINTS = {
    'AIOStreams unavailable or invalid JSON; check --aio-url and try again',
    'AIOStreams rate limit; try again later',
    'AIOStreams wrong login; check UUID and password',
    'AIOStreams protected; check the instance access key and try again',
    'AIOStreams cannot reach Skald; same Docker network? else use --base https address',
    'AIOStreams rejected setup; check its settings or use the manual steps',
}


def refresh(data, *, transport=None, server=None):
    try:
        data = Path(data)
        saved = setup.state(data)
        url = saved.get('aio_url')
        if not url:
            return None
        logins = setup.aiostreams_logins(data)
        if not logins:
            return None
        base = setup.remote.validate_base(url)
        url = base + '/api/v1/user'
        for person, login in logins.items():
            reply = setup.aiostreams_request(url, login=login, transport=transport)['data']
            config = reply['userData']
            setup.aiostreams_request(url, method='PUT', body={'config': config}, login=login, transport=transport)
            if config.get('presets'):
                manifest = setup.aiostreams_request(base + '/stremio/' + login['uuid'] + '/' +
                    reply['encryptedPassword'] + '/manifest.json', transport=transport)
                with nullcontext(server) if server is not None else setup.server(data) as s:
                    prefix = setup.catalog_prefix(s, manifest)
                    if not prefix:
                        raise ValueError('AIOStreams has no Skald catalogs yet')
                    config = setup.aiostreams_request(url, login=login, transport=transport)['data']['userData']
                    changed = setup.keep_catalog_places(s, config, manifest, person,
                        first_added=not config.get('catalogModifications'))
                    order = config.setdefault('upstreamCatalogOrder', [])
                    if prefix not in order:
                        order.append(prefix)
                        changed = True
                    if changed:
                        setup.aiostreams_request(url, method='PUT', body={'config': config}, login=login, transport=transport)
        saved = setup.state(data)
        saved['aiostreams_pending'] = False
        saved.pop('aiostreams_hint', None)
        setup.write_private(data / 'setup.json', json.dumps(saved) + '\n')
        return f'AIOStreams rows refreshed ({len(logins)} logins)'
    except Exception as error:
        # Only known hints are safe; transport errors and private files can contain secrets.
        hint = str(error) if isinstance(error, ValueError) and str(error) in SAFE_HINTS else FAILURE
        try:
            saved = setup.state(Path(data))
            saved.update(aiostreams_pending=True, aiostreams_hint=hint)
            setup.write_private(Path(data) / 'setup.json', json.dumps(saved) + '\n')
        except Exception:
            pass
        return hint


class BackgroundRefresh:
    def __init__(self, data, report, *, transport=None, server=None):
        self.data, self.report, self.transport = data, report, transport
        self.server = server
        self.lock = threading.Lock()
        self.thread = None
        self.pending = False
        self.failed = False

    def schedule(self):
        try:
            data = Path(self.data)
            if not (data / 'setup.json').is_file() or not setup.aiostreams_logins(data):
                return
            with self.lock:
                self.pending = True
                if self.thread is None:
                    self.thread = threading.Thread(target=self.run, daemon=True, name='skald-aio-refresh')
                    self.thread.start()
        except Exception:
            with self.lock:
                self.thread = None
                self.failed = True
            self.report(FAILURE)

    def run(self):
        while True:
            time.sleep(45)
            with self.lock:
                self.pending = False
            line = refresh(self.data, transport=self.transport, server=self.server() if self.server else None)
            self.failed = bool(line and not line.startswith('AIOStreams rows refreshed'))
            if line:
                self.report(line)
            with self.lock:
                if not self.pending:
                    self.thread = None
                    return

    def repair(self):
        """Retry saved setup or a failed row refresh without creating credentials."""
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return 'pending'
            if not setup.aiostreams_logins(Path(self.data)):
                return False
            self.thread = threading.Thread(target=self.retry, daemon=True, name='skald-aio-repair')
            self.thread.start()
        return 'pending'

    def retry(self):
        with self.lock:
            self.pending = False
        try:
            data = Path(self.data)
            saved = setup.state(data)
            if saved.get('aiostreams_pending'):
                from .__main__ import parser
                args = parser().parse_args(['setup', 'aiostreams', '--yes', '--json'])
                args.member = saved.get('household_member')
                logins = setup.aiostreams_logins(data)
                if (args.member or 'household') not in logins:
                    raise ValueError('saved login missing')
                def ask(value, label, default, flag):
                    if value or default:
                        return value or default
                    raise ValueError('saved setup incomplete')
                access = setup.connectors.aiostreams_access_key(saved['source_path']) if saved.get('source_path') else None
                with setup.server(data) as s:
                    result = setup.setup_aiostreams(s, args, saved, ask,
                        [], lambda: None, transport=self.transport, secret_input=access, private=True)
                self.failed = not result['ok']
            else:
                line = refresh(data, transport=self.transport)
                self.failed = bool(line and not line.startswith('AIOStreams rows refreshed'))
        except Exception:
            self.failed = True
        finally:
            with self.lock:
                pending = self.pending
                if not pending:
                    self.thread = None
            if pending:
                self.run()
