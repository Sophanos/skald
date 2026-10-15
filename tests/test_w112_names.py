"""Seed row names, synthetic cards and offline transports."""
import copy
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM, close_server, load_server
import test_w108_because as because_tests

from skald import commands, i18n, members, row_names
from skald.__main__ import main


FIXED_NAMES = {
    'row.mood.cozy': ('Cozy Mysteries', 'Gemütliche Krimis', 'Krimi za opuštanje', 'Крими за опуштање'),
    'row.mood.edge': ('Gripping Thrillers', 'Packende Thriller', 'Napeti trileri', 'Напети трилери'),
    'row.mood.heart': ('Moving Dramas', 'Bewegende Dramen', 'Dirljive drame', 'Дирљиве драме'),
    'row.mood.grit': ('Underdog Stories', 'Außenseiter-Geschichten', 'Priče o autsajderima', 'Приче о аутсајдерима'),
    'row.mood.ideas': ('Mind-Bending', 'Kopfkino', 'Igre uma', 'Игре ума'),
    'row.mood.true': ('Based on True Stories', 'Nach wahren Begebenheiten', 'Po istinitim pričama', 'По истинитим причама'),
    'row.mood.home': ('Heartwarming Stories', 'Herzerwärmende Geschichten', 'Priče za dušu', 'Приче за душу'),
    'row.mood.dark': ('Dark and Twisted', 'Düster und abgründig', 'Mračno i uvrnuto', 'Мрачно и уврнуто'),
    'row.newtoyou': ('Something Different', 'Mal was anderes', 'Nešto drugačije', 'Нешто другачије'),
    'row.leap': ('Similar, but Different', 'Ähnlich, aber anders', 'Slično, ali drugačije', 'Слично, али другачије'),
}
for key in ('watched', 'watch'):
    FIXED_NAMES['row.seed.' + key] = ('Because You Watched {title}', 'Weil du {title} gesehen hast',
                                     'Jer si gledao/la {title}', 'Јер си гледао/ла {title}')
for key in ('loved', 'liked'):
    FIXED_NAMES['row.seed.' + key] = ('Because You Liked {title}', 'Weil dir {title} gefallen hat',
                                     'Jer ti se dopao {title}', 'Јер ти се допао {title}')


class FixedNames(unittest.TestCase):
    def test_owner_names_in_every_locale(self):
        for key, names in FIXED_NAMES.items():
            for lang, expected in zip(('en', 'de', 'sr-latn', 'sr-cyrl'), names):
                with self.subTest(key=key, lang=lang):
                    self.assertEqual(i18n._STRINGS[lang][key], expected)

    def test_serbian_mood_names_differ_from_experience_lanes(self):
        for lang in ('sr-latn', 'sr-cyrl'):
            for mood, experience in (('cozy', 'cozy_case'), ('home', 'heartwarming')):
                with self.subTest(lang=lang, mood=mood):
                    self.assertNotEqual(i18n.t('row.mood.' + mood, lang),
                                        i18n.t('row.experience.' + experience, lang))

    def test_home_mood_and_experience_names_are_distinct(self):
        for lang in ('en', 'de', 'sr-latn', 'sr-cyrl'):
            labels = {}
            for key, label in i18n._STRINGS[lang].items():
                if key.startswith(('row.mood.', 'row.experience.')):
                    with self.subTest(lang=lang, key=key):
                        self.assertNotIn(label.casefold(), labels, f'{key} duplicates {labels.get(label.casefold())}')
                    labels[label.casefold()] = key


