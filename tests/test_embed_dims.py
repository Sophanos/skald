"""Synthetic copies and transports only."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from eval import embed_dims, embed_eval, replay
from skald.engine import Engine
from skald.members import fresh_config


class EmbedDimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'copy'
        self.data.mkdir()
        entries = [dict(key=f'tmdb:{i}', id=i, kind='movie', title=f'Synthetic {i}',
                        year='2000', signal='loved', weight=2., age=0, who='told',
                        by=['alex', 'sam'], flags=[], genres=[], item={}) for i in range(1, 7)]
        self.vectors = {f'tmdb:{i}': [0.1, 1.] for i in range(1, 41)}
        self.vectors['tmdb:7'] = [1., 0.]
        encoded = {k: base64.b64encode(struct.pack('2e', *v)).decode() for k, v in self.vectors.items()}
        docs = {'skald.json': fresh_config(['alex', 'sam']),
                '.skald-format': {'format': 5, 'release': 'synthetic'},
                'profile.json': {'entries': entries, 'seen': [e['key'] for e in entries]},
                'emb.json': {'model': 'synthetic-baseline', 'dim': 2, 'v': encoded},
                'facets.json': {'items': {k: {'kind': 'movie', 'title': f'Synthetic {k[5:]}',
                                            'g': []} for k in encoded}}}
        for name, doc in docs.items():
            (self.data / name).write_text(json.dumps(doc))
        self.file = Path(self.temp.name) / 'vectors.json'
        candidate = {**encoded, 'tmdb:7': encoded['tmdb:8'], 'tmdb:8': encoded['tmdb:7']}
        self.file.write_text(json.dumps({'model': 'synthetic-candidate', 'dims': 2, 'v': candidate}))

    def test_truncation_renormalises(self):
        self.assertEqual(embed_dims.normalise([3., 4., 12.], 2), [0.6, 0.8])
        self.assertAlmostEqual(math.hypot(*embed_dims.normalise([3., 4., 12.], 2)), 1.)
        with self.assertRaises(ValueError):
            embed_dims.normalise([0., 0.], 1)

    def test_compare_refuses_repository_output(self):
        with self.assertRaises(ValueError):
            embed_dims.compare(self.data, self.file, [2], [0., .3], replay.ROOT / 'eval/out/private')

    def test_baseline_and_offline_report(self):
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        output, out = io.StringIO(), Path(self.temp.name) / 'report'
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
            self.assertEqual(embed_dims.main(['compare', '--data', str(self.data), '--vectors', str(self.file),
                '--dims', '1,2', '--weights', '0,.3', '--out', str(out), '--network', 'none']), 0)
        result = json.loads((out / 'metrics.json').read_text())
        self.assertEqual(result['arms'][0]['model'], 'synthetic-baseline')
        self.assertEqual(result['arms'][0]['dims'], 2)
        self.assertEqual(result['arms'][0]['titles'], 40)
        self.assertEqual(set(result['arms'][0]['people']), {'alex', 'sam'})
        self.assertIn('ci95', result['arms'][1]['delta']['recall@30'])
        self.assertIn('Screen view', (Path(self.temp.name) / 'report/report.md').read_text())
        self.assertNotIn('Synthetic', output.getvalue())
        self.assertEqual((out / 'metrics.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_embedding_weight_changes_production_order(self):
        with Engine(self.data) as engine:
            s = engine._server
            embed_dims.install(s, embed_dims.read_vectors(self.data / 'emb.json'), 2)
            model = replay.build_contexts(s, engine._profile, [replay.variants.REGISTRY['current']],
                                          replay.SEED)['current'].model
            orders = []
            for weight in (0., .3):
                s.SCORE_BLEND = (weight, *s.SCORE_BLEND[1:])
                orders.append(replay.ranking(lambda k, p=None: s.score_key(model, k)[0], ['tmdb:7', 'tmdb:8']))
            self.assertEqual(orders, [['tmdb:7', 'tmdb:8'], ['tmdb:8', 'tmdb:7']])

    def test_compare_accepts_legacy_baseline_without_model(self):
        path = self.data / 'emb.json'
        doc = json.loads(path.read_text())
        del doc['model']
        doc['dim'] = 256
        doc['v'] = {k: base64.b64encode(struct.pack('256e', *v, *([0.] * 254))).decode()
                    for k, v in self.vectors.items()}
        path.write_text(json.dumps(doc))
        before = path.read_bytes()
        out = Path(self.temp.name) / 'legacy-report'
        result = embed_dims.compare(self.data, self.file, [2], [0.], out)
        self.assertEqual(result['arms'][0]['model'], 'openai/text-embedding-3-small')
        self.assertEqual(result['arms'][0]['dims'], 256)
        self.assertTrue((out / 'report.md').is_file())
        self.assertEqual(path.read_bytes(), before)

    def test_compare_excludes_internal_slots_from_replay_and_screen(self):
        profile = json.loads((self.data / 'profile.json').read_text())
        profile['entries'][0].update(kind='tv', signal='watched')
        (self.data / 'profile.json').write_text(json.dumps(profile))
        facets = json.loads((self.data / 'facets.json').read_text())
        slots = {'tmdb:1@tv': 'Watched synthetic TV', 'tmdb:7@tv': 'Unseen synthetic TV'}
        facets['items'].update({k: {'kind': 'tv', 'title': title, 'g': []} for k, title in slots.items()})
        (self.data / 'facets.json').write_text(json.dumps(facets))
        for path in (self.data / 'emb.json', self.file):
            doc = json.loads(path.read_text())
            doc['v'].update({k: base64.b64encode(struct.pack('2e', 1., 0.)).decode() for k in slots})
            doc['kinds'] = {k: 'tv' for k in slots}
            path.write_text(json.dumps(doc))
        result = embed_dims.compare(self.data, self.file, [2], [0., .3], Path(self.temp.name) / 'slots-report')
        for block in result['arms']:
            self.assertTrue(all(row['candidates'] == 35 for row in block['rows']))
        for views in result['screen'].values():
            for view in views.values():
                for keys in (view['top15'], *view['because'].values()):
                    self.assertNotIn('tmdb:1', keys)
                    self.assertTrue(all('@' not in k for k in keys))
        with Engine(self.data) as engine:
            embed_dims.install(engine._server, embed_dims.read_vectors(self.data / 'emb.json'), 2)
            self.assertEqual(list(engine._server.femb('tmdb:1', 'tv')), [1., 0.])

    def test_embed_copy_unchanged_and_error_sanitised(self):
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        argv = ['embed', '--data', str(self.data), '--out', str(self.file), '--model', 'synthetic',
                '--max-dims', '3072', '--check-truncation', '2', '--limit', '3']
        output, errors, calls = io.StringIO(), io.StringIO(), []
        def faithful(url, payload, key):
            calls.append(payload['dimensions'])
            response = embed_eval.fixture_transport(url, {**payload, 'dimensions': 3072}, key)
            for row in response['data']:
                row['embedding'] = row['embedding'][:payload['dimensions']]
            response['usage']['total_tokens'] = 12
            return response
        with patch.dict('os.environ', {'OR_KEY': 'synthetic-secret'}), \
                patch.object(embed_dims.TextEngine, '_title_text', return_value='Synthetic text'), \
                patch.object(embed_dims.time, 'sleep') as sleep, redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(embed_dims.main(argv, transport=faithful), 0)
            doc = json.loads(self.file.read_text())
            self.assertAlmostEqual(doc['truncation_cosine'], 1.)
            self.assertEqual(doc['tokens'], 24)
            self.assertEqual(calls, [3072, 768])
            saved = self.file.read_bytes()
            def failed(*args):
                raise ValueError('synthetic-secret')
            self.assertEqual(embed_dims.main(argv, transport=failed), 1)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(saved, self.file.read_bytes())
        self.assertNotIn('synthetic-secret', output.getvalue() + errors.getvalue())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_embed_uses_stored_facets_without_tmdb(self):
        facets = json.loads((self.data / 'facets.json').read_text())
        for k, rec in facets['items'].items():
            rec.update(y='2001', g=['Drama', 'Comedy'], k=[[2, 'heist'], [1, 'friendship']], o=f'Plot of {k}.')
        (self.data / 'facets.json').write_text(json.dumps(facets))
        out, texts = Path(self.temp.name) / 'facet-vectors.json', []
        def transport(url, payload, key):
            texts.extend(payload['input'])
            return embed_eval.fixture_transport(url, payload, key)
        with patch.dict('os.environ', {'OR_KEY': 'synthetic-secret'}), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(embed_dims.main(['embed', '--data', str(self.data), '--out', str(out), '--model', 'synthetic',
                                              '--max-dims', '8', '--limit', '3'], transport=transport), 0)
        self.assertEqual(json.loads(out.read_text())['counts']['embedded'], 3)
        self.assertEqual(texts[0], 'Synthetic 1 (2001), movie. Genres: Drama, Comedy. Keywords: heist, friendship. '
                                   'Plot of tmdb:1.')
        with Engine(self.data) as engine:
            s = engine._server
            self.assertEqual(s.title_text('movie', '1'), texts[0])
            self.assertEqual(s.title_text('movie', 1), texts[0])
