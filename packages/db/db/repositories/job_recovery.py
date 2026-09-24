"""Conservative recovery shared by dispatcher and executor."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from db.models.library import LiteraturePdfUpload
from db.models.paper import GenerationJob


async def mark_execution_interrupted(session, job, intent):
    is_pdf = "pdf" in intent.function
    changed = await session.scalar(
        update(GenerationJob)
        .where(GenerationJob.id == job.id, GenerationJob.status.in_(["queued", "running"]))
        .values(
            status="failed" if is_pdf else "paused",
            error_json={"reason": "execution_interrupted", "recovery": "resume_or_retry"},
            finished_at=datetime.now(UTC),
        )
        .returning(GenerationJob.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        return False
    await session.refresh(job)
    if is_pdf and intent.kwargs_json.get("upload_id"):
        matching = "match" in intent.function
        await session.execute(
            update(LiteraturePdfUpload)
            .where(
                LiteraturePdfUpload.id == uuid.UUID(intent.kwargs_json["upload_id"]),
                LiteraturePdfUpload.project_id == job.project_id,
                LiteraturePdfUpload.status.in_(
                    ["matching"] if matching else ["parsing", "extracting"]
                ),
            )
            .values(
                status="match_failed" if matching else "parse_failed",
                error_json={"reason": "execution_interrupted", "job_id": str(job.id)},
            )
        )
    return True
