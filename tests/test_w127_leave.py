"""Leaving, forgetting and temporary absence, with synthetic people and titles."""
import copy
import datetime as dt
import io
import json
import threading
from unittest.mock import patch

from test_w92_commands import Household
from decide_kit import ReplayLLM
from skald import commands as c, history, leave, members, setup, shelves
from skald.__main__ import main


class Leave(Household):
    def plan(self, op, person='ben', actor='ana'):
        plan = c.preview(self.s, 'synthetic change', actor, answer=dict(op=op, person=person,
                         **({'shared': 'drop'} if op == 'member_forget' else {})))
        if plan.get('forget'):
            plan['source'] = 'cli'
        return plan

    def save(self, name, doc):
        (self.root / name).write_text(json.dumps(doc))

    def test_forget_search_is_read_only_and_yes_cannot_erase(self):
        plan = c.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='ben'))
        saved = (self.root/'skald.json').read_bytes()
        with patch.object(c, 'preview', return_value=plan):
            card = c.special(self.s, 'Forget Ben', 'movie', routed=True)['metas'][0]
        self.assertTrue(plan.get('read_only'))
        self.assertTrue(card['description'].endswith(
            'To erase, run on the server: python -m skald members forget ben --person ana --yes'))
        self.assertNotIn('yes ', card['name']+card['description'])
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        c.special(self.s, 'yes '+c.confirm_code(plan), 'movie')
        self.assertEqual((self.root/'skald.json').read_bytes(), saved)

    def test_forget_api_is_read_only_and_cannot_apply_or_set_source(self):
        req = dict(viewer='ana', client={'name': 'synthetic'}, body={'in': {'query': 'Forget Ben'}})
        with ReplayLLM(self.s) as llm:
            llm.arm({'choices': [{'message': {'content': '{"op":"member_forget","person":"ben"}'}}]})
            plan = c.api_preview(self.s, req)['preview']
        self.assertTrue(plan.get('read_only'))
        self.assertTrue(plan['description'].endswith(
            'To erase, run on the server: python -m skald members forget ben --person ana --yes'))
        self.assertNotEqual(plan.get('source'), 'cli')
        self.assertIsNone(c.get_pending(self.s, 'api:synthetic'))
        saved = (self.root/'skald.json').read_bytes()
        for body in ({'plan': plan['plan']}, {'plan': plan['plan'], 'source': 'cli'}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                c.api_apply(self.s, {**req, 'body': body})
        with self.assertRaises(ValueError):
            c.api_preview(self.s, {**req, 'body': {**req['body'], 'source': 'cli'}})
        self.assertEqual((self.root/'skald.json').read_bytes(), saved)

    def test_forget_without_an_actor_shows_the_adult_placeholder(self):
        ending = 'To erase, run on the server: python -m skald members forget ben --person ADULT --yes'
        for surface in ('search', 'api'):
            with self.subTest(surface=surface), ReplayLLM(self.s) as llm:
                llm.arm({'choices': [{'message': {'content': '{"op":"member_forget","person":"ben"}'}}]})
                if surface == 'search':
                    with patch.object(c, 'context', return_value=('w92', None, 'both')):
                        card = c.special(self.s, 'Forget Ben', 'movie', routed=True)['metas'][0]
                    self.assertTrue(card['description'].endswith(ending))
                    self.assertNotIn('yes ', card['name']+card['description'])
                else:
                    plan = c.api_preview(self.s, dict(viewer=None, client={'name': 'synthetic'},
                        body={'in': {'query': 'Forget Ben'}}))['preview']
                    self.assertTrue(plan['read_only'])
                    self.assertTrue(plan['description'].endswith(ending))
        self.assertIsNone(c.get_pending(self.s, 'w92'))
        self.assertIsNone(c.get_pending(self.s, 'api:synthetic'))
        self.assertIn('ben', c.config(self.s)['household']['members'])

    def test_forget_apply_requires_cli_before_touching_data(self):
        self.save('history.json', {'items': [dict(id='tmdb:990150', type='movie', played=True, at=1000, profile='ben')]})
        plan = c.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='ben'))
        saved = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for source in (None, 'search', 'api'):
            for read_only in (False, True):
                attempt = {**plan, 'read_only': read_only}
                attempt.pop('source', None)
                if source is not None:
                    attempt['source'] = source
                with self.subTest(source=source, read_only=read_only):
                    with self.assertRaises(PermissionError) as caught:
                        c.apply(self.s, attempt)
                    self.assertIn('only from the server command line', str(caught.exception))
                    self.assertEqual(saved, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_forget_api_cannot_apply_an_old_writable_plan(self):
        plan = c.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='ben'))
        c.pending(self.s)['api:synthetic'] = (self.s.time.time(), plan)
        with self.assertRaises(PermissionError):
            c.api_apply(self.s, dict(viewer='ana', client={'name': 'synthetic'}, body={'plan': plan['plan']}))
        self.assertIn('ben', c.config(self.s)['household']['members'])

    def test_forget_cli_yes_erases_after_preview(self):
        def run(*flags):
            out = io.StringIO()
            self.assertEqual(main(['members', 'forget', 'ben', '--person', 'ana', '--json', *flags], out), 0, out.getvalue())
            return json.loads(out.getvalue())
        plan = run()
        self.assertFalse(plan.get('read_only'))
        self.assertEqual(plan['source'], 'cli')
        self.assertIn('ben', c.config(self.s)['household']['members'])
        self.assertIn('Done', run('--yes')['name'])
        self.assertNotIn('ben', c.config(self.s)['household']['members'])

    def test_forget_cli_yes_without_an_adult_names_the_missing_flag(self):
        out = io.StringIO()
        self.assertEqual(main(['members', 'forget', 'ben', '--json', '--yes'], out), 2)
        self.assertEqual(json.loads(out.getvalue()), {'error': 'Add --person with the adult who erases'})
        self.assertIn('ben', c.config(self.s)['household']['members'])

    def test_managed_plays_without_a_member_profile_stay_unowned(self):
        cfg = c.config(self.s)
        cfg['plays'] = {'mode': 'off'}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        item = dict(id='tmdb:990150', type='movie', played=True, at=1792008000000)
        (self.root/'sessions.jsonl').write_text(json.dumps(dict(id='synthetic', t=item['at']/1000-10, who='ana'))+'\n')
        self.save('history-forgotten.json', {'ben': '2026-10-15'})
        for profile in (None, 'unknown', 'both'):
            watch = {**item, **({'profile': profile} if profile is not None else {})}
            with self.subTest(profile=profile):
                self.assertEqual(self.s.attribute(watch)[:2], (self.s.GUEST, [self.s.GUEST]))
                self.assertEqual(history.filter_forgotten(self.root, cfg, 'history.json', [watch]), [watch])
                self.assertEqual(history.owners(watch, cfg, windows=history.session_windows(self.root), data=self.root, origin='history.json'), [])
                self.assertNotIn('history_by', watch)
        self.assertEqual(history.owners(item, cfg, data=self.root, origin='imports.jsonl'), ['ana', 'ben'])
        for profile, expected in (('ana', ['ana']), ('household', ['ana', 'ben']), ('together', ['ana', 'ben'])):
            with self.subTest(profile=profile):
                self.assertEqual(history.owners({**item, 'profile': profile}, cfg,
                    windows=history.session_windows(self.root), data=self.root, origin='history.json'), expected)

    def test_forget_managed_shared_plays_ignores_personal_session(self):
        cfg = c.config(self.s)
        cfg['plays'] = {'mode': 'off'}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        items = [dict(id='tmdb:'+str(990150+i), type='movie', played=True, at=1000, profile=profile)
                 for i, profile in enumerate(('household', 'together'))]
        self.save('history.json', {'items': items})
        (self.root/'sessions.jsonl').write_text(json.dumps(dict(id='synthetic', t=0, who='ana'))+'\n')
        for item in items:
            self.assertEqual(self.s.attribute(item)[:2], ('together', ['together']))
        plan = self.plan('member_forget')
        self.assertEqual(plan['forget']['counts']['shared'], {'ana': 2})
        c.apply(self.s, plan)
        self.assertEqual(json.loads((self.root/'history.json').read_text())['items'], [])
        self.assertEqual(set(json.loads((self.root/'history-seen.json').read_text())), {it['id'] for it in items})

    def test_forget_auto_https_shared_play_ignores_personal_session(self):
        cfg = c.config(self.s)
        cfg['history']['source'] = 'none'
        setup.publish(self.root, cfg, replace=True)
        (self.root/'credentials').mkdir()
        self.save('credentials/aiostreams.json', {})
        self.s.refresh_preferences()
        item = dict(id='tmdb:990152', type='movie', played=True, at=1000, profile='household')
        self.save('history.json', {'items': [item]})
        (self.root/'sessions.jsonl').write_text(json.dumps(dict(id='synthetic', t=0, who='ana'))+'\n')
        self.assertEqual(self.s.attribute(item)[:2], ('together', ['together']))
        c.apply(self.s, self.plan('member_forget'))
        self.assertEqual(json.loads((self.root/'history.json').read_text())['items'], [])
        self.assertIn(item['id'], json.loads((self.root/'history-seen.json').read_text()))

    def test_forget_removes_only_that_persons_plays_switch(self):
        cfg = c.config(self.s)
        cfg['plays'] = {'people': {'ana': False, 'ben': False, 'together': True}}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, self.plan('member_forget'))
        loaded, source = members.load_config(str(self.root), {}, readonly=True)
        self.assertEqual(source, 'file')
        self.assertEqual(loaded['plays'], {'people': {'ana': False, 'together': True}})
        self.assertNotIn('ben', self.s.PEOPLE)

    def test_moved_out_away_back_and_undo_keep_plays_switch_loadable(self):
        cfg = c.config(self.s)
        cfg['plays'] = {'people': {'ben': False}}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        for ops in (('member_remove',), ('member_away', 'member_back')):
            for op in ops:
                c.apply(self.s, self.plan(op))
                loaded, source = members.load_config(str(self.root), {}, readonly=True)
                self.assertEqual(source, 'file')
                self.assertEqual(loaded['plays'], cfg['plays'])
            for _ in ops:
                c.undo(self.s, ['ana'])
                loaded, source = members.load_config(str(self.root), {}, readonly=True)
                self.assertEqual(source, 'file')
                self.assertEqual(loaded['plays'], cfg['plays'])
            self.assertEqual(c.config(self.s), cfg)

    def test_guest_expiry_and_undo_keep_plays_switch_loadable(self):
        now = dt.datetime(2026, 10, 15, 12)
        with patch.object(self.s, 'local_now', side_effect=lambda ts=None: now):
            c.apply(self.s, c.preview(self.s, '', 'ana', answer=dict(op='member_create', person='kim', label='Kim', guest=True, days=1)))
            cfg = c.config(self.s)
            cfg['plays'] = {'people': {'kim': False}}
            setup.publish(self.root, cfg, replace=True)
            self.s.refresh_preferences()
            now += dt.timedelta(days=2)
            c.expire(self.s)
            self.assertNotIn('kim', self.s.PEOPLE)
            loaded, source = members.load_config(str(self.root), {}, readonly=True)
            self.assertEqual(source, 'file')
            self.assertEqual(loaded['plays'], cfg['plays'])
            c.undo(self.s, ['ana'])
            loaded, source = members.load_config(str(self.root), {}, readonly=True)
            self.assertEqual(source, 'file')
            self.assertEqual(loaded['plays'], cfg['plays'])
            self.assertIn('kim', self.s.PEOPLE)

    def test_guest_removal_and_erasure_clear_end_dates_and_timed_shelves(self):
        s = self.s
        c.apply(s, c.preview(s, '', 'ana', answer=dict(op='member_create', person='kim', label='Kim', guest=True, days=1)))
        for person, partner in (('kim', None), ('ana', 'kim')):
            c.apply(s, c.preview(s, '', 'ana', answer=dict(op='shelf_add', person=person, with_person=partner,
                values=['invented visit', 'always'], days=1)))
        cfg, wishes = c.config(s), shelves.read(s)['wishes']
        changes, _ = leave.prepare(s, cfg, 'kim', s.local_now().date().isoformat())
        self.assertEqual(json.loads(changes['shelves.json'])['wishes'], [])
        with self.subTest(action='forget'):
            self.assertNotIn('kim', leave.settings(copy.deepcopy(cfg), 'kim')['household'].get('guest_until', {}))
        c.apply(s, self.plan('member_remove', 'kim'))
        with self.subTest(action='remove'):
            self.assertNotIn('kim', c.config(s)['household'].get('guest_until', {}))
            self.assertEqual(shelves.read(s)['wishes'], [])
        c.undo(s, ['ana'])
        self.assertEqual(c.config(s), cfg)
        self.assertEqual(shelves.read(s)['wishes'], wishes)

    def test_away_back_and_undo_preserve_child_age_and_permissions(self):
        s = self.s
        c.apply(s, c.preview(s, '', 'ana', answer=dict(op='member_create', person='kim', label='Kim', role='child', age=12)))
        c.apply(s, self.plan('member_away'))
        away = c.journal(s)['ana'][-1]['id']
        c.apply(s, c.preview(s, '', 'ana', answer=dict(op='member_update', person='kim', age=13)))
        c.apply(s, self.plan('member_back'))
        c.undo(s, ['ana'])
        c.apply(s, c.preview(s, '', 'ana', answer=dict(op='undo', values=[away])))
        self.assertEqual(c.config(s)['household']['ages']['kim'], 13)
        self.assertEqual(s.kids_age('kim'), 13)
        for op in ('member_away', 'member_back', 'member_forget'):
            with self.subTest(op=op), self.assertRaises(PermissionError):
                self.plan(op, actor='kim')

    def test_expiry_skips_away_and_forgotten_people_and_their_shelves(self):
        s = self.s
        now = dt.datetime(2026, 10, 15, 12)
        with patch.object(s, 'local_now', side_effect=lambda ts=None: now):
            c.apply(s, c.preview(s, '', 'ana', answer=dict(op='member_create', person='kim', label='Kim')))
            for person, partner in (('kim', None), ('ana', 'kim')):
                c.apply(s, c.preview(s, '', 'ana', answer=dict(op='shelf_add', person=person, with_person=partner,
                    values=['invented visit', 'always'], days=1)))
            c.apply(s, self.plan('member_away', 'kim'))
            saved = {name: (self.root / name).read_bytes() for name in ('skald.json', 'commands.json', 'shelves.json')}
            now += dt.timedelta(days=2)
            c.expire(s)
            self.assertEqual(saved, {name: (self.root / name).read_bytes() for name in saved})
            c.apply(s, self.plan('member_forget', 'kim'))
            saved = {name: (self.root / name).read_bytes() for name in saved}
            c.expire(s)
            self.assertEqual(saved, {name: (self.root / name).read_bytes() for name in saved})

    def test_forget_and_disabled_metrics_refresh_do_not_deadlock(self):
        from skald import metrics
        s = self.s
        plan = self.plan('member_forget')
        self.assertFalse(metrics.settings(s.CONFIG)['enabled'])
        published, refreshing = threading.Event(), threading.Event()
        errors = []
        publish, disable = setup.publish, metrics.disable

        def publish_then_refresh(*args, **kwargs):
            publish(*args, **kwargs)
            published.set()
            self.assertTrue(refreshing.wait(2))

        def bounded_disable(data):
            refreshing.set()  # refresh_preferences holds _home_lock here.
            if not metrics._lock.acquire(timeout=2):
                raise TimeoutError('metrics lock blocked catalog refresh')
            try:
                disable(data)
            finally:
                metrics._lock.release()

        def forget():
            try:
                c.apply(s, plan)
            except Exception as error:
                errors.append(error)

        def catalog():
            try:
                self.assertTrue(published.wait(2))
                s.refresh_preferences()
            except Exception as error:
                errors.append(error)

        workers = [threading.Thread(target=forget), threading.Thread(target=catalog)]
        with patch.object(setup, 'publish', side_effect=publish_then_refresh), \
                patch.object(metrics, 'disable', side_effect=bounded_disable):
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(refreshing.wait(2))
                for worker in workers:
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), 'forget and catalog refresh deadlocked')
            finally:
                for worker in workers:
                    worker.join(3)
        self.assertEqual(errors, [])
        self.assertNotIn('ben', s.PEOPLE)

    def test_feed_save_does_not_block_pins_or_resurrect_forgotten_feeds(self):
        s = self.s
        plan = self.plan('member_forget')
        s._feeds[s.rid('ben'), s.LANG] = {
            'metas': {'movie': [{'id': 'tmdb:990113', 'name': 'Invented Erased Pick', 'type': 'movie'}]}}
        writing, resume, pinned = threading.Event(), threading.Event(), threading.Event()
        errors = []
        dump = s.json.dump

        def blocked_dump(doc, fh, **kwargs):
            if threading.current_thread() is not saver:
                return dump(doc, fh, **kwargs)
            write = fh.write
            def blocked_write(text):
                writing.set()
                if not resume.wait(3):
                    raise TimeoutError('feed write was not released')
                return write(text)
            with patch.object(fh, 'write', side_effect=blocked_write):
                return dump(doc, fh, **kwargs)

        def save():
            try:
                s.save_feeds()
            except Exception as error:
                errors.append(error)

        def pin():
            with s._pin_lock:
                pinned.set()

        saver, manifest = threading.Thread(target=save), threading.Thread(target=pin)
        with patch.object(s.json, 'dump', side_effect=blocked_dump):
            try:
                saver.start()
                self.assertTrue(writing.wait(2))
                manifest.start()
                manifest.join(1)
                self.assertFalse(manifest.is_alive(), 'feed disk write blocked the pin lock')
                self.assertTrue(pinned.is_set())
                c.apply(s, plan)
                s.save_feeds()
                self.assertTrue(saver.is_alive())
            finally:
                resume.set()
                saver.join(3)
                if manifest.ident is not None:
                    manifest.join(3)
        self.assertFalse(saver.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(s._feeds, {})
        self.assertEqual(json.loads((self.root / 'feeds.json').read_text()), {})
        self.assertEqual(list(self.root.glob('*feeds*tmp*')) + list(self.root.glob('.feeds-*')), [])

    def test_shared_mult_person_sync_is_excluded_before_narrowing(self):
        s = self.s
        item = dict(id='tmdb:990101', type='movie', title='Invented Shared', played=True,
                    at=1000, history_by=['ana', 'ben'])
        self.save('history.json', {'items': [item]})
        c.apply(s, self.plan('member_forget'))
        s._connector_write('history', {'items': [item]})
        with patch.object(s, 'tmdb_details', return_value=('movie', dict(id=990101, title=item['title']))):
            p = s.build_profile(s.read_history(), [], remote=False)
        self.assertEqual(p['entries'], [])
        self.assertIn('tmdb:990101', p['seen'])

    def test_shared_import_sync_is_excluded_before_expanding_members(self):
        from skald import history
        s = self.s
        s.write_imports([dict(who='both', key='tmdb:990112', kind='movie', title='Invented Shared Import',
                             action='like', src='plain', conf=1, t='2026-01-01')])
        item = s.read_imports()[0]
        c.apply(s, self.plan('member_forget'))
        s.save_imports([item])
        p = s.build_profile([], [], remote=True)
        self.assertEqual(p['entries'], [])
        self.assertIn(item['key'], p['seen'])
        future = {**item, 't': '2099-01-01'}
        self.assertEqual(history.filter_forgotten(self.root, s.CONFIG, 'imports.jsonl', [future]), [future])

    def test_forget_clears_pins_and_derived_caches(self):
        s = self.s
        cid = s.rid('foryou')
        row = {'metas': {'movie': [{'id': 'tmdb:990102', 'name': 'Invented Old Pick', 'type': 'movie'}]}}
        s._feeds[cid, s.LANG] = row
        s._shown[cid, s.LANG] = row
        s._shown_scoped['ana'] = {(cid, s.LANG): row}
        for store in (s._pin_cache, s._home_cache, s._person_models):
            store['old'] = row
        c.apply(s.command_context(), self.plan('member_forget'))
        self.assertEqual(s.feed(cid, s.LANG), {})
        with s.addon_scope('ana'):
            self.assertEqual(s.feed(cid, s.LANG), {})
        for store in (s._shown, s._pin_cache, s._home_cache, s._person_models, s._feeds_empty):
            self.assertFalse(store)

    def test_removed_connector_poll_cannot_write_or_return_person_data(self):
        s = self.s
        cfg = c.config(s)
        cfg['connectors']['mdblist'] = dict(kind='api', enabled=False, auth=None,
            accounts={'ben': 'file:synthetic.json', 'ana': 'file:other.json'}, options={})
        setup.publish(self.root, cfg, replace=True)
        s.refresh_preferences()
        plan = self.plan('member_forget')
        item = dict(id='tmdb:990103', type='movie', title='Invented In Flight', played=True, at=1000)
        def poll(account, now):
            c.apply(s, plan)
            s._connector_write('mdblist-ben.history', {'source': 'mdblist', 'items': [item]})
            return [{'who': 'ben'}], [1, 1], {'events': 1, 'error': None, 'accounts': {'ben': account}}
        with patch.dict(s.CONNECTOR_POLL, mdblist=poll):
            events, _, stats = s._connector_job({'id': 'mdblist', 'person': 'ben'}, s.time.time())
        self.assertFalse((self.root / 'mdblist-ben.history.json').exists())
        self.assertEqual(events, [])
        self.assertNotIn('ben', stats.get('accounts', {}))
        s._connector_write('mdblist-ben.history', {'source': 'mdblist', 'items': [item]})
        self.assertFalse((self.root / 'mdblist-ben.history.json').exists())

    def test_connector_snapshot_publish_waits_for_forget(self):
        from skald import history
        s = self.s
        now = s.time.time()
        row = s._connector('manual:plain:ben', 'manual_export', 'ben', now, '2026-01-01', now)
        s._connectors[row['id']] = row
        self.save('connectors.json', {'version': 1, 'connectors': {row['id']: row}})
        plan = self.plan('member_forget')
        ready, done = threading.Event(), threading.Event()
        saved, errors = [], []
        fsync = s.os.fsync
        def flushed(fd):
            fsync(fd)
            ready.set()
        def save():
            try:
                saved.append(s._connectors_save({row['id']: row}, s._connectors_generation))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=save)
        try:
            with patch.object(s.os, 'fsync', side_effect=flushed), history.locked(self.root):
                worker.start()
                self.assertTrue(ready.wait(2))
                self.assertFalse(done.wait(.05))
                c.apply(s, plan)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(saved, [False])
        self.assertNotIn(row['id'], json.loads((self.root / 'connectors.json').read_text())['connectors'])

    def test_connector_load_cannot_publish_pre_forget_snapshot(self):
        s = self.s
        now = s.time.time()
        row = s._connector('manual:plain:ben', 'manual_export', 'ben', now, '2026-01-01', now)
        self.save('connectors.json', {'version': 1, 'connectors': {row['id']: row}})
        plan = self.plan('member_forget')
        def seeds(now):
            c.apply(s, plan)
            return {}
        with patch.object(s, 'connector_seeds', side_effect=seeds):
            s.connectors_load(now)
        self.assertNotIn(row['id'], s._connectors)
        self.assertNotIn(row['id'], json.loads((self.root / 'connectors.json').read_text())['connectors'])

    def test_forget_during_model_build_cannot_publish(self):
        s = self.s
        s._profiles['household'] = {'entries': [], 'hash': 'old'}
        plan = self.plan('member_forget')
        def build(*args, **kwargs):
            c.apply(s, plan)
            return {'aff': {}, 'paff': {'ben': {}}, 'cal': False, 'taste_line': 'old'}
        with patch.object(s, 'build_model', side_effect=build), patch.object(s, 'calibration_pairs', return_value=[]):
            self.assertIsNone(s.get_model())
        self.assertIsNone(s._model['m'])
        self.assertFalse((self.root / 'taste_ring.json').exists())

    def test_forget_during_profile_build_cannot_publish(self):
        s = self.s
        def build(*args, **kwargs):
            c.apply(s, self.plan('member_forget'))
            return {'entries': [], 'seen': {'tmdb:990104'}, 'negative': set()}
        with patch.object(s, 'build_profile', side_effect=build), patch.object(s, 'active_profiles', return_value=['household']):
            s.refresh_profiles()
        self.assertEqual(s._profiles, {})

    def test_forget_during_feed_build_cannot_publish(self):
        s = self.s
        cid = s.rid('foryou')
        plan = self.plan('member_forget')
        def build(lang):
            c.apply(s, plan)
            return {cid: {'movie': [{'id': 'tmdb:990105', 'name': 'Invented Stale Pick', 'type': 'movie'}]}}
        with patch.object(s, 'job_specs', return_value=[('synthetic', [cid], 'old', 0, build)]), \
                patch.object(s, 'connectors_tick'):
            s.build_feeds(s.local_now(), {}, {}, only={'synthetic'})
        self.assertEqual(s.feed(cid, s.LANG), {})
        self.assertFalse((self.root / 'feeds.json').exists())

    def test_forget_preserves_resolved_shared_imdb_key(self):
        s = self.s
        item = dict(id='tt9990106', type='movie', title='Invented External', played=True,
                    at=1000, history_by=['ana', 'ben'])
        self.save('history.json', {'items': [item]})
        with patch.object(s, 'tmdb_details', return_value=('movie', dict(id=990106, title=item['title']))):
            c.apply(s, self.plan('member_forget'))
            p = s.build_profile(s.read_history(), [], remote=False)
        self.assertIn('tmdb:990106', p['seen'])
        self.assertEqual(p['entries'], [])

    def test_forget_preview_covers_target_records_in_all_stores(self):
        from skald import leave
        cfg = c.config(self.s)
        baseline = leave.preview(self.s, cfg, 'ben')
        stores = {
            'clients.json': {'clients': [{'id': 'synthetic', 'person': 'ben'}]},
            'commands.json': {'ben': [{'op': 'hide', 'values': ['anime']}]},
            'connectors.json': {'connectors': {'manual:plain:ben': {'person': 'ben'}}},
            'interview.json': [{'who': 'ben', 'key': 'tmdb:990107'}],
            'archetypes.json': {'items': {'ben': {'name': 'Invented Label'}}},
        }
        for name, doc in stores.items():
            with self.subTest(store=name):
                self.save(name, doc)
                self.assertNotEqual(leave.preview(self.s, cfg, 'ben')['stamp'], baseline['stamp'])
                (self.root / name).unlink()
        plan = self.plan('member_forget')
        self.save('clients.json', stores['clients.json'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(self.s, plan)
        self.assertIn('ben', c.config(self.s)['household']['members'])

    def test_forget_erases_raw_and_folded_person_metrics(self):
        from skald import metrics
        s = self.s
        cfg = c.config(s)
        cfg['metrics'] = {'enabled': True}
        setup.publish(self.root, cfg, replace=True)
        s.refresh_preferences()
        now = s.time.time()
        for person, key in (('ana', 990108), ('ben', 990109), ('together', 990110)):
            metrics.record_shown(self.root, cfg, s.rid('foryou'), person,
                                 [{'id': 'tmdb:'+str(key), 'type': 'movie'}], now-100)
            metrics.update(self.root, cfg, [dict(person=person, key='tmdb:'+str(key), kind='movie',
                           source='history', at=now-50)], now)
        with metrics._lock:
            state = metrics._state(self.root, cfg, now)
            state['folded']['2026-01-05'] = {s.rid('foryou'): {
                p: dict(shown=1, played=1, sources={'history': 1}, complete=True, closed=True,
                        extra={'plays': ['synthetic-'+p], 'showings': {'old': {'at': 1}}}) for p in ('ana', 'ben', 'together')}}
            metrics._save(self.root, state)
        other = copy.deepcopy(state['shown'][0])
        plan = self.plan('member_forget')
        metrics.record_shown(self.root, cfg, s.rid('foryou'), 'ben',
                             [{'id': 'tmdb:990111', 'type': 'movie'}], now)
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(s, plan)
        c.apply(s, self.plan('member_forget'))
        state = json.loads((self.root / 'metrics_shown.json').read_text())
        self.assertEqual(state['shown'], [other])
        for filename, field in (('metrics_shown.json', 'folded'), ('metrics.json', 'weeks')):
            doc = json.loads((self.root / filename).read_text())
            self.assertTrue(all(set(people) <= {'ana'} for rows in doc[field].values() for people in rows.values()))
            self.assertNotIn('tmdb:990109', json.dumps(doc))
            self.assertNotIn('synthetic-ben', json.dumps(doc))

    def test_away_only_lab_idea_hidden_until_back(self):
        from skald import lab
        s = self.s
        cfg = c.config(s)
        cfg['metrics'] = {'enabled': True}
        setup.publish(self.root, cfg, replace=True)
        s.refresh_preferences()
        ideas = [dict(id=identity, state='trial', members=people, months='any', moments=[])
                 for identity, people in (('own', ['ben']), ('shared', ['ana', 'ben']))]
        self.save('lab.json', {'ideas': ideas})
        saved = (self.root / 'lab.json').read_bytes()
        c.apply(s, self.plan('member_away'))
        cid = s.rid('lab_own')
        for scope in (None, 'together', 'ana'):
            with self.subTest(scope=scope), s.addon_scope(scope):
                self.assertNotIn('own', [i['id'] for i in lab.select(self.root, s.CONFIG, person=scope)])
                self.assertFalse(s.row_scope_ok(cid))
        with s.addon_scope('ben'):
            self.assertIn('own', [i['id'] for i in lab.select(self.root, s.CONFIG, person='ben')])
            self.assertTrue(s.row_scope_ok(cid))
        self.assertEqual((self.root / 'lab.json').read_bytes(), saved)
        c.apply(s, self.plan('member_back'))
        self.assertIn('own', [i['id'] for i in lab.select(self.root, s.CONFIG)])
        self.assertTrue(s.row_scope_ok(cid))

    def test_away_back_preserves_custom_pair_label(self):
        cfg = c.config(self.s)
        cfg['household']['labels']['pair'] = 'Our Friday picks'
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        for op in ('member_away', 'member_back'):
            c.apply(self.s, self.plan(op))
            self.assertEqual(c.config(self.s)['household']['labels']['pair'], 'Our Friday picks')

    def test_remove_hint_and_neutral_model_routing(self):
        with ReplayLLM(self.s) as llm:
            for text in ('Ben moved out', 'We broke up, remove Ben', 'Ben passed away'):
                llm.arm({'choices': [{'message': {'content': '{"op":"member_remove","person":"ben"}'}}]})
                plan = c.preview(self.s, text, 'ana')
                self.assertTrue(plan['description'].endswith(
                    'To also erase their history, preview it on the server: python -m skald members forget ben --person ana'))
                self.assertEqual(plan['name'], 'Remove member Ben')
        self.assertNotIn('retired', c.config(self.s)['household'])

    def test_leaving_reason_is_not_saved(self):
        s = self.s
        text = 'Ben passed away'
        record, _ = s.log_search(text, deferred=True)
        plan = c.preview(s, text, 'ana', answer=dict(op='member_remove', person='ben', reason='passed away'))
        s.write_search(record)
        c.apply(s, plan)
        self.assertNotIn('passed away', (self.root / 'commands.json').read_text())
        self.assertNotIn(text, s.recent_searches())
        self.assertFalse((self.root / 'searches.jsonl').exists())

    def test_forget_invalidates_live_learning_and_memory(self):
        s = self.s
        store = s._memory_read()
        store['forgotten']['ben'] = 1
        self.save('memory.json', store)
        s._learn['hits'] = [{'key': 'tmdb:990090', 'at': 1}]
        c.apply(s, self.plan('member_forget'))
        s._refresh_learning()
        self.assertEqual(s._learn['hits'], [])
        self.assertNotIn('ben', s._memory_read()['forgotten'])

    def test_forget_invalidates_connector_poll_in_flight_via_commander_context(self):
        s = self.s
        s._connectors.update({'manual:plain:ben': {'person': 'ben'}, 'manual:plain:ana': {'person': 'ana'}})
        generation = s._connectors_generation
        ctx = s.command_context()
        c.apply(ctx, self.plan('member_forget'))
        self.assertNotIn('manual:plain:ben', s._connectors)
        self.assertIn('manual:plain:ana', s._connectors)
        self.assertGreater(s._connectors_generation, generation)

    def test_forget_exact_own_data_and_shared_watches(self):
        cfg = c.config(self.s)
        cfg['taste']['people']['ben'] = {'prefs': {'hide': ['anime']}}
        cfg['taste']['people']['ana'] = {'prefs': {'runtime': {'movie': 100}}}
        cfg['connectors']['jellyfin'] = dict(enabled=False, kind='player', auth='file:jellyfin.json',
            options={'url': 'http://example.invalid', 'users': {'a': 'ana', 'b': 'ben'}})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        for who in ('ana', 'ben'):
            self.s.write_feedback(dict(who=who, key='tmdb:990001', kind='movie', title='Invented Lantern', action='love'))
            self.s.write_imports([dict(who=who, key='tmdb:990002', kind='movie', title='Invented Meadow', action='like', src='plain', conf=1)])
            c.apply(self.s, c.preview(self.s, 'x', who, answer=dict(op='shelf_add', values=['invented meadows', 'always'])))
        c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=dict(op='shelf_add', values=['invented lanterns', 'always'], with_person='ben')))
        items = [dict(id='tmdb:'+str(n), type='movie', title='Invented Watch', played=True, at=1000,
                      history_by=by) for n, by in ((990003, ['ben']), (990004, ['ana']),
                                                   (990005, ['together']), (990006, ['ana', 'ben']))]
        self.save('history.json', {'items': items})
        self.save('lab.json', {'ideas': [dict(id='own', members=['ben']), dict(id='shared', members=['ana', 'ben']), dict(id='other', members=['ana'])]})
        self.save('taste.json', {'people': copy.deepcopy(cfg['taste']['people'])})
        plan = self.plan('member_forget')
        self.assertIn('cannot be undone', plan['description'])
        self.assertEqual(plan['forget']['counts']['links'], 1)
        self.assertEqual(plan['forget']['counts']['shared'], {'ana': 2})
        self.assertEqual(json.loads((self.root / 'history.json').read_text())['items'], items)
        c.apply(self.s, plan)
        after = c.config(self.s)
        self.assertNotIn('ben', after['household']['members'])
        self.assertNotIn('ben', after['household']['labels'])
        self.assertNotIn('ben', after['taste']['people'])
        self.assertEqual(after['taste']['people']['ana'], cfg['taste']['people']['ana'])
        self.assertEqual(after['connectors']['jellyfin']['options']['users'], {'a': 'ana'})
        self.assertEqual([e['who'] for e in self.s.read_feedback()], ['ana'])
        self.assertEqual([e['who'] for e in self.s.read_imports()], ['ana'])
        self.assertEqual([w['scope'] for w in shelves.read(self.s)['wishes']], ['ana'])
        self.assertEqual([i['id'] for i in json.loads((self.root / 'lab.json').read_text())['ideas']], ['shared', 'other'])
        self.assertNotIn('ben', json.loads((self.root / 'taste.json').read_text())['people'])
        with patch.object(self.s, 'tmdb_details', side_effect=lambda it: ('movie', dict(id=int(it['id'][5:]), title=it['title']))), \
                patch.object(self.s, 'told_events', return_value=iter(())):
            p = self.s.build_profile(self.s.read_history(), [], remote=True)
        self.assertTrue({'tmdb:990005', 'tmdb:990006'} <= p['seen'])
        self.assertFalse({'tmdb:990003', 'tmdb:990005', 'tmdb:990006'} & {e['key'] for e in p['entries']})
        self.assertTrue(any(e['key'] == 'tmdb:990001' and 'ana' in e['by'] for e in p['entries']))
        c.undo(self.s, ['ana'])
        self.assertNotIn('ben', c.config(self.s)['household']['members'])
        self.assertFalse(list((self.root / 'backups').glob('pre-people-*')))
        self.assertEqual(len(list((self.root / 'backups').glob('state-*.tar.gz'))), 1)

    def test_forget_after_remove_and_stale_preview(self):
        c.apply(self.s, self.plan('member_remove'))
        plan = self.plan('member_forget')
        self.s.write_feedback(dict(who='ana', key='tmdb:990011', kind='movie', title='Invented Other', action='love'))
        # An unrelated person's new rating is preserved, including when it lands after preview.
        c.apply(self.s, plan)
        self.assertEqual(self.s.read_feedback()[0]['who'], 'ana')

    def test_model_knows_removed_people_and_forget_stays_a_preview(self):
        s = self.s
        c.apply(s, self.plan('member_remove'))
        with ReplayLLM(s) as llm:
            llm.arm({'choices': [{'message': {'content': '{"op":"member_forget","person":"ben"}'}}]})
            card = c.special(s, 'Forget Ben', 'movie', routed=True)
            self.assertIn('"id": "ben"', llm.bodies[0]['messages'][1]['content'])
        self.assertIn('cannot be undone', card['metas'][0]['description'])
        self.assertIn('ben', c.config(s)['household']['members'])
        self.assertTrue(card['metas'][0]['description'].endswith(
            'To erase, run on the server: python -m skald members forget ben --person ana --yes'))
        self.assertIsNone(c.get_pending(s, 'w92'))
        c.special(s, 'yes 42', 'movie')
        self.assertIn('ben', c.config(s)['household']['members'])

    def test_shared_sync_cannot_reteach_but_own_rating_and_later_watch_can(self):
        items = [dict(id='tmdb:990021', type='movie', title='Invented Past', played=True, at=1000, history_by=['together'])]
        self.save('history.json', {'items': items})
        c.apply(self.s, self.plan('member_forget'))
        s = self.s
        def profile(rows):
            with patch.object(s, 'tmdb_details', side_effect=lambda it: ('movie', dict(id=int(it['id'][5:]), title=it['title']))):
                return s.build_profile(rows, [], remote=True)
        p = profile(items)
        self.assertEqual(p['entries'], [])
        self.assertIn('tmdb:990021', p['seen'])
        with patch.object(s, '_profiles', {'household': p}), s.addon_scope('ana'):
            self.assertIn('tmdb:990021', s.search_profile()['seen'])
        s.write_feedback(dict(who='ana', key='tmdb:990021', kind='movie', title='Invented Past', action='love'))
        self.assertEqual(profile(items)['entries'][0]['signal'], 'loved')
        self.assertTrue(profile([{**items[0], 'at': (s.time.time()+86400)*1000}])['entries'])

    def test_forget_private_accounts_caches_and_stale_own_data(self):
        cfg = c.config(self.s)
        cfg['connectors']['mdblist'] = dict(kind='api', enabled=False, auth=None,
            accounts={'ben': 'file:fake-ben.json', 'ana': 'file:fake-ana.json'}, options={})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        (self.root / 'credentials').mkdir()
        for name in ('fake-ben.json', 'fake-ana.json'):
            self.save('credentials/'+name, {'synthetic': True})
        self.save('archetypes.json', {'items': {'ana': {'name': 'Invented Other'}, 'ben': {'name': 'Invented Own'}, 'household': {}}})
        self.save('mdblist-ben.history.json', {'source': 'mdblist', 'items': [], 'watched': ['tmdb:990022']})
        self.save('connectors.json', {'version': 1, 'connectors': {
            'manual:plain:ben': {'person': 'ben'}, 'manual:plain:ana': {'person': 'ana'},
            'mdblist': {'person': None, 'accounts': {'ben': {'person': 'ben'}, 'ana': {'person': 'ana'}}}}})
        plan = self.plan('member_forget')
        self.s.write_feedback(dict(who='ben', key='tmdb:990022', kind='movie', title='Invented New', action='love'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            c.apply(self.s, plan)
        c.apply(self.s, self.plan('member_forget'))
        self.assertFalse((self.root / 'credentials/fake-ben.json').exists())
        self.assertTrue((self.root / 'credentials/fake-ana.json').exists())
        self.assertFalse((self.root / 'mdblist-ben.history.json').exists())
        state = json.loads((self.root / 'connectors.json').read_text())['connectors']
        self.assertNotIn('manual:plain:ben', state)
        self.assertEqual(state['mdblist']['accounts'], {'ana': {'person': 'ana'}})
        self.assertEqual(json.loads((self.root / 'archetypes.json').read_text())['items'], {'ana': {'name': 'Invented Other'}})

    def test_forget_summary_shared_and_other_people_files_unchanged(self):
        from skald import history
        cfg = c.config(self.s)
        cfg['household']['members'].append('eva')
        cfg['household']['labels']['eva'] = 'Eva'
        cfg['taste']['people']['eva'] = {}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        rows = [dict(origin='history.json', key='tmdb:'+str(n), kind='movie', who=by[0], by=by, month='2026-01', src='tv')
                for n, by in ((990031, ['ben']), (990032, ['ana']), (990033, ['together']), (990034, ['ben', 'eva']))]
        self.save(history.SUMMARY, {'v': 1, 'items': rows})
        self.save('trakt-eva.history.json', {'items': []})
        raw = (self.root / 'trakt-eva.history.json').read_bytes()
        plan = self.plan('member_forget')
        self.assertEqual(plan['forget']['counts']['shared'], {'ana': 1, 'eva': 2})
        c.apply(self.s, plan)
        self.assertEqual(history.read(self.root), [rows[1]])
        self.assertEqual((self.root / 'trakt-eva.history.json').read_bytes(), raw)
        self.assertTrue({'tmdb:990033', 'tmdb:990034'} <= history.seen_keys(self.root))

    def test_forget_unlinks_only_player_and_pending_shelf(self):
        cfg = c.config(self.s)
        cfg['history']['source'] = 'jellyfin'
        cfg['connectors']['jellyfin'] = dict(kind='player', enabled=True, auth='file:fake-player.json',
            options={'url': 'http://example.invalid', 'users': {'synthetic-user': 'ben'}})
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        (self.root / 'credentials').mkdir()
        self.save('credentials/fake-player.json', {'synthetic': True})
        shelves.change(self.s, 'ben', 'add', text='invented pending', name='Invented Pending')
        c.apply(self.s, self.plan('member_forget'))
        self.assertEqual(c.config(self.s)['history']['source'], 'none')
        self.assertNotIn('jellyfin', c.config(self.s)['connectors'])
        self.assertFalse((self.root / 'credentials/fake-player.json').exists())
        self.assertEqual(shelves.read(self.s)['plans'], {})

    def test_other_person_undone_rating_stays_undone(self):
        s = self.s
        event = s.write_feedback(dict(who='ana', key='tmdb:990023', kind='movie', title='Invented Undone', action='love'))
        s.write_feedback({**{k: v for k, v in event.items() if k not in ('id', 't')}, 'undo': event['id']})
        c.apply(s, self.plan('member_forget'))
        self.assertEqual(len(s.read_feedback()), 2)
        self.assertEqual(s.active_feedback(s.read_feedback()), {})

    def test_away_cap_and_named_guest_refusals(self):
        c.apply(self.s, self.plan('member_away'))
        for person in ('lena', 'eva', 'ana2', 'ben2'):
            c.apply(self.s, c.preview(self.s, 'x', 'ana', answer=dict(op='member_create', person=person, label=person.title(), guest=True)))
        with self.assertRaisesRegex(ValueError, 'household.members'):
            c.preview(self.s, 'x', 'ana', answer=dict(op='member_create', person='lena2', label='Lena', guest=True))
        for op in ('member_forget', 'member_away', 'member_back'):
            self.assertEqual(c.REGISTRY[op]['scope'], 'household')
            with self.assertRaises(PermissionError):
                self.plan(op, actor='lena')
            if op != 'member_forget':
                with self.assertRaises(PermissionError):
                    self.plan(op, person='lena')
            else:
                self.assertIn('forget', self.plan(op, person='lena'))

    def test_away_shared_shelf_stops_affecting_other_person(self):
        s = self.s
        c.apply(s, c.preview(s, 'x', 'ana', answer=dict(op='shelf_add', values=['invented shared', 'always'], with_person='ben')))
        saved = (self.root / 'shelves.json').read_bytes()
        c.apply(s, self.plan('member_away'))
        with s.addon_scope('ana'):
            self.assertEqual(s.shelf_rows(), [])
        self.assertEqual((self.root / 'shelves.json').read_bytes(), saved)
        c.apply(s, self.plan('member_back'))
        with s.addon_scope('ana'):
            self.assertTrue(s.shelf_rows())

    def test_unchanged_two_adult_folding_is_byte_identical(self):
        s = self.s
        items = [dict(id='tmdb:990024', type='movie', title='Invented Normal', played=True, at=1000)]
        with patch.object(s, 'tmdb_details', return_value=('movie', dict(id=990024, title='Invented Normal'))):
            stages = [(stage, list(events)) for stage, events in s.event_stages(s.time.time(), items, (), False)]
        now = s.time.time()
        expected = json.dumps(s._fold_events(copy.deepcopy(stages), now), sort_keys=True, default=sorted)
        actual = json.dumps(s.fold_events(copy.deepcopy(stages), now), sort_keys=True, default=sorted)
        self.assertEqual(actual.encode(), expected.encode())

    def test_away_back_undo_and_personal_state(self):
        cfg = c.config(self.s)
        cfg['household']['member_rows'] = ['ana', 'ben']
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        c.apply(self.s, c.preview(self.s, 'x', 'ben', answer=dict(op='shelf_add', values=['invented meadows', 'always'])))
        saved = (self.root / 'shelves.json').read_bytes()
        before = c.config(self.s)
        c.apply(self.s, self.plan('member_away'))
        self.assertEqual(self.s.PEOPLE, ('ana', 'ben'))
        self.assertEqual(self.s.member_people(), ('ana',))
        self.assertTrue(c.member_list(self.s)['people'][1]['away'])
        self.assertEqual((self.root / 'shelves.json').read_bytes(), saved)
        self.assertFalse(self.s.row_scope_ok(self.s.rid('ben')))
        with self.s.addon_scope('ben'):
            self.assertTrue(self.s.row_scope_ok(self.s.rid('ben')))
            self.assertTrue(shelves.slots(self.s, 'ben'))
        c.apply(self.s, c.preview(self.s, 'hide anime', 'ben', answer=dict(op='hide', values=['anime'])))
        for op in ('member_back', 'member_away', 'member_forget', 'member_remove', 'row_hide'):
            with self.subTest(op=op), self.assertRaises(PermissionError):
                c.preview(self.s, 'x', 'ben', answer=dict(op=op, person='ana', values=['top10'] if op == 'row_hide' else []))
        c.undo(self.s, ['ben'], actor='ben')
        c.apply(self.s, self.plan('member_back'))
        self.assertEqual(self.s.member_people(), ('ana', 'ben'))
        self.assertEqual(c.config(self.s), before)
        self.assertEqual((self.root / 'shelves.json').read_bytes(), saved)
        c.undo(self.s, ['ana'])
        self.assertEqual(self.s.member_people(), ('ana',))
        c.undo(self.s, ['ana'])
        self.assertEqual(c.config(self.s), before)

    def test_away_guest_equivalence_and_untouched_household(self):
        s = self.s
        before = json.dumps(s.build_profile([], [], remote=False), sort_keys=True, default=sorted)
        self.assertEqual(json.dumps(s.build_profile([], [], remote=False), sort_keys=True, default=sorted), before)
        c.apply(s, self.plan('member_away'))
        baseline = s.taste_hash('household', [])
        s.write_feedback(dict(who='ben', key='tmdb:990012', kind='movie', title='Invented Alone', action='not_for_us'))
        self.assertEqual(s.taste_hash('household', []), baseline)
        p = s.build_profile([], [], remote=True)
        self.assertNotIn('tmdb:990012', p['seen'])
        self.assertNotIn('tmdb:990012', p['negative'])
        with patch.object(s, '_profiles', {'household': p}):
            with s.addon_scope('ben'):
                self.assertIn('tmdb:990012', s.search_profile()['seen'])
                self.assertIn('tmdb:990012', s.remote_masks()[0])
            with s.addon_scope('ana'):
                self.assertNotIn('tmdb:990012', s.search_profile()['seen'])
                self.assertNotIn('tmdb:990012', s.remote_masks()[0])

    def test_refusals_and_six_profile_cap(self):
        for op in ('member_forget', 'member_away'):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.plan(op, 'ana')
        cfg = c.config(self.s)
        cfg['taste']['people']['ben']['prefs'] = {'kids_age': 12}
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        for op in ('member_forget', 'member_away', 'member_back'):
            if op != 'member_forget':
                with self.subTest(op=op), self.assertRaises(PermissionError):
                    self.plan(op)
            else:
                self.assertIn('forget', self.plan(op))
            with self.subTest(op=op), self.assertRaises(PermissionError):
                self.plan(op, 'ana', actor='ben')
            with self.subTest(op=op), self.assertRaises(PermissionError):
                self.plan(op, 'ana', actor='guest')
        for op in ('member_forget', 'member_away'):
            with self.assertRaisesRegex(ValueError, 'last adult'):
                self.plan(op, 'ana')

    def test_cc016e7_saved_form_rule_and_cli(self):
        def run(action, *extra):
            out = io.StringIO()
            self.assertEqual(main(['members', action, 'ben', '--person', 'ana', '--json', *extra], out), 0, out.getvalue())
            return json.loads(out.getvalue())
        plan = run('away')
        self.assertNotIn('away', c.config(self.s)['household'])
        run('away', '--plan', plan['plan'])
        cfg = c.config(self.s)
        # Copied from cc016e7:server.py: unknown household keys survive update.
        old = members.fresh_config(cfg['household']['members'])
        old['household'].update(cfg['household'])
        hh = old['household']
        retired = hh.get('retired', [])
        self.assertTrue(isinstance(retired, list) and all(p in hh['members'] for p in retired)
                        and len(set(retired)) == len(retired) and len(retired) < len(hh['members']))
        self.assertIn(len(hh['members']) - len(retired), members.MEMBER_COUNTS)
        self.assertTrue(all(isinstance(hh['labels'].get(p), str) and hh['labels'][p].strip()
                            and not any(ord(ch) < 32 for ch in hh['labels'][p]) for p in (*hh['members'], 'pair')))
        self.assertEqual(hh['away'], ['ben'])
        run('back', '--yes')
        preview = run('forget')
        self.assertIn('cannot be undone', preview['description'])
        run('forget', '--plan', preview['plan'])
        self.assertNotIn('ben', c.config(self.s)['household']['members'])
