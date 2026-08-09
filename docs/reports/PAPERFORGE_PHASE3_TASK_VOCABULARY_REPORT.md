# PaperForge Phase 3 — Task-Configurable Domain Vocabulary

**Date:** 2026-08-09
**Phase:** 3 of 5 — *Task-configurable domain vocabulary* (audit P0-5 / §4.6, corrections C-3/C-5/C-6)
**Plan:** `docs/plans/PAPERFORGE_P0_REMEDIATION_PLAN.md` §Phase 3
**Shipped state:** `TASK_PROFILE_FALLBACK=all_tasks` — **no behaviour change for existing projects**
**Result:** Complete and merged

---

## 1. What this fixes

Three related defects, all consequences of domain knowledge living in Python instead of data:

1. **Hardcoded vocabulary.** `evidence.py` carried five literal lists — model families, input
   representations, pretrained backbones, split strategies, task variants — covering exactly two
   domains (BGC protein modelling and sequential recommendation). For a chemistry, clinical or
   materials paper these fields were either empty or **wrong**: any paper mentioning "BERT" in its
   related-work section got `pretrained_backbone=BERT`.

2. **A "universal" metric list that wasn't.** `tasks.py`'s `universal` list smuggled in `tanimoto`
   (cheminformatics), `asr`/`er@k` (attack), and `ndcg`/`hr`/`mrr`/`map` (recommendation). A clinical
   paper's "HR" (hazard ratio) was extracted as recsys Hit Rate.

3. **The leak mechanism itself** (audit correction C-6). `list_project_task_specs` returns **all**
   task definitions when a project has no `ProjectTaskProfile` — and no route ever creates those
   rows. So in practice *every* project inherited both domains' whitelists. The audit named the
   symptom without identifying this fallback as the cause.

Plus the `baselines` field, which matched `[A-Z][A-Za-z0-9+-]{2,24}` — i.e. any capitalised word —
and stored `The`, `We`, `Table` and author surnames as a paper's "baseline methods".

---

## 2. Files changed

| Path | Change |
|---|---|
| **new** `packages/db/migrations/versions/0024_task_vocabulary.py` | `vocabulary_json` column, backfill for 13 tasks, `generic.scholarly` row |
| `packages/db/db/models/paper.py` | `TaskDefinition.vocabulary_json` |
| `packages/db/db/repositories/tasks.py` | `TaskVocabulary`, `vocabulary_for_tasks`, universal-metric cleanup, single metric alternation, fallback parameter, authoring helpers |
| `services/worker/paperforge_worker/pipelines/evidence.py` | five literal lists → ontology lookups; `baselines` deleted; two duplicate metric lists consolidated |
| `services/worker/paperforge_worker/config.py` | `TASK_PROFILE_FALLBACK` |
| `services/api/paperforge_api/admin.py` | `paperforge-admin tasks {list,export,upsert}` |
| **new** `packages/db/db/seeds/task_definitions.json` | canonical committed ontology (14 tasks) |
| `scripts/ontology_literal_lint.py` | broadened beyond dataset names; 4 target files; documented allow-list |
| `.github/workflows/ci.yml` | **ontology lint is now a required CI step** |
| **new** `services/worker/tests/test_task_vocabulary.py` | 19 tests |
| **new** `packages/db/tests/test_task_ontology_contracts.py` | 6 Postgres-backed tests |
| `services/worker/tests/test_pipelines.py` | one test updated to the new metric semantics (§6) |
| `.env.example`, `.env.production.example` | operator documentation |

---

## 3. The vocabulary contract

`TaskDefinition.vocabulary_json`:

```json
{
  "model_families":        ["CNN", "SASRec"],
  "input_representations": ["item sequence"],
  "pretrained_backbones":  ["ESM2", "RoBERTa"],
  "split_strategies":      [{"cue": "leave-one-out", "name": "leave-one-out"}],
  "task_variants":         [{"cue": "multi-class",   "name": "multi-class"}]
}
```

