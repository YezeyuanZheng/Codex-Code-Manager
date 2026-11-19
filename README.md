# CodeManager

Continuing the retrieval work? Read [HANDOFF.md](HANDOFF.md) for the current
implementation, real Codex evaluation results, artifacts, and remaining gaps.

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
.venv/bin/python -m codemanager context --repo /path/to/checkout "Add a check that prevents unauthenticated users from opening private pages" --budget 6000
```

`embed` warms the model and vector cache; searches also generate any missing
vectors automatically. Restart the MCP server using this environment's Python.
If you need keyword-only operation without the embedding dependency or model:

```bash
python -m codemanager context --repo /path/to/checkout "authenticate" --bm25-only
# Save the default for the prompt hook, CLI, library, and unpinned MCP server:
python -m codemanager retrieval-mode --repo /path/to/checkout --set bm25
# Enforce keyword-only operation on a server, including per-request overrides:
python -m codemanager serve --repo /path/to/checkout --bm25-only
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

## Set up Codex hooks for each checkout

For automatic retrieved context and completion checks in Codex, install the hooks
once for every checkout you work on. Use the same Python environment in which you
installed CodeManager and FastEmbed:

```bash
.venv/bin/python -m codemanager install-codex-hook --repo /path/to/checkout
```

This creates or updates the target checkout's `.codex/hooks.json` and adds managed
guidance to its `AGENTS.md`, preserving other instructions and hook handlers:

- **UserPromptSubmit** supplies budgeted CodeManager source and relationship hints
  before Codex handles your message.
- **Stop** checks file-length limits before Codex finishes, even when optional
  independent review is disabled.

Start a new Codex session in that checkout and use `/hooks` to review and trust
the hooks. The project must be trusted; the installer does not grant trust.

The generated commands contain absolute paths to your Python interpreter,
CodeManager installation, and target checkout. Generate them locally rather than
copying another machine's commands. Rerun installation after moving a checkout or
changing its Python environment. A Git clone alone does not install or activate
these hooks; the setup command is repeatable without duplicating owned handlers.

