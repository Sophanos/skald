#!/usr/bin/env python3
"""Compare decision models and prompts over synthetic live router cases; key from env only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.decisions import live_cases, route_input, route_score
from eval.commands_eval import percentile
from decide_kit import load_server, close_server


def post(path, body, key, timeout):
    request = urllib.request.Request('https://openrouter.ai' + path, json.dumps(body).encode(),
                                    {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'X-Title': 'Skald eval'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def state(row):
    inp = route_input(row)
    if 'last' not in inp:
        return inp['query']
    shown = inp.get('shown') or []
    if isinstance(shown, str):
        shown = shown.split(', ')
    return f"Last search: {inp['last']}\nSome results shown: {', '.join(shown[:3])}\nTyped now: {inp['query']}"


def run(config, questions, cases, key):
    model, prompt = config
    def one(case):
        started = time.monotonic()
        answer, cost, error = {}, 0, None
        try:
            reply = post('/api/alpha/decisions', dict(model=model, state=state(case), questions=questions), key, 30)
            answer = {('choice' if k == 'route' else k): (v or {}).get('choice') for k, v in reply['answers'].items()}
            cost = (reply.get('usage') or {}).get('cost') or 0
        except Exception as exc:
            error = type(exc).__name__
        return dict(q=case['q'], lang=case['lang'], ok=route_score(case, answer), answer=answer,
                    ms=(time.monotonic() - started) * 1000, cost=cost, error=error)
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(model=model, prompt=prompt, rows=list(pool.map(one, cases)))


def summarize(rows):
    counts, hits = {}, {}
    for row in rows:
        for question, hit in row['ok'].items():
            counts[question] = counts.get(question, 0) + 1
            hits[question] = hits.get(question, 0) + hit
    n = len(rows)
    return dict(n=n, errors=sum(bool(r['error']) for r in rows), question_counts=counts,
                accuracy={q: hits[q] / count for q, count in counts.items()},
                all=sum(all(r['ok'].values()) for r in rows) / n if n else 0,
                p50_ms=percentile([r['ms'] for r in rows], .5), p90_ms=percentile([r['ms'] for r in rows], .9),
                cost=sum(r['cost'] for r in rows), cost_per_1000=1000 * sum(r['cost'] for r in rows) / n if n else 0)


def print_table(results, out):
    print('model | prompt | lang | n | errors | route | watched | list | previous | all | p50 ms | p90 ms | USD | USD/1000', file=out)
    for result in results:
        rows = result['rows']
        for lang in ('all', *sorted({r['lang'] for r in rows})):
            m = summarize(rows if lang == 'all' else [r for r in rows if r['lang'] == lang])
            scores = ' | '.join(f"{m['accuracy'][q]:.1%}" if q in m['accuracy'] else '-' for q in ('route', 'watched', 'list', 'previous'))
            print(f"{result['model']} | {result['prompt']} | {lang} | {m['n']} | {m['errors']} | {scores} | {m['all']:.1%} | {m['p50_ms']:.0f} | {m['p90_ms']:.0f} | {m['cost']:.6f} | {m['cost_per_1000']:.6f}", file=out)


def main(argv=None, *, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True, help='comma-separated decision model ids')
    parser.add_argument('--prompts', type=Path, help='JSON map of prompt names to decision questions; default: base and followup')
    parser.add_argument('--json', type=Path, help='optional report with per-case answers')
    args = parser.parse_args(argv)
    models = [m.strip() for m in args.models.split(',') if m.strip()]
    if not models:
        parser.error('--models must name at least one model')
    key = os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OR_KEY')
    if not key:
        print('Set OPENROUTER_API_KEY or OR_KEY in the environment.', file=out)
        return 1
    if args.prompts:
        prompts = json.loads(args.prompts.read_text())
    else:
        with tempfile.TemporaryDirectory() as data:
            server = load_server(data)
            try:
                prompts = dict(base=server.ROUTE_Q, followup=server.ROUTE_FOLLOWUP_Q)
            finally:
                close_server(server)
    results = [run((model, name), questions, live_cases(), key) for model in models for name, questions in prompts.items()]
    print_table(results, out)
    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
