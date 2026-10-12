"""Reload AIOStreams manifests by saving each stored user's unchanged config."""
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
    'AIOStreams cannot reach the Skald URL; same Docker network? else use --base https address',
    'AIOStreams rejected setup; check its settings or use the manual steps',
}


def refresh(data, *, transport=None):
    try:
        data = Path(data)
        url = setup.state(data).get('aio_url')
        if not url:
            return None
        logins = setup.aiostreams_logins(data)
        if not logins:
            return None
        url = setup.remote.validate_base(url) + '/api/v1/user'
        for login in logins.values():
            config = setup.aiostreams_request(url, login=login, transport=transport)['data']['userData']
            setup.aiostreams_request(url, method='PUT', body={'config': config}, login=login, transport=transport)
        return f'AIOStreams rows refreshed ({len(logins)} logins)'
    except Exception as error:
        # Only known hints are safe; transport errors and private files can contain secrets.
        return str(error) if isinstance(error, ValueError) and str(error) in SAFE_HINTS else FAILURE


class BackgroundRefresh:
    def __init__(self, data, report, *, transport=None):
        self.data, self.report, self.transport = data, report, transport
        self.lock = threading.Lock()
        self.thread = None
        self.pending = False

    def schedule(self):
        try:
            data = Path(self.data)
            if not (data / 'setup.json').is_file() or not (data / 'credentials/aiostreams.json').is_file():
                return
            with self.lock:
                self.pending = True
                if self.thread is None:
                    self.thread = threading.Thread(target=self.run, daemon=True, name='skald-aio-refresh')
                    self.thread.start()
        except Exception:
            with self.lock:
                self.thread = None
            self.report(FAILURE)

    def run(self):
        while True:
            time.sleep(2)
            with self.lock:
                self.pending = False
            line = refresh(self.data, transport=self.transport)
            if line:
                self.report(line)
            with self.lock:
                if not self.pending:
                    self.thread = None
                    return
