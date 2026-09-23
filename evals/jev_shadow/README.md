# Jev citation shadow calibration

The production integration remains `shadow`. This evaluation never writes production quality
reports, citations, or job ledgers. It evaluates the exact production soft-check inputs, not the
separate claim-entailment gate. All raw artifacts belong in a private, ignored directory.

## Reproduce

Use `uv run --package paperforge-worker python -m evals.jev_shadow.run` for each command:

```text
freeze --directory .paperforge/jev-shadow-RUN --env-file /path/to/private.env [--db-host HOST]
review-draft --directory .paperforge/jev-shadow-RUN --env-file /path/to/private.env
adjudicate --directory .paperforge/jev-shadow-RUN --labels /path/to/codex-review.json
lock-labels --directory .paperforge/jev-shadow-RUN --labels /path/to/reviewed-labels.json
run --phase dev --directory .paperforge/jev-shadow-RUN --env-file /path/to/private.env
report --phase dev --directory .paperforge/jev-shadow-RUN
run --phase test --directory .paperforge/jev-shadow-RUN --env-file /path/to/private.env
report --phase test --directory .paperforge/jev-shadow-RUN
```

Freeze uses a read-only repeatable-read DB transaction, latest documents, current-section usages,
selected library entries and the shared production evidence selection. Complete production batches
(up to 40 occurrences) are retained, so a 400-unique-pair target can be exceeded. Projects are split
between development and holdout; duplicate text across projects is excluded. Snapshot, pair,
corpus and locked annotation hashes make data changes detectable.

The blind-review draft is **not gold**. The annotator sees only context/evidence, never evaluated
answers. Codex adjudicates uncertain items and a fixed sample before locking labels. Record actual
review coverage and annotator model; a same-model reviewer and verifier are correlated. Preserve
the original annotation and the rationale for every correction. Grades 0/1 are weak, 2 is partial,
3/4 mostly/directly supported. Ambiguous labels are excluded from accuracy and their acceptance is
reported separately. Failure to determine a grade must not turn into a confident negative.

Development locks a threshold only if all qualification tests pass. Test reads that lock and
never chooses another threshold. A start marker prevents accidental holdout reruns. Failed or
budget-limited runs retain raw requests/responses and partial group artifacts; do not delete the
marker to silently retry. Create a separately named run if a corrected experiment is necessary.

## Accounting and interpretation

The runner reserves at most 500 HTTP attempts and US$5 of configured-price upper bounds across
annotation, retries, Jev and baseline. Unknown prices stop admission. Failed/unresolved calls keep
their reservation. UTF-8 request bytes plus a 4096-token envelope and output budget give a
conservative text-token estimate. This is not provider-invoice reconciliation. Only one process
may use a run directory at a time. Files use mode 0600; no authorization headers are recorded.

Main baseline calls use the production prompt, parser, output budget and truncation handling.
Transport retries are capped at one additional attempt for the experiment. Jev uses the production
two-second deadline and no cache. Development ablations run the old unbound questions, sizes
1/10/20/40, three repeats at size 40 and reversed order on a single frozen development group.
They are diagnostic observations, not extra independent labels or holdout tuning data.

Reports separate agreement from reference-label accuracy, per-item from whole-batch coverage,
and actual shadow calls from hypothetical fallback savings. Existing fast-path admission requires
the **entire** batch to pass. Partial acceptance alone saves no baseline call; even a smaller
fallback request could take as long. Full-job call savings require a real full-job measurement,
which this offline evaluator does not claim.

Qualification requires at least 100 accepted certain labels, a 95% Wilson error upper bound <=5%,
weak precision >=95%, selective weak recall >=80%, ambiguous acceptance <=5%, item coverage >=30%,
complete baseline observations, and conservative paired error-increase upper bounds <=2 points.
A call-reduction claim additionally needs whole-batch coverage >=20%. Missing classes/counts are
insufficient evidence, never a perfect score. Pair-level Wilson intervals do not correct for
within-project correlation. AI reference labels cannot establish independent human-level quality.

## Production changes

`jev-soft-check-v2` explicitly binds each question to its pair. The existing all-items admission
and shadow return value are retained. Events add per-occurrence scores, raw confidence and
probabilities, source-text hashes, missing reasons, model/request IDs and cache provenance; no
manuscript text is added to events. Old event consumers can ignore the new fields. No migration
or public API change is needed.

`TYPESAFE_CACHE_ENABLED=false` disables both cache reads and writes.
`TYPESAFE_MODEL_PRICES` defaults to the pinned Jev input price, output free; unknown actual model
IDs remain unpriced. Price configuration is a ledger estimate. Score/Choice parsing validates
the complete answer and rounded probability sum without renormalizing values; Score also checks
legend keys/descriptions. A valid zero confidence remains zero.

Official contract: [API](https://docs.typesafe.ai/api),
[confidence](https://docs.typesafe.ai/confidence), [pricing](https://docs.typesafe.ai/models).

## Verification

```text
uv run pytest -q packages/llm_runtime/tests services/worker/tests/test_quality_pipeline.py services/worker/tests/test_worker.py evals/jev_shadow
```

After code changes, follow repository deployment acceptance: `./scripts/dev restart`, Funnel
target verification and byte-for-byte public `/login` comparison. Keep older deployments that
may still own jobs. Production must remain in shadow after this evaluation, including if a
candidate threshold is found.
