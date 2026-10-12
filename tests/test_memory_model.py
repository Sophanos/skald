"""Memory affects taste and serving, without feeding curate or writing previews."""
from skald.members import rid
from skald import members as row_config
from decide_kit import write_legacy
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from eval.profile_golden import NOW, ROOT, offline_server
from skald import Engine
from skald.__main__ import main


class MemoryModelTests(unittest.TestCase):
    def setUp(self):
        ctx = offline_server(ROOT / 'server.py')
        self.s = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        s = self.s
        s._facets = {f'tmdb:{i}': {'kind': 'movie', 'g': ['Drama'] + (['Horror'] if i < 3 else []),
                                  'x': {'found_family': 2 if i < 3 else 1}} for i in range(10)}
        self.names = {'g:Horror': 'Horror', 'g:Drama': 'Drama', 'x:found_family': 'found family'}
        s._model['m'] = {'names': self.names}
        s._profiles['household'] = {'seen': set(), 'hash': 'h', 'entries': []}
        s._memory = s._memory_empty()
        s._memory_index.clear()
        s._memory_veto.clear()

    def fact(self, seq=1, **kw):
        return {'id': f'm{seq}', 'person': 'ben', 'kind': 'like', 'about': 'g:Horror',
                'src': 'told', 'status': 'active', 'strength': 1., 'seen': NOW, 'created': NOW,
                'confirmed': None, 'end': None, 'text': 'likes Horror', 'n': 1, 'gates': {},
                'evidence': [f'op:{seq}'], 'via': 'told:cli', 'into': None, **kw}

    def store(self, *facts):
        return {**self.s._memory_empty(), 'seq': max((int(f['id'][1:]) for f in facts), default=0), 'facts': list(facts)}

    def install(self, *facts):
        self.s.memory_save(self.store(*facts), [])

    def test_floor_sign_no_stacking_and_input_unchanged(self):
        s = self.s
        paff = {'g:Horror': .2, 'g:Drama': 2., 'x:found_family': -.6}
        facts = self.store(self.fact(), self.fact(2, kind='avoid', about='x:found_family'))
        out = s.memory_pseudo('ben', paff, NOW, facts)
        self.assertEqual(out, {'g:Horror': max(.2, .2*s.MEMORY_PSEUDO), 'g:Drama': 2., 'x:found_family': min(-.6, -.2*s.MEMORY_PSEUDO)})
        # With enough positive values the person's own p99 sets the floor.
        paff.update({f'k:{i}': 2. for i in range(200)})
        out = s.memory_pseudo('ben', paff, NOW, facts)
        self.assertEqual((out['g:Horror'], out['x:found_family']), (2.*s.MEMORY_PSEUDO, -2.*s.MEMORY_PSEUDO))
        self.assertEqual((paff['g:Horror'], paff['x:found_family']), (.2, -.6))
        self.assertEqual(s.memory_pseudo('ben', out, NOW, facts), out)

    def test_empty_identity_and_person_scope(self):
        paff = {'g:Horror': .2}
        out = self.s.memory_pseudo('ben', paff, NOW, self.store())
        self.assertEqual(out, paff)
        self.assertIsNot(out, paff)
        self.assertEqual(self.s.memory_pseudo('ana', paff, NOW, self.store(self.fact())), paff)

    def test_only_active_told_or_confirmed(self):
        s = self.s
        for fields in ({'status': 'proposed'}, {'src': 'implicit'}, {'src': 'reason'}):
            self.assertEqual(s.memory_pseudo('ben', {}, NOW, self.store(self.fact(**fields))), {})
        self.assertEqual(s.memory_pseudo('ben', {}, NOW, self.store(self.fact(src='implicit', confirmed=NOW))), {'g:Horror': s.MEMORY_PSEUDO})

    def test_rejected_pull_no_flip_and_cooldown(self):
        f = self.fact(status='rejected', end=NOW)
        for a in (-2., 2.):
            self.assertEqual(self.s.memory_pseudo('ben', {'g:Horror': a}, NOW, self.store(f)), {'g:Horror': a*.5})
        f['end'] = NOW-self.s.MEMORY_COOLDOWN*self.s.DAY
        self.assertEqual(self.s.memory_pseudo('ben', {'g:Horror': 2.}, NOW, self.store(f)), {'g:Horror': 2.})

    def test_model_signature_changes_on_tell_without_file_read(self):
        s = self.s
        with patch.object(s, 'build_model', return_value={'aff': {}, 'paff': {}, 'cal': None, 'taste_line': ''}) as build, redirect_stdout(io.StringIO()):
            s.get_model()
            before = s._model['sig']
            s.memory_tell('ben', 'like', 'g:Horror', names=self.names)
            with patch('builtins.open', side_effect=AssertionError('signature read a file')):
                s.get_model()
            self.assertEqual(build.call_count, 2)
            self.assertEqual(s._model['sig'][:3], before[:3])
            self.assertGreater(s._model['sig'][3], before[3])

    def test_build_model_preserves_paff0_and_reason_floor(self):
        s = self.s
        p = s._profiles['household']
        with redirect_stdout(io.StringIO()):
            before = s.build_model(p)
            s.memory_tell('ben', 'avoid', 'g:Horror', names=self.names)
            after = s.build_model(p)
        self.assertNotIn('paff0', before)
        self.assertEqual(after['paff0'], before['paff'])
        self.assertEqual(after['paff']['ben']['g:Horror'], -s.MEMORY_PSEUDO)

    def test_empty_memory_file_model_identity(self):
        s = self.s
        with redirect_stdout(io.StringIO()):
            before = s.build_model(s._profiles['household'])
            s.memory_save(s._memory_empty(), [])
            s._memory = None
            after = s.build_model(s._profiles['household'])
        self.assertEqual(before, after)

    def test_curate_uses_pre_memory_ranks(self):
        s = self.s
        m = {'paff': {'ben': {'g:Horror': 99.}}, 'paff0': {'ben': {}}, 'names': self.names, 'nexp': {}}
        evts = [s._event(NOW-i*8*s.DAY, f'remote:movie:tmdb:{i}', f'tmdb:{i}', 'movie', 'Synthetic', '2020',
                         'ben', ['ben'], 'like', 'remote', {}) for i in range(3)]
        self.install(self.fact())
        r = s.curate(iter(evts), m, NOW, dry_run=True)
        again = s.curate(iter(evts), m, NOW, store=r['store'], dry_run=True)
        self.assertEqual(r['stats']['candidates']['ben'], 0)
        self.assertEqual(r['ops'], [])
        self.assertEqual(again['ops'], [])

    def test_veto_person_both_other_and_strength(self):
        s = self.s
        self.install(self.fact(kind='avoid'))
        for who in ('ben', 'both'):
            self.assertEqual(s.veto_abouts(who, NOW), frozenset({'g:Horror'}))
            self.assertEqual(s.veto_keys(who, NOW), frozenset(f'tmdb:{i}' for i in range(3)))
        self.assertEqual(s.veto_keys('ana', NOW), frozenset())
        self.install(self.fact(kind='avoid', strength=.79))
        self.assertEqual(s.veto_keys('both', NOW), frozenset())

    def test_veto_breadth_cap_lapse_and_told_age(self):
        s = self.s
        self.install(self.fact(kind='avoid', about='g:Drama'))
        self.assertEqual(s.veto_keys('ben', NOW), frozenset())
        self.assertEqual(s.memory_view(now=NOW)[0]['veto'], 'too_broad')
        self.install(self.fact(kind='avoid', src='reason', confirmed=NOW-181*s.DAY, seen=NOW-181*s.DAY))
        view = s.memory_view(now=NOW)[0]
        self.assertEqual(view['veto'], 'lapsed')
        self.assertEqual(view['veto_until'], NOW-s.DAY)
        self.assertEqual(s.veto_keys('ben', NOW), frozenset())
        self.install(self.fact(kind='avoid', seen=NOW-400*s.DAY))
        self.assertEqual(len(s.veto_keys('both', NOW)), 3)
        self.assertIsNone(s.memory_view(now=NOW)[0]['veto_until'])

    def test_confirmed_veto_uses_raw_strength(self):
        s = self.s
        self.install(self.fact(kind='avoid', src='reason', strength=.8, confirmed=NOW-179*s.DAY, seen=NOW-179*s.DAY))
        self.assertLess(s.memory_view(now=NOW)[0]['strength'], .8)
        self.assertEqual(len(s.veto_keys('ben', NOW)), 3)

    def test_veto_cache_stays_warm_across_sessions(self):
        s = self.s
        self.install(self.fact(kind='avoid'))
        before = {who: s.veto_keys(who, NOW) for who in (*s.PEOPLE, 'both')}
        with patch.object(s, 'facet_pairs', side_effect=AssertionError('warm veto rescanned facets')):
            for who in (*s.PEOPLE, 'both'):
                self.assertEqual(s.veto_keys(who, NOW), before[who])

    def test_why_text_minimum_and_fallback(self):
        s = self.s
        for fields, text in (({}, 'Because you told me you like Horror'),
                             ({'src': 'implicit'}, 'You like Horror'),
                             ({'src': 'implicit', 'confirmed': NOW}, 'Because you told me you like Horror')):
            self.install(self.fact(**fields))
            why = s.memory_why('tmdb:0', 'movie', 'ben', NOW)
            self.assertEqual(why['why'], text)
            self.assertEqual(why['strength'], 1.)
        self.names['g:Horror'] = 'a'*45
        self.assertEqual(s.memory_why('tmdb:0', 'movie', 'ben', NOW)['why'], 'You like '+'a'*45)
        self.install(self.fact(src='implicit', strength=.78, seen=NOW-180*s.DAY))
        self.assertIsNone(s.memory_why('tmdb:0', 'movie', 'ben', NOW))
        self.install(self.fact(src='implicit', strength=.4))
        self.assertIsNotNone(s.memory_why('tmdb:0', 'movie', 'ben', NOW))
        self.install(self.fact(src='implicit', confirmed=0))
        self.assertTrue(s.memory_why('tmdb:0', 'movie', 'ben', NOW)['told'])

    def test_together_requires_both(self):
        s = self.s
        self.install(self.fact())
        self.assertIsNone(s.memory_why('tmdb:0', 'movie', 'together', NOW))
        self.install(self.fact(), self.fact(2, person='ana'))
        self.assertEqual(s.memory_why('tmdb:0', 'movie', 'together', NOW)['why'], 'You both like Horror')
        self.install(self.fact(), self.fact(2, person='ana', src='implicit'))
        why = s.memory_why('tmdb:0', 'movie', 'together', NOW)
        self.assertEqual((why['fact'], why['told']), ('m1', True))

    def serve(self, cid=None, who='ben', veto=frozenset()):
        s = self.s
        cid = cid or rid('foryou', s.CONFIG)
        metas = [dict(id=f'tmdb:{i}', name=f'Synthetic Card {i}', type='movie', description='overview', _row={'overview': 'overview'}) for i in range(10)]
        with ExitStack() as stack:
            for name, value in {'row_eligible': True, 'row_member': False, 'feed': {}, 'tired_keys': {}, 'get_model': None}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, 'row_list', side_effect=lambda c, *a: metas if c == cid else []))
            for name in ('fill_row', 'person_order', 'balance_row', 'polish_row', 'rotate_row', 'person_animation_sink', 'member_balance'):
                stack.enter_context(patch.object(s, name, side_effect=lambda xs, *a, **kw: xs))
            p = {'entries': [{'key': x['id'], 'kind': 'movie', 'title': x['name'], 'signal': 'watched',
                              'weight': 2, 'item': {}, 'by': ['together']} for x in metas]}
            out = s.served_row(cid, 'tv' if cid in s.MIXED else 'movie', 'de-DE', p, set(), (set(), set(), veto), who)
        self.assertFalse(any('_memory' in x for x in metas))
        return out

    def test_served_cap_strip_and_session_scope(self):
        s = self.s
        self.install(self.fact())
        raw = self.serve()
        self.assertEqual(sum('_memory' in x for x in raw), 2)
        cards = s.human_cards(raw)
        self.assertEqual(sum('Because you told me' in x.get('description', '') for x in cards), 2)
        self.assertFalse(any('_memory' in x or '_row' in x for x in cards))
        self.assertFalse(any('_memory' in x for x in self.serve(who='both')))
        self.install(self.fact(), self.fact(2, person='ana'))
        self.assertTrue(any(x.get('_memory', {}).get('why', '').startswith('You both like') for x in self.serve(who='both')))
        self.assertFalse(any(x.get('_memory', {}).get('why', '').startswith('You both like') for x in self.serve(who='ben')))

    def test_memory_note_bypasses_late_because_drop(self):
        cards = [dict(id='tmdb:0', name='Card', type='movie', description='overview', _row={'overview': 'overview'}) for _ in range(4)]
        cards[-1]['_memory'] = {'why': 'Because you told me you like Horror'}
        self.assertIn('Because you told me', self.s.human_cards(cards)[-1]['description'])

    def test_served_veto_exempt_and_two_tuple_remote(self):
        s = self.s
        veto = frozenset({'tmdb:0'})
        self.assertNotIn('tmdb:0', {x['id'] for x in self.serve(veto=veto)})
        for cid in s.VETO_EXEMPT:
            self.assertIn('tmdb:0', {x['id'] for x in self.serve(cid, veto=veto)})
        self.assertEqual(s.remote_order([{'id': 'tmdb:0'}], (set(), set())), [{'id': 'tmdb:0'}])
        self.assertEqual(len(s.remote_masks()), 2)

    def test_view_fades_and_explain_avoid(self):
        s = self.s
        self.install(self.fact(src='implicit', strength=.6), self.fact(2, kind='avoid', person='ana'))
        view = s.memory_view(now=NOW)
        self.assertEqual(view[0]['fades_in_days'], 360)
        self.assertIsNone(view[1]['fades_in_days'])
        p = s._profiles['household']
        with redirect_stdout(io.StringIO()):
            s._model['m'] = s.build_model(p)
        why = s.explain('tmdb:0', 'movie')['per_person']
        self.assertEqual(why['ben']['memory']['likes'][0]['id'], 'm1')
        self.assertTrue(why['ana']['memory']['vetoed'])
        self.assertEqual(why['ana']['memory']['avoids'][0]['kind'], 'avoid')

    def test_job_flat_refused_no_writes_and_nightly(self):
        s = self.s
        r = {'ops': {'add': 3}, 'active': {'ben': 2, 'ana': 1}, 'ms': 1.5, 'refused': 0}
        with patch.object(s, 'memory_daily', return_value=r):
            out = s.memory_job(NOW)
        self.assertEqual(out['ops'], 3)
        self.assertTrue(all(type(v) in (str, int) for v in out.values()))
        self.assertLessEqual(len(json.dumps(out).encode()), 1024)
        with patch.object(s, 'memory_daily', return_value={**r, 'refused': 1}) as daily:
            self.assertEqual(s.memory_job(NOW)['refused'], 1)
            daily.assert_called_once_with(NOW, dry_run=True)
        self.assertFalse(list(Path(s.DATA).glob('memory*')))
        s._memory.update(stamp='h', at=NOW)
        self.assertTrue(s.memory_due(NOW+s.DAY-1))
        self.assertTrue(s.memory_due(NOW+s.DAY))
        self.assertIn(('memory', s.memory_due, s.memory_job), s.DAILY_JOBS)
        run = Mock(return_value=out)
        s._daily = None
        with patch.object(s, 'DAILY_JOBS', [('memory', s.memory_due, run)]), \
                patch.object(s, 'local_now', side_effect=lambda t=None: s.datetime.datetime.fromtimestamp(NOW if t is None else t, s.datetime.timezone.utc)):
            s.daily_jobs(NOW)
            s._memory['at'] = NOW+18*3600  # a tell less than a day before the next night
            s.daily_jobs(NOW+s.DAY)
            s.daily_jobs(NOW+s.DAY+600)
        self.assertEqual(run.call_count, 2)

    def test_refused_job_preserves_memory_bytes(self):
        s = self.s
        self.install(*(self.fact(i+1, src='implicit', strength=.6, seen=NOW-700*s.DAY) for i in range(5)))
        s._model['m'] = {'paff': {}, 'names': self.names, 'nexp': {}}
        before = {p.name: p.read_bytes() for p in Path(s.DATA).glob('memory*')}
        with patch.object(s, 'events', return_value=iter(())):
            self.assertEqual(s.memory_job(NOW)['refused'], 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(s.DATA).glob('memory*')})


class MemoryPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        cfg = json.loads((self.data / 'legacy.json').read_text())
        cfg['region']['lang'] = 'de-DE'
        (self.data / 'legacy.json').write_text(json.dumps(cfg))
        values = {'profile.json': {'hash': 'h', 'entries': [], 'seen': [], 'negative': []},
                  'facets.json': {'items': {f'tmdb:{i}': {'kind': 'movie', 'title': f'Synthetic Card {i}', 'g': ['Horror'] if i < 3 else ['Drama']} for i in range(10)}},
                  'feeds.json': {f"{rid('foryou', row_config.CONFIG)}|de-DE": {'metas': {'tv': [dict(id=f'tmdb:{i}', name=f'Card {i}', type='movie') for i in range(10)]}}}}
        for name, value in values.items():
            (self.data/name).write_text(json.dumps(value))

    def files(self, data):
        return {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}

    def test_engine_preview_restores_temp_store_and_two_tuples(self):
        with Engine(self.data) as engine:
            s = engine._server
            s.memory_tell('ben', 'like', 'g:Drama')
            engine._refresh()
            before = self.files(engine._work)
            store = copy.deepcopy(s._memory)
            pools = {'rows': [{'id': rid('foryou', s.CONFIG), 'titles': [{'key': f'tmdb:{i}'} for i in range(10)]}]}
            with patch.object(engine, 'rows', return_value=pools):
                result = engine.react([], 'ben', facts=[('ben', 'avoid', 'g:Horror')])
            self.assertFalse(result['writes'])
            self.assertTrue(any(result['vetoed'].values()))
            self.assertEqual(self.files(engine._work), before)
            self.assertEqual(s._memory, store)
            self.assertEqual(s.veto_keys('ben'), frozenset())
            with self.assertRaises(ValueError):
                engine.react([], facts=[('ben', 'like', 'g:Horror'), ('ben', 'like', 'g:Unknown')])
            self.assertEqual(self.files(engine._work), before)

    def test_cli_tell_repeatable_alone_or_with_reaction_read_only(self):
        before = self.files(self.data)
        for args in (['--tell', 'ben:like:g:Horror'],
                     ['like', 'tmdb:1', '--tell', 'ben:like:g:Horror', '--tell', 'ana:avoid:g:Horror']):
            out = io.StringIO()
            code = main(['--snapshot', str(self.data), '--state-dir', str(self.data/'unused'), 'react', *args, '--json'], out=out)
            # State must be outside the snapshot.
            self.assertEqual(code, 2)
            out = io.StringIO()
            code = main(['--snapshot', str(self.data), '--state-dir', str(self.data.parent/'w10b-unused'), 'react', *args, '--json'], out=out)
            self.assertEqual(code, 0, out.getvalue())
            self.assertFalse(json.loads(out.getvalue())['writes'])
            self.assertEqual(self.files(self.data), before)
        for args in ([], ['--tell', 'bad'], ['--tell', 'both:like:g:Horror'], ['like']):
            out = io.StringIO()
            self.assertEqual(main(['--snapshot', str(self.data), 'react', *args, '--json'], out=out), 2)

    def test_mixed_person_facts_show_both_taste_diffs(self):
        with Engine(self.data) as engine:
            before = self.files(engine._work)
            result = engine.react([], facts=[('ben', 'like', 'g:Horror'), ('ana', 'like', 'g:Drama')])
            self.assertEqual({f['person'] for f in result['facets']}, {'ben', 'ana'})
            self.assertEqual(self.files(engine._work), before)
