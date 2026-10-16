"""W24 counterexample: generic metadata can overpower experience coverage."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from skald.engine import Engine
from skald.members import fresh_config
from eval.replay import same_franchise
from test_stop_report import entry


def snapshot(data):
    (data/'skald.json').write_text(json.dumps(fresh_config(['eva', 'oskar'])))
    entries = [entry(i) for i in range(1, 21)] + [entry(i, 'oskar') for i in range(21, 41)]
    facets = {}
    for i in range(1, 343):
        serious = i <= 18 or i == 42
        facets[f'tmdb:{i}'] = {'kind': 'movie', 'title': f'Fable {chr(65+i//26)}{chr(65+i%26)}', 'y': '2020',
            'lang': 'ja' if i <= 12 else 'en',
            'k': [[1, 'Adventure'], [2, 'Friendship']] if i <= 42 else [[i, f'Background {i}'], [1000+i, f'Theme {i}']],
            'c': [[1, 'Fixture studio']], 'g': ['Animation', 'Science Fiction'] if serious else ['Family', 'Comedy'],
            'x': {'dread': 3, 'big_ideas': 3} if serious else {'feel_good': 3, 'banter_duo': 3},
            'o': 'An invented story.'}
    for e in entries:
        e.update(title=facets[e['key']]['title'], genres=facets[e['key']]['g'],
                 flags=['anime'] if e['id'] <= 12 else [])
    # Held out thumbs down: neither candidate enters the training profile.
    for i, title in ((41, 'Pixel Picnic'), (42, 'Ash Orbit')):
        facets[f'tmdb:{i}'].update(title=title)
        del facets[f'tmdb:{i}']['x']
    (data/'profile.json').write_text(json.dumps({'entries': entries, 'seen': [e['key'] for e in entries]}))
    (data/'facets.json').write_text(json.dumps({'items': facets}))
    return {'tmdb:41': {'feel_good': 3, 'banter_duo': 3}, 'tmdb:42': {'dread': 3, 'big_ideas': 3}}


class ToneFitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.overlay = snapshot(Path(temp.name))
        with redirect_stdout(io.StringIO()):
            self.e = Engine(temp.name)
        self.addCleanup(self.e.close)

    def pct(self, key, person):
        s, m = self.e._server, self.e._model
        return s.confidence(m, key, *s.person_score(m, key, person), person)[1]

    def test_baseline_reproduces_top_decile(self):
        self.assertGreaterEqual(len(self.e._model['pbg']['eva']), 200)
        self.assertGreaterEqual(self.pct('tmdb:41', 'eva'), 90)
        self.assertNotIn('tmdb:41', {e['key'] for e in self.e._profile['entries']})
        for e in self.e._profile['entries']:
            self.assertFalse(same_franchise(self.e._server, e['key'],
                {'entries': [other for other in self.e._profile['entries'] if other['key'] != e['key']]}))
        a, b = (self.e._server._facets[f'tmdb:{i}'] for i in (41, 42))
        self.assertEqual(a['k'], b['k'])
        self.assertEqual(a['c'], b['c'])
        self.assertEqual(sum(len(a.get(f, [])) for f in 'kdancgx'),
                         sum(len(b.get(f, [])) for f in 'kdancgx'))

    def expand(self):
        self.e._server._facets.update({key: {**self.e._server._facets[key], 'x': tags}
                                      for key, tags in self.overlay.items()})
        self.e._refresh()

    @unittest.expectedFailure  # W24a-B: Pixel Picnic still reaches Eva's top decile.
    def test_expanded_coverage_leaves_evas_top_decile(self):
        self.expand()
        self.assertLess(self.pct('tmdb:41', 'eva'), 90)

    def test_expanded_coverage_ranks_below_ash_orbit_for_eva(self):
        self.expand()
        self.assertLess(self.e._server.person_score(self.e._model, 'tmdb:41', 'eva')[0],
                        self.e._server.person_score(self.e._model, 'tmdb:42', 'eva')[0])

    def test_expanded_coverage_keeps_oskar_in_top_decile(self):
        self.expand()
        self.assertGreaterEqual(self.pct('tmdb:41', 'oskar'), 90)


if __name__ == '__main__':
    unittest.main()
