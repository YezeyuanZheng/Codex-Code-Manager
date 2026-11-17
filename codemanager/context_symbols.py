"""Bounded symbol resolution and suggestions from the current index."""
import difflib
import re


_QUERY_IDENTIFIER = re.compile(r'(?<![\w])(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?![\w])')


def query_identifiers(queries, maximum=12):
    """Extract code-shaped names while leaving ordinary query words lexical."""
    found = []
    seen = set()
    for query in queries:
        for match in _QUERY_IDENTIFIER.finditer(query):
            name = match.group()
            code_shaped = ('_' in name or '.' in name or
                           re.search(r'[a-z][A-Z]', name) is not None)
            if code_shaped and name not in seen:
                found.append(name)
                seen.add(name)
                if len(found) >= maximum:
                    return found
    return found


def resolve(graph, symbol, *, suggest=True):
    path, name = symbol.rsplit(':', 1) if ':' in symbol else (None, symbol)
    suffix = '.'+name
    rows = graph.db.execute('''SELECT * FROM nodes WHERE kind IN ('function','class')
        AND (name=? OR substr(name,-?)=?) AND (? IS NULL OR path=?)
        ORDER BY name!=?,length(name),path,start LIMIT 5''',
        (name,len(suffix),suffix,path,path,name)).fetchall()
    if not rows and path is None and '.' in name:
        parts = name.split('.')
        for split in range(len(parts)-1, 0, -1):
            module = '/'.join(parts[:split])+'.py'
            rows = graph.db.execute('''SELECT * FROM nodes WHERE name=?
                AND (path=? OR substr(path,-?)=?) ORDER BY path,start LIMIT 5''',
                ('.'.join(parts[split:]),module,len(module)+1,'/'+module)).fetchall()
            if rows:
                break
    if rows:
        return [(dict(row), 'symbol') for row in rows[:4]], ([] if len(rows)<5 else
                [{'symbol':symbol,'reason':'ambiguous_more_matches','hint':'Qualify with path.py:symbol'}])
    if not suggest:
        return [], []
    leaf = name.rsplit('.',1)[-1].lstrip('_').lower()
    parent = name.rsplit('.',1)[0] if '.' in name else None
    # Restrict suggestions by container or file rather than scanning the entire repository.
    candidates = graph.db.execute('''SELECT name,path FROM nodes WHERE kind IN ('function','class')
        AND (? IS NULL OR path=?) AND (? IS NULL OR name LIKE ?) ORDER BY name,path LIMIT 200''',
        (path,path,parent,(parent+'.%') if parent else None)).fetchall()
    ranked = sorted(candidates, key=lambda r: -difflib.SequenceMatcher(
        None,leaf,r['name'].rsplit('.',1)[-1].lstrip('_').lower()).ratio())
    suggestions = [r['path']+':'+r['name'] for r in ranked[:3] if difflib.SequenceMatcher(
        None,leaf,r['name'].rsplit('.',1)[-1].lstrip('_').lower()).ratio()>=0.5]
    item = {'symbol':symbol,'reason':'not_found'}
    if suggestions:
        item['suggestions'] = suggestions
    return [], [item]


def focus_containers(graph, matches, queries):
    """For a long requested class plus a question, return matching indexed members."""
    if not queries:
        return matches
    from .graph import terms
    tokens = terms(' '.join(queries)).split()[:40]
    match = ' OR '.join('"'+word+'"' for word in tokens)
    focused = []
    for row, reason in matches:
        members = []
        if match and row['kind']=='class' and len(row['body'])>6000:
            prefix = row['name']+'.'
            members = graph.db.execute('''SELECT nodes.* FROM search JOIN nodes ON search.id=nodes.id
                WHERE search MATCH ? AND nodes.path=? AND nodes.kind='function'
                AND substr(nodes.name,1,?)=? ORDER BY bm25(search,0,8,3,1),nodes.id LIMIT 3''',
                (match,row['path'],len(prefix),prefix)).fetchall()
        focused.extend((dict(member),'symbol') for member in members) if members else focused.append((row,reason))
    return focused
