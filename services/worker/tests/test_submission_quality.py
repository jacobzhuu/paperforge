from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from paperforge_worker.pipelines.outline import systematic_method_section
from paperforge_worker.pipelines.quality import (
    apply_readiness_gate,
    build_claim_evidence,
    build_depth_metrics,
    build_original_claim_grounding,
    build_quality_report,
    classify_claim,
    verify_cross_language_claim_evidence,
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


async def test_bilingual_semantic_verification_clears_review_evidence_and_diversity_gates() -> None:
    first = _row("狄利克雷邻域采样降低了污染节点对目标物品表示的影响。")
    first.section_key = "s1"
    first.body_ir_json["blocks"][0]["runs"][1] = {
        "t": "cite",
        "keys": ["defense2022"],
    }
    first.cite_keys_json = ["defense2022"]
    second = _row("传统目标攻击在联邦推荐约束下的效能显著下降。")
    second.section_key = "s2"
    second.body_ir_json["blocks"][0]["runs"][1] = {
        "t": "cite",
        "keys": ["federated2025"],
    }
    second.cite_keys_json = ["federated2025"]
    first_work, second_work = uuid.uuid4(), uuid.uuid4()
    anchors = build_claim_evidence(
        rows=[first, second],
        evidence_sources={
            "defense2022": {
                "work_id": first_work,
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": (
                            "Dirichlet neighborhood sampling reduces the influence of "
                            "polluted nodes on target-item representations."
                        ),
                        "section": "Methodology",
                        "paragraph": 2,
                    }
                ],
            },
            "federated2025": {
                "work_id": second_work,
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": (
                            "Conventional targeted attacks become substantially less "
                            "effective under federated recommendation constraints."
                        ),
                        "section": "Experiments",
                        "paragraph": 1,
                    }
                ],
            },
        },
    )
    assert all(anchor["support_status"] == "insufficient_support" for anchor in anchors)

    class _Verifier:
        enabled = True

        async def agenerate_json(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return SimpleNamespace(
                ok=True,
                value={
                    "judgements": [
                        {
                            "index": 0,
                            "verdict": "supported",
                            "confidence": 0.98,
                            "reason": "The first excerpt directly states the same effect.",
                        },
                        {
                            "index": 1,
                            "verdict": "supported",
                            "confidence": 0.97,
                            "reason": "The second excerpt directly states the same limitation.",
                        },
                    ]
                },
            )

    summary = await verify_cross_language_claim_evidence(
        anchors=anchors,
        runner=_Verifier(),  # type: ignore[arg-type]
    )
    assert summary["promoted_count"] == 2

    report = _report(first, anchors)
    report.quality_profile = "scholarly"
    project = _project()
    project.paper_type = "review"
    apply_readiness_gate(
        report,
        rows=[first, second],
        project=project,
        whitelist={"defense2022", "federated2025"},
        search_runs=[],
    )
    assert report.core_claim_fulltext_coverage == 1
    assert report.readiness_status == "preflight_ready"
    assert not {
        "core_claim_fulltext_missing",
        "review_source_diversity_low",
    } & {item["code"] for item in report.blockers}


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


def test_manual_confirmation_counts_toward_review_source_diversity() -> None:
    first = _row("The first treatment improved recovery substantially.")
    second = _row("The second intervention reduced recurrence substantially.")
    first.cite_keys_json = ["first2024"]
    second.cite_keys_json = ["second2025"]
    first.body_ir_json["blocks"][0]["runs"][-1]["keys"] = ["first2024"]
    second.body_ir_json["blocks"][0]["runs"][-1]["keys"] = ["second2025"]
    anchors = build_claim_evidence(
        rows=[first, second],
        evidence_sources={
            "first2024": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "Alpha beta gamma delta.",
                        "section": "Results",
                    }
                ],
            },
            "second2025": {
                "work_id": uuid.uuid4(),
                "fulltext_used": True,
                "quotable_points": [
                    {
                        "text": "Epsilon zeta eta theta.",
                        "section": "Results",
                    }
                ],
            },
        },
    )
    assert len(anchors) == 2
    assert all(anchor["support_status"] == "insufficient_support" for anchor in anchors)
    for anchor in anchors:
        anchor["manual_status"] = "confirmed"

    report = _report(first, anchors)
    report.quality_profile = "scholarly"
    project = _project()
    project.paper_type = "review"
    apply_readiness_gate(
        report,
        rows=[first, second],
        project=project,
        whitelist={"first2024", "second2025"},
        search_runs=[],
    )

    assert report.readiness_status == "preflight_ready"
    assert "review_source_diversity_low" not in {item["code"] for item in report.blockers}


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


