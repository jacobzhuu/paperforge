# PaperForge P1-4 — Cost Accounting

**Date:** 2026-08-12
**Audit ref:** P1-4 — *"Implement `cost_estimate` (per-model price table; image cost from provider capabilities)"*
**Migration:** none — every column already existed
**Result:** Complete

---

## 1. What was actually broken

Production, measured before any change:

```
llm_call_log        2,108 calls
  with usage        1,746   (83%)
  with cost         0       ← the entire finding
input tokens        6,258,588
output tokens       3,666,332
failed calls        305
```

So the token ledger worked all along. `LLMRunner._record` simply never passed a
`cost_estimate`, and `LlmCallLog.cost_estimate` stayed NULL forever. Every consumer —
`project_llm_cost`, `/cost/detail`, the overview panel — aggregates with
`coalesce(sum(cost_estimate), 0.0)`, so **9.9M tokens of real spend rendered as `$0.00`**.

That is worse than showing nothing: the panel asserted a number, and the number was wrong in
the direction that reassures.

---

## 2. Where the price comes from, and why not from Python

The audit suggested "a per-model price table in `llm_runtime/config.py`". The table is there,
and it is **deliberately empty**:

```python
DEFAULT_MODEL_PRICES: dict[str, ModelPrice] = {}
```

Prices vary by provider, by contract, and by month. A hardcoded table does not fail loudly when
it goes stale — it starts quietly reporting a *confidently wrong* amount, which is the same
class of failure as the `$0.00` it replaced. This deployment runs `deepseek-v4-pro` and
`deepseek-v4-flash`; a plausible-looking guess at their rates would have been indistinguishable
from a correct one on the panel.

So pricing is operator-supplied through `LLM_MODEL_PRICES`, in the unit providers actually
quote — **currency per million tokens** — so the number is copied from a price page without
arithmetic:

```json
LLM_MODEL_PRICES={"deepseek-v4-pro": {"input": 0.27, "output": 1.10}}
```

`price_for_model` matches exactly first, then by longest prefix, so `deepseek-v4-pro-0711`
inherits the `deepseek-v4-pro` entry. Without that, every provider snapshot release would
silently drop the deployment back to unpriced.

---

## 3. The distinction the whole change is built around

**`cost_estimate` is non-NULL if and only if the amount could be computed.**

| situation | `cost_estimate` |
|---|---|
| priced model + usage returned | the amount, possibly `0.0` |
| model not in `LLM_MODEL_PRICES` | `NULL` |
| provider returned no usage | `NULL` |
| call failed | `NULL`, and it is counted as failed, not unpriced |

A self-hosted model that genuinely costs nothing records `0.0` and is *priced*. "Free" and
"unknown" never collapse into the same value — which is exactly the collapse that made the old
panel misleading.

Aggregation carries the distinction upward. `project_llm_cost` now returns:

```
priced_call_count      calls whose amount is known
unpriced_call_count    successful calls whose amount is not
cost_complete          unpriced_call_count == 0
```

When `cost_complete` is false the UI renders **`≥ $X`** and states how many calls could not be
priced and why. Image generation has no price source at all — all three providers declare
`cost_estimate_available=False` — so it is reported as unpriced rather than as free, and it
drags the project total down to a lower bound too.

---

## 4. `paperforge-admin cost`

The panel answers "what did this project cost". Rolling out a feature asks a different
question: *"what did **this role** cost after I turned it on."*

```
paperforge-admin cost --role synthesizer --since 2026-08-12
paperforge-admin cost --project <uuid>
paperforge-admin cost --job <uuid>
```

It groups by role and model, prints tokens and cost, and — the part that matters — **names
every model that still needs a price entry**, with the JSON snippet to add. An operator can go
from "the total looks low" to a corrected configuration without reading any code.

Role is the reliable axis: every row has one. Stage metadata (`metadata_json->>'stage'`)
covers only 388 of 2,108 production calls (18%), so per-stage cost is not available and the CLI
does not pretend otherwise. Each new semantic feature gets its own role
(`experiment_extractor`, `evidence_classifier`, `synthesizer`), so role-level attribution is
sufficient for the rollout questions that prompted this work.

---

## 5. Files

| Path | Change |
|---|---|
| `packages/llm_runtime/llm_runtime/config.py` | `ModelPrice`, `parse_model_prices`, `price_for_model`, `estimate_cost` |
| `packages/llm_runtime/llm_runtime/runner.py` | `_record` computes the estimate |
| `services/worker/paperforge_worker/config.py` | `LLM_MODEL_PRICES` |
| `packages/db/db/repositories/jobs.py` | `unpriced_call_count()`, three new fields on `project_llm_cost` |
| `services/api/paperforge_api/routers/settings.py` | per-role and image unpriced counts |
| `services/api/paperforge_api/schemas.py` | `CostResponse` fields |
| `services/api/paperforge_api/admin.py` | `paperforge-admin cost` |
| `apps/web/components/project/project-overview.tsx` | `≥` prefix and the unpriced explanation |
| `apps/web/lib/types.ts`, `.env*.example` | contract and documentation |

---

## 6. Verification

| Check | Result |
|---|---|
| `ruff check .` | exit 0 |
| `pytest packages services/worker` | **728 passed, 2 skipped, 1 failed** (the known Pillow/visuald one) |
| `pytest services/api` | 158 passed + the cost-endpoint test updated and passing |
| `tsc --noEmit` | exit 0 |
| `vitest` | **141 passed** (138 → +3) |

### Tests added — 26

**16 pricing** (`packages/llm_runtime/tests/test_model_pricing.py`): per-million arithmetic;
**an unpriced model yields `None`, not `0.0`**; missing usage yields `None` even for a priced
model; one-sided usage still prices; version suffixes inherit the base price; longest prefix
wins; case-insensitive lookup; malformed entries are skipped without dropping the table; a
non-mapping table degrades to empty; **a genuine zero price stays distinguishable from
unpriced**; and three runner-level tests proving the record actually carries the value.

**7 aggregation** (`packages/db/tests/test_cost_accounting.py`, real Postgres): a fully priced
project reports complete; **one unpriced model turns the total into a lower bound**; a provider
returning no usage counts as unpriced; a *failed* call does not; a real `0.0` is priced; an
empty project is complete; another project's calls are excluded.

**3 UI** (`apps/web/tests/project-overview.test.tsx`): a complete total prints the amount
plainly; an incomplete one prints `≥` and says how many calls could not be priced; unpriced
image generation alone is enough to demote the total.

---

## 7. Residuals

**Nothing is priced until an operator says so.** That is the design, but it means the panel
still shows a lower bound of `$0.0000` on this deployment until `LLM_MODEL_PRICES` is set for
`deepseek-v4-pro` and `deepseek-v4-flash`. `paperforge-admin cost` names them.

**Historical rows stay NULL.** 2,108 existing calls have usage but no price. A backfill is a
one-line UPDATE once prices are known, and is deliberately not automated — retroactively
stamping a *current* price onto a year-old call is its own kind of wrong number.

**Image cost has no source.** The three providers report `cost_estimate_available=False`, so
image spend is structurally unknown, not zero. Pricing it would need a size/quality table per
provider, which is a separate piece of work.

**`provider` is NULL on 1,788 historical rows** (pre-`0019`). Harmless for cost, but it makes
provider-level grouping incomplete for anything older than that migration.

**Stage attribution is 18%.** Adding `metadata={"stage": ...}` at every call site would make
per-stage cost possible; today only role is reliable.
