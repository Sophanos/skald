"""Settings operations use synthetic households and mocked title verification."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from decide_kit import load_server, close_server, ReplayLLM
from skald import commands as c, members, setup, migrate


class SettingsCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'SKALD_CONFIG': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        setup.publish(Path(self.temp.name), members.fresh_config(['ana', 'ben']))
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        self.net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def plan(self, op, kind=None, values=None, person=None):
        return c.preview(self.s, 'synthetic', 'ana', answer=dict(op=op, kind=kind, values=values or [], person=person))

    def test_glm_fallback_and_override(self):
        spec = self.s.DECISIONS['parse_command']
        self.assertEqual(spec['ver'], 20)
        fallback = spec['llm']['fallback']
        self.assertEqual(fallback['timeout'], 6)
        body = fallback['prompt']([{'query': 'hide anime'}])
        self.assertEqual((body['model'], body['reasoning']['effort'], body['max_tokens']), ('z-ai/glm-5.3-flash', 'low', 1200))
        with patch.dict(os.environ, {'SKALD_COMMAND_FALLBACK_MODEL': 'synthetic/model', 'SKALD_COMMAND_FALLBACK_EFFORT': 'none'}):
            other = load_server(self.temp.name)
            try:
                body = other.DECISIONS['parse_command']['llm']['fallback']['prompt']([{'query': 'hide anime'}])
                self.assertEqual((body['model'], body['reasoning']['effort'], body['max_tokens']), ('synthetic/model', 'none', 1200))
            finally:
                close_server(other)

    def test_tolerant_json_and_missing_keys(self):
        parse = self.s.DECISIONS['parse_command']['llm']['parse']
        for content in ('```json\n{"op":null}\n```', 'Here: {"op":null} done', 'prefix {"op":"like","values":["A {Title} \\\"B\\\""]} suffix'):
            answer = parse({'choices': [{'message': {'content': content}}]}, [])[0][0]
            self.assertEqual(set(answer), set(c.BASE))
            self.assertIsNone(answer['person'])
            self.assertIsNone(answer['kind'])
        self.assertIsNone(parse({'choices': [{'message': {'content': '{"op":null}'}}]}, [])[0][0]['values'])
        for content in ('no json', '{broken}', '[{"op":null'):
            with self.assertRaises(ValueError):
                parse({'choices': [{'message': {'content': content}}]}, [])

    def test_prompt_guards(self):
        prompt = c.command_prompt(self.s)
        for text in ('hide horror movies', 'whole type', 'Do not guess', 'questions', 'every key', 'null when unknown'):
            self.assertIn(text, prompt)

    def test_genre_hide_rule(self):
        answer = self.s.DECISIONS['parse_command']['rule']({'query': 'hide horror movies'}, {})[0]
        self.assertEqual((answer['op'], answer['kind'], answer['values']), ('exclude_genre', 'movie', ['Horror']))

    def test_soft_weights_apply_score_and_undo(self):
        metas = [dict(id='tmdb:1', type='movie', genres=['Horror']), dict(id='tmdb:2', type='movie', genres=['Comedy'])]
        with patch.object(self.s, 'frec', return_value={}):
            plan = self.plan('less', 'genre', ['Horror'])
            self.assertIn('Less', plan['name'])
            c.apply(self.s, plan)
            self.assertEqual(migrate.check(self.temp.name)['format'], migrate.FORMAT)
            self.assertTrue(self.s.preference_ok(metas[0], self.s.rid('foryou'), 'ana'))
            self.assertEqual(self.s.preference_order(metas, self.s.rid('foryou'), 'ana')[0]['id'], 'tmdb:2')
            c.undo(self.s, ['ana'])
            self.assertEqual(self.s.preference_order(metas, self.s.rid('foryou'), 'ana'), metas)
            c.apply(self.s, self.plan('more', 'genre', ['Horror']))
            self.assertEqual(self.s.preference_order(list(reversed(metas)), self.s.rid('foryou'), 'ana')[0]['id'], 'tmdb:1')
            self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['weights']['genre']['Horror'], 1)

    def test_runtime_and_undo(self):
        c.apply(self.s, self.plan('runtime', 'movie', [120]))
        with patch.object(self.s, 'frec', return_value={}):
            for runtime, allowed in ((100, True), (121, False), (None, False)):
                meta = dict(id='tmdb:1', type='movie', _row={'runtime': runtime})
                self.assertEqual(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'), allowed)
        c.undo(self.s, ['ana'])
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])
        for values in ([0], [True], [100, 120]):
            with self.assertRaises(ValueError):
                self.plan('runtime', 'movie', values)

    def test_seen_requires_verified_title_and_undo(self):
        with patch.object(self.s, 'resolve_pick', return_value=None):
            with self.assertRaises(ValueError):
                self.plan('seen', values=['Unknown'])
        self.assertEqual(self.s.read_feedback(), [])
        with patch.object(self.s, 'resolve_pick', return_value=({'id': 7, 'title': 'Arrival', 'release_date': '2016-01-01'}, {})) as resolve:
            plan = self.plan('seen', 'movie', ['Arrival'])
            resolve.assert_called_once()
            self.assertEqual(self.s.read_feedback(), [])
            c.apply(self.s, plan)
        self.assertEqual(self.s.read_feedback()[0]['action'], 'seen')
        c.undo(self.s, ['ana'])
        self.assertFalse(self.s.active_feedback(self.s.read_feedback()))

    def test_kids_age_undo_remains_available_to_adult(self):
        c.apply(self.s, self.plan('kids_age', values=[12], person='ben'))
        self.assertEqual(self.s.kids_age('ben'), 12)
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.s._commands_last = {None: ['ben']}
            self.assertIn('Undone', c.special(self.s, 'undo', 'movie')['metas'][0]['name'])
        self.assertIsNone(self.s.kids_age('ben'))
        for value in (2, 18, True):
            with self.assertRaises(ValueError):
                self.plan('kids_age', values=[value])

    def test_focus_people_uses_existing_credit_names(self):
        c.apply(self.s, self.plan('focus', 'people', ['Christopher Nolan']))
        metas = [dict(id='tmdb:1', type='movie'), dict(id='tmdb:2', type='movie')]
        def record(key, kind):
            return {'d': [[1, 'Christopher Nolan']]} if key == 'tmdb:2' else {}
        with patch.object(self.s, 'frec', side_effect=record):
            self.assertEqual(self.s.preference_order(metas, self.s.rid('foryou'), 'ana')[0]['id'], 'tmdb:2')
        c.undo(self.s, ['ana'])
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])

    def test_read_only_cards_and_typed_undo(self):
        before = (Path(self.temp.name)/'skald.json').read_bytes()
        for query in ('show my shelves', 'what are my settings'):
            plan = c.preview(self.s, query, 'ana')
            self.assertTrue(plan['read_only'])
            self.assertNotIn('plan', plan)
            self.assertEqual(c.apply(self.s, plan), plan)
            with patch.object(self.s, 'who_watching', return_value=('ana', None)):
                card = c.special(self.s, query, 'movie')['metas'][0]
                self.assertNotIn('yes ', card['name'])
        self.assertEqual(before, (Path(self.temp.name)/'skald.json').read_bytes())
        c.apply(self.s, self.plan('more', 'genre', ['Comedy']))
        undo = c.preview(self.s, 'undo', 'ana')
        self.assertEqual(undo['change']['op'], 'undo')
        c.apply(self.s, undo)
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])

    def test_schedules_rule_and_apply_undo(self):
        cases = [('every monday,friday 19-22 comedy', ['comedy', 'mon,fri', '19-22']),
                 ('montags morgens comedy', ['comedy', 'mon', '05-12']),
                 ('freitags abends comedy', ['comedy', 'fri', '18-24']),
                 ('wochentags morgens comedy', ['comedy', 'weekdays', '05-12']),
                 ('am Wochenende abends comedy', ['comedy', 'weekend', '18-24']),
                 ('every friday at 20:00 a comedy', ['comedy', 'fri', '20-24'])]
        for query, values in cases:
            with self.subTest(query=query):
                answer = c.parse_shelf(query, self.s)
                self.assertEqual(answer['values'], values)
                plan = c.preview(self.s, query, 'ana')
                c.apply(self.s, plan)
                c.undo(self.s, ['ana'])

    def test_new_config_fields_are_validated(self):
        cfg = c.config(self.s)
        for prefs in ({'weights': {'genre': {'Made up': 1}}}, {'weights': {'series': 2}}, {'runtime': {'movie': -1}}, {'focus': {'people': ['']}}):
            cfg['taste']['people']['ana']['prefs'] = prefs
            with self.subTest(prefs=prefs), self.assertRaises(ValueError):
                self.s.validate_config(cfg)

    def test_optional_settings_migration_preserves_existing_config(self):
        path = Path(self.temp.name)/'skald.json'
        before = path.read_bytes()
        self.assertEqual(migrate.FORMAT, 5)
        self.assertEqual(max(migrate.MIGRATIONS), migrate.FORMAT)
        migrate.MIGRATIONS[2](Path(self.temp.name))
        migrate.MIGRATIONS[2](Path(self.temp.name))
        self.assertEqual(path.read_bytes(), before)

    def test_row_commands_preview_apply_serving_and_undo(self):
        cases = [('rename For You to Ana Picks', 'row_rename', ['foryou', 'Ana Picks']),
                 ('call the top rated row Classics', 'row_rename', ['toprated', 'Classics']),
                 ('hide the top rated row', 'row_hide', ['toprated']),
                 ('remove Latest Digital Releases', 'row_hide', ['digital']),
                 ('Top 10 Reihe ausblenden', 'row_hide', ['top10']),
                 ('bring back the top rated row', 'row_show', ['toprated']),
                 ('put Top 10 above For You', 'row_move', ['top10', 'foryou'])]
        for query, op, values in cases:
            with self.subTest(query=query):
                answer = c.parse_local(query, self.s)
                self.assertEqual((answer['op'], answer['values']), (op, values))
        before = c.config(self.s)['household']
        with patch.object(self.s, 'feed', return_value={}), patch.object(self.s, 'shelf_rows', return_value=[]):
            c.apply(self.s, c.preview(self.s, cases[0][0], 'ana'))
            self.assertEqual(next(r[2] for r in self.s.feed_rows() if r[0] == self.s.rid('foryou')), 'Ana Picks')
            c.undo(self.s, ['ana'])
            c.apply(self.s, c.preview(self.s, 'hide Top 10', 'ana'))
            self.assertFalse(self.s.row_enabled(self.s.rid('top10')))
            c.undo(self.s, ['ana'])
            self.assertTrue(self.s.row_enabled(self.s.rid('top10')))
            c.apply(self.s, c.preview(self.s, 'show Latest Digital Releases', 'ana'))
            self.assertTrue(self.s.row_enabled(self.s.rid('digital')))
            c.undo(self.s, ['ana'])
            c.apply(self.s, c.preview(self.s, cases[-1][0], 'ana'))
            ids = [r[0] for r in self.s.feed_rows()]
            self.assertLess(ids.index(self.s.rid('top10')), ids.index(self.s.rid('foryou')))
            c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s)['household'], before)

    def test_row_undo_rejects_newer_household_changes(self):
        c.apply(self.s, self.plan('row_hide', values=['top10']))
        c.apply(self.s, c.preview(self.s, 'show Latest Digital Releases', 'ben'))
        with self.assertRaisesRegex(ValueError, 'later change first'):
            c.undo(self.s, ['ana'])
        self.assertEqual(len(c.journal(self.s)['ana']), 1)

    def test_unknown_rows_and_empty_labels_rejected(self):
        for op, values in [('row_hide', ['madeup']), ('row_rename', ['foryou', '']), ('row_move', ['top10', 'top10'])]:
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.plan(op, values=values)

    def test_soft_weights_have_a_bounded_effect(self):
        c.apply(self.s, self.plan('less', 'genre', ['Horror']))
        c.apply(self.s, self.plan('more', 'genre', ['Comedy']))
        metas = [dict(id='tmdb:1', type='movie', genres=['Horror'])] + [dict(id='tmdb:'+str(i), type='movie', genres=['Comedy']) for i in range(2, 12)]
        with patch.object(self.s, 'frec', return_value={}):
            ranked = self.s.preference_order(metas, self.s.rid('foryou'), 'ana')
        self.assertEqual(set(m['id'] for m in ranked), set(m['id'] for m in metas))
        self.assertLessEqual(ranked.index(metas[0]), 3)

    def test_hiding_a_row_keeps_the_other_rows_in_place(self):
        c.apply(self.s, self.plan('row_rename', values=['foryou', 'Picks']))
        with patch.object(self.s, 'feed', return_value={}), patch.object(self.s, 'shelf_rows', return_value=[]):
            before = [r[0] for r in self.s.feed_rows()]
            c.apply(self.s, self.plan('row_hide', values=['top10']))
            after = [r[0] for r in self.s.feed_rows()]
        self.assertEqual(after, [cid for cid in before if cid != self.s.rid('top10')])

    def test_typed_undo_of_child_age_requires_an_adult(self):
        c.apply(self.s, self.plan('kids_age', values=[12], person='ben'))
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'undo', 'ben')
        plan = c.preview(self.s, 'undo for ben', 'ana')
        c.apply(self.s, plan)
        self.assertIsNone(self.s.kids_age('ben'))

    def test_public_settings_include_row_labels_and_order(self):
        c.apply(self.s, self.plan('row_rename', values=['foryou', 'Ana Picks']))
        c.apply(self.s, self.plan('row_move', values=['top10', 'foryou']))
        public = self.s.public_config(c.config(self.s))['household']
        self.assertEqual(public['row_labels']['foryou'], 'Ana Picks')
        self.assertLess(public['row_order'].index('top10'), public['row_order'].index('foryou'))
        self.assertIn('Ana Picks', c.preview(self.s, 'what are my settings', 'ana')['description'])

    def test_runtime_facts_include_cached_movie_and_episode_minutes(self):
        for kind, details, minutes in [('movie', {'runtime': 95}, 95), ('tv', {'episode_run_time': [45]}, 45)]:
            with self.subTest(kind=kind), patch.object(self.s, 'tmdb', return_value=details):
                record = self.s.facet_record(kind, 1)
                self.assertEqual(record['runtime'], minutes)
            with patch.object(self.s, 'frec', return_value=record):
                self.assertEqual(self.s.row_facts({'id': 'tmdb:1', 'type': 'movie' if kind == 'movie' else 'series'})['runtime'], minutes)

    def test_seen_rejects_missing_or_nontext_title_before_lookup(self):
        with patch.object(self.s, 'resolve_pick') as lookup:
            for values in ([None], [123], [' ']):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    self.plan('seen', values=values)
            lookup.assert_not_called()

    def test_schedule_separators_need_real_day_words(self):
        self.assertEqual(c.parse_shelf('mondayandfriday mornings comedy')['op'], None)

    def test_child_age_typed_undo_confirmation_checks_current_adult(self):
        c.apply(self.s, self.plan('kids_age', values=[12], person='ben'))
        plan = c.preview(self.s, 'undo for ben', 'ana')
        c.pending(self.s)[None] = (self.s.time.time(), plan)
        query = 'yes '+c.confirm_code(plan)
        with patch.object(self.s, 'who_watching', return_value=('ben', None)):
            self.assertIn('grown-up', c.special(self.s, query, 'movie')['metas'][0]['name'])
        self.assertEqual(self.s.kids_age('ben'), 12)
        # Use a fresh result scope after the denied child confirmation.
        with patch.object(c, 'context', return_value=('adult', None, 'ana')):
            c.pending(self.s)['adult'] = (self.s.time.time(), plan)
            self.assertIn('Undone', c.special(self.s, query, 'movie')['metas'][0]['name'])
        self.assertIsNone(self.s.kids_age('ben'))

    def test_adult_can_update_a_child_age_and_undo_it(self):
        c.apply(self.s, self.plan('kids_age', values=[12], person='ben'))
        plan = c.preview(self.s, 'kids age 13 for ben', 'ana')
        c.pending(self.s)[None] = (self.s.time.time(), plan)
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            self.assertIn('Done:', c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')['metas'][0]['name'])
        self.assertEqual(self.s.kids_age('ben'), 13)
        c.undo(self.s, ['ben'], actor='ana')
        self.assertEqual(self.s.kids_age('ben'), 12)

    def test_untyped_genre_exclusion_include_and_journal_undo(self):
        for query in ('no horror', 'hide horror'):
            answer = c.parse_local(query, self.s)
            self.assertEqual((answer['op'], answer['kind'], answer['values']), ('exclude_genre', None, ['Horror']))
        plan = self.plan('exclude_genre', values=['Horror'])
        c.apply(self.s, plan)
        self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['excluded_genres'], {'movie': ['Horror'], 'series': ['Horror']})
        with patch.object(self.s, 'frec', return_value={}):
            for kind in ('movie', 'series'):
                meta = dict(id='tmdb:1', type=kind, genres=['Horror'], _row={'genre_ids': [27] if kind == 'movie' else [18]})
                self.assertFalse(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'))
        c.apply(self.s, self.plan('include_genre', values=['Horror']))
        self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['excluded_genres'], {'movie': [], 'series': []})
        c.undo(self.s, ['ana'])
        self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['excluded_genres']['series'], ['Horror'])
        c.undo(self.s, ['ana'])
        self.assertNotIn('prefs', self.s.CONFIG['taste']['people']['ana'])

    def test_typed_genre_exclusion_remains_movie_only(self):
        c.apply(self.s, c.preview(self.s, 'hide horror movies', 'ana'))
        self.assertEqual(self.s.CONFIG['taste']['people']['ana']['prefs']['excluded_genres'], {'movie': ['Horror']})
        with patch.object(self.s, 'frec', return_value={}):
            self.assertTrue(self.s.preference_ok(dict(id='tmdb:1', type='series', genres=['Horror']), self.s.rid('foryou'), 'ana'))

    def test_creation_phrases_preserve_the_theme_and_scope(self):
        cases = [('Create a new catalog about psygoloy crime', 'psygoloy crime', None),
                 ('make a row with 80s action movies', '80s action movies', None),
                 ('add a catalog for crime', 'crime', None),
                 ('new a shelf of Crime Movies', 'Crime Movies', None),
                 ('create a new list with Slow Crime for ana', 'Slow Crime', 'ana'),
                 ('neue Reihe mit Horrorfilmen', 'Horrorfilmen', None),
                 ('erstelle einen Katalog über Psychologie und Krimi', 'Psychologie und Krimi', None),
                 ('mach eine neue Liste mit Krimis für uns', 'Krimis', 'together')]
        for query, theme, person in cases:
            with self.subTest(query=query):
                answer = dict(op='shelf_add', kind=None, values=[theme, 'always'], person=person)
                self.assertEqual(c.parse_shelf(query, self.s), answer)
                self.assertIsNone(self.s.DECISIONS['parse_command']['rule']({'query': query}, {}))
                with ReplayLLM(self.s) as llm:
                    self.assertIsNone(self.s.DECISIONS['parse_command']['rule']({'query': query}, {}))
                    llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
                    self.assertEqual(self.s.decide('parse_command', {'query': query})['answer'], answer)
                    self.assertEqual(llm.calls, 1)
        for query in ('best catalog of crime movies', 'a list of crime movies', 'catalog about crime', 'something about crime tonight'):
            self.assertEqual(c.parse_shelf(query, self.s), c.NONE)

    def test_route_prompt_meaning_and_version_cache(self):
        spec = self.s.DECISIONS['route_query']
        self.assertEqual(spec['ver'], 10)
        criteria = spec['llm']['prompt']([{'query': 'Create a new catalog about psygoloy crime'}])['questions']['route']['criteria']
        self.assertIn('change or remember something for later', criteria['command'])
        self.assertIn('changes nothing saved', criteria['request'])
        query = 'Create a new catalog about psygoloy crime'
        inp = self.s.route_norm({'query': query})
        digest = self.s.hashlib.sha256(json.dumps(inp, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()[:32]
        self.s._decision_put('route_query|4|'+digest, {**spec, 'ver': 4}, {'choice': 'request', 'confidence': .9}, .9)
        with patch.object(self.s, 'openrouter', return_value={'answers': {'route': {'choice': 'command', 'confidence': .99}}}) as request:
            answer = self.s.decide('route_query', {'query': query})
        self.assertEqual(answer['answer']['choice'], 'command')
        self.assertEqual(answer['source'], 'llm')
        request.assert_called_once()

    def test_phone_creation_query_uses_structured_command_and_previews(self):
        answer = dict(op='shelf_add', kind=None, values=['psygoloy crime', 'always'], person=None)
        with ReplayLLM(self.s) as llm, patch.object(self.s, 'who_watching', return_value=('ana', None)), patch.object(self.s, 'ai_future', side_effect=AssertionError('command previews directly')), patch.object(self.s, 'search_both', side_effect=AssertionError('not a one-off search')):
            llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            card = self.s.search('movie', 'Create a new catalog about psygoloy crime', 'en-US')['metas'][0]
            self.assertEqual(llm.calls, 1)
        self.assertIn('shelf', card['name'])
        self.assertIn('Nothing changes', card['description'])
        self.assertEqual(self.s.shelf_list()['wishes'], [])

    def test_row_hide_and_show_keep_family_picks(self):
        c.apply(self.s, self.plan('kids_age', values=[12], person='ben'))
        with patch.object(self.s, 'feed', return_value={}):
            self.assertIn(self.s.rid('family'), [row[0] for row in self.s.feed_rows()])
            for op, key in (('row_hide', 'top10'), ('row_show', 'digital')):
                with self.subTest(op=op):
                    c.apply(self.s, self.plan(op, values=[key]))
                    self.assertTrue(self.s.row_enabled(self.s.rid('family')))
                    self.assertIn(self.s.rid('family'), [row[0] for row in self.s.feed_rows()])
                    c.undo(self.s, ['ana'])

    def test_worker_backfills_runtime_for_old_cached_series(self):
        from contextlib import ExitStack
        c.apply(self.s, self.plan('runtime', 'series', [45]))
        old = {'kind': 'tv', 'g': ['Drama'], 'lang': 'en', 'title': 'Synthetic Series'}
        self.s._facets['tmdb:7'] = copy.deepcopy(old)
        meta = dict(id='tmdb:7', type='series', _row={'runtime': None})
        self.assertFalse(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'))
        with ExitStack() as stack:
            tmdb = stack.enter_context(patch.object(self.s, 'tmdb', return_value={'episode_run_time': [42]}))
            for name in ('connectors_tick', 'feed_pass', 'backfill_countries', 'backfill_ratings', 'daily_jobs', 'save_learn', 'save_nzb', 'save_emb', 'save_facets'):
                stack.enter_context(patch.object(self.s, name))
            stack.enter_context(patch.object(self.s.time, 'sleep', side_effect=RuntimeError('cycle complete')))
            with self.assertRaisesRegex(RuntimeError, 'cycle complete'):
                self.s.feed_worker()
        self.assertEqual(self.s.frec('tmdb:7', 'tv'), {**old, 'runtime': 42})
        tmdb.assert_called_once_with('tv/7', language='en-US')
        self.assertTrue(self.s.preference_ok(meta, self.s.rid('foryou'), 'ana'))
        with patch.object(self.s, 'tmdb', side_effect=AssertionError('runtime already refreshed')):
            self.s.backfill_runtime()

    def test_undo_preview_rejects_a_changed_journal_entry(self):
        arrival = dict(key='tmdb:1', kind='movie', title='Arrival', year='2016')
        dune = dict(key='tmdb:2', kind='movie', title='Dune', year='2021')
        with patch.object(c, 'resolve', return_value=arrival):
            c.apply(self.s, self.plan('like', 'movie', ['Arrival']))
        stale = c.preview(self.s, 'undo', 'ana')
        with patch.object(c, 'resolve', return_value=dune):
            c.apply(self.s, self.plan('seen', 'movie', ['Dune']))
        before = c.journal(self.s)
        with self.assertRaisesRegex(ValueError, 'preview.*again'):
            c.apply(self.s, stale)
        self.assertEqual(c.journal(self.s), before)
        self.assertEqual(len(self.s.active_feedback(self.s.read_feedback())), 2)
        fresh = c.preview(self.s, 'undo', 'ana')
        self.assertNotEqual(stale['plan'], fresh['plan'])
        c.apply(self.s, fresh)
        self.assertEqual(list(self.s.active_feedback(self.s.read_feedback())), [('tmdb:1', 'ana')])

    def test_renamed_dynamic_row_names_reach_remote_and_api(self):
        c.apply(self.s, self.plan('row_rename', values=['because', 'My Crime Picks']))
        with patch.object(self.s, 'feed', return_value={'names': {'title': 'Generated Name', 'lane': 'k:20'}}), patch.object(self.s, 'row_metas', return_value=[]), patch.object(self.s, 'who_watching', return_value=('both', None)):
            self.assertEqual(self.s.row_name(self.s.rid('because'), 'en-US'), 'My Crime Picks')
            self.assertEqual(self.s.row_name(self.s.rid('because'), 'en-US', key='lane'), 'k:20')
            remote = self.s.remote_rows('en-US')['rows']
            api = self.s.rows_view({'viewer': None, 'query': {'person': 'ana', 'row': self.s.rid('because')}})['rows']
        for rows in (remote, api):
            self.assertEqual(next(row['name'] for row in rows if row['id'] == self.s.rid('because')), 'My Crime Picks')

    def test_cli_read_only_commands_print_the_answer_content(self):
        import io
        from skald.__main__ import main
        c.apply(self.s, self.plan('exclude_genre', values=['Horror']))
        self.s.shelf_change('ana', 'add', text='crime', name='Crime Shelf', apply=True)
        for query, expected in (('what are my settings', 'Horror'), ('show my shelves', 'Crime Shelf')):
            with self.subTest(query=query):
                out = io.StringIO()
                self.assertEqual(main(['command', '--person', 'ana', query], out=out), 0)
                self.assertIn(expected, out.getvalue())
                result = io.StringIO()
                self.assertEqual(main(['command', '--person', 'ana', query, '--json'], out=result), 0)
                self.assertIn(expected, json.loads(result.getvalue())['description'])
