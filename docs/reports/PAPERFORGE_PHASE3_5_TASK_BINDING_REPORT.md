# PaperForge Phase 3.5 — Explicit Task Binding

**Date:** 2026-08-09
**Phase:** 3.5 — *Wire `replace_project_task_profile` to API and UI; define a safe exit from the `all_tasks` fallback*
**Scope discipline:** no migration, no production flag change, no shadow mode, `0025` untouched
**Result:** Complete and merged

---

## 1. What Phase 3 got wrong, measured

Phase 3's report said the binding path existed but "no API route or UI calls it, so every project is
still unbound." Measuring production shows that was wrong in two ways.

**`replace_project_task_profile` *is* called** — from `pipelines/qdecomp.py`, on every QDECOMP run.
It was never fully unwired.

**One project is bound, not zero:**

```
projects total      = 29
with task profile   = 1        ← 3%
profile rows        = 1
sub-questions with a task_id = 1 / 52
```

The single bound project is the NRP/polyketide review — the one topic that matches the seeded BGC
ontology. So the mechanism works; its *coverage* is 3%, because `infer_task_id` only fires when a
sub-question contains an ontology cue, and the ontology covers exactly two domains.

The operative problem is therefore narrower and more specific than "nothing binds": **there is no way
for a user to say what their project is about**, and automatic inference can only recognise two
domains.

---

## 2. A latent data-loss bug found while reading that path

`persist_question_decomposition` called `replace_project_task_profile` unconditionally, and that
function deletes before inserting. Two consequences:

- A user's explicit binding would be silently replaced on the next pipeline run.
- More commonly, a run that **infers nothing** deletes everything and inserts nothing — wiping a
  binding a previous run had correctly established. Given that 51 of 52 sub-questions infer no task,
  this is the *normal* path, not an edge case.

Fixed in `_bind_inferred_tasks`: never touch a user-marked binding; never write when nothing was
inferred; only replace when inference actually produced tasks that differ from what is stored. Two
regression tests cover exactly these cases.

---

## 3. No migration was needed

`project_task_profile` has existed since `0011`. The one new piece of state — "did the user choose
this, or did QDECOMP infer it?" — is stored as a boolean marker in the existing
`paper_project.scope_json`, alongside the established `generator='user'` convention that already
exempts hand-edited scope from regeneration.

The binding rows themselves remain the single source of truth; the marker only answers *who* wrote
them. A dedicated column would have bought stricter typing at the cost of a migration, and migrations
should be driven by real schema needs.

Verified: **24 migration files, head still `0024_task_vocabulary`, `alembic check` clean.** `0025` is
free for Phase 4.

One consequence worth stating: "explicitly bound to the generic task" and "never bound" are already
distinguishable, because the former writes a real `generic.scholarly` row. So a user can commit to
"this is a general scholarly project" and become immune to the fallback switch — no schema support
required.

---

