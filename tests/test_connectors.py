"""Local connector contracts: no network, auth resolution, or duplicate spine folds."""
from decide_kit import write_legacy
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from decide_kit import load_server, close_server
from test_api import raw_request
from skald.__main__ import main, parser, execute, text_output
from skald.engine import Engine, DATA_FILES
from skald import guide, backup

NOW = 1791734400.0
CANARY = 'SYNTHETIC_CONNECTOR_TOKEN_DO_NOT_STORE'


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        write_legacy(self.data)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.write('history', {'items': []})
        self.write('trakt', {'exported_at': '2026-10-11T00:00:00Z', 'items': []})
        with redirect_stdout(io.StringIO()):
            self.s.connectors_load(NOW)

    def write(self, name, doc):
        (self.data / (name + '.json')).write_text(json.dumps(doc))

    def row(self, kind='file_drop', **kw):
        c = self.s._connector('history', kind, 'both', NOW, 0, NOW)
        c.update(kw)
        return c

    def tick(self, now=NOW):
        with patch.object(self.s, 'tmdb', side_effect=AssertionError('network forbidden')):
            self.s.connectors_tick(now)

    def test_cadence_halve_double_floor_ceiling(self):
        s = self.s
        for interval, new, expected in ((4*3600, True, 2*3600), (3600, True, 3600),
                                        (3600, False, 7200), (s.DAY, False, s.DAY)):
            self.assertEqual(s.connector_next(self.row(interval=interval), new, NOW), (expected, NOW+expected))

    def test_idle_ceiling_and_daily_kinds(self):
        s = self.s
        for kind in ('file_drop', 'api', 'player'):
            c = self.row(kind, interval=4*s.DAY, last_new=NOW-30*s.DAY)
            expected = 7*s.DAY if kind == 'file_drop' else s.DAY
            self.assertEqual(s.connector_next(c, False, NOW), (expected, NOW+expected))
        c = self.row(last_new=NOW-30*s.DAY+1, interval=s.DAY)
        self.assertEqual(s.connector_next(c, False, NOW)[0], s.DAY)
        self.assertEqual(s.connector_next(c, True, NOW)[0], s.DAY/2)
        self.assertEqual(s.connector_next(self.row(interval=7*s.DAY,last_new=NOW-40*s.DAY),True,NOW)[0],3.5*s.DAY)

    def test_manual_never_due_and_reminder_boundary(self):
        s = self.s
        c = s._connector('manual:plain:'+s.PEOPLE[0], 'manual_export', s.PEOPLE[0], NOW,
                         '2026-09-11', NOW-s.CONNECTOR_REMIND)
        self.assertEqual(s.connector_next(c, True, NOW), (s.CONNECTOR_MIN, None))
        self.assertIsNone(s.connector_reminder(c, NOW-1))
        self.assertIn('30 days old', s.connector_reminder(c, NOW))
        s._connectors[c['id']] = c
        self.tick()
        self.assertEqual(s._connectors[c['id']]['health']['state'], 'stale')
        self.assertIsNone(s._connectors[c['id']]['next_due'])

    def test_missing_corrupt_wrong_version_seed_and_unknown_ids(self):
        s = self.s
        for value in (None, '{', '{"version":2,"connectors":{}}', '{"version":1,"connectors":[]}', '{"version":1,"connectors":{},"bad":NaN}'):
            path = self.data/'connectors.json'
            if value is None:
                path.unlink()
            else:
                path.write_text(value)
            with patch('builtins.print') as log:
                s.connectors_load(NOW)
            self.assertEqual(set(s._connectors), {'history','trakt','trakt_api','letterboxd_rss','player','aiostreams_db','nzbhydra'})
            self.assertEqual(log.call_count, 1)
        (self.data/'connectors.json').unlink()
        with patch.object(s,'_connectors_save',side_effect=PermissionError(CANARY)), redirect_stdout(io.StringIO()) as log:
            s.connectors_load(NOW)
        self.assertEqual(len(s._connectors),7)
        self.assertNotIn(CANARY,log.getvalue())
        unknown = s._connector('future', 'new_kind', 'both', NOW)
        self.write('connectors', {'version': 1, 'connectors': {'future': {**unknown, 'custom': 7}}})
        s.connectors_load(NOW)
        self.assertEqual(s._connectors['future']['custom'], 7)
        self.assertEqual(s._connectors['future']['health']['state'], 'disabled')
        with patch.object(s,'_connectors_save',side_effect=AssertionError('no load write')):
            s.connectors_load(NOW+1)

    def test_atomic_private_write_and_stale_generation(self):
        s = self.s
        with patch.object(s.os, 'replace', wraps=os.replace) as replace, patch.object(s.os, 'fsync', wraps=os.fsync) as sync:
            self.assertTrue(s._connectors_save(s._connectors, s._connectors_generation))
        replace.assert_called_once()
        sync.assert_called_once()
        self.assertEqual(stat.S_IMODE((self.data/'connectors.json').stat().st_mode), 0o600)
        before = (self.data/'connectors.json').read_bytes()
        self.assertFalse(s._connectors_save({}, s._connectors_generation-1))
        self.assertEqual((self.data/'connectors.json').read_bytes(), before)
        self.assertFalse(list(self.data.glob('.connectors-*')))

    def test_noop_tick_and_idempotent_load_do_not_write(self):
        self.tick()
        before = (self.data/'connectors.json').stat().st_mtime_ns
        with patch.object(self.s, '_connectors_save', side_effect=AssertionError('no write')), patch('builtins.open', side_effect=AssertionError('no read')):
            self.tick(NOW+1)
        self.assertEqual((self.data/'connectors.json').stat().st_mtime_ns, before)
        with patch.object(self.s, '_connectors_save', side_effect=AssertionError('no load write')):
            self.s.connectors_load(NOW+1)

    def test_history_stamp_churn_is_not_new(self):
        self.write('history', {'items': [{'id': 'tmdb:1','type':'movie','at':1000}]})
        self.tick()
        last_new = self.s._connectors['history']['last_new']
        path = self.data/'history.json'
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns+1))
        self.tick(NOW+1800)
        c = self.s._connectors['history']
        self.assertEqual(c['cursor'], 0)
        self.assertEqual(c['last_new'], last_new)
        self.assertEqual(c['counts']['new_last'], 0)

    def test_warm_tick_stats_each_drop_once(self):
        self.tick()
        with patch.object(self.s.os, 'stat', wraps=os.stat) as stats:
            self.tick(NOW+1)
        paths = [Path(call.args[0]).name for call in stats.call_args_list]
        self.assertEqual(paths.count('history.json'), 1)
        self.assertEqual(paths.count('trakt.json'), 1)

    def test_demand_debounce_eligibility_and_memory_only(self):
        s = self.s
        self.tick()
        c = s._connectors['history']
        due = c['next_due']
        c['last_sync'] = NOW
        manual = s._connector('manual:plain:both', 'manual_export', 'both', NOW)
        s._connectors[manual['id']] = manual
        with patch('builtins.open', side_effect=AssertionError('no IO')), patch.object(s.os, 'stat', side_effect=AssertionError('no stat')), \
                patch.object(s, '_connectors_save', side_effect=AssertionError('no save')), patch.object(s, 'poll', side_effect=AssertionError('no poll')):
            s.connectors_demand(NOW+299)
            self.assertEqual(c['next_due'], due)
            s.connectors_demand(NOW+300)
        self.assertEqual(c['next_due'], NOW+300)
        self.assertIsNone(manual['next_due'])
        self.assertIsNone(s._connectors['trakt_api']['next_due'])

    def test_history_and_trakt_good_corrupt_missing_cache_policy(self):
        s = self.s
        items = [{'id':'tmdb:1', 'type':'movie','at':1000}]
        self.write('history', {'items':items})
        self.write('trakt', {'items':items + [{'id':'tt1'}]})
        first = s.read_trakt()
        self.assertEqual(first, items)
        hist = s.read_history()
        hist.clear()
        self.assertEqual(s.read_history(), items)
        for name in ('history', 'trakt'):
            (self.data/(name+'.json')).write_text('{"items":'+CANARY)
        with redirect_stdout(io.StringIO()) as logs:
            self.assertEqual(s.read_history(), [])
            self.assertIs(s.read_trakt(), first)
        self.assertNotIn(CANARY, logs.getvalue())
        for name in ('history','trakt'):
            (self.data/(name+'.json')).unlink()
        self.assertEqual(s.read_history(), [])
        self.assertEqual(s.read_trakt(), [])
        self.assertEqual(first, items)

    def test_poll_trakt_matches_adapter_and_preserves_cache(self):
        s = self.s
        items = [{'id':'tmdb:1','type':'movie','at':NOW*1000,'title':'Synthetic','genres':['drama']}]
        self.write('trakt', {'exported_at': '2026-10-11T00:00:00Z', 'items':items})
        first = s.read_trakt()
        expected = list(s.trakt_events(NOW, first))
        c = s._connector('trakt', 'file_drop', s.TRAKT_PERSON, NOW, ['2026-10-10',0])
        events, cursor, stats = s.poll(c, NOW)
        self.assertEqual(events, expected)
        self.assertEqual(cursor, ['2026-10-11T00:00:00Z',1])
        self.assertEqual(first, items)
        self.assertIs(s.read_trakt(), first)

    def test_poll_history_matches_adapter_without_network_or_spine_mutation(self):
        s = self.s
        item = {'id':'tmdb:1','type':'movie','at':NOW*1000,'played':True}
        det = {'id':1, 'title':'Synthetic','release_date':'2020-01-01','genres':[{'name':'Drama'}]}
        self.write('history', {'items':[item]})
        s._profiles['household'] = {'entries':[{'item':item,'key':'tmdb:1','title':'Synthetic','year':'2020','genres':['Drama']}]}
        with patch.object(s, 'tmdb_details', return_value=('movie',det)):
            expected = list(s.tv_events(NOW,[item]))
        before = copy.deepcopy(s._profiles)
        with patch.object(s, 'tmdb_details', side_effect=AssertionError('no network')), \
                patch.object(s, 'fold_events', side_effect=AssertionError('no fold')):
            events, cursor, stats = s.poll(self.row(), NOW)
        self.assertEqual(events, expected)
        self.assertEqual(cursor, NOW*1000)
        self.assertEqual(s._profiles, before)

    def test_history_missing_metadata_retries_without_losing_cursor(self):
        s = self.s
        item = {'id':'tmdb:2','type':'movie','at':2000,'played':True}
        self.write('history', {'items':[item]})
        c = self.row()
        events, cursor, stats = s.poll(c,NOW)
        self.assertEqual((events,cursor),([],0))
        s._profiles['household'] = {'entries':[{'item':item,'key':'tmdb:2','title':'Synthetic','year':'2020','genres':[]}]}
        events, cursor, stats = s.poll(c,NOW+1)
        self.assertEqual(cursor,2000)
        self.assertEqual(len(events),1)

    def test_generation_change_during_fsync_defers_material_save(self):
        s = self.s
        self.tick()
        s._connectors['history']['interval']=4*3600
        before=(self.data/'connectors.json').read_bytes()
        with patch.object(s.os,'fsync',side_effect=lambda fd:s.connectors_demand(NOW+300)):
            self.assertFalse(s._connectors_save(s._connectors,s._connectors_generation))
        self.assertEqual((self.data/'connectors.json').read_bytes(),before)
        self.tick(NOW+1)
        self.assertNotEqual((self.data/'connectors.json').read_bytes(),before)

    def test_doctor_finding_uses_shared_reminder_and_warning_switch(self):
        state_temp=tempfile.TemporaryDirectory()
        self.addCleanup(state_temp.cleanup)
        state=Path(state_temp.name)
        self.assertFalse(state.is_relative_to(self.data))
        self.write('profile',{'entries':[]})
        c=self.s._connector('manual:plain:both','manual_export','both',NOW,'2026-09-01',NOW-40*self.s.DAY)
        self.s._connectors[c['id']]=c
        self.s._connectors_save(self.s._connectors,self.s._connectors_generation)
        args=parser().parse_args(['--snapshot',str(self.data),'doctor'])
        with patch('time.time',return_value=NOW):
            data=guide.doctor(args,state)
        finding=next(f for f in data['findings'] if f['checkId']=='connectors.health')
        self.assertEqual(finding['severity'],'warning')
        self.assertEqual(finding['fixHint'],self.s.connector_reminder(c,NOW))

    def test_history_optional_last_good_keeps_mutable_returns_independent(self):
        s=self.s
        items=[{'id':'tmdb:1','at':1000}]
        self.write('history',{'items':items})
        with patch.object(s,'CONNECTOR_HISTORY_LAST_GOOD',True):
            first=s.read_history()
            first.clear()
            (self.data/'history.json').write_text('{')
            self.assertEqual(s.read_history(),items)
            (self.data/'history.json').unlink()
            self.assertEqual(s.read_history(),items)
        self.assertEqual(s.read_history(),[])

    def test_backup_policy_switch_is_shared_by_server_and_cli(self):
        import importlib.util
        with patch.dict(os.environ,{'SKALD_CONNECTOR_BACKUP':'0'}):
            s=load_server(self.data)
            try:
                spec=importlib.util.spec_from_file_location('connector_backup',backup.__file__)
                local=importlib.util.module_from_spec(spec);spec.loader.exec_module(local)
                self.assertEqual(s.BACKUP_FILES,local.BACKUP_FILES)
                self.assertNotIn('connectors.json',s.BACKUP_FILES)
                self.assertIn('connectors.json',s.BACKUP_SKIP)
            finally:
                close_server(s)

    def test_surface_policy_can_fold_rows_into_metrics(self):
        import importlib.util
        source=Path(self.s.__file__).read_text().replace('CONNECTOR_SEPARATE_ROUTE = True','CONNECTOR_SEPARATE_ROUTE = False')
        path=self.data/'surface.py';path.write_text(source)
        spec=importlib.util.spec_from_file_location('connector_surface',path)
        s=importlib.util.module_from_spec(spec)
        with patch.dict(os.environ,{'TMDB_API':'offline','DATA_DIR':str(self.data),'OPENROUTER_API_KEY':''}):
            spec.loader.exec_module(s)
        try:
            self.assertEqual(len(s.API_ROUTES),12)
            self.assertNotIn('connectors',s.API_ROUTES)
            view=s.API_ROUTES['metrics']['methods']['GET'][1]
            with patch.object(s,'metrics_view',return_value={'events':0}), patch.object(s,'connectors_view',return_value={'connectors':[]}):
                self.assertEqual(view({}),{'events':0,'connectors':[]})
        finally:
            close_server(s)

    def test_manual_fixture_seeds_and_new_import_discovery(self):
        s = self.s
        fixture = Path(__file__).parent/'fixtures/events/data/imports.jsonl'
        (self.data/'imports.jsonl').write_bytes(fixture.read_bytes())
        records = s.read_imports()
        seeds = s.connector_seeds(NOW)
        pairs = {(e['src'],e['who']) for e in records}
        self.assertTrue(pairs)
        self.assertEqual(sum(c['kind']=='manual_export' for c in seeds.values()),len(pairs))
        for src,who in pairs:
            c = seeds[f'manual:{src}:{who}']
            self.assertEqual(c['cursor'],max(e['t'] for e in records if (e['src'],e['who'])==(src,who)))
        self.tick()
        self.assertTrue(set(seeds) <= set(s._connectors))
        e={**records[0],'id':'f'*32}
        cid=f"manual:{e['src']}:{e['who']}"
        count=s._connectors[cid]['counts']['events']
        path=self.data/'imports.jsonl'
        path.write_text(path.read_text()+json.dumps(e)+'\n')
        with patch.object(s,'_connectors_save',side_effect=AssertionError('counts alone do not write')):
            self.tick(NOW+1)
        self.assertEqual(s._connectors[cid]['counts']['events'],count+1)

    def test_stale_thresholds_and_recovery(self):
        s = self.s
        path = self.data/'history.json'
        os.utime(path,(NOW-s.CONNECTOR_STALE['history'],NOW-s.CONNECTOR_STALE['history']))
        self.tick()
        self.assertEqual(s._connectors['history']['health']['state'],'stale')
        self.tick(NOW+s.CONNECTOR_STALE['history'])
        self.assertEqual(s._connectors['history']['health']['state'],'stale')
        path = self.data/'history.json'
        os.utime(path, ns=(path.stat().st_atime_ns,path.stat().st_mtime_ns+1))
        self.tick(NOW+s.CONNECTOR_STALE['history']+1)
        self.assertEqual(s._connectors['history']['health']['state'],'ok')
        c = s._connectors['trakt']
        c['last_new'] = NOW-30*s.DAY
        self.tick(NOW+1)
        self.assertEqual(s._connectors['trakt']['health']['state'],'stale')

    def test_raw_auth_rejected_load_and_save_no_secret_resolution(self):
        s = self.s
        tokenfile = self.data/'token-canary'
        tokenfile.write_text(CANARY)
        rows = copy.deepcopy(s._connectors)
        rows['history']['auth']=CANARY
        rows['trakt']['auth']='file:token-canary'
        self.write('connectors', {'version':1,'connectors':rows})
        original_open = open
        def guarded(path, *args, **kw):
            if isinstance(path,(str,os.PathLike)):
                self.assertNotEqual(Path(path).name,tokenfile.name)
            return original_open(path,*args,**kw)
        with patch.dict(os.environ, {'TRAKT_TOKEN':CANARY}), patch('builtins.open',side_effect=guarded), redirect_stdout(io.StringIO()) as logs:
            s.connectors_load(NOW)
            view = s.connectors_view()
            rows['history']['auth']=CANARY
            s._connectors_save(rows,s._connectors_generation)
            with tempfile.TemporaryDirectory() as state:
                for flags in ([],['--json']):
                    cli=io.StringIO()
                    self.assertEqual(main(['--snapshot',str(self.data),'--state-dir',state,*flags,'connectors'],out=cli),0)
                    self.assertNotIn(CANARY,cli.getvalue())
        c = s._connectors['history']
        self.assertIsNone(c['auth'])
        self.assertEqual(c['health']['error'],'AuthNotReference')
        for value in ((self.data/'connectors.json').read_text(),json.dumps(view),logs.getvalue()):
            self.assertNotIn(CANARY,value)
        self.assertIn('connector history',logs.getvalue())
        self.assertNotIn('env:TRAKT_TOKEN',json.dumps(view))
        self.assertNotIn('file:token-canary',json.dumps(view))

    def test_api_guest_person_filter_and_auth_mask(self):
        s = self.s
        s._connectors['private'] = s._connector('private','api',s.PEOPLE[0],NOW,auth='env:SYNTHETIC')
        for person in (*s.PEOPLE,s.GUEST):
            token = s.new_client('connector-'+person,['read'],person)
            code, _, raw, _ = raw_request(s,'/api/v1/connectors',headers={'Authorization':'Bearer '+token})
            if person == s.GUEST:
                self.assertEqual(code,403)
                continue
            self.assertEqual(code,200)
            rows = json.loads(raw)['connectors']
            self.assertTrue(all(c['person'] in (person,'both') for c in rows))
            self.assertNotIn('env:',raw.decode() if isinstance(raw,bytes) else raw)
            self.assertTrue(all(c['auth'] in (None,'ref') for c in rows))

    def test_threaded_demand_does_not_wait_on_home_feeds_or_poll_io(self):
        s = self.s
        entered, release = threading.Event(), threading.Event()
        original = s.poll
        def slow(c,now):
            entered.set()
            release.wait(2)
            return original(c,now)
        with s._home_lock, s._feeds_lock, patch.object(s,'poll',side_effect=slow):
            worker = threading.Thread(target=self.tick)
            worker.start()
            self.assertTrue(entered.wait(1))
            elapsed=[]
            def demand():
                start=time.perf_counter()
                s.connectors_demand(NOW+1)
                elapsed.append((time.perf_counter()-start)*1000)
            caller=threading.Thread(target=demand)
            caller.start(); caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertLess(elapsed[0],5)
            release.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(s._connectors['history']['next_due'],NOW+1)

    def test_cli_derived_local_registry_content_ages_and_canary(self):
        state = self.data/'state'; state.mkdir()
        self.write('profile', {'entries':[]})
        row = {'file':'synthetic.csv','status':'applied','at':'2026-09-01',
               'counts':{'candidates':1,'accepted':1,'review':0},'added':[]}
        (state/'onboard.json').write_text(json.dumps({'v':1,'people':{self.s.PEOPLE[0]:{'plain':row}}}))
        (self.data/'connectors.json').unlink()
        args=parser().parse_args(['connectors'])
        with patch('time.time',return_value=NOW), patch.dict(os.environ,{'TRAKT_TOKEN':CANARY}), Engine(self.data) as engine:
            result=execute(args,engine,state,self.data)
            self.assertEqual(next(c for c in result['connectors'] if c['id']=='history')['origin'],'derived')
            local=next(c for c in result['connectors'] if c['origin']=='local')
            self.assertEqual(local['health']['state'],'stale')
            out=io.StringIO(); text_output(args,result,out)
            self.assertIn('id | kind | person | health | last new | next due | reminder',out.getvalue())
            self.assertIn('(local)',out.getvalue())
            self.assertNotIn(CANARY,out.getvalue()+json.dumps(result))
        s=self.s
        s._connectors['trakt']['last_new']=NOW-40*s.DAY
        s._connectors_save(s._connectors,s._connectors_generation)
        with Engine(self.data) as engine:
            c=next(c for c in guide.connectors(engine,state)['connectors'] if c['id']=='trakt')
            self.assertEqual(c['last_new'],NOW-40*s.DAY)
            self.assertEqual(c['origin'],'registry')

    def test_backup_classification_and_data_files(self):
        self.assertIn('connectors.json',DATA_FILES)
        self.assertIn('connectors.json',self.s.BACKUP_FILES)
        self.assertEqual(self.s.BACKUP_FILES,backup.BACKUP_FILES)
        self.assertIsNone(self.s.BACKUP_SECRET.search('connectors.json'))


if __name__ == '__main__':
    unittest.main()
