"""Person damping contracts; household arithmetic stays exact."""
from skald.members import rid
from collections import Counter
import json
import math
import unittest
from unittest.mock import patch

from eval.profile_golden import ROOT, offline_server
from eval import reaction_detail
from test_taste_report import TasteCase


class ReactionW15(unittest.TestCase):
    def setUp(self):
        context = offline_server(ROOT / 'server.py')
        self.s = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.profile = self.s.build_profile(self.s.read_history(), [], self.s.read_trakt())
        self.store = {self.s.fkey(e['key'], e['kind']): {
            'k': [[i % 3, 'Keyword']], 'd': [[1, 'Director']], 'a': [[i, 'Actor']],
            'n': [[1, 'Network']], 'c': [[2, 'Studio']], 'g': ['Drama'],
            't': ['Mystery'], 's': 'original', 'x': {'cozy_case': 3, 'dark': 2 if i % 2 else 1}}
            for i, e in enumerate(self.profile['entries'])}
        self.df = Counter(fid for rec in self.store.values() for fid, _ in self.s.facet_pairs(rec))

    def old_affinities(self, entries, store, df, n_all, hits, loose=False):
        s = self.s
        pos, neg, npos = {}, {}, {}
        xtot = 0.0
        for e in entries:
            rec = store.get(s.fkey(e['key'], e['kind']))
            if not rec:
                continue
            w = s.entry_weight(e) + (1.5 if e['key'] in hits and not loose else 0)
            if w > 0 and rec.get('x'):
                xtot += w
            for fid, _ in s.facet_pairs(rec):
                if w > 0:
                    pos[fid] = pos.get(fid, 0) + w
                    npos[fid] = npos.get(fid, 0) + 1
                else:
                    neg[fid] = neg.get(fid, 0) - w
        aff = {}
        n_x = sum(1 for r in store.values() if r.get('x')) or 1
        for fid in set(pos) | set(neg):
            p_, n_ = pos.get(fid, 0), neg.get(fid, 0)
            if npos.get(fid, 0) < (1 if loose else s.FACET_SUPPORT[fid[0]]) and not n_:
                continue
            if fid[0] == 'x':
                lift = (p_ / (xtot or 1) + 0.01) / (df.get(fid, 0) / n_x + 0.01)
                if loose:
                    aff[fid] = s.FACET_W['x'] * (max(0, min(1.5, math.log(lift))) * min(1, p_/2) - .5*n_/(p_+n_+1))
                    continue
                aff[fid] = s.FACET_W['x'] * (max(-1.0, min(1.5, math.log(lift))) * min(1.0, npos.get(fid, 0) / (1 if loose else 4))
                                           - 0.5 * n_ / (p_ + n_ + 1.0))
                continue
            idf = math.log(n_all / (df.get(fid, 0) + 1)) / math.log(n_all)
            aff[fid] = (p_ - n_) / (p_ + n_ + 1.0) * math.log1p(max(1, npos.get(fid, 0))) * max(idf, 0.05) * s.FACET_W[fid[0]]
        return aff, npos

    def test_household_bytes_match_old_formula(self):
        entries = self.profile['entries']
        for loose in (False, True):
            for weights in (None, 0, -2, .07, 3):
                xs = entries if weights is None else [{**e, 'weight': weights} for e in entries]
                args = (xs, self.store, self.df, len(self.store)+1, {entries[0]['key']}, loose)
                with self.subTest(loose=loose, weights=weights):
                    old = json.dumps(self.old_affinities(*args), sort_keys=True).encode()
                    self.assertEqual(json.dumps(self.s.affinities(*args), sort_keys=True).encode(), old)
                    self.assertEqual(json.dumps(self.s.affinities(*args, person=False), sort_keys=True).encode(), old)

    def test_strict_support_ramp(self):
        for t, sup in self.s.FACET_SUPPORT.items():
            if t == 'x':
                continue
            for npos, ramp in ((sup-1, None), (sup, .5), (2*sup, 1), (3*sup, 1)):
                args = (t+':fixture', 3*npos, 0, npos, 30, 2, 10, 20)
                value = self.s.facet_value(*args, person=True)
                if ramp is None:
                    self.assertIsNone(value)
                else:
                    self.assertEqual(value, self.s.facet_value(*args)*ramp)
                    self.assertEqual(self.s.facet_value(*args, loose=True, person=True),
                                     self.s.facet_value(*args, loose=True))

    def test_x_prior_zero_and_large_mass(self):
        s = self.s
        for loose in (False, True):
            self.assertAlmostEqual(s.facet_value('x:dark', 0, 2, 0, 0, 2, 100, 101, loose, True), -.4)
            self.assertEqual(s.facet_value('x:dark', 0, 0, 0, 0, 2, 100, 101, loose, True), None)
            for xtot, p_ in ((3, 3), (1000000, 40000)):
                share = (p_ + 20*.02)/(xtot+20)
                expected = 1.2 * min(1.5, math.log((share+.01)/(.02+.01)))
                self.assertAlmostEqual(s.facet_value('x:dark', p_, 0, 4, xtot, 2, 100, 101, loose, True), expected)
            old = s.facet_value('x:dark', 40000, 0, 4, 1000000, 2, 100, 101, loose)
            new = s.facet_value('x:dark', 40000, 0, 4, 1000000, 2, 100, 101, loose, True)
            self.assertLess(abs(new-old), .00001)

    def test_facet_value_agrees_with_fixture_affinities(self):
        s, entries = self.s, self.profile['entries']
        self.assertTrue(entries)
        for loose in (False, True):
            pos, neg, npos, xtot = Counter(), Counter(), Counter(), 0.
            for e in entries:
                rec = self.store[s.fkey(e['key'], e['kind'])]
                w = s.entry_weight(e)
                if w > 0 and rec.get('x'):
                    xtot += w
                for fid, _ in s.facet_pairs(rec):
                    if w > 0:
                        pos[fid] += w
                        npos[fid] += 1
                    else:
                        neg[fid] -= w
            for person in (False, True):
                aff, counts = s.affinities(entries, self.store, self.df, len(self.store)+1, set(), loose, person)
                self.assertEqual(counts, dict(npos))
                for fid in self.df:
                    value = s.facet_value(fid, pos[fid], neg[fid], npos[fid], xtot, self.df[fid],
                                          len(self.store), len(self.store)+1, loose, person)
                    self.assertEqual(aff.get(fid), value, fid)

    def test_animation_sink_zero_one_two_seeds(self):
        s = self.s
        metas = [{'id': 'anim'}, {'id': 'live'}]
        with patch.object(s, 'row_facts', side_effect=lambda x: {'g': ['Animation'] if x['id'] == 'anim' else []}), \
                patch.object(s, 'frec', side_effect=lambda k, *a: {'g': ['Animation'] if k.startswith('anim') else []}):
            for n in (0, 1, 2):
                seeds = ['live'] + [f'anim{i}' for i in range(n)]
                with patch.object(s, 'get_model', return_value={'nexp': {'ana': 4}}), \
                        patch.object(s, 'explicit_seeds', return_value=seeds):
                    self.assertEqual(s.person_animation_sink(metas, 'ana'), metas[::-1] if n < 2 else metas)
                    self.assertEqual(s.person_animation_sink(metas, 'ana', seeds), metas[::-1] if n < 2 else metas)
            with patch.object(s, 'get_model', return_value={'nexp': {'ana': 20}}):
                self.assertIs(s.person_animation_sink(metas, 'ana', ['live']), metas)
            with patch.object(s, 'get_model', return_value={'nexp': {'ana': 4}}):
                self.assertIs(s.person_animation_sink(metas, 'ana', []), metas)

    def test_detail_uses_unrounded_row_tau(self):
        before = list(range(20))
        after = before[:8][::-1] + [9, 8] + before[10:]
        changes = reaction_detail.row_changes({'edge': before, 'related': [100]}, {'edge': after, 'related': [100]}, {100})
        self.assertEqual(set(changes), {'edge'})
        self.assertEqual(changes['edge']['departures'], 0)
        self.assertAlmostEqual(changes['edge']['tau'], 1-58/190)
        self.assertLess(changes['edge']['tau'], .70)


