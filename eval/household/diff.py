"""Private, stdout-only Home changes. Never pass this output to the share exporter."""
import json
from pathlib import Path

from eval.household import snapshot


def changes(a, b):
    old = {(r['id'], r['type']): (i, r) for i, r in enumerate(a, 1)}
    new = {(r['id'], r['type']): (i, r) for i, r in enumerate(b, 1)}
    result = {k: [] for k in ('added', 'removed', 'renamed', 'moved', 'cards_changed')}
    for key, (position, row) in new.items():
        identity = {'id': key[0], 'type': key[1]}
        if key not in old:
            result['added'].append({**identity, 'position': position, 'label': row['label']})
            continue
        before, previous = old[key]
        if previous['label'] != row['label']:
            result['renamed'].append({**identity, 'a': previous['label'], 'b': row['label']})
        if before != position:
            result['moved'].append({**identity, 'a': before, 'b': position})
        if not (previous.get('network_attempts') or row.get('network_attempts')):
            aa, bb = ([m['id'] for m in r['cards']] for r in (previous, row))
            if aa != bb:
                result['cards_changed'].append({**identity, 'new_titles': len(set(bb)-set(aa))})
    result['removed'] = [{'id': key[0], 'type': key[1], 'position': i, 'label': r['label']}
                         for key, (i, r) in old.items() if key not in new]
    return result


def run(args, replay):
    target = Path(args.snapshot)
    doc = snapshot.load(target / 'snapshot.json' if target.is_dir() else target)
    config = doc['inputs']['skald.json']['household']
    members = [p for p in config['members'] if p not in config.get('retired', [])] + ['together']
    if args.member:
        if args.member not in members:
            raise ValueError('INVALID_INPUT')
        members = [args.member]
    request = {'inputs': doc['inputs'], 'members': members, 'origins': [doc['generation']],
               'lang': args.lang, 'row_details': True}
    a, b = replay(args.a, request), replay(args.b, request)
    homes = [{'member': member, 'a': aa['rows'], 'b': bb['rows'],
              'changes': changes(aa['rows'], bb['rows'])} for member, aa, bb in zip(members, a, b)]
    report = {'lang': args.lang, 'homes': homes}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    else:
        for home in homes:
            print(home['member'])
            print('Build | Position | Label | Row id | Type | Cards | First 5 titles')
            for side in ('a', 'b'):
                for row in home[side]:
                    count = row['status'] if row['network_attempts'] else str(row['count'])
                    print(' | '.join([side.upper(), str(row['position']), row['label'], row['id'],
                                      row['type'], count, ', '.join(m['name'] for m in row['cards'][:5])]))
            for kind, rows in home['changes'].items():
                print(kind + ': ' + (json.dumps(rows, ensure_ascii=False) if rows else 'none'))
    return 0
