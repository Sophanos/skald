"""Typed memory contracts, synthetic evidence and read-only snapshot inspection."""
from decide_kit import write_legacy
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.profile_golden import NOW, ROOT, offline_server
from skald.__main__ import main

from snapshot_support import snapshot_path, snapshot_people
SNAPSHOT = snapshot_path(updated=True)
ANA, BEN = snapshot_people(SNAPSHOT)
CANARY = 'PRIVATE_CANARY_DO_NOT_STORE'


class MemoryTests(unittest.TestCase):
    def setUp(self):
        ctx = offline_server(ROOT / 'server.py')
        self.s = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.s._memory = None
        self.s._memory_index.clear()
        self.names = {'x:found_family': 'found family', 'g:Horror': 'Horror'}
        self.m = {'paff': {'ben': {'x:found_family': 1.}, 'ana': {}}, 'names': self.names, 'nexp': {}}
        self.s._facets = {}
        self.s._model['m'] = self.m
        self.s._profiles['household'] = {'hash': 'test'}

    def ev(self, i, src='trakt', age=0, action='like', by=None, reasons=None, conf=1., dated=True, key=None):
        key = key or f'tmdb:{i}'
        self.s._facets[key] = {'kind': 'movie', 'x': {'found_family': 2}, 'g': ['Horror']}
        return self.s._event(NOW-age*self.s.DAY if dated else None, f'{src}:movie:{key}', key, 'movie',
                             CANARY, '2020', 'ben', ['ben'] if by is None else by, action, src,
                             {}, conf=conf, reasons=reasons)

    def evidence(self, n=3, **kw):
        return [self.ev(i+1, age=8 if i == 0 else 0, **kw) for i in range(n)]

    def run_curate(self, evts, **kw):
        return self.s.curate(iter(evts), self.m, now=NOW, **kw)

    def fact(self, seq=1, status='proposed', strength=.6, n=3, seen=NOW, **kw):
        return {'id': f'm{seq}', 'person': 'ben', 'kind': 'like', 'about': 'x:found_family',
                'text': 'likes found family', 'strength': strength, 'n': n, 'evidence': [f'e{seq}'],
                'src': 'implicit', 'via': 'curate', 'status': status, 'gates': {}, 'created': NOW,
                'confirmed': None, 'seen': seen, 'end': None if status in ('active', 'proposed') else NOW,
                'into': None, **kw}

    def store(self, facts=(), **kw):
        return {**self.s._memory_empty(), 'seq': max([int(f['id'][1:]) for f in facts], default=0),
                'facts': list(facts), **kw}

    def files(self):
        return {p.name: p.read_bytes() for p in Path(self.s.DATA).glob('memory*') if p.is_file()}

    def test_support_two(self):
        self.assertEqual(self.run_curate(self.evidence(2))['ops'], [])

    def test_one_family_one_week(self):
        self.assertEqual(self.run_curate([self.ev(i) for i in range(3)])['ops'], [])

    def test_two_families(self):
        evts = [self.ev(1), self.ev(2), self.ev(3, src='remote')]
        f = self.run_curate(evts)['store']['facts'][0]
        self.assertEqual((f['status'], f['strength'], f['gates']['families']), ('proposed', .3, 2))

    def test_two_weeks(self):
        f = self.run_curate(self.evidence())['store']['facts'][0]
        self.assertEqual(f['gates'], {'support': 3, 'families': 1, 'weeks': 2, 'rank': 1})
        self.assertEqual(f['seen'], NOW)
        self.assertEqual(len(f['evidence']), 3)

    def test_support_eight(self):
        self.assertEqual(self.run_curate(self.evidence(8))['store']['facts'][0]['strength'], .7)

    def test_rank_sixteen(self):
        self.m['paff']['ben'].update({f'g:g{i}': 2. for i in range(15)})
        self.names.update({f'g:g{i}': f'g{i}' for i in range(15)})
        self.assertEqual(self.run_curate(self.evidence())['ops'], [])

    def test_speaks_false(self):
        with patch.object(self.s, 'speaks', return_value=False):
            self.assertEqual(self.run_curate(self.evidence())['ops'], [])

    def test_together_is_not_a_person(self):
        self.assertEqual(self.run_curate(self.evidence(by=['together']))['ops'], [])

    def test_tainted_sources(self):
        for src in ('agent:x', 'utterance', 'import:chrome'):
            with self.subTest(src=src):
                r = self.run_curate(self.evidence(src=src))
                self.assertEqual((r['ops'], r['stats']['tainted']), ([], 3))

    def test_unknown_trust_is_cached(self):
        before = self.s._event_stats['unknown_src']
        self.run_curate(self.evidence(src=CANARY))
        self.assertEqual(self.s._event_stats['unknown_src']-before, 1)

    def test_undated_and_equal_time_forget(self):
        store = self.store(forgotten={'ben': NOW})
        self.assertEqual(self.run_curate(self.evidence(dated=False), store=store)['ops'], [])
        self.assertEqual(self.run_curate(self.evidence(), store=store)['ops'], [])

    def test_reason_once(self):
        self.assertEqual(self.run_curate([self.ev(1, action='not_for_us', reasons=['not my genre'])])['ops'], [])

    def test_same_reason_active(self):
        evts = [self.ev(i, action='not_for_us', reasons=['not my genre'], conf=.8) for i in (1, 2)]
        f = self.run_curate(evts)['store']['facts'][0]
        self.assertEqual((f['about'], f['status'], f['src']), ('g:Horror', 'active', 'reason'))
        self.assertAlmostEqual(f['strength'], .7*.8)
        self.assertEqual(f['gates'], {'support': 2, 'reason': 'not my genre'})

    def test_reason_requires_held_facet_and_strength(self):
        evts = [self.ev(i, action='not_for_us', reasons=['seen enough of these']) for i in (1, 2)]
        for rec in self.s._facets.values():
            rec['x'].update({'dark': 1, 'grim': 0})
        facts = self.run_curate(evts)['store']['facts']
        self.assertEqual([f['about'] for f in facts], ['x:found_family'])
        evts = [self.ev(i, action='not_for_us', reasons=['too violent']) for i in (3, 4)]
        self.assertEqual(self.run_curate(evts)['ops'], [])

    def test_tainted_reason(self):
        evts = [self.ev(i, src='agent:x', action='not_for_us', reasons=['not my genre']) for i in (1, 2)]
        self.assertEqual(self.run_curate(evts)['ops'], [])

    def test_reason_fids_parity(self):
        def old(reason, rec):
            fids = [f'x:{x}' for x in self.s.REASON_FACETS.get(reason, ())]
            if reason == 'not my genre':
                fids = [f'g:{g}' for g in rec.get('g', ())]
            elif reason == 'seen enough of these':
                fids = [f'x:{x}' for x, st in (rec.get('x') or {}).items() if st >= 2]
            return fids
        for reason in self.s.REMOTE_REASONS:
            for rec in ({}, {'g': ['Horror']}, {'g': ['Drama'], 'x': {'dark': 1, 'grim': 2, 'bleak': 3}}):
                self.assertEqual(self.s.reason_fids(reason, rec), old(reason, rec))

    def test_tells_trusted_and_tainted(self):
        f = self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW)
        self.assertEqual((f['status'], f['src'], f['via']), ('active', 'told', 'told:cli'))
        f = self.s.memory_tell('ana', 'like', 'x:found_family', src='agent:luna', now=NOW)
        self.assertEqual((f['status'], f['confirmed']), ('proposed', None))
        for about in ('x:missing', 'g:bad\n', 'a:person', CANARY):
            with self.assertRaises(ValueError):
                self.s.memory_tell('ben', 'like', about, names={}, now=NOW)
        for conf in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                self.s.memory_tell('ben', 'like', 'x:found_family', conf=conf)

    def test_tell_reinforce_and_opposite(self):
        f = self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW)
        same = self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW+1)
        self.assertEqual((f['id'], same['n']), (same['id'], 2))
        opposite = self.s.memory_tell('ben', 'avoid', 'x:found_family', now=NOW+2)
        ended = self.s.memory_explain(f['id'])
        self.assertEqual((ended['status'], ended['into'], ended['evidence']), ('superseded', opposite['id'], []))

    def test_tell_full_and_proposed_cap(self):
        facts = [self.fact(i+1, status='active', about=f'g:{i}') for i in range(40)]
        self.s.memory_save(self.store(facts), [])
        with self.assertRaises(self.s.MemoryFull):
            self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW)
        facts[0]['status'] = 'proposed'
        self.s.memory_save(self.store(facts), [])
        self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW)
        self.assertEqual(self.s.memory_explain('m1')['status'], 'capped')
        self.assertEqual(len(self.s.memory_view()), 40)

    def test_confirm_reject_and_viewer(self):
        f = self.run_curate(self.evidence())['store']['facts'][0]
        for op in (self.s.memory_confirm, self.s.memory_reject, self.s.memory_explain):
            with self.assertRaises(PermissionError):
                op(f['id'], viewer='ana')
        confirmed = self.s.memory_confirm(f['id'], viewer='ben', now=NOW+1)
        self.assertEqual((confirmed['status'], confirmed['strength'], confirmed['confirmed']), ('active', .8, NOW+1))
        rejected = self.s.memory_reject(f['id'], now=NOW+2)
        self.assertEqual((rejected['status'], rejected['end'], rejected['evidence']), ('rejected', NOW+2, []))
        with self.assertRaises(ValueError):
            self.s.memory_confirm(f['id'])

    def cooldown(self, status, days, n=6):
        old = self.fact(status=status, evidence=[], end=NOW-days*self.s.DAY)
        return self.run_curate(self.evidence(n), store=self.store([old]), dry_run=True)

    def test_rejected_doubled_30_days(self):
        self.assertEqual(self.cooldown('rejected', 30)['ops'], [])

    def test_rejected_doubled_100_days(self):
        self.assertEqual(self.cooldown('rejected', 100)['ops'][0]['op'], 'add')

    def test_rejected_100_days_not_doubled(self):
        self.assertEqual(self.cooldown('rejected', 100, n=3)['ops'], [])

    def test_decayed_doubled_30_days(self):
        self.assertEqual(self.cooldown('decayed', 30)['ops'][0]['op'], 'add')

    def contrary(self, ages, action='not_for_us', created=None):
        f = self.fact(status='active', created=NOW-100*self.s.DAY if created is None else created)
        evts = [self.ev(i+1, age=age, action=action) for i, age in enumerate(ages)]
        return self.run_curate(evts, store=self.store([f]), dry_run=True)

    def test_supersede_explicit(self):
        self.assertEqual(self.contrary([10, 20])['store']['facts'][0]['status'], 'superseded')

    def test_contrary_40_days_apart(self):
        self.assertEqual(self.contrary([10, 50])['ops'], [])

    def test_contrary_plays_not_explicit(self):
        f = self.fact(kind='avoid', status='active', created=NOW-100*self.s.DAY)
        self.assertEqual(self.run_curate(self.evidence(2, action='play'), store=self.store([f]), dry_run=True)['ops'], [])

    def test_contrary_before_created(self):
        self.assertEqual(self.contrary([10, 20], created=NOW)['ops'], [])

    def test_contrary_repeats_are_distinct_events(self):
        f = self.fact(status='active', created=NOW-100*self.s.DAY)
        evts = [self.ev(1, age=age, action='not_for_us') for age in (10, 20)]
        evts[1]['id'] += '#2'
        r = self.run_curate(evts, store=self.store([f]), dry_run=True)
        self.assertEqual(r['store']['facts'][0]['status'], 'superseded')

    def test_reason_promotes_existing_implicit_fact(self):
        self.m['paff']['ben']['x:found_family'] = -1.
        evts = self.evidence(action='not_for_us')
        self.run_curate(evts)
        for ev in evts:
            ev['reasons'] = ['seen enough of these']
        r = self.run_curate(evts)
        self.assertEqual(r['store']['facts'][0]['status'], 'active')
        self.assertEqual(self.run_curate(evts)['ops'], [])

    def test_decay(self):
        f = self.fact(seen=NOW-180*self.s.DAY)
        self.assertEqual(self.s.memory_strength(f, NOW), .3)
        f.update(seen=NOW-400*self.s.DAY)
        self.assertEqual(self.run_curate([], store=self.store([f]), dry_run=True)['store']['facts'][0]['status'], 'decayed')
        f.update(src='told')
        self.assertEqual(self.s.memory_strength(f, NOW), .6)
        self.assertEqual(self.run_curate([], store=self.store([f]), dry_run=True)['ops'], [])

    def test_merge(self):
        facts = [self.fact(i+1, about=f'g:{i}', evidence=[f'e{i}']) for i in range(33)]
        facts[0].update(evidence=['a', 'b', 'c'], strength=.7)
        facts[1]['evidence'] = ['a', 'b', 'c', 'd']
        r = self.run_curate([], store=self.store(facts), dry_run=True)
        self.assertEqual(len(self.s.memory_view(store=r['store'])), 32)
        self.assertEqual((r['ops'][0]['op'], r['store']['facts'][1]['into']), ('merge', 'm1'))

    def test_cap(self):
        facts = [self.fact(i+1, about=f'g:{i}', strength=.3 if i == 0 else .6) for i in range(41)]
        facts[1]['status'] = 'active'
        r = self.run_curate([], store=self.store(facts), dry_run=True)
        self.assertEqual((r['store']['facts'][0]['status'], len(self.s.memory_view(store=r['store']))), ('capped', 40))
        self.assertEqual(r['store']['facts'][1]['status'], 'active')

    def test_curate_cannot_overflow_active_cap(self):
        facts = [self.fact(i+1, status='active', about=f'g:{i}') for i in range(40)]
        evts = [self.ev(i, action='not_for_us', reasons=['not my genre']) for i in (1, 2)]
        r = self.run_curate(evts, store=self.store(facts), dry_run=True)
        self.assertEqual(r['refused'][0]['reason'], 'cap')
        self.assertEqual(r['store'], self.store(facts))

    def test_trim(self):
        facts = [self.fact(i+1, status='rejected', end=NOW+i, evidence=[]) for i in range(45)]
        r = self.run_curate([], store=self.store(facts), dry_run=True)
        self.assertEqual((len(r['store']['facts']), r['store']['facts'][0]['id']), (40, 'm6'))

    def test_safe_rewrite_refused(self):
        facts = [self.fact(i+1, status='active', about=f'g:{i}', seen=NOW-(400 if i < 3 else 0)*self.s.DAY) for i in range(8)]
        self.s.memory_save(self.store(facts), [])
        before = self.files()
        r = self.run_curate([])
        self.assertEqual(r['refused'], [{'person': 'ben', 'active_before': 8, 'ending': 3}])
        after = self.files()
        self.assertEqual(before['memory.json'], after['memory.json'])
        self.assertEqual(before['memory.prev.json'], after['memory.prev.json'])
        self.assertEqual([op['op'] for op in self.s._memory_logs()], ['refused'])
        self.assertEqual(r['store'], self.s.memory_load())

    def test_safe_rewrite_one_allowed(self):
        f = self.fact(status='active', seen=NOW-400*self.s.DAY)
        r = self.run_curate([], store=self.store([f]), dry_run=True)
        self.assertEqual((r['refused'], r['ops'][0]['op']), ([], 'decay'))

    def test_idempotent_and_input_not_mutated(self):
        evts = self.evidence()
        before_events = copy.deepcopy(evts)
        self.run_curate(evts, stamp='hash')
        before = self.files()
        second = self.run_curate(evts, stamp='hash')
        self.assertEqual((second['ops'], second['store']['stamp']), ([], 'hash'))
        self.assertEqual(before, self.files())
        self.assertEqual(evts, before_events)

    def test_confirmed_reinforce_floor(self):
        evts = self.evidence()
        f = self.run_curate(evts)['store']['facts'][0]
        self.s.memory_confirm(f['id'], now=NOW)
        f = self.run_curate(self.evidence(4))['store']['facts'][0]
        self.assertEqual((f['strength'], f['status']), (.8, 'active'))

    def test_newest_evidence_bounded_stream_once(self):
        evts = self.evidence(20)
        class Once:
            def __iter__(self):
                if getattr(self, 'used', False):
                    raise AssertionError('second pass')
                self.used = True
                yield from evts
        r = self.s.curate(Once(), self.m, now=NOW, dry_run=True)
        f = r['store']['facts'][0]
        self.assertEqual((f['n'], len(f['evidence'])), (20, 12))
        self.assertNotIn(evts[0]['id'], f['evidence'])
        self.assertEqual(r['stats']['events'], 20)

    def test_modes_preimage_corrupt_and_cleanup(self):
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)
        self.run_curate(self.evidence())
        first = self.files()['memory.json']
        self.s.memory_confirm('m1', now=NOW)
        self.assertEqual(self.files()['memory.prev.json'], first)
        for name in ('memory.json', 'memory.prev.json', 'memory.log.jsonl'):
            self.assertEqual((Path(self.s.DATA)/name).stat().st_mode & 0o777, 0o600)
        (Path(self.s.DATA)/'memory.json').write_bytes(b'broken')
        os.chmod(Path(self.s.DATA)/'memory.json', 0o666)
        self.s._memory = None
        self.assertEqual(self.s.memory_load(), self.s._memory_empty())
        self.assertEqual((Path(self.s.DATA)/'memory.json.bad').read_bytes(), b'broken')
        self.assertEqual((Path(self.s.DATA)/'memory.json.bad').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.s._memory_logs()[-1]['op'], 'corrupt')
        replace = self.s.os.replace
        for name in ('memory.prev.json', 'memory.json'):
            def failing(source, target):
                if Path(target).name == name:
                    raise OSError('injected')
                return replace(source, target)
            with patch.object(self.s.os, 'replace', side_effect=failing):
                with self.assertRaises(OSError):
                    self.s.memory_save(self.store(), [])
            self.assertFalse(list(Path(self.s.DATA).glob('.decide-*')))
            self.assertFalse(list(Path(self.s.DATA).glob('.memory-*')))

    def test_log_rotation(self):
        self.s._memory_log([{'t': NOW, 'op': 'confirm', 'n': i} for i in range(2000)])
        logs = self.s._memory_logs()
        self.assertEqual((len(logs), logs[0]['n'], logs[-1]['n']), (1600, 400, 1999))

    def test_valid_json_with_bad_schema_is_quarantined(self):
        for store in ({'ver': 1, 'seq': 0, 'facts': [], 'forgotten': {}},
                      self.store([self.fact(seen='bad')]), self.store([self.fact(), self.fact()])):
            self.s._memory = None
            (Path(self.s.DATA)/'memory.json').write_text(json.dumps(store))
            self.assertEqual(self.s.memory_load(), self.s._memory_empty())

    def test_forget_scrubs_and_filters(self):
        self.run_curate(self.evidence())
        old = self.s.memory_explain('m1')
        self.s.memory_tell('ana', 'like', 'g:Horror', now=NOW)
        self.assertEqual(self.s.memory_forget('ben', now=NOW), 1)
        self.assertEqual(self.s.memory_forgotten(), {'ben': NOW})
        for raw in self.files().values():
            for value in [old['id'], old['about'], *old['evidence']]:
                self.assertNotIn(value.encode(), raw)
        self.assertEqual(self.files()['memory.json'], self.files()['memory.prev.json'])
        self.assertEqual(len(self.s.memory_view()), 1)

    def test_privacy(self):
        evts = self.evidence()
        evts.extend([self.ev(50, src=CANARY), self.ev(51, action='not_for_us', reasons=[CANARY])])
        self.run_curate(evts)
        self.s.memory_tell('ben', 'like', 'x:found_family', now=NOW)
        self.assertNotIn(CANARY, json.dumps(self.s.memory_explain('m1')))
        self.assertNotIn(CANARY, json.dumps(self.s.memory_view()))
        for raw in self.files().values():
            self.assertNotIn(CANARY.encode(), raw)

    def test_views_cards_summary_and_copies(self):
        self.run_curate(self.evidence())
        self.s.memory_tell('ana', 'like', 'g:Horror', now=NOW)
        self.assertEqual(len(self.s.memory_view()), 2)
        self.assertEqual([f['person'] for f in self.s.memory_view(viewer='ana')], ['ana'])
        with self.assertRaises(PermissionError):
            self.s.memory_view('ben', viewer='ana')
        with self.assertRaises(PermissionError):
            self.s.memory_cards('ben', viewer='ana')
        self.assertEqual(len(self.s.memory_cards('ben', n=1)), 1)
        self.assertNotIn('evidence', self.s.memory_view()[0])
        view = self.s.memory_view(); view[0]['text'] = 'changed'
        self.assertNotEqual(self.s.memory_view()[0]['text'], 'changed')
        summary = self.s.memory_summary(NOW)
        self.assertEqual(summary['since']['proposed'], 1)
        self.assertTrue(all(type(v) is int for d in summary.values() for v in d.values()))

    def test_cards_order(self):
        facts = [self.fact(1, strength=.6, n=2), self.fact(2, strength=.7), self.fact(3, strength=.6, n=5)]
        self.s.memory_save(self.store(facts), [])
        self.assertEqual([f['id'] for f in self.s.memory_cards('ben')], ['m2', 'm3', 'm1'])

    def test_why_active_together_kind_and_length(self):
        self.ev(1)
        self.run_curate(self.evidence())
        self.assertIsNone(self.s.memory_why('tmdb:1', 'movie', 'ben'))
        self.s.memory_confirm('m1', now=NOW)
        self.assertEqual(self.s.memory_why('tmdb:1', 'movie', 'ben'),
                         {'why': 'Because you told me you like found family', 'fact': 'm1', 'rung': 'memory', 'told': True, 'strength': .8})
        self.assertIsNone(self.s.memory_why('tmdb:1', 'tv', 'ben'))
        self.assertIsNone(self.s.memory_why('tmdb:1', 'movie', None))
        self.assertIsNone(self.s.memory_why('tmdb:1', 'movie', 'together'))
        self.s.memory_tell('ana', 'like', 'x:found_family', now=NOW)
        self.assertEqual(self.s.memory_why('tmdb:1', 'movie', 'together')['why'], 'You both like found family')
        self.names['x:found_family'] = 'a'*100
        self.assertLessEqual(len(self.s.memory_why('tmdb:1', 'movie', 'together')['why']), 60)
        self.s.memory_tell('ben', 'avoid', 'x:found_family', now=NOW)
        self.assertIsNone(self.s.memory_why('tmdb:1', 'movie', 'ben'))

    def test_why_index_and_decayed_order(self):
        self.ev(1)
        self.s._facets['tmdb:1']['g'] = ['Horror']
        facts = [self.fact(status='active', strength=.9, seen=NOW-180*self.s.DAY),
                 self.fact(2, status='active', about='g:Horror', strength=.6)]
        self.s.memory_save(self.store(facts), [])
        self.assertEqual(self.s.memory_why('tmdb:1', 'movie', 'ben')['fact'], 'm2')
        class NoScan(list):
            def __iter__(self):
                raise AssertionError('index scanned store')
        self.s._memory['facts'] = NoScan(self.s._memory['facts'])
        self.assertEqual(self.s.memory_why('tmdb:1', 'movie', 'ben')['fact'], 'm2')

    def test_due_no_file_io_and_daily(self):
        self.s._profiles.clear()
        self.assertFalse(self.s.memory_due(NOW))
        self.s._profiles['household'] = {'hash': 'h'}
        with patch('builtins.open', side_effect=AssertionError('due opened a file')):
            self.assertTrue(self.s.memory_due(NOW))
            self.s._memory = self.store(stamp='h', at=NOW)
            self.assertTrue(self.s.memory_due(NOW+self.s.DAY-1))
            self.assertTrue(self.s.memory_due(NOW+self.s.DAY))
        with patch.object(self.s, 'events', return_value=iter(self.evidence())):
            result = self.s.memory_daily(NOW, m=self.m, dry_run=True)
        self.assertEqual((result['ops'], result['live']['ben']), ({'add': 1}, 1))
        self.assertEqual(self.files(), {})

    def test_section_has_no_hash(self):
        section = (ROOT/'server.py').read_text().split('# --- taste memory (W10a) ---')[1].split('# --- searches')[0]
        self.assertNotIn('hashlib', section)

    def test_report_and_cli_canary(self):
        from eval.memory_report import report
        with tempfile.TemporaryDirectory() as data:
            snapshot = Path(data)
            write_legacy(snapshot)
            entries, trakt, facets = [], [], {}
            for i in range(1, 9):
                key = f'tmdb:{i}'
                age = 8 if i == 1 else 0
                item = {'id': key, 'type': 'movie', 'title': CANARY, 'year': 2020,
                        'signal': 'watched', 'weight': 1., 'at': (NOW-age*self.s.DAY)*1000,
                        'backfilled': i < 5}
                trakt.append(item)
                entries.append({'key': key, 'id': i, 'kind': 'movie', 'stype': 'movie', 'title': CANARY,
                                'year': '2020', 'signal': 'watched', 'weight': 1., 'age': age,
                                'who': 'owner', 'by': ['ben'], 'flags': [], 'genres': [], 'item': item})
                facets[key] = {'title': CANARY, 'kind': 'movie', 'x': {'found_family': 2}}
            values = {'profile.json': {'hash': 'synthetic', 'built_at': NOW, 'entries': entries,
                                      'seen': [e['key'] for e in entries], 'negative': []},
                      'trakt.json': {'items': trakt}, 'history.json': {'items': []},
                      'facets.json': {'items': facets}, 'taste.json': {'people': {}}, 'learn.json': {'hits': []}}
            for name, value in values.items():
                (snapshot/name).write_text(json.dumps(value))
            with redirect_stdout(io.StringIO()):
                result = report(snapshot)
            self.assertEqual(set(result), {'shares', 'hit_ab', 'curate', 'holdout', 'budget'})
            self.assertEqual(result['shares']['backfill'], .5)
            self.assertEqual(result['curate']['second_run_ops'], 0)
            self.assertNotIn(CANARY, json.dumps(result))
            out = io.StringIO()
            with patch('time.time', return_value=NOW):
                code = main(['--snapshot', data, 'memory', '--json'], out=out)
            self.assertEqual(code, 0)
            self.assertNotIn(CANARY, out.getvalue())
            self.assertFalse((snapshot/'memory.json').exists())

    def test_hit_gate_model_vector_signature(self):
        from array import array
        p = {'hash': 'synthetic', 'seen': set(), 'entries': []}
        self.s.EMB_DIM = 2
        for i in range(8):
            key = f'tmdb:{i+1}'
            self.s._facets[key] = {'kind': 'movie', 'g': ['Horror' if i < 4 else 'Drama'],
                                   'x': {'found_family' if i < 4 else 'cozy_case': 2}}
            self.s._emb[key] = array('f', [1., 0.] if i < 4 else [0., 1.])
            p['entries'].append({'key': key, 'kind': 'movie', 'id': i+1, 'title': 'Synthetic', 'year': '2020',
                                 'weight': 1., 'signal': 'watched', 'who': 'owner', 'by': ['ben'], 'flags': [], 'genres': []})
        self.s._learn['hits'] = []
        with redirect_stdout(io.StringIO()):
            a = self.s.build_model(p)
            va = self.s.taste_vector(p)
            self.s._profiles['household'] = p
            self.s.get_model()
        self.s._learn['hits'] = [{'key': e['key']} for e in p['entries'] if e['weight'] > 0][:4]
        with redirect_stdout(io.StringIO()):
            b = self.s.build_model(p)
        self.assertTrue(a == b, 'hit gate changed model')
        self.assertEqual(va, self.s.taste_vector(p))
        with patch.object(self.s, 'build_model', side_effect=AssertionError('rebuilt for hits')):
            self.s.get_model()
        with patch.object(self.s, 'HIT_TASTE', True), redirect_stdout(io.StringIO()):
            c = self.s.build_model(p)
        self.assertTrue(a != c, 'patched hit gate did not change model')


