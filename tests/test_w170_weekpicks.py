"""Personal weekly picks, with synthetic households and offline transports."""
import copy
import datetime as dt
import io
import json
import os
from unittest.mock import patch
import unittest

from decide_kit import close_server, load_server, ReplayLLM
from skald import commands, members, row_names
import test_w109_week as recent

card = recent.card


class PersonalWeekly(unittest.TestCase):
    setUp = recent.Weekly.setUp
    put = recent.Weekly.put

    def answer(self):
        return {who: [dict(title=card(n)['name'], year=1990, type='movie', anchor=0,
                           reason='Unreviewed model prose') for n in ns]
                for who, ns in [('ana', (0, 1, 2)), ('ben', (3, 4, 5)), ('together', (6, 7, 8))]}

    def picker(self):
        s = self.s
        entries = [dict(title='Invented Loved '+p.title(), key='tmdb:90'+str(i), kind='movie',
                        weight=5, by=[p]) for i, p in enumerate(('ana', 'ben', 'together'))]
        s._profiles['household']['entries'] = entries
        self.enterContext(patch.object(s, 'person_entries', side_effect=lambda p, who: [e for e in entries if who in e['by']]))
        self.enterContext(patch.object(s, 'read_taste', return_value={'people': {}}))
        def resolve(stype, kind, picks, lang):
            return [card(n, date='1990-01-01', digital='1990-01-01') for n in range(9)
                    if picks[0][0] == card(n)['name']]
        self.enterContext(patch.object(s, 'row_search', side_effect=resolve))

    def test_labels_adults_locales_and_row_settings(self):
        s = self.s
        for lang, expected in [('en', '✨ This Week for Ana & Ben'), ('de', '✨ Diese Woche für Ana & Ben'),
                               ('sr-latn', '✨ Ove nedelje za Ana i Ben'), ('sr-cyrl', '✨ Ове недеље за Ana и Ben')]:
            self.assertEqual(row_names.describe(s.rid('weekpicks'), {}, s.CONFIG, lang)['label'], expected)
        cfg = members.fresh_config(['ana', 'ben', 'kim', 'lena', 'ivo'])
        cfg['household']['guests'] = ['lena']
        cfg['taste']['people']['kim']['prefs'] = {'kids_age': 9}
        self.assertEqual(row_names.describe(s.rid('weekpicks'), {}, cfg, 'en')['label'], '✨ This Week for Ana, Ben & Ivo')
        cfg['household']['away'] = ['ben', 'ivo']
        self.assertEqual(row_names.describe(s.rid('weekpicks'), {}, cfg, 'de')['label'], '✨ Diese Woche für Ana')
        self.assertIn('weekpicks', s.ROW_KEYS)
        legacy = members.fresh_config(['ana', 'ben'])
        legacy['household'].pop('weekpicks_added', None)
        legacy['household'].update(rows=['week', 'people'], row_order=['week', 'people'])
        upgraded = s.validate_config(legacy)
        self.assertEqual(upgraded['household']['rows'], ['week', 'people'])
        self.assertEqual(upgraded['household']['row_order'], ['week', 'weekpicks', 'people'])
        rows = [cid for cid, _, _ in s.feed_rows(profiles=False)]
        self.assertEqual(rows[rows.index(s.rid('week'))+1], s.rid('weekpicks'))
        commands.edit_rows(s.CONFIG, dict(op='row_hide', kind=None, values=['weekpicks']), s)
        self.assertFalse(s.row_enabled(s.rid('weekpicks')))
        self.assertNotIn('weekpicks', s.validate_config(s.CONFIG)['household']['rows'])
        commands.edit_rows(s.CONFIG, dict(op='row_show', kind=None, values=['weekpicks']), s)
        commands.edit_rows(s.CONFIG, dict(op='row_rename', kind=None, values=['weekpicks', 'Our saved picks']), s)
        self.assertEqual(dict((cid, name) for cid, _, name in s.feed_rows())[s.rid('weekpicks')], 'Our saved picks')

    def test_explicit_rows_survive_load_save_and_enable_via_cli_and_commander(self):
        from skald import setup
        from skald.__main__ import main
        s = self.s
        chosen = ['episodes', 'foryou', 'week', 'upcoming', 'catchup', 'people']
        path = self.root/'skald.json'
        for control in ('cli', 'commander'):
            with self.subTest(control=control), patch.dict(os.environ, {'DATA_DIR': str(self.root), 'SKALD_CONFIG': ''}), \
                    patch('skald.aio_refresh.refresh', return_value=None), patch.object(s, 'aio_rows_changed'):
                legacy = members.fresh_config(['ana', 'ben'])
                legacy['household'].pop('weekpicks_added')
                legacy['household'].update(rows=list(chosen), row_order=list(chosen))
                path.write_text(json.dumps(legacy))
                loaded, source = s.load_config(str(self.root), environ={})
                self.assertEqual(source, 'file')
                self.assertEqual(loaded['household']['rows'], chosen)
                setup.publish(self.root, loaded, replace=True)
                self.assertEqual(json.loads(path.read_text())['household']['rows'], chosen)
                s.refresh_preferences()
                self.assertNotIn(s.rid('weekpicks'), [cid for cid, _, _ in s.feed_rows(profiles=False)])
                self.assertEqual(s.row_metas(s.rid('weekpicks'), 'tv', s.LANG), [])
                if control == 'cli':
                    out = io.StringIO()
                    self.assertEqual(main(['setup', 'rows', ','.join(chosen + ['weekpicks']), '--json'], out=out), 0)
                    self.assertTrue(json.loads(out.getvalue())['ok'])
                else:
                    plan = commands.preview(s, 'show the weekpicks row', viewer='ana')
                    self.assertEqual(plan['change']['op'], 'row_show')
                    self.assertEqual(plan['change']['values'], ['weekpicks'])
                    commands.apply(s, plan)
                s.refresh_preferences()
                self.assertEqual(json.loads(path.read_text())['household']['rows'], chosen + ['weekpicks'])
                with patch.object(s, 'pin_shown'), patch.object(s, 'row_metas', return_value=[card(0)]):
                    ids = [row['id'] for row in s.manifest(s.LANG)['catalogs']]
                self.assertEqual(ids[ids.index(s.rid('week'))+1], s.rid('weekpicks'))

    def test_existing_default_rows_still_include_weekpicks(self):
        s = self.s
        legacy = members.fresh_config(['ana', 'ben'])
        legacy['household'].pop('weekpicks_added')
        legacy['household']['row_order'] = ['week', 'people']
        upgraded = s.validate_config(legacy)
        self.assertNotIn('rows', upgraded['household'])
        self.assertEqual(upgraded['household']['row_order'], ['week', 'weekpicks', 'people'])
        with patch.object(s, 'CONFIG', upgraded):
            rows = [cid for cid, _, _ in s.feed_rows(profiles=False)]
        self.assertEqual(rows[rows.index(s.rid('week'))+1], s.rid('weekpicks'))

    def test_one_call_freezes_across_languages_restart_and_next_monday(self):
        self.picker()
        s = self.s
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(self.answer())}}]})
            first = s.weekpicks_feed(s.LANG)
            self.assertEqual(len(first[s.rid('weekpicks')]['tv']), 9)
            self.assertEqual(s.weekpicks_feed('de-DE'), first)
            self.assertEqual(replay.calls, 1)
            prompt = json.loads(replay.bodies[0]['messages'][1]['content'].split('\n')[-1])
            self.assertEqual(prompt['scopes']['ana']['loved'], [{'i': 0, 'title': 'Invented Loved Ana'}])
            first[s.rid('weekpicks')]['tv'][0]['_row']['overview'] = 'changed by a caller'
            self.assertNotIn('changed by a caller', json.dumps(s.weekpicks_feed(s.LANG)))
            first = s.weekpicks_feed(s.LANG)
        other = load_server(self.root)
        try:
            other.load_state()
            with patch.object(other, 'local_now', return_value=self.now), patch.object(other, 'llm_text', side_effect=AssertionError('second call')):
                self.assertEqual(other.weekpicks_feed(other.LANG), first)
        finally:
            close_server(other)
        self.now += dt.timedelta(days=5)
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())) as llm:
            s.weekpicks_feed(s.LANG)
            self.assertEqual(llm.call_count, 1)

    def test_failure_no_key_budget_and_failed_reservation_never_fallback_or_retry(self):
        self.picker()
        s = self.s
        for failure in (TimeoutError('fake'), s.BudgetExceeded('fake'), None):
            s._learn.pop('weekpicks', None)
            s._usage.pop('weekpicks_week', None)
            with patch.object(s, 'OR_KEY', 'synthetic' if failure else ''), patch.object(s, 'llm_text', side_effect=failure) as llm:
                self.assertEqual(s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv'], [])
                s.weekpicks_feed(s.LANG)
                self.assertEqual(llm.call_count, int(failure is not None))
        s._learn.pop('weekpicks', None)
        with patch.object(s, 'save_learn', return_value=False), patch.object(s, 'llm_text') as llm:
            self.assertEqual(s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv'], [])
            llm.assert_not_called()

    def test_reject_seen_veto_unknown_scope_and_plain_reason_for_bad_anchor(self):
        self.picker()
        s = self.s
        s._profiles['household'].update(seen={'tmdb:0'}, negative={'tmdb:1'})
        answer = self.answer()
        answer['ana'][2]['anchor'] = 99
        answer['unknown'] = answer['ben']
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(answer)):
            cards = s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv']
        self.assertEqual([x['id'] for x in cards], [f'tmdb:{n}' for n in range(2, 9)])
        self.assertEqual(cards[0]['_week']['ana'], 'For Ana')
        self.assertEqual(cards[1]['_week']['ben'], 'For Ben · like Invented Loved Ben')
        self.assertNotIn('Unreviewed', json.dumps(s._learn['weekpicks']))

    def test_new_release_dedupe_and_personal_before_together_with_safe_homes(self):
        self.picker()
        s = self.s
        self.put([card(0)], 'week')
        s._learn['week'] = [dict(week=s.week_start(), people=s.week_people(), picks=[dict(id='tmdb:0')])]
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())):
            cards = s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv']
        self.assertNotIn('tmdb:0', [x['id'] for x in cards])
        cfg = members.fresh_config(['ana', 'ben', 'kim', 'lena'])
        cfg['household']['guests'] = ['lena']
        cfg['taste']['people']['kim']['prefs'] = {'kids_age': 9}
        with patch.object(s, 'CONFIG', cfg), patch.object(s, 'PEOPLE', tuple(cfg['household']['members'])), \
                patch.object(s, 'ROW_MIN', 1), patch.object(s, 'row_eligible', return_value=True):
            self.put(list(reversed(cards)), 'weekpicks')
            serve = lambda who, above=set(): s.served_row(s.rid('weekpicks'), 'tv', s.LANG, s._profiles['household'], above, (set(), set()), who=who)
            self.assertEqual([x['id'] for x in serve('ana')], ['tmdb:2', 'tmdb:1', 'tmdb:8', 'tmdb:7', 'tmdb:6'])
            for who in ('kim', 'lena', s.GUEST):
                self.assertTrue(all(set(x['_week']) == {'together'} for x in serve(who)))
            self.assertNotIn('tmdb:1', [x['id'] for x in serve('ana', {'tmdb:1'})])

    def test_personal_row_does_not_change_the_recent_release_candidate_pool(self):
        s = self.s
        self.put([card(0)], 'weekpicks')
        self.put([card(1)])
        self.assertEqual([x['id'] for x in s.week_candidates(s.LANG)], ['tmdb:1'])

    def test_recall_cli_op_eight_weeks_validation_and_forget(self):
        from skald.engine import Engine
        from skald.__main__ import execute, parser
        self.picker()
        s = self.s
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())):
            for _ in range(10):
                s.weekpicks_feed(s.LANG)
                self.now += dt.timedelta(weeks=1)
        self.now -= dt.timedelta(weeks=1)
        self.assertEqual(len(s._learn['weekpicks']), 8)
        self.assertEqual(sum('cards' in r for r in s._learn['weekpicks']), 1)
        expected = s.week_history(1, 'ana')
        self.assertEqual(expected['weeks'], [])
        self.assertEqual(len(expected['personal'][0]['picks']), 3)
        with Engine(self.root) as engine:
            self.assertEqual(execute(parser().parse_args(['rows', 'week', '--person', 'ana', '--json']), engine, None, None), expected)
            with patch.object(engine._server, 'local_now', return_value=self.now):
                result = commands.preview(engine._server, '', viewer='ana', answer=dict(op='week_picks', kind=None, values=[1, 0], person='ana'))
                self.assertEqual(result['week_picks'], expected)
        s._learn['weekpicks'] += [None, {}, dict(week='bad', people=[], picks=[])]
        s.load_week_history()
        self.assertEqual(len(s._learn['weekpicks']), 8)
        # Forget already discards learned history and serving caches for every member.
        s.forget_person_caches()
        self.assertFalse((self.root/'learn.json').exists())
        cfg = copy.deepcopy(s.CONFIG)
        from skald.leave import settings
        settings(cfg, 'ben')
        self.assertEqual(row_names.describe(s.rid('weekpicks'), {}, cfg, 'en')['label'], '✨ This Week for Ana')

    def test_manifest_and_aiostreams_refresh_payload_place_after_week(self):
        from skald import aio_refresh, setup
        from test_setup_aiostreams import FakeAIO
        s = self.s
        self.put([card(n) for n in range(3)], 'weekpicks')
        with patch.object(s, 'pin_shown'), patch.object(s, 'row_metas', return_value=[card(n) for n in range(3)]):
            ids = [x['id'] for x in s.manifest(s.LANG)['catalogs']]
        self.assertEqual(ids[ids.index(s.rid('week'))+1], s.rid('weekpicks'))
        fake = FakeAIO()
        prefix = 'synthetic.'
        fake.catalogs = [dict(id=prefix+s.rid('week')), dict(id=prefix+s.rid('weekpicks'))]
        fake.configs['synthetic-login'] = {'presets': [{}], 'catalogModifications': [dict(id=prefix+s.rid('week'), type='tv', enabled=True)]}
        (self.root/'setup.json').write_text(json.dumps({'aio_url': 'https://aio.invalid'}))
        with patch.object(setup, 'aiostreams_logins', return_value={'household': dict(uuid='synthetic-login', password='synthetic')}):
            self.assertIn('refreshed', aio_refresh.refresh(self.root, transport=fake, server=s))
        ids = [x['id'] for x in fake.calls[-1][2]['config']['catalogModifications']]
        self.assertEqual(ids[ids.index(prefix+s.rid('week'))+1], prefix+s.rid('weekpicks'))

    def test_commander_and_odin_use_the_filled_localized_label(self):
        from skald import rowplan
        s = self.s
        cid = s.rid('weekpicks')
        self.put([card(n) for n in range(6)], 'weekpicks')
        for lang, expected in [('en', '✨ This Week for Ana & Ben'),
                               ('de', '✨ Diese Woche für Ana & Ben')]:
            with self.subTest(lang=lang), patch.dict(s.CONFIG['region'], row_lang=lang):
                with patch.object(s, 'pin_shown'), patch.object(s, 'row_metas', return_value=[card(n) for n in range(6)]):
                    catalog = next(r for r in s.manifest(s.LANG)['catalogs'] if r['id'] == cid)
                self.assertEqual(catalog['name'], expected)
                rows = rowplan.command_input(s, '', viewer='ana')['rows']
                self.assertEqual(next(r['label'] for r in rows if r['id'] == cid), expected)
                self.assertEqual(rowplan.row_id(s, expected), cid)
                proposal = commands.preview(s, '', 'ana', answer=dict(
                    op='row_refresh', kind=None, values=[expected], person=None))
                self.assertEqual(proposal['name'], 'Refresh '+expected+' for the household')
        s.CONFIG['household']['row_labels'] = {'weekpicks': 'Our saved picks'}
        self.assertEqual(rowplan.label(s, cid, s.LANG), 'Our saved picks')

    def test_why_shows_one_to_five_personal_picks_like_home(self):
        from skald import rowplan
        s = self.s
        cid = s.rid('weekpicks')
        s.CONFIG['household']['rows'] = ['weekpicks']
        with patch.object(s, 'row_eligible', return_value=True):
            for count in range(1, 6):
                with self.subTest(count=count):
                    self.put([{**card(n), '_week': {'ana': 'For Ana'}, '_week_start': s.week_start()}
                              for n in range(count)], 'weekpicks')
                    shown = s.served_row(cid, 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), who='ana')
                    self.assertEqual(len(shown), count)
                    row = rowplan.why(s, 'weekpicks', 'ana')
                    self.assertEqual(row['position'], 1)
                    self.assertIsNone(row['rejection'])
                    self.assertEqual(row['own_cards'], count)

    def test_health_keeps_personal_weekly_feed_fresh_for_seven_days(self):
        from skald import health
        s = self.s
        cid, lang, built = s.rid('weekpicks'), s.LANG, 1_000_000
        s._feeds[cid, lang] = {'built': built, 'metas': {}}
        check = health.key('feed', ('weekpicks', cid, lang))
        observation = s.health_snapshot()
        self.assertEqual(observation['feeds'][check]['interval'], 7*s.DAY)
        observation.update(worker_alive=True, disk_free=1024**3)
        (self.root/'health-settings.json').write_text(json.dumps({'enabled': True}))
        now = built + 6*s.DAY
        monitor = health.Health(self.root, clock=lambda: now)
        result = monitor.tick(observation)
        self.assertEqual(next(c['state'] for c in result['checks'] if c['id'] == check), 'ok')
        now = built + 7*s.DAY + 301
        result = monitor.tick(observation)
        self.assertEqual(next(c['state'] for c in result['checks'] if c['id'] == check), 'error')

    def test_vetoes_adult_filter_and_child_ratings_reuse_row_rules(self):
        self.picker()
        s = self.s
        s._facets['tmdb:0'] = {'kind': 'movie', 'adult': True}
        with patch.object(s, 'veto_keys', return_value={'tmdb:1'}), \
                patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())):
            cards = s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv']
        self.assertNotIn('tmdb:0', [x['id'] for x in cards])
        self.assertNotIn('tmdb:1', [x['id'] for x in cards])
        self.put(cards, 'weekpicks')
        s.CONFIG['taste']['people']['ben']['prefs'] = {'kids_age': 9}
        with patch.object(s, 'PEOPLE', (*s.PEOPLE, 'kim')):
            s.CONFIG['taste']['people']['kim'] = {'prefs': {'kids_age': 9}}
            self.assertEqual(s.served_row(s.rid('weekpicks'), 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), who='kim'), [])

    def test_shared_card_never_exposes_another_persons_anchor(self):
        self.picker()
        s = self.s
        answer = self.answer()
        answer['together'][0]['title'] = answer['ana'][0]['title']
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(answer)):
            self.put(s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv'], 'weekpicks')
        for who in ('ben', s.GUEST):
            cards = s.served_row(s.rid('weekpicks'), 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), who=who)
            self.assertTrue(cards)
            self.assertNotIn('Invented Loved Ana', json.dumps(s.human_cards(cards, who)))

    def test_public_cards_keep_the_fixed_anchor_note_beside_a_synopsis(self):
        self.picker()
        s = self.s
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())):
            cards = s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv']
        for x in cards:
            x['_row']['overview'] = 'An invented quiet mystery.'
        self.put(cards, 'weekpicks')
        for lang, note in [('en', 'For Ana · like Invented Loved Ana'), ('de', 'Für Ana · wie Invented Loved Ana'),
                           ('sr-latn', 'Za Ana · kao Invented Loved Ana'), ('sr-cyrl', 'За Ana · као Invented Loved Ana')]:
            with self.subTest(lang=lang), patch.dict(s.CONFIG['region'], row_lang=lang):
                shown = s.served_row(s.rid('weekpicks'), 'tv', s.LANG, s._profiles['household'], set(), (set(), set()), who='ana')
                public = s.human_cards(shown, 'ana')
                self.assertTrue(public)
                self.assertEqual(public[0]['description'], note + ' · An invented quiet mystery.')

    def test_feedback_accepts_row_and_bound_api_keeps_household_privacy(self):
        s = self.s
        cid = s.rid('weekpicks')
        self.assertIn(cid, s.REMOTE_ROWS)
        event = s.validate_feedback(dict(key='tmdb:1', kind='movie', title='Invented Letter B', who='ana', action='like', row=cid))
        self.assertEqual(event['row'], cid)
        req = dict(viewer='ana', query={})
        with patch.object(s, 'member_view'), patch.object(s, 'metrics', return_value={'rows': {cid: {'shown': 5}}}):
            self.assertNotIn(cid, s.metrics_view(req)['metrics']['rows'])
        with patch.object(s, 'feed_rows', return_value=[(cid, ('tv',), 'Our saved picks')]), patch.object(s, 'row_metas') as row:
            self.assertEqual(s.rows_view(req)['rows'], [])
            row.assert_not_called()

    def test_forget_clears_personal_history_and_label_through_existing_flow(self):
        self.picker()
        s = self.s
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())):
            s.weekpicks_feed(s.LANG)
        plan = commands.preview(s, '', 'ana', answer=dict(op='member_forget', person='ben', shared='keep'))
        plan['source'] = 'cli'
        commands.apply(s, plan)
        self.assertFalse((self.root/'learn.json').exists())
        self.assertEqual(s.week_history()['personal'], [])
        self.assertEqual(row_names.describe(s.rid('weekpicks'), {}, s.CONFIG, 'en')['label'], '✨ This Week for Ana')
        other = load_server(self.root)
        try:
            other.load_state()
            other._profiles['household'] = {'entries': [], 'seen': set(), 'negative': set(), 'searches': []}
            with patch.object(other, 'local_now', return_value=self.now), patch.object(other, 'get_model', return_value={'cal': True}), \
                    patch.object(other, 'OR_KEY', 'synthetic'), patch.object(other, 'llm_text', return_value='{}') as llm:
                self.assertEqual(other.weekpicks_feed(other.LANG)[other.rid('weekpicks')]['tv'], [])
                llm.assert_not_called()
        finally:
            close_server(other)
        s._profiles['household'] = {'entries': [], 'seen': set(), 'negative': set(), 'searches': []}
        with patch.object(s, 'OR_KEY', 'synthetic'), patch.object(s, 'llm_text', return_value=json.dumps(self.answer())) as llm:
            self.assertEqual(s.weekpicks_feed(s.LANG)[s.rid('weekpicks')]['tv'], [])
            llm.assert_not_called()
