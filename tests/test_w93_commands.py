"""Title fixes: defer, reject, seen, ignore learning, reattribute, explain and title-shaped searches."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import backup, commands as c, history, members, setup

NEW_OPS = ('defer', 'reject', 'ignore_learning', 'reattribute', 'explain')
DUNE = {'id': 438631, 'title': 'Dune', 'release_date': '2021-09-15'}
ARRIVAL = {'id': 329865, 'title': 'Arrival', 'release_date': '2016-11-10'}


def pick(kind, title, year, lang):
    hit = {'dune': DUNE, 'arrival': ARRIVAL}.get(title.casefold())
    return (hit, {}) if hit and kind == 'movie' else None


def entry(item, by):
    return {'key': 'tmdb:' + str(item['id']), 'kind': 'movie', 'title': item['title'], 'year': item['release_date'][:4],
            'by': by, 'signal': 'watched', 'weight': 1.0, 'genres': [], 'age': 3, 'flags': [], 'item': {}}


class Household(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'SKALD_CONFIG': ''})
        env.start()
        self.addCleanup(env.stop)
        setup.publish(self.root, members.fresh_config(['ana', 'ben']))
        self.s = load_server(self.root)
        self.addCleanup(close_server, self.s)
        for target, value in (('urllib.request.urlopen', None),):
            net = patch(target, side_effect=AssertionError('network forbidden'))
            net.start()
            self.addCleanup(net.stop)
        ctx = patch.object(c, 'context', return_value=('w93', None, 'ana'))
        ctx.start()
        self.addCleanup(ctx.stop)
        titles = patch.object(self.s, 'resolve_pick', side_effect=pick)
        titles.start()
        self.addCleanup(titles.stop)

    def say(self, text):
        return c.special(self.s, text, 'movie')['metas'][0]

    def confirm(self):
        return self.say('yes ' + c.confirm_code(c.get_pending(self.s, 'w93')))

    def parse(self, text):
        return c.parse_local(text, self.s)

    def fixes(self):
        path = self.root / c.FIXES
        return json.loads(path.read_text())['fixes'] if path.exists() else []

    def profile(self, *entries):
        self.s._profiles['household'] = {'entries': list(entries), 'seen': {e['key'] for e in entries}, 'negative': set()}

    def feedback(self):
        return {k: e for k, e in self.s.active_feedback(self.s.read_feedback()).items()}


class Registry(unittest.TestCase):
    def test_new_ops_append_and_prompt_names_them(self):
        old = c.OPS[:c.OPS.index('search') + 1]
        self.assertEqual(c.OPS, (*old, *NEW_OPS, 'like_results', 'shelf_from_results', 'taste_drivers', 'home_plan', 'home_more', 'home_fewer', 'row_refresh'))
        self.assertIn('until', c.OUT)
        self.assertIn('reason', c.OUT)
        specs = {}
        s = SimpleNamespace(DATA='.', PEOPLE=['ana', 'ben'], LABELS={'ana': 'Ana', 'ben': 'Ben'}, HIDE_CHOICES=('anime',),
                            PICK_MODEL='fixture', DAY=86400, route_norm=lambda x: x,
                            decision=lambda name, **spec: specs.update({name: spec}))
        c.register(s)
        self.assertEqual(specs['parse_command']['ver'], 12)
        prompt = c.command_prompt(s)
        for op in NEW_OPS:
            self.assertIn(op + ' ', prompt)
        self.assertIn('kind title', prompt)
        self.assertTrue(prompt.endswith('Unsupported settings return op null. Do not invent an operation.'))
        self.assertTrue(c.REGISTRY['explain']['kids'])


class Parse(Household):
    def test_contract_cases(self):
        cases = {
            'Not tonight for Dune; keep it for another evening': dict(op='defer', values=['Dune'], until='tonight'),
            'Arrival bitte nicht vor dem 2026-12-24 zeigen': dict(op='defer', values=['Arrival'], until='2026-12-24'),
            'Не приказуј Arrival пре 2027-01-05': dict(op='defer', values=['Arrival'], until='2027-01-05'),
            'Hide Dune until 2026-12-01': dict(op='defer', values=['Dune'], until='2026-12-01'),
            'Heute Abend nicht: Dune': dict(op='defer', values=['Dune'], until='tonight'),
            'Ne večeras Dune': dict(op='defer', values=['Dune'], until='tonight'),
            'Dune nije za mene, previše je spor': dict(op='reject', values=['Dune'], reason='previše je spor'),
            'Dune is not for me, too dark': dict(op='reject', values=['Dune'], reason='too dark'),
            'Arrival ist nichts für mich': dict(op='reject', values=['Arrival'], reason=None),
            'Arrival is already seen; remove it from my unseen picks': dict(op='seen', values=['Arrival']),
            'Arrival habe ich schon gesehen': dict(op='seen', values=['Arrival']),
            'Već sam gledala Dune': dict(op='seen', values=['Dune']),
            'I already watched Dune': dict(op='seen', values=['Dune']),
            'Lerne bitte nichts aus meinem Abspielen von Dune': dict(op='ignore_learning', values=['Dune']),
            "Don't learn from my watching of Arrival": dict(op='ignore_learning', values=['Arrival']),
            'Ne uči ništa iz mog gledanja Dune': dict(op='ignore_learning', values=['Dune']),
            'Arrival je gledao Ben, ne ja; ispravi kome pripada gledanje': dict(op='reattribute', values=['Arrival'], person='ben'),
            'Ben watched Arrival, not me': dict(op='reattribute', values=['Arrival'], person='ben'),
            'Ben hat Dune geschaut, nicht ich': dict(op='reattribute', values=['Dune'], person='ben'),
            'Why is Dune here in my recommendations?': dict(op='explain', values=['Dune']),
            'Warum wird mir Arrival empfohlen?': dict(op='explain', values=['Arrival']),
            'Zašto je Dune ovde?': dict(op='explain', values=['Dune']),
            'Film Mirno': dict(op='search', values=['Mirno'], kind='title'),
            'Der Film Happy': dict(op='search', values=['Happy'], kind='title'),
        }
        for text, expect in cases.items():
            answer = self.parse(text)
            self.assertEqual({k: answer.get(k) for k in expect}, expect, text)

    def test_person_suffix_still_names_the_person(self):
        for text, expect in (('I already watched Dune for Ben', dict(op='seen', values=['Dune'], person='ben')),
                             ('Not tonight Dune for Ben', dict(op='defer', values=['Dune'], person='ben'))):
            answer = self.parse(text)
            self.assertEqual({k: answer.get(k) for k in expect}, expect, text)

    def test_look_alikes_change_nothing(self):
        for text in ('Find the film Not Tonight', 'Suche den Film Schon gesehen', 'Suche den Film Happy',
                     'Find the film Why Is Dune Here', 'Tražim film Nije za mene', 'not now', 'Not Tonight',
                     'Den Film bitte bis irgendwann nach den Ferien ausblenden', 'Möglicherweise schon gesehen'):
            answer = self.parse(text)
            self.assertNotIn(answer['op'], (*NEW_OPS, 'seen', 'correct'), text)
            self.assertNotIn(c.REGISTRY.get(answer['op'], {}).get('group'), ('prefs', 'rows', 'feedback'), text)
        self.assertEqual(self.parse('Den Film bitte bis irgendwann nach den Ferien ausblenden')['op'], 'ask')
        # Ben, not a member here, is a title word rather than a person.
        self.assertIsNone(self.parse('Kim watched Arrival, not me')['op'])

    def test_title_search_never_changes_settings(self):
        before = (self.root / 'skald.json').read_text()
        self.assertIsNone(c.special(self.s, 'Film Mirno', 'movie'))
        self.assertIsNone(c.get_pending(self.s, 'w93'))
        self.assertEqual((self.root / 'skald.json').read_text(), before)


class Defer(Household):
    def test_tonight_hides_for_one_person_and_returns_by_itself(self):
        card = self.say('Not tonight for Dune; keep it for another evening')
        self.assertIn('Dune', card['name'])
        self.assertIn('Ana', card['description'])
        self.assertEqual(self.fixes(), [])
        self.assertIn('Hide', self.confirm()['name'])
        self.assertEqual(os.stat(self.root / c.FIXES).st_mode & 0o777, 0o600)
        [row] = self.fixes()
        self.assertEqual((row['op'], row['who'], row['key']), ('defer', 'ana', 'tmdb:438631'))
        self.assertLessEqual(row['until'] - self.s.time.time(), 24 * 3600 + 60)
        self.assertGreater(row['until'], self.s.time.time())
        self.assertIn('tmdb:438631', self.s.row_masks('ana')[0])
        self.assertNotIn('tmdb:438631', self.s.row_masks('ben')[0])
        self.assertIn('tmdb:438631', self.s.row_masks('both')[0])
        self.assertEqual(self.s.deferred_keys('ana', now=row['until'] + 1), frozenset())
        self.say('undo')
        self.assertEqual(self.fixes(), [])
        self.assertNotIn('tmdb:438631', self.s.row_masks('ana')[0])

    def test_tonight_ends_at_five_in_the_morning(self):
        local = dt.datetime(2026, 10, 13, 21, 30)
        self.assertEqual(c.until_time(self.s, 'tonight', now=1000.0, local=local), 1000.0 + 7.5 * 3600)
        local = dt.datetime(2026, 10, 14, 2, 0)
        self.assertEqual(c.until_time(self.s, 'tonight', now=1000.0, local=local), 1000.0 + 3 * 3600)

    def test_dates_must_be_after_today_and_within_a_year(self):
        today = self.s.local_now().date()
        soon = (today + dt.timedelta(days=30)).isoformat()
        self.assertIn('Dune', self.say('Hide Dune until ' + soon)['name'])
        self.confirm()
        [row] = self.fixes()
        self.assertEqual(row['until_text'], soon)
        for bad in ((today - dt.timedelta(days=1)).isoformat(), today.isoformat(), (today + dt.timedelta(days=400)).isoformat()):
            with self.assertRaises(ValueError):
                c.preview(self.s, 'x', 'ana', answer=dict(op='defer', kind=None, values=['Dune'], person=None, until=bad))
        for bad in (None, 'someday', 'forever', '2026-13-40'):
            with self.assertRaises(ValueError):
                c.preview(self.s, 'x', 'ana', answer=dict(op='defer', kind=None, values=['Dune'], person=None, until=bad))

    def test_tonight_and_dates_follow_daylight_saving(self):
        # 2026-10-24 21:00 in Berlin is CEST (UTC+2); the clocks go back on 25 October.
        now = dt.datetime(2026, 10, 24, 19, 0, tzinfo=dt.timezone.utc).timestamp()
        with patch.object(self.s, 'TZ', 'Europe/Berlin'):
            self.assertEqual(c.until_time(self.s, 'tonight', now=now), now + 9 * 3600)
            self.assertEqual(c.until_time(self.s, '2026-10-26', now=now), now + 28 * 3600)

    def test_custom_shelves_hide_deferred_titles(self):
        metas = [{'id': 'tmdb:' + str(n), 'type': 'movie', 'name': str(n)} for n in range(438631, 438639)]
        with patch.object(self.s, 'shelf_rows', return_value=[('shelf-x',)]), \
                patch.object(self.s, 'shelf_feed', return_value={'shelf-x': {'tv': metas}}), \
                patch.object(self.s, 'preference_ok', return_value=True):
            self.say('Not tonight for Dune')
            self.confirm()
            self.assertNotIn('tmdb:438631', [m['id'] for m in self.s.shelf_items('shelf-x', 'movie', 'en', 'ana')])
            self.assertIn('tmdb:438631', [m['id'] for m in self.s.shelf_items('shelf-x', 'movie', 'en', 'ben')])

    def test_expired_rows_are_pruned_on_the_next_write(self):
        c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=dict(op='defer', kind=None, values=['Dune'], person=None, until='tonight')))
        until = self.fixes()[0]['until']
        with patch.object(self.s.time, 'time', return_value=until + 10):
            c.apply(self.s, c.preview(self.s, 'y', 'ana', answer=dict(op='defer', kind=None, values=['Arrival'], person=None, until='tonight')))
        self.assertEqual([r['key'] for r in self.fixes()], ['tmdb:329865'])

    def test_targeted_undo_and_kids(self):
        self.say('Not tonight for Dune')
        self.confirm()
        self.assertIn('plain undo only', self.say('undo Dune')['description'])
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'x', 'ana', 'guest', answer=dict(op='defer', kind=None, values=['Dune'], person=None, until='tonight'))


class Reject(Household):
    def test_reason_maps_to_chips_and_undo_restores(self):
        card = self.say('Dune nije za mene, previše je spor')
        self.assertIn('previše je spor', card['description'])
        self.confirm()
        event = self.feedback()[('tmdb:438631', 'ana')]
        self.assertEqual((event['action'], event['reasons']), ('not_for_us', ['too slow']))
        entry = c.journal(self.s)['ana'][-1]
        self.assertEqual(entry['reason'], 'previše je spor')
        self.say('undo')
        self.assertNotIn(('tmdb:438631', 'ana'), self.feedback())

    def test_existing_dislike_is_reused(self):
        self.s.write_feedback(dict(key='tmdb:438631', kind='movie', title='Dune', year='2021', who='ana', action='not_for_us',
                                   reasons=['too slow']))
        lines = (self.root / 'feedback.jsonl').read_text()
        card = self.say('Dune is not for me')
        self.assertIn('Already', card['name'])
        self.assertIsNone(c.get_pending(self.s, 'w93'))
        self.assertEqual((self.root / 'feedback.jsonl').read_text(), lines)
        card = self.say('Dune is not for me, too dark')
        self.confirm()
        self.assertEqual(self.feedback()[('tmdb:438631', 'ana')]['reasons'], ['too slow', 'too dark'])

    def test_new_reason_on_an_existing_dislike_is_noted(self):
        self.s.write_feedback(dict(key='tmdb:438631', kind='movie', title='Dune', year='2021', who='ana', action='not_for_us', reasons=[]))
        lines = (self.root / 'feedback.jsonl').read_text()
        prefs = (self.root / 'skald.json').read_text()
        card = self.say('Dune is not for me, bad acting')
        self.assertIn('bad acting', card['description'])
        self.confirm()
        self.assertEqual(c.journal(self.s)['ana'][-1]['reason'], 'bad acting')
        self.assertEqual((self.root / 'feedback.jsonl').read_text(), lines)
        self.say('undo')
        self.assertEqual((self.root / 'feedback.jsonl').read_text(), lines)
        self.assertEqual((self.root / 'skald.json').read_text(), prefs)

    def test_other_person_keeps_their_feedback(self):
        self.say('Dune is not for me')
        self.confirm()
        self.assertNotIn(('tmdb:438631', 'ben'), self.feedback())


class Seen(Household):
    def test_seen_is_neutral_feedback(self):
        self.say('Arrival is already seen; remove it from my unseen picks')
        self.confirm()
        event = self.feedback()[('tmdb:329865', 'ana')]
        self.assertEqual(event['action'], 'seen')
        self.assertEqual(self.s.event_weight({**event, 'src': 'remote', 'by': ['ana'], 'ctx': {}, 'w0': None, 'conf': 1.0}, self.s.time.time())[:2],
                         ('watched', 1.0))


class Learning(Household):
    def test_ignore_keeps_history_and_only_this_person(self):
        self.profile(entry(DUNE, ['ana', 'ben']))
        self.assertEqual(len(self.s.person_entries(self.s._profiles['household'], 'ana')), 1)
        stamp = self.s.title_fix_stamp()
        self.say('Lerne bitte nichts aus meinem Abspielen von Dune')
        self.confirm()
        p = self.s._profiles['household']
        [kept] = self.s.person_entries(p, 'ana')
        self.assertEqual((kept['key'], kept['weight']), ('tmdb:438631', 0))
        self.assertEqual(self.s.person_entries(p, 'ben')[0]['weight'], 1.0)
        self.assertEqual(len(p['entries']), 1)
        self.assertNotEqual(self.s.title_fix_stamp(), stamp)
        self.assertIn('Already', self.say("Don't learn from my watching of Dune")['name'])
        self.say('undo')
        self.assertEqual(self.s.person_entries(p, 'ana')[0]['weight'], 1.0)
        self.assertEqual(self.s.title_fix_stamp(), stamp)

    def test_ignore_keeps_explicit_opinions(self):
        loved = {**entry(DUNE, ['ana']), 'opinions': {'ana': {'signal': 'loved', 'weight': 2.0}}}
        self.profile(loved, entry(ARRIVAL, ['ana']))
        for text in ("Don't learn from my watching of Dune", "Don't learn from my watching of Arrival"):
            self.say(text)
            self.confirm()
        weights = {e['key']: e['weight'] for e in self.s.person_entries(self.s._profiles['household'], 'ana')}
        self.assertEqual(weights, {'tmdb:438631': 2.0, 'tmdb:329865': 0})

    def test_household_taste_drops_the_ignoring_persons_share(self):
        self.profile(entry(DUNE, ['ana']), entry(ARRIVAL, ['ana', 'ben']))
        for text in ("Don't learn from my watching of Dune", "Don't learn from my watching of Arrival"):
            self.say(text)
            self.confirm()
        weights = {e['key']: e['weight'] for e in self.s.taste_entries(self.s._profiles['household']['entries'])}
        self.assertEqual(weights, {'tmdb:438631': 0, 'tmdb:329865': 0.5})

    def test_new_owner_can_fix_a_moved_watch(self):
        self.profile(entry(ARRIVAL, ['ana']))
        p = self.s._profiles['household']
        self.say('Ben watched Arrival, not me')
        self.confirm()
        with patch.object(c, 'context', return_value=('w93b', None, 'ben')):
            self.assertNotIn('No watch', self.say("Don't learn from my watching of Arrival")['name'])
            self.say('yes ' + c.confirm_code(c.get_pending(self.s, 'w93b')))
            self.assertEqual(self.s.person_entries(p, 'ben')[0]['weight'], 0)
            self.say('undo')
            self.say('Ana watched Arrival, not me')
            self.say('yes ' + c.confirm_code(c.get_pending(self.s, 'w93b')))
        self.assertEqual(self.s.person_entries(p, 'ben'), [])
        self.assertEqual(len(self.s.person_entries(p, 'ana')), 1)

    def test_previews_are_stable_for_plan_hash(self):
        self.profile(entry(ARRIVAL, ['ana']), entry(DUNE, ['ana']))
        for answer in (dict(op='defer', values=['Dune'], until='tonight'), dict(op='ignore_learning', values=['Dune']),
                       dict(op='reattribute', values=['Arrival'], person='ben')):
            answer = {'kind': None, 'person': None, **answer}
            first, second = (c.preview(self.s, 'x', 'ana', answer=dict(answer)) for _ in range(2))
            self.assertEqual(first['plan'], second['plan'], answer['op'])

    def test_reattribute_moves_one_watch(self):
        self.profile(entry(ARRIVAL, ['ana']))
        p = self.s._profiles['household']
        card = self.say('Arrival je gledao Ben, ne ja; ispravi kome pripada gledanje')
        self.assertIn('Ben', card['description'])
        self.confirm()
        self.assertEqual(self.s.person_entries(p, 'ana'), [])
        [moved] = self.s.person_entries(p, 'ben')
        self.assertTrue(moved['explicit'])
        self.assertEqual(p['entries'][0]['by'], ['ana'])
        self.say('undo')
        self.assertEqual(len(self.s.person_entries(p, 'ana')), 1)
        self.assertEqual(self.s.person_entries(p, 'ben'), [])

    def test_reattribute_needs_a_watch_and_another_member(self):
        self.profile(entry(ARRIVAL, ['ben']))
        self.assertIn('No watch', self.say('Ben watched Dune, not me')['description'])
        with self.assertRaises(ValueError):
            c.preview(self.s, 'x', 'ana', answer=dict(op='reattribute', kind=None, values=['Arrival'], person='ana'))
        with self.assertRaises(ValueError):
            c.preview(self.s, 'x', 'ana', answer=dict(op='reattribute', kind=None, values=['Arrival'], person='kim'))


class Replay(Household):
    def test_same_confirm_code_for_a_new_plan_applies_it(self):
        # Two plans can share a two-digit code; a repeated "yes NN" must only replay the plan it confirmed.
        with patch.object(c, 'confirm_code', return_value='42'):
            self.say('no horror')
            self.say('yes 42')
            self.say('not horror, thriller')
            self.say('yes 42')
        prefs = c.config(self.s)['taste']['people']['ana']['prefs']
        self.assertEqual(prefs['excluded_genres'], {'movie': ['Thriller'], 'series': ['Thriller']})


class Explain(Household):
    def test_explain_is_read_only(self):
        before = sorted(os.listdir(self.root))
        card = self.say('Why is Dune here in my recommendations?')
        self.assertIn('Dune', card['name'])
        self.assertIsNone(c.get_pending(self.s, 'w93'))
        with patch.object(self.s, 'explain', return_value={'key': 'tmdb:438631', 'title': 'Dune', 'percentile': 81,
                                                           'reasons': ['Desert epic like Lawrence of Arabia'],
                                                           'per_person': {'ana': {'percentile': 77, 'memory': {'vetoed': False}}}}):
            card = self.say('Zašto je Dune ovde?')
        self.assertIn('Desert epic', card['description'])
        self.assertIn('77', card['description'])
        self.assertEqual(sorted(os.listdir(self.root)), before)


class Forget(Household):
    def test_forgetting_removes_the_person_from_fixes(self):
        self.profile(entry(ARRIVAL, ['ana']), entry(DUNE, ['ben']))
        for text in ('Not tonight for Dune', 'Arrival je gledao Ben, ne ja'):
            self.say(text)
            self.confirm()
        with patch.object(c, 'context', return_value=('w93b', None, 'ben')):
            self.say('Lerne bitte nichts aus meinem Abspielen von Dune')
            self.say('yes ' + c.confirm_code(c.get_pending(self.s, 'w93b')))
        self.assertEqual(len(self.fixes()), 3)
        cfg = c.config(self.s)
        preview = history.forget(self.root, cfg, '2100-01-01', 'ana')
        self.assertEqual(len(self.fixes()), 3)
        self.assertNotIn('fixes', preview)
        history.forget(self.root, cfg, '2100-01-01', 'ben', apply=True)
        self.assertEqual([(r['op'], r['who']) for r in self.fixes()], [('defer', 'ana')])
        self.assertIn('title_fixes.json', backup.BACKUP_FILES)
        self.assertIn('title_fixes.json', self.s.BACKUP_FILES)

    def test_forgetting_the_source_keeps_a_moved_watch(self):
        self.profile(entry(ARRIVAL, ['ana']))
        self.say('Ben watched Arrival, not me')
        self.confirm()
        cfg = c.config(self.s)
        history.forget(self.root, cfg, '2100-01-01', 'ana', apply=True)
        self.assertEqual([(r['op'], r['who'], r['to']) for r in self.fixes()], [('move', 'ana', 'ben')])
        history.forget(self.root, cfg, '2100-01-01', 'ben', apply=True)
        self.assertEqual(self.fixes(), [])

    def test_member_with_fixes_counts_as_data(self):
        self.say('Not tonight for Dune')
        self.confirm()
        self.assertTrue(setup.member_data(self.root, 'ana', c.config(self.s), skip=('commands.json',)))


if __name__ == '__main__':
    unittest.main()
