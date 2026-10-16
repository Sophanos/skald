#!/usr/bin/env python3
"""Fresh-process snapshot peak RSS; --trace records allocations (with tracer RSS overhead)."""
import argparse
import os
from contextlib import redirect_stdout
import gc
import io
import json
from pathlib import Path
import resource
import sys
import tracemalloc

from replay import offline_server, profile_with, read_json, workspace_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    parser.add_argument('--trace', action='store_true')
    args = parser.parse_args()
    if args.trace:
        tracemalloc.start()
    with offline_server(Path(args.snapshot), workspace_output('eval/out/w2/memory')) as s:
        p = read_json(Path(args.snapshot) / 'profile.json')
        with redirect_stdout(io.StringIO()):
            m = s.build_model(profile_with(p, p['entries']))
        assert s._emb and all(len(v) == s.EMB_DIM for v in s._emb.values()) and m['aff']
        gc.collect()
        out = {'evidence': 'cached-profile', 'vectors': len(s._emb), 'storage': type(next(iter(s._emb.values()))).__name__,
               'peak_rss_mib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == 'darwin' else 1024),
               'vector_bytes': sum(sys.getsizeof(v) + (sum(map(sys.getsizeof, v)) if isinstance(v, list) else 0)
                                   for v in s._emb.values())}
        if args.trace:
            out['traced_current_mib'], out['traced_peak_mib'] = [v/2**20 for v in tracemalloc.get_traced_memory()]
        print(json.dumps(out, sort_keys=True))


if __name__ == '__main__':
    main()
