"""Review candidates are private to their project and require explicit adoption."""

import copy
import uuid
from typing import Annotated

from db import document_snapshot_hash, get_writing_whitelist, latest_document, list_sections
from db.models.paper import ExportArtifact, GenerationJob, PaperDocument, PaperProject
from db.models.revision import DraftRevision
from db.repositories.revision_recovery import MESSAGES, recovery_block
from db.repositories.revisions import clone_document
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperforge_api.deps import authorize_project_request, get_authorized_project, get_session

router = APIRouter(
    prefix="/api/v1", tags=["revisions"], dependencies=[Depends(authorize_project_request)]
)
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


@router.get("/projects/{project_id}/revisions")
async def revisions(project_id: str, session: Session):
    project = await get_authorized_project(session, project_id)
    rows = await session.scalars(
        select(DraftRevision)
        .where(DraftRevision.project_id == project.id)
        .order_by(DraftRevision.created_at.desc())
        .limit(20)
    )
    result = []
    for row in rows:
        document = await session.get(PaperDocument, row.candidate_document_id)
        job = await session.get(GenerationJob, row.job_id)
        artifacts = await session.scalars(
            select(ExportArtifact).where(
                ExportArtifact.project_id == project.id,
                ExportArtifact.document_version == document.version,
            )
        )
        reason = (
            recovery_block(row, job.checkpoint_json or {})
            if job.status in {"paused", "failed"}
            else None
        )
        result.append(
            {
                "id": str(row.id),
                "status": row.status,
                "job_id": str(row.job_id),
                "job_status": job.status,
                "job_error": job.error_json,
                "can_resume": job.status in {"paused", "failed"} and reason is None,
                "resume_block_reason": MESSAGES.get(reason),
                "recovery_message": MESSAGES.get(
                    reason or (job.error_json or {}).get("code")
                ),
                "resume_block_code": reason,
                "source_document_id": str(row.source_document_id),
                "candidate_document_id": str(row.candidate_document_id),
                "review": (
                    (job.checkpoint_json or {}).get("manuscript_review_progress") or row.review_json
                    if row.status in {"pending", "running"}
                    else row.review_json
                ),
                "artifacts": [{"id": str(a.id), "format": a.format} for a in artifacts],
                "created_at": row.created_at.isoformat(),
            }
        )
    return {"data": result}


async def locked_revision(session, project_id, revision_id):
    project = await get_authorized_project(session, project_id)
    await session.scalar(
        select(PaperProject.id).where(PaperProject.id == project.id).with_for_update()
    )
    row = await session.scalar(
        select(DraftRevision)
        .where(DraftRevision.id == revision_id, DraftRevision.project_id == project.id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "修订候选不存在")
    if row.status not in {"ready", "needs_review"}:
        raise HTTPException(409, "候选尚未完成或已经处理")
    return project, row


@router.post("/projects/{project_id}/revisions/{revision_id}/accept")
async def accept(project_id: str, revision_id: uuid.UUID, session: Session):
    project, row = await locked_revision(session, project_id, revision_id)
    current = await latest_document(session, project.id)
    if current is None or current.id != row.source_document_id:
        raise HTTPException(409, "当前稿件已变化，请重新生成修订候选")
    await session.scalar(
        select(PaperDocument.id).where(PaperDocument.id == current.id).with_for_update()
    )
    if document_snapshot_hash(await list_sections(session, current.id)) != row.source_hash:
        raise HTTPException(409, "正文已编辑，请重新生成修订候选；已有编辑不会被覆盖")
    active = await session.scalar(
        select(GenerationJob.id)
        .where(
            GenerationJob.project_id == project.id, GenerationJob.status.in_(["running", "queued"])
        )
        .limit(1)
    )
    if active:
        raise HTTPException(409, "请等待当前任务结束后采纳")
    candidate = await session.get(PaperDocument, row.candidate_document_id)
    from paper_ir.schema import Section

    sections = await list_sections(session, candidate.id)
    whitelist = set(await get_writing_whitelist(session, project.id))
    for section in sections:
        Section.model_validate(section.body_ir_json)
        if any(key not in whitelist for key in (section.cite_keys_json or [])):
            raise HTTPException(409, "引用白名单已变化，请重新核验")
    # Create a new current version, preserving both downloadable snapshots and their reports.
    adopted = await clone_document(session, candidate, status="draft")
    await copy_delivery(session, project.id, candidate, adopted)
    row.status = "accepted"
    return {"document_id": str(adopted.id), "version": adopted.version}


@router.post("/projects/{project_id}/revisions/{revision_id}/discard")
async def discard(project_id: str, revision_id: uuid.UUID, session: Session):
    _, row = await locked_revision(session, project_id, revision_id)
    row.status = "discarded"
    return {"status": "discarded"}


async def copy_delivery(session, project_id, candidate, adopted):
    """The adopted body is byte-identical; retain its reviewed report and downloadable files."""
    from db import create_quality_report, list_claim_evidence, replace_claim_evidence
    from db.models.paper import ClaimEvidenceAnchor, QualityReportRecord

    snapshot = document_snapshot_hash(await list_sections(session, adopted.id))
    reports = await session.scalars(
        select(QualityReportRecord).where(
            QualityReportRecord.document_id == candidate.id,
            QualityReportRecord.stale.is_(False),
            QualityReportRecord.paper_snapshot_hash == snapshot,
        )
    )
    report_ids = {}
    for report in reports:
        copied = await create_quality_report(
            session,
            project_id=project_id,
            document_id=adopted.id,
            document_version=adopted.version,
            paper_snapshot_hash=snapshot,
            quality_profile=report.quality_profile,
            review_style=report.review_style,
            readiness_status=report.readiness_status,
            blockers=copy.deepcopy(report.blockers_json),
            warnings=copy.deepcopy(report.warnings_json),
            scores=copy.deepcopy(report.scores_json),
            metrics=copy.deepcopy(report.metrics_json),
            layout_checks=copy.deepcopy(report.layout_checks_json),
        )
        report_ids[report.id] = copied.id
        anchors = await list_claim_evidence(session, project_id, quality_report_id=report.id)
        await replace_claim_evidence(
            session,
            quality_report_id=copied.id,
            project_id=project_id,
            document_id=adopted.id,
            anchors=[
                {
                    column.name: getattr(anchor, column.name)
                    for column in ClaimEvidenceAnchor.__table__.columns
                    if column.name
                    not in {"id", "created_at", "quality_report_id", "project_id", "document_id"}
                }
                for anchor in anchors
            ],
        )
    artifacts = await session.scalars(
        select(ExportArtifact).where(
            ExportArtifact.project_id == project_id,
            ExportArtifact.document_version == candidate.version,
            ExportArtifact.paper_snapshot_hash == snapshot,
        )
    )
    for artifact in artifacts:
        session.add(
            ExportArtifact(
                project_id=project_id,
                document_version=adopted.version,
                quality_report_id=report_ids.get(artifact.quality_report_id),
                **{
                    name: getattr(artifact, name)
                    for name in (
                        "export_run_id",
                        "format",
                        "object_key",
                        "compile_log_key",
                        "content_hash",
                        "quality_profile",
                        "readiness_status",
                        "paper_snapshot_hash",
                    )
                },
            )
        )
