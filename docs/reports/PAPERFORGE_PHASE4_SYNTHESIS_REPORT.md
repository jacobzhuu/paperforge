# PaperForge Phase 4 — Bounded, Evidence-Constrained Cross-Study Synthesis

**Date:** 2026-08-12
**Phase:** 4 — *Replace stance-counting with reasoning, without granting the model new authority*
**Migration:** `0025_question_synthesis`
**Flag:** `SYNTHESIS_LLM_ENABLED=false` (off in production)
**Result:** Complete and merged; not yet enabled anywhere

---

## 1. The problem this closes

Deterministic SYNTH classifies a comparable cluster by counting stances:

```python
if "supports" in stances and "contradicts" in stances:   classification = "conflicting"
elif "conditional" in stances or len(conditions) > 1:    classification = "conditional"
elif stances <= {"supports"}:                            classification = "consistent"
```

That answers *whether* studies disagree. It cannot answer **under what conditions a finding holds**,
or **why** two studies disagree — so `_synthesis_argument_points` can only hand the writer a generic
instruction ("state unresolved conflict under the same comparable setting"), and the prose comes back
as a list of papers.

Phase 4 adds the missing reasoning step as an **enrichment**, and gives it no new powers.

---

## 2. What the model is and is not allowed to do

| | |
|---|---|
| **Can** | produce a `claim`, and `agreement` / `conditional` / `conflict` / `gap` entries, each bound to `EVIDENCE_ID`s from that same bundle |
| **Cannot** | write `answer_status`, change evidence membership, change `comparison_clusters`, write a citation, or introduce a number |

`answer_status` is written before enrichment runs and is never read back. `readiness.py` and the
pre-write gate therefore see **identical inputs whether the flag is on or off** — which is the
property that makes this safe to enable on a live deployment.

### The five hard filters

Each is a discard, not a repair — the `qmatrix._normalize_links` discipline. A repaired entry looks
bound but isn't, which is worse than no entry.

| # | Rule | Failure it prevents |
|---|---|---|
| 1 | every `evidence_id` must be in this bundle; an entry left with none is dropped | cross-question contamination — an entry that cites another sub-question's evidence |
| 2 | a `conflict` must name a real cluster, cite only ids **inside** it, and span ≥2 `work_id` | asserting disagreement between studies that measured different things |
| 3 | `conditional.dimension` ∈ task ontology `dimension_schema_json` ∪ core set | free-text "it depends" with no comparable axis |
| 4 | entries citing only `D_abstract_only` are demoted to `gap` with a note | abstract-level claims laundered into synthesis conclusions |
| 5 | every number must be traceable to a cited unit's text or measurements | fabricated effect sizes |

---

## 3. Three adjustments to the plan, and why

### 3.1 Rule 2 is stricter than specified

The plan required "≥2 distinct `work_id` **and** a shared `comparability_key` present in the bundle's
`comparison_clusters`." Implemented literally, a response can name a genuine cluster key while citing
evidence from a *different* cluster: the key validates, the work count validates, and the assertion is
still a comparison across incomparable studies — the exact error the whole mechanism exists to prevent.

The implementation additionally requires `set(cited) ⊆ cluster.evidence_ids`. Both the loose failure
(different keys) and the tight one (right key, wrong evidence) have rejecting tests.

### 3.2 Rule 5 reuses NUMLINT rather than `numbers.issubset(values)`

The plan said to reuse `writing.extract_numbers` with the `numbers.issubset(values)` pattern. Applied
to synthesis statements that rule is too blunt: it rejects any sentence containing a year, a table
number, a section number, or a version string. *"Table 2 summarises the 2020 benchmark"* would be
discarded, and the model's most natural phrasing would be systematically destroyed.

`ingest.numlint` already solves exactly this and is the same red line enforced before export — it
carries `YEAR_RE`, `TRIVIAL_NUMBERS`, ordinal context (`图 1` / `Table 2` / `Eq. 4`) and version
identifiers as explicit exemptions. Rule 5 is therefore `build_asset_index` + `lint_text` over the
cited units, with measurement rows expanded into the source text so that values from structured
extraction count as sourced. One implementation of the number rule, not two.

### 3.3 Attachment happens on load; generation only in `synthesize_questions`

The plan put enrichment in `synthesize_questions`. But `load_synthesis_bundles` has five call sites
(outline, writing, two supplement rounds, alignment), and the writer reads bundles through those. Had
only `synthesize_questions` attached the narrative, the writer would have seen it on some paths and
not others.

Split instead:

- `load_synthesis_bundles` → **reads** stored rows and attaches them. No LLM call, ever.
- `synthesize_questions` → **generates** what is missing, persists, attaches.

### 3.4 A rejected response is remembered

