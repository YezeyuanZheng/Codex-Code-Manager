"""Resolve batched retrieval requests and bounded one-hop relationships."""


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
    from .context_symbols import resolve, focus_containers
    focused_containers = set()
    for symbol in symbols:
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
                          'direction': 'outgoing' if edge['source'] == ident else 'incoming'})
        for hint in callers(graph, spec['row']):
            if not any(item['anchor']==ident and item['id']==hint['id'] for item in hints):
                hints.append(hint)
    from .graph import terms
    # Generic request words should not promote large, unrelated callers.
    stopwords = set('a an and are as at be by can code do does for from how i in is it '
                    'of on or show that the this to what when where which with'.split())
    def tokens(text):
        return set(terms(text).lower().replace('_', ' ').split())
    words = tokens(query) - stopwords
    def priority(hint):
        row = graph.db.execute('SELECT path,kind,name,body FROM nodes WHERE id=?', (hint['id'],)).fetchone()
        name_words = tokens(row['name']) if row else set()
        body_words = tokens(row['body']) if row else set()
        overlap = len(words & body_words)
        # Name matches identify focused helpers; density breaks ties in favor of
        # compact evidence rather than callers mentioning many unrelated topics.
        relevance = 2 * len(words & name_words) + overlap
        density = overlap / max(1, len(body_words))
        return (hint['kind']=='contains', is_test(row['path']) if row else True,
                -relevance, -density, row['kind']!='function' if row else True)
    hints.sort(key=priority)
    return hints
