import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codemanager.automation import check_snapshot, seed_validation_cache
from codemanager.graph import Graph, bucket_for
from codemanager.snapshot import encoded


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        (self.repo / 'app.py').write_text('def target():\n    return 1\n\ndef caller():\n    return target()\n')
        (self.repo / 'consumer.py').write_text('import app\n')
        (self.repo / 'unrelated.py').write_text('def other():\n    pass\n\ndef caller():\n    return other()\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)

    def snapshot(self, root=None):
        folder = (root or self.repo) / '.codemanager' / 'index'
        manifest = (folder / 'manifest.json').read_bytes()
        return {'manifest.json': manifest, **{entry['file']:(folder / entry['file']).read_bytes() for entry in json.loads(manifest)['shards']}}

    def assert_matches_clean_build(self):
        with tempfile.TemporaryDirectory(dir=self.base) as folder:
            root = Path(folder)
            graph = Graph(self.repo, load_snapshot=False, cache_dir=root / 'cache')
            try:
                from codemanager.snapshot import export_snapshot
                export_snapshot(graph, output_dir=root / '.codemanager' / 'index', full=True)
                self.assertEqual(self.snapshot(), self.snapshot(root))
            finally:
                graph.close()

    def git(self, *args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True,text=True).stdout.strip()

    def test_noop_does_not_read_source_parse_or_serialize(self):
        self.graph.export()
        read = Path.read_bytes
        def guard(path):
            if path.parent == self.repo and path.suffix == '.py':
                self.fail('Unchanged source must not be read')
            return read(path)
        with patch.object(Path, 'read_bytes', guard), patch.object(self.graph, 'parse', side_effect=AssertionError('Unchanged source parsed')), patch('codemanager.snapshot.serialize_bucket', side_effect=AssertionError('Unchanged partition serialized')):
            result = self.graph.export()
        self.assertEqual(result['refreshed']['hashed_files'], 0)
        self.assertEqual(result['refreshed']['resolved_references'], 0)
        self.assertEqual(result['rebuilt_partitions'], 0)

    def test_one_edit_resolves_only_affected_sources_and_reuses_other_partitions(self):
        self.graph.export()
        (self.repo / 'app.py').write_text('def target():\n    return 2\n\ndef caller():\n    return target()\n')
        with patch.object(self.graph, 'parse', wraps=self.graph.parse) as parser, patch.object(self.graph, 'resolve_reference', wraps=self.graph.resolve_reference) as resolver:
            result = self.graph.export()
        self.assertEqual([call.args[0] for call in parser.call_args_list], ['app.py'])
        resolved_paths = {call.args[1] for call in resolver.call_args_list}
        self.assertEqual(resolved_paths, {'app.py', 'consumer.py'})
        self.assertEqual(result['refreshed']['hashed_files'], 1)
        self.assertEqual(result['rebuilt_partitions'], len({bucket_for('app.py'),bucket_for('consumer.py')}))
        self.assertGreaterEqual(result['reused_partitions'], 1)
        self.assert_matches_clean_build()

    def test_add_delete_and_rename_match_full_rebuild(self):
        self.graph.export()
        (self.repo / 'late.py').write_text('def new_symbol():\n    pass\n')
        (self.repo / 'consumer.py').write_text('import late\n')
        self.graph.export()
        self.assert_matches_clean_build()
        (self.repo / 'late.py').rename(self.repo / 'renamed.py')
        result = self.graph.export()
        self.assertEqual(result['refreshed']['deleted_files'], 1)
        self.assertFalse(self.graph.db.execute("SELECT * FROM edges WHERE kind='imports_candidate'").fetchall())
        self.assert_matches_clean_build()
        (self.repo / 'renamed.py').unlink()
        self.graph.export()
        self.assert_matches_clean_build()

    def test_new_module_resolves_existing_importer_without_reparsing_it(self):
        (self.repo / 'consumer.py').write_text('import late\n')
        self.graph.export()
        (self.repo / 'late.py').write_text('def new_symbol():\n    pass\n')
        with patch.object(self.graph, 'parse', wraps=self.graph.parse) as parser:
            self.graph.export()
        self.assertEqual([call.args[0] for call in parser.call_args_list], ['late.py'])
        self.assertEqual(len(self.graph.db.execute("SELECT * FROM edges WHERE kind='imports_candidate'").fetchall()), 1)
        self.assert_matches_clean_build()

    def test_same_size_edit_with_restored_mtime_is_detected(self):
        self.graph.export()
        path = self.repo / 'app.py'
        stat = path.stat()
        path.write_text(path.read_text().replace('return 1','return 2'))
        os.utime(path, ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertEqual(self.graph.export()['refreshed']['changed_files'], 1)

    def test_git_revision_changes_are_reported_and_only_changed_file_parsed(self):
        self.git('init','-q')
        self.git('config','user.name','Test')
        self.git('config','user.email','test@example.com')
        self.git('config','commit.gpgsign','false')
        self.git('add','*.py')
        self.git('commit','-qm','First')
        first = self.git('rev-parse','HEAD')
        self.graph.export()
        (self.repo / 'app.py').write_text('def revised():\n    return 5\n')
        self.git('add','app.py')
        self.git('commit','-qm','Second')
        second = self.git('rev-parse','HEAD')
        result = self.graph.export()
        self.assertEqual(result['refreshed']['base_revision'], first)
        self.assertEqual(result['refreshed']['revision'], second)
        self.assertEqual(result['refreshed']['changed_files'], 1)
        self.git('checkout','-q',first)
        result = self.graph.export()
        self.assertEqual(result['refreshed']['changed_files'], 1)
        self.assert_matches_clean_build()

    def test_validation_reuses_cache_and_full_audit_is_independent(self):
        self.git('init','-q')
        (self.repo / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        self.graph.export()
        self.git('add','.')
        with patch.object(Graph, 'parse', side_effect=AssertionError('Warm validation reparsed unchanged code')):
            for staged in (False,True):
                result = check_snapshot(self.repo, staged=staged)
                self.assertTrue(result['reused_cache'])
                self.assertEqual(result['parsed_files'], 0)
        audited = check_snapshot(self.repo, staged=True, full=True)
        self.assertFalse(audited['reused_cache'])
        self.assertEqual(audited['parsed_files'], 3)

    def test_validation_copy_excludes_large_derived_indexes(self):
        self.graph.export()
        ident = self.graph.db.execute('SELECT id FROM nodes LIMIT 1').fetchone()[0]
        with self.graph.db:
            self.graph.db.execute('INSERT INTO embeddings VALUES (?,?,?,?,?)', (ident,0,'test','digest',b'vector-data'))
        destination = self.base / 'validation-cache'
        self.assertTrue(seed_validation_cache(self.repo,destination))
        copied = sqlite3.connect(destination/'graph.sqlite3')
        try:
            self.assertEqual(copied.execute('SELECT count(*) FROM embeddings').fetchone()[0],0)
            self.assertEqual(copied.execute('SELECT count(*) FROM search').fetchone()[0],0)
            self.assertEqual(copied.execute('SELECT count(*) FROM nodes').fetchone()[0],self.graph.stats()['nodes'])
        finally:
            copied.close()
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM embeddings').fetchone()[0],1)

    def test_full_rebuild_and_no_cache_check_remain_available(self):
        self.graph.export()
        result = self.graph.export(full=True)
        self.assertEqual(result['refreshed']['changed_files'], 3)
        self.assertEqual(result['refreshed']['hashed_files'], 3)
        # A fresh copy has no local cache and must bootstrap once.
        clone = self.base / 'clone'
        shutil.copytree(self.repo,clone,ignore=shutil.ignore_patterns('graph.sqlite3*','snapshot.lock'))
        result = check_snapshot(clone)
        self.assertFalse(result['reused_cache'])
        self.assertEqual(result['parsed_files'], 3)

    def test_legacy_snapshot_loads_without_reparse_then_migrates(self):
        self.graph.export()
        data = bytearray()
        from codemanager.snapshot import TABLES
        for table, columns in TABLES.items():
            names = ','.join(columns)
            for row in self.graph.db.execute(f'SELECT {names} FROM {table} ORDER BY {names}'):
                data.extend(encoded({'table':table,'values':list(row)}))
        clone = self.base / 'legacy'
        clone.mkdir()
        for path in self.repo.glob('*.py'):
            shutil.copy(path,clone/path.name)
        folder = clone / '.codemanager' / 'index'
        folder.mkdir(parents=True)
        digest = hashlib.sha256(data).hexdigest()
        filename = digest+'.jsonl'
        (folder / filename).write_bytes(data)
        (folder / 'manifest.json').write_bytes(encoded({'format':1,'shards':[{'file':filename,'sha256':digest,'bytes':len(data)}]}))
        with patch.object(Graph, 'parse', side_effect=AssertionError('Legacy load reparsed code')):
            graph = Graph(clone)
        try:
            graph.export()
            self.assertEqual(json.loads((folder/'manifest.json').read_bytes())['format'], 4)
            self.assertFalse((folder / filename).exists())
        finally:
            graph.close()

    def test_large_fixture_single_edit_preserves_unrelated_shards(self):
        for number in range(300):
            (self.repo/f'module_{number}.py').write_text(f'def symbol_{number}():\n    return {number}\n')
        self.graph.export()
        before = self.snapshot()
        (self.repo/'module_42.py').write_text('def symbol_42():\n    return 999\n')
        result = self.graph.export()
        after = self.snapshot()
        self.assertEqual(result['refreshed']['changed_files'], 1)
        self.assertEqual(result['refreshed']['hashed_files'], 1)
        self.assertEqual(result['rebuilt_partitions'], 1)
        self.assertGreater(result['reused_partitions'], 100)
        self.assertGreater(len(set(before)&set(after)), 100)
        self.assert_matches_clean_build()
