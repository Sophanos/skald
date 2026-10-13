"""Ordered, idempotent migrations of backed-up household state."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import tarfile
import time

FORMAT = 5
LOCK = '.skald-migrate.lock'
LOCK_TIMEOUT = 30

def settings_v2(data):
    """Declare optional row labels/order, weights, runtime and people focus.

    The format marker is the only required write for these additive settings.
    Missing maps retain the old behavior; stored ids and journals stay intact.
    """

def local_row_metrics(data):
    """New metrics start empty; old serving pins have no exposure timestamp."""
    from .metrics import empty, settings
    from .members import load_config
    from .intake import write_private
    config, _ = load_config(str(data), {}, readonly=True)
    if settings(config)['enabled']:
        path = Path(data)/'metrics.json'
        if not path.exists():
            write_private(path, json.dumps(empty()) + '\n')


def settings_history_v4(data):
    """Keep undated commands as legacy history without inventing a date."""
    from .intake import write_private
    path = Path(data)/'commands.json'
    if not path.exists():
        return
    entries = json.loads(path.read_text())
    for rows in entries.values():
        for entry in rows:
            for key, value in dict(t=None, text='', source=None, change=None, after=None).items():
                entry.setdefault(key, value)
    write_private(path, json.dumps(entries, ensure_ascii=False) + '\n')


def monthly_history(data):
    from .history import SUMMARY
    from .intake import write_private
    if not (data / SUMMARY).exists():
        write_private(data / SUMMARY, json.dumps({'v': 1, 'items': []}) + '\n')


# Keys are destination formats; functions accept a staged DATA path.
MIGRATIONS = {2: settings_v2, 3: local_row_metrics, 4: settings_history_v4, 5: monthly_history}


def rewrite_needed(data):
    """Read-only commands can defer settings, optional metrics and history to the engine."""
    return any(MIGRATIONS[v] not in (settings_v2, local_row_metrics, settings_history_v4, monthly_history)
               for v in range(check(data)['format'] + 1, FORMAT + 1))


class FormatError(ValueError):
    pass


def check(data):
    from .members import VERSION
    path = Path(data)/'.skald-format'
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {'format': 1, 'release': VERSION}
    except (ValueError, UnicodeError):
        raise FormatError('invalid DATA/.skald-format; restore a backup') from None
    if not isinstance(value, dict) or type(value.get('format')) is not int or value['format'] < 1 or not isinstance(value.get('release'), str):
        raise FormatError('invalid DATA/.skald-format; restore a backup')
    if value['format'] > FORMAT:
        raise FormatError(f"DATA needs Skald {value['release']} (format {value['format']}); upgrade Skald or restore a backup")
    return value


def ensure(data):
    data = Path(data)
    check(data)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(data/LOCK, os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FormatError(f'timed out waiting for DATA/{LOCK}; another migration is running') from None
                time.sleep(.05)
        try:
            return _ensure(data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _ensure(data):
    from . import backup
    from .intake import write_private
    from .members import VERSION
    data = Path(data)
    current = check(data)['format']
    target = {'format': FORMAT, 'release': VERSION}
    # Fresh households have no old state to archive or migrate.
    fresh = not any((data / name).exists() for name in backup.BACKUP_FILES if name != '.skald-format')
    if current < FORMAT and not fresh:
        steps = [MIGRATIONS[version] for version in range(current + 1, FORMAT + 1)]
        archive = backup.create(data, from_format=current, to_format=FORMAT)
        with tarfile.open(archive) as saved:
            archived = json.load(saved.extractfile('manifest.json'))['files']
            config = saved.extractfile('skald.json').read().decode('utf-8') if 'skald.json' in archived else None
        names = tuple(dict.fromkeys((*backup.BACKUP_FILES, *archived)))
        for name in names:
            if name not in archived and os.path.lexists(data/name):
                raise FormatError(f'migration destination {name} is not in the verified backup; restore or remove it first')
        # Only archived state is migrated; credentials and serving caches stay in place.
        with tempfile.TemporaryDirectory(prefix='.skald-migrate-', dir=data) as temp:
            original, staged = Path(temp)/'original', Path(temp)/'staged'
            original.mkdir()
            for name in archived:
                if (data/name).is_file():
                    shutil.copy2(data/name, original/name)
            shutil.copytree(original, staged)
            staged_config = config is not None and not (staged/'skald.json').exists()
            if staged_config:
                write_private(staged/'skald.json', config)
            for step in steps:
                step(staged)
            if staged_config:
                (staged/'skald.json').unlink()
            write_private(staged/'.skald-format', json.dumps(target) + '\n')
            changed = []
            try:
                for name in (*[n for n in names if n != '.skald-format'], '.skald-format'):
                    source, dest, old = staged/name, data/name, original/name
                    if source.exists() and old.exists() and source.read_bytes() == old.read_bytes():
                        continue
                    if source.exists():
                        os.replace(source, dest)
                    elif old.exists():
                        dest.unlink()
                    else:
                        continue
                    changed.append(name)
            except Exception:
                for name in reversed(changed):
                    if (original/name).exists():
                        os.replace(original/name, data/name)
                    else:
                        (data/name).unlink(missing_ok=True)
                raise
    if not (data/'.skald-format').exists() or check(data) != target:
        write_private(data/'.skald-format', json.dumps(target) + '\n')
    return target
