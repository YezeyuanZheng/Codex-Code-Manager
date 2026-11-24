"""Local embeddings, incremental vector caching, and reciprocal rank fusion."""
from contextlib import redirect_stdout
import hashlib
from heapq import heappop, heappush
from importlib.metadata import PackageNotFoundError, version
from itertools import groupby
import math
import os
from pathlib import Path
import struct
import sys

from .config import DEFAULT_MODEL, validate_model
PASSAGE_CHARS = 1200
BATCH_SIZE = 32


class EmbeddingError(ValueError):
    """An explicit semantic-search setup or inference failure."""


class FastEmbedder:
    def __init__(self, model_name=None):
        self.model_name = validate_model(model_name if model_name is not None else os.environ.get('CODEMANAGER_EMBEDDING_MODEL', DEFAULT_MODEL))
        try:
            package_version = version('fastembed')
        except PackageNotFoundError as exc:
            raise EmbeddingError('Hybrid search requires FastEmbed. Install with: python -m pip install -e . ; use --mode bm25 for explicit keyword-only search.') from exc
        self.identity = f'fastembed:{package_version}:{self.model_name}:passages-v1'
        self._model = None

    def model(self):
        if self._model is None:
            try:
                cache = Path(os.environ.get('CODEMANAGER_MODEL_CACHE', Path.home() / '.cache' / 'codemanager' / 'models'))
                # Model initialization may download weights on its first use.
                with redirect_stdout(sys.stderr):
                    from fastembed import TextEmbedding
                    self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(cache),
                                                threads=2, providers=['CPUExecutionProvider'])
            except Exception as exc:
                raise EmbeddingError(f'Cannot load local embedding model {self.model_name}. First use requires a model download; check network access and CODEMANAGER_MODEL_CACHE. {exc}') from exc
        return self._model

    def documents(self, texts):
        try:
            with redirect_stdout(sys.stderr):
                return list(self.model().passage_embed(texts, batch_size=BATCH_SIZE))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f'Local document embedding failed: {exc}') from exc

    def query(self, text):
        try:
            with redirect_stdout(sys.stderr):
                return next(iter(self.model().query_embed(text)))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f'Local query embedding failed: {exc}') from exc


    def queries(self, texts):
        try:
            with redirect_stdout(sys.stderr):
                return list(self.model().query_embed(texts, batch_size=BATCH_SIZE))
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f'Local query embedding failed: {exc}') from exc


def normalize(vector):
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingError('Embedding must contain numeric values') from exc
    if not values or not all(math.isfinite(value) for value in values):
        raise EmbeddingError('Embedding must be nonempty and finite')
    norm = math.sqrt(math.fsum(value*value for value in values))
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError('Embedding has an invalid norm')
    return [value/norm for value in values]


def pack(vector):
    return struct.pack(f'<{len(vector)}f', *vector)


def unpack(blob):
    if not blob or len(blob) % 4:
        raise EmbeddingError('Invalid cached vector; remove the local cache and rebuild')
    return struct.unpack(f'<{len(blob)//4}f', blob)


def owned_ranges(nodes):
    """Assign each source line to the most specific searchable node."""
    starts, boundaries = {}, set()
    for node in nodes:
        start, stop = node['start'], node['end'] + 1
        if start >= stop:
            continue
        priority = (node['kind'] == 'chunk', stop - start, node['id'])
        starts.setdefault(start, []).append((priority, stop))
        boundaries.update((start, stop))
    active, ranges = [], {}
    points = sorted(boundaries)
    for start, stop in zip(points, points[1:]):
        for entry in starts.get(start, ()):
            heappush(active, entry)
        while active and active[0][1] <= start:
            heappop(active)
        if active:
            owned = ranges.setdefault(active[0][0][2], [])
            if owned and owned[-1][1] == start:
                owned[-1] = owned[-1][0], stop
            else:
                owned.append((start, stop))
    return ranges


def passage_spans(node, ranges=None):
    """Yield each contiguous passage with its original character offset."""
    header = f"{node['path']}\n{node['kind']} {node['name']}"[:400] + '\n'
    mapping = node['request_mapping'] if 'request_mapping' in node.keys() else '[]'
    if mapping != '[]':
        header += mapping + '\n'
    text = node['body']
    spans = [(0, len(text))]
    if ranges is not None and ranges != [(node['start'], node['end'] + 1)]:
        offsets = [0]
        for line in text.split('\n'):
            offsets.append(offsets[-1] + len(line) + 1)
        spans = [(offsets[start - node['start']], offsets[stop - node['start']] - 1)
                 for start, stop in ranges]
        spans = [(start, stop) for start, stop in spans if text[start:stop].strip()]
    for start, stop in spans:
        for offset in range(start, max(start + 1, stop), PASSAGE_CHARS):
            body = text[offset:min(stop, offset + PASSAGE_CHARS)]
            yield offset, header + body, body


