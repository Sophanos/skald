"""Opt-in retention and forgetting, using only synthetic histories."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from eval.profile_golden import NOW, ROOT, offline_server, dump


class HistoryPrivacyTests(unittest.TestCase):
    def test_summary_is_durable_before_raw_replacement(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            s.CONFIG['history']['keep_raw_days'] = 0
            fsync, replace = os.fsync, os.replace
            file_synced = False
            summary_replaced = False
            directory_synced = False
            def synced(fd):
                nonlocal file_synced, directory_synced
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    directory_synced |= summary_replaced
                else:
                    file_synced = True
                return fsync(fd)
            def replaced(source, dest):
                nonlocal summary_replaced
                if Path(dest).name == 'history-summary.json':
                    self.assertTrue(file_synced)
                    summary_replaced = True
                elif Path(dest).name in ('history.json', 'trakt.json', 'imports.jsonl'):
                    self.assertTrue(directory_synced, 'summary must be durable before pruning raw evidence')
                return replace(source, dest)
            with patch('skald.intake.os.fsync', synced), patch('skald.intake.os.replace', replaced):
                self.assertGreater(s.cleanup_history(NOW)['pruned'], 0)
            self.assertTrue(directory_synced)

    def test_summary_directory_sync_failure_keeps_raw_evidence(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            s.CONFIG['history']['keep_raw_days'] = 0
            before = {name: (data / name).read_bytes() for name in ('history.json', 'trakt.json', 'imports.jsonl')}
            fsync = os.fsync
            def failed(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('directory sync failed')
                return fsync(fd)
            with patch('skald.intake.os.fsync', failed), self.assertRaisesRegex(OSError, 'directory sync failed'):
                s.cleanup_history(NOW)
            self.assertEqual(before, {name: (data / name).read_bytes() for name in before})

    def test_cache_only_cleanup_also_writes_summary_first(self):
        from skald.history import SUMMARY, write_private
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('history.json', 'trakt.json', 'imports.jsonl', SUMMARY):
                (data / name).unlink(missing_ok=True)
            s.CONFIG['history']['keep_raw_days'] = 0
            path = data / 'mdblist-ana.history.json'
            path.write_text(json.dumps({'source': 'mdblist', 'items': [], 'watched': ['synthetic']}))
            with patch('skald.history.write_private', wraps=write_private) as writes:
                s.cleanup_history(NOW)
            self.assertEqual([call.args[0] for call in writes.call_args_list], [data / SUMMARY, path])

    def test_cleanup_reports_unresolved_residual_and_honours_no_raw_settings(self):
        for setting in ('days', 'count', 'zero', 'connector'):
            with self.subTest(setting=setting), offline_server(ROOT / 'server.py') as s:
                data = Path(s.DATA)
                for name in ('trakt.json', 'imports.jsonl'):
                    (data / name).unlink()
                unknown = {'id': 'tt0000999', 'type': 'movie', 'played': True,
                           'at': (NOW - 40 * 86400) * 1000, 'client': 'Synthetic client'}
                recent = {**unknown, 'id': 'tmdb:5', 'at': (NOW - 86400) * 1000}
                items = [unknown, recent] if setting == 'count' else [unknown]
                (data / 'history.json').write_text(json.dumps({'items': items}))
                s.tmdb_details = lambda it: None
                if setting == 'days':
                    s.CONFIG['history']['keep_raw_days'] = 30
                elif setting in ('count', 'zero'):
                    s.CONFIG['history']['max_raw_items'] = 1 if setting == 'count' else 0
                else:
                    s.CONFIG['connectors']['history'] = {'keep_raw': False}
                result = s.cleanup_history(NOW)
                forced = setting in ('zero', 'connector')
                self.assertEqual(result['residual'], 0 if forced else 1)
                self.assertEqual(result['pruned'], int(forced))
                self.assertEqual(json.loads((data / 'history.json').read_text())['items'], [] if forced else items)

    def test_history_cleanup_cli_reports_residual_without_preview_writes(self):
        from skald.__main__ import main
        with offline_server(ROOT / 'server.py') as s, patch.dict('os.environ', {'DATA_DIR': s.DATA, 'SKALD_CONFIG': ''}):
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl'):
                (data / name).unlink()
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tt0000999', 'type': 'movie', 'played': True, 'at': (NOW - 40 * 86400) * 1000}]}))
            s.CONFIG['history']['keep_raw_days'] = 30
            (data / 'skald.json').write_text(json.dumps(s.CONFIG))
            before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
            out = io.StringIO()
            self.assertEqual(main(['history', '--cleanup', '--json'], out), 0)
            self.assertEqual(json.loads(out.getvalue())['residual'], 1)
            self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()})
            out = io.StringIO()
            self.assertEqual(main(['history', '--cleanup'], out), 0)
            self.assertIn('Residual raw records: 1', out.getvalue())
            s.CONFIG['history']['max_raw_items'] = 0
            (data / 'skald.json').write_text(json.dumps(s.CONFIG))
            out = io.StringIO()
            self.assertEqual(main(['history', '--cleanup', '--yes', '--json'], out), 0)
            self.assertEqual(json.loads(out.getvalue())['residual'], 0)
            self.assertEqual(json.loads((data / 'history.json').read_text())['items'], [])

    def test_forget_includes_disconnected_connector_histories(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for source in ('trakt', 'mdblist', 'stremio'):
                s.CONFIG['connectors'].pop('trakt_api' if source == 'trakt' else source, None)
                for person in ('ana', 'ben'):
                    item = {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': (NOW - 30 * 86400) * 1000}
                    doc = {'source': source, 'items': [item]}
                    if source == 'mdblist':
                        doc.update(watched=[item], watchlist=[item], activities={'precise': item})
                    if source == 'stremio':
                        doc.update(library={'title': item}, likes={'title': item})
                    (data / f'{source}-{person}.history.json').write_text(json.dumps(doc))
            before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
            preview = forget(data, s.CONFIG, '2026-10-01', 'ana')
            self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()})
            result = forget(data, s.CONFIG, '2026-10-01', 'ana', apply=True)
            self.assertEqual(result['raw'], preview['raw'])
            for source in ('trakt', 'mdblist', 'stremio'):
                doc = json.loads((data / f'{source}-ana.history.json').read_text())
                self.assertEqual(doc['items'], [])
                for field in ('watched', 'watchlist', 'activities', 'library', 'likes'):
                    self.assertFalse(doc.get(field))
                self.assertEqual((data / f'{source}-ben.history.json').read_bytes(), before[f'{source}-ben.history.json'])

    def test_forget_removes_health_repair_copies_and_rejects_older_copies(self):
        import os
        from skald.history import forget
        from skald.health import Health, SERVING_CACHES
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            health = Health(data)
            old = {s.rid('week') + '|en': {'metas': {'movie': [{'id': 'tmdb:5'}]}, 'names': {}}}
            for name in SERVING_CACHES:
                (data / name).write_text(json.dumps({'ana': old} if name == 'shown_scopes.json' else old))
            health.json_checks({'repair': True})
            before = {name: (data / ('health-cache-' + name)).read_bytes() for name in SERVING_CACHES}
            forget(data, s.CONFIG, '2026-10-01', 'ana')
            self.assertEqual(before, {name: (data / ('health-cache-' + name)).read_bytes() for name in SERVING_CACHES})
            forget(data, s.CONFIG, '2026-10-01', 'ana', apply=True)
            for name in SERVING_CACHES:
                good = data / ('health-cache-' + name)
                self.assertFalse(good.exists())
                (data / name).write_text('broken')
                self.assertTrue(health.cache_repair(name))
                self.assertFalse((data / name).exists())
                # An old copy brought back from before forget is still ineligible.
                good.write_bytes(before[name])
                cutoff = (data / 'history-forgotten.json').stat().st_mtime_ns
                os.utime(good, ns=(cutoff - 1_000_000, cutoff - 1_000_000))
                health.cache_repair(name)
                self.assertFalse((data / name).exists())
                (data / name).write_text('{}')
            health.json_checks({'repair': True})
            for name in SERVING_CACHES:
                (data / name).write_text('broken')
                health.cache_repair(name)
                self.assertEqual(json.loads((data / name).read_text()), {})

    def test_forget_invalidates_legacy_learning_and_online_copies(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            config = s.fresh_config(['ana'])
            old = NOW - 30 * 86400
            row = s.rid('week')
            learning = {'served': {'tmdb:5': {'name': 'Forgotten title', 'rows': {}}},
                        'rows': {row: {'shown': 1, 'hits': 1}},
                        'hits': [{'key': 'tmdb:5', 'title': 'Forgotten title', 'row': row, 'at': old, 'share': 1}],
                        'credited': {'tmdb:5': old}, 'lanes': {}, 'moodpicks': {},
                        'ctx': [{'key': 'tmdb:5', 'title': 'Forgotten title', 'at': old}]}
            (data / 'learn.json').write_text(json.dumps(learning))
            s.load_state()
            s.note_exposure(row, [{'id': 'tmdb:5', 'type': 'movie'}], old)
            s.note_tv({'entries': []}, [{'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': old * 1000}], NOW)
            s.save_online()
            with patch.object(s, 'PEOPLE', ['ana']), patch.object(s, 'CONFIG', config):
                self.assertTrue(s.hit_titles())
                before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
                forget(data, config, '2026-10-01', 'ana')
                self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()})
                forget(data, config, '2026-10-01', 'ana', apply=True)
                self.assertFalse((data / 'learn.json').exists())
                self.assertFalse((data / 'online.json').exists())
                self.assertEqual(s.hit_titles(), [])
                self.assertEqual(s.online_state()['obs'], {})
                s._learn_dirty.set()
                s._online_dirty.set()
                s.save_learn()
                self.assertNotIn('tmdb:5', (data / 'learn.json').read_text())
                self.assertNotIn('tmdb:5', (data / 'online.json').read_text())

    def test_post_forget_evidence_survives_folding_in_the_cutoff_month(self):
        from skald.history import forget
        for origin in ('history.json', 'imports.jsonl'):
            with self.subTest(origin=origin), offline_server(ROOT / 'server.py') as s:
                data = Path(s.DATA)
                for name in ('history.json', 'trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                    (data / name).unlink()
                s.person_loves = lambda: []
                forget(data, s.CONFIG, '2026-10-08', 'ana', apply=True)
                if origin == 'history.json':
                    new = {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'history_by': ['ana'],
                           'at': NOW * 1000}
                    stale = {**new, 'id': 'tmdb:7', 'at': (NOW - 6 * 86400) * 1000}
                    (data / origin).write_text(json.dumps({'items': [new, stale]}))
                else:
                    new = {'id': 'a' * 32, 't': '2026-10-11', 'key': 'tmdb:5', 'kind': 'movie',
                           'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love',
                           'src': 'plain', 'conf': 1, 'n': 1}
                    stale = {**new, 'id': 'b' * 32, 'key': 'tmdb:7', 't': '2026-10-05'}
                    (data / origin).write_text(''.join(json.dumps(e) + '\n' for e in (new, stale)))
                s.CONFIG['history']['max_raw_items'] = 0
                s.cleanup_history(NOW + 86400)
                profile = s.build_profile(s.read_history(), [])
                self.assertIn('tmdb:5', profile['seen'])
                self.assertNotIn('tmdb:7', profile['seen'])
                self.assertEqual([e['by'] for e in profile['entries']], [['ana']])

    def test_validated_opt_in_settings(self):
        with offline_server(ROOT / 'server.py') as s:
            cfg = s.fresh_config(['ana', 'ben'])
            cfg['history'].update(keep_raw_days=30, max_raw_items=100)
            cfg['connectors']['history'] = {'kind': 'file_drop', 'enabled': True, 'keep_raw': False}
            self.assertEqual(s.validate_config(cfg)['history']['keep_raw_days'], 30)
            for field in ('keep_raw_days', 'max_raw_items'):
                for bad in (-1, True, '30', 1.5):
                    invalid = copy.deepcopy(cfg)
                    invalid['history'][field] = bad
                    with self.assertRaises(s.ConfigError):
                        s.validate_config(invalid)
            cfg['connectors']['history']['keep_raw'] = 0
            with self.assertRaises(s.ConfigError):
                s.validate_config(cfg)

    def test_default_off_and_folded_fixture(self):
        with offline_server(ROOT / 'server.py') as s:
            before = s.build_profile(s.read_history(), [], s.read_trakt())
            data = Path(s.DATA)
            original = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
            self.assertEqual(s.cleanup_history(NOW), {'pruned': 0, 'summaries': 0, 'residual': 0})
            self.assertEqual(original, {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()})
            s.CONFIG['history']['keep_raw_days'] = 0
            result = s.cleanup_history(NOW)
            self.assertGreater(result['pruned'], 0)
            after = s.build_profile(s.read_history(), [], s.read_trakt())
            self.assertEqual(before['seen'], after['seen'])
            self.assertEqual(before['negative'], after['negative'])
            # Monthly dates can alter decay, age, ordering and inferred abandonment.
            def claims(p):
                return sorted((e['key'], e['kind'], e['title'], sorted(e['by'])) for e in p['entries'])
            self.assertEqual([c for c in claims(before) if c != ('tmdb:1', 'movie', 'Fixture 1', ['together'])], claims(after))
            self.assertEqual((len(before['entries']), len(after['entries'])), (82, 81))
            def key(e):
                return e['key'], e['kind'], e['who'], tuple(e['by'])
            originals = {key(e): e for e in before['entries']}
            stable = ('id', 'stype', 'title', 'year', 'signal', 'flags', 'genres')
            for e in after['entries']:
                self.assertEqual({k: originals[key(e)][k] for k in stable}, {k: e[k] for k in stable})
            summary = json.loads((data / 'history-summary.json').read_text())
            for e in summary['items']:
                if e['month'] is not None:
                    self.assertRegex(e['month'], r'^\d{4}-\d{2}$')
                self.assertNotIn('at', e)
                self.assertFalse({'device', 'client', 'at'} & set(e.get('item', {})))
            self.assertEqual(s.cleanup_history(NOW)['pruned'], 0)

    def test_days_count_and_connector_cleanup(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl'):
                (data / name).unlink()
            items = [{'id': 'tmdb:' + str(i), 'type': 'movie', 'played': True, 'at': (NOW - age * 86400) * 1000}
                     for i, age in ((1, 40), (5, 20), (7, 1))]
            (data / 'history.json').write_text(json.dumps({'items': items}))
            s.CONFIG['history']['keep_raw_days'] = 30
            self.assertEqual(s.cleanup_history(NOW)['pruned'], 1)
            self.assertEqual([it['id'] for it in json.loads((data / 'history.json').read_text())['items']], ['tmdb:5', 'tmdb:7'])
            s.CONFIG['history']['max_raw_items'] = 1
            self.assertEqual(s.cleanup_history(NOW)['pruned'], 1)
            s.CONFIG['connectors']['history'] = {'keep_raw': False}
            self.assertEqual(s.cleanup_history(NOW)['pruned'], 1)
            self.assertEqual(json.loads((data / 'history.json').read_text())['items'], [])

    def test_forget_preview_person_month_boundary_and_preserved_ratings(self):
        from skald import history
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            s.CONFIG['history']['keep_raw_days'] = 0
            s.cleanup_history(NOW)
            before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
            result = history.forget(data, s.CONFIG, '2026-10-08', 'ana')
            self.assertTrue(result['dry_run'])
            self.assertGreater(result['summaries'], 0)
            self.assertEqual(before, {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()})
            applied = history.forget(data, s.CONFIG, '2026-10-08', 'ana', apply=True)
            self.assertEqual(applied['summaries'], result['summaries'])
            self.assertEqual((data / 'feedback.jsonl').read_bytes(), before['feedback.jsonl'])
            self.assertEqual((data / 'taste.json').read_bytes(), before['taste.json'])
            for e in history.read(data):
                if e['month'] and e['month'] <= '2026-10':
                    self.assertNotIn('ana', e['by'])
            with self.assertRaises(ValueError):
                history.forget(data, s.CONFIG, 'invalid', 'ana')

    def test_history_cli_defaults_to_preview(self):
        from skald.__main__ import main
        with offline_server(ROOT / 'server.py') as s, patch.dict('os.environ', {'DATA_DIR': s.DATA, 'SKALD_CONFIG': ''}):
            out = io.StringIO()
            self.assertEqual(main(['history', '--forget-before', '2026-10-08', '--person', 'ana', '--json'], out), 0)
            result = json.loads(out.getvalue())
            self.assertTrue(result['dry_run'])
            self.assertGreater(result['raw'], 0)
            self.assertFalse((Path(s.DATA) / 'history-forgotten.json').exists())
            out = io.StringIO()
            self.assertEqual(main(['history', '--forget-before', '2026-10-08', '--person', 'ana', '--yes', '--json'], out), 0)
            self.assertFalse(json.loads(out.getvalue())['dry_run'])

    def test_migration_adds_empty_summary_without_changing_history(self):
        from skald import migrate, backup
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            original = (data / 'history.json').read_bytes()
            self.assertEqual(migrate.ensure(data)['format'], 5)
            self.assertEqual(json.loads((data / 'history-summary.json').read_text()), {'v': 1, 'items': []})
            self.assertEqual((data / 'history.json').read_bytes(), original)
            self.assertIn('history-summary.json', backup.BACKUP_FILES)
            self.assertIn('history-summary.json', s.BACKUP_FILES)

    def test_month_rounding_changes_only_documented_decay_for_one_title(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': (NOW - 20 * 86400) * 1000}]}))
            before = s.build_profile(s.read_history(), [])['entries'][0]
            s.CONFIG['history']['keep_raw_days'] = 0
            s.cleanup_history(NOW)
            after = s.build_profile(s.read_history(), [])['entries'][0]
            self.assertEqual(before['signal'], after['signal'])
            self.assertEqual(after['weight'], before['weight'] / 2)
            self.assertAlmostEqual(after['age'], 40 + 2 / 3)
            self.assertEqual(before['genres'], after['genres'])
            self.assertEqual(before['by'], after['by'])

    def test_daily_cleanup_preserves_memory_and_explicit_ratings(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            memory = {'ver': 1, 'seq': 1, 'facts': [], 'forgotten': {}, 'at': 0, 'stamp': 'synthetic'}
            (data / 'memory.json').write_text(json.dumps(memory))
            before = {name: (data / name).read_bytes() for name in ('memory.json', 'feedback.jsonl', 'taste.json')}
            s.CONFIG['history']['keep_raw_days'] = 0
            jobs = [job for job in s.DAILY_JOBS if job[0] == 'history']
            self.assertEqual(len(jobs), 1)
            s._daily = None
            with patch.object(s, 'DAILY_JOBS', jobs):
                s.daily_jobs(NOW)
                s.daily_jobs(NOW + 60)
            for name, raw in before.items():
                self.assertEqual((data / name).read_bytes(), raw)
            self.assertTrue(json.loads((data / 'daily.json').read_text())['history']['ok'])

    def test_forget_inferred_memory_reloads_running_model_and_preserves_typed_facts(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            at = NOW - 90 * 86400
            def fact(i, **kw):
                return {'id': 'm' + str(i), 'person': 'ana', 'kind': 'like', 'about': 'g:Drama',
                        'text': 'likes Drama', 'strength': .6, 'n': 3, 'evidence': ['synthetic'],
                        'src': 'implicit', 'via': 'curate', 'status': 'active', 'gates': {},
                        'created': at, 'seen': at, 'confirmed': None, 'end': None, 'into': None, **kw}
            store = {**s._memory_empty(), 'seq': 3, 'facts': [fact(1), fact(2, src='told'), fact(3, confirmed=at)]}
            (data / 'memory.json').write_text(json.dumps(store))
            (data / 'memory.prev.json').write_text(json.dumps(store))
            (data / 'memory.log.jsonl').write_text(json.dumps({'t': at, 'person': 'ana', 'id': 'm1', 'op': 'add'}) + '\n')
            self.assertEqual(len(s.memory_load()['facts']), 3)
            result = forget(data, s.CONFIG, '2026-10-01', 'ana', apply=True)
            self.assertEqual(result['facts'], 1)
            self.assertEqual([f['id'] for f in s.memory_load()['facts']], ['m2', 'm3'])
            self.assertEqual((data / 'memory.log.jsonl').read_text(), '')

    def test_forget_respects_shared_tv_session_attribution(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl'):
                (data / name).unlink()
            at = NOW - 20 * 86400
            items = [{'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': at * 1000},
                     {'id': 'tmdb:7', 'type': 'movie', 'played': True, 'at': (at + 86400) * 1000}]
            (data / 'history.json').write_text(json.dumps({'items': items}))
            sessions = [{'t': at - 60, 'id': '1' * 32, 'who': 'ana', 'fix': None, 'via': 'api'},
                        {'t': at + 86400 - 60, 'id': '2' * 32, 'who': 'ben', 'fix': None, 'via': 'api'}]
            (data / 'sessions.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in sessions))
            result = forget(data, s.CONFIG, '2026-10-01', 'ana', apply=True)
            self.assertEqual(result['raw'], 1)
            self.assertEqual([it['id'] for it in json.loads((data / 'history.json').read_text())['items']], ['tmdb:7'])

    def test_fold_keeps_play_counts_and_rating_without_client_details(self):
        from skald.history import read
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            item = {'id': 'tmdb:5', 'type': 'movie', 'title': 'Synthetic', 'played': True,
                    'plays': 4, 'rating': 8, 'signal': 'loved', 'weight': 3, 'at': NOW * 1000,
                    'client': 'synthetic client', 'device': 'synthetic device'}
            (data / 'trakt.json').write_text(json.dumps({'items': [item]}))
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            e = next(e for e in read(data) if e['origin'] == 'trakt.json')
            self.assertEqual(e['play_count'], 4)
            self.assertEqual(e['item']['rating'], 8)
            self.assertNotIn('client', e['item'])
            self.assertNotIn('device', e['item'])

    def test_forget_first_of_month_preserves_that_month(self):
        from skald.history import forget, read
        with offline_server(ROOT / 'server.py') as s:
            s.CONFIG['history']['keep_raw_days'] = 0
            s.cleanup_history(NOW)
            forget(s.DATA, s.CONFIG, '2026-10-01', 'ana', apply=True)
            self.assertTrue(any(e['month'] == '2026-10' and 'ana' in e['by'] for e in read(s.DATA)))

    def test_cleanup_and_forget_remove_connector_evidence_caches(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for source in ('mdblist', 'stremio'):
                s.CONFIG['connectors'][source] = {'accounts': {'ana': 'file:unused.json'}}
                item = {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'at': (NOW - 20 * 86400) * 1000,
                        'signal': 'watched', 'weight': 2, 'title': 'Synthetic'}
                caches = {'watched': [item], 'watchlist': [item], 'activities': {'watched_at': '2026-09-21'}} if source == 'mdblist' else {
                    'library': {'synthetic': {'state': {'lastWatched': '2026-09-21'}, 'item': item}}, 'likes': {'synthetic': item}}
                path = data / (source + '-ana.history.json')
                original = {'source': source, 'items': [item], **caches}
                for operation in ('cleanup', 'forget'):
                    path.write_text(json.dumps(original))
                    if operation == 'cleanup':
                        s.CONFIG['history']['max_raw_items'] = 0
                        s.cleanup_history(NOW)
                    else:
                        forget(data, s.CONFIG, '2026-10-01', 'ana', apply=True)
                    doc = json.loads(path.read_text())
                    self.assertEqual(doc['items'], [])
                    for field in caches:
                        self.assertFalse(doc.get(field), (source, operation, field))
                    if operation == 'forget':
                        s._connector_write(source + '-ana.history', original)
                        repolled = json.loads(path.read_text())
                        self.assertEqual(repolled['items'], [])
                        for field in caches:
                            self.assertFalse(repolled.get(field), (source, 're-poll', field))

    def test_forget_preserves_a_concurrent_server_import(self):
        import os
        import subprocess
        import sys
        import threading
        from skald import history
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            ready, release = threading.Event(), threading.Event()
            errors = []
            write = history.write_private
            def delayed(path, content):
                if Path(path).name == 'imports.jsonl':
                    ready.set()
                    if not release.wait(5):
                        raise AssertionError('forget writer did not resume')
                return write(path, content)
            def forget():
                try:
                    history.forget(data, s.CONFIG, '2026-10-08', 'ana', apply=True)
                except BaseException as error:
                    errors.append(error)
            incoming = {'key': 'tmdb:500', 'kind': 'movie', 'title': 'New synthetic import', 'year': '2020',
                        'who': 'ana', 'action': 'seen', 'src': 'plain', 'conf': 1, 't': '2026-10-11'}
            script = """import json, os, sys
