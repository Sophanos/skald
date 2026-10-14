"""Catalog turns survive Home cuts, using invented titles and fake transports."""
from collections import Counter
import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald import members, reach, rowplan
from test_w111_pool import catalog, model


class ServedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        config = members.fresh_config(['ana', 'ben'])
        config['household']['member_rows'] = ['ana', 'ben']
        config['household']['rows'] = ['people']
        Path(self.temp, 'skald.json').write_text(json.dumps(config))
        self.s = load_server(self.temp)
        self.addCleanup(close_server, self.s)
        self.p = {'entries': [{'key': f'tmdb:{i}', 'id': i, 'kind': 'movie', 'stype': 'movie',
                              'title': f'Invented anchor {chr(64+i)}', 'weight': 2, 'age': 0,
                              'signal': 'loved', 'explicit': True, 'item': {}, 'by': ['ana', 'ben']} for i in range(1, 4)],
                  'seen': {'tmdb:1', 'tmdb:2', 'tmdb:3'}, 'negative': set(), 'searches': [], 'likes': []}
        self.m = model(12)
        self.m['holders'] = {'x:mind_bender': [(2, 'tmdb:1')]}
        self.s._profiles['household'] = self.p
        self.s._facets.update(catalog(12))
        self.catalog = set(self.s._facets)
        for rec in self.s._facets.values():
            rec.pop('backdrop_path')
            rec['x'] = {'mind_bender': 3}
        self.recs = [{'id': 900+i, 'title': 'Invented source '+chr(65+i//26)+chr(65+i%26),
                      'release_date': '2020-01-01', 'genre_ids': [18], 'vote_count': 500,
                      'vote_average': 8, 'poster_path': '/fake.jpg', 'backdrop_path': '/fake-wide.jpg'} for i in range(30)]
        for it in self.recs:
            self.s._facets[f'tmdb:{it["id"]}'] = {'kind': 'movie', 'g': ['Drama'], 'x': {'mind_bender': 2}}
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'get_model', return_value=self.m))
        self.enterContext(patch.object(self.s, 'with_art', return_value=(self.recs, {})))
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=AssertionError('unexpected TMDB')))
        self.enterContext(patch.object(self.s, 'openrouter', side_effect=AssertionError('unexpected LLM')))
        self.enterContext(patch.object(self.s, 'genres', return_value={18: 'Drama'}))
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            self.enterContext(patch.object(self.s, name))
        self.enterContext(patch.object(self.s, 'tired_keys', return_value={}))
        self.enterContext(patch.object(self.s, 'archetype_view', return_value={}))
        self.enterContext(patch.object(self.s, 'seed_similarity', side_effect=self.similarity))
        self.enterContext(patch.object(self.s, 'rank_by_taste', side_effect=self.rank))

    def similarity(self, seed, key):
        return .9 if int(seed[5:]) == int(key[5:]) % 3 + 1 else .1

    def rank(self, m, items, **kwargs):
        return [(self.s.score_key(m, key, kind=kind)[0], [], key, kind, meta) for key, kind, meta in items]

    def put(self, key, feed, **names):
        cid = self.s.rid(key)
        self.s._feeds[cid, self.s.LANG] = {'metas': feed[cid], 'names': names}
        return cid

    def home(self, key, person='ana'):
        row = next(r for r in rowplan.plan(self.s, person, cards=True)['rows'] if r['id'] == self.s.rid(key))
        self.assertIsNotNone(row['position'], row)
        return [x['key'] for x in row['cards'] if x['removed_by'] is None][:20]

    def assert_turns(self, ids):
        self.assertGreaterEqual(len(set(ids) & self.catalog), 5, ids)
        self.assertGreaterEqual(sum((a in self.catalog) != (b in self.catalog) for a, b in zip(ids, ids[1:])), 12, ids)
        self.assertEqual(len(ids), len(set(ids)))

    def assert_balance(self, ids):
        self.assertTrue(all(n <= 4 for n in Counter(int(k[5:]) % 3 for k in ids[:10]).values()))
        bleak = lambda key: 'dread' in self.s.frec(key, 'movie' if key not in self.catalog or int(key[5:]) % 2 else 'tv').get('x', {})
        self.assertLessEqual(sum(map(bleak, ids[:10])), 1)
        self.assertLessEqual(sum(map(bleak, ids[:20])), 5)

    def test_member_catalog_turns_survive_build_serving_and_reach(self):
        s = self.s
        for person in s.PEOPLE:
            with self.subTest(person=person):
                self.assertEqual(len(s.catalog_picks(self.p, self.m, s.rid(person), person)), 12)
                feed = s.member_feed(s.LANG, person)
                stored = feed[s.rid(person)]['tv']
                self.assertEqual(len(stored), 40)
                cid = self.put(person, feed)
                self.assert_turns([x['id'] for x in stored[:20]])
                served = s.served_row(cid, 'tv', s.LANG, self.p, set(), (set(), set()), who=person)
                self.assert_balance([x['id'] for x in served])
                ids = self.home(person, person)
                self.assert_turns(ids)
                report = reach.view(s, person)['people'][person]
                self.assertEqual(report['hits'], len(set(ids) & self.catalog))
                self.assertGreaterEqual(report['hits'], 5)
                self.assertTrue(all(s.rid(person) in x['rows'] for x in report['titles'] if x['key'] in ids))

    def test_member_catalog_turns_keep_bleak_seed_and_filter_limits(self):
        s = self.s
        for i in (100, 101, 102, 103, 104, 105, 106, 107):
            s._facets[f'tmdb:{i}']['x'] = {'dread': 3, 'mind_bender': 2}
        self.put('ana', s.member_feed(s.LANG, 'ana'))
        self.p['seen'].add('tmdb:108')
        self.p['negative'].add('tmdb:109')
        for card in s._feeds[s.rid('ana'), s.LANG]['metas']['tv']:
            if card['id'] == 'tmdb:110':
                card['_row']['rating'] = 3
        s._facets['tmdb:111']['g'] = ['Family']
        ids = self.home('ana')
        self.assert_balance(ids)
        self.assertFalse(set(ids) & {f'tmdb:{i}' for i in range(108, 112)})
        self.assertGreaterEqual(len(set(ids) & self.catalog), 5, ids)

    def test_two_seeds_keep_bleak_limits_when_balanced_front_is_short(self):
        s = self.s
        self.p['entries'] = self.p['entries'][:2]
        own = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:24]]
        more = []
        for key in sorted(self.catalog):
            s._facets[key]['x'] = {'dread': 3, 'mind_bender': 2}
            meta = s.known_meta(key, s._facets[key]['kind'])
            meta['_row']['catalog'] = True
            more.append(meta)
        with patch.object(s, 'seed_similarity', side_effect=lambda seed, key: .9 if
                int(seed[5:]) == int(key[5:]) % 2 + 1 else .1):
            polished = s.polish_row(s.interleave(more, own), catalog=True)
            for strict in (False, True):
                with self.subTest(strict=strict):
                    balanced = s.member_balance(polished, 'ana', strict=strict)
                    served = s.rotate_row(balanced, s.rid('ana'))[:20]
                    self.assertEqual(served[:5], polished[:5])
                    self.assertLessEqual(sum(x['id'] in self.catalog for x in served[:10]), 1)
                    self.assertEqual(sum(x['id'] in self.catalog for x in served), 5)
                    self.assertEqual(len(served), len({x['id'] for x in served}))
                    if not strict:
                        self.assertCountEqual(balanced, polished)
            for card in more:
                card['_row'].pop('catalog')
                card['background'] = 'fake-wide'
            legacy = s.member_balance(s.polish_row(s.interleave(more, own), catalog=True), 'ana', strict=True)
            self.assertEqual([x['id'] for x in legacy[:10]], [f'tmdb:{i}' for i in
                             (100, 900, 901, 902, 903, 904, 905, 907, 101, 102)])

    def test_sparse_member_catalog_keeps_deferred_bleak_cards_out_of_first_twenty(self):
        s = self.s
        own = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:5]]
        more = []
        for key in sorted(self.catalog):
            s._facets[key]['x'] = {'dread': 3, 'mind_bender': 2}
            meta = s.known_meta(key, s._facets[key]['kind'])
            meta['_row']['catalog'] = True
            more.append(meta)
        polished = s.polish_row(s.interleave(more, own), catalog=True)
        for strict in (False, True):
            with self.subTest(strict=strict):
                balanced = s.member_balance(polished, 'ana', strict=strict)
                self.assertLessEqual(sum(x['id'] in self.catalog for x in balanced[:10]), 1)
                self.assertLessEqual(sum(x['id'] in self.catalog for x in balanced[:20]), 5)
                self.assertEqual(len(balanced), 6)
                self.assertEqual(balanced[:5], own)

    def test_member_catalog_with_backgrounds_still_takes_turns_after_ranking(self):
        for key in self.catalog:
            self.s._facets[key]['backdrop_path'] = '/fake-wide.jpg'
        self.put('ana', self.s.member_feed(self.s.LANG, 'ana'))
        ids = self.home('ana')
        self.assert_turns(ids)
        self.assert_balance(ids)

    def test_foryou_catalog_turns_survive_build_and_home(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou']
        cards = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs]
        with patch.object(s, 'candidate_pool', side_effect=lambda *a, **kw: cards if kw.get('metas') else []), \
                patch.object(s, 'llm_sections', return_value={}), patch.object(s, 'profile_text', return_value='Invented taste'):
            feed = s.personal_feed(s.LANG)
        self.put('foryou', feed)
        self.assert_turns([x['id'] for x in feed[s.rid('foryou')]['tv'][:20]])
        self.assert_turns(self.home('foryou'))
        self.assertGreaterEqual(reach.view(s, 'ana')['people']['ana']['hits'], 5)

    def test_foryou_keeps_polished_replacements_when_original_five_are_seen(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou']
        cards = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs]
        with patch.object(s, 'candidate_pool', side_effect=lambda *a, **kw: cards if kw.get('metas') else []), \
                patch.object(s, 'llm_sections', return_value={}), patch.object(s, 'profile_text', return_value='Invented taste'):
            feed = s.personal_feed(s.LANG)
        stored = feed[s.rid('foryou')]['tv']
        self.p['seen'].update(x['id'] for x in stored[:5])
        cid = self.put('foryou', feed)
        for who in ('ana', 'ben', 'both'):
            with self.subTest(who=who):
                served = s.served_row(cid, 'tv', s.LANG, self.p, set(), (set(), set()), who=who)
                self.assertEqual([x['id'] for x in served[:5]], [x['id'] for x in cards[5:10]])
                self.assertTrue(all(x.get('poster') and x.get('background') for x in served[:5]))
                self.assertFalse(self.p['seen'] & {x['id'] for x in served})
                self.assert_turns([x['id'] for x in served[:20]])

    def test_foryou_keeps_polished_refills_when_only_three_recommendations_remain(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['foryou']
        cards = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:28]]
        refills = []
        for i in range(20):
            it = {**self.recs[0], 'id': 1000+i, 'title': f'Invented refill {chr(65+i)}'}
            s._facets[f'tmdb:{it["id"]}'] = {'kind': 'movie', 'g': ['Drama'], 'x': {'mind_bender': 2}}
            refills.append(s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it))
        self.put('ben', {s.rid('ben'): {'tv': refills}})
        with patch.object(s, 'candidate_pool', side_effect=lambda *a, **kw: cards if kw.get('metas') else []), \
                patch.object(s, 'llm_sections', return_value={}), patch.object(s, 'profile_text', return_value='Invented taste'):
            feed = s.personal_feed(s.LANG)
        cid = self.put('foryou', feed)
        self.p['seen'].update(x['id'] for x in cards[:25])
        for seen_catalog in (0, 8, 12):
            self.p['seen'].update(sorted(self.catalog)[:seen_catalog])
            for who in ('ana', 'ben', 'both'):
                with self.subTest(seen_catalog=seen_catalog, who=who):
                    served = s.served_row(cid, 'tv', s.LANG, self.p, set(), (set(), set()), who=who)
                    self.assertEqual([x['id'] for x in served[:5]],
                                     [f'tmdb:{i}' for i in (925, 926, 927, 1000, 1001)])
                    self.assertTrue(all(x.get('poster') and x.get('background') for x in served[:5]))
                    self.assertFalse(self.p['seen'] & {x['id'] for x in served})
                    self.assertEqual(len(served), max(12, 17-seen_catalog))
                    self.assertEqual(len(served), len({x['id'] for x in served}))

    def test_unmarked_foryou_keeps_stored_order_after_person_ranking(self):
        s = self.s
        cards = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs]
        for i, card in enumerate(cards):
            s._facets[card['id']]['d'] = [[i+1, 'Invented maker']]
        cid = self.put('foryou', {s.rid('foryou'): {'tv': cards}})
        served = s.served_row(cid, 'tv', s.LANG, self.p, set(), (set(), set()), who='ben')
        self.assertEqual([x['id'] for x in served], [x['id'] for x in cards[:24]])

    def test_unrelated_polishing_keeps_because_medium_order(self):
        s = self.s
        cards = [s.row_meta(it, 'series' if i % 3 == 2 else 'movie', 'tv' if i % 3 == 2 else 'movie',
                            {18: 'Drama'}, it) for i, it in enumerate(self.recs[:18])]
        for card in cards:
            if card['type'] == 'series':
                card['_row']['catalog'] = True
        balanced = s.because_balance(cards, 'tmdb:1')
        self.assertEqual([x['id'] for x in s.polish_row(balanced)], [x['id'] for x in balanced])

    def test_catalog_filler_does_not_displace_own_universe_reservations(self):
        s = self.s
        own = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:8]]
        more = [s.known_meta(key, s.frec(key, None)['kind']) for key in sorted(self.catalog)[:4]]
        for card in own[:4] + more:
            card['_row']['c'] = [{'id': 42, 'name': 'Marvel Studios'}]
        for card in more:
            card['_row']['catalog'] = True
            card['background'] = 'fake-wide'
        ids = {x['id'] for x in own}
        kept = s.polish_row(own + more, own_ids=ids, catalog=True)
        self.assertTrue(ids <= {x['id'] for x in kept})
        self.assertFalse({x['id'] for x in more} & {x['id'] for x in kept})

    def test_catalog_filler_stays_after_own_picks_in_polished_tail(self):
        s = self.s
        own = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:10]]
        filler = s.known_meta('tmdb:101', 'movie')
        filler['_row']['catalog'] = True
        kept = s.polish_row(own + [filler], own_ids={x['id'] for x in own}, catalog=True)
        self.assertEqual(kept, own + [filler])

    def test_catalog_sequel_still_follows_its_original(self):
        s = self.s
        original = s.row_meta(self.recs[0], 'movie', 'movie', {18: 'Drama'}, self.recs[0])
        sequel = s.known_meta('tmdb:101', 'movie')
        original['_row']['collection'] = sequel['_row']['collection'] = 7
        original['_row']['date'] = '2019-01-01'
        sequel['_row']['catalog'] = True
        sequel['background'] = 'fake-wide'
        others = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[1:]]
        kept = [x['id'] for x in s.polish_row([sequel, original, *others], catalog=True)]
        self.assertLess(kept.index(original['id']), kept.index(sequel['id']))

    def test_feeling_catalog_turns_survive_polishing_and_home(self):
        s = self.s
        for key in ('mood1', 'leap', 'newtoyou'):
            with self.subTest(row=key):
                s.CONFIG['household']['rows'] = ['moods' if key == 'mood1' else key]
                pool = s.feeling_pool(self.m, self.p, ['mind_bender'], s.LANG, cid=s.rid(key))
                cards = [meta for _, _, meta in pool]
                if key != 'mood1':
                    cards = s.polish_row(s.balance_row(cards), catalog=True)[:30]
                self.put(key, {s.rid(key): {'tv': cards}}, mood='ideas', seed='tmdb:1')
                self.assert_turns(self.home(key))

    def test_existing_feeling_source_keeps_legacy_order(self):
        s = self.s
        own = [s.row_meta(it, 'movie', 'movie', {18: 'Drama'}, it) for it in self.recs[:20]]
        more = [s.known_meta(key, s._facets[key]['kind']) for key in sorted(self.catalog)]
        for card in more:
            card['_row']['feeling_source'] = 'catalog'
        cards = own + more
        self.assertEqual(s.polish_row(cards, catalog=True), s.polish_row(cards))
        for key in ('mood1', 'leap', 'newtoyou'):
            with self.subTest(row=key):
                s.CONFIG['household']['rows'] = ['moods' if key == 'mood1' else key]
                cid = self.put(key, {s.rid(key): {'tv': cards}}, mood='ideas', seed='tmdb:1')
                served = s.served_row(cid, 'tv', s.LANG, self.p, set(), (set(), set()), who='ana')
                self.assertEqual(served, s.rotate_row(own, cid))

    def test_new_feeling_catalog_cards_mark_weaving(self):
        pool = self.s.feeling_pool(self.m, self.p, ['mind_bender'], self.s.LANG, cid=self.s.rid('mood1'))
        for key, _, meta in pool:
            self.assertEqual(meta['_row'].get('catalog') is True, key in self.catalog)

    def test_feeling_builders_keep_catalog_before_their_stored_cuts(self):
        s = self.s
        for i in range(1, 4):
            s._facets[f'tmdb:{i}'] = {'kind': 'movie', 'title': f'Invented anchor {chr(64+i)}',
                                     'g': ['Drama'], 'y': '2020', 'x': {'mind_bender': 3}}
        for key in self.catalog | {f'tmdb:{it["id"]}' for it in self.recs}:
            s._facets[key]['x']['big_ideas'] = 2

        def transport(path, body, timeout, lane, name):
            self.assertEqual(name, 'theme_source')
            inp = json.loads(body['messages'][1]['content'])
            keep = list(range(len(inp['candidates'])))
            answer = {'fits_now': True, 'months': 'any', 'keep': keep, 'add': [],
                      'why': {str(i): 'An invented puzzle changes how its world works.' for i in keep}}
            return {'choices': [{'message': {'content': json.dumps(answer)}}]}

        with patch.object(s, 'OR_KEY', 'fake'), patch.object(s, 'openrouter', side_effect=transport), \
                patch.object(s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)), \
                patch.object(s, 'pick_moods', return_value=['ideas']), \
                patch.object(s, 'taste_hint', return_value='Invented taste'), \
                patch.object(s, 'archetype_view', return_value={'name': 'Invented taste'}), \
                patch.object(s, 'newtoyou_plan', return_value=[('mind_bender', 'big_ideas', 'neighbour')]):
            for key, build in (('mood1', s.mood_feed), ('leap', s.leap_feed), ('newtoyou', s.newtoyou_feed)):
                with self.subTest(row=key):
                    s.CONFIG['household']['rows'] = ['moods' if key == 'mood1' else key]
                    feed = build(s.LANG)
                    self.assertGreaterEqual(sum(x['id'] in self.catalog for x in feed[s.rid(key)]['tv']), 5)
                    self.put(key, feed, **feed['_names'][s.rid(key)])
                    self.assert_turns(self.home(key))
            with patch.object(s, 'themed_row', return_value=None):
                s.CONFIG['household']['rows'] = ['leap']
                feed = s.leap_feed(s.LANG)
                self.put('leap', feed, **feed['_names'][s.rid('leap')])
                self.assert_turns(self.home('leap'))


if __name__ == '__main__':
    unittest.main()
