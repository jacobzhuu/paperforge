"""Whole manuscript coverage and summary-to-verified-body consistency."""

from __future__ import annotations

import re

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.semantic_review import review_section

FRAME_KEYS = {"abstract", "conclusion", "synthesis", "discussion"}


def _terms(text):
    return set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]{2}", text.casefold()))


def summary_material(material, verified, body_hash):
    claims, selected = [], {}
    for claim in material["claims"]:
        words = _terms(claim["text"])
        ranked = sorted(
            verified,
            key=lambda c: (
                len(
                    set(c.get("resolved_evidence_ids", []))
                    & set(claim.get("resolved_evidence_ids", []))
                ),
                len(words & _terms(c["text"])),
            ),
            reverse=True,
        )
        candidates = [
            c
            for c in ranked
            if words & _terms(c["text"])
            or set(c.get("resolved_evidence_ids", [])) & set(claim.get("resolved_evidence_ids", []))
        ][:8]
        selected.update({c["claim_id"]: c for c in candidates})
        claims.append({**claim, "allowed_body_claim_ids": [c["claim_id"] for c in candidates]})
    return {
        **material,
        "claims": claims,
        "verified_body_claims": list(selected.values()),
        "body_snapshot": body_hash,
        "summary_consistency_required": True,
    }


async def review_manuscript(context, inputs, *, language):
    verified, verdicts, unassessed = [], {}, []
    checks = []
    ordered = sorted(inputs, key=lambda i: i["section_key"] in FRAME_KEYS)
    body_hash = fingerprint(
        [i.get("review_input") for i in ordered if i["section_key"] not in FRAME_KEYS]
    )
    total = sum(len((i.get("review_input") or {}).get("claims", [])) for i in ordered)
    for item in ordered:
        material = item.get("review_input")
        if material and item["section_key"] in FRAME_KEYS:
            material = summary_material(material, verified, body_hash)
        verdict = await review_section(
            section_key=item["section_key"],
            question=item["question"],
            prose=item["prose"],
            evidence=item["evidence"],
            runner=context.llm_runner(),
            language=language,
            review_input=material,
            trace_context=context,
            repair_invalid=True,
        )
        if verdict is None:
            unassessed.append(item["section_key"])
            checks.extend(
                {"claim_id": c["claim_id"], "status": "unassessed"}
                for c in (material or {}).get("claims", [])
            )
            continue
        verdicts[item["section_key"]] = verdict.to_payload()
        checks.extend(verdict.claim_checks)
        if item["section_key"] not in FRAME_KEYS:
            blocked_ids = {i["claim_id"] for i in verdict.deterministic_findings}
            supported = {
                c["claim_id"]
                for c in verdict.claim_checks
                if c["status"] in {"supported", "inference"} and c["claim_id"] not in blocked_ids
            }
            verified.extend(
                c for c in (material or {}).get("claims", []) if c["claim_id"] in supported
            )
        if hasattr(context, "emit"):
            await context.emit(
                "review.manuscript_progress",
                {"section_key": item["section_key"]},
                checkpoint={
                    "manuscript_review_progress": {
                        "status": "running",
                        "claim_count": total,
                        "checked_count": sum(c["status"] != "unassessed" for c in checks),
                        "uncertain_count": sum(c["status"] == "uncertain" for c in checks),
                        "unassessed_count": total
                        - sum(c["status"] != "unassessed" for c in checks),
                        "sections_reviewed": len(verdicts),
                        "unassessed": unassessed,
                        "sections_failing": [k for k, v in verdicts.items() if not v["acceptable"]],
                    }
                },
            )
    pending = sum(c["status"] == "unassessed" for c in checks)
    uncertain = sum(c["status"] == "uncertain" for c in checks)
    if hasattr(context, "emit"):
        await context.emit(
            "review.manuscript_completed",
            {
                "claim_count": len(checks),
                "unassessed_count": pending,
                "uncertain_count": uncertain,
                "body_snapshot": body_hash,
            },
            checkpoint={"verified_body_review": {"body_snapshot": body_hash, "claims": verified}},
        )
    return {
        "status": "partial" if pending or uncertain or unassessed else "completed",
        "sections_reviewed": len(verdicts),
        "unassessed": unassessed,
        "sections_failing": [k for k, v in verdicts.items() if not v["acceptable"]],
        "claim_count": len(checks),
        "checked_count": len(checks) - pending,
        "unassessed_count": pending,
        "uncertain_count": uncertain,
        "supported_count": sum(c["status"] in {"supported", "inference"} for c in checks),
        "findings": list(verdicts.values()),
        "body_snapshot": body_hash,
    }
