"""Alignment reads synthetic evidence and offers existing previews only."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM
from skald import commands, members, setup
from skald.__main__ import main
from skald.engine import Engine


class TasteAlign(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'skald.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        self.entries = [self.entry(1, 'Glass Harbor', weight=9., flags=['backfill_only'],
                                   item={'backfilled': True, 'at': None}),
                        self.entry(2, 'Paper Moonlight', weight=1.),
                        self.entry(1, 'Glass Harbor', by=['ben'], signal='disliked', weight=-3.),
                        self.entry(3, 'Quiet Orchard', by=['ben'], weight=2.)]
        self.write_profile()
        (self.data / 'facets.json').write_text(json.dumps({'items': {
            'tmdb:1': {'kind': 'movie', 'title': 'Glass Harbor', 'g': ['Drama'],
                       'k': [[900001, 'Synthetic theme']], 'd': [[900001, 'Synthetic maker']], 'x': {'cozy_case': 3}},
            'tmdb:2': {'kind': 'movie', 'title': 'Paper Moonlight', 'g': ['Comedy']},
            'tmdb:3': {'kind': 'movie', 'title': 'Quiet Orchard', 'g': ['Adventure']}}}))
        net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)
        self.e = Engine(self.data)
        self.addCleanup(self.e.close)
        self.s = self.e._server

    def entry(self, i, title, **changes):
        return dict(key=f'tmdb:{i}', id=i, title=title, year='2020', kind='movie', stype='movie',
                    signal='watched', weight=1., age=2., who='owner', wsrc='trakt', by=['ana'],
                    flags=[], item={'at': 1791734400000}, genres=[]) | changes

    def write_profile(self):
        (self.data / 'profile.json').write_text(json.dumps({'entries': self.entries, 'built_at': 1791734400,
                                                         'seen': [e['key'] for e in self.entries]}))

    def files(self, root):
        return {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob('*') if p.is_file()}

    def test_drivers_shape_and_structural_checks(self):
        data = self.e.align('ana')
        self.assertEqual(set(data), {'person', 'drivers', 'taste', 'check'})
        self.assertEqual(set(data['taste']), {'genres', 'themes', 'makers', 'feelings'})
        driver = data['drivers'][0]
        self.assertEqual(set(driver), {'key', 'kind', 'title', 'weight', 'age_days', 'sources', 'flags', 'rows', 'share'})
        self.assertEqual((driver['key'], driver['weight'], driver['age_days'], driver['share']), ('tmdb:1', 9., 2., .9))
        self.assertEqual(driver['sources'], ['trakt'])
        self.assertEqual(driver['flags'], {'backfill_only': True, 'backfilled': True})
        self.assertNotIn('favorite', driver['flags'])
        checks = {c['signal']: c for c in data['check'] if c['key'] == 'tmdb:1'}
        self.assertTrue({'backfill_only', 'dominant', 'member_conflict'} <= checks.keys())
        fix = checks['backfill_only']['fixes'][0]
        self.assertEqual(fix['op'], {'op': 'ignore_learning', 'kind': 'movie', 'values': ['Glass Harbor'], 'person': 'ana'})
        self.assertNotIn('cli', fix)
        from skald.alignment import text
        self.assertIn(json.dumps(fix['op']), text(data))
        before = self.files(self.e._work)
        plan = commands.preview(self.s, '', viewer='ana', bound='ana', answer=fix['op'])
        self.assertEqual(plan['fix'][0]['op'], 'ignore')
        self.assertEqual(self.files(self.e._work), before)
        json.dumps(data, allow_nan=False)

    def test_facets_reuse_view_and_share_whole_kind(self):
        data = self.e.align('ana', limit=1)
        self.assertEqual(len(data['drivers']), 1)
        for name, types in (('genres', 'g'), ('themes', 'kt'), ('makers', 'dancs'), ('feelings', 'x')):
            facets = data['taste'][name]
            existing = self.e._facets(self.e._model, 'ana', types, 1)
            self.assertEqual([{k: v for k, v in f.items() if k != 'share'} for f in facets], existing)
            self.assertTrue(all(0 <= f['share'] <= 1 for f in facets))
        genre = data['taste']['genres'][0]
        aff = self.e._model['paff']['ana']
        self.assertAlmostEqual(genre['share'], max(0, aff[genre['id']]) / sum(max(0, v) for k, v in aff.items() if k[0] == 'g'), places=4)

    def test_seed_functions_match_without_fetching_candidates(self):
        with patch.object(self.s, 'candidate_pool', side_effect=AssertionError('fetched pool')):
            data = self.e.align('ana', limit=100)
        p = {'entries': self.s.person_entries(self.e._profile, 'ana')}
        seeds = self.s.pool_seeds(p)
        self.assertEqual({(d['key'], d['kind']) for d in data['drivers'] if 'foryou' in d['rows']},
                         {(e['key'], e['kind']) for e in seeds})
        seed = self.s.because_seed(p)
        self.assertEqual([d['key'] for d in data['drivers'] if 'because' in d['rows']], [seed['key']])

    def test_ignore_removes_watch_from_drivers(self):
        (self.e._work / 'title_fixes.json').write_text(json.dumps({'fixes': [
            {'op': 'ignore', 'key': 'tmdb:1', 'who': 'ana', 'kind': 'movie'}]}))
        data = self.e.align('ana')
        self.assertNotIn('tmdb:1', [d['key'] for d in data['drivers']])
        self.assertNotIn('ignored_seeding', [c['signal'] for c in data['check']])

    def test_ignored_person_opinion_does_not_restore_because_weight_with_child(self):
        self.e._profile['entries'] = [self.entry(1, 'Glass Harbor', opinions={
            'ana': {'signal': 'watched', 'weight': 9.}}),
            self.entry(3, 'Quiet Orchard', by=['ben'], weight=2.)]
        (self.e._work / 'title_fixes.json').write_text(json.dumps({'fixes': [
            {'op': 'ignore', 'key': 'tmdb:1', 'who': 'ana', 'kind': 'movie'}]}))
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ben' else None):
            data = self.e.align('ana')
        self.assertEqual(data['drivers'], [])

    def test_household_ignored_seeds_match_row_selectors(self):
        self.e._profile['entries'] = [self.entry(1, 'Glass Harbor', weight=9.)]
        (self.e._work / 'title_fixes.json').write_text(json.dumps({'fixes': [
            {'op': 'ignore', 'key': 'tmdb:1', 'who': 'ana', 'kind': 'movie'}]}))
        data = self.e.align('household')
        p = self.s.adult_profile(self.e._profile)
        for row, seeds in [('top_seeds', self.s.top_seeds(p, 8)),
                           ('foryou', self.s.pool_seeds(p)),
                           ('because', [self.s.because_seed(p)])]:
            with self.subTest(row=row):
                self.assertEqual([(d['key'], d['kind']) for d in data['drivers'] if row in d['rows']],
                                 [(e['key'], e['kind']) for e in seeds])
        self.assertTrue(data['drivers'][0]['flags']['learning_ignored'])
        self.assertEqual(data['drivers'][0]['share'], 0)
        self.assertIn(('tmdb:1', 'ignored_seeding'), [(c['key'], c['signal']) for c in data['check']])

    def test_person_ignore_keeps_another_members_household_watch(self):
        self.e._profile['entries'] = [self.entry(1, 'Glass Harbor', by=['ana', 'ben'], weight=9.)]
        self.e._profile['negative'] = set()
        (self.e._work / 'title_fixes.json').write_text(json.dumps({'fixes': [
            {'op': 'ignore', 'key': 'tmdb:1', 'who': 'ana', 'kind': 'movie'}]}))
        self.assertEqual(self.s.person_entries(self.e._profile, 'ana')[0]['weight'], 0)
        self.assertEqual(self.s.person_entries(self.e._profile, 'ben')[0]['weight'], 9.)
        self.assertEqual(self.s.taste_entries(self.e._profile['entries'])[0]['weight'], 4.5)
        with patch.object(self.s, 'with_art', return_value=([], {})) as fetch:
            self.s.member_feed('en-US', 'ana')
        fetch.assert_not_called()
        data = self.e.align('household')
        self.assertIn('foryou', data['drivers'][0]['rows'])
        self.assertIn('ignored_seeding', [c['signal'] for c in data['check']])

    def test_person_because_preserves_scoped_opinion_and_adult_filter(self):
        self.e._profile['entries'] = [self.entry(1, 'Glass Harbor', by=['ana', 'together'],
            opinions={'ana': {'signal': 'watched', 'weight': 3.}},
            shared={'signal': 'watched', 'weight': 20.})]
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ben' else None), \
                patch.object(self.s, 'top_seeds', wraps=self.s.top_seeds) as choose:
            data = self.e.align('ana')
        self.assertEqual(choose.call_args_list[0].args[0]['entries'][0]['weight'], 3.)
        self.e._profile['entries'][0]['by'] = ['ben']
        self.e._profile['entries'][0].pop('opinions')
        self.e._profile['entries'][0].pop('shared')
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ben' else None):
            data = self.e.align('ben')
        self.assertFalse(any('because' in d['rows'] for d in data['drivers']))

    def test_undated_import_and_shared_only_provenance(self):
        self.e._profile['entries'].extend([
            self.entry(4, 'Blue Lantern', who='import', wsrc='import', age=999, flags=['import', 'plain'], item={}),
            self.entry(5, 'Copper Garden', who='tv', by=['together'], wsrc='tv')])
        data = self.e.align('ana')
        self.assertIn(('tmdb:4', 'undated'), [(c['key'], c['signal']) for c in data['check']])
        self.assertIn(('tmdb:5', 'shared_only'), [(c['key'], c['signal']) for c in data['check']])
        self.assertNotIn('undated', next(d for d in data['drivers'] if d['key'] == 'tmdb:4')['flags'])
        self.e._profile['entries'][-1]['opinions'] = {'ana': {'weight': 3., 'signal': 'loved', 'wsrc': 'remote', 'flags': ['remote'], 'item': {}}}
        data = self.e.align('ana')
        self.assertNotIn(('tmdb:5', 'shared_only'), [(c['key'], c['signal']) for c in data['check']])

    def test_mixed_real_and_backfill_is_not_backfill_only(self):
        entry = self.e._profile['entries'][0]
        entry['flags'] = []
        entry['item']['at'] = 1791734400000
        data = self.e.align('ana')
        self.assertTrue(data['drivers'][0]['flags']['backfilled'])
        self.assertNotIn('backfill_only', [c['signal'] for c in data['check']])

    def test_duplicate_title_share_and_negative_do_not_inflate_total(self):
        self.e._profile['entries'].append(self.entry(1, 'Glass Harbor', weight=1.))
        data = self.e.align('ana')
        self.assertEqual([d['key'] for d in data['drivers']].count('tmdb:1'), 1)
        self.assertAlmostEqual(data['drivers'][0]['share'], 10/11, places=4)

    def test_sources_include_each_positive_contribution(self):
        self.e._profile['entries'].append(self.entry(1, 'Glass Harbor', who='import', wsrc='import', flags=['import', 'plain']))
        data = self.e.align('ana')
        self.assertEqual(data['drivers'][0]['sources'], ['trakt', 'plain'])

    def test_dominant_weight_is_checked_even_outside_seed_rows(self):
        self.e._profile['entries'].append(self.entry(4, 'Amber Window', signal='interest', weight=100.))
        data = self.e.align('ana')
        self.assertNotIn('tmdb:4', [d['key'] for d in data['drivers']])
        check = next(c for c in data['check'] if c['key'] == 'tmdb:4')
        self.assertEqual(check['signal'], 'dominant')
        self.assertEqual(check['fixes'][0]['op']['op'], 'dislike')

    def test_shared_provenance_does_not_use_another_persons_opinion(self):
        shared = self.entry(4, 'Copper Garden', who='tv', by=['together'], wsrc='tv',
            opinions={'ben': {'weight': 3., 'signal': 'loved', 'wsrc': 'remote', 'flags': ['remote'], 'item': {}}})
        self.e._profile['entries'].append(shared)
        data = self.e.align('ana')
        self.assertIn(('tmdb:4', 'shared_only'), [(c['key'], c['signal']) for c in data['check']])
        self.assertNotIn(('tmdb:4', 'shared_only'), [(c['key'], c['signal']) for c in self.e.align('ben')['check']])

    def test_cli_fix_is_omitted_when_title_text_cannot_round_trip(self):
        self.e._profile['entries'][0]['title'] = 'Glass Harbor; Chapter Two'
        data = self.e.align('ana')
        check = next(c for c in data['check'] if c['signal'] == 'backfill_only')
        self.assertNotIn('cli', check['fixes'][0])
        plan = commands.preview(self.s, '', viewer='ana', bound='ana', answer=check['fixes'][0]['op'])
        self.assertEqual(plan['item']['title'], 'Glass Harbor; Chapter Two')

    def test_local_cli_is_read_only_and_uses_current_data(self):
        before = self.files(self.data)
        env = {k: v for k, v in os.environ.items() if not k.startswith('SKALD_')}
        env.update(DATA_DIR=str(self.data))
        with patch.dict(os.environ, env, clear=True):
            out = io.StringIO()
            self.assertEqual(main(['--state-dir', str(self.root/'state'), 'taste', 'align', '--person', 'ana'], out), 0)
        self.assertIn('Drivers', out.getvalue())
        self.assertIn('Glass Harbor', out.getvalue())
        self.assertEqual(self.files(self.data), before)
        self.assertFalse((self.root/'state').exists())

    def test_for_you_seed_helper_keeps_shared_account_extras(self):
        p = {'entries': [self.entry(i, f'Synthetic driver {chr(64+i)}', weight=30-i) for i in range(1, 19)] +
                       [self.entry(19, 'Shared Lantern', who='tv', by=['together'], weight=.1)], 'seen': set(), 'negative': set()}
        ids = [e['id'] for e in self.s.pool_seeds(p)]
        self.assertEqual(len(ids), self.s.POOL_SEEDS)
        self.assertIn(19, ids)
        with patch.object(self.s, 'get_model', return_value=self.e._model), \
                patch.object(self.s, 'pool_seeds', wraps=self.s.pool_seeds) as choose, \
                patch.object(self.s, 'tmdb', return_value={'results': []}):
            self.s.candidate_pool(p)
            choose.assert_called_once_with(p, None)

    def test_read_only_engine_and_cli(self):
        original, work = self.files(self.data), self.files(self.e._work)
        self.e.align('ana')
        self.assertEqual(self.files(self.e._work), work)
        self.assertEqual(self.files(self.data), original)
        env = {k: v for k, v in os.environ.items() if not k.startswith('SKALD_')}
        env.update(DATA_DIR=str(self.data))
        with patch.dict(os.environ, env, clear=True):
            out = io.StringIO()
            code = main(['--snapshot', str(self.data), '--state-dir', str(self.root/'state'),
                         'taste', 'align', '--person', 'ana', '--limit', '1', '--json'], out)
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(len(data['drivers']), 1)
            self.assertEqual(data['person'], 'ana')
        self.assertEqual(self.files(self.data), original)
        self.assertFalse((self.root/'state').exists())

    def test_limit_and_unknown_person_validation(self):
        for n in (0, -1, 101, True):
            with self.subTest(limit=n), self.assertRaises(ValueError):
                self.e.align('ana', n)
        with self.assertRaises(ValueError):
            self.e.align('unknown')
        self.assertIsNone(self.e.align('household')['person'])

    def test_commander_fake_model_routes_and_localizes(self):
        cases = [('what shapes my picks?', 'en', 'Drivers'),
                 ('warum empfiehlst du mir so viele Krimis?', 'de', 'Einflüsse'),
                 ('zašto mi stalno nudiš anime?', 'sr-latn', 'Uticaji')]
        for text, language, heading in cases:
            with self.subTest(text=text), patch.object(self.s, 'openrouter', self.e._network_openrouter), ReplayLLM(self.s) as fake:
                answer = dict(op='taste_drivers', kind=None, values=[], person=None, language=language)
                fake.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
                plan = commands.preview(self.s, text, viewer='ana', bound='ana')
                self.assertEqual(fake.calls, 1)
                self.assertIn('taste_drivers', fake.bodies[0]['messages'][0]['content'])
                self.assertTrue(plan['read_only'])
                self.assertEqual(plan['people'], ['ana'])
                self.assertIn(heading, plan['description'])
                self.assertIn('Glass Harbor', plan['description'])
                self.assertNotIn('Quiet Orchard', plan['description'])
                self.assertEqual(plan['alignment'], self.e.align('ana'))
                self.assertNotIn('plan', plan)

    def test_commander_read_permissions_and_files(self):
        answer = dict(op='taste_drivers', kind=None, values=[], person='ben', language='en')
        for bound in ('ana', None):
            with self.subTest(bound=bound), self.assertRaises(PermissionError):
                commands.preview(self.s, 'synthetic read', viewer='ana', bound=bound, answer=answer)
        answer['person'] = None
        before = self.files(self.e._work)
        commands.preview(self.s, 'synthetic read', viewer='ana', bound='ana', answer=answer)
        self.assertEqual(self.files(self.e._work), before)
        self.assertEqual(commands.REGISTRY['taste_drivers']['group'], 'read')
        self.assertTrue(commands.REGISTRY['taste_drivers']['kids'])
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ana' else None):
            plan = commands.preview(self.s, 'synthetic read', viewer='ana', bound='ana', answer=answer)
            self.assertTrue(plan['read_only'])
            with self.assertRaises(PermissionError):
                commands.preview(self.s, 'synthetic read', viewer='ana', bound='ana', answer=answer | {'person': 'together'})
        house = commands.preview(self.s, 'synthetic read', viewer='ana', bound='ana', answer=answer | {'person': 'together'})
        self.assertIn('Quiet Orchard', json.dumps(house['alignment']))

    def test_commander_without_cached_profile_reads_saved_data(self):
        before = self.files(self.data)
        answer = dict(op='taste_drivers', kind=None, values=[], person=None, language='en')
        with setup.server(self.data) as s:
            plan = commands.preview(s, 'synthetic read', viewer='ana', bound='ana', answer=answer)
        self.assertEqual(plan['alignment'], self.e.align('ana'))
        self.assertEqual(self.files(self.data), before)

    def test_live_server_without_profile_does_not_load_a_second_engine(self):
        answer = dict(op='taste_drivers', kind=None, values=[], person=None, language='en')
        with setup.server(self.data) as s, patch('skald.engine.Engine', side_effect=AssertionError('second engine')):
            s._CONFIG_READONLY = False
            s._profiles.pop('household', None)
            with self.assertRaises(ValueError):
                commands.preview(s, 'synthetic read', viewer='ana', bound='ana', answer=answer)

    def test_kids_api_uses_model_read_permission(self):
        req = dict(viewer='ana', client={'name': 'synthetic'}, body={'in': {'query': 'what shapes my picks?'}})
        answer = dict(op='taste_drivers', kind=None, values=[], person=None, language='en')
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ana' else None), \
                patch.object(self.s, 'openrouter', self.e._network_openrouter), ReplayLLM(self.s) as fake:
            fake.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            result = commands.api_preview(self.s, req)
        self.assertTrue(result['preview']['read_only'])
        self.assertNotIn('api:synthetic', commands.pending(self.s))

    def test_household_drivers_use_existing_adult_profile(self):
        with patch.object(self.s, 'kids_age', side_effect=lambda p: 10 if p == 'ana' else None):
            data = self.e.align('household')
            p = self.s.adult_profile(self.e._profile)
            expected = {(e['key'], e['kind']) for e in self.s.pool_seeds(p)}
        actual = {(d['key'], d['kind']) for d in data['drivers'] if 'foryou' in d['rows']}
        self.assertEqual(actual, expected)
        self.assertNotIn('tmdb:2', [d['key'] for d in data['drivers']])

    def test_favorite_and_infinite_age_json(self):
        self.e._profile['entries'][0]['item']['favorite'] = True
        self.e._profile['entries'][0]['age'] = float('inf')
        data = self.e.align('ana')
        driver = next(d for d in data['drivers'] if d['key'] == 'tmdb:1')
        self.assertTrue(driver['flags']['favorite'])
        self.assertIsNone(driver['age_days'])
        json.dumps(data, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