def test_explicit_evidence_unit_drives_r4_grade_check() -> None:
    evidence_id = str(uuid.uuid4())
    work_id = uuid.uuid4()
    row = _row("The treatment increased recovery by 27%.")
    row.body_ir_json["blocks"][0]["runs"][1]["evidence_ids"] = [evidence_id]
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": work_id,
                "fulltext_used": False,
                "quotable_points": [],
            }
        },
        evidence_units={
            evidence_id: {
                "id": evidence_id,
                "work_id": str(work_id),
                "grade": "D_abstract_only",
                "text": "The treatment increased recovery by 27%.",
                "measurements": [],
            }
        },
    )
    assert anchors[0]["evidence_unit_id"] == evidence_id
    assert anchors[0]["grade_ok"] is False
    assert anchors[0]["support_status"] == "grade_not_permitted"


def test_r6_numeric_claim_requires_page_or_structured_object() -> None:
    evidence_id = str(uuid.uuid4())
    work_id = uuid.uuid4()
    row = _row("The treatment increased recovery by 27%.")
    row.body_ir_json["blocks"][0]["runs"][1]["evidence_ids"] = [evidence_id]
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "smith2020": {
                "work_id": work_id,
                "fulltext_used": True,
                "quotable_points": [],
            }
        },
        evidence_units={
            evidence_id: {
                "id": evidence_id,
                "work_id": str(work_id),
                "grade": "B_located_prose",
                "text": "The treatment increased recovery by 27%.",
                "section_path": "Results",
                "measurements": [],
            }
        },
    )
    assert anchors[0]["grade_ok"] is True
    assert anchors[0]["support_status"] == "numeric_locator_missing"


def test_r5_cross_study_comparison_requires_shared_comparability_key() -> None:
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    first_work, second_work = uuid.uuid4(), uuid.uuid4()
    row = _row("Method A was higher than Method B.")
    row.cite_keys_json = ["first2024", "second2024"]
    row.body_ir_json["blocks"][0]["runs"][1] = {
        "t": "cite",
        "keys": row.cite_keys_json,
        "evidence_ids": [first_id, second_id],
    }
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "first2024": {"work_id": first_work, "fulltext_used": True},
            "second2024": {"work_id": second_work, "fulltext_used": True},
        },
        evidence_units={
            first_id: {
                "id": first_id,
                "work_id": str(first_work),
                "grade": "A_located_structured",
                "text": "Method A was higher.",
                "object_ref": "table:1",
                "measurements": [{"comparability_key": "dataset-a"}],
            },
            second_id: {
                "id": second_id,
                "work_id": str(second_work),
                "grade": "A_located_structured",
                "text": "Method B was lower.",
                "object_ref": "table:2",
                "measurements": [{"comparability_key": "dataset-b"}],
            },
        },
    )
    assert len(anchors) == 2
    assert all(anchor["comparability_ok"] is False for anchor in anchors)
    assert all(anchor["support_status"] == "not_comparable" for anchor in anchors)


def test_scholarly_profile_blocks_evidence_but_warns_about_submission_metadata() -> None:
    row = _row("Established background context only.", status="draft")
    report = _report(row, build_claim_evidence(rows=[row], evidence_sources={}))
    report.quality_profile = "scholarly"
    project = _project()
    project.authors_json = []
    apply_readiness_gate(
        report,
        rows=[row],
        project=project,
        whitelist={"smith2020"},
        search_runs=[],
    )
    assert report.blockers == []
    assert report.readiness_status == "preflight_ready"
    assert {item["code"] for item in report.warnings} >= {
        "sections_unapproved",
        "authors_missing",
    }


