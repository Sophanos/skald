"""R3 lane depth, coherent reserves and bench composition; no network."""
from skald.members import rid
from contextlib import ExitStack, redirect_stdout
import copy
import io
import tempfile
import unittest
from unittest.mock import patch

from eval.profile_golden import ROOT, offline_server


def cards(n, start=0):
    return [dict(id=f'tmdb:{start+i}', name=f'Synthetic {start+i} Card', type='movie', poster='p', background='b') for i in range(n)]


class LanesR3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        s = self.s
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cfg = copy.deepcopy(s.CONFIG)
        cfg['household']['row_order'] = []
        self.stack.enter_context(patch.object(s, 'CONFIG', cfg))
        for store in (s._feeds, s._shown, s._facets, s._home_cache):
            self.stack.enter_context(patch.dict(store, {}, clear=True))
        self.stack.enter_context(patch.dict(s._profiles, {'household': {'seen': set(), 'negative': set(), 'entries': []}}, clear=True))
        self.stack.enter_context(patch.dict(s._learn, {'lanes': {}, 'rows': {}, 'served': {}}, clear=True))
        for name, value in {'remote_masks': (set(), set()), 'get_model': {'test': True}, 'score_key': (None, []),
                            'tired_keys': {}, 'speaks': True, 'because_seed': None}.items():
            self.stack.enter_context(patch.object(s, name, return_value=value))
        self.stack.enter_context(patch.object(s, 'with_match', side_effect=lambda x: x))
        self.stack.enter_context(patch.object(s, 'person_order', side_effect=lambda xs, *a: xs))
        self.stack.enter_context(patch.object(s, 'person_animation_sink', side_effect=lambda xs, *a: xs))

    def facet(self, xs, fid):
        t, value = fid.split(':', 1)
        for x in xs:
            rec = self.s._facets.setdefault(x['id'], {'kind': 'movie'})
            if t == 'x':
                rec.setdefault('x', {})[value] = 2
            else:
                rec.setdefault(t, []).append([int(value), fid])

    def put(self, cid, xs, fid, pool=()):
        self.facet(xs + list(pool), fid)
        self.s._feeds[cid, 'de-DE'] = {'metas': {'tv': xs, 'pool': list(pool)},
                                       'names': {'title': f'Title {fid}', 'lane': fid}}

    def build(self, groups, source=None):
        s = self.s
        model = {'aff': {fid: len(groups)-i for i, fid in enumerate(groups)}, 'names': {fid: 'HBO' if fid[0] in 'nc' else fid for fid in groups}}
        for fid, xs in groups.items():
            self.facet(xs, fid)
        with patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'lane_titles', side_effect=lambda fid, lang, **kw: [(x['id'], 'movie', x) for x in groups[fid]]), \
                patch.object(s, 'rank_by_taste', side_effect=lambda m, xs: [(None, [], *x) for x in xs]), \
                patch.object(s, 'themed_row', side_effect=source or (lambda m, p, theme, items, *a, **kw: items)), \
                patch.object(s, 'lane_title', side_effect=lambda fid, *a: f'Title {fid}'), redirect_stdout(io.StringIO()):
            return s.lanes_feed('de-DE')

    def test_build_excludes_every_built_front_and_all_moods(self):
        s, front, deep = self.s, [], []
        s.CONFIG['household']['home'] = {'family_caps': {'mood': len(s.MOODS_ROWS)}}
        for i, cid in enumerate(s.DEDUPE_ORDER[:s.DEDUPE_ORDER.index(s.LANES[0])]):
            if not s.row_enabled(cid) or cid in s.READY_ROWS:
                continue
            xs = cards(14, 1000+i*100)
            rows = {'movie': xs, 'series': []} if cid in (rid('top10', s.CONFIG), rid('toprated', s.CONFIG)) else {'series': xs} if cid == rid('episodes', s.CONFIG) else {'tv': xs}
            s._feeds[cid, 'de-DE'] = {'metas': rows}
            front += xs if cid in s.MOODS_ROWS else xs[:12]
            if cid not in s.MOODS_ROWS:
                deep += xs[12:]
        # A stale pin is not the new build's exclusion source.
        s._shown[rid('week', s.CONFIG), 'de-DE'] = {'metas': {'tv': []}}
        out = self.build({'k:1': front + deep + cards(60)})
        chosen = out[s.LANES[0]]['tv']
        self.assertEqual(len(chosen), 40)
        self.assertFalse({x['id'] for x in chosen} & {x['id'] for x in front})
        self.assertTrue({x['id'] for x in chosen} & {x['id'] for x in deep})

    def test_build_depth_reserve_and_sixteen_floor(self):
        out = self.build({'k:1': cards(80)})
        self.assertEqual(len(out[rid('lane1', self.s.CONFIG)]['tv']), 40)
        self.assertEqual(len(out[rid('lane1', self.s.CONFIG)]['pool']), 80)
        self.assertEqual(self.build({'k:1': cards(15)})['_names'], {})
        self.assertIn(rid('lane1', self.s.CONFIG), self.build({'k:1': cards(16)})['_names'])

    def test_hidden_moods_do_not_reserve_lane_titles(self):
        s = self.s
        for cap, shared in ((0, 1), (1, 2)):
            s._feeds.clear()
            s.CONFIG['household']['home'] = {'family_caps': {'mood': cap}}
            for i in (1, 2):
                s._feeds[s.rid('mood'+str(i)), 'de-DE'] = {'metas': {'tv': cards(16, i*100)}}
            with self.subTest(cap=cap):
                out = self.build({'k:1': cards(16, shared*100)})
                self.assertEqual(len(out[s.LANES[0]]['tv']), 16)
        s.CONFIG['household']['rows'] = ['moods', 'lanes']
        s.CONFIG['household']['home']['family_caps']['mood'] = 0
        self.assertEqual(self.build({'k:1': cards(16, 100)})[s.LANES[0]]['tv'], [])

    def test_source_status_follows_selected_theme_to_final_slot(self):
        from skald import rowplan
        s = self.s
        self.put(s.LANES[1], [], 'k:1')
        sourced = []
        def source(m, p, theme, items, lang, cid, **kw):
            sourced.append(theme['key'])
            rowplan.source_status(s, cid, lang, date='2026-10-11',
                                  reused=theme['key'] == 'k:2', failed=theme['key'] == 'd:3')
            return items
        with patch.dict(s._row_source_status, {}, clear=True):
            out = self.build({'k:1': cards(16, 100), 'k:2': cards(16, 200), 'd:3': cards(1, 300)}, source)
            self.assertEqual(sourced, ['k:1', 'k:2', 'd:3'])
            self.assertEqual(out['_names'][s.LANES[1]]['lane'], 'k:1')
            self.assertTrue(s._row_source_status[s.LANES[1], 'de-DE']['fresh'])
            self.assertEqual(out['_names'][s.LANES[0]]['lane'], 'k:2')
            self.assertTrue(s._row_source_status[s.LANES[0], 'de-DE']['reused_today'])
            self.assertFalse(s._row_source_status[s.LANES[0], 'de-DE']['failed'])

    def test_build_earlier_lanes_exclude_the_whole_depth(self):
        xs = cards(40)
        out = self.build({'k:1': xs, 'k:2': xs + cards(20, 100)})
        first = {x['id'] for x in out[rid('lane1', self.s.CONFIG)]['tv']}
        second = {x['id'] for x in out[rid('lane2', self.s.CONFIG)]['tv']}
        self.assertEqual(len(second), 20)
        self.assertFalse(first & second)

    def test_anime_policy_parity(self):
        s, xs = self.s, cards(20)
        for x in xs[:11]:
            x['genres'] = ['Animation']
        for title, allowed in (('Animated Favorites', False), ('Anime Favorites', True)):
            for x in xs:
                x['_row'] = {'anime_lane': allowed}
            s._learn['lanes']['k:1'] = {'title': title}
            built = self.build({'k:1': xs})
            self.put(rid('lane1', s.CONFIG), xs, 'k:1')
            s._feeds[rid('lane1', s.CONFIG), 'de-DE']['names']['title'] = title
            served = s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set()))
            self.assertEqual(bool(built['_names']), allowed)
            self.assertEqual(bool(served), allowed)

    def test_build_two_bench_feeds(self):
        fids = ['x:hero_burden', 'x:war_sacrifice', 'k:1', 'k:2', 'd:1', 'c:1', 'x:team_spectacle']
        out = self.build({fid: cards(20, i*100) for i, fid in enumerate(fids)})
        self.assertEqual(len(out['_names']), 7)
        self.assertTrue(all(len(out[cid]['tv']) >= 16 for cid in self.s.LANE_ROWS))
        self.assertEqual(next(x[1] for x in self.s.job_specs(self.s.local_now(), {}) if x[0] == 'lanes'), self.s.LANE_ROWS)

    def test_universe_lane_keeps_depth_and_franchise_cap(self):
        s, xs = self.s, cards(20)
        self.put(rid('lane1', s.CONFIG), xs, 'k:1')
        with patch.object(s, 'universe_key', return_value='marvel'), \
                patch.object(s, 'franchise_key', side_effect=lambda x: 'same' if x in xs[:3] else x['id']):
            row = s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set()))
        self.assertEqual(len(row), 19)
        self.assertEqual(sum(x in xs[:3] for x in row), 2)

    def test_animation_guard_after_twenty_card_limit(self):
        s, xs = self.s, cards(30)
        for x in xs:
            x['_row'] = {'anime_lane': False}
        for x in xs[:11]:
            x['genres'] = ['Animation']
        self.put(rid('lane1', s.CONFIG), xs, 'k:1')
        self.assertTrue(s.lane_allowed(xs, 'Animated Favorites'))
        self.assertEqual(s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set())), [])

    def test_legacy_anime_pin_serves_unchanged_until_rebuilt(self):
        s, xs = self.s, cards(20)
        for x in xs:
            x['genres'] = ['Animation']
        self.put(s.LANES[0], xs, 'k:1')
        s._feeds[s.LANES[0], 'de-DE']['names']['title'] = 'Anime Favorites'
        before = copy.deepcopy(s._feeds[s.LANES[0], 'de-DE'])
        with patch.object(s, 'is_anime', return_value=True), patch.object(s, 'row_eligible', return_value=True):
            row = s.served_row(s.LANES[0], 'tv', 'de-DE', s._profiles['household'], set(), (set(), set()))
        self.assertEqual(len(row), 20)
        self.assertEqual(s._feeds[s.LANES[0], 'de-DE'], before)

    def test_bench_title_learning_tired_and_no_reuse(self):
        s = self.s
        self.put(rid('lane1', s.CONFIG), cards(4), 'k:1')
        self.put(s.LANE_BENCH[0], cards(5, 100), 'k:2')
        self.put(s.LANE_BENCH[1], cards(16, 200), 'k:3')
        with tempfile.TemporaryDirectory() as temp, patch.object(s, 'DATA', temp), patch.object(s, 'DEDUPE_ORDER', s.LANES), patch.object(s, 'LANG', 'de-DE'):
            s.pin_shown('de-DE')
        with patch.object(s, 'tired_keys', return_value={}) as tired:
            row = s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set()))
        self.assertEqual(row.names['title'], 'Title k:3')
        self.assertEqual(row.names['lane'], 'k:3')
        self.assertIn(({'lane:k:3'},), [c.args for c in tired.call_args_list])
        above = s.mark_above(set(), rid('lane1', s.CONFIG), row)
        again = s.served_row(rid('lane2', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], above, (set(), set()))
        self.assertEqual(again, [])
        with patch.object(s, 'row_metas', return_value=s.human_cards(row)), patch.object(s, 'note_exposure'):
            self.assertEqual(dict((cid, title) for cid, _, title in s.feed_rows('de-DE'))[rid('lane1', s.CONFIG)], 'Title k:3')
            s.note_served(s.learn_id(rid('lane1', s.CONFIG), 'de-DE'), row)
        self.assertEqual(set(s._learn['rows']), {'lane:k:3'})
        self.assertTrue(all(set(x['rows']) == {'lane:k:3'} for x in s._learn['served'].values()))

    def test_pool_and_bench_persist_and_refill_in_score_order(self):
        s = self.s
        self.put(rid('lane1', s.CONFIG), cards(4), 'k:1', cards(16, 100))
        self.put(s.LANE_BENCH[0], cards(16, 200), 'k:2', cards(3, 300))
        before = copy.deepcopy(s._feeds)
        s.save_feeds()
        s._feeds.clear()
        with patch.object(s, 'get_model', return_value=None), patch.object(s, 'load_facets'), patch.object(s, 'load_emb'):
            s.load_state()
        self.assertEqual(s._feeds, before)
        self.assertEqual(len(s.source_metas(rid('lane1', s.CONFIG), 'de-DE')), 20)
        with patch.object(s, 'score_key', side_effect=lambda m, key, *a: (int(key[5:]), [])):
            row = s.fill_row(cards(4), rid('lane1', s.CONFIG), 'de-DE', s._profiles['household'], set(), (set(), set()))
        self.assertEqual([x['id'] for x in row[4:]], [f'tmdb:{i}' for i in range(115, 99, -1)])

    def test_coherence_initial_own_and_borrowed(self):
        s, good, rogue = self.s, cards(16), cards(3, 100)
        self.put(rid('lane1', s.CONFIG), good, 'x:hero_burden', rogue)
        for x in rogue:
            s._facets[x['id']]['x']['hero_burden'] = 1
        s._feeds[rid('lane1', s.CONFIG), 'de-DE']['metas']['tv'] += rogue
        self.put(rid('lane2', s.CONFIG), cards(8, 200), 'x:war_sacrifice')
        row = s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set()))
        self.assertEqual(len(row), 16)
        self.assertTrue(all(s.lane_member(x, row.names['lane']) for x in row))
        self.assertFalse({x['id'] for x in row} & {x['id'] for x in rogue})

    def test_missing_facet_cannot_serve_a_lane(self):
        s = self.s
        s._feeds[rid('lane1', s.CONFIG), 'de-DE'] = {'metas': {'tv': cards(20)}, 'names': {'title': 'Synthetic'}}
        self.assertEqual(s.served_row(rid('lane1', s.CONFIG), 'tv', 'de-DE', s._profiles['household'], set(), (set(), set())), [])

    def test_bench_cache_identity_all_sessions_and_types(self):
        s = self.s
        self.put(rid('lane1', s.CONFIG), cards(4), 'k:1')
        self.put(s.LANE_BENCH[0], cards(16, 100), 'k:2')
        with patch.object(s, 'DEDUPE_ORDER', s.LANES), patch.object(s, 'LANG', 'de-DE'):
            with tempfile.TemporaryDirectory() as temp, patch.object(s, 'DATA', temp):
                s.pin_shown('de-DE')
            for who in ('none', 'ana', 'ben', 'both'):
                for typ in s.MIX:
                    cached = s.row_metas(rid('lane1', s.CONFIG), typ, 'de-DE', who=who)
                    with patch.object(s, '_HOME_CACHE_ENABLED', False):
                        plain = s.row_metas(rid('lane1', s.CONFIG), typ, 'de-DE', who=who)
                    self.assertEqual(cached, plain)
                    self.assertEqual(cached.names, plain.names)
                    self.assertEqual(cached.names['lane'], 'k:2')


if __name__ == '__main__':
    unittest.main()
