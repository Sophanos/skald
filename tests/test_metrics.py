"""Online measurement acceptance, offline fixtures and bounded-state checks."""
from skald.members import rid
from decide_kit import write_legacy
from contextlib import redirect_stdout
import copy
import datetime
import gc
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from skald.__main__ import main, text_output
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def at(wall):
    return datetime.datetime.fromisoformat(wall).replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()


class MetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('metrics_server', ROOT / 'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': ''}):
            spec.loader.exec_module(cls.s)
        for name in ('_pool', '_outer', '_nzb_pool'):
            cls.addClassCleanup(getattr(cls.s, name).shutdown)

    def setUp(self):
        timezone = patch.object(self.s, 'TZ', 'Europe/Berlin')
        timezone.start()
        self.addCleanup(timezone.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        cfg = json.loads((self.data / 'legacy.json').read_text())
        cfg['region']['tz'] = 'Europe/Berlin'
        (self.data / 'legacy.json').write_text(json.dumps(cfg))
        s = self.s
        s._online = None
        s._online_meta = {'errors': 0, 'corrupt': False, 'skipped': 0, 'unmapped': 0}
        s._online_dirty.clear()
        s._online_error_at = None
        s._searches = []
        s._learn = {'served': {}, 'rows': {}, 'hits': [], 'credited': {}, 'lanes': {}, 'ctx': [], 'moodpicks': {}}
        s._learn_dirty.clear()
        s._remote_cache.clear()
        for name, value in (('DATA', self.temp.name), ('OR_KEY', 'offline')):
            p = patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, value):
        (self.data / (name + '.json')).write_text(json.dumps(value))

    def metas(self, *keys, kind='movie'):
        return [{'id': 'tmdb:' + str(k), 'type': kind} for k in keys]

    def item(self, key, t, **kwargs):
        return {'id': 'tmdb:' + str(key), 'type': 'movie', 'profile': 'household',
                'at': t * 1000 if t is not None else None, **kwargs}

    def observe(self, items, now):
        self.s.note_tv({'entries': []}, items=items, now=now)

    def active(self, *times):
        self.s._searches = [(t, 'synthetic') for t in times]
        self.observe([], max(times))

    def test_a1_activity_boundaries(self):
        cases = [('2026-10-06T20:10', '2026-10-06T23:05', 'open', True),
                 ('2026-10-06T20:10', '2026-10-07T00:10', 'play', False),
                 ('2026-10-06T04:30', '2026-10-06T05:10', 'search', False),
                 ('2026-10-10T22:00', '2026-10-10T19:30', 'remote', True),
                 ('2026-10-10T22:00', None, None, False)]
        for fetch, activity, source, yes in cases:
            with self.subTest(fetch=fetch, activity=activity):
                self.s._online = None
                self.s._searches = []
                t = at(fetch)
                self.s.note_exposure('row', self.metas(1), t=t)
                now = max(t, at(activity)) if activity else t
                if source in ('open', 'play'):
                    self.observe([self.item(99, at(activity), progress=.33 if source == 'play' else 0)], now)
                elif source == 'remote':
                    with patch.object(self.s, 'feedback_state', return_value={'active': {'event': {'t': at(activity)}}, 'tonight': {}}):
                        self.observe([], now)
                elif activity:
                    self.active(at(activity))
                report = self.s.metrics(now=now)
                self.assertEqual(report['impressions'], int(yes))
                self.assertEqual((report['fetches'], report['exposures']), (1, 1))

    def test_a1_signature_dedup_and_remote_activity(self):
        s, t = self.s, at('2026-10-06T20:10')
        for _ in range(300):
            s.note_exposure('row', self.metas(1, 2), t=t)
        feedback = {'active': {}, 'tonight': {('tmdb:9', 'both'): {'t': t + 3 * 3600}}}
        with patch.object(s, 'feedback_state', return_value=feedback):
            self.observe([], t + 4 * 3600)
        report = s.metrics(now=t + 4 * 3600)
        self.assertEqual((report['fetches'], report['exposures'], report['impressions'], report['title_days']), (300, 1, 1, 2))
        rec = next(iter(s.online_state()['exp'][s.online_day(t)[0]]['row'].values()))
        self.assertEqual(rec, [300, 15, int(t)])
        s.note_exposure('row', self.metas(1, 2), t=at('2026-10-06T21:40'))
        self.assertEqual(len(rec), 5)
        self.assertEqual(s.metrics(now=t + 4 * 3600)['title_days'], 2)
        s.note_exposure('row', self.metas(2, 1), t=t)
        report = s.metrics(now=t + 4 * 3600)
        self.assertEqual((report['exposures'], report['impressions'], report['title_days']), (2, 2, 2))

    def test_a2_legacy_calendar_bug_and_forward_only_credit(self):
        s = self.s
        fetched, played = at('2026-10-10T01:00'), at('2026-10-10T00:30')
        # Earlier baseline establishes since, without showing the tested title.
        s.note_exposure('other', self.metas(99), t=played - 3600)
        with patch.object(s.time, 'time', return_value=fetched):
            s.note_served(rid('foryou', s.CONFIG), self.metas(1, 2, 7))
        item = self.item(7, played, progress=.33)
        entry = {'key': 'tmdb:7', 'kind': 'movie', 'who': 'tv', 'signal': 'interest', 'title': 'Synthetic', 'item': item}
        self.write('history', {'items': [item]})
        with patch.object(s.time, 'time', return_value=fetched), redirect_stdout(io.StringIO()):
            s.learn_from_plays({'entries': [entry]})
        self.assertEqual(s._learn['rows'][rid('foryou', s.CONFIG)]['hits'], 1)
        self.assertEqual(s.metrics(now=fetched)['plays']['credited'], 0)
        self.assertEqual(s.metrics(now=fetched)['plays']['n'], 1)

    def test_a2_shares_kind_window_and_repeat_days(self):
        s, t = self.s, at('2026-10-10T20:00')
        for lid in ('a', 'b'):
            s.note_exposure(lid, self.metas(7), t=t)
        self.observe([self.item(7, t + 1800, progress=.33)], t + 1800)
        self.observe([self.item(7, t + 1800, progress=.33)], t + 1800)
        r = s.metrics(now=t + 1800)
        self.assertEqual((r['plays']['n'], r['plays']['credited']), (1, 1))
        self.assertEqual([r['rows'][lid]['credit'] for lid in ('a', 'b')], [.5, .5])
        self.observe([self.item(7, t + 3600, type='series', episodes_played=1)], t + 3600)
        self.assertEqual(s.metrics(now=t + 3600)['plays']['credited'], 1)
        self.observe([self.item(7, t + 22 * s.DAY, progress=.6)], t + 22 * s.DAY)
        self.assertEqual(s.metrics(now=t + 22 * s.DAY)['plays']['credited'], 0)
        s._online = None
        s.note_exposure('a', self.metas(7), t=t)
        self.observe([self.item(7, t + 60, progress=.33)], t + 60)
        self.observe([self.item(7, t + s.DAY + 60, progress=.6)], t + s.DAY + 60)
        r = s.metrics(now=t + s.DAY + 60)
        self.assertEqual((r['plays']['n'], r['plays']['credited']), (2, 2))
        self.assertEqual(r['rows']['a']['credit'], 2)
        self.assertIsNone(r['rows']['a']['ci'])  # Two play-days from one title-day isn't binomial.
        with patch.object(s.time, 'time', return_value=t):
            s.note_served(rid('foryou', s.CONFIG), self.metas(7))
        for when, progress in ((t + 60, .33), (t + s.DAY + 60, .6)):
            entry = {'key': 'tmdb:7', 'kind': 'movie', 'who': 'tv', 'signal': 'interest',
                     'title': 'Synthetic', 'item': self.item(7, when, progress=progress)}
            with patch.object(s.time, 'time', return_value=when), redirect_stdout(io.StringIO()):
                s.learn_from_plays({'entries': [entry]})
        self.assertEqual(s._learn['rows'][rid('foryou', s.CONFIG)]['hits'], 1)

    def test_a3_hit_depths_and_wilson(self):
        s, t = self.s, at('2026-10-10T20:00')
        s.note_exposure('row', self.metas(*range(20)), t=t)
        self.observe([self.item(k, t + 60, progress=.33) for k in (3, 10, 15)], t + 60)
        r = s.metrics(now=t + 60)
        self.assertEqual(r['plays']['hit'], {'5': [1, 3, .061, .792], '12': [2, 3, .208, .939], '20': [3, 3, .438, 1.]})
        self.assertEqual(s.online_wilson(0, 3), [0., .562])
        self.assertEqual(s.online_wilson(1, 1), [.207, 1.])
        self.assertIsNone(s.online_wilson(0, 0))
        self.assertEqual((r['title_days'], r['titles']), (12, 12))

    def test_a4_raw_delta_and_mapping(self):
        s, t = self.s, at('2026-10-10T20:00')
        cases = [(None, {'progress': .33}, 1), (None, {'type': 'series', 'episodes_played': 0, 'progress': 0}, 0),
                 ({'type': 'series', 'episodes_played': 58}, {'type': 'series', 'episodes_played': 58, 'progress': .1}, 0),
                 ({'type': 'series', 'episodes_played': 58}, {'type': 'series', 'episodes_played': 59}, 1),
                 ({'progress': .33}, {'progress': .6}, 1), ({'played': False}, {'played': True}, 1)]
        for i, (prior, current, real) in enumerate(cases):
            if prior is not None:
                self.observe([self.item(i, t, **prior)], t)
            self.observe([self.item(i, t + s.DAY, **current)], t + s.DAY)
            self.assertEqual(s.online_state()['tv'][-1][2], real)
            n = len(s.online_state()['tv'])
            self.observe([self.item(i, t + s.DAY, **current)], t + s.DAY)
            self.assertEqual(len(s.online_state()['tv']), n)
        self.observe([self.item(99, None)], t)
        self.assertEqual(s._online_meta['skipped'], 1)
        self.observe([self.item(100, t, id='tt-unmapped')], t)
        self.assertEqual(s._online_meta['unmapped'], 1)
        raw = self.item(101, t, id='tt-mapped', progress=0, type='series')
        p = {'entries': [{'key': 'tmdb:101', 'kind': 'tv', 'item': raw, 'who': 'both', 'signal': 'watched'}]}
        self.assertTrue(s.real_play(p['entries'][0]))
        s.note_tv(p, items=[raw], now=t)
        self.assertEqual(s.online_state()['tv'][-1], [t, 'tmdb:101@tv', 0])
        trakt = {'key': 'tmdb:102', 'kind': 'movie', 'who': 'both', 'signal': 'watched', 'item': {'id': 'tt-trakt'}}
        s.note_tv({'entries': [trakt]}, items=[self.item(102, t, type='series', progress=0)], now=t)
        self.assertEqual(s.online_state()['tv'][-1][1:], ['tmdb:102@tv', 0])

    def test_a5_stale_profile_sees_new_episode(self):
        s, t = self.s, at('2026-10-10T20:00')
        old = self.item(1419, t, type='series', episodes_played=58, played=True)
        p = {'entries': [{'key': 'tmdb:1419', 'kind': 'tv', 'who': 'tv', 'signal': 'watched', 'title': 'Synthetic', 'item': old}]}
        s.note_exposure('row', self.metas(1419, kind='series'), t=t + 10)
        self.write('history', {'items': [old]})
        with patch.object(s.time, 'time', return_value=t + 20):
            s.learn_from_plays(p)
        newer = {**old, 'at': (t + s.DAY) * 1000, 'episodes_played': 59}
        with patch.object(s, 'recent_searches', return_value=[]), patch.object(s, 'read_trakt', return_value=[]), \
                patch.object(s, 'feedback_hash', return_value=''):
            self.assertEqual(s.taste_hash('household', [old]), s.taste_hash('household', [newer]))
        legacy = json.dumps(s._learn)
        self.write('history', {'items': [newer]})
        with patch.object(s.time, 'time', return_value=t + s.DAY):
            s.learn_from_plays(p)
        self.assertEqual(json.dumps(s._learn), legacy)
        r = s.metrics(now=t + s.DAY)
        self.assertEqual((r['plays']['n'], r['plays']['pre_since'], r['plays']['credited']), (1, 1, 1))

    def test_a6_clicks_time_and_abandon(self):
        s, t = self.s, at('2026-10-10T20:00')
        s.note_exposure('row', self.metas(1, 2, 3, 4), t=t)
        for k, minutes in ((1, 12), (2, 30), (3, 60)):
            self.observe([self.item(k, t + minutes * 60, progress=.33)], t + minutes * 60)
        self.observe([self.item(4, t + 180 * 60)], t + 180 * 60)
        r = s.metrics(now=t + 180 * 60)
        self.assertEqual(r['clicks'], 1)
        self.assertEqual(r['ttp_min'], {'n': 4, 'p25': 12., 'p50': 30., 'p75': 60.})
        s._online = None
        now = t + 4 * s.DAY
        self.observe([self.item(1, now - 72 * 3600), self.item(2, now - 72 * 3600), self.item(3, now - 10 * 3600)], now)
        self.observe([self.item(2, now - 42 * 3600, progress=.33)], now)
        self.assertEqual(s.metrics(now=now)['abandon'], {'k': 1, 'n': 2, 'pending': 1, 'ci': [.095, .905]})

    def test_credit_uses_older_impressions_and_future_resolution(self):
        s, t = self.s, at('2026-10-10T20:00')
        s.note_exposure('old', self.metas(1), t=t)
        self.active(t)
        self.observe([self.item(1, t + s.DAY, progress=.33)], t + s.DAY)
        r = s.metrics(now=t + s.DAY, days=1)
        self.assertEqual((r['impressions'], r['plays']['credited'], r['rows']['old']['credit']), (0, 1, 1))
        self.assertIsNone(r['ttp_min'])
        # A real play outside the reporting window still resolves its earlier open.
        s._online = None
        self.observe([self.item(2, t)], t)
        self.observe([self.item(2, t + 30 * 3600, progress=.33)], t + 30 * 3600)
        r = s.metrics(now=t + 72 * 3600)
        self.assertEqual(r['abandon']['k'], 0)

    def test_who_context_relabels_and_sessions(self):
        s, t = self.s, at('2026-10-10T22:00')
        windows = [{'t': t, 'until': t + 3600, 'who': 'ana'}]
        with patch.object(s, 'session_windows', create=True, return_value=windows), \
                patch.object(s, 'who_watching', create=True, return_value=('ana', 'tap')) as who:
            s.note_exposure('row', self.metas(1), t=t)
            self.observe([self.item(1, t + 60, progress=.33)], t + 3600)
            r = s.metrics(now=t + 3600)
            self.assertEqual(r['who']['ana'], {'impressions': 1, 'plays': 1, 'credited': 1})
            self.assertEqual(r['who']['sessions'], {'n': 1, 'min': {'p25': 60., 'p50': 60., 'p75': 60.}})
            who.return_value = ('ben', 'fix')
            self.assertIn('ben', s.metrics(now=t + 3600)['who'])
            self.assertNotIn('ana', s.metrics(now=t + 3600)['who'])
        self.assertNotIn('who', s.metrics(now=t + 3600))
        self.assertEqual(r['context']['weekend/late night']['impressions'], 1)
        self.assertEqual(s.online_cell(at('2026-10-11T10:00')), 'weekend/morning')
        self.assertEqual(s.online_cell(at('2026-10-09T23:00')), 'weekend/late night')
        self.assertEqual(r['days']['span_min']['p50'], 1.)

    def test_persistence_pruning_and_reintern(self):
        s, t = self.s, at('2026-10-10T20:00')
        s.note_exposure('row', self.metas(1, 2), t=t - 35 * s.DAY)
        s.note_exposure('row', self.metas(3, 4), t=t - 34 * s.DAY)
        s.note_exposure('row', self.metas(4, 5), t=t)
        self.active(t - 35 * s.DAY, t)
        state = s.online_state()
        state['tv'] = [[t - 121 * s.DAY, 'tmdb:1@movie', 1]] + [[t - i, 'tmdb:4@movie', 1] for i in range(3001)]
        with patch.object(s.time, 'time', return_value=t):
            s.save_online()
        saved = json.loads((self.data / 'online.json').read_text())
        self.assertEqual(len(saved['exp']), 2)
        self.assertEqual(len(saved['act']), 1)
        self.assertEqual(len(saved['lists']), 2)
        self.assertEqual(set(saved['keys']), {'tmdb:3@movie', 'tmdb:4@movie', 'tmdb:5@movie'})
        self.assertEqual(len(saved['tv']), s.ONLINE_TV_MAX)
        s._online = None
        self.assertEqual(s.online_state(), saved)
        s.note_exposure('row', self.metas(5, 6), t=t)
        self.assertEqual(s._online_index['tmdb:6@movie'], 3)
        s._learn_dirty.clear()
        with patch.object(s.time, 'time', return_value=t):
            s.save_learn()
        self.assertFalse((self.data / 'learn.json').exists())
        self.assertIn('tmdb:6@movie', json.loads((self.data / 'online.json').read_text())['keys'])

    def test_obs_prune_watermark_prevents_duplicate_tv_on_resync(self):
        s, now = self.s, at('2026-10-11T18:00')
        items = [self.item(k, now - k, played=True) for k in range(2005)]
        state = s.online_state()
        self.assertNotIn('obs_floor', state)
        self.observe(items, now)
        self.assertEqual(len(state['tv']), 2005)
        with patch.object(s.time, 'time', return_value=now):
            s.save_online()
        self.assertEqual(state['obs_floor'], now - 2000)
        self.assertEqual(len(state['obs']), 2000)
        s._online = None
        state = s.online_state()
        self.observe(items, now)
        self.assertEqual(len(state['tv']), 2005)
        self.assertEqual(s._online_meta['skipped'], 5)
        self.observe([self.item(2004, now + 1, played=True)], now + 1)
        self.assertEqual(len(state['tv']), 2006)
        floor = state['obs_floor']
        state['obs'].pop('tmdb:0')
        state['obs'].update({f'old{i}': [floor - 100 - i, False, 0, 0] for i in range(5)})
        with patch.object(s.time, 'time', return_value=now + s.DAY):
            s.save_online()
        self.assertEqual(state['obs_floor'], floor)

    def test_obs_floor_validation_and_legacy_load(self):
        s, now = self.s, at('2026-10-11T18:00')
        state = s.online_state()
        self.write('online', state)
        s._online = None
        self.assertNotIn('obs_floor', s.online_state())
        self.observe([self.item(1, now - 120 * s.DAY, played=True)], now)
        self.assertEqual(len(s.online_state()['tv']), 1)
        for floor in (-1, True, 'bad', float('nan')):
            with self.subTest(floor=floor), self.assertRaisesRegex(ValueError, 'online observation floor'):
                s.online_valid({**state, 'obs_floor': floor})

    def test_missing_corrupt_and_pure_reads(self):
        s, t = self.s, at('2026-10-10T20:00')
        self.assertEqual(s.metrics(now=t)['plays']['hit'], {'5': None, '12': None, '20': None})
        s.online_summary()
        s.save_online()
        self.assertEqual([p.name for p in self.data.iterdir()], ['legacy.json'])
        for raw in ('broken', '{"v":1}', json.dumps({'v': 1, 'since': None, 'keys': [], 'lists': {}, 'exp': {}, 'act': {}, 'tv': [[t, 'bare', 1]], 'obs': {}})):
            s._online = None
            (self.data / 'online.json').write_text(raw)
            with redirect_stdout(io.StringIO()):
                s.online_state()
            self.assertTrue(s._online_meta['corrupt'])
            self.assertEqual((self.data / 'online.json.bad').read_text(), raw)
            self.assertFalse((self.data / 'online.json').exists())
        s.note_exposure('row', self.metas(1), t=t)
        self.active(t)
        before = copy.deepcopy(s.online_state())
        self.assertEqual(s.metrics(now=t), s.metrics(now=t))
        self.assertEqual(before, s.online_state())
        with self.assertRaises(ValueError):
            s.metrics(now=t, days=0)

    def test_a9_legacy_identical_and_fail_safe_hooks(self):
        s, t = self.s, at('2026-10-10T20:00')
        empty = copy.deepcopy(s._learn)
        item = self.item(1, t + 1800, progress=.33)
        self.write('history', {'items': [item]})
        p = {'entries': [{'key': 'tmdb:1', 'kind': 'movie', 'item': item, 'who': 'tv', 'signal': 'interest', 'title': 'Synthetic'}]}
        def fixture():
            with patch.object(s.time, 'time', return_value=t), redirect_stdout(io.StringIO()):
                s.note_served(rid('foryou', s.CONFIG), self.metas(1, 2))
            with patch.object(s.time, 'time', return_value=t + 3600), redirect_stdout(io.StringIO()):
                s.learn_from_plays(p)
                s.log_play_context(p)
                s.save_learn()
            return json.dumps(s._learn)
        live = fixture()
        s._learn = copy.deepcopy(empty)
        with patch.object(s, 'note_exposure'), patch.object(s, 'note_tv'), patch.object(s, 'save_online'):
            self.assertEqual(live, fixture())
        self.assertNotIn('"hour"', json.dumps(s.online_summary()))
        with patch.object(s, 'online_state', side_effect=RuntimeError('synthetic')), redirect_stdout(io.StringIO()) as logs:
            s._online_dirty.set()
            s.note_served(rid('foryou', s.CONFIG), self.metas(2))
            s.learn_from_plays(p)
            s.save_learn()
            s.learn_stats()
        self.assertEqual(s._online_meta['errors'], 4)
        self.assertEqual(len(logs.getvalue().splitlines()), 1)

    def test_cli_json_text_and_privacy(self):
        from skald import metrics as row_metrics
        s, t = self.s, at('2026-10-10T20:00')
        s.note_exposure('row', self.metas(1), t=t)
        self.observe([self.item(1, t + 60, progress=.33)], t + 60)
        with patch.object(s.time, 'time', return_value=t + 60):
            s.save_online()
        self.write('profile', {'entries': [], 'seen': [], 'negative': [], 'genres': {}})
        config = json.loads((self.data/'legacy.json').read_text())
        config['metrics'] = {'enabled': True}
        (self.data/'legacy.json').write_text(json.dumps(config))
        row_metrics.record_shown(self.data, config, rid('week', config), 'ana', self.metas(1), t)
        row_metrics.update(self.data, config, [dict(key='tmdb:1', kind='movie', person='ana',
                           at=t+60, source='history')], t+60)
        # The CLI's local state must be outside its read-only snapshot.
        with tempfile.TemporaryDirectory() as state, patch('time.time', return_value=t + 60):
            args = ['--snapshot', str(self.data), '--state-dir', state]
            out = io.StringIO()
            self.assertEqual(main(args + ['metrics', '--days', '1', '--json'], out=out), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report['rows'][0]['played'], 1)
            raw = json.dumps(report)
            self.assertNotIn('tmdb:', raw)
            self.assertNotIn('"hour"', raw)
            self.assertNotIn('"at"', raw)
            out = io.StringIO()
            self.assertEqual(main(args + ['metrics'], out=out), 0)
            self.assertLessEqual(len(out.getvalue().splitlines()), 20)
            self.assertIn('100.0%', out.getvalue())
            self.assertIn('Shown', out.getvalue())
            out = io.StringIO()
            self.assertEqual(main(args + ['learn', '--json'], out=out), 0)
            self.assertIn('online', json.loads(out.getvalue()))
            self.assertNotIn('"at": 17', out.getvalue())
        empty = row_metrics.report(self.data, config, now=t - s.DAY)
        out = io.StringIO()
        text_output(SimpleNamespace(command='metrics'), empty, out)
        self.assertIn('Played', out.getvalue())

    def test_a8_budgets(self):
        s, now = self.s, at('2026-10-10T23:00')
        for day in range(35):
            for row in range(34):
                for variant in range(4):
                    base = (day * 137 + row * 79 + variant * 23) % 3000
                    metas = self.metas(*[(base + i) % 3000 for i in range(20)])
                    for h in range(6):
                        s.note_exposure('row:' + str(row), metas, t=now - day * s.DAY - h * 3600)
            self.active(now - day * s.DAY - 2 * 3600)
        self.observe([self.item(k, now - k, progress=.33) for k in range(500)], now)
        with patch.object(s.time, 'time', return_value=now):
            s.save_online()
        size = (self.data / 'online.json').stat().st_size
        s._online = None
        s._online_index = {}
        gc.collect()
        tracemalloc.start()
        try:
            s.online_state()
            memory = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        start = time.perf_counter()
        report = s.metrics(now=now, days=35)
        metrics_ms = (time.perf_counter() - start) * 1000
        self.assertEqual(report['exposures'], 3952)
        self.assertEqual(len(s.online_state()['lists']), 2000)
        times = []
        for _ in range(1000):
            start = time.perf_counter()
            s.note_exposure('row:0', self.metas(*range(20)), t=now)
            times.append((time.perf_counter() - start) * 1000)
        p95 = sorted(times)[949]
        # All 500 items advance, rather than benchmarking an idempotent no-op.
        items = [self.item(k, now + 60, progress=.6) for k in range(500)]
        start = time.perf_counter()
        s.note_tv({'entries': []}, items=items, now=now + 60)
        tv_ms = (time.perf_counter() - start) * 1000
        print(f'A8 bytes={size} loaded_bytes={memory} metrics_ms={metrics_ms:.3f} exposure_p95_ms={p95:.3f} tv_ms={tv_ms:.3f}')
        self.assertLessEqual(size, 1_500_000)
        self.assertLessEqual(memory, 4_000_000)
        self.assertLessEqual(metrics_ms, 500)
        self.assertLessEqual(p95, 1)
        self.assertLessEqual(tv_ms, 20)

    def test_snapshot_replay_synthetic_order_kind_and_read_only(self):
        from eval.online_report import online_report
        t = at('2026-10-10T20:00')
        self.write('learn', {'served': {'tmdb:1': {'rows': {'a': {'first': t + .02, 'days': []}, 'b': {'first': t + 30, 'days': []}}},
                                      'tmdb:2': {'rows': {'a': {'first': t + .01, 'days': []}}},
                                      'tmdb:3': {'rows': {'a': {'first': t + 20, 'days': []}}},
                                      'tmdb:4': {'rows': {'a': {'first': t + 40, 'days': []}}}},
                             'rows': {'a': {'shown': 4}, 'b': {'shown': 1}}, 'hits': [], 'ctx': []})
        self.write('feeds', {'row': {'metas': {'movie': self.metas(1)}}})
        self.write('shown', {'row': {'metas': {'series': self.metas(2, kind='series')}}})
        self.write('facets', {'items': {'tmdb:3': {}, 'tmdb:3@tv': {'kind': 'tv'}}})
        self.write('profile', {'entries': []})
        self.write('history', {'items': [self.item(1, t + 60)]})
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        report = online_report(self.data, '2026-10-10T21:00')
        self.assertEqual(report['legacy'], {'shown': 5, 'learn_ids': 2, 'titles': 4, 'hits': 0, 'ctx': 0})
        self.assertEqual(report['replay'], {'observations': 1, 'unknown_keys': 1})
        online = report['online']
        self.assertEqual((online['fetches'], online['exposures'], online['title_days']), (4, 4, 5))
        self.assertEqual(online['clicks'], 1)
        self.assertEqual(online['window']['since'], '2026-10-10 20:00')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})
        self.assertNotIn('tmdb:', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
