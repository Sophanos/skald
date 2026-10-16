"""Kind-safe cache slots and legacy snapshot repair, offline and stdlib only."""
from skald.members import rid
from decide_kit import write_legacy
from array import array
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import base64
import tempfile
import unittest
from unittest.mock import patch

from skald.engine import Engine, doctor_keys
from skald.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=False)
ANA, BEN = snapshot_people(SNAPSHOT)


class KeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('keys_server', ROOT / 'server.py')
        cls.s = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TMDB_API': 'offline', 'OPENROUTER_API_KEY': ''}):
            spec.loader.exec_module(cls.s)
        for name in ('_pool', '_outer', '_nzb_pool'):
            cls.addClassCleanup(getattr(cls.s, name).shutdown)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        for name in ('_facets', '_emb', '_emb_kinds', '_profiles', '_feeds', '_shown'):
            getattr(self.s, name).clear()
        self.s._tagging.clear()
        self.s._row_kinds.update(profile=None, keys={})
        for name, value in (('DATA', self.temp.name), ('OR_KEY', 'offline')):
            p = patch.object(self.s, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, data):
        (self.data / (name + '.json')).write_text(json.dumps(data))

    def record(self, kind):
        return {'kind': kind, 'title': kind + ' title', 'g': ['Crime' if kind == 'tv' else 'Comedy'],
                'y': '2020', 'o': kind + ' overview', 'x': {'cozy_case' if kind == 'tv' else 'feel_good': 3}}

    def test_engine_snapshot_ignores_live_skald_config(self):
        self.write('skald', self.s.fresh_config(['ana', 'ben']))
        with tempfile.TemporaryDirectory() as live:
            config = Path(live) / 'household.json'
            config.write_text(json.dumps(self.s.fresh_config(['cy'])))
            with patch.dict(os.environ, {'SKALD_CONFIG': str(config), 'SKALD_LIVE': '1'}), \
                    redirect_stdout(io.StringIO()), Engine(self.data) as engine:
                self.assertEqual(engine._server.PEOPLE, ('ana', 'ben'))

    def vector(self, slot, kind, axis):
        self.s._emb[slot] = array('f', [float(i == axis) for i in range(self.s.EMB_DIM)])
        self.s._emb_kinds[slot] = kind

    def test_both_orders_coexist_with_tags_and_vectors(self):
        s = self.s
        for first, second in (('movie', 'tv'), ('tv', 'movie')):
            with self.subTest(first=first):
                s._facets.clear()
                s._emb.clear()
                s._emb_kinds.clear()
                with patch.object(s, 'facet_record', side_effect=lambda kind, tid: self.record(kind)):
                    self.assertEqual(s.ensure_facets([('tmdb:1415', first), ('tmdb:1415', second)]), 2)
                    self.assertEqual(s.ensure_facets([('tmdb:1415', first), ('tmdb:1415', second)]), 0)
                self.vector('tmdb:1415', first, 0)
                self.vector('tmdb:1415@' + second, second, 1)
                for kind, axis in ((first, 0), (second, 1)):
                    self.assertEqual(s.frec('tmdb:1415', kind), self.record(kind))
                    self.assertEqual(s.femb('tmdb:1415', kind)[axis], 1)
                self.assertEqual(set(s._facets), {'tmdb:1415', 'tmdb:1415@' + second})

    def test_unknown_facet_kind_and_unknown_embedding_remain_compatible(self):
        self.s._facets['tmdb:1'] = {'title': 'old record'}
        self.s._emb['tmdb:1'] = array('f', [1.])
        for kind in ('movie', 'tv'):
            self.assertEqual(self.s.fkey('tmdb:1', kind), 'tmdb:1')
            self.assertEqual(self.s.frec('tmdb:1', kind)['title'], 'old record')
            self.assertEqual(list(self.s.femb('tmdb:1', kind)), [1.])

    def test_missing_alias_does_not_borrow_other_kind(self):
        self.s._facets['tmdb:1'] = self.record('movie')
        self.vector('tmdb:1', 'movie', 0)
        self.assertEqual(self.s.frec('tmdb:1', 'tv'), {})
        self.assertIsNone(self.s.femb('tmdb:1', 'tv'))

    def test_tagging_collision_creates_alias_without_touching_owner(self):
        s = self.s
        for first, second in (('movie', 'tv'), ('tv', 'movie')):
            with self.subTest(first=first):
                s._facets.clear()
                owner = self.record(first)
                s._facets['tmdb:1415'] = copy.deepcopy(owner)
                def record(kind, tid):
                    out = self.record(kind)
                    out.pop('x')
                    out.pop('o')
                    return out
                with patch.object(s, 'facet_record', side_effect=record), \
                        patch.object(s, 'tmdb', return_value={'overview': second + ' overview', 'first_air_date': '2020'}), \
                        patch.object(s, 'llm_text', return_value='1|cozy_case:3'):
                    self.assertEqual(s.tag_experiences([('tmdb:1415', second)]), 1)
                self.assertEqual(s._facets['tmdb:1415'], owner)
                alias = s.frec('tmdb:1415', second)
                self.assertEqual(alias['kind'], second)
                self.assertEqual(alias['o'], second + ' overview')
                self.assertEqual(alias['x'], {'cozy_case': 3})

    def test_ranking_tags_missing_alias_after_fifty_tagged_titles_and_retries(self):
        s = self.s
        s._facets.update({f'tmdb:{i}': self.record('movie') for i in range(1, 52)})
        for i in range(1, 51):
            s._facets[f'tmdb:{i}']['x_state'] = {'status': 'manual'}
        s._facets['tmdb:51@tv'] = {**self.record('tv'), 'o': 'Fixture series'}
        del s._facets['tmdb:51@tv']['x']
        items = [(f'tmdb:{i}', 'movie', {}) for i in range(1, 51)] + [('tmdb:51', 'tv', {})]
        with patch.object(s, 'ensure_facets'), patch.object(s, 'ensure_emb'), \
                patch.object(s.time, 'time', return_value=1800000000.) as clock, \
                patch.object(s, 'llm_text', side_effect=[s.BudgetExceeded('fixture'), '', '1|dread:3']) as llm:
            for attempt in range(3):
                s.rank_by_taste(None, items)
                self.assertFalse(s._tagging)
                self.assertEqual(bool(s._facets['tmdb:51@tv'].get('x')), attempt == 2)
                clock.return_value += s.TAG_COOLDOWN
        self.assertEqual(llm.call_count, 3)
        self.assertEqual(s._facets['tmdb:51']['x'], self.record('movie')['x'])

    def test_embedding_kind_mismatch_embeds_alias_without_facets(self):
        s = self.s
        self.vector('tmdb:1', 'movie', 0)
        response = {'data': [{'index': 0, 'embedding': [0., 2.] + [0.] * (s.EMB_DIM - 2)}]}
        with patch.object(s, 'title_text', return_value='tv text') as text, \
                patch.object(s, 'openrouter', return_value=response) as embed:
            self.assertEqual(s.ensure_emb([('tmdb:1', 'tv')]), 1)
            self.assertEqual(s.ensure_emb([('tmdb:1', 'tv')]), 1)
            text.assert_called_once_with('tv', '1')
            embed.assert_called_once()
        self.assertEqual(s._emb['tmdb:1'][0], 1)
        self.assertEqual(s.femb('tmdb:1', 'tv')[1], 1)
        self.assertEqual(s._emb_kinds['tmdb:1@tv'], 'tv')

    def test_embedding_first_keeps_bare_slot_owner(self):
        s = self.s
        self.vector('tmdb:1', 'movie', 0)
        with patch.object(s, 'facet_record', side_effect=lambda kind, tid: self.record(kind)):
            s.ensure_facets([('tmdb:1', 'tv'), ('tmdb:1', 'movie')])
        self.assertEqual(s.frec('tmdb:1', 'movie')['title'], 'movie title')
        self.assertEqual(s.frec('tmdb:1', 'tv')['title'], 'tv title')
        self.assertEqual(s.femb('tmdb:1', 'movie')[0], 1)
        self.assertIsNone(s.femb('tmdb:1', 'tv'))

    def test_both_missing_embedding_kinds_in_one_batch(self):
        s = self.s
        response = {'data': [{'index': i, 'embedding': [float(i == j) for j in range(s.EMB_DIM)]} for i in range(2)]}
        with patch.object(s, 'title_text', side_effect=lambda kind, tid: kind), \
                patch.object(s, 'openrouter', return_value=response):
            self.assertEqual(s.ensure_emb([('tmdb:1', 'movie'), ('tmdb:1', 'tv')]), 2)
        self.assertEqual(s.femb('tmdb:1', 'movie')[0], 1)
        self.assertEqual(s.femb('tmdb:1', 'tv')[1], 1)

    def test_new_files_round_trip_and_old_reader(self):
        s = self.s
        s._facets.update({'tmdb:1': self.record('movie'), 'tmdb:1@tv': self.record('tv')})
        self.vector('tmdb:1', 'movie', 0)
        self.vector('tmdb:1@tv', 'tv', 1)
        s._facets_dirty.set()
        s._emb_dirty.set()
        s.save_facets()
        s.save_emb()
        # Same bare lookups and float16 decoder as the old reader; extra fields are ignored.
        old_facets = json.loads((self.data / 'facets.json').read_text())['items']
        old_emb = json.loads((self.data / 'emb.json').read_text())
        self.assertEqual(old_facets['tmdb:1']['title'], 'movie title')
        self.assertEqual(struct.unpack(f'{s.EMB_DIM}e', base64.b64decode(old_emb['v']['tmdb:1']))[0], 1)
        s._facets.clear()
        s._emb.clear()
        s._emb_kinds.clear()
        s.load_emb()
        s.load_facets()
        for kind, axis in (('movie', 0), ('tv', 1)):
            self.assertEqual(s.frec('tmdb:1', kind)['title'], kind + ' title')
            self.assertEqual(s.femb('tmdb:1', kind)[axis], 1)
        self.assertEqual(s._emb_kinds, {'tmdb:1': 'movie', 'tmdb:1@tv': 'tv'})

    def test_skald_reads_profile_kind_without_exposing_alias_ids(self):
        s = self.s
        s._facets.update({'tmdb:1': self.record('movie'), 'tmdb:1@tv': self.record('tv')})
        s._facets_dirty.set()
        s.save_facets()
        entry = {'key': 'tmdb:1', 'id': 1, 'kind': 'tv', 'stype': 'series', 'title': 'tv title',
                 'year': '2020', 'signal': 'loved', 'weight': 2, 'age': 0, 'who': 'owner',
                 'by': ['ana'], 'flags': [], 'genres': [], 'item': {}}
        self.write('profile', {'entries': [entry], 'seen': ['tmdb:1'], 'negative': []})
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            self.assertNotIn('tmdb:1@tv', engine._index)
            self.assertEqual(engine._offline_tmdb('tv/1')['genres'], [{'name': 'Crime'}])
            engine._model['aff']['x:cozy_case'] = 1
            engine._model['names']['x:cozy_case'] = 'cozy case'
            self.assertGreater(engine.score('tmdb:1')['score'], 0)
            self.assertEqual(engine.explain('tmdb:1')['title'], 'tv title')

    def test_old_embedding_file_infers_kind_and_leaves_unknown_either(self):
        s = self.s
        for slot, kind in (('tmdb:1', 'tv'), ('tmdb:2', 'movie')):
            self.vector(slot, kind, 0)
        s._emb_dirty.set()
        s.save_emb()
        raw = json.loads((self.data / 'emb.json').read_text())
        raw.pop('kinds')
        self.write('emb', raw)
        self.write('facets', {'items': {'tmdb:1': self.record('tv')}})
        s._emb.clear()
        s._emb_kinds.clear()
        s.load_emb()
        s.load_facets()  # Production load order, facets infer old vector ownership.
        self.assertEqual(s._emb_kinds, {'tmdb:1': 'tv'})
        self.assertIsNone(s.femb('tmdb:1', 'movie'))
        self.assertIsNotNone(s.femb('tmdb:2', 'movie'))
        self.assertIsNotNone(s.femb('tmdb:2', 'tv'))

    def test_each_evidence_source_repairs_mixed_record_and_embedding(self):
        s = self.s
        for source in ('feeds', 'shown', 'history', 'trakt', 'profile'):
            with self.subTest(source=source):
                for path in self.data.glob('*.json'):
                    path.unlink()
                self.write('facets', {'items': {'tmdb:1': self.record('movie')}})
                self.write(source, {'items': [{'id': 'tmdb:1', 'type': 'series'}]})
                self.vector('tmdb:1', 'movie', 0)
                with redirect_stdout(io.StringIO()) as log:
                    s.load_facets()
                self.assertEqual(s._facets, {})
                self.assertNotIn('tmdb:1', s._emb)
                self.assertNotIn('tmdb:1', s._emb_kinds)
                self.assertIn('dropped 1 mixed', log.getvalue())

    def test_clean_collision_and_completed_alias_are_not_mixtures(self):
        facets = {'tmdb:1': self.record('movie')}
        self.write('trakt', {'items': [{'id': 'tmdb:1', 'type': 'movie'}, {'id': 'tmdb:1', 'type': 'series'}]})
        self.assertEqual(self.s.mixed_keys(self.data, facets), [])
        self.write('trakt', {'items': [{'id': 'tmdb:1', 'type': 'series'}]})
        self.assertEqual(len(self.s.mixed_keys(self.data, facets)), 1)
        facets['tmdb:1@tv'] = self.record('tv')
        self.assertEqual(self.s.mixed_keys(self.data, facets), [])

    def test_tv_play_and_negative_do_not_hide_same_id_movie_row(self):
        s = self.s
        movie = {'id': 'tmdb:1415', 'type': 'movie', 'name': 'Movie title', 'poster': 'art', 'background': 'art'}
        tv = {**movie, 'type': 'series'}
        for weight in (2, -2):
            with self.subTest(weight=weight):
                p = {'seen': {'tmdb:1415'}, 'negative': {'tmdb:1415'} if weight < 0 else set(),
                     'entries': [{'key': 'tmdb:1415', 'kind': 'tv', 'weight': weight}]}
                s._profiles['household'] = p
                s._feeds[(rid('top10', s.CONFIG), 'en-US')] = {'metas': {'movie': [movie], 'series': [tv]}}
                with patch.object(s, 'DEDUPE_ORDER', [rid('top10', s.CONFIG)]), patch.object(s, 'remote_masks', return_value=(set(), {})):
                    self.assertEqual([m['id'] for m in s.row_metas(rid('top10', s.CONFIG), 'movie', 'en-US')], ['tmdb:1415'])
                    self.assertEqual(s.row_metas(rid('top10', s.CONFIG), 'series', 'en-US'), [])
                self.assertIs(s.profile_kinds(p), s.profile_kinds(p))

    def test_meta_read_and_score_use_requested_kind(self):
        s = self.s
        s._facets.update({'tmdb:1': self.record('movie'), 'tmdb:1@tv': self.record('tv')})
        model = {'vec': None, 'aff': {'x:cozy_case': 1}, 'bscale': 1, 'vbase': 0, 'names': {'x:cozy_case': 'cozy case'}}
        self.assertEqual(s.row_facts({'id': 'tmdb:1', 'type': 'series'})['g'], {'Crime'})
        self.assertGreater(s.score_key(model, 'tmdb:1', kind='tv')[0], s.score_key(model, 'tmdb:1', kind='movie')[0])


