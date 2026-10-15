"""Offline erasure checks with synthetic household data only."""
import copy
from contextlib import contextmanager
import io
import json
import selectors
import subprocess
import sys
import threading
from unittest.mock import Mock, patch

from test_api import raw_request
from test_w92_commands import Household
from skald import commands as c, erase, history, leave, members, migrate, plays, setup
from skald.__main__ import main


class Erase(Household):
    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_cli(self, *args, code=0):
        out = io.StringIO()
        self.assertEqual(main([*args, '--json'], out), code, out.getvalue())
        return json.loads(out.getvalue())

    def test_shared_keep_default_scrubs_owners_and_preserves_taste(self):
        item = dict(id='tmdb:990151', type='movie', played=True, at=1000,
                    profile='household', history_by=['ana', 'ben'])
        self.save('history.json', {'items': [item]})
        self.save('history-summary.json', {'v': 1, 'items': [dict(origin='history.json',
            key=item['id'], kind='movie', who='ben', by=['ana', 'ben'], month='2020-01', src='file', signal=1,
            item={}, title='Invented Watch', year=2020, genres=[], profile='household')]})
        with patch.object(self.s, 'tmdb_details', side_effect=AssertionError('offline')):
            changes, state = leave.prepare(self.s, c.config(self.s), 'ben', '2026-10-15')
        kept = json.loads(changes['history.json'])['items']
        self.assertEqual(kept, [{**item, 'history_by': ['ana']}])
        self.assertEqual(state['shared'], 'keep')
        for name, content in changes.items():
            if content is not None and name != 'history-forgotten.json':
                self.assertFalse(leave.contains(json.loads(content), 'ben'), name)
        self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--yes')
        self.s.refresh_preferences()
        self.assertEqual(history.owners(self.s.read_history()[0], self.s.CONFIG), ['ana'])

    def test_shared_drop_and_plan_choice(self):
        self.save('history.json', {'items': [dict(id='tmdb:990151', type='movie', played=True,
            at=1000, profile='household')]})
        keep = self.run_cli('members', 'forget', 'ben', '--person', 'ana')
        drop = self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--shared', 'drop')
        self.assertNotEqual(keep['plan'], drop['plan'])
        self.assertIn('stay for Ana without Ben', keep['description'])
        self.assertIn('--shared drop', keep['description'])
        self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--shared', 'drop', '--plan', keep['plan'], code=2)
        self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--shared', 'drop', '--plan', drop['plan'])
        self.assertEqual(json.loads((self.root/'history.json').read_text())['items'], [])
        self.assertIn('tmdb:990151', history.seen_keys(self.root))

    def test_shared_keep_preview_needs_no_tmdb(self):
        self.save('history.json', {'items': [dict(id='tt990151', type='movie', played=True,
            at=1000, profile='household')]})
        with patch.object(self.s, 'tmdb_details', side_effect=AssertionError('offline')):
            plan = c.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='ben'))
        self.assertEqual(plan['forget']['counts']['shared'], {'ana': 1})

    def test_shared_cache_survives_later_pruning_without_erased_person(self):
        row = ['12345678', 'movie', 'tmdb:990151', 1000, 1, 0, 0, 1, 1, None, None, None]
        self.save('plays.json', {'records': [{'person': 'together', 'row': row}],
            'https': {'together': {'rows': [row]}, 'ben': {'rows': [row]}}})
        self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--yes')
        cfg = c.config(self.s)
        saved = plays.state(self.s)
        self.assertFalse(leave.contains(saved, 'ben'))
        self.assertEqual(plays.prune_cache(self.root, cfg, copy.deepcopy(saved), 2000)['records'], saved['records'])
        self.assertEqual(len(saved['records']), 1)

    def test_mapping_alone_gets_odin_reminder(self):
        cfg = c.config(self.s)
        cfg['connectors']['aiostreams_db'] = dict(kind='player', enabled=True,
            options=dict(path='/synthetic/missing.db', profiles={'abcdef01': 'ben'}))
        setup.publish(self.root, cfg, replace=True)
        plan = self.run_cli('members', 'forget', 'ben', '--person', 'ana')
        self.assertIn("Ben's Odin login", plan['description'])

    def test_children_and_guests_can_be_forgotten_by_cli(self):
        for name, flags in (('kid', ['--age', '12']), ('visitor', ['--guest', '--days', '2'])):
            self.run_cli('members', 'add', name, '--person', 'ana', '--yes', *flags)
            cfg = c.config(self.s)
            cfg['plays'] = {'people': {name: False}}
            setup.publish(self.root, cfg, replace=True)
            self.s.refresh_preferences()
            self.run_cli('shelf', 'add', 'invented flowers', '--person', name, '--yes')
            self.save('history.json', {'items': [dict(id='tmdb:990151', type='movie', played=True, at=1000, profile=name)]})
            self.run_cli('members', 'forget', name, '--person', 'ana', '--yes')
            self.s.refresh_preferences()
            self.assertFalse(leave.contains(c.config(self.s), name))
            for path in self.root.glob('*.json'):
                if path.name != 'history-forgotten.json':
                    self.assertFalse(leave.contains(json.loads(path.read_text()), name), path.name)

    def test_forget_actor_must_be_current_adult(self):
        self.run_cli('members', 'add', 'kid', '--age', '12', '--person', 'ana', '--yes')
        self.run_cli('members', 'add', 'visitor', '--guest', '--person', 'ana', '--yes')
        self.run_cli('members', 'add', 'away', '--person', 'ana', '--yes')
        self.run_cli('members', 'away', 'away', '--person', 'ana', '--yes')
        for actor in ('kid', 'visitor', 'away', 'ben'):
            with self.subTest(actor=actor):
                result = self.run_cli('members', 'forget', 'ben', '--person', actor, '--yes', code=2)
                self.assertNotIn('could not read or write', result['error'])

    def test_cli_keeps_filesystem_permission_errors_private(self):
        with patch.object(c, 'member_list', side_effect=PermissionError(13, 'synthetic filesystem failure')):
            result = self.run_cli('members', 'list', code=2)
        self.assertEqual(result['error'], 'could not read or write local data')

    def test_saved_odin_login_and_mapping_are_removed_with_reminder(self):
        login = dict(uuid='abcdef010000', password='synthetic', member='ben')
        self.save('credentials/aiostreams.json', {'ben': login, 'ana': {**login, 'uuid': 'abcdef020000', 'member': 'ana'}})
        cfg = c.config(self.s)
        cfg['history']['source'] = 'aiostreams_db'
        cfg['connectors']['aiostreams_db'] = dict(kind='player', enabled=True,
            options=dict(path='/synthetic/missing.db', profiles={'abcdef01': 'ben', 'abcdef02': 'ana'}))
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        reminder = "Ben's Odin login and its watch history still exist in Odin and AIOStreams. Remove them there if Ben should lose access."
        plan = self.run_cli('members', 'forget', 'ben', '--person', 'ana')
        self.assertIn(reminder, plan['description'])
        done = self.run_cli('members', 'forget', 'ben', '--person', 'ana', '--plan', plan['plan'])
        self.assertIn(reminder, done['description'])
        self.assertEqual(set(setup.aiostreams_logins(self.root)), {'ana'})
        mappings = self.run_cli('connectors', 'mappings')
        self.assertNotIn('abcdef01', json.dumps(mappings))
        self.assertIn('abcdef02', json.dumps(mappings))

    def test_no_odin_reminder_without_saved_login_or_mapping(self):
        plan = self.run_cli('members', 'forget', 'ben', '--person', 'ana')
        self.assertNotIn('still exist in Odin', plan['description'])

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def household_data(self):
        self.save('addon.json', {'token': 's'*43})
        self.save('.skald-format', {'format': 5, 'release': '1.5.0'})
        self.save('setup.json', {'base': 'https://example.invalid', 'aio_url': 'http://aio.invalid',
            'internal_base': 'http://engine:7000', 'household_member': 'ben', 'children_asked': True})
        self.save('history.json', {'items': [dict(id='tmdb:990151', profile='ben')]})
        (self.root/'imports.jsonl').write_text(json.dumps({'id': 'synthetic', 'who': 'ana'})+'\n')
        self.save('shelves.json', {'wishes': [{'id': 'synthetic', 'scope': 'ana', 'slot': 1,
            'name': 'Invented shelf', 'text': 'invented flowers', 'when': 'always'}], 'plans': {}})
        self.save('commands.json', {'ana': [{'id': 'synthetic'}]})
        self.save('credentials/aiostreams.json', {'ben': {'password': 'synthetic'}})
        self.save('backups/state-synthetic.tar.gz', {'synthetic': True})
        self.save('backups/pre-people-synthetic.json', {'synthetic': True})
        self.save('profile.json', {'synthetic': True})
        self.save('future-personal-file.bin', {'synthetic': True})

    def test_household_preview_counts_without_writes(self):
        self.household_data()
        saved = self.snapshot()
        plan = self.run_cli('erase', '--household')
        self.assertEqual(plan['counts']['people'], 2)
        for kind in ('watches', 'imports', 'shelves', 'settings history', 'logins', 'backups', 'caches'):
            self.assertGreater(plan['counts'][kind], 0, kind)
        self.assertIn('addon.json', plan['kept'])
        self.assertIn('addresses only', plan['description'])
        self.assertEqual(self.snapshot(), saved)

    def test_household_both_confirmations_and_fresh_plan_required(self):
        self.household_data()
        plan = self.run_cli('erase', '--household')
        saved = self.snapshot()
        for flags in (['--yes', '--plan', plan['plan']],
                      ['--yes', '--plan', plan['plan'], '--confirm', 'erase'],
                      ['--plan', plan['plan'], '--confirm', 'ERASE'],
                      ['--yes', '--plan', 'wrong', '--confirm', 'ERASE']):
            with self.subTest(flags=flags):
                self.run_cli('erase', '--household', *flags, code=2)
                self.assertEqual(self.snapshot(), saved)
        self.save('new-unknown-file', {'synthetic': True})
        saved = self.snapshot()
        self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE', code=2)
        self.assertEqual(self.snapshot(), saved)

    def test_household_erase_keeps_only_wiring_and_allows_setup_again(self):
        from skald import erase
        self.household_data()
        token = (self.root/'addon.json').read_bytes()
        plan = self.run_cli('erase', '--household')
        done = self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
        self.assertTrue(done['name'].endswith('Backups outside Skald\'s data folder, for example copies your host makes, are not touched. Delete them yourself.'))
        self.assertLessEqual(set(self.snapshot()), set(erase.KEEP))
        self.assertEqual((self.root/'addon.json').read_bytes(), token)
        self.assertEqual(json.loads((self.root/'setup.json').read_text()), {
            'base': 'https://example.invalid', 'aio_url': 'http://aio.invalid', 'internal_base': 'http://engine:7000'})
        self.s.refresh_preferences()
        self.assertEqual(list(self.s.feed_rows()), [])
        self.s.save_feeds()
        self.assertLessEqual(set(self.snapshot()), set(erase.KEEP))
        result = self.run_cli('setup', 'household', '--member', 'newana', code=2)
        self.assertIn('supply the mounted database', result['error'])
        setup.household(self.root, ['newana', 'newben'], 'en-US', 'UTC', 'United States')
        cfg, source = members.load_config(str(self.root), {}, readonly=True)
        self.assertEqual(source, 'file')
        self.assertEqual(cfg['household']['members'], ['newana', 'newben'])

    def test_household_search_and_api_preview_can_never_apply(self):
        self.household_data()
        plan = c.preview(self.s, '', 'ana', answer=dict(op='household_erase'))
        self.assertTrue(plan['read_only'])
        self.assertTrue(plan['description'].endswith('To erase everything, run on the server: python -m skald erase --household'))
        saved = self.snapshot()
        for source in ('search', 'api', None):
            with self.subTest(source=source), self.assertRaises(PermissionError):
                c.apply(self.s, {**plan, 'read_only': False, 'source': source})
        req = dict(viewer='ana', client={'name': 'synthetic'}, body={'in': {'query': 'erase everything'}})
        with patch.object(self.s, 'decide', return_value={'answer': dict(op='household_erase', kind=None, values=[], person=None)}):
            api = c.api_preview(self.s, req)['preview']
            card = c.special(self.s, 'erase everything', 'movie', routed=True)['metas'][0]
        self.assertTrue(api['read_only'])
        self.assertTrue(card['description'].endswith('To erase everything, run on the server: python -m skald erase --household'))
        with self.assertRaises(ValueError):
            c.api_apply(self.s, {**req, 'body': {'plan': api['plan']}})
        self.assertEqual(self.snapshot(), saved)

    def test_household_terminal_second_confirmation(self):
        from skald import erase
        self.household_data()
        plan = self.run_cli('erase', '--household')
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='wrong'):
            out = io.StringIO()
            self.assertEqual(main(['erase', '--household', '--yes', '--plan', plan['plan']], out), 2)
        self.assertTrue((self.root/'skald.json').exists())
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='ERASE'):
            self.assertEqual(main(['erase', '--household', '--yes', '--plan', plan['plan']], io.StringIO()), 0)
        self.assertLessEqual(set(self.snapshot()), set(erase.KEEP))

    def test_erased_server_and_restart_keep_old_addon_links_empty(self):
        self.household_data()
        plan = self.run_cli('erase', '--household')
        self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
        handler = object.__new__(self.s.Handler)
        handler.send_json = lambda code, body, **kwargs: (code, body)
        code, body = handler.route(['en-US', 'p', 'ben', 'manifest.json'], True)
        self.assertEqual((code, body['catalogs']), (200, []))
        handler = object.__new__(self.s.NotConfiguredHandler)
        handler.path = '/'+('s'*43)+'/en-US/p/ben/manifest.json'
        handler.wfile = io.BytesIO()
        status = []
        handler.send_response = status.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        self.assertEqual(status, [200])
        self.assertEqual(json.loads(handler.wfile.getvalue())['catalogs'], [])

    def test_old_server_cannot_restore_data_after_new_setup(self):
        self.household_data()
        self.s._feeds[(self.s.rid('foryou'), self.s.LANG)] = {'movie': [{'id': 'tmdb:990151', 'name': 'Invented Old Pick'}]}
        plan = self.run_cli('erase', '--household')
        self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
        setup.household(self.root, ['newana'], 'en-US', 'UTC', 'United States')
        self.s.save_feeds()
        self.assertFalse((self.root/'feeds.json').exists())
        self.s.write_search({'q': 'synthetic old search'})
        self.s._decision_write_json(str(self.root/'decide_cache.json'), {'synthetic': True})
        self.s._memory_atomic(str(self.root/'memory.json'), b'{}')
        self.assertFalse((self.root/'searches.jsonl').exists())
        self.assertFalse((self.root/'decide_cache.json').exists())
        self.assertFalse((self.root/'memory.json').exists())

    def test_erase_preserves_guard_inode(self):
        from skald import erase
        with erase.locked(self.root):
            inode = (self.root/erase.LOCK).stat().st_ino
        plan = self.run_cli('erase', '--household')
        self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
        self.assertEqual((self.root/erase.LOCK).stat().st_ino, inode)

    def test_server_write_waits_for_other_process_then_sees_erase(self):
        code = '''
import fcntl, os, sys
from pathlib import Path
data = Path(sys.argv[1])
with (data/'erase.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.readline()
    (data/'.skald-erased').touch()
    (data/'skald.json').unlink()
'''
        with subprocess.Popen([sys.executable, '-c', code, str(self.root)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            writer = threading.Thread(target=self.s.write_search, args=({'q': 'synthetic'},), daemon=True)
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(3), 'guard process did not start')
                self.assertEqual(process.stdout.readline().strip(), 'locked')
                writer.start()
                writer.join(.1)
                self.assertTrue(writer.is_alive(), 'writer bypassed the process guard')
                process.communicate('\n', timeout=3)
                self.assertEqual(process.returncode, 0)
                writer.join(1)
                self.assertFalse(writer.is_alive())
                self.assertFalse((self.root/'searches.jsonl').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)
                if writer.ident is not None:
                    writer.join(3)

    def test_private_snapshot_cannot_publish_after_erase_and_setup(self):
        staged, resume = threading.Event(), threading.Event()
        errors = []
        fsync = self.s.os.fsync
        def sync(fd):
            fsync(fd)
            if threading.current_thread() is worker:
                staged.set()
                if not resume.wait(3):
                    raise TimeoutError('private write was not released')

        def save():
            try:
                setup.write_private(self.root/'profile.json', '{"synthetic": true}\n')
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=save, daemon=True)
        with patch.object(self.s.os, 'fsync', side_effect=sync):
            try:
                worker.start()
                self.assertTrue(staged.wait(1))
                plan = self.run_cli('erase', '--household')
                self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
                setup.household(self.root, ['newana'], 'en-US', 'UTC', 'United States')
            finally:
                resume.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertFalse((self.root/'profile.json').exists())

    def test_command_in_flight_cannot_republish_erased_household(self):
        plan = c.preview(self.s, '', 'ana', answer=dict(op='hide', values=['anime']))
        validate = self.s.validate_config
        erased = False
        def erase_during_validation(cfg, **kwargs):
            nonlocal erased
            if not erased and cfg['taste']['people']['ana'].get('prefs', {}).get('hide') == ['anime']:
                erased = True
                preview = self.run_cli('erase', '--household')
                self.run_cli('erase', '--household', '--yes', '--plan', preview['plan'], '--confirm', 'ERASE')
            return validate(cfg, **kwargs)
        with patch.object(self.s, 'validate_config', side_effect=erase_during_validation):
            with self.assertRaises((PermissionError, ValueError)):
                c.apply(self.s, plan)
        self.assertFalse((self.root/'skald.json').exists())

    def test_journal_cannot_reappear_after_erase_between_publish_and_save(self):
        plan = c.preview(self.s, '', 'ana', answer=dict(op='hide', values=['anime']))
        publish = erase.publish
        def publish_then_erase(s, cfg):
            publish(s, cfg)
            preview = self.run_cli('erase', '--household')
            self.run_cli('erase', '--household', '--yes', '--plan', preview['plan'], '--confirm', 'ERASE')

        with patch.object(erase, 'publish', side_effect=publish_then_erase):
            c.apply(self.s, plan)
        self.assertFalse((self.root/'commands.json').exists())
        self.assertEqual(list(self.root.glob('.commands-*')), [])

    def test_wish_cannot_reappear_after_erase_before_history_lock(self):
        migrate.ensure(self.root)
        plan = c.preview(self.s, '', 'ana', answer=dict(op='wish', values=['invented quiet mysteries']))
        locked = history.locked
        erased = False
        @contextmanager
        def erase_before_history(data):
            nonlocal erased
            if not erased:
                erased = True
                preview = self.run_cli('erase', '--household')
                self.run_cli('erase', '--household', '--yes', '--plan', preview['plan'], '--confirm', 'ERASE')
            with locked(data):
                yield

        with patch.object(history, 'locked', side_effect=erase_before_history):
            c.apply(self.s, plan)
        self.assertFalse((self.root/'wishes.jsonl').exists())

    def test_model_publication_and_pin_shown_do_not_deadlock(self):
        home_held, pin_entered = threading.Event(), threading.Event()
        errors = []
        home = threading.RLock()

        class TimedHome:
            def __enter__(self):
                if not home.acquire(timeout=2):
                    raise TimeoutError('home lock cleanup timeout')

            def __exit__(self, *args):
                home.release()

        note = self.s.taste_ring_note
        def ring(model, now):
            home_held.set()
            if not pin_entered.wait(1):
                raise TimeoutError('pin did not enter')
            return note(model, now)

        def pins(lang):
            pin_entered.set()
            return {}

        def run(fn):
            try:
                fn()
            except Exception as error:
                errors.append(error)

        self.s._profiles['household'] = {'hash': 'synthetic'}
        model = dict(aff={}, paff={}, cal=None, taste_line='synthetic')
        with patch.object(self.s, '_home_lock', TimedHome()), \
                patch.object(self.s, '_memory_read', return_value={'seq': 0}), \
                patch.object(self.s, 'calibration_pairs', return_value=[]), \
                patch.object(self.s, 'title_fix_stamp', return_value=None), \
                patch.object(self.s, 'build_model', return_value=model), \
                patch.object(self.s, 'taste_ring_note', side_effect=ring), \
                patch.object(self.s, 'pin_decision', side_effect=pins):
            model_thread = threading.Thread(target=run, args=(self.s.get_model,), daemon=True)
            pin_thread = threading.Thread(target=run, args=(lambda: self.s.pin_shown(self.s.LANG),), daemon=True)
            model_thread.start()
            self.assertTrue(home_held.wait(1))
            pin_thread.start()
            try:
                model_thread.join(.25)
                pin_thread.join(.25)
                self.assertFalse(model_thread.is_alive() or pin_thread.is_alive(), 'serving threads timed out')
                self.assertEqual(errors, [])
            finally:
                model_thread.join(3)
                pin_thread.join(3)

    def test_in_flight_addon_results_are_empty_and_not_cached_after_erase(self):
        self.household_data()
        meta = {'id': 'tmdb:990151', 'type': 'movie', 'name': 'Invented personal pick'}
        def rows(*args, **kwargs):
            plan = self.run_cli('erase', '--household')
            self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
            return [meta]

        handler = object.__new__(self.s.Handler)
        handler.command = 'GET'
        handler.route_path = '/'+self.s.LANG+'/catalog/movie/'+self.s.rid('foryou')+'.json'
        handler.wfile = io.BytesIO()
        headers = {}
        handler.send_response = lambda code: self.assertEqual(code, 200)
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        with patch.object(self.s, 'feed_rows', return_value=[(self.s.rid('foryou'), ['movie'], 'For You')]), \
                patch.object(self.s, 'row_metas', side_effect=rows), \
                patch.object(self.s, 'note_served'), \
                patch.object(self.s, 'CONNECTOR_CATALOG_DEMAND', False), \
                patch.object(self.s.row_metrics, 'record_shown'):
            handler.route(handler.route_path.strip('/').split('/'), True)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {'metas': []})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        handler.wfile = io.BytesIO()
        handler.send_json(200, {'meta': meta}, 900, cors=True)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {'meta': None})
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_in_flight_api_profile_is_unavailable_after_erase(self):
        body = {'person': 'ana', 'taste': 'invented personal profile'}
        view = Mock(return_value=body)
        def erase_during_view(req):
            plan = self.run_cli('erase', '--household')
            self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
            return body

        client = dict(name='synthetic', person=None, scopes=['read'])
        with patch.object(self.s, '_api_auth', return_value=(client, None)), \
                patch.dict(self.s.API_ROUTES['profile']['methods'], {'GET': ('read', view, 200)}):
            code, _, raw, _ = raw_request(self.s, '/api/v1/profile')
            self.assertEqual((code, json.loads(raw)), (200, body))
            view.side_effect = erase_during_view
            code, headers, raw, _ = raw_request(self.s, '/api/v1/profile')
        self.assertEqual(code, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'invented personal profile', raw)

    def test_in_flight_remote_feedback_is_unavailable_after_erase(self):
        body = {'events': [{'title': 'Invented private feedback'}], 'summary': {'ana': 'Invented taste'}}
        def erase_during_view():
            plan = self.run_cli('erase', '--household')
            self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
            return body

        path = '/taste/'+('s'*43)+'/feedback.json'
        with patch.object(self.s, 'remote_token', return_value='s'*43), \
                patch.object(self.s, 'remote_history', return_value=body) as view:
            code, _, raw, _ = raw_request(self.s, path, handler=self.s.RemoteHandler)
            self.assertEqual((code, json.loads(raw)), (200, body))
            view.side_effect = erase_during_view
            code, headers, raw, _ = raw_request(self.s, path, handler=self.s.RemoteHandler)
        self.assertEqual(code, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'Invented private feedback', raw)
        self.assertNotIn(b'Invented taste', raw)

    def test_in_flight_addon_stats_are_private_after_erase_without_cors(self):
        body = {'taste': 'invented private stats'}
        def erase_during_view():
            plan = self.run_cli('erase', '--household')
            self.run_cli('erase', '--household', '--yes', '--plan', plan['plan'], '--confirm', 'ERASE')
            return body

        path = '/'+('s'*43)+'/stats'
        with patch.object(self.s, 'addon_token', return_value='s'*43), \
                patch.object(self.s, 'stats', return_value=body) as view:
            code, _, raw, _ = raw_request(self.s, path, handler=self.s.Handler)
            self.assertEqual((code, json.loads(raw)), (200, body))
            view.side_effect = erase_during_view
            code, headers, raw, _ = raw_request(self.s, path, handler=self.s.Handler)
        self.assertEqual(code, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertNotIn(b'invented private stats', raw)
