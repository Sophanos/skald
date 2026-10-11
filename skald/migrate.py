"""Ordered, idempotent migrations of backed-up household state."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import tarfile

FORMAT = 1
# Keys are destination formats; functions accept a staged DATA path.
MIGRATIONS = {}


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
    from . import backup
    from .intake import write_private
    from .members import VERSION
    data = Path(data)
    current = check(data)['format']
    target = {'format': FORMAT, 'release': VERSION}
    if current < FORMAT:
        steps = [MIGRATIONS[version] for version in range(current + 1, FORMAT + 1)]
        archive = backup.create(data, from_format=current, to_format=FORMAT)
        with tarfile.open(archive) as saved:
            archived = json.load(saved.extractfile('manifest.json'))['files']
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
            for step in steps:
                step(staged)
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
