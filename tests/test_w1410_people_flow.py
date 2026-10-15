"""One offline household journey through the 1.4.10 people controls."""
import copy
import datetime as dt
import json
from unittest.mock import patch

from decide_kit import ReplayLLM
from test_w92_commands import Household
from skald import commands as c, history, leave, metrics, setup, shelves


class PeopleFlow(Household):
    def test_people_expiry_forget_and_named_budget_undo(self):
        s = self.s
        now = dt.datetime(2026, 10, 15, 12)
        self.enterContext(patch.object(s, 'local_now', side_effect=lambda ts=None: now))
        self.enterContext(patch.object(s.time, 'time', side_effect=lambda: now.replace(tzinfo=dt.timezone.utc).timestamp()))

        def change(op, **fields):
            plan = c.preview(s, '', 'ana', answer=dict(op=op, **fields))
            if plan.get('forget'):
                plan['source'] = 'cli'
            c.apply(s, plan)
            return plan

        def save(name, doc):
            (self.root / name).write_text(json.dumps(doc))

        before = c.config(s)
        with ReplayLLM(s) as llm:
            llm.arm({'choices': [{'message': {'content': json.dumps(dict(
                op='member_create', person='mia', label='Mia', role='child'))}}]})
            card = c.special(s, 'Add my child Mia', 'movie', routed=True)['metas'][0]
            self.assertEqual(card['name'], 'How old is Mia?')
            self.assertTrue(c.get_pending(s, 'w92')['ask'])
            self.assertEqual(c.config(s), before)
            llm.arm({'choices': [{'message': {'content': json.dumps(dict(
                op='member_create', person='mia', label='Mia', role='child', age=12))}}]})
            c.special(s, '12', 'movie', routed=False)
            self.assertIn('How old is Mia?', llm.bodies[-1]['messages'][1]['content'])
            c.apply(s, c.get_pending(s, 'w92'))
            self.assertEqual(llm.calls, 2)
        self.assertEqual(s.kids_age('mia'), 12)

        change('member_create', person='kim', label='Kim', guest=True, days=3)
        change('shelf_add', values=['invented heists', 'always'], with_person='kim', days=3)
        wish = shelves.read(s)['wishes'][0]
        self.assertEqual((wish['scope'], wish['with_person'], wish['until']), ('ana', 'kim', '2026-10-18'))
        with s.addon_scope('ana'):
            self.assertTrue(s.shelf_rows())
        self.assertEqual(c.config(s)['household']['guest_until'], {'kim': '2026-10-18'})
        original_budget = s.ai_monthly_usd()
        change('ai_budget', values=[5])
        budget_id = c.journal(s)['ana'][-1]['id']
        listed = c.preview(s, '', 'ana', answer=dict(op='history', person='household', since='7d'))
        self.assertTrue(listed['read_only'])
        self.assertEqual(len(listed['changes']), 4)
        self.assertIn(budget_id, [row['id'] for row in listed['changes']])
        self.assertEqual(c.config(s)['household']['ai_monthly_usd'], 5)

        now = dt.datetime(2026, 10, 18, 23, 59, 59)
        c.expire(s)
        self.assertIn('kim', s.PEOPLE)
        now += dt.timedelta(seconds=1)
        c.expire(s)
        self.assertNotIn('kim', s.PEOPLE)
        self.assertIn('kim', c.config(s)['household']['retired'])
        self.assertEqual(shelves.read(s)['wishes'], [])
        expired = [e for e in c.journal(s)['ana'] if e.get('expired')]
        self.assertEqual(len(expired), 2)
        self.assertTrue(all(e['source'] == 'expiry' for e in expired))

        with self.assertRaisesRegex(PermissionError, 'adult household member'):
            change('member_away', person='mia', until='2026-10-20')
        change('member_away', person='ben', until='2026-10-20')
        self.assertEqual(c.config(s)['household']['away_until'], {'ben': '2026-10-20'})
        now = dt.datetime(2026, 10, 20, 23, 59, 59)
        c.expire(s)
        self.assertIn('ben', c.config(s)['household']['away'])
        now = dt.datetime(2026, 10, 21)
        c.expire(s)
        self.assertNotIn('ben', c.config(s)['household'].get('away', []))
        self.assertNotIn('ben', c.config(s)['household'].get('away_until', {}))
        returned = c.journal(s)['ana'][-1]
        self.assertEqual((returned['change']['op'], returned['source'], returned['expired']),
                         ('member_back', 'expiry', True))
        self.assertEqual(s.kids_age('mia'), 12)

        cfg = c.config(s)
        cfg['metrics'] = {'enabled': True}
        cfg['connectors']['mdblist'] = dict(kind='api', enabled=False, auth=None,
            accounts={'ben': 'file:synthetic-ben.json', 'ana': 'file:synthetic-ana.json'}, options={})
        setup.publish(self.root, cfg, replace=True)
        s.refresh_preferences()
        (self.root / 'credentials').mkdir()
        for who in ('ana', 'ben'):
            save('credentials/synthetic-'+who+'.json', {'synthetic': True})
            s.write_feedback(dict(who=who, key='tmdb:990001', kind='movie', title='Invented Lantern', action='love'))
            s.write_imports([dict(who=who, key='tmdb:990002', kind='movie', title='Invented Meadow', action='like', src='plain', conf=1)])
            change('shelf_add', person=who, values=['invented meadows', 'always'])
        change('shelf_add', values=['invented lanterns', 'always'], with_person='ben')
        items = [dict(id='tmdb:'+str(key), type='movie', title='Invented Watch', played=True, at=1000,
                      history_by=people) for key, people in ((990003, ['ben']), (990004, ['ana']), (990005, ['ana', 'ben']))]
        stores = {
            'history.json': {'items': items},
            history.SUMMARY: {'v': 1, 'items': [dict(origin='history.json', key='tmdb:990003', kind='movie',
                who='ben', by=['ben'], month='2026-01', src='tv')]},
            'mdblist-ben.history.json': {'source': 'mdblist', 'items': [], 'watched': ['tmdb:990003']},
            'connectors.json': {'version': 1, 'connectors': {'manual:plain:ben': {'person': 'ben'},
                'manual:plain:ana': {'person': 'ana'}}},
            'clients.json': {'clients': [{'id': 'synthetic', 'person': 'ben'}]},
            'interview.json': [{'who': 'ben', 'key': 'tmdb:990003'}],
            'archetypes.json': {'items': {'ben': {'name': 'Invented Own'}, 'ana': {'name': 'Invented Other'}}},
            'lab.json': {'ideas': [dict(id='own', members=['ben']), dict(id='shared', members=['ana', 'ben'])]},
            'taste.json': {'people': copy.deepcopy(cfg['taste']['people'])},
            'title_fixes.json': {'fixes': [{'who': 'ben', 'key': 'tmdb:990003'}]},
        }
        for name, doc in stores.items():
            save(name, doc)
        memory = s._memory_read()
        memory['forgotten']['ben'] = 1
        save('memory.json', memory)
        for person in ('ana', 'ben', 'together'):
            metrics.record_shown(self.root, cfg, s.rid('foryou'), person,
                                 [{'id': 'tmdb:990003', 'type': 'movie'}], s.time.time()-100)
            metrics.update(self.root, cfg, [dict(person=person, key='tmdb:990003', kind='movie',
                           source='history', at=s.time.time()-50)], s.time.time())
        row = {'metas': {'movie': [{'id': 'tmdb:990003', 'name': 'Invented Old Pick', 'type': 'movie'}]}}
        s._feeds[s.rid('ben'), s.LANG] = row
        s._shown[s.rid('ben'), s.LANG] = row
        s._shown_scoped['ana'] = {(s.rid('foryou'), s.LANG): row}
        for store in (s._pin_cache, s._home_cache, s._person_models):
            store['old'] = row
        s._learn['hits'] = [{'key': 'tmdb:990003', 'at': 1}]
        change('member_forget', person='ben', shared='drop')

        expected = c.config(s)
        expected['household'].pop('ai_monthly_usd')
        journal = c.journal(s)
        change('undo', values=['AI budget'])
        self.assertEqual(s.ai_monthly_usd(), original_budget)
        self.assertEqual(c.config(s), expected)
        self.assertTrue(next(e for e in c.journal(s)['ana'] if e['id'] == budget_id)['undone'])
        self.assertEqual([e for e in c.journal(s)['ana'] if e['id'] != budget_id],
                         [e for e in journal['ana'] if e['id'] != budget_id])
        self.assertFalse(leave.contains(c.config(s), 'ben'))
        self.assertEqual(s.kids_age('mia'), 12)
        self.assertIn('kim', c.config(s)['household']['retired'])
        self.assertEqual([e['who'] for e in s.read_feedback()], ['ana'])
        self.assertEqual([e['who'] for e in s.read_imports()], ['ana'])
        self.assertEqual([w['scope'] for w in shelves.read(s)['wishes']], ['ana'])
        self.assertEqual(history.read(self.root), [])
        self.assertIn('tmdb:990005', history.seen_keys(self.root))
        for name in (*stores, 'commands.json', 'shelves.json', 'feedback.jsonl', 'imports.jsonl',
                     'memory.json', 'metrics.json', 'metrics_shown.json'):
            with self.subTest(store=name):
                path = self.root / name
                self.assertNotIn('ben', path.read_text().casefold() if path.exists() else '')
        self.assertFalse((self.root / 'credentials/synthetic-ben.json').exists())
        self.assertTrue((self.root / 'credentials/synthetic-ana.json').exists())
        for store in (s._feeds, s._shown, s._shown_scoped, s._pin_cache, s._home_cache, s._person_models, s._feeds_empty):
            self.assertFalse(store)
        s._refresh_learning()
        self.assertEqual(s._learn['hits'], [])
        with patch.object(s, 'tmdb_details', side_effect=lambda it: ('movie', dict(id=int(it['id'][5:]), title=it['title']))):
            profile = s.build_profile(s.read_history(), [], remote=True)
        self.assertIn('tmdb:990005', profile['seen'])
        self.assertFalse({'tmdb:990003', 'tmdb:990005'} & {e['key'] for e in profile['entries']})
