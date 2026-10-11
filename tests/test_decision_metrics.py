import json
from pathlib import Path
import tempfile
import unittest

from decide_kit import close_server, fixture_rows, load_server, run_type


class DecisionMetricsTests(unittest.TestCase):
    def test_recorded_and_synthetic_accuracy_and_empty_group(self):
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            self.addCleanup(close_server, server)
            rows = fixture_rows(server, "route_query")[:3]
            rows[0]["note"] = "Recorded case"
            for row in rows[1:]:
                row["note"] = "Synthetic case"
            rows[2]["expect"]["choice"] = "wrong"
            path = Path(data, "rows.jsonl")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            metrics = run_type(server, "route_query", str(path))
            self.assertEqual(metrics["recorded"], {"n": 1, "acc": 1})
            self.assertEqual(metrics["synthetic"], {"n": 2, "acc": .5})
            self.assertEqual(metrics["acc"], 2 / 3)
            path.write_text(json.dumps(rows[0]) + "\n")
            self.assertEqual(run_type(server, "route_query", str(path))["synthetic"], {"n": 0, "acc": None})
