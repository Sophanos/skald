"""Offline adapter parity, bounded scheduling, device flow and credential isolation."""
from skald.members import rid
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from decide_kit import close_server, load_server
from history_kit import ROOT, NOW, Reply, watch_db, trakt_pages, trakt_web_export
from skald import members
from skald.__main__ import main
from test_api import raw_request

CANARY = 'FAKE_HISTORY_AUTH_CANARY_9472'


def oauth_bundle(access=CANARY, refresh=CANARY + '_refresh', created=int(NOW), expires=7200):
    return {'access_token': access, 'refresh_token': refresh, 'created_at': created, 'expires_in': expires}



def offline_importer():
    import reference_history
    return reference_history


class HistoryConnectors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.credentials = self.data/'credentials'
        self.credentials.mkdir(mode=0o700)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        cfg = self.s.fresh_config(['ana', 'ben'])
        cfg['household']['archived'] = {'retired': 'Retired'}
        self.s.CONFIG = self.s.validate_config(cfg)
        self.s.PEOPLE, self.s.TRAKT_PERSON = ('ana', 'ben'), 'ana'
        self.addCleanup(members.configure)

    def configure(self, cid, auth=None, options=None, enabled=True):
        kind = 'player' if cid == 'aiostreams_db' else 'api'
        self.s.CONFIG['connectors'][cid] = {'kind': kind, 'enabled': enabled, 'auth': auth, 'options': options or {}}
        if cid == 'aiostreams_db':
            self.s.CONFIG['history']['source'] = cid
        self.s.CONFIG = self.s.validate_config(self.s.CONFIG)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        return self.s._connectors[cid]

    def test_per_person_accounts_validate_in_both_config_loaders(self):
        cfg = copy.deepcopy(self.s.CONFIG)
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'ana': 'file:trakt-ana.json', 'ben': 'file:trakt-ben.json'}}
        self.assertEqual(self.s.validate_config(cfg), members.validate_config(cfg))
        self.assertEqual(self.s.validate_config(cfg)['connectors']['trakt_api']['accounts'],
                         cfg['connectors']['trakt_api']['accounts'])

    def test_per_person_accounts_reject_unknown_members(self):
        cfg = copy.deepcopy(self.s.CONFIG)
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'retired': 'file:trakt-retired.json'}}
        for loader in (self.s, members):
            with self.assertRaises(loader.ConfigError):
                loader.validate_config(cfg)

    def test_per_person_accounts_reject_duplicate_auth_and_ambiguous_legacy_auth(self):
        for extra in ({'accounts': {'ana': 'file:same.json', 'ben': 'file:same.json'}},
                      {'accounts': {'ana': 'file:trakt-ana.json'}, 'auth': 'file:legacy.json'}):
            cfg = copy.deepcopy(self.s.CONFIG)
            cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, **extra}
            for loader in (self.s, members):
                with self.subTest(extra=extra, loader=loader.__name__), self.assertRaises(loader.ConfigError):
                    loader.validate_config(cfg)

    def test_per_person_accounts_require_private_file_references(self):
        for value in ('env:FAKE_TRAKT_TOKEN', 'file:../outside.json', 'file:/tmp/outside.json',
                      'file:credentials/outside.json', 'file:skald.json'):
            cfg = copy.deepcopy(self.s.CONFIG)
            cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'accounts': {'ana': value}}
            for loader in (self.s, members):
                with self.subTest(value=value, loader=loader.__name__), self.assertRaises(loader.ConfigError):
                    loader.validate_config(cfg)

    def configure_accounts(self):
        self.s.CONFIG['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'ana': 'file:trakt-ana.json', 'ben': 'file:trakt-ben.json'}}
        self.s.CONFIG = self.s.validate_config(self.s.CONFIG)
        for person in ('ana', 'ben'):
            (self.credentials/f'trakt-{person}.json').write_text('fake-'+person)
        (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        return self.s._connectors['trakt_api']

    def test_expired_bundle_renews_and_sync_continues(self):
        self.configure('trakt_api', auth='file:renew.json')
        path = self.credentials / 'renew.json'
        path.write_text(json.dumps(oauth_bundle(created=int(NOW) - 7200)))
        pages, renewals = trakt_pages(), []
        renewed = oauth_bundle(access=CANARY + '_new', refresh=CANARY + '_rotated')
        def transport(req, timeout):
            if req.full_url == 'https://api.trakt.tv/oauth/token':
                self.assertEqual(req.get_method(), 'POST')
                payload = json.loads(req.data)
                self.assertEqual(payload['grant_type'], 'refresh_token')
                self.assertTrue(payload['refresh_token'] == CANARY + '_refresh')
                self.assertTrue(payload['client_secret'] == CANARY + '_secret')
                renewals.append(1)
                return Reply(renewed)
            if req.get_header('Authorization') != 'Bearer ' + renewed['access_token']:
                raise urllib.error.HTTPError(req.full_url, 401, CANARY, {}, None)
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY + '_secret'}), \
                patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(len(renewals), 1)
        self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')
        self.assertTrue(json.loads(path.read_text()) == renewed)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue((self.data / 'trakt.json').is_file())

    def test_refresh_failure_preserves_member_output_and_other_account_continues(self):
        self.configure_accounts()
        before = b'{"last-good":"synthetic"}\n'
        (self.data / 'trakt-ana.history.json').write_bytes(before)
        for person in ('ana', 'ben'):
            (self.credentials / f'trakt-{person}.json').write_text(json.dumps(
                oauth_bundle(access=CANARY + '_' + person, refresh=CANARY + '_' + person + '_refresh',
                             created=int(NOW) - 7200)))
        pages, calls = trakt_pages(), []
        def transport(req, timeout):
            if req.full_url.endswith('/oauth/token'):
                person = 'ana' if json.loads(req.data)['refresh_token'].endswith('_ana_refresh') else 'ben'
                calls.append(person)
                if person == 'ana':
                    raise urllib.error.HTTPError(req.full_url, 400, CANARY, {}, io.BytesIO(CANARY.encode()))
                return Reply(oauth_bundle(access=CANARY + '_ben_new', refresh=CANARY + '_ben_rotated'))
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY + '_secret'}), \
                patch.object(self.s, '_connector_open', transport), redirect_stdout(out), redirect_stderr(err):
            self.drain()
        accounts = self.s._connectors['trakt_api']['accounts']
        self.assertEqual(accounts['ana']['health'].get('fixHint'), 're-run skald trakt login --member ana')
        self.assertEqual(accounts['ana']['last_error'], 'TraktLoginRequired')
        self.assertEqual((self.data / 'trakt-ana.history.json').read_bytes(), before)
        self.assertEqual(accounts['ben']['health']['state'], 'ok')
        self.assertNotIn('last_error', accounts['ben'])
        self.assertEqual(calls, ['ana', 'ben'])
        public = json.dumps(self.s.connectors_view(now=NOW)) + (self.data / 'connectors.json').read_text()
        token = self.s.new_client('renewal-test', ['read'])
        code, _, raw, _ = raw_request(self.s, '/api/v1/connectors', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(code, 200)
        public += raw.decode() + out.getvalue() + err.getvalue()
        self.assertNotIn(CANARY, public)
        cli = io.StringIO()
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(main(['--snapshot', str(self.data), '--state-dir', state, 'connectors', '--person', 'ana'], out=cli), 0)
        self.assertIn('re-run skald trakt login --member ana', cli.getvalue())
        self.assertNotIn(CANARY, cli.getvalue())
        self.s.backup_state(NOW)
        archive = self.data / 'backups' / f'state-{self.s.online_day(NOW)[0]}.tar.gz'
        with tarfile.open(archive) as backup:
            for entry in backup:
                self.assertNotIn(CANARY.encode(), backup.extractfile(entry).read())

    def test_unexpired_bundle_renews_once_on_401(self):
        self.configure('trakt_api', auth='file:renew.json')
        (self.credentials / 'renew.json').write_text(json.dumps(oauth_bundle()))
        pages, renewals = trakt_pages(), []
        def transport(req, timeout):
            if req.full_url.endswith('/oauth/token'):
                renewals.append(1)
                return Reply(oauth_bundle(access='fake-renewed'))
            if req.get_header('Authorization') != 'Bearer fake-renewed':
                raise urllib.error.HTTPError(req.full_url, 401, CANARY, {}, None)
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY}), \
                patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(len(renewals), 1)
        self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')

    def test_member_accounts_renew_independently(self):
        self.configure_accounts()
        for person in ('ana', 'ben'):
            (self.credentials / f'trakt-{person}.json').write_text(json.dumps(
                oauth_bundle(access='fake-' + person, refresh='fake-' + person + '-refresh',
                             created=int(NOW) - 7200)))
        pages, renewals, polls = trakt_pages(), [], []
        def transport(req, timeout):
            if req.full_url.endswith('/oauth/token'):
                person = json.loads(req.data)['refresh_token'].split('-')[1]
                renewals.append(person)
                return Reply(oauth_bundle(access='renewed-' + person, refresh='rotated-' + person))
            polls.append(req.get_header('Authorization'))
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY}), \
                patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(renewals, ['ana', 'ben'])
        self.assertEqual(polls, ['Bearer renewed-ana'] * 4 + ['Bearer renewed-ben'] * 4)
        for person in ('ana', 'ben'):
            self.assertEqual(json.loads((self.credentials / f'trakt-{person}.json').read_text())['access_token'], 'renewed-' + person)
            self.assertTrue((self.data / f'trakt-{person}.history.json').is_file())

    def test_old_access_token_file_works_until_rejected_then_requests_login(self):
        self.configure('trakt_api', auth='file:old.token')
        (self.credentials / 'old.token').write_text(CANARY + '\n')
        pages = trakt_pages()
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id'}), \
                patch.object(self.s, '_connector_open', lambda req, timeout: Reply(pages[urllib.parse.urlsplit(req.full_url).path])):
            self.drain()
        output = self.data / 'trakt.json'
        before, stamp = output.read_bytes(), output.stat().st_mtime_ns
        self.s._connectors['trakt_api']['next_due'] = NOW + 7200
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id'}), \
                patch.object(self.s, '_connector_open', side_effect=urllib.error.HTTPError('https://api.trakt.tv', 401, CANARY, {}, None)):
            self.drain(NOW + 7200)
        self.assertEqual(self.s._connectors['trakt_api']['health']['fixHint'], 're-run skald trakt login --member ana')
        self.assertEqual((output.read_bytes(), output.stat().st_mtime_ns), (before, stamp))

    def test_malformed_refresh_bundle_keeps_credentials_and_last_good_output(self):
        self.configure('trakt_api', auth='file:renew.json')
        path = self.credentials / 'renew.json'
        previous = json.dumps(oauth_bundle(created=int(NOW) - 7200))
        output = self.data / 'trakt.json'
        output.write_bytes(b'{"last-good":true}\n')
        for response in (None, [], {'access_token': CANARY}, oauth_bundle(expires=True),
                         oauth_bundle(access=CANARY + '\n'), oauth_bundle(refresh='x' * 65536)):
            with self.subTest(shape=type(response).__name__):
                path.write_text(previous)
                self.s._connectors['trakt_api']['next_due'] = NOW
                with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY}), \
                        patch.object(self.s, '_connector_open', lambda req, timeout: Reply(response)):
                    self.drain()
                self.assertTrue(path.read_text() == previous)
                self.assertEqual(output.read_bytes(), b'{"last-good":true}\n')
                self.assertEqual(self.s._connectors['trakt_api']['health']['fixHint'], 're-run skald trakt login --member ana')

    def test_refresh_rate_limit_keeps_shared_backoff(self):
        self.configure_accounts()
        for person in ('ana', 'ben'):
            (self.credentials / f'trakt-{person}.json').write_text(json.dumps(oauth_bundle(created=int(NOW) - 7200)))
        calls = []
        def transport(req, timeout):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 429, CANARY, {'Retry-After': '7200'}, None)
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY}), \
                patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(len(calls), 1)
        accounts = self.s._connectors['trakt_api']['accounts']
        self.assertEqual(accounts['ana']['health']['fixHint'], 're-run skald trakt login --member ana')
        self.assertEqual(accounts['ben']['last_error'], 'RateLimited')
        self.assertTrue(all(a['next_due'] >= NOW + 7200 for a in accounts.values()))

    def test_connectors_reports_health_per_account_and_cli_person_scope(self):
        self.configure_accounts()
        view = self.s.connectors_view(now=NOW)['connectors']
        accounts = [c for c in view if c['id'].startswith('trakt_api:')]
        self.assertEqual({c['person'] for c in accounts}, {'ana', 'ben'})
        self.assertTrue(all(c['health']['state'] == 'ok' and c['auth'] == 'ref' for c in accounts))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as state, redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--snapshot', str(self.data), '--state-dir', state, '--json',
                                   'connectors', '--person', 'ben'], out=out), 0)
        accounts = [c for c in json.loads(out.getvalue())['connectors'] if c['id'].startswith('trakt_api:')]
        self.assertEqual([c['person'] for c in accounts], ['ben'])
        self.assertNotIn('fake-ben', out.getvalue())

    def test_one_tick_polls_all_accounts_sequentially_into_separate_outputs(self):
        self.configure_accounts()
        pages, calls = trakt_pages(), []
        def transport(req, timeout):
            person = req.get_header('Authorization').removeprefix('Bearer fake-')
            calls.append(person)
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(calls, ['ana']*4 + ['ben']*4)
        self.assertFalse((self.data/'trakt.json').exists())
        for person in ('ana', 'ben'):
            self.assertTrue(json.loads((self.data/f'trakt-{person}.history.json').read_text())['items'])
        accounts = [c for c in self.s.connectors_view(now=NOW)['connectors'] if c['id'].startswith('trakt_api:')]
        self.assertTrue(all(c['counts']['polls'] == 1 and c['last_sync'] is not None for c in accounts))

    def drain(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            self.s.connectors_tick(now)
            if self.s._connector_pending:
                try:
                    self.s._connector_pending[2].result(timeout=3)
                except Exception:
                    pass
                self.s.connectors_tick(now+.01)

    def test_account_failure_preserves_output_health_and_other_account_updates(self):
        self.configure_accounts()
        pages = trakt_pages()
        def transport(req, timeout):
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', transport):
            self.drain()
        prior = copy.deepcopy(self.s._connectors['trakt_api']['accounts']['ana'])
        path = self.data/'trakt-ana.history.json'
        content, stamp = path.read_bytes(), path.stat().st_mtime_ns
        self.s._connectors['trakt_api']['next_due'] = NOW+3600
        calls = []
        def fail_one(req, timeout):
            person = req.get_header('Authorization').removeprefix('Bearer fake-')
            calls.append(person)
            if person == 'ana':
                raise urllib.error.HTTPError(req.full_url, 401, CANARY, {}, None)
            return transport(req, timeout)
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', fail_one):
            self.drain(NOW+3600)
        self.assertEqual(calls, ['ana']+['ben']*4)
        current = self.s._connectors['trakt_api']['accounts']
        self.assertEqual(current['ana']['health'], prior['health'] | {'fixHint': 're-run skald trakt login --member ana'})
        self.assertEqual(current['ana']['cursor'], prior['cursor'])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(current['ben']['counts']['polls'], 2)
        view = next(c for c in self.s.connectors_view(now=NOW+3600)['connectors'] if c['person'] == 'ana' and c['kind'] == 'api')
        self.assertEqual(view['last_error'], 'TraktLoginRequired')
        self.assertNotIn(CANARY, json.dumps(self.s.connectors_view(now=NOW+3600)))

    def test_accounts_share_http_page_budget_and_rate_limit_backoff(self):
        self.configure_accounts()
        pages, calls = trakt_pages(), []
        def transport(req, timeout):
            calls.append(req.get_header('Authorization'))
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), \
                patch.object(self.s, '_connector_open', transport), patch.object(self.s, 'CONNECTOR_HTTP_PAGES', 5):
            self.drain()
        self.assertEqual(len(calls), 5)
        accounts = self.s._connectors['trakt_api']['accounts']
        self.assertEqual(accounts['ben']['last_error'], 'PageCapExceeded')
        self.assertFalse((self.data/'trakt-ben.history.json').exists())
        self.s._connectors['trakt_api']['next_due'] = NOW+3600
        calls.clear()
        def limited(req, timeout):
            calls.append(req.get_header('Authorization'))
            raise urllib.error.HTTPError(req.full_url, 429, CANARY, {'Retry-After': '7200'}, None)
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', limited):
            self.drain(NOW+3600)
        self.assertEqual(calls, ['Bearer fake-ana'])
        self.assertGreaterEqual(self.s._connectors['trakt_api']['next_due'], NOW+10800)
        for account in self.s._connectors['trakt_api']['accounts'].values():
            self.assertEqual(account['next_due'], self.s._connectors['trakt_api']['next_due'])

    def test_member_b_ratings_never_enter_member_a_model_interests_or_rows(self):
        self.configure_accounts()
        item = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Private B rating', 'year': 1999,
                'person': 'ben', 'signal': 'interest', 'rating': 5, 'weight': 1, 'genres': ['action'], 'at': NOW*1000}
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], [item])
        self.assertEqual(self.s.person_entries(profile, 'ana'), [])
        self.assertEqual([e['title'] for e in self.s.person_entries(profile, 'ben')], ['Private B rating'])
        self.s._facets['tmdb:603'] = {'kind': 'movie', 'title': 'Private B rating', 'g': ['Action'],
                                     'x': {'competence': 3}, 'd': [[123, 'Private B creator']]}
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), redirect_stdout(io.StringIO()):
            model = self.s.build_model(profile)
        self.assertEqual(model['paff']['ana'], {})
        self.assertTrue(model['paff']['ben'])
        self.s._profiles['household'], self.s._model['m'] = profile, model
        self.assertEqual(self.s.profile_view({'viewer': 'ana', 'query': {}})['titles'], 0)
        self.assertEqual(self.s.explicit_seeds('ana'), [])
        self.s._feeds[(rid('rewatch', self.s.CONFIG), self.s.LANG)] = {'metas': {'tv': [
            {'id': 'tmdb:603', 'type': 'movie', 'name': 'Private B rating', 'releaseInfo': '1999',
             'poster': 'fixture-poster', 'background': 'fixture-background'}]}}
        query = {'row': rid('rewatch', self.s.CONFIG)}
        self.assertEqual(self.s.rows_view({'viewer': 'ana', 'query': query})['rows'][0]['items'], [])
        self.assertEqual(self.s.rows_view({'viewer': 'ben', 'query': query})['rows'][0]['items'][0]['title'], 'Private B rating')

    def test_each_account_output_is_read_only_as_its_configured_member(self):
        self.configure_accounts()
        for person, tid in (('ana', 603), ('ben', 605)):
            self.s._connector_write('trakt-'+person+'.history', {'items': [{'id': 'tmdb:'+str(tid), 'type': 'movie',
                'title': person+' private title', 'person': 'ana', 'signal': 'interest', 'weight': 1}]})
        self.s._connector_write('trakt', {'items': [{'id': 'tmdb:999', 'type': 'movie', 'title': 'old migration data'}]})
        items = self.s.read_trakt()
        self.assertEqual([(it['id'], it['person']) for it in items], [('tmdb:603', 'ana'), ('tmdb:605', 'ben')])
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        self.assertEqual(self.s._connectors['trakt']['health']['state'], 'disabled')

    def test_scoped_watch_again_excludes_another_accounts_watch(self):
        self.configure_accounts()
        item = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Only Ben watched', 'year': 1999,
                'person': 'ben', 'signal': 'watched', 'weight': 2, 'at': (NOW-365*86400)*1000}
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], [item])
            self.s._model['m'] = self.s.build_model(profile)
        self.s._profiles['household'] = profile
        self.s._feeds[(rid('rewatch', self.s.CONFIG), self.s.LANG)] = {'metas': {'tv': [
            {'id': 'tmdb:603', 'type': 'movie', 'name': item['title'], 'releaseInfo': '1999',
             'poster': 'fixture-poster', 'background': 'fixture-background'}]}}
        token = self.s.addon_token()
        def catalog(person):
            path = f"/{token}/{self.s.LANG}/p/{person}/catalog/tv/{rid('rewatch', self.s.CONFIG)}.json"
            status, _, raw, _ = raw_request(self.s, path, handler=self.s.Handler)
            self.assertEqual(status, 200)
            return [meta['id'] for meta in json.loads(raw)['metas']]
        self.assertEqual(catalog('ana'), [])
        self.assertEqual(catalog('ben'), ['tmdb:603'])
        self.assertEqual(catalog('together'), ['tmdb:603'])

    def test_same_title_account_opinions_remain_personal(self):
        self.configure_accounts()
        base = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Same title', 'weight': 2, 'at': NOW*1000}
        items = [{**base, 'person': 'ana', 'signal': 'watched'},
                 {**base, 'person': 'ben', 'signal': 'loved', 'weight': 4}]
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], items)
        self.assertEqual(self.s.person_entries(profile, 'ana')[0]['signal'], 'watched')
        self.assertEqual(self.s.person_entries(profile, 'ana')[0]['weight'], 2)
        self.assertEqual(self.s.person_entries(profile, 'ben')[0]['signal'], 'loved')
        self.assertEqual(self.s.person_entries(profile, 'ben')[0]['weight'], 4)

    def test_personal_explanation_never_anchors_on_another_accounts_love(self):
        self.configure_accounts()
        base = {'type': 'movie', 'year': 1999, 'at': NOW*1000}
        items = [{**base, 'id': 'tmdb:603', 'title': 'Disliked X', 'person': 'ana', 'signal': 'disliked', 'weight': -1},
                 {**base, 'id': 'tmdb:603', 'title': 'Disliked X', 'person': 'ben', 'signal': 'loved', 'weight': 4},
                 {**base, 'id': 'tmdb:602', 'title': 'Loved Y', 'person': 'ana', 'signal': 'loved', 'weight': 4}]
        for key, title in (('tmdb:603', 'Disliked X'), ('tmdb:602', 'Loved Y'), ('tmdb:999', 'Candidate')):
            self.s._facets[key] = {'kind': 'movie', 'title': title, 'd': [[123, 'Director X']]}
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], items)
            self.s._model['m'] = self.s.build_model(profile)
        self.s._profiles['household'] = profile
        ana = self.s.title_view('tmdb:999', 'movie', 'ana')['reasons']
        self.assertTrue(ana)
        self.assertFalse(any('like Disliked X' in reason for reason in ana), ana)
        self.assertTrue(any('like Loved Y' in reason for reason in ana), ana)
        ben = self.s.title_view('tmdb:999', 'movie', 'ben')['reasons']
        self.assertTrue(any('like Disliked X' in reason for reason in ben), ben)

    def test_another_accounts_recent_watch_preserves_own_age_and_metadata(self):
        self.configure_accounts()
        base = {'id': 'tmdb:1419', 'type': 'series', 'title': 'Shared series', 'year': 2009,
                'signal': 'watched', 'weight': 2}
        ana = {**base, 'person': 'ana', 'at': (NOW-365*86400)*1000, 'genres': ['comedy'],
               'episodes_played': 4, 'last_episode': [1, 4], 'flags': ['comfort']}
        ben = {**base, 'person': 'ben', 'at': NOW*1000, 'genres': ['action'],
               'episodes_played': 21, 'last_episode': [3, 1], 'flags': ['binge']}
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            before = self.s.build_profile([], [], [ana])
            after = self.s.build_profile([], [], [ana, ben])
        fields = ('age', 'signal', 'weight', 'item', 'flags', 'genres', 'title', 'year')
        previous = self.s.person_entries(before, 'ana')[0]
        current = self.s.person_entries(after, 'ana')[0]
        self.assertEqual({k: current[k] for k in fields}, {k: previous[k] for k in fields})
        own_b = self.s.person_entries(after, 'ben')[0]
        self.assertEqual(own_b['age'], 0)
        self.assertEqual(own_b['genres'], ['Action'])
        self.assertEqual(own_b['item']['last_episode'], [3, 1])
        self.assertEqual(after['entries'][0]['age'], 0)
        self.assertEqual(after['entries'][0]['item']['last_episode'], [3, 1])
        details = {'id': 1419, 'name': 'Shared series', 'first_air_date': '2009-01-01',
                   'genres': [{'name': 'Comedy'}]}
        with patch('time.time', return_value=NOW), patch.object(self.s, 'tmdb_details', return_value=('tv', details)), \
                redirect_stdout(io.StringIO()):
            before = self.s.build_profile([ana], [], [])
            after = self.s.build_profile([ana], [], [ben])
        previous = self.s.person_entries(before, 'ana')[0]
        current = self.s.person_entries(after, 'ana')[0]
        self.assertEqual({k: current[k] for k in fields}, {k: previous[k] for k in fields})
        self.assertEqual(self.s.person_entries(after, 'together')[0]['age'], 0)

    def test_snapshot_engine_copies_member_outputs_and_rebuilds_with_attribution(self):
        from skald.engine import Engine
        self.configure_accounts()
        self.s._connector_write('trakt-ben.history', {'items': [{'id': 'tmdb:603', 'type': 'movie', 'title': 'Only Ben',
            'signal': 'interest', 'weight': 1, 'genres': ['action']}]})
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            self.assertEqual(engine.profile('ana')['titles'], 0)
            self.assertEqual(engine.profile('ben')['titles'], 1)
            self.assertEqual(engine.profile('ben')['signals'], {'interest': 1})

    def test_snapshot_engine_excludes_member_output_credential_aliases_and_symlinks(self):
        from skald.engine import Engine
        self.configure_accounts()
        credential = self.credentials/'private.token'
        credential.write_text(json.dumps({'items': [], 'token': CANARY}))
        (self.data/'trakt-ana.history.json').symlink_to(credential)
        os.link(credential, self.data/'trakt-ben.history.json')
        with redirect_stdout(io.StringIO()), Engine(self.data) as engine:
            self.assertFalse((engine._work/'trakt-ben.history.json').exists(), 'credential alias copied')
            self.assertFalse((engine._work/'trakt-ana.history.json').exists(), 'symlink copied')

    def test_cli_snapshot_with_member_outputs_needs_no_credentials(self):
        from skald.engine import data_files
        self.configure_accounts()
        self.s._connector_write('trakt-ben.history', {'items': [{'id': 'tmdb:603', 'type': 'movie', 'title': 'Only Ben',
            'signal': 'interest', 'weight': 1, 'genres': ['action']}]})
        with tempfile.TemporaryDirectory() as snapshot, tempfile.TemporaryDirectory() as state:
            for name in data_files(self.s.CONFIG):
                source = self.data/name
                if source.is_file():
                    (Path(snapshot)/name).write_bytes(source.read_bytes())
            out = io.StringIO()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--snapshot', snapshot, '--state-dir', state, '--json',
                                       'taste', 'show', '--person', 'ben'], out=out), 0)
            self.assertEqual(json.loads(out.getvalue())['titles'], 1)

    def test_live_cli_copies_outputs_from_pulled_household_config_offline(self):
        import subprocess
        from unittest.mock import Mock
        from test_cli import LiveTests
        self.configure_accounts()
        item = {'items': [{'id': 'tmdb:603', 'type': 'movie', 'title': 'Only Ben',
                          'signal': 'interest', 'weight': 1, 'genres': ['action']}]}
        archive = LiveTests().archive({'skald.json': json.dumps(self.s.CONFIG), 'emb.json': '{}',
                                      'facets.json': '{}', 'trakt-ben.history.json': json.dumps(item)})
        (self.data/'skald.json').write_text(json.dumps(self.s.fresh_config(['cy'])))
        run = Mock(return_value=subprocess.CompletedProcess([], 0, archive, b''))
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '', 'SKALD_SSH_HOST': 'synthetic-host.test'}), \
                patch('subprocess.run', run), redirect_stderr(io.StringIO()):
            out = io.StringIO()
            self.assertEqual(main(['--live', '--state-dir', state, '--json', 'taste', 'show'], out=out), 0)
        self.assertEqual(json.loads(out.getvalue())['titles'], 1)

    def test_personal_scores_do_not_blend_other_accounts_ratings(self):
        self.configure_accounts()
        items = [{'id': 'tmdb:'+str(tid), 'type': 'movie', 'title': 'Ben rating', 'person': 'ben',
                  'signal': 'loved', 'weight': 4, 'genres': ['action']} for tid in range(603, 607)]
        for item in items:
            self.s._facets[item['id']] = {'kind': 'movie', 'title': 'Ben rating', 'd': [[123, 'Ben creator']]}
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], items)
            model = self.s.build_model(profile)
        self.assertGreater(self.s.person_score(model, 'tmdb:603')[0], 0)
        self.assertEqual(self.s.person_score(model, 'tmdb:603', 'ana')[0], 0)

    def test_account_rating_scores_shape_only_that_members_taste(self):
        self.configure_accounts()
        def transport(req, timeout):
            url = urllib.parse.urlsplit(req.full_url)
            rows = []
            if url.path == '/sync/ratings':
                rating = 3 if req.get_header('Authorization') == 'Bearer fake-ana' else 9
                rows = [{'movie': {'title': 'Same rated title', 'year': 1999, 'ids': {'tmdb': 603}, 'genres': ['action']},
                         'rating': rating, 'rated_at': '2026-10-11T00:00:00Z'}]
            return Reply(rows)
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', transport):
            self.drain()
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], self.s.read_trakt())
        self.assertEqual(self.s.person_entries(profile, 'ana')[0]['signal'], 'disliked')
        self.assertLess(self.s.person_entries(profile, 'ana')[0]['weight'], 0)
        self.assertEqual(self.s.person_entries(profile, 'ben')[0]['signal'], 'loved')
        self.assertFalse(self.s.real_play(self.s.person_entries(profile, 'ben')[0]))
        for person, rating in (('ana', 3), ('ben', 9)):
            entry = self.s.person_entries(profile, person)[0]
            self.assertTrue(entry['explicit'])
            self.assertEqual(entry['item']['rating'], rating)
            self.assertEqual(entry['item']['rated_at'], '2026-10-11T00:00:00Z')

    def test_legacy_api_ratings_survive_profile_fold(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        movie = lambda tid: {'title': 'Synthetic movie', 'year': 2020, 'ids': {'tmdb': tid}, 'genres': ['action']}
        stamp = '2026-10-11T00:00:00Z'
        pages = {'/sync/history': [{'id': tid, 'movie': movie(tid), 'watched_at': stamp} for tid in (603, 604)],
                 '/sync/watched/movies': [{'movie': movie(tid), 'plays': 1} for tid in (603, 604)],
                 '/sync/watched/shows': [],
                 '/sync/ratings': [{'movie': movie(603), 'rating': 9, 'rated_at': stamp},
                                   {'show': {'title': 'Synthetic show', 'ids': {'tmdb': 1419}},
                                    'rating': 3, 'rated_at': stamp}]}
        with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': 'synthetic', 'TRAKT_CLIENT_ID': 'synthetic'}), \
                patch.object(self.s, '_connector_open', lambda req, timeout: Reply(pages[urllib.parse.urlsplit(req.full_url).path])):
            self.drain()
        with patch('time.time', return_value=NOW), redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], self.s.read_trakt())
        entries = {e['key']: e for e in self.s.person_entries(profile, 'ana')}
        for key, rating, signal, weight in (('tmdb:603', 9, 'loved', 3), ('tmdb:1419', 3, 'disliked', -3)):
            with self.subTest(rating=rating):
                self.assertEqual((entries[key]['signal'], entries[key]['weight']), (signal, weight))
                self.assertTrue(entries[key]['explicit'])
                self.assertEqual(entries[key]['item']['rating'], rating)
                self.assertEqual(entries[key]['item']['rated_at'], stamp)
        self.assertEqual((entries['tmdb:604']['signal'], entries['tmdb:604']['weight']), ('watched', 2))
        self.assertNotIn('rating', entries['tmdb:604']['item'])

    def test_rating_changes_invalidate_household_profile_even_with_same_signal(self):
        self.configure_accounts()
        self.s._connector_write('trakt-ben.history', {'items': [{'id': 'tmdb:603', 'type': 'movie',
                                                       'signal': 'watched', 'weight': 2}]})
        before = self.s.taste_hash('household', [])
        self.s._connector_write('trakt-ben.history', {'items': [{'id': 'tmdb:603', 'type': 'movie',
                                                       'signal': 'watched', 'weight': .3}]})
        self.assertNotEqual(self.s.taste_hash('household', []), before)

    def test_legacy_trakt_ignores_item_person_field(self):
        item = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Legacy title', 'person': 'ben',
                'signal': 'watched', 'weight': 2}
        with redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], [item])
        self.assertEqual(profile['entries'][0]['by'], ['ana'])
        self.assertTrue(self.s.real_play({'who': 'tv', 'signal': 'loved', 'item': {'rating': 9}}))
        # Legacy Trakt opinions need watch evidence just like per-member ratings.
        self.assertFalse(self.s.real_play({'who': 'owner', 'wsrc': 'trakt', 'signal': 'loved', 'item': {'rating': 9}}))

    def test_profile_prompt_names_the_account_owner_for_each_log(self):
        self.configure_accounts()
        item = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Ben title', 'person': 'ben',
                'signal': 'watched', 'weight': 2}
        self.s.LABELS = self.s.CONFIG['household']['labels']
        with redirect_stdout(io.StringIO()):
            profile = self.s.build_profile([], [], [item])
        text = self.s.profile_text(profile)
        self.assertIn('Ben keeps a long-term watch log', text)
        self.assertNotIn('Ana keeps a long-term watch log', text)

    def test_connectors_text_shows_last_account_poll_failure(self):
        c = self.configure_accounts()
        c['accounts']['ben']['last_error'] = 'HTTPError'
        self.s._connectors_save(self.s._connectors, self.s._connectors_generation)
        with tempfile.TemporaryDirectory() as state, redirect_stderr(io.StringIO()):
            out = io.StringIO()
            self.assertEqual(main(['--snapshot', str(self.data), '--state-dir', state, 'connectors'], out=out), 0)
        line = next(line for line in out.getvalue().splitlines() if line.startswith('trakt_api:ben'))
        self.assertIn('HTTPError', line)

    def test_tick_retains_health_reporting_when_executor_cannot_submit(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        self.s._connector_pool.shutdown(wait=True)
        with redirect_stdout(io.StringIO()):
            self.s.connectors_tick(NOW)
        self.assertEqual(self.s._connectors['trakt_api']['health']['error'], 'RuntimeError')

    def test_corrupt_persisted_account_error_is_not_displayed(self):
        self.configure_accounts()
        path = self.data/'connectors.json'
        doc = json.loads(path.read_text())
        doc['connectors']['trakt_api']['accounts']['ben']['last_error'] = 'private response: '+CANARY
        path.write_text(json.dumps(doc))
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
        self.assertNotIn(CANARY, json.dumps(self.s.connectors_view(now=NOW)))

    def test_backups_include_member_outputs_without_credentials(self):
        from skald import backup
        self.configure_accounts()
        for person in ('ana', 'ben'):
            self.s._connector_write('trakt-'+person+'.history', {'items': []})
        with redirect_stdout(io.StringIO()):
            self.s.backup_state(NOW)
        archive = next((self.data/'backups').glob('state-*.tar.gz'))
        with tarfile.open(archive) as tar:
            self.assertTrue({'trakt-ana.history.json', 'trakt-ben.history.json'} <= set(tar.getnames()))
            self.assertFalse(any('credentials' in name for name in tar.getnames()))
        self.assertTrue(backup.verify(archive)['ok'])

    def test_doctor_counts_failed_accounts_while_retaining_successful_health(self):
        c = self.configure_accounts()
        c['accounts']['ben']['last_error'] = 'HTTPError'
        self.s._connectors_save(self.s._connectors, self.s._connectors_generation)
        with tempfile.TemporaryDirectory() as state, redirect_stderr(io.StringIO()):
            out = io.StringIO()
            main(['--snapshot', str(self.data), '--state-dir', state, '--json', 'doctor'], out=out)
        finding = next(f for f in json.loads(out.getvalue())['findings'] if f['checkId'] == 'connectors.health')
        self.assertEqual(finding['severity'], 'warning')
        self.assertIn('1 connector errors', finding['message'])

    def transport(self, req, timeout):
        self.assertEqual(timeout, 10)
        self.assertEqual(req.get_header('Trakt-api-version'), '2')
        self.assertEqual(req.get_header('Trakt-api-key'), 'fake-client')
        self.assertEqual(req.get_header('Authorization'), 'Bearer '+CANARY)
        url = urllib.parse.urlsplit(req.full_url)
        page = int(urllib.parse.parse_qs(url.query)['page'][0])
        rows = self.pages[url.path]
        response = Reply(rows[(page-1)*100:page*100], pages=max(1, (len(rows)+99)//100))
        response.headers['X-Pagination-Item-Count'] = str(len(rows))
        return response

    def test_sqlite_export_bytes_series_archived_favorites_and_cap(self):
        options = watch_db(self.data/'watch.sqlite', cap=True)
        c = self.configure('aiostreams_db', options=options)
        reference = offline_importer().export_history(options['path'], options['profiles'])
        self.s.poll(c, NOW)
        actual = (self.data/'history.json').read_bytes()
        self.assertEqual(actual, reference)
        items = json.loads(actual)['items']
        self.assertEqual(len(items), 120)
        self.assertTrue(items[0]['favorite'])
        self.assertEqual(items[0]['episodes_played'], 2)
        self.assertEqual(items[0]['resume_episode'], [2, 1])
        self.assertEqual(items[0]['last_episode'], [1, 2])
        self.assertFalse(any(it['profile']=='retired' for it in items))
        self.assertNotIn('resume_episode', items[2])
        stamp = (self.data/'history.json').stat().st_mtime_ns
        with patch.object(self.s.os, 'replace', side_effect=AssertionError('unchanged output rewritten')):
            self.s.poll(c, NOW+1)
        self.assertEqual((self.data/'history.json').stat().st_mtime_ns, stamp)
        self.assertEqual(stat.S_IMODE((self.data/'history.json').stat().st_mode), 0o644)
        self.assertEqual(hashlib.sha256(actual).hexdigest(), hashlib.sha256(reference).hexdigest())

    def test_schema_and_read_only_open_failure_preserve_history(self):
        path = self.data/'broken.sqlite'
        with sqlite3.connect(path) as con:
            con.execute('create table watch_state (uuid text)')
        options = {'path': str(path), 'profiles': {'a1b2c3d4': 'household'}}
        self.configure('aiostreams_db', options=options)
        old = b'{"items": []}'
        (self.data/'history.json').write_bytes(old)
        self.drain()
        self.assertEqual(self.s._connectors['aiostreams_db']['health']['error'], 'SchemaMismatch')
        self.assertEqual((self.data/'history.json').read_bytes(), old)
        self.s._connectors['aiostreams_db']['next_due'] = NOW+1
        with patch.object(self.s.sqlite3, 'connect', side_effect=sqlite3.OperationalError(CANARY)) as connect:
            self.drain(NOW+1)
        self.assertEqual(connect.call_count, 1)
        self.assertIn('mode=ro', connect.call_args.args[0])
        self.assertTrue(connect.call_args.kwargs['uri'])
        self.assertEqual(self.s._connectors['aiostreams_db']['health']['error'], 'OperationalError')
        self.assertEqual((self.data/'history.json').read_bytes(), old)

    def test_live_wal_transaction_reads_committed_state_or_preserves_prior_on_error(self):
        path = self.data/'watch.sqlite'
        options = watch_db(path)
        self.configure('aiostreams_db', options=options)
        with sqlite3.connect(path) as writer:
            self.assertEqual(writer.execute('pragma journal_mode=wal').fetchone()[0], 'wal')
            writer.execute('pragma wal_autocheckpoint=0')
            writer.execute('update watch_state set favorite=1 where base_id=?', ('tmdb:605',))
            writer.commit()
            # The live writer owns both sidecars before either read-only query.
            sidecars = {p.name for p in self.data.glob('watch.sqlite*')}
            self.assertEqual(sidecars, {'watch.sqlite', 'watch.sqlite-wal', 'watch.sqlite-shm'})
            committed = self.s._aiostreams_db(self.s.CONFIG['connectors']['aiostreams_db']['options'])
            self.assertEqual({p.name for p in self.data.glob('watch.sqlite*')}, sidecars)
            self.s._connector_write('history', committed)
            prior = (self.data/'history.json').read_bytes()
            stamp = (self.data/'history.json').stat().st_mtime_ns
            writer.execute('begin immediate')
            writer.execute('update watch_state set base_id=?', ('tmdb:999999',))
            wal = Path(str(path)+'-wal')
            self.assertTrue(wal.is_file())
            database_bytes, wal_bytes = path.read_bytes(), wal.read_bytes()
            connect = sqlite3.connect
            def read_only(uri, **kwargs):
                self.assertIn('?mode=ro', uri)
                self.assertTrue(kwargs['uri'])
                return connect(uri, **kwargs)
            with patch.object(self.s.sqlite3, 'connect', side_effect=read_only):
                self.drain()
            health = self.s._connectors['aiostreams_db']['health']
            self.assertIn(health['state'], ('ok', 'error'))
            if health['state'] == 'error':
                self.assertIsNotNone(health['error'])
            else:
                self.assertEqual(json.loads((self.data/'history.json').read_text()), committed)
            self.assertEqual((self.data/'history.json').read_bytes(), prior)
            self.assertEqual((self.data/'history.json').stat().st_mtime_ns, stamp)
            self.assertEqual(path.read_bytes(), database_bytes)
            self.assertEqual(wal.read_bytes(), wal_bytes)
            self.assertEqual({p.name for p in self.data.glob('watch.sqlite*')}, sidecars)
            self.assertTrue(writer.in_transaction)
            writer.rollback()

    def test_path_outside_configuration_and_symlink_refused(self):
        options = watch_db(self.data/'watch.sqlite')
        self.configure('aiostreams_db', options=options)
        with self.assertRaises(self.s.PathMismatch):
            self.s._aiostreams_db({**options, 'path': str(self.data/'other.sqlite')})
        link = self.data/'link.sqlite'
        link.symlink_to(self.data/'watch.sqlite')
        self.s.CONFIG['connectors']['aiostreams_db']['options']['path'] = str(link)
        with self.assertRaises(self.s.PathMismatch):
            self.s._aiostreams_db(self.s.CONFIG['connectors']['aiostreams_db']['options'])

    def test_mixed_case_prefixes_match_known_uuids_and_ignore_unknown_uuids(self):
        options = watch_db(self.data/'watch.sqlite')
        options['profiles'] = {'A1b2C3d4': 'household', 'B1B2C3D4': 'ana'}
        options['archived'] = {'C1B2C3D4': 'retired'}
        with sqlite3.connect(options['path']) as con:
            con.execute('update watch_state set uuid=upper(uuid) where uuid like ?', ('a1b2c3d4%',))
            con.execute("insert into watch_state select 'unknown1-device', media_type, 'tmdb:999999', last_played_at, "
                        "played, kind, favorite, duration_ms, position_ms, rating, likes, dropped, season, episode "
                        "from watch_state limit 1")
        self.configure('aiostreams_db', options=options)
        self.drain()
        self.assertEqual(self.s._connectors['aiostreams_db']['health']['state'], 'ok')
        self.assertEqual(self.s.CONFIG['connectors']['aiostreams_db']['options']['profiles'],
                         {'a1b2c3d4': 'household', 'b1b2c3d4': 'ana'})
        items = json.loads((self.data/'history.json').read_text())['items']
        self.assertEqual({it['id'] for it in items}, {'tmdb:1419', 'tmdb:1420', 'tmdb:603', 'tmdb:605'})
        self.assertEqual({it['profile'] for it in items}, {'household', 'ana'})

    def test_colliding_normalized_prefixes_rejected_in_both_config_loaders(self):
        options = watch_db(self.data/'watch.sqlite')
        for target in ('profiles', 'archived'):
            cfg = copy.deepcopy(self.s.CONFIG)
            cfg['connectors']['aiostreams_db'] = {'kind': 'player', 'enabled': True, 'auth': None, 'options': copy.deepcopy(options)}
            cfg['connectors']['aiostreams_db']['options'][target]['A1B2C3D4'] = 'ana'
            with self.subTest(target=target):
                for loader in (members, self.s):
                    with self.assertRaises(loader.ConfigError):
                        loader.validate_config(cfg)

    def test_atomic_writer_repairs_invalid_bytes_and_preserves_old_output_on_failure(self):
        path = self.data/'history.json'
        path.write_bytes(b'\xffbroken')
        self.assertTrue(self.s._connector_write('history', {'items': []}))
        before = path.read_bytes()
        with patch.object(self.s.os,'replace',side_effect=OSError(CANARY)):
            with self.assertRaises(OSError):
                self.s._connector_write('history', {'items': [{'id':'tmdb:1'}]})
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(list(self.data.glob('.history-*')))

    def test_trakt_api_offline_importer_byte_parity_and_auth_canaries(self):
        self.pages = trakt_pages()
        # Watch inference keeps byte parity; explicit API ratings have their own fold tests.
        self.pages['/sync/ratings'] = []
        for reference in ('env:FAKE_TRAKT_TOKEN', 'file:fake-trakt.token'):
            with self.subTest(reference=reference):
                (self.credentials/'fake-trakt.token').write_text(CANARY+'\n')
                c = self.configure('trakt_api', auth=reference)
                c.update(next_due=NOW, cursor=None)
                logs = io.StringIO()
                with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                     patch.object(self.s, '_connector_open', self.transport), redirect_stdout(logs), redirect_stderr(logs):
                    self.drain()
                    view = self.s.connectors_view()
                    token = self.s.new_client('fixture-'+reference.split(':')[0], ['read'], 'ana')
                    code, _, api, _ = raw_request(self.s, '/api/v1/connectors', headers={'Authorization': 'Bearer '+token})
                    self.assertEqual(code, 200)
                offline = offline_importer()
                offline.write_atomic(self.data/'reference-trakt.json', offline.import_history(trakt_web_export(self.pages), NOW))
                actual = (self.data/'trakt.json').read_bytes()
                self.assertEqual(actual, (self.data/'reference-trakt.json').read_bytes())
                self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')
                (self.data/'skald.json').write_text(json.dumps(self.s.CONFIG))
                cli = io.StringIO()
                with redirect_stdout(logs), redirect_stderr(logs):
                    self.assertEqual(main(['--snapshot',str(self.data),'--state-dir',str(self.data.parent/'fake-state'),
                                           '--json','connectors'], out=cli), 0)
                for raw in ((self.data/'connectors.json').read_text(), actual.decode(), json.dumps(view), api.decode(), logs.getvalue(), cli.getvalue(),
                            json.dumps(self.s.public_config(self.s.CONFIG))):
                    self.assertNotIn(CANARY, raw)
                self.assertFalse((self.data/'history.json').exists())
                self.assertEqual(self.s._connectors['history']['health']['state'], 'disabled')

    def test_pure_transform_all_offline_rules_remain_equal(self):
        offline = offline_importer()
        data = trakt_web_export(trakt_pages())
        data['movies'].append({'tmdb': 1, 'title': 'Anime', 'genres': ['animation'], 'lang': 'ja', 'plays': [int(NOW//60)-2, int(NOW//60)]})
        data['shows'].extend([{'tmdb': 2, 'title': 'Returning', 'eps_total': 3, 'status': 'returning series',
                              'eps': [[1, i, int(NOW//60)+i*30, 24] for i in range(1,4)]},
                             {'tmdb': 3, 'title': 'Stopped', 'eps_total': 20, 'eps': [[1,1,int(NOW//60)-90*1440,24]]}])
        self.assertEqual(self.s._trakt_import_history(data, NOW), offline.import_history(data, NOW))

    def test_incomplete_or_inconsistent_trakt_pages_preserve_previous_output(self):
        complete = trakt_pages()
        cases = {}
        cases['empty history'] = {**complete, '/sync/history': []}
        cases['truncated history'] = {**complete, '/sync/history': complete['/sync/history'][:-1]}
        cases['empty watched movies'] = {**complete, '/sync/watched/movies': []}
        cases['truncated watched shows'] = {**complete, '/sync/watched/shows': complete['/sync/watched/shows'][:-1]}
        cases['missing replay'] = {**complete, '/sync/history': complete['/sync/history'][:1]+complete['/sync/history'][2:]}
        cases['duplicate history id'] = {**complete, '/sync/history': complete['/sync/history']+[complete['/sync/history'][1]]}
        cases['changed movie summary'] = copy.deepcopy(complete)
        cases['changed movie summary']['/sync/watched/movies'][0]['plays'] = 1
        cases['changed episode summary'] = copy.deepcopy(complete)
        cases['changed episode summary']['/sync/watched/shows'][0]['seasons'][0]['episodes'][0]['plays'] = 3
        old = b'{"items": [], "prior": true}\n'
        for name, pages in cases.items():
            with self.subTest(name=name):
                self.pages = pages
                self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
                self.s._connectors['trakt_api']['next_due'] = NOW
                path = self.data/'trakt.json'
                path.write_bytes(old)
                stamp = path.stat().st_mtime_ns
                with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                     patch.object(self.s, '_connector_open', self.transport):
                    self.drain()
                self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'error')
                self.assertEqual(path.read_bytes(), old)
                self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_trakt_polling_401_preserves_previous_output_and_sanitizes_health(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        path = self.data/'trakt.json'
        path.write_bytes(b'{"prior": true}')
        stamp = path.stat().st_mtime_ns
        logs = io.StringIO()
        with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
             patch.object(self.s, '_connector_open', side_effect=urllib.error.HTTPError('https://api.trakt.tv', 401, CANARY, {}, None)), \
             redirect_stdout(logs), redirect_stderr(logs):
            self.drain()
        self.assertEqual(self.s._connectors['trakt_api']['health']['error'], 'TraktLoginRequired')
        self.assertEqual(self.s._connectors['trakt_api']['health']['fixHint'], 're-run skald trakt login --member ana')
        self.assertEqual(path.read_bytes(), b'{"prior": true}')
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertNotIn(CANARY, logs.getvalue()+(self.data/'connectors.json').read_text())

    def test_advertised_pagination_totals_preserve_output_on_short_or_inconsistent_pages(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        path = self.data/'trakt.json'
        pages = trakt_pages()
        for headers, rows in (({'X-Pagination-Page-Count': '1', 'X-Pagination-Item-Count': '1'}, []),
                              ({'X-Pagination-Page-Count': '2', 'X-Pagination-Item-Count': '101'}, [{}]),
                              ({'X-Pagination-Page-Count': '2'}, []),
                              ({'X-Pagination-Page-Count': '1', 'X-Pagination-Item-Count': '-1'}, []),
                              ({'X-Pagination-Page-Count': '1', 'X-Pagination-Item-Count': 'invalid'}, [])):
            with self.subTest(headers=headers):
                def transport(req, timeout):
                    route = urllib.parse.urlsplit(req.full_url).path
                    response = Reply(rows if route == '/sync/ratings' else pages[route])
                    if route == '/sync/ratings':
                        response.headers = headers
                    return response
                path.write_bytes(b'{"prior": true}')
                stamp = path.stat().st_mtime_ns
                self.s._connectors['trakt_api']['next_due'] = NOW
                with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                     patch.object(self.s, '_connector_open', transport):
                    self.drain()
                self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'error')
                self.assertEqual(path.read_bytes(), b'{"prior": true}')
                self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_page_count_only_short_pages_preserve_output(self):
        pages = trakt_pages()
        path = self.data/'trakt.json'
        for size in (1, 99, 101):
            self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
            path.write_bytes(b'{"prior": true}\n')
            stamp = path.stat().st_mtime_ns
            def transport(req, timeout):
                url = urllib.parse.urlsplit(req.full_url)
                query = urllib.parse.parse_qs(url.query)
                self.assertEqual(query['limit'], ['100'])
                if url.path == '/sync/ratings':
                    rows = [pages[url.path][0]]*size if query['page'] == ['1'] else pages[url.path][1:]
                    return Reply(rows, pages=2)
                return Reply(pages[url.path])
            with self.subTest(size=size), patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                 patch.object(self.s, '_connector_open', transport):
                self.drain()
                self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'error')
                self.assertEqual(self.s._connectors['trakt_api']['health']['error'], 'ValueError')
                self.assertEqual(path.read_bytes(), b'{"prior": true}\n')
                self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_page_count_only_oversized_final_page_preserves_output(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        path = self.data/'trakt.json'
        path.write_bytes(b'{"prior": true}\n')
        stamp = path.stat().st_mtime_ns
        pages = trakt_pages()
        def transport(req, timeout):
            url = urllib.parse.urlsplit(req.full_url)
            page = urllib.parse.parse_qs(url.query)['page'][0]
            if url.path == '/sync/ratings':
                return Reply([pages[url.path][0]] * (100 if page == '1' else 101), pages=2)
            return Reply(pages[url.path])
        with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                patch.object(self.s, '_connector_open', transport):
            self.drain()
        self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'error')
        self.assertEqual(path.read_bytes(), b'{"prior": true}\n')
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_zero_page_count_without_item_count(self):
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        path = self.data/'trakt.json'
        path.write_bytes(b'{"items": [], "prior": true}\n')
        with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
             patch.object(self.s, '_connector_open', side_effect=lambda *args, **kwargs: Reply([], pages=0)) as opened:
            self.drain()
        self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(json.loads(path.read_bytes())['items'], [])
        self.assertNotIn('prior', json.loads(path.read_bytes()))

    def test_pagination_metadata_and_unpaginated_responses_both_work(self):
        for supplied in (False, True):
            self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
            self.s._connectors['trakt_api']['next_due'] = NOW
            pages = trakt_pages()
            def transport(req, timeout):
                rows = pages[urllib.parse.urlsplit(req.full_url).path]
                response = Reply(rows)
                response.headers = {'X-Pagination-Page-Count': '1', 'X-Pagination-Item-Count': str(len(rows))} if supplied else {}
                return response
            with self.subTest(supplied=supplied), patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
                 patch.object(self.s, '_connector_open', transport):
                self.drain()
            self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')
            self.assertTrue(json.loads((self.data/'trakt.json').read_text())['items'])
        for count in ('0', '1'):
            response = Reply([])
            response.headers = {'X-Pagination-Page-Count': count, 'X-Pagination-Item-Count': '0'}
            with patch.object(self.s, '_connector_open', return_value=response):
                self.assertEqual(self.s._http_get('https://api.trakt.tv/sync/ratings', paged=True), [])

    def test_full_pages_work_and_changed_pagination_metadata_fails(self):
        for changed in (None, 'pages', 'total'):
            def transport(req, timeout):
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)['page'][0])
                response = Reply([{}]* (100 if page == 1 else 1), pages=2)
                response.headers['X-Pagination-Item-Count'] = '101'
                if page == 2 and changed:
                    response.headers['X-Pagination-'+('Page-Count' if changed == 'pages' else 'Item-Count')] = '3' if changed == 'pages' else '102'
                return response
            with self.subTest(changed=changed), patch.object(self.s, '_connector_open', transport):
                if changed:
                    with self.assertRaises(ValueError):
                        self.s._http_get('https://api.trakt.tv/sync/ratings', paged=True)
                else:
                    self.assertEqual(len(self.s._http_get('https://api.trakt.tv/sync/ratings', paged=True)), 101)

    def test_url_page_byte_limits_and_no_partial_output(self):
        s = self.s
        for url in ('http://example.com/path', 'http://100.64.0.1/', 'http://169.254.1.1/', 'ftp://jellyfin/',
                    'https://user:secret@example.com/', 'http://8.8.8.8/'):
            with self.subTest(url=url), patch.object(s, '_connector_open', side_effect=AssertionError('opened unsafe URL')):
                with self.assertRaises(s.UnsafeURL):
                    s._http_get(url)
        for url in ('http://127.0.0.1/', 'http://[::1]/', 'http://localhost/', 'http://jellyfin:8096/',
                    'http://192.168.1.1/', 'http://172.16.0.1/', 'http://10.0.0.1/', 'https://example.com/',
                    'http://hydra.local:5076/api', 'http://nzbhydra.home.arpa:5076', 'http://hydra.lan:5076/api',
                    'http://[fd00::1]:5076/api', 'http://[fe80::1]:5076/api'):
            with patch.object(s, '_connector_open', return_value=Reply([])):
                self.assertEqual(s._http_get(url), [])
        with self.assertRaises(s.UnsafeURL):
            s._ConnectorNoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com/')
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        old = b'{"items": []}'
        (self.data/'trakt.json').write_bytes(old)
        with patch.dict(os.environ, {'FAKE_TRAKT_TOKEN': CANARY, 'TRAKT_CLIENT_ID': 'fake-client'}), \
             patch.object(s, 'CONNECTOR_HTTP_BYTES', 10), patch.object(s, '_connector_open', return_value=Reply(['long value'])):
            self.drain()
        self.assertEqual(s._connectors['trakt_api']['health']['error'], 'ByteCapExceeded')
        self.assertEqual((self.data/'trakt.json').read_bytes(), old)
        with patch.object(s, 'CONNECTOR_HTTP_PAGES', 2), patch.object(s, '_connector_open', side_effect=lambda *a,**k: Reply([],3)):
            with self.assertRaises(s.PageCapExceeded):
                s._http_get('https://example.com/', paged=True)
        for reference in ('file:../token', 'file:..', 'file:.', 'env:secret/token', CANARY,
                          'file:remote.json', 'file:clients.json', 'file:hydra.json', 'file:.env'):
            with self.assertRaises(s.AuthMissing):
                s._resolve_auth(reference)
        (self.credentials/'link.token').symlink_to(self.credentials/'fake.token')
        with self.assertRaises(OSError):
            s._resolve_auth('file:link.token')

    def test_one_bounded_worker_nonblocking_feed_and_idle_tick(self):
        s = self.s
        self.configure('trakt_api', auth='env:FAKE_TRAKT_TOKEN')
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        def slow(c, now):
            entered.set()
            gate.wait(3)
            return [], [now, 0], {'events': 0, 'error': None}
        with patch.dict(s.CONNECTOR_POLL, {'trakt_api': slow}):
            start = time.perf_counter()
            s.connectors_tick(NOW)
            self.assertLess(time.perf_counter()-start, .05)
            self.assertTrue(entered.wait(1))
            future = s._connector_pending[2]
            for _ in range(10):
                s.connectors_tick(NOW+1)
                self.assertIs(s._connector_pending[2], future)
            with patch.object(s, 'refresh_profiles', return_value={}), patch.object(s, 'job_specs', return_value=[]):
                start = time.perf_counter()
                s.feed_pass(s.local_now(), {})
                self.assertLess(time.perf_counter()-start, .05)
            gate.set()
            future.result(timeout=1)
            s.connectors_tick(NOW+1)
        self.assertEqual(s._connectors['trakt_api']['counts']['polls'], 1)
        with patch.object(s, '_connector_open', side_effect=AssertionError('idle did network')):
            elapsed = []
            for _ in range(100):
                start = time.perf_counter()
                s.connectors_tick(NOW+2)
                elapsed.append((time.perf_counter()-start)*1000)
            self.assertLess(sorted(elapsed)[94], 1)

    def test_poll_refuses_writable_credentials_directory(self):
        (self.credentials/'fake.token').write_text(CANARY)
        pages = trakt_pages()
        hint = 'Set DATA/credentials/ owner to the engine uid and chmod 0700.'
        original_chmod = os.fchmod
        def chmod_file(fd, mode):
            self.assertFalse(stat.S_ISDIR(os.fstat(fd).st_mode), 'poll chmodded a directory')
            original_chmod(fd, mode)
        for mode in (0o777, 0o720, 0o702):
            self.credentials.chmod(0o700)
            self.configure('trakt_api', auth='file:fake.token')
            path = self.data/'trakt.json'
            path.write_bytes(b'{"prior": true}\n')
            stamp = path.stat().st_mtime_ns
            self.credentials.chmod(mode)
            def poll(req, timeout):
                return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
            with self.subTest(mode=oct(mode), seam='poll'), patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), \
                 patch.object(self.s, '_connector_open', side_effect=poll) as opened, patch.object(os, 'fchmod', side_effect=chmod_file):
                self.drain()
                health = self.s._connectors['trakt_api']['health']
                self.assertEqual(health['state'], 'error')
                self.assertEqual(health['error'], 'CredentialsDirectoryUnsafe')
                self.assertEqual(health['fixHint'], hint)
                self.assertEqual(path.read_bytes(), b'{"prior": true}\n')
                self.assertEqual(path.stat().st_mtime_ns, stamp)
                self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), mode)
                opened.assert_not_called()
            from skald import trakt
            def device(req, timeout):
                if req.full_url.endswith('/oauth/device/code'):
                    return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                                  'expires_in': 60, 'interval': 1})
                return Reply(oauth_bundle())
            self.credentials.chmod(mode)
            with self.subTest(mode=oct(mode), seam='login'), patch.dict(os.environ, {'DATA_DIR': str(self.data),
                    'TRAKT_CLIENT_ID': 'fake-client', 'TRAKT_CLIENT_SECRET': CANARY}), patch.object(members, 'CONFIG', self.s.CONFIG), \
                 patch.object(trakt, '_connector_open', side_effect=device) as opened, patch.object(trakt.time, 'sleep'):
                with self.assertRaisesRegex(ValueError, 'CredentialsDirectoryUnsafe'):
                    trakt.login(io.StringIO())
                opened.assert_not_called()
                self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), mode)
        self.credentials.chmod(0o700)
        self.s._connectors['trakt_api']['next_due'] = NOW
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), patch.object(self.s, '_connector_open', poll):
            self.drain()
        self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'ok')
        self.assertNotIn('fixHint', self.s._connectors['trakt_api']['health'])

    def test_fifo_rejected_promptly_and_next_connector_completes(self):
        os.mkfifo(self.credentials/'fifo.token')
        self.configure('trakt_api', auth='file:fifo.token')
        self.configure('aiostreams_db', options=watch_db(self.data/'watch.sqlite'))
        started = time.perf_counter()
        with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), \
             patch.object(self.s, '_connector_open', side_effect=AssertionError('FIFO reached transport')):
            self.s.connectors_tick(NOW)
            future = self.s._connector_pending[2]
            try:
                future.result(timeout=.2)
                prompt = True
            except TimeoutError:
                prompt = False
                # Release a broken blocking open so a red run cannot strand the executor.
                fd = os.open(self.credentials/'fifo.token', os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                future.result(timeout=1)
            self.s.connectors_tick(NOW+.01)
            self.drain(NOW+.02)
        self.assertTrue(prompt, 'FIFO blocked the sole connector executor')
        self.assertLess(time.perf_counter()-started, .2)
        self.assertEqual(self.s._connectors['trakt_api']['health']['error'], 'AuthMissing')
        self.assertEqual(self.s._connectors['aiostreams_db']['health']['state'], 'ok')
        self.assertTrue((self.data/'history.json').exists())

    def test_directory_and_symlink_auth_never_reach_transport(self):
        (self.credentials/'dir.token').mkdir()
        (self.credentials/'real.token').write_text(CANARY)
        (self.credentials/'link.token').symlink_to(self.credentials/'real.token')
        for reference in ('file:dir.token', 'file:link.token', 'file:../real.token'):
            with self.subTest(reference=reference):
                self.configure('trakt_api', auth='file:fake.token')
                self.s.CONFIG['connectors']['trakt_api']['auth'] = reference
                self.s._connectors['trakt_api']['next_due'] = NOW
                with patch.dict(os.environ, {'TRAKT_CLIENT_ID': 'fake-client'}), \
                     patch.object(self.s, '_connector_open', side_effect=AssertionError('invalid auth reached transport')):
                    self.drain()
                self.assertEqual(self.s._connectors['trakt_api']['health']['state'], 'error')
                self.assertFalse((self.data/'trakt.json').exists())

    def test_config_validation_public_refs_and_idempotent_seeding(self):
        s = self.s
        valid = copy.deepcopy(s.CONFIG)
        valid['connectors'] = {'trakt_api': {'kind': 'api', 'enabled': True, 'auth': 'file:fake.token',
                             'options': {'client_id': 'env:TRAKT_CLIENT_ID', 'client_secret': 'env:TRAKT_CLIENT_SECRET'}}}
        self.assertEqual(s.validate_config(valid), valid)
        self.assertEqual(s.public_config(valid)['connectors'], valid['connectors'])
        for source in ('jellyfin', 'plex'):
            cfg = copy.deepcopy(valid)
            cfg['history']['source'] = source
            with self.assertRaisesRegex(s.ConfigError, 'history.source'):
                s.validate_config(cfg)
        for field, value in (('auth', CANARY), ('auth', 'file:..'), ('enabled', 1), ('kind', 'stream'), ('options', {'client_id': CANARY}),
                             ('options', {'token': CANARY})):
            cfg = copy.deepcopy(valid)
            cfg['connectors']['trakt_api'][field] = value
            with self.assertRaises(s.ConfigError) as error:
                s.validate_config(cfg)
            self.assertNotIn(CANARY, str(error.exception))
        self.configure('trakt_api', auth='file:fake.token')
        path = self.data/'connectors.json'
        before, stamp = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(s, '_connectors_save', side_effect=AssertionError('idempotent load wrote')) as save:
            s.connectors_load(NOW+1)
            save.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        s._connectors['trakt_api'].update(auth=None, health={'state':'error','error':'AuthNotReference','at':NOW})
        s._connectors_save(s._connectors,s._connectors_generation)
        s.connectors_load(NOW+2)
        self.assertEqual(s._connectors['trakt_api']['auth'],'file:fake.token')
        self.assertEqual(s._connectors['trakt_api']['health']['state'],'ok')
        s.CONFIG['connectors'].clear()
        s.connectors_load(NOW+3)
        self.assertEqual(s._connectors['trakt_api']['health']['state'], 'disabled')

    def test_hydra_declaration_is_allowed_but_never_a_credential_source(self):
        cfg = copy.deepcopy(self.s.CONFIG)
        cfg['connectors']['nzbhydra'] = {'kind': 'availability', 'enabled': True, 'auth': 'file:hydra.json', 'options': {}}
        for loader in (members, self.s):
            self.assertEqual(loader.validate_config(cfg)['connectors'], cfg['connectors'])
        for loader in (self.s, __import__('skald.trakt', fromlist=['trakt'])):
            with patch.object(loader.os, 'open', side_effect=AssertionError('opened Hydra as credential')):
                with self.assertRaises(loader.AuthMissing):
                    loader._resolve_auth('file:hydra.json')
        for option in ('client_id', 'client_secret'):
            bad = copy.deepcopy(cfg)
            bad['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'auth': 'file:fake.token',
                                             'options': {option: 'file:hydra.json'}}
            for loader in (members, self.s):
                with self.assertRaises(loader.ConfigError):
                    loader.validate_config(bad)

    def test_empty_household_doctor_info_and_no_history_io(self):
        cfg = self.s.CONFIG
        (self.data/'skald.json').write_text(json.dumps(cfg))
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)
            self.s.connectors_tick(NOW)
        self.assertEqual(self.s._connectors['trakt']['health']['state'], 'disabled')
        # Doctor requires a separate writable state directory.
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            state.chmod(0o700)
            out = io.StringIO()
            self.assertIn(main(['--snapshot',str(self.data),'--state-dir',str(state),'--json','doctor'],out=out),(0,1))
            finding = next(f for f in json.loads(out.getvalue())['findings'] if f['checkId']=='connectors.health')
            self.assertEqual(finding['severity'], 'info')
            self.assertEqual(finding['fixHint'], 'Connect Trakt, drop a CSV export or paste titles; see docs/CONNECTORS.md')
            out = io.StringIO()
            main(['--snapshot',str(self.data),'--state-dir',str(state),'doctor'],out=out)
            self.assertEqual(sum('docs/CONNECTORS.md' in line for line in out.getvalue().splitlines()), 1)
        s = self.s
        s.connectors_load(NOW)
        with patch.object(s, '_connector_open', side_effect=AssertionError('network')), patch.object(s.sqlite3, 'connect', side_effect=AssertionError('database')):
            s.connectors_tick(NOW)
        self.assertEqual(s._connectors['history']['health']['state'], 'disabled')
        self.assertFalse((self.data/'history.json').exists())


class TraktLogin(unittest.TestCase):
    def setUp(self):
        from skald import trakt
        self.trakt = trakt
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.credentials = self.data/'credentials'
        cfg = members.fresh_config(['ana'])
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'auth': 'file:fake.token', 'options': {}}
        (self.data/'skald.json').write_text(json.dumps(cfg))
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TRAKT_CLIENT_ID': 'fake-id', 'TRAKT_CLIENT_SECRET': CANARY,
                                         'SKALD_SNAPSHOT': ''})
        self.env.start()
        self.addCleanup(members.configure)
        self.addCleanup(self.env.stop)

    def test_login_persists_full_oauth_bundle_privately(self):
        bundle = {'access_token': CANARY, 'refresh_token': CANARY + '_refresh',
                  'created_at': int(NOW), 'expires_in': 7200}
        def device(req, timeout):
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 60, 'interval': 1})
            return Reply(bundle)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.trakt, '_connector_open', device), patch.object(self.trakt.time, 'sleep'), redirect_stderr(err):
            self.assertEqual(main(['trakt', 'login', '--member', 'ana'], out=out), 0)
        self.assertEqual(json.loads((self.credentials / 'fake.token').read_text()), bundle)
        self.assertEqual(stat.S_IMODE((self.credentials / 'fake.token').stat().st_mode), 0o600)
        self.assertNotIn(CANARY, out.getvalue() + err.getvalue())

    def test_login_trakt_person_writes_only_selected_private_account(self):
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'ana': 'file:trakt-ana.json', 'ben': 'file:trakt-ben.json'}}
        (self.data/'skald.json').write_text(json.dumps(cfg))
        def device(req, timeout):
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 60, 'interval': 1})
            return Reply(oauth_bundle())
        out = io.StringIO()
        with patch.object(self.trakt, '_connector_open', device), patch.object(self.trakt.time, 'sleep'):
            self.assertEqual(main(['login', 'trakt', '--person', 'ben'], out=out), 0)
        self.assertTrue(json.loads((self.credentials/'trakt-ben.json').read_text()) == oauth_bundle())
        self.assertFalse((self.credentials/'trakt-ana.json').exists())
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.credentials/'trakt-ben.json').stat().st_mode), 0o600)
        self.assertNotIn(CANARY, out.getvalue())

    def test_login_refuses_unknown_person_before_authorization(self):
        members.configure()
        with patch.object(self.trakt, '_connector_open', side_effect=AssertionError('authorization attempted')) as opened:
            with self.assertRaisesRegex(ValueError, 'unknown person'):
                self.trakt.login(io.StringIO(), 'unknown')
            opened.assert_not_called()
        self.assertFalse(self.credentials.exists())

    def test_login_rechecks_active_config_before_replace(self):
        self.credentials.mkdir(mode=0o700)
        destination = self.credentials/'fake.token'
        alias = self.data/'config-alias.json'
        active = self.data/'active-config.json'
        cfg = json.loads((self.data/'skald.json').read_text())
        for target in (destination, alias):
            destination.write_bytes(b'previous credential\n')
            active.symlink_to(self.data/'skald.json')
            def device(req, timeout):
                if req.full_url.endswith('/oauth/device/code'):
                    return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                                  'expires_in': 60, 'interval': 1})
                if target == alias:
                    os.link(destination, alias)
                active.unlink()
                active.symlink_to(target)
                return Reply(oauth_bundle())
            with self.subTest(target=target.name), patch.dict(os.environ, {'SKALD_CONFIG': str(active)}), \
                 patch.object(members, 'CONFIG', cfg), patch.object(self.trakt, '_connector_open', side_effect=device) as opened, \
                 patch.object(self.trakt.time, 'sleep'):
                with self.assertRaisesRegex(ValueError, 'AuthMissing'):
                    self.trakt.login(io.StringIO())
                self.assertEqual(opened.call_count, 2)
                self.assertEqual(destination.read_bytes(), b'previous credential\n')
                self.assertEqual(active.read_bytes(), b'previous credential\n')
                self.assertFalse(list(self.credentials.glob('.trakt-auth-*')))
            active.unlink()
            if alias.exists():
                alias.unlink()

    def test_hardlinked_data_auth_and_credential_backup_refused(self):
        self.credentials.mkdir(mode=0o700)
        (self.data/'state.json').write_text(CANARY)
        os.link(self.data/'state.json', self.credentials/'data.token')
        cfg = json.loads((self.data/'skald.json').read_text())
        cfg['connectors']['trakt_api']['auth'] = 'file:data.token'
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        s.CONFIG = cfg
        with self.subTest(seam='login'), patch.object(members, 'CONFIG', cfg), \
             patch.object(self.trakt, '_connector_open', side_effect=AssertionError('hard link reached transport')) as opened:
            with self.assertRaisesRegex(ValueError, 'AuthMissing'):
                self.trakt.login(io.StringIO())
            opened.assert_not_called()
        with self.subTest(seam='poll'), patch.object(s, '_connector_open', side_effect=AssertionError('hard link reached transport')) as opened:
            with self.assertRaises(s.AuthMissing):
                s.poll({'id': 'trakt_api'}, NOW)
            opened.assert_not_called()
        (self.credentials/'private.token').write_text(CANARY)
        nested = self.credentials/'old'
        nested.mkdir()
        (nested/'private.token').write_text(CANARY)
        os.link(self.credentials/'private.token', self.data/'taste.json')
        os.link(nested/'private.token', self.data/'history.json')
        (self.data/'public.log').write_bytes(b'{"synthetic":1}\n')
        os.link(self.data/'public.log', self.data/'feedback.jsonl')
        with self.subTest(seam='backup'), redirect_stdout(io.StringIO()):
            s.backup_state(NOW)
            archive = next((self.data/'backups').glob('state-*.tar.gz'))
            with tarfile.open(archive) as tar:
                names = tar.getnames()
                self.assertNotIn('taste.json', names)
                self.assertNotIn('history.json', names)
                self.assertIn('feedback.jsonl', names)
                manifest = json.load(tar.extractfile('manifest.json'))
                for name in ('taste.json', 'history.json'):
                    self.assertEqual(manifest['skipped'][name], 'credential alias')
                for member in tar.getmembers():
                    self.assertNotIn(CANARY.encode(), tar.extractfile(member).read())

    def test_denied_destinations_preserve_state_and_backups(self):
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        names = {'household.json', 'online.json.bad', 'imports.jsonl.bak-2026-10-11', '.history-abc123',
                 '.skald-abc123', '.daily-abc123.tmp', 'state-2026-10-11.tar.gz', 'hydra.json'}
        def transport(req, timeout):
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 60, 'interval': 1})
            return Reply(oauth_bundle())
        cfg = json.loads((self.data/'skald.json').read_text())
        for name in sorted(names):
            path = self.data/name
            path.write_bytes(b'{"synthetic":1}\n')
            cfg['connectors']['trakt_api']['auth'] = 'file:'+name
            with self.subTest(name=name, seam='config'), patch.dict(os.environ, {'SKALD_CONFIG': str(self.data/'household.json')}):
                for loader in (members, s):
                    with self.assertRaises(loader.ConfigError):
                        loader.validate_config(cfg)
            with self.subTest(name=name, seam='login'), patch.object(members, 'CONFIG', cfg), \
                 patch.object(self.trakt, '_connector_open', side_effect=transport) as opened, patch.object(self.trakt.time, 'sleep'):
                with self.assertRaises(ValueError):
                    self.trakt.login(io.StringIO())
                opened.assert_not_called()
                self.assertEqual(path.read_bytes(), b'{"synthetic":1}\n')
            with self.subTest(name=name, seam='resolve'):
                for loader in (s, self.trakt):
                    loader.DATA = str(self.data)
                    with self.assertRaises(loader.AuthMissing):
                        loader._resolve_auth('file:'+name)
        with redirect_stdout(io.StringIO()):
            s.backup_state(NOW)
        archive = next((self.data/'backups').glob('state-*.tar.gz'))
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                self.assertNotIn(CANARY.encode(), tar.extractfile(member).read())

    def test_login_and_poll_use_only_private_credentials_and_backup_excludes_them(self):
        cfg = json.loads((self.data/'skald.json').read_text())
        cfg['connectors']['trakt_api']['auth'] = 'file:trakt-main.json'
        (self.data/'skald.json').write_text(json.dumps(cfg))
        def device(req, timeout):
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 60, 'interval': 1})
            return Reply(oauth_bundle())
        synced = []
        original_fsync = os.fsync
        def sync(fd):
            synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
            original_fsync(fd)
        with patch.object(self.trakt, '_connector_open', device), patch.object(self.trakt.time, 'sleep'), patch.object(os, 'fsync', side_effect=sync):
            self.assertEqual(main(['trakt', 'login'], out=io.StringIO()), 0)
        self.assertEqual(synced, [stat.S_IFREG, stat.S_IFDIR])
        credentials = self.data/'credentials'
        token = credentials/'trakt-main.json'
        self.assertTrue(json.loads(token.read_text()) == oauth_bundle())
        self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertFalse((self.data/'trakt-main.json').exists())
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        pages = trakt_pages()
        def poll(req, timeout):
            self.assertEqual(req.get_header('Authorization'), 'Bearer '+CANARY)
            return Reply(pages[urllib.parse.urlsplit(req.full_url).path])
        with patch.object(s, '_connector_open', poll), redirect_stdout(io.StringIO()):
            s.connectors_load(NOW)
            s.poll(s._connectors['trakt_api'], NOW)
            s.backup_state(NOW)
        self.assertTrue(json.loads((self.data/'trakt.json').read_text())['items'])
        self.assertEqual(list(credentials.iterdir()), [token])
        archive = next((self.data/'backups').glob('state-*.tar.gz'))
        with tarfile.open(archive) as tar:
            self.assertFalse(any('credentials' in member.name for member in tar.getmembers()))
            for member in tar.getmembers():
                self.assertNotIn(CANARY.encode(), tar.extractfile(member).read())

    def test_parent_symlink_or_foreign_owner_refused_before_login_or_poll(self):
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        cfg = json.loads((self.data/'skald.json').read_text())
        cfg['connectors']['trakt_api']['auth'] = 'file:trakt-main.json'
        outside = self.data/'outside'
        outside.mkdir()
        (outside/'trakt-main.json').write_text('previous')
        self.credentials.symlink_to(outside, target_is_directory=True)
        original_fstat = os.fstat
        def foreign_owner(fd):
            st = original_fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                values = list(st)
                values[4] = os.getuid()+1
                return os.stat_result(values)
            return st
        for kind in ('symlink', 'foreign owner'):
            if kind == 'foreign owner':
                self.credentials.unlink()
                self.credentials.mkdir(mode=0o700)
                (self.credentials/'trakt-main.json').write_text('previous')
            with self.subTest(kind=kind), patch.object(members, 'CONFIG', cfg), \
                 patch.object(self.trakt, '_connector_open', side_effect=AssertionError('unsafe parent reached transport')) as opened, \
                 patch.object(os, 'fstat', side_effect=foreign_owner if kind == 'foreign owner' else original_fstat):
                with self.assertRaises(ValueError):
                    self.trakt.login(io.StringIO())
                opened.assert_not_called()
                for loader in (s, self.trakt):
                    loader.DATA = str(self.data)
                    with self.assertRaises((OSError, loader.AuthMissing)):
                        loader._resolve_auth('file:trakt-main.json')
            self.assertEqual((self.credentials/'trakt-main.json').read_text(), 'previous')
            self.assertEqual((outside/'trakt-main.json').read_text(), 'previous')

    def test_active_config_and_legacy_root_reference_fail_validation_with_move_hint(self):
        self.credentials.mkdir(mode=0o700)
        cfg = json.loads((self.data/'skald.json').read_text())
        for path in (self.data/'household.json', self.credentials/'household.json'):
            cfg['connectors']['trakt_api']['auth'] = 'file:household.json'
            path.write_text(json.dumps(cfg))
            before = path.read_bytes()
            with self.subTest(path=path), patch.dict(os.environ, {'SKALD_CONFIG': str(path)}):
                with self.assertRaisesRegex(members.ConfigError, 'DATA/credentials/'):
                    members.load_config(str(self.data))
                out, err = io.StringIO(), io.StringIO()
                with patch.object(self.trakt, '_connector_open', side_effect=AssertionError('active config reached transport')) as opened, redirect_stderr(err):
                    self.assertEqual(main(['trakt', 'login'], out=out), 2)
                opened.assert_not_called()
                self.assertIn('DATA/credentials/', err.getvalue()+out.getvalue())
            self.assertEqual(path.read_bytes(), before)
        (self.credentials/'household.json').unlink()
        cfg['connectors']['trakt_api']['auth'] = 'file:legacy.token'
        (self.data/'legacy.token').write_text(CANARY)
        with self.assertRaisesRegex(members.ConfigError, 'move the file into DATA/credentials/'):
            members.validate_config(cfg, data=str(self.data))

    def test_login_rejects_traversal_symlink_fifo_and_directory_before_transport(self):
        self.credentials.mkdir(mode=0o700)
        (self.credentials/'real.token').write_text('previous')
        (self.credentials/'link.token').symlink_to(self.credentials/'real.token')
        (self.credentials/'dir.token').mkdir()
        os.mkfifo(self.credentials/'fifo.token')
        cfg = json.loads((self.data/'skald.json').read_text())
        def transport(req, timeout):
            raise RuntimeError('transport must not run')
        for name in ('../real.token', 'link.token', 'fifo.token', 'dir.token'):
            cfg['connectors']['trakt_api']['auth'] = 'file:'+name
            with self.subTest(name=name), patch.object(members, 'CONFIG', cfg), \
                 patch.object(self.trakt, '_connector_open', side_effect=transport) as opened:
                with self.assertRaises(ValueError):
                    self.trakt.login(io.StringIO())
                opened.assert_not_called()
                self.assertEqual((self.credentials/'real.token').read_text(), 'previous')

    def test_device_pending_slowdown_success_and_private_atomic_write(self):
        calls = []
        def transport(req, timeout):
            calls.append(req)
            self.assertEqual(timeout, 10)
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private-device', 'user_code': 'USER-CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 60, 'interval': 1})
            self.assertEqual(json.loads(req.data)['client_secret'], CANARY)
            if len(calls) in (2,3):
                raise urllib.error.HTTPError(req.full_url, 400 if len(calls)==2 else 429, CANARY, {}, None)
            return Reply(oauth_bundle())
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.trakt, '_connector_open', transport), patch.object(self.trakt.time, 'sleep') as sleep, redirect_stderr(err):
            self.assertEqual(main(['trakt','login'],out=out),0)
        self.assertEqual(len(calls),4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1,1,6])
        self.assertTrue(json.loads((self.credentials/'fake.token').read_text()) == oauth_bundle())
        self.assertEqual(stat.S_IMODE((self.credentials/'fake.token').stat().st_mode),0o600)
        self.assertNotIn(CANARY,out.getvalue()+err.getvalue())
        self.assertFalse(list(self.credentials.glob('.trakt-auth-*')))

    def test_errors_never_echo_secrets_or_replace_existing_token(self):
        self.credentials.mkdir(mode=0o700)
        token = self.credentials/'fake.token'
        token.write_text('previous')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.trakt, '_connector_open', side_effect=RuntimeError(CANARY)), redirect_stderr(err):
            self.assertEqual(main(['--json','trakt','login'],out=out),2)
        self.assertNotIn(CANARY,out.getvalue()+err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['error'],'trakt login: RuntimeError')
        self.assertEqual(token.read_text(),'previous')
        out = io.StringIO()
        with patch.object(self.trakt, '_connector_open', side_effect=AssertionError('snapshot login')):
            self.assertEqual(main(['--snapshot',str(self.data),'--json','trakt','login'],out=out),2)
        cfg = json.loads((self.data/'skald.json').read_text())
        cfg['connectors']['trakt_api']['auth'] = 'file:history.json'
        (self.data/'skald.json').write_text(json.dumps(cfg))
        (self.data/'history.json').write_text('{"items": []}')
        out = io.StringIO()
        with patch.object(self.trakt,'_connector_open',side_effect=AssertionError('reserved file login')):
            self.assertEqual(main(['--json','trakt','login'],out=out),2)
        self.assertEqual((self.data/'history.json').read_text(),'{"items": []}')

    def test_expired_or_denied_device_flow_writes_nothing(self):
        def reply(req, timeout):
            if req.full_url.endswith('/oauth/device/code'):
                return Reply({'device_code': 'private', 'user_code': 'CODE', 'verification_url': 'https://trakt.tv/activate',
                              'expires_in': 2, 'interval': 1})
            raise urllib.error.HTTPError(req.full_url, 403, CANARY, {}, None)
        for clock in (None, [0,0,0,3]):
            out = io.StringIO()
            with patch.object(self.trakt, '_connector_open', reply), patch.object(self.trakt.time,'sleep'), \
                 patch.object(self.trakt.time,'monotonic', side_effect=clock) if clock else patch.object(self.trakt.time,'sleep'):
                self.assertEqual(main(['--json','trakt','login'],out=out),2)
            self.assertNotIn(CANARY,out.getvalue())
            self.assertFalse((self.credentials/'fake.token').exists())


if __name__ == '__main__':
    unittest.main()
