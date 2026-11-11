"""Small, shareable repository configuration for local model selection."""
import json
import os
from pathlib import Path
import re

DEFAULT_MODEL = 'jinaai/jina-embeddings-v2-base-code'
CONFIG_FILE = 'codemanager.json'
PRESETS = [DEFAULT_MODEL, 'BAAI/bge-small-en-v1.5', 'sentence-transformers/all-MiniLM-L6-v2',
           'nomic-ai/nomic-embed-text-v1.5']


def validate_model(value):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Model must be a model ID such as jinaai/jina-embeddings-v2-base-code (at most 128 characters)')
    return value


def read_config(repo):
    path = Path(repo) / CONFIG_FILE
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{CONFIG_FILE} must be a regular file')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f'Invalid {CONFIG_FILE}: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('embedding', {}), dict):
        raise ValueError(f'{CONFIG_FILE} must be an object with an embedding object')
    if 'model' in data.get('embedding', {}):
        validate_model(data['embedding']['model'])
    return data


def selected_model(repo, override=None):
    if override is not None:
        return validate_model(override), 'override'
    if os.environ.get('CODEMANAGER_EMBEDDING_MODEL'):
        return validate_model(os.environ['CODEMANAGER_EMBEDDING_MODEL']), 'environment'
    data = read_config(repo)
    if 'model' in data.get('embedding', {}):
        return data['embedding']['model'], CONFIG_FILE
    return DEFAULT_MODEL, 'default'


def model_settings(repo, *, save=None, reset=False):
    root = Path(repo).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Repository must be a directory')
    data = read_config(root)
    path = root / CONFIG_FILE
    if save is not None:
        data.setdefault('embedding', {})['model'] = validate_model(save)
    elif reset:
        data.get('embedding', {}).pop('model', None)
        if not data.get('embedding'):
            data.pop('embedding', None)
    if save is not None or reset:
        if data:
            from .snapshot import atomic_write
            atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode())
        else:
            path.unlink(missing_ok=True)
    effective, source = selected_model(root)
    return {'model': effective, 'source': source,
            'saved_model': data.get('embedding', {}).get('model'),
            'config_file': str(path), 'suggested_models': PRESETS,
            'note': 'Model must be supported by your installed FastEmbed version. Selection does not download weights; embed or search initializes the model.'}
