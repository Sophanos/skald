"""Synthetic parity checks for the actual home serving adapter."""
import copy
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.household import protocol

NOW = 1791734400.0


def cards(start, n):
    return [dict(id=f'tmdb:{i}', name=f'Synthetic {chr(65 + i % 26)} {i} Card',
                 type='movie' if i % 2 else 'series', poster='p', background='b',
                 _row={'g': ['Drama'], 'rating': 8, 'votes': 1000, 'date': '2020-01-01'})
            for i in range(start, start + n)]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='hh-adapter-'))
        self.stack.enter_context(patch.dict(os.environ, {'DATA_DIR': temp}, clear=True))
        self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('offline')))
        spec = importlib.util.spec_from_file_location('hh_adapter_server', Path('server.py'))
        self.s = s = importlib.util.module_from_spec(spec)
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(s)
        for name in ('_pool', '_outer', '_nzb_pool', '_connector_pool'):
            self.addCleanup(getattr(s, name).shutdown, wait=True, cancel_futures=True)
        self.stack.enter_context(patch.object(s, 'read_taste', return_value=s.DEFAULT_TASTE))
        self.stack.enter_context(patch.object(s, 'refresh_preferences', return_value=None))
        self.stack.enter_context(patch.object(s, 'remote_masks', return_value=(set(), set())))
        self.stack.enter_context(patch.object(s, 'veto_keys', return_value=set()))
        self.stack.enter_context(patch.object(s, '_memory_read', return_value={'seq': 0, 'facts': []}))
        self.stack.enter_context(patch.object(s, 'tmdb', side_effect=AssertionError('offline')))
        s._profiles['household'] = {'entries': [], 'seen': set(), 'negative': set(),
                                    'searches': ['synthetic'], 'genres': {}}
        s.CONFIG['household']['rows'] = ['hbo', 'mood1', 'mood2', 'top10']
        s.CONFIG['household']['row_order'] = ['mood1', 'hbo', 'top10', 'mood2']
        self.lang = 'en-US'

    def put(self, suffix, metas, names=None, store=None):
        s = self.s
        record = {'metas': {'tv': metas}, 'names': names or {}}
        (s._feeds if store is None else store)[s.rid(suffix), self.lang] = record
        for meta in metas:
            s._facets[meta['id']] = {'kind': 'movie' if meta['type'] == 'movie' else 'tv',
                                    'g': ['Drama'], 'x': {'feel_good': 2},
                                    'cr': {'US': 'PG' if meta['type'] == 'movie' else 'TV-PG'}}

    def test_adapter_matches_actual_served_rows(self):
        from eval.household import adapter
        s = self.s
        base = cards(100, 16)
        self.put('hbo', base)
        self.put('mood1', base[:3], {'mood': 'edge', 'title': 'Fresh source'})
        self.put('mood2', cards(200, 5), {'mood': 'edge', 'title': 'Too small'})
        pin = cards(300, 12)
        self.put('mood1', pin, {'mood': 'cozy', 'title': 'Pinned cozy'}, s._shown)
        scope = next(iter(s.SCOPES))
        scoped = cards(400, 12)
        s._shown_scoped[scope] = {}
        self.put('mood1', scoped, {'mood': 'cozy', 'title': 'Scoped cozy'}, s._shown_scoped[scope])
        person = s.SCOPES[scope]
        s.CONFIG['taste']['people'][person] = {'prefs': {'excluded_genres': {'movie': ['Horror']}}}
        scoped[1]['_row']['g'] = ['Horror']
        s._learn['served'][base[0]['id']] = {'name': 'Synthetic tired',
            'rows': {s.rid('hbo'): {'days': [str(i) for i in range(s.LEARN_TIRED)]}}}
        before = copy.deepcopy((s._feeds, s._shown, s._shown_scoped))
        self.assertIsNone(adapter.replay_home(s, now=NOW))
        for selected_scope in (None, scope):
            with s.addon_scope(selected_scope), patch('time.time', return_value=NOW):
                s._home_cache.clear()
                expected = [(cid, 'tv' if 'tv' in types else typ,
                             s.row_metas(cid, 'tv' if 'tv' in types else typ, self.lang, who=person))
                            for cid, types, _ in s.feed_rows(self.lang)
                            for typ in (('tv',) if 'tv' in types else types)]
            with patch.object(s, 'build_home', wraps=s.build_home) as build:
                got = adapter.replay_home(s, now=NOW, lang=self.lang, scope=selected_scope,
                                          who=person, enabled=True)
                self.assertEqual(build.call_count, 1)
            self.assertEqual([(r['id'], r['type'], r['metas']) for r in got['rows']], expected)
            mood = next(r for r in got['rows'] if r['id'] == s.rid('mood1'))
            self.assertEqual(mood['name'], 'Pinned cozy' if selected_scope is None else 'Scoped cozy')
            self.assertEqual(got['rows'][0]['id'], s.rid('mood1'))
            stages = got['candidate_inputs'][s.rid('mood1'), 'tv']
            self.assertEqual(stages['curated'], pin if selected_scope is None else scoped)
            self.assertTrue(stages['pool'])
            self.assertIn('ranked', stages)
            self.assertTrue({x['id'] for x in stages['backfilled']} <= {x['id'] for x in stages['curated']})
            hbo = next(r for r in got['rows'] if r['id'] == s.rid('hbo'))
            self.assertNotIn(base[0]['id'], [m['id'] for m in hbo['metas'][:5]])
            self.assertFalse({m['id'] for m in hbo['metas'][:s.ROW_WINDOW]} &
                             {m['id'] for m in mood['metas']})
            with patch('time.time', return_value=NOW):
                rotated = s.rotate_row(base[1:] + base[:1], s.rid('hbo'))
            self.assertEqual([m['id'] for m in hbo['metas']], [m['id'] for m in rotated])
            self.assertFalse(next(r['metas'] for r in got['rows'] if r['id'] == s.rid('mood2')))
            if selected_scope is not None:
                self.assertNotIn(scoped[1]['id'], [m['id'] for m in mood['metas']])
        self.assertEqual((s._feeds, s._shown, s._shown_scoped), before)
        with s.addon_scope(person), patch('time.time', return_value=NOW):
            s.CONFIG['taste']['people'][person]['prefs']['kids_age'] = 8
            s._facets[scoped[2]['id']]['adult'] = True
            got = adapter.replay_home(s, now=NOW, scope=person, who=person, enabled=True)
            self.assertNotIn(scoped[2]['id'], [m['id'] for r in got['rows'] for m in r['metas']])
        s.CONFIG['taste']['people'][person]['prefs'] = {}
        s.CONFIG['household']['rows'] = ['mood1']
        s._feeds.clear()
        s._shown.clear()
        personal = cards(500, 12)
        self.put('mood1', personal, {'mood': 'cozy'})
        s._model['m'] = {'nexp': {person: 60}}
        with patch.object(s, 'explicit_seeds', return_value=['tmdb:900000']), \
                patch.object(s, 'score_key', side_effect=lambda m, key, *a, **kw:
                             (1000 if key == 'tmdb:506' else -int(key[5:]), [])), \
                patch.object(s, 'person_order', wraps=s.person_order) as reorder:
            got = adapter.replay_home(s, now=NOW, who=person, enabled=True)
            self.assertEqual(reorder.call_count, 1)
            ranked = got['candidate_inputs'][s.rid('mood1'), 'tv']['ranked']
            self.assertNotEqual([m['id'] for m in ranked], [m['id'] for m in personal])
            again = adapter.replay_home(s, now=NOW, who=person, enabled=True)
            self.assertEqual(got, again)

    def test_mixed_catalog_aliases_not_tripled(self):
        from eval.household import adapter
        s = self.s
        s.CONFIG['household']['rows'] = ['hbo', 'top10']
        self.put('hbo', cards(100, 14))
        chart = cards(200, 22)
        s._feeds[s.rid('top10'), self.lang] = {'metas': {
            typ: [m for m in chart if m['type'] == typ] for typ in ('movie', 'series')}}
        order = [(s.rid('hbo'), typ) for typ in ('movie', 'series', 'tv')]
        order += [(s.rid('top10'), typ) for typ in ('movie', 'series')]
        got = adapter.replay_home(s, now=NOW, order=order, enabled=True)
        self.assertEqual(len(got['rows']), 3)
        self.assertEqual(got['rows'][0]['type'], 'tv')
        self.assertEqual([len(r['metas']) for r in got['rows']], [14, 10, 10])
        self.assertEqual(len(got['rankings']), 3)
        self.assertEqual(len(got['row_specs']), 3)

    def test_member_scope_defaults_to_personal_viewer(self):
        from eval.household import adapter
        s = self.s
        s.CONFIG['household']['rows'] = ['mood1']
        self.put('mood1', cards(500, 12), {'mood': 'cozy'})
        person = s.PEOPLE[0]
        s._model['m'] = {'nexp': {person: 60}}
        with patch.object(s, 'explicit_seeds', return_value=['tmdb:900000']), \
                patch.object(s, 'score_key', side_effect=lambda m, key, *a, **kw:
                             (1000 if key == 'tmdb:506' else -int(key[5:]), [])), \
                patch.object(s, 'person_order', wraps=s.person_order) as reorder:
            expected = adapter.replay_home(s, now=NOW, scope=person, who=person, enabled=True)
            reorder.reset_mock()
            actual = adapter.replay_home(s, now=NOW, scope=person, enabled=True)
            self.assertEqual(reorder.call_count, 1)
            self.assertEqual(actual, expected)
            explicit = adapter.replay_home(s, now=NOW, scope=person, who='both', enabled=True)
            self.assertNotEqual(explicit['candidate_inputs'], expected['candidate_inputs'])

    def test_changed_llm_request_is_unsupported(self):
        from eval.household import adapter
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou']
        a = adapter.personal_request(s, ['Synthetic A (match 60%)'], now=NOW)
        b = adapter.personal_request(s, ['Synthetic B (match 90%)'], now=NOW)
        self.assertNotEqual(protocol.protocol_id(a), protocol.protocol_id(b))
        fixed = adapter.curation_evidence(a, b)
        self.assertEqual(fixed['evidence_level'], protocol.FIXED_CURATION)
        self.assertTrue(fixed['request_changed'])
        with self.assertRaisesRegex(ValueError, 'unsupported personal request'):
            adapter.curation_evidence(a, b, complete=True,
                                      supported_request_hashes=[protocol.protocol_id(a)])
        with self.assertRaises(ValueError):
            adapter.curation_evidence(a, a, complete=True)
        supported = adapter.curation_evidence(a, b, complete=True,
                                             supported_request_hashes=[protocol.protocol_id(b)])
        self.assertEqual(supported['evidence_level'], protocol.COMPLETE)
