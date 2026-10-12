"""Family rows and adult Together taste, using synthetic inputs only."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from skald import setup
from skald.__main__ import main
import test_prefs
from test_rows_r2 import cards


class Family(unittest.TestCase):
    setUpClass = classmethod(test_prefs.PrefsRows.setUpClass.__func__)
    prefs = test_prefs.PrefsRows.prefs
    served = test_prefs.PrefsRows.served

    def prompt(self):
        s = self.s
        p = {'entries': [], 'searches': [], 'likes': 'Synthetic adult favourite', 'genres': {},
             'seen': set(), 'negative': set()}
        with patch.object(s, '_profiles', {'household': p}), patch.object(s, 'get_model', return_value=None), \
                patch.object(s, 'candidate_pool', return_value=[]), patch.object(s, 'hit_titles', return_value=[]), \
                patch.object(s, 'tired_keys', return_value={}), patch.object(s, 'llm_sections', return_value={}) as llm:
            s.personal_feed('en-US')
            return llm.call_args.args[:2]

    def test_no_child_household_unchanged(self):
        s = self.s
        with patch.object(s, 'pin_shown'), patch.object(s, 'feed', return_value={}):
            value = [s.manifest('en-US'), self.served(s.rid('leap'), cards(8), who='both'), self.prompt()]
        self.assertEqual(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest(), '46df855824d7e5dbe290f6b31737cc421700e11725f8f82bb43e852e1f90e617')
        self.assertEqual(s.adult_people(), s.PEOPLE)
        self.assertNotIn('family', [j[0] for j in s.job_specs(s.local_now(), {})])

    def test_family_row_only_with_child(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'pin_shown'), \
                patch.object(s, 'feed', return_value={'metas': {}}), patch.object(s, 'row_metas', return_value=cards(8)):
            for scope in (None, 'together', 'ana'):
                self.assertIn(s.rid('family'), [r['id'] for r in s.manifest('en-US', scope)['catalogs']])
            jobs = {j[0]: j for j in s.job_specs(s.local_now(), {'household': 'taste'})}
            self.assertIn('family', jobs)
            self.assertIn('taste', jobs['family'][2])
            with patch.object(s, 'feed', return_value={}), patch.object(s, 'row_metas', return_value=cards(s.ROW_MIN - 1)):
                self.assertNotIn(s.rid('family'), [r['id'] for r in s.manifest('en-US')['catalogs']])

    def test_family_row_youngest_age_fail_closed(self):
        s = self.s
        xs = cards(10)
        def rec(key, kind):
            return {'cr': {'US': 'G' if int(key[5:]) < 7 else 'PG-13'}} if key != 'tmdb:9' else {}
        with self.prefs(ana={'kids_age': 8}, ben={'kids_age': 13}), patch.object(s, 'frec', side_effect=rec):
            self.assertEqual(self.served(s.rid('family'), xs, who='both'), xs[:7])
            self.assertEqual(self.served(s.rid('family'), xs, who='ben'), xs[:7])
            self.assertEqual(self.served(s.rid('family'), xs[:5], who='both'), [])
        with self.prefs(ana={'kids_age': 8}, ben={'hide': ['animation']}), \
                patch.object(s, 'frec', return_value={'cr': {'US': 'G'}}):
            self.assertEqual(self.served(s.rid('family'), [{**x, 'genres': ['Animation']} for x in xs], who='ana'), [])
        with self.prefs(ana={'kids_age': 8}), ExitStack() as stack:
            for name, value in {'get_model': None, 'ensure_facets': None, 'quality_ok': True,
                                'frec': {'cr': {'US': 'G'}}, 'row_meta': None}.items():
                stack.enter_context(patch.object(s, name, return_value=value))
            stack.enter_context(patch.object(s, '_profiles', {'household': {'seen': {'tmdb:0'}, 'negative': {'tmdb:1'}}}))
            stack.enter_context(patch.object(s, 'row_meta', side_effect=lambda it, *a, **kw: xs[it['id']]))
            discover = stack.enter_context(patch.object(s, 'with_art', return_value=([{'id': i} for i in range(10)], {})))
            score = stack.enter_context(patch.object(s, 'score_key', side_effect=lambda m, key, person, kind: (int(key[5:]), [])))
            result = s.family_feed('en-US')[s.rid('family')]['tv']
            self.assertEqual(result[0]['id'], 'tmdb:9')
            self.assertTrue(all(c.args[2] == 'together' for c in score.call_args_list))
            self.assertEqual({x['id'] for x in result}, {x['id'] for x in xs[2:]})
            self.assertTrue(all('with_genres' in c.kwargs for c in discover.call_args_list))

    def test_family_row_not_on_adult_member_link(self):
        s = self.s
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'pin_shown'), \
                patch.object(s, 'feed', return_value={}), s.addon_scope('ben'):
            self.assertNotIn(s.rid('family'), [r['id'] for r in s.manifest('en-US', 'ben')['catalogs']])
            self.assertEqual(self.served(s.rid('family'), cards(8), who='ben'), [])

    def test_child_plays_do_not_feed_together(self):
        s = self.s
        adult = {'key': 'tmdb:1', 'title': 'Adult drama', 'kind': 'movie', 'by': ['ben'],
                 'signal': 'liked', 'weight': 2, 'genres': ['Drama'], 'who': 'remote', 'stype': 'movie',
                 'age': 20, 'item': {}, 'flags': []}
        child = {**adult, 'key': 'tmdb:2', 'title': 'Child cartoon', 'by': ['ana'], 'genres': ['Animation']}
        p = {'entries': [adult, child], 'genres': {'Drama': 2, 'Animation': 2}, 'searches': [],
             'likes': '', 'seen': {'tmdb:1', 'tmdb:2'}, 'negative': set()}
        with self.prefs(ana={'kids_age': 8}), patch.object(s, 'person_loves', return_value=[]):
            self.assertEqual(s.adult_people(), ('ben',))
            self.assertIsNone(s.because_seed({**p, 'entries': [child]}))
            mixed = {**child, 'by': ['ana', 'ben'], 'weight': 10,
                     'opinions': {'ana': {'weight': 10, 'signal': 'loved'}, 'ben': {'weight': -2, 'signal': 'disliked'}}}
            self.assertEqual(s.adult_profile({**p, 'entries': [mixed]})['entries'][0]['weight'], -2)
            with patch.object(s, '_profiles', {'household': {**p, 'entries': [child]}}), \
                    patch.object(s, 'get_model', return_value={'synthetic': True}), \
                    patch.object(s, 'frec', return_value={'x': {'warm': 3}}), patch.object(s, 'feeling_pool', return_value=[]) as pool:
                s.leap_feed('en-US')
                pool.assert_not_called()
            self.assertEqual([e['key'] for e in s.adult_profile(p)['entries']], ['tmdb:1'])
            self.assertEqual([e['key'] for e in s.person_entries(p, 'ana')], ['tmdb:2'])
            with patch.object(s, '_profiles', {'household': p}), patch.object(s, 'get_model', return_value=None), \
                    patch.object(s, 'candidate_pool', return_value=[]), patch.object(s, 'hit_titles', return_value=[]), \
                    patch.object(s, 'tired_keys', return_value={}), patch.object(s, 'llm_sections', return_value={}) as llm:
                s.personal_feed('en-US')
                self.assertNotIn('Child cartoon', llm.call_args.args[1])
                self.assertNotIn('Ana', llm.call_args.args[1])
                s.week_feed('en-US')
                self.assertNotIn('Child cartoon', llm.call_args.args[1])
                self.assertNotIn('FOR ANA', llm.call_args.args[0])
                s.docs_ai_feed('en-US')
                self.assertNotIn('Child cartoon', llm.call_args.args[1])
            with patch.object(s, 'taste_vector', return_value=None) as vector, \
                    patch.object(s, 'affinities', return_value=({}, {})) as affinities, \
                    patch.object(s, 'active_feedback', return_value={}):
                model = s.build_model(p)
                self.assertEqual([e['key'] for e in vector.call_args_list[0].args[0]['entries']], ['tmdb:1'])
                self.assertEqual([e['key'] for e in affinities.call_args_list[0].args[0]], ['tmdb:1'])
                self.assertEqual(model['nexp']['ana'], 1)
            model.update(aff={'adult': .5}, paff={'ben': {'adult': .5}, 'ana': {'child': 100}},
                         bscale=1, vbase=0, pbscale={'ben': 1, 'ana': 1}, nexp={'ben': 40, 'ana': 40})
            with patch.object(s, 'frec', return_value={'title': 'Synthetic'}), \
                    patch.object(s, 'fit', return_value=None), patch.object(s, 'quality_prior', return_value=0), \
                    patch.object(s, 'facet_bonus', side_effect=lambda m, r, aff=None: (sum((aff if aff is not None else m['aff']).values()), [])):
                self.assertEqual(s.score_key(model, 'tmdb:3', 'together'), s.score_key(model, 'tmdb:3', 'ben'))
                model['paff']['ana']['child'] = -100
                self.assertEqual(s.score_key(model, 'tmdb:3', 'together'), s.score_key(model, 'tmdb:3', 'ben'))

    def test_setup_asks_children_default_no(self):
        for answers, age in (([''], None), (['yes', 'sam', '8'], 8)):
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATA_DIR': temp, 'SKALD_CONFIG': '',
                    'TMDB_API': 'fake', 'OPENROUTER_API_KEY': ''}), patch.object(setup, 'check_tmdb', return_value=True), \
                    patch('builtins.input', side_effect=answers) as ask:
                flags = ['setup', '--members', 'alex,sam', '--lang', 'en-US', '--timezone', 'UTC',
                         '--country', 'United States', '--base', 'https://tv.example', '--watch', 'stremio', '--connect', 'files']
                self.assertEqual(main(flags, out=io.StringIO()), 0)
                self.assertEqual(ask.call_args_list[0].args[0], 'Any children watching? [no]: ')
                cfg = json.loads((Path(temp) / 'skald.json').read_text())
                self.assertEqual(cfg['taste']['people']['sam'].get('prefs', {}).get('kids_age'), age)
                with patch('builtins.input', side_effect=AssertionError('asked twice')):
                    self.assertEqual(main(flags, out=io.StringIO()), 0)
