from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from paperforge_worker.pipelines.outline import systematic_method_section
from paperforge_worker.pipelines.quality import (
    apply_readiness_gate,
    build_claim_evidence,
    build_quality_report,
    classify_claim,
)


def _row(text: str, *, status: str = "approved") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        section_key="results",
        title="Results",
        status=status,
        cite_keys_json=["smith2020"],
        body_ir_json={
            "blocks": [
                {
                    "type": "paragraph",
                    "runs": [
                        {"t": "text", "v": text},
                        {"t": "cite", "keys": ["smith2020"]},
                    ],
                }
            ]
        },
    )


def _project() -> SimpleNamespace:
    return SimpleNamespace(
        publication_title="A submission title",
        authors_json=["Ada Lovelace"],
        keywords_json=["evidence"],
        metadata_confirmed_at=datetime.now(UTC),
        language="en",
    )


def _report(row: SimpleNamespace, anchors: list[dict]):
    report = build_quality_report(
        sections=[
            {
                "section_key": row.section_key,
                "title": row.title,
                "word_count": 3200,
                "cite_keys": row.cite_keys_json,
                "kind": "body",
            }
        ],
        whitelist_size=1,
        publication_years=[2020],
        fulltext_coverage=1.0,
    )
    report.quality_profile = "submission"
    report.review_style = "narrative"
    report.claim_evidence = anchors
    report.layout_checks = {"passed": None, "status": "not_run"}
    return report


def test_abstract_cannot_support_a_core_numeric_claim() -> None:
    row = _row("The treatment increased recovery by 27%.")
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": False,
                "quotable_points": [{"text": "Recovery increased by 27%."}],
            }
        },
    )
    report = _report(row, anchors)
    apply_readiness_gate(
        report,
        rows=[row],
        project=_project(),
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert report.core_claim_fulltext_coverage == 0
    assert any(item["code"] == "core_claim_fulltext_missing" for item in report.blockers)


def test_citations_bind_to_the_nearest_claim_not_the_whole_paragraph() -> None:
    row = _row("unused")
    row.body_ir_json = {
        "blocks": [
            {
                "type": "paragraph",
                "runs": [
                    {"t": "text", "v": "This is established background context."},
                    {"t": "cite", "keys": ["background2020"]},
                    {"t": "text", "v": "The treatment increased recovery by 27%."},
                    {"t": "cite", "keys": ["result2024"]},
                ],
            }
        ]
    }
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "background2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": False,
                "quotable_points": [{"text": "Established background context."}],
            },
            "result2024": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "The treatment increased recovery by 27%.",
                        "page": 8,
                        "section": "Results",
                        "paragraph": 2,
                    }
                ],
            },
        },
    )
    assert [(anchor["claim_kind"], anchor["cite_key"]) for anchor in anchors] == [
        ("background", "background2020"),
        ("numeric", "result2024"),
    ]


def test_located_fulltext_can_support_a_core_numeric_claim() -> None:
    row = _row("The treatment increased recovery by 27%.")
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "The treatment increased recovery by 27%.",
                        "page": 8,
                        "section": "Results",
                        "paragraph": 2,
                    }
                ],
            }
        },
    )
    report = _report(row, anchors)
    apply_readiness_gate(
        report,
        rows=[row],
        project=_project(),
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert report.core_claim_fulltext_coverage == 1
    assert report.readiness_status == "preflight_ready"


def test_claim_matrix_selects_the_best_matching_fulltext_excerpt() -> None:
    row = _row("The treatment increased recovery by 27%.")
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {"text": "An unrelated methods description.", "page": 2},
                    {
                        "text": "The treatment increased recovery by 27%.",
                        "page": 8,
                        "section": "Results",
                        "paragraph": 2,
                    },
                ],
            }
        },
    )
    assert anchors[0]["source_page"] == 8
    assert anchors[0]["support_status"] == "supported"


def test_manual_confirmation_can_promote_located_fulltext_after_recheck() -> None:
    row = _row("The treatment increased recovery by 27%.")
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "A located but lexically different result statement.",
                        "page": 8,
                        "section": "Results",
                        "paragraph": 2,
                    }
                ],
            }
        },
    )
    assert anchors[0]["support_status"] == "insufficient_support"
    anchors[0]["manual_status"] = "confirmed"
    report = _report(row, anchors)
    apply_readiness_gate(
        report,
        rows=[row],
        project=_project(),
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert report.core_claim_fulltext_coverage == 1
    assert report.readiness_status == "preflight_ready"


def test_manual_rejection_overrides_automatic_support_after_recheck() -> None:
    row = _row("The treatment increased recovery by 27%.")
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "The treatment increased recovery by 27%.",
                        "page": 8,
                        "section": "Results",
                        "paragraph": 2,
                    }
                ],
            }
        },
    )
    anchors[0]["manual_status"] = "rejected"
    report = _report(row, anchors)
    apply_readiness_gate(
        report,
        rows=[row],
        project=_project(),
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert report.core_claim_fulltext_coverage == 0
    assert any(item["code"] == "core_claim_fulltext_missing" for item in report.blockers)


def test_chemical_name_is_not_classified_as_a_numeric_claim() -> None:
    assert classify_claim("吲哚-3-乙酸参与植物信号传导。") == "background"


def test_model_and_gene_identifiers_are_not_numeric_claims() -> None:
    assert classify_claim("GPT-12 and IL-17 are established identifiers.") == "background"


def test_publication_title_must_match_the_confirmed_language_script() -> None:
    row = _row("Established background context only.")
    report = _report(row, build_claim_evidence(rows=[row], evidence_sources={}))
    project = _project()
    project.publication_title = "中文项目内部题名"
    apply_readiness_gate(
        report,
        rows=[row],
        project=project,
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert any(item["code"] == "metadata_script_mismatch" for item in report.blockers)


def test_systematic_method_section_is_deterministic_from_search_log() -> None:
    section = systematic_method_section(
        {
            "databases": ["OpenAlex", "Crossref"],
            "queries": ["root injury"],
            "dates": ["2026-07-27"],
            "retrieved_count": 42,
            "selected_count": 12,
            "inclusion_criteria": ["direct relevance"],
        },
        language="en",
    )
    assert section["key"] == "search_methods"
    assert "42 records" in section["deterministic_text"]
    assert "12 works" in section["deterministic_text"]
    assert section["cite_keys"] == []


def test_failed_search_is_visible_in_narrative_quality_warnings() -> None:
    row = _row("Established background context only.")
    report = _report(row, build_claim_evidence(rows=[row], evidence_sources={}))
    report.quality_profile = "draft"
    apply_readiness_gate(
        report,
        rows=[row],
        project=_project(),
        whitelist={"smith2020"},
        search_runs=[SimpleNamespace(status="failed", error="timeout", provider="openalex")],
    )
    assert any(item["code"] == "search_degraded" for item in report.warnings)
