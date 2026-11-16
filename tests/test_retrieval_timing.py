import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codemanager.retrieval_timing import file_search, record_timing, span
from codemanager.retrieval_benchmark import measure


class RetrievalTimingTests(unittest.TestCase):
    def test_log_pairs_measurements_without_storing_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = {'status': 'ok', 'total_ms': 12}
            with patch('codemanager.retrieval_timing.file_search', return_value={'total_ms': 2}):
                record_timing(folder, 'private request', metrics)
            raw = (Path(folder) / '.codemanager/retrieval-timings.jsonl').read_text()
            row = json.loads(raw)
            self.assertNotIn('private request', raw)
            self.assertEqual(row['codemanager'], metrics)
            self.assertEqual(row['rg_read_proxy']['total_ms'], 2)
            self.assertEqual(len(row['prompt_sha256']), 64)

    def test_logging_failure_and_opt_out_do_not_break_retrieval(self):
        with patch.dict(os.environ, {'CODEMANAGER_TIMING': '0'}):
            with patch('codemanager.retrieval_timing.file_search') as search:
                record_timing('/unused', 'query', {})
                search.assert_not_called()
        with patch('codemanager.retrieval_timing.file_search', side_effect=OSError('disk full')):
            record_timing('/unused', 'query', {})

    def test_failed_stage_still_has_duration(self):
        metrics = {}
        with self.assertRaises(ValueError):
            with span(metrics, 'search_ms'):
                raise ValueError('missing model')
        self.assertGreaterEqual(metrics['search_ms'], 0)

    def test_benchmark_extracts_evidence_paths(self):
        content = 'Retrieved evidence (JSON; at most 6000 serialized bytes):\n'
        content += json.dumps({'results': [{'path': 'sample.py'}]})
        value = {'metrics': {'status': 'ok'}, 'response': {
            'hookSpecificOutput': {'additionalContext': content}}}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(value))
        with patch('codemanager.retrieval_benchmark.subprocess.run', return_value=result):
            self.assertEqual(measure('.', 'query')['paths'], ['sample.py'])

    def test_proxy_reads_matches_and_reports_missing_rg(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'sample.py').write_text('def multiply(a, b):\n    return a * b\n')
            metrics = file_search(folder, 'multiply', budget=16)
            self.assertEqual(metrics['status'], 'ok')
            self.assertEqual(metrics['hits'], 1)
            self.assertEqual(metrics['result_bytes'], 16)
            with patch('codemanager.retrieval_timing.subprocess.run', side_effect=FileNotFoundError()):
                self.assertEqual(file_search(folder, 'multiply')['status'], 'error')