The plan persists `generator='deterministic'` when validation drops everything. That row is also
treated as a negative cache: the same bundle is never sent again. Without it, every subsequent SYNTH
pass (up to 4 per job) pays again for a response already known to fail validation.

### 3.5 `dimension_schema_json` has its first reader

Audit correction **C-4** recorded that `TaskDefinition.dimension_schema_json` was seeded but read by
nothing. Rule 3 is that reader. A project bound to a task whose schema declares
`["dataset","metric_name","split"]` accepts exactly those plus the core set; anything else is rejected
as `unknown_dimension`.

---

## 4. Schema — `0025_question_synthesis`

```
question_synthesis
  id, research_question_id → research_question ON DELETE CASCADE
  project_id               → paper_project     ON DELETE CASCADE
  generator      'llm:<model>' | 'deterministic'
  bundle_hash    sha256 of the deterministic bundle
  claim, agreement_json, conditional_json, conflict_json, gap_json
  created_at
UNIQUE (research_question_id, bundle_hash)
```

`bundle_hash` is the idempotence key. It covers the whole deterministic bundle — question text,
evidence rows, grades, measurements, clusters — and deliberately excludes the attached `synthesis`
key, which would otherwise make the hash unreachable after the first write.

Consequences, both tested:

- Re-running SYNTH over an unchanged bundle costs **zero** calls.
- Adding evidence in a supplement round changes the hash, so the narrative is regenerated rather than
  silently describing the old evidence set.

`alembic check` exits 0 — the index and constraint are declared in `__table_args__`, so this migration
does not repeat the drift Phase 1.5 had to clean up.

---

## 5. Cost

Bounded on three sides:

| Bound | Effect |
|---|---|
| `SYNTHESIS_LLM_MAX_QUESTIONS=8` | at most 8 calls per SYNTH pass |
| `bundle_hash` | later passes in the same job cost 0 |
| skip rules | `insufficient_evidence`, or <2 full-text units → never sent |

The skip rules matter more than the cap: they remove exactly the questions where the evidence is
thinnest, which are both the least useful to synthesize and the most likely to induce the model to
fill the gap.

Role `synthesizer` falls back to `planner` (reasoning, not schema-reading), so existing deployment
env files need no change.

---

## 6. Files

| Path | Change |
|---|---|
| **new** `services/worker/paperforge_worker/pipelines/synthesis_llm.py` | prompt, contract, the five rules, fingerprint |
| **new** `packages/db/db/repositories/synthesis.py` | `list_question_syntheses`, `upsert_question_synthesis` |
| **new** `packages/db/migrations/versions/0025_question_synthesis.py` | table |
| `packages/db/db/models/paper.py` | `QuestionSynthesis` |
| `services/worker/paperforge_worker/pipelines/synthesis.py` | attach on load; generate in `synthesize_questions`; 4 new counters |
| `services/worker/paperforge_worker/pipelines/outline.py` | carries `synthesis` through to the section payload |
| `services/worker/paperforge_worker/pipelines/writing.py` | `_synthesis_block` renders `[SYNTHESIS …]` |
| `packages/llm_runtime/llm_runtime/config.py` | role `synthesizer` → `planner` |
| `services/worker/paperforge_worker/config.py` | two flags |
| `.env.example`, `.env.production.example` | documented |

The writer prompt is **not** relaxed. `[SYNTHESIS]` is guidance about how to argue; sentence-level
`evidence_ids` remain mandatory and `enforce_sentence_evidence_rules` still runs.

---

## 7. Verification

| Check | Result |
|---|---|
| `ruff check .` | exit 0 |
| ontology lint | ok (**5** files — `synthesis_llm.py` added as a target) |
| `pytest packages services/worker` (real PostgreSQL 16) | **704 passed, 2 skipped, 1 failed** |
| `pytest services/api` (clean database) | **159 passed, 0 failed** |
| `alembic upgrade head` → `alembic check` | 0025 applied; **exit 0, no drift** |
| mypy | 0 new errors in production code |

`apps/web` was not touched, so vitest and `tsc` were not re-run.

The single failure is `test_visual_export.py::test_docx_converts_uploaded_pdf_figure_to_embedded_png`
— the long-documented environmental one (`paperforge-worker:local` has no Pillow; it lives in
visuald). Unrelated to this phase.

mypy reports two `upsert_work` protocol-mismatch errors in the new test's `_Candidate` fixture. That is
the same known pattern as `packages/db/tests/test_library_contracts.py`, which has the identical
errors on an identical fixture; no production file gained an error.

One existing test needed a fix, and it is worth naming: `test_ontology_lint_ignores_literals_inside_comments`
rewrote the lint script by **enumerating** its targets to strip them, so adding a fifth target broke
the helper. It now replaces the whole `TARGETS` list with a regex, and adding a target no longer
breaks it.

### Tests added — 30

**23 validation rules** (`services/worker/tests/test_synthesis_llm.py`), each rule with both an
accepting and a rejecting case:

