"""Opt-in isolated, cache-only A/B scoring and home replay workers; Python 3.12 stdlib.

run_pair(source_a, source_b, snapshot, request, *, image, python_version,
         enabled=False) returns {code} or {code: OK, a, b, versions}.
Sources are complete public Python trees (server.py and skald/**/*.py).
Snapshot is caller-prepared, credential-free input, never a live DATA tree.
Accepts snapshot.freeze's file or directory, or flat allowlisted state files.
Symlinks are rejected.
request: {now: finite UTC seconds, seed: nonnegative int, timezone: IANA name,
          candidates: [[key, movie|tv], ...]}; candidates retain input order.
Optional lang, scope, who and order are passed to adapter.replay_home.
Snapshot includes skald.json, history.json, trakt.json, emb.json, facets.json,
and cache.json: {details: {history_id: [kind, TMDB detail]},
 tmdb: [{path: str, params: dict, value: cached JSON response}, ...]}.
Other production state and per-account history files are optional.
TMDB requests must exactly match cached path/params. Missing vectors, facets,
experience tags (x), resolutions or calibration inputs fail MISSING_INPUT.
Production build_profile/build_model/score_key run unchanged, including caps.
Worker output: {code: OK, entries: int, vector: bool, scores: [float|None],
                rows: final row_metas outputs, row_specs, rankings}.
Failures contain only fixed codes; process output is captured before imports.
Numeric per-candidate results stay local, not a share document.
versions hashes public source dependencies, evaluator and wave-1 protocol.

Requires a trusted locally installed immutable image (sha256 digest) with the
exact python_version, which must be 3.12.x. No pulls are allowed. Docker uses
network=none, read-only source/input/harness mounts, no host sockets or env,
a read-only root and a private writable scratch tmpfs for each process.
The private worker entry point is also unit tested directly on synthetic data;
that test path has NO sandbox guarantee. Audit hooks are tripwires only.
External generation remains disabled; no release gate is implied.
No import-time I/O. Configuration and timezone come only from staged inputs.
"""
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

FILES = {'skald.json', 'legacy.json', '.skald-format', 'history.json', 'trakt.json',
         'profile.json', 'taste.json', 'emb.json', 'facets.json', 'learn.json',
         'feedback.jsonl', 'imports.jsonl', 'online.json', 'memory.json',
         'interview.json', 'taste_ring.json', 'sessions.jsonl', 'cache.json',
         'feeds.json', 'shown.json', 'shown_scopes.json'}
HARNESS_FILES = ('runner.py', 'protocol.py', 'adapter.py')
CODES = {'MISSING_INPUT', 'WORKER_ERROR', 'PYTHON_VERSION', 'NETWORK_BLOCKED'}


class MissingInput(BaseException):
    pass


class NetworkBlocked(BaseException):
    pass


def _state(name):
    return name in FILES or bool(re.fullmatch(r'(trakt|mdblist|stremio)-[a-z][a-z0-9_]*\.history\.json', name))


def _copy(path, dest):
    if path.is_symlink() or not path.is_file():
        raise ValueError('INVALID_INPUT')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.parent.chmod(0o755)
    shutil.copyfile(path, dest)
    dest.chmod(0o444)


def source_hash(source):
    """Hash all shipped Python dependencies, including names, not private data."""
    source = Path(source)
    if source.is_symlink() or (source / 'skald').is_symlink():
        raise ValueError('INVALID_INPUT')
    source = source.resolve()
    paths = [source / 'server.py', *sorted((source / 'skald').rglob('*.py'))]
    digest = hashlib.sha256()
    for path in paths:
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError('INVALID_INPUT')
        digest.update(path.relative_to(source).as_posix().encode() + b'\0')
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


def _valid_output(value):
    if value == {'code': value.get('code')} and value['code'] in CODES:
        return value
    if (set(value) == {'code', 'entries', 'vector', 'scores', 'rows', 'row_specs', 'rankings'}
            and value['code'] == 'OK'
            and type(value['entries']) is int and value['entries'] >= 0
            and type(value['vector']) is bool and isinstance(value['scores'], list)
            and isinstance(value['rows'], list) and isinstance(value['row_specs'], list)
            and isinstance(value['rankings'], dict)
            and all(v is None or type(v) in (int, float) and math.isfinite(v)
                    for v in value['scores'])):
        return value
    raise ValueError('WORKER_ERROR')