class SnapshotKeysTests(unittest.TestCase):
    def setUp(self):
        if SNAPSHOT is None or not SNAPSHOT.is_dir():
            self.skipTest('set SKALD_SNAPSHOT to the 2026-10-11 snapshot')

    def test_doctor_exactly_three_and_cli(self):
        report = doctor_keys(SNAPSHOT)
        self.assertEqual([r['key'] for r in report['records']], ['tmdb:1415', 'tmdb:1830', 'tmdb:4546'])
        self.assertEqual(report['mixed'], 3)
        out = io.StringIO()
        self.assertEqual(main(['--snapshot', str(SNAPSHOT), 'doctor', 'keys', '--json'], out=out), 0)
        self.assertEqual(json.loads(out.getvalue()), report)

    def test_snapshot_replay_accepts_repaired_embedding_count(self):
        from eval.replay import offline_server
        root = ROOT / 'eval' / 'out'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as work, redirect_stdout(io.StringIO()):
            with offline_server(SNAPSHOT, work) as s:
                for key in ('tmdb:1415', 'tmdb:1830', 'tmdb:4546'):
                    self.assertNotIn(key, s._facets)
                    self.assertNotIn(key, s._emb)
                self.assertEqual(s._facets['tmdb:1891']['title'], 'The Empire Strikes Back')

    def test_snapshot_served_mismatches_drop_to_zero(self):
        facets = json.loads((SNAPSHOT / 'facets.json').read_text())['items']
        feeds = json.loads((SNAPSHOT / 'feeds.json').read_text())
        metas = {(m['id'], m['type']): m for feed in feeds.values() for xs in feed.get('metas', {}).values()
                 if isinstance(xs, list) for m in xs if isinstance(m, dict)}
        mismatches = lambda store: sum((store.get(key) or {}).get('kind') not in (None, 'movie' if typ == 'movie' else 'tv')
                                      for key, typ in metas)
        self.assertEqual((mismatches(facets), len(metas)), (3, 621))
        for rec in doctor_keys(SNAPSHOT)['records']:
            facets.pop(rec['key'])
        self.assertEqual(mismatches(facets), 0)
