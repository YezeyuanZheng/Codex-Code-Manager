# Indexing, validation, and development

## Incremental graph construction

Normal `index`, `export`, MCP polling, and pre-commit validation now reuse the
existing graph. A first index still needs to parse the repository; ordinary
updates process changes rather than reconstructing every definition.

- File fingerprints include device, inode, size, modification time, and change
  time. Unchanged files are not opened or rehashed. Changed fingerprints are
  hashed; only changed content is parsed. Restoring just the modification time
  does not hide an ordinary edit.
- The cache records its last Git HEAD. When HEAD changes, Git's revision diff
  forces verification of the affected paths; metadata checks also cover local
  edits and new files. Missing revision bases fall back to hash verification.
  Renames are handled as a deletion and an addition. No Git HEAD is embedded in
  the snapshot, avoiding a circular dependency on the commit containing it.
- Changed files replace their own nodes and containment edges. Candidate call
  edges are re-resolved in those files; import edges are updated in importers of
  changed modules. Unrelated references are left intact. This uses the current
  resolver's supported same-file calls and exact repository-root imports.
- Snapshot format 4 has up to 256 stable path-hash partitions, each split into
  shards no larger than 1 MiB. Only dirty partitions are serialized. Unchanged
  shards are reused, obsolete shards are removed, and source hashes remain in
  the snapshot. Format 1 snapshots still load; the next export migrates them.
- Staged validation reuses a transactionally copied graph cache, excludes large
  derived vector/full-text indexes, and streams raw Git blobs one file at a time.
  It respects partial staging and never changes the user's Git index. It still
  reads staged blobs and copies graph rows, so validation has repository-size
  I/O even when it parses zero files.

Commands report `changed_files`, `hashed_files`, `unchanged_files`,
`resolved_references`, `base_revision`, and `revision`. Exports also report
`rebuilt_partitions` and `reused_partitions`. Warm checks report `reused_cache`
and `parsed_files`. An unchanged warm export should hash/parse zero sources and
rebuild zero partitions.

For an explicit full rebuild or independent audit:

```bash
python -m codemanager index --repo /path/to/checkout --full
python -m codemanager export --repo /path/to/checkout --full
python -m codemanager check --repo /path/to/checkout --staged --full
```

Use `--full` after changing parser semantics or repairing a suspect local cache.
Incremental commit checks rely on the local graph cache; full checks regenerate
independently. Source discovery/metadata checks, cold snapshot loading, and
validation copies still grow with project size. A dirty partition may contain
unchanged files too, and an edited file is parsed as a whole. These improvements
do not replace the separate linear vector search with an approximate index.

## Automatic updates and commit checks

The MCP server refreshes and exports the graph every second while running, even
when Codex is editing without calling a search tool. Each tool call refreshes
before returning results. All work stays on the server's main thread; protocol
input is queued separately. Diagnostics go to stderr, keeping stdout valid MCP.
Change the polling interval with `serve --interval 2` (0.1 to 60 seconds).

For edits outside a running MCP session, keep a watcher open:

```bash
python -m codemanager watch --repo /path/to/checkout --interval 1
```

The watcher refreshes SQLite and the portable snapshot on each poll. It detects
added, changed, and deleted eligible files. Unchanged exports do not rewrite the
manifest or shards. Stop it with Ctrl+C. No watcher continues after its process
exits. Polling records saved filesystem content, rather than unsaved editor buffers.
`index`, `export`, and CLI searches also publish the refreshed snapshot.

Install the pre-commit check once for each clone:

```bash
python -m codemanager install-hook --repo /path/to/checkout
```

The hook first enforces the independent [file length policy](file-limits.md),
then blocks a commit unless the **staged graph matches the staged source**
and **missing/stale embeddings have been regenerated successfully**. When optional
review is enabled in the staged configuration, the hook also requires a passing
review of the exact staged tree and its current parent commit. The hook invokes
`python -m codemanager commit-check --repo .`. Existing custom hooks can invoke
that command without being replaced.
It copies cached graph rows to temporary storage, updates them against raw staged
Git blobs, then compares the deterministic manifest and referenced shards. Changed
files are parsed; unchanged graph rows are reused. Without a cache, it bootstraps
from source once. `check --staged --full` performs an independent full audit. It does not run the
repository's code, change your working files, or stage anything. After graph
validation, it embeds the staged source using the staged model configuration
(environment overrides still apply), reuses unchanged vectors, removes obsolete
vectors, and writes the result to the persistent local SQLite vector cache.
Dependency, model-load, or inference failures block the commit. A fresh graph is
required even for the first commit. To fix a stale snapshot:

```bash
python -m codemanager export --repo /path/to/checkout
git -C /path/to/checkout add .codemanager/index/
# Stage exactly the matching source changes, then retry git commit.
```

For partial commits, a working-tree export may include unstaged edits. If so, the
hook correctly rejects it. Export from a separate worktree containing exactly the
intended commit, or finish staging the intended source edits before exporting.
Neither this tool nor the hook automatically stages source files. Embedding input
also comes from the Git index, so unstaged edits are not embedded as committed code.
The working-tree graph remains untouched; when staged and working code differ,
subsequent searches reconcile vectors against the working tree before using them.
The hook checks that staged entries did not change while inference was running.

