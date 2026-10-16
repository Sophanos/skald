"""Dated release marathons, shared by Home and read-only agent answers."""


def member(s, release, entries, explicit):
    if release['kind'] == 'movie':
        positive = {e['key'] for e in entries if e['kind'] == 'movie' and e['weight'] > 0 and (s.real_play(e) or e['signal'] == 'loved')}
        watched = {e['key'] for e in entries if e['kind'] == 'movie' and s.real_play(e)}
        seen = [x['id'] for x in release['parts'] if x['id'] in watched]
        reason = 'adult' if release['adult'] else 'no_earlier_parts' if not release['parts'] else \
                 'saga' if release['collection'] in explicit else 'watched_part' if any(x['id'] in positive for x in release['parts']) else 'no_interest'
        return dict(reason=reason, seen=seen)
    watched = [e for e in entries if e['kind'] == 'tv' and e['key'] == release['key'] and e['weight'] > 0 and s.real_play(e)]
    last = max((tuple(e['item'].get('last_episode') or ()) for e in watched), default=())
    count = (release['meta']['_row'].get('counts') or {}).get(str(last[0]), 0) if len(last) == 2 else 0
    valid = len(last) == 2 and count > 0 and 0 < last[1] <= count
    remaining = [x['season_number'] for x in release['seasons'] if valid and
                 (x['season_number'] > last[0] or x['season_number'] == last[0] and last[1] < count)]
    reason = 'adult' if release['adult'] else 'not_watched' if not watched else \
             'unknown_progress' if not valid else 'catch_up' if remaining else 'caught_up'
    return dict(reason=reason, seasons=remaining)


def build(s, lang):
    p = s._profiles.get('household') or {'entries': []}
    today = s.local_now().date()
    people = {who: s.person_entries(p, who) for who in s.member_people()}
    explicit = set((s.read_taste().get('household') or {}).get('rewatch_collections') or [])
    names = {kind: s.genres(kind, lang) for kind in ('movie', 'tv')}
    releases = []
    for cid in sorted(s.movie_collections(p, cache_only=True)):
        try:
            col, en = (s.tmdb(f'collection/{cid}', language=l, _cache_only=True) for l in (lang, 'en-US'))
        except Exception:
            releases.append(dict(key=f'collection:{cid}', kind='movie', title='', date=None, parts=[],
                                 people={who: {'reason': 'lookup_failed'} for who in people}))
            continue
        if not col or not en:
            continue
        parts = sorted((x for x in col.get('parts') or [] if s.date_or_none(x.get('release_date'))),
                       key=lambda x: (x['release_date'], x['id']))
        art = {x['id']: x for x in en.get('parts') or []}
        for target in col.get('parts') or []:
            if not s.date_or_none(target.get('release_date')):
                releases.append(dict(key=f"tmdb:{target['id']}", kind='movie', title=target.get('title') or '', date=None, parts=[],
                                     people={who: {'reason': 'undated'} for who in people}))
        targets = [x for x in parts if (s.date_or_none(x['release_date']) - today).days >= -7] or parts[-1:]
        for target in targets:
            earlier = [x for x in parts if x['release_date'] < target['release_date'] and x['release_date'] <= today.isoformat()]
            cards = []
            for i, x in enumerate(earlier, 1):
                if x.get('adult') or art.get(x['id'], {}).get('adult'):
                    continue
                meta = s.row_meta(x, 'movie', 'movie', names['movie'], art.get(x['id'], {}), collection=cid, lang=lang)
                meta['_row'].update(ready_part=i, ready_count=len(earlier))
                cards.append(meta)
            title = target.get('title') or art.get(target['id'], {}).get('title') or ''
            releases.append(dict(key=f"tmdb:{target['id']}", kind='movie', title=title, date=target['release_date'], collection=cid,
                                 adult=bool(target.get('adult') or art.get(target['id'], {}).get('adult')),
                                 meta=s.row_meta(target, 'movie', 'movie', names['movie'], art.get(target['id'], {}), collection=cid, lang=lang),
                                 aliases=[col.get('name', ''), s.saga_name(col, en), title, art.get(target['id'], {}).get('title', ''),
                                          *[x.get('title', '') for x in earlier]], parts=cards))
    shows = list(dict.fromkeys(e['id'] for e in p['entries'] if e['kind'] == 'tv' and e['weight'] > 0
                              and e['signal'] in ('watched', 'liked', 'loved')))[:120]
    for tid in shows:
        try:
            det, en = s.details_pair('tv', tid, lang, cache_only=True)
        except Exception:
            releases.append(dict(key=f'tmdb:{tid}', kind='tv', title='', date=None, parts=[],
                                 people={who: {'reason': 'lookup_failed'} for who in people}))
            continue
        if not det or not en:
            continue
        seasons = sorted((x for x in en.get('seasons') or [] if x.get('season_number', 0) > 0), key=lambda x: x['season_number'])
        dated = [x for x in seasons if x['season_number'] > 1 and s.date_or_none(x.get('air_date'))]
        targets = [x for x in dated if (s.date_or_none(x['air_date']) - today).days >= -7] or dated[-1:]
        for target in targets:
            prior = [x for x in seasons if x['season_number'] < target['season_number']]
            meta = s.row_meta(det, 'series', 'tv', names['tv'], en, lang=lang)
            releases.append(dict(key=f'tmdb:{tid}', kind='tv', title=det.get('name') or en.get('name') or '', date=target['air_date'],
                                 adult=bool(det.get('adult') or en.get('adult')),
                                 season=target['season_number'], seasons=prior, aliases=[det.get('name', ''), en.get('name', '')],
                                 meta=meta, parts=[meta]))
    for release in releases:
        if 'meta' in release:
            release['people'] = {who: member(s, release, entries, explicit) for who, entries in people.items()}
    return {s.rid('ready1'): {'releases': releases}, s.rid('ready2'): {}}


