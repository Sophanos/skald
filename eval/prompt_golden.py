#!/usr/bin/env python3
"""Offline outgoing OpenRouter body identity for every household LLM job."""
import argparse
import os
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.profile_golden import NOW
from eval.rows_report import home_server


def capture(snapshot, source):
    bodies = {}
    with redirect_stdout(io.StringIO()), patch('time.time', return_value=NOW), home_server(snapshot, source) as s:
        s.OR_KEY = 'fixture'
        s._profiles['household'].setdefault('searches', [])
        def transport(req, **kwargs):
            assert req.full_url.startswith('https://openrouter.ai/'), 'network forbidden in prompt replay'
            body = json.loads(req.data)
            bodies.setdefault(job, []).append(req.data)
            reply = {'answers': {k: {'score': 1.} for k in body.get('questions', {})},
                     'choices': [{'message': {'content': ''}}], 'usage': {'cost': 0, 'total_tokens': 0}}
            return io.BytesIO(json.dumps(reply).encode())
        with patch('urllib.request.urlopen', transport), patch.object(s, '_connector_open', transport, create=True):
            m = s.get_model(False)
            s._learn['moodpicks'].clear()
            job = 'mood'
            s.pick_moods(m, dict.fromkeys(s.MOODS, 0.), list(s.MOODS)[:len(s.MOODS_ROWS)])
            for job, build in (('week', s.week_feed), ('personal', s.personal_feed), ('docs_ai', s.docs_ai_feed)):
                build('de-DE')
        for thread in threading.enumerate():
            if getattr(thread, '_target', None) is s.save_usage:
                thread.join(2)
        s.flush_decisions()
        s._decide_queue.put(False)
        s._decide_queue.join()
    assert set(bodies) == {'mood', 'week', 'personal', 'docs_ai'}, 'missing LLM request'
    assert all(len(values) == 1 for values in bodies.values()), 'unexpected LLM request count'
    return {job: {'calls': len(values), 'sha256': hashlib.sha256(b''.join(values)).hexdigest()}
            for job, values in bodies.items()}


def report(snapshot, before, after):
    old, new = capture(snapshot, before), capture(snapshot, after)
    return {'before': old, 'after': new, 'identical': old == new}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot', type=Path, default=os.environ.get('SKALD_SNAPSHOT') or None, required=not os.environ.get('SKALD_SNAPSHOT'))
    ap.add_argument('--before', type=Path, required=True)
    ap.add_argument('--after', type=Path, default=ROOT / 'server.py')
    args = ap.parse_args()
    out = report(args.snapshot, args.before, args.after)
    print(json.dumps(out, sort_keys=True))
    raise SystemExit(0 if out['identical'] else 1)
