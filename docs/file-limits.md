# File length policy and Codex setup

File length policy is independent of optional review. Defaults are **500 lines**
and **20,000 characters** per text file. Either threshold blocks task completion
and the CodeManager commit check, even when `review.enabled` is false. Code is
never truncated to pass. New content must fit; old oversized files may be split
or deleted without their previous contents blocking the refactor.

## Configuration

```bash
python -m codemanager file-limits --repo . --max-lines 500 --max-chars 20000
```

This writes a top-level policy, without enabling review:

```json
{
  "file_limits": {
    "max_lines": 500,
    "max_chars": 20000
  }
}
```

Both values must be positive integers. `file-limits --repo .` shows the effective
settings and migrates legacy `review.max_file_lines` / `review.max_file_chars`
settings into this section, removing the old keys and preserving other settings.
New standalone settings take precedence. The previous `review-config
--max-file-lines/--max-file-chars` flags have been removed.

## Checks without a reviewer

```bash
python -m codemanager check-length --repo .
python -m codemanager check-length --repo . --all
python -m codemanager check-length --repo . --staged
```

The default scope is changed tracked files plus nonignored untracked files,
relative to HEAD. `--all` audits all tracked and nonignored untracked files.
In a new repository without commits, all candidate files are checked. Outside
Git, the checker walks the directory using CodeManager's standard exclusions.

`--staged` checks raw staged blobs against the staged `codemanager.json`, leaving
working files and partial staging intact. Deleted files, symlinks, known generated
cache/build directories (including `.codemanager/`), and files containing NUL bytes
are excluded. Text counts physical lines and decoded characters, not tokens;
invalid UTF-8 bytes count as replacement characters. Exactly meeting a limit is
allowed. Violations report the file, actual counts, and limits; the CLI exits 1.

`complete-task` always runs this checker. If optional review is disabled, it then
returns without invoking a model. `commit-check` enforces staged file lengths
before validating the graph/embeddings and optional review approval. Install the
local Git hook with `install-hook --repo .`, or invoke `commit-check` from an
existing pre-commit workflow. This rule does not depend on review being enabled.

## Codex instructions and Stop hook

Install both layers using the environment where CodeManager is installed:

```bash
python -m codemanager install-codex-hook --repo .
```

The installer appends a managed general file-policy section to `AGENTS.md`,
preserving other instructions. It also merges command hooks into `.codex/hooks.json`: `Stop` checks file length,
and `UserPromptSubmit` [supplies retrieved source context](context.md). Other
lifecycle handlers are preserved.
Repeated installation updates the owned block and handler without duplicates.
The hook uses that environment's absolute Python path and CodeManager location;
install it once per clone/environment and reinstall after moving the checkout or
changing the Python environment. Do not copy another machine's hook command.

Start a new Codex session and use `/hooks` to review and trust the new hook.
Project-local hooks require a trusted project. Trust is enforced by Codex; the
installer does not grant trust or bypass that review. See the
[official Codex hook documentation](https://developers.openai.com/codex/hooks/).

At completion the hook runs `codex-length-hook`, which checks the saved working
tree regardless of optional review configuration. On violations it returns a
blocking reason asking Codex to split the affected files. Once corrected, the
hook allows completion. Invalid policy/check errors also ask Codex to continue
instead of silently passing. Reviewer subprocesses skip this hook to avoid
recursion. It never stages changes or creates a commit.

File limits govern resulting source files; this is not an operating-system ban
on temporarily creating long files during edits. `AGENTS.md` supplies guidance
while coding, the trusted Stop hook checks completion, and the Git hook checks
commits. Review may cover several files, so per-file bounds are not a total
context-window budget. Reviewers receive the same independent `file_limits`
policy and must respect it when inspecting additional dependencies.
