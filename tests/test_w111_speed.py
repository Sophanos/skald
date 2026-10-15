"""Streaming search latency with synthetic OpenRouter and TMDB transports."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import patch

from decide_kit import close_server, load_server
import test_w110_search as fallback

WAIT = Event.wait


class TimedSSE:
    def __init__(self, lines, first=.03, interval=.005, tail=0, timeout=7, error=False, clock=time.monotonic, finish_gate=None):
        self.clock, self.start = clock, clock()
        self.timeout, self.closed = timeout, False
        self.stopped = Event()
        self.finish_gate = finish_gate
        self.reading_tail = Event()
        self.events = []
        for i, line in enumerate(lines):
            for part in (line[:4], line[4:] + '\n'):
                data = {'choices': [{'delta': {'content': part}}]}
                self.events.append((first + i * interval, ('data: ' + json.dumps(data) + '\n').encode()))
        end = first + max(0, len(lines) - 1) * interval + tail
        data = {'error': {'message': 'synthetic failure'}} if error else {'choices': [], 'usage': {'cost': .001, 'total_tokens': 90}}
        self.events.extend([(end, ('data: ' + json.dumps(data) + '\n').encode()), (end, b'data: [DONE]\n')])

    def read1(self, size=-1):
        if not self.events:
            return b''
        if len(self.events) == 2 and self.finish_gate is not None:
            self.reading_tail.set()
            self.finish_gate.wait()
            if self.stopped.is_set():
                return b''
        at, data = self.events.pop(0)
        left = self.timeout - (self.clock() - self.start)
        delay = max(0, self.start + at - self.clock())
        if WAIT(self.stopped, min(delay, max(0, left))):
            return b''
        if delay >= left:
            raise TimeoutError('synthetic stall')
        return data[:size] if size >= 0 else data

    def close(self):
        self.closed = True
        self.stopped.set()
        if self.finish_gate is not None:
            self.finish_gate.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SearchSpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.s = load_server(cls.temp.name)
        cls.addClassCleanup(close_server, cls.s)

    def setUp(self):
        self.h = fallback.SearchFallbackTests()
        self.h.s = self.s
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.lines = [f'Title {i}|2000|m' for i in range(100, 130)]
        self.first, self.interval, self.tail = .03, .005, 0
        self.error = False
        self.finish_gate = None
        self.streams, self.resolved = [], []
        self.title_delay, self.resolve_delay = 0, .005
        self.enterContext(patch.object(self.s, '_connector_open', side_effect=self.reply))
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=self.tmdb))
        self.enterContext(patch.object(self.s, '_model', {'m': None}))
        self.enterContext(patch.object(self.s, 'SEARCH_SERVE_AFTER', .16, create=True))
        from skald.search import ResolutionCache
        directory = self.enterContext(tempfile.TemporaryDirectory())
        cache = ResolutionCache(Path(directory) / 'resolve.sqlite3')
        self.addCleanup(cache.close)
        self.enterContext(patch.object(self.s, '_search_resolutions', cache))
        self.addCleanup(self.h.finish_jobs)

    def reply(self, req, timeout):
        body = json.loads(req.data)
        if body.get('stream'):
            stream = TimedSSE(self.lines, self.first, self.interval, self.tail, timeout, self.error,
                              finish_gate=self.finish_gate)
            self.streams.append(stream)
            return stream
        return self.h.reply(req, timeout)

    def tmdb(self, path, **params):
        if path == 'search/multi':
            time.sleep(self.title_delay)
        elif path in ('search/movie', 'search/tv') and params.get('query', '').startswith('Title 1'):
            time.sleep(self.resolve_delay)
            self.resolved.append((params['query'], time.monotonic(), bool(self.streams and not self.streams[-1].closed)))
        return self.h.tmdb_reply(path, **params)

    def test_stream_resolves_before_close_and_serves_ranked_threshold(self):
        self.finish_gate = Event()
        self.addCleanup(self.finish_gate.set)
        self.enterContext(patch.object(self.s, 'SEARCH_TIMEOUT', 10))
        self.enterContext(patch.object(self.s, 'SEARCH_HEAD_START', 30))
        submit = self.s._pool.submit
        def ready_genres(fn, *args, **kwargs):
            if fn is self.s.genres:
                job = Future()
                job.set_result(fn(*args, **kwargs))
                return job
            return submit(fn, *args, **kwargs)
        # Cache reuse is independent of scheduling another genre lookup after the last stream event.
        self.enterContext(patch.object(self.s._pool, 'submit', side_effect=ready_genres))
        self.enterContext(patch.object(self.s, '_model', {'m': {'paff': {'ana': {'x:quiet': 1}}}}))
        start = time.monotonic()
        metas = self.h.results()
        elapsed = time.monotonic() - start
        self.assertTrue(self.streams, 'wide picker must stream')
        self.assertTrue(any(opened for _, _, opened in self.resolved))
        self.assertFalse(self.streams[0].closed)
        self.assertGreaterEqual(len(metas), 15)
        self.assertLess(elapsed, 3)
        ids = [int(m['id'][5:]) for m in metas]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertTrue(self.s._scoping.search_timing['early'])
        start = time.monotonic()
        self.h.results()
        self.assertLess(time.monotonic() - start, 3, 'repeat while the stream is open')
        self.assertFalse(self.streams[0].closed)
        self.finish_gate.set()
        self.h.finish_jobs()
        self.assertTrue(self.streams[0].closed)
        picker = next(plan.result()['picks'] for _, plan in self.s._ai.values())
        self.assertFalse(picker.search_stream.jobs, 'completed answers retain picks, not TMDB futures')
        calls = len(self.resolved)
        self.h.tmdb_calls.clear()
        start = time.monotonic()
        full = self.h.results()
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(full[0]['id'], 'tmdb:129')
        self.assertEqual(len(self.streams), 1)
        self.assertEqual(len(self.resolved), calls)
        self.assertFalse(any(path == 'search/multi' for path, _ in self.h.tmdb_calls))

    def test_closed_stream_does_not_wait_for_slow_resolutions_after_threshold(self):
        self.first, self.interval = .01, .001
        release, entered = Event(), Event()
        self.addCleanup(release.set)
        self.enterContext(patch.object(self.s, 'SEARCH_TIMEOUT', 10))
        self.enterContext(patch.object(self.s, 'SEARCH_HEAD_START', 30))
        original = self.tmdb
        def tmdb(path, **params):
            if path == 'search/movie' and params.get('query') == 'Title 100':
                entered.set()
                release.wait()
            return original(path, **params)
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=tmdb))
        rank = self.s.wide_rank
        def after_close(*args, **kwargs):
            self.assertTrue(self.streams[0].stopped.wait(10), 'rank the completed stream')
            return rank(*args, **kwargs)
        self.enterContext(patch.object(self.s, 'wide_rank', side_effect=after_close))
        start = time.monotonic()
        metas = self.h.results(query='a gentle wish with a slow resolution')
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(len(metas), 15)
        self.assertNotIn('tmdb:100', [m['id'] for m in metas])
        self.assertTrue(self.streams[0].closed)
        self.assertTrue(entered.is_set())
        self.assertFalse(release.is_set(), 'the response must not wait for the slow resolution')

    def test_background_resolution_does_not_delay_title_search(self):
        self.first, self.interval = .01, .001
        self.finish_gate = Event()
        release, entered = Event(), Event()
        self.addCleanup(release.set)
        self.addCleanup(self.finish_gate.set)
        self.enterContext(patch.object(self.s, 'SEARCH_TIMEOUT', 10))
        self.enterContext(patch.object(self.s, 'SEARCH_HEAD_START', 30))
        original = self.tmdb
        def tmdb(path, **params):
            query = params.get('query', '')
            if path == 'search/movie' and query.startswith('Title ') and int(query[6:]) >= 115:
                entered.set()
                release.wait()
            return original(path, **params)
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=tmdb))
        self.h.results(query='a quiet background wish')
        self.assertTrue(entered.wait(10))
        self.h.literal = [{'id': 5, 'title': 'Synthetic Title'}]
        start = time.monotonic()
        metas = self.s.search('movie', 'Synthetic Title', 'en-US', title_only=True)['metas']
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(metas[0]['id'], 'tmdb:5')
        self.assertFalse(release.is_set(), 'background resolutions must still be blocked')

    def test_stall_and_error_fall_back_inside_one_deadline(self):
        for error in (False, True):
            release = Event()
            self.finish_gate = None if error else release
            with self.subTest(error=error), patch.object(self.s, 'SEARCH_TIMEOUT', 3), \
                    patch.object(self.s, 'SEARCH_HEAD_START', .1):
                self.s._ai.clear()
                self.s._last_search.clear()
                self.lines = []
                self.first, self.tail, self.error = 0, 0, error
                try:
                    start = time.monotonic()
                    metas = self.h.results(query=f'a quiet wish {error}')
                    self.assertLess(time.monotonic() - start, 4)
                    self.assertIn('tmdb:90', [m['id'] for m in metas])
                finally:
                    release.set()
                    self.h.finish_jobs()

    def test_stream_and_tmdb_share_the_remaining_deadline(self):
        release = Event()
        self.finish_gate = release
        original = self.tmdb
        def tmdb(path, **params):
            if path == 'search/multi':
                release.wait()
            return original(path, **params)
        with patch.object(self.s, 'SEARCH_TIMEOUT', 2), patch.object(self.s, 'SEARCH_HEAD_START', 30), \
                patch.object(self.s, 'SEARCH_SERVE_AFTER', 30), patch.object(self.s, 'tmdb', side_effect=tmdb):
            self.first, self.interval = 0, 0
            try:
                start = time.monotonic()
                self.h.results(query='a quiet deadline wish')
                self.assertLess(time.monotonic() - start, 2.5)
                calls = [params for path, params in self.h.tmdb_calls if path.startswith('search/')]
                self.assertTrue(calls)
                self.assertTrue(all(0 < params.get('_timeout', 8) <= 2 for params in calls))
            finally:
                release.set()
                self.h.finish_jobs()

    def test_title_check_overlaps_settle_and_exact_title_skips_ai(self):
        title_started, settled, release = Event(), Event(), Event()
        self.addCleanup(release.set)
        self.enterContext(patch.object(self.s, 'SEARCH_TIMEOUT', 10))
        self.enterContext(patch.object(self.s, 'SEARCH_HEAD_START', 30))
        def settle(query, wait):
            if query == 'Made Up Harbor':
                release.wait()
            else:
                self.assertTrue(title_started.wait(10), 'title lookup must overlap settle')
                settled.set()
            return False
        original = self.tmdb
        def tmdb(path, **params):
            if path == 'search/multi' and params.get('query') != 'Made Up Harbor':
                title_started.set()
                self.assertTrue(settled.wait(10), 'settle must overlap title lookup')
            return original(path, **params)
        self.enterContext(patch.object(self.s, 'tmdb', side_effect=tmdb))
        self.enterContext(patch.object(self.s, 'superseded', side_effect=settle))
        self.h.results()
        self.assertTrue(settled.is_set())
        self.h.finish_jobs()
        self.h.literal = [{'id': 5, 'title': 'Made Up Harbor'}]
        self.s._last_search.clear()
        count = len(self.streams)
        start = time.monotonic()
        metas = self.h.results(query='Made Up Harbor')
        self.assertEqual(metas[0]['id'], 'tmdb:5')
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(release.is_set(), 'exact titles must not wait for settle')
        self.assertEqual(len(self.streams), count)

    def test_resolution_cache_is_bounded_and_survives_restart(self):
        from skald.search import ResolutionCache
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resolve.sqlite3'
            cache = ResolutionCache(path, entries=3, byte_limit=1200)
            for i in range(8):
                cache.put(str(i), [{'id': i, 'title': 'Title ' + str(i)}, {}])
            self.assertIsNone(cache.get('0'))
            cache.close()
            restarted = ResolutionCache(path, entries=3, byte_limit=1200)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.get('7')[0]['id'], 7)
            self.assertLessEqual(restarted.stats()['entries'], 3)
            self.assertLessEqual(restarted.stats()['bytes'], 1200)
            restarted.put('large', [{'overview': 'x' * 1400}, {}])
            self.assertIsNone(restarted.get('large'))
            restarted.byte_limit = 80
            restarted.put('last', [{'id': 9}, {}])
            self.assertLessEqual(restarted.stats()['bytes'], 80)

    def test_resolution_cache_reuses_tmdb_fields_across_restart(self):
        from skald.search import ResolutionCache
        path = Path(self.temp.name) / 'restart.sqlite3'
        for i in range(2):
            cache = ResolutionCache(path)
            with patch.object(self.s, '_search_resolutions', cache):
                self.assertEqual(self.s.resolve_pick('movie', 'Title 156', '2000', 'en-US')[0]['id'], 156)
            cache.close()
        self.assertEqual(sum(path == 'search/movie' for path, _ in self.h.tmdb_calls), 1)

    def test_timing_cli_reads_saved_stage_times_without_ai(self):
        from skald.__main__ import main
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DATA_DIR': directory}):
            Path(directory, 'searches.jsonl').write_text(json.dumps({'t': 1, 'q': 'quiet stories',
                'timing': {'total_ms': 2700, 'picker_ms': 2200, 'first_line_ms': 600}}) + '\n')
            out = io.StringIO()
            cases = Path(__file__).resolve().parents[1] / 'eval/commands_cases.jsonl'
            case = next(json.loads(line) for line in cases.read_text().splitlines() if json.loads(line)['id'] == 'search-timing-cli')
            self.assertEqual(main(case['argv'], out=out), 0)
            self.assertEqual(json.loads(out.getvalue())['searches'][0]['timing']['total_ms'], 2700)

    def test_streamed_route_fixtures_keep_requests_and_title_lookalikes(self):
        path = Path(__file__).resolve().parents[1] / 'eval/fixtures/decisions/route_query.jsonl'
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row['note'] != 'Synthetic: streamed search speed contract':
                continue
            self.s._ai.clear()
            self.s._last_search.clear()
            query = row['in']['query']
            title = row['expect']['choice'] == 'title'
            self.h.literal = [{'id': 5, 'title': query}] if title else []
            stype, code = ('series', 's') if query.startswith(('Serien', 'serije')) else ('movie', 'm')
            self.lines = [f'Title {i}|2000|{code}' for i in range(100, 130)]
            with self.subTest(query=query):
                metas = self.h.results(query=query, stype=stype)
                self.assertEqual(metas[0]['id'], 'tmdb:5' if title else 'tmdb:100')
            self.h.finish_jobs()


if __name__ == '__main__':
    unittest.main()
