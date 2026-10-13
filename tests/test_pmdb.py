"""PublicMetaDB uses synthetic accounts, fake HTTP and fake title metadata only."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch
from decide_kit import write_legacy, load_server, close_server

NOW = 1791734400.
STAMP = '2026-10-10T12:00:00Z'
KEY = 'SYNTHETIC_PMDB_KEY'


def row(n, tid=None, kind='movie', **fields):
    return {'id': n, 'tmdb_id': tid or n, 'media_type': kind, **fields}


def envelope(items, page=1, size=500):
    return {'items': items[(page-1)*size:page*size], 'total': len(items), 'page': page,
            'perPage': size, 'totalPages': max(1, (len(items)+size-1)//size)}


class PMDBTests(unittest.TestCase):
    def setUp(self):
        from skald import members
        self.addCleanup(members.configure)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        write_legacy(self.data)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.person = self.s.PEOPLE[-1]
        self.s.CONFIG = copy.deepcopy(self.s.CONFIG)
        self.s.CONFIG['connectors']['pmdb'] = {'kind': 'api', 'enabled': True, 'auth': None,
            'accounts': {self.person: 'file:synthetic.token'}, 'options': {}}
        self.s.os.close(self.s._credentials_dir(create=True))
        key = self.data/'credentials'/'synthetic.token'
        key.write_text(KEY)
        key.chmod(0o600)
        self.c = self.s._connector('pmdb:' + self.person, 'api', self.person, NOW, auth='file:synthetic.token')
        self.routes = {'watched': [row(1, watched_at=STAMP), row(2, 1, 'tv', season=1, episode=2, watched_at=None),
                                  row(3, 2, watched_at=None), row(4, 2, watched_at=None)],
                       'resume': [row(5, progress=98, position_ms=980, runtime_ms=1000, updated=STAMP),
                                  row(6, kind='tv', season=2, episode=3, progress=20, updated=STAMP)],
                       'dropped': [row(7, created=STAMP)],
                       'lists': [{'id': 'watch', 'type': 'watchlist'}, {'id': 'custom', 'type': 'custom'}],
                       'lists/watch/items': [row(8)], 'lists/custom/items': [row(9)]}
        self.path = self.data/('pmdb-' + self.person + '.history.json')
        self.http = patch.object(self.s, '_http_get', side_effect=self.fake)
        self.http.start()
        self.addCleanup(self.http.stop)
        metadata = patch.object(self.s, 'tmdb_details', side_effect=self.metadata)
        metadata.start()
        self.addCleanup(metadata.stop)

    def metadata(self, item):
        return ('movie' if item['type'] == 'movie' else 'tv',
                {'id': int(item['id'][5:]), 'title': 'Synthetic title', 'name': 'Synthetic show',
                 'number_of_episodes': 10, 'status': 'Ended', 'genres': [{'name': 'Drama'}]})

    def fake(self, url, headers=None, **kw):
        self.assertTrue(url.startswith('https://publicmetadb.com/api/external/'))
        self.assertEqual(headers, {'Authorization': 'Bearer ' + KEY})
        self.assertNotIn('body', kw)
        self.assertFalse(kw.get('paged'))
        parsed = urllib.parse.urlsplit(url)
        name = parsed.path.split('/api/external/')[1]
        self.assertNotEqual(name, 'lists/custom/items')
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query['perPage'], ['500'])
        return envelope(self.routes[name], int(query['page'][0]))

    def sync(self, now=NOW, c=None):
        return self.s._connector_pmdb(c or self.c, now)

    def items(self):
        return {(it['type'], it['id']): it for it in self.s.read_trakt()}

    def test_signals_kind_null_dates_rewatch_and_provenance(self):
        events, _, _ = self.sync()
        items = self.items()
        self.assertEqual(len(items), 7)
        movie, show = items['movie', 'tmdb:1'], items['series', 'tmdb:1']
        self.assertTrue(movie['played'])
        self.assertEqual(show['last_episode'], [1, 2])
        self.assertEqual(show['episodes_played'], 1)
        self.assertIsNone(show['at'])
        self.assertIsNone(show['first_at'])
        rewatch = items['movie', 'tmdb:2']
        self.assertEqual((rewatch['plays'], rewatch['rewatch']), (2, 1))
        self.assertIn('rewatch', rewatch['flags'])
        self.assertIsNone(rewatch['at'])
        self.assertEqual(items['movie', 'tmdb:7']['signal'], 'abandoned')
        self.assertEqual(items['movie', 'tmdb:8']['signal'], 'interest')
        self.assertNotIn(('movie', 'tmdb:9'), items)
        self.assertEqual({e['src'] for e in events}, {'sync:pmdb'})
        self.assertEqual({e['who'] for e in events}, {self.person})
        profile = self.s.build_profile([], [], self.s.read_trakt(), remote=False)
        self.assertEqual(len(profile['entries']), 7)
        self.assertEqual({e['kind'] for e in profile['entries'] if e['key'] == 'tmdb:1'}, {'movie', 'tv'})

    def test_resume_is_fraction_and_never_a_play(self):
        self.sync()
        it = self.items()['movie', 'tmdb:5']
        self.assertEqual(it['progress'], .98)
        self.assertEqual(it['signal'], 'interest')
        self.assertFalse(it['played'])
        self.assertFalse(self.s.real_play({'item': it, 'signal': it['signal']}))
        show = self.items()['series', 'tmdb:6']
        self.assertEqual(show['resume_episode'], [2, 3])
        self.assertFalse(show['played'])
        self.assertFalse(show.get('episodes_played'))

    def test_unchanged_resync_deletion_and_undrop(self):
        self.sync()
        before = self.path.read_bytes()
        self.assertEqual(self.sync()[0], [])
        self.assertEqual(self.path.read_bytes(), before)
        self.routes['watched'].pop(0)
        self.routes['dropped'] = []
        self.sync(NOW + 60)
        self.assertNotIn(('movie', 'tmdb:1'), self.items())
        self.assertNotIn(('movie', 'tmdb:7'), self.items())
        self.routes['dropped'] = [row(1, created=STAMP)]
        self.sync()
        self.assertEqual(self.items()['movie', 'tmdb:1']['signal'], 'abandoned')
        self.routes['dropped'] = []
        self.routes['watched'].append(row(1, watched_at=STAMP))
        self.sync()
        self.assertEqual(self.items()['movie', 'tmdb:1']['signal'], 'watched')

    def test_two_page_envelopes(self):
        self.routes['watched'] = [row(n, watched_at=STAMP) for n in range(1, 502)]
        value = self.s._pmdb_pages('/watched', KEY, {'bytes': 0, 'pages': 0})
        self.assertEqual(len(value), 501)
        self.assertEqual(value[-1]['id'], 501)

    def test_envelope_validation_and_caps(self):
        for field, value in (('page', 2), ('page', True), ('total', -1), ('totalPages', 101),
                             ('perPage', 501), ('perPage', 0), ('items', {}), ('total', 2)):
            bad = {**envelope([row(1)]), field: value}
            with self.subTest(field=field, value=value), patch.object(self.s, '_http_get', return_value=bad):
                with self.assertRaises((self.s.SchemaMismatch, self.s.PageCapExceeded)):
                    self.s._pmdb_pages('/watched', KEY, {'bytes': 0, 'pages': 0})
        pages = [envelope([row(n) for n in range(501)]),
                 {**envelope([row(n) for n in range(501)], 2), 'total': 502}]
        with patch.object(self.s, '_http_get', side_effect=pages):
            with self.assertRaises(self.s.SchemaMismatch):
                self.s._pmdb_pages('/watched', KEY, {'bytes': 0, 'pages': 0})

    def test_bad_item_keeps_cache(self):
        self.sync()
        before = self.path.read_bytes()
        for bad in (row(1, kind='episode'), row(1, watched_at='bad'), row(1, kind='tv', season=1),
                    row(1, tmdb_id=True), row(1, progress=float('nan'))):
            endpoint = 'resume' if 'progress' in bad else 'watched'
            with self.subTest(bad=bad), patch.dict(self.routes, {endpoint: [bad]}):
                with self.assertRaises(self.s.SchemaMismatch):
                    self.sync()
                self.assertEqual(self.path.read_bytes(), before)

    def test_errors_keep_cache_and_redact_bodies(self):
        self.sync()
        before = self.path.read_bytes()
        for error in (urllib.error.HTTPError('https://publicmetadb.com', 401, KEY, {}, None),
                      urllib.error.HTTPError('https://publicmetadb.com', 429, KEY, {'Retry-After': '9999999'}, None),
                      TimeoutError(KEY), ValueError(KEY)):
            output = io.StringIO()
            def fail(url, *args, **kw):
                if '/dropped?' in url:
                    raise error
                return self.fake(url, *args, **kw)
            with self.subTest(error=type(error).__name__), redirect_stdout(output), redirect_stderr(output), \
                    patch.object(self.s, '_http_get', side_effect=fail):
                with self.assertRaises(Exception) as caught:
                    self.sync()
                self.assertNotIn(KEY, str(caught.exception) + output.getvalue())
                self.assertEqual(self.path.read_bytes(), before)

    def test_accounts_isolated_and_rate_limit_bounded(self):
        other = self.s.PEOPLE[0]
        self.s.CONFIG['connectors']['pmdb']['accounts'][other] = 'file:other.token'
        token = self.data/'credentials'/'other.token'
        token.write_text(KEY)
        token.chmod(0o600)
        self.sync()
        self.routes['watched'] = [row(20, watched_at=STAMP)]
        c = {**self.c, 'id': 'pmdb:' + other, 'person': other, 'auth': 'file:other.token'}
        self.sync(c=c)
        self.assertEqual({it['person'] for it in self.s.read_trakt()}, {other, self.person})
        parent = self.s.connector_seeds(NOW)['pmdb']
        error = urllib.error.HTTPError('https://publicmetadb.com', 429, KEY, {'Retry-After': '999999'}, None)
        with patch.object(self.s, '_http_get', side_effect=error) as http:
            _, _, stats = self.s._connector_pmdb(parent, NOW)
        self.assertEqual(http.call_count, 1)
        self.assertGreater(stats['backoff_until'], NOW)
        self.assertLessEqual(stats['backoff_until'], NOW + self.s.CONNECTOR_IDLE_MAX)
        self.assertEqual(stats['accounts'][self.person]['health']['error'], 'PMDBRateLimited')
        self.assertEqual(stats['accounts'][other]['last_error'], 'PMDBRateLimited')

    def test_trust_applies_once_and_trakt_duplicate_rating_wins(self):
        self.routes['watched'] = [row(1, watched_at=STAMP)]
        self.sync()
        it = self.items()['movie', 'tmdb:1']
        event = next(self.s.trakt_events(NOW, [it]))
        _, weight, _ = self.s.event_weight(event, NOW)
        trakt = {**it, 'src': 'trakt'}
        _, original, _ = self.s.event_weight(next(self.s.trakt_events(NOW, [trakt])), NOW)
        self.assertAlmostEqual(weight, original * .8)
        self.assertLess(weight, original)
        self.s.CONFIG['connectors']['trakt_api'] = {'accounts': {self.person: 'file:synthetic.token'}}
        self.s._connector_write('trakt-' + self.person + '.history', {'items': [trakt]})
        merged = [i for i in self.s.read_trakt() if i['id'] == 'tmdb:1' and i['type'] == 'movie']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['plays'], 1)
        rated = {**trakt, 'rating': 2, 'signal': 'disliked', 'weight': -3}
        self.s._connector_write('trakt-' + self.person + '.history', {'items': [rated]})
        self.assertEqual(self.items()['movie', 'tmdb:1']['rating'], 2)

    def test_odin_completion_wins_without_double_counting(self):
        self.routes['watched'] = [row(1, watched_at=STAMP)]
        self.sync()
        odin = {'id': 'tmdb:1', 'type': 'movie', 'profile': self.person,
                'played': True, 'at': NOW*1000, 'progress': 1}
        with patch.object(self.s, 'attribute', return_value=(self.person, [self.person], {})):
            profile = self.s.build_profile([odin], [], self.s.read_trakt(), remote=False)
        matching = [e for e in self.s.person_entries(profile, self.person) if e['key'] == 'tmdb:1']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['weight'], self.s.event_weight(next(self.s.tv_events(NOW, [odin])), NOW)[1])
        self.assertEqual(matching[0]['item']['progress'], 1)

    def test_probe_get_only_no_writes_and_each_account(self):
        from skald import connectors
        self.http.stop()
        other = self.s.PEOPLE[0]
        self.s.CONFIG['connectors']['pmdb']['accounts'][other] = 'file:synthetic.token'
        before = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        calls = []
        def transport(req, timeout):
            self.assertEqual(req.get_method(), 'GET')
            self.assertIsNone(req.data)
            self.assertEqual(timeout, 10)
            self.assertNotIn(KEY, req.full_url)
            calls.append(req)
            return io.BytesIO(json.dumps(self.fake(req.full_url, {'Authorization': req.get_header('Authorization')})).encode())
        result = connectors.test(self.s, 'pmdb', now=NOW, transport=transport)
        self.assertTrue(result['ok'])
        self.assertEqual(len(calls), 10)
        self.assertEqual(result['connectors'][0]['plays_30d'], 2)
        self.assertEqual({r['person'] for r in result['connectors'][0]['accounts']}, {other, self.person})
        self.assertEqual(before, {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob('*') if p.is_file()})
        self.assertNotIn(KEY, json.dumps(result))

    def test_probe_errors_are_distinct(self):
        from skald import connectors
        for error, message in ((urllib.error.HTTPError('https://publicmetadb.com', 401, KEY, {}, None), 'key rejected'),
                               (urllib.error.HTTPError('https://publicmetadb.com', 429, KEY, {}, None), 'rate limit'),
                               (TimeoutError(KEY), 'timeout'), (ValueError(KEY), 'malformed')):
            with self.subTest(message=message), patch.object(self.s, '_http_get', side_effect=error):
                result = connectors.test(self.s, 'pmdb', now=NOW)
                self.assertFalse(result['ok'])
                self.assertIn(message, result['connectors'][0]['error'])
                self.assertNotIn(KEY, json.dumps(result))

    def test_setup_private_prompt_stdin_preserves_accounts_and_rolls_back(self):
        from skald import setup, members
        from skald.__main__ import parser
        self.s.CONFIG['connectors'].pop('pmdb')
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        for person, flags in ((self.person, []), (self.s.PEOPLE[0], ['--yes', '--key-stdin'])):
            args = parser().parse_args(['setup', '--connect', 'pmdb', '--person', person] + flags)
            lines = []
            with patch('sys.stdin', io.StringIO(KEY+'\n')), patch('skald.setup.getpass.getpass', return_value=KEY):
                result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, lines, lambda: None)
            self.assertTrue(result['ok'])
            self.assertNotIn(KEY, json.dumps(result) + '\n'.join(lines))
            ref = self.s.CONFIG['connectors']['pmdb']['accounts'][person]
            key = self.data/'credentials'/ref[5:]
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(key.read_text(), KEY)
        self.assertEqual(len(self.s.CONFIG['connectors']['pmdb']['accounts']), 2)
        before = (self.data/'skald.json').read_bytes()
        files = set((self.data/'credentials').iterdir())
        with patch('sys.stdin', io.StringIO(KEY+'\n')), patch.object(self.s, '_http_get', side_effect=TimeoutError()):
            result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, [], lambda: None)
        self.assertFalse(result['ok'])
        self.assertEqual((self.data/'skald.json').read_bytes(), before)
        self.assertEqual(set((self.data/'credentials').iterdir()), files)
        self.assertFalse(self.path.exists())
        args = parser().parse_args(['setup', '--connect', 'pmdb', '--person', self.person])
        self.assertNotIn(KEY, repr(args))
        self.assertFalse(hasattr(args, 'key'))

    def test_backup_engine_and_forgetting(self):
        from skald import backup, history
        from skald.engine import Engine
        self.sync()
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        with Engine(self.data) as engine:
            self.assertEqual(len(engine._server.read_trakt()), 7)
            self.assertFalse((engine._work/'credentials').exists())
        self.s.backup_state(NOW)
        archive = self.data/'backups'/('state-' + self.s.online_day(NOW)[0] + '.tar.gz')
        report = backup.verify(archive)
        self.assertTrue(report['ok'])
        self.assertIn(self.path.name, report['files'])
        self.assertFalse(any('token' in n or 'credentials' in n for n in report['files']))
        history.forget(self.data, self.s.CONFIG, '2026-10-11', self.person, apply=True)
        self.assertEqual(json.loads(self.path.read_text())['items'], [])
        self.sync()
        self.assertEqual(json.loads(self.path.read_text())['items'], [])

    def test_doctor_reports_account_error_classes(self):
        from skald import guide
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        parent = self.s.connector_seeds(NOW)['pmdb']
        for error, text in (('PMDBKeyRejected', 'key rejected'), ('PMDBRateLimited', 'rate limit'),
                            ('PMDBTimeout', 'timeout'), ('SchemaMismatch', 'malformed')):
            parent['accounts'][self.person]['health'] = {'state': 'error', 'error': error, 'at': NOW}
            parent['accounts'][self.person]['last_error'] = error
            self.s._connectors = {'pmdb': parent}
            self.s._connectors_path = str(self.data)
            self.s._connectors_save(self.s._connectors, self.s._connectors_generation)
            with tempfile.TemporaryDirectory() as state:
                result = guide.doctor(SimpleNamespace(snapshot=str(self.data), live=False, remote=False, fix=False), Path(state))
            self.assertIn(text, json.dumps(result))
            self.assertNotIn(KEY, json.dumps(result))

    def test_episode_and_watch_date_changes_refresh_hashes(self):
        self.routes['watched'] = [row(1, 3, 'tv', season=1, episode=1, watched_at=STAMP)]
        self.sync()
        before = (self.s.taste_hash('household', []), self.s.episodes_hash([]))
        for changes in ({'watched_at': '2026-10-10T13:00:00Z'}, {'episode': 2}):
            self.routes['watched'][0].update(changes)
            self.sync()
            after = (self.s.taste_hash('household', []), self.s.episodes_hash([]))
            self.assertNotEqual(before[0], after[0])
            self.assertNotEqual(before[1], after[1])
            before = after

    def test_summary_preserves_trust_and_forget_disconnected_file(self):
        from skald import history
        self.sync()
        self.s.CONFIG['connectors']['pmdb']['keep_raw'] = False
        history.cleanup(self.s, NOW, apply=True)
        summaries = history.read(self.data)
        pmdb = [e for e in summaries if e['origin'] == self.path.name]
        self.assertTrue(pmdb)
        self.assertEqual({e['src'] for e in pmdb}, {'sync:pmdb'})
        self.assertEqual({i['src'] for i in self.s.read_trakt()}, {'sync:pmdb'})
        self.s.CONFIG['connectors'].pop('pmdb')
        history.forget(self.data, self.s.CONFIG, '2026-10-11', self.person, apply=True)
        self.assertEqual(json.loads(self.path.read_text())['items'], [])
        self.assertFalse([e for e in history.read(self.data) if e['origin'] == self.path.name])

    def test_paged_failure_keeps_cache_and_transport_caps(self):
        self.sync()
        before = self.path.read_bytes()
        self.routes['watched'] = [row(n, watched_at=STAMP) for n in range(1, 502)]
        def transport(req, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(req.get_method(), 'GET')
            if 'page=2' in req.full_url:
                raise TimeoutError(KEY)
            return io.BytesIO(json.dumps(self.fake(req.full_url, {'Authorization': req.get_header('Authorization')})).encode())
        self.http.stop()
        with patch.object(self.s, '_connector_open', side_effect=transport):
            with self.assertRaises(self.s.PMDBTimeout):
                self.sync()
            with self.assertRaises(self.s.PageCapExceeded):
                self.s._pmdb_pages('/watched', KEY, {'pages': self.s.CONNECTOR_HTTP_PAGES, 'bytes': 0})
            with self.assertRaises(self.s.ByteCapExceeded):
                self.s._pmdb_pages('/watched', KEY, {'pages': 0, 'bytes': self.s.CONNECTOR_HTTP_BYTES})
        self.assertEqual(self.path.read_bytes(), before)

    def test_probe_checks_next_account_after_rejection(self):
        from skald import connectors
        other = self.s.PEOPLE[0]
        self.s.CONFIG['connectors']['pmdb']['accounts'][other] = 'file:synthetic.token'
        adapter = self.s._connector_pmdb
        def probe(c, now, **kw):
            if c['person'] == self.person:
                raise self.s.PMDBKeyRejected()
            return adapter(c, now, **kw)
        with patch.object(self.s, '_connector_pmdb', side_effect=probe) as call:
            result = connectors.test(self.s, 'pmdb', now=NOW)
        self.assertEqual(call.call_count, 2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['connectors'][0]['plays_30d'], 1)
        self.assertTrue(result['connectors'][0]['accounts'][1]['ok'])

    def test_known_rewatches_match_trakt_aggregation(self):
        self.routes['watched'] = [row(1, watched_at=STAMP),
            row(2, 1, watched_at='2026-10-09T12:00:00Z')]
        self.sync()
        it = self.items()['movie', 'tmdb:1']
        self.assertEqual((it['plays'], it['rewatch'], it['signal']), (2, 1, 'loved'))
        self.assertEqual(it['at'], self.s._connector_time(STAMP)*1000)
        self.assertEqual(it['first_at'], self.s._connector_time('2026-10-09T12:00:00Z')*1000)

    def test_disconnect_preserves_other_account_and_history(self):
        from skald import setup
        from skald.__main__ import parser
        other = self.s.PEOPLE[0]
        self.s.CONFIG['connectors']['pmdb']['accounts'][other] = 'file:other.token'
        token = self.data/'credentials'/'other.token'
        token.write_text(KEY)
        token.chmod(0o600)
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        self.sync()
        args = parser().parse_args(['setup', '--disconnect', 'pmdb', '--person', self.person])
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}):
            result = setup.run(args)
        self.assertTrue(result['ok'])
        self.assertFalse((self.data/'credentials'/'synthetic.token').exists())
        self.assertTrue(token.exists())
        self.assertTrue(self.path.exists())
        self.assertEqual(set(json.loads((self.data/'skald.json').read_text())['connectors']['pmdb']['accounts']), {other})

    def test_pmdb_full_watch_beats_partial_trakt_completion(self):
        self.routes['watched'] = [row(1, watched_at=STAMP)]
        self.sync()
        self.s.CONFIG['connectors']['trakt_api'] = {'accounts': {self.person: 'file:synthetic.token'}}
        partial = {'id': 'tmdb:1', 'type': 'movie', 'played': True, 'progress': .2,
                   'signal': 'interest', 'weight': 1}
        self.s._connector_write('trakt-' + self.person + '.history', {'items': [partial]})
        self.assertEqual(self.items()['movie', 'tmdb:1']['src'], 'sync:pmdb')
        self.assertEqual(self.items()['movie', 'tmdb:1']['signal'], 'watched')

    def test_forgotten_resync_emits_no_events(self):
        from skald import history
        self.sync()
        history.forget(self.data, self.s.CONFIG, '2026-10-11', self.person, apply=True)
        events, _, stats = self.sync()
        self.assertEqual(events, [])
        self.assertEqual(stats['events'], 0)
        before = self.path.read_bytes()
        self.assertEqual(self.sync(NOW + 60)[0], [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_show_completion_beats_single_episode_progress(self):
        self.routes['resume'] = []
        self.routes['watched'] = [row(n, 1, 'tv', season=1, episode=n,
            watched_at=f'2026-10-10T{n:02d}:00:00Z') for n in range(1, 10)]
        self.sync()
        odin = {'id': 'tmdb:1', 'type': 'series', 'profile': self.person, 'played': True,
                'progress': 1, 'episodes_played': 1, 'last_episode': [1, 1], 'at': NOW * 1000}
        with patch.object(self.s, 'attribute', return_value=(self.person, [self.person], {})), \
                patch('time.time', return_value=NOW):
            profile = self.s.build_profile([odin], [], self.s.read_trakt(), remote=False)
        own = next(e for e in self.s.person_entries(profile, self.person) if e['key'] == 'tmdb:1')
        self.assertEqual(own['item']['episodes_played'], 9)
        self.assertEqual(own['item']['completion'], .9)
        self.assertEqual(own['item']['last_episode'], [1, 9])
        self.s._profiles['household'] = profile
        show = {'seasons': [{'season_number': 1, 'episode_count': 10}],
                'last_episode_to_air': {'season_number': 1, 'episode_number': 10}}
        with patch.object(self.s, 'tmdb', return_value=show), patch.object(self.s, 'genres', return_value={}), \
                patch.object(self.s, 'row_meta', side_effect=lambda *args, **kw: {'note': kw['note']}):
            self.assertEqual(self.s.episodes_feed('en-US')['series'], [{'note': 'Up next: S1E10'}])

    def test_unplayed_drop_beats_trakt_watchlist_interest(self):
        self.sync()
        self.s.CONFIG['connectors']['trakt_api'] = {'accounts': {self.person: 'file:synthetic.token'}}
        interest = {'id': 'tmdb:7', 'type': 'movie', 'played': False, 'signal': 'interest', 'weight': 1}
        self.s._connector_write('trakt-' + self.person + '.history', {'items': [interest]})
        it = self.items()['movie', 'tmdb:7']
        self.assertEqual(it['signal'], 'abandoned')
        self.assertFalse(it['played'])
        self.assertEqual(it['src'], 'sync:pmdb')
        self.assertLess(it['weight'], 0)
        for fields in ({'played': True, 'signal': 'watched', 'weight': 2},
                       {'rating': 8, 'signal': 'loved', 'weight': 3}):
            self.s._connector_write('trakt-' + self.person + '.history', {'items': [{**interest, **fields}]})
            self.assertEqual(self.items()['movie', 'tmdb:7']['signal'], fields['signal'])

    def test_resume_timestamp_keeps_show_in_next_episode_row(self):
        self.routes['watched'] = []
        self.routes['resume'] = [row(1, 1, 'tv', season=1, episode=2, progress=20, updated=STAMP,
                                     created='2026-10-09T12:00:00Z')]
        self.sync()
        it = self.items()['series', 'tmdb:1']
        self.assertEqual(it['at'], self.s._connector_time(STAMP) * 1000)
        self.assertIsNone(it['first_at'])
        self.assertFalse(it['played'])
        self.assertEqual(it['plays'], 0)
        self.assertEqual(it['signal'], 'interest')
        with patch('time.time', return_value=NOW):
            self.s._profiles['household'] = self.s.build_profile([], [], self.s.read_trakt(), remote=False)
        with patch.object(self.s, 'tmdb', return_value={}), patch.object(self.s, 'genres', return_value={}), \
                patch.object(self.s, 'row_meta', side_effect=lambda *args, **kw: {'note': kw['note']}):
            self.assertEqual(self.s.episodes_feed('en-US')['series'], [{'note': 'Continue S1E2'}])
        # Resume dates must not replace an unknown watched date on the same show.
        self.routes['watched'] = [row(2, 1, 'tv', season=1, episode=1, watched_at=None)]
        self.sync()
        self.assertIsNone(self.items()['series', 'tmdb:1']['at'])
        self.routes['watched'] = []
        self.routes['resume'][0]['updated'] = None
        self.sync()
        self.assertEqual(self.items()['series', 'tmdb:1']['at'], self.s._connector_time('2026-10-09T12:00:00Z') * 1000)

    def test_live_snapshot_collects_and_copies_pmdb_history(self):
        import tarfile
        import shlex
        from skald import live
        from skald.engine import Engine
        self.sync()
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        name = self.path.name
        with self.subTest('collection'), patch.dict('os.environ', {'SKALD_SSH_HOST': 'synthetic-host.test'}):
            self.assertIn("glob.glob('pmdb-*.history.json')", shlex.split(live.command('snapshot')[-1])[6])
        files = {'skald.json': (self.data/'skald.json').read_bytes(),
                 'legacy.json': (self.data/'legacy.json').read_bytes(), name: self.path.read_bytes(),
                 'emb.json': b'{}', 'facets.json': b'{}'}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            for filename, raw in files.items():
                member = tarfile.TarInfo(filename)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        with self.subTest('copy'), patch.object(live, 'command', return_value=['synthetic-command']):
            destination = live.pull(self.data/'copied', run=lambda *args, **kw: SimpleNamespace(stdout=buffer.getvalue()))
            self.assertEqual((destination/name).read_bytes(), self.path.read_bytes())
            with Engine(destination) as engine:
                self.assertEqual(len(engine._server.read_trakt()), 7)
