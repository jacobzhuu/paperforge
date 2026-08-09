# PaperForge Phase 1 — Engineering Baseline Report

**Date:** 2026-08-06
**Phase:** 1 of 5 — *Version control, secret hygiene, Ruff fix, CI activation*
**Plan:** `docs/plans/PAPERFORGE_P0_REMEDIATION_PLAN.md` §Phase 1
**Commit:** `b77c1a298bf1bbd04a7fb089f28e1983bd134d7b` (`b77c1a2`) on `main`
**Result:** Complete, with **two blockers requiring user action** (§10)

> **No secret value appears anywhere in this report.** Credential material is described only by
> key name, length, entropy, or hash prefix.

---

## 1. Headline correction to the audit

**`PAPERFORGE_DEEP_AUDIT.md` §4.4 and §10.3 claimed the repository's `.env` held live API keys at
mode 0664. That claim was wrong.** It is corrected here, and no rotation was performed because none
is warranted.

The audit's redaction command (`sed 's/\(KEY=\).*/\1<redacted>/'`) rewrote *empty* values to
`<redacted>` exactly as it rewrote populated ones. I read the resulting `<redacted>` markers as
evidence of live credentials. Direct inspection shows otherwise:

| File | Sensitive keys | Actual state |
|---|---|---|
| `.env` | `LLM_OPENAI_API_KEY`, `OPENALEX_API_KEY`, `YUNWU_API_KEY` | **empty** |
| `.env` | `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | documented dev defaults (`paperforge` / `paperforge-secret`) |
| `.env.example` | same five | identical to `.env` — empty / dev defaults |
| `.env.production.example` | `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_ROOT_PASSWORD`, `SMTP_PASSWORD` | all `CHANGE_ME…` placeholders |
| `.paperforge/deploy.env` | none | deployment topology only (project name, web port, Redis DB index, subnets, image tag) |

Live credentials are held **outside the repository** at a path referenced by
`PAPERFORGE_ENV_FILE`, already at mode `600`, owned by the operator. `scripts/ops:45` refuses to
start unless that variable points outside the repo. **The project's secret hygiene was already
correct.** The audit finding was a false positive and its P0-1 rotation sub-item is withdrawn.

The remaining, genuine parts of audit §4.4 stand and are addressed here: the working tree was not
under version control and `ruff check .` exited 1.

> **Further correction (2026-08-09, §15.2).** "There was no version control / CI had never run" is
> also wrong, though less severely. A remote *did* exist with 24 commits and a passing CI history
> through 2026-07-27; the working tree had lost its `.git` directory, and ten days of work then
> accumulated outside version control. The accurate statement is **"CI stopped seeing the code,"**
> which is why the Ruff failure and the migration drift went undetected.

---

## 2. Files changed

Only two files were modified. Everything else in the commit is the pre-existing tree imported
unchanged.

| File | Change | Lines |
|---|---|---|
| `.gitignore` | hardened (§4) | +25 |
| `scripts/ontology_literal_lint.py` | removed unused `import sys` (§6) | −1 |
| `docs/reports/PAPERFORGE_PHASE1_BASELINE_REPORT.md` | this report | new |

```
$ git show --stat --oneline HEAD -- .gitignore scripts/ontology_literal_lint.py
b77c1a2 Initial commit: PaperForge engineering baseline
 .gitignore                       | 62 ++++++++++++++++++++++++++++++++++++++++
 scripts/ontology_literal_lint.py | 38 ++++++++++++++++++++++++
 2 files changed, 100 insertions(+)
