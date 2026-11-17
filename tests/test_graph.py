import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph


class GraphTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'CODEMANAGER_SEARCH_MODE': 'bm25'})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'app.py').write_text('def authenticate(token):\n    return token == "ok"\n\ndef login(token):\n    return authenticate(token)\n')
        self.graph = Graph(self.root)

    def tearDown(self):
        self.graph.close()
        self.temp.cleanup()

    def test_incremental_edges_and_deletion(self):
        self.assertEqual(self.graph.index()['changed_files'], 1)
        self.assertEqual(self.graph.index()['changed_files'], 0)
        edges = self.graph.db.execute("SELECT * FROM edges WHERE kind='calls_candidate'").fetchall()
        self.assertEqual(len(edges), 1)
        self.assertIn('login', edges[0]['source'])
        self.assertIn('authenticate', edges[0]['target'])
        (self.root / 'app.py').write_text('def replacement():\n    pass\n')
        self.graph.index()
        self.assertFalse(self.graph.search('authenticate')['results'])
        (self.root / 'app.py').unlink()
        self.assertEqual(self.graph.index()['deleted_files'], 1)
        self.assertEqual(self.graph.stats(), {'files': 0, 'nodes': 0, 'edges': 0})

    def test_budget_and_graph_expansion(self):
        self.graph.index()
        result = self.graph.search('login', 10000)
        self.assertTrue(any(r['reason'] == 'graph_neighbor'
                            for r in result['results'] + result.get('references', [])))
        for budget in (256, 400, 1000):
            result = self.graph.search('authenticate', budget)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), budget)

    def test_fallback_exclusions_and_symlink(self):
        (self.root / 'script.ts').write_text('export function hello() { return "world"; }')
        (self.root / '.env').write_text('SECRET=hidden')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'ignore.py').write_text('secret=1')
        (self.root / 'link.py').symlink_to(self.root / 'app.py')
        (self.root / 'broken.py').write_text('def unfinished(')
        self.assertEqual(self.graph.index()['files'], 3)
        self.assertTrue(self.graph.search('hello')['results'])

    def test_gitignore(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('ignored.py\n')
        (self.root / 'ignored.py').write_text('secret=1')
        self.assertEqual(self.graph.index()['files'], 1)

    def test_imported_call_alias_resolves_to_target_node(self):
        (self.root/'helpers.py').write_text('def work():\n    return 1\n')
        (self.root/'app.py').write_text('from helpers import work as run\n\ndef entry():\n    return run()\n')
        self.graph.index()
        edge = self.graph.db.execute("""SELECT source,target FROM edges
            WHERE kind='imported_calls_candidate'""").fetchone()
        self.assertIsNotNone(edge)
        self.assertTrue(edge['source'].endswith(':entry'))
        self.assertTrue(edge['target'].endswith(':work'))

    def test_test_call_is_tagged_as_a_test_to_implementation_edge(self):
        (self.root/'helpers.py').write_text('def work():\n    return 1\n')
        (self.root/'tests').mkdir()
        (self.root/'tests'/'test_helpers.py').write_text(
            'from helpers import work\n\ndef test_work():\n    assert work() == 1\n')
        self.graph.index()
        edge = self.graph.db.execute("""SELECT source,target FROM edges
            WHERE kind='test_calls_candidate'""").fetchone()
        self.assertIsNotNone(edge)
        self.assertTrue(edge['source'].endswith(':test_work'))
        self.assertTrue(edge['target'].endswith(':work'))

    def test_mcp_stdio(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'index_repository'}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'get_context', 'arguments': {'query': 'login'}}},
        ]
        proc = subprocess.run([sys.executable, '-m', 'codemanager', 'serve', '--repo', str(self.root)], input='\n'.join(map(json.dumps, requests))+'\n', capture_output=True, text=True, check=True)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        names = {tool['name'] for tool in responses[1]['result']['tools']}
        self.assertEqual(len(names), 9)
        self.assertIn('get_context', names)
        self.assertFalse({'search_code','retrieve_nodes'} & names)
        self.assertFalse(responses[-1]['result']['isError'])
        self.assertTrue(json.loads(responses[-1]['result']['content'][0]['text'])['results'])


if __name__ == '__main__':
    unittest.main()
