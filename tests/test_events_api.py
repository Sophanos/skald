import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_api_routes import ApiCase


def event(i=1, **extra):
    return {"id": f"{i:032x}", "t": "2026-10-11", "key": f"tmdb:{i}", "kind": "movie", "title": f"Synthetic {i}",
            "who": "ben", "action": "love", "src": "plain", "conf": 1, **extra}


class EventsTests(ApiCase):
    def post(self, events, stream="imports", **kwargs):
        return self.request("/api/v1/events", "POST", {"stream": stream, "events": events}, **kwargs)

    def test_whole_request_guards(self):
        self.assertEqual(self.post([event()], client="read")[0], 403)
        for src in ("agent:x", "sync:trakt", "utterance"):
            self.assertEqual(self.post([event(), event(2, src=src)])[0], 403)
            self.assertEqual(self.s.read_imports(), [])
        self.assertEqual(self.post([event(who="ana")], client="ben")[0], 403)
        self.assertEqual(self.post([event(who="both")], client="ben")[0], 403)
        self.assertEqual(self.post([{}] * 501)[0], 400)
        self.assertEqual(self.request("/api/v1/events", "POST", b"x" * 65537)[0], 413)
        for body in ({}, {"stream": "imports", "events": [], "extra": 1}, [], {"stream": "bad", "events": []},
                     {"stream": "imports", "events": None}):
            self.assertEqual(self.request("/api/v1/events", "POST", body)[0], 400)

    def test_accounting_ids_conflicts_exact_repeat_and_partial_validation(self):
        self.assertEqual(self.post([event(), event(2)])[1], {"accepted": 2, "duplicates": 0, "rejected": []})
        self.assertEqual(self.post([event(), event(2)])[1], {"accepted": 0, "duplicates": 2, "rejected": []})
        reply = self.post([event(title="changed"), event(3, key="tmdb:2", title="Synthetic 2"), event(4, conf=-1), 1])[1]
        self.assertEqual(reply, {"accepted": 0, "duplicates": 1, "rejected": [
            {"i": 0, "error": "conflict"}, {"i": 2, "error": "input"}, {"i": 3, "error": "input"}]})
        self.assertEqual(reply["accepted"] + reply["duplicates"] + len(reply["rejected"]), 4)
        self.assertEqual({e["id"] for e in self.s.read_imports()}, {event()["id"], event(2)["id"]})
        reply = self.post([event(8), event(8), event(8, action="like")])[1]
        self.assertEqual(reply, {"accepted": 1, "duplicates": 1, "rejected": [{"i": 2, "error": "conflict"}]})

    def test_id_retry_without_timestamp_preserves_original_day(self):
        old = event(t="2000-01-01")
        self.s.write_imports([old], keep_ids=True)
        retry = {k: v for k, v in old.items() if k != "t"}
        self.assertEqual(self.post([retry])[1], {"accepted": 0, "duplicates": 1, "rejected": []})

    def test_overflow_is_atomic_409(self):
        with patch.object(self.s, "IMPORT_MAX", 1):
            self.assertEqual(self.post([event(), event(2)])[0], 409)
        self.assertEqual(self.s.read_imports(), [])

    def test_undo_unknown_other_person_and_replay(self):
        self.post([event(), event(2, who="ana")])
        code, reply = self.post([{"id": event(2)["id"]}, {"id": event(3)["id"]}, {"id": "bad"},
                                 {"id": event()["id"]}], "imports_undo", client="ben")
        self.assertEqual(code, 200)
        self.assertEqual(reply, {"accepted": 1, "duplicates": 0, "rejected": [{"i": i, "error": "id"} for i in range(3)]})
        self.assertEqual(self.post([{"id": event()["id"]}], "imports_undo")[1]["rejected"], [{"i": 0, "error": "id"}])
        self.assertEqual(len(self.s.read_imports()), 1)

    def test_default_write_keeps_uuid_and_strongest_batch_behavior(self):
        s = self.s
        added = s.write_imports([event(action="seen", n=58), event(action="love")])
        self.assertEqual(len(added), 1)
        self.assertNotEqual(added[0]["id"], event()["id"])
        self.assertEqual((added[0]["action"], added[0]["n"]), ("love", 58))
        self.assertEqual(s.write_imports([event(action="love", n=58)]), [])
        before = Path(s.DATA, "imports.jsonl").read_bytes()
        with self.assertRaises(ValueError):
            s.write_imports([event(2), event(3, conf=-1)])
        self.assertEqual(Path(s.DATA, "imports.jsonl").read_bytes(), before)

    def test_undo_batch_reads_and_saves_once_and_rejects_repeated_id(self):
        events = [event(i) for i in range(1, 501)]
        self.s.write_imports(events, keep_ids=True)
        remaining = self.s.read_imports()[499:]
        undo = [{"id": e["id"]} for e in events[:499]] + [{"id": events[0]["id"]}]
        with patch.object(self.s, "read_imports", wraps=self.s.read_imports) as read, \
                patch.object(self.s, "save_imports", wraps=self.s.save_imports) as save:
            reply = self.s.undo_write(undo, {"viewer": "ben"})
            read.assert_called_once_with()
            save.assert_called_once()
        self.assertEqual(reply, {"accepted": 499, "duplicates": 0, "rejected": [{"i": 499, "error": "id"}]})
        self.assertEqual(self.s.read_imports(), remaining)

    def test_stream_extension_needs_no_route_edit(self):
        with patch.dict(self.s.EVENT_STREAMS, {"toy": {"check": lambda e, r: None,
                "write": lambda e, r: {"accepted": len(e), "duplicates": 0, "rejected": []}, "example": [1]}}):
            self.assertEqual(self.post([1], "toy")[1], {"accepted": 1, "duplicates": 0, "rejected": []})


if __name__ == "__main__":
    unittest.main()
