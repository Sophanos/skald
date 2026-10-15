"""Synthetic Odin database and bd1de5b Jellyfin responses, without sockets."""
from contextlib import nullcontext, redirect_stdout
import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from decide_kit import close_server, load_server
from history_kit import NOW, Reply
from skald import members


def packed(kind, base, season=65535, episode=65535):
    code, number = (1, int(base[2:])) if base.startswith('tt') else (2, int(base[5:]))
    return (bytes((161, {'movie': 1, 'series': 2, 'episode': 4}[kind]*16+code,
                   1 if kind == 'movie' else 2)) + number.to_bytes(6, 'big') +
            season.to_bytes(2, 'big') + episode.to_bytes(2, 'big') + bytes(3)).hex()


class Plays(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.cfg = members.fresh_config(['ana', 'ben', 'kim', 'marko'])
        self.write_config()
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.addCleanup(members.configure)
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '',
                                                'SKALD_SNAPSHOT_U': ''}))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, '_connector_open', side_effect=AssertionError('network forbidden')))

    def write_config(self):
        (self.data/'skald.json').write_text(json.dumps(self.cfg))

    def database(self):
        path = self.data/'watch.sqlite'
        self.rows = [
            ('a1b2c3d4', 'movie', 'tmdb:990001', int(NOW*1000), 1, 'movie', 1, 100, 100, None, None, 0, None, None),
            ('a1b2c3d4', 'series', 'tt0990002', int(NOW*1000)-120000, 1, 'episode', 0, 100, 100, None, None, 0, 1, 1),
            ('a1b2c3d4', 'series', 'tt0990002', int(NOW*1000)+120000, 0, 'episode', 0, 100, 40, None, None, 0, 1, 2),
            ('b1b2c3d4', 'movie', 'tmdb:990003', None, 0, 'movie', 1, 0, 0, None, None, 0, None, None),
        ] + [('a1b2c3d4', 'series', 'tmdb:990004', int(NOW*1000)-600000, 1, 'episode',
              0, 100, 100, None, None, 0, 1, ep) for ep in range(1, 6)]
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE watch_state (uuid TEXT, media_type TEXT, base_id TEXT, '
                        'last_played_at INTEGER, played INTEGER, kind TEXT, favorite INTEGER, '
                        'duration_ms INTEGER, position_ms INTEGER, rating INTEGER, likes INTEGER, '
                        'dropped INTEGER, season INTEGER, episode INTEGER)')
            con.executemany('INSERT INTO watch_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', self.rows)
        self.cfg['history']['source'] = 'aiostreams_db'
        self.cfg['connectors']['aiostreams_db'] = {'kind': 'player', 'enabled': True, 'auth': None,
            'options': {'path': str(path), 'profiles': {'a1b2c3d4': 'ana', 'b1b2c3d4': 'ben'}}}
        self.s.CONFIG = self.s.validate_config(self.cfg)
        self.write_config()

    def test_plays_config_defaults_validation_and_old_loader(self):
        from skald import plays
        self.assertEqual(plays.settings(self.cfg), dict(mode='auto', every=900, people={}, live=False))
        self.cfg['plays'] = dict(mode='https', every=300, people={'ben': False}, live=False)
        self.assertEqual(members.validate_config(self.cfg)['plays'], self.cfg['plays'])
        self.assertEqual(members.public_config(members.validate_config(self.cfg))['plays'], self.cfg['plays'])
        try:
            old = subprocess.check_output(['git', 'show', '54a5c0f:server.py'], text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('the 1.4.10 source (54a5c0f) is not in this checkout')
        section = old.split('# --- household config (also loaded alone by skald/members.py) ---')[1].split('# --- end household config ---')[0]
        scope = dict(vars(members))
        exec(compile(section, 'old-config', 'exec'), scope)
        self.assertEqual(scope['validate_config'](self.cfg)['plays'], self.cfg['plays'])
        for value in ({'mode': 'push'}, {'every': 299}, {'every': 21601}, {'every': True},
                      {'people': {'kim': 0}}, {'people': {'ANA': False}}, {'live': True}, {'token': 'fake'}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'plays'):
                members.validate_config({**self.cfg, 'plays': value})
        for text, seconds in (('5m', 300), ('15m', 900), ('1h', 3600), ('6h', 21600)):
            self.assertEqual(plays.duration(text), seconds)
        for text in ('4m', '7h', '0m', '-5m', '5.5m', '300', '', None):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '5m.*6h'):
                plays.duration(text)

    def test_auto_prefers_db_and_preserves_bytes(self):
        from skald import plays
        self.database()
        before = json.dumps(self.s._aiostreams_db(self.s.CONFIG['connectors']['aiostreams_db']['options'])).encode()
        state = plays.check(self.s, NOW)
        self.assertEqual(state['path'], 'db')
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertEqual(state['next_check'], NOW+900)
        self.assertEqual(plays.status(self.s)['new_plays']['ana'], 2)

    def test_not_connected_and_off_do_not_read(self):
        from skald import plays
        state = plays.status(self.s)
        self.assertEqual(state['path'], 'not connected')
        self.assertEqual(state['fix'], 'skald setup aiostreams')
        self.s.CONFIG['plays'] = {'mode': 'off'}
        with patch.object(self.s.sqlite3, 'connect', side_effect=AssertionError('DB read')):
            self.assertEqual(plays.check(self.s, NOW)['path'], 'off')

    def test_db_person_off_is_excluded_in_query(self):
        from skald import plays
        self.database()
        self.s.CONFIG['plays'] = {'people': {'ana': False}}
        plays.check(self.s, NOW)
        self.assertEqual([x['profile'] for x in self.s.read_history()], ['ben'])

    def https(self):
        self.canary = 'FAKE_PASSWORD_CANARY'
        self.token = 'FAKE_ACCESS_TOKEN_CANARY'
        (self.data/'credentials').mkdir(exist_ok=True, mode=0o700)
        (self.data/'credentials/aiostreams.json').write_text(json.dumps({
            person: dict(uuid=prefix+'-0000-0000-0000-000000000000', password=self.canary, member=person)
            for person, prefix in (('ana', 'a1b2c3d4'), ('ben', 'b1b2c3d4'))}))
        (self.data/'setup.json').write_text(json.dumps({'aio_url': 'https://aio.invalid'}))
        self.calls = []
        self.http_rows = {}
        for r in self.rows:
            prefix, typ, base, at, played, kind, favorite, duration, position, rating, likes, dropped, season, episode = r
            item = dict(Id=packed(kind, base, season if season is not None else 65535, episode if episode is not None else 65535),
                        Type='Movie' if typ == 'movie' else 'Episode', RunTimeTicks=duration*10000,
                        ProviderIds={'Tmdb': base[5:]} if base.startswith('tmdb:') else {'Imdb': base},
                        UserData=dict(Played=bool(played), PlayCount=int(bool(played)), IsFavorite=bool(favorite),
                                      PlaybackPositionTicks=position*10000))
            if at:
                item['UserData']['LastPlayedDate'] = datetime.datetime.fromtimestamp(at/1000, datetime.timezone.utc).isoformat()
            if typ == 'series':
                item.update(SeriesId=packed('series', base), ParentIndexNumber=season, IndexNumber=episode)
            self.http_rows.setdefault(prefix, []).append(item)
        self.enterContext(patch.object(self.s, '_connector_open', side_effect=self.transport))

    def transport(self, req, timeout):
        url = urllib.parse.urlsplit(req.full_url)
        self.calls.append(url.path+'?'+url.query)
        self.assertNotIn(self.canary, req.full_url)
        self.assertNotIn(self.token, req.full_url)
        if url.path.endswith('/AuthenticateByName'):
            self.assertEqual(json.loads(req.data)['Pw'], self.canary)
            uid = json.loads(req.data)['Username'].replace('-', '')
            return Reply({'User': {'Id': uid}, 'AccessToken': self.token})
        self.assertEqual(req.get_header('X-emby-token'), self.token)
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query['SortBy'], ['DatePlayed'])
        self.assertEqual(query['SortOrder'], ['Descending'])
        rows = self.http_rows.get(url.path.split('/')[3][:8], [])
        filtered = query['Filters'][0]
        rows = [r for r in rows if (r['UserData']['Played'] if filtered == 'IsPlayed' else
                r['UserData']['PlaybackPositionTicks'] > 0 and not r['UserData']['Played'] if filtered == 'IsResumable' else
                r['UserData']['IsFavorite'])]
        rows.sort(key=lambda r: r['UserData'].get('LastPlayedDate', ''), reverse=True)
        start, limit = int(query['StartIndex'][0]), int(query['Limit'][0])
        return Reply(dict(Items=rows[start:start+limit], TotalRecordCount=len(rows), StartIndex=start))

    def test_child_watching_days_survive_https_raw_retention(self):
        from skald import plays
        self.database()
        self.https()
        self.s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 9}
        self.s.CONFIG['plays'] = {'mode': 'https'}
        self.s.CONFIG['history']['max_raw_items'] = 0
        plays.check(self.s, NOW)
        self.assertEqual(plays.days_state(self.data), {'ana': [plays.logical_day(self.s.CONFIG, NOW).isoformat()]})
        self.assertEqual(plays.state(self.s)['records'], [])
        self.assertEqual(plays.state(self.s)['https']['ana']['rows'], [])

    def test_child_backfill_never_creates_watching_day(self):
        from skald import plays
        self.database()
        self.rows = self.rows[-5:]
        self.replace_db(self.rows)
        self.https()
        for i, row in enumerate(self.http_rows['a1b2c3d4'][1:]):
            row['Id'] = 'b2'+f'{i:030x}'
        self.s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 9}
        for mode in ('https', 'db'):
            for retention in ({}, {'max_raw_items': 0}, {'max_raw_items': 1}, {'keep_raw_days': 0}):
                with self.subTest(mode=mode, retention=retention), patch.object(self.s.time, 'time', return_value=NOW):
                    for name in ('plays.json', plays.WATCHING_DAYS):
                        (self.data/name).unlink(missing_ok=True)
                    self.s.CONFIG['plays'] = {'mode': mode}
                    self.s.CONFIG['history'] = {'source': 'aiostreams_db', **retention}
                    for at in (NOW, NOW+900):
                        self.assertEqual(plays.check(self.s, at)['path'], mode)
                        self.assertEqual(plays.days_state(self.data), {})
                        self.assertEqual(plays.watching_days(self.s), {})

    def test_https_parity_switch_dedup_and_compare_no_writes(self):
        from skald import plays
        self.database()
        self.https()
        expected = self.s._aiostreams_db(self.s.CONFIG['connectors']['aiostreams_db']['options'])
        self.assertEqual(plays.https(self.s)[0], expected)
        plays.check(self.s, NOW)
        self.s.CONFIG['plays'] = {'mode': 'https'}
        result = plays.check(self.s, NOW+1)
        self.assertEqual(result['path'], 'https')
        self.assertEqual(json.loads((self.data/'history.json').read_text()), expected)
        self.assertEqual(sum(result['new_plays'].values()), 0)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.rglob('*') if p.is_file()}
        self.assertTrue(plays.compare(self.s)['equal'])
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.rglob('*') if p.is_file()}
        self.assertEqual(after, before)
        self.s.CONFIG['plays']['mode'] = 'db'
        self.assertEqual(sum(plays.check(self.s, NOW+2)['new_plays'].values()), 0)
        self.assertEqual(json.loads((self.data/'history.json').read_text()), expected)
        output = json.dumps(plays.status(self.s))
        self.assertNotIn(self.canary, output)
        self.assertNotIn(self.token, output)
        self.assertNotIn('a1b2c3d4-0000', output)

    def test_auto_falls_back_on_missing_or_bad_schema_and_skips_off_person(self):
        from skald import plays
        self.database()
        self.https()
        (self.data/'watch.sqlite').unlink()
        self.s.CONFIG['plays'] = {'people': {'ben': False}}
        self.assertEqual(plays.check(self.s, NOW)['path'], 'https')
        self.assertFalse(any('b1b2c3d4' in call for call in self.calls))
        self.assertFalse(any(it['profile'] == 'ben' for it in self.s.read_history()))
        with sqlite3.connect(self.data/'watch.sqlite') as con:
            con.execute('CREATE TABLE watch_state (uuid TEXT)')
        self.assertIn('SchemaMismatch', plays.status(self.s)['reason'])

    def test_https_incremental_paging_keeps_boundary_minute_for_backfill(self):
        from skald import plays
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https', 'people': {'ben': False}}
        movie = self.http_rows['a1b2c3d4'][0]
        self.http_rows['a1b2c3d4'] = [dict(movie, Id=packed('movie', 'tmdb:'+str(991000+i)),
            UserData={**movie['UserData'], 'LastPlayedDate': datetime.datetime.fromtimestamp(
                NOW-i*120, datetime.timezone.utc).isoformat()}) for i in range(205)]
        plays.check(self.s, NOW)
        self.assertTrue(any('StartIndex=200' in call for call in self.calls))
        self.calls.clear()
        plays.check(self.s, NOW+900)
        self.assertFalse(any('Filters=IsPlayed' in call and 'StartIndex=100' in call for call in self.calls))
        self.assertEqual(sum(plays.status(self.s)['new_plays'].values()), 0)

    def test_https_failure_redacts_secrets_and_preserves_last_good(self):
        from skald import plays
        self.database()
        self.https()
        plays.check(self.s, NOW)
        before = (self.data/'history.json').read_bytes()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        with patch.object(self.s, '_connector_open', side_effect=OSError(self.canary+self.token)):
            result = plays.check(self.s, NOW+1)
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertEqual(result['path'], 'not connected')
        self.assertNotIn(self.canary, json.dumps(result))
        self.assertNotIn(self.token, json.dumps(result))

    def test_scheduler_uses_one_reader_and_fixed_interval(self):
        from skald import plays
        self.database()
        self.s.CONFIG['plays'] = {'every': 300}
        seeds = self.s.connector_seeds(NOW)
        self.assertEqual(seeds['aiostreams_db']['health']['state'], 'ok')
        self.assertEqual(seeds['player']['health']['state'], 'disabled')
        self.assertEqual(self.s.connector_next(seeds['aiostreams_db'], False, NOW), (300, NOW+300))
        self.cfg['history']['source'] = 'none'
        self.cfg['connectors'] = {}
        self.s.CONFIG = {**self.cfg, 'plays': {'mode': 'https'}}
        seeds = self.s.connector_seeds(NOW)
        self.assertEqual(seeds['player']['health']['state'], 'ok')
        self.assertIn('player', self.s.CONNECTOR_POLL)
        self.assertTrue(self.s.player_history_active())

    def test_demand_does_not_accelerate_odin_polling(self):
        self.database()
        self.s.connectors_load(NOW)
        c = self.s._connectors['aiostreams_db']
        c.update(last_sync=NOW, next_due=NOW+900)
        self.s.connectors_demand(NOW+300)
        self.assertEqual(c['next_due'], NOW+900)

    def test_cli_status_settings_check_compare_and_json(self):
        from skald import setup, plays
        from skald.__main__ import main
        self.database()
        self.https()
        with patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)):
            for args in ([], ['mode', 'https'], ['every', '5m'], ['person', 'ben', 'off'], ['check'], ['compare']):
                with self.subTest(args=args):
                    output = io.StringIO()
                    diagnostics = io.StringIO()
                    with redirect_stdout(diagnostics):
                        self.assertEqual(main(['plays', *args, '--json'], out=output), 0)
                    self.assertEqual(diagnostics.getvalue(), '')
                    self.assertIsInstance(json.loads(output.getvalue()), dict)
                    self.assertNotIn(self.canary, output.getvalue())
                    self.assertNotIn(self.token, output.getvalue())
            output = io.StringIO()
            self.assertEqual(main(['plays'], out=output), 0)
            self.assertIn('https', output.getvalue())
        saved = json.loads((self.data/'skald.json').read_text())
        self.assertEqual(saved['plays'], {'mode': 'https', 'every': 300, 'people': {'ben': False}, 'live': False})

    def answer(self, op, values=(), person=None):
        return dict(op=op, kind=None, values=list(values), person=person)

    def test_person_switch_without_odin_preserves_history_source_cli_and_commander(self):
        from skald import commands, setup
        from skald.__main__ import main
        for source in ('file', 'jellyfin', 'plex', 'none'):
            for value in ('off', 'on'):
                for surface in ('cli', 'commander'):
                    with self.subTest(source=source, value=value, surface=surface):
                        self.cfg = members.fresh_config(['ana', 'ben'])
                        self.cfg['history']['source'] = source
                        if source in ('jellyfin', 'plex'):
                            options = {'url': 'https://example.invalid', 'profile': 'household'}
                            if source == 'jellyfin':
                                options['user_id'] = 'synthetic-user'
                            self.cfg['connectors'][source] = dict(kind='player', enabled=True,
                                auth='file:synthetic.json', options=options)
                        self.s.CONFIG = self.s.validate_config(self.cfg)
                        self.write_config()
                        self.s._connector_write('history', {'items': [dict(profile='household', type='movie',
                            id='tmdb:990150', played=True, at=1000)]})
                        seeds = self.s.connector_seeds(NOW)
                        before = {p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
                        if surface == 'cli':
                            output = io.StringIO()
                            with patch.object(setup, 'server', return_value=nullcontext(self.s)):
                                self.assertEqual(main(['plays', 'person', 'ana', value, '--json'], out=output), 2)
                            self.assertEqual(json.loads(output.getvalue()), {'error': 'Odin plays are not set up.'})
                        else:
                            with patch.object(commands, 'context', return_value=('synthetic', None, 'ana')), \
                                    patch.object(self.s, 'decide', return_value={'answer': self.answer('plays_person', [value])}):
                                card = commands.special(self.s, 'stop reading my Odin watches', 'movie', routed=True)
                            self.assertEqual(card['metas'][0]['description'], 'Odin plays are not set up.')
                            self.assertIsNone(commands.get_pending(self.s, 'synthetic'))
                        self.assertEqual({p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}, before)
                        self.assertEqual(self.s.CONFIG['history']['source'], source)
                        self.assertNotIn('plays', self.s.CONFIG)
                        self.assertEqual(self.s.connector_seeds(NOW), seeds)

    def test_commander_person_switch_without_odin_uses_reviewed_reply(self):
        from decide_kit import ReplayLLM
        from eval.commands_eval import load_cases
        from skald import commands
        before = (self.data/'skald.json').read_bytes()
        for case in load_cases():
            if not case['id'].startswith('plays-self-'):
                continue
            with self.subTest(case=case['id']), ReplayLLM(self.s) as replay, \
                    patch.object(commands, 'context', return_value=('synthetic', None, 'ana')):
                replay.arm(case['recorded'])
                card = commands.special(self.s, case['text'], 'movie', routed=True)
                if case['expect']['op'] is None:
                    self.assertEqual(card['metas'][0]['description'], 'Skald cannot set this yet')
                else:
                    self.assertEqual(card['metas'][0]['description'], 'Odin plays are not set up.')
                self.assertIsNone(commands.get_pending(self.s, 'synthetic'))
                self.assertEqual((self.data/'skald.json').read_bytes(), before)
                self.assertNotIn('plays', self.s.CONFIG)

    def test_commander_changes_preview_apply_undo_and_kid_permissions(self):
        from skald import commands
        self.database()
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 12}
        self.write_config()
        self.s.refresh_preferences()
        for op, values, person in (('plays_mode', ['https'], None), ('plays_every', ['1h'], None),
                                   ('plays_person', ['off'], 'ben')):
            with self.subTest(op=op):
                before = (self.data/'skald.json').read_bytes()
                plan = commands.preview(self.s, '', 'ana', answer=self.answer(op, values, person))
                self.assertIn('plan', plan)
                self.assertEqual((self.data/'skald.json').read_bytes(), before)
                commands.apply(self.s, plan)
                self.assertIn('plays', json.loads((self.data/'skald.json').read_text()))
                commands.undo(self.s, ['ana'], actor='ana')
                self.assertNotIn('plays', json.loads((self.data/'skald.json').read_text()))
                with self.assertRaises(PermissionError):
                    commands.preview(self.s, '', 'kim', answer=self.answer(op, values, person))

    def test_commander_recent_privacy_in_one_helper(self):
        from skald import commands, plays
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 12}
        self.write_config()
        self.s.refresh_preferences()
        items = [dict(profile=p, type='movie', id='tmdb:'+str(992000+i), at=int(NOW*1000), played=True)
                 for i, p in enumerate(('ana', 'ben', 'kim', 'household', 'household'))]
        items[3]['history_by'] = ['ana', 'kim']
        items[4]['history_by'] = ['ana', 'ben']
        self.s._connector_write('history', {'items': items})
        with patch.object(self.s.time, 'time', return_value=NOW), patch.object(plays, 'may_see', wraps=plays.may_see) as visibility:
            plan = commands.preview(self.s, '', 'kim', answer=self.answer('plays_recent'))
        self.assertTrue(plan['read_only'])
        self.assertEqual([it['id'] for it in plan['plays']['items']], ['tmdb:992002', 'tmdb:992003'])
        self.assertEqual(visibility.call_count, len(items))
        self.assertNotIn('ben', json.dumps(plan))
        with self.assertRaises(PermissionError):
            commands.preview(self.s, '', 'kim', answer=self.answer('plays_recent', person='ana'))
        self.assertFalse(plays.may_see(self.s, self.s.GUEST, items[0]))

    def test_commander_status_and_check_have_no_preview_and_household_limit(self):
        from skald import commands, plays
        with patch.object(self.s.time, 'time', return_value=NOW), patch.object(self.s, 'plays_queue_check') as reader:
            status = commands.preview(self.s, '', 'ana', answer=self.answer('plays_status'))
            self.assertTrue(status['read_only'])
            self.assertEqual(reader.call_count, 0)
            first = commands.preview(self.s, '', 'ana', answer=self.answer('plays_check'))
            second = commands.preview(self.s, '', 'ben', answer=self.answer('plays_check'))
            self.assertTrue(first['read_only'])
            self.assertTrue(second['plays']['rate_limited'])
            self.assertEqual(reader.call_count, 1)
        with patch.object(self.s.time, 'time', return_value=NOW+120):
            self.assertFalse(commands.preview(self.s, '', 'ben', answer=self.answer('plays_check'))['plays'].get('rate_limited', False))

    def test_commander_uses_llm_for_plays_intent(self):
        from decide_kit import ReplayLLM, reset
        from skald import commands
        answer = self.answer('plays_status')
        reset(self.s)
        with ReplayLLM(self.s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = commands.preview(self.s, 'is Skald seeing what I watch?', 'ana')
            self.assertEqual(replay.calls, 1)
        self.assertTrue(plan['read_only'])

    def test_commander_can_switch_self_and_reject_stale_apply_or_undo(self):
        from skald import commands, plays
        self.database()
        plan = commands.preview(self.s, '', 'ana', answer=self.answer('plays_person', ['off']))
        self.assertFalse(plan['plays']['after']['people']['ana'])
        plays.change(self.s, 'every', '1h')
        with self.assertRaisesRegex(ValueError, 'changed'):
            commands.apply(self.s, plan)
        plan = commands.preview(self.s, '', 'ana', answer=self.answer('plays_mode', ['db']))
        commands.apply(self.s, plan)
        plays.change(self.s, 'mode', 'off')
        with self.assertRaisesRegex(ValueError, 'changed'):
            commands.undo(self.s, ['ana'], actor='ana')

    def test_incremental_same_minute_backfill_and_duplicate_keys(self):
        from skald import plays
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https', 'people': {'ben': False}}
        burst = [r for r in self.http_rows['a1b2c3d4'] if r['Id'] == packed('episode', 'tmdb:990004', 1, r.get('IndexNumber', 0))]
        self.http_rows['a1b2c3d4'] = burst[:3]
        plays.check(self.s, NOW)
        self.assertIsNotNone(self.s.read_history()[0]['at'])
        self.http_rows['a1b2c3d4'] = burst + [burst[0]]
        plays.check(self.s, NOW+900)
        item = self.s.read_history()[0]
        self.assertTrue(item['backfilled'])
        self.assertIsNone(item['at'])
        self.assertEqual(item['episodes_played'], 5)
        self.assertEqual(item['entries'], 5)

    def test_https_backfill_flag_survives_partial_minute_retention(self):
        from skald import plays
        self.database()
        at = NOW//60*60+40
        self.rows = [[*r[:3], int((at-(30 if i < 4 else 0))*1000), *r[4:]]
                     for i, r in enumerate(self.rows[-5:])]
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        burst = self.http_rows['a1b2c3d4'][:]
        for maximum in (None, 1):
            with self.subTest(max_raw_items=maximum):
                (self.data/'plays.json').unlink(missing_ok=True)
                self.s.CONFIG['history'] = dict(source='aiostreams_db', keep_raw_days=1, max_raw_items=maximum)
                self.http_rows['a1b2c3d4'] = burst[:]
                self.assertEqual(plays.check(self.s, at)['path'], 'https')
                self.http_rows['a1b2c3d4'] = burst[-1:]
                self.assertEqual(plays.check(self.s, at+self.s.DAY-10)['path'], 'https')
                records = plays.state(self.s)['records']
                self.assertEqual(len(records), 1)
                self.assertTrue(all(r['backfilled'] for r in records))

    def test_opaque_ids_are_reported_without_guessing_from_provider_ids(self):
        from skald import plays
        self.database()
        self.https()
        row = self.http_rows['a1b2c3d4'][0]
        row['Id'] = 'b2'+'ab'*15
        self.s.CONFIG['plays'] = {'mode': 'https'}
        result = plays.check(self.s, NOW)
        self.assertEqual(result['unmatched_ids'], 1)
        self.assertFalse(any(it['id'] == 'tmdb:990001' for it in self.s.read_history()))
        self.assertFalse(plays.compare(self.s)['equal'])

    def test_https_budgets_preserve_history_and_tokens_are_never_saved(self):
        from skald import plays
        self.database()
        self.https()
        plays.check(self.s, NOW)
        before = (self.data/'history.json').read_bytes()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        for caps in ({'CONNECTOR_HTTP_PAGES': 1}, {'CONNECTOR_HTTP_BYTES': 10}):
            with patch.multiple(self.s, **caps):
                result = plays.check(self.s, NOW+1)
            self.assertEqual(result['path'], 'not connected')
            self.assertEqual((self.data/'history.json').read_bytes(), before)
        for p in self.data.rglob('*.json'):
            if p.name != 'aiostreams.json':
                self.assertNotIn(self.canary, p.read_text())
                self.assertNotIn(self.token, p.read_text())

    def test_db_mode_never_falls_back_and_https_status_preserves_date_errors(self):
        from skald import plays
        self.database()
        self.https()
        (self.data/'watch.sqlite').unlink()
        self.s.CONFIG['plays'] = {'mode': 'db'}
        self.assertEqual(plays.check(self.s, NOW)['path'], 'not connected')
        self.assertEqual(self.calls, [])
        self.s.CONFIG['plays']['mode'] = 'https'
        self.http_rows['a1b2c3d4'][0]['UserData']['LastPlayedDate'] = self.canary
        self.assertEqual(plays.check(self.s, NOW+1)['path'], 'not connected')
        self.assertEqual(plays.status(self.s)['path'], 'not connected')
        self.assertNotIn(self.canary, json.dumps(plays.status(self.s)))

    def test_compare_exposes_lossy_jellyfin_fields(self):
        from skald import plays
        self.database()
        self.https()
        with sqlite3.connect(self.data/'watch.sqlite') as con:
            con.execute('UPDATE watch_state SET rating=9 WHERE base_id=?', ('tmdb:990001',))
        self.http_rows['a1b2c3d4'][0]['UserData'].update(Rating=9, Likes=True)
        result = plays.compare(self.s)
        self.assertFalse(result['equal'])
        self.assertEqual(result['differences']['ana'][0]['db']['likes'], None)
        self.assertEqual(result['differences']['ana'][0]['https']['likes'], 1)
        self.assertIn('tmdb:990001', plays.text(result))

    def test_explicit_attribution_overrides_the_original_profile_for_privacy(self):
        from skald import plays, history, metrics
        item = dict(profile='kim', history_by=['ana'], type='movie', id='tmdb:996001',
                    played=True, at=int(NOW*1000))
        self.assertFalse(plays.may_see(self.s, 'kim', item))
        self.assertTrue(plays.may_see(self.s, 'ana', item))
        item.pop('history_by')
        self.assertEqual(history.owners(item, self.s.CONFIG, windows=[{'t': NOW-1, 'until': NOW+1, 'who': 'ana'}]), ['kim'])
        self.s.CONFIG['plays'] = {'mode': 'https'}
        self.s._connector_write('history', {'items': [item]})
        collected, _ = metrics.collect(self.s, NOW)
        self.assertEqual([p['person'] for p in collected], ['kim'])

    def test_capped_source_preserves_history_and_resumable_totals_can_grow(self):
        from skald import plays
        self.database()
        self.https()
        plays.check(self.s, NOW)
        before = (self.data/'history.json').read_bytes()
        self.s.CONFIG['plays'] = {'mode': 'https', 'people': {'ben': False}}
        movie = self.http_rows['a1b2c3d4'][0]
        self.http_rows['a1b2c3d4'] = [dict(movie, Id=packed('movie', 'tmdb:'+str(994000+i))) for i in range(500)]
        self.assertEqual(plays.check(self.s, NOW+1)['path'], 'not connected')
        self.assertEqual((self.data/'history.json').read_bytes(), before)
        self.assertFalse(plays.compare(self.s)['equal'])
        self.calls.clear()
        self.http_rows['a1b2c3d4'] = [dict(movie, Id=packed('movie', 'tmdb:'+str(994000+i)),
            UserData={**movie['UserData'], 'Played': False, 'PlaybackPositionTicks': 400000}) for i in range(205)]
        def growing_totals(req, timeout):
            response = self.transport(req, timeout)
            page = json.loads(response.read())
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            if query.get('Filters') == ['IsResumable']:
                page['TotalRecordCount'] = min(205, int(query['StartIndex'][0])+100)
            return Reply(page)
        with patch.object(self.s, '_connector_open', side_effect=growing_totals):
            self.assertEqual(plays.check(self.s, NOW+2)['path'], 'https')
        self.assertTrue(any('Filters=IsResumable' in call and 'StartIndex=200' in call for call in self.calls))

    def test_search_confirmation_uses_existing_yes_and_undo_flow(self):
        from skald import commands
        plan = commands.preview(self.s, '', 'ana', answer=self.answer('plays_mode', ['off']))
        with patch.object(self.s, 'who_watching', return_value=('ana', None)):
            commands.pending(self.s)[None] = (self.s.time.time(), plan)
            result = commands.special(self.s, 'yes '+commands.confirm_code(plan), 'movie')
            self.assertIn('Done:', result['metas'][0]['description'])
            self.assertEqual(self.s.CONFIG['plays']['mode'], 'off')
            commands.special(self.s, 'undo', 'movie')
            self.assertNotIn('plays', self.s.CONFIG)

    def test_existing_aiostreams_probe_is_read_only(self):
        from skald import connectors
        self.database()
        before = {p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        result = connectors.test(self.s, 'aiostreams', now=NOW)
        self.assertTrue(result['connectors'][0]['ok'])
        self.assertEqual({p: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}, before)

    def test_status_next_check_tracks_interval_and_off_changes(self):
        from skald import plays
        self.database()
        plays.check(self.s, NOW)
        self.s.CONFIG['plays'] = {'every': 300}
        self.assertEqual(plays.status(self.s)['next_check'], NOW+300)
        self.s.CONFIG['plays']['mode'] = 'off'
        self.assertIsNone(plays.status(self.s)['next_check'])

    def test_auto_notices_saved_logins_without_a_restart(self):
        self.database()
        self.s.CONFIG['history']['source'] = 'none'
        self.s.CONFIG['connectors'] = {}
        self.s.connectors_load(NOW)
        self.assertEqual(self.s._connectors['player']['health']['state'], 'disabled')
        self.https()
        with patch.object(self.s, '_connector_async', return_value=None):
            self.s.connectors_tick(NOW)
        self.assertEqual(self.s._connectors['player']['health']['state'], 'ok')

    def test_legacy_household_and_together_logins_dedup_by_person(self):
        from skald import plays
        self.database()
        self.https()
        path = self.data/'credentials/aiostreams.json'
        saved = json.loads(path.read_text())
        for person, prefix in (('household', 'c1b2c3d4'), ('together', 'd1b2c3d4')):
            saved[person] = dict(uuid=prefix+'-0000-0000-0000-000000000000', password=self.canary)
            self.http_rows[prefix] = [self.http_rows['a1b2c3d4'][1]]
        path.write_text(json.dumps(saved))
        items = [it for it in plays.https(self.s)[0]['items'] if it['profile'] == 'household']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['episodes_played'], 1)

    def replace_db(self, rows):
        with sqlite3.connect(self.data/'watch.sqlite') as con:
            con.execute('DELETE FROM watch_state')
            con.executemany('INSERT INTO watch_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)

    def test_conflicting_login_is_skipped_on_both_paths_and_compare(self):
        from skald import plays
        self.database()
        self.https()
        path = self.data/'credentials/aiostreams.json'
        saved = json.loads(path.read_text())
        saved['kim'] = saved.pop('ana')
        path.write_text(json.dumps(saved))
        for mode in ('db', 'https'):
            self.s.CONFIG['plays'] = {'mode': mode}
            result = plays.check(self.s, NOW)
            self.assertFalse(any(it['profile'] in ('ana', 'kim', 'household') for it in self.s.read_history()))
            self.assertEqual([r for r in result['mappings'] if r['login'] == 'a1b2c3d4'], [
                dict(login='a1b2c3d4', person=None, paths=['db', 'https'], status='conflict')])
        compared = plays.compare(self.s)
        self.assertEqual(compared['differences']['ana'], [])
        self.assertEqual(compared['differences']['kim'], [])
        self.assertFalse(any('a1b2c3d4' in call for call in self.calls))

    def test_unmapped_prefixes_are_reported_on_both_paths(self):
        from skald import plays
        self.database()
        self.https()
        self.replace_db([('c1b2c3d4-device', *self.rows[0][1:])])
        path = self.data/'credentials/aiostreams.json'
        saved = json.loads(path.read_text())
        saved['unknown'] = dict(uuid='d1b2c3d4-0000-0000-0000-000000000000', password=self.canary)
        path.write_text(json.dumps(saved))
        for mode in ('db', 'https'):
            self.s.CONFIG['plays'] = {'mode': mode}
            report = plays.check(self.s, NOW)['mappings']
            self.assertIn(dict(login='c1b2c3d4', person=None, paths=['db'], status='unmapped'), report)
            self.assertIn(dict(login='d1b2c3d4', person=None, paths=['https'], status='unmapped'), report)
            self.assertFalse(any(it['profile'] == 'household' for it in self.s.read_history()))
        self.assertFalse(any('d1b2c3d4' in call for call in self.calls))

    def test_switch_paths_keeps_newest_episode_timestamp_at_second_precision(self):
        from skald import plays
        self.database()
        row = list(self.rows[1])
        row[3] = 1791734400123
        self.rows = [row]
        self.replace_db(self.rows)
        self.https()
        self.http_rows['a1b2c3d4'][0]['UserData']['LastPlayedDate'] = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).isoformat()
        counts = []
        for i, mode in enumerate(('db', 'https', 'db')):
            self.s.CONFIG['plays'] = {'mode': mode}
            counts.append(plays.check(self.s, NOW+i)['new_plays']['ana'])
            self.assertEqual(self.s.read_history()[0]['at'], 1791734400123)
        self.assertEqual(counts, [1, 0, 0])

    def test_only_played_traversal_advances_watermark(self):
        from skald import plays
        self.database()
        self.https()
        movie, resume = self.http_rows['a1b2c3d4'][0], self.http_rows['a1b2c3d4'][2]
        movie['UserData']['LastPlayedDate'] = '2026-10-11T11:58:00Z'
        resume['UserData']['LastPlayedDate'] = '2026-10-11T12:00:00Z'
        resume['UserData']['IsFavorite'] = True
        self.http_rows['a1b2c3d4'] = [movie, resume]
        _, cache, _ = plays.https(self.s)
        self.assertEqual(cache['ana']['seen'], int(self.s._connector_time('2026-10-11T11:58:00Z')*1000))
        finished = dict(movie, Id=packed('movie', 'tmdb:997001'), UserData={**movie['UserData'],
            'LastPlayedDate': '2026-10-11T11:59:59Z', 'IsFavorite': False})
        self.http_rows['a1b2c3d4'].append(finished)
        doc, _, _ = plays.https(self.s, cache)
        self.assertIn('tmdb:997001', [it['id'] for it in doc['items']])

    def test_opaque_episode_counts_toward_backfill_burst(self):
        from skald import plays
        self.database()
        self.rows = self.rows[-5:]
        self.replace_db(self.rows)
        self.https()
        self.http_rows['a1b2c3d4'][0]['Id'] = 'b2'+'ab'*15
        item = plays.https(self.s)[0]['items'][0]
        self.assertTrue(item.get('backfilled'))
        self.assertIsNone(item['at'])

    def test_forget_purges_cached_rows_and_prevents_recaching(self):
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        plays.check(self.s, NOW)
        history.forget(self.data, self.s.CONFIG, '2026-10-13', 'ana', apply=True)
        saved = plays.state(self.s)
        self.assertEqual(saved['https']['ana']['rows'], [])
        self.assertFalse(any(r['person'] == 'ana' for r in saved.get('records', [])))
        plays.check(self.s, NOW+900)
        self.assertEqual(plays.state(self.s)['https']['ana']['rows'], [])
        self.assertFalse(any(it['profile'] == 'ana' for it in self.s.read_history()))

    def test_leave_prunes_cutoff_day_cache_once_and_blocks_both_transports(self):
        from skald import commands, history, plays
        for mode in ('db', 'https'):
            with self.subTest(mode=mode):
                self.cfg = members.fresh_config(['ana', 'ben'])
                (self.data/'watch.sqlite').unlink(missing_ok=True)
                for name in ('plays.json', 'history.json', 'history-seen.json', 'history-forgotten.json'):
                    (self.data/name).unlink(missing_ok=True)
                self.database()
                cutoff_day = datetime.datetime(2026, 10, 12, 23, 59, 59, tzinfo=datetime.timezone.utc)
                at = int(cutoff_day.timestamp()*1000)
                row = list(self.rows[0])
                row[0], row[3] = 'c1b2c3d4', at
                after = [*row]
                after[2], after[3] = 'tmdb:990011', at+2000
                solo = [*after]
                solo[0], solo[2] = 'b1b2c3d4', 'tmdb:990012'
                cached_only = [*row]
                cached_only[2] = 'tmdb:990013'
                self.rows = [row, after, solo, cached_only]
                self.replace_db(self.rows)
                self.cfg['connectors']['aiostreams_db']['options']['profiles']['c1b2c3d4'] = 'household'
                self.cfg['plays'] = {'mode': mode}
                self.write_config()
                self.s.refresh_preferences()
                self.https()
                path = self.data/'credentials/aiostreams.json'
                accounts = json.loads(path.read_text())
                accounts['together'] = dict(uuid='c1b2c3d4-0000-0000-0000-000000000000', password=self.canary)
                path.write_text(json.dumps(accounts))
                plays.check(self.s, at/1000+10)
                doc = json.loads((self.data/'history.json').read_text())
                doc['items'] = [it for it in doc['items'] if it['id'] != cached_only[2]]
                (self.data/'history.json').write_text(json.dumps(doc))
                before = (self.data/'plays.json').read_bytes()
                with patch.object(self.s, 'local_now', return_value=cutoff_day.replace(tzinfo=None)):
                    plan = commands.preview(self.s, '', 'ana', answer={**self.answer('member_forget', person='ben'), 'shared': 'drop'})
                    plan['source'] = 'cli'
                    self.assertEqual(plan['forget']['counts']['history'], 3)
                    self.assertEqual(plan['forget']['counts']['shared'], {'ana': 2})
                    self.assertEqual((self.data/'plays.json').read_bytes(), before)
                    commands.apply(self.s, plan)
                saved = plays.state(self.s)
                self.assertEqual({r['row'][2] for r in saved['records']}, {after[2]})
                if mode == 'https':
                    self.assertEqual({r[2] for r in saved['https']['together']['rows']}, {after[2]})
                    self.assertNotIn('ben', saved['https'])
                self.assertEqual(history.cutoffs(self.data)['ben'], '2026-10-13')
                self.assertTrue({row[2], cached_only[2]} <= history.seen_keys(self.data))
                for next_mode in ('db', 'https'):
                    self.s.CONFIG['plays']['mode'] = next_mode
                    result = plays.check(self.s, at/1000+1000)
                    self.assertEqual(sum(result['new_plays'].values()), 0)
                    self.assertEqual({r['row'][2] for r in plays.state(self.s)['records']}, {after[2]})
                    self.assertEqual({it['id'] for it in self.s.read_history()}, {after[2]})
                def details(item):
                    return 'movie', dict(id=int(item['id'][5:]), title='Invented '+item['id'])
                with patch.object(self.s, 'tmdb_details', side_effect=details):
                    profile = self.s.build_profile(self.s.read_history(), [], remote=False)
                self.assertTrue({row[2], cached_only[2]} <= set(profile['seen']))
                self.assertEqual({e['key'] for e in profile['entries']}, {after[2]})
                personal = dict(profile='household', history_by=['ana'], type='movie', id=row[2], at=at, played=True)
                self.assertEqual(history.filter_forgotten(self.data, self.s.CONFIG, 'history.json', [personal]), [personal])

    def test_leave_prunes_old_episode_before_db_title_aggregation(self):
        from skald import commands, plays
        self.cfg = members.fresh_config(['ana', 'ben'])
        self.database()
        at = datetime.datetime(2026, 10, 12, 23, 59, 59)
        row = list(self.rows[1])
        row[0], row[2], row[3] = 'c1b2c3d4', 'tmdb:990014', int(at.replace(tzinfo=datetime.timezone.utc).timestamp()*1000)
        after = [*row]
        after[3], after[13] = row[3]+2000, 2
        self.replace_db([row, after])
        self.cfg['connectors']['aiostreams_db']['options']['profiles']['c1b2c3d4'] = 'household'
        self.write_config()
        self.s.refresh_preferences()
        plays.check(self.s, row[3]/1000+10)
        with patch.object(self.s, 'local_now', return_value=at):
            commands.apply(self.s, dict(commands.preview(self.s, '', 'ana', answer={**self.answer('member_forget', person='ben'), 'shared': 'drop'}), source='cli'))
        plays.check(self.s, row[3]/1000+1000)
        self.assertEqual(self.s.read_history()[0]['episodes_played'], 1)

    def test_resumable_completion_at_same_time_counts_once_on_both_paths(self):
        from skald import plays
        self.database()
        row = list(self.rows[0])
        row[4], row[8] = 0, 40
        self.rows = [row]
        self.replace_db(self.rows)
        self.https()
        for mode in ('db', 'https'):
            with self.subTest(mode=mode):
                (self.data/'plays.json').unlink(missing_ok=True)
                (self.data/'history.json').unlink(missing_ok=True)
                self.s.CONFIG['plays'] = {'mode': mode}
                row[4] = 0
                self.replace_db([row])
                self.http_rows['a1b2c3d4'][0]['UserData']['Played'] = False
                self.assertEqual(plays.check(self.s, NOW)['new_plays']['ana'], 0)
                row[4] = 1
                self.replace_db([row])
                self.http_rows['a1b2c3d4'][0]['UserData']['Played'] = True
                self.assertEqual(plays.check(self.s, NOW+900)['new_plays']['ana'], 1)
                self.assertEqual(plays.check(self.s, NOW+1800)['new_plays']['ana'], 0)

    def test_retention_purges_cache_even_without_history_events(self):
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        plays.check(self.s, NOW)
        self.s.tmdb_details = lambda it: None
        self.s.CONFIG['history']['keep_raw_days'] = 1
        self.s._connector_write('history', {'items': []})
        history.cleanup(self.s, NOW+2*self.s.DAY)
        self.assertTrue(all(not a['rows'] for a in plays.state(self.s)['https'].values()))
        self.assertEqual(plays.state(self.s).get('records', []), [])
        plays.check(self.s, NOW+2*self.s.DAY)
        self.assertTrue(all(not a['rows'] for a in plays.state(self.s)['https'].values()))

    def test_disabled_pending_connector_does_not_block_other_connectors(self):
        from concurrent.futures import Future
        self.database()
        self.s.CONFIG['plays'] = {'mode': 'off'}
        self.s.connectors_load(NOW)
        future = Future()
        future.set_running_or_notify_cancel()
        self.s._connector_pending = (self.s.DATA, 'aiostreams_db', future)
        self.s.connectors_tick(NOW)
        self.assertIsNotNone(self.s._connector_pending)
        future.set_result(([], [0, 0], {'events': 0, 'error': None}))
        self.s.connectors_tick(NOW+1)
        self.assertIsNone(self.s._connector_pending)
        other = self.s._connector('mdblist', 'api', 'both', NOW)
        with patch.object(self.s._connector_pool, 'submit', return_value=Future()) as submit:
            self.s._connector_async(other, NOW+2)
        submit.assert_called_once()

    def test_commander_queues_check_without_network_or_history_lock(self):
        from concurrent.futures import Future
        from skald import commands, plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        with patch.object(self.s.time, 'time', return_value=NOW), \
                patch.object(self.s._connector_pool, 'submit', return_value=Future()) as submit, \
                patch.object(plays, 'check', side_effect=AssertionError('synchronous poll')), \
                patch.object(history, 'locked', side_effect=AssertionError('history lock')):
            first = commands.preview(self.s, '', 'ana', answer=self.answer('plays_check'))
            second = commands.preview(self.s, '', 'ben', answer=self.answer('plays_check'))
        self.assertIn('Checking now, results in a minute', first['description'])
        self.assertTrue(second['plays']['rate_limited'])
        self.assertEqual(plays.state(self.s)['requested'], NOW)
        submit.assert_called_once()

    def test_https_retry_after_reaches_scheduler_without_secrets(self):
        from skald import plays
        from concurrent.futures import Future
        import urllib.error
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https', 'every': 300}
        self.s.connectors_load(NOW)
        c = self.s._connectors['aiostreams_db']
        error = urllib.error.HTTPError('https://'+self.canary, 429, self.token, {'Retry-After': '3600'}, None)
        with patch.object(self.s, '_connector_open', side_effect=error):
            result = self.s._connector_job(c, NOW)
        self.assertEqual(result[2].get('backoff_until'), NOW+3600)
        future = Future()
        future.set_result(result)
        self.s._connector_pending = (self.s.DATA, c['id'], future)
        self.s.connectors_tick(NOW)
        self.assertEqual(self.s._connectors[c['id']]['next_due'], NOW+3600)
        output = json.dumps(plays.status(self.s))+(self.data/'plays.json').read_text()
        self.assertNotIn(self.canary, output)
        self.assertNotIn(self.token, output)

    def test_retry_after_survives_reload_restart_and_queued_check(self):
        from concurrent.futures import Future
        from skald import commands, plays, setup
        import urllib.error
        self.database()
        self.https()
        self.enterContext(patch.object(self.s.time, 'time', return_value=NOW+1))
        self.cfg['plays'] = {'mode': 'https', 'every': 300}
        self.write_config()
        self.s.refresh_preferences()
        self.s.connectors_load(NOW)
        c = self.s._connectors['aiostreams_db']
        error = urllib.error.HTTPError('https://aio.invalid', 429, 'rate limited', {'Retry-After': '3600'}, None)
        with patch.object(self.s, '_connector_open', side_effect=error):
            result = self.s._connector_job(c, NOW)
        future = Future()
        future.set_result(result)
        self.s._connector_pending = (self.s.DATA, c['id'], future)
        c['next_due'] = NOW
        self.s.connectors_tick(NOW)
        self.assertEqual(plays.state(self.s)['backoff_until'], NOW+3600)
        self.cfg['plays']['every'] = 600
        setup.publish(self.data, self.cfg, replace=True)
        self.s.refresh_preferences()
        for server in (self.s, load_server(self.data)):
            with self.subTest(restarted=server is not self.s):
                if server is not self.s:
                    self.addCleanup(close_server, server)
                server.connectors_load(NOW+1)
                connector = server._connectors[c['id']]
                self.assertEqual(connector['next_due'], NOW+3600)
                (self.data/'plays.json').write_text(json.dumps({k: v for k, v in plays.state(server).items() if k != 'requested'}))
                with patch.object(server.time, 'time', return_value=NOW+1), \
                        patch.object(server._connector_pool, 'submit', return_value=Future()) as submit:
                    plan = commands.preview(server, '', 'ana', answer=self.answer('plays_check'))
                    self.assertTrue(plan['plays']['queued'])
                    self.assertEqual(plan['description'], 'Check queued for 2026-10-11 17:00:00 UTC (Retry-After backoff)')
                    self.assertEqual(connector['next_due'], NOW+3600)
                    server.connectors_tick(NOW+3599)
                    submit.assert_not_called()
                    server.connectors_tick(NOW+3600)
                    submit.assert_called_once()
                server.CONFIG['plays']['mode'] = 'off'
                server._connectors[c['id']]['health']['state'] = 'disabled'
                saved = plays.state(server)
                saved.pop('requested', None)
                (self.data/'plays.json').write_text(json.dumps(saved))
                with patch.object(server.time, 'time', return_value=NOW+1):
                    off = commands.preview(server, '', 'ana', answer=self.answer('plays_check'))
                self.assertIsNone(off['plays']['next_check'])

    def test_recent_requires_a_date_in_the_week_but_explain_keeps_undated(self):
        from skald import commands, plays
        self.database()
        rows = []
        for i, at in enumerate((None, int((NOW-7*self.s.DAY)*1000)-1,
                                int((NOW-7*self.s.DAY)*1000), int(NOW*1000), int(NOW*1000)+1, int(NOW*1000))):
            row = list(self.rows[0])
            row[2], row[3] = 'tmdb:'+str(990020+i), at
            rows.append(row)
        self.replace_db(rows)
        for cached in (False, True):
            with self.subTest(cached=cached), patch.object(self.s.time, 'time', return_value=NOW):
                if cached:
                    plays.check(self.s, NOW)
                    saved = plays.state(self.s)
                    for record in saved['records']:
                        record['backfilled'] = record['row'][2] == 'tmdb:990025'
                    (self.data/'plays.json').write_text(json.dumps(saved))
                else:
                    doc = plays.database(self.s, plays.db_options(self.s))
                    for item in doc['items']:
                        item['backfilled'] = item['id'] == 'tmdb:990025'
                    self.s._connector_write('history', doc)
                result = commands.preview(self.s, '', 'ana', answer=self.answer('plays_recent'))
                self.assertEqual({it['id'] for it in result['plays']['items']}, {'tmdb:990022', 'tmdb:990023'})
                explanation = commands.preview(self.s, '', 'ana', answer=self.answer('plays_explain', ['tmdb:990020']))
                self.assertEqual([it['at'] for it in explanation['plays']['items']], [None])
                explanation = commands.preview(self.s, '', 'ana', answer=self.answer('plays_explain', ['tmdb:990025']))
                self.assertEqual([it['at'] for it in explanation['plays']['items']], [None])

    def test_recent_why_cli_commander_privacy_and_provenance(self):
        from skald import plays, commands, setup
        from skald.__main__ import main
        self.database()
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 12}
        self.write_config()
        self.s.refresh_preferences()
        prefixes = ('a1b2c3d4', 'b1b2c3d4', 'c1b2c3d4', 'd1b2c3d4', 'e1b2c3d4')
        people = ('ana', 'ben', 'kim', 'marko', 'household')
        opts = self.s.CONFIG['connectors']['aiostreams_db']['options']
        opts['profiles'] = dict(zip(prefixes, people))
        rows = [(p, *self.rows[1][1:]) for p in prefixes]
        self.replace_db(rows)
        self.s._profiles['household'] = {'entries': [dict(key='tt0990002', title='Invented Lantern')]}
        plays.check(self.s, NOW)
        for actor in ('ana', 'ben', 'kim', 'marko'):
            with self.subTest(actor=actor), patch.object(self.s, 'GUEST', 'marko'), patch.object(self.s.time, 'time', return_value=NOW), \
                    patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)):
                expected = {actor} | ({'household'} if actor in ('ana', 'ben') else set())
                recent = plays.recent(self.s, actor)
                self.assertEqual({it['person'] for it in recent['items']}, expected)
                for it in recent['items']:
                    self.assertEqual((it['season'], it['episode'], it['source'], it['backfilled']), (1, 1, 'db', False))
                    self.assertEqual(len(it['login']), 8)
                for action, args, op in (('recent', ['--limit', '20'], 'plays_recent'),
                                         ('why', ['Invented Lantern'], 'plays_explain')):
                    out = io.StringIO()
                    self.assertEqual(main(['plays', action, *args, '--person', actor, '--json'], out=out), 0)
                    plan = commands.preview(self.s, '', actor, answer=self.answer(op, ['Invented Lantern'] if action == 'why' else []))
                    self.assertEqual(json.loads(out.getvalue()), plan['plays'])
                with self.assertRaises(PermissionError):
                    commands.preview(self.s, '', actor, answer=self.answer('plays_explain', ['Invented Lantern'], 'ben' if actor != 'ben' else 'ana'))
        with patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)):
            out = io.StringIO()
            self.assertEqual(main(['connectors', 'mappings', '--json'], out=out), 0)
            self.assertEqual(len(json.loads(out.getvalue())['items']), 5)
        self.assertEqual(plays.recent(self.s, self.s.GUEST)['items'], [])

    def test_plays_attribution_never_falls_back_to_together(self):
        self.database()
        item = dict(profile='unmapped', plays_source='db', type='movie', id='tmdb:998001', played=True, at=int(NOW*1000))
        with patch.object(self.s, 'session_at', return_value=None):
            self.assertEqual(self.s.attribute(item), (self.s.GUEST, [self.s.GUEST], {}))
    def test_forget_during_https_read_does_not_publish_or_count_old_rows(self):
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        reader = plays.https
        def concurrent_forget(*args, **kwargs):
            doc = reader(*args, **kwargs)
            history.forget(self.data, self.s.CONFIG, '2026-10-13', 'ana', apply=True)
            return doc
        with patch.object(plays, 'https', side_effect=concurrent_forget):
            result = plays.check(self.s, NOW)
        self.assertEqual(result['new_plays']['ana'], 0)
        self.assertEqual(plays.state(self.s)['https']['ana']['rows'], [])
        self.assertFalse(any(it['profile'] == 'ana' for it in self.s.read_history()))

    def test_raw_retention_limits_and_keep_raw_false_cover_both_caches(self):
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        plays.check(self.s, NOW)
        self.s.tmdb_details = lambda it: None
        for limit in (2, 0):
            self.s.CONFIG['history']['max_raw_items'] = limit
            history.cleanup(self.s, NOW, apply=False)
            self.assertTrue(plays.state(self.s)['https']['ana']['rows'])
            history.cleanup(self.s, NOW)
            saved = plays.state(self.s)
            self.assertLessEqual(len(saved['records']), limit)
            self.assertLessEqual(sum(len(a['rows']) for a in saved['https'].values()), limit)
            if limit == 2:
                plays.check(self.s, NOW+1)
        self.s.CONFIG['history'].pop('max_raw_items')
        self.s.CONFIG['connectors']['aiostreams_db']['keep_raw'] = False
        plays.check(self.s, NOW+2)
        saved = plays.state(self.s)
        self.assertEqual(saved['records'], [])
        self.assertTrue(all(not a['rows'] for a in saved['https'].values()))

    def test_recent_ignores_new_mapping_conflicts_and_respects_history_corrections(self):
        from skald import plays
        self.database()
        self.https()
        plays.check(self.s, NOW)
        items = self.s.read_history()
        for item in items:
            if item['profile'] == 'ana':
                item['history_by'] = ['ben']
        self.s._connector_write('history', {'items': items})
        with patch.object(self.s.time, 'time', return_value=NOW):
            self.assertEqual(plays.recent(self.s, 'ana')['items'], [])
            self.assertTrue(plays.explain(self.s, 'ben', 'tt0990002')['items'])
        path = self.data/'credentials/aiostreams.json'
        saved = json.loads(path.read_text())
        saved['kim'] = saved.pop('ana')
        path.write_text(json.dumps(saved))
        with patch.object(self.s.time, 'time', return_value=NOW):
            self.assertEqual(plays.explain(self.s, 'ben', 'tt0990002')['items'], [])

    def test_network_worker_holds_neither_commander_nor_history_lock(self):
        import threading
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        entered, release = threading.Event(), threading.Event()
        original = self.transport
        def stalled(req, timeout):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(req, timeout)
        with patch.object(self.s, '_connector_open', side_effect=stalled):
            self.s.plays_queue_check()
            self.assertTrue(entered.wait(2))
            try:
                self.assertTrue(history._writer_lock.acquire(timeout=0.1))
                history._writer_lock.release()
            finally:
                release.set()
            self.s._connector_pending[2].result(3)
        self.assertEqual(plays.state(self.s)['path'], 'https')

    def test_explain_intent_uses_fake_llm_in_three_languages(self):
        from decide_kit import ReplayLLM, reset
        from skald import commands
        for text in ('why did you count Invented Lantern as watched?',
                     'warum zählt Erfundene Laterne als gesehen?',
                     'zašto se Izmišljeni fenjer računa kao gledan?'):
            reset(self.s)
            with self.subTest(text=text), ReplayLLM(self.s) as replay:
                replay.arm({'choices': [{'message': {'content': json.dumps(self.answer('plays_explain', ['Invented Lantern']))}}]})
                plan = commands.preview(self.s, text, 'ana')
                self.assertEqual(replay.calls, 1)
                self.assertTrue(plan['read_only'])
    def test_dedup_collapses_device_rows_before_counting(self):
        from skald import plays
        self.database()
        row = list(self.rows[1])
        row[3] = int(NOW*1000)-1000
        newer = [row[0]+'-device', *row[1:]]
        newer[3] += 1000
        self.replace_db([row, newer])
        self.assertEqual(plays.check(self.s, NOW)['new_plays']['ana'], 1)
        self.assertEqual(len(plays.state(self.s)['records']), 1)
        self.assertEqual(self.s.read_history()[0]['at'], int(NOW*1000))

    def test_unseen_episode_is_counted_even_before_latest_title_time(self):
        from skald import plays
        self.database()
        row = list(self.rows[1])
        row[3] = int(NOW*1000)
        self.replace_db([row])
        plays.check(self.s, NOW)
        another = row[:]
        another[13], another[3] = 2, int(NOW*1000)-1000
        self.replace_db([row, another])
        self.assertEqual(plays.check(self.s, NOW+1)['new_plays']['ana'], 1)
        self.assertEqual(plays.check(self.s, NOW+2)['new_plays']['ana'], 0)

    def test_forget_removes_old_watermarks_and_burst_minutes(self):
        from skald import plays, history
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        plays.check(self.s, NOW)
        history.forget(self.data, self.s.CONFIG, '2026-10-13', 'ana', apply=True)
        account = plays.state(self.s)['https']['ana']
        self.assertEqual(account['seen'], 0)
        self.assertEqual(account['bursts'], [])

    def test_unknown_or_shared_plays_have_explicit_attribution_without_a_session(self):
        self.database()
        for profile, who, by in (('ana', 'ana', ['ana']), ('household', 'together', ['together']),
                                 ('unknown', self.s.GUEST, [self.s.GUEST])):
            item = dict(profile=profile, type='movie', id='tmdb:998001', played=True, at=int(NOW*1000))
            with self.subTest(profile=profile), patch.object(self.s, 'session_at', side_effect=AssertionError('session fallback')):
                self.assertEqual(self.s.attribute(item), (who, by, {}))
    def test_cli_recent_and_why_read_saved_titles_without_loading_a_profile(self):
        from skald import plays, setup
        from skald.__main__ import main
        self.database()
        plays.check(self.s, NOW)
        self.s._profiles.clear()
        (self.data/'profile.json').write_text(json.dumps({'entries': [dict(key='tt0990002', title='Invented Lantern')]}))
        before = {p.name: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}
        with patch.object(self.s.time, 'time', return_value=NOW), \
                patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)):
            out = io.StringIO()
            self.assertEqual(main(['plays', 'why', 'Invented Lantern', '--person', 'ana', '--json'], out=out), 0)
            self.assertEqual(json.loads(out.getvalue())['items'][0]['title'], 'Invented Lantern')
            self.assertIn('Invented Lantern', json.dumps(plays.recent(self.s, 'ana')))
        self.assertEqual({p.name: p.read_bytes() for p in self.data.rglob('*') if p.is_file()}, before)
    def test_saved_login_mapping_is_shared_with_database_reader(self):
        from skald import plays
        self.database()
        self.https()
        self.s.CONFIG['connectors']['aiostreams_db']['options']['profiles'].pop('a1b2c3d4')
        db = plays.database(self.s, plays.db_options(self.s))
        self.assertTrue(any(it['profile'] == 'ana' for it in db['items']))
        self.assertTrue(plays.compare(self.s)['equal'])

    def test_https_read_surfaces_show_backfill_and_never_secrets(self):
        from skald import plays, commands, setup
        from skald.__main__ import main
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        self.s._profiles['household'] = {'entries': [dict(key='tmdb:990004', title='Invented Burst')]}
        plays.check(self.s, NOW)
        with patch.object(self.s.time, 'time', return_value=NOW), \
                patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)):
            for args in (['plays', 'recent', '--person', 'ana', '--limit', '2'],
                         ['plays', 'why', 'Invented Burst', '--person', 'ana'], ['connectors', 'mappings']):
                with self.subTest(args=args):
                    out = io.StringIO()
                    self.assertEqual(main([*args, '--json'], out=out), 0)
                    for secret in (self.canary, self.token, 'a1b2c3d4-0000'):
                        self.assertNotIn(secret, out.getvalue())
            result = commands.preview(self.s, '', 'ana', answer=self.answer('plays_explain', ['Invented Burst']))
            self.assertEqual(len(result['plays']['items']), 5)
            for item in result['plays']['items']:
                self.assertEqual((item['login'], item['source'], item['at'], item['backfilled']), ('a1b2c3d4', 'https', None, True))
            self.assertEqual(len(plays.recent(self.s, 'ana', 2)['items']), 2)
            for op, values in (('plays_recent', []), ('plays_explain', ['Invented Burst'])):
                with self.assertRaises(PermissionError):
                    commands.preview(self.s, '', 'ana', bound=self.s.GUEST, answer=self.answer(op, values))
    def test_queued_request_cannot_restore_rows_removed_by_forget(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from skald import plays, history, setup
        self.database()
        self.https()
        self.s.CONFIG['plays'] = {'mode': 'https'}
        plays.check(self.s, NOW)
        writing, release, purging, forgotten = (threading.Event() for _ in range(4))
        writer, purge = setup.write_private, plays.purge
        def paused_write(path, content):
            if path.name == 'plays.json' and threading.current_thread().name.startswith('plays-request'):
                writing.set()
                if not release.wait(3):
                    raise AssertionError('request did not resume')
            return writer(path, content)
        def signaled_purge(*args, **kwargs):
            purging.set()
            return purge(*args, **kwargs)
        def forget():
            history.forget(self.data, self.s.CONFIG, '2026-10-13', 'ana', apply=True)
            forgotten.set()
        with patch.object(setup, 'write_private', side_effect=paused_write), \
                patch.object(plays, 'purge', side_effect=signaled_purge), patch.object(self.s, 'plays_queue_check'), \
                ThreadPoolExecutor(1, thread_name_prefix='plays-request') as requests, ThreadPoolExecutor(1) as privacy:
            requested = requests.submit(plays.run_check, self.s)
            try:
                self.assertTrue(writing.wait(2))
                removed = privacy.submit(forget)
                self.assertTrue(purging.wait(2))
                forgotten.wait(0.1)
            finally:
                release.set()
            requested.result(3)
            removed.result(3)
        self.assertEqual(plays.state(self.s)['https']['ana']['rows'], [])
    def test_other_player_shared_history_keeps_session_attribution(self):
        self.s.CONFIG['history']['source'] = 'jellyfin'
        item = dict(profile='household', type='movie', id='tmdb:998001', played=True, at=int(NOW*1000))
        with patch.object(self.s, 'session_at', return_value=dict(who='ana', src='manual')):
            self.assertEqual(self.s.attribute(item), ('ana', ['ana'], {'src_who': 'manual'}))

    def test_shared_plays_respect_named_guests_away_and_child_roles(self):
        from skald import plays
        self.cfg['household'].update(guests=['marko'], guest_until={'marko': '2026-10-18'}, away=['ben'])
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 12}
        self.write_config()
        self.s.refresh_preferences()
        shared = dict(profile='household', type='movie', id='tmdb:998001', played=True, at=int(NOW*1000))
        for viewer in ('ana', 'ben', 'kim', 'marko', self.s.GUEST):
            with self.subTest(viewer=viewer):
                self.assertEqual(plays.may_see(self.s, viewer, shared), viewer == 'ana')
                self.assertEqual(plays.may_see(self.s, viewer, {**shared, 'history_by': ['together']}), viewer == 'ana')
                self.assertEqual(plays.may_see(self.s, viewer, {**shared, 'history_by': [viewer]}), viewer != self.s.GUEST)

    def test_plays_guests_and_away_change_only_their_own_collection(self):
        from skald import commands
        self.cfg['plays'] = {}
        self.cfg['household'].update(guests=['marko'], guest_until={'marko': '2026-10-18'}, away=['ben'])
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 12}
        self.write_config()
        self.s.refresh_preferences()
        for actor in ('ben', 'marko', 'kim', self.s.GUEST):
            for op, values, person in (('plays_mode', ['off'], None), ('plays_every', ['1h'], None),
                                       ('plays_person', ['off'], 'ana'), ('plays_person', ['off'], 'together')):
                with self.subTest(actor=actor, op=op, person=person), self.assertRaises(PermissionError):
                    commands.preview(self.s, '', actor, answer=self.answer(op, values, person))
        for actor in ('ben', 'marko'):
            commands.apply(self.s, commands.preview(self.s, '', actor, answer=self.answer('plays_person', ['off'])))
            self.assertFalse(commands.config(self.s)['plays']['people'][actor])
            commands.undo(self.s, [actor], actor=actor)
        with self.assertRaises(PermissionError):
            commands.preview(self.s, '', 'kim', bound='ana', answer=self.answer('plays_mode', ['off']))

    def test_plays_apply_and_undo_recheck_guest_or_away_actor(self):
        from skald import commands, setup
        for key in ('guests', 'away'):
            for op, values in (('plays_mode', ['off']), ('plays_every', ['1h'])):
                with self.subTest(key=key, op=op):
                    plan = commands.preview(self.s, '', 'ben', answer=self.answer(op, values))
                    commands.apply(self.s, plan)
                    cfg = commands.config(self.s)
                    cfg['household'][key] = ['ben']
                    setup.publish(self.data, cfg, replace=True)
                    self.s.refresh_preferences()
                    with self.assertRaises(PermissionError):
                        commands.apply(self.s, plan)
                    with self.assertRaises(PermissionError):
                        commands.undo(self.s, ['ben'], actor='ben')
                    cfg['household'].pop(key)
                    setup.publish(self.data, cfg, replace=True)
                    self.s.refresh_preferences()
                    commands.undo(self.s, ['ben'], actor='ben')

    def test_plays_keep_moved_out_history_without_collecting_it(self):
        from skald import commands, plays
        self.database()
        self.https()
        old = dict(profile='ben', type='movie', id='tmdb:998001', played=True, at=int(NOW*1000))
        self.s._connector_write('history', {'items': [old]})
        plan = commands.preview(self.s, '', 'ana', answer=self.answer('plays_person', ['off'], 'ben'))
        self.cfg['household']['retired'] = ['ben']
        self.write_config()
        self.s.refresh_preferences()
        for mode in ('db', 'https'):
            with self.subTest(mode=mode):
                self.s.CONFIG['plays'] = {'mode': mode}
                plays.check(self.s, NOW)
                self.assertIn(old, self.s.read_history())
                self.assertNotIn('ben', plays.logins(self.s)[1])
                self.assertFalse(plays.may_see(self.s, 'ben', old))
                self.assertEqual(self.s.attribute(old), ('ben', ['ben'], {}))
        with self.assertRaises(ValueError):
            commands.preview(self.s, '', 'ana', answer=self.answer('plays_person', ['on'], 'ben'))
        with self.assertRaises(ValueError):
            commands.apply(self.s, plan)

    def test_mapping_keeps_moved_out_logins_unmapped_even_with_stale_people(self):
        from skald import plays
        self.database()
        self.https()
        self.s.CONFIG['household']['retired'] = ['ana']
        self.assertFalse(plays.may_see(self.s, 'ana', dict(profile='ana')))
        self.assertFalse(any(r['person'] == 'ana' and r['status'] == 'mapped' for r in plays.person_mapping(self.s)))
        self.assertFalse(any(it['profile'] == 'ana' for it in plays.database(self.s, plays.db_options(self.s))['items']))
        self.assertNotIn('ana', plays.logins(self.s)[1])
        path = self.data/'credentials/aiostreams.json'
        saved = json.loads(path.read_text())
        saved['kim'] = saved.pop('ana')
        path.write_text(json.dumps(saved))
        mapping = next(r for r in plays.person_mapping(self.s) if r['login'] == 'a1b2c3d4')
        self.assertEqual(mapping['status'], 'conflict')
        self.assertFalse(any(it['profile'] in ('ana', 'kim') for it in plays.database(self.s, plays.db_options(self.s))['items']))

    def test_auto_https_connector_keeps_its_state_across_reload(self):
        self.database()
        self.https()
        self.cfg['history']['source'] = 'none'
        self.cfg['connectors'] = {}
        self.write_config()
        self.s.refresh_preferences()
        self.s.connectors_load(NOW)
        before = (self.data/'connectors.json').read_bytes()
        self.assertEqual(json.loads(before)['connectors']['player']['health']['state'], 'ok')
        self.s.connectors_load(NOW+900)
        self.assertEqual((self.data/'connectors.json').read_bytes(), before)

    def test_plays_targeted_undo_redo_and_change_order(self):
        from skald import commands
        commands.apply(self.s, commands.preview(self.s, '', 'ana', answer=self.answer('plays_mode', ['off'])))
        entry = commands.journal(self.s)['ana'][-1]
        commands.apply(self.s, commands.preview(self.s, '', 'ana', answer=self.answer('undo', [entry['id']])))
        self.assertNotIn('plays', commands.config(self.s))
        commands.apply(self.s, commands.preview(self.s, '', 'ana', answer=self.answer('redo', [entry['id']])))
        self.assertEqual(commands.config(self.s)['plays']['mode'], 'off')
        commands.apply(self.s, commands.preview(self.s, '', 'ben', answer=self.answer('plays_mode', ['https'])))
        latest = commands.journal(self.s)['ana'][-1]
        with self.assertRaisesRegex(ValueError, 'later change'):
            commands.preview(self.s, '', 'ana', answer=self.answer('undo', [latest['id']]))
