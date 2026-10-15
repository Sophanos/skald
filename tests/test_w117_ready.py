"""Release marathons from invented titles and a frozen household, entirely offline."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr
from unittest.mock import patch

from decide_kit import ReplayLLM, close_server, load_server, reset
from skald import commands, members, rowplan
from skald.__main__ import main
from test_w108_rows import cards


TODAY = dt.datetime(2026, 10, 1, 12)


def entry(tid, person, kind='movie', **item):
    return dict(id=tid, key=f'tmdb:{tid}', title=f'Invented {tid}', year='2024', kind=kind,
                stype='movie' if kind == 'movie' else 'series', by=[person], signal='watched',
                weight=1., age=100, flags=[], genres=[], item=item)


class Ready(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['row_prefix'] = 'house42'
        self.data.joinpath('skald.json').write_text(json.dumps(cfg))
        self.s = s = load_server(self.data)
        self.openrouter = s.openrouter
        self.original_tmdb = s.tmdb
        self.original_remote_masks = s.remote_masks
        self.addCleanup(close_server, s)
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        for name in ('openrouter', 'ensure_emb', 'ensure_facets'):
            self.enterContext(patch.object(s, name, side_effect=AssertionError(name+' forbidden')))
        self.enterContext(patch.object(s, 'local_now', return_value=TODAY))
        self.enterContext(patch.object(s, 'get_model', return_value=None))
        self.enterContext(patch.object(s, 'who_watching', return_value=('none', None)))
        self.enterContext(patch.object(s, 'remote_masks', return_value=(set(), set())))
        self.enterContext(patch.object(s, 'veto_keys', return_value=set()))
        self.calls = []
        self.parts = [dict(id=101+i, title=f'Invented Ember {i+1}', release_date=date,
                           poster_path='/fake.jpg', genre_ids=[18], adult=False)
                      for i, date in enumerate(('2020-01-01', '2023-01-01', '2026-10-31'))]
        self.collections = {901: dict(id=901, name='Invented Ember Collection', parts=self.parts)}
        self.show = dict(id=201, name='Invented Orbit', first_air_date='2024-01-01', adult=False,
                         poster_path='/fake.jpg', genres=[dict(id=18, name='Drama')],
                         seasons=[dict(season_number=n, episode_count=4, air_date=date)
                                  for n, date in enumerate(('2024-01-01', '2025-01-01', '2026-10-11'), 1)])
        self.enterContext(patch.object(s, 'tmdb', side_effect=self.tmdb))
        self.profile(entry(101, 'ana'), entry(201, 'ben', 'tv', last_episode=[1, 2], episodes_played=2))

    def tmdb(self, path, **params):
        self.calls.append((path, params))
        if path.startswith('genre/'):
            return {'genres': [dict(id=18, name='Drama')]}
        if path.startswith('collection/'):
            return copy.deepcopy(self.collections[int(path.split('/')[1])])
        if path.startswith('movie/'):
            return {'belongs_to_collection': {'id': 901}}
        if path == 'tv/201':
            return copy.deepcopy(self.show)
        raise AssertionError('Unexpected TMDB call: '+path)

    def profile(self, *entries):
        self.s._profiles['household'] = dict(entries=list(entries), seen={e['key'] for e in entries}, negative=set())

    def build(self):
        s = self.s
        rows = s.ready_feed(s.LANG)
        names = rows.pop('_names', {})
        for cid, metas in rows.items():
            s._feeds[cid, s.LANG] = dict(metas=metas, names=names.get(cid, {}))
        return rows

    def view(self, person=None):
        from skald import ready
        return ready.view(self.s) if person is None else ready.view(self.s, person)

    def assert_private(self, data, viewer):
        for release in data['releases']:
            self.assertEqual(set(release['people']), {viewer} if viewer in self.s.PEOPLE else set())
            self.assertLessEqual(set(release['for']), {viewer})
            for part in release.get('parts', []):
                self.assertLessEqual(set(part['seen_by']), {viewer})
        self.assertNotIn('ben', json.dumps(data['releases']))

    def test_ready_projection_for_member_household_together_and_other_member(self):
        from skald import ready
        self.profile(entry(101, 'ana'), entry(102, 'ben'), entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.build()
        full = copy.deepcopy(self.view())
        for person in (None, 'household', 'together', 'ben'):
            with self.subTest(person=person):
                result = commands.preview(self.s, 'synthetic read', viewer='ana',
                                          answer=dict(commands.NONE, op='home_plan', values=['ready'], person=person))
                self.assert_private(result['ready'], 'ana')
                self.assertNotIn('ben', result['description'])
                movie = next(r for r in result['ready']['releases'] if r['kind'] == 'movie')
                self.assertEqual(movie['people']['ana'], dict(reason='watched_part', seen=['tmdb:101']))
                self.assertEqual(movie['parts'][0]['seen_by'], ['ana'])
                self.assertEqual(movie['parts'][1]['seen_by'], [])
        self.assertEqual(self.view(), full)
        self.assertEqual(set(full['releases'][0]['people']), {'ana', 'ben'})
        self.assertEqual(full['releases'][0]['people']['ben']['seasons'], [1, 2])
        self.assert_private(ready.view(self.s, 'ana'), 'ana')

    def test_ready_projection_for_child_and_anonymous_guest(self):
        s = self.s
        cfg = members.fresh_config(['ana', 'ben', 'kim'])
        cfg['row_prefix'] = 'house42'
        cfg['taste']['people']['kim']['prefs'] = {'kids_age': 8}
        self.enterContext(patch.object(s, 'CONFIG', cfg))
        self.enterContext(patch.object(s, 'PEOPLE', ('ana', 'ben', 'kim')))
        self.enterContext(patch.dict(s.SCOPES, {'kim': 'kim'}))
        self.enterContext(patch.dict(s.LABELS, {'kim': 'Kim'}))
        for tid, kind in ((101, 'movie'), (102, 'movie'), (103, 'movie'), (201, 'tv')):
            s._facets[f'tmdb:{tid}'] = dict(cr={'DE': '6', 'US': 'G'}, adult=False, kind=kind)
        self.profile(entry(101, 'ana'), entry(102, 'ben'), entry(101, 'kim'),
                     entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.build()
        for viewer in ('kim', 'guest', None):
            for person in (None, 'household', 'together', 'ben'):
                with self.subTest(viewer=viewer, person=person):
                    result = commands.preview(s, 'synthetic read', viewer=viewer,
                                              answer=dict(commands.NONE, op='home_plan', values=['ready'], person=person))
                    self.assert_private(result['ready'], viewer)
                    self.assertNotIn('ana', json.dumps(result))
        self.assertEqual(next(r for r in self.view('kim')['releases'] if r['kind'] == 'movie')['people']['kim']['seen'], ['tmdb:101'])

    def test_other_home_plan_paths_project_ready_release_summaries(self):
        self.build()
        for values, person in (([], None), (['ready1'], 'household')):
            with self.subTest(values=values):
                result = commands.preview(self.s, 'synthetic read', viewer='ana',
                                          answer=dict(commands.NONE, op='home_plan', values=values, person=person))
                releases = [r['release'] for r in result['home']['rows'] if r.get('release')]
                self.assertTrue(releases)
                self.assert_private({'releases': releases}, 'ana')
        self.assert_private({'releases': [rowplan.why(self.s, 'ready1', 'ana')['release']]}, 'ana')

    def test_remote_ready_answer_and_odin_card_use_projected_view(self):
        s = self.s
        self.build()
        self.enterContext(patch.object(s, 'openrouter', self.openrouter))
        change = dict(commands.NONE, op='home_plan', values=['ready'])
        with ReplayLLM(s) as fake:
            fake.arm({'choices': [{'message': {'content': json.dumps(change)}}]})
            result = commands.api_preview(s.command_context(), dict(viewer='ana', client={'name': 'synthetic'},
                                          body={'in': {'query': 'synthetic ready read'}}))['preview']
            fake.arm({'choices': [{'message': {'content': json.dumps(change)}}]})
            with s.addon_scope('ana'):
                card = commands.special(s.command_context(), 'synthetic ready read', 'movie', routed=True)['metas'][0]
        self.assert_private(result['ready'], 'ana')
        self.assertNotIn('ben', card['description'])
        self.assertIn('ana: watched_part', card['description'])

    def test_cli_ready_json_projects_explicit_person_and_keeps_operator_view(self):
        self.build()
        s = self.s
        self.data.joinpath('feeds.json').write_text(json.dumps({cid+'|'+lang: rec for (cid, lang), rec in s._feeds.items()}))
        self.data.joinpath('profile.json').write_text(json.dumps(s._profiles['household'], default=list))
        for person in (None, 'ana', 'guest', 'household', 'together'):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(person=person), redirect_stderr(err):
                code = main(['rows', 'ready', '--json', *(['--person', person] if person else [])], out=out)
                self.assertEqual(code, 0, err.getvalue())
                data = json.loads(out.getvalue())
                self.assertIn('cause', data)
                if person is None:
                    self.assertEqual(set(data['releases'][0]['people']), {'ana', 'ben'})
                    self.assertEqual(data['releases'][0]['people']['ben']['seasons'], [1, 2])
                else:
                    self.assert_private(data, person)
        s.CONFIG['household']['rows'] = ['catchup']
        self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
        out = io.StringIO()
        self.assertEqual(main(['rows', 'ready', '--person', 'ana', '--json'], out=out), 0)
        self.assertEqual(json.loads(out.getvalue())['cause'], 'disabled')

    def assert_empty(self, cause, english, german, person='ana'):
        from skald import ready
        for lang, line in (('en', english), ('de', german)):
            with self.subTest(lang=lang):
                self.s.CONFIG['region']['row_lang'] = lang
                data = self.view(person)
                self.assertEqual(data['cause'], cause)
                self.assertEqual(data['message'], line)
                self.assertEqual(ready.text(data), line)

    def test_cli_home_plan_and_why_project_explicit_household(self):
        self.build()
        s = self.s
        self.data.joinpath('feeds.json').write_text(json.dumps({cid+'|'+lang: rec for (cid, lang), rec in s._feeds.items()}))
        self.data.joinpath('profile.json').write_text(json.dumps(s._profiles['household'], default=list))
        for op in ('plan', 'why'):
            for person in (None, 'household', 'together', 'ana'):
                with self.subTest(op=op, person=person):
                    out = io.StringIO()
                    code = main(['rows', op, *(['ready1'] if op == 'why' else []), '--json',
                                 *(['--person', person] if person else [])], out=out)
                    self.assertEqual(code, 0)
                    data = json.loads(out.getvalue())
                    releases = [r['release'] for r in data.get('rows', [data]) if r.get('release')]
                    self.assertTrue(releases)
                    if person:
                        self.assert_private({'releases': releases}, person)
                    else:
                        self.assertEqual(set(releases[0]['people']), {'ana', 'ben'})

    def test_empty_ready_no_releases(self):
        self.profile()
        self.build()
        self.assert_empty('no_releases', 'No Get Ready row: no film or season release is saved.',
                          'Keine Einstimmen-Reihe: Es ist kein Film- oder Staffelstart gespeichert.')

    def test_empty_ready_disabled(self):
        self.build()
        self.s.CONFIG['household']['rows'] = []
        self.assert_empty('disabled', 'No Get Ready row: the row is turned off.',
                          'Keine Einstimmen-Reihe: Die Reihe ist ausgeschaltet.')

    def test_empty_ready_outside_window(self):
        self.profile(entry(101, 'ana'))
        for days in (60, -8):
            with self.subTest(days=days):
                self.parts[2]['release_date'] = (TODAY.date()+dt.timedelta(days=days)).isoformat()
                self.build()
                self.assert_empty('outside_window', 'No Get Ready row: the release is too far away or already out.',
                                  'Keine Einstimmen-Reihe: Der Start liegt zu weit in der Zukunft oder zu lange zurück.')

    def test_empty_ready_no_interest(self):
        self.profile()
        with patch.object(self.s, 'movie_collections', return_value={901}):
            self.build()
        self.assert_empty('no_interest', 'No Get Ready row: no interest in the earlier parts.',
                          'Keine Einstimmen-Reihe: Es gibt kein Interesse an den früheren Teilen.')

    def test_empty_ready_no_earlier_parts(self):
        self.profile(entry(101, 'ana'))
        self.parts[:] = self.parts[-1:]
        self.build()
        self.assert_empty('no_earlier_parts', 'No Get Ready row: there are no earlier parts.',
                          'Keine Einstimmen-Reihe: Es gibt keine früheren Teile.')

    def test_empty_ready_not_watched(self):
        self.profile(entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.build()
        self.assert_empty('not_watched', 'No Get Ready row: you have not watched the earlier seasons.',
                          'Keine Einstimmen-Reihe: Du hast die früheren Staffeln noch nicht gesehen.')

    def test_empty_ready_unknown_progress(self):
        self.profile(entry(201, 'ana', 'tv'))
        self.build()
        self.assert_empty('unknown_progress', 'No Get Ready row: your season progress is unknown.',
                          'Keine Einstimmen-Reihe: Dein Fortschritt bei den Staffeln ist unbekannt.')

    def test_empty_ready_caught_up(self):
        self.profile(entry(201, 'ana', 'tv', last_episode=[2, 4]))
        self.build()
        self.assert_empty('caught_up', 'No Get Ready row: you are caught up on the earlier seasons.',
                          'Keine Einstimmen-Reihe: Du bist bei den früheren Staffeln auf dem aktuellen Stand.')

    def test_empty_ready_adult(self):
        self.profile(entry(101, 'ana'))
        self.parts[2]['adult'] = True
        self.build()
        self.assert_empty('adult', 'No Get Ready row: the release is filtered as adult content.',
                          'Keine Einstimmen-Reihe: Der Start wird als Inhalt für Erwachsene ausgefiltert.')

    def test_empty_ready_filtered(self):
        self.profile(entry(101, 'ana'))
        self.build()
        self.s.CONFIG['taste']['people']['ana']['prefs'] = {'hide': ['movies']}
        self.assert_empty('filtered', 'No Get Ready row: it is filtered for this viewer.',
                          'Keine Einstimmen-Reihe: Sie wird für diese Person ausgefiltert.')
        self.assert_empty('filtered', 'No Get Ready row: it is filtered for this viewer.',
                          'Keine Einstimmen-Reihe: Sie wird für diese Person ausgefiltert.', person='guest')

    def test_empty_ready_undated(self):
        self.profile(entry(101, 'ana'))
        self.parts[:] = [dict(self.parts[-1], release_date='')]
        self.build()
        self.assert_empty('undated', 'No Get Ready row: the release has no date.',
                          'Keine Einstimmen-Reihe: Für den Start gibt es kein Datum.')

    def test_empty_ready_lookup_failed(self):
        self.profile(entry(101, 'ana'))
        with patch.object(self.s, 'movie_collections', return_value={901}), \
                patch.object(self.s, 'tmdb', side_effect=OSError('synthetic failure')):
            self.build()
        self.assert_empty('lookup_failed', 'No Get Ready row: release details could not be loaded.',
                          'Keine Einstimmen-Reihe: Die Angaben zum Start konnten nicht geladen werden.')

    def test_empty_ready_not_selected(self):
        self.build()
        self.s._feeds[self.s.rid('ready1'), self.s.LANG]['ready_slots'] = {'ana': []}
        self.assert_empty('not_selected', 'No Get Ready row: no release is selected in the saved row slots.',
                          'Keine Einstimmen-Reihe: In den gespeicherten Reihenplätzen ist kein Start ausgewählt.')

    def test_empty_ready_shared_view_does_not_report_another_members_progress(self):
        self.profile(entry(201, 'ben', 'tv', last_episode=[2, 4]))
        self.build()
        for person in ('household', 'together'):
            self.assert_empty('no_qualifying_release', 'No Get Ready row: no release qualifies for this Home.',
                              'Keine Einstimmen-Reihe: Kein Start kommt für diese Startseite infrage.', person=person)

    def test_ready_title_filter_keeps_saved_slot_and_enabled_state(self):
        from skald import ready
        self.profile(entry(101, 'ana'), entry(201, 'ana', 'tv', last_episode=[1, 2]))
        self.build()
        self.s.CONFIG['household']['rows'] = ['ready2']
        for title in (None, 'Invented Ember 3'):
            with self.subTest(title=title):
                data = ready.view(self.s, 'ana', title=title)
                self.assertTrue(data['enabled'])
                self.assertIsNone(data['cause'])
                self.assertIsNone(data['message'])
        data = ready.view(self.s, 'ana', title='Invented Orbit')
        self.assertEqual(data['cause'], 'disabled')
        self.assertEqual(ready.text(data), 'No Get Ready row: the row is turned off.')

    def test_operator_default_still_rejects_invalid_explicit_people(self):
        from skald import ready
        for person in ('', False, [], {}, 'unknown'):
            for view in (ready.view, rowplan.plan):
                with self.subTest(person=person, view=view.__name__), self.assertRaises(ValueError):
                    view(self.s, person)

    def serve(self, slot=1, who='none', masks=None, above=None):
        s = self.s
        return s.served_row(s.rid('ready'+str(slot)), 'tv', s.LANG, s._profiles['household'],
                            above or set(), masks or (set(), set(), set()), who=who)

    def test_saga_and_series_order_notes_and_member_scope(self):
        self.build()
        self.assertEqual([x['id'] for x in self.serve(who='ben')], ['tmdb:201'])
        self.assertEqual([x['id'] for x in self.serve(who='ana')], ['tmdb:101', 'tmdb:102'])
        saga = self.serve(2)
        self.assertEqual([x['id'] for x in saga], ['tmdb:101', 'tmdb:102'])
        self.assertIn('Part 1 of 2', saga[0]['description'])
        self.assertIn('Seen', saga[0]['description'])
        self.assertIn('Not seen', saga[1]['description'])
        self.assertIn('Season 3 starts 11 Oct · 2 seasons to catch up', self.serve()[0]['description'])
        releases = self.view()['releases']
        self.assertEqual([r['date'] for r in releases if r['qualifies']], ['2026-10-11', '2026-10-31'])
        show = next(r for r in releases if r['kind'] == 'tv')
        self.assertEqual(show['for'], ['ben'])
        self.assertEqual(show['people']['ana']['reason'], 'not_watched')
        self.assertEqual(show['people']['ben']['seasons'], [1, 2])

    def test_window_inclusive_and_stale_pins_expire(self):
        self.profile(entry(101, 'ana'))
        for days, qualifies in ((42, True), (60, False), (0, True), (-7, True), (-8, False)):
            with self.subTest(days=days):
                self.parts[2]['release_date'] = (TODAY.date()+dt.timedelta(days=days)).isoformat()
                self.build()
                self.assertEqual(bool(self.serve()), qualifies)
                self.assertEqual(self.view()['releases'][0]['qualifies'], qualifies)
        self.parts[2]['release_date'] = TODAY.date().isoformat()
        self.build()
        with patch.object(self.s, 'local_now', return_value=TODAY+dt.timedelta(days=8)):
            self.assertEqual(self.serve(), [])

    def test_saved_release_uses_current_member_progress(self):
        self.profile(entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.build()
        self.assertEqual(len(self.serve(who='ben')), 1)
        self.profile(entry(201, 'ben', 'tv', last_episode=[2, 4]))
        self.assertEqual(self.serve(who='ben'), [])
        self.assertEqual(self.view('ben')['releases'][0]['people']['ben']['reason'], 'caught_up')

    def test_disabled_ready_cache_leaves_explicit_home_identical_to_parent(self):
        s = self.s
        self.profile()
        with patch.object(s, 'movie_collections', return_value={901}), \
                patch.object(s, 'read_taste', return_value={'household': {'rewatch_collections': [901]}}):
            self.build()
            s._feeds[s.rid('foryou'), s.LANG] = {'metas': {'tv': cards(5, 500)}}
            s.build_home(s.LANG, s._profiles['household'], s.row_masks('none'), 'none')
            s.CONFIG['household']['rows'] = ['foryou']
            self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
            with patch.object(s, 'DEDUPE_ORDER', [cid for cid in s.DEDUPE_ORDER if cid not in s.READY_ROWS]):
                parent = s.build_home(s.LANG, s._profiles['household'], s.row_masks('none'), 'none')
            actual = s.build_home(s.LANG, s._profiles['household'], s.row_masks('none'), 'none')
        self.assertEqual(len(parent[s.rid('foryou'), 'tv']), 5)
        self.assertEqual(json.dumps(list(actual.items())), json.dumps(list(parent.items())))

    def test_final_home_preserves_ready_part_and_seen_notes(self):
        s = self.s
        self.build()
        home = s.build_home(s.LANG, s._profiles['household'], s.row_masks('ana'), 'ana')
        saga = home[s.rid('ready1'), 'tv']
        self.assertEqual([x['description'] for x in saga], ['Part 1 of 2 · Seen', 'Part 2 of 2 · Not seen'])

    def test_final_child_home_preserves_ready_catchup_note(self):
        s = self.s
        self.build()
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 8}
        s._facets['tmdb:201'] = dict(cr={'DE': '6', 'US': 'TV-Y7'}, adult=False, kind='tv')
        with s.addon_scope('ben'):
            home = s.build_home(s.LANG, s._profiles['household'], s.row_masks('ben'), 'ben')
        self.assertEqual(home[s.rid('ready1'), 'tv'][0].get('description'),
                         'Season 3 starts 11 Oct · 2 seasons to catch up')

    def test_manifest_pins_each_viewers_slots_until_refresh(self):
        s = self.s
        self.profile(entry(101, 'ana'), entry(101, 'ben'), entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.build()
        s.CONFIG['household']['rows'] = ['ready']
        self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
        for scope in (None, 'ana', 'ben'):
            manifest = s.manifest(s.LANG, scope)
            self.assertIn('Invented Ember' if scope == 'ana' else 'Invented Orbit', manifest['catalogs'][0]['name'])
        self.profile(entry(101, 'ana'), entry(101, 'ben'), entry(201, 'ben', 'tv', last_episode=[2, 4]))
        self.build()
        for scope, who in ((None, 'none'), ('ben', 'ben')):
            with s.addon_scope(scope):
                self.assertEqual(s.row_metas(s.rid('ready1'), 'tv', s.LANG, who=who), [])
                self.assertEqual([x['id'] for x in s.row_metas(s.rid('ready2'), 'tv', s.LANG, who=who)],
                                 ['tmdb:101', 'tmdb:102'])
        with s.addon_scope('ana'):
            self.assertEqual([x['id'] for x in s.row_metas(s.rid('ready1'), 'tv', s.LANG, who='ana')],
                             ['tmdb:101', 'tmdb:102'])
        manifest = s.manifest(s.LANG, 'ben')
        self.assertIn('Invented Ember', manifest['catalogs'][0]['name'])
        with s.addon_scope('ben'):
            self.assertEqual([x['id'] for x in s.row_metas(s.rid('ready1'), 'tv', s.LANG, who='ben')],
                             ['tmdb:101', 'tmdb:102'])
            self.assertEqual(s.row_metas(s.rid('ready2'), 'tv', s.LANG, who='ben'), [])

    def test_manifest_does_not_swap_two_seasons_of_the_same_show(self):
        s = self.s
        self.profile(entry(201, 'ben', 'tv', last_episode=[1, 2]))
        self.show['seasons'].append(dict(season_number=4, episode_count=4, air_date='2026-11-01'))
        self.build()
        s.CONFIG['household']['rows'] = ['ready']
        self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
        manifest = s.manifest(s.LANG, 'ben')
        self.assertIn('Season 3', manifest['catalogs'][0]['name'])
        self.assertIn('Season 4', manifest['catalogs'][1]['name'])
        self.profile(entry(201, 'ben', 'tv', last_episode=[2, 4]))
        with s.addon_scope('ben'):
            self.assertEqual(s.row_metas(s.rid('ready1'), 'tv', s.LANG, who='ben'), [])
            self.assertIn('Season 4', s.row_metas(s.rid('ready2'), 'tv', s.LANG, who='ben')[0]['description'])

    def test_no_interest_and_explicit_saga(self):
        self.profile()
        with patch.object(self.s, 'movie_collections', return_value={901}):
            self.build()
            self.assertEqual(self.serve(), [])
            self.assertEqual(self.view()['releases'][0]['people']['ana']['reason'], 'no_interest')
            with patch.object(self.s, 'read_taste', return_value={'household': {'rewatch_collections': [901]}}):
                self.build()
                self.assertEqual(len(self.serve()), 2)

    def test_loved_part_without_seen_mask_qualifies(self):
        e = entry(101, 'ana')
        e['signal'] = 'loved'
        self.profile(e)
        self.s._profiles['household']['seen'] = set()
        self.build()
        self.assertEqual(len(self.serve(who='ana')), 2)

    def test_finished_prior_seasons_and_unknown_progress_do_not_qualify(self):
        for item, reason in ((dict(last_episode=[2, 4]), 'caught_up'), ({}, 'unknown_progress')):
            with self.subTest(item=item):
                self.profile(entry(201, 'ben', 'tv', **item))
                self.build()
                self.assertEqual(self.serve(who='ben'), [])
                self.assertEqual(self.view()['releases'][0]['people']['ben']['reason'], reason)

    def test_two_nearest_rows_and_no_padding_or_franchise_dedupe(self):
        self.collections[902] = dict(id=902, name='Invented Tide Collection', parts=[
            dict(self.parts[0], id=301), dict(self.parts[2], id=302, release_date='2026-10-21')])
        self.enterContext(patch.object(self.s, 'read_taste', return_value={'household': {'rewatch_collections': [902]}}))
        with patch.object(self.s, 'movie_collections', return_value={901, 902}):
            self.build()
        self.assertEqual([x['id'] for x in self.serve(2)], ['tmdb:301'])
        self.assertEqual(sum(r['selected'] for r in self.view()['releases']), 2)
        above = self.s.mark_above(set(), self.s.rid('foryou'), [self.parts_meta()])
        above = self.s.mark_above(above, self.s.rid('week'), [self.parts_meta()])
        self.assertEqual(len(self.serve(2, who='ana', above=above)), 2)

    def parts_meta(self):
        return self.s.row_meta(self.parts[0], 'movie', 'movie', {}, {}, collection=901)

    def test_negatives_hides_and_vetoes_keep_release_order(self):
        self.build()
        s = self.s
        s._profiles['household']['negative'] = {'tmdb:102'}
        self.assertEqual([x['id'] for x in self.serve(2)], ['tmdb:101'])
        self.assertEqual(self.serve(who='ben', masks=({'tmdb:201'}, set(), set())), [])
        self.assertEqual(self.serve(who='ben', masks=(set(), set(), {'tmdb:201'})), [])
        s.CONFIG['taste']['people']['ana']['prefs'] = {'hide': ['movies']}
        self.assertEqual(self.serve(who='ana'), [])
        self.assertEqual(self.serve(who=s.GUEST), [])

    def test_ready_reasons_only_include_present_members(self):
        self.build()
        for state in ('away', 'guests'):
            with self.subTest(state=state), patch.dict(self.s.CONFIG['household'], {state: ['ben']}):
                for release in self.view()['releases']:
                    self.assertEqual(set(release['people']), {'ana'})
                rows = self.build()
                for release in rows[self.s.rid('ready1')]['releases']:
                    self.assertEqual(set(release['people']), {'ana'})
                for release in self.view()['releases']:
                    self.assertEqual(set(release['people']), {'ana'})

    def test_together_and_child_veto_match_other_home_rows(self):
        s = self.s
        self.build()
        s._feeds[s.rid('digital'), s.LANG] = {'metas': {'movie': cards(8, start=101)}}
        with patch.object(s, 'remote_masks', self.original_remote_masks), patch.object(s, 'quality_ok', return_value=True):
            def rows():
                masks = s.row_masks('ana')
                return (self.serve(who='ana', masks=masks),
                        s.served_row(s.rid('digital'), 'movie', s.LANG, {}, set(), masks, who='ana'))
            for actor in ('both', 'ben'):
                with self.subTest(actor=actor):
                    self.data.joinpath('feedback.jsonl').unlink(missing_ok=True)
                    s._remote_cache.clear()
                    if actor == 'ben':
                        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 12}
                    for row in rows():
                        self.assertIn('tmdb:101', [x['id'] for x in row])
                    s.write_feedback(dict(who=actor, key='tmdb:101', kind='movie', title='Invented Ember 1', action='not_for_us'))
                    for row in rows():
                        self.assertTrue(row)
                        self.assertNotIn('tmdb:101', [x['id'] for x in row])

    def test_forget_clears_ready_reasons(self):
        self.build()
        self.assertIn('ben', self.view()['releases'][0]['people'])
        with patch.object(self.s, 'aio_rows_changed'):
            commands.apply(self.s, dict(commands.preview(self.s, '', 'ana', answer=dict(op='member_forget', person='ben')), source='cli'))
        self.assertEqual(self.s.feed(self.s.rid('ready1'), self.s.LANG), {})
        self.assertNotIn('ben', json.dumps(self.view()['releases']))

    def test_adult_parts_and_release_are_filtered(self):
        self.profile(entry(101, 'ana'))
        self.parts[1]['adult'] = True
        self.build()
        self.assertEqual([x['id'] for x in self.serve()], ['tmdb:101'])
        self.assertIn('Part 1 of 2', self.serve()[0]['description'])
        self.parts[2]['adult'] = True
        self.build()
        self.assertEqual(self.serve(), [])

    def test_member_reasons_include_own_filters_and_prior_season_progress(self):
        ana = entry(201, 'ana', 'tv', last_episode=[2, 4])
        ben = entry(201, 'ben', 'tv', last_episode=[1, 2])
        shared = {**ben, 'by': ['ana', 'ben'], 'opinions': {'ana': ana, 'ben': ben}}
        self.profile(shared)
        self.build()
        release = self.view()['releases'][0]
        self.assertEqual(release['for'], ['ben'])
        self.assertEqual(release['people']['ana']['reason'], 'caught_up')
        self.s.CONFIG['taste']['people']['ben']['prefs'] = {'hide': ['series']}
        release = self.view()['releases'][0]
        self.assertEqual(release['for'], [])
        self.assertEqual(release['people']['ben']['reason'], 'filtered')
        self.assertEqual(self.serve(), [])

    def test_negative_new_release_and_remote_love_is_not_a_seen_part(self):
        e = entry(101, 'ana')
        e.update(signal='loved', who='remote')
        self.profile(e)
        self.build()
        self.assertIn('Not seen', self.serve()[0]['description'])
        self.s._profiles['household']['negative'] = {'tmdb:103'}
        self.assertEqual(self.serve(), [])

    def test_liked_but_unwatched_movie_and_unwatched_series_do_not_trigger(self):
        e = entry(101, 'ana')
        e.update(signal='liked', who='remote')
        self.profile(e)
        self.build()
        self.assertEqual(self.serve(who='ana'), [])
        e = entry(201, 'ben', 'tv', last_episode=[1, 2])
        e.update(signal='loved', who='remote')
        self.profile(e)
        self.build()
        self.assertEqual(self.serve(who='ben'), [])

    def test_release_questions_and_title_lookalikes_route_through_fake_model(self):
        self.enterContext(patch.object(self.s, 'openrouter', self.openrouter))
        from decide_kit import fixture_rows
        cases = [case for case in fixture_rows(self.s, 'route_query') if case['note'].startswith('Synthetic: get ready')]
        self.assertGreaterEqual(len(cases), 8)
        for case in cases:
            reset(self.s)
            with ReplayLLM(self.s) as fake:
                fake.arm(case['recorded'])
                answer = self.s.decide('route_query', case['in'])['answer']
            self.assertEqual(answer['choice'], case['expect']['choice'])

    def test_localized_tmdb_title_without_english_script_fallback(self):
        original = self.tmdb
        def translated(path, **params):
            result = original(path, **params)
            if path == 'tv/201' and params['language'] == 'sr-Cyrl-RS':
                result.update(name='Измишљена орбита', original_name='Invented Orbit', original_language='en')
            return result
        self.s.LANG = 'sr-Cyrl-RS'
        with patch.object(self.s, 'tmdb', side_effect=translated):
            self.build()
        self.assertEqual(self.serve(who='ben')[0]['name'], 'Измишљена орбита')
        self.assertIn('Измишљена орбита', self.view('ben')['releases'][0]['label'])

    def test_missing_date_and_failed_lookup_are_reported(self):
        self.profile(entry(101, 'ana'))
        self.parts[2]['release_date'] = ''
        self.build()
        self.assertTrue(any(r['people']['ana']['reason'] == 'undated' for r in self.view()['releases']))
        with patch.object(self.s, 'movie_collections', return_value={901}), \
                patch.object(self.s, 'tmdb', side_effect=OSError('synthetic failure')):
            self.build()
        self.assertEqual(self.view()['releases'][0]['people']['ana']['reason'], 'lookup_failed')

    def test_api_and_worker_register_both_slots(self):
        self.build()
        s = self.s
        self.assertTrue(set(s.READY_ROWS) <= s.API_ROWS)
        jobs = [spec for spec in s.job_specs(TODAY, {}) if spec[0] == 'ready']
        self.assertEqual(jobs[0][1], s.READY_ROWS)
        with patch.object(s, 'pin_shown'):
            manifest = s.manifest(s.LANG, 'ben')['catalogs']
        rows = [r for r in manifest if r['id'] in s.READY_ROWS]
        self.assertEqual(len(rows), 1)
        self.assertIn('Invented Orbit', rows[0]['name'])

    def test_kids_use_existing_age_policy(self):
        self.build()
        self.s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 8}
        self.assertEqual(self.serve(who='ben'), [])
        self.s._facets['tmdb:201'] = dict(cr={'DE': '6', 'US': 'TV-Y7'}, adult=False, kind='tv')
        self.assertEqual([x['id'] for x in self.serve(who='ben')], ['tmdb:201'])

    def test_child_ready_reads_hide_adult_release_titles(self):
        s = self.s
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 8}
        for flagged in (False, True):
            with self.subTest(adult=flagged):
                self.show['adult'] = flagged
                s._facets['tmdb:201'] = dict(cr={'DE': '18', 'US': 'TV-MA'}, adult=flagged, kind='tv')
                self.build()
                self.assertNotIn('Invented Orbit', json.dumps(self.view('ben')))
                result = commands.preview(s, '', viewer='ben', answer=dict(commands.NONE, op='home_plan', values=['ready']))
                self.assertNotIn('Invented Orbit', json.dumps(result))

    def test_default_explicit_rows_manifest_plan_and_together(self):
        self.build()
        s = self.s
        self.assertEqual(s.row_groups()['ready'], s.READY_ROWS)
        self.assertTrue(all(s.row_enabled(cid) for cid in s.READY_ROWS))
        for person, count in (('household', 2), ('together', 2), ('ana', 1), ('ben', 1)):
            data = rowplan.plan(s, person)
            rows = [r for r in data['rows'] if r['id'] in s.READY_ROWS and r['position']]
            self.assertEqual(len(rows), count, person)
            self.assertTrue(all(r['source'] == 'history' for r in rows))
        with patch.object(s, 'pin_shown'):
            rows = [r for r in s.manifest(s.LANG)['catalogs'] if r['id'] in s.READY_ROWS]
            self.assertEqual(len(rows), 2)
            self.assertIn('Invented Ember 3, in cinemas 31 Oct', rows[1]['name'])
        s.CONFIG['household']['rows'] = ['catchup']
        self.assertFalse(any(s.row_enabled(cid) for cid in s.READY_ROWS))
        self.assertNotIn('ready', s.CONFIG['household']['rows'])

    def test_empty_row_hidden_before_first_build(self):
        with patch.object(self.s, 'pin_shown'):
            self.assertFalse(set(self.s.READY_ROWS) & {r['id'] for r in self.s.manifest(self.s.LANG)['catalogs']})

    def test_fixed_localized_labels_and_tmdb_titles(self):
        self.build()
        for lang, beginning in (('en', 'Get Ready for'), ('de', 'Einstimmen auf'),
                                ('sr-latn', 'Pripremi se za'), ('sr-cyrl', 'Припреми се за')):
            self.s.CONFIG['region']['row_lang'] = lang
            label = rowplan.why(self.s, 'ready1')['label']
            self.assertTrue(label.startswith(beginning), label)
            self.assertIn('Invented Orbit', label)

    def test_owner_locale_values_are_exact(self):
        values = {
            'en': {'row.ready': 'Get Ready', 'row.ready.named': 'Get Ready for {title}',
                   'ready.catchup': 'Season {season} starts {date} · {count} seasons to catch up',
                   'ready.catchup.one': 'Season {season} starts {date} · {count} season to catch up'},
            'de': {'row.ready': 'Einstimmen', 'row.ready.named': 'Einstimmen auf {title}',
                   'ready.catchup': 'Staffel {season} ab {date} · noch {count} Staffeln nachholen',
                   'ready.catchup.one': 'Staffel {season} ab {date} · noch {count} Staffel nachholen',
                   'ready.season': '{title}, Staffel {season} ab {date}',
                   'ready.seen': 'gesehen', 'ready.unseen': 'noch nicht gesehen',
                   'ready.cinemas': '{title}, ab {date} im Kino'},
            'sr-latn': {'row.ready.named': 'Pripremi se za {title}'},
            'sr-cyrl': {'row.ready.named': 'Припреми се за {title}'},
        }
        for lang, expected in values.items():
            actual = json.loads(Path('skald/locales', lang+'.json').read_text())
            with self.subTest(lang=lang):
                self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_cached_tmdb_calls_only_no_per_part_details(self):
        self.build()
        paths = [path for path, _ in self.calls]
        self.assertNotIn('movie/102', paths)
        self.assertNotIn('movie/103', paths)
        self.assertTrue(all(path in ('movie/101', 'collection/901', 'tv/201', 'genre/movie/list', 'genre/tv/list') for path in paths))

    def test_existing_tmdb_cache_avoids_all_extra_transport_calls(self):
        s = self.s
        def transport(request, **kwargs):
            url = urllib.parse.urlsplit(request.full_url)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items() if k != 'api_key'}
            return io.BytesIO(json.dumps(self.tmdb(url.path.removeprefix('/3/'), **params)).encode())
        with patch.object(s, 'tmdb', self.original_tmdb), patch('urllib.request.urlopen', side_effect=transport):
            s.movie_collections(s._profiles['household'])
            s.details_pair('tv', 201, s.LANG)
            for language in (s.LANG, 'en-US'):
                s.tmdb('collection/901', language=language)
            for kind in ('movie', 'tv'):
                s.genres(kind, s.LANG)
            self.calls.clear()
            self.build()
        self.assertEqual(self.calls, [])

    def test_german_child_ready_defers_cold_details_and_keeps_warm_row(self):
        s = self.s
        s.LANG = 'de-DE'
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 8}
        self.data.joinpath('skald.json').write_text(json.dumps(s.CONFIG))
        watched = entry(201, 'ben', 'tv', last_episode=[1, 2], episodes_played=2)
        watched['age'] = 200
        self.profile(watched)
        s._facets['tmdb:201'] = dict(cr={'DE': '6', 'US': 'TV-Y7'}, adult=False, kind='tv')
        def transport(request, **kwargs):
            url = urllib.parse.urlsplit(request.full_url)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items() if k != 'api_key'}
            return io.BytesIO(json.dumps(self.tmdb(url.path.removeprefix('/3/'), **params)).encode())
        with patch.object(s, 'tmdb', self.original_tmdb), patch('urllib.request.urlopen', side_effect=transport):
            for source in (s.episodes_feed, s.rewatch_feed, s.upcoming_feed, s.catchup_feed):
                source(s.LANG)
            self.assertFalse(any(path == 'tv/201' for path, _ in self.calls))
            self.calls.clear()
            self.build()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.serve(who='ben'), [])
            s.details_pair('tv', 201, s.LANG)
            self.calls.clear()
            self.build()
            self.assertEqual(self.calls, [])
            self.assertEqual([x['id'] for x in self.serve(who='ben')], ['tmdb:201'])

    def test_ready_defers_uncached_movie_and_collection_details(self):
        s = self.s
        s.LANG = 'de-DE'
        self.profile(entry(101, 'ana'))
        def transport(request, **kwargs):
            url = urllib.parse.urlsplit(request.full_url)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items() if k != 'api_key'}
            return io.BytesIO(json.dumps(self.tmdb(url.path.removeprefix('/3/'), **params)).encode())
        with patch.object(s, 'tmdb', self.original_tmdb), patch('urllib.request.urlopen', side_effect=transport):
            self.build()
            self.assertFalse(any(path.startswith(('movie/', 'collection/')) for path, _ in self.calls))
            self.assertEqual(self.serve(who='ana'), [])
            s.movie_collections(s._profiles['household'])
            s.tmdb('collection/901', language='en-US')
            self.calls.clear()
            self.build()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.serve(who='ana'), [])
            s.tmdb('collection/901', language=s.LANG)
            self.calls.clear()
            self.build()
            self.assertEqual(self.calls, [])
            self.assertEqual([x['id'] for x in self.serve(who='ana')], ['tmdb:101', 'tmdb:102'])

    def test_one_season_note_and_ready_why_show_date_and_parts(self):
        self.profile(entry(201, 'ben', 'tv', last_episode=[1, 4]))
        self.build()
        self.assertIn('1 season to catch up', self.serve(who='ben')[0]['description'])
        row = rowplan.why(self.s, 'ready1', 'ben')
        self.assertEqual(row['release']['date'], '2026-10-11')
        self.assertEqual(row['release']['for'], ['ben'])

    def test_explicit_ready_list_and_hide_show_preview_keep_other_choices(self):
        self.build()
        s = self.s
        s.CONFIG['household']['rows'] = ['catchup']
        self.assertEqual([r[0] for r in s.feed_rows()], [s.rid('catchup')])
        cfg = copy.deepcopy(s.CONFIG)
        commands.edit_rows(cfg, dict(commands.NONE, op='row_show', values=['ready']), s)
        self.assertEqual(cfg['household']['rows'], ['catchup', 'ready'])
        with patch.object(s, 'CONFIG', cfg):
            self.assertTrue(set(s.READY_ROWS) <= {r[0] for r in s.feed_rows()})

    def test_cli_json_uses_saved_data_offline(self):
        self.build()
        s = self.s
        self.data.joinpath('feeds.json').write_text(json.dumps({cid+'|'+lang: rec for (cid, lang), rec in s._feeds.items()}))
        self.data.joinpath('profile.json').write_text(json.dumps(s._profiles['household'], default=list))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = main(['rows', 'ready', '--json'], out=out)
        self.assertEqual(code, 0, err.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual(len(data['releases']), 2)
        self.assertIn('parts', data['releases'][1])

    def test_commander_reads_same_data_and_row_controls(self):
        self.build()
        from skald import ready
        change = dict(commands.NONE, op='home_plan', values=['ready', 'Invented Ember 3'])
        result = commands.preview(self.s, 'invented query', viewer='ana', answer=change)
        self.assertTrue(result['read_only'])
        self.assertEqual([r['key'] for r in result['ready']['releases']], ['tmdb:103'])
        self.assertIn('Invented Ember 1', result['description'])
        self.assertEqual(result['ready'], ready.view(self.s, 'ana', title='Invented Ember 3'))
        cfg = copy.deepcopy(self.s.CONFIG)
        commands.edit_rows(cfg, dict(commands.NONE, op='row_hide', values=['ready']), self.s)
        self.assertNotIn('ready', cfg['household']['rows'])
        commands.edit_rows(cfg, dict(commands.NONE, op='row_show', values=['ready']), self.s)
        self.assertIn('ready', cfg['household']['rows'])

    def test_commander_release_answer_uses_only_final_home_parts(self):
        self.build()
        s = self.s
        s._profiles['household']['negative'] = {'tmdb:102'}
        home = s.build_home(s.LANG, s._profiles['household'], s.row_masks('ana'), 'ana')
        change = dict(commands.NONE, op='home_plan', values=['ready', 'Invented Ember 3'])
        result = commands.preview(s, 'invented query', viewer='ana', answer=change)
        self.assertEqual([x['key'] for x in result['ready']['releases'][0]['parts']],
                         [x['id'] for x in home[s.rid('ready1'), 'tv']])
        self.assertNotIn('Invented Ember 2', result['description'])

    def test_real_hide_confirm_show_sequence_without_injected_last(self):
        self.build()
        s = self.s
        self.enterContext(patch.object(s, 'openrouter', self.openrouter))
        self.enterContext(patch.object(s, 'aio_rows_changed'))
        with s.addon_scope('ana'), ReplayLLM(s) as fake:
            for op, query in (('row_hide', 'hide the get ready row'), ('row_show', 'show it again')):
                fake.arm({'choices': [{'message': {'content': json.dumps(dict(commands.NONE, op=op, values=['ready']))}}]})
                commands.special(s.command_context(), query, 'movie', routed=True)
                pending = commands.get_pending(s.command_context(), 'ana')
                self.assertIsNotNone(pending)
                self.assertEqual(pending['change']['op'], op)
                body = fake.bodies[-1]['messages'][-1]['content']
                self.assertNotIn('Last search:', body)
                self.assertIn(s.rid('ready1'), body)
                if op == 'row_show':
                    self.assertIn('(hidden)', body)
                commands.special(s.command_context(), 'yes '+commands.confirm_code(pending), 'movie')
                self.assertEqual(s.row_enabled(s.rid('ready1')), op == 'row_show')
            self.assertEqual(fake.calls, 2)

    def test_followup_fixtures_use_hidden_context_without_last(self):
        from eval import commands_eval
        cases = [case for case in commands_eval.load_cases() if case['id'] in
                 ('w117-ready-12', 'w117-ready-13', 'w117-ready-14')]
        self.assertEqual(len(cases), 3)
        for case in cases:
            self.assertNotIn('last', case)
            self.assertTrue(case['rows'])
        def transport(url, payload, key, timeout):
            body = payload['messages'][-1]['content']
            self.assertIn('(hidden)', body)
            self.assertNotIn('Last search:', body)
            self.assertEqual(json.loads(body.split('Last confirmed row action: ')[1]),
                             dict(id='ready', action='hide', at=1001.))
            return next(case['recorded'] for case in cases if body.startswith(case['text']))
        report = commands_eval.evaluate(cases, ['synthetic/model'], transport=transport)[0]
        self.assertEqual(report['errors'], {})
        self.assertEqual(report['now_full_accuracy'], 1)

    def test_commander_followup_context_keeps_last_confirmed_row_per_scope(self):
        s = self.s
        original = copy.deepcopy(s.CONFIG)
        self.enterContext(patch.object(s, 'openrouter', self.openrouter))
        self.enterContext(patch.object(s, 'aio_rows_changed'))
        for order in (('anime', 'ready'), ('ready', 'anime')):
            with self.subTest(order=order):
                self.data.joinpath('skald.json').write_text(json.dumps(original))
                s.refresh_preferences()
                reset(s)
                with s.addon_scope('ana'), ReplayLLM(s) as fake:
                    for i, row in enumerate(order):
                        fake.arm({'choices': [{'message': {'content': json.dumps(dict(commands.NONE, op='row_hide', values=[row]))}}]})
                        commands.special(s.command_context(), 'hide the '+row+' row', 'movie', routed=True)
                        pending = commands.get_pending(s.command_context(), 'ana')
                        self.assertIsNotNone(pending)
                        with patch.object(s.time, 'time', return_value=1000.+i):
                            commands.special(s.command_context(), 'yes '+commands.confirm_code(pending), 'movie')
                        self.assertTrue(all(not s.row_enabled(cid) for cid in s.row_groups()[row]))
                    fake.arm({'choices': [{'message': {'content': json.dumps(commands.NONE)}}]})
                    commands.special(s.command_context(), 'show it again', 'movie', routed=True)
                    body = fake.bodies[-1]['messages'][-1]['content']
                    self.assertIn('Last confirmed row action: ', body)
                    action = json.loads(body.split('Last confirmed row action: ')[1])
                    self.assertEqual(action, dict(id=order[-1], action='hide', at=1001.))
                with s.addon_scope('ben'):
                    self.assertNotIn('last_row_action', rowplan.command_input(s.command_context(), 'show it again'))
                with s.addon_scope('ana'):
                    pending = commands.preview(s.command_context(), 'show ready', viewer='ana', bound='ana',
                                               answer=dict(commands.NONE, op='row_show', values=['ready']))
                    self.assertEqual(rowplan.command_input(s.command_context(), 'show it again')['last_row_action'], action)
                    commands.pending(s.command_context())['ana'] = (s.time.time(), pending)
                    with patch.object(s.time, 'time', return_value=1002.):
                        commands.special(s.command_context(), 'yes '+commands.confirm_code(pending), 'movie')
                    self.assertEqual(rowplan.command_input(s.command_context(), 'hide it again')['last_row_action'],
                                     dict(id='ready', action='show', at=1002.))

    def test_api_followup_context_keeps_confirmed_row_in_client_scope(self):
        s = self.s.command_context()
        self.enterContext(patch.object(self.s, 'openrouter', self.openrouter))
        self.enterContext(patch.object(self.s, 'aio_rows_changed'))
        req = dict(viewer='ana', client={'name': 'synthetic'}, body={'in': {'query': 'hide the ready row'}})
        with ReplayLLM(self.s) as fake:
            fake.arm({'choices': [{'message': {'content': json.dumps(dict(commands.NONE, op='row_hide', values=['ready']))}}]})
            plan = commands.api_preview(self.s.command_context(), req)['preview']
            self.assertIsNotNone(plan)
            with patch.object(self.s.time, 'time', return_value=1000.):
                commands.api_apply(s, {**req, 'body': {'plan': plan['plan']}})
            fake.arm({'choices': [{'message': {'content': json.dumps(commands.NONE)}}]})
            commands.api_preview(self.s.command_context(), {**req, 'body': {'in': {'query': 'show it again'}}})
            body = fake.bodies[-1]['messages'][-1]['content']
            self.assertIn('Last confirmed row action: ', body)
            self.assertEqual(json.loads(body.split('Last confirmed row action: ')[1]),
                             dict(id='ready', action='hide', at=1000.))
        with self.s.addon_scope('ana'):
            self.assertNotIn('last_row_action', rowplan.command_input(self.s.command_context(), 'show it again'))

    def test_command_intent_uses_fake_model_in_all_locales(self):
        self.enterContext(patch.object(self.s, 'openrouter', self.openrouter))
        cases = [json.loads(line) for line in Path('eval/commands_cases.jsonl').read_text().splitlines()
                 if json.loads(line)['id'].startswith('w117-')]
        self.assertGreaterEqual(len(cases), 12)
        for case in cases:
            reset(self.s)
            inp = {'query': case['text'], **{k: case[k] for k in ('last', 'rows', 'last_row_action') if k in case}}
            self.assertIsNone(self.s.DECISIONS['parse_command']['rule'](inp, {}), case['text'])
            with ReplayLLM(self.s) as fake:
                fake.arm(case['recorded'])
                answer = self.s.decide('parse_command', inp)['answer']
            self.assertEqual({k: answer[k] for k in case['expect']}, case['expect'])
            self.assertEqual(fake.calls, 1)


if __name__ == '__main__':
    unittest.main()
