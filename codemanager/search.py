"""Budgeted keyword and embedding retrieval for graph nodes."""
import json
import os
from .locking import snapshot_lock


class GraphSearch:
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

    def embed(self, model=None):
        from .embeddings import sync_embeddings
        with snapshot_lock(self.cache_dir):
            return sync_embeddings(self, self.embedding_provider(model))

    def search(self, query, budget=6000, limit=8, mode=None, model=None):
        with snapshot_lock(self.cache_dir):
            return self._search(query, budget, limit, mode, model)

    def _search(self, query, budget=6000, limit=8, mode=None, model=None):
        if not 256 <= budget <= 100000 or not 1 <= limit <= 50:
            raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
        mode = mode or os.environ.get('CODEMANAGER_SEARCH_MODE', 'hybrid')
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
        from .embeddings import dense_search, fuse, passages
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
                _, row['start'], row['end'], row['body'] = list(passages(row))[chunk]
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
        # Budget includes serialized metadata and escaping, not just source bodies.
        output = {'results': [], 'budget_bytes': budget, 'truncated': False, 'mode': mode,
                  'model': getattr(provider, 'model_name', provider.identity) if provider is not None else None}
        for row, reason in candidates:
            item = {k: row[k] for k in ('id', 'path', 'name', 'kind', 'start', 'end', 'body')}
            item['request_mapping'] = json.loads(row['request_mapping'])
            item['reason'] = reason
            item['signals'] = row.get('signals', ['graph'])
            item['truncated'] = False
            output['results'].append(item)
            if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                continue
            item['truncated'] = True
            output['truncated'] = True
            body = item['body']
            lo, hi = 0, len(body)
            while lo < hi:
                mid = (lo+hi+1)//2
                item['body'] = body[:mid]
                if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                    lo = mid
                else:
                    hi = mid-1
            item['body'] = body[:lo]
            if not lo:
                output['results'].pop()
            break
        return output
