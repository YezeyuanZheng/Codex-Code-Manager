"""Current file requirements survive source edits; Git owns their earlier versions."""
import json
from pathlib import PurePosixPath


def text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f'{label} must be nonempty text of at most 4000 characters')
    return value.strip()


def source_path(value):
    if not isinstance(value, str) or not value or '\0' in value or '\\' in value:
        raise ValueError('path must be repository-relative source')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or path.as_posix() != value or path.parts[0] in ('.git', '.codemanager'):
        raise ValueError('path must be repository-relative source, outside .git and .codemanager')
    return value


def validate_purposes(value):
    if not isinstance(value, dict):
        raise ValueError('File purposes must be an object')
    for path, purpose in value.items():
        source_path(path)
        if not isinstance(purpose, dict) or set(purpose) != {'purpose', 'requirements', 'request'}:
            raise ValueError('Invalid file purpose')
        text(purpose['purpose'], 'purpose')
        text(purpose['request'], 'request')
        requirements = purpose['requirements']
        if not isinstance(requirements, list) or not requirements or len(requirements) > 100:
            raise ValueError('File purpose needs 1..100 current requirements')
        seen = set()
        for entry in requirements:
            if not isinstance(entry, dict) or set(entry) != {'text', 'request'}:
                raise ValueError('Invalid file requirement')
            requirement = text(entry['text'], 'requirement')
            text(entry['request'], 'requirement request')
            if requirement in seen:
                raise ValueError('Duplicate file requirement')
            seen.add(requirement)
    return value


def purposes(graph):
    row = graph.db.execute("SELECT value FROM metadata WHERE key='file_purposes'").fetchone()
    return validate_purposes(json.loads(row[0])) if row else {}


def store_purposes(graph, value):
    validate_purposes(value)
    graph.db.execute("INSERT OR REPLACE INTO metadata VALUES ('file_purposes',?)", (json.dumps(value, sort_keys=True, ensure_ascii=False),))


def get_file_purpose(graph, path):
    source_path(path)
    return {'path': path, 'file_purpose': purposes(graph).get(path)}


def set_file_purpose(graph, path, purpose, requirements, request,
                     remove_requirements=None, removal_request=None):
    """Merge current requirements; removal needs an explicit user change request."""
    source_path(path)
    purpose = text(purpose, 'purpose')
    request = text(request, 'request')
    if not isinstance(requirements, list) or len(requirements) > 100:
        raise ValueError('requirements must be a list of at most 100 requirement strings')
    additions = [text(item, 'requirement') for item in requirements]
    removals = remove_requirements if remove_requirements is not None else []
    if not isinstance(removals, list):
        raise ValueError('remove_requirements must be a list')
    removals = [text(item, 'removed requirement') for item in removals]
    if removals:
        removal_request = text(removal_request, 'removal_request explaining the user-authorized requirement change')
        if removal_request != request:
            raise ValueError('removal_request must match the actual request supplied for this purpose update')
    from .review import settings, begin_task
    if not settings(graph.repo)['enabled']:
        raise ValueError('File purposes require optional review: run review-config --enable first')
    graph.export()
    current = purposes(graph)
    previous = current.get(path, {}).get('requirements', [])
    known = {item['text'] for item in previous}
    if set(removals) - known:
        raise ValueError('Cannot remove an unknown requirement')
    if set(removals) & set(additions):
        raise ValueError('A requirement cannot be added and removed together')
    merged = [item for item in previous if item['text'] not in removals]
    for item in additions:
        if item not in {entry['text'] for entry in merged}:
            merged.append({'text': item, 'request': request})
    entry = {'purpose': purpose, 'requirements': merged, 'request': request}
    current[path] = entry
    validate_purposes(current)
    begin_task(graph, request)
    with graph.db:
        store_purposes(graph, current)
    graph.export()
    return {'path': path, 'file_purpose': entry}
