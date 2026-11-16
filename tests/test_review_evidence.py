import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from codemanager.review import review
from codemanager.review_evidence import BUDGET, EvidenceGraph, prepare_evidence, read_evidence
from review_fixture import ReviewFixture


class ReviewEvidenceTests(ReviewFixture, unittest.TestCase):
    def prepare(self):
        self.change()
        folder = self.root / 'evidence'
        folder.mkdir()
        node = self.graph.db.execute("SELECT id,name FROM nodes WHERE name='calculate'").fetchone()
        evidence, access, cache = prepare_evidence(self.graph, folder, ['Multiply two numbers'],
            [dict(node)], {'app.py': '+added line\n'*1000})
        return evidence, access, cache

    def test_cache_allows_bounded_queries_and_node_expansion_without_writes(self):
        evidence, access, cache = self.prepare()
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False).encode()), BUDGET)
        before = hashlib.sha256(cache.read_bytes()).hexdigest()
        result = read_evidence(self.repo, cache, query='calculate', budget=800)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 800)
        result = read_evidence(self.repo, cache, node_ids=['app.py:1:calculate'], budget=1200)
        self.assertIn('a * b', result['results'][0]['body'])
        reader = EvidenceGraph(self.repo, cache)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute('DELETE FROM nodes')
        finally:
            reader.close()
        self.assertEqual(before, hashlib.sha256(cache.read_bytes()).hexdigest())
        self.assertEqual(list(cache.parent.iterdir()), [cache])
        cli = subprocess.run(access['command']+['--node-id', 'app.py:1:calculate', '--budget', '1200'],
                             capture_output=True, text=True, check=True)
        self.assertLessEqual(len(json.dumps(json.loads(cli.stdout)).encode()), 1200)
        with self.assertRaises(ValueError):
            read_evidence(self.repo, cache, query='calculate', budget=6001)

    def test_diff_retrieval_is_bounded_and_can_continue_without_dumping_entire_diff(self):
        _, _, cache = self.prepare()
        first = read_evidence(self.repo, cache, diff_path='app.py', budget=500)
        self.assertTrue(first['truncated'])
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=False).encode()), 500)
        second = read_evidence(self.repo, cache, diff_path='app.py', offset=first['next_offset'], budget=500)
        self.assertEqual(second['offset'], first['next_offset'])
        self.assertGreater(second['next_offset'], first['next_offset'])
        with self.assertRaises(ValueError):
            read_evidence(self.repo, cache, diff_path='unknown.py')

    def test_review_starts_from_bounded_evidence_and_marks_large_diff_incomplete(self):
        self.change()
        self.source.write_text('def calculate(a,b):\n    return a*b\n'+'# revised calculation detail\n'*200)
        from codemanager.review import map_change
        map_change(self.graph, 'app.py', 'Multiply instead of adding', 'Return product and document calculation')
        def inspect(snapshot, payload, *args):
            self.assertIn('app.py', payload['review_files'])
            self.assertTrue(payload['diff_truncated'])
            self.assertLessEqual(len(payload['diff'].encode()), 2000)
            self.assertLessEqual(len(json.dumps(payload['evidence'], ensure_ascii=False).encode()), 6000)
            command = payload['evidence_access']['command']
            result = subprocess.run(command+['--query', 'calculate'], capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(result.stdout)['results'])
            return dict(passed=False, summary='Insufficient evidence', findings=['Requirement remains unresolved'])
        with patch('codemanager.review.run_reviewer', side_effect=inspect):
            self.assertFalse(review(self.repo)['passed'])

    def test_reviewer_cannot_modify_retrieval_evidence_and_approve(self):
        self.change()
        def mutate(snapshot, payload, *args):
            command = payload['evidence_access']['command']
            cache = Path(command[command.index('--cache')+1])
            cache.write_bytes(b'changed')
            return dict(passed=True, summary='OK', findings=[])
        with patch('codemanager.review.run_reviewer', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'modified its evidence cache'):
                review(self.repo)
