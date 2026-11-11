"""Commit-time inference tests use controlled vectors; no model downloads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codemanager.automation import check_snapshot, install_hook
from codemanager.embeddings import EmbeddingError, passages
from codemanager.graph import Graph


class CommitEmbeddingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        environment = patch.dict(os.environ, {'CODEMANAGER_EMBEDDING_MODEL':'', 'CODEMANAGER_SEARCH_MODE':'bm25'})
        environment.start()
        self.addCleanup(environment.stop)
        self.git('init','-q')
        self.git('config','user.name','Test')
        self.git('config','user.email','test@example.com')
        self.git('config','commit.gpgsign','false')
        (self.repo/'.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        (self.repo/'app.py').write_text('def permit(ticket):\n    return ticket.original\n')
        (self.repo/'other.py').write_text('def unrelated():\n    return 1\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)
        self.texts = []
        self.models = []
        self.fail_model = False
        self.callback = None

    def git(self, *args, check=True):
        return subprocess.run(['git','-C',str(self.repo),*args],capture_output=True,text=True,check=check)

    def stage(self):
        self.graph.export()
        self.git('add','.')

    def provider(self, name):
        self.models.append(name)
        owner = self
        class Provider:
            identity = 'test:'+name
            def documents(self, texts):
                owner.texts.extend(texts)
                if owner.callback:
                    owner.callback()
                if owner.fail_model:
                    raise EmbeddingError('Model unavailable')
                return [[1.0,0.0] for _ in texts]
        return Provider()

    def prepare(self, **kwargs):
        with patch('codemanager.embeddings.FastEmbedder', side_effect=self.provider):
            return check_snapshot(self.repo,staged=True,embeddings=True,**kwargs)

    def vectors(self):
        return [tuple(row) for row in self.graph.db.execute('SELECT * FROM embeddings ORDER BY node_id,chunk,model')]

    def test_embeddings_are_persisted_and_unchanged_vectors_are_reused(self):
        self.stage()
        before = self.git('ls-files','--stage','-z').stdout
        result = self.prepare()
        self.assertEqual(result['embeddings']['embedded_passages'],2)
        self.assertEqual(len(self.vectors()),2)
        self.texts.clear()
        self.assertEqual(self.prepare()['embeddings']['embedded_passages'],0)
        self.assertFalse(self.texts)
        (self.repo/'app.py').write_text('def permit(ticket):\n    return ticket.updated\n')
        self.stage()
        result = self.prepare()
        self.assertEqual(result['embeddings']['embedded_passages'],1)
        self.assertEqual(len(self.texts),1)
        self.assertIn('ticket.updated',self.texts[0])
        self.assertNotEqual(before,self.git('ls-files','--stage','-z').stdout)
        self.texts.clear()
        self.prepare()
        self.assertFalse(self.texts)

    def test_partial_staging_embeds_staged_content_without_changing_working_graph(self):
        self.stage()
        before = self.git('ls-files','--stage','-z').stdout
        (self.repo/'app.py').write_text('def permit(ticket):\n    return ticket.unstaged\n')
        self.graph.index()
        self.prepare()
        self.assertTrue(any('ticket.original' in text for text in self.texts))
        self.assertFalse(any('ticket.unstaged' in text for text in self.texts))
        self.assertEqual(before,self.git('ls-files','--stage','-z').stdout)
        current = self.graph.db.execute("SELECT * FROM nodes WHERE name='permit'").fetchone()
        self.assertIn('ticket.unstaged',current['body'])
        cached = self.graph.db.execute('SELECT digest FROM embeddings WHERE node_id=?',(current['id'],)).fetchone()[0]
        current_digest = hashlib.sha256(next(passages(current))[0].encode()).hexdigest()
        self.assertNotEqual(cached,current_digest)

    def test_staged_model_setting_and_explicit_override(self):
        path = self.repo/'codemanager.json'
        path.write_text(json.dumps({'embedding':{'model':'test/staged'}}))
        self.stage()
        path.write_text(json.dumps({'embedding':{'model':'test/unstaged'}}))
        self.prepare()
        self.assertEqual(self.models[-1],'test/staged')
        self.prepare(model='test/explicit')
        self.assertEqual(self.models[-1],'test/explicit')
        self.assertEqual({row[2] for row in self.vectors()},{'test:test/explicit'})

    def test_inference_failure_blocks_and_rolls_back_vector_changes(self):
        self.stage()
        self.prepare()
        before = self.vectors()
        (self.repo/'app.py').write_text('def permit(ticket):\n    return ticket.updated\n')
        self.stage()
        self.fail_model = True
        with self.assertRaisesRegex(ValueError,'Commit blocked: staged embedding refresh failed'):
            self.prepare()
        self.assertEqual(before,self.vectors())

    def test_stale_graph_is_rejected_before_inference(self):
        self.stage()
        (self.repo/'app.py').write_text('def permit(ticket):\n    return ticket.updated\n')
        self.git('add','app.py')
        with patch('codemanager.embeddings.FastEmbedder',side_effect=AssertionError('Inference must follow graph validation')):
            with self.assertRaisesRegex(ValueError,'does not match staged code'):
                check_snapshot(self.repo,staged=True,embeddings=True)

    def test_index_changes_during_inference_block_commit(self):
        self.stage()
        def change_index():
            (self.repo/'other.py').write_text('def changed_during_commit():\n    pass\n')
            self.git('add','other.py')
        self.callback = change_index
        with self.assertRaisesRegex(ValueError,'Staged files changed during'):
            self.prepare()

    def test_embeddings_flag_requires_staged_validation(self):
        with self.assertRaisesRegex(ValueError,'requires --staged'):
            check_snapshot(self.repo,embeddings=True)

    def test_installer_upgrades_owned_hook_and_real_commit_blocks_on_model_failure(self):
        self.stage()
        hook = self.repo/'.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\n# CodeManager staged graph check v1\nexit 0\n')
        install_hook(self.repo)
        self.assertIn('--embeddings',hook.read_text())
        # Simulate a broken local runtime in the subprocess, independently of
        # whether FastEmbed happens to be installed on the developer's machine.
        runtime = self.base/'broken-runtime'
        package = runtime/'fastembed'
        package.mkdir(parents=True)
        (package/'__init__.py').write_text('raise RuntimeError("Controlled model-load failure")\n')
        metadata = runtime/'fastembed-0.9.1.dist-info'
        metadata.mkdir()
        (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: fastembed\nVersion: 0.9.1\n')
        with patch.dict(os.environ,{'PYTHONPATH':str(runtime)+os.pathsep+os.environ.get('PYTHONPATH','')}):
            result = self.git('commit','-qm','Must be blocked',check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Commit blocked: staged embedding refresh failed',result.stderr)
        self.assertNotEqual(self.git('rev-parse','--verify','HEAD',check=False).returncode,0)