def test_depth_metrics_cover_synthesis_comparability_and_question_answers() -> None:
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    first_work, second_work = uuid.uuid4(), uuid.uuid4()
    row = _row("Method A was higher than Method B.")
    row.cite_keys_json = ["first2024", "second2024"]
    row.body_ir_json["blocks"][0].update(
        {
            "stance_summary": "consistent",
            "runs": [
                {"t": "text", "v": "Method A was higher than Method B."},
                {
                    "t": "cite",
                    "keys": row.cite_keys_json,
                    "evidence_ids": [first_id, second_id],
                },
            ],
        }
    )
    units = {
        first_id: {
            "id": first_id,
            "work_id": str(first_work),
            "grade": "A_located_structured",
            "text": "Method A was higher than Method B.",
            "section_path": "Methods and Results",
            "object_ref": "table:1",
            "measurements": [{"comparability_key": "same", "value": 0.8}],
        },
        second_id: {
            "id": second_id,
            "work_id": str(second_work),
            "grade": "A_located_structured",
            "text": "Method A was higher than Method B.",
            "section_path": "Methods and Results",
            "object_ref": "table:2",
            "measurements": [{"comparability_key": "same", "value": 0.7}],
        },
    }
    anchors = build_claim_evidence(
        rows=[row],
        evidence_sources={
            "first2024": {"work_id": first_work, "fulltext_used": True},
            "second2024": {"work_id": second_work, "fulltext_used": True},
        },
        evidence_units=units,
    )
    report = _report(row, anchors)
    report.core_claim_fulltext_coverage = 1.0
    metrics = build_depth_metrics(
        report=report,
        rows=[row],
        evidence_units=units,
        selected_work_count=2,
        questions=[
            SimpleNamespace(kind="sub", answer_status="answered"),
            SimpleNamespace(kind="sub", answer_status="insufficient_evidence"),
        ],
    )
    assert metrics["2_methods_results_section_coverage"] == 1.0
    assert metrics["3_structured_object_coverage"] == 1.0
    assert metrics["8_invalid_comparison_rate"] == 0.0
    assert metrics["9_cross_study_synthesis_paragraph_rate"] == 1.0
    assert metrics["11_question_answer_completeness"] == 0.5


def test_original_asset_audit_rejects_qualitative_claims_bound_only_to_a_table() -> None:
    asset_id = uuid.uuid4()
    row = _row("The proposed method clearly outperformed the baseline.")
    row.section_key = "s4"
    row.cite_keys_json = []
    row.body_ir_json["blocks"][0]["runs"] = [
        {"t": "text", "v": "The proposed method clearly outperformed the baseline."},
        {"t": "grounding", "source_refs": ["ua_table"]},
    ]
    anchors = build_original_claim_grounding(
        rows=[row],
        assets_by_ref={
            "ua_table": {
                "_asset_id": str(asset_id),
                "type": "table",
                "filename": "results.csv",
                "numeric_cells": {"model::accuracy": "92.5%"},
                "numbers": ["92.5%"],
            }
        },
    )
    assert anchors[0]["source_key"] == "asset:ua_table"
    assert anchors[0]["user_asset_id"] == asset_id
    assert anchors[0]["support_status"] == "asset_content_mismatch"


def test_original_asset_audit_accepts_exact_table_numbers_and_method_content() -> None:
    table_id, note_id = uuid.uuid4(), uuid.uuid4()
    results = _row("The recorded accuracy was 92.5%.")
    results.section_key = "s4"
    results.body_ir_json["blocks"][0]["runs"] = [
        {"t": "text", "v": "The recorded accuracy was 92.5%."},
        {"t": "grounding", "source_refs": ["ua_table"]},
    ]
    method = _row("Samples were normalized before model training.")
    method.section_key = "s2"
    method.body_ir_json["blocks"][0]["runs"] = [
        {"t": "text", "v": "Samples were normalized before model training."},
        {"t": "grounding", "source_refs": ["ua_note"]},
    ]
    anchors = build_original_claim_grounding(
        rows=[method, results],
        assets_by_ref={
            "ua_table": {
                "_asset_id": str(table_id),
                "type": "table",
                "numeric_cells": {"model::accuracy": "92.5%"},
                "numbers": ["92.5%"],
            },
            "ua_note": {
                "_asset_id": str(note_id),
                "type": "note",
                "text": "Samples were normalized before model training and evaluation.",
                "numbers": [],
            },
        },
    )
    assert {anchor["section_key"] for anchor in anchors} == {"s2", "s4"}
    assert all(anchor["support_status"] == "supported" for anchor in anchors)


def test_original_asset_audit_rejects_a_value_attached_to_the_wrong_metric() -> None:
    row = _row("The recorded recall was 92.5%.")
    row.section_key = "s4"
    row.body_ir_json["blocks"][0]["runs"] = [
        {"t": "text", "v": "The recorded recall was 92.5%."},
        {"t": "grounding", "source_refs": ["ua_table"]},
    ]
    anchors = build_original_claim_grounding(
        rows=[row],
        assets_by_ref={
            "ua_table": {
                "_asset_id": str(uuid.uuid4()),
                "type": "table",
                "numeric_cells": {"model::accuracy": "92.5%"},
                "numbers": ["92.5%"],
            }
        },
    )
    assert anchors[0]["support_status"] == "asset_numeric_context_mismatch"