```

(Counts show full file content because this is the initial commit; the net semantic delta is the
two changes above.)

**No production code was modified.** Evidence extraction, synthesis, quality verification, writing,
figure generation and export are untouched — `scripts/ontology_literal_lint.py` is a CI helper that
is not imported by any service.

---

## 3. Git initialization and remote status

| Item | Value |
|---|---|
| Repository | created at `/data/zhuzy/projects/paper-forge` |
| Branch | `main` |
| Commit | `b77c1a298bf1bbd04a7fb089f28e1983bd134d7b` |
| Tracked files | **481** |
| Working tree | clean (`git status --porcelain` empty) |
| Remote | **none configured** |
| `gh` CLI | not installed |
| SSH keys | none present |
| Credential helper | none configured |

`/data/zhuzy/projects/paper-forge` and `/home/zhuzy/projects/paper-forge` are the same inode
(`2049:90201348`), so a single repository covers both paths.

**Remote configuration was not performed** — no remote URL, no `gh` authentication, no SSH key and
no credential helper exist on this host. Per the task's instruction not to fabricate completion,
this is recorded as a blocker in §10, with exact commands.

---

## 4. `.gitignore` audit results

### 4.1 Method

The tree was inventoried **before** `git init`. Every top-level entry was sized and classified, and
the filesystem was swept for credential-shaped and generated files outside already-ignored paths:

```bash
find . -type f \
  -not -path './.venv/*' -not -path '*/node_modules/*' -not -path './.mypy_cache/*' \
  -not -path './.pytest_cache/*' -not -path './.ruff_cache/*' -not -path './apps/web/.next/*' \
  -not -path '*/__pycache__/*' -not -path './.paperforge/*' \
  \( -name '.env*' -o -name '*.pem' -o -name '*.key' -o -name '*.p12' -o -name '*credential*' \
     -o -name '*secret*' -o -name '*.pdf' -o -name '*.sqlite*' -o -name '*.db' -o -name '*.log' \
     -o -name 'id_rsa*' -o -name '*.tfstate' \) 
# → only .env, .env.example, .env.production.example
```

### 4.2 Pre-existing coverage — verified correct

`__pycache__/`, `*.py[cod]`, `.venv/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`,
`*.egg-info/`, `node_modules/`, `apps/web/.next/`, `apps/web/out/`, `*.tsbuildinfo`, `.env`,
`.env.*`, `data/`, `artifacts/`, `*.log`, `.DS_Store`, `.minio/`, `.pg/`, `.paperforge/`, local note
files, and `_tmp_*`.

Measured impact: `.venv` (393 MB), `apps/web/.next` (366 MB), `.mypy_cache` (41 MB) and
`apps/web/node_modules` were all correctly excluded.

### 4.3 Gaps found and closed

| Gap | Risk | Rule added |
|---|---|---|
| Private keys / certificates | a stray key would be committed | `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `secrets/` |
| Local databases and PID files | runtime state in history | `*.pid`, `*.sqlite`, `*.sqlite3`, `*.db` |
| Evaluation harness outputs | `evals/review_depth/anonymize.py` writes generated manuscripts and a blind-key mapping under the repo | `evals/review_depth/{generated,blind,results}/` |
| Machine-local agent config | `.claude/settings.local.json` is host-specific | `.claude/settings.local.json` |

`.claude/launch.json` **is** tracked — it is a shareable dev launch config containing only a
`localhost` API base. `.claude/settings.local.json` was inspected structurally (keys only:
`permissions.allow`) and contains no secrets, but is excluded as machine-local by convention.

### 4.4 Intentionally tracked

`.env.example` and `.env.production.example` remain force-included via `!` rules. Both were verified
to contain only empty values, documented dev defaults, or `CHANGE_ME…` placeholders (§1).
`uv.lock` and `apps/web/pnpm-lock.yaml` are tracked, as CI requires (`uv sync --frozen`,
`pnpm install --frozen-lockfile`).

### 4.5 Post-commit assertions

```
PASS: .env not tracked
PASS: no venv/node_modules/.next/__pycache__
PASS: no data//.paperforge//artifacts/
PASS: .claude/settings.local.json not tracked
PASS: no .DS_Store/.log/.pid
env templates tracked (intended): 2
```

