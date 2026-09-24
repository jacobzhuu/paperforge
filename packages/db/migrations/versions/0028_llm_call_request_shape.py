"""record what was actually asked of the model, not only what it cost

Revision ID: 0028_llm_request_shape
Revises: 0027_job_heartbeat
Create Date: 2026-08-22

``llm_call_log`` answers "what did this job spend" and nothing else.  Diagnosing
the truncation work required knowing each role's ``max_output_tokens``, and that
number is nowhere in the ledger — it had to be read out of the pipeline source,
role by role, and the answer would go stale the moment a call site changed.  The
same gap hides the more common question: a section comes back thin, and the
ledger cannot say whether the writer was handed a one-line agenda or a full one,
because no property of the request survives the call.

Five columns, all nullable, so a draining old worker under blue/green keeps
INSERTing without them (0020 exists because a NOT NULL default was dropped from
this same table and old workers started failing).

``prompt_sha256`` is a digest, not the prompt: it costs 64 bytes, carries no
content, and still answers "is this the same prompt as last time" — which is the
question that separates a prompt regression from a model regression.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_llm_request_shape"
down_revision = "0027_job_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_call_log", sa.Column("max_output_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_call_log", sa.Column("finish_reason", sa.String(length=32), nullable=True))
    op.add_column("llm_call_log", sa.Column("prompt_sha256", sa.String(length=64), nullable=True))
    op.add_column("llm_call_log", sa.Column("prompt_chars", sa.Integer(), nullable=True))
    op.add_column("llm_call_log", sa.Column("output_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_call_log", "output_chars")
    op.drop_column("llm_call_log", "prompt_chars")
    op.drop_column("llm_call_log", "prompt_sha256")
    op.drop_column("llm_call_log", "finish_reason")
    op.drop_column("llm_call_log", "max_output_tokens")
