"""Resolve batched retrieval requests and bounded one-hop relationships."""


def strings(value, name, maximum):
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > maximum or
        any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ValueError(f'{name} must be a list of at most {maximum} nonempty strings')
    return list(dict.fromkeys(value))


def select(graph, queries, symbols, node_ids, offsets, limit, mode, model):
    explicit, missing = [], []
    for ident in node_ids:
        row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (ident,)).fetchone()
        if row is None:
            missing.append({'node_id': ident, 'reason': 'stale_or_unknown'})
        else:
            explicit.append((dict(row), 'requested'))
    for symbol in symbols:
        path, name = symbol.rsplit(':', 1) if ':' in symbol else (None, symbol)
        suffix = '.' + name
        rows = graph.db.execute('''SELECT * FROM nodes WHERE kind IN ('function','class')
            AND (name=? OR substr(name,-?)=?) AND (? IS NULL OR path=?)
            ORDER BY name!=?, length(name), path, start LIMIT 5''',
            (name, len(suffix), suffix, path, path, name)).fetchall()
        if not rows and path is None and '.' in name:
            parts = name.split('.')
            for split in range(len(parts)-1, 0, -1):
                module_path = '/'.join(parts[:split])+'.py'
                leaf = '.'.join(parts[split:])
                rows = graph.db.execute('''SELECT * FROM nodes WHERE name=?
                    AND (path=? OR substr(path,-?)=?) ORDER BY path,start LIMIT 5''',
                    (leaf, module_path, len(module_path)+1, '/'+module_path)).fetchall()
                if rows:
                    break
        if not rows:
            missing.append({'symbol': symbol, 'reason': 'not_found'})
        else:
            # Ambiguity is visible through distinct IDs; never silently choose one file.
            explicit.extend((dict(row), 'symbol') for row in rows[:4])
            if len(rows)>4:
                missing.append({'symbol': symbol, 'reason': 'ambiguous_more_matches',
                                'hint': 'Qualify with path.py:symbol'})
    groups = []
    actual_mode = 'lookup'
    actual_model = None
    for query in queries:
        candidates, provider, actual_mode = graph._search_candidates(query, limit, mode, model)
        actual_model = getattr(provider, 'model_name', provider.identity) if provider is not None else None
        groups.append([(row, 'search') for row, reason in candidates
                       if reason == 'search' and row['kind'] != 'file'])
        if not groups[-1]:
            groups[-1] = [(row, 'search') for row, reason in candidates if reason == 'search']
    # Interleave independent questions so the first cannot consume every selected slot.
    ranked = explicit + [group[index] for index in range(max(map(len, groups), default=0))
                         for group in groups if index < len(group)]
    selected, seen, deferred = [], set(), []
    for row, reason in ranked:
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        if len(selected) < limit:
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


def relationships(graph, selected, maximum):
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
    return hints
