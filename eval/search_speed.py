#!/usr/bin/env python3
"""Offline latency benchmark. --before accepts an untouched server.py copy."""
import argparse
from concurrent.futures import Future
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from decide_kit import close_server, load_server
from test_w110_search import SearchFallbackTests
from test_w111_speed import TimedSSE
from skald.search import ResolutionCache

CLOCK = time.monotonic
TITLE = [.13, .18, .24, .31, .36, .42, .49, .55, .62, .67, .71, .74, .77, .79, .8, .82]
PICKER = [2.5, 2.55, 2.58, 2.6, 2.65, 2.7, 2.8, 2.9, 3, 3.1, 3.2, 3.35, 3.5, 3.65, 3.8, 4]
RESOLVE = [.18, .19, .2, .21, .22, .23, .238, .238, .25, .3, .4, .7, 1, 1.4, 1.776, 2.5]


def summary(values):
    return {'p50_s': round(statistics.median(values), 3), 'worst_s': round(max(values), 3)}


def run(before=None, scale=.1, serve_after=2.7, minimum=15):
    times, repeats, sizes = [], [], []
    with tempfile.TemporaryDirectory() as directory:
        if before:
            spec = importlib.util.spec_from_file_location('speed_before', before)
            s = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {'DATA_DIR': directory, 'TMDB_API': 'offline', 'OPENROUTER_API_KEY': '', 'API_PORT': ''}), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
                spec.loader.exec_module(s)
        else:
            s = load_server(directory)
        if not hasattr(s, '_search_pool'):
            s._search_pool = s.BuildPool(max_workers=8)
        try:
            for i, (title, picker, resolve) in enumerate(zip(TITLE, PICKER, RESOLVE)):
                h = SearchFallbackTests()
                h.s = s
                h.setUp()
                stype, code = ('movie', 'm') if i % 2 == 0 else ('series', 's')
                lines = [f'Title {1000 + i * 100 + j}|2000|{code}' for j in range(30)]
                first = .45 + .4 * i / 15
                origin = CLOCK()
                result_wait, event_wait = Future.result, threading.Event.wait
                def reply(req, timeout):
                    body = json.loads(req.data)
                    if body.get('stream'):
                        return TimedSSE(lines, first * scale, (picker - first) / 29 * scale,
                                        timeout=timeout * scale, clock=CLOCK)
                    if 'messages' not in body:
                        time.sleep((.64 + .58 * i / 15) * scale)
                    elif not body['messages'][0]['content'].startswith('Extract only'):
                        time.sleep((picker if '30 lines' in body['messages'][0]['content'] else 2) * scale)
                        return io.BytesIO(json.dumps({'choices': [{'message': {'content': '\n'.join(lines)}}]}).encode())
                    return h.reply(req, timeout)
                def tmdb(path, **params):
                    if path == 'search/multi':
                        time.sleep(title * scale)
                    query = params.get('query', '')
                    if path in ('search/movie', 'search/tv') and query.startswith('Title '):
                        index = int(query[6:]) % 100
                        time.sleep((resolve if index == 5 else .06) * scale)
                    return h.tmdb_reply(path, **params)
                def settle(query, wait):
                    time.sleep(wait * scale)
                    return False
                try:
                    with patch.object(s.time, 'monotonic', side_effect=lambda: (CLOCK() - origin) / scale), \
                            patch.object(Future, 'result', lambda f, timeout=None: result_wait(f, None if timeout is None else timeout * scale)), \
                            patch.object(threading.Event, 'wait', lambda e, timeout=None: event_wait(e, None if timeout is None else timeout * scale)), \
                            patch.object(s, '_connector_open', side_effect=reply), \
                            patch.object(s, 'tmdb', side_effect=tmdb), patch.object(s, 'superseded', side_effect=settle), \
                            patch.object(s, '_model', {'m': None}), \
                            patch.object(s, 'SEARCH_SERVE_AFTER', serve_after, create=True), \
                            patch.object(s, 'SEARCH_SERVE_MIN', minimum, create=True):
                        start = CLOCK()
                        metas = h.results(query=f'a quiet synthetic wish {i}', stype=stype)
                        times.append((CLOCK() - start) / scale)
                        sizes.append(len(metas))
                        start = CLOCK()
                        h.results(query=f'a quiet synthetic wish {i}', stype=stype)
                        repeats.append((CLOCK() - start) / scale)
                        h.finish_jobs()
                finally:
                    h.doCleanups()
            assert all(size == 15 for size in sizes), sizes
            assert max(times) < s.SEARCH_TIMEOUT, times
            return {'uncached': summary(times), 'repeat': summary(repeats), 'rows': sizes,
                    'runs': len(times), 'scale': scale, 'serve_after_s': serve_after, 'minimum': minimum}
        finally:
            if hasattr(s, '_search_resolutions'):
                s._search_resolutions.close()
            close_server(s)


def cache_size():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    with tempfile.TemporaryDirectory() as directory:
        cache = ResolutionCache(Path(directory) / 'resolve.sqlite3')
        for i in range(5000):
            item = {'id': i + 1, 'title': f'Synthetic Harbor {i}', 'original_title': f'Synthetic Harbor {i}',
                    'original_language': 'en', 'release_date': '2000-01-01', 'genre_ids': [18, 35],
                    'overview': 'A made-up household story. ' * 16, 'poster_path': '/synthetic.jpg',
                    'backdrop_path': '/synthetic-wide.jpg', 'vote_count': 200, 'vote_average': 7.2, 'adult': False}
            cache.put(json.dumps((item['title'].casefold(), '2000', 'movie', 'en-US')), (item, item))
        stats = cache.stats()
        assert stats['entries'] == 5000, stats
        stats['sqlite_page_cache_limit_bytes'] = 1024 * 1024
        stats['python_current_bytes'], stats['python_peak_bytes'] = tracemalloc.get_traced_memory()
        added = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss
        stats['process_peak_added_bytes'] = added * (1 if sys.platform == 'darwin' else 1024)
        tracemalloc.stop()
        cache.close()
        return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--scale', type=float, default=.1)
    parser.add_argument('--serve-after', type=float, default=2.7)
    parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    if args.cache_only:
        print(json.dumps(cache_size()))
        return
    report = {}
    if args.before:
        report['before'] = run(args.before, args.scale)
    report['overlap'] = run(scale=args.scale, minimum=31)
    report['after'] = run(scale=args.scale, serve_after=args.serve_after)
    raw = subprocess.check_output([sys.executable, str(Path(__file__)), '--cache-only'], text=True)
    report['cache_5000'] = json.loads(raw)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
