"""New weekly picks, using invented titles and offline transports only."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, ReplayLLM
from skald import members


def card(n, kind='movie', **facts):
    return {'id': f'tmdb:{n}', 'name': f'Invented Letter {chr(65+n)}', 'type': kind,
            'poster': 'p', 'description': 'An invented quiet mystery.',
            '_row': {'date': '2026-10-07', 'digital': '2026-10-07', 'votes': 300,
                     'rating': 8, 'g': ['Mystery'], **facts}}


class Weekly(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'skald.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        self.s = s = load_server(self.root)
        self.addCleanup(close_server, s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.now = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
        self.enterContext(patch.object(s, 'local_now', side_effect=lambda *a: self.now))
        self.enterContext(patch.object(s, 'get_model', return_value={'cal': True}))
        self.enterContext(patch.object(s, 'person_score', side_effect=lambda m, k, *a: (100-int(k[5:]), [])))
        self.enterContext(patch.object(s, 'match_pct', side_effect=lambda m, score, *a: int(score)))
        self.enterContext(patch.object(s, 'genres', return_value={}))
        self.enterContext(patch.object(s, 'tmdb', return_value={}))
        self.enterContext(patch.object(s, 'ensure_emb', side_effect=AssertionError('embedding forbidden')))
        self.enterContext(patch.object(s, 'ensure_facets', side_effect=AssertionError('enrichment forbidden')))
        self.enterContext(patch.object(s, 'tone_ok', return_value=True))
        s._profiles['household'] = {'entries': [], 'seen': set(), 'negative': set(), 'searches': []}
        self.put([card(i) for i in range(9)])

    def put(self, cards, key='digital'):
        self.s._feeds[self.s.rid(key), self.s.LANG] = {'metas': {'tv': cards}}

    def test_only_recent_watchable_candidates(self):
        s = self.s
        self.put([card(0, digital='1980-01-01', date='1980-01-01'),
                  card(1), card(2, digital='2026-11-01'),
                  card(3, kind='series', digital=None, date='2026-09-25'),
                  card(4, kind='series', digital=None, date='2010-01-01', season='2026-10-01'),
                  card(5, kind='series', digital=None, date='2010-01-01', season='2026-08-01')])
        self.assertEqual([x['id'] for x in s.week_candidates(s.LANG)], ['tmdb:1', 'tmdb:3', 'tmdb:4'])

    def test_release_facts_survive_feed_and_facet_caches(self):
        s = self.s
        movie = {'id': 1, 'title': 'Invented Lantern', 'release_date': '2026-01-01',
                 'release_dates': {'results': [{'iso_3166_1': s.rating_country(), 'release_dates': [
                     {'type': 3, 'release_date': '2026-01-01'},
                     {'type': 6, 'release_date': '2026-10-08'},
                     {'type': 4, 'release_date': '2026-12-01'}]}]}}
        series = {'id': 2, 'name': 'Invented Harbor', 'first_air_date': '2010-01-01', 'seasons': [
            {'season_number': 0, 'air_date': '2026-10-13'},
            {'season_number': 2, 'air_date': '2026-10-09'},
            {'season_number': 3, 'air_date': '2027-01-01'}]}
        for kind, det, field, expected in [('movie', movie, 'digital', '2026-10-08'),
                                          ('tv', series, 'season', '2026-10-09')]:
            with self.subTest(kind=kind), patch.object(s, 'tmdb', return_value=det):
                rec = s.facet_record(kind, det['id'])
                meta = s.row_meta(det, 'movie' if kind == 'movie' else 'series', kind, {}, det)
                self.assertEqual(rec.get(field), expected)
                self.assertEqual(meta['_row'].get(field), expected)

    def test_movie_rereleases_use_the_digital_old_premiere_rule(self):
        s = self.s
        det = {'id': 1, 'title': 'Invented Archive', 'release_date': '2026-10-07',
               'release_dates': {'results': [
                   {'iso_3166_1': 'DE', 'release_dates': [
                       {'type': 1, 'release_date': '1980-01-01'}, {'type': 2, 'release_date': '0000-00-00'}]},
                   {'iso_3166_1': s.rating_country(), 'release_dates': [
                       {'type': 3, 'release_date': '2026-10-01'},
                       {'type': 4, 'release_date': '2026-10-07'}]}]}}
        meta = s.row_meta(det, 'movie', 'movie', {}, det)
        rec = s.facet_record('movie', 1, det)
        for source in ('feed', 'feed_pair', 'facet', 'legacy_date', 'legacy_year', 'supplement'):
            with self.subTest(source=source):
                s._feeds.clear()
                s._facets.clear()
                if source == 'feed':
                    self.put([meta])
                elif source == 'feed_pair':
                    recent = {**det, 'release_dates': {'results': det['release_dates']['results'][1:]}}
                    self.put([s.row_meta(recent, 'movie', 'movie', {}, det)])
                elif source == 'facet':
                    s._facets['tmdb:1'] = rec
                    self.put([card(1, date=None)])
                elif source == 'legacy_date':
                    self.put([card(1, date='1980-01-01')])
                elif source == 'legacy_year':
                    s._facets['tmdb:1'] = {'kind': 'movie', 'y': '1980'}
                    self.put([card(1, date=None)])
                else:
                    with patch.object(s, 'tmdb', side_effect=[{'results': [det]}, det]):
                        self.assertEqual(s.week_candidates(s.LANG, s.week_digital(s.LANG)), [])
                    continue
                self.assertEqual(s.week_candidates(s.LANG), [])
        cutoff = self.now.date() - dt.timedelta(days=60+365)
        self.put([card(2, date=cutoff.isoformat()), card(3, date=(cutoff-dt.timedelta(days=1)).isoformat())])
        self.assertEqual([x['id'] for x in s.week_candidates(s.LANG)], ['tmdb:2'])

    def test_scope_requires_good_match_unseen_and_child_eligibility(self):
        s = self.s
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 10}
        safe, adult, low, seen, negative = card(1), card(2), card(31), card(4), card(5)
        self.put([safe, adult, low, seen, negative])
        for x in (safe, adult, low, seen, negative):
            s._facets[x['id']] = {'kind': 'movie', 'cr': {'US': 'R' if x is adult else 'PG'}, 'adult': False}
        s._profiles['household'].update(seen={seen['id']}, negative={negative['id']})
        candidates = s.week_candidates(s.LANG)
        self.assertEqual([x['id'] for x in s.week_scope(candidates, 'ana')], ['tmdb:1', 'tmdb:2'])
        self.assertEqual([x['id'] for x in s.week_scope(candidates, 'ben')], ['tmdb:1'])

    def test_week_uses_only_indices_and_freezes_across_languages_and_taste_changes(self):
        s = self.s
        answer = {'ana': [{'i': 2, 'reason': 'A quiet mystery matching your taste.'},
                          {'i': 999, 'reason': 'Invented outsider'}],
                  'ben': [{'i': 3, 'reason': 'The patient mystery you enjoy.'}],
                  'together': [{'i': 4, 'reason': 'Shared mystery for your evening.'}]}
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            first = s.week_feed(s.LANG)
            self.assertEqual([x['id'] for x in first[s.rid('week')]['tv']], ['tmdb:2', 'tmdb:3', 'tmdb:4'])
            self.put([card(8)])
            self.assertEqual(s.week_feed(s.LANG), first)
            self.assertEqual(s.week_feed('de-DE'), first)
            self.assertEqual(replay.calls, 1)
            prompt = replay.bodies[0]['messages']
            self.assertIn('indices', prompt[0]['content'])
            self.assertIn(s.LANG, prompt[0]['content'])
        self.now += dt.timedelta(days=7)
        next_week = s.week_feed(s.LANG)
        self.assertEqual([x['id'] for x in next_week[s.rid('week')]['tv']], ['tmdb:8'])

    def test_failure_and_budget_use_taste_order_once(self):
        s = self.s
        for failure in (TimeoutError('fake'), s.BudgetExceeded('fake')):
            with self.subTest(failure=type(failure).__name__), patch.object(s, 'llm_text', side_effect=failure) as llm, \
                    patch.object(s, 'OR_KEY', 'synthetic'):
                s._learn.pop('week', None)
                result = s.week_feed(s.LANG)[s.rid('week')]['tv']
                self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])
                self.assertIn('New since 7 Oct', result[0]['description'])
                self.assertIn('100%', result[0]['description'])
                s.week_feed(s.LANG)
                self.assertEqual(llm.call_count, 1)

    def test_german_fallback_reason_reads_naturally(self):
        s = self.s
        with patch.object(s, 'LANG', 'de-DE'), patch.object(s, 'OR_KEY', None):
            s._learn.pop('week', None)
            result = s.week_feed('de-DE')[s.rid('week')]['tv']
        self.assertIn('100 % Übereinstimmung · Neu seit 7. Okt', result[0]['description'])
        self.assertEqual(s.day_text('2027-02-03'), '3 Feb 2027')
        with patch.object(s, 'LANG', 'de-DE'):
            self.assertEqual(s.day_text('2026-10-14', True), s.t('weekday.3') + ' 14. Okt')

    def test_raw_taste_fallback_and_variety_caps(self):
        s = self.s
        self.put([card(i, collection=42 if i < 3 else None) for i in range(15)])
        with patch.object(s, 'match_pct', return_value=None):
            self.assertEqual([x['id'] for x in s.week_scope(s.week_candidates(s.LANG), 'ana')],
                             ['tmdb:0', 'tmdb:1', 'tmdb:2'])
        with patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 4):
            result = s.week_feed(s.LANG)[s.rid('week')]['tv']
        self.assertLessEqual(sum(x['_row'].get('collection') == 42 for x in result), 2)
        self.assertLessEqual(sum(int(x['id'][5:]) < 4 for x in result), 2)

    def test_job_rolls_over_monday_midnight_without_a_key(self):
        s = self.s
        def stamp(day):
            return next(x[2] for x in s.job_specs(day, {'household': 'taste'}) if x[0] == 'week')
        sunday = dt.datetime(2026, 10, 18, 23, 59)
        self.assertNotEqual(stamp(sunday), stamp(sunday + dt.timedelta(minutes=1)))
        self.assertEqual(stamp(self.now), stamp(self.now + dt.timedelta(days=1)))
        before = stamp(self.now)
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 10}
        self.assertNotEqual(stamp(self.now), before)

    def test_background_digital_lookup_is_bounded_and_cache_first(self):
        s = self.s
        s._feeds.clear()
        s.CONFIG['household']['rows'] = ['week']
        calls = []
        def tmdb(path, **params):
            calls.append((path, params))
            if path == 'discover/movie':
                return {'results': [{'id': i, 'title': f'Invented Letter {chr(65+i)}'} for i in range(20)]}
            n = int(path.split('/')[1])
            return {'id': n, 'title': f'Invented Letter {chr(65+n)}', 'release_date': '2026-06-01',
                    'vote_count': 300, 'vote_average': 8, 'genres': [],
                    'release_dates': {'results': [{'iso_3166_1': s.rating_country(), 'release_dates': [
                        {'type': 4, 'release_date': '2026-10-07'}]}]}}
        with patch.object(s, 'tmdb', side_effect=tmdb):
            result = s.week_feed(s.LANG)
            s.week_feed('de-DE')
        self.assertTrue(result[s.rid('week')]['tv'])
        self.assertEqual(len(calls), 13)
        self.assertEqual(calls[0][1]['with_release_type'], '4|6')
        self.assertEqual(calls[0][1]['release_date.gte'], '2026-09-02')
        s._learn.pop('week')
        self.put([card(i) for i in range(9)])
        with patch.object(s, 'tmdb', side_effect=AssertionError('cached releases suffice')):
            self.assertTrue(s.week_feed(s.LANG)[s.rid('week')]['tv'])

    def test_serving_freezes_order_drops_watched_and_never_refills(self):
        s = self.s
        # Give every scope distinct picks so both scoped and household rows meet the minimum.
        result = s.week_feed(s.LANG)[s.rid('week')]
        s._feeds[s.rid('week'), s.LANG] = {'metas': result}
        p = s._profiles['household']
        def serve(who=None):
            return s.served_row(s.rid('week'), 'tv', s.LANG, p, set(), (set(), set()), who)
        with patch.object(s, 'week_candidates', side_effect=AssertionError('request lookup')), \
                patch.object(s, 'tmdb', side_effect=AssertionError('request network')):
            first = serve()
            self.assertEqual(len(first), 9)
            self.now += dt.timedelta(days=1)
            self.assertEqual(serve(), first)
            p['seen'].add(first[0]['id'])
            self.assertEqual([x['id'] for x in serve()], [x['id'] for x in first[1:]])
            p['negative'].add(first[1]['id'])
            self.assertNotIn(first[1]['id'], [x['id'] for x in serve()])
            self.assertEqual(serve('ana'), [])  # 3 personal + 3 together, less 2 watched/disliked.
            self.assertEqual(len(serve('ben')), 6)
            p['seen'].update(x['id'] for x in first[:5])
            self.assertEqual(serve(), [])

    def test_history_last_n_weeks_offline_cli_commander_and_old_readers(self):
        from skald import commands
        from skald.engine import Engine
        from skald.__main__ import execute, parser
        s = self.s
        for _ in range(10):
            self.put([card(i, digital=self.now.date().isoformat()) for i in range(9)])
            s.week_feed(s.LANG)
            self.now += dt.timedelta(days=7)
        self.now -= dt.timedelta(days=7)
        self.assertEqual(len(s._learn['week']), 8)
        self.assertEqual(sum('cards' in r for r in s._learn['week']), 1)
        expected = s.week_history(2, 'ana')
        self.assertEqual([r['week'] for r in expected['weeks']], ['2026-12-14', '2026-12-07'])
        self.assertTrue(all(p['member'] == 'ana' for r in expected['weeks'] for p in r['picks']))
        self.assertNotIn('cards', expected['weeks'][0])
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with Engine(self.root) as engine, patch.object(engine._server, 'local_now', return_value=self.now + dt.timedelta(weeks=12)):
            args = parser().parse_args(['rows', 'week', '--weeks', '2', '--person', 'ana', '--json'])
            self.assertEqual(execute(args, engine, None, None), expected)
            engine._server.local_now.return_value = self.now
            result = commands.preview(engine._server, 'synthetic recall', viewer='ana', answer={
                'op': 'week_picks', 'kind': None, 'values': [2, 0], 'person': 'ana'})
            self.assertTrue(result['read_only'])
            self.assertEqual(result['week_picks'], expected)
            last = commands.preview(engine._server, 'synthetic last week', answer={
                'op': 'week_picks', 'kind': None, 'values': [1, 1], 'person': None})
            self.assertEqual(last['week_picks']['weeks'][0]['week'], '2026-12-07')
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, before)
        # The candidate's old loader updates known learning fields and leaves extra keys alone.
        stored = json.loads((self.root/'learn.json').read_text())
        legacy = {'served': {}, 'rows': {}, 'hits': [], 'credited': {}, 'lanes': {}, 'ctx': [], 'moodpicks': {}}
        legacy.update(stored)
        self.assertEqual(legacy['hits'], [])
        self.assertEqual(s.learn_stats()['tracked'], 0)
        for n in (0, 9):
            with self.assertRaises(ValueError):
                s.week_history(n)

    def test_restarts_and_people_changes_cannot_spend_twice(self):
        s = self.s
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"ana":[],"ben":[],"together":[]}'}}]})
            s.week_feed(s.LANG)
            s._learn.clear()
            s._learn.update(json.loads((self.root/'learn.json').read_text()))
            s.week_feed(s.LANG)
            s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 10}
            s.week_feed(s.LANG)
            self.assertEqual(replay.calls, 1)
            self.assertFalse(any(p['member'] == 'ben' for p in s._learn['week'][-1]['picks']))

    def test_incomplete_reservation_resumes_without_another_llm_call(self):
        s = self.s
        for stage, calls in (('week_candidates', 0), ('week_spread', 1)):
            with self.subTest(stage=stage), ReplayLLM(s) as replay:
                s._learn.pop('week', None)
                replay.arm({'choices': [{'message': {'content': '{"ana":[{"i":0,"reason":"Recent mystery."}]}'}}]})
                with patch.object(s, stage, side_effect=RuntimeError('synthetic restart')):
                    with self.assertRaises(RuntimeError):
                        s.week_feed(s.LANG)
                stored = json.loads((self.root/'learn.json').read_text())
                self.assertNotIn('cards', stored['week'][-1])
                s._learn.clear()
                s._learn.update(stored)
                result = s.week_feed(s.LANG)[s.rid('week')]['tv']
                self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])
                self.assertEqual(replay.calls, calls)

    def test_failed_learning_save_preserves_dirty_flag(self):
        s = self.s
        s._learn_dirty.set()
        with patch.object(s.os, 'replace', side_effect=OSError('synthetic full disk')):
            self.assertFalse(s.save_learn())
        self.assertTrue(s._learn_dirty.is_set())
        self.assertTrue(s.save_learn())
        self.assertFalse(s._learn_dirty.is_set())

    def test_failed_reservation_save_uses_taste_without_transports(self):
        s = self.s
        baseline = copy.deepcopy(s._learn)
        real_open = open
        def full_disk(path, *args, **kwargs):
            if str(path).endswith('learn.json.tmp'):
                raise OSError('synthetic read-only storage')
            return real_open(path, *args, **kwargs)
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{}'}}]})
            for _ in range(2):
                s._learn.clear()
                s._learn.update(copy.deepcopy(baseline))
                with patch('builtins.open', side_effect=full_disk):
                    result = s.week_feed(s.LANG)[s.rid('week')]['tv']
                self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])
                self.assertEqual(replay.calls, 0)
                self.assertTrue(s._learn_dirty.is_set())
            s._learn.clear()
            s._learn.update(copy.deepcopy(baseline))
            s.week_feed(s.LANG)
            s._learn.clear()
            s.load_state()
            s.week_feed(s.LANG)
            self.assertEqual(replay.calls, 1)
        s._learn.pop('week')
        s._feeds.clear()
        with patch('builtins.open', side_effect=full_disk), patch.object(s, 'tmdb') as tmdb:
            self.assertEqual(s.week_feed(s.LANG)[s.rid('week')]['tv'], [])
            tmdb.assert_not_called()

    def test_failed_completion_save_keeps_the_reservation_across_restart(self):
        s = self.s
        replace = s.os.replace
        def fail_completion(src, dst):
            if str(dst).endswith('learn.json') and (self.root/'learn.json').exists():
                raise OSError('synthetic full disk after reservation')
            replace(src, dst)
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{}'}}]})
            with patch.object(s.os, 'replace', side_effect=fail_completion):
                s.week_feed(s.LANG)
            self.assertTrue(s._learn_dirty.is_set())
            s._learn.clear()
            s.load_state()
            self.put([card(i) for i in range(9)])
            result = s.week_feed(s.LANG)[s.rid('week')]['tv']
            self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])
            self.assertEqual(replay.calls, 1)

    def test_people_rebuild_reuses_supplement_and_rechecks_freshness_and_scope(self):
        s = self.s
        s._feeds.clear()
        s.CONFIG['household']['rows'] = ['week']
        calls = []
        def tmdb(path, **params):
            calls.append(path)
            if path == 'discover/movie':
                return {'results': [{'id': i, 'title': f'Invented Letter {chr(65+i)}'} for i in range(9)]}
            n = int(path.split('/')[1])
            return {'id': n, 'title': f'Invented Letter {chr(65+n)}', 'release_date': '2026-06-01',
                    'vote_count': 300, 'vote_average': 8, 'genres': [],
                    'release_dates': {'results': [{'iso_3166_1': s.rating_country(), 'release_dates': [
                        {'type': 4, 'release_date': '2026-09-03' if n == 2 else '2026-10-07'}]}]}}
        with ReplayLLM(s) as replay, patch.object(s, 'tmdb', side_effect=tmdb):
            replay.arm({'choices': [{'message': {'content': '{}'}}]})
            first = s.week_feed(s.LANG)[s.rid('week')]['tv']
            self.assertEqual(len(first), 9)
            self.assertEqual(len(calls), 10)
            s._learn.clear()
            s.load_state()
            self.now += dt.timedelta(days=2)
            s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 10}
            s._profiles['household'].update(seen={'tmdb:0'}, negative={'tmdb:1'})
            s._facets['tmdb:3']['cr'] = {'US': 'PG'}
            with patch.object(s, 'tmdb', side_effect=AssertionError('people rebuild must reuse saved metadata')):
                result = s.week_feed(s.LANG)[s.rid('week')]['tv']
            self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(3, 9)])
            self.assertFalse(any(p['member'] == 'ben' for p in s._learn['week'][-1]['picks']))
            self.assertEqual(replay.calls, 1)
            self.assertEqual(s.week_feed(s.LANG)[s.rid('week')]['tv'], result)

    def test_malformed_week_history_is_skipped_on_load_and_offline_recall(self):
        from skald import commands
        from skald.engine import Engine
        from skald.__main__ import execute, parser
        s = self.s
        week = s.week_start()
        pick = {'id': 'tmdb:1', 'member': 'ana', 'reason': 'Recent mystery.', 'week': week,
                'kind': 'movie', 'title': 'Invented Archive'}
        valid = {'week': week, 'people': s.week_people(), 'picks': [pick]}
        reservation = {'week': '2026-10-05', 'people': s.week_people(), 'picks': []}
        bad = [{}, None, 1, 'bad', {**valid, 'week': 'bad'}, {**valid, 'week': 1},
               {**valid, 'people': None}, {**valid, 'picks': None}, {**valid, 'picks': [{}]},
               {**valid, 'picks': [None]}, {**valid, 'picks': [{**pick, 'reason': ''}]},
               {**valid, 'picks': [{**pick, 'kind': 'bad'}]}, {**valid, 'cards': {}}]
        expected = {'person': None, 'weeks': [{'week': week, 'picks': [pick]}], 'personal': []}
        for records, kept in (([{}], []), ([reservation, valid, *bad], [reservation, valid]), (None, []), ({}, [])):
            with self.subTest(records=records):
                path = self.root/'learn.json'
                path.write_text(json.dumps({'week': records}))
                before = path.read_bytes()
                with Engine(self.root) as engine:
                    self.assertEqual(engine._server._learn['week'], kept)
                    args = parser().parse_args(['rows', 'week', '--json'])
                    self.assertEqual(execute(args, engine, None, None), expected if kept else {'person': None, 'weeks': [], 'personal': []})
                    engine._server.local_now = lambda: self.now
                    result = commands.preview(engine._server, 'synthetic recall', answer={
                        'op': 'week_picks', 'kind': None, 'values': [1, 0], 'person': None})
                    self.assertEqual(result['week_picks'], expected if kept else {'person': None, 'weeks': [], 'personal': []})
                self.assertEqual(path.read_bytes(), before)

    def test_serving_skips_cached_cards_with_empty_scopes(self):
        s = self.s
        cards = [{**card(i), '_week_start': s.week_start(), '_week': {'ana': 'Recent mystery.'}} for i in range(9)]
        cards[0]['_week'] = {}
        self.put(cards, 'week')
        with patch.object(s, 'row_eligible', return_value=True):
            result = s.served_row(s.rid('week'), 'tv', s.LANG, {}, set(), (set(), set()))
        self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(1, 9)])

    def test_real_budget_gate_falls_back_without_transport(self):
        s = self.s
        with ReplayLLM(s) as replay:
            s._usage_roll()
            s._usage['calls']['feed:synthetic'] = s.AI_CAPS['feed']
            result = s.week_feed(s.LANG)[s.rid('week')]['tv']
            self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])
            self.assertEqual(replay.calls, 0)

    def test_invalid_indices_fall_back_and_child_recall_filters_ratings(self):
        s = self.s
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"ana":[{"i":999,"reason":"invented"}]}'}}]})
            s.week_feed(s.LANG)
        self.assertEqual([p['id'] for p in s.week_history(1, 'ana')['weeks'][0]['picks']],
                         ['tmdb:0', 'tmdb:1', 'tmdb:2'])
        s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 10}
        s._facets['tmdb:0'] = {'kind': 'movie', 'cr': {'US': 'PG'}}
        self.assertEqual([p['id'] for p in s.week_history(1, 'ana')['weeks'][0]['picks']], ['tmdb:0'])

    def test_recall_intent_is_model_only_in_three_languages(self):
        from eval import commands_eval
        from skald import commands
        cases = [c for c in commands_eval.load_cases() if c['id'].startswith('w109-week-')]
        self.assertEqual(len(cases), 24)
        for lang in ('en', 'de', 'sr'):
            group = [c for c in cases if c['lang'] == lang]
            self.assertEqual(len(group), 8)
            self.assertEqual({c['expect']['op'] for c in group}, {'week_picks', None})
        for case in cases:
            self.assertIsNone(commands.parse_local(case['text'], self.s)['op'])
            with self.subTest(id=case['id']), ReplayLLM(self.s) as replay:
                replay.arm({'choices': [{'message': {'content': json.dumps(case['expect'])}}]})
                answer = self.s.decide('parse_command', {'query': case['text']})['answer']
                self.assertEqual(answer['op'], case['expect']['op'])
                self.assertEqual(answer['values'], case['expect']['values'])
                self.assertEqual(replay.calls, 1)

    def test_loading_taste_does_not_freeze_an_empty_week(self):
        s = self.s
        with patch.object(s, 'get_model', return_value=None), \
                patch.object(s, 'tmdb', side_effect=AssertionError('taste is still loading')):
            self.assertIsNone(s.week_feed(s.LANG))
        self.assertFalse(s._learn.get('week'))
        self.assertTrue(s.week_feed(s.LANG)[s.rid('week')]['tv'])

    def test_digital_feed_keeps_its_verified_release_date(self):
        s = self.s
        with patch.object(s, 'with_art', return_value=([{'id': 1, 'title': 'Invented Lantern'}], {})), \
                patch.object(s, 'tmdb', return_value={'results': [{'iso_3166_1': s.rating_country(), 'release_dates': [
                    {'type': 4, 'release_date': '2026-10-01T00:00:00Z'}]}]}):
            digital = s.digital_feed(s.LANG)
        self.assertEqual(digital['movie'][0]['_row'].get('digital'), '2026-10-01')

    def test_empty_background_week_never_borrows_old_titles(self):
        s = self.s
        self.put([card(31)])
        with patch.object(s, '_langs', {s.LANG}), patch.object(s, 'connectors_tick'), \
                patch.object(s, 'fill_row', side_effect=AssertionError('weekly refill forbidden')):
            s.build_feeds(self.now, {}, {}, only={'week'})
            self.assertEqual(s.served_row(s.rid('week'), 'tv', s.LANG, {}, set(), (set(), set())), [])

    def test_legacy_week_without_release_proof_hides_until_background_rebuild(self):
        s = self.s
        self.put([card(i, date='1980-01-01', digital=None) for i in range(9)], 'week')
        with patch.object(s, 'row_eligible', return_value=True), \
                patch.object(s, 'fill_row', side_effect=AssertionError('legacy week refill forbidden')):
            self.assertEqual(s.served_row(s.rid('week'), 'tv', s.LANG, {}, set(), (set(), set())), [])

    def test_cached_releases_in_another_language_prevent_extra_lookups(self):
        s = self.s
        with patch.object(s, 'tmdb', side_effect=AssertionError('reuse cached release facts')):
            result = s.week_feed('de-DE')[s.rid('week')]['tv']
        self.assertEqual([x['id'] for x in result], [f'tmdb:{i}' for i in range(9)])

    def test_week_pick_fixtures(self):
        s = self.s
        path = Path(__file__).resolve().parents[1]/'eval/fixtures/decisions/week.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 20)
        for row in rows:
            s._learn.pop('week', None)
            s._profiles['household']['seen'] = set(row['in'].get('seen', []))
            with self.subTest(note=row['note']), patch.object(s, 'LANG', row['in']['language']), ReplayLLM(s) as replay:
                replay.arm(row['recorded'])
                result = s.week_feed(s.LANG)[s.rid('week')]['tv']
                self.assertEqual([x['id'] for x in result], row['expect']['ids'])
                self.assertEqual(replay.calls, 1)


if __name__ == '__main__':
    unittest.main()
