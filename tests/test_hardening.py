"""Stage hardening: offline identity, cache lifetime and trust-boundary regressions."""
from skald.members import rid
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from decide_kit import ROOT, close_server, load_server
from eval.home_perf import identity
from eval.profile_golden import NOW
from eval.rows_report import home_server
from test_api import raw_request


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)
        for target, value in [('time.time', NOW), ('urllib.request.urlopen', None)]:
            p = patch(target, return_value=value) if value else patch(target, side_effect=AssertionError('network forbidden'))
            p.start()
            self.addCleanup(p.stop)
        self.s._profiles['household'] = {'hash': 'synthetic', 'entries': [], 'seen': set(), 'negative': set()}

    def test_home_key_fifo_and_knowledge_invalidation(self):
        s = self.s
        with patch.object(s, 'build_home', return_value={(rid('foryou', s.CONFIG), 'tv'): []}) as build, \
                patch.object(s, 'remote_masks', return_value=(set(), set())) as masks, \
                patch.object(s, 'who_watching', return_value=('both', 'prior')) as who:
            def request(lang='de-DE'):
                return s.row_metas(rid('foryou', s.CONFIG), 'tv', lang)
            request(); request()
            self.assertEqual(build.call_count, 1)
            oldest = next(iter(s._home_cache))
            s._profiles['household']['hash'] = 'changed'
            request()
            s._model['m'] = {}
            request()
            s._feeds[rid('foryou', s.CONFIG), 'de-DE'] = {'built': 1}
            request()
            s._shown[rid('lane5', s.CONFIG), 'de-DE'] = {'built': 1}
            request()
            self.assertEqual(len(s._home_cache), 5)
            self.assertIn(oldest, s._home_cache)
            masks.return_value = ({'tmdb:1'}, {'tmdb:2'})
            request()
            who.return_value = ('ben', 'tap')
            request()
            request('en-US')
            with patch('time.time', return_value=NOW+60):
                request()
            self.assertEqual(build.call_count, 9)
            self.assertEqual(len(s._home_cache), 8)
            self.assertNotIn(oldest, s._home_cache)
            s.seed_similarity('a', 'b')
            s.clear_seed_cache()
            self.assertEqual(s.seed_similarity.cache_info().currsize, 0)
            self.assertFalse(s._home_cache)
            request()
            self.assertEqual(build.call_count, 10)
            s._profiles.clear()
            request(); request()
            self.assertEqual(build.call_count, 11)

    def test_concurrent_home_requests_build_once(self):
        s = self.s
        ready, release, together = threading.Event(), threading.Event(), threading.Barrier(3)
        def build(*args):
            ready.set()
            self.assertTrue(release.wait(3))
            return {(rid('foryou', self.s.CONFIG), 'tv'): []}
        def request():
            together.wait(3)
            return s.row_metas(rid('foryou', s.CONFIG), 'tv', 'de-DE')
        with patch.object(s, 'build_home', side_effect=build) as spy, ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(request), pool.submit(request)
            together.wait(3)
            self.assertTrue(ready.wait(3))
            release.set()
            self.assertEqual(a.result(3), b.result(3))
        self.assertEqual(spy.call_count, 1)

    def test_catalog_cache_hits_still_note_served_per_request(self):
        s = self.s
        with patch.object(s, 'note_served') as note, patch.object(s, 'build_home', return_value={(rid('foryou', s.CONFIG), 'tv'): []}) as build:
            for _ in range(2):
                self.assertEqual(raw_request(s, f"/de-DE/catalog/tv/{rid('foryou', s.CONFIG)}.json", handler=s.Handler)[0], 200)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(note.call_count, 2)
        with patch.object(s, '_row_metas_uncached', return_value=[]) as uncached:
            s.row_metas(rid('cal', s.CONFIG), 'tv', 'de-DE')
            uncached.assert_called_once_with(rid('cal', s.CONFIG), 'tv', 'de-DE')

    def test_week_seeds_once_per_row_and_missing_person(self):
        s = self.s
        metas = [{'id': f'tmdb:{i}', 'type': 'movie'} for i in range(20)]
        model = {'nexp': {'ben': 40}}
        with patch.object(s, 'get_model', return_value=model), patch.object(s, 'score_key', return_value=(.1, [])), \
                patch.object(s, 'tone_ok', return_value=True), patch.object(s, 'row_eligible', return_value=True), \
                patch.object(s, 'source_metas', return_value=[]), patch.object(s, 'row_pool', return_value=metas), \
                patch.object(s, 'row_facts', return_value={'g': []}), \
                patch.object(s, 'week_seeds', return_value=['seed']) as seeds, \
                patch.object(s, 'explicit_seeds', return_value=['seed']), patch.object(s, 'seed_knn', return_value=1):
            self.assertEqual(len(s.fill_row([], rid('week', s.CONFIG), 'de-DE', {}, set(), (set(), set()))), 20)
            self.assertEqual(seeds.call_count, 2)
            self.assertTrue(s.week_ok(metas[0]))
            self.assertEqual(len(s.week_scores(metas)), 20)
        with patch.object(s, 'tmdb', side_effect=LookupError), redirect_stdout(io.StringIO()):
            m = s.build_model(s._profiles['household'])
        m['nexp'].pop('ana')
        s._facets['tmdb:1'] = {'g': ['Drama']}
        self.assertIsNotNone(s.score_key(m, 'tmdb:1', 'together')[0])
        s._model['m'] = m
        self.assertEqual(s.explain('tmdb:1')['per_person']['ana']['n_explicit'], 0)

    def test_similarity_lru_and_all_reload_build_paths_clear(self):
        s = self.s
        def warm():
            s.seed_similarity('a', 'b'); s.seed_similarity('a', 'b')
            self.assertGreater(s.seed_similarity.cache_info().hits, 0)
            s._home_cache['synthetic'] = None
        def cleared():
            self.assertEqual(s.seed_similarity.cache_info().currsize, 0)
            self.assertFalse(s._home_cache)
        self.assertEqual(s.seed_similarity.cache_info().maxsize, 65536)
        for name in ('load_emb', 'load_facets'):
            warm()
            getattr(s, name)()
            cleared()
        warm()
        with patch.object(s, 'facet_record', return_value={'kind': 'movie', 'g': []}):
            s.ensure_facets([('a', 'movie')])
        cleared()
        warm()
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'title_text', return_value='synthetic'), \
                patch.object(s, 'openrouter', return_value={'data': [{'index': 0, 'embedding': [1., 0.]}]}):
            s.ensure_emb([('a', 'movie')])
        cleared()
        warm()
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'tmdb', return_value={}), \
                patch.object(s, 'llm_text', return_value='1|cozy_case:3'), redirect_stdout(io.StringIO()):
            s.tag_experiences([('a', 'movie')])
        cleared()
        warm()
        with patch.object(s, 'tmdb', side_effect=LookupError), redirect_stdout(io.StringIO()):
            s.build_model(s._profiles['household'])
        cleared()
        s._emb.update(a=array('f', [1., 0.]), b=array('f', [1., 0.]))
        s.clear_seed_cache()
        self.assertEqual(s.seed_similarity('a', 'b'), .5)
        s._emb['b'] = array('f', [0., 1.])
        s.clear_seed_cache()
        self.assertEqual(s.seed_similarity('a', 'b'), 0)

    def test_online_private_mode_daily_oldest_prune_and_valid_indices(self):
        s = self.s
        ld = s.online_day(NOW)[0]
        state = s.online_state()
        state['obs'] = {f'item{i}': [NOW-2100+i, False, 0, 0] for i in range(2100)}
        state['keys'] = [f'tmdb:{i}@movie' for i in range(2100)]
        state['lists'] = {f'{i:016x}': str(i) for i in range(2100)}
        state['exp'] = {ld: {rid('foryou', s.CONFIG): {f'{i:016x}': [1, 12, NOW-2100+i] for i in range(2100)}}}
        path = Path(s.DATA, 'online.json')
        for name in (path, Path(str(path)+'.tmp')):
            name.write_text('{}'); name.chmod(0o666)
        s._online_dirty.set()
        s.save_online()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(state['obs']), 2000)
        self.assertNotIn('item0', state['obs'])
        self.assertIn('item2099', state['obs'])
        self.assertEqual(len(state['lists']), 2000)
        self.assertNotIn('0000000000000000', state['lists'])
        self.assertIn(f'{2099:016x}', state['lists'])
        s.online_valid(json.loads(path.read_text()))
        self.assertEqual(len(state['keys']), 2000)

    def test_told_tmdb_failure_skips_one_item_without_staling_profile(self):
        s = self.s
        def resolve(kind, title, year, lang):
            if title == 'Bad resolve':
                raise LookupError('synthetic')
            return [{'id': 1 if title == 'Bad genres' else 2, 'name': title, 'genre_ids': [1]}]
        before = s._online_meta['errors']
        with patch.object(s, 'person_loves', return_value=[('ana', title, None, 'tv') for title in
                ('Bad resolve', 'Bad genres', 'Good')]), patch.object(s, 'resolve_pick', side_effect=resolve), \
                patch.object(s, 'genres', side_effect=[LookupError('synthetic'), {1: 'Drama'}]), redirect_stdout(io.StringIO()):
            p = s.build_profile([], [], remote=False)
        self.assertEqual([e['key'] for e in p['entries']], ['tmdb:2'])
        self.assertEqual(s._online_meta['errors']-before, 2)

    def test_bound_session_conflicts_and_unbound_overrides(self):
        s = self.s
        token = s.new_client('bound-session', ['read', 'events'], 'ben')
        headers = {'Authorization': 'Bearer '+token}
        def post(who, extra=None):
            return raw_request(s, '/api/v1/session', method='POST', body={'who': who, **(extra or {})}, headers=headers)[0]
        self.assertEqual(post('off'), 409)
        self.assertEqual(post('both'), 201)
        self.assertEqual(post('ben'), 201)
        self.assertEqual(post('off'), 201)
        s.write_session({'who': 'ana'}, 'phone')
        for who in ('off', 'both', 'ben'):
            self.assertEqual(post(who), 409)
            self.assertEqual(s.who_watching()[0], 'ana')
        self.assertEqual(post('ben', {'fix': s.session_windows()[-1]['id']}), 403)
        unbound = s.new_client('unbound-session', ['events'])
        self.assertEqual(raw_request(s, '/api/v1/session', method='POST', body={'who': 'both'},
                                    headers={'Authorization': 'Bearer '+unbound})[0], 201)
        self.assertEqual(post('off'), 201)
        s.write_session({'who': 'ana'}, 'phone')
        s.write_session({'who': 'ben'}, 'phone')
        self.assertEqual(s.who_watching()[0], 'ben')
        with patch('time.time', return_value=NOW+4*3600):
            self.assertEqual(post('both'), 201)

    def test_argv_unknown_starts_and_admin_paths_keep_their_contract(self):
        s = self.s
        tail = (ROOT / 'server.py').read_text().split('if __name__ == "__main__":', 1)[1]
        code = compile(tail.replace('\n    ', '\n'), 'server-main', 'exec')
        for argv in (['server.py'], ['server.py', '--unknown', 'private-value', '--new', 'x']):
            err = io.StringIO()
            namespace = {**s.__dict__, 'CONFIG_SOURCE': 'legacy', 'remote_token': Mock(), 'load_state': Mock(), 'ThreadingHTTPServer': MagicMock()}
            with patch.object(sys, 'argv', argv), patch.object(s.threading, 'Thread'), redirect_stderr(err):
                exec(code, namespace)
            namespace['ThreadingHTTPServer'].return_value.__enter__.return_value.serve_forever.assert_called_once()
            self.assertEqual(err.getvalue(), 'ignoring unknown args: 4\n' if len(argv) > 1 else '')
        for flag in ('--new-client', '--revoke-client'):
            namespace = {**s.__dict__, 'CONFIG_SOURCE': 'legacy', 'new_client': Mock(return_value='synthetic'), 'revoke_client': Mock(), 'remote_token': Mock()}
            with patch.object(sys, 'argv', ['server.py', flag, 'synthetic-client']), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                exec(code, namespace)
            self.assertEqual(raised.exception.code, 0)
            namespace[flag[2:].replace('-', '_')].assert_called_once()
            namespace['remote_token'].assert_not_called()

    def test_resolve_500_char_alias_truncates_and_choice_contract_stays(self):
        s = self.s
        inp = {'title': 'a'*120, 'year': 2020, 'kind': 'movie', 'choices': [
            {'key': 'tmdb:1', 'kind': 'movie', 'title': 'other', 'aliases': ['a'*500], 'year': 2020, 'pop': 0.}]}
        s.check(s.DECISIONS['resolve_title']['inp'], inp)
        with patch.object(s.difflib, 'SequenceMatcher', wraps=s.difflib.SequenceMatcher) as match:
            s.resolve_rule(inp, s.DECISIONS['resolve_title']['params'])
            self.assertTrue(all(len(call.args[1]) <= 120 and len(call.args[2]) <= 120 for call in match.call_args_list))
        self.assertEqual(s.resolve_normal('a'*500), 'a'*120)
        self.assertEqual(s.DECISIONS['resolve_title']['inp']['choices'][1], 8)
        inp['choices'] *= 9
        with self.assertRaisesRegex(ValueError, 'choices'):
            s.check(s.DECISIONS['resolve_title']['inp'], inp)


    def test_feed_worker_job_specs_failure_continues_lang_and_maintenance(self):
        s = self.s
        jobs = [('synthetic', [rid('synthetic', s.CONFIG)], 'stamp', 0, lambda lang: {rid('synthetic', s.CONFIG): {}})]
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            stack.enter_context(patch.object(s, '_langs', ['de-DE', 'en-US']))
            stack.enter_context(patch.object(s, 'refresh_profiles', return_value={}))
            specs = stack.enter_context(patch.object(s, 'job_specs', side_effect=[ValueError('synthetic\n' + 'x'*300), jobs]))
            log = stack.enter_context(patch.object(s, '_daily_log'))
            saves = [stack.enter_context(patch.object(s, 'save_' + name)) for name in ('feeds', 'learn', 'nzb', 'emb', 'facets')]
            daily = stack.enter_context(patch.object(s, 'daily_jobs'))
            sleep = stack.enter_context(patch.object(s.time, 'sleep', side_effect=RuntimeError('pass complete')))
            with self.assertRaisesRegex(RuntimeError, '^pass complete$'):
                s.feed_worker()
        self.assertEqual(specs.call_count, 2)
        for save in saves:
            save.assert_called_once_with()
        daily.assert_called_once_with(NOW)
        sleep.assert_called_once_with(60)
        log.assert_called_once_with(('feed lang de-DE failed: ValueError: synthetic ' + 'x'*300)[:240])
        self.assertIn((rid('synthetic', s.CONFIG), 'en-US'), s._feeds)

    def test_feed_worker_whole_pass_failure_runs_maintenance(self):
        s = self.s
        with ExitStack() as stack:
            stack.enter_context(patch.object(s, 'local_now', side_effect=ValueError('synthetic\rbroken')))
            log = stack.enter_context(patch.object(s, '_daily_log'))
            saves = [stack.enter_context(patch.object(s, 'save_' + name)) for name in ('learn', 'nzb', 'emb', 'facets')]
            daily = stack.enter_context(patch.object(s, 'daily_jobs'))
            sleep = stack.enter_context(patch.object(s.time, 'sleep', side_effect=RuntimeError('pass complete')))
            with self.assertRaisesRegex(RuntimeError, '^pass complete$'):
                s.feed_worker()
        for save in saves:
            save.assert_called_once_with()
        daily.assert_called_once_with(NOW)
        sleep.assert_called_once_with(60)
        log.assert_called_once_with('feed pass failed: ValueError: synthetic broken')

    def test_feed_pass_stamp_and_built_print_failures_continue_jobs(self):
        s = self.s
        jobs = [('stamp', [rid('stamp', s.CONFIG)], 'stamp', 1, Mock()),
                ('print', ['nodot'], 'stamp', 0, lambda lang: {'nodot': {'tv': []}}),
                ('good', [rid('good', s.CONFIG)], 'stamp', 0, lambda lang: {rid('good', s.CONFIG): {}})]
        s._feeds[rid('stamp', s.CONFIG), 'de-DE'] = {'stamp': 123}
        retry = {}
        with patch.object(s, '_langs', ['de-DE']), patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'job_specs', return_value=jobs), patch.object(s, '_daily_log') as log, \
                patch.object(s, 'save_feeds') as save, redirect_stdout(io.StringIO()):
            s.feed_pass(s.local_now(), retry)
        self.assertEqual(log.call_count, 2)
        self.assertIn('job stamp de-DE failed: AttributeError:', log.call_args_list[0].args[0])
        self.assertIn('job print de-DE failed: IndexError:', log.call_args_list[1].args[0])
        save.assert_called_once_with()
        self.assertIn((rid('good', s.CONFIG), 'de-DE'), s._feeds)
        self.assertEqual(retry, {})


class SnapshotHardening(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SKALD_SNAPSHOT'), 'SKALD_SNAPSHOT not set')
    def test_gate_a_every_feed_type_four_sessions(self):
        with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW), home_server(os.environ['SKALD_SNAPSHOT']) as s:
            result = identity(s)
        self.assertEqual(result['sessions'], 4)
        self.assertGreater(result['rows_types'], 29)


if __name__ == '__main__':
    unittest.main()
