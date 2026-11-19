"""Run with python -m codemanager.retrieval_benchmark --repo . --repeat 3."""
import argparse
import json
import os
import re
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

from .retrieval_timing import file_search

CASES = [
    ('What does the UserPromptSubmit prompt_hook do?', 'codemanager/context.py'),
    ('How does stop_hook block completion for file length violations?', 'codemanager/codex_hooks.py'),
    ('How does commit_check validate staged embeddings?', 'codemanager/review.py'),
    ('How are changed files selected for file length checks?', 'codemanager/file_limits.py'),
    ('How are hybrid search BM25 and embeddings combined?', 'codemanager'),
]


def worker(repo):
    from .context import prompt_hook
    metrics = {}
    response = prompt_hook(repo, {'prompt': sys.stdin.read()}, timing=metrics)
    print(json.dumps({'metrics': metrics, 'response': response}))


def measure(repo, prompt):
    started = perf_counter()
    result = subprocess.run(
        [sys.executable, '-m', 'codemanager.retrieval_benchmark', '--repo', str(repo), '--worker'],
        input=prompt, capture_output=True, text=True, timeout=120,
        env={**os.environ, 'CODEMANAGER_TIMING': '0'})
    wall = round((perf_counter() - started) * 1000, 3)
    if result.returncode:
        return {'status': 'error', 'process_wall_ms': wall,
                'error': 'worker exit '+str(result.returncode)}
    value = json.loads(result.stdout)
    metrics = value['metrics']
    metrics['process_wall_ms'] = wall
    content = value['response'].get('hookSpecificOutput', {}).get('additionalContext', '')
    marker = re.search(r"^Retrieved evidence \(JSON; at most [0-9]+ serialized bytes\):\n", content, re.M)
    if marker:
        evidence = json.loads(content[marker.end():])
        metrics['paths'] = sorted({item['path'] for item in evidence.get('results', [])})
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if args.worker:
        worker(repo)
        return
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    rows = []
    for iteration in range(args.repeat):
        for prompt, expected in CASES:
            # Alternate order to reduce systematic filesystem cache advantage.
            if iteration % 2:
                proxy = file_search(repo, prompt)
                cm = measure(repo, prompt)
            else:
                cm = measure(repo, prompt)
                proxy = file_search(repo, prompt)
            row = {'iteration': iteration, 'prompt': prompt, 'codemanager': cm,
                   'rg_read_proxy': proxy,
                   'expected_path_found': {
                       'codemanager': any(expected in path for path in cm.get('paths', [])),
                       'rg_read_proxy': any(expected in path for path in proxy.get('paths', []))}}
            rows.append(row)
            print(json.dumps(row), flush=True)
    summary = {}
    for name in ('codemanager', 'rg_read_proxy'):
        successful = [row[name] for row in rows if row[name]['status'] == 'ok']
        summary[name] = {'successful': len(successful), 'runs': len(rows)}
        for field in ('total_ms', 'process_wall_ms'):
            values = [item[field] for item in successful if field in item]
            if values:
                summary[name]['median_'+field] = round(statistics.median(values), 3)
    report = {'limitations': 'rg_read_proxy is fixed keyword extraction plus bounded reads; '
              'it excludes Codex reasoning and adaptive searches. Bytes and relevance differ. '
              'First run is first observed, not a guaranteed cold cache. Workers are fresh processes.',
              'python': sys.executable, 'summary': summary, 'runs': rows}
    path = Path(args.output) if args.output else repo / '.codemanager' / 'retrieval-benchmark.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'report': str(path), 'summary': summary}), flush=True)


if __name__ == '__main__':
    main()
