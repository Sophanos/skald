"""Separate household labels from title metadata, entirely offline and synthetic."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from decide_kit import ReplayLLM, close_server, load_server, reset
from skald import commands, members, row_names, rowplan, setup
from skald.__main__ import main


class RowLanguage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.cfg = members.fresh_config(['ana', 'ben'])
        self.cfg['region']['lang'] = 'de-DE'
        self.write(self.cfg)
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.enterContext(patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'fake',
                                                'SKALD_SNAPSHOT': '', 'SKALD_SNAPSHOT_U': ''}, clear=True))
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'ensure_emb', side_effect=AssertionError('embedding forbidden')))
        self.addCleanup(members.configure)

    def write(self, cfg):
        self.data.joinpath('skald.json').write_text(json.dumps(cfg))

    def test_config_accepts_only_bundled_label_languages(self):
        for lang in ('en-US', 'de-DE', 'sr-Latn-RS', 'sr-Cyrl-RS', 'en', 'de', 'sr-latn', 'sr-cyrl'):
            with self.subTest(lang=lang):
                self.cfg['region']['row_lang'] = lang
                self.assertEqual(members.validate_config(self.cfg)['region']['row_lang'], lang)
                self.assertEqual(members.public_config(self.cfg)['region']['row_lang'], lang)
        for lang in ('fr-FR', 'xx-XX', 'en-unknown', '', None, 7, ['en-US']):
            with self.subTest(lang=lang):
                self.cfg['region']['row_lang'] = lang
                with self.assertRaisesRegex(ValueError, 'region.row_lang.*locale'):
                    members.validate_config(self.cfg)

    def manifest_names(self):
        s = self.s
        with patch.object(s, 'pin_shown'), patch.object(s, 'feed', return_value={}), \
                patch.object(s, 'default_rows', return_value=False), patch.object(s, 'row_metas', return_value=[{}]*30):
            return {r['id']: r['name'] for r in s.manifest(s.LANG)['catalogs']}

    def test_manifest_labels_and_dynamic_names_use_override(self):
        s = self.s
        s.CONFIG['region']['row_lang'] = 'en-US'
        self.assertEqual(self.manifest_names()[s.rid('foryou')], '✨ For You')
        with patch.object(s, 'feed', return_value={'names': {'mood': 'cozy'}}):
            self.assertEqual(s.row_name(s.rid('mood1'), s.LANG), 'Cozy Mysteries')
        self.assertEqual(s.t('note.rank', rank=1), '#1 this week')
        self.assertEqual(s.lane_title('x:cozy_case', '', []), 'Cozy Case-of-the-Week')
        self.assertIn('/de-DE/manifest.json', s.addon_urls('https://tv.example')[0])

    def test_missing_override_and_same_keep_existing_output(self):
        s = self.s
        before = self.manifest_names()
        stamps = [x[2] for x in s.job_specs(s.local_now(), {})]
        self.assertEqual(before[s.rid('foryou')], '✨ Für dich')
        self.assertEqual(s.t('note.rank', rank=1), '#1 diese Woche')
        cfg = setup.household(self.data, None, None, None, None, row_lang='en-US')
        self.assertEqual(cfg['region']['lang'], 'de-DE')
        cfg = setup.household(self.data, None, None, None, None, row_lang='same')
        self.assertNotIn('row_lang', cfg['region'])
        self.assertEqual(cfg, self.cfg)
        self.assertEqual(before, self.manifest_names())
        self.assertEqual(stamps, [x[2] for x in s.job_specs(s.local_now(), {})])

    def test_tmdb_metadata_and_feed_keys_keep_title_language(self):
        s = self.s
        s.CONFIG['region']['row_lang'] = 'en-US'
        def tmdb(path, **params):
            german = params['language'] == 'de-DE'
            return {'results': [{'id': 99001, 'title': 'Erfundene Laterne' if german else 'Invented Lantern',
                                 'overview': 'Erfundene Handlung.' if german else 'Invented plot.',
                                 'poster_path': '/de.jpg' if german else '/en.jpg'}]}
        specs = [('top10', [s.rid('top10')], 'today', 0, lambda lang: {s.rid('top10'): s.top10_feed(lang)})]
        with patch.object(s, 'tmdb', side_effect=tmdb) as transport, patch.object(s, 'genres', return_value={}), \
                patch.object(s, 'match_note', return_value=[]), patch.object(s, 'refresh_profiles', return_value={}), \
                patch.object(s, 'job_specs', return_value=specs), patch.object(s, 'pin_decision'), \
                patch.object(s, 'save_feeds'):
            s.feed_pass(s.local_now(), {})
        self.assertEqual({c.kwargs['language'] for c in transport.call_args_list}, {'de-DE', 'en-US'})
        self.assertEqual(set(s._feeds), {(s.rid('top10'), 'de-DE')})
        card = s._feeds[s.rid('top10'), 'de-DE']['metas']['movie'][0]
        self.assertEqual(card['name'], 'Erfundene Laterne')
        self.assertIn('Erfundene Handlung.', card['description'])
        self.assertIn('#1 this week', card['description'])
        with patch.object(s, 'tmdb', side_effect=tmdb), patch.object(s, 'genres', return_value={}), \
                patch.object(s, 'match_note', return_value=[]):
            s.CONFIG['region'].pop('row_lang')
            self.assertEqual(s.top10_feed(s.LANG)['movie'][0]['poster'], card['poster'])

    def test_cached_label_jobs_rebuild_on_override(self):
        s = self.s
        def stamps():
            return {name: stamp for name, _, stamp, _, _ in s.job_specs(s.local_now(), {})}
        before = stamps()
        s.CONFIG['region']['row_lang'] = 'en-US'
        self.assertTrue(all(before[k] != v for k, v in stamps().items()))
        s.CONFIG['region'].pop('row_lang')
        self.assertEqual(before, stamps())

    def test_persisted_pins_follow_label_language_after_restart_and_failed_rebuild(self):
        s = self.s
        now = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
        cid = s.rid('because')
        scope = next(iter(s.SCOPES))
        def build(lang):
            self.assertEqual(lang, 'de-DE')
            cards = [{'id': f'tmdb:{99001+i}', 'type': 'movie', 'name': 'Erfundene Laterne',
                      'description': s.t('note.rank', rank=1)+' · Erfundene Handlung.',
                      '_row': {'overview': 'Erfundene Handlung.'}} for i in range(30)]
            return {cid: {'tv': cards}, '_names': {cid: {'label_key': 'row.seed.loved',
                                                       'fields': {'title': 'Erfundene Saat'}}}}
        def contexts(server):
            self.enterContext(patch.object(server, 'local_now', return_value=now))
            self.enterContext(patch.object(server, 'get_model', return_value=None))
            self.enterContext(patch.object(server, 'connectors_tick'))
        contexts(s)
        with patch.object(s, 'because_feed', side_effect=build):
            s.build_feeds(now, {}, {}, only={'because'})
        original = s.feed(cid, s.LANG)
        s.pin_shown(s.LANG)
        s.pin_shown(s.LANG, scope)
        self.assertTrue(self.data.joinpath('shown.json').exists())
        self.assertTrue(self.data.joinpath('shown_scopes.json').exists())
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        contexts(s)
        s.load_state()
        for selected in (None, scope):
            with s.addon_scope(selected):
                self.assertEqual(s.feed(cid, s.LANG), original)
        for lang in ('en-US', 'same'):
            with self.subTest(lang=lang):
                setup.household(self.data, None, None, None, None, row_lang=lang)
                s = load_server(self.data)
                self.addCleanup(close_server, s)
                contexts(s)
                s.load_state()
                for result in (None, RuntimeError('synthetic failed rebuild')):
                    with patch.object(s, 'because_feed', return_value=None, side_effect=result):
                        s.build_feeds(now, {}, {}, only={'because'})
                    for selected in (None, scope):
                        with s.addon_scope(selected):
                            self.assertEqual(s.feed(cid, s.LANG), {})
                            self.assertEqual(s.source_metas(cid, s.LANG), [])
                with patch.object(s, 'because_feed', side_effect=build):
                    s.build_feeds(now, {}, {}, only={'because'})
                for selected in (None, scope):
                    with s.addon_scope(selected):
                        rec = s.feed(cid, s.LANG)
                        self.assertIn(s.t('note.rank', rank=1), rec['metas']['tv'][0]['description'])
                        self.assertEqual(rec['metas']['tv'][0]['name'], 'Erfundene Laterne')
                        self.assertIn('Erfundene Handlung.', rec['metas']['tv'][0]['description'])
                        self.assertEqual(s.row_name(cid, s.LANG), s.t('row.seed.loved', title='Erfundene Saat'))
                self.assertEqual({lang for _, lang in s._feeds}, {'de-DE'})
                s.pin_shown(s.LANG)
                s.pin_shown(s.LANG, scope)
        self.assertEqual(s.feed(cid, s.LANG)['metas'], original['metas'])

    def test_search_notice_uses_labels_language(self):
        s = self.s
        s.CONFIG['region']['row_lang'] = 'en-US'
        with patch.object(s, 'OR_KEY', ''), patch.object(s, 'tmdb', return_value={}), \
                patch.object(s, 'genres', return_value={}), patch.object(s, 'row_pool', return_value=[]), \
                patch.object(s.search_commands, 'special', return_value=None), patch.object(s, 'get_model', return_value=None):
            card = s.search('movie', 'invented quiet mysteries for tonight', s.LANG)['metas'][0]
        self.assertIn(s.t('search.reason.config'), card['description'])
        self.assertEqual(card['name'], s.t('search.unavailable.title'))

    def test_names_cli_reports_title_and_label_languages_and_previews(self):
        self.cfg['region']['row_lang'] = 'en-US'
        self.write(self.cfg)
        for flags, label, language in (([], '✨ For You', 'en-US'), (['--lang', 'de-DE'], '✨ Für dich', 'de-DE')):
            out = io.StringIO()
            self.assertEqual(main(['rows', 'names', '--json', *flags], out), 0, out.getvalue())
            data = json.loads(out.getvalue())
            self.assertEqual((data['lang'], data['row_lang'], data['language']), ('de-DE', 'en-US', language))
            self.assertEqual(next(r['label'] for r in data['rows'] if r['id'] == self.s.rid('foryou')), label)
        self.assertEqual(json.loads(self.data.joinpath('skald.json').read_text()), self.cfg)

    def test_names_preview_keeps_legacy_cache_choice_without_override(self):
        cid = self.s.rid('because')
        self.data.joinpath('feeds.json').write_text(json.dumps({cid+'|'+lang: {'names': {
            'label_key': 'row.seed.loved', 'fields': {'title': title}}}
            for lang, title in (('en-US', 'Invented Seed'), ('de-DE', 'Erfundene Saat'))}))
        def label():
            return next(r['label'] for r in row_names.view(self.data, self.cfg, 'en-US')['rows'] if r['id'] == cid)
        self.assertIn('Invented Seed', label())
        self.cfg['region']['row_lang'] = 'en-US'
        self.assertIn('Erfundene Saat', label())

    def test_names_cli_uses_unbuilt_shelf_fallback_with_override(self):
        self.cfg['region']['row_lang'] = 'en-US'
        self.write(self.cfg)
        self.data.joinpath('shelves.json').write_text(json.dumps({'wishes': [
            {'scope': 'ana', 'slot': 1, 'name': 'Quiet Evenings', 'text': 'Invented quiet stories'}]}))
        out = io.StringIO()
        self.assertEqual(main(['rows', 'names', '--json'], out), 0, out.getvalue())
        rows = json.loads(out.getvalue())['rows']
        self.assertEqual(next(r['label'] for r in rows if r['id'] == self.s.rid('shelf_ana_1')), 'Quiet Evenings')

    def test_setup_cli_sets_removes_reports_and_requires_restart(self):
        setup.write_private(self.data/'setup.json', json.dumps({'base': 'https://tv.example', 'watch': 'stremio',
                                                              'taste': 'files', 'tmdb_checked': True}))
        setup.record_startup(self.data, self.cfg)
        self.enterContext(patch.object(setup, 'check_tmdb', return_value=True))
        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            for lang, expected in (('en-US', 'en-US'), ('same', 'de-DE')):
                out = io.StringIO()
                self.assertEqual(main(['setup', '--yes', '--row-lang', lang, '--json'], out), 0, out.getvalue())
                result = json.loads(out.getvalue())
                self.assertEqual((result['lang'], result['row_lang']), ('de-DE', expected))
                cfg = json.loads(self.data.joinpath('skald.json').read_text())
                self.assertEqual(cfg['region'].get('row_lang', cfg['region']['lang']), expected)
                if lang == 'same':
                    self.assertNotIn('row_lang', cfg['region'])
                else:
                    self.assertIn(setup.RESTART, result['next'])
                status = io.StringIO()
                self.assertEqual(main(['setup', '--status', '--json'], status), 0)
                self.assertEqual(json.loads(status.getvalue())['row_lang'], expected)
        before = self.data.joinpath('skald.json').read_bytes()
        for lang in ('fr-FR', ''):
            out = io.StringIO()
            with self.subTest(lang=lang), redirect_stderr(out):
                self.assertEqual(main(['setup', '--yes', '--row-lang', lang], out), 2)
            self.assertIn('region.row_lang', out.getvalue())
            self.assertIn('locale', out.getvalue())
            self.assertEqual(before, self.data.joinpath('skald.json').read_bytes())

    def test_ai_label_prompts_and_caches_use_override(self):
        s = self.s
        s.CONFIG['region']['row_lang'] = 'en-US'
        brief = {'scope': 'member', 'experiences': ['cozy_case'], 'themes': [], 'groups': ['cozy']}
        self.assertEqual(json.loads(s.archetype_request(brief, 'fake', 'none')['messages'][1]['content'])['language'], 'en-US')
        chosen = [('k:1', [{'name': 'Erfundene Laterne'}], 1, [])]
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"names":["Quiet Evenings"]}'}}]})
            self.assertEqual(s.name_lanes(chosen, {'names': {'k:1': 'quiet life'}}), {'k:1': 'Quiet Evenings'})
            self.assertIn('en-US', replay.bodies[0]['messages'][1]['content'])
        self.assertEqual(s._learn['lanes']['k:1']['titles'], {'en-us': 'Quiet Evenings'})

    def test_week_reasons_change_language_without_another_model_call(self):
        s = self.s
        now = dt.datetime(2026, 10, 14, 12, tzinfo=dt.timezone.utc)
        card = {'id': 'tmdb:99001', 'type': 'movie', 'name': 'Erfundene Laterne', 'description': 'Erfundene Handlung.',
                '_row': {'overview': 'Erfundene Handlung.', 'watchable': '2026-10-12'}, '_week_pct': 90}
        with patch.object(s, 'local_now', return_value=now), patch.object(s, 'get_model', return_value={'cal': True}), \
                patch.object(s, 'week_candidates', return_value=[card]), patch.object(s, 'week_scope', return_value=[card]), \
                patch.object(s, 'week_spread', side_effect=lambda xs: xs), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"ana":[{"i":0,"reason":"Erfundener Grund."}],"ben":[],"together":[]}'}}]})
            self.assertIn('Erfundener Grund.', s.week_feed(s.LANG)[s.rid('week')]['tv'][0]['description'])
            s.CONFIG['region']['row_lang'] = 'en-US'
            with patch.object(s, 'get_model', side_effect=AssertionError('cached week needs no model')):
                result = s.week_feed(s.LANG)[s.rid('week')]['tv'][0]
            self.assertIn(s.t('week.taste'), result['description'])
            self.assertIn('Erfundene Handlung.', result['description'])
            self.assertEqual(result['name'], 'Erfundene Laterne')
            self.assertEqual(replay.calls, 1)
            self.assertEqual(s.week_feed(s.LANG)[s.rid('week')]['tv'][0], result)
            self.assertEqual([p['id'] for p in s._learn['week'][-1]['picks']], ['tmdb:99001'])

    def test_historical_week_reasons_localize_with_and_without_cards(self):
        s = self.s
        pick = {'id': 'tmdb:99001', 'member': 'ana', 'reason': 'Erfundener Grund.',
                'kind': 'movie', 'title': 'Erfundene Laterne'}
        s._learn['week'] = [{'week': week, 'people': s.week_people(), 'picks': [{**pick, 'week': week}]}
                            for week in ('2026-10-05', '2026-10-12')]
        s._learn['week'][-1]['cards'] = [{'id': pick['id'], '_row': {'watchable': '2026-10-12'}}]
        with patch.object(s, 'week_start', return_value='2026-10-12'):
            before = s.week_history(2)
            self.assertTrue(all(w['picks'][0]['reason'] == pick['reason'] for w in before['weeks']))
            s.CONFIG['region']['row_lang'] = 'en-US'
            result = s.week_history(2, 'ana')
            self.assertTrue(all(s.t('week.taste') in w['picks'][0]['reason'] for w in result['weeks']))
            self.assertTrue(all(w['picks'][0]['title'] == pick['title'] for w in result['weeks']))
            self.assertEqual(result, s.week_history(2, 'ana'))
            s.CONFIG['region'].pop('row_lang')
            self.assertEqual(before, s.week_history(2))

    def test_commander_row_replies_translate_only_with_override(self):
        s = self.s
        cases = [('row_lang', ['en-US'], 'Reihennamen und Kartenbeschriftungen'),
                 ('row_rename', ['foryou', 'Invented Picks'], 'Umbenennen'),
                 ('row_hide', ['top10'], 'Ausblenden'), ('row_show', ['top10'], 'Einblenden'),
                 ('row_move', ['top10', 'foryou'], 'Verschieben'), ('row_names', [], 'Reihennamen')]
        for op, values, expected in cases:
            with self.subTest(op=op):
                answer = dict(op=op, kind=None, values=values, person=None)
                before = commands.preview(s, op, 'ana', answer=answer)
                s.CONFIG['region']['row_lang'] = 'DE-de'
                translated = commands.preview(s, op, 'ana', answer=answer)
                self.assertIn(expected, translated['name'])
                s.CONFIG['region'].pop('row_lang')
                self.assertEqual(before, commands.preview(s, op, 'ana', answer=answer))

    def test_home_plan_replies_use_full_bundled_locale(self):
        s = self.s
        answer = dict(op='home_plan', kind=None, values=[], person=None)
        for rows in ([], [{'label': 'Invented Row', 'rejection': 'not_built', 'position': None}],
                     [{'label': 'Invented Row', 'rejection': None, 'position': 1, 'own_cards': 3, 'borrowed_cards': 2}]):
            with patch.object(rowplan, 'plan', return_value={'rows': rows}):
                legacy = commands.preview(s, 'home', 'ana', answer=answer)
                for lang, name, text in (('DE-de', 'Startseitenplan', 'Reihen'),
                                         ('sr-Cyrl-RS', 'План почетне стране', 'редова')):
                    with self.subTest(lang=lang, rows=rows):
                        s.CONFIG['region']['row_lang'] = lang
                        reply = commands.preview(s, 'home', 'ana', answer=answer)
                        self.assertEqual(reply['name'], name)
                        self.assertIn(text, reply['description'])
                        if rows:
                            self.assertIn('noch keine brauchbaren Titel' if lang == 'DE-de' and rows[0]['rejection'] else
                                          'још нема употребљивих наслова' if rows[0]['rejection'] else
                                          'eigene und' if lang == 'DE-de' else 'сопствених и', reply['description'])
                        s.CONFIG['region'].pop('row_lang')
                        self.assertEqual(legacy, commands.preview(s, 'home', 'ana', answer=answer))

    def test_commander_compound_confirm_restart_and_undo_requires_restart(self):
        s = self.s
        answer = dict(op='plan', kind=None, values=[], person=None, steps=[
            dict(op='row_lang', kind=None, values=['en-US'], person=None),
            dict(op='hide', kind=None, values=['anime'], person=None)])
        before = self.data.joinpath('skald.json').read_bytes()
        with patch.object(commands, 'context', return_value=('rowlang', None, 'ana')), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            commands.special(s, 'English row names and hide anime', 'movie', routed=True)
            plan = commands.get_pending(s, 'rowlang')
            self.assertEqual(before, self.data.joinpath('skald.json').read_bytes())
            self.assertIn(setup.RESTART, commands.special(s, 'yes '+commands.confirm_code(plan), 'movie')['metas'][0]['description'])
        s = load_server(self.data)
        self.addCleanup(close_server, s)
        self.assertEqual(s.row_lang(), 'en-US')
        self.assertEqual(commands.config(s)['taste']['people']['ana']['prefs']['hide'], ['anime'])
        with patch.object(commands, 'context', return_value=('rowlang', None, 'ana')):
            reply = commands.special(s, 'undo', 'movie')['metas'][0]
        self.assertIn(setup.RESTART, reply['description'])
        self.assertEqual(json.loads(before), commands.config(s))

    def test_commander_model_preview_confirm_journal_and_undo(self):
        s = self.s
        before = self.data.joinpath('skald.json').read_bytes()
        answer = dict(op='row_lang', kind=None, values=['en-US'], person=None)
        self.assertIsNone(commands.parse_local('show my row names in English', s)['op'])
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = commands.preview(s, 'show my row names in English', 'ana')
            self.assertEqual(replay.calls, 1)
        self.assertEqual(before, self.data.joinpath('skald.json').read_bytes())
        self.assertIn('en-US', plan['name'])
        self.assertIn(setup.RESTART, commands.apply(s, plan)['name'])
        self.assertEqual(json.loads(self.data.joinpath('skald.json').read_text())['region']['row_lang'], 'en-US')
        self.assertEqual(commands.journal(s)['ana'][-1]['change']['op'], 'row_lang')
        undo = commands.preview(s, 'undo', 'ana', answer=dict(op='undo', kind=None, values=[], person=None))
        commands.apply(s, undo)
        self.assertNotIn('row_lang', json.loads(self.data.joinpath('skald.json').read_text())['region'])
        self.assertTrue(commands.journal(s)['ana'][-1]['undone'])
        self.assertIn('row_lang', commands.command_prompt(s))

    def test_commander_refuses_invalid_language_and_stale_preview(self):
        s = self.s
        with self.assertRaisesRegex(ValueError, 'region.row_lang'):
            commands.preview(s, 'change', 'ana', answer=dict(op='row_lang', kind=None, values=['xx-XX'], person=None))
        plan = commands.preview(s, 'change', 'ana', answer=dict(op='row_lang', kind=None, values=['en-US'], person=None))
        self.cfg['region']['row_lang'] = 'sr-Latn-RS'
        self.write(self.cfg)
        with self.assertRaisesRegex(ValueError, 'Rows changed'):
            commands.apply(s, plan)

    def test_commander_same_and_undo_after_other_row_changes(self):
        s = self.s
        def apply(op, values):
            return commands.apply(s, commands.preview(s, op, 'ana', answer=dict(op=op, kind=None, values=values, person=None)))
        apply('row_lang', ['en-US'])
        apply('row_lang', ['same'])
        self.assertNotIn('row_lang', commands.config(s)['region'])
        apply('undo', [])
        self.assertEqual(commands.config(s)['region']['row_lang'], 'en-US')
        apply('row_rename', ['foryou', 'Invented Picks'])
        apply('undo', [])
        self.assertIn(setup.RESTART, apply('undo', [])['name'])
        self.assertNotIn('row_lang', commands.config(s)['region'])

    def test_multilingual_commands_contract_uses_model_only(self):
        from eval.commands_eval import load_cases
        cases = [c for c in load_cases() if c['group'] == 'row language']
        self.assertEqual(len(cases), 10)
        with ReplayLLM(self.s) as replay:
            for case in cases:
                with self.subTest(case=case['id']):
                    reset(self.s)
                    self.assertEqual(case['contract'], 'now')
                    self.assertIsNone(commands.parse_local(case['text'], self.s)['op'])
                    replay.arm(case['recorded'])
                    answer = self.s.decide('parse_command', {'query': case['text']})['answer']
                    self.assertTrue(all(answer.get(k) == v for k, v in case['expect'].items()), answer)
            self.assertEqual(replay.calls, len(cases))


if __name__ == '__main__':
    unittest.main()
