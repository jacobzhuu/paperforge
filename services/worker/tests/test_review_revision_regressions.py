"""Regressions from the September natural-product review (no live user data)."""

from types import SimpleNamespace

from paperforge_worker.pipelines.citation_decisions import bound_citation_usages, citation_pairs
from paperforge_worker.pipelines.outline import systematic_method_section
from paperforge_worker.pipelines.scholarly_content import accept_math, task_comparison_table
from paperforge_worker.pipelines.scope import systematic_query_plan
from paperforge_worker.pipelines.writing import SectionDraft, section_minimum_words


def test_review_selects_bound_sentence_not_first_formula_or_other_work():
    row = SimpleNamespace(
        section_key="s1",
        body_ir_json={
            "blocks": [
                {
                    "type": "paragraph",
                    "runs": [
                        {"t": "text", "v": "DeepMASS public version 35.9%; SIRIUS 38.4%."},
                        {"t": "cite", "keys": ["mass"], "evidence_ids": ["correct", "wrong"]},
                    ],
                }
            ]
        },
    )
    units = {
        "correct": {"work_id": "w1", "text": "Without NIST: 35.9%; SIRIUS: 38.4%."},
        "wrong": {"work_id": "w2", "text": "Unrelated NMR formula"},
    }
    usages = bound_citation_usages([row], units, {"mass": {"work_id": "w1"}})
    pair = citation_pairs(usages, {"mass": "An unrelated introduction"})[0]
    assert pair["section_key"] == "s1"
    assert "Without NIST" in pair["evidence"]
    assert "NMR" not in pair["evidence"]
    assert "SIRIUS 38.4" in pair["context"]


def test_late_result_is_retrieved_and_verdict_cache_changes_with_binding():
    usage = {"cite_key": "a", "context_snippet": "SpectraNP has 111181 molecules."}
    source = "Introduction " * 500 + "\n\nSpectraNP: 111181 molecules."
    first = citation_pairs([usage], {"a": source})[0]
    assert "111181" in first["evidence"]
    second = citation_pairs(
        [{**usage, "bound_evidence": "SpectraNP revised: 111182"}], {"a": source}
    )[0]
    assert first["pair_hash"] != second["pair_hash"]


def test_lone_operator_is_inline_and_does_not_break_sentence():
    draft = SectionDraft(
        section_key="s1",
        title="Dataset",
        paragraphs=[
            {
                "text": "此前数据集{{eq:small}}101个原子。",
                "sentences": [{"text": "此前数据集{{eq:small}}101个原子。"}],
            }
        ],
    )
    catalog = {
        "small": {"latex": r"\leq", "cite_keys": ["a"], "evidence_ids": ["e"], "source_refs": []}
    }
    accept_math(
        draft,
        {
            "equations": [
                {
                    "source_id": "small",
                    "after_paragraph": 0,
                    "explanation": "An unnecessary explanation",
                }
            ]
        },
        catalog,
    )
    assert not draft.equations
    assert draft.paragraphs[0]["text"] == "此前数据集{{math:small}}101个原子。"
    assert not draft.generation["formula_issues"]


def test_parent_brief_does_not_trigger_length_padding():
    assert (
        section_minimum_words(
            {"has_children": True, "target_words": 200}, language="zh", is_frame=False
        )
        == 60
    )


def test_systematic_search_reuses_budget_and_covers_early_years():
    scope = {
        "time_range": {"start_year": 2020, "end_year": 2026},
        "sub_questions": [{"search_query": f"natural products task {i}"} for i in range(6)],
    }
    queries, filters = systematic_query_plan(scope, ["broad query"] * 12)
    assert len(queries) == 12
    assert queries[:6] == queries[6:]
    assert all(f["time_range"] == {"start_year": 2020, "end_year": 2023} for f in filters[6:])


def test_incomplete_search_is_not_silently_narrative_or_claimed_human_screened():
    section = systematic_method_section(
        {
            "complete": False,
            "retrieved_count": 100,
            "selected_count": 10,
            "databases": ["OpenAlex"],
        },
        language="zh",
    )
    text = section["deterministic_text"]
    assert "系统综述初稿" in text and "含跨检索重复" in text and "不等同于人工" in text


def test_task_matrix_preserves_missing_validation_without_inventing_mechanism():
    bundles = [
        {
            "question": task,
            "evidence": [
                {
                    "cite_key": "paper",
                    "evidence_id": "e",
                    "grade": "B_located_prose",
                    "page": 3,
                    "text": "NPGPT is a language model.",
                    "measurements": [
                        {"dataset": "COCONUT", "metric_name": "validity", "value": 0.9}
                    ],
                }
            ],
        }
        for task in ["Representation", "Generation"]
    ]
    table = task_comparison_table(bundles, language="en")[0]
    assert table["rows"][0][1] == "Not extracted"
    assert table["rows"][0][4] == "Not extracted"
    assert "do not establish absence" in table["caption"]


async def test_invalid_review_retries_with_binding_error_and_cannot_clear_uncertainty(monkeypatch):
    from unittest.mock import AsyncMock

    from paperforge_worker.pipelines import semantic_review as sr

    payload = {
        "answers_question": "full",
        "support": "sufficient",
        "calibration": "matched",
        "synthesis_mode": "synthesized",
        "diagnosis": "none",
        "gap_declared": False,
        "unsupported_claims": [],
        "claim_checks": [
            {
                "claim_id": "c1",
                "status": "supported",
                "evidence_ids": ["foreign"],
                "reason": "matched",
            }
        ],
    }
    corrected = {
        **payload,
        "claim_checks": [
            {"claim_id": "c1", "status": "supported", "evidence_ids": ["e1"], "reason": "matched"}
        ],
    }
    judge = AsyncMock(side_effect=[payload, corrected])
    monkeypatch.setattr(sr, "_judge", judge)
    kwargs = dict(
        section_key="s1",
        question="Q?",
        prose="Claim.",
        evidence=[],
        runner=None,
        repair_invalid=True,
        review_input={
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Claim.",
                    "resolved_evidence_ids": ["e1"],
                    "source_refs": [],
                }
            ]
        },
    )
    verdict = await sr.review_section(**kwargs)
    assert verdict.acceptable
    assert judge.await_count == 2
    assert "foreign_claim_evidence" in judge.call_args.kwargs["user_prompt"]
    uncertain = {
        **payload,
        "claim_checks": [
            {"claim_id": "c1", "status": "uncertain", "evidence_ids": ["e1"], "reason": "ambiguous"}
        ],
    }
    monkeypatch.setattr(sr, "_judge", AsyncMock(side_effect=[uncertain, uncertain]))
    result = await sr.review_section(**kwargs)
    assert not result.acceptable
    assert result.claim_checks[0]["status"] == "uncertain"
