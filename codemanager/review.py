"""Opt-in task review, bound to an immutable Git tree without making a commit."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from .review_config import settings, configure
from .review_runner import load_report, run_reviewer
from . import file_limits
from .snapshot import atomic_write


def git(repo, *args, input=None, env=None):
    result = subprocess.run(['git', '-C', str(repo), *args], input=input,
                            capture_output=True, env=env)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace').strip())
    return result.stdout


def head(repo):
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
    return result.stdout.decode().strip() if result.returncode == 0 else ''


def validate_context(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'base', 'requests', 'changes'} or
        not isinstance(value['base'], str) or
        (value['base'] and (len(value['base']) not in (40, 64) or any(c not in '0123456789abcdef' for c in value['base']))) or
        not isinstance(value['requests'], list) or not value['requests'] or
        any(not isinstance(r, str) or not r.strip() or len(r) > 4000 for r in value['requests']) or
        not isinstance(value['changes'], list)):
        raise ValueError('Invalid graph review context')
    for change in value['changes']:
        if (not isinstance(change, dict) or set(change) != {'path', 'request', 'fulfills', 'before', 'after'} or
            any(not isinstance(change[k], str) or not change[k].strip() for k in ('path', 'request', 'fulfills'))):
            raise ValueError('Invalid graph change mapping')
        path = PurePosixPath(change['path'])
        if path.is_absolute() or '..' in path.parts or path.as_posix() != change['path']:
            raise ValueError('Change mapping path must be repository-relative')
        for key in ('before', 'after'):
            digest = change[key]
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise ValueError('Invalid change mapping digest')
    return value


def context(graph):
    row = graph.db.execute("SELECT value FROM metadata WHERE key='review_context'").fetchone()
    return validate_context(json.loads(row[0])) if row else None


def set_context(graph, value):
    validate_context(value)
    graph.db.execute("INSERT OR REPLACE INTO metadata VALUES ('review_context',?)", (json.dumps(value, sort_keys=True),))


def clear_mappings(graph):
    from .requests import update_mapping
    for node in graph.db.execute("SELECT * FROM nodes WHERE request_mapping!='[]'").fetchall():
        update_mapping(graph, node, [])


def sync_scope(graph):
    """Keep mapping collection optional; historical intent stays until the next task."""
    enabled = settings(graph.repo)['enabled']
    if not enabled:
        clear_mappings(graph)
        set_context(graph, None)


def begin_task(graph, request):
    if not settings(graph.repo)['enabled']:
        return {'enabled': False, 'status': 'disabled'}
    if not isinstance(request, str) or not request.strip() or len(request) > 4000:
        raise ValueError('request must be nonempty text of at most 4000 characters')
    graph.export()
    value = context(graph)
    if value is None or value['base'] != head(graph.repo):
        with graph.db:
            clear_mappings(graph)
        value = {'base': head(graph.repo), 'requests': [], 'changes': []}
    if request.strip() not in value['requests']:
        value['requests'].append(request.strip())
    with graph.db:
        set_context(graph, value)
    graph.export()
    return {'enabled': True, 'status': 'started', 'base': value['base'], 'request': request.strip()}


def require_mapping(graph, request):
    if not settings(graph.repo)['enabled']:
        raise ValueError('Request mappings require optional review: run review-config --enable first')
    begin_task(graph, request)


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def file_versions(graph, path):
    relative = PurePosixPath(path)
    if relative.is_absolute() or '..' in relative.parts or relative.as_posix() != path or path.startswith('.codemanager/'):
        raise ValueError('Mapping path must be repository-relative source, outside .codemanager/')
    base = context(graph)['base']
    before = None
    if base:
        result = subprocess.run(['git', '-C', str(graph.repo), 'show', f'{base}:{path}'], capture_output=True)
        if result.returncode == 0:
            before = result.stdout
    source = graph.repo / path
    if source.is_symlink() or not source.resolve().is_relative_to(graph.repo):
        raise ValueError('Map regular source files only')
    after = source.read_bytes() if source.is_file() else None
    if before == after:
        raise ValueError('Mapping path has no changes relative to the review base')
    return digest(before), digest(after)


def map_change(graph, path, request, fulfills):
    if not isinstance(fulfills, str) or not fulfills.strip() or len(fulfills) > 4000:
        raise ValueError('fulfills must be nonempty text of at most 4000 characters')
    require_mapping(graph, request)
    before, after = file_versions(graph, path)
    value = context(graph)
    entry = dict(path=path, request=request.strip(), fulfills=fulfills.strip(), before=before, after=after)
    value['changes'] = [c for c in value['changes'] if (c['path'], c['request']) != (path, request.strip())]
    value['changes'].append(entry)
    value['changes'].sort(key=lambda c: (c['path'], c['request']))
    with graph.db:
        set_context(graph, value)
    graph.export()
    return entry


def tree_id(repo, staged=False):
    if staged:
        return git(repo, 'write-tree').decode().strip()
    # Review saved changes without touching the user's staging or committing anything.
    with tempfile.TemporaryDirectory(prefix='codemanager-index-') as folder:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(folder) / 'index'))
        base = head(repo)
        git(repo, 'read-tree', base if base else '--empty', env=env)
        git(repo, 'add', '-A', '--', '.', ':!.codemanager', env=env)
        git(repo, 'add', '-f', '-A', '--', '.codemanager/index', env=env)
        return git(repo, 'write-tree', env=env).decode().strip()


def policy_for_tree(repo, tree):
    result = subprocess.run(['git', '-C', str(repo), 'show', f'{tree}:codemanager.json'], capture_output=True)
    data = json.loads(result.stdout) if result.returncode == 0 else {}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path / 'codemanager.json').write_text(json.dumps(data), encoding='utf-8')
        return {**settings(path), 'file_limits': file_limits.settings(path)}


def review_key(base, tree, policy):
    return hashlib.sha256(json.dumps([base, tree, policy], sort_keys=True).encode()).hexdigest()


def materialize_tree(repo, tree, destination):
    """Read raw Git blobs, without archive export-ignore/export-subst transformations."""
    for record in git(repo, 'ls-tree', '-rz', '--full-tree', tree).split(b'\0'):
        if not record:
            continue
        metadata, raw_path = record.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        path = destination / os.fsdecode(raw_path)
        if kind != b'blob':
            raise ValueError('Review of submodule changes is not supported')
        path.parent.mkdir(parents=True, exist_ok=True)
        data = git(repo, 'cat-file', 'blob', oid.decode())
        if mode == b'120000':
            target = os.fsdecode(data)
            if not (path.parent / target).resolve().is_relative_to(destination):
                raise ValueError('Review checkout contains an external symlink')
            path.symlink_to(target)
        else:
            path.write_bytes(data)
            path.chmod(0o755 if mode == b'100755' else 0o644)


def checkout_fingerprint(root):
    records = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted(dirs+files):
            path = parent / name
            if path.is_symlink():
                value = ['symlink', os.readlink(path)]
            elif path.is_file():
                value = ['file', bool(path.stat().st_mode & 0o111), digest(path.read_bytes())]
            else:
                continue
            records.append([path.relative_to(root).as_posix(), value])
    return hashlib.sha256(json.dumps(sorted(records)).encode()).hexdigest()


def review(repo, *, staged=False):
    from .automation import check_snapshot
    from .graph import Graph
    if os.environ.get('CODEMANAGER_REVIEWER'):
        raise ValueError('A reviewer cannot recursively request a review')
    repo = Path(repo).resolve(strict=True)
    file_limits.require(file_limits.check(repo, staged=staged))
    if not staged:
        if not settings(repo)['enabled']:
            return {'enabled': False, 'status': 'disabled'}
        graph = Graph(repo)
        try:
            graph.export()
        finally:
            graph.close()
    tree = tree_id(repo, staged)
    policy = policy_for_tree(repo, tree)
    if not policy['enabled']:
        return {'enabled': False, 'status': 'disabled'}
    base = head(repo)
    key = review_key(base, tree, policy)
    destination = repo / '.codemanager' / 'reviews' / (key+'.json')
    destination.unlink(missing_ok=True)
    empty = git(repo, 'hash-object', '-w', '-t', 'tree', '--stdin', input=b'').decode().strip()
    diff = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', base or empty, tree, '--', '.', ':!.codemanager').decode(errors='replace')
    with tempfile.TemporaryDirectory(prefix='codemanager-review-') as name:
        folder = Path(name)
        snapshot = folder / 'checkout'
        snapshot.mkdir()
        materialize_tree(repo, tree, snapshot)
        check_snapshot(snapshot, full=True)
        graph = Graph(snapshot, use_git=False, cache_dir=folder / 'graph')
        try:
            value = context(graph)
            if value is None or value['base'] != base:
                raise ValueError('Missing current change mappings; call begin_task and map the changed code first')
            nodes = [{'id': n['id'], 'path': n['path'], 'name': n['name'],
                      'request_mapping': json.loads(n['request_mapping'])}
                     for n in graph.db.execute("SELECT * FROM nodes WHERE request_mapping!='[]'")]
            if not nodes and not value['changes'] and diff:
                raise ValueError('Changes need request mappings before review')
            from .purposes import purposes
            mapped_paths = {node['path'] for node in nodes} | {change['path'] for change in value['changes']}
            changed_paths = {os.fsdecode(path) for path in git(repo, 'diff', '--name-only', '--no-renames', '-z', base or empty, tree).split(b'\0') if path}
            current_purposes = purposes(graph)
            missing = mapped_paths - current_purposes.keys()
            if missing:
                raise ValueError('Missing current file purpose: '+', '.join(sorted(missing)))
            base_manifest = subprocess.run(['git', '-C', str(repo), 'show', f'{base}:.codemanager/index/manifest.json'], capture_output=True) if base else None
            base_purposes = json.loads(base_manifest.stdout).get('purposes', {}) if base_manifest is not None and base_manifest.returncode == 0 else {}
            purpose_changes = {path for path, entry in current_purposes.items() if base_purposes.get(path) != entry}
            file_purposes = {path: entry for path, entry in current_purposes.items() if path in changed_paths | mapped_paths | purpose_changes}
            file_limits.require(file_limits.check_tree(repo, tree, changed_paths | mapped_paths | purpose_changes, policy['file_limits']))
            for change in value['changes']:
                path = snapshot / change['path']
                after = digest(path.read_bytes()) if path.is_file() else None
                before_result = subprocess.run(['git', '-C', str(repo), 'show', f"{base}:{change['path']}"], capture_output=True) if base else None
                before = digest(before_result.stdout) if before_result is not None and before_result.returncode == 0 else None
                if (before, after) != (change['before'], change['after']):
                    raise ValueError('Stale file change mapping: '+change['path'])
            from .review_evidence import prepare_evidence, diff_preview
            requirements = [requirement['text'] for entry in file_purposes.values() for requirement in entry['requirements']]
            patches = {path: git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', base or empty, tree, '--', path).decode(errors='replace')
                       for path in changed_paths if not path.startswith('.codemanager/')}
            evidence, access, evidence_cache = prepare_evidence(graph, folder, requirements, nodes, patches, paths=sorted(file_purposes))
            preview, diff_truncated = diff_preview(diff)
            payload = dict(base=base, tree=tree, requests=value['requests'], mappings=nodes,
                           changes=value['changes'], file_purposes=file_purposes,
                           file_limits=policy['file_limits'], diff=preview, diff_truncated=diff_truncated,
                           evidence=evidence, evidence_access=access,
                           review_files=sorted(path for path in changed_paths | mapped_paths | purpose_changes if not path.startswith('.codemanager/')))
            evidence_digest = digest(evidence_cache.read_bytes())
            fingerprint = checkout_fingerprint(snapshot)
            report = run_reviewer(snapshot, payload, policy, folder)
            if digest(evidence_cache.read_bytes()) != evidence_digest:
                raise ValueError('Reviewer modified its evidence cache; no approval recorded')
            if checkout_fingerprint(snapshot) != fingerprint:
                raise ValueError('Reviewer modified its checkout; no approval recorded')
        finally:
            graph.close()
    if head(repo) != base or tree_id(repo, staged) != tree:
        raise ValueError('Code, graph, or staging changed during review; review again')
    receipt = dict(base=base, tree=tree, policy=policy, report=report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(destination, (json.dumps(receipt, sort_keys=True)+'\n').encode())
    return {'enabled': True, 'status': 'passed' if report['passed'] else 'failed',
            'base': base, 'tree': tree, **report}


def require_review(repo):
    tree = tree_id(repo, staged=True)
    policy = policy_for_tree(repo, tree)
    if not policy['enabled']:
        return {'required': False}
    base = head(repo)
    path = Path(repo) / '.codemanager' / 'reviews' / (review_key(base, tree, policy)+'.json')
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
        report = load_report(receipt['report'])
        valid = receipt['base'] == base and receipt['tree'] == tree and receipt['policy'] == policy and report['passed']
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError('Commit blocked: this staged code and graph need a passing review. Run complete-task, or review --staged for partial staging, then retry.')
    return {'required': True, 'passed': True, 'tree': tree}


def commit_check(repo):
    from .automation import check_snapshot
    before = tree_id(repo, staged=True)
    base = head(repo)
    lengths = file_limits.require(file_limits.check(repo, staged=True))
    result = check_snapshot(repo, staged=True, embeddings=True)
    result['file_limits'] = lengths
    result['review'] = require_review(repo)
    if tree_id(repo, staged=True) != before or head(repo) != base:
        raise ValueError('Staging changed during commit checks; retry')
    return result