@unittest.skipUnless(SNAPSHOT is not None and SNAPSHOT.is_dir(), 'set SKALD_SNAPSHOT_U to run snapshot gates')
class MemoryCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)/'state'
        # Public input allowlist; never read credential files for a hash.
        from skald.engine import DATA_FILES
        self.paths = [SNAPSHOT/name for name in DATA_FILES if (SNAPSHOT/name).exists()]
        self.before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths}
        self.addCleanup(self.unchanged)

    def unchanged(self):
        self.assertEqual(self.before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths})
        self.assertFalse((SNAPSHOT/'memory.json').exists())

    def cli(self, *args):
        out = io.StringIO()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), redirect_stderr(io.StringIO()):
            status = main(['--snapshot', str(SNAPSHOT), '--state-dir', str(self.state), 'memory', *args], out=out)
        return status, out.getvalue()

    def test_json(self):
        code, raw = self.cli('--json')
        self.assertEqual(code, 0)
        data = json.loads(raw)
        self.assertTrue(data['dry_run'])
        self.assertEqual(data['refused'], 0)
        self.assertTrue(all(len(f['text']) <= 60 for f in data['facts']))
        self.assertTrue(all(f['status'] == 'proposed' for f in data['facts']))
        self.assertTrue(6 <= len(data['facts']) <= 12)

    def test_ana(self):
        code, raw = self.cli('--person', ANA, '--json')
        self.assertEqual((code, json.loads(raw)['facts']), (0, []))

    def test_unknown_explain(self):
        self.assertEqual(self.cli('--explain', 'm999', '--json')[0], 2)

    def test_text_header(self):
        code, raw = self.cli()
        self.assertEqual(code, 0)
        self.assertTrue(raw.startswith('memory (dry run, nothing written)'))


if __name__ == '__main__':
    unittest.main()