class ReactionDetailW15(TasteCase):
    def test_feasibility_direction_and_frozen_limit(self):
        reaction = reaction_detail.reaction
        pre = {rid('foryou', self.s.CONFIG): [f'tmdb:{i}' for i in range(3, 15)]}
        before = [f'tmdb:{i}' for i in range(3, 84) if i != 20]
        self.e._model['paff']['ben'] = {'x:cozy_case': .1}
        after_model = {**self.e._model, 'paff': {**self.e._model['paff'], 'ben': {'x:cozy_case': 1.8}}}
        p = {**self.e._profile, 'seen': {'tmdb:1', 'tmdb:2', 'tmdb:20'}}
        for start, feasible in ((30, True), (5, False)):
            related = set(before[start:start+2])
            after = before[start:start+2] + [k for k in before if k not in related]
            with patch.object(reaction, 'feedback_model', return_value=(p, after_model, self.s.feedback_state())), \
                    patch.object(reaction, 'served', side_effect=[pre, {rid('foryou', self.s.CONFIG): []}]), \
                    patch.object(self.e, '_rank', side_effect=[before, after]), patch.object(reaction, 'latency', return_value=1):
                result = reaction_detail.measure(self.e, 'tmdb:20', 'ben', related, pre, 2.)
            self.assertEqual(result['feasible'], feasible)
            self.assertEqual(result['checks']['K1feas'], True if feasible else None)
            self.assertTrue(result['checks']['K1dir'])
            self.assertEqual(result['values']['K1dir'], start)
            self.assertEqual(result['values']['R90'], 1.)
            self.assertTrue(result['checks']['K3F'])
            self.assertEqual(result['K3_limit'], 2.)
            self.assertAlmostEqual(result['values']['K3F'], 1.7)


if __name__ == '__main__':
    unittest.main()
