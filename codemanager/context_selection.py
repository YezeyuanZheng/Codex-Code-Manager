"""Resolve batched retrieval requests and bounded one-hop relationships."""
import ast
import textwrap


def call_site(row, callee_name):
    """Return compact indexed call expressions that explain an edge."""
    leaf = callee_name.rsplit('.', 1)[-1]
    try:
        tree = ast.parse(textwrap.dedent(row['body']))
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == leaf:
            found.append({'path': row['path'], 'line': row['start']+node.lineno-1,
                          'expression': ast.get_source_segment(textwrap.dedent(row['body']), node)})
    return found[:3]
def strings(value, name, maximum):
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > maximum or
        any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ValueError(f'{name} must be a list of at most {maximum} nonempty strings')
    return list(dict.fromkeys(value))


def select(graph, queries, symbols, node_ids, offsets, limit, mode, model, include_related=True):
    explicit, missing = [], []
    for ident in node_ids:
        row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (ident,)).fetchone()
        if row is None:
            missing.append({'node_id': ident, 'reason': 'stale_or_unknown'})
        else:
            explicit.append((dict(row), 'requested'))
    from .context_symbols import resolve, focus_containers, query_identifiers
    focused_containers = set()
    resolved_symbols = list(symbols)
    if not symbols:
        for symbol in query_identifiers(queries, maximum=12):
            matches, unresolved = resolve(graph, symbol, suggest=False)
            if len(matches) == 1 and not unresolved and matches[0][0]['kind'] == 'function':
                resolved_symbols.append(symbol)
                break
    for symbol in resolved_symbols:
        matches, unresolved = resolve(graph, symbol)
        focused = focus_containers(graph, matches, queries)
        kept = {row['id'] for row, _ in focused}
        focused_containers.update(row['id'] for row, _ in matches if row['id'] not in kept)
        explicit.extend(focused)
        missing.extend(unresolved)
    groups = []
    actual_mode = 'lookup'
    actual_model = None
    for query in queries:
        from .caller_context import rank_candidates
        candidates, provider, actual_mode = graph._search_candidates(query, min(50, max(12, limit*3)), mode, model)
        actual_model = getattr(provider, 'model_name', provider.identity) if provider is not None else None
        groups.append([(row, 'search') for row, reason in candidates
                       if reason == 'search' and row['kind'] != 'file' and row['id'] not in focused_containers])
        if not groups[-1]:
            groups[-1] = [(row, 'search') for row, reason in candidates if reason == 'search' and row['id'] not in focused_containers]
        groups[-1] = rank_candidates(groups[-1], query)[:limit]
    # Interleave independent questions so the first cannot consume every selected slot.
    ranked = explicit + [group[index] for index in range(max(map(len, groups), default=0))
                         for group in groups if index < len(group)]
    selected, seen, deferred = [], set(), []
    # Leave room for caller/callee bodies; explicit requests always retain their slots.
    reserve = min(max(1, limit//4), max(0, limit-len(explicit)-min(2, len(queries))))
    primary_limit = max(1, limit-reserve) if queries and include_related else limit
    for row, reason in ranked:
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        if len(selected) < primary_limit:
            start = offsets.get(row['id'], 0)
            if start > len(row['body']):
                raise ValueError('offset exceeds node source length')
            # Small definitions are returned from their beginning, even for a dense tail hit.
            if row['id'] not in offsets and len(row['body']) > 6000:
                start = row.get('_offset', 0)
            selected.append({'row': row, 'reason': reason, 'offset': start,
                             'length': min(6000, len(row['body']) - start)})
        else:
            deferred.append({'id': row['id'], 'offset': offsets.get(row['id'], 0)})
    return selected, missing, deferred, actual_mode, actual_model


def relationships(graph, selected, maximum, query=""):
    from .caller_context import callers, is_test
    hints, seen = [], set()
    for spec in selected:
        ident = spec['row']['id']
        edges = graph.db.execute('''SELECT source,target,kind FROM edges
            WHERE source=? OR target=? ORDER BY kind='contains',kind,source,target LIMIT ?''',
            (ident, ident, maximum)).fetchall()
        for edge in edges:
            key = edge['source'], edge['target'], edge['kind']
            if key in seen:
                continue
            seen.add(key)
            hints.append({'anchor': ident,
                          'id': edge['target'] if edge['source'] == ident else edge['source'],
                          'kind': edge['kind'],
                          'direction': 'outgoing' if edge['source'] == ident else 'incoming',
                          **({'call_sites': call_site(spec['row'], graph.db.execute(
                              'SELECT name FROM nodes WHERE id=?',
                              (edge['target'],)).fetchone()['name'])}
                             if edge['source'] == ident and edge['kind'] in
                             ('calls_candidate', 'imported_calls_candidate') else {})})
        indexed_calls = graph.db.execute("SELECT 1 FROM edges WHERE target=? AND kind IN "
                                         "('calls_candidate','imported_calls_candidate','test_calls_candidate') LIMIT 1",
                                         (ident,)).fetchone()
        for hint in ([] if indexed_calls else callers(graph, spec['row'])):
            if not any(item['anchor']==ident and item['id']==hint['id'] for item in hints):
                hints.append(hint)
    # Add only a one-node bridge on a two-edge call path between selected
    # functions. This connects relevant endpoints without broad graph crawling.
    anchors = [spec['row'] for spec in selected if spec['row']['kind']=='function']
    if len(anchors) > 1:
        adjacency = {}

        def outgoing(source):
            if source not in adjacency:
                adjacency[source] = [
                    edge['target'] for edge in graph.db.execute(
                        "SELECT target FROM edges WHERE source=? AND kind IN "
                        "('calls_candidate','imported_calls_candidate') ORDER BY rowid",
                        (source,))
                ]
            return adjacency[source]

        selected_ids = {row['id'] for row in anchors}
        for start in anchors:
            for middle in outgoing(start['id']):
                if middle in selected_ids:
                    continue
                for end in outgoing(middle):
                    if end in selected_ids and end != start['id']:
                        key = start['id'], middle, 'path_bridge'
                        if key not in seen:
                            seen.add(key)
                            hints.append({'anchor': start['id'], 'id': middle,
                                          'kind': 'path_bridge', 'direction': 'outgoing',
                                          'path_end': end})
    from .graph import terms
    # Generic request words should not promote large, unrelated callers.
    stopwords = set('a an and are as at be by can code do does for from how i in is it '
                    'of on or show that the this to what when where which with'.split())
    def tokens(text):
        return set(terms(text).lower().replace('_', ' ').split())
    words = tokens(query) - stopwords
    flow_query = bool(set(words) & {'trace', 'through', 'flow', 'call', 'calls', 'invoke', 'execution'})
    test_query = bool(set(words) & {'test', 'tests', 'testing'})
    ranked_nodes = {}
    for ident in dict.fromkeys(hint['id'] for hint in hints):
        row = graph.db.execute('SELECT path,kind,name,body FROM nodes WHERE id=?', (ident,)).fetchone()
        if row:
            ranked_nodes[ident] = (row, tokens(row['name']), tokens(row['body']))

    def priority(hint):
        row, name_words, body_words = ranked_nodes.get(hint['id'], (None, (), ()))
        if row is None:
            name_words = body_words = set()
        overlap = len(words & body_words)
        # Name matches identify focused helpers; density breaks ties in favor of
        # compact evidence rather than callers mentioning many unrelated topics.
        relevance = 2 * len(words & name_words) + overlap
        density = overlap / max(1, len(body_words))
        edge_kind = hint['kind']
        flow_penalty = (edge_kind not in ('calls_candidate', 'text_call_candidate')
                        if flow_query and hint['direction']=='outgoing' else False)
        test_penalty = is_test(row['path']) and not test_query if row else True
        return (hint['kind']=='contains', flow_penalty, test_penalty,
                -relevance, -density, row['kind']!='function' if row else True)
    hints.sort(key=priority)
    return hints