Hooks call CodeManager directly and do not require a running MCP server. To expose
`get_context` for follow-up retrieval, also configure the MCP connection described
under [Use from Codex](#use-from-codex). Standalone CLI/MCP use does not require
Codex hooks. See [automatic context setup](docs/context.md) for details.

## Ship the graph with a Git clone

Export a portable snapshot after changing the code:

```bash
python -m codemanager export --repo /path/to/checkout
```

This refreshes the graph, then writes a deterministic manifest and content-addressed
JSONL shards under `.codemanager/index/`. Snapshot format 4 partitions records by
a stable hash of the source path, so edits rewrite only affected partitions. Each
shard is at most 1 MiB. Nodes store
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

CodeManager includes a local stdio MCP server with nine tools:

- `index_repository`: refresh the graph and publish the portable snapshot.
- `get_context`: the single retrieval tool; batch questions, symbols, or node IDs,
  with compact source excerpts and per-node continuations under one shared byte budget.
- `graph_stats`: inspect counts.
- `begin_task`: register a coding task when optional review is enabled.
- `get_file_purpose`: read a file's current purpose and requirements before editing.
- `set_file_purpose`: update its purpose, preserving requirements unless explicitly removed.
- `map_request`: attach the request and fulfilled requirement to a code node.
- `map_change`: explain a changed file or deletion outside a code node.
- `complete_task`: run the optional independent review without staging or committing.

The old MCP retrieval names are removed. Lower-level `search` and `node-context`
CLI commands remain available for compatibility. Restart an existing CodeManager
MCP server to load the updated catalog.

Install with `python -m pip install -e /absolute/path/to/codemanager` in your preferred
virtual environment. Point the Codex MCP configuration to that environment's
Python so it can find FastEmbed. Example for this project's `.venv`:

```toml
[mcp_servers.codemanager]
command = "/absolute/path/to/codemanager/.venv/bin/python"
args = ["-m", "codemanager", "serve", "--repo", "/absolute/path/to/checkout"]
cwd = "/absolute/path/to/codemanager"
# Allow complete_task to finish its optional review (default reviewer timeout: 600s).
tool_timeout_sec = 660
```

Merge this example into your Codex MCP configuration and restart the client.
Use an absolute Python executable if your client environment differs from your
terminal. One server instance is bound to one checkout. This project does not
modify your global configuration or automatically register itself.

### Retrieve just enough source

1. Start with the prompt hook. It returns up to two source nodes within a
   3,500-byte budget. Use a focused `get_context` query if you need more evidence.
2. Batch known definitions in `symbols`. Exact lookup accepts qualified names
   such as `Cache.load` or `src/cache.py:Cache.load`. When the response lists
   `called_symbols`, copy any names you need into `symbols` on the next request.
   These graph-backed hints identify callees visible in the returned source.
3. If a required definition is incomplete, send its entries from `pending`
   with the latest `context_token`. That token lets retrieval skip source
   you've already seen. Pass `include_related: true` when you need related source.
4. If a needed range is still missing after a focused follow-up, have the agent
   read that file and line range. The hook and MCP server supply source and hints.
   The agent makes the fallback read.

For example, an MCP follow-up can send
`{"symbols":["src/cache.py:Cache.load"],"budget":4000}`. The MCP byte budget
defaults to 12,000. See [repository guidance and retrieval examples](docs/node-context.md)
for instructions you can put in the target project's `AGENTS.md`. After edits,
call `index_repository` to publish the updated portable graph immediately.

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

The lower-level `search` command reports `mode` and per-hit `signals` (`bm25`, `embedding`, or `graph`).
A semantic hit in a long definition returns the matching passage and its line
range. Passages contain up to 1200 source characters with a path/name header;
the model may further tokenize/truncate unusually dense code. No generated
summaries or LLM calls are used.

Choose `--mode hybrid`, `--mode semantic`, or `--mode bm25` on CLI searches or
`serve` and `codex-context-hook`. MCP `get_context` also accepts `mode` or
`bm25_only: true`. An explicit request/CLI mode overrides the environment, then the
saved `retrieval.mode` in `codemanager.json`, then the hybrid default. A server
started with `--bm25-only` rejects hybrid/semantic requests. Set
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
a model or running inference. `embed --progress` explicitly warms vectors after
edits and prints completed/total counts to stderr. Reusable content digests retain
vectors when node IDs move because earlier lines changed; orphan reuse is bounded
and model-specific, and failed regeneration remains transactional.
Graph-only checks remain offline and deterministic. The installed pre-commit
hook additionally requires missing/stale embeddings to be regenerated successfully.

Dense search reuses a decoded NumPy matrix (with a standard-library fallback),
batches query embeddings, and checks source/vector generations once per context
batch. File-stat bookkeeping does not invalidate the matrix; actual code, mapping,
vector, or model changes do. Exact cosine search still has a cost that grows
with passage count times vector dimensions; large repositories may benefit from
a future approximate nearest-neighbor index. Real-model retrieval quality and
context savings must be measured on representative feature requests.

For source coverage across calls, pass the returned `context_token` into the next
`get_context` request; use `include_seen: true` to reread it. Tokens are explicit,
bounded local derived receipts, with per-file digest checks after edits. See
[context retrieval](docs/node-context.md) and the [information-efficiency rubric](docs/retrieval-evaluation.md).

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

MCP `get_context` accepts a `model` argument too. A per-request override takes
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

## More documentation

- [Indexing, validation, and development](docs/internals.md)
- [Optional task review and current file purposes](docs/review.md)
- [Independent file limits and Codex setup](docs/file-limits.md)
- [Automatic retrieved context in Codex](docs/context.md)
- [Relevant graph nodes and bounded review evidence](docs/node-context.md)
