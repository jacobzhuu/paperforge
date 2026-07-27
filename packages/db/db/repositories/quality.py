"""投稿质量报告与论断证据仓储。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import ClaimEvidenceAnchor, QualityReportRecord

QUALITY_PROFILES = frozenset({"draft", "submission"})
REVIEW_STYLES = frozenset({"narrative", "systematic"})
READINESS_STATUSES = frozenset(
    {"draft", "needs_revision", "preflight_ready", "submission_ready", "unassessed"}
)


async def create_quality_report(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    paper_snapshot_hash: str,
    quality_profile: str,
    review_style: str,
    readiness_status: str,
    blockers: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    scores: dict[str, Any],
    metrics: dict[str, Any],
    layout_checks: dict[str, Any] | None = None,
) -> QualityReportRecord:
    if quality_profile not in QUALITY_PROFILES:
        raise ValueError(f"unsupported quality profile: {quality_profile}")
    if review_style not in REVIEW_STYLES:
        raise ValueError(f"unsupported review style: {review_style}")
    if readiness_status not in READINESS_STATUSES:
        raise ValueError(f"unsupported readiness status: {readiness_status}")
    await session.execute(
        update(QualityReportRecord)
        .where(
            QualityReportRecord.project_id == project_id,
            QualityReportRecord.quality_profile == quality_profile,
            QualityReportRecord.stale.is_(False),
        )
        .values(stale=True)
    )
    report = QualityReportRecord(
        project_id=project_id,
        document_id=document_id,
        document_version=document_version,
        paper_snapshot_hash=paper_snapshot_hash,
        quality_profile=quality_profile,
        review_style=review_style,
        readiness_status=readiness_status,
        stale=False,
        blockers_json=blockers,
        warnings_json=warnings,
        scores_json=scores,
        metrics_json=metrics,
        layout_checks_json=layout_checks or {},
    )
    session.add(report)
    await session.flush()
    return report


async def latest_quality_report(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    quality_profile: str | None = None,
) -> QualityReportRecord | None:
    stmt = select(QualityReportRecord).where(QualityReportRecord.project_id == project_id)
    if quality_profile:
        stmt = stmt.where(QualityReportRecord.quality_profile == quality_profile)
    return await session.scalar(stmt.order_by(QualityReportRecord.created_at.desc()).limit(1))


async def get_quality_report(
    session: AsyncSession, report_id: uuid.UUID
) -> QualityReportRecord | None:
    return await session.get(QualityReportRecord, report_id)


async def invalidate_quality_reports_for_document(
    session: AsyncSession, document_id: uuid.UUID
) -> None:
    await session.execute(
        update(QualityReportRecord)
        .where(
            QualityReportRecord.document_id == document_id,
            QualityReportRecord.stale.is_(False),
        )
        .values(stale=True)
    )


async def invalidate_quality_reports_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """Invalidate current reports when publication metadata changes outside the document IR."""
    await session.execute(
        update(QualityReportRecord)
        .where(
            QualityReportRecord.project_id == project_id,
            QualityReportRecord.stale.is_(False),
        )
        .values(stale=True)
    )


async def replace_claim_evidence(
    session: AsyncSession,
    *,
    quality_report_id: uuid.UUID,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    anchors: list[dict[str, Any]],
) -> int:
    await session.execute(
        delete(ClaimEvidenceAnchor).where(
            ClaimEvidenceAnchor.quality_report_id == quality_report_id
        )
    )
    for anchor in anchors:
        session.add(
            ClaimEvidenceAnchor(
                quality_report_id=quality_report_id,
                project_id=project_id,
                document_id=document_id,
                **anchor,
            )
        )
    await session.flush()
    return len(anchors)


async def list_claim_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    quality_report_id: uuid.UUID | None = None,
    core_only: bool = False,
) -> list[ClaimEvidenceAnchor]:
    stmt = select(ClaimEvidenceAnchor).where(ClaimEvidenceAnchor.project_id == project_id)
    if quality_report_id:
        stmt = stmt.where(ClaimEvidenceAnchor.quality_report_id == quality_report_id)
    if core_only:
        stmt = stmt.where(ClaimEvidenceAnchor.is_core.is_(True))
    return list(
        (
            await session.scalars(
                stmt.order_by(
                    ClaimEvidenceAnchor.section_key,
                    ClaimEvidenceAnchor.created_at,
                )
            )
        ).all()
    )


async def set_claim_manual_status(
    session: AsyncSession,
    anchor: ClaimEvidenceAnchor,
    status: str,
) -> ClaimEvidenceAnchor:
    if status not in {"unreviewed", "confirmed", "rejected"}:
        raise ValueError(f"unsupported manual status: {status}")
    anchor.manual_status = status
    await session.flush()
    return anchor


__all__ = [
    "QUALITY_PROFILES",
    "READINESS_STATUSES",
    "REVIEW_STYLES",
    "create_quality_report",
    "get_quality_report",
    "invalidate_quality_reports_for_document",
    "invalidate_quality_reports_for_project",
    "latest_quality_report",
    "list_claim_evidence",
    "replace_claim_evidence",
    "set_claim_manual_status",
]
