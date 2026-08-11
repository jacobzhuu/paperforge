# PaperForge Phase 4 — Synthesis Shadow Evaluation

**Date:** 2026-08-12
**Harness:** `evals/synthesis_shadow/` (`python -m evals.synthesis_shadow.run`)
**Target:** the live production database, 4 review projects, 23 sub-question bundles
**Model:** `deepseek-v4-pro` (role `synthesizer` → `planner` tier, thinking enabled)
**Spend:** 147,671 input + 146,964 output tokens across all runs
**Result:** two real defects found and fixed; the feature is sound on everything the data can exercise, and one rule is structurally untestable today

---

## 1. What "shadow" means here

Strictly: the real `load_synthesis_bundles`, the real prompt, the real `build_synthesis` filters,
run against real production evidence — and **no prose, no `answer_status` write, no persisted
row** (the `--persist` flag exists but was not used, because production is still at migration
`0024` and `question_synthesis` does not exist there yet).

The one thing the harness adds over the production path is bookkeeping: production collapses
rejections into a single counter, and the distribution is exactly what an evaluation needs.

---

## 2. Two defects found

### 2.1 The output budget was too small — the feature would have silently half-worked

First run, one project, 6 sub-questions:

```
LLM calls           6
responses parsed    2      ← 4 of 6 produced nothing
failed calls        10
  output_truncated  10
```

`synthesizer` routes to the `planner` tier, which runs with thinking **enabled** — and reasoning
tokens count against `max_output_tokens`. At 1600 the model exhausted the budget before emitting
any JSON, so the runner's retry ladder doubled to the 8192 ceiling and gave up.

In production this is the worst available outcome: **two or three billed calls per sub-question,
and nothing to show for them.** The unit tests could not have caught it — they feed responses
directly to `build_synthesis` and never exercise the token budget.

Raised to 4000, then to **6000** after measuring: successful calls average 3,663 output tokens,
so 4000 still truncated 7 of 29 calls (24%). At 6000 the last three runs had **zero**
truncations.

### 2.2 "Do not write author names or years" was advice, not a rule

Prompt instruction 6 forbids citation markers. Across 92 accepted entries the model wrote one
anyway:

> Qin等人（2025）在摘要中提及专为联邦序列推荐设计的防御策略，但因证据仅限摘要，具体机制未描述。

The substance is correct — that is exactly the rule-4 abstract-only demotion working. The
attribution is the problem: it is neither bound nor whitelisted, and the codebase's whole stance
is that the model *structurally cannot* write a reference, not that it is asked not to.

Now stripped, matching `writing._clean_paragraph`'s existing treatment of prose, in both Latin
(`Smith et al. (2020)`) and CJK (`Qin等人（2025）`) forms. The statement survives; only the
attribution goes, and grounding continues to come from `evidence_ids`. A test pins that
`"Both studies use the 2020 benchmark"` is *not* touched — over-stripping would quietly corrupt
legitimate sentences.

---

## 3. Results on 23 production bundles

```
bundles seen              23
  skipped (insufficient)   1
LLM calls                 22
responses parsed          22   (100%)

entries proposed         111
entries accepted          92   (82.9%)
    agreement             41
    conditional           15
    conflict               0   ← see §4
    gap                   36

rejected  no_bundle_evidence   17
          unsourced_number      1
          unknown_dimension     1

claims kept              20/22
bundles with nothing kept  1
```

**Rule 1 is the load-bearing filter.** 17 of 19 rejections are the model citing an
`evidence_id` that is not in the bundle — 15% of everything it proposes. Without that filter
those entries would look bound and be unbound. Nothing else comes close.

**Rules 3 and 5 fire rarely but do fire.** One fabricated number, one invented dimension. The
numeric red line is largely respected once the evidence is in front of the model.

**Rule 3 is the first real consumer of `dimension_schema_json`** (audit correction C-4 recorded
it as having zero readers). The accepted dimensions include `threat_model` and `attack_goal` —
neither is in `CORE_DIMENSIONS`; both come from the `recsys_attack` ontology rows. Domain
vocabulary flowing from data into a semantic decision is Phase 3 paying off in Phase 4.

### A verified example

The most quantitative sub-question — *"which sequential recommenders are the attacks effective
against, and how is that quantified?"* — produced this claim:

> 在无防御的未指定攻击下，BERT4Rec、SASRec和NARM的NDCG@10变化率分别为32.7%、32.4%和33.6%。

Checked against the source evidence unit:

