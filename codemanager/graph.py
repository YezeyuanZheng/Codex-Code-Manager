"""SQLite code graph with conservative Python name resolution."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

SKIP = {'.git', '.codemanager', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', '.next', 'vendor'}
EXTENSIONS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.c', '.h', '.cpp', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.sql', '.md', '.toml', '.yaml', '.yml', '.json', '.vue', '.svelte'}


def terms(text):
    return ' '.join(re.findall(r'[A-Za-z0-9_]+', re.sub(r'([a-z])([A-Z])', r'\1 \2', text)))


class Graph:
    def __init__(self, repo):
        self.repo = Path(repo).resolve(strict=True)
        if not self.repo.is_dir():
            raise ValueError('Repository must be a directory')
        folder = self.repo / '.codemanager'
        folder.mkdir(exist_ok=True)
        self.db = sqlite3.connect(folder / 'graph.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, digest TEXT);
            CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, path TEXT, name TEXT,
                kind TEXT, start INTEGER, end INTEGER, body TEXT);
            CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);
            CREATE TABLE IF NOT EXISTS edges(source TEXT, target TEXT, kind TEXT,
                PRIMARY KEY(source,target,kind));
            CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
            CREATE TABLE IF NOT EXISTS refs(source TEXT, name TEXT, kind TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, name, path, body);
        ''')
        try:
            self.load_bundled_snapshot()
        except Exception:
            self.db.close()
            raise

    def load_bundled_snapshot(self):
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

    def export(self):
        from .snapshot import export_snapshot
        return export_snapshot(self)

    def close(self):
        self.db.close()

    def paths(self):
        # Git supplies tracked and untracked files while respecting gitignore.
        result = subprocess.run(['git', '-C', str(self.repo), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True)
        if result.returncode == 0:
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
        self.db.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?,?)', (ident, path, name, kind, start, end, body))
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

    def index(self):
        changed = 0
        warnings = []
        seen = set()
        with self.db:
            for path, full in self.paths():
                try:
                    raw = full.read_bytes()
                    if b'\0' in raw:
                        continue
                    text = raw.decode('utf-8')
                except (OSError, UnicodeError):
                    warnings.append(path)
                    continue
                seen.add(path)
                digest = hashlib.sha256(raw).hexdigest()
                old = self.db.execute('SELECT digest FROM files WHERE path=?', (path,)).fetchone()
                if old and old['digest'] == digest:
                    continue
                self.remove(path)
                self.parse(path, text)
                self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (path, digest))
                changed += 1
            deleted = [r[0] for r in self.db.execute('SELECT path FROM files') if r[0] not in seen]
            for path in deleted:
                self.remove(path)
            # Rebuild resolution after every update, including newly added targets.
            self.db.execute("DELETE FROM edges WHERE kind != 'contains'")
            for ref in self.db.execute('SELECT refs.*, nodes.path FROM refs JOIN nodes ON nodes.id=refs.source').fetchall():
                if ref['kind'] == 'calls':
                    # Only a unique same-file top-level definition is a candidate.
                    targets = self.db.execute("SELECT id FROM nodes WHERE path=? AND name=? AND kind IN ('function','class')", (ref['path'], ref['name'])).fetchall()
                else:
                    module = ref['name'].replace('.', '/')
                    targets = self.db.execute("SELECT id FROM nodes WHERE kind='file' AND (path=? OR path=?)", (module+'.py', module+'/__init__.py')).fetchall()
                if len(targets) == 1:
                    self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)', (ref['source'], targets[0]['id'], ref['kind']+'_candidate'))
        return {**self.stats(), 'changed_files': changed, 'deleted_files': len(deleted), 'unreadable_files': warnings}

    def remove(self, path):
        ids = [r[0] for r in self.db.execute('SELECT id FROM nodes WHERE path=?', (path,))]
        for ident in ids:
            self.db.execute('DELETE FROM search WHERE id=?', (ident,))
            self.db.execute('DELETE FROM refs WHERE source=?', (ident,))
            self.db.execute('DELETE FROM edges WHERE source=? OR target=?', (ident, ident))
        self.db.execute('DELETE FROM nodes WHERE path=?', (path,))
        self.db.execute('DELETE FROM files WHERE path=?', (path,))

    def stats(self):
        return {table: self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('files', 'nodes', 'edges')}

    def search(self, query, budget=6000, limit=8):
        if not 256 <= budget <= 100000 or not 1 <= limit <= 50:
            raise ValueError('budget must be 256..100000 bytes; limit must be 1..50')
        tokens = terms(query).split()[:40]
        if not tokens:
            raise ValueError('Query must contain letters or numbers')
        match = ' OR '.join('"'+t+'"' for t in tokens)
        hits = self.db.execute('''SELECT nodes.*, bm25(search,0,8,3,1) AS rank FROM search
            JOIN nodes ON search.id=nodes.id WHERE search MATCH ? ORDER BY rank LIMIT ?''', (match, limit)).fetchall()
        candidates = [(dict(r), 'search') for r in hits]
        known = {r['id'] for r in hits}
        for hit in hits:
            rows = self.db.execute('''SELECT nodes.* FROM edges JOIN nodes ON nodes.id =
                CASE WHEN edges.source=? THEN edges.target ELSE edges.source END
                WHERE (edges.source=? OR edges.target=?) AND edges.kind!='contains' LIMIT 4''', (hit['id'], hit['id'], hit['id'])).fetchall()
            for row in rows:
                if row['id'] not in known:
                    known.add(row['id'])
                    candidates.append((dict(row), 'graph_neighbor'))
        # Budget includes serialized metadata and escaping, not just source bodies.
        output = {'results': [], 'budget_bytes': budget, 'truncated': False}
        for row, reason in candidates:
            item = {k: row[k] for k in ('id', 'path', 'name', 'kind', 'start', 'end', 'body')}
            item['reason'] = reason
            item['truncated'] = False
            output['results'].append(item)
            if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                continue
            item['truncated'] = True
            output['truncated'] = True
            body = item['body']
            lo, hi = 0, len(body)
            while lo < hi:
                mid = (lo+hi+1)//2
                item['body'] = body[:mid]
                if len(json.dumps(output, ensure_ascii=False).encode()) <= budget:
                    lo = mid
                else:
                    hi = mid-1
            item['body'] = body[:lo]
            if not lo:
                output['results'].pop()
            break
        return output
