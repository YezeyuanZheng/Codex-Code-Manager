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


def pack(specs, grants, metadata, budget, seen=None):
    output = {key: value.copy() if isinstance(value, list) else value for key, value in metadata.items()}
    output['results'] = []
    from .context_receipts import coverage as prior_coverage
    coverage = prior_coverage(seen or {})
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
            item['citation'] = item['path']+':'+str(item['start'])
            if spec.get('call_sites'):
                item['call_sites'] = spec['call_sites']
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
    if pending:
        output.setdefault('pending', []).extend(pending)
    return output


def get_context(graph, query=None, queries=None, symbols=None, node_ids=None, offsets=None,
                budget=None, limit=None, mode=None, model=None, include_related=True, budget_bytes=None,
                bm25_only=False, context_token=None, include_seen=False):
    if budget_bytes is not None:
        if type(budget_bytes) is not int:
            raise ValueError('budget_bytes must be an integer alias for budget')
        if budget is not None and (type(budget) is not int or budget != budget_bytes):
            raise ValueError('budget and budget_bytes must match when both are supplied')
        budget = budget_bytes
    budget = 12000 if budget is None else budget
    queries = strings(queries, 'queries', 8)
    symbols = strings(symbols, 'symbols', 20)
    node_ids = strings(node_ids, 'node_ids', 20)
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query must be nonempty text')
        queries = strings([query]+queries, 'queries', 8)
    if not queries and not symbols and not node_ids:
        raise ValueError('Provide query, queries, symbols, or node_ids')
    if limit is None:
        limit = min(50, max(8, budget//1500 if type(budget) is int else 8,
                            len(node_ids)+len(symbols)+min(2,len(queries))))
    if type(budget) is not int or not 256 <= budget <= 100000 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
    if type(bm25_only) is not bool or type(include_seen) is not bool:
        raise ValueError('bm25_only and include_seen must be boolean')
    if bm25_only:
        if mode not in (None, 'bm25'):
            raise ValueError('bm25_only conflicts with mode; use mode=bm25 or omit mode')
        mode = 'bm25'
    if type(include_related) is not bool:
        raise ValueError('include_related must be boolean')
    if mode not in (None, 'hybrid', 'bm25', 'semantic'):
        raise ValueError('mode must be hybrid, bm25, or semantic')
    offsets = {} if offsets is None else offsets
    if (not isinstance(offsets, dict) or any(key not in node_ids or type(value) is not int or value < 0
                                           for key, value in offsets.items())):
        raise ValueError('offsets must map requested node_ids to nonnegative character offsets')
    from . import context_receipts
    previous = context_receipts.load(graph, context_token)
    seen = {} if include_seen else previous
    receipt = context_receipts.token() if budget >= 1000 else None
    specs, missing, deferred, actual_mode, actual_model = select(graph, queries, symbols, node_ids, offsets,
                                                                limit, mode, model, include_related)
    metadata = {'budget': budget, 'truncated': bool(deferred), 'mode': actual_mode, 'model': actual_model}
    if receipt:
        metadata['context_token'] = receipt
    append_metadata(metadata, 'unresolved', missing, budget)
    append_metadata(metadata, 'pending', deferred, budget)
    # Reserve a small part of the same budget for inspected relationships, never full containers.
    if include_related:
        hints = relationships(graph, specs, min(limit*2, 20), ' '.join(queries))
        known = {spec['row']['id'] for spec in specs}
        for hint in hints:
            if hint.get('call_sites'):
                for spec in specs:
                    if spec['row']['id'] == hint['id']:
                        spec.setdefault('call_sites', []).extend(hint['call_sites'])
        for hint in hints:
            if len(specs) >= limit:
                break
            if hint['kind'] == 'contains' or hint['id'] in known:
                continue
            row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (hint['id'],)).fetchone()
            if row is not None and row['kind'] in ('function', 'class'):
                known.add(row['id'])
                specs.append({'row': dict(row), 'reason': 'related', 'offset': 0,
                              'length': min(6000, len(row['body'])),
                              'call_sites': hint.get('call_sites', [])})
        # Restore search selections when relationships do not fill the reserved slots.
        for item in deferred:
            if len(specs) >= limit:
                break
            if item['id'] in known:
                continue
            row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (item['id'],)).fetchone()
            known.add(item['id'])
            specs.append({'row': dict(row), 'reason': 'search', 'offset': item['offset'],
                          'length': min(6000, len(row['body'])-item['offset'])})
        selected_ids = {spec['row']['id'] for spec in specs}
        if 'pending' in metadata:
            metadata['pending'] = [item for item in metadata['pending'] if item['id'] not in selected_ids]
            metadata['truncated'] = (any(item['id'] not in selected_ids for item in deferred) or
                                     len(metadata.get('unresolved', [])) < len(missing))
            if not metadata['pending']:
                del metadata['pending']
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
        if size(pack(specs, trial, metadata, budget, seen)) <= budget:
            grants = trial
    # Explicit requests first, then implementation relationships, then search matches.
    # Otherwise small secondary hits can starve the integration bodies we expanded.
    for index in sorted(range(len(specs)), key=lambda i: (
            0 if specs[i]['reason'] in ('requested', 'symbol') else
            2 if specs[i]['reason']=='related' else 1,
            specs[i]['length']-grants[i])):
        trial = grants.copy()
        trial[index] = specs[index]['length']
        if size(pack(specs, trial, metadata, budget, seen)) <= budget:
            grants = trial
    # Use remaining space on the highest-priority incomplete selection.
    for index, spec in enumerate(specs):
        low, high = grants[index], spec['length']
        while low < high:
            middle = (low+high+1)//2
            trial = grants.copy()
            trial[index] = middle
            if size(pack(specs, trial, metadata, budget, seen)) <= budget:
                low = middle
            else:
                high = middle-1
        grants[index] = low
    output = pack(specs, grants, metadata, budget, seen)
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
    if receipt:
        context_receipts.save(graph, receipt, previous, output)
    return output
