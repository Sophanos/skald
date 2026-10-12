"""Laptop lookup and token filtering, using synthetic API transport only."""
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from skald import intake, remote
from skald.ingest import Candidate
from test_api_routes import ApiCase


class LaptopLookupTests(ApiCase):
    def lookup(self, path, **params):
        self.lookups.append((path, params))
        if path.startswith('find/'):
            return {'movie_results': [{'id': 42, 'title': 'Example', 'release_date': '2000-01-01'}]}
        return {'results': [{'id': 42, 'title': params['query'], 'release_date': '2000-01-01', 'media_type': 'movie'}]}

    def send(self, path, body=None):
        self.bodies.append(body)
        code, reply = self.request(path, 'POST', body, client='read')
        if code != 200:
            raise ValueError(reply['error'])
        return reply

    def resolve(self, candidates, max_ai_usd=0, dry_run=True):
        engine = SimpleNamespace(_server=SimpleNamespace(tmdb=lambda *a, **kw: {}))
        with patch.object(self.s, 'API_KEY', 'synthetic'), patch.object(self.s, 'tmdb', side_effect=self.lookup), \
                patch.object(remote, 'request', side_effect=self.send), patch.dict('os.environ', {'TMDB_API': ''}):
            return intake.resolve(engine, Path(self.temp.name), candidates, online=False, remote=True,
                                  max_ai_usd=max_ai_usd, dry_run=dry_run)

    def setUp(self):
        super().setUp()
        self.bodies, self.lookups = [], []

    def test_ambiguous_server_match_goes_to_review_without_ai_key(self):
        engine = SimpleNamespace(_server=SimpleNamespace(tmdb=lambda *a, **kw: {}))
        item = {'title': 'Example', 'year': '', 'kind': '', 'ids': {}}
        def send(path, body=None):
            if body is None:
                return {'types': [{'type': 'resolve_title', 'available': False}]}
            return self.send(path, body)
        choices = {'results': [{'id': i, 'title': 'Example', 'media_type': 'movie'} for i in (1, 2)]}
        with patch.object(self.s, 'API_KEY', 'synthetic'), patch.object(self.s, 'tmdb', return_value=choices), patch.object(remote, 'request', side_effect=send):
            _, results = intake.resolve(engine, Path(self.temp.name), [Candidate(**item)], online=False,
                                        remote=True, max_ai_usd=.05, dry_run=False)
        self.assertFalse(results[0]['high_confidence'])
        self.assertEqual(results[0]['method'], 'unresolved')

    def test_server_matches_need_no_decide_provider(self):
        _, results = self.resolve([Candidate('Example', '2000', 'movie')], max_ai_usd=.05, dry_run=False)
        self.assertTrue(results[0]['high_confidence'])
        self.assertEqual(len(self.bodies), 1)

    def test_transport_preserves_safe_missing_key_and_403_reasons(self):
        hint = 'Server has no TMDB key; use --resolve-online with TMDB_API on your laptop.'
        for code, reason, message in ((503, hint, hint), (403, 'person', 'api 403: person')):
            error = urllib.error.HTTPError('http://synthetic.test', code, 'error', {}, io.BytesIO(json.dumps({'error': reason}).encode()))
            opener = Mock()
            opener.open.side_effect = error
            with patch.object(remote, 'settings', return_value=('http://synthetic.test', '')), patch('urllib.request.build_opener', return_value=opener):
                with self.assertRaises(ValueError) as raised:
                    remote.request('/api/v1/resolve', {'items': []})
            self.assertEqual(str(raised.exception), message)
            self.assertNotIn('\n', str(raised.exception))

    def test_online_uses_laptop_tmdb_without_remote_lookup(self):
        self.s.urllib = __import__('urllib')
        engine = SimpleNamespace(_server=self.s, _network_tmdb=self.lookup)
        with patch.dict('os.environ', {'TMDB_API': 'synthetic'}), patch.object(remote, 'request', side_effect=AssertionError('remote lookup')):
            _, results = intake.resolve(engine, Path(self.temp.name), [Candidate('Example', '2000', 'movie')],
                                        online=True, remote=True, max_ai_usd=0, dry_run=True)
        self.assertTrue(results[0]['high_confidence'])

    def test_remote_dry_run_ids_title_year_and_lookup_fields_only(self):
        candidates = [Candidate('Example', '2000', 'movie', {'imdb': 'tt42'}, last_ts='2020-01-01', person='ben'),
                      Candidate('Another', '2000', 'movie')]
        _, results = self.resolve(candidates)
        self.assertTrue(all(r['high_confidence'] for r in results))
        self.assertEqual([r['method'] for r in results], ['id', 'search'])
        self.assertEqual(set(self.bodies[0]), {'items'})
        self.assertTrue(all(set(item) == {'title', 'year', 'kind', 'ids'} for item in self.bodies[0]['items']))
        self.assertEqual(self.lookups[0][0], 'find/tt42')
        self.assertTrue(all(0 < params['_timeout'] <= 8 for _, params in self.lookups))
        self.assertFalse(Path(self.temp.name, 'resolve.json').exists())

    def test_250_candidates_use_three_requests(self):
        _, results = self.resolve([Candidate(f'Example {i}', '2000', 'movie') for i in range(250)])
        self.assertEqual([len(b['items']) for b in self.bodies], [100, 100, 50])
        self.assertTrue(all(r['high_confidence'] for r in results))

    def test_no_server_key_hint(self):
        with patch.object(self.s, 'API_KEY', ''):
            code, reply = self.request('/api/v1/resolve', 'POST', {'items': []}, client='read')
        self.assertEqual(code, 503)
        self.assertEqual(reply['error'], 'Server has no TMDB key; use --resolve-online with TMDB_API on your laptop.')

    def test_limit_validation_and_deadline(self):
        item = {'title': 'Example', 'year': '2000', 'kind': 'movie', 'ids': {}}
        with patch.object(self.s, 'API_KEY', 'synthetic'):
            for items in ([item] * 101, [{**item, 'last_ts': 'private'}], [{**item, 'ids': {'imdb': 'bad'}}]):
                self.assertEqual(self.request('/api/v1/resolve', 'POST', {'items': items})[0], 400)
            with patch.object(self.s.time, 'monotonic', side_effect=[0, 21]):
                reply = self.s.api_resolve({'body': {'items': [item]}})
                self.assertEqual(reply['results'][0]['method'], 'unresolved')

    def test_low_confidence_stays_unresolved(self):
        with patch.object(self.s, 'API_KEY', 'synthetic'), patch.object(self.s, 'tmdb', return_value={'results': []}):
            code, reply = self.request('/api/v1/resolve', 'POST', {'items': [
                {'title': 'Unknown', 'year': '', 'kind': '', 'ids': {}}]})
        self.assertEqual(code, 200)
        self.assertFalse(reply['results'][0]['high_confidence'])

    def test_index_reports_token_person(self):
        self.assertEqual(self.request('/api/v1/decide', client='ben')[1]['person'], 'ben')
        self.assertIsNone(self.request('/api/v1/decide')[1]['person'])
