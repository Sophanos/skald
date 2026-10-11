"""Taste v2 contracts; stdlib, no services or network."""
from array import array
from contextlib import redirect_stdout
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('engine_test_server', Path(__file__).resolve().parents[1]/'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': ''}), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            spec.loader.exec_module(cls.s)
        cls.ensure_emb = staticmethod(cls.s.ensure_emb)

    @classmethod
    def tearDownClass(cls):
        for name in ('_pool', '_outer', '_nzb_pool'):
            getattr(cls.s, name).shutdown(wait=True, cancel_futures=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s.DATA = self.temp.name
        self.s._emb.clear()
        self.s._facets.clear()
        self.s._learn['hits'] = []
        self.s._model['m'] = None
        Path(self.temp.name, 'taste.json').write_text(json.dumps({'people': {}}))
        for name in ('tmdb', 'openrouter', 'anilist'):
            p = patch.object(self.s, name, side_effect=AssertionError('network forbidden'))
            p.start()
            self.addCleanup(p.stop)
        p = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        p.start()
        self.addCleanup(p.stop)
        for name in ('ensure_emb', 'ensure_facets', 'tag_experiences'):
            p = patch.object(self.s, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def entry(self, key='tmdb:1', **changes):
        return {'key': key, 'id': int(key[5:]), 'title': 'Seed', 'year': '2000', 'kind': 'tv', 'stype': 'series',
                'signal': 'loved', 'weight': 2., 'age': 0, 'by': ['ana'], 'who': 'told',
                'flags': [], 'genres': [], 'item': {}, **changes}

    def vector(self, key, values):
        self.s._emb[key] = array('f', values + [0.]*(self.s.EMB_DIM-len(values)))

    def model(self, entries):
        with redirect_stdout(io.StringIO()):
            return self.s.build_model({'entries': entries, 'seen': set()})

    def tv(self):
        return {'id': 'tmdb:1', 'type': 'series', 'episodes_played': 58, 'at': self.s.time.time()*1000}

    def test_equal_weight_told_love_upgrades_castle(self):
        with patch.object(self.s, 'person_loves', return_value=[('ana', 'Castle', '2009', 'tv')]), \
                patch.object(self.s, 'resolve_pick', return_value=[{'id': 1, 'name': 'Castle'}]), \
                patch.object(self.s, 'tmdb_details', return_value=('tv', {'id': 1, 'name': 'Castle'})):
            p = self.s.build_profile([self.tv()], [], remote=False)
        e = p['entries'][0]
        self.assertEqual((e['signal'], e['weight']), ('loved', 2.))
        self.assertEqual(e['shared']['signal'], 'watched')
        self.assertEqual(self.s.person_entries(p, 'ana')[0]['signal'], 'loved')
        self.assertEqual(self.s.person_entries(p, 'ben')[0]['signal'], 'watched')

    def test_trakt_duplicates_do_not_invent_shared_attribution(self):
        item = {'id': 'tmdb:1', 'type': 'tv', 'title': 'Seed', 'signal': 'watched', 'weight': 2.}
        p = self.s.build_profile([], [], [item, {**item, 'signal': 'loved'}], remote=False)
        self.assertEqual(len(p['entries']), 1)
        self.assertEqual((p['entries'][0]['who'], p['entries'][0]['by']), ('owner', ['ben']))
        self.assertEqual(p['entries'][0]['signal'], 'loved')
        self.assertEqual(self.s.person_entries(p, 'ana'), [])

    def test_tv_trakt_overlap_preserves_person_opinions(self):
        item = {'id': 'tmdb:1', 'type': 'tv', 'title': 'Seed', 'signal': 'loved', 'weight': 3., 'flags': ['binge']}
        with patch.object(self.s, 'tmdb_details', return_value=('tv', {'id': 1, 'name': 'Seed'})):
            p = self.s.build_profile([self.tv()], [], [item], remote=False)
        self.assertEqual(p['entries'][0]['by'], ['together'])
        self.assertEqual(p['entries'][0]['who'], 'both')
        self.assertEqual(self.s.person_entries(p, 'ana')[0]['signal'], 'watched')
        self.assertEqual(self.s.person_entries(p, 'ana')[0]['flags'], [])
        self.assertEqual(self.s.person_entries(p, 'ben')[0]['signal'], 'loved')

    def test_sparse_person_uses_own_seed_never_household_vector(self):
        for i in range(1, 8):
            key = f'tmdb:{i}'
            self.vector(key, [1., 0.] if i == 1 else [0., 1.])
            self.s._facets[key] = {'x': {'cozy_case': 3} if i == 1 else {'epic_quest': 3}}
        entries = [self.entry()] + [self.entry(f'tmdb:{i}', by=['ben']) for i in range(2, 8)]
        m = self.model(entries)
        self.assertAlmostEqual(m['pvec']['ana'][0], 1.)
        self.assertGreater(self.s.score_key(m, 'tmdb:1', 'ana')[0], self.s.score_key(m, 'tmdb:2', 'ana')[0])
        m['pvec']['ana'] = None
        with patch.object(self.s, 'SCORE_BLEND', (1, 0, 0)):
            before = self.s.score_key(m, 'tmdb:1', 'ana')[0]
            self.assertEqual(before, 0)
            m['vec'] = [0., 1.]
            self.assertEqual(before, self.s.score_key(m, 'tmdb:1', 'ana')[0])

    def test_single_unembedded_seed_has_facets_and_no_inherited_vector(self):
        self.s._facets['tmdb:1'] = {'g': ['Romance'], 'x': {'period_society': 3}}
        m = self.model([self.entry()])
        self.assertIsNone(m['pvec']['ana'])
        self.assertGreater(m['paff']['ana']['g:Romance'], 0)
        self.assertGreater(self.s.score_key(m, 'tmdb:1', 'ana')[0], 0)
        self.assertEqual(self.s.score_key(m, 'tmdb:1', 'ben')[0], 0)

    def test_remote_rejection_overrides_told_seed_per_person(self):
        with patch.object(self.s, 'person_loves', return_value=[('ana', 'Seed', None, 'tv')]):
            entries = [self.entry(by=['ben'], who='owner'),
                       self.entry(who='remote', signal='disliked', weight=-3.)]
            mine = self.s.person_entries({'entries': entries}, 'ana')
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['weight'], -3.)

    def test_explicit_seed_does_not_credit_a_different_year(self):
        with patch.object(self.s, 'person_loves', return_value=[('ana', 'Seed', '1992', 'tv')]):
            self.assertEqual(self.s.person_entries({'entries': [self.entry(by=['ben'])]}, 'ana'), [])
            mine = self.s.person_entries({'entries': [self.entry(by=['ben'], year='1992')]}, 'ana')
            self.assertEqual(mine[0]['signal'], 'loved')

    def test_remote_like_and_love_are_independent_of_ben_volume(self):
        self.s._facets.update({'tmdb:1': {'g': ['Romance'], 'x': {'period_society': 3}},
                               'tmdb:2': {'g': ['Action'], 'x': {'team_spectacle': 3}}})
        for signal, weight in (('liked', 2.), ('loved', 3.)):
            her = self.entry(who='remote', signal=signal, weight=weight)
            one = self.model([her, self.entry('tmdb:2', by=['ben'])])
            many = self.model([her] + [self.entry('tmdb:2', by=['ben'])]*50)
            self.assertEqual(one['paff']['ana'], many['paff']['ana'])
            self.assertEqual(self.s.score_key(one, 'tmdb:1', 'ana'), self.s.score_key(many, 'tmdb:1', 'ana'))

    def test_together_is_symmetric_and_penalizes_disagreement(self):
        self.s._facets['tmdb:1'] = {'g': ['Romance']}
        m = self.model([self.entry()])
        a = self.s.score_key(m, 'tmdb:1', 'ana')[0]
        b = self.s.score_key(m, 'tmdb:1', 'ben')[0]
        house = self.s.score_key(m, 'tmdb:1')[0]
        a = a/40 + 39/40*house
        b = house
        together = self.s.score_key(m, 'tmdb:1', 'together')[0]
        self.assertAlmostEqual(together, (a+b)/2 - .25*abs(a-b))
        self.assertLess(together, (a+b)/2)
        m['paff']['ben'], m['paff']['ana'] = m['paff']['ana'], m['paff']['ben']
        m['pbscale']['ben'], m['pbscale']['ana'] = m['pbscale']['ana'], m['pbscale']['ben']
        m['nexp']['ben'], m['nexp']['ana'] = m['nexp']['ana'], m['nexp']['ben']
        self.assertEqual(together, self.s.score_key(m, 'tmdb:1', 'together')[0])

    def test_reason_negatives_are_person_level_and_cover_all_reasons(self):
        self.s._facets.update({'tmdb:1': {'g': ['Horror'], 'x': {'dread': 3, 'revenge': 3}},
                               'tmdb:2': {'g': ['Romance'], 'x': {'heartwarming': 3}}})
        neg = self.entry(who='remote', signal='disliked', weight=-3.)
        m = self.model([neg, self.entry('tmdb:2')])
        self.assertLess(m['paff']['ana']['g:Horror'], 0)
        self.assertLess(self.s.score_key(m, 'tmdb:1', 'ana')[0], self.s.score_key(m, 'tmdb:2', 'ana')[0])
        for reason, fid in [('too dark', 'x:bleak'), ('too slow', 'x:slow'), ('too violent', 'x:violent'),
                            ('too silly', 'x:silly'), ('not my genre', 'g:Horror'), ('seen enough of these', 'x:dread')]:
            with patch.object(self.s, 'active_feedback', return_value={('tmdb:1', 'ana'): {'reasons': [reason]}}):
                m = self.model([neg, self.entry('tmdb:2')])
            self.assertLess(m['paff']['ana'][fid], 0)
            self.assertNotIn(fid, m['paff']['ben'])
        self.assertLess(self.s.facet_bonus(m, self.s._facets['tmdb:1'], m['paff']['ana'])[0], 0)

    def test_two_person_reasons_do_not_cross_contaminate(self):
        entries = [self.entry(who='remote', weight=-3.), self.entry(who='remote', weight=-3., by=['ben'])]
        active = {('tmdb:1', 'ana'): {'reasons': ['too dark']},
                  ('tmdb:1', 'ben'): {'reasons': ['too silly']}}
        with patch.object(self.s, 'feedback_reasons', return_value={'tmdb:1': ['too dark', 'too silly']}), \
                patch.object(self.s, 'active_feedback', return_value=active):
            m = self.model(entries)
        self.assertNotIn('x:silly', m['paff']['ana'])
        self.assertNotIn('x:dark', m['paff']['ben'])

    def test_quality_prior_is_bounded_and_old_records_are_neutral(self):
        self.assertEqual(self.s.quality_prior({}), 0)
        self.assertFalse(self.s.QUALITY_PRIOR)
        self.assertEqual(self.s.quality_prior({'vote_average': 8, 'vote_count': 10000}), 0)
        with patch.object(self.s, 'QUALITY_PRIOR', True):
            low = self.s.quality_prior({'vote_average': 8, 'vote_count': 1})
            high = self.s.quality_prior({'vote_average': 8, 'vote_count': 10000})
            self.assertEqual(self.s.quality_prior({'vote_average': -2, 'vote_count': -1}), 0)
        self.assertLess(low, high)
        self.assertLessEqual(high, .02)
        self.s._facets['tmdb:1'] = {'vote_average': 8, 'vote_count': 10000}
        m = self.model([])
        self.assertEqual(self.s.score_key(m, 'tmdb:1')[0], 0)
        with patch.object(self.s, 'QUALITY_PRIOR', True):
            self.assertAlmostEqual(self.s.score_key(m, 'tmdb:1')[0], high)
        with patch.object(self.s, 'tmdb', return_value={'vote_average': 8, 'vote_count': 100, 'belongs_to_collection': {'id': 5}}):
            rec = self.s.facet_record('movie', 1)
        self.assertEqual((rec['vote_average'], rec['vote_count'], rec['collection']), (8, 100, 5))

    def test_episode_weight_is_logarithmic_and_tames_flat_binge_boosts(self):
        e = self.entry(who='owner', flags=['binge', 'comfort'], weight=3.)
        values = [self.s.entry_weight({**e, 'item': {'episodes_played': n}}) for n in (10, 100, 1000)]
        self.assertLess(values[1]-values[0], values[0])
        self.assertLess(values[2]/values[0], 2)
        self.assertAlmostEqual(values[1], 2*(1+.1*math.log1p(100)))
        self.assertEqual(self.s.entry_weight({**e, 'weight': -3., 'item': {'episodes_played': 1000}}), -3.)

    def test_mmr_diversity(self):
        self.vector('tmdb:1', [1., 0.])
        self.vector('tmdb:2', [1., 0.])
        self.vector('tmdb:3', [0., 1.])
        keys = ['tmdb:1', 'tmdb:2', 'tmdb:3', 'tmdb:1']
        self.assertEqual(self.s.mmr(keys, 1), keys[:3])
        self.assertEqual(self.s.mmr(keys, .2), ['tmdb:1', 'tmdb:3', 'tmdb:2'])
        self.assertEqual(self.s.mmr([], .5), [])
        with self.assertRaises(ValueError):
            self.s.mmr(keys, 1.1)

    def test_array_storage_round_trip_preserves_float16_json(self):
        self.vector('tmdb:1', [.6, .8])
        self.s._emb_dirty.set()
        self.s.save_emb()
        saved = Path(self.temp.name, 'emb.json').read_bytes()
        self.s._emb.clear()
        self.s.load_emb()
        self.assertIsInstance(self.s._emb['tmdb:1'], array)
        self.assertEqual(self.s._emb['tmdb:1'].typecode, 'f')
        self.assertAlmostEqual(self.s._emb['tmdb:1'][0], .6, places=3)
        self.s._emb_dirty.set()
        self.s.save_emb()
        self.assertEqual(saved, Path(self.temp.name, 'emb.json').read_bytes())

    def test_new_embedding_is_normalized_array(self):
        response = {'data': [{'index': 0, 'embedding': [3., 4.] + [0.]*(self.s.EMB_DIM-2)}]}
        with patch.object(self.s, 'OR_KEY', 'offline'), patch.object(self.s, 'title_text', return_value='Seed'), \
                patch.object(self.s, 'openrouter', return_value=response):
            self.assertEqual(self.ensure_emb([('tmdb:1', 'tv')]), 1)
        self.assertIsInstance(self.s._emb['tmdb:1'], array)
        self.assertAlmostEqual(self.s.fit(self.s._emb['tmdb:1'], 'tmdb:1'), 1.)

    def test_calibration_median_and_ties_are_seventy(self):
        for bg in ([i/100 for i in range(80)], [.5]*80):
            m = {'cal': len(bg), 'bg': bg, 'nexp': {'ben': 40}}
            self.assertEqual(self.s.match_pct(m, bg[40]), 70)
            self.assertGreaterEqual(self.s.match_pct(m, bg[-1]), 70)
        self.assertIsNone(self.s.match_pct({'cal': None}, .5))
        for i in range(1, 81):
            self.s._facets[f'tmdb:{i}'] = {'g': ['Romance'], 'vote_average': i/8, 'vote_count': 1000}
        with patch.object(self.s, 'tmdb', return_value={'results': [{'id': i} for i in range(1, 81)]}):
            m = self.model([self.entry()])
        self.assertEqual(m['vbase'], 0)
        self.assertEqual(m['cal'], 80)
        m['nexp'] = {'ben': 40, 'ana': 40}
        self.assertEqual(self.s.match_pct(m, m['bg'][40]), 70)
        self.assertEqual(self.s.match_pct(m, m['pbg']['ana'][40], 'ana'), 70)
        self.assertEqual(self.s.match_pct(m, m['pbg']['together'][40], 'together'), 70)

    def test_sparse_positive_x_lift_never_invents_dislikes(self):
        self.s._facets.update({f'tmdb:{i}': {'x': {'cozy_case': 3}} for i in range(1, 10)})
        self.s._facets['tmdb:10'] = {'x': {'period_society': 3}}
        entries = [self.entry(weight=.07), self.entry('tmdb:10')]
        df = {'x:cozy_case': 9, 'x:period_society': 1}
        aff = self.s.affinities(entries, self.s._facets, df, 11, set(), loose=True)[0]
        self.assertEqual(aff['x:cozy_case'], 0)
        self.assertTrue(all(v >= 0 for fid, v in aff.items() if fid.startswith('x:')))
        strict = self.s.affinities(entries*2, self.s._facets, df, 11, set())[0]
        self.assertLess(strict['x:cozy_case'], 0)  # Household lift stays unchanged.

    def test_person_confidence_and_notes_use_person_percentiles(self):
        m = self.model([])
        m.update(cal=20, bg=[0.]*20, pbg={'ana': [1.]*20}, nexp={'ana': 40})
        self.assertEqual(self.s.confidence(m, 'tmdb:1', .5, [], 'ana')[1], 40)
        self.assertEqual(self.s.confidence(m, 'tmdb:1', .5, [])[1], 99)
        self.s._model['m'] = m
        with patch.object(self.s, 'score_key', return_value=(.5, [])), \
                patch.object(self.s, 'reasons', return_value=['reason']):
            self.assertEqual(self.s.match_note('tmdb:1', person='ana'), [])
            self.assertEqual(self.s.match_note('tmdb:1'), ['reason'])

    def test_shared_negative_reason_does_not_cross_people(self):
        self.s._facets['tmdb:1'] = {'x': {'dread': 3}}
        entry = self.entry(weight=-3., by=['together'], who='both')
        before = self.model([entry])
        active = {('tmdb:1', 'ben'): {'action': 'not_for_us', 'reasons': ['too dark']}}
        with patch.object(self.s, 'active_feedback', return_value=active), \
                patch.object(self.s, 'feedback_reasons', return_value={'tmdb:1': ['too dark']}):
            after = self.model([entry])
        self.assertEqual(before['paff']['ana'], after['paff']['ana'])
        self.assertEqual(before['px']['ana'], after['px']['ana'])
        self.assertLess(after['paff']['ben']['x:dark'], 0)

    def test_accumulated_reason_delta_is_capped_per_person_facet(self):
        entries = [self.entry(f'tmdb:{i}', weight=-3., who='remote') for i in range(1, 6)]
        active = {(e['key'], 'ana'): {'reasons': ['too dark', 'too slow']} for e in entries}
        with patch.object(self.s, 'active_feedback', return_value=active):
            m = self.model(entries)
        self.assertAlmostEqual(m['paff']['ana']['x:dark'], -.6)
        self.assertAlmostEqual(m['paff']['ana']['x:dread'], -.6)
        self.assertEqual(m['px']['ana']['dark'], -3)
        self.assertEqual(m['px']['ana']['dread'], -3)
        self.assertNotIn('x:dark', m['paff']['ben'])
        self.s._facets.update({e['key']: {'x': {'dread': 3}} for e in entries})
        base = self.model(entries)
        with patch.object(self.s, 'active_feedback', return_value=active):
            m = self.model(entries)
        self.assertAlmostEqual(m['paff']['ana']['x:dread'] - base['paff']['ana']['x:dread'], -.6)
        self.assertEqual(m['px']['ana']['dread'] - base['px']['ana']['dread'], -3)

    def test_tv_weight_with_trakt_flags_keeps_unboosted_weight(self):
        e = self.entry(who='both', weight=2., flags=['binge', 'comfort'], item={'episodes_played': 100})
        factor = 1+.1*math.log1p(100)
        self.assertAlmostEqual(self.s.entry_weight(e), 2*factor)
        self.assertAlmostEqual(self.s.entry_weight({**e, 'wsrc': 'trakt'}), factor)
        with patch.object(self.s, 'tmdb_details', return_value=('tv', {'id': 1, 'name': 'Seed'})):
            p = self.s.build_profile([self.tv()], [], [{'id': 'tmdb:1', 'type': 'tv', 'title': 'Seed',
                'signal': 'watched', 'weight': 1., 'flags': ['binge']}], remote=False)
        both = p['entries'][0]
        self.assertNotEqual(both.get('wsrc'), 'trakt')
        self.assertEqual(self.s.person_entries(p, 'ben')[0]['wsrc'], 'trakt')
        self.assertNotEqual(self.s.person_entries(p, 'ana')[0].get('wsrc'), 'trakt')

    def test_emb_without_facets_is_unknown_for_every_scorer(self):
        self.vector('tmdb:1', [1., 0.])
        m = self.model([self.entry()])
        for person in (None, *self.s.PEOPLE, 'together'):
            self.assertEqual(self.s.score_key(m, 'tmdb:1', person), (None, []))

    def test_vector_errors_print_and_only_guard_the_vector_build(self):
        original = self.s.taste_vector
        def vector(p, **kwargs):
            if not p.get('learned', True):
                raise ValueError('bad vector')
            return original(p, **kwargs)
        with patch.object(self.s, 'taste_vector', side_effect=vector), patch('builtins.print') as printed:
            m = self.model([self.entry()])
        self.assertIsNone(m['pvec']['ana'])
        self.assertTrue(any('ValueError: bad vector' in str(call) for call in printed.call_args_list))
        with patch.object(self.s, 'person_entries', return_value=[self.entry()]), patch.dict(self.s._emb, {'tmdb:1': [1.]}):
            m = self.model([])  # Older entries without explicit do not break vector selection.
        self.assertIsNone(m['pvec']['ana'])

    def test_add_person_loves_tolerates_missing_item_and_flags(self):
        e = self.entry(by=['together'])
        del e['item'], e['flags']
        with patch.object(self.s, 'person_loves', return_value=[('ana', 'Seed', None, 'tv')]), \
                patch.object(self.s, 'resolve_pick', return_value=[{'id': 1, 'name': 'Seed'}]):
            self.s.add_person_loves([e], {e['key']: e})
        self.assertEqual(e['opinions']['ana']['signal'], 'loved')
        self.assertEqual(e['shared']['item'], {})
        self.assertEqual(e['shared']['flags'], [])

    def test_explicit_counts_support_and_explain_gate(self):
        self.s._facets['tmdb:1'] = {'a': [[1, 'One-off actor']]}
        entries = [self.entry(f'tmdb:{i}') for i in range(1, 40)]
        sparse = self.model(entries)
        mature = self.model(entries+[self.entry('tmdb:40')])
        self.assertEqual(sparse['nexp']['ana'], 39)
        self.assertEqual(mature['nexp']['ana'], 40)
        self.assertIn('a:1', sparse['paff']['ana'])
        self.assertNotIn('a:1', mature['paff']['ana'])
        mixed = self.model([self.entry(), self.entry('tmdb:2', by=['together']),
                            self.entry('tmdb:3', weight=-3.), self.entry('tmdb:4', weight=0.)])
        self.assertEqual(mixed['nexp']['ana'], 1)
        mixed.update(cal=20, bg=[0.]*20, pbg={'ana': [0.]*20})
        self.s._model['m'] = mixed
        self.assertEqual(self.s.explain('tmdb:1')['per_person']['ana']['n_explicit'], 1)
        self.assertIsNone(self.s.explain('tmdb:1')['per_person']['ana']['percentile'])
        mixed['nexp']['ana'] = 20
        self.assertIsNotNone(self.s.explain('tmdb:1')['per_person']['ana']['percentile'])

    def test_seed_similarity_and_top_k_mean(self):
        self.vector('tmdb:1', [1., 0.])
        self.vector('tmdb:2', [.6, .8])
        self.s._facets.update({'tmdb:1': {'g': ['Crime'], 'x': {'cozy_case': 3}},
                              'tmdb:2': {'g': ['Crime'], 'x': {'cozy_case': 3}},
                              'tmdb:3': {'g': ['Drama']}})
        self.model([])
        ov = math.log(4/3)/math.log(4)*(self.s.FACET_W['g']+self.s.FACET_W['x'])
        self.assertAlmostEqual(self.s.seed_similarity('tmdb:1', 'tmdb:2'), .5*.6+.5*math.tanh(ov/2))
        self.assertEqual(self.s.seed_similarity('missing', 'tmdb:1'), 0)
        self.assertIsNone(self.s.seed_knn([], 'tmdb:1'))
        with self.assertRaises(ValueError):
            self.s.seed_knn(['tmdb:1'], 'tmdb:2', k=0)
        sims = [self.s.seed_similarity(k, 'tmdb:1') for k in ('tmdb:1', 'tmdb:2', 'tmdb:3')]
        self.assertAlmostEqual(self.s.seed_knn(['tmdb:1', 'tmdb:2', 'tmdb:3'], 'tmdb:1', k=2), sum(sorted(sims)[-2:])/2)


if __name__ == '__main__':
    unittest.main()