def run_pair(source_a, source_b, snapshot, request, *, image='', python_version='', enabled=False):
    if enabled is not True:
        return {'code': 'DISABLED'}
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image) or not re.fullmatch(r'3\.12\.\d+', python_version):
        return {'code': 'INVALID_INPUT'}
    try:
        from eval.household.protocol import manifest, protocol_id
        from eval.household.snapshot import load
        with tempfile.TemporaryDirectory(prefix='skald-hh-') as temp:
            root = Path(temp)
            harness = root / 'harness'
            for name in HARNESS_FILES:
                _copy(Path(__file__).with_name(name), harness / 'eval' / 'household' / name)
            inputs = root / 'inputs'
            inputs.mkdir()
            snapshot = Path(snapshot)
            frozen = snapshot / 'snapshot.json' if snapshot.is_dir() else snapshot
            if frozen.is_file():
                for name, value in load(frozen)['inputs'].items():
                    if _state(name):
                        if name == 'skald.json':
                            value = {**value, 'history': {'source': 'file'}, 'connectors': {}}
                        content = ('\n'.join(json.dumps(row) for row in value)
                                   if name.endswith('.jsonl') else json.dumps(value))
                        (inputs / name).write_text(content, encoding='utf-8')
                        (inputs / name).chmod(0o444)
            else:
                for path in sorted(snapshot.iterdir()):
                    if _state(path.name):
                        _copy(path, inputs / path.name)
            versions = {'protocol': protocol_id(manifest()),
                        'evaluator': source_hash_evaluator(harness)}
            outputs = []
            for label, source in (('a', Path(source_a)), ('b', Path(source_b))):
                staged = root / label
                versions['build_' + label] = source_hash(source)
                source = source.resolve()
                for path in [source / 'server.py', *sorted((source / 'skald').rglob('*.py'))]:
                    _copy(path, staged / path.relative_to(source))
                command = ['docker', 'run', '--rm', '--pull=never', '--network=none',
                           '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                           '--user=65534:65534', '--pids-limit=128', '--ipc=none',
                           '--tmpfs=/scratch:rw,noexec,nosuid,size=128m,mode=0700,uid=65534,gid=65534',
                           '--env=PYTHONHASHSEED=' + str(request['seed']), '--entrypoint=python']
                for path, target in ((staged, '/source'), (inputs, '/input'), (harness, '/harness')):
                    command += ['--mount', f'type=bind,src={path},dst={target},readonly']
                command += [image, '-s', '-P', '/harness/eval/household/runner.py',
                            '--worker', '/source', '/input', '/scratch', python_version]
                proc = subprocess.run(command, input=json.dumps(request, allow_nan=False),
                                      text=True, capture_output=True, timeout=120,
                                      env={'PATH': os.environ.get('PATH', os.defpath)})
                if proc.returncode:
                    return {'code': 'BOUNDARY_UNAVAILABLE'}
                value = _valid_output(json.loads(proc.stdout))
                if value['code'] != 'OK':
                    return value
                outputs.append(value)
            return {'code': 'OK', 'a': outputs[0], 'b': outputs[1], 'versions': versions}
    except (OSError, subprocess.SubprocessError):
        return {'code': 'BOUNDARY_UNAVAILABLE'}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'code': 'INVALID_INPUT'}


def source_hash_evaluator(harness):
    return hashlib.sha256(b''.join((harness / 'eval' / 'household' / n).read_bytes()
                                  for n in HARNESS_FILES)).hexdigest()


