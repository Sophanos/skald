"""Per-person precedence, entry provenance, intake dedup and reversible reactions."""
from skald.members import rid
import copy
import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from eval.precedence import cases, event, fold_case, signals, views
from eval.profile_golden import DAY, NOW, ROOT, dump, offline_server
from skald.engine import Engine


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.context = offline_server(ROOT / 'server.py')
        self.s = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def raw(self, action='love', age=10, src='letterboxd', who='ana', **kw):
        return {'key': 'tmdb:1', 'kind': 'movie', 'title': 'Synthetic', 'year': '2020',
                'who': who, 'action': action, 'src': src, 'conf': 1., 'n': 1,
                't': datetime.datetime.fromtimestamp(NOW-age*DAY, datetime.timezone.utc).date().isoformat(), **kw}

    def test_evidence_in_both_file_orders(self):
        s = self.s
        for name, evts, ana, ben in cases(s):
            for reverse in (False, True):
                with self.subTest(case=name, reverse=reverse):
                    entries = fold_case(s, evts, reverse)
                    self.assertEqual(signals(s, entries), {'ana': [] if ana is None else [ana],
                                                         'ben': [] if ben is None else [ben]})
                    if name == 'I5': self.assertEqual(len(entries), 1)
                    if name in ('S1', 'S2', 'S5'):
                        self.assertEqual(entries[0]['signal'], 'loved' if name == 'S5' else 'watched')
                        # A favorite without playback cannot protect shared play credit.
                        self.assertEqual(entries[0]['by'], ['ben'] if name == 'S5' else ['together'])
                    if name == 'S6':
                        self.assertEqual(entries, fold_case(s, evts[:1]))
                    if name == 'R5':
                        pv = views(s, entries)
                        self.assertAlmostEqual(pv['ben'][0]['weight'], 1.*.35)
                        self.assertAlmostEqual(pv['ana'][0]['weight'], -3.)
                    if name in ('I1', 'I3'):
                        self.assertAlmostEqual(entries[0]['weight'], 3. if name == 'I1' else 2.)

    def test_owner_decisions_are_single_constant_switches(self):
        s = self.s
        rows = {name: evts for name, evts, _, _ in cases(s)}
        switches = [('IMPORT_TOLD_LOCKED', 'D1', 'ana', 'disliked'),
                    ('IMPORT_REMOTE_NEWEST', 'R3', 'ana', 'disliked'),
                    ('IMPORT_COMPLETION_FIRST', 'A4', 'ana', 'abandoned'),
                    ('IMPORT_EXPLICIT_FIRST', 'K4', 'ben', 'loved'),
                    ('IMPORT_SHARED_OPINIONS', 'S2', 'ana', 'watched'),
                    ('OPINION_TRUST_FIRST', 'T2', 'ana', 'loved'),
                    ('REMOTE_PER_PERSON', 'R1', 'ben', 'disliked')]
        for constant, name, person, expected in switches:
            with self.subTest(constant=constant), patch.object(s, constant, False):
                self.assertEqual(signals(s, fold_case(s, rows[name]))[person], [expected])

    def test_ties_each_comparator_field_and_undated(self):
        s = self.s
        make = lambda action, **kw: event(s, action, 10, **kw)
        pairs = [
            # Trust, channel, priority, person specificity, source name, then record id.
            (make('love', src='import:netflix'), make('like'), 'liked', 'import'),
            (make('love'), make('like', src='interview'), 'liked', 'remote'),
            (make('love'), make('not_for_us', src='import:imdb'), 'disliked', 'import'),
            (make('love', who='both', eid='a'), make('love', eid='z'), 'loved', 'import'),
            (make('love', src='import:letterboxd', eid='a'), make('like', src='import:imdb', eid='z'), 'loved', 'import'),
            (make('love', eid='z'), make('love', eid='a'), 'loved', 'import'),
        ]
        for a, b, signal, who in pairs:
            for reverse in (False, True):
                with self.subTest(a=a['src'], b=b['src'], reverse=reverse):
                    entries = fold_case(s, [a, b], reverse)
                    pv = views(s, entries)['ana']
                    self.assertEqual((pv[0]['signal'], pv[0]['who']), (signal, who))
        a, b = make('love', eid='a'), make('love', eid='z')
        a['conf'], b['conf'] = .5, 1.
        for reverse in (False, True):
            self.assertAlmostEqual(fold_case(s, [a, b], reverse)[0]['weight'], 1.5)
        a, b = make('love', src='import:imdb'), make('love', src='import:letterboxd')
        a['conf'], b['conf'] = .5, 1.
        for reverse in (False, True):
            self.assertEqual(fold_case(s, [a, b], reverse)[0]['flags'], ['import', 'imdb'])
        a, b = make('love', who='both'), make('love', eid='z')
        a['conf'] = .5
        for reverse in (False, True):
            pv = views(s, fold_case(s, [a, b], reverse))
            self.assertAlmostEqual(pv['ana'][0]['weight'], 3.)
            self.assertAlmostEqual(pv['ben'][0]['weight'], 1.5)
        a, b = make('not_for_us'), make('like', src='import:imdb')
        a['t'] = None
        for reverse in (False, True): self.assertEqual(signals(s, fold_case(s, [a, b], reverse))['ana'], ['liked'])
        # Import t may be in the future; its clamped decay age is not its claim day.
        b['t'] = NOW+DAY
        self.assertEqual(signals(s, fold_case(s, [make('love'), b]))['ana'], ['liked'])

    def test_inferred_rank_before_date_in_either_order(self):
        s = self.s
        for strong, weak in (('seen', 'abandon'), ('abandon', 'interest'), ('like', 'abandon')):
            for ages in ((400, 10), (10, 400)):
                evts = [event(s, strong, ages[0]), event(s, weak, ages[1], src='import:imdb')]
                for ev in evts:
                    if ev['action'] == 'abandon':
                        ev.update(src='import:netflix', conf=.4, ctx={'src': 'netflix'})
                for rev in (False, True):
                    self.assertEqual(signals(s, fold_case(s, evts, rev))['ana'], [s.IMPORT_SIGNALS[strong][0]])

        evts = [event(s, 'love', 400, src='trakt', who='ben'), event(s, 'seen', 10, who='ben')]
        for rev in (False, True): self.assertEqual(signals(s, fold_case(s, evts, rev))['ben'], ['loved'])

    def test_interest_cap_and_adapter_order_are_unchanged(self):
        s = self.s
        raw = [self.raw('interest', 0, src='chrome', who='both', key=f'tmdb:{i}', id=f'{i:032x}') for i in range(1, 26)]
        evts = list(s.import_events(NOW, raw))
        expected = list(s._import_event_entries(NOW, iter(copy.deepcopy(evts))))
        entries, _, _ = s.fold_events((('import', iter(evts)),), NOW)
        self.assertEqual(dump(entries), dump(expected))
        self.assertEqual([e['key'] for e in entries], [f'tmdb:{i}' for i in range(25, 5, -1)])
        for p in s.PEOPLE: self.assertEqual(sum(p in e['by'] for e in entries), 20)

    def test_equal_remote_actions_keep_legacy_bytes(self):
        s = self.s
        a, b = event(s, 'love', 400, src='remote', who='ben'), event(s, 'love', 10, src='remote')
        for evts in ([a, b], [b, a]):
            entries = s._feedback_event_entries(NOW, iter(evts))
            expected = s._feedback_event_entries(NOW, iter([b]))
            expected[0]['by'] = ['ana', 'ben']
            self.assertEqual(dump(entries), dump(expected))

    def test_joint_agreement_preserves_owner_play_bytes(self):
        s = self.s
        base = event(s, 'love', 400, src='trakt', who='ben')
        reactions = [event(s, 'love', 10, src='remote', who=p) for p in s.PEOPLE]
        expected = fold_case(s, [base])[0]
        expected.update(weight=3., by=['ben', 'ana'], flags=['remote'])
        for rev in (False, True):
            self.assertEqual(dump(fold_case(s, [base, *reactions], rev)), dump([expected]))

    def test_D1_joint_reaction_preserves_solo_play(self):
        s = self.s
        for src in ('trakt', 'tv'):
            base = event(s, 'seen', 100, src=src, who='ben', played=True)
            base['kind'] = 'tv'
            base['ctx']['item'].update(type='series', episodes_played=3, last_episode=[2, 3], genres=['Drama'])
            base['ctx']['genres'] = ['Drama']
            reactions = [event(s, 'love', 5, src='remote', who=p) for p in s.PEOPLE]
            for ev in reactions: ev['kind'] = 'tv'
            for rev in (False, True):
                with self.subTest(src=src, reverse=rev):
                    entries = fold_case(s, [base, *reactions], rev)
                    e = entries[0]
                    self.assertEqual((len(entries), e['who'], e['age'], e['signal']),
                                     (1, 'owner' if src == 'trakt' else 'tv', 100, 'loved'))
                    self.assertEqual((e['item'], e['genres']), (base['ctx']['item'], ['Drama']))
                    self.assertTrue(s.real_play(e))
                    self.assertEqual(signals(s, entries), {p: ['loved'] for p in s.PEOPLE})
                    s._profiles['household'] = {'entries': entries}
                    with patch.object(s, 'show_catchup', return_value=None) as catchup, \
                            patch.object(s, 'movie_collections', return_value=[]):
                        s.catchup_feed('en-US')
                    catchup.assert_called_once()
                    if src == 'tv':
                        at = e['item']['at']/1000
                        day = s.local_now(at).date().isoformat()
                        s._learn.update(served={e['key']: {'rows': {rid('foryou', s.CONFIG): {'days': [day]}}}},
                                        rows={}, hits=[], credited={}, ctx=[])
                        with patch('builtins.print'):
                            s.learn_from_plays({'entries': entries})
                        s.log_play_context({'entries': entries})
                        self.assertIn(e['key'], s._learn['credited'])
                        self.assertEqual(len(s._learn['ctx']), 1)
                        self.assertEqual(s.session_plays({'t': at-1, 'until': at+1}), entries)

    def test_D2_import_agreement_uses_import_claim_day(self):
        s = self.s
        tv = event(s, 'seen', 100, src='tv', who='together', played=True)
        for action, reaction, expected in (('not_for_us', 'love', 'disliked'), ('love', 'not_for_us', 'loved')):
            remote = event(s, reaction, 50, src='remote')
            imported = event(s, action, 10)
            fresh = fold_case(s, [tv, imported, remote])
            base = copy.deepcopy(fresh)
            s._merge_import_events(base, {e['key']: e for e in base}, NOW, iter([imported]))
            s._merge_feedback_events(base, {}, NOW, iter([remote]))
            self.assertEqual(signals(s, base)['ana'], [expected])
            self.assertEqual(views(s, base), views(s, fresh))

    def test_D2_engine_undo_and_refresh_match_fresh(self):
        s = self.s
        tv = event(s, 'seen', 100, src='tv', who='together', played=True)
        for action, reaction in (('not_for_us', 'love'), ('love', 'not_for_us')):
            imports = [self.raw(action, 10, id='a'*32)]
            remote = event(s, reaction, 50, src='remote')
            active = {(remote['key'], remote['who']): {**remote, 'row': None}}
            base = fold_case(s, [tv, event(s, action, 10), remote])
            engine = Engine.__new__(Engine)
            engine._server, engine._base, engine._work, engine._index = s, {'entries': base}, Path(s.DATA), {}
            with patch.object(engine, '_build', return_value={}), \
                    patch.object(s, 'feedback_state', return_value={'active': active}):
                s.save_imports(imports)
                engine._refresh()
                with self.subTest(action=action, stage='refresh'):
                    self.assertEqual(views(s, engine._profile['entries']), views(s, base))
                s.undo_import(imports[0]['id'])
                active.clear()
                engine._refresh()
                fresh = fold_case(s, [tv])
                with self.subTest(action=action, stage='undo'):
                    self.assertEqual(views(s, engine._profile['entries']), views(s, fresh))
                    self.assertAlmostEqual(views(s, engine._profile['entries'])['ana'][0]['weight'], .35)
                s.save_imports([self.raw('love', 200, src='imdb', id='b'*32), *imports])
                s.undo_import(imports[0]['id'])
                engine._refresh()
                fresh = fold_case(s, [tv, event(s, 'love', 200, src='import:imdb')])
                with self.subTest(action=action, stage='older love left'):
                    self.assertEqual(views(s, engine._profile['entries']), views(s, fresh))
                    self.assertAlmostEqual(views(s, engine._profile['entries'])['ana'][0]['weight'], .75)
                s.save_imports([])
                engine._base = {'entries': fold_case(s, [tv])}
                engine._base['entries'][0]['shared'] = {'signal': 'disliked', 'weight': -3., 'wsrc': 'import'}
                engine._refresh()
                self.assertEqual(views(s, engine._profile['entries']), views(s, fold_case(s, [tv])))

    def test_D3_claim_records_end_with_fold_and_on_error(self):
        s = self.s
        raw = [self.raw('seen', 400, key=f'tmdb:{i}', id=f'{i:032x}') for i in range(1, 301)]
        entries = []
        s.merge_imports(entries, {}, NOW, raw)
        self.assertFalse(getattr(s._claim_records, 'entries', {}))
        active = {}
        for i in range(1, 301):
            ev = event(s, 'not_for_us', 10, src='remote', eid=str(i))
            ev['key'] = f'tmdb:{i}'
            active[(ev['key'], 'ana')] = {**ev, 'row': None}
        s.merge_feedback(entries, {}, NOW, active)
        self.assertEqual(signals(s, entries)['ana'], ['disliked']*300)
        self.assertFalse(s._claim_records.entries)
        def broken():
            yield event(s, 'love', 10)
            raise ValueError('synthetic failed event')
        with patch.object(s, 'import_events', return_value=broken()), self.assertRaises(ValueError):
            s.merge_imports([], {}, NOW, [])
        self.assertFalse(s._claim_records.entries)
        with patch.object(s, 'remote_events', return_value=broken()), self.assertRaises(ValueError):
            s.merge_feedback([], {}, NOW, {})
        self.assertFalse(s._claim_records.entries)
        failed = []
        records = s.merge_imports(failed, {}, NOW, raw[:1])
        with patch.object(s, '_claim_rank', side_effect=ValueError('synthetic failed rank')), self.assertRaises(ValueError):
            s.merge_feedback(failed, {}, NOW, active, records=records)
        self.assertFalse(records)
        self.assertFalse(s._claim_records.entries)
        s.fold_events((('import', iter([event(s, 'seen', 10)])),), NOW)
        self.assertFalse(s._claim_records.entries)
        value = entries[0]
        expected = s._claim_rank(value, NOW)
        s._claim_records.entries[id(value)] = ({}, (NOW, 'import:letterboxd', 'love', 'ana', 'stale'))
        self.assertEqual(s._claim_rank(value, NOW), expected)
        s._feedback_event_entries(NOW, iter([event(s, 'love', 10, src='remote')]))
        self.assertEqual(len(s._claim_records.entries), 1)
        s._claim_records.entries.clear()

    def test_PEOPLE_together_expansion(self):
        s = self.s
        with patch.object(s, 'PEOPLE', (*s.PEOPLE, 'third')):
            for by in (['together'], ['both'], ['together', 'extra']):
                self.assertEqual(s._expand_together(by), set(s.PEOPLE) | ({'extra'} if 'extra' in by else set()))
            raw = self.raw('love', who='both', id='a'*32)
            self.assertEqual(next(s.import_events(NOW, [raw]))['by'], sorted(s.PEOPLE))
        self.assertEqual(s._expand_together([]), set())
        self.assertEqual(s._expand_together(['ben']), {'ben'})

    def test_D4_undated_batch_row_ranks_as_written_day(self):
        s = self.s
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        for rev in (False, True):
            s.save_imports([])
            undated = self.raw('love')
            del undated['t']
            rows = [self.raw('not_for_us', 400, n=58), undated]
            if rev: rows.reverse()
            added = s.write_imports(rows)
            self.assertEqual((added[0]['action'], added[0]['t'], added[0]['n']), ('love', today, 58))

    def test_agreeing_reactors_compare_their_own_days(self):
        s = self.s
        evts = [event(s, 'not_for_us', 200, src='remote'),
                event(s, 'not_for_us', 10, src='remote', who='ben'), event(s, 'love', 20)]
        for rev in (False, True):
            self.assertEqual(signals(s, fold_case(s, evts, rev)), {'ana': ['loved'], 'ben': ['disliked']})

    def test_same_signal_imports_choose_newest_and_meh_is_explicit(self):
        s = self.s
        for rev in (False, True):
            evts = [event(s, 'love', 400), event(s, 'love', 10, src='import:netflix')]
            self.assertAlmostEqual(fold_case(s, evts, rev)[0]['weight'], 2.4)
            evts = [event(s, 'meh', 400), event(s, 'seen', 10, src='import:imdb')]
            self.assertAlmostEqual(fold_case(s, evts, rev)[0]['weight'], .075)

    def test_engine_remerge_matches_fresh_R1_R2_R3(self):
        s = self.s
        for name, evts, _, _ in cases(s):
            if name not in ('R1', 'R2', 'R3'): continue
            for rev in (False, True):
                ordered = list(reversed(evts)) if rev else evts
                remote = [copy.deepcopy(e) for e in ordered if e['src'] == 'remote']
                imported = [copy.deepcopy(e) for e in ordered if e['src'].startswith('import:')]
                base = fold_case(s, remote)
                by_key = {e['key']: e for e in base}
                # Public wrappers exercise the same seams used by Engine._refresh.
                raw = [self.raw(e['action'], (NOW-e['t'])/DAY, e['ctx']['src'], e['who'], id=f'{i+1:032x}')
                       for i, e in enumerate(imported)]
                active = {(e['key'], e['who']): {**e, 'reasons': [], 'row': rid('foryou', s.CONFIG)} for e in remote}
                records = s.merge_imports(base, by_key, NOW, raw)
                s.merge_feedback(base, by_key, NOW, active, records=records)
                self.assertTrue(all(any(value is e for e in base) for value in by_key.values()), name)
                fresh = fold_case(s, ordered)
                key = lambda e: (e['key'], e['by'])
                self.assertEqual(dump(sorted(base, key=key)), dump(sorted(fresh, key=key)), name)
                self.assertEqual(views(s, base), views(s, fresh), name)

    def test_engine_remerge_restores_both_import_slots_once(self):
        s = self.s
        for reactors in (('ana',), ('ben', 'ana')):
            remote = [event(s, 'not_for_us', 200, src='remote', who=p) for p in reactors]
            base = copy.deepcopy(fold_case(s, remote))
            raw = self.raw('love', 10, who='both', id='a'*32)
            records = s.merge_imports(base, {e['key']: e for e in base}, NOW, [raw])
            active = {(e['key'], e['who']): {**e, 'row': rid('foryou', s.CONFIG)} for e in remote}
            s.merge_feedback(base, {}, NOW, active, records=records)
            fresh = fold_case(s, [event(s, 'love', 10, who='both'), *remote])
            self.assertEqual(dump(base), dump(fresh))
            self.assertEqual(base[0]['by'], ['ana', 'ben'])

    def test_engine_remote_meh_stays_explicit_without_side_metadata(self):
        s = self.s
        base = copy.deepcopy(fold_case(s, [event(s, 'meh', 10, src='remote')]))
        expected = copy.deepcopy(base)
        s.merge_imports(base, {e['key']: e for e in base}, NOW, [self.raw('not_for_us', 400, id='a'*32)])
        self.assertEqual(base, expected)

    def test_batch_date_then_same_day_ties_max_count_and_keep_ids(self):
        s = self.s
        for reverse in (False, True):
            for old_action, new_action in (('not_for_us', 'love'), ('love', 'seen')):
                s.save_imports([])
                rows = [self.raw(old_action, 400, n=58), self.raw(new_action, 10)]
                if reverse: rows.reverse()
                added = s.write_imports(rows)
                self.assertEqual((added[0]['action'], added[0]['n']), (new_action, 58))
            s.save_imports([])
            rows = [self.raw('love'), self.raw('not_for_us')]
            if reverse: rows.reverse()
            self.assertEqual(s.write_imports(rows)[0]['action'], 'not_for_us')
        s.save_imports([])
        rows = [self.raw('love', 400, id='a'*32), self.raw('like', 10, id='b'*32)]
        self.assertEqual(s.write_imports(rows, keep_ids=True), (rows, 0, []))

    def test_same_source_replace_undo_newest_and_remote_undo(self):
        s = self.s
        for src in ('letterboxd', 'imdb'):
            s.save_imports([])
            a = s.write_imports([self.raw('love', 400)])[0]
            b = s.write_imports([self.raw('not_for_us', 10, src=src)])[0]
            self.assertEqual(signals(s, s.fold_events((('import', s.import_events(NOW)),), NOW)[0])['ana'], ['disliked'])
            s.undo_import(b['id'])
            self.assertEqual(s.active_imports(), [a])
            self.assertEqual(signals(s, s.fold_events((('import', s.import_events(NOW)),), NOW)[0])['ana'], ['loved'])
        Path(s.DATA, 'feedback.jsonl').write_text('')
        s._remote_cache.clear()
        base = [event(s, 'seen', 100, src='tv', who='together', played=True)]
        body = {'key': 'tmdb:1', 'kind': 'movie', 'title': 'Synthetic', 'year': '2020', 'who': 'ana',
                'action': 'not_for_us', 'reasons': [], 'row': rid('foryou', s.CONFIG)}
        reaction = s.write_feedback(body)
        reacted = fold_case(s, base + list(s.remote_events(NOW)))
        self.assertEqual(signals(s, reacted)['ana'], ['disliked'])
        s.write_feedback({**body, 'undo': reaction['id']})
        self.assertEqual(fold_case(s, base + list(s.remote_events(NOW))), fold_case(s, base))

    def test_told_lock_on_an_existing_play_and_remote_override(self):
        s = self.s
        tv = event(s, 'seen', 100, src='tv', who='together', played=True)
        told = event(s, 'love', 400, src='told')
        imported = event(s, 'not_for_us', 10)
        entries = fold_case(s, [tv, told, imported])
        self.assertEqual(signals(s, entries)['ana'], ['loved'])
        remote = event(s, 'not_for_us', 0, src='remote')
        self.assertEqual(signals(s, fold_case(s, [tv, told, imported, remote]))['ana'], ['disliked'])


if __name__ == '__main__':
    unittest.main()
