"""Offline packaging checks; stdlib only, no Docker daemon or YAML parser."""
from pathlib import Path
import fnmatch
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_compose_data_mount_is_gitignored(self):
        self.assertIn('/data/', (ROOT / '.gitignore').read_text().splitlines())

    def test_dockerfile(self):
        text = (ROOT / "Dockerfile").read_text()
        for line in ("FROM python:3.12-alpine", "# TODO pin digest", "USER 65534", "VOLUME /data", "EXPOSE 8080"):
            self.assertIn(line, text.splitlines())
        self.assertRegex(text, r"RUN .*tzdata")
        self.assertRegex(text, r"RUN .*mkdir /data.*chown 65534 /data.*chmod 700 /data")
        self.assertRegex(text, r"(?m)^HEALTHCHECK .*python -c .*urllib.*127\.0\.0\.1:8080/health")
        self.assertIn("COPY server.py", text)
        self.assertIn("COPY skald/", text)
        self.assertNotRegex(text, r"(?m)^COPY .*?(?:\.env|\*\.json|(?:^|\s)\.(?:\s|$))")
        for env in ("DATA_DIR=/data", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"):
            self.assertIn(env, text)

    def test_compose_hardening_and_publication(self):
        for name in ("compose.yaml", "compose.selfhost.example.yaml"):
            text = (ROOT / name).read_text()
            for line in ("read_only: true", "tmpfs: /tmp", 'user: "65534"', "cap_drop: [ALL]",
                         "security_opt: [no-new-privileges:true]", "./data:/data", "env_file: .env", "healthcheck:"):
                self.assertIn(line, text)
            self.assertNotRegex(text, r"8081|API_PORT|REMOTE_PORT")
            self.assertIn("127.0.0.1:8080/health", text)
        remote = (ROOT / "compose.yaml").read_text()
        self.assertEqual(re.findall(r'^\s*- "([^"\n]+:[0-9]+)"$', remote, re.M), ["127.0.0.1:8080:8080"])
        aio = (ROOT / "compose.selfhost.example.yaml").read_text()
        self.assertNotRegex(aio, r"(?m)^\s*ports:")
        self.assertRegex(aio, r"networks:\s*\n  aio:\s*\n    external: true")
        self.assertEqual(re.findall(r"(?m)^  ([a-z]+):$", aio), ["engine", "aio"])

    def test_env_names_and_dockerignore(self):
        text = (ROOT / ".env.example").read_text()
        self.assertEqual(text.splitlines(), ["TMDB_API", "OPENROUTER_API_KEY", "SKALD_EMBED_MODEL", "SKALD_MEMBERS", "SKALD_LANG", "SKALD_TZ"])
        self.assertNotIn("=", text)
        ignore = (ROOT / ".dockerignore").read_text().splitlines()
        for name in (".env", "*.json", "snapshot/", "eval/out/"):
            self.assertIn(name, ignore)

    def test_setup_and_connector_guides(self):
        text = (ROOT / "docs/SETUP.md").read_text()
        for phrase in ("HTTPS", "Caddy", "Cloudflare Tunnel", "skald setup", "API_PORT=8081"):
            self.assertIn(phrase, text)
        self.assertFalse((ROOT / "docs/SELF-HOST.md").exists())
        text = (ROOT / 'docs/CONNECTORS.md').read_text()
        for phrase in ('0700', '0600', 'skald setup --connect', 'skald connectors test', 'one user'):
            self.assertIn(phrase, text)

    def test_private_files_are_excluded_from_build_context(self):
        rules = (ROOT / ".dockerignore").read_text().splitlines()
        private = (".review/findings.md", "snapshot/2026-10-11/cache.bin", "tests/fixtures/export.bin",
                   "household.sqlite", "household.db", "ratings.csv", "export.zip", "state.tar",
                   "state.tar.gz", "eval/out/report.html")
        for path in private:
            with self.subTest(path=path):
                self.assertTrue(any(fnmatch.fnmatch(path, rule) or rule.endswith("/") and path.startswith(rule)
                                    for rule in rules), "private build input was not excluded")


if __name__ == "__main__":
    unittest.main()
