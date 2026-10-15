#!/usr/bin/env python3
"""Private synthetic family demo. No real household inputs or external calls."""
import argparse
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import datetime as dt
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from decide_kit import close_server, load_server

NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc).timestamp()
START = dt.date(2026, 9, 8)


def denied(*args, **kwargs):
    raise AssertionError('network forbidden in synthetic family')


def files(data):
    return {p.relative_to(data): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in data.rglob('*') if p.is_file()}


def simulate(lang='en', *, as_json=False):
    with tempfile.TemporaryDirectory(prefix='skald-family-') as temp, ExitStack() as stack:
        data = Path(temp)
        env = {'PATH': os.defpath, 'DATA_DIR': temp, 'PYTHONDONTWRITEBYTECODE': '1'}
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch('time.time', return_value=NOW))
        stack.enter_context(patch('urllib.request.urlopen', denied))
        for name in ('connect', 'connect_ex'):
            stack.enter_context(patch.object(socket.socket, name, denied))
        stack.enter_context(patch.object(socket, 'getaddrinfo', denied))
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(redirect_stderr(io.StringIO()))
        from skald import members, plays, setup
        from skald.__main__ import main as skald_main
        from skald.migrate import ensure
        cfg = members.fresh_config(['ana', 'ben', 'kim', 'lena'])
        cfg['region'].update(lang={'en': 'en-US', 'de': 'de-DE'}.get(lang, 'sr-RS'), row_lang=lang)
        cfg['household'].update(guests=['lena'], away=['ben'], ages={'kim': 9})
        cfg['taste']['people']['kim']['prefs'] = {'kids_age': 9}
        cfg['history']['source'] = 'aiostreams_db'
        cfg['connectors']['aiostreams_db'] = dict(kind='player', enabled=True, auth=None,
            options=dict(path=str(data / 'watch.sqlite'), profiles={
                'c1234567': 'kim', 'a1234567': 'ana', 'b1234567': 'ben', 'f1234567': 'household'}))
        (data / 'skald.json').write_text(json.dumps(cfg))
        ensure(data)
        s = load_server(data)
        stack.callback(close_server, s)
        for name in ('tmdb', 'openrouter', '_connector_open', 'llm_sections'):
            stack.enter_context(patch.object(s, name, denied))
        stack.enter_context(patch.object(setup, 'server', side_effect=lambda *a, **kw: nullcontext(s)))
        old_members = {name: getattr(members, name) for name in ('CONFIG', 'CONFIG_SOURCE', 'CONFIG_ERROR')}
        old_people = members.PEOPLE[:]
        def restore_members():
            for name, value in old_members.items():
                setattr(members, name, value)
            members.PEOPLE[:] = old_people
        stack.callback(restore_members)

        def at(day, hour=18):
            return dt.datetime.combine(day, dt.time(hour), ZoneInfo('Europe/Berlin')).timestamp()

        days = [START + dt.timedelta(days=i) for i in range(35)]
        watching = [day for i, day in enumerate(days) if i < 10 or i >= 19 and day.weekday() >= 5]
        events = [('c1234567', at(day), False) for day in watching]
        events += [('c1234567', at(dt.date(2026, 10, 12), 2), False)]
        events += [('f1234567', at(START+dt.timedelta(days=i)), False) for i in (14, 16, 29)]
        events += [(login, at(START+dt.timedelta(days=i)), False)
                   for login in ('a1234567', 'b1234567') for i in (2, 21, 33)]
        events += [('c1234567', at(dt.date(2026, 9, 23)), True)] * s.BACKFILL_PLAYS
        rows = [[login, 'series' if bulk else 'movie', f'tmdb:{991000+i}', int(stamp*1000),
                 1, 'episode' if bulk else 'movie', 0, 100, 100, None, None, 0,
                 1 if bulk else None, i if bulk else None]
                for i, (login, stamp, bulk) in enumerate(events)]
        with sqlite3.connect(data / 'watch.sqlite') as con:
            con.execute('CREATE TABLE watch_state ('+', '.join(plays.COLUMNS)+')')
            con.executemany('INSERT INTO watch_state VALUES ('+','.join('?' for _ in plays.COLUMNS)+')', rows)
        assert plays.check(s, NOW)['path'] == 'db'
        assert plays.days_state(data) == {'kim': [day.isoformat() for day in watching]}
        # The same local metadata transport used by the child-read tests builds real attributed entries.
        def details(item):
            tid = int(item['id'].split(':')[-1])
            kind = 'movie' if item['type'] == 'movie' else 'tv'
            return kind, dict(id=tid, title='Invented Family Watch '+str(tid))
        with patch.object(s, 'tmdb_details', details):
            profile = s.build_profile(s.read_history(), [], remote=False)
        profile['built_at'] = NOW
        (data / 'profile.json').write_text(json.dumps(profile, default=lambda value: sorted(value)))
        s._profiles['household'] = profile

        commands = [f'{op} kim --person ana' for op in ('plays recent', 'plays around', 'taste align')]
        for op in ('plays recent', 'plays around', 'taste align'):
            commands.extend(f'{op} {target} --person {actor}' for actor, target in (
                ('lena', 'kim'), ('kim', 'kim' if op == 'plays around' else 'ana'), ('ben', 'kim'), ('ana', 'ben')))
        commands.append('members forget kim --person ana')
        results = []
        for i, command in enumerate(commands):
            before = files(data) if i == len(commands)-1 else None
            out, errors = io.StringIO(), io.StringIO()
            with redirect_stderr(errors):
                code = skald_main([*command.split(), *(['--json'] if as_json else [])], out=out)
            if before is not None:
                assert files(data) == before, 'forget preview changed synthetic data'
            assert code == (2 if 3 <= i < len(commands)-1 else 0)
            results.append({'command': command, 'exit': code, 'output': out.getvalue()+errors.getvalue()})
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lang', choices=('en', 'de', 'sr-latn', 'sr-cyrl'), default='en')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    results = simulate(args.lang, as_json=args.json)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, allow_nan=False))
    else:
        for result in results:
            print('python -m skald '+result['command'])
            print(result['output'], end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