**Semantics: "what this task's papers are expected to report"** — not "what exists in the world".
That is why the shared ML architectures (CNN, LSTM, Transformer, …) are declared in *both* the bgc
and recsys vocabularies rather than hoisted into `generic.scholarly`. A BGC paper reporting a CNN is
expected; a generic scholarly project declares nothing, so those fields stay empty — honest ("we
don't know what to look for") rather than wrong ("we found BERT because the word appears").

`split_strategies` and `task_variants` are ordered `(cue, name)` pairs because they replace an
if-chain where **order was priority**: `cluster-based` had to beat `random split`. The migration
preserves that exact order, and a test asserts it.

Backbones keep a version-suffix affordance (`ESM2` in the vocabulary still matches `ESM2-650M` in
text), reproducing the original `ESM2[- ]?\d+[A-Z]?` without requiring hand-authored JSON to contain
regex fragments.

---

## 4. Migration `0024`

- Adds `vocabulary_json`.
- Backfills **13** existing tasks (7 `bgc` + 6 `recsys_attack`) from the literals that were in
  `evidence.py`, partitioned by domain. *The plan said 7 tasks; the real ontology has 13.*
- Inserts `generic.scholarly`: empty vocabulary, empty datasets, empty metric whitelist,
  `dimension_schema_json = ["dataset","metric_name","split"]`.
- Leaves any future domain with `{}` rather than `NULL`, so readers need not distinguish.

Downgrade drops the column and the generic row; round-trip verified.

---

## 5. Authoring path

Before this phase, the only way to write `task_definition` was to add an Alembic migration — so
"configurable task definitions" was not true (audit correction C-5).

```bash
paperforge-admin tasks list [--domain D]
paperforge-admin tasks export --output tasks.json
paperforge-admin tasks upsert --file tasks.json [--dry-run]
```

`upsert` validates everything **before** writing anything — a half-applied ontology would leave the
evidence stage reading an inconsistent vocabulary. It **never deletes**: the ontology is shared, so a
partial file must not silently remove someone else's tasks. `--dry-run` prints the add/change diff.

The canonical state is committed at `packages/db/db/seeds/task_definitions.json` and
`export → upsert → export` is verified byte-identical, both by hand and by a test.

### A validation rule I had to relax

The plan specified "no duplicate cues across tasks in a domain" as an error. Running it against the
real ontology produced **43 errors** — the shipped recsys tasks deliberately share cues like
`sequential recommendation`, and `infer_task_id` scores by cue *count* and longest-cue length, so
sharing is meaningful rather than broken. Blocking on it would have rejected the project's own data.

Downgraded to `task_ontology_warnings()`: printed on upsert, never blocking. The current ontology
emits 9 such notices.

---

## 6. Behaviour changes

Two are intentional and user-visible in extracted data.

**(a) Domain metrics now require the ontology.** `NDCG@10`, `ASR`, `Tanimoto` etc. are no longer in
the universal core. Without a task declaring them, they are not extracted. `test_pipelines.py` had a
case asserting `nDCG@10` extraction *with no ontology*; it was rewritten to assert both directions —
absent without the ontology, present with it. That test was encoding the bug.

**(b) Out-of-domain fields stay empty.** A chemistry paper under `generic.scholarly` now yields
`model_families == []` and `pretrained_backbone is None` instead of a false `BERT`. Generic training
hyperparameters (`optimizer`, `learning_rate`, `batch_size`, `epochs`, `regularization`) are
unaffected — they are genuinely cross-disciplinary and stay in code.

**`TASK_PROFILE_FALLBACK` ships as `all_tasks`**, so nothing changes for existing projects until an
operator flips it. Both fallbacks have Postgres-backed tests, including the leak the flag fixes.

---

## 7. The lint now earns its keep

`ontology_literal_lint.py` existed since before the audit and had **never been invoked** — no CI
step, no script, no test. It also only covered *dataset names*, which is why the model/backbone/split
literals survived in the very file it was meant to guard.

Now: 4 target files, broadened `FORBIDDEN` (datasets + domain model families + backbones +
representations + split strategies + domain metrics), comment lines skipped, a documented `ALLOWED`
exemption list, and **a required CI step**.

It immediately paid for itself: on first run it caught two `tanimoto` occurrences I had missed —
including a **third** copy of the metric list inside `_metric_from_header`, which parses markdown
table headers and is on the main extraction path. All three metric lists are now one
`metric_alternation()` shared by prose and header matching.

---

## 8. Verification

| Check | Result |
|---|---|
| `ruff check .` | exit 0 |
| **ontology lint** | ok (4 files) — now blocking in CI |
| `pytest` (real PostgreSQL 16) | **828 passed, 4 skipped, 0 failed** (Phase 2: 802/4 → +26 new) |
| `vitest` / `tsc` | 129 passed / exit 0 |
| `alembic upgrade head` from empty | 24 migrations |
| `alembic check` | exit 0 |
| `alembic downgrade -1` → re-upgrade | exit 0, drift clean |
| mypy | **208** — identical to the Phase 2 baseline; **0** errors in new files |
| CLI round-trip | `export → upsert → export` byte-identical |

### Tests added

**19 unit** (`test_task_vocabulary.py`) — equivalence of the refactor against the previous hardcoded
behaviour (model families, representations, backbone incl. version suffix, split priority order,
task variant); empty-vocabulary emptiness; `baselines` gone; merge ordering and dedup; malformed
vocabulary degradation; universal core excludes domain metrics; seed-file validity and round-trip;
validation rejections; shared cues are warnings; the lint passes on the tree, fails on a
reintroduced literal, and ignores literals in comments.

**6 Postgres-backed** (`test_task_ontology_contracts.py`) — `all_tasks` leaks every domain (recorded
explicitly, since that is the current default); `generic_only` stops it and yields a `None` dataset
pattern; an explicit profile beats either fallback; `generic_only` degrades safely when the row is
absent; upsert is idempotent and never deletes; vocabulary round-trips through the database.

---

## 9. Residuals

**`BPR` in the loss-function list.** `_structured_payload`'s `loss_function` regex is otherwise
generic (`cross-entropy`, `focal loss`, `mean squared error`) but includes `BPR`, which is
recommendation-specific. Adding a sixth vocabulary category for one term was not worth it; it is a
documented entry in the lint's `ALLOWED` list. Fold it in whenever loss functions are ontologised.

**`dimension_schema_json` is still unread.** Correction C-4 identified it as the natural home for the
Phase 2 extractor's per-task dimension contract. Phase 3 populates it for `generic.scholarly` but no
code consumes it yet — that belongs with the extractor work, not here.

**`TASK_PROFILE_FALLBACK` is not yet flipped.** Doing so narrows matching for every unbound project.
The plan calls for measuring measurement counts and `answer_status` distribution on a sample first.
Realistically the better fix is to *bind* projects to tasks — `replace_project_task_profile` exists
and is now reachable via the CLI, but no API route or UI calls it, so every project is still unbound.
Worth its own small piece of work.

---

## 10. Rollback

1. `TASK_PROFILE_FALLBACK=all_tasks` (already the default) — pure config.
2. Revert the phase commit; `0024`'s column becomes unread, no data loss.
3. `alembic downgrade 0023_experiment_extraction` — verified round-trip; drops the column and the
   generic row.

Note that reverting the code while keeping the migration is safe: `_vocabulary_from_row` tolerates a
missing attribute, and the old hardcoded regexes would simply come back.

---

## 11. Next

Phase 4 — bounded, evidence-constrained cross-study synthesis (migration `0025`). With Phase 2
filling `task`/`dataset`/`split` and Phase 3 making that vocabulary correct per domain,
`comparability_key` can finally group results across papers, which is the input Phase 4's synthesis
pass needs.
