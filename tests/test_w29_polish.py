"""Public polish contracts, using synthetic inputs only."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from skald import setup
from skald.__main__ import main, parser
from eval import replay, rows_report, seed_probes

ROOT = Path(__file__).resolve().parents[1]


class PublicPolish(unittest.TestCase):
    def test_help_explains_every_flag(self):
        def check(p):
            for action in p._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values():
                        check(child)
                elif action.option_strings:
                    with self.subTest(command=p.prog, flag=action.option_strings[0]):
                        self.assertTrue(action.help)
                        self.assertNotEqual(action.help, action.dest.replace('_', ' '))
        check(parser())
        p = parser()._subparsers._group_actions[0].choices['setup']
        self.assertNotIn('onboard', p.format_help())
        self.assertIn('manifest.json', p.format_help())

    def test_missing_tmdb_names_both_installations_and_one_next(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp}, clear=True):
            for flags in ([], ['--json']):
                out = io.StringIO()
                self.assertEqual(main(['setup', '--status', *flags], out=out), 1)
                text = out.getvalue()
                for phrase in ('TMDB_API not set', '.env', 'docker compose up -d', 'engine',
                               'docker compose -f compose.stack.example.yaml up -d', 'skald', 'export TMDB_API=...'):
                    self.assertIn(phrase, text)
                if flags:
                    self.assertEqual(json.loads(text)['next'], 'skald setup')
                else:
                    self.assertEqual(text.count('Next: '), 1)
            with self.assertRaises(ValueError) as error:
                setup.check_tmdb(Path(temp))
            for phrase in ('engine', 'docker compose -f compose.stack.example.yaml up -d', 'export TMDB_API=...'):
                self.assertIn(phrase, str(error.exception))

    def test_row_order_fallback_environment_and_explicit_override(self):
        s = SimpleNamespace(CONFIG={'row_prefix': 'demo'},
                            feed_rows=lambda lang: [('demo.foryou', ('tv', 'movie', 'series'), 'For you'),
                                                    ('demo.top10', ('movie', 'series'), {'movie': 'Movies', 'series': 'Series'})],
                            row_metas=lambda cid, typ, lang: [{'id': 'tmdb:1'}])
        with patch.dict(os.environ, {}, clear=True):
            rows = rows_report.display_rows(s)
            self.assertEqual([(r['id'], r['type'], r['name']) for r in rows],
                             [('demo.foryou', 'tv', 'For you'), ('demo.top10', 'movie', 'Movies'),
                              ('demo.top10', 'series', 'Series')])
            self.assertTrue(all(row['metas'] for row in rows))
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'order.mjs'
                path.write_text("const ORDER = ['player.top10:series', 'player.foryou:tv'];")
                other = Path(temp)/'other.mjs'
                other.write_text("const ORDER = ['player.top10:movie'];")
                expected = [('demo.top10', 'series'), ('demo.foryou', 'tv')]
                self.assertEqual([(r['id'], r['type']) for r in rows_report.display_rows(s, order=path)], expected)
                with patch.dict(os.environ, {'SKALD_ROW_ORDER': str(path)}):
                    self.assertEqual([(r['id'], r['type']) for r in rows_report.display_rows(s)], expected)
                    self.assertEqual([(r['id'], r['type']) for r in rows_report.display_rows(s, order=other)],
                                     [('demo.top10', 'movie')])

    def test_rows_report_requires_snapshot(self):
        with patch.object(sys, 'argv', ['rows_report.py']), patch.object(rows_report, 'SNAPSHOT', None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                rows_report.main()
            self.assertEqual(error.exception.code, 2)

    def test_probes_only_use_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(replay, 'ROOT', Path(temp)):
            root = Path(temp)/'eval'
            root.mkdir()
            (root/'probes.example.json').write_text('{"probes": [], "seed_probes": []}')
            path = root/'custom.json'
            path.write_text('[{"custom": true}]')
            self.assertEqual(replay.probe_data('probes.json'), [])
            self.assertEqual(replay.probe_data('probes.json', path), [{'custom': True}])
            self.assertEqual(seed_probes.load_probes(None), [])

    def test_contributor_paths_and_release(self):
        text = (ROOT/'AGENTS.md').read_text()
        self.assertLessEqual(len(text.splitlines()), 110)
        words = [word for code in re.findall(r'`([^`]+)`', text) for word in code.split()]
        paths = [word for word in words if word == 'Dockerfile' or
                 (re.fullmatch(r'[\w./-]+', word) and ('/' in word or '.' in word))]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((ROOT/path).exists())
        self.assertEqual((ROOT/'CLAUDE.md').read_text().splitlines()[0], '@AGENTS.md')
        self.assertIn('VERSION = "1.2.0"', (ROOT/'server.py').read_text())
        self.assertEqual(re.search(r'^## (\d+\.\d+\.\d+)', (ROOT/'CHANGELOG.md').read_text(), re.M).group(1), '1.2.0')
