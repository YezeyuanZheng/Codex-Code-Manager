import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph
from codemanager.requests import map_request
from codemanager.purposes import set_file_purpose
from codemanager.review import (begin_task, configure, context, map_change, review,
                                require_review, run_reviewer, settings, tree_id)


from review_fixture import ReviewFixture

from codemanager import file_limits

class FileLimitTests(ReviewFixture, unittest.TestCase):
    def test_oversized_file_blocks_before_reviewer_and_cannot_commit(self):
        self.change()
        self.source.write_text('x = 1\n' * 501)
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Updated calculation implementation')
        with patch('codemanager.review.run_reviewer') as runner:
            with self.assertRaisesRegex(ValueError, r'app.py: 501 lines'):
                review(self.repo)
            runner.assert_not_called()
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)
        file_limits.configure(self.repo, max_lines=600)
        self.graph.export()
        self.assertTrue(review(self.repo)['passed'])

    def test_single_line_character_limit_and_exact_line_boundary(self):
        self.change()
        self.source.write_text('#' + 'é' * 20000)
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Updated implementation')
        with patch('codemanager.review.run_reviewer') as runner:
            with self.assertRaisesRegex(ValueError, '1 lines, 20001 characters'):
                review(self.repo)
            runner.assert_not_called()
        file_limits.configure(self.repo, max_lines=20)
        self.source.write_text('x = 1\n' * 20)
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Updated implementation')
        self.assertTrue(review(self.repo)['passed'])
        self.source.write_text('x = 1\n' * 21)
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Updated implementation')
        with self.assertRaisesRegex(ValueError, '21 lines'):
            review(self.repo)

    def test_limit_settings_validation_and_staged_policy(self):
        self.change()
        file_limits.configure(self.repo, max_lines=50, max_chars=5000)
        self.graph.export()
        self.assertTrue(review(self.repo)['passed'])
        self.git('add', '.')
        # The staged policy is authoritative even if the worktree limits change.
        file_limits.configure(self.repo, max_lines=1)
        self.assertTrue(review(self.repo, staged=True)['passed'])
        self.assertTrue(require_review(self.repo)['passed'])
        original = (self.repo / 'codemanager.json').read_bytes()
        for value in (0, -1, True, '500'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'positive integer'):
                    file_limits.configure(self.repo, max_lines=value)
        self.assertEqual(original, (self.repo / 'codemanager.json').read_bytes())

    def test_deleted_oversized_file_is_allowed_and_non_utf8_new_text_is_bounded(self):

        self.source.write_text('x = 1\n' * 501)
        self.graph.export()
        self.git('add', '.')
        self.git('commit', '-qm', 'Legacy large file')
        old = self.git('rev-parse', 'HEAD').stdout.strip()
        self.source.unlink()
        self.graph.export()
        tree = tree_id(self.repo)
        # Deleting/refactoring an old oversized file is allowed: policy checks new content.
        self.assertTrue(file_limits.check_tree(self.repo, tree, {'app.py'}, file_limits.settings(self.repo))['passed'])
        self.source.write_bytes(b'#'+b'\xff'*20000)
        tree = tree_id(self.repo)
        with self.assertRaisesRegex(ValueError, '20001 characters'):
            file_limits.require(file_limits.check_tree(self.repo, tree, {'app.py'}, file_limits.settings(self.repo)))

    def test_disabled_review_still_blocks_completion_and_commit(self):
        from codemanager.review import commit_check
        self.source.write_text('x = 1\n' * 501)
        self.assertFalse(settings(self.repo)['enabled'])
        self.assertFalse(file_limits.check(self.repo)['passed'])
        with self.assertRaisesRegex(ValueError, '501 lines'):
            review(self.repo)
        self.git('add', 'app.py')
        with patch('codemanager.automation.check_snapshot') as graph_check:
            with self.assertRaisesRegex(ValueError, '501 lines'):
                commit_check(self.repo)
            graph_check.assert_not_called()

    def test_migrate_review_only_settings_without_enabling_review(self):
        path = self.repo / 'codemanager.json'
        path.write_text(json.dumps({'review': {'enabled': False, 'max_file_lines': 700,
            'max_file_chars': 30000}, 'file_limits': {'max_lines': 600},
            'embedding': {'model': 'BAAI/bge-small-en-v1.5'}}))
        self.assertEqual(file_limits.configure(self.repo), {'max_lines': 600, 'max_chars': 30000})
        data = json.loads(path.read_text())
        self.assertEqual(data['review'], {'enabled': False})
        self.assertEqual(data['embedding']['model'], 'BAAI/bge-small-en-v1.5')
        self.assertFalse(settings(self.repo)['enabled'])

    def test_standalone_cli_and_revoked_review_flags(self):
        result = subprocess.run([sys.executable, '-m', 'codemanager', 'file-limits',
            '--repo', str(self.repo), '--max-lines', '40', '--max-chars', '1000'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'max_lines': 40, 'max_chars': 1000})
        self.source.write_text('x = 1\n' * 41)
        result = subprocess.run([sys.executable, '-m', 'codemanager', 'check-length',
            '--repo', str(self.repo)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['violations'][0]['path'], 'app.py')
        result = subprocess.run([sys.executable, '-m', 'codemanager', 'review-config',
            '--repo', str(self.repo), '--max-file-lines', '40'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_changed_scope_exclusions_and_full_audit(self):
        self.source.write_text('x = 1\n' * 501)
        self.git('add', 'app.py')
        self.git('commit', '-qm', 'Legacy long source')
        self.assertTrue(file_limits.check(self.repo)['passed'])
        self.assertFalse(file_limits.check(self.repo, all_files=True)['passed'])
        (self.repo / 'binary.dat').write_bytes(b'\0' + b'x' * 25000)
        (self.repo / 'node_modules').mkdir()
        (self.repo / 'node_modules/generated.py').write_text('x\n' * 1000)
        (self.repo / 'link.py').symlink_to(self.source)
        self.assertTrue(file_limits.check(self.repo)['passed'])
        self.source.write_text('x = 1\n')
        self.assertTrue(file_limits.check(self.repo, all_files=True)['passed'])
