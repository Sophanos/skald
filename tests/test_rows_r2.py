"""R2 row contracts; synthetic and offline unless marked snapshot."""
from skald.members import rid
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from eval.profile_golden import ROOT, offline_server
from snapshot_support import require_snapshot
from eval.rows_report import SNAPSHOT, display_rows, home_server


def cards(n, start=0):
    return [dict(id=f'tmdb:{start+i}', name=f'Synthetic {start+i} Card', type='movie', poster='p', background='b') for i in range(n)]


class RowsR2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def served(self, cid, metas, above=None, who=None, typ='tv'):
        s = self.s
        with ExitStack() as stack:
            for name, value in {'row_eligible': True, 'row_related': True, 'row_member': False, 'get_model': None,
                                'feed': {}, 'tired_keys': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'row_list', side_effect=lambda c, *a: metas.copy() if c == cid else []))
            for name in ('fill_row', 'balance_row', 'polish_row', 'rotate_row', 'remote_order'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            p = {'entries': [{'key': x['id'], 'kind': 'movie' if x['type'] == 'movie' else 'tv',
                              'signal': 'watched', 'item': {}} for x in metas]}
            return s.served_row(cid, typ, 'de-DE', p, above if above is not None else set(), (set(), set()), who)

    def test_rewatch_dedupes_catchup_and_refills_own_candidates(self):
        s = self.s
        shared = [{**x, 'type': 'series'} for x in cards(2)]
        catchup = self.served(rid('catchup', s.CONFIG), shared)
        above = s.mark_above(set(), rid('catchup', s.CONFIG), catchup)
        own = cards(20, 100)
        self.assertEqual(self.served(rid('top10', s.CONFIG), shared, above, typ='series'), shared)
        self.assertEqual(self.served(rid('rewatch', s.CONFIG), shared + own, above), own)
        self.assertEqual(self.served(rid('rewatch', s.CONFIG), shared + own[:3], above), own[:3])

    def test_window_check_uses_scoped_catalog_viewer(self):
        s = self.s
        cid = rid('rewatch', s.CONFIG)
        check = SnapshotR2('test_snapshot_window_rule')
        check.s = s
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(s, 'feed_rows', return_value=[(cid, ('tv',), 'Watch Again')]), \
                patch.object(s, 'row_metas', return_value=[]) as row_metas:
            check.test_snapshot_window_rule()
        self.assertEqual([c.kwargs for c in row_metas.call_args_list],
                         [{}] + [{'who': s.SCOPES[scope]} for scope in sorted(s.SCOPES)])

    def test_window_allows_deep_but_blocks_front(self):
        # A visible mood can reuse an unseen deep card, while its front stays distinct.
        s = self.s
        row = cards(20)
        above = s.mark_above(set(), rid('foryou', s.CONFIG), row)
        result = self.served(rid('mood1', s.CONFIG), [row[2], row[14]] + cards(5, 100), above)
        self.assertNotIn(row[2], result)
        self.assertIn(row[14], result)

    def test_window_rows_never_share(self):
        # Full window shelves block every card in later moods and lanes.
        s = self.s
        row = cards(20)
        above = s.mark_above(set(), rid('mood1', s.CONFIG), row)
        for cid in (rid('mood2', s.CONFIG), rid('lane1', s.CONFIG)):
            self.assertFalse({x['id'] for x in self.served(cid, row + cards(6, 100), above)} & {x['id'] for x in row})

    def test_strict_rows_block_deep_cards(self):
        # Changing window rows must not weaken strict dedupe for the other shelves.
        s = self.s
        row = cards(20)
        above = s.mark_above(set(), rid('foryou', s.CONFIG), row)
        self.assertEqual(self.served(rid('hbo', s.CONFIG), [row[14]] + cards(6, 100), above), cards(6, 100))

    def test_hide_five_keep_six(self):
        # Shelf visibility is based on servable titles, with an inclusive six-card floor.
        for cid in self.s.MOODS_ROWS + self.s.LANES:
            self.assertEqual(self.served(cid, cards(5)), [])
            self.assertEqual(len(self.served(cid, cards(6))), 6)

    def test_person_alpha_floor_with_seeds(self):
        # Explicit seed evidence makes a sparse person's active session visible.
        s = self.s
        with patch.object(s, 'explicit_seeds', return_value=['seed']):
            for n in (0, 4, 20, 40, 309):
                self.assertEqual(s.person_alpha({'nexp': {'ana': n}}, 'ana'), .5)
            self.assertEqual(s.person_alpha({'nexp': {}}, 'both'), 0)
            self.assertEqual(s.person_alpha(None, 'ana'), 0)

    def test_person_no_seeds_identity(self):
        # A session without explicit seeds cannot invent person evidence.
        s, metas = self.s, cards(24)
        with patch.object(s, 'get_model', return_value={'nexp': {'ana': 40}}), patch.object(s, 'explicit_seeds', return_value=[]):
            self.assertIs(s.person_order(metas, 'ana'), metas)

    def test_person_blend_thresholds_and_memo(self):
        # Seed ranks dominate sparse taste, then rich evidence uses the old person scorer.
        s, metas = self.s, cards(8)
        self.assertEqual([s.member_weight(n) for n in (4, 20, 40)], [.8, .5, .2])
        for n in (4, 20, 40):
            model = {'nexp': {'ana': n}}
            with patch.object(s, 'get_model', return_value=model), patch.object(s, 'explicit_seeds', return_value=['seed']), \
                    patch.object(s, 'frec', return_value={}), \
                    patch.object(s, 'score_key', side_effect=lambda m, k, *a: (-int(k[5:]),)) as score, \
                    patch.object(s, 'seed_knn', side_effect=lambda seeds, k, n: 100 if k == 'tmdb:4' else -int(k[5:])) as knn:
                first = s.person_order(metas, 'ana')
                self.assertEqual(s.person_order(metas, 'ana'), first)
                self.assertEqual(score.call_count, len(metas))
                self.assertEqual(knn.call_count, len(metas) if n < 20 else 0)
                if n < 20:
                    self.assertLess(first.index(metas[4]), 4)
                else:
                    self.assertEqual(first.index(metas[4]), 4)

    def test_ben_old_order(self):
        # The evidence-rich Ben ordering keeps the existing half-rank interpolation.
        s, metas = self.s, cards(24)
        with patch.object(s, 'get_model', return_value={'nexp': {'ben': 309}}), \
                patch.object(s, 'explicit_seeds', return_value=['seed']), patch.object(s, 'seed_knn') as knn, \
                patch.object(s, 'score_key', side_effect=lambda m, k, *a: (100 if k == 'tmdb:17' else -int(k[5:]),)):
            self.assertEqual(s.person_order(metas, 'ben').index(metas[17]), 9)
            knn.assert_not_called()

    def test_animation_sink_on_and_off(self):
        # Animation goes behind live action until a sparse person has two Animation seeds.
        s, metas = self.s, cards(8)
        metas[0]['genres'] = ['Animation']
        for animated in (0, 1, 2):
            with patch.object(s, 'get_model', return_value={'nexp': {'ana': 4}}), \
                    patch.object(s, 'explicit_seeds', return_value=['seed', 'seed2']), \
                    patch.object(s, 'frec', side_effect=lambda k, *a: {'g': ['Animation']} if k in ['seed', 'seed2'][:animated] else {}), \
                    patch.object(s, 'score_key', return_value=(0,)), patch.object(s, 'seed_knn', return_value=0):
                self.assertEqual(s.person_order(metas, 'ana').index(metas[0]), 0 if animated == 2 else 7)

    def tail_gate(self, similarity=.5, tone=True, joint=1, n=4):
        s = self.s
        with patch.object(s, 'get_model', return_value={'nexp': {'ana': n, 'ben': 309}}), \
                patch.object(s, 'tone_ok', return_value=tone), patch.object(s, 'score_key', return_value=(joint,)), \
                patch.object(s, 'seed_knn', return_value=similarity):
            return s.week_tail_ok(cards(1)[0], {'ana': ['seed'], 'ben': []})

    def test_week_tail_floor(self):
        # Tail evidence includes .45 but still excludes cards eligible for the strict head.
        self.assertTrue(self.tail_gate(.45))
        self.assertFalse(self.tail_gate(.449))
        self.assertFalse(self.tail_gate(.64))
        self.assertFalse(self.tail_gate(.5, n=20))

    def test_week_full_strict_head_keeps_twenty(self):
        # More evidence can fill Week with strict picks without being capped at fourteen.
        self.assertEqual(self.served(rid('week', self.s.CONFIG), cards(20)), cards(20))

    def test_week_short_head_cannot_expose_tail(self):
        # A weaker pick cannot enter the first ten when the strict head itself is short.
        self.assertEqual(self.s.week_tail(cards(9), 'de-DE', {}, set(), (set(), set()), cards(6, 100)), [])

    def test_week_tail_tone_and_joint(self):
        # Lowering the seed floor must not lower tone or together-score gates.
        self.assertFalse(self.tail_gate(tone=False))
        self.assertFalse(self.tail_gate(joint=0))
        self.assertFalse(self.tail_gate(joint=-1))

    def test_week_tail_every_sparse_person(self):
        # Each sparse person must pass the tail floor independently.
        s = self.s
        with patch.object(s, 'get_model', return_value={'nexp': {'ana': 4, 'ben': 4}}), \
                patch.object(s, 'tone_ok', return_value=True), patch.object(s, 'score_key', return_value=(1,)), \
                patch.object(s, 'seed_knn', side_effect=lambda seeds, *a: .5 if seeds == ['n'] else .4):
            self.assertFalse(s.week_tail_ok(cards(1)[0], {'ana': ['n'], 'ben': ['m']}))

    def test_week_tail_hero_cap_and_order(self):
        # The highest eligible score leads the tail, with at most one hero.
        s, head, pool = self.s, cards(14), cards(12, 100)
        with patch.object(s, 'row_pool', return_value=pool), patch.object(s, 'row_eligible', return_value=True), \
                patch.object(s, 'week_tail_ok', return_value=True), patch.object(s, 'week_scores', return_value={}), \
                patch.object(s, 'served_row', return_value=[]), patch.object(s, 'member_pick', return_value=False), \
                patch.object(s, 'week_score', side_effect=lambda x, scores: (0, -int(x['id'][5:]))), \
                patch.object(s, 'is_hero', side_effect=lambda x: 100 <= int(x['id'][5:]) < 105):
            tail = s.week_tail(head, 'de-DE', {}, set(), (set(), set()))
            self.assertEqual(len(tail), 6)
            self.assertEqual(tail[0]['id'], 'tmdb:100')
            self.assertEqual(sum(100 <= int(x['id'][5:]) < 105 for x in tail), 1)
            self.assertEqual(head, cards(14))
            with patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) in (0, 1) or 100 <= int(x['id'][5:]) < 105):
                tail = s.week_tail(head, 'de-DE', {}, set(), (set(), set()))
                self.assertFalse(any(s.is_hero(x) for x in tail))

    def test_week_tail_masks_and_dedupe(self):
        # Tail supply cannot bypass row eligibility, feedback or strict dedupe.
        s, pool = self.s, cards(6, 100)
        pool.append(pool[-1].copy())
        with patch.object(s, 'row_pool', return_value=pool), \
                patch.object(s, 'row_eligible', side_effect=lambda x, *a: x['id'] != 'tmdb:103'), \
                patch.object(s, 'week_tail_ok', return_value=True), patch.object(s, 'week_scores', return_value={}), \
                patch.object(s, 'served_row', return_value=[]), patch.object(s, 'member_pick', return_value=False), \
                patch.object(s, 'week_score', return_value=(1, 1)), patch.object(s, 'is_hero', return_value=False):
            tail = s.week_tail(cards(14), 'de-DE', {}, {'tmdb:100'}, ({'tmdb:101'}, set()))
            self.assertEqual({x['id'] for x in tail}, {'tmdb:102', 'tmdb:104', 'tmdb:105'})
            self.assertEqual(len(tail), 3)

    def test_week_tail_not_front_after_rotation(self):
        # Even a tail original sharing a franchise cannot enter the first ten on rotation.
        s, head, tail = self.s, cards(14), cards(6, 100)
        with ExitStack() as stack:
            stack.enter_context(patch.object(s, 'row_list', side_effect=lambda cid, *a: head.copy() if cid == rid('week', s.CONFIG) else []))
            for name, value in {'row_eligible': True, 'row_member': False, 'feed': {}, 'tired_keys': {}, 'week_scores': {}}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            for name in ('fill_row', 'balance_row', 'polish_row'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            stack.enter_context(patch.object(s, 'week_score', return_value=(0, 0)))
            stack.enter_context(patch.object(s, 'week_tail', return_value=tail))
            stack.enter_context(patch.object(s, 'day_index', return_value=100))
            stack.enter_context(patch.object(s, 'franchise_key', return_value='same'))
            stack.enter_context(patch.object(s, 'row_facts', side_effect=lambda x: {'date': '1990' if x in tail else '2000'}))
            result = s.served_row(rid('week', s.CONFIG), 'tv', 'de-DE', {}, set(), (set(), {x['id'] for x in head}))
            self.assertEqual(len(result), 20)
            self.assertFalse(set(x['id'] for x in result[:10]) & set(x['id'] for x in tail))
            self.assertEqual(set(x['id'] for x in result[:14]), set(x['id'] for x in head))

    def lane_build(self, items, front=(), anime=False):
        s = self.s
        model = {'aff': {'k:1': 1}, 'names': {'k:1': 'anime' if anime else 'cartoon'}}
        with patch.object(s, 'get_model', return_value=model), patch.dict(s._profiles, {'household': {'seen': set(), 'negative': set()}}), \
                patch.dict(s._feeds, {(rid('foryou', s.CONFIG), 'de-DE'): {'metas': {'tv': list(front)}}}, clear=True), \
                patch.dict(s._learn, {'lanes': {}, 'rows': {}}, clear=True), \
                patch.object(s, 'because_seed', return_value=None), patch.object(s, 'speaks', return_value=True), \
                patch.object(s, 'lane_titles', return_value=[(x['id'], 'movie', x) for x in items]), \
                patch.object(s, 'tired_keys', return_value={}), \
                patch.object(s, 'rank_by_taste', side_effect=lambda m, xs: [(None, [], *x) for x in xs]), \
                patch.object(s, 'with_match', side_effect=lambda x: x), patch.object(s, 'is_hero', return_value=False), \
                patch.object(s, 'lane_member', return_value=True), \
                patch.object(s, 'lane_title', return_value='Synthetic Anime' if anime else 'Synthetic'), redirect_stdout(io.StringIO()):
            return s.lanes_feed('de-DE')

    def test_lane_tone_failing_cartoon_skipped(self):
        # A keyword named cartoon cannot pass viability using kid-toned inventory.
        metas = cards(20)
        for x in metas:
            x['genres'] = ['Family']
        self.assertEqual(self.lane_build(metas)['_names'], {})

    def test_lane_front_overlap_skipped(self):
        # Stored front-of-home overlap must not count as fresh lane supply.
        metas = cards(20)
        self.assertEqual(self.lane_build(metas, metas[:12])['_names'], {})

    def test_lane_healthy_chosen_and_storage_retained(self):
        # A viable shelf is selected while its stored reserve remains unchanged.
        metas = cards(32)
        out = self.lane_build(metas, metas[24:])
        self.assertEqual(out['_names'][rid('lane1', self.s.CONFIG)]['lane'], 'k:1')
        self.assertEqual(out[rid('lane1', self.s.CONFIG)]['pool'], metas)
        self.assertEqual(len(out[rid('lane1', self.s.CONFIG)]['tv']), 24)

    def test_lane_majority_animation_skip_unless_anime(self):
        # The animation majority rule permits only an explicitly anime-labelled lane.
        metas = cards(20)
        for x in metas[:11]:
            x['genres'] = ['Animation']
        self.assertEqual(self.lane_build(metas)['_names'], {})
        self.assertTrue(self.lane_build(metas, anime=True)['_names'])

    def test_lane_quality_not_fresh(self):
        # Low-quality cards cannot prop up the minimum fresh count.
        metas = cards(20)
        for x in metas[:10]:
            x['_row'] = {'rating': 3}
        self.assertEqual(self.lane_build(metas)['_names'], {})

    def test_leap_animation_guard(self):
        # Leap allows Animation only when its own seed supplies that context.
        s, meta = self.s, cards(1)[0]
        meta['genres'] = ['Animation']
        for animation in (False, True):
            with patch.object(s, 'row_seed', return_value='seed'), \
                    patch.object(s, 'frec', side_effect=lambda k, *a: {'g': ['Animation']} if animation and k == 'seed' else {}):
                self.assertEqual(s.row_eligible(meta, rid('leap', s.CONFIG), 'de-DE', {}), animation)

    def test_member_animation_guard(self):
        # Ana's shelf admits Animation only after one of her explicit seeds does.
        s, meta = self.s, cards(1)[0]
        meta['genres'] = ['Animation']
        for animation in (False, True):
            with patch.object(s, 'explicit_seeds', return_value=['seed']), \
                    patch.object(s, 'frec', side_effect=lambda k, *a: {'g': ['Animation']} if animation and k == 'seed' else {}):
                self.assertEqual(s.row_eligible(meta, rid('ana', s.CONFIG), 'de-DE', {}), animation)

    def test_member_refill_bounded_and_not_heroes(self):
        # Animation loss is replenished through R1's bounded non-hero candidate pool.
        s, metas = self.s, cards(60, 100)
        with patch.object(s, 'row_pool', return_value=metas), patch.object(s, 'source_metas', return_value=[]), \
                patch.object(s, 'row_related', return_value=False), patch.object(s, 'row_eligible', return_value=True), \
                patch.object(s, 'member_pick', return_value=False), patch.object(s, 'is_hero', side_effect=lambda x: x['id'] == 'tmdb:159'), \
                patch.object(s, 'score_key', side_effect=lambda m, k, *a: (int(k[5:]),)):
            result = s.fill_row([], rid('ana', s.CONFIG), 'de-DE', {}, set(), (set(), set()))
            self.assertEqual(len(result), s.ROW_MAX)
            self.assertNotIn('tmdb:159', {x['id'] for x in result})

    def test_member_serving_dread_limit(self):
        # The serving cap cannot fill a sparse shelf with deferred dread overflow.
        s, metas = self.s, cards(18)
        with patch.object(s, 'explicit_seeds', return_value=['seed']), patch.object(s, 'seed_similarity', return_value=1), \
                patch.object(s, 'frec', side_effect=lambda k, *a: {'x': {'dread': 3}} if int(k[5:]) < 8 else {}):
            strict = s.member_balance(metas, "ana", strict=True)
            self.assertEqual(len(strict), 15)
            self.assertEqual(sum(int(x['id'][5:]) < 8 for x in strict), 5)
            self.assertCountEqual(s.member_balance(metas, "ana"), metas)

    def test_mood_heroes_move_without_loss(self):
        # Moving surplus heroes must preserve every card and cap the first ten at three.
        s, metas = self.s, cards(20)
        with patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 8):
            result = s.mood_heroes(metas)
            self.assertEqual(sum(s.is_hero(x) for x in result[:10]), 3)
            self.assertCountEqual(result, metas)
            self.assertEqual(result[-5:], metas[3:8])

    def test_mood_move_preserves_selected_twenty(self):
        # Hero movement happens inside the selected shelf rather than losing cards at its size limit.
        s, metas = self.s, cards(30)
        with patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 8):
            result = self.served(rid('mood1', s.CONFIG), metas)
            self.assertCountEqual(result, metas[:20])
            self.assertLessEqual(sum(s.is_hero(x) for x in result[:10]), 3)

    def test_report_measures_row_targets(self):
        # The report must cover every session target without exposing title data.
        from eval.rows_r2_report import rows_diff
        a = [{'id': 'a', 'type': 'tv', 'metas': cards(2)}]
        b = [{'id': 'a', 'type': 'tv', 'metas': cards(1)}, {'id': 'b', 'type': 'tv', 'metas': cards(1, 10)}]
        self.assertEqual(rows_diff(a, b), {'rows_changed': 2, 'metas_changed': 2})
        self.assertEqual(rows_diff(a, a), {'rows_changed': 0, 'metas_changed': 0})


