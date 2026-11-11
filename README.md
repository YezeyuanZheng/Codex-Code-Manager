# CodeManager

A local repository index for coding agents. CodeManager combines BM25 keyword
search with local embedding cosine similarity, then returns graph-related source
snippets under an explicit output budget. Describe a feature or intended code
change, or search for an exact symbol. No API key or external database is required.
Requires Python 3.11+, SQLite with FTS5, Git for ignore-aware discovery, and FastEmbed
for the default hybrid search. Model weights download on first embedding use.

## Try it

Create an environment and install the helper from this project directory:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m codemanager index --repo /path/to/checkout
.venv/bin/python -m codemanager embed --repo /path/to/checkout
.venv/bin/python -m codemanager search --repo /path/to/checkout "Add a check that prevents unauthenticated users from opening private pages" --budget 6000
```

`embed` warms the model and vector cache; searches also generate any missing
vectors automatically. Restart the MCP server using this environment's Python.
If you need keyword-only operation without the embedding dependency or model:

```bash
python -m codemanager search --repo /path/to/checkout "authenticate" --mode bm25
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
No Git LFS, network call, or manual import is needed for loading the graph bundle.
Embedding vectors are derived locally after clone, once per passage and model;
they are not included in the committed graph. The first semantic search may need
to download model weights, while subsequent searches reuse the local model cache.

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
- `search_code`: refresh, then retrieve code and graph neighbors; `query`, `budget`, `limit`, `mode` (default `hybrid`), and optional `model`.
- `graph_stats`: inspect counts.

Install with `python -m pip install -e /home/yeo/codemanager` in your preferred
virtual environment. Point the Codex MCP configuration to that environment's
Python so it can find FastEmbed. Example for this project's `.venv`:

```toml
[mcp_servers.codemanager]
command = "/home/yeo/codemanager/.venv/bin/python"
args = ["-m", "codemanager", "serve", "--repo", "/absolute/path/to/checkout"]
cwd = "/home/yeo/codemanager"
```

Merge this example into your Codex MCP configuration and restart the client.
Use an absolute Python executable if your client environment differs from your
terminal. One server instance is bound to one checkout. This project does not
modify your global configuration or automatically register itself.

Add guidance like this to the indexed project's `AGENTS.md`:

> Use CodeManager's `search_code` tool to locate relevant code before broad file
> reads. Describe the intended feature or change in natural language; use exact
> names when known. Hybrid search combines keywords and embeddings. The server refreshes automatically. After editing files, call
> `index_repository` before finishing to publish the final graph immediately.
> Start with a 6000-byte budget. Use returned paths and line ranges for focused
> reads when more detail is needed. Treat candidate edges as hints and verify
> source before editing. If search is incomplete, fall back to ripgrep.

Codex MCP setup reference: https://developers.openai.com/learn/docs-mcp

## Hybrid retrieval

The default `hybrid` mode retrieves up to `max(30, 4 * limit)` candidates from each
branch, merges the rankings, selects `limit` primary hits, and adds candidate
call/import neighbors within the same output budget:

1. SQLite FTS5 BM25 scores exact terms, with higher weights for names and paths.
2. FastEmbed embeds source passages and the full natural-language query locally.
   Normalized vector dot products give cosine similarity. Each symbol uses its
   highest-scoring passage; file preview nodes are excluded from vector search.
3. Equal-weight reciprocal rank fusion scores each candidate as the sum of
   `1 / (60 + rank)` across the two lists. This avoids directly adding BM25 and
   cosine scores, whose numerical scales differ.

Results report `mode` and per-hit `signals` (`bm25`, `embedding`, or `graph`).
A semantic hit in a long definition returns the matching passage and its line
range. Passages contain up to 1200 source characters with a path/name header;
the model may further tokenize/truncate unusually dense code. No generated
summaries or LLM calls are used.

Choose `--mode hybrid`, `--mode semantic`, or `--mode bm25` on CLI searches or
`serve`. MCP `search_code` also accepts a per-query `mode`. Set
`CODEMANAGER_SEARCH_MODE=bm25` for an explicit keyword-only default. Hybrid does
not silently fall back to keyword search when dependency, model, or inference
setup fails; it returns an actionable error.

