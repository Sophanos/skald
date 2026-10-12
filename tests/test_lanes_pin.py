"""Manifest-time lane pins and the persisted taste ring; no network."""
from skald.members import rid
import copy
import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from eval.profile_golden import NOW, ROOT, offline_server
from skald.engine import Engine
from test_api import raw_request
import test_lanes_r3 as r3

cards = r3.cards

SESSIONS = ('none', 'ana', 'ben', 'both')
LANG = 'de-DE'


class LanesPin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        r3.LanesR3.setUp(self)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(self.s, 'DATA', temp))
        self.stack.enter_context(patch.object(self.s, 'DEDUPE_ORDER', self.s.LANES))
        self.stack.enter_context(patch.dict(self.s._taste_ring, {}, clear=True))
        self.stack.enter_context(patch.dict(self.s._pin_cache, {}, clear=True))

    put = r3.LanesR3.put
    facet = r3.LanesR3.facet

    def test_pin_cache_hit_ignores_viewer_and_shown(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        with patch.object(s, 'served_row', wraps=s.served_row) as serve:
            s.pin_shown(LANG)
            calls = serve.call_count
            self.assertEqual({c.kwargs['who'] for c in serve.call_args_list}, set(SESSIONS))  # both has its own W10b veto mask
            pinned = s.feed(s.LANES[0], LANG)
            s._shown.clear()
            for who in SESSIONS:
                with patch.object(s, 'who_watching', return_value=(who, 'tap')):
                    s.pin_shown(LANG)
                self.assertEqual(serve.call_count, calls)
                self.assertIs(s.feed(s.LANES[0], LANG), pinned)

    def test_pin_cache_misses_on_changed_inputs(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        masks = self.stack.enter_context(patch.object(s, 'remote_masks', return_value=(set(), set())))
        clock = self.stack.enter_context(patch.object(s, 'local_now', return_value=s.local_now()))
        model = self.stack.enter_context(patch.object(s, 'get_model', return_value={'test': True}))
        with patch.object(s, 'served_row', wraps=s.served_row) as serve:
            s.pin_shown(LANG)
            calls = serve.call_count
            def miss():
                nonlocal calls
                s.pin_shown(LANG)
                self.assertGreater(serve.call_count, calls)
                calls = serve.call_count
            self.put(s.LANES[0], cards(16, 100), 'k:2')
            miss()
            masks.return_value = ({'tmdb:100'}, {'tmdb:101'})
            miss()
            s._profiles['household'] = copy.deepcopy(s._profiles['household'])
            miss()
            s._profiles['household']['hash'] = 'changed'
            miss()
            clock.return_value += datetime.timedelta(days=1)
            miss()
            clock.return_value = clock.return_value.replace(hour=4)
            miss()
            clock.return_value += datetime.timedelta(hours=2)
            miss()
            model.return_value = {'test': 'new'}
            miss()
            s.clear_seed_cache()
            miss()

    def test_preferences_reload_before_manifest_and_invalidate_lane_pins(self):
        s = self.s
        cid = s.LANES[0]
        self.put(cid, [{**x, 'genres': ['Horror']} for x in cards(16)], 'k:1')
        self.put(s.LANE_BENCH[0], [{**x, 'genres': ['Drama']} for x in cards(16, 100)], 'k:2')
        path = Path(s.DATA, 'skald.json')
        cfg = copy.deepcopy(s.CONFIG)
        path.write_text(json.dumps(cfg))
        with patch.dict(os.environ, {'SKALD_CONFIG': str(path)}), patch.object(s, 'CONFIG_SOURCE', 'file'), \
                patch.object(s, 'CONFIG', copy.deepcopy(cfg)), patch.object(s, '_prefs_stamp', None), \
                patch.object(s, '_pin_epoch', s._pin_epoch), patch.dict(s._shown_scoped, {}, clear=True):
            s.manifest(LANG, 'ana')
            self.assertEqual(s._shown_scoped['ana'][cid, LANG]['names']['lane'], 'k:1')
            for catalog_first in (False, True):
                with self.subTest(catalog_first=catalog_first):
                    cfg['taste']['people']['ana']['prefs'] = {'excluded_genres': {'movie': ['Horror']}}
                    path.write_text(json.dumps(cfg))
                    if catalog_first:
                        s.row_metas(cid, 'tv', LANG, who='ana')
                    catalogs = s.manifest(LANG, 'ana')['catalogs']
                    self.assertIn(cid, {row['id'] for row in catalogs})
                    self.assertEqual(s._shown_scoped['ana'][cid, LANG]['names']['lane'], 'k:2')
                    with s.addon_scope('ana'):
                        self.assertGreaterEqual(len(s.row_metas(cid, 'tv', LANG, who='ana')), s.ROW_MIN)
                    cfg['taste']['people']['ana'].pop('prefs')
                    path.write_text(json.dumps(cfg))
                    s.manifest(LANG, 'ana')
                    self.assertEqual(s._shown_scoped['ana'][cid, LANG]['names']['lane'], 'k:1')

    def test_prewarm_computes_bench_but_only_manifest_applies_it(self):
        s = self.s
        self.put(s.LANES[0], cards(4), 'k:1')
        self.put(s.LANE_BENCH[0], cards(16, 100), 'k:2')
        old = s._feeds[s.LANES[0], LANG]
        s._shown[s.LANES[0], LANG] = old
        pins = s.pin_decision(LANG)
        self.assertEqual(pins[s.LANES[0], LANG]['names']['lane'], 'k:2')
        self.assertTrue(any(rec is old for rec in s._pin_cache[LANG][2]))
        self.assertIs(s.feed(s.LANES[0], LANG), old)
        self.assertFalse(Path(s.DATA, 'shown.json').exists())
        with patch.object(s, 'served_row', side_effect=AssertionError('prewarm cache miss')):
            s.pin_shown(LANG)
        self.assertEqual(s.feed(s.LANES[0], LANG)['names']['lane'], 'k:2')

    def test_feed_rebuild_prewarms_once_after_storing_feeds(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        rows = {s.LANES[0]: s._feeds[s.LANES[0], LANG]['metas'],
                '_names': {s.LANES[0]: s._feeds[s.LANES[0], LANG]['names']}}
        jobs = [('lanes', [s.LANES[0]], 'new', 0, lambda lang: rows)]
        with patch.object(s, '_langs', {LANG}), patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'job_specs', return_value=jobs), patch.object(s, 'save_feeds'), \
                patch.object(s, 'pin_decision', wraps=s.pin_decision) as warm, patch('builtins.print'):
            s.feed_pass(s.local_now(), {})
        warm.assert_called_once_with(LANG)
        self.assertIs(s._pin_cache[LANG][1][s.LANES[0], LANG], s._feeds[s.LANES[0], LANG])
        self.assertFalse(s._shown)
        with patch.object(s, 'served_row', side_effect=AssertionError('rebuild cache miss')):
            s.pin_shown(LANG)

    def test_catalog_can_finish_while_prewarm_is_evaluating(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        expected = s.row_metas(s.LANES[0], 'tv', LANG, who='none')
        entered, release = threading.Event(), threading.Event()
        errors, result = [], []
        original = s.served_row
        def serve(*args, **kwargs):
            if threading.current_thread().name == 'prewarm':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('catalog queued behind prewarm')
            return original(*args, **kwargs)
        def run(fn):
            try:
                fn()
            except BaseException as e:
                errors.append(e)
        with patch.object(s, 'served_row', side_effect=serve):
            warm = threading.Thread(name='prewarm', target=run, args=(lambda: s.pin_decision(LANG),), daemon=True)
            catalog = threading.Thread(target=run, args=(lambda: result.append(s.row_metas(s.LANES[0], 'tv', LANG, who='none')),), daemon=True)
            try:
                warm.start()
                self.assertTrue(entered.wait(2))
                catalog.start()
                catalog.join(1)
                self.assertFalse(catalog.is_alive(), 'catalog blocked by prewarm')
                self.assertEqual(result, [expected])
            finally:
                release.set()
                warm.join(3)
                if catalog.ident:
                    catalog.join(3)
        self.assertFalse(warm.is_alive())
        self.assertEqual(errors, [])

    def test_cross_session_name_content_and_first_safe_unused_bench(self):
        s = self.s
        for i, cid in enumerate(s.LANES):
            self.put(cid, cards(4 if i < 2 else 16, i*100), f'k:{i+1}')
        self.put(s.LANE_BENCH[0], cards(16, 600), 'k:6')
        self.put(s.LANE_BENCH[1], cards(16, 700), 'k:7')
        for manifest_who in SESSIONS:
            with self.subTest(manifest=manifest_who), patch.object(s, 'who_watching', return_value=(manifest_who, 'tap')):
                names = {r['id']: r['name'] for r in s.manifest(LANG)['catalogs'] if r['id'] in s.LANES}
                for cid, bench in zip(s.LANES, s.LANE_BENCH):
                    self.assertEqual(s.feed(cid, LANG), {**s._feeds[bench, LANG], '_bench': bench})
                for who in SESSIONS:
                    for cid in s.LANES:
                        row = s.row_metas(cid, 'tv', LANG, who=who)
                        self.assertTrue(row)
                        self.assertEqual(names[cid], row.names['title'])

    def test_primary_and_bench_must_reach_floor_in_every_session(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        self.put(s.LANE_BENCH[0], cards(16, 100), 'k:2')
        self.put(s.LANE_BENCH[1], cards(16, 200), 'k:3')
        def order(xs, who):
            return xs[:4] if who == 'ben' and xs and xs[0]['id'] in ('tmdb:0', 'tmdb:100') else xs
        with patch.object(s, 'person_order', side_effect=order):
            s.pin_shown(LANG)
        self.assertEqual(s.feed(s.LANES[0], LANG)['names']['lane'], 'k:3')
        s._feeds.pop((s.LANE_BENCH[1], LANG))
        s._shown.clear()
        with patch.object(s, 'person_order', side_effect=order):
            s.pin_shown(LANG)
            self.assertEqual(s.feed(s.LANES[0], LANG)['names']['lane'], 'k:1')
            self.assertFalse(s.row_metas(s.LANES[0], 'tv', LANG, who='ben'))

    def test_no_live_swap_after_pin_or_feed_rebuild(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        self.put(s.LANE_BENCH[0], cards(16, 100), 'k:2')
        s.pin_shown(LANG)
        pinned = s.feed(s.LANES[0], LANG)
        self.put(s.LANES[0], cards(16, 200), 'k:3')
        with patch.object(s, 'remote_masks', return_value=({x['id'] for x in cards(16)}, set())), \
                patch.object(s, 'row_metas', wraps=s.row_metas) as compose:
            for who in SESSIONS:
                row = s.row_metas(s.LANES[0], 'tv', LANG, who=who)
                self.assertFalse(row)
                self.assertEqual(row.names, pinned['names'])
            before = compose.call_count
            s.feed_rows(LANG)
            self.assertEqual(s.learn_id(s.LANES[0], LANG), 'lane:k:1')
            self.assertEqual(compose.call_count, before)
        self.assertIs(s.feed(s.LANES[0], LANG), pinned)

    def test_bench_pin_keeps_rows_above_and_their_refill_supply(self):
        s = self.s
        self.put(s.LANES[0], cards(4), 'k:1')
        self.put(s.LANE_BENCH[0], cards(16, 100), 'k:2')
        with patch.object(s, 'DEDUPE_ORDER', [rid('foryou', s.CONFIG)] + s.LANES):
            before = s.row_metas(rid('foryou', s.CONFIG), 'tv', LANG, who='none')
            pool = list(s.row_pool(LANG))
            s.pin_shown(LANG)
            self.assertEqual(list(s.row_pool(LANG)), pool)
            self.assertEqual(s.row_metas(rid('foryou', s.CONFIG), 'tv', LANG, who='none'), before)
            row = s.row_metas(s.LANES[0], 'tv', LANG, who='none')
            self.assertEqual(len(row), 16)
            self.assertEqual(row.names['lane'], 'k:2')

    def test_restart_keeps_bench_record_and_its_reserve(self):
        s = self.s
        self.put(s.LANES[0], cards(4), 'k:1')
        self.put(s.LANE_BENCH[0], cards(4, 100), 'k:2', cards(12, 200))
        s.pin_shown(LANG)
        pinned = copy.deepcopy(s.feed(s.LANES[0], LANG))
        s.save_feeds()
        s._shown.clear()
        s._feeds.clear()
        s._home_cache.clear()
        s.load_state()
        self.assertEqual(s.feed(s.LANES[0], LANG), pinned)
        self.assertEqual(pinned['names']['lane'], 'k:2')
        row = s.row_metas(s.LANES[0], 'tv', LANG, who='none')
        self.assertEqual(len(row), 16)
        self.assertEqual(row.names, pinned['names'])

    def test_old_shown_without_bench_or_pool(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        old = copy.deepcopy(s._feeds[s.LANES[0], LANG])
        old['metas'].pop('pool')
        for metas in ({'tv': cards(16)}, {'movie': cards(16), 'series': []}):
            old['metas'] = metas
            Path(s.DATA, 'shown.json').write_text(json.dumps({s.LANES[0]+'|'+LANG: old}))
            self.put(s.LANES[0], cards(16, 100), 'k:2')
            s._shown.clear()
            s._home_cache.clear()
            s.load_state()
            self.assertEqual(s.feed(s.LANES[0], LANG), old)
            row = s.row_metas(s.LANES[0], 'tv', LANG, who='none')
            self.assertEqual(len(row), 16)
            self.assertEqual(row.names['lane'], 'k:1')

    def test_manifest_during_catalog_has_no_lock_inversion(self):
        s = self.s
        self.put(s.LANES[0], cards(16), 'k:1')
        held, waiting = threading.Event(), threading.Event()
        lock = threading.Lock()
        errors = []
        class HomeLock:
            def __enter__(self):
                if threading.current_thread().name == 'manifest':
                    waiting.set()
                lock.acquire()
            def __exit__(self, *args):
                lock.release()
        def model(*args):
            if threading.current_thread().name == 'catalog':
                held.set()
                if not waiting.wait(2):
                    raise AssertionError('manifest did not arrive during catalog')
            return None
        def run(fn):
            try:
                fn()
            except BaseException as e:
                errors.append(e)
        with patch.object(s, '_home_lock', HomeLock()), patch.object(s, 'get_model', side_effect=model):
            catalog = threading.Thread(name='catalog', target=run, args=(lambda: s.row_metas(s.LANES[0], 'tv', LANG, who='none'),), daemon=True)
            manifest = threading.Thread(name='manifest', target=run, args=(lambda: s.manifest(LANG),), daemon=True)
            catalog.start()
            self.assertTrue(held.wait(2))
            manifest.start()
            catalog.join(3)
            manifest.join(3)
            self.assertFalse(catalog.is_alive() or manifest.is_alive(), 'manifest/catalog deadlock')
        self.assertEqual(errors, [])

    def test_name_helper_all_four_callers_and_learning_uses_served_names(self):
        s = self.s
        cid = s.LANES[0]
        self.put(cid, cards(16), 'k:1')
        s.pin_shown(LANG)
        row = s.row_metas(cid, 'tv', LANG, who='none')
        self.put(cid, cards(16, 100), 'k:2')
        s._shown[cid, LANG] = s._feeds[cid, LANG]
        engine = Engine.__new__(Engine)
        engine._server = s
        engine.score = lambda key: {'reasons': []}
        with patch.object(s, 'feed_rows', return_value=[(cid, s.MIX, 'Fallback')]), \
                patch.object(s, 'row_metas', return_value=row) as compose, \
                patch.object(s, 'title_view', return_value=None), \
                patch.object(s, 'row_name', wraps=s.row_name) as name, \
                patch.object(s, 'note_served') as note:
            req = {'viewer': None, 'query': {}}
            for fn in (lambda: s.remote_rows(LANG), lambda: engine.rows(LANG), lambda: s.rows_view(req)):
                self.assertEqual(fn()['rows'][0]['name'], 'Title k:1')
            before = compose.call_count
            self.assertEqual(raw_request(s, f'/{LANG}/catalog/tv/{cid}.json', handler=s.Handler)[0], 200)
            self.assertEqual(compose.call_count, before+1)
            note.assert_called_once_with('lane:k:1', row)
            self.assertEqual(name.call_count, 4)
        self.assertEqual(s.row_name(cid, LANG), 'Title k:2')
        self.assertEqual(s.learn_id(cid, LANG), 'lane:k:2')

    def test_taste_ring_persistence_overwrite_private_and_clamp(self):
        s = self.s
        self.stack.enter_context(patch.object(s, 'local_now', side_effect=lambda ts=None:
            datetime.datetime.fromtimestamp(NOW if ts is None else ts, datetime.timezone.utc)))
        m = {'aff': {'k:1': .1}, 'paff': {p: {'k:1': .1} for p in s.PEOPLE},
             'names': {'k:1': 'Synthetic'}, 'ent': {}, 'holders': {}}
        for i in range(10):
            s.taste_ring_note(m, NOW+i*s.DAY)
        self.assertEqual(len(s._taste_ring), s.TASTE_RING_DAYS)
        m['aff']['k:1'] = .9
        s.taste_ring_note(m, NOW+9*s.DAY+60)
        newest = max(s._taste_ring)
        self.assertEqual(json.loads(zlib.decompress(s._taste_ring[newest]['household'])), {'k:1': .9})
        path = Path(s.DATA, 'taste_ring.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = copy.deepcopy(s._taste_ring)
        s._taste_ring.clear()
        s.load_state()
        self.assertEqual(s._taste_ring, saved)
        with patch.object(s, 'get_model', return_value=m):
            reply = s.taste_report_view({'viewer': None, 'query': {'days': 365}})
        self.assertEqual(reply['movers']['from'], min(saved))
        self.assertEqual(reply['movers']['to'], newest)
        self.assertEqual(reply['movers']['facets'][0]['delta'], .8)

    def test_missing_or_corrupt_taste_ring_is_empty(self):
        s = self.s
        path = Path(s.DATA, 'taste_ring.json')
        for raw in (None, 'bad json', '[]', '{"not-a-day":{}}', '{"2026-10-11":{"household":"bad"}}'):
            s._taste_ring['old'] = {}
            if raw is not None:
                path.write_text(raw)
            s.load_state()
            self.assertEqual(s._taste_ring, {})

    @unittest.skipUnless(os.environ.get('SKALD_SNAPSHOT'), 'set SKALD_SNAPSHOT for snapshot gates')
    def test_snapshot_all_manifest_catalog_session_pairs(self):
        from eval.rows_report import home_server, stub_refills
        with patch('time.time', return_value=NOW):
            for stub in (False, True):
                with home_server(os.environ['SKALD_SNAPSHOT']) as s:
                    if stub:
                        stub_refills(s)
                    for manifest_who in SESSIONS:
                        with patch.object(s, 'who_watching', return_value=(manifest_who, 'tap')):
                            names = {r['id']: r['name'] for r in s.manifest(LANG)['catalogs'] if r['id'] in s.LANES}
                        for who in SESSIONS:
                            for cid in s.LANES:
                                row = s.row_metas(cid, 'tv', LANG, who=who)
                                with self.subTest(stub=stub, manifest=manifest_who, catalog=who, slot=cid):
                                    self.assertTrue(not row or names[cid] == row.names['title'])


if __name__ == '__main__':
    unittest.main()