---

## 5. Secret exposure risks found

### 5.1 Repository tree

**None.** See §1 — the tree contains no live credential.

### 5.2 Staged- and committed-content scan

A scanner was written to inspect blob **content** (not just paths) for provider credential formats
(`sk-…`, `AKIA…`, `AIza…`, `ghp_…`, `xox[abprs]-…`, PEM private-key blocks, JWTs, 32-hex Cloudflare
account IDs) and for credential-shaped **string literals** assigned to sensitive names, scored by
Shannon entropy. It prints file, line, and classification only — never a value.

Run against the index before commit and against `HEAD` after commit; both produced the **same 25
findings across 481 blobs**, confirming staged and committed trees are identical.

Every finding was manually verified as a non-secret:

| Finding | Count | Verified as |
|---|---|---|
| `account_id="0123456789abcdef0123456789abcdef"` | 10 | one synthetic sequential-hex fixture, used in `test_specs_and_provider.py` and `test_worker.py` |
| `api_key="yunwu-secret"` / `"openalex-secret"` / `"cloudflare-key"` | 8 | test doubles |
| `PASSWORD = "correct horse battery staple"`, `NEW_PASSWORD = "a much better replacement passphrase"` | 2 | auth test fixtures |
| `foreign_token = "foreign-session-token"` | 1 | test fixture |
| `password_hash="!development-login-only"` | 2 | deliberate sentinel — a leading `!` makes it an invalid Argon2 hash, so it can never authenticate |
| `_PASSWORD_HASHER = PasswordHasher(` | 1 | scanner over-match on a multi-line constructor, not a literal |
| `claim_tokens = tokens(claim)` and similar | 1 | scanner over-match on an expression |

**Verdict: the committed tree passes the secret scan.**

### 5.3 First-pass scanner over-matching (disclosed for reproducibility)

