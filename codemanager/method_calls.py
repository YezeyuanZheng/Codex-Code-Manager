"""Index local receiver calls and expose the ones visible in retrieved source."""
from collections import defaultdict
import re

RECEIVER_CALL = re.compile(r'\b(?:self|cls)\.([A-Za-z_]\w*)\s*\(')


def initialize(graph):
    with graph.db:
        graph.db.execute('''CREATE TABLE IF NOT EXISTS method_calls(
            source TEXT, target TEXT, start INTEGER, stop INTEGER,
            PRIMARY KEY(source, start, target)) WITHOUT ROWID''')
        if graph.db.execute("SELECT 1 FROM metadata WHERE key='method_calls_v1'").fetchone():
            return
        claimed = graph.db.execute("INSERT OR IGNORE INTO metadata VALUES ('method_calls_v1','1')")
        if claimed.rowcount:
            paths = [row[0] for row in graph.db.execute(
                "SELECT DISTINCT path FROM nodes WHERE kind='function'")]
            refresh(graph, paths)


def refresh(graph, paths):
    for path in paths:
        rows = graph.db.execute(
            "SELECT id,name,body FROM nodes WHERE path=? AND kind='function'", (path,)
        ).fetchall()
        names = {row['name']: row['id'] for row in rows}
        for row in rows:
            owner, separator, _ = row['name'].rpartition('.')
            if not separator:
                continue
            for call in RECEIVER_CALL.finditer(row['body']):
                target = names.get(f'{owner}.{call[1]}')
                if target:
                    graph.db.execute('INSERT OR IGNORE INTO method_calls VALUES (?,?,?,?)',
                                     (row['id'], target, *call.span()))


def called_methods(graph, results):
    if not results:
        return
    sources = tuple(dict.fromkeys(item['id'] for item in results))
    placeholders = ','.join('?' for _ in sources)
    rows = graph.db.execute(f'''SELECT calls.source,calls.start,calls.stop,nodes.path,nodes.name
        FROM method_calls AS calls JOIN nodes ON nodes.id=calls.target
        WHERE calls.source IN ({placeholders}) ORDER BY calls.source,calls.start,nodes.name''', sources)
    by_source = defaultdict(list)
    for row in rows:
        by_source[row['source']].append(row)
    seen = {(item['path'], item['name']) for item in results}
    for item in results:
        start = item['offset']
        stop = start + len(item['body'])
        for row in by_source[item['id']]:
            key = row['path'], row['name']
            if key not in seen and start <= row['start'] and row['stop'] <= stop:
                seen.add(key)
                yield ':'.join(key)
