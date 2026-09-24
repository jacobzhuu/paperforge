# Evaluator trust regressions

This suite replays evaluator inputs only. It does not run writers, change production settings,
write the database, or decide the A/B/C release gate. Model calls require explicit `--live --budget-file /private/budget.sqlite --price-currency CNY`.

```bash
uv run python -m evals.evaluator_trust.run --live \
  --budget-file /private/budget.sqlite --price-currency CNY --output /path/to/new-output-directory
uv run python -m evals.evaluator_trust.manuscripts --live \
  --budget-file /private/budget.sqlite --price-currency CNY \
  --source .paperforge/agent-benchmark-20260916 \
  --output /path/to/another-new-directory
```

Use the normal WorkerSettings model/provider environment and reachable shared limiter. Outputs
must not already exist. Exact prompts/inputs go to `objects/`; `trace.jsonl` and
`llm_call_log.jsonl` retain judgments, usage and failures. Targeted replay records source hashes.
The runners have no database session factory. Avoid publishing private source evidence or provider logs.

`cases.json` contains 12 AI-adjudicated cases extracted from frozen manuscripts, including three
controlled corrections. The model sees anonymous section labels, original claims and their complete
bound EvidenceUnits; it does not see expected labels. This is targeted regression, **not independent
human blind review or a validated accuracy benchmark**. Two CUB shot-assignment cases have ambiguous
source wording and must not be treated as unambiguous gold without better source evidence.

Claim-level correctness and whole-section acceptance are different. `claim_outcome` measures the
former; `section_acceptable` retains the unchanged production criterion. A factually supported
single sentence can still fail a whole-section synthesis requirement. An honest gap can be correct
without answering the full question. Neither case authorizes weakening the production criterion.

Historical validation in `.paperforge/evaluator-trust-validation-20260917/` retains both initial
and subsequent failures. Its early `actual`/`passed` fields used whole-section acceptance; see the
repair report for the correction. Historical files are not overwritten. Historical input hashes in
`targeted*/results.json` predate deterministic input enrichment; the exact authoritative hashes are
in each `review.input` trace and corresponding stored request. The current runner hashes the enriched
input first.

`manuscript_review.py` is an **offline experiment**. All nine initial manuscript responses failed
exact quote verification. It is deliberately not imported by the production worker and is not a
new delivery gate. Its findings must not be counted as verified hard violations.
