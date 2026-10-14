"""Household labels, cached lane names and read-only inspection, all synthetic."""
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from decide_kit import ReplayLLM, close_server, load_server, reset
from skald import commands, i18n, members, migrate
from skald.__main__ import main


class Names(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['region']['lang'] = 'de-DE'
        cfg['household']['member_rows'] = ['ana']
        self.data.joinpath('skald.json').write_text(json.dumps(cfg))
        self.s = load_server(self.data)
        self.addCleanup(close_server, self.s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))

    def test_labels_resolve_at_use_in_household_language(self):
        s = self.s
        with patch.object(s, 'feed', return_value={}), patch.object(s, 'default_rows', return_value=False):
            rows = {cid: name for cid, _, name in s.feed_rows('en-US', profiles=False)}
        self.assertEqual(rows[s.rid('foryou')], '✨ Für dich')
        self.assertEqual(rows[s.rid('episodes')], 'Weiterschauen')
        self.assertEqual(rows[s.rid('week')], '✨ Neu diese Woche')
        self.assertEqual(rows[s.rid('wonder')], 'Wunder unserer Welt')
        self.assertEqual(s.lane_title('x:cozy_case', '', []), 'Gemütliche Krimifälle')
        with patch.object(s, 'LANG', 'sr-latn'):
            self.assertEqual(s.t('row.foryou'), '✨ Za tebe')
            self.assertEqual(s.lane_title('x:cozy_case', '', []), 'Ušuškani krimići')
        with patch.object(s, 'get_model', return_value=None):
            self.assertEqual(s.confidence(None, 'tmdb:99001', None, []), (None, None))
        self.assertEqual(s.t('note.rank', rank=2), '#2 diese Woche')

    def test_all_translations_and_week_contract(self):
        for lang in ('de', 'sr-latn', 'sr-cyrl'):
            self.assertEqual(set(i18n._STRINGS[lang]), set(i18n._STRINGS['en']))
            for key, source in i18n._STRINGS['en'].items():
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, i18n._STRINGS[lang])
                    self.assertEqual(i18n._fields(source), i18n._fields(i18n._STRINGS[lang][key]))
            self.assertNotIn('{', i18n.t('row.week', lang))
        self.assertEqual(i18n.t('row.week'), '✨ New This Week')

    def test_card_flags_work_without_english_text(self):
        s = self.s
        card = dict(id='tmdb:99001', type='movie', name='Invented Window', description='Für Ana: Ruhiger Humor. · Erfunden.',
                    _row={'overview': 'Erfunden.', 'for': ['ana'], 'personal': True})
        with patch.object(s, 'is_hero', return_value=False), patch.object(s, 'is_anime', return_value=False), \
                patch.object(s, 'get_model', return_value=None):
            self.assertTrue(s.member_pick(card, 'ana'))
            self.assertFalse(s.member_pick(card, 'ben'))
            self.assertEqual(s.balance_row([card], foryou=True), [card])
        card['_row'].pop('for')
        card['_row'].pop('personal')
        with patch.object(s, 'is_hero', return_value=False), patch.object(s, 'is_anime', return_value=False), \
                patch.object(s, 'member_pick', side_effect=lambda x, p: p == 'ana'):
            named = s.balance_row([card], foryou=True)[0]
            self.assertEqual(named['_row']['for'], ['ana'])
            self.assertTrue(named['description'].startswith('Tipps für Ana · '))
            self.assertEqual(s.balance_row([named], foryou=True), [named])

    def test_ai_archetype_language_and_legacy_cache(self):
        s = self.s
        brief = {'scope': 'member', 'experiences': ['cozy_case'], 'themes': [], 'groups': ['cozy']}
        body = s.archetype_request(brief, 'fake-model', 'none')
        self.assertIn('de-DE', str(body['messages']))
        self.assertNotIn('English words', str(body['messages']))
        self.data.joinpath('archetypes.json').write_text(json.dumps({'v': 1, 'items': {
            'ana': {'name': 'Quiet Viewer', 'line': 'Quiet invented stories.', 'source': 'llm', 'at': 1}}}))
        with patch.object(s, 'get_model', return_value=None):
            self.assertEqual(s.archetype_view('ana')['source'], 'rule')

    def test_personal_pick_targets_are_structured(self):
        s = self.s
        pick = s.parse_picks('Invented Window|2026|m|Ruhiger Humor|ana')
        with patch.object(s, 'resolve_pick', return_value=({'id': 99001, 'title': 'Invented Window', 'overview': 'Erfunden.'}, {})), \
                patch.object(s, 'genres', return_value={}):
            card = s.picks_to_rows(pick, 'en-US', set())['movie'][0]
        self.assertEqual(card['_row']['for'], ['ana'])
        self.assertTrue(card['_row']['personal'])
        self.assertIn('Tipps für Ana', card['description'])
        with patch.object(s, 'resolve_pick', return_value=({'id': 99002, 'title': 'Invented Signal'}, {})), patch.object(s, 'genres', return_value={}):
            shared = s.picks_to_rows(s.parse_picks('Invented Signal|2026|m|Ruhiger Humor|together'), 'en-US', set())['movie'][0]
        self.assertIn('Tipps für Ana & Ben', shared['description'])
        self.assertEqual(s.ai_search('movie', 'movie', pick, 'en-US', {}, history=[]), [])

    def test_served_templates_and_cached_inspector_agree(self):
        from skald.row_names import describe
        s = self.s
        cases = [
            ('mood1', {'mood': 'cozy', 'title': 'Old English'}, 'Ein gemütlicher Krimiabend', 'row.mood.cozy'),
            ('lane1', {'lane': 'd:1', 'maker': True, 'name': 'Invented Maker', 'title': 'Old English'}, 'Mehr von Invented Maker', 'row.maker'),
            ('because', {'label_key': 'row.seed.loved', 'fields': {'title': 'Invented Seed'}, 'title': 'Old English'}, 'Weil du Invented Seed liebst', 'row.seed.loved')]
        for key, names, expected, label_key in cases:
            cid = s.rid(key)
            with self.subTest(key=key), patch.object(s, 'feed', return_value={'names': names}):
                self.assertEqual(s.row_name(cid, 'en-US', 'fallback'), expected)
                item = describe(cid, names, s.CONFIG, s.LANG)
                self.assertEqual((item['label'], item['key']), (expected, label_key))
        feeling = describe(s.rid('lane1'), {'lane': 'x:cozy_case'}, s.CONFIG, s.LANG)
        self.assertEqual(feeling['source'], 'fixed')
        with patch.dict(i18n._STRINGS['de'], {'row.foryou': 'Fehler {extra}'}, clear=True):
            item = describe(s.rid('foryou'), {}, s.CONFIG, s.LANG)
            self.assertEqual(item['label'], '✨ For You')
            self.assertEqual(item['missing'], ['row.foryou'])
        with patch.object(s, 'anchor', return_value=None), patch.object(s, 'frec', return_value={}):
            self.assertEqual(s.reasons({'names': {'x:cozy_case': s.EXPERIENCES['cozy_case'][0]}}, 'tmdb:99001', ['x:cozy_case']), ['Gemütliche Krimifälle'])
            self.assertEqual(s.nice_facet('x:cozy_case', s.EXPERIENCES['cozy_case'][0]), 'gemütliche Krimifälle')

    def test_lane_animation_policy_uses_card_flag(self):
        s = self.s
        xs = [dict(id='tmdb:99001', type='movie', name='Invented Window', genres=['Animation'],
                   _row={'anime_lane': True})]
        with patch.object(s, 'frec', return_value={}):
            self.assertTrue(s.lane_allowed(xs, 'Welten voller Wunder'))
            xs[0]['_row']['anime_lane'] = False
            self.assertFalse(s.lane_allowed(xs, 'Anime Favorites'))

    def test_lane_names_accept_sourcing_status(self):
        chosen = [(fid, metas, reserve, title, {'fresh': True}) for fid, metas, reserve, title in self.chosen()]
        with patch.object(self.s, 'decide', return_value={'source': 'llm', 'answer': {'names': ['Leise Momente', 'Neue Welten']}}):
            self.assertEqual(self.s.name_lanes(chosen, {'names': {'k:1': 'quiet life', 'k:2': 'imagined worlds'}}),
                             {'k:1': 'Leise Momente', 'k:2': 'Neue Welten'})

    def test_catchup_flags_new_seasons_without_reading_german(self):
        s = self.s
        det = {'id': 99001, 'name': 'Invented Station', 'seasons': [
            {'season_number': 1, 'episode_count': 2}, {'season_number': 2, 'episode_count': 2}],
            'last_episode_to_air': {'season_number': 2, 'episode_number': 2}}
        entry = {'id': 99001, 'item': {'last_episode': [1, 2]}, 'weight': 2}
        with patch.object(s, 'details_pair', return_value=(det, det)), patch.object(s, 'genres', return_value={}):
            card = s.show_catchup(entry, 'en-US', s.local_now().date())[2]
        self.assertTrue(card['_row']['season_out'])
        self.assertIn('Neue Staffel 2 ist da', card['description'])

    def test_upcoming_uses_structured_date_and_announcement(self):
        s = self.s
        card = dict(id='tmdb:99001', type='movie', name='Invented Window', description='Termin noch offen',
                    _row={'when': s.local_now().date().isoformat(), 'announced': True})
        with patch.object(s, 'frec', return_value={}):
            self.assertFalse(s.upcoming_ok(card))
            card['_row']['announced'] = False
            self.assertTrue(s.upcoming_ok(card))

    def chosen(self):
        return [('k:1', [dict(name='Invented Window')], [], 'Quiet Life'),
                ('k:2', [dict(name='Invented Signal')], [], 'Artificial Intelligence (a.i.)')]

    def test_lane_pass_batches_names_and_preserves_old_fallback_across_languages(self):
        from test_lanes_r3 import LanesR3, cards
        self.facet = lambda xs, fid: LanesR3.facet(self, xs, fid)
        s = self.s
        self.enterContext(patch.dict(s._profiles, {'household': {'seen': set(), 'negative': set(), 'entries': []}}, clear=True))
        self.enterContext(patch.dict(s._learn, {'lanes': {}, 'rows': {}, 'served': {}}, clear=True))
        for name, value in {'get_model': {}, 'tired_keys': {}, 'speaks': True, 'because_seed': None,
                            'maker_seeds': [], 'row_pool': [], 'row_list': []}.items():
            self.enterContext(patch.object(s, name, return_value=value))
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"names":["Leise Momente","Ferne Welten"]}'}}]})
            built = LanesR3.build(self, {'k:1': cards(20), 'k:2': cards(20, 100)})
            self.assertEqual(replay.calls, 1)
            self.assertEqual({n['title'] for n in built['_names'].values()}, {'Leise Momente', 'Ferne Welten'})
            s._learn['lanes']['k:1']['last'] = '2000-01-01'
            again = LanesR3.build(self, {'k:1': cards(20), 'k:2': cards(20, 100)})
            self.assertEqual(built['_names'], again['_names'])
            self.assertEqual(replay.calls, 1)
        with patch.object(s, 'LANG', 'sr-Latn-RS'):
            fallback = LanesR3.build(self, {'k:1': cards(20), 'k:2': cards(20, 100)})
            self.assertEqual({n['title'] for n in fallback['_names'].values()}, {'Title k:1', 'Title k:2'})
        self.assertEqual(s._learn['lanes']['k:1']['titles']['de-de'], 'Leise Momente')

    def test_lane_names_one_call_and_restart(self):
        s = self.s
        model = {'names': {'k:1': 'quiet life', 'k:2': 'artificial intelligence (a.i.)'}}
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"names":["Leise Momente","Künstliche Welten"]}'}}]})
            got = s.name_lanes(self.chosen(), model)
            self.assertEqual(got, {'k:1': 'Leise Momente', 'k:2': 'Künstliche Welten'})
            self.assertEqual(replay.calls, 1)
            self.assertEqual(s.name_lanes(self.chosen(), model), got)
            s.save_learn()
            self.assertEqual(replay.calls, 1)
        other = load_server(self.data)
        self.addCleanup(close_server, other)
        with patch.object(other, 'get_model', return_value=None), patch.object(other, 'load_emb'), patch.object(other, 'load_facets'):
            other.load_state()
        with patch.object(other, 'decide', side_effect=AssertionError('already named')):
            self.assertEqual(other.name_lanes(self.chosen(), model), got)
        self.assertEqual(s._learn['lanes']['k:1']['titles']['de-de'], 'Leise Momente')

    def test_serbian_script_language_can_be_configured(self):
        for lang, label in [('sr-Latn-RS', '✨ Za tebe'), ('sr-Cyrl-RS', '✨ За тебе')]:
            cfg = members.fresh_config(['ana', 'ben'])
            cfg['region']['lang'] = lang
            members.validate_config(cfg)
            self.assertEqual(i18n.t('row.foryou', lang), label)
            self.assertTrue(self.s.LANG_RE.fullmatch(lang))

    def test_german_prose_is_not_read_as_english(self):
        s = self.s
        self.assertEqual(s.clean_note('Damit fängt die Reise an'), 'Damit fängt die Reise an.')
        xs = [dict(id=f'tmdb:{99001+i}', type='movie', name='Invented Window',
                   description='Because ist ein erfundener Filmtitel. · Erfunden.',
                   _row={'overview': 'Erfunden.'}) for i in range(3)]
        for card in xs[:2]:
            card['description'] = 'Erfunden.'
        with patch.object(s, 'frec', return_value={}):
            self.assertIn('Because ist ein erfundener Filmtitel.', s.human_cards(xs)[2]['description'])

    def test_lane_name_fallback_and_budget(self):
        s = self.s
        model = {'names': {'k:1': 'quiet life', 'k:2': 'artificial intelligence (a.i.)'}}
        self.assertEqual(s.name_lanes(self.chosen(), model), {})
        with ReplayLLM(s) as replay:
            reset(s)
            s._usage['calls']['feed:other'] = s.AI_CAPS['feed']
            self.assertEqual(s.name_lanes(self.chosen(), model), {})
            self.assertEqual(replay.calls, 0)
            reset(s)
            replay.arm({'choices': [{'message': {'content': '{"names":["too many words for a row name","Fine"]}'}}]})
            self.assertEqual(s.name_lanes(self.chosen(), model), {})
            self.assertEqual(replay.calls, 1)
            replay.arm({'choices': [{'message': {'content': '{"names":["„Quoted Name“","Fine"]}'}}]})
            self.assertEqual(s.name_lanes(self.chosen(), model), {'k:1': '„Quoted Name“', 'k:2': 'Fine'})
            self.assertEqual(replay.calls, 2)
        self.assertEqual(s.lane_title('k:1', 'quiet life', []), 'Quiet Life')

    def test_cli_cached_json_no_engine_or_writes(self):
        self.data.joinpath('feeds.json').write_text(json.dumps({self.s.rid('lane1')+'|de-DE': {'names': {'lane': 'k:1', 'title': 'Leise Momente'}}}))
        self.data.joinpath('learn.json').write_text(json.dumps({'lanes': {'k:1': {'titles': {'de-de': 'Leise Momente'}}}}))
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        out = io.StringIO()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}, clear=True), \
                patch('skald.__main__.Engine', side_effect=AssertionError('no Engine')), \
                patch('skald.migrate.ensure', side_effect=AssertionError('read only')):
            self.assertEqual(main(['rows', 'names', '--lang', 'de-DE', '--json'], out), 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result['language'], 'de-DE')
        rows = {r['id']: r for r in result['rows']}
        self.assertEqual(rows[self.s.rid('foryou')], dict(id=self.s.rid('foryou'), key='row.foryou', label='✨ Für dich', source='fixed', missing=[]))
        self.assertEqual(rows[self.s.rid('lane1')]['source'], 'ai')
        self.assertEqual(rows[self.s.rid('lane1')]['label'], 'Leise Momente')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.data.iterdir()})

    def test_commander_read_and_existing_rename_confirmation(self):
        s = self.s
        answer = dict(op='row_names', kind=None, values=[], person=None)
        with ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
            plan = commands.preview(s, 'Wie heißen meine Reihen?', 'ana')
        self.assertTrue(plan['read_only'])
        self.assertIn('✨ Für dich', plan['description'])
        self.assertIn('row_names', commands.command_prompt(s))
        plan = commands.preview(s, 'rename', 'ana', answer=dict(op='row_rename', kind=None, values=['foryou', 'Anas Auswahl'], person=None))
        self.assertNotIn('row_labels', s.CONFIG['household'])
        commands.apply(s, plan)
        self.assertEqual(s.CONFIG['household']['row_labels']['foryou'], 'Anas Auswahl')

    def test_names_remain_format5_and_old_build_loads_without_migration(self):
        self.data.joinpath('.skald-format').write_text('{"format":5,"release":"previous"}')
        old = {'lanes': {'k:1': {'title': 'Old Name', 'since': '2026-10-01'}}}
        self.data.joinpath('learn.json').write_text(json.dumps(old))
        self.data.joinpath('archetypes.json').write_text(json.dumps({'v': 1, 'items': {
            'ana': {'name': 'Quiet Viewer', 'line': 'Quiet invented stories.', 'source': 'llm', 'at': 1}}}))
        before = {name: self.data.joinpath(name).read_bytes() for name in ('learn.json', 'archetypes.json')}
        migrate.ensure(self.data)
        self.assertEqual(migrate.FORMAT, 5)
        self.assertEqual(set(migrate.MIGRATIONS), {2, 3, 4, 5})
        self.assertEqual(before, {name: self.data.joinpath(name).read_bytes() for name in before})
        s = self.s
        with patch.dict(s._learn, {**old, 'served': {}, 'rows': {}}, clear=True), ReplayLLM(s) as replay:
            replay.arm({'choices': [{'message': {'content': '{"names":["Leise Momente","Ferne Welten"]}'}}]})
            s.name_lanes(self.chosen(), {'names': {'k:1': 'quiet life', 'k:2': 'imagined worlds'}})
            s.save_learn()
        s._archetype_save({'ana': {'name': 'Leise Welten', 'line': 'Mag leise erfundene Welten.',
                                 'source': 'llm', 'at': 1, 'language': 'de-DE'}})
        legacy = {'__name__': 'skald.migrate', '__package__': 'skald'}
        code = subprocess.run(['git', 'show', 'HEAD:skald/migrate.py'], check=True, capture_output=True, text=True).stdout
        exec(compile(code, 'format5-migrate', 'exec'), legacy)
        self.assertEqual(legacy['FORMAT'], 5)
        self.assertEqual(legacy['check'](self.data)['format'], 5)
        code = subprocess.run(['git', 'show', 'HEAD:server.py'], check=True, capture_output=True, text=True).stdout
        legacy = {'__name__': 'format5_server', '__file__': str(Path(__file__).resolve().parents[1] / 'server.py')}
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'TMDB_API': 'offline', 'OPENROUTER_API_KEY': '', 'API_PORT': ''}), \
                patch.object(migrate, 'ensure', side_effect=AssertionError('no migration step')):
            exec(compile(code, legacy['__file__'], 'exec'), legacy)
            self.addCleanup(close_server, SimpleNamespace(**legacy))
            with patch.dict(legacy, get_model=lambda *a: None, load_emb=lambda: None, load_facets=lambda: None):
                legacy['load_state']()
        self.assertEqual(legacy['_learn']['lanes']['k:1']['title'], 'Old Name')
        self.assertEqual(legacy['_learn']['lanes']['k:1']['titles']['de-de'], 'Leise Momente')

    def test_malformed_cached_names_fall_back_in_serving_and_cli(self):
        from skald.row_names import describe, view
        s = self.s
        for bad in (None, 7, [], 'wrong'):
            for lane in ({'titles': bad, 'title': 'Cached Name'}, {'titles': {'de-de': bad}, 'title': 'Cached Name'}, bad):
                with self.subTest(bad=bad, lane=lane), patch.dict(s._learn, {'lanes': {'k:1': lane}}):
                    names = {'lane': 'k:1', 'title': 'Cached Name'}
                    expected = bad if lane == {'titles': {'de-de': bad}, 'title': 'Cached Name'} and isinstance(bad, str) else 'Cached Name'
                    with patch.object(s, 'feed', return_value={'names': names}):
                        self.assertEqual(s.row_name(s.rid('lane1'), 'de-DE'), expected)
                    self.data.joinpath('learn.json').write_text(json.dumps({'lanes': {'k:1': lane}}))
                    self.data.joinpath('feeds.json').write_text(json.dumps({s.rid('lane1')+'|de-DE': {'names': names}}))
                    row = next(r for r in view(self.data)['rows'] if r['id'] == s.rid('lane1'))
                    self.assertEqual(row['label'], expected)
            for names in ({'label_key': 'row.seed.loved', 'fields': bad, 'title': 'Cached Name'},
                          {'lane': 'd:1', 'name': bad, 'title': 'Cached Name'},
                          {'lane': 'k:1', 'title': bad}, bad):
                with self.subTest(names=names):
                    row = describe(s.rid('lane1'), names, s.CONFIG, s.LANG)
                    self.assertIsInstance(row['label'], str)
                    self.assertNotIn('{', row['label'])
            for cache in ('feeds.json', 'shown.json', 'learn.json'):
                self.data.joinpath(cache).write_text(json.dumps(bad))
            self.assertTrue(view(self.data)['rows'])
        row = describe(s.rid('lane1'), {'label_key': 'row.seed.loved', 'fields': {'title': 7}, 'title': 'Cached Name'}, s.CONFIG, s.LANG)
        self.assertEqual(row['label'], 'Cached Name')
        row = describe(s.rid('nextseason'), {'label_key': 'row.seed.loved', 'fields': None}, s.CONFIG, s.LANG)
        self.assertNotIn('{', row['label'])

    def test_member_pick_ignores_malformed_targets(self):
        s = self.s
        card = dict(id='tmdb:99001', type='movie', name='Invented Window')
        with patch.object(s, 'get_model', return_value=None), patch.object(s, 'frec', return_value={}):
            for target in (None, 'ana', {'ana': True}, 7, ['ana', None], ['ana', 'unknown']):
                with self.subTest(target=target):
                    self.assertFalse(s.member_pick({**card, '_row': {'for': target}}, 'ana'))
            self.assertTrue(s.member_pick({**card, '_row': {'for': ['ana']}}, 'ana'))

    def test_optional_cache_fields_use_defaults_for_wrong_types(self):
        s = self.s
        card = dict(id='tmdb:99001', type='movie', name='Invented Window', genres=['Animation'],
                    releaseInfo=str(s.local_now().year), description='Invented overview.')
        with patch.object(s, 'frec', return_value={}), patch.object(s, 'is_hero', return_value=False), \
                patch.object(s, 'is_anime', return_value=False), patch.object(s, 'member_pick', side_effect=lambda x, p: p == 'ana'):
            for bad in (None, 7, [], 'wrong', {'ana': True}):
                with self.subTest(bad=bad):
                    tagged = {**card, '_row': {'for': bad, 'personal': bad, 'when': bad, 'announced': bad, 'anime_lane': bad}}
                    self.assertEqual(s.soon_date(tagged).year, s.local_now().year)
                    self.assertFalse(s.lane_allowed([tagged], 'Invented Lane'))
                    self.assertEqual(s.balance_row([tagged], foryou=True)[0]['_row']['for'], ['ana'])
                    cfg = {'v': 1, 'items': {'ana': {'name': 'Invented Taste', 'line': 'Invented taste mix.', 'source': 'llm', 'at': 1, 'language': bad}}}
                    self.data.joinpath('archetypes.json').write_text(json.dumps(cfg))
                    with patch.object(s, 'LANG', 'en-US'):
                        if isinstance(bad, str):
                            self.assertEqual(s.archetype_items(), {})
                        else:
                            self.assertEqual(s.archetype_items()['ana']['language'], 'en-US')
        with patch.object(s, 'feed', return_value={'names': {'title': 7}}):
            self.assertEqual(s.row_name(s.rid('lane1'), 'de-DE'), i18n.t('row.picked', s.LANG))

    def test_title_reason_rung_uses_anchor_in_every_language_and_cache(self):
        s = self.s
        fid, key = 'x:cozy_case', 'tmdb:99001'
        model = {'ent': {}, 'names': {fid: s.EXPERIENCES['cozy_case'][0]}}
        with patch.object(s, 'get_model', return_value=model), patch.object(s, 'frec', return_value={'title': 'Invented Window'}), \
                patch.object(s, 'title_allowed', return_value=True), patch.object(s, 'person_score', return_value=(1, [fid])), \
                patch.object(s, 'confidence', return_value=('Good fit', 75)), patch.object(s, 'reason_ok', return_value=True), \
                patch.object(s, 'maturity', return_value={'explicit': 2}):
            for lang in ('en-US', 'de-DE', 'sr-Latn-RS', 'sr-Cyrl-RS'):
                with self.subTest(lang=lang), patch.object(s, 'LANG', lang), \
                        patch.object(s, 'anchor', return_value=('movie:99002', 'Invented Seed', ['ana'])):
                    row = s.title_view(key, 'movie', 'ana')
                    self.assertEqual(row['rung'], 'seed')
                    name = i18n.t('experience.cozy_case', lang)
                    self.assertEqual(row['reasons'][0], i18n.t('why.anchor', lang, name=name[:1].upper()+name[1:], title='Invented Seed', person=''))
                    self.assertEqual(s.title_view(key, 'movie', 'ana'), row)
            model = {**model, 'ent': {}}
            with patch.object(s, 'get_model', return_value=model), patch.object(s, 'anchor', return_value=None):
                self.assertEqual(s.title_view(key, 'movie', 'ana')['rung'], 'facet')

    def test_owner_plain_names_and_short_taste_templates(self):
        expected = {
            'en': ('Continue Watching', 'Watch It Again', 'Coming Soon', 'Picks for {person}'),
            'de': ('Weiterschauen', 'Nochmal schauen', 'Demnächst', 'Tipps für {person}'),
            'sr-latn': ('Nastavi gledanje', 'Gledaj ponovo', 'Uskoro', 'Predlozi za {person}'),
            'sr-cyrl': ('Настави гледање', 'Гледај поново', 'Ускоро', 'Предлози за {person}')}
        for lang, values in expected.items():
            strings = i18n._STRINGS[lang]
            self.assertEqual(tuple(strings[k] for k in ('row.episodes', 'row.rewatch', 'row.upcoming', 'row.member')), values)
            self.assertEqual(strings['row.hbo'], 'HBO')
            self.assertEqual(strings['row.paramount'], 'Paramount+')
            self.assertEqual(strings['row.lane.brand'], '{name}')
            for key in ('row.because2', 'row.because3', 'row.top10', 'row.airing', 'row.nextseason'):
                self.assertIn(key, strings)
            for key, value in strings.items():
                if key.startswith('row.'):
                    with self.subTest(lang=lang, key=key):
                        self.assertNotIn('&', value)
                        if not key.startswith('row.top10'):
                            self.assertLessEqual(len(value.split()), 4)

    def test_ai_prompt_and_cached_word_limit(self):
        from skald.row_names import describe
        s = self.s
        batch = [{'language': 'de-DE', 'lanes': [{'keyword': 'quiet life', 'examples': ['Invented Window']}]}]
        prompt = s.lane_names_request(batch)['messages'][0]['content']
        self.assertIn('household language', prompt)
        self.assertIn('4 words', prompt)
        self.assertIn('&', prompt)
        names = {'lane': 'k:1', 'title': 'Cached Name'}
        lanes = {'k:1': {'title': 'Cached Name', 'titles': {'de-de': 'a cached title with five words'}}}
        self.assertEqual(describe(s.rid('lane1'), names, s.CONFIG, s.LANG, lanes)['label'], 'Cached Name')
        with patch.dict(s._learn, {'lanes': lanes}), patch.object(s, 'decide', return_value={'source': 'fallback'}):
            self.assertEqual(s.name_lanes(self.chosen(), {'names': {'k:1': 'quiet life', 'k:2': 'invented worlds'}}), {})

    def test_episode_and_season_facts_survive_on_served_card_text(self):
        s = self.s
        today = s.local_now().date()
        det = {'id': 99001, 'name': 'Invented Station', 'overview': 'Invented overview.', 'seasons': [
            {'season_number': 1, 'episode_count': 2}, {'season_number': 2, 'episode_count': 2}],
            'last_episode_to_air': {'season_number': 2, 'episode_number': 2, 'air_date': today.isoformat()}}
        for lang, episode, season in [('en-US', 'New episode', 'New season'), ('de-DE', 'Neue Folge', 'Neue Staffel'),
                                      ('sr-Latn-RS', 'Nova epizoda', 'Nova sezona'), ('sr-Cyrl-RS', 'Нова епизода', 'Нова сезона')]:
            with self.subTest(lang=lang), patch.object(s, 'LANG', lang), patch.object(s, 'tmdb', return_value=det), \
                    patch.object(s, 'genres', return_value={}), patch.object(s, 'details_pair', return_value=(det, det)), \
                    patch.object(s, 'reason_ok', return_value=True):
                entry = dict(id=99001, stype='series', signal='liked', age=1, weight=2, item={'last_episode': [2, 1]})
                with patch.dict(s._profiles, {'household': {'entries': [entry]}}):
                    card = s.human_cards(s.episodes_feed(lang)['series'])[0]
                self.assertIn(episode, card['description'])
                entry['item']['last_episode'] = [1, 2]
                card = s.human_cards([s.show_catchup(entry, lang, today)[2]])[0]
                self.assertIn(season, card['description'])
                self.assertNotIn('_row', card)
