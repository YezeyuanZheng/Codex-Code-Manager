import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codemanager.config import CONFIG_FILE, DEFAULT_MODEL, model_settings, selected_model
from codemanager.embeddings import EmbeddingError
from codemanager.graph import Graph
from codemanager.__main__ import main, serve


class ModelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        (self.repo / 'app.py').write_text('def permit(ticket):\n    return ticket.valid\n')
        environment = patch.dict(os.environ, {'CODEMANAGER_EMBEDDING_MODEL': ''})
        environment.start()
        self.addCleanup(environment.stop)

    def test_persistent_default_and_override_precedence(self):
        self.assertEqual(selected_model(self.repo), (DEFAULT_MODEL, 'default'))
        value = model_settings(self.repo, save='BAAI/bge-small-en-v1.5')
        self.assertEqual(value['saved_model'], 'BAAI/bge-small-en-v1.5')
        self.assertEqual(selected_model(self.repo), ('BAAI/bge-small-en-v1.5', CONFIG_FILE))
        with patch.dict(os.environ, {'CODEMANAGER_EMBEDDING_MODEL':'nomic-ai/nomic-embed-text-v1.5'}):
            self.assertEqual(selected_model(self.repo), ('nomic-ai/nomic-embed-text-v1.5', 'environment'))
            self.assertEqual(selected_model(self.repo, 'test/one-off'), ('test/one-off', 'override'))
        self.assertEqual(model_settings(self.repo, reset=True)['model'], DEFAULT_MODEL)
        self.assertFalse((self.repo / CONFIG_FILE).exists())

    def test_reset_preserves_other_configuration_and_invalid_input_does_not_write(self):
        path = self.repo / CONFIG_FILE
        path.write_text(json.dumps({'other': {'enabled': True}, 'embedding': {'other_option': 3}}))
        model_settings(self.repo, save='BAAI/bge-small-en-v1.5')
        model_settings(self.repo, reset=True)
        self.assertEqual(json.loads(path.read_text()), {'other': {'enabled': True}, 'embedding': {'other_option': 3}})
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Model must be'):
            model_settings(self.repo, save='../outside')
        self.assertEqual(path.read_bytes(), before)
        path.write_text('invalid json')
        with self.assertRaisesRegex(ValueError, 'Invalid codemanager.json'):
            selected_model(self.repo)

    def provider(self, name):
        dimensions = 3 if name != 'test/large' else 5
        class Provider:
            identity = 'test:'+name
            model_name = name
            def documents(self, texts):
                if name == 'test/broken':
                    raise EmbeddingError('Unavailable model')
                return [[1]+[0]*(dimensions-1) for _ in texts]
            def query(self, text):
                return [1]+[0]*(dimensions-1)
        return Provider()

    def test_switch_rebuilds_different_dimensions_and_reload_of_saved_default(self):
        model_settings(self.repo, save='test/small')
        graph = Graph(self.repo)
        self.addCleanup(graph.close)
        graph.index()
        with patch('codemanager.embeddings.FastEmbedder', side_effect=self.provider) as factory:
            first = graph.search('permit', mode='hybrid')
            self.assertEqual(first['model'], 'test/small')
            graph.search('permit', mode='hybrid')
            self.assertEqual(factory.call_count, 1)
            override = graph.search('permit', mode='hybrid', model='test/large')
            self.assertEqual(override['model'], 'test/large')
            self.assertEqual([r[0] for r in graph.db.execute('SELECT DISTINCT model FROM embeddings')], ['test:test/large'])
            self.assertEqual(model_settings(self.repo)['model'], 'test/small')
            self.assertEqual(graph.search('permit', mode='hybrid')['model'], 'test/small')
            model_settings(self.repo, save='test/large')
            self.assertEqual(graph.search('permit', mode='hybrid')['model'], 'test/large')

    def test_failed_switch_does_not_destroy_working_vectors(self):
        graph = Graph(self.repo, embedding_model='test/small')
        self.addCleanup(graph.close)
        graph.index()
        with patch('codemanager.embeddings.FastEmbedder', side_effect=self.provider):
            graph.embed()
            before = [tuple(r) for r in graph.db.execute('SELECT * FROM embeddings')]
            with self.assertRaisesRegex(EmbeddingError, 'Unavailable model'):
                graph.search('permit', mode='hybrid', model='test/broken')
            self.assertEqual(before, [tuple(r) for r in graph.db.execute('SELECT * FROM embeddings')])
            self.assertEqual(graph.search('permit', mode='hybrid')['model'], 'test/small')

    def test_cli_save_and_read_work_without_model_download(self):
        for args in (['--set', 'BAAI/bge-small-en-v1.5'], []):
            result = subprocess.run([sys.executable, '-m', 'codemanager', 'model', '--repo', str(self.repo), *args],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['model'], 'BAAI/bge-small-en-v1.5')
        with patch('sys.argv', ['codemanager','embed','--repo',str(self.repo),'--model','test/large']), patch('codemanager.embeddings.FastEmbedder', side_effect=self.provider), patch('sys.stdout', io.StringIO()) as output:
            main()
            self.assertEqual(json.loads(output.getvalue())['model'], 'test:test/large')
        self.assertEqual(selected_model(self.repo)[0], 'BAAI/bge-small-en-v1.5')

    def test_mcp_request_can_override_server_model(self):
        graph = Graph(self.repo, embedding_model='test/small')
        self.addCleanup(graph.close)
        requests = [
            {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_context','arguments':{'query':'permit','model':'test/large','compact':False}}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_context','arguments':{'query':'permit','compact':False}}},
        ]
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('\n'.join(map(json.dumps, requests))+'\n')), patch('sys.stdout', output), patch('codemanager.embeddings.FastEmbedder', side_effect=self.provider):
            serve(graph, mode='hybrid')
        models = [json.loads(json.loads(line)['result']['content'][0]['text'])['model'] for line in output.getvalue().splitlines()]
        self.assertEqual(models, ['test/large', 'test/small'])


if __name__ == '__main__':
    unittest.main()
