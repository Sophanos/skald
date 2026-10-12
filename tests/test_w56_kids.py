"""Child profiles: synthetic data, with TMDB stubbed."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skald import members, setup
from skald.__main__ import main
import test_prefs
from test_rows_r2 import cards

class Kids(unittest.TestCase):
    setUpClass = classmethod(test_prefs.PrefsRows.setUpClass.__func__)
    prefs = test_prefs.PrefsRows.prefs
    served = test_prefs.PrefsRows.served

    def test_kids_age_validation_and_cli(self):
        cfg = members.fresh_config(['mia'])
        for value in (3, 8, 17, 2, 18, True, '8', None, 8.0):
            cfg['taste']['people']['mia'] = {'prefs': {'kids_age': value}}
            if type(value) is int and 3 <= value <= 17:
                self.assertEqual(members.validate_config(cfg)['taste']['people']['mia']['prefs']['kids_age'], value)
            else:
                with self.assertRaises(members.ConfigError):
                    members.validate_config(cfg)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': ''}):
            setup.publish(Path(temp), members.fresh_config(['mia']))
            def run(*flags):
                return main(['setup', 'prefs', '--person', 'mia', '--json', *flags], out=io.StringIO())
            self.assertEqual(run('--kids-age', '8'), 0)
            path = Path(temp) / 'skald.json'
            self.assertIn('child, age 8', setup.prefs_line(json.loads(path.read_text()), 'mia'))
            before = path.read_bytes()
            for flags in (('--kids-age', '2'), ('--kids-age', '18'), ('--kids-age', 'bad'), ('--clear', '--kids-age', '8')):
                self.assertEqual(run(*flags), 2)
                self.assertEqual(path.read_bytes(), before)
            self.assertEqual(run('--discovery', 'gems', '--kids-age', 'off'), 0)
            self.assertEqual(json.loads(path.read_text())['taste']['people']['mia']['prefs'], {'discovery': 'gems'})

    def test_age_ok_tables_and_fallback(self):
        s, tables = self.s, [('US', 'movie', {'G': 0, 'PG': 8, 'PG-13': 13, 'R': 17, 'NC-17': 18}),
                  ('US', 'tv', {'TV-Y': 0, 'TV-Y7': 7, 'TV-G': 0, 'TV-PG': 8, 'TV-14': 14, 'TV-MA': 17}),
                  ('GB', 'movie', {'U': 0, 'PG': 8, '12A': 12, '12': 12, '15': 15, '18': 18}),
                  ('DE', 'movie', {'FSK 0': 0, 'FSK 6': 6, '12': 12, 'FSK 16': 16, '18': 18})]
        for country, kind, ratings in tables:
            with patch.object(s, 'COUNTRY', country):
                for rating, minimum in ratings.items():
                    with self.subTest(country=country, rating=rating):
                        self.assertTrue(s.age_ok({'cr': {country: rating}}, kind, minimum))
                        self.assertFalse(s.age_ok({'cr': {country: rating}}, kind, minimum - 1))
        with patch.object(s, 'COUNTRY', 'Germany'):
            self.assertFalse(s.age_ok({'cr': {'DE': '12', 'US': 'G'}}, 'movie', 8))
            self.assertTrue(s.age_ok({'cr': {'DE': '', 'US': 'G'}}, 'movie', 8))
            self.assertTrue(s.age_ok({'cr': {'DE': 'unrated', 'US': 'G'}}, 'movie', 8))
            for rec in ({}, {'cr': {}}, {'cr': {'DE': 'unknown'}}, {'cr': {'US': 'G'}, 'adult': True}):
                self.assertFalse(s.age_ok(rec, 'movie', 8))

    def test_unknown_rating_hidden_for_child(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', return_value={}):
            self.assertFalse(s.preference_ok(cards(1)[0], s.rid('episodes'), 'ana'))
            self.assertEqual(self.served(s.MOODS_ROWS[0], cards(8), who='ana'), [])

    def rated(self, key, kind):
        if key is None:
            return {}
        return {'cr': {'US': ('G' if kind == 'movie' else 'TV-G') if int(key[5:]) % 2 == 0 else
                        ('R' if kind == 'movie' else 'TV-MA')}}

    def test_child_link_filters_every_row_type(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', side_effect=self.rated), s.addon_scope('ana'), \
                patch.object(s, 'person_entries', side_effect=lambda p, who: p['entries']):
            for cid in list(s.DEDUPE_ORDER) + list(s.DOCS):
                with self.subTest(cid=cid):
                    result = self.served(cid, cards(20), who='ana')
                    self.assertFalse({x['id'] for x in result} & {x['id'] for x in cards(20)[1::2]})
            self.assertEqual([x['id'] for x in self.served(s.rid('episodes'), cards(8), who='ana', typ='movie')],
                             [x['id'] for x in cards(8)[::2]])

    def test_child_viewer_on_shared_link_filters_rows(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', side_effect=self.rated), s.addon_scope(None), \
                patch.object(s, 'who_watching', return_value=('ana', 'explicit')):
            self.assertEqual(self.served(s.rid('top10'), cards(20), who=s.who_watching()[0], typ='movie'), cards(20)[::2])

    def test_child_search_filtered(self):
        s, data = self.s, [{'id': i, 'title': 'Synthetic', 'popularity': 10-i} for i in range(3)]
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', side_effect=self.rated), \
                patch.object(s, 'search_both', return_value=(data, {})), patch.object(s, 'genres', return_value={}):
            for scope in ('ana', None):
                with s.addon_scope(scope), patch.object(s, 'who_watching', return_value=('ana', 'explicit')):
                    for kind in ('movie', 'series'):
                        self.assertEqual([x['id'] for x in s.search(kind, 'Synthetic', 'en-US', title_only=True)['metas']],
                                         ['tmdb:0', 'tmdb:2'])

    def test_adult_rows_and_search_unchanged_with_child_member(self):
        s = self.s
        with patch.object(s, 'frec', return_value={}), patch.object(s, 'search_both', return_value=([{'id': 1, 'title': 'Synthetic'}], {})), \
                patch.object(s, 'genres', return_value={}):
            baseline = s.search('movie', 'Synthetic', 'en-US', title_only=True)
            with self.prefs(ana={'kids_age': 8}), s.addon_scope('ben'):
                self.assertEqual(s.search('movie', 'Synthetic', 'en-US', title_only=True), baseline)
                self.assertEqual(self.served(s.rid('top10'), cards(8), who='ben', typ='movie'), cards(8))

    def test_child_rows_keep_family_titles(self):
        s, x = self.s, {**cards(1)[0], 'genres': ['Family', 'Animation'], '_row': {'rating': 8, 'votes': 1000}}
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', return_value={'cr': {'US': 'G'}}), \
                patch.object(s, 'explicit_seeds', return_value=[]), s.addon_scope('ana'):
            self.assertTrue(s.tone_ok(x, animation=True, person='ana'))
            self.assertFalse(s.tone_ok(x, animation=True, person='ben'))
            self.assertTrue(s.row_eligible(x, s.rid('ana'), 'en-US', {}))

    def test_rating_backfill_bounded_and_only_with_child(self):
        s, facets = self.s, {f'tmdb:{i}': {'kind': 'movie'} for i in range(205)}
        facets['tmdb:1@tv'] = {'kind': 'tv'}
        detail = {'adult': False, 'release_dates': {'results': [
            {'iso_3166_1': 'DE', 'release_dates': [{'certification': '6'}, {'certification': '12'}]},
            {'iso_3166_1': 'US', 'release_dates': [{'certification': 'G'}]},
            {'iso_3166_1': 'FR', 'release_dates': [{'certification': '18'}]}]}}
        with patch.object(s, '_facets', facets), patch.object(s, 'COUNTRY', 'Germany'), \
                patch.object(s, 'tmdb', return_value=detail) as tmdb, patch.object(s, 'clear_seed_cache'):
            with self.prefs(ana={}):
                s.backfill_ratings()
                tmdb.assert_not_called()
            with self.prefs(ana={'kids_age': 8}):
                tmdb.side_effect = [OSError('synthetic')] + [detail] * 199
                s.backfill_ratings()
                self.assertEqual(tmdb.call_count, 200)
                self.assertNotIn('cr', facets['tmdb:0'])
                self.assertEqual(facets['tmdb:1']['cr'], {'DE': '12', 'US': 'G'})
                tmdb.side_effect = None
                tmdb.reset_mock()
                s.backfill_ratings()
                self.assertEqual(tmdb.call_count, 7)
                self.assertIn(unittest.mock.call('tv/1', language='en-US',
                    append_to_response='keywords,credits,content_ratings'), tmdb.call_args_list)
            self.assertEqual(s.facet_record('movie', '1')['cr'], {'DE': '12', 'US': 'G'})
            self.assertIn('release_dates', tmdb.call_args.kwargs['append_to_response'])
            tmdb.return_value = {'content_ratings': {'results': [{'iso_3166_1': 'US', 'rating': 'TV-Y7'}]}}
            self.assertEqual(s.facet_record('tv', '1')['cr'], {'US': 'TV-Y7'})
            self.assertIn('content_ratings', tmdb.call_args.kwargs['append_to_response'])

    def test_child_metadata_hidden(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'get_model', return_value=None), patch.object(s, '_profiles', {}):
            for rec in ({'title': 'Synthetic', 'cr': {'US': 'R'}}, {'title': 'Synthetic'}):
                with patch.object(s, 'frec', return_value=rec), s.addon_scope('ana'):
                    self.assertIsNone(s.title_view('tmdb:1', 'movie', 'together'))

    def test_cached_interview_anchors_rechecked(self):
        s, anchors = self.s, [{'key': 'tmdb:1', 'kind': 'movie'}, {'key': 'tmdb:2', 'kind': 'movie'}]
        stamps = [(str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), None, None)
                  for p in (Path(s.DATA)/n for n in ('feedback.jsonl', 'interview.json'))]
        with patch.object(s, 'get_model', return_value=None), patch.object(s, '_interview_cache', {'ana': (None, stamps, {'anchors': anchors})}), \
                patch.object(s, 'frec', side_effect=lambda key, kind: {'cr': {'US': 'PG-13' if key == 'tmdb:1' else 'G'}}):
            with self.prefs(ana={'kids_age': 13}):
                self.assertEqual(s.interview_next('ana')['anchors'], anchors)
            with self.prefs(ana={'kids_age': 8}):
                self.assertEqual(s.interview_next('ana')['anchors'], anchors[1:])
            with self.prefs(ana={}):
                self.assertEqual(s.interview_next('ana')['anchors'], anchors)

    def test_rank_child_tail_and_fallback_filtered(self):
        s, choices = self.s, [{'key': 'tmdb:1', 'kind': 'movie'}, {'key': 'tmdb:2', 'kind': 'movie'}]
        inp = {'choices': choices, 'person': 'together'}
        with self.prefs(ana={'kids_age': 8}), s.addon_scope('ana'), patch.object(s, 'frec', side_effect=self.rated), \
                patch.object(s, 'title_view', return_value=None):
            self.assertEqual(s.rank_rule()(inp, {})[0]['ranked'], choices[1:])
            self.assertEqual(s.rank_fallback(inp, RuntimeError())[0]['ranked'], choices[1:])

    def test_child_decide_together_keeps_bound_subject(self):
        s, req = self.s, {'body': {'person': 'together', 'in': {'choices': [{'key': 'tmdb:1', 'kind': 'movie'}], 'person': 'together'}},
               'client': {'name': 'synthetic'}, 'tail': 'rank_titles', 'viewer': 'ana'}
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'connectors_demand'), patch.object(s, 'decide_many', return_value=[{}]) as decide:
            s.api_decide(req)
            self.assertEqual(decide.call_args.kwargs['person'], 'ana')
            self.assertEqual(decide.call_args.args[1][0]['person'], 'ana')

    def test_safety_preferences_refresh(self):
        s = self.s
        with tempfile.TemporaryDirectory() as temp, self.prefs(ana={}), patch.dict(os.environ, {'SKALD_CONFIG': str(Path(temp)/'skald.json')}), \
                patch.object(s, 'CONFIG_SOURCE', 'file'), patch.object(s, '_prefs_stamp', None), patch.object(s, 'get_model', return_value=None), \
                patch.object(s, '_profiles', {}), patch.object(s, 'frec', return_value={'title': 'Synthetic', 'cr': {'US': 'R'}}), \
                patch.object(s, 'search_both', return_value=([{'id': 1, 'title': 'Synthetic'}], {})), patch.object(s, 'genres', return_value={}), s.addon_scope('ana'):
            cfg = json.loads(json.dumps(s.CONFIG))
            cfg['taste']['people']['ana']['prefs'] = {'kids_age': 8}
            setup.publish(Path(temp), cfg, replace=True)
            self.assertEqual(s.search('movie', 'Synthetic', 'en-US', title_only=True)['metas'], [])
            self.assertIsNone(s.title_view('tmdb:1', 'movie', 'ana'))

    def test_child_responses_no_store(self):
        from test_scoped_addon import request
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'row_metas', return_value=[]), \
                patch.object(s, 'search', return_value={'metas': []}), patch.object(s, 'log_search'), patch.object(s, 'manifest', return_value={}):
            for scope in (None, 'ana'):
                for tail in ('manifest.json', f'catalog/movie/{s.rid("top10")}.json', f'catalog/movie/{s.rid("search")}/search=Synthetic.json'):
                    self.assertIn('no-store', request(s, tail, scope)[1]['Cache-Control'])
            self.assertNotIn('no-store', request(s, 'manifest.json', 'ben')[1]['Cache-Control'])

    def test_child_top10_minimum(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'frec', side_effect=self.rated):
            self.assertEqual(self.served(s.rid('top10'), cards(4), who='ana', typ='movie'), [])
            self.assertEqual(self.served(s.rid('top10'), cards(4), who='ben', typ='movie'), cards(4))
            with patch.object(s, 'pin_shown'), patch.object(s, 'feed_rows', return_value=[(s.rid('top10'), ('movie',), 'Chart')]), \
                    patch.object(s, 'feed', return_value={'built': 1}), patch.object(s, 'row_metas', return_value=cards(2)):
                self.assertFalse(any(c['id'] == s.rid('top10') for c in s.manifest('en-US', 'ana')['catalogs']))

    def test_older_child_viewer_cannot_loosen_bound_child(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}, ben={'kids_age': 17}), patch.object(s, 'who_watching', return_value=('ben', 'explicit')), \
                patch.object(s, 'frec', return_value={'title': 'Synthetic PG13', 'cr': {'US': 'PG-13'}}), \
                patch.object(s, 'get_model', return_value=None), patch.object(s, '_profiles', {}), patch.object(s, 'connectors_demand'):
            req = {'viewer': 'ana', 'query': {'kind': 'movie', 'person': 'together'}, 'tail': 'tmdb:1'}
            with self.subTest(path='metadata'), self.assertRaises(s.ApiError):
                s.api_title(req)
            with self.subTest(path='rank tail'):
                self.assertEqual(s.rank_fallback({'choices': [{'key': 'tmdb:1', 'kind': 'movie'}], 'person': 'ana'}, None)[0]['ranked'], [])
            with self.subTest(path='decide'):
                req.update(client={'name': 'synthetic'}, tail='rank_titles', body={'person': 'together', 'in': {'choices': [{'key': 'tmdb:1', 'kind': 'movie'}]}})
                self.assertEqual(s.api_decide(req)['answer']['ranked'], [])
            stamps = [(str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), None, None)
                      for p in (Path(s.DATA)/n for n in ('feedback.jsonl', 'interview.json'))]
            with self.subTest(path='interview'), patch.object(s, '_interview_cache', {'ana': (None, stamps, {'anchors': [{'key': 'tmdb:1', 'kind': 'movie'}]})}):
                self.assertEqual(s.interview_next('ana')['anchors'], [])
            with self.subTest(path='link'), s.addon_scope('ana'):
                self.assertIsNone(s.title_view('tmdb:1', 'movie', 'ben'))

    def test_child_viewer_never_filters_adult_api(self):
        s = self.s
        with patch.object(s, 'frec', return_value={'title': 'Synthetic PG13', 'cr': {'US': 'PG-13'}}), \
                patch.object(s, 'get_model', return_value=None), patch.object(s, '_profiles', {}):
            expected = s.api_title({'viewer': 'ben', 'query': {'kind': 'movie'}, 'tail': 'tmdb:1'})
            choices = [{'key': 'tmdb:1', 'kind': 'movie'}]
            with self.prefs(ana={'kids_age': 8}), patch.object(s, 'who_watching', return_value=('ana', 'explicit')):
                for viewer in ('ben', None):
                    with self.subTest(viewer=viewer):
                        self.assertEqual(s.api_title({'viewer': viewer, 'query': {'kind': 'movie', 'person': 'ben'}, 'tail': 'tmdb:1'}), expected)
                        self.assertEqual(s.rank_rule()({'choices': choices, 'person': 'ben'}, {})[0]['ranked'], choices)
                        self.assertEqual(s.rank_fallback({'choices': choices, 'person': 'ben'}, None)[0]['ranked'], choices)
                with s.addon_scope(None):
                    self.assertIsNone(s.title_view('tmdb:1', 'movie'))

    def test_child_row_headings_name_no_blocked_title(self):
        s = self.s
        blocked = 'Synthetic Forbidden'
        x = {**cards(1)[0], 'description': 'Found family like ' + blocked + ' · Safe overview', '_row': {'overview': 'Safe overview'}}
        model = {'holders': {'x:family': [(1, 'tmdb:1')]}, 'ent': {'tmdb:1': (blocked, ['ana'])}, 'kinds': {'tmdb:1': 'movie'}}
        with self.prefs(ana={'kids_age': 8}), s.addon_scope('ana'), patch.object(s, 'pin_shown'), \
                patch.object(s, 'feed', return_value={'names': {'title': 'Same Feeling as ' + blocked}}), \
                patch.object(s, 'row_metas', return_value=cards(s.ROW_MIN)), patch.object(s, 'frec', return_value={'cr': {'US': 'G'}}), \
                patch.object(s, 'femb', return_value=None), patch.object(s, 'fit', return_value=1), patch.object(s, '_taste_accounts', return_value=False):
            self.assertNotIn(blocked, json.dumps(s.manifest('en-US', 'ana')))
            self.assertIsNone(s.anchor(model, 'tmdb:2', 'x:family', person='ana', kind='movie'))
            for card in (x, {**x, '_memory': {'why': 'Because you loved ' + blocked}}):
                self.assertNotIn(blocked, json.dumps(s.human_cards([card])))
                self.assertEqual(s.human_cards([card])[0]['description'], 'Safe overview')

    def test_child_token_refused_on_profile_history_resolve(self):
        from test_api import raw_request
        s = self.s
        client = {'name': 'synthetic', 'person': 'ana', 'scopes': ['read', 'decide', 'events']}
        with self.prefs(ana={'kids_age': 8}), patch.object(s, '_api_auth', return_value=(client, None)), \
                patch.object(s, 'tmdb', side_effect=AssertionError('network forbidden')), patch.object(s, '_api_rates', {}):
            for path, method, body in [('profile', 'GET', None), ('taste', 'GET', None), ('session', 'GET', None),
                                       ('history', 'GET', None), ('feedback', 'GET', None), ('resolve', 'POST', {'items': []}),
                                       ('decide/resolve_title', 'POST', {'in': {}})]:
                with self.subTest(path=path):
                    self.assertEqual(raw_request(s, '/api/v1/' + path, method=method, body=body, handler=s.ApiHandler)[0], 403)
            self.assertEqual(raw_request(s, '/api/v1/decide', handler=s.ApiHandler)[0], 200)
            with patch.object(s, 'write_session', return_value={'active': None, 'last': None}), patch.object(s, 'api_session', return_value={'active': None, 'last': None}):
                self.assertEqual(raw_request(s, '/api/v1/session', method='POST', body={'who': 'ana'}, handler=s.ApiHandler)[0], 201)
            with s.addon_scope('ana'), patch.object(s, 'remote_token', return_value='synthetic'):
                for tail in ('feedback.json', 'session.json'):
                    self.assertEqual(raw_request(s, '/taste/synthetic/' + tail, handler=s.RemoteHandler)[0], 403)
                with patch.object(s, 'session_windows', return_value=[{'id': 'synthetic', 'who': 'ana', 't': s.time.time()-60,
                        'until': s.time.time()+60, 'src': 'tap'}]), patch.object(s, 'session_plays', return_value=[{'title': 'Synthetic Forbidden'}]):
                    self.assertIsNone(s.session_view()['last'])

    def test_cached_reasons_stripped_after_child_age_set(self):
        s = self.s
        blocked = 'Synthetic Forbidden'
        model = {'ent': {}, 'cal': None}
        cached_card = {**cards(1)[0], 'description': 'Found family like ' + blocked}
        with patch.object(s, 'get_model', return_value=model), patch.object(s, '_profiles', {}), \
                patch.object(s, '_title_notes', {'model': None, 'notes': {}}), patch.object(s, 'maturity', return_value={'explicit': 0}), \
                patch.object(s, 'frec', return_value={'title': 'Synthetic Safe', 'cr': {'US': 'G'}}), \
                patch.object(s, 'person_score', return_value=(1, [])), patch.object(s, 'confidence', return_value=('Strong fit', 90)), \
                patch.object(s, 'reasons', return_value=['Found family like ' + blocked]) as reasons, \
                patch.object(s, 'feed_rows', return_value=[(s.rid('ana'), ('tv',), 'Same Feeling as ' + blocked)]), \
                patch.object(s, 'feed', return_value={'names': {'title': 'Same Feeling as ' + blocked}}), \
                patch.object(s, 'row_metas', return_value=[cached_card]):
            with self.prefs(ana={}):
                before = s.title_view('tmdb:0', 'movie', 'ana')
                self.assertIn(blocked, json.dumps(before))
            reasons.reset_mock()
            with self.prefs(ana={'kids_age': 8}):
                for viewer in ('ana', None):
                    with self.subTest(viewer=viewer):
                        row = s.rows_view({'viewer': viewer, 'query': {'person': 'ana'}})['rows'][0]
                        self.assertNotIn(blocked, json.dumps(row))
                        self.assertEqual(row['name'], 'Picked for you')
                self.assertEqual(s.title_view('tmdb:0', 'movie', 'ana')['reasons'], [])
                reasons.assert_not_called()
            with self.prefs(ana={}):
                self.assertEqual(s.title_view('tmdb:0', 'movie', 'ana'), before)

    def test_remote_server_child_refused(self):
        from test_api import raw_request
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'who_watching', return_value=('ana', 'explicit')), \
                patch.object(s, 'remote_token', return_value='synthetic'), patch.object(s, 'remote_history', return_value={}), \
                patch.object(s, 'session_view', return_value={'active': None, 'last': None}), patch.object(s, 'write_session', return_value={'active': None, 'last': None}):
            for tail in ('feedback.json', 'session.json'):
                with self.subTest(tail=tail):
                    self.assertEqual(raw_request(s, '/taste/synthetic/' + tail, handler=s.RemoteHandler)[0], 403)
            self.assertEqual(raw_request(s, '/taste/synthetic/feedback.json', method='POST', body={}, handler=s.RemoteHandler)[0], 403)
            self.assertEqual(raw_request(s, '/taste/synthetic/session.json', method='POST', body={'who': 'ana'}, handler=s.RemoteHandler)[0], 201)
            self.assertIsNone(s._safety_context.get())
            with patch.object(s, 'who_watching', return_value=('ben', 'explicit')):
                for tail in ('feedback.json', 'session.json'):
                    self.assertEqual(raw_request(s, '/taste/synthetic/' + tail, handler=s.RemoteHandler)[0], 200)
