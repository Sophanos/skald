import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from skald import remote
from skald.__main__ import main
from test_api_routes import ApiCase
from test_events_api import event


class FakeClock:
    def __init__(self):
        self.now, self.waits = 0., []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class CLIPushTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.state = Path(self.temp.name, "state")
        self.state.mkdir()
        self.clock = FakeClock()
        self.calls, self.failures = [], 0
        p = patch.dict(os.environ, {"SKALD_API": "http://synthetic.test:8081"})
        p.start()
        self.addCleanup(p.stop)

    def log(self, n=1200):
        (self.state / "imports.jsonl").write_text("".join(json.dumps(event(i)) + "\n" for i in range(1, n + 1)))
        (self.state / "undone.json").write_text(json.dumps([f"{1201+i:032x}" for i in range(3)]))

    def send(self, path, body):
        self.assertEqual(path, "/api/v1/events")
        self.calls.append((self.clock(), body, len(json.dumps(body, allow_nan=False).encode())))
        if self.failures:
            self.failures -= 1
            raise ValueError("api 429")
        return self.s.intake(body["stream"], body["events"], {"viewer": None})

    def push(self):
        return remote.push_imports(self.state, apply=True, clock=self.clock, sleep=self.clock.sleep)

    def test_1200_events_bytes_pacing_and_idempotency(self):
        self.log()
        with patch.object(remote, "request", side_effect=self.send):
            first = self.push()
            self.assertEqual((first["accepted"], first["duplicates"], first["already_absent"]), (1200, 0, 3))
            self.assertEqual(first["rejected"], [])
            calls = list(self.calls)
            second = self.push()
        self.assertEqual((second["accepted"], second["duplicates"], second["already_absent"]), (0, 1200, 3))
        self.assertEqual(first["requests"], len(calls))
        self.assertLessEqual(max(b for t, body, b in calls), 60000)
        self.assertLessEqual(max(len(body["events"]) for t, body, b in calls), 500)
        self.assertGreaterEqual(round(min(b[0] - a[0] for a, b in zip(calls, calls[1:])), 10), 1.1)

    def test_preview_never_sends_and_cli_bypasses_snapshot(self):
        self.log()
        with patch.object(remote, "request", side_effect=AssertionError("preview sent")), \
                patch("skald.__main__.Engine", side_effect=AssertionError("remote Engine")), \
                patch("skald.__main__.live.pull", side_effect=AssertionError("remote ssh")):
            out = io.StringIO()
            code = main(["--state-dir", str(self.state), "--live", "push", "imports", "--remote", "--json"], out=out)
            self.assertEqual(code, 0)
            preview = json.loads(out.getvalue())
            self.assertEqual((preview["count"], preview["undone"], preview["host"], preview["requests"]), (1200, 3, "synthetic.test", 0))
            self.assertGreater(preview["chunks"], 1)
            self.assertGreater(preview["bytes"], 0)

    def test_one_429_waits_61_seconds_once(self):
        self.log(2)
        self.failures = 1
        with patch.object(remote, "request", side_effect=self.send):
            data = self.push()
        self.assertEqual(data["accepted"], 2)
        self.assertEqual(self.clock.waits.count(61), 1)
        self.assertEqual(self.calls[1][0] - self.calls[0][0], 61)

    def test_two_429_abort_nonzero_then_rerun_converges(self):
        self.log(2)
        self.failures = 2
        with patch.object(remote, "request", side_effect=self.send), patch("time.monotonic", self.clock), patch("time.sleep", self.clock.sleep):
            out = io.StringIO()
            self.assertNotEqual(main(["--state-dir", str(self.state), "push", "imports", "--remote", "--apply", "--json"], out=out), 0)
            self.assertEqual(json.loads(out.getvalue()), {"error": "rate limited; re-run (idempotent)"})
            self.assertEqual(self.push()["accepted"], 2)
            self.assertEqual(self.push()["duplicates"], 2)

    def test_undo_and_other_rejections_exit_one(self):
        self.log(2)
        (self.state / "undone.json").write_text(json.dumps([event(1)["id"]]))
        with patch.object(remote, "request", side_effect=self.send):
            result = self.push()
        self.assertEqual((result["accepted"], result["undone"], result["already_absent"]), (2, 1, 0))
        with patch.object(remote, "push_imports", return_value={"accepted": 0, "duplicates": 0, "rejected": [{"i": 0, "error": "conflict"}],
                                                               "undone": 0, "already_absent": 0, "requests": 1}):
            self.assertEqual(main(["push", "imports", "--remote", "--apply", "--json"], out=io.StringIO()), 1)

    def test_bad_response_is_sanitized_before_merging_counts(self):
        self.log(2)
        replies = ({}, {"accepted": True, "duplicates": 1, "rejected": []},
                   {"accepted": 0, "duplicates": 0, "rejected": []},
                   {"accepted": 1, "duplicates": 0, "rejected": [{"i": 1, "error": "private-value"}]})
        for reply in replies:
            with patch.object(remote, "request", return_value=reply):
                with self.assertRaisesRegex(ValueError, "^api response$"):
                    self.push()

    def test_bad_logs_or_overlarge_item_fail_before_sending(self):
        self.log(1)
        (self.state / "undone.json").write_text('["bad"]')
        with patch.object(remote, "request", side_effect=AssertionError("invalid log sent")):
            with self.assertRaisesRegex(ValueError, "undo"):
                self.push()
            (self.state / "undone.json").write_text('[]')
            (self.state / "imports.jsonl").write_text(json.dumps(event(1, title="x" * 60000)))
            with self.assertRaisesRegex(ValueError, "body limit"):
                self.push()


if __name__ == "__main__":
    unittest.main()
