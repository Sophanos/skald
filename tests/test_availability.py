"""Optional Hydra keeps today's filtering, verified against the unmodified base."""
from contextlib import redirect_stdout
import importlib.util
import io
from itertools import product
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from decide_kit import close_server, load_server
from eval.rows_report import SNAPSHOT, home_server, display_rows

BASE = Path(os.environ['SKALD_GOLDEN_BASE']) if os.environ.get('SKALD_GOLDEN_BASE') else None
METAS = [{'id': 'tmdb:'+str(i), 'type': 'movie', 'name': 'Fixture Movie '+str(i)} for i in range(1,5)]


class Availability(unittest.TestCase):
    def server(self, data, before=False):
        if not before:
            s = load_server(data)
        else:
            spec = importlib.util.spec_from_file_location('availability_before', BASE)
            s = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {'DATA_DIR': str(data), 'TMDB_API': 'offline', 'API_PORT': '', 'OPENROUTER_API_KEY': ''}):
                spec.loader.exec_module(s)
        self.addCleanup(close_server, s)
        return s

    def test_absent_keeps_all_with_no_transport_and_disabled_seed(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.server(temp)
            with patch('urllib.request.urlopen', side_effect=AssertionError('availability accessed network')) as network:
                self.assertEqual(s.nzb_keep(METAS, 2), METAS[:2])
                self.assertIsNone(s.nzb_check('tmdb:1','movie','Fixture Movie 1'))
                seeds = s.connector_seeds(1)
                network.assert_not_called()
            self.assertEqual(seeds['nzbhydra']['kind'], 'availability')
            self.assertEqual(seeds['nzbhydra']['health']['state'], 'disabled')
            self.assertIsNone(seeds['nzbhydra']['next_due'])

    def test_owner_lan_hydra_urls_stay_enabled_and_match_base_filtering(self):
        if (BASE is None or not BASE.is_file()):
            self.skipTest('base server copy is unavailable')
        urls = ('http://hydra/api', 'http://hydra:5076/api', 'http://hydra.local:5076/api',
                'http://nzbhydra.home.arpa:5076', 'http://hydra.lan:5076/api',
                'http://192.168.1.2:5076/api', 'http://172.16.0.2:5076/api', 'http://10.0.0.2:5076/api',
                'http://localhost:5076/api', 'http://127.0.0.1:5076/api', 'http://[::1]:5076/api',
                'http://[fd00::1]:5076/api', 'http://[fc00::1]:5076/api', 'http://[fe80::1]:5076/api')
        results = {}
        for before, url in product((True,False), urls):
            with self.subTest(before=before, url=url), tempfile.TemporaryDirectory() as temp:
                s = self.server(temp,before)
                Path(temp,'hydra.json').write_text(json.dumps({'url':url,'key':'fake-hydra-key'}))
                s.tmdb = lambda path,**kw: {'imdb_id':'tt'+path.split('/')[1]} if path.endswith('/external_ids') else {
                    'release_date':'2020-01-01','title':'Fixture Movie '+path.split('/')[1]}
                def transport(url,timeout):
                    self.assertEqual(timeout,20)
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    found = query.get('imdbid',[''])[0] in ('tt1','tt3')
                    return io.BytesIO(('<rss><title>Channel</title>'+('<title>Fake release</title>' if found else '')+'</rss>').encode())
                with patch('urllib.request.urlopen',transport), patch.object(s, '_connector_open', transport, create=True), redirect_stdout(io.StringIO()):
                    kept = s.nzb_keep(METAS,4)
                results[before, url] = ([it['id'] for it in kept], sorted(k for k,v in s._nzb['titles'].items() if not v['ok']))
                if not before:
                    with redirect_stdout(io.StringIO()):
                        s.connectors_load(1)
                    self.assertEqual(s._connectors['nzbhydra']['health']['state'],'ok', url)
                    self.assertIsNone(s._connectors['nzbhydra']['next_due'])
                    with patch.object(s,'poll',side_effect=AssertionError('availability polled')):
                        s._connectors['history']['health']['state']='disabled'
                        s._connectors['trakt']['health']['state']='disabled'
                        s.connectors_demand(2)
                        s.connectors_tick(2)
                    self.assertIsNone(s._connectors['nzbhydra']['next_due'])
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(results[True, url], results[False, url])
                self.assertEqual(results[False, url], (['tmdb:1','tmdb:3'],['tmdb:2','tmdb:4']))

    def test_malformed_optional_hydra_disables_availability_without_aborting_load(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.server(temp)
            for value in ([], None, 1, 'invalid', {}, {'url': None, 'key': 'fake'},
                          {'url': [], 'key': 'fake'}, {'url': 'http://hydra/api', 'key': 1},
                          {'url': '', 'key': 'fake'}, {'url': 'http://hydra/api', 'key': ' '}):
                with self.subTest(value=value), patch('urllib.request.urlopen', side_effect=AssertionError('invalid Hydra reached network')):
                    Path(temp,'hydra.json').write_text(json.dumps(value))
                    s._hydra.clear()
                    with redirect_stdout(io.StringIO()):
                        s.load_state()
                    self.assertIsNone(s.hydra_cfg())
                    self.assertEqual(s._connectors['nzbhydra']['health']['state'], 'disabled')
                    self.assertIsNone(s._connectors['nzbhydra']['next_due'])
                    self.assertEqual(s.nzb_keep(METAS,2), METAS[:2])

    def test_unsafe_hydra_url_disables_availability_without_aborting_load(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.server(temp)
            for url in ('http://hydra.example/api', 'file:///tmp/hydra', '//hydra/api',
                        'http://bad host.local/api', 'http://hydra.local.example/api',
                        'http://[2001:4860::1]:5076/api',
                        'https://user:password@hydra.example/api', 'https://hydra.example/api#fragment',
                        'https://[broken/api'):
                with self.subTest(url=url), patch('urllib.request.urlopen', side_effect=AssertionError('unsafe Hydra reached network')):
                    Path(temp, 'hydra.json').write_text(json.dumps({'url': url, 'key': 'fake'}))
                    s._hydra.clear()
                    with redirect_stdout(io.StringIO()):
                        s.load_state()
                    self.assertIsNone(s.hydra_cfg())
                    self.assertEqual(s._connectors['nzbhydra']['health']['state'], 'disabled')
                    self.assertEqual(s.nzb_keep(METAS, 2), METAS[:2])

    def test_hydra_failure_keeps_titles_and_logs_only_exception_type(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.server(temp)
            Path(temp, 'hydra.json').write_text(json.dumps({'url': 'http://hydra/api', 'key': 'synthetic-key'}))
            s.tmdb = lambda *a, **kw: {'imdb_id': 'tt1'}
            logs = io.StringIO()
            body = b'<error description="SyntheticRemoteSecret" />'
            with patch.object(s, '_connector_open', side_effect=lambda *a, **kw: io.BytesIO(body)), redirect_stdout(logs):
                self.assertEqual(s.nzb_keep(METAS[:1], 1), METAS[:1])
            self.assertEqual(logs.getvalue(), 'nzb tmdb:1: RuntimeError\n')
            self.assertIsNone(s.nzb_state('tmdb:1'))

    def test_absent_documentary_rows_equal_base(self):
        if (BASE is None or not BASE.is_file()) or (SNAPSHOT is None or not Path(SNAPSHOT).is_dir()):
            self.skipTest('snapshot and base copy required')
        outputs = []
        for source in (BASE, Path(__file__).resolve().parents[1]/'server.py'):
            with redirect_stdout(io.StringIO()), home_server(SNAPSHOT,source) as s, \
                 patch('urllib.request.urlopen',side_effect=AssertionError('docs accessed network')) as network:
                outputs.append([{'id': cid, 'type': typ, 'metas': s.row_metas(cid,typ,'de-DE')}
                                for cid, types, _ in s.feed_rows('de-DE') if cid in s.DOCS for typ in types])
                network.assert_not_called()
        self.assertTrue(outputs[0])
        self.assertEqual(outputs[0],outputs[1])


if __name__ == '__main__':
    unittest.main()
