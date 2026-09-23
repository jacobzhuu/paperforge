import uuid
from datetime import UTC, datetime, timedelta

from db import create_job, create_project, create_user
from db.models.research import CitationShadow
from paperforge_worker.citation_shadow import expire_uncertain_attempts, finish_attempt
from sqlalchemy import select


async def test_shadow_timeout_uses_attempt_start_and_late_completion_is_ignored(session_factory):
    now = datetime.now(UTC)
    async with session_factory() as session:
        user = await create_user(
            session, email="shadow-review@example.test", password_hash="!disabled"
        )
        project = await create_project(
            session, title="Shadow review", paper_type="review", owner_id=user.id
        )
        job = await create_job(session, project_id=project.id, kind="research")
        job.status = "succeeded"
        rows = []
        for index, started_at in enumerate(
            (now - timedelta(minutes=5), now - timedelta(hours=2), None)
        ):
            row = CitationShadow(
                project_id=project.id, job_id=job.id, fingerprint=f"shadow-review-{index}",
                status="running", payload_json={}, started_at=started_at,
                created_at=now - timedelta(days=2),
                attempt_id=uuid.uuid4(),
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        recent, old, unknown = [(r.id, r.attempt_id) for r in rows]

    async with session_factory() as session:
        await expire_uncertain_attempts(session, now=now)
        await session.commit()
        statuses = dict(
            (await session.execute(select(CitationShadow.id, CitationShadow.status))).all()
        )
        assert statuses[recent[0]] == "running"
        assert statuses[old[0]] == statuses[unknown[0]] == "interrupted"
        assert not await finish_attempt(session, old[0], old[1], "completed", {"late": True})
        assert not await finish_attempt(session, unknown[0], unknown[1], "completed", {})
        assert not await finish_attempt(session, recent[0], uuid.uuid4(), "completed", {})
        assert await finish_attempt(session, recent[0], recent[1], "completed", {"ok": True})
        await session.commit()

    async with session_factory() as session:
        assert (await session.get(CitationShadow, old[0])).result_json == {
            "error": "execution_uncertain"
        }
        assert (await session.get(CitationShadow, recent[0])).result_json == {"ok": True}
