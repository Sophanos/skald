"""Child reads use synthetic people, logins and a fixed local calendar."""
from contextlib import nullcontext
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, ReplayLLM, fixture_rows
from skald import commands, members, plays, setup
from skald.__main__ import main

NOW = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc).timestamp()


class KidRead(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.cfg = members.fresh_config(['ana', 'ben', 'kim', 'lena'])
        self.cfg['household']['guests'] = ['lena']
        self.cfg['taste']['people']['kim']['prefs'] = {'kids_age': 9}
        self.save('skald.json', self.cfg)
        from skald.migrate import ensure
        ensure(self.data)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.addCleanup(members.configure)
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s.time, 'time', return_value=NOW))
        self.enterContext(patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(self.s)))

    def save(self, name, doc):
        (self.data/name).write_text(json.dumps(doc))

    def preview(self, op, actor, person='kim', values=None, **kwargs):
        return commands.preview(self.s, '', actor, answer=dict(op=op, person=person, kind=None,
                                values=values or [], language='en'), **kwargs)

    def cli(self, *args, code=0):
        out = io.StringIO()
        self.assertEqual(main([*args, '--json'], out), code, out.getvalue())
        return json.loads(out.getvalue())

    def history(self):
        rows = [dict(profile=p, type='movie', id=f'tmdb:{990000+i}', title=f'Invented Watch {i}',
                     at=int(NOW*1000), played=True) for i, p in enumerate(('ana', 'ben', 'kim', 'household', 'household'))]
        rows[3]['history_by'] = ['ana', 'kim']
        rows[4]['history_by'] = ['ana', 'ben']
        self.s._connector_write('history', {'items': rows})

    def test_adults_read_child_recent_and_why_cli_and_commander(self):
        self.history()
        for adult in ('ana', 'ben'):
            for bound in (None, adult):
                with self.subTest(adult=adult, bound=bound):
                    plan = self.preview('plays_recent', adult, bound=bound)
                    self.assertTrue(plan['read_only'])
                    self.assertEqual({r['id'] for r in plan['plays']['items']}, {'tmdb:990002', 'tmdb:990003'})
                    self.assertEqual(plan['name'], "Kim's plays this week")
                    self.assertNotIn('plan', plan)
            recent = self.cli('plays', 'recent', 'kim', '--person', adult)
            self.assertEqual(recent, plan['plays'])
            why = self.cli('plays', 'why', 'tmdb:990002', 'kim', '--person', adult)
            self.assertEqual(why, self.preview('plays_explain', adult, values=['tmdb:990002'])['plays'])
            self.assertEqual(self.preview('plays_explain', adult, values=['tmdb:990000'])['plays']['items'], [])

    def test_empty_child_plays_use_resolved_locale(self):
        for language, expected in (('en', 'No plays found'), ('de', 'Keine Wiedergaben gefunden'),
                                   ('sr-latn', 'Nema pronađenih gledanja'), ('sr-cyrl', 'Нема пронађених гледања')):
            for override in (False, True):
                self.s.CONFIG['region']['row_lang'] = language if override else None
                for op in ('plays_recent', 'plays_explain'):
                    with self.subTest(language=language, override=override, op=op):
                        plan = commands.preview(self.s, '', 'ana', answer=dict(op=op, person='kim', kind=None,
                            values=['missing'] if op == 'plays_explain' else [], language='en' if override else language))
                        self.assertEqual(plan['description'], expected)
            self.s.CONFIG['region']['row_lang'] = language
            for args in (('recent', 'kim'), ('why', 'missing', 'kim')):
                with self.subTest(language=language, cli=args):
                    out = io.StringIO()
                    self.assertEqual(main(['plays', *args, '--person', 'ana'], out), 0)
                    self.assertEqual(out.getvalue().strip(), expected)

    def test_other_reads_stay_refused_and_own_reads_stay_own(self):
        self.history()
        for actor, target in (('kim', 'ana'), ('lena', 'kim'), ('ana', 'ben')):
            with self.subTest(actor=actor, target=target):
                with self.assertRaises(PermissionError):
                    self.preview('plays_recent', actor, target)
                self.cli('plays', 'recent', target, '--person', actor, code=2)
        self.s.CONFIG['household']['away'] = ['ben']
        with self.assertRaises(PermissionError):
            self.preview('plays_recent', 'ben')
        self.cli('plays', 'recent', 'kim', '--person', 'ben', code=2)
        own = self.cli('plays', 'recent', '--person', 'kim')
        self.assertEqual(own, self.preview('plays_recent', 'kim', None)['plays'])

    def taste(self):
        entries = [dict(key=f'tmdb:{990100+i}', id=990100+i, title=f'Invented Taste {p}', year='2020',
                        kind='movie', stype='movie', signal='watched', weight=2., age=2., who=p,
                        wsrc='tv', by=[p], flags=[], item={'at': int(NOW*1000)}, genres=[])
                   for i, p in enumerate(('ana', 'ben', 'kim'))]
        profile = dict(entries=entries, built_at=NOW, seen=[e['key'] for e in entries])
        self.save('profile.json', profile)
        self.s._profiles['household'] = profile
        return entries

    def test_adults_read_child_drivers_without_correction_write_rights(self):
        self.taste()
        with patch.object(self.s, 'get_model', return_value=None):
            for adult in ('ana', 'ben'):
                for bound in (None, adult):
                    plan = self.preview('taste_drivers', adult, bound=bound)
                    self.assertEqual([d['title'] for d in plan['alignment']['drivers']], ['Invented Taste kim'])
                    self.assertTrue(plan['read_only'])
                    self.assertNotIn('plan', plan)
                    for check in plan['alignment']['check']:
                        self.assertEqual(check['fixes'], [])
                    with self.assertRaises(PermissionError):
                        commands.preview(self.s, '', adult, answer=dict(op='ignore_learning', kind='movie',
                                         values=['Invented Taste kim'], person='kim'))
                data = self.cli('taste', 'align', 'kim', '--person', adult)
                self.assertEqual(data['person'], 'kim')
                self.assertEqual([d['title'] for d in data['drivers']], ['Invented Taste kim'])
            for actor in ('lena', 'kim'):
                with self.assertRaises(PermissionError):
                    self.preview('taste_drivers', actor, 'ana' if actor == 'kim' else 'kim')
            self.s.CONFIG['household']['away'] = ['ben']
            with self.assertRaises(PermissionError):
                self.preview('taste_drivers', 'ben')
            with self.assertRaises(PermissionError):
                self.preview('taste_drivers', 'ana', 'ben')

    def test_child_own_login_teaches_child_taste(self):
        self.s.CONFIG['plays'] = {}
        watch = dict(profile='kim', type='movie', id='tmdb:990199', played=True, at=int(NOW*1000))
        self.assertEqual(self.s.attribute(watch)[:2], ('kim', ['kim']))
        with patch.object(self.s, 'tmdb_details', return_value=('movie', dict(id=990199, title='Invented Child Watch'))):
            profile = self.s.build_profile([watch], [], remote=False)
        entries = self.s.person_entries(profile, 'kim')
        self.assertEqual([e['key'] for e in entries], ['tmdb:990199'])
        self.assertGreater(entries[0]['weight'], 0)
        self.assertEqual(self.s.person_entries(profile, 'ana'), [])

    def database(self, days, *, people=None):
        path = self.data/'watch.sqlite'
        profiles = people or {'c1234567': 'kim', 'a1234567': 'ana', 'f1234567': 'household'}
        self.cfg['history']['source'] = 'aiostreams_db'
        self.cfg['connectors']['aiostreams_db'] = dict(kind='player', enabled=True, auth=None,
            options=dict(path=str(path), profiles=profiles))
        self.save('skald.json', self.cfg)
        self.s.refresh_preferences()
        rows = [[prefix, 'movie', f'tmdb:{991000+i}', int(at*1000), 1, 'movie', 0, 100, 100, None, None, 0, None, None]
                for i, (prefix, at) in enumerate(days)]
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE watch_state ('+', '.join(plays.COLUMNS)+')')
            con.executemany('INSERT INTO watch_state VALUES ('+','.join('?' for _ in plays.COLUMNS)+')', rows)

    def test_watching_days_own_login_logical_day_retention_and_role(self):
        from zoneinfo import ZoneInfo
        own = dt.datetime(2026, 10, 15, 2, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        self.database([('c1234567', own), ('a1234567', NOW), ('f1234567', NOW)])
        self.s.CONFIG['history']['max_raw_items'] = 0
        plays.check(self.s, NOW)
        saved = json.loads((self.data/'watching-days.json').read_text())
        self.assertEqual(saved, {'kim': ['2026-10-14']})
        self.assertEqual((self.data/'watching-days.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(plays.state(self.s)['records'], [])
        self.assertEqual(self.preview('plays_around', 'ana')['plays']['last_day'], '2026-10-14')
        self.s.CONFIG['taste']['people']['kim']['prefs'].pop('kids_age')
        plays.check(self.s, NOW)
        self.assertEqual(json.loads((self.data/'watching-days.json').read_text()), {})

    def test_https_backfill_never_saves_day_after_partial_minute_retention(self):
        self.database([])
        self.s.CONFIG['plays'] = {'mode': 'https'}
        at = NOW-self.s.DAY+40
        login = dict(uuid='c1234567-0000-0000-0000-000000000000', password='fake')
        def reply(url, *args, **kwargs):
            if url.endswith('/AuthenticateByName'):
                return dict(User={'Id': login['uuid']}, AccessToken='fake')
            return dict(Items=[], TotalRecordCount=0, StartIndex=0)
        with patch.object(plays, 'logins', return_value=('https://aio.invalid', {'kim': login})), \
                patch.object(self.s, '_http_get', side_effect=reply):
            for maximum in (None, 1):
                with self.subTest(max_raw_items=maximum):
                    (self.data/plays.WATCHING_DAYS).unlink(missing_ok=True)
                    self.s.CONFIG['history'] = dict(source='aiostreams_db', keep_raw_days=1, max_raw_items=maximum)
                    _, cache, _ = plays.https(self.s, now=at)
                    cache['kim'].update(seen=int(at*1000), bursts=[int(at//60)], rows=[
                        ['c1234567', 'series', 'tmdb:991000', int((at-(30 if ep < 5 else 0))*1000), 1, 'episode', 0, 100, 100,
                         None, None, 0, 1, ep] for ep in range(1, 6)])
                    self.save('plays.json', {'https': cache})
                    self.assertEqual(plays.check(self.s, NOW+30)['path'], 'https')
                    self.assertEqual(plays.days_state(self.data), {})
                    self.assertEqual(plays.watching_days(self.s), {})

    def test_around_fixed_calendar_cli_commander_and_permissions(self):
        self.database([])
        days = ['2026-08-20', '2026-08-21', '2026-08-23', '2026-08-24', '2026-09-04', '2026-09-05',
                '2026-09-06', '2026-09-11', '2026-09-12', '2026-09-13', '2026-09-18', '2026-09-19', '2026-09-20',
                '2026-10-09', '2026-10-10', '2026-10-11', '2026-10-12', '2026-10-13']
        self.save('watching-days.json', {'kim': days, 'ana': ['2026-10-15']})
        for adult in ('ana', 'ben'):
            plan = self.preview('plays_around', adult, bound=adult)
            data = plan['plays']
            self.assertTrue(plan['read_only'])
            self.assertEqual(self.cli('plays', 'around', 'kim', '--person', adult), data)
            self.assertEqual((data['last_day'], data['around'], data['days_7'], data['days_28']), ('2026-10-13', True, 5, 8))
            self.assertEqual(data['usual_weekdays'], ['fri', 'sat', 'sun'])
            self.assertEqual(data['stretches'][0], dict(start='2026-10-09', end='2026-10-13', days=5))
            self.assertEqual(data['gaps'], [dict(start='2026-09-21', end='2026-10-08', days=18),
                                           dict(start='2026-08-25', end='2026-09-03', days=10)])
            self.assertIn('5 of the last 7 days', data['text'])
            self.assertNotIn('holiday', data['text'])
        self.assertNotIn('2026-08-20', (self.data/'watching-days.json').read_text())
        for actor, target in (('kim', 'kim'), ('lena', 'kim'), ('ana', 'ben')):
            with self.subTest(actor=actor, target=target), self.assertRaises(PermissionError):
                self.preview('plays_around', actor, target)
            self.cli('plays', 'around', target, '--person', actor, code=2)
        self.s.CONFIG['household']['away'] = ['ben']
        with self.assertRaises(PermissionError):
            self.preview('plays_around', 'ben')

    def test_around_fewer_than_three_full_weeks_and_no_own_login(self):
        self.save('watching-days.json', {'kim': ['2026-10-01', '2026-10-08', '2026-10-15']})
        result = self.preview('plays_around', 'ana')['plays']
        self.assertEqual(set(result), {'person', 'setup', 'text'})
        self.assertIn('python -m skald setup aiostreams --member kim', result['text'])
        self.database([])
        result = self.preview('plays_around', 'ana')['plays']
        self.assertIsNone(result['usual_weekdays'])
        self.assertTrue(result['around'])
        self.save('watching-days.json', {'kim': ['2026-10-08']})
        self.assertFalse(self.preview('plays_around', 'ana')['plays']['around'])

    def test_forget_child_counts_and_removes_days_and_cutoff_keeps_newer(self):
        from skald import history
        self.save('watching-days.json', {'kim': ['2026-09-01', '2026-10-01', '2026-10-15']})
        preview = history.forget(self.data, self.cfg, '2026-10-01', 'kim')
        self.assertEqual(preview['watching_days'], 1)
        history.forget(self.data, self.cfg, '2026-10-01', 'kim', apply=True)
        self.assertEqual(json.loads((self.data/'watching-days.json').read_text()), {'kim': ['2026-10-01', '2026-10-15']})
        preview = self.cli('members', 'forget', 'kim', '--person', 'ana')
        self.assertEqual(preview['forget']['counts']['watching_days'], 2)
        self.cli('members', 'forget', 'kim', '--person', 'ana', '--yes')
        self.assertNotIn('kim', json.loads((self.data/'watching-days.json').read_text()))

    def test_household_erase_deletes_watching_days_file(self):
        from skald import erase
        self.save('watching-days.json', {'kim': ['2026-10-15']})
        self.assertNotIn('watching-days.json', erase.KEEP)
        plan = erase.preview(self.s)
        erase.apply(self.s, {**plan, 'source': 'cli', 'confirm': 'ERASE'})
        self.assertFalse((self.data/'watching-days.json').exists())

    def test_locales_have_all_child_read_strings(self):
        from skald import i18n
        english = {key for key in i18n._STRINGS['en'] if key.startswith('plays.')}
        self.assertGreaterEqual(len(english), 20)
        for lang in ('en', 'de', 'sr-latn', 'sr-cyrl'):
            self.assertTrue(english <= i18n._STRINGS[lang].keys(), lang)
            for key in english:
                self.assertEqual(i18n._fields(i18n._STRINGS[lang][key]), i18n._fields(i18n._STRINGS['en'][key]))

    def test_stopping_child_role_drops_days_on_preferences_reload(self):
        self.save('watching-days.json', {'kim': ['2026-10-15']})
        self.cfg['taste']['people']['kim']['prefs'].pop('kids_age')
        self.save('skald.json', self.cfg)
        self.s.refresh_preferences()
        self.assertEqual(json.loads((self.data/'watching-days.json').read_text()), {})

    def test_day_math_keeps_only_56_days_and_limits_runs(self):
        self.database([])
        today = dt.date(2026, 10, 15)
        start = today-dt.timedelta(days=55)
        days = [(start+dt.timedelta(days=i)).isoformat() for i in range(56) if i % 4 < 3]
        self.save('watching-days.json', {'kim': [(start-dt.timedelta(days=1)).isoformat(), *days]})
        data = self.preview('plays_around', 'ana')['plays']
        self.assertEqual(len(data['stretches']), 5)
        self.assertEqual(data['stretches'][0]['start'], '2026-10-12')
        self.save('watching-days.json', {'kim': [(start+dt.timedelta(days=i)).isoformat() for i in (0, 8, 16, 24, 32, 40)]})
        data = self.preview('plays_around', 'ana')['plays']
        self.assertEqual(len(data['gaps']), 3)
        self.assertTrue(all(r['days'] == 7 for r in data['gaps']))

    def test_watching_days_do_not_return_after_cutoff_during_read(self):
        from skald import history
        at = NOW-10*self.s.DAY
        self.database([('c1234567', at)])
        reader = plays.select
        def forgotten_while_reading(*args, **kwargs):
            doc = reader(*args, **kwargs)
            history.forget(self.data, self.cfg, '2026-10-10', 'kim', apply=True)
            return doc
        with patch.object(plays, 'select', side_effect=forgotten_while_reading):
            plays.check(self.s, NOW)
        self.assertEqual(plays.days_state(self.data), {})

    def test_child_read_hints_invalidate_cached_parses_and_routes(self):
        self.assertEqual(self.s.DECISIONS['parse_command']['ver'], 19)
        self.assertEqual(self.s.DECISIONS['route_query']['ver'], 9)
        self.assertIn('person the named child', commands.REGISTRY['plays_recent']['hint'])
        self.assertIn('person the named child', commands.REGISTRY['plays_explain']['hint'])
        self.assertIn('person the named child', commands.REGISTRY['taste_drivers']['hint'])
        self.assertIn('watching days', self.s.ROUTE_Q['route']['criteria']['command'])

    def test_child_queries_use_llm_without_local_phrase_matching(self):
        for case in fixture_rows(self.s, 'parse_command'):
            if not case['note'].startswith('Synthetic: child reads'):
                continue
            with self.subTest(query=case['in']['query']), ReplayLLM(self.s) as fake:
                self.assertIsNone(self.s.DECISIONS['parse_command']['rule'](case['in'], {}))
                fake.arm(case['recorded'])
                self.assertEqual(self.s.decide('parse_command', case['in'])['answer'], case['expect'])
                self.assertEqual(fake.calls, 1)

    def test_generated_skill_explains_adult_child_reads(self):
        from eval.decisions import skill_block
        self.assertIn("Current adults may also use Commander to read a child's plays, taste and watching days.", skill_block())

    def test_household_login_named_shared_watch_never_creates_child_day(self):
        self.database([('f1234567', NOW)])
        plays.check(self.s, NOW)
        history = self.s.read_history()
        history[0]['history_by'] = ['ana', 'kim']
        self.s._connector_write('history', {'items': history})
        self.assertEqual(len(self.preview('plays_recent', 'ana')['plays']['items']), 1)
        self.assertEqual(plays.days_state(self.data), {})

    def test_child_reads_stay_refused_for_guest_and_away_cli(self):
        self.taste()
        self.cfg['household']['away'] = ['ben']
        self.save('skald.json', self.cfg)
        self.s.refresh_preferences()
        for actor in ('lena', 'ben'):
            self.cli('taste', 'align', 'kim', '--person', actor, code=2)
            self.cli('plays', 'around', 'kim', '--person', actor, code=2)

