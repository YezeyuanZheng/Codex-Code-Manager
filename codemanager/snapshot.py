"""Deterministic, sharded text snapshots. Source bodies stay in source files."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
from .locking import snapshot_lock

FORMAT = 2
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


def bucket_descriptors(info):
    """Format 2 uses stable path-hash partitions; format 1 remains readable."""
    if info.get('format') != FORMAT:
        return {}
    descriptors = {entry['file']: entry for entry in info['shards']}
    return {bucket: [descriptors[name] for name in names] for bucket, names in info['buckets'].items()}


def reusable_shard(graph, folder, entry):
    from .graph import file_signature
    name = entry['file']
    if not is_shard_name(name) or name != entry['sha256']+'.jsonl':
        return False
    path = folder / name
    if not path.is_file() or path.is_symlink() or path.stat().st_size > SHARD_BYTES:
        return False
    signature = file_signature(path)
    cached = graph.db.execute('SELECT signature FROM shard_state WHERE name=?', (name,)).fetchone()
    if not cached or cached[0] != signature:
        data = path.read_bytes()
        if len(data) > SHARD_BYTES or len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            return False
        graph.db.execute('INSERT OR REPLACE INTO shard_state VALUES (?,?)', (name, signature))
    return True


def serialize_bucket(graph, bucket):
    """Only rows owned by this partition are queried and serialized."""
    for table, columns in TABLES.items():
        names = ','.join('t.'+column for column in columns)
        order = ','.join('t.'+column for column in columns)
        if table == 'files':
            sql = f'SELECT {names} FROM files t WHERE bucket=? ORDER BY {order}'
        elif table == 'nodes':
            sql = f'SELECT {names} FROM nodes t JOIN files f ON f.path=t.path WHERE f.bucket=? ORDER BY {order}'
        else:
            sql = f'SELECT {names} FROM {table} t JOIN nodes n ON n.id=t.source JOIN files f ON f.path=n.path WHERE f.bucket=? ORDER BY {order}'
        for row in graph.db.execute(sql, (bucket,)):
            yield encoded({'table': table, 'values': list(row)})


def export_snapshot(graph, *, output_dir=None, full=False):
    with snapshot_lock(graph.cache_dir):
        return _export_snapshot(graph, output_dir=output_dir, full=full)


def _export_snapshot(graph, *, output_dir=None, full=False):
    from .graph import bucket_for, file_signature
    refreshed = graph.index(full=full)
    folder = Path(output_dir) if output_dir is not None else graph.repo / '.codemanager' / 'index'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        previous = bucket_descriptors(json.loads((folder / 'manifest.json').read_bytes()))
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
    cached = {row['bucket']: row['descriptors'] for row in graph.db.execute('SELECT * FROM snapshot_buckets')}
    dirty = {bucket_for(row[0]) for row in graph.db.execute('SELECT path FROM snapshot_dirty')}
    partitions, written, reused = {}, 0, 0
    with graph.db:
        for (bucket,) in graph.db.execute('SELECT DISTINCT bucket FROM files ORDER BY bucket').fetchall():
            old = previous.get(bucket)
            if not full and bucket not in dirty and old is not None and cached.get(bucket) == json.dumps(old, sort_keys=True) and all(reusable_shard(graph, folder, entry) for entry in old):
                partitions[bucket] = old
                reused += 1
                continue
            entries, chunk = [], bytearray()

            def flush():
                if not chunk:
                    return
                data = bytes(chunk)
                digest = hashlib.sha256(data).hexdigest()
                filename = digest + '.jsonl'
                path = folder / filename
                atomic_write(path, data)
                graph.db.execute('INSERT OR REPLACE INTO shard_state VALUES (?,?)', (filename, file_signature(path)))
                entries.append({'file': filename, 'sha256': digest, 'bytes': len(data)})
                chunk.clear()

            for line in serialize_bucket(graph, bucket):
                if len(line) > SHARD_BYTES:
                    raise ValueError('A snapshot record exceeds the 1 MiB shard limit')
                if len(chunk) + len(line) > SHARD_BYTES:
                    flush()
                chunk.extend(line)
            flush()
            partitions[bucket] = entries
            written += 1
        shards = [entry for entries in partitions.values() for entry in entries]
        manifest = encoded({'format': FORMAT, 'buckets': {key: [entry['file'] for entry in value] for key, value in partitions.items()}, 'shards': shards})
        atomic_write(folder / 'manifest.json', manifest)
        # Git history owns old versions; the working tree contains only current shards.
        current = {entry['file'] for entry in shards}
        for path in folder.glob('*.jsonl'):
            if is_shard_name(path.name) and path.name not in current and (path.is_file() or path.is_symlink()):
                path.unlink(missing_ok=True)
                graph.db.execute('DELETE FROM shard_state WHERE name=?', (path.name,))
        graph.db.execute('DELETE FROM snapshot_buckets')
        graph.db.executemany('INSERT INTO snapshot_buckets VALUES (?,?)', ((key, json.dumps(value, sort_keys=True)) for key, value in partitions.items()))
        graph.db.execute('DELETE FROM snapshot_dirty')
        if output_dir is None:
            graph.db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot',?)", (hashlib.sha256(manifest).hexdigest(),))
    return {'snapshot': '.codemanager/index/manifest.json',
            'shards': len(shards), 'bytes': len(manifest) + sum(entry['bytes'] for entry in shards),
            'rebuilt_partitions': written, 'reused_partitions': reused, 'refreshed': refreshed}


def load_snapshot(graph, manifest):
    """Validate all input before replacing the cache. No SQL comes from the snapshot."""
    try:
        info = json.loads(manifest)
        if info['format'] not in (1, FORMAT):
            raise ValueError('Unsupported snapshot format; regenerate with this CodeManager version')
        partitions = bucket_descriptors(info)
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

    from .graph import bucket_for, file_signature, terms
    with graph.db:
        existing = {row['path']: row['digest'] for row in graph.db.execute('SELECT path,digest FROM files')}
        cached = {row['bucket']: row['descriptors'] for row in graph.db.execute('SELECT * FROM snapshot_buckets')}
        altered = {key for key, value in partitions.items() if cached.get(key) != json.dumps(value, sort_keys=True)}
        replacing = {path for path in sources if existing.get(path) != files[path] or not partitions or bucket_for(path) in altered}
        removed = set(existing) - sources.keys()
        for path in removed | replacing:
            graph.remove(path, keep_embeddings=path in replacing)
        for path in replacing:
            graph.db.execute('INSERT INTO files VALUES (?,?,?)', (path, files[path], bucket_for(path)))
        loaded = {row[0] for row in graph.db.execute('SELECT id FROM nodes')}
        for ident, path, name, kind, start, end in rows['nodes']:
            if path not in replacing:
                continue
            body = '\n'.join(sources[path][start-1:end])
            graph.db.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)', (ident, path, name, kind, start, end, body))
            graph.db.execute('INSERT INTO search VALUES (?,?,?,?)', (ident, terms(name), terms(path), terms(body)))
            loaded.add(ident)
        for row in rows['edges']:
            if nodes[row[0]][1] in replacing and row[0] in loaded and row[1] in loaded:
                graph.db.execute('INSERT INTO edges VALUES (?,?,?)', row)
        for row in rows['refs']:
            if nodes[row[0]][1] in replacing and row[0] in loaded:
                graph.db.execute('INSERT INTO refs VALUES (?,?,?)', row)
        # A removed or reloaded target can change an otherwise unchanged importer's edges.
        excluded = set(files) - sources.keys()
        graph.resolve_changes(removed | replacing | excluded)
        graph.mark_dirty(removed | replacing | excluded)
        graph.index()
        graph.db.execute('DELETE FROM snapshot_buckets')
        graph.db.executemany('INSERT INTO snapshot_buckets VALUES (?,?)', ((key, json.dumps(value, sort_keys=True)) for key, value in partitions.items()))
        graph.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('snapshot', hashlib.sha256(manifest).hexdigest()))
