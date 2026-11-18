"""Bounded indexed call candidates, including attribute calls absent from graph edges."""
import ast
import re
import textwrap
from functools import lru_cache


@lru_cache(maxsize=256)
def called_names(body):
    # Key by source so edits cannot reuse the previous call names.
    try:
        tree = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return frozenset()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return frozenset(names)


def callers(graph, row, maximum=4):
    if row['kind'] != 'function':
        return []
    name = row['name'].rsplit('.', 1)[-1]
    if not re.fullmatch(r'[A-Za-z_]\w*', name):
        return []
    # FTS narrows candidates without rereading or scanning the source tree.
    from .graph import terms
    match = ' AND '.join('"'+token+'"' for token in terms(name).split())
    if not match:
        return []
    rows = graph.db.execute('''SELECT nodes.* FROM search JOIN nodes ON search.id=nodes.id
        WHERE search MATCH ? AND nodes.kind='function' AND nodes.id!=?
        ORDER BY rank,nodes.id LIMIT 40''', (match, row['id'])).fetchall()
    found = [dict(candidate) for candidate in rows if name in called_names(candidate['body'])]
    found.sort(key=lambda item: (is_test(item['path']), len(item['body']), item['id']))
    return [{'anchor': row['id'], 'id': item['id'], 'kind': 'text_call_candidate',
             'direction': 'incoming'} for item in found[:maximum]]


def is_test(path):
    return any(part in ('test', 'tests', 'testing') or part.startswith('test_') or part.endswith('_test.py')
               for part in path.split('/'))


def rank_candidates(candidates, query):
    """Prefer implementation units while retaining original relevance within each tier."""
    if re.search(r'\b(docs?|documentation|readme|guides?)\b', query, re.I):
        return candidates
    test_only = bool(re.match(r'\s*(?:(?:which|what|find|list|show)\s+)?tests?\b', query, re.I))
    def tier(candidate):
        row = candidate[0]
        return (int(is_test(row['path']) != test_only),
                {'function': 0, 'class': 1, 'module': 2, 'file': 3}.get(row['kind'], 2))
    ranked = sorted(candidates, key=tier)
    if test_only or not re.search(r'\btests?\b', query, re.I):
        return ranked
    code = [c for c in ranked if not is_test(c[0]['path'])]
    tests = [c for c in ranked if is_test(c[0]['path'])]
    # Mixed implementation/test requests get supporting tests without promoting every test hit.
    result = []
    while code or tests:
        result.extend(code[:3]); del code[:3]
        result.extend(tests[:1]); del tests[:1]
    return result
