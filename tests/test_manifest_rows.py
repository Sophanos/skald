"""Manifest visibility from synthetic, stored catalog rows."""
from contextlib import ExitStack
import tempfile
import unittest
from unittest.mock import patch

from eval.profile_golden import ROOT, offline_server


def cards(n, start=0, typ='movie'):
    return [dict(id=f'tmdb:{start+i}', name=f'Synthetic Card {start+i}', type=typ) for i in range(n)]


class ManifestRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        s = self.s
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(s, 'DATA', self.stack.enter_context(tempfile.TemporaryDirectory())))
        self.stack.enter_context(patch.object(s, '_feeds_empty', set()))
        for store in (s._feeds, s._shown, s._shown_scoped, s._pin_cache, s._home_cache, s._profiles):
            self.stack.enter_context(patch.dict(store, {}, clear=True))
        for name, value in {'row_eligible': True, 'row_related': True, 'row_member': False,
                            'get_model': None, 'tired_keys': {}, 'remote_masks': (set(), set()),
                            'veto_keys': set(), 'who_watching': ('none', None), 'active_profiles': []}.items():
            self.stack.enter_context(patch.object(s, name, return_value=value))
        for name in ('fill_row', 'balance_row', 'polish_row', 'rotate_row', 'remote_order'):
            self.stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))

    def put(self, cid, xs, typ='tv', **names):
        self.s._feeds[cid, self.s.LANG] = {'metas': {typ: xs}, 'names': names}

    def rows(self, scope=None):
        return self.s.manifest(self.s.LANG, scope)['catalogs']

    def pass_job(self, cids, build):
        s = self.s
        jobs = [('synthetic', cids, 'today', 0, build)]
        with patch.object(s, '_langs', {s.LANG}), \
                patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'job_specs', return_value=jobs), \
                patch.object(s, 'pin_decision'), patch.object(s, 'save_feeds'):
            s.feed_pass(s.local_now(), {})

    def test_no_signal_then_build_restores_manifest_row(self):
        s = self.s
        cids = [s.rid('because'), s.rid('leap')]
        self.assertTrue(set(cids) <= {r['id'] for r in self.rows()})
        self.pass_job(cids, lambda lang: None)
        self.assertEqual(s._feeds, {})
        self.assertTrue(set(cids).isdisjoint(r['id'] for r in self.rows()))
        self.pass_job(cids, lambda lang: {cid: {'tv': cards(s.ROW_MIN, i * 100)}
                                         for i, cid in enumerate(cids)})
        self.assertEqual(s._feeds_empty, set())
        self.assertTrue(set(cids) <= {r['id'] for r in self.rows()})

    def test_no_signal_keeps_old_stored_row(self):
        s, cid = self.s, self.s.rid('because')
        self.put(cid, cards(s.ROW_MIN))
        old = s._feeds[cid, s.LANG]
        self.pass_job([cid], lambda lang: None)
        self.assertIs(s._feeds[cid, s.LANG], old)
        self.assertIn(cid, {r['id'] for r in self.rows()})

    def test_failed_build_keeps_unchecked_row_listed(self):
        s, cid = self.s, self.s.rid('because')
        def upstream_failure(lang):
            s.upstream_failed()
            return None
        def exception(lang):
            raise RuntimeError('synthetic failure')
        for build in (upstream_failure, exception):
            with self.subTest(build=build.__name__):
                self.pass_job([cid], build)
                self.assertEqual(s._feeds, {})
                self.assertIn(cid, {r['id'] for r in self.rows()})

    def test_unknown_then_empty_then_six(self):
        s = self.s
        expected = {cid for cid, _, _ in s.feed_rows(s.LANG)} | {s.rid('search')}
        self.assertEqual({r['id'] for r in self.rows()}, expected)
        for cid, typs, _ in s.feed_rows(s.LANG):
            self.put(cid, [], 'tv' if cid in s.MIXED else typs[0])
        self.assertEqual({r['id'] for r in self.rows()}, {s.rid('top10'), s.rid('search')})
        cid = s.rid('rewatch')
        for n in (5, 6, 0, 6):
            self.put(cid, cards(n))
            self.assertEqual(cid in {r['id'] for r in self.rows()}, n >= s.ROW_MIN)

    def test_scope_counts_served_cards_not_raw_cards(self):
        s, cid = self.s, self.s.rid('hbo')
        self.put(cid, cards(6))
        member = s.PEOPLE[0]
        with patch.object(s, 'veto_keys', side_effect=lambda who: {'tmdb:0'} if who == member else set()):
            self.assertIn(cid, {r['id'] for r in self.rows()})
            self.assertNotIn(cid, {r['id'] for r in self.rows(member)})
            self.assertIn(cid, {r['id'] for r in self.rows('together')})
            with s.addon_scope(member):
                self.assertEqual(len(s.row_metas(cid, 'tv', s.LANG, who=member)), 5)

    def test_pinned_names_and_visibility_use_same_row(self):
        s, cid = self.s, self.s.MOODS_ROWS[0]
        self.put(cid, cards(6), title='Built Mood')
        s.pin_shown(s.LANG)
        self.put(cid, [])
        with patch.object(s, 'pin_shown'):
            row = next(r for r in self.rows() if r['id'] == cid)
        self.assertEqual(row['name'], 'Built Mood')
        self.assertEqual(len(s.row_metas(cid, 'tv', s.LANG)), 6)

    def test_duplicate_names_mixed_types_and_exceptions(self):
        s = self.s
        rows = self.rows()
        home = [r for r in rows if r['id'] != s.rid('search')]
        self.assertEqual(len({r['name'] for r in home}), len(home))
        self.assertEqual([r['name'] for r in rows if r['id'] in s.MOODS_ROWS],
                         ["Tonight's Mood", "Tonight's Mood · 2", "Tonight's Mood · 3", "Tonight's Mood · 4"])
        self.assertEqual([r['name'] for r in rows if r['id'] in s.LANES],
                         ['Picked for You'] + [f'Picked for You · {i}' for i in range(2, 6)])
        self.put(s.MOODS_ROWS[0], [])
        self.put(s.MOODS_ROWS[1], cards(6), title='Built Mood')
        rows = self.rows()
        self.assertEqual([r['name'] for r in rows if r['id'] in s.MOODS_ROWS],
                         ['Built Mood', "Tonight's Mood", "Tonight's Mood · 2"])
        self.assertTrue(all(r['type'] == 'tv' for r in rows if r['id'] in s.MIXED))
        self.put(s.rid('episodes'), cards(1, 100, 'series'), 'series')
        self.put(s.rid('top10'), [], 'movie')
        rows = self.rows()
        self.assertIn(s.rid('episodes'), {r['id'] for r in rows})
        self.assertEqual([r['type'] for r in rows if r['id'] == s.rid('top10')], ['movie', 'series'])
        self.assertEqual([r['type'] for r in rows if r['id'] == s.rid('search')], ['movie', 'series'])

    def test_suffixes_reserve_original_names(self):
        s = self.s
        catalogs = [(cid, ('tv',), name) for cid, name in
                    zip(s.MOODS_ROWS, ('Space', 'Space', 'Space · 2', 'Space'))]
        with patch.object(s, 'feed_rows', return_value=catalogs):
            names = [r['name'] for r in self.rows() if r['id'] != s.rid('search')]
        self.assertEqual(names, ['Space', 'Space · 3', 'Space · 2', 'Space · 4'])

    def test_search_names_are_unchanged(self):
        s = self.s
        search = [r for r in self.rows() if r['id'] == s.rid('search')]
        self.assertEqual([(r['type'], r['name']) for r in search],
                         [('movie', 'Search'), ('series', 'Search')])
