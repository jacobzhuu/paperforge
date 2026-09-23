"""Research results and proposals, separate from graph execution checkpoints."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ResearchAnalysis(Base):
    __tablename__ = "research_analysis"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_job.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending")
    input_json: Mapped[dict] = mapped_column(JSONB)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    proposal_json: Mapped[dict | None] = mapped_column(JSONB)
    committed_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_document.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CitationShadow(Base):
    __tablename__ = "citation_shadow"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_job.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    payload_json: Mapped[dict] = mapped_column(JSONB)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
