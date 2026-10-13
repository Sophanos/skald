"""Public clone and recommended-stack contracts, entirely offline."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members
from skald.__main__ import main
from eval import replay, rows_report, seed_probes

ROOT = Path(__file__).resolve().parents[1]


class PublicSetupTests(unittest.TestCase):
    def test_existing_legacy_only_and_event_only(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            cfg = members.fresh_config(['eve', 'fin'])
            cfg['addon']['id'] = 'example.existing'
            (data/'legacy.json').write_text(json.dumps(cfg))
            self.assertEqual(members.load_config(temp, {}, readonly=True), (cfg, 'legacy'))
            (data/'legacy.json').unlink()
            for name in ('imports.jsonl', 'feedback.jsonl', 'trakt-eve.history.json'):
                with self.subTest(name=name):
                    (data/name).write_text('{}\n')
                    with self.assertRaises(members.ConfigError):
                        members.load_config(temp, {'SKALD_MEMBERS': 'eve,fin'}, readonly=True)
                    (data/name).unlink()

    def test_shipped_history_reference_and_generic_snapshot_tests(self):
        history = (ROOT/'tests/test_history_connectors.py').read_text()
        self.assertTrue('ops/' not in history)
        self.assertTrue((ROOT/'tests/reference_history.py').is_file())
        cli = (ROOT/'tests/test_cli.py').read_text().split('class CLITests', 1)[1].split('    def test_import_dry_run', 1)[0]
        self.assertNotIn('Castle', cli)
        self.assertNotIn('Grantchester', cli)

    def test_missing_row_order_uses_server_order(self):
        from types import SimpleNamespace
        server = SimpleNamespace(CONFIG={'row_prefix': 'demo'},
                                 feed_rows=lambda lang: [('demo.foryou', ('tv',), 'For you')],
                                 row_metas=lambda *args: [])
        with patch.dict(os.environ, {}, clear=True):
            rows = rows_report.display_rows(server)
        self.assertEqual([(r['id'], r['type']) for r in rows], [('demo.foryou', 'tv')])

    def test_public_probe_default_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(replay, 'ROOT', Path(temp)):
            root = Path(temp)/'eval'
            root.mkdir()
            example = {'probes': [], 'seed_probes': []}
            (root/'probes.example.json').write_text(json.dumps(example))
            self.assertEqual(replay.probe_data('probes.json'), [])
            self.assertEqual(seed_probes.load_probes(None), [])
            path = root/'custom.json'
            path.write_text('[{"custom": true}]')
            self.assertEqual(replay.probe_data('probes.json'), [])
            self.assertEqual(replay.probe_data('probes.json', path), [{'custom': True}])

    def test_public_identity_and_only_requested_prompt_replacements(self):
        source = (ROOT/'server.py').read_text()
        self.assertEqual(members.DEFAULT_ADDON['id'], 'community.skald')
        self.assertEqual(members.LEGACY['addon']['id'], 'community.skald.legacy')
        self.assertIn('Because you loved Paddington 2', source)
        self.assertIn('Witty banter duo like Moonlighting', source)
        self.assertIn("'Paddington', 'dark'", source)

    def test_stack_template(self):
        source = (ROOT/'compose.stack.example.yaml').read_text()
        skald, aio = source.split('  skald:', 1)[1].split('  aiostreams:', 1)
        aio = aio.split('\nvolumes:', 1)[0]
        self.assertNotIn('ports:', skald)
        self.assertIn('127.0.0.1:3000:3000', aio)
        self.assertIn('aiostreams-data:/app/data', aio)
        self.assertIn('env_file: .env.aiostreams', aio)
        self.assertIn('sha256:d2698597d588ce96288d8bf177f7ad2c0d237c33bfba8ca658a5929ad727d4f9', aio)
        self.assertNotIn('SECRET_KEY', source)
        env = (ROOT/'.env.aiostreams.example').read_text()
        self.assertIn('DATABASE_URI=sqlite://./data/db.sqlite', env)
        self.assertIn('JELLYFIN_MAX_LIBRARIES=40', env)
        self.assertEqual({line.split('=')[0] for line in env.splitlines() if line and not line.startswith('#')},
                         {'SECRET_KEY', 'BASE_URL', 'DATABASE_URI', 'JELLYFIN_ENABLED', 'JELLYFIN_MAX_LIBRARIES', 'MANIFEST_CACHE_TTL'})

    def test_stack_setup_internal_token_url_without_public_base(self):
        for watch in ('aiostreams', 'jellyfin-app'):
            with self.subTest(watch=watch), tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, {'DATA_DIR': temp, 'TMDB_API': 'fake', **({'SKALD_INTERNAL_BASE': 'http://skald:8080'} if watch == 'aiostreams' else {})}, clear=True), \
                    patch('skald.setup.check_tmdb', return_value=True):
                args = ['setup', '--yes', '--members', 'eve,fin', '--watch', watch,
                        '--connect', 'files', '--json']
                if watch == 'jellyfin-app':
                    args.extend(['--internal-base', 'http://skald:8080'])
                out = io.StringIO()
                self.assertEqual(main(args, out=out), 0, out.getvalue())
                result = json.loads(out.getvalue())
                with __import__('skald.setup', fromlist=['server']).server(Path(temp)) as s:
                    url = s.addon_urls('http://skald:8080')[0]
                self.assertIn(url.replace(s.addon_token(), '<addon-token>'), '\n'.join(result['lines']))
                self.assertFalse(result['addon_urls_shown'])
                self.assertNotEqual(url, 'http://skald:8080/en-US/manifest.json')
                self.assertEqual(result['next'], 'skald setup aiostreams')

    def test_setup_refuses_event_only_household(self):
        from skald import setup
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data/'imports.jsonl').write_text('{}\n')
            with self.assertRaises(members.ConfigError):
                setup.household(data, ['eve', 'fin'], 'en-US', 'UTC', 'United States')
            self.assertFalse((data/'skald.json').exists())

    def test_stack_boot_names_the_correct_service(self):
        from skald import boot
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_INTERNAL_BASE': 'http://skald:8080'}, clear=True):
            out = io.StringIO()
            with patch.object(boot.os, 'execv'):
                boot.main(out=out)
            self.assertIn('compose.stack.example.yaml exec skald skald setup', out.getvalue())

    def test_empty_public_seed_example_skips_report(self):
        with patch.object(seed_probes, 'load_probes', return_value=[]):
            self.assertIn('skipped', seed_probes.run(None, []))
