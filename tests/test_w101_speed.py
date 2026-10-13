"""Search latency, identity and label changes with synthetic data only."""
import json
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_w98_search as search_base
import test_w92_commands as command_base
import test_w100_talk as talk_base
from test_api import raw_request
from skald import commands as c


class Search(unittest.TestCase):
    setUpClass = classmethod(search_base.SearchRouting.setUpClass.__func__)
    def setUp(self):
        self.real_llm_picks = self.s.llm_picks
        self.real_superseded = self.s.superseded
        search_base.SearchRouting.setUp(self)
    resolve = search_base.SearchRouting.resolve
    results = search_base.SearchRouting.results
    finish_pickers = search_base.SearchRouting.finish_pickers
    http_results = talk_base.Talk.http_results

    def test_typo_title_keeps_ready_picker_after_person_timeout_or_failure(self):
        self.route['choice'] = 'title'
        self.llm_picks.side_effect = self.real_llm_picks
        clock, wait = time.monotonic, Future.result
        origin = clock()
        def slow_person(path, **kwargs):
            time.sleep(.2)
            return {'results': []}
        for lookup in (slow_person, RuntimeError('synthetic person failure')):
            self.s._ai.clear()
            self.s._last_search.clear()
            self.tmdb.side_effect = lookup
            with self.subTest(lookup=lookup), patch.object(self.s, 'llm_text', return_value='Arrival|2016|m') as picker, \
                    patch.object(self.s, 'resolve_pick', return_value=({'id': 42, 'title': 'Arrival', 'release_date': '2016-01-01'}, {})), \
                    patch.object(self.s.time, 'monotonic', side_effect=lambda: (clock() - origin) * 50), \
                    patch.object(Future, 'result', lambda future, timeout=None: wait(future, None if timeout is None else timeout / 50)):
                start = clock()
                metas = self.results('Arrivl')
                elapsed = clock() - start
                self.assertEqual([(meta['id'], meta['name']) for meta in metas], [('tmdb:42', self.s.AI_MARK+'Arrival')])
                self.assertLess(elapsed, .18, 'typo fallback exceeded the scaled .14-second deadline')
                picker.assert_called_once()

    def test_slow_person_search_and_credits_respect_deadline(self):
        self.route['choice'] = 'title'
        self.llm_picks.return_value = []
        clock, wait = time.monotonic, Future.result
        origin = clock()
        def tmdb(path, **kw):
            time.sleep(.12)
            if path == 'search/person':
                return {'results': [{'id': 7, 'name': 'Tom Hanks'}]}
            return {'cast': [{'id': 90, 'title': 'Credit', 'media_type': 'movie'}]}
        self.tmdb.side_effect = tmdb
        for title in ('Tom Hanks', 'A normal title result', None):
            self.s._ai.clear()
            self.s._last_search.clear()
            self.search_both.return_value = ([{'id': 40, 'title': title}] if title else [], {})
            with self.subTest(title=title), patch.object(self.s.time, 'monotonic', side_effect=lambda: (clock() - origin) * 50), \
                    patch.object(Future, 'result', lambda future, timeout=None: wait(future, None if timeout is None else timeout / 50)):
                start = clock()
                metas = self.results('Tom Hanks')
                elapsed = clock() - start
            self.assertLess(elapsed, .18, 'person lookup exceeded the scaled .14-second search deadline')
            self.assertEqual([m['id'] for m in metas], ['tmdb:40'] if title else [])

    def test_nonempty_invalid_picker_falls_back_and_is_retried(self):
        self.llm_picks.side_effect = self.real_llm_picks
        self.search_both.return_value = ([{'id': 40, 'title': 'TMDB fallback'}], {})
        with patch.object(self.s, 'llm_text', return_value='Sorry, I cannot suggest anything.') as text:
            for _ in range(2):
                self.assertEqual([m['id'] for m in self.results('synthetic malformed picker')], ['tmdb:40'])
            self.assertEqual(text.call_count, 2)
            self.assertEqual(self.s._ai, {})

    def test_deliberate_empty_picker_stays_empty_and_cached(self):
        self.llm_picks.side_effect = self.real_llm_picks
        self.search_both.return_value = ([{'id': 40, 'title': 'TMDB fallback'}], {})
        for reply in ('', ' \n'):
            self.s._ai.clear()
            self.s._last_search.clear()
            with self.subTest(reply=reply), patch.object(self.s, 'llm_text', return_value=reply) as text:
                self.assertEqual(self.results('synthetic other type only'), [])
                self.assertEqual(self.results('synthetic other type only'), [])
                text.assert_called_once()

    def test_slow_identity_lookup_starts_ai_only_for_last_prefix(self):
        entered = {q: threading.Event() for q in ('Ince', 'Incep', 'Incept')}
        def identity(query, lang):
            entered[query].set()
            time.sleep(1)
            return False
        self.looks_like_title.side_effect = identity
        self.superseded.side_effect = self.real_superseded
        self.s._searches.clear()
        def lane(query):
            with self.s.addon_scope('ana'):
                self.s.log_search(query)
                return self.results(query)
        with patch.object(self.s, 'SEARCH_SETTLE', .02), ThreadPoolExecutor(max_workers=3) as player:
            jobs = []
            for query in entered:
                jobs.append(player.submit(lane, query))
                self.assertTrue(entered[query].wait(.5))
                if query != 'Incept':
                    time.sleep(.15)
            rows = [job.result(timeout=2) for job in jobs]
        self.assertEqual([call.args[1]['query'] for call in self.decide.call_args_list], ['Incept'])
        self.assertEqual([call.args[1].splitlines()[0] for call in self.llm_picks.call_args_list], ['Incept'])
        self.assertEqual(rows[:2], [[], []])
        self.assertTrue(rows[2])

    def test_paired_http_search_and_cached_retry_log_one_line(self):
        self.s._searches.clear()
        log = Path(self.s.DATA, 'searches.jsonl')
        log.unlink(missing_ok=True)
        query, gate = 'one logical AI search', threading.Barrier(2)
        def lane(stype):
            gate.wait(timeout=1)
            path = f"/en-US/p/ana/catalog/{stype}/{self.s.rid('search')}/search={self.s.urllib.parse.quote(query)}.json"
            status, _, _, _ = raw_request(self.s, path, handler=self.s.Handler)
            self.assertEqual(status, 200)
        with ThreadPoolExecutor(max_workers=2) as player:
            jobs = [player.submit(lane, stype) for stype in ('movie', 'series')]
            for job in jobs:
                job.result(timeout=2)
        self.http_results(query)
        records = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['q'], query)
        self.assertIn('timing', records[0])

    def test_context_picker_starts_before_slow_router_and_meets_deadline(self):
        self.results('first wish')
        started = threading.Event()
        parallel = []
        def route(*args):
            parallel.append(started.wait(.1))
            time.sleep(.08)
            return {'answer': {**self.route, 'previous': 'new'}, 'source': 'llm'}
        self.decide.side_effect = route
        def pick(*args):
            started.set()
            time.sleep(.06)
            return self.picks[:5]
        self.llm_picks.side_effect = pick
        original = Future.result
        def wait(future, timeout=None):
            return original(future, timeout=.15 if timeout and timeout > 2 else timeout)
        with patch.object(Future, 'result', wait):
            self.assertTrue(self.results('next wish'))
        self.assertEqual(parallel, [True])

    def test_per_type_cache_and_prompt(self):
        movie = self.s.ai_future('synthetic wish', stype='movie')
        series = self.s.ai_future('synthetic wish', stype='series')
        self.assertIsNot(movie, series)
        self.assertIs(movie, self.s.ai_future('SYNTHETIC WISH', stype='movie'))
        for future in (movie, series):
            future.result(timeout=2)['picks'].result(timeout=2)
        self.assertEqual(self.llm_picks.call_count, 2)
        for call in self.llm_picks.call_args_list:
            system, _, tokens, *_ = call.args
            self.assertIn('18 lines', system)
            self.assertTrue('Title|Year|m.' in system or 'Title|Year|s.' in system)
            self.assertNotIn('m or s', system)
            self.assertLess(tokens, 1400)
            self.assertNotIn('at least 12', system)
        systems = [call.args[0] for call in self.llm_picks.call_args_list]
        self.assertEqual(sum('This catalog shows movies only' in system for system in systems), 1)
        self.assertEqual(sum('This catalog shows series only' in system for system in systems), 1)

    def test_movie_and_series_pickers_run_together(self):
        movie, series = threading.Event(), threading.Event()
        def pick(system, *args):
            mine, other = (movie, series) if 'Title|Year|m.' in system else (series, movie)
            mine.set()
            self.assertTrue(other.wait(1), 'other catalog must not wait for this picker')
            return self.picks[:5]
        self.llm_picks.side_effect = pick
        with ThreadPoolExecutor(max_workers=2) as requests:
            movie_row = requests.submit(self.results, 'parallel catalogs', 'movie')
            series_row = requests.submit(self.results, 'parallel catalogs', 'series')
            self.assertTrue(movie_row.result(timeout=2))
            self.assertEqual(series_row.result(timeout=2), [])

    def test_ordered_player_session_keeps_both_lanes_within_deadline(self):
        s, deadline, scale = self.s, .7, 10
        with s._decide_lock:
            s._decide_cache.clear()
        clock, future_result = time.monotonic, Future.result
        origin, lock = clock(), threading.Lock()
        foreign = 'etwas Warmes und Lustiges für heute Abend'
        routes = {'dark movies': ('request', 'new'), 'but less violent': ('request', 'refine'),
                  'Dark': ('title', 'refine'), foreign: ('request', 'new')}
        raw_starts, pick_ends, router_answers, router_calls = {}, {}, {}, []
        prompts, inputs, fallbacks = [], [], []

        def title_results(kind, query, lang):
            if query not in ('Dune', 'Dark'):
                return [], {}
            ident = (801 if query == 'Dune' else 901) + (kind == 'tv')
            return [{'id': ident, 'title': query, 'name': query}], {}

        def pick(system, prompt, tokens, timeout, *args):
            stype = 'movie' if 'Title|Year|m.' in system else 'series'
            combined = prompt.startswith('Answer the combined wish.')
            query = (prompt.split('Typed now: ', 1)[1] if combined else prompt).splitlines()[0]
            with lock:
                prompts.append((system, tokens, stype))
                if not combined:
                    raw_starts[query, stype] = clock()
            time.sleep(.5 if stype == 'movie' else .3)
            with lock:
                pick_ends[query, stype, combined] = clock()
            first = (501 if combined else 101) + (100 if stype == 'series' else 0)
            return [(f'Title {i}', '2000', stype, '') for i in range(first, first + 18)]

        with ThreadPoolExecutor(max_workers=4) as routers, ThreadPoolExecutor(max_workers=2) as player:
            def transport(path, body, timeout, lane, label):
                query = body['state'].split('Typed now: ', 1)[-1]
                choice, previous = routes[query]
                with lock:
                    router_calls.append((query, timeout))
                def reply():
                    time.sleep(.45 if query == 'dark movies' else .12)
                    return {'answers': {'route': {'choice': choice, 'confidence': .99},
                                        'watched': {'choice': 'any'}, 'list': {'choice': 'best'},
                                        'previous': {'choice': previous}}}
                return future_result(routers.submit(reply), timeout=timeout)

            def route(name, inp):
                answer = s.decide_many(name, [inp])[0]
                with lock:
                    inputs.append(dict(inp))
                    router_answers.setdefault(inp['query'], []).append(clock())
                    if answer['source'] == 'fallback':
                        fallbacks.append(answer['why'])
                return answer

            def scaled_wait(future, timeout=None):
                return future_result(future, timeout=None if timeout is None else timeout / scale)

            def lane(stype, query, barrier):
                with s.addon_scope('ana'):
                    barrier.wait(timeout=1)
                    start = clock()
                    result = s.search(stype, query, 'de-DE' if query == foreign else 'en-US')
                    finish = clock()
                    return result['metas'], finish - start, finish

            self.looks_like_title.side_effect = lambda query, lang: query == 'Dune'
            self.search_both.side_effect = title_results
            self.llm_picks.side_effect = pick
            self.decide.side_effect = route
            # Scale production deadline arithmetic and Future waits, leaving real sleeps unchanged.
            with patch.object(s.time, 'monotonic', side_effect=lambda: (clock() - origin) * scale), \
                    patch.object(Future, 'result', scaled_wait), \
                    patch.dict(s.DECISIONS['route_query']['llm'], timeout=.25), \
                    patch.object(s, 'openrouter', side_effect=transport):
                steps = [('Dune', 'title', None), ('dark movies', 'request', 'Dune'),
                         ('but less violent', 'refine', 'dark movies'), ('history', 'control', None),
                         ('Dark', 'title', 'but less violent'), (foreign, 'request', 'Dark')]
                for query, intent, previous in steps:
                    with self.subTest(query=query):
                        barrier = threading.Barrier(2)
                        jobs = {stype: player.submit(lane, stype, query, barrier) for stype in ('movie', 'series')}
                        rows = {stype: future_result(job, timeout=1) for stype, job in jobs.items()}
                        for stype, (metas, elapsed, _) in rows.items():
                            if intent in ('request', 'refine'):
                                self.assertTrue(metas, (query, stype, 'empty request lane'))
                                self.assertTrue(all(meta['type'] == stype for meta in metas))
                            elif intent == 'title':
                                ident = (801 if query == 'Dune' else 901) + (stype == 'series')
                                self.assertEqual([(m['id'], m['name']) for m in metas], [(f'tmdb:{ident}', query)])
                            self.assertLess(elapsed, deadline, (query, stype, elapsed))
                        if intent == 'control':
                            self.assertEqual(rows['movie'][0][0]['name'], 'History for Ana')
                            self.assertTrue(rows['movie'][0][0]['id'].startswith(s.rid('cmd:')))
                            self.assertEqual(rows['series'][0], [])
                            with s.addon_scope('ana'):
                                self.assertEqual(s.last_search()['query'], 'but less violent')
                        if previous:
                            self.assertEqual([i['last'] for i in inputs if i['query'] == query], [previous, previous])
                            for stype in rows:
                                self.assertLess(raw_starts.get((query, stype), float('inf')), min(router_answers[query]),
                                                (query, stype, 'raw picker must precede router answer with context'))
                        if intent in ('request', 'refine'):
                            combined = intent == 'refine'
                            self.assertLess(rows['series'][2], pick_ends[query, 'movie', combined],
                                            'series waited for the longer movie answer')
                            first = 501 if combined else 101
                            self.assertEqual(rows['movie'][0][0]['id'], f'tmdb:{first}')
                            self.assertEqual(rows['series'][0][0]['id'], f'tmdb:{first + 100}')

        self.assertEqual(sum(query == 'dark movies' for query, _ in router_calls), 1)
        self.assertTrue(all(timeout == .25 for _, timeout in router_calls))
        self.assertEqual(fallbacks.count('TimeoutError'), 1)
        self.assertTrue(prompts)
        for system, tokens, stype in prompts:
            self.assertIn('18 lines', system)
            self.assertIn('This catalog shows movies only' if stype == 'movie' else 'This catalog shows series only', system)
            self.assertEqual(tokens, 850)

    def test_refine_drops_raw_picker_and_uses_combined_wish(self):
        self.results('first wish')
        self.route['previous'] = 'refine'
        raw_started, release = threading.Event(), threading.Event()
        def route(*args):
            self.assertTrue(raw_started.wait(1))
            return {'answer': self.route}
        def pick(system, prompt, *args):
            if prompt.startswith('next wish'):
                raw_started.set()
                release.wait(1)
                return self.picks[:5]
            self.assertIn('Answer the combined wish', prompt)
            return [('Title 10', '2000', 'movie', '')]
        self.decide.side_effect = route
        self.llm_picks.side_effect = pick
        try:
            self.assertEqual([m['id'] for m in self.results('next wish')], ['tmdb:10'])
        finally:
            release.set()

    def test_command_and_act_cancel_speculative_picker(self):
        context = {'query': 'last wish', 'rows': {}}
        for answer in (dict(choice='command'), dict(choice='request', previous='act')):
            picker = Future()
            with self.subTest(answer=answer), patch.object(self.s, 'search_picks', return_value=picker) as picks:
                self.decide.return_value = {'answer': answer}
                self.s.ai_plan('change this', [], context)
                self.assertTrue(picker.cancelled())
                picks.assert_called_once()

    def test_act_plan_is_cached_after_cancelling_raw_picker(self):
        self.results('first wish')
        self.route.update(choice='request', previous='act')
        with patch.object(self.s, 'search_picks', side_effect=lambda *a, **kw: Future()) as picks:
            first = self.s.ai_future('save these')
            self.assertTrue(first.result(timeout=1)['picks'].cancelled())
            self.assertIs(first, self.s.ai_future('save these'))
            picks.assert_called_once()

    def test_router_timeout_is_two_and_a_half_seconds(self):
        self.assertEqual(self.s.DECISIONS['route_query']['llm']['timeout'], 2.5)

    def test_exact_title_in_either_type_beats_refine(self):
        self.results('first wish')
        self.route.update(choice='title', previous='refine')
        self.search_both.side_effect = lambda kind, *a: ([{'id': 40, 'name': 'Dark'}], {}) if kind == 'tv' else ([], {})
        self.assertEqual(self.results('Dark', 'movie'), [])
        self.assertEqual([m['id'] for m in self.results('Dark', 'series')], ['tmdb:40'])

    def test_other_type_hits_do_not_fall_back_to_picker(self):
        self.route['choice'] = 'title'
        self.search_both.side_effect = lambda kind, *a: ([{'id': 40, 'name': 'Kaulitz & Kaulitz'}], {}) if kind == 'tv' else ([], {})
        self.assertEqual(self.results('Kaulitz'), [])
        self.search_both.side_effect = None
        self.assertTrue(self.results('Arrivl'))

    def test_other_type_partial_hits_stay_empty_even_with_refine(self):
        self.results('first wish')
        self.route.update(choice='title', previous='refine')
        self.search_both.side_effect = lambda kind, *a: ([{'id': 40, 'name': 'Kaulitz & Kaulitz'}], {}) if kind == 'tv' else ([], {})
        self.assertEqual(self.results('Kaulitz'), [])

    def test_exact_person_credits_and_exact_titles_first(self):
        self.route['choice'] = 'title'
        self.search_both.return_value = ([{'id': 50, 'title': 'Jose Actor', 'popularity': 1}], {})
        def tmdb(path, **kw):
            if path == 'search/person':
                return {'results': [{'id': 7, 'name': 'José  Actor', 'original_name': 'Jose Actor'}]}
            self.assertEqual(path, 'person/7/combined_credits')
            return {'cast': [{'id': i, 'title': f'Credit {i}', 'media_type': 'movie', 'popularity': i, 'adult': i == 21}
                             for i in range(1, 22)],
                    'crew': [{'id': 30, 'title': 'Directed', 'media_type': 'movie', 'job': 'Director', 'popularity': 100},
                             {'id': 31, 'title': 'Written', 'media_type': 'movie', 'job': 'Writer', 'popularity': 200},
                             {'id': 32, 'name': 'Series', 'media_type': 'tv', 'job': 'Director', 'popularity': 300},
                             {'id': 33, 'title': 'Directing crew', 'media_type': 'movie', 'department': 'Directing',
                              'job': 'First Assistant Director', 'popularity': 150}]}
        self.tmdb.side_effect = tmdb
        metas = self.results('Jose Actor')
        self.assertEqual(len(metas), 15)
        self.assertEqual([m['id'] for m in metas[:4]], ['tmdb:50', 'tmdb:33', 'tmdb:30', 'tmdb:20'])
        self.assertFalse({'tmdb:21', 'tmdb:31', 'tmdb:32'} & {m['id'] for m in metas})
        with patch.object(self.s, 'kids_age', return_value=10), patch.object(self.s, 'ensure_facets'), \
                patch.object(self.s, 'child_ok', side_effect=lambda m, _: m['id'] == 'tmdb:30'):
            self.assertEqual([m['id'] for m in self.results('Jose Actor')], ['tmdb:30'])

    def test_non_exact_person_does_not_serve_credits(self):
        self.route['choice'] = 'title'
        self.tmdb.return_value = {'results': [{'id': 7, 'name': 'Jose Actor Jr'}]}
        self.assertTrue(self.results('Jose Actor'))
        self.assertTrue(all(x.args[0] == 'search/person' for x in self.tmdb.call_args_list))

    def test_person_original_name_identity_and_empty_type(self):
        self.route['choice'] = 'title'
        def tmdb(path, **kw):
            if path == 'search/person':
                return {'results': [{'id': 7, 'name': 'Translated', 'original_name': 'Жозе Актор'}]}
            return {'cast': [{'id': 20, 'title': 'Credit', 'media_type': 'movie'}]}
        self.tmdb.side_effect = tmdb
        self.assertEqual([m['id'] for m in self.results('  ЖОЗЕ   АКТОР ')], ['tmdb:20'])
        self.assertEqual(self.results('  ЖОЗЕ   АКТОР ', 'series'), [])

    def test_ai_log_has_timings_and_no_result_titles(self):
        self.s._searches.clear()
        log = Path(self.s.DATA, 'searches.jsonl')
        log.unlink(missing_ok=True)
        self.http_results('synthetic timing wish')
        self.http_results('synthetic timing wish')
        self.assertEqual(len(log.read_text().splitlines()), 1)
        with patch.object(self.s.time, 'time', return_value=self.s.time.time()+31):
            self.http_results('synthetic timing wish')
        rows = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            timing = row['timing']
            self.assertEqual(set(timing), {'router_ms', 'router_source', 'picker_ms', 'resolve_ms', 'channel', 'rows'})
            self.assertEqual(timing['channel'], 'picker')
            self.assertEqual(timing['rows'], {'movie': 15})
            self.assertNotIn('Title ', json.dumps(row))
        self.assertEqual(rows[-1]['timing']['picker_ms'], 'cached')

    def test_ai_log_records_fallback_source_and_picker_timeout(self):
        self.decide.return_value = {'answer': {'choice': None}, 'source': 'fallback'}
        self.http_results('synthetic router fallback')
        row = json.loads(Path(self.s.DATA, 'searches.jsonl').read_text().splitlines()[-1])
        self.assertEqual(row['timing']['router_source'], 'fallback')
        release = threading.Event()
        def pick(*args):
            release.wait(1)
            return self.picks[:5]
        self.llm_picks.side_effect = pick
        original = Future.result
        def wait(future, timeout=None):
            return original(future, timeout=.02 if timeout and timeout > 2 else timeout)
        try:
            with patch.object(Future, 'result', wait):
                _, metas = self.http_results('synthetic picker timeout')
            self.assertEqual(metas, [])
            row = json.loads(Path(self.s.DATA, 'searches.jsonl').read_text().splitlines()[-1])
            self.assertEqual(row['timing']['picker_ms'], 'timeout')
            self.assertEqual(row['timing']['rows'], {'movie': 0})
        finally:
            release.set()


