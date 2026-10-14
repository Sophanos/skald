"""Row labels from config and serving caches, without loading the engine."""
import datetime
import json
from pathlib import Path

from . import i18n, members

FIXED = {key: 'row.' + key for key in ('episodes', 'foryou', 'week', 'upcoming', 'catchup', 'rewatch',
         'because', 'because2', 'because3', 'leap', 'digital', 'toprated', 'family', 'newtoyou', 'hbo', 'paramount')}
FIXED.update(airing='row.airing', nextseason='row.nextseason', doctrend='row.docs.trending',
             **{key: 'row.docs.' + key for key in ('wonder', 'cosmos', 'history', 'meaning')})
LANE_TEMPLATE = {'d': 'row.lane.from', 'a': 'row.lane.starring', 'n': 'row.lane.brand', 'c': 'row.lane.from'}


def mapping(value):
    return value if isinstance(value, dict) else {}


def text(value):
    return value if isinstance(value, str) else ''


def ai_title(lane, lang):
    title = text(mapping(mapping(lane).get('titles')).get(lang.lower()))
    return title if 1 <= len(title.split()) <= 4 else ''


def missing(key, lang):
    keys = key.values() if isinstance(key, dict) else [key] if key else []
    out = []
    for key in keys:
        for code in i18n._chain(lang):
            value = i18n._STRINGS.get(code, {}).get(key)
            try:
                valid = value is not None and i18n._fields(value) == i18n._fields(i18n._STRINGS['en'][key])
            except ValueError:
                valid = False
            if code == 'en':
                if lang.lower().split('-')[0] != 'en':
                    out.append(key)
                break
            if valid:
                break
    return out


