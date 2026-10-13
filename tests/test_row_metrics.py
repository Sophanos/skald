"""Local row attribution, using synthetic ids and temporary directories only."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from skald import members, metrics, migrate
from skald.__main__ import main

DAY = 86400
NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc).timestamp()


class RowMetrics(unittest.TestCase):
    def test_folded_play_month_does_not_award_exact_time_credit(self):
        from eval.profile_golden import ROOT, offline_server
        shown = dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc).timestamp()
        played = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc).timestamp()
        now = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc).timestamp()
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl'):
                (data / name).unlink()
            s.CONFIG['metrics'] = {'enabled': True}
            s.CONFIG['history']['max_raw_items'] = 0
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': played * 1000, 'history_by': ['ana']}]}))
            row = s.rid('week')
            metrics.record_shown(data, s.CONFIG, row, 'ana', [{'id': 'tmdb:5', 'type': 'movie'}], shown)
            plays, sources = metrics.collect(s, now)
            metrics.update(data, s.CONFIG, plays, now, sources=sources)
            self.assertEqual(metrics.report(data, s.CONFIG, now, weeks=6)['rows'][0]['played'], 0)
            s.cleanup_history(now)
            plays, sources = metrics.collect(s, now)
            metrics.update(data, s.CONFIG, plays, now, sources=sources)
            report = metrics.report(data, s.CONFIG, now, weeks=6)
            self.assertEqual(report['rows'][0]['played'], 0)
            self.assertEqual(report['sources']['history'], {'dated': 0, 'undated': 1})
            self.assertEqual([p['at'] for p in plays], [None])

    def test_forget_purges_attribution_and_invalidates_raw_and_folded_play_credit(self):
        from skald.history import forget
        for folded in (False, True):
            with self.subTest(folded=folded), tempfile.TemporaryDirectory() as data:
                path = Path(data)
                for person in ('ana', 'ben', 'together'):
                    metrics.record_shown(data, self.cfg, self.row, person,
                                         [{'id': 'tmdb:1', 'type': 'movie'}], NOW)
                plays = [self.play('ana'), self.play('ben')]
                now = NOW + (31 if folded else 2) * DAY
                metrics.update(data, self.cfg, plays, now)
                self.assertEqual({r['person']: r['played'] for r in metrics.report(data, self.cfg, now, weeks=6)['rows']},
                                 {'ana': 1, 'ben': 1, 'together': 1})
                before = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
                preview = forget(data, self.cfg, '2026-10-08', 'ana')
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()})
                result = forget(data, self.cfg, '2026-10-08', 'ana', apply=True)
                state = json.loads((path / 'metrics_shown.json').read_text())
                self.assertTrue(all(r['person'] == 'ben' for r in state['shown']))
                metrics.update(data, self.cfg, plays, now)
                rows = {r['person']: r for r in metrics.report(data, self.cfg, now, weeks=6)['rows']}
                self.assertEqual({p: r['played'] for p, r in rows.items()}, {'ana': 0, 'ben': 1, 'together': 0})
                for person in ('ana', 'together'):
                    self.assertEqual(rows[person]['sources'], {})
                    self.assertFalse(rows[person]['complete'])
                    self.assertIsNone(rows[person]['rate'])
                self.assertEqual(result['metrics'], preview['metrics'])
                self.assertGreater(result['metrics'], 0)
                metrics.record_shown(data, self.cfg, self.row, 'ana', [{'id': 'tmdb:1', 'type': 'movie'}], NOW)
                self.assertTrue(all(r['person'] == 'ben' for r in json.loads((path / 'metrics_shown.json').read_text())['shown']))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.cfg = members.fresh_config(['ana', 'ben'])
        self.cfg['metrics'] = {'enabled': True}
        self.row = members.rid('week', self.cfg)

    def shown(self, who='ana', at=NOW, key='tmdb:1', kind='movie', row=None):
        metrics.record_shown(self.data, self.cfg, row or self.row, who,
                             [{'id': key, 'type': kind, 'name': 'PRIVATE SYNTHETIC TITLE'}], at)

    def play(self, who='ana', at=NOW + DAY, key='tmdb:1', source='jellyfin', kind='movie'):
        return dict(person=who, at=at, key=key, kind=kind, source=source)

    def rows(self, now=NOW + DAY):
        return metrics.report(self.data, self.cfg, now)['rows']

    def test_window_edges_and_exposure_week(self):
        for offset, credited in ((-1, 0), (0, 1), (14*DAY, 1), (14*DAY+1, 0)):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as data:
                metrics.record_shown(data, self.cfg, self.row, 'ana', [{'id': 'tmdb:1', 'type': 'movie'}], NOW)
                metrics.update(data, self.cfg, [self.play(at=NOW + offset)], NOW + 15*DAY)
                row = metrics.report(data, self.cfg, NOW + 15*DAY)['rows'][0]
                self.assertEqual((row['week'], row['shown'], row['played'], row['rate']),
                                 ('2026-10-05', 1, credited, float(credited)))

    def test_together_person_and_kind_boundaries(self):
        for person in ('ana', 'ben', 'together'):
            self.shown(person)
        self.shown('ana', key='tmdb:2', kind='series')
        metrics.update(self.data, self.cfg, [self.play(), self.play(key='tmdb:2')], NOW + DAY)
        rows = {r['person']: r for r in self.rows()}
        self.assertEqual(rows['ana']['played'], 1)
        self.assertEqual(rows['ben']['played'], 0)
        self.assertEqual(rows['together']['played'], 1)
        self.assertEqual(rows['together']['sources'], {'jellyfin': 1})
        metrics.update(self.data, self.cfg, [self.play('together', key='tmdb:2', kind='tv')], NOW + DAY)
        self.assertEqual({r['person']: r for r in self.rows()}['ana']['played'], 1)

    def test_repeats_multiple_rows_sources_and_restart_are_unique(self):
        self.shown()
        self.shown(at=NOW + 2*DAY)
        second = members.rid('foryou', self.cfg)
        self.shown(row=second)
        plays = [self.play(), self.play(source='trakt_api')]
        for _ in range(2):
            metrics.update(self.data, self.cfg, plays, NOW + 3*DAY)
        for row in self.rows(NOW + 3*DAY):
            self.assertEqual((row['shown'], row['played'], row['rate']), (1, 1, 1.0))
            self.assertEqual(row['sources'], {'jellyfin': 1, 'trakt_api': 1})
        raw = (self.data/'metrics.json').read_text()
        self.assertEqual(json.loads(raw)['weeks']['2026-10-05'][self.row]['ana']['rate'], 1.0)
        self.assertNotIn('tmdb:', raw)
        self.assertNotIn('PRIVATE SYNTHETIC TITLE', raw)

    def test_retention_folds_counts_before_removing_raw_and_preserves_pins(self):
        self.shown()
        (self.data/'shown.json').write_text('synthetic serving pins')
        metrics.update(self.data, self.cfg, [self.play()], NOW + 31*DAY)
        state = json.loads((self.data/'metrics_shown.json').read_text())
        self.assertEqual(state['shown'], [])
        self.assertNotIn('tmdb:1', json.dumps(state))
        report = metrics.report(self.data, self.cfg, NOW + 31*DAY, weeks=6)
        self.assertEqual((report['rows'][0]['shown'], report['rows'][0]['played']), (1, 1))
        self.assertEqual((self.data/'shown.json').read_text(), 'synthetic serving pins')
        metrics.update(self.data, self.cfg, [self.play()], NOW + 32*DAY)
        self.assertEqual(metrics.report(self.data, self.cfg, NOW + 32*DAY, weeks=6)['rows'][0]['played'], 1)

    def test_max_items_marks_incomplete_instead_of_reporting_false_rate(self):
        self.cfg['metrics'] = {'enabled': True, 'shown_max_items': 2}
        for index in range(3):
            self.shown(key=f'tmdb:{index+1}', at=NOW+index)
        state = json.loads((self.data/'metrics_shown.json').read_text())
        self.assertLessEqual(sum(len(r['times']) for r in state['shown']), 2)
        row = self.rows()[0]
        self.assertFalse(row['complete'])
        self.assertIsNone(row['rate'])

    def test_off_deletes_and_does_not_create_extra_data(self):
        self.shown()
        self.cfg['metrics'] = {'enabled': False}
        metrics.update(self.data, self.cfg, [], NOW + DAY)
        self.shown()
        self.assertFalse((self.data/'metrics.json').exists())
        self.assertFalse((self.data/'metrics_shown.json').exists())
        self.assertFalse(metrics.report(self.data, self.cfg, NOW)['enabled'])

    def test_config_defaults_validation(self):
        self.assertEqual(metrics.settings(members.fresh_config(['ana', 'ben'])), dict(enabled=False, attribution_days=14,
                         shown_retention_days=30, shown_max_items=10000))
        for field, value in (('enabled', 'no'), ('shown_retention_days', 0),
                             ('shown_max_items', True), ('attribution_days', -1), ('unexpected', 1)):
            cfg = {**self.cfg, 'metrics': {field: value}}
            with self.subTest(field=field), self.assertRaises(members.ConfigError):
                members.validate_config(cfg)

    def test_metrics_preferences_reload_without_restart(self):
        from skald.setup import startup_digest
        before = startup_digest(self.cfg)
        self.cfg['metrics'] = {'enabled': False, 'shown_retention_days': 7}
        self.assertEqual(startup_digest(self.cfg), before)

    def test_collector_keeps_sources_and_rejects_ratings_and_undated_imports(self):
        self.cfg['history'] = {'source': 'jellyfin'}
        self.cfg['connectors'] = {source: {'enabled': True, 'accounts': {'ana': None}}
                                  for source in ('trakt_api', 'mdblist', 'stremio')}
        item = dict(id='tmdb:1', type='movie', played=True, at=(NOW+DAY)*1000)
        rating = dict(id='tmdb:2', type='movie', rating=10, at=(NOW+DAY)*1000)
        dropped = dict(id='tmdb:3', type='movie', played=True, at=None)
        imports = [dict(key='tmdb:4', kind='movie', who='ana', src='netflix', action='seen',
                        t='2026-10-06', conf=1)]
        server = SimpleNamespace(CONFIG=self.cfg, DATA=str(self.data), _idmap={},
            read_history=lambda: [item, rating], attribute=lambda item: ('ana', ['ana'], {}),
            _drop_read=lambda name: [item, rating, dropped], active_imports=lambda: imports)
        plays, sources = metrics.collect(server, NOW+DAY)
        self.assertEqual(sources, {'jellyfin', 'trakt_api', 'mdblist', 'stremio', 'import:netflix'})
        self.assertFalse(any(p['key'] == 'tmdb:2' for p in plays))
        self.assertIsNone(next(p for p in plays if p['source'] == 'import:netflix')['at'])
        self.shown()
        metrics.update(self.data, self.cfg, plays, NOW+DAY, sources=sources)
        report = metrics.report(self.data, self.cfg, NOW+DAY)
        self.assertEqual(report['rows'][0]['played'], 1)
        self.assertEqual(set(report['rows'][0]['sources']), {'jellyfin', 'trakt_api', 'mdblist', 'stremio'})
        self.assertEqual(report['sources']['stremio'], {'dated': 1, 'undated': 1})

    def test_short_retention_marks_unfinished_cohort_incomplete(self):
        self.cfg['metrics'] = {'enabled': True, 'shown_retention_days': 1}
        self.shown()
        metrics.update(self.data, self.cfg, [], NOW+2*DAY)
        self.assertFalse(self.rows(NOW+2*DAY)[0]['complete'])
        self.shown(at=NOW+2*DAY)
        self.assertEqual(self.rows(NOW+2*DAY)[0]['shown'], 1)

    def test_restored_aggregates_do_not_double_count_the_same_title(self):
        self.shown()
        metrics.update(self.data, self.cfg, [self.play()], NOW+DAY)
        (self.data/'metrics_shown.json').unlink()
        self.shown(at=NOW+2*DAY)
        row = self.rows(NOW+2*DAY)[0]
        self.assertEqual((row['shown'], row['played']), (1, 1))
        self.assertIsNone(row['rate'])

    def test_server_catalog_hook_and_privacy_reload(self):
        from decide_kit import load_server, close_server
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        server = load_server(self.data)
        self.addCleanup(close_server, server)
        server.refresh_preferences()
        handler = object.__new__(server.Handler)
        replies = []
        handler.send_json = lambda code, value, *args, **kwargs: replies.append(value)
        with patch.object(server, 'row_metas', return_value=[dict(id='tmdb:1', type='movie')]), \
                patch.object(server, 'feed_rows', return_value=[(self.row, ('movie',), 'Week')]), \
                patch.object(server, 'note_served'), patch.object(server, 'connectors_demand'), \
                patch('skald.metrics.time.time', return_value=NOW):
            handler.addon_get(['en-US', 'catalog', 'movie', self.row+'.json'], 'en-US', 'ana')
        self.assertEqual(self.rows(NOW)[0]['person'], 'ana')
        self.assertEqual(replies, [{'metas': [dict(id='tmdb:1', type='movie')]}])
        self.cfg['metrics'] = {'enabled': False}
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        server.refresh_preferences()
        self.assertFalse((self.data/'metrics.json').exists())
        self.assertFalse((self.data/'metrics_shown.json').exists())
        self.assertIn('row_metrics', [job[0] for job in server.DAILY_JOBS])

    def test_wrong_shaped_shown_state_is_rejected_as_invalid_metrics(self):
        for state in ({}, [], {'shown': [], 'folded': [], 'sources': {}, 'cleaned': None}):
            with self.subTest(state=state):
                (self.data/'metrics_shown.json').write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    self.shown()

    def test_metrics_failures_do_not_break_catalog_delivery(self):
        from decide_kit import load_server, close_server
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        server = load_server(self.data)
        self.addCleanup(close_server, server)
        handler = object.__new__(server.Handler)
        handler.send_json = Mock()
        metas = [dict(id='tmdb:1', type='movie')]
        with patch.object(server, 'row_metas', return_value=metas), \
                patch.object(server, 'feed_rows', return_value=[(self.row, ('movie',), 'Week')]), \
                patch.object(server, 'note_served'), patch.object(server, 'connectors_demand'), \
                patch.object(server, '_daily_log') as log, \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            for state in ({}, [], {'shown': [{}], 'folded': {}, 'sources': {}, 'cleaned': None}):
                with self.subTest(state=state):
                    (self.data/'metrics_shown.json').write_text(json.dumps(state))
                    handler.addon_get(['en-US', 'catalog', 'movie', self.row+'.json'], 'en-US', 'ana')
                    self.assertEqual(handler.send_json.call_args.args[:2], (200, {'metas': metas}))
            with patch('skald.metrics.record_shown', side_effect=RuntimeError('synthetic failure')):
                handler.addon_get(['en-US', 'catalog', 'movie', self.row+'.json'], 'en-US', 'ana')
            self.assertEqual(handler.send_json.call_count, 4)
            self.assertEqual(log.call_count, 4)
            self.assertTrue(all(call.args == ('row metrics exposure could not be saved',)
                                for call in log.call_args_list))

    def test_cli_local_and_snapshot_counts_only(self):
        self.shown()
        metrics.update(self.data, self.cfg, [self.play()], NOW + DAY)
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        for args in (['metrics'], ['--snapshot', str(self.data), 'metrics']):
            out = io.StringIO()
            with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True), \
                    patch('skald.metrics.time.time', return_value=NOW + DAY):
                code = main(args, out=out)
            self.assertEqual(code, 0, out.getvalue())
            text = out.getvalue()
            self.assertIn('Shown', text)
            self.assertIn('Played', text)
            self.assertIn('jellyfin', text)
            self.assertNotIn('PRIVATE SYNTHETIC TITLE', text)
            self.assertNotIn('tmdb:1', text)

    def test_offline_engine_preserves_complete_current_week_metrics(self):
        from skald.engine import Engine
        self.shown()
        metrics.update(self.data, self.cfg, [self.play()], NOW+DAY)
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        (self.data/'.skald-format').write_text(json.dumps({'format': migrate.FORMAT, 'release': members.VERSION}))
        (self.data/'profile.json').write_text(json.dumps({'entries': [], 'seen': [], 'negative': [], 'genres': {}}))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch('skald.metrics.time.time', return_value=NOW+DAY):
            direct = metrics.report(self.data, self.cfg)
            self.assertEqual((direct['rows'][0]['complete'], direct['rows'][0]['rate']), (True, 1.0))
            with Engine(self.data) as engine:
                self.assertEqual(engine.metrics(), direct)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_numbered_migration_adds_only_empty_aggregates(self):
        (self.data/'skald.json').write_text(json.dumps(self.cfg))
        migrate.ensure(self.data)
        self.assertEqual(migrate.check(self.data)['format'], migrate.FORMAT)
        self.assertEqual(json.loads((self.data/'metrics.json').read_text())['weeks'], {})
        before = (self.data/'metrics.json').read_bytes()
        migrate.ensure(self.data)
        self.assertEqual((self.data/'metrics.json').read_bytes(), before)
