"""Deterministic, sharded text snapshots. Source bodies stay in source files."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
from .locking import snapshot_lock

FORMAT = 1
SHARD_BYTES = 1024 * 1024
TABLES = {
    'files': ('path', 'digest'),
    'nodes': ('id', 'path', 'name', 'kind', 'start', 'end'),
    'edges': ('source', 'target', 'kind'),
    'refs': ('source', 'name', 'kind'),
}


def is_shard_name(name):
    return (name.endswith('.jsonl') and len(name) == 70
            and all(c in '0123456789abcdef' for c in name[:-6]))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def atomic_write(path, data):
    if path.exists() and path.read_bytes() == data:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def export_snapshot(graph, *, output_dir=None):
    with snapshot_lock(graph.cache_dir):
        return _export_snapshot(graph, output_dir=output_dir)


def _export_snapshot(graph, *, output_dir=None):
    refreshed = graph.index()
    folder = Path(output_dir) if output_dir is not None else graph.repo / '.codemanager' / 'index'
    folder.mkdir(parents=True, exist_ok=True)
    shards = []
    chunk = bytearray()

    def flush():
        if not chunk:
            return
        data = bytes(chunk)
        digest = hashlib.sha256(data).hexdigest()
        filename = digest + '.jsonl'
        atomic_write(folder / filename, data)
        shards.append({'file': filename, 'sha256': digest, 'bytes': len(data)})
        chunk.clear()

    with graph.db:
        for table, columns in TABLES.items():
            names = ','.join(columns)
            for row in graph.db.execute(f'SELECT {names} FROM {table} ORDER BY {names}'):
                line = encoded({'table': table, 'values': list(row)})
                if len(line) > SHARD_BYTES:
                    raise ValueError('A snapshot record exceeds the 1 MiB shard limit')
                if len(chunk) + len(line) > SHARD_BYTES:
                    flush()
                chunk.extend(line)
        flush()
    manifest = encoded({'format': FORMAT, 'shards': shards})
    atomic_write(folder / 'manifest.json', manifest)
    # Each Git tree contains its current graph; Git history preserves older graphs.
    current = {shard['file'] for shard in shards}
    for path in folder.glob('*.jsonl'):
        if is_shard_name(path.name) and path.name not in current and (path.is_file() or path.is_symlink()):
            path.unlink(missing_ok=True)
    return {'snapshot': '.codemanager/index/manifest.json',
            'shards': len(shards), 'bytes': len(manifest) + sum(s['bytes'] for s in shards),
            'refreshed': refreshed}


def load_snapshot(graph, manifest):
    """Validate all input before replacing the cache. No SQL comes from the snapshot."""
    try:
        info = json.loads(manifest)
        if info['format'] != FORMAT:
            raise ValueError('Unsupported snapshot format; regenerate with this CodeManager version')
        rows = {table: [] for table in TABLES}
        folder = graph.repo / '.codemanager' / 'index'
        for shard in info['shards']:
            digest = shard['sha256']
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid snapshot shard digest')
            if shard['file'] != digest + '.jsonl':
                raise ValueError('Invalid snapshot shard filename')
            path = folder / shard['file']
            if path.is_symlink() or not path.resolve().is_relative_to(graph.repo):
                raise ValueError('Snapshot shard must be inside the checkout')
            if path.stat().st_size > SHARD_BYTES:
                raise ValueError('Snapshot shard exceeds 1 MiB')
            data = path.read_bytes()
            if len(data) != shard['bytes'] or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Snapshot shard checksum mismatch')
            for line in data.splitlines():
                record = json.loads(line)
                table, values = record['table'], record['values']
                if table not in TABLES or not isinstance(values, list) or len(values) != len(TABLES[table]):
                    raise ValueError('Invalid snapshot record')
                for i, value in enumerate(values):
                    numeric = table == 'nodes' and i in (4, 5)
                    if type(value) is not (int if numeric else str):
                        raise ValueError('Invalid snapshot field type')
                rows[table].append(values)
        files = dict(rows['files'])
        if len(files) != len(rows['files']):
            raise ValueError('Duplicate snapshot file')
        for path in files:
            relative = PurePosixPath(path)
            if relative.is_absolute() or '..' in relative.parts or relative.as_posix() != path:
                raise ValueError('Invalid snapshot source path')
        sources = {}
        available = dict(graph.paths())
        for path, digest in files.items():
            if path not in available:
                continue
            try:
                raw = available[path].read_bytes()
                if hashlib.sha256(raw).hexdigest() == digest:
                    sources[path] = raw.decode('utf-8').splitlines()
            except (OSError, UnicodeError):
                continue
        nodes = {}
        for row in rows['nodes']:
            ident, path, name, kind, start, end = row
            if ident in nodes or path not in files or start < 1 or end < start - 1:
                raise ValueError('Invalid or duplicate snapshot node')
            if path in sources and end > len(sources[path]):
                raise ValueError('Snapshot node exceeds source length')
            nodes[ident] = row
        for source, target, _ in rows['edges']:
            if source not in nodes or target not in nodes:
                raise ValueError('Dangling snapshot edge')
        for source, _, _ in rows['refs']:
            if source not in nodes:
                raise ValueError('Dangling snapshot reference')
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Invalid snapshot: {exc}') from exc

    with graph.db:
        for table in (*TABLES, 'search'):
            graph.db.execute(f'DELETE FROM {table}')
        for path in sources:
            graph.db.execute('INSERT INTO files VALUES (?,?)', (path, files[path]))
        loaded = set()
        for ident, path, name, kind, start, end in rows['nodes']:
            if path not in sources:
                continue
            body = '\n'.join(sources[path][start-1:end])
            graph.db.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)', (ident, path, name, kind, start, end, body))
            from .graph import terms
            graph.db.execute('INSERT INTO search VALUES (?,?,?,?)', (ident, terms(name), terms(path), terms(body)))
            loaded.add(ident)
        for row in rows['edges']:
            if row[0] in loaded and row[1] in loaded:
                graph.db.execute('INSERT INTO edges VALUES (?,?,?)', row)
        for row in rows['refs']:
            if row[0] in loaded:
                graph.db.execute('INSERT INTO refs VALUES (?,?,?)', row)
        # Parse only changed/new source files, remove deleted files, and resolve links.
        graph.index()
        graph.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('snapshot', hashlib.sha256(manifest).hexdigest()))
