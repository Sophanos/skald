"""Feeling catalog reach with invented titles and fake transports."""
from collections import Counter
from contextlib import redirect_stdout
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from decide_kit import close_server, load_server, reset
from skald import commands, members, rowplan
from skald.__main__ import main, parser, text_output
from test_w111_pool import catalog, model


class FeelingReachTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        Path(self.temp, 'skald.json').write_text(json.dumps(members.fresh_config(['ana', 'ben'])))
        self.s = load_server(self.temp)
        self.addCleanup(close_server, self.s)
        self.p = {'entries': [{'key': 'tmdb:1', 'kind': 'movie', 'title': 'Invented anchor',
                              'weight': 2, 'signal': 'loved', 'by': ['ana']}],
                  'seen': {'tmdb:1'}, 'negative': set()}
        self.m = model()
        self.m['names'] = {fid: 'Invented maker' for fid in self.m['aff']}
        self.m['holders'] = {'x:mind_bender': [(2, 'tmdb:1')]}
        self.s._profiles['household'] = self.p
        self.s._model['m'] = self.m
        self.s._facets.update(catalog())
        for rec in self.s._facets.values():
            rec['x'] = {'mind_bender': 3}
        self.s._facets['tmdb:1'] = {'kind': 'movie', 'title': 'Invented anchor', 'x': {'mind_bender': 3}}
        self.recs, self.calls = [], []
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=AssertionError('unexpected TMDB')))
        self.enterContext(patch.object(self.s, 'with_art', side_effect=lambda *a: (self.recs, {})))
        self.enterContext(patch.object(self.s, 'genres', return_value={18: 'Drama'}))
        self.enterContext(patch.object(self.s, 'get_model', return_value=self.m))
        self.tired = self.enterContext(patch.object(self.s, 'tired_keys', wraps=self.s.tired_keys))
        self.enterContext(patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 10, 14, 18)))
        self.enterContext(patch.object(self.s, 'pick_moods', return_value=['ideas']))
        self.enterContext(patch.object(self.s, 'OR_KEY', 'fake'))
        self.enterContext(patch.object(self.s, 'openrouter', side_effect=self.llm))
        self.facets = self.enterContext(patch.object(self.s, 'ensure_facets'))
        self.emb = self.enterContext(patch.object(self.s, 'ensure_emb'))
        self.tags = self.enterContext(patch.object(self.s, 'tag_experiences'))
        self.enterContext(patch.object(self.s, 'archetype_view', return_value={'name': 'Invented taste'}))
        self.enterContext(patch.object(self.s, 'taste_hint', return_value='Invented taste'))
        self.enterContext(patch.object(self.s, 'save_feeds'))
        self.enterContext(patch.object(self.s, 'connectors_tick'))
        self.enterContext(patch.object(self.s, 'aio_rows_changed'))
        self.enterContext(redirect_stdout(io.StringIO()))

    def llm(self, path, body, timeout, lane, name):
        self.assertEqual(name, 'theme_source')
        inp = json.loads(body['messages'][1]['content'])
        self.calls.append(inp)
        keep = list(range(len(inp['candidates'])))
        return {'choices': [{'message': {'content': json.dumps({'fits_now': True, 'keep': keep,
                'add': [], 'why': {str(i): 'An invented puzzle changes how its world works.' for i in keep}})}}],
                'usage': {'cost': .0001}}

    def recommendations(self, n=80):
        for i in range(900, 900+n):
            self.recs.append({'id': i, 'title': f'Invented recommendation {i}', 'release_date': '2020-01-01',
                              'genre_ids': [18], 'vote_count': 500, 'vote_average': 8, 'poster_path': '/fake.jpg'})
            self.s._facets[f'tmdb:{i}'] = {'kind': 'movie', 'title': f'Invented recommendation {i}',
                    'x': {'mind_bender': 2}, 'g': ['Drama'], 'd': [[1, 'Invented maker']]}

    def pool(self, **kwargs):
        return self.s.feeling_pool(self.m, self.p, ['mind_bender'], 'en-US', **kwargs)

    def build(self):
        self.s.build_feeds(self.s.local_now(), {}, {}, only={'moods'}, force=True)
        return self.s._feeds[self.s.MOODS_ROWS[0], 'en-US']

    def test_top_tagged_catalog_title_absent_from_recommendations_reaches_big_ideas(self):
        feed = self.build()
        self.assertEqual(feed['names']['mood'], 'ideas')
        self.assertIn('tmdb:179', {x['id'] for x in feed['metas']['tv']})
        self.assertEqual(self.recs, [])

    def test_strength_one_never_enters_catalog_source(self):
        self.s._facets['tmdb:100']['x'] = {'mind_bender': 1}
        keys = {key for key, _, _ in self.pool()}
        self.assertIn('tmdb:101', keys)
        self.assertNotIn('tmdb:100', keys)

    def test_catalog_filters_seen_negative_tired_ignored_talk_votes_quality_and_tone(self):
        s = self.s
        self.p['seen'].add('tmdb:100')
        self.p['negative'].add('tmdb:101')
        s._facets['tmdb:104']['g'] = ['Reality']
        s._facets['tmdb:105']['vote_count'] = 1
        s._facets['tmdb:106']['vote_average'] = 3
        s._facets['tmdb:107']['g'] = ['Family']
        with patch.object(s, 'tired_keys', return_value={'tmdb:102': 'Invented tired'}), \
                patch.object(s, 'title_fixes', return_value=[{'op': 'ignore', 'key': 'tmdb:103', 'who': 'ana'}]):
            keys = {key for key, _, _ in self.pool(cid=s.MOODS_ROWS[0])}
        self.assertIn('tmdb:108', keys)
        self.assertFalse(keys & {f'tmdb:{i}' for i in range(100, 108)})

    def test_catalog_keeps_kid_safety(self):
        self.s.CONFIG['taste']['people']['ana'] = {'prefs': {'kids_age': 10}}
        rec = self.s._facets['tmdb:100']
        rec['cr'] = {'US': 'TV-G'}
        keys = {key for key, _, _ in self.pool(person='ana')}
        self.assertIn('tmdb:100', keys)
        self.assertNotIn('tmdb:101', keys)

    def test_pool_computes_exclusions_once_with_populated_learning(self):
        s, cid = self.s, self.s.MOODS_ROWS[0]
        s._learn['served'] = {f'tmdb:{100+i}': {'name': 'Invented shown',
            'rows': {cid: {'days': list(range(s.LEARN_TIRED if i == 0 else 3))}}} for i in range(4000)}
        with patch.object(s, 'row_masks', wraps=s.row_masks) as masks:
            keys = {key for key, _, _ in self.pool(cid=cid)}
        self.assertNotIn('tmdb:100', keys)
        self.assertIn('tmdb:101', keys)
        self.tired.assert_called_once_with({cid})
        masks.assert_called_once_with('both')

    def test_sources_take_equal_turns_before_forty_and_rank_by_strength_and_match(self):
        self.recommendations()
        # Recommendation records have no stored card, but TMDB supplies their cards.
        with patch.object(self.s, 'match_pct', side_effect=lambda m, sc, *a: sc), \
                patch.object(self.s, 'person_score', side_effect=lambda m, k, *a: (90 if k == 'tmdb:100' else 60, [])), \
                patch.object(self.s, 'score_key', side_effect=lambda m, k, **kw: (99 if k == 'tmdb:979' else 50, [])):
            pool = self.pool()
        first = [x for _, _, x in pool[:40]]
        self.assertEqual(Counter(x['_row']['feeling_source'] for x in first), {'catalog': 20, 'recommendations': 20})
        self.assertEqual([k for k, _, _ in pool[:2]], ['tmdb:100', 'tmdb:979'])
        self.assertEqual(len({k for k, _, _ in pool}), len(pool))

    def test_catalog_duplicate_does_not_spend_recommendation_turns(self):
        self.recommendations()
        self.recs.insert(0, {'id': 179, 'title': 'Invented overlap', 'vote_count': 500,
                           'vote_average': 8, 'release_date': '2020-01-01', 'poster_path': '/fake.jpg'})
        pool = self.pool()
        self.assertEqual(sum(k == 'tmdb:179' for k, _, _ in pool), 1)
        self.assertEqual(Counter(x['_row']['feeling_source'] for _, _, x in pool[:40]),
                         {'catalog': 20, 'recommendations': 20})

    def test_missing_stored_card_skips_and_counts_but_cached_art_works(self):
        for i in (100, 101):
            self.s._facets[f'tmdb:{i}'].pop('poster_path')
        self.s._feeds[self.s.rid('leap'), 'en-US'] = {'metas': {'tv': [
            {'id': 'tmdb:101', 'type': 'movie', 'name': 'Invented cached card', 'poster': 'stored-p'}]}}
        report = {}
        pool = self.pool(report=report)
        cards = {k: x for k, _, x in pool}
        self.assertNotIn('tmdb:100', cards)
        self.assertEqual(cards['tmdb:101']['poster'], 'stored-p')
        self.assertEqual(report['missing_cards'], 1)

    def test_build_reuses_ranks_for_feelings_home_and_languages(self):
        s = self.s
        with patch.object(s, '_langs', ['en-US', 'de-DE']), \
                patch.object(s, 'llm_sections', return_value={}), patch.object(s, 'profile_text', return_value='Invented'), \
                patch.object(s, 'known_rank', wraps=s.known_rank) as rank:
            s.build_feeds(s.local_now(), {}, {}, only={'moods', 'personal', 'ana', 'ben', 'leap'}, force=True)
        self.assertEqual(Counter(call.args[2] for call in rank.call_args_list), {None: 1, 'ana': 1, 'ben': 1})
        self.assertIsNone(getattr(s._upstream, 'catalog_ranks', None))

    def test_vetting_and_tagging_call_counts_unchanged_and_catalog_not_enriched(self):
        self.recommendations(160)
        s = self.s
        for key, rec in s._facets.items():
            if key != 'tmdb:1':
                rec['x'] = {x: 3 for _, xs, _ in s.MOODS.values() for x in xs}
        self.m['holders'] = {f'x:{x}': [(2, 'tmdb:1')] for _, xs, _ in s.MOODS.values() for x in xs}
        with patch.object(s, 'themed_row', wraps=s.themed_row) as theme:
            tags = s._facets['tmdb:100']['x']
            for i in range(100, 180):
                s._facets[f'tmdb:{i}']['x'] = {}
            self.build()
            before = (theme.call_count, len(self.calls), self.tags.call_count)
            for i in range(100, 180):
                s._facets[f'tmdb:{i}']['x'] = tags
            reset(s)
            s._feeds.clear()
            s._theme_sources.clear()
            self.calls.clear()
            theme.reset_mock()
            for mock in (self.tags, self.facets, self.emb):
                mock.reset_mock()
            self.build()
        # Four shelves still make one pool tagging and one vetting tagging call each.
        self.assertEqual(theme.call_count, 4)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.tags.call_count, 8)
        self.assertEqual((theme.call_count, len(self.calls), self.tags.call_count), before)
        for mock in (self.tags, self.facets, self.emb):
            self.assertTrue(all(int(k[5:]) >= 900 for call in mock.call_args_list for k, _ in call.args[0]))
        for inp in self.calls:
            names = [x['title'] for x in inp['candidates']]
            self.assertTrue(any(n.startswith('Invented recommendation') for n in names))
            self.assertTrue(any(not n.startswith('Invented recommendation') for n in names))

    def test_moods_keep_their_taken_and_tired_exclusions(self):
        s = self.s
        with patch.object(s, 'tired_keys', side_effect=lambda cids: {'tmdb:179': 'Invented tired'}
                          if cids == {'mood:ideas'} else {}):
            self.build()
        first = {x['id'] for x in s._feeds[s.MOODS_ROWS[0], s.LANG]['metas']['tv']}
        second = {x['id'] for x in s._feeds[s.MOODS_ROWS[1], s.LANG]['metas']['tv']}
        self.assertNotIn('tmdb:179', first)
        self.assertIn('tmdb:179', second)
        self.assertFalse(first & second)

    def test_leap_lane_and_newtoyou_share_catalog_reach(self):
        s = self.s
        s._facets['tmdb:1']['x']['big_ideas'] = 3
        for key, rec in s._facets.items():
            if key != 'tmdb:1':
                rec['x']['big_ideas'] = 3
        leap = s.leap_feed('en-US')
        self.assertIn('tmdb:179', {x['id'] for x in leap[s.rid('leap')]['tv']})
        lane = s.lane_titles('x:mind_bender', 'en-US')
        self.assertIn('tmdb:179', {key for key, _, _ in lane})
        with patch.object(s, 'newtoyou_plan', return_value=[('mind_bender', 'big_ideas', 'group')]):
            fresh = s.newtoyou_feed('en-US')
        self.assertIn('tmdb:179', {x['id'] for x in fresh[s.rid('newtoyou')]['tv']})

    def test_newtoyou_plan_reports_sources_and_missing_cards(self):
        s = self.s
        for rec in s._facets.values():
            rec['x']['big_ideas'] = 3
        s._facets['tmdb:100'].pop('poster_path')
        with patch.object(s, 'newtoyou_plan', return_value=[('mind_bender', 'big_ideas', 'group')]):
            fresh = s.newtoyou_feed('en-US')
        cid = s.rid('newtoyou')
        s._feeds[cid, s.LANG] = {'metas': fresh[cid], 'names': fresh['_names'][cid]}
        row = rowplan.why(s, 'newtoyou')
        self.assertEqual(row['candidate_sources'], {'catalog': 79, 'recommendations': 0, 'missing_cards': 1})

    def test_explain_includes_catalog_source_of_pinned_cards(self):
        s = self.s
        meta = s.known_meta('tmdb:179', 'movie')
        meta['_row']['feeling_source'] = 'catalog'
        s._shown[s.rid('leap'), s.LANG] = {'metas': {'tv': [meta]}}
        self.assertIn(s.rid('leap'), s.explain('tmdb:179', 'movie')['feeling_catalog_rows'])

    def test_explain_includes_scoped_catalog_pin_after_feed_changes(self):
        s, cid = self.s, self.s.rid('leap')
        meta = s.known_meta('tmdb:179', 'movie')
        meta['_row']['feeling_source'] = 'catalog'
        s._shown_scoped['ana'] = {(cid, s.LANG): {'metas': {'tv': [meta]}}}
        s._feeds[cid, s.LANG] = {'metas': {'tv': []}}
        with s.addon_scope('ana'):
            self.assertEqual(s.feed(cid, s.LANG)['metas']['tv'], [meta])
            self.assertEqual(s.explain('tmdb:179', 'movie')['feeling_catalog_rows'], [cid])

    def test_commander_resolves_unambiguous_stored_cards_offline(self):
        s = self.s
        for store in (s._feeds, s._shown, s._shown_scoped.setdefault('ana', {})):
            with self.subTest(store=store):
                store[s.rid('leap'), s.LANG] = {'metas': {'tv': [
                    {'id': 'tmdb:9000', 'type': 'series', 'name': 'Invented stored card', 'releaseInfo': '2020'}]}}
                item = commands.find_title(s, {'values': ['invented STORED card'], 'kind': None})
                self.assertEqual(item, {'key': 'tmdb:9000', 'kind': 'tv', 'title': 'Invented stored card', 'year': '2020'})
                store.clear()

    def test_commander_catalog_resolution_respects_kind_ambiguity_and_watched_priority(self):
        s = self.s
        title = s._facets['tmdb:179']['title']
        s._facets['tmdb:9000@tv'] = {'kind': 'tv', 'title': title, 'y': '2021'}
        change = {'values': [title], 'kind': None}
        with patch.object(s, 'resolve_pick', return_value=None) as lookup:
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                commands.find_title(s, change)
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(commands.find_title(s, {**change, 'kind': 'tv'})['key'], 'tmdb:9000')
            self.assertEqual(lookup.call_count, 2)
        self.p['entries'].append({'key': 'tmdb:9001', 'kind': 'movie', 'title': title, 'year': '2019'})
        self.assertEqual(commands.find_title(s, change)['key'], 'tmdb:9001')

    def test_plan_json_and_explain_and_commander_carry_catalog_provenance(self):
        s = self.s
        s._facets['tmdb:100'].pop('poster_path')
        feed = self.build()
        counts = rowplan.why(s, 'mood1')['ai_sourcing']['candidate_sources']
        self.assertEqual(counts, {'catalog': 79, 'recommendations': 0, 'missing_cards': 1})
        # Counts remain available when only persisted feed names are loaded.
        s._row_source_status.clear()
        self.assertEqual(rowplan.why(s, 'mood1')['ai_sourcing']['candidate_sources'], counts)
        engine = MagicMock(_server=s)
        engine.__enter__.return_value = engine
        out = io.StringIO()
        with patch('skald.__main__.Engine', return_value=engine):
            self.assertEqual(main(['--snapshot', self.temp, 'rows', 'plan', '--json'], out=out), 0)
        row = next(r for r in json.loads(out.getvalue())['rows'] if r['id'] == s.MOODS_ROWS[0])
        self.assertEqual(row['ai_sourcing']['candidate_sources'], counts)
        key = feed['metas']['tv'][0]['id']
        kind = 'movie' if feed['metas']['tv'][0]['type'] == 'movie' else 'tv'
        info = s.explain(key, kind)
        self.assertIn(s.MOODS_ROWS[0], info['feeling_catalog_rows'])
        item = {'key': key, 'kind': kind, 'title': s.frec(key, kind)['title']}
        answer = commands.preview(s, 'Why is '+item['title']+' in the Big Ideas row?', viewer='ana', bound='ana',
                                  answer={'op': 'explain', 'values': [item['title']]})
        self.assertIn('catalog', answer['description'])
        self.assertEqual(answer['explanation']['feeling_catalog_rows'], info['feeling_catalog_rows'])
        out = io.StringIO()
        text_output(parser().parse_args(['explain', key]), info, out)
        self.assertIn('catalog', out.getvalue())


if __name__ == '__main__':
    unittest.main()
