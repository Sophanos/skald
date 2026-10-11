"""The parity gate must detect a changed server, independently of its token alias."""
import ast
from decide_kit import write_legacy
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from eval import route_parity
from eval.rows_report import FILES
from test_api import raw_request

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def memory_transport(s):
    def get(path):
        status, headers, body, connection = raw_request(s, path, handler=s.Handler)
        wire = b"".join(call.args[0] for call in connection.sendall.call_args_list)
        return status, headers, body, wire
    yield get


class RouteParityTests(unittest.TestCase):
    def git_baseline(self):
        try:
            source = self.enterContext(route_parity.baseline_source(base_ref="60989fb"))
            from skald.members import LEGACY
            text = source.read_text()
            start = text.index('LEGACY = ')
            end = text.index('# LEGACY END', start)
            prefix = next(n.value.split('.')[0] for n in ast.walk(ast.parse(text))
                          if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.endswith('.search'))
            from skald.members import rid
            text = text[:start] + 'LEGACY = ' + repr(LEGACY) + '\n' + text[end:]
            source.write_text(text.replace(prefix + '.', rid('', LEGACY)))
            return source
        except route_parity.BaselineUnavailable as error:
            self.skipTest(str(error))

    def test_cli_offers_git_ref_or_explicit_baseline(self):
        result = subprocess.run([sys.executable, str(ROOT / "eval/route_parity.py"), "--help"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--base-ref", result.stdout)
        self.assertIn("--base ", result.stdout)

    def test_default_baseline_is_git_commit_in_temporary_file(self):
        try:
            baseline = self.enterContext(route_parity.baseline_source())
        except route_parity.BaselineUnavailable as error:
            self.skipTest(str(error))
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(),
                         "79b02db6f3ad6c21c825f00d0651484fe750a151b5df9be02e628c1c90f1b83f")
        self.assertNotEqual(baseline.parent, ROOT)

    def test_explicit_baseline_works_without_git(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp, "base.py")
            base.write_text("# explicit baseline\n")
            with patch("subprocess.run", side_effect=FileNotFoundError("git unavailable")), \
                    route_parity.baseline_source(base=base) as source:
                self.assertEqual(source.read_text(), "# explicit baseline\n")
            self.assertTrue(base.exists())

    def test_unavailable_git_or_ref_has_clear_baseline_error(self):
        for error in (FileNotFoundError("git unavailable"),
                      subprocess.CalledProcessError(128, ["git", "show", "missing:server.py"])):
            with self.subTest(error=type(error).__name__), patch("subprocess.run", side_effect=error):
                with self.assertRaisesRegex(route_parity.BaselineUnavailable,
                                            "git or baseline ref missing unavailable"):
                    with route_parity.baseline_source(base_ref="missing"):
                        self.fail("unavailable baseline was accepted")

    def test_loopback_transport_preserves_complete_wire_bytes(self):
        wire = b"HTTP/1.0 200 Custom reason\r\nX-First: one\r\nContent-Length: 2\r\n\r\n{}"
        server = Mock(server_address=("127.0.0.1", 12345))
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.makefile.return_value = io.BytesIO(wire)
        s = Mock(ThreadingHTTPServer=Mock(return_value=server))
        with patch("threading.Thread"), patch("socket.create_connection", return_value=connection), \
                patch("http.client.HTTPConnection") as parsed:
            parsed.return_value.getresponse.return_value.status = 200
            parsed.return_value.getresponse.return_value.getheaders.return_value = []
            parsed.return_value.getresponse.return_value.read.return_value = b"{}"
            with route_parity.loopback_server(s) as get:
                result = get("/health")
        self.assertEqual(result, (200, {"X-First": "one", "Content-Length": "2"}, b"{}", wire))

    def test_changed_manifest_byte_fails_base_comparison(self):
        base = self.git_baseline()
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp, "data")
            snapshot.mkdir()
            write_legacy(snapshot)
            for name in FILES:
                (snapshot / name).write_text('{}')
            profile = json.loads((ROOT / "tests/fixtures/events/golden.json").read_text())["household"]
            (snapshot / "profile.json").write_text(json.dumps(profile))
            source = (ROOT / "server.py").read_text()
            changed = Path(temp, "changed.py")
            changed.write_text(source.replace('"version": CONFIG["addon"]["version"],',
                                              '"version": CONFIG["addon"]["version"] + "!",', 1))
            original = importlib.util.spec_from_file_location
            def staged_source(name, path, **kwargs):
                return original(name, changed if Path(path) == ROOT / "server.py" else path, **kwargs)
            with patch("importlib.util.spec_from_file_location", staged_source), \
                    patch.object(route_parity, "loopback_server", memory_transport):
                with self.assertRaisesRegex(AssertionError, "base wire parity failed"):
                    route_parity.report(snapshot, base=base)

    def test_changed_error_byte_fails_base_comparison(self):
        base = self.git_baseline()
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp, "data")
            snapshot.mkdir()
            write_legacy(snapshot)
            for name in FILES:
                (snapshot / name).write_text('{}')
            profile = json.loads((ROOT / "tests/fixtures/events/golden.json").read_text())["household"]
            (snapshot / "profile.json").write_text(json.dumps(profile))
            source = (ROOT / "server.py").read_text()
            changed = Path(temp, "changed.py")
            head, handler = source.split("class Handler(BaseHTTPRequestHandler):", 1)
            changed.write_text(head + "class Handler(BaseHTTPRequestHandler):" + handler.replace(
                'return self.send_json(404, {"error": "not found"})',
                'return self.send_json(404, {"error": "not found!"})', 1))
            original = importlib.util.spec_from_file_location
            def staged_source(name, path, **kwargs):
                return original(name, changed if Path(path) == ROOT / "server.py" else path, **kwargs)
            with patch("importlib.util.spec_from_file_location", staged_source), \
                    patch.object(route_parity, "loopback_server", memory_transport):
                with self.assertRaisesRegex(AssertionError, "base error wire parity failed"):
                    route_parity.report(snapshot, base=base)


if __name__ == "__main__":
    unittest.main()
