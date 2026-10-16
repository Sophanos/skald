"""Explicit shared-title retag requests, consumed only by the enrichment worker."""
import json
from pathlib import Path
import uuid

from . import history
from .erase import writable
from .intake import write_private

FILE = 'tag-queue.json'


def pending(s):
    try:
        raw = json.loads((Path(s.DATA)/FILE).read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        raise ValueError(s.t('tags.queue_error')) from None
    if not isinstance(raw, dict) or not isinstance(raw.get('items'), dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in raw['items'].items()):
        raise ValueError(s.t('tags.queue_error'))
    return raw['items']


def save(s, items):
    with writable(s.DATA, s.household_erased) as allowed:
        if allowed:
            write_private(Path(s.DATA)/FILE, json.dumps({'items': items}) + '\n')


def queue(s, title=None, *, stale=False, apply=True):
    if stale == (title is not None):
        raise ValueError(s.t('tags.arguments'))
    with s._facets_lock:
        records = dict(s._facets)
    states = {k: s.tag_state(r) for k, r in records.items()}
    counts = dict.fromkeys(('missing', 'ok', 'partial', 'invalid', 'untaggable', 'manual', 'stale'), 0)
    for state in states.values():
        counts['stale' if state['stale'] else state['status']] += 1
    if stale:
        selected = [k for k, state in states.items() if state['stale']]
    else:
        selected = [k for k, r in records.items() if title.casefold() in (k.casefold(), str(r.get('title', '')).casefold())]
        if len(selected) != 1:
            raise ValueError(s.t('tags.title_error'))
    selected = [k for k in selected if states[k]['status'] != 'manual' and records[k].get('kind') in ('movie', 'tv')]
    if apply and selected:
        with history.locked(s.DATA):
            items = pending(s)
            items.update({k: uuid.uuid4().hex for k in selected})
            save(s, items)
    return {'queued': len(selected), 'counts': counts}


def finished(s, requests):
    if not requests:
        return
    with history.locked(s.DATA):
        items = pending(s)
        save(s, {k: v for k, v in items.items() if requests.get(k) != v})
