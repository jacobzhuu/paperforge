# Research-intent homepage acceptance — 2026-09-24

The homepage now starts from a research goal or uploaded materials. A persistent intake job combines intent interpretation and scope planning in one planner call. Assisted mode stops at a plan; automatic mode continues only after direction is resolved and original-paper material checks pass.

## Behavior and interfaces

- Default toolbar: upload, independent fast-draft switch (off), start research. Current collaboration mode and default language remain visible; settings use a keyboard-accessible dialog.
- `POST /projects` accepts optional `intake` overrides. Legacy clients keep their flow; opted-in clients default to assisted mode and Chinese.
- `GET /projects/{id}/intake` returns recoverable understanding, clarification questions, material provenance and type-lock status. `POST` accepts an input version, optional goal/answer and manual overrides, and returns a durable Job.
- Version checks and the project job lock prevent duplicate submissions and stale results. Manual overrides survive subsequent model calls. Pending understanding blocks downstream execution. Type changes lock after downstream jobs or artifacts exist.
- Scope and intake state share the existing JSON storage. No new migration is required for intake. Existing execution-profile changes in the workspace are preserved.
- Project responses expose a compact intake projection; detailed answers and input records remain in the project-specific intake endpoint.

## Local verification

- Frontend: 158 tests pass across 32 files; TypeScript check and production build pass. Final intake panel regression: 5 tests pass.
- Worker/planning/quality regression: 84 tests pass; final intake planner regression: 8 tests pass.
- Real PostgreSQL project/API regression: 91 tests pass. Final intake-only regression, including safe opt-in defaults: 11 tests pass.
- Job serialization, execution profile and deployment scripts: 34 tests pass.
- Browser: 4 Playwright tests pass (desktop keyboard/focus behavior, mobile settings layout, independent quick-draft switch and reduced motion).
- Ruff and whitespace checks pass.
- PostgreSQL tests used an isolated temporary container and database, not production data.

## Deployment verification

Deployment `paperforge-deploy-20260924-104942` is live at https://paperforge-linux.tail53ab99.ts.net (local port 3009). `./scripts/dev restart` passed service health, Funnel target and byte-for-byte public `/login` verification. Login SHA-256: `b3cd16b8346611ebee5d2a760f0ab8f1d5f651e1da966ff7492ab3db09fd3f4b`.

Real authenticated browser acceptance used the configured planner (no model mock):

- Homepage has no default combobox, displays assisted/Chinese defaults and starts a durable task.
- Explicit English original-research intent produces a persisted, editable summary. Missing materials remain visible; no writing task starts.
- A Chinese follow-up preserves manually pinned English.
- An ambiguous goal asks only research-direction questions. Downstream generation returns HTTP 409 until clarified.
- Refresh restores the summary; type and language remain editable before downstream work. Mobile clarification has no horizontal overflow; no browser page errors occurred.

Initial public acceptance exposed an intermittent schema failure. The prompt now scopes the planning reference and ends with the final JSON Schema. Strict validation reports field/type diagnostics without user input. Another check exposed an unnecessary submission-target question; clarification instructions now explicitly exclude submission settings. Final public checks passed after both fixes.

Synthetic projects were soft-deleted; the acceptance account was disabled and its sessions revoked. Screenshots: `artifacts/intake-public-home.png`, `artifacts/intake-public-understanding.png`, `artifacts/intake-public-clarification-mobile.png`.

 The first restart was blocked by the database connection budget. Three obsolete deployments were independently verified as drained twice and reaped with the repository script; the live deployment and two deployments with in-progress markers were retained.
