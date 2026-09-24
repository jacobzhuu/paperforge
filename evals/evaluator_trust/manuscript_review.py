"""OFFLINE EXPERIMENT ONLY: final manuscript audit; not a production quality gate.

No rewrite or retrieval. Persist the exact input/output; incomplete assessment is not a pass.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.review_contract import EVIDENCE_REVIEW_RULES
from storage import make_object_store

VERSION = "manuscript-review-v1"
MAX_INPUT_CHARS = 320_000
PROMPT = (
    """核查最终完整文稿的一致性和证据支持，不重写正文。
所有章节（包括摘要、引言、结论、综合、表格、附录）都要检查。
重点：同一方法的结果方向、实验条件与shot是否跨章改变；总结是否把常规分类结果升级为
少样本结果；表内缺值是否变成有效指标；教师/学生是否倒置；排版长度是否被误作指标；
源材料的内部矛盾是否未经说明复制到文稿。只报告具体、可定位的实质问题。
不得为了凑问题把合理限定的作者自报、表内推理或普通证据缺口判错。
每项问题必须引用原始section.body中的连续原文quote和相应section_key。
至少一项quote必须来自文稿；对跨章冲突同时引用两处。evidence_ids仅可用输入IDs。
返回JSON：{"reviewed_sections":["全部输入section_key"],
"findings":[{"kind":"contradiction|unsupported|source_conflict|artifact_error",
"locations":[{"section_key":"...","quote":"原文短句"}],
"evidence_ids":["..."],"reason":"具体冲突或证据边界"}]}。
无问题返回空findings，不输出建议或审美偏好。无法完成时不要声称覆盖所有章节。
"""
    + EVIDENCE_REVIEW_RULES
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def build_manuscript_input(rows: list, evidence: dict, *, snapshot: str) -> dict:
    sections = [
        {"section_key": r.section_key, "title": r.title, "body": r.body_ir_json or {}} for r in rows
    ]

    # Grounding is explicit; do not inject all project evidence as if it were cited.
    def ids(value: Any) -> set[str]:
        if isinstance(value, dict):
            found = set(map(str, value.get("evidence_ids") or []))
            return found | {eid for v in value.values() for eid in ids(v)}
        if isinstance(value, list):
            return {eid for v in value for eid in ids(v)}
        return set()

    bound = ids(sections)
    return {
        "version": VERSION,
        "snapshot": snapshot,
        "sections": sections,
        "evidence": [evidence[e] for e in sorted(bound) if e in evidence],
        "missing_evidence_ids": sorted(bound - evidence.keys()),
    }


def validate_manuscript_result(payload: Any, material: dict) -> str | None:
    if not isinstance(payload, dict):
        return "invalid_output"
    sections = {s["section_key"]: s for s in material["sections"]}
    covered = payload.get("reviewed_sections")
    if (
        not isinstance(covered, list)
        or any(not isinstance(k, str) for k in covered)
        or len(covered) != len(sections)
        or set(covered) != set(sections)
    ):
        return "incomplete_section_coverage"
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return "invalid_findings"
    known = {str(e.get("evidence_id") or e.get("id")) for e in material["evidence"]}
    for f in findings:
        if (
            not isinstance(f, dict)
            or not isinstance(f.get("kind"), str)
            or f["kind"]
            not in {"contradiction", "unsupported", "source_conflict", "artifact_error"}
        ):
            return "invalid_finding"
        if not isinstance(f.get("reason"), str) or not f["reason"].strip():
            return "missing_reason"
        locations = f.get("locations")
        if not isinstance(locations, list) or not locations:
            return "missing_location"
        for loc in locations:
            if not isinstance(loc, dict):
                return "invalid_location"
            key, quote = loc.get("section_key"), loc.get("quote")
            if not isinstance(key, str) or key not in sections or not isinstance(quote, str):
                return "invalid_location"
            if len(quote.strip()) < 4 or not any(
                quote in s for s in _strings(sections[key]["body"])
            ):
                return "unverifiable_quote"
        eids = f.get("evidence_ids")
        if not isinstance(eids, list) or any(
            not isinstance(e, str) or e not in known for e in eids
        ):
            return "unknown_evidence"
    return None


async def review_manuscript(material: dict, *, runner: Any, context: Any) -> dict:
    serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    input_hash = fingerprint(material)
    request = {
        "input": material,
        "system_prompt": PROMPT,
        "temperature": 0.0,
        "max_output_tokens": 8192,
    }
    artifact_hash = fingerprint(request)
    store = make_object_store(context.settings)
    key = f"projects/{context.project_id}/manuscript-reviews/{artifact_hash}.json"
    await asyncio.to_thread(
        store.put,
        key,
        json.dumps(request, ensure_ascii=False, sort_keys=True, default=str).encode(),
        content_type="application/json",
    )
    meta = {
        "stage": "manuscript_review",
        "review_version": VERSION,
        "review_input_hash": input_hash,
        "snapshot": material["snapshot"],
    }
    await context.emit("manuscript_review.input", {**meta, "artifact_key": key})
    reason = None
    payload = None
    if len(serialized) > MAX_INPUT_CHARS:
        reason = "input_budget_exceeded"
    elif runner is None or not runner.enabled:
        reason = "model_unavailable"
    else:
        result = await runner.agenerate_json(
            "section_reviewer",
            system_prompt=PROMPT,
            user_prompt=serialized,
            max_output_tokens=8192,
            temperature=0.0,
            metadata=meta,
        )
        payload = result.value if result.ok else None
        reason = validate_manuscript_result(payload, material)
    receipt = {
        **meta,
        "status": "unassessed" if reason else "assessed",
        "reason": reason,
        "acceptable": False if reason else not payload["findings"],
        "result": payload,
        "artifact_key": key,
    }
    result_key = (
        f"projects/{context.project_id}/manuscript-reviews/{fingerprint(receipt)}.result.json"
    )
    await asyncio.to_thread(
        store.put,
        result_key,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str).encode(),
        content_type="application/json",
    )
    await context.emit("manuscript_review.result", {**receipt, "result_artifact_key": result_key})
    return receipt
