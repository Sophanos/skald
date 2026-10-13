"""Cold build publication and enrichment, with synthetic transports only."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class FastBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location('w73_server', Path(__file__).resolve().parents[1] / 'server.py')
        self.s = importlib.util.module_from_spec(spec)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'TMDB_API': 'offline', 'OPENROUTER_API_KEY': '', 'SKALD_CONFIG': ''}))
        self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        spec.loader.exec_module(self.s)
        for name in ('_pool', '_outer', '_nzb_pool', '_connector_pool'):
            self.addCleanup(getattr(self.s, name).shutdown, wait=True, cancel_futures=True)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(self.s, 'connectors_tick'))
        self.save_feeds = self.s.save_feeds
        self.stack.enter_context(patch.object(self.s, 'save_feeds'))
        self.stack.enter_context(patch.object(self.s, 'pin_decision'))
        self.s._langs = {'de-DE', 'en-US'}

    def ordering(self, warm=False, enabled=('top10', 'toprated')):
        s, events = self.s, []
        if warm:
            s._feeds[s.rid('top10'), 'de-DE'] = {'stamp': 'old'}
        def specs(d, hashes):
            def build(name, lang):
                events.append((name, lang))
                return {s.rid(name): {'movie': [{'id': 'tmdb:1'}]}}
            return [(name, [s.rid(name)], name, 0, lambda lang, n=name: build(n, lang))
                    for name in ('because', *enabled)]
        def profiles():
            events.append('profiles')
            if not warm:
                for lang in s._langs:
                    for name in enabled:
                        self.assertIn((s.rid(name), lang), s._feeds)
            return {}
        with patch.object(s, 'job_specs', side_effect=specs), patch.object(s, 'refresh_profiles', side_effect=profiles):
            s.feed_pass(s.local_now(), {})
        return events

    def test_cold_publishes_both_languages_before_profiles(self):
        events = self.ordering()
        self.assertEqual(events.index('profiles'), 4)
        self.assertEqual(len(events), 7)

    def test_cold_only_runs_enabled_fast_rows(self):
        events = self.ordering(enabled=('top10',))
        self.assertEqual(events.index('profiles'), 2)
        self.assertEqual(len(events), 5)

    def test_warm_keeps_normal_order(self):
        events = self.ordering(warm=True)
        self.assertEqual(events[0], 'profiles')
        self.assertEqual(events[1][0], 'because')

    def test_deferred_model_then_enrichment_invalidates_taste_jobs(self):
        s, calls = self.s, []
        s._langs = {'en-US'}
        p = {'hash': 'synthetic', 'entries': [{'key': 'tmdb:9', 'kind': 'movie', 'title': 'Synthetic',
             'weight': 2., 'signal': 'loved', 'by': list(s.PEOPLE), 'item': {}}], 'seen': {'tmdb:9'}}
        def tmdb(path, **kwargs):
            calls.append(('tmdb', path))
            return {'results': [{'id': i} for i in range(1, 25)], 'id': 1, 'title': 'Synthetic', 'overview': 'A case', 'genres': []}
        def router(path, payload, *args):
            calls.append(('router', path))
            if path.endswith('embeddings'):
                return {'data': [{'index': i, 'embedding': [1.] + [0.] * (s.EMB_DIM - 1)} for i in range(len(payload['input']))]}
            return {'choices': [{'message': {'content': '1|cozy_case:3'}}]}
        def profiles():
            s._profiles['household'] = p
            return {'household': p['hash']}
        def build(lang):
            m = s.get_model()
            self.assertIsNone(m['vec'])
            self.assertIsNone(m['cal'])
            self.assertTrue(calls)
            self.assertTrue(all(c[0] == 'tmdb' and c[1] == 'movie/9' for c in calls))
            s.ensure_emb([('tmdb:2', 'movie')])
            s.tag_experiences([('tmdb:2', 'movie')])
            self.assertTrue(calls)
            self.assertTrue(all(c[0] == 'tmdb' and c[1] == 'movie/9' for c in calls))
            return {s.rid('because'): {'movie': [{'id': 'tmdb:2'}]}}
        retry = {('because', 'de-DE'): 99999999999, ('top10', 'de-DE'): 99999999999}
        specs = [('because', [s.rid('because')], 'today/synthetic', 1800, build),
                 ('top10', [s.rid('top10')], 'today', 0, lambda l: {s.rid('top10'): {}})]
        with patch.object(s, 'OR_KEY', 'offline'), patch.object(s, 'tmdb', side_effect=tmdb), \
                patch.object(s, 'openrouter', side_effect=router), patch.object(s, 'refresh_profiles', side_effect=profiles), \
                patch.object(s, 'job_specs', return_value=specs):
            s.feed_pass(s.local_now(), retry)
            self.assertIn((s.rid('because'), 'en-US'), s._feeds)
            initial = s.get_model(False)
            self.assertIsNotNone(initial)
            self.assertIsNone(initial['cal'])
            self.assertTrue(all(c[0] == 'tmdb' and c[1] == 'movie/9' for c in calls))
            s._person_models['synthetic'] = (initial, initial)
            s.finish_cold_enrichment(s.local_now(), retry)
            self.assertIsNot(s.get_model(False), initial)
            self.assertTrue(any(c == ('router', '/api/v1/embeddings') for c in calls))
            self.assertTrue(any(c == ('router', '/api/v1/chat/completions') for c in calls))
            self.assertGreaterEqual(s.get_model(False)['cal'], 20)
            self.assertTrue(any(c == ('tmdb', 'trending/movie/week') for c in calls))
            self.assertNotIn('stamp', s._feeds[s.rid('because'), 'en-US'])
            self.assertIn('stamp', s._feeds[s.rid('top10'), 'en-US'])
            self.assertNotIn(('because', 'de-DE'), retry)
            self.assertIn(('top10', 'de-DE'), retry)
            self.assertEqual(s._person_models, {})
            before = list(calls)
            s.finish_cold_enrichment(s.local_now(), retry)
            self.assertEqual(calls, before)

    def test_warm_model_uses_existing_full_build(self):
        s = self.s
        p = {'hash': 'synthetic', 'entries': [], 'seen': set()}
        s._profiles['household'] = p
        s._feeds[s.rid('top10'), 'en-US'] = {'stamp': 'warm'}
        model = {'aff': {}, 'paff': {}, 'cal': 80, 'taste_line': ''}
        with patch.object(s, 'refresh_profiles', return_value={}), patch.object(s, 'job_specs', return_value=[]), \
                patch.object(s, 'build_model', return_value=model) as build:
            s.feed_pass(s.local_now(), {})
            self.assertIs(s.get_model(), model)
            s.finish_cold_enrichment(s.local_now(), {})
            self.assertIs(s.get_model(), model)
            build.assert_called_once_with(p)

    def test_enrichment_invalidates_anime_stamps_and_retries(self):
        s = self.s
        s._profiles['household'] = {'hash': 'synthetic'}
        s._defer_enrichment = True
        anime = {'airing', 'nextseason'}
        independent = {'top10', 'toprated', 'digital'}
        retry = {(job, lang): 99999999999 for job in anime | independent for lang in s._langs}
        with patch.object(s, 'row_enabled', side_effect=lambda cid: cid in {s.rid(job) for job in anime | independent}), \
                patch.object(s, 'get_model'), patch.object(s, 'anime_feed', return_value={'series': []}) as build:
            for job, cids, stamp, _, _ in s.job_specs(s.local_now(), {}):
                for lang in s._langs:
                    s._feeds[cids[0], lang] = {'stamp': f'v{s.STAMP_V}/{stamp}', 'built': 1, 'metas': {}}
            s.finish_cold_enrichment(s.local_now(), retry)
            for job in anime | independent:
                for lang in s._langs:
                    rec = s._feeds[s.rid(job), lang]
                    if job in anime:
                        self.assertNotIn('stamp', rec)
                        self.assertNotIn((job, lang), retry)
                    else:
                        self.assertIn('stamp', rec)
                        self.assertIn((job, lang), retry)
            s.build_feeds(s.local_now(), {}, retry, only=anime)
            self.assertEqual(build.call_count, 4)

    def test_enrichment_persists_invalidated_stamps_for_restart(self):
        s = self.s
        s._langs = {'en-US'}
        s._profiles['household'] = {'hash': 'synthetic'}
        s._defer_enrichment = True
        with patch.object(s, 'get_model'), patch.object(s, 'save_feeds', self.save_feeds), \
                patch.object(s, 'because_feed', return_value={s.rid('because'): {'movie': [{'id': 'tmdb:2'}]}}) as build, \
                patch.object(s, 'row_enabled', side_effect=lambda cid: cid in {s.rid('because'), s.rid('top10')}), \
                patch.object(s, 'refresh_profiles', return_value={}):
            for job, cids, stamp, _, _ in s.job_specs(s.local_now(), {}):
                s._feeds[cids[0], 'en-US'] = {'stamp': f'v{s.STAMP_V}/{stamp}', 'built': 1,
                                             'metas': {'movie': [{'id': 'tmdb:1'}]}}
            s.save_feeds()
            s.finish_cold_enrichment(s.local_now(), {})
            saved = json.loads(Path(s.DATA, 'feeds.json').read_text())
            self.assertNotIn('stamp', saved[f"{s.rid('because')}|en-US"])
            self.assertIn('stamp', saved[f"{s.rid('top10')}|en-US"])
            s._feeds.clear()
            s._profiles.clear()
            s._feed_started = False
            s.load_state()
            self.assertEqual(s._feeds[s.rid('because'), 'en-US']['metas']['movie'], [{'id': 'tmdb:1'}])
            s.feed_pass(s.local_now(), {})
            build.assert_called_once_with('en-US')
            self.assertFalse(s._defer_enrichment)
            self.assertEqual(s._feeds[s.rid('because'), 'en-US']['metas']['movie'], [{'id': 'tmdb:2'}])
