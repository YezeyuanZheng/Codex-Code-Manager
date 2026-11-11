# CodeManager

A local repository index for coding agents. CodeManager stores code nodes and graph
edges in SQLite, ranks full-text matches, and returns source snippets under an
explicit output budget. No API keys, embeddings, external database, or runtime
Python dependencies are required. Requires Python 3.11+, SQLite with FTS5, and Git
for ignore-aware file discovery.

## Try it

Run from this project directory (installation is optional):

```bash
python -m codemanager index --repo /path/to/checkout
python -m codemanager search --repo /path/to/checkout "authentication login" --budget 6000
python -m codemanager stats --repo /path/to/checkout
```

For a GitHub repository, clone it first with your existing Git authentication:

```bash
git clone https://github.com/OWNER/REPOSITORY.git /path/to/checkout
```

The index is stored at `/path/to/checkout/.codemanager/graph.sqlite3`. Add
the cache ignore rules below to that checkout's `.gitignore`. CLI searches refresh
automatically, and the MCP server polls for changes while running. Unchanged files
are not reparsed; deleted nodes are removed and relationships are resolved again. Files are read as data; repository code is never imported or run.
Without a Git repository, discovery uses a directory walk and built-in exclusions;
that fallback does not interpret `.gitignore`.

## Ship the graph with a Git clone

Export a portable snapshot after changing the code:

```bash
python -m codemanager export --repo /path/to/checkout
```

This refreshes the graph, then writes a deterministic manifest and content-addressed
JSONL shards under `.codemanager/index/`. Each shard is at most 1 MiB. Nodes store
paths, names, kinds, and line ranges; files store SHA-256 hashes. Source bodies and
SQLite full-text indexes are not duplicated in Git. There are no timestamps or
absolute paths, so an unchanged graph produces identical snapshot files.

Use these rules in the target repository's `.gitignore` (replace an existing
`.codemanager/` rule, which would otherwise hide the snapshot too):

```gitignore
.codemanager/*
!.codemanager/index/
```

Commit the snapshot together with its matching source changes:

```bash
git -C /path/to/checkout add .codemanager/index .gitignore
# Stage the matching source changes, then commit and push normally.
```

After `git clone`, starting `search`, `stats`, or the MCP server automatically
loads the bundled graph into a local `.codemanager/graph.sqlite3` cache. It verifies
source hashes, reads snippets from the checkout, and builds the local full-text
index. Unchanged source is not reparsed; new or changed files are parsed and
removed files are dropped. A changed manifest on a later pull reloads the cache.
Subsequent starts reuse the cache; searches refresh automatically after local edits.
No Git LFS, network call, or manual import is needed for loading a bundle.

The CodeManager executable and Codex MCP connection still need to be set up once;
cloning a repository does not install software or register tools automatically.
The snapshot format is versioned and shard checksums are verified before loading.
Corrupt or unsupported snapshots fail explicitly instead of silently replacing
the cache. Regenerate the snapshot at its source to repair it.

A SQLite file can also be committed as a regular Git blob if it is small enough,
but the text snapshot is the supported sharing format here. GitHub warns about
files larger than 50 MiB and blocks files larger than 100 MiB; our 1 MiB shard
limit stays below those thresholds. Total snapshot size and Git history still grow
with the project. See [GitHub's file size documentation](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github).

Each commit contains the graph for its own source version. Export removes obsolete
content-addressed shards from the working tree; Git history preserves the older
snapshots alongside their corresponding source commits. No separate graph history
is kept in the current tree. Stage `.codemanager/index/` including deletions when
committing. Commit checks reject leftover obsolete shards. Snapshot loading and
publishing share a local file lock, so a publisher cannot remove shards while
another helper is loading them. Exported metadata covers the local indexed working
tree, including nonignored untracked files; commit matching source files and avoid
exporting unrelated local work.

## Use from Codex

CodeManager includes a local stdio MCP server with three tools:

- `index_repository`: refresh the graph and publish the portable snapshot.
- `search_code`: refresh, then retrieve ranked code and graph neighbors; `query`, `budget`, `limit`.
- `graph_stats`: inspect counts.

For an installed command, use `python -m pip install -e /home/yeo/codemanager` in
your preferred virtual environment. Alternatively, no installation is needed with
the following Codex configuration example:

```toml
[mcp_servers.codemanager]
command = "python"
args = ["-m", "codemanager", "serve", "--repo", "/absolute/path/to/checkout"]
cwd = "/home/yeo/codemanager"
```

Merge this example into your Codex MCP configuration and restart the client.
Use an absolute Python executable if your client environment differs from your
terminal. One server instance is bound to one checkout. This project does not
modify your global configuration or automatically register itself.

Add guidance like this to the indexed project's `AGENTS.md`:

> Use CodeManager's `search_code` tool to locate relevant code before broad file
> reads. The server refreshes automatically. After editing files, call
> `index_repository` before finishing to publish the final graph immediately.
> Start with a 6000-byte budget. Use returned paths and line ranges for focused
> reads when more detail is needed. Treat candidate edges as hints and verify
> source before editing. If search is incomplete, fall back to ripgrep.

Codex MCP setup reference: https://developers.openai.com/learn/docs-mcp

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

The hook blocks a commit unless the **staged graph matches the staged source**.
It reconstructs the graph from raw Git index blobs in a temporary directory, then
compares the deterministic manifest and referenced shards. It does not run the
repository's code, change your working files, or stage anything. A fresh graph is
required even for the first commit. To fix a stale snapshot:

```bash
python -m codemanager export --repo /path/to/checkout
git -C /path/to/checkout add .codemanager/index/
# Stage exactly the matching source changes, then retry git commit.
```

For partial commits, a working-tree export may include unstaged edits. If so, the
hook correctly rejects it. Export from a separate worktree containing exactly the
intended commit, or finish staging the intended source edits before exporting.
Neither this tool nor the hook automatically stages source files.

Manual checks (also useful in CI):

```bash
python -m codemanager check --repo /path/to/checkout
python -m codemanager check --repo /path/to/checkout --staged
```

Checks use temporary storage and leave the repository cache and snapshot untouched.
Working-tree checks include nonignored untracked code; staged checks use only staged
files, including deletions and partial edits. Staged checks read raw blobs without
Git smudge filters, so repositories that transform line endings or file contents
must generate a snapshot matching those canonical staged bytes.

The installer preserves an existing pre-commit hook or `core.hooksPath` and reports
how to add `check --staged` to that workflow. Its launcher points to the current
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
- Search: SQLite FTS5 BM25, with extra weight for names and paths, followed by
  one-hop expansion across candidate calls/imports. CamelCase is split for search.
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
languages currently get text retrieval only. Search is lexical rather than
embedding-based. The index represents the last refresh, not a live filesystem
snapshot. Polling scans and hashes eligible files, so cost grows with repository
size. There is no remote GitHub synchronization; local Git commands publish changes.

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