A first scanner version reported 116 findings, dominated by two false-positive classes: expressions
assigned to `*_tokens` / `api_key` identifiers, and the documented dev DSN
`postgresql+asyncpg://paperforge:paperforge@…` (which also appears in `.github/workflows/ci.yml` as
the CI service's own credentials). The scanner was tightened to require a quoted literal and to
whitelist known dev defaults. Both versions are retained in the session scratchpad; the tightened
one is authoritative.

---

## 6. Ruff fix

**Before:**

```
F401 [*] `sys` imported but unused
 --> scripts/ontology_literal_lint.py:7:8
Found 1 error.
$ echo $?
1
```

**Change:** removed line 7 (`import sys`). Nothing else. The module exits via
`raise SystemExit(main())`, which is a builtin — `sys` was genuinely unused.

**After:**

```
$ .venv/bin/python -m ruff check .
All checks passed!
$ echo $?
0
```

Script still functional: `.venv/bin/python scripts/ontology_literal_lint.py` →
`ontology_literal_lint: ok`, exit 0.

**Why this mattered:** the CI backend job runs `ruff check .` *before* `pytest`. In its prior state
CI would have failed at lint and never executed the 768 tests. That this went unnoticed is direct
evidence CI had never run.

---

## 7. File-permission changes

| File | Before | After | Rationale |
|---|---|---|---|
| `.env` | `664` | **`600`** | dev-only values, but the file is the natural place a real key would later be pasted |
| `.env.example` | `664` | `664` (unchanged) | committed template, no secrets |
| `.env.production.example` | `644` | `644` (unchanged) | committed template, `CHANGE_ME…` only |
| production env file (outside repo) | `600` | `600` (unchanged) | already correct |

---

## 8. CI-equivalent commands executed and exact results

All five CI-equivalent steps were run locally. A **disposable** PostgreSQL 16 container was started
on `127.0.0.1:15433` with no bind mounts, used for the database-backed steps, and removed
afterwards. The production database was never contacted; `paperforge-prod-postgres-1` publishes no
host port.

| # | Step | Command | Result |
|---|---|---|---|
| 1 | Ruff | `.venv/bin/python -m ruff check .` | **`All checks passed!` — exit 0** |
| 2 | TypeScript | `npx --no-install tsc --noEmit` (in `apps/web`) | **exit 0, no output** |
| 3 | Web tests | `npx --no-install vitest run` (in `apps/web`) | **26 files, 129 passed, 0 failed — exit 0** (8.82 s) |
| 4 | Python tests | `pytest -q -p no:cacheprovider` with real PostgreSQL | **764 passed, 4 skipped, 0 failed — exit 0** (755.61 s) |
| 5a | Alembic upgrade | `alembic upgrade head` | **exit 0** — all 21 migrations `0001`→`0021` applied |
| 5b | Alembic drift | `alembic check` | **exit 255 — FAILED** (§9) |
| 6 | Ontology lint | `scripts/ontology_literal_lint.py` | **`ok` — exit 0** |
| 7 | Secret scan | staged index, then `HEAD` | **pass** (§5.2) |

Environment: git 2.25.1, Python 3.12.12, ruff 0.16.0, Node v24.18.0, tsc 5.9.3,
PostgreSQL 16-alpine.

### 8.1 Python test detail

The full suite with a real database is a meaningful improvement over the audit's measurement:

| Condition | Passed | Skipped |
|---|---|---|
| Audit run (no PostgreSQL) | 602 | 166 |
| **This run (PostgreSQL 16)** | **764** | **4** |

The 162 previously-skipped database contract tests — R1 whitelist enforcement, `bibtex_key`
uniqueness, `job_event` sequence integrity, PDF-upload contracts, question-replacement logic — all
**pass**.

### 8.2 The 4 remaining skips — explicitly identified, not counted as passed

```
SKIPPED packages/latex_render/tests/test_project_and_compile.py:151
        set PAPERFORGE_TEST_TEXD_URL to run the real texd regression
SKIPPED packages/latex_render/tests/test_project_and_compile.py:176
        set PAPERFORGE_TEST_TEXD_URL to run the real texd regression
SKIPPED services/worker/tests/test_visual_export.py:145   pandoc is not installed
SKIPPED services/worker/tests/test_visual_export.py:158   pandoc is not installed
```

- **texd (2):** the three running `texd` containers publish no host port by design (no-egress
  sandbox), so no reachable `PAPERFORGE_TEST_TEXD_URL` exists without altering deployment
  networking — out of scope for this phase.
- **pandoc (2):** not installed on this host. Pandoc ships inside the worker Docker image, so the
  DOCX export path is exercised in the real runtime, just not here.

**Both skips also occur on GitHub CI**, which sets neither `PAPERFORGE_TEST_TEXD_URL` nor installs
pandoc. This local run is therefore equal to or better than the configured CI job.

---

## 9. Migration consistency result

`alembic upgrade head` — **PASS**, all 21 migrations applied to an empty database.

`alembic check` — **FAIL (exit 255)**. This is the first time the check has ever run, and it found
genuine, **pre-existing** drift between the ORM models and the migration history. It was not
introduced by this phase (no model or migration file was modified).

Seven operations, **all index-level — no table, column, type, constraint or default drift**:

| # | Operation | Cause |
|---|---|---|
| 1 | remove `ix_claim_evidence_user_asset_id` on `claim_evidence_anchor` | migration created a hand-named index |
| 2 | add `ix_claim_evidence_anchor_user_asset_id` | model's `index=True` auto-names it `ix_<table>_<column>` |
| 3 | add `ix_eligibility_decision_project_id` | model declares `index=True`; migration `0012` omitted it |
| 4 | add `ix_eligibility_decision_work_id` | same |
| 5 | remove `ix_evidence_unit_task_topical` (composite `task_id`,`topical_status`) | migration `0011` created a composite index |
| 6 | add `ix_evidence_unit_task_id` | model declares `index=True` on `task_id` alone |
| 7 | add `ix_research_question_task_id` | model declares `index=True`; migration omitted it |

**Impact:** performance and schema-reproducibility only. No data-correctness or behavioural risk —
indexes do not change query results. However, the live database's index set does not match what a
fresh `alembic upgrade head` produces, so a rebuilt environment will have different query plans, and
**the CI backend job will fail at the drift step** once a remote is attached.

**Not fixed in this phase, deliberately.** A fix requires a new migration, which (a) is a schema
change against the production database, (b) is beyond the stated Phase 1 scope, and (c) collides
with migration number `0022`, which the remediation plan reserves for Phase 2. Recorded as a blocker
in §10 with a ready-to-apply remedy.

---

## 10. Remaining blockers

> **Status update (2026-08-09, Phase 1.5 baseline closure).**
> **B-1 is RESOLVED** by migration `0022_index_alignment` (commit `4451d46`); `alembic check` now
> exits 0. The analysis below is retained as the historical record of how the drift was found.
> **B-2 is RESOLVED** — remote configured, history grafted (all 24 pre-existing commits preserved),
> and CI is green on all four jobs at `61c656e`. See §15.2, which also corrects the audit's
> "no version control / CI never ran" claim.
> **B-3** stays withdrawn. **B-4** remains an open observation tracked as plan item P1-1.

### B-1 — `alembic check` fails on pre-existing index drift *(RESOLVED — see §15)*

**Owner:** engineering. **Effort:** ~2 hours.

Generate the corrective migration, review it (it must contain **only** the 7 index operations in
§9), and apply:

```bash
export DATABASE_URL=postgresql+asyncpg://paperforge:paperforge@127.0.0.1:15433/paperforge
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "align index definitions with ORM models"
# review the generated file — reject anything beyond the 7 index ops
uv run alembic upgrade head && uv run alembic check   # must exit 0
```

Two cautions: the file must be numbered to avoid colliding with `0022`, which the remediation plan
reserves for Phase 2; and because older deployment generations are still running (§11),
`CREATE INDEX CONCURRENTLY` should be considered so the migration does not lock tables an active
worker is writing to.

### B-2 — No Git remote; CI cannot run *(RESOLVED — see §15.2)*

**Owner:** user. No remote URL, `gh` authentication, SSH key or credential helper exists on this
host, so I could not push or verify the workflow. Nothing was fabricated.

Once a remote is available:

```bash
cd /data/zhuzy/projects/paper-forge
git remote add origin <REMOTE_URL>
git push -u origin main
```

Then confirm all four CI jobs (`backend`, `frontend`, `macos-host`, `multiarch-images`). Expect
`backend` to fail at the drift step until B-1 lands; the other three should pass, matching the local
results in §8. Note that `macos-host` and `multiarch-images` have never executed and may surface
first-run issues.

### B-3 — Credential rotation: not required, not performed

Withdrawn. §1 establishes there is no exposure: no live credential is in the repo tree, nothing was
ever committed (no prior history existed), and the production env file is outside the repo at mode
`600`. Rotating would have caused an outage for no security benefit. Should rotation be wanted for
unrelated reasons, the safe order is: mint replacements → write to the production env file →
`./scripts/dev restart` → verify the Funnel target and public `/login` per `AGENTS.md` → only then
revoke the old values.

### B-4 — Observation, not a blocker: `SEMANTIC_SCHOLAR_API_KEY` is provisioned but unusable

The production env file defines `SEMANTIC_SCHOLAR_API_KEY`, but audit §4.5 established that
`SemanticScholarDiscoveryAdapter` is absent from `ADAPTER_REGISTRY` and therefore unreachable, and
that `snowball` defaults `enable_semantic_scholar=False` with no caller overriding it. The key is
paid for and never sent. Tracked as remediation-plan item **P1-1**; out of scope here.

---

## 11. Deployment state

Unchanged and verified functional throughout.

| Check | Result |
|---|---|
| PaperForge containers running | 20, all `healthy` |
| Deployment generations | 3 (`20260803-144625`, `20260805-015133`, `20260805-170524`) — **none stopped or deleted** |
| Active deploy project | `paperforge-deploy-20260805-170524` (web port 3005) |
| `http://127.0.0.1:3005/login` | **HTTP 200**, 18,109 bytes — before and after all work |
| Production PostgreSQL | untouched; publishes no host port; never contacted |
| Disposable test PostgreSQL | `pf-phase1-testdb` on `127.0.0.1:15433`, created and removed (`--rm`); 0 remaining |

No container was stopped, restarted or removed. No deployment generation was reaped (audit item
P1-2 remains open).

---

## 12. Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| A valid Git repository exists | **PASS** | commit `b77c1a2`, branch `main`, 481 files |
| No `.env`, credential, private document, artifact, local DB, object-store data or deployment state tracked | **PASS** | §4.5 assertions, all PASS |
| Staged and committed trees pass a secret scan | **PASS** | §5.2 — 25 findings, all verified synthetic; staged ≡ committed |
| `ruff check .` exits successfully | **PASS** | `All checks passed!`, exit 0 |
| Python and web tests do not regress | **PASS** | 764 passed / 4 skipped (was 602/166 without a DB); 129 web passed |
| TypeScript checking remains clean | **PASS** | `tsc --noEmit` exit 0 |
| Skipped DB checks explicitly identified | **PASS** | §8.2 (4 skips, both classes also skipped by GitHub CI); §9 (`alembic check` reported as FAILED, not passed) |
| Live deployment remains functional | **PASS** | 20 containers healthy; `/login` HTTP 200 |
| No semantic or user-visible pipeline change | **PASS** | only `.gitignore` and a CI-helper import changed; no service module touched |
| Report contains reproducible evidence | **PASS** | every command and exit code in §8 |

---

## 13. Rollback instructions

Ordered least to most invasive. Nothing here touches production data.

**Undo the Ruff fix only**

```bash
cd /data/zhuzy/projects/paper-forge
git revert --no-commit HEAD -- scripts/ontology_literal_lint.py && git commit -m "revert ruff fix"
# `ruff check .` returns to exit 1
```

**Undo the `.gitignore` hardening only**

```bash
git revert --no-commit HEAD -- .gitignore && git commit -m "revert gitignore hardening"
```

**Restore `.env` permissions**

```bash
chmod 664 /data/zhuzy/projects/paper-forge/.env
```

**Remove version control entirely** (returns the tree to its pre-Phase-1 state; the working files
are byte-identical to the committed ones, so nothing is lost):

```bash
cd /data/zhuzy/projects/paper-forge
git status --porcelain          # confirm empty before proceeding
rm -rf .git
git checkout -- . 2>/dev/null || true   # no-op; .git is gone
# then optionally revert the two file edits by hand:
#   .gitignore                       — delete the blocks added under §4.3
#   scripts/ontology_literal_lint.py — restore `import sys` on line 7
```

**Deployment rollback:** not applicable. No deployment was rolled, stopped, or reconfigured, and no
image was rebuilt. The stack running now is the same one that was running before this phase began.

**Disposable test database:** already removed. If a stray instance is ever found:
`docker rm -f pf-phase1-testdb`.

---

## 14. Recommended next step

Fix **B-1** (index drift) and resolve **B-2** (remote), then confirm CI green. Only then start
Phase 2 (LLM structured extraction) — it introduces a new migration, and starting it while
`alembic check` is red would make future drift indistinguishable from the pre-existing kind.

*(B-1 was subsequently closed — see §15.)*

---

## 15. Phase 1.5 — baseline closure (2026-08-09)

A follow-up pass addressing only the two blockers above. No pipeline behaviour changed.

### 15.1 B-1 — index drift: RESOLVED

Migration **`0022_index_alignment`** (commit `4451d46`), 7 index operations, nothing else.

Autogenerate proposed exactly the 7 drift operations and no extras, confirming the drift was
index-only. The generated file was then rewritten by hand to (a) use `ALTER INDEX … RENAME` for the
name-only mismatch instead of drop + recreate, (b) guard every statement with `IF EXISTS` /
`IF NOT EXISTS` for blue/green safety, and (c) document the reasoning per item.

| # | Operation | Why |
|---|---|---|
| 1 | rename `ix_claim_evidence_user_asset_id` → `ix_claim_evidence_anchor_user_asset_id` | name-only mismatch; the model's `index=True` auto-names after the table, and the six sibling indexes already follow that convention. Renamed, not rebuilt — identical definition, atomic catalog op. |
| 2 | create `ix_eligibility_decision_project_id` | CASCADE FK; migration `0012` created only the composite + unique constraint |
| 3 | create `ix_eligibility_decision_work_id` | CASCADE FK with **no** covering index at all |
| 4 | drop `ix_evidence_unit_task_topical` | serves no query — `topical_status` never appears in a WHERE clause, and `EvidenceUnit.task_id` is filtered in Python by `qmatrix._rank_candidates`, not SQL. Dropping also removes write amplification during bulk EVIDENCE inserts. |
| 5 | create `ix_evidence_unit_task_id` | the single-column FK index the model actually declares |
| 6 | create `ix_research_question_task_id` | declared by the model, never created; 52 rows so near-free |

Plain `CREATE INDEX`, not `CONCURRENTLY`: the affected tables hold 5,190 / 5,966 / 7,328 / 52 rows,
so creation is milliseconds, whereas `CONCURRENTLY` requires escaping the migration transaction and
can leave `INVALID` indexes behind.

**Verification**

| Check | Result |
|---|---|
| `alembic upgrade head` | exit 0 |
| `alembic check` | **exit 0 — "No new upgrade operations detected"** |
| `alembic downgrade -1` | exit 0; restores exactly `ix_claim_evidence_user_asset_id` + `ix_evidence_unit_task_topical` and nothing else |
| re-`upgrade` after downgrade | exit 0, check still clean (guards are idempotent) |
| `upgrade head` from an **empty** database | 22 migrations, check clean |
| `pytest` | **764 passed, 4 skipped** — byte-identical to the Phase 1 baseline |
| `vitest` / `tsc` / `ruff` / ontology lint | 129 passed / exit 0 / exit 0 / exit 0 |

Production was never contacted; all verification ran on a disposable `postgres:16-alpine`
(`pf-p15-db`, `127.0.0.1:15433`, no bind mounts, removed afterwards).

**Numbering note.** This migration took revision `0022`, which
`docs/plans/PAPERFORGE_P0_REMEDIATION_PLAN.md` had reserved for Phase 2. The plan was updated: Phase
2–5 now allocate `0023`–`0026`.

**Rollback:** `alembic downgrade 0021_question_locking`, then `git revert 4451d46`. Verified to
restore the exact prior index set.

### 15.2 B-2 — remote and CI: RESOLVED

**Outcome:** `origin` = `github.com/jacobzhuu/paperforge`, pushed as a fast-forward
`a498f20..61c656e`, **CI run 31298257690 green on all four jobs**.

#### 15.2.1 The repository was not new — a correction to the audit

The remote already held **24 commits** through 2026-07-27, with a **passing** CI history. The audit's
"the project is not under version control / CI has never run" was inferred from the local absence of
`.git` without checking for a remote. Corrected in `PAPERFORGE_DEEP_AUDIT.md` §1.3.

What actually happened: the working tree lost its `.git` directory some time after `a498f20`, and
roughly ten days of work continued unversioned. `git init` therefore produced a history with **no
common ancestor** — `git merge-base` returned nothing.

The real finding is not "CI was never set up" but **"CI stopped seeing the code."** That is exactly
why the Ruff failure (§6) and the index drift (§9) accumulated undetected: both post-date `a498f20`.

#### 15.2.2 Graft, not force-push

A force-push would have destroyed 24 commits. Instead the local work was replayed onto the real
history, with operator approval:

```
a498f20  (24 commits of preserved history)
  └─ 728c51f  chore: import unversioned work 2026-07-27..2026-08-06 + CI baseline
       └─ 2a16e56  docs: add Phase 1 engineering baseline report
            └─ 6d4ac1c  db: align index definitions with ORM models (0022)
                 └─ f82e940  docs: record Phase 1.5 baseline closure
                      └─ 61c656e  chore: ignore debug probe files
```

**Integrity check:** the grafted tree hash `d4f403d9…` is **byte-identical** to the pre-graft tree —
nothing was lost or altered. A `pre-graft-backup` branch (`d1e870c`) was retained locally.

Two files present at `a498f20` were removed, with approval:
`apps/web/components/projects/new-project-wizard.tsx` (13.8 KB; zero references remained after the
project-centric rework — content still reachable at `a498f20`) and `apps/web/_probe_ovr.txt` (7-byte
debug leftover). `_probe_*` was added to `.gitignore` so that class cannot recur.

#### 15.2.3 Push mechanics

Three obstacles, all resolved:

| Obstacle | Resolution |
|---|---|
| `gh` not installed | installed v2.97.0 to `~/.local/bin` (userspace, no sudo) |
| `fatal: could not read Username` | `gh auth login` does not wire git — `gh auth setup-git` was required |
| `refusing to allow an OAuth App to … workflow … without workflow scope` | the unversioned work legitimately changed `.github/workflows/` (added `macos-host` + `multiarch-images`, added `ubuntu-compat.yml`, pinned runners to `ubuntu-22.04`, added `pnpm test`); operator ran `gh auth refresh -s workflow` |

#### 15.2.4 CI result — run 31298257690 @ `61c656e`

| Job | Result | Note |
|---|---|---|
| `backend` | **SUCCESS** | |
| `frontend` | **SUCCESS** | |
| `macos-host` | **SUCCESS** | first execution ever |
| `multiarch-images` | **SUCCESS** | first execution ever |

Backend steps of specific interest:

| Step | Result |
|---|---|
| Ruff | **SUCCESS** — the failure fixed in §6 |
| Alembic migration | **SUCCESS** |
| **Migration drift check** (`alembic check`) | **SUCCESS** — the blocker fixed in §15.1 |
| Tests | **SUCCESS** — `764 passed, 4 skipped` in 610.76s |

CI's `764 passed, 4 skipped` is **identical** to the local figure in §8.1, confirming the
CI-equivalence claim made there was accurate.

#### 15.2.5 Residual note

`gh` stores its token in plain text at `~/.config/gh/hosts.yml` (it warns about this at login). That
is `gh`'s standard behaviour on a host without a secret store; the file is user-readable only. Worth
knowing, not a blocker.

Re-checked on 2026-08-09; the prerequisites are unchanged:

| Prerequisite | State |
|---|---|
| `git remote` | none configured |
| `gh` CLI | **not installed** |
| SSH keys | none |
| Credential helper | none |
| `GH_TOKEN` / `GITHUB_TOKEN` | not set |
| Outbound to GitHub | **OK** — `github.com` and `api.github.com` both return 200, and `git ls-remote` over HTTPS succeeds, via the proxy already configured in the environment |

So the network path is fine; only the identity is missing. Agreed approach: the operator installs
and authenticates `gh`, supplies the URL of an existing **public** repository, and the push is then
performed from this session. Public visibility means CI status can be read back through the
unauthenticated GitHub Actions API.

Nothing about B-2 has been faked: no remote was added, no push attempted, no CI run claimed.
