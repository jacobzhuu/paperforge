"""Deterministic, project-scoped semantic review inputs; no model calls or I/O."""

from __future__ import annotations

from typing import Any

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.quality import _claims_with_local_citations
from paperforge_worker.pipelines.review_contract import table_role_conflicts

REVIEW_INPUT_VERSION = "section-review-v3"
MAX_REVIEW_INPUT_CHARS = 64_000


def build_review_input(
    *,
    section_key: str,
    question: str,
    prose: str,
    body: dict,
    evidence: list[dict],
    whitelist: dict,
    scope: dict | None = None,
    assets: list[dict] | None = None,
) -> dict[str, Any]:
    units = {str(e.get("evidence_id") or e.get("id")): e for e in evidence}
    assets_by_ref = {
        str(a[k]): a
        for a in assets or []
        for k in ("_asset_ref", "_asset_id", "source_ref")
        if a.get(k)
    }
    claims, selected, selected_assets, issues = [], {}, {}, []
    for bi, block in enumerate(body.get("blocks") or []):
        containers = (
            [block.get("runs") or []]
            if block.get("type") == "paragraph"
            else [i.get("runs") or [] for i in block.get("items") or []]
        )
        for ci, runs in enumerate(containers):
            for si, (text, cites, ids, refs) in enumerate(
                _claims_with_local_citations(runs, min_chars=1)
            ):
                claim_id = f"{section_key}:{bi}:{ci}:{si}"
                valid = []
                for eid in ids:
                    unit = units.get(eid)
                    works = {str(whitelist[k]) for k in cites if k in whitelist}
                    if unit is None or not works or str(unit.get("work_id")) not in works:
                        issues.append(
                            {
                                "claim_id": claim_id,
                                "evidence_id": eid,
                                "reason": "missing_or_mismatched_binding",
                            }
                        )
                        continue
                    valid.append(eid)
                    selected[eid] = {
                        **unit,
                        "evidence_id": eid,
                        "selection_reason": "explicit_claim_binding",
                        "content_hash": fingerprint(unit),
                    }
                for cite in cites:
                    if cite not in whitelist:
                        issues.append(
                            {"claim_id": claim_id, "cite_key": cite, "reason": "unknown_citation"}
                        )
                # A citation without an EvidenceUnit does not acquire a fabricated binding.
                if cites and not ids:
                    issues.append({"claim_id": claim_id, "reason": "citation_without_evidence"})
                for ref in refs:
                    if ref in assets_by_ref:
                        selected_assets[ref] = assets_by_ref[ref]
                    else:
                        issues.append(
                            {
                                "claim_id": claim_id,
                                "source_ref": ref,
                                "reason": "missing_source_ref",
                            }
                        )
                claims.append(
                    {
                        "claim_id": claim_id,
                        "text": text,
                        "cite_keys": cites,
                        "evidence_ids": ids,
                        "resolved_evidence_ids": valid,
                        "source_refs": refs,
                    }
                )
    payload = {
        "version": REVIEW_INPUT_VERSION,
        "scope": scope or {},
        "section_key": section_key,
        "question": question,
        "prose": prose,
        "body_hash": fingerprint(body),
        "claims": claims,
        "evidence": [selected[k] for k in sorted(selected)],
        "assets": [selected_assets[k] for k in sorted(selected_assets)],
        "binding_issues": issues,
        "coverage": {
            "bound_ids": len({i for c in claims for i in c["evidence_ids"]}),
            "resolved_ids": len(selected),
            "binding_issues": len(issues),
        },
    }
    payload["table_role_conflicts"] = table_role_conflicts(payload)
    payload["input_hash"] = fingerprint(payload)
    return payload
