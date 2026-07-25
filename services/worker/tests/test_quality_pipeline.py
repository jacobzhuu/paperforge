"""M5 测试：语义软校验、覆盖建议、质量评分。

三者都必须是**提示**而非门槛：低分引用不会被删除，覆盖不足不会阻断交付
（设计 §3.4：formal completion 的 12 项硬门槛 → 质量评分报告，仅提示）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from llm_runtime import LLMConfig, LLMRunner
from llm_runtime.types import LLMResponse
from paperforge_worker.pipelines.quality import (
    SOFT_CHECK_THRESHOLD,
    SoftCheckFinding,
    build_quality_report,
    count_words,
    coverage_hints,
    soft_check_citations,
)

NOW = datetime(2026, 7, 25, tzinfo=UTC)


class _Stub:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def generate(self, request):
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return LLMResponse(text=text, model="stub", provider="stub")


def _runner(payload: Any) -> LLMRunner:
    return LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
        provider=_Stub(payload),
    )


def _sections(**overrides: Any) -> list[dict[str, Any]]:
    base = [
        {
            "section_key": "s1",
            "title": "技术基础",
            "word_count": 1200,
            "cite_keys": ["a2020x", "b2021y"],
            "kind": "body",
        },
        {
            "section_key": "s2",
            "title": "评测方法",
            "word_count": 900,
            "cite_keys": [],
            "kind": "body",
        },
        {
            "section_key": "abstract",
            "title": "摘要",
            "word_count": 200,
            "cite_keys": [],
            "kind": "frame",
        },
    ]
    base[0].update(overrides)
    return base


# ---- 语义软校验 ----


async def test_soft_check_flags_weak_citations_without_removing_them() -> None:
    findings = await soft_check_citations(
        usages=[
            {"cite_key": "a2020x", "section_key": "s1", "context_snippet": "关于检索器的论述"},
            {"cite_key": "b2021y", "section_key": "s1", "context_snippet": "关于评测的论述"},
        ],
        abstracts={"a2020x": "本文提出稠密检索器。", "b2021y": "本文研究蛋白质折叠。"},
        runner=_runner(
            {
                "judgements": [
                    {"index": 0, "score": 0.92, "reason": "主题一致"},
                    {"index": 1, "score": 0.12, "reason": "主题不相关"},
                ]
            }
        ),
    )
    assert len(findings) == 2
    weak = [f for f in findings if f.weak]
    assert len(weak) == 1
    assert weak[0].cite_key == "b2021y"
    # 只出提示：软校验不返回任何「删除」指令。
    assert all(hasattr(f, "score") for f in findings)


async def test_soft_check_ignores_out_of_range_indexes() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a2020x", "section_key": "s1", "context_snippet": "ctx"}],
        abstracts={"a2020x": "abs"},
        runner=_runner({"judgements": [{"index": 99, "score": 0.1}]}),
    )
    assert findings == []


async def test_soft_check_without_runner_is_empty_not_failing() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a", "section_key": "s", "context_snippet": "c"}],
        abstracts={"a": "abs"},
        runner=None,
    )
    assert findings == []


async def test_soft_check_survives_unparsable_output() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a", "section_key": "s", "context_snippet": "c"}],
        abstracts={"a": "abs"},
        runner=_runner("<html>not json</html>"),
    )
    assert findings == []


# ---- 覆盖建议 ----


def test_section_without_citations_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2024, 2025],
        now=NOW,
    )
    kinds = {hint["kind"] for hint in hints}
    assert "no_citations" in kinds
    assert any(hint.get("section_key") == "s2" for hint in hints)


def test_unused_library_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=10,
        used_keys={"a2020x"},
        publication_years=[2025],
        now=NOW,
    )
    assert any(hint["kind"] == "unused_library" for hint in hints)


def test_stale_library_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2001, 2003, 2005, 2007],
        now=NOW,
    )
    assert any(hint["kind"] == "stale_library" for hint in hints)


def test_uncovered_subtopic_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2025],
        scope={"topic": "检索增强生成", "subtopics": ["多模态检索"]},
        now=NOW,
    )
    assert any(hint["kind"] == "uncovered_subtopic" for hint in hints)


def test_subtopics_that_are_just_topic_words_do_not_spam_hints() -> None:
    """确定性回退的 subtopics 就是主题切词——逐词提示只会刷屏。"""
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2025],
        scope={
            "topic": "retrieval augmented generation for science",
            "subtopics": ["retrieval", "augmented", "generation"],
        },
        now=NOW,
    )
    assert not [hint for hint in hints if hint["kind"] == "uncovered_subtopic"]


def test_frame_sections_are_not_hinted_for_missing_citations() -> None:
    hints = coverage_hints(
        sections=[
            {
                "section_key": "abstract",
                "title": "摘要",
                "word_count": 1000,
                "cite_keys": [],
                "kind": "frame",
            }
        ],
        whitelist_size=1,
        used_keys={"a"},
        publication_years=[2025],
        now=NOW,
    )
    # 摘要/引言/结论不带引用是正常的，不该刷提示。
    assert not [hint for hint in hints if hint["kind"] == "no_citations"]


# ---- 质量评分 ----


def test_quality_report_computes_density_and_coverage() -> None:
    report = build_quality_report(
        sections=_sections(),
        whitelist_size=4,
        publication_years=[2024, 2025, 2019, 2018],
        fulltext_coverage=0.5,
        soft_check=[
            SoftCheckFinding(cite_key="b2021y", section_key="s1", score=0.2, reason="不相关")
        ],
        now=NOW,
    )
    payload = report.to_payload()
    assert payload["word_count"] == 2300
    assert payload["unique_cite_count"] == 2
    assert payload["library_coverage"] == 0.5
    assert payload["citation_density"] > 0
    assert payload["fulltext_coverage"] == 0.5
    assert payload["sections_without_citations"] == ["s2"]
    # 只有弱引用进报告，强引用不刷屏。
    assert len(payload["soft_check"]) == 1
    assert payload["soft_check"][0]["score"] < SOFT_CHECK_THRESHOLD


def test_quality_report_never_blocks_on_empty_document() -> None:
    report = build_quality_report(
        sections=[],
        whitelist_size=0,
        publication_years=[],
        now=NOW,
    )
    payload = report.to_payload()
    # 空文稿也返回结构化报告，而不是抛错或拒绝（draft-first）。
    assert payload["section_count"] == 0
    assert payload["citation_density"] == 0.0


def test_count_words_matches_writing_pipeline() -> None:
    assert count_words("检索增强生成") == 6
    assert count_words("retrieval augmented generation") == 3
