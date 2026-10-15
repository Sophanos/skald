"""Check the small Compose examples offline, without a YAML dependency."""
from pathlib import Path
import re
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'
SETUPS = {'stremio', 'odin-jellyfin', 'existing-aiostreams',
          'reverse-proxy', 'tunnel', 'small-box'}


class ComposeExamplesTests(unittest.TestCase):
    def folders(self):
        self.assertTrue(EXAMPLES.is_dir(), 'missing examples/')
        return {p.name: p for p in EXAMPLES.iterdir() if p.is_dir()}

    def public_files(self):
        for setup in sorted(SETUPS):
            for name in ('compose.yaml', 'env.example'):
                yield EXAMPLES / setup / name
        for name in ('README.md', 'odin-jellyfin/aiostreams.env.example',
                     'tunnel/tunnel.env.example', 'reverse-proxy/Caddyfile'):
            yield EXAMPLES / name

    def test_complete_folders_and_index(self):
        folders = self.folders()
        self.assertEqual(set(folders), SETUPS)
        readme = (EXAMPLES / 'README.md').read_text()
        listed = set(re.findall(r'\[([a-z-]+)\]\(\1/\)', readme))
        self.assertEqual(listed, set(folders))
        for folder in folders.values():
            for name in ('compose.yaml', 'env.example'):
                self.assertTrue((folder / name).is_file(), str(folder / name))

    def test_env_files_and_comments(self):
        for folder in self.folders().values():
            compose = (folder / 'compose.yaml').read_text()
            for name in re.findall(r'^\s+env_file: (\S+)\s*$', compose, re.M):
                example = 'env.example' if name == '.env' else name.lstrip('.') + '.example'
                self.assertTrue((folder / example).is_file(), example)
            for path in folder.glob('*env*.example'):
                lines = path.read_text().splitlines()
                for i, line in enumerate(lines):
                    if re.match(r'[A-Z][A-Z0-9_]*=', line):
                        self.assertGreater(i, 0)
                        self.assertRegex(lines[i - 1], r'^# (Required|Optional)\b')

    def test_env_names_have_a_source(self):
        self.folders()
        # Read only public source and docs, never household inputs or audit notes.
        sources = [ROOT / 'server.py', *sorted((ROOT / 'skald').rglob('*.py'))]
        sources += [ROOT / 'docs' / name for name in
                    ('SETUP.md', 'WATCH.md', 'CONNECTORS.md', 'UPGRADE.md', 'CLI.md')]
        known = '\n'.join(path.read_text() for path in sources)
        for path in self.public_files():
            text = path.read_text()
            names = set(re.findall(r'\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b', text))
            for name in names:
                if not name.startswith('YOUR_'):
                    self.assertIsNotNone(re.search(r'\b' + name + r'\b', known),
                                         f'{path}: undocumented variable {name}')

    def test_no_secret_values(self):
        self.folders()
        for path in self.public_files():
            text = path.read_text()
            for name, value in re.findall(r'^([A-Z][A-Z0-9_]*)=(.*)$', text, re.M):
                if any(word in name for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'TMDB_API')):
                    self.assertTrue(not value.strip(), f'{path}: {name} must be blank')
            # Ignore image digests, but catch key-like literals and private keys.
            text = re.sub(r'@sha256:[0-9a-f]{64}', '', text)
            self.assertTrue(not re.search(r'\b[A-Za-z0-9+/]{40,}={0,2}|\b[0-9a-fA-F]{32,}\b|sk-or-v1-|eyJ[A-Za-z0-9_-]{20,}|BEGIN .*PRIVATE KEY', text),
                            f'{path}: possible secret value')

    def test_skald_hardening_and_build_path(self):
        for folder in self.folders().values():
            compose = (folder / 'compose.yaml').read_text()
            service = re.search(r'^  skald:\n(.*?)(?=^  \w[^\n]*:|^\S|\Z)', compose, re.M | re.S)
            self.assertIsNotNone(service, folder.name)
            block = service[1]
            for setting in ('build: ../..', 'read_only: true', 'tmpfs: /tmp',
                            'user: "65534"', 'cap_drop: [ALL]',
                            'security_opt: [no-new-privileges:true]', './data:/data',
                            'healthcheck:', "http://127.0.0.1:8080/health"):
                self.assertIn(setting, block, folder.name)

    def test_root_files_and_readme_size(self):
        for name in ('compose.yaml', 'compose.selfhost.example.yaml', 'compose.stack.example.yaml'):
            self.assertTrue((ROOT / name).is_file())
            self.assertIn('examples/', (ROOT / name).read_text())
        self.assertLessEqual(len((ROOT / 'README.md').read_text().splitlines()), 260)

    def test_pins_and_port_exposure(self):
        exposed = {'stremio', 'odin-jellyfin', 'small-box', 'reverse-proxy'}
        for name, folder in self.folders().items():
            compose = (folder / 'compose.yaml').read_text()
            self.assertEqual('ports:' in compose, name in exposed)
            for image in re.findall(r'^\s+image: (\S+)', compose, re.M):
                self.assertRegex(image, r'@sha256:[0-9a-f]{64}$|:[0-9][\w.-]+$')
            if name == 'small-box':
                self.assertIn('mem_limit:', compose)
                self.assertIn('cpus:', compose)


class ComposeScannerPrivacyTests(unittest.TestCase):
    def test_scanners_never_read_deployment_files(self):
        private = [EXAMPLES / 'stremio' / '.env',
                   EXAMPLES / 'odin-jellyfin' / '.aiostreams.env',
                   EXAMPLES / 'tunnel' / '.tunnel.env',
                   EXAMPLES / 'stremio' / 'data' / 'private.json',
                   EXAMPLES / 'stremio' / 'credentials' / 'private.json']
        original_walk, original_read, original_file = Path.rglob, Path.read_text, Path.is_file

        def walk(path, pattern):
            return iter(private) if path == EXAMPLES else original_walk(path, pattern)

        def read(path, *args, **kwargs):
            self.assertNotIn(path, private, 'scanner attempted to read a deployment file')
            return original_read(path, *args, **kwargs)

        with patch.object(Path, 'rglob', walk), patch.object(Path, 'read_text', read), \
                patch.object(Path, 'is_file', lambda path: path in private or original_file(path)):
            for name in ('test_env_names_have_a_source', 'test_no_secret_values'):
                with self.subTest(check=name):
                    getattr(ComposeExamplesTests(), name)()

    def test_secret_failures_do_not_echo_values(self):
        original_read = Path.read_text
        for value in ('synthetic-private-value', 'syntheticPrivateValue123' * 3):
            text = 'TMDB_API=' + value + '\n' if '-' in value else value

            def read(path, *args, **kwargs):
                if path == EXAMPLES / 'stremio' / 'env.example':
                    return text
                return original_read(path, *args, **kwargs)

            with self.subTest(value_kind='assignment' if '-' in value else 'literal'):
                with patch.object(Path, 'read_text', read):
                    with self.assertRaises(AssertionError) as error:
                        ComposeExamplesTests().test_no_secret_values()
                self.assertNotIn(value, str(error.exception))


if __name__ == '__main__':
    unittest.main()
