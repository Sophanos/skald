"""One offline, synthetic comparison through the household modules."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from eval.household import events, export, metrics, protocol, splits, uncertainty


class HouseholdCompositionTests(unittest.TestCase):
    def test_records_to_paired_aggregate_export(self):
        start = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)
        week = timedelta(days=7)
        members = ('synthetic-a', 'synthetic-b')
        scopes = [('individual', member) for member in members]
        records = []
        for i in range(12):
            at = (start + i * week + timedelta(days=1)).timestamp()
            for member in members:
                for j in range(3):
                    records.append({'media_kind': 'movie',
                                    'provider_id': f'tmdb:{member}-{i}-{j}',
                                    'member': member, 'action': 'played',
                                    'source': 'synthetic', 'reliable': True,
                                    'occurred_at': at, 'observed_at': at,
                                    'timestamp_precision': 'second'})
        records.append({**records[0], 'provider_id': 'tmdb:explicit-rewatch', 'rewatch': True})
        normalized = events.normalize_events(records)
        self.assertEqual(len(normalized['discovery']), 72)
        folds = splits.weekly_folds(normalized['events'], start + 13 * week, timezone='UTC')
        frame = []
        for fold in folds:
            if fold.phase != 'outer':
                continue
            targets = []
            for e in fold.labels:
                if e['playback'] and not e['rewatch']:
                    title = ':'.join(e['title'])
                    j = int(title.rsplit('-', 1)[1])
                    targets.append({'id': title, 'franchise': f'f{j}', 'member': e['member'],
                                    'values': {(e['member'], 'main'): (int(j == 0), int(j < 2))}})
            frame.append({'week': fold.origin.isoformat(), 'mature': True, 'targets': targets})

        def macro(draw):
            scores = []
            for build in (0, 1):
                weeks = []
                for vector in draw:
                    for scope in scopes:
                        selected = [t for t in vector['targets'] if t['member'] == scope[1]]
                        rankings = {'main': [t['id'] for t in selected
                                             if t['values'][(scope[1], 'main')][build]]}
                        weeks.append(metrics.evaluate_week(scope, vector['week'],
                                     {'discovery': [t['id'] for t in selected]},
                                     [{'id': 'main'}], rankings))
                scores.append(metrics.summarize(weeks, scopes, resampled=True)
                              ['tasks']['discovery']['score'])
            return tuple(scores)

        self.assertEqual(len(frame), 12)
        self.assertEqual(macro(frame), (1 / 3, 2 / 3))
        paired = uncertainty.analyze(frame, macro)
        self.assertEqual(paired['status'], 'B_BETTER')
        self.assertEqual(paired['support']['targets'], 72)
        public_versions = {key: hashlib.sha256(key.encode()).hexdigest() for key in export.VERSIONS}
        public_versions['protocol'] = protocol.protocol_id(protocol.manifest())
        shared = export.build_share({'status': 'ok', 'versions': public_versions, 'metrics': {
            'home_recovery': {'count': 72, 'events': 48, 'score': macro(frame)[1]},
            'home_recovery_delta': paired}})
        export.validate_share(shared)
        self.assertEqual(shared['metrics']['home_recovery']['score'], .667)
        self.assertEqual(shared['metrics']['home_recovery_delta']['ci95'], list(paired['ci95']))
        encoded = json.dumps(shared)
        self.assertFalse(any(member in encoded for member in members))
        self.assertNotIn('tmdb:', encoded)
        self.assertNotIn('2026-', encoded)
