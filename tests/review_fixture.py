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


class ReviewFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'commit.gpgsign', 'false')
        (self.repo / '.gitignore').write_text('.codemanager/*\n!.codemanager/index/\n')
        self.source = self.repo / 'app.py'
        self.source.write_text('def calculate(a, b):\n    return a + b\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)
        self.graph.export()
        self.git('add', '.')
        self.git('commit', '-qm', 'Baseline')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.runner = self.root / 'reviewer.py'
        self.runner.write_text('import json, sys\npayload = json.load(sys.stdin)\n'
                               'assert payload["mappings"] or payload["changes"]\n'
                               'assert "diff" in payload and "requests" in payload\n'
                               'print(json.dumps({"passed": True, "summary": "Checked", "findings": []}))\n')

    def git(self, *args, check=True):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=check,
                              capture_output=True, text=True)

    def enable(self):
        (self.repo / 'codemanager.json').write_text(json.dumps({'review': {
            'enabled': True, 'command': [sys.executable, str(self.runner)]}}))

    def change(self):
        self.enable()
        begin_task(self.graph, 'Multiply instead of adding')
        set_file_purpose(self.graph, 'app.py', 'Calculate a product', ['Multiply two numbers'], 'Multiply instead of adding')
        self.source.write_text('def calculate(a, b):\n    return a * b\n')
        self.graph.export()
        node = self.graph.db.execute("SELECT id FROM nodes WHERE name='calculate'").fetchone()[0]
        map_request(self.graph, node, 'Multiply instead of adding', 'Return the product of the arguments')
