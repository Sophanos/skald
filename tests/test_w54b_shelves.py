"""Standing shelves, using only temporary synthetic household data."""
import datetime as dt
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from eval.profile_golden import ROOT, offline_server
from skald.__main__ import main

class Shelves(unittest.TestCase):
    def setUp(self):
        self.ctx = offline_server(ROOT / 'server.py')
        self.s = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.s._feeds.clear()
        self.cards = [dict(id=f'tmdb:{900+i}', type='movie', name=f'Synthetic Film {i}') for i in range(9)]
    def add(self, scope='ana', **kw):
        return self.s.shelf_change(scope, 'add', text='a classic film', name='Classics', apply=True, **kw)
    def fill(self, now=None):
        s = self.s
        with patch.object(s, 'OR_KEY', 'stub'), patch.object(s, 'llm_picks', return_value=[]) as ai, \
                patch.object(s, 'ai_search', side_effect=lambda typ, *a: self.cards if typ == 'movie' else []), \
                patch.object(s, 'member_scores', side_effect=lambda xs, p: {x['id']: i for i, x in enumerate(xs)}):
            out = s.shelf_feed(s.LANG, now=now)
        return out, ai.call_count
    def test_only_active_slots_declared(self):
        s = self.s
        with patch.object(s, 'pin_shown'), patch.object(s, 'feed', return_value={'metas': {}}), patch.object(s, 'row_metas', return_value=[]):
            for scope in ('ana', 'together', None):
                self.assertFalse([r for r in s.manifest(s.LANG, scope)['catalogs'] if 'shelf_' in r['id']])
            self.add()
            rows = [r for r in s.manifest(s.LANG, 'ana')['catalogs'] if 'shelf_' in r['id']]
            self.assertEqual([(r['id'], r['name']) for r in rows], [(s.rid('shelf_ana_1'), 'Classics')])
            s.shelf_change('ana', 'stop', slot=1, apply=True)
            self.assertFalse([r for r in s.manifest(s.LANG, 'ana')['catalogs'] if 'shelf_' in r['id']])
    def test_unnamed_shelf_uses_its_wish_and_stays_listed_outside_window(self):
        from skald import shelves
        s = self.s
        s.shelf_change('ana', 'add', text='genre:drama', when='sun 05-12', apply=True)
        with s.addon_scope('ana'):
            self.assertEqual(s.shelf_rows(), [(s.rid('shelf_ana_1'), ('tv',), 'genre:drama')])
            self.assertEqual(shelves.slots(s, 'ana'), [(s.rid('shelf_ana_1'), 'ana', 1)])
        self.assertEqual(self.fill(dt.datetime(2026, 10, 12, 6))[0]['_names'][s.rid('shelf_ana_1')]['title'], 'genre:drama')

    def test_count_validation_0_to_10(self):
        s = self.s
        for scope in ('ana', 'together'):
            for n in (0, 10):
                s.shelf_change(scope, 'count', count=n, apply=True)
            for n in (-1, 11, True):
                with self.assertRaises(ValueError):
                    s.shelf_change(scope, 'count', count=n, apply=True)
                cfg = json.loads(json.dumps(s.CONFIG))
                cfg['household']['shelves'] = n
                with self.assertRaises(ValueError):
                    s.validate_config(cfg)
    def test_one_off_wish_fills_and_names_slot(self):
        self.add()
        out, calls = self.fill()
        cid = self.s.rid('shelf_ana_1')
        self.assertEqual(len(out[cid]['tv']), 9)
        self.assertEqual(out['_names'][cid]['title'], 'Classics')
        self.assertEqual(calls, 1)
        with self.s.addon_scope('ana'):
            self.assertEqual(len(self.s.row_metas(cid, 'tv', self.s.LANG)), 9)
        with self.s.addon_scope('together'):
            self.assertEqual(self.s.row_metas(cid, 'tv', self.s.LANG), [])
        self.assertTrue((Path(self.s.DATA) / 'shelves.json').exists())
        with patch.object(self.s, 'search_profile', return_value={'seen': {'tmdb:900'}, 'negative': {'tmdb:901'}}):
            self.assertEqual(len(self.fill()[0][cid]['tv']), 7)
    def test_schedule_window_fills_only_inside(self):
        self.add(when='sun 05-12')
        cid = self.s.rid('shelf_ana_1')
        for day, hour, n in [(11, 4, 0), (11, 5, 9), (11, 12, 0), (12, 6, 0)]:
            out, _ = self.fill(dt.datetime(2026, 10, day, hour))
            self.assertEqual(len(out[cid]['tv']), n)
    def test_fill_applies_preferences_and_child_age(self):
        s = self.s
        self.add()
        s.CONFIG['taste']['people'].setdefault('ana', {})['prefs'] = {'kids_age': 8}
        facts = {'date': '2000-01-01', 'g': [], 'cr': {'US': 'G'}}
        with patch.object(s, 'frec', side_effect=lambda key, *a: {**facts, 'cr': {'US': 'R' if key == self.cards[0]['id'] else 'G'}}):
            out, _ = self.fill()
            self.assertEqual(len(out[s.rid('shelf_ana_1')]['tv']), 8)
            s.CONFIG['taste']['people']['ana']['prefs']['years'] = [2010, None]
            out, _ = self.fill()
            self.assertEqual(out[s.rid('shelf_ana_1')]['tv'], [])
    def test_no_openrouter_discover_fallback(self):
        s = self.s
        s.shelf_change('ana', 'add', text='genre:drama year:1990-2000 country:US', apply=True)
        with patch.object(s, 'tmdb', return_value={'results': [{'id': 900+i} for i in range(9)]}) as tmdb, \
                patch.object(s, 'to_meta', side_effect=lambda it, typ, *a: self.cards[it['id']-900]), patch.object(s, 'member_scores', return_value={}):
            out = s.shelf_feed(s.LANG)
        self.assertTrue(out[s.rid('shelf_ana_1')]['tv'])
        self.assertTrue(all(c.args[0].startswith('discover/') for c in tmdb.call_args_list))
        self.add(slot=2)
        s.shelf_feed(s.LANG)
        self.assertEqual(s.shelf_list('ana')['wishes'][1]['status'], 'needs OpenRouter')
    def test_cli_add_preview_plan_yes_stop(self):
        with patch.dict('os.environ', {'DATA_DIR': self.s.DATA}):
            def run(*args):
                out = io.StringIO()
                self.assertEqual(main(['shelf', *args, '--json'], out=out), 0, out.getvalue())
                return json.loads(out.getvalue())
            preview = run('add', '--person', 'ana', '--name', 'Classics', 'a classic film')
            self.assertIn('plan', preview)
            self.assertFalse(self.s.shelf_list('ana')['wishes'])
            run('add', '--person', 'ana', '--plan', preview['plan'], '--name', 'Classics', 'a classic film')
            run('stop', '--person', 'ana', 'Classics')
            run('add', '--person', 'ana', '--yes', 'a classic film')
            self.assertEqual(len(run('list', '--person', 'ana')['wishes']), 1)
    def test_api_bound_token_own_scope(self):
        s = self.s
        self.add()
        self.add('together')
        self.add(s.PEOPLE[1])
        self.assertEqual({w['scope'] for w in s.api_shelves({'viewer': 'ana'})['wishes']}, {'ana', 'together'})
        from test_api import raw_request
        token = s.new_client('shelf-test', ['read', 'events'], 'ana')
        code, _, raw, _ = raw_request(s, '/api/v1/shelves', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(code, 200)
        self.assertEqual({w['scope'] for w in json.loads(raw)['wishes']}, {'ana', 'together'})
        plan = s.shelf_change('ana', 'replace', slot=1, text='another classic')['plan']
        code, _, _, _ = raw_request(s, '/api/v1/shelves', 'POST', {'plan': plan}, {'Authorization': 'Bearer ' + token})
        self.assertEqual(code, 200)
        for viewer in ('guest', s.PEOPLE[1]):
            preview = s.shelf_change('ana', 'replace', slot=1, text='other film')
            with self.assertRaises(PermissionError):
                s.api_shelves({'viewer': viewer, 'body': {'plan': preview['plan']}})
        with patch.object(s, 'kids_age', return_value=8):
            with self.assertRaises(PermissionError):
                s.child_access('ana', 'shelves')
    def test_one_ai_call_per_window(self):
        self.add(when='daily 05-12')
        self.assertEqual(self.fill(dt.datetime(2026, 10, 11, 6))[1], 1)
        self.s._feeds.clear()
        self.s.load_state()
        self.assertEqual(self.fill(dt.datetime(2026, 10, 11, 7))[1], 0)
        self.assertEqual(self.fill(dt.datetime(2026, 10, 12, 6))[1], 1)
    def test_undo_journal(self):
        self.add()
        for _ in range(25):
            self.s.shelf_change('ana', 'replace', slot=1, text='another film', apply=True)
        events = [json.loads(x) for x in (Path(self.s.DATA) / 'undo.jsonl').read_text().splitlines()]
        self.assertEqual(len(events), 20)
        self.assertEqual(events[-1]['before']['wishes'][0]['text'], 'another film')
    def test_distinct_wishes_stay_in_their_slot_scope(self):
        s = self.s
        scopes = (*s.PEOPLE[:2], 'together')
        for scope in scopes:
            s.shelf_change(scope, 'add', text=scope, name=scope, apply=True)
        with patch.object(s, 'OR_KEY', 'stub'), patch.object(s, 'llm_picks', side_effect=lambda sys, text, *a: text), \
                patch.object(s, 'ai_search', side_effect=lambda typ, kind, picks, *a: [{**x, 'name': picks} for x in self.cards] if typ == 'movie' else []):
            s.shelf_feed(s.LANG)
        for link in (*scopes, None):
            visible = (link, 'together') if link in s.PEOPLE else ('together',)
            with s.addon_scope(link):
                self.assertEqual({r[0] for r in s.shelf_rows()}, {s.rid(f'shelf_{p}_1') for p in visible})
                for owner in scopes:
                    items = s.row_metas(s.rid(f'shelf_{owner}_1'), 'tv', s.LANG)
                    self.assertEqual({x['name'] for x in items}, {owner} if owner in visible else set())

    def test_catalog_cache_ends_at_window_boundary(self):
        from test_scoped_addon import request
        s = self.s
        self.add(when='sun 05-12')
        for hour, minute, second, ttl in [(11, 58, 0, 120), (11, 59, 50, 60), (4, 58, 0, 120), (6, 0, 0, 900)]:
            with patch.object(s, 'local_now', return_value=dt.datetime(2026, 10, 11, hour, minute, second)):
                code, headers, _ = request(s, 'catalog/tv/' + s.rid('shelf_ana_1') + '.json', 'ana')
            self.assertEqual(code, 200)
            self.assertEqual(headers['Cache-Control'], f'public, max-age={ttl}')

    def test_change_waits_for_another_process_lock(self):
        import fcntl
        import subprocess
        import sys
        import threading
        process = subprocess.Popen([sys.executable, '-c',
            "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()",
            str(Path(self.s.DATA) / 'shelves.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            entered = threading.Event()
            original = self.s.shelves.read
            with patch.object(self.s.shelves, 'read', side_effect=lambda s: (entered.set(), original(s))[1]):
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(self.add)
                    try:
                        self.assertFalse(entered.wait(.2), 'read-modify-write bypassed the process lock')
                    finally:
                        process.stdin.write('\n')
                        process.stdin.flush()
                    future.result(timeout=5)
            self.assertEqual(len(self.s.shelf_list('ana')['wishes']), 1)
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()

    def test_failed_commit_preserves_undo_history(self):
        s = self.s
        self.add()
        journal = Path(s.DATA) / 'undo.jsonl'
        before = journal.read_bytes()
        for op, kwargs, failed in [('replace', {'slot': 1, 'text': 'replacement'}, 'shelves.json'), ('count', {'count': 6}, 'skald.json')]:
            original = s.shelves.write_private
            def write(path, text):
                if Path(path).name == failed:
                    raise OSError('synthetic write failure')
                return original(path, text)
            with patch.object(s.shelves, 'write_private', side_effect=write):
                with self.assertRaises(OSError):
                    s.shelf_change('ana', op, apply=True, **kwargs)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(s.shelf_list('ana')['wishes'][0]['text'], 'a classic film')
