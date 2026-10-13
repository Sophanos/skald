"""Synthetic worker checks; container launch is checked without a daemon."""
import base64
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local_run = subprocess.run
        self.source = self.root / 'source'
        self.source.mkdir()
        shutil.copyfile(ROOT / 'server.py', self.source / 'server.py')
        for path in (ROOT / 'skald').rglob('*.py'):
            dest = self.source / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        shutil.copytree(ROOT / 'skald/locales', self.source / 'skald/locales')
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        cfg = {'v': 1, 'household': {'members': ['ana', 'ben']},
               'taste': {}, 'region': {'lang': 'en-US', 'tz': 'UTC', 'country': 'US'},
               'addon': {'id': 'synthetic', 'name': 'Synthetic', 'version': '1', 'legacy_open': True},
               'history': {'source': 'file'}, 'connectors': {}}
        details, facets, vectors, history = {}, {}, {}, []
        for i in range(1, 47):
            key = f'tmdb:{i}'
            history.append({'id': key, 'type': 'movie', 'at': 1791734400000,
                            'progress': 1., 'played': True, 'name': 'Synthetic'})
            details[key] = ['movie', {'id': i, 'title': 'Synthetic', 'genres': []}]
            facets[key] = {'kind': 'movie', 'x': {}, 'g': []}
            vectors[key] = base64.b64encode(struct.pack('256e', 1., *([0.] * 255))).decode()
        for name, value in {'skald.json': cfg, 'history.json': {'items': history[:45]},
                            'trakt.json': {'items': []}, 'taste.json': {'people': {}},
                            'facets.json': {'items': facets},
                            'emb.json': {'dim': 256, 'model': 'synthetic', 'v': vectors,
                                         'kinds': {k: 'movie' for k in vectors}},
                            'cache.json': {'details': details, 'tmdb': [
                                {'path': f'trending/{kind}/week',
                                 'params': {'language': 'en-US', 'page': page}, 'value': {'results': []}}
                                for kind in ('movie', 'tv') for page in (1, 2)]}}.items():
            (self.inputs / name).write_text(json.dumps(value))
        self.request = {'now': 1791734400., 'seed': 7, 'timezone': 'UTC',
                        'candidates': [['tmdb:46', 'movie']]}

    def worker(self, env=None):
        from eval.household import runner
        scratch = self.root / ('scratch' + str(len(list(self.root.glob('scratch*')))))
        scratch.mkdir(mode=0o700)
        return subprocess.run([sys.executable, '-I', str(Path(runner.__file__).resolve()),
                               '--worker', str(self.source), str(self.inputs), str(scratch),
                               '.'.join(map(str, sys.version_info[:3]))],
                              input=json.dumps(self.request), text=True, capture_output=True,
                              env=env, timeout=30)

    def test_source_hash_includes_bundled_translations(self):
        from eval.household import runner
        before = runner.source_hash(self.source)
        strings = self.source / 'skald/locales/en.json'
        strings.write_text(strings.read_text() + '\n')
        self.assertNotEqual(runner.source_hash(self.source), before)

    def launch_local(self, command, **kwargs):
        mounts = [arg for arg in command if arg.startswith('type=bind,')]
        paths = {arg.split('dst=')[1].split(',')[0]:
                 arg.split('src=')[1].split(',')[0] for arg in mounts}
        scratch = self.root / ('pair-scratch' + str(len(list(self.root.glob('pair-scratch*')))))
        scratch.mkdir()
        return self.local_run([sys.executable, '-I', paths['/harness'] + '/eval/household/runner.py',
                               '--worker', paths['/source'], paths['/input'], str(scratch),
                               '.'.join(map(str, sys.version_info[:3]))], **kwargs)

    def test_cached_embedding_still_changes_score_offline(self):
        baseline = json.loads(self.worker().stdout)
        code = (self.source / 'server.py').read_text()
        (self.source / 'server.py').write_text(code.replace('SCORE_BLEND = (0, 0, .3)',
                                                           'SCORE_BLEND = (1, 0, .3)'))
        candidate = json.loads(self.worker().stdout)
        self.assertEqual(baseline['code'], 'OK')
        self.assertEqual(candidate['code'], 'OK')
        self.assertTrue(candidate['vector'])
        self.assertEqual(candidate['entries'], 40)
        self.assertGreater(candidate['scores'][0], baseline['scores'][0])
        cache = (self.inputs / 'cache.json').read_text()
        (self.inputs / 'cache.json').write_text(json.dumps({'details': json.loads(cache)['details'], 'tmdb': []}))
        self.assertEqual(json.loads(self.worker().stdout)['code'], 'MISSING_INPUT')
        (self.inputs / 'cache.json').write_text(cache)
        (self.inputs / 'emb.json').unlink()
        self.assertEqual(json.loads(self.worker().stdout)['code'], 'MISSING_INPUT')

    def test_frozen_snapshot_file_and_directory_reach_workers(self):
        from eval.household import runner, snapshot
        names = ['skald.json', 'history.json', 'trakt.json', 'emb.json',
                 'facets.json', 'cache.json', 'taste.json']
        frozen = snapshot.freeze(self.inputs, self.root / 'frozen', inputs=names,
                                 generation=lambda: 1, enabled=True)
        before = frozen.read_bytes()

        for path in (frozen, frozen.parent):
            with self.subTest(path=path.name), patch.object(runner.subprocess, 'run', side_effect=self.launch_local):
                result = runner.run_pair(self.source, self.source, path, self.request,
                    enabled=True, image='sha256:' + 'a' * 64, python_version='3.12.13')
                self.assertEqual(result['code'], 'OK')
                self.assertEqual(result['a']['entries'], 40)
                self.assertEqual(result['a'], result['b'])
        self.assertEqual(frozen.read_bytes(), before)

    def test_worker_replays_final_rows_from_feeds_and_pins(self):
        from eval.household import runner, snapshot
        from skald.members import rid
        cfg = json.loads((self.inputs / 'skald.json').read_text())
        cfg['row_prefix'] = 'synthetic'
        cfg['household']['rows'] = ['moods']
        (self.inputs / 'skald.json').write_text(json.dumps(cfg))
        cid = rid('mood1', cfg)
        self.request['order'] = [[cid, 'tv']]
        metas = [{'id': f'tmdb:{i}', 'name': f'Synthetic {i} Card', 'type': 'movie',
                  'poster': 'p', 'background': 'b',
                  '_row': {'g': ['Drama'], 'rating': 8, 'votes': 1000, 'date': '2020-01-01'}}
                 for i in range(46, 62)]
        facets = json.loads((self.inputs / 'facets.json').read_text())
        facets['items'].update({m['id']: {'kind': 'movie', 'g': ['Drama'],
                                         'x': {'feel_good': 2}} for m in metas})
        (self.inputs / 'facets.json').write_text(json.dumps(facets))
        emb = json.loads((self.inputs / 'emb.json').read_text())
        emb['v'].update({m['id']: emb['v']['tmdb:46'] for m in metas})
        emb['kinds'].update({m['id']: 'movie' for m in metas})
        (self.inputs / 'emb.json').write_text(json.dumps(emb))
        for name in ('feeds.json', 'shown.json', 'shown_scopes.json'):
            with self.subTest(state=name):
                record = {cid + '|en-US': {'metas': {'tv': metas}, 'names': {'mood': 'cozy'}}}
                if name == 'shown_scopes.json':
                    record = {'ana': record}
                    self.request['scope'] = 'ana'
                (self.inputs / name).write_text(json.dumps(record))
                self.assertTrue(runner._state(name))
                value = json.loads(self.worker().stdout)
                self.assertEqual(value['code'], 'OK')
                row = next(r for r in value['rows'] if r['id'] == cid)
                self.assertTrue(row['metas'])
                self.assertTrue({m['id'] for m in row['metas']} <= {m['id'] for m in metas})
                self.assertEqual(value['rankings'][cid + ':tv'], [m['id'] for m in row['metas']])
                self.assertEqual(value['row_specs'], [{'id': cid + ':tv', 'task': 'discovery', 'top10': False}])
                frozen = snapshot.freeze(self.inputs, self.root / ('frozen-' + name),
                    inputs=[p.name for p in self.inputs.iterdir()], generation=lambda: 1, enabled=True)
                for path in (self.inputs, frozen):
                    with patch.object(runner.subprocess, 'run', side_effect=self.launch_local):
                        pair = runner.run_pair(self.source, self.source, path, self.request,
                            enabled=True, image='sha256:' + 'a' * 64, python_version='3.12.13')
                    self.assertEqual(pair['code'], 'OK')
                    self.assertEqual(pair['a']['rows'], value['rows'])
                    self.assertEqual(pair['a']['rankings'], value['rankings'])
                    self.assertEqual(pair['a'], pair['b'])
                (self.inputs / name).unlink()

    def test_archived_history_is_filtered_before_first_40_cap(self):
        cfg = json.loads((self.inputs / 'skald.json').read_text())
        cfg['household']['archived'] = {'old': 'Archived'}
        (self.inputs / 'skald.json').write_text(json.dumps(cfg))
        baseline = json.loads(self.worker().stdout)
        history = json.loads((self.inputs / 'history.json').read_text())
        history['items'] = [dict(history['items'][0], id=f'tmdb:{1000 + i}', profile='old')
                            for i in range(40)] + history['items']
        (self.inputs / 'history.json').write_text(json.dumps(history))
        self.assertEqual(json.loads(self.worker().stdout), baseline)

    def test_inherited_config_cannot_escape_snapshot(self):
        env = dict(os.environ, SKALD_CONFIG='/outside/forbidden', DATA_DIR='/outside',
                   SKALD_TZ='invalid', PYTHONPATH='/outside')
        proc = self.worker(env)
        self.assertEqual(json.loads(proc.stdout)['code'], 'OK')
        self.assertEqual(proc.stderr, '')
        from eval.household import runner
        before = runner.source_hash(self.source)
        module = self.source / 'skald' / '__init__.py'
        module.write_text(module.read_text() + '\n')
        self.assertNotEqual(before, runner.source_hash(self.source))
        self.assertEqual(runner.run_pair(None, None, None, None), {'code': 'DISABLED'})
        result = subprocess.CompletedProcess([], 0, proc.stdout, '')
        with patch.object(runner.subprocess, 'run', return_value=result) as launch:
            pair = runner.run_pair(self.source, self.source, self.inputs, self.request,
                                   enabled=True, image='sha256:' + 'a'*64, python_version='3.12.13')
        self.assertEqual(pair['code'], 'OK')
        self.assertEqual(launch.call_count, 2)
        for call in launch.call_args_list:
            command = call.args[0]
            self.assertIn('--network=none', command)
            self.assertIn('--read-only', command)
            self.assertIn('--pull=never', command)
            self.assertEqual(sum(arg.endswith(',readonly') for arg in command), 3)
            self.assertEqual(set(call.kwargs['env']), {'PATH'})
        self.assertNotEqual(launch.call_args_list[0].args[0], launch.call_args_list[1].args[0])

    def test_worker_output_contains_no_private_text(self):
        path = self.source / 'server.py'
        path.write_text("import os, sys\nprint('PRIVATE_SENTINEL')\n"
                        "os.write(2, b'PRIVATE_SENTINEL')\nraise ValueError('PRIVATE_SENTINEL')\n")
        proc = self.worker()
        self.assertEqual(proc.stderr, '')
        self.assertEqual(json.loads(proc.stdout), {'code': 'WORKER_ERROR'})
        self.assertNotIn('PRIVATE_SENTINEL', proc.stdout)
        from eval.household import runner
        with patch.object(runner.subprocess, 'run', side_effect=OSError('PRIVATE_SENTINEL')):
            self.assertEqual(runner.run_pair(self.source, self.source, self.inputs,
                             self.request, enabled=True, image='sha256:' + 'a'*64,
                             python_version='3.12.12')['code'], 'BOUNDARY_UNAVAILABLE')
