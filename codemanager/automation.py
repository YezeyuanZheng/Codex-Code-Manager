"""Polling updates and Git-index validation without changing staged files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sqlite3
import sys
import tempfile
import time

from .graph import EXTENSIONS, SKIP, Graph
from .locking import snapshot_lock
from .snapshot import SHARD_BYTES, is_shard_name


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
    # Stream raw blobs one file at a time instead of buffering the entire project.
    with tempfile.TemporaryFile() as requests:
        for _, oid, _ in selected:
            requests.write(oid+b'\n')
        requests.seek(0)
        with subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'],
                              stdin=requests, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                for path, _, _ in selected:
                    header = process.stdout.readline().split()
                    if len(header) != 3 or header[1] != b'blob':
                        raise ValueError('Invalid Git blob stream')
                    size = int(header[2])
                    data = process.stdout.read(size)
                    if len(data) != size or process.stdout.read(1) != b'\n':
                        raise ValueError('Invalid Git blob stream')
                    target = destination / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                errors = process.stderr.read()
                if process.wait():
                    raise ValueError(errors.decode(errors='replace'))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()


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
        obsolete = [p.name for p in folder.glob('*.jsonl') if is_shard_name(p.name) and p.name not in result]
        if obsolete:
            raise ValueError('Obsolete graph shards found. Run export and stage .codemanager/index/ including deletions')
        return result
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError(f'Invalid graph snapshot: {exc}') from exc


def seed_validation_cache(repo, destination):
    source = repo / '.codemanager' / 'graph.sqlite3'
    if not source.is_file():
        return False
    destination.mkdir(parents=True, exist_ok=True)
    # Copy only graph rows in a consistent read transaction: embeddings and the
    # full-text index can dominate database size and are unnecessary for validation.
    with snapshot_lock(source.parent):
        reader = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
        target = Graph(repo, load_snapshot=False, cache_dir=destination)
        try:
            reader.execute('BEGIN')
            available = {row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables = ('metadata', 'files', 'nodes', 'edges', 'refs', 'file_state',
                      'snapshot_dirty', 'snapshot_buckets', 'shard_state', 'method_calls')
            with target.db:
                target.db.execute("DELETE FROM metadata WHERE key='method_calls_v1'")
                for table in tables:
                    if table not in available:
                        continue
                    wanted = {row[1] for row in target.db.execute(f'PRAGMA table_info({table})')}
                    columns = [row[1] for row in reader.execute(f'PRAGMA table_info({table})') if row[1] in wanted]
                    names = ','.join(columns)
                    placeholders = ','.join('?' for _ in columns)
                    target.db.executemany(f'INSERT INTO {table} ({names}) VALUES ({placeholders})', reader.execute(f'SELECT {names} FROM {table}'))
        finally:
            reader.close()
            target.close()
    return True


def refresh_staged_embeddings(repo, graph, model=None):
    """Embed the validated staged graph into the persistent local vector cache."""
    from .embeddings import sync_embeddings
    live = Graph(repo, load_snapshot=False)
    try:
        with snapshot_lock(live.cache_dir):
            try:
                result = sync_embeddings(graph, graph.embedding_provider(model), cache_db=live.db)
            except (ValueError, OSError, sqlite3.Error) as exc:
                raise ValueError(f'Commit blocked: staged embedding refresh failed. Install the embedding dependency and make the selected model available, then retry. {exc}') from exc
    finally:
        live.close()
    return result


def check_snapshot(repo, staged=False, *, full=False, embeddings=False, model=None):
    repo = Path(repo).resolve(strict=True)
    if embeddings and not staged:
        raise ValueError('--embeddings requires --staged so commit vectors describe the staged code')
    index_before = git(repo, 'ls-files', '--stage', '-z') if staged else None
    embedding_result = None
    with tempfile.TemporaryDirectory(prefix='codemanager-check-') as name:
        root = Path(name)
        cache = root / 'cache'
        seeded = seed_validation_cache(repo, cache) if not full else False
        if staged:
            tree = root / 'staged'
            tree.mkdir()
            materialize_staged(repo, tree)
            expected = bundled_bytes(tree)
            graph = Graph(tree, load_snapshot=False, use_git=False, cache_dir=cache)
            output = None
        else:
            tree = root / 'result'
            expected = bundled_bytes(repo)
            graph = Graph(repo, load_snapshot=False, cache_dir=cache)
            output = tree / '.codemanager' / 'index'
            # Copy just the existing snapshot so unchanged partitions can be reused.
            output.mkdir(parents=True, exist_ok=True)
            for filename, data in expected.items():
                (output / filename).write_bytes(data)
        try:
            from .review import set_context
            manifest = expected.get('manifest.json')
            with graph.db:
                set_context(graph, json.loads(manifest).get('review') if manifest else None)
                from .purposes import store_purposes
                store_purposes(graph, json.loads(manifest).get('purposes', {}) if manifest else {})
            from .requests import bundled_mappings, restore_mappings
            # Intent is authored metadata: validate against the staged/bundled graph,
            # never against mappings from a different working-tree cache.
            with graph.db:
                graph.db.execute("UPDATE nodes SET request_mapping='[]'")
                restore_mappings(graph, bundled_mappings(graph))
            graph._read_mapping_snapshot = True
            from .snapshot import export_snapshot
            result = export_snapshot(graph, output_dir=output, full=full)
            actual = bundled_bytes(tree)
            if expected != actual:
                context = 'staged code' if staged else 'working tree'
                raise ValueError(f'Graph snapshot does not match {context}. Run export, stage matching source and .codemanager/index/, then retry. For partial staging, export from a worktree containing exactly the intended commit.')
            if embeddings:
                embedding_result = refresh_staged_embeddings(repo, graph, model)
            if staged and git(repo, 'ls-files', '--stage', '-z') != index_before:
                raise ValueError('Staged files changed during validation or embedding generation; retry the commit')
        finally:
            graph.close()
    return {'valid': True, 'against': 'staged' if staged else 'working_tree',
            'reused_cache': seeded, 'parsed_files': result['refreshed']['changed_files'],
            'full': full, 'embeddings': embedding_result}


def install_hook(repo):
    repo = Path(repo).resolve(strict=True)
    top = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip())
    if top != repo:
        raise ValueError('Install the hook at the repository root')
    # Do not write to a shared/global custom hooksPath.
    configured = subprocess.run(['git', '-C', str(repo), 'config', '--get', 'core.hooksPath'], capture_output=True)
    if configured.returncode == 0:
        raise ValueError('core.hooksPath is already configured. Add the check --staged --embeddings command to your existing pre-commit workflow instead.')
    path = Path(os.fsdecode(git(repo, 'rev-parse', '--git-path', 'hooks/pre-commit')).strip())
    if not path.is_absolute():
        path = repo / path
    marker = '# CodeManager staged graph, embedding and optional review check v3'
    owned_markers = (marker, '# CodeManager staged graph and embedding check v2', '# CodeManager staged graph check v1')
    if path.exists() and not any(owned in path.read_text() for owned in owned_markers):
        raise ValueError('A pre-commit hook already exists. Add the check --staged --embeddings command to it; the existing hook was preserved.')
    module_root = str(Path(__file__).resolve().parent.parent)
    script = f'import sys; sys.path.insert(0, {module_root!r}); from codemanager.__main__ import main; main()'
    command = shlex.join([sys.executable, '-c', script, 'commit-check', '--repo', str(repo)])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + marker + '\nexec ' + command + '\n')
    path.chmod(0o755)
    return {'installed': str(path)}
