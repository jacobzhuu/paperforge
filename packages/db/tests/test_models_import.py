import uuid

from db import (
    Base,
    lock_generation_job_stmt,
    next_job_event_seq_stmt,
    writing_whitelist_stmt,
)
from db import models as m
from sqlalchemy import UniqueConstraint
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
        "document_file",
        "user_asset",
        "outline",
        "paper_document",
        "paper_section",
        "citation_usage",
        "search_run",
        "export_artifact",
        "llm_call_log",
    } <= tables


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
    ]
    assert all(column.property.columns[0].server_default is not None for column in columns)
