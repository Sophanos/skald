"""Synthetic checks for group evidence and member protection."""
from copy import deepcopy
import importlib
import unittest


X, Y = ('movie', 'tmdb:1'), ('movie', 'tmdb:2')


def event(action='played', identity=X, **changes):
    return {'media_kind': identity[0], 'provider_id': identity[1],
            'action': action, 'source': 'player', 'reliable': True,
            'session_id': 's1', **changes}


class HouseholdGroupsTests(unittest.TestCase):
    def setUp(self):
        self.groups = importlib.import_module('eval.household.groups')
        self.session = {'id': 's1', 'participants': ['ana', 'ben']}

    def test_default_together_is_not_verified_cowatch(self):
        for attribution in ({'who': 'together', 'by': ['together'], 'ctx': {}},
                            {'who': 'both', 'source': 'prior'},
                            {'who': 'both', 'source': 'tap'}):
            with self.subTest(attribution=attribution):
                result = self.groups.evaluate_session(
                    {'id': 's1', **attribution}, [X], [event('completed')])
                self.assertEqual(result['scope'], ('shared-unknown', 'household'))
                self.assertIsNone(result['group_choices'])
                self.assertIsNone(result['group_completion'])
                self.assertIsNone(result['viewing_events'])
                self.assertEqual(result['joint_approval'][X]['status'], 'unknown')
        known = self.groups.evaluate_session(self.session, [X], [event()])
        self.assertEqual(known['scope'][0], 'verified-group')

    def test_unknown_member_is_not_a_dislike(self):
        records = [event('like', member='ana'), event('like')]
        preferences = {'ana': [event('like')]}
        result = self.groups.evaluate_session(self.session, [X], records, preferences)
        self.assertEqual(result['joint_approval'][X],
                         {'status': 'unknown', 'missing_members': ['ben']})
        balance = result['member_balance']
        self.assertEqual(balance['label'], 'member-balance proxies')
        self.assertEqual(balance['members'], {'ana': 1.0, 'ben': None})
        self.assertEqual(balance['missing_members'], ['ben'])
        self.assertIsNone(balance['mean'])
        self.assertIsNone(balance['minimum'])
        self.assertIsNone(balance['spread'])
        self.assertFalse(result['hard_failure'])
        records.append(event('like', member='ben'))
        approved = self.groups.evaluate_session(self.session, [X], records, preferences)
        self.assertEqual(approved['joint_approval'][X]['status'], 'approved')
        records.append(event('veto', member='ben'))
        preferences['ben'] = [event('love')]
        vetoed = self.groups.evaluate_session(self.session, [X], records, preferences)
        self.assertEqual(vetoed['joint_approval'][X]['status'], 'rejected')
        self.assertEqual(vetoed['veto_violations'], [X])
        self.assertTrue(vetoed['hard_failure'])
        self.assertEqual(vetoed['member_balance']['mean'], 1)
        preferences['ben'].append(event('veto'))
        self.assertTrue(self.groups.evaluate_session(
            self.session, [X], [], preferences)['hard_failure'])

    def test_joint_play_counts_once(self):
        records = [event(), event(source='trakt'), event('completed'),
                   event('progress', progress=.95, source='trakt'),
                   event('completed', identity=Y, reliable=False)]
        preferences = {'ana': [event('like')], 'ben': [event('love', identity=Y)]}
        inputs = deepcopy((self.session, records, preferences))
        result = self.groups.evaluate_session(self.session, [X], records, preferences)
        self.assertEqual(result['viewing_events'], 1)
        self.assertEqual(result['completion_events'], 1)
        self.assertEqual(result['group_choices']['targets'], 1)
        self.assertEqual(result['group_choices']['hits'], 1)
        self.assertEqual(result['group_completion']['targets'], 1)
        balance = result['member_balance']
        self.assertEqual(balance['members'], {'ana': 1.0, 'ben': 0.0})
        self.assertEqual((balance['mean'], balance['minimum'], balance['spread']),
                         (.5, 0, 1))
        self.assertEqual((self.session, records, preferences), inputs)
        self.assertEqual(result, self.groups.evaluate_session(
            self.session, [X], records, preferences))

    def test_episode_completion_and_slate_veto_boundary(self):
        tv = ('tv', 'tmdb:3')
        records = [event('completed', identity=tv),
                   event('completed', identity=tv, episode=(1, 1)),
                   event('completed', identity=tv, episode=(1, 1), source='trakt'),
                   event('veto', identity=Y, member='ben')]
        result = self.groups.evaluate_session(self.session, [tv], records, k=1)
        self.assertEqual(result['completion_events'], 1)
        self.assertEqual(result['group_completion']['hits'], 1)
        self.assertFalse(result['hard_failure'])
        result = self.groups.evaluate_session(self.session, [tv, Y], records, k=1)
        self.assertTrue(result['hard_failure'])

    def test_invalid_session_evidence_is_rejected(self):
        for session in ({'id': ''}, {'id': 's1', 'participants': ['ana', 'ana']},
                        {'id': 's1', 'participants': ['together']},
                        {'id': 's1', 'participants': 'ana'}):
            with self.subTest(session=session), self.assertRaises(ValueError):
                self.groups.evaluate_session(session, [X], [])
        with self.assertRaises(ValueError):
            self.groups.evaluate_session(self.session, [X], [event(session_id='s2')])


if __name__ == '__main__':
    unittest.main()