class SeedNames(unittest.TestCase):
    seed = because_tests.BecauseRows.seed
    item = because_tests.BecauseRows.item
    transport = because_tests.BecauseRows.transport
    built = because_tests.BecauseRows.built

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['region']['lang'] = 'en-US'
        self.data.joinpath('skald.json').write_text(json.dumps(cfg))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        because_tests.BecauseRows.setUp(self)

    def seeds(self):
        out = [self.seed(i, signal=signal) for i, signal in enumerate(('loved', 'liked', 'watched'), 1)]
        for seed, title in zip(out, ('Invented Window', 'Imagined Signal', 'Madeup Harbour')):
            seed['title'] = title
        return out

    def feeling(self):
        s = self.s
        cards = [dict(id=f'tmdb:{99000+i}', name=f'Imaginary Card {i}', type='movie') for i in range(12)]
        self.enterContext(patch.object(s, 'get_model', return_value={'names': {}}))
        self.enterContext(patch.object(s, 'feeling_pool', return_value=[(x['id'], 'movie', x) for x in cards]))
        self.enterContext(patch.object(s, 'themed_row', return_value=cards))
        self.enterContext(patch.object(s, 'balance_row', side_effect=lambda xs: xs))
        self.enterContext(patch.object(s, 'polish_row', side_effect=lambda xs, **kw: xs))
        self.enterContext(patch.object(s, 'theme_months', return_value=None))
        return cards

    def test_seed_rows_use_templates_without_naming_calls_or_learning_changes(self):
        self.seeds()
        self.feeling()
        before = copy.deepcopy(self.s._learn)
        with patch.object(self.s, 'decide', side_effect=AssertionError('no seed naming decision')), ReplayLLM(self.s) as replay:
            for _ in range(2):
                names = self.built()['_names']
                self.assertEqual([n['title'] for n in names.values()], [
                    'Because You Liked Invented Window', 'Because You Liked Imagined Signal', 'Because You Watched Madeup Harbour'])
                feeling = self.s.leap_feed('en-US')['_names'][self.s.rid('leap')]
                self.assertEqual(feeling['title'], 'Same Feeling as Invented Window')
            self.p['entries'].pop(0)
            self.assertEqual(self.built()['_names'][self.rows[0]]['fields']['title'], 'Imagined Signal')
            self.assertEqual(self.s.leap_feed('en-US')['_names'][self.s.rid('leap')]['title'], 'Same Feeling as Imagined Signal')
            self.assertEqual(replay.calls, 0)
        self.assertEqual(self.s._learn, before)
        self.s._learn_dirty.set()
        self.s.save_learn()
        self.assertEqual(json.loads(self.data.joinpath('learn.json').read_text()), before)

    def test_full_build_keeps_existing_lane_call_counts(self):
        s = self.s
        self.seeds()
        cards = self.feeling()
        lane = 'k:99'
        def lanes(lang):
            chosen = [(lane, cards, [], 'Quiet Places')]
            named = s.name_lanes(chosen, {'names': {lane: 'quiet places'}})
            return {s.rid('lane1'): {'tv': cards}, '_names': {s.rid('lane1'): {
                'lane': lane, 'title': named.get(lane, 'Quiet Places'), 'name': 'quiet places'}}}
        jobs = [('because', self.rows, 'test', 0, s.because_feed),
                ('leap', [s.rid('leap')], 'test', 0, s.leap_feed),
                ('lanes', [s.rid('lane1')], 'test', 0, lanes)]
        with patch.object(s, 'job_specs', return_value=jobs), patch.object(s, '_langs', {'en-US', 'de-DE'}), \
                patch.object(s, 'connectors_tick'), patch.object(s, 'pin_decision'), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps({'names': ['Quiet Corners']})}}]})
            s.build_feeds(datetime.datetime(2026, 10, 14), {}, {})
            self.assertEqual(replay.calls, 1)
            for lang in ('en-US', 'de-DE'):
                self.assertEqual(s._feeds[s.rid('leap'), lang]['names']['title'], 'Same Feeling as Invented Window')
                self.assertEqual(s._feeds[s.rid('lane1'), lang]['names']['title'], 'Quiet Corners')
            s.build_feeds(datetime.datetime(2026, 10, 14), {}, {}, force=True)
            self.assertEqual(replay.calls, 1)
            self.p['entries'].pop(0)
            s.build_feeds(datetime.datetime(2026, 10, 14), {}, {}, force=True)
            self.assertEqual(replay.calls, 1)
            lane = 'k:100'
            cards[0]['name'] = 'Imaginary New Example'
            replay.arm({'choices': [{'message': {'content': json.dumps({'names': ['Quiet Places']})}}]})
            s.build_feeds(datetime.datetime(2026, 10, 14), {}, {}, force=True)
            self.assertEqual(replay.calls, 2)
        self.assertEqual(set(s._learn['lanes']), {'k:99', 'k:100'})

    def cache(self, built, filename='feeds.json'):
        records = {(cid, 'en-US'): {'names': built['_names'][cid], 'metas': built[cid]}
                   for cid in built['_names']}
        self.data.joinpath(filename).write_text(json.dumps({cid+'|'+lang: rec for (cid, lang), rec in records.items()}))
        return records

    def test_inspector_and_commander_show_template_source_and_seed_without_writes(self):
        self.seeds()
        self.feeling()
        built, feeling = self.built(), self.s.leap_feed('en-US')
        built['_names'].update(feeling.pop('_names'))
        built.update(feeling)
        self.s._feeds.update(self.cache(built))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        out = io.StringIO()
        with patch.dict('os.environ', {'DATA_DIR': str(self.data)}, clear=True), \
                patch('skald.__main__.Engine', side_effect=AssertionError('no engine')):
            self.assertEqual(main(['rows', 'names', '--json'], out=out), 0)
        names = {r['id']: r for r in json.loads(out.getvalue())['rows']}
        for cid, label, seed, title in (
                (self.rows[0], 'Because You Liked Invented Window', 'tmdb:1', 'Invented Window'),
                (self.s.rid('leap'), 'Same Feeling as Invented Window', 'tmdb:1', 'Invented Window')):
            row = names[cid]
            self.assertEqual((row['label'], row['source'], row['seed'], row['seed_title']), (label, 'template', seed, title))
            self.assertEqual(self.s.row_name(cid, 'en-US'), row['label'])
        plan = commands.preview(self.s, 'What are my rows called?', 'ana',
                                answer=dict(op='row_names', kind=None, values=[], person=None))
        self.assertTrue(plan['read_only'])
        self.assertEqual(plan['row_names']['rows'], list(names.values()))
        self.assertIn('template; tmdb:1, Invented Window', plan['description'])
        translated = row_names.view(self.data, self.s.CONFIG, 'de-DE')
        first = next(r for r in translated['rows'] if r['id'] == self.rows[0])
        self.assertEqual((first['label'], first['source']), ('Weil dir Invented Window gefallen hat', 'template'))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_custom_and_legacy_names_still_expose_the_seed(self):
        cfg = copy.deepcopy(self.s.CONFIG)
        cfg['household']['row_labels'] = {'because': 'Our Stories', 'leap': 'A New World'}
        for key, title in (('because', 'Because You Liked Invented Window'), ('leap', 'Same Feeling as Invented Window')):
            names = {'seed': 'tmdb:1', 'title': title}
            row = row_names.describe(self.s.rid(key), names, self.s.CONFIG, 'en-US')
            self.assertEqual((row['source'], row['label'], row['seed']), ('template', title, 'tmdb:1'))
            row = row_names.describe(self.s.rid(key), names, cfg, 'en-US')
            self.assertEqual((row['source'], row['label'], row['seed']), ('custom', cfg['household']['row_labels'][key], 'tmdb:1'))

    def test_inspector_keeps_row_language_and_localized_lab_pause_reason(self):
        s = self.s
        s.CONFIG['region']['row_lang'] = 'de-DE'
        s.CONFIG.setdefault('metrics', {})['enabled'] = False
        self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
        self.data.joinpath('feeds.json').write_text(json.dumps({s.rid('because') + '|en-US': {'names': {
            'seed': 'tmdb:1', 'label_key': 'row.seed.liked', 'fields': {'title': 'Invented Window'}}}}))
        self.data.joinpath('lab.json').write_text(json.dumps({'ideas': [{'id': 'one', 'name': 'Invented Puzzles'}]}))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        out = io.StringIO()
        with patch.dict('os.environ', {'DATA_DIR': str(self.data)}, clear=True), \
                patch('skald.__main__.Engine', side_effect=AssertionError('no engine')):
            self.assertEqual(main(['rows', 'names', '--json'], out=out), 0)
        data = json.loads(out.getvalue())
        self.assertEqual((data['lang'], data['row_lang'], data['language']), ('en-US', 'de-DE', 'de-DE'))
        plan = commands.preview(s, 'Wie heißen meine Reihen?', 'ana',
                                answer=dict(op='row_names', kind=None, values=[], person=None))
        self.assertEqual(plan['name'], i18n.t('command.row_names', 'de-DE'))
        self.assertEqual(plan['row_names'], data)
        self.assertEqual(plan['description'].splitlines()[0], i18n.t('lab.paused', 'de-DE'))
        row = next(row for row in data['rows'] if row['id'] == s.rid('because'))
        self.assertEqual((row['label'], row['seed'], row['seed_title']),
                         ('Weil dir Invented Window gefallen hat', 'tmdb:1', 'Invented Window'))
        self.assertIn('template; tmdb:1, Invented Window', plan['description'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_commander_shows_seed_without_a_resolved_title(self):
        for fields in (None, {}, {'title': ''}, {'title': None}):
            with self.subTest(fields=fields):
                cached = {self.s.rid(key) + '|en-US': {'names': {
                    'seed': 'tmdb:1', 'label_key': label_key, 'fields': fields}}
                    for key, label_key in (('because', 'row.seed.liked'), ('leap', 'row.feeling'))}
                self.data.joinpath('feeds.json').write_text(json.dumps(cached))
                before = {p.name: p.read_bytes() for p in self.data.iterdir()}
                plan = commands.preview(self.s, 'What are my rows called?', 'ana',
                                        answer=dict(op='row_names', kind=None, values=[], person=None))
                self.assertTrue(plan['read_only'])
                rows = {row['id']: row for row in plan['row_names']['rows']}
                for key in ('because', 'leap'):
                    cid = self.s.rid(key)
                    self.assertEqual(rows[cid]['seed'], 'tmdb:1')
                    self.assertIsNone(rows[cid]['seed_title'])
                    line = next(line for line in plan['description'].splitlines() if line.startswith(cid + ':'))
                    self.assertIn('(template; tmdb:1)', line)
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_dynamic_names_keep_the_seed_last_fetched(self):
        self.seeds()
        self.feeling()
        old = self.built()
        feeling = self.s.leap_feed('en-US')
        old['_names'].update(feeling.pop('_names'))
        old.update(feeling)
        self.s._shown.update(self.cache(old, 'shown.json'))
        self.p['entries'].pop(0)
        new = self.built()
        feeling = self.s.leap_feed('en-US')
        new['_names'].update(feeling.pop('_names'))
        new.update(feeling)
        self.s._feeds.update(self.cache(new))
        names = {r['id']: r for r in row_names.view(self.data, self.s.CONFIG)['rows']}
        for cid, title in ((self.rows[0], 'Because You Liked Invented Window'),
                           (self.s.rid('leap'), 'Same Feeling as Invented Window')):
            self.assertEqual(self.s.row_name(cid, 'en-US'), title)
            self.assertEqual((names[cid]['label'], names[cid]['seed']), (title, 'tmdb:1'))
            self.assertIn(cid, self.s.DYNAMIC)
