"""The four W56b review fixes, with synthetic history and stubbed services."""
import hashlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import test_prefs


class TogetherFixes(unittest.TestCase):
    setUpClass = classmethod(test_prefs.PrefsRows.setUpClass.__func__)
    prefs = test_prefs.PrefsRows.prefs

    def test_returning_rows_use_adult_seeds(self):
        s = self.s
        entries = [dict(key=f'tmdb:{i}', id=i, kind=kind, stype=typ, title='Synthetic', signal='loved',
                        weight=2, age=100, by=[person], who='tv', genres=[], flags=[], item={'last_episode': [1, 1]})
                   for i, kind, typ, person in ((1, 'movie', 'movie', 'ben'), (2, 'tv', 'series', 'ben'),
                                               (3, 'movie', 'movie', 'ana'), (4, 'tv', 'series', 'ana'))]
        p = dict(entries=entries, seen={e['key'] for e in entries}, negative=set())
        for prefs, expected in (({}, {1, 2, 3, 4}), ({'kids_age': 8}, {1, 2})):
            with self.prefs(ana=prefs), ExitStack() as stack:
                stack.enter_context(patch.object(s, '_profiles', {'household': p}))
                stack.enter_context(patch.object(s, 'read_taste', return_value={}))
                details = stack.enter_context(patch.object(s, 'details_pair', return_value=({}, {})))
                stack.enter_context(patch.object(s, 'row_meta', return_value={}))
                for name in ('show_upcoming', 'show_catchup', 'anime_upcoming', 'soon_for_taste'):
                    stack.enter_context(patch.object(s, name, return_value=[]))
                collections = stack.enter_context(patch.object(s, 'movie_collections', return_value=[]))
                s.rewatch_feed('en-US')
                self.assertEqual({c.args[1] for c in details.call_args_list}, expected)
                s.upcoming_feed('en-US'); s.catchup_feed('en-US')
                self.assertTrue(all({e['id'] for e in c.args[0]['entries']} == expected for c in collections.call_args_list))

    def test_mood_learning_uses_play_attribution_not_title_overlap(self):
        s = self.s
        raw = [dict(id='tmdb:1', at=1000, profile='ben'), dict(id='tmdb:1', at=2000, profile='ana')]
        ctx = [dict(key='tmdb:1', at=i, title=title, kind='movie', weekday='Sunday', part='evening')
               for i, title in ((1, 'Adult play'), (2, 'Child replay'))]
        mood = next(iter(s.MOODS)); row = 'mood:' + mood
        hits = [{**c, 'row': row, 'share': share} for c, share in zip(ctx, (1, 5))]
        p = {'entries': [{'key': 'tmdb:1', 'by': ['ben', 'ana'], 'item': raw[1]}]}
        learn = dict(ctx=ctx, hits=hits, rows={row: {'shown': 8, 'hits': 6}}, moodpicks={})
        with self.prefs(ana={'kids_age': 8}), patch.object(s, '_profiles', {'household': p}), \
                patch.object(s, '_learn', learn), patch.object(s, 'read_history', return_value=raw), \
                patch.object(s, 'attribute', side_effect=lambda it: (it['profile'], [it['profile']], {})):
            state = s.mood_state({'names': {}, 'aff': {}})
            self.assertIn('Adult play', state)
            self.assertNotIn('Child replay', state)
            self.assertIn('shown 8x, played 1', state)
            self.assertNotIn('Child replay', '; '.join(s.hit_titles()))
            self.assertEqual(learn['rows'][row]['hits'], 6)

    def test_together_maturity_counts_adults_only(self):
        s = self.s
        m = {'nexp': {'ana': 100, 'ben': 5}, 'ent': {'shared': ('Synthetic', ('ana', 'ben'))}}
        with self.prefs(ana={'kids_age': 8}):
            for who in (None, 'both', 'together'):
                self.assertEqual(s.maturity(m, who)['explicit'], 5)
                self.assertEqual(s.maturity(m, who)['state'], 'too_new')
            self.assertEqual(s.maturity(m, 'ana')['explicit'], 100)
            self.assertEqual(s.maturity(m, 'ana')['state'], 'ready')
            self.assertEqual(s.maturity({**m, 'nexp': {**m['nexp'], 'household': 105}}, 'together')['explicit'], 5)

    def test_shared_stamps_change_with_child_set_only(self):
        s = self.s
        def stamps():
            return {name: stamp for name, _, stamp, _, _ in s.job_specs(s.local_now(), {'household': 'same'})}
        with patch.object(s, 'OR_KEY', 'synthetic'):
            before = stamps()
            self.assertEqual(hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(), '692d4460d206e4cc29bf3ac9a91c0439eea738d797127cf002b22dc3c8b09d5a')
            with self.prefs(ana={'kids_age': 8}):
                child = stamps()
            with self.prefs(ana={'kids_age': 13}):
                older = stamps()
            with self.prefs(ben={'kids_age': 8}):
                other = stamps()
            for job in ('rewatch', 'upcoming', 'catchup', 'because', 'leap', 'lanes', 'moods', 'docs', 'studios', 'personal', 'week', 'docsai'):
                self.assertNotEqual(before[job], child[job], job)
                self.assertEqual(child[job], older[job], job)
                self.assertNotEqual(child[job], other[job], job)
            for job in ('episodes', 'top10', 'airing', 'nextseason', *s.PEOPLE):
                if job in before:
                    self.assertEqual(before[job], child[job], job)
            self.assertEqual(before, stamps())
            rule = list(s.MOODS)[:len(s.MOODS_ROWS)]; old = list(reversed(rule))
            day = (s.local_now() - s.datetime.timedelta(hours=5)).date().isoformat()
            record = {'stamp': 'v' + s.STAMP_V + '/' + before['moods']}
            with patch.object(s, 'OR_KEY', ''), patch.object(s, '_feeds', {(s.MOODS_ROWS[0], s.LANG): record}), \
                    patch.object(s, '_learn', {'moodpicks': {day: {'picks': old}}}):
                self.assertEqual(s.pick_moods({}, {}, rule), old)
                with self.prefs(ana={'kids_age': 8}):
                    self.assertEqual(s.pick_moods({}, {}, rule), rule)
                    record['stamp'] = 'v' + s.STAMP_V + '/' + child['moods']
                    self.assertEqual(s.pick_moods({}, {}, old), rule)