def passages(node):
    line = node['start']
    for _, text, body in passage_spans(node):
        start = line
        line += body.count('\n')
        end = min(line, node['end'])
        yield text, start, end, body


def sync_embeddings(graph, embedder, *, cache_db=None, progress=None):
    """Refresh only missing/stale passages. Cache identity includes the model version."""
    model = embedder.identity
    cache = cache_db if cache_db is not None else graph.db
    # File previews repeat chunks or definitions; keep them out of dense retrieval.
    nodes = graph.db.execute("SELECT * FROM nodes WHERE kind!='file' ORDER BY path,id")
    expected = {}
    for _, group in groupby(nodes, key=lambda node: node['path']):
        file_nodes = list(group)
        ranges = owned_ranges(file_nodes)
        for node in file_nodes:
            for chunk, (_, text, _) in enumerate(passage_spans(node, ranges.get(node['id'], []))):
                expected[(node['id'], chunk)] = (hashlib.sha256(text.encode()).hexdigest(), text)
    rows = cache.execute('SELECT node_id,chunk,digest,vector FROM embeddings WHERE model=?', (model,)).fetchall()
    cached = {(row['node_id'], row['chunk']): row['digest'] for row in rows}
    by_digest = {row['digest']: row['vector'] for row in rows}
    has_reuse = cache.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_reuse'").fetchone()
    if has_reuse:
        by_digest.update({row['digest']:row['vector'] for row in cache.execute(
            'SELECT digest,vector FROM embedding_reuse WHERE model=?', (model,))})
    missing = [(key, digest, text) for key, (digest, text) in expected.items() if cached.get(key) != digest]
    reused = [(key, digest, by_digest[digest]) for key, digest, _ in missing if digest in by_digest]
    missing = [(key, digest, text) for key, digest, text in missing if digest not in by_digest]
    from time import perf_counter
    started = perf_counter()
    def report(completed):
        if progress is not None:
            progress({'completed':completed,'total':len(missing),'reused':len(reused),
                      'elapsed_seconds':round(perf_counter()-started,3)})
    report(0)
    with cache:
        # Keep a single current model cache, not historical vectors.
        cache.execute('DELETE FROM embeddings WHERE model!=?', (model,))
        if has_reuse:
            cache.execute('DELETE FROM embedding_reuse WHERE model!=?', (model,))
        for key in cached.keys() - expected.keys():
            cache.execute('DELETE FROM embeddings WHERE node_id=? AND chunk=? AND model=?', (*key, model))
        for (ident, chunk), digest, vector in reused:
            cache.execute('INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?)',
                          (ident, chunk, model, digest, vector))
        dimension = None
        for offset in range(0, len(missing), BATCH_SIZE):
            batch = missing[offset:offset+BATCH_SIZE]
            vectors = list(embedder.documents([text for _, _, text in batch]))
            if len(vectors) != len(batch):
                raise EmbeddingError('Embedding provider returned the wrong number of vectors')
            for ((ident, chunk), digest, _), vector in zip(batch, vectors, strict=True):
                values = normalize(vector)
                if dimension is not None and dimension != len(values):
                    raise EmbeddingError('Embedding dimensions changed within a batch')
                dimension = len(values)
                cache.execute('INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?)',
                                 (ident, chunk, model, digest, pack(values)))
            report(min(offset+BATCH_SIZE, len(missing)))
    return {'model': model, 'embedded_passages': len(missing), 'cached_passages': len(expected), 'reused_passages': len(reused)}


def dense_search(graph, embedder, query, limit):
    from .vector_search import search
    return search(graph, embedder, query, limit)


def fuse(lexical, semantic, limit, constant=60):
    """Equal-weight RRF avoids mixing incompatible BM25 and cosine score scales."""
    scores, signals = {}, {}
    for label, ranking in (('bm25', lexical), ('embedding', semantic)):
        for rank, ident in enumerate(ranking, 1):
            scores[ident] = scores.get(ident, 0) + 1/(constant+rank)
            signals.setdefault(ident, []).append(label)
    ids = sorted(scores, key=lambda ident: (-scores[ident], ident))[:limit]
    return [(ident, signals[ident]) for ident in ids]
