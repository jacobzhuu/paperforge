"""Record the running shadow attempt independently of queue age."""

import sqlalchemy as sa
from alembic import op

revision = "0036_shadow_attempt"
down_revision = "0035_research_analysis"
branch_labels = depends_on = None


def upgrade():
    op.add_column("citation_shadow", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("citation_shadow", sa.Column("attempt_id", sa.UUID()))
    # Existing running requests have no trustworthy start time. Their cost is uncertain;
    # mark them interrupted so that no automatic replay occurs.
    op.execute("""
        UPDATE citation_shadow
        SET status = 'interrupted', result_json = '{"error":"execution_uncertain"}'::jsonb
        WHERE status = 'running'
    """)
    # A worker from the preceding blue/green deployment can still finish an
    # in-flight request with its old unconditional ORM update. Protect the
    # durable interrupted verdict at the database boundary as well.
    op.execute("""
        CREATE FUNCTION citation_shadow_preserve_interrupted() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'interrupted' AND
               (NEW.status IS DISTINCT FROM OLD.status OR
                NEW.result_json IS DISTINCT FROM OLD.result_json) THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER citation_shadow_interrupted_guard
        BEFORE UPDATE ON citation_shadow
        FOR EACH ROW EXECUTE FUNCTION citation_shadow_preserve_interrupted()
    """)


def downgrade():
    op.execute("DROP TRIGGER citation_shadow_interrupted_guard ON citation_shadow")
    op.execute("DROP FUNCTION citation_shadow_preserve_interrupted()")
    op.drop_column("citation_shadow", "attempt_id")
    op.drop_column("citation_shadow", "started_at")
