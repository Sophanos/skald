"""Calendar gates use synthetic themes and fake transports only."""
import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from decide_kit import check_contract, close_server, load_server, reset
import test_w108_source as sources


class Seasons(unittest.TestCase):
    setUpClass = sources.ThemedSources.__dict__['setUpClass']
    setUp = sources.ThemedSources.setUp
    source = sources.ThemedSources.source
    resolve = sources.ThemedSources.resolve

    def llm(self, path, body, timeout, lane, name):
        self.calls.append((path, body, timeout, lane, name))
        answer = {'months': self.answer['months']} if name == 'theme_season' else self.answer
        return {'choices': [{'message': {'content': json.dumps(answer)}}]}

    def build(self):
        xs = [sources.card(i) for i in range(1, 21)]
        self.m['aff'] = {'k:1': 1}
        with patch.object(self.s, 'lane_titles', return_value=[(x['id'], 'movie', x) for x in xs]), \
                patch.object(self.s, 'lane_title', return_value=self.theme['name']):
            return self.s.lanes_feed('de-DE')

    def winter_build(self):
        self.theme['source'] = ['k:1', self.m['names']['k:1']]
        self.answer.update(months=[12], keep=list(range(20)),
                           why={str(i): 'A patient advocate untangles an impossible case.' for i in range(20)})
        with patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 12, 14, 18)):
            out = self.build()
        self.s._feeds[self.s.LANES[0], 'de-DE'] = {
            'metas': out[self.s.LANES[0]], 'names': out['_names'][self.s.LANES[0]]}
        self.s._theme_sources.clear()
        return out

    def test_winter_window_stored_and_reuse_hides_in_october_shows_in_december(self):
        out = self.winter_build()
        self.assertEqual(out['_names'][self.s.LANES[0]]['months'], [12])
        with patch.object(self.s, 'openrouter', side_effect=OSError('synthetic provider failure')):
            hidden = self.build()
            self.assertEqual(hidden[self.s.LANES[0]]['tv'], [])
            self.assertEqual(hidden['_names'][self.s.LANES[0]]['months'], [12])
            with patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 12, 15, 18)):
                self.assertEqual(len(self.build()[self.s.LANES[0]]['tv']), 20)

    def test_offseason_lane_keeps_naming_metadata(self):
        self.answer.update(months=[12], fits_now=False, keep=[], why={})
        names = self.build()['_names'][self.s.LANES[0]]
        self.assertEqual(names.get('name'), self.m['names']['k:1'])
        self.assertIs(names.get('maker'), False)

    def test_cap_fallback_obeys_known_window_and_any_time(self):
        self.winter_build()
        s = self.s
        xs = [sources.card(i) for i in range(1, 21)]
        with patch.object(s, 'openrouter', side_effect=AssertionError('no paid calls')):
            self.assertEqual(s.themed_row(self.m, self.p, self.theme, xs, 'de-DE', s.LANES[0], allow_llm=False), [])
            with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 12, 15, 18)):
                self.assertEqual(len(s.themed_row(self.m, self.p, self.theme, xs, 'de-DE', s.LANES[0], allow_llm=False)), 20)
        self.theme.update(key='k:2', source=['k:2', 'lawyer'])
        self.answer.update(months='any')
        self.source(xs)
        self.assertEqual(len(s.themed_row(self.m, self.p, self.theme, xs, 'de-DE', s.LANES[0], allow_llm=False)), 20)

    def test_source_keeps_season_and_animation_policy(self):
        self.answer.update(months=[12], anime_lane=True)
        with patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 12, 14, 18)):
            xs = self.source()
        self.assertEqual(self.s.theme_months(self.theme), [12])
        self.assertTrue(xs[0]['_row'].get('anime_lane'))

    def test_unknown_season_fallback_judged_once_across_restart(self):
        s = self.s
        self.answer['months'] = [12]
        self.assertEqual(s.themed_row(self.m, self.p, self.theme, [sources.card(1)], 'de-DE', s.LANES[0], allow_llm=False), [])
        self.assertEqual([x[-1] for x in self.calls], ['theme_season'])
        s._learn.setdefault('served', {})
        s.save_learn()
        s.flush_decisions()
        other = load_server(s.DATA)
        self.addCleanup(close_server, other)
        with patch.object(other, 'openrouter', side_effect=AssertionError('season must survive restart')):
            other.load_state()
            self.assertEqual(other.theme_months(self.theme, allow_llm=True), [12])
        self.assertTrue(json.loads(Path(s.DATA, 'learn.json').read_text())['seasons'])

    def test_ai_down_keeps_unknown_theme_fallback(self):
        with patch.object(self.s, 'openrouter', side_effect=OSError('synthetic provider failure')):
            xs = self.s.themed_row(self.m, self.p, self.theme, [sources.card(1)], 'de-DE', self.s.LANES[0], allow_llm=False)
        self.assertEqual([x['id'] for x in xs], ['tmdb:1'])

    def test_season_only_uses_remaining_shared_theme_budget(self):
        s = self.s
        self.answer['months'] = [12]
        s._usage['calls']['feed:theme_source'] = 11
        with patch.object(s, 'openrouter', wraps=self.llm) as paid:
            self.assertEqual(s.decide('theme_season', {k: self.theme[k] for k in ('kind', 'name', 'source')})['answer']['months'], [12])
        self.assertEqual(paid.call_count, 1)
        s._usage['calls']['feed:theme_season'] = 1
        self.assertEqual(s.decide_stats()['theme_source']['cap'], 0)
        self.theme['name'] = 'Synthetic second theme'
        with patch.object(s, 'openrouter', side_effect=AssertionError('shared cap spent')):
            self.assertEqual(s.decide('theme_season', {k: self.theme[k] for k in ('kind', 'name', 'source')})['why'], 'cap')
            self.assertEqual(s.decide('theme_source', {'kind': 'lane', 'theme': self.theme['name'], 'source': [],
                'date': '2026-10-14', 'language': 'en-US', 'taste': 'synthetic', 'candidates': []})['why'], 'cap')

    def test_new_offseason_theme_keeps_window_for_diagnostics(self):
        self.answer.update(months=[12], fits_now=False, keep=[], why={})
        out = self.build()
        self.assertEqual(out[self.s.LANES[0]]['tv'], [])
        self.assertEqual(out['_names'][self.s.LANES[0]]['months'], [12])

    def test_invalid_season_windows_are_unknown_and_never_persisted(self):
        for months in ([], [0], [13], [True], [12, 12], None):
            with self.subTest(months=months):
                self.s._decide_cache.clear()
                self.answer['months'] = months
                result = self.s.decide('theme_season', {k: self.theme[k] for k in ('kind', 'name', 'source')})
                self.assertEqual(result['source'], 'fallback')
                self.assertIsNone(result['answer']['months'])

    def test_malformed_source_window_keeps_valid_cards_and_previous_window(self):
        s = self.s
        self.theme['source'] = ['k:1', self.m['names']['k:1']]
        self.answer.update(keep=list(range(20)), why={str(i): 'A patient advocate untangles an impossible case.' for i in range(20)})
        for stored in (None, 'any', [12]):
            for months in ('missing', None, [], [13], [True], [12, 12], '12'):
                with self.subTest(stored=stored, months=months):
                    reset(s)
                    s._theme_sources.clear()
                    s._learn.pop('seasons', None)
                    if stored is not None:
                        s.theme_months(self.theme, stored)
                    self.answer.pop('months', None)
                    if months != 'missing':
                        self.answer['months'] = months
                    with patch.object(s, 'local_now', return_value=datetime.datetime(2026, 12, 14, 18)):
                        out = self.build()
                    self.assertEqual(len(out[s.LANES[0]]['tv']), 20)
                    self.assertEqual(out['_names'][s.LANES[0]]['months'], stored)
                    self.assertTrue(s._row_source_status[s.LANES[0], 'de-DE']['fresh'])

    def test_build_finishes_lane_leap_and_mood_sourcing_before_season_only_work(self):
        s = self.s
        self.m['aff'] = {f'k:{i}': 41-i for i in range(1, 41)}
        self.m['names'] = {f'k:{i}': f'Synthetic Theme {i}' for i in range(1, 41)}
        self.p['entries'] = [{'key': 'tmdb:99', 'kind': 'movie', 'title': 'Synthetic Seed',
                              'signal': 'loved', 'weight': 4, 'by': ['ana']}]
        specs = s.job_specs(s.local_now(), {})
        order, sourced = [], []
        def run(job, build, lang):
            order.append(job)
            return build(lang) if job in ('lanes', 'leap', 'moods') else {}
        def reply(req, **kw):
            body = json.loads(req.data)
            inp = json.loads(body['messages'][1]['content'])
            if 'candidates' not in inp:
                answer = {'months': 'any'}
            else:
                sourced.append((inp['kind'], inp['theme']))
                count = len(inp['candidates'])
                answer = {'fits_now': True, 'months': 'any' if inp['kind'] == 'lane' else None,
                          'keep': list(range(count)), 'add': [],
                          'why': {str(i): 'A patient advocate untangles an impossible case.' for i in range(count)}}
            return sources.io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}],
                                                  'usage': {'cost': .001}}).encode())
        def pool(m, p, tags, lang, **kw):
            start = 1000 + list(s.MOODS).index(next(k for k, v in s.MOODS.items() if v[1] == tags)) * 100 if 'extra' not in kw else 100
            return [(x['id'], 'movie', x) for x in (sources.card(i) for i in range(start, start + 20))]
        with patch.object(s, 'job_specs', return_value=[(job, cids, stamp, gap, lambda l, j=job, b=build: run(j, b, l))
                                                       for job, cids, stamp, gap, build in specs]), \
                patch.object(s, '_langs', {'de-DE'}), patch.object(s, 'pin_decision'), \
                patch.object(s, 'connectors_tick'), patch.object(s, 'save_feeds'), \
                patch.object(s, 'lane_titles', return_value=[('tmdb:1', 'movie', sources.card(1))]), \
                patch.object(s, 'feeling_pool', side_effect=pool), patch.object(s, 'pick_moods', return_value=[]), \
                patch.object(s, 'openrouter', side_effect=self.transport), \
                patch.object(s, '_connector_open', side_effect=reply):
            s.build_feeds(s.local_now(), {}, {}, force=True)
        self.assertEqual(order, [job for job, *_ in specs])
        self.assertEqual([kind for kind, _ in sourced], ['lane'] * 8 + ['leap'] + ['mood'] * 3)
        self.assertEqual(s._usage['calls']['feed:theme_source'], 12)
        self.assertEqual(s._usage['calls'].get('feed:theme_season', 0), 0)

    def test_deferred_season_judges_selected_fallback_and_stores_its_window(self):
        s = self.s
        self.m['aff'] = {f'k:{i}': 41-i for i in range(1, 41)}
        self.m['names'] = {f'k:{i}': f'Synthetic Theme {i}' for i in range(1, 41)}
        def titles(fid, lang):
            xs = [sources.card(i) for i in range(1, 21)] if fid == 'k:40' else [sources.card(1)]
            return [(x['id'], 'movie', x) for x in xs]
        def reply(req, **kw):
            inp = json.loads(json.loads(req.data)['messages'][1]['content'])
            self.calls.append(inp)
            answer = self.answer if 'candidates' in inp else {'months': [12]}
            return sources.io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}],
                                                  'usage': {'cost': .001}}).encode())
        with patch.object(s, '_langs', {'de-DE'}), patch.object(s, 'connectors_tick'), \
                patch.object(s, 'lane_titles', side_effect=titles), patch.object(s, 'pin_decision'), \
                patch.object(s, 'openrouter', side_effect=self.transport), patch.object(s, '_connector_open', side_effect=reply):
            s.build_feeds(s.local_now(), {}, {}, only={'lanes'}, force=True)
        rec = s._feeds[s.LANES[0], 'de-DE']
        self.assertEqual(rec['names']['lane'], 'k:40')
        self.assertEqual(rec['names']['months'], [12])
        self.assertEqual(len(rec['metas']['tv']), 20)
        self.assertEqual(s._usage['calls']['feed:theme_season'], 1)
        self.assertEqual(self.calls[-1]['source'][0], 'k:40')
        other = load_server(s.DATA)
        self.addCleanup(close_server, other)
        other.load_state()
        for month, count in ((10, 0), (12, 20)):
            with patch.object(other, 'local_now', return_value=datetime.datetime(2026, month, 14, 18)):
                self.assertEqual(len(other.row_list(other.LANES[0], 'tv', 'de-DE')), count)

    def test_source_judges_season_once_and_persists_without_manual_save(self):
        self.answer['months'] = 'any'
        self.source()
        self.assertTrue(json.loads(Path(self.s.DATA, 'learn.json').read_text())['seasons'])
        with patch.object(self.s, 'local_now', return_value=datetime.datetime(2026, 10, 15, 18)):
            self.source()
        inp = json.loads(self.calls[-1][1]['messages'][1]['content'])
        self.assertEqual(inp['months'], 'any')
        self.assertEqual([x[-1] for x in self.calls], ['theme_source', 'theme_source'])

    def test_season_decision_fixture_contract(self):
        with patch.object(self.s, 'openrouter', self.transport):
            check_contract(self, self.s, 'theme_season')
