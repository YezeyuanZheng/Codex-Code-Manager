# Retrieval work handoff — 2026-11-19

The goal is complete source coverage with less model-facing context and lower
retrieval and session latency. The prompt hook uses hybrid retrieval by default.

## Graph-backed hints evaluation

The 16-session run used GPT-6.2 Sol at low reasoning across four arms and the
same four frozen pytest prompts. The prompt hook now returns up to two source
nodes under 3,500 bytes. Compact MCP retrieval can return graph-backed
`called_symbols` hints for exact follow-up lookups.

| Arm | Bodies before shell | Final bodies | Input tokens | Total wall time |
| --- | ---: | ---: | ---: | ---: |
| Default Codex | — | 11/13 | 537,213 | 214.49 s |
| Strict hybrid | 12/13 | 12/13 | 397,748 | 208.61 s |
| Strict BM25 | 12/13 | 12/13 | 398,253 | 215.36 s |
| BM25 with hybrid override | 12/13 | 12/13 | 410,526 | 231.90 s |

Wall time includes hook startup and shell calls. Input tokens are session totals.
All three retrieval arms missed `PytestPluginManager._import_plugin_specs`.
Strict hybrid surfaced its exact name in `called_symbols`. A separate replay
added one targeted shell read and raised strict-hybrid coverage to 13/13.
The replay did not measure agent-selected fallback or its input-token cost.
Raw traces are local benchmark artifacts.

## Current implementation

- The prompt hook and MCP `get_context` return compact source spans, context
  tokens, and actionable pending entries. CLI and library output remains detailed
  by default. Use `compact: false` for MCP graph diagnostics.
- A follow-up can send the needed `pending` entries with the latest
  `context_token`. Packing preserves line boundaries when a complete line fits.
  For a method cut off inside its class, request its qualified name in `symbols`.
- Use `budget` for byte limits. MCP also accepts `max_bytes` and `budget_bytes`.
  A trace showed that an unsupported `max_bytes` caused a wasted tool round trip
  before the alias was added.
- Commit `6d90235` added direct NumPy loading of cached vectors, cached parsed
  caller names, and the continuation and alias fixes. The latest work adds
  heap-based top-K ranking and a bounded cache for source line offsets.
  An ASCII caller prefilter was tested and dropped after an inconsistent repeat.

## Archived frozen pytest baseline

The four questions use pytest at `36eed6b7b0eb59760fc4b33e90bdaac5057388f0`.
The archived baseline is in the Git-ignored bundle
`.codemanager/retrieval-eval-sol-low-GQuu16JT/`. It combines 12 original sessions
with four refreshed strict-hybrid sessions. All used GPT-6.2 Sol at low
reasoning. CodeManager revisions differ by arm and are recorded in `setup.json`.

| Arm | Coverage | Before shell | Shell calls | Cumulative input tokens | Total session time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default Codex | 12/13 | — | 22 | 486,453 | 219.4 s |
| Strict hybrid | 13/13 | 13/13 | 0 | 468,505 | 273.4 s |
| Strict BM25 | 9/13 | 9/13 | 0 | 457,012 | 261.1 s |
| BM25 with hybrid override | 12/13 | 11/13 | 3 | 545,827 | 286.8 s |

The first Sol strict-hybrid run scored 11/13. It left
`KeywordMatcher.from_item` and `Config.parse` incomplete. The latest rerun
recovered both and had zero retrieval errors across 13 MCP calls, including six
successful `max_bytes` calls. Its initial hooks took 3.81 s total, outside the
session timer. Session traces lack per-call model and tool durations, so the
54.0 s gap against default Codex remains unattributed.

The top-K and line-offset changes have only been tested by replaying the 13
recorded MCP requests with regenerated context tokens. Together they reduced
median backend time from 697 to 653 ms across nine paired replays, winning every
pair. Response payloads matched apart from context tokens, with 13/13 coverage.
The caller prefilter was tested separately and is absent from the current code.

The older `.codemanager/retrieval-eval-v2/` bundle preserves the baseline from
`c5e470b` under the CLI's then-default model settings. Its strict-hybrid arm
also scored 13/13. A separate account-wide snapshot moved from 89% to 91%
during the initial 16-session Sol run. That change includes concurrent activity.

## Reproduce the live benchmark

Run from the CodeManager repository root. The runner writes results into its
own directory. A new copy preserves the prior reports.

```bash
run_dir=$(mktemp -d .codemanager/retrieval-eval-next-XXXXXXXX)
cp .codemanager/retrieval-eval-sol-low-GQuu16JT/{run.py,evidence.py,efficiency.py,rescore.py,cases.json,PROTOCOL.md} "$run_dir/"
.venv/bin/python "$run_dir/run.py" &&
  .venv/bin/python "$run_dir/rescore.py"
```

The frozen checkout is `/tmp/codemanager-eval-v1-20261117/frozen`. Recreate it
at the pinned pytest revision and warm embeddings if it is missing. The
bundles, traces, and replay scripts are local artifacts. Strict BM25 runs with
`serve --bm25-only`. Exact lookups report `lookup` and are valid in either
strict arm.

## Next session

1. Measure current code in fresh matched sessions to verify live coverage and
   latency. Use the same model and reasoning effort across arms, and keep
   the run and rescored output together.
2. Timestamp model and tool events, and measure MCP handler time separately
   from the startup hook and overall subprocess time. Compare session latency,
   coverage, failed calls, payload bytes, repeated source, and cumulative tokens.
3. Use recorded request replay for backend tuning. Treat shell fallback,
   account usage, and full session times as live-session measurements.

The current suite ran 157 tests with one skip. Follow AGENTS.md for exporting
the portable graph, file-length checks, and staged commit validation.
