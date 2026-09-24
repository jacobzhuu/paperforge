"""降级出来的章节必须看得见，并且能被质量修复捡走。

回归自项目 6a6bbf18（2026-08-14）：writer 反复 `output_truncated`，11 节里 5 节
走了降级路径。质量门当时**确实**发现了异常（language_mismatch 精确点名 s3/s4/s6），
但 draft 档把一切阻断项降级成提示，稿子照常交付，而 language_mismatch 只是碰巧
撞上——原文是英文。同语言的照抄当时没有任何检查会响。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from paperforge_worker.pipelines.quality import (
    ALWAYS_BLOCKER_CODES,
    RECOVERABLE_BLOCKER_CODES,
    SCHOLARLY_BLOCKER_CODES,
    apply_readiness_gate,
    build_quality_report,
    recoverable_findings,
    repairable_finding_count,
    verbatim_evidence_copies,
)

EVIDENCE_TEXT = (
    "Given that P. polymyxa could favorably alter the soil microbiome, we investigated "
    "whether the AIP quorum sensing autoinducers could shape the natural rhizosphere "
    "microbiome of Arabidopsis thaliana grown in a tropical forest soil."
)


def _row(
    *,
    section_key: str,
    status: str,
    text: str = "占位",
    evidence_ids: list[str] | None = None,
) -> SimpleNamespace:
    runs: list[dict[str, Any]] = [{"t": "text", "v": text}]
    if evidence_ids:
        runs.append({"t": "cite", "keys": ["chen2025"], "evidence_ids": evidence_ids})
    return SimpleNamespace(
        id=uuid.uuid4(),
        section_key=section_key,
        title=section_key,
        status=status,
        cite_keys_json=["chen2025"] if evidence_ids else [],
        body_ir_json={"blocks": [{"type": "paragraph", "runs": runs}]},
    )


def _project() -> SimpleNamespace:
    return SimpleNamespace(
        publication_title="一篇综述",
        authors_json=["Ada Lovelace"],
        keywords_json=["evidence"],
        metadata_confirmed_at=None,
        language="zh",
        paper_type="review",
    )


def _gate(rows: list[SimpleNamespace], *, profile: str, evidence_units=None):
    report = build_quality_report(
        sections=[
            {
                "section_key": row.section_key,
                "title": row.title,
                "word_count": 1200,
                "cite_keys": row.cite_keys_json,
                "kind": "body",
            }
            for row in rows
        ],
        whitelist_size=1,
        publication_years=[2024],
        fulltext_coverage=1.0,
    )
    report.quality_profile = profile
    report.review_style = "narrative"
    report.claim_evidence = []
    report.layout_checks = {"passed": None, "status": "not_run"}
    apply_readiness_gate(
        report,
        rows=rows,
        project=_project(),
        whitelist={"chen2025"},
        search_runs=[],
        evidence_units=evidence_units,
    )
    return report


def test_a_section_with_no_body_blocks_even_in_draft() -> None:
    """draft 档「不设门槛」的前提是确实有一份稿子；空洞不是粗糙。"""
    rows = [
        _row(section_key="s3", status="needs_rewrite"),
        _row(section_key="s5", status="generated"),
    ]
    report = _gate(rows, profile="draft")

    blocker = next(item for item in report.blockers if item["code"] == "section_not_generated")
    assert blocker["section_keys"] == ["s3"]
    assert "section_not_generated" in ALWAYS_BLOCKER_CODES
    # 其余问题码在 draft 档仍然降级成提示，这一条不能顺手改掉。
    assert all(item["code"] == "section_not_generated" for item in report.blockers)


def test_a_fully_generated_draft_still_reports_no_blockers() -> None:
    report = _gate([_row(section_key="s5", status="generated")], profile="draft")
    assert report.blockers == []


def test_scholarly_profile_also_blocks_and_names_the_sections() -> None:
    rows = [_row(section_key="s6", status="needs_rewrite")]
    report = _gate(rows, profile="scholarly")

    codes = {item["code"] for item in report.blockers}
    assert "section_not_generated" in codes
    assert "section_not_generated" in SCHOLARLY_BLOCKER_CODES


def test_a_missing_section_counts_as_repairable_in_draft() -> None:
    """概览页的修复卡读的是这个计数；读不到就等于「0 处可修复」而正文有洞。"""
    rows = [_row(section_key="s3", status="needs_rewrite")]
    assert repairable_finding_count(_gate(rows, profile="draft")) >= 1


def test_draft_findings_are_offered_to_the_repair_loop_not_just_reported() -> None:
    """草稿档也要先自动修一轮。

    检测到问题却只写进报告，等于把一份自己知道有洞的稿子交出去。修复轮的触发条件
    读的是 ``recoverable_findings``，它必须同时看阻断项和被降级的提示——draft 档
    把大部分码降级成了提示，只读 blockers 会让这一档永远等不到修复。
    """
    rows = [
        _row(section_key="s3", status="needs_rewrite"),
        _row(
            section_key="s4",
            status="generated",
            text=EVIDENCE_TEXT,
            evidence_ids=["e-1"],
        ),
    ]
    report = _gate(rows, profile="draft", evidence_units={"e-1": {"text": EVIDENCE_TEXT}})

    codes = {str(item.get("code")) for item in recoverable_findings(report)}
    assert "section_not_generated" in codes, "阻断项要被修复轮看到"
    assert "verbatim_evidence_copy" in codes, "被降级成提示的码同样要被看到"
    assert codes <= RECOVERABLE_BLOCKER_CODES

    # 修复轮据此挑章节：两条发现项各自点名的章节都要进去。
    targets: set[str] = set()
    for finding in recoverable_findings(report):
        targets.update(str(key) for key in finding.get("section_keys") or [])
        targets.update(str(key) for key in finding.get("sections") or [])
    assert targets == {"s3", "s4"}


def test_a_clean_draft_asks_for_no_repair_round() -> None:
    """没有可恢复缺陷时不能凭空触发修复——那是白花两轮改写的钱。"""
    report = _gate([_row(section_key="s5", status="generated")], profile="draft")
    assert recoverable_findings(report) == []


def test_verbatim_evidence_copy_is_caught_regardless_of_language() -> None:
    units = {"e-1": {"text": EVIDENCE_TEXT}}
    copied = _row(
        section_key="s4",
        status="generated",
        text=EVIDENCE_TEXT,
        evidence_ids=["e-1"],
    )
    findings = verbatim_evidence_copies([copied], units)
    assert [item["section_key"] for item in findings] == ["s4"]
    assert findings[0]["overlap"] >= 0.9


def test_a_paragraph_spliced_from_several_evidence_units_is_still_caught() -> None:
    """整段照抄要按**段落**比对该段绑定证据的并集。

    实际事故里 s3 的照抄段落是几条证据拼起来的，cite 散落在段落中间：逐句比、
    或者逐条证据比，都会因为「每一条都不过阈值」而全部放过。
    """
    second = (
        "Calcium dependent protein kinases participate in stress responses, development, "
        "and hormone signaling across Arabidopsis and Nicotiana tabacum."
    )
    units = {"e-1": {"text": EVIDENCE_TEXT}, "e-2": {"text": second}}
    spliced = SimpleNamespace(
        id=uuid.uuid4(),
        section_key="s3",
        title="s3",
        status="generated",
        cite_keys_json=["chen2025"],
        body_ir_json={
            "blocks": [
                {
                    "type": "paragraph",
                    "runs": [
                        {"t": "text", "v": EVIDENCE_TEXT},
                        {"t": "cite", "keys": ["chen2025"], "evidence_ids": ["e-1"]},
                        {"t": "text", "v": second},
                        {"t": "cite", "keys": ["chen2025"], "evidence_ids": ["e-2"]},
                    ],
                }
            ]
        },
    )
    findings = verbatim_evidence_copies([spliced], units)
    assert len(findings) == 1
    assert findings[0]["evidence_ids"] == ["e-1", "e-2"]
    assert findings[0]["overlap"] >= 0.9


def test_a_written_claim_that_merely_cites_the_same_evidence_is_not_flagged() -> None:
    units = {"e-1": {"text": EVIDENCE_TEXT}}
    written = _row(
        section_key="s4",
        status="generated",
        text=(
            "群体感应自诱导肽能够重塑根际微生物组的组成，说明信号分子的作用范围"
            "并不局限于产生菌自身，而是延伸到周边群落的装配过程。"
        ),
        evidence_ids=["e-1"],
    )
    assert verbatim_evidence_copies([written], units) == []


def test_the_gate_reports_verbatim_copies_as_a_finding() -> None:
    units = {"e-1": {"text": EVIDENCE_TEXT}}
    rows = [
        _row(
            section_key="s4",
            status="generated",
            text=EVIDENCE_TEXT,
            evidence_ids=["e-1"],
        )
    ]
    report = _gate(rows, profile="scholarly", evidence_units=units)
    blocker = next(item for item in report.blockers if item["code"] == "verbatim_evidence_copy")
    assert blocker["sections"] == ["s4"]
