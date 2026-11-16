"""Configuration and Codex guidance for optional independent review."""
import json
from pathlib import Path
from .config import read_config
from .snapshot import atomic_write

GUIDANCE_START = '<!-- codemanager-review:start -->'
GUIDANCE_END = '<!-- codemanager-review:end -->'
GUIDANCE = """## Optional CodeManager review

For coding tasks, call CodeManager's `begin_task` with the actual user request at
startup. If review is disabled this is a no-op. When enabled, read the file's
`get_file_purpose` before revising it, and maintain its current purpose and
requirements with `set_file_purpose`. New requirements merge with existing ones;
remove a requirement only when the user changes it, supplying remove_requirements
and removal_request matching that actual user request. Preserve purpose through
code revisions; refresh stale code mappings, not the requirements to fit the code.
Map code you create
or edit using `map_request` (node_id, request, fulfills); use `map_change` (path,
request, fulfills) for deletions or changes outside indexed nodes. Before your
final response, call `complete_task` to run the independent reviewer. Review reads each affected file in full within file limits, checking all of its
current requirements. Node retrieval is optional for review dependencies and
extra context; node-first reading remains the coding workflow. Requirements without adequate evidence must remain unresolved. Fix findings
and review again, or report the failed review. Task completion does not authorize
a commit. Commit only when the user asks; include the matching graph and use
`commit-check`. Partial staging needs `review --staged` on the exact staged tree.
CLI equivalents: `python -m codemanager begin-task --repo . --request TEXT`,
`map-request`, `map-change`, and `complete-task --repo .`. Use the environment
where CodeManager is installed. Do not invoke these workflows from a reviewer.
"""


def settings(repo):
    value = read_config(repo).get('review', {})
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('review must be an object with a boolean enabled setting')
    command = value.get('command')
    if command is not None and (not isinstance(command, list) or not command or
                                any(not isinstance(arg, str) or not arg for arg in command)):
        raise ValueError('review.command must be a nonempty array of command arguments')
    if 'model' in value and (not isinstance(value['model'], str) or not value['model'].strip()):
        raise ValueError('review.model must be nonempty text')
    timeout = value.get('timeout', 600)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError('review.timeout must be 1..3600 seconds')
    return {'enabled': value.get('enabled', False), 'command': command,
            'model': value.get('model'), 'timeout': timeout}


def guidance_update(repo, enabled):
    path = repo / 'AGENTS.md'
    if path.is_symlink():
        raise ValueError('AGENTS.md must be a regular file to install review guidance')
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    if GUIDANCE_START in text or GUIDANCE_END in text:
        if text.count(GUIDANCE_START) != 1 or text.count(GUIDANCE_END) != 1 or text.index(GUIDANCE_START) > text.index(GUIDANCE_END):
            raise ValueError('Invalid CodeManager review guidance markers in AGENTS.md')
        start = text.index(GUIDANCE_START)
        end = text.index(GUIDANCE_END)+len(GUIDANCE_END)
        block = GUIDANCE_START+'\n'+GUIDANCE+GUIDANCE_END if enabled else ''
        return text[:start]+block+text[end:]
    if not enabled:
        return None
    return text+('\n\n' if text and not text.endswith('\n\n') else '')+GUIDANCE_START+'\n'+GUIDANCE+GUIDANCE_END+'\n'


def configure(repo, enabled=None, model=None):
    repo = Path(repo).resolve(strict=True)
    settings(repo)
    data = read_config(repo)
    if enabled is not None or model is not None:
        review = data.setdefault('review', {})
        if enabled is not None:
            review['enabled'] = enabled
        if model is not None:
            if not model.strip():
                raise ValueError('review model must be nonempty')
            review['model'] = model
        guidance = guidance_update(repo, enabled) if enabled is not None else None
        if enabled:
            from .automation import install_hook
            install_hook(repo)
        atomic_write(repo / 'codemanager.json', (json.dumps(data, indent=2, ensure_ascii=False)+'\n').encode())
        if guidance is not None:
            atomic_write(repo / 'AGENTS.md', guidance.encode())
    return settings(repo)