Model weights and vectors remain local, outside Git. The hook can require a model
download on first use. Install CodeManager and warm the selected model in the same
Python environment used to install the hook. Rerun `install-hook` to upgrade a
previous CodeManager graph-only hook, or to use a different Python interpreter.
The embedding step is mandatory in the installed hook even if search mode is BM25.
Failed embedding transactions preserve the previous vector rows.

Manual checks (also useful in CI):

```bash
python -m codemanager check --repo /path/to/checkout
python -m codemanager check --repo /path/to/checkout --staged
# Same graph validation plus mandatory staged-vector refresh as the hook:
python -m codemanager check --repo /path/to/checkout --staged --embeddings
```

`--embeddings` requires `--staged`. It accepts `--model MODEL` for an explicit
model override. Without `--embeddings`, manual `check` is graph-only and does not
satisfy the installed hook's embedding requirement.

Checks use temporary storage and leave the repository graph rows and snapshot
untouched. The shared lock file may be opened for coordination. Embedding vectors
and full-text indexes are excluded from the validation copy; `--embeddings`
separately refreshes the actual local vector cache after validation succeeds. Working-tree checks
include nonignored untracked code; staged checks use only staged
files, including deletions and partial edits. Staged checks read raw blobs without
Git smudge filters, so repositories that transform line endings or file contents
must generate a snapshot matching those canonical staged bytes.

The installer preserves an existing pre-commit hook or `core.hooksPath` and reports
how to add `check --staged --embeddings` to that workflow. Its launcher points to the current
Python interpreter and CodeManager installation; reinstall it if either moves.
Git does not activate local hooks on clone, and `--no-verify` can bypass them.
Use a required CI check if you need enforcement for every contributor. See
[Git's hook documentation](https://git-scm.com/docs/githooks).

## What is indexed

- Python: AST-derived functions, classes, nesting, module chunks, and candidate
  calls/imports. Direct bare-name calls resolve only to a unique same-file
  top-level definition. Imports resolve only to exact repository-root module paths.
- Other supported source, config, and documentation formats: 60-line text chunks
  with file containment edges. These formats do not yet have semantic parsing.
- Search: FTS5 BM25 plus local embedding cosine similarity, merged with reciprocal
  rank fusion, followed by one-hop candidate call/import expansion. CamelCase is
  split for keyword search.
- Discovery: tracked and untracked nonignored Git files. Hidden paths, common
  dependency/build directories, symlinks, binary/non-UTF-8 files, unsupported
  extensions, and files larger than 1 MB are skipped. This is not a secret scanner;
  review what is present in a repository before exposing it to an agent.

The `budget` limits the UTF-8 bytes of the serialized search result, including
snippet metadata. It is **not an exact model token count**. A truncated result
keeps the original node's full line range and marks `truncated`; its body can be
only a prefix. MCP adds a small protocol envelope around the result.

## Limits and next steps

This is a working first version, not a complete semantic graph for every language.
Python edges are static candidates: aliases, cross-file calls, attribute calls,
inheritance, dynamic dispatch, and scope shadowing are not fully resolved. Other
languages receive text chunks for both keyword and embedding retrieval, without
semantic language parsing. The index represents the last refresh, not a live filesystem
snapshot. Polling still inventories paths and file metadata; only changed
fingerprints trigger source reads and hashes. There is no remote GitHub synchronization; local Git commands publish changes.

Context reduction depends on the task and the agent following retrieval guidance;
it has not yet been benchmarked. A useful next milestone is an evaluation set of
real repository questions measuring recall, returned bytes, latency, and answer
quality against ordinary file search. Then add language parsers and stronger
resolution based on the repositories that matter most.

## Development

```bash
python -m unittest discover -s tests -v
```

Tests cover incremental updates, removed files, candidate edges, budget limits,
ignore handling, symlinks, syntax-error fallback, and a subprocess MCP handshake
through tool execution. Snapshot tests also exercise a real Git commit/clone,
loading without reparsing, deterministic output, source changes, manifest updates,
and corruption rejection. Automation tests exercise actual blocked/allowed commits,
partial staging, hook preservation, polling, and idle MCP refresh.

Hybrid tests use controlled vectors to validate fusion, no-keyword-overlap hits,
incremental cache behavior, model changes, long-symbol passages, output budgets,
provider errors, and MCP output. They do not prove a real model's relevance quality.
Run the opt-in real-model smoke test after installing and downloading the model:

```bash
CODEMANAGER_REAL_EMBEDDINGS=1 .venv/bin/python -m unittest discover -s tests -p test_hybrid.py -v
```

Incremental tests compare snapshots byte-for-byte with independent full builds
following edits, additions, deletions, renames, and Git revision changes. A
303-file fixture verifies that a single edit hashes/parses one file and rebuilds
one partition. Tests also ensure unchanged exports perform no source reads or
partition serialization, warm checks parse zero files, legacy snapshots migrate,
and validation copies exclude derived indexes. This fixture is a correctness
check, not a production-scale performance benchmark.

Commit-embedding tests verify persistent staged vectors, unchanged-vector reuse,
partial staging, staged model selection, inference-failure rollback, rejection of
stale graph snapshots before inference, and detection of staging changes during
inference. Subprocess hook tests use controlled embedding runtimes to verify both
successful commits and blocked commits without downloading a model.
