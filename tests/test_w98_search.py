"""Search routing and filtering with synthetic history and offline model replies."""
import tempfile
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server


class SearchRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.s = load_server(cls.temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        s = self.s
        s._ai.clear()
        s._last_search.clear()
        s._profiles.clear()
        s._model['m'] = None
        self.history = [dict(key=f'tmdb:{i}', id=i, title=f'Title {i}', year='2000',
                             stype='movie', kind='movie', age=i, weight=1,
                             signal='watched', by=[person], item={})
                        for i, person in ((1, 'ana'), (2, 'ben'))]
        s._profiles['household'] = {'entries': self.history, 'seen': {'tmdb:1', 'tmdb:2'},
                                    'negative': {'tmdb:3'}}
        self.route = {'choice': 'request', 'confidence': .99, 'watched': 'any', 'list': 'taste'}
        self.picks = [(f'Title {i}', '2000', 'movie', '') for i in range(1, 21)]
        for name, kwargs in (
                ('OR_KEY', {'new': 'offline'}), ('refresh_preferences', {}),
                ('looks_like_title', {'return_value': False}), ('superseded', {'return_value': False}),
                ('kids_age', {'return_value': None}), ('genres', {'return_value': {}}),
                ('search_both', {'return_value': ([], {})}), ('tmdb', {'return_value': {'results': []}}),
                ('resolve_pick', {'side_effect': self.resolve}),
                ('decide', {'return_value': {'answer': self.route}}),
                ('llm_picks', {'return_value': self.picks}),
                ('taste_rerank', {'side_effect': lambda metas: metas})):
            mock = patch.object(s, name, **kwargs)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        network = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.addCleanup(self.finish_pickers)

    def finish_pickers(self):
        for _, plan in self.s._ai.values():
            picks = plan.result(timeout=2)['picks']
            if picks is not None and not picks.cancelled():
                picks.result(timeout=2)

    def resolve(self, kind, title, year, lang):
        i = int(title.split()[-1])
        return {'id': i, 'title': title, 'name': title}, {}

    def results(self, query='synthetic request', stype='movie'):
        return self.s.search(stype, query, 'en-US')['metas']

    def test_havent_uses_router_to_hide_watched_and_negative(self):
        self.route['watched'] = 'unseen'
        ids = {m['id'] for m in self.results('Animes I havent watched')}
        self.assertTrue(ids)
        self.assertFalse(ids & {'tmdb:1', 'tmdb:2', 'tmdb:3'})

    def test_seen_resolves_only_history_without_tmdb_lookup(self):
        self.route['watched'] = 'seen'
        self.resolve_pick.side_effect = AssertionError('resolve from history')
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:1', 'tmdb:2'])

    def test_best_skips_taste_rerank(self):
        self.route['list'] = 'best'
        self.results('the greats across every decade')
        self.taste_rerank.assert_not_called()

    def test_picker_failure_preserves_unseen_and_seen_fallback_constraints(self):
        self.llm_picks.side_effect = TimeoutError('picker failed')
        self.search_both.return_value = ([{'id': i, 'title': f'Title {i}'} for i in range(1, 6)], {})
        for order in ('parallel', 'router_first'):
            for watched, expected in (('unseen', ['tmdb:4', 'tmdb:5']), ('seen', ['tmdb:1', 'tmdb:2'])):
                self.s._ai.clear()
                self.route.update(watched=watched, list='best')
                with self.subTest(order=order, watched=watched), patch.object(self.s, 'SEARCH_ORDER', order):
                    self.assertEqual([m['id'] for m in self.results()], expected)

    def test_failed_or_cancelled_picker_is_retried_after_recovery(self):
        failed = Future()
        failed.set_exception(TimeoutError('picker failed'))
        cancelled = Future()
        cancelled.cancel()
        self.route['watched'] = 'unseen'
        for picker in (failed, cancelled):
            self.s._ai.clear()
            recovered = Future()
            recovered.set_result(self.picks[:5])
            with self.subTest(picker=picker), patch.object(self.s, 'search_picks', side_effect=[picker, recovered]) as pick:
                self.assertEqual(self.results('Animes I havent watched'), [])
                self.assertEqual([m['id'] for m in self.results('Animes I havent watched')], ['tmdb:4', 'tmdb:5'])
                self.assertEqual(pick.call_count, 2)

    def test_failed_picker_without_a_search_consumer_is_not_reused(self):
        self.llm_picks.side_effect = [TimeoutError('picker failed'), self.picks[:5]]
        first = self.s.ai_future('synthetic request')
        with self.assertRaises(TimeoutError):
            first.result(timeout=2)['picks'].result(timeout=2)
        second = self.s.ai_future('synthetic request')
        self.assertIsNot(first, second)
        self.assertEqual(second.result(timeout=2)['picks'].result(timeout=2), self.picks[:5])

    def test_router_first_gives_picker_its_full_timeout_after_a_slow_router(self):
        for order in ('parallel', 'router_first'):
            clock = [0]
            picker, plan = Mock(spec=Future), Mock(spec=Future)
            def routed(timeout):
                clock[0] += 3
                return {**self.route, 'picks': picker}
            def picked(timeout):
                needed = 5 if order == 'router_first' else 2
                if timeout < needed:
                    raise TimeoutError('valid picks discarded')
                clock[0] += needed
                return self.picks[:5]
            plan.result.side_effect = routed
            picker.result.side_effect = picked
            with self.subTest(order=order), patch.object(self.s, 'SEARCH_ORDER', order), \
                    patch.object(self.s, 'ai_future', return_value=plan), \
                    patch.object(self.s.time, 'monotonic', side_effect=lambda: clock[0]):
                self.assertEqual([m['id'] for m in self.results()], ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])
                if order == 'router_first':
                    self.assertEqual(picker.result.call_args.kwargs['timeout'], 6)

    def test_orders_share_channels_and_cancel_command_picker(self):
        for route, expected in (('title', ['tmdb:1']), ('command', ['command-card']),
                                ('request', ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2']),
                                ('discover', ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])):
            self.route['choice'] = route
            self.llm_picks.return_value = self.picks[:5]
            self.search_both.return_value = ([{'id': 1, 'title': 'Other title'}], {})
            for order in ('parallel', 'router_first'):
                self.s._ai.clear()
                self.llm_picks.reset_mock()
                self.search_both.reset_mock()
                def command(ctx, query, stype, routed=False, results=None):
                    return {'metas': [{'id': 'command-card'}]} if routed else None
                with self.subTest(route=route, order=order), patch.object(self.s, 'SEARCH_ORDER', order, create=True), \
                        patch.object(self.s.search_commands, 'special', side_effect=command):
                    self.assertEqual([m['id'] for m in self.results()], expected)
                    self.finish_pickers()
                    if order == 'router_first' and route != 'title':
                        self.search_both.assert_not_called()

    def test_empty_picker_keeps_requested_type_empty_in_both_orders(self):
        self.llm_picks.return_value = []
        self.search_both.return_value = ([{'id': 1, 'title': 'Other title'}], {})
        for order in ('parallel', 'router_first'):
            self.s._ai.clear()
            with self.subTest(order=order), patch.object(self.s, 'SEARCH_ORDER', order):
                self.assertEqual(self.results(), [])

    def test_command_errors_keep_existing_error_path(self):
        self.route['choice'] = 'command'
        for order in ('parallel', 'router_first'):
            self.s._ai.clear()
            with patch.object(self.s, 'SEARCH_ORDER', order), \
                    patch.object(self.s.search_commands, 'special', side_effect=[None, RuntimeError('command failed')]), \
                    self.assertRaisesRegex(RuntimeError, 'command failed'):
                self.results()

    def test_empty_title_results_use_per_type_picker_in_both_orders(self):
        self.route.update(choice='title', confidence=.91)
        self.llm_picks.return_value = self.picks[:5]
        for order in ('parallel', 'router_first'):
            self.s._ai.clear()
            self.llm_picks.reset_mock()
            with self.subTest(order=order), patch.object(self.s, 'SEARCH_ORDER', order, create=True):
                self.assertEqual([m['id'] for m in self.results('dark movies')],
                                 ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])
                self.results('dark movies', 'series')
                self.assertEqual(self.llm_picks.call_count, 2)

    def test_router_first_taste_hint_and_best_omission(self):
        self.s._model['m'] = {'aff': {}}
        with patch.object(self.s, 'SEARCH_ORDER', 'router_first', create=True), \
                patch.object(self.s, 'taste_hint', return_value='cozy mysteries'):
            for listing in ('taste', 'best'):
                self.s._ai.clear()
                self.route['list'] = listing
                self.taste_rerank.reset_mock()
                self.results()
                prompt = self.llm_picks.call_args.args[1]
                self.assertIn('cozy mysteries', prompt)
                self.assertEqual(self.taste_rerank.called, listing == 'taste')

    def test_taste_reranks_only_unseen_then_moves_watched_last(self):
        self.llm_picks.return_value = self.picks[:5]
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])
        self.assertEqual([m['id'] for m in self.taste_rerank.call_args.args[0]], ['tmdb:4', 'tmdb:5'])

    def test_rows_are_capped_at_fifteen(self):
        self.llm_picks.return_value += [(f'Title {i}', '2000', 'series', '') for i in range(21, 41)]
        for stype in ('movie', 'series'):
            self.assertEqual(len(self.results(stype=stype)), 15)

    def test_unseen_top_up_keeps_ai_first_and_filters_candidates(self):
        self.route['watched'] = 'unseen'
        self.llm_picks.return_value = self.picks[:4]
        self.tmdb.return_value = {'results': [
            {'id': i, 'title': f'Title {i}', 'media_type': 'movie', 'adult': i == 5}
            for i in range(1, 25)] + [{'id': 30, 'name': 'Wrong type', 'media_type': 'tv'}]}
        metas = self.results()
        self.assertEqual(len(metas), 15)
        self.assertEqual(metas[0]['id'], 'tmdb:4')
        self.assertFalse({m['id'] for m in metas} & {'tmdb:1', 'tmdb:2', 'tmdb:3', 'tmdb:5', 'tmdb:30'})
        self.assertTrue(all(call.args[0] == 'movie/4/recommendations' for call in self.tmdb.call_args_list))

    def test_no_top_up_for_eight_kept_picks_or_seen_or_any(self):
        for watched, count in (('unseen', 11), ('seen', 4), ('any', 4)):
            self.s._ai.clear()
            self.route['watched'] = watched
            self.llm_picks.return_value = self.picks[:count]
            self.results()
        self.tmdb.assert_not_called()

    def test_top_up_keeps_age_filter_and_survives_recommendation_failure(self):
        self.route['watched'] = 'unseen'
        self.llm_picks.return_value = self.picks[:5]
        self.tmdb.side_effect = RuntimeError('offline recommendation failure')
        with patch.object(self.s, 'kids_age', return_value=8), patch.object(self.s, 'ensure_facets'), \
                patch.object(self.s, 'child_ok', side_effect=lambda meta, who: meta['id'] == 'tmdb:4'):
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:4'])
        self.s._ai.clear()
        self.tmdb.side_effect = None
        self.tmdb.return_value = {'results': [{'id': i, 'title': f'Title {i}'} for i in range(6, 25)]}
        with patch.object(self.s, 'kids_age', return_value=8), patch.object(self.s, 'ensure_facets'), \
                patch.object(self.s, 'child_ok', side_effect=lambda meta, who: meta['id'] not in {'tmdb:5', 'tmdb:6'}):
            metas = self.results()
        self.assertEqual(len(metas), 15)
        self.assertFalse({m['id'] for m in metas} & {'tmdb:5', 'tmdb:6'})

    def test_spellings_share_one_ai_answer_for_twenty_four_hours(self):
        self.s.ai_future("  Animes I HAVEN'T watched! ").result(timeout=2)["picks"].result(timeout=2)
        with patch.object(self.s.time, 'time', return_value=self.s.time.time() + 86300):
            self.s.ai_future('animes i havent watched').result(timeout=2)["picks"].result(timeout=2)
        self.llm_picks.assert_called_once()
        self.decide.assert_called_once()
        self.assertEqual(self.s.CACHE_TTL, 600)

    def test_cache_and_prompt_are_scoped_even_for_neutral_requests(self):
        with patch.object(self.s, 'PEOPLE', ('ana', 'ben')), \
                patch.object(self.s, 'SCOPES', {'ana': 'ana', 'ben': 'ben', 'together': 'both'}):
            for scope, titles in (('ana', [1]), ('ben', [2]), ('together', [1, 2])):
                with self.s.addon_scope(scope):
                    self.s.ai_future('synthetic request').result(timeout=2)["picks"].result(timeout=2)
                    prompt = self.llm_picks.call_args.args[1]
                    for i in (1, 2):
                        self.assertEqual(f'Title {i}|2000|m' in prompt, i in titles)
        self.assertEqual(self.llm_picks.call_count, 3)

    def test_picker_always_has_query_history_and_conditional_taste_hint(self):
        self.s._model['m'] = {'aff': {}}
        with patch.object(self.s, 'taste_hint', return_value='cozy mysteries'):
            self.s.ai_future('best titles I havent watched').result(timeout=2)["picks"].result(timeout=2)
        system, prompt, tokens, timeout, *_ = self.llm_picks.call_args.args
        for text in ('best titles I havent watched', 'Household taste', 'personal taste',
                     'ignored for best-of/consensus lists', 'cozy mysteries', 'Title 1|2000|m',
                     'new to the viewer', 'choose only from these'):
            self.assertIn(text, prompt)
        self.assertIn('18 lines', system)
        self.assertIn('movies only', system)
        self.assertEqual(tokens, 850)
        self.assertEqual(timeout, 6)

    def test_router_failure_or_timeout_uses_default_picker_behaviour(self):
        self.llm_picks.return_value = self.picks[:5]
        for error in (ValueError('bad reply'), TimeoutError('late reply')):
            with self.subTest(error=error):
                self.s._ai.clear()
                self.decide.side_effect = error
                self.assertEqual([m['id'] for m in self.results('havent watched')],
                                 ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])
        self.assertEqual(self.taste_rerank.call_count, 2)

    def test_router_fallback_answer_uses_defaults(self):
        self.route.update(choice=None, confidence=None)
        self.llm_picks.return_value = self.picks[:5]
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:4', 'tmdb:5', 'tmdb:1', 'tmdb:2'])

    def test_title_router_skips_picker_and_uses_plain_results(self):
        self.route.update(choice='title', watched='unseen')
        self.search_both.return_value = ([{'id': 1, 'title': 'Unseen'}], {})
        self.assertEqual([m['id'] for m in self.results('Find Unseen')], ['tmdb:1'])

    def test_decision_schema_optional_fields_and_parse_defaults(self):
        s = self.s
        s.check(s.DECISIONS['route_query']['out'], {'choice': 'request', 'confidence': .9})
        pair = s.route_parse({'answers': {'route': {'choice': 'request', 'confidence': .9}}}, [{}])[0]
        self.assertEqual(pair[0], {'choice': 'request', 'confidence': .9, 'watched': 'any', 'list': 'taste'})
        pair = s.route_parse({'answers': {'route': {'choice': 'request', 'confidence': .9},
                                        'watched': {'choice': 'unseen'}, 'list': {'choice': 'best'}}}, [{}])[0]
        self.assertEqual((pair[0]['watched'], pair[0]['list']), ('unseen', 'best'))
        self.assertEqual(s.DECISIONS['route_query']['ver'], 6)
