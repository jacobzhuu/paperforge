"""Export an immutable 60-item blind packet from three isolated project snapshots."""

import argparse
import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from db.models.paper import ClaimEvidenceAnchor, QualityReportRecord
from db.session import make_engine, make_session_factory
from sqlalchemy import select

from evals.agent_writing.run import evaluation_url


async def prepare(projects, output):
    if len(projects) != 3:
        raise ValueError("exactly three independent project topics are required")
    engine = make_engine(evaluation_url())
    cases, private_mapping = [], {}
    try:
        async with make_session_factory(engine)() as session:
            for index, project_id in enumerate(projects):
                report_id = await session.scalar(
                    select(QualityReportRecord.id)
                    .where(QualityReportRecord.project_id == project_id)
                    .order_by(QualityReportRecord.created_at.desc())
                    .limit(1)
                )
                rows = list(
                    (
                        await session.scalars(
                            select(ClaimEvidenceAnchor)
                            .where(
                                ClaimEvidenceAnchor.project_id == project_id,
                                ClaimEvidenceAnchor.quality_report_id == report_id,
                                ClaimEvidenceAnchor.evidence_excerpt.is_not(None),
                            )
                            .order_by(ClaimEvidenceAnchor.claim_hash)
                        )
                    ).all()
                )
                unique = {}
                for row in rows:
                    unique.setdefault(row.claim_hash, row)
                selected = list(unique.values())[:20]
                if len(selected) < 20:
                    raise ValueError(f"project {project_id} needs 20 distinct located review cases")
                for row in selected:
                    identifier = hashlib.sha256(
                        f"{row.claim_hash}:{row.evidence_hash}".encode()
                    ).hexdigest()[:16]
                    cases.append(
                        {
                            "id": identifier,
                            "topic": f"topic-{index + 1}",
                            "split": "holdout" if index == 2 else "development",
                            "claim": row.claim_text,
                            "evidence": row.evidence_excerpt,
                            "page": row.source_page,
                            "section": row.source_section,
                            "source_kind": row.source_kind,
                            "source_hash": row.evidence_hash,
                            "label": None,
                            "reviewer": None,
                            "rationale": None,
                        }
                    )
                    private_mapping[identifier] = {
                        "project_id": str(project_id),
                        "anchor_id": str(row.id),
                        "evidence_id": str(row.evidence_unit_id) if row.evidence_unit_id else None,
                    }
        output.mkdir(parents=True, exist_ok=False)
        payload = json.dumps(cases, ensure_ascii=False, indent=2)
        (output / "cases.json").write_text(payload)
        (output / "mapping.private.json").write_text(json.dumps(private_mapping, indent=2))
        (output / "manifest.json").write_text(
            json.dumps(
                {
                    "version": "upgrade-review-v1",
                    "case_count": len(cases),
                    "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    "human_review_complete": False,
                    "labels": ["supported", "contradicted", "insufficient"],
                    "note": ("Candidates from actual manuscripts, not human gold. "
                             "Do not infer labels from IDs."),
                },
                indent=2,
            )
        )
        (output / "README.md").write_text(
            "# 人工盲审材料\n\n仅将 cases.json 交给评审者，勿附 mapping.private.json 或机器判断。\n"
            "逐条核对论断、实验条件、数字归属、来源位置，填写 label、reviewer、rationale。\n"
            "证据不足或含糊时标 insufficient，不猜测；可查原文再判断。\n"
            "第三主题为保留测试集，在提示词与规则固定前不得用于调优。\n"
            "这些候选未保证覆盖所有错误类别，应由评审者补充数字/表格/跨节结论案例。\n"
        )
        return {"cases": len(cases), "human_review_complete": False}
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=uuid.UUID, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(prepare(args.project_id, args.output))))
