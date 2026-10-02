"""Bounded, resumable claim review. Unknown findings never count as clearance."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.review_inputs import MAX_REVIEW_INPUT_CHARS, REVIEW_INPUT_VERSION

MAX_BATCH_CLAIMS = 8


def subset(material: dict, claims: list[dict]) -> dict:
    ids = {c["claim_id"] for c in claims}
    evidence_ids = {e for c in claims for e in c.get("resolved_evidence_ids", [])}
    refs = {r for c in claims for r in c.get("source_refs", [])}
    result = {
        **material,
        "claims": claims,
        "verified_body_claims": [
            c
            for c in material.get("verified_body_claims", [])
            if c["claim_id"]
            in {i for claim in claims for i in claim.get("allowed_body_claim_ids", [])}
        ],
        "prose": "\n".join(dict.fromkeys(c.get("paragraph_context") or c["text"] for c in claims)),
        "evidence": [
            e
            for e in material.get("evidence", [])
            if str(e.get("evidence_id") or e.get("id")) in evidence_ids
        ],
        "assets": [
            a
            for a in material.get("assets", [])
            if any(str(a.get(k)) in refs for k in ("_asset_ref", "_asset_id", "source_ref"))
        ],
        "derived_comparisons": [
            c
            for c in material.get("derived_comparisons", [])
            if set(c["evidence_ids"]) <= evidence_ids
        ],
        "binding_issues": [i for i in material.get("binding_issues", []) if i["claim_id"] in ids],
        "table_role_conflicts": [
            i for i in material.get("table_role_conflicts", []) if i["claim_id"] in ids
        ],
    }
    result.pop("input_hash", None)
    return result


def batches(material: dict) -> list[dict]:
    result, current = [], []
    for claim in material["claims"]:
        candidate = subset(material, current + [claim])
        if current and (
            len(current) >= MAX_BATCH_CLAIMS
            or len(json.dumps(candidate, ensure_ascii=False, default=str)) > MAX_REVIEW_INPUT_CHARS
        ):
            result.append(subset(material, current))
            current = []
        current.append(claim)
    if current:
        result.append(subset(material, current))
    return result


def unknown(claim: dict, reason: str) -> dict:
    return {
        "claim_id": claim["claim_id"],
        "status": "unassessed",
        "evidence_ids": [],
        "reason": reason,
    }


async def review_batched(
    *,
    section_key,
    question,
    prose,
    evidence,
    runner,
    language="zh",
    review_input=None,
    trace_context=None,
    repair_invalid=False,
):
    from paperforge_worker.pipelines.semantic_review import (
        _review_section_batch,
        build_section_verdict,
    )

    if review_input is None or "claims" not in review_input:
        return await _review_section_batch(
            section_key=section_key,
            question=question,
            prose=prose,
            evidence=evidence,
            runner=runner,
            language=language,
            review_input=review_input,
            trace_context=trace_context,
            repair_invalid=repair_invalid,
        )
    pieces = batches(review_input)
    if not pieces:
        return build_section_verdict(
            {
                "answers_question": "full",
                "support": "sufficient",
                "calibration": "matched",
                "synthesis_mode": "synthesized",
                "diagnosis": "none",
                "gap_declared": False,
                "claim_checks": [],
                "rationale": "No factual claims in this section.",
            },
            section_key=section_key,
        )
    cache = {}
    if trace_context is not None and hasattr(trace_context, "checkpoint"):
        cache = dict(trace_context.checkpoint.get("claim_review_batches") or {})
    results = []
    for piece in pieces:
        if trace_context is not None and hasattr(trace_context, "raise_if_stopped"):
            await trace_context.raise_if_stopped()
        key = fingerprint(
            {
                "version": REVIEW_INPUT_VERSION,
                # Unchanged claims retain their results after an unrelated sentence edit.
                # Paragraph context, evidence, bindings, scope and summary body snapshot remain.
                "material": {k: v for k, v in piece.items() if k not in {"body_hash", "coverage"}},
                "model": runner.model_for("section_reviewer")
                if runner and hasattr(runner, "model_for")
                else "unknown",
            }
        )
        payload = cache.get(key)
        previous = payload
        pending_ids = {
            c["claim_id"]
            for c in (payload or {}).get("claim_checks", [])
            if c.get("status") == "unassessed"
        }
        if payload is None or pending_ids:
            pending_piece = (
                subset(piece, [c for c in piece["claims"] if c["claim_id"] in pending_ids])
                if pending_ids
                else piece
            )
            verdict = await _review_section_batch(
                section_key=section_key,
                question=question,
                prose=pending_piece["prose"],
                evidence=pending_piece["evidence"],
                runner=runner,
                language=language,
                review_input=pending_piece,
                trace_context=trace_context,
                repair_invalid=True,
            )
            if verdict is None:
                oversized = (
                    len(json.dumps(piece, ensure_ascii=False, default=str)) > MAX_REVIEW_INPUT_CHARS
                )
                payload = {
                    "answers_question": "partial",
                    "support": "thin",
                    "calibration": "matched",
                    "synthesis_mode": "mixed",
                    "diagnosis": "none",
                    "gap_declared": False,
                    "claim_checks": [
                        unknown(
                            c,
                            "input_budget_exceeded"
                            if oversized
                            else "model_unavailable_or_invalid",
                        )
                        for c in piece["claims"]
                    ],
                }
            else:
                payload = verdict.to_payload()
                if previous and pending_ids:
                    replacements = {c["claim_id"]: c for c in payload["claim_checks"]}
                    payload["claim_checks"] = [
                        replacements.get(c["claim_id"], c) for c in previous["claim_checks"]
                    ]
                    for field, order in {
                        "answers_question": ["full", "partial", "no"],
                        "synthesis_mode": ["synthesized", "mixed", "listed"],
                        "calibration": ["matched", "underclaimed", "overclaimed"],
                    }.items():
                        payload[field] = max((previous[field], payload[field]), key=order.index)
                    payload["unanswered_aspects"] = list(
                        dict.fromkeys(
                            previous.get("unanswered_aspects", [])
                            + payload.get("unanswered_aspects", [])
                        )
                    )
                    payload["unsupported_claims"] = list(
                        dict.fromkeys(
                            previous.get("unsupported_claims", [])
                            + payload.get("unsupported_claims", [])
                        )
                    )
                    payload["deterministic_findings"] = previous.get(
                        "deterministic_findings", []
                    ) + payload.get("deterministic_findings", [])
                if trace_context is not None and hasattr(trace_context, "checkpoint"):
                    if not hasattr(trace_context, "_claim_review_lock"):
                        trace_context._claim_review_lock = asyncio.Lock()
                    async with trace_context._claim_review_lock:
                        cache = dict(trace_context.checkpoint.get("claim_review_batches") or {})
                        cache[key] = payload
                        await trace_context.emit(
                            "review.batch_completed",
                            {
                                "section_key": section_key,
                                "batch_hash": key,
                                "claims": len(piece["claims"]),
                            },
                            checkpoint={"claim_review_batches": cache},
                        )
        if previous and pending_ids and verdict is None:
            payload = previous
        results.append(payload)
    checks = [c for p in results for c in p.get("claim_checks", [])]

    def worst(field, order):
        return max((p.get(field, order[-1]) for p in results), key=order.index)

    return build_section_verdict(
        {
            "answers_question": worst("answers_question", ["full", "partial", "no"]),
            "support": worst("support", ["sufficient", "thin", "unsupported"]),
            "calibration": worst("calibration", ["matched", "underclaimed", "overclaimed"]),
            "synthesis_mode": worst("synthesis_mode", ["synthesized", "mixed", "listed"]),
            "diagnosis": worst(
                "diagnosis", ["none", "writing_gap", "synthesis_gap", "evidence_gap"]
            ),
            "gap_declared": all(
                p.get("gap_declared") for p in results if p.get("answers_question") != "full"
            ),
            "claim_checks": checks,
            "unsupported_claims": [s for p in results for s in p.get("unsupported_claims", [])],
            "deterministic_findings": [
                s for p in results for s in p.get("deterministic_findings", [])
            ],
            "unanswered_aspects": [s for p in results for s in p.get("unanswered_aspects", [])],
            "rationale": " / ".join(p.get("rationale", "") for p in results),
        },
        section_key=section_key,
    )


def valid_checks(payload: Any, material: dict) -> tuple[list[dict], dict[str, str]]:
    """Salvage only individually valid checks, never fabricate a model judgment."""
    from paperforge_worker.pipelines.review_contract import validate_claim_checks

    claims = material["claims"]
    if not isinstance(payload, dict):
        return [], {c["claim_id"]: "invalid_json" for c in claims}
    checks = payload.get("claim_checks")
    if not isinstance(checks, list):
        return [], {c["claim_id"]: "incomplete_claim_coverage" for c in claims}
    good, errors = [], {}
    for claim in claims:
        matched = [
            c for c in checks if isinstance(c, dict) and c.get("claim_id") == claim["claim_id"]
        ]
        error = (
            "invalid_or_duplicate_claim_id"
            if len(matched) != 1
            else validate_claim_checks(
                {**payload, "claim_checks": matched}, {**material, "claims": [claim]}
            )
        )
        if error:
            errors[claim["claim_id"]] = error
        else:
            good.append(matched[0])
    # Unanchored allegations or foreign IDs must not vanish during salvage.
    ids = {c["claim_id"] for c in claims}
    extra = payload.get("unsupported_claims", [])
    failed = {c["claim_id"] for c in good if c["status"] in {"unsupported", "contradicted"}}
    if (
        any(not isinstance(c, dict) or c.get("claim_id") not in ids for c in checks)
        or not isinstance(extra, list)
        or any(not isinstance(s, str) or not any(cid in s for cid in failed) for s in extra)
    ):
        return [], {c["claim_id"]: "unanchored_review_finding" for c in claims}
    return good, errors
