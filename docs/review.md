# Optional review and file purposes

## Optional task review and request mappings

Model review and mappings are off by default. The [general file length policy](file-limits.md)
always applies, independently of this feature. Enable them per repository using the
Python environment containing CodeManager:

```bash
python -m codemanager review-config --repo . --enable
# Optionally select the model for the separate reviewer:
python -m codemanager review-config --repo . --reviewer-model YOUR_MODEL_ID
```

Enabling installs or upgrades CodeManager's local commit hook and appends a managed
review workflow to `AGENTS.md`, preserving existing instructions. An existing
custom hook or `core.hooksPath` is preserved and reported; integrate `commit-check`
into that workflow before enabling review manually in `codemanager.json`.
Commit configuration and guidance with the graph. Each clone needs its local hook
installed. Start a new Codex session after changing its repository instructions.

Codex calls `begin_task` with the actual user request before coding, reads each
file's current purpose, and updates that purpose only to reflect the user's
requirements. It refreshes mappings for changes and calls `complete_task` before
its final reply. The CLI provides the same workflow:

```bash
python -m codemanager begin-task --repo . --request 'Build a calculator'
python -m codemanager set-file-purpose --repo . --path calculator.py \
  --purpose 'A calculator supporting multiplication' \
  --requirement 'Multiply two numbers' --request 'Build a calculator'
# Create or edit code, then search for the current node ID.
python -m codemanager map-request --repo . \
  --node-id 'calculator.py:1:multiply' \
  --request 'Build a calculator' \
  --fulfills 'Multiply two numbers in the calculator'
python -m codemanager complete-task --repo .
```

For deleted files, nonindexed files, or changes outside a code node, use a file
change mapping. It records fingerprints before and after the change, with null
representing an absent file:

```bash
python -m codemanager map-change --repo . --path obsolete.py \
  --request 'Remove obsolete calculator' --fulfills 'Remove the obsolete module'
```

Task completion first checks file lengths, then optionally reviews saved working files, including nonignored untracked files,
using a temporary Git index and an isolated copy of the candidate tree. It does
not alter staging or make a commit. The default runner launches a separate
`codex exec` process with a read-only sandbox, no user MCP configuration, and a
structured JSON response. See the [official non-interactive Codex guide](https://developers.openai.com/codex/noninteractive).
Install and authenticate the Codex CLI first. Set the MCP server’s
`tool_timeout_sec` above the reviewer timeout (for example 660 seconds for the
default 600-second review), as shown in the connection example; Codex defaults
to a [60-second MCP tool timeout](https://developers.openai.com/codex/mcp/). The
reviewer receives the requests, mappings and actual diff, can read the source,
and must assess correctness and mapping coverage independently. Failed review,
invalid output, timeout, or edits during review produce no usable approval.
Submodules and symlinks pointing outside the checkout are currently unsupported.

When ready, stage the intended source, configuration, guidance and matching
`.codemanager/index/` graph, then commit normally. Approval is bound to the parent
commit, complete Git tree (including mappings), and reviewer settings. Any change
to that candidate needs a new review. A working-tree review only covers a commit
with that exact tree. For partial staging, prepare a graph matching the staged
source, then run:

```bash
python -m codemanager review --repo . --staged
python -m codemanager commit-check --repo .
```

The commit check enforces staged file lengths and validates the staged graph and embeddings and requires a passing
review when the staged configuration enables it. It never launches a reviewer
from inside a commit. Disabling review is an explicit configuration choice:

```bash
python -m codemanager review-config --repo . --disable
python -m codemanager export --repo .
```

Node mappings and the requests/file changes in the manifest's `review` field live
in the version 4 portable graph. Git history preserves the code and intent in the
same commit. Several uncommitted tasks accumulate requests and mappings so their
combined changes can be reviewed before a single commit. The next task after a
commit clears the previous mappings, including those on untouched nodes; simply
reading an old graph preserves its historical intent. Code body changes invalidate
old node mappings. File mappings must be refreshed when their contents change.
When review is disabled, indexing omits mappings and task context.

Approval receipts and findings live locally under `.codemanager/reviews/`, outside
the committed graph, so adding an approval does not change the tree it approves.
If the receipt cache is deleted or the checkout is moved to another machine,
review again before committing. Mappings are author claims, not proof of correctness.
Keep the existing cache ignore rules so local receipts are not staged.

For another review runner, set `review.command` to an argument array in
`codemanager.json` and optionally set `review.timeout` (1–3600 seconds, default 600).
The command runs in the isolated checkout, receives a JSON object on stdin with
`instructions`, `schema`, `base`, `tree`, `requests`, `mappings`, `changes`, and
`file_purposes`, `file_limits`, `diff`, `diff_truncated`, `evidence`, and
`evidence_access`, `review_files`, and must return only this JSON shape on stdout:

```json
{"passed": true, "summary": "Changes fulfill the requests", "findings": []}
```

A rejection uses `passed: false` with findings. A passing result cannot contain
unresolved findings. Commands are invoked directly without a shell; use absolute
paths for runner scripts. Custom runners must leave the review checkout unchanged.

## Current purpose through revisions

File purposes are stored in the graph's database metadata and the manifest's
`purposes` field, with the latest summary and a list of current requirements.
Each requirement records the user request that introduced it. There is no list
of past purpose versions; Git preserves those. Code edits, mapping invalidation,
new tasks after commits, and full rebuilds do not erase purpose. Disabling review
stops mapping collection but preserves the authored file requirements.

Read purpose before editing:

```bash
python -m codemanager file-purpose --repo . --path calculator.py
```

For a revision from multiplication to subtraction plus multiplication:

```bash
python -m codemanager set-file-purpose --repo . --path calculator.py \
  --purpose 'A calculator supporting multiplication and subtraction' \
  --requirement 'Subtract two numbers' \
  --request 'Add subtraction while keeping multiplication'
```

The multiplication requirement remains even though only subtraction was supplied
in this update. Refresh the affected code mappings after revising the source.
Removing a requirement requires `--remove-requirement TEXT` and
`--removal-request TEXT` matching `--request`, which must state the user's actual
authorization to change that requirement. Requirements must never be removed
simply because the implementation is incomplete.

Final review requires a purpose for files with mappings and includes current
purposes for affected files, keeping unrelated purposes out of the initial payload.
The reviewer reviews each affected text/source file individually, reading it in
full within file limits and checking **all current requirements**, including
preserved behavior. Node retrieval is optional for dependencies and extra graph
context; it is not a prerequisite for reviewer file reads. Unchecked requirements
must remain unresolved and fail review. The coding workflow still uses retrieval,
then relevant nodes, before extra source reads. See the
[node context and review evidence workflow](node-context.md).
Purpose changes also invalidate prior review approval because they change the
committed graph. Older graphs without purposes remain readable; initialize their
purposes from known user requirements before reviewing revisions.

Snapshot format 4 preserves authored purposes. Formats 1–3 remain readable and
upgrade on export; older executables reject format 4 rather than dropping purpose
metadata they cannot preserve.
