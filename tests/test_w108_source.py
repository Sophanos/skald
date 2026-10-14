"""Themed sources use synthetic decisions and transports only."""
from contextlib import contextmanager, redirect_stdout
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import check_contract, close_server, load_server, reset


def card(i, **fields):
    return {'id': f'tmdb:{i}', 'name': f'Synthetic Story {i}', 'type': 'movie',
            'releaseInfo': '2020', 'description': 'Old keyword · Synthetic overview.',
            '_row': {'overview': 'Synthetic overview.', 'rating': 8, 'votes': 500,
                     'date': '2020-01-01'}, **fields}


class ThemedSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        s = self.s
        reset(s)
        Path(s.DATA, 'usage.json').unlink(missing_ok=True)
        self.p = {'entries': [], 'seen': set(), 'negative': set()}
        self.m = {'aff': {'x:cozy_case': 1, 'k:1': 1},
                  'names': {'x:cozy_case': 'Gentle puzzles', 'k:1': 'Courtroom stories'}}
        self.theme = {'kind': 'lane', 'name': 'Courtroom stories', 'key': 'k:1',
                      'source': ['k:1', 'lawyer']}
        self.answer = {'fits_now': True, 'months': 'any', 'keep': [0], 'add': [],
                       'why': {'0': 'A patient advocate untangles an impossible case.'}}
        self.calls = []
        self.scores = {}
        self.enterContext(patch.dict(s._theme_sources, {}, clear=True))
        for store in (s._feeds, s._shown, s._facets):
            self.enterContext(patch.dict(store, {}, clear=True))
        self.enterContext(patch.dict(s._profiles, {'household': self.p}, clear=True))
        self.enterContext(patch.dict(s._learn, {'served': {}, 'lanes': {}, 'rows': {}, 'moodpicks': {}}, clear=True))
        self.rank, self.ensure_facets, self.frec = s.rank_by_taste, s.ensure_facets, s.frec
        for name, value in {'get_model': self.m, 'taste_hint': 'experiences: Gentle puzzles',
                            'genres': {}, 'tired_keys': {}, 'because_seed': None,
                            'row_pool': [], 'remote_masks': (set(), set()), 'row_list': [],
                            'frec': {'x': {'cozy_case': 2}}, 'femb': None, 'fit': .6,
                            'ensure_emb': None, 'ensure_facets': None, 'tag_experiences': None}.items():
            self.enterContext(patch.object(s, name, return_value=value))
        self.enterContext(patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)))
        self.enterContext(patch.object(s, 'OR_KEY', 'fake'))
        self.transport = s.openrouter
        self.enterContext(patch.object(s, 'openrouter', side_effect=self.llm))
        self.enterContext(patch.object(s, 'rank_by_taste', side_effect=lambda m, xs: [
            (self.scores.get(k, 60), [], k, kind, x) for k, kind, x in xs]))
        self.enterContext(patch.object(s, 'score_key', side_effect=lambda m, k, **kw: (self.scores.get(k, 60), [])))
        self.enterContext(patch.object(s, 'person_score', side_effect=lambda m, k, *a: (self.scores.get(k, 60), [])))
        self.enterContext(patch.object(s, 'match_pct', side_effect=lambda m, sc, *a: sc))
        self.enterContext(patch.object(s, 'resolve_pick', side_effect=self.resolve))
        for name in ('balance_row', 'polish_row', 'with_match'):
            self.enterContext(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(s, '_connector_open', side_effect=AssertionError('network forbidden')))
        self.enterContext(redirect_stdout(io.StringIO()))

    def llm(self, path, body, timeout, lane, name):
        self.calls.append((path, body, timeout, lane, name))
        answer = {'months': 'any'} if name == 'theme_season' else self.answer
        return {'choices': [{'message': {'content': json.dumps(answer)}}], 'usage': {'cost': .001}}

    def resolve(self, kind, title, year, lang):
        i = int(title.rsplit(' ', 1)[1])
        return ({'id': i, 'title' if kind == 'movie' else 'name': title,
                 'release_date' if kind == 'movie' else 'first_air_date': '2020-01-01',
                 'vote_count': 500, 'vote_average': 8, 'overview': 'Added synthetic overview.'}, {})

    def source(self, items=None, person=None):
        return self.s.themed_row(self.m, self.p, self.theme,
                                items if items is not None else [card(1), card(2)],
                                'de-DE', self.s.LANES[0], person=person)

    @contextmanager
    def metered(self, persist=False):
        s = self.s
        def reply(req, **kw):
            body = json.loads(req.data)
            try:
                inp = json.loads(body['messages'][1]['content'])
            except ValueError:
                inp = {}
            answer = {'months': 'any'} if 'source' in inp and 'candidates' not in inp else self.answer
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}],
                                         'usage': {'cost': .001}}).encode())
        with patch.object(s, 'openrouter', side_effect=self.transport) as paid, \
                patch.object(s, '_connector_open', side_effect=reply), \
                patch.object(s, 'save_usage', wraps=s.save_usage if persist else None):
            yield paid

    def forty_lanes(self):
        s = self.s
        self.m['aff'] = {f'k:{i}': 41-i for i in range(1, 41)}
        self.m['names'] = {f'k:{i}': f'Synthetic Theme {i}' for i in range(1, 41)}
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020, 'type': 'movie',
                              'why': 'A patient advocate untangles an impossible case.'} for i in range(100, 130)]
        def titles(fid, lang):
            xs = [card(i) for i in range(1, 21)] if fid == 'k:40' else [card(1)]
            return [(x['id'], 'movie', x) for x in xs]
        with patch.object(s, 'lane_titles', side_effect=titles) as candidates, \
                patch.object(s, 'resolve_pick', return_value=None) as resolve:
            out = s.lanes_feed('de-DE')
        return out, candidates.call_count, resolve.call_count

    def test_forty_themes_bound_vetting_and_resolve_searches_and_keep_fallback(self):
        with self.metered() as paid:
            out, tries, searches = self.forty_lanes()
        self.assertEqual(tries, 40)
        with self.subTest(metric='theme_source'):
            self.assertEqual(sum(c.args[-1] == 'theme_source' for c in paid.call_args_list), 8)
        with self.subTest(metric='resolve_pick'):
            self.assertEqual(searches, 240)
        with self.subTest(metric='fallback'):
            self.assertEqual(len(out[self.s.LANES[0]]['tv']), 20)
            self.assertEqual(out['_names'][self.s.LANES[0]]['lane'], 'k:40')

    def test_lanes_and_moods_leave_budget_for_personal_week_and_docsai(self):
        s = self.s
        self.m['paff'] = {}
        self.p['searches'] = ['Synthetic preference']
        with self.metered() as paid:
            self.forty_lanes()
            # Failed mood shelves walk all twelve themes and exhaust only the theme ceiling.
            with patch.object(s, 'pick_moods', return_value=[]), \
                    patch.object(s, 'feeling_pool', return_value=[('tmdb:1', 'movie', card(1))]), \
                    patch.object(s, 'resolve_pick', return_value=None):
                s.mood_feed('de-DE')
            fresh = card(7, _row={**card(7)['_row'], 'watchable': '2026-10-10'})
            # The weekly call needs at least one vetted new release.
            with patch.object(s, 'candidate_pool', return_value=[]), \
                    patch.object(s, 'profile_text', return_value='Synthetic preference'), \
                    patch.object(s, 'hit_titles', return_value=[]), \
                    patch.object(s, 'week_candidates', return_value=[fresh]), \
                    patch.object(s, 'week_scope', return_value=[{**fresh, '_week_pct': 80}]):
                s.personal_feed('de-DE')
                s.week_feed('de-DE')
                s.docs_ai_feed('de-DE')
        labels = [c.args[-1] for c in paid.call_args_list]
        self.assertEqual(sum(s._usage['calls'].get('feed:'+n, 0) for n in ('theme_source', 'theme_season')), 12)
        for label in ('personal', 'week', 'docs'):
            self.assertEqual(s._usage['calls'].get('feed:' + label), 1)
            self.assertIn(label, labels)

    def test_forty_uncached_themes_bound_fallback_detail_calls(self):
        s = self.s
        self.m['aff'] = {f'k:{i}': 41-i for i in range(1, 41)}
        self.m['names'] = {f'k:{i}': f'Synthetic Theme {i}' for i in range(1, 41)}
        self.answer.update(fits_now=False, keep=[], why={})
        def titles(fid, lang):
            start = int(fid[2:]) * 160
            return [(f'tmdb:{i}', 'movie', card(i)) for i in range(start, start + 160) for _ in range(2)]
        def detail(path, **params):
            self.assertTrue(path.startswith('movie/'), path)
            i = int(path.split('/')[1])
            return {'id': i, 'title': f'Synthetic Story {i}', 'original_language': 'en'}
        with self.metered() as paid, \
                patch.object(s, 'lane_titles', side_effect=titles) as candidates, \
                patch.object(s, 'rank_by_taste', self.rank), \
                patch.object(s, 'ensure_facets', self.ensure_facets), \
                patch.object(s, 'frec', self.frec), \
                patch.object(s, 'score_key', return_value=(54, [])), \
                patch.object(s, 'tmdb', side_effect=detail) as details:
            out = s.lanes_feed('de-DE')
        self.assertEqual(candidates.call_count, 40)
        self.assertEqual(paid.call_count, 12)
        self.assertEqual(s._usage['calls']['feed:theme_source'], 8)
        self.assertEqual(s._usage['calls']['feed:theme_season'], 4)
        s.resolve_pick.assert_not_called()
        self.assertFalse(any(out[cid]['tv'] for cid in s.LANE_ROWS))
        self.assertEqual(details.call_count, 32 * 40)

    def test_failed_theme_transports_stay_counted_after_restart_without_manual_save(self):
        s = self.s
        reservations = []
        def fail(req, **kw):
            path = Path(s.DATA, 'usage.json')
            calls = json.loads(path.read_text())['calls'] if path.exists() else {}
            reservations.append(sum(calls.get('feed:'+n, 0) for n in ('theme_source', 'theme_season')))
            raise TimeoutError()
        with patch.object(s, 'openrouter', side_effect=self.transport), \
                patch.object(s, '_connector_open', side_effect=fail) as transport, \
                patch.object(s, 'save_usage', wraps=s.save_usage) as saved:
            for restarted in (False, True):
                if restarted:
                    s._usage.update(day='', calls={})
                    s.load_state()
                for i in range(15):
                    self.theme.update(key=f'k:{i}', name=f'Synthetic Theme {i}')
                    self.source()
            self.assertEqual(saved.call_count, 84)  # Each call saves at reservation and exit; failed or capped attempts also save their reason.
            self.assertEqual(transport.call_count, 12)
            self.assertEqual(reservations, list(range(1, 13)))  # Usage is saved when each call is reserved.
            self.assertEqual(sum(s._usage['calls'].get('feed:'+n, 0) for n in ('theme_source', 'theme_season')), 12)
            self.assertEqual(sum(json.loads(Path(s.DATA, 'usage.json').read_text())['calls'].get('feed:'+n, 0)
                                 for n in ('theme_source', 'theme_season')), 12)

    def test_daily_theme_cap_survives_source_cache_restart_and_rolls_over(self):
        s = self.s
        with self.metered(persist=True) as paid:
            for i in range(15):
                self.theme.update(kind=('lane', 'mood', 'leap')[i % 3], key=f'k:{i}', name=f'Synthetic Theme {i}')
                self.source()
                s._theme_sources.clear()
                s._decide_cache.clear()
            self.assertEqual(paid.call_count, 12)
            self.assertEqual(s._usage['calls']['feed:theme_source'], 12)
            self.assertEqual(s.save_usage.call_count, 30)  # Two saves per paid call plus one per capped attempt.
            s._usage.update(day='', calls={})
            s.load_state()
            self.source()
            self.assertEqual(paid.call_count, 12)
            with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 15, 18)):
                self.source()
            self.assertEqual(paid.call_count, 13)

    def test_empty_candidates_spend_nothing_for_each_theme_kind(self):
        for kind in ('lane', 'mood', 'leap'):
            self.theme['kind'] = kind
            with self.subTest(kind=kind):
                self.assertEqual(self.source([]), [])
        self.assertEqual(self.calls, [])
        self.s.resolve_pick.assert_not_called()

    def test_unvetted_fallback_filters_ranks_and_does_not_cache_as_vetted(self):
        s = self.s
        self.p['seen'].add('tmdb:3')
        self.p['negative'].add('tmdb:4')
        self.scores.update({'tmdb:1': 60, 'tmdb:2': 90, 'tmdb:5': 54})
        xs = [card(i) for i in range(1, 7)]
        with patch.object(s, 'row_eligible', side_effect=lambda x, *a, **kw: x['id'] != 'tmdb:6'):
            metas = s.themed_row(self.m, self.p, self.theme, xs, 'de-DE', s.LANES[0], allow_llm=False)
        self.assertEqual([x['id'] for x in metas], ['tmdb:2', 'tmdb:1'])
        self.assertTrue(all('theme' not in x['_row'] for x in metas))
        self.assertEqual([c[-1] for c in self.calls], ['theme_season'])
        self.assertEqual(s._theme_sources, {})
        self.assertEqual([x['id'] for x in self.source(xs)], ['tmdb:1'])
        self.assertEqual(len(self.calls), 2)

    def test_unvetted_fallback_uses_taste_order_and_never_resolves_cached_decisions(self):
        s = self.s
        self.answer['add'] = [{'title': 'Synthetic Story 99', 'year': 2020, 'type': 'movie',
                               'why': 'A patient advocate untangles an impossible case.'}]
        self.scores.update({'tmdb:1': 89, 'tmdb:2': 90, 'tmdb:3': 55})
        xs = [card(i) for i in range(1, 4)]
        self.source(xs)
        s._theme_sources.clear()
        s.resolve_pick.reset_mock()
        metas = s.themed_row(self.m, self.p, self.theme, xs, 'de-DE', s.LANES[0], allow_llm=False)
        self.assertEqual([x['id'] for x in metas], ['tmdb:2', 'tmdb:1', 'tmdb:3'])
        s.resolve_pick.assert_not_called()

    def test_keep_drops_misfit_and_notes_use_why(self):
        metas = self.source()
        self.assertEqual([x['id'] for x in metas], ['tmdb:1'])
        self.assertEqual(self.s.card_parts(metas[0])[0], [self.answer['why']['0']])
        self.assertNotIn('Old keyword', metas[0]['description'])
        self.assertEqual(metas[0]['_row']['theme'], 'k:1')

    def test_prompt_is_bounded_command_json_with_date_language_and_search_taste(self):
        self.source([card(i) for i in range(1, 61)])
        path, body, timeout, lane, name = self.calls[0]
        self.assertEqual((path, body['model'], timeout, lane, name),
                         ('/api/v1/chat/completions', self.s.COMMAND_MODEL, 15, 'feed', 'theme_source'))
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertLessEqual(body['max_tokens'], 2000)
        inp = json.loads(body['messages'][1]['content'])
        self.assertEqual((inp['date'], inp['language']), ('2026-10-14', self.s.LANG))
        self.assertEqual(inp['taste'], 'experiences: Gentle puzzles')
        self.assertEqual(len(inp['candidates']), 40)
        self.assertEqual(inp['candidates'][0]['overview'], 'Synthetic overview.')
        self.assertEqual(self.s.decide_stats()['theme_source']['sources']['llm'], 1)

    def test_added_movies_and_series_resolve_filter_seen_and_deduplicate(self):
        self.p['seen'].add('tmdb:3')
        self.p['negative'].add('tmdb:4')
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020,
                               'type': 'series' if i == 5 else 'movie',
                               'why': 'A clever witness changes the course of the trial.'} for i in (3, 4, 5, 1)]
        metas = self.source()
        self.assertEqual([x['id'] for x in metas], ['tmdb:1', 'tmdb:5'])
        self.assertEqual(metas[1]['type'], 'series')
        self.assertTrue(metas[1]['description'].startswith(self.answer['add'][2]['why']))

    def test_quality_tone_family_and_type_filters_apply_to_kept_and_added(self):
        self.answer['keep'] = [0, 1]
        self.answer['why']['1'] = 'A calm investigator patiently follows a trail of evidence.'
        self.answer['add'] = [{'title': 'Synthetic Story 3', 'year': 2020, 'type': 'movie',
                               'why': 'A courtroom puzzle rewards careful attention.'}]
        with patch.object(self.s, 'tone_ok', side_effect=lambda x, *a, **kw: x['id'] != 'tmdb:3'), \
                patch.object(self.s, 'title_allowed', side_effect=lambda k, *a: k != 'tmdb:1'):
            self.assertEqual(self.source([card(1), card(2, _row={'rating': 2, 'votes': 500})]), [])
        reset(self.s)
        self.s._theme_sources.clear()
        with patch.object(self.s, 'resolve_pick', side_effect=self.resolve) as resolve:
            self.answer['add'][0]['type'] = 'tv'
            self.assertIsNone(self.source())
            resolve.assert_not_called()

    def test_blend_uses_row_minmax_and_llm_position_for_kept_and_added(self):
        self.answer['keep'] = [1, 0]
        self.answer['why']['1'] = 'A quiet lawyer protects a reluctant witness.'
        self.answer['add'] = [{'title': 'Synthetic Story 3', 'year': 2020, 'type': 'movie',
                               'why': 'A resourceful advocate exposes a carefully hidden crime.'}]
        self.scores.update({'tmdb:1': 80, 'tmdb:2': 60, 'tmdb:3': 81})
        # LLM positions: 2, 1, 3. Blends: .2, .8952, .8667.
        self.assertEqual([x['id'] for x in self.source(person='ana')], ['tmdb:1', 'tmdb:3', 'tmdb:2'])
        self.s.person_score.assert_called()
        self.s.taste_hint.assert_called_with(self.m, 'ana')

    def test_equal_taste_preserves_llm_order_and_zero_is_a_score(self):
        self.answer['keep'] = [1, 0]
        self.answer['why']['1'] = 'A quiet lawyer protects a reluctant witness.'
        self.theme['kind'] = 'mood'
        self.scores.update({'tmdb:1': 0, 'tmdb:2': 0})
        self.assertEqual([x['id'] for x in self.source()], ['tmdb:2', 'tmdb:1'])

    def test_raw_taste_orders_uncalibrated_and_percentile_ties(self):
        self.answer['keep'] = [0, 1]
        self.answer['why']['1'] = 'A quiet lawyer protects a reluctant witness.'
        self.scores.update({'tmdb:1': .1, 'tmdb:2': .9})
        for pct in (None, 70):
            for person in (None, 'ana'):
                with self.subTest(pct=pct, person=person), patch.object(self.s, 'match_pct', return_value=pct):
                    self.assertEqual([x['id'] for x in self.source(person=person)], ['tmdb:2', 'tmdb:1'])

    def test_resolved_reality_series_is_excluded_from_every_theme_kind(self):
        self.answer['add'] = [{'title': 'Synthetic Story 3', 'year': 2020, 'type': 'series',
                               'why': 'Island survivors learn to trust each other.'}]
        hit, art = self.resolve('tv', 'Synthetic Story 3', 2020, 'de-DE')
        for kind in ('lane', 'mood', 'leap'):
            for genres in ({'genre_ids': [10764]}, {'genres': [{'id': 10764, 'name': 'Reality'}]}):
                self.theme['kind'] = kind
                self.s._theme_sources.clear()
                reset(self.s)
                with self.subTest(kind=kind, genres=genres), \
                        patch.object(self.s, 'resolve_pick', return_value=({**hit, **genres}, art)):
                    self.assertEqual([x['id'] for x in self.source()], ['tmdb:1'])

    def test_same_day_rebuilds_reuse_each_vetted_theme_source(self):
        self.answer['keep'] = list(range(12))
        self.answer['why'] = {str(i): f'A gentle puzzle unfolds around witness number {i}.' for i in range(12)}
        for build in (self.mood, self.leap):
            with self.subTest(build=build.__name__):
                first = build()
                calls = len(self.calls)
                self.m['aff']['k:1'] += 1
                self.s.taste_hint.return_value += '; themes: Warm friendships'
                self.assertEqual(build(), first)
                self.assertEqual(len(self.calls), calls)

    def test_same_day_lane_build_reuses_vetted_additions(self):
        self.answer['keep'], self.answer['why'] = [], {}
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020, 'type': 'movie',
                               'why': f'A patient advocate unravels case number {i}.'} for i in range(1, 21)]
        first = self.build_lane()
        calls = len(self.calls)
        self.s.taste_hint.return_value = 'experiences: Warm friendships'
        self.assertEqual(self.build_lane(), first)
        self.assertEqual(len(self.calls), calls)

    def test_same_day_source_reapplies_filters_and_raw_taste_after_candidates_change(self):
        self.answer['keep'] = [0, 1]
        self.answer['why']['1'] = 'A quiet lawyer protects a reluctant witness.'
        self.assertEqual([x['id'] for x in self.source()], ['tmdb:1', 'tmdb:2'])
        self.scores['tmdb:2'] = 90
        self.s.taste_hint.return_value = 'experiences: Warm friendships'
        self.assertEqual([x['id'] for x in self.source([card(99)])], ['tmdb:2', 'tmdb:1'])
        self.p['seen'].add('tmdb:1')
        self.assertEqual([x['id'] for x in self.source([])], ['tmdb:2'])
        self.assertEqual(len(self.calls), 1)

    def test_same_day_hidden_theme_stays_hidden_when_candidates_change(self):
        self.answer['fits_now'] = False
        self.assertEqual(self.source(), [])
        self.assertEqual(self.source([card(99)]), [])
        self.assertEqual(len(self.calls), 1)

    def test_new_day_or_theme_sources_again_and_failed_attempt_retries(self):
        with patch.object(self.s, 'openrouter', side_effect=TimeoutError()) as failed:
            self.assertIsNone(self.source())
            self.assertEqual(failed.call_count, 2)
        self.assertEqual([x['id'] for x in self.source()], ['tmdb:1'])
        self.assertEqual(len(self.calls), 1)
        with patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 10, 15, 18)):
            self.source()
            self.assertEqual(len(self.calls), 2)
            self.theme.update(key='k:2', name='Island survivors', source=['k:2', 'island'])
            self.source()
            self.assertEqual(len(self.calls), 3)

    def test_offseason_returns_empty_without_resolving(self):
        self.answer['fits_now'] = False
        self.answer['add'] = [{'title': 'Synthetic Story 3', 'year': 2020, 'type': 'movie', 'why': 'A warm winter reunion.'}]
        self.assertEqual(self.source(), [])
        self.s.resolve_pick.assert_not_called()

    def test_bad_json_indices_limits_and_bare_keyword_fail_closed(self):
        bad = [None, {'fits_now': 'yes', 'keep': [], 'add': [], 'why': {}},
               {**self.answer, 'keep': [99]}, {**self.answer, 'keep': [-1]},
               {**self.answer, 'keep': [0, 0]}, {**self.answer, 'why': {}},
               {**self.answer, 'add': [{'title': 'Synthetic Story 3', 'year': 2020, 'type': 'movie',
                                      'why': 'A warm reunion comforts everyone.'}] * 31}]
        for answer in bad:
            with self.subTest(answer=answer):
                reset(self.s)
                self.answer = answer
                self.assertIsNone(self.source())
        for reason in ('lawyer', 'Courtroom stories', 'Gentle puzzles'):
            reset(self.s)
            self.answer = {'fits_now': True, 'months': 'any', 'keep': [0], 'add': [], 'why': {'0': reason}}
            self.assertIsNone(self.source())

    def build_lane(self):
        with patch.object(self.s, 'lane_titles', return_value=[('tmdb:99', 'movie', card(99))]), \
                patch.object(self.s, 'lane_title', return_value='Courtroom stories'):
            return self.s.lanes_feed('de-DE')

    def test_lane_can_build_from_additions_and_added_own_pick_survives_related_check(self):
        self.answer['keep'], self.answer['why'] = [], {}
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020, 'type': 'movie',
                               'why': f'A patient advocate unravels case number {i}.'} for i in range(1, 21)]
        out = self.build_lane()
        cid = next(iter(out['_names']))
        self.assertEqual(len(out[cid]['tv']), 20)
        self.s._feeds[cid, 'de-DE'] = {'metas': out[cid], 'names': out['_names'][cid]}
        with patch.object(self.s, 'feed', return_value=self.s._feeds[cid, 'de-DE']), \
                patch.object(self.s, 'lane_member', return_value=False):
            self.assertTrue(self.s.row_related(out[cid]['tv'][0], cid, 'de-DE'))
            self.assertFalse(self.s.row_related(card(99), cid, 'de-DE'))

    def test_lane_failure_hides_unvetted_and_reuses_only_same_vetted_lane(self):
        s = self.s
        xs = [card(i, _row={**card(i)['_row'], 'theme': 'k:1'}) for i in range(1, 21)]
        s._feeds[s.LANES[0], 'de-DE'] = {'metas': {'tv': xs}, 'names': {'lane': 'k:2'}}
        for err in (TimeoutError(), ValueError(), s.BudgetExceeded()):
            with patch.object(s, 'openrouter', side_effect=err):
                reset(s)
                self.assertEqual(self.build_lane()['_names'], {})
                s._feeds[s.LANES[0], 'de-DE']['names']['lane'] = 'k:1'
                self.p['seen'].add('tmdb:1')
                reset(s)
                out = self.build_lane()
                self.assertEqual(len(out[s.LANES[0]]['tv']), 19)
                self.assertNotIn('tmdb:1', [x['id'] for x in out[s.LANES[0]]['tv']])
                s._feeds[s.LANES[0], 'de-DE']['names']['lane'] = 'k:2'
        s._feeds[s.LANES[0], 'de-DE'] = {'metas': {'tv': [card(i) for i in range(1, 21)]}, 'names': {'lane': 'k:1'}}
        with patch.object(s, 'openrouter', side_effect=TimeoutError()):
            self.assertEqual(self.build_lane()['_names'], {})

    def mood(self):
        with patch.object(self.s, 'MOODS', {'cozy': ('Gentle evenings', ['cozy_case'], '0123456')}), \
                patch.object(self.s, 'pick_moods', return_value=['cozy']), \
                patch.object(self.s, 'feeling_pool', return_value=[(x['id'], 'movie', x) for x in [card(i) for i in range(1, 21)]]):
            return self.s.mood_feed('de-DE')

    def leap(self):
        self.p['entries'] = [dict(key='tmdb:90', kind='movie', title='Synthetic Seed', signal='loved',
                                  by=['ana', 'ben', 'together'], weight=4)]
        with patch.object(self.s, 'adult_people', return_value=('ana', 'ben')), \
                patch.object(self.s, 'feeling_pool', return_value=[(x['id'], 'movie', x) for x in [card(i) for i in range(1, 21)]]):
            return self.s.leap_feed('de-DE')

    def test_mood_and_leap_failure_keep_tag_picks_and_false_hides_this_build(self):
        s = self.s
        for build, cid in ((self.mood, s.MOODS_ROWS[0]), (self.leap, s.rid('leap'))):
            for err in (TimeoutError(), ValueError(), s.BudgetExceeded()):
                reset(s)
                with patch.object(s, 'openrouter', side_effect=err):
                    self.assertEqual(len(build()[cid]['tv']), 20)
            reset(s)
            self.answer['fits_now'] = False
            result = build()
            self.assertEqual(result.get(cid, {}).get('tv', []), [])
            self.assertNotIn(cid, result.get('_names', {}))

    def test_mood_and_leap_use_vetted_reasons_and_new_sources(self):
        self.answer['keep'] = list(range(12))
        self.answer['why'] = {str(i): f'A gentle puzzle unfolds around witness number {i}.' for i in range(12)}
        for build, cid in ((self.mood, self.s.MOODS_ROWS[0]), (self.leap, self.s.rid('leap'))):
            reset(self.s)
            result = build()
            self.assertEqual(len(result[cid]['tv']), 12)
            self.assertTrue(result[cid]['tv'][0]['description'].startswith(self.answer['why']['0']))

    def test_mood_additions_respect_earlier_rows_and_tired_titles(self):
        s = self.s
        self.answer['keep'], self.answer['why'] = [], {}
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020, 'type': 'movie',
                               'why': f'A gentle puzzle unfolds around neighbor number {i}.'} for i in range(1, 26)]
        with patch.object(s, 'MOODS', {'cozy': ('Gentle evenings', ['cozy_case'], '0123456'),
                                      'heart': ('Warm friendships', ['found_family'], '0123456')}), \
                patch.object(s, 'pick_moods', return_value=['cozy', 'heart']), \
                patch.object(s, 'feeling_pool', return_value=[('tmdb:99', 'movie', card(99))]), \
                patch.object(s, 'tired_keys', return_value={'tmdb:25': 'Synthetic Story 25'}):
            out = s.mood_feed('de-DE')
        self.assertEqual(len(out[s.MOODS_ROWS[0]]['tv']), 24)
        self.assertNotIn('tmdb:25', [x['id'] for x in out[s.MOODS_ROWS[0]]['tv']])
        self.assertEqual(out[s.MOODS_ROWS[1]]['tv'], [])

    def test_lane_offseason_never_reuses_previous_vetted_build(self):
        s = self.s
        xs = [card(i, _row={**card(i)['_row'], 'theme': 'k:1'}) for i in range(1, 21)]
        s._feeds[s.LANES[0], 'de-DE'] = {'metas': {'tv': xs}, 'names': {'lane': 'k:1'}}
        self.answer.update(fits_now=False, keep=[], why={})
        self.assertEqual(self.build_lane()['_names'], {})

    def test_theme_job_stamps_invalidate_older_sources(self):
        stamps = {name: stamp for name, _, stamp, _, _ in self.s.job_specs(self.s.local_now(), {})}
        for name in ('lanes', 'moods', 'leap'):
            self.assertTrue(stamps[name].startswith('source1/'), stamps[name])

    def test_new_lane_batches_one_naming_call_after_sourcing(self):
        s = self.s
        self.m['aff'] = {'k:1': 1}
        self.answer['keep'], self.answer['why'] = [], {}
        self.answer['add'] = [{'title': f'Synthetic Story {i}', 'year': 2020, 'type': 'movie',
                               'why': f'A patient advocate unravels case number {i}.'} for i in range(1, 21)]
        with patch.object(s, 'lane_titles', return_value=[('tmdb:99', 'movie', card(99))]), \
                patch.object(s, 'llm_text') as naming:
            out = s.lanes_feed('de-DE')
        naming.assert_not_called()
        self.assertEqual([c[4] for c in self.calls], ['theme_source', 'name_lanes'])
        self.assertEqual(len(out[s.LANES[0]]['tv']), 20)

    def test_leap_filters_use_this_builds_seed(self):
        s = self.s
        theme = {'kind': 'leap', 'name': 'Same feeling as Synthetic Seed',
                 'key': 'tmdb:90', 'source': ['Synthetic Seed', 'found_family: 3']}
        with patch.object(s, 'row_animation', return_value=False), \
                patch.object(s, 'frec', side_effect=lambda key, *a: {'g': ['Family']} if key in ('tmdb:1', 'tmdb:90') else {}):
            metas = s.themed_row(self.m, self.p, theme, [card(1)], 'de-DE', s.rid('leap'), seed='tmdb:90')
        self.assertEqual([x['id'] for x in metas], ['tmdb:1'])

    def test_budget_gate_makes_no_paid_call(self):
        self.s._decide_over.add('theme_source')
        self.assertIsNone(self.source())
        self.assertEqual(self.calls, [])

    def test_decision_fixture_contract(self):
        with patch.object(self.s, 'openrouter', self.transport):
            check_contract(self, self.s, 'theme_source')


if __name__ == '__main__':
    unittest.main()
