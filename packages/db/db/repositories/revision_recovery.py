"""Candidate-owned operation ledger shared by the API and worker.

A started operation is not proof of completion. Only the transaction which saves
its output may mark it completed. Unknown outcomes require inspection, not replay.
"""

import copy
import hashlib
import json

from sqlalchemy import select

from db.models.revision import DraftRevision

MAX_ATTEMPTS = 2
MESSAGES = {
    "legacy_attempt": "旧任务缺少逐步记录，无法确认已付费操作的结果；请检查候选，暂不自动重试。",
    "unknown_outcome": "上次操作的结果尚未确认，已保留候选；为避免重复调用，暂不能继续。",
    "inputs_changed": "修稿输入已变化，当前恢复记录不能继续使用；请检查候选。",
    "retry_exhausted": "该修复已达到两次尝试上限，请检查候选并手动处理剩余问题。",
    "budget_exhausted": "本轮累计执行预算已用尽，继续任务不会重置额度；首稿和候选均已保留。",
    "operation_failed": "部分修复未成功，可继续重试未完成部分；已保存的章节不会重复修复。",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def recovery_block(row, checkpoint):
    state = row.recovery_json or {}
    if not state and (
        checkpoint.get("revision_repair_attempts") or checkpoint.get("revision_retrieval_attempted")
    ):
        return "legacy_attempt"
    budget = checkpoint.get("agent_budget") or {}
    if budget.get("stop_reason"):
        return "budget_exhausted"
    limits = budget.get("limits") or {}
    if any(
        budget.get(k, 0) >= limits[k]
        for k in ("calls", "reserved_tokens", "active_seconds")
        if k in limits
    ):
        return "budget_exhausted"
    if state.get("blocked"):
        return state["blocked"]
    for operation in state.get("operations", {}).values():
        if operation["status"] in {"started", "unknown"}:
            return "unknown_outcome"
        if operation["status"] == "failed" and operation["attempts"] >= MAX_ATTEMPTS:
            return "retry_exhausted"
    return None


async def locked_state(session, revision_id, project_id, job_id=None):
    row = await session.scalar(
        select(DraftRevision)
        .where(DraftRevision.id == revision_id, DraftRevision.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or (job_id is not None and row.job_id != job_id):
        raise ValueError("revision execution no longer owns candidate")
    return row, copy.deepcopy(row.recovery_json or {"version": 1, "operations": {}})


async def complete_section(session, context, section_key, body):
    revision_id = getattr(context, "revision_id", None)
    if revision_id is None:
        return
    row, state = await locked_state(session, revision_id, context.project_id, context.job_id)
    operation = state["operations"].get("section:" + section_key)
    if operation is None or operation["status"] != "started":
        raise ValueError("section has no active revision operation")
    operation.update(status="completed", output_hash=digest(body), error=None)
    row.recovery_json = state
