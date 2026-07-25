from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# 文献域（方案 §4.3）。scholarly_work 系四表迁移自 DeepSearch（字段基本不变，去 source_priority
# 等 lane 相关字段）；library_entry / literature_card / document_file 为新表。


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class ScholarlyWork(Base, TimestampMixin):
    __tablename__ = "scholarly_work"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    canonical_title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    abstract: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer, index=True)
    publication_date: Mapped[str | None] = mapped_column(String(32))
    work_type: Mapped[str | None] = mapped_column(String(64))
    venue_name: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    pmid: Mapped[str | None] = mapped_column(String(32), index=True)
    pmcid: Mapped[str | None] = mapped_column(String(32))
    arxiv_id: Mapped[str | None] = mapped_column(String(64), index=True)
    openalex_id: Mapped[str | None] = mapped_column(String(32), index=True)
    semantic_scholar_id: Mapped[str | None] = mapped_column(String(80))
    corpus_id: Mapped[str | None] = mapped_column(String(32))
    is_retracted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oa_status: Mapped[str | None] = mapped_column(String(32))
    license: Mapped[str | None] = mapped_column(String(64))
    # 引文影响力信号：雪球种子排序与 OA 全文预算依赖（设计 §3.1 snowball）。
    citation_count: Mapped[int | None] = mapped_column(Integer)
    influential_citation_count: Mapped[int | None] = mapped_column(Integer)


class WorkIdentifier(Base):
    __tablename__ = "work_identifier"
    __table_args__ = (UniqueConstraint("work_id", "id_type", "id_value"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    id_type: Mapped[str] = mapped_column(String(32), nullable=False)
    id_value: Mapped[str] = mapped_column(String(255), nullable=False)


class WorkUrl(Base):
    __tablename__ = "work_url"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_type: Mapped[str | None] = mapped_column(String(32))
    is_oa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WorkAuthor(Base):
    __tablename__ = "work_author"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    author_name: Mapped[str] = mapped_column(Text, nullable=False)
    author_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_affiliation: Mapped[str | None] = mapped_column(Text)


class ScholarlyHttpCache(Base):
    """学术 API 响应缓存（迁移自 DeepSearch scholarly_http_cache，保留表结构）。

    存原始响应体而非 JSON：arXiv 返回 Atom XML，Europe PMC/OpenAlex 返回 JSON，
    统一按字节缓存才能覆盖五源。TTL 由读取方按 ttl_hours 判定（见 cache.py）。
    OA 全文 PDF **不**进本表。
    """

    __tablename__ = "scholarly_http_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    provider_name: Mapped[str | None] = mapped_column(String(32), index=True)


class LibraryEntry(Base, TimestampMixin):
    __tablename__ = "library_entry"
    __table_args__ = (UniqueConstraint("project_id", "bibtex_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    rank_reason_json: Mapped[dict | None] = mapped_column(JSONB)
    user_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    added_via: Mapped[str] = mapped_column(String(32), nullable=False)
    bibtex_key: Mapped[str | None] = mapped_column(String(128))
    # R1：verified_at 为空的 entry 不进入写作白名单。
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiteratureCard(Base, TimestampMixin):
    __tablename__ = "literature_card"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    summary: Mapped[str | None] = mapped_column(Text)
    contributions_json: Mapped[list | None] = mapped_column(JSONB)
    methods_json: Mapped[list | None] = mapped_column(JSONB)
    results_json: Mapped[list | None] = mapped_column(JSONB)
    limitations_json: Mapped[list | None] = mapped_column(JSONB)
    quotable_points_json: Mapped[list | None] = mapped_column(JSONB)
    fulltext_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extraction_model: Mapped[str | None] = mapped_column(String(128))
    # source_hash 支持跨项目缓存复用（方案 §4.3）。
    source_hash: Mapped[str | None] = mapped_column(String(80), index=True)


class DocumentFile(Base, TimestampMixin):
    __tablename__ = "document_file"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # oa_pdf|html|xml
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128))
    bytes: Mapped[int | None] = mapped_column(Integer)
    fetched_from_url: Mapped[str | None] = mapped_column(Text)
