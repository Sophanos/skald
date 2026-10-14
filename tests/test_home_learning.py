"""Home learning from synthetic cohorts and local attribution."""
import copy
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from skald import members, metrics
from skald.__main__ import execute, main, parser

DAY = 86400
NOW = dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc).timestamp()


class HomeLearning(unittest.TestCase):
    def setUp(self):
        self.cfg = members.fresh_config(['ana', 'ben'])
        self.cfg['metrics'] = {'enabled': True}
        self.row = members.rid('foryou', self.cfg)
        self.other = members.rid('week', self.cfg)
        self.store = {'enabled': True, 'weeks': {}, 'extra': {'tz': 'UTC'}}

    def cohort(self, row=None, *, weeks=4, shown=400, played=40, person='ana', **flags):
        week = dt.datetime.fromtimestamp(NOW - weeks * 7 * DAY, dt.timezone.utc).date().isoformat()
        self.store['weeks'].setdefault(week, {}).setdefault(row or self.row, {})[person] = {
            'shown': shown, 'played': played, 'sources': {}, 'complete': True, 'closed': False,
            'extra': {'plays': [f'{week}:{row or self.row}:{person}:{i}' for i in range(played)]}, **flags}

    def details(self, **kwargs):
        return metrics.row_learning_details(self.store, scope='ana', now=NOW, **kwargs)

    def supported(self, row=None, **kwargs):
        for week in (4, 5):
            self.cohort(row, weeks=week, **kwargs)

    def test_posterior_baseline_and_shrinkage(self):
        self.supported()
        self.supported(self.other, shown=800, played=20)
        rows = self.details()
        s = sum(r['S'] for r in rows.values())
        p0 = (sum(r['P'] for r in rows.values()) + 1) / (s + 1000)
        for row in rows.values():
            self.assertAlmostEqual(row['p0'], p0)
            self.assertAlmostEqual(row['p'], (row['P'] + 1000 * p0) / (row['S'] + 1000))
            expected = .05 * max(-1, min(1, math.log2(row['p'] / p0)))
            self.assertAlmostEqual(row['L'], expected)
            self.assertEqual(row['support'], 2)
            self.assertEqual(row['neutral'], '')
        self.assertEqual(metrics.row_learning(self.store, scope='ana', now=NOW),
                         {row: r['L'] for row, r in rows.items()})

    def test_one_lucky_play_cannot_dominate_or_move_learning_over_one_cent(self):
        self.supported(shown=400, played=0)
        self.supported(self.other, shown=100000, played=0)
        before = self.details()[self.row]['L']
        self.cohort(weeks=4, shown=400, played=1, sources={'history': 1, 'trakt': 1})
        row = self.details()[self.row]
        self.assertLessEqual(abs(row['L'] - before), .01)
        self.assertLessEqual(row['L'], .01)
        self.assertAlmostEqual(row['P'], 2 ** (-21 / 28))

    def test_support_neutral_until_two_weeks_and_200_effective_showings(self):
        self.cohort(shown=1000)
        self.assertEqual(self.details()[self.row]['neutral'], 'fewer than two mature weeks')
        self.cohort(weeks=5, shown=1)
        self.assertNotEqual(self.details()[self.row]['L'], 0)
        self.store['weeks'] = {}
        self.supported(shown=100)
        row = self.details()[self.row]
        self.assertEqual((row['L'], row['neutral']), (0, 'fewer than 200 effective showings'))

    def test_immature_incomplete_forgotten_and_restored_are_ignored(self):
        self.supported()
        expected = self.details()
        for week in (0, 1, 2, -1, 13):
            self.cohort(weeks=week, shown=100000, played=100000)
        self.cohort(weeks=6, shown=100000, complete=False)
        self.cohort(weeks=7, shown=100000, played=0, complete=False, closed=True)
        self.cohort(weeks=8, shown=100000, played=100000, extra={'restored': True})
        self.assertEqual(self.details(), expected)
        self.store['extra']['restored'] = True
        self.assertEqual(self.details(), {})

    def test_maturity_boundary_and_existing_attribution_setting(self):
        self.cohort(weeks=3)
        self.assertEqual(self.details(), {})
        self.assertEqual(self.details(attribution_days=7)[self.row]['support'], 1)
        self.store['extra']['attribution_days'] = 7
        self.assertEqual(self.details()[self.row]['support'], 1)
        self.store['extra']['tz'] = 'America/New_York'
        self.assertEqual(metrics.row_learning_details(self.store, scope='ana', now=NOW + 4 * 3600,
                                                      attribution_days=14), {})
        self.assertIn(self.row, metrics.row_learning_details(self.store, scope='ana', now=NOW + 4 * 3600 + 1,
                                                             attribution_days=14))

    def test_decay_halves_after_28_days_and_last_12_weeks_only(self):
        self.cohort(weeks=4)
        first = self.details()[self.row]
        later = metrics.row_learning_details(self.store, scope='ana', now=NOW + 28 * DAY)[self.row]
        self.assertAlmostEqual(later['S'], first['S'] / 2)
        self.assertAlmostEqual(later['P'], first['P'] / 2)
        self.assertAlmostEqual(first['S'], 400 * 2 ** (-21 / 28))
        self.assertEqual(metrics.row_learning(self.store, scope='ana', now=NOW + 12 * 7 * DAY), {})

    def test_future_cohorts_do_not_leak(self):
        self.supported()
        expected = self.details()
        self.cohort(weeks=-3, shown=100000, played=100000)
        self.assertEqual(self.details(), expected)

    def test_rotating_slots_pool_by_family_including_empty_current_slots(self):
        mood1, mood2 = (members.rid(f'mood{i}', self.cfg) for i in (1, 2))
        lane1, lane2 = (members.rid(f'lane{i}', self.cfg) for i in (1, 2))
        self.cohort(mood1, weeks=4, played=0)
        self.cohort(mood2, weeks=5, played=80)
        self.supported(lane1, played=10)
        families = {mood1: 'mood', mood2: 'mood', lane1: 'lane', lane2: 'lane'}
        rows = self.details(families=families)
        self.assertEqual(rows[mood1], rows[mood2])
        self.assertEqual(rows[lane1], rows[lane2])
        self.assertEqual(rows[mood1]['support'], 2)
        self.assertAlmostEqual(rows[mood1]['p0'],
                               (rows[mood1]['P'] + rows[lane1]['P'] + 1) /
                               (rows[mood1]['S'] + rows[lane1]['S'] + 1000))
        self.assertNotEqual(rows[mood1]['L'], rows[lane1]['L'])

    def test_one_play_credited_to_multiple_rotating_slots_has_one_cent_limit(self):
        families = {members.rid(f'mood{i}', self.cfg): 'mood' for i in range(1, 5)}
        for row in families:
            self.supported(row, shown=200, played=0)
        self.supported(self.other, shown=1000000, played=0)
        before = self.details(families=families)
        for row in families:
            self.cohort(row, weeks=4, shown=200, played=1, extra={'plays': ['shared-play']})
        after = self.details(families=families)
        for row in families:
            self.assertLessEqual(abs(after[row]['L'] - before[row]['L']), .01)

    def test_restore_then_resume_keeps_restored_cohorts_out_of_learning(self):
        self.supported(shown=400, played=400)
        with tempfile.TemporaryDirectory() as data:
            path = Path(data)
            (path / 'metrics.json').write_text(json.dumps(
                {'v': 1, 'weeks': self.store['weeks'], 'sources': {}}))
            metrics.record_shown(path, self.cfg, self.row, 'ana',
                                 [{'id': 'tmdb:1', 'type': 'movie'}], NOW)
            metrics.update(path, self.cfg, [], NOW)
            self.assertTrue((path / 'metrics_shown.json').exists())
            self.assertEqual(metrics.learning_report(path, self.cfg, NOW, scope='ana')['rows'], {})
            saved = json.loads((path / 'metrics.json').read_text())
            self.assertEqual(saved['weeks']['2026-09-14'][self.row]['ana']['played'], 400)

    def test_one_play_across_weeks_and_people_has_one_cent_limit_after_retention(self):
        metas = [{'id': f'tmdb:{i}', 'type': 'movie'} for i in range(1, 401)]
        at = dt.datetime(2026, 9, 21, tzinfo=dt.timezone.utc).timestamp()
        play = dict(key='tmdb:1', kind='movie', person='ana', source='history', at=at)
        for people, scope in ((('ana',), 'ana'), (('ana', 'together'), 'household')):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as data:
                for day in (13, 20):
                    shown = dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc).timestamp()
                    for person in people:
                        metrics.record_shown(data, self.cfg, self.row, person, metas, shown)
                before = metrics.learning_report(data, self.cfg, NOW, scope=scope)['rows'][self.row]
                for source in ('history', 'trakt'):
                    metrics.update(data, self.cfg, [{**play, 'source': source}], NOW)
                after = metrics.learning_report(data, self.cfg, NOW, scope=scope)['rows'][self.row]
                self.assertLessEqual(after['L'] - before['L'], .01)
                self.assertAlmostEqual(after['P'], (2 ** (-28 / 28) + 2 ** (-21 / 28)) / 2)
                self.assertEqual(after['support'], 2)
                metrics.update(data, self.cfg, [], NOW + 10 * DAY)
                raw = json.loads((Path(data) / 'metrics_shown.json').read_text())
                self.assertEqual(raw['shown'], [])
                self.assertTrue(all(c['closed'] and c['complete'] for rows in raw['folded'].values()
                                    for people in rows.values() for c in people.values()))
                retained = metrics.learning_report(data, self.cfg, NOW + 10 * DAY, scope=scope)['rows'][self.row]
                self.assertAlmostEqual(retained['P'], after['P'] * 2 ** (-10 / 28))

    def test_unused_family_slots_leave_existing_learning_unchanged(self):
        mood = members.rid('mood1', self.cfg)
        self.supported(mood, played=1)
        before = self.details(families={mood: 'mood'})[mood]
        families = {members.rid(f'mood{i}', self.cfg): 'mood' for i in range(1, 5)}
        after = self.details(families=families)
        for row in families:
            self.assertEqual(after[row], before)

    def test_legacy_play_counts_load_without_inventing_unique_credits(self):
        self.supported()
        for rows in self.store['weeks'].values():
            rows[self.row]['ana'].pop('extra')
        with tempfile.TemporaryDirectory() as data:
            path = Path(data)
            (path / 'metrics_shown.json').write_text(json.dumps(
                {'v': 1, 'shown': [], 'folded': self.store['weeks'], 'sources': {}, 'cleaned': None}))
            self.assertEqual(len(metrics.report(path, self.cfg, NOW, weeks=6)['rows']), 2)
            self.assertEqual(metrics.learning_report(path, self.cfg, NOW, scope='ana')['rows'], {})

    def test_learning_credits_are_capped_per_cohort_and_cleared_on_forget(self):
        metas = [{'id': f'tmdb:{i}', 'type': 'movie'} for i in range(1, 4)]
        shown = NOW - 28 * DAY
        plays = [dict(key=m['id'], kind='movie', person='ana', source='history', at=shown + DAY)
                 for m in metas]
        with tempfile.TemporaryDirectory() as data, patch.object(metrics, 'MAX_PLAY_CREDITS', 2, create=True):
            metrics.record_shown(data, self.cfg, self.row, 'ana', metas, shown)
            metrics.update(data, self.cfg, plays, NOW + 10 * DAY)
            cell = json.loads((Path(data) / 'metrics.json').read_text())['weeks']['2026-09-14'][self.row]['ana']
            self.assertEqual(cell['played'], 3)
            self.assertEqual(len(cell.get('extra', {}).get('plays', [])), 2)
            self.assertEqual(metrics.learning_report(data, self.cfg, NOW + 10 * DAY, scope='ana')['rows'], {})
            metrics.forget(data, self.cfg, '2026-10-01', 'ana', apply=True)
            cell = json.loads((Path(data) / 'metrics.json').read_text())['weeks']['2026-09-14'][self.row]['ana']
            self.assertEqual(cell['extra']['plays'], [])

    def test_play_identity_ignores_source_and_numeric_timestamp_representation(self):
        credits = []
        shown = NOW - 28 * DAY
        for source, at in (('history', shown + DAY), ('trakt', int(shown + DAY))):
            with tempfile.TemporaryDirectory() as data:
                metrics.record_shown(data, self.cfg, self.row, 'ana',
                                     [{'id': 'tmdb:1', 'type': 'movie'}], shown)
                metrics.update(data, self.cfg, [dict(key='tmdb:1', kind='movie', person='ana',
                                                   source=source, at=at)], NOW)
                cell = json.loads((Path(data) / 'metrics.json').read_text())['weeks']['2026-09-14'][self.row]['ana']
                credits.append(cell['extra']['plays'])
        self.assertEqual(credits[0], credits[1])

    def test_utility_chart_and_shelf_excluded_from_baseline_and_learning(self):
        self.supported()
        expected = self.details()
        families = {}
        for family in ('utility', 'chart', 'shelf'):
            row = members.rid(family, self.cfg)
            families[row] = family
            self.supported(row, shown=100000, played=100000)
        self.assertEqual(self.details(families=families), expected)

    def test_scopes_and_metrics_off_missing(self):
        self.supported()
        self.supported(person='ben', played=0)
        self.supported(person='together', played=5)
        ana = self.details()[self.row]
        together = metrics.row_learning_details(self.store, scope='together', now=NOW)[self.row]
        household = metrics.row_learning_details(self.store, scope='household', now=NOW)[self.row]
        self.assertAlmostEqual(household['S'], ana['S'] * 3)
        self.assertLess(together['P'], ana['P'])
        self.assertEqual(metrics.row_learning(self.store, scope='kid:ana', now=NOW), {})
        self.assertEqual(metrics.row_learning(self.store, scope='guest', now=NOW), {})
        for store in (None, {}, {**self.store, 'enabled': False}):
            self.assertEqual(metrics.row_learning(store, scope='ana', now=NOW), {})

    def test_bounds_determinism_and_no_mutation(self):
        self.supported(shown=100000, played=100000)
        self.supported(self.other, shown=200000, played=0, closed=True)
        before = copy.deepcopy(self.store)
        first = self.details()
        self.assertEqual(first, self.details())
        reversed_store = {**self.store, 'weeks': dict(reversed(list(self.store['weeks'].items())))}
        self.assertEqual(first, metrics.row_learning_details(reversed_store, scope='ana', now=NOW))
        self.assertEqual(self.store, before)
        self.assertEqual(first[self.row]['L'], .05)
        self.assertEqual(first[self.other]['L'], -.05)

    def test_cli_learning_is_read_only_json_text_and_restores(self):
        self.supported()
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as state:
            path = Path(data)
            (path / 'skald.json').write_text(json.dumps(self.cfg))
            saved = {'v': 1, 'weeks': self.store['weeks'], 'sources': {}}
            (path / 'metrics.json').write_text(json.dumps(saved))
            raw = {'v': 1, 'shown': [], 'folded': saved['weeks'], 'sources': {}, 'cleaned': None}
            (path / 'metrics_shown.json').write_text(json.dumps(raw))
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            with patch('time.time', return_value=NOW):
                args = ['--snapshot', data, '--state-dir', state, 'metrics', 'learning', '--person', 'ana']
                out = io.StringIO()
                self.assertEqual(main(args + ['--json'], out=out), 0)
                result = json.loads(out.getvalue())
                self.assertEqual(result['rows'][self.row], self.details()[self.row])
                out = io.StringIO()
                self.assertEqual(main(args, out=out), 0)
                for word in ('S', 'P', 'p0', 'support', 'L', self.row):
                    self.assertIn(word, out.getvalue())
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})
                (path / 'metrics_shown.json').unlink()
                out = io.StringIO()
                self.assertEqual(main(args + ['--json'], out=out), 0)
                self.assertEqual(json.loads(out.getvalue())['rows'], {})
                self.assertIn('restored', out.getvalue())

    def test_cli_disabled_does_not_delete_metrics(self):
        self.cfg['metrics']['enabled'] = False
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'DATA_DIR': data}):
            path = Path(data)
            (path / 'skald.json').write_text(json.dumps(self.cfg))
            (path / 'metrics.json').write_text(json.dumps(metrics.empty()))
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            out = io.StringIO()
            self.assertEqual(main(['metrics', 'learning', '--json'], out=out), 0)
            self.assertFalse(json.loads(out.getvalue())['enabled'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_cli_flags(self):
        args = parser().parse_args(['--json', 'metrics', 'learning', '--person', 'ana'])
        self.assertEqual(args.metrics_action, 'learning')
        self.assertEqual(args.person, 'ana')
        self.assertTrue(args.json)

    def test_engine_dispatch_uses_learning_report(self):
        args = parser().parse_args(['metrics', 'learning', '--person', 'ana'])
        engine = SimpleNamespace(_work=Path('/synthetic'), _server=SimpleNamespace(CONFIG=self.cfg))
        with patch.object(metrics, 'learning_report', return_value={'rows': {}}) as report:
            self.assertEqual(execute(args, engine, None, None), {'rows': {}})
            report.assert_called_once_with(engine._work, self.cfg, scope='ana')


if __name__ == '__main__':
    unittest.main()
