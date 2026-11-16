"""Repository-wide file length policy, independent of optional code review."""
import json
import os
from pathlib import Path
import subprocess

from .config import read_config
from .snapshot import atomic_write

DEFAULT_MAX_LINES = 500
DEFAULT_MAX_CHARS = 20000


def positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'file_limits.{name} must be a positive integer')
    return value


def from_config(data):
    value = data.get('file_limits', {})
    if not isinstance(value, dict):
        raise ValueError('file_limits must be an object')
    # Read old settings until the next file-limits command migrates them.
    legacy = data.get('review', {})
    legacy = legacy if isinstance(legacy, dict) else {}
    return {'max_lines': positive(value.get('max_lines', legacy.get('max_file_lines', DEFAULT_MAX_LINES)), 'max_lines'),
            'max_chars': positive(value.get('max_chars', legacy.get('max_file_chars', DEFAULT_MAX_CHARS)), 'max_chars')}


def settings(repo):
    return from_config(read_config(repo))


def configure(repo, max_lines=None, max_chars=None):
    repo = Path(repo).resolve(strict=True)
    data = read_config(repo)
    policy = from_config(data)
    if max_lines is not None:
        policy['max_lines'] = positive(max_lines, 'max_lines')
    if max_chars is not None:
        policy['max_chars'] = positive(max_chars, 'max_chars')
    legacy = data.get('review', {})
    migrate = isinstance(legacy, dict) and any(key in legacy for key in ('max_file_lines', 'max_file_chars'))
    if max_lines is not None or max_chars is not None or migrate:
        data['file_limits'] = policy
        if migrate:
            legacy.pop('max_file_lines', None)
            legacy.pop('max_file_chars', None)
            if not legacy:
                data.pop('review')
        atomic_write(repo / 'codemanager.json', (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode())
    return policy


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip())
    return result.stdout


def root(path):
    path = Path(path).resolve(strict=True)
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True)
    return Path(os.fsdecode(result.stdout).strip()) if result.returncode == 0 else path


def paths(repo, staged=False, all_files=False):
    probe = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--git-dir'], capture_output=True)
    if probe.returncode:
        if staged:
            raise ValueError('--staged requires a Git repository')
        from .graph import SKIP
        found = []
        for directory, dirs, files in os.walk(repo, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
            found.extend((Path(directory) / name).relative_to(repo).as_posix() for name in files)
        return sorted(found)
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
    if all_files or head.returncode:
        selected = git(repo, 'ls-files', '-z', '--cached')
    else:
        args = ['diff', '--name-only', '--no-renames', '-z']
        if staged:
            args.append('--cached')
        selected = git(repo, *args, 'HEAD', '--')
    if not staged:
        selected += git(repo, 'ls-files', '-z', '--others', '--exclude-standard')
    return sorted({os.fsdecode(p) for p in selected.split(b'\0') if p})


def violation(path, raw, policy):
    if b'\0' in raw:
        return None
    content = raw.decode('utf-8', errors='replace')
    lines, chars = len(content.splitlines()), len(content)
    if lines > policy['max_lines'] or chars > policy['max_chars']:
        return {'path': path, 'lines': lines, 'characters': chars}
    return None


def excluded(path):
    from .graph import SKIP
    return any(part in SKIP for part in Path(path).parts)


def check_tree(repo, tree, selected, policy):
    violations = []
    for path in sorted(selected):
        if excluded(path):
            continue
        entry = git(repo, 'ls-tree', '-z', tree, '--', path)
        if not entry or not entry.startswith((b'100644 ', b'100755 ')):
            continue
        raw = git(repo, 'show', f'{tree}:{path}')
        item = violation(path, raw, policy)
        if item:
            violations.append(item)
    return {'passed': not violations, 'limits': policy, 'violations': violations}


def check(repo, *, staged=False, all_files=False):
    repo = root(repo)
    selected = paths(repo, staged, all_files)
    if staged:
        tree = git(repo, 'write-tree').decode().strip()
        result = subprocess.run(['git', '-C', str(repo), 'show', f'{tree}:codemanager.json'], capture_output=True)
        policy = from_config(json.loads(result.stdout) if result.returncode == 0 else {})
        return check_tree(repo, tree, selected, policy)
    policy = settings(repo)
    violations = []
    for name in selected:
        if excluded(name):
            continue
        path = repo / name
        if path.is_symlink() or not path.resolve().is_relative_to(repo) or not path.is_file():
            continue
        item = violation(name, path.read_bytes(), policy)
        if item:
            violations.append(item)
    return {'passed': not violations, 'limits': policy, 'violations': violations}


def failure(report):
    policy = report['limits']
    return ('File length limits exceeded. Split large files into modules; do not truncate code or raise limits just to pass.\n' +
            '\n'.join(f"{v['path']}: {v['lines']} lines, {v['characters']} characters; "
                      f"limits {policy['max_lines']} lines / {policy['max_chars']} characters" for v in report['violations']))


def require(report):
    if not report['passed']:
        raise ValueError(failure(report))
    return report
