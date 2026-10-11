"""Person-scoped addon contracts; synthetic inputs and optional read-only snapshots."""
from skald.members import rid
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from eval.profile_golden import NOW, ROOT, offline_server
from eval.rows_report import home_server
from test_api import raw_request
import test_lanes_r3 as r3

LANG = 'de-DE'
SESSIONS = ('none', 'ben', 'ana', 'both', 'guest')


def request(s, tail, scope=None):
    path = f'/{LANG}/' + (f'p/{scope}/' if scope is not None else '') + tail
    return raw_request(s, path, handler=s.Handler)[:3]


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def catalogs(s):
    return [(cid, typ) for cid, typs, _ in s.feed_rows(LANG) for typ in typs]


def attribution(s):
    return digest([s.session_windows(), s.who_watching(), s.session_view(),
                   [s.attribute(e.get('item') or {}) for e in s._profiles['household']['entries']]])


class ScopedAddon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = offline_server(ROOT / 'server.py')
        cls.s = cls.context.__enter__()
        cls.addClassCleanup(cls.context.__exit__, None, None, None)

    def setUp(self):
        r3.LanesR3.setUp(self)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(self.s, 'DATA', temp))
        self.stack.enter_context(patch.dict(self.s._shown_scoped, {}, clear=True))
        self.stack.enter_context(patch.dict(self.s._pin_cache, {}, clear=True))
        self.stack.enter_context(patch.object(self.s, 'DEDUPE_ORDER', self.s.LANES))
        self.put(self.s.LANES[0], r3.cards(16), 'k:1')

    put = r3.LanesR3.put
    facet = r3.LanesR3.facet

    def test_manifest_only_identity_fields_differ_and_pin_cache_is_shared(self):
        s = self.s
        code, headers, raw = request(s, 'manifest.json')
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'public, max-age=3600')
        legacy = json.loads(raw)
        shown = Path(s.DATA, 'shown.json').read_bytes()
        with patch.object(s, 'served_row', side_effect=AssertionError('pin cache missed')):
            for scope in s.SCOPES:
                code, _, raw = request(s, 'manifest.json', scope)
                expected = {**legacy, 'id': legacy['id'] + '.' + scope,
                            'name': legacy['name'] + ' · ' + scope.title(),
                            'description': legacy['description'] + ' · ' + scope.title()}
                self.assertEqual((code, raw), (200, wire(expected)))
        self.assertEqual(Path(s.DATA, 'shown.json').read_bytes(), shown)

    def test_scope_never_guesses_and_learning_uses_scope_pins(self):
        s, cid = self.s, self.s.LANES[0]
        request(s, 'manifest.json', 'ben')
        self.put(cid, r3.cards(16, 100), 'k:2')
        request(s, 'manifest.json', 'ana')
        with patch.object(s, 'who_watching', side_effect=AssertionError('scoped guess')), \
                patch.object(s, 'note_served', wraps=s.note_served) as served:
            code, headers, raw = request(s, f'catalog/tv/{cid}.json', 'ben')
            self.assertEqual(code, 200)
            self.assertEqual(headers['Cache-Control'], 'public, max-age=60')
            self.assertTrue(json.loads(raw)['metas'])
            self.assertEqual(served.call_args.args[0], 'lane:k:1')
            with patch.object(s, '_HOME_CACHE_ENABLED', False):
                self.assertEqual(request(s, f'catalog/tv/{cid}.json', 'ben')[0], 200)
        self.assertIsNone(getattr(s._scoping, 'scope', None))
        with patch.object(s, 'row_metas', return_value=[]):
            self.assertEqual(request(s, f"catalog/movie/{rid('top10', s.CONFIG)}.json", 'ben')[1]['Cache-Control'], 'public, max-age=900')

    def test_search_bytes_and_errors_equal_legacy(self):
        s = self.s
        with patch.object(s, 'search', return_value={'metas': r3.cards(1)}), patch.object(s, 'log_search'), \
                patch.object(s, 'who_watching', side_effect=AssertionError('search guessed')):
            for typ in ('movie', 'series'):
                for extra in ('search=Castle', 'search='):
                    tail = f"catalog/{typ}/{rid('search', s.CONFIG)}/{extra}.json"
                    expected = request(s, tail)
                    for scope in s.SCOPES:
                        self.assertEqual(request(s, tail, scope), expected)
            with patch.object(s, 'search', side_effect=ValueError('offline')):
                tail = f"catalog/movie/{rid('search', s.CONFIG)}/search=x.json"
                self.assertEqual(request(s, tail, 'ben'), request(s, tail))
        self.assertIsNone(s._scoping.scope)

    def test_ten_malformed_scopes_and_extra_routes_are_404(self):
        s = self.s
        for scope in ('..', '%2e', 'Ben', 'BEN', 'guest', 'both', '', 'unknown', 'ben/extra', '%62en'):
            with self.subTest(scope=scope):
                self.assertEqual(request(s, 'manifest.json', scope)[0], 404)
                self.assertEqual(request(s, f"catalog/tv/{rid('lane1', s.CONFIG)}.json", scope)[0], 404)
                self.assertEqual(request(s, f"catalog/movie/{rid('search', s.CONFIG)}/search=x.json", scope)[0], 404)
        for tail in ('health', 'stats', 'why/tmdb:1.json', 'manifest.json/extra', 'manifest.json/',
                     f"catalog/tv/{rid('lane1', s.CONFIG)}.json/extra", f"catalog/tv/{rid('search', s.CONFIG)}/search=x.json"):
            self.assertEqual(request(s, tail, 'ben')[0], 404)

    def test_three_people_derive_and_route_four_scopes(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp, 'server.py')
            source.write_text((ROOT / 'server.py').read_text())
            data = Path(temp, 'data')
            shutil.copytree(ROOT / 'tests/fixtures/events/data', data)
            cfg = copy.deepcopy(self.s.LEGACY)
            cfg['household']['members'].append('third')
            cfg['household']['labels']['third'] = 'Third'
            (data / 'skald.json').write_text(json.dumps(cfg))
            with offline_server(source, data) as s, patch.object(s, 'pin_decision', return_value={}), \
                    patch.object(s, 'row_metas', return_value=r3.cards(1)) as rows:
                self.assertEqual(s.SCOPES, {'ana': 'ana', 'ben': 'ben', 'third': 'third', 'together': 'both'})
                for scope, who in s.SCOPES.items():
                    self.assertEqual(request(s, 'manifest.json', scope)[0], 200)
                    self.assertEqual(request(s, f"catalog/tv/{rid('foryou', s.CONFIG)}.json", scope)[0], 200)
                    self.assertEqual(rows.call_args.kwargs['who'], who)

    def test_pin_lookup_priority_initial_copy_and_cache_generation(self):
        s, cid = self.s, self.s.LANES[0]
        old = copy.deepcopy(s._feeds[cid, LANG])
        s._shown[cid, LANG] = old
        with s.addon_scope('ben'):
            self.assertIs(s.feed(cid, LANG), old)
            self.assertIsNot(s._shown_scoped['ben'], s._shown)
            s._pinning.feeds = {(cid, LANG): {'names': {'title': 'Decision'}}}
            try:
                self.assertEqual(s.feed(cid, LANG)['names']['title'], 'Decision')
            finally:
                del s._pinning.feeds
            s.row_metas(cid, 'tv', LANG, who='ben')
            with patch.object(s, 'build_home', wraps=s.build_home) as build:
                s._shown[cid, LANG] = copy.deepcopy(old)
                s.row_metas(cid, 'tv', LANG, who='ben')
                build.assert_not_called()
                s._shown_scoped['ben'][cid, LANG] = copy.deepcopy(old)
                s.row_metas(cid, 'tv', LANG, who='ben')
                self.assertEqual(build.call_count, 1)
        s.row_metas(cid, 'tv', LANG, who='both')
        with s.addon_scope('together'):
            s.row_metas(cid, 'tv', LANG, who='both')
        self.assertEqual({k[2] for k in s._home_cache}, {None, 'ben', 'together'})
        for scope in s.SCOPES:
            with s.addon_scope(scope):
                for who in SESSIONS:
                    s.row_metas(cid, 'tv', LANG, who=who)
        self.assertEqual(len(s._home_cache), 8)

    def test_name_content_four_by_four_after_generation_change(self):
        s = self.s
        for x in (None, *s.SCOPES):
            for y in (None, *s.SCOPES):
                first = json.loads(request(s, 'manifest.json', x)[2])
                if x == y:
                    s._feeds[s.LANES[0], LANG] = s._feeds[s.LANES[0], LANG].copy()
                else:
                    self.put(s.LANES[0], r3.cards(16, 100), 'k:2')
                s.clear_seed_cache()
                request(s, 'manifest.json', y)
                names = {r['id']: r['name'] for r in first['catalogs']}
                with s.addon_scope(x):
                    for cid in s.LANES:
                        row = s.row_metas(cid, 'tv', LANG, who=s.SCOPES[x] if x else 'both')
                        self.assertTrue(not row or names[cid] == row.names['title'], (x, y, cid))
                self.put(s.LANES[0], r3.cards(16), 'k:1')

    def test_legacy_bytes_and_session_files_survive_scoped_fetches(self):
        s = self.s
        request(s, 'manifest.json')
        for who in SESSIONS:
            s.write_session({'who': 'off' if who == 'none' else who}, 'phone')
            pairs = [(cid, typ) for cid, typ in catalogs(s) if cid in s.LANES]
            before = [request(s, f'catalog/{typ}/{cid}.json')[2] for cid, typ in pairs]
            shown = Path(s.DATA, 'shown.json').read_bytes()
            sessions = Path(s.DATA, 'sessions.jsonl').read_bytes()
            state = attribution(s)
            for scope in s.SCOPES:
                request(s, 'manifest.json', scope)
                request(s, f"catalog/tv/{rid('lane1', s.CONFIG)}.json", scope)
            self.assertEqual(before, [request(s, f'catalog/{typ}/{cid}.json')[2] for cid, typ in pairs])
            self.assertEqual(Path(s.DATA, 'shown.json').read_bytes(), shown)
            self.assertEqual(Path(s.DATA, 'sessions.jsonl').read_bytes(), sessions)
            self.assertEqual(attribution(s), state)

    def test_persistence_private_atomic_and_missing_or_corrupt_fallback(self):
        s = self.s
        request(s, 'manifest.json')
        request(s, 'manifest.json', 'ben')
        self.put(s.LANES[0], r3.cards(16, 100), 'k:2')
        request(s, 'manifest.json', 'ana')
        path = Path(s.DATA, 'shown_scopes.json')
        expected = copy.deepcopy(s._shown_scoped)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(path.with_suffix('.json.tmp').exists())
        s._shown_scoped.clear()
        s.load_state()
        for scope, records in expected.items():
            self.assertEqual(s._shown_scoped[scope], records)
        # Existing permissive modes must also be tightened on replacement.
        path.with_suffix('.json.tmp').write_text('old')
        path.with_suffix('.json.tmp').chmod(0o666)
        request(s, 'manifest.json', 'ben')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for bad in (None, '{', 'null', '[]', '{"ben":null}', '{"ben":{"bad":{}}}',
                    f'''{{"ben":{{"{rid('lane1', s.CONFIG)}|de-DE":{{"names":[]}}}}}}'''):
            if bad is None:
                path.unlink()
            else:
                path.write_text(bad)
            s.load_state()
            for scope in s.SCOPES:
                self.assertEqual(s._shown_scoped[scope], s._shown)
                self.assertIsNot(s._shown_scoped[scope], s._shown)
        with patch.object(s.os, 'replace', side_effect=OSError('offline write failure')):
            self.assertEqual(request(s, 'manifest.json', 'ben')[0], 200)

    def test_manifest_catalog_interleave_without_deadlock(self):
        s = self.s
        for scope in s.SCOPES:
            with s.addon_scope(scope):
                pass
        for manifest_scope in (None, 'ben'):
            held, waiting = threading.Event(), threading.Event()
            lock, errors = threading.Lock(), []
            class HomeLock:
                def __enter__(self):
                    if threading.current_thread().name == 'manifest':
                        waiting.set()
                    lock.acquire()
                def __exit__(self, *args):
                    lock.release()
            def model(*args):
                if threading.current_thread().name == 'catalog':
                    held.set()
                    if not waiting.wait(2):
                        raise AssertionError('manifest did not interleave')
                return {'test': True}
            def run(fn):
                try:
                    fn()
                except BaseException as e:
                    errors.append(e)
            with patch.object(s, '_home_lock', HomeLock()), patch.object(s, 'get_model', side_effect=model):
                threads = [threading.Thread(name='catalog', target=run, args=(lambda: request(s, f"catalog/tv/{rid('lane1', s.CONFIG)}.json", 'ana'),), daemon=True),
                           threading.Thread(name='manifest', target=run, args=(lambda: request(s, 'manifest.json', manifest_scope),), daemon=True)]
                threads[0].start()
                self.assertTrue(held.wait(2))
                threads[1].start()
                for t in threads:
                    t.join(3)
                self.assertFalse(any(t.is_alive() for t in threads), 'manifest/catalog deadlock')
            self.assertEqual(errors, [])
        barrier = threading.Barrier(8)
        errors = []
        def interleave(scope, manifest):
            try:
                barrier.wait(2)
                for _ in range(12):
                    self.assertEqual(request(s, 'manifest.json' if manifest else f"catalog/tv/{rid('lane1', s.CONFIG)}.json", scope)[0], 200)
            except BaseException as e:
                errors.append(e)
        threads = [threading.Thread(target=interleave, args=(scope, manifest), daemon=True)
                   for scope in (None, *s.SCOPES) for manifest in (False, True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(any(t.is_alive() for t in threads), 'interleaving deadlock')
        self.assertEqual(errors, [])


@unittest.skipUnless(os.environ.get('SKALD_SNAPSHOT'), 'set SKALD_SNAPSHOT for snapshot gates')
class ScopedSnapshot(unittest.TestCase):
    def setUp(self):
        self.clock = patch('time.time', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.context = home_server(os.environ['SKALD_SNAPSHOT'])
        self.s = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def test_g1_g3_all_sessions_catalogs_and_card_fields(self):
        s = self.s
        request(s, 'manifest.json')
        pairs = catalogs(s)
        self.assertEqual(len(pairs), 77)
        for scope in s.SCOPES:
            request(s, 'manifest.json', scope)
        expected = {scope: [wire({'metas': s.row_metas(cid, typ, LANG, who=who)}) for cid, typ in pairs]
                    for scope, who in s.SCOPES.items()}
        for who in ('none', 'both', *reversed(s.PEOPLE)):
            s.write_session({'who': 'off' if who == 'none' else who}, 'phone')
            before = [request(s, f'catalog/{typ}/{cid}.json')[2] for cid, typ in pairs]
            shown = Path(s.DATA, 'shown.json').read_bytes()
            for scope in s.SCOPES:
                request(s, 'manifest.json', scope)
                actual = [request(s, f'catalog/{typ}/{cid}.json', scope)[2] for cid, typ in pairs]
                self.assertEqual(actual, expected[scope], (who, scope))
                for legacy, scoped in zip(expected[scope], actual):
                    self.assertEqual([set(x) for x in json.loads(legacy)['metas']], [set(x) for x in json.loads(scoped)['metas']])
            self.assertEqual(before, [request(s, f'catalog/{typ}/{cid}.json')[2] for cid, typ in pairs])
            self.assertEqual(Path(s.DATA, 'shown.json').read_bytes(), shown)

    def test_g2_four_by_four_generation(self):
        s = self.s
        for x in (None, *s.SCOPES):
            for y in (None, *s.SCOPES):
                first = json.loads(request(s, 'manifest.json', x)[2])
                for cid in s.LANES:
                    record = s._feeds.get((cid, LANG))
                    if record:
                        s._feeds[cid, LANG] = record.copy() if x == y else {
                            **record, 'names': {**record.get('names', {}), 'title': 'Changed generation'}}
                s.clear_seed_cache()
                request(s, 'manifest.json', y)
                names = {r['id']: r['name'] for r in first['catalogs']}
                with s.addon_scope(x):
                    for cid in s.LANES:
                        row = s.row_metas(cid, 'tv', LANG, who=s.SCOPES[x] if x else 'both')
                        self.assertTrue(not row or names[cid] == row.names['title'], (x, y, cid))

    def test_g4_thousand_fetches_preserve_attribution(self):
        s = self.s
        s.write_session({'who': s.PEOPLE[1]}, 'phone')
        before, sessions = attribution(s), Path(s.DATA, 'sessions.jsonl').read_bytes()
        pairs, scopes = catalogs(s), list(s.SCOPES)
        for i in range(1000):
            cid, typ = pairs[i % len(pairs)]
            self.assertEqual(request(s, f'catalog/{typ}/{cid}.json', scopes[i % len(scopes)])[0], 200)
        self.assertEqual(attribution(s), before)
        self.assertEqual(Path(s.DATA, 'sessions.jsonl').read_bytes(), sessions)

    def test_g6_memory_notes_only_use_scope_person(self):
        from eval.memory_report import synthetic
        s = self.s
        with redirect_stdout(io.StringIO()):
            chosen = synthetic(s, s._profiles['household'], s.get_model(False))
        store = s.memory_load()
        store['facts'] = [f for f in store['facts'] if f['kind'] == 'like' and f['about'] == chosen[f['person']]['likes'][0]]
        s.memory_save(store, [])
        s.clear_seed_cache()
        s._model['m'] = s.build_model(s._profiles['household'])
        facts = store['facts']
        self.assertEqual(len(facts), len(s.PEOPLE))
        for person in s.PEOPLE:
            returned, memory_why = [], s.memory_why
            def why(*args, **kwargs):
                result = memory_why(*args, **kwargs)
                if result:
                    returned.append(result['fact'])
                return result
            with patch.object(s, 'memory_why', side_effect=why) as calls:
                for cid, typ in catalogs(s):
                    self.assertEqual(request(s, f'catalog/{typ}/{cid}.json', person)[0], 200)
                self.assertTrue(calls.call_count)
                self.assertTrue(all(call.args[2] == person for call in calls.call_args_list))
                self.assertTrue(returned)
                self.assertEqual(set(returned), {f['id'] for f in facts if f['person'] == person})


if __name__ == '__main__':
    unittest.main()
