# Retrieval work handoff — 2026-11-17

Start here when continuing this work. Usage is in [README.md](README.md); the
information-efficiency rubric is in [docs/retrieval-evaluation.md](docs/retrieval-evaluation.md).

## Prioritized next tasks

1. Run fresh, matched Codex sessions on the current code after query-identifier
   promotion. The current frozen replay is retrieval-only and reuses historical
   MCP arguments; it does not measure agent behavior or shell fallback.
2. Improve BM25 coverage for `src/_pytest/config/__init__.py:1685:Config.parse`.
   In the latest current-code replay it is the sole BM25 miss; controlled hybrid
   retrieval includes it. Record candidate rank and budget effects before tuning.
3. Preserve a durable evaluation bundle outside temporary directories. Include
   runner/analyzer code, frozen cases, dependency versions, source/tool revisions,
   configuration, raw traces, and checksums. Pass when a fresh checkout can
   reproduce the analysis without the original workspace or `/tmp` package.
4. Repeat comparisons across repositories and runs. Report setup/indexing,
   embedding generation, cold queries, warm queries, and full-session costs
   separately, including variability and transport failures. Do not change the
   default mode based only on the current four-question comparison.

Use `.venv/bin/python` for commands in this checkout. After implementation
changes, run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m codemanager export --repo .
.venv/bin/python -m codemanager check --repo .
.venv/bin/python -m codemanager check-length --repo .
```

## Current implementation

- `get_context` remains the only advertised MCP code-retrieval tool. Legacy MCP
  `search_code` / `retrieve_nodes` names remain rejected; legacy CLI/library
  operations still exist. Do not restore the old MCP tools merely for expansion.
- `budget` is the canonical request/response parameter. `budget_bytes` is a
  compatibility input alias; conflicting values fail. Unknown arguments such as
  `repo` and `max_bytes` produce actionable errors. MCP is repository-bound.
- Selection preserves query matches alongside explicit symbols, sizes omitted
  node limits to the byte budget, ranks related bodies against the question,
  distinguishes implementation-plus-tests from test-only questions, suggests
  current names for missing symbols, and focuses large requested classes on
  matching members. Explicit `limit` values remain hard maxima.
- If a query contains code-shaped identifiers but no explicit `symbols` list,
  selection promotes the first identifier that resolves to one function. It
  skips ambiguous names and fuzzy suggestions. Supplying `symbols` disables this
  fallback so an explicit symbol set is not crowded by extra query identifiers.
  The MCP tool description asks callers to pass exact query identifiers through
  `symbols` as well as keeping natural language in `query`.
- Indexed AST-checked call-name candidates supplement graph edges for attribute
  calls. These are hints, not resolved receiver types or proven call paths.
- Calls resolved from test files use `test_calls_candidate` edges; test-focused
  queries prioritize these linked tests without broad test-name matching.
- Responses of at least 1,000 bytes include an explicit `context_token`. Pass it
  into follow-ups to skip previously emitted source. Receipts are immutable,
  retain the newest 128 entries, and validate each file's indexed digest. Changed
  files become eligible again. `include_seen: true` forces rereading. Partial
  continuations and source citations are preserved. Shell reads are not deduped.
- BM25-only is available through `--bm25-only`, MCP `bm25_only: true`, or
  `mode: "bm25"`. A server started with `--bm25-only` rejects embedding-backed
  overrides. The prompt hook now follows the configured mode rather than forcing
  hybrid. Request/CLI mode overrides environment, saved mode, then hybrid default.
- Hybrid caches decoded vectors in a NumPy matrix (stdlib fallback), batches query
  embeddings, and reuses synchronization across a request. SQLite generation
  counters track source/vector changes separately from file-stat bookkeeping.
- Content-digest reuse preserves vectors after line-number/node-ID shifts.
  Orphan reuse is bounded and model-specific. Regeneration remains transactional.
  `embed --progress` prints completed/total counts to stderr.

Useful commands, using the installed environment:

```bash
.venv/bin/python -m codemanager retrieval-mode --repo .
.venv/bin/python -m codemanager retrieval-mode --repo . --set bm25
.venv/bin/python -m codemanager context --repo . --query 'QUESTION' --bm25-only
.venv/bin/python -m codemanager retrieval-mode --repo . --set hybrid
```

The saved mode was not changed for this checkout; its effective default is hybrid.
Local `.codex/` hook configuration is intentionally untracked. Hook installation
for other clones is documented in README; do not accidentally commit this local
configuration with implementation changes.

## Latest frozen pytest replay with current code

The replay uses the real pytest source at commit
`36eed6b7b0eb59760fc4b33e90bdaac5057388f0` and the current working-tree
CodeManager package. It replays the recorded hook question and historical MCP
arguments; it is not a new Codex session and does not count shell output.

| Mode | Exact | Discovery | Trace | Edge | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Hybrid | 4/4 | 5/5 | 4/4 | 4/4 | **17/17** |
| BM25 | 4/4 | 5/5 | 4/4 | **3/4** | **16/17** |

Hybrid now retrieves `call_fixture_func` from the code-shaped identifier in the
trace query, even when the caller omitted `symbols`. BM25's remaining miss is
`Config.parse`. Hybrid output was 96,871 bytes across the replay, 746 bytes more
than before identifier promotion. BM25 output remained 86,227 bytes. The
identifier fallback runs only when no explicit symbols were supplied; broad
promotion regressed BM25 discovery and was discarded.

Reproduce with:

```bash
.venv/bin/python .codemanager/retrieval-replay-d140969/replay.py updated
```

## Earlier actual Codex comparison (before query-symbol promotion)

Used the real pytest repository at commit
`36eed6b7b0eb59760fc4b33e90bdaac5057388f0`: 274 Python files, 117,613 lines.
The earlier synthetic checkout was removed and was not used in reported results.

Twelve transport-clean fresh sessions: four questions × default/hybrid/BM25,
with frozen source and retrieval package, matched model/reasoning settings,
balanced arm ordering, real hook output and native MCP. Shell fallback remained
available. Questions cover keyword selection, capture phases/failure, fixture
teardown, and plugin autoload/exclusions. Saved real vectors were warmed.

Totals across four questions per arm (decimal kB; tokens are cumulative, not peak
context-window occupancy):

| Arm | Tool calls | Output bytes | Input tokens | Seconds |
| --- | ---: | ---: | ---: | ---: |
| Default Codex | 19 | 243,064 | 553,069 | 178.111 |
| Updated hybrid | 17 | 213,415 | 469,950 | 190.087 |
| BM25-only | 21 | 228,240 | 504,958 | 189.636 |

Hybrid used 10.5% fewer calls, 12.2% less output, and 15.0% fewer cumulative input
tokens than default, but took 6.7% longer. BM25 and hybrid were essentially tied
on elapsed time. Do not claim a universal speed improvement from this small set.

In those earlier actual sessions, all arms eventually retrieved 17/17 required
implementation units and passed 16/16 factual checks per arm. Hook plus MCP
alone covered **14/17 for hybrid** and **16/17 for BM25**; shell reads supplied
remaining evidence. These figures predate the latest query-symbol change and
must not be compared as if they were fresh sessions on current code. Every
scored MCP call used the context token, with no mode contamination or failed
retrieval calls.

Source-information audit, using unique source characters:

| Arm | Judged relevant | Unrelated | Uncertain | Repeated exposure |
| --- | ---: | ---: | ---: | ---: |
| Default | 61.95% | 14.80% | 23.24% | 7.93% |
| Hybrid | 68.11% | 14.70% | 17.19% | 9.06% |
| BM25 | 68.44% | 12.29% | 19.27% | 9.27% |

Relevant includes preregistered core/support plus audited useful extras. Beyond
core, unique source was 160,360 characters for default, 115,916 for hybrid, and
123,829 for BM25. Extra is not synonymous with waste. Important unique source /
all source exposure was 57.04%, 61.94%, and 62.10%, respectively.

The audit pooled 724 extra source units without arm labels. It is a same-assistant
function/block relevance judgment, not independent, perfectly blinded, or a
minimum-necessary-token measurement. Uncertain source remains separate. Citation
coordinates passed; exhaustive semantic validation of every citation is not claimed.

One BM25 run included DNS/WebSocket/HTTPS reconnection delays (168.636 seconds).
It was retained and retried using the first transport-clean run, not the fastest.
This retry rule was disclosed after observing the outage. Initial runner config
failures happened before question sessions and are archived separately.

Supplemental backend diagnostics (not substitutes for actual Codex results):
first hybrid query 1,540 ms, repeated hybrid 92 ms, two-query batch 183 ms, BM25
40 ms, exact symbol lookup 5 ms. Previous first-use pytest embedding generation
required about 45 minutes for 12,459 passages; this comparison reused all vectors.
BM25 avoids model initialization and document/query embedding costs entirely.

## Artifacts and next session

Raw artifacts are local and Git-ignored; they survive a new session in this same
workspace, but will not accompany a new clone:

- `.codemanager/retrieval-eval-v4/report.json` — final aggregate report.
- `.codemanager/retrieval-eval-v4/summary.csv` — per-question results.
- `cases.json`, `metrics.json` — frozen questions, references, rubric.
- `measurements.json`, `information.json`, `structured-recall.json` — source
  mapping, important/extra/repeated information, and CodeManager-only coverage.
- `.codemanager/retrieval-replay-d140969/updated.json` — current-code frozen
  replay coverage, response bytes, and latency; `updated-before-query-symbols.json`
  is the per-change comparison point.
- `extra-source-pool.json`, `extra-source-audit.json`, `quality.json` — audits.
- `*.events.jsonl`, `*.timeline.json`, `*.answer.txt`, `*.hook.json` — raw traces.
- `protocol-amendments.json`, `transport-affected/`, `invalid-*` — exclusions and
  retained infrastructure failures.
- `setup.json` identifies the frozen source/tool package under
  `/tmp/codemanager-eval-v4-122o8yeq`; temporary directories may be cleaned later.
- Earlier comparisons remain under `.codemanager/retrieval-eval-v2/` and `-v3/`.

Remaining work: run fresh Codex sessions on current code, investigate the BM25
`Config.parse` candidate miss, and avoid shell rereads. Do not increase byte
budgets blindly. The latest replay has full hybrid coverage and one BM25 miss,
but the current-code result still needs fresh-session confirmation. Larger
repeated and independent-repository evaluations are needed for reliable speed
conclusions.

If reusing the analyzer, include hook observations for both `hybrid` and `bm25`
arm names; an old `arm == 'codemanager'` condition silently omitted hook evidence.
The final v4 measurements corrected this and include hook bytes and source.

Validation at handoff: 156 unit tests passed, one optional real-model test skipped;
portable graph and file-length checks passed. Optional independent review is
currently disabled. Follow AGENTS.md for graph refresh, staged checks, and commits.
