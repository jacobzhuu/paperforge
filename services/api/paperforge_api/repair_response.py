"""Snapshot-bound, single-use human responses; never bypasses final quality gates."""

from typing import Literal

from db import document_snapshot_hash, latest_document, list_sections, update_job
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class RepairResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interrupt_id: str = Field(min_length=1, max_length=64)
    choice: Literal["continue", "finish"]


async def validate_response(session, job, response: RepairResponse | None) -> dict:
    checkpoint = dict(job.checkpoint_json or {})
    if checkpoint.get("resumed_job_id"):
        raise HTTPException(409, "job already resumed")
    request = checkpoint.get("repair_interrupt")
    if not request:
        if response is not None:
            raise HTTPException(409, "no pending repair input")
        return {}
    if response is None or response.interrupt_id != request.get("id"):
        raise HTTPException(409, "matching repair input is required")
    if request.get("version") != "semantic-graph-v1":
        raise HTTPException(409, "unsupported repair version")
    document = await latest_document(session, job.project_id)
    rows = await list_sections(session, document.id) if document else []
    if (str(document.id) if document else None) != request.get("document_id") or (
        document_snapshot_hash(rows) if document else None
    ) != request.get("snapshot_hash"):
        raise HTTPException(409, "manuscript changed; run quality repair on the current draft")
    return {"repair_response": response.model_dump()}


async def consume_response(session, job, resumed):
    await update_job(session, job, checkpoint={"resumed_job_id": str(resumed.id)})
