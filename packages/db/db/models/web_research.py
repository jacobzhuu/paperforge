"""Project-private web research, deliberately separate from scholarly evidence."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class WebResearchRun(Base, TimestampMixin):
    __tablename__ = "web_research_run"
    __table_args__ = (UniqueConstraint("id", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_job.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[str] = mapped_column(String(24), default="running")
    plan_json: Mapped[dict] = mapped_column(JSONB)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(80))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebResearchSource(Base, TimestampMixin):
    __tablename__ = "web_research_source"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["web_research_run.id", "web_research_run.project_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "url_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64))
    order_no: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="discovered")
    verification_json: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpToolInvocation(Base):
    __tablename__ = "mcp_tool_invocation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["web_research_run.id", "web_research_run.project_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "request_hash", "attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"), index=True)
    tool: Mapped[str] = mapped_column(String(80))
    schema_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    trace_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), default="reserved")
    error: Mapped[str | None] = mapped_column(String(80))
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class McpDailyBudget(Base):
    """Counters survive project deletion and are shared across deployment queues."""

    __tablename__ = "mcp_daily_budget"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
