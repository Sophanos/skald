#!/usr/bin/env python3
"""Measure command rules offline or command models with explicit live requests."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
import os
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from skald.commands import OPS, command_prompt, parse_json, register


def load_cases():
    cases = [json.loads(line) for line in (ROOT / 'eval/commands_cases.jsonl').read_text().splitlines() if line.strip()]
    for case in cases:
        case.setdefault('contract', 'now' if case['expect']['op'] in (*OPS, None) else 'next')
    return cases


def normalize(value):
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def theme_matches(expected, actual):
    if not isinstance(expected, str) or not isinstance(actual, str):
        return normalize(expected) == normalize(actual)
    words = re.findall(r'\w+', expected.casefold())
    found = set(re.findall(r'\w+', actual.casefold()))
    return sum(word in found for word in words) * 2 >= len(words) if words else normalize(expected) == normalize(actual)


def score(expect, answer):
    if not isinstance(answer, dict) or 'op' not in answer:
        return False, False
    op_ok = expect['op'] == answer['op']
    full_ok = op_ok
    for field in ('person', 'kind'):
        if field in expect:
            full_ok = full_ok and field in answer and normalize(expect[field]) == normalize(answer[field])
    if 'values' in expect:
        expected, actual = expect['values'], answer.get('values')
        values_ok = isinstance(actual, list) and len(expected) == len(actual)
        if values_ok:
            for i, (e, a) in enumerate(zip(expected, actual)):
                if expect['op'] == 'shelf_stop' or expect['op'] == 'shelf_add' and i == 0:
                    match = theme_matches(e, a)
                elif expect['op'] == 'shelf_add' and i in (1, 2):
                    match = isinstance(e, str) and isinstance(a, str) and normalize(e) == normalize(a)
                else:
                    match = normalize(e) == normalize(a)
                values_ok = values_ok and match
        full_ok = full_ok and values_ok
    return op_ok, full_ok


def synthetic_spec(data):
    # Fixed household, empty shelves and no server import or private DATA access.
    specs = {}
    s = SimpleNamespace(DATA=data, PEOPLE=['ana', 'ben'], LABELS={'ana': 'Ana', 'ben': 'Ben'},
                        HIDE_CHOICES=('movies', 'series', 'documentaries', 'animation', 'anime'),
                        PICK_MODEL='fixture', DAY=86400, route_norm=lambda x: x,
                        decision=lambda name, **spec: specs.update({name: spec}))
    register(s)
    return s, specs['parse_command']


def http_transport(url, payload, key, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def percentile(values, fraction):
    """Linearly interpolate between sorted samples, including timeout durations."""
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def summarize(model, cases, results):
    n = len(results)
    now = [r for r in results if r['contract'] == 'now']
    groups, tiers = {}, {}
    for case, result in zip(cases, results):
        group = groups.setdefault(case['group'], {'correct': 0, 'count': 0})
        group['count'] += 1
        group['correct'] += result['full_ok']
        tier = tiers.setdefault(case.get('tier', 'base'), [])
        if result['contract'] == 'now':
            tier.append(result)
    by_tier = {tier: dict(now_count=len(rows),
                          op_accuracy=sum(r['op_ok'] for r in rows) / len(rows) if rows else 0,
                          full_accuracy=sum(r['full_ok'] for r in rows) / len(rows) if rows else 0)
               for tier, rows in tiers.items()}
    cost = sum(r['cost'] for r in results)
    return dict(model=model, count=n,
                op_accuracy=sum(r['op_ok'] for r in results) / n if n else 0,
                full_accuracy=sum(r['full_ok'] for r in results) / n if n else 0,
                now_count=len(now),
                now_op_accuracy=sum(r['op_ok'] for r in now) / len(now) if now else 0,
                now_full_accuracy=sum(r['full_ok'] for r in now) / len(now) if now else 0,
                by_tier=by_tier, by_group=groups, timeouts=sum(r.get('error') == 'TimeoutError' for r in results),
                no_answer=sum(r.get('error') == 'no answer' for r in results),
                errors=dict(Counter(r['error'] for r in results if r.get('error') and r['error'] != 'no answer')),
                p50_ms=percentile([r['latency_ms'] for r in results], .5),
                p95_ms=percentile([r['latency_ms'] for r in results], .95),
                total_cost=cost, cost_per_1000=cost * 1000 / n if n else 0,
                failed_ids=[r['id'] for r in results if not r['full_ok']], results=results)


def evaluate(cases, models=None, *, transport=None, key='', base='https://openrouter.ai', timeout=4, clock=time.perf_counter):
    transport = transport or http_transport
    reports = []
    with tempfile.TemporaryDirectory(prefix='skald-command-eval-') as data:
        s, spec = synthetic_spec(data)
        for model_spec in models if models is not None else ['fixture']:
            model, effort = model_spec.rsplit('@', 1) if '@' in model_spec else (model_spec, 'none')
            label = f'{model}@{effort}' if models is not None else 'fixture'
            def run(case):
                start = clock() if models is not None else 0
                answer, error, cost = None, None, 0.0
                try:
                    if models is None:
                        result = spec['rule']({'query': case['text']}, {})
                        if result is None:
                            error = 'no answer'
                        else:
                            answer = result[0]
                    else:
                        payload = spec['llm']['prompt']([{'query': case['text']}])
                        payload.update(model=model, reasoning={'effort': effort},
                                       max_tokens=200 if effort == 'none' else 1200, usage={'include': True})
                        payload['messages'][0]['content'] = command_prompt(s)
                        reply = transport(base.rstrip('/') + spec['llm']['path'], payload, key, timeout)
                        cost = float((reply.get('usage') or {}).get('cost') or 0)
                        answer = parse_json(reply['choices'][0]['message']['content'])
                        if not isinstance(answer, dict) or 'op' not in answer or not (answer['op'] is None or isinstance(answer['op'], str)):
                            raise ValueError('invalid answer')
                except Exception as exc:
                    error = 'TimeoutError' if isinstance(exc, TimeoutError) or isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError) else type(exc).__name__
                    answer = None
                latency = (clock() - start) * 1000 if models is not None else 0.0
                op_ok, full_ok = score(case['expect'], answer)
                row = dict(id=case['id'], contract=case['contract'], tier=case.get('tier', 'base'), op_ok=op_ok, full_ok=full_ok, latency_ms=latency,
                           cost=cost, answer_op=answer.get('op') if answer is not None else None)
                if error:
                    row['error'] = error
                return row
            if models is None:
                results = [run(case) for case in cases]
            else:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(run, cases))
            reports.append(summarize(label, cases, results))
    return reports


def print_report(report, out):
    r = report
    print('model | all cases | all op accuracy | all full accuracy | now cases | now op accuracy | now full accuracy | no answer | timeouts | p50 ms | p95 ms | total USD | USD/1000', file=out)
    print(f"{r['model']} | {r['count']} | {r['op_accuracy']:.2%} | {r['full_accuracy']:.2%} | {r['now_count']} | {r['now_op_accuracy']:.2%} | {r['now_full_accuracy']:.2%} | {r['no_answer']} | {r['timeouts']} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['total_cost']:.6f} | {r['cost_per_1000']:.6f}", file=out)
    print('by-group full accuracy: ' + '; '.join(f"{g} {v['correct']}/{v['count']} ({v['correct']/v['count']:.2%})" for g, v in r['by_group'].items()), file=out)
    print('by-tier now accuracy: ' + '; '.join(
        f"{tier} {v['now_count']} cases op {v['op_accuracy']:.2%} full {v['full_accuracy']:.2%}"
        for tier, v in r['by_tier'].items()), file=out)
    print('errors: ' + (', '.join(f'{k}={v}' for k, v in sorted(r['errors'].items())) or 'none'), file=out)
    print('failed ids: ' + ', '.join(r['failed_ids']), file=out)


def main(argv=None, *, transport=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fixture', action='store_true', help='rule only, no network (default)')
    mode.add_argument('--live', action='store_true', help='measure models alone via OpenRouter')
    parser.add_argument('--models', help='comma-separated model or model@effort ids (default effort: none), required for --live')
    parser.add_argument('--timeout', type=float, default=4, help='request timeout in seconds (default: 4)')
    parser.add_argument('--json', type=Path, help='write reports and per-case results')
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    models = [m.strip() for m in (args.models or '').split(',') if m.strip()]
    if args.live and not models:
        parser.error('--live requires --models')
    if args.models and not args.live:
        parser.error('--models requires --live')
    key = (os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_KEY') or '') if args.live else ''
    if args.live and not key:
        print('Live mode requires OPENROUTER_API_KEY or OR_KEY in the environment.', file=out)
        return 1
    reports = evaluate(load_cases(), models if args.live else None, transport=transport,
                       key=key, base=os.environ.get('OPENROUTER_BASE', 'https://openrouter.ai'), timeout=args.timeout)
    for report in reports:
        print_report(report, out)
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
