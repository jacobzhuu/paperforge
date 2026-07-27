from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# 项目/任务/论文结构/用户素材/检索留痕/产物与成本（方案 §4.3）。


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class PaperProject(Base, TimestampMixin):
    __tablename__ = "paper_project"
    __table_args__ = (Index("ix_paper_project_owner_created", "owner_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    paper_type: Mapped[str] = mapped_column(String(16), nullable=False)  # review|original
    writing_mode: Mapped[str] = mapped_column(String(16), nullable=False)  # auto|assisted
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)  # zh|en
    venue_template: Mapped[str | None] = mapped_column(String(64))
    citation_style: Mapped[str] = mapped_column(String(32), default="author_year", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    scope_json: Mapped[dict | None] = mapped_column(JSONB)


class GenerationJob(Base):
    __tablename__ = "generation_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    # kind: search|ingest|cards|outline|write|compile|visual|full
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # status: queued|running|succeeded|failed|cancelled（Draft-first：无「拒绝产出」终态）
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    checkpoint_json: Mapped[dict | None] = mapped_column(JSONB)
    error_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    """SSE 进度源（借鉴 DeepSearch task_event）。"""

    __tablename__ = "job_event"
    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_job_event_job_id_seq"),
        Index("ix_job_event_job_id_seq", "job_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_job.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserAsset(Base, TimestampMixin):
    __tablename__ = "user_asset"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    # kind: dataset|result_table|figure|method_note|code|bib
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    object_key: Mapped[str | None] = mapped_column(String(512))
    # 确定性解析结果（数字硬规则的注入来源；不编造数据）。
    parsed_json: Mapped[dict | None] = mapped_column(JSONB)


class VisualAsset(Base, TimestampMixin):
    """版本化视觉资产；每次重生成都创建新行，禁止原地覆盖 rendition。"""

    __tablename__ = "visual_asset"
    __table_args__ = (
        Index("ix_visual_asset_project_kind_status", "project_id", "kind", "generation_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    generation_status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    target_section_key: Mapped[str | None] = mapped_column(String(64))
    suggested_block_index: Mapped[int | None] = mapped_column(Integer)
    figure_label: Mapped[str] = mapped_column(String(128), nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    renditions_json: Mapped[dict | None] = mapped_column(JSONB)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("visual_asset.id", ondelete="SET NULL"), index=True
    )


class VisualSourceAsset(Base):
    __tablename__ = "visual_source_asset"
    __table_args__ = (
        UniqueConstraint("visual_id", "user_asset_id", name="uq_visual_source_dependency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    visual_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visual_asset.id", ondelete="CASCADE"), index=True
    )
    user_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_asset.id", ondelete="RESTRICT"), index=True
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VisualGenerationAttempt(Base):
    __tablename__ = "visual_generation_attempt"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    visual_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visual_asset.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    output_width: Mapped[int | None] = mapped_column(Integer)
    output_height: Mapped[int | None] = mapped_column(Integer)
    usage_json: Mapped[dict | None] = mapped_column(JSONB)
    cost_estimate: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Outline(Base, TimestampMixin):
    __tablename__ = "outline"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    tree_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)


class PaperDocument(Base, TimestampMixin):
    __tablename__ = "paper_document"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    outline_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outline.id"))
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)


class PaperSection(Base):
    __tablename__ = "paper_section"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_document.id", ondelete="CASCADE"), index=True
    )
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_key: Mapped[str | None] = mapped_column(String(64))
    order_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_ir_json: Mapped[dict | None] = mapped_column(JSONB)  # PaperIR Section blocks
    cite_keys_json: Mapped[list | None] = mapped_column(JSONB)  # R2 白名单校验对象
    asset_refs_json: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="generated", nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CitationUsage(Base):
    __tablename__ = "citation_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_section.id", ondelete="CASCADE")
    )
    work_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scholarly_work.id", ondelete="RESTRICT"))
    cite_key: Mapped[str] = mapped_column(String(128), nullable=False)
    context_snippet: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SearchRun(Base):
    """检索留痕（轻量复现，非账本）—— 取代旧 occurrence 守恒（方案 §3.3）。"""

    __tablename__ = "search_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    filters_json: Mapped[dict | None] = mapped_column(JSONB)
    hit_count: Mapped[int | None] = mapped_column(Integer)
    retrieved_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExportArtifact(Base):
    __tablename__ = "export_artifact"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    document_version: Mapped[int | None] = mapped_column(Integer)
    # format: latex_zip|pdf|docx|bibtex|markdown|markdown_bundle|compile_log
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    compile_log_key: Mapped[str | None] = mapped_column(String(512))
    content_hash: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LlmCallLog(Base):
    """LLM 成本记账（简化自旧 token_ledger 设计）。"""

    __tablename__ = "llm_call_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper_project.id"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("generation_job.id"))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_estimate: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # 失败调用也要留痕：否则成本面板里「失败」与「零 token 成功」无法区分。
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
