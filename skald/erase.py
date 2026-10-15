"""Owner-run household erasure. Unknown DATA files are personal by default."""
import functools
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading

LOCK = 'erase.lock'
_write_lock = threading.RLock()
_locked_paths = set()

KEEP = {
    'addon.json': 'addon capability, so existing player links keep answering',
    'remote.json': 'server remote capability, with no household records',
    '.skald-format': 'data format and release marker',
    'setup.json': 'addresses only: base, internal_base, aio_url',
    'history.lock': 'stable lock inode for history writers',
    'shelves.lock': 'stable lock inode for shelf writers',
    'usage.lock': 'stable lock inode for usage writers',
    LOCK: 'stable lock inode shared by erase and file publication',
    '.skald-erased': 'empty generation marker that blocks stale server writes until restart',
}
ADDRESSES = ('base', 'internal_base', 'aio_url')
OUTSIDE = "Backups outside Skald's data folder, for example copies your host makes, are not touched. Delete them yourself."


def erased(data):
    return (Path(data)/'.skald-erased').exists() and not (Path(data)/'skald.json').exists()


def epoch(data):
    try:
        st = (Path(data)/'.skald-erased').stat()
        return st.st_ino, st.st_mtime_ns
    except FileNotFoundError:
        return None


@contextmanager
def locked(data):
    """Leaf lock: callers take all other locks first; only file IO belongs inside."""
    path = Path(data).resolve() / LOCK
    with _write_lock:
        if path in _locked_paths:
            yield
            return
        with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'a') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            _locked_paths.add(path)
            try:
                yield
            finally:
                _locked_paths.remove(path)
                fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def writable(data, check):
    with locked(data):
        yield not check(data)


def writing(fn):
    """Guard file primitives only, never functions that take application locks."""
    @functools.wraps(fn)
    def write(*args, **kwargs):
        data = fn.__globals__['DATA']
        with locked(data):
            if fn.__globals__['household_erased'](data):
                return False
            return fn(*args, **kwargs)
    return write


def publish(s, cfg):
    from .setup import publish as save
    return save(Path(s.DATA), cfg, replace=True, write_check=s.household_erased)


def preview(s):
    from .commands import seal
    from .members import load_config
    data = Path(s.DATA)
    cfg, source = load_config(str(data), readonly=True)
    counts = dict.fromkeys(('people', 'watches', 'imports', 'summaries', 'shelves', 'settings history',
                            'logins', 'backups', 'caches', 'other files'), 0)
    counts['people'] = len(cfg['household']['members']) if source != 'implicit' else 0
    stamp = hashlib.sha256()
    for path in sorted(data.rglob('*')):
        name = path.relative_to(data).as_posix()
        if name in ('history.lock', 'shelves.lock', 'usage.lock', LOCK):
            continue
        stamp.update(name.encode()+b'\0')
        if path.is_symlink():
            stamp.update(os.readlink(path).encode())
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as fh:
                for block in iter(lambda: fh.read(1024*1024), b''):
                    digest.update(block)
            stamp.update(digest.digest())
        else:
            continue
        if name in KEEP:
            continue
        if name.startswith('backups/'):
            counts['backups'] += 1
        elif name.startswith('credentials/'):
            if name == 'credentials/aiostreams.json':
                counts['logins'] += len(json.loads(path.read_text()))
            else:
                counts['logins'] += 1
        elif name == 'imports.jsonl':
            counts['imports'] += len([line for line in path.read_text().splitlines() if line.strip()])
        elif name == 'history.json' or name == 'trakt.json' or name.endswith('.history.json'):
            counts['watches'] += len(json.loads(path.read_text()).get('items', []))
        elif name == 'history-summary.json':
            counts['summaries'] += len(json.loads(path.read_text()).get('items', []))
        elif name == 'shelves.json':
            counts['shelves'] += len(json.loads(path.read_text()).get('wishes', []))
        elif name == 'commands.json':
            counts['settings history'] += sum(len(rows) for rows in json.loads(path.read_text()).values())
        elif name in ('profile.json', 'plays.json', 'connectors.json', 'feeds.json') or 'cache' in name:
            counts['caches'] += 1
        else:
            counts['other files'] += 1
    description = '\n'.join([str(n)+' '+kind+' deleted' for kind, n in counts.items()] +
        ['Keeps '+name+': '+why+'.' for name, why in KEEP.items()] +
        ['This cannot be undone. Two confirmations are required: --yes --plan CODE, then type ERASE or add --confirm ERASE.',
         'To erase everything, run on the server: python -m skald erase --household'])
    return seal(dict(read_only=True, people=[], household_erase=True, counts=counts, kept=KEEP,
                     stamp=stamp.hexdigest(), name='Erase the whole household', description=description, source='search'))


def apply(s, plan):
    from . import commands, history, metrics, plays, shelves
    from .intake import write_private
    if plan.get('source') != 'cli':
        raise PermissionError('Erase runs only from the server command line')
    if plan.get('confirm') != 'ERASE':
        raise ValueError('Second confirmation required: --confirm ERASE')
    data = Path(s.DATA)
    with commands._LOCK, shelves.locked(s), history.locked(data), plays._request_lock, metrics._lock:
        if preview(s)['plan'] != plan['plan']:
            raise ValueError('Plan changed; preview again')
        from .setup import state
        saved = state(data)
        addresses = {key: saved[key] for key in ADDRESSES if key in saved}
        with locked(data):
            write_private(data/'.skald-erased', '')
            if (data/'setup.json').exists():
                write_private(data/'setup.json', json.dumps(addresses)+'\n')
            for path in data.iterdir():
                if path.name in KEEP and path.is_file() and not path.is_symlink():
                    continue
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        s.forget_person_caches()
        commands.pending(s).clear()
    return {'name': 'Done: household erased. Run python -m skald setup household, then restart Skald. '+OUTSIDE}
