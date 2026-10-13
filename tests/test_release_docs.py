"""Public setup instructions describe the shipped Docker build and shared recipes."""
from pathlib import Path
import unittest
from skald import recipes

ROOT = Path(__file__).resolve().parents[1]


class ReleaseDocs(unittest.TestCase):
    def test_taste_doc_explains_adaptation_after_how_it_works(self):
        text = (ROOT / 'docs/TASTE.md').read_text()
        start = text.index('## What changes as you watch')
        self.assertLess(text.index('## How it works'), start)
        section = text[start:text.index('\n## ', start + 1)]
        self.assertIn('| What happens | What Skald changes | How soon |', section)
        self.assertIn('OpenRouter', section)
        self.assertNotIn('\u2014', section)
        self.assertGreaterEqual(len([line for line in section.splitlines() if line.startswith('| ')]) - 1, 8)

    def test_readme_person_token_skips_other_members(self):
        text = (ROOT / 'README.md').read_text()
        self.assertIn("other members' titles are skipped", text)
        self.assertNotIn("pushing another member's imports fails with 403", text)

    def test_readme_links_and_plain_quickstart(self):
        text = (ROOT/'README.md').read_text()
        self.assertLessEqual(len(text.splitlines()), 220)
        for name in ('SETUP', 'WATCH', 'CONNECTORS', 'UPGRADE', 'CLI', 'ROWS', 'SEARCH', 'TASTE', 'PRIVACY'):
            self.assertIn('docs/' + name + '.md', text)
        self.assertIn('docker compose exec engine skald setup', text)
        self.assertNotIn('Setup A', text)
        self.assertNotIn('seed id', text)

    def test_readme_shows_home_search_and_member_controls(self):
        text = (ROOT/'README.md').read_text()
        self.assertIn('skald setup --connect stremio --person NAME', (ROOT/'docs/CONNECTORS.md').read_text())
        self.assertIn('skald setup prefs --person NAME', (ROOT/'docs/ROWS.md').read_text())
        self.assertIn('images/skald-import-preview.svg', (ROOT/'docs/TASTE.md').read_text())
        for image in ('skald-odin.jpg', 'skald-home.jpg', 'skald-search.jpg'):
            self.assertIn('docs/images/' + image, text)
        self.assertNotIn('addons cannot read', text)

    def test_readme_starts_with_install_and_safe_import_steps(self):
        text = (ROOT/'README.md').read_text()
        headings = ('## Install', '### 1. Pick your setup', '### 2. Connect AI (optional)', '### 3. Quickstart',
                    '## How Skald learns', '## Sources', '## Rows', '## Talk to it from the search box',
                    '## Your data', '## Docs', '## License')
        self.assertEqual(sorted(headings, key=text.index), list(headings))
        self.assertLess(len(text[:text.index('## Install')].splitlines()), 35)
        commands = ('python3 -m skald connect',
                    'python3 -m skald import imdb ratings.csv --person NAME --remote',
                    'python3 -m skald push imports --remote\n',
                    'python3 -m skald push imports --remote --apply')
        self.assertEqual(sorted(commands, key=text.index), list(commands))
        self.assertIn('saves only on the laptop', text)
        self.assertNotIn('--dry-run', text)
        self.assertNotIn('--yes', text)
        for fact in ('stored on your machine', 'docs/PRIVACY.md', 'Keep addon links, tokens, data and backups private',
                     "Anyone with the household addon link can choose any member's rows"):
            self.assertIn(fact, text)
        privacy = (ROOT/'docs/PRIVACY.md').read_text()
        for fact in ('Raw exports stay on the laptop', 'title candidates', 'resolved signals',
                     'Keep addon links, tokens, data and backups private',
                     "Anyone with the household addon link can choose any member's rows",
                     'TMDB', 'OpenRouter', 'AniList', 'NZBHydra', 'AIOStreams', 'Telegram'):
            self.assertIn(fact, privacy)
        for block in text.split('```text\n')[1:]:
            diagram = block.split('```', 1)[0].splitlines()
            self.assertLessEqual(len(diagram), 14)
            self.assertTrue(all(len(line) <= 72 for line in diagram))
        diagram = text.split('## Sources', 1)[1].split('```text\n', 1)[1].split('```', 1)[0]
        for source in ('Stremio', 'Trakt', 'MDBList', 'Jellyfin', 'Plex', 'AIOStreams',
                       'Netflix', 'IMDb', 'Letterboxd', 'YouTube', 'Chrome', 'Trakt zip', 'Plain text', 'Together'):
            self.assertIn(source, diagram)

    def test_public_readme_docs_links_and_table_spacing(self):
        import re
        for name in ('README.md', 'docs/ROWS.md', 'docs/SEARCH.md', 'docs/TASTE.md', 'docs/PRIVACY.md', 'docs/CONNECTORS.md', 'docs/CLI.md'):
            with self.subTest(name=name):
                path = ROOT/name
                text = path.read_text()
                self.assertNotIn(chr(0x2014), text)
                for target in re.findall(r'\]\(([^)]+)\)|src="([^"]+)"', text):
                    link = next(part for part in target if part)
                    if '://' in link or link.startswith('#'):
                        continue
                    self.assertNotIn('eval/README.md', link)
                    self.assertNotIn('ops/', link)
                    self.assertNotIn('briefs/', link)
                    for private in ('HANDOVER', 'A1-NETFLIX-AUDIT', 'OPUS-REVIEW', 'skald-luna-notes'):
                        self.assertNotIn(private, link)
                    self.assertTrue((path.parent/link.split('#', 1)[0]).exists(), link)
                lines = text.splitlines()
                for before, after in zip(lines, lines[1:]):
                    if before.startswith('|') and not after.startswith('|'):
                        self.assertEqual(after, '', 'A table needs a blank line after it')
        search = (ROOT/'README.md').read_text().split('## Talk to it from the search box', 1)[1].split('## Your data', 1)[0]
        rows = [line for line in search.splitlines() if line.startswith('|')][2:]
        self.assertLessEqual(len(rows), 7)
        self.assertTrue(all(len(row.split('|')[1].strip()) <= 35 for row in rows))

    def test_upgrade_build_matches_compose(self):
        text = (ROOT/'docs/UPGRADE.md').read_text()
        commands = ('backup', 'git pull', 'docker compose build', 'docker compose up -d', 'skald setup --status')
        self.assertEqual(sorted(commands, key=text.index), list(commands))

    def test_upgrade_rollback_replaces_data_and_uses_new_verifier(self):
        raw = (ROOT / 'docs/UPGRADE.md').read_text()
        text = ' '.join(raw.split())
        for instruction in ('external `SKALD_CONFIG` file separately', 'empty folder',
                            'replace the DATA folder', 'owner and permissions',
                            "new release's `skald backup restore`", 'before switching back',
                            'older verifier rejects its manifest'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, text)
        self.assertNotIn('--dry-run', text.split('To roll back,', 1)[1])
        self.assertLessEqual(len(raw.splitlines()), 55)

    def test_watch_includes_server_lookup_recipe(self):
        self.assertIn('--remote --dry-run', recipes.watch_doc())
        self.assertIn('The laptop needs no TMDB key', recipes.watch_doc())

    def test_file_onboarding_uses_trakt_zip(self):
        self.assertIn('trakt export.zip', recipes.FILES)
        self.assertNotIn('trakt export-folder', recipes.FILES)
        self.assertIn('trakt export.zip', recipes.watch_doc())

    def test_watch_exact_shared_text(self):
        self.assertEqual((ROOT/'docs/WATCH.md').read_text(), recipes.watch_doc())

    def test_watch_explains_stremio_history(self):
        for name in ('stremio', 'aiostreams'):
            with self.subTest(name=name):
                text = recipes.RECIPES[name]
                self.assertIn('Stremio viewing reaches Skald directly', text)
                self.assertIn('skald setup --connect trakt', text)
                self.assertIn('skald setup --connect stremio --person NAME', text)
        self.assertIn('tested by us', recipes.RECIPES['aiostreams'].splitlines()[0])
        self.assertNotIn('not yet tested', recipes.RECIPES['aiostreams'].splitlines()[0])

    def test_recommended_setup_prepares_private_data_first(self):
        command = 'mkdir -p data && sudo chown 65534 data && sudo chmod 700 data'
        for name in ('README.md', 'docs/WATCH.md', 'docs/SETUP.md'):
            with self.subTest(name=name):
                text = (ROOT/name).read_text()
                if name == 'README.md':
                    text = text.split('### 3. Quickstart', 1)[1]
                self.assertIn(command, text)
                self.assertLess(text.index(command), text.index('docker compose -f compose.stack.example.yaml'))
