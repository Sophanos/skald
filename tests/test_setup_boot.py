import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members


class BootTests(unittest.TestCase):
    def test_fresh_container_waits_for_setup_then_starts_server(self):
        from skald import boot
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'DATA_DIR': folder}, clear=True):
            def sleep(seconds):
                self.assertEqual(seconds, 1)
                Path(folder, 'skald.json').write_text(json.dumps(members.fresh_config(['alex', 'sam'])))
            with patch.object(boot.time, 'sleep', side_effect=sleep) as wait, patch.object(boot.os, 'execv') as execute:
                boot.main(out=io.StringIO())
            wait.assert_called_once()
            self.assertEqual(execute.call_args.args[1][-1], str(Path(boot.__file__).resolve().parents[1]/'server.py'))

    def test_docker_launcher_and_wait_entrypoint(self):
        text = (Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        self.assertIn('/usr/local/bin/skald', text)
        self.assertIn('"skald.boot"', text)