> *"Without adopting defense methods, we observe 27.4% NDCG@10 variation on Locker in untargeted
> attacks, compared to 32.7%, 32.4% and 33.6% of BERT4Rec, SASRec and NARM."*

Correct numbers, correct model-to-number pairing, correct condition, translated into the paper's
language. Deterministically this bundle yields `partial` with the generic argument point *"state
current findings and boundaries according to evidence grade"* — 24 evidence units and 11 works
producing no comparative sentence. That gap is the whole reason Phase 4 exists.

Note what this instance also shows: **both structured entries for that same bundle were
rejected** for citing ids outside the bundle. The model produced a good claim and bad bindings
in one response, and the filters kept exactly the right half.

---

## 4. What this evaluation could **not** test

**Zero comparison clusters exist in production.** Measured across the whole database:

```
evidence_measurement rows            359
  with task                            0
  with split                           0
  with dataset                       180
distinct comparability_key           306   (of 359 rows)
clusters (≥2 units sharing a key)      0
measurements linked to sub-questions  15
```

`comparability_key` salts with the evidence-unit id whenever task, dataset **or** split is
missing, so with `task` and `split` universally absent the key is effectively a unique id and
nothing ever groups.

Consequence: **rule 2 cannot accept a `conflict` entry in production today, no matter what the
model proposes.** The `conflict: 0` line above is an absence of measurement, not an absence of
problems, and the harness prints that caveat rather than letting the zero read as clean. The
plan's headline release question — *"does any conflict entry survive validation, and is it a
real conflict?"* — remains **unanswered**.

It becomes answerable only after `EXPERIMENT_EXTRACTION_MODE` leaves `off`, which is what
populates task/dataset/split. The dependency chain stated in the Phase 4 report is not
hypothetical; it is binding right now.

The plan's other quality criterion — more cross-study comparative sentences under
`problem_driven` than `legacy` on ≥3 `g2` topics — also remains unrun. `evals/review_depth` is a
five-expert blind protocol whose fixtures are deliberately `needs_expert_review`; it needs human
reviewers, not another script.

---

## 5. Cost

| run | in | out | truncated |
|---|---|---|---|
| first (1 project, budget 1600) | 8,673 | 6,329 | 10 |
| diagnosis (2 questions) | 13,239 | 9,299 | 2 |
| **main (4 projects, 23 bundles)** | **76,389** | **80,592** | 7 |
| confirmation (6 questions, budget 6000) | 29,579 | 31,509 | 0 |
| instrumentation (3 questions) | 19,791 | 19,235 | 0 |
| **total** | **147,671** | **146,964** | |

The main run works out to ≈3,470 input + 3,660 output tokens per sub-question. For a review with
6 sub-questions that is ~21k in / ~22k out per SYNTH pass, once per distinct bundle.

The dollar figure is **not** stated because `LLM_MODEL_PRICES` is unset on this deployment, and
P1-4's rule is that an uncomputable amount is reported as uncomputable rather than as zero. Set
prices for `deepseek-v4-pro` and the same run reports currency:

```
paperforge-admin cost --role synthesizer
```

---

## 6. Changes this evaluation produced

| Change | Basis |
|---|---|
| `MAX_OUTPUT_TOKENS` 1600 → 6000 | 4 of 6 sub-questions produced nothing; 3,663-token average output |
| author-year attributions stripped from statements | 1 occurrence in 92 accepted entries |
| rejections record the offending value | "unknown dimension" is unactionable without knowing which |
| `evals/synthesis_shadow/` | there was no way to ask these questions at all |

The harness has 13 unit tests of its own. The one that matters asserts the wording: with zero
clusters, the report must say **"UNTESTED here, not clean"**.

---

## 7. Recommendation

**Do not enable `SYNTHESIS_LLM_ENABLED` by default yet** — not because anything measured is bad,
but because the rule most capable of producing a confidently wrong sentence (rule 2, conflict
assertion) has never once been exercised.

Order:

1. Set `LLM_MODEL_PRICES` so the next run reports currency.
2. Enable `EXPERIMENT_EXTRACTION_MODE=shadow`, then `on` once locator-verified ≥80%. That is what
   creates comparison clusters.
3. Re-run this harness. With clusters present, conflict entries become possible; read every one
   that survives.
4. Then enable synthesis on a single review project and compare prose.

Enabling synthesis on `agreement`/`conditional`/`gap` alone would already be defensible on these
numbers — 82.9% acceptance, one fabricated number in 111 proposals, verified quotation. The
reason to wait is that `conflict` is the entry type a reader would trust most and the one nobody
has yet seen the system get right.
