"""Selectable home rows and public movie lists, entirely offline."""
import copy
from contextlib import contextmanager, ExitStack
import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from skald import commands, members, setup, shelves
from skald.__main__ import main
import test_prefs
from test_rows_r2 import cards


class RowConfig(unittest.TestCase):
    def test_rows_writer_preserves_concurrent_config_and_refreshes_unlocked(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            data = Path(temp)
            setup.publish(data, members.fresh_config(['alex']))
            original = setup.server
            for blocker in ('prefs', 'shelves', 'process'):
                ready, finished = threading.Event(), threading.Event()
                results, probes = [], []
                @contextmanager
                def server(*args, **kwargs):
                    with original(*args, **kwargs) as s:
                        ready.set()
                        yield s
                def run():
                    try:
                        results.append(main(['setup', 'rows', 'digital'], out=io.StringIO()))
                    finally:
                        finished.set()
                def refresh(data):
                    def probe():
                        for lock in (commands._LOCK, shelves.LOCK):
                            acquired = lock.acquire(blocking=False)
                            probes.append(acquired)
                            if acquired:
                                lock.release()
                        with (Path(data) / 'shelves.lock').open('a') as fh:
                            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    worker = threading.Thread(target=probe)
                    worker.start()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(probes, [True, True])
                with self.subTest(blocker=blocker), patch.object(setup, 'server', server), patch('skald.aio_refresh.refresh', side_effect=refresh):
                    cfg = members.fresh_config(['alex'])
                    setup.publish(data, cfg, replace=True)
                    with ExitStack() as stack:
                        if blocker == 'process':
                            fh = stack.enter_context((data / 'shelves.lock').open('a'))
                            fcntl.flock(fh, fcntl.LOCK_EX)
                        else:
                            stack.enter_context(commands._LOCK if blocker == 'prefs' else shelves.LOCK)
                        worker = threading.Thread(target=run)
                        worker.start()
                        try:
                            self.assertTrue(ready.wait(3))
                            blocked = not finished.wait(.1)
                            cfg['household']['shelves'] = 7
                            cfg['taste']['people']['alex']['prefs'] = {'hide': ['anime']}
                            setup.publish(data, cfg, replace=True)
                        finally:
                            stack.close()
                            worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(blocked, 'rows must wait for the existing config writer')
                    self.assertEqual(results, [0])
                    saved = json.loads((data / 'skald.json').read_text())
                    self.assertEqual(saved['household']['shelves'], 7)
                    self.assertEqual(saved['taste']['people']['alex']['prefs'], {'hide': ['anime']})
                    self.assertEqual(saved['household']['rows'], ['digital'])
                    self.assertEqual(probes, [True, True])

    def test_rows_docs(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('SETUP', 'CLI'):
            text = (root / 'docs' / (name + '.md')).read_text()
            self.assertIn('skald setup rows episodes,foryou,top10,digital,toprated', text)
            self.assertIn('skald setup rows all', text)
        self.assertLessEqual(len((root / 'README.md').read_text().splitlines()), 220)

    def test_invalid_rows(self):
        for rows in ([], ['unknown'], 'top10', [True]):
            cfg = members.fresh_config(['alex'])
            cfg['household']['rows'] = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(members.ConfigError, 'household.rows.*episodes.*toprated'):
                members.validate_config(cfg)

    def test_cli_rows_set_all_and_refresh(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}), \
                patch('skald.aio_refresh.refresh', return_value=None) as refresh:
            setup.publish(Path(temp), members.fresh_config(['alex']))
            path = Path(temp) / 'skald.json'
            for value, line in [('toprated,episodes,digital', 'Rows: toprated, episodes, digital.'), ('all', 'Rows: all default rows.')]:
                out = io.StringIO()
                self.assertEqual(main(['setup', 'rows', value], out=out), 0)
                self.assertEqual(out.getvalue().strip(), line)
                cfg = json.loads(path.read_text())
                self.assertEqual(cfg['household'].get('rows'), None if value == 'all' else value.split(','))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(refresh.call_count, 2)
            before = path.read_bytes()
            for value in ('', 'unknown'):
                self.assertEqual(main(['setup', 'rows', value], out=io.StringIO()), 2)
                self.assertEqual(path.read_bytes(), before)


class SelectedRows(unittest.TestCase):
    setUpClass = classmethod(test_prefs.PrefsRows.setUpClass.__func__)
    prefs = test_prefs.PrefsRows.prefs
    served = test_prefs.PrefsRows.served

    def test_personal_job_skips_disabled_half_work_and_cache(self):
        s = self.s
        p = {'entries': [], 'searches': [], 'likes': 'Synthetic mystery', 'seen': set(), 'negative': set()}
        for key, section in [('foryou', 'FOR YOU'), ('rewatch', 'REWATCH')]:
            cfg = copy.deepcopy(s.CONFIG)
            cfg['household']['rows'] = [key]
            with self.subTest(key=key), patch.object(s, 'CONFIG', cfg), patch.object(s, 'OR_KEY', 'synthetic'), \
                    patch.object(s, '_profiles', {'household': p}), patch.object(s, 'get_model', return_value=None), \
                    patch.object(s, 'profile_text', return_value='Synthetic profile'), \
                    patch.object(s, 'candidate_pool', return_value=[]) as pool, \
                    patch.object(s, 'hit_titles', return_value=[]), patch.object(s, 'tired_keys', return_value={}) as tired, \
                    patch.object(s, 'llm_sections', return_value={'FOR YOU': ['new'], 'REWATCH': ['old']}) as llm, \
                    patch.object(s, 'picks_to_rows', return_value={}) as picks:
                personal = next(j for j in s.job_specs(s.local_now(), {}) if j[0] == 'personal')
                expected = s.rid('foryou' if key == 'foryou' else 'rewatchai')
                self.assertEqual(personal[1], [expected])
                result = personal[4]('en-US')
                self.assertEqual(set(result), {expected})
                self.assertEqual([c.args[0] for c in picks.call_args_list], [['new'] if key == 'foryou' else ['old']])
                self.assertEqual(llm.call_args.args[5], [section])
                self.assertNotIn('## ' + ('REWATCH' if key == 'foryou' else 'FOR YOU'), llm.call_args.args[0])
                if key == 'rewatch':
                    pool.assert_not_called()
                    tired.assert_not_called()

    def test_public_rows_focus_orders_before_truncation_without_filtering(self):
        s = self.s
        xs = [{**x, '_row': {'lang': 'es' if i == 24 else 'en'}} for i, x in enumerate(cards(25))]
        with self.prefs(ana={'focus': {'languages': ['es']}}), patch.object(s, 'frec', return_value={}):
            for key in ('digital', 'toprated'):
                cid = s.rid(key)
                self.assertEqual(self.served(cid, xs, who='ana', typ='movie'), [xs[24], *xs[:19]])
                self.assertEqual(self.served(cid, xs[:8], who='ana', typ='movie'), xs[:8])

    def test_player_order_only_skips_explicitly_disabled_rows(self):
        from eval.rows_report import display_rows
        s = self.s
        with tempfile.TemporaryDirectory() as temp, patch.object(s, 'feed_rows', return_value=[]):
            order = Path(temp) / 'order.mjs'
            order.write_text("const ORDER = ['player.foryou:tv'];")
            with self.assertRaises(KeyError):
                display_rows(s, order=order)
            cfg = copy.deepcopy(s.CONFIG)
            cfg['household']['rows'] = ['foryou']
            with patch.object(s, 'CONFIG', cfg), self.assertRaises(KeyError):
                display_rows(s, order=order)
            cfg['household']['rows'] = ['top10']
            with patch.object(s, 'CONFIG', cfg):
                self.assertEqual(display_rows(s, order=order), [])

    def test_rows_filter_order_manifest_and_jobs(self):
        s = self.s
        cfg = copy.deepcopy(s.CONFIG)
        cfg['household']['rows'] = ['toprated', 'top10', 'episodes', 'digital']
        with patch.object(s, 'CONFIG', cfg), patch.object(s, 'pin_shown'), patch.object(s, 'feed', return_value={}):
            self.assertEqual([r[0] for r in s.feed_rows()], [s.rid(k) for k in ('episodes', 'top10', 'digital', 'toprated')])
            cats = s.manifest('en-US')['catalogs']
            self.assertEqual([r['id'] for r in cats], [s.rid(k) for k in ('episodes', 'top10', 'top10', 'digital', 'toprated', 'search', 'search')])
            self.assertEqual({j[0] for j in s.job_specs(s.local_now(), {})}, {'episodes', 'top10', 'digital', 'toprated'})
            self.assertEqual(s.row_metas(s.rid('because'), 'tv', 'en-US'), [])
        self.assertNotIn(s.rid('digital'), [r[0] for r in s.feed_rows()])
        self.assertNotIn('toprated', {j[0] for j in s.job_specs(s.local_now(), {})})

    def test_rows_reload_without_restart_and_disabled_cache(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            cfg = members.fresh_config(['alex'])
            setup.publish(Path(temp), cfg)
            with setup.server(Path(temp)) as s, patch.object(s, 'feed', return_value={'metas': {'movie': cards(8)}}):
                cfg['household']['rows'] = ['digital']
                setup.publish(Path(temp), cfg, replace=True)
                s.refresh_preferences()
                self.assertEqual([r[0] for r in s.feed_rows()], [s.rid('digital')])
                self.assertEqual(s.row_metas(s.rid('top10'), 'movie', 'en-US'), [])
                self.assertNotIn(s.rid('top10'), [r['id'] for r in s.guest_rows_view('en-US')['rows']])
                with patch.object(s, 'served_row', return_value=[]) as served:
                    s.build_home('en-US', {}, (set(), set()), 'alex')
                    self.assertEqual([(c.args[0], c.args[1]) for c in served.call_args_list], [(s.rid('digital'), 'movie')])
                cfg['household'].pop('rows')
                setup.publish(Path(temp), cfg, replace=True)
                s.refresh_preferences()
                self.assertNotIn(s.rid('digital'), [r[0] for r in s.feed_rows()])

    def test_row_key_groups_and_skipped_name_reads(self):
        s = self.s
        cfg = copy.deepcopy(s.CONFIG)
        for key, cids in [('people', list(s.ROW_OWNER)), ('moods', s.MOODS_ROWS), ('lanes', s.LANES),
                          ('docs', s.DOCS), ('studios', s.STUDIO_ROWS),
                          ('anime', [s.rid('airing'), s.rid('nextseason')]), ('shelves', [r[0] for r in s.shelf_rows()])]:
            cfg['household']['rows'] = [key]
            with self.subTest(key=key), patch.object(s, 'CONFIG', cfg), patch.object(s, 'feed', return_value={}) as feed:
                self.assertEqual({r[0] for r in s.feed_rows()}, set(cids))
                self.assertTrue(all(s.row_enabled(c.args[0]) for c in feed.call_args_list))

    def test_child_selection_keeps_only_listed_keys(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'feed', return_value={}):
            s.CONFIG['household']['rows'] = ['people']
            self.assertEqual({r[0] for r in s.feed_rows()}, set(s.ROW_OWNER))
            self.assertNotIn('family', {j[0] for j in s.job_specs(s.local_now(), {})})

    def fake_tmdb(self, path, **params):
        if path == 'genre/movie/list':
            return {'genres': []}
        if path.endswith('/release_dates'):
            i = int(path.split('/')[1])
            country, typ, days = {0: ('DE', 4, 0), 1: ('DE', 4, 60), 2: ('DE', 4, 61),
                                  3: ('DE', 4, -1), 4: ('US', 4, 5), 5: ('DE', 3, 5)}.get(i, ('DE', 4, 5))
            date = (datetime.date(2026, 10, 12) - datetime.timedelta(days=days)).isoformat()
            return {'results': [{'iso_3166_1': country, 'release_dates': [{'type': typ, 'release_date': date + 'T00:00:00Z'}]}]}
        return {'results': [{'id': i, 'title': 'Synthetic Movie ' + str(i), 'adult': i == 6,
                             'popularity': i, 'vote_count': 199 if i == 7 else 500,
                             'vote_average': 8, 'release_date': '2020-01-01'} for i in range(12)]}

    def test_digital_release_country_type_window_and_adult(self):
        s = self.s
        cfg = copy.deepcopy(s.CONFIG)
        cfg['region']['country'] = 'Germany'
        with patch.object(s, 'CONFIG', cfg), patch.object(s, 'tmdb', side_effect=self.fake_tmdb), patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 12)):
            xs = s.digital_feed('en-US')['movie']
        self.assertEqual([x['id'] for x in xs], ['tmdb:' + str(i) for i in (11, 10, 9, 8, 7, 1, 0)])

    def test_toprated_vote_floor_and_adult(self):
        s = self.s
        with patch.object(s, 'tmdb', side_effect=self.fake_tmdb) as tmdb:
            xs = s.toprated_feed('en-US')['movie']
        self.assertTrue(any(c.args[0] == 'movie/top_rated' for c in tmdb.call_args_list))
        self.assertEqual([x['id'] for x in xs], ['tmdb:' + str(i) for i in range(12) if i not in (6, 7)])

    def test_public_rows_safety_hide_minimum_and_no_personal_order(self):
        s = self.s
        xs = cards(12)
        for key in ('digital', 'toprated'):
            cid = s.rid(key)
            with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', side_effect=lambda key, kind: {'cr': {'US': 'G' if int(key[5:]) < 7 else 'R'}}):
                self.assertEqual(self.served(cid, xs, who='ana', typ='movie'), xs[:7])
                self.assertEqual(self.served(cid, xs, above={x['id'] for x in xs}, who='ana', typ='movie'), [])
                self.assertEqual(self.served(cid, xs[:5], who='ana', typ='movie'), [])
            with self.prefs(ana={'hide': ['movies']}), patch.object(s, 'frec', return_value={}):
                self.assertEqual(self.served(cid, xs, who='ana', typ='movie'), [])
            with self.prefs(ana={'hide': ['animation']}), patch.object(s, 'frec', return_value={}):
                self.assertEqual(self.served(cid, [{**x, 'genres': ['Animation']} for x in xs], who='ana', typ='movie'), [])

    def test_public_rows_keep_explicit_blocks_and_vetoes(self):
        s = self.s
        xs = cards(8)
        with patch.object(s, 'row_list', return_value=xs), patch.object(s, 'frec', return_value={}):
            for key in ('digital', 'toprated'):
                result = s.served_row(s.rid(key), 'movie', 'en-US', {}, set(),
                                      ({'tmdb:0'}, set(), {'tmdb:1'}), who='ana')
                self.assertEqual(result, xs[2:])
