"""W25b arithmetic and member isolation, using synthetic evidence only."""
from array import array
from contextlib import redirect_stdout
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval.profile_golden import NOW, ROOT, offline_server
from skald.members import fresh_config


def entry(i, person='eva', weight=3., **kw):
    return dict(key=f'tmdb:{i}', id=i, kind='movie', stype='movie', title=f'Fable {i}',
                year='2020', signal='loved' if weight > 0 else 'disliked', weight=weight,
                age=0., who='remote', by=[person], flags=[], genres=[], item={}, **kw)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        (data/'skald.json').write_text(json.dumps(fresh_config(['eva', 'oskar'])))
        ctx = offline_server(ROOT/'server.py', data)
        self.s = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.s._facets = {f'tmdb:{i}': {'kind': 'movie', 'title': f'Fable {i}'} for i in range(1, 201)}
        self.s._emb.clear()

    def model(self, entries):
        profile = dict(entries=entries, seen={e['key'] for e in entries}, negative=set())
        self.s._profiles['household'] = profile
        with redirect_stdout(io.StringIO()):
            return self.s.build_model(profile, enrich=False)

    def test_negative_only_facet_survives_strict_support(self):
        s = self.s
        for i in (41, 42, 43, 44):
            s._facets[f'tmdb:{i}']['g'] = ['Horror']
        m = self.model([entry(i) for i in range(1, 41)] + [entry(41, weight=-3.)] +
                       [entry(i, 'oskar') for i in (42, 43)])
        self.assertLess(m['paff']['eva']['g:Horror'], 0)
        self.assertLess(s.facet_bonus(m, s._facets['tmdb:44'], m['paff']['eva'])[0], 0)
        self.assertGreater(m['paff']['oskar']['g:Horror'], 0)
        for p, n, count in ((0, 3, 0), (2, 3, 1), (6, 3, 3), (3, 6, 3)):
            args = ('g:Horror', p, n, count, 40, 4, 100, 201)
            base = s.facet_value(*args)
            actual = s.facet_value(*args, person=True)
            expected = base * min(1, count/6) if base > 0 else base
            self.assertAlmostEqual(actual, expected)

    def test_unrelated_fortieth_positive_does_not_create_tone_reversal(self):
        s = self.s
        for i, rec in enumerate(s._facets.values(), 1):
            rec['x'] = {'feel_good': 3} if i <= 160 else {'dread': 3}
        entries = [entry(i, weight=.07 if i <= 4 else 3.) for i in range(1, 51)]
        for i in range(5, 51):
            s._facets[f'tmdb:{i}']['x'] = {'dread': 3}
        oskar = [entry(i, 'oskar') for i in range(101, 121)]
        values = {}
        for n in (20, 30, 39, 40, 41, 50):
            m = self.model(entries[:n] + oskar)
            values[n] = m['paff']['eva']['x:feel_good']
            self.assertGreater(m['paff']['oskar']['x:feel_good'], 0)
        self.assertEqual(values[20], 0)
        self.assertEqual(values[30], 0)
        self.assertLess(abs(values[40]-values[39]), .1)
        self.assertLess(abs(values[41]-values[40]), .1)
        for n in (39, 40, 41):
            mine = s.person_entries({'entries': entries[:n]}, 'eva')
            df = {'x:feel_good': sum('feel_good' in r['x'] for r in s._facets.values()),
                  'x:dread': sum('dread' in r['x'] for r in s._facets.values())}
            loose = s.affinities(mine, s._facets, df, 201, set(), loose=True, person=True)[0]['x:feel_good']
            strict = s.affinities(mine, s._facets, df, 201, set(), person=True)[0]['x:feel_good']
            blend = (n-30)/20
            self.assertAlmostEqual(values[n], (1-blend)*loose+blend*strict)

    def test_together_is_pareto_monotone_for_one_to_six_adults(self):
        s = self.s
        rec = s._facets['tmdb:200']
        rec['d'] = [[1, 'Fable director']]
        m = dict(vec=None, aff={}, names={'d:1': 'Fable director'}, bscale=1., vbase=0.,
                 paff={}, pvec={}, pbscale={}, nexp={})
        for n in range(1, 7):
            people = ('eva', 'oskar', 'ida', 'niko', 'lea', 'tom')[:n]
            m['paff'] = {p: {'d:1': i*.1} for i, p in enumerate(people)}
            for counts in ((40,)*n, tuple(i*8 for i in range(n))):
                m['nexp'] = dict(zip(people, counts))
                with patch.object(s, 'adult_people', return_value=people):
                    before = s.score_key(m, 'tmdb:200', 'together')[0]
                    house = s.score_key(m, 'tmdb:200')[0]
                    values = [min(1, m['nexp'][p]/40)*s.score_key(m, 'tmdb:200', p)[0] +
                              (1-min(1, m['nexp'][p]/40))*house for p in people]
                    self.assertEqual(before, sum(values)/n-min(.25, 1/n)*(max(values)-min(values)))
                    for p in people:
                        m['paff'][p]['d:1'] += .2
                        self.assertGreaterEqual(s.score_key(m, 'tmdb:200', 'together')[0]+1e-15, before)
                        m['paff'][p]['d:1'] -= .2
                    for order in itertools.permutations(people):
                        with patch.object(s, 'adult_people', return_value=order):
                            self.assertAlmostEqual(s.score_key(m, 'tmdb:200', 'together')[0], before)

    def test_oskar_account_connection_does_not_switch_eva_fallback(self):
        s = self.s
        for i in (1, 2, 3, 200):
            s._facets[f'tmdb:{i}']['d'] = [[1, 'Fable director']]
        for eva in ([], [entry(4, weight=0.)], [entry(4)], [entry(200, weight=-3.)]):
            s.CONFIG['connectors'] = {}
            m = self.model([entry(i, 'oskar') for i in (1, 2, 3)] + eva)
            before = {p: s.person_score(m, 'tmdb:200', p, 'movie')[0] for p in ('eva', 'together')}
            s.CONFIG['connectors']['stremio'] = {'accounts': {'oskar': {}}}
            m = self.model([entry(i, 'oskar') for i in (1, 2, 3)] + eva)
            for p in before:
                self.assertEqual(s.person_score(m, 'tmdb:200', p, 'movie')[0], before[p])
            if not eva or eva[0]['weight'] == 0:
                self.assertEqual(s.person_score(m, 'tmdb:200', 'eva')[0], 0)
            elif eva[0]['weight'] < 0:
                self.assertEqual(s.person_score(m, 'tmdb:200', 'eva')[0], s.score_key(m, 'tmdb:200', 'eva')[0])

    def test_strong_fit_requires_eligible_personal_holders(self):
        s = self.s
        for i in (1, 2, 3, 4, 200):
            s._facets[f'tmdb:{i}']['x'] = {'found_family': 3}
        for accounts in ({}, {'stremio': {'accounts': {'oskar': {}}}}):
            s.CONFIG['connectors'] = accounts
            for own in ([], [entry(3), entry(4)],
                        [entry(3, abandon='related'), entry(4, abandon='related')]):
                m = self.model([entry(1, 'oskar'), entry(2, 'oskar')] + own)
                m.update(cal=20, bg=[0.]*20, pbg={p: [0.]*20 for p in (*s.PEOPLE, 'together')},
                         nexp={p: 40 for p in s.PEOPLE})
                label, pct = s.confidence(m, 'tmdb:200', 1., ['x:found_family'], 'eva')
                strong = bool(own and not own[0].get('abandon'))
                self.assertEqual(label, s.t('match.strong') if strong else s.t('match.good'))
                self.assertEqual(pct, 99)
                self.assertEqual(bool(s.anchor(m, 'tmdb:200', 'x:found_family', person='eva', kind='movie')), strong)
                together = s.confidence(m, 'tmdb:200', 1., ['x:found_family'], 'together')[0]
                self.assertEqual(together, s.t('match.shared') if strong else s.t('match.one'))

    def test_neutral_stops_cannot_propose_avoid_memory(self):
        from eval.memory_report import supports
        s = self.s
        for i in (1, 2, 3):
            s._facets[f'tmdb:{i}'].update(x={'dread': 3})
        m = {'paff': {p: {'x:dread': -1.} for p in s.PEOPLE}, 'names': {'x:dread': 'dread'}}
        evts = [s._event(NOW-age*s.DAY, f'stop:{i}', f'tmdb:{i}', 'movie', f'Fable {i}', '2020',
                         'eva', list(s.PEOPLE), 'abandon', 'import:imdb', {})
                for i, age in ((1, 14), (2, 7), (3, 0))]
        for reading in ('early', 'unsure', 'related', 'pending'):
            entries = [entry(i, weight=0., abandon=reading) for i in (1, 2, 3)]
            for e in entries:
                e['by'] = list(s.PEOPLE)
                e['opinions'] = {'eva': {'signal': 'abandoned', 'weight': 0., 'abandon': reading},
                                 'oskar': {'signal': 'disliked', 'weight': -3.}}
            s._profiles['household'] = {'entries': entries}
            result = s.curate(iter(evts), m, NOW, store=s._memory_empty(), dry_run=True)
            self.assertEqual([f['person'] for f in result['store']['facts']], ['oskar'])
            support = supports(s, evts, m)
            self.assertEqual(support['eva']['x:dread']['keys'], set())
            self.assertEqual(support['oskar']['x:dread']['keys'], {'tmdb:1', 'tmdb:2', 'tmdb:3'})
        reasons = [{**ev, 'id': f'reason:{i}', 'by': ['oskar'], 'action': 'not_for_us',
                    'src': 'remote', 'reasons': ['too slow']} for i, ev in enumerate(evts)]
        result = s.curate(iter(reasons), m, NOW, store=s._memory_empty(), dry_run=True)
        self.assertEqual([(f['person'], f['src'], f['status']) for f in result['store']['facts']],
                         [('oskar', 'reason', 'active')])

    def test_neutral_stops_stop_supporting_unconfirmed_memory(self):
        s = self.s
        s._facets['tmdb:1']['x'] = {'dread': 3}
        m = {'paff': {'eva': {'x:dread': -1.}, 'oskar': {}}, 'names': {'x:dread': 'dread'}}
        s._profiles['household'] = {'entries': [entry(1, weight=0., abandon='early')]}
        ev = s._event(NOW, 'stop:1', 'tmdb:1', 'movie', 'Fable 1', '2020', 'eva', ['eva'],
                      'abandon', 'import:imdb', {})
        for status, src, confirmed in (('proposed', 'implicit', None), ('active', 'implicit', None),
                                       ('active', 'told', NOW), ('active', 'implicit', NOW)):
            store = s._memory_empty()
            fact = s._memory_new(store, 'eva', 'avoid', 'x:dread', NOW, m['names'], src=src,
                                 via='curate', n=3, strength=.6, status=status, confirmed=confirmed,
                                 evidence=['stop:1'], seen=NOW)
            result = s.curate(iter([ev]), m, NOW, store=store, dry_run=True)
            actual = result['store']['facts'][0]
            self.assertEqual(actual['status'], status if src == 'told' or confirmed else 'superseded')

    def test_neutral_stop_cleanup_can_retract_several_unconfirmed_facts(self):
        s = self.s
        names = {f'x:{tag}': tag for tag in ('dread', 'dark', 'slow')}
        m = {'paff': {'eva': dict.fromkeys(names, -1.), 'oskar': {}}, 'names': names}
        s._facets['tmdb:1']['x'] = dict.fromkeys(('dread', 'dark', 'slow'), 3)
        s._profiles['household'] = {'entries': [entry(1, weight=0., abandon='early')]}
        ev = s._event(NOW, 'stop:1', 'tmdb:1', 'movie', 'Fable 1', '2020', 'eva', ['eva'],
                      'abandon', 'import:imdb', {})
        store = s._memory_empty()
        for fid in names:
            s._memory_new(store, 'eva', 'avoid', fid, NOW, names, src='implicit', via='curate',
                          n=3, strength=.6, status='active', evidence=['stop:1'], seen=NOW)
            store['seq'] += 1
        result = s.curate(iter([ev]), m, NOW, store=store, dry_run=True)
        self.assertEqual(result['refused'], [])
        self.assertTrue(all(f['status'] == 'superseded' for f in result['store']['facts']))

    def test_moved_dislikes_cannot_support_the_previous_members_memory(self):
        from eval.memory_report import supports
        s = self.s
        entries = [entry(i, weight=-3.) for i in (1, 2)]
        for e in entries:
            s._facets[e['key']]['x'] = {'dread': 3}
        evts = [s._event(NOW, f'dislike:{i}', e['key'], 'movie', e['title'], '2020',
                         'eva', ['eva'], 'not_for_us', 'remote', {}, reasons=['too slow'])
                for i, e in enumerate(entries)]
        m = self.model(entries)
        before = s.curate(iter(evts), m, NOW, store=s._memory_empty(), dry_run=True)
        self.assertEqual([(f['person'], f['status']) for f in before['store']['facts']], [('eva', 'active')])
        fixes = [{'op': 'move', 'key': e['key'], 'who': 'eva', 'to': 'oskar', 't': NOW} for e in entries]
        Path(s.DATA, 'title_fixes.json').write_text(json.dumps({'fixes': fixes}))
        m = self.model(entries)
        self.assertEqual(s.person_entries(s._profiles['household'], 'eva'), [])
        self.assertEqual(len(s.person_entries(s._profiles['household'], 'oskar')), 2)
        self.assertEqual(supports(s, evts, m)['oskar']['x:dread']['keys'], {'tmdb:1', 'tmdb:2'})
        stores = [s._memory_empty()]
        for status in ('proposed', 'active'):
            store = s._memory_copy(before['store'])
            store['facts'][0]['status'] = status
            stores.append(store)
        for store in stores:
            with self.subTest(previous_status=store['facts'][0]['status'] if store['facts'] else None):
                result = s.curate(iter(evts), m, NOW, store=store, dry_run=True)
                self.assertEqual(result['refused'], [])
                self.assertEqual([(f['person'], f['status']) for f in result['store']['facts'] if s._memory_live(f)],
                                 [('oskar', 'active')])

    def test_strong_fit_rejects_keyword_holders_that_cannot_anchor(self):
        s = self.s
        for i in (1, 2, 200):
            s._facets[f'tmdb:{i}']['k'] = [[7, 'Lantern craft']]
        m = self.model([entry(1), entry(2)])
        m.update(cal=20, bg=[0.]*20, pbg={'eva': [0.]*20}, nexp={'eva': 40, 'oskar': 0})
        s._emb['tmdb:200'] = array('f', [1., 0.])
        for i in (1, 2):
            s._emb[f'tmdb:{i}'] = array('f', [0., 1.])
        self.assertIsNone(s.anchor(m, 'tmdb:200', 'k:7', person='eva', kind='movie'))
        self.assertEqual(s.reason_details(m, 'tmdb:200', ['k:7'], person='eva', kind='movie'), [])
        self.assertEqual(s.confidence(m, 'tmdb:200', 1., ['k:7'], 'eva'), (s.t('match.good'), 99))
        for i in (1, 2):
            s._emb[f'tmdb:{i}'] = array('f', [1., 0.])
        self.assertIsNotNone(s.anchor(m, 'tmdb:200', 'k:7', person='eva', kind='movie'))
        self.assertEqual(s.confidence(m, 'tmdb:200', 1., ['k:7'], 'eva'), (s.t('match.strong'), 99))

    def test_round_trip_move_revalidates_intermediate_owners_memory(self):
        s = self.s
        entries = [entry(i, weight=-3.) for i in (1, 2)]
        for e in entries:
            s._facets[e['key']]['x'] = {'dread': 3}
        evts = [s._event(NOW, f'round:{i}', e['key'], 'movie', e['title'], '2020',
                         'eva', ['eva'], 'not_for_us', 'remote', {}, reasons=['too slow'])
                for i, e in enumerate(entries)]
        fixes = [{'op': 'move', 'key': e['key'], 'who': 'eva', 'to': 'oskar', 't': NOW} for e in entries]
        path = Path(s.DATA, 'title_fixes.json')
        path.write_text(json.dumps({'fixes': fixes}))
        middle = s.curate(iter(evts), self.model(entries), NOW, store=s._memory_empty(), dry_run=True)
        self.assertEqual([(f['person'], f['status']) for f in middle['store']['facts']], [('oskar', 'active')])
        fixes += [{'op': 'move', 'key': e['key'], 'who': 'oskar', 'to': 'eva', 't': NOW+1} for e in entries]
        path.write_text(json.dumps({'fixes': fixes}))
        m = self.model(entries)
        self.assertEqual(s.person_entries(s._profiles['household'], 'oskar'), [])
        for status, src, confirmed in (('proposed', 'reason', None), ('active', 'reason', None),
                                       ('active', 'told', NOW), ('active', 'reason', NOW)):
            with self.subTest(status=status, src=src, confirmed=confirmed):
                store = s._memory_copy(middle['store'])
                store['facts'][0].update(status=status, src=src, confirmed=confirmed)
                result = s.curate(iter(evts), m, NOW+1, store=store, dry_run=True)
                self.assertEqual(result['refused'], [])
                people = sorted(f['person'] for f in result['store']['facts'] if s._memory_live(f))
                self.assertEqual(people, ['eva', 'oskar'] if src == 'told' or confirmed else ['eva'])

    def test_title_view_confidence_uses_series_embedding_on_id_collision(self):
        s = self.s
        for i in (1, 2, 200):
            s._facets[f'tmdb:{i}']['k'] = [[7, 'Lantern craft']]
        s._facets['tmdb:200@tv'] = {'kind': 'tv', 'title': 'Lantern Harbor', 'k': [[7, 'Lantern craft']]}
        m = self.model([entry(1), entry(2)])
        m.update(cal=20, bg=[0.]*20, pbg={p: [0.]*20 for p in (*s.PEOPLE, 'together')},
                 nexp={'eva': 40, 'oskar': 0})
        for key in ('tmdb:1', 'tmdb:2', 'tmdb:200'):
            s._emb[key] = array('f', [1., 0.])
        s._emb['tmdb:200@tv'] = array('f', [0., 1.])
        with patch.object(s, 'get_model', return_value=m):
            series = s.title_view('tmdb:200', 'tv', 'eva')
            self.assertEqual(series['reasons'], [])
            self.assertEqual((series['confidence'], series['match']), (s.t('match.good'), 99))
            movie = s.title_view('tmdb:200', 'movie', 'eva')
            self.assertTrue(movie['reasons'])
            self.assertEqual((movie['confidence'], movie['match']), (s.t('match.strong'), 99))
        for person in (None, 'eva', 'together'):
            self.assertEqual(s.confidence(m, 'tmdb:200', 1., ['k:7'], person, kind='tv'), (s.t('match.good'), 99))


if __name__ == '__main__':
    unittest.main()
