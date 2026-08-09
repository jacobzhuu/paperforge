# Problem-driven review evaluation

This directory implements the M-G protocol without claiming unreviewed model output as
human gold.

- **G1**: `fixtures/g1_annotation_slots.json` contains 20 empty annotation slots. An
  annotator must add an OA source URL, source hash, section spans, and table/equation/figure
  objects, then change `annotation_status` to `expert_verified`. Evaluation code must reject
  any other status.
- **G2**: `fixtures/g2_topics.json` seeds five domains and the required comparison axes.
  Sub-questions and expected stance decisions are intentionally marked `needs_expert_review`.
- **G3**: `fixtures/g3_ablation_manifest.json` fixes three conditions for each topic:
  `legacy`, `fulltext_only`, and `problem_driven`. Use the same selected library, model roles,
  and token ceiling in all three conditions.

The run result JSON is a list of:

```json
{"topic_id":"g2-01","condition":"problem_driven","metrics":{"4_core_claim_evidence_coverage":0.9},"input_tokens":1000,"output_tokens":400,"wall_seconds":25}
```

Create blind packets and keep the key away from reviewers:

```bash
uv run python -m evals.review_depth.anonymize \
  --manifest evals/review_depth/fixtures/g3_ablation_manifest.json \
  --source-root evals/review_depth/generated \
  --output-root evals/review_depth/blind \
  --key-output /secure/path/blind-key.json
```

Five experts score depth, organization, evidence, criticality, credibility, and readability
from 1–5 using `fixtures/expert_score_template.json`. Analyze automatic metrics, cost, wall
time, and paired Wilcoxon tests:

```bash
uv run python -m evals.review_depth.analysis \
  --runs results/runs.json --scores results/scores.json \
  --blind-key /secure/path/blind-key.json --output results/report.json
```

Required release checks are metric 4/7/9/10 deltas, token and wall-time deltas, mean expert
score ≥3.8, and a paired comparison against `legacy`. Metrics 7, 8, and 10 are lower-is-better.

