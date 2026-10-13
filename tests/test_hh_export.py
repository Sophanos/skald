"""Synthetic checks for the opt-in aggregate sharing boundary."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from eval.household import export


def report(events=40, count=100):
    return {"status": "ok", "versions": {
        key: hashlib.sha256(key.encode()).hexdigest()
        for key in ("evaluator", "build_a", "build_b", "protocol")},
        "metrics": {"hit_rate": {"count": count, "events": events}}}


class ExportTests(unittest.TestCase):
    def test_paired_cells_validate_bounds_and_suppress_sparse_support(self):
        source = report()
        paired = {'delta': .1, 'ci95': [-.05, .2],
                  'support': {'weeks': 12, 'target_weeks': 8, 'targets': 30}}
        source['metrics'] = {'home_recovery_delta': paired}
        shared = export.build_share(source)
        self.assertIn('home_recovery_delta', shared['metrics'])
        for field, value in (('delta', True), ('delta', float('nan')),
                             ('ci95', [.2, -.05]), ('ci95', [-1.1, .2]),
                             ('ci95', [float('inf'), .2])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                export.build_share({**source, 'metrics': {
                    'home_recovery_delta': {**paired, field: value}}})
        for field, value in (('weeks', 11), ('target_weeks', 7), ('targets', 29)):
            sparse = {**paired, 'support': {**paired['support'], field: value}}
            suppressed = export.build_share({**source, 'metrics': {'home_recovery_delta': sparse}})
            self.assertEqual(suppressed['metrics']['home_recovery_delta'], {'status': 'suppressed'})
            export.validate_share(suppressed)
        for field, value in (('delta', True), ('ci95', [.2, -.05]),
                             ('ci95', [-1.1, .2]), ('extra', 'SYNTHETIC-PRIVATE')):
            bad = json.loads(json.dumps(shared))
            bad['metrics']['home_recovery_delta'][field] = value
            with self.assertRaisesRegex(ValueError, 'invalid aggregate document'):
                export.validate_share(bad)
        source['metrics'] = {'home_recovery': {'count': 100, 'events': 4, 'score': .75}}
        self.assertEqual(export.build_share(source)['metrics']['home_recovery'], {'status': 'suppressed'})

    def test_home_recovery_preserves_macro_score_and_paired_interval(self):
        source = report()
        source['metrics'] = {
            'home_recovery': {'count': 100, 'events': 40, 'score': .75},
            'home_recovery_delta': {'delta': -.2, 'ci95': (-.35, -.05),
                                    'support': {'weeks': 12, 'target_weeks': 8,
                                                'targets': 30, 'status': 'SUPPORTED'},
                                    'scopes': {'SYNTHETIC-PRIVATE-MEMBER': {}}}}
        shared = export.build_share(source)
        self.assertEqual(set(shared['metrics']), {'home_recovery', 'home_recovery_delta'})
        macro = shared['metrics']['home_recovery']
        self.assertEqual(macro['score'], .75)
        self.assertNotIn('confidence', macro)
        paired = shared['metrics']['home_recovery_delta']
        self.assertEqual(paired['delta'], -.2)
        self.assertEqual(paired['ci95'], [-.35, -.05])
        self.assertNotIn('scopes', paired)
        export.validate_share(shared)
        self.assertEqual(export.compare_households([shared, shared])['rates']['home_recovery'], .75)
        self.assertEqual(export.compare_households([shared, shared])['deltas']['home_recovery_delta'], -.2)
        source['metrics']['hit_rate'] = source['metrics'].pop('home_recovery_delta')
        mapped = export.build_share(source)
        self.assertEqual(mapped['metrics']['hit_rate']['ci95'], [-.35, -.05])
        export.validate_share(mapped)

    def test_export_rejects_dynamic_keys_and_item_hashes(self):
        source = report()
        marker = "SYNTHETIC-PRIVATE-TITLE"
        item_hash = hashlib.sha256(marker.encode()).hexdigest()
        source.update({marker: {marker: item_hash}, "dataset_hash": item_hash,
                       "item_hashes": [item_hash], "members": [{marker: 10}],
                       "weeks": [{marker: 20}], "embeddings": [0.5],
                       "titles": [marker], "catalog_ids": [marker], "row_labels": [marker],
                       "names": [marker], "dates": [marker], "paths": [marker]})
        source["metrics"][marker] = {"count": 100, "events": 50}
        source["metrics"]["hit_rate"][marker] = item_hash
        source["versions"]["dataset"] = item_hash
        shared = export.build_share(source)
        encoded = json.dumps(shared)
        self.assertFalse(marker in encoded or item_hash in encoded)
        self.assertEqual(set(shared["metrics"]), {"hit_rate"})
        for mutate in (lambda d: d.update({marker: 1}),
                       lambda d: d["metrics"].update({marker: {}}),
                       lambda d: d["versions"].update({"dataset": item_hash}),
                       lambda d: d["metrics"]["hit_rate"].update({marker: 1})):
            bad = json.loads(encoded)
            mutate(bad)
            with self.assertRaisesRegex(ValueError, "invalid aggregate document"):
                export.validate_share(bad)

    def test_error_title_never_reaches_share_file(self):
        marker = "SYNTHETIC-PRIVATE-TITLE"
        secret = "SYNTHETIC-SECRET-SENTINEL"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "share.json"
            self.assertIsNone(export.write_share(directory, report()))
            self.assertFalse(target.exists())
            for status in ("error", "golden_mismatch", "worker_crash", marker):
                source = report()
                source.update(status=status, exception=RuntimeError(marker + secret),
                              golden_mismatch={marker: secret}, worker_crash={secret: marker})
                source["metrics"]["hit_rate"]["exception"] = marker
                export.write_share(directory, source, enabled=True)
                encoded = target.read_text()
                self.assertFalse(marker in encoded or secret in encoded)
                shared = json.loads(encoded)
                export.validate_share(shared)
                self.assertEqual(shared["metrics"], {})
            before = target.read_bytes()
            for field in ("versions", "metrics"):
                source = report()
                source[field][next(iter(source[field]))] = marker + secret
                with self.assertRaises(ValueError) as caught:
                    export.write_share(directory, source, enabled=True)
                message = str(caught.exception)
                self.assertFalse(marker in message or secret in message)
                self.assertEqual(target.read_bytes(), before)

    def test_small_cells_and_complements_suppressed(self):
        for events, count in ((4, 100), (96, 100), (5, 9), (0, 0)):
            shared = export.build_share(report(events, count))
            self.assertEqual(shared["metrics"]["hit_rate"], {"status": "suppressed"})
            export.validate_share(shared)
            shared["metrics"]["hit_rate"]["count"] = count
            with self.assertRaises(ValueError):
                export.validate_share(shared)
        cell = export.build_share(report(5, 10))["metrics"]["hit_rate"]
        self.assertEqual((cell["count"], cell["events"], cell["rate"]), (10, 5, 0.5))
        self.assertLess(cell["confidence"][0], 0.5)
        self.assertGreater(cell["confidence"][1], 0.5)

    def test_equal_household_weight_and_matching_public_versions(self):
        first, second = export.build_share(report(10)), export.build_share(report(900, 1000))
        result = export.compare_households([first, second])
        self.assertEqual(result["rates"]["hit_rate"], 0.5)
        self.assertEqual(result["households"], 2)
        self.assertEqual(result["status"], "replication_only")
        self.assertEqual(export.compare_households([first, export.build_share(report(4))])["rates"], {})
        second["versions"]["protocol"] = "a" * 64
        with self.assertRaises(ValueError):
            export.compare_households([first, second])

    def test_invalid_numbers_and_tampered_documents_fail_closed(self):
        for events, count in ((True, 100), (10, 1_000_001), (-1, 100), (101, 100)):
            with self.assertRaises(ValueError):
                export.build_share(report(events, count))
        for field, value in (("rate", float("nan")), ("confidence", [0, 1]), ("events", 4)):
            shared = export.build_share(report())
            shared["metrics"]["hit_rate"][field] = value
            with self.assertRaises(ValueError):
                export.validate_share(shared)
        shared = export.build_share(report(999_995, 1_000_000))
        shared["metrics"]["hit_rate"]["rate"] = True
        with self.assertRaises(ValueError):
            export.validate_share(shared)
