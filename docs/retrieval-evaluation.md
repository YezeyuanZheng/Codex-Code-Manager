# Retrieval correctness and information efficiency

Use actual fresh Codex sessions for comparisons, allowing the default arm to
choose its own searches. Run BM25-only and hybrid arms with the same pinned source,
model/reasoning, questions, budgets, and guidance. Count prompt-hook context,
failed calls, MCP calls, shell output, and follow-up source reads. Report first-use
index/embedding setup separately from saved-cache latency. Balance arm order.

Freeze required implementation units and critical source lines before runs. A
unit is recalled only when all critical lines appear in the retrieved evidence.
Report coverage from the hook, hook plus MCP, and all sources separately: shell
fallback must not conceal gaps in CodeManager retrieval. Check final factual
claims against source, and report citation coordinates separately from semantic
citation support. Answers and source mapping are retained for inspection.

For each question, classify retrieved source into disjoint categories:

- Core: the implementation required to answer the question.
- Supporting: relevant callers, callees, configuration, and tests beyond the core.
- Audited useful extra: additional relevant source established by a source audit.
- Audited unrelated: source that does not help answer this question.
- Uncertain extra: off-reference source not yet resolved by an audit.

Freeze core/supporting references before runs. Audit extra regions from a pooled
list of source locations without displaying which arm retrieved them, preserving
the original references and recording audit additions separately. Do not silently
call all off-reference code irrelevant, or grade importance from answer length.
The same assistant's audit is not independent or fully blinded; disclose that.

`codemanager.retrieval_metrics.information_metrics` measures source-character
ranges. Every emitted occurrence counts toward exposure; source positions are
unioned for unique coverage. Metadata and unmapped output bytes remain separate.

- Important density = (core + supporting + audited useful extra) / unique source.
- Effective information fraction = important unique source / all source exposure.
- Repeated exposure = all emitted source characters minus unique source characters.
- Extra beyond core includes supporting source; it is not synonymous with waste.
- Unrelated and uncertain fractions are reported separately.

Overlapping reference regions count only once. Category priority is core,
supporting, useful extra, unrelated, then uncertain. Also report total output bytes,
cumulative model input tokens (including cached tokens), tool calls, time to core
evidence, and elapsed time. Cumulative input is not peak context-window occupancy.
A smaller single response does not establish lower cumulative context cost.
