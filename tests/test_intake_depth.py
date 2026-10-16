"""Synthetic intake signals, merge guards and previews; no external services."""
from decide_kit import write_legacy
from contextlib import redirect_stdout
from dataclasses import replace
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from skald.__main__ import main, text_output
from skald.engine import Engine
from skald.ingest import Candidate, EXPLICIT, SIGNALS, combine, parse, rating_signal
from skald.ingest.resolve import event
from skald.intake import cross_source, learned, preview

FIXTURES = Path(__file__).parent / 'fixtures/ingest'


def imported(action='seen', src='netflix', key='tmdb:87000001', **kw):
    return {'key': key, 'title': 'Synthetic Beacon', 'year': '2021', 'kind': 'movie',
            'who': 'ana', 'action': action, 'src': src, 'conf': 1., 'n': 1, 't': '2026-10-11', **kw}


def specs():
    return [('imdb', FIXTURES / 'imdb-ratings-2026.csv'),
            ('letterboxd', FIXTURES / 'letterboxd-rewatch'),
            ('netflix', FIXTURES / 'ViewingActivity-duration.csv'),
            ('trakt', FIXTURES / 'trakt-export')]


def fixture_metadata():
    records = {}
    for source, path in specs():
        for c in parse(source, path, profile_map={'Export B': 'ana'}):
            key = (c.title, c.year)
            if key not in records:
                tid = c.ids.get('tmdb') or int(c.ids['imdb'][2:]) if c.ids else 87001000 + len(records)
                records[key] = {'id': tid, 'title': c.title, 'year': c.year, 'kind': c.kind or 'movie',
                                'imdb': c.ids.get('imdb')}
            else:
                records[key]['imdb'] = c.ids.get('imdb') or records[key].get('imdb')
    return list(records.values())


def metadata_provider(records):
    def tmdb(path, **kw):
        def shape(r):
            return {**r, 'media_type': r['kind'], 'name': r['title'], 'release_date': r['year']+'-01-01' if r['year'] else '',
                    'first_air_date': r['year']+'-01-01' if r['year'] else ''}
        if path.startswith('find/'):
            return {field: [shape(r) for r in records if r.get('imdb') == path[5:] and r['kind'] == kind]
                    for field, kind in [('movie_results', 'movie'), ('tv_results', 'tv')]}
        if path.startswith('search/'):
            return {'results': [shape(r) for r in records if r['title'] == kw['query']]}
        kind, _, tid = path.partition('/')
        for r in records:
            if str(r['id']) == tid and r['kind'] == kind:
                return shape(r)
        raise LookupError('synthetic metadata unavailable')
    return tmdb


