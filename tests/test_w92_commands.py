"""Command registry, plans, corrections, questions, wishes, shelf rules, members and search intents."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from skald import backup, commands as c, history, members, setup, shelves

OLD_OPS = ('hide', 'show', 'exclude_genre', 'include_genre', 'focus', 'unfocus', 'discovery', 'years', 'like', 'dislike',
           'seen', 'less', 'more', 'runtime', 'kids_age', 'settings', 'history', 'undo', 'redo', 'reset', 'shelf_list',
           'shelf_add', 'shelf_stop', 'shelf_count', 'row_rename', 'row_hide', 'row_show', 'row_move')


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
        net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)
        ctx = patch.object(c, 'context', return_value=('w92', None, 'ana'))
        ctx.start()
        self.addCleanup(ctx.stop)

    def say(self, text):
        return c.special(self.s, text, 'movie')['metas'][0]

    def confirm(self):
        return self.say('yes ' + c.confirm_code(c.get_pending(self.s, 'w92')))

    def prefs(self, person='ana'):
        return self.s.CONFIG['taste']['people'][person].get('prefs', {})

    def parse(self, text):
        return c.parse_local(text, self.s)


class Registry(Household):
    def test_registry_is_the_one_op_table(self):
        self.assertEqual(c.OPS, tuple(c.REGISTRY))
        self.assertEqual(c.OPS[:len(OLD_OPS)], OLD_OPS)
        self.assertEqual(c.REGISTRY['hide']['inverse'], 'show')
        self.assertEqual(c.REGISTRY['include_genre']['inverse'], 'exclude_genre')
        self.assertEqual(c.REGISTRY['like']['feedback'], 'love')
        prompt = c.command_prompt(self.s)
        for op in ('plan', 'correct', 'ask', 'wish', 'shelf_filter', 'shelf_schedule', 'member_create', 'member_update', 'search'):
            self.assertIn(op, c.REGISTRY)
            self.assertIn(op + ' ', prompt)
        self.assertTrue(prompt.endswith('Unsupported settings return op null. Do not invent an operation.'))

    def test_old_answers_keep_their_four_keys(self):
        self.assertEqual(set(c.parse_json('{"op": "hide", "values": ["anime"]}')), {'op', 'kind', 'values', 'person'})
        self.assertEqual(self.parse('kids age 9'), dict(op='kids_age', kind=None, values=[9], person=None))
        self.assertEqual(c.preview(self.s, 'x', 'ana', answer=dict(op='show', kind=None, values=['anime'], person=None))['name'],
                         'Show anime for Ana')


class Plans(Household):
    def test_sentences_become_ordered_plans(self):
        cases = {
            'Hide anime and cap movies at 90 minutes for Ana': [('hide', None, ['anime'], 'ana'), ('runtime', 'movie', [90], 'ana')],
            'Für Ben keinen Horror und nur Filme ab 2000': [('exclude_genre', None, ['Horror'], 'ben'), ('years', None, [2000, None], 'ben')],
            'Za Anu više komedija i manje serija': [('more', 'genre', ['Comedy'], 'ana'), ('less', 'series', [], 'ana')],
            'Rename For You to Ana Picks and hide Top 10': [('row_rename', None, ['foryou', 'Ana Picks'], None), ('row_hide', None, ['top10'], None)],
            'Anime wieder anzeigen und den Fokus auf Deutsch entfernen': [('show', None, ['anime'], None), ('unfocus', 'languages', ['de'], None)],
            'Сакриј аниме и врати мешавину популарних и мање познатих наслова': [('hide', None, ['anime'], None), ('discovery', None, ['balanced'], None)],
            'hide documentaries and no horror': [('hide', None, ['documentaries'], None), ('exclude_genre', None, ['Horror'], None)],
        }
        for text, steps in cases.items():
            answer = self.parse(text)
            self.assertEqual(answer['op'], 'plan', text)
            self.assertEqual([(x['op'], x['kind'], x['values'], x['person']) for x in answer['steps']], steps, text)
        for text in ('Find the film Hide and Show', 'Tražim film Više i manje', 'I already watched Pride and Prejudice',
                     'Suche den Film Hide und Show'):
            self.assertNotEqual(self.parse(text)['op'], 'plan', text)
        self.assertEqual(self.parse('Find the film Hide and Show')['op'], None)
        self.assertEqual(self.parse('I already watched Pride and Prejudice')['values'], ['Pride and Prejudice'])

    def test_plan_previews_all_steps_applies_together_and_undoes_once(self):
        card = self.say('hide anime and rename For You to Ana Picks')
        self.assertIn('Hide anime for Ana', card['description'])
        self.assertIn('Rename For You to Ana Picks', card['description'])
        self.assertEqual(self.prefs(), {})
        self.confirm()
        self.assertEqual(self.prefs()['hide'], ['anime'])
        self.assertEqual(self.s.CONFIG['household']['row_labels']['foryou'], 'Ana Picks')
        self.assertEqual(len(c.journal(self.s)['ana']), 1)
        self.say('undo')
        self.assertEqual(self.prefs(), {})
        self.assertNotIn('foryou', self.s.CONFIG['household'].get('row_labels', {}))

    def test_one_bad_step_changes_nothing(self):
        answer = dict(op='plan', kind=None, values=[], person=None,
                      steps=[dict(op='hide', kind=None, values=['anime'], person=None),
                             dict(op='runtime', kind='movie', values=[900], person=None)])
        with self.assertRaises(ValueError):
            c.preview(self.s, 'x', 'ana', 'ana', answer=answer)
        good = dict(answer, steps=answer['steps'][:1] + [dict(op='years', kind=None, values=[2000, None], person=None)])
        plan = c.preview(self.s, 'x', 'ana', 'ana', answer=good)
        c.apply(self.s, c.preview(self.s, 'y', 'ana', 'ana', answer=dict(op='hide', kind=None, values=['movies'], person=None)))
        with self.assertRaises(ValueError):
            c.apply(self.s, plan)
        self.assertEqual(self.prefs(), {'hide': ['movies']})


class Corrections(Household):
    def test_parse(self):
        self.assertEqual((self.parse('no, I meant Ben')['op'], self.parse('no, I meant Ben')['person']), ('correct', 'ben'))
        self.assertEqual(self.parse('nein, ich meinte Ben')['op'], 'correct')
        self.assertEqual(self.parse('ne, mislila sam na Bena')['person'], 'ben')
        self.assertEqual(self.parse('not horror, thriller')['values'], ['horror', 'thriller'])
        self.assertEqual(self.parse('nicht Horror, sondern Thriller')['values'], ['Horror', 'Thriller'])
        self.assertEqual(self.parse('ne horor nego triler')['op'], 'correct')
        for text in ('Not tonight for Dune; keep it for another evening', 'Find the film Not Tonight', 'not now'):
            self.assertNotEqual(self.parse(text)['op'], 'correct', text)

    def test_fix_pending_change(self):
        self.say('no horror')
        card = self.say('no, I meant Ben')
        self.assertIn('No more Horror for Ben', card['description'])
        self.confirm()
        self.assertEqual(self.prefs('ben')['excluded_genres']['movie'], ['Horror'])
        self.assertEqual(self.prefs('ana'), {})

    def test_fix_applied_change_replaces_it(self):
        self.say('no horror')
        self.confirm()
        self.assertEqual(self.prefs()['excluded_genres']['movie'], ['Horror'])
        card = self.say('not horror, thriller')
        self.assertIn('No more Thriller for Ana', card['description'])
        self.confirm()
        self.assertEqual(self.prefs()['excluded_genres'], {'movie': ['Thriller'], 'series': ['Thriller']})

    def test_nothing_to_correct(self):
        self.assertIn('Nothing to correct', self.say('no, I meant Ben')['description'])


class Questions(Household):
    def test_unsure_requests_ask_one_question(self):
        cases = {'Maybe hide some of those?': 'change', 'Mach es für sie weniger gruselig': 'person',
                 'Možda sam to već gledala, nisam sigurna': 'title',
                 'Hide horror for me or maybe for everyone, I cannot decide': 'scope',
                 'Den Film bitte bis irgendwann nach den Ferien ausblenden': 'date',
                 'Dodaj mi neku policu, ne znam za koje dane': 'schedule'}
        for text, slot in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['slot']), ('ask', slot), text)
            self.assertTrue(answer['question'].endswith('?'))
            self.assertEqual((answer['values'], answer['person'], answer['kind']), ([], None, None))
        card = self.say('Maybe hide some of those?')
        self.assertTrue(card['description'].splitlines()[0].endswith('?'))
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        self.assertEqual(self.prefs(), {})
        self.assertIsNone(self.parse('Find the film Maybe Tomorrow')['op'])


class Wishes(Household):
    def test_wish_is_logged_without_settings_change(self):
        cases = {'I wish there were more gentle mysteries in my rows': (['gentle mysteries'], None),
                 'Ich wünschte, es gäbe mehr kurze Komödien für Ana': (['kurze Komödien'], 'ana'),
                 'Volela bih više domaćih filmova': (['domaćih filmova'], None),
                 'It would be nice if Ben had more nature documentaries': (['nature documentaries'], 'ben'),
                 'Schön wäre eine größere Auswahl ruhiger Serien': (['ruhiger Serien'], None),
                 'Волела бих више веселих серија за нас': (['веселих серија'], 'together')}
        for text, (values, person) in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['values'], answer['person']), ('wish', values, person), text)
        self.assertIsNone(self.parse('Find the movie Wish')['op'])
        before = (self.root / 'skald.json').read_text()
        self.assertIn('gentle mysteries', self.say('I wish there were more gentle mysteries in my rows')['description'])
        self.assertIn('No settings changed', self.confirm()['description'])
        rows = [json.loads(line) for line in (self.root / 'wishes.jsonl').read_text().splitlines()]
        self.assertEqual([(r['who'], r['text']) for r in rows], [('ana', 'gentle mysteries')])
        self.assertEqual((self.root / 'skald.json').read_text(), before)
        self.assertEqual(os.stat(self.root / 'wishes.jsonl').st_mode & 0o777, 0o600)
        self.assertIn('wishes.jsonl', backup.BACKUP_FILES)
        self.assertIn('wishes.jsonl', self.s.BACKUP_FILES)
        cfg = c.config(self.s)
        history.forget(self.root, cfg, '2100-01-01', 'ana', apply=True)
        self.assertEqual((self.root / 'wishes.jsonl').read_text(), '')


class ShelfRules(Household):
    def add_shelf(self, text):
        c.apply(self.s, c.preview(self.s, text, 'ana', 'ana'))
        return shelves.read(self.s)['wishes'][-1]

    def test_parse_filters_and_schedules(self):
        cases = {'Limit my Classics shelf to movies under 90 minutes': (['Classics'], None, {'kind': 'movie', 'runtime': 90}),
                 'Im Regal Krimi nur Filme von 1990 bis 1999': (['Krimi'], None, {'kind': 'movie', 'years': [1990, 1999]}),
                 'Na polici Porodično samo filmovi na nemačkom': (['Porodično'], None, {'kind': 'movie', 'languages': ['de']}),
                 'Exclude horror from the Family shelf for Ana': (['Family'], 'ana', {'exclude_genres': ['Horror']}),
                 'Für Ben im Regal Kurz nur Serienepisoden bis 30 Minuten': (['Kurz'], 'ben', {'kind': 'series', 'runtime': 30}),
                 'Na polici Vikend isključi anime': (['Vikend'], None, {'hide': ['anime']})}
        for text, (values, person, filters) in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['values'], answer['person'], answer['filters']),
                             ('shelf_filter', values, person, filters), text)
        cases = {'Christmas movies every December': (['Christmas movies'], None, [12]),
                 'Jeden Dezember Weihnachtsfilme für uns': (['Weihnachtsfilme'], 'together', [12]),
                 'Božićni filmovi svakog decembra za Anu': (['Božićni filmovi'], 'ana', [12]),
                 'Halloween horror films every October for Ben': (['Halloween horror films'], 'ben', [10]),
                 'Jedes Jahr im Juli und August Sommerkomödien': (['Sommerkomödien'], None, [7, 8]),
                 'Сваког јануара зимски класици за нас': (['зимски класици'], 'together', [1])}
        for text, (values, person, months) in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['values'], answer['person'], answer['months']),
                             ('shelf_schedule', values, person, months), text)
        for text in ('Find the film Shelf Life', 'Suche den Film Unter 90 Minuten', 'Pronađi film Decembar',
                     'Find the film Christmas Every December'):
            self.assertIsNone(self.parse(text)['op'], text)

    def test_filter_applies_and_undoes(self):
        wish = self.add_shelf('create a shelf about classics')
        self.say('Limit my Classics shelf to movies under 90 minutes')
        self.confirm()
        self.assertEqual(shelves.rules(self.s)[wish['id']]['filters'], {'kind': 'movie', 'runtime': 90})
        movie = {'id': 'tt1', 'type': 'movie'}
        with patch.object(self.s, 'row_facts', return_value={'g': set(), 'runtime': 120, 'date': '1990-01-01'}):
            self.assertFalse(shelves.rule_ok(self.s, movie, shelves.rules(self.s)[wish['id']]))
        with patch.object(self.s, 'row_facts', return_value={'g': set(), 'runtime': 80, 'date': '1990-01-01'}):
            self.assertTrue(shelves.rule_ok(self.s, movie, shelves.rules(self.s)[wish['id']]))
            self.assertFalse(shelves.rule_ok(self.s, {'id': 'tt2', 'type': 'series'}, shelves.rules(self.s)[wish['id']]))
        self.say('undo')
        self.assertNotIn(wish['id'], shelves.rules(self.s))
        self.assertIn('unknown or ambiguous shelf', self.say('Limit my Westerns shelf to movies under 90 minutes')['description'])

    def test_season_schedule(self):
        self.say('Christmas movies every December')
        self.confirm()
        wish = shelves.read(self.s)['wishes'][-1]
        self.assertEqual(shelves.rules(self.s)[wish['id']]['months'], [12])
        self.assertIsNone(shelves.window(wish, dt.datetime(2026, 7, 1, 20), shelves.rules(self.s).get(wish['id'])))
        self.assertEqual(shelves.window(wish, dt.datetime(2026, 12, 1, 20), shelves.rules(self.s).get(wish['id'])), 'always')
        self.assertIn('shelf_rules.json', backup.BACKUP_FILES)
        self.say('undo')
        self.assertEqual(shelves.read(self.s)['wishes'], [])


class Members(Household):
    def test_parse(self):
        cases = {'Add a household member named Kim, age 12': ('member_create', 'kim', 12, None),
                 'Neues Mitglied Kim, 14 Jahre alt, bitte anlegen': ('member_create', 'kim', 14, None),
                 'Dodaj člana Kim, ima 10 godina': ('member_create', 'kim', 10, None),
                 'Update Ben to age 13': ('member_update', 'ben', 13, None),
                 'Ändere Anas Anzeigenamen zu Kim': ('member_update', 'ana', None, 'Kim'),
                 'Ben sada ima 15 godina, ažuriraj njegov uzrast': ('member_update', 'ben', 15, None)}
        for text, expect in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['person'], answer.get('age'), answer.get('label')), expect, text)
        for text in ('Find the film New Member', 'Pronađi film Novi član Kim'):
            self.assertIsNone(self.parse(text)['op'], text)

    def test_create_update_and_undo(self):
        self.assertIn('Kim', self.say('Add a household member named Kim, age 12')['description'])
        self.assertIn('Restart', self.confirm()['description'])
        cfg = c.config(self.s)
        self.assertEqual(cfg['household']['members'], ['ana', 'ben', 'kim'])
        self.assertEqual(cfg['taste']['people']['kim']['prefs']['kids_age'], 12)
        self.say('Ändere Bens Anzeigenamen zu Benny')
        self.confirm()
        self.assertEqual(c.config(self.s)['household']['labels']['ben'], 'Benny')
        self.say('undo')
        self.assertEqual(c.config(self.s)['household']['labels']['ben'], 'Ben')
        self.say('rückgängig')  # A repeated identical undo within a minute is deduplicated.
        self.assertEqual(c.config(self.s)['household']['members'], ['ana', 'ben'])
        self.assertIn('already', self.say('Add a household member named Ben, age 30')['description'])


class SearchIntents(Household):
    def test_search_requests_are_not_settings(self):
        cases = {'Something like Arrival': ('similar', ['Arrival']), 'Mehr wie Dark bitte': ('similar', ['Dark']),
                 'Nešto kao Dune': ('similar', ['Dune']), 'Movies by Kim for tonight': ('people', ['Kim']),
                 'Heute etwas Gemütliches': ('mood', ['Gemütliches']), 'Нешто весело за вечерас, молим': ('mood', ['весело'])}
        for text, (kind, values) in cases.items():
            answer = self.parse(text)
            self.assertEqual((answer['op'], answer['kind'], answer['values']), ('search', kind, values), text)
        before = (self.root / 'skald.json').read_text()
        self.assertIsNone(c.special(self.s, 'Something like Arrival', 'movie'))
        self.assertIsNone(c.special(self.s, 'Something like Arrival', 'movie', routed=True))
        self.assertEqual((self.root / 'skald.json').read_text(), before)


class ReviewFixes(Household):
    def member(self, person, label, viewer='ana'):
        return c.preview(self.s, 'x', viewer, None, answer=dict(op='member_update', person=person, label=label))

    def test_member_apply_locks_before_reading_and_checks_only_its_member(self):
        benny, annie = self.member('ben', 'Benny'), self.member('ana', 'Annie', 'ben')
        real, first = shelves.locked, [True]

        @contextlib.contextmanager
        def racing(s):
            if first:
                first.pop()
                c.apply(self.s, annie)
            with real(s):
                yield
        with patch.object(shelves, 'locked', racing):
            c.apply(self.s, benny)
        labels = c.config(self.s)['household']['labels']
        self.assertEqual((labels['ana'], labels['ben']), ('Annie', 'Benny'))
        self.assertEqual((len(c.journal(self.s)['ana']), len(c.journal(self.s)['ben'])), (1, 1))
        c.undo(self.s, ['ana'])
        labels = c.config(self.s)['household']['labels']
        self.assertEqual((labels['ana'], labels['ben']), ('Annie', 'Ben'))

    def test_member_creation_undo_refuses_when_member_has_data(self):
        self.say('Add a household member named Kim, age 12')
        self.confirm()
        (self.root / 'memory.json').write_text(json.dumps({'facts': [{'who': 'kim', 'text': 'likes owls'}]}))
        self.assertIn('has data', self.say('undo')['description'])
        self.assertIn('kim', c.config(self.s)['household']['members'])

    def test_wish_append_holds_the_forgetting_lock(self):
        seen, real = [], history.locked

        @contextlib.contextmanager
        def spy(data):
            with real(data):
                seen.append(Path(data).resolve())
                yield
        self.say('I wish there were more gentle mysteries')
        with patch.object(history, 'locked', spy):
            self.confirm()
        self.assertIn(self.root.resolve(), seen)

    def test_api_corrections_use_pending_and_last_change(self):
        def req(body):
            return {'body': body, 'viewer': 'ana', 'client': {'name': 't'}}
        c.api_preview(self.s, req({'in': {'query': 'no horror'}}))
        plan = c.api_preview(self.s, req({'in': {'query': 'not horror, thriller'}}))['preview']
        self.assertEqual(plan['name'], 'No more Thriller for Ana')
        c.api_apply(self.s, req({'plan': plan['plan']}))
        plan = c.api_preview(self.s, req({'in': {'query': 'not thriller, horror'}}))['preview']
        self.assertEqual(plan['name'], 'Fix: No more Horror for Ana')

    def test_season_redo_keeps_months(self):
        self.say('Christmas movies every December')
        self.confirm()
        self.say('undo')
        self.say('redo Christmas')
        c.apply(self.s, c.get_pending(self.s, 'w92'))  # The same yes code within a minute repeats its first result.
        wish = shelves.read(self.s)['wishes'][-1]
        self.assertEqual(shelves.rules(self.s)[wish['id']]['months'], [12])

    def test_excluding_a_whole_type_keeps_the_other(self):
        c.apply(self.s, c.preview(self.s, 'create a shelf about classics', 'ana', 'ana'))
        plan = c.preview(self.s, 'Exclude movies from my Classics shelf', 'ana', 'ana')
        self.assertEqual(plan['rule']['after']['filters'], {'kind': 'series'})

    def test_plan_steps_see_earlier_steps(self):
        self.say('hide Top Rated')
        self.confirm()
        card = self.say('show Top Rated and move Top Rated before For You')
        self.assertIn('Move Best Movies before For You', card['description'])
        self.confirm()
        self.assertNotIn('toprated', self.s.CONFIG['household'].get('hidden_rows', []))

    def test_serbian_correction_uses_genre_aliases(self):
        self.say('no horror')
        self.confirm()
        self.assertIn('No more Thriller for Ana', self.say('Ne horor, nego triler')['description'])

    def test_correction_to_everyone(self):
        self.say('no horror')
        self.assertIn('No more Horror for Ana, Ben', self.say('No, I meant for everyone')['description'])

if __name__ == '__main__':
    unittest.main()
