"""Top-rated discovery uses synthetic charts and offline viewer scores."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from decide_kit import close_server, load_server, ReplayLLM
from skald import commands, members, migrate, row_names, rowplan, setup
from skald.__main__ import main


class TopRated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        cfg = members.fresh_config(['ana', 'ben'])
        cfg['region']['lang'] = 'en-US'
        setup.publish(self.data, cfg)
        self.s = load_server(self.data)
        self.real_score = self.s.score_key
        self.addCleanup(close_server, self.s)
        self.enterContext(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(self.s, 'femb', side_effect=AssertionError('no embedding calls')))
        self.enterContext(patch.object(self.s, 'connectors_tick'))
        self.enterContext(patch.object(self.s, 'aio_rows_changed'))
        self.enterContext(patch.object(self.s, 'pin_shown'))
        self.enterContext(patch.object(self.s, 'who_watching', return_value=('both', None)))
        self.enterContext(patch.object(self.s, 'remote_masks', return_value=(set(), set())))
        self.enterContext(patch.object(self.s, 'veto_keys', return_value=set()))
        self.enterContext(patch.object(self.s, 'frec', return_value={}))
        self.enterContext(patch.object(self.s, 'row_pool', return_value=[]))
        self.enterContext(patch.object(self.s, 'get_model', return_value={'synthetic': True}))
        self.enterContext(patch.object(self.s, 'score_key', side_effect=self.score))
        self.calls = []
        self.lock = threading.Lock()

    def score(self, model, key, person=None, kind=None, *, rec=None):
        target = 150 if person != 'ben' else 140
        number = int(key.split(':')[1]) % 1000
        return (1 if number == target else number / 1000), []

    def tmdb(self, path, **params):
        if path.startswith('genre/'):
            return {'genres': []}
        self.assertIn(path, ('movie/top_rated', 'tv/top_rated'))
        with self.lock:
            self.calls.append((path, params['page'], params['language']))
        start = (params['page'] - 1) * 20 + 1
        offset = 0 if path.startswith('movie/') else 1000
        return {'results': [dict(id=offset+i, **{('title' if offset == 0 else 'name'): f'Invented {"movie" if offset == 0 else "series"} {i} Card'},
                                 genre_ids=[], vote_count=500, vote_average=9-i/1000,
                                 poster_path='/poster', backdrop_path='/background',
                                 release_date='2000-01-01', first_air_date='2000-01-01')
                            for i in range(start, start+20)]}

    def build(self):
        s = self.s
        with patch.object(s, 'tmdb', side_effect=self.tmdb), patch.object(s, '_langs', {s.LANG}), \
                patch.object(s, 'openrouter', side_effect=AssertionError('no AI in rows')):
            start = time.perf_counter()
            s.build_feeds(s.local_now(), {}, {}, only={'toprated'})
        return time.perf_counter()-start

    def test_default_build_serve_and_daily_page_count(self):
        s = self.s
        self.assertTrue(s.row_enabled(s.rid('toprated')))
        elapsed = self.build()
        self.assertEqual(len(self.calls), 15)
        self.assertEqual({(path, page) for path, page, _ in self.calls},
                         {('movie/top_rated', p) for p in range(1, 11)} | {('tv/top_rated', p) for p in range(1, 6)})
        self.assertEqual(len(s.row_metas(s.rid('toprated'), 'movie', s.LANG, who='ana')), 20)
        self.assertEqual(len(s.row_metas(s.rid('toprated'), 'series', s.LANG, who='ana')), 20)
        self.build()
        self.assertEqual(len(self.calls), 15)
        self.assertEqual(migrate.FORMAT, 5)
        print(f'W112 fake build: {elapsed*1000:.1f} ms; 15 chart pages')

    def test_explicit_selection_does_not_build_or_serve(self):
        s = self.s
        s.CONFIG['household']['rows'] = ['episodes']
        setup.publish(self.data, s.CONFIG, replace=True)
        self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual(s.row_metas(s.rid('toprated'), 'movie', s.LANG, who='ana'), [])

    def test_legacy_daily_source_rebuilds_once_for_the_wider_pool(self):
        s = self.s
        day = (s.local_now()-s.datetime.timedelta(hours=5)).strftime('%Y-%m-%d')
        s._feeds[s.rid('toprated'), s.LANG] = {'stamp': f'v{s.STAMP_V}/{day}', 'metas': {'movie': []}}
        self.build()
        self.assertEqual(len(self.calls), 15)
        self.build()
        self.assertEqual(len(self.calls), 15)

    def test_deep_movie_and_viewer_orders_with_deterministic_ties(self):
        s = self.s
        self.build()
        cid = s.rid('toprated')
        ana = s.row_metas(cid, 'movie', s.LANG, who='ana')
        ben = s.row_metas(cid, 'movie', s.LANG, who='ben')
        self.assertEqual(ana[0]['id'], 'tmdb:150')
        self.assertEqual(ben[0]['id'], 'tmdb:140')
        self.assertNotEqual(ana, ben)
        raw = s.row_list(cid, 'movie', s.LANG)
        ordered = s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ana')
        with patch.object(s, 'score_key', return_value=(0, [])):
            tied = s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ana')
        self.assertLess(ordered.index(next(x for x in ordered if x['id'] == 'tmdb:150')), 10)
        self.assertEqual(tied, raw[:20])

    def test_household_and_together_use_the_joint_taste_score(self):
        s = self.s
        self.build()
        raw = s.row_list(s.rid('toprated'), 'movie', s.LANG)
        for who in (None, 'none', 'both', 'together'):
            with self.subTest(who=who), patch.object(s, 'score_key', return_value=(1, [])) as score:
                s.toprated_scores(raw, who)
                self.assertTrue(all(call.args[2] == 'together' for call in score.call_args_list))

    def test_seen_negative_blocks_quality_overlap_and_variety(self):
        s = self.s
        self.build()
        cid = s.rid('toprated')
        p = {'seen': {'tmdb:150'}, 'negative': {'tmdb:200'}}
        masks = ({'tmdb:199'}, set(), {'tmdb:198'})
        above = s.RowAbove({'tmdb:197'})
        with patch.object(s, 'is_hero', side_effect=lambda x: int(x['id'][5:]) > 190):
            xs = s.served_row(cid, 'movie', s.LANG, p, above, masks, 'ana')
        self.assertFalse({'tmdb:150', 'tmdb:200', 'tmdb:199', 'tmdb:198', 'tmdb:197'} & {x['id'] for x in xs})
        self.assertEqual(sum(int(x['id'][5:]) > 190 for x in xs), 2)
        s._feeds[cid, s.LANG]['metas']['movie'][149]['_row']['rating'] = 2
        self.assertNotIn('tmdb:150', {x['id'] for x in s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ana')})

    def test_member_seen_scope_uses_own_history_and_kind(self):
        s = self.s
        self.build()
        entry = dict(key='tmdb:150', id=150, kind='movie', stype='movie', title='Invented movie 150',
                     year='2000', signal='watched', weight=1, age=1, item={'played': True}, by=['ana'])
        p = {'entries': [entry], 'seen': {'tmdb:150'}, 'negative': set()}
        s._profiles['household'] = p
        cid = s.rid('toprated')
        for who, hidden in (('ana', True), ('ben', False), ('both', True)):
            with self.subTest(who=who), patch.object(s, 'score_key', side_effect=lambda m, key, *a, **kw: (1 if key == 'tmdb:150' else 0, [])):
                xs = s.served_row(cid, 'movie', s.LANG, p, set(), (set(), set()), who)
                self.assertEqual('tmdb:150' not in {x['id'] for x in xs}, hidden)
        p = copy.deepcopy(p)
        p['entries'][0]['kind'] = 'tv'
        xs = s.served_row(cid, 'movie', s.LANG, p, set(), (set(), set()), 'ana')
        self.assertIn('tmdb:150', {x['id'] for x in xs})

    def test_series_floor_talk_news_and_adult_exclusions(self):
        s = self.s
        original = self.tmdb
        def transport(path, **params):
            data = original(path, **params)
            if path == 'tv/top_rated' and params['page'] == 1:
                for i, genre in enumerate((10763, 10764, 10767, 99)):
                    data['results'][i]['genre_ids'] = [genre]
                data['results'][4]['vote_count'] = 99
                data['results'][5]['vote_count'] = 100
                data['results'][6]['adult'] = True
            return data
        with patch.object(s, 'tmdb', side_effect=transport):
            result = s.toprated_feed(s.LANG)
        self.assertIn('series', result)
        keys = {x['id'] for x in result['series']}
        self.assertFalse({f'tmdb:{i}' for i in (1001, 1002, 1003, 1004, 1005, 1007)} & keys)
        self.assertIn('tmdb:1006', keys)

    def test_parallel_pages_fetch_once_in_a_localized_build(self):
        s = self.s
        barrier = threading.Barrier(2)
        first = []
        def transport(path, **params):
            if path.endswith('/top_rated'):
                with self.lock:
                    first.append(path)
                    wait = len(first) <= 2
                if wait:
                    barrier.wait(2)
            return self.tmdb(path, **params)
        with patch.object(s, 'tmdb', side_effect=transport):
            result = s.toprated_feed('de-DE')
        self.assertEqual(len(self.calls), 15)
        self.assertTrue(all(lang == 'de-DE' for _, _, lang in self.calls))
        self.assertEqual([len(result[k]) for k in ('movie', 'series')], [200, 100])

    def test_two_type_names_in_all_locales_and_manifest(self):
        s = self.s
        self.build()
        for lang, names in [('en-US', ('Best Movies', 'Best Series')),
                            ('de-DE', ('Die besten Filme', 'Die besten Serien')),
                            ('sr-latn', ('Najbolji filmovi', 'Najbolje serije')),
                            ('sr-cyrl', ('Најбољи филмови', 'Најбоље серије'))]:
            described = row_names.describe(s.rid('toprated'), {}, s.CONFIG, lang)
            self.assertEqual(described['label'], dict(zip(('movie', 'series'), names)))
        cats = [c for c in s.manifest(s.LANG, 'ana')['catalogs'] if c['id'] == s.rid('toprated')]
        self.assertEqual([(c['type'], c['name']) for c in cats],
                         [('movie', 'Best Movies'), ('series', 'Best Series')])

    def test_member_hide_applies_before_the_first_build(self):
        s = self.s
        s.CONFIG['taste']['people']['ana']['prefs'] = {'hide': ['series']}
        setup.publish(self.data, s.CONFIG, replace=True)
        cats = [c for c in s.manifest(s.LANG, 'ana')['catalogs'] if c['id'] == s.rid('toprated')]
        self.assertEqual([c['type'] for c in cats], ['movie'])

    def test_ranked_plan_why_and_explain_and_commander_read(self):
        s = self.s
        self.build()
        result = rowplan.why(s, 'toprated', 'ana')
        plan = next(r for r in rowplan.plan(s, 'ana')['rows'] if r['id'] == s.rid('toprated'))
        self.assertEqual(plan['cards'], result['cards'])
        movie = next(x for x in plan['cards'] if x['key'] == 'tmdb:150')
        self.assertEqual((movie['type'], movie['position'], movie['rating'], movie['taste_match']), ('movie', 1, 8.85, 100))
        self.assertAlmostEqual(movie['blend'], .65 + .35*.885)
        info = s.explain('tmdb:150', 'movie', person='ana')
        self.assertEqual(info['toprated'], movie)
        preview = commands.preview(s, '', 'ana', answer=dict(op='home_plan', kind=None, values=['toprated'], person='ana'))
        self.assertTrue(preview['read_only'])
        self.assertIn('Invented movie 150', preview['description'])
        self.assertIn('8.85', preview['description'])
        self.assertIn('100', preview['description'])

    def test_ranked_read_only_lists_the_type_that_survives_the_minimum(self):
        s = self.s
        self.build()
        s._feeds[s.rid('toprated'), s.LANG]['metas']['series'] = s.row_list(s.rid('toprated'), 'series', s.LANG)[:5]
        result = rowplan.why(s, 'toprated', 'ana')
        self.assertEqual(len(result['cards']), 20)
        self.assertTrue(all(x['type'] == 'movie' and x['position'] <= 20 for x in result['cards']))

    def test_cli_ranked_reads_and_controls_use_the_same_row(self):
        s = self.s
        self.build()
        raw = s.row_list(s.rid('toprated'), 'movie', s.LANG)
        for x in raw:
            x['_row']['genre_ids'] = [35 if x['id'] == 'tmdb:150' else 18]
        s.save_feeds()
        s.CONFIG['household']['rows'] = ['toprated']
        setup.publish(self.data, s.CONFIG, replace=True)
        with patch.dict(os.environ, {'DATA_DIR': str(self.data), 'SKALD_CONFIG': ''}):
            for args in (['rows', 'plan', '--person', 'ana', '--json'],
                         ['rows', 'why', 'toprated', '--person', 'ana', '--json'],
                         ['explain', 'tmdb:1', '--person', 'ana', '--json']):
                out = io.StringIO()
                self.assertEqual(main(['--snapshot', str(self.data), *args], out=out), 0, out.getvalue())
                data = json.loads(out.getvalue())
                if args[0] == 'explain':
                    pick = data['toprated']
                else:
                    row = data if args[1] == 'why' else next(r for r in data['rows'] if r['id'] == s.rid('toprated'))
                    pick = next(x for x in row['cards'] if x.get('position'))
                self.assertTrue({'rating', 'taste_match', 'position', 'type', 'blend'} <= pick.keys())
            self.assertEqual(main(['setup', 'rows', 'episodes'], out=io.StringIO()), 0)
            s.refresh_preferences()
            self.assertFalse(s.row_enabled(s.rid('toprated')))
            self.assertEqual(main(['setup', 'rows', 'episodes,toprated'], out=io.StringIO()), 0)
            s.refresh_preferences()
            self.assertTrue(s.row_enabled(s.rid('toprated')))

    def test_guest_ranked_read_has_no_household_seen_or_taste_effect(self):
        s = self.s
        self.build()
        s._profiles['household'] = {'seen': {'tmdb:1'}, 'negative': {'tmdb:2'}}
        with patch.object(s, 'title_view', return_value={}):
            result = rowplan.why(s, 'toprated', 'guest')
        pick = next(x for x in result['cards'] if x['key'] == 'tmdb:1')
        self.assertEqual(pick['position'], 1)
        self.assertEqual(pick['rating'], 8.999)
        self.assertIsNone(pick['taste_match'])

    def test_preferences_apply_after_taste_and_guest_keeps_source_order(self):
        s = self.s
        self.build()
        cid = s.rid('toprated')
        raw = s.row_list(cid, 'movie', s.LANG)
        self.assertEqual(s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), s.GUEST), raw[:20])
        raw[0]['_row']['lang'] = 'es'
        s.CONFIG['taste']['people']['ana']['prefs'] = {'focus': {'languages': ['es']}}
        xs = s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ana')
        self.assertEqual(xs[0]['id'], 'tmdb:1')
        s.CONFIG['taste']['people']['ana']['prefs'] = {'hide': ['series']}
        self.assertEqual(s.served_row(cid, 'series', s.LANG, {}, set(), (set(), set()), 'ana'), [])

    def test_uncached_titles_use_chart_genres_without_enrichment(self):
        s = self.s
        self.build()
        cid = s.rid('toprated')
        raw = s.row_list(cid, 'movie', s.LANG)
        for x in raw:
            x['_row']['genre_ids'] = [35 if x['id'] == 'tmdb:150' else 18]
        model = dict(vec=None, pvec={}, vbase=0, aff={'g:Comedy': 2, 'g:Drama': -1},
                     paff={'ana': {'g:Comedy': 2, 'g:Drama': -1}, 'ben': {'g:Comedy': -1, 'g:Drama': 2}},
                     bscale=1, pbscale={}, names={}, nexp={'ana': 50, 'ben': 50})
        with patch.object(s, 'score_key', side_effect=self.real_score), patch.object(s, 'get_model', return_value=model), \
                patch.object(s, 'femb', return_value=None), patch.object(s, 'openrouter', side_effect=AssertionError('no enrichment')):
            ana = s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ana')
            ben = s.served_row(cid, 'movie', s.LANG, {}, set(), (set(), set()), 'ben')
        self.assertEqual(ana[0]['id'], 'tmdb:150')
        self.assertEqual(ben[0]['id'], 'tmdb:1')

    def test_row_controls_preserve_default_and_explicit_choices(self):
        s = self.s
        cfg = commands.config(s)
        commands.edit_rows(cfg, dict(op='row_hide', kind=None, values=['moods']), s)
        self.assertIn('toprated', cfg['household']['rows'])
        for text, op in [('hide top rated', 'row_hide'), ('show top rated series', 'row_show')]:
            answer = dict(op=op, kind=None, values=['toprated'], person=None)
            with ReplayLLM(s) as llm:
                llm.arm({'choices': [{'message': {'content': json.dumps(answer)}}]})
                preview = commands.preview(s, text, 'ana')
                commands.apply(s, preview)
            self.assertEqual(s.row_enabled(s.rid('toprated')), op == 'row_show')

    def test_home_counts_before_and_after(self):
        s = self.s
        self.build()
        for key, typ, offset in [('episodes', 'series', 3000), ('top10', 'movie', 4000)]:
            s._feeds[s.rid(key), s.LANG] = {'metas': {typ: [dict(id=f'tmdb:{offset+i}', type=typ,
                name=f'Invented home {offset+i}', poster='p', background='b') for i in range(8)]}}
        original = s.row_enabled
        counts = {}
        nonempty = {}
        with patch.object(s, 'row_eligible', return_value=True):
            for scope in (None, 'ana', 'ben', 'together'):
                with patch.object(s, 'row_enabled', side_effect=lambda cid: cid != s.rid('toprated') and original(cid)):
                    before_cats = [r for r in s.manifest(s.LANG, scope)['catalogs'] if r['id'] != s.rid('search')]
                    with s.addon_scope(scope):
                        before_full = sum(bool(s.row_metas(r['id'], r['type'], s.LANG)) for r in before_cats)
                s._home_cache.clear()
                after_cats = [r for r in s.manifest(s.LANG, scope)['catalogs'] if r['id'] != s.rid('search')]
                with s.addon_scope(scope):
                    after_full = sum(bool(s.row_metas(r['id'], r['type'], s.LANG)) for r in after_cats)
                counts[scope or 'household'] = [len(before_cats), len(after_cats)]
                nonempty[scope or 'household'] = [before_full, after_full]
                self.assertEqual(len(after_cats)-len(before_cats), 2)
                self.assertEqual(after_full-before_full, 2)
        print('W112 synthetic Home catalogs: '+json.dumps(counts, sort_keys=True))
        print('W112 synthetic nonempty Home rows: '+json.dumps(nonempty, sort_keys=True))


class TopRatedNames(unittest.TestCase):
    def test_row_names_resolve_without_a_model(self):
        from skald.commands import row_id
        for name in ('top rated series', 'Best Movies', 'best series row', 'Die besten Serien', 'die besten filme'):
            self.assertEqual(row_id(name), 'toprated', name)
