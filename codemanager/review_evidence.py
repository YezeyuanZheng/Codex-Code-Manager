"""Read-only, budgeted source/graph evidence for the isolated reviewer."""
import json
from pathlib import Path
import sqlite3
import sys

from .node_context import retrieve
from .search import GraphSearch

BUDGET = 6000
DIFF_PREVIEW_BYTES = 2000


class EvidenceGraph(GraphSearch):
    def __init__(self, repo, cache):
        self.repo = Path(repo).resolve(strict=True)
        path = Path(cache).resolve(strict=True)
        self.db = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)
        self.db.row_factory = sqlite3.Row

    def search(self, query, budget=BUDGET, limit=8):
        # No model downloads, vector updates, refreshes or filesystem locks in a reviewer.
        return self._search(query, budget, limit, mode='bm25')

    def retrieve_nodes(self, node_ids, budget=BUDGET, limit=8, include_related=True, offset=0, include_requested=True):
        return retrieve(self, node_ids, budget, limit, include_related, offset, include_requested)

    def close(self):
        self.db.close()


def read_evidence(repo, cache, *, query=None, node_ids=None, diff_path=None,
                  budget=BUDGET, limit=8, offset=0, related_only=False):
    if type(budget) is not int or not 256 <= budget <= BUDGET:
        raise ValueError('Reviewer evidence budget must be 256..6000 bytes')
    if sum(arg is not None for arg in (query, node_ids, diff_path)) != 1:
        raise ValueError('Choose a query, node IDs, or a diff path')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative character offset')
    graph = EvidenceGraph(repo, cache)
    try:
        if query is not None:
            return graph.search(query, budget, limit)
        if node_ids is not None:
            return graph.retrieve_nodes(node_ids, budget, limit, offset=offset, include_requested=not related_only)
        row = graph.db.execute('SELECT patch FROM review_diffs WHERE path=?', (diff_path,)).fetchone()
        if row is None:
            raise ValueError('Unknown changed file in review evidence')
        patch = row[0]
        if offset > len(patch):
            raise ValueError('offset exceeds diff length')
        output = {'path': diff_path, 'offset': offset, 'next_offset': offset,
                  'patch': '', 'truncated': offset < len(patch), 'budget_bytes': budget}
        if len(json.dumps(output, ensure_ascii=False).encode()) > budget:
            raise ValueError('Diff metadata exceeds budget; use a larger budget')
        lo, hi = 0, len(patch)-offset
        while lo < hi:
            mid = (lo+hi+1)//2
            output.update(patch=patch[offset:offset+mid], next_offset=offset+mid, truncated=offset+mid < len(patch))
            if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                lo = mid
            else:
                hi = mid-1
        output.update(patch=patch[offset:offset+lo], next_offset=offset+lo,
                      truncated=offset+lo < len(patch))
        return output
    finally:
        graph.close()


def prepare_evidence(graph, folder, requirements, mapped_nodes, patches, paths=None):
    """Keep source in a local cache, sending only bounded initial evidence to the model."""
    cache = folder / 'evidence.sqlite3'
    target = sqlite3.connect(cache)
    try:
        with target:
            graph.db.backup(target)
            target.execute('CREATE TABLE review_diffs(path TEXT PRIMARY KEY, patch TEXT)')
            target.executemany('INSERT INTO review_diffs VALUES (?,?)', sorted(patches.items()))
    finally:
        target.close()
    # Requirement text and mapped symbol names provide a deterministic, offline starting query.
    query = ' '.join(requirements + [item['name'] for item in mapped_nodes] + list(paths or [])) or 'code requirements'
    initial = graph.search(query, budget=2800, limit=4, mode='bm25')
    ids = [item['id'] for item in mapped_nodes[:1]]
    if not ids:
        ids = [item['id'] for item in initial['results'] if item['kind'] != 'file'][:1]
    related = graph.retrieve_nodes(ids, budget=2800, limit=4, include_requested=False) if ids else {'results': []}
    evidence = {'budget_bytes': BUDGET, 'search': initial, 'node_context': related}
    if len(json.dumps(evidence, ensure_ascii=False).encode()) > BUDGET:
        raise ValueError('Initial review evidence exceeded its context budget')
    module_root = str(Path(__file__).resolve().parent.parent)
    bootstrap = f'import sys; sys.path.insert(0, {module_root!r}); from codemanager.__main__ import main; main()'
    command = [sys.executable, '-c', bootstrap, 'review-evidence', '--repo', str(graph.repo), '--cache', str(cache)]
    return evidence, {'command': command, 'max_budget_bytes': BUDGET, 'mode': 'bm25'}, cache


def diff_preview(patch):
    raw = patch.encode()
    return raw[:DIFF_PREVIEW_BYTES].decode(errors='ignore'), len(raw) > DIFF_PREVIEW_BYTES
