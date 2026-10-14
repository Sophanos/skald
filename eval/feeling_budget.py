#!/usr/bin/env python3
"""Synthetic Home, member, mood and leap build cost, with fake transports."""
from collections import Counter
from contextlib import ExitStack, redirect_stdout
import datetime
import gc
import io
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from decide_kit import close_server, load_server
from skald import members
from test_w111_pool import catalog, model


def measure(n, viewers):
    people = ('ana', 'ben', 'cal', 'dee')[:viewers]
    with tempfile.TemporaryDirectory() as data, ExitStack() as stack:
        cfg = members.fresh_config(people)
        cfg['household']['member_rows'] = list(people)
        Path(data, 'skald.json').write_text(json.dumps(cfg))
        s = load_server(data)
        stack.callback(close_server, s)
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        s._facets.update(catalog(n))
        feelings = list(s.EXPERIENCES)
        for i, rec in enumerate(s._facets.values()):
            rec['x'] = {feelings[(i+j) % len(feelings)]: 3 for j in range(3)} if i % 4 else {}
        p = {'entries': [{'key': 'tmdb:1', 'id': 1, 'kind': 'movie', 'stype': 'movie', 'weight': 2,
                          'title': 'Invented anchor', 'signal': 'loved', 'by': list(people),
                          'age': 0, 'who': 'both', 'item': {}}],
             'seen': {'tmdb:1'}, 'negative': set(), 'searches': [], 'likes': []}
        s._facets['tmdb:1'] = {'kind': 'movie', 'title': 'Invented anchor', 'x': {'mind_bender': 3}}
        m = model(n)
        m['holders'] = {'x:mind_bender': [(2, 'tmdb:1')]}
        m['paff'] = {who: m['aff'] if i % 2 else model(n)['paff']['ana'] for i, who in enumerate(people)}
        m['nexp'] = {**{who: 50 for who in people}, 'household': 50 * viewers}
        m['pbg'] = {who: m['bg'] for who in people}
        s._profiles['household'], s._model['m'] = p, m
        s._learn['served'] = {f'tmdb:{100+i}': {'name': f'Invented shown {i}',
            'rows': {s.MOODS_ROWS[0]: {'days': ['2026-10-11', '2026-10-12', '2026-10-13']}}} for i in range(4000)}
        def llm(path, body, timeout, lane, name):
            assert name == 'theme_source'
            inp = json.loads(body['messages'][1]['content'])
            keep = list(range(len(inp['candidates'])))
            return {'choices': [{'message': {'content': json.dumps({'fits_now': True, 'keep': keep, 'add': [],
                    'why': {str(i): 'An invented puzzle changes how its world works.' for i in keep}})}}]}
        for name, value in {'get_model': m, 'with_art': ([], {}), 'tmdb': {'results': []}, 'genres': {},
                            'pick_moods': ['ideas'], 'llm_sections': {}, 'profile_text': 'Invented taste',
                            'taste_hint': 'Invented taste', 'OR_KEY': 'fake', 'connectors_tick': None,
                            'save_feeds': None, 'aio_rows_changed': None,
                            'local_now': datetime.datetime(2026, 10, 14, 18)}.items():
            stack.enter_context(patch.object(s, name, return_value=value) if callable(getattr(s, name)) else
                                patch.object(s, name, value))
        stack.enter_context(patch.object(s, 'openrouter', side_effect=llm))
        for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
            stack.enter_context(patch.object(s, name, side_effect=lambda pairs, **kw: assert_empty(pairs)))
        rank = stack.enter_context(patch.object(s, 'known_rank', wraps=s.known_rank))
        jobs = {'personal', 'moods', 'leap', *people}
        def build():
            s._feeds.clear()
            s._theme_sources.clear()
            s._row_source_status.clear()
            s._decide_cache.clear()
            s._usage.update(day='', month='', calls={}, month_cost=0, cost=0, tokens=0, month_calls=0)
            rank.reset_mock()
            s.build_feeds(s.local_now(), {}, {}, only=jobs, force=True)
            assert Counter(c.args[2] for c in rank.call_args_list) == {None: 1, **{who: 1 for who in people}}
            assert s._feeds[s.MOODS_ROWS[0], s.LANG]['names']['candidate_sources']['catalog'] > 0
        elapsed = []
        for _ in range(3):
            gc.collect()
            start = time.perf_counter()
            build()
            elapsed.append(time.perf_counter() - start)
        gc.collect()
        tracemalloc.start()
        build()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return {'known_titles': n, 'viewers': viewers, 'shown_records': len(s._learn['served']),
                'median_seconds': round(statistics.median(elapsed), 3),
                'peak_build_bytes': peak, 'rank_passes': rank.call_count}


def assert_empty(pairs):
    assert not pairs, 'stored catalog must not be enriched'


if __name__ == '__main__':
    for n in (4000, 20000):
        for viewers in (1, 4):
            print(json.dumps(measure(n, viewers)), flush=True)
