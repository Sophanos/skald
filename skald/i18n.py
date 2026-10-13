"""Bundled fixed interface text, with English as the final fallback."""
import json
from pathlib import Path
from string import Formatter

_REGION_PARENTS = {
    'zh-cn': ('zh-hans',), 'zh-sg': ('zh-hans',),
    'zh-tw': ('zh-hant',), 'zh-hk': ('zh-hant',), 'es-mx': ('es-419', 'es'),
}
_STRINGS = {path.stem.lower(): json.loads(path.read_text(encoding='utf-8'))
            for path in (Path(__file__).parent / 'locales').glob('*.json')}


def _chain(lang):
    lang = lang.lower()
    if lang.split('-')[0] == 'sh':
        lang = 'sr-latn' + lang[2:]
    base, *subtags = lang.split('-')
    script = next((s for s in subtags if len(s) == 4 and s.isalpha()), None)
    chain = [lang]
    if base == 'sr' and script is None:
        chain = ([lang] if subtags else []) + ['sr-cyrl', 'sr']
    elif script:
        chain.append(base + '-' + script)
        if base == 'sr' and script == 'cyrl':
            chain.append('sr')
    else:
        chain.extend(_REGION_PARENTS.get(lang, (base,)))
    return dict.fromkeys([*chain, 'en'])


def _fields(template):
    fields = set()
    for _, name, spec, _ in Formatter().parse(template):
        if name is not None:
            fields.add(name)
            fields.update(_fields(spec))
    return fields


def t(key, lang='en', **fields):
    """Unknown keys return their key; incomplete formatting returns the English template."""
    english = _STRINGS.get('en', {}).get(key, key)
    names = _fields(english)
    for code in _chain(lang):
        template = _STRINGS.get(code, {}).get(key)
        if template is None:
            continue
        try:
            if _fields(template) != names:
                continue
        except ValueError:
            continue
        try:
            return template.format(**fields)
        except (KeyError, IndexError, ValueError, AttributeError):
            return english
    return english