def choices(s, lang, who, masks):
    feed = s.feed(s.rid('ready1'), lang)
    source = (feed.get('metas') or {}).get('releases') or []
    if not source:
        return []
    p = s._profiles.get('household') or {'entries': []}
    known = {e['key'] for e in p['entries']}
    profiles = {}
    for person in s.member_people():
        entries = s.person_entries(p, person)
        profiles[person] = {**p, 'entries': entries, 'negative': set(p.get('negative', ())) - known |
                           {e['key'] for e in entries if e['weight'] < 0}}
    p = profiles.get(who, p)
    explicit = set((s.read_taste().get('household') or {}).get('rewatch_collections') or [])
    blocked = masks[0] | (masks[2] if len(masks) > 2 else set())
    out = []
    for release in source:
        date = s.date_or_none(release.get('date'))
        in_window = date is not None and -7 <= (date - s.local_now().date()).days <= 42
        people = {person: member(s, release, profile['entries'], explicit) for person, profile in profiles.items()} if 'meta' in release else \
                 {person: dict(info) for person, info in release['people'].items() if person in profiles}
        def allowed(x, subject, profile, hidden):
            held = subject in profiles and any(e['key'] == x['id'] and e['kind'] == ('movie' if x['type'] == 'movie' else 'tv')
                                               and e.get('abandon') == 'early' and s._abandon_seen(e)
                                               for e in profile['entries'])
            return not held and x['id'] not in hidden and not s.row_member(x, profile, 'negative') and s.preference_ok(x, s.rid('ready1'), subject)
        own_cards = {}
        for person, info in people.items():
            if not in_window:
                info['reason'] = 'outside_window' if date else info.get('reason', 'undated')
            elif info['reason'] in ('saga', 'watched_part', 'catch_up'):
                with s.addon_scope(person):
                    hidden = s.row_masks(person)
                    hidden = hidden[0] | (hidden[2] if len(hidden) > 2 else set())
                    own_cards[person] = [x for x in release['parts'] if allowed(x, person, profiles[person], hidden)]
                    if not own_cards[person] or not allowed(release['meta'], person, profiles[person], hidden):
                        info['reason'] = 'filtered'
        eligible = [person for person, info in people.items() if info['reason'] in ('saga', 'watched_part', 'catch_up')]
        permitted = {x['id'] for person in eligible for x in own_cards[person]}
        cards = [x for x in release['parts'] if x['id'] in permitted and allowed(x, who, p, blocked)] if who != s.GUEST else []
        if cards and not allowed(release['meta'], who, p, blocked):
            cards = []
        qualifies = bool(cards and eligible and (who not in s.PEOPLE or who in eligible))
        out.append({**release, 'people': people, 'for': eligible, 'qualifies': qualifies, 'selected': False, 'cards': cards})
    out.sort(key=lambda x: (x.get('date') or '9999', x['key']))
    slots = (feed.get('ready_slots') or {}).get(who, [[r['key'], r.get('season')] for r in out if r['qualifies']][:2])
    for release in out:
        release['selected'] = [release['key'], release.get('season')] in slots
    return out


def slot(s, cid, lang, who, masks=None):
    rows = [r for r in choices(s, lang, who, s.row_masks(who) if masks is None else masks) if r['selected']]
    i = s.READY_ROWS.index(cid)
    return rows[i] if i < len(rows) and rows[i]['qualifies'] else None


def label(s, release):
    if not release:
        return s.t('row.ready')
    date = s.day_text(s.date_or_none(release['date']))
    what = s.t('ready.cinemas', title=release['title'], date=date) if release['kind'] == 'movie' else \
           s.t('ready.season', title=release['title'], season=release['season'], date=date)
    return s.t('row.ready.named', title=what)


