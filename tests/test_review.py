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

class ReviewTests(ReviewFixture, unittest.TestCase):
    def test_disabled_is_noop_and_enable_installs_gate(self):
        index = self.git('write-tree').stdout
        self.assertFalse(settings(self.repo)['enabled'])
        self.assertEqual(begin_task(self.graph, 'task')['status'], 'disabled')
        self.assertEqual(review(self.repo)['status'], 'disabled')
        with self.assertRaisesRegex(ValueError, 'optional review'):
            map_request(self.graph, 'app.py:1:calculate', 'task', 'intent')
        self.assertFalse(require_review(self.repo)['required'])
        self.assertEqual(index, self.git('write-tree').stdout)
        self.assertTrue(configure(self.repo, True)['enabled'])
        self.assertIn('commit-check', (self.repo / '.git/hooks/pre-commit').read_text())

    def test_task_completion_no_commit_or_staging_and_exact_commit_match(self):
        self.change()
        staged_before = self.git('write-tree').stdout
        result = review(self.repo)
        self.assertTrue(result['passed'])
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD').stdout.strip())
        self.assertEqual(staged_before, self.git('write-tree').stdout)
        self.git('add', '.')
        self.assertTrue(require_review(self.repo)['passed'])
        # A mapping-only change also invalidates approval.
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Use multiplication')
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)
        self.assertTrue(review(self.repo, staged=True)['passed'])
        self.assertTrue(require_review(self.repo)['passed'])
        # Code changes invalidate review, even before refreshing the graph.
        self.source.write_text('def calculate(a, b):\n    return 0\n')
        self.git('add', 'app.py')
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)

    def test_failure_invalid_output_and_change_during_review_block(self):
        self.change()
        self.runner.write_text('import json\nprint(json.dumps({"passed":False,"summary":"Wrong","findings":["Bug"]}))\n')
        self.assertFalse(review(self.repo)['passed'])
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)
        self.runner.write_text('print("not JSON")\n')
        with self.assertRaisesRegex(ValueError, 'Invalid reviewer response'):
            review(self.repo)
        def mutate(*args):
            self.source.write_text('def calculate(a,b):\n    return 0\n')
            return dict(passed=True, summary='OK', findings=[])
        with patch('codemanager.review.run_reviewer', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during review'):
                review(self.repo)

    def test_missing_mapping_deletion_and_stale_deletion_intent(self):
        self.enable()
        begin_task(self.graph, 'Remove obsolete calculator')
        set_file_purpose(self.graph, 'app.py', 'Calculate two numbers', ['Calculate two numbers'], 'Original calculator request')
        self.source.unlink()
        self.graph.export()
        with self.assertRaisesRegex(ValueError, 'need request mappings'):
            review(self.repo)
        map_change(self.graph, 'app.py', 'Remove obsolete calculator', 'Remove the obsolete calculator module')
        self.assertTrue(review(self.repo)['passed'])
        self.source.write_text('reintroduced = True\n')
        self.graph.export()
        with self.assertRaisesRegex(ValueError, 'Stale file change mapping'):
            review(self.repo)

    def test_uncommitted_tasks_accumulate_and_new_commit_starts_fresh_scope(self):
        self.change()
        begin_task(self.graph, 'Add documentation')
        self.assertEqual(len(context(self.graph)['requests']), 2)
        self.assertTrue(self.graph.db.execute("SELECT request_mapping FROM nodes WHERE name='calculate'").fetchone()[0] != '[]')
        review(self.repo)
        self.git('add', '.')
        self.git('commit', '-qm', 'Reviewed change')
        # Historical mapping stays readable after commit until a new task starts.
        self.graph.export()
        self.assertTrue(context(self.graph)['requests'])
        begin_task(self.graph, 'Next task')
        self.assertEqual(context(self.graph)['requests'], ['Next task'])
        self.assertFalse(self.graph.db.execute("SELECT 1 FROM nodes WHERE request_mapping!='[]'").fetchone())
        self.assertFalse(context(self.graph)['changes'])

    def test_staged_review_uses_staged_policy_despite_working_tree_disable(self):
        self.change()
        review(self.repo)
        self.git('add', '.')
        (self.repo / 'codemanager.json').write_text('{"review":{"enabled":false}}')
        self.assertTrue(require_review(self.repo)['passed'])
        # Staged config still enables review even when the worktree disables it.
        self.runner.write_text('print("{}")\n')
        with self.assertRaisesRegex(ValueError, 'Invalid reviewer response'):
            review(self.repo, staged=True)
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)

    def test_codex_runner_arguments_and_timeout(self):
        def fake(command, **kwargs):
            self.assertIn('--sandbox', command)
            self.assertIn('read-only', command)
            self.assertIn('Review each affected text/source file individually in full', kwargs['input'])
            self.assertIn('no node-retrieval prerequisite applies to the reviewer', kwargs['input'])
            self.assertNotIn('Do not read whole files by default', kwargs['input'])
            self.assertIn('--ignore-user-config', command)
            self.assertIn('review-model', command)
            answer = Path(command[command.index('--output-last-message')+1])
            answer.write_text('{"passed":true,"summary":"OK","findings":[]}')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch('codemanager.review.subprocess.run', side_effect=fake):
            self.assertTrue(run_reviewer(self.repo, {}, dict(command=None, model='review-model', timeout=10), self.root)['passed'])
        with patch('codemanager.review.subprocess.run', side_effect=subprocess.TimeoutExpired('codex', 1)):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                run_reviewer(self.repo, {}, dict(command=None, model=None, timeout=1), self.root)

    def test_real_commit_hook_blocks_without_review_then_accepts_approved_tree(self):
        self.change()
        configure(self.repo, True)
        self.graph.export()
        self.git('add', '.')
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'fastembed.py').write_text(
            'class TextEmbedding:\n'
            '    def __init__(self, **kwargs): pass\n'
            '    def passage_embed(self, texts, **kwargs):\n'
            '        return [[1.0, 0.0] for _ in texts]\n')
        metadata = runtime / 'fastembed-0.9.1.dist-info'
        metadata.mkdir()
        (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: fastembed\nVersion: 0.9.1\n')
        with patch.dict(os.environ, {'PYTHONPATH': str(runtime)+os.pathsep+os.environ.get('PYTHONPATH', '')}):
            failed = self.git('commit', '-qm', 'Missing review', check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('passing review', failed.stderr)
            self.assertEqual(self.base, self.git('rev-parse', 'HEAD').stdout.strip())
            self.assertTrue(review(self.repo)['passed'])
            self.git('commit', '-qm', 'Reviewed implementation')
        manifest = json.loads((self.repo / '.codemanager/index/manifest.json').read_text())
        self.assertEqual(manifest['review']['base'], self.base)
        self.assertIn('Multiply instead of adding', manifest['review']['requests'])

    def test_guidance_preserves_existing_instructions_and_can_be_disabled(self):
        path = self.repo / 'AGENTS.md'
        path.write_text('Existing project instructions.\n')
        configure(self.repo, True)
        first = path.read_text()
        configure(self.repo, True)
        self.assertEqual(first, path.read_text())
        self.assertIn('Existing project instructions.', first)
        self.assertIn('complete_task', first)
        configure(self.repo, False)
        self.assertIn('Existing project instructions.', path.read_text())
        self.assertNotIn('complete_task', path.read_text())

    def test_reviewer_cannot_approve_a_modified_snapshot(self):
        self.change()
        def mutate(snapshot, *args):
            (snapshot / 'app.py').write_text('unreviewed = True\n')
            return dict(passed=True, summary='OK', findings=[])
        with patch('codemanager.review.run_reviewer', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'modified its checkout'):
                review(self.repo)

    def test_revision_retains_multiplication_requirement_after_mapping_invalidates(self):
        from codemanager.purposes import purposes
        self.change()
        self.source.write_text('def subtract(a, b):\n    return a - b\n')
        self.graph.export()
        self.assertFalse(self.graph.db.execute("SELECT 1 FROM nodes WHERE request_mapping!='[]'").fetchone())
        set_file_purpose(self.graph, 'app.py', 'Calculator with multiplication and subtraction',
                         ['Subtract two numbers'], 'Add subtraction while keeping multiplication')
        entries = purposes(self.graph)['app.py']['requirements']
        self.assertEqual([r['text'] for r in entries], ['Multiply two numbers', 'Subtract two numbers'])
        map_request(self.graph, 'app.py:1:subtract', 'Add subtraction while keeping multiplication', 'Subtract two numbers')
        def assess(snapshot, payload, *args):
            requirements = [r['text'] for r in payload['file_purposes']['app.py']['requirements']]
            self.assertIn('Multiply two numbers', requirements)
            self.assertIn('Subtract two numbers', requirements)
            source = (snapshot / 'app.py').read_text()
            passed = 'def multiply' in source and 'def subtract' in source
            return dict(passed=passed, summary='Check all current requirements', findings=[] if passed else ['Multiplication is missing'])
        with patch('codemanager.review.run_reviewer', side_effect=assess):
            self.assertFalse(review(self.repo)['passed'])
            self.source.write_text('def multiply(a, b):\n    return a * b\n\ndef subtract(a, b):\n    return a - b\n')
            self.graph.export()
            map_request(self.graph, 'app.py:1:multiply', 'Add subtraction while keeping multiplication', 'Retain multiplication')
            map_request(self.graph, 'app.py:4:subtract', 'Add subtraction while keeping multiplication', 'Add subtraction')
            self.assertTrue(review(self.repo)['passed'])

    def test_purpose_removal_needs_explicit_user_request_and_additions_merge(self):
        from codemanager.purposes import purposes
        self.change()
        set_file_purpose(self.graph, 'app.py', 'Current calculator', [], 'Keep existing requirements')
        self.assertEqual(len(purposes(self.graph)['app.py']['requirements']), 1)
        with self.assertRaisesRegex(ValueError, 'removal_request'):
            set_file_purpose(self.graph, 'app.py', 'Subtraction calculator', ['Subtract two numbers'],
                             'Replace multiplication with subtraction', ['Multiply two numbers'])
        request = 'Replace multiplication with subtraction'
        set_file_purpose(self.graph, 'app.py', 'Subtraction calculator', ['Subtract two numbers'],
                         request, ['Multiply two numbers'], request)
        self.assertEqual([r['text'] for r in purposes(self.graph)['app.py']['requirements']], ['Subtract two numbers'])

    def test_purpose_survives_rebuild_clone_scope_reset_and_staged_validation(self):
        import shutil
        from codemanager.automation import check_snapshot
        from codemanager.purposes import purposes
        self.change()
        self.graph.export(full=True)
        saved = purposes(self.graph)
        check_snapshot(self.repo, full=True)
        self.git('add', '.')
        set_file_purpose(self.graph, 'app.py', 'Product and difference', ['Subtract two numbers'], 'Also subtract')
        # The staged graph uses its own purpose, not the working-tree metadata.
        self.assertTrue(check_snapshot(self.repo, staged=True)['valid'])
        self.git('commit', '-qm', 'Staged multiplication purpose')
        begin_task(self.graph, 'New task')
        self.assertTrue(purposes(self.graph)['app.py']['requirements'])
        clone = self.root / 'clone'
        shutil.copytree(self.repo, clone, ignore=shutil.ignore_patterns('.git', 'graph.sqlite3*', 'snapshot.lock'))
        recovered = Graph(clone, use_git=False)
        try:
            recovered.export(full=True)
            self.assertEqual(purposes(recovered), purposes(self.graph))
        finally:
            recovered.close()
        set_file_purpose(self.graph, 'app.py', 'Other current summary', [], 'Reword purpose')
        self.graph.export()
        self.assertNotEqual(saved, purposes(self.graph))

    def test_review_payload_limits_purposes_to_affected_files_and_invalidates_approval(self):
        self.enable()
        (self.repo / 'unrelated.py').write_text('def helper():\n    return 1\n')
        set_file_purpose(self.graph, 'unrelated.py', 'An unchanged helper', ['Keep helper behavior'], 'Existing helper requirements')
        self.git('add', '.')
        self.git('commit', '-qm', 'Existing helper purpose')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.change()
        def check_payload(snapshot, payload, *args):
            self.assertEqual(set(payload['file_purposes']), {'app.py'})
            return dict(passed=True, summary='All file requirements checked', findings=[])
        with patch('codemanager.review.run_reviewer', side_effect=check_payload):
            review(self.repo)
        self.git('add', '.')
        self.assertTrue(require_review(self.repo)['passed'])
        set_file_purpose(self.graph, 'app.py', 'Updated calculator purpose', ['Handle decimals'], 'Also handle decimals')
        self.git('add', '.')
        with self.assertRaisesRegex(ValueError, 'passing review'):
            require_review(self.repo)

    def test_file_purpose_cli_and_legacy_graph_without_purposes(self):
        import shutil
        self.enable()
        command = [sys.executable, '-m', 'codemanager']
        result = subprocess.run(command+['set-file-purpose', '--repo', str(self.repo),
            '--path', 'app.py', '--purpose', 'Calculator', '--requirement', 'Multiply numbers',
            '--request', 'Build a calculator'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['file_purpose']['purpose'], 'Calculator')
        result = subprocess.run(command+['file-purpose', '--repo', str(self.repo), '--path', 'app.py'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['file_purpose']['requirements'][0]['text'], 'Multiply numbers')
        clone = self.root / 'legacy'
        shutil.copytree(self.repo, clone, ignore=shutil.ignore_patterns('.git', 'graph.sqlite3*', 'snapshot.lock'))
        manifest = clone / '.codemanager/index/manifest.json'
        info = json.loads(manifest.read_text())
        info['format'] = 3
        info.pop('purposes')
        manifest.write_text(json.dumps(info))
        recovered = Graph(clone, use_git=False)
        try:
            from codemanager.purposes import purposes
            self.assertEqual(purposes(recovered), {})
            recovered.export()
            self.assertEqual(json.loads(manifest.read_text())['format'], 4)
        finally:
            recovered.close()

    def test_purpose_only_revision_is_reviewed_without_source_diff(self):
        self.change()
        self.git('add', '.')
        self.git('commit', '-qm', 'Multiplication implementation')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        begin_task(self.graph, 'Also support subtraction')
        set_file_purpose(self.graph, 'app.py', 'Multiplication and subtraction',
                         ['Subtract two numbers'], 'Also support subtraction')
        def reject_missing_subtraction(snapshot, payload, *args):
            self.assertEqual(payload['diff'], '')
            self.assertEqual(set(payload['file_purposes']), {'app.py'})
            self.assertEqual(len(payload['file_purposes']['app.py']['requirements']), 2)
            return dict(passed=False, summary='Subtraction is unimplemented', findings=['Missing subtraction'])
        with patch('codemanager.review.run_reviewer', side_effect=reject_missing_subtraction):
            self.assertFalse(review(self.repo)['passed'])
