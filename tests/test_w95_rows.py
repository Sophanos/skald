"""Row quality regressions with synthetic titles and offline transports."""
from contextlib import ExitStack
import datetime
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server


def title(i, **fields):
    return {'id': i, 'title': f'Synthetic Movie {i}', 'vote_count': 500,
            'vote_average': 8, 'popularity': 1000-i, 'poster_path': '/synthetic.jpg',
            'release_date': '2026-10-01', **fields}


class W95Rows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))

    def candidates(self, recs, trends, scores, model):
        s = self.s
        seeds = [{'key': f'tmdb:{i}', 'id': i, 'kind': 'movie', 'title': f'Synthetic Seed {i}'} for i in (9000, 9001)]
        def tmdb(path, **params):
            return {'results': recs[:15] if path == 'movie/9000/recommendations' else
                    recs[15:] if path == 'movie/9001/recommendations' else
                    trends if path == 'trending/movie/week' else []}
        def rank(m, items, **params):
            return sorted([(scores.get(k), [], k, kind, payload) for k, kind, payload in items],
                          key=lambda x: (x[0] is None, -(x[0] or 0)))
        with ExitStack() as stack:
            for name, value in {'get_model': model, 'pool_seeds': seeds, 'tired_keys': {},
                                'match_pct': None, 'reasons': [], 'genres': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'with_match', side_effect=lambda x: x))
            stack.enter_context(patch.object(s, 'tmdb', side_effect=tmdb))
            stack.enter_context(patch.object(s, 'rank_by_taste', side_effect=rank))
            return s.candidate_pool({'entries': [], 'seen': set(), 'negative': set()}, metas=True)

    def test_trending_requires_median_recommendation_score(self):
        recs = [title(i) for i in range(1, 5)]
        trends = [title(i) for i in (1, 5, 6, 7, 8)]
        scores = {'tmdb:1': -4, 'tmdb:2': 2, 'tmdb:3': 6, 'tmdb:4': 8,
                  'tmdb:5': 3, 'tmdb:6': 4, 'tmdb:7': 9, 'tmdb:8': None}
        cards = self.candidates(recs, trends, scores, {'vec': None})
        self.assertEqual([x['id'] for x in cards],
                         ['tmdb:7', 'tmdb:4', 'tmdb:3', 'tmdb:6', 'tmdb:2', 'tmdb:1'])

    def test_trending_without_scored_recommendations_is_not_offered(self):
        self.assertEqual([x['id'] for x in self.candidates([title(1)], [title(2)], {}, {'vec': None})], ['tmdb:1'])

    def test_trending_without_model_only_fills_sparse_pool(self):
        recs = [title(i) for i in range(1, 21)]
        trends = [title(21), title(22)]
        self.assertEqual([x['id'] for x in self.candidates(recs, trends, {}, None)],
                         [f'tmdb:{i}' for i in (*range(1, 7), *range(16, 21))] + ['tmdb:21', 'tmdb:22'])
        self.assertEqual([x['id'] for x in self.candidates(recs[:19], trends, {}, None)],
                         [f'tmdb:{i}' for i in (*range(1, 7), *range(16, 20))] + ['tmdb:21', 'tmdb:22'])
        seeds = [{'key': f'tmdb:{i}', 'id': i, 'kind': 'movie', 'title': f'Synthetic Seed {i}'}
                 for i in range(9000, 9004)]
        with patch.object(self.s, 'pool_seeds', return_value=seeds), \
                patch.object(self.s, 'get_model', return_value=None), patch.object(self.s, 'tired_keys', return_value={}), \
                patch.object(self.s, 'tmdb', side_effect=lambda path, **params: {'results':
                    [title(100 + i) for i in range(2)] if path.startswith('trending/') else
                    [title(int(path.split('/')[1]) * 100 + i) for i in range(6)]}), \
                patch.object(self.s, 'genres', return_value={}), patch.object(self.s, 'with_match', side_effect=lambda x: x):
            cards = self.s.candidate_pool({'entries': [], 'seen': set(), 'negative': set()}, metas=True)
        self.assertEqual(len(cards), 24)
        self.assertNotIn('tmdb:100', [x['id'] for x in cards])

    def test_ai_picks_and_refills_share_served_trending_cap(self):
        s, cid = self.s, self.s.rid('foryou')
        recs, trends = [title(i) for i in range(1, 5)], [title(i) for i in (1, 5, 6, 7, 8, 9)]
        seed = {'key': 'tmdb:9000', 'id': 9000, 'kind': 'movie', 'stype': 'movie',
                'title': 'Synthetic Seed', 'weight': 1, 'age': 1, 'signal': 'watched'}
        p = {'entries': [seed], 'seen': set(), 'negative': set(), 'searches': []}
        def tmdb(path, **params):
            return {'results': recs if path.endswith('/recommendations') else
                    trends if path == 'trending/movie/week' else []}
        picks = [(title(i)['title'], '2026', 'movie', 'A quiet mystery') for i in (5, 1, 6, 2, 7, 3, 8, 4)]
        with ExitStack() as stack:
            for name, value in {'get_model': None, 'top_seeds': [seed], 'tired_keys': {},
                                'hit_titles': [], 'genres': {}, 'read_taste': {}, 'profile_text': '',
                                'row_eligible': True, 'row_member': False, 'frec': {},
                                'llm_sections': {'FOR YOU': picks}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'adult_profile', return_value=p))
            stack.enter_context(patch.object(s, 'row_enabled', side_effect=lambda key: key == cid))
            stack.enter_context(patch.object(s, 'tmdb', side_effect=tmdb))
            stack.enter_context(patch.object(s, 'resolve_pick', side_effect=lambda kind, name, *a: (title(int(name.rsplit(' ', 1)[1])), {})))
            for name in ('balance_row', 'polish_row', 'rotate_row'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            rows = s.personal_feed('en-US')
            refill = s.row_meta(title(9), 'movie', 'movie', {}, {}, lang='en-US')
            rows[cid]['tv'] = [x for x in rows[cid]['tv'] if x['id'] != refill['id']]
            stack.enter_context(patch.object(s, 'feed', side_effect=lambda key, *a: {
                'metas': rows[key], 'names': rows.get('_names', {}).get(key, {})} if key == cid else {}))
            stack.enter_context(patch.object(s, 'fill_row', side_effect=lambda xs, *a, **kw: xs + [refill]))
            served = s.served_row(cid, 'tv', 'en-US', p, set(), (set(), set()))
        self.assertEqual([x['id'] for x in served], ['tmdb:5', 'tmdb:1', 'tmdb:2', 'tmdb:6', 'tmdb:3', 'tmdb:4'])

    def test_low_vote_trending_series_cannot_bypass_ai_cap(self):
        s, cid = self.s, self.s.rid('foryou')
        p = {'entries': [], 'seen': set(), 'negative': set(), 'searches': [], 'likes': 'Synthetic mystery'}
        series = [title(i, name=f'Synthetic Series {i}', vote_count=75) for i in (1, 2, 3)]
        picks = [(it['name'], '2026', 'series', 'A quiet mystery') for it in series]
        with ExitStack() as stack:
            for name, value in {'get_model': None, 'top_seeds': [], 'tired_keys': {}, 'hit_titles': [],
                                'genres': {}, 'read_taste': {}, 'profile_text': '', 'frec': {},
                                'llm_sections': {'FOR YOU': picks}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'adult_profile', return_value=p))
            stack.enter_context(patch.object(s, 'row_enabled', side_effect=lambda key: key == cid))
            stack.enter_context(patch.object(s, 'tmdb', side_effect=lambda path, **kw: {
                'results': series if path == 'trending/tv/week' else []}))
            stack.enter_context(patch.object(s, 'resolve_pick', side_effect=lambda kind, name, *a: (
                next(it for it in series if it['name'] == name), {})))
            for name in ('balance_row', 'polish_row', 'rotate_row', 'fill_row'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            rows = s.personal_feed('en-US')
            stack.enter_context(patch.object(s, 'feed', side_effect=lambda key, *a: {
                'metas': rows[cid], 'names': rows.get('_names', {}).get(cid, {})} if key == cid else {}))
            served = s.served_row(cid, 'tv', 'en-US', p, set(), (set(), set()))
        self.assertEqual([x['id'] for x in served], ['tmdb:1', 'tmdb:2'])

    def movie_feed(self, key, per_page=20, release_dates=None):
        s, pages, lookups = self.s, [], []
        today = datetime.datetime(2026, 10, 13)
        country = s.rating_country(s.CONFIG['region']['country'])
        def tmdb(path, **params):
            if path.endswith('/release_dates'):
                i = int(path.split('/')[1])
                lookups.append(i)
                return {'results': release_dates(i) if release_dates else [
                    {'iso_3166_1': country, 'release_dates': [{'type': 4, 'release_date': '2026-10-01'}]}]}
            if path == 'genre/movie/list':
                return {'genres': []}
            page = params.get('page', 1)
            pages.append(page)
            return {'results': [title(i) for i in range((page-1)*per_page+1, page*per_page+1)]}
        with patch.object(s, 'tmdb', side_effect=tmdb), patch.object(s, 'local_now', return_value=today):
            feed = getattr(s, key + '_feed')('en-US')
        return feed['movie'], pages, lookups

    def test_movie_rows_refill_after_seen_masks_and_above(self):
        s = self.s
        for key in ('digital', 'toprated'):
            with self.subTest(key=key):
                cards, pages, _ = self.movie_feed(key)
                with patch.object(s, 'row_list', return_value=cards), patch.object(s, 'frec', return_value={}):
                    served = s.served_row(s.rid(key), 'movie', 'en-US', {'seen': {'tmdb:1', 'tmdb:2'}},
                                          {'tmdb:4', 'tmdb:5'}, ({'tmdb:1', 'tmdb:2', 'tmdb:3'}, set(), {'tmdb:6'}))
                self.assertEqual([x['id'] for x in served], [f'tmdb:{i}' for i in range(7, 27)])
                self.assertEqual(pages, [1, 2, 3])

    def test_movie_rows_drop_profile_history_and_refill_to_twenty(self):
        s = self.s
        for key in ('digital', 'toprated'):
            with self.subTest(key=key):
                cards, _, _ = self.movie_feed(key)
                with patch.object(s, 'row_list', return_value=cards), patch.object(s, 'frec', return_value={}):
                    served = s.served_row(s.rid(key), 'movie', 'en-US',
                                          {'seen': {'tmdb:1'}, 'negative': {'tmdb:2'}},
                                          set(), (set(), set()))
                self.assertEqual([x['id'] for x in served], [f'tmdb:{i}' for i in range(3, 23)])

    def test_digital_stops_release_lookups_at_sixty_qualifying_titles(self):
        cards, _, lookups = self.movie_feed('digital', per_page=25)
        self.assertEqual(len(cards), 60)
        self.assertEqual(len(lookups), 60)

    def test_digital_excludes_old_premieres_in_any_country(self):
        s = self.s
        cutoff = datetime.date(2026, 10, 13) - datetime.timedelta(days=60+365)
        country = s.rating_country(s.CONFIG['region']['country'])
        def releases(i):
            dates = [{'type': 4, 'release_date': '2026-10-01'}]
            if i in (1, 2, 3):
                foreign = [{'type': i, 'release_date': (cutoff-datetime.timedelta(days=1)).isoformat()},
                           {'type': 3, 'release_date': '2026-10-01'}]
            else:
                foreign = {4: [{'type': 1, 'release_date': cutoff.isoformat()}],
                           5: [{'type': 3, 'release_date': (cutoff+datetime.timedelta(days=1)).isoformat()}],
                           6: [{'type': 5, 'release_date': '1972-01-01'}],
                           7: [{'type': 2, 'release_date': 'invalid'}]}.get(i, [])
            return [{'iso_3166_1': country, 'release_dates': dates},
                    {'iso_3166_1': 'ZZ', 'release_dates': foreign}]
        cards, _, _ = self.movie_feed('digital', release_dates=releases)
        self.assertEqual([x['id'] for x in cards], [f'tmdb:{i}' for i in range(4, 61)])

    def test_dangling_or_emptied_pick_reasons_are_dropped(self):
        s = self.s
        for why in ('For your.', 'For your.x_crime.', 'The.', 'A.', 'An.', 'Your.', 'Their.',
                    'Our.', 'His.', 'Her.', 'Its.', 'My.', 'Their quiet mystery for the.', '...',
                    'Word '*16 + 'for your next mystery', 'For your…', 'The…', '…'):
            with self.subTest(reason=why):
                self.assertEqual(s.clean_note(why), '')
                with patch.object(s, 'resolve_pick', return_value=(title(1, overview='Synthetic overview.'), {})), \
                        patch.object(s, 'genres', return_value={}):
                    rows = s.picks_to_rows([('Synthetic Movie 1', '2026', 'movie', why)], 'en-US', set())
                self.assertEqual(rows['movie'][0]['description'], 'Synthetic overview.')
        for why in ('A quiet mystery', 'Because you loved Synthetic Seed', 'For your quiet evenings'):
            self.assertEqual(s.clean_note(why), why + '.')

    def test_serving_skips_reason_that_becomes_dangling(self):
        s = self.s
        cards = [s.row_meta(title(i), 'movie', 'movie', {}, {}, lang='en-US') for i in (1, 2, 3)]
        cards[2]['description'] = 'For your loved Synthetic Seed · A quiet mystery · Synthetic overview.'
        cards[2]['_row']['overview'] = 'Synthetic overview.'
        with patch.object(s, 'frec', return_value={}):
            self.assertEqual(s.human_cards(cards)[2]['description'], 'A quiet mystery. · Synthetic overview.')

    def test_warm_restart_builds_model_after_first_feed_pass(self):
        s = self.s
        p = {'hash': 'synthetic', 'entries': [], 'seen': set()}
        cid = s.rid('foryou')
        cards = [s.row_meta(title(i), 'movie', 'movie', {}, {}, lang='en-US') for i in range(1, 8)]
        model = {'aff': {}, 'cal': 80, 'taste_line': ''}
        with ExitStack() as stack:
            for name, value in {'_feed_started': False, '_defer_enrichment': False,
                                '_profiles': {'household': p}, '_model': {'sig': None, 'm': None},
                                '_feeds': {(cid, 'en-US'): {'stamp': 'cached', 'metas': {'tv': cards}}}}.items():
                stack.enter_context(patch.object(s, name, value))
            for name, value in {'refresh_profiles': {'household': 'synthetic'}, 'job_specs': [],
                                'frec': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            for name in ('pin_decision', 'taste_ring_note', 'save_feeds'):
                stack.enter_context(patch.object(s, name))
            build = stack.enter_context(patch.object(s, 'build_model', return_value=model))
            s.feed_pass(s.local_now(), {})
            build.assert_not_called()
            s.finish_cold_enrichment(s.local_now(), {})
            self.assertIs(s.get_model(False), model)
            self.assertEqual(s.row_list(cid, 'tv', 'en-US'), cards)
            s.feed_pass(s.local_now(), {})
            s.finish_cold_enrichment(s.local_now(), {})
            build.assert_called_once_with(p)

