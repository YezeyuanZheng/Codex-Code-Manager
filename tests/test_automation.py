import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codemanager.automation import check_snapshot, install_hook
from codemanager.graph import Graph


class AutomationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'CODEMANAGER_SEARCH_MODE': 'bm25'})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo with spaces'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'commit.gpgsign', 'false')
        (self.repo / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        (self.repo / 'app.py').write_text('def original():\n    return 1\n')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True, check=check)

    def export(self):
        graph = Graph(self.repo, load_snapshot=False)
        try:
            graph.export()
        finally:
            graph.close()

    def test_staged_partial_edit_uses_index_and_never_changes_it(self):
        self.export()
        self.git('add', '.')
        tree = self.git('write-tree').stdout
        # An unstaged edit does not invalidate a snapshot matching the staged code.
        (self.repo / 'app.py').write_text('def unstaged():\n    return 2\n')
        self.assertTrue(check_snapshot(self.repo, staged=True)['valid'])
        with self.assertRaisesRegex(ValueError, 'does not match working tree'):
            check_snapshot(self.repo)
        self.assertEqual(tree, self.git('write-tree').stdout)
        self.git('add', 'app.py')
        with self.assertRaisesRegex(ValueError, 'does not match staged code'):
            check_snapshot(self.repo, staged=True)
        self.export()
        self.git('add', '.codemanager/index')
        self.assertTrue(check_snapshot(self.repo, staged=True)['valid'])
        self.assertTrue(check_snapshot(self.repo)['valid'])

    def test_hook_blocks_stale_commit_then_allows_refreshed_commit(self):
        self.export()
        self.git('add', '.')
        install_hook(self.repo)
        self.git('commit', '-qm', 'Fresh graph')
        (self.repo / 'app.py').write_text('def changed():\n    pass\n')
        self.git('add', 'app.py')
        failed = self.git('commit', '-qm', 'Stale graph', check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('does not match staged code', failed.stderr)
        self.export()
        self.git('add', '.codemanager/index')
        self.git('commit', '-qm', 'Updated graph')

    def test_missing_bundle_and_deletion(self):
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'snapshot is missing'):
            check_snapshot(self.repo, staged=True)
        self.export()
        self.git('add', '.')
        (self.repo / 'app.py').unlink()
        self.git('add', '-u')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            check_snapshot(self.repo, staged=True)
        self.export()
        self.git('add', '.codemanager/index')
        self.assertTrue(check_snapshot(self.repo, staged=True)['valid'])

    def test_staged_check_rejects_obsolete_shards(self):
        self.export()
        folder = self.repo / '.codemanager' / 'index'
        (folder / ('0'*64 + '.jsonl')).write_text('obsolete')
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'Obsolete graph shards'):
            check_snapshot(self.repo, staged=True)
        self.export()
        self.git('add', '.codemanager/index')
        self.assertTrue(check_snapshot(self.repo, staged=True)['valid'])

    def test_existing_hook_and_custom_hooks_path_are_preserved(self):
        hook = self.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\necho existing\n')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            install_hook(self.repo)
        self.assertEqual(hook.read_text(), '#!/bin/sh\necho existing\n')
        hook.unlink()
        self.git('config', 'core.hooksPath', '/tmp/shared-hooks')
        with self.assertRaisesRegex(ValueError, 'hooksPath'):
            install_hook(self.repo)

    def start(self, command):
        return subprocess.Popen([sys.executable, '-m', 'codemanager', command,
                                 '--repo', str(self.repo), '--interval', '0.1'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def wait_for_manifest(self, previous=None):
        manifest = self.repo / '.codemanager' / 'index' / 'manifest.json'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if manifest.exists():
                value = manifest.read_bytes()
                if value != previous:
                    return value
            time.sleep(0.03)
        self.fail('Automatic refresh did not publish a changed manifest')

    def stop(self, proc):
        proc.terminate()
        proc.communicate(timeout=5)

    def test_watch_updates_additions_and_deletions_without_rewriting_unchanged_manifest(self):
        proc = self.start('watch')
        try:
            first = self.wait_for_manifest()
            manifest = self.repo / '.codemanager' / 'index' / 'manifest.json'
            modified = manifest.stat().st_mtime_ns
            time.sleep(0.25)
            self.assertEqual(manifest.stat().st_mtime_ns, modified)
            (self.repo / 'new.ts').write_text('export const freshSymbol = 1;')
            second = self.wait_for_manifest(first)
            (self.repo / 'new.ts').unlink()
            self.assertEqual(self.wait_for_manifest(second), first)
        finally:
            self.stop(proc)

    def test_mcp_idle_refresh_and_query_freshness(self):
        proc = self.start('serve')
        try:
            first = self.wait_for_manifest()
            (self.repo / 'app.py').write_text('def codex_edit():\n    return 99\n')
            self.wait_for_manifest(first)
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': 'search_code', 'arguments': {'query': 'codex_edit'}}}
            output, errors = proc.communicate(json.dumps(request)+'\n', timeout=5)
            self.assertEqual(proc.returncode, 0, errors)
            result = json.loads(output)['result']
            self.assertFalse(result['isError'])
            self.assertTrue(json.loads(result['content'][0]['text'])['results'])
        finally:
            if proc.poll() is None:
                self.stop(proc)


if __name__ == '__main__':
    unittest.main()
