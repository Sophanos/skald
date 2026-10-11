"""Event spine contracts and exact raw-input profile replay, entirely offline."""
from skald.members import rid
from array import array
import datetime
import math
import inspect
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from eval.profile_golden import FIXTURE, NOW, ROOT, compare, difference, dump, offline_server, sections

SCHEMA = {'t', 'id', 'key', 'kind', 'title', 'year', 'who', 'by', 'action', 'src', 'conf', 'n', 'reasons', 'w0', 'ctx'}


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.context = offline_server(ROOT / 'server.py')
        self.s = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def ev(self, src='remote', action='like', age=0, **changes):
        return self.s._event(NOW-age*self.s.DAY, 'e', 'tmdb:1', 'movie', 'Synthetic', '2020',
                             'ana', ['ana'], action, src, {'signal': 'liked'}, **changes)

    def test_canonical_dump_and_exact_difference(self):
        self.assertEqual(dump({'z': {2, 1}, 'a': array('f', [1., 2.]), 'b': (3, 4)}),
                         '{"z":[1,2],"a":[1.0,2.0],"b":[3,4]}')
        self.assertEqual(dump(float('nan')), 'NaN')
        with self.assertRaises(TypeError):
            dump(object())
        self.assertEqual(difference({'a': 1.}, {'a': math.nextafter(1., 2.)})[0], '$.a')
        self.assertEqual(difference({'a': 1, 'b': 2}, {'b': 2, 'a': 1})[0], '$.keys')

    # Generic member names change name ordering and hashes only.
    def test_fixture_golden(self):
        saved = json.loads((FIXTURE / 'golden.json').read_text())
        actual = sections(ROOT / 'server.py')
        self.assertEqual({k: dump(v, k == 'hashes') for k, v in saved.items()}, actual)
        self.assertEqual(actual, sections(ROOT / 'server.py'))

    def test_schema_identity_and_stable_unique_ids(self):
        s = self.s
        items, trakt = s.read_history(), s.read_trakt()
        first = list(s.events(NOW, items, trakt))
        second = list(s.events(NOW, items, trakt))
        self.assertEqual([e['id'] for e in first], [e['id'] for e in second])
        self.assertEqual(len(first), len({e['id'] for e in first}))
        self.assertIn('tv:movie:tmdb:1#2', {e['id'] for e in first})
        for adapter in (s.tv_events(NOW, items), s.trakt_events(NOW, trakt)):
            ids = [e['id'] for e in adapter]
            self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('trakt:movie:tmdb:60#2', {e['id'] for e in first})
        for e in first:
            self.assertEqual(set(e), SCHEMA)
            self.assertRegex(e['key'], r'^tmdb:[0-9]+$')
            self.assertIn(e['kind'], ('movie', 'tv'))
            self.assertIn(e['who'], ('ben', 'ana', 'both', 'together', 'unknown'))
            self.assertTrue(e['src'] in ('tv', 'trakt', 'told', 'remote', 'interview', 'utterance') or
                            re.fullmatch(r'(import|sync|agent):[^:]+', e['src']))
            self.assertNotIn('trust', e)
            if e['src'] in ('tv', 'trakt'):
                source = items if e['src'] == 'tv' else trakt
                self.assertTrue(any(e['ctx']['item'] is x for x in source))
                entries, _, _ = s.fold_events(((e['src'], iter([e])),), NOW)
                self.assertIs(entries[0]['item'], e['ctx']['item'])
        self.assertTrue(any(e['src'] == 'tv' and e['t'] is None for e in first))
        self.assertTrue(any(e['src'] == 'trakt' and e['t'] is None for e in first))
        self.assertTrue(any(e['src'] == 'trakt' and e['action'] == 'mystery' for e in first))
        self.assertFalse(any(e['src'] == 'remote' and e['action'] == 'not_tonight' for e in first))
        both = [e for e in first if e['key'] == 'tmdb:85' and e['src'] == 'remote']
        self.assertEqual({e['id'].rsplit(':', 1)[1] for e in both}, {'ben', 'ana'})

    def test_injected_duplicate_ids_and_source_prefixes(self):
        s = self.s
        raw = s.read_imports()[0]
        records = [{**raw, 'id': '0'*32, 'key': f'tmdb:{i}', 'src': src}
                   for i, src in enumerate(('plain', 'sync:letterboxd', 'agent:client', 'utterance'), 1)]
        # A bare utterance in a file is namespaced, as every unprefixed file source is.
        with patch.object(s, 'active_imports', return_value=records), patch.object(s, 'person_loves', return_value=[]):
            evts = [e for e in s.events(NOW, []) if e['src'] != 'remote']
        self.assertEqual([e['id'] for e in evts], ['0'*32] + ['0'*32+f'#{i}' for i in range(2, 5)])
        self.assertEqual([e['src'] for e in evts], ['import:utterance', 'agent:client', 'sync:letterboxd', 'import:plain'])
        self.assertIs(evts[-1]['ctx']['src'], records[0]['src'])

    def test_adapters_are_lazy_generators(self):
        s = self.s
        with patch.object(s, 'tmdb_details', side_effect=AssertionError), \
                patch.object(s, 'person_loves', side_effect=AssertionError), \
                patch.object(s, 'active_imports', side_effect=AssertionError), \
                patch.object(s, 'feedback_state', side_effect=AssertionError):
            adapters = (s.tv_events(NOW, s.read_history()), s.trakt_events(NOW, s.read_trakt()),
                        s.told_events(), s.import_events(NOW), s.remote_events(NOW))
            for adapter in adapters:
                self.assertTrue(inspect.isgenerator(adapter))
            self.assertIsInstance(s.event_stages(NOW, []), tuple)
            self.assertTrue(inspect.isgenerator(s.events(NOW, [])))
        with patch.object(s, 'tmdb_details', return_value=None) as resolve:
            adapter = s.tv_events(NOW, s.read_history())
            self.assertEqual(list(adapter), [])
            self.assertEqual(resolve.call_count, 40)

    def test_unchanged_stamps_do_not_reopen_import_or_feedback(self):
        s = self.s
        items, trakt = s.read_history(), s.read_trakt()
        s.build_profile(items, [], trakt)
        with patch('builtins.open', wraps=open) as opened:
            s.build_profile(items, [], trakt)
        paths = [str(call.args[0]) for call in opened.call_args_list]
        self.assertFalse(any(Path(p).name in ('imports.jsonl', 'feedback.jsonl') for p in paths))

    def test_read_trakt_stamp_includes_path_nanoseconds_and_size(self):
        s = self.s
        path = Path(s.DATA) / 'trakt.json'
        s.read_trakt()
        with patch('builtins.open', wraps=open) as opened:
            s.read_trakt()
        opened.assert_not_called()
        old = path.stat()
        path.write_text(path.read_text().replace('Fixture 60', 'Fixture 66'))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns+1))
        self.assertEqual(s.read_trakt()[1]['title'], 'Fixture 66')
        path.write_text(path.read_text()+' ')
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns+1))
        self.assertEqual(s._trakt.get('stamp')[2], len(path.read_bytes())-1)
        s.read_trakt()
        self.assertEqual(s._trakt['stamp'], (str(path), path.stat().st_mtime_ns, path.stat().st_size))
        other = Path(s.DATA) / 'other'
        other.mkdir()
        (other / 'trakt.json').write_text('{"items":[]}')
        with patch.object(s, 'DATA', str(other)):
            self.assertEqual(s.read_trakt(), [])

    def test_weight_table_each_family_action(self):
        s = self.s
        for signal, action in s.EVENT_ACTION.items():
            self.assertEqual(s.event_weight(self.ev('tv', action), NOW), (signal, s.SIGNAL_WEIGHT[signal], 0))
            ev = self.ev('trakt', action, w0=7.3)
            ev['ctx']['signal'] = signal
            self.assertEqual(s.event_weight(ev, NOW), (signal, 7.3, 0))
        for action, (signal, weight) in s.IMPORT_SIGNALS.items():
            ev = self.ev('import:plain', action, conf=.57)
            self.assertEqual(s.event_weight(ev, NOW), (signal, weight*.57*.8, 0))
        remote = {'love': ('loved', 3.), 'like': ('liked', 2.), 'meh': ('watched', .3),
                  'seen': ('watched', 1.), 'not_for_us': ('disliked', -3.)}
        for action, pair in remote.items():
            for src in ('remote', 'interview'):
                self.assertEqual(s.event_weight(self.ev(src, action), NOW), (*pair, 0))
        ev = self.ev('told', 'love', age=10000)
        ev['t'] = None
        self.assertEqual(s.event_weight(ev, NOW), ('loved', s.SEED_WEIGHT, s.SEED_AGE))
        self.assertEqual(set(s.ACTION_W), {(family, action) for family in ('tv', 'trakt') for action in s.EVENT_ACTION.values()} |
                         {('import', action) for action in s.IMPORT_SIGNALS} | {('remote', action) for action in remote} | {('told', 'love')})

    def test_step_boundaries_and_remote_love_grace(self):
        s = self.s
        for src, action, initial in (('tv', 'like', 2.), ('remote', 'like', 2.), ('import:trakt', 'like', 2.)):
            for age, decay in ((30, 1.), (31, .5), (120, .5), (121, .25)):
                self.assertEqual(s.event_weight(self.ev(src, action, age), NOW), ('liked', initial*decay, age))
        for age, weight in ((89, 3.), (90, 3.), (91, 1.5), (120, 1.5), (121, .75)):
            self.assertEqual(s.event_weight(self.ev('remote', 'love', age), NOW), ('loved', weight, age))
        ev = self.ev('tv', 'open'); ev['t'] = None
        self.assertEqual(s.event_weight(ev, NOW), ('opened', .05, 999))
        self.assertEqual(s.event_weight(self.ev('remote', 'like', -1), NOW), ('liked', 2., 0))

    def test_import_interest_days_and_fold_limits(self):
        s = self.s
        for age in (0, 30, 119, 120):
            self.assertEqual(s.event_weight(self.ev('import:plain', 'interest', age), NOW),
                             ('interest', .2*1.0*.8*1.0*(1-age/120), age))
        self.assertEqual(s.event_weight(self.ev('import:plain', 'like', 30.9), NOW), ('liked', 1.6, 30))
        out = s.import_entries(NOW)
        interests = [e for e in out if 200 <= e['id'] <= 220]
        self.assertEqual(len(interests), 20)
        self.assertEqual([e['id'] for e in interests], list(range(220, 200, -1)))
        self.assertNotIn(110, [e['id'] for e in out])
        self.assertNotIn(111, [e['id'] for e in out])
        self.assertFalse(any(e['weight'] == 0 for e in out))
        self.assertEqual(next(e['signal'] for e in out if e['id'] == 112), 'loved')
        raw = s.read_imports()[0]
        for age in (120, 130):
            raw = {**raw, 'action': 'interest', 't': datetime.datetime.fromtimestamp(NOW-age*s.DAY, datetime.timezone.utc).date().isoformat()}
            self.assertEqual(s.import_entries(NOW, [raw]), [])
        ev = next(s.import_events(NOW, [raw]))
        self.assertEqual(ev['t'] % s.DAY, 0)
        self.assertEqual(ev['n'], raw['n'])
        self.assertEqual(ev['conf'], raw['conf'])

    def test_trakt_unknown_signal_and_w0_passthrough(self):
        s = self.s
        raw = {'id': 'tmdb:123', 'type': 'movie', 'signal': 'unrecognised', 'weight': -7.125}
        ev = next(s.trakt_events(NOW, [raw]))
        self.assertEqual(ev['action'], 'unrecognised')
        self.assertIs(ev['ctx']['item'], raw)
        self.assertEqual(s.event_weight(ev, NOW), ('unrecognised', -7.125, 999))
        raw['weight'] = None
        self.assertEqual(s.event_weight(next(s.trakt_events(NOW, [raw])), NOW)[1], 0.)

    def test_trust_rows_and_live_source_table(self):
        s = self.s
        for src in ('tv', 'trakt', 'told', 'remote', 'interview'):
            self.assertEqual(s.trust(src), 1.)
        for source, weight in s.IMPORT_TRUST.items():
            for prefix in ('import:', 'sync:'):
                self.assertEqual(s.trust(prefix+source), weight)
        self.assertEqual(s.trust('agent:x'), .8)
        self.assertEqual(s.trust('utterance'), .8)
        before = s._event_stats['unknown_src']
        self.assertEqual(s.trust('unrecognised'), 0.)
        self.assertEqual(s.trust('sync:unrecognised'), 0.)
        self.assertEqual(s._event_stats['unknown_src'], before+2)
        with patch.dict(s.IMPORT_TRUST, {'youtube': .9, 'new': .7}):
            self.assertEqual(s.trust('import:new'), .7)
            self.assertEqual(s.event_weight(self.ev('import:youtube'), NOW)[1], 2*.9)
        self.assertEqual(s.event_weight(self.ev('unknown'), NOW)[1], 0.)

    def test_tainted_truth_table_and_observer(self):
        s = self.s
        for src in ('agent:x', 'utterance', 'import:youtube', 'import:chrome', 'unknown'):
            self.assertTrue(s.tainted(self.ev(src)))
        for src in ('tv', 'remote', 'interview', 'import:netflix', 'import:plain', 'sync:letterboxd'):
            self.assertFalse(s.tainted(self.ev(src)))
        ev = self.ev(); self.assertEqual(s.observer(ev), 'self')
        ev['who'] = 'both'; self.assertEqual(s.observer(ev), 'other')
        ev['by'].append('ben'); self.assertEqual(s.observer(ev), 'self')
        ev['who'] = 'together'; self.assertEqual(s.observer(ev), 'self')
        self.assertEqual(s.OBSERVER, {'self': 1., 'other': 1.})

    def test_patched_told_seam_does_not_resolve(self):
        s = self.s
        with patch.object(s, 'add_person_loves', return_value=set()) as loves, \
                patch.object(s, 'resolve_pick', side_effect=AssertionError) as resolve:
            p = s.build_profile([], [], remote=False)
        self.assertEqual(p['entries'], [])
        loves.assert_called_once()
        self.assertTrue(inspect.isgenerator(loves.call_args.args[2]))
        resolve.assert_not_called()

    def test_told_still_runs_without_remote_and_marks_saga(self):
        s = self.s
        with patch.object(s, 'import_events', side_effect=AssertionError), \
                patch.object(s, 'remote_events', side_effect=AssertionError):
            p = s.build_profile([], [], remote=False)
        self.assertEqual({e['key'] for e in p['entries']}, {'tmdb:1', 'tmdb:60', 'tmdb:70'})
        self.assertIn('tmdb:71', p['seen'])

    def test_interview_has_identical_remote_entry(self):
        s = self.s
        raw = s.read_feedback()[0]
        active = {(raw['key'], raw['who']): raw}
        normal = s.feedback_entries(NOW, active)
        interview = {**raw, 'row': rid('interview', s.CONFIG)}
        state = {'active': {(raw['key'], raw['who']): interview}}
        with patch.object(s, 'feedback_state', return_value=state):
            ev = next(s.remote_events(NOW))
            self.assertEqual(ev['src'], 'interview')
            self.assertEqual(s.feedback_entries(NOW), normal)
            entries = []
            s.merge_feedback(entries, {}, NOW)
            self.assertEqual(entries, normal)
        self.assertEqual(s.feedback_weight(interview, NOW), s.feedback_weight(raw, NOW))
        self.assertIs(ev['reasons'], interview['reasons'])

    def test_stage_order_is_enforced(self):
        for stages in ((('remote', iter(())), ('tv', iter(()))), (('tv', iter(())), ('tv', iter(()))),
                       (('unknown', iter(())),)):
            with self.assertRaises(ValueError):
                self.s.fold_events(stages, NOW)

    def test_streamed_import_merge_matches_wrapper(self):
        s = self.s
        p = s.build_profile(s.read_history(), [], s.read_trakt(), remote=False)
        entries = json.loads(dump(p['entries']))
        by_key = {e['key']: e for e in entries}
        s.merge_imports(entries, by_key, NOW)
        base, _, _ = s.fold_events(s.event_stages(NOW, s.read_history(), s.read_trakt(), False), NOW)
        s._merge_import_events(base, {e['key']: e for e in base}, NOW, s.import_events(NOW))
        self.assertEqual(dump(sorted(entries, key=lambda e: e['key'])), dump(sorted(base, key=lambda e: e['key'])))
        # New imports attach to remote slots; told claims stay locked.
        for who in ('remote', 'told'):
            old = {**{k: v for k, v in entries[0].items() if k not in ('opinions', 'shared')},
                   'key': 'tmdb:4', 'who': who, 'by': ['ana'], 'signal': 'opened'}
            current = [old]
            raw = next(e for e in s.read_imports() if e['key'] == 'tmdb:4')
            s.merge_imports(current, {old['key']: old}, NOW, [{**raw, 'who': 'ana'}])
            self.assertEqual(current, [old])
            self.assertEqual(old.get('opinions', {}).get('ana', {}).get('signal'), 'loved' if who == 'remote' else None)

    def test_remote_opinions_remain_per_person(self):
        s = self.s
        newer = {**s.read_feedback()[0], 'who': 'ana', 'action': 'love', 't': NOW}
        older = {**newer, 'who': 'ben', 'action': 'meh', 't': NOW-1}
        active = {(newer['key'], 'ana'): newer, (older['key'], 'ben'): older}
        # Conflicting reactions describe each reactor, rather than collapsing the key.
        entries = s.feedback_entries(NOW, active)
        self.assertEqual([(e['signal'], e['by']) for e in entries],
                         [('loved', ['ana']), ('watched', ['ben'])])


class SnapshotGolden(unittest.TestCase):
    def test_snapshot_ab(self):
        source = os.environ.get('SKALD_GOLDEN_BASE')
        from snapshot_support import require_snapshot
        snapshot = require_snapshot(updated=True)
        if not source or not Path(source).is_file() or not snapshot.is_dir():
            self.skipTest('set SKALD_GOLDEN_BASE to base server.py and SKALD_SNAPSHOT to raw inputs')
        before = sections(Path(source), snapshot)
        self.assertTrue(compare(before, sections(Path(source), snapshot)))
        self.assertTrue(compare(before, sections(ROOT / 'server.py', snapshot)))


if __name__ == '__main__':
    unittest.main()
