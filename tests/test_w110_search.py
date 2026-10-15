"""Search outages and picker hedging with synthetic, offline transports."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from threading import Event, Lock, Thread, current_thread
from unittest.mock import patch

from decide_kit import close_server, load_server, reset


class SearchFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.s = load_server(cls.temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        s = self.s
        reset(s)
        s._ai.clear()
        s._last_search.clear()
        s._profiles.clear()
        s._facets.clear()
        s._feeds.clear()
        s._shown.clear()
        self.calls, self.tmdb_calls, self.jobs = [], [], []
        self.failure = None
        self.transport = None
        self.literal, self.keywords, self.discovered = [], [], []
        self.route = {'choice': 'request', 'watched': 'any', 'list': 'taste'}
        self.picks = 'Title 90|2000|m\nTitle 91|2000|s'
        self.narrow = None
        self.cards = [{'id': f'tmdb:{i}', 'type': typ, 'name': f'Title {i}', 'releaseInfo': '2000'}
                      for i, typ in ((70, 'movie'), (71, 'movie'), (80, 'series'), (81, 'series'))]
        s._feeds[s.rid('foryou'), 'en-US'] = {'metas': {'tv': self.cards}}
        s._model['m'] = {'paff': {'ana': {'x:quiet': 1}, 'ben': {'x:lively': 1}}}
        for name, kwargs in (
                ('OR_KEY', {'new': 'offline'}), ('PEOPLE', {'new': ('ana', 'ben')}),
                ('SCOPES', {'new': {'ana': 'ana', 'ben': 'ben', 'together': 'both'}}),
                ('refresh_preferences', {}), ('superseded', {'return_value': False}),
                ('kids_age', {'return_value': None}), ('ensure_facets', {}),
                ('ensure_emb', {'side_effect': AssertionError('embedding forbidden')}),
                ('candidate_pool', {'side_effect': AssertionError('pool rebuild forbidden')}),
                ('taste_hint', {'return_value': 'quiet stories'}),
                ('score_key', {'side_effect': lambda m, key, person, **kw:
                 ((int(key[5:]) if person == 'ana' else -int(key[5:])), [])}),
                ('_connector_open', {'side_effect': self.reply}),
                ('tmdb', {'side_effect': self.tmdb_reply})):
            mock = patch.object(s, name, **kwargs)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        self.addCleanup(self.finish_jobs)
        for pool in (s._pool, s._outer, s._search_pool):
            def capture(fn, *args, submit=pool.submit, **kwargs):
                job = submit(fn, *args, **kwargs)
                self.jobs.append(job)
                return job
            self.enterContext(patch.object(pool, 'submit', side_effect=capture))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))

    def finish_jobs(self):
        for job in self.jobs:
            try:
                job.result(timeout=10)
            except Exception:
                pass

    def reply(self, req, timeout):
        body = json.loads(req.data)
        if 'messages' not in body:
            label = 'route_query'
        elif body['messages'][0]['content'].startswith('Extract only'):
            label = 'rules'
        else:
            label = 'wide_picks' if '30 lines' in body['messages'][0]['content'] else 'picks'
        self.calls.append((label, time.monotonic(), timeout, body))
        if self.transport:
            self.transport(label, timeout)
        if self.failure:
            raise self.failure
        if label == 'route_query':
            reply = {'answers': {('route' if k == 'choice' else k): {'choice': v, 'confidence': .99}
                                 for k, v in self.route.items()}}
        else:
            content = (json.dumps({'from': None, 'to': None, 'exclude': []}) if label == 'rules' else
                       self.narrow if label == 'picks' and self.narrow is not None else self.picks)
            reply = {'choices': [{'message': {'content': content}}]}
        if body.get('stream'):
            chunks = [{'choices': [{'delta': {'content': content}}]}]
            return io.BytesIO((''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n').encode())
        return io.BytesIO(json.dumps(reply).encode())

    def tmdb_reply(self, path, **params):
        self.tmdb_calls.append((path, params))
        if path.startswith('genre/'):
            return {'genres': []}
        if path == 'search/multi':
            return {'results': [{**it, 'media_type': 'movie'} for it in self.literal]}
        if path == 'search/keyword':
            return {'results': self.keywords}
        if path.startswith('discover/'):
            return {'results': self.discovered}
        query = params.get('query', '')
        if query.startswith('Title ') and query[6:].isdigit():
            return {'results': [{'id': int(query[6:]), 'title': query, 'name': query,
                                 'release_date': '2000', 'first_air_date': '2000'}]}
        return {'results': self.literal}

    def results(self, query='dark thrillers', stype='movie', lang='en-US', scope='ana'):
        with self.s.addon_scope(scope):
            return self.s.search(stype, query, lang)['metas']

    def labels(self):
        self.finish_jobs()
        return [label for label, *_ in self.calls]

    def assert_notice(self, metas, text):
        self.assertTrue(metas[0]['id'].startswith(self.s.rid('cmd:')))
        self.assertIn(text, metas[0]['description'])
        self.assertTrue(any(x['id'].startswith('tmdb:') for x in metas[1:]))

    def test_credit_failure_fills_both_rows_and_logs_reason_once_per_row(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'misleading timeout', {}, None)
        with patch('builtins.print') as log:
            for stype, expected in (('movie', ['tmdb:71', 'tmdb:70']), ('series', ['tmdb:81', 'tmdb:80'])):
                metas = self.results(stype=stype)
                self.assert_notice(metas, 'credit used up')
                self.assertEqual([x['id'] for x in metas[1:]], expected)
        lines = [str(c.args[0]) for c in log.call_args_list if 'search fallback' in str(c.args[0])]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all('credit' in line and 'dark thrillers' not in line for line in lines))

    def test_daily_cap_has_its_own_notice_and_spends_nothing(self):
        with patch.dict(self.s.AI_CAPS, search=0):
            metas = self.results()
        self.assert_notice(metas, 'Skald budget reached')
        self.assertEqual(self.calls, [])

    def test_household_zero_budget_uses_tmdb_without_paid_calls(self):
        from skald.members import fresh_config
        cfg = fresh_config(['ana', 'ben'])
        cfg['household']['ai_monthly_usd'] = 0
        path = Path(self.s.DATA, 'skald.json')
        path.write_text(json.dumps(cfg))
        self.addCleanup(path.unlink)
        self.literal = [{'id': 990001, 'title': 'Invented Lantern', 'release_date': '2026-01-01'}]
        metas = self.results('invented quiet mystery')
        self.assert_notice(metas, 'Skald budget reached')
        self.assertIn('tmdb:990001', [m['id'] for m in metas])
        self.assertEqual(self.calls, [])

    def test_timeout_fills_from_cached_taste_within_deadline(self):
        release = Event()
        def slow(label, timeout):
            release.wait(9)
            raise TimeoutError('synthetic late transport')
        self.transport = slow
        try:
            with patch.object(self.s, 'SEARCH_TIMEOUT', .3):
                start = time.monotonic()
                metas = self.results()
                self.assertLess(time.monotonic() - start, .4)
                self.assert_notice(metas, 'timed out')
        finally:
            release.set()

    def test_literal_then_raw_tmdb_keywords_then_scoped_taste(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.literal = [{'id': 60, 'title': 'Literal match'}]
        self.keywords = [{'id': 700, 'name': 'Provider keyword'}, {'id': 701, 'name': 'Another keyword'}]
        self.discovered = [{'id': 61, 'title': 'Discovered match'}, {'id': 60, 'title': 'Duplicate'}]
        metas = self.results(query='could you find a cozi moive for tonight?')
        self.assertEqual([m['id'] for m in metas[1:]], ['tmdb:60', 'tmdb:61', 'tmdb:71', 'tmdb:70'])
        keywords = next(params for path, params in self.tmdb_calls if path == 'search/keyword')
        self.assertEqual(keywords['query'], 'could you find a cozi moive for tonight?')
        discover = next(params for path, params in self.tmdb_calls if path == 'discover/movie')
        self.assertEqual(discover['with_keywords'], '700|701')
        self.assertEqual(discover['include_adult'], 'false')
        self.assertLessEqual(discover['_timeout'], self.s.SEARCH_TIMEOUT)

    def test_full_literal_row_skips_keywords_and_keeps_cap(self):
        self.failure = TimeoutError('synthetic timeout')
        self.literal = [{'id': i, 'title': f'Literal {i}'} for i in range(30)]
        metas = self.results()
        self.assertEqual(len(metas), 15)
        self.assertNotIn('search/keyword', [path for path, _ in self.tmdb_calls])

    def test_exact_title_wins_without_ai_or_notice(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.literal = [{'id': 60, 'title': 'The Quiet Lantern'}]
        self.assertEqual([m['id'] for m in self.results('The Quiet Lantern')], ['tmdb:60'])
        self.assertEqual(self.calls, [])

    def test_fallback_is_retried_immediately_when_ai_recovers(self):
        self.failure = TimeoutError('synthetic late transport')
        self.assert_notice(self.results(), 'timed out')
        self.finish_jobs()
        self.failure = None
        self.calls.clear()
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:90'])
        self.assertIn('wide_picks', self.labels())

    def test_failed_broad_and_narrow_none_keep_reason_and_retry_after_recovery(self):
        def fail_broad(label, timeout):
            if label == 'wide_picks':
                raise urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.transport, self.narrow = fail_broad, 'NONE'
        with patch('builtins.print') as log:
            self.assert_notice(self.results(), 'credit used up')
        self.assertEqual(sum('search fallback: credit' in str(c.args[0]) for c in log.call_args_list), 1)
        self.assertEqual(self.s._ai, {})
        self.finish_jobs()
        self.transport, self.narrow = None, None
        self.calls.clear()
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:90'])
        self.assertIn('wide_picks', self.labels())

    def test_empty_foryou_uses_week_pool_with_scoped_filters_and_taste_order(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.s._feeds.clear()
        self.s._feeds[self.s.rid('week'), 'en-US'] = {'metas': {'tv': self.cards + [
            {'id': 'tmdb:72', 'type': 'movie', 'name': 'Seen'},
            {'id': 'tmdb:73', 'type': 'movie', 'name': 'Negative'},
            {'id': 'tmdb:74', 'type': 'movie', 'name': 'Blocked'}]}}
        entries = [dict(key=f'tmdb:{i}', id=i, title=f'Title {i}', stype='movie', kind='movie',
                        age=1, weight=weight, signal='watched', by=['ana'], item={})
                   for i, weight in ((72, 1), (73, -1))]
        self.s._profiles['household'] = {'entries': entries, 'seen': {e['key'] for e in entries}}
        with patch.object(self.s, 'kids_age', return_value=8), \
                patch.object(self.s, 'child_ok', side_effect=lambda meta, who: meta['id'] != 'tmdb:74'):
            metas = self.results()
        self.assert_notice(metas, 'credit used up')
        self.assertEqual([m['id'] for m in metas[1:]], ['tmdb:71', 'tmdb:70'])
        self.assertTrue(all(c.args[2] == 'ana' for c in self.score_key.call_args_list))

    def test_watched_exact_literal_wins_when_multi_lookup_times_out(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.literal = [{'id': 60, 'title': 'The Quiet Lantern'}]
        self.s._profiles['household'] = {'entries': [dict(key='tmdb:60', id=60, title='The Quiet Lantern',
            stype='movie', kind='movie', age=1, weight=1, signal='watched', by=['ana'], item={})],
            'seen': {'tmdb:60'}}
        def lookup(path, **params):
            if path == 'search/multi':
                raise TimeoutError('synthetic title lookup')
            return self.tmdb_reply(path, **params)
        self.tmdb.side_effect = lookup
        self.assertEqual([m['id'] for m in self.results('The Quiet Lantern')], ['tmdb:60'])
        self.assertNotIn('search/keyword', [path for path, _ in self.tmdb_calls])

    def test_cancel_between_hedge_check_and_backup_lock_spends_nothing(self):
        self.picks = 'NONE'
        picker = self.s.search_picks('synthetic wish', [], deadline=time.monotonic() + 2)
        picker.search_hedge.result(timeout=2)
        entered, release, lock = Event(), Event(), Lock()
        class PausedLock:
            def __enter__(self):
                if current_thread().name == 'synthetic hedge':
                    entered.set()
                    release.wait(2)
                lock.acquire()
            def __exit__(self, *args):
                lock.release()
        self.calls.clear()
        with patch.object(self.s, '_ai_lock', PausedLock()):
            hedge = Thread(target=self.s.search_backup, args=(picker,), name='synthetic hedge')
            hedge.start()
            try:
                self.assertTrue(entered.wait(1))
                self.s.cancel_search_picks(picker)
            finally:
                release.set()
                hedge.join(2)
        self.finish_jobs()
        self.assertNotIn('picks', self.labels())
        self.assertIsNone(picker.search_fallback)

    def test_cancelling_picker_stops_existing_backup(self):
        self.picks = 'NONE'
        picker = self.s.search_picks('synthetic wish', [], deadline=time.monotonic() + 2)
        picker.search_hedge.result(timeout=2)
        backup = self.s.search_backup(picker)
        self.s.cancel_search_picks(picker)
        self.assertTrue(backup.search_stop.is_set())

    def test_missing_ai_key_uses_pool_and_notice_but_preserves_exact_title(self):
        with patch.object(self.s, 'OR_KEY', ''):
            self.assert_notice(self.results(), 'AI is not configured')
            self.literal = [{'id': 60, 'title': 'The Quiet Lantern'}]
            self.assertEqual([m['id'] for m in self.results('The Quiet Lantern')], ['tmdb:60'])
        self.assertEqual(self.calls, [])

    def test_slow_keywords_and_local_scoring_share_one_deadline(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        self.cards[:] = [{'id': f'tmdb:{i}', 'type': 'movie', 'name': f'Title {i}'} for i in range(70, 100)]
        release = Event()
        def lookup(path, **params):
            if path == 'search/keyword':
                release.wait(9)
            return self.tmdb_reply(path, **params)
        def score(*args, **kwargs):
            time.sleep(.04)
            return 1, []
        self.tmdb.side_effect, self.score_key.side_effect = lookup, score
        try:
            with patch.object(self.s, 'SEARCH_TIMEOUT', .6):
                start = time.monotonic()
                self.assert_notice(self.results(), 'credit used up')
                self.assertLess(time.monotonic() - start, .6)
        finally:
            release.set()

    def test_slow_tmdb_ai_and_local_scoring_together_fit_timeout(self):
        release = Event()
        self.transport = lambda label, timeout: release.wait(9)
        self.cards[:] = [{'id': f'tmdb:{i}', 'type': 'movie', 'name': f'Title {i}'} for i in range(70, 100)]
        def lookup(path, **params):
            if path.startswith('search/'):
                release.wait(9)
            return self.tmdb_reply(path, **params)
        def score(*args, **kwargs):
            time.sleep(.04)
            return 1, []
        self.tmdb.side_effect, self.score_key.side_effect = lookup, score
        try:
            start = time.monotonic()
            self.assert_notice(self.results(), 'timed out')
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, self.s.SEARCH_TIMEOUT)
            self.assertCountEqual([label for label, *_ in self.calls], ['route_query', 'rules', 'wide_picks', 'picks'])
            self.assertGreater(self.score_key.call_count, 0)
            print(f'W110 combined slow TMDB, AI and local scoring: {elapsed:.3f}s')
        finally:
            release.set()

    def test_slow_local_ranking_is_bounded_for_both_pickers(self):
        self.picks = '\n'.join(f'Title {i}|2000|m' for i in range(100, 130))
        self.s._model['m']['cal'] = True
        def score(*args, **kwargs):
            time.sleep(.04)
            return 1, []
        self.score_key.side_effect = score
        for narrow in (False, True):
            self.s._ai.clear()
            self.s._last_search.clear()
            def transport(label, timeout):
                if narrow and label == 'wide_picks':
                    raise TimeoutError('synthetic wide timeout')
            self.transport = transport
            with self.subTest(narrow=narrow), patch.object(self.s, 'SEARCH_TIMEOUT', .6), \
                    patch.object(self.s, 'match_pct', return_value=70), \
                    patch.object(self.s, 'with_match', side_effect=lambda meta, **kw: meta):
                start = time.monotonic()
                metas = self.results()
                self.assertLess(time.monotonic() - start, .6)
                self.assert_notice(metas, 'timed out')
            self.finish_jobs()

    def test_fallback_context_contains_titles_without_the_notice(self):
        self.failure = TimeoutError('synthetic late transport')
        self.results()
        with self.s.addon_scope('ana'):
            context = self.s.last_search()
        self.assertEqual([x['key'] for x in context['rows']['movie']], ['tmdb:71', 'tmdb:70'])
        self.assertEqual(context['query'], 'dark thrillers')

    def test_fast_broad_starts_three_calls(self):
        self.assertEqual([m['id'] for m in self.results()], ['tmdb:90'])
        self.assertCountEqual(self.labels(), ['wide_picks', 'rules', 'route_query'])

    def test_narrow_local_ranking_keeps_the_searchers_scope(self):
        self.s._model['m']['cal'] = True
        self.picks = 'Title 90|2000|m\nTitle 92|2000|m'
        def fail_broad(label, timeout):
            if label == 'wide_picks':
                raise TimeoutError('synthetic wide timeout')
        self.transport = fail_broad
        with patch.object(self.s, 'match_pct', return_value=70), \
                patch.object(self.s, 'with_match', side_effect=lambda meta, **kw: meta):
            self.assertTrue(self.results())
        self.assertTrue(self.score_key.call_args_list)
        self.assertTrue(all(c.args[2] == 'ana' for c in self.score_key.call_args_list))

    def test_slow_broad_starts_backup_after_head_start(self):
        release = Event()
        def slow(label, timeout):
            if label == 'wide_picks':
                release.wait(9)
                raise TimeoutError('synthetic late wide picker')
        self.transport = slow
        try:
            start = time.monotonic()
            metas = self.results()
            elapsed = time.monotonic() - start
            labels = {label: at for label, at, *_ in self.calls}
            self.assertGreaterEqual(labels['picks'] - labels['wide_picks'], .45)
            self.assertLess(labels['picks'] - labels['wide_picks'], .8)
            self.assertEqual([m['id'] for m in metas], ['tmdb:90'])
            self.assertLess(elapsed, self.s.SEARCH_TIMEOUT + .1)
            print(f'W110 fake nine-second broad transport: {elapsed:.3f}s')
        finally:
            release.set()

    def test_all_nine_second_transports_stay_inside_search_timeout(self):
        release = Event()
        self.transport = lambda label, timeout: release.wait(9)
        try:
            start = time.monotonic()
            metas = self.results()
            elapsed = time.monotonic() - start
            self.assert_notice(metas, 'timed out')
            self.assertLess(elapsed, self.s.SEARCH_TIMEOUT + .1)
            print(f'W110 fake nine-second transport for every AI call: {elapsed:.3f}s')
        finally:
            release.set()

    def test_slow_keyword_transport_still_serves_cached_taste_at_deadline(self):
        self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
        release = Event()
        def slow(path, **params):
            if path == 'search/keyword':
                release.wait(9)
                return {'results': []}
            return self.tmdb_reply(path, **params)
        self.tmdb.side_effect = slow
        try:
            start = time.monotonic()
            metas = self.results()
            elapsed = time.monotonic() - start
            self.assert_notice(metas, 'credit used up')
            self.assertLess(elapsed, self.s.SEARCH_TIMEOUT)
            print(f'W110 fake nine-second keyword transport: {elapsed:.3f}s')
        finally:
            release.set()

    def test_fallback_filters_scoped_seen_negative_and_child_titles(self):
        self.failure = TimeoutError('synthetic timeout')
        entries = [dict(key=f'tmdb:{i}', id=i, title=f'Title {i}', year='2000', stype='movie', kind='movie',
                        age=1, weight=weight, signal='watched', by=[person], item={})
                   for i, person, weight in ((70, 'ana', 1), (72, 'ana', -1), (71, 'ben', -1))]
        self.s._profiles['household'] = {'entries': entries, 'seen': {e['key'] for e in entries}}
        self.cards += [{'id': 'tmdb:72', 'type': 'movie', 'name': 'Negative'},
                       {'id': 'tmdb:73', 'type': 'movie', 'name': 'Blocked'},
                       {'id': 'tmdb:74', 'type': 'movie', 'name': 'Unknown age'}]
        self.s._facets.update({f'tmdb:{i}': {'kind': 'movie', 'cr': {'DE': '6' if i < 73 else '16'}}
                               for i in (70, 71, 72, 73)})
        self.literal = [{'id': 73, 'title': 'Blocked'}]
        self.discovered = [{'id': 73, 'title': 'Blocked'}]
        self.keywords = [{'id': 700}]
        with patch.object(self.s, 'kids_age', return_value=8), patch.object(self.s, 'COUNTRY', 'DE'):
            self.assertEqual([m['id'] for m in self.results()[1:]], ['tmdb:71'])
        self.assertTrue(all(c.args[2] == 'ana' for c in self.score_key.call_args_list))

    def test_empty_or_unresolvable_picks_use_pool_without_failure_notice(self):
        for reply in ('', 'Unknown synthetic title|2000|m'):
            self.s._ai.clear()
            self.s._last_search.clear()
            self.picks = reply
            with self.subTest(reply=reply):
                self.assertEqual([m['id'] for m in self.results()], ['tmdb:71', 'tmdb:70'])

    def test_refinement_does_not_start_raw_rules_or_backup(self):
        self.results(query='a cozy wish')
        self.finish_jobs()
        self.calls.clear()
        self.route['previous'] = 'refine'
        self.results(query='but a little calmer')
        self.assertCountEqual(self.labels(), ['route_query', 'rules', 'wide_picks'])
        for label, _, _, body in self.calls:
            if label != 'route_query':
                self.assertIn('Answer the combined wish.', body['messages'][1]['content'])

    def test_slow_context_router_falls_back_without_starting_pickers(self):
        self.results(query='a cozy wish')
        self.finish_jobs()
        self.calls.clear()
        release = Event()
        self.transport = lambda label, timeout: release.wait(9)
        try:
            with patch.object(self.s, 'SEARCH_TIMEOUT', .3):
                start = time.monotonic()
                metas = self.results(query='a different wish')
                self.assert_notice(metas, 'timed out')
                self.assertLess(time.monotonic() - start, .4)
                self.assertEqual([label for label, *_ in self.calls], ['route_query'])
        finally:
            release.set()

    def test_outage_fixtures(self):
        path = Path(__file__).resolve().parents[1] / 'eval/fixtures/decisions/search_fallback.jsonl'
        for line in path.read_text().splitlines():
            row = json.loads(line)
            self.s._ai.clear()
            self.failure = (urllib.error.HTTPError('https://offline', row['status'], 'credit timeout budget', {}, None)
                            if row['status'] is not None else None)
            with self.subTest(query=row['in']['query']), patch.object(self.s, 'OR_KEY', 'offline' if self.failure else ''):
                metas = self.results(**row['in'])
                self.assert_notice(metas, row['reason'])
                self.assertEqual([m['id'] for m in metas[1:]], row['expect'])

    def test_status_types_and_translated_notices(self):
        cases = [(402, 'credit used up'), (403, 'provider refused access'), (429, 'provider rate limit'),
                 (500, 'provider unavailable'), (503, 'provider unavailable')]
        for status, text in cases:
            self.s._ai.clear()
            self.failure = urllib.error.HTTPError('https://offline', status, 'timeout credit budget', {}, None)
            with self.subTest(status=status):
                self.assert_notice(self.results(), text)
        for lang, text in (('de-DE', 'Guthaben aufgebraucht'), ('sr-Latn', 'kredit je potrošen'),
                           ('sr-Cyrl', 'кредит је потрошен')):
            self.s._ai.clear()
            self.failure = urllib.error.HTTPError('https://offline', 402, 'ignored', {}, None)
            with self.subTest(lang=lang):
                self.assert_notice(self.results(lang=lang), text)
        self.s._ai.clear()
        self.failure = urllib.error.URLError(TimeoutError('misleading credit message'))
        self.assert_notice(self.results(), 'timed out')


if __name__ == '__main__':
    unittest.main()
