import json
from pathlib import Path
import tempfile
import unittest

from codemanager.graph import Graph
from codemanager.requests import map_request
from codemanager.embeddings import passages


class RequestMappingTests(unittest.TestCase):
    def test_mapping_rebuild_move_change_and_search(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'codemanager.json').write_text(json.dumps({'review': {'enabled': True}}))
            source = root / 'calculator.py'
            source.write_text('def multiply(a, b):\n    return a * b\n')
            graph = Graph(root)
            try:
                graph.export()
                map_request(graph, 'calculator.py:1:multiply', 'Build a calculator', 'Fulfill multiplication requirement')
                result = graph.search('multiplication', mode='bm25')
                node = next(row for row in result['results'] if row['name'] == 'multiply')
                self.assertEqual(node['request_mapping'][0]['fulfills'], 'Fulfill multiplication requirement')
                row = graph.db.execute("SELECT * FROM nodes WHERE name='multiply'").fetchone()
                self.assertIn('multiplication', next(passages(row))[0])
                source.write_text('\n\ndef multiply(a, b):\n    return a * b\n')
                graph.export(full=True)
                row = graph.db.execute("SELECT * FROM nodes WHERE name='multiply'").fetchone()
                self.assertTrue(json.loads(row['request_mapping']))
                graph.close()
                graph = Graph(root, cache_dir=root / '.fresh')
                graph.index(full=True)
                row = graph.db.execute("SELECT * FROM nodes WHERE name='multiply'").fetchone()
                self.assertTrue(json.loads(row['request_mapping']))
                source.write_text('def multiply(a, b):\n    return 0\n')
                graph.index()
                row = graph.db.execute("SELECT * FROM nodes WHERE name='multiply'").fetchone()
                self.assertEqual(json.loads(row['request_mapping']), [])
                map_request(graph, row['id'], 'Build a calculator', 'New explanation')
                entries = json.loads(graph.db.execute("SELECT request_mapping FROM nodes WHERE name='multiply'").fetchone()[0])
                self.assertEqual(len(entries), 1)
                with self.assertRaises(ValueError):
                    map_request(graph, 'missing', 'request', 'intent')
            finally:
                graph.close()

    def test_snapshot_only_recovery_and_staged_review(self):
        import shutil
        import subprocess
        from codemanager.automation import check_snapshot
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'repo'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
            (root / 'codemanager.json').write_text(json.dumps({'review': {'enabled': True}}))
            source = root / 'calculator.py'
            source.write_text('def multiply(a, b):\n    return a * b\n')
            graph = Graph(root)
            try:
                graph.export()
                before = (root / '.codemanager/index/manifest.json').read_bytes()
                map_request(graph, 'calculator.py:1:multiply', 'Build a calculator', 'Multiply two numbers')
                self.assertNotEqual(before, (root / '.codemanager/index/manifest.json').read_bytes())
                check_snapshot(root, full=True)
                subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
                # A different working-tree explanation must not leak into staged validation.
                map_request(graph, 'calculator.py:1:multiply', 'Build a calculator', 'UNSTAGED explanation')
                self.assertTrue(check_snapshot(root, staged=True)['valid'])
            finally:
                graph.close()
            clone = Path(folder) / 'clone'
            shutil.copytree(root, clone, ignore=shutil.ignore_patterns('graph.sqlite3*', 'snapshot.lock', '.git'))
            recovered = Graph(clone)
            try:
                self.assertFalse((clone / 'codemanager.requests.json').exists())
                recovered.export(full=True)
                node = recovered.db.execute("SELECT * FROM nodes WHERE name='multiply'").fetchone()
                self.assertEqual(json.loads(node['request_mapping'])[0]['fulfills'], 'UNSTAGED explanation')
                (clone / 'calculator.py').write_text('def multiply(a, b):\n    return a + b\n')
                with self.assertRaises(ValueError):
                    check_snapshot(clone, full=True)
            finally:
                recovered.close()
