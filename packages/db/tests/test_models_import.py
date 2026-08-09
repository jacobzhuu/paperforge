import uuid

from db import (
    Base,
    lock_generation_job_stmt,
    next_job_event_seq_stmt,
    writing_whitelist_stmt,
)
from db import models as m
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql


def test_expected_tables_registered():
    tables = set(Base.metadata.tables.keys())
    assert {"app_user", "user_session", "auth_action_token"} <= tables
    # 保留的 scholarly_work 系 5 张
    assert {
        "scholarly_work",
        "work_identifier",
        "work_url",
        "work_author",
        "scholarly_http_cache",
    } <= tables
    # 新增的项目/论文结构域
    assert {
        "paper_project",
        "generation_job",
        "job_event",
        "library_entry",
        "literature_card",
        "literature_pdf_upload",
        "document_file",
        "user_asset",
        "outline",
        "paper_document",
        "paper_section",
        "citation_usage",
        "search_run",
        "export_artifact",
        "llm_call_log",
        "evidence_unit",
        "evidence_measurement",
        "research_question",
        "question_evidence_link",
    } <= tables


def test_evidence_schema_carries_traceability_and_comparability_fields():
    evidence_columns = {column.name for column in m.EvidenceUnit.__table__.columns}
    assert {
        "kind",
        "grade",
        "text_hash",
        "page",
        "section_path",
        "paragraph_index",
        "object_ref",
        "char_start",
        "char_end",
        "source_document_file_id",
    } <= evidence_columns
    measurement_columns = {column.name for column in m.EvidenceMeasurement.__table__.columns}
    assert {
        "metric_name",
        "value",
        "dataset",
        "task",
        "sample_size",
        "split",
        "comparability_key",
    } <= measurement_columns
    experiment_columns = {column.name for column in m.ExperimentResult.__table__.columns}
    assert {
        "task_id",
        "task_variant",
        "dataset_version",
        "split_strategy",
        "model_family",
        "input_representation",
        "loss_function",
        "optimizer",
        "learning_rate",
        "batch_size",
        "epochs",
        "ci_low",
        "ci_high",
        "baseline_name",
        "baseline_value",
        "anchor_strength",
    } <= experiment_columns
    anchor_columns = {column.name for column in m.ClaimEvidenceAnchor.__table__.columns}
    assert {"evidence_unit_id", "comparability_ok", "grade_ok"} <= anchor_columns


def test_llm_call_log_retains_provider_stage_and_occurrence_time():
    columns = {column.name for column in m.LlmCallLog.__table__.columns}
    assert {"provider", "metadata_json", "occurred_at"} <= columns


def test_project_owner_is_required_uuid_foreign_key():
    owner = m.PaperProject.__table__.c.owner_id
    assert owner.nullable is False
    assert str(owner.type) == "UUID"
    assert {fk.target_fullname for fk in owner.foreign_keys} == {"app_user.id"}


def test_library_entry_has_bibtex_unique_and_verified_at():
    cols = {c.name for c in m.LibraryEntry.__table__.columns}
    assert "bibtex_key" in cols
    assert "verified_at" in cols  # R1：为空不进写作白名单
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in m.LibraryEntry.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("project_id", "bibtex_key") in unique_columns


def test_private_pdf_schema_encodes_tenant_scope_and_role_vocabularies():
    entry = m.LibraryEntry.__table__
    role = entry.c.literature_role
    assert role.nullable is False
    assert str(role.default.arg) == "general"
    entry_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in entry.constraints
        if isinstance(constraint, CheckConstraint)
    }
    role_values = {
        "general",
        "core",
        "background",
        "method",
        "benchmark",
        "controversy",
    }
    role_check = entry_checks["ck_library_entry_literature_role"]
    assert all(f"'{value}'" in role_check for value in role_values)

    document = m.DocumentFile.__table__
    assert document.c.project_id.nullable is True
    assert {fk.target_fullname for fk in document.c.project_id.foreign_keys} == {"paper_project.id"}
    assert document.c.access_scope.nullable is False
    assert str(document.c.access_scope.default.arg) == "shared"
    indexes = {index.name: index for index in document.indexes}
    assert indexes["uq_document_file_shared_work_content"].unique is True
    assert indexes["uq_document_file_private_project_work_content"].unique is True
    assert tuple(
        column.name for column in indexes["uq_document_file_private_project_work_content"].columns
    ) == ("project_id", "work_id", "content_hash")

    upload = m.LiteraturePdfUpload.__table__
    assert {column.name for column in upload.columns} >= {
        "project_id",
        "object_key",
        "filename",
        "mime",
        "bytes",
        "content_hash",
        "status",
        "extracted_metadata_json",
        "match_candidates_json",
        "matched_work_id",
        "document_file_id",
        "match_method",
        "match_confidence",
        "match_metadata_json",
        "error_json",
        "confirmed_at",
    }
    assert {fk.target_fullname for fk in upload.c.project_id.foreign_keys} == {"paper_project.id"}
    assert {fk.target_fullname for fk in upload.c.matched_work_id.foreign_keys} == {
        "scholarly_work.id"
    }
    assert {fk.target_fullname for fk in upload.c.document_file_id.foreign_keys} == {
        "document_file.id"
    }
    upload_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in upload.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("object_key",) in upload_uniques
    assert ("project_id", "content_hash") not in upload_uniques


def test_r1_writing_whitelist_query_encodes_every_gate():
    sql = str(
        writing_whitelist_stmt(uuid.uuid4()).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "library_entry.status = 'selected'" in sql
    assert "library_entry.verified_at IS NOT NULL" in sql
    assert "library_entry.bibtex_key IS NOT NULL" in sql
    assert "scholarly_work.is_retracted IS false" in sql


def test_job_event_has_unique_sequence_constraint_and_composite_index():
    table = m.JobEvent.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    indexes = {tuple(column.name for column in index.columns) for index in table.indexes}
    assert ("job_id", "seq") in unique_columns
    assert ("job_id", "seq") in indexes


def test_job_event_sequence_writer_locks_job_before_allocating_next_seq():
    job_id = uuid.uuid4()
    lock_sql = str(
        lock_generation_job_stmt(job_id).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    seq_sql = str(
        next_job_event_seq_stmt(job_id).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE" in lock_sql
    assert "max(job_event.seq)" in seq_sql


def test_manual_timestamp_columns_have_server_defaults():
    columns = [
        m.GenerationJob.created_at,
        m.JobEvent.created_at,
        m.CitationUsage.created_at,
        m.SearchRun.executed_at,
        m.ExportArtifact.created_at,
        m.LlmCallLog.created_at,
        m.LlmCallLog.occurred_at,
    ]
    assert all(column.property.columns[0].server_default is not None for column in columns)