class Commands(unittest.TestCase):
    setUp = command_base.Household.setUp
    say = command_base.Household.say
    confirm = command_base.Household.confirm

    def answer(self, **kw):
        return dict(op='member_update', person='ana', label='Anja', **kw)

    def test_every_exact_control_keeps_context_behaviour(self):
        controls = ('history', 'verlauf', 'what did i change', 'was habe ich geändert',
                    'settings', 'my settings', 'what are my settings', 'help', 'hilfe', '?',
                    'undo', 'rückgängig', 'redo', 'ideas', 'suggest',
                    'show my shelves', 'list shelves', 'shelf list', 'y', 'ye', 'yes', 'ja', 'ok', 'yes 12', 'no 12')
        for query in controls:
            with self.subTest(query=query), patch.object(self.s, 'decide', side_effect=AssertionError('exact control needs no model')):
                plain = c.special(self.s, query, 'movie')
                contextual = c.special(self.s, query, 'movie', results={'query': 'wish', 'rows': {}})
                self.assertEqual(contextual, plain)

    def test_shared_link_self_rename_preview_confirm_and_undo(self):
        with patch.object(c, 'context', return_value=('w92', None, 'both')), \
                patch.object(self.s, 'decide', return_value={'answer': self.answer()}):
            meta = c.special(self.s, 'rename Ana to Anja', 'movie', routed=True)['metas'][0]
            self.assertIn('display name Anja', meta['description'])
            self.assertEqual(c.get_pending(self.s, 'w92')['actor'], 'ana')
            self.say('yes')
            self.assertEqual(c.config(self.s)['household']['labels']['ana'], 'Ana')
            self.confirm()
            self.assertEqual(c.config(self.s)['household']['labels']['ana'], 'Anja')
            self.say('undo')
            self.assertEqual(c.config(self.s)['household']['labels']['ana'], 'Ana')

    def test_rename_gates_age_creation_child_and_guest(self):
        for change in (dict(op='member_update', person='ana', age=30),
                       dict(op='member_create', person='kim', label='Kim')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.preview(self.s, 'x', 'both', answer=change)
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ana' else None):
            with self.assertRaises(ValueError):
                c.preview(self.s, 'x', 'both', answer=self.answer())
            with self.assertRaises(PermissionError):
                c.preview(self.s, 'x', 'ana', answer=self.answer())
        with self.assertRaises(PermissionError):
            c.preview(self.s, 'x', self.s.GUEST, answer=self.answer())

    def test_shared_self_rename_with_for_hint_keeps_same_target(self):
        for bound in (None, 'both'):
            with self.subTest(bound=bound), patch.object(c, 'context', return_value=('w92', bound, None)), \
                    patch.object(self.s, 'decide', return_value={'answer': self.answer()}):
                c.special(self.s, 'rename Ana to Anja for Ana', 'movie', routed=True)
                self.assertEqual(c.get_pending(self.s, 'w92')['member']['name'], 'ana')
                self.confirm()
                self.assertEqual(c.config(self.s)['household']['labels']['ana'], 'Anja')
                self.say('undo')

    def test_together_label_preview_confirm_undo_and_validation(self):
        original = c.config(self.s)['household']['labels']['pair']
        answer = dict(op='member_update', person='together', label='Wir zwei')
        with patch.object(self.s, 'decide', return_value={'answer': answer}):
            meta = c.special(self.s, 'nenne gemeinsam um in Wir zwei', 'movie', routed=True)['metas'][0]
            self.assertIn('Wir zwei', meta['description'])
            self.confirm()
            self.assertEqual(c.config(self.s)['household']['labels']['pair'], 'Wir zwei')
            self.say('undo')
            self.assertEqual(c.config(self.s)['household']['labels']['pair'], original)
        for label in ('', 'x'*41, 'bad\x7flabel', 'bad\nlabel'):
            with self.subTest(label=label), self.assertRaises(ValueError):
                c.preview(self.s, 'x', 'ana', answer={**answer, 'label': label})
        with self.assertRaises(ValueError):
            c.preview(self.s, 'x', 'both', answer=answer)
        for actor in ('ana', self.s.GUEST):
            with self.subTest(actor=actor), patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ana' else None), self.assertRaises(PermissionError):
                c.preview(self.s, 'x', actor, answer=answer)

    def test_row_rename_preview_uses_current_label(self):
        plan = c.preview(self.s, 'x', 'ana', answer=dict(op='row_rename', values=['foryou', 'X']))
        self.assertEqual(plan['name'], 'Rename For You to X for the household')
        c.apply(self.s, plan)
        again = c.preview(self.s, 'x', 'ana', answer=dict(op='row_rename', values=['foryou', 'Y']))
        self.assertEqual(again['name'], 'Rename X to Y for the household')
        self.assertIn('person together label', c.command_prompt(self.s))

    def test_missing_actor_hint_does_not_suggest_another_rename_target(self):
        meta = c.error_card(self.s, ValueError('Add a member name to this command'), 'movie', 'rename Ana to Anja')['metas'][0]
        self.assertNotIn('rename Ana to Anja for ', meta['description'])
        self.assertIn("Who's watching", meta['description'])
