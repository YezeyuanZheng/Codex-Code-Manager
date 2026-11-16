# Retrieve relevant nodes before reading more source

The coding workflow is:

1. Search against the request or current requirements.
2. Retrieve relevant graph node IDs and their related nodes.
3. Refine the query or continue a truncated node when evidence is incomplete.
4. Only then read focused source ranges for a specific unresolved gap.

Whole-file reads are not the coding default. Review is a separate file-by-file
workflow where full-file reads are allowed within file limits. A relationship is a conservative graph hint,
not proof of a call path or complete behavior. Verify it against the retrieved
source. Unchecked requirements remain unresolved and must fail review.

## MCP and CLI

`retrieve_nodes` accepts `node_ids`, `budget` (default 6000 serialized bytes),
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
incoming/outgoing direction. Duplicate nodes are not repeated. Graph resolution
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
