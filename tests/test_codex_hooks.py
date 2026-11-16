import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from codemanager.codex_hooks import install, stop_hook
from review_fixture import ReviewFixture


class CodexHookTests(ReviewFixture, unittest.TestCase):
    def test_installs_guidance_and_preserves_other_hook_handlers(self):
        path = self.repo / '.codex/hooks.json'
        path.parent.mkdir()
        custom = {'type': 'command', 'command': 'echo existing'}
        path.write_text(json.dumps({'description': 'Existing hooks', 'hooks': {
            'Stop': [{'hooks': [custom]}], 'SessionStart': [{'hooks': [custom]}],
            'UserPromptSubmit': [{'hooks': [custom]}]}}))
        (self.repo / 'AGENTS.md').write_text('Existing instructions.\n')
        install(self.repo)
        first = path.read_bytes()
        guidance = (self.repo / 'AGENTS.md').read_bytes()
        install(self.repo)
        self.assertEqual(first, path.read_bytes())
        self.assertEqual(guidance, (self.repo / 'AGENTS.md').read_bytes())
        self.assertIn(b'Existing instructions.', guidance)
        self.assertIn(b'whether or not review is enabled', guidance)
        hooks = json.loads(first)['hooks']
        self.assertEqual(hooks['Stop'][0]['hooks'], [custom])
        self.assertEqual(hooks['SessionStart'][0]['hooks'], [custom])
        self.assertEqual(len(hooks['Stop']), 2)
        self.assertEqual(hooks['UserPromptSubmit'][0]['hooks'], [custom])
        self.assertIn('codex-context-hook', hooks['UserPromptSubmit'][1]['hooks'][0]['command'])
        self.assertEqual(hooks['UserPromptSubmit'][1]['hooks'][0]['additionalContextLimit'], 0)
        self.assertFalse((self.repo / 'codemanager.json').exists())

    def test_stop_hook_blocks_then_allows_after_fix_with_review_disabled(self):
        install(self.repo)
        handler = json.loads((self.repo / '.codex/hooks.json').read_text())['hooks']['Stop'][0]['hooks'][0]
        self.source.write_text('x = 1\n' * 501)
        result = subprocess.run(handler['command'], shell=True,
            input=json.dumps({'cwd': str(self.repo), 'stop_hook_active': False}),
            text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)['decision'], 'block')
        self.assertIn('501 lines', json.loads(result.stdout)['reason'])
        self.source.write_text('x = 1\n')
        self.assertEqual(stop_hook(self.repo, {'stop_hook_active': True}), {})

    def test_invalid_config_blocks_and_reviewer_does_not_recurse(self):
        (self.repo / 'codemanager.json').write_text('{"file_limits":{"max_lines":false}}')
        self.assertEqual(stop_hook(self.repo, {})['decision'], 'block')
        with patch.dict(os.environ, {'CODEMANAGER_REVIEWER': '1'}):
            self.assertEqual(stop_hook(self.repo, {}), {})

    def test_invalid_existing_hook_is_preserved(self):
        path = self.repo / '.codex/hooks.json'
        path.parent.mkdir()
        content = '{"hooks":{"Stop":"invalid"}}'
        path.write_text(content)
        with self.assertRaises(ValueError):
            install(self.repo)
        self.assertEqual(path.read_text(), content)
        self.assertFalse((self.repo / 'AGENTS.md').exists())