def _evaluate(source, inputs, scratch, request):
    os.environ.clear()
    os.environ.update(DATA_DIR=str(scratch), SKALD_CONFIG=str(scratch / 'skald.json'),
                      TZ=request['timezone'], PYTHONDONTWRITEBYTECODE='1')
    time.tzset()
    random.seed(request['seed'])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    sys.path.insert(1, str(Path(__file__).resolve().parents[2]))
    from eval.household.adapter import replay_home
    if not all((inputs / name).is_file() for name in
               ('skald.json', 'history.json', 'trakt.json', 'emb.json', 'facets.json', 'cache.json')):
        raise MissingInput()
    for path in inputs.iterdir():
        if _state(path.name):
            shutil.copyfile(path, scratch / path.name)
    config = json.loads((scratch / 'skald.json').read_text())
    if config['region']['tz'] != request['timezone']:
        raise MissingInput()
    cache = json.loads((inputs / 'cache.json').read_text())
    spec = importlib.util.spec_from_file_location('household_worker_server', source / 'server.py')
    s = importlib.util.module_from_spec(spec)
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(request['now'], tz)
        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(request['now'], datetime.timezone.utc).replace(tzinfo=None)
    try:
        with patch('time.time', return_value=request['now']), patch('datetime.datetime', Frozen):
            spec.loader.exec_module(s)
            if s.CONFIG_ERROR:
                raise MissingInput()
            def deny(*args, **kwargs):
                raise NetworkBlocked()
            def tmdb(path, **params):
                for record in cache['tmdb']:
                    if record['path'] == path and record['params'] == params:
                        return record['value']
                raise MissingInput()
            def details(item):
                if item['id'] not in cache['details']:
                    raise MissingInput()
                return cache['details'][item['id']]
            def require(pairs, getter, tags=False):
                pairs = list(pairs)
                for key, kind in pairs:
                    value = getter(key, kind)
                    if value is None or tags and 'x' not in value:
                        raise MissingInput()
                return len(pairs)
            s.tmdb, s.tmdb_details = tmdb, details
            s.openrouter = s.anilist = s._connector_open = deny
            s.ensure_emb = lambda pairs: require(pairs, s.femb)
            s.ensure_facets = lambda pairs: require(pairs, s.frec)
            s.tag_experiences = lambda pairs, **kw: require(pairs, s.frec, True)
            s.load_state()
            history, trakt = s.read_history(), s.read_trakt()
            if any(record.get('error') for record in s._drop_status.values()):
                raise MissingInput()
            history = [item for item in history if item.get('profile', 'household') == 'household'
                       or s.CONFIG['history']['source'] in s.HISTORY_ADAPTERS
                       and item.get('profile') in s.PEOPLE]
            profile = s.build_profile(history, [], trakt)
            s._profiles['household'] = profile
            model = s.build_model(profile)
            candidates = request['candidates']
            require(candidates, s.frec)
            require(candidates, s.femb)
            s._model['m'] = model
            home = replay_home(s, now=request['now'], lang=request.get('lang', s.LANG),
                               scope=request.get('scope'), who=request.get('who'),
                               order=request.get('order'), enabled=True)
            return {'code': 'OK', 'entries': len(profile['entries']), 'vector': model['vec'] is not None,
                    'scores': [s.score_key(model, key, kind=kind)[0] for key, kind in candidates],
                    **{name: home[name] for name in ('rows', 'row_specs', 'rankings')}}
    finally:
        for name in ('_pool', '_outer', '_nzb_pool', '_connector_pool'):
            pool = getattr(s, name, None)
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)


def _entry():
    # Capture native fd output too, before any production import or error.
    output = os.dup(1)
    with open(os.devnull, 'w') as noise:
        os.dup2(noise.fileno(), 1)
        os.dup2(noise.fileno(), 2)
    def audit(event, args):
        if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.exec', 'os.posix_spawn'):
            raise NetworkBlocked()
    sys.addaudithook(audit)
    try:
        if sys.version_info[:2] != (3, 12) or '.'.join(map(str, sys.version_info[:3])) != sys.argv[5]:
            value = {'code': 'PYTHON_VERSION'}
        else:
            request = json.load(sys.stdin)
            if (type(request['now']) not in (int, float) or not math.isfinite(request['now'])
                    or type(request['seed']) is not int or not 0 <= request['seed'] <= 4294967295):
                raise MissingInput()
            value = _valid_output(_evaluate(*(Path(p) for p in sys.argv[2:5]), request))
    except MissingInput:
        value = {'code': 'MISSING_INPUT'}
    except NetworkBlocked:
        value = {'code': 'NETWORK_BLOCKED'}
    except BaseException:
        value = {'code': 'WORKER_ERROR'}
    with os.fdopen(output, 'w') as stream:
        stream.write(json.dumps(value, allow_nan=False) + '\n')


if __name__ == '__main__':
    _entry()
