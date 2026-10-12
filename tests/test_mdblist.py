"""MDBList uses synthetic accounts and fake HTTP only."""
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from decide_kit import write_legacy, load_server, close_server

NOW = 1791734400.
KEY = 'SENTINEL_MDBLIST_KEY_NEVER_PRINT'
STAMP = '2026-10-10T12:00:00Z'


def title(tid=1):
    return {'title': 'Synthetic', 'year': 2020, 'ids': {'tmdb': tid}}


class MDBListTests(unittest.TestCase):
    def setUp(self):
        from skald import members
        self.addCleanup(members.configure)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.person = self.s.PEOPLE[-1]
        self.s.CONFIG = copy.deepcopy(self.s.CONFIG)
        self.s.CONFIG['connectors']['mdblist'] = {'kind': 'api', 'enabled': True, 'auth': None,
            'accounts': {self.person: 'file:synthetic.token'}, 'options': {}}
        directory = self.s._credentials_dir(create=True)
        self.s.os.close(directory)
        path = self.data/'credentials'/'synthetic.token'
        path.write_text(KEY)
        path.chmod(0o600)
        self.c = self.s._connector('mdblist:' + self.person, 'api', self.person, NOW, auth='file:synthetic.token')

    def fake(self, path, key, **kwargs):
        self.assertEqual(key, KEY)
        if path == '/sync/last_activities':
            return {'watched_at': STAMP, 'watchlisted_at': STAMP}
        offset = kwargs['offset']
        if path == '/watchlist/items':
            return {'movies': [{'ids': {'tmdb': 9}, 'title': 'Later'}, {'title': 'No ids'}],
                    'shows': [], 'pagination': {'has_more': False}}
        return {'movies': [{'movie': title(), 'last_watched_at': STAMP}] if offset == 0 else [],
                'episodes': [{'episode': {'season': 1, 'number': offset//500 + 1, 'show': title(2)},
                              'last_watched_at': STAMP}],
                'shows': [{'title': 'Unverified'}], 'seasons': [],
                'pagination': {'has_more': offset < 1000, 'limit': 500}}

    def test_paging_mapping_person_and_resync(self):
        with patch.object(self.s, '_mdblist_http', side_effect=self.fake) as http:
            events, cursor, stats = self.s._connector_mdblist(self.c, NOW)
            self.c['cursor'] = cursor
            self.assertEqual([c.kwargs.get('offset') for c in http.call_args_list if c.args[0] == '/sync/watched'], [0, 500, 1000])
            items = self.s.read_trakt()
            self.assertEqual(len(items), 3)
            show = next(i for i in items if i['id'] == 'tmdb:2')
            self.assertEqual(show['episodes_played'], 3)
            self.assertEqual(show['signal'], 'watched')
            self.assertEqual({e['who'] for e in events}, {self.person})
            self.assertEqual({e['action'] for e in events}, {'play', 'interest'})
            http.reset_mock()
            self.assertEqual(self.s._connector_mdblist(self.c, NOW + 60)[0], [])
            self.assertEqual([c.args[0] for c in http.call_args_list], ['/sync/last_activities'])
            with patch.object(self.s, '_mdblist_http', side_effect=lambda p, k, **kw: {'watched_at': STAMP + 'changed', 'watchlisted_at': STAMP} if p == '/sync/last_activities' else self.fake(p, k, **kw)):
                self.assertEqual(self.s._connector_mdblist(self.c, NOW + 120)[0], [])

    def test_errors_redact_key(self):
        for code, message in ((401, 'key rejected'), (403, 'key rejected'), (429, 'rate limit'), (500, 'request failed')):
            with self.subTest(code=code), patch.object(self.s, '_http_get', side_effect=urllib.error.HTTPError('https://fake/?apikey=' + KEY, code, KEY, {}, None)):
                with self.assertRaises(ValueError) as caught:
                    self.s._mdblist_http('/sync/watched', KEY)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))
                import traceback
                self.assertNotIn(KEY, "".join(traceback.format_exception(caught.exception)))

    def test_empty_page_stops(self):
        with patch.object(self.s, '_mdblist_http', return_value={'movies': [], 'episodes': [], 'pagination': {'has_more': True}}) as http:
            with self.assertRaises(ValueError):
                self.s._mdblist_pages('/sync/watched', KEY, ('movies', 'episodes', 'shows', 'seasons'))
            self.assertEqual(http.call_count, 1)

    def test_config_status_and_disconnect(self):
        from skald import setup, members
        from skald.__main__ import parser
        cfg = members.validate_config(self.s.CONFIG, data=str(self.data))
        (self.data/'skald.json').write_text(json.dumps(cfg))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            status = setup.status(self.data)
            self.assertTrue(status['connections']['mdblist'][self.person])
            self.s.connectors_load(NOW)
            view = self.s.connectors_view()
            self.assertIn('mdblist', self.s._connectors)
            args = parser().parse_args(['setup', '--disconnect', 'mdblist', '--person', self.person])
            with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}):
                setup.run(args)
        self.assertFalse((self.data/'credentials'/'synthetic.token').exists())
        self.assertFalse(setup.status(self.data)['connections']['mdblist'][self.person])
        for value in (output.getvalue(), json.dumps(status), json.dumps(view)):
            self.assertNotIn(KEY, value)

    def test_watchlist_shapes_imdb_fallback_and_movie_mapping(self):
        rows = {'movies': [{'movie': title(4)}, title(5), {'movie': {'ids': {'imdb': 'tt123'}, 'title': 'IMDb only'}}, {'ids': {}}],
                'shows': [{'show': title(6)}]}
        with patch.object(self.s, 'tmdb_details', return_value=('movie', {'id': 7})) as resolve:
            items = self.s._mdblist_items(rows, NOW, False)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual({i['id'] for i in items}, {'tmdb:4', 'tmdb:5', 'tmdb:6', 'tmdb:7'})
        self.assertEqual({i['signal'] for i in items}, {'interest'})
        watched = self.s._mdblist_items({'movies': [{'movie': title(), 'last_watched_at': STAMP}]}, NOW, True)
        expected = self.s._trakt_import_history({'exported_at': STAMP, 'movies': [{'tmdb': 1, 'title': 'Synthetic', 'year': 2020,
            'genres': [], 'plays': [int(self.s._connector_time(STAMP) / 60)]}], 'shows': []}, NOW)['items'][0]
        for field in ('signal', 'weight', 'at', 'plays'):
            self.assertEqual(watched[0][field], expected[field])

    def test_http_timeout_query_and_pagination_fallback(self):
        response = io.BytesIO(json.dumps({'watched_at': STAMP}).encode())
        with patch.object(self.s, '_connector_open', return_value=response) as http:
            self.s._mdblist_http('/sync/last_activities', KEY)
        self.assertEqual(http.call_args.kwargs['timeout'], 10)
        self.assertIn('apikey=' + KEY, http.call_args.args[0].full_url)
        pages = [{'movies': [title()], 'pagination': {'has_more': True}},
                 {'movies': [title(2)], 'pagination': {'has_more': False}}]
        with patch.object(self.s, '_mdblist_http', side_effect=pages) as http:
            self.assertEqual(len(self.s._mdblist_pages('/watchlist/items', KEY, ('movies', 'shows'))['movies']), 2)
        self.assertEqual([c.kwargs['offset'] for c in http.call_args_list], [0, 500])

    def test_rate_limit_stops_accounts_and_preserves_history(self):
        output = io.StringIO()
        with patch.object(self.s, '_mdblist_http', side_effect=self.fake):
            self.s._connector_mdblist(self.c, NOW)
        path = self.data/('mdblist-' + self.person + '.history.json')
        before = path.read_bytes()
        self.s.CONFIG['connectors']['mdblist']['accounts'][self.s.PEOPLE[0]] = 'file:other.token'
        parent = self.s.connector_seeds(NOW)['mdblist']
        with redirect_stdout(output), redirect_stderr(output), patch.object(self.s, '_http_get',
                side_effect=urllib.error.HTTPError('https://fake/?apikey=' + KEY, 429, KEY, {}, None)) as http:
            events, cursor, stats = self.s._connector_mdblist(parent, NOW)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(events, [])
        self.assertGreater(stats['backoff_until'], NOW)
        self.assertIn('rate limit', stats['accounts'][self.person]['health']['fixHint'])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(KEY, output.getvalue() + json.dumps(stats))

    def test_connect_stdin_private_per_person_and_probe(self):
        from skald import setup, members
        from skald.__main__ import parser, main
        (self.data/'skald.json').write_text(json.dumps(members.validate_config(self.s.CONFIG)))
        other = self.s.PEOPLE[0]
        args = parser().parse_args(['setup', '--connect', 'mdblist', '--person', other, '--yes', '--secret-stdin'])
        output = io.StringIO()
        with patch.dict('os.environ', {'DATA_DIR': str(self.data), 'SKALD_CONFIG': str(self.data/'skald.json')}), \
                patch('sys.stdin', io.StringIO(KEY + '\n')), redirect_stdout(output), redirect_stderr(output), \
                patch.object(self.s, '_mdblist_http', side_effect=self.fake):
            result = setup.configure_connector(self.s, args, lambda v, label, default, flag: v or default, [], lambda: None)
            self.assertTrue(result['ok'])
            saved = json.loads((self.data/'skald.json').read_text())
            accounts = saved['connectors']['mdblist']['accounts']
            self.assertEqual(set(accounts), set(self.s.PEOPLE))
            token = self.data/'credentials'/accounts[other][5:]
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token.read_text(), KEY)
            self.assertEqual(main(['setup', '--status'], out=output), 1 if setup.status(self.data)['missing'] else 0)
            self.assertFalse(list(self.data.glob('mdblist-*.history.json')))
        self.assertNotIn(KEY, output.getvalue() + json.dumps(saved))

    def test_persisted_sync_folds_one_signal_per_show(self):
        with patch.object(self.s, '_mdblist_http', side_effect=self.fake):
            self.s._connector_mdblist(self.c, NOW)
        items = self.s.read_trakt()
        with patch.object(self.s.time, "time", return_value=NOW):
            first = self.s.build_profile([], [], items, remote=False)
            second = self.s.build_profile([], [], items, remote=False)
        self.assertEqual(first, second)
        show = [e for e in first['entries'] if e['key'] == 'tmdb:2']
        self.assertEqual(len(show), 1)
        self.assertEqual(show[0]['by'], [self.person])
        with redirect_stdout(io.StringIO()), patch.object(self.s, '_mdblist_http', side_effect=self.fake):
            self.s.connectors_load(NOW)
            events, cursor, stats = self.s.poll(self.s._connectors['mdblist'], NOW)
        # The cached activities skip list HTTP even after connector state is reloaded.
        self.assertEqual(events, [])

    def mixed_profile(self, trakt, mdblist, person=None):
        person = person or self.person
        self.s.CONFIG['connectors']['trakt_api'] = {'accounts': {person: 'file:synthetic.token'}}
        self.s._connector_write('trakt-' + person + '.history', {'items': trakt})
        self.s._connector_write('mdblist-' + self.person + '.history', {'items': mdblist})
        items = self.s.read_trakt()
        with patch.object(self.s.time, 'time', return_value=NOW):
            profile = self.s.build_profile([], [], items, remote=False)
        return items, profile

    def test_trakt_rating_survives_mdblist_watchlist(self):
        rated = {'id': 'tmdb:1', 'type': 'movie', 'rating': 9, 'signal': 'loved', 'weight': 3}
        watchlist = {'id': 'tmdb:1', 'type': 'movie', 'signal': 'interest', 'weight': .35}
        _, alone = self.mixed_profile([rated], [])
        items, mixed = self.mixed_profile([rated], [watchlist])
        entry = self.s.person_entries(mixed, self.person)[0]
        self.assertEqual(entry['signal'], 'loved')
        self.assertEqual(entry['weight'], self.s.person_entries(alone, self.person)[0]['weight'])
        self.assertEqual(items, [{**rated, 'person': self.person}])

    def test_watched_beats_watchlist_from_either_source(self):
        watched = {'id': 'tmdb:1', 'type': 'movie', 'played': True, 'signal': 'watched', 'weight': 2}
        watchlist = {'id': 'tmdb:1', 'type': 'movie', 'signal': 'interest', 'weight': .35}
        for trakt, mdblist in ((watchlist, watched), (watched, watchlist)):
            with self.subTest(trakt=trakt['signal']):
                items, profile = self.mixed_profile([trakt], [mdblist])
                self.assertEqual(items, [{**watched, 'person': self.person}])
                self.assertEqual(self.s.person_entries(profile, self.person)[0]['signal'], 'watched')

    def test_same_title_for_different_members_stays_separate(self):
        other = self.s.PEOPLE[0]
        rated = {'id': 'tmdb:1', 'type': 'movie', 'rating': 9, 'signal': 'loved', 'weight': 3}
        watchlist = {'id': 'tmdb:1', 'type': 'movie', 'signal': 'interest', 'weight': .35}
        items, profile = self.mixed_profile([rated], [watchlist], other)
        self.assertEqual(items, [{**rated, 'person': other}, {**watchlist, 'person': self.person}])
        self.assertEqual(self.s.person_entries(profile, other)[0]['signal'], 'loved')
        self.assertEqual(self.s.person_entries(profile, self.person)[0]['signal'], 'interest')

    def test_summary_does_not_invent_or_duplicate_episodes(self):
        summary = {'show': title(2), 'last_watched_at': STAMP}
        rows = {'episodes': [{'episode': {'show': title(2), 'season': 1, 'number': n}, 'last_watched_at': STAMP}
                             for n in (1, 2, 3)], 'shows': [summary], 'seasons': [summary]}
        items = self.s._mdblist_items(rows, NOW, True)
        self.assertEqual(items[0]['episodes_played'], 3)
        summary_items = self.s._mdblist_items({'shows': [summary]}, NOW, True)
        self.assertEqual(summary_items[0]['signal'], 'watched')
        self.assertEqual(summary_items[0]['type'], 'series')
        self.assertFalse(summary_items[0].get('last_episode'))

    def test_history_allowlists_exclude_keys(self):
        import tarfile
        from skald import live, backup
        name = 'mdblist-' + self.person + '.history.json'
        self.assertTrue(live._snapshot_file(name))
        member = tarfile.TarInfo(name)
        backup._member(member, set(), 0)
        self.assertFalse(live._snapshot_file('credentials/synthetic.token'))
        with self.assertRaises(ValueError):
            backup._member(tarfile.TarInfo('credentials/synthetic.token'), set(), 0)
