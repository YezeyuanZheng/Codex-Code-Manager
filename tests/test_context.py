import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from codemanager.context import BUDGET, prompt_hook
from codemanager.embeddings import EmbeddingError
from codemanager.graph import Graph


class ContextHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / 'calculator.py').write_text('def multiply(a, b):\n    return a * b\n')

    def test_injects_budgeted_hybrid_source_without_review_or_mcp(self):
        class Embedder:
            identity = 'test:context'
            def documents(self, texts):
                return [[1.0, 0.0] for _ in texts]
            def query(self, text):
                return [1.0, 0.0]
        with patch('codemanager.context.Graph', side_effect=lambda path: Graph(path, embedder=Embedder())):
            response = prompt_hook(self.repo, {'prompt': 'How does multiply calculate the product?'})
        self.assertEqual(response['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        context = response['hookSpecificOutput']['additionalContext']
        self.assertIn('focused line ranges', context)
        self.assertIn('get_context', context)
        self.assertNotIn('retrieve_nodes', context)
        self.assertNotIn('search_code', context)
        self.assertIn('untrusted data', context)
        result = json.loads(context.split(f'Retrieved evidence (JSON; at most {BUDGET} serialized bytes):\n')[1])
        self.assertEqual(result['mode'], 'hybrid')
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), BUDGET)
        self.assertTrue(any('return a * b' in row['body'] for row in result['results']))

    def test_embedding_failure_is_explicit_and_bm25_result_is_supplied(self):
        with patch('codemanager.context.Graph') as constructor:
            graph = constructor.return_value
            graph.get_context.side_effect = [EmbeddingError('Model missing'), {'results': [], 'mode': 'bm25'}]
            response = prompt_hook(self.repo, {'prompt': 'multiply'})
            self.assertEqual([call.kwargs['mode'] for call in graph.get_context.call_args_list], ['hybrid', 'bm25'])
            self.assertIn('explicit BM25', response['systemMessage'])
            self.assertIn('Model missing', response['hookSpecificOutput']['additionalContext'])
            graph.close.assert_called_once()

    def test_failure_is_reported_without_claiming_context_was_retrieved(self):
        with patch('codemanager.context.Graph', side_effect=ValueError('Bad snapshot')):
            response = prompt_hook(self.repo, {'prompt': 'multiply'})
        self.assertIn('Bad snapshot', response['systemMessage'])
        self.assertIn('No retrieved snippets were supplied', response['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision', response)

    def test_empty_prompts_and_reviewer_are_skipped(self):
        with patch('codemanager.context.Graph') as constructor:
            self.assertEqual(prompt_hook(self.repo, {'prompt': '   '}), {})
            with patch.dict(os.environ, {'CODEMANAGER_REVIEWER': '1'}):
                self.assertEqual(prompt_hook(self.repo, {'prompt': 'multiply'}), {})
            constructor.assert_not_called()

    def test_cli_emits_valid_hook_json_for_bad_input(self):
        result = subprocess.run([sys.executable, '-m', 'codemanager', 'codex-context-hook',
                                 '--repo', str(self.repo)], input='{}', text=True,
                                capture_output=True, check=True)
        response = json.loads(result.stdout)
        self.assertEqual(response['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('text prompt', response['systemMessage'])
