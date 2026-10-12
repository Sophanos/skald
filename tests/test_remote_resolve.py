import json
import unittest
from unittest.mock import patch

from skald.ingest import Candidate
from skald.remote import resolve_titles


class RemoteResolveTests(unittest.TestCase):
    def test_unicode_batches_respect_serialized_bytes_and_count(self):
        candidates = [Candidate("界" * 100 + str(i), "2020", "movie") for i in range(205)]
        calls = []
        def send(path, body):
            self.assertEqual(path, "/api/v1/resolve")
            self.assertLessEqual(len(json.dumps(body, allow_nan=False).encode()), 65536)
            self.assertLessEqual(len(body["items"]), 100)
            calls.extend(item["title"] for item in body["items"])
            return {"results": [{"match": None, "conf": 0, "method": "unresolved",
                                 "high_confidence": False, "choices": []} for item in body["items"]]}
        with patch("skald.remote.request", side_effect=send):
            self.assertEqual(len(resolve_titles(candidates)), len(candidates))
        self.assertEqual(calls, [c.title for c in candidates])
