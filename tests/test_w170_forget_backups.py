"""Forgetting a person removes old backups, using synthetic households only."""
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
from unittest.mock import patch

from test_w92_commands import Household
from skald import backup, commands, leave, members, migrate, setup
from skald.__main__ import main


class ForgetBackups(Household):
    def setUp(self):
        super().setUp()
        cfg = members.fresh_config(['ana', 'ben', 'kim', 'lena'])
        cfg['household']['ages'] = {'kim': 9}
        cfg['household']['guests'] = ['lena']
        cfg['taste']['people']['kim'] = {'prefs': {'kids_age': 9}}
        setup.publish(self.root, cfg, replace=True)
        (self.root/'.skald-format').write_text(json.dumps({'format': migrate.FORMAT, 'release': members.VERSION}))
        self.s.refresh_preferences()
        changed = patch.object(self.s, 'aio_rows_changed')
        changed.start()
        self.addCleanup(changed.stop)

    def run_cli(self, *flags, text=False, code=0):
        out = io.StringIO()
        with patch.object(setup, 'server', return_value=nullcontext(self.s)):
            result = main(['members', 'forget', *flags, *([] if text else ['--json'])], out)
        self.assertEqual(result, code, out.getvalue())
        return out.getvalue() if text else json.loads(out.getvalue())

    def seed(self):
        (self.root/'feedback.jsonl').write_text(''.join(json.dumps(dict(
            id=person, t=1000, who=person, key='tmdb:990170', kind='movie',
            title='Invented Film', action='love'))+'\n' for person in self.s.PEOPLE))
        now = self.s.time.time()
        for offset in (20, 2, 0):
            self.s.backup_state(now-offset*86400)
        backup.before_people(self.s, commands.config(self.s))
        self.s.backup_state(now, archive_name='pre-migrate-5-5-1-'+'a'*32+'.tar.gz')
        (self.root/'backups'/'.state-leftover.tmp').write_text('synthetic leftover')
        return self.saved()

    def saved(self):
        return {p.name: p.read_bytes() for p in (self.root/'backups').iterdir() if p.is_file()}

    def plan(self, person):
        plan = commands.preview(self.s, '', 'ana', answer=dict(op='member_forget', person=person))
        return {**plan, 'source': 'cli', 'read_only': False}

    def search(self, record):
        with patch.object(self.s._scoping, 'private_command', False, create=True):
            self.s.write_search(record)

    def assert_fresh(self, person, result):
        files = list((self.root/'backups').iterdir())
        self.assertEqual([p.name for p in files], [result['fresh_backup']])
        self.assertTrue(backup.verify(files[0])['ok'])
        with tarfile.open(files[0]) as archive:
            for item in archive:
                # The cutoff prevents old connector data from restoring this id.
                if item.name != 'history-forgotten.json':
                    raw = archive.extractfile(item).read()
                    rows = [json.loads(line) for line in raw.splitlines() if line] if item.name.endswith('.jsonl') else json.loads(raw) if raw else None
                    self.assertFalse(leave.contains(rows, person), item.name)
                    self.assertFalse(leave.contains(rows, person.title()), item.name)

    def test_forget_child_replaces_every_old_backup(self):
        old = self.seed()
        result = self.run_cli('kim', '--person', 'ana', '--yes')
        self.assertEqual(result['backups_deleted'], len(old)+1)
        self.assert_fresh('kim', result)
        self.assertIn(f"Older backups deleted: {len(old)+1}; fresh backup: {result['fresh_backup']}.", result['description'])

    def test_forget_removes_own_searches_before_fresh_backup(self):
        self.seed()
        kept = [dict(t=1000, p='ana', q='kim'), dict(t=1001, q='kim')]
        for record in [dict(t=1, p='kim', q='Invented adventure'), *kept,
                       dict(t=self.s.time.time()+86400, p='kim', q='Invented comedy')]:
            self.search(record)
        raw = (self.root/'searches.jsonl').read_bytes()
        plan = self.run_cli('kim', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['searches'], 2)
        self.assertIn('Search records deleted: 2.', plan['description'])
        self.assertIn('Search records deleted: 2.', self.run_cli('kim', '--person', 'ana', text=True))
        commander = commands.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='kim'))
        self.assertEqual(commander['forget']['counts']['searches'], 2)
        self.assertIn('Search records deleted: 2.', commander['description'])
        self.assertEqual((self.root/'searches.jsonl').read_bytes(), raw)
        result = self.run_cli('kim', '--person', 'ana', '--yes', '--plan', plan['plan'])
        self.assertEqual([json.loads(line) for line in (self.root/'searches.jsonl').read_text().splitlines()], kept)
        with tarfile.open(self.root/'backups'/result['fresh_backup']) as archive:
            self.assertEqual([json.loads(line) for line in archive.extractfile('searches.jsonl')], kept)

    def test_new_own_search_stales_forget_plan_and_keeps_backups(self):
        old = self.seed()
        plan = self.plan('kim')
        self.search(dict(t=1000, p='kim', q='Invented adventure'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            commands.apply(self.s, plan)
        self.assertEqual(self.saved(), old)

    def test_new_other_person_search_keeps_forget_plan_valid(self):
        self.search(dict(t=1000, p='kim', q='Invented adventure'))
        plan = self.plan('kim')
        other = dict(t=1001, p='ana', q='kim')
        self.search(other)
        result = commands.apply(self.s, plan)
        self.assertEqual([json.loads(line) for line in (self.root/'searches.jsonl').read_text().splitlines()], [other])
        self.assertIsNotNone(result['fresh_backup'])

    def test_forget_removes_person_interview_decisions_before_fresh_backup(self):
        self.seed()
        with patch.object(self.s, 'interview_rank', return_value=[]):
            for person in ('ana', 'ben'):
                self.s.decide('interview_next', {'person': person}, person=person, allow_llm=False,
                              client='ben' if person == 'ana' else None)
        self.assertTrue(self.s.flush_decisions())
        path = self.root/'decisions.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r['answer']['person'] for r in rows], ['ana', 'ben'])
        plan = self.run_cli('ben', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['decisions'], 1)
        self.assertIn('Decision records deleted: 1.', plan['description'])
        result = self.run_cli('ben', '--person', 'ana', '--yes')
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], rows[:1])
        archive_path = self.root/'backups'/result['fresh_backup']
        self.assertTrue(backup.verify(archive_path)['ok'])
        with tarfile.open(archive_path) as archive:
            self.assertEqual([json.loads(line) for line in archive.extractfile('decisions.jsonl')], rows[:1])

    def test_forget_clears_quarantined_memory_but_preserves_usage_totals(self):
        (self.root/'memory.json.bad').write_text('{"facts":[{"person":"kim"')
        self.assertTrue(self.s.save_usage())
        usage = (self.root/'usage.json').read_bytes()
        self.assert_fresh('kim', self.run_cli('kim', '--person', 'ana', '--yes'))
        self.assertFalse((self.root/'memory.json.bad').exists())
        self.assertEqual((self.root/'usage.json').read_bytes(), usage)

    def test_forget_removes_daily_person_counts_and_stale_daily_writes(self):
        saved = {'memory': {'day': '2026-10-15', 'out': {'active_ana': 2, 'active_kim': 1, 'ops': 3}}}
        self.s._daily = json.loads(json.dumps(saved))
        self.s._daily_save()
        plan = self.run_cli('kim', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['daily'], 1)
        self.assertIn('Daily job records deleted: 1.', plan['description'])
        self.run_cli('kim', '--person', 'ana', '--yes')
        expected = {'active_ana': 2, 'ops': 3}
        self.assertEqual(json.loads((self.root/'daily.json').read_text())['memory']['out'], expected)
        self.assertEqual(self.s._daily['memory']['out'], expected)
        self.s._daily = saved
        self.s._daily_save()
        self.assertEqual(json.loads((self.root/'daily.json').read_text())['memory']['out'], expected)

    def test_daily_write_after_household_erase_still_stops_before_reading_config(self):
        with patch.object(self.s, 'household_erased', return_value=True), \
                patch.object(self.s, 'load_config', side_effect=AssertionError('erased config must not be read')):
            self.assertFalse(self.s._daily_save())

    def test_inflight_search_and_decision_writes_cannot_restore_forgotten_person(self):
        self.seed()
        self.search(dict(t=1000, p='ben', q='Invented adventure'))
        result, workers = {}, []
        search_done, decision_done = threading.Event(), threading.Event()
        backup_state = self.s.backup_state

        def delayed_search():
            self.search(dict(t=1001, p='ben', q='Invented late search'))
            search_done.set()

        def paused_backup(*args, **kwargs):
            self.s._decide_queue.put((str(self.root), {'answer': {'person': 'ben'}}, False))
            self.s._decide_queue.put(decision_done)
            worker = threading.Thread(target=delayed_search)
            workers.append(worker)
            worker.start()
            self.assertFalse(search_done.wait(.1), 'search writes must wait for forget')
            self.assertFalse(decision_done.wait(.1), 'decision writes must wait for forget')
            return backup_state(*args, **kwargs)

        try:
            with patch.object(self.s, 'backup_state', side_effect=paused_backup):
                result = commands.apply(self.s, self.plan('ben'))
        finally:
            for worker in workers:
                worker.join(5)
        self.assertTrue(search_done.is_set())
        self.assertTrue(self.s.flush_decisions())
        self.assertEqual((self.root/'searches.jsonl').read_text(), '')
        if (self.root/'decisions.jsonl').exists():
            self.assertNotIn('ben', (self.root/'decisions.jsonl').read_text())
        self.assert_fresh('ben', result)

    def test_forget_removes_suggestion_and_legacy_wish_records(self):
        kept = dict(person='ana', text='kim', t=1000)
        for name in ('suggest-dismissed.json', 'wishes.json'):
            (self.root/name).write_text(json.dumps([dict(person='kim', text='Invented adventure', t=1000), kept]))
        plan = self.run_cli('kim', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['suggestions'], 1)
        self.assertEqual(plan['forget']['counts']['wishes'], 1)
        self.assertIn('Dismissed suggestions deleted: 1.', plan['description'])
        self.assertIn('Saved wishes deleted: 1.', plan['description'])
        result = self.run_cli('kim', '--person', 'ana', '--yes')
        for name in ('suggest-dismissed.json', 'wishes.json'):
            self.assertEqual(json.loads((self.root/name).read_text()), [kept])
        self.assert_fresh('kim', result)

    def test_forget_removes_old_legacy_settings_copy(self):
        (self.root/'legacy.json').write_text(json.dumps(commands.config(self.s)))
        old = self.seed()
        plan = self.plan('kim')
        self.assertEqual(plan['forget']['counts']['settings_copies'], 1)
        self.assertIn('Older settings copies deleted: 1.', plan['description'])
        cfg = json.loads((self.root/'legacy.json').read_text())
        cfg['household']['labels']['kim'] = 'Invented new name'
        (self.root/'legacy.json').write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, 'changed'):
            commands.apply(self.s, plan)
        self.assertEqual(self.saved(), old)
        self.assert_fresh('kim', self.run_cli('kim', '--person', 'ana', '--yes'))
        self.assertFalse((self.root/'legacy.json').exists())

    def test_forget_log_preview_is_localized(self):
        self.search(dict(t=1000, p='kim', q='Invented adventure'))
        for lang, sentence in (
            ('de', 'Gelöschte Sucheinträge: 1.'),
            ('sr-latn', 'Obrisani zapisi pretrage: 1.'),
            ('sr-cyrl', 'Обрисани записи претраге: 1.'),
        ):
            with self.subTest(lang=lang), patch.object(self.s, 'row_lang', return_value=lang):
                self.assertIn(sentence, self.run_cli('kim', '--person', 'ana')['description'])

    def test_forget_guest_replaces_every_old_backup(self):
        self.seed()
        self.assert_fresh('lena', self.run_cli('lena', '--person', 'ana', '--yes'))

    def test_forget_adult_replaces_every_old_backup(self):
        self.seed()
        self.assert_fresh('ben', self.run_cli('ben', '--person', 'ana', '--yes'))

    def test_forget_away_adult_replaces_every_old_backup(self):
        cfg = commands.config(self.s)
        cfg['household']['away'] = ['ben']
        setup.publish(self.root, cfg, replace=True)
        self.s.refresh_preferences()
        self.seed()
        self.assert_fresh('ben', self.run_cli('ben', '--person', 'ana', '--yes'))

    def test_forget_also_deletes_safety_backup_created_by_migration(self):
        (self.root/'.skald-format').unlink()
        old = self.seed()
        result = self.run_cli('kim', '--person', 'ana', '--yes')
        self.assertEqual(result['backups_deleted'], len(old)+2)
        self.assert_fresh('kim', result)

    def test_preview_counts_backups_on_cli_and_commander(self):
        old = self.seed()
        sentence = f'Older backups to delete: {len(old)}. Skald then takes one fresh backup.'
        plan = self.run_cli('kim', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['backups'], len(old))
        self.assertIn(sentence, plan['description'])
        self.assertIn(sentence, self.run_cli('kim', '--person', 'ana', text=True))
        commander = commands.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='kim'))
        with patch.object(commands, 'preview', return_value=commander):
            card = commands.special(self.s, 'Forget Kim', 'movie', routed=True)['metas'][0]
        self.assertTrue(commander['read_only'])
        self.assertEqual(commander['forget']['counts']['backups'], len(old))
        self.assertIn(sentence, commander['description'])
        self.assertIn(sentence, card['description'])
        self.assertNotIn('settings copies stay unchanged', commander['description'])
        self.assertEqual(self.saved(), old)

    def test_empty_preview_omits_backup_sentence(self):
        plan = self.run_cli('kim', '--person', 'ana')
        self.assertEqual(plan['forget']['counts']['backups'], 0)
        self.assertNotIn('Older backups', plan['description'])

    def test_english_backup_counts_are_neutral(self):
        for count in (0, 1, 2):
            for key, sentence in (
                ('preview', f'Older backups to delete: {count}. Skald then takes one fresh backup.'),
                ('done', f'Older backups deleted: {count}; fresh backup: state-example.tar.gz.'),
                ('failed', f'Older backups deleted: {count}. No backup exists until the next daily backup.'),
            ):
                with self.subTest(count=count, key=key):
                    self.assertEqual(self.s.t('forget.backups_'+key, lang='en', count=count,
                                              name='state-example.tar.gz'), sentence)

    def test_backup_text_is_localized(self):
        count = len(self.seed())
        for lang, sentence in (
            ('de', f'Skald löscht außerdem die älteren Sicherungen ({count}) und erstellt danach eine neue Sicherung.'),
            ('sr-latn', f'Skald briše i starije rezervne kopije ({count}), a zatim pravi novu rezervnu kopiju.'),
            ('sr-cyrl', f'Skald брише и старије резервне копије ({count}), а затим прави нову резервну копију.'),
        ):
            with self.subTest(lang=lang), patch.object(self.s, 'row_lang', return_value=lang):
                self.assertIn(sentence, self.run_cli('kim', '--person', 'ana')['description'])

    def test_german_backup_result_handles_one_copy(self):
        self.assertEqual(self.s.t('forget.backups_done', lang='de', count=1, name='state-example.tar.gz'),
                         'Ältere Sicherungen gelöscht: 1; neue Sicherung: state-example.tar.gz.')
        self.assertEqual(self.s.t('forget.backups_failed', lang='de', count=1),
                         'Ältere Sicherungen gelöscht: 1. Bis zur nächsten täglichen Sicherung gibt es keine Sicherung.')

    def test_stale_plan_leaves_every_backup_untouched(self):
        old = self.seed()
        plan = self.plan('kim')
        (self.root/'feedback.jsonl').write_text((self.root/'feedback.jsonl').read_text()+json.dumps(dict(
            id='new', t=2000, who='kim', key='tmdb:990171', kind='movie', title='Other Film', action='love'))+'\n')
        with self.assertRaises(ValueError):
            commands.apply(self.s, plan)
        self.assertEqual(self.saved(), old)

    def test_failed_forget_keeps_old_backups_and_safety_copy(self):
        old = self.seed()
        plan = self.plan('kim')
        with patch.object(setup, 'publish', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                commands.apply(self.s, plan)
        saved = self.saved()
        self.assertEqual({name: saved[name] for name in old}, old)
        self.assertEqual(len(saved), len(old)+1)

    def test_failed_fresh_backup_keeps_erasure_and_reports_no_backup(self):
        old = self.seed()
        with patch.object(self.s, 'backup_state', side_effect=OSError('synthetic failure')):
            result = self.run_cli('kim', '--person', 'ana', '--yes')
        self.assertEqual(result['backups_deleted'], len(old)+1)
        self.assertIsNone(result['fresh_backup'])
        self.assertEqual(list((self.root/'backups').iterdir()), [])
        self.assertNotIn('kim', commands.config(self.s)['household']['members'])
        self.assertIn('No backup exists until the next daily backup.', result['description'])

    def test_failed_verification_is_reported_in_cli_text(self):
        self.seed()
        with patch.object(self.s, '_backup_verify', side_effect=ValueError('synthetic failure')):
            text = self.run_cli('kim', '--person', 'ana', '--yes', text=True)
        self.assertIn('No backup exists until the next daily backup.', text)
        self.assertEqual(list((self.root/'backups').iterdir()), [])

    def test_backup_failure_after_publication_leaves_no_archive(self):
        self.seed()
        with patch.object(self.s, 'backup_prune', side_effect=OSError('synthetic failure')):
            result = self.run_cli('kim', '--person', 'ana', '--yes')
        self.assertIsNone(result['fresh_backup'])
        self.assertEqual(list((self.root/'backups').iterdir()), [])

    def test_backup_symlink_is_removed_without_touching_target(self):
        old = self.seed()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)/'keep.json'
            target.write_text('synthetic outside file')
            link = self.root/'backups'/'old-copy.json'
            link.symlink_to(target)
            result = self.run_cli('kim', '--person', 'ana', '--yes')
            self.assertEqual(target.read_text(), 'synthetic outside file')
            self.assertFalse(link.is_symlink())
            self.assertEqual(result['backups_deleted'], len(old)+2)
            self.assert_fresh('kim', result)

    def race(self, first):
        self.seed()
        plan = self.plan('kim')
        ready, release, done = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], []
        verify = self.s._backup_verify
        def paused(*args):
            if threading.current_thread().name == 'first':
                ready.set()
                if not release.wait(5):
                    raise AssertionError('backup was not released')
            return verify(*args)
        def forget():
            results.append(commands.apply(self.s, plan))
        def daily():
            self.s.backup_state(self.s.time.time())
        def run(fn, signal=False):
            try:
                fn()
            except Exception as error:
                errors.append(error)
            finally:
                if signal:
                    done.set()
        one, two = (daily, forget) if first == 'daily' else (forget, daily)
        threads = [threading.Thread(name='first', target=run, args=(one,)),
                   threading.Thread(name='second', target=run, args=(two, True))]
        with patch.object(self.s, '_backup_verify', side_effect=paused):
            try:
                threads[0].start()
                self.assertTrue(ready.wait(5))
                threads[1].start()
                self.assertFalse(done.wait(.1), 'the second operation must wait for the first')
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assert_fresh('kim', results[0])

    def test_forget_waits_for_inflight_daily_backup_then_removes_it(self):
        self.race('daily')

    def test_daily_backup_waits_for_forget_then_reads_clean_state(self):
        self.race('forget')