The default is `jinaai/jina-embeddings-v2-base-code`, a code-specific model trained
on natural-language/code pairs. It produces 768-dimensional vectors and supports
English and 30 programming languages. FastEmbed supports its local ONNX inference;
the model download is roughly 0.64 GB, with additional runtime memory required.
There is no single industry-standard embedding model; this is an established,
code-focused default rather than a claim of universal adoption or best quality.
See the [publisher's model card](https://huggingface.co/jinaai/jina-embeddings-v2-base-code).
For a smaller general-text alternative, set
`CODEMANAGER_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`. You can also select another
FastEmbed-supported model.
Set `CODEMANAGER_MODEL_CACHE` to control weight storage (default
`~/.cache/codemanager/models`). Inference uses two CPU threads. See the official
[FastEmbed model registry](https://qdrant.tech/documentation/embeddings/fastembed/)
and [RRF overview](https://www.elastic.co/docs/solutions/search/hybrid-search).

Vectors are stored in the ignored SQLite cache. Identity includes the FastEmbed
version, model name, preprocessing version, and passage hash. Unchanged passages
reuse their vectors; changed passages are re-embedded before the next hybrid or
semantic search, deleted nodes lose their vectors, and model changes replace the
old model cache. Polling/export keeps graph metadata current without downloading
a model or running inference. `embed` explicitly warms vectors after edits.
Commit checks remain offline and deterministic; they do not require embeddings.

Dense search currently scans the cached vectors exactly in Python. Its cost grows
with passage count times vector dimensions; large repositories may benefit from
a future approximate nearest-neighbor index. Real-model retrieval quality and
context savings must be measured on representative feature requests.

## Switch embedding models

Inspect the effective model, its source, and suggested alternatives without
loading a model or downloading weights:

```bash
python -m codemanager model --repo /path/to/checkout
```

Save a default for this repository:

```bash
python -m codemanager model --repo /path/to/checkout --set BAAI/bge-small-en-v1.5
```

This writes a shareable `codemanager.json` in the repository root:

```json
{
  "embedding": {
    "model": "BAAI/bge-small-en-v1.5"
  }
}
```

Override the default for one command or for an MCP server instance:

```bash
python -m codemanager search --repo /path/to/checkout "Add an authorization check" --model jinaai/jina-embeddings-v2-base-code
python -m codemanager embed --repo /path/to/checkout --model BAAI/bge-small-en-v1.5
python -m codemanager serve --repo /path/to/checkout --model BAAI/bge-small-en-v1.5
```

MCP `search_code` accepts a `model` argument too. A per-query override takes
priority over the server's `--model`. Otherwise, precedence is command-line model,
`CODEMANAGER_EMBEDDING_MODEL`, repository configuration, then the built-in Jina
code model. Search output reports the actual model used (`null` for BM25-only).
The saved default is reread on each search, so an unpinned running server picks
up changes without restarting. One-off overrides do not modify the saved default.

Switching models rebuilds vectors lazily on the next hybrid/semantic search, or
immediately with `embed`. Different model vectors are never mixed. A failed rebuild
keeps the previous cache intact and reports the error. Repeatedly switching models
will recompute vectors because only the current model's vector cache is retained.
Downloaded model weights may remain in the separate model cache for reuse.

Restore the built-in default (environment overrides still apply):

```bash
python -m codemanager model --repo /path/to/checkout --reset
```

Model IDs must be supported by the installed FastEmbed version; suggestions are
not the complete registry. Syntax is checked when saving, while FastEmbed verifies
model support when inference initializes. Saving a selection does not prove its
weights are available. Repository configuration contains the model ID only;
weights and vectors stay outside Git. If committing `codemanager.json`, refresh
and stage its matching graph snapshot as for other indexed files.

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

Hybrid tests use controlled vectors to validate fusion, no-keyword-overlap hits,
incremental cache behavior, model changes, long-symbol passages, output budgets,
provider errors, and MCP output. They do not prove a real model's relevance quality.
Run the opt-in real-model smoke test after installing and downloading the model:

```bash
CODEMANAGER_REAL_EMBEDDINGS=1 .venv/bin/python -m unittest discover -s tests -p test_hybrid.py -v
```
