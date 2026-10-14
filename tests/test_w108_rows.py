"""Honest home composition from synthetic, offline row sources."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server


def cards(n, start=0, **facts):
    return [dict(id=f'tmdb:{start+i}', name=f'Invented {start+i} Card', type='movie',
                 poster='p', background='b', description='An invented overview.',
                 _row={'overview': 'An invented overview.', 'date': '2025-01-01', **facts}) for i in range(n)]


class HonestRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.s = load_server(temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        s = self.s
        cfg = copy.deepcopy(s.CONFIG)
        for key in ('rows', 'row_order', 'row_labels'):
            cfg['household'].pop(key, None)
        self.enterContext(patch.object(s, 'CONFIG', cfg))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        for store in (s._feeds, s._shown, s._shown_scoped, s._home_cache, s._facets, s._profiles):
            self.enterContext(patch.dict(store, {}, clear=True))
        for name, value in {'get_model': None, 'tired_keys': {}, 'memory_why': None,
                            'remote_masks': (set(), set()), 'veto_keys': set(),
                            'who_watching': ('none', None), 'active_profiles': [],
                            'explicit_seeds': [], 'week_ok': True, 'week_tail': []}.items():
            self.enterContext(patch.object(s, name, return_value=value))
        self.enterContext(patch.object(s, 'score_key', side_effect=lambda m, key, *a, **kw: (int(key[5:]), [])))

    def put(self, key, xs, pool=(), **names):
        cid = self.s.rid(key)
        if key == 'week':
            xs = [{**x, '_week': {'together': 'A recent taste match'}, '_week_start': self.s.week_start()} for x in xs]
        self.s._feeds[cid, self.s.LANG] = {'metas': {'tv': xs, 'pool': list(pool)}, 'names': names}
        return cid

    def serve(self, cid, above=None, who=None):
        return self.s.served_row(cid, 'tv', self.s.LANG, {}, above if above is not None else set(),
                                 (set(), set()), who)

    def loose_filters(self):
        for name in ('row_eligible', 'row_related'):
            self.enterContext(patch.object(self.s, name, return_value=True))

    def test_themes_never_borrow_but_refill_own_reserve(self):
        s = self.s
        self.loose_filters()
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 100)))
        for cid in s.MOODS_ROWS + s.LANE_ROWS + s.DOCS + s.STUDIO_ROWS + [s.rid('because'), s.rid('leap')]:
            with self.subTest(cid=cid):
                self.assertEqual(s.fill_row(cards(2), cid, s.LANG, {}, set(), (set(), set())), cards(2))
        cid = self.put('lane1', cards(2), cards(6, 20), lane='k:42')
        self.assertEqual(len(s.fill_row(cards(2), cid, s.LANG, {}, set(), (set(), set()))), 8)

    def test_thin_themes_hide_after_all_filters(self):
        s = self.s
        self.loose_filters()
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 100)))
        for key in ('mood1', 'lane1', 'because', 'leap', 'doctrend', 'hbo'):
            with self.subTest(key=key):
                cid = self.put(key, cards(6), lane='k:42')
                self.assertEqual(self.serve(cid, {'tmdb:0'}), [])
                self.assertEqual(len(self.serve(cid)), 6)

    def test_foryou_own_order_then_taste_and_only_minimum_fill(self):
        s = self.s
        self.loose_filters()
        own = cards(4)
        own = [own[i] for i in (2, 0, 3, 1)]
        own = [{**x, 'description': 'Chosen for its quiet wit. · An invented overview.'} for x in own]
        cid = self.put('foryou', own)
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(30, 100)))
        row = self.serve(cid, who=s.PEOPLE[0])
        self.assertEqual([x['id'] for x in row[:4]], [x['id'] for x in own])
        self.assertEqual([x['id'] for x in row[4:]], [f'tmdb:{i}' for i in range(129, 121, -1)])
        self.assertTrue(all(x['description'] == 'An invented overview.' for x in row[4:]))
        self.put('foryou', cards(16))
        self.assertEqual([x['id'] for x in self.serve(cid)], [x['id'] for x in cards(16)])

    def test_foryou_fill_excludes_week_and_filters_before_minimum(self):
        s = self.s
        self.loose_filters()
        cid = self.put('foryou', cards(4))
        self.put('week', cards(5, 100))
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 100)))
        row = self.serve(cid)
        self.assertEqual(len(row), 12)
        self.assertFalse({x['id'] for x in row} & {x['id'] for x in cards(5, 100)})

    def test_hero_cap_includes_unaffiliated_and_different_universes(self):
        s = self.s
        self.loose_filters()
        xs = cards(10)
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] in {'tmdb:0', 'tmdb:1', 'tmdb:2', 'tmdb:3'}))
        self.enterContext(patch.object(s, 'universe_key', side_effect=lambda x: {'tmdb:0': 'world-a', 'tmdb:1': 'world-b'}.get(x['id'])))
        cid = self.put('foryou', xs)
        self.assertEqual(sum(s.is_hero(x) for x in self.serve(cid)), 2)

    def test_lane_theme_uses_own_source_not_label_or_borrowed_cards(self):
        s = self.s
        self.loose_filters()
        xs = cards(8)
        self.enterContext(patch.object(s, 'is_hero', return_value=True))
        self.enterContext(patch.object(s, 'universe_key', return_value='world-a'))
        cid = self.put('lane1', xs, lane='k:42', title='Quiet evenings')
        self.assertEqual(len(self.serve(cid)), 8)
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] in {y['id'] for y in xs[:4]}))
        self.enterContext(patch.object(s, 'universe_key', return_value=None))
        self.put('lane1', xs, lane='k:42', title='Superheroes')
        self.assertEqual(sum(s.is_hero(x) for x in self.serve(cid)), 2)

    def test_franchise_appears_in_two_rows_counted_once_per_row(self):
        s = self.s
        self.loose_filters()
        above = set()
        for i, key in enumerate(('foryou', 'week', 'mood1')):
            xs = cards(8, i * 100)
            for x in xs[:2]:
                x['_row']['collection'] = 42
            cid = self.put(key, xs)
            row = self.serve(cid, above)
            self.assertEqual(sum(s.franchise_key(x) == 'collection:42' for x in row), 2 if i < 2 else 0)
            above = s.mark_above(above, cid, row)

    def test_member_catalogs_and_direct_serving_respect_scope(self):
        s = self.s
        self.loose_filters()
        cid, owner = next(iter(s.ROW_OWNER.items()))
        s.CONFIG['household']['rows'] = ['people']
        s._feeds[cid, s.LANG] = {'metas': {'tv': cards(8)}}
        for scope in (None, 'together', *s.PEOPLE):
            allowed = scope in (None, 'together', owner)
            with self.subTest(scope=scope), s.addon_scope(scope), patch.object(s, 'pin_shown'):
                self.assertEqual(cid in {x[0] for x in s.feed_rows()}, allowed)
                self.assertEqual(bool(self.serve(cid, who=s.SCOPES.get(scope))), allowed)
                self.assertEqual(cid in {x['id'] for x in s.manifest(s.LANG, scope)['catalogs']}, allowed)

    def test_default_studios_off_explicit_configuration_unchanged(self):
        s = self.s
        self.assertTrue(all(not s.row_enabled(cid) for cid in s.STUDIO_ROWS))
        for key, value in (('rows', ['studios']), ('row_order', ['studios']), ('row_labels', {'studios': 'Made here'})):
            with self.subTest(key=key), patch.dict(s.CONFIG['household'], {key: value}):
                self.assertTrue(all(s.row_enabled(cid) for cid in s.STUDIO_ROWS))
                self.assertTrue(set(s.STUDIO_ROWS) <= {r[0] for r in s.feed_rows()})

    def test_default_doc_rotation_and_three_nonempty_moods_and_lanes(self):
        s = self.s
        self.loose_filters()
        self.enterContext(patch.object(s, 'row_pool', return_value=[]))
        for i, cid in enumerate(s.MOODS_ROWS + s.LANES + s.DOCS):
            s._feeds[cid, s.LANG] = {'metas': {'tv': cards(8 if i != 0 else 0, i*100)}, 'names': {'lane': 'k:42'}}
        for day in range(3):
            with patch.object(s, 'day_index', return_value=day):
                s._home_cache.clear()
                home = s.build_home(s.LANG, {}, (set(), set()), 'none')
                listed = {r[0] for r in s.feed_rows()}
                self.assertEqual({cid for cid in s.DOCS if home.get((cid, 'tv'))}, {s.DOCS[day]})
                self.assertEqual(set(s.DOCS) & listed, {s.DOCS[day]})
                self.assertEqual(set(s.MOODS_ROWS) & listed, set(s.MOODS_ROWS[1:4]))
                self.assertEqual(set(s.LANES) & listed, set(s.LANES[:3]))
        s.CONFIG['household']['rows'] = ['docs', 'moods', 'lanes']
        self.assertEqual(set(s.DOCS + s.MOODS_ROWS + s.LANES) & {r[0] for r in s.feed_rows()},
                         set(s.DOCS + s.MOODS_ROWS + s.LANES))

    def test_doc_rotation_skips_rows_below_filtered_floor(self):
        s = self.s
        for i, cid in enumerate(s.DOCS):
            s._feeds[cid, s.LANG] = {'metas': {'tv': cards(8, i*100, g=['Documentary'])}}
        self.enterContext(patch.object(s, 'row_eligible', side_effect=lambda x, cid, *a: cid in s.DOCS[2:4]))
        for day in (0, 1):
            with patch.object(s, 'day_index', return_value=day):
                home = s.build_home(s.LANG, {}, (set(), set()), 'none')
                self.assertEqual({cid for cid in s.DOCS if home.get((cid, 'tv'))}, {s.DOCS[2+day]})

    def test_unanchored_keywords_and_companies_have_no_reason(self):
        s = self.s
        m = {'names': {'k:42': 'Neighbor', 'c:43': 'Invented Studio', 'n:44': 'Invented Network'}}
        with patch.object(s, 'anchor', return_value=None):
            self.assertEqual(s.reasons(m, 'tmdb:1', list(m['names'])), [])
        with patch.object(s, 'anchor', return_value=('tmdb:999', 'Invented Favorite', [])):
            self.assertEqual(len(s.reasons(m, 'tmdb:1', ['k:42'])), 1)

    def test_because_allows_animation_from_animated_seed(self):
        s = self.s
        seed = 'tmdb:9000'
        s._facets[seed] = {'g': ['Animation'], 'kind': 'tv', 'title': 'Invented Seed', 'y': '2020'}
        cid = self.put('because', cards(8, g=['Animation'], lang='ja'), seed=seed)
        with patch.object(s, 'seed_similarity', return_value=.8):
            self.assertEqual(len(self.serve(cid)), 8)
            s._facets[seed]['g'] = ['Drama']
            self.assertEqual(self.serve(cid), [])

    def test_member_because_and_leap_receive_serving_variety_cap(self):
        s = self.s
        self.loose_filters()
        xs = cards(10)
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] in {y['id'] for y in xs[:4]}))
        for key in (next(iter(s.ROW_OWNER.values())), 'because', 'leap'):
            with self.subTest(key=key):
                cid = self.put(key, xs)
                self.assertEqual(sum(s.is_hero(x) for x in self.serve(cid)), 2)

    def test_foryou_reaches_minimum_after_variety_filters(self):
        s = self.s
        self.loose_filters()
        xs = cards(24, 100)
        cid = self.put('foryou', cards(4))
        self.enterContext(patch.object(s, 'row_pool', return_value=xs))
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) >= 116))
        self.assertEqual(len(self.serve(cid)), 12)

    def test_foryou_own_heroes_take_priority_over_borrowed_heroes(self):
        s = self.s
        self.loose_filters()
        own = cards(6)
        cid = self.put('foryou', own)
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(20, 100)))
        self.enterContext(patch.object(s, 'person_order', side_effect=lambda xs, *a: xs[::-1]))
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] in {'tmdb:0', 'tmdb:1'} or int(x['id'][5:]) >= 100))
        row = self.serve(cid, who=s.PEOPLE[0])
        self.assertEqual([x['id'] for x in row[:6]], [x['id'] for x in own])

    def test_foryou_polish_preserves_own_pick_before_older_filler(self):
        s = self.s
        self.loose_filters()
        own = cards(6)
        own[0]['description'] = 'Chosen for its quiet wit. · An invented overview.'
        own[0]['_row']['date'] = '2026-01-01'
        cid = self.put('foryou', own)
        for shared in ('franchise', 'universe'):
            with self.subTest(shared=shared):
                pool = cards(4, 100) + cards(20, 200)
                own[0]['_row']['collection'] = 42 if shared == 'franchise' else None
                for x in pool[:4]:
                    x['_row']['collection'] = 42 if shared == 'franchise' else None
                related = {own[0]['id']} | {x['id'] for x in pool[:4]}
                with patch.object(s, 'row_pool', return_value=pool), \
                        patch.object(s, 'person_order', side_effect=lambda xs, *a: xs[::-1]), \
                        patch.object(s, 'universe_key', side_effect=lambda x: 'world-a' if x['id'] in related else None):
                    row = self.serve(cid, who=s.PEOPLE[0])
                self.assertEqual(row[:6], own)
                self.assertEqual(len(row), 12)
                self.assertLessEqual(sum(x['id'] in related for x in row), 2 if shared == 'franchise' else 4)

    def test_anime_variety_caps_discovery_but_keeps_watched_series(self):
        s = self.s
        self.loose_filters()
        xs = [{**x, 'type': 'series'} for x in cards(10)]
        for x in (xs[0], xs[7]):
            x['_row']['collection'] = 42
        p = {'seen': {x['id'] for x in xs[:4]}}
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] in {y['id'] for y in xs[:7]}))
        for key in ('airing', 'nextseason'):
            cid = s.rid(key)
            s._feeds[cid, s.LANG] = {'metas': {'series': xs}}
            above = s.RowAbove(set())
            above.franchises['collection:42'] = 2
            with self.subTest(key=key):
                row = s.served_row(cid, 'series', s.LANG, p, above, (set(), set()), s.PEOPLE[0])
                self.assertTrue(p['seen'] <= {x['id'] for x in row})
                self.assertNotIn(xs[7]['id'], {x['id'] for x in row})
                self.assertEqual(sum(s.is_hero(x) for x in row if x['id'] not in p['seen']), 2)
                with patch.object(s, 'preference_ok', return_value=True):
                    guest = s.served_row(cid, 'series', s.LANG, p, above, (set(), set()), s.GUEST)
                    self.assertEqual(len(guest), 10)
                    with patch.object(s, 'kids_age', return_value=10):
                        kid = s.served_row(cid, 'series', s.LANG, p, above, (set(), set()), s.PEOPLE[0])
                        self.assertEqual(len(kid), 10)

    def test_optional_public_rows_and_newtoyou_obey_variety_cap(self):
        s = self.s
        self.loose_filters()
        xs = cards(10)
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 4))
        for key in ('digital', 'toprated', 'newtoyou'):
            with self.subTest(key=key), patch.object(s, 'row_list', return_value=xs):
                row = s.served_row(s.rid(key), 'movie' if key != 'newtoyou' else 'tv', s.LANG, {}, set(), (set(), set()))
                self.assertEqual(sum(s.is_hero(x) for x in row), 2)

    def test_variety_filters_own_candidates_before_row_limit(self):
        s = self.s
        self.loose_filters()
        cid = self.put('mood1', cards(30))
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) < 20))
        row = self.serve(cid)
        self.assertEqual(len(row), 12)
        self.assertEqual(sum(s.is_hero(x) for x in row), 2)

    def test_anchor_is_never_reduced_to_a_bare_keyword(self):
        s = self.s
        xs = cards(4)
        xs[3]['description'] = 'Neighbor like Invented Favorite · An invented overview.'
        self.assertIn('like Invented Favorite', s.human_cards(xs)[3]['description'])

    def test_report_counts_scopes_without_writes_or_network(self):
        from eval.home_rows_report import scope_report
        s = self.s
        self.loose_filters()
        s.CONFIG['household']['rows'] = ['foryou', 'week', 'people']
        own = cards(6, collection=42)
        cid = self.put('foryou', own)
        self.put('week', cards(6, 200))
        self.put(next(iter(s.ROW_OWNER.values())), cards(6, 300))
        self.enterContext(patch.object(s, 'polish_row', side_effect=lambda xs, *a, **kw: xs))
        self.enterContext(patch.object(s, 'is_hero', side_effect=lambda x: x['id'] == own[0]['id']))
        self.enterContext(patch.object(s, 'row_pool', return_value=cards(10, 100)))
        s._shown_scoped.update({scope: {} for scope in s.SCOPES})
        before = copy.deepcopy((s._shown, s._shown_scoped, s._learn, s._feeds))
        with patch.object(s, 'pin_shown', side_effect=AssertionError('pin write')), \
                patch.object(s, 'save_learn', side_effect=AssertionError('learn write')), \
                patch.object(s, 'note_exposure', side_effect=AssertionError('learn write')), \
                patch.object(s, 'save_feeds', side_effect=AssertionError('feed write')):
            report = scope_report(s)
        self.assertEqual(set(report), {'household', *s.SCOPES})
        home = report['household']
        self.assertEqual(home['rows'], 3)
        self.assertEqual(home['cards'], 28)
        self.assertEqual(home['borrowed_cards'], 10)
        self.assertEqual(home['superhero_cards'], 1)
        self.assertEqual(home['superhero_rows'], 1)
        self.assertEqual(home['most_repeated_franchise_rows'], 1)
        self.assertEqual(home['unique_titles'], 28)
        self.assertEqual((s._shown, s._shown_scoped, s._learn, s._feeds), before)

    def test_report_household_uses_session_viewer_for_preferences(self):
        from eval.home_rows_report import scope_report
        s = self.s
        self.loose_filters()
        s.CONFIG['household']['rows'] = ['moods']
        s.CONFIG['taste']['people'][s.PEOPLE[0]]['prefs'] = {'excluded_genres': {'movie': ['Horror']}}
        self.put('mood1', cards(8) + cards(1, 100, g=['Horror']))
        for who, count in (('both', 8), (s.PEOPLE[1], 9)):
            with self.subTest(who=who), patch.object(s, 'who_watching', return_value=(who, 'prior')):
                report = scope_report(s)
                self.assertEqual(report['household']['cards'], count)
                self.assertEqual(report[s.PEOPLE[0]]['cards'], 8)
                self.assertEqual(report[s.PEOPLE[1]]['cards'], 9)

    def test_offline_report_copies_scope_pins_and_preserves_input_files(self):
        from eval.home_rows_report import scope_report
        from skald.engine import Engine
        from skald.members import fresh_config, rid, VERSION
        from skald.migrate import FORMAT
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            cfg = fresh_config(['ana', 'ben'])
            cfg['household']['rows'] = ['moods']
            cid = rid('mood1', cfg)
            key = cid + '|en-US'
            (data/'skald.json').write_text(json.dumps(cfg))
            (data/'.skald-format').write_text(json.dumps({'format': FORMAT, 'release': VERSION}))
            (data/'feeds.json').write_text(json.dumps({key: {'metas': {'tv': cards(8)}, 'names': {'mood': 'edge'}}}))
            (data/'shown_scopes.json').write_text(json.dumps({'ana': {key: {'metas': {'tv': cards(8, 100)}, 'names': {'mood': 'cozy'}}}}))
            before = {p.name: p.read_bytes() for p in data.iterdir()}
            with Engine(data, offline=True) as engine:
                s = engine._server
                with s.addon_scope('ana'):
                    self.assertEqual(s.feed(cid, 'en-US')['metas']['tv'], cards(8, 100))
                with patch.object(s, 'pin_shown', side_effect=AssertionError('pin write')):
                    report = scope_report(s, 'en-US')
                self.assertEqual(report['ana']['cards'], 8)
            self.assertEqual({p.name: p.read_bytes() for p in data.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
