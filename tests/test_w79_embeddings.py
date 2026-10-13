"""Model switching uses synthetic vectors and a fake OpenRouter only."""
from array import array
import base64
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch


OLD = 'openai/text-embedding-3-small'
NEW = 'google/gemini-embedding-2'
ROOT = Path(__file__).resolve().parents[1]


class EmbeddingSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.s = self.load_server('')
        self.calls = []
        for name, value in (('OR_KEY', 'synthetic'), ('title_text', lambda kind, tid: tid),
                            ('openrouter', self.router)):
            p = patch.object(self.s, name, value)
            p.start()
            self.addCleanup(p.stop)

    def load_server(self, override):
        spec = importlib.util.spec_from_file_location('w79_server', ROOT / 'server.py')
        s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'TMDB_API': 'offline',
                                    'OPENROUTER_API_KEY': '', 'SKALD_EMBED_MODEL': override}):
            spec.loader.exec_module(s)
        # Join outer jobs before shutting down their leaf pool.
        self.addCleanup(s._pool.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(s._outer.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(s._nzb_pool.shutdown, wait=True, cancel_futures=True)
        return s

    def router(self, path, body, timeout, lane, label):
        self.calls.append((path, body, lane, label))
        return {'data': [{'index': i, 'embedding': [0., 3., 4.] + [0.] * 253}
                         for i in reversed(range(len(body['input'])))]}

    def old_cache(self, keys=('tmdb:1', 'tmdb:2'), model=None):
        encoded = base64.b64encode(struct.pack('256e', 1., *([0.] * 255))).decode()
        raw = {'dim': 256, 'v': dict.fromkeys(keys, encoded),
               'kinds': dict.fromkeys(keys, 'movie')}
        if model is not None:
            raw['model'] = model
        Path(self.temp.name, 'emb.json').write_text(json.dumps(raw))
        self.s.load_emb()

    def wait_for_embed(self):
        job = getattr(self.s, '_emb_job', None)
        if job is not None:
            job.result(timeout=5)

    def needed(self, keys):
        self.s._profiles['household'] = {'entries': [
            {'key': key, 'kind': 'movie', 'weight': 1., 'signal': 'watched'} for key in keys]}

    def test_default_request_and_normalization(self):
        s = self.s
        self.assertEqual(s.EMB_MODEL, NEW)
        self.assertEqual(s.ensure_emb([('tmdb:1', 'movie')]), 1)
        path, body, lane, label = self.calls[0]
        self.assertEqual((path, body['model'], body['dimensions'], lane, label),
                         ('/api/v1/embeddings', NEW, 256, 'embed', 'titles'))
        v = s.femb('tmdb:1', 'movie')
        self.assertIsInstance(v, array)
        self.assertEqual(len(v), 256)
        self.assertAlmostEqual(v[1], .6)
        self.assertAlmostEqual(v[2], .8)
        self.assertAlmostEqual(math.sqrt(sum(x*x for x in v)), 1.)
        s.save_emb()
        self.assertEqual(json.loads(Path(self.temp.name, 'emb.json').read_text())['model'], NEW)

    def test_override_is_requested(self):
        s = self.load_server('synthetic/embedding')
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'title_text', return_value='1'), \
                patch.object(s, 'openrouter', self.router):
            s.ensure_emb([('tmdb:1', 'movie')])
        self.assertEqual(self.calls[0][1]['model'], 'synthetic/embedding')

    def test_legacy_file_loads_and_saves_as_old_model(self):
        self.old_cache()
        self.assertEqual(self.s._emb_model, OLD)
        self.assertEqual(self.s.femb('tmdb:1', 'movie')[0], 1.)
        self.s._emb_dirty.set()
        self.s.save_emb()
        self.assertEqual(json.loads(Path(self.temp.name, 'emb.json').read_text())['model'], OLD)

    def test_partial_coverage_keeps_old_then_switches_and_drops_old(self):
        s = self.s
        self.old_cache(('tmdb:1', 'tmdb:2', 'tmdb:99'), OLD)
        self.needed(('tmdb:1', 'tmdb:2'))
        def partial(path, body, *args):
            return {'data': [{'index': 0, 'embedding': [0., 1.] + [0.] * 254}]}
        with patch.object(s, 'openrouter', partial):
            self.assertEqual(s.ensure_emb([('tmdb:1', 'movie')]), 1)
            self.wait_for_embed()
        self.assertEqual(s._emb_model, OLD)
        self.assertEqual(s.femb('tmdb:1', 'movie')[0], 1.)
        self.assertEqual(s.emb_status(), {'model': OLD, 'active': 2, 'pending': 1, 'needed': 2})
        self.assertAlmostEqual(s.seed_similarity('tmdb:1', 'tmdb:2'), .5)
        with patch.object(s, 'ensure_emb', return_value=0):
            old_vec = s.taste_vector(s._profiles['household'], min_titles=1)
        old_title = s.femb('tmdb:1', 'movie')
        s.ensure_emb([('tmdb:2', 'movie')])
        self.wait_for_embed()
        self.assertEqual(s._emb_model, NEW)
        self.assertEqual(set(s._emb), {'tmdb:1', 'tmdb:2'})
        self.assertIsNone(s.fit(old_vec, 'tmdb:2', 'movie'))
        self.assertIsNone(s.fit(old_title, 'tmdb:2', 'movie'))
        self.assertAlmostEqual(s.fit(s.femb('tmdb:1', 'movie'), 'tmdb:2', 'movie'), .6)
        self.assertAlmostEqual(s.seed_similarity('tmdb:1', 'tmdb:2'), .3)
        self.assertEqual(s.emb_status(), {'model': NEW, 'active': 2, 'pending': 0, 'needed': 2})
        s.save_emb()
        raw = json.loads(Path(self.temp.name, 'emb.json').read_text())
        self.assertEqual(raw['model'], NEW)
        self.assertNotIn('tmdb:99', raw['v'])
        s._emb.clear()
        s.load_emb()
        self.assertEqual(s._emb_model, NEW)
        self.assertEqual(set(s._emb), {'tmdb:1', 'tmdb:2'})

    def test_switch_does_not_wait_for_upstream(self):
        s = self.s
        self.old_cache(('tmdb:1',), OLD)
        started, release = threading.Event(), threading.Event()
        def blocked(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('test did not release fake router')
            return self.router(*args)
        with patch.object(s, 'openrouter', blocked):
            try:
                self.assertEqual(s.ensure_emb([('tmdb:1', 'movie')]), 1)
                self.assertTrue(started.wait(2))
                self.assertEqual(s._emb_model, OLD)
                self.assertEqual(s.femb('tmdb:1', 'movie')[0], 1.)
            finally:
                release.set()
                self.wait_for_embed()
        self.assertEqual(s._emb_model, NEW)

    def test_error_and_budget_keep_old_and_retry_later(self):
        s = self.s
        self.old_cache(('tmdb:1',), OLD)
        for error in (OSError('synthetic failure'), s.BudgetExceeded('embed budget')):
            with patch.object(s, 'openrouter', side_effect=error) as router:
                s.ensure_emb([('tmdb:1', 'movie')])
                self.wait_for_embed()
                self.assertEqual(s._emb_model, OLD)
                self.assertEqual(s.femb('tmdb:1', 'movie')[0], 1.)
                s.ensure_emb([('tmdb:1', 'movie')])
                self.wait_for_embed()
                self.assertEqual(router.call_count, 1)
            with patch.object(s.time, 'time', return_value=s._emb_retry + 1):
                if isinstance(error, s.BudgetExceeded):
                    s.ensure_emb([('tmdb:1', 'movie')])
                    self.wait_for_embed()
                else:
                    # The next iteration can exercise the budget rejection.
                    s._emb_retry = 0
        self.assertEqual(s._emb_model, NEW)

    def test_empty_old_coverage_can_switch(self):
        self.old_cache(('tmdb:99',), OLD)
        self.s.ensure_emb([('tmdb:1', 'movie')])
        self.wait_for_embed()
        self.assertEqual(self.s._emb_model, NEW)
        self.assertEqual(set(self.s._emb), {'tmdb:1'})

    def test_bad_dimensions_never_replace_old(self):
        self.old_cache(('tmdb:1',), OLD)
        with patch.object(self.s, 'openrouter', return_value={
                'data': [{'index': 0, 'embedding': [1., 0.]}]}):
            self.s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
        self.assertEqual(self.s._emb_model, OLD)
        self.assertEqual(len(self.s.femb('tmdb:1', 'movie')), 256)

    def test_bad_response_index_never_replaces_old(self):
        self.old_cache(('tmdb:1',), OLD)
        with patch.object(self.s, 'openrouter', return_value={
                'data': [{'index': -1, 'embedding': [1.] + [0.] * 255}]}):
            self.s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
        self.assertEqual(self.s._emb_model, OLD)

    def test_stats_reports_loaded_model_and_only_coverage(self):
        self.old_cache()
        self.needed(('tmdb:1', 'tmdb:2', 'tmdb:3'))
        self.assertEqual(self.s.stats()['embedding'],
                         {'model': OLD, 'active': 2, 'pending': 0, 'needed': 3})

    def test_cached_rows_count_towards_needed_coverage(self):
        self.old_cache()
        self.needed(('tmdb:1',))
        self.s._feeds['synthetic', 'en-US'] = {'metas': {'movie': [
            {'id': 'tmdb:2', 'type': 'movie'}]}}
        with patch.object(self.s, 'openrouter', return_value={
                'data': [{'index': 0, 'embedding': [1.] + [0.] * 255}]}):
            self.s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
        self.assertEqual(self.s.emb_status(), {'model': OLD, 'active': 2, 'pending': 1, 'needed': 2})

    def test_removed_titles_stop_blocking_migration(self):
        s = self.s
        self.old_cache(('tmdb:1', 'tmdb:2', 'tmdb:3'), OLD)
        self.needed(('tmdb:1', 'tmdb:2'))
        s._feeds[s.rid('because'), 'en-US'] = {'metas': {'movie': [
            {'id': 'tmdb:3', 'type': 'movie'}]}}
        def title(kind, tid):
            if tid != '1':
                raise OSError('synthetic title removed')
            return tid
        with patch.object(s, 'title_text', title):
            s.ensure_emb([('tmdb:1', 'movie'), ('tmdb:2', 'movie'), ('tmdb:3', 'movie')])
            self.wait_for_embed()
            self.assertEqual(s.emb_status(), {'model': OLD, 'active': 3, 'pending': 1, 'needed': 3})
            self.needed(('tmdb:1',))
            s._feeds.clear()
            self.assertEqual(s.emb_status(), {'model': OLD, 'active': 1, 'pending': 1, 'needed': 1})
            s.ensure_emb([])
            self.wait_for_embed()
        self.assertEqual(s._emb_model, NEW)
        self.assertEqual(s.emb_status(), {'model': NEW, 'active': 1, 'pending': 0, 'needed': 1})
        self.assertEqual(set(s._emb), {'tmdb:1'})

    def check_pinned_coverage_after_restart(self, scoped):
        s = self.s
        self.old_cache(('tmdb:1', 'tmdb:2', 'tmdb:3'), OLD)
        cid, lang = s.rid('because'), 'en-US'
        meta = {'id': 'tmdb:2', 'type': 'movie', 'name': 'Synthetic candidate'}
        rec = {'metas': {'movie': [meta]}, 'names': {'seed': 'tmdb:3'}}
        records = {f'{cid}|{lang}': rec}
        scope = next(iter(s.SCOPES)) if scoped else None
        filename = 'shown_scopes.json' if scoped else 'shown.json'
        Path(self.temp.name, filename).write_text(json.dumps({scope: records} if scoped else records))
        with patch.object(s, 'connectors_load'):
            s.load_state()
        self.needed(('tmdb:1',))
        with s.addon_scope(scope):
            self.assertEqual(s.feed(cid, lang), rec)
        with patch.object(s, 'openrouter', return_value={
                'data': [{'index': 0, 'embedding': [1.] + [0.] * 255}]}):
            s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
        self.assertEqual(s._emb_model, OLD)
        self.assertEqual(s.emb_status(), {'model': OLD, 'active': 3, 'pending': 1, 'needed': 3})
        s.ensure_emb([])
        self.wait_for_embed()
        self.assertEqual(s._emb_model, NEW)
        self.assertEqual(set(s._emb), {'tmdb:1', 'tmdb:2', 'tmdb:3'})
        s._facets['tmdb:3'] = {'kind': 'movie', 'g': ['Drama'], 'y': '2000'}
        with patch.object(s, 'tone_ok', return_value=True), \
                patch.object(s, 'quality_ok', return_value=True), \
                patch.object(s, 'row_facts', return_value={'g': {'Drama'}, 'date': '2000'}):
            self.assertTrue(s.because_ok(meta, 'tmdb:3'))

    def test_shared_pinned_row_counts_after_restart(self):
        self.check_pinned_coverage_after_restart(scoped=False)

    def test_scoped_pinned_row_counts_after_restart(self):
        self.check_pinned_coverage_after_restart(scoped=True)

    def test_pending_batches_survive_budget_error(self):
        s = self.s
        keys = tuple(f'tmdb:{i}' for i in range(1, 98))
        self.old_cache(keys, OLD)
        self.needed(keys)
        def limited(*args):
            if self.calls:
                raise s.BudgetExceeded('synthetic daily limit')
            return self.router(*args)
        with patch.object(s, 'openrouter', limited):
            s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
        self.assertEqual(s.emb_status(), {'model': OLD, 'active': 97, 'pending': 96, 'needed': 97})
        with patch.object(s.time, 'time', return_value=s._emb_retry + 1):
            s.ensure_emb([])
            self.wait_for_embed()
        self.assertEqual(s._emb_model, NEW)
        self.assertEqual(len(self.calls[-1][1]['input']), 1)

    def test_migration_preserves_kind_aliases(self):
        self.old_cache(('tmdb:1',), OLD)
        self.s.ensure_emb([('tmdb:1', 'movie'), ('tmdb:1', 'tv')])
        self.wait_for_embed()
        self.assertEqual(self.s._emb_model, NEW)
        self.assertEqual(self.s._emb_kinds, {'tmdb:1': 'movie', 'tmdb:1@tv': 'tv'})
        self.assertIsNotNone(self.s.femb('tmdb:1', 'movie'))
        self.assertIsNotNone(self.s.femb('tmdb:1', 'tv'))

    def test_switch_changes_taste_model_cache_signature(self):
        s = self.s
        self.old_cache(('tmdb:1',), OLD)
        self.needed(('tmdb:1',))
        model = {'aff': {}, 'cal': None, 'taste_line': ''}
        with patch.object(s, 'build_model', return_value=model) as build, \
                patch.object(s, '_memory_read', return_value={'seq': 0}), \
                patch.object(s, 'taste_ring_note'), patch('builtins.print'):
            s.get_model()
            s.get_model()
            self.assertEqual(build.call_count, 1)
            s.ensure_emb([('tmdb:1', 'movie')])
            self.wait_for_embed()
            s.get_model()
            self.assertEqual(build.call_count, 2)

    def test_worker_rebuilds_stale_centroids_after_switch(self):
        s = self.s
        self.old_cache(('tmdb:1',), OLD)
        self.needed(('tmdb:1',))
        with patch.object(s, 'ensure_emb', return_value=0):
            old_vec = s.taste_vector(s._profiles['household'], min_titles=1)
        # A sparse household can still have a cached personal centroid.
        s._model['m'] = {'vec': None, 'pvec': {'ana': old_vec}}
        s.ensure_emb([('tmdb:1', 'movie')])
        self.wait_for_embed()
        with patch.object(s, '_feed_started', True), \
                patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'build_feeds'), patch.object(s, 'get_model') as rebuild:
            s.feed_pass(s.local_now(), {})
        rebuild.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
