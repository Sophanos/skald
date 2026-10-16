"""Synthetic stop context and separation, without private history or network."""
from contextlib import redirect_stdout
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald.members import fresh_config
from skald.engine import Engine
from skald.__main__ import execute, parser
from eval import stop_report


def entry(i, person='eva', signal='loved', weight=3., **extra):
    return dict(key=f'tmdb:{i}', id=i, kind='movie', stype='movie', title=f'Fixture {i}',
                year='2020', signal=signal, weight=weight, age=10., who='remote',
                by=[person], flags=[], genres=[], item={}, **extra)


class StopReportTests(unittest.TestCase):
    def test_count_metrics_match_hand_computed_cases(self):
        row = stop_report.count_metrics([40, 70, 90, 99, None])
        self.assertEqual((row['n'], row['unscored'], row['coverage']), (5, 1, .8))
        self.assertEqual(row['median_index'], 80)
        self.assertEqual(row['histogram'], {'40': 1, '70': 1, '90': 1, '99': 1})
        self.assertEqual(row['ge90_share'], .5)
        self.assertEqual(row['ge90_bounds'], [.4, .6])
        self.assertAlmostEqual(row['wilson95'][0], .150038989, places=8)
        self.assertAlmostEqual(row['wilson95'][1], .849961011, places=8)
        self.assertEqual(stop_report.count_metrics([90, 40, 90])['median_index'], 90)
        self.assertIsNone(stop_report.count_metrics([None])['wilson95'])
        pairs = stop_report.pair_accuracy([1., 2., None], [0., 1., None])
        self.assertEqual(pairs, {'W': 3, 'T': 1, 'L': 0, 'unscored': 5, 'n': 9, 'accuracy': .875})
        self.assertEqual(stop_report.pair_accuracy([0., 1.], [1., 2.]),
                         {'W': 0, 'T': 1, 'L': 3, 'unscored': 0, 'n': 4, 'accuracy': .125})
        self.assertIsNone(stop_report.pair_accuracy([1.], [])['accuracy'])

    def test_positive_recovery_does_not_pass_negative_quality_gate(self):
        report = {'evidence': 'raw-evidence', 'separation': {
            'loved': stop_report.count_metrics([95] * 12),
            'stops_unsure_far': stop_report.count_metrics([70] * 12)},
            'cohorts': {'explicit_negative': stop_report.count_metrics([99] * 12)},
            'pairs': stop_report.pair_accuracy([.9] * 12, [1.] * 12)}
        checks = stop_report.targets(report)
        self.assertTrue(next(t['pass'] for t in checks if t['target'] == 'loved median >= 90'))
        self.assertFalse(all(t['pass'] for t in checks))
        self.assertFalse(next(t['pass'] for t in checks if t['target'] == 'explicit negative median <= 75'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        config = fresh_config(['eva', 'oskar'])
        config['region']['tz'] = 'Europe/Berlin'
        (self.data/'skald.json').write_text(json.dumps(config))
        at = datetime.datetime(2026, 1, 10, 23, tzinfo=datetime.timezone.utc).timestamp()*1000
        entries = [entry(i) for i in (*range(1, 8), *range(14, 27))] + [entry(8, 'oskar')]
        entries += [entry(9, signal='watched', weight=1.),
                    entry(10, signal='abandoned', weight=0., abandon='unsure'),
                    entry(11, signal='abandoned', weight=-.5, abandon='far'),
                    entry(12, signal='interest', weight=.5, abandon='related'),
                    entry(13, signal='disliked', weight=-2.)]
        entries[-4]['item'] = {'at': at, 'progress': .1}
        entries[-5]['item'] = {'at': at, 'progress': 1.}
        (self.data/'profile.json').write_text(json.dumps({'entries': entries}))
        facets = {f'tmdb:{i}': {'kind': 'movie', 'title': f'Fixture {i}',
                  'runtime': 120, 'k': [[1, 'Space']], 'g': ['Drama']} for i in range(1, 251)}
        facets['tmdb:10']['dr'] = '2026-02-01'
        (self.data/'facets.json').write_text(json.dumps({'items': facets}))
        library = {'library': {'tt0000010': {'item': {'id': 'tmdb:10', 'type': 'movie'},
            'state': {'_id': 'tt0000010', 'type': 'movie', 'state': {
                'duration': 120000, 'timeOffset': 60000, 'lastWatched': at}}}}}
        (self.data/'stremio-eva.history.json').write_text(json.dumps(library))

    def test_tables_context_unknowns_and_source_unchanged(self):
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        r = stop_report.stop_report(self.data, 'eva')
        self.assertEqual({k: v['n'] for k, v in r['separation'].items()},
                         {'loved': 20, 'stops_unsure_far': 2, 'stops_related': 1,
                          'watched': 1, 'dislikes': 1})
        self.assertEqual(set(r['separation']['loved']),
                         {'n', 'median_percentile', 'ge90', 'lt50', 'unknown', 'median_index',
                          'ge90_bounds', 'wilson95', 'histogram', 'coverage', 'unscored', 'ge90_share'})
        self.assertEqual(r['separation']['loved']['median_percentile'], 70)
        self.assertEqual(r['separation']['loved']['unknown'], 0)
        stops, finished = r['context']['stops'], r['context']['finished']
        self.assertEqual(stops['n'], 3)
        self.assertEqual(stops['runtime'], {'median': 120, 'unknown': 0})
        self.assertEqual(stops['stop_minute'], {'median': 1, 'unknown': 2})
        self.assertEqual(stops['day_part'], {'late night': 1, 'unknown': 2})
        self.assertEqual(stops['year_season'], {'winter': 1, 'unknown': 2})
        self.assertEqual(stops['fri_sun'], {'yes': 1, 'no': 0, 'unknown': 2})
        self.assertEqual(stops['short_stream'], {'yes': 1, 'no': 0, 'unknown': 2})
        self.assertEqual(stops['before_digital_release'], {'yes': 1, 'no': 0, 'unknown': 2})
        self.assertEqual(finished['n'], 21)
        self.assertEqual(finished['before_digital_release']['unknown'], 21)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})
        self.assertEqual(stop_report.stop_report(self.data, 'oskar')['context']['stops']['n'], 0)

    def test_json_text_and_check_exit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = stop_report.main(['--snapshot', str(self.data), '--person', 'eva', '--json', '--check-targets'])
        r = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(len(r['targets']), 7)
        self.assertTrue(all('pass' in target for target in r['targets']))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(stop_report.main(['--snapshot', str(self.data), '--person', 'eva']), 0)
        self.assertIn('unknown 2', out.getvalue())
        passing = {'evidence': 'raw-evidence', 'separation': {
            'loved': stop_report.count_metrics([92] * 22),
            'stops_unsure_far': stop_report.count_metrics([70] * 10 + [90] * 2)},
            'cohorts': {'explicit_negative': stop_report.count_metrics([40] * 12)},
            'pairs': stop_report.pair_accuracy([1.], [-1.])}
        with patch.object(stop_report, 'stop_report', return_value=passing), redirect_stdout(io.StringIO()):
            self.assertEqual(stop_report.main(['--snapshot', str(self.data), '--person', 'eva', '--json', '--check-targets']), 0)
        passing['separation']['stops_unsure_far']['ge90'] = 3
        self.assertFalse(all(t['pass'] for t in stop_report.targets(passing)))
        passing['separation']['stops_unsure_far'].update(n=0, median_percentile=None, ge90=0)
        self.assertFalse(all(t['pass'] for t in stop_report.targets(passing)))

    def test_missing_percentiles_are_unknown(self):
        (self.data/'facets.json').write_text('{"items": {}}')
        r = stop_report.stop_report(self.data, 'eva')
        self.assertEqual(r['separation']['loved']['unknown'], 20)
        self.assertIsNone(r['separation']['loved']['median_percentile'])
        self.assertEqual(r['context']['stops']['runtime']['unknown'], 3)

    def test_stremio_raw_id_and_timestamp_fallback_and_zero_offset(self):
        profile = json.loads((self.data/'profile.json').read_text())
        e = next(e for e in profile['entries'] if e['id'] == 10)
        e['item'] = {'id': 'tt0000010'}
        (self.data/'profile.json').write_text(json.dumps(profile))
        path = self.data/'stremio-eva.history.json'
        library = json.loads(path.read_text())
        row = library['library']['tt0000010']
        row['item']['id'] = 'tt0000010'
        row['state']['state']['timeOffset'] = 0
        path.write_text(json.dumps(library))
        stops = stop_report.stop_report(self.data, 'eva')['context']['stops']
        self.assertEqual(stops['stop_minute'], {'median': 0, 'unknown': 2})
        self.assertEqual(stops['day_part'], {'late night': 1, 'unknown': 2})

    def test_shared_title_keeps_each_members_own_stop_reading(self):
        path = self.data/'profile.json'
        profile = json.loads(path.read_text())
        shared = entry(27, signal='interest', weight=.5, abandon='related')
        shared.update(by=['eva', 'oskar'], opinions={
            'eva': {'signal': 'interest', 'weight': .5, 'abandon': 'related', 'item': {}},
            'oskar': {'signal': 'watched', 'weight': 2., 'item': {}}})
        profile['entries'].append(shared)
        path.write_text(json.dumps(profile))
        r = stop_report.stop_report(self.data, 'oskar')
        self.assertEqual(r['separation']['watched']['n'], 1)
        self.assertEqual(r['context']['stops']['n'], 0)
        self.assertEqual(r['context']['finished']['n'], 2)
        self.assertEqual(stop_report.stop_report(self.data, 'eva')['separation']['stops_related']['n'], 2)

    def test_imported_stops_read_once_in_engine_stream_and_server_profiles(self):
        at = datetime.datetime(2026, 10, 15, 12, tzinfo=datetime.timezone.utc).timestamp()
        records = [dict(t='2026-10-15', id=f'{i:032x}', key=f'tmdb:{i}', kind='movie',
                        title=f'Fixture {i}', year='2020', who='eva', action=action,
                        src='netflix' if action == 'abandon' else 'imdb',
                        conf=.4 if action == 'abandon' else 1., n=1)
                   for i, action in [(i, 'love') for i in range(1, 21)] +
                                    [(30, 'abandon'), (31, 'abandon'), (32, 'abandon'), (33, 'not_for_us')]]
        (self.data/'imports.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        cached = entry(99, signal='abandoned', weight=-.5, abandon='far')
        cached['who'] = 'owner'
        (self.data/'profile.json').write_text(json.dumps({'entries': [cached]}))
        path = self.data/'facets.json'
        facets = json.loads(path.read_text())
        for i in (1, 30):
            facets['items'][f'tmdb:{i}']['d'] = [[77, 'Fixture director']]
        for i in (32, 33, 34, 99):
            facets['items'][f'tmdb:{i}'].update(k=[[99, 'Different']], g=['Family'])
        path.write_text(json.dumps(facets))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        # Netflix progress: -1.5 base, .4 confidence, .8 trust, then a third for far.
        far_weight = -0.16000000000000003
        expected = {30: ('abandoned', 0., 'related'), 31: ('abandoned', 0., 'unsure'),
                    32: ('abandoned', far_weight, 'far'), 33: ('disliked', -3., None)}
        def readings(profile):
            return {e['id']: (e['signal'], e['weight'], e.get('abandon')) for e in profile['entries']}
        with patch('time.time', return_value=at), redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            s = engine._server
            for _ in range(2):
                with self.subTest(path='Engine refresh'):
                    self.assertEqual({i: readings(engine._profile)[i] for i in expected}, expected)
                    self.assertEqual(readings(engine._profile)[99], ('abandoned', -.5, 'far'))
                engine._refresh()
            # Reusing an already-read profile for a preview must not reduce a far stop again.
            self.assertEqual(readings(engine._profile_with(engine._profile)), readings(engine._profile))
            with self.subTest(path='server build after import'):
                built = s.build_profile([], [])
                self.assertEqual({i: readings(built)[i] for i in expected}, expected)
            with self.subTest(path='streamed import merge'):
                entries = []
                claims = s.merge_imports(entries, {}, at)
                s.merge_feedback(entries, {e['key']: e for e in entries}, at, records=claims)
                built = engine._profile_with({'entries': entries})
                self.assertEqual({i: readings(built)[i] for i in expected}, expected)
            with self.subTest(path='event replay model'):
                captured = []
                build = engine._build
                def capture(profile):
                    captured.append(profile)
                    return build(profile)
                with patch.object(engine, '_build', side_effect=capture):
                    engine._events_model([('import', list(s.import_events(at)))], at)
                self.assertEqual({i: readings(captured[0])[i] for i in expected}, expected)
            with self.subTest(path='import preview'):
                event = {'key': 'tmdb:34', 'kind': 'movie', 'title': 'Fixture 34', 'year': '2020',
                         'action': 'abandon', 'src': 'netflix', 'conf': .4, 'n': 1}
                with patch.object(engine, '_build', side_effect=capture), \
                        patch.object(engine, 'rows', return_value={'rows': []}):
                    engine.react([event], person='eva')
                self.assertEqual({i: readings(captured[-1])[i] for i in expected}, expected)
                self.assertEqual(readings(captured[-1])[34], ('abandoned', far_weight, 'far'))
                self.assertEqual(readings(captured[-1])[99], ('abandoned', -.5, 'far'))
            with self.subTest(path='CLI explain'):
                with patch('skald.__main__.PEOPLE', ['eva', 'oskar']):
                    args = parser().parse_args(['explain', 'tmdb:32', '--person', 'eva', '--json'])
                trace = execute(args, engine, None, None)
                self.assertEqual(trace['per_person']['eva']['abandon'], 'far')
                self.assertEqual(trace['score'], s.person_score(engine._model, 'tmdb:32', 'eva', 'movie')[0])
                self.assertEqual({i: readings(engine._profile)[i] for i in expected}, expected)
            report = stop_report.stop_report(self.data, 'eva')
            self.assertEqual(report['separation']['stops_unsure_far']['n'], 3)
            self.assertEqual(report['separation']['stops_related']['n'], 1)
            self.assertEqual(report['separation']['dislikes']['n'], 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_import_and_feedback_merges_preserve_shared_stop_reading(self):
        at = datetime.datetime(2026, 10, 15, 12, tzinfo=datetime.timezone.utc).timestamp()
        cached = entry(99, signal='abandoned', weight=-.5, abandon='far')
        cached.update(who='tv', by=['together'], item={'progress': .3})
        (self.data/'profile.json').write_text(json.dumps({'entries': [cached]}))
        common = {'id': 'f'*32, 'key': 'tmdb:99', 'kind': 'movie', 'title': 'Fixture 99',
                  'year': '2020', 'who': 'eva', 'action': 'love'}
        for source, extra in (('imports.jsonl', {'t': '2026-10-15', 'src': 'imdb', 'conf': 1., 'n': 1}),
                              ('feedback.jsonl', {'t': at, 'row': None, 'reasons': [], 'undo': None})):
            path = self.data/source
            path.write_text(json.dumps({**common, **extra})+'\n')
            with self.subTest(source=source), patch('time.time', return_value=at), \
                    redirect_stdout(io.StringIO()), Engine(self.data) as engine:
                own = engine.profile('oskar')['entries'][0]
                self.assertEqual((own['signal'], own['weight']), ('abandoned', -.175))
                shared = engine._profile['entries'][0]['shared']
                self.assertEqual(shared.get('abandon'), 'far')
                self.assertEqual(shared['weight'], -.5)
            path.unlink()

    def test_explicit_feedback_replaces_a_cached_solo_stop_reading(self):
        at = datetime.datetime(2026, 10, 15, 12, tzinfo=datetime.timezone.utc).timestamp()
        cached = entry(99, signal='abandoned', weight=-.5, abandon='far')
        cached.update(who='tv', item={'progress': .3})
        (self.data/'profile.json').write_text(json.dumps({'entries': [cached]}))
        for action, signal, weight in (('love', 'loved', 3.), ('not_for_us', 'disliked', -3.)):
            event = {'id': 'f'*32, 't': at, 'key': 'tmdb:99', 'kind': 'movie', 'title': 'Fixture 99',
                     'year': '2020', 'who': 'eva', 'action': action, 'row': None, 'reasons': [], 'undo': None}
            (self.data/'feedback.jsonl').write_text(json.dumps(event)+'\n')
            with self.subTest(action=action), patch('time.time', return_value=at), \
                    redirect_stdout(io.StringIO()), Engine(self.data) as engine:
                own = engine._profile['entries'][0]
                self.assertEqual((own['signal'], own['weight']), (signal, weight))
                self.assertNotIn('abandon', own)


if __name__ == '__main__':
    unittest.main()