sys.path.insert(0, 'tests')
from decide_kit import load_server, close_server
s = load_server(os.environ['DATA_DIR'])
print('ready', flush=True)
s.write_imports(json.loads(sys.stdin.read()))
close_server(s)
"""
            with patch.object(history, 'write_private', side_effect=delayed):
                worker = threading.Thread(target=forget)
                worker.start()
                try:
                    self.assertTrue(ready.wait(2))
                    with subprocess.Popen([sys.executable, '-c', script], cwd=ROOT,
                            env={**os.environ, 'DATA_DIR': str(data)}, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                        child.stdin.write(json.dumps([incoming]))
                        child.stdin.close()
                        self.assertEqual(child.stdout.readline().strip(), 'ready')
                        try:
                            child.wait(timeout=.2)
                        except subprocess.TimeoutExpired:
                            pass
                        release.set()
                        worker.join(2)
                        self.assertEqual(child.wait(timeout=3), 0)
                finally:
                    release.set()
                    worker.join(2)
            self.assertEqual(errors, [])
            self.assertIn('tmdb:500', [e['key'] for e in s.read_imports()])

    def test_newly_folded_tv_title_stays_in_the_model(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('imports.jsonl', 'trakt.json', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            s.tmdb_details = lambda it: ('movie', {'id': int(it['id'][5:]), 'title': 'Synthetic', 'genres': []})
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:' + str(i), 'type': 'movie', 'played': True, 'at': (NOW - 40 * 86400) * 1000}
                for i in range(1, 41)]}))
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:500', 'type': 'movie', 'played': True, 'at': NOW * 1000}]}))
            s.cleanup_history(NOW)
            self.assertIn('tmdb:500', s.build_profile(s.read_history(), [])['seen'])

    def test_new_rating_replaces_an_older_folded_dislike_in_the_same_month(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('imports.jsonl', 'trakt.json', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            s.CONFIG['history']['max_raw_items'] = 0
            for rating, age in ((2, 4), (8, 1)):
                (data / 'history.json').write_text(json.dumps({'items': [
                    {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'rating': rating, 'at': (NOW - age * 86400) * 1000}]}))
                s.cleanup_history(NOW)
            entry = s.build_profile(s.read_history(), [])['entries'][0]
            self.assertEqual(entry['item']['rating'], 8)
            self.assertEqual(entry['signal'], 'loved')

    def test_new_months_folded_rating_replaces_the_older_dislike(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('imports.jsonl', 'trakt.json', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            s.CONFIG['history']['max_raw_items'] = 0
            for rating, age in ((2, 30), (8, 1)):
                (data / 'history.json').write_text(json.dumps({'items': [
                    {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'rating': rating,
                     'history_by': ['ana'], 'at': (NOW - age * 86400) * 1000}]}))
                s.cleanup_history(NOW)
            profile = s.build_profile(s.read_history(), [])
            self.assertNotIn('tmdb:5', profile['negative'])
            self.assertEqual([e['signal'] for e in profile['entries']], ['loved'])
            self.assertEqual([e['item']['rating'] for e in profile['entries']], [8])

    def test_raw_tv_evidence_keeps_another_members_folded_evidence(self):
        for refreshed_id in ('tmdb:5', 'tt0000005'):
            with self.subTest(refreshed_id=refreshed_id), offline_server(ROOT / 'server.py') as s:
                data = Path(s.DATA)
                for name in ('imports.jsonl', 'trakt.json', 'feedback.jsonl'):
                    (data / name).unlink()
                s.person_loves = lambda: []
                s.tmdb_details = lambda it: ('movie', {'id': 5, 'title': 'Synthetic', 'genres': []})
                old = {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'rating': 2,
                       'profile': 'household', 'history_by': ['ben'], 'at': (NOW - 30 * 86400) * 1000}
                (data / 'history.json').write_text(json.dumps({'items': [old]}))
                s.CONFIG['history']['max_raw_items'] = 0
                s.cleanup_history(NOW)
                new = {**old, 'id': refreshed_id, 'history_by': ['ana'], 'rating': 8, 'at': NOW * 1000}
                (data / 'history.json').write_text(json.dumps({'items': [new]}))
                entries = s.build_profile(s.read_history(), [])['entries']
                self.assertEqual(sorted((e['by'], e['signal']) for e in entries),
                                 [(['ana'], 'loved'), (['ben'], 'disliked')])

    def test_backdated_import_correction_keeps_log_order_after_folding(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            events = [{'id': 'a' * 32, 't': '2026-10-01', 'key': 'tmdb:500', 'kind': 'movie',
                       'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1},
                      {'id': 'b' * 32, 't': '2026-09-01', 'key': 'tmdb:500', 'kind': 'movie',
                       'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'not_for_us', 'src': 'plain', 'conf': 1, 'n': 1}]
            for name in ('history.json', 'trakt.json', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            def save(events):
                (data / 'imports.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            save(events)
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'disliked')
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'disliked')
            # A later correction to an existing month must move after older claims too.
            save([{**events[0], 'id': 'c' * 32, 't': '2026-10-02'}])
            s.cleanup_history(NOW)
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'loved')

    def test_folded_import_keeps_its_listed_undo_id(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            event = {'id': 'd' * 32, 't': '2026-09-01', 'key': 'tmdb:500', 'kind': 'movie',
                     'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            (data / 'imports.jsonl').write_text(json.dumps(event) + '\n')
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            listed = s.active_imports()[0]
            self.assertEqual(listed['id'], event['id'])
            self.assertEqual(s.undo_import(listed['id'])['id'], event['id'])
            self.assertEqual(s.active_imports(), [])
            self.assertFalse(any(e['key'] == 'tmdb:500' for e in s.build_profile(s.read_history(), [], s.read_trakt())['entries']))

    def test_cli_can_undo_a_folded_import_and_keep_it_undone(self):
        import tempfile
        from skald.__main__ import main
        with offline_server(ROOT / 'server.py') as s, tempfile.TemporaryDirectory() as state:
            data = Path(s.DATA)
            event = {'id': 'e' * 32, 't': '2026-09-01', 'key': 'tmdb:500', 'kind': 'movie',
                     'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            (data / 'imports.jsonl').write_text(json.dumps(event) + '\n')
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            base = ['--snapshot', s.DATA, '--state-dir', state, '--json', 'imports']
            out = io.StringIO()
            self.assertEqual(main(base + ['list'], out), 0)
            listed = json.loads(out.getvalue())['events'][0]
            out = io.StringIO()
            self.assertEqual(main(base + ['undo', listed['id']], out), 0)
            out = io.StringIO()
            self.assertEqual(main(base + ['list'], out), 0)
            self.assertEqual(json.loads(out.getvalue())['events'], [])

    def test_live_push_applies_folded_import_undo_on_the_server(self):
        import shlex
        import subprocess
        import sys
        import tempfile
        from skald import live
        from skald.__main__ import main
        local_run = subprocess.run
        with offline_server(ROOT / 'server.py') as s, tempfile.TemporaryDirectory() as state:
            data = Path(s.DATA)
            event = {'id': 'e' * 32, 't': '2026-09-21', 'key': 'tmdb:500', 'kind': 'movie',
                     'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            other = {**event, 'id': 'f' * 32, 'key': 'tmdb:501', 'who': 'ben'}
            (data / 'imports.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in (event, other)))
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            original = (data / 'history-summary.json').read_bytes()

            def fake_ssh(argv, **kwargs):
                self.assertEqual(argv[0], 'ssh')
                parts = shlex.split(argv[-1])
                script = parts[-1] if parts[0] == 'sudo' else argv[-1]
                script = script.replace('/opt/skald/app', str(ROOT)).replace('python3', sys.executable)
                return local_run(['sh', '-c', script], **kwargs)

            with patch.dict('os.environ', {'SKALD_SSH_HOST': 'synthetic-host.test'}), \
                    patch.object(live, 'REMOTE_DATA', str(data)), patch.object(live, 'pull', return_value=data), \
                    patch.object(live.subprocess, 'run', side_effect=fake_ssh):
                base = ['--live', '--state-dir', state, '--json']
                self.assertEqual(main(base + ['imports', 'undo', event['id']], io.StringIO()), 0)
                self.assertEqual({e['id'] for e in s.active_imports()}, {event['id'], other['id']})
                self.assertEqual(main(base + ['push', 'imports'], io.StringIO()), 0)
                self.assertEqual((data / 'history-summary.json').read_bytes(), original)
                self.assertEqual(main(base + ['push', 'imports', '--apply'], io.StringIO()), 0)
            self.assertEqual([e['id'] for e in s.active_imports()], [other['id']])
            self.assertEqual([p.read_bytes() for p in data.glob('history-summary.json.bak-*')], [original])

    def test_a_movie_does_not_suppress_a_folded_series_with_the_same_tmdb_number(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            s.tmdb_details = lambda it: ('movie' if it['type'] == 'movie' else 'tv',
                                        {'id': 77, 'title': 'Synthetic', 'name': 'Synthetic', 'genres': []})
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:77', 'type': 'movie', 'played': True, 'at': NOW * 1000},
                {'id': 'tmdb:77', 'type': 'series', 'episodes_played': 3, 'at': (NOW - 40 * 86400) * 1000}]}))
            s.CONFIG['history']['keep_raw_days'] = 30
            s.cleanup_history(NOW)
            kinds = {(e['key'], e['kind']) for e in s.build_profile(s.read_history(), [])['entries']}
            self.assertEqual(kinds, {('tmdb:77', 'movie'), ('tmdb:77', 'tv')})

    def test_fold_keeps_a_partially_watched_series_in_continue_watching(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            details = {'id': 77, 'name': 'Synthetic', 'genres': []}
            s.tmdb_details = lambda it: ('tv', details)
            s.tmdb = lambda *args, **kw: details
            s.row_meta = lambda det, *args, **kw: {'id': 'tmdb:77', 'note': kw['note']}
            (data / 'history.json').write_text(json.dumps({'items': [
                {'id': 'tmdb:77', 'type': 'series', 'progress': .4, 'resume_episode': [1, 2], 'at': NOW * 1000}]}))
            s._profiles['household'] = s.build_profile(s.read_history(), [])
            self.assertEqual(s.episodes_feed('en-US')['series'], [{'id': 'tmdb:77', 'note': 'Continue S1E2'}])
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            s._profiles['household'] = s.build_profile(s.read_history(), [])
            self.assertEqual(s.episodes_feed('en-US')['series'], [{'id': 'tmdb:77', 'note': 'Continue S1E2'}])

    def test_a_folded_backdated_correction_wins_over_an_earlier_remaining_raw_claim(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('history.json', 'trakt.json', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            original = {'id': 'a' * 32, 't': '2026-10-01', 'key': 'tmdb:500', 'kind': 'movie',
                        'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            correction = {**original, 'id': 'b' * 32, 't': '2026-09-01', 'action': 'not_for_us'}
            (data / 'imports.jsonl').write_text(json.dumps(original) + '\n' + json.dumps(correction) + '\n')
            s.CONFIG['history']['keep_raw_days'] = 30
            s.cleanup_history(NOW)
            self.assertEqual([e['id'] for e in s.read_imports()], ['a' * 32])
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'disliked')
            newer = {**original, 'id': 'c' * 32, 't': '2026-09-02', 'action': 'like'}
            s.write_imports([newer], keep_ids=True)
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'liked')
            s.cleanup_history(NOW)
            self.assertEqual(s.build_profile([], [])['entries'][0]['signal'], 'liked')

    def test_preserved_import_ids_do_not_merge_other_people_after_forgetting(self):
        from skald.history import forget
        with offline_server(ROOT / 'server.py') as s:
            s.CONFIG['household']['members'].append('cy')
            s.PEOPLE = ('ana', 'ben', 'cy')
            event = {'id': 'f' * 32, 't': '2026-09-01', 'key': 'tmdb:500', 'kind': 'movie',
                     'title': 'Synthetic', 'year': '2020', 'who': 'both', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            (Path(s.DATA) / 'imports.jsonl').write_text(json.dumps(event) + '\n')
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            forget(s.DATA, s.CONFIG, '2026-10-01', 'ana', apply=True)
            self.assertEqual({e['who'] for e in s.active_imports()}, {'ben', 'cy'})
            self.assertEqual({e['id'] for e in s.active_imports()}, {event['id']})

    def test_member_undo_keeps_the_other_member_with_the_same_import_id(self):
        from skald.history import forget
        for folded in (False, True, 'mixed'):
            with self.subTest(folded=folded), offline_server(ROOT / 'server.py') as s:
                s.CONFIG['household']['members'].append('cy')
                s.PEOPLE = ('ana', 'ben', 'cy')
                event = {'id': 'f' * 32, 't': '2026-09-21', 'key': 'tmdb:500', 'kind': 'movie',
                         'title': 'Synthetic', 'year': '2020', 'who': 'both', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
                (Path(s.DATA) / 'imports.jsonl').write_text(json.dumps(event) + '\n')
                if folded is True:
                    s.CONFIG['history']['max_raw_items'] = 0
                    s.cleanup_history(NOW)
                forget(s.DATA, s.CONFIG, '2026-10-01', 'ana', apply=True)
                if folded == 'mixed':
                    for name in ('history.json', 'trakt.json', 'feedback.jsonl'):
                        (Path(s.DATA) / name).unlink(missing_ok=True)
                    correction = {**event, 'id': 'e' * 32, 'who': 'cy', 't': '2026-09-01', 'action': 'not_for_us'}
                    s.save_imports(s.read_imports() + [correction])
                    s.CONFIG['history']['max_raw_items'] = 1
                    s.cleanup_history(NOW)
                cy_before = next(e for e in s.active_imports() if e['who'] == 'cy')
                self.assertEqual(s.undo_write([{'id': event['id']}], {'viewer': 'ben'}),
                                 {'accepted': 1, 'duplicates': 0, 'rejected': []})
                self.assertEqual({e['who'] for e in s.active_imports()}, {'cy'})
                self.assertEqual(s.active_imports()[0], cy_before)
                self.assertEqual(s.undo_write([{'id': event['id']}], {'viewer': 'ben'})['accepted'], 0)
                self.assertEqual(s.undo_write([{'id': event['id']}], {'viewer': 'cy'})['accepted'], 1)
                if folded == 'mixed':
                    self.assertEqual(s.undo_write([{'id': correction['id']}], {'viewer': 'cy'})['accepted'], 1)
                self.assertEqual(s.active_imports(), [])

    def test_reupload_of_an_unchanged_folded_import_is_a_duplicate(self):
        with offline_server(ROOT / 'server.py') as s:
            event = {'id': '1' * 32, 't': '2026-09-21', 'key': 'tmdb:500', 'kind': 'movie',
                     'title': 'Synthetic', 'year': '2020', 'who': 'ana', 'action': 'love', 'src': 'plain', 'conf': 1, 'n': 1}
            (Path(s.DATA) / 'imports.jsonl').write_text(json.dumps(event) + '\n')
            s.CONFIG['history']['max_raw_items'] = 0
            s.cleanup_history(NOW)
            self.assertEqual(s.active_imports()[0]['t'], '2026-09-01')
            self.assertEqual(s.write_imports([event], keep_ids=True), ([], 1, []))
            for changed in ({'t': '2026-08-21'}, {'action': 'not_for_us'}, {'conf': .5}, {'who': 'ben'}):
                self.assertEqual(s.write_imports([{**event, **changed}], keep_ids=True), ([], 0, [0]))
            self.assertEqual(s.read_imports(), [])

    def test_refreshed_imdb_item_suppresses_its_tmdb_summary(self):
        with offline_server(ROOT / 'server.py') as s:
            data = Path(s.DATA)
            for name in ('trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                (data / name).unlink()
            s.person_loves = lambda: []
            s.tmdb_details = lambda it: ('movie' if it['type'] == 'movie' else 'tv',
                                        {'id': 77, 'title': 'Synthetic', 'name': 'Synthetic', 'genres': []})
            s.CONFIG['history']['max_raw_items'] = 0
            # Both identifier formats can name the original evidence being folded.
            for old_id in ('tmdb:77', 'tt0000077'):
                with self.subTest(old_id=old_id):
                    (data / 'history-summary.json').unlink(missing_ok=True)
                    (data / 'history.json').write_text(json.dumps({'items': [
                        {'id': old_id, 'type': 'movie', 'played': True, 'rating': 2, 'at': (NOW - 4 * 86400) * 1000},
                        {'id': 'tmdb:77', 'type': 'series', 'episodes_played': 3, 'at': (NOW - 40 * 86400) * 1000}]}))
                    s.cleanup_history(NOW)
                    (data / 'history.json').write_text(json.dumps({'items': [
                        {'id': 'tt0000077', 'type': 'movie', 'played': True, 'rating': 8, 'at': NOW * 1000}]}))
                    entries = s.build_profile(s.read_history(), [])['entries']
                    movies = [e for e in entries if e['kind'] == 'movie']
                    self.assertEqual(len(movies), 1)
                    self.assertEqual(movies[0]['signal'], 'loved')
                    self.assertEqual(len([e for e in entries if e['kind'] == 'tv']), 1)

    def test_folding_keeps_the_newest_tv_rating_regardless_of_record_order(self):
        for newest_first in (True, False):
            with self.subTest(newest_first=newest_first), offline_server(ROOT / 'server.py') as s:
                data = Path(s.DATA)
                for name in ('trakt.json', 'imports.jsonl', 'feedback.jsonl'):
                    (data / name).unlink()
                s.person_loves = lambda: []
                newest = {'id': 'tmdb:5', 'type': 'movie', 'played': True, 'rating': 8, 'at': (NOW - 1 * 86400) * 1000}
                older = {**newest, 'rating': 2, 'at': (NOW - 4 * 86400) * 1000}
                items = [newest, older] if newest_first else [older, newest]
                (data / 'history.json').write_text(json.dumps({'items': items}))
                s.CONFIG['history']['max_raw_items'] = 0
                s.cleanup_history(NOW)
                entries = s.build_profile(s.read_history(), [])['entries']
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]['item']['rating'], 8)
                self.assertEqual(entries[0]['signal'], 'loved')
