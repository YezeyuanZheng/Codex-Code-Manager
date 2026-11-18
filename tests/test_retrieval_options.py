"""Retrieval behavior across requests, file revisions, modes, and vector batches."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph
from codemanager.context import prompt_hook
from codemanager.retrieval_settings import configure, selected_mode
from codemanager.embeddings import sync_embeddings
from codemanager.__main__ import serve
from test_hybrid import ControlledEmbedder


class RetrievalOptionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root/'app.py'
        self.path.write_text('def first(value):\n    return second(value)\n\ndef second(value):\n    return value + 1\n')
        self.provider = ControlledEmbedder()
        self.graph = Graph(self.root, embedder=self.provider); self.addCleanup(self.graph.close)
        self.graph.export()

    def test_receipt_skips_only_previously_emitted_characters_and_is_opt_in(self):
        first = self.graph.get_context(symbols=['first'], include_related=False)
        follow = self.graph.get_context(symbols=['first','second'], context_token=first['context_token'], include_related=False)
        self.assertEqual([r['name'] for r in follow['results']], ['second'])
        self.assertTrue(any(r['id'].endswith(':first') for r in follow['references']))
        independent = self.graph.get_context(symbols=['first'], include_related=False)
        self.assertEqual(len(independent['results']), 1)
        forced = self.graph.get_context(symbols=['first'], context_token=first['context_token'], include_seen=True)
        self.assertTrue(any(r['name']=='first' for r in forced['results']))
        # Receipts are immutable: a branch from the first receipt has not seen second.
        branch = self.graph.get_context(symbols=['second'], context_token=first['context_token'])
        self.assertTrue(branch['results'])

    def test_receipt_invalidates_edited_file_but_retains_unchanged_files(self):
        (self.root/'other.py').write_text('def stable():\n    return 7\n'); self.graph.index()
        initial = self.graph.get_context(symbols=['first','stable'], include_related=False)
        self.path.write_text('\n'+self.path.read_text().replace('second(value)', 'second(value) + 2', 1))
        self.graph.index()
        result = self.graph.get_context(symbols=['first','stable'], context_token=initial['context_token'], include_related=False)
        self.assertEqual([r['name'] for r in result['results']], ['first'])
        self.assertIn('+ 2', result['results'][0]['body'])
        with self.assertRaisesRegex(ValueError, 'expired or is unknown'):
            self.graph.get_context(symbols=['first'], context_token='0'*32)
        with self.assertRaisesRegex(ValueError, 'context_token'):
            self.graph.get_context(symbols=['first'], context_token='../elsewhere')

    def test_receipt_continuations_preserve_unicode_and_do_not_hide_clipped_tails(self):
        self.path.write_text('def first():\n    return "'+'é'*3500+'"\n'); self.graph.index()
        first = self.graph.get_context(symbols=['first'], budget=1600, include_related=False)
        item = first['results'][0]
        result = self.graph.get_context(node_ids=[item['id']], offsets={item['id']:item['next_offset']},
            context_token=first['context_token'], budget=1600, include_related=False)
        next_item = result['results'][0]
        self.assertEqual(next_item['offset'], item['next_offset'])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1600)
        self.assertGreater(next_item['offset']+len(next_item['body']), item['next_offset'])

    def test_saved_bm25_mode_and_hook_never_initialize_a_model(self):
        configure(self.root, 'bm25')
        self.assertEqual(selected_mode(self.root), 'bm25')
        with patch.object(Graph, 'embedding_provider', side_effect=AssertionError('No embeddings')):
            self.assertEqual(self.graph.get_context(query='first')['mode'], 'bm25')
            with patch.dict(os.environ, {'CODEMANAGER_TIMING':'0'}):
                hook = prompt_hook(self.root, {'prompt':'first'})
            self.assertIn('"mode":"bm25"', hook['hookSpecificOutput']['additionalContext'])
        with patch.dict(os.environ, {'CODEMANAGER_SEARCH_MODE':'hybrid'}):
            self.assertEqual(selected_mode(self.root), 'hybrid')
            self.assertEqual(selected_mode(self.root, 'bm25'), 'bm25')
        with patch.object(self.graph, 'embedding_provider', side_effect=AssertionError('No embeddings')):
            self.graph.get_context(query='first', bm25_only=True)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.graph.get_context(query='first', mode='hybrid', bm25_only=True)

    def test_unknown_parameters_give_actionable_canonical_example(self):
        with self.assertRaisesRegex(ValueError, 'Repository is already bound.*Use budget'):
            self.graph.get_context(query='first', repo='.', max_bytes=3000)
        request = {'id':1,'method':'tools/call','params':{'name':'get_context','arguments':{'query':'first','repo':'.'}}}
        output = io.StringIO()
        with patch('sys.stdin',io.StringIO(json.dumps(request)+'\n')), patch('sys.stdout',output):
            serve(self.graph, mode='bm25')
        value = json.loads(output.getvalue())['result']
        self.assertTrue(value['isError']); self.assertIn('Use budget', value['content'][0]['text'])

    def test_mixed_questions_preserve_query_code_and_explicit_requests(self):
        for number in range(10):
            (self.root/f'aux{number}.py').write_text(f'def aux{number}():\n    return {number}\n')
        self.graph.index()
        result = self.graph.get_context(query='second value', symbols=[f'aux{i}' for i in range(8)],
                                        budget=20000, mode='bm25', include_related=False)
        self.assertTrue({'second',*(f'aux{i}' for i in range(8))} <= {r['name'] for r in result['results']})
        limited = self.graph.get_context(symbols=[f'aux{i}' for i in range(8)], limit=2, include_related=False)
        self.assertEqual(len(limited['results']), 2)
        self.assertTrue(limited['pending'])

    def test_unresolved_symbols_suggest_current_names(self):
        self.path.write_text('class Config:\n    def parse(self, value):\n        return value\n');self.graph.index()
        result = self.graph.get_context(symbols=['Config._preparse'])
        self.assertIn('app.py:Config.parse', result['unresolved'][0]['suggestions'])

    def test_content_reuse_survives_shifted_line_ids(self):
        self.graph.embed(); self.provider.documents_seen.clear()
        self.path.write_text('\n'+self.path.read_text()); self.graph.index()
        result = self.graph.embed()
        self.assertLessEqual(result['embedded_passages'], 1)
        self.assertEqual(result['reused_passages'], 2)
        self.assertFalse(any('function first' in text or 'function second' in text
                             for text in self.provider.documents_seen))

    def test_batch_checks_vectors_once_and_batches_query_embeddings(self):
        queries = []
        self.provider.queries = lambda texts: queries.append(list(texts)) or [[1,0,0] for _ in texts]
        with patch('codemanager.embeddings.sync_embeddings', wraps=sync_embeddings) as sync:
            self.graph.get_context(queries=['first','second'], mode='hybrid', include_related=False)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(queries, [['first','second']])
        cached = self.graph._decoded_vector_cache
        self.graph.export()  # Stat/signature bookkeeping must not discard decoded vectors.
        self.graph.get_context(query='first', mode='hybrid')
        self.assertIs(self.graph._decoded_vector_cache, cached)
        # Another connection changes source and embeddings; SQLite data_version invalidates both caches.
        other = Graph(self.root, embedder=self.provider)
        try:
            self.path.write_text('def replacement():\n    return 3\n'); other.index();other.embed()
        finally: other.close()
        result = self.graph.get_context(query='replacement', mode='hybrid', include_related=False)
        self.assertIsNot(self.graph._decoded_vector_cache, cached)
        self.assertTrue(any(r['name']=='replacement' for r in result['results']))

    def test_bm25_only_server_rejects_embedding_override(self):
        request = {'id':1,'method':'tools/call','params':{'name':'get_context',
                   'arguments':{'query':'first','mode':'hybrid'}}}
        output = io.StringIO()
        with patch('sys.stdin',io.StringIO(json.dumps(request)+'\n')), patch('sys.stdout',output), \
             patch.object(self.graph,'embedding_provider',side_effect=AssertionError('No model')):
            serve(self.graph, bm25_only=True)
        self.assertIn('BM25-only', json.loads(output.getvalue())['result']['content'][0]['text'])

    def test_question_focuses_long_class_on_matching_member(self):
        self.path.write_text('class Large:\n'+''.join(
            f'    def filler_{i}(self):\n        return "'+('x'*100)+'"\n\n' for i in range(90))+
            '    def refresh_cache(self):\n        return "fresh"\n')
        self.graph.index()
        result = self.graph.get_context(query='refresh cache', symbols=['Large'], mode='bm25', include_related=False)
        self.assertTrue(any(item['name']=='Large.refresh_cache' for item in result['results']))
        self.assertFalse(any(item['name']=='Large' for item in result['results']))
