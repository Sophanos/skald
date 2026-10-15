"""Wide serving and hedged legacy fallback with synthetic, offline transports."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server


class WideSearchTests(unittest.TestCase):
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
        s._facets.clear()
        s._model['m'] = {'paff': {'ana': {'x:cozy': 1}}}
        self.route = {'choice': 'request', 'watched': 'any', 'list': 'taste'}
        self.rules = {'from': None, 'to': None, 'exclude': []}
        self.wide = {'movie': 'Title 1|2000|m\nTitle 2|2001|m\nTitle 3|2002|m',
                     'series': 'Title 11|2000|s\nTitle 12|2001|s'}
        self.calls = []
        self.transport = None
        for name, kwargs in (
                ('OR_KEY', {'new': 'offline'}), ('refresh_preferences', {}),
                ('looks_like_title', {'return_value': False}), ('superseded', {'return_value': False}),
                ('kids_age', {'return_value': None}), ('genres', {'return_value': {}}),
                ('search_both', {'return_value': ([], {})}), ('tmdb', {'return_value': {'results': []}}),
                ('resolve_pick', {'side_effect': self.resolve}),
                ('decide', {'return_value': {'answer': self.route}}),
                ('taste_hint', {'return_value': 'cozy'}),
                ('score_key', {'side_effect': lambda model, key, person, **kw: (int(key[5:]), [])}),
                ('match_pct', {'side_effect': lambda model, score, person: 60 + score if score is not None else None}),
                ('taste_rerank', {'side_effect': AssertionError('no second rerank')}),
                ('openrouter', {'side_effect': self.reply})):
            mock = patch.object(s, name, **kwargs)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        mock = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        mock.start()
        self.addCleanup(mock.stop)
        self.jobs = []
        for pool in (s._pool, s._outer, s._search_pool):
            def capture(fn, *args, submit=pool.submit, **kwargs):
                job = submit(fn, *args, **kwargs)
                self.jobs.append(job)
                return job
            mock = patch.object(pool, 'submit', side_effect=capture)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(self.finish_jobs)

    def finish_jobs(self):
        for job in self.jobs:
            try:
                job.result(timeout=2)
            except Exception:
                pass

    def resolve(self, kind, title, year, lang):
        i = int(title.split()[-1])
        return {'id': i, 'title': title, 'name': title,
                'release_date': year, 'first_air_date': year}, {}

    def reply(self, path, body, timeout, lane, label, on_text=None, stop=None):
        self.calls.append((path, body, timeout, lane, label))
        if self.transport:
            reply = self.transport(path, body, timeout, lane, label)
            if on_text:
                on_text(reply['choices'][0]['message']['content'])
            return reply
        if label == 'rules':
            content = json.dumps(self.rules)
        else:
            stype = 'series' if body['messages'][0]['content'].startswith('You pick series') else 'movie'
            content = self.wide[stype] if label == 'wide_picks' else ('Title 9|2005|m' if stype == 'movie' else 'Title 19|2005|s')
            if isinstance(content, Exception):
                raise content
        if on_text:
            on_text(content)
        return {'choices': [{'message': {'content': content}}]}

    def results(self, stype='movie', query='something cozy tonight'):
        return self.s.search(stype, query, 'en-US')['metas']

    def labels(self):
        self.finish_jobs()
        return [c[-1] for c in self.calls]

    def test_blend_person_scope_cap_and_cached_legacy_hedge(self):
        self.wide['movie'] = '\n'.join(f'Title {i}|2000|m' for i in range(1, 21))
        with patch.object(self.s, 'PEOPLE', ('ana', 'ben')), \
                patch.object(self.s, 'SCOPES', {'ana': 'ana'}), self.s.addon_scope('ana'):
            metas = self.results()
            self.assertEqual([m['id'] for m in metas], [f'tmdb:{i}' for i in range(20, 5, -1)])
            self.assertTrue(all(c.args[2] == 'ana' for c in self.score_key.call_args_list))
            self.results()
        self.assertEqual(self.labels().count('picks'), 0)
        self.taste_rerank.assert_not_called()

    def test_picker_position_breaks_equal_taste(self):
        self.score_key.return_value = (1, [])
        self.score_key.side_effect = None
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:1', 'tmdb:2', 'tmdb:3'])

    def test_blend_uses_min_max_and_keeps_picker_weight(self):
        self.score_key.side_effect = lambda model, key, person, **kw: ({1: 98, 2: 99, 3: 40}[int(key[5:])], [])
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:1', 'tmdb:2', 'tmdb:3'])
        self.s._ai.clear()
        self.score_key.side_effect = lambda model, key, person, **kw: ({1: 40, 2: 41, 3: 41}[int(key[5:])], [])
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:2', 'tmdb:3', 'tmdb:1'])

    def test_raw_taste_survives_missing_calibration_and_rounded_percentiles(self):
        self.s._model['m']['cal'] = None
        self.match_pct.side_effect = None
        for pct in (None, 70):
            with self.subTest(pct=pct):
                self.s._ai.clear()
                self.match_pct.return_value = pct
                with patch.object(self.s, 'PEOPLE', ('ana', 'ben')), \
                        patch.object(self.s, 'SCOPES', {'ana': 'ana'}), self.s.addon_scope('ana'):
                    self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])
                self.assertTrue(all(c.args[2] == 'ana' for c in self.score_key.call_args_list))

    def test_unknown_raw_scores_use_neutral_taste(self):
        self.score_key.side_effect = lambda model, key, person, **kw: ({1: None, 2: 0, 3: 1}[int(key[5:])], [])
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:1', 'tmdb:2'])

    def test_facet_exclusions_snapshot_under_lock(self):
        s = self.s
        class GrowingFacets(dict):
            def items(self):
                iterator = iter(super().items())
                yield next(iterator)
                if not s._facets_lock.locked():
                    self['tmdb:99'] = {'kind': 'movie', 'title': 'Background title'}
                yield from iterator
        with patch.object(s, '_facets', GrowingFacets({'tmdb:50': {'kind': 'movie', 'title': 'Cached title'}})):
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])

    def test_reference_alias_excluded_by_resolved_typed_id_with_empty_cache(self):
        self.rules['exclude'] = ['Title 2']
        self.wide['movie'] += '\nTitelwind|2001|m'
        original = self.resolve
        def resolve(kind, title, year, lang):
            hit, art = original(kind, 'Title 2' if title == 'Titelwind' else title, year, lang)
            if hit['id'] == 2:
                hit.update(title='Titelwind', name='Titelwind')
            return hit, art
        self.resolve_pick.side_effect = resolve
        self.assertEqual([m['id'] for m in self.s.search('movie', 'movies like Title 2', 'de-DE')['metas']],
                         ['tmdb:3', 'tmdb:1'])

    def test_seen_resolves_all_scoped_history_picks_without_tmdb(self):
        entries = [dict(key=f'tmdb:{i}', id=i, title=f'Title {i}', year=str(1999 + i), stype='movie',
                        kind='movie', age=1, weight=1, signal='watched', by=[person], item={})
                   for i, person in ((1, 'ana'), (2, 'ana'), (3, 'ben'))]
        self.s._profiles['household'] = {'entries': entries, 'seen': {e['key'] for e in entries}}
        self.route['watched'] = 'seen'
        self.resolve_pick.side_effect = lambda kind, title, year, lang: self.resolve(kind, title, year, lang) if title == 'Title 1' else None
        with patch.object(self.s, 'PEOPLE', ('ana', 'ben')), \
                patch.object(self.s, 'SCOPES', {'ana': 'ana'}), self.s.addon_scope('ana'):
            self.assertEqual([m['id'] for m in self.results(query='anime I have watched')], ['tmdb:2', 'tmdb:1'])
        self.resolve_pick.assert_not_called()

    def test_reference_exclusion_survives_localized_resolution(self):
        self.rules['exclude'] = ['Title 2']
        original = self.resolve
        def resolve(*args):
            hit, art = original(*args)
            hit.update(title='Localized name', name='Localized name')
            return hit, art
        self.resolve_pick.side_effect = resolve
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:1'])

    def test_reference_id_exclusion_is_per_type(self):
        self.rules['exclude'] = ['Series reference']
        self.s._facets['tmdb:2@tv'] = {'kind': 'tv', 'title': 'Series reference'}
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])

    def test_rules_years_and_named_references(self):
        self.rules.update({'from': 2001, 'to': 2002, 'exclude': ['Title 2']})
        self.wide['movie'] += '\nTitle 4|2003|m'
        self.assertEqual([m['id'] for m in self.results(query='like Title 2 from 2001 to 2002')], ['tmdb:3'])
        call = next(c for c in self.calls if c[-1] == 'rules')
        self.assertEqual(call[0], '/api/v1/chat/completions')
        self.assertEqual(call[1]['model'], self.s.COMMAND_MODEL)
        self.assertEqual(call[1]['response_format'], {'type': 'json_object'})

    def test_unseen_and_disliked_are_filtered(self):
        self.route['watched'] = 'unseen'
        self.s._profiles['household'] = {'seen': {'tmdb:1'}, 'negative': {'tmdb:2'}}
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:3'])
        self.tmdb.assert_not_called()

    def test_seen_uses_scoped_history_and_child_rules(self):
        entry = dict(key='tmdb:2', id=2, title='Title 2', year='2001', stype='movie',
                     kind='movie', age=1, weight=1, signal='watched', item={})
        self.s._profiles['household'] = {'entries': [entry], 'seen': {'tmdb:2'}}
        self.route['watched'] = 'seen'
        with patch.object(self.s, 'kids_age', return_value=8), patch.object(self.s, 'ensure_facets'), \
                patch.object(self.s, 'child_ok', side_effect=lambda meta, who: meta['id'] == 'tmdb:2'):
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:2'])

    def test_failure_and_empty_fall_back_independently_and_cache(self):
        for empty in ('', 'Title 1|2000|s', 'Title 1|1990|m', TimeoutError('late'), ValueError('bad')):
            with self.subTest(empty=empty):
                self.s._ai.clear()
                self.s._last_search.clear()
                self.calls.clear()
                self.wide['movie'] = empty
                self.rules['from'] = 2000
                self.route['list'] = 'best'
                self.assertEqual([m['id'] for m in self.results()], ['tmdb:9'])
                self.assertEqual([m['id'] for m in self.results('series')], ['tmdb:12', 'tmdb:11'])
                self.results()
                self.results('series')
                self.assertEqual(self.labels().count('picks'), 1)
                self.assertEqual(self.labels().count('wide_picks'), 2)

    def test_filtered_empty_falls_back(self):
        self.route.update(watched='unseen', list='best')
        self.s._profiles['household'] = {'seen': {'tmdb:1', 'tmdb:2', 'tmdb:3'}}
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:9'])
        self.assertEqual(self.labels().count('picks'), 1)

    def test_empty_wide_failed_hedge_keeps_plain_fallback(self):
        def transport(path, body, timeout, lane, label):
            if label == 'picks':
                raise ValueError('bad legacy reply')
            content = json.dumps(self.rules) if label == 'rules' else ''
            return {'choices': [{'message': {'content': content}}]}
        self.transport = transport
        self.search_both.return_value = ([{'id': 8, 'title': 'Plain title'}], {})
        metas = self.results()
        self.assertTrue(metas[0]['id'].startswith(self.s.rid('cmd:')))
        self.assertEqual([m['id'] for m in metas[1:]], ['tmdb:8'])

    def test_series_failure_does_not_fall_back_movies(self):
        self.wide['series'] = TimeoutError('late series')
        self.route['list'] = 'best'
        self.assertEqual([m['id'] for m in self.results('series')], ['tmdb:19'])
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])
        self.assertEqual(self.labels().count('picks'), 1)

    def test_failed_legacy_after_empty_wide_keeps_timeout_cache_behaviour(self):
        wide, plan = Future(), Future()
        wide.search_wide = True
        wide.search_timing = {'ms': 1}
        wide.search_rules = Future()
        wide.search_rules.set_result(self.rules)
        wide.set_result([])
        plan.set_result({**self.route, 'picks': wide})
        legacy = Mock(spec=Future)
        legacy.search_wide = False
        legacy.search_timing = {}
        legacy.result.side_effect = TimeoutError('pending legacy')
        legacy.cancelled.return_value = False
        legacy.done.return_value = False
        wide.search_fallback = legacy
        with patch.object(self.s, 'ai_future', return_value=plan), \
                patch.object(self.s, 'search_picks', return_value=legacy), \
                patch.object(self.s, 'remember_search') as remember:
            self.assertTrue(self.results()[0]['id'].startswith(self.s.rid('cmd:')))
        remember.assert_called_once()
        self.assertEqual(self.s._scoping.search_timing['picker_ms'], 'timeout')

    def test_none_stays_empty_cached_without_fallback(self):
        self.wide['series'] = ' NONE\n'
        self.assertEqual(self.results('series'), [])
        self.assertEqual(self.results('series'), [])
        self.assertEqual(self.labels().count('wide_picks'), 1)
        self.assertEqual(self.labels().count('picks'), 0)

    def test_wide_constants_used_for_prompt_transport_and_wait(self):
        with patch.object(self.s, 'SEARCH_WIDE_SIZE', 40, create=True), \
                patch.object(self.s, 'SEARCH_WIDE_TIMEOUT', 11, create=True):
            self.results()
        call = next(c for c in self.calls if c[-1] == 'wide_picks')
        self.assertIn('40 lines', call[1]['messages'][0]['content'])
        self.assertIn('give 40 movies', call[1]['messages'][0]['content'])
        self.assertGreaterEqual(call[1]['max_tokens'], 2000)
        self.assertLess(call[2], self.s.SEARCH_TIMEOUT)

    def test_pending_wide_future_uses_timeout_then_legacy(self):
        wide = Mock(spec=Future)
        wide.search_wide = True
        wide.search_timing = {'ms': 12000}
        wide.search_rules = Future()
        wide.search_rules.set_result(self.rules)
        wide.result.side_effect = TimeoutError('pending')
        wide.cancel.return_value = False
        legacy = Future()
        legacy.search_wide = False
        legacy.search_timing = {'ms': 1}
        legacy.set_result([('Title 9', '2005', 'movie', '')])
        wide.search_fallback = legacy
        self.route['list'] = 'best'
        with patch.object(self.s, 'search_picks', side_effect=[wide, legacy]) as picks, \
                patch.object(self.s, 'SEARCH_WIDE_TIMEOUT', 10):
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:9'])
        self.assertGreater(wide.result.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(wide.result.call_args.kwargs['timeout'], self.s.SEARCH_TIMEOUT)
        picks.assert_called_once()

    def test_rules_use_combined_refinement(self):
        context = {'query': 'like Title 2 from the nineties', 'wish': 'like Title 2 from the nineties',
                   'rows': {}, 'at': self.s.time.time()}
        self.route['previous'] = 'refine'
        with patch.object(self.s, 'search_turn', return_value=context):
            self.results(query='after 2000 please')
        rules = next(c for c in self.calls if c[-1] == 'rules' and 'Typed now:' in c[1]['messages'][1]['content'])
        prompt = rules[1]['messages'][1]['content']
        self.assertIn(context['wish'], prompt)
        self.assertIn('Typed now: after 2000 please', prompt)

    def test_rules_future_timeout_keeps_wide_results(self):
        wide, plan = Future(), Future()
        wide.search_wide = True
        wide.search_timing = {'ms': 1}
        wide.search_rules = Mock(spec=Future)
        wide.search_rules.result.side_effect = TimeoutError('late rules')
        wide.set_result(self.s.parse_picks(self.wide['movie']))
        plan.set_result({**self.route, 'picks': wide})
        with patch.object(self.s, 'ai_future', return_value=plan):
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])
        self.assertNotIn('picks', self.labels())

    def test_wide_timeout_serves_parallel_picker_without_waiting_for_rules(self):
        release, picked = Event(), Event()
        self.route['list'] = 'best'
        def transport(path, body, timeout, lane, label):
            if label in ('wide_picks', 'rules'):
                release.wait(2)
                content = '' if label == 'wide_picks' else json.dumps(self.rules)
            else:
                Event().wait(.25)
                picked.set()
                content = 'Title 9|2005|m'
            return {'choices': [{'message': {'content': content}}]}
        self.transport = transport
        try:
            with patch.object(self.s, 'SEARCH_WIDE_TIMEOUT', .4):
                start = time.monotonic()
                self.assertEqual([m['id'] for m in self.results()], ['tmdb:9'])
                self.assertLess(time.monotonic() - start, .8)
                self.assertTrue(picked.is_set())
        finally:
            release.set()

    def test_nine_second_wide_transport_serves_fallback_within_eight_seconds(self):
        release = Event()
        self.route['list'] = 'best'
        def transport(path, body, timeout, lane, label):
            if label == 'wide_picks':
                release.wait(9)
                content = self.wide['movie']
            else:
                content = json.dumps(self.rules) if label == 'rules' else 'Title 9|2005|m'
            return {'choices': [{'message': {'content': content}}]}
        self.transport = transport
        try:
            start = time.monotonic()
            result = self.results()
            elapsed = time.monotonic() - start
            print(f'Fake nine-second wide transport: {elapsed:.3f}s')
            self.assertLess(elapsed, 8)
            self.assertEqual([m['id'] for m in result], ['tmdb:9'])
        finally:
            release.set()

    def test_one_deadline_bounds_routing_rules_and_resolution(self):
        for phase in ('route', 'rules', 'wide_resolution', 'fallback_resolution', 'title_lookup', 'genres', 'combined'):
            release = Event()
            self.addCleanup(release.set)
            self.s._ai.clear()
            self.s._last_search.clear()
            self.route['list'] = 'best'
            def transport(path, body, timeout, lane, label):
                if label == 'rules' and phase in ('rules', 'combined'):
                    release.wait()
                if label == 'wide_picks' and phase == 'fallback_resolution':
                    release.wait()
                content = json.dumps(self.rules) if label == 'rules' else self.wide['movie'] if label == 'wide_picks' else 'Title 9|2005|m'
                return {'choices': [{'message': {'content': content}}]}
            def route(*args, **kwargs):
                if phase == 'route':
                    release.wait()
                return {'answer': self.route}
            def resolve(kind, title, year, lang):
                if phase in ('wide_resolution', 'combined') and title != 'Title 9' or phase == 'fallback_resolution':
                    release.wait()
                return self.resolve(kind, title, year, lang)
            def lookup(*args, **kwargs):
                if phase == 'title_lookup':
                    release.wait()
                return [], {}
            def genres(*args, **kwargs):
                if phase == 'genres':
                    release.wait()
                return {}
            self.transport = transport
            try:
                with self.subTest(phase=phase), patch.object(self.s, 'SEARCH_TIMEOUT', 3), \
                        patch.object(self.s, 'SEARCH_HEAD_START', .1), \
                        patch.object(self.s, 'decide', side_effect=route), \
                        patch.object(self.s, 'resolve_pick', side_effect=resolve), \
                        patch.object(self.s, 'search_both', side_effect=lookup), \
                        patch.object(self.s, 'genres', side_effect=genres), \
                        patch.object(self.s, 'feed', return_value={'metas': {'tv': [{'id': 'tmdb:8', 'type': 'movie', 'name': 'Cached title'}]}}):
                    start = time.monotonic()
                    result = self.results()
                    self.assertLess(time.monotonic() - start, 4, 'search exceeded one deadline plus startup margin')
                    if phase in ('wide_resolution', 'combined'):
                        self.assertTrue(result[0]['id'].startswith(self.s.rid('cmd:')))
                        self.assertEqual([m['id'] for m in result[1:]], ['tmdb:8'])
            finally:
                release.set()
                self.finish_jobs()

    def test_wide_success_skips_backup_picker(self):
        self.enterContext(patch.object(self.s, 'SEARCH_HEAD_START', 30))
        started, release, finished = Event(), Event(), Event()
        def transport(path, body, timeout, lane, label):
            if label == 'picks':
                started.set()
                release.wait(2)
                finished.set()
                content = 'Title 9|2005|m'
            elif label == 'wide_picks':
                content = self.wide['movie']
            else:
                content = json.dumps(self.rules)
            return {'choices': [{'message': {'content': content}}]}
        self.transport = transport
        try:
            self.assertEqual([m['id'] for m in self.results()], ['tmdb:3', 'tmdb:2', 'tmdb:1'])
            self.assertFalse(started.is_set())
            self.assertFalse(finished.is_set())
        finally:
            release.set()

    def test_rules_and_picker_overlap(self):
        started = Event()
        def transport(path, body, timeout, lane, label):
            if label == 'rules':
                started.set()
                content = json.dumps(self.rules)
            else:
                self.assertTrue(started.wait(1), 'rules must run alongside picker')
                content = self.wide['movie']
            return {'choices': [{'message': {'content': content}}]}
        self.transport = transport
        self.assertTrue(self.results())

    def test_rules_fixture_replay(self):
        path = Path(__file__).resolve().parents[1] / 'eval/fixtures/decisions/search_rules.jsonl'
        for line in path.read_text().splitlines():
            row = json.loads(line)
            with self.subTest(query=row['in']['query']):
                self.rules = row['recorded']
                self.assertEqual(self.s.search_rules(row['in']['query']), row['expect'])

    def test_invalid_rules_are_ignored(self):
        for rules in ({'from': True}, {'to': '2000'}, {'from': 2002, 'to': 2001},
                      {'exclude': 'Title 1'}, {'exclude': [1]}, []):
            with self.subTest(rules=rules):
                self.rules = rules
                self.assertEqual(self.s.search_rules('something cozy'), {'from': None, 'to': None, 'exclude': []})
