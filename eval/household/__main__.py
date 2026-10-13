"""Explicit diagnostics; import performs no work. Owners pause writers: no write fence.
Trusted local workers have no historical journals and use
network tripwires, not Docker isolation. Release approval and tuning stay off.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

if __name__ == '__main__' and sys.argv[1:2] == ['--worker']:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.household import export, metrics, protocol, runner, snapshot, splits, uncertainty

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('INVALID_ARGUMENT')

def _worker():
    # Capture native output before importing either source tree.
    output = os.dup(1)
    with open(os.devnull, 'w') as noise:
        os.dup2(noise.fileno(), 1)
        os.dup2(noise.fileno(), 2)
    attempts = []
    def audit(event, args):
        if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.exec', 'os.posix_spawn'):
            attempts.append(1)
            raise runner.NetworkBlocked()
    sys.addaudithook(audit)
    try:
        from unittest.mock import patch
        import random
        from eval.household import adapter
        source = Path(sys.argv[2])
        sys.path.insert(0, str(source))
        from skald.engine import Engine
        request = json.load(sys.stdin)
        random.seed(protocol.SAMPLING_SEED)
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            os.environ['DATA_DIR'] = temp
            for name, value in request['inputs'].items():
                if name in snapshot.INPUTS:
                    value = {**value, 'history': {'source': 'file'}, 'connectors': {}} if name == 'skald.json' else value
                    text = '\n'.join(json.dumps(v) for v in value) if name.endswith('.jsonl') else json.dumps(value)
                    (data / name).write_text(text, encoding='utf-8')
            rows = []
            for origin in request['origins']:
                with patch('time.time', return_value=origin), Engine(data, source / 'server.py') as engine:
                    for member in request['members']:
                        home = adapter.replay_home(engine._server, now=origin, who=member,
                                                   scope=member, enabled=True)
                        rows.append({'specs': home['row_specs'], 'rankings': home['rankings']})
            value = {'code': 'NETWORK_BLOCKED'} if attempts else {'code': 'OK', 'homes': rows}
    except BaseException:
        value = {'code': 'WORKER_ERROR'}
    with os.fdopen(output, 'w') as stream:
        stream.write(json.dumps(value, allow_nan=False) + '\n')

def _replay(source, request):
    proc = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                           '--worker', str(source)], input=json.dumps(request),
                          capture_output=True, text=True, timeout=120,
                          env={'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'})
    value = json.loads(proc.stdout)
    if proc.returncode or value.get('code') != 'OK':
        raise ValueError('WORKER_ERROR')
    return value['homes']

def _macro(frame):
    cells = sorted({c for w in frame for c in w['cells']})
    pair = []
    for side in (0, 1):
        scope_scores = []
        for cell in cells:
            weeks = []
            for week in frame:
                values = {t['id']: t['values'][cell][side] for t in week['targets'] if cell in t['values']}
                if values:
                    weeks.append(sum(values.values()) / len(values))
            if not weeks:
                return None
            scope_scores.append(sum(weeks) / len(weeks))
        pair.append(sum(scope_scores) / len(scope_scores) if scope_scores else None)
    return tuple(pair)

def _compare(args):
    target = Path(args.snapshot)
    target = target / 'snapshot.json' if target.is_dir() else target
    provenance = snapshot.evaluate(target)
    doc = json.loads(target.read_text(encoding='utf-8'))
    if doc['digest'] != protocol.protocol_id({k: v for k, v in doc.items() if k != 'digest'}):
        raise ValueError('INVALID_INPUT')
    state = doc['inputs']
    config = state['skald.json']
    members = config['household']['members']
    if not members or not all(name in state for name in ('history.json', 'facets.json', 'emb.json')):
        raise ValueError('MISSING_INPUT')
    # CLI generation is an intake clock marker, not a production write fence.
    as_of = datetime.fromtimestamp(doc['generation'], timezone.utc)
    dated = [e for e in provenance['targets']['events'] if e['member'] in members]
    folds = [f for f in splits.weekly_folds(dated, as_of, timezone=config['region']['tz']) if f.phase == 'outer']
    request = {'inputs': state, 'members': members, 'origins': [f.origin.timestamp() for f in folds]}
    versions = {'build_a': runner.source_hash(args.a), 'build_b': runner.source_hash(args.b),
                'protocol': protocol.protocol_id(protocol.manifest()),
                'evaluator': hashlib.sha256(b''.join(p.read_bytes() for p in
                    sorted(Path(__file__).parent.glob('*.py')))).hexdigest()}
    a, repeat, b = _replay(args.a, request), _replay(args.a, request), _replay(args.b, request)
    if versions['build_a'] != runner.source_hash(args.a) or versions['build_b'] != runner.source_hash(args.b):
        raise ValueError('SOURCE_CHANGED')
    frame, scores = [], [[], []]
    cells = [(m, 'home') for m in members]
    for i, fold in enumerate(folds):
        targets = []
        for j, member in enumerate(members):
            prior = {e['identity'] for e in fold.train if e['playback']}
            labels = sorted({e['identity'][1] for e in fold.labels if e['member'] == member
                             and e['playback'] and not e['rewatch'] and e['identity'] not in prior})
            recovered = []
            for side, homes in enumerate((a, b)):
                home = homes[i * len(members) + j]
                scores[side].append(metrics.evaluate_week(('individual', member), str(i),
                    {'discovery': labels}, home['specs'], home['rankings']))
                footprint = set()
                for spec in [s for s in home['specs'] if s['task'] == 'discovery'][:protocol.HOME_ROWS]:
                    footprint.update(home['rankings'][spec['id']][:
                        protocol.TOP10_CAP if spec['top10'] else protocol.HOME_CARDS])
                recovered.append(footprint)
            targets.extend({'id': title, 'franchise': None,
                            'values': {cells[j]: tuple(int(title in r) for r in recovered)}} for title in labels)
        frame.append({'week': str(i), 'mature': True, 'cells': cells, 'targets': targets})
    home = uncertainty.analyze(frame, _macro)
    evidence = protocol.result(protocol.manifest(), protocol.DIAGNOSTIC,
                               required_components=('home', 'historical_inputs'), supported_components=('home',))
    gate = protocol.compare(evidence, evidence)
    verdict = 'INSUFFICIENT' if not gate['release_pass'] else 'INCONCLUSIVE'
    if a != repeat:
        verdict = 'HARD FAIL'
    aggregate = metrics.summarize(scores[1], [('individual', m) for m in members])['tasks']['discovery']['score']
    report = {'verdict': verdict, 'evidence_level': protocol.DIAGNOSTIC, 'aa_identical': a == repeat,
              'release_pass': False, 'home': home, 'recovery': aggregate, 'ab_identical': a == b,
              'coverage_failures': gate['coverage_failures'], 'versions': versions,
              'limitations': ['current_features_and_state', 'no_writer_fence', 'local_worker_not_sandboxed',
                              'no_historical_eligibility', 'unknown_attendance', 'tuning_disabled']}
    home['rows'] = {json.dumps(k): v for k, v in home['rows'].items()}
    out = Path(args.out)
    if out.resolve().is_relative_to(target.parent.resolve()):
        raise ValueError('INVALID_OUTPUT')
    out.mkdir(mode=0o700)
    with (out / 'report.json').open('x', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, sort_keys=True, allow_nan=False)
    export.write_share(out, {'status': 'ok' if a == repeat else 'error', 'versions': versions,
                            'metrics': {'home_recovery_delta': home}}, enabled=True)
    print(verdict, protocol.DIAGNOSTIC, home['support']['weeks'], home['support']['targets'])
    return 1 if verdict == 'HARD FAIL' else 2

def main(argv=None):
    try:
        parser = Parser(prog='python3.12 -m eval.household', allow_abbrev=False)
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        for name, flags in (('freeze', ('data', 'out')), ('compare', ('snapshot', 'a', 'b', 'out')),
                            ('check-share', ())):
            command = commands.add_parser(name, allow_abbrev=False)
            for flag in flags:
                command.add_argument('--' + flag, required=True)
            if name == 'check-share':
                command.add_argument('file')
        args = parser.parse_args(argv)
        if args.command == 'compare':
            return _compare(args)
        if args.command == 'freeze':
            if Path(args.out).resolve().is_relative_to(Path(args.data).resolve()):
                raise ValueError('INVALID_OUTPUT')
            marker = int(time.time())
            snapshot.freeze(args.data, args.out, inputs=list(snapshot.INPUTS),
                            generation=lambda: marker, enabled=True)
            print('OK', protocol.DIAGNOSTIC)
        else:
            export.validate_share(json.loads(Path(args.file).read_text(encoding='utf-8')))
            print('OK')
        return 0
    except Exception:
        print('HARD FAIL', protocol.DIAGNOSTIC)
        return 1

if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        _worker()
    else:
        raise SystemExit(main())
