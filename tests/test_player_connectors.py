"""Offline, unverified player API fixtures through validation and polling."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from decide_kit import close_server, load_server
from history_kit import NOW, Reply, ROOT
from player_kit import player_fixture, jellyfin_transport, plex_transport
from skald import members
from skald.__main__ import main
from test_api import raw_request

CANARY = 'FAKE_PLAYER_TOKEN_CANARY_23d'


class PlayerConnectors(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.s.CONFIG = self.s.fresh_config(['ana', 'ben'])
        self.s.PEOPLE = ('ana', 'ben')
        self.s.TRAKT_PERSON = 'ana'
        self.auth = patch.dict(os.environ, {'FAKE_PLAYER_TOKEN': CANARY})
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(members.configure)

    def config(self, cid):
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['history']['source'] = cid
        cfg['connectors'][cid] = {'kind': 'player', 'enabled': True, 'auth': 'env:FAKE_PLAYER_TOKEN',
            'options': {'url': 'http://player:8096', 'profile': 'household', 'section': 'movies'}}
        if cid == 'jellyfin':
            cfg['connectors'][cid]['options']['user_id'] = 'fixture-user'
        return cfg

    def configure(self, cid):
        self.s.CONFIG = self.s.validate_config(self.config(cid), data=str(self.data))
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        return self.s._connectors[cid]

    def drain(self, now=NOW):
        self.s.connectors_tick(now)
        if self.s._connector_pending:
            self.s._connector_pending[2].result(timeout=3)
            self.s.connectors_tick(now + .01)

    def test_config_accepts_player_sources_with_referenced_auth(self):
        for cid in ('jellyfin', 'plex'):
            with self.subTest(cid=cid):
                cfg = self.config(cid)
                self.assertEqual(self.s.validate_config(cfg, data=str(self.data)), cfg)

    def test_config_rejects_bad_options_without_exposing_values(self):
        for cid in ('jellyfin', 'plex'):
            changes = [('url', ''), ('url', 1), ('url', 'https://user:' + CANARY + '@example.com'),
                       ('url', 'https://example.com/?token=' + CANARY), ('url', 'http://player/#' + CANARY),
                       ('url', 'https://player/\n'), ('section', []), ('section', ''),
                       ('profile', 'stranger'), ('profile', []), ('token', CANARY)]
            if cid == 'jellyfin':
                changes += [('user_id', ''), ('user_id', None)]
            for field, value in changes:
                with self.subTest(cid=cid, field=field, value=value):
                    cfg = self.config(cid)
                    cfg['connectors'][cid]['options'][field] = value
                    with self.assertRaises(self.s.ConfigError) as error:
                        self.s.validate_config(cfg, data=str(self.data))
                    self.assertNotIn(CANARY, str(error.exception))

    def test_jellyfin_movie_maps_user_data_to_exporter_shape(self):
        self.configure('jellyfin')
        movie = json.loads((ROOT / 'tests/fixtures/connectors/jellyfin.json').read_text())['items'][0]
        def transport(req, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.get_header('X-emby-token'), CANARY)
            url = urllib.parse.urlsplit(req.full_url)
            self.assertEqual(url.path, '/Users/fixture-user/Items')
            query = urllib.parse.parse_qs(url.query)
            self.assertEqual(query['Recursive'], ['true'])
            self.assertEqual(query['IncludeItemTypes'], ['Movie,Episode,Series' if query['Filters'] == ['IsFavorite'] else 'Movie,Episode'])
            self.assertEqual(query['Fields'], ['ProviderIds'])
            self.assertEqual(query['EnableUserData'], ['true'])
            self.assertEqual(query['ParentId'], ['movies'])
            return Reply({'Items': [movie] if query['Filters'] == ['IsPlayed'] else [], 'TotalRecordCount':
                          1 if query['Filters'] == ['IsPlayed'] else 0, 'StartIndex': 0})
        with patch.object(self.s, '_connector_open', transport), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['jellyfin']['health']['state'], 'ok')
        self.assertEqual(json.loads((self.data / 'history.json').read_text()), {'items': [
            {'profile': 'household', 'type': 'movie', 'id': 'tmdb:603', 'at': 1791590400000,
             'played': True, 'favorite': True, 'progress': 0, 'entries': 1, 'rating': None, 'likes': None, 'dropped': False}]})

    def test_jellyfin_series_ids_union_filters_drops_order_and_unchanged_write(self):
        self.configure('jellyfin')
        logs = io.StringIO()
        with patch.object(self.s, '_connector_open', jellyfin_transport(player_fixture('jellyfin'))), redirect_stdout(logs):
            self.drain()
        self.assertEqual(self.s._connectors['jellyfin']['health']['state'], 'ok')
        expected = {'items': [
            {'profile':'household','type':'series','id':'tmdb:1419','at':1791676800000,'played':True,'favorite':True,
             'progress':.42,'entries':3,'rating':None,'likes':None,'dropped':False,'episodes_played':2,
             'last_episode':[1,2],'resume_episode':[2,1]},
            {'profile':'household','type':'movie','id':'tmdb:603','at':1791590400000,'played':True,'favorite':True,
             'progress':0,'entries':1,'rating':None,'likes':None,'dropped':False},
            {'profile':'household','type':'movie','id':'tmdb:604','at':1791504000000,'played':False,'favorite':False,
             'progress':.25,'entries':1,'rating':None,'likes':None,'dropped':False},
            {'profile':'household','type':'movie','id':'tmdb:605','at':None,'played':False,'favorite':True,
             'progress':0,'entries':1,'rating':None,'likes':None,'dropped':False}]}
        path = self.data / 'history.json'
        self.assertEqual(path.read_bytes(), json.dumps(expected).encode())
        self.assertIn('jellyfin dropped_idless=1', logs.getvalue())
        stamp = path.stat().st_mtime_ns
        with patch.object(self.s, '_connector_open', jellyfin_transport(player_fixture('jellyfin'))), \
                patch.object(self.s.os, 'replace', side_effect=AssertionError('unchanged history rewritten')), redirect_stdout(logs):
            self.s.poll(self.s._connectors['jellyfin'], NOW + 1)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_jellyfin_pages_are_complete_before_ordering_and_120_cap(self):
        for name in ('jellyfin', 'aiostreams-jellyfin'):
            with self.subTest(fixture=name):
                self.configure('jellyfin')
                self.s._connectors['jellyfin']['next_due'] = NOW
                fixture = player_fixture(name)
                movie = fixture.get('history_items', fixture['items'])[0]
                fixture['items'] = [{**movie, 'Id': str(i), 'ProviderIds': {'Tmdb': str(1000 + i)},
                                    'UserData': {**movie['UserData'], 'IsFavorite': False}} for i in range(125)]
                fixture['items'] += [fixture['items'][0], {**movie, 'Id': 'idless', 'ProviderIds': {}}]
                logs = io.StringIO()
                with patch.object(self.s, '_connector_open', jellyfin_transport(fixture)), redirect_stdout(logs):
                    self.drain()
                self.assertEqual(self.s._connectors['jellyfin']['health']['error'], None)
                items = json.loads((self.data / 'history.json').read_text())['items']
                self.assertEqual(len(items), 120)
                self.assertEqual(items[0]['id'], 'tmdb:1000')
                self.assertEqual(items[-1]['id'], 'tmdb:1119')
                self.assertEqual(items[0]['entries'], 1)
                self.assertIn('jellyfin dropped_idless=1', logs.getvalue())

    def test_plex_episode_leaf_pagination_keeps_furthest_watched_episode(self):
        cfg = self.config('plex')
        cfg['connectors']['plex']['options']['section'] = 'shows'
        self.s.CONFIG = self.s.validate_config(cfg)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        fixture = player_fixture('plex')
        episode = fixture['leaves']['s1'][0]
        fixture['leaves']['s1'] = [{**episode, 'ratingKey':str(i), 'index':i + 1} for i in range(125)]
        with patch.object(self.s, '_connector_open', plex_transport(fixture)), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], None)
        item = json.loads((self.data / 'history.json').read_text())['items'][0]
        self.assertEqual((item['id'], item['entries'], item['episodes_played'], item['last_episode']),
                         ('tmdb:1419', 125, 125, [1,125]))

    def test_emulation_without_user_data_is_unsupported_and_preserves_history(self):
        self.configure('jellyfin')
        path = self.data / 'history.json'
        old = b'{"items": [{"profile":"household","type":"movie","id":"tmdb:1","at":1}]}'
        path.write_bytes(old)
        stamp = path.stat().st_mtime_ns
        logs = io.StringIO()
        with patch.object(self.s, '_connector_open', jellyfin_transport(player_fixture('aiostreams-jellyfin'))), \
                redirect_stdout(logs), redirect_stderr(logs):
            self.drain()
        self.assertEqual(self.s._connectors['jellyfin']['health']['error'], 'Unsupported')
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_plex_movies_use_header_auth_and_selected_section(self):
        self.configure('plex')
        fixture = player_fixture('plex')
        fixture['libraries']['movies'] = fixture['libraries']['movies'][:1]
        opened = []
        transport = plex_transport(fixture)
        def checked(req, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.get_header('X-plex-token'), CANARY)
            self.assertEqual(req.get_header('Accept'), 'application/json')
            opened.append(urllib.parse.urlsplit(req.full_url).path)
            if opened[-1].endswith('/all'):
                self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)['includeGuids'], ['1'])
            return transport(req, timeout)
        with patch.object(self.s, '_connector_open', checked), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], None)
        self.assertEqual(json.loads((self.data / 'history.json').read_text()), {'items': [
            {'profile':'household','type':'movie','id':'tmdb:603','at':1791590400000,'played':True,
             'favorite':False,'progress':0,'entries':1,'rating':None,'likes':None,'dropped':False}]})
        self.assertEqual(opened, ['/library/sections', '/library/sections/movies/all'])

    def test_plex_episodes_use_show_guids_and_ignore_unplayed_titles(self):
        cfg = self.config('plex')
        del cfg['connectors']['plex']['options']['section']
        cfg['connectors']['plex']['options']['profile'] = 'ana'
        self.s.CONFIG = self.s.validate_config(cfg)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        logs = io.StringIO()
        with patch.object(self.s, '_connector_open', plex_transport(player_fixture('plex'))), redirect_stdout(logs):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], None)
        self.assertEqual(json.loads((self.data / 'history.json').read_text()), {'items': [
            {'profile':'ana','type':'series','id':'tmdb:1419','at':1791676800000,'played':True,'favorite':False,
             'progress':.42,'entries':3,'rating':None,'likes':None,'dropped':False,'episodes_played':2,
             'last_episode':[1,2],'resume_episode':[2,1]},
            {'profile':'ana','type':'movie','id':'tmdb:603','at':1791590400000,'played':True,'favorite':False,
             'progress':0,'entries':1,'rating':None,'likes':None,'dropped':False},
            {'profile':'ana','type':'movie','id':'tmdb:604','at':1791504000000,'played':False,'favorite':False,
             'progress':.25,'entries':1,'rating':None,'likes':None,'dropped':False}]})
        self.assertIn('plex dropped_idless=1', logs.getvalue())
        path = self.data / 'history.json'
        stamp = path.stat().st_mtime_ns
        with patch.object(self.s, '_connector_open', plex_transport(player_fixture('plex'))), \
                patch.object(self.s.os, 'replace', side_effect=AssertionError('unchanged history rewritten')), redirect_stdout(logs):
            self.s.poll(self.s._connectors['plex'], NOW + 1)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_plex_pages_sections_movies_and_leaves_before_120_cap(self):
        self.configure('plex')
        fixture = player_fixture('plex')
        movie = fixture['libraries']['movies'][0]
        fixture['sections'] = [{'key': str(i), 'type': 'artist'} for i in range(101)] + fixture['sections']
        fixture['libraries']['movies'] = [{**movie, 'ratingKey': str(i), 'Guid': [{'id': 'tmdb://' + str(1000 + i)}]}
                                         for i in range(125)]
        fixture['libraries']['movies'] += [fixture['libraries']['movies'][0], {**movie, 'ratingKey': 'idless', 'Guid': []}]
        logs = io.StringIO()
        with patch.object(self.s, '_connector_open', plex_transport(fixture)), redirect_stdout(logs):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], None)
        items = json.loads((self.data / 'history.json').read_text())['items']
        self.assertEqual(len(items), 120)
        self.assertEqual(items[0]['id'], 'tmdb:1000')
        self.assertEqual(items[-1]['id'], 'tmdb:1119')
        self.assertEqual(items[0]['entries'], 1)
        self.assertIn('plex dropped_idless=1', logs.getvalue())

    def test_malformed_player_numbers_preserve_the_previous_history(self):
        cases = [('jellyfin', 'UserData.Played', 'false'), ('jellyfin', 'UserData.IsFavorite', 1),
                 ('jellyfin', 'UserData.PlayCount', -1), ('jellyfin', 'UserData.PlaybackPositionTicks', float('nan')),
                 ('jellyfin', 'UserData.LastPlayedDate', 'not-a-date'), ('jellyfin', 'RunTimeTicks', -1),
                 ('plex', 'viewCount', -1), ('plex', 'viewCount', True), ('plex', 'viewOffset', -1),
                 ('plex', 'lastViewedAt', float('inf')), ('plex', 'duration', -1)]
        for cid, field, value in cases:
            with self.subTest(cid=cid, field=field):
                self.configure(cid)
                path = self.data / 'history.json'
                old = b'{"items": []}'
                path.write_bytes(old)
                fixture = player_fixture(cid)
                row = fixture['items'][0] if cid == 'jellyfin' else fixture['libraries']['movies'][0]
                target = row
                if '.' in field:
                    parent, field = field.split('.')
                    target = row[parent]
                target[field] = value
                transport = jellyfin_transport(fixture) if cid == 'jellyfin' else plex_transport(fixture)
                self.s._connectors[cid]['next_due'] = NOW + 10000
                with patch.object(self.s, '_connector_open', transport), redirect_stdout(io.StringIO()):
                    self.drain(NOW + 10000)
                self.assertEqual(self.s._connectors[cid]['health']['error'], 'ValueError')
                self.assertEqual(path.read_bytes(), old)

    def test_only_the_primary_player_is_enabled_for_polling(self):
        cfg = self.config('jellyfin')
        cfg['connectors']['plex'] = self.config('plex')['connectors']['plex']
        self.s.CONFIG = self.s.validate_config(cfg)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        self.assertEqual(self.s._connectors['plex']['health']['state'], 'disabled')
        with patch.object(self.s, '_connector_open', jellyfin_transport(player_fixture('jellyfin'))), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['jellyfin']['counts']['polls'], 1)
        self.assertEqual(self.s._connectors['plex']['counts']['polls'], 0)

    def test_inconsistent_player_pages_never_replace_history(self):
        for cid in ('jellyfin', 'plex'):
            for failure in ('short_page', 'changed_total', 'wrong_offset', 'negative_total'):
                with self.subTest(cid=cid, failure=failure):
                    self.configure(cid)
                    self.s._connectors[cid]['next_due'] = NOW
                    path = self.data / 'history.json'
                    old = b'{"items": []}'
                    path.write_bytes(old)
                    movie = player_fixture('jellyfin')['items'][0] if cid == 'jellyfin' else player_fixture('plex')['libraries']['movies'][0]
                    rows = [{**movie, 'Id': str(i), 'ratingKey': str(i)} for i in range(125)]
                    def transport(req, timeout):
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
                        if cid == 'plex' and urllib.parse.urlsplit(req.full_url).path == '/library/sections':
                            return Reply({'MediaContainer': {'Directory': [{'key':'movies','type':'movie'}], 'size':1}})
                        start = int(query['StartIndex' if cid == 'jellyfin' else 'X-Plex-Container-Start'][0])
                        page = rows[start:start + 100]
                        total, offset = 125, start
                        if failure == 'short_page' and start == 0:
                            page = page[:1]
                        if failure == 'changed_total' and start:
                            total = 126
                        if failure == 'wrong_offset':
                            offset += 1
                        if failure == 'negative_total':
                            total = -1
                        return Reply({'Items':page,'TotalRecordCount':total,'StartIndex':offset} if cid == 'jellyfin' else
                                     {'MediaContainer':{'Metadata':page,'totalSize':total,'offset':offset,'size':len(page)}})
                    with patch.object(self.s, '_connector_open', transport), redirect_stdout(io.StringIO()):
                        self.drain()
                    self.assertEqual(self.s._connectors[cid]['health']['error'], 'ValueError')
                    self.assertEqual(path.read_bytes(), old)

    def test_invalid_tmdb_ids_are_dropped_and_counted_without_exposure(self):
        for cid in ('jellyfin', 'plex'):
            with self.subTest(cid=cid):
                self.configure(cid)
                fixture = player_fixture(cid)
                invalid = ('0', '-1', True, 1.5, CANARY, None)
                if cid == 'jellyfin':
                    movie = fixture['items'][0]
                    fixture['items'] = [{**movie, 'Id':str(i), 'ProviderIds':{'Tmdb':tid}} for i, tid in enumerate(invalid)]
                else:
                    movie = fixture['libraries']['movies'][0]
                    fixture['libraries']['movies'] = [{**movie, 'ratingKey':str(i), 'Guid':[{'id':'tmdb://' + str(tid)}]}
                                                     for i, tid in enumerate(invalid)]
                logs = io.StringIO()
                transport = jellyfin_transport(fixture) if cid == 'jellyfin' else plex_transport(fixture)
                with patch.object(self.s, '_connector_open', transport), redirect_stdout(logs):
                    self.drain()
                self.assertEqual(self.s._connectors[cid]['health']['error'], None)
                self.assertEqual(json.loads((self.data / 'history.json').read_text()), {'items': []})
                self.assertIn(cid + ' dropped_idless=6', logs.getvalue())
                self.assertNotIn(CANARY, logs.getvalue() + (self.data / 'history.json').read_text())

    def test_auth_canaries_stay_out_of_files_health_api_cli_and_logs(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        credentials = self.data / 'credentials'
        credentials.mkdir(mode=0o700)
        (credentials / 'player.token').write_text(CANARY + '\n')
        for cid in ('jellyfin', 'plex'):
            for reference in ('env:FAKE_PLAYER_TOKEN', 'file:player.token'):
                with self.subTest(cid=cid, reference=reference):
                    cfg = self.config(cid)
                    cfg['connectors'][cid]['auth'] = reference
                    self.s.CONFIG = self.s.validate_config(cfg, data=str(self.data))
                    logs, cli = io.StringIO(), io.StringIO()
                    transport = jellyfin_transport(player_fixture(cid)) if cid == 'jellyfin' else plex_transport(player_fixture(cid))
                    def checked(req, timeout):
                        self.assertEqual(req.get_header('X-emby-token' if cid == 'jellyfin' else 'X-plex-token'), CANARY)
                        self.assertNotIn(CANARY, req.full_url)
                        return transport(req, timeout)
                    with patch.object(self.s, '_connector_open', checked), redirect_stdout(logs), redirect_stderr(logs):
                        self.s.connectors_load(NOW)
                        self.s._connectors[cid]['next_due'] = NOW
                        self.drain()
                        token = self.s.new_client('fixture-' + cid + reference.split(':')[0], ['read'], 'ana')
                        code, _, api, _ = raw_request(self.s, '/api/v1/connectors', headers={'Authorization':'Bearer ' + token})
                        self.assertEqual(code, 200)
                        (self.data / 'skald.json').write_text(json.dumps(self.s.CONFIG))
                        self.assertEqual(main(['--snapshot',str(self.data),'--state-dir',state.name,
                                               '--json','connectors'], out=cli), 0, logs.getvalue() + cli.getvalue())
                    self.assertEqual(self.s._connectors[cid]['health']['error'], None)
                    for raw in ((self.data / 'connectors.json').read_text(), (self.data / 'history.json').read_text(),
                                api.decode(), cli.getvalue(), logs.getvalue(), json.dumps(self.s.public_config(self.s.CONFIG))):
                        self.assertNotIn(CANARY, raw)

    def test_shared_transport_errors_preserve_history_without_exposing_auth(self):
        for cid in ('jellyfin', 'plex'):
            for failure in ('public_http', 'byte_cap', 'page_cap', 'remote_error'):
                with self.subTest(cid=cid, failure=failure):
                    self.configure(cid)
                    self.s._connectors[cid]['next_due'] = NOW
                    path = self.data / 'history.json'
                    old = b'{"items": []}'
                    path.write_bytes(old)
                    if failure == 'public_http':
                        self.s.CONFIG['connectors'][cid]['options']['url'] = 'http://example.com'
                    transport = jellyfin_transport(player_fixture(cid)) if cid == 'jellyfin' else plex_transport(player_fixture(cid))
                    logs = io.StringIO()
                    error = {'public_http':'UnsafeURL', 'byte_cap':'ByteCapExceeded', 'page_cap':'PageCapExceeded', 'remote_error':'RuntimeError'}[failure]
                    with patch.object(self.s, 'CONNECTOR_HTTP_BYTES', 10 if failure == 'byte_cap' else 20 * 1024 * 1024), \
                            patch.object(self.s, 'CONNECTOR_HTTP_PAGES', 1 if failure == 'page_cap' else 100), \
                            patch.object(self.s, '_connector_open', side_effect=AssertionError('unsafe URL opened') if failure == 'public_http' else
                                         RuntimeError(CANARY) if failure == 'remote_error' else transport), redirect_stdout(logs), redirect_stderr(logs):
                        self.drain()
                    self.assertEqual(self.s._connectors[cid]['health']['error'], error)
                    self.assertEqual(path.read_bytes(), old)
                    self.assertNotIn(CANARY, logs.getvalue() + (self.data / 'connectors.json').read_text())

    def test_invalid_item_kinds_and_episode_coordinates_preserve_history(self):
        for cid in ('jellyfin', 'plex'):
            for failure in ('wrong_kind', 'bad_season', 'bad_episode'):
                with self.subTest(cid=cid, failure=failure):
                    cfg = self.config(cid)
                    cfg['connectors'][cid]['options'].pop('section')
                    self.s.CONFIG = self.s.validate_config(cfg)
                    with redirect_stdout(io.StringIO()):
                        self.s.connectors_load(NOW)
                    self.s._connectors[cid]['next_due'] = NOW
                    fixture = player_fixture(cid)
                    row = fixture['items'][3] if cid == 'jellyfin' else fixture['leaves']['s1'][0]
                    field = ('Type' if cid == 'jellyfin' else 'type') if failure == 'wrong_kind' else (
                        ('ParentIndexNumber' if cid == 'jellyfin' else 'parentIndex') if failure == 'bad_season' else
                        ('IndexNumber' if cid == 'jellyfin' else 'index'))
                    row[field] = 'not-an-episode' if failure == 'wrong_kind' else -1
                    path = self.data / 'history.json'
                    old = b'{"items": []}'
                    path.write_bytes(old)
                    transport = jellyfin_transport(fixture) if cid == 'jellyfin' else plex_transport(fixture)
                    with patch.object(self.s, '_connector_open', transport), redirect_stdout(io.StringIO()):
                        self.drain()
                    self.assertEqual(self.s._connectors[cid]['health']['error'], 'ValueError')
                    self.assertEqual(path.read_bytes(), old)

    def test_supported_emulation_hypothesis_maps_shared_login_to_household(self):
        self.configure('jellyfin')
        fixture = player_fixture('aiostreams-jellyfin')
        fixture['items'] = fixture['history_items']
        with patch.object(self.s, '_connector_open', jellyfin_transport(fixture)), redirect_stdout(io.StringIO()):
            self.drain()
        items = json.loads((self.data / 'history.json').read_text())['items']
        self.assertEqual([item['id'] for item in items], ['tmdb:603','tmdb:604'])
        self.assertEqual([item['progress'] for item in items], [0,.25])
        self.assertTrue(all(item['profile'] == 'household' for item in items))
        self.assertEqual(self.s.attribute(items[0])[:2], ('together', ['together']))

    def test_fresh_setup_a_uses_stub_jellyfin_and_has_for_you(self):
        from eval.fresh_household import smoke
        result = smoke(2, 'A')
        self.assertEqual(result['history'], 2)
        self.assertGreater(result['foryou'], 0)

    def test_keyless_empty_household_serves_catalog(self):
        from eval.fresh_household import smoke
        result = smoke(1, keyless=True)
        self.assertEqual((result['members'], result['imports'], result['history'], result['experience_tags']), (1, 0, 0, 0))
        self.assertTrue(result['keyless'])
        self.assertGreater(result['catalogs'], 0)
        self.assertGreater(result['token_metas'], 0)
        self.assertEqual((result['rows'], result['nonempty'], result['foryou'], result['rewatch'], result['popular']),
                         (28, 4, 24, 0, 10))

    def test_stale_plex_section_selection_preserves_history(self):
        self.configure('plex')
        fixture = player_fixture('plex')
        fixture['sections'] = [{'key':'renamed','type':'movie'}]
        path = self.data / 'history.json'
        old = b'{"items": [{"type":"movie","id":"tmdb:1","at":1}]}'
        path.write_bytes(old)
        with patch.object(self.s, '_connector_open', plex_transport(fixture)), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], 'ValueError')
        self.assertEqual(path.read_bytes(), old)

    def test_member_player_history_contributes_and_events_credit_that_member(self):
        for cid in ('jellyfin', 'plex'):
            for profile in ('ana', 'household'):
                with self.subTest(cid=cid, profile=profile):
                    cfg = self.config(cid)
                    cfg['connectors'][cid]['options']['profile'] = profile
                    self.s.CONFIG = self.s.validate_config(cfg)
                    self.s._profiles.clear()
                    with redirect_stdout(io.StringIO()):
                        self.s.connectors_load(NOW)
                    self.s._connectors[cid].update(next_due=NOW, cursor=None)
                    fixture = player_fixture(cid)
                    if cid == 'jellyfin':
                        fixture['items'] = fixture['items'][:1]
                        transport = jellyfin_transport(fixture)
                    else:
                        fixture['libraries']['movies'] = fixture['libraries']['movies'][:1]
                        transport = plex_transport(fixture)
                    details = ('movie', {'id':603,'title':'Synthetic member film','release_date':'1999-01-01','genres':[]})
                    with patch.object(self.s, '_connector_open', transport), \
                            patch.object(self.s, 'tmdb_details', return_value=details), \
                            patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
                        self.drain()
                        self.s.refresh_profiles()
                        entries = self.s._profiles['household']['entries']
                        self.assertEqual([entry['key'] for entry in entries], ['tmdb:603'])
                        who = 'ana' if profile == 'ana' else 'together'
                        self.assertEqual(entries[0]['by'], [who])
                        self.assertEqual(len(self.s.person_entries(self.s._profiles['household'], 'ana')), 1)
                        self.assertEqual(len(self.s.person_entries(self.s._profiles['household'], 'ben')), int(profile == 'household'))
                        events, _, _ = self.s.poll(self.s._connectors[cid], NOW)
                        self.assertEqual([(event['who'], event['by']) for event in events], [(who, [who])])

    def test_jellyfin_parent_favorites_reach_episodes_and_favorite_only_series(self):
        self.configure('jellyfin')
        fixture = player_fixture('jellyfin')
        parent_data = {'Played':False,'PlayCount':0,'PlaybackPositionTicks':0,'IsFavorite':True}
        parent = {**fixture['series']['show-1'], 'UserData':parent_data}
        favorite_only = {'Id':'favorite-only','Type':'Series','ProviderIds':{'Tmdb':'2000'},'UserData':parent_data}
        fixture['series']['show-1'] = parent
        fixture['items'] = [fixture['items'][3], parent, favorite_only]
        with patch.object(self.s, '_connector_open', jellyfin_transport(fixture)), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['jellyfin']['health']['error'], None)
        items = json.loads((self.data / 'history.json').read_text())['items']
        self.assertEqual([(item['id'], item['favorite']) for item in items], [('tmdb:1419',True),('tmdb:2000',True)])
        self.assertEqual([item['entries'] for item in items], [1,1])
        self.assertEqual([item['episodes_played'] for item in items], [1,0])

    def test_plex_large_library_fetches_leaves_only_for_three_active_shows(self):
        for field in ('viewedLeafCount', 'viewOffset', 'lastViewedAt'):
            with self.subTest(field=field):
                cfg = self.config('plex')
                cfg['connectors']['plex']['options']['section'] = 'shows'
                self.s.CONFIG = self.s.validate_config(cfg)
                with redirect_stdout(io.StringIO()):
                    self.s.connectors_load(NOW)
                self.s._connectors['plex']['next_due'] = NOW
                fixture = player_fixture('plex')
                active = {7,42,149}
                fixture['libraries']['shows'] = [{'ratingKey':str(i),'type':'show','Guid':[{'id':f'tmdb://{1000+i}'}],
                                                  field:1 if i in active else 0} for i in range(150)]
                episode = fixture['leaves']['s1'][0]
                fixture['leaves'] = {str(i):[{**episode,'ratingKey':f'episode-{i}'}] for i in range(150)}
                opened = []
                transport = plex_transport(fixture)
                def checked(req, timeout):
                    opened.append(urllib.parse.urlsplit(req.full_url).path)
                    return transport(req, timeout)
                with patch.object(self.s, '_connector_open', checked), redirect_stdout(io.StringIO()):
                    self.drain()
                self.assertEqual(self.s._connectors['plex']['health']['error'], None)
                items = json.loads((self.data / 'history.json').read_text())['items']
                self.assertEqual([item['id'] for item in items], ['tmdb:1007','tmdb:1042','tmdb:1149'])
                self.assertEqual(sum(path.endswith('/allLeaves') for path in opened), 3)
                self.assertEqual(len(opened), 6)

    def test_plex_unplayed_show_keeps_resume_only_episode(self):
        cfg = self.config('plex')
        cfg['connectors']['plex']['options']['section'] = 'shows'
        self.s.CONFIG = self.s.validate_config(cfg)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        fixture = player_fixture('plex')
        show = fixture['libraries']['shows'][0]
        show.update(viewedLeafCount=0, viewOffset=0, lastViewedAt=int(NOW))
        row = fixture['leaves']['s1'][0]
        row.update(viewCount=0, viewOffset=42000, duration=100000, lastViewedAt=int(NOW), parentIndex=1, index=7)
        fixture['leaves']['s1'] = [row]
        with patch.object(self.s, '_connector_open', plex_transport(fixture)), redirect_stdout(io.StringIO()):
            self.drain()
        self.assertEqual(self.s._connectors['plex']['health']['error'], None)
        items = json.loads((self.data / 'history.json').read_text())['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'tmdb:1419')
        self.assertFalse(items[0]['played'])
        self.assertEqual(items[0]['episodes_played'], 0)
        self.assertEqual(items[0]['progress'], .42)
        self.assertEqual(items[0]['resume_episode'], [1,7])
        self.assertNotIn('last_episode', items[0])

    def test_connector_overview_reports_available_player_sources(self):
        overview = (ROOT / 'docs/CONNECTORS.md').read_text().split('| Seed id |')[0]
        self.assertNotIn('reserved source names', overview)
        self.assertNotIn('not built yet', overview)

    def test_played_zero_episode_omits_last_episode_like_exporter(self):
        for cid in ('jellyfin', 'plex'):
            with self.subTest(cid=cid):
                cfg = self.config(cid)
                cfg['connectors'][cid]['options'].pop('section')
                self.s.CONFIG = self.s.validate_config(cfg)
                with redirect_stdout(io.StringIO()):
                    self.s.connectors_load(NOW)
                self.s._connectors[cid]['next_due'] = NOW
                fixture = player_fixture(cid)
                if cid == 'jellyfin':
                    row = fixture['items'][3]
                    row.update(ParentIndexNumber=0, IndexNumber=0, RunTimeTicks=100)
                    row['UserData']['PlaybackPositionTicks'] = 10
                    fixture['items'] = [row]
                    transport = jellyfin_transport(fixture)
                else:
                    row = fixture['leaves']['s1'][0]
                    row.update(parentIndex=0, index=0, viewOffset=10, duration=100)
                    fixture['leaves']['s1'] = [row]
                    fixture['libraries']['movies'] = []
                    transport = plex_transport(fixture)
                with patch.object(self.s, '_connector_open', transport), redirect_stdout(io.StringIO()):
                    self.drain()
                self.assertEqual(self.s._connectors[cid]['health']['error'], None)
                item = json.loads((self.data / 'history.json').read_text())['items'][0]
                self.assertEqual(item['episodes_played'], 1)
                self.assertNotIn('last_episode', item)
                self.assertNotIn('resume_episode', item)


if __name__ == '__main__':
    unittest.main()
