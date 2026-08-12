# PaperForge — Structured Dimension Backfill

**Date:** 2026-08-12
**Goal:** populate `task` / `dataset` / `split` on production evidence so `comparability_key` groups
results, and Phase 4's conflict rule finally sees real data
**Command:** `paperforge-admin backfill-dimensions --project <id> --linked-only --apply`
**Applied to:** project `21016fe8` (近3年序列推荐攻击的文献综述), 13 works
**Result:** two blocking defects found and fixed, plus two more found while validating; dimensions
are populated and tasks canonicalised; **no cross-paper comparability exists in this corpus, so
Phase 4's conflict rule remains untested — and that is a property of the papers, not of the code**

---

## 1. Two defects that would have made Phase 2 a no-op

### 1.1 Nothing in production could be locator-verified

First dry run, 3 works: **0.0% locator-verified**, so `_apply_llm_measurements` — which drops
unverified cells by design — would have written nothing.

Measured across the whole database:

```
document_chunk        18,302 rows / 300 parses
  with page                0
  with section_path        0
  with object_ref          0
```

Not one chunk carries a locator. `build_locator_index` therefore had only the parse metadata's
`structured_objects` (figures, equations) to verify against, so a result cell honestly located at
`"Abstract"` or `"Experiments / Performance Comparison (RQ1)"` failed.

**Turning `EXPERIMENT_EXTRACTION_MODE=on` today would have paid for every extraction call and
written approximately nothing** — the feature would have looked enabled and done nothing.

Fixed by admitting **evidence anchors** into the verification surface: the locators already carried
by the candidates/units of the same document. That is not a new trust source — the pipeline recorded
them from the same parse, they are what grades evidence A/B, and `_apply_llm_measurements` binds
cells by exactly that `source_location`. Without them the verification surface and the binding
surface were different coordinate systems. Two tests pin both directions: anchors turn an otherwise
empty index into one that verifies `"Abstract"`, and anchors still reject a page or section nobody
recorded.

### 1.2 `experiment_extractor` never got a thinking policy

`experiment_extractor` resolves its **model** through `ROLE_MODEL_FALLBACKS` → `extractor` →
`deepseek-v4-flash`. Its **thinking policy** resolved through nothing:

```python
value = str(self.role_thinking.get(role) or "")   # deployment map only
```

Deployments configure `LLM_ROLE_THINKING` as a hardcoded full mapping that cannot know about roles
added later. So a task whose own module calls itself *"按封闭 schema 读文本，不是规划或写作"* ran
with thinking **enabled**, burning its output budget on reasoning before emitting JSON.

Measured on the same 13 works:

| | thinking on | thinking off |
|---|---|---|
| works yielding an extraction | 7 / 13 | **11 / 13** |
| result cells | 14 | **65** |
| locator-verified | 12 (85.7%) | **61 (93.8%)** |
| failed calls | 15, all `output_truncated` | **0** |
| output tokens | 55,814 | 46,244 |

4.6× the cells, a higher verification rate, no truncations, and *fewer* output tokens.

Fixed by giving `thinking_for_role` the fallback layer `model_for_role` already had: deployment
config first, then the built-in defaults, where `experiment_extractor` is now `disabled`.
Deliberately **not** reusing `ROLE_MODEL_FALLBACKS` — `evidence_classifier` shares the extractor's
model tier on purpose but keeps a separate thinking policy, and a test pins that it stays `None`.

This is the same failure class as Phase 4's output budget, found the same way: unit tests feed
responses straight to the parser and never exercise the token budget.

---

## 2. What the backfill did

```
11 work(s), 64 cell(s), 60 locator-verified (93.8%), 28 rejected
  skipped (extraction_unavailable): 2
spend: 17 call(s), 151,951 in / 48,595 out
40 measurement(s) written
```

93.8% clears the plan's 0.8 promotion gate. The command buffers every extraction, computes the
**aggregate** rate, and refuses to write below the gate — one extraction pass, a real gate, no
double spend.

### Comparability now works

```
                        before        after
evidence_measurement      359           398
  with task                 0            39
  with split                0            29
distinct comparability_key 306 / 359    323 / 398
```

The `before` numbers are the pathology: 306 distinct keys for 359 rows means the key was effectively
a unique id, because it salts with the evidence-unit id whenever task, dataset **or** split is
missing. Among the 40 LLM-written measurements results do now collide within a paper — the salting no longer
fires, because task, dataset and split are all present:

```
recsys.adversarial_defense   31
recsys.poisoning_attack       9
```

Grouping every measured unit in this project yields **8 clusters over 16 evidence units — every one
of them confined to a single paper.** See §2.1: that is the corrected number, and it is the finding.

---

### 2.1 Correction — and the real conclusion

An earlier version of this report said the backfill produced *"9 comparison clusters over 18 evidence
units and 9 distinct works."* **That was wrong.** `sum(works)` across 9 clusters was read as a
distinct-work count; each cluster spans exactly one work:

```
works_spanned | clusters | units
            1 |        8 |    16
```

**No cross-paper cluster exists in this corpus.** A follow-up hypothesis — that free-text `task` was
splitting apart genuine comparisons — was also wrong. After canonicalisation the two candidate pairs
resolve to:

```
HR@10 | Beauty | test | recsys.poisoning_attack     → "Potent but Stealthy: Rethink Profile Pollution Attack…"
HR@10 | Beauty | test | recsys.adversarial_defense  → "SimRec: Mitigating the Cold-Start Problem…"
```

