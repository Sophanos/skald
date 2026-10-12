"""Maker lanes use synthetic evidence and cached TMDB seams only."""
from contextlib import redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import patch

import test_lanes_r3 as lanes

cards = lanes.cards


class Makers(unittest.TestCase):
    setUpClass = classmethod(lanes.LanesR3.setUpClass.__func__)
    setUp = lanes.LanesR3.setUp
    facet = lanes.LanesR3.facet
    put = lanes.LanesR3.put

    def evidence(self, person, maker=1, n=2, age=1, signal='loved', weight=2):
        p = self.s._profiles['household']
        for i in range(n):
            key = f'tmdb:{10000+len(p["entries"])}'
            p['entries'].append(dict(key=key, kind='movie', title=key, year='2020', by=[person],
                                     age=age+i, signal=signal, weight=weight))
            self.s._facets[key] = {'kind': 'movie', 'd': [[maker, f'Maker {maker}']]}
            p['seen'].add(key)

    def build(self, pool, themes=()):
        s = self.s
        model = {'aff': {fid: 1 for fid in themes}, 'names': {fid: fid for fid in themes}}
        for fid, xs in pool.items():
            self.facet(xs, fid)
        with patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'lane_titles', side_effect=lambda fid, *a, **kw: [(x['id'], 'movie', x) for x in pool[fid]]) as fetch, \
                patch.object(s, 'rank_by_taste', side_effect=lambda m, xs: [(None, [], *x) for x in xs]), \
                patch.object(s, 'lane_title', side_effect=lambda fid, *a: f'Title {fid}'), \
                patch.object(s, 'person_loves', return_value=[]), redirect_stdout(io.StringIO()):
            out = s.lanes_feed('en-US')
        return out, fetch

    def test_maker_builds_with_ten_unseen_titles(self):
        s, xs = self.s, cards(10)
        self.evidence(s.PEOPLE[0], signal='liked')
        out, fetch = self.build({'d:1': xs})
        self.assertEqual(out['_names'], {s.LANES[0]: {'title': 'More from Maker 1', 'lane': 'd:1'}})
        self.assertEqual(out[s.LANES[0]]['tv'], xs)
        fetch.assert_called_once_with('d:1', 'en-US', makers_only=True)

    def test_seven_unseen_maker_titles_fall_back_to_theme(self):
        s = self.s
        self.evidence(s.PEOPLE[0], signal='liked')
        out, fetch = self.build({'d:1': cards(7), 'k:1': cards(20, 100)}, themes=('k:1',))
        self.assertEqual(out['_names'], {s.LANES[0]: {'title': 'Title k:1', 'lane': 'k:1'}})
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['d:1', 'k:1'])

    def test_theme_with_ten_items_is_skipped(self):
        out, fetch = self.build({'k:1': cards(10)}, themes=('k:1',))
        self.assertEqual(out['_names'], {})
        fetch.assert_called_once_with('k:1', 'en-US')

    def test_maker_requires_two_positive_distinct_titles(self):
        s = self.s
        self.evidence(s.PEOPLE[0], n=1)
        self.evidence(s.PEOPLE[0], signal='disliked', weight=-1)
        self.assertEqual(s.maker_seeds(s._profiles['household'], s.PEOPLE[0]), [])
        self.evidence(s.PEOPLE[0], n=1, signal='watched')
        self.assertEqual(s.maker_seeds(s._profiles['household'], s.PEOPLE[0])[0][0], 'd:1')
        s._profiles['household']['entries'] *= 2
        self.assertEqual(len(s.maker_seeds(s._profiles['household'], s.PEOPLE[0])), 1)

    def test_strongest_count_then_recency_and_three_maker_bound(self):
        s = self.s
        for maker in range(1, 6):
            self.evidence(s.PEOPLE[0], maker, age=maker)
        self.evidence(s.PEOPLE[0], 5, n=1, age=90)
        seeds = s.maker_seeds(s._profiles['household'], s.PEOPLE[0])[:3]
        self.assertEqual([fid for fid, name in seeds], ['d:5', 'd:1', 'd:2'])
        out, fetch = self.build({fid: [] for fid, name in seeds})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(out['_names'], {})

    def test_title_seen_hidden_shown_exclusion_and_floor(self):
        s, xs = self.s, cards(12)
        self.evidence(s.PEOPLE[0])
        p = s._profiles['household']
        p['seen'].add(xs[0]['id'])
        p['negative'].add(xs[1]['id'])
        s._feeds[s.rid('week'), 'en-US'] = {'metas': {'tv': xs[2:4]}}
        out, fetch = self.build({'d:1': xs})
        cid = next(iter(out['_names']))
        self.assertEqual(out['_names'][cid], {'title': 'More from Maker 1', 'lane': 'd:1'})
        self.assertEqual(out[cid]['tv'], xs[4:])
        self.assertEqual(self.build({'d:1': xs[:11]})[0]['_names'], {})

    def test_together_requires_support_from_every_member_and_served_minimum(self):
        s, xs = self.s, cards(20)
        self.evidence(s.PEOPLE[0])
        self.put(s.LANES[0], xs, 'd:1')
        s._feeds[s.LANES[0], 'de-DE']['names']['title'] = 'More from Maker 1'
        p = s._profiles['household']
        for who, expected in ((s.PEOPLE[0], True), (s.PEOPLE[1], False), ('both', False), ('none', True)):
            self.assertEqual(bool(s.served_row(s.LANES[0], 'tv', 'de-DE', p, set(), (set(), set()), who)), expected)
        self.evidence(s.PEOPLE[1], signal='liked')
        self.assertTrue(s.served_row(s.LANES[0], 'tv', 'de-DE', p, set(), (set(), set()), 'both'))
        self.assertEqual(s.served_row(s.LANES[0], 'tv', 'de-DE', p, {x['id'] for x in xs[5:]}, (set(), set()), 'both'), [])

    def test_no_qualifying_maker_leaves_existing_lanes_unchanged(self):
        before = lanes.LanesR3.build(self, {'k:1': cards(20)})
        self.evidence(self.s.PEOPLE[0], n=1)
        self.assertEqual(lanes.LanesR3.build(self, {'k:1': cards(20)}), before)

    def test_each_member_gets_their_strongest_maker(self):
        s = self.s
        for person, maker in zip(s.PEOPLE, (1, 2)):
            self.evidence(person, maker)
            self.evidence(person, maker+10, age=20)
        out, fetch = self.build({f'd:{mid}': cards(20, mid*100) for mid in (1, 2, 11, 12)})
        self.assertEqual([x['title'] for x in out['_names'].values()], ['More from Maker 1', 'More from Maker 2'])
        self.assertEqual(fetch.call_count, 2)
        spec = next(x for x in s.job_specs(s.local_now(), {}) if x[0] == 'lanes')
        self.assertEqual(spec[1], s.LANE_ROWS)
        self.assertEqual(spec[3], 3600)

    def test_shared_support_does_not_skip_members_stronger_viable_maker(self):
        s = self.s
        for person, maker, count in ((s.PEOPLE[0], 1, 5), (s.PEOPLE[0], 2, 4),
                                     (s.PEOPLE[1], 3, 3), (s.PEOPLE[1], 2, 2)):
            self.evidence(person, maker, n=count)
        out, fetch = self.build({f'd:{mid}': cards(20, mid*100) for mid in (1, 2, 3)})
        self.assertEqual([x['lane'] for x in out['_names'].values()], ['d:1', 'd:3'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['d:1', 'd:3'])
        out, fetch = self.build({'d:1': [], 'd:2': cards(20, 200), 'd:3': cards(20, 300)})
        self.assertEqual([x['lane'] for x in out['_names'].values()], ['d:2', 'd:3'])
        out, fetch = self.build({'d:1': [], 'd:2': cards(20, 200), 'd:3': []})
        self.assertEqual([x['lane'] for x in out['_names'].values()], ['d:2'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['d:1', 'd:2', 'd:3'])

    def test_shared_titles_respect_each_members_own_opinion(self):
        s = self.s
        self.evidence('together')
        p = s._profiles['household']
        for e in p['entries']:
            e['opinions'] = {s.PEOPLE[1]: {'signal': 'disliked', 'weight': -1}}
        self.assertTrue(s.maker_seeds(p, s.PEOPLE[0]))
        self.assertEqual(s.maker_seeds(p, s.PEOPLE[1]), [])
        self.assertFalse(s.maker_view({'title': 'More from Maker 1', 'lane': 'd:1'}, p, 'both'))

    def test_remote_hidden_candidate_is_excluded(self):
        s, xs = self.s, cards(17)
        self.evidence(s.PEOPLE[0])
        with patch.object(s, 'remote_masks', return_value=({xs[0]['id']}, set())):
            out, fetch = self.build({'d:1': xs})
        cid = next(iter(out['_names']))
        self.assertEqual(out[cid]['tv'], xs[1:])

    def test_personal_pin_survives_other_viewers_and_home_cache_matches(self):
        s = self.s
        self.evidence(s.PEOPLE[0])
        self.put(s.LANES[0], cards(20), 'd:1')
        s._feeds[s.LANES[0], 'de-DE']['names']['title'] = 'More from Maker 1'
        self.put(s.LANE_BENCH[0], cards(20, 100), 'k:2')
        with patch.object(s, 'DEDUPE_ORDER', s.LANES), patch.object(s, 'LANG', 'de-DE'), \
                tempfile.TemporaryDirectory() as temp, patch.object(s, 'DATA', temp):
            s.pin_shown('de-DE')
        with patch.object(s, 'DEDUPE_ORDER', s.LANES):
            for who in ('none', *s.PEOPLE, 'both'):
                cached = s.row_metas(s.LANES[0], 'tv', 'de-DE', who=who)
                with patch.object(s, '_HOME_CACHE_ENABLED', False):
                    plain = s.row_metas(s.LANES[0], 'tv', 'de-DE', who=who)
                self.assertEqual(cached, plain)
                self.assertEqual(cached.names['title'], 'More from Maker 1')
                self.assertEqual(bool(cached), who in ('none', s.PEOPLE[0]))

    def test_combined_credits_only_movie_directors_and_series_creators(self):
        s = self.s
        crew = [dict(id=i, media_type=kind, job=job, vote_count=500, vote_average=8, title='Synthetic',
                     name='Synthetic', poster_path='/p', genre_ids=[], release_date='2020-01-01', first_air_date='2020-01-01')
                for i, (kind, job) in enumerate([('movie', 'Director'), ('tv', 'Creator'), ('movie', 'Writer'),
                                               ('tv', 'Director'), ('movie', 'Creator')])]
        with patch.object(s, 'tmdb', return_value={'crew': crew, 'cast': crew}) as fetch, \
                patch.object(s, 'genres', return_value={}), patch.object(s, 'quality_ok', return_value=True), \
                patch.object(s, 'tone_ok', return_value=True):
            result = s.lane_titles('d:1', 'en-US', makers_only=True)
        self.assertEqual([x[0] for x in result], ['tmdb:0', 'tmdb:1'])
        self.assertEqual(fetch.call_count, 1)



if __name__ == '__main__':
    unittest.main()
