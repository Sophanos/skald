"""Optional Row Lab ideas; catalog identities and names never change."""
import copy
import json
from pathlib import Path
import time

from . import i18n, metrics
from .intake import write_private
from .members import rid

DEFAULTS = dict(enabled=True, trial_showing_days=10, kept_showing_days=30)
CONTROL = ('state', 'retirement', 'manual_keep', 'reason', 'retired_at', 'retired_from', 'baseline')


def settings(config):
    return {**DEFAULTS, **config.get('lab', {})}


def read(data):
    try:
        doc = json.loads((Path(data)/'lab.json').read_text())
    except FileNotFoundError:
        return {'ideas': [], 'attempt_day': None}
    if not isinstance(doc, dict) or not isinstance(doc.get('ideas'), list):
        raise ValueError('invalid local row lab')
    return doc


def save(data, doc):
    write_private(Path(data)/'lab.json', json.dumps(doc, ensure_ascii=False, allow_nan=False) + '\n')


def paused(config):
    return 'metrics' if not metrics.settings(config)['enabled'] else 'off' if not settings(config)['enabled'] else None


def sync(data, config, now=None, doc=None):
    now = time.time() if now is None else now
    doc = copy.deepcopy(read(data) if doc is None else doc)
    if paused(config):
        return doc
    with metrics._lock:
        store = metrics._totals(metrics._state(data, config, now))
    for idea in doc['ideas']:
        cid = rid('lab_'+idea['id'], config)
        cells = [cell for rows in store['weeks'].values() for cell in rows.get(cid, {}).values()]
        if not cells:
            continue
        complete = all(c.get('complete') and not c.get('extra', {}).get('restored') for c in cells)
        valid = [c.get('extra', {}) for c in cells if c.get('complete') and not c.get('extra', {}).get('restored')]
        showings = {t: c for extra in valid for t, c in extra.get('showings', {}).items()}
        plays = {credit: c for extra in valid for credit, c in extra.get('play_contexts', {}).items()}
        last = max((c['at'] for c in plays.values()), default=None)
        state = 'kept' if plays or idea.get('manual_keep') or idea['state'] == 'kept' or idea.get('retired_from') == 'kept' else 'trial'
        baseline = max(idea.get('created', 0), idea.get('manual_keep', 0), idea.get('baseline', 0), last or 0)
        inactive = {c['date'] for c in showings.values() if c['at'] > baseline or state == 'trial' and c['at'] == baseline}
        idea.update(moments=sorted({c['moment'] for c in plays.values()}), plays=len(plays), last_play=last,
                    showing_days=len({c['date'] for c in showings.values()}), inactivity_days=len(inactive),
                    baseline=baseline, evidence_complete=complete)
        if idea.get('retirement') == 'manual':
            idea.update(state='retired', reason='manual_retire')
            continue
        reason = 'manual_keep' if idea.get('manual_keep', 0) > (last or 0) else 'play' if plays else state
        idea.update(state=state, reason=reason)
        idea.pop('retirement', None)
        limit = settings(config)['kept_showing_days' if state == 'kept' else 'trial_showing_days']
        if len(inactive) >= limit:
            idea.update(state='retired', reason='inactive', retirement='automatic', retired_from=state)
            idea.setdefault('retired_at', now)
        else:
            idea.pop('retired_at', None)
            idea.pop('retired_from', None)
    return doc


def select(data, config, now=None, person=None, doc=None):
    now = time.time() if now is None else now
    if paused(config) or person == 'guest' or person in config['household']['members'] and config['taste'].get(
            'people', {}).get(person, {}).get('prefs', {}).get('kids_age') is not None:
        return []
    bucket, month = metrics.moment(config, now)
    ideas = (read(data) if doc is None else doc)['ideas']
    eligible = [i for i in ideas if i['state'] != 'retired' and
                (i.get('months', 'any') == 'any' or month in i['months']) and
                (person not in (*config['household']['members'], 'together') or person in i['members'] or 'together' in i['members']) and
                (i['state'] == 'trial' or not i.get('moments') or bucket in i['moments'])]
    trials = [i for i in eligible if i['state'] == 'trial'][:2]
    return trials + [i for i in eligible if i['state'] == 'kept'][:3-len(trials)]


def view(data, config, now=None):
    now = time.time() if now is None else now
    lang = config['region']['lang']
    doc = sync(data, config, now)
    reason = paused(config)
    bucket, month = metrics.moment(config, now)
    return {**doc, 'enabled': settings(config)['enabled'], 'moment': bucket, 'month': month,
            'paused_reason': i18n.t('lab.paused' if reason == 'metrics' else 'lab.disabled', lang) if reason else None,
            'ideas': [{'showing_days': 0, 'inactivity_days': 0, 'plays': 0, **i, 'catalog_id': rid('lab_'+i['id'], config),
                       'state_label': i18n.t('lab.'+i['state'], lang),
                       'why': i18n.t('lab.reason.'+i.get('reason', 'trial'), lang)} for i in doc['ideas']]}


def preview(data, config, op, identity=None, now=None):
    if op in ('on', 'off'):
        before = copy.deepcopy(config.get('lab'))
        return dict(id=None, before=before, after={**(before or {}), 'enabled': op == 'on'})
    hits = [i for i in read(data)['ideas'] if identity in (i['id'], rid('lab_'+i['id'], config))
            or isinstance(identity, str) and identity.casefold() == i['name'].casefold()]
    if len(hits) != 1:
        raise LookupError(i18n.t('lab.ask', config['region']['lang']))
    idea = hits[0]
    if op == 'keep' and idea.get('retirement') == 'manual':
        raise ValueError(i18n.t('lab.final', config['region']['lang']))
    before = {k: copy.deepcopy(idea.get(k)) for k in CONTROL}
    after = {**before, 'state': 'kept' if op == 'keep' else 'retired', 'reason': 'manual_'+op,
             'retirement': None if op == 'keep' else 'manual', 'retired_at': None if op == 'keep' else 'now'}
    if op == 'keep':
        after.update(manual_keep='now', baseline='now')
    return dict(id=idea['id'], before=before, after=after, name=idea['name'])


def apply(data, config, change):
    from .history import locked
    with locked(data):
        if change['id'] is None:
            current = config.get('lab')
        else:
            doc = read(data)
            idea = next((i for i in doc['ideas'] if i['id'] == change['id']), None)
            current = {k: idea.get(k) for k in CONTROL} if idea else None
        if current != change['before']:
            raise ValueError(i18n.t('lab.changed', config['region']['lang']))
        if change['id'] is not None:
            now = time.time()
            for key in ('manual_keep', 'retired_at', 'baseline'):
                if change['after'].get(key) == 'now':
                    change['after'][key] = now
        if change['id'] is None:
            config = copy.deepcopy(config)
            config.pop('lab', None)
            if change['after'] is not None:
                config['lab'] = change['after']
        else:
            for k, v in change['after'].items():
                idea.pop(k, None)
                if v is not None:
                    idea[k] = v
            save(data, doc)
        return config


def text(result, lang='en'):
    yield i18n.t('lab.title', lang)
    if result['paused_reason']:
        yield result['paused_reason']
    for idea in result['ideas']:
        yield f"{idea['id']}  {idea['name']}: {idea['state_label']}; {idea['why']}"
    if not result['ideas']:
        yield i18n.t('lab.empty', lang)