def describe(cid, names, config, lang, lanes=None):
    names = mapping(names)
    key = cid.removeprefix(members.rid('', config))
    hh, fields, source = config['household'], {}, 'fixed'
    label_key = FIXED.get(key)
    label = text(names.get('title')) or text(names.get('mixed'))
    group = next((group for group, ids in {
        'people': [*hh['member_rows'], *hh['archived']], 'moods': [f'mood{i}' for i in range(1, 5)],
        'lanes': [*[f'lane{i}' for i in range(1, 6)], 'lanebench1', 'lanebench2'],
        'docs': ['wonder', 'cosmos', 'history', 'meaning', 'doctrend', 'docsai'],
        'studios': ['hbo', 'paramount'], 'anime': ['airing', 'nextseason'],
        'rewatch': ['rewatch', 'rewatchai'], 'because': ['because', 'because2', 'because3'],
        'shelves': [key] if key.startswith('shelf_') else []}.items() if key in ids), key)
    custom = text(mapping(hh.get('row_labels')).get(group))
    if custom:
        return dict(id=cid, key=None, label=custom, source='custom', missing=[])
    if key in hh['member_rows'] or key in hh['archived']:
        label_key, source = ('row.member' if key in hh['member_rows'] else 'row.archived'), 'template'
        fields = {'person': hh['labels'].get(key, hh['archived'].get(key, key))}
    elif key in ('top10', 'toprated'):
        label_key = {kind: 'row.' + key + '.' + kind for kind in ('movie', 'series')}
        if key == 'toprated':
            label_key['movie'] = 'row.toprated'
        label = {kind: i18n.t(k, lang) for kind, k in label_key.items()}
    elif text(names.get('label_key')) in i18n._STRINGS['en']:
        label_key, fields, source = names['label_key'], mapping(names.get('fields')), 'template'
        required = i18n._fields(i18n._STRINGS['en'][label_key])
        if not required <= fields.keys() or any(not isinstance(fields[k], str) for k in required):
            return dict(id=cid, key=label_key, label=label or describe(cid, {}, config, lang)['label'],
                        source=source, missing=missing(label_key, lang))
        fields = {k: fields[k] for k in required}
    elif text(names.get('mood')):
        label_key, source = 'row.mood.' + names['mood'], 'fixed'
    elif text(names.get('lane')):
        fid = names['lane']
        source = 'template'
        if fid.startswith('x:'):
            source = 'fixed'
            label_key = 'row.experience.' + fid[2:]
        elif fid[0] in LANE_TEMPLATE:
            label_key = 'row.maker' if names.get('maker') is True else LANE_TEMPLATE[fid[0]]
            fields = {'name': text(names.get('name'))}
            if not fields['name']:
                return dict(id=cid, key=label_key, label=label or i18n.t('row.picked', lang), source=source,
                            missing=missing(label_key, lang))
        else:
            label_key = None
            lane = mapping(mapping(lanes).get(fid))
            title = ai_title(lane, lang)
            label, source = (title, 'ai') if title else (text(lane.get('title')) or label, 'fixed')
    elif label and (key.startswith('mood') or key.startswith('lane')):
        return dict(id=cid, key=None, label=label, source='fixed', missing=[])
    elif key.startswith('mood'):
        label_key = 'row.mood'
    elif key.startswith('lane'):
        label_key = 'row.picked'
    elif key.startswith('because'):
        label_key = 'row.because'
        if label:
            return dict(id=cid, key=label_key, label=label, source='template', missing=missing(label_key, lang))
    elif key in ('leap', 'newtoyou') and label:
        return dict(id=cid, key='row.feeling' if key == 'leap' else 'row.newtoyou.named',
                    label=label, source='template', missing=missing(label_key, lang))
    elif key.startswith('shelf_'):
        return dict(id=cid, key=None, label=label or '', source='custom', missing=[])
    if key == 'nextseason':
        today = datetime.date.today()
        season = (today.month - 1) // 3 + 1
        fields = {'season': i18n.t('season.' + ('winter', 'spring', 'summer', 'fall')[season % 4], lang),
                  'year': today.year + season // 4}
        source = 'template'
    if label_key and not isinstance(label_key, dict):
        label = i18n.t(label_key, lang, **fields)
    return dict(id=cid, key=label_key, label=label or i18n.t('row.picked', lang), source=source,
                missing=missing(label_key, lang))


def read(path):
    try:
        return mapping(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return {}


def view(data, config=None, lang=None):
    config = config or members.load_config(str(data), readonly=True)[0]
    row_lang = config['region'].get('row_lang', config['region']['lang'])
    lang = lang or row_lang
    cache_lang = config['region']['lang'] if 'row_lang' in config['region'] else lang
    cached = {**read(Path(data) / 'feeds.json'), **read(Path(data) / 'shown.json')}
    lanes = read(Path(data) / 'learn.json').get('lanes', {})
    keys = [*FIXED, 'top10', 'because2', 'because3', *[f'mood{i}' for i in range(1, 5)],
            *[f'lane{i}' for i in range(1, 6)], 'lanebench1', 'lanebench2', 'docsai', 'rewatchai',
            *config['household']['member_rows'], *config['household']['archived']]
    wishes = read(Path(data) / 'shelves.json').get('wishes')
    for wish in wishes if isinstance(wishes, list) else []:
        key = f"shelf_{wish['scope']}_{wish['slot']}"
        keys.append(key)
        cached.setdefault(members.rid(key, config) + '|' + cache_lang, {'names': {'title': wish['name'] or wish['text'][:100]}})
    rows = [describe(members.rid(key, config), mapping(cached.get(members.rid(key, config) + '|' + cache_lang) or
                     cached.get(members.rid(key, config) + '|' + config['region']['lang'])).get('names'),
                     config, lang, lanes) for key in dict.fromkeys(keys)]
    from . import lab
    ideas = lab.read(data)['ideas']
    rows.extend(dict(id=members.rid('lab_'+i['id'], config), key=None, label=i['name'], source='ai', missing=[])
                for i in ideas)
    out = {'language': lang, 'lang': config['region']['lang'], 'row_lang': row_lang, 'rows': rows}
    if ideas:
        reason = lab.paused(config)
        out['lab_paused_reason'] = i18n.t('lab.paused' if reason == 'metrics' else 'lab.disabled', lang) if reason else None
    return out
