from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# 文献域（方案 §4.3）。scholarly_work 系四表迁移自 DeepSearch（字段基本不变，去 source_priority
# 等 lane 相关字段）；library_entry / literature_card / document_file 为新表。

LITERATURE_ROLES = frozenset(
    {"general", "core", "background", "method", "benchmark", "controversy"}
)
DOCUMENT_ACCESS_SCOPES = frozenset({"shared", "private"})
PDF_UPLOAD_STATUSES = frozenset(
    {
        "matching",
        "needs_confirmation",
        "match_failed",
        "parsing",
        "extracting",
        "ready",
        "parse_failed",
        "rejected",
    }
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class ScholarlyWork(Base, TimestampMixin):
    __tablename__ = "scholarly_work"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    # A manifestation (preprint / conference / journal version) points at the
    # single project-facing intellectual-work entity. Roots keep this NULL.
    canonical_work_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="SET NULL"), index=True
    )
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


class WorkVersionRelation(Base, TimestampMixin):
    """Explicit provenance-preserving relation between two manifestations."""

    __tablename__ = "work_version_relation"
    __table_args__ = (
        UniqueConstraint(
            "source_work_id", "target_work_id", "relation_type", name="uq_work_version_relation"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    target_work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    rationale_json: Mapped[dict | None] = mapped_column(JSONB)


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
    __table_args__ = (
        UniqueConstraint("project_id", "bibtex_key"),
        UniqueConstraint("project_id", "work_id", name="uq_library_entry_project_work"),
        CheckConstraint(
            "literature_role IN ('general','core','background','method','benchmark','controversy')",
            name="ck_library_entry_literature_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="RESTRICT"), index=True
    )
    # ``candidate_uncertain`` is 19 characters; keep room for future
    # auditable screening states.
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    rank_reason_json: Mapped[dict | None] = mapped_column(JSONB)
    user_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    literature_role: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    added_via: Mapped[str] = mapped_column(String(32), nullable=False)
    bibtex_key: Mapped[str | None] = mapped_column(String(128))
    # R1：verified_at 为空的 entry 不进入写作白名单。
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EligibilityDecision(Base, TimestampMixin):
    """Project-scoped include/exclude decision with replayable criterion hits."""

    __tablename__ = "eligibility_decision"
    __table_args__ = (
        UniqueConstraint("project_id", "work_id", name="uq_eligibility_project_work"),
        Index("ix_eligibility_project_decision", "project_id", "decision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    criterion_hits_json: Mapped[dict | None] = mapped_column(JSONB)
    anchor_facet_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))


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
    __table_args__ = (
        CheckConstraint(
            "(access_scope = 'shared' AND project_id IS NULL) OR "
            "(access_scope = 'private' AND project_id IS NOT NULL)",
            name="ck_document_file_access_scope_project",
        ),
        Index(
            "uq_document_file_shared_work_content",
            "work_id",
            "content_hash",
            unique=True,
            postgresql_where=text("access_scope = 'shared'"),
        ),
        Index(
            "uq_document_file_private_project_work_content",
            "project_id",
            "work_id",
            "content_hash",
            unique=True,
            postgresql_where=text("access_scope = 'private'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    access_scope: Mapped[str] = mapped_column(String(16), default="shared", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # oa_pdf|html|xml
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128))
    bytes: Mapped[int | None] = mapped_column(Integer)
    fetched_from_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class LiteraturePdfUpload(Base, TimestampMixin):
    """A project-private PDF while metadata matching and parsing are in flight."""

    __tablename__ = "literature_pdf_upload"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_literature_pdf_upload_object_key"),
        CheckConstraint(
            "status IN "
            "('matching','needs_confirmation','match_failed','parsing','extracting',"
            "'ready','parse_failed','rejected')",
            name="ck_literature_pdf_upload_status",
        ),
        Index(
            "ix_literature_pdf_upload_project_status_created",
            "project_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True, nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), default="application/pdf", nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="matching", nullable=False)
    extracted_metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    match_candidates_json: Mapped[list | None] = mapped_column(JSONB)
    matched_work_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="SET NULL"), index=True
    )
    document_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_file.id", ondelete="SET NULL"), index=True
    )
    match_method: Mapped[str | None] = mapped_column(String(64))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    match_metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    error_json: Mapped[dict | None] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FulltextAttempt(Base, TimestampMixin):
    """Per-URL ledger: a failure is observable without failing unrelated works."""

    __tablename__ = "fulltext_attempt"
    __table_args__ = (
        Index("ix_fulltext_attempt_project_work", "project_id", "work_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_job.id", ondelete="SET NULL"), index=True
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)


class DocumentParse(Base, TimestampMixin):
    """Durable parse result; raw objects can be re-used without re-downloading."""

    __tablename__ = "document_parse"
    __table_args__ = (
        UniqueConstraint(
            "document_file_id", "parser_version", name="uq_document_parse_file_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_file.id", ondelete="CASCADE"), index=True
    )
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    error_json: Mapped[dict | None] = mapped_column(JSONB)


class DocumentChunk(Base, TimestampMixin):
    """Section/page/object-addressable source chunks used by cards and writing."""

    __tablename__ = "document_chunk"
    __table_args__ = (
        UniqueConstraint("document_parse_id", "chunk_no", name="uq_document_chunk_parse_no"),
        Index("ix_document_chunk_parse_role", "document_parse_id", "content_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_parse_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_parse.id", ondelete="CASCADE"), index=True
    )
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_role: Mapped[str | None] = mapped_column(String(32))
    page: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(Text)
    object_ref: Mapped[str | None] = mapped_column(String(64))
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)


class StructuredExtraction(Base, TimestampMixin):
    """Schema-versioned extraction of the experiment/reproducibility payload."""

    __tablename__ = "structured_extraction"
    __table_args__ = (
        UniqueConstraint(
            "work_id",
            "document_file_id",
            "schema_version",
            name="uq_structured_extraction_source_schema",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True
    )
    document_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_file.id", ondelete="CASCADE"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    source_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    extraction_model: Mapped[str | None] = mapped_column(String(128))
    validation_json: Mapped[dict | None] = mapped_column(JSONB)


class ExperimentResult(Base, TimestampMixin):
    """One normalized experiment-v2 result cell with a source pointer.

    Legacy attack-specific columns remain readable for one migration cycle;
    newly extracted records use the generic task-parameterized dimensions.
    """

    __tablename__ = "experiment_result"
    __table_args__ = (
        Index("ix_experiment_result_comparability", "comparability_key"),
        # 影子模式按 (抽取, 来源) 统计定位核验率，这是唯一的查询形状。
        Index(
            "ix_experiment_result_extraction_source",
            "structured_extraction_id",
            "extraction_source",
        ),
        UniqueConstraint(
            "structured_extraction_id",
            "metric_name",
            "dataset",
            "victim_model",
            "value",
            "source_location",
            name="uq_experiment_result_cell",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    structured_extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("structured_extraction.id", ondelete="CASCADE"), index=True
    )
    evidence_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence_unit.id", ondelete="SET NULL"), index=True
    )
    record_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="main_result")
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_definition.slug", ondelete="SET NULL"), index=True
    )
    task_variant: Mapped[str | None] = mapped_column(String(255))
    task: Mapped[str | None] = mapped_column(String(255))
    attack_goal: Mapped[str | None] = mapped_column(Text)
    threat_model: Mapped[str | None] = mapped_column(Text)
    victim_model: Mapped[str | None] = mapped_column(String(255))
    surrogate_model: Mapped[str | None] = mapped_column(String(255))
    dataset: Mapped[str | None] = mapped_column(String(255))
    dataset_version: Mapped[str | None] = mapped_column(String(128))
    split_strategy: Mapped[str | None] = mapped_column(String(128))
    train_size: Mapped[int | None] = mapped_column(Integer)
    val_size: Mapped[int | None] = mapped_column(Integer)
    test_size: Mapped[int | None] = mapped_column(Integer)
    positive_count: Mapped[int | None] = mapped_column(Integer)
    negative_count: Mapped[int | None] = mapped_column(Integer)
    negative_sampling: Mapped[str | None] = mapped_column(Text)
    label_source: Mapped[str | None] = mapped_column(Text)
    model_family: Mapped[str | None] = mapped_column(String(255))
    architecture_detail: Mapped[str | None] = mapped_column(Text)
    pretrained_backbone: Mapped[str | None] = mapped_column(String(255))
    input_representation: Mapped[str | None] = mapped_column(String(255))
    param_count: Mapped[int | None] = mapped_column(Integer)
    loss_function: Mapped[str | None] = mapped_column(String(255))
    optimizer: Mapped[str | None] = mapped_column(String(128))
    learning_rate: Mapped[float | None] = mapped_column(Float)
    batch_size: Mapped[int | None] = mapped_column(Integer)
    epochs: Mapped[int | None] = mapped_column(Integer)
    regularization: Mapped[str | None] = mapped_column(Text)
    attack_budget_json: Mapped[dict | None] = mapped_column(JSONB)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    std: Mapped[float | None] = mapped_column(Float)
    baseline_name: Mapped[str | None] = mapped_column(String(255))
    baseline_value: Mapped[float | None] = mapped_column(Float)
    clean_value: Mapped[float | None] = mapped_column(Float)
    delta_value: Mapped[float | None] = mapped_column(Float)
    comparability_key: Mapped[str] = mapped_column(String(40), nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_strength: Mapped[str | None] = mapped_column(String(24))
    # regex | llm | reconciled —— 这一行的维度是谁读出来的。
    extraction_source: Mapped[str | None] = mapped_column(String(24))
    # source_location 是否真的落在本文档的某个 DocumentChunk 上。
    # 模型回填的定位符可能指向不存在的页/表；未核验的单元格不得进入跨研究比较。
    locator_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # {字段: {"regex": x, "llm": y}}——两条抽取路径对同一格给出不同维度时的留痕。
    extraction_conflict_json: Mapped[dict | None] = mapped_column(JSONB)


class EvidenceUnit(Base, TimestampMixin):
    """可回放到原始文档的证据原子；project_id 为空时可跨项目复用。"""

    __tablename__ = "evidence_unit"
    __table_args__ = (
        UniqueConstraint(
            "work_id",
            "project_id",
            "text_hash",
            "source_document_file_id",
            name="uq_evidence_unit_source_text",
        ),
        Index("ix_evidence_unit_project_grade", "project_id", "grade"),
        Index("ix_evidence_unit_work_grade", "work_id", "grade"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scholarly_work.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_project.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    grade: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(Text)
    paragraph_index: Mapped[int | None] = mapped_column(Integer)
    object_ref: Mapped[str | None] = mapped_column(String(64))
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    source_document_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_file.id", ondelete="SET NULL"), index=True
    )
    extraction_model: Mapped[str | None] = mapped_column(String(128))
    source_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_definition.slug", ondelete="SET NULL"), index=True
    )
    topical_status: Mapped[str] = mapped_column(String(16), nullable=False, default="uncertain")
    anchor_strength: Mapped[str] = mapped_column(String(24), nullable=False, default="prose_only")
    locator_display: Mapped[str | None] = mapped_column(Text)


class EvidenceMeasurement(Base, TimestampMixin):
    """从 EvidenceUnit 中确定性或模型抽出的可比较数值。"""

    __tablename__ = "evidence_measurement"
    __table_args__ = (
        UniqueConstraint(
            "evidence_unit_id",
            "metric_name",
            "dataset",
            "split",
            "value",
            name="uq_evidence_measurement_value",
        ),
        Index("ix_evidence_measurement_comparability", "comparability_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    evidence_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_unit.id", ondelete="CASCADE"), index=True, nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    std: Mapped[float | None] = mapped_column(Float)
    dataset: Mapped[str | None] = mapped_column(String(255))
    task: Mapped[str | None] = mapped_column(String(255))
    model_family: Mapped[str | None] = mapped_column(String(255))
    attack_goal: Mapped[str | None] = mapped_column(Text)
    threat_model: Mapped[str | None] = mapped_column(Text)
    victim_model: Mapped[str | None] = mapped_column(String(255))
    attack_budget_json: Mapped[dict | None] = mapped_column(JSONB)
    protocol_json: Mapped[dict | None] = mapped_column(JSONB)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    split: Mapped[str | None] = mapped_column(String(128))
    comparability_key: Mapped[str] = mapped_column(String(40), nullable=False)
    # 与 ExperimentResult 同义：维度来源，以及定位符是否核验过。
    extraction_source: Mapped[str | None] = mapped_column(String(24))
    locator_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
