#!/usr/bin/env python3
"""Fresh 1–6-member households: cache-only CSV intake, v1 POST and two offline feed passes."""
from contextlib import redirect_stdout, redirect_stderr
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from skald.members import rid
from eval.profile_golden import NOW
from decide_kit import close_server
from test_api import raw_request
from skald import members
from skald.__main__ import main
from skald.ingest import parse
from skald.ingest.resolve import Resolver
from history_kit import Reply, trakt_pages
from player_kit import player_fixture, jellyfin_transport


FIXTURE = ROOT / 'tests/fixtures/ingest/imdb-ratings.csv'


def smoke(count, setup=None, keyless=False):
    csv = setup is None and not keyless
    people = ['ana', 'ben', 'cy', 'dex', 'eli', 'flo'][:count]
    assert len(people) == count, 'member fixture count'
    fixture = json.loads((ROOT / 'tests/fixtures/rows/tmdb.json').read_text())
    titles = fixture.pop('titles')
    def details(kind, tid):
        title, year = ('The Matrix', '1999') if tid == 603 else ('Example series', '2009') if tid == 1419 else (titles[(tid-9000000) % len(titles)], '2025')
        documentary = bool(setup and 9100000 <= tid < 9100200)
        return {**fixture, 'id': tid, 'title' if kind == 'movie' else 'name': title,
                'release_date' if kind == 'movie' else 'first_air_date': year + '-01-01',
                'genres': [{'id': 99, 'name': 'Documentary'}] if documentary else [{'id': 28, 'name': 'Action'}],
                'genre_ids': [99] if documentary else [28], 'keywords': {'keywords': []},
                'credits': {'cast': [], 'crew': []}, 'networks': [], 'production_companies': [], 'status': 'Ended'}
    def tmdb(path, **params):
        if path.startswith('find/'):
            kind, tid = ('movie', 603) if path.endswith('tt0133093') else ('tv', 1419)
            return {'movie_results' if kind == 'movie' else 'tv_results': [details(kind, tid)]}
        if path.startswith('genre/'):
            return {'genres': [{'id': 28, 'name': 'Action'}, {'id': 99, 'name': 'Documentary'}]}
        kind = 'tv' if '/tv' in path or path.startswith('tv/') else 'movie'
        if re.fullmatch(r'(movie|tv)/[0-9]+', path):
            return details(kind, int(path.split('/')[1]))
        if path.startswith('collection/'):
            return {'parts': []}
        offset = 9100000 if setup and str(params.get('with_genres')) == '99' else 9000000
        data = [details(kind, offset + (100 if kind == 'tv' else 0) + i) for i in range(len(titles))]
        return {'results': data, 'cast': [], 'crew': []}
    def transport(req, **kwargs):
        url = urllib.parse.urlparse(req.full_url)
        if url.hostname == 'api.themoviedb.org':
            reply = tmdb(url.path.removeprefix('/3/'), **{k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
        elif url.hostname == 'graphql.anilist.co':
            reply = {'data': {'Page': {'media': []}}}
        elif url.hostname == 'openrouter.ai':
            assert not keyless, 'keyless replay called OpenRouter'
            body = json.loads(req.data)
            reply = {'choices': [{'message': {'content': ''}}],
                     'answers': {k: {'score': 1.} for k in body.get('questions', {})}, 'usage': {'cost': 0}}
        else:
            raise AssertionError('network forbidden in fresh replay')
        return io.BytesIO(json.dumps(reply).encode())
    with tempfile.TemporaryDirectory(prefix='skald-fresh-') as temp:
        data, state = Path(temp, 'data'), Path(temp, 'state')
        data.mkdir(); state.mkdir()
        cfg = members.fresh_config(people)
        cfg['region'].update(lang='en-US', tz='UTC')
        pages = None
        player_open = None
        if setup == 'A':
            cfg['history']['source'] = 'jellyfin'
            cfg['connectors']['jellyfin'] = {'kind': 'player', 'enabled': True, 'auth': 'env:FAKE_PLAYER_TOKEN',
                'options': {'url':'http://player:8096', 'user_id':'fixture-user', 'profile':'household'}}
            player = player_fixture('aiostreams-jellyfin')
            player['items'] = player['history_items']
            player_open = jellyfin_transport(player)
        elif setup == 'B':
            cfg['connectors']['trakt_api'] = {'kind': 'api', 'enabled': True, 'auth': 'env:FAKE_TRAKT_TOKEN', 'options': {}}
            pages = trakt_pages()
            for rows in pages.values():
                for row in rows:
                    for kind in ('movie', 'show'):
                        if kind in row:
                            row[kind]['genres'] = ['action']
        (data / 'skald.json').write_text(json.dumps(cfg))
        if csv:
            Resolver(tmdb, cache=state / 'resolve.json', namespace='snapshot').resolve(parse('imdb', FIXTURE))
        noise = io.StringIO()
        env = {**os.environ, 'DATA_DIR': str(data), 'TMDB_API': 'offline', 'OPENROUTER_API_KEY': 'fixture', 'API_PORT': '',
               'FAKE_PLAYER_TOKEN': 'synthetic-player-token',
               'FAKE_TRAKT_TOKEN': 'synthetic-token', 'TRAKT_CLIENT_ID': 'synthetic-client'}
        for key in ('SKALD_CONFIG', 'SKALD_MEMBERS', 'SKALD_LANG', 'SKALD_TZ', 'SKALD_COUNTRY'):
            env.pop(key, None)
        if keyless:
            env.pop('OPENROUTER_API_KEY', None)
        with patch.dict(os.environ, env, clear=True), patch('time.time', return_value=NOW), \
                patch('urllib.request.urlopen', transport), redirect_stdout(noise), redirect_stderr(noise):
            started = time.perf_counter()
            code = main(['--snapshot', str(data), '--state-dir', str(state), 'import', 'imdb', str(FIXTURE),
                         '--person', people[0], '--yes', '--json'], out=io.StringIO()) if csv else 0
            import_ms = (time.perf_counter()-started)*1000
            assert code == 0, 'CSV import failed'
            events = [json.loads(line) for line in (state / 'imports.jsonl').read_text().splitlines()] if csv else []
            assert len(events) == (2 if csv else 0), 'CSV import count'
            spec = importlib.util.spec_from_file_location('skald_fresh_server', ROOT / 'server.py')
            s = importlib.util.module_from_spec(spec)
            started = time.perf_counter()
            spec.loader.exec_module(s)
            s._connector_open = transport
            load_ms = (time.perf_counter()-started)*1000
            try:
                if events:
                    token = s.new_client('fixture-client', ['read', 'events'], people[0])
                    status, _, raw, _ = raw_request(s, '/api/v1/events', 'POST', {'stream': 'imports', 'events': events},
                                                    {'Authorization': 'Bearer ' + token})
                    assert status == 200 and json.loads(raw)['accepted'] == 2, 'v1 events intake'
                for name in ('ensure_facets', 'ensure_emb', 'tag_experiences'):
                    setattr(s, name, lambda *a, **kw: None)
                # Public metadata fixture supplies the ranking inputs without AI enrichment.
                for kind in ('movie', 'tv'):
                    for tid in ([603] if kind == 'movie' else [1419,1420]) + list(range(87000101,87000114)) + [9000000+(100 if kind == 'tv' else 0)+i for i in range(len(titles))]:
                        it = details(kind, tid)
                        s._facets[f'tmdb:{tid}'] = {'kind': kind, 'title': it.get('title') or it.get('name'),
                            'y': (it.get('release_date') or it.get('first_air_date'))[:4], 'g': ['Action'],
                            **({} if keyless else {'x': {'competence': 2, 'banter_duo': 2}})}
                s.load_state()
                poll_ms = 0
                if setup:
                    if player_open is not None:
                        s._connector_open = player_open
                    if pages is not None:
                        s._connector_open = lambda req, timeout: Reply(pages[urllib.parse.urlsplit(req.full_url).path])
                    started = time.perf_counter()
                    s.connectors_tick(NOW)
                    assert s._connector_pending, 'adapter was not scheduled'
                    s._connector_pending[2].result(timeout=3)
                    s.connectors_tick(NOW+.01)
                    poll_ms = (time.perf_counter()-started)*1000
                    cid = 'jellyfin' if setup == 'A' else 'trakt_api'
                    assert s._connectors[cid]['health']['state'] == 'ok', 'adapter health'
                    assert (data / ('history.json' if setup == 'A' else 'trakt.json')).is_file(), 'adapter output'
                    if setup == 'B':
                        assert not (data/'history.json').exists() and not (data/'watch.sqlite').exists(), 'unexpected player access'
                    assert s.hydra_cfg() is None, 'unexpected availability config'
                    metas = [{'id': 'tmdb:1'}, {'id': 'tmdb:2'}]
                    assert s.nzb_keep(metas,2) == metas, 'absent availability filtered'
                s._connector_open = transport
                retry, passes = {}, []
                for _ in range(2):
                    started = time.perf_counter()
                    s.feed_pass(s.local_now(), retry)
                    passes.append(round((time.perf_counter()-started)*1000, 3))
                manifest = s.manifest(s.LANG)
                assert manifest['name'] == 'Skald' and manifest['catalogs'], 'manifest branding'
                names = {row['id']: row['name'] for row in manifest['catalogs']}
                assert not any(rid('', s.CONFIG) + p in names for p in people), 'default member shelves'
                assert names[rid('week', s.CONFIG)] == '✨ This Week for ' + ' & '.join(p.title() for p in people), 'week label'
                rows = {r['id'] + ':' + r['type']: s.row_metas(r['id'], r['type'], s.LANG)
                        for r in manifest['catalogs'] if r['id'] != rid('search', s.CONFIG)}
                if keyless:
                    assert 'OPENROUTER_API_KEY' not in os.environ and not s.OR_KEY, 'fixture LLM key'
                    assert not s.read_history() and not s.read_trakt() and not s._profiles['household']['entries'], 'nonempty cold history'
                    experience_tags = sum(len(rec.get('x') or {}) for rec in s._facets.values())
                    assert experience_tags == 0, 'synthetic experience tags'
                assert rows[rid('foryou:tv', s.CONFIG)], 'empty For You'
                if setup is None:
                    assert not rows[rid('rewatch:tv', s.CONFIG)], 'owner rewatch seed'
                outputs = [manifest, rows, json.loads((data / 'taste.json').read_text()), json.loads((data / 'skald.json').read_text())]
                assert 'Germany' not in json.dumps(outputs), 'unexpected region'
                started = time.perf_counter()
                addon = s.addon_token()
                root = f'/{s.LANG}/p/{people[0]}/'
                get = lambda path: raw_request(s, path, handler=s.Handler)
                code, _, raw, _ = get('/' + addon + root + 'manifest.json')
                assert code == 200 and json.loads(raw)['catalogs'], 'token manifest'
                code, _, raw, _ = get('/' + addon + root + f'catalog/tv/{rid('foryou', s.CONFIG)}.json')
                token_metas = len(json.loads(raw)['metas'])
                assert code == 200 and token_metas, 'empty token catalog'
                assert get(root + 'manifest.json')[0] == 404, 'tokenless manifest'
                assert get(root + f'catalog/tv/{rid('foryou', s.CONFIG)}.json')[0] == 404, 'tokenless catalog'
                for person in (*people, 'together'):
                    scoped = f'/{addon}/{s.LANG}/p/{person}/'
                    code, _, raw, _ = get(scoped + 'manifest.json')
                    assert code == 200 and json.loads(raw)['catalogs'], 'scoped manifest'
                    code, _, raw, _ = get(scoped + f'catalog/tv/{rid('foryou', s.CONFIG)}.json')
                    assert code == 200 and json.loads(raw)['metas'], 'scoped For You'
                token_ms = round((time.perf_counter()-started)*1000, 3)
                out = {'members': count, 'catalogs': len(manifest['catalogs']), 'rows': len(rows),
                       'nonempty': sum(bool(xs) for xs in rows.values()), 'foryou': len(rows[rid('foryou:tv', s.CONFIG)]),
                       'rewatch': len(rows[rid('rewatch:tv', s.CONFIG)]), 'imports': len(events),
                       'load_ms': round(load_ms, 3), 'import_ms': round(import_ms, 3), 'feed_ms': passes,
                       'token_metas': token_metas, 'token_http_ms': token_ms, 'tokenless_404': 2}
                if keyless:
                    out.update(keyless=True, history=0, experience_tags=experience_tags,
                               popular=len(rows[rid('top10:movie', s.CONFIG)]))
                if setup:
                    out.update(setup=setup, poll_ms=round(poll_ms,3), history=len(s.read_history()), trakt=len(s.read_trakt()),
                               docs=sum(len(rows.get(cid+':tv',[])) for cid in s.DOCS))
                    assert out['docs'] > 0, 'empty documentary fixtures'
            finally:
                for thread in threading.enumerate():
                    if getattr(thread, '_target', None) is s.save_usage:
                        thread.join(2)
                close_server(s)
                members.configure()
            return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument('--connectors', action='store_true', help='offline setups A and B using the same metadata fixtures')
    modes.add_argument('--keyless', action='store_true', help='one CSV-less member, empty history, no LLM key or experience tags')
    args = ap.parse_args()
    print(json.dumps([smoke(1, keyless=True)] if args.keyless else
                     [smoke(2,setup) for setup in ('A','B')] if args.connectors else [smoke(n) for n in range(1,7)], sort_keys=True))