- rule 1 — a foreign id is dropped; an entry with only foreign ids is dropped; **a wholly foreign
  response yields an empty synthesis rather than a cross-contaminated one**
- rule 2 — accepts a real cluster across two works; **rejects a conflict spanning different
  comparability keys**; rejects an invented key; rejects a conflict inside one work
- rule 3 — accepts a dimension from the task ontology; rejects an invented one
- rule 4 — demotes an abstract-only entry to `gap` with its note and evidence intact; keeps an entry
  that mixes abstract and full-text evidence
- rule 5 — **nulls a claim containing a fabricated percentage**; rejects a fabricated statement;
  accepts a number quoted from the evidence text; accepts one that exists only in a measurement row;
  **does not reject a year or a table number**
- structural — gap entries need no ids; a malformed response is empty, not partially trusted; the
  fingerprint ignores the attached synthesis but tracks evidence changes; skip rules; an oversized
  response is bounded on the number of entries **read**, not just the number kept

**7 orchestration** (`services/worker/tests/test_synthesis_enrichment.py`, real Postgres):

- flag off ⇒ no call, no `synthesis` key on the bundle, no row, no payload change
- flag on ⇒ narrative attached and `answer_status` **identical to the flag-off run**
- an unchanged bundle makes **no second call** and still attaches the stored narrative
- new evidence invalidates the stored synthesis and triggers regeneration
- a fully rejected response is remembered, not retried, and leaves a degradation warning
- a question below the evidence floor is never sent
- the per-job cap bounds the spend

Plus 2 writing-side tests: the `[SYNTHESIS]` block renders all five entry kinds with their evidence
ids, and a section with no synthesis produces a prompt with no `SYNTHESIS` text at all.

### Not verified

The plan's **quality** criterion — on ≥3 `g2` topics, more cross-study comparative sentences under
`problem_driven` than `legacy` with no increase in `insufficient_support` anchors — has **not** been
run. It requires the flag on against a live LLM provider and a full pipeline run per topic. It belongs
to step 3 of the rollout below.

> **Update 2026-08-12.** A shadow evaluation against 23 production bundles has since run and is
> reported in `PAPERFORGE_PHASE4_SHADOW_EVALUATION.md`. It found two defects in the code above —
> an output-token budget that silently killed 4 of 6 sub-questions, and an unenforced
> no-attribution instruction — both now fixed. It also established that **rule 2 is currently
> unexercisable in production**, because zero comparison clusters exist until Phase 2 runs.

---

## 8. Rollout

1. **Now.** Shipped with `SYNTHESIS_LLM_ENABLED=false`. Production behaviour is unchanged; the table
   exists and is empty.
2. **Enable on one review project.** Inspect `question_synthesis` rows against their bundles by hand.
   The question that matters: *did any `conflict` entry survive validation, and is it a real conflict?*
   If conflicts never survive, rule 2 is doing its job and the deterministic `conflicting`
   classification was over-claiming; if they survive but read as bogus, rule 2 is too loose.
3. **Compare prose.** `evals/review_depth`'s `g3_ablation_manifest.json` already defines
   `legacy` vs `problem_driven` for this comparison.
4. **Enable by default** once cluster-level conflict assertions are verified sound.

**Do P1-4 (cost accounting) first if you intend to leave it on.** `cost_estimate` is still never
populated, so this adds up to 8 planner-tier calls per SYNTH pass that nothing measures.

---

## 9. Residuals

**`claim` is the loosest field.** By the response contract it carries no `evidence_ids`, so rule 1
cannot constrain it; only rule 5 does. A claim with no numbers is accepted on the strength of the
prompt alone. It is rendered as `[SYNTHESIS claim]` guidance and never becomes a sentence without
independent binding, so the blast radius is bounded — but it is the one field a determined model could
overstate.

**No UI surface.** The narrative is worker-internal: it reaches the writer prompt and the
`question_synthesis` table, and there is no endpoint or panel that shows a user what the system
concluded. Worth adding before the feature is defaulted on, since step 2 above is a manual review task
currently done in SQL.

**`_synthesis_argument_points` is unchanged.** The outline still derives its argument points
deterministically from cluster classifications. That is intentional — the outline is a structural
contract — but it means an accepted `conditional` entry influences prose only through the writer
prompt, not through the section plan.

**Two flags, one meaning, still unenforced.** `TASK_PROFILE_FALLBACK` must agree between worker and
API (Phase 3.5 §7). Phase 4 reads task specs through the same path, so a disagreement now also skews
which dimensions rule 3 accepts.

---

## 10. Rollback

`SYNTHESIS_LLM_ENABLED=false`. Bundles omit the key, the writer prompt loses the block, stored rows
become unread. No migration reversal is needed and no other stage changes behaviour. The table can be
dropped later if the feature is abandoned.
