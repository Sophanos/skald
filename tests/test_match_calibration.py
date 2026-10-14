"""Local match ranks and agent diagnostics, using made-up household data only."""
from array import array
from contextlib import redirect_stdout
import copy
import datetime
import io
import json
import random
from pathlib import Path
import statistics
import tempfile
import time
import unittest
from unittest.mock import patch

from decide_kit import write_legacy
from skald.engine import Engine
from skald.__main__ import main


def write_store(data, n=720, people=('ana', 'ben')):
    write_legacy(data)
    config = json.loads((data / 'legacy.json').read_text())
    config['household']['members'] = list(people)
    config['household']['labels'].update({p: p.title() for p in people})
    config['taste']['people'] = {p: {'type': '', 'loves': []} for p in people}
    (data / 'skald.json').write_text(json.dumps(config))
    rng = random.Random(108)
    facets, entries = {}, []
    genres = ('Drama', 'Comedy', 'Mystery', 'Adventure')
    for i in range(n):
        owner = i % len(people)
        group = owner if i < 60 * len(people) else rng.randrange(len(people) + 2)
        kind = 'movie' if i % 3 else 'tv'
        rec = {'kind': kind, 'title': f'Invented {i}', 'y': '2020',
               'g': [genres[group % 4]],
               'k': [[group * 8 + rng.randrange(8), f'Motif {group}'],
                     [80 + rng.randrange(20), 'Secondary motif']],
               'd': [[group * 4 + rng.randrange(4), f'Maker {group}']],
               'vote_average': 6 + rng.random() * 3, 'vote_count': 100 + rng.randrange(10000)}
        facets[f'tmdb:{i + 1}'] = rec
        if i < 60 * len(people):
            entries.append({'key': f'tmdb:{i + 1}', 'id': i + 1, 'kind': kind,
                'stype': 'movie' if kind == 'movie' else 'series', 'title': rec['title'], 'year': '2020',
                'weight': 3., 'signal': 'loved', 'age': 10., 'by': [people[owner]],
                'who': 'remote', 'flags': [], 'genres': rec['g'], 'item': {}})
    (data / 'profile.json').write_text(json.dumps({'entries': entries, 'seen': [e['key'] for e in entries]}))
    (data / 'taste.json').write_text('{"people": {}}')
    (data / 'facets.json').write_text(json.dumps({'items': facets}))


class MatchCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_store(self.data)
        self.network = self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        with redirect_stdout(io.StringIO()):
            self.engine = Engine(self.data)
        self.addCleanup(self.engine.close)
        self.s = self.engine._server

    def test_offline_and_fast_build_have_local_match_without_fetches(self):
        s, p = self.s, self.engine._profile
        for enrich in (True, False):
            with patch.object(s, 'tmdb', side_effect=AssertionError('local reference must not fetch')), \
                    patch.object(s, 'ensure_emb'), patch.object(s, 'ensure_facets'), \
                    patch.object(s, 'tag_experiences'), redirect_stdout(io.StringIO()):
                m = s.build_model(p, enrich=enrich)
            self.assertEqual(m['cal_reference'], 'known')
            self.assertEqual(m['cal'], 600)
            self.assertIsNotNone(s.match_pct(m, s.score_key(m, 'tmdb:121', kind='tv')[0]))
            self.assertEqual(self.engine.score('tmdb:121')['calibration'], 'known')

    def test_curated_fits_rank_below_better_known_titles_and_scope_differs(self):
        s, m = self.s, self.engine._model
        ranked = sorted((s.score_key(m, key, kind=rec['kind'])[0], key, rec['kind'])
                        for key, rec in s._facets.items() if key not in self.engine._profile['seen'])
        curated = ranked[240:360]
        self.assertTrue(all(score > 0 for score, _, _ in curated))
        local = [s.match_pct(m, score) for score, _, _ in curated]
        self.assertTrue(all(pct is not None and pct < 80 for pct in local))
        old = {**m, 'cal': 80, 'bg': [score for score, _, _ in ranked[:80]]}
        self.assertTrue(all(s.match_pct(old, score) >= 80 for score, _, _ in curated))
        self.assertNotEqual(m['pbg']['ana'], m['bg'])
        self.assertNotEqual(m['pbg']['ana'], m['pbg']['ben'])
        self.assertNotEqual(m['pbg']['together'], m['bg'])

    def test_exclusions_kind_aliases_and_sample_are_deterministic(self):
        s, p = self.s, copy.deepcopy(self.engine._profile)
        p['negative'].add('tmdb:121')
        s._facets['tmdb:122@tv'] = {**s._facets['tmdb:122'], 'kind': 'tv'}
        s._facets['tmdb:1@movie'] = {**s._facets['tmdb:1'], 'kind': 'movie'}
        calls = []
        original = s.score_key
        def score(m, key, person=None, kind=None):
            calls.append((key, kind))
            return original(m, key, person, kind)
        with patch.object(s, 'score_key', side_effect=score), redirect_stdout(io.StringIO()):
            first = s.build_model(p, enrich=False)
        self.assertNotIn(('tmdb:121', 'tv'), calls)
        self.assertNotIn(('tmdb:1', 'tv'), calls)
        self.assertNotIn(('tmdb:1', 'movie'), calls)
        self.assertIn(('tmdb:122', 'movie'), calls)
        self.assertIn(('tmdb:122', 'tv'), calls)
        self.assertEqual(first['cal'], 600)
        s._facets = dict(reversed(list(s._facets.items())))
        with redirect_stdout(io.StringIO()):
            second = s.build_model(p, enrich=False)
        self.assertEqual(first['bg'], second['bg'])
        self.assertEqual(first['pbg'], second['pbg'])

    def test_tiny_store_falls_back_to_fake_trending(self):
        s = self.s
        s._facets = dict(list(s._facets.items())[:180])
        def trending(path, **kwargs):
            self.assertTrue(path.startswith('trending/'))
            kind = path.split('/')[1]
            return {'results': [{'id': int(k[5:])} for k, rec in s._facets.items()
                                if rec['kind'] == kind and k not in self.engine._profile['seen']]}
        with patch.object(s, 'tmdb', side_effect=trending) as transport, redirect_stdout(io.StringIO()):
            m = s.build_model(self.engine._profile)
        self.assertEqual(m['cal_reference'], 'trending')
        self.assertEqual(m['cal'], 60)
        self.assertEqual(transport.call_count, 4)
        self.assertIsNotNone(s.match_pct(m, m['bg'][30]))

    def test_adult_records_cannot_supply_known_reference(self):
        s, p = self.s, self.engine._profile
        s._facets = {key: rec for key, rec in s._facets.items()
                     if key in p['seen'] or 121 <= int(key[5:]) <= 321}
        for key, rec in s._facets.items():
            if key not in p['seen']:
                rec.update(adult=True, cr={'US': 'NC-17'})
        with patch.object(s, 'score_key', wraps=s.score_key) as score, redirect_stdout(io.StringIO()):
            m = s.build_model(p, enrich=False)
        self.assertIsNone(m['cal_reference'])
        self.assertIsNone(m['cal'])
        score.assert_not_called()

    def test_child_reference_excludes_blocked_titles_before_sampling(self):
        s, p = self.s, self.engine._profile
        template = s._facets['tmdb:122']
        for i in range(721, 1621):
            s._facets[f'tmdb:{i}'] = {**template, 'title': f'Invented {i}', 'kind': 'movie'}
        eligible = set()
        for key, rec in s._facets.items():
            i = int(key[5:])
            if i % 3 == 0:
                rec['cr'] = {'US': 'PG' if rec['kind'] == 'movie' else 'TV-PG'}
                if key not in p['seen']:
                    eligible.add(key)
            elif i % 3 == 1:
                rec['cr'] = {'US': 'R' if rec['kind'] == 'movie' else 'TV-MA'}
        s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 10}
        calls = []
        original = s.person_score
        def score(m, key, person=None, kind=None):
            calls.append((key, person))
            return original(m, key, person, kind)
        with patch.object(s, 'person_score', side_effect=score), redirect_stdout(io.StringIO()):
            m = s.build_model(p, enrich=False)
        self.assertEqual(m['cal'], s.CALIBRATION_MAX)
        self.assertEqual(len(m['pbg']['ben']), s.CALIBRATION_MAX)
        self.assertEqual(len(m['pbg']['together']), s.CALIBRATION_MAX)
        self.assertEqual({key for key, person in calls if person == 'ana'}, eligible)
        self.assertEqual(len(m['pbg']['ana']), len(eligible))

    def test_worker_rebuilds_when_eligible_reference_crosses_threshold(self):
        s, p = self.s, self.engine._profile
        full = dict(s._facets)
        small = {key: rec for key, rec in full.items() if int(key[5:]) <= 319}
        s._facets = dict(small)
        s._model['sig'] = None
        with patch.object(s, '_defer_enrichment', True), redirect_stdout(io.StringIO()):
            first = s.get_model()
            self.assertIsNone(first['cal'])
            s._facets.update({key: {**rec, 'adult': True} for key, rec in full.items() if key not in small})
            with patch.object(s, 'build_model', side_effect=AssertionError('still below eligible threshold')), \
                    patch.object(s, 'score_key', side_effect=AssertionError('signature must not score')):
                self.assertIs(s.get_model(), first)
            s._facets = full
            with patch.object(s, 'build_model', side_effect=AssertionError('request must use cache')), \
                    patch.object(s, 'score_key', side_effect=AssertionError('request must not score')):
                self.assertIs(s.get_model(False), first)
            second = s.get_model()
            self.assertIsNot(second, first)
            self.assertEqual(second['cal_reference'], 'known')
            self.assertEqual(second['cal'], 600)
            s._facets['tmdb:721'] = {**full['tmdb:122'], 'title': 'Invented 720'}
            with patch.object(s, 'build_model', side_effect=AssertionError('threshold unchanged')), \
                    patch.object(s, 'score_key', side_effect=AssertionError('signature must not score')):
                self.assertIs(s.get_model(), second)

    def test_cli_reports_cached_bands_without_building_rows_or_writing_inputs(self):
        s, m = self.s, self.engine._model
        self.assertEqual(self.engine.calibration()['rows'], {})
        s._feeds[s.rid('foryou'), s.LANG] = {'metas': {'tv': [
            {'id': f'tmdb:{i}', 'type': 'movie' if i % 3 != 1 else 'series'} for i in (121, 122, 123)],
            'pool': [{'id': 'tmdb:124', 'type': 'series'}]}}
        s._feeds[s.rid('ana'), s.LANG] = {'metas': {'tv': [{'id': 'tmdb:121', 'type': 'series'}]}}
        wish = {'scope': 'ana', 'slot': 1, 'name': 'Invented shelf', 'text': 'A made-up wish'}
        (Path(s.DATA) / 'shelves.json').write_text(json.dumps({'wishes': [wish], 'plans': {}}))
        s._feeds[s.rid('shelf_ana_1'), s.LANG] = {'metas': {'tv': [{'id': 'tmdb:121', 'type': 'series'}]}}
        with patch.object(s, 'row_metas', side_effect=AssertionError('must only inspect cached rows')):
            report = self.engine.calibration('ana')
        self.assertEqual(report['reference'], {'kind': 'known', 'size': 600})
        self.assertEqual(set(report['scores']), {'p0', 'p10', 'p25', 'p50', 'p75', 'p90', 'p100'})
        self.assertEqual(sum(report['rows'][s.rid('foryou')]['bands'].values()) + report['rows'][s.rid('foryou')]['unknown'], 3)
        self.assertEqual(report['rows'][s.rid('ana')]['person'], 'ana')
        self.assertEqual(report['rows'][s.rid('shelf_ana_1')]['person'], 'ana')
        self.assertEqual(len(report['rows']), 3)
        self.assertIsNone(self.engine.calibration()['rows'][s.rid('foryou')]['person'])
        self.assertEqual(self.engine.calibration()['rows'][s.rid('shelf_ana_1')]['person'], 'ana')
        self.assertEqual(self.engine.calibration('together')['reference']['size'], 600)
        self.assertEqual(self.engine.calibration('household')['scores'], self.engine.calibration()['scores'])
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}), \
                patch('skald.live.pull', side_effect=AssertionError('no live pull')):
            for args in (['taste', 'calibration'], ['taste', 'calibration', '--person', 'ana', '--json'],
                         ['--snapshot', str(self.data), 'taste', 'calibration', '--person', 'together', '--json']):
                out = io.StringIO()
                self.assertEqual(main(args, out=out), 0, out.getvalue())
                if '--json' in args:
                    self.assertEqual(json.loads(out.getvalue())['reference']['kind'], 'known')
                else:
                    self.assertIn('known', out.getvalue())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})
        with self.assertRaises(ValueError):
            self.engine.calibration('stranger')

    def test_calibration_cli_never_migrates_source_or_pulls_live(self):
        from skald import migrate
        ensure = migrate.ensure
        def copied_only(path):
            self.assertNotEqual(Path(path).resolve(), self.data.resolve())
            return ensure(path)
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}), \
                patch.object(migrate, 'rewrite_needed', return_value=True), patch.object(migrate, 'ensure', side_effect=copied_only), \
                patch('skald.live.pull', side_effect=AssertionError('no live pull')):
            out = io.StringIO()
            for args in (['taste', 'calibration', '--json'], ['taste', '--json', 'calibration'],
                         ['--json', 'taste', 'calibration']):
                self.assertEqual(main(args, out=out), 0, out.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--live', 'taste', 'calibration', '--json'], out=out), 2)

    def test_doctor_does_not_claim_local_matches_are_unavailable(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as state, patch.dict('os.environ', {'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}):
            main(['--snapshot', str(self.data), '--state-dir', state, 'doctor', '--json'], out=out)
        findings = {f['checkId']: f for f in json.loads(out.getvalue())['findings']}
        self.assertEqual(findings['calibration.no_bg']['message'], 'known reference: 600 titles')

    def test_member_and_upcoming_gates_keep_curated_fits_with_local_reference(self):
        s, m = self.s, self.engine._model
        meta = {'id': 'tmdb:122', 'type': 'movie', 'name': 'Invented 121', 'description': ''}
        date = datetime.date(2026, 10, 15)
        for reference, expected in (('known', True), ('trending', False)):
            model = {**m, 'cal_reference': reference}
            with patch.object(s, 'get_model', return_value=model), \
                    patch.object(s, 'match_pct', return_value=68), \
                    patch.object(s, 'procedural', return_value=True), patch.object(s, 'is_hero', return_value=False), \
                    patch.object(s, 'with_art', return_value=([], {})), patch.object(s, 'genres', return_value={}), \
                    patch.object(s, 'rank_by_taste', return_value=[(.1, [], meta['id'], 'movie', (date, meta))]), \
                    patch.object(s, 'reasons', return_value=['A shared motif']):
                self.assertEqual(s.member_pick(meta, 'ana'), expected)
                self.assertEqual(bool(s.soon_for_taste(self.engine._profile, s.LANG, date, set())), expected)

    def test_threshold_pass_rates_on_fixture(self):
        self.assert_pass_rates(self.engine)

    def assert_pass_rates(self, engine):
        s, m = engine._server, engine._model
        for person in (None, *s.PEOPLE, 'together'):
            ranked = sorted(s.person_score(m, k, person, r['kind'])[0] for k, r in s._facets.items()
                            if k not in engine._profile['seen'])
            rng = random.Random(108)
            trending = sorted([rng.choice(ranked[:len(ranked)*2//5]) for _ in range(60)] +
                              [rng.choice(ranked) for _ in range(20)])
            old = {**m, 'cal': 80, 'bg': trending, 'pbg': {person: trending}}
            curated = ranked[len(ranked)*2//5:len(ranked)*9//10]
            for before, after, low, high in ((55, 55, .99, 1.), (65, 65, .90, 1.),
                                           (70, 65, .90, 1.), (80, 65, .90, 1.),
                                           (70, 70, .70, .85), (88, 88, .10, .25)):
                previous = sum(s.match_pct(old, v, person) >= before for v in curated)/len(curated)
                current = sum(s.match_pct(m, v, person) >= after for v in curated)/len(curated)
                self.assertEqual(previous, 1.)
                self.assertTrue(low <= current <= high, (person, before, after, current))

    def test_five_thousand_titles_four_people_build_in_two_seconds(self):
        write_store(self.data, 5000, ('ana', 'ben', 'clara', 'dino'))
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            s, times = engine._server, []
            self.assert_pass_rates(engine)
            for i, (key, rec) in enumerate(s._facets.items()):
                rec['k'] += [[1000+(i+j)%80, f'Invented motif {(i+j)%80}'] for j in range(6)]
                rec['a'] = [[(i+j)%200, f'Invented actor {(i+j)%200}'] for j in range(8)]
                rec['c'] = [[i%10, f'Invented studio {i%10}']]
                rec['t'] = ['invented setting '+str(i%12)]
                s._emb[key] = array('f', [1.] + [0.]*(s.EMB_DIM-1))
            for _ in range(3):
                start = time.perf_counter()
                m = s.build_model(engine._profile)
                times.append(time.perf_counter() - start)
            self.assertEqual(m['cal_reference'], 'known')
            self.assertEqual(m['cal'], 800)
            self.assertTrue(all(len(bg) == 800 for bg in m['pbg'].values()))
            self.assertLess(statistics.median(times), 2.)
            s._facets = dict(reversed(list(s._facets.items())))
            again = s.build_model(engine._profile)
            self.assertEqual(m['bg'], again['bg'])
            self.assertEqual(m['pbg'], again['pbg'])


if __name__ == '__main__':
    unittest.main()
