"""Review regressions using only synthetic transports and disposable caches."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from decide_kit import close_server, load_server
import test_w110_search as fallback


def event(value):
    return ('data: ' + json.dumps(value) + '\n\n').encode()


class FragmentedSSE:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = Event()
        self.reads = 0

    def read1(self, size):
        if not self.chunks or self.closed.is_set():
            return b''
        delay, chunk = self.chunks.pop(0)
        if self.closed.wait(delay):
            return b''
        self.reads += 1
        if len(chunk) > size:
            self.chunks.insert(0, (0, chunk[size:]))
        return chunk[:size]

    def readline(self, size):
        line = b''
        while len(line) < size:
            chunk = self.read1(1)
            if not chunk:
                break
            line += chunk
            if chunk == b'\n':
                break
        return line

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SearchReviewTests(unittest.TestCase):
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
        self.enterContext(patch.object(self.s, '_model', {'m': None}))
        from skald.search import ResolutionCache
        directory = self.enterContext(tempfile.TemporaryDirectory())
        cache = ResolutionCache(Path(directory) / 'resolve.sqlite3')
        self.addCleanup(cache.close)
        self.enterContext(patch.object(self.s, '_search_resolutions', cache))

    def stream(self, chunks):
        stream = FragmentedSSE(chunks)
        self.enterContext(patch.object(self.s, '_connector_open', return_value=stream))
        return stream

    def test_byte_trickle_closes_at_absolute_deadline(self):
        stream = self.stream([(.01, bytes([c])) for c in event({'choices': []})])
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.s.openrouter('/api/v1/chat/completions', {}, .05, 'search', 'wide_picks', lambda text: None)
        self.assertLess(time.monotonic() - start, .1)
        self.assertTrue(stream.closed.is_set())

    def test_fragmented_events_keep_text_and_finalize_usage_once(self):
        payload = (b': keepalive\n\n' + event({'choices': [{'delta': {'content': 'Made Up '}}]})
                   + event({'choices': [{'delta': {'content': 'H\u00e4rbor|2000|m\n'}}]})
                   + event({'usage': {'cost': .4, 'total_tokens': 900}}) + b'data: [DONE]\n\n')
        self.stream([(0, bytes([c])) for c in payload])
        texts = []
        with patch.object(self.s, 'save_usage', wraps=self.s.save_usage) as saved:
            result = self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'wide_picks', texts.append)
        self.assertEqual(''.join(texts), 'Made Up H\u00e4rbor|2000|m\n')
        self.assertEqual(result['choices'][0]['message']['content'], ''.join(texts))
        self.assertEqual(saved.call_count, 2, 'one reservation and one completion save')
        self.assert_usage_saved()

    def assert_usage_saved(self):
        for usage in (self.s._usage, json.loads(Path(self.s.DATA, 'usage.json').read_text())):
            self.assertEqual(usage['cost'], .4)
            self.assertEqual(usage['month_cost'], .4)
            self.assertEqual(usage['tokens'], 900)
            self.assertEqual(usage['calls'], {'search:wide_picks': 1})
            self.assertEqual(usage['month_calls'], 1)

    def test_usage_received_before_incomplete_eof_is_saved(self):
        self.stream([(0, event({'usage': {'cost': .4, 'total_tokens': 900}}))])
        with patch.object(self.s, 'save_usage', wraps=self.s.save_usage) as saved:
            with self.assertRaises(TimeoutError):
                self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'wide_picks', lambda text: None)
        self.assertEqual(saved.call_count, 3, 'reservation, completion and failure reason saves')
        self.assert_usage_saved()

    def test_usage_in_provider_error_is_saved_and_code_is_preserved(self):
        self.stream([(0, event({'usage': {'cost': .4, 'total_tokens': 900}, 'error': {'code': 402}}))])
        try:
            self.s.openrouter('/api/v1/chat/completions', {}, 1, 'search', 'wide_picks', lambda text: None)
        except Exception as error:
            self.assertEqual(self.s.search_failure(error), 'credit')
        else:
            self.fail('provider error must fail the stream')
        self.assert_usage_saved()

    def test_midstream_codes_reach_existing_fallback_notices(self):
        for status, notice in ((402, 'credit used up'), (429, 'provider rate limit'), (503, 'provider unavailable')):
            self.s._ai.clear()
            self.s._last_search.clear()
            def reply(req, timeout):
                if json.loads(req.data).get('stream'):
                    return io.BytesIO(event({'error': {'code': status}}))
                return self.h.reply(req, timeout)
            def narrow_fails(label, timeout):
                if label == 'picks':
                    raise TimeoutError('synthetic narrow failure')
            self.h.transport = narrow_fails
            with self.subTest(status=status), patch.object(self.s, '_connector_open', side_effect=reply), \
                    patch.object(self.s, 'SEARCH_TIMEOUT', .3):
                start = time.monotonic()
                self.h.assert_notice(self.h.results(), notice)
                self.assertLess(time.monotonic() - start, .3)
                self.h.finish_jobs()

    def test_cancel_after_early_serve_closes_active_stream(self):
        first = event({'choices': [{'delta': {'content': '\n'.join(f'Title {i}|2000|m' for i in range(100, 118)) + '\n'}}]})
        stream = FragmentedSSE([(0, first), (.5, event({'choices': []})), (0, b'data: [DONE]\n')])
        def reply(req, timeout):
            return stream if json.loads(req.data).get('stream') else self.h.reply(req, timeout)
        with patch.object(self.s, '_connector_open', side_effect=reply), \
                patch.object(self.s, 'SEARCH_TIMEOUT', .25), patch.object(self.s, 'SEARCH_SERVE_AFTER', .05):
            self.assertEqual(len(self.h.results()), 15)
            self.assertTrue(self.s._scoping.search_timing['early'])
            picker = next(plan.result()['picks'] for _, plan in self.s._ai.values())
            self.assertFalse(stream.closed.is_set())
            self.s.cancel_search_picks(picker)
            self.assertTrue(stream.closed.wait(.08), 'cancel must interrupt an active read')
            with self.assertRaises(TimeoutError):
                picker.result(timeout=.08)
            reads = stream.reads
            time.sleep(.02)
            self.assertEqual(stream.reads, reads)

    def test_cancel_stops_queued_resolutions_and_new_submissions(self):
        release, entered = Event(), Event()
        self.addCleanup(release.set)
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        self.enterContext(patch.object(self.s, '_search_pool', pool))
        stream = self.s.SearchStream('movie', {})
        def resolve(*args):
            entered.set()
            release.wait(1)
        with patch.object(self.s, 'resolve_pick', side_effect=resolve) as resolved:
            stream.resolving('movie', 'en-US', time.monotonic() + 2)
            for i in range(5):
                stream.add((f'Title {i}', '2000', 'movie', ''))
            self.assertTrue(entered.wait(1))
            picker = Mock(search_stop=Event(), search_stream=stream, search_rules=None, search_fallback=None)
            self.s.cancel_search_picks(picker)
            count = len(stream.jobs)
            stream.add(('Later title', '2000', 'movie', ''))
            self.assertEqual(len(stream.jobs), count)
            release.set()
            pool.shutdown()
            self.assertEqual(resolved.call_count, 1)

    def test_new_search_stops_old_hedge(self):
        release, entered = Event(), Event()
        def transport(label, timeout):
            if label == 'wide_picks':
                entered.set()
                release.wait(1)
        self.h.transport = transport
        with self.s.addon_scope('ana'), patch.object(self.s, 'SEARCH_HEAD_START', .08):
            self.s.start_search('old synthetic wish')
            plan = self.s.ai_future('old synthetic wish', deadline=time.monotonic() + 2)
            picker = plan.result(timeout=1)['picks']
            try:
                self.assertTrue(entered.wait(1))
                self.s.start_search('new synthetic wish')
                picker.search_hedge.result(timeout=.3)
                self.assertNotIn('picks', [label for label, *_ in self.h.calls])
                self.assertTrue(picker.search_stop.is_set())
            finally:
                release.set()

    def test_new_search_stops_queued_provider_jobs(self):
        release = Event()
        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(release.wait, 1)
        try:
            with self.s.addon_scope('ana'), patch.object(self.s, '_pool', pool):
                self.s.start_search('old queued wish')
                plan = self.s.ai_future('old queued wish', deadline=time.monotonic() + 2)
                picker = plan.result(timeout=1)['picks']
                self.s.start_search('new queued wish')
                release.set()
                pool.shutdown()
                self.assertTrue(picker.cancelled())
                self.assertFalse(any(label in ('wide_picks', 'picks', 'rules') for label, *_ in self.h.calls))
        finally:
            release.set()
            pool.shutdown()


class ResolutionCacheReviewTests(unittest.TestCase):
    def setUp(self):
        from skald.search import ResolutionCache
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'resolve.sqlite3'
        self.cache = ResolutionCache(self.path)
        self.addCleanup(self.cache.close)

    def test_corrupt_image_restarts_empty_and_accepts_replacements(self):
        self.path.write_bytes(b'corrupt synthetic sqlite image' * 10)
        self.assertIsNone(self.cache.get('old'))
        self.cache.put('new', [{'id': 1, 'title': 'Made Up Harbor'}, {}])
        self.assertEqual(self.cache.get('new')[0]['id'], 1)
        self.assertEqual(self.cache.stats()['entries'], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.cache.close()
        self.assertEqual(self.cache.get('new')[0]['id'], 1)

    def test_stats_on_corrupt_image_returns_empty_cache(self):
        self.path.write_bytes(b'corrupt synthetic sqlite image' * 10)
        self.assertEqual(self.cache.stats()['entries'], 0)

    def test_failed_initialization_does_not_keep_connection(self):
        db = Mock()
        db.execute.side_effect = sqlite3.OperationalError('synthetic initialization failure')
        with patch('skald.search.sqlite3.connect', return_value=db):
            self.assertIsNone(self.cache.get('missing'))
        self.assertIsNone(self.cache.db)
        db.close.assert_called_once()
        self.cache.put('new', [{'id': 2}, {}])
        self.assertEqual(self.cache.get('new')[0]['id'], 2)


if __name__ == '__main__':
    unittest.main()
