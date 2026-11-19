# Retrieve relevant nodes before reading more source

The coding workflow is:

1. Start with the prompt-hook source and relationship hints; stop if sufficient.
2. Batch independent gaps into one `get_context` call, using questions, exact
   symbols, or selected node IDs. It includes bounded expansion already.
3. Continue incomplete nodes with batched per-node offsets when needed.
4. Only then read focused source ranges for a specific unresolved gap.

Whole-file reads are not the coding default. Review is a separate file-by-file
workflow where full-file reads are allowed within file limits. A relationship is a conservative graph hint,
not proof of a call path or complete behavior. Verify it against the retrieved
source. Unchecked requirements remain unresolved and must fail review.

## MCP and CLI

Prefer `get_context` or the `context` CLI for fewer agent round trips:

```bash
python -m codemanager context --repo . \
  --query 'Where is a request validated?' --query 'Where is it saved?'
python -m codemanager context --repo . \
  --symbol validate --symbol persist --symbol package.module.Handler.handle
python -m codemanager context --repo . \
  --cursor 'app.py:4:calculate=1800' --cursor 'other.py:10:save=600'
```

MCP accepts `query` or up to eight `queries`, up to twenty `symbols` and `node_ids`,
and an `offsets` mapping from requested node IDs to character offsets. Symbols
accept exact/qualified names or `path.py:symbol`; ambiguous matches remain visible,
and stale IDs or missing names appear under `unresolved` without discarding other
matches. Exact-symbol/node-only requests bypass embeddings. The default shared
budget is 12,000 serialized bytes. Compact retrieval selects up to two nodes per
query plus slots for explicitly requested symbols and IDs, capped at fifty.
The prompt hook selects up to two nodes under a 3,500-byte budget. Related source is opt-in with
`include_related: true`. Detailed retrieval sizes selection to the budget and
explicit requests (eight to fifty slots). An explicit `limit` sets the maximum.
The shared byte parameter is named `budget` in requests and responses. The
deprecated `budget_bytes` input (CLI `--budget-bytes`) is accepted as an alias;
supplying both names requires equal values.

This operation combines discovery and selected source, with optional one-hop context.
Small definitions are completed where the shared budget permits, rather than
always stopping at 1,200 characters. Each selection starts with a small excerpt;
remaining space first completes explicit definitions, then search matches, then
related definitions, preferring smaller definitions within each group.
Query discovery considers a larger candidate pool and prefers implementation
functions over containers. Mixed implementation/test questions interleave a small
supporting test share; merely mentioning tests does not promote all test hits. Some
selection slots are reserved for related bodies, falling back to search matches
when no useful neighbors exist. Related bodies are ranked against the question.
Missing symbols can include current-name suggestions; suggestions are not silently
substituted. A question paired with a large class selects matching indexed members
instead of blindly returning the class prefix. Indexed, AST-checked call-name matches supplement
graph edges for attribute calls; `text_call_candidate` is a candidate relationship,
not a resolved receiver type. No source-tree scan is required for this expansion.
Individual excerpts remain bounded at 6,000 source characters. Containers stay
compact relationship pointers; matched sections of larger definitions use precise
continuation offsets. Source overlaps are filtered across the whole response.
`pending` exposes deferred selections, and returned `next_offset` values resume
incomplete excerpts. Each result includes a ready-to-use `citation` path and line.

Responses with a budget of at least 1,000 bytes include `context_token`. Pass it in
MCP or `--context-token TOKEN` to reuse prior emitted source coverage, including
partial excerpts; without a token each call starts independently. Each file's
indexed digest is checked, so edited files become eligible again while unchanged
files retain coverage. Tokens are immutable local receipts: branches do not change
one another, and the newest 128 receipts are retained. Expired tokens return a
clear error; retry without one. `include_seen: true` / `--include-seen` rereads prior
source. Covered nodes keep reference pointers; this does not suppress separate
shell reads. Receipts never authorize assuming old source is current after edits.

Use `mode: "bm25"` or `bm25_only: true` for keyword-only retrieval with no model
initialization or query embeddings. The CLI accepts `--bm25-only`; save defaults
with `retrieval-mode --repo . --set bm25` (or `hybrid`). The prompt hook follows
that saved mode. MCP is repository-bound: omit `repo`; use `budget`, not
`max_bytes`. Example: `{"query":"Where is validation implemented?","budget":12000}`.
`truncated: false` describes the selected source ranges; it does not guarantee
that every part of the user's question has been answered.

