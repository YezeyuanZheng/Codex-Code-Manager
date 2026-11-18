# Retrieval work handoff — 2026-11-18

The goal is complete source coverage with as little model-facing context and
retrieval time as possible. Shell remains a fallback for CodeManager and the
primary retrieval mechanism for the default Codex baseline.

## Current implementation

- On top of `c5e470b` (focused workflow follow-up guidance), the prompt hook
  and MCP `get_context` default to compact output. It keeps source, path and
  line coordinates, the reuse token, and actionable pending or unresolved
  items. Graph relationships still inform selection. MCP callers can request
  `compact: false` for diagnostics. The CLI and library retain detailed defaults.
- The compact format and shorter hook instructions reduced the saved hybrid
  sessions' same-source payload from 169.0 kB to 118.3 kB (30%). This was
  offline reformatting of saved responses. Live coverage and token usage
  remain to be measured.
- Graph expansion follows indexed outgoing edges from selected functions and
  their neighbors to find two-edge bridges. It reuses ranking inputs for nodes
  with multiple hints. On 24 matched pytest samples median relationship
  expansion was 13.06 ms before and 11.47 ms after, with identical outputs.
  This is a graph-stage timing, separate from end-to-end retrieval.
- Strict BM25 uses `serve --bm25-only`. BM25 with override is a separate arm.
  Exact lookups report `lookup` and are valid in either strict arm. Audit
  effective modes and count failed calls separately.

## Latest measured baseline

The v2 baseline used 16 fresh Codex CLI sessions on `c5e470b`, four frozen
questions across four arms, with pytest at
`36eed6b7b0eb59760fc4b33e90bdaac5057388f0`.

| Arm | Total coverage | Before shell | Payload | Extra source | Median session |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default Codex | 12/13 | — | 113.7 kB | 67.3 kB | 45.4 s |
| Strict hybrid | 13/13 | 13/13 | 169.0 kB | 66.2 kB | 66.6 s |
| Strict BM25 | 11/13 | 10/13 | 173.2 kB | 74.7 kB | 58.3 s |
| BM25 with hybrid override | 11/13 | 10/13 | 181.3 kB | 75.2 kB | 59.6 s |

Hybrid made zero shell calls. All CodeManager arms retrieved the four capture
units before shell. Earlier controlled capture replay recovered 1/4 vs 4/4
depending on the follow-up request, with equal coverage between modes for
each fixed request. Extra source counts code outside the required AST spans,
including potentially useful supporting code. Shell source accounting uses
numbered lines. Payload includes hook text and tool results. Input tokens are
cumulative and do not measure peak occupancy.

## Next session

1. Run fresh matched CLI sessions with the compact responses and optimized
   graph expansion. Use a new result directory to preserve the v2 baseline.
   The runner writes into its own directory. Copy its scripts, cases, and
   protocol before launching it with `.venv/bin/python`, then run its
   `rescore.py` for the corrected coverage and efficiency report.
2. Compare total coverage, extra source, repeated source, payload bytes,
   input tokens, and shell fallback. Use pre-shell coverage only for the
   CodeManager arms. Measure warm backend time separately from session time.
3. If coverage holds, identify which source outside the required spans is
   useful before changing selection. Repeat cases and repositories before
   drawing speed conclusions or changing the default retrieval mode.

The Git-ignored `.codemanager/retrieval-eval-v2/` bundle contains `run.py`,
`evidence.py`, `efficiency.py`, `rescore.py`, `cases.json`, `PROTOCOL.md`,
`efficiency-report.json`, `capture-replay.json`, and the raw traces. The
runner's frozen checkout is `/tmp/codemanager-eval-v1-20261117/frozen`.
Recreate it at the pinned revision and warm embeddings if that path is gone.
These artifacts are local to this workspace. Use the evaluation runner's
ephemeral Codex CLI sessions for the measured runs.

Validation before handoff: 156 unit tests ran with one skip. The portable
graph and file-length checks passed. Follow AGENTS.md for commit checks.
