import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph
from codemanager.source_coverage import SourceCoverage, source_item


class SourceCoverageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        self.source = self.repo / 'app.py'
        self.source.write_text('class Box:\n    def size(self):\n        return 17\n\n'
                               'def outside():\n    return 23\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)
        self.graph.index()

    def assert_disjoint(self, result):
        coverage = SourceCoverage()
        for item in result['results']:
            row = dict(self.graph.db.execute('SELECT * FROM nodes WHERE id=?', (item['id'],)).fetchone())
            offset = item['offset']
            stop = offset + len(item['body'])
            self.assertEqual(list(coverage.uncovered(row, offset, len(item['body']))), [(offset, stop)])
            self.assertEqual(item['body'], row['body'][offset:stop])
            self.assertEqual(item['start'], source_item(row, offset, stop)['start'])
            self.assertEqual(item['end'], source_item(row, offset, stop)['end'])
            coverage.add(row, offset, len(item['body']))

    def test_requested_function_precedes_parent_without_repeating_source(self):
        result = self.graph.retrieve_nodes(['app.py:2:Box.size'], budget=10000)
        self.assertEqual(result['results'][0]['id'], 'app.py:2:Box.size')
        self.assert_disjoint(result)
        emitted = ''.join(item['body'] for item in result['results'])
        self.assertEqual(emitted.count('return 17'), 1)
        self.assertIn('class Box:', emitted)

    def test_parent_covers_child_but_does_not_consume_unique_result_slots(self):
        result = self.graph.retrieve_nodes(['app.py:1:Box', 'app.py:2:Box.size', 'app.py:5:outside'],
                                           include_related=False, limit=2, budget=10000)
        self.assertEqual([item['name'] for item in result['results']], ['Box', 'outside'])
        self.assertEqual(result['references'][0]['id'], 'app.py:2:Box.size')
        self.assertEqual(result['references'][0]['covered_by'], ['app.py:1:Box'])
        self.assertFalse(result['truncated'])
        self.assert_disjoint(result)

    def test_covered_graph_neighbor_retains_a_pointer_to_its_source(self):
        self.source.write_text('def authenticate(token):\n    return token == "ok"\n\n'
                               'def login(token):\n    return authenticate(token)\n')
        self.graph.index()
        result = self.graph.search('login', mode='bm25', budget=10000)
        pointers = [item for item in result.get('references', []) if item['reason']=='graph_neighbor']
        self.assertTrue(pointers)
        for pointer in pointers:
            self.assertNotIn('body', pointer)
            self.assertTrue(set(pointer['covered_by']) <= {item['id'] for item in result['results']})
        self.assert_disjoint(result)

    def test_search_filters_overlapping_module_class_and_function_nodes(self):
        result = self.graph.search('Box size return', mode='bm25', budget=10000, limit=20)
        self.assert_disjoint(result)
        self.assertEqual(''.join(item['body'] for item in result['results']).count('return 17'), 1)
        self.assertIn('return 23', ''.join(item['body'] for item in result['results']))

    def test_semantic_passage_midline_does_not_hide_the_rest_of_that_line(self):
        self.source.write_text('def long_value():\n    return "'+'é'*2400+'"\n')
        self.graph.index()
        row = dict(self.graph.db.execute("SELECT * FROM nodes WHERE name='long_value'").fetchone())
        # The dense hit starts inside a long line; a lexical file hit contains its beginning.
        with patch('codemanager.embeddings.dense_search', return_value=[(row['id'], 1.0, 1)]):
            with patch.object(self.graph, 'embedding_provider') as provider:
                provider.return_value.identity = 'test'
                provider.return_value.model_name = 'test'
                result = self.graph.search('long_value', mode='hybrid', budget=20000, limit=20)
        self.assert_disjoint(result)
        fragments = [item for item in result['results'] if item['id']==row['id']]
        self.assertEqual(fragments[0]['offset'], 1200)
        self.assertEqual(fragments[0]['body'], row['body'][1200:2400])
        self.assertTrue(any(item['offset']==0 for item in result['results']))

    def test_coverage_is_per_file_and_per_response_and_refreshes_after_edits(self):
        (self.repo / 'other.py').write_text(self.source.read_text())
        self.graph.index()
        first = self.graph.retrieve_nodes(['app.py:1:Box', 'other.py:1:Box'], include_related=False)
        self.assertEqual(len(first['results']), 2)
        self.assertEqual(first, self.graph.retrieve_nodes(['app.py:1:Box', 'other.py:1:Box'], include_related=False))
        self.source.write_text(self.source.read_text().replace('return 17', 'return 42'))
        self.graph.index()
        self.assertIn('return 42', self.graph.retrieve_nodes(['app.py:1:Box'], include_related=False)['results'][0]['body'])

    def test_budget_and_continuation_preserve_unicode_after_partial_overlap(self):
        self.source.write_text('class Box:\n    def size(self):\n        return "'+'é'*2400+'"\n')
        self.graph.index()
        initial = self.graph.retrieve_nodes(['app.py:2:Box.size', 'app.py:1:Box'], budget=2200)
        self.assertLessEqual(len(json.dumps(initial, ensure_ascii=False).encode()), 2200)
        self.assert_disjoint(initial)
        row = dict(self.graph.db.execute("SELECT * FROM nodes WHERE name='Box'").fetchone())
        pieces, offset = [], 0
        while offset is not None:
            result = self.graph.retrieve_nodes([row['id']], include_related=False, offset=offset, budget=1200)
            item = result['results'][0]
            pieces.append(item['body'])
            self.assertTrue(item['next_offset'] is None or item['next_offset']>offset)
            offset = item['next_offset']
        self.assertEqual(''.join(pieces), row['body'])
        with self.assertRaisesRegex(ValueError, 'offset exceeds'):
            self.graph.retrieve_nodes([row['id']], offset=len(row['body'])+1)
