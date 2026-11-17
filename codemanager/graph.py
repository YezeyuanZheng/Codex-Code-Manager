"""SQLite code graph with conservative Python name resolution."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from .locking import snapshot_lock
from .search import GraphSearch


def bucket_for(path):
    return hashlib.sha256(path.encode()).hexdigest()[:2]


def file_signature(path):
    stat = path.stat()
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])

SKIP = {'.git', '.codemanager', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', '.next', 'vendor'}
EXTENSIONS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.c', '.h', '.cpp', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.sql', '.md', '.toml', '.yaml', '.yml', '.json', '.vue', '.svelte'}


def terms(text):
    return ' '.join(re.findall(r'[A-Za-z0-9_]+', re.sub(r'([a-z])([A-Z])', r'\1 \2', text)))


class Graph(GraphSearch):
    def __init__(self, repo, *, load_snapshot=True, use_git=True, cache_dir=None, embedder=None, embedding_model=None):
        self.use_git = use_git
        self._embedder = embedder
        self._provider_model = None
        self.embedding_model = embedding_model
        self.repo = Path(repo).resolve(strict=True)
        if not self.repo.is_dir():
            raise ValueError('Repository must be a directory')
        folder = Path(cache_dir) if cache_dir is not None else self.repo / '.codemanager'
        folder.mkdir(parents=True, exist_ok=True)
        self.cache_dir = folder
        self.db = sqlite3.connect(folder / 'graph.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, digest TEXT, bucket TEXT);
            CREATE TABLE IF NOT EXISTS file_state(path TEXT PRIMARY KEY, signature TEXT);
            CREATE TABLE IF NOT EXISTS snapshot_dirty(path TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS snapshot_buckets(bucket TEXT PRIMARY KEY, descriptors TEXT);
            CREATE TABLE IF NOT EXISTS shard_state(name TEXT PRIMARY KEY, signature TEXT);
            CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, path TEXT, name TEXT,
                kind TEXT, start INTEGER, end INTEGER, body TEXT);
            CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);
            CREATE INDEX IF NOT EXISTS nodes_name ON nodes(path,name,kind);
            CREATE TABLE IF NOT EXISTS edges(source TEXT, target TEXT, kind TEXT,
                PRIMARY KEY(source,target,kind));
            CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
            CREATE TABLE IF NOT EXISTS embeddings(node_id TEXT, chunk INTEGER, model TEXT,
                digest TEXT, vector BLOB, PRIMARY KEY(node_id,chunk,model));
            CREATE TABLE IF NOT EXISTS embedding_reuse(model TEXT,digest TEXT,vector BLOB,
                PRIMARY KEY(model,digest));
            CREATE TABLE IF NOT EXISTS refs(source TEXT, name TEXT, kind TEXT);
            CREATE INDEX IF NOT EXISTS refs_source ON refs(source);
            CREATE INDEX IF NOT EXISTS refs_name ON refs(kind,name);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, name, path, body);
        ''')
        if 'request_mapping' not in {row[1] for row in self.db.execute('PRAGMA table_info(nodes)')}:
            self.db.execute("ALTER TABLE nodes ADD COLUMN request_mapping TEXT NOT NULL DEFAULT '[]'")
        if 'bucket' not in {row[1] for row in self.db.execute('PRAGMA table_info(files)')}:
            self.db.execute('ALTER TABLE files ADD COLUMN bucket TEXT')
        with self.db:
            for row in self.db.execute('SELECT path FROM files WHERE bucket IS NULL').fetchall():
                self.db.execute('UPDATE files SET bucket=? WHERE path=?', (bucket_for(row[0]), row[0]))
        self.db.execute('CREATE INDEX IF NOT EXISTS files_bucket ON files(bucket)')
        from .cache_revisions import install
        install(self.db)
        try:
            if load_snapshot:
                self.load_bundled_snapshot()
        except Exception:
            self.db.close()
            raise

    def load_bundled_snapshot(self):
        with snapshot_lock(self.cache_dir):
            self._load_bundled_snapshot()

    def _load_bundled_snapshot(self):
        manifest = self.repo / '.codemanager' / 'index' / 'manifest.json'
        if not manifest.exists():
            return
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(self.repo):
            raise ValueError('Snapshot manifest must be inside the checkout')
        data = manifest.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        cached = self.db.execute("SELECT value FROM metadata WHERE key='snapshot'").fetchone()
        if not cached or cached[0] != digest:
            from .snapshot import load_snapshot
            load_snapshot(self, data)
        else:
            from .snapshot import reusable_shard
            with self.db:
                for entry in json.loads(data)['shards']:
                    if not reusable_shard(self, manifest.parent, entry):
                        raise ValueError('Snapshot shard checksum mismatch or missing shard; run export --full to repair')

    def export(self, *, full=False):
        from .snapshot import export_snapshot
        return export_snapshot(self, full=full)

    def close(self):
        self.db.close()

    def paths(self):
        # Git supplies tracked and untracked files while respecting gitignore.
        result = subprocess.run(['git', '-C', str(self.repo), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True) if self.use_git else None
        if result is not None and result.returncode == 0:
            paths = (Path(os.fsdecode(p)) for p in result.stdout.split(b'\0') if p)
        else:
            candidates = []
            for root, dirs, files in os.walk(self.repo, followlinks=False):
                dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.')]
                candidates.extend((Path(root) / f).relative_to(self.repo) for f in files)
            paths = iter(candidates)
        for path in sorted(set(paths)):
            full = self.repo / path
            if any(p in SKIP or p.startswith('.') for p in path.parts):
                continue
            if path.suffix.lower() not in EXTENSIONS or full.is_symlink():
                continue
            if not full.resolve().is_relative_to(self.repo):
                continue
            try:
                if full.is_file() and full.stat().st_size <= 1_000_000:
                    yield path.as_posix(), full
            except OSError:
                continue

    def add(self, path, name, kind, start, end, lines):
        ident = f'{path}:{start}:{name}'
        body = '\n'.join(lines[start-1:end])
        self.db.execute('INSERT INTO nodes (id,path,name,kind,start,end,body) VALUES (?,?,?,?,?,?,?)', (ident, path, name, kind, start, end, body))
        self.db.execute('INSERT INTO search VALUES (?,?,?,?)', (ident, terms(name), terms(path), terms(body)))
        return ident

    def parse(self, path, text):
        lines = text.splitlines()
        file_id = self.add(path, path, 'file', 1, min(40, len(lines)), lines)
        tree = None
        if path.endswith('.py'):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                pass
        if tree is None:
            for offset in range(0, len(lines), 60):
                ident = self.add(path, f'chunk_{offset+1}', 'chunk', offset+1, min(offset+60, len(lines)), lines)
                self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)', (file_id, ident, 'contains'))
            return

        def visit(node, parent, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = '.'.join(prefix + [child.name])
                    start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                    ident = self.add(path, name, 'class' if isinstance(child, ast.ClassDef) else 'function', start, child.end_lineno, lines)
                    self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)', (parent, ident, 'contains'))
                    visit(child, ident, prefix + [child.name])
                else:
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                        self.db.execute('INSERT INTO refs VALUES (?,?,?)', (parent, child.func.id, 'calls'))
                    if isinstance(child, ast.Import):
                        for alias in child.names:
                            self.db.execute('INSERT INTO refs VALUES (?,?,?)', (parent, alias.name, 'imports'))
                    if isinstance(child, ast.ImportFrom) and child.module:
                        self.db.execute('INSERT INTO refs VALUES (?,?,?)', (parent, child.module, 'imports'))
                    visit(child, parent, prefix)
        visit(tree, file_id, [])
        # Cover module-level constants, imports, and code outside definitions.
        covered = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                covered.update(range(min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno+1))
        for offset in range(0, len(lines), 60):
            if any(n not in covered for n in range(offset+1, min(offset+60, len(lines))+1)):
                ident = self.add(path, f'module_{offset+1}', 'chunk', offset+1, min(offset+60, len(lines)), lines)
                self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)', (file_id, ident, 'contains'))

    def revision_changes(self):
        previous = self.db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        previous = previous[0] if previous else None
        if not self.use_git:
            return previous, None, set()
        head = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
        current = head.stdout.decode().strip() if head.returncode == 0 else None
        changed = set()
        if previous and current and previous != current:
            diff = subprocess.run(['git', '-C', str(self.repo), 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '--no-renames', '-z', previous, current, '--'], capture_output=True)
            # A pruned/shallow base means all source fingerprints need verification.
            changed = {os.fsdecode(path) for path in diff.stdout.split(b'\0') if path} if diff.returncode == 0 else None
        return previous, current, changed

    def mark_dirty(self, paths):
        self.db.executemany('INSERT OR IGNORE INTO snapshot_dirty VALUES (?)', ((path,) for path in paths))

    def resolve_changes(self, paths):
        """Same-file calls plus importers of changed modules are the affected set."""
        sources = set()
        for path in paths:
            sources.update(row[0] for row in self.db.execute('SELECT id FROM nodes WHERE path=?', (path,)))
            if path.endswith('.py'):
                module = path[:-12] if path.endswith('/__init__.py') else path[:-3]
                module = module.replace('/', '.')
                sources.update(row[0] for row in self.db.execute("SELECT source FROM refs WHERE kind='imports' AND name=?", (module,)))
        resolved = 0
        for source in sorted(sources):
            row = self.db.execute('SELECT path FROM nodes WHERE id=?', (source,)).fetchone()
            if not row:
                continue
            self.mark_dirty([row['path']])
            self.db.execute("DELETE FROM edges WHERE source=? AND kind!='contains'", (source,))
            for ref in self.db.execute('SELECT * FROM refs WHERE source=?', (source,)).fetchall():
                self.resolve_reference(ref, row['path'])
                resolved += 1
        return resolved

    def resolve_reference(self, ref, path):
        if ref['kind'] == 'calls':
            targets = self.db.execute("SELECT id FROM nodes WHERE path=? AND name=? AND kind IN ('function','class')", (path, ref['name'])).fetchall()
        else:
            module = ref['name'].replace('.', '/')
            targets = self.db.execute("SELECT id FROM nodes WHERE kind='file' AND (path=? OR path=?)", (module+'.py', module+'/__init__.py')).fetchall()
        if len(targets) == 1:
            self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)', (ref['source'], targets[0]['id'], ref['kind']+'_candidate'))

    def index(self, *, full=False):
        from .requests import bundled_mappings, validate_mappings, restore_mappings
        mappings = bundled_mappings(self) if getattr(self, '_read_mapping_snapshot', False) else {}
        self._read_mapping_snapshot = False
        for row in self.db.execute('SELECT path,name,kind,request_mapping FROM nodes'):
            if row['request_mapping'] != '[]':
                mappings[(row['path'], row['name'], row['kind'])] = validate_mappings(row['request_mapping'])
        changed, warnings, seen, hashed = set(), [], set(), 0
        previous, revision, revision_paths = self.revision_changes()
        cached = {row['path']: row['digest'] for row in self.db.execute('SELECT path,digest FROM files')}
        states = {row['path']: row['signature'] for row in self.db.execute('SELECT * FROM file_state')}
        with self.db:
            for path, source in self.paths():
                try:
                    signature = file_signature(source)
                    if not full and path in cached and states.get(path) == signature and revision_paths is not None and path not in revision_paths:
                        seen.add(path)
                        continue
                    raw = source.read_bytes()
                    hashed += 1
                    if b'\0' in raw:
                        continue
                    text = raw.decode('utf-8')
                    # If a writer raced this read, do not cache a fingerprint for it.
                    stable = file_signature(source) == signature
                except (OSError, UnicodeError):
                    warnings.append(path)
                    continue
                seen.add(path)
                digest = hashlib.sha256(raw).hexdigest()
                if full or cached.get(path) != digest:
                    self.remove(path, keep_embeddings=True)
                    self.parse(path, text)
                    self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (path, digest, bucket_for(path)))
                    changed.add(path)
                if stable:
                    self.db.execute('INSERT OR REPLACE INTO file_state VALUES (?,?)', (path, signature))
                else:
                    self.db.execute('DELETE FROM file_state WHERE path=?', (path,))
            deleted = set(cached) - seen
            for path in deleted:
                self.remove(path)
            affected = changed | deleted
            resolved = self.resolve_changes(affected) if affected else 0
            self.mark_dirty(affected)
            if affected:
                self.db.execute('''INSERT OR IGNORE INTO embedding_reuse
                    SELECT model,digest,vector FROM embeddings WHERE node_id NOT IN (SELECT id FROM nodes)''')
                self.db.execute('DELETE FROM embedding_reuse WHERE rowid NOT IN (SELECT rowid FROM embedding_reuse ORDER BY rowid DESC LIMIT 10000)')
                self.db.execute('DELETE FROM embeddings WHERE node_id NOT IN (SELECT id FROM nodes)')
            if revision and revision != previous:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)", (revision,))
        with self.db:
            restore_mappings(self, mappings)
            from .review import sync_scope
            sync_scope(self)
        return {**self.stats(), 'changed_files': len(changed), 'deleted_files': len(deleted),
                'hashed_files': hashed, 'unchanged_files': len(seen-changed),
                'resolved_references': resolved, 'base_revision': previous, 'revision': revision,
                'unreadable_files': warnings}

    def remove(self, path, *, keep_embeddings=False):
        ids = [r[0] for r in self.db.execute('SELECT id FROM nodes WHERE path=?', (path,))]
        for ident in ids:
            if not keep_embeddings:
                self.db.execute('DELETE FROM embeddings WHERE node_id=?', (ident,))
            self.db.execute('DELETE FROM search WHERE id=?', (ident,))
            self.db.execute('DELETE FROM refs WHERE source=?', (ident,))
            self.db.execute('DELETE FROM edges WHERE source=? OR target=?', (ident, ident))
        self.db.execute('DELETE FROM nodes WHERE path=?', (path,))
        self.db.execute('DELETE FROM files WHERE path=?', (path,))
        self.db.execute('DELETE FROM file_state WHERE path=?', (path,))

    def stats(self):
        return {table: self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('files', 'nodes', 'edges')}
