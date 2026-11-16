"""Explicit request intent stored in code nodes and portable graph snapshots."""
import hashlib
import json
from .graph import terms


def body_digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def validate_mappings(value):
    entries = json.loads(value)
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) or set(entry) != {'request', 'fulfills', 'source_digest'} or
        any(not isinstance(v, str) or not v.strip() for v in entry.values()) or
        len(entry['source_digest']) != 64 or
        any(c not in '0123456789abcdef' for c in entry['source_digest'])
        for entry in entries
    ):
        raise ValueError('Invalid request mapping')
    return entries


def update_mapping(graph, node, entries):
    value = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    graph.db.execute('UPDATE nodes SET request_mapping=? WHERE id=?', (value, node['id']))
    graph.db.execute('DELETE FROM search WHERE id=?', (node['id'],))
    intent = ' '.join(m['request']+' '+m['fulfills'] for m in entries)
    graph.db.execute('INSERT INTO search VALUES (?,?,?,?)',
                     (node['id'], terms(node['name']), terms(node['path']), terms(node['body']+' '+intent)))
    graph.mark_dirty([node['path']])


def map_request(graph, node_id, request, fulfills):
    """Attach the requirement and code fingerprint to a current graph node."""
    for value in (request, fulfills):
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError('request and fulfills must be nonempty text of at most 4000 characters')
    from .review import require_mapping
    require_mapping(graph, request)
    graph.export()
    node = graph.db.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
    if node is None:
        raise ValueError('Unknown node_id; search for the current code node first')
    entries = [m for m in validate_mappings(node['request_mapping']) if m['request'] != request.strip()]
    entry = dict(request=request.strip(), fulfills=fulfills.strip(), source_digest=body_digest(node['body']))
    entries.append(entry)
    entries.sort(key=lambda item: item['request'])
    with graph.db:
        update_mapping(graph, node, entries)
    graph.export()
    return {'node_id': node_id, **entry, 'snapshot': '.codemanager/index/manifest.json'}


def restore_mappings(graph, mappings):
    """Carry intent across line shifts only when the annotated code is identical."""
    for node in graph.db.execute('SELECT * FROM nodes').fetchall():
        entries = mappings.get((node['path'], node['name'], node['kind']), [])
        entries = [m for m in entries if m['source_digest'] == body_digest(node['body'])]
        if entries != json.loads(node['request_mapping']):
            update_mapping(graph, node, entries)


def bundled_mappings(graph):
    """Read authoritative mapping metadata, including for changed source files."""
    folder = graph.repo / '.codemanager' / 'index'
    manifest = folder / 'manifest.json'
    if not manifest.exists():
        return {}
    info = json.loads(manifest.read_bytes())
    if info.get('format') not in (3, 4):
        return {}
    mappings = {}
    for shard in info['shards']:
        from .snapshot import reusable_shard
        if not reusable_shard(graph, folder, shard):
            raise ValueError('Snapshot shard checksum mismatch or missing shard')
        for line in (folder / shard['file']).read_bytes().splitlines():
            record = json.loads(line)
            if record['table'] == 'nodes':
                _, path, name, kind, _, _, value = record['values']
                entries = validate_mappings(value)
                if entries:
                    mappings[(path, name, kind)] = entries
    return mappings
