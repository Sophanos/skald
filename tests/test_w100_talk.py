"""Search conversations use synthetic titles and offline model replies only."""
import json
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import patch
from decide_kit import load_server, close_server
from test_api import raw_request

import test_w98_search as search_tests
import test_w93_commands as command_tests
from skald import commands as c, shelves


class Talk(unittest.TestCase):
    setUpClass = search_tests.SearchRouting.setUpClass.__func__
    setUpClass = classmethod(setUpClass)
    resolve = search_tests.SearchRouting.resolve
    results = search_tests.SearchRouting.results
    finish_pickers = search_tests.SearchRouting.finish_pickers

    def setUp(self):
        search_tests.SearchRouting.setUp(self)
        getattr(self.s, '_last_search', {}).clear()

    def http_results(self, query):
        path = f"/en-US/p/ana/catalog/movie/{self.s.rid('search')}/search={self.s.urllib.parse.quote(query, safe='')}.json"
        status, headers, raw, _ = raw_request(self.s, path, handler=self.s.Handler)
        self.assertEqual(status, 200)
        return headers, json.loads(raw)['metas']

    def test_adult_followup_responses_cannot_be_cached_by_the_client(self):
        self.http_results('dark movies')
        self.route['previous'] = 'refine'
        headers, _ = self.http_results('more like this')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.route['previous'] = 'new'
        self.llm_picks.return_value = [('Title 9', '2000', 'movie', '')]
        self.http_results('cozy movies')
        self.route['previous'] = 'refine'
        self.llm_picks.return_value = [('Title 10', '2000', 'movie', '')]
        headers, metas = self.http_results('more like this')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual([m['id'] for m in metas], ['tmdb:10'])

    def test_adult_timeout_response_cannot_hide_finished_picks_on_retry(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        def slow(*args):
            gate.wait(2)
            return self.picks[:5]
        self.llm_picks.side_effect = slow
        original = Future.result
        def wait(future, timeout=None):
            return original(future, timeout=.02 if timeout and timeout > 2 else timeout)
        with patch.object(Future, 'result', wait):
            headers, metas = self.http_results('dark movies')
        self.assertEqual(metas, [])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        gate.set()
        headers, metas = self.http_results('dark movies')
        self.assertTrue(metas)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.llm_picks.assert_called_once()

    def test_context_rows_ttl_and_scope(self):
        s = self.s
        with s.addon_scope('ana'):
            self.results('dark movies')
            ctx = s.last_search()
            self.assertEqual(ctx['query'], 'dark movies')
            self.assertEqual(ctx['answers']['watched'], 'any')
            self.assertEqual(len(ctx['rows']['movie']), 15)
            self.assertEqual(set(ctx['rows']['movie'][0]), {'key', 'kind', 'title', 'year'})
            self.results('dark movies', 'series')
            self.assertEqual(len(s.last_search()['rows']['movie']), 15)
            with patch.object(s.time, 'time', return_value=s.time.time() + 1800):
                self.assertIsNone(s.last_search())
        with s.addon_scope('ben'):
            self.assertIsNone(s.last_search())

    def test_title_search_sets_context(self):
        self.search_both.return_value = ([{'id': 8, 'title': 'Title 8', 'release_date': '2000-01-01'}], {})
        self.looks_like_title.return_value = True
        self.results('Title 8')
        self.assertEqual(self.s.last_search()['rows']['movie'], [
            dict(key='tmdb:8', kind='movie', title='Title 8', year='2000')])

    def test_skipped_keystroke_does_not_replace_the_last_search(self):
        self.results('dark movies')
        original = self.s.last_search()
        self.superseded.return_value = True
        self.search_both.return_value = ([{'id': 99, 'title': 'Partial match'}], {})
        self.assertEqual(self.results('but less violen')[0]['id'], 'tmdb:99')
        self.assertEqual(self.s.last_search(), original)
        self.superseded.return_value = False
        self.route['previous'] = 'refine'
        self.results('but less violent')
        prompt = self.llm_picks.call_args.args[1]
        for text in ('dark movies', 'Title 4', 'but less violent'):
            self.assertIn(text, prompt)
        self.assertNotIn('Partial match', prompt)

    def test_late_series_response_does_not_replace_refined_results(self):
        self.llm_picks.return_value = [('Title 4', '2000', 'movie', ''), ('Title 21', '2000', 'series', '')]
        self.results('dark movies')
        started, release = threading.Event(), threading.Event()
        def resolve(kind, title, year, lang):
            if kind == 'tv' and title == 'Title 21':
                started.set()
                if not release.wait(2):
                    raise AssertionError('late response was not released')
            return self.resolve(kind, title, year, lang)
        self.resolve_pick.side_effect = resolve
        with ThreadPoolExecutor(max_workers=1) as requests:
            older = requests.submit(self.results, 'dark movies', 'series')
            try:
                self.assertTrue(started.wait(2))
                self.route['previous'] = 'refine'
                self.llm_picks.return_value = [('Title 9', '2000', 'movie', '')]
                self.assertEqual(self.results('but less violent')[0]['id'], 'tmdb:9')
            finally:
                release.set()
            self.assertEqual(older.result(timeout=2)[0]['id'], 'tmdb:21')
        self.assertEqual(self.s.last_search()['query'], 'but less violent')
        self.route.update(previous='act', choice='command')
        def command(s, query, stype, routed=False, results=None):
            return {'metas': []} if routed else None
        with patch.object(c, 'special', side_effect=command) as special:
            self.results('save this as a row')
        self.assertEqual([x['key'] for x in self.s.shown_titles(special.call_args.kwargs['results'])], ['tmdb:9'])

    def test_empty_search_replaces_context_and_missing_year_is_null(self):
        self.results('dark movies')
        self.llm_picks.return_value = []
        self.results('empty wish')
        self.assertEqual(self.s.last_search()['query'], 'empty wish')
        self.assertEqual(self.s.last_search()['rows']['movie'], [])
        self.search_both.return_value = ([{'id': 8, 'title': 'Title 8'}], {})
        self.route.update(previous='new', choice='title')
        self.results('Title 8')
        self.assertIsNone(self.s.last_search()['rows']['movie'][0]['year'])

    def test_unbound_viewers_do_not_share_context(self):
        token = self.s._viewer.set('ana')
        try:
            self.results('dark movies')
            self.assertIsNotNone(self.s.last_search())
            other = self.s._viewer.set('ben')
            try:
                self.assertIsNone(self.s.last_search())
            finally:
                self.s._viewer.reset(other)
        finally:
            self.s._viewer.reset(token)

    def test_router_state_and_verbatim_questions(self):
        s = self.s
        inp = {'query': 'also some series', 'last': 'dark movies', 'shown': 'Title 4, Title 5, Title 6'}
        body = s.DECISIONS['route_query']['llm']['prompt']([s.route_norm(inp)])
        self.assertEqual(body['state'], 'Last search: dark movies\nSome results shown: Title 4, Title 5, Title 6\nTyped now: also some series')
        self.assertEqual(set(body['questions']), {'route', 'watched', 'list', 'previous'})
        self.assertEqual(set(body['questions']['previous']['criteria']), {'new', 'refine', 'act'})
        for key in ('watched', 'list'):
            self.assertEqual(body['questions'][key], s.ROUTE_Q[key])
        self.assertIn('combined wish', body['questions']['route']['instructions'])
        self.assertEqual(s.DECISIONS['route_query']['llm']['prompt']([{'query': 'Dark'}])['questions'], s.ROUTE_Q)

    def test_refine_picker_uses_context_and_current_router_answers(self):
        self.results('dark movies')
        self.route.update(previous='refine', watched='seen', list='best')
        self.results('more like the second one')
        prompt = self.llm_picks.call_args.args[1]
        for text in ('dark movies', 'more like the second one', 'Title 4', 'Title 5', 'tmdb:5', 'combined wish'):
            self.assertIn(text, prompt)
        self.assertNotIn('Household taste', prompt)
        self.assertEqual(self.s.last_search()['answers']['watched'], 'seen')

    def test_context_changes_cache_key_and_types_have_separate_pickers(self):
        with patch.object(self.s, 'search_picks', wraps=self.s.search_picks) as picker:
            self.results('dark movies')
            self.route['previous'] = 'refine'
            self.results('more like this')
            first = picker.call_count
            self.results('more like this', 'series')
            self.assertEqual(picker.call_count, first + 2)
            self.results('cozy movies')
            self.results('more like this')
            self.assertEqual(picker.call_count, first + 6)

    def test_router_cache_includes_ids_and_previous_answers(self):
        self.results('dark movies')
        ctx = self.s.last_search()
        first = self.s.ai_plan('more like this', [], ctx)
        original = self.decide.call_args.args[1]
        if first['picks'] is not None:
            first['picks'].result(timeout=2)
        ctx['rows']['movie'][0]['key'] = 'tmdb:999'
        ctx['answers']['watched'] = 'unseen'
        second = self.s.ai_plan('more like this', [], ctx)
        changed = self.decide.call_args.args[1]
        if second['picks'] is not None:
            second['picks'].result(timeout=2)
        self.assertNotEqual(self.s.route_norm(original), self.s.route_norm(changed))
        self.assertEqual(self.s.route_state(original), self.s.route_state(changed))

    def test_act_passes_shown_titles_to_command_parser(self):
        self.results('dark movies')
        self.route.update(previous='act', choice='command')
        def command(s, query, stype, routed=False, results=None):
            return {'metas': []} if routed else None
        self.llm_picks.reset_mock()
        with patch.object(c, 'special', side_effect=command) as special:
            self.results('save this as a row')
        self.assertTrue(special.call_args.kwargs['routed'])
        self.assertEqual(special.call_args.kwargs['results']['query'], 'dark movies')
        self.assertEqual(special.call_args.kwargs['results']['rows']['movie'][0]['title'], 'Title 4')
        self.assertLessEqual(self.llm_picks.call_count, 1)

    def test_followup_uses_router_instead_of_local_settings_rules(self):
        self.results('dark movies')
        self.route.update(previous='refine')
        self.results('more series')
        self.assertIn('more series', self.llm_picks.call_args.args[1])
        self.assertEqual(self.decide.call_args.args[0], 'route_query')

    def test_new_title_search_after_context_uses_current_text(self):
        self.results('dark movies')
        self.route.update(previous='new', choice='title')
        self.search_both.return_value = ([{'id': 8, 'title': 'Title 8'}], {})
        self.looks_like_title.return_value = True
        self.llm_picks.reset_mock()
        self.assertEqual([x['id'] for x in self.results('Title 8')], ['tmdb:8'])
        self.assertEqual(self.s.last_search()['query'], 'Title 8')
        self.assertLessEqual(self.llm_picks.call_count, 1)

    def test_slow_picker_stays_cached_until_it_finishes(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        def slow(*args):
            gate.wait(2)
            return self.picks[:5]
        self.llm_picks.side_effect = slow
        original = Future.result
        def wait(future, timeout=None):
            return original(future, timeout=.02 if timeout in (6, 7) or timeout and timeout > 2 else timeout)
        with patch.object(Future, 'result', wait):
            self.assertEqual(self.results('dark movies'), [])
        self.assertIsNone(self.s.last_search())
        self.assertEqual(len(self.s._ai), 1)
        gate.set()
        self.assertTrue(self.results('dark movies'))
        self.llm_picks.assert_called_once()
        self.decide.assert_called_once()


class ResultCommands(unittest.TestCase):
    say = command_tests.Household.say
    confirm = command_tests.Household.confirm
    feedback = command_tests.Household.feedback

    def setUp(self):
        command_tests.Household.setUp(self)
        self.results = dict(query='quiet mysteries', rows={'movie': [
            dict(key='tmdb:438631', kind='movie', title='Dune', year='2021'),
            dict(key='tmdb:329865', kind='movie', title='Arrival', year='2016')]})

    def preview(self, op, viewer='ana', bound=None):
        return c.preview(self.s, 'save these', viewer, bound,
                         answer=dict(op=op, kind=None, values=[], person=None), results=self.results)

    def test_stale_routed_save_cannot_replace_a_newer_shelf_preview(self):
        started, release = threading.Event(), threading.Event()
        def decide(name, inp):
            if name == 'parse_command':
                return {'answer': dict(op='shelf_from_results', kind=None, values=[], person=None)}
            if inp['query'] == 'save this as a row':
                started.set()
                if not release.wait(2):
                    raise AssertionError('delayed router was not released')
            act = inp['query'] in ('save this as a row', 'save these as a row')
            return {'answer': dict(choice='command' if act else 'request', previous='act' if act else 'new',
                                   watched='any', list='taste', confidence=.99)}
        with patch.object(self.s, 'OR_KEY', 'offline'), patch.object(self.s, 'refresh_preferences'), \
                patch.object(self.s, 'looks_like_title', return_value=False), patch.object(self.s, 'superseded', return_value=False), \
                patch.object(self.s, 'search_both', return_value=([], {})), patch.object(self.s, 'genres', return_value={}), \
                patch.object(self.s, 'decide', side_effect=decide), \
                patch.object(self.s, 'llm_picks', return_value=[('Dune', '2021', 'movie', '')]) as picks, \
                ThreadPoolExecutor(max_workers=1) as requests:
            self.s.search('movie', 'dark movies', 'en-US')
            older = requests.submit(self.s.search, 'movie', 'save this as a row', 'en-US')
            try:
                self.assertTrue(started.wait(2))
                picks.return_value = [('Arrival', '2016', 'movie', '')]
                self.s.search('movie', 'cozy movies', 'en-US')
                self.s.search('movie', 'save these as a row', 'en-US')
                pending = self.s.command_context()
                newer = c.get_pending(pending, 'w93')
                self.assertEqual([x['title'] for x in newer['seeds']], ['Arrival'])
                code = c.confirm_code(newer)
            finally:
                release.set()
            self.assertEqual(older.result(timeout=2), {'metas': []})
            self.assertEqual(c.get_pending(pending, 'w93'), newer)
            card = self.s.search('movie', 'yes '+code, 'en-US')['metas'][0]
            self.assertIn('Done:', card['name'])
        self.assertEqual(shelves.read(self.s)['wishes'][0]['name'], 'Cozy Movies')

    def test_like_results_confirms_all_titles_and_applies_once(self):
        answer = dict(op='like_results', kind=None, values=[], person=None)
        with patch.object(self.s, 'decide', return_value={'answer': answer}) as decide:
            card = c.special(self.s, 'I like these, save them as my taste', 'movie', routed=True, results=self.results)['metas'][0]
        self.assertIn('Dune', card['description'])
        self.assertIn('Arrival', card['description'])
        self.assertEqual(decide.call_args.args[1]['shown'], ['Dune', 'Arrival'])
        self.assertEqual(self.feedback(), {})
        self.say('yes')
        self.assertEqual(self.feedback(), {})
        code = c.confirm_code(c.get_pending(self.s, 'w93'))
        self.confirm()
        self.assertEqual({e['title'] for e in self.feedback().values()}, {'Dune', 'Arrival'})
        self.assertTrue(all(e['action'] == 'love' and e['who'] == 'ana' for e in self.feedback().values()))
        before = self.s.read_feedback()
        self.say('yes '+code)
        self.assertEqual(self.s.read_feedback(), before)

    def test_result_preview_identity_includes_title_keys(self):
        for op in ('shelf_from_results', 'like_results'):
            with self.subTest(op=op):
                self.results['rows']['movie'][0]['key'] = 'tmdb:438631'
                original = self.preview(op)
                self.results['rows']['movie'][0]['key'] = 'tmdb:900001'
                changed = self.preview(op)
                self.assertNotEqual(original['plan'], changed['plan'])
                self.assertNotEqual(c.confirm_code(original), c.confirm_code(changed))
                self.assertEqual(changed['plan'], self.preview(op)['plan'])
                if op == 'shelf_from_results':
                    c.pending(self.s)['w93'] = (self.s.time.time(), changed)
                    self.say('yes '+c.confirm_code(original))
                    self.assertEqual(shelves.read(self.s)['wishes'], [])
                    self.assertEqual(c.get_pending(self.s, 'w93'), changed)
                    self.confirm()
                    seed = self.s._feeds[self.s.rid('shelfraw_ana_1'), self.s.LANG]['candidates'][0]
                    self.assertEqual(seed['id'], 'tmdb:900001')

    def test_guest_and_child_gates_match_like_and_shelf(self):
        for op in ('like_results', 'shelf_from_results'):
            with self.subTest(op=op), self.assertRaises(PermissionError):
                self.preview(op, self.s.GUEST, self.s.GUEST)
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ben' else None):
            with self.assertRaises(PermissionError):
                self.preview('like_results', 'ben', 'ben')
            self.assertEqual(self.preview('shelf_from_results', 'ben', 'ben')['people'], ['ben'])

    def test_shelf_uses_existing_storage_and_keeps_pins_first(self):
        plan = self.preview('shelf_from_results')
        c.apply(self.s, plan)
        wish = shelves.read(self.s)['wishes'][0]
        self.assertEqual(wish['name'], 'Quiet Mysteries')
        with patch.object(self.s, 'ensure_facets'), patch.object(self.s, 'preference_ok', return_value=True), \
                patch.object(self.s, 'preference_order', side_effect=lambda metas, *a: list(reversed(metas))), \
                patch.object(self.s, 'member_scores', return_value={'tmdb:329865': 99}), \
                patch.object(self.s, 'llm_picks', side_effect=AssertionError('seeded shelf needs no model call')):
            row = shelves.fill(self.s, self.s.LANG)[self.s.rid('shelf_ana_1')]['tv']
        self.assertEqual([m['id'] for m in row], ['tmdb:438631', 'tmdb:329865'])
        self.say('undo')
        self.assertEqual(shelves.read(self.s)['wishes'], [])

    def test_pins_survive_restart_and_small_rows_are_served(self):
        c.apply(self.s, self.preview('shelf_from_results'))
        restarted = load_server(self.root)
        self.addCleanup(close_server, restarted)
        restarted.load_state()
        with patch.object(restarted, 'ensure_facets'), patch.object(restarted, 'preference_ok', return_value=True), \
                patch.object(restarted, 'member_scores', return_value={}), \
                patch.object(restarted, 'llm_picks', side_effect=AssertionError('no new picker')), restarted.addon_scope('ana'):
            row = restarted.shelf_items(restarted.rid('shelf_ana_1'), 'movie', restarted.LANG, 'ana')
        self.assertEqual([m['id'] for m in row], ['tmdb:438631', 'tmdb:329865'])
        self.assertIsNone(restarted.last_search())

    def test_results_ops_cannot_run_without_context(self):
        for op in ('like_results', 'shelf_from_results'):
            with self.subTest(op=op), self.assertRaises(ValueError):
                c.preview(self.s, 'save these', 'ana', answer=dict(op=op, kind=None, values=[], person=None))

    def test_unknown_year_can_be_saved(self):
        self.results['rows']['movie'][0]['year'] = None
        c.apply(self.s, self.preview('like_results'))
        self.assertEqual(len(self.feedback()), 2)

    def test_all_titles_are_validated_before_any_taste_save(self):
        self.results['rows']['movie'][1]['year'] = 'wrong'
        with self.assertRaises(ValueError):
            self.preview('like_results')
        self.assertEqual(self.feedback(), {})
