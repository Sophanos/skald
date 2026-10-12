"""Unattended household setup, entirely synthetic and offline."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald.__main__ import main, parser
from skald import remote, live, members


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'fake',
                                          'SKALD_SNAPSHOT': '', 'OPENROUTER_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(members.configure)

    def test_fresh_and_resume_without_prompts(self):
        from skald import setup
        out = io.StringIO()
        with patch.object(setup, 'check_tmdb', return_value=True), patch('builtins.input', side_effect=AssertionError('prompt')):
            self.assertEqual(main(['setup', '--yes', '--members', 'alex,sam', '--lang', 'en-US',
                '--timezone', 'UTC', '--country', 'United States', '--base', 'https://tv.example',
                '--watch', 'stremio', '--connect', 'files'], out=out), 0)
            before = (self.data/'skald.json').read_bytes()
            self.assertEqual(main(['setup', '--yes'], out=io.StringIO()), 0)
        self.assertEqual((self.data/'skald.json').read_bytes(), before)
        self.assertTrue((self.data/'.skald-household').exists())
        self.assertIn('Together:', out.getvalue())
        self.assertIn('rows work without it', out.getvalue())
        status = io.StringIO()
        self.assertEqual(main(['setup', '--status', '--json'], out=status), 0)
        self.assertIn('next', json.loads(status.getvalue()))

    def test_finish_includes_existing_doctor_result(self):
        from skald import setup
        out = io.StringIO()
        with patch.object(setup, 'check_tmdb', return_value=True):
            self.assertEqual(main(['setup', '--yes', '--members', 'alex,sam', '--base', 'http://localhost:8080',
                '--connect', 'files', '--json'], out=out), 0)
        result = json.loads(out.getvalue())
        self.assertIn('checksRun', result['doctor'])
        self.assertEqual(result['next'], 'nothing left')

    def test_setup_json_contains_recipe_and_private_urls(self):
        from skald import setup
        out = io.StringIO()
        with patch.object(setup, 'check_tmdb', return_value=True):
            self.assertEqual(main(['setup', '--yes', '--members', 'alex,sam', '--base', 'https://tv.example',
                '--watch', 'aiostreams', '--connect', 'files', '--json'], out=out), 0)
        result = json.loads(out.getvalue())
        self.assertTrue(any('Together:' in line for line in result['lines']))
        self.assertTrue(any('MANIFEST_CACHE_TTL' in line for line in result['lines']))
        manifest = next(line.split(': ', 1)[1] for line in result['lines'] if line.startswith('Household: https://'))
        self.assertTrue(any('http://engine:8080/' + manifest.split('https://tv.example/', 1)[1] in line for line in result['lines']))

    def test_missing_address_keeps_completed_steps(self):
        from skald import setup
        with patch.object(setup, 'check_tmdb', return_value=True):
            self.assertEqual(main(['setup', '--yes', '--members', 'alex,sam'], out=io.StringIO()), 2)
        result = setup.status(self.data)
        self.assertEqual(result['missing'][0], 'address')

    def test_interactive_checks_print_before_questions(self):
        from skald import setup
        answers = iter(['alex,sam', 'en-US', 'UTC', 'United States', 'https://tv.example', 'stremio', 'files'])
        out = io.StringIO()
        def answer(prompt):
            print(prompt, file=out)
            return next(answers)
        with patch.object(setup, 'check_tmdb', return_value=True), patch('builtins.input', side_effect=answer):
            self.assertEqual(main(['setup'], out=out), 0)
        self.assertLess(out.getvalue().index('Checks:'), out.getvalue().index('Household names'))

    def test_member_removal_with_data_refused_and_add_preserves_config(self):
        from skald import setup
        cfg = members.fresh_config(['alex', 'sam'])
        cfg['extra'] = {'keep': True}
        (self.data/'skald.json').write_text(json.dumps(cfg))
        (self.data/'imports.jsonl').write_text('{"who":"sam"}\n')
        with self.assertRaisesRegex(ValueError, 'sam.*data'):
            setup.household(self.data, ['alex'], 'en-US', 'UTC', 'United States')
        result = setup.household(self.data, ['alex', 'sam', 'jo'], None, None, None)
        self.assertEqual(result['extra'], {'keep': True})

    def test_legacy_readonly_and_unsafe_data(self):
        from skald import setup
        (self.data/'legacy.json').write_text(json.dumps(members.fresh_config(['alex'])))
        with self.assertRaisesRegex(ValueError, 'read-only'):
            setup.household(self.data, ['sam'], None, None, None)
        self.data.chmod(0o777)
        with self.assertRaisesRegex(ValueError, '0700'):
            setup.check_data(self.data)

    def test_legacy_cli_does_not_write_setup_state(self):
        from skald import setup
        (self.data/'legacy.json').write_text(json.dumps(members.fresh_config(['alex'])))
        with patch.object(setup, 'check_tmdb', return_value=True):
            self.assertEqual(main(['setup', '--yes', '--base', 'https://tv.example'], out=io.StringIO()), 2)
        self.assertEqual({p.name for p in self.data.iterdir()}, {'legacy.json'})

    def test_check_tmdb_uses_helper_and_injected_transport(self):
        from skald import setup
        from history_kit import Reply
        calls = []
        def transport(req, timeout):
            calls.append(req.full_url)
            return Reply({'images': {}})
        self.assertTrue(setup.check_tmdb(self.data, transport=transport))
        self.assertEqual(len(calls), 1)
        self.assertIn('/3/configuration?', calls[0])

    def test_link_calls_existing_token_logic(self):
        from skald import setup
        (self.data/'skald.json').write_text(json.dumps(members.fresh_config(['alex', 'sam'])))
        with setup.server(self.data) as s:
            with patch.object(s, 'new_client', wraps=s.new_client) as new:
                result = setup.link(s, 'laptop', 'alex', ['read', 'decide', 'events'], 'https://api.example')
                new.assert_called_once_with('laptop', ['read', 'decide', 'events'], 'alex')
        self.assertIn('skald connect https://api.example', '\n'.join(result['lines']))
        self.assertNotIn(result['token'], (self.data/'clients.json').read_text())


class LaptopTests(unittest.TestCase):
    def test_connect_private_storage_and_overrides(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=Path(home)), \
                patch('getpass.getpass', return_value='fake_token'):
            out = io.StringIO()
            self.assertEqual(main(['connect', 'https://api.example', '--json'], out=out), 0)
            folder = Path(home)/'.config/skald'
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            for name in ('api', 'token'):
                self.assertEqual((folder/name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(remote.settings(), ('https://api.example', 'fake_token'))
            with patch.dict(os.environ, {'SKALD_API': 'http://other.test', 'SKALD_TOKEN': 'override'}):
                self.assertEqual(remote.settings(), ('http://other.test', 'override'))
            self.assertNotIn('fake_token', out.getvalue())

    def test_cold_laptop_can_read_export_instructions(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'DATA_DIR': folder, 'SKALD_SNAPSHOT': ''}, clear=True):
            out = io.StringIO()
            self.assertEqual(main(['onboard', '--sources', '--json'], out=out), 0)
            self.assertIn('imdb', [r['name'] for r in json.loads(out.getvalue())['sources']])

    def test_cold_remote_import_needs_no_snapshot_or_ssh(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'DATA_DIR': folder, 'SKALD_SNAPSHOT': ''}, clear=True):
            export = Path(folder)/'titles.txt'
            export.write_text('Synthetic title\n')
            out = io.StringIO()
            from skald.ingest.resolve import Resolver
            reply = {"results": [Resolver(lambda *a, **kw: {}, cache=Path(folder)/'resolve.json').resolution(
                {"id": 42, "title": "Synthetic title", "year": "2000", "kind": "movie"}, 1, "search")]}
            with patch.object(remote, 'request', return_value=reply) as lookup, \
                    patch('skald.live.pull', side_effect=AssertionError('ssh forbidden')):
                self.assertEqual(main(['--state-dir', str(Path(folder)/'state'), 'import', 'plain', str(export),
                    '--person', 'alex', '--remote', '--dry-run', '--json'], out=out), 0, out.getvalue())
            lookup.assert_called_once_with('/api/v1/resolve', {'items': [
                {'title': 'Synthetic title', 'year': '', 'kind': '', 'ids': {}}]})
            self.assertEqual(len(json.loads(out.getvalue())['events']), 1)
            self.assertEqual(json.loads(out.getvalue())['candidates'], 1)

    def test_persistent_state_and_explicit_live_host(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': '/tmp/synthetic-data', 'SKALD_SSH_HOST': ''}):
            self.assertEqual(parser().parse_args(['me']).state_dir, '/tmp/synthetic-data/skald')
            with self.assertRaisesRegex(ValueError, 'SKALD_SSH_HOST'):
                live.command('snapshot')
