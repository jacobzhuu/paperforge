"""Snapshot-bound background revisions; candidates never become current implicitly."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DraftRevision(Base):
    __tablename__ = "draft_revision"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    source_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_job.id"), unique=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_job.id"), unique=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper_document.id"))
    candidate_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_document.id"), unique=True
    )
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    review_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
