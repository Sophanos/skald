"""Pool spread with synthetic titles and fake transports only."""
import ast
from collections import Counter
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server


def seed(i, **fields):
    return {'key': f'tmdb:{i}', 'id': i, 'title': f'Invented seed {chr(64 + i)}',
            'kind': 'movie', 'stype': 'movie', 'weight': 1, 'signal': 'watched',
            'age': i, 'who': 'ana', 'item': {'at': 1}, **fields}


def title(i, votes=500):
    return {'id': i, 'title': f'Invented title {i}', 'vote_count': votes,
            'vote_average': 8, 'release_date': '2026-01-01', 'poster_path': '/fake.jpg'}


class PoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.ensure_facets = staticmethod(cls.s.ensure_facets)
        cls.ensure_emb = staticmethod(cls.s.ensure_emb)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'ensure_emb'))
        self.enterContext(patch.object(self.s, 'ensure_facets'))
        self.enterContext(patch.object(self.s, 'openrouter', side_effect=AssertionError('LLM forbidden')))

    def profile(self, entries):
        return {'entries': entries, 'seen': {e['key'] for e in entries}, 'negative': set()}

    def select(self, entries, facts=None, negative=()):
        p = self.profile(entries)
        p['negative'] = set(negative)
        with patch.object(self.s, 'frec', side_effect=lambda key, kind: (facts or {}).get(key, {})):
            report = {}
            chosen = self.s.pool_seeds(p, report)
        return chosen, report

    def test_duplicate_franchise_and_universe_are_not_seeds(self):
        entries = [seed(i) for i in range(1, 5)]
        with patch.object(self.s, 'franchise_key', side_effect=lambda meta: 'saga' if meta['id'] in ('tmdb:1', 'tmdb:2') else meta['id']), \
                patch.object(self.s, 'universe_key', side_effect=lambda meta: 'world' if meta['id'] in ('tmdb:3', 'tmdb:4') else None):
            chosen, report = self.select(entries)
        self.assertEqual([e['id'] for e in chosen], [1, 3])
        self.assertEqual([e['reason'] for e in report['seeds'] if not e['chosen']],
                         ['duplicate franchise', 'duplicate universe'])

    def test_cached_collection_excludes_differently_named_seed(self):
        entries = [seed(1, title='Azure Lantern'), seed(2, title='Scarlet Gate')]
        chosen, report = self.select(entries, {'tmdb:1': {'collection': 42}, 'tmdb:2': {'collection': 42}})
        self.assertEqual([e['id'] for e in chosen], [1])
        self.assertEqual(report['seeds'][1]['reason'], 'duplicate franchise')

    def test_similar_areas_wait_for_distinct_areas(self):
        entries = [seed(i) for i in range(1, 19)]
        facts = {e['key']: {'x': {f'area_{1 if e["id"] == 2 else e["id"]}': 1}} for e in entries}
        chosen, report = self.select(entries, facts)
        self.assertEqual(len(chosen), 16)
        self.assertNotIn(2, [e['id'] for e in chosen])
        self.assertEqual(next(e['reason'] for e in report['seeds'] if e['key'] == 'tmdb:2'), 'similar area')

    def test_sparse_similar_areas_can_fill_but_franchises_cannot(self):
        chosen, report = self.select([seed(1), seed(2)], {'tmdb:1': {'x': {'quiet': 1}}, 'tmdb:2': {'x': {'quiet': 1}}})
        self.assertEqual(len(chosen), 2)
        self.assertEqual(report['seeds'][1]['reason'], 'similar area fallback')

    def test_negative_and_undated_backfill_excluded_except_loved(self):
        entries = [seed(1), seed(2, item={'backfilled': True}),
                   seed(3, item={'_summary': {'origin': 'history.json'}}),
                   seed(4, signal='loved', item={'backfilled': True}), seed(5)]
        chosen, report = self.select(entries, negative={'tmdb:5'})
        self.assertEqual({e['id'] for e in chosen}, {1, 4})
        self.assertEqual(Counter(e['reason'] for e in report['seeds'] if not e['chosen']),
                         {'backfill': 2, 'negative': 1})

    def build(self, entries, responses, model=True, scores=None, facts=None):
        report, paths = {}, []
        def tmdb(path, **params):
            paths.append(path)
            return {'results': responses.get(path, [])}
        def rank(m, items, **params):
            return sorted([((scores or {}).get(key, float(it['id'])), [], key, kind, (it, e))
                           for key, kind, (it, e) in items], reverse=True)
        with ExitStack() as stack:
            for name, value in {'get_model': {'vec': None} if model else None,
                                'tired_keys': {}, 'match_pct': None, 'reasons': [], 'genres': {}}.items():
                stack.enter_context(patch.object(self.s, name, return_value=value))
            stack.enter_context(patch.object(self.s, 'frec', side_effect=lambda key, kind: (facts or {}).get(key, {})))
            for name in ('with_match',):
                stack.enter_context(patch.object(self.s, name, side_effect=lambda x: x))
            for name in ('quality_ok', 'tone_ok'):
                stack.enter_context(patch.object(self.s, name, return_value=True))
            stack.enter_context(patch.object(self.s, 'tmdb', side_effect=tmdb))
            stack.enter_context(patch.object(self.s, 'rank_by_taste', side_effect=rank))
            cards = self.s.candidate_pool(self.profile(entries), metas=True, report=report)
        return cards, report, paths

    def test_no_model_trending_fills_pool_after_seed_cap(self):
        cards, report, _ = self.build([seed(1)], {
            'movie/1/recommendations': [title(i) for i in range(101, 116)],
            'movie/1/similar': [title(i) for i in range(116, 121)],
            'trending/movie/week': [title(201), title(202)]}, model=False)
        self.assertEqual([c['id'] for c in cards], [f'tmdb:{i}' for i in range(101, 107)] + ['tmdb:201', 'tmdb:202'])
        self.assertEqual(report['pool_size'], 8)

    def test_report_counts_cached_collections_as_one_franchise(self):
        cards, report, _ = self.build([seed(1)], {
            'movie/1/recommendations': [{**title(101), 'title': 'Azure Lantern'},
                                        {**title(102), 'title': 'Scarlet Gate'}]}, model=False,
            facts={'tmdb:101': {'collection': 84}, 'tmdb:102': {'collection': 84}})
        self.assertEqual(len(cards), 2)
        self.assertEqual(report['distinct_franchises'], 1)

    def test_real_ranking_bounds_details_and_enriches_next_candidates_on_later_builds(self):
        s, entries, paths = self.s, [seed(i) for i in range(1, 17)], []
        responses = {f'movie/{e["id"]}/{endpoint}': [
            {**title(e['id'] * 100 + offset + i), 'genre_ids': [18 if i < 10 else 35],
             'vote_average': 7 if i < 10 else 10}
            for i in range(20)] for e in entries for endpoint, offset in (('recommendations', 0), ('similar', 20))}
        responses.update({f'trending/{kind}/week': [{**title(2000 + offset + i), 'genre_ids': [18]}
                                                 for i in range(20)] for kind, offset in (('movie', 0), ('tv', 20))})
        model = {'vec': None, 'vbase': 0, 'aff': {'g:Comedy': 1, 'g:Drama': -1, 'd:7': 2},
                 'bscale': 1, 'names': {}, 'nexp': {}, 'ent': {}, 'cal': False}
        items = {('movie' if path.startswith('movie/') or path == 'trending/movie/week' else 'tv', it['id']): it
                 for path, results in responses.items() for it in results}
        def tmdb(path, **params):
            paths.append(path)
            if path in responses:
                return {'results': responses[path]}
            kind, tid = path.split('/')
            it = items[kind, int(tid)]
            return {**it, 'genres': [{'id': g, 'name': s.TMDB_GENRES['movie'][g]} for g in it['genre_ids']],
                    'original_language': 'en', 'overview': 'An invented story.'}
        def openrouter(path, body, *args):
            self.assertEqual(path, '/api/v1/embeddings')
            return {'data': [{'index': i, 'embedding': [1.] + [0.] * (s.EMB_DIM - 1)}
                             for i in range(len(body['input']))]}
        cached = {'kind': 'movie', 'title': 'Invented cached candidate', 'd': [[7, 'Invented Maker']],
                  'g': ['Drama'], 'o': 'An invented story.'}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(s._facets, {}, clear=True))
            for store in (s._emb, s._emb_kinds):
                stack.enter_context(patch.dict(store, {}, clear=True))
            for name, value in {'get_model': model, 'tired_keys': {}, 'genres': {},
                                'llm_text': '', 'reasons': []}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'ensure_facets', self.ensure_facets))
            stack.enter_context(patch.object(s, 'ensure_emb', self.ensure_emb))
            stack.enter_context(patch.object(s, 'openrouter', side_effect=openrouter))
            stack.enter_context(patch.object(s, 'OR_KEY', 'fake'))
            stack.enter_context(patch.object(s, '_emb_model', s.EMB_MODEL))
            stack.enter_context(patch.object(s, 'tmdb', side_effect=tmdb))
            stack.enter_context(patch.object(s, 'with_match', side_effect=lambda x: x))
            details = []
            for build in range(2):
                if build:
                    s._facets['tmdb:100'] = cached
                paths.clear()
                cards = s.candidate_pool(self.profile(entries), metas=True)
                lists = [p for p in paths if p in responses]
                details.append([p for p in paths if p not in responses])
                self.assertEqual(len(lists), 34)
                self.assertEqual(len(details[-1]), 50)
                self.assertEqual(len(paths), 84)
                self.assertEqual(cards[0]['id'], 'tmdb:100' if build else 'tmdb:110')
            best = [f'movie/{it["id"]}' for path, results in responses.items() for it in results
                    if it['genre_ids'] == [35]]
            self.assertEqual(set(details[0]), set(best[:50]))
            self.assertEqual(set(details[1]), set(best[50:100]))
            self.assertTrue(set(details[0]).isdisjoint(details[1]))
            for (kind, tid), it in items.items():
                s._facets[s.fkey(f'tmdb:{tid}', kind)] = {**cached, 'kind': kind, 'title': it['title']}
            paths.clear()
            s.candidate_pool(self.profile(entries), metas=True)
            self.assertEqual(len(paths), 34)
            with patch.object(s, '_emb_model', 'previous'), \
                    patch.object(s._outer, 'submit', side_effect=AssertionError('pool started global backfill')) as submit:
                s.candidate_pool(self.profile(entries), metas=True)
                submit.assert_not_called()
            s._facets.clear()
            paths.clear()
            def unavailable(path, **params):
                if path in responses:
                    return tmdb(path, **params)
                paths.append(path)
                raise LookupError('synthetic detail unavailable')
            with patch.object(s, 'tmdb', side_effect=unavailable):
                s.candidate_pool(self.profile(entries), metas=True)
            self.assertEqual(len(paths), 84)

    def test_endpoints_merge_dedup_vote_floor_and_cap_after_rank(self):
        recs = [title(i) for i in range(101, 111)]
        similar = [title(101), title(111), title(112, 99)]
        cards, report, paths = self.build([seed(1)], {
            'movie/1/recommendations': recs, 'movie/1/similar': similar})
        self.assertEqual(set(paths), {'movie/1/recommendations', 'movie/1/similar',
                                     'trending/movie/week', 'trending/tv/week'})
        self.assertEqual([c['id'] for c in cards], [f'tmdb:{i}' for i in range(111, 105, -1)])
        self.assertEqual(report['seeds'][0]['candidates'], 11)
        self.assertEqual(report['seeds'][0]['kept'], 6)
        self.assertEqual(report['cap_removed'], 5)

    def test_trending_median_uses_all_ranked_recommendations_before_cap(self):
        cards, _, _ = self.build([seed(1)], {
            'movie/1/recommendations': [title(i) for i in range(101, 111)],
            'trending/movie/week': [title(201), title(202), title(120, 99)]},
            scores={'tmdb:201': 105.5, 'tmdb:202': 104})
        self.assertIn('tmdb:201', [c['id'] for c in cards])
        self.assertNotIn('tmdb:202', [c['id'] for c in cards])
        self.assertNotIn('tmdb:120', [c['id'] for c in cards])

    def test_worst_case_is_34_list_calls_and_no_seed_exceeds_cap(self):
        entries = [seed(i) for i in range(1, 19)]
        responses = {f'movie/{e["id"]}/recommendations': [title(e['id'] * 100 + i) for i in range(30, 50)] for e in entries}
        cards, report, paths = self.build(entries, responses)
        self.assertEqual(len(paths), 34)
        self.assertEqual(report['distinct_seeds'], 16)
        self.assertEqual(report['pool_size'], 96)
        self.assertEqual(len(cards), 96)
        self.assertTrue(all(e['kept'] <= 6 for e in report['seeds']))

    def test_shared_tv_and_old_favourites_keep_reserved_places(self):
        entries = [seed(i, title=f'Invented anchor {chr(64 + i)}') for i in range(1, 21)]
        entries += [seed(i, age=400, signal='loved', title=f'Invented favourite {chr(64 + i - 20)}') for i in range(21, 24)]
        entries += [seed(i, age=200, who='tv', title=f'Invented shared {chr(64 + i - 23)}') for i in (24, 25)]
        chosen, report = self.select(entries)
        self.assertEqual(len(chosen), 16)
        self.assertTrue({1, 21, 22, 23, 24, 25} <= {e['id'] for e in chosen})
        self.assertEqual(Counter(e['reason'] for e in report['seeds'] if e['chosen']),
                         {'recent': 11, 'favourite': 3, 'shared TV': 2})

    def test_prompt_candidate_counts_stay_bounded(self):
        entries = [seed(i) for i in range(1, 19)]
        responses = {f'movie/{e["id"]}/recommendations': [title(e['id'] * 100 + i) for i in range(30, 50)] for e in entries}
        for model, limit in ((None, 40), ({'vec': None}, 50)):
            with self.subTest(model=bool(model)), \
                    patch.object(self.s, 'get_model', return_value=model), \
                    patch.object(self.s, 'tmdb', side_effect=lambda path, **params: {'results': responses.get(path, [])}), \
                    patch.object(self.s, 'tired_keys', return_value={}), \
                    patch.object(self.s, 'rank_by_taste', side_effect=lambda m, items, **kw: [(1, [], *it) for it in items]), \
                    patch.object(self.s, 'match_pct', return_value=None), patch.object(self.s, 'reasons', return_value=[]):
                self.assertEqual(len(self.s.candidate_pool(self.profile(entries))), limit)

    def test_pool_calls_belong_to_background_personal_builder(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text())
        callers = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                   and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == 'candidate_pool'
                           for c in ast.walk(node))]
        self.assertEqual(callers, ['personal_feed'])
        with patch.object(self.s, 'candidate_pool', side_effect=AssertionError('request built pool')):
            self.assertEqual(self.s.feed(self.s.rid('foryou'), 'en-US'), {})
            self.s.served_row(self.s.rid('foryou'), 'tv', 'en-US', self.profile([]), set(), (set(), set()))
        with patch.object(self.s, 'OR_KEY', 'fake'):
            self.assertTrue(any(build is self.s.personal_feed for _, _, _, _, build in self.s.job_specs(self.s.local_now(), {})))

    def test_offline_report_uses_fake_cache_and_never_changes_input(self):
        import json
        from skald.engine import Engine
        from skald.members import fresh_config
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / 'skald.json').write_text(json.dumps(fresh_config(['ana', 'ben'])))
            (data / 'profile.json').write_text(json.dumps({'entries': []}))
            before = {p.name: p.read_bytes() for p in data.iterdir()}
            with Engine(data) as engine:
                responses = {'movie/1/recommendations': {'results': [title(i) for i in range(101, 111)]}}
                with patch.object(engine._server, 'adult_profile', return_value=self.profile([seed(1)])), \
                        patch.object(engine._server, 'get_model', return_value=None), \
                        patch.object(engine._server, 'pin_shown', side_effect=AssertionError('shown write')), \
                        patch.object(engine._server, 'save_learn', side_effect=AssertionError('learn write')):
                    report = engine.pool(tmdb=lambda path, **params: responses.get(path, {'results': []}))
                    self.assertEqual(report['pool_size'], 6)
                    self.assertEqual(report['cap_removed'], 4)
                    self.assertEqual(report['unique_foryou_titles'], 0)
                    missing = engine.pool()
                    self.assertEqual(missing['unavailable_endpoints'], 4)
            self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir()})

    def test_cached_background_counts_keep_today_selection_separate(self):
        from skald.engine import Engine
        engine = object.__new__(Engine)
        engine._server = self.s
        cached = {'pool_size': 12, 'seeds': [{'key': 'tmdb:1', 'kind': 'movie', 'chosen': True,
                                           'candidates': 10, 'kept': 6}], 'cap_removed': 4}
        with patch.object(self.s, 'adult_profile', return_value=self.profile([seed(1), seed(2)])), \
                patch.object(self.s, 'feed', return_value={'built': 123, 'names': {'pool': cached}, 'metas': {'tv': []}}), \
                patch.object(self.s, 'tmdb', side_effect=AssertionError('cached report fetched')):
            report = engine.pool()
        self.assertEqual([e['key'] for e in report['seeds'] if e['chosen']], ['tmdb:1', 'tmdb:2'])
        self.assertEqual(report['seeds'][0]['kept'], 6)
        self.assertIsNone(report['seeds'][1]['kept'])
        self.assertEqual(report['counts_built_at'], 123)
        self.assertFalse(report['selection_matches_build'])

    def test_background_report_stores_chosen_seeds_only(self):
        s = self.s
        report = {'seeds': [{'key': 'tmdb:1', 'chosen': True}, {'key': 'tmdb:2', 'chosen': False}],
                  'pool_size': 0}
        def pool(p, **params):
            if params.get('report') is not None:
                params['report'].update(report)
            return []
        with ExitStack() as stack:
            for name, value in {'adult_profile': {**self.profile([seed(1)]), 'searches': []},
                                'get_model': None, 'hit_titles': [], 'tired_keys': {}, 'read_taste': {},
                                'profile_text': '', 'llm_sections': {}, 'picks_to_rows': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'row_enabled', side_effect=lambda cid: cid == s.rid('foryou')))
            stack.enter_context(patch.object(s, 'candidate_pool', side_effect=pool))
            for name in ('polish_row', 'balance_row'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, **params: xs))
            out = s.personal_feed('en-US')
        self.assertEqual(out['_names'][s.rid('foryou')]['pool']['seeds'], [{'key': 'tmdb:1', 'chosen': True}])

    def test_rows_pool_parser_and_output(self):
        from skald.__main__ import parser, execute, text_output
        from types import SimpleNamespace
        args = parser().parse_args(['rows', 'pool', '--json'])
        engine = SimpleNamespace(pool=lambda: {'pool_size': 0, 'seeds': []})
        data = execute(args, engine, None, None)
        self.assertEqual(data['pool_size'], 0)
        out = io.StringIO()
        text_output(args, data, out)
        self.assertIn('Pool', out.getvalue())
