"""Synthetic, in-memory point-in-time reconstruction checks."""
import copy
from datetime import datetime, timedelta, timezone
import unittest

from eval.household.metrics import row_metrics
from eval.household.protocol import manifest, protocol_id
from eval.household.splits import Fold


ORIGIN = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def version(key, value, at=None, available_at=None, **extra):
    at = at or ORIGIN - DAY
    return dict(id=key, value=value, at=at,
                available_at=available_at or at, **extra)


class AsOfTests(unittest.TestCase):
    def setUp(self):
        from eval.household import asof
        self.a = asof
        self.fold = Fold('outer', ORIGIN, ORIGIN + 7 * DAY,
                         ({'member': 'a', 'title': 'old', 'at': ORIGIN - DAY,
                           'available_at': ORIGIN - DAY},), (), ())
        self.inputs = {name: [] for name in asof.DEPENDENCIES}
        self.policy = manifest()
        self.policy.update(reference_catalog=['old', 'target', 'unavailable', 'unknown'],
                           reference_required_components=['metadata', 'facets'])
        for title in self.policy['reference_catalog']:
            self.inputs['eligibility'].append(version(title, {'eligible': True}))
            self.inputs['metadata'].append(version(title, {'genre': 'synthetic'}))
            self.inputs['facets'].append(version(title, {'facet': 'synthetic'}))
        self.inputs['eligibility'][2]['value'] = {'eligible': False, 'reason': 'unavailable'}
        self.inputs['metadata'][-1]['available_at'] = ORIGIN + DAY

    def builder(self, inputs):
        return {'profile': {'entries': inputs['train']},
                'model': {'old': 1 + len(inputs['state']['credits'])},
                'inputs': inputs}

    def test_future_credit_cannot_help_old_training_title(self):
        baseline = self.a.rebuild_pair(self.fold, self.inputs, self.policy,
                                       self.builder, self.builder)
        self.inputs['credits'] = [version('old', {'boost': 2}, ORIGIN + DAY),
                                 version('late', {'boost': 2}, available_at=ORIGIN + DAY),
                                 version('boundary', {'boost': 2}, available_at=ORIGIN)]
        pair = self.a.rebuild_pair(self.fold, self.inputs, self.policy,
                                   self.builder, self.builder)
        self.assertEqual(pair['A']['model'], baseline['A']['model'])
        self.assertEqual(pair['B']['model'], baseline['B']['model'])
        self.assertEqual(pair['A']['profile']['entries'], self.fold.train)
        self.assertEqual(pair['A']['inputs'], pair['B']['inputs'])
        for name in self.a.DEPENDENCIES:
            with self.subTest(dependency=name):
                inputs = copy.deepcopy(self.inputs)
                inputs[name] += [version('future', {}, ORIGIN + DAY),
                                 version('late-dependency', {}, available_at=ORIGIN + DAY)]
                state = self.a.reconstruct(self.fold, inputs)
                self.assertNotIn('future', state['state'][name])
                self.assertNotIn('late-dependency', state['state'][name])
                self.assertFalse(any(r['id'] in ('future', 'late-dependency')
                                     for r in state['journals'][name]))

    def test_later_session_fix_not_visible_early(self):
        old = version('session', {'member': 'a', 'title': 'old'})
        fix = version('session', {'member': 'b', 'title': 'target'},
                      available_at=ORIGIN + DAY)
        self.inputs['sessions'] = [fix, old]
        self.inputs['feedback'] = [version('opinion', {'action': 'love'})]
        self.inputs['undo'] = [version('undo', {'feedback_id': 'opinion'}, ORIGIN + DAY)]
        early = self.a.reconstruct(self.fold, self.inputs)
        self.assertEqual(early['state']['sessions']['session'], old['value'])
        self.assertIn('opinion', early['state']['feedback'])
        self.assertEqual(early['state']['undo'], {})
        labels = self.a.session_labels(self.inputs['sessions'], ORIGIN + 2 * DAY)
        self.assertEqual(labels['session'], fix['value'])
        self.assertEqual(early['state']['sessions']['session'], old['value'])

    def test_future_metadata_rejected(self):
        self.inputs['metadata'] += [version('target', {'popularity': 1000}, ORIGIN + DAY)]
        pair = self.a.rebuild_pair(self.fold, self.inputs, self.policy,
                                   self.builder, self.builder)
        self.assertEqual(pair['A']['inputs']['state']['metadata']['target'],
                         {'genre': 'synthetic'})
        reference = pair['reference']
        self.assertEqual(reference['eligible'], ('old', 'target'))
        self.assertEqual(reference['exclusion_counts'],
                         {'unavailable': 1, 'ineligible': 0, 'historically_unverifiable': 1})
        targets = self.a.reference_targets(reference, ['target', 'unavailable', 'unknown'])
        self.assertEqual(targets['targets'], ('target',))
        self.assertEqual(row_metrics([], targets['targets'])['targets'], 1)
        self.assertEqual(row_metrics([], targets['targets'])['hit_at_k'], 0)
        self.assertEqual(reference['protocol_id'], protocol_id(self.policy))
        self.assertEqual(targets['exclusion_counts'],
                         {'unavailable': 1, 'ineligible': 0,
                          'historically_unverifiable': 1, 'outside_reference': 0})

    def test_normalized_targets_match_reference_and_adapter_rankings(self):
        from eval.household.events import normalize_events
        from test_hh_adapter import AdapterTests, cards, NOW
        reference = {'eligible': ('tmdb:100',), 'excluded': {
            'unavailable': ('tmdb:101',), 'ineligible': ('tmdb:102',),
            'historically_unverifiable': ('tmdb:103',)}}
        targets = normalize_events([
            {'media_kind': 'movie', 'provider_id': f'tmdb:{i}', 'action': 'played',
             'source': 'synthetic', 'reliable': True} for i in range(100, 105)])['discovery']
        result = self.a.reference_targets(reference, targets)
        self.assertEqual(result['targets'], ('tmdb:100',))
        self.assertEqual(result['exclusion_counts'], {
            'unavailable': 1, 'ineligible': 1, 'historically_unverifiable': 1,
            'outside_reference': 1})
        fixture = AdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.s.CONFIG['household']['rows'] = ['hbo']
        fixture.put('hbo', cards(100, 14))
        from eval.household.adapter import replay_home
        home = replay_home(fixture.s, now=NOW, enabled=True)
        self.assertEqual(row_metrics(next(iter(home['rankings'].values())),
                                     result['targets'])['hits'], 1)

    def test_strict_household_prefix_and_deletions(self):
        old = self.fold.train[0]
        other = dict(old, member='b', title='other')
        hidden = [dict(old, title='future', at=ORIGIN),
                  dict(old, title='late', available_at=ORIGIN),
                  dict(old, title='immature', mature_at=ORIGIN + DAY)]
        fold = Fold('outer', ORIGIN, ORIGIN + 7 * DAY,
                    (old, other, *hidden), (), ())
        self.inputs['memory'] = [version('fact', {'weight': 1}, ORIGIN - 3 * DAY),
                                 version('fact', None, ORIGIN - 2 * DAY, deleted=True),
                                 version('fact', {'weight': 10}, ORIGIN)]
        state = self.a.reconstruct(fold, self.inputs)
        self.assertEqual(state['train'], (old, other))
        self.assertEqual(state['state']['memory'], {})
        self.assertEqual(len(state['journals']['memory']), 2)
        self.assertEqual(self.a.session_labels([version('at-boundary', {},
                                                       available_at=ORIGIN)], ORIGIN),
                         {'at-boundary': {}})

    def test_catalog_is_protocol_defined_and_not_retrieval_defined(self):
        inputs = self.a.reconstruct(self.fold, self.inputs)
        for component in ('metadata', 'popularity', 'facets', 'embeddings'):
            inputs['state'][component]['target'] = None
            policy = dict(self.policy, reference_required_components=['metadata', component]
                          if component != 'metadata' else ['metadata'])
            ref = self.a.freeze_reference(policy, inputs)
            self.assertIn('target', ref['excluded']['historically_unverifiable'])
        with self.assertRaises(ValueError):
            self.a.freeze_reference(manifest(), inputs)
        for value in ([], ['unknown'], ['metadata', 'metadata']):
            with self.assertRaises(ValueError):
                self.a.freeze_reference(dict(self.policy, reference_required_components=value), inputs)
        with self.assertRaises(ValueError):
            self.a.rebuild_pair(self.fold, self.inputs, self.policy,
                                lambda inputs: {'model': {}}, self.builder)

    def test_builders_are_fresh_and_cannot_change_each_other_or_reference(self):
        original = copy.deepcopy(self.inputs)

        def mutate(inputs):
            result = self.builder(inputs)
            inputs['state']['metadata'].clear()
            inputs['train'][0]['title'] = 'mutated'
            return result

        pair = self.a.rebuild_pair(self.fold, self.inputs, self.policy, mutate, self.builder)
        self.assertEqual(pair['B']['profile']['entries'], self.fold.train)
        self.assertEqual(pair['reference']['eligible'], ('old', 'target'))
        self.assertEqual(self.inputs, original)
        self.assertEqual(self.a.reconstruct(self.fold, self.inputs),
                         self.a.reconstruct(self.fold, {k: list(reversed(v))
                                                      for k, v in self.inputs.items()}))

    def test_missing_chronology_and_ambiguous_revisions_fail_closed(self):
        for change in ('missing_component', 'missing_time', 'naive_time', 'ambiguous'):
            with self.subTest(change=change):
                inputs = copy.deepcopy(self.inputs)
                if change == 'missing_component':
                    del inputs['memory']
                else:
                    row = version('bad', {})
                    if change == 'missing_time':
                        del row['available_at']
                    elif change == 'naive_time':
                        row['at'] = ORIGIN.replace(tzinfo=None)
                    else:
                        inputs['memory'].append(version('bad', {'different': True}))
                    inputs['memory'].append(row)
                with self.assertRaises(ValueError):
                    self.a.reconstruct(self.fold, inputs)


if __name__ == '__main__':
    unittest.main()
