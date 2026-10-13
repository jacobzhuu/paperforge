"""Execute revision work once, keeping uncertain outcomes visible."""

import uuid

from db import list_sections
from db.repositories.revision_recovery import (
    MAX_ATTEMPTS,
    digest,
    locked_state,
    recovery_block,
)

from paperforge_worker.context import JobStopped


async def initialize(context, revision_id):
    context.revision_id = uuid.UUID(str(revision_id))
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        if state.get("execution_job_id") == str(context.job_id):
            return None
        reason = recovery_block(row, context.checkpoint)
        if reason:
            raise JobStopped("pause", reason=reason)
        state["execution_job_id"] = str(context.job_id)
        row.recovery_json = state
        return state


async def save_plan(context, plan):
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        if "plan" not in state:
            state["plan"] = plan
            for key in plan["notes"]:
                state["operations"].setdefault(
                    "section:" + key,
                    {
                        "kind": "section",
                        "target": key,
                        "input_hash": None,
                        "status": "pending",
                        "attempts": 0,
                        "job_id": str(context.job_id),
                        "output_hash": None,
                        "error": None,
                    },
                )
            row.recovery_json = state
        return state["plan"]


async def guard_sources(context, sources, *, field="repair_source_hash"):
    if not getattr(context, "revision_id", None):
        return
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        current = digest(sources)
        changed = state.get(field, current) != current
        if changed:
            state["blocked"] = "inputs_changed"
        else:
            state[field] = current
        row.recovery_json = state
    if changed:
        raise JobStopped("pause", reason="inputs_changed")


async def repair_targets(context, targets):
    """Remember dependent chapters even if their parent completed before interruption."""
    if not getattr(context, "revision_id", None):
        return targets
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        if "repair_targets" not in state:
            state["repair_targets"] = sorted(targets)
            for key in targets:
                state["operations"].setdefault(
                    "section:" + key,
                    {
                        "kind": "section",
                        "target": key,
                        "input_hash": None,
                        "status": "pending",
                        "attempts": 0,
                        "job_id": str(context.job_id),
                        "output_hash": None,
                        "error": None,
                    },
                )
            row.recovery_json = state
        return set(state["repair_targets"])


async def begin(context, key, inputs):
    """Return saved output for completed work, None for a newly started operation."""
    await context.raise_if_stopped()
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        operation = state["operations"].get(key)
        if operation:
            if operation["status"] == "completed":
                return operation
            if operation["status"] == "pending":
                reason = None
            elif operation["input_hash"] != digest(inputs):
                state["blocked"] = "inputs_changed"
                row.recovery_json = state
                reason = "inputs_changed"
            elif operation["status"] in {"started", "unknown"}:
                reason = "unknown_outcome"
            elif operation["attempts"] >= MAX_ATTEMPTS:
                reason = "retry_exhausted"
            elif operation.get("job_id") == str(context.job_id):
                reason = "operation_failed"
            else:
                reason = None
            if reason:
                # Commit the explanation before raising the cooperative stop.
                if reason != "operation_failed":
                    state["blocked"] = reason
                    row.recovery_json = state
        else:
            reason = None
        if not reason:
            state["operations"][key] = {
                "kind": key.split(":")[0],
                "target": key.split(":", 1)[-1],
                "input_hash": digest(inputs),
                "status": "started",
                "attempts": (operation or {}).get("attempts", 0) + 1,
                "job_id": str(context.job_id),
                "output_hash": None,
                "error": None,
            }
            row.recovery_json = state
    if reason:
        raise JobStopped("pause", reason=reason)
    return None


async def finish(context, key, *, output=None, error=None, unknown=False):
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        operation = state["operations"][key]
        if operation["status"] == "completed":
            return
        operation.update(
            status="unknown" if unknown else "failed" if error else "completed",
            output=output,
            output_hash=digest(output),
            error=error,
        )
        row.recovery_json = state


async def section_completed(context, item):
    if not getattr(context, "revision_id", None):
        return False
    async with context.session() as session:
        _, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
    op = state["operations"].get("section:" + item["section_key"])
    if not op or op["status"] != "completed":
        return False
    # section_started validates the output fingerprint without issuing another call.
    return not await section_started(context, item, None)


async def section_started(context, item, notes):
    if not getattr(context, "revision_id", None):
        return True
    key = "section:" + item["section_key"]
    saved = await begin(context, key, {"material": item["review_input"], "notes": notes})
    if saved:
        async with context.session() as session:
            row, _ = await locked_state(
                session, context.revision_id, context.project_id, context.job_id
            )
            sections = await list_sections(session, row.candidate_document_id)
            body = next(s.body_ir_json for s in sections if s.section_key == item["section_key"])
        if digest(body) != saved["output_hash"]:
            async with context.session() as session:
                row, state = await locked_state(
                    session, context.revision_id, context.project_id, context.job_id
                )
                state["blocked"] = "inputs_changed"
                row.recovery_json = state
            raise JobStopped("pause", reason="inputs_changed")
        return False
    return True


async def ensure_settled(context):
    async with context.session() as session:
        row, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
        reason = recovery_block(row, context.checkpoint)
        if not reason and any(
            o["status"] in {"failed", "pending"} for o in state["operations"].values()
        ):
            reason = "operation_failed"
    if reason:
        raise JobStopped("pause", reason=reason)


async def step(context, key, action):
    """Persist each retrieval step separately; exceptions never imply safe replay."""
    if not getattr(context, "revision_id", None):
        return await action()
    async with context.session() as session:
        _, state = await locked_state(
            session, context.revision_id, context.project_id, context.job_id
        )
    saved = await begin(context, "retrieval:" + key, state["plan"])
    if saved:
        return saved.get("output")
    try:
        result = await action()
        payload = result.to_payload() if hasattr(result, "to_payload") else result
        await finish(context, "retrieval:" + key, output=payload)
        return payload
    except JobStopped:
        raise
    except Exception as error:
        await finish(context, "retrieval:" + key, error=type(error).__name__, unknown=True)
        raise
