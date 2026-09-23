# PaperForge writing Agent upgrade

The implementation keeps ARQ jobs, deployment-scoped queues, PostgreSQL execution fences,
the existing Evidence/PaperIR model and the current final quality gates. It adds no Agent
framework, vector store, MCP server or cross-worker child jobs. Migration
`0031_writing_context` is additive, so draining workers can finish against the same database.

## Execution and recovery

`WRITER_EXECUTION_MODE` accepts `legacy` (default), `dag_serial`, and `dag_parallel`.
Parallel mode defaults to two writers per job (`WRITER_CONCURRENCY=2`) and still passes through
the shared LLM concurrency/RPM/TPM limits. It is **not enabled on the public deployment** until
the real-model A/B/C evaluation passes. `legacy` names the rolling-context scheduler, not a
promise to reproduce every historic prompt: the frame-context and grounding fixes apply to
all new code paths.

The outline carries explicit `depends_on: [section_key, ...]` or `independent: true` contracts.
Missing contracts retain a conservative previous-body dependency. Invalid keys/cycles fail
before generation. Question-bundle sections built by the existing outline pipeline declare
independence; their subsections depend on the parent; the review synthesis depends on all
body arguments. Display order and title nesting do not imply concurrency. Frames always
depend on the complete body.

Each writer gets a frozen glossary, its Evidence whitelist and only declared dependency
summaries. The coordinator accepts results in outline order, preserving deterministic term
ownership and event order. A failed dependency pauses downstream generation. Graceful pause
stops admission and drains admitted calls. A hard process kill can still lose a provider
response that has not reached a committed node: external calls are not exactly-once.

The document stores the versioned graph/context identity; sections store terms, input identity,
provenance references and generation metadata. Node completion and section output commit in
one transaction. On resume, matching completed nodes are reused; changed inputs invalidate
their descendants and polish checkpoints. Before accepting a DAG result, source identity is
checked again. Every generated section update has an optimistic body guard; edits detected
after generation began are preserved and pause execution. A changed persisted DAG manuscript
requires an explicit rebuild instead of silently overwriting the author's edits.

Old checkpoint/document records without the mode/state remain on the serial compatibility
path. The deployment script retains older workers/Redis databases with in-flight jobs.

## Integration, evidence and memory

Body recovery and coherence editing run before frame generation. Integration supplies full
body findings, canonical terminology, term conflicts and duplicate-paragraph locations to
the existing editor; it does not introduce a second whole-paper writer. The abstract,
introduction and conclusion receive the full body findings rather than the preceding-two
section window. Repair refreshes frames after body repairs. Final quality reports reject a
frame bound to a different body snapshot in scholarly/submission mode; draft mode reports it
as a warning. The existing final semantic/grounding evaluator remains authoritative.

Coherence requests carry sentence IDs. Each returned sentence must preserve its identity,
order, citation/evidence/source bindings and numeric values; otherwise the original draft is
kept. This structural check does not establish entailment after paraphrasing: the final
snapshot-bound evidence verifier is still required.

Memory remains in its domain stores: working state in document/section metadata and checkpoint,
episodes in events/repair history, semantic facts in project Evidence and synthesis, artifacts
in existing IR/object storage. No automatic cross-project experience sharing is introduced.
Synthesis cache identities now include model, prompt, language and policy. Verification cache
reads/writes are project-scoped and cascade on project deletion. Historic NULL-project cache
rows remain for draining workers and are never read/copied by the new worker path; their
retention is a separate maintenance decision, not a destructive migration.

## Traces and budgets

Stage/tool, coordinator, writer, evaluator and repair spans share trace/parent/node/attempt
metadata through task-local context. Events and `llm_call_log` are the persistent sources;
call records are written before returning model results, with a deferred final retry on a
transient ledger failure. Prompt hashes are recorded; prompt bodies are not added to traces.
Runtime metrics distinguish local queue wait, shared provider-slot wait and rate-budget wait
when those limiters are configured. Model latency includes provider work/retries; it is not
an isolated network RTT. Unknown usage/pricing remains unknown.

Before each model invocation, all writing, verification and repair loops reserve from the
same durable job budget. Defaults are 2,000 model invocations, 50,000,000 conservatively
reserved tokens, and a 7,200-second wall deadline from the first reservation. Configure with
`AGENT_MAX_CALLS`, `AGENT_MAX_RESERVED_TOKENS`, `AGENT_MAX_SECONDS`. Truncation retries reserve
again; HTTP transport retries remain metered by the provider limiter, not this invocation
counter. UTF-8 bytes plus output allowance are an admission estimate, not billing usage.
Ambiguous reservations are not refunded, and resume does not reset budget/deadline. Exhaustion
pauses at a safe boundary; an exhausted task needs a larger configured allowance to continue.
These are per-task limits, not daily user quotas.

Trace export is read-only and follows the trace across resumed job IDs in the same project:

```bash
uv run python -m evals.agent_writing.trace --job-id UUID --output trace.json
```

Unfinished spans and reservations without call logs are reported explicitly as ambiguous.

## Evaluation and release

The regression suite uses controlled writers to verify serial/parallel input identity,
actual overlap, persistence, pause/drain/resume, source changes, human-edit conflicts,
sentence provenance, frame freshness, scoped cache deletion, budget inheritance and trace
parentage. The archived document recording retains its historical frame-context adapter to
test IR assembly; it is not represented as a real-model evaluation of the new prompt.

For a paid model comparison, restore a representative project with its outline, selected
library, evidence and assets into an **isolated database ending in `_eval`**, with current
migrations. Do not run any API/Worker against that evaluation database during the experiment.
The runner creates new documents/jobs only there. The live flag is mandatory:

```bash
PAPERFORGE_EVAL_DATABASE_URL=postgresql+asyncpg://.../paperforge_eval \
  uv run python -m evals.agent_writing.run \
  --project-id UUID --repeats 3 --cache-mode cold --live --output results/cold.json
uv run python -m evals.agent_writing.report results/cold.json
```

A is the rolling-context serial scheduler; B is the frozen-context DAG at width 1; C is the
same DAG at width 2. Order rotates between repeats. Run cold and warm experiments separately;
each condition begins with the same captured verification-cache state. Model roles, retries,
thinking configuration, evidence and budget are held fixed. Reports include writing time,
total writing+evaluation time, token/cost coverage, semantic verdicts and quality blockers;
generated Markdown is saved for blind review. Retrieval/replanning are not part of this
benchmark, to avoid changing the shared evidence between conditions. It does not measure
full-job capacity, renderer latency, or multi-user fairness.

Attach independently obtained `expert_scores` for organization/evidence/consistency (1–5)
and set `expert_verified` only after blind review. Missing expert scores or missing token
usage cannot pass the gate. The preliminary gate requires paired repeats, no additional
hard-violation counts, each quality dimension within 0.2 points, at least 15% writing-time
reduction from B to C, and at most 10% token growth. Meeting it on a small fixture set is not
a claim about all topics or production load. Existing review_depth annotation placeholders
are not human gold.

No paid model comparison or capacity soak is claimed by this change. Parallel mode stays
off until real evidence satisfies the gate. Cross-worker subjobs and MCP remain deferred.
Deployment acceptance is `./scripts/dev restart`, including the Funnel target check and
byte-for-byte public `/login` comparison. Never stop an older deployment with in-flight jobs.
