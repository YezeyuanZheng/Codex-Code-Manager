"""Shared retrieval mode selection for library, prompt hook, CLI, and MCP."""
import json
import os
from pathlib import Path
from .config import read_config
from .snapshot import atomic_write

MODES = ('hybrid', 'bm25', 'semantic')


def selected_mode(repo, override=None):
    data = read_config(repo).get('retrieval', {})
    if not isinstance(data, dict):
        raise ValueError('retrieval must be an object')
    saved = data.get('mode', 'hybrid')
    if saved not in MODES:
        raise ValueError('retrieval.mode must be hybrid, bm25, or semantic')
    mode = override if override is not None else os.environ.get('CODEMANAGER_SEARCH_MODE', saved)
    if mode not in MODES:
        raise ValueError('mode must be hybrid, bm25, or semantic')
    return mode


def configure(repo, mode=None):
    repo = Path(repo).resolve(strict=True)
    data = read_config(repo)
    if mode is not None:
        if mode not in MODES:
            raise ValueError('mode must be hybrid, bm25, or semantic')
        data.setdefault('retrieval', {})['mode'] = mode
        atomic_write(repo/'codemanager.json', (json.dumps(data, indent=2)+'\n').encode())
    return {'mode': selected_mode(repo), 'saved_mode': data.get('retrieval', {}).get('mode')}
