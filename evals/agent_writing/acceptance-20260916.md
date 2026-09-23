# Writing Agent upgrade acceptance — 2026-09-16

## Automated validation

- Full regression: **1351 passed, 8 skipped**, 1 upstream Starlette deprecation warning
  (468.40 seconds), after the checkpoint row-lock fix.
- Command: `PAPERFORGE_TEST_DATABASE_URL=<isolated paperforge_agent_test database> REDIS_URL=<test Redis> uv run pytest -q packages services evals/agent_writing/test_report.py evals/review_depth/tests evals/synthesis_shadow/tests`.
- Shared Redis limiter integration tests: **3 passed**, run separately with
  `CAPACITY_TEST_REDIS_URL`. These account for three of the full-run skips; the other
  five require a real texd test URL (three) or local pandoc (two).
- Checkpoint budget, job control and event-notification regression: **12 passed**.
- `ruff check .`, `git diff --check`, `bash -n scripts/dev`, and ontology literal lint passed.
- Empty isolated database migration to `0031_writing_context` and Alembic drift check passed.

## Deployment acceptance

- Final deployment: `paperforge-deploy-20260916-220357`, local web port `3007`,
  deployment-specific Redis database `3`, deployed using `./scripts/dev restart`.
- Public URL: https://paperforge-linux.tail53ab99.ts.net.
- Required Funnel target check and byte-for-byte public/local `/login` comparison passed.
  Login response SHA-256: `7c07a5eed0c09e3f5e2bcc2c3bae1cd1d0a1ea242a07f77e90f9e3299ae297f8`.
- Health, offline texd longtable preflight, MinIO round trip and image-provider route checks passed.
- Both API instances and all three workers match local source hashes for
  `paperforge_worker.context`, `paperforge_worker.pipelines.document`,
  `paperforge_worker.orchestration.writing_graph`, and `llm_runtime.runner`.
- The deployed checkpoint-lock implementation SHA-256 is
  `b8a40303a0ad619ee183b2b43b3ee973fc9ccd214e6f5d5dcf30ec2116b71bdc`.
- Runtime database migration is `0031_writing_context`.
- Runtime defaults verified: mode `legacy`, DAG width `2`, call budget `2000`, reserved
  token budget `50000000`, wall deadline `7200` seconds.
- An intermediate build missed the final checkpoint lock change; it was superseded by the
  final deployment above. Database headroom initially blocked the replacement. The existing
  `reap --apply` script removed only superseded deployments verified drained twice, and the
  replacement then passed admission. Deployment `paperforge-deploy-20260916-215014` remains
  available; no deployment owning verified in-flight work was removed.

## Evidence limits

No paid real-model A/B/C comparison, blind expert scoring or capacity soak was performed.
Parallel mode remains disabled by default. Automated scheduling/recovery tests and deployment
checks do not demonstrate a measured improvement in manuscript quality or model latency.
See [the implementation and evaluation guide](../../docs/agent-writing-upgrade.md).
