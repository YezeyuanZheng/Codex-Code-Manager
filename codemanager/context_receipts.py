"""Explicit, immutable context receipts; coverage is checked against current file digests."""
import json
import re
from uuid import uuid4
from .snapshot import atomic_write
from .source_coverage import SourceCoverage

MAX_BYTES = 1_000_000
MAX_RECEIPTS = 128


def load(graph, token):
    if token is None:
        return {}
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        raise ValueError('context_token must be a token returned by get_context')
    path = graph.cache_dir/'context-receipts'/(token+'.json')
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise ValueError('Invalid context receipt')
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('context_token expired or is unknown; retry without it') from exc
    if value.get('repo') != str(graph.repo):
        raise ValueError('context_token belongs to a different repository')
    digests = dict(graph.db.execute('SELECT path,digest FROM files'))
    # A file edit invalidates all of that file's previous positions, including shifted IDs.
    return {path: item['ranges'] for path, item in value['files'].items()
            if digests.get(path) == item['digest']}


def coverage(ranges):
    result = SourceCoverage()
    result.ranges = {path: [(tuple(a),tuple(b),ident) for a,b,ident in spans]
                     for path,spans in ranges.items()}
    return result


def token():
    return uuid4().hex


def save(graph, ident, ranges, output):
    result = coverage(ranges)
    for item in output.get('results', []):
        row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (item['id'],)).fetchone()
        result.add(row, item['offset'], len(item['body']))
    digests = dict(graph.db.execute('SELECT path,digest FROM files'))
    files = {path: {'digest': digests[path], 'ranges': spans}
             for path, spans in result.ranges.items() if path in digests}
    value = json.dumps({'repo': str(graph.repo), 'files': files})
    if len(value.encode()) > MAX_BYTES:
        # Bounded derived cache: retain newest file evidence when the receipt grows.
        while files and len(value.encode()) > MAX_BYTES:
            files.pop(next(iter(files)))
            value = json.dumps({'repo': str(graph.repo), 'files': files})
    folder = graph.cache_dir/'context-receipts'
    folder.mkdir(exist_ok=True)
    atomic_write(folder/(ident+'.json'), value.encode())
    receipts = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in receipts[MAX_RECEIPTS:]:
        path.unlink(missing_ok=True)