class SnapshotR2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snapshot = require_snapshot()
        if not snapshot.is_dir():
            raise unittest.SkipTest('set SKALD_SNAPSHOT to a home snapshot')
        cls.context = home_server(snapshot)
        with redirect_stdout(io.StringIO()):
            cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)
        cls.rows = display_rows(cls.s)

    def test_snapshot_window_rule(self):
        # Snapshot fronts stay distinct; only chart ranks may repeat.
        s = self.s
        row_metas = s.row_metas
        for scope in [None] + sorted(s.SCOPES):
            with s.addon_scope(scope), ExitStack() as stack:
                if scope is not None:
                    stack.enter_context(patch.object(s, 'row_metas', side_effect=lambda *args: row_metas(*args, who=s.SCOPES[scope])))
                rows = display_rows(s)
            above, first12 = set(), set()
            for row in rows:
                metas = row['metas']
                keys = {x['id'] for x in metas}
                if row['id'] != rid('top10', s.CONFIG):
                    blocked = above if row['id'] in s.MOODS_ROWS + s.LANES else getattr(above, 'deep', above)
                    self.assertFalse(keys & blocked, (scope, row['id']))
                    self.assertFalse({x['id'] for x in metas[:12]} & first12, (scope, row['id']))
                first12.update(x['id'] for x in metas[:12])
                above = s.mark_above(above, row['id'], metas)

    def test_snapshot_row_targets(self):
        # Real served counts enforce the pre-registered mood, lane, Week and shelf targets.
        s = self.s
        rows = {r['id']: r['metas'] for r in self.rows}
        self.assertEqual([len(rows[c]) for c in s.MOODS_ROWS[:2]], [20, 20])
        self.assertTrue(all(len(rows[c]) >= 16 for c in s.MOODS_ROWS[2:]))
        self.assertTrue(all(not rows[c] or len(rows[c]) >= 6 for c in s.LANES))
        self.assertGreaterEqual(sum(bool(rows[c]) for c in s.LANES), 2)
        # W15 leaves 13 eligible head cards; the existing tail is capped at six.
        self.assertEqual(len(rows[rid('week', s.CONFIG)]), 19)
        self.assertGreaterEqual(len(rows[rid('leap', s.CONFIG)]), 10)
        self.assertEqual(len(rows[rid('', s.CONFIG) + self.s.PEOPLE[0]]), 20)
        self.assertGreaterEqual(sum(len(r['metas']) for r in self.rows), 435)

    def test_snapshot_member_session(self):
        # A sparse seeded session must improve her visible picks and keep Animation at the tail.
        s = self.s
        baseline = next(r['metas'] for r in self.rows if r['id'] == rid('foryou', s.CONFIG))[:12]
        home = s.build_home('de-DE', s._profiles['household'], s.remote_masks(), self.s.PEOPLE[0])
        fy = home[rid('foryou', s.CONFIG), 'tv'][:12]
        seeds, model = s.explicit_seeds(self.s.PEOPLE[0]), s.get_model(False)
        self.assertGreaterEqual(len({x['id'] for x in fy} - {x['id'] for x in baseline}), 4)
        self.assertGreaterEqual(sum(s.seed_knn(seeds, x['id'], 1) or 0 for x in fy)/len(fy), .45)
        self.assertGreaterEqual(sum(s.score_key(model, x['id'], kind='movie' if x['type'] == 'movie' else 'tv')[0] or 0 for x in fy)/len(fy), .230)
        hers = home[rid('', s.CONFIG) + self.s.PEOPLE[0], 'tv'][:12]
        self.assertGreaterEqual(sum(s.seed_knn(seeds, x['id'], 1) or 0 for x in hers)/len(hers), .45)
        self.assertFalse(any('Animation' in s.row_facts(x)['g'] for x in fy))
        for cid in s.PERSON_ROWS:
            flags = ['Animation' in s.row_facts(x)['g'] for x in home[cid, 'tv']]
            self.assertEqual(flags, sorted(flags), cid)


if __name__ == '__main__':
    unittest.main()
