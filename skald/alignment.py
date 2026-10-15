"""Read recommendation drivers and offer existing correction previews."""
from collections import Counter
import math

from .engine import facet_view
from .i18n import t

DOMINANT_SHARE = .5


def view(s, profile, model, person=None, limit=10, *, viewer=None):
    if viewer is not None:
        from .plays import read_person
        person = read_person(s, viewer, person)
    if person in ('household', 'together'):
        person = None
    if person is not None and person not in s.PEOPLE:
        raise ValueError('unknown person')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('--limit must be between 1 and 100')
    entries = s.person_entries(profile, person) if person else s.taste_entries(s.adult_profile(profile)['entries'])
    p = {**profile, 'entries': entries} if person else s.adult_profile(profile)
    # Keep scoped weights when because_seeds applies the adult filter again.
    because_profile = {**p, 'entries': [{k: v for k, v in e.items() if k not in ('opinions', 'shared')}
                                      for e in entries]} if person else p
    selected = [('top_seeds', s.top_seeds(p, 8)), ('foryou', s.pool_seeds(p)),
                ('because', s.because_seeds(because_profile))]
    weights = Counter()
    grouped = {}
    for e in entries:
        weights[e['key'], e['kind']] += max(0, e['weight'])
        if e['weight'] > 0:
            grouped.setdefault((e['key'], e['kind']), []).append(e)
    total = sum(weights.values())
    drivers = {}
    for row, seeds in selected:
        for e in seeds:
            key = e['key'], e['kind']
            grouped.setdefault(key, [e])
            if key not in drivers:
                flags, sources = {}, []
                for contribution in grouped[key]:
                    flags.update(dict.fromkeys(contribution.get('flags', ()), True))
                    for value in (contribution.get('item') or {}, contribution):
                        for flag in ('backfilled', 'favorite'):
                            if flag in value:
                                flags[flag] = flags.get(flag, False) or value[flag]
                    source = contribution.get('wsrc') or contribution.get('who')
                    srcs = [f for f in contribution.get('flags', ()) if f in s.IMPORT_TRUST] if source == 'import' else [source]
                    sources.extend(src for src in (srcs or [source]) if src not in sources)
                drivers[key] = dict(key=e['key'], kind=e['kind'], title=e['title'], weight=e['weight'],
                    age_days=round(e['age'], 2) if math.isfinite(e['age']) else None,
                    sources=sources, flags=flags, rows=[], share=round(weights[key]/total, 4) if total else 0)
            if row not in drivers[key]['rows']:
                drivers[key]['rows'].append(row)
    aff = (model or {}).get('paff', {}).get(person, {}) if person else (model or {}).get('aff', {})
    taste = {}
    for name, types in (('genres', 'g'), ('themes', 'kt'), ('makers', 'dancs'), ('feelings', 'x')):
        facets = facet_view(model, person, types, limit) if model else []
        denominator = sum(max(0, v) for fid, v in aff.items() if fid[0] in types)
        taste[name] = [{**f, 'share': round(max(0, aff[f['id']])/denominator, 4) if denominator else 0} for f in facets]
    ignored = {(r['key'], r.get('who')) for r in s.title_fixes() if r.get('op') == 'ignore'}
    scopes = [person] if person else s.adult_people()
    personal = {who: s.person_entries(profile, who) for who in s.PEOPLE}
    checks = []
    for key, relevant in grouped.items():
        driver = drivers.get(key) or dict(title=relevant[0]['title'], share=round(weights[key]/total, 4))
        signals = []
        if key in drivers and all('backfill_only' in e.get('flags', ()) or
                                 ((e.get('item') or {}).get('backfilled') and not (e.get('item') or {}).get('at')) for e in relevant):
            signals.append('backfill_only')
        if key in drivers and any(not math.isfinite(e['age']) or 'undated' in e.get('flags', ()) or
               (e['age'] == 999 and not (e.get('item') or {}).get('at') and
                (e.get('who') in ('import', 'owner') or e.get('wsrc') in ('import', 'trakt', 'sync:pmdb')))
               for e in relevant):
            signals.append('undated')
        if total and weights[key]/total >= DOMINANT_SHARE:
            signals.append('dominant')
        positive = [who for who in scopes if any((e['key'], e['kind']) == key and e['weight'] > 0 for e in personal[who])]
        if positive and any((e['key'], e['kind']) == key and (e['weight'] < 0 or e['signal'] == 'disliked')
                            for who in s.PEOPLE if who not in positive for e in personal[who]):
            signals.append('member_conflict')
        if all(e.get('who') in ('tv', 'both') and not (e.get('opinions', {}).get(person) if person else e.get('opinions')) and
                            e.get('wsrc') in (None, 'tv') and 'told' not in e.get('flags', ()) and
                            'remote' not in e.get('flags', ()) for e in relevant):
            signals.append('shared_only')
        if key in drivers and any((key[0], who) in ignored for who in scopes):
            driver['flags']['learning_ignored'] = True
            signals.append('ignored_seeding')
        fixes = []
        for who in positive:
            own = [e for e in personal[who] if (e['key'], e['kind']) == key and e['weight'] > 0]
            op = 'ignore_learning' if all(e['signal'] == 'watched' for e in own) else 'dislike'
            fix = {'op': dict(op=op, kind=key[1], values=[driver['title']], person=who)}
            fixes.append(fix)
        checks.extend(dict(signal=signal, key=key[0], kind=key[1], title=driver['title'],
                           share=driver['share'], fixes=fixes) for signal in signals)
    if viewer is not None and viewer != person:
        for check in checks:
            check['fixes'] = []
    return dict(person=person, drivers=list(drivers.values())[:limit], taste=taste, check=checks)


def text(data, language='en'):
    import json
    lines = [t('align.drivers', language)]
    for d in data['drivers']:
        age = str(d['age_days']) if d['age_days'] is not None else '?'
        lines.append(f"  {d['title']}: {d['weight']:.2f}, {d['share']:.0%}, {age} " + t('align.days', language) +
                     '; '+', '.join(d['sources']) + ('; '+', '.join(d['flags']) if d['flags'] else ''))
    for name, facets in data['taste'].items():
        if facets:
            lines.append(t('align.'+name, language)+': '+', '.join(f"{f['name']} {f['share']:.0%}" for f in facets))
    lines.append(t('align.check', language))
    for c in data['check']:
        lines.append('  '+c['title']+': '+t('align.'+c['signal'], language))
        for fix in c['fixes']:
            lines.append('    '+json.dumps(fix['op'], ensure_ascii=False))
    if not data['check']:
        lines.append(t('align.none', language))
    return '\n'.join(lines)
