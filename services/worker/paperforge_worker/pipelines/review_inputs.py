"""Deterministic, project-scoped semantic review inputs; no model calls or I/O."""

from __future__ import annotations

from typing import Any

from paperforge_worker.orchestration.writing_graph import fingerprint
from paperforge_worker.pipelines.quality import _claims_with_local_citations
from paperforge_worker.pipelines.review_contract import table_role_conflicts

REVIEW_INPUT_VERSION = "section-review-v4"
MAX_REVIEW_INPUT_CHARS = 48_000


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
    work_keys = {str(work): key for key, work in whitelist.items()}
    claims, selected, selected_assets, issues = [], {}, {}, []
    for bi, block in enumerate(body.get("blocks") or []):
        containers = (
            [block.get("runs") or []]
            if block.get("type") == "paragraph"
            else [i.get("runs") or [] for i in block.get("items") or []]
        )
        if block.get("type") in {"table", "equation", "figure", "algorithm"}:
            import json

            source = block.get("source") or {}
            data = source.get("data") or {}
            keys = data.get("cite_keys") or []
            ids = data.get("evidence_ids") or block.get("source_ids") or []
            refs = [r for r in [source.get("ref"), block.get("asset_ref")] if r]
            if block.get("type") == "equation":
                ids = []
                for source_id in block.get("source_ids") or []:
                    if source_id in units:
                        ids.append(source_id)
                        key = work_keys.get(str(units[source_id].get("work_id")))
                        if key:
                            keys.append(key)
                    elif source_id in assets_by_ref:
                        refs.append(source_id)
                    else:
                        ids.append(source_id)
            if block.get("type") == "table" and not keys and bi > 0:
                # Older inline tables store their explicit sources in a preceding cite-only block.
                previous = (body.get("blocks") or [])[bi - 1]
                prior_runs = previous.get("runs") or []
                if prior_runs and all(r.get("t") == "cite" for r in prior_runs):
                    keys = [k for r in prior_runs for k in r.get("keys", [])]
                    ids = [e for r in prior_runs for e in r.get("evidence_ids", [])]
            text = json.dumps(block, ensure_ascii=False, sort_keys=True)
            containers = [
                [
                    {"t": "text", "v": text},
                    {"t": "cite", "keys": keys, "evidence_ids": ids},
                    {"t": "grounding", "source_refs": refs},
                ]
            ]
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
                        "paragraph_context": " ".join(
                            str(r.get("v", "")) for r in runs if r.get("t") == "text"
                        ),
                        "cite_keys": cites,
                        "evidence_ids": ids,
                        "resolved_evidence_ids": valid,
                        "source_refs": refs,
                    }
                )
    payload = {
        "section_role": (scope or {}).get("section_role", "body"),
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
    from paperforge_worker.pipelines.field_provenance import derived_comparisons

    payload["derived_comparisons"] = derived_comparisons(payload["evidence"])
    payload["table_role_conflicts"] = table_role_conflicts(payload)
    payload["input_hash"] = fingerprint(payload)
    return payload
