"""Bounded personal cards and idempotent local undo through existing paths."""
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from skald import guide
from skald.engine import Engine
from test_onboard_wizard import FrontDoorCase, SNAPSHOT, ANA


class MeTests(FrontDoorCase):
    def test_source_forget_restores_profile_and_second_forget_is_noop(self):
        def digest():
            value, _ = self.cli('taste', 'show', '--person', 'ana')
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        before = digest()
        self.assertEqual(self.apply()['added'], 1)
        card, _ = self.cli('me', '--person', 'ana')
        self.assertEqual([s['name'] for s in card['sources']], ['plain'])
        self.assertNotEqual(digest(), before)
        result, _ = self.cli('me', '--person', 'ana', 'forget', '--source', 'plain')
        self.assertEqual(result['changed'], 1)
        self.assertEqual(digest(), before)
        files = {p.name: p.read_bytes() for p in self.state.iterdir()}
        result, _ = self.cli('me', 'forget', '--source', 'plain', '--person', 'ana')
        self.assertEqual(result['changed'], 0)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.state.iterdir()})
        self.assertEqual(self.cli('me', '--person', 'ana')[0]['sources'][0]['status'], 'undone')

    def test_import_forget_is_idempotent_and_person_bound(self):
        self.apply()
        event = self.cli('imports', 'list')[0]['events'][0]
        result, _ = self.cli('me', '--person', 'ben', 'forget', '--import', event['id'], code=2)
        self.assertEqual(result['error'], 'import belongs to another person')
        self.assertEqual(self.cli('me', '--person', 'ana', 'forget', '--import', event['id'])[0]['changed'], 1)
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        self.assertEqual(self.cli('me', '--person', 'ana', 'forget', '--import', event['id'])[0]['changed'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})
        self.assertEqual(self.cli('imports', 'list')[0]['events'], [])

    def test_fact_routes_print_exact_pending_command_and_exit_three(self):
        for action in ('forget', 'correct'):
            result, _ = self.cli('me', '--person', 'ana', action, '--fact', 'm1', code=3)
            self.assertEqual(result, {'v': 1, 'changed': 0, 'say': 'needs W10c', 'cli': f'skald memory {action} --person ana --fact m1'})
        self.assertEqual(self.cli('me', '--person', 'ana', 'correct', '--fact', 'bad id', code=2)[0]['error'], 'invalid memory fact id')
        self.assertFalse(self.state.exists())

    def test_card_shape_read_only_and_future_maturity(self):
        card, _ = self.cli('me', '--person', 'ana')
        self.assertEqual(set(card), {'v', 'person', 'say', 'maturity', 'sources', 'top', 'facts', 'pending', 'actions', 'archetype'})
        self.assertEqual(card['maturity']['state'], 'too_new')  # W19 maturity now present
        self.assertLessEqual(len(card['say']), 140)
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 4096)
        self.assertFalse(self.state.exists())
        with Engine(self.data) as engine:
            profile = engine.profile
            def too_new(person):
                return {**profile(person), 'maturity': {'state': 'too_new', 'explicit': 4, 'target': 20}}
            with patch.object(engine, 'profile', side_effect=too_new):
                card, count = guide.me(engine, self.state, 'ana')
            self.assertIn('too new — 4 of 20 rated', count)
            self.assertEqual(card['actions'][0]['id'], 'interview')

    def test_card_removes_foreign_anchors(self):
        with Engine(self.data) as engine:
            value = engine.profile('ana')
            value['entries'] = [{'title': 'Mine', 'by': ['ana']}, {'title': 'Shared', 'by': ['together']}, {'title': 'Foreign', 'by': ['ben']}]
            value['experiences'] = [{'id': 'x:test', 'name': 'Test', 'affinity': 1, 'anchors': ['Foreign', 'Mine', 'Shared']}]
            with patch.object(engine, 'profile', return_value=value):
                card, _ = guide.me(engine, self.state, 'ana')
            self.assertEqual(card['top']['experiences'][0]['anchors'], ['Mine', 'Shared'])
            self.assertNotIn('Foreign', json.dumps(card))

    def test_large_card_keeps_sources_within_byte_limit(self):
        from skald.intake import _json
        records = {name: {'file': 'a' * 16, 'status': 'applied', 'at': '2026-10-11', 'added': [],
                         'counts': {'candidates': 100, 'accepted': 100, 'review': 0}} for name in
                   ('trakt', 'letterboxd', 'imdb', 'netflix', 'youtube', 'chrome', 'plain')}
        _json(self.state / 'onboard.json', {'v': 1, 'people': {'ana': records}})
        with Engine(self.data) as engine:
            value = engine.profile('ana')
            title = 'é' * 120
            value['entries'] = [{'title': title, 'by': ['ana']}]
            for group in ('experiences', 'themes', 'creators'):
                value[group] = [{'id': 'x:' + 'é' * 80, 'name': 'é' * 80, 'affinity': 1, 'anchors': [title, title]} for _ in range(3)]
            with patch.object(engine, 'profile', return_value=value):
                card, _ = guide.me(engine, self.state, 'ana')
            self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 4096)
            self.assertEqual(len(card['sources']), 7)

    @unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), 'set SKALD_SNAPSHOT to run snapshot gates')
    def test_snapshot_text_and_json_limits_and_anchor_ownership(self):
        card, _ = self.cli('me', '--person', ANA, data=SNAPSHOT)
        text, _ = self.cli('me', '--person', ANA, text=True, data=SNAPSHOT)
        self.assertLessEqual(len(text.splitlines()), 20)
        self.assertTrue('too new' in text or 'explicit 4' in text)
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False).encode()), 4096)
        with Engine(SNAPSHOT) as engine:
            allowed = {e['title'] for e in engine.profile(ANA)['entries'] if ANA in e['by'] or 'together' in e['by']}
        for facets in card['top'].values():
            self.assertLessEqual(len(facets), 3)
            for facet in facets:
                self.assertTrue(set(facet['anchors']) <= {a[:60] for a in allowed})


if __name__ == '__main__':
    unittest.main()
