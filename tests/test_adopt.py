"""Adopt and roll back a synthetic legacy household, offline."""
import io
from contextlib import redirect_stderr
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members, setup
from skald.__main__ import main, parser
from test_api import raw_request


class AdoptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        env = patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(members.configure)
        self.cfg = members.fresh_config(['alex', 'sam'])
        self.cfg['row_prefix'] = 'house42'
        self.cfg['addon'].update(id='synthetic.house', name='Our household', legacy_open=True)
        self.cfg['region']['country'] = 'Canada'
        self.cfg['connectors'] = {'reserved': {'kind': 'api', 'enabled': False, 'auth': 'env:RESERVED'}}
        del self.cfg['household']['note']  # Validation defaults must not be published.
        self.legacy = self.data/'legacy.json'
        self.legacy.write_text(json.dumps(self.cfg))
        self.original = self.legacy.read_bytes()

    def adopt(self, *flags):
        out = io.StringIO()
        with redirect_stderr(out):
            rc = main(['setup', 'adopt', *flags], out=out)
        return rc, out.getvalue()

    def test_adopt_preserves_object_and_publishes_private_config(self):
        rc, text = self.adopt('--yes')
        self.assertEqual(rc, 0, text)
        path = self.data/'skald.json'
        self.assertEqual(json.loads(path.read_text()), self.cfg)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.data/'.skald-household').is_file())
        self.assertEqual(self.legacy.read_bytes(), self.original)
        self.assertEqual(len(text.splitlines()), 3)
        for phrase in ('Adopted', 'setup link', '--connect', 'imports', 'remove', '.skald-household', 'legacy.json'):
            self.assertIn(phrase, text)

    def test_refuses_existing_config_or_missing_legacy_without_writes(self):
        path = self.data/'skald.json'
        path.write_text(json.dumps(self.cfg))
        for existing in (True, False):
            with self.subTest(existing=existing):
                if not existing:
                    path.unlink()
                    self.legacy.unlink()
                before = {p.name: p.read_bytes() for p in self.data.iterdir()}
                rc, text = self.adopt('--yes')
                self.assertNotEqual(rc, 0)
                self.assertIn('already exists' if existing else 'legacy.json', text)
                self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_confirmation_and_validation_before_writes(self):
        with patch('builtins.input', return_value='n'):
            self.assertNotEqual(self.adopt()[0], 0)
        self.assertEqual({p.name for p in self.data.iterdir()}, {'legacy.json'})
        rc, text = self.adopt('--json')
        self.assertNotEqual(rc, 0)
        self.assertIn('--yes', text)
        with patch('builtins.input', return_value='y'):
            self.assertEqual(self.adopt()[0], 0)
        (self.data/'skald.json').unlink()
        (self.data/'.skald-household').unlink()
        self.legacy.write_text('{"v": 0}')
        self.assertNotEqual(self.adopt('--yes')[0], 0)
        self.assertEqual({p.name for p in self.data.iterdir()}, {'legacy.json', 'erase.lock'})

    def test_custom_config_path(self):
        path = self.data/'custom.json'
        with patch.dict(os.environ, {'SKALD_CONFIG': str(path)}):
            self.assertEqual(self.adopt('--yes')[0], 0)
            self.assertEqual(json.loads(path.read_text()), self.cfg)
            self.assertNotEqual(self.adopt('--yes')[0], 0)
        self.assertFalse((self.data/'skald.json').exists())

    def test_adopt_write_failure_restores_legacy_loading(self):
        args = parser().parse_args(['setup', 'adopt', '--yes'])
        for error in (OSError('config write failed'), members.ConfigError('skald.json')):
            with self.subTest(error=type(error).__name__):
                with patch.object(members.json, 'dump', side_effect=error):
                    with self.assertRaises(type(error)):
                        setup.run(args)
                self.assertFalse((self.data/'skald.json').exists())
                self.assertFalse((self.data/'.skald-household').exists())
                self.assertEqual(self.legacy.read_bytes(), self.original)
                cfg, source = members.load_config(str(self.data), readonly=True)
                self.assertEqual(source, 'legacy')
                self.assertEqual(cfg, members.validate_config(self.cfg, data=str(self.data)))

    def test_adopt_write_failure_preserves_existing_stamp(self):
        stamp = self.data/'.skald-household'
        stamp.write_bytes(b'existing stamp')
        args = parser().parse_args(['setup', 'adopt', '--yes'])
        with patch.object(members.json, 'dump', side_effect=OSError('config write failed')):
            with self.assertRaises(OSError):
                setup.run(args)
        self.assertEqual(stamp.read_bytes(), b'existing stamp')
        self.assertFalse((self.data/'skald.json').exists())
        self.assertEqual(self.legacy.read_bytes(), self.original)

    def served(self):
        with setup.server(self.data) as s:
            manifest = raw_request(s, f'/{s.LANG}/manifest.json', handler=s.Handler)
            with patch.object(s, 'search', return_value={'metas': []}) as search:
                catalog = raw_request(s, f'/{s.LANG}/catalog/movie/{s.rid("search")}/search=fixture.json', handler=s.Handler)
                search.assert_called_once()
            self.assertEqual(manifest[0], 200)
            self.assertEqual(catalog[0], 200)
            return json.loads(manifest[2]), json.loads(catalog[2])

    def test_manifest_catalog_link_status_and_rollback(self):
        before = self.served()
        out = io.StringIO()
        main(['setup', '--status', '--json'], out=out)
        self.assertTrue(json.loads(out.getvalue())['legacy_readonly'])
        args = ['setup', 'link', '--name', 'laptop', '--base', 'https://api.example']
        out = io.StringIO()
        with redirect_stderr(out):
            self.assertNotEqual(main(args, out=out), 0)
        self.assertIn('run skald setup adopt to make it editable', out.getvalue())
        self.assertEqual(self.adopt('--yes')[0], 0)
        out = io.StringIO()
        main(['setup', '--status', '--json'], out=out)
        self.assertFalse(json.loads(out.getvalue())['legacy_readonly'])
        self.assertEqual(self.served(), before)
        self.assertEqual(main(args, out=io.StringIO()), 0)
        self.assertTrue((self.data/'clients.json').exists())
        (self.data/'skald.json').unlink()
        (self.data/'.skald-household').unlink()
        cfg, source = members.load_config(str(self.data), readonly=True)
        self.assertEqual(source, 'legacy')
        self.assertEqual(cfg, members.validate_config(self.cfg, data=str(self.data)))
        self.assertTrue(setup.status(self.data)['legacy_readonly'])
        self.assertEqual(self.legacy.read_bytes(), self.original)
