#!/usr/bin/env python3
"""Read-only pool aggregates: python eval/pool_report.py --data PATH [--tmdb-cache JSON]."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skald.engine import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--tmdb-cache', type=Path, help='JSON object of TMDB paths to response objects, in English')
    args = parser.parse_args()
    cache = json.loads(args.tmdb_cache.read_text()) if args.tmdb_cache else None
    if cache is not None and (not isinstance(cache, dict) or any(not isinstance(v, dict) or not isinstance(v.get('results'), list)
                                                              for v in cache.values())):
        parser.error('TMDB cache must map paths to objects with results lists')

    def tmdb(path, **params):
        if path.startswith('genre/'):
            return {'genres': []}
        if path not in cache:
            raise LookupError('endpoint unavailable in offline cache')
        return cache[path]

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), Engine(args.data, offline=True) as engine:
        report = engine.pool(tmdb if cache is not None else None)
    report.pop('seeds')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
