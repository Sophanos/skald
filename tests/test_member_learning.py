"""Synthetic household enrichment, distinct title evidence and visible coverage."""
from array import array
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from skald.__main__ import main, parser, text_output
from skald import setup
from test_taste_report import TasteCase, NOW
from decide_kit import load_server, close_server


class MemberLearningTests(TasteCase):
    def setUp(self):
        super().setUp()
        self.s._facets.clear()
        self.s._emb.clear()
        self.s._emb_kinds.clear()

    def entry(self, i, person, weight=1., kind='movie', **extra):
        return {'key': f'tmdb:{i}', 'id': i, 'kind': kind,
                'stype': 'movie' if kind == 'movie' else 'series', 'title': f'Fixture {i} {kind}',
                'year': '2020', 'signal': 'watched', 'weight': weight, 'age': 0.,
                'by': [person], 'who': 'import', 'flags': [], 'genres': [], 'item': {}, **extra}

    def profile(self):
        return {'entries': [self.entry(i, 'ben', 1.8) for i in range(1001, 1301)] +
                           [self.entry(i, 'ana', .21) for i in range(1, 451)], 'seen': set()}

    def build(self, p, **kwargs):
        with redirect_stdout(io.StringIO()):
            return self.s.build_model(p, **kwargs)

    def test_facets_fetch_missing_fairly_and_converge(self):
        s, p, requested = self.s, self.profile(), []
        def fetch(pairs):
            pairs = list(pairs)
            self.assertLessEqual(len(pairs), 400)
            self.assertTrue(all(not s.frec(k, kind) for k, kind in pairs))
            requested.append(pairs)
            for k, kind in pairs:
                s._facets[s.fkey(k, kind)] = {'kind': kind, 'title': k}
        with patch.object(s, 'ensure_facets', side_effect=fetch):
            for _ in range(3):
                self.build(p, enrich=False)
        self.assertEqual(requested[0][:2], [('tmdb:1', 'movie'), ('tmdb:1001', 'movie')])
        self.assertEqual(sum(int(k[5:]) <= 450 for k, _ in requested[0]), 200)
        self.assertEqual([len(batch) for batch in requested], [400, 350, 0])
        self.assertEqual(len({pair for batch in requested for pair in batch}), 750)
        for who, total in (('ana', 450), ('ben', 300)):
            self.assertEqual(sum(bool(s.frec(e['key'], e['kind'])) for e in s.person_entries(p, who)), total)

    def test_tags_receive_full_fair_order_and_keep_batch_limit(self):
        with patch.object(self.s, 'tag_experiences') as tags:
            self.build(self.profile())
        pairs = tags.call_args.args[0]
        self.assertEqual(pairs[:2], [('tmdb:1', 'movie'), ('tmdb:1001', 'movie')])
        self.assertEqual(len(pairs), 750)
        self.assertEqual(tags.call_args.kwargs['max_batches'], 16)

    def test_tagging_keeps_adult_history_scope_and_fair_order(self):
        s = self.s
        p = {'entries': [self.entry(5001, 'kid'), self.entry(5002, 'kid'),
                        self.entry(1001, 'ben', 1.8), self.entry(1002, 'ben', 1.8),
                        self.entry(1, 'ana', .21), self.entry(2, 'ana', .21),
                        self.entry(9, 'ana', -1), self.entry(10, 'ben', 0),
                        self.entry(3, 'together'), self.entry(7, 'kid'),
                        self.entry(7, 'ana', kind='tv'), self.entry(9, 'kid')], 'seen': set()}
        with patch.object(s, 'PEOPLE', ('ana', 'ben', 'kid')), \
                patch.object(s, 'kids_age', side_effect=lambda who: 12 if who == 'kid' else None), \
                patch.object(s, 'tag_experiences') as tags, patch.object(s, 'ensure_emb') as embeddings, \
                patch.object(s, 'ensure_facets') as facets:
            self.build(p)
        self.assertEqual(tags.call_args.args[0], [('tmdb:1', 'movie'), ('tmdb:1001', 'movie'),
            ('tmdb:3', 'movie'), ('tmdb:2', 'movie'), ('tmdb:1002', 'movie'), ('tmdb:7', 'tv'),
            ('tmdb:10', 'movie'), ('tmdb:9', 'movie')])
        self.assertEqual(tags.call_args.kwargs, {'max_batches': 16, 'fetch_facets': False})
        self.assertEqual(set(embeddings.call_args_list[0].args[0]),
            {('tmdb:1', 'movie'), ('tmdb:2', 'movie'), ('tmdb:3', 'movie'), ('tmdb:7', 'tv'),
             ('tmdb:9', 'movie'), ('tmdb:10', 'movie'), ('tmdb:1001', 'movie'), ('tmdb:1002', 'movie')})
        self.assertEqual(set(facets.call_args.args[0]), {(e['key'], e['kind']) for e in p['entries']})

    def test_tag_queue_includes_all_stop_readings_and_dislikes(self):
        s = self.s
        entries = [self.entry(1, 'eva'), self.entry(2, 'oskar'),
                   self.entry(3, 'eva', 0, signal='abandoned', abandon='unsure'),
                   self.entry(4, 'eva', -.5, signal='abandoned', abandon='far'),
                   self.entry(5, 'oskar', .5, signal='interest', abandon='related'),
                   self.entry(6, 'oskar', -2, signal='disliked')]
        with patch.object(s, 'PEOPLE', ('eva', 'oskar')), patch.object(s, 'tag_experiences') as tags:
            m = self.build({'entries': entries, 'seen': set()})
        self.assertEqual(set(tags.call_args.args[0]), {(e['key'], e['kind']) for e in entries})
        self.assertTrue({'tmdb:3', 'tmdb:4', 'tmdb:5'}.isdisjoint(m['ent']))

    def test_worker_refreshes_model_and_calibration_after_candidate_tags(self):
        s = load_server(Path(self.temp.name))
        self.addCleanup(close_server, s)
        entries = [self.entry(i, 'eva', 3., signal='loved') for i in range(1, 21)]
        p = {'entries': entries, 'seen': {e['key'] for e in entries}, 'hash': 'fixture'}
        s._facets.update({f'tmdb:{i}': {'kind': 'movie', 'title': f'Fixture {i}', 'o': 'Fixture',
            'k': [[1, 'Space']], **({'x': {'dread': 3}} if i <= 20 else
                                  {'x': {'feel_good': 3}} if i > 30 else {})} for i in range(1, 251)})
        for rec in s._facets.values():
            if rec.get('x'):
                rec['x_state'] = {'status': 'manual'}
        def enrich(*args):
            s.tag_experiences([('tmdb:21', 'movie')], max_batches=2)
        with patch.object(s, 'PEOPLE', ('eva', 'oskar')), patch.object(s, 'OR_KEY', 'offline'), \
                patch.object(s, 'llm_text', return_value='1|dread:3'), \
                patch.object(s, 'ensure_emb'), patch.object(s, '_feed_started', True), \
                patch.object(s, 'refresh_profiles', return_value={}), patch.object(s, 'build_feeds', side_effect=enrich):
            s._profiles['household'] = p
            old = s.get_model()
            old_bg = list(old['pbg']['eva'])
            s.feed_pass(s.local_now(), {})
            new = s.get_model(False)
            self.assertTrue(new is not old)
            self.assertNotEqual(new['pbg']['eva'], old_bg)
            with redirect_stdout(io.StringIO()):
                expected = s.build_model(p)
            self.assertEqual(new['paff'], expected['paff'])
            self.assertEqual(new['pbg'], expected['pbg'])

    def test_tags_landing_during_model_build_still_trigger_worker_refresh(self):
        s = load_server(Path(self.temp.name))
        self.addCleanup(close_server, s)
        entries = [self.entry(i, 'eva', 3., signal='loved') for i in range(1, 21)]
        p = {'entries': entries, 'seen': {e['key'] for e in entries}, 'hash': 'fixture'}
        s._facets.update({f'tmdb:{i}': {'kind': 'movie', 'title': f'Fixture {i}', 'o': 'Fixture',
            'k': [[1, 'Space']], **({'x': {'dread': 3}} if i <= 20 else
                                  {'x': {'feel_good': 3}} if i > 30 else {})} for i in range(1, 251)})
        for rec in s._facets.values():
            if rec.get('x'):
                rec['x_state'] = {'status': 'manual'}
        captured, resume = threading.Event(), threading.Event()
        affinities = s.affinities
        def pause(*args, **kwargs):
            if not captured.is_set():
                captured.set()  # build_model has already captured its facet store and tag version.
                if not resume.wait(5):
                    raise AssertionError('model build was not resumed')
            return affinities(*args, **kwargs)
        with patch.object(s, 'PEOPLE', ('eva', 'oskar')), patch.object(s, 'OR_KEY', 'offline'), \
                patch.object(s, 'llm_text', return_value='1|dread:3'), patch.object(s, 'ensure_emb'), \
                patch.object(s, 'affinities', side_effect=pause), patch.object(s, '_feed_started', True), \
                patch.object(s, 'refresh_profiles', return_value={}), patch.object(s, 'build_feeds'):
            s._profiles['household'] = p
            version = s._tag_version
            pending = s._outer.submit(s.get_model)
            try:
                self.assertTrue(captured.wait(5))
                self.assertEqual(s.tag_experiences([('tmdb:21', 'movie')]), 1)
            finally:
                resume.set()
                built = pending.result(timeout=5)
            self.assertEqual(built['tag_version'], version)
            self.assertGreater(s._tag_version, version)
            s.feed_pass(s.local_now(), {})
            fresh = s.get_model(False)
            self.assertTrue(fresh is not built)
            self.assertEqual(fresh['tag_version'], s._tag_version)
            expected = s.build_model(p)
            self.assertEqual(fresh['paff'], expected['paff'])
            self.assertEqual(fresh['pbg'], expected['pbg'])

    def test_child_overlap_does_not_advance_one_adults_tagging_queue(self):
        s = self.s
        entries = [self.entry(i, 'ben', 1.8) for i in range(1001, 1601)] + \
                  [self.entry(i, 'ana', .21, by=['ana', 'kid']) for i in range(1, 601)]
        s._facets.update({e['key']: {'kind': e['kind'], 'title': e['title']} for e in entries})
        with patch.object(s, 'PEOPLE', ('ana', 'ben', 'kid')), \
                patch.object(s, 'kids_age', side_effect=lambda who: 12 if who == 'kid' else None), \
                patch.object(s, 'tag_experiences') as tags:
            self.build({'entries': entries, 'seen': set()})
        pairs = tags.call_args.args[0]
        first = pairs[:s.TAG_BATCH * tags.call_args.kwargs['max_batches']]
        ana = sum(int(k[5:]) <= 600 for k, _ in first)
        ben = len(first) - ana
        self.assertEqual(len(first), 400)
        self.assertLessEqual(abs(ana - ben), 1)
        self.assertEqual(len(set(pairs)), 1200)

    def test_tagging_alias_fetch_cannot_exceed_build_facet_cap(self):
        p, fetched = self.profile(), []
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': 'offline'}), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                setup.server(Path(self.temp.name)) as s:
            # Every movie is missing its alias because a series owns the bare slot.
            s._facets.update({e['key']: {'kind': 'tv', 'title': 'Fixture series'} for e in p['entries']})
            def fetch(pairs):
                for k, kind in pairs:
                    fetched.append((k, kind))
                    s._facets[s.fkey(k, kind)] = {'kind': kind, 'title': k, 'o': 'Fixture overview'}
            with patch.object(s, 'ensure_facets', side_effect=fetch), \
                    patch.object(s, 'llm_text', return_value=''), patch.object(s, 'ensure_emb'), \
                    patch.object(s, 'tmdb', return_value={'results': []}), patch.object(self, 's', s):
                self.build(p)
        self.assertEqual(len(fetched), 400)

    def test_embeddings_fetch_missing_fairly_and_converge(self):
        s, p, requested = self.s, self.profile(), []
        def embed(pairs):
            pairs = list(pairs)
            self.assertLessEqual(len(pairs), 400)
            self.assertTrue(all(s.femb(k, kind) is None for k, kind in pairs))
            requested.append(pairs)
            for k, kind in pairs:
                s._emb[k] = array('f', [1.] + [0.] * (s.EMB_DIM - 1))
                s._emb_kinds[k] = kind
        with patch.object(s, 'ensure_emb', side_effect=embed):
            for _ in range(3):
                s.taste_vector(p)
        self.assertEqual(requested[0][:2], [('tmdb:1', 'movie'), ('tmdb:1001', 'movie')])
        self.assertEqual([len(batch) for batch in requested], [400, 350, 0])

    def test_embedding_budget_exceeded_still_uses_existing_vectors(self):
        self.s._emb['tmdb:1'] = array('f', [1.] + [0.] * (self.s.EMB_DIM - 1))
        with patch.object(self.s, 'ensure_emb', side_effect=self.s.BudgetExceeded('fixture')):
            self.assertIsNotNone(self.s.taste_vector({'entries': [self.entry(1, 'ana')]}, min_titles=1))

    def test_fair_order_positive_first_shared_and_kind_identity(self):
        p = {'entries': [self.entry(9, 'ana', -1), self.entry(1, 'ana'), self.entry(2, 'ben'),
                         self.entry(3, 'together'), self.entry(1, 'ben'), self.entry(1, 'ana', kind='tv')]}
        self.assertEqual(self.s.enrichment_pairs(p), [('tmdb:1', 'movie'), ('tmdb:2', 'movie'),
            ('tmdb:3', 'movie'), ('tmdb:1', 'tv'), ('tmdb:9', 'movie')])

    def test_fair_order_uses_existing_ownership_and_ignored_watch_rules(self):
        p = {'entries': [self.entry(1, 'ana'), self.entry(2, 'ana')]}
        fixes = [{'op': 'move', 'key': 'tmdb:1', 'who': 'ana', 'to': 'ben'},
                 {'op': 'ignore', 'key': 'tmdb:2', 'who': 'ana'}]
        with patch.object(self.s, 'title_fixes', return_value=fixes):
            self.assertEqual(self.s.enrichment_pairs(p), [('tmdb:2', 'movie'), ('tmdb:1', 'movie')])

    def test_duplicate_title_support_drops_facet_but_weights_still_add(self):
        s = self.s
        entries = [self.entry(1, 'ana', .2), self.entry(1, 'ben', 2.), self.entry(1, 'ana', -1)]
        s._facets['tmdb:1'] = {'kind': 'movie', 'k': [[1, 'Theme']]}
        with patch.object(s, 'facet_value', wraps=s.facet_value) as value:
            _, counts = s.affinities(entries, s._facets, {'k:1': 1}, 10, set())
        self.assertEqual(counts['k:1'], 1)
        self.assertEqual(value.call_args.args[1:4], (2.2, 1., 1))
        aff, _ = s.affinities(entries[:2], s._facets, {'k:1': 1}, 10, set())
        self.assertNotIn('k:1', aff)

    def test_same_number_movie_and_series_support_two_titles(self):
        s = self.s
        for kind in ('movie', 'tv'):
            s._facets[s.fkey('tmdb:1', kind)] = {'kind': kind, 'k': [[1, 'Theme']]}
        entries = [self.entry(1, 'ana'), self.entry(1, 'ben'), self.entry(1, 'ana', kind='tv')]
        aff, counts = s.affinities(entries, s._facets, {'k:1': 2}, 10, set())
        self.assertEqual(counts['k:1'], 2)
        self.assertIn('k:1', aff)
        model = self.build({'entries': entries}, enrich=False)
        self.assertEqual(len(model['holders']['k:1']), 2)
        self.assertEqual({model['ent'][k][0] for _, k in model['holders']['k:1']},
                         {'Fixture 1 movie', 'Fixture 1 tv'})
        self.assertEqual(s.anchor(model, 'tmdb:1', 'k:1', kind='tv')[1], 'Fixture 1 movie')
        model['holders']['x:cozy_case'] = model['holders']['k:1']
        with patch.object(s, 'with_art', return_value=([], {})) as recommendations:
            s.feeling_pool(model, {'entries': entries, 'seen': set(), 'negative': set()}, ['cozy_case'], 'en-US')
        self.assertEqual({call.args[0] for call in recommendations.call_args_list},
                         {'movie/1/recommendations', 'tv/1/recommendations'})

    def test_holders_keep_highest_weight_once_per_title(self):
        self.s._facets.update({f'tmdb:{i}': {'kind': 'movie', 'k': [[1, 'Theme']]} for i in (1, 2)})
        m = self.build({'entries': [self.entry(1, 'ana', .2), self.entry(1, 'ben', 2.),
                                   self.entry(2, 'ana', .5)]}, enrich=False)
        self.assertEqual(m['holders']['k:1'], [(2., 'tmdb:1'), (.5, 'tmdb:2')])

    def test_report_coverage_in_text_and_json_per_adult(self):
        self.e._profile['entries'] = [self.entry(1, 'ana'), self.entry(1, 'ana'),
            self.entry(1, 'ana', kind='tv'), self.entry(2, 'ben'), self.entry(3, 'ana', -1)]
        self.s._facets['tmdb:1'] = {'kind': 'movie', 'title': 'Fixture 1'}
        with patch.object(self.e, '_report_events', return_value=(NOW, [])), \
                patch.object(self.s, 'feed_rows', return_value=[]):
            report = self.e.report()
            self.assertEqual(report['coverage'], {'ana': {'learned': 1, 'total': 2}, 'ben': {'learned': 0, 'total': 1}})
            out = io.StringIO()
            text_output(parser().parse_args(['taste', 'report']), report, out)
            self.assertIn('Ana: Learned from 1 of 2 titles', out.getvalue())
            self.assertIn('Ben: Learned from 0 of 1 titles', out.getvalue())
            with patch('skald.__main__.Engine', return_value=self.e):
                out = io.StringIO()
                self.assertEqual(main(['--snapshot', self.temp.name, 'taste', 'report', '--json'], out=out), 0)
            self.assertEqual(json.loads(out.getvalue())['coverage'], report['coverage'])

    def test_report_coverage_uses_positive_member_and_shared_titles_only(self):
        self.e._profile['entries'] = [self.entry(1, 'ana'), self.entry(2, 'together'), self.entry(3, 'ana')]
        self.s._facets['tmdb:2'] = {'kind': 'movie', 'title': 'Shared fixture'}
        fixes = [{'op': 'ignore', 'key': 'tmdb:3', 'who': 'ana'}]
        with patch.object(self.s, 'title_fixes', return_value=fixes), \
                patch.object(self.s, 'adult_people', return_value=('ana',)), \
                patch.object(self.e, '_report_events', return_value=(NOW, [])), \
                patch.object(self.s, 'feed_rows', return_value=[]):
            self.assertEqual(self.e.report('ana')['coverage'], {'ana': {'learned': 1, 'total': 2}})


if __name__ == '__main__':
    unittest.main()
