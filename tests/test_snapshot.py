import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph
from codemanager.snapshot import SHARD_BYTES


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'CODEMANAGER_SEARCH_MODE': 'bm25'})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'origin'
        self.root.mkdir()
        (self.root / 'app.py').write_text('def authenticate(token):\n    return token == "ok"\n\ndef login(token):\n    return authenticate(token)\n')
        self.graph = Graph(self.root)

    def tearDown(self):
        self.graph.close()
        self.temp.cleanup()

    def test_real_clone_loads_without_parsing(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        self.graph.export()
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'Bundle code graph'], check=True)
        clone = Path(self.temp.name) / 'clone'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(clone)], check=True)
        self.assertFalse((clone / '.codemanager' / 'graph.sqlite3').exists())
        with patch.object(Graph, 'parse', side_effect=AssertionError('Clone must not reparse unchanged files')):
            graph = Graph(clone)
        try:
            result = graph.search('login', 10000)
            self.assertTrue(any(r['reason'] == 'graph_neighbor' for r in result['results']))
            self.assertTrue(any('return authenticate(token)' in r['body'] for r in result['results']))
        finally:
            graph.close()

    def test_deterministic_compact_shards(self):
        self.graph.export()
        folder = self.root / '.codemanager' / 'index'
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.graph.export()
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})
        self.assertFalse(any(b'return authenticate(token)' in data for data in before.values()))
        self.assertTrue(all(len(data) <= SHARD_BYTES for name, data in before.items() if name.endswith('.jsonl')))

    def test_changed_deleted_and_added_sources(self):
        self.graph.export()
        clone = Path(self.temp.name) / 'clone'
        shutil.copytree(self.root, clone, ignore=shutil.ignore_patterns('graph.sqlite3*'))
        (clone / 'app.py').unlink()
        (clone / 'new.py').write_text('def new_symbol():\n    return 42\n')
        graph = Graph(clone)
        try:
            self.assertFalse(graph.search('authenticate')['results'])
            self.assertTrue(graph.search('new_symbol')['results'])
            self.assertEqual(graph.stats()['files'], 1)
        finally:
            graph.close()

    def test_modified_source_is_parsed_instead_of_loading_old_ranges(self):
        self.graph.export()
        clone = Path(self.temp.name) / 'clone'
        shutil.copytree(self.root, clone, ignore=shutil.ignore_patterns('graph.sqlite3*'))
        (clone / 'app.py').write_text('def changed_symbol():\n    return 7\n')
        graph = Graph(clone)
        try:
            self.assertFalse(graph.search('authenticate')['results'])
            self.assertTrue(graph.search('changed_symbol')['results'])
        finally:
            graph.close()

    def test_shard_split_roundtrip(self):
        with patch('codemanager.snapshot.SHARD_BYTES', 400):
            result = self.graph.export()
            self.assertGreater(result['shards'], 1)
            clone = Path(self.temp.name) / 'clone'
            shutil.copytree(self.root, clone, ignore=shutil.ignore_patterns('graph.sqlite3*'))
            graph = Graph(clone)
            try:
                self.assertTrue(graph.search('authenticate')['results'])
            finally:
                graph.close()

    def test_changed_manifest_replaces_existing_cache(self):
        self.graph.export()
        clone = Path(self.temp.name) / 'clone'
        shutil.copytree(self.root, clone, ignore=shutil.ignore_patterns('graph.sqlite3*'))
        graph = Graph(clone)
        graph.close()
        (self.root / 'app.py').write_text('def new_symbol():\n    pass\n')
        self.graph.export()
        shutil.copytree(self.root / '.codemanager' / 'index', clone / '.codemanager' / 'index', dirs_exist_ok=True)
        shutil.copy(self.root / 'app.py', clone / 'app.py')
        with patch.object(Graph, 'parse', side_effect=AssertionError('Updated snapshot must hydrate the cache')):
            graph = Graph(clone)
        try:
            self.assertTrue(graph.search('new_symbol')['results'])
            self.assertFalse(graph.search('authenticate')['results'])
        finally:
            graph.close()

    def test_export_keeps_only_current_graph_and_git_preserves_history(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args],
                                  capture_output=True, text=True, check=True).stdout.strip()

        git('init', '-q')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.com')
        git('config', 'commit.gpgsign', 'false')
        (self.root / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        self.graph.export()
        folder = self.root / '.codemanager' / 'index'
        first = json.loads((folder / 'manifest.json').read_text())
        old_shard = first['shards'][0]['file']
        old_data = (folder / old_shard).read_text()
        git('add', '.')
        git('commit', '-qm', 'Original source and graph')
        old_commit = git('rev-parse', 'HEAD')
        (folder / 'notes.jsonl').write_text('User notes, not a generated shard')
        (self.root / 'app.py').write_text('def replacement():\n    return 3\n')
        self.graph.export()
        current = json.loads((folder / 'manifest.json').read_text())
        self.assertFalse((folder / old_shard).exists())
        self.assertTrue((folder / 'notes.jsonl').exists())
        self.assertEqual({p.name for p in folder.glob('*.jsonl') if p.name != 'notes.jsonl'},
                         {shard['file'] for shard in current['shards']})
        git('add', '-A')
        git('commit', '-qm', 'Updated source and graph')
        self.assertEqual(git('show', old_commit+':.codemanager/index/'+old_shard), old_data.strip())
        git('checkout', '-q', old_commit)
        # Reuse a cache that previously contained the newer code.
        graph = Graph(self.root)
        try:
            self.assertTrue(graph.search('authenticate')['results'])
            self.assertFalse(graph.search('replacement')['results'])
        finally:
            graph.close()

    def test_corrupt_bundle_does_not_replace_cache(self):
        self.graph.export()
        expected = self.graph.stats()
        folder = self.root / '.codemanager' / 'index'
        manifest = json.loads((folder / 'manifest.json').read_text())
        (folder / manifest['shards'][0]['file']).write_text('damaged')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.graph.load_bundled_snapshot()
        self.assertEqual(self.graph.stats(), expected)


if __name__ == '__main__':
    unittest.main()
