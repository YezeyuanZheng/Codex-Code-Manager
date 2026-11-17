"""Budgeted keyword and embedding retrieval for graph nodes."""
import json
import os
from .locking import snapshot_lock
from .source_coverage import SourceCoverage, add_reference, source_item


class GraphSearch:
    def get_context(self, query=None, queries=None, symbols=None, node_ids=None, offsets=None,
                    budget=None, limit=None, mode=None, model=None, include_related=True, budget_bytes=None,
                    bm25_only=False, context_token=None, include_seen=False, **unknown):
        from .code_context import get_context
        if unknown:
            raise ValueError('Unknown get_context parameter(s): '+', '.join(sorted(unknown))+
                             '. Repository is already bound; omit repo. Use budget for byte limits. '
                             'Example: {"query":"QUESTION","budget":12000,"mode":"bm25"}')
        with snapshot_lock(self.cache_dir):
            self._context_queries = ([query] if query else []) + (queries or []) if isinstance(queries, (list, type(None))) else []
            self._prepared_query_vectors = {}
            try:
                return get_context(self, query, queries, symbols, node_ids, offsets,
                                   budget, limit, mode, model, include_related, budget_bytes,
                                   bm25_only, context_token, include_seen)
            finally:
                self._context_queries = None
                self._prepared_query_vectors = {}

    def retrieve_nodes(self, node_ids, budget=6000, limit=8, include_related=True, offset=0, include_requested=True):
        from .node_context import retrieve
        with snapshot_lock(self.cache_dir):
            return retrieve(self, node_ids, budget, limit, include_related, offset, include_requested)

    def embedding_provider(self, model=None):
        # Preserve injected providers used by library clients and offline tests.
        if self._embedder is not None and self._provider_model is None and model is None and self.embedding_model is None:
            return self._embedder
        from .config import selected_model
        from .embeddings import FastEmbedder
        selected, _ = selected_model(self.repo, model if model is not None else self.embedding_model)
        if self._embedder is None or self._provider_model != selected:
            provider = FastEmbedder(selected)
            self._embedder = provider
            self._provider_model = selected
        return self._embedder

    def embed(self, model=None, progress=None):
        from .embeddings import sync_embeddings
        with snapshot_lock(self.cache_dir):
            return sync_embeddings(self, self.embedding_provider(model), progress=progress)

    def search(self, query, budget=6000, limit=8, mode=None, model=None):
        with snapshot_lock(self.cache_dir):
            return self._search(query, budget, limit, mode, model)

    def _search_candidates(self, query, limit=8, mode=None, model=None):
        if not 1 <= limit <= 50:
            raise ValueError('limit must be 1..50')
        from .retrieval_settings import selected_mode
        mode = selected_mode(self.repo, mode)
        if mode not in ('hybrid', 'bm25', 'semantic'):
            raise ValueError('mode must be hybrid, bm25, or semantic')
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Query must be nonempty text')
        from .graph import terms
        tokens = terms(query).split()[:40]
        if not tokens and mode == 'bm25':
            raise ValueError('BM25 query must contain letters or numbers')
        match = ' OR '.join('"'+t+'"' for t in tokens)
        pool = max(30, limit*4)
        lexical = self.db.execute('''SELECT nodes.*, bm25(search,0,8,3,1) AS rank FROM search
            JOIN nodes ON search.id=nodes.id WHERE search MATCH ? ORDER BY rank, nodes.id LIMIT ?''', (match, pool)).fetchall() if tokens and mode != 'semantic' else []
        from .embeddings import PASSAGE_CHARS, dense_search, fuse, passages
        provider = self.embedding_provider(model) if mode != 'bm25' else None
        semantic = dense_search(self, provider, query, pool) if provider is not None else []
        semantic_info = {ident: (score, chunk) for ident, score, chunk in semantic}
        ranked = fuse([r['id'] for r in lexical], [r[0] for r in semantic], limit)
        candidates = []
        known = {ident for ident, _ in ranked}
        hits = []
        for ident, signals in ranked:
            row = dict(self.db.execute('SELECT * FROM nodes WHERE id=?', (ident,)).fetchone())
            # Return the best matching part of long symbols, retaining its actual lines.
            if ident in semantic_info:
                _, chunk = semantic_info[ident]
                row['_offset'] = chunk * PASSAGE_CHARS
                row['_length'] = len(list(passages(row))[chunk][3])
            row['signals'] = signals
            hits.append(row)
            candidates.append((row, 'search'))
        for hit in hits:
            rows = self.db.execute('''SELECT nodes.* FROM edges JOIN nodes ON nodes.id =
                CASE WHEN edges.source=? THEN edges.target ELSE edges.source END
                WHERE (edges.source=? OR edges.target=?) AND edges.kind!='contains'
                ORDER BY nodes.id LIMIT 4''', (hit['id'], hit['id'], hit['id'])).fetchall()
            for row in rows:
                if row['id'] not in known:
                    known.add(row['id'])
                    candidates.append((dict(row), 'graph_neighbor'))
        return candidates, provider, mode

    def _search(self, query, budget=6000, limit=8, mode=None, model=None):
        if not 256 <= budget <= 100000 or not 1 <= limit <= 50:
            raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
        candidates, provider, mode = self._search_candidates(query, limit, mode, model)
        # Budget includes serialized metadata and escaping, not just source bodies.
        output = {'results': [], 'budget_bytes': budget, 'truncated': False, 'mode': mode,
                  'model': getattr(provider, 'model_name', provider.identity) if provider is not None else None}
        coverage = SourceCoverage()
        excerpts = ((row, reason, start, end) for row, reason in candidates
                    for start, end in (list(coverage.uncovered(row, row.get('_offset', 0), row.get('_length')))
                                       or [(None, None)]))
        for row, reason, start, end in excerpts:
            if start is None:
                add_reference(output, row, reason, coverage, budget, row.get('_offset', 0), row.get('_length'))
                continue
            item = {k: row[k] for k in ('id', 'path', 'name', 'kind')}
            item.update(source_item(row, start, end))
            item['request_mapping'] = json.loads(row['request_mapping'])
            item['reason'] = reason
            item['signals'] = row.get('signals', ['graph'])
            item['truncated'] = False
            output['results'].append(item)
            if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                coverage.add(row, start, len(item['body']))
                continue
            item['truncated'] = True
            output['truncated'] = True
            body = item['body']
            lo, hi = 0, len(body)
            while lo < hi:
                mid = (lo+hi+1)//2
                item.update(source_item(row, start, start+mid))
                if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                    lo = mid
                else:
                    hi = mid-1
            item.update(source_item(row, start, start+lo))
            if not lo:
                output['results'].pop()
            break
        return output
