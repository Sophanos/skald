"""Who's watching contracts, stdlib and offline: python3.12 -m unittest discover -s tests."""
from skald.members import rid
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from eval.profile_golden import FIXTURE, NOW, ROOT, dump, offline_server, sections
from test_api import raw_request


class WhoTests(unittest.TestCase):
    def setUp(self):
        context = offline_server(ROOT / 'server.py')
        self.s = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.s._profiles.clear()
        self.s._feeds.clear()
        self.s._model['m'] = None
        for name in ('imports.jsonl', 'feedback.jsonl'):
            Path(self.s.DATA, name).unlink(missing_ok=True)
        for name, value in [('person_loves', []), ('recent_searches', []), ('read_trakt', [])]:
            p = patch.object(self.s, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.path = Path(self.s.DATA, 'sessions.jsonl')

    def start(self, who='ana', at=NOW, fix=None):
        body = {'who': who}
        if fix is not None:
            body['fix'] = fix
        with patch.object(self.s.time, 'time', return_value=at):
            return self.s.write_session(body, 'phone')

    def fixture(self):
        self.start()
        self.start('off', NOW+3.5*3600)
        self.start('both', NOW+29*3600)
        items = []
        for i, (name, hour, eps) in enumerate([('m1', 1, 0), ('s1', 2, 58), ('s2', 3, 2),
                ('m2', 3.75, 0), ('m3', -1/6, 0), ('m4', None, 0), ('m5', 30, 0), ('m6', 33+1/60, 0)], 1):
            items.append({'id': f'tmdb:{i}', 'type': 'series' if eps else 'movie', 'title': name,
                          'at': (NOW+hour*3600)*1000 if hour is not None else None,
                          'episodes_played': eps, 'played': True, 'profile': 'household'})
        def details(it):
            kind = 'tv' if it['type'] == 'series' else 'movie'
            return kind, {'id': int(it['id'][5:]), 'title': it['title'], 'name': it['title'], 'genres': []}
        p = patch.object(self.s, 'tmdb_details', side_effect=details)
        p.start()
        self.addCleanup(p.stop)
        return items

    def test_reads_missing_expiry_off_and_latest_fix(self):
        s = self.s
        self.assertEqual(s.session_windows(), [])
        self.assertEqual(s.who_watching(), ('both', 'prior'))
        self.assertEqual(s.session_view(), {'active': None, 'last': None})
        self.assertFalse(self.path.exists())
        view = self.start()
        sid = view['active']['id']
        self.assertEqual(s.who_watching(NOW+4*3600-1), ('ana', 'tap'))
        self.assertEqual(s.who_watching(NOW+4*3600), ('both', 'prior'))
        self.start('ben', NOW+10, sid)
        self.start('both', NOW+20, sid)
        self.assertEqual(s.who_watching(NOW), ('both', 'fix'))
        self.assertEqual(s.session_windows()[0]['t'], NOW)
        self.assertEqual(s.session_windows()[0]['until'], NOW+4*3600)
        self.start('off', NOW+100)
        self.assertEqual(s.who_watching(NOW+99), ('both', 'fix'))
        self.assertEqual(s.who_watching(NOW+100), ('both', 'prior'))

    def test_attribution_table_and_person_weights_and_trakt_union(self):
        s, items = self.s, self.fixture()
        events = list(s.tv_events(NOW, items))
        # A confirmed solo session stays solo even after a long series run.
        self.assertEqual([(e['who'], e['by'], e['ctx'].get('src_who')) for e in events], [
            ('ana', ['ana'], 'tap'), ('ana', ['ana'], 'tap'),
            ('ana', ['ana'], 'tap'), ('together', ['together'], None),
            ('together', ['together'], None), ('together', ['together'], None),
            ('together', ['together'], 'tap'), ('together', ['together'], None)])
        self.assertTrue(all(s.observer(e) == 'self' for e in events[:3]))
        profile = s.build_profile(items, [], remote=False)
        ana = {e['key']: e for e in s.person_entries(profile, 'ana')}
        ben = {e['key']: e for e in s.person_entries(profile, 'ben')}
        self.assertTrue(all(ana[f'tmdb:{i}']['explicit'] for i in (1, 2, 3)))
        self.assertNotIn('tmdb:1', ben)
        self.assertNotIn('tmdb:3', ben)
        self.assertNotIn('tmdb:2', ben)
        self.assertNotIn('tmdb:2', {e['key'] for e in s.person_entries(profile, 'together')})
        trakt = [{'id': 'tmdb:1', 'type': 'movie', 'title': 'm1', 'at': NOW*1000, 'signal': 'loved', 'weight': 3}]
        profile = s.build_profile(items, [], trakt, remote=False)
        self.assertEqual(next(e['by'] for e in profile['entries'] if e['key'] == 'tmdb:1'), ['ana', 'ben'])
        for person in s.PEOPLE:
            self.assertTrue(next(e['explicit'] for e in s.person_entries(profile, person) if e['key'] == 'tmdb:1'))

    def test_correction_hash_once_and_expiry_never_rebuilds(self):
        s, items = self.s, self.fixture()
        with patch.object(s, 'read_history', return_value=items), patch.object(s, 'active_profiles', return_value=['household']), \
                patch.object(s, 'learn_from_plays'), patch.object(s, 'log_play_context'), \
                patch.object(s, 'build_profile', wraps=s.build_profile) as build, redirect_stdout(io.StringIO()):
            s.refresh_profiles()
            first = s._profiles['household']['hash']
            other = s.taste_hash('cal', items)
            sid = s.session_windows()[0]['id']
            self.start('ben', NOW+4*3600, sid)
            build.reset_mock()
            s.refresh_profiles()
            changed = s._profiles['household']['hash']
            s.refresh_profiles()
            self.assertNotEqual(first, changed)
            self.assertEqual(other, s.taste_hash('cal', items))
            self.assertEqual(build.call_count, 1)
            self.assertEqual([s.attribute(x)[1] for x in items[:3]], [['ben'], ['ben'], ['ben']])
            for hours in (5, 48):
                build.reset_mock()
                with patch.object(s.time, 'time', return_value=NOW+hours*3600):
                    self.assertEqual(s.taste_hash('household', items), changed)
                    s.refresh_profiles()
                self.assertEqual(build.call_count, 0)
        self.assertEqual(s.attrib_hash(items[3:]), None)

    def test_validation(self):
        s = self.s
        for body in (None, [], {}, {'who': []}, {'who': 'unknown'}, {'who': 'both', 'extra': 1},
                     {'who': 'off', 'fix': 'a'*32}, {'who': 'ben', 'fix': None}, {'who': 'ben', 'fix': 'a'*32}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                s.validate_session(body)
        sid = self.start()['active']['id']
        self.assertEqual(s.validate_session({'who': 'ben', 'fix': sid}), {'who': 'ben', 'fix': sid})
        for body in ({'who': 'ana'}, {'who': 'ben', 'fix': sid}, {'who': 'both', 'fix': None}):
            with self.assertRaises(PermissionError):
                s.validate_session(body, 'ben')
        for who in ('ben', 'both', 'off'):
            with self.assertRaises(s.SessionConflict):
                s.validate_session({'who': who}, 'ben')
        self.start('ben')
        for who in ('ben', 'both', 'off'):
            s.validate_session({'who': who}, 'ben')
        with self.assertRaises(ValueError):
            s.write_session({'who': 'ben'}, 'other')

    def test_rotation_and_orphan_fixes(self):
        events = [{'t': NOW+i, 'id': f'{i:032x}', 'who': 'ben', 'via': 'api', 'fix': None} for i in range(1999)]
        self.path.write_text(''.join(json.dumps(e)+'\n' for e in events))
        self.start('both', NOW+2000)
        kept = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(kept), 1000)
        self.assertEqual(kept[0]['id'], events[1000]['id'])
        events[-1]['fix'] = events[0]['id']
        events[-2]['fix'] = events[1500]['id']
        self.path.write_text(''.join(json.dumps(e)+'\n' for e in events))
        self.start('both', NOW+2000)
        kept = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(kept), 999)
        self.assertNotIn(events[-1]['id'], {e['id'] for e in kept})
        self.assertIn(events[-2]['id'], {e['id'] for e in kept})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corruption_and_cached_reads(self):
        self.start()
        with self.path.open('a') as fh:
            fh.write('{bad json\n')
        self.assertEqual(len(self.s.session_windows()), 1)
        with patch('builtins.open', side_effect=AssertionError('unchanged sessions reopened')):
            self.assertEqual(len(self.s.session_windows()), 1)
        self.start('both')
        kept = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(kept), 2)
        self.assertTrue(all(set(e) == {'t', 'id', 'who', 'via', 'fix'} for e in kept))

    def test_person_order_blend_none_ties_and_model_cache(self):
        s = self.s
        metas = [{'id': f'tmdb:{i}', 'type': 'movie'} for i in range(24)]
        model = {'nexp': {'ben': 20}}
        with patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'explicit_seeds', return_value=['seed']) as seeds, \
                patch.object(s, 'seed_knn', return_value=0), \
                patch.object(s, 'score_key', side_effect=lambda m, key, *a: (100 if key == 'tmdb:17' else -int(key[5:]),)) as score:
            self.assertEqual(s.person_order(metas, 'ben').index(metas[17]), 9)
            self.assertEqual(score.call_count, 24)
            self.assertEqual(s.person_order(metas, 'ben').index(metas[17]), 9)
            self.assertEqual(score.call_count, 24)
            model['nexp']['ben'] = 4
            self.assertEqual(s.person_order(metas, 'ben').index(metas[17]), 9)
            model['nexp']['ben'] = 0
            seeds.return_value = []
            self.assertIs(s.person_order(metas, 'ben'), metas)
        with patch.object(s, 'get_model', return_value={'nexp': {'ben': 20}}), \
                patch.object(s, 'explicit_seeds', return_value=['seed']), patch.object(s, 'score_key', return_value=(None,)) as score:
            self.assertEqual(s.person_order(metas, 'ben'), metas)
            self.assertEqual(score.call_count, 24)
        with patch.object(s, 'get_model', return_value=None):
            self.assertIs(s.person_order(metas, 'ben'), metas)
        self.assertEqual([x['id'] for x in metas], [f'tmdb:{i}' for i in range(24)])

    def test_only_person_rows_and_solo_sessions_order_before_limit(self):
        s = self.s
        metas = [{'id': f'tmdb:{i}', 'name': f'Synthetic {i}', 'type': 'movie'} for i in range(30)]
        with patch.object(s, 'row_list', side_effect=lambda cid, *a: [] if cid == rid('week', s.CONFIG) else metas.copy()), \
                patch.object(s, 'row_eligible', return_value=True), patch.object(s, 'row_related', return_value=True), \
                patch.object(s, 'row_member', return_value=False), \
                patch.object(s, 'fill_row', side_effect=lambda xs, *a: xs), patch.object(s, 'score_key', return_value=(0,)), \
                patch.object(s, 'balance_row', side_effect=lambda xs, **kw: xs), \
                patch.object(s, 'polish_row', side_effect=lambda xs, **kw: xs), \
                patch.object(s, 'rotate_row', side_effect=lambda xs, *a: xs), patch.object(s, 'feed', return_value={}), \
                patch.object(s, 'tired_keys', return_value={}), patch.object(s, 'person_order', side_effect=lambda xs, who: xs[::-1]) as order:
            for who in ('ben', 'ana', 'both', None):
                for cid in sorted(s.PERSON_ROWS | {rid('top10', s.CONFIG), rid('leap', s.CONFIG), rid('docs1', s.CONFIG), rid('rewatch', s.CONFIG)}):
                    order.reset_mock()
                    result = s.served_row(cid, 'tv', 'de-DE', {}, set(), (set(), set()), who=who)
                    expected = who in s.PEOPLE and cid in s.PERSON_ROWS
                    self.assertEqual(order.call_count, int(expected))
                    if expected:
                        self.assertEqual(result[0]['id'], 'tmdb:29')
            with patch.object(s, 'who_watching', return_value=('ben', 'tap')) as watching, \
                    patch.object(s, 'served_row', return_value=[]) as served:
                s.row_metas(rid('lane5', s.CONFIG), 'tv', 'de-DE')
                watching.assert_called_once_with()
                self.assertTrue(all(c.kwargs['who'] == 'ben' for c in served.call_args_list))

    def test_session_view_and_stats_counts_only(self):
        s, items = self.s, self.fixture()
        for it in items: it['profile'] = 'private-history-name'
        s._profiles['household'] = s.build_profile(items, [], remote=False)
        view = s.session_view()
        self.assertEqual(view['last']['plays'], 3)
        self.assertEqual(len(view['last']['titles']), 2)
        self.assertEqual(s.session_view('ben'), {'active': None, 'last': None})
        who = s.stats()['who']
        self.assertEqual(who, {'active': 1, 'minutes_left': 210, 'windows': 2,
                              'attributed': {'ana': 3, 'ben': 0, 'both': 1}, 'labelled': 4})
        self.assertNotIn('private-history-name', json.dumps(view))
        self.assertNotIn('private-history-name', json.dumps(s.stats()))
        self.assertTrue(all(type(v) is int for k, v in who.items() if k != 'attributed'))
        with patch.object(s.time, 'time', return_value=NOW+25*3600):
            self.assertIsNone(s.session_view()['last'])

    def test_favorite_without_playback_has_no_play_credit(self):
        s, items = self.s, self.fixture()
        favorite = {**items[0], 'played': False, 'favorite': True}
        profile = s.build_profile([favorite], [], remote=False)
        entry = profile['entries'][0]
        self.assertEqual((entry['signal'], entry['weight']), ('loved', 3))
        self.assertFalse(s.real_play(entry))
        s._profiles['household'] = profile
        with patch.object(s.time, 'time', return_value=NOW+1.5*3600):
            self.assertIsNone(s.session_view()['last'])
        for playback in ({'played': True}, {'progress': .25}, {'type': 'series', 'episodes_played': 1}):
            with self.subTest(playback=playback):
                self.assertTrue(s.real_play({**entry, 'item': {**favorite, **playback}}))

    def test_base_abandonment_conditions_and_resumed_playback(self):
        s = self.s
        for typ, days, eps, progress in (('movie', 3, 0, .1), ('series', 15, 1, 1.0)):
            with self.subTest(type=typ, episodes=eps):
                item = {'type': typ, 'at': (NOW-days*s.DAY)*1000, 'progress': progress, 'episodes_played': eps}
                self.assertEqual(s.classify(item, NOW), ('abandoned', days))
                resumed = {'progress': .7} if typ == 'movie' else {'episodes_played': 2}
                self.assertEqual(s.classify({**item, **resumed, 'at': NOW*1000}, NOW), ('watched', 0))

    def test_merged_legacy_rating_has_no_play_credit(self):
        s, items = self.s, self.fixture()
        opened = {**items[0], 'played': False}
        rating = {'id': opened['id'], 'type': 'movie', 'title': opened['title'], 'at': None,
                  'rating': 9, 'rated_at': '2026-10-11T00:00:00Z', 'signal': 'loved', 'weight': 3}
        Path(s.DATA, 'history.json').write_text(json.dumps({'items': [opened]}))
        profile = s.build_profile([opened], [], [rating], remote=False)
        entry = profile['entries'][0]
        self.assertEqual((entry['signal'], entry['weight']), ('loved', 3))
        self.assertEqual(entry['item'], opened)
        self.assertEqual(entry['opinions'][s.TRAKT_PERSON]['item']['rating'], 9)
        with self.subTest(credit='real play'):
            self.assertFalse(s.real_play(entry))
        s._profiles['household'] = profile
        with patch.object(s.time, 'time', return_value=NOW+1.5*3600):
            with self.subTest(credit='session'):
                self.assertIsNone(s.session_view()['last'])
            s.note_served(rid('foryou', s.CONFIG), [{'id': opened['id'], 'type': 'movie', 'name': opened['title']}])
            with redirect_stdout(io.StringIO()):
                s.learn_from_plays(profile)
                s.log_play_context(profile)
        with self.subTest(credit='row learning'):
            self.assertEqual((opened['id'] in s._learn['credited'], s._learn['rows'][rid('foryou', s.CONFIG)]['hits']), (False, 0))
        with self.subTest(credit='play context'):
            self.assertFalse(any(c['key'] == opened['id'] for c in s._learn['ctx']))
        for playback in ({'played': True}, {'progress': .25}, {'episodes_played': 1}):
            with self.subTest(playback=playback):
                self.assertTrue(s.real_play({**entry, 'item': {**opened, **playback}}))

    def test_legacy_rating_only_has_no_play_credit(self):
        s = self.s
        self.start()
        rating = {'id': 'tmdb:603', 'type': 'movie', 'title': 'Synthetic rating', 'at': None,
                  'rating': 9, 'rated_at': '2026-10-11T00:00:00Z', 'signal': 'loved', 'weight': 3}
        profile = s.build_profile([], [], [rating], remote=False)
        entry = profile['entries'][0]
        self.assertEqual((entry['signal'], entry['weight']), ('loved', 3))
        with self.subTest(credit='real play'):
            self.assertFalse(s.real_play(entry))
        s._profiles['household'] = profile
        self.assertIsNone(s.session_view()['last'])
        s.note_served(rid('foryou', s.CONFIG), [{'id': rating['id'], 'type': 'movie', 'name': rating['title']}])
        with redirect_stdout(io.StringIO()):
            s.learn_from_plays(profile)
        self.assertEqual((rating['id'] in s._learn['credited'], s._learn['rows'][rid('foryou', s.CONFIG)]['hits']), (False, 0))
        for playback in ({'played': True}, {'progress': .25}, {'episodes_played': 1}):
            with self.subTest(playback=playback):
                self.assertTrue(s.real_play({**entry, 'item': {**rating, **playback}}))

    def test_sessions_feed_existing_online_metrics(self):
        s, items = self.s, self.fixture()
        p = s.build_profile(items, [], remote=False)
        s.note_tv(p, items=items, now=NOW+3*3600)
        self.assertEqual(s._online_meta['errors'], 0)
        metrics = s.metrics(now=NOW+3*3600)
        self.assertEqual(metrics['who']['sessions']['n'], 1)
        self.assertEqual(metrics['who']['ana']['plays'], 3)
        self.start('ben', NOW+4*3600, s.session_windows()[0]['id'])
        metrics = s.metrics(now=NOW+3*3600)
        self.assertEqual(metrics['who']['ben']['plays'], 3)
        self.assertNotIn('ana', metrics['who'])

    def request(self, body=None, headers=None, method='GET', path=None, api=False):
        if api and headers is None:
            if not hasattr(self, 'api_token'):
                self.api_token = self.s.new_client('session-test', ['read', 'events'])
            headers = {'Authorization': 'Bearer '+self.api_token}
        return raw_request(self.s, path or ('/api/v1/session' if api else '/taste/test-cap/session.json'),
                           method=method, body=body, headers=headers, handler=self.s.ApiHandler if api else self.s.RemoteHandler)

    def test_phone_roundtrip_guards_and_combined_rate(self):
        s = self.s
        with patch.object(s, 'remote_token', return_value='test-cap'):
            self.assertEqual(json.loads(self.request()[2]), {'active': None, 'last': None})
            self.assertFalse(self.path.exists())
            status, headers, raw, _ = self.request({'who': 'ana'}, method='POST')
            self.assertEqual(status, 201)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            sid = json.loads(raw)['session']['active']['id']
            self.assertEqual(json.loads(self.request()[2])['active']['who'], 'ana')
            self.assertEqual(self.request({'who': 'ben', 'fix': sid}, method='POST')[0], 201)
            self.assertEqual(self.request({'who': 'off'}, method='POST')[0], 201)
            for body, headers, expected in [({'who': 'both'}, {'Sec-Fetch-Site': 'cross-site'}, 403),
                    ({'who': 'both'}, {'Origin': 'https://other'}, 403), (b'x'*513, {}, 413),
                    ({'who': 'both', 'extra': 1}, {}, 400), ({'who': 'ben', 'fix': 'f'*32}, {}, 400),
                    ({'who': 'both'}, {'Content-Type': 'text/plain'}, 415)]:
                self.assertEqual(self.request(body, headers, 'POST')[0], expected)
            s._remote_posts.clear()
            feedback = {'key': 'tmdb:1', 'kind': 'movie', 'title': 'Synthetic', 'who': 'both', 'action': 'like'}
            for i in range(60):
                path = '/taste/test-cap/' + ('session.json' if i % 2 else 'feedback.json')
                self.assertEqual(self.request({'who': 'both'} if i % 2 else feedback, method='POST', path=path)[0], 201)
            self.assertEqual(self.request({'who': 'both'}, method='POST')[0], 429)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(all(set(json.loads(line)) == {'t', 'id', 'who', 'via', 'fix'} for line in self.path.read_text().splitlines()))

    def test_api_scope_bound_privacy_and_mounts(self):
        s = self.s
        token = s.new_client('private-client-name', ['read', 'events'], 'ben')
        headers = {'Authorization': 'Bearer '+token}
        self.start('ana')
        for handler in (s.ApiHandler, s.RemoteHandler):
            with patch.object(s, 'API_PORT', s.REMOTE_PORT):
                status, response_headers, raw, _ = raw_request(s, '/api/v1/session', headers=headers, handler=handler)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)['active'], None)
                self.assertEqual(response_headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(self.request(api=True)[2])['active']['who'], 'ana')
        self.assertEqual(self.request({'who': 'ana'}, headers, 'POST', api=True)[0], 403)
        self.assertEqual(self.request({'who': 'both'}, headers, 'POST', api=True)[0], 409)
        self.start('off')
        self.assertEqual(self.request({'who': 'both'}, headers, 'POST', api=True)[0], 201)
        sid = s.session_windows()[-1]['id']
        self.assertEqual(self.request({'who': 'ben', 'fix': sid}, headers, 'POST', api=True)[0], 403)
        read = s.new_client('read-client', ['read'])
        self.assertEqual(self.request({'who': 'both'}, {'Authorization': 'Bearer '+read}, 'POST', api=True)[0], 403)
        self.assertEqual(self.request({'who': 'ben'}, {**headers, 'Origin': ''}, 'POST', api=True)[0], 403)
        unbound = s.new_client('unbound-client', ['read', 'events'])
        self.assertEqual(self.request({'who': 'ana', 'fix': sid}, {'Authorization': 'Bearer '+unbound}, 'POST', api=True)[0], 201)
        self.assertNotIn('private-client-name', self.path.read_text())
        self.assertNotIn('private-client-name', json.dumps(s.stats()))
        self.assertNotIn('private-client-name', self.request(headers=headers, api=True)[2].decode())
        self.assertEqual(self.request(method='PUT', headers=headers, api=True)[0], 405)

    def test_catalog_ttl(self):
        s = self.s
        with patch.object(s, 'row_metas', return_value=[]), patch.object(s, 'note_served'), \
                patch.object(s, 'feed_rows', return_value=[(rid('foryou', s.CONFIG), ['tv'], ''), (rid('top10', s.CONFIG), ['movie'], ''),
                    (rid('week', s.CONFIG), ['tv'], ''), (rid('episodes', s.CONFIG), ['series'], ''), (rid('nextseason', s.CONFIG), ['series'], '')]):
            for cid, typ, age in [(rid('foryou', s.CONFIG), 'tv', 60), (rid('top10', s.CONFIG), 'movie', 900),
                    (rid('week', s.CONFIG), 'tv', 60), (rid('episodes', s.CONFIG), 'series', 900), (rid('nextseason', s.CONFIG), 'series', 60)]:
                status, headers, _, _ = raw_request(s, f'/de-DE/catalog/{typ}/{cid}.json', handler=s.Handler)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], f'public, max-age={age}')

    @unittest.skipUnless(os.environ.get('SKALD_SNAPSHOT'), 'SKALD_SNAPSHOT not set')
    def test_snapshot_rows_golden_and_latency(self):
        from eval.who_report import report
        with redirect_stdout(io.StringIO()):
            result = report(Path(os.environ['SKALD_SNAPSHOT']))
        self.assertTrue(result['both_rows_identical'])
        from snapshot_support import snapshot_people
        person = snapshot_people(Path(os.environ['SKALD_SNAPSHOT']))[1]
        self.assertGreaterEqual(result['people'][person]['foryou_mean'], result['foryou_mean'])
        self.assertLessEqual(result['p95_ms'][person]-result['p95_ms']['none'], 25)

    def test_fixture_both_off_golden(self):
        from eval.who_report import both_sections
        saved = json.loads((FIXTURE / 'golden.json').read_text())
        self.assertEqual({k: dump(v, k == 'hashes') for k, v in saved.items()}, both_sections(ROOT / 'server.py'))


if __name__ == '__main__':
    unittest.main()
