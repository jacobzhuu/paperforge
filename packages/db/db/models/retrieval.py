"""Content-addressed project embeddings. Portable arrays allow safe extension rollout."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class EvidenceEmbedding(Base):
    __tablename__ = "evidence_embedding"
    __table_args__ = (Index("ix_evidence_embedding_project_model", "project_id", "model"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_project.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence_unit.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(160), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
