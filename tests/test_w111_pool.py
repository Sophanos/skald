"""Known catalog reach, with invented titles and offline transports."""
import io
import json
import os
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server
from skald import commands, members


def catalog(n=80):
    return {f'tmdb:{100+i}': {'kind': 'movie' if i % 2 else 'tv',
            'title': 'Invented ' + chr(65+i//26) + chr(65+i%26), 'y': '2020',
            'g': ['Drama'], 'd': [[i+1, 'Invented maker']], 'x': {'heartwarming': 1},
            'vote_count': 500, 'vote_average': 8, 'cr': {'US': 'R' if i % 2 else 'TV-MA'},
            'o': 'An invented story.', 'poster_path': '/fake.jpg', 'backdrop_path': '/fake-wide.jpg'} for i in range(n)}


def model(n=80):
    a = {f'd:{i+1}': (n-i)/n for i in range(n)}
    b = {f'd:{i+1}': (i+1)/n for i in range(n)}
    bg = [i/400 for i in range(140)]
    return {'vec': None, 'pvec': {}, 'vbase': 0, 'aff': b, 'paff': {'ana': a, 'ben': b},
            'bscale': 1, 'pbscale': {}, 'names': {}, 'holders': {}, 'ent': {},
            'nexp': {'ana': 50, 'ben': 50, 'household': 100}, 'cal': len(bg),
            'bg': bg, 'pbg': {'ana': bg, 'ben': bg, 'together': bg}}


class CatalogReachTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        config = members.fresh_config(['ana', 'ben'])
        config['household']['member_rows'] = ['ana', 'ben']
        Path(self.temp, 'skald.json').write_text(json.dumps(config))
        Path(self.temp, 'taste.json').write_text('{"people": {}}')
        self.s = load_server(self.temp)
        self.addCleanup(close_server, self.s)
        self.p = {'entries': [], 'seen': set(), 'negative': set(), 'searches': ['invented'], 'likes': []}
        self.m = model()
        self.s._profiles['household'] = self.p
        self.s._model['m'] = self.m
        self.s._facets.update(catalog())
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            self.enterContext(patch.object(self.s, name, side_effect=AssertionError('catalog enriched')))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=lambda *a, **kw: {'results': []}))
        self.enterContext(patch.object(self.s, 'openrouter', side_effect=AssertionError('LLM forbidden')))
        self.enterContext(patch.object(self.s, 'get_model', return_value=self.m))
        self.enterContext(patch.object(self.s, 'tired_keys', return_value={}))
        self.enterContext(patch.object(self.s, 'archetype_view', return_value={}))

    def build(self):
        s = self.s
        with patch.object(s, 'llm_sections', return_value={}), patch.object(s, 'profile_text', return_value='Invented taste'):
            feeds = s.personal_feed('en-US')
        for person in s.PEOPLE:
            feeds.update(s.member_feed('en-US', person) or {})
        for cid, metas in feeds.items():
            if cid != '_names':
                s._feeds[cid, 'en-US'] = {'metas': metas, 'names': feeds.get('_names', {}).get(cid, {})}
        return feeds

    def test_top_five_absent_from_tmdb_reaches_home_and_each_member_improves(self):
        from skald import reach
        before = reach.view(self.s)
        feeds = self.build()
        self.assertIn('tmdb:100', {x['id'] for x in feeds[self.s.rid('ana')]['tv']})
        after = reach.view(self.s)
        for person in ('ana', 'ben'):
            self.assertGreater(after['people'][person]['hits'], before['people'][person]['hits'])
        self.assertTrue(after['people']['ana']['titles'][0]['rows'])

    def test_shared_catalog_takes_turns_even_when_blend_favors_ben(self):
        cards = self.s.catalog_picks(self.p, self.m, self.s.rid('foryou'))
        self.assertEqual([x['id'] for x in cards[:2]], ['tmdb:100', 'tmdb:179'])
        self.assertEqual(cards[2]['id'], 'tmdb:101')
        self.assertEqual(len(cards), self.s.POOL_CATALOG)
        with self.s.addon_scope('together'):
            together = self.s.catalog_picks(self.p, self.m, self.s.rid('foryou'))
        self.assertEqual([x['id'] for x in together], [x['id'] for x in cards])

    def test_shared_catalog_gives_only_eligible_members_equal_turns(self):
        s = self.s
        ranks = {who: [(80-i, [], f'tmdb:{100+i}', 'movie' if i % 2 else 'tv')
                       for i in range(group, 80, 3)] for group, who in enumerate(('ana', 'ben', 'cal'))}
        ranks.update({None: ranks['ben'], 'together': ranks['ben'], 'dee': ranks['ben']})
        with patch.object(s, 'PEOPLE', ('ana', 'ben', 'cal', 'dee')), \
                patch.dict(s.CONFIG['taste']['people'], {'dee': {'prefs': {'kids_age': 10}}}):
            for scope in (None, 'together'):
                with self.subTest(scope=scope), s.addon_scope(scope):
                    cards = s.catalog_picks(self.p, self.m, s.rid('foryou'), ranks=ranks)
                self.assertEqual(Counter((int(x['id'][5:])-100) % 3 for x in cards), {0: 4, 1: 4, 2: 4})

    def test_catalog_cutoff_retains_previous_pick_through_score_noise(self):
        s = self.s
        aff = self.m['paff']['ana']
        aff['d:13'] = aff['d:12']
        first = s.member_feed('en-US', 'ana')
        cid = s.rid('ana')
        original = {x['id'] for x in first[cid]['tv']}
        self.assertIn('tmdb:111', original)
        self.assertNotIn('tmdb:112', original)
        for delta in (1e-9, -1e-9, 1e-9):
            s._feeds[cid, 'en-US'] = {'metas': first[cid]}
            aff['d:13'] = aff['d:12'] + delta
            first = s.member_feed('en-US', 'ana')
            s._feeds[cid, 'en-US'] = {'metas': first[cid]}
            served = s.served_row(cid, 'tv', 'en-US', self.p, set(), (set(), set()), who='ana')
            self.assertEqual({x['id'] for x in served}, original)
        aff['d:13'] = aff['d:12'] + .01
        self.assertIn('tmdb:112', {x['id'] for x in s.member_feed('en-US', 'ana')[cid]['tv']})
        self.p['negative'].add('tmdb:111')
        self.assertNotIn('tmdb:111', {x['id'] for x in s.member_feed('en-US', 'ana')[cid]['tv']})

    def test_dense_seed_pool_keeps_trending_on_served_foryou(self):
        from test_w108_pool import seed, title
        s = self.s
        self.p['entries'] = [seed(i) for i in range(1, 17)]
        self.p['seen'] = {e['key'] for e in self.p['entries']}
        sources = [dict(title(900+i), title='Invented source '+chr(65+i//26)+chr(65+i%26),
                        backdrop_path='/fake-wide.jpg') for i in range(108)]
        trending = {f'tmdb:{x["id"]}' for x in sources[96:]}
        def transport(path, **kw):
            if path.startswith('trending/movie/'):
                return {'results': sources[96:]}
            if path.endswith('/recommendations'):
                start = (int(path.split('/')[1])-1)*6
                return {'results': sources[start:start+6]}
            return {'results': []}
        with patch.object(s, 'tmdb', side_effect=transport), \
                patch.object(s, 'rank_by_taste', side_effect=lambda m, items, **kw: [(1, [], *x) for x in items]), \
                patch.object(s, 'ensure_facets'), patch.object(s, 'ensure_emb'), \
                patch.object(s, 'tag_experiences'), patch.object(s, 'llm_sections', return_value={}), \
                patch.object(s, 'profile_text', return_value='Invented taste'):
            feed = s.personal_feed('en-US')
        cid = s.rid('foryou')
        s._feeds[cid, 'en-US'] = {'metas': feed[cid], 'names': feed['_names'][cid]}
        served = s.served_row(cid, 'tv', 'en-US', self.p, set(), (set(), set()))
        ids = {x['id'] for x in served}
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(ids & trending), 2)
        self.assertEqual(len({x['id'] for x in feed[cid]['tv']} & trending), s.POOL_TRENDING)
        self.assertTrue(ids & set(s._facets))
        self.assertTrue(ids & {f'tmdb:{x["id"]}' for x in sources[:96]})

    def test_build_reuses_catalog_rankings_across_rows_and_languages(self):
        s = self.s
        with patch.object(s, '_langs', ['en-US', 'de-DE']), patch.object(s, 'OR_KEY', 'fake'), \
                patch.object(s, 'llm_sections', return_value={}), \
                patch.object(s, 'profile_text', return_value='Invented taste'), \
                patch.object(s, 'connectors_tick'), patch.object(s, 'save_feeds'), \
                patch.object(s, 'known_rank', wraps=s.known_rank) as rank:
            for build in range(1, 3):
                s.build_feeds(s.local_now(), {}, {}, only={'personal', 'ana', 'ben'}, force=True)
                self.assertEqual(Counter(call.args[2] for call in rank.call_args_list), {'ana': build, 'ben': build})
        self.assertIsNone(getattr(s._upstream, 'catalog_ranks', None))

    def test_catalog_excludes_seen_negative_tired_ignored_and_below_vote_floor(self):
        self.p['seen'].add('tmdb:100')
        self.p['negative'].add('tmdb:101')
        self.s._facets['tmdb:104']['vote_count'] = 1
        self.s._facets['tmdb:105']['adult'] = True
        with patch.object(self.s, 'tired_keys', return_value={'tmdb:102': 'Invented'}), \
                patch.object(self.s, 'title_fixes', return_value=[{'op': 'ignore', 'key': 'tmdb:103', 'who': 'ana'}]):
            cards = self.s.catalog_picks(self.p, self.m, self.s.rid('ana'), 'ana')
        self.assertFalse({f'tmdb:{i}' for i in range(100, 106)} & {x['id'] for x in cards})

    def test_catalog_keeps_child_and_family_filters(self):
        self.s.CONFIG['taste']['people']['ana'] = {'prefs': {'kids_age': 10}}
        self.assertEqual(self.s.catalog_picks(self.p, self.m, self.s.rid('ana'), 'ana'), [])
        self.assertEqual(self.s.catalog_picks(self.p, self.m, self.s.rid('family')), [])

    def test_sources_keep_share_and_catalog_never_enriches(self):
        recs = [{'id': 900+i, 'title': 'Invented seed pick '+chr(65+i), 'vote_count': 500,
                 'vote_average': 8, 'release_date': '2020-01-01'} for i in range(10)]
        entry = {'key': 'tmdb:1', 'id': 1, 'kind': 'movie', 'stype': 'movie', 'title': 'Invented seed',
                 'weight': 1, 'age': 0, 'signal': 'loved', 'item': {}, 'by': {'ana': 1}}
        p = {**self.p, 'entries': [entry], 'seen': {'tmdb:1'}}
        with patch.object(self.s, 'tmdb', side_effect=lambda path, **kw: {'results': recs if 'recommendations' in path else []}), \
                patch.object(self.s, 'rank_by_taste', side_effect=lambda m, items, **kw: [(1, [], *x) for x in items]), \
                patch.object(self.s, 'ensure_facets'), patch.object(self.s, 'ensure_emb'), patch.object(self.s, 'tag_experiences'):
            report = {}
            cards = self.s.candidate_pool(p, metas=True, report=report)
        ids = {x['id'] for x in cards}
        self.assertEqual(len(ids & {f'tmdb:{900+i}' for i in range(10)}), self.s.POOL_PER_SEED)
        self.assertEqual(len(ids & set(self.s._facets)), self.s.POOL_CATALOG)
        self.assertEqual(report['pool_size'], len(cards))

    def test_together_catalog_reasons_use_together_score(self):
        with self.s.addon_scope('together'), patch.object(self.s, 'with_match', side_effect=lambda x, **kw: x) as match:
            self.s.catalog_picks(self.p, self.m, self.s.rid('foryou'))
        self.assertTrue(all(call.kwargs['person'] == 'together' for call in match.call_args_list))

    def test_shared_turn_skips_filtered_titles_before_giving_other_member_a_turn(self):
        self.p['negative'] = {f'tmdb:{i}' for i in range(100, 120)}
        cards = self.s.catalog_picks(self.p, self.m, self.s.rid('foryou'))
        self.assertEqual([x['id'] for x in cards[:2]], ['tmdb:120', 'tmdb:179'])
        self.assertEqual(cards[2]['id'], 'tmdb:121')

    def test_reach_reports_first_drop_and_explain_rank_and_home(self):
        from skald import reach
        self.s._facets['tmdb:101']['vote_count'] = 1
        self.p['negative'].add('tmdb:102')
        with patch.object(self.s, 'tired_keys', return_value={'tmdb:103': 'Invented'}):
            report = reach.view(self.s, 'ana')
        reasons = {x['key']: x['dropped'] for x in report['people']['ana']['titles']}
        self.assertEqual(reasons['tmdb:100'], 'not in pool')
        self.assertEqual(reasons['tmdb:101'], 'below floor')
        self.assertEqual(reasons['tmdb:102'], 'filtered')
        self.assertEqual(reasons['tmdb:103'], 'tired')
        self.assertEqual(reasons['tmdb:119'], 'capped')
        self.build()
        info = self.s.explain('tmdb:100', 'tv')
        self.assertEqual(info['per_person']['ana']['known_rank'], 1)
        self.assertTrue(info['per_person']['ana']['home']['rows'])

    def test_commander_read_uses_llm_and_changes_no_files(self):
        answer = {'op': 'home_reach', 'kind': None, 'values': [], 'person': None}
        before = {p.name: p.read_bytes() for p in Path(self.temp).iterdir() if p.is_file()}
        with patch.object(self.s, 'decide', return_value={'answer': answer}) as decide:
            result = commands.preview(self.s, 'are my best matches on my home?', viewer='ana', bound='ana')
        decide.assert_called_once()
        self.assertTrue(result['read_only'])
        self.assertEqual(set(result['reach']['people']), {'ana'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.temp).iterdir() if p.is_file()})

    def test_commander_reach_defaults_to_shared_home_and_checks_members(self):
        answer = {'op': 'home_reach', 'kind': None, 'values': [], 'person': None}
        for viewer, expected in ((None, {'household', 'ana', 'ben'}),
                                 ('both', {'together'}), ('ana', {'ana'})):
            with self.subTest(viewer=viewer), patch.object(self.s, 'decide', return_value={'answer': answer}):
                result = commands.preview(self.s, 'are my best matches on my home?', viewer=viewer)
                self.assertTrue(result['read_only'])
                self.assertEqual(set(result['reach']['people']), expected)
        for viewer, bound in (('ana', None), (None, 'ana')):
            with self.subTest(viewer=viewer, bound=bound), self.assertRaises(PermissionError):
                commands.preview(self.s, 'synthetic read', viewer=viewer, bound=bound,
                                 answer={**answer, 'person': 'ben'})
        with self.assertRaises(PermissionError):
            commands.preview(self.s, 'synthetic read', viewer=self.s.GUEST, answer=answer)

    def test_cli_reach_json_is_offline_and_input_unchanged(self):
        from skald.__main__ import main
        Path(self.temp, 'profile.json').write_text(json.dumps({'entries': []}))
        Path(self.temp, 'facets.json').write_text(json.dumps({'items': catalog()}))
        before = {p.name: p.read_bytes() for p in Path(self.temp).iterdir() if p.is_file()}
        out = io.StringIO()
        with patch.dict(os.environ, {'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}):
            self.assertEqual(main(['--snapshot', self.temp, 'rows', 'reach', '--top', '20', '--json'], out=out), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(set(result['people']), {'household', 'ana', 'ben'})
        self.assertEqual(result['top'], 20)
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(self.temp).iterdir() if p.is_file()})

    def test_commander_explain_includes_rank_and_home(self):
        item = {'key': 'tmdb:100', 'kind': 'tv', 'title': 'Invented AA', 'year': '2020'}
        with patch.object(commands, 'find_title', return_value=item):
            result = commands.preview(self.s, 'why this invented title?', viewer='ana', bound='ana',
                                      answer={'op': 'explain', 'values': ['Invented AA']})
        self.assertIn('1 of 80', result['description'])
        self.assertFalse(result['explanation']['per_person']['ana']['home']['present'])

    def test_artless_catalog_can_reach_home_with_existing_source_art(self):
        s = self.s
        for rec in s._facets.values():
            rec.pop('poster_path')
            rec.pop('backdrop_path')
        recs = [{'id': 900+i, 'title': 'Invented source '+chr(65+i//26)+chr(65+i%26), 'vote_count': 500,
                 'vote_average': 8, 'release_date': '2020-01-01', 'genre_ids': [18],
                 'poster_path': '/fake.jpg', 'backdrop_path': '/fake-wide.jpg'} for i in range(8)]
        for it in recs:
            s._facets[f'tmdb:{it["id"]}'] = {'kind': 'movie', 'title': it['title'], 'g': ['Drama'],
                                          'x': {'heartwarming': 1}, 'vote_count': 500, 'vote_average': 8}
        self.p['entries'] = [{'key': 'tmdb:1', 'id': 1, 'kind': 'movie', 'stype': 'movie', 'title': 'Invented anchor',
                             'weight': 1, 'age': 0, 'signal': 'loved', 'item': {}, 'by': {'ana': 1}, 'who': 'ana'}]
        with patch.object(s, 'with_art', return_value=(recs, {})), patch.object(s, 'rank_by_taste'), \
                patch.object(s, 'genres', return_value={18: 'Drama'}):
            feed = s.member_feed('en-US', 'ana')
        self.assertIn('tmdb:100', {x['id'] for x in feed[s.rid('ana')]['tv']})

    def test_empty_catalog_leaves_existing_member_source_capacity(self):
        s = self.s
        recs = [{'id': 900+i, 'title': 'Invented source '+chr(65+i//26)+chr(65+i%26), 'vote_count': 500,
                 'vote_average': 8, 'release_date': '2020-01-01', 'poster_path': '/fake.jpg',
                 'backdrop_path': '/fake-wide.jpg'} for i in range(35)]
        for it in recs:
            s._facets[f'tmdb:{it["id"]}'] = {'kind': 'movie', 'title': it['title'], 'g': ['Drama'],
                                          'x': {'heartwarming': 1}, 'vote_count': 500, 'vote_average': 8}
        self.p['entries'] = [{'key': 'tmdb:1', 'id': 1, 'kind': 'movie', 'stype': 'movie', 'title': 'Invented anchor',
                             'weight': 1, 'age': 0, 'signal': 'loved', 'item': {}, 'by': {'ana': 1}, 'who': 'ana'}]
        with patch.object(s, 'catalog_picks', return_value=[]), patch.object(s, 'with_art', return_value=(recs, {})), \
                patch.object(s, 'rank_by_taste'), patch.object(s, 'genres', return_value={}):
            feed = s.member_feed('en-US', 'ana')
        self.assertEqual(len(feed[s.rid('ana')]['tv']), 35)

    def test_catalog_build_keeps_franchise_cap_and_foryou_borrow_limit(self):
        s = self.s
        for rec in s._facets.values():
            rec['collection'] = 7 if rec['d'][0][0] <= 4 else None
        feeds = self.build()
        for cid in (s.rid('foryou'), s.rid('ana'), s.rid('ben')):
            cards = feeds[cid]['tv']
            self.assertLessEqual(sum(s.franchise_key(x) == 'collection:7' for x in cards), 2)
        from test_w108_rows import cards
        own = cards(4, 500)
        with patch.object(s, 'row_pool', return_value=cards(30, 600)), \
                patch.object(s, 'row_eligible', return_value=True):
            s._feeds[s.rid('foryou'), 'en-US'] = {'metas': {'tv': own}}
            served = s.served_row(s.rid('foryou'), 'tv', 'en-US', self.p, set(), (set(), set()))
        self.assertLessEqual(sum(x['id'] not in {y['id'] for y in own} for x in served), 12-len(own))

    def test_reach_ranks_each_viewer_once(self):
        from skald import reach
        with patch.object(self.s, 'known_rank', wraps=self.s.known_rank) as rank:
            reach.view(self.s)
        self.assertEqual(len(rank.call_args_list), 4)

    def test_joint_best_match_reaches_together_home(self):
        from skald import reach
        self.m['paff']['ben'] = self.m['paff']['ana']
        self.build()
        self.assertGreater(reach.view(self.s, 'together')['people']['together']['hits'], 0)

    def test_catalog_reuses_stored_art_without_fetching(self):
        s = self.s
        rec = s._facets['tmdb:100']
        rec.pop('poster_path')
        rec.pop('backdrop_path')
        s._feeds[s.rid('leap'), 'en-US'] = {'metas': {'tv': [
            {'id': 'tmdb:100', 'type': 'series', 'name': 'Invented AA', 'poster': 'stored-p', 'background': 'stored-b'}]}}
        cards = s.catalog_picks(self.p, self.m, s.rid('ana'), 'ana')
        self.assertEqual(cards[0]['poster'], 'stored-p')
        self.assertEqual(cards[0]['background'], 'stored-b')


if __name__ == '__main__':
    unittest.main()
