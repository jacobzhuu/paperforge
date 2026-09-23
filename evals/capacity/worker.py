"""Infrastructure-only workload. Never claims to generate a real paper."""

import asyncio
import os
import uuid

from arq import func
from db import update_job
from db.models.paper import GenerationJob
from paperforge_worker.context import job_context
from paperforge_worker.execution import guarded
from paperforge_worker.worker import WorkerSettings as ProductionSettings
from paperforge_worker.worker import startup
from storage import make_object_store


async def run_full_pipeline(ctx, project_id, job_id, **kwargs):
    async with job_context(
        project_id=uuid.UUID(project_id),
        job_id=uuid.UUID(job_id),
        settings=ctx["settings"],
        session_factory=ctx["session_factory"],
        event_publisher=ctx["redis"],
    ) as context:
        async with context.session() as session:
            job = await session.get(GenerationJob, uuid.UUID(job_id))
            await update_job(session, job, status="running")
        store = await asyncio.to_thread(make_object_store, ctx["settings"])
        key = f"capacity/{job_id}/fixture.bin"
        payload = b"capacity-fixture\n" * 4096
        await context.emit("capacity.started", {}, checkpoint={"capacity_setup": True})
        duration = float(os.environ.get("CAPACITY_JOB_SECONDS", "10"))
        if kwargs.get("quality_profile") == "submission" and not context.checkpoint.get(
            "resumed_from"
        ):
            duration = 1800.0
        for step in range(5):
            await context.raise_if_stopped()
            await asyncio.sleep(duration / 5)
            await asyncio.to_thread(store.put, key, payload)
            assert await asyncio.to_thread(store.get, key) == payload
            await context.emit("capacity.stage", {"step": step}, checkpoint={f"step_{step}": True})
        await asyncio.to_thread(store.delete, key)
        async with context.session() as session:
            job = await session.get(GenerationJob, uuid.UUID(job_id))
            await update_job(session, job, status="succeeded", progress=1.0)
        await context.notify_event()


class WorkerSettings(ProductionSettings):
    on_startup = startup
    functions = [func(guarded(run_full_pipeline), name="run_full_pipeline", timeout=10800)]
    health_check_interval = 5


# ARQ reads __dict__, not inherited class attributes.
for _name, _value in ProductionSettings.__dict__.items():
    if not _name.startswith("_") and _name not in WorkerSettings.__dict__:
        setattr(WorkerSettings, _name, _value)
