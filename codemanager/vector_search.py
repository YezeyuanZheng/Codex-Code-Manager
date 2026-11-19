"""Reusable decoded vectors and batched queries, invalidated by SQLite changes."""
import math
from heapq import nsmallest


def stamp(graph):
    return tuple(graph.db.execute("SELECT key,value FROM retrieval_counters WHERE key IN ('retrieval_revision','vector_revision') ORDER BY key").fetchall())


def search(graph, embedder, query, limit):
    from .embeddings import EmbeddingError, normalize, sync_embeddings, unpack
    key = embedder.identity, stamp(graph)
    if getattr(graph, '_embedding_sync_key', None) != key:
        sync_embeddings(graph, embedder)
        graph._embedding_sync_key = embedder.identity, stamp(graph)
    queries = getattr(graph, '_context_queries', None) or [query]
    prepared = getattr(graph, '_prepared_query_vectors', {})
    query_key = embedder.identity, query
    if query_key not in prepared:
        texts = list(dict.fromkeys(queries))
        batch = getattr(embedder, 'queries', None)
        vectors = list(batch(texts)) if batch is not None else [embedder.query(text) for text in texts]
        if len(vectors) != len(texts):
            raise EmbeddingError('Query embedder returned the wrong number of vectors')
        prepared.update({(embedder.identity, text): normalize(v) for text, v in zip(texts, vectors, strict=True)})
        graph._prepared_query_vectors = prepared
    vector = prepared[query_key]
    # Search() without a context batch must not retain an unbounded history of queries.
    if not getattr(graph, '_context_queries', None):
        graph._prepared_query_vectors = {}
    key = embedder.identity, stamp(graph)
    cache = getattr(graph, '_decoded_vector_cache', None)
    if cache is None or cache[0] != key:
        rows = graph.db.execute('SELECT node_id,chunk,vector FROM embeddings WHERE model=? ORDER BY node_id,chunk',
                                (embedder.identity,)).fetchall()
        blobs = [row['vector'] for row in rows]
        for blob in blobs:
            if len(blob) != 4 * len(vector):
                unpack(blob)  # Preserve the invalid-cache diagnostic for malformed blobs.
                raise EmbeddingError('Query and cached vector dimensions differ; rebuild with a consistent model')
        try:
            import numpy as np
        except ImportError:
            matrix = [unpack(blob) for blob in blobs]
        else:
            matrix = np.frombuffer(b''.join(blobs), dtype='<f4').reshape((len(rows), len(vector)))
        locations = [(row['node_id'], row['chunk']) for row in rows]
        cache = graph._decoded_vector_cache = key, locations, matrix, len(vector)
    _, locations, matrix, dimension = cache
    if dimension != len(vector):
        raise EmbeddingError('Query and cached vector dimensions differ; rebuild with a consistent model')
    if isinstance(matrix, list):
        scores = [math.fsum(a*b for a, b in zip(vector, values, strict=True)) for values in matrix]
    else:
        # einsum avoids starting a large BLAS thread pool for one query vector.
        import numpy as np
        scores = np.einsum('ij,j->i', matrix, vector, dtype='float64')
    best = {}
    for (node_id, chunk), score in zip(locations, scores, strict=True):
        if not math.isfinite(score):
            raise EmbeddingError('Cached vector contains nonfinite values')
        if score > 0 and (node_id not in best or score > best[node_id][0]):
            best[node_id] = float(score), chunk
    return [(ident, score, chunk) for ident, (score, chunk) in
            nsmallest(limit, best.items(), key=lambda item: (-item[1][0], item[0]))]
