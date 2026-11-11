# Working on CodeManager

Use the CodeManager MCP search tool, when connected, to locate relevant code before
broad file reads. Candidate graph edges are hints; verify source before editing.

After changing indexed code or documentation, refresh the portable graph before
finishing:

```bash
python -m codemanager export --repo .
python -m codemanager check --repo .
```

The MCP server polls and publishes edits automatically while running. Call
`index_repository` after the final edit to publish immediately. Outside MCP, use
the export command above or run `python -m codemanager watch --repo .`.

Before committing, stage matching source changes and `.codemanager/index/`, then
run `python -m codemanager check --repo . --staged`. Keep partial staging intact;
do not stage unrelated user edits to satisfy the graph check. Install the local
commit check with `python -m codemanager install-hook --repo .` once per clone.

Run `python -m unittest discover -s tests -v` for implementation changes.
