"""Regression cases for focused related evidence under a fixed node budget."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codemanager.context_selection import relationships
from codemanager.graph import Graph


class RelationshipRelevanceTests(unittest.TestCase):
    def test_test_query_prioritizes_linked_test_for_implementation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'helpers.py').write_text('def work():\n    return 1\n')
            (path / 'tests').mkdir()
            (path / 'tests' / 'test_helpers.py').write_text(
                'from helpers import work\n\ndef test_work():\n    assert work() == 1\n')
            graph = Graph(path)
            self.addCleanup(graph.close)
            graph.index()
            rows = {row['name']: dict(row) for row in graph.db.execute(
                "SELECT * FROM nodes WHERE kind='function'")}
            hints = relationships(graph, [{'row': rows['work']}], 8,
                                  'show tests for work')
            self.assertEqual(hints[0]['id'], rows['test_work']['id'])
            self.assertEqual(hints[0]['kind'], 'test_calls_candidate')

    def test_two_edge_call_path_offers_single_bridge_node(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'app.py').write_text(
                'def first():\n    return middle()\n\n'
                'def middle():\n    return last()\n\n'
                'def last():\n    return 1\n')
            graph = Graph(path)
            self.addCleanup(graph.close)
            graph.index()
            rows = {row['name']: dict(row) for row in graph.db.execute(
                "SELECT * FROM nodes WHERE kind='function'")}
            hints = relationships(graph, [{'row': rows['first']}, {'row': rows['last']}],
                                  8, 'trace first through last')
            self.assertTrue(any(h['kind']=='path_bridge' and h['id']==rows['middle']['id']
                                for h in hints))

    def test_focused_helper_precedes_broad_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'app.py').write_text(
                'def entry():\n    return 1\n\n'
                'def validate_token(token):\n    return bool(token)\n\n'
                'def broad_report():\n'
                '    # validate token and show how the code does this\n'
                '    # accounts invoices rendering dashboard permissions unrelated\n'
                '    return entry()\n')
            graph = Graph(path)
            self.addCleanup(graph.close)
            graph.index()
            rows = {row['name']: dict(row) for row in graph.db.execute(
                "SELECT * FROM nodes WHERE kind='function'")}
            anchor = rows['entry']['id']
            for name in ('broad_report', 'validate_token'):
                graph.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)',
                                 (anchor, rows[name]['id'], 'calls_candidate'))
            with patch('codemanager.caller_context.callers', return_value=[]):
                hints = relationships(graph, [{'row': rows['entry']}], 8,
                                      'show how the code does validate token')
            bodies = [hint['id'] for hint in hints if hint['kind'] != 'contains']
            self.assertEqual(bodies[0], rows['validate_token']['id'])
            # Retain integration evidence as a lower-priority option.
            self.assertIn(rows['broad_report']['id'], bodies)
            with patch('codemanager.caller_context.callers', return_value=[]):
                result = graph.get_context(query='validate token', symbols=['entry'],
                                           mode='bm25', limit=2, budget=2000)
            emitted = '\n'.join(item['body'] for item in result['results'])
            self.assertIn('def validate_token', emitted)
            self.assertNotIn('def broad_report', emitted)
            self.assertLessEqual(len(__import__('json').dumps(result, ensure_ascii=False).encode()), 2000)
