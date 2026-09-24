"""Kill only recorded local fixture workers; verify lease expiry and explicit resume."""

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from db import create_project, create_user, create_user_session
from db.models.paper import GenerationJob
from db.session import make_engine, make_session_factory


async def main(args):
    url = os.environ["DATABASE_URL"]
    if url != "postgresql+asyncpg://paperforge:paperforge@127.0.0.1:25432/paperforge_capacity":
        raise SystemExit("isolated capacity database required")
    factory = make_session_factory(make_engine(url))
    records = []
    async with httpx.AsyncClient(base_url="http://127.0.0.1:28080", timeout=30) as client:
        for trial in range(args.rounds):
            token = uuid.uuid4().hex
            async with factory() as session:
                user = await create_user(
                    session, email=f"{token}@capacity.invalid", password_hash="!"
                )
                await create_user_session(
                    session,
                    user_id=user.id,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
                project = await create_project(
                    session, title="Fault fixture", paper_type="review", owner_id=user.id
                )
                await session.commit()
            headers = {"Cookie": f"paperforge_session={token}", "Origin": "http://127.0.0.1:28080"}
            response = await client.post(
                f"/api/v1/projects/{project.id}/generate",
                json={"quality_profile": "submission"},
                headers=headers,
            )
            response.raise_for_status()
            job_id = response.json()["id"]

            async def wait_for(identifier, statuses, timeout):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    async with factory() as session:
                        job = await session.get(GenerationJob, uuid.UUID(identifier))
                        if job.status in statuses:
                            return job
                    await asyncio.sleep(0.5)
                raise TimeoutError(f"job did not reach {statuses}")

            await wait_for(job_id, {"running"}, 15)
            await asyncio.sleep(5)
            killed_at = time.monotonic()
            for name in ("worker1", "worker2"):
                pid = int(Path(f".paperforge/capacity/{name}.pid").read_text())
                command = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True)
                if "arq worker.WorkerSettings" not in command:
                    raise RuntimeError("fixture worker PID ownership changed")
                os.kill(pid, signal.SIGKILL)
            paused = await wait_for(job_id, {"paused"}, 145)
            recovery_seconds = time.monotonic() - killed_at
            assert paused.checkpoint_json.get("capacity_setup") is True
            for name in ("worker1", "worker2"):
                with open(f".paperforge/capacity/{name}.log", "a") as log:
                    process = subprocess.Popen(
                        [".venv/bin/arq", "worker.WorkerSettings"],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                Path(f".paperforge/capacity/{name}.pid").write_text(str(process.pid))
            response = await client.post(
                f"/api/v1/projects/{project.id}/jobs/{job_id}/resume", headers=headers
            )
            response.raise_for_status()
            resumed_id = response.json()["id"]
            resumed_at = time.monotonic()
            finished = await wait_for(resumed_id, {"succeeded", "failed"}, 45)
            assert finished.status == "succeeded"
            records.append(
                {
                    "trial": trial + 1,
                    "configured_original_job_seconds": 1800,
                    "seconds_running_before_kill": 5,
                    "kill_to_paused_seconds": recovery_seconds,
                    "resume_to_success_seconds": time.monotonic() - resumed_at,
                    "checkpoint_preserved": True,
                    "original_job": job_id,
                    "resumed_job": resumed_id,
                }
            )
            Path(args.output).write_text(
                json.dumps(
                    {"kind": "fixture_hard_kill_explicit_resume", "trials": records}, indent=2
                )
                + "\n"
            )
            print(json.dumps(records[-1]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
