"""Row Lab mechanics with invented titles and offline transports."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, ReplayLLM
from skald import members


def at(value):
    return dt.datetime.fromisoformat(value).timestamp()


def idea(identity='one', start=1, **fields):
    return dict(id=identity, name='Quiet Puzzles ' + identity, state='trial', members=['together'],
                months='any', moments=[], picks=[dict(key=f'tmdb:{i}', kind='movie') for i in range(start, start+10)]) | fields


class Lab(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = members.fresh_config(['ana', 'ben'])
        self.cfg['region']['tz'] = 'Europe/Berlin'
        self.cfg['household']['member_rows'] = ['ana', 'ben']
        self.cfg['metrics'] = {'enabled': True}
        (self.root/'skald.json').write_text(json.dumps(self.cfg))
        self.s = s = load_server(self.root)
        self.addCleanup(close_server, s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(s, 'tmdb', side_effect=AssertionError('TMDB forbidden')))
        self.enterContext(patch.object(s, 'ensure_facets', side_effect=AssertionError('enrichment forbidden')))
        self.enterContext(patch.object(s, 'ensure_emb', side_effect=AssertionError('embedding forbidden')))
        self.now = at('2026-10-14T18:00:00+02:00')
        self.enterContext(patch.object(s.time, 'time', side_effect=lambda: self.now))
        s._profiles['household'] = {'entries': [], 'seen': set(), 'negative': set()}
        for i in range(1, 301):
            s._facets[f'tmdb:{i}'] = dict(title=f'Invented Lantern {i}', kind='movie', y='2020',
                g=['Mystery'], x={'cozy_case': 1}, runtime=None, vote_count=300, vote_average=8,
                cr={'US': 'PG'}, adult=False)
        self.enterContext(patch.object(s, 'get_model', return_value={'cal': True, 'aff': {}, 'paff': {}, 'names': {}}))
        self.enterContext(patch.object(s, 'person_score', side_effect=lambda m, k, *a: (1-int(k[5:])/1000, [])))
        self.enterContext(patch.object(s, 'match_pct', return_value=90))
        self.enterContext(patch.object(s, 'tone_ok', return_value=True))

    def test_cached_manifest_never_gets_replacement_concept(self):
        from skald import lab
        s = self.s
        doc = {'ideas': [idea()], 'attempt_day': None}
        lab.save(self.root, doc)
        cid = s.rid('lab_one')
        first = s.feed(cid, s.LANG)
        s._shown[cid, s.LANG] = copy.deepcopy(first)
        s._shown_scoped['ana'] = {(cid, s.LANG): copy.deepcopy(s._shown[cid, s.LANG])}
        doc['ideas'][0]['picks'] = idea(start=20)['picks']
        lab.save(self.root, doc)
        self.assertEqual(s.feed(cid, s.LANG)['names']['title'], 'Quiet Puzzles one')
        self.assertEqual(s.feed(cid, s.LANG)['metas']['tv'][0]['id'], 'tmdb:20')
        doc['ideas'][0]['state'] = 'retired'
        doc['ideas'].append(idea('two', 40))
        lab.save(self.root, doc)
        for scope in (None, 'ana'):
            with s.addon_scope(scope):
                self.assertEqual(s.feed(cid, s.LANG), {})
                self.assertEqual(s.feed(s.rid('lab_two'), s.LANG)['names']['title'], 'Quiet Puzzles two')
        restarted = load_server(self.root)
        self.addCleanup(close_server, restarted)
        self.assertEqual(restarted.feed(cid, restarted.LANG), {})
        self.assertEqual(restarted.feed(restarted.rid('lab_two'), restarted.LANG)['names']['title'], 'Quiet Puzzles two')

    def test_moment_boundaries_dst(self):
        from skald import metrics
        for stamp, bucket in [('2026-10-16T16:59:59+02:00', 'day'), ('2026-10-16T17:00:00+02:00', 'weekend'),
                              ('2026-10-18T21:59:59+02:00', 'weekend'), ('2026-10-18T22:00:00+02:00', 'late'),
                              ('2026-10-19T03:59:59+02:00', 'late'), ('2026-10-19T04:00:00+02:00', 'day'),
                              ('2026-10-19T17:00:00+02:00', 'evening'), ('2026-10-19T22:00:00+02:00', 'late'),
                              ('2026-10-25T02:30:00+02:00', 'late'), ('2026-10-25T02:30:00+01:00', 'late'),
                              ('2026-10-25T04:00:00+01:00', 'weekend'), ('2026-11-01T04:00:00+01:00', 'weekend')]:
            with self.subTest(stamp=stamp):
                self.assertEqual(metrics.moment(self.cfg, at(stamp)), (bucket, int(stamp[5:7])))

    def test_lab_uses_epoch_in_utc_container(self):
        from skald import lab
        s = self.s
        self.addCleanup(s.time.tzset)
        self.enterContext(patch.dict(os.environ, {'TZ': 'UTC'}))
        s.time.tzset()
        for zone, stamp, bucket in [('Europe/Berlin', '2026-10-16T15:30:00+02:00', 'day'),
                                    ('America/New_York', '2026-10-16T18:30:00-04:00', 'weekend')]:
            with self.subTest(zone=zone):
                self.now = at(stamp)
                self.cfg['region']['tz'] = s.CONFIG['region']['tz'] = s.TZ = zone
                self.cfg['household']['rows'] = s.CONFIG['household']['rows'] = ['lab']
                (self.root/'skald.json').write_text(json.dumps(self.cfg))
                lab.save(self.root, {'ideas': [idea('day', state='kept', moments=['day']),
                    idea('weekend', 20, state='kept', moments=['weekend'])]})
                self.assertIsNone(s.local_now().tzinfo)
                cid = s.rid('lab_'+bucket)
                with patch.object(lab, 'select', wraps=lab.select) as select:
                    s.lab_catalogs()
                    self.assertEqual(dict(s._lab_visible)[None], (bucket,))
                    home = s._home_rows(s.LANG, 'both')
                    self.assertEqual({row for (row, typ), xs in home.items() if typ == 'tv' and xs}, {cid})
                    self.assertEqual([r[0] for r in s.feed_rows(s.LANG, who='both', home=home)], [cid])
                    self.assertTrue(s.served_row(cid, 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), 'both'))
                    self.assertTrue(select.call_args_list)
                    self.assertTrue(all(c.args[2] == self.now for c in select.call_args_list))
                self.assertEqual(s.lab_stamp()[-1], (bucket, 10))

    def shown(self, stamp, row='lab_one', n=1, person='ana'):
        from skald import metrics
        metrics.record_shown(self.root, self.cfg, self.s.rid(row), person,
                             [{'id': f'tmdb:{n}', 'type': 'movie'}], at(stamp))

    def play(self, stamp, n=1, person='ana'):
        from skald import metrics
        metrics.update(self.root, self.cfg, [dict(key=f'tmdb:{n}', kind='movie', person=person,
            source='history', at=at(stamp))], at(stamp))

    def test_multiple_moments_survive_fold(self):
        from skald import metrics
        self.shown('2026-10-14T12:00:00+02:00')
        self.shown('2026-10-14T18:00:00+02:00')
        metrics.update(self.root, self.cfg, [], at('2026-12-01T12:00:00+01:00'), cleanup=True)
        saved = json.loads((self.root/'metrics.json').read_text())
        cell = saved['weeks']['2026-10-12'][self.s.rid('lab_one')]['ana']
        self.assertEqual(cell['extra']['days'], ['2026-10-14'])
        self.assertEqual({k: v['shown'] for k, v in cell['extra']['moments'].items()}, {'day': 1, 'evening': 1})
        self.assertEqual(cell['shown'], 1)

    def test_play_moment_differs_from_showing(self):
        from skald import metrics
        self.shown('2026-10-14T12:00:00+02:00')
        self.play('2026-10-14T23:00:00+02:00')
        cell = json.loads((self.root/'metrics.json').read_text())['weeks']['2026-10-12'][self.s.rid('lab_one')]['ana']
        context = next(iter(cell['extra']['play_contexts'].values()))
        self.assertEqual(context['moment'], 'late')
        self.assertEqual(context['at'], at('2026-10-14T23:00:00+02:00'))
        report = metrics.report(self.root, self.cfg, at('2026-10-15T12:00:00+02:00'), moment='late')
        self.assertEqual(report['rows'][0]['played'], 1)
        self.assertEqual(report['rows'][0]['shown'], 0)

    def test_legacy_totals_unchanged(self):
        from skald import metrics
        record = dict(week='2026-10-12', row='old', person='ana', times=[self.now], sources=['history'], play='opaque')
        weeks = {}
        metrics._fold(weeks, record)
        self.assertEqual(weeks['2026-10-12']['old']['ana'], dict(shown=1, played=1, sources={'history': 1},
            complete=True, closed=False, extra={'plays': ['opaque']}))

    def lab_state(self, **fields):
        from skald import lab
        lab.save(self.root, {'ideas': [idea(**fields)], 'attempt_day': None})

    def test_duplicate_fetches_count_one_day(self):
        from skald import lab
        self.lab_state()
        for day in range(1, 11):
            for hour in (12, 13, 18):
                self.shown(f'2026-10-{day:02}T{hour}:00:00+02:00')
        result = lab.sync(self.root, self.cfg, self.now)['ideas'][0]
        self.assertEqual(result['showing_days'], 10)
        self.assertEqual(result['state'], 'retired')
        self.assertEqual(result['retirement'], 'automatic')
        self.assertEqual(lab.sync(self.root, self.cfg, self.now), lab.sync(self.root, self.cfg, self.now))

    def test_retirement_resets_after_play(self):
        from skald import lab
        self.lab_state(manual_keep=at('2026-09-01T12:00:00+02:00'))
        for day in range(1, 31):
            self.shown(f'2026-09-{day:02}T18:00:00+02:00')
        result = lab.sync(self.root, self.cfg, self.now)
        self.assertEqual(result['ideas'][0]['state'], 'retired')
        lab.save(self.root, result)
        self.play('2026-09-30T19:00:00+02:00')
        kept = lab.sync(self.root, self.cfg, self.now)['ideas'][0]
        self.assertEqual(kept['state'], 'kept')
        self.assertEqual(kept['inactivity_days'], 0)
        self.assertEqual(kept['moments'], ['evening'])

    def test_delayed_credit_respects_manual_retirement(self):
        from skald import lab
        for manual in (False, True):
            self.lab_state(state='retired', retirement='manual' if manual else 'automatic')
            self.shown('2026-10-14T12:00:00+02:00')
            self.play('2026-10-14T23:00:00+02:00')
            result = lab.sync(self.root, self.cfg, self.now + 86400)['ideas'][0]
            self.assertEqual(result['state'], 'retired' if manual else 'kept')
            self.assertEqual(result['moments'], ['late'])

    def test_metrics_off_pauses_lab(self):
        from skald import lab
        self.lab_state()
        self.cfg['metrics']['enabled'] = False
        self.s.CONFIG['metrics']['enabled'] = False
        self.assertEqual(lab.sync(self.root, self.cfg, self.now), lab.read(self.root))
        self.assertEqual(lab.select(self.root, self.cfg, self.now), [])
        self.assertEqual(self.s.lab_feed(self.s.LANG), {})
        self.assertTrue(lab.view(self.root, self.cfg, self.now)['paused_reason'])

    def test_lab_config_validation(self):
        for options in ({'enabled': 'yes'}, {'trial_showing_days': 0}, {'kept_showing_days': True}, {'unknown': 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                members.validate_config({**self.cfg, 'lab': options})
        self.assertEqual(members.public_config({**self.cfg, 'lab': {'enabled': False}})['lab'], {'enabled': False})

    def test_forget_and_incomplete_evidence_cannot_retire_or_learn(self):
        from skald import lab, metrics
        self.lab_state()
        for day in range(1, 11):
            self.shown(f'2026-10-{day:02}T12:00:00+02:00')
        self.play('2026-10-10T23:00:00+02:00')
        self.assertEqual(lab.sync(self.root, self.cfg, self.now)['ideas'][0]['state'], 'kept')
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now))
        metrics.forget(self.root, self.cfg, '2026-10-11', 'ana', apply=True)
        result = lab.sync(self.root, self.cfg, self.now)['ideas'][0]
        self.assertEqual(result['moments'], [])
        self.assertEqual(result['plays'], 0)
        self.assertEqual(result['state'], 'kept')

    def test_candidate_pool_never_enriches(self):
        s = self.s
        s._profiles['household']['seen'].add('tmdb:1')
        s._facets['tmdb:2@tv'] = {**s._facets['tmdb:2'], 'kind': 'tv'}
        with patch.object(s, 'build_home', return_value={(s.rid('foryou'), 'tv'): [
                {'id': 'tmdb:3', 'type': 'movie'}]}):
            pool = s.lab_candidates()
        self.assertEqual(len(pool), 250)
        tokens = {c['token'] for c in pool}
        self.assertNotIn('movie:1', tokens)
        self.assertNotIn('movie:3', tokens)
        self.assertIn('movie:2', tokens)
        self.assertIn('tv:2', tokens)
        self.assertIsNone(pool[0]['runtime'])

    def proposal_input(self):
        return dict(day='2026-10-14', language='en', moment='evening', month=10, capacity=2,
                    members=['ana', 'ben', 'together'], home_names=['Already Home'], outcomes=[], taste=[], refills=[],
                    candidates=[dict(token=f'movie:{i}', title=f'Invented Lantern {i}', year='2020', kind='movie',
                                     runtime=None, genres=['Mystery'], experience=['cozy_case'], match=90) for i in range(1, 31)])

    def proposal(self, name='Quiet Puzzles', picks=None, **fields):
        return dict(id=None, name=name, members=['together'], months='any',
                    picks=list(range(10)) if picks is None else picks) | fields

    def test_proposal_validation(self):
        s = self.s
        proposals = [self.proposal(picks=[0, 0, 'movie:2', 'movie:999', *range(2, 10)]),
                     self.proposal('Too Short', [0, 1, 2]), self.proposal('Already Home'),
                     self.proposal('Bad Scope', members=['outsider']), self.proposal('Bad Months', months=[0]),
                     self.proposal('Everyday Name', months=[11, 12])]
        reply = {'choices': [{'message': {'content': json.dumps({'ideas': proposals})}}]}
        result = s.lab_parse(reply, [self.proposal_input()])[0][0]['ideas']
        self.assertEqual([x['name'] for x in result], ['Quiet Puzzles', 'Everyday Name'])
        self.assertEqual(result[0]['picks'], [f'movie:{i}' for i in range(1, 11)])

    def test_daily_attempt_survives_restart_and_failure(self):
        from skald import lab
        s = self.s
        with patch.object(s, 'lab_candidates', return_value=self.proposal_input()['candidates']), \
                patch.object(s, 'build_home', return_value={}):
            with ReplayLLM(s) as replay:
                replay.arm({'choices': [{'message': {'content': 'broken synthetic response'}}]})
                s.lab_job(self.now)
                self.assertEqual(replay.calls, 1)
            self.assertEqual(lab.read(self.root)['attempt_day'], '2026-10-14')
            restarted = load_server(self.root)
            self.addCleanup(close_server, restarted)
            with ReplayLLM(restarted) as replay:
                restarted.lab_job(self.now)
                self.assertEqual(replay.calls, 0)
            s.CONFIG['metrics']['enabled'] = False
            with patch.object(s, 'decide', side_effect=AssertionError('paused lab called AI')):
                s.lab_job(self.now + 86400)
            self.assertEqual(lab.read(self.root)['attempt_day'], '2026-10-14')

    def test_lab_home_caps_scope_windows_and_seen(self):
        from skald import lab
        s = self.s
        doc = {'ideas': [idea(str(i), i*20+1, state='trial' if i < 3 else 'kept',
                              months=[10], moments=['evening'] if i >= 3 else [], members=['ana']) for i in range(6)]}
        lab.save(self.root, doc)
        s.CONFIG['household']['rows'] = ['people', 'lab']
        s.lab_catalogs()
        self.assertEqual(list(s.row_pool(s.LANG)), [])
        s._profiles['household']['seen'].add('tmdb:1')
        s._facets['tmdb:2']['adult'] = True
        with s.addon_scope('ana'):
            home = s.build_home(s.LANG, s._profiles['household'], (set(), set(), set()), 'ana')
            visible = [cid for (cid, typ), xs in home.items() if typ == 'tv' and xs and cid.startswith(s.rid('lab_'))]
            self.assertEqual(len(visible), 3)
            self.assertEqual(sum(cid in (s.rid('lab_0'), s.rid('lab_1'), s.rid('lab_2')) for cid in visible), 2)
            self.assertNotIn('tmdb:1', {x['id'] for xs in home.values() for x in xs})
            self.assertNotIn('tmdb:2', {x['id'] for xs in home.values() for x in xs})
            rows = s.feed_rows(s.LANG, home=home)
            self.assertEqual([r[0] for r in rows if r[0].startswith(s.rid('lab_'))], visible)
            last_personal = max(i for i, cid in enumerate(s.DEDUPE_ORDER) if cid in s.ROW_OWNER)
            self.assertEqual(s.DEDUPE_ORDER[last_personal+1], s.rid('lab_0'))
        with s.addon_scope('ben'):
            self.assertFalse(any(s.build_home(s.LANG, s._profiles['household'], (set(), set()), 'ben').values()))
        self.assertEqual(s.served_row(s.rid('lab_0'), 'tv', s.LANG, {}, set(), (set(), set()), s.GUEST), [])
        s.CONFIG['taste']['people']['ana']['prefs'] = {'kids_age': 10}
        self.assertEqual(s.served_row(s.rid('lab_0'), 'tv', s.LANG, {}, set(), (set(), set()), 'ana'), [])
        s.CONFIG['taste']['people']['ana'].pop('prefs')
        self.now = at('2026-10-15T12:00:00+02:00')
        self.assertEqual(s.served_row(s.rid('lab_3'), 'tv', s.LANG, {}, set(), (set(), set()), 'ana'), [])
        self.now = at('2026-11-15T18:00:00+01:00')
        self.assertEqual(s.served_row(s.rid('lab_0'), 'tv', s.LANG, {}, set(), (set(), set()), 'ana'), [])

    def animation_veto(self):
        s = self.s
        s._facets['tmdb:1']['g'] = ['Animation']
        s._memory = {**s._memory_empty(), 'seq': 1, 'facts': [dict(person='ana', status='active',
            kind='avoid', src='told', strength=1., about='g:Animation')]}
        self.enterContext(patch.object(s, 'openrouter', side_effect=AssertionError('LLM forbidden')))

    def test_catalog_drop_together_uses_shared_memory_veto(self):
        self.animation_veto()
        s = self.s
        p, cid = s._profiles['household'], s.rid('foryou')
        for person in ('together', 'both', *s.PEOPLE):
            with self.subTest(person=person):
                self.assertEqual(s.catalog_drop(s.known_meta('tmdb:1', 'movie'), p, cid, person),
                                 None if person == 'ben' else 'filtered')
                self.assertIsNone(s.catalog_drop(s.known_meta('tmdb:2', 'movie'), p, cid, person))

    def test_served_together_lab_applies_memory_veto(self):
        self.animation_veto()
        self.lab_state()
        s = self.s
        cid, p = s.rid('lab_one'), s._profiles['household']
        with s.addon_scope('together'):
            self.assertEqual(len(s.row_list(cid, 'tv', s.LANG)), 10)
            cards = s.served_row(cid, 'tv', s.LANG, p, set(), (set(), set()), 'both')
        self.assertEqual({x['id'] for x in cards}, {f'tmdb:{n}' for n in range(2, 11)})

    def test_together_regular_catalog_picks_apply_memory_veto(self):
        self.animation_veto()
        s = self.s
        with s.addon_scope('together'):
            for person in (None, 'together'):
                with self.subTest(person=person):
                    cards = s.catalog_picks(s._profiles['household'], s.get_model(False), s.rid('foryou'), person)
                    self.assertEqual(len(cards), s.POOL_CATALOG)
                    self.assertNotIn('tmdb:1', {x['id'] for x in cards})
                    self.assertIn('tmdb:2', {x['id'] for x in cards})

    def test_together_lab_applies_shared_catalog_filters(self):
        s = self.s
        picks = [dict(key=f'tmdb:{n}', kind='movie') for n in range(1, 9)]
        self.lab_state(picks=picks)
        for pick in picks:
            s._facets[pick['key']]['g'] = ['Animation']
        cid, p = s.rid('lab_one'), s._profiles['household']
        for scope in (None, 'together'):
            for filtered in ('hide', 'excluded_genres', 'seen', 'negative'):
                with self.subTest(scope=scope, filtered=filtered), s.addon_scope(scope):
                    s.CONFIG['taste']['people']['ana']['prefs'] = (
                        {'hide': ['animation']} if filtered == 'hide' else
                        {'excluded_genres': {'movie': ['Animation']}} if filtered == 'excluded_genres' else {})
                    p['seen'] = {x['key'] for x in picks} if filtered == 'seen' else set()
                    p['negative'] = {x['key'] for x in picks} if filtered == 'negative' else set()
                    raw = s.row_list(cid, 'tv', s.LANG)
                    self.assertEqual(len(raw), 8)
                    self.assertTrue(all(s.catalog_drop(x, p, s.rid('foryou'), 'both') for x in raw))
                    self.assertEqual(s.served_row(cid, 'tv', s.LANG, p, set(), (set(), set()), 'both'), [])

    def test_lab_build_with_plain_saga_ids(self):
        s = self.s
        self.cfg['household']['rows'] = s.CONFIG['household']['rows'] = ['lab']
        (self.root/'skald.json').write_text(json.dumps(self.cfg))
        self.lab_state()
        record = {'metas': {'movie': [s.known_meta('tmdb:1', 'movie')], 'sagas': ['tmdb:1']}}
        s._feeds[s.rid('rewatch'), s.LANG] = copy.deepcopy(record)
        s._shown[s.rid('rewatch'), s.LANG] = copy.deepcopy(record)
        s._shown_scoped['ana'] = {(s.rid('rewatch'), s.LANG): copy.deepcopy(record)}
        p = s._profiles['household']
        self.assertTrue(s.catalog_picks(p, s.get_model(False), s.rid('lab_one')))
        self.assertEqual(s._needed_emb([]), {('tmdb:1', 'movie')})
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps({'ideas': [self.proposal()]})}}]})
            s.lab_job(self.now)
            self.assertEqual(replay.calls, 1)
        home = s.build_home(s.LANG, p, (set(), set()), 'both')
        self.assertEqual(sum(bool(xs) for (cid, typ), xs in home.items() if typ == 'tv'), 2)

    def test_unlisted_lab_catalogs_return_empty(self):
        s = self.s
        self.cfg['household']['rows'] = s.CONFIG['household']['rows'] = ['lab']
        (self.root/'skald.json').write_text(json.dumps(self.cfg))
        handler = object.__new__(s.Handler)
        handler.send_json = lambda code, body, *args, **kwargs: (code, body)
        cid = s.rid('lab_one')
        for case in ('unknown', 'retired', 'moment', 'month', 'scope', 'child', 'disabled', 'too_few'):
            with self.subTest(case=case):
                fields = {'state': 'kept', 'moments': ['day']} if case == 'moment' else (
                    {'state': 'retired'} if case == 'retired' else {'months': [11]} if case == 'month' else
                    {'members': ['ana']} if case == 'scope' else {'picks': []} if case == 'too_few' else {})
                self.lab_state(**fields)
                s.CONFIG['lab'] = {'enabled': case != 'disabled'}
                s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 10} if case == 'child' else {}
                requested = s.rid('lab_old') if case == 'unknown' else cid
                scope = 'ben' if case in ('scope', 'child') else None
                with s.addon_scope(scope):
                    self.assertNotIn(requested, {c['id'] for c in s.manifest(s.LANG, scope)['catalogs']})
                    for typ in s.MIX:
                        self.assertEqual(handler.addon_get([s.LANG, 'catalog', typ, requested+'.json'], s.LANG, scope),
                                         (200, {'metas': []}))
        self.assertEqual(handler.addon_get([s.LANG, 'catalog', 'tv', s.rid('unknown')+'.json'], s.LANG, None),
                         (404, {'error': 'not found'}))
        self.assertEqual(handler.addon_get([s.LANG, 'catalog', 'invalid', cid+'.json'], s.LANG, None),
                         (404, {'error': 'not found'}))

    def test_lab_cycle(self):
        from skald import lab, metrics
        s = self.s
        s.CONFIG['household']['rows'] = ['lab']
        self.cfg['household']['rows'] = ['lab']
        (self.root/'skald.json').write_text(json.dumps(self.cfg))
        answer = {'ideas': [self.proposal(), self.proposal('Wild Puzzles', list(range(10, 20)))]}
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            s.lab_job(self.now)
            self.assertEqual(replay.calls, 1)
        doc = lab.read(self.root)
        self.assertEqual(len(doc['ideas']), 2)
        first, second = doc['ideas']
        cid = s.rid('lab_'+first['id'])
        home = s.build_home(s.LANG, s._profiles['household'], (set(), set()), 'ana')
        self.assertGreaterEqual(len(home[cid, 'tv']), 8)
        with patch.object(s, 'pin_shown'):
            manifest = s.manifest(s.LANG)
        self.assertIn(cid, {c['id'] for c in manifest['catalogs']})
        metrics.record_shown(self.root, self.cfg, cid, 'ana', home[cid, 'tv'], self.now)
        play_time = at('2026-10-14T23:00:00+02:00')
        metrics.update(self.root, self.cfg, [dict(key=home[cid, 'tv'][0]['id'], kind='movie', person='ana', source='history', at=play_time)], play_time)
        self.now = play_time
        s.lab_job(self.now)
        kept = lab.read(self.root)['ideas'][0]
        self.assertEqual((kept['state'], kept['plays'], kept['moments']), ('kept', 1, ['late']))
        self.assertTrue(s.served_row(cid, 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), 'ana'))
        self.now = at('2026-10-15T18:00:00+02:00')
        self.assertEqual(s.served_row(cid, 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), 'ana'), [])
        second_cid = s.rid('lab_'+second['id'])
        for day in range(15, 25):
            metrics.record_shown(self.root, self.cfg, second_cid, 'ana', [dict(id=second['picks'][0]['key'], type='movie')],
                                 at(f'2026-10-{day}T18:00:00+02:00'))
        self.now = at('2026-10-24T19:00:00+02:00')
        s.lab_job(self.now)
        retired = lab.read(self.root)['ideas'][1]
        self.assertEqual((retired['state'], retired['showing_days'], retired['plays']), ('retired', 10, 0))
        self.assertEqual(s.feed(second_cid, s.LANG), {})

    def test_moments_excludes_undated_imports(self):
        from skald import metrics
        self.shown('2026-10-14T12:00:00+02:00')
        metrics.update(self.root, self.cfg, [dict(key='tmdb:1', kind='movie', person='ana', source='import:netflix', at=None)], self.now)
        result = metrics.moments_report(self.root, self.cfg, self.now)
        self.assertEqual(result['undated_imports'], 1)
        self.assertEqual(result['rows'][0]['moments']['day'], {'showings': 1, 'plays': 0})
        self.play('2026-10-14T23:00:00+02:00')
        result = metrics.moments_report(self.root, self.cfg, self.now+86400)
        self.assertEqual(result['rows'][0]['moments']['late'], {'showings': 0, 'plays': 1})

    def test_lab_controls_preview_confirm_undo(self):
        from skald import commands, lab
        self.lab_state()
        s = self.s
        def preview(op, values=None):
            return commands.preview(s, 'synthetic control', 'ana', answer=dict(op=op, kind=None, values=values or [], person=None))
        for op in ('lab_keep', 'lab_retire', 'lab_off', 'lab_on'):
            before = lab.read(self.root)
            cfg_before = commands.config(s).get('lab')
            plan = preview(op, ['Quiet Puzzles one'] if op in ('lab_keep', 'lab_retire') else [])
            self.assertIn('plan', plan)
            self.assertEqual(lab.read(self.root), before)
            commands.apply(s, plan)
            if op in ('lab_keep', 'lab_retire'):
                self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'kept' if op == 'lab_keep' else 'retired')
            else:
                self.assertEqual(commands.config(s)['lab']['enabled'], op == 'lab_on')
            commands.undo(s, ['ana'])
            self.assertEqual(lab.read(self.root), before)
            self.assertEqual(commands.config(s).get('lab'), cfg_before)
        for op in ('lab_status', 'lab_moments'):
            self.assertTrue(preview(op)['read_only'])
        doc = lab.read(self.root)
        doc['ideas'].append(idea('two', 20, name='Quiet Puzzles one'))
        lab.save(self.root, doc)
        self.assertTrue(preview('lab_keep', ['Quiet Puzzles one'])['ask'])
        commands.apply(s, preview('lab_retire', ['one']))
        with self.assertRaises(ValueError):
            preview('lab_keep', ['one'])

    def test_cli_lab_reads_and_confirmed_controls_are_offline(self):
        import io
        import os
        from skald import lab
        from skald.__main__ import main
        self.lab_state()
        with patch.dict(os.environ, {'DATA_DIR': str(self.root)}):
            for argv in (['rows', 'lab', '--json'], ['rows', 'moments', '--json'],
                         ['rows', 'lab', '--keep', 'one', '--json']):
                before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
                out = io.StringIO()
                self.assertEqual(main(argv, out=out), 0, out.getvalue())
                self.assertTrue(json.loads(out.getvalue()))
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, before)
            out = io.StringIO()
            self.assertEqual(main(['rows', 'lab', '--keep', 'one', '--yes', '--json'], out=out), 0, out.getvalue())
            self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'kept')

    def test_commander_resolves_stop_and_ambiguity_via_model(self):
        from skald import commands
        self.lab_state()
        s = self.s
        for text, op in [('stop trying new rows', 'lab_off'), ('Was probiert Skald gerade aus?', 'lab_status')]:
            with ReplayLLM(s) as replay:
                replay.arm({'choices': [{'message': {'content': json.dumps(dict(op=op, kind=None, values=[], person=None))}}]})
                plan = commands.preview(s, text, 'ana')
                self.assertEqual(plan.get('change', {}).get('op', op), op)
                self.assertEqual(replay.calls, 1)
        self.assertIn({'id': s.rid('lab_one'), 'label': 'Quiet Puzzles one'}, __import__('skald.rowplan', fromlist=['command_input']).command_input(s, '')['rows'])

    def test_lab_locales(self):
        from skald import i18n
        keys = ['lab.'+k for k in ('title', 'empty', 'paused', 'trial', 'kept', 'retired', 'keep', 'retire', 'on', 'off', 'moments')]
        keys += ['moment.'+m for m in ('day', 'evening', 'late', 'weekend')]
        for lang in ('en', 'de', 'sr-latn', 'sr-cyrl'):
            for key in keys:
                self.assertIn(key, i18n._STRINGS[lang])
        from skald.__main__ import parser
        with patch.dict(members.CONFIG['region'], {'lang': 'de'}):
            rows = next(a for a in parser()._actions if a.dest == 'command').choices['rows']
            actions = next(a for a in rows._actions if a.dest == 'rows_action')
            self.assertIn('Eine Idee behalten', actions.choices['lab'].format_help())

    def test_manual_keep_plan_stable_and_baseline_starts_on_apply(self):
        from skald import lab
        self.lab_state()
        first = lab.preview(self.root, self.cfg, 'keep', 'one', self.now)
        self.assertEqual(first, lab.preview(self.root, self.cfg, 'keep', 'one', self.now+60))
        with patch('time.time', return_value=self.now+120):
            lab.apply(self.root, self.cfg, first)
        self.assertEqual(lab.read(self.root)['ideas'][0]['manual_keep'], self.now+120)

    def test_storage_backup_prefix_and_optional_format(self):
        from skald import backup, lab, migrate, row_names
        from skald.engine import DATA_FILES
        self.assertIn('lab.json', DATA_FILES)
        self.assertIn('lab.json', backup.BACKUP_FILES)
        self.assertIn('lab.json', self.s.BACKUP_FILES)
        self.assertEqual(migrate.FORMAT, 5)
        cfg = {**self.cfg, 'row_prefix': 'invented'}
        self.lab_state()
        row = row_names.view(self.root, cfg)['rows'][-1]
        self.assertEqual(row['id'], members.rid('lab_one', cfg))
        self.assertEqual(row['label'], 'Quiet Puzzles one')
        self.assertEqual((self.root/'lab.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(lab.select(self.root, cfg, self.now)[0]['id'], 'one')

    def test_attribution_promotes_without_proposal(self):
        from skald import lab
        s = self.s
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        plays = [dict(key='tmdb:1', kind='movie', person='ana', source='history', at=at('2026-10-14T23:00:00+02:00'))]
        with patch.object(s.row_metrics, 'collect', return_value=(plays, {'history'})), \
                patch.object(s, 'decide', side_effect=AssertionError('learning must not propose')):
            s.update_row_metrics(at('2026-10-15T12:00:00+02:00'))
        self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'kept')

    def test_commander_exact_confirmation_and_undo(self):
        from skald import commands, lab
        s = self.s
        self.lab_state()
        with patch.object(commands, 'context', return_value=('app:ana', 'ana', 'ana')), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(dict(op='lab_keep', kind=None, values=[s.rid('lab_one')], person=None))}}]})
            commands.special(s, 'Keep the Quiet Puzzles one row', 'movie', routed=True)
            plan = commands.get_pending(s, 'app:ana')
            self.assertIsNotNone(plan)
            commands.special(s, 'yes', 'movie')
            self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'trial')
            commands.special(s, 'yes '+commands.confirm_code(plan), 'movie')
            self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'kept')
            commands.special(s, 'undo', 'movie')
            self.assertEqual(lab.read(self.root)['ideas'][0]['state'], 'trial')

    def test_refill_retains_identity_name_and_uses_one_call(self):
        from skald import lab
        s = self.s
        self.lab_state(state='kept', manual_keep=self.now, created=self.now)
        s._profiles['household']['seen'].update(f'tmdb:{i}' for i in range(1, 5))
        s.refresh_preferences()
        response = {'ideas': [self.proposal('Changed name', ['movie:'+str(i) for i in range(1, 11)], id='one')]}
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(response)}}]})
            s.lab_job(self.now)
            s.lab_job(self.now)
            self.assertEqual(replay.calls, 1)
            brief = json.loads(replay.bodies[0]['messages'][1]['content'])
            self.assertIn('Refills may retain their supplied existing picks.', replay.bodies[0]['messages'][0]['content'])
            self.assertEqual(brief['refills'][0]['id'], 'one')
            self.assertIn({'name': 'Quiet Puzzles one', 'state': 'kept', 'moments': []}, brief['outcomes'])
        row = lab.read(self.root)['ideas'][0]
        self.assertEqual((row['id'], row['name']), ('one', 'Quiet Puzzles one'))

    def test_lab_decision_contract(self):
        from decide_kit import check_contract
        check_contract(self, self.s, 'lab_propose')

    def test_multilingual_command_eval(self):
        from eval import commands_eval
        cases = [c for c in commands_eval.load_cases() if c['group'] == 'row lab']
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual({c['lang'] for c in cases}, {'en', 'de', 'sr-latn', 'sr-cyrl'})
        self.assertTrue(all(c['contract'] == 'now' for c in cases))
        report = commands_eval.evaluate(cases)[0]
        self.assertEqual(report['now_full_accuracy'], 1)
        def transport(url, body, key, timeout):
            text = body['messages'][1]['content']
            case = next(c for c in cases if text.startswith(c['text']))
            self.assertIn('Row context: ', text)
            return case['recorded']
        self.assertEqual(commands_eval.evaluate(cases, ['synthetic/model'], transport=transport)[0]['now_full_accuracy'], 1)

    def test_scoped_together_excludes_personal_lab_ideas(self):
        from skald import lab
        self.lab_state(members=['ana'])
        self.assertEqual(lab.select(self.root, self.cfg, self.now, 'together'), [])
        with self.s.addon_scope('ben'):
            self.assertFalse(self.s.row_scope_ok(self.s.rid('lab_one')))

    def test_restored_counts_cannot_promote(self):
        from skald import lab
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        self.play('2026-10-14T23:00:00+02:00')
        (self.root/'metrics_shown.json').unlink()
        result = lab.sync(self.root, self.cfg, self.now+86400)['ideas'][0]
        self.assertEqual((result['state'], result['plays']), ('trial', 0))

    def test_resume_preserves_a_kept_idea(self):
        from skald import lab, metrics
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        self.play('2026-10-14T23:00:00+02:00')
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now+86400))
        metrics.disable(self.root)
        result = lab.sync(self.root, self.cfg, self.now+86400)['ideas'][0]
        self.assertEqual(result['state'], 'kept')

    def test_manual_keep_undo_restores_inactivity_baseline(self):
        from skald import commands, lab
        s = self.s
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now))
        old = lab.read(self.root)['ideas'][0]['baseline']
        plan = commands.preview(s, '', 'ana', answer=dict(op='lab_keep', kind=None, values=['one'], person=None))
        commands.apply(s, plan)
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now+86400))
        commands.undo(s, ['ana'])
        self.assertEqual(lab.sync(self.root, self.cfg, self.now+86400)['ideas'][0]['baseline'], old)

    def test_manual_keep_survives_unchanged_syncs_and_undo(self):
        from skald import commands, lab
        s = self.s
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        self.play('2026-10-14T13:00:00+02:00')
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now))
        before = lab.read(self.root)['ideas'][0]
        plan = commands.preview(s, '', 'ana', answer=dict(op='lab_keep', kind=None, values=['one'], person=None))
        commands.apply(s, plan)
        for offset in (60, 120):
            lab.save(self.root, lab.sync(self.root, self.cfg, self.now+offset))
        commands.undo(s, ['ana'])
        self.assertEqual(lab.read(self.root)['ideas'][0], before)

    def test_new_play_after_manual_keep_updates_evidence(self):
        from skald import commands, lab
        s = self.s
        self.lab_state()
        self.shown('2026-10-14T12:00:00+02:00')
        self.play('2026-10-14T13:00:00+02:00')
        lab.save(self.root, lab.sync(self.root, self.cfg, self.now))
        plan = commands.preview(s, '', 'ana', answer=dict(op='lab_keep', kind=None, values=['one'], person=None))
        commands.apply(s, plan)
        self.shown('2026-10-14T18:30:00+02:00', n=2)
        self.play('2026-10-14T19:00:00+02:00', n=2)
        doc = lab.sync(self.root, self.cfg, self.now+86400)
        row = doc['ideas'][0]
        self.assertEqual((row['reason'], row['plays'], row['last_play']), ('play', 2, at('2026-10-14T19:00:00+02:00')))
        lab.save(self.root, doc)
        with self.assertRaisesRegex(ValueError, s.t('lab.changed')):
            commands.undo(s, ['ana'])

    def test_catalog_membership_change_schedules_aio_refresh(self):
        self.lab_state(state='kept', moments=['evening'])
        s = self.s
        with patch.object(s, 'aio_rows_changed') as refresh:
            s.lab_catalogs()
            self.assertEqual(refresh.call_count, 1)
            s.lab_catalogs()
            self.assertEqual(refresh.call_count, 1)
            self.now = at('2026-10-15T12:00:00+02:00')
            s.lab_catalogs()
            self.assertEqual(refresh.call_count, 2)

    def test_structural_bad_idea_does_not_discard_valid_neighbor(self):
        proposals = [self.proposal(id={}), self.proposal(members=[{}]), self.proposal()]
        reply = {'choices': [{'message': {'content': json.dumps({'ideas': proposals})}}]}
        ideas = self.s.lab_parse(reply, [self.proposal_input()])[0][0]['ideas']
        self.assertEqual([i['name'] for i in ideas], ['Quiet Puzzles'])

    def test_candidate_rankings_are_interleaved(self):
        s = self.s
        rankings = [[(1, [], f'tmdb:{n}', 'movie') for n in ids] for ids in ((1, 2), (3, 4), (5, 6))]
        with patch.object(s, 'known_rank', side_effect=rankings), patch.object(s, 'build_home', return_value={}):
            self.assertEqual([c['token'] for c in s.lab_candidates()], ['movie:1', 'movie:3', 'movie:5', 'movie:2', 'movie:4', 'movie:6'])

    def test_failed_reservation_never_calls_model(self):
        from skald import lab
        with patch.object(lab, 'save', side_effect=OSError('synthetic full disk')), \
                patch.object(self.s, 'decide', side_effect=AssertionError('unreserved AI call')) as decision:
            self.s.lab_job(self.now)
        decision.assert_not_called()

    def test_trial_counts_first_fetch_at_creation(self):
        from skald import lab
        self.lab_state(created=self.now)
        for day in range(14, 24):
            self.shown(f'2026-10-{day}T18:00:00+02:00')
        row = lab.sync(self.root, self.cfg, at('2026-10-23T19:00:00+02:00'))['ideas'][0]
        self.assertEqual((row['state'], row['showing_days'], row['inactivity_days']), ('retired', 10, 10))

    def test_complete_days_can_expire_despite_older_incomplete_counts(self):
        from skald import lab, metrics
        self.lab_state()
        self.shown('2026-09-01T12:00:00+02:00')
        metrics.forget(self.root, self.cfg, '2026-09-02', 'ana', apply=True)
        for day in range(1, 11):
            self.shown(f'2026-10-{day:02}T12:00:00+02:00')
        row = lab.sync(self.root, self.cfg, self.now)['ideas'][0]
        self.assertEqual((row['state'], row['showing_days']), ('retired', 10))

    def test_paused_names_plan_and_command_context_remain_useful(self):
        from skald import i18n, row_names, rowplan
        self.lab_state()
        s = self.s
        s.CONFIG['metrics']['enabled'] = False
        self.cfg['metrics']['enabled'] = False
        names = row_names.view(self.root, self.cfg)
        self.assertEqual(names['lab_paused_reason'], i18n.t('lab.paused', s.LANG))
        self.assertEqual(rowplan.label(s, s.rid('lab_one'), s.LANG), 'Quiet Puzzles one')
        self.assertIn(dict(id=s.rid('lab_one'), label='Quiet Puzzles one'), rowplan.command_input(s, '')['rows'])
        row = next(r for r in rowplan.plan(s)['rows'] if r['id'] == s.rid('lab_one'))
        self.assertEqual(row['reason'], i18n.t('lab.paused', s.LANG))

    def test_command_context_stays_bounded_with_a_long_lab_history(self):
        from skald import lab, rowplan
        s = self.s
        lab.save(self.root, {'ideas': [idea(state='kept')] + [idea('old'+str(i), state='retired') for i in range(120)]})
        data = rowplan.command_input(s, 'synthetic question')
        self.assertLessEqual(len(data['rows']), 100)
        self.assertIn(dict(id=s.rid('lab_one'), label='Quiet Puzzles one'), data['rows'])
        s.check(s.DECISIONS['parse_command']['inp'], data)

    def test_refill_batch_stays_bounded_as_kept_ideas_accumulate(self):
        from skald import lab
        s = self.s
        lab.save(self.root, {'ideas': [idea(str(i), state='kept', manual_keep=self.now) for i in range(120)]})
        with patch.object(s, 'build_home', return_value={}), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"ideas":[]}'}}]})
            s.lab_job(self.now)
            self.assertEqual(replay.calls, 1)
            brief = json.loads(replay.bodies[0]['messages'][1]['content'])
            self.assertEqual(len(brief['refills']), 3)
