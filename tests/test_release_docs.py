"""Public setup instructions describe the shipped Docker build and shared recipes."""
from pathlib import Path
import unittest
from skald import recipes

ROOT = Path(__file__).resolve().parents[1]


class ReleaseDocs(unittest.TestCase):
    def test_readme_links_and_plain_quickstart(self):
        text = (ROOT/'README.md').read_text()
        self.assertLessEqual(len(text.splitlines()), 120)
        for name in ('SETUP', 'WATCH', 'CONNECTORS', 'UPGRADE', 'CLI'):
            self.assertIn('docs/' + name + '.md', text)
        self.assertIn('docker compose exec engine skald setup', text)
        self.assertNotIn('Setup A', text)
        self.assertNotIn('seed id', text)

    def test_upgrade_build_matches_compose(self):
        text = (ROOT/'docs/UPGRADE.md').read_text()
        commands = ('backup', 'git pull', 'docker compose build', 'docker compose up -d', 'skald setup --status')
        self.assertEqual(sorted(commands, key=text.index), list(commands))

    def test_watch_exact_shared_text(self):
        self.assertEqual((ROOT/'docs/WATCH.md').read_text(), recipes.watch_doc())

    def test_recommended_setup_prepares_private_data_first(self):
        command = 'mkdir -p data && sudo chown 65534 data && sudo chmod 700 data'
        for name in ('README.md', 'docs/WATCH.md', 'docs/SETUP.md'):
            with self.subTest(name=name):
                text = (ROOT/name).read_text()
                if name == 'README.md':
                    text = text.split('## Recommended: the setup we run', 1)[1]
                self.assertIn(command, text)
                self.assertLess(text.index(command), text.index('docker compose -f compose.stack.example.yaml'))
