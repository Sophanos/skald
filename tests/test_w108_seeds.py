"""Bulk player marks and favourite seeds, with synthetic history and transports."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from history_kit import NOW
from skald import members


class BulkSeeds(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.s.CONFIG = self.s.fresh_config(['ana', 'ben'])
        self.s.PEOPLE = ('ana', 'ben')
        self.addCleanup(members.configure)
        self.at = int(NOW * 1000)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def episode(self, tid, number, at=None, played=True, favorite=False):
        return ('series', tid, self.at if at is None else at, played, favorite, 1 if played else .4, [1, number])

    def history(self, adapter, records, ben=()):
        if adapter == 'player':
            with redirect_stdout(io.StringIO()):
                return [item for profile, rows in (('ana', records), ('ben', ben))
                        for item in self.s._player_history(rows, profile, 'jellyfin')['items']]
        path = self.data / 'watch.sqlite'
        with sqlite3.connect(path) as con:
            con.execute('DROP TABLE IF EXISTS watch_state')
            con.execute('CREATE TABLE watch_state (uuid TEXT, media_type TEXT, base_id TEXT, '
                        'last_played_at INTEGER, played INTEGER, kind TEXT, favorite INTEGER, '
                        'duration_ms INTEGER, position_ms INTEGER, rating INTEGER, likes INTEGER, '
                        'dropped INTEGER, season INTEGER, episode INTEGER)')
            con.executemany('INSERT INTO watch_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [(prefix + '-device', typ, 'tmdb:' + str(tid), at, int(played),
                  'episode' if episode else 'movie', int(favorite), 100, int(progress * 100),
                  None, None, 0, episode[0] if episode else None, episode[1] if episode else None)
                 for prefix, rows in (('a1b2c3d4', records), ('b1b2c3d4', ben))
                 for typ, tid, at, played, favorite, progress, episode in rows])
        options = {'path': str(path), 'profiles': {'a1b2c3d4': 'ana', 'b1b2c3d4': 'ben'}}
        self.s.CONFIG['connectors']['aiostreams_db'] = {'options': options}
        return self.s._aiostreams_db(options)['items']

    def details(self, item):
        tid = int(item['id'][5:])
        return ('tv' if item['type'] == 'series' else 'movie',
                {'id': tid, 'name': f'Synthetic {tid}', 'title': f'Synthetic {tid}',
                 'first_air_date': '2020-01-01', 'release_date': '2020-01-01', 'genres': []})

    def profile(self, items):
        with patch.object(self.s.time, 'time', return_value=NOW), \
                patch.object(self.s, 'tmdb_details', side_effect=self.details), \
                patch.object(self.s, 'person_loves', return_value=[]):
            return self.s.build_profile(items, [], remote=False)

    def test_bulk_marks_keep_seen_counts_and_favorite_but_have_no_recent_date(self):
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                items = self.history(adapter, [self.episode(101, i, favorite=True) for i in range(1, 157)])
                item, = items
                self.assertIsNone(item['at'])
                self.assertTrue(item['backfilled'])
                self.assertEqual((item['played'], item['episodes_played'], item['last_episode'],
                                  item['favorite'], item['entries']), (True, 156, [1, 156], True, 156))
                self.assertNotIn('resume_episode', item)
                p = self.profile(items)
                self.assertEqual(p['seen'], {'tmdb:101'})
                self.assertEqual(p['entries'][0]['age'], 999)

    def test_later_real_play_or_resume_restores_recency(self):
        for adapter in ('db', 'player'):
            for played in (True, False):
                with self.subTest(adapter=adapter, played=played):
                    records = [self.episode(101, i) for i in range(1, 6)]
                    records += [self.episode(101, 6, self.at + 60_123, played=played)]
                    item, = self.history(adapter, records)
                    self.assertEqual(item['at'], self.at + 60_123)
                    self.assertTrue(item['backfilled'])
                    self.assertEqual(item['episodes_played'], 5 + int(played))
                    self.assertEqual(item['last_episode'], [1, 6 if played else 5])
                    if not played:
                        self.assertEqual(item['resume_episode'], [1, 6])
                    self.assertLess(self.profile([item])['entries'][0]['age'], 1)

    def test_four_episodes_are_real_and_minutes_are_per_profile(self):
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                items = self.history(adapter, [self.episode(101, i) for i in range(1, 5)],
                                     [self.episode(102, i) for i in range(1, 5)])
                self.assertEqual({item['profile'] for item in items}, {'ana', 'ben'})
                for item in items:
                    self.assertEqual(item['at'], self.at)
                    self.assertFalse(item.get('backfilled', False))

    def test_real_date_before_bulk_is_kept_when_resume_is_in_the_bulk_minute(self):
        records = [self.episode(101, i, self.at + i * 1000) for i in range(1, 6)]
        records += [self.episode(101, 0, self.at - 60_000), self.episode(101, 6, self.at + 15_000, played=False)]
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                item, = self.history(adapter, records)
                self.assertEqual(item['at'], self.at - 60_000)
                self.assertTrue(item['backfilled'])
                self.assertEqual((item['episodes_played'], item['last_episode'], item['resume_episode']),
                                 (6, [1, 5], [1, 6]))

    def test_bulk_minutes_span_titles_and_also_backfill_movies(self):
        records = [self.episode(101 if i <= 3 else 102, i, self.at + i * 1000) for i in range(1, 6)]
        records += [('movie', 103, self.at + 9999, True, False, 1, None)]
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                items = self.history(adapter, records)
                self.assertEqual(len(items), 3)
                for item in items:
                    self.assertIsNone(item['at'])
                    self.assertTrue(item['backfilled'])
                self.assertEqual(self.profile(items)['seen'], {'tmdb:101', 'tmdb:102', 'tmdb:103'})

    def test_movies_and_unplayed_episodes_do_not_make_a_bulk_episode_minute(self):
        records = [self.episode(101, i) for i in range(1, 5)]
        records += [self.episode(101, 5, played=False)]
        records += [('movie', 200 + i, self.at, True, False, 1, None) for i in range(5)]
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                for item in self.history(adapter, records):
                    self.assertEqual(item['at'], self.at)
                    self.assertFalse(item.get('backfilled', False))

    def test_row_limit_orders_by_real_activity_before_bulk_marks(self):
        records = [self.episode(1000 + i, ep) for i in range(121) for ep in range(1, 6)]
        records += [self.episode(101, 1, self.at - 60_000)]
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                items = self.history(adapter, records)
                self.assertEqual(len(items), 120)
                self.assertEqual(items[0]['id'], 'tmdb:101')
                self.assertEqual(items[0]['at'], self.at - 60_000)

    def test_bulk_only_is_old_in_recent_seeds_because_and_continue(self):
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                records = [self.episode(101, i) for i in range(1, 157)]
                records += [self.episode(102, i, self.at - i * 60_000) for i in (1, 2)]
                p = self.profile(self.history(adapter, records))
                self.assertEqual(self.s.top_seeds(p, 1)[0]['key'], 'tmdb:102')
                bulk = next(e for e in p['entries'] if e['key'] == 'tmdb:101')
                self.assertEqual(bulk['age'], 999)
                with patch.object(self.s.random, 'Random') as rng:
                    rng.return_value.choices.side_effect = lambda seeds, weights: [seeds[weights.index(max(weights))]]
                    self.assertEqual(self.s.because_seed(p)['key'], 'tmdb:102')
                self.s._profiles['household'] = p
                paths = []
                def tmdb(path, **params):
                    paths.append(path)
                    return {}
                with patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'tmdb', side_effect=tmdb):
                    self.assertEqual(self.s.episodes_feed('en-US'), {'series': []})
                self.assertEqual(set(paths), {'tv/102'})

    def test_bulk_title_beyond_metadata_limit_stays_seen(self):
        records = [self.episode(101, i) for i in range(1, 157)]
        records += [self.episode(200 + i, 1, self.at - (i + 1) * 60_000) for i in range(40)]
        for adapter in ('db', 'player'):
            with self.subTest(adapter=adapter):
                self.s.CONFIG['history']['source'] = 'aiostreams_db' if adapter == 'db' else 'jellyfin'
                items = self.history(adapter, records)
                self.assertEqual(items[-1]['id'], 'tmdb:101')
                with patch.object(self.s, 'tmdb_details', side_effect=self.details) as details, \
                        patch.object(self.s, 'person_loves', return_value=[]), patch.object(self.s.time, 'time', return_value=NOW):
                    p = self.s.build_profile(items, [], remote=False)
                    self.assertEqual(details.call_count, 40)
                self.assertEqual(p['seen'], {item['id'] for item in items})

    def test_refresh_profiles_rebuilds_bulk_title_after_real_play_or_resume(self):
        for adapter in ('db', 'player'):
            for played in (True, False):
                with self.subTest(adapter=adapter, played=played):
                    self.s._profiles.clear()
                    self.s.CONFIG['history']['source'] = 'aiostreams_db' if adapter == 'db' else 'jellyfin'
                    records = [self.episode(101, i) for i in range(1, 6)]
                    items = self.history(adapter, records)
                    with patch.object(self.s, 'read_history', side_effect=lambda: items), \
                            patch.object(self.s, 'active_profiles', return_value=['household']), \
                            patch.object(self.s, 'tmdb_details', side_effect=self.details), \
                            patch.object(self.s, 'person_loves', return_value=[]), \
                            patch.object(self.s, 'learn_from_plays'), patch.object(self.s, 'log_play_context'), \
                            patch.object(self.s.time, 'time', return_value=NOW + 60), redirect_stdout(io.StringIO()):
                        before = self.s.refresh_profiles()
                        self.assertEqual(self.s._profiles['household']['entries'][0]['age'], 999)
                        items = self.history(adapter, records + [self.episode(101, 6, self.at + 60_000, played=played)])
                        after = self.s.refresh_profiles()
                    self.assertNotEqual(before['household'], after['household'])
                    p = self.s._profiles['household']
                    self.assertLess(p['entries'][0]['age'], 1)
                    paths = []
                    def tmdb(path, **params):
                        paths.append(path)
                        return {'id': 101, 'name': 'Synthetic 101', 'seasons': [{'season_number': 1, 'episode_count': 10}],
                                'last_episode_to_air': {'season_number': 1, 'episode_number': 10}}
                    with patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'tmdb', side_effect=tmdb), \
                            patch.object(self.s, 'row_meta', return_value={'id': 'tmdb:101'}):
                        self.assertEqual(self.s.episodes_feed('en-US')['series'], [{'id': 'tmdb:101'}])
                    self.assertEqual(set(paths), {'tv/101'})

    def entry(self, tid, age=1, signal='watched', weight=1, episodes=0, backfilled=False):
        return {'key': f'tmdb:{tid}', 'id': tid, 'title': f'Synthetic {tid}', 'kind': 'tv',
                'stype': 'series', 'signal': signal, 'weight': weight, 'age': age, 'who': 'owner',
                'item': {'episodes_played': episodes, 'backfilled': backfilled,
                         'at': None if backfilled and age == 999 else int((NOW - age * self.s.DAY) * 1000)}}

    def pool_seeds(self, entries, model=None, negative=()):
        paths, counts = [], []
        def tmdb(path, **params):
            if path.endswith('/recommendations'):
                tid = int(path.split('/')[1])
                paths.append(tid)
                return {'results': [{'id': 1000000 + tid * 100 + i, 'name': f'Synthetic pick {i}', 'vote_count': 100}
                                    for i in range(25)]}
            return {'results': []}
        def rank(m, items, **params):
            counts[:] = items
            return [(1, [], key, kind, payload) for key, kind, payload in items]
        p = {'entries': entries, 'seen': {e['key'] for e in entries}, 'negative': set(negative)}
        with patch.object(self.s, 'get_model', return_value=model), patch.object(self.s, 'tired_keys', return_value={}), \
                patch.object(self.s, 'tmdb', side_effect=tmdb), patch.object(self.s, 'rank_by_taste', side_effect=rank), \
                patch.object(self.s, 'franchise_key', side_effect=lambda meta: meta['id']), \
                patch.object(self.s, 'match_pct', return_value=None), patch.object(self.s, 'reasons', return_value=[]):
            out = self.s.candidate_pool(p, n=200)
        return paths, len(counts) if model else len(out)

    def test_pool_reserves_three_favorites_with_wider_seed_limit(self):
        recent = [self.entry(i) for i in range(1, 21)]
        favorites = [self.entry(101, 400, 'loved', 2), self.entry(102, 400, 'liked', 2),
                     self.entry(103, 400, episodes=50)]
        for model, limit, per in ((None, 16, 6), ({'vec': None}, 16, 20)):
            with self.subTest(model=bool(model)):
                paths, count = self.pool_seeds(recent + favorites, model)
                self.assertEqual(len(paths), limit)
                self.assertEqual(set(paths) & {101, 102, 103}, {101, 102, 103})
                self.assertIn(1, paths)
                self.assertEqual(count, limit * per)

    def test_pool_favorites_use_year_decay_and_exclude_unloved_backfill_only(self):
        recent = [self.entry(i) for i in range(1, 21)]
        favorites = [self.entry(101, 100, 'liked', 2), self.entry(102, 400, 'liked', 3),
                     self.entry(103, 500, episodes=50)]
        excluded = [self.entry(104, 999, 'liked', 10, backfilled=True),
                    self.entry(105, 999, weight=10, episodes=156, backfilled=True),
                    self.entry(106, 10, 'disliked', -1, episodes=50)]
        loved = self.entry(107, 999, 'loved', 10, backfilled=True)
        paths, _ = self.pool_seeds(recent + favorites + excluded + [loved])
        self.assertEqual(set(paths), {*range(1, 14), 101, 102, 107})
        self.assertEqual(len(paths), len(set(paths)))

    def test_pool_sparse_history_keeps_recent_seeds_without_duplicates(self):
        entries = [self.entry(101, 400, 'loved', 2), self.entry(102), self.entry(103)]
        paths, count = self.pool_seeds(entries)
        self.assertEqual(set(paths), {101, 102, 103})
        self.assertEqual(count, 18)

    def test_pool_favorites_exclude_household_negative_keys(self):
        entries = [self.entry(i) for i in range(1, 5)]
        entries += [{**self.entry(101, 400, 'loved', 2), 'by': ['ana']},
                    {**self.entry(101, 400, 'disliked', -1), 'by': ['ben']}]
        paths, count = self.pool_seeds(entries, negative={'tmdb:101'})
        self.assertEqual(set(paths), {1, 2, 3, 4})
        self.assertEqual(count, 24)

    def test_pool_favorites_are_unique_by_title_and_kind(self):
        recent = [self.entry(i) for i in range(1, 5)]
        favorites = [{**self.entry(101, 400, 'loved', 2), 'by': ['ana']},
                     {**self.entry(101, 400, 'liked', 2), 'by': ['ben']}]
        paths, count = self.pool_seeds(recent + favorites)
        self.assertEqual(set(paths), {1, 2, 3, 4, 101})
        self.assertEqual(len(paths), 5)
        self.assertEqual(count, 30)

    def test_pool_favorites_exclude_identity_already_in_recent_seeds(self):
        entries = [self.entry(i) for i in range(1, 5)]
        entries += [{**self.entry(1, 400, 'loved', 2), 'by': ['ana']}]
        paths, count = self.pool_seeds(entries)
        self.assertEqual(set(paths), {1, 2, 3, 4})
        self.assertEqual(len(paths), 4)
        self.assertEqual(count, 24)

    def test_pool_favorites_exclude_undated_unloved_player_summary(self):
        records = [self.episode(101, i) for i in range(1, 157)]
        item, = self.history('player', records)
        self.s.CONFIG['history']['source'] = 'jellyfin'
        self.s.CONFIG['history']['max_raw_items'] = 0
        (self.data / 'history.json').write_text(json.dumps({'items': [item]}))
        with patch.object(self.s, 'tmdb_details', return_value=('tv', {
                'id': 101, 'name': 'Synthetic 101', 'genres': []})), redirect_stdout(io.StringIO()):
            self.assertEqual(self.s.cleanup_history(NOW)['pruned'], 1)
        restored, = self.s.read_history()
        self.assertNotIn('backfilled', restored)
        self.assertIsNone(restored['at'])
        summary, = self.profile([restored])['entries']
        self.assertEqual(summary['signal'], 'watched')
        recent = [{**self.entry(i), 'who': 'tv'} for i in range(1, 5)]
        paths, count = self.pool_seeds(recent + [summary])
        self.assertEqual(set(paths), {1, 2, 3, 4})
        self.assertEqual(count, 24)
        loved = {**summary, 'signal': 'loved', 'weight': 2}
        self.assertIn(101, self.pool_seeds(recent + [loved])[0])
