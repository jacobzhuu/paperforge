"""Project-private MCP web research and durable invocation budgets."""

import sqlalchemy as sa
from alembic import op

revision = "0033_mcp_web_research"
down_revision = "0032_evaluator_shadow"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "paper_project",
        sa.Column("web_research_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("""
CREATE TABLE web_research_run (
    id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    job_id UUID, 
    status VARCHAR(24) NOT NULL, 
    plan_json JSONB NOT NULL, 
    calls INTEGER NOT NULL, 
    error VARCHAR(80), 
    finished_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (id, project_id), 
    FOREIGN KEY(project_id) REFERENCES paper_project (id) ON DELETE CASCADE, 
    UNIQUE (job_id), 
    FOREIGN KEY(job_id) REFERENCES generation_job (id) ON DELETE SET NULL
)
""")
    op.execute("CREATE INDEX ix_web_research_run_project_id ON web_research_run (project_id)")
    op.execute("""
CREATE TABLE web_research_source (
    id UUID NOT NULL, 
    run_id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    url TEXT NOT NULL, 
    url_hash VARCHAR(64) NOT NULL, 
    order_no INTEGER NOT NULL, 
    title TEXT NOT NULL, 
    snippet TEXT NOT NULL, 
    body TEXT NOT NULL, 
    content_hash VARCHAR(64), 
    status VARCHAR(24) NOT NULL, 
    verification_json JSONB, 
    fetched_at TIMESTAMP WITH TIME ZONE, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(run_id, project_id) REFERENCES web_research_run (id, project_id) ON DELETE CASCADE, 
    UNIQUE (run_id, url_hash)
)
""")
    op.execute("CREATE INDEX ix_web_research_source_project_id ON web_research_source (project_id)")
    op.execute("CREATE INDEX ix_web_research_source_run_id ON web_research_source (run_id)")
    op.execute("""
CREATE TABLE mcp_tool_invocation (
    id UUID NOT NULL, 
    run_id UUID NOT NULL, 
    project_id UUID NOT NULL, 
    owner_id UUID NOT NULL, 
    tool VARCHAR(80) NOT NULL, 
    schema_hash VARCHAR(64) NOT NULL, 
    request_hash VARCHAR(64) NOT NULL, 
    attempt INTEGER NOT NULL, 
    trace_json JSONB NOT NULL, 
    status VARCHAR(24) NOT NULL, 
    error VARCHAR(80), 
    result_json JSONB, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(run_id, project_id) REFERENCES web_research_run (id, project_id) ON DELETE CASCADE, 
    UNIQUE (run_id, request_hash, attempt), 
    FOREIGN KEY(owner_id) REFERENCES app_user (id)
)
""")
    op.execute("CREATE INDEX ix_mcp_tool_invocation_created_at ON mcp_tool_invocation (created_at)")
    op.execute("CREATE INDEX ix_mcp_tool_invocation_owner_id ON mcp_tool_invocation (owner_id)")
    op.execute("CREATE INDEX ix_mcp_tool_invocation_run_id ON mcp_tool_invocation (run_id)")
    op.execute("""
CREATE TABLE mcp_daily_budget (
    key VARCHAR(80) NOT NULL, 
    calls INTEGER NOT NULL, 
    PRIMARY KEY (key)
)
""")


def downgrade():
    op.drop_table("mcp_daily_budget")
    op.drop_table("mcp_tool_invocation")
    op.drop_table("web_research_source")
    op.drop_table("web_research_run")
    op.drop_column("paper_project", "web_research_enabled")
