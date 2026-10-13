import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class BootTests(unittest.TestCase):
    def test_fresh_container_hands_waiting_and_health_to_server(self):
        from skald import boot
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'DATA_DIR': folder}, clear=True):
            with patch.object(boot.os, 'execv') as execute:
                boot.main(out=io.StringIO())
            self.assertFalse(Path(folder, 'skald.json').exists())
            self.assertEqual(execute.call_args.args[1][-1], str(Path(boot.__file__).resolve().parents[1]/'server.py'))

    def test_docker_launcher_and_wait_entrypoint(self):
        text = (Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        self.assertIn('/usr/local/bin/skald', text)
        self.assertIn('"skald.boot"', text)
