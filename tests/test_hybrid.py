"""Controlled vectors test ranking/cache correctness, not model relevance quality."""
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codemanager.embeddings import EmbeddingError, FastEmbedder, fuse, normalize
from codemanager.graph import Graph
from codemanager.__main__ import serve


class ControlledEmbedder:
    identity = 'controlled-test-model-v1'

    def __init__(self):
        self.documents_seen = []

    def documents(self, texts):
        self.documents_seen.extend(texts)
        return [[1, 0, 0] if 'ticket' in text or 'TAIL_MARKER' in text else [0, 1, 0] for text in texts]

    def query(self, text):
        return [1, 0, 0]


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'access.py').write_text('def permit(ticket):\n    return ticket.signature == expected\n')
        (self.root / 'billing.py').write_text('def settle(invoice):\n    return gateway.capture(invoice)\n')
        self.provider = ControlledEmbedder()
        self.graph = Graph(self.root, embedder=self.provider)
        self.addCleanup(self.graph.close)
        self.graph.index()

    def test_semantic_hit_with_no_keyword_overlap(self):
        query = 'allow people into protected areas'
        self.assertFalse(self.graph.search(query, mode='bm25')['results'])
        result = self.graph.search(query, mode='hybrid')
        self.assertEqual(result['mode'], 'hybrid')
        self.assertEqual(result['results'][0]['name'], 'permit')
        self.assertEqual(result['results'][0]['signals'], ['embedding'])

    def test_fusion_promotes_agreement_and_preserves_exact_matches(self):
        result = fuse(['exact', 'both'], ['both', 'concept'], 3)
        self.assertEqual(result[0], ('both', ['bm25', 'embedding']))
        self.assertEqual({ident for ident, _ in result}, {'exact', 'both', 'concept'})
        result = self.graph.search('settle', mode='hybrid')
        self.assertTrue(any(row['name']=='settle' and 'bm25' in row['signals'] for row in result['results']))

    def test_unchanged_embeddings_are_reused_across_connections(self):
        self.graph.search('allow people', mode='hybrid')
        original_count = len(self.provider.documents_seen)
        self.assertEqual(original_count, 2)
        self.graph.search('protect this endpoint', mode='hybrid')
        self.assertEqual(len(self.provider.documents_seen), original_count)
        graph = Graph(self.root, embedder=self.provider)
        try:
            graph.search('protect this endpoint', mode='hybrid')
            self.assertEqual(len(self.provider.documents_seen), original_count)
        finally:
            graph.close()

    def test_edit_reembeds_only_changed_file_and_deletion_removes_vector(self):
        self.graph.embed()
        self.provider.documents_seen.clear()
        (self.root / 'access.py').write_text('def permit(ticket):\n    return ticket.valid\n')
        self.graph.index()
        self.graph.embed()
        self.assertEqual(len(self.provider.documents_seen), 1)
        self.assertIn('ticket.valid', self.provider.documents_seen[0])
        (self.root / 'access.py').unlink()
        self.graph.index()
        result = self.graph.search('protect endpoints', mode='semantic')
        self.assertFalse(result['results'])
        self.assertFalse(self.graph.db.execute("SELECT * FROM embeddings WHERE node_id LIKE 'access.py%'").fetchall())

    def test_unchanged_symbol_in_edited_file_keeps_its_vector(self):
        original = 'def permit(ticket):\n    return ticket.valid\n\ndef untouched():\n    return 1\n'
        (self.root / 'access.py').write_text(original)
        self.graph.index()
        self.graph.embed()
        self.provider.documents_seen.clear()
        (self.root / 'access.py').write_text(original.replace('ticket.valid', 'ticket.fresh'))
        self.graph.index()
        self.graph.embed()
        self.assertTrue(any('\nfunction permit\n' in text and 'ticket.fresh' in text for text in self.provider.documents_seen))
        self.assertFalse(any('\nfunction untouched\n' in text for text in self.provider.documents_seen))

    def test_model_change_rebuilds_and_discards_old_vectors(self):
        self.graph.embed()
        self.provider.documents_seen.clear()
        self.provider.identity = 'controlled-test-model-v2'
        self.graph.embed()
        self.assertEqual(len(self.provider.documents_seen), 2)
        models = [row[0] for row in self.graph.db.execute('SELECT DISTINCT model FROM embeddings')]
        self.assertEqual(models, ['controlled-test-model-v2'])

    def test_invalid_vectors_roll_back_without_partial_cache(self):
        with patch.object(self.provider, 'documents', return_value=[[float('nan')], [1]]):
            with self.assertRaises(EmbeddingError):
                self.graph.embed()
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM embeddings').fetchone()[0], 0)
        with patch.object(self.provider, 'documents', return_value=[[1]]):
            with self.assertRaisesRegex(EmbeddingError, 'wrong number'):
                self.graph.embed()

    def test_query_dimension_mismatch_fails_explicitly(self):
        self.graph.embed()
        with patch.object(self.provider, 'query', return_value=[1, 0]):
            with self.assertRaisesRegex(EmbeddingError, 'dimensions differ'):
                self.graph.search('protected endpoint', mode='hybrid')

    def test_long_symbol_returns_matching_tail_and_stays_in_budget(self):
        (self.root / 'access.py').write_text('def permit(ticket):\n' + '    value = 123456789\n'*180 + '    return "TAIL_MARKER"\n')
        self.graph.index()
        # Ignore the header's ticket so this test can select only the matching tail.
        with patch.object(self.provider, 'documents', side_effect=lambda texts: [[1, 0, 0] if 'TAIL_MARKER' in t else [0, 1, 0] for t in texts]):
            result = self.graph.search('modify the final condition', budget=6000, mode='semantic')
        hit = result['results'][0]
        self.assertIn('TAIL_MARKER', hit['body'])
        self.assertGreater(hit['start'], 1)
        for budget in (256, 600, 2000):
            result = self.graph.search('protected endpoint', budget=budget, mode='hybrid')
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), budget)

    def test_keyword_mode_needs_no_embedding_provider_and_reports_missing_setup(self):
        with patch('codemanager.embeddings.FastEmbedder', side_effect=EmbeddingError('missing dependency')):
            graph = Graph(self.root)
            try:
                self.assertTrue(graph.search('settle', mode='bm25')['results'])
                with self.assertRaisesRegex(EmbeddingError, 'missing dependency'):
                    graph.search('settle', mode='hybrid')
            finally:
                graph.close()

    def test_snapshot_remains_independent_of_local_vectors(self):
        self.graph.export()
        folder = self.root / '.codemanager' / 'index'
        before = {p.name:p.read_bytes() for p in folder.iterdir()}
        self.graph.embed()
        self.graph.export()
        self.assertEqual(before, {p.name:p.read_bytes() for p in folder.iterdir()})

    def test_mcp_uses_hybrid_and_preserves_json_protocol(self):
        request = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'search_code','arguments':{'query':'allow people into protected areas'}}}
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(request)+'\n')), patch('sys.stdout', output):
            serve(self.graph, mode='hybrid')
        response = json.loads(output.getvalue())['result']
        self.assertFalse(response['isError'])
        self.assertEqual(json.loads(response['content'][0]['text'])['results'][0]['name'], 'permit')

    def test_local_adapter_calls_query_and_passage_methods(self):
        model = SimpleNamespace(passage_embed=lambda texts, **kwargs: [[3,4] for _ in texts], query_embed=lambda text: iter([[3,4]]))
        fake_module = SimpleNamespace(TextEmbedding=lambda **kwargs: model)
        with patch('codemanager.embeddings.version', return_value='test'), patch.dict('sys.modules', {'fastembed':fake_module}):
            provider = FastEmbedder()
            self.assertEqual(provider.documents(['code']), [[3,4]])
            self.assertEqual(provider.query('direction'), [3,4])
        self.assertEqual(normalize([3,4]), [0.6,0.8])

    @unittest.skipUnless(os.environ.get('CODEMANAGER_REAL_EMBEDDINGS')=='1', 'Requires downloaded FastEmbed model; opt in with CODEMANAGER_REAL_EMBEDDINGS=1')
    def test_real_model_feature_direction(self):
        (self.root / 'access.py').write_text('def permit(ticket):\n    """Verify a signed credential before granting access to a protected resource."""\n    return ticket.signature == expected\n')
        self.graph.index()
        self.graph._embedder = FastEmbedder()
        result = self.graph.search('Add a check to stop unauthenticated users from opening private pages', mode='hybrid')
        self.assertTrue(any(row['path']=='access.py' and 'embedding' in row['signals'] for row in result['results']))


if __name__ == '__main__':
    unittest.main()
