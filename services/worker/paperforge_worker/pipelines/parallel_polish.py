"""Frozen-input polishing with an atomic, document-owned result ledger."""

import time
from copy import deepcopy
from dataclasses import asdict

from db import get_outline, get_writing_whitelist, grounded_asset_payloads, list_sections
from db.models.paper import PaperDocument
from sqlalchemy import select

from paperforge_worker.concurrency import bounded_map
from paperforge_worker.context import JobStopped
from paperforge_worker.orchestration.polish_policy import VERSION, triage
from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.writing import WritingContext, coherence_pass


async def polish(
    context,
    *,
    drafts,
    document_id,
    order_by_key,
    whitelist,
    language,
    writing_context,
    runner,
    outcome,
):
    from paperforge_worker.pipelines.document import (
        _POLISH_PROGRESS_END,
        _POLISH_PROGRESS_START,
        _card_context,
        _integrate_body,
        _persist_draft,
        _polish_skip_requested,
    )

    started = time.monotonic()
    policy = context.checkpoint["writer_polish_policy"]
    width = context.checkpoint["writer_polish_concurrency"]
    keys = [
        k
        for k in order_by_key
        if k in drafts
        and (drafts[k].generator.startswith("llm") or drafts[k].generator == "resumed")
    ]
    if not keys:
        return
    model_config = fingerprint(
        {
            "writer": context.settings.llm_config().model_for_role("writer"),
            "thinking": context.settings.llm_role_thinking,
            "retry": context.settings.llm_role_retry,
        }
    )
    async with context.session() as session:
        document = await session.scalar(
            select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
        )
        state = deepcopy(document.writing_state_json or {})
        current = {
            r.section_key: fingerprint(r.body_ir_json)
            for r in await list_sections(session, document_id)
            if r.section_key in drafts
        }
        outline = await get_outline(session, document.outline_id)
        source_hash = fingerprint(
            {
                "outline": outline.tree_json if outline else None,
                "cards": await _card_context(session, context.project_id),
                "whitelist": await get_writing_whitelist(session, context.project_id),
                "assets": await grounded_asset_payloads(session, context.project_id),
            }
        )
        ledger = state.get("polish")
        if ledger:
            if (
                ledger["expected"] != current
                or ledger["policy"] != policy
                or ledger["version"] != VERSION
                or ledger.get("model_config") != model_config
                or ledger.get("source_hash") != source_hash
            ):
                raise JobStopped("pause", reason="polish_inputs_changed")
        else:
            ledger = {
                "version": VERSION,
                "policy": policy,
                "expected": current,
                "source_hash": source_hash,
                "model_config": model_config,
                "context": asdict(writing_context),
                "results": {},
                "decisions": {},
            }
            ledger["snapshot"] = fingerprint(ledger)
            state["polish"] = ledger
            document.writing_state_json = state
    frozen = WritingContext(**deepcopy(ledger["context"]))
    pending = [k for k in keys if k not in ledger["results"]]
    await context.emit(
        "polish.started",
        {
            "total": len(keys),
            "policy": policy,
            "concurrency": width,
            "version": VERSION,
        },
        stage="polish",
        progress=_POLISH_PROGRESS_START
        + (_POLISH_PROGRESS_END - _POLISH_PROGRESS_START) * len(ledger["results"]) / len(keys),
    )
    if policy == "selective_parallel" and pending and not ledger["decisions"]:
        await context.raise_if_stopped()
        async with context.span("polish", "triage"):
            ledger["decisions"] = await triage(
                {k: deepcopy(drafts[k]) for k in pending},
                frozen,
                runner,
                stop_check=context.raise_if_stopped,
                concurrency=width,
            )
        async with context.session() as session:
            document = await session.scalar(
                select(PaperDocument).where(PaperDocument.id == document_id).with_for_update()
            )
            current = {
                r.section_key: fingerprint(r.body_ir_json)
                for r in await list_sections(session, document_id)
                if r.section_key in drafts
            }
            if current != ledger["expected"]:
                raise JobStopped("pause", reason="polish_inputs_changed")
            state = deepcopy(document.writing_state_json or {})
            state["polish"] = deepcopy(ledger)
            document.writing_state_json = state

    async def generate(key):
        await context.raise_if_stopped()
        skip_requested = await _polish_skip_requested(context)
        draft = deepcopy(drafts[key])
        decision = ledger["decisions"].get(key, {"rewrite": True, "reason": "full_polish"})
        if skip_requested:
            outcome.polish_skipped = True
            decision = {"rewrite": False, "reason": "user_skipped"}
        if not decision["rewrite"]:
            draft.generation["polish_result"] = {
                "accepted": False,
                "changed": False,
                "reason": decision["reason"],
            }
        else:
            async with context.span("polish", "section", node_id=key):
                draft = await coherence_pass(
                    draft=draft, context=deepcopy(frozen), whitelist=set(whitelist), runner=runner
                )
        draft.generation["polish_decision"] = {**decision, "snapshot": ledger["snapshot"]}
        return draft

    async def commit(_index, key, result):
        if isinstance(result, BaseException):
            raise result
        await _persist_draft(
            context,
            document_id=document_id,
            draft=result,
            order_no=order_by_key[key],
            whitelist=whitelist,
            language=language,
            polish_guard=ledger,
        )
        drafts[key] = result
        await context.emit(
            "polish.section",
            {
                "section": key,
                "title": result.title,
                "policy": policy,
                "decision": result.generation["polish_decision"]["rewrite"],
                "result": result.generation["polish_result"],
                "done": len(ledger["results"]),
                "total": len(keys),
            },
            stage="polish",
            progress=_POLISH_PROGRESS_START
            + (_POLISH_PROGRESS_END - _POLISH_PROGRESS_START) * len(ledger["results"]) / len(keys),
        )

    await bounded_map(
        pending, generate, limit=width, on_ready=commit, stop_check=context.raise_if_stopped
    )
    for key in keys:
        writing_context.register(key, drafts[key])
    await _integrate_body(context, drafts, writing_context)
    results = list(ledger["results"].values())
    outcome.polished_count = sum(r["rewrite"] for r in results)
    outcome.polish_pending_count = 0
    await context.emit(
        "polish.completed",
        {
            "policy": policy,
            "total": len(keys),
            "done": len(results),
            "rewritten": outcome.polished_count,
            "skipped": sum(not r["rewrite"] for r in results),
            "rejected": sum(r["rewrite"] and not r["accepted"] for r in results),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        },
        stage="polish",
        progress=_POLISH_PROGRESS_END,
    )
