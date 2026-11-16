import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codemanager.graph import Graph


class NodeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.source = self.repo / 'app.py'
        self.source.write_text('def multiply(a,b):\n    return a*b\n\ndef calculate(a,b):\n    return multiply(a,b)\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)
        self.graph.export()

    def test_expands_callees_and_callers_from_exact_node_ids(self):
        result = self.graph.retrieve_nodes(['app.py:4:calculate'])
        callee = next(item for item in result['results'] if item['name'] == 'multiply')
        self.assertEqual(callee['via']['kind'], 'calls_candidate')
        self.assertEqual(callee['via']['direction'], 'outgoing')
        self.assertIn('return a*b', callee['body'])
        neighbors = self.graph.retrieve_nodes(['app.py:4:calculate'], include_requested=False)
        self.assertFalse(any(item['name'] == 'calculate' for item in neighbors['results']))
        self.assertTrue(any(item['name'] == 'multiply' for item in neighbors['results']))
        reverse = self.graph.retrieve_nodes(['app.py:1:multiply'])
        caller = next(item for item in reverse['results'] if item['name'] == 'calculate')
        self.assertEqual(caller['via']['direction'], 'incoming')
        self.assertEqual(len(self.graph.retrieve_nodes(['app.py:4:calculate'], include_related=False)['results']), 1)

    def test_budget_long_nodes_and_stale_ids(self):
        self.source.write_text('def multiply(a,b):\n    return "'+'é'*2000+'"\n')
        self.graph.index()
        for budget in (256, 400, 1200, 6000):
            result = self.graph.retrieve_nodes(['app.py:1:multiply'], budget=budget)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), budget)
            self.assertTrue(result['truncated'])
        with self.assertRaisesRegex(ValueError, 'refresh search'):
            self.graph.retrieve_nodes(['app.py:4:calculate'])
        with self.assertRaises(ValueError):
            self.graph.retrieve_nodes([])
        with self.assertRaises(ValueError):
            self.graph.retrieve_nodes(['app.py:1:multiply'], include_related='yes')

    def test_legacy_cli_and_combined_mcp_context(self):
        args = [sys.executable, '-m', 'codemanager']
        cli = subprocess.run(args+['node-context', '--repo', str(self.repo), '--node-id',
            'app.py:4:calculate'], capture_output=True, text=True, check=True)
        self.assertTrue(any(item['name'] == 'multiply' for item in json.loads(cli.stdout)['results']))
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'get_context', 'arguments': {'node_ids': ['app.py:4:calculate'], 'budget': 1200}}}
        mcp = subprocess.run(args+['serve', '--repo', str(self.repo)], input=json.dumps(request)+'\n',
                             capture_output=True, text=True, check=True)
        response = json.loads(mcp.stdout)['result']
        self.assertFalse(response['isError'])
        self.assertLessEqual(len(response['content'][0]['text'].encode()), 1200)

    def test_node_continuation_does_not_skip_source_after_budget_truncation(self):
        self.source.write_text('def multiply(a,b):\n    return "'+'é'*2000+'"\n')
        self.graph.index()
        expected = self.graph.db.execute("SELECT body FROM nodes WHERE name='multiply'").fetchone()[0]
        pieces, offset = [], 0
        while offset is not None:
            result = self.graph.retrieve_nodes(['app.py:1:multiply'], budget=1200,
                                                include_related=False, offset=offset)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1200)
            row = result['results'][0]
            pieces.append(row['body'])
            next_offset = row['next_offset']
            self.assertTrue(next_offset is None or next_offset > offset)
            offset = next_offset
        self.assertEqual(''.join(pieces), expected)
