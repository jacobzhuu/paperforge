"""Apply validated sentence patches without regenerating unrelated text or bindings."""

from __future__ import annotations

import copy
import json

from db import latest_document, list_sections, upsert_section
from db.models.paper import PaperDocument
from sqlalchemy import select

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.claim_review import subset
from paperforge_worker.pipelines.semantic_review import review_section


def replace_claim(body, claim, text):
    """Ambiguous/split text and structured objects are left for explicit review."""
    parts = claim["claim_id"].rsplit(":", 3)
    try:
        bi, ci, _ = map(int, parts[1:])
        block = body["blocks"][bi]
        runs = block["runs"] if block["type"] == "paragraph" else block["items"][ci]["runs"]
    except (ValueError, KeyError, IndexError):
        return False
    matching = [r for r in runs if r.get("t") == "text" and claim["text"] in r.get("v", "")]
    if len(matching) != 1 or matching[0]["v"].count(claim["text"]) != 1:
        return False
    matching[0]["v"] = matching[0]["v"].replace(claim["text"], text, 1)
    return True


async def repair_local(context, item, verdict, *, language):
    """Return handled=True for factual-only failures, including unsuccessful repairs."""
    if verdict.synthesis_mode == "listed" or verdict.unanswered_aspects:
        return False
    failed = {
        c["claim_id"]: c
        for c in verdict.claim_checks
        if c["status"] in {"unsupported", "contradicted"}
    }
    if not failed or verdict.deterministic_findings:
        return False
    material = item["review_input"]
    async with context.session() as session:
        document = await latest_document(session, context.project_id)
        rows = await list_sections(session, document.id)
        row = next(r for r in rows if r.section_key == item["section_key"])
        body = copy.deepcopy(row.body_ir_json)
        metadata = {k: getattr(row, k) for k in ("title", "order_no", "parent_key", "model")}
        cites, refs = row.cite_keys_json, row.asset_refs_json
        document_id = document.id
    original_hash = fingerprint(body)
    if original_hash != material.get("body_hash"):
        return True
    runner = context.llm_runner()
    for claim in material["claims"]:
        if claim["claim_id"] not in failed:
            continue
        await context.raise_if_stopped()
        one = subset(material, [claim])
        result = await runner.agenerate_json(
            "writer",
            system_prompt=(
                '只修复指定论断，输出 JSON {"text":"修复句"}。仅使用绑定证据；'
                "保留主体和研究范围，不新增结论，不把数字改成无依据的定性断言。"
                "证据不足时 text=null，不改写其他句子。"
            ),
            user_prompt=json.dumps(
                {"input": one, "finding": failed[claim["claim_id"]]},
                ensure_ascii=False,
                default=str,
            ),
            max_output_tokens=2000,
            temperature=0.0,
            metadata={"stage": "claim_repair", "section": item["section_key"]},
        )
        candidate = (
            result.value.get("text") if result.ok and isinstance(result.value, dict) else None
        )
        accepted = False
        if isinstance(candidate, str) and candidate.strip():
            revised = {
                **claim,
                "text": candidate,
                "paragraph_context": claim.get("paragraph_context", claim["text"]).replace(
                    claim["text"], candidate, 1
                ),
            }
            checked = await review_section(
                section_key=item["section_key"],
                question=item["question"],
                prose=candidate,
                evidence=one["evidence"],
                runner=runner,
                language=language,
                review_input=subset(one, [revised]),
                trace_context=context,
                repair_invalid=True,
            )
            accepted = bool(
                checked
                and checked.acceptable
                and checked.claim_checks
                and all(
                    c["status"] in {"supported", "inference", "gap", "nonfactual"}
                    for c in checked.claim_checks
                )
            )
            if accepted:
                accepted = replace_claim(body, claim, candidate)
        await context.emit(
            "revision.claim_repair",
            {
                "claim_id": claim["claim_id"],
                "original": claim["text"],
                "candidate": candidate,
                "accepted": accepted,
                "evidence_hash": fingerprint(one),
            },
        )
    if fingerprint(body) == original_hash:
        return True
    async with context.session() as session:
        await session.scalar(
            select(PaperDocument.id).where(PaperDocument.id == document_id).with_for_update()
        )
        current = await list_sections(session, document_id)
        row = next(r for r in current if r.section_key == item["section_key"])
        if fingerprint(row.body_ir_json) != original_hash:
            await context.emit("revision.patch_conflict", {"section_key": item["section_key"]})
            return True
        await upsert_section(
            session,
            document_id=document_id,
            section_key=item["section_key"],
            body_ir=body,
            cite_keys=cites,
            asset_refs=refs,
            **metadata,
        )
    return True
