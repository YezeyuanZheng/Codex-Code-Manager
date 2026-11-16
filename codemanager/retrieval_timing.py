"""Local retrieval timings and a lexical file-search proxy for comparison."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from time import perf_counter


@contextmanager
def span(metrics, name):
    started = perf_counter()
    try:
        yield
    finally:
        metrics[name] = round((perf_counter() - started) * 1000, 3)


def file_search(repo, prompt, *, budget=6000, limit=8):
    """Measure rg discovery plus bounded reads, excluding model decision time."""
    started = perf_counter()
    stopwords = set('a an and are as at be by can do does for from how i in is it '
                    'of on or that the this to what when where which with'.split())
    words = list(dict.fromkeys(w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z_0-9]*', prompt)
                               if w.lower() not in stopwords))[:12]
    metrics = {'status': 'ok', 'hits': 0, 'result_bytes': 0, 'paths': []}
    try:
        if not words:
            return metrics
        command = ['rg', '-n', '-i', '-F', '--no-heading', '--color', 'never', '-m', '1']
        for word in words:
            command.extend(['-e', word])
        command.append('.')
        # Disk-backed output keeps memory bounded even for a broad query.
        import tempfile
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(command, cwd=repo, stdout=output, stderr=subprocess.PIPE,
                                    timeout=10)
            if result.returncode not in (0, 1):
                raise ValueError('rg exited with code '+str(result.returncode))
            output.seek(0)
            for raw in output:
                try:
                    path, line, _ = raw.decode('utf-8').split(':', 2)
                    path = Path(repo) / path
                    if path.is_symlink() or not path.resolve().is_relative_to(Path(repo).resolve()):
                        continue
                    number = int(line)
                    snippet = []
                    with path.open(encoding='utf-8') as source:
                        for index, text in enumerate(source, 1):
                            if index >= max(1, number - 6):
                                snippet.append(text)
                            if index >= number + 12:
                                break
                    size = len(''.join(snippet).encode())
                except (OSError, ValueError, UnicodeError):
                    continue
                metrics['result_bytes'] += min(size, budget - metrics['result_bytes'])
                metrics['hits'] += 1
                metrics['paths'].append(path.relative_to(repo).as_posix())
                if metrics['hits'] >= limit or metrics['result_bytes'] >= budget:
                    break
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        metrics.update(status='error', error=type(exc).__name__)
    finally:
        metrics['total_ms'] = round((perf_counter() - started) * 1000, 3)
    return metrics


def record_timing(repo, prompt, metrics):
    """Best-effort logging; never change the hook's JSON or completion behavior."""
    if os.environ.get('CODEMANAGER_TIMING', '1') == '0':
        return
    try:
        started = perf_counter()
        baseline = file_search(repo, prompt)
        row = {'schema': 1, 'timestamp': datetime.now(timezone.utc).isoformat(),
               'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
               'codemanager': dict(metrics), 'rg_read_proxy': baseline}
        path = Path(repo) / '.codemanager' / 'retrieval-timings.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        row['logging_overhead_ms'] = round((perf_counter() - started) * 1000, 3)
        with path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
    except (OSError, ValueError, TypeError):
        pass