Same metric, same dataset, same split — a profile-pollution attack paper and a cold-start mitigation
paper. They are **not** comparable, and `task` keeping them apart is the mechanism working.

So the conclusion is not "one more fix away". Of 13 works, 11 yielded extractions and each runs a
genuinely different experiment. **Testing Phase 4's rule 2 requires a corpus with real head-to-head
evaluation** — a benchmark-heavy topic where papers compete on one task — not more pipeline work.

## 3. Why bundles still have zero clusters

Synthesis reads only evidence **linked to a sub-question**. For this project:

```
evidence units in the project        618
  linked to a sub-question            68   (11%)
  carrying an LLM measurement         11
  both                                 1
```

Expected overlap if the two sets were independent: 68 × 11 / 618 ≈ **1.2**. Observed: **1**. They
are independent, and the code says why — `_deterministic_links` selects by grade and lexical
relevance score, and uses measurements *only* to render a `condition_note`:

```python
dimensions = measurements.get(unit.id, [])
if unit.grade not in {"A_located_structured", "B_located_prose"} or score < 0.45:
    continue
condition = "; ".join(f"dataset=… metric=… split=…" for item in dimensions[:3])
```

Nothing connects *"this unit carries a comparable measurement"* to *"this sub-question asks for a
comparison."* So a cluster can only form by coincidence — which is the deeper reason
`comparison_clusters` has always been 0, one layer below the missing dimensions.

All 11 measured units are A/B grade, so they are *eligible* for linking; they simply never scored
high enough lexically. That makes this fixable, not structural.

**Phase 4's rule 2 therefore remains untested.** The blocker has moved from "no dimensions exist" to
"the matrix does not route measured units into bundles", which is narrower and precisely located,
but it is not the same as done.

---

## 4. Two fixes worth keeping regardless

### 4.1 `task` is canonicalised against the bound ontology

`ExperimentExtraction.task` is whatever prose the model returns, and it is part of the key. The same
setup arrived three ways — `sequential recommendation`, `Sequential recommendation with …`,
`Profile Pollution Attack against …` — so one setup became three keys.

`_canonical_task` now routes it through `infer_task_id` against the project's bound task specs,
falling back to case/whitespace normalisation when nothing matches, so an unmatched task loses only
its formatting rather than being forced into someone else's slug. Production now stores:

```
recsys.adversarial_defense   31
recsys.poisoning_attack       9
```

This did **not** create cross-paper clusters (§2.1 explains why it should not have), but it removes a
real source of spurious fragmentation.

### 4.2 A re-run replaces its own rows

The upsert matches on `(unit, metric, value, dataset, split)`, and a re-extraction need not reproduce
cells byte-identically — so rows from an earlier run survived. Production briefly held **one paper
carrying two different task values at once**, which fragments keys exactly the way §4.1 fixes.
`apply_work_dimensions` now clears that work's `extraction_source='llm'` rows before writing. Regex
rows are untouched; both behaviours have a test.

## 5. The comparability bridge (unproven here)

`_diverse_ranked_candidates` now pulls the members of a cross-paper comparable cluster into the
candidate pool, bounded by `MAX_COMPARABILITY_BRIDGE` and the per-work cap. The scarcity it targets
is slot allocation, not the lexical floor: a probe showed `_rank_candidates` already admits ~615 of
618 units, and the measured ones simply lose the top-24 cut.

Its production effect today is **+0 candidates**, because the admission rule requires a key spanning
two papers and no such key exists (§2.1). Six tests cover the mechanism, including that a salted key
can never bridge and that a cluster inside one paper does not qualify. It is kept as correct code
with unproven production value, not as a demonstrated improvement.

---

## 5. Verification

| Check | Result |
|---|---|
| `ruff check .` | exit 0 |
| ontology lint | ok (5 files) |
| `pytest packages services/worker evals` | **760 passed, 2 skipped, 1 failed** (known Pillow/visuald) |

**12 tests added.** 10 for the backfill (`test_dimension_backfill.py`, real Postgres): dimensions
land on existing units; **two works with the same setup finally share one `comparability_key`**;
different datasets stay incomparable; **the evidence units themselves are byte-identical
afterwards**; an unverified cell is never written; missing full text and missing units are skips,
not failures; running twice does not duplicate; the extractor is called once per work even though
apply runs separately; and the backfill works with `EXPERIMENT_EXTRACTION_MODE=off`, because it is
an explicit operator action, not a pipeline mode.

2 for the locator surface, 2 for the thinking fallback.

---

## 6. Rollback

`DELETE FROM evidence_measurement WHERE extraction_source = 'llm';` — before the run there were 0
such rows, so that boundary is exact. A table-scoped backup taken beforehand is at
`/data1/zhuzy/paperforge-evidence-measurement-prebackfill-20260812-151614.sql.gz` (359 rows).

The two code fixes are independent of the data and should be kept regardless: without them Phase 2
writes nothing and burns 58% of its calls on truncation.

---

## 7. What would actually finish this

1. **Canonicalise `task`** against the project's bound ontology before writing (§4).
2. **Make the matrix measurement-aware** — let a comparison-shaped sub-question preferentially link
   units that carry measurements sharing a `comparability_key`. This is the step that turns 9 latent
   clusters into clusters synthesis can see, and it is the only one that unblocks Phase 4's rule 2.
3. Re-run the backfill on the remaining projects, then re-run
   `evals/synthesis_shadow` and read every `conflict` entry that survives.

Step 2 is a design change to QMATRIX, not a configuration flip, and is deliberately left as a
decision rather than taken here.
