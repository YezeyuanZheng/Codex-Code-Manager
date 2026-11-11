"""Polling updates and Git-index validation without changing staged files."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tempfile
import time

from .graph import EXTENSIONS, SKIP, Graph
from .snapshot import SHARD_BYTES


def interval_seconds(value):
    value = float(value)
    if not 0.1 <= value <= 60:
        raise ValueError('interval must be between 0.1 and 60 seconds')
    return value


def watch(graph, interval=1):
    interval = interval_seconds(interval)
    try:
        while True:
            result = graph.export()
            if result['refreshed']['unreadable_files']:
                print('codemanager: unreadable files: ' + ', '.join(result['refreshed']['unreadable_files']), file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip())
    return result.stdout


def candidate(path):
    relative = PurePosixPath(path)
    return (not any(part in SKIP or part.startswith('.') for part in relative.parts)
            and relative.suffix.lower() in EXTENSIONS)


def materialize_staged(repo, destination):
    """Read raw Git blobs, bypassing working-tree changes and smudge filters."""
    entries = []
    for record in git(repo, 'ls-files', '--stage', '-z').split(b'\0'):
        if not record:
            continue
        metadata, raw_path = record.split(b'\t', 1)
        mode, oid, stage = metadata.split()
        path = os.fsdecode(raw_path)
        if stage != b'0':
            raise ValueError('Resolve staged merge conflicts before checking the graph')
        bundle = path.startswith('.codemanager/index/')
        if not candidate(path) and not bundle:
            continue
        if mode not in (b'100644', b'100755'):
            if bundle:
                raise ValueError('Snapshot files must be regular Git files')
            continue
        relative = PurePosixPath(path)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid Git index path')
        entries.append((path, oid, bundle))
    # Query sizes first so large source artifacts never have to be loaded.
    if not entries:
        return
    checks = subprocess.run(
        ['git', '-C', str(repo), 'cat-file', '--batch-check'],
        input=b''.join(oid+b'\n' for _, oid, _ in entries), capture_output=True, check=True).stdout
    selected = []
    for entry, line in zip(entries, checks.splitlines(), strict=True):
        _, kind, size = line.split()
        limit = SHARD_BYTES if entry[2] else 1_000_000
        if kind != b'blob' or int(size) > limit:
            if entry[2]:
                raise ValueError('Snapshot file exceeds the supported size limit')
            continue
        selected.append(entry)
    if not selected:
        return
    result = subprocess.run(['git', '-C', str(repo), 'cat-file', '--batch'],
                            input=b''.join(oid+b'\n' for _, oid, _ in selected),
                            capture_output=True, check=True)
    stream = io.BytesIO(result.stdout)
    for path, _, _ in selected:
        _, _, size = stream.readline().split()
        data = stream.read(int(size))
        if stream.read(1) != b'\n':
            raise ValueError('Invalid Git blob stream')
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def bundled_bytes(repo):
    folder = repo / '.codemanager' / 'index'
    manifest = folder / 'manifest.json'
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError('Graph snapshot is missing. Run export and stage .codemanager/index/')
    raw = manifest.read_bytes()
    try:
        info = json.loads(raw)
        result = {'manifest.json': raw}
        for shard in info['shards']:
            name = shard['file']
            digest = shard['sha256']
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or name != digest+'.jsonl':
                raise ValueError('Invalid snapshot shard')
            path = folder / name
            if path.is_symlink():
                raise ValueError('Snapshot shards must be regular files')
            data = path.read_bytes()
            if len(data) > SHARD_BYTES or len(data) != shard['bytes'] or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Invalid snapshot shard checksum or size')
            result[name] = data
        return result
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError(f'Invalid graph snapshot: {exc}') from exc


def check_snapshot(repo, staged=False):
    repo = Path(repo).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='codemanager-check-') as name:
        root = Path(name)
        if staged:
            materialize_staged(repo, root)
            expected = bundled_bytes(root)
            graph = Graph(root, load_snapshot=False, use_git=False)
            try:
                graph.export()
                actual = bundled_bytes(root)
            finally:
                graph.close()
        else:
            expected = bundled_bytes(repo)
            # Build separately, keeping both the repository cache and bundle untouched.
            graph = Graph(repo, load_snapshot=False, cache_dir=root / 'cache')
            try:
                from .snapshot import export_snapshot
                export_snapshot(graph, output_dir=root / '.codemanager' / 'index')
                actual = bundled_bytes(root)
            finally:
                graph.close()
        if expected != actual:
            context = 'staged code' if staged else 'working tree'
            raise ValueError(f'Graph snapshot does not match {context}. Run export, stage matching source and .codemanager/index/, then retry. For partial staging, export from a worktree containing exactly the intended commit.')
    return {'valid': True, 'against': 'staged' if staged else 'working_tree'}


def install_hook(repo):
    repo = Path(repo).resolve(strict=True)
    top = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip())
    if top != repo:
        raise ValueError('Install the hook at the repository root')
    # Do not write to a shared/global custom hooksPath.
    configured = subprocess.run(['git', '-C', str(repo), 'config', '--get', 'core.hooksPath'], capture_output=True)
    if configured.returncode == 0:
        raise ValueError('core.hooksPath is already configured. Add the check --staged command to your existing pre-commit workflow instead.')
    path = Path(os.fsdecode(git(repo, 'rev-parse', '--git-path', 'hooks/pre-commit')).strip())
    if not path.is_absolute():
        path = repo / path
    marker = '# CodeManager staged graph check v1'
    if path.exists() and marker not in path.read_text():
        raise ValueError('A pre-commit hook already exists. Add the check --staged command to it; the existing hook was preserved.')
    module_root = str(Path(__file__).resolve().parent.parent)
    script = f'import sys; sys.path.insert(0, {module_root!r}); from codemanager.__main__ import main; main()'
    command = shlex.join([sys.executable, '-c', script, 'check', '--repo', str(repo), '--staged'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + marker + '\nexec ' + command + '\n')
    path.chmod(0o755)
    return {'installed': str(path)}