def cards(s, release, who):
    if not release or not release['qualifies']:
        return []
    people = [who] if who in s.PEOPLE else release['for']
    seen = {key for person in people for key in release['people'][person].get('seen', [])}
    count = max((len(release['people'][person].get('seasons', [])) for person in people), default=0)
    out = []
    for i, meta in enumerate(release['parts'], 1):
        if meta not in release['cards']:
            continue
        facts = meta.get('_row') or {}
        note = s.t('ready.part', part=facts.get('ready_part', i), count=facts.get('ready_count', len(release['parts'])), seen=s.t('ready.seen' if meta['id'] in seen else 'ready.unseen')) if release['kind'] == 'movie' else \
               s.t('ready.catchup.one' if count == 1 else 'ready.catchup', season=release['season'], date=s.day_text(s.date_or_none(release['date'])), count=count)
        out.append({**meta, '_row': {**facts, 'ready_note': note},
                    'description': ' · '.join(x for x in (note, facts.get('overview')) if x)})
    return out


def project(release, viewer):
    return {**release, 'people': {p: info for p, info in release['people'].items() if p == viewer},
            'for': [p for p in release['for'] if p == viewer],
            **({'parts': [{**part, 'seen_by': [p for p in part['seen_by'] if p == viewer]}
                         for part in release['parts']]} if 'parts' in release else {})}


def view(s, person=None, lang=None, title=None, *, viewer=None):
    viewer = viewer or person
    person = 'household' if person is None else 'together' if person == 'both' else person
    if person not in ('household', 'together', 'guest', *s.PEOPLE):
        raise ValueError('unknown person')
    lang = lang or s.LANG
    if not s.LANG_RE.fullmatch(lang):
        raise ValueError('invalid language')
    subject = viewer if viewer in (*s.PEOPLE, s.GUEST) else person
    scope = None if subject in ('household', 'guest') else subject
    with s.addon_scope(scope):
        who = s.GUEST if subject == 'guest' else s.SCOPES[scope] if scope else s.who_watching()[0]
        releases = choices(s, lang, who, s.row_masks(who))
        selected = list(zip(s.READY_ROWS, (r for r in releases if r['selected'])))
        if title:
            releases = [r for r in releases if title.casefold() in {a.casefold() for a in r.get('aliases', [])}]
            selected = [(cid, r) for cid, r in selected if r in releases]
        enabled = any(s.row_enabled(cid) for cid in s.READY_ROWS)
        if not enabled:
            cause = 'disabled'
        elif any(r['qualifies'] and s.row_enabled(cid) for cid, r in selected):
            cause = None
        elif not releases:
            cause = 'no_releases'
        elif any(r['qualifies'] for _, r in selected):
            cause = 'disabled'
        elif any(r['qualifies'] for r in releases):
            cause = 'not_selected'
        else:
            reasons = [info['reason'] for r in releases for p, info in r['people'].items()
                       if viewer is None or p == viewer or info['reason'] in ('outside_window', 'undated', 'lookup_failed')]
            cause = next((reason for reason in reasons if reason not in ('saga', 'watched_part', 'catch_up')),
                         'filtered' if who == s.GUEST or viewer in s.PEOPLE else 'no_qualifying_release')
        if s.kids_age(s.safety_subject(who)) is not None:
            releases = [r for r in releases if not r.get('adult') and 'meta' in r and s.child_ok(r['meta'], who)]
        rows = [{**{k: v for k, v in r.items() if k not in ('aliases', 'parts', 'cards', 'meta')},
                 'label': label(s, r) if r.get('date') else s.t('row.ready'),
                 'parts': [dict(key=x['id'], title=x['name'], part=(x.get('_row') or {}).get('ready_part', i),
                                seen_by=[p for p, info in r['people'].items() if x['id'] in info.get('seen', [])])
                           for i, x in enumerate(cards(s, r, who), 1)]} for r in releases]
        return dict(person=person, lang=lang, built=s.feed(s.rid('ready1'), lang).get('built'), enabled=enabled,
                    cause=cause, message=s.t('ready.empty.'+cause) if cause else None,
                    releases=[project(r, viewer) for r in rows] if viewer else rows)


def text(data):
    if data.get('cause'):
        return data['message']
    lines = []
    for release in data['releases']:
        lines.append(release['label'] + ': ' + ', '.join(release['for']))
        lines.extend('  ' + part['title'] for part in release['parts'])
        lines.extend('  ' + person + ': ' + info['reason'] for person, info in release['people'].items())
    return '\n'.join(lines) or 'No get-ready releases saved.'
