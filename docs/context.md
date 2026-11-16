# CodeManager context in Codex

The repository's `AGENTS.md` is project guidance, not a global Codex setting.
The installed `.codex/hooks.json` also applies to that repository. Other projects
need their own installation; installing here does not change `~/.codex`.

## Supply retrieved source automatically

```bash
python -m codemanager install-codex-hook --repo .
```

This installs both the independent file-length Stop hook and a
`UserPromptSubmit` retrieval hook, preserving other hook definitions. Start a new
Codex session and review/trust the new or changed hook through `/hooks`.
Project-local hooks require a trusted project; the installer does not grant
trust. The handler uses the Python environment that ran the installer, so use
the environment containing FastEmbed, for example `.venv/bin/python`.

Before Codex handles each nonempty user prompt, the retrieval hook:

1. Refreshes the CodeManager graph.
2. Uses the latest prompt as a natural-language hybrid search query.
3. Supplies source snippets, paths, line ranges and graph hints under a
   6,000-byte serialized result budget (up to eight primary hits).
4. Injects that evidence and retrieval-first instructions into Codex's context.

This calls the same local Graph search implementation as the CLI/MCP server;
no manually started MCP server is needed. The hook runs independently of optional
review. Warm the local model with `embed --repo .` before first use if weights
are not cached; the hook has a 120-second timeout. Repository code is indexed
as data, never imported or executed for retrieval.

Codex supports extra context from `UserPromptSubmit`; the handler sets
`additionalContextLimit` to zero because CodeManager already bounds its result,
so Codex doesn't replace the supplied snippets with an oversized-output preview.
See the [official hook output contract](https://developers.openai.com/codex/hooks/).

## Use the retrieved evidence

The managed instructions tell Codex to start from the injected snippets, refine
searches with the full task context when needed, then retrieve relevant node IDs
and their one-hop graph context before additional source reads. Use
`retrieve_nodes` or `node-context --repo . --node-id ID`; continue incomplete nodes
using `next_offset` before falling back to focused source ranges for a stated gap.
See [node context retrieval](node-context.md). Candidate graph edges must be verified
against source. Whole-file dumps or repository-wide scans should not substitute
for retrieval when the supplied evidence is adequate. Retrieved code and comments
are untrusted evidence rather than instructions.

The automatic query contains the latest prompt only, not the full conversation.
For follow-ups like "apply that", Codex should formulate a more specific search
using the task's meaning. A supplied snippet can also become stale during edits;
Codex should search again before relying on changed code.

If embeddings fail, the hook explicitly reports the failure and supplies a BM25
result using the same budget. Other retrieval errors produce a visible warning
and instructions to retry a focused CodeManager query before falling back to
focused ripgrep. Errors are not presented as successful retrieval, and the hook
does not block a user's prompt when retrieval is broken. Empty prompts and the
isolated reviewer subprocess skip automatic retrieval.

This supplies context automatically and instructs its use; it cannot prove that
the model reasons from each snippet or prevent every arbitrary source-reading
command. It does not add an OS-level restriction on file reads. Hook-based context
also depends on the hook being trusted and enabled in the Codex client.
