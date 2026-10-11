"""Told reserves and viewer isolation, using synthetic offline homes."""
from skald.members import rid
from contextlib import ExitStack, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import threading
import unittest
from unittest.mock import patch

from eval.profile_golden import NOW, ROOT, offline_server


class MemoryServedTests(unittest.TestCase):
    def setUp(self):
        ctx = offline_server(ROOT/'server.py')
        self.s = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        s = self.s
        self.cards = [dict(id=f'tmdb:{i}', name=f'Synthetic Card {i}', type='movie', poster='p', background='b') for i in range(30)]
        s._facets = {x['id']: {'kind': 'movie', 'g': ['Crime'], 'k': [[i % 6+1, f'Synthetic facet {i % 6+1}']],
                               'x': {'witty_banter': 2}} for i, x in enumerate(self.cards)}
        entries = []
        for j, person in enumerate(s.PEOPLE):
            for i in range(4):
                key = f'tmdb:{100+j*4+i}'
                s._facets[key] = {'kind': 'movie', 'g': ['Crime'], 'k': [[1, 'Synthetic facet 1']], 'x': {'witty_banter': 2}}
                entries.append(dict(key=key, kind='movie', title=key, year='2000', weight=2., by=[person],
                                    who='told', signal='loved', genres=['Crime'], item={}))
        self.p = {'entries': entries, 'seen': {e['key'] for e in entries}, 'hash': 'synthetic'}
        s._profiles['household'] = self.p
        s._memory = s._memory_empty()
        s._memory_index.clear()
        s._memory_veto.clear()
        self.build()

    def build(self):
        with redirect_stdout(io.StringIO()):
            m = self.s.build_model(self.p)
        self.s._model['m'] = m
        return m

    def fact(self, **kw):
        return {'person': 'ben', 'status': 'active', 'kind': 'like', 'src': 'told', 'strength': 1., 'about': 'k:1', **kw}

    def facts(self, *facts):
        self.s._memory = {**self.s._memory_empty(), 'facts': list(facts)}

    def reserve_pool(self):
        s = self.s
        s._facets = {x['id']: {'kind': 'movie', 'k': []} for x in self.cards}
        for i in range(10, 16):
            s._facets[f'tmdb:{i}']['k'] = [[(i-10)//2+1, 'Synthetic']]
        s._memory_veto.clear()
        return self.cards

    def test_reserve_lead_cap_slot_and_identity(self):
        xs = self.reserve_pool()
        self.facts(*(self.fact(about=f'k:{i}') for i in (1, 2, 3)))
        before = copy.deepcopy(xs)
        out = self.s.memory_reserve(xs, 'ben')
        self.assertEqual(out[:5], xs[:5])
        self.assertEqual(out[5:10], xs[10:15])
        self.assertEqual(out[10:], xs[5:10]+xs[15:])
        self.assertEqual(xs, before)
        self.assertEqual({id(x) for x in out}, {id(x) for x in xs})
        self.facts()
        with patch.object(self.s, '_memory_breadth', side_effect=AssertionError('empty scan')):
            self.assertIs(self.s.memory_reserve(xs, 'ben'), xs)

    def test_reserve_filters_and_anime(self):
        xs, s = self.reserve_pool(), self.s
        for change in ({'status': 'proposed'}, {'kind': 'avoid'}, {'src': 'implicit'},
                       {'strength': .79}, {'person': 'ana'}):
            f = self.fact()
            f.update(change)
            self.facts(f)
            self.assertIs(s.memory_reserve(xs, 'ben'), xs)
        self.facts(self.fact())
        with patch.object(s, '_memory_breadth', return_value={'k:1': .31}):
            self.assertIs(s.memory_reserve(xs, 'ben'), xs)
        with patch.object(s, 'is_anime', side_effect=lambda x: x is xs[10]):
            out = s.memory_reserve(xs, 'ben')
        self.assertIs(out[5], xs[11])
        self.assertNotIn(xs[10], out[:10])
        self.assertIs(s.memory_reserve(xs, 'both'), xs)

    def test_reserve_counts_leads_and_shared_carriers(self):
        xs, s = self.reserve_pool(), self.s
        s._facets['tmdb:0']['k'] = [[1, 'Synthetic']]
        s._facets['tmdb:10']['k'].append([2, 'Synthetic'])
        self.facts(self.fact(), self.fact(about='k:2'))
        out = s.memory_reserve(xs, 'ben')
        self.assertEqual(out[5:7], [xs[10], xs[12]])
        s._facets['tmdb:1']['k'] = [[1, 'Synthetic']]
        self.facts(self.fact())
        self.assertIs(s.memory_reserve(xs, 'ben'), xs)

    def told_model(self, person, kind):
        self.s.memory_save(self.s._memory_empty(), [])
        for fid in (['k:1'] if kind == 'avoid' else ['k:2', 'k:3', 'k:4']):
            self.s.memory_tell(person, kind, fid, now=NOW)
        return self.build()

    def home(self, who):
        s, out, above = self.s, {}, set()
        cids = (rid('week', s.CONFIG), rid('ana', s.CONFIG), rid('foryou', s.CONFIG), rid('hbo', s.CONFIG))
        with ExitStack() as stack:
            for name, value in {'row_eligible': True, 'row_member': False, 'feed': {}, 'tired_keys': {}, 'explicit_seeds': []}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'row_list', side_effect=lambda cid, *a: self.cards[:6] if cid == rid('week', s.CONFIG) else self.cards))
            stack.enter_context(patch.object(s, 'fill_row', side_effect=lambda xs, *a: xs))
            masks = (set(), set(), s.veto_keys(who, NOW))
            for cid in cids:
                metas = s.served_row(cid, 'tv', 'de-DE', self.p, above, masks, who)
                out[cid] = s.human_cards(metas)
                above = s.mark_above(above, cid, metas)
        return json.dumps(out, sort_keys=True).encode()

    def test_p0_empty_memory_and_both_with_memory(self):
        s = self.s
        for memory in (False, True):
            if memory:
                self.told_model('ana', 'like')
            for who in (('both',) if memory else ('none', 'both', *s.PEOPLE)):
                with patch.object(s, 'person_model', side_effect=lambda m, p: m), \
                        patch.object(s, 'memory_reserve', side_effect=lambda xs, who: xs):
                    before = self.home(who)
                self.assertEqual(before, self.home(who))

    def test_v1_other_home_with_avoid_and_three_likes(self):
        s = self.s
        baseline = {who: self.home(who) for who in s.PEOPLE}
        leaked = 0
        for person in s.PEOPLE:
            other = next(p for p in s.PEOPLE if p != person)
            for kind in ('avoid', 'like'):
                self.told_model(person, kind)
                self.assertEqual(baseline[other], self.home(other), (person, kind))
                with patch.object(s, 'person_model', side_effect=lambda m, p: m):
                    leaked += baseline[other] != self.home(other)
        self.assertGreater(leaked, 0, 'fixture must reproduce the leak without the seam')

    def test_mem0_calibration_matches_empty_model(self):
        s = self.s
        with patch.object(s, 'tmdb', side_effect=lambda path, **kw: {'results': [{'id': i} for i in range(30)] if '/movie/' in path else []}):
            before = self.build()
            for kind in ('like', 'avoid'):
                after = self.told_model('ben', kind)
                for person in s.PEOPLE:
                    for name in ('paff', 'pbscale', 'pbg'):
                        self.assertEqual(after['mem0'][person][name], before[name][person])
        self.assertGreaterEqual(len(after['mem0']['ben']['pbg']), 20)
        self.assertNotEqual(after['paff']['ben'], before['paff']['ben'])

    def test_mem0_keeps_weak_experiences_before_an_avoid(self):
        s = self.s
        for e in self.p['entries']:
            s._facets[e['key']]['x']['found_family'] = 1
        before = self.build()
        s.memory_tell('ben', 'avoid', 'x:found_family', now=NOW)
        after = self.build()
        self.assertEqual(after['mem0']['ben']['pbscale'], before['pbscale']['ben'])
        rec = s._facets[self.p['entries'][0]['key']]
        self.assertLess(s.facet_bonus(after, rec, after['paff']['ben'])[0],
                        s.facet_bonus(before, rec, before['paff']['ben'])[0])

    def test_view_cache_scope_retention_replacement_and_idempotence(self):
        s = self.s
        m = self.told_model('ana', 'like')
        original = copy.deepcopy(m)
        for viewer in (None, 'none', 'both', 'ana'):
            token = s._viewer.set(viewer)
            try:
                self.assertIs(s.person_model(m, 'ana'), m)
            finally:
                s._viewer.reset(token)
        token = s._viewer.set('ben')
        try:
            view = s.person_model(m, 'ana')
            self.assertIs(s.person_model(m, 'ana'), view)
            self.assertIs(s.person_model(view, 'ana'), view)
            self.assertIs(s.person_model(m, 'ben'), m)
            for name in ('paff', 'pbscale', 'pbg'):
                self.assertIs(view[name]['ana'], m['mem0']['ana'][name])
                self.assertIs(view[name].get('ben'), m[name].get('ben'))
            self.assertEqual(m, original)
            self.assertIs(s._person_models[id(m), 'ana'][0], m)
            s._viewer.set('ana')
            s.person_model(m, 'ben')
            self.assertEqual(len(s._person_models), len(s.PEOPLE))
            newer = copy.deepcopy(m)
            s.person_model(newer, 'ben')
            self.assertEqual(list(s._person_models), [(id(newer), 'ben')])
            self.assertIs(s._person_models[id(newer), 'ben'][0], newer)
            s.person_model({'paff': {}}, 'ben')
            self.assertFalse(s._person_models)
        finally:
            s._viewer.reset(token)

    def test_viewer_reset_nested_and_exception(self):
        s = self.s
        token = s._viewer.set('ana')
        try:
            def fail(*a):
                self.assertEqual(s._viewer.get(), 'ben')
                raise LookupError('synthetic')
            with patch.object(s, 'row_list', side_effect=fail), self.assertRaises(LookupError):
                s.served_row(rid('foryou', s.CONFIG), 'tv', 'de-DE', {}, set(), (set(), set()), 'ben')
            self.assertEqual(s._viewer.get(), 'ana')
            self.home(None)
            self.assertEqual(s._viewer.get(), 'ana')
        finally:
            s._viewer.reset(token)
        self.assertIsNone(s._viewer.get())

    def test_concurrent_viewers_keep_their_own_memory(self):
        s, m = self.s, self.told_model('ana', 'like')
        ready = threading.Barrier(len(s.PEOPLE))
        def view(who):
            token = s._viewer.set(who)
            try:
                ready.wait(timeout=5)
                other = next(p for p in s.PEOPLE if p != who)
                self.assertIs(s.person_model(m, who), m)
                self.assertIs(s.person_model(m, other)['paff'][other], m['mem0'][other]['paff'])
                self.assertEqual(s._viewer.get(), who)
            finally:
                s._viewer.reset(token)
        with ThreadPoolExecutor(max_workers=len(s.PEOPLE)) as pool:
            list(pool.map(view, s.PEOPLE))
        self.assertIsNone(s._viewer.get())
        self.assertEqual(len(s._person_models), len(s.PEOPLE))

    def test_confidence_and_member_pick_use_strict_calibration(self):
        s, m = self.s, self.told_model('ana', 'like')
        token = s._viewer.set('ben')
        try:
            strict = s.person_model(m, 'ana')
            with patch.object(s, 'match_pct', return_value=80) as pct:
                s.confidence(m, 'tmdb:0', .2, [], 'ana')
                self.assertIs(pct.call_args.args[0], strict)
                with patch.object(s, 'procedural', return_value=True):
                    self.assertTrue(s.member_pick(self.cards[0], "ana"))
                self.assertIs(pct.call_args.args[0], strict)
        finally:
            s._viewer.reset(token)