class DepthParserTests(unittest.TestCase):
    def setUp(self):
        from skald import members
        members.configure(readonly=True)

    def test_every_half_step_has_one_band(self):
        for scale in (5, 10, 100):
            for half in range(scale * 2 + 1):
                value = half / 2
                scaled = value * 10 / scale
                bands = [s for s, (low, high) in SIGNALS.items() if low <= scaled < high]
                self.assertEqual(len(bands), 1)
                self.assertEqual(rating_signal(value, scale=scale, default='interest'), bands[0])
        for scale, value in ((10, 5), (5, 2.5)):
            self.assertEqual(rating_signal(value, scale=scale), 'meh')

    def test_combine_keeps_conf_of_winner(self):
        c = Candidate('Synthetic', signal='like', conf=.7)
        self.assertEqual(combine([c, replace(c, signal='seen', conf=1.)])[0].conf, .7)
        self.assertEqual(combine([c, replace(c, conf=.8)])[0].conf, .8)

    def test_imdb_current_types_and_skips(self):
        stats = {}
        rows = parse('imdb', FIXTURES / 'imdb-ratings-2026.csv', stats=stats)
        self.assertEqual(stats, {'skipped_type': 4})
        self.assertEqual(len(rows), 9)
        self.assertEqual([c.kind for c in rows[:4]], ['movie', 'tv', 'tv', 'tv'])
        self.assertTrue(all(c.kind == 'movie' for c in rows[4:]))
        self.assertEqual((rows[1].signal, rows[-1].signal), ('meh', 'interest'))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'types.csv'
            p.write_text('Title,Title Type\nSynthetic A,tv_series\nSynthetic B,TV-mini-series\nSynthetic C,tv episode\n')
            self.assertTrue(all(c.kind == 'tv' for c in parse('imdb', p)))

    def test_letterboxd_distinct_dates_rating_and_likes(self):
        rows = {c.title: c for c in parse('letterboxd', FIXTURES / 'letterboxd-rewatch')}
        self.assertEqual((rows['Synthetic Rewind'].n, rows['Synthetic Rewind'].signal, rows['Synthetic Rewind'].conf), (2, 'like', .7))
        self.assertEqual((rows['Synthetic Beacon'].n, rows['Synthetic Beacon'].signal, rows['Synthetic Beacon'].conf), (1, 'love', 1.))
        self.assertEqual(rows['Synthetic Dull'].signal, 'meh')
        self.assertEqual(rows['Synthetic Favorite'].signal, 'love')

    def test_diary_does_not_redate_an_old_rating(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'ratings.csv').write_text('Date,Name,Year,Rating\n2026-01-01,Synthetic,2020,2\n')
            (p / 'diary.csv').write_text('Date,Name,Year,Watched Date\n2026-10-11,Synthetic,2020,2026-10-11\n')
            c = parse('letterboxd', p)[0]
            self.assertEqual((c.signal, c.last_ts), ('not_for_us', '2026-01-01'))

    def test_netflix_duration_rules_and_thresholds(self):
        stats = {}
        rows = {c.title: c for c in parse('netflix', FIXTURES / 'ViewingActivity-duration.csv',
                                         profile_map={'Export B': 'ana'}, stats=stats)}
        self.assertEqual(stats, {'skipped_short': 2})
        self.assertNotIn('Synthetic Flash', rows)
        self.assertNotIn('Synthetic Auto', rows)
        for title in ('Synthetic Boundary', 'Synthetic Auto Kept', 'Synthetic Half'):
            self.assertEqual((rows[title].kind, rows[title].signal, rows[title].conf), ('movie', 'abandon', .4))
        self.assertEqual(rows['Synthetic Whole'].signal, 'seen')
        self.assertEqual((rows['Synthetic Lost'].signal, rows['Synthetic Lost'].conf), ('abandon', .5))
        self.assertEqual(rows['Synthetic Lost'].n, 3)
        for title in ('Synthetic Active', 'Synthetic Three'):
            self.assertEqual(rows[title].signal, 'seen')
        self.assertEqual(rows['Synthetic Trailer'].signal, 'interest')
        self.assertTrue(all(c.person == 'ana' for c in rows.values()))

    def test_netflix_no_duration_identity(self):
        rows = parse('netflix', FIXTURES / 'ViewingActivity.csv', profile_map={'Export A': 'ana'})
        expected = [Candidate('Castle', kind='tv', n=3, last_ts='2026-10-11', src='netflix', person='ana'),
                    Candidate('Arrival', signal='interest', last_ts='2026-10-10', src='netflix', person='ana')]
        self.assertEqual(rows, expected)
        self.assertTrue(all(c.conf == 1. for c in parse('netflix', FIXTURES / 'NetflixViewingHistory.csv')))

    def test_netflix_missing_or_invalid_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'duration.csv'
            p.write_text('Title,Date,Duration\nSynthetic Blank,2026-10-11,\n')
            self.assertEqual(parse('netflix', p)[0].signal, 'seen')
            p.write_text('Title,Date,Duration\nSynthetic Bad,2026-10-11,00:99:00\n')
            with self.assertRaises(ValueError):
                parse('netflix', p)

    def test_event_multiplies_conf_and_checks_kind(self):
        c = Candidate('Synthetic', signal='abandon', conf=.4, src='netflix')
        r = {'match': {'key': 'tmdb:87000001', 'title': 'Synthetic', 'kind': 'movie'}, 'conf': .923}
        self.assertEqual((event(c, r, 'ana')['action'], event(c, r, 'ana')['conf']), ('abandon', .369))
        r['match']['kind'] = 'tv'
        self.assertEqual((event(c, r, 'ana')['action'], event(c, r, 'ana')['conf']), ('seen', .369))
        self.assertEqual(event(replace(c, kind='tv'), r, 'ana')['action'], 'abandon')

    def test_trakt_folder_shapes_and_signals(self):
        rows = {c.ids['tmdb'] - 87000100: c for c in parse('trakt', FIXTURES / 'trakt-export')}
        self.assertEqual(len(rows), 13)
        for n, signal, conf in [(1, 'meh', 1.), (2, 'love', 1.), (3, 'meh', .7), (4, 'seen', 1.),
                                (5, 'interest', 1.), (6, 'like', .7), (7, 'like', .7), (8, 'abandon', .5),
                                (9, 'abandon', .4), (10, 'seen', 1.), (11, 'not_for_us', 1.), (12, 'not_for_us', 1.), (13, 'not_for_us', 1.)]:
            self.assertEqual((rows[n].signal, rows[n].conf), (signal, conf), n)
        self.assertEqual(rows[8].n, 3)
        self.assertEqual(rows[6].n, 2)

    def test_trakt_single_file_shape_beats_filename_and_wallclock(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'watchlist.json'
            rows = [{'show': {'title': 'Synthetic', 'ids': {'tmdb': 1}}, 'watched_at': '2026-07-01'},
                    {'movie': {'title': 'Synthetic New'}, 'rating': 5, 'rated_at': '2026-10-11'}]
            p.write_text(json.dumps(rows))
            with patch('time.time', side_effect=AssertionError('wall clock forbidden')):
                out = parse('trakt', p)
            self.assertEqual([(c.signal, c.conf) for c in out], [('abandon', .4), ('meh', 1.)])

    def test_chrome_days_conf_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'History'
            epoch = dt.datetime(1601, 1, 1)
            stamp = lambda date: int((dt.datetime.fromisoformat(date) - epoch).total_seconds() * 1000000)
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE urls(id INTEGER, url TEXT, title TEXT, visit_count INTEGER, last_visit_time INTEGER)')
                db.execute('CREATE TABLE visits(url INTEGER, visit_time INTEGER)')
                for n in range(25):
                    db.execute('INSERT INTO urls VALUES(?,?,?,?,?)', (n, f'https://imdb.com/title/tt{n+1}/', f'Synthetic {n}', 3, stamp('2026-10-11')))
                    db.execute('INSERT INTO visits VALUES(?,?)', (n, stamp('2026-10-11')))
                    if n:
                        db.execute('INSERT INTO visits VALUES(?,?)', (n, stamp('2026-10-10')))
                db.execute('INSERT INTO urls VALUES(?,?,?,?,?)', (26, 'https://imdb.com/title/tt27/', 'Synthetic Old', 3, stamp('2026-06-13')))
            before = p.read_bytes()
            rows = parse('chrome', p)
            self.assertEqual(p.read_bytes(), before)
            self.assertEqual(len(rows), 20)
            self.assertEqual(rows[0].conf, .3)
            self.assertTrue(all(c.conf == .6 for c in rows[1:]))
            with sqlite3.connect(p) as db:
                db.execute('DROP TABLE visits')
            self.assertTrue(all(c.conf == .6 for c in parse('chrome', p)))

    def test_youtube_conf_cap_and_export_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'history.json'
            rows = [{'title': f'Watched Synthetic {n} Trailer', 'time': f'2026-10-{n+1:02}'} for n in range(25)]
            rows += [{'title': 'Watched Synthetic Old Trailer', 'time': '2026-06-27'},
                     {'title': 'Watched Non Promotional', 'time': '2026-10-26'}]
            p.write_text(json.dumps(rows))
            out = parse('youtube', p)
            self.assertEqual(len(out), 20)
            self.assertTrue(all(c.conf == .5 for c in out))
            self.assertEqual(min(c.last_ts for c in out), '2026-10-06')


class CrossSourceTests(unittest.TestCase):
    def setUp(self):
        from skald import members
        members.configure(readonly=True)

    def merge(self, rows, **kw):
        return cross_source(rows, person='ana', **kw)

    def test_explicit_wins_inferred_and_max_never_sum(self):
        out, counts = self.merge([imported('seen', n=8), imported('meh', 'imdb', n=2), imported('like', 'trakt', conf=.7, n=3)])
        self.assertEqual((out[0]['action'], out[0]['src'], out[0]['n']), ('meh', 'imdb', 8))
        self.assertEqual((counts['merged'], counts['corroborated']), (2, 1))

    def test_resolution_confidence_does_not_turn_rating_into_inference(self):
        out, _ = self.merge([imported('meh', 'letterboxd', conf=.94), imported('love', 'trakt', conf=.7)])
        self.assertEqual(out[0]['action'], 'meh')
        out, counts = self.merge([imported('seen')], existing=[imported('like', 'letterboxd', conf=.89)])
        self.assertEqual((out, counts['inferred_dropped']), ([], 1))

    def test_same_sign_strongest_and_source_survives(self):
        out, counts = self.merge([imported('like', 'imdb', t='2026-10-11'), imported('love', 'letterboxd', t='2026-09-01')])
        self.assertEqual((out[0]['action'], out[0]['src']), ('love', 'letterboxd'))
        self.assertEqual(counts['conflicts_new'], 0)

    def test_opposite_sign_newest_then_trust(self):
        out, counts = self.merge([imported('not_for_us', 'imdb', t='2026-10-01'), imported('love', 'trakt')])
        self.assertEqual(out[0]['action'], 'love')
        self.assertEqual(counts['conflicts_new'], 1)
        rows = [imported('love', 'plain'), imported('not_for_us', 'trakt')]
        self.assertEqual(self.merge(rows)[0][0]['src'], 'trakt')
        self.assertEqual(self.merge(list(reversed(rows)))[0], self.merge(rows)[0])

    def test_known_explicit_in_imports_blocks_inference(self):
        for action in EXPLICIT:
            out, counts = self.merge([imported('seen'), imported('like', conf=.7)], existing=[imported(action, 'trakt')])
            self.assertEqual(out, [])
            self.assertEqual(counts['inferred_dropped'], 2)
        out, _ = self.merge([imported('seen')], existing=[imported('like', 'trakt', conf=.7)])
        self.assertEqual(len(out), 1)

    def test_known_explicit_in_profile_blocks_inference(self):
        profile = [{'key': 'tmdb:87000001', 'who': 'trakt', 'by': ['ana'], 'signal': 'disliked', 'weight': -3}]
        out, counts = self.merge([imported('seen')], profile=profile)
        self.assertEqual(out, [])
        self.assertEqual(counts['inferred_dropped'], 1)

    def test_profile_mid_rating_and_import_provenance(self):
        profile = [{'key': 'tmdb:87000001', 'who': 'trakt', 'by': ['ana'], 'signal': 'watched',
                    'item': {'rating': 5}, 'weight': .3}]
        self.assertEqual(self.merge([imported('interest')], profile=profile)[0], [])
        profile[0].update(who='import', signal='liked', item={})
        self.assertEqual(self.merge([imported('seen')], profile=profile)[0], [])
        out, _ = self.merge([imported('seen')], profile=profile, existing=[imported('like', 'trakt', conf=.7)])
        self.assertEqual(len(out), 1)

    def test_existing_conflicts_are_reported_and_kept(self):
        row = imported('love', 'imdb')
        out, counts = self.merge([row], existing=[imported('not_for_us', 'trakt')])
        self.assertEqual(out, [row])
        self.assertEqual(counts['conflicts_existing'], 1)

    def test_abandon_never_over_positive_for_owner_or_both(self):
        for who, by in [('trakt', ['ana']), ('both', []), ('tv', []), ('told', ['together']), ('ana', [])]:
            profile = [{'key': 'tmdb:87000001', 'who': who, 'by': by, 'weight': .1, 'signal': 'watched'}]
            out, counts = self.merge([imported('abandon', conf=.4)], profile=profile)
            self.assertEqual(out, [])
            self.assertEqual(counts['abandon_dropped'], 1)
        out, counts = self.merge([imported('abandon', conf=.4)], existing=[imported('like', 'trakt')])
        self.assertEqual((out, counts['abandon_dropped']), ([], 1))
        out, _ = self.merge([imported('abandon', conf=.4)], profile=[{'key': 'tmdb:87000001', 'by': ['ben'], 'weight': 3}])
        self.assertEqual(len(out), 1)

    def test_separate_people_and_keys(self):
        rows = [imported('like', 'imdb'), imported('like', 'trakt'), imported('like', 'imdb', who='ben'),
                imported('seen', key='tmdb:87000002')]
        out, _ = self.merge(rows)
        self.assertEqual(len({(e['key'], e['who']) for e in out}), len(out))
        self.assertEqual(len(out), 3)

    def test_learned_names_format_determinism_and_no_review_titles(self):
        engine = SimpleNamespace(_model={'names': {'x:crime': 'Crime', 'x:mystery': 'slow-burn mysteries'}},
                                 react=Mock(return_value={'facets': [{'id': 'x:crime', 'name': 'Wrong', 'delta': .2},
                                     {'id': 'x:unknown', 'name': 'Review Canary', 'delta': .9},
                                     {'id': 'x:mystery', 'name': 'Wrong too', 'delta': .1}], 'rows': {}}))
        counts = {'corroborated': 9, 'conflicts_existing': 3, 'skipped_short': 40, 'skipped_type': 4, 'abandon_dropped': 1}
        rows = [imported('abandon', conf=.4)]
        lines = learned(engine, rows, counts, 'ana')
        self.assertEqual(lines, learned(engine, rows, counts, 'ana'))
        self.assertLessEqual(len(lines), 6)
        self.assertTrue(all(len(line) <= 90 for line in lines))
        self.assertEqual(lines[0], 'Ana: Crime and slow-burn mysteries moved up most')
        self.assertNotIn('Review Canary', '\n'.join(lines))
        self.assertNotIn('Synthetic Beacon', '\n'.join(lines))
        summary = preview(engine, rows, 'ana', counts=counts)
        self.assertEqual(summary['learned'], lines)
        out = io.StringIO()
        text_output(SimpleNamespace(command='onboard'), {'sources': [], 'mode': 'dry_run', 'added': 0, 'react': None,
                    'plan': None, 'next': None, 'learned': lines}, out)
        self.assertIn('What I learned:\n', out.getvalue())


class DepthOfflineGates(unittest.TestCase):
    def setUp(self):
        from skald import members
        members.configure(readonly=True)

    def test_new_fixtures_dry_run_merge_confidence_and_server_abandon(self):
        records = fixture_metadata()
        with tempfile.TemporaryDirectory() as tmp:
            data, state = Path(tmp) / 'data', Path(tmp) / 'state'
            data.mkdir(); state.mkdir()
            write_legacy(data)
            (data / 'profile.json').write_text('{"entries": []}')
            (state / 'canary').write_text('unchanged')
            sha = lambda: {str(p.relative_to(tmp)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in Path(tmp).rglob('*') if p.is_file()}
            before = sha()
            args = ['--snapshot', str(data), '--state-dir', str(state), '--json', 'onboard', '--person', 'ana',
                    '--non-interactive', '--profile-map', 'Export B=ana', '--dry-run',
                    *[v for s, p in specs() for v in ('--source', f'{s}={p}')]]
            provider = metadata_provider(records)
            with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                    patch('urllib.request.build_opener', side_effect=AssertionError('network forbidden')), \
                    patch.object(Engine, '_offline_tmdb', side_effect=provider), redirect_stdout(io.StringIO()):
                outputs = []
                for _ in range(2):
                    out = io.StringIO()
                    self.assertEqual(main(args, out=out), 0, out.getvalue())
                    outputs.append(json.loads(out.getvalue()))
                self.assertEqual(outputs, outputs[:1] * 2)
                with Engine(data) as engine:
                    row = engine._server.validate_import(imported('abandon', conf=.4))
                    self.assertEqual(row['action'], 'abandon')
                    self.assertEqual(engine._server.IMPORT_SIGNALS['abandon'], ('abandoned', -1.5))
            self.assertEqual(sha(), before)
            self.assertEqual(outputs[0]['merge']['skipped_short'], 2)
            self.assertEqual(outputs[0]['merge']['skipped_type'], 4)
            events = []
            for s, p in specs():
                for c in parse(s, p, profile_map={'Export B': 'ana'}):
                    r = next(r for r in records if r['title'] == c.title and r['year'] == c.year)
                    events.append(event(c, {'match': {'key': f'tmdb:{r["id"]}', 'title': c.title, 'year': c.year,
                                                      'kind': r['kind']}, 'conf': 1.}, 'ana'))
            self.assertTrue(all(0 < e['conf'] <= 1 for e in events))
            merged, _ = cross_source(events, person='ana')
            self.assertEqual(len(merged), len({(e['key'], e['who']) for e in merged}))
            positives = [{'key': e['key'], 'who': 'trakt', 'by': ['ana'], 'weight': 2}
                         for e in events if e['action'] == 'abandon']
            guarded, counts = cross_source(events, profile=positives, person='ana')
            self.assertEqual(counts['abandon_dropped'], sum(e['action'] == 'abandon' for e in events))
            self.assertTrue(all(e['action'] != 'abandon' for e in guarded))
