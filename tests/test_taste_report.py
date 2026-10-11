from skald.members import rid
from decide_kit import write_legacy
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from skald.engine import Engine
from skald.__main__ import execute, parser, text_output

NOW = 1791734400.


class TasteCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name)
        write_legacy(data)
        entries = [{"key": f"tmdb:{i}", "id": i, "kind": "movie", "stype": "movie", "title": f"Fixture {i}",
                    "year": "2020", "weight": 3., "signal": "loved", "age": 10., "by": [who],
                    "who": "remote", "flags": [], "genres": [], "item": {}}
                   for i, who in ((1, "ben"), (2, "ana"))]
        (data / "profile.json").write_text(json.dumps({"entries": entries, "built_at": NOW, "seen": [e['key'] for e in entries]}))
        (data / "taste.json").write_text(json.dumps({"people": {}}))
        facets = {f"tmdb:{i}": {"kind": "movie", "title": f"Fixture {i}", "y": "2020", "x": {"cozy_case": 2},
                  "k": [[i % 5, f"Theme {i % 5}"]], "d": [[i % 3, f"Director {i % 3}"]], "g": ["Drama"]}
                  for i in range(1, 81)}
        (data / "facets.json").write_text(json.dumps({"items": facets}))
        with redirect_stdout(io.StringIO()):
            self.e = Engine(data)
        self.addCleanup(self.e.close)
        self.s = self.e._server


class TasteReportTests(TasteCase):
    def stages(self):
        s = self.s
        def ev(i, t):
            return s._event(t, str(i), f"tmdb:{i}", "movie", f"Fixture {i}", "2020", "ben", ["ben"],
                            "love", "remote", {"row": None})
        return [("remote", [ev(1, NOW-20*s.DAY), ev(2, NOW), ev(3, None)])]

    def test_axes_movers_attribution_decay_and_undated(self):
        stages = self.stages()
        # A recent title adds its unique keyword, while a 31-day-old like crosses a decay boundary.
        self.s._facets['tmdb:2']['k'] = [[100, 'New theme']]
        stages[0][1][0].update(t=NOW-31*self.s.DAY, action='like')
        kept = [(stage, [ev for ev in evts if ev['t'] is None or ev['t'] <= NOW-7*self.s.DAY]) for stage, evts in stages]
        with patch.object(self.e, '_report_events', return_value=(NOW, stages)), patch.object(self.s, 'feed_rows', return_value=[]):
            r = self.e.report('ben')
            full = self.e._events_model(stages, NOW)['paff']['ben']
            old = self.e._events_model(kept, NOW)['paff']['ben']
            expected = {f for f in full.keys() | old.keys() if abs(full.get(f, 0)-old.get(f, 0)) > 1e-9}
            self.assertEqual({f['id'] for f in r['movers']['facets']}, expected)
            self.assertEqual(r['movers']['window']['undated'], 1)
            f = next(f for f in r['movers']['facets'] if f['id'] == 'k:100')
            self.assertEqual([ev['key'] for ev in f['events']], ['tmdb:2'])
            self.assertTrue(r['movers']['decay'])
            self.assertEqual(self.e.report('ben', since=90)['movers']['facets'], [])
        self.assertEqual(set(r['axes']), {'x', 'kt', 'd', 'a', 'g'})
        self.assertEqual(r['axes'], self.s.taste_axes(self.e._model, 'ben'))
        self.assertIn('k:3', old)  # undated title remains in the baseline.

    def test_trace_sum_fifty_keys_each_person(self):
        for rec in self.s._facets.values():
            rec.update(vote_average=9., vote_count=10000)
        with patch.object(self.e, '_report_events', return_value=(NOW, [])), \
                patch.object(self.s, 'SCORE_BLEND', (.4, .2, .3)), patch.object(self.s, 'QUALITY_PRIOR', True), \
                patch.object(self.s, 'fit', return_value=.25):
            for person in self.s.PEOPLE:
                for key in random.Random(7).sample(sorted(self.e._index), 50):
                    trace = self.e.trace(key, person)
                    score = self.s.person_score(self.e._model, key, person, 'movie')[0]
                    self.assertAlmostEqual(sum(trace['parts'].values()), score, delta=1e-9)

    def test_calibrated_leave_one_out_and_trace_evidence(self):
        def trending(path, **kw):
            self.assertTrue(path.startswith('trending/'))
            return {'results': [{'id': i} for i in range(10, 50)] if '/movie/' in path else []}
        with patch.object(self.s, 'tmdb', side_effect=trending), \
                patch.object(self.e, '_report_events', return_value=(NOW, self.stages())), \
                patch.object(self.s, 'feed_rows', return_value=[]):
            self.e._model = self.e._build(self.e._profile)
            self.s._model['m'] = self.e._model
            report = self.e.report('ben')
            held = report['calibration']['loved_wildcard']
            self.assertEqual(held['n'], 0)
            self.assertIsNone(held['share'])
            self.assertEqual(report['maturity']['state'], 'too_new')
            self.assertEqual(held['titles'][0]['key'], 'tmdb:1')
            trace = self.e.trace('tmdb:1', 'ben')
            self.assertTrue(trace['events'])
            self.assertLessEqual(len(trace['events']), 5)
            self.assertTrue(all({'src', 'day', 'weight'} <= ev.keys() for ev in trace['events']))

    def test_rows_calibration_alias_and_text_bound(self):
        m = self.e._model
        m['cal'] = 21
        m['bg'] = [i/100 for i in range(-30, 31, 3)]
        m['pbg'] = {p: m['bg'] for p in self.s.PEOPLE}
        metas = [{'id': f'tmdb:{i}', 'type': 'movie'} for i in (3, 4, 5)]
        stages = self.stages()
        with patch.object(self.e, '_report_events', return_value=(NOW, stages)), \
                patch.object(self.s, 'feed_rows', return_value=[(rid('foryou', self.s.CONFIG), ('tv',), 'For You'),
                    (rid('top10', self.s.CONFIG), ('movie', 'series'), 'Top 10')]), \
                patch.object(self.s, 'row_metas', return_value=metas):
            r = self.e.report('ben', before=self.temp.name)
            self.assertAlmostEqual(sum(r['rows'][rid('foryou', self.s.CONFIG)].values()), 1.)
            self.assertEqual(r['rows'][rid('foryou', self.s.CONFIG)]['memory'], 0)
            self.assertIn(f"{rid('top10', self.s.CONFIG)}:movie", r['rows'])
            self.assertIn(f"{rid('top10', self.s.CONFIG)}:series", r['rows'])
            self.assertEqual(sum(r['calibration']['hist'].values()), 0)
            self.assertAlmostEqual(sum(r['calibration']['labels'].values()), 1.)
            self.assertEqual(set(r['calibration']['loved_wildcard']), {'share', 'n', 'titles'})
            self.assertEqual(r['calibration']['overlap']['jaccard'], 1.)
            args = parser().parse_args(['taste', 'report', '--person', 'ben'])
            out = io.StringIO()
            text_output(args, r, out)
            self.assertLessEqual(len(out.getvalue().splitlines()), 13)
            alias = parser().parse_args(['explain', 'tmdb:3', '--person', 'ben'])
            why = parser().parse_args(['taste', 'why', 'tmdb:3', '--person', 'ben', '--trace'])
            self.assertEqual(execute(alias, self.e, None, None), execute(why, self.e, None, None))
        for bad in (0, -1, 36501):
            with self.assertRaises(ValueError):
                self.e.report(since=bad)


if __name__ == '__main__':
    unittest.main()
