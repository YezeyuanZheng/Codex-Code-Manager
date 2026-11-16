"""Bounded, explicit retrieval of selected graph nodes and their one-hop context."""
import json

from .embeddings import PASSAGE_CHARS
from .source_coverage import SourceCoverage, add_reference, source_item


def retrieve(graph, node_ids, budget=6000, limit=8, include_related=True, offset=0, include_requested=True):
    if (not isinstance(node_ids, list) or not 1 <= len(node_ids) <= 20 or
        any(not isinstance(node, str) or not node for node in node_ids)):
        raise ValueError('node_ids must contain 1..20 nonempty node IDs')
    if type(budget) is not int or not 256 <= budget <= 100000 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
    if type(include_related) is not bool or type(include_requested) is not bool:
        raise ValueError('include_related and include_requested must be boolean')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative character offset')
    selected = list(dict.fromkeys(node_ids))
    rows = {}
    for ident in selected:
        row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown node ID: {ident}; refresh search to get current node IDs')
        rows[ident] = dict(row)
    candidates = [(rows[ident], 'requested_node', None) for ident in selected] if include_requested else []
    if include_related:
        for ident in selected:
            # Candidate relations are conservative hints, not verified call/dataflow edges.
            edges = graph.db.execute('''SELECT source,target,kind FROM edges
                WHERE source=? OR target=? ORDER BY kind='contains',kind,source,target LIMIT ?''',
                (ident, ident, limit*4)).fetchall()
            for edge in edges:
                other = edge['target'] if edge['source'] == ident else edge['source']
                if other in rows:
                    continue
                row = graph.db.execute('SELECT * FROM nodes WHERE id=?', (other,)).fetchone()
                if row is None:
                    continue
                rows[other] = dict(row)
                via = {'node_id': ident, 'kind': edge['kind'],
                       'direction': 'outgoing' if edge['source'] == ident else 'incoming'}
                candidates.append((rows[other], 'related_node', via))
    output = {'results': [], 'budget_bytes': budget, 'truncated': False,
              'scope': 'selected_nodes_and_one_hop_candidates'}
    coverage = SourceCoverage()
    excerpts = ((row, reason, via, start, end) for row, reason, via in candidates
                for start, end in (list(coverage.uncovered(row, offset if reason == 'requested_node' else 0,
                                                          PASSAGE_CHARS)) or [(None, None)]))
    for row, reason, via, current, stop in excerpts:
        if current is None:
            add_reference(output, row, reason, coverage, budget,
                          offset if reason == 'requested_node' else 0, PASSAGE_CHARS, via)
            continue
        if len(output['results']) >= limit:
            output['truncated'] = True
            break
        # Long nodes start with one passage; do not dump the whole definition.
        body = row['body'][current:stop]
        item = {key: row[key] for key in ('id', 'path', 'name', 'kind')}
        item.update(source_item(row, current, stop))
        item.update(node_start=row['start'], node_end=row['end'], reason=reason, via=via,
                    truncated=current > 0 or len(body) < len(row['body']),
                    request_mapping=json.loads(row['request_mapping']))
        output['results'].append(item)
        if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
            coverage.add(row, current, len(item['body']))
            output['truncated'] |= item['truncated']
            # Setting the flag changes false to true, reducing serialized size.
            continue
        output['truncated'] = True
        item['truncated'] = True
        lo, hi = 0, len(body)
        while lo < hi:
            mid = (lo+hi+1)//2
            item.update(source_item(row, current, current+mid))
            if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                lo = mid
            else:
                hi = mid-1
        item.update(source_item(row, current, current+lo))
        if not lo:
            output['results'].pop()
        break
    return output