## 4. What shipped

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/tasks[?domain=]` | task catalogue for the picker |
| `GET /api/v1/projects/{id}/tasks` | effective task set **and where it came from** |
| `PUT /api/v1/projects/{id}/tasks` | explicit binding; empty list unbinds |

The response's `source` field is the point of the whole phase:

- `explicit` — the user chose it; the pipeline will not overwrite it
- `inferred` — QDECOMP recognised an ontology cue
- `fallback` — nothing is bound, so `TASK_PROFILE_FALLBACK` decides

When the source is `fallback`, the response also carries a plain-language `fallback_note` saying what
that actually means for extraction. Under today's `all_tasks` default it says the project is
inheriting every domain's whitelists — a fact that was previously invisible everywhere.

Validation rejects unknown slugs with `422` **before** writing, so a rejected request cannot leave a
partial binding. A test asserts the prior binding survives a rejected call.

### UI

`TaskBinding` in the scope workbench: catalogue grouped by domain, a source badge, the fallback
warning, and a save/clear pair. The generic task is mutually exclusive with specific domains —
selecting both would make "generic" meaningless.

### Operations

`paperforge-admin tasks coverage` reports bound vs unbound, breaks out user-bound, and states plainly
whether flipping the fallback is safe yet.

---

## 5. The safe exit path from `all_tasks`

The fallback is **not** changed by this phase. The sequence to retire it:

1. **Measure.** `paperforge-admin tasks coverage`. Today that reads 1/29 (3%).
2. **Extend the ontology where it is thin.** Most projects infer nothing because only two domains
   exist. `paperforge-admin tasks upsert` now makes adding a domain a data change.
3. **Bind the backlog.** Existing projects get bound through the scope workbench, or in bulk via
   `PUT /api/v1/projects/{id}/tasks`. Projects with no matching domain should bind
   `generic.scholarly` explicitly — that is a positive statement, not an absence.
4. **Re-measure.** Coverage at or near 100% means the flag affects nobody.
5. **Flip `TASK_PROFILE_FALLBACK=generic_only`** on the worker *and* the API (§7).
6. **Verify** that measurement counts and `answer_status` distribution did not move for bound
   projects; only unbound ones should change, and by then there should be none.

The gating property: **once every project is bound, the fallback is dead code and flipping it is a
no-op.** That is a far safer trigger than "we think the narrowing is fine."

---

## 6. Verification

| Check | Result |
|---|---|
| `ruff check .` | exit 0 |
| ontology lint | ok (4 files) |
| `pytest` (real PostgreSQL 16) | **843 passed, 4 skipped, 0 failed** (Phase 3: 828/4 → +15 new) |
| `vitest` | **138 passed** (Phase 3: 129 → +9 new) |
| `tsc --noEmit` | exit 0 |
| `next build` | success |
| `alembic check` | exit 0; **still 24 migrations, head `0024`** |
| mypy | **208** — identical to baseline; 0 errors in new files |

### Tests added

**9 API contract** (`services/api/tests/test_task_binding_api.py`) — catalogue listing and domain
filter; an unbound project reports `fallback` with all tasks effective and an explanatory note;
explicit binding narrows the effective set and persists across re-reads; order preserved and
deduplicated; empty list unbinds; unknown slugs rejected `422` with the prior binding intact; binding
scoped to its own project; 404 on unknown projects.

**6 Postgres-backed worker** (`services/worker/tests/test_task_binding_preservation.py`) — inference
binds an unbound project; **inferring nothing no longer wipes an existing binding**; a user binding
survives inference that disagrees; an unmarked inferred binding can still be corrected by later
inference; repeated runs are idempotent; a project that infers nothing stays unbound rather than
being bound to something arbitrary.

**9 UI** (`apps/web/tests/task-binding.test.tsx`) — unbound state states the consequence rather than
silently inheriting; bound state shows provenance and drops the warning; `inferred` and `explicit`
are visually distinct; save disabled until dirty; submits the selection and reflects the server's
response; generic/specific mutual exclusion in both directions; clearing submits an empty list;
**a failed save keeps the user's selection instead of pretending to succeed**.

---

## 7. Residuals

**The flag lives in two places.** `TASK_PROFILE_FALLBACK` is now read by both `WorkerSettings` (which
governs actual extraction) and the API `Settings` (which reports the effective set to the UI). They
must be configured identically or the UI will describe a task set the worker does not use. Both
`.env` examples document it, but nothing enforces agreement at runtime — worth a startup check if
this pattern recurs.

**No bulk-binding tool.** Backlog binding is per-project. With 28 unbound projects that is tolerable;
at a larger scale step 3 above wants a `paperforge-admin tasks bind --project … --task …` or a
suggestion pass that proposes a task per project from its scope.

**Inference remains two-domain.** Nothing here widens `infer_task_id`; it widens who can *correct*
it. Coverage will stay low until the ontology grows, which is now a data change rather than a code
change.

**`dimension_schema_json` is still unread** — carried over from Phase 3, belongs with the extractor.

---

## 8. Rollback

1. Revert the phase commit. The endpoints and the UI section disappear; `project_task_profile` rows
   written through them remain valid and continue to be honoured by `list_project_task_specs`.
2. No migration to reverse.
3. `qdecomp`'s non-destructive behaviour reverts with the commit. Note that reverting *reinstates* the
   wipe-on-no-inference bug, so prefer keeping it even if the API and UI are rolled back.
4. Bindings created by users can be cleared individually with `PUT … {"task_ids": []}`, or in SQL:
   `DELETE FROM project_task_profile WHERE project_id = …`.

---

## 9. Next

Phase 4 (bounded cross-study synthesis) is unblocked and `0025` is available. Before starting it,
consider the coverage work in §5 — Phase 4's synthesis quality depends on `comparability_key`, which
depends on the dimensions Phase 2 extracts, which depend on the vocabulary the task binding selects.
A project bound to the wrong domain will produce confidently wrong comparability.
