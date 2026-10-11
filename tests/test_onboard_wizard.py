"""W12f consent, bounded rendering and unchanged scripted plan binding."""
from skald.members import rid
from decide_kit import write_legacy
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from skald.__main__ import main, parser
from skald.engine import Engine
from skald import guide, members
from skald.members import rid

PYROOT = Path(__file__).resolve().parents[1]
FIXTURES = PYROOT / 'tests/fixtures/ingest'
from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=False)
ANA, BEN = snapshot_people(SNAPSHOT)


class FrontDoorCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data, self.state = self.root / 'data', self.root / 'state'
        self.data.mkdir()
        write_legacy(self.data)
        (self.data / 'profile.json').write_text('{"entries": []}')
        (self.data / 'facets.json').write_text(json.dumps({'items': {'tmdb:87000001': {
            'kind': 'movie', 'title': 'Synthetic Beacon', 'y': '2021', 'g': [], 'x': {'found_family': 2}}}}))
        self.export = self.root / 'plain.txt'
        self.export.write_text('Synthetic Beacon (2021)\n')
        for target in ('socket.create_connection', 'urllib.request.urlopen', 'urllib.request.build_opener', 'subprocess.run'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def cli(self, *args, code=0, text=False, data=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main([*([] if '--live' in args else ['--snapshot', str(data or self.data)]), '--state-dir', str(self.state),
                           *([] if text else ['--json']), *args], out=out)
        self.assertEqual(result, code, (out.getvalue(), err.getvalue()))
        if '--verbose' not in args:
            self.assertNotIn('keys: dropped', err.getvalue())
        return (out.getvalue() if text else json.loads(out.getvalue())), err.getvalue()

    def stage(self):
        return self.cli('onboard', '--person', 'ana', '--source', f'plain={self.export}', '--non-interactive')[0]

    def apply(self):
        staged = self.stage()
        return self.cli('onboard', '--person', 'ana', '--source', f'plain={self.export}',
                        '--non-interactive', '--yes', '--plan', staged['plan'])[0]


class WizardTests(FrontDoorCase):
    def test_quick_names_only_and_confirmation_before_open(self):
        downloads = self.root / 'downloads'
        downloads.mkdir()
        (downloads / 'private.txt').write_text('DO_NOT_READ')
        (downloads / 'trakt.json').write_text('INVALID_DO_NOT_READ')
        selected = downloads / 'ViewingActivity.csv'
        selected.write_text('Title,Date\nSynthetic Beacon,10/11/26\n')
        nested = downloads / 'nested'
        nested.mkdir()
        (nested / 'watch-history.json').write_text('DO_NOT_READ')
        stdin = io.StringIO(f'quick\n{downloads}\nn\ny\nana\nlater\n')
        original = Path.open
        def guarded(path, *args, **kw):
            if path.name in ('trakt.json', 'private.txt', 'watch-history.json'):
                self.fail('opened an unconfirmed export')
            return original(path, *args, **kw)
        with patch('sys.stdin', stdin), patch.object(stdin, 'isatty', return_value=True), patch.object(Path, 'open', guarded):
            result, err = self.cli('onboard', '--dry-run')
        self.assertEqual([r['name'] for r in result['sources']], ['netflix'])
        self.assertIn('Quick / Custom', err)
        self.assertFalse(self.state.exists())

    def test_custom_and_both_from_people(self):
        stdin = io.StringIO(f'custom\nplain\n{self.export}\n\nboth\n')
        with patch('sys.stdin', stdin), patch.object(stdin, 'isatty', return_value=True):
            result, err = self.cli('onboard', '--dry-run')
        self.assertEqual(result['person'], 'both')
        self.assertNotIn('guest', err)
        self.assertEqual(set(result['maturity']), {'ana', 'ben'})

    def test_scripted_apply_resume_and_reset_are_idempotent(self):
        applied = self.apply()
        self.assertEqual(applied['added'], 1)
        before = (self.state / 'onboard.json').read_bytes()
        rerun = self.cli('onboard', '--person', 'ana', '--source', f'plain={self.export}', '--non-interactive')[0]
        self.assertEqual(rerun['added'], 0)
        self.assertEqual((self.state / 'onboard.json').read_bytes(), before)
        self.assertEqual(self.cli('onboard', '--person', 'ana', '--reset', '--non-interactive', code=2)[0]['error'], '--reset needs confirmation or --yes')
        self.assertEqual(self.cli('onboard', '--person', 'ana', '--reset', '--yes', '--non-interactive')[0]['changed'], 1)
        before = (self.state / 'onboard.json').read_bytes()
        self.assertEqual(self.cli('onboard', '--person', 'ana', '--reset', '--yes', '--non-interactive')[0]['changed'], 0)
        self.assertEqual((self.state / 'onboard.json').read_bytes(), before)

    def test_source_errors_keep_safe_reasons(self):
        cases = [('netflix', FIXTURES / 'ViewingActivity-duration.csv', "unmapped profile 'Export B' — add --profile-map 'Export B=ana'"),
                 ('chrome', FIXTURES / 'chrome.sql', 'not a Chrome History database'),
                 ('plain', self.root / 'missing.txt', 'file not found')]
        for source, path, reason in cases:
            with self.subTest(source=source):
                result, _ = self.cli('onboard', '--person', 'ana', '--non-interactive', '--dry-run', '--source', f'{source}={path}', code=2)
                self.assertEqual(result['error'], f'{source} ({path.name}): {reason}')
        bad = self.root / 'imdb.csv'
        bad.write_text('Title,Your Rating\nPRIVATE_ROW_CANARY,PRIVATE_RATING_CANARY\n')
        result, _ = self.cli('onboard', '--person', 'ana', '--non-interactive', '--dry-run', '--source', f'imdb={bad}', code=2)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertIn('imdb (imdb.csv):', result['error'])

    def test_sources_help_and_no_args_do_not_construct_engine(self):
        def walk(p):
            for action in p._actions:
                if action.dest != 'person':
                    self.assertTrue(action.help, action.dest)
                if isinstance(action, argparse._SubParsersAction):
                    for choice in action._choices_actions:
                        self.assertTrue(choice.help)
                    for child in action.choices.values():
                        walk(child)
        walk(parser())
        with patch('skald.__main__.Engine', side_effect=AssertionError('help must be cheap')):
            result, _ = self.cli(text=True)
            self.assertIn('start with: skald onboard', result)
            started = time.process_time()
            with self.assertRaises(SystemExit) as exit, redirect_stdout(io.StringIO()):
                main(['--help'])
            self.assertEqual(exit.exception.code, 0)
            self.assertLess(time.process_time() - started, .2)
        result, _ = self.cli('onboard', '--sources', text=True)
        self.assertNotIn('added 0', result)
        self.assertEqual(result.count('(unverified)'), len(guide.HOWTO))
        for how in guide.HOWTO.values():
            self.assertLessEqual(len(how['steps']), 4)
            self.assertIsNone(how['checked'])

    def test_maturity_is_read_defensively_and_next_is_ordered(self):
        with Engine(self.data) as engine:
            self.assertEqual(guide.member(engine, 'ana')[0]['state'], 'too_new')  # W19 maturity now present
            profile = engine.profile
            def ready(person):
                return {**profile(person), 'maturity': {'state': 'too_new', 'explicit': 4, 'target': 20}}
            with patch.object(engine, 'profile', side_effect=ready):
                maturity, line = guide.member(engine, 'ana')
                self.assertIn('too new — 4 of 20 rated', line)
                self.assertEqual([a['id'] for a in guide.next_actions(engine, self.state, 'ana', pending=2, pushed=1)], ['interview', 'review', 'push'])

    @unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), 'set SKALD_SNAPSHOT to run snapshot gates')
    def test_four_source_transcript_and_v1_shape(self):
        flags = ['onboard', '--person', ANA, '--non-interactive', '--dry-run',
                 '--source', f'trakt={FIXTURES / "trakt.json"}', '--source', f'letterboxd={FIXTURES / "letterboxd"}',
                 '--source', f'imdb={FIXTURES / "imdb-ratings-2026.csv"}', '--source', f'youtube={FIXTURES / "watch-history.json"}']
        started = time.process_time()
        output, _ = self.cli(*flags, text=True, data=SNAPSHOT)
        self.assertLessEqual(len(output.splitlines()), 25)
        self.assertLess(time.process_time() - started, 1.5)
        self.assertNotIn(rid('', members.load_config(str(SNAPSHOT), {}, readonly=True)[0]), output)
        self.assertIn('4 unsupported title types', output)
        verbose, _ = self.cli(*flags, '--verbose', text=True, data=SNAPSHOT)
        self.assertIn(rid('', members.load_config(str(SNAPSHOT), {}, readonly=True)[0]), verbose)
        result, _ = self.cli(*flags, data=SNAPSHOT)
        self.assertEqual(result['v'], 1)
        self.assertTrue({'person', 'mode', 'sources', 'remote', 'react', 'plan', 'added', 'review_file', 'next'} <= result.keys())
        self.assertIsInstance(result['next'], list)
        self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
