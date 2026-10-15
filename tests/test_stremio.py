"""Stremio synthetic accounts and fake HTTP only."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
import traceback
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch
from decide_kit import write_legacy, load_server, close_server

NOW = 1791734400.
STAMP = '2026-10-10T12:00:00Z'
KEY = 'SENTINEL_STREMIO_KEY'
PASSWORD = 'SENTINEL_STREMIO_PASSWORD'
ADDON_KEY = 'SENTINEL_ADDON_KEY/+ &?'


def item(n, **kw):
    return {'_id': 'tt' + str(n), 'type': 'movie', 'name': 'Synthetic', '_mtime': STAMP, **kw}


class StremioTests(unittest.TestCase):
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
        self.s.CONFIG['connectors']['stremio'] = {'kind': 'api', 'enabled': True, 'auth': None,
            'accounts': {self.person: 'file:synthetic.token'}, 'options': {}}
        self.s.os.close(self.s._credentials_dir(create=True))
        key = self.data/'credentials'/'synthetic.token'
        key.write_text(KEY)
        key.chmod(0o600)
        self.c = self.s._connector('stremio:' + self.person, 'api', self.person, NOW, auth='file:synthetic.token')
        self.rows = [item(1, state={'timesWatched': 1, 'lastWatched': STAMP}),
            item(2, temp=True, state={'flaggedWatched': 1, 'lastWatched': STAMP}),
            item(3, type='series', state={'duration': 1000, 'timeWatched': 700, 'lastWatched': STAMP, 'video_id': 'tt3:1:2'}),
            item(4), item(5, removed=True, temp=True, state={'timesWatched': 1, 'lastWatched': STAMP}), item(6, temp=True), item(13, removed=True), item(7, _id='other'), item(8, type='channel'),
            item(9, temp=True, state={'duration': 1000, 'timeWatched': 699}),
            item(10, state={'duration': 1000, 'timeWatched': 699})]

    def fake(self, url, headers=None, **kw):
        if url.endswith('/login'):
            self.assertEqual(kw['body'], {'email': 'synthetic@example.test', 'password': PASSWORD})
            return {'result': {'authKey': KEY}}
        if url.endswith('/datastoreGet'):
            self.assertEqual(kw['body'], {'authKey': KEY, 'collection': 'libraryItem', 'ids': [], 'all': True})
            return {'result': self.rows}
        if '/getAddonKey?' in url:
            return {'key': ADDON_KEY}
        self.assertEqual(headers, {'User-Agent': 'Skald/' + self.s.VERSION})
        self.assertIn('/user=' + urllib.parse.quote(ADDON_KEY, safe='') + '/', url)
        return {'metas': [{'id': 'tt11', 'type': 'movie', 'name': 'Outside'}] if '/loved/' in url and '/movie/' in url else
                         [{'id': 'tt4', 'type': 'movie', 'name': 'Liked'}] if '/liked/' in url and '/movie/' in url else []}

    def resolve(self, it):
        self.assertEqual(it['type'], 'series' if it['id'] == 'tt3' else 'movie')
        return ('movie' if it['type'] == 'movie' else 'tv', {'id': int(it['id'][2:])})

    def test_addon_key_forms_url_sequence_and_library_body(self):
        auth = KEY + '/+ &?'
        for raw in (json.dumps({'key': ADDON_KEY}).encode(), json.dumps(ADDON_KEY).encode(), ADDON_KEY.encode()):
            requests = []
            def transport(req, timeout):
                requests.append(req)
                body = {'result': self.rows} if req.full_url.endswith('/datastoreGet') else {'metas': []}
                return io.BytesIO(raw if '/getAddonKey?' in req.full_url else json.dumps(body).encode())
            with self.subTest(raw=raw), patch.object(self.s, '_resolve_auth', return_value=auth), \
                    patch.object(self.s, '_connector_open', side_effect=transport), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                doc, _, stats = self.s._connector_stremio(self.c, NOW, readonly=True)
            urls = [req.full_url for req in requests]
            expected = ['https://api.strem.io/api/datastoreGet',
                        'https://likes.stremio.com/getAddonKey?key='+urllib.parse.quote(auth, safe='')]
            expected += [f'https://likes.stremio.com/addons/{level}/movies-shows/user={urllib.parse.quote(ADDON_KEY, safe="")}/catalog/{kind}/stremio-{level}-{kind}.json'
                         for level in ('liked', 'loved') for kind in ('movie', 'series')]
            self.assertEqual(urls, expected)
            self.assertEqual(json.loads(requests[0].data), {'authKey': auth, 'collection': 'libraryItem', 'ids': [], 'all': True})
            self.assertTrue(stats['likes_available'])
            self.assertTrue(doc['items'])
            self.assertNotIn(KEY, json.dumps(stats))
            self.assertNotIn(ADDON_KEY, json.dumps(stats))

    def test_likes_failure_keeps_library_cached_likes_and_probe_stats(self):
        from skald import connectors
        failures = [('getAddonKey', 500), ('getAddonKey', 401), ('getAddonKey', {'key': 7}),
                    ('/liked/', 500), ('/loved/', {'metas': 'bad shape'}), ('/loved/', TimeoutError(KEY+ADDON_KEY))]
        for endpoint, failure in failures:
            with self.subTest(endpoint=endpoint, failure=failure), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                cached = {f'movie:tt{n}': {**self.s._stremio_item(item(n), NOW), 'rating': 10, 'signal': 'loved', 'weight': 5} for n in (1, 11)}
                self.s._connector_write('stremio-'+self.person+'.history', {'library': {}, 'likes': cached, 'items': list(cached.values())})
                def http(url, headers=None, **kw):
                    if endpoint in url:
                        if isinstance(failure, int):
                            raise urllib.error.HTTPError(url, failure, KEY+ADDON_KEY, {}, None)
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    if url.endswith('/datastoreGet'):
                        return {'result': self.rows}
                    if '/getAddonKey?' in url:
                        return json.dumps({'key': ADDON_KEY})
                    return {'metas': []}
                output = io.StringIO()
                with patch.object(self.s, '_http_get', side_effect=http), redirect_stdout(output), redirect_stderr(output):
                    _, _, stats = self.s._connector_stremio(self.c, NOW)
                    probe = connectors.test(self.s, 'stremio', now=NOW)
                doc = json.loads((self.data/('stremio-'+self.person+'.history.json')).read_text())
                items = {it['id']: it for it in doc['items']}
                self.assertTrue(items['tmdb:1']['played'])
                self.assertEqual(items['tmdb:1']['rating'], 10)
                self.assertEqual(items['tmdb:11']['rating'], 10)
                self.assertIn('tmdb:2', items)
                self.assertFalse(stats['likes_available'])
                self.assertTrue(probe['ok'])
                self.assertFalse(probe['connectors'][0]['likes_available'])
                self.assertIn('likes unavailable', ' '.join(connectors.text(probe)))
                text = json.dumps(stats)+json.dumps(probe)+output.getvalue()
                self.assertNotIn(KEY, text)
                self.assertNotIn('SENTINEL_ADDON_KEY', text)
                self.assertNotIn('https://', text)

    def test_addon_key_and_catalog_rate_limits_still_stop_without_keys(self):
        for endpoint in ('getAddonKey', '/liked/'):
            def http(url, headers=None, **kw):
                if endpoint in url:
                    raise urllib.error.HTTPError(url, 429, KEY+ADDON_KEY, {}, None)
                if url.endswith('/datastoreGet'):
                    return {'result': self.rows}
                if '/getAddonKey?' in url:
                    return json.dumps(ADDON_KEY)
                return {'metas': []}
            with self.subTest(endpoint=endpoint), patch.object(self.s, '_http_get', side_effect=http), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                with self.assertRaises(self.s.StremioRateLimited) as caught:
                    self.s._connector_stremio(self.c, NOW)
                self.assertNotIn(KEY, ''.join(traceback.format_exception(caught.exception)))
                self.assertNotIn('SENTINEL_ADDON_KEY', ''.join(traceback.format_exception(caught.exception)))

    def test_text_transport_still_counts_and_caps_bytes(self):
        budget = {'bytes': 0, 'pages': 0}
        with patch.object(self.s, '_connector_open', return_value=io.BytesIO(b'bare-key')):
            self.assertEqual(self.s._http_get('https://likes.stremio.com/getAddonKey', text=True, budget=budget), 'bare-key')
        self.assertEqual(budget, {'bytes': 8, 'pages': 1})
        with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 3), patch.object(self.s, '_connector_open', return_value=io.BytesIO(b'bare-key')):
            with self.assertRaises(self.s.ByteCapExceeded):
                self.s._http_get('https://likes.stremio.com/getAddonKey', text=True)

    def test_invalid_addon_key_shapes_leave_library_available(self):
        for raw in (b'null', b'[]', b'123', b'false', b'""', b'{"key": ""}', b'{"other": "value"}'):
            requests = []
            def transport(req, timeout):
                requests.append(req.full_url)
                return io.BytesIO(raw if '/getAddonKey?' in req.full_url else json.dumps({'result': self.rows}).encode())
            with self.subTest(raw=raw), patch.object(self.s, '_connector_open', side_effect=transport), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                doc, _, stats = self.s._connector_stremio(self.c, NOW, readonly=True)
                self.assertFalse(stats['likes_available'])
                self.assertTrue(doc['items'])
                self.assertEqual(len(requests), 2)

    def test_mapping_likes_and_unchanged_resync(self):
        with patch.object(self.s, '_http_get', side_effect=self.fake) as http, patch.object(self.s, 'tmdb_details', side_effect=self.resolve) as resolve:
            events, cursor, stats = self.s._connector_stremio(self.c, NOW)
            self.assertEqual(http.call_count, 6)
            items = {i['id']: i for i in self.s.read_trakt()}
            self.assertEqual(set(items), {'tmdb:1', 'tmdb:2', 'tmdb:3', 'tmdb:4', 'tmdb:5', 'tmdb:9', 'tmdb:10', 'tmdb:11'})
            for n in (1, 2, 3, 5):
                self.assertEqual(items['tmdb:' + str(n)]['signal'], 'watched')
                self.assertTrue(items['tmdb:' + str(n)]['played'])
            self.assertEqual(items['tmdb:3']['last_episode'], [1, 2])
            self.assertEqual(items['tmdb:1']['at'], self.s._connector_time(STAMP) * 1000)
            self.assertEqual(items['tmdb:4']['signal'], 'loved')
            self.assertEqual(items['tmdb:11']['signal'], 'loved')
            self.assertEqual(items['tmdb:4']['rating'], 8)
            self.assertEqual(items['tmdb:11']['rating'], 10)
            library_only = self.s._stremio_item(item(12), NOW)
            self.assertEqual(library_only['signal'], 'interest')
            self.assertFalse(library_only['played'])
            self.assertEqual(items['tmdb:10']['signal'], 'interest')
            self.assertEqual((items['tmdb:9']['signal'], items['tmdb:9']['progress']), ('interest', 0.699))
            self.assertFalse(items['tmdb:10']['played'])
            self.assertEqual({e['who'] for e in events}, {self.person})
            self.assertIn('interest', {e['action'] for e in events})
            http.reset_mock()
            resolve.reset_mock()
            self.assertEqual(self.s._connector_stremio(self.c, NOW + 60)[0], [])
            self.assertEqual(http.call_count, 6)
            resolve.assert_not_called()
            self.rows[0]['_mtime'] = '2026-10-11T12:00:00Z'
            self.assertEqual(self.s._connector_stremio(self.c, NOW + 120)[0], [])

    def test_cached_rows_from_older_rules_are_mapped_again(self):
        watched = item(5, removed=True, temp=True, state={'timesWatched': 1, 'lastWatched': STAMP})
        self.rows = [watched]
        self.s._connector_write('stremio-' + self.person + '.history', {'version': 1, 'library': {'tt5': {'state': watched, 'item': None}}, 'likes': {}, 'items': []})
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        self.assertEqual({i['id']: i['signal'] for i in self.s.read_trakt() if i['id'] == 'tmdb:5'}, {'tmdb:5': 'watched'})

    def test_started_films_follow_the_shared_watch_rule(self):
        def started(n, offset, days, **kw):
            stamp = self.s.datetime.datetime.fromtimestamp(NOW - days * 86400, self.s.datetime.timezone.utc).isoformat()
            return item(n, removed=True, temp=True, state={'duration': 1000, 'timeOffset': offset, 'lastWatched': stamp}, **kw)
        self.rows = [started(21, 100, 5), started(22, 100, 1), started(23, 500, 5), started(24, 10, 5),
                     started(25, 100, 5, type='series')]
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        items = {i['id']: i for i in self.s.read_trakt()}
        self.assertEqual({k: v['signal'] for k, v in items.items() if k != 'tmdb:4' and k != 'tmdb:11'},
                         {'tmdb:21': 'abandoned', 'tmdb:22': 'interest', 'tmdb:23': 'interest'})
        self.assertEqual(items['tmdb:21']['weight'], self.s.SIGNAL_WEIGHT['abandoned'])
        self.assertFalse(items['tmdb:21']['played'])
        self.rows = [item(21, removed=True, temp=True, state={'timesWatched': 1, 'lastWatched': STAMP})]
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW + 60)
        self.assertEqual(next(i for i in self.s.read_trakt() if i['id'] == 'tmdb:21')['signal'], 'watched')

    def test_tmdb_ids_map_without_lookup_and_merge_with_imdb_rows(self):
        self.rows = [item(31, _id='tmdb:31', state={'timesWatched': 1, 'lastWatched': STAMP}),
                     item(32, _id='tmdb:3', type='series'), item(3, type='series', state={'timesWatched': 1, 'lastWatched': STAMP})]
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve) as resolve:
            self.s._connector_stremio(self.c, NOW)
        items = [i for i in self.s.read_trakt() if i['id'] in ('tmdb:31', 'tmdb:3')]
        self.assertEqual(sorted((i['id'], i['signal']) for i in items), [('tmdb:3', 'watched'), ('tmdb:31', 'watched')])
        self.assertNotIn('tmdb:31', [c.args[0]['id'] for c in resolve.call_args_list])

    def test_rewatched_films_count_as_loved(self):
        self.rows = [item(41, state={'timesWatched': 21, 'lastWatched': STAMP}),
                     item(42, state={'timesWatched': 1, 'lastWatched': STAMP})]
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        items = {i['id']: i for i in self.s.read_trakt()}
        self.assertEqual((items['tmdb:41']['signal'], items['tmdb:41']['plays']), ('loved', 21))
        self.assertIn('comfort', items['tmdb:41']['flags'])
        self.assertEqual(items['tmdb:42']['signal'], 'watched')

    def test_trakt_rating_wins(self):
        rated = {'id': 'tmdb:4', 'type': 'movie', 'rating': 2, 'signal': 'disliked', 'weight': -3}
        self.s.CONFIG['connectors']['trakt_api'] = {'accounts': {self.person: 'file:synthetic.token'}}
        self.s._connector_write('trakt-' + self.person + '.history', {'items': [rated]})
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        self.assertEqual(next(i for i in self.s.read_trakt() if i['id'] == 'tmdb:4'), {**rated, 'person': self.person})

    def test_stremio_rating_merges_into_unrated_watch(self):
        for source in ('trakt', 'mdblist'):
            for tid, rating in ((4, 8), (11, 10)):
                with self.subTest(source=source, rating=rating):
                    self.s.CONFIG['connectors'].pop('trakt_api', None)
                    self.s.CONFIG['connectors'].pop('mdblist', None)
                    cid = 'trakt_api' if source == 'trakt' else source
                    self.s.CONFIG['connectors'][cid] = {'accounts': {self.person: 'file:synthetic.token'}}
                    watched = {'id': 'tmdb:' + str(tid), 'type': 'movie', 'played': True,
                               'plays': 2, 'at': NOW * 1000, 'signal': 'watched', 'weight': 2}
                    self.s._connector_write(source + '-' + self.person + '.history', {'items': [watched]})
                    with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                        self.s._connector_stremio(self.c, NOW)
                    merged = next(i for i in self.s.read_trakt() if i['id'] == watched['id'])
                    self.assertEqual(merged.get('rating'), rating)
                    self.assertEqual(merged['signal'], 'loved')
                    self.assertGreater(merged['weight'], watched['weight'])
                    for field in ('played', 'plays', 'at'):
                        self.assertEqual(merged[field], watched[field])

    def test_stremio_episode_sync_refreshes_profile_and_episode_hash(self):
        self.rows = [item(3, type='series', state={'timesWatched': 1, 'lastWatched': STAMP, 'video_id': 'tt3:1:1'})]
        show = {'seasons': [{'season_number': 1, 'episode_count': 3}],
                'last_episode_to_air': {'season_number': 1, 'episode_number': 3}}
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve), \
                patch('time.time', return_value=NOW), patch.object(self.s, 'learn_from_plays'), \
                patch.object(self.s, 'log_play_context'), patch.object(self.s, 'tmdb', return_value=show), \
                patch.object(self.s, 'genres', return_value={}), \
                patch.object(self.s, 'row_meta', side_effect=lambda *args, **kw: {'note': kw['note']}), redirect_stdout(io.StringIO()):
            self.s._connector_stremio(self.c, NOW)
            before = self.s.refresh_profiles()
            entry = lambda: next(e for e in self.s._profiles['household']['entries'] if e['key'] == 'tmdb:3')
            self.assertEqual(entry()['item']['last_episode'], [1, 1])
            self.assertEqual(self.s.episodes_feed('en-US')['series'], [{'note': 'Up next: S1E2'}])
            original = self.s.read_trakt()[0]
            for state in ({'lastWatched': '2026-10-10T13:00:00Z'}, {'video_id': 'tt3:1:2'}):
                self.rows[0]['state'].update(state)
                self.assertTrue(self.s._connector_stremio(self.c, NOW + 60)[0])
                latest = self.s.read_trakt()[0]
                self.assertEqual((latest['signal'], latest['weight']), (original['signal'], original['weight']))
                after = self.s.refresh_profiles()
                for name in ('household', 'episodes'):
                    self.assertNotEqual(after[name], before[name], name)
                self.assertEqual(entry()['item']['last_episode'], latest['last_episode'])
                self.assertEqual(entry()['item']['at'], latest['at'])
                before = after
            self.assertEqual(entry()['item']['last_episode'], [1, 2])
            self.assertEqual(self.s.episodes_feed('en-US')['series'], [{'note': 'Up next: S1E3'}])

    def test_errors_redact_secrets_and_stop(self):
        for code, message in ((401, 'key rejected'), (403, 'key rejected'), (429, 'rate limit'), (500, 'request failed')):
            output = io.StringIO()
            with self.subTest(code=code), redirect_stdout(output), redirect_stderr(output), patch.object(self.s, '_http_get', side_effect=urllib.error.HTTPError('https://likes.stremio.com/' + KEY, code, PASSWORD, {}, None)) as http:
                with self.assertRaises(ValueError) as caught:
                    self.s._connector_stremio(self.c, NOW)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(http.call_count, 1)
                text = output.getvalue() + ''.join(traceback.format_exception(caught.exception))
                self.assertNotIn(KEY, text)
                self.assertNotIn(PASSWORD, text)
        with patch.object(self.s, '_http_get', return_value={'error': {'message': 'Session expired ' + KEY}}):
            with self.assertRaisesRegex(ValueError, 'key rejected'):
                self.s._connector_stremio(self.c, NOW)

    def test_connect_status_disconnect_and_login(self):
        from skald import setup, members
        from skald.__main__ import parser, main
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        output = io.StringIO()
        prompts = iter(([KEY], ['', 'synthetic@example.test', PASSWORD]))
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}), redirect_stdout(output), redirect_stderr(output), patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            for flags in (['--yes', '--key-stdin'], ['--secret-stdin'], [], []):
                args = parser().parse_args(['setup', '--connect', 'stremio', '--person', self.person] + flags)
                with patch('sys.stdin', io.StringIO(KEY + '\n')), patch('skald.setup.getpass.getpass', side_effect=next(prompts) if not flags else []):
                    if not flags:
                        self.s.CONFIG['connectors'].pop('stremio', None)
                    result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, [], lambda: None)
                self.assertTrue(result['ok'])
                self.assertFalse(list(self.data.glob('stremio-*.history.json')))
                saved = json.loads((self.data/'skald.json').read_text())
                path = self.data/'credentials'/saved['connectors']['stremio']['accounts'][self.person][5:]
                self.assertEqual(path.read_text(), KEY)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(PASSWORD, ''.join(p.read_text() for p in (self.data/'credentials').iterdir()))
            status = setup.status(self.data)
            self.assertTrue(status['connections']['stremio'][self.person])
            main(['setup', '--status'], out=output)
            self.s.connectors_load(NOW)
            view = self.s.connectors_view()
            args = parser().parse_args(['setup', '--disconnect', 'stremio', '--person', self.person])
            setup.run(args)
            self.assertFalse(path.exists())
            self.assertFalse(setup.status(self.data)['connections']['stremio'][self.person])
        text = output.getvalue() + json.dumps(saved) + json.dumps(status) + json.dumps(view)
        self.assertNotIn(KEY, text)
        self.assertNotIn(PASSWORD, text)

    def test_likes_errors_and_account_rate_limit(self):
        from skald import connectors
        for code, message in ((429, 'rate limit'),):
            def fail(url, headers=None, **kw):
                if 'likes.stremio.com' in url:
                    raise urllib.error.HTTPError(url, code, KEY + PASSWORD, {}, None)
                return self.fake(url, headers, **kw)
            with self.subTest(code=code), patch.object(self.s, '_http_get', side_effect=fail) as http, patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
                with self.assertRaisesRegex(ValueError, message) as caught:
                    self.s._connector_stremio(self.c, NOW)
                self.assertNotIn(KEY, ''.join(traceback.format_exception(caught.exception)))
                self.assertNotIn(PASSWORD, str(caught.exception))
                self.assertFalse(list(self.data.glob('stremio-*.history.json')))
                probe = connectors.test(self.s, 'stremio', now=NOW)
                self.assertFalse(probe['ok'])
                self.assertIn(message, probe['connectors'][0]['error'])
                self.assertNotIn(KEY, json.dumps(probe))
                self.s.CONFIG['connectors']['stremio']['accounts'][self.s.PEOPLE[0]] = 'file:synthetic.token'
                parent = self.s.connector_seeds(NOW)['stremio']
                http.reset_mock()
                events, cursor, stats = self.s.poll(parent, NOW)
                self.assertEqual(events, [])
                self.assertNotIn(KEY, json.dumps(stats))
                if code == 429:
                    self.assertEqual(http.call_count, 2)
                    self.assertGreater(stats['backoff_until'], NOW)
                self.s.CONFIG['connectors']['stremio']['accounts'].pop(self.s.PEOPLE[0])
        with patch.object(self.s, '_http_get', side_effect=TimeoutError(KEY + PASSWORD)):
            with self.assertRaisesRegex(ValueError, 'request failed') as caught:
                self.s._stremio_login('synthetic@example.test', PASSWORD)
            self.assertNotIn(PASSWORD, ''.join(traceback.format_exception(caught.exception)))

    def test_offline_history_copy_and_backup(self):
        from skald import members, backup
        from skald.engine import Engine, data_files
        name = 'stremio-' + self.person + '.history.json'
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        self.assertIn(name, data_files(self.s.CONFIG))
        with Engine(self.data) as engine:
            self.assertEqual(len(engine._server.read_trakt()), 8)
            self.assertFalse((engine._work/'credentials').exists())
        self.s.backup_state(NOW)
        archive = self.data/'backups'/('state-' + self.s.online_day(NOW)[0] + '.tar.gz')
        report = backup.verify(archive)
        self.assertTrue(report['ok'])
        self.assertIn(name, report['files'])
        self.assertFalse(any('token' in name or 'credentials' in name for name in report['files']))

    def test_rate_limit_keeps_last_good_history(self):
        with patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.s._connector_stremio(self.c, NOW)
        path = self.data/('stremio-' + self.person + '.history.json')
        before = path.read_bytes()
        def fail(url, headers=None, **kw):
            if '/loved/' in url:
                raise urllib.error.HTTPError(url, 429, KEY, {}, None)
            return self.fake(url, headers, **kw)
        self.rows[0]['state']['timesWatched'] = 2
        with patch.object(self.s, '_http_get', side_effect=fail), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            with self.assertRaisesRegex(ValueError, 'rate limit'):
                self.s._connector_stremio(self.c, NOW + 60)
        self.assertEqual(path.read_bytes(), before)

    def test_transport_timeout_and_requests(self):
        requests = []
        def transport(req, timeout):
            self.assertEqual(timeout, 10)
            requests.append(req)
            self.assertNotIn(KEY, str(req.headers))
            self.assertNotIn(PASSWORD, str(req.headers))
            self.assertEqual(req.get_header('User-agent'), 'Skald/' + self.s.VERSION)
            body = json.loads(req.data) if req.data else None
            self.assertEqual(req.get_method(), 'POST' if body else 'GET')
            result = self.fake(req.full_url, {'User-Agent': req.get_header('User-agent')}, **({'body': body} if body else {}))
            return io.BytesIO(json.dumps(result).encode())
        with patch.object(self.s, '_connector_open', side_effect=transport), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            self.assertEqual(self.s._stremio_login('synthetic@example.test', PASSWORD), KEY)
            self.s._connector_stremio(self.c, NOW)
        self.assertEqual(len(requests), 7)
        self.assertEqual(len([req for req in requests if req.get_method() == 'GET']), 5)

    def test_connect_by_link_without_key_or_password(self):
        from skald import setup, members
        from skald.__main__ import parser
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        self.s.CONFIG['connectors'].pop('stremio', None)
        reads, waits = iter([{'error': {'code': 101, 'message': 'Invalid or expired token'}}, TimeoutError(),
                             {'result': {'authKey': KEY}}]), []
        def fake(url, headers=None, **kw):
            self.assertEqual(headers['User-Agent'], 'Skald/' + self.s.VERSION)
            if url == 'https://link.stremio.com/api/v2/create?type=Create':
                return {'result': {'success': True, 'code': 'AB12', 'link': 'https://link.stremio.com/AB12'}}
            if url == 'https://link.stremio.com/api/v2/read?type=Read&code=AB12':
                value = next(reads)
                if isinstance(value, Exception):
                    raise value
                return value
            return self.fake(url, headers, **kw)
        output = io.StringIO()
        args = parser().parse_args(['setup', '--connect', 'stremio', '--person', self.person])
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}), redirect_stdout(output), \
                patch.object(self.s, '_http_get', side_effect=fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve), \
                patch('skald.setup.getpass.getpass', return_value=''), patch.object(self.s.time, 'sleep', side_effect=waits.append):
            lines = []
            result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, lines, lambda: None)
        self.assertTrue(result['ok'])
        self.assertEqual(waits, [3, 3, 3])
        self.assertIn('https://link.stremio.com/AB12', lines)
        self.assertIn('\x1b[30;107m', output.getvalue())
        saved = json.loads((self.data/'skald.json').read_text())
        path = self.data/'credentials'/saved['connectors']['stremio']['accounts'][self.person][5:]
        self.assertEqual(path.read_text(), KEY)
        self.assertNotIn(KEY, output.getvalue() + '\n'.join(lines))

    def test_link_times_out_and_falls_back_to_email(self):
        with patch.object(self.s, '_http_get', return_value={'error': {'code': 101, 'message': 'Invalid or expired token'}}), \
                patch.object(self.s.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'not confirmed'):
                self.s._stremio_link_key('AB12')
        self.assertEqual(sleep.call_count, 100)
        from skald import setup, members
        from skald.__main__ import parser
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        self.s.CONFIG['connectors'].pop('stremio', None)
        def fake(url, headers=None, **kw):
            if 'link.stremio.com' in url:
                raise urllib.error.HTTPError(url, 503, 'down', {}, None)
            return self.fake(url, headers, **kw)
        lines = []
        args = parser().parse_args(['setup', '--connect', 'stremio', '--person', self.person])
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}), \
                patch.object(self.s, '_http_get', side_effect=fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve), \
                patch('skald.setup.getpass.getpass', side_effect=['', 'synthetic@example.test', PASSWORD]):
            result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, lines, lambda: None)
        self.assertTrue(result['ok'])
        self.assertIn('Stremio link unavailable; sign in with email and password instead', lines)

    def test_connect_preserves_other_person(self):
        from skald import setup, members
        from skald.__main__ import parser
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        other = self.s.PEOPLE[0]
        args = parser().parse_args(['setup', '--connect', 'stremio', '--person', other, '--yes', '--key-stdin'])
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}), patch('sys.stdin', io.StringIO(KEY + '\n')), patch.object(self.s, '_http_get', side_effect=self.fake), patch.object(self.s, 'tmdb_details', side_effect=self.resolve):
            result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, [], lambda: None)
        self.assertTrue(result['ok'])
        accounts = json.loads((self.data/'skald.json').read_text())['connectors']['stremio']['accounts']
        self.assertEqual(set(accounts), set(self.s.PEOPLE))
        self.assertEqual(accounts[self.person], 'file:synthetic.token')
        self.assertNotEqual(accounts[other], accounts[self.person])

    def test_history_allowlists(self):
        import tarfile
        from skald import live, backup
        name = 'stremio-' + self.person + '.history.json'
        self.assertTrue(live._snapshot_file(name))
        backup._member(tarfile.TarInfo(name), set(), 0)
        self.assertFalse(live._snapshot_file('credentials/synthetic.token'))
