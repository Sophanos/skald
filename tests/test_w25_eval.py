"""Evaluation contracts using only invented titles and temporary evidence."""
from array import array
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skald.engine import Engine
from skald.members import fresh_config
from skald.__main__ import execute, parser
from eval import stop_report, rows_report
from eval.profile_golden import ROOT, offline_server
from test_stop_report import entry


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        config = fresh_config(['eva', 'oskar'])
        config['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True,
            'accounts': {'eva': 'file:eva.json', 'oskar': 'file:oskar.json'}}
        self.write('skald.json', config)
        self.write('facets.json', {'items': {f'tmdb:{i}': {
            'kind': 'movie', 'title': f'Invented Lantern {i}', 'y': '2020',
            'runtime': 120, 'g': ['Drama'], 'k': [[i % 3, 'Invented motif']]}
            for i in range(1, 301)}})

    def write(self, name, value):
        (self.data/name).write_text(json.dumps(value))

    def engine(self):
        with redirect_stdout(io.StringIO()):
            engine = Engine(self.data)
        self.addCleanup(engine.close)
        return engine

    def test_cached_and_raw_profiles_agree_on_stop_rule_version(self):
        raw = dict(id='tmdb:1', type='movie', title='Invented Lantern 1', year='2020',
                   signal='abandoned', weight=-1.5, duration_min=1)
        self.write('trakt-eva.history.json', {'items': [raw]})
        self.write('trakt-oskar.history.json', {'items': [{**raw, 'id': 'tmdb:2',
                   'title': 'Invented Lantern 2', 'signal': 'disliked', 'weight': -3.}]})
        current = self.engine()
        version = current._server.LEARN_RULES
        old = entry(1, signal='abandoned', weight=-.5, abandon='far')
        old.update(who='eva', item=raw)
        rejection = entry(2, 'oskar', signal='disliked', weight=-3.)
        rejection['who'] = 'oskar'
        for stamp in ({}, {'LEARN_RULES': version - 1}):
            with self.subTest(stamp=stamp):
                self.write('profile.json', {'entries': [old, rejection], **stamp})
                rebuilt = self.engine()
                self.assertEqual(rebuilt.profile('eva')['entries'], current.profile('eva')['entries'])
                self.assertEqual(rebuilt._profile['entries'][0].get('abandon'), 'early')
                self.assertEqual(rebuilt.profile('oskar')['signals'], {'disliked': 1})
                self.assertEqual(rebuilt._base['LEARN_RULES'], version)
                self.assertEqual(rebuilt.provenance()['evidence'], 'raw-evidence')
        for name in ('trakt-eva.history.json', 'trakt-oskar.history.json'):
            (self.data/name).unlink()
        cached = self.engine()
        self.assertEqual(cached.profile('eva')['evidence'], 'cached-profile')
        self.assertEqual(cached.trace('tmdb:1', 'eva')['evidence'], 'cached-profile')
        report = stop_report.stop_report(self.data, 'eva')
        self.assertEqual(report['evidence'], 'cached-profile')
        self.assertFalse(all(t['pass'] for t in stop_report.targets(report)))

    def test_match_index_export_includes_rank_and_background_version(self):
        self.write('profile.json', {'entries': [entry(i) for i in range(1, 25)]})
        engine = self.engine()
        s, m = engine._server, engine._model
        m.update(cal=20, bg=[-1.] * 10 + [1.] * 10)
        m['pbg']['eva'] = [0.] * 10 + [2.] * 10
        with patch('skald.__main__.PEOPLE', ['eva', 'oskar']):
            args = parser().parse_args(['explain', 'tmdb:1', '--person', 'eva', '--json'])
        with patch.object(s, 'person_score', return_value=(0., [])):
            trace = execute(args, engine, None, None)
        self.assertEqual(trace['rank'], .25)
        self.assertEqual(trace['match'], 55)
        self.assertEqual(trace['background']['count'], 20)
        self.assertTrue(trace['background']['version'])
        self.assertEqual(trace['bonus'], s.facet_bonus(m, s.frec('tmdb:1', 'movie'), m['paff']['eva'])[0])
        old_version = trace['background']['version']
        m['pbg']['eva'][0] = -1.
        self.assertNotEqual(engine.trace('tmdb:1', 'eva')['background']['version'], old_version)
        m['pbg']['eva'] = []
        self.assertIsNone(engine.trace('tmdb:1', 'eva')['rank'])

    def test_engine_confidence_uses_candidate_kind_on_id_collision(self):
        facets = {f'tmdb:{i}': {'kind': 'movie', 'title': f'Invented Lantern {i}', 'y': '2020'}
                  for i in range(1, 301)}
        for i in (1, 2, 200):
            facets[f'tmdb:{i}']['k'] = [[7, 'Lantern craft']]
        facets['tmdb:200@tv'] = {'kind': 'tv', 'title': 'Invented Harbor', 'k': [[7, 'Lantern craft']]}
        self.write('facets.json', {'items': facets})
        for kind, label in (('movie', 'match.strong'), ('tv', 'match.good')):
            candidate = {**entry(200), 'kind': kind, 'stype': 'movie' if kind == 'movie' else 'series'}
            self.write('profile.json', {'entries': [entry(i) for i in range(1, 21)] + [candidate]})
            engine = self.engine()
            s = engine._server
            s._emb.update({key: array('f', values) for key, values in (
                ('tmdb:1', [1., 0.]), ('tmdb:2', [1., 0.]),
                ('tmdb:200', [1., 0.]), ('tmdb:200@tv', [0., 1.]))})
            engine._model = engine._build(engine._profile)
            s._model['m'] = engine._model
            with self.subTest(kind=kind, output='score'):
                score = engine.score('tmdb:200', 'eva')
                self.assertEqual((score['confidence'], score['percentile']), (s.t(label), 99))
            with self.subTest(kind=kind, output='report'):
                report = engine.report('eva')
                held = report['calibration']['loved_wildcard']['titles']
                self.assertEqual(next(e['label'] for e in held if e['key'] == 'tmdb:200'), s.t(label))

    def test_partial_watch_evidence_keeps_cached_explicit_love(self):
        watch = dict(id='tmdb:1', type='movie', title='Invented Lantern 1', year='2020',
                     signal='watched', weight=2.)
        self.write('trakt-eva.history.json', {'items': [watch]})
        love = dict(id='a'*32, t=1791734400., key='tmdb:1', kind='movie',
                    title='Invented Lantern 1', year='2020', who='eva', action='love',
                    reasons=[], row=None, undo=None)
        (self.data/'feedback.jsonl').write_text(json.dumps(love)+'\n')
        complete = self.engine()
        self.assertEqual(complete.provenance()['evidence'], 'raw-evidence')
        self.assertEqual(complete.profile('eva')['signals'], {'loved': 1})
        self.write('profile.json', json.loads((complete._work/'profile.json').read_text()))
        rebuilt = self.engine()
        self.assertEqual(rebuilt.provenance()['evidence'], 'raw-evidence')
        self.assertEqual(rebuilt.profile('eva')['signals'], {'loved': 1})
        (self.data/'feedback.jsonl').unlink()
        partial = self.engine()
        self.assertEqual(partial.provenance()['evidence'], 'cached-profile')
        self.assertEqual(partial.profile('eva')['signals'], {'loved': 1})

    def test_heldout_feedback_cannot_leak_through_memory(self):
        self.write('profile.json', {'entries': [entry(i) for i in range(1, 25)]})
        feedback = dict(id='a'*32, t=1791734400., key='tmdb:1', kind='movie',
            title='Invented Lantern 1', year='2020', who='eva', action='love',
            reasons=[], row=None, undo=None)
        (self.data/'feedback.jsonl').write_text(json.dumps(feedback)+'\n')
        engine = self.engine()
        s = engine._server
        s.memory_tell('eva', 'like', 'k:1')
        memory = s.memory_load()
        memory['facts'][0].update(src='implicit', via='curate', evidence=['a'*32])
        s.memory_save(memory, [])
        engine._refresh()
        before = engine.score('tmdb:26', 'eva')
        with rows_report.heldout(engine, {'tmdb:1'}):
            self.assertNotIn('tmdb:1', {e['key'] for e in engine.profile('eva')['entries']})
            self.assertEqual(s.memory_load()['facts'], [])
            self.assertNotIn('tmdb:1', {key for key, who in s.active_feedback()})
            trace = engine.trace('tmdb:26', 'eva')
            self.assertNotIn('tmdb:1', {event['key'] for event in trace['events']})
            self.assertNotIn('Invented Lantern 1', json.dumps(trace.get('memory', {})))
        self.assertEqual(engine.score('tmdb:26', 'eva'), before)
        self.assertEqual(len(s.memory_load()['facts']), 1)

    def test_empty_row_cannot_pass_quality_gate(self):
        self.write('profile.json', {'entries': [entry(i) for i in range(1, 4)]})
        config = json.loads((self.data/'skald.json').read_text())
        config['household']['rows'] = ['toprated']
        self.write('skald.json', config)
        report = rows_report.quality_report(self.data, 'eva', k=10)
        self.assertEqual(report['fill']['returned'], 0)
        self.assertGreater(report['fill']['requested'], 0)
        self.assertEqual(report['negative_leakage']['slots'], 0)
        self.assertEqual(report['heldout']['recovered'], 0)
        self.assertFalse(all(t['pass'] for t in rows_report.quality_targets(report)))

    def test_final_row_counts_include_populated_dynamic_home_rows(self):
        config = json.loads((self.data/'skald.json').read_text())
        config['metrics'] = {'enabled': True}
        self.write('skald.json', config)
        self.write('lab.json', {'ideas': [dict(id='one', name='Invented Lanterns', state='trial',
            members=['eva'], months='any', moments=[], picks=[])]})
        self.write('profile.json', {'entries': [entry(1, signal='disliked', weight=-3.)]})
        engine = self.engine()
        s = engine._server
        dynamic = {s.MOODS_ROWS[0], s.LANES[0], s.DOCS[0], s.rid('lab_one')}
        cards = [dict(id=f'tmdb:{i}', type='movie', name=f'Invented Lantern {i}')
                 for i in range(1, s.ROW_MIN+1)]
        s._feeds.update({(cid, s.LANG): {'metas': {'tv': cards}, 'names': {}} for cid in dynamic})
        with patch.object(engine, 'close'), patch.object(rows_report, 'Engine', return_value=engine), \
                patch.object(s, 'served_row', side_effect=lambda cid, *a, **kw: cards if cid in dynamic else []):
            with s.addon_scope('eva'), patch.object(s, 'get_model', return_value=engine._model):
                production = s.feed_rows(s.LANG)
            self.assertTrue(dynamic <= {cid for cid, _, _ in production})
            requested = sum(1 if 'tv' in types else len(types) for _, types, _ in production)*2
            report = rows_report.quality_report(self.data, 'eva', k=2)
        self.assertEqual(report['fill'], {'returned': 8, 'requested': requested})
        self.assertEqual(report['negative_leakage'], {'slots': 4, 'returned': 8, 'known_negatives': 1})
        self.assertEqual(report['rows'], requested//2)

    def test_baseline_freezes_targets_and_counts_cohort_migration(self):
        stop = entry(1, signal='abandoned', weight=-.5, abandon='far')
        stop['who'] = 'eva'
        self.write('profile.json', {'entries': [stop, entry(2)]})
        baseline = stop_report.stop_report(self.data, 'eva')
        stop.update(abandon='early', weight=0.)
        self.write('profile.json', {'entries': [stop, entry(2)]})
        current = stop_report.stop_report(self.data, 'eva', baseline)
        self.assertEqual(current['separation']['stops_unsure_far']['n'], 0)
        self.assertEqual(current['fixed_separation']['stops_unsure_far']['n'], 1)
        self.assertIn({'from': 'stops_far', 'to': 'stops_early', 'n': 1}, current['cohort_migration'])
        self.assertNotIn('tmdb:', json.dumps(current))

    def test_stop_cohorts_keep_explicit_negatives_separate_from_inferred_stops(self):
        entries = [entry(1), entry(2, signal='liked', weight=2.),
                   entry(3, signal='watched', weight=1.), entry(4, signal='disliked', weight=-3.)]
        for n, reading in enumerate(('related', 'unsure', 'far', 'early', 'pending'), 5):
            e = entry(n, signal='abandoned', weight=-.5)
            e['who'] = 'eva'
            if reading:
                e['abandon'] = reading
            entries.append(e)
        entries.append(entry(10, signal='abandoned', weight=-1.5))
        self.write('profile.json', {'entries': entries})
        report = stop_report.stop_report(self.data, 'eva')
        self.assertEqual(report['cohorts']['explicit_negative']['n'], 2)
        self.assertEqual(report['cohorts']['stops_far']['n'], 1)
        self.assertEqual(report['cohorts']['stops_pending']['n'], 1)
        self.assertEqual(report['pairs']['n'], 6)

    def test_final_rows_recover_heldout_loves_without_exporting_titles(self):
        config = json.loads((self.data/'skald.json').read_text())
        config['household']['rows'] = ['top10']
        self.write('skald.json', config)
        facets = json.loads((self.data/'facets.json').read_text())
        for rec in facets['items'].values():
            rec['k'] = [[1, 'Invented motif']]
        self.write('facets.json', facets)
        feedback = [dict(id=f'{i:032x}', t=1791734400., key=f'tmdb:{i}', kind='movie',
            title=f'Invented Lantern {i}', year='2020', who='eva',
            action='not_for_us' if i == 50 else 'love', reasons=[], row=None, undo=None)
            for i in (1, 2, 3, 50)]
        (self.data/'feedback.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in feedback))
        engine = self.engine()
        s = engine._server
        cards = [dict(id=f'tmdb:{i}', name=f'Invented Lantern {i}', type='movie',
            poster='p', background='b', _row={'date': '2020-01-01', 'votes': 1000}) for i in range(1, 101)]
        self.write('feeds.json', {s.rid('top10')+'|'+s.LANG: {'metas': {'movie': cards, 'series': []}, 'names': {}}})
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        report = rows_report.quality_report(self.data, 'eva')
        self.assertEqual(report['evidence'], 'raw-evidence')
        self.assertGreater(report['fill']['returned'], 0)
        self.assertEqual(report['fill']['requested'], 20)
        self.assertEqual(report['negative_leakage']['known_negatives'], 1)
        self.assertEqual(report['negative_leakage']['slots'], 0)
        self.assertEqual(report['heldout'], {'n': 3, 'recovered': 3, 'recall_at_k': 1.})
        self.assertNotIn('tmdb:', json.dumps(report))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_deliberate_import_drop_is_negative_even_with_old_inferred_reading(self):
        event = dict(t='2026-10-15', id='a'*32, key='tmdb:1', kind='movie',
            title='Invented Lantern 1', year='2020', who='eva', action='abandon', src='imdb', conf=1., n=1)
        (self.data/'imports.jsonl').write_text(json.dumps(event)+'\n')
        report = stop_report.stop_report(self.data, 'eva')
        self.assertEqual(report['cohorts']['explicit_negative']['n'], 1)
        self.assertEqual(report['cohorts']['stops_far']['n'], 0)
        self.assertEqual(report['separation']['stops_unsure_far']['n'], 0)

    def test_deliberate_drop_after_play_counts_for_its_member_and_leakage(self):
        play = entry(1, signal='watched', weight=2.)
        play.update(who='tv', item={'progress': .8})
        other = entry(1, 'oskar', signal='abandoned', weight=-.5, abandon='far')
        other.update(who='tv', item={'progress': .2})
        self.write('profile.json', {'entries': [play, other]})
        engine = self.engine()
        s = engine._server
        drop = dict(id='a'*32, t=1791734400., key='tmdb:1', kind='movie',
                    title='Invented Lantern 1', year='2020', who='eva', action='abandon',
                    reasons=[], row=None, undo=None)
        # Supply the review's drop at the fold boundary; remote HTTP does not offer abandon.
        state = {'active': {('tmdb:1', 'eva'): drop}, 'tonight': {}, 'blocked': set()}
        card = {'id': 'tmdb:1', 'type': 'movie', 'name': 'Invented Lantern 1'}
        with patch.dict(s.ACTION_W, {('remote', 'abandon'): ('abandoned', -1.5)}), \
                patch.object(s, 'feedback_state', return_value=state), patch.object(engine, 'close'), \
                patch.object(stop_report, 'Engine', return_value=engine), \
                patch.object(rows_report, 'Engine', return_value=engine), \
                patch.object(s, 'feed_rows', return_value=[(s.rid('top10'), ('movie',), '')]), \
                patch.object(s, 'row_metas', return_value=[card]):
            engine._refresh()
            merged = s.person_entries(engine._profile, 'eva')[0]
            self.assertEqual(merged['who'], 'tv')
            self.assertIn('remote', merged['flags'])
            report = stop_report.stop_report(self.data, 'eva')
            self.assertEqual(report['cohorts']['explicit_negative']['n'], 1)
            self.assertEqual(report['cohorts']['stops_far']['n'], 0)
            self.assertEqual(rows_report.quality_report(self.data, 'eva')['negative_leakage'],
                             {'slots': 1, 'returned': 1, 'known_negatives': 1})
            self.assertEqual(stop_report.stop_report(self.data, 'oskar')['cohorts']['explicit_negative']['n'], 0)
            self.assertEqual(rows_report.quality_report(self.data, 'oskar')['negative_leakage']['slots'], 0)

    def test_heldout_raw_feedback_drops_derived_seen_state(self):
        event = dict(id='a'*32, t=1791734400., key='tmdb:1', kind='movie',
            title='Invented Lantern 1', year='2020', who='eva', action='love', reasons=[], row=None, undo=None)
        (self.data/'feedback.jsonl').write_text(json.dumps(event)+'\n')
        self.write('history-seen.json', {'tmdb:1': {}})
        engine = self.engine()
        self.assertEqual(engine.provenance()['evidence'], 'raw-evidence')
        with rows_report.heldout(engine, {'tmdb:1'}):
            self.assertNotIn('tmdb:1', engine._profile['seen'])

    def test_heldout_explanations_do_not_remove_an_unrelated_seed(self):
        self.write('profile.json', {'entries': [entry(1), entry(10)]})
        engine = self.engine()
        s = engine._server
        row = s.rid('because'), s.LANG
        s._feeds[row] = {'names': {'seed': 'tmdb:10'}, 'metas': {'tv': [
            {'id': 'tmdb:20', 'type': 'movie', 'description': 'Saved explanation', '_memory': {'why': 'Saved reason'}}]}}
        with rows_report.heldout(engine, {'tmdb:1'}):
            self.assertIn(row, s._feeds)
            card = s._feeds[row]['metas']['tv'][0]
            self.assertNotIn('description', card)
            self.assertNotIn('_memory', card)

    def test_raw_evidence_can_remove_a_stale_cached_entry(self):
        old = entry(1)
        old['who'] = 'import'
        self.write('profile.json', {'LEARN_RULES': 0, 'entries': [old]})
        event = dict(t='2026-10-15', id='a'*32, key='tmdb:1', kind='movie',
            title='Invented Lantern 1', year='2020', who='eva', action='love', src='imdb', conf=0., n=1)
        (self.data/'imports.jsonl').write_text(json.dumps(event)+'\n')
        engine = self.engine()
        self.assertEqual(engine.provenance()['evidence'], 'raw-evidence')
        self.assertEqual(engine.profile('eva')['entries'], [])

    def test_told_seeds_are_raw_evidence_without_watch_files(self):
        old = entry(1, weight=.5)
        old.update(who='told', title='Invented Lantern 1')
        self.write('profile.json', {'entries': [old]})
        self.write('taste.json', {'people': {'eva': {'loves': ['Invented Lantern 1|2020|m']}}})
        engine = self.engine()
        self.assertEqual(engine.provenance()['evidence'], 'raw-evidence')
        self.assertEqual(engine._base['entries'][0]['weight'], engine._server.SEED_WEIGHT)

    def test_row_fill_also_counts_enabled_rewatch_rows(self):
        config = json.loads((self.data/'skald.json').read_text())
        config['household']['rows'] = ['top10', 'rewatch']
        self.write('skald.json', config)
        self.write('profile.json', {'entries': [entry(1)]})
        report = rows_report.quality_report(self.data, 'eva')
        self.assertEqual(report['rows'], 3)
        self.assertEqual(report['fill']['requested'], 30)

    def test_engine_can_close_twice_without_waiting_for_a_stopped_worker(self):
        result = subprocess.run([sys.executable, '-c',
            'from skald.engine import Engine; import sys; e = Engine(sys.argv[1]); e.close(); e.close()', str(self.data)],
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_heldout_movie_is_not_recovered_by_a_series_with_the_same_id(self):
        config = json.loads((self.data/'skald.json').read_text())
        config['household']['rows'] = ['top10']
        self.write('skald.json', config)
        self.write('profile.json', {'entries': [entry(1)]})
        facets = json.loads((self.data/'facets.json').read_text())
        facets['items']['tmdb:1@tv'] = {'kind': 'tv', 'title': 'Invented Series', 'y': '2020', 'g': ['Drama']}
        self.write('facets.json', facets)
        engine = self.engine()
        s = engine._server
        card = dict(id='tmdb:1', name='Invented Series', type='series', poster='p', background='b',
                    _row={'date': '2020-01-01', 'votes': 1000})
        self.write('feeds.json', {s.rid('top10')+'|'+s.LANG: {'metas': {'movie': [], 'series': [card]}, 'names': {}}})
        self.assertEqual(rows_report.quality_report(self.data, 'eva')['heldout']['recovered'], 0)

    def test_profile_replay_stops_decision_writer_before_removing_data(self):
        cleanup, observed, servers = tempfile.TemporaryDirectory.cleanup, [], []
        def guarded_cleanup(temp):
            s = servers[0]
            active = s.flush_decisions(timeout=.1)
            observed.append(active)
            if active:
                s._decide_queue.put(False)
                s._decide_queue.join()
            cleanup(temp)
        with patch.object(tempfile.TemporaryDirectory, 'cleanup', guarded_cleanup):
            with offline_server(ROOT/'server.py') as server:
                servers.append(server)
        self.assertEqual(observed, [False])


if __name__ == '__main__':
    unittest.main()
