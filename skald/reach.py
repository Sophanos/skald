"""Known taste ranks against the rows actually selected for Home, without sourcing."""
from . import rowplan


def home(s, person):
    plan = rowplan.plan(s, person, cards=True)
    shown, dropped, pool = {}, {}, set()
    for row in plan['rows']:
        for card in row.get('cards', []):
            pair = (card['key'], card['kind'])
            pool.add(pair)
            if row['position'] is not None and card['removed_by'] is None:
                shown.setdefault(pair, []).append(row['id'])
            else:
                rule = card['removed_by'] or row['rejection']
                dropped.setdefault(pair, 'below floor' if rule == 'quality' else
                                   'filtered' if rule in ('negative', 'seen', 'tone', 'unsafe') else 'capped')
    return shown, dropped, pool


def view(s, person=None, top=20):
    if type(top) is not int or not 1 <= top <= 10000:
        raise ValueError('--top must be between 1 and 10000')
    if person is not None and person not in ('household', 'together', *s.PEOPLE):
        raise ValueError('unknown person')
    p, m = s._profiles.get('household') or {}, s.get_model(False)
    people, ranks = {}, {}
    for who in (None, *s.PEOPLE, 'together'):
        if person is None or person in ('household', 'together') or who == person:
            ranks[who] = s.known_rank(m, p, who)
    for who in (person,) if person else ('household', *s.PEOPLE):
        viewer = None if who == 'household' else who
        ranked = ranks[viewer]
        shown, dropped, pool = home(s, who)
        cid = s.rid(viewer) if viewer in s.PEOPLE else s.rid('foryou')
        with s.addon_scope(None if who == 'household' else who):
            selected = {(x['id'], 'movie' if x['type'] == 'movie' else 'tv')
                        for x in s.catalog_picks(p, m, cid, viewer if viewer in s.PEOPLE else None, ranks=ranks)}
            titles = []
            for rank, (score, _, key, kind) in enumerate(ranked[:top], 1):
                pair = (key, kind)
                rows = shown.get(pair, [])
                reason = None if rows else s.catalog_drop(s.known_meta(key, kind), p, cid, viewer) or dropped.get(pair)
                if not rows and reason is None:
                    reason = 'capped' if pair in pool or pair not in selected else 'not in pool'
                titles.append({'key': key, 'kind': kind, 'title': s.frec(key, kind)['title'], 'rank': rank,
                               'score': score, 'match': s.match_pct(m, score, viewer), 'rows': rows, 'dropped': reason})
        hits = sum(bool(x['rows']) for x in titles)
        people[who] = {'known_unseen': len(ranked), 'hits': hits, 'of': len(titles),
                       'reach': hits/len(titles) if titles else None, 'titles': titles}
    return {'top': top, 'people': people}


def title(s, key, kind):
    p, m = s._profiles.get('household') or {}, s.get_model(False)
    out = {}
    for who in ('household', *s.PEOPLE):
        viewer = None if who == 'household' else who
        ranked = s.known_rank(m, p, viewer)
        rank = next((i for i, (_, _, k, typ) in enumerate(ranked, 1) if k == key and (kind is None or typ == kind)), None)
        shown, _, _ = home(s, who)
        rows = list(dict.fromkeys(cid for (k, typ), cids in shown.items() if k == key and (kind is None or typ == kind) for cid in cids))
        out[who] = {'known_rank': rank, 'known_unseen': len(ranked), 'home': {'present': bool(rows), 'rows': rows}}
    return out


def text(result):
    return '\n'.join(f"{who}: {p['hits']} of {p['of']} best unseen known matches on Home."
                     for who, p in result['people'].items())
