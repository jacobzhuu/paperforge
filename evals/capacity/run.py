"""Run against the isolated capacity stack; emits raw JSON, never production claims."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from db import create_project, create_user, create_user_session
from db.models.library import LibraryEntry, ScholarlyWork
from db.models.paper import GenerationJob, JobDispatch
from db.session import make_engine, make_session_factory
from sqlalchemy import select


def percentile(values, fraction):
    if not values:
        return None
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))]


async def main(args):
    url = os.environ["DATABASE_URL"]
    if not url.endswith("/paperforge_capacity") or "127.0.0.1:25432" not in url:
        raise SystemExit("requires dedicated local paperforge_capacity database")
    if args.base != "http://127.0.0.1:28080":
        raise SystemExit("refusing non-capacity API")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    users = []
    async with factory() as session:
        for _ in range(100):
            token = uuid.uuid4().hex
            user = await create_user(session, email=f"{token}@capacity.invalid", password_hash="!")
            await create_user_session(
                session,
                user_id=user.id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            project = await create_project(
                session, title="Capacity fixture", paper_type="review", owner_id=user.id
            )
            works = [
                ScholarlyWork(
                    id=uuid.uuid4(),
                    canonical_title=f"Synthetic capacity fixture {index}",
                    abstract="Deterministic test metadata. " * 30,
                    publication_year=2025,
                )
                for index in range(args.works_per_project)
            ]
            session.add_all(works)
            await session.flush()
            session.add_all(
                [
                    LibraryEntry(
                        project_id=project.id,
                        work_id=work.id,
                        status="candidate",
                        added_via="search",
                    )
                    for work in works
                ]
            )
            users.append((token, str(project.id)))
        await session.commit()
    latencies = []
    statuses = {}
    jobs = []
    sse_events = 0
    sse_errors = []
    sse_active = 0
    sse_peak = 0
    running_peak = 0
    queue_peak = 0
    began = time.monotonic()
    async with httpx.AsyncClient(
        base_url=args.base, timeout=30, limits=httpx.Limits(max_connections=200)
    ) as client:

        async def request(method, path, token):
            start = time.monotonic()
            try:
                response = await client.request(
                    method,
                    path,
                    headers={"Origin": args.base, "Cookie": f"paperforge_session={token}"},
                )
                statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
                return response
            except Exception as error:
                key = type(error).__name__
                statuses[key] = statuses.get(key, 0) + 1
                return None
            finally:
                latencies.append(time.monotonic() - start)

        async def sample_jobs():
            nonlocal running_peak, queue_peak
            while time.monotonic() - began < args.seconds:
                async with factory() as session:
                    states = list(
                        await session.scalars(
                            select(GenerationJob.status).where(
                                GenerationJob.id.in_([uuid.UUID(item) for item in jobs])
                            )
                        )
                    )
                running_peak = max(running_peak, states.count("running"))
                queue_peak = max(queue_peak, states.count("queued"))
                await asyncio.sleep(0.5)

        async def browse(user):
            token, project_id = user
            iteration = 0
            while time.monotonic() - began < args.seconds:
                suffix = ("", "/library", "/jobs")[iteration % 3]
                iteration += 1
                await request("GET", f"/api/v1/projects/{project_id}{suffix}", token)
                await asyncio.sleep(args.think)

        async def events(user, job_id):
            nonlocal sse_events, sse_active, sse_peak
            token, project_id = user
            seen = set()
            try:
                async with client.stream(
                    "GET",
                    f"/api/v1/projects/{project_id}/jobs/{job_id}/events",
                    headers={"Cookie": f"paperforge_session={token}"},
                    timeout=None,
                ) as response:
                    response.raise_for_status()
                    sse_active += 1
                    sse_peak = max(sse_peak, sse_active)
                    try:
                        async for line in response.aiter_lines():
                            if line.startswith("id:"):
                                seen.add(line[3:].strip())
                    finally:
                        sse_active -= 1
                sse_events += len(seen)
            except Exception as error:
                code = getattr(getattr(error, "response", None), "status_code", "")
                sse_errors.append(f"{type(error).__name__}:{code}")

        async def submit(user):
            token, project_id = user
            response = await request("POST", f"/api/v1/projects/{project_id}/generate", token)
            if response is not None and response.status_code == 202:
                job_id = response.json()["id"]
                jobs.append(job_id)
                return [
                    asyncio.create_task(events(user, job_id)) for _ in range(args.streams_per_job)
                ]

        readers = [asyncio.create_task(browse(user)) for user in users[: args.active]]
        sampler = asyncio.create_task(sample_jobs())
        if args.submit_interval:
            batches = []
            for index, user in enumerate(users[: args.jobs]):
                await asyncio.sleep(max(0, began + index * args.submit_interval - time.monotonic()))
                batches.append(await submit(user))
        else:
            batches = await asyncio.gather(*(submit(user) for user in users[: args.jobs]))
        streams = [task for batch in batches if batch for task in batch]
        await asyncio.gather(*readers, sampler)
        for task in streams:
            if not task.done():
                task.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
    elapsed = time.monotonic() - began
    async with factory() as session:
        rows = (
            await session.execute(
                select(GenerationJob, JobDispatch)
                .join(JobDispatch, JobDispatch.job_id == GenerationJob.id)
                .where(GenerationJob.id.in_([uuid.UUID(item) for item in jobs]))
            )
        ).all()
        waits = [
            (intent.started_at - job.created_at).total_seconds()
            for job, intent in rows
            if intent.started_at
        ]
        completed = sum(job.status == "succeeded" for job, _ in rows)
        job_states = {str(job.id): job.status for job, _ in rows}
    result = {
        "kind": "infrastructure_fixture_not_real_agent_or_llm",
        "resource_profile": os.environ.get("CAPACITY_RESOURCE_PROFILE", "unbounded host processes"),
        "streams_per_job": args.streams_per_job,
        "works_per_project": args.works_per_project,
        "submit_interval_seconds": args.submit_interval,
        "read_mix": ["project", "library", "jobs"],
        "at": datetime.now(UTC).isoformat(),
        "seconds": elapsed,
        "accounts": 100,
        "active_users": args.active,
        "job_fixture_seconds": os.environ.get("CAPACITY_JOB_SECONDS", "10"),
        "request_count": len(latencies),
        "request_per_second": len(latencies) / elapsed,
        "http_statuses": statuses,
        "latency_seconds": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "sampled_running_peak": running_peak,
        "sampled_queued_peak": queue_peak,
        "job_failure_ratio": sum(state == "failed" for state in job_states.values())
        / max(1, len(jobs)),
        "accepted_jobs": len(jobs),
        "completed_jobs": completed,
        "job_states": job_states,
        "observed_completions_per_hour": completed / elapsed * 3600,
        "queue_wait_seconds": {
            "p50": percentile(waits, 0.5),
            "p95": percentile(waits, 0.95),
            "max": max(waits, default=None),
        },
        "sse_peak": sse_peak,
        "sse_unique_events_received": sse_events,
        "sse_errors": sse_errors,
        "failure_ratio": sum(
            value for key, value in statuses.items() if not key.isdigit() or int(key) >= 500
        )
        / max(1, len(latencies)),
    }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps({key: value for key, value in result.items() if key != "job_states"}, indent=2)
    )
    await engine.dispose()
    assert result["failure_ratio"] == 0, "HTTP transport/server failures; see result artifact"
    assert not sse_errors, "SSE failures; see result artifact"
    assert len(jobs) == args.jobs, "not all submissions were accepted"
    assert result["job_failure_ratio"] == 0, "job failures; see result artifact"
    if not args.allow_incomplete:
        assert completed == len(jobs), (
            "unfinished jobs; extend duration or explicitly allow incomplete"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:28080")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--active", type=int, default=30)
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--streams-per-job", type=int, default=1)
    parser.add_argument("--think", type=float, default=1)
    parser.add_argument("--works-per-project", type=int, default=40)
    parser.add_argument("--submit-interval", type=float, default=0)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
