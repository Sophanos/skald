import copy
import json
import random
import time
import tracemalloc
import unittest
from unittest.mock import patch

from test_api_routes import ApiCase


class TasteReportApiTests(ApiCase):
    def test_route_privacy_and_empty_ring(self):
        self.assertIn('taste', self.s.API_ROUTES)
        self.assertTrue(self.request('/api/v1/taste?person=together', client=None)[1]['axes']['x'])
        self.assertEqual(self.request('/api/v1/taste?person=ana', client='ben')[0], 403)
        for person in ('ben', 'together'):
            code, data = self.request('/api/v1/taste?person='+person, client='ben')
            self.assertEqual(code, 200)
            self.assertEqual(data['movers'], {'warming_up': True})
            self.assertNotIn('Ana', json.dumps(data))
        with patch.object(self.s, 'build_model', side_effect=AssertionError('read rebuilt model')), \
                patch.object(self.s, 'explain', side_effect=AssertionError('read used explain')):
            self.assertEqual(self.request('/api/v1/taste')[0], 200)
        for days in ('0', '-1', '366', 'bad'):
            self.assertEqual(self.request('/api/v1/taste?days='+days)[0], 400)

    def test_ring_daily_bound_and_numeric_movers(self):
        s = self.s
        m = s._model['m']
        rng = random.Random(7)
        models = []
        for i in range(30):
            aff = {f'k:{10000+j}': rng.random() for j in range(1500)}
            models.append({**m, 'aff': aff, 'paff': {p: {k: v+i/100 for k, v in aff.items()} for p in s.PEOPLE}})
        tracemalloc.start()
        try:
            start = tracemalloc.get_traced_memory()[0]
            for i, model in enumerate(models):
                s.taste_ring_note(model, 1791734400+i*s.DAY)
            growth = tracemalloc.get_traced_memory()[0]-start
        finally:
            tracemalloc.stop()
        self.assertLessEqual(growth, 20*1024)
        self.assertEqual(len(s._taste_ring), 8)
        before = dict(s._taste_ring)
        s.taste_ring_note(m, 1791734400+29*s.DAY+60)
        self.assertNotEqual(before, s._taste_ring)
        self.assertEqual(before.keys(), s._taste_ring.keys())
        data = self.request('/api/v1/taste?person=ben&days=7', client='ben')[1]
        self.assertIn('facets', data['movers'])
        self.assertEqual(s._model['m'], m)

    def test_size_and_warm_latency(self):
        m = self.s._model['m']
        for key, (title, by) in list(m['ent'].items()):
            m['ent'][key] = ('長'*300, by)
        for fid in m['names']:
            m['names'][fid] = '長'*300
        calls = []
        for _ in range(30):
            start = time.perf_counter()
            code, data = self.request('/api/v1/taste', client='ben')
            calls.append((time.perf_counter()-start)*1000)
            self.assertEqual(code, 200)
            self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), 8192)
        self.assertLessEqual(sorted(calls)[28], 50)


if __name__ == '__main__':
    unittest.main()
