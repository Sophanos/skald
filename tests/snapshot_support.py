"""Optional snapshot gates use only explicit environment paths."""
import os
from pathlib import Path
import unittest


def snapshot_path(*, updated=False):
    value = os.environ.get('SKALD_SNAPSHOT_U' if updated else 'SKALD_SNAPSHOT', '').strip()
    return Path(value) if value else None


def require_snapshot(*, updated=False):
    path = snapshot_path(updated=updated)
    if path is None or not path.is_dir():
        raise unittest.SkipTest('set ' + ('SKALD_SNAPSHOT_U' if updated else 'SKALD_SNAPSHOT') + ' to run snapshot gates')
    return path


def snapshot_people(path):
    if path is None or not path.is_dir():
        return ('ana', 'ben')
    from skald.members import load_config
    return tuple(load_config(str(path), {}, readonly=True)[0]['household']['members'])
