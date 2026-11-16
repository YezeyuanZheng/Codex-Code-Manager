"""One-call code context with batched lookup, expansion, and a shared byte budget."""
import json

from .context_selection import relationships, select, strings
from .source_coverage import SourceCoverage, add_reference, source_item


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def append_metadata(output, name, items, budget):
    for item in items:
        output.setdefault(name, []).append(item)
        if size(output) > budget:
            output[name].pop()
            if not output[name]:
                del output[name]
            output['truncated'] = True
            break


def pack(specs, grants, metadata, budget):
    output = {key: value.copy() if isinstance(value, list) else value for key, value in metadata.items()}
    output['results'] = []
    coverage = SourceCoverage()
    for spec, length in zip(specs, grants, strict=True):
        row, offset = spec['row'], spec['offset']
        if not length:
            continue
        pieces = list(coverage.uncovered(row, offset, length))
        if not pieces:
            continue
        for start, stop in pieces:
            item = {key: row[key] for key in ('id', 'path', 'name')}
            item.update(source_item(row, start, stop))
            item['truncated'] = start > 0 or stop < len(row['body'])
            item['reason'] = spec['reason']
            mapping = json.loads(row['request_mapping'])
            if mapping:
                item['request_mapping'] = mapping
            output['results'].append(item)
            coverage.add(row, start, stop-start)
    pending = []
    for spec in specs:
        row, offset = spec['row'], spec['offset']
        missing = list(coverage.uncovered(row, offset))
        cursor = missing[0][0] if missing else None
        owned = [item for item in output['results'] if item['id']==row['id']]
        for item in owned:
            item['next_offset'] = cursor
        if missing:
            pending.append({'id': row['id'], 'offset': cursor})
            output['truncated'] = True
        elif not owned:
            add_reference(output, row, spec['reason'], coverage, budget, offset)
    append_metadata(output, 'pending', pending, budget)
    return output


def get_context(graph, query=None, queries=None, symbols=None, node_ids=None, offsets=None,
                budget=12000, limit=8, mode=None, model=None, include_related=True):
    queries = strings(queries, 'queries', 8)
    symbols = strings(symbols, 'symbols', 20)
    node_ids = strings(node_ids, 'node_ids', 20)
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query must be nonempty text')
        queries = strings([query]+queries, 'queries', 8)
    if not queries and not symbols and not node_ids:
        raise ValueError('Provide query, queries, symbols, or node_ids')
    if type(budget) is not int or not 256 <= budget <= 100000 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
    if type(include_related) is not bool:
        raise ValueError('include_related must be boolean')
    if mode not in (None, 'hybrid', 'bm25', 'semantic'):
        raise ValueError('mode must be hybrid, bm25, or semantic')
    offsets = {} if offsets is None else offsets
    if (not isinstance(offsets, dict) or any(key not in node_ids or type(value) is not int or value < 0
                                           for key, value in offsets.items())):
        raise ValueError('offsets must map requested node_ids to nonnegative character offsets')
    specs, missing, deferred, actual_mode, actual_model = select(graph, queries, symbols, node_ids, offsets,
                                                                limit, mode, model)
    metadata = {'budget_bytes': budget, 'truncated': bool(deferred), 'mode': actual_mode, 'model': actual_model}
    append_metadata(metadata, 'unresolved', missing, budget)
    append_metadata(metadata, 'pending', deferred, budget)
    # Reserve a small part of the same budget for inspected relationships, never full containers.
    if include_related:
        hints = relationships(graph, specs, min(limit*2, 20))
        known = {spec['row']['id'] for spec in specs}
        for hint in hints:
            if len(specs) >= limit:
                break
            if hint['kind'] == 'contains' or hint['id'] in known:
                continue
            row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (hint['id'],)).fetchone()
            if row is not None and row['kind'] in ('function', 'class'):
                known.add(row['id'])
                specs.append({'row': dict(row), 'reason': 'related', 'offset': 0,
                              'length': min(6000, len(row['body']))})
        baseline = size(metadata)
        for hint in hints:
            candidate = {**metadata, 'related': metadata.get('related', []) + [hint]}
            if size(candidate)-baseline > min(1000, budget//8) or size(candidate)+20 > budget:
                break
            metadata = candidate
    grants = [0]*len(specs)
    # Give each selection a starting excerpt before spending the rest on complete definitions.
    for index, spec in enumerate(specs):
        trial = grants.copy()
        trial[index] = min(256, spec['length'])
        if size(pack(specs, trial, metadata, budget)) <= budget:
            grants = trial
    # Finishing small functions avoids needless follow-up calls and leaves large nodes bounded.
    for index in sorted(range(len(specs)), key=lambda i: (specs[i]['reason']=='related',
                                                         specs[i]['length']-grants[i])):
        trial = grants.copy()
        trial[index] = specs[index]['length']
        if size(pack(specs, trial, metadata, budget)) <= budget:
            grants = trial
    # Use remaining space on the highest-priority incomplete selection.
    for index, spec in enumerate(specs):
        low, high = grants[index], spec['length']
        while low < high:
            middle = (low+high+1)//2
            trial = grants.copy()
            trial[index] = middle
            if size(pack(specs, trial, metadata, budget)) <= budget:
                low = middle
            else:
                high = middle-1
        grants[index] = low
    output = pack(specs, grants, metadata, budget)
    # Metadata-only replies must obey even a minimum-sized budget.
    while size(output) > budget:
        for name in ('pending', 'related', 'unresolved'):
            if output.get(name):
                output[name].pop()
                if not output[name]:
                    del output[name]
                break
        else:
            raise ValueError('Context metadata cannot fit the requested budget')
        output['truncated'] = True
    return output