`get_context` is the only advertised MCP retrieval tool. No additional expansion
is required when sufficient evidence is returned. Read focused source ranges only
for a specific unresolved gap. Old MCP retrieval names are rejected with guidance
to use `get_context`; the lower-level CLI and Python interfaces remain available.

The lower-level operation remains available for targeted and legacy pagination:

The Python `Graph.retrieve_nodes` method accepts `node_ids`, `budget` (default 6000 serialized bytes),
`limit` (default eight results), `include_related` (default true),
`include_requested` (default true), and `offset` (default zero). Use IDs returned
by the current search; IDs include line numbers and may change after edits.

The CLI provides the same operation:

```bash
python -m codemanager node-context --repo . \
  --node-id 'calculator.py:4:calculate' --budget 3000
```

Results contain bounded passages for selected nodes, followed by one-hop callers,
callees, imports or containers. Relations carry the anchor ID, edge kind and
incoming/outgoing direction. Source coverage is checked against indexed file
positions within each response, including character positions on partial lines.
Fully covered source bodies are omitted; partially covered candidates return only
their uncovered excerpts. A node can therefore have multiple disjoint excerpts,
each retaining its ID, source coordinates, and continuation offset. Requested
nodes precede related nodes, and omitted duplicate snippets do not consume the
result limit. Covered nodes retain compact `references` with their IDs, reasons,
graph relationship hints, and `covered_by` IDs pointing to returned source.
Reference metadata shares the response byte budget. Graph resolution
is conservative; non-Python files currently provide chunks and containment rather
than resolved function calls.

When you already have the selected source snippet, use `include_requested: false`
or `--related-only` to spend the budget on neighbors instead of repeating it.
Use `include_related: false` or `--no-related` to inspect only selected nodes.
Long nodes initially supply up to 1200 source characters, further constrained by
the total byte budget. `truncated` marks incomplete evidence; use a result's
`next_offset` with `--offset N` to continue the same node without reading a file.
Offsets count characters in that node's source, not absolute file bytes. Neighbor
nodes start at offset zero. A stale/unknown ID produces an explicit error asking
for a refreshed search.

Search responses apply the same source-coverage filtering and include node-relative
`offset` and `next_offset` coordinates, including when a semantic hit starts partway
through a long line. Coverage records only emitted source, not the full indexed
extent of a truncated node. The filter uses the index without reading source files;
it resets on every call, so it neither hides evidence across calls nor assumes
previously returned code is still current after an edit. Search ranking, snippet
budgets, and the retrieval-first workflow are unchanged.

## Review context

An independent reviewer starts with requirements, mappings, and **at most 6000
bytes of initial source evidence**: a requirement-based keyword search plus
related-node context. A source diff preview is limited to 2000 bytes and has an
explicit `diff_truncated` flag. Requirement/mapping metadata is separate from
these source budgets; this is not a total conversation-token limit.

The payload's `evidence_access.command` is an argument array for an isolated,
read-only evidence helper. Append one of:

- `--query TEXT`: bounded BM25 retrieval from the immutable candidate graph.
- `--node-id ID` (repeatable): selected nodes and one-hop context; optionally
  `--related-only` or `--offset N` for neighbors/continuations.
- `--diff-path PATH`: a bounded diff for a changed file; use the returned
  `next_offset` with `--offset N` to continue only if needed.

Each response is capped at 6000 serialized bytes, with smaller budgets available
through `--budget`. The reviewer helper uses deterministic BM25 and graph lookups:
it needs no embedding download, source refresh, SQLite writes, or filesystem lock.
Ordinary Codex request retrieval continues using hybrid search. The helper's cache
includes the validated candidate graph and per-file diffs, lives only for that
review, and is checked for modifications before accepting the result.

The reviewer reviews each affected text/source file individually and can read the
full file within the configured file limits. `review_files` lists its scope. It
has no mandatory node-retrieval step before file reads. The bounded initial
evidence and read-only helper remain available for dependencies, graph context
and diffs when useful. The coding workflow still retrieves relevant nodes before
supplemental reads.

The reviewer must assess all current requirements, including preserved behavior,
and fail requirements it cannot adequately check. The configured review runner
still decides correctness; these tools and instructions cannot guarantee a
model's reasoning.
