"""M2 写作管线测试：R2 三道防线、大纲不变量、连贯性 pass 的数字红线。

反例优先：模型返回白名单外的 cite key、在连贯性 pass 里塞新数字、
在正文里手写 [1] 标记——每一种都必须被挡下。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from llm_runtime import LLMConfig, LLMRunner
from llm_runtime.types import LLMResponse
from paper_ir import PaperIR, PaperMeta
from paperforge_worker.pipelines.outline import (
    CardBrief,
    deterministic_body_sections,
    generate_outline,
    imrad_body_sections,
)
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    coherence_pass,
    count_words,
    deterministic_paragraphs,
    extract_numbers,
    normalize_paragraphs,
    summarize_paragraphs,
    write_section,
)

WHITELIST = {"lewis2020retrieval", "gao2023survey", "izacard2022atlas"}
CARDS: dict[str, dict[str, Any]] = {
    "lewis2020retrieval": {
        "title": "Retrieval-Augmented Generation",
        "year": 2020,
        "summary": "Introduces RAG combining a retriever with a generator.",
        "contributions": ["RAG architecture"],
    },
    "gao2023survey": {
        "title": "A Survey of RAG",
        "year": 2023,
        "summary": "Surveys retrieval-augmented generation methods.",
    },
    "izacard2022atlas": {"title": "Atlas", "year": 2022, "summary": "Few-shot RAG."},
}
SECTION = {
    "key": "s1",
    "title": "Retrieval-Augmented Generation",
    "summary": "Explain how retrieval grounds generation.",
    "argument_points": ["retrieval reduces hallucination"],
    "cite_keys": sorted(WHITELIST),
}


class _StubProvider:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.requests: list[Any] = []

    def generate(self, request):
        self.requests.append(request)
        payload = self.payloads.pop(0) if self.payloads else ""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(text=text, model="stub-model", provider="stub")


def _runner(payloads: list[Any]) -> tuple[LLMRunner, _StubProvider]:
    provider = _StubProvider(payloads)
    return (
        LLMRunner(
            LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
            provider=provider,
        ),
        provider,
    )


def _context() -> WritingContext:
    return WritingContext(
        outline={"topic": "RAG", "research_question": "How does RAG work?", "sections": [SECTION]},
        language="en",
    )


# ---- R2：写作阶段 ----


async def test_writer_output_within_whitelist_passes_untouched() -> None:
    runner, provider = _runner(
        [
            {
                "paragraphs": [
                    {
                        "text": "Retrieval grounds generation in external evidence.",
                        "cite_keys": ["lewis2020retrieval"],
                    }
                ],
                "terms": [{"term": "RAG", "translation": "retrieval-augmented generation"}],
            }
        ]
    )
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    assert draft.rewrite_count == 0
    assert draft.paragraphs[0]["cite_keys"] == ["lewis2020retrieval"]
    assert draft.citation_warnings == []
    assert draft.terms == {"RAG": "retrieval-augmented generation"}
    # 只调用一次：没有违规就不该触发重写。
    assert len(provider.requests) == 1


async def test_hallucinated_cite_key_triggers_one_rewrite() -> None:
    """R2 第二道：首轮越权 → 带错误信息重写一次；重写合规则无告警。"""
    runner, provider = _runner(
        [
            {
                "paragraphs": [
                    {"text": "RAG works.", "cite_keys": ["smith2024imaginary"]},
                ]
            },
            {
                "paragraphs": [
                    {"text": "RAG works.", "cite_keys": ["lewis2020retrieval"]},
                ]
            },
        ]
    )
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    assert draft.rewrite_count == 1
    assert len(provider.requests) == 2
    assert "smith2024imaginary" in provider.requests[1].user_prompt
    assert draft.paragraphs[0]["cite_keys"] == ["lewis2020retrieval"]
    assert draft.citation_warnings == []


async def test_second_violation_is_stripped_and_warned() -> None:
    """R2 第三道：二次违规 strip 并把告警持久化到 Section IR。"""
    hallucinated = {
        "paragraphs": [
            {"text": "RAG works.", "cite_keys": ["smith2024imaginary", "lewis2020retrieval"]}
        ]
    }
    runner, _provider = _runner([hallucinated, hallucinated])
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    assert draft.rewrite_count == 1
    assert draft.paragraphs[0]["cite_keys"] == ["lewis2020retrieval"]
    assert draft.citation_warnings
    assert "smith2024imaginary" in draft.citation_warnings[0]["rejected_keys"]

    section = draft.to_ir_section()
    assert section.citation_warnings
    # IR 里绝不残留越权 key。
    ir = PaperIR(meta=PaperMeta(title="t"), sections=[section])
    assert ir.collect_cite_keys() <= WHITELIST


async def test_keys_outside_section_assignment_are_rejected() -> None:
    """章节只能引用分配给它的文献——白名单是项目级，分配是章节级。"""
    section = {**SECTION, "cite_keys": ["lewis2020retrieval"]}
    runner, _provider = _runner(
        [
            {"paragraphs": [{"text": "x", "cite_keys": ["gao2023survey"]}]},
            {"paragraphs": [{"text": "x", "cite_keys": ["lewis2020retrieval"]}]},
        ]
    )
    draft = await write_section(
        section=section,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    assert draft.paragraphs[0]["cite_keys"] == ["lewis2020retrieval"]


async def test_inline_citation_markers_are_scrubbed_from_prose() -> None:
    runner, _provider = _runner(
        [
            {
                "paragraphs": [
                    {
                        "text": "RAG improves grounding [1,2] as (Lewis et al., 2020) showed.",
                        "cite_keys": ["lewis2020retrieval"],
                    }
                ]
            }
        ]
    )
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    text = draft.paragraphs[0]["text"]
    assert "[1,2]" not in text
    assert "Lewis et al., 2020" not in text


async def test_llm_failure_falls_back_to_deterministic_section() -> None:
    runner, _provider = _runner(["not json"])
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=runner,
    )
    assert draft.generator == "deterministic_fallback"
    assert draft.paragraphs
    assert set(draft.paragraphs[-1]["cite_keys"]) <= WHITELIST


async def test_without_runner_uses_deterministic_paragraphs() -> None:
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=None,
    )
    assert draft.generator == "deterministic"
    assert len(draft.paragraphs) >= 2


# ---- 连贯性 pass ----


async def test_coherence_pass_rejects_rewrites_that_add_numbers() -> None:
    """数字红线优先于文采：改写引入新数值就整体放弃改写。"""
    draft = SectionDraft(section_key="s1", title="RAG")
    draft.paragraphs = [{"text": "Retrieval grounds generation.", "cite_keys": []}]
    draft.generator = "llm:stub"
    runner, _provider = _runner(
        [{"paragraphs": [{"text": "Retrieval improves accuracy by 12.5%.", "cite_keys": []}]}]
    )
    result = await coherence_pass(
        draft=draft,
        context=_context(),
        whitelist=WHITELIST,
        runner=runner,
    )
    assert result.paragraphs[0]["text"] == "Retrieval grounds generation."


async def test_coherence_pass_applies_clean_rewrite() -> None:
    draft = SectionDraft(section_key="s1", title="RAG")
    draft.paragraphs = [{"text": "Retrieval grounds generation.", "cite_keys": []}]
    runner, _provider = _runner(
        [{"paragraphs": [{"text": "Retrieval anchors generation in evidence.", "cite_keys": []}]}]
    )
    result = await coherence_pass(
        draft=draft,
        context=_context(),
        whitelist=WHITELIST,
        runner=runner,
    )
    assert result.paragraphs[0]["text"] == "Retrieval anchors generation in evidence."


async def test_coherence_pass_cannot_add_cite_keys() -> None:
    draft = SectionDraft(section_key="s1", title="RAG")
    draft.paragraphs = [{"text": "Retrieval grounds generation.", "cite_keys": []}]
    runner, _provider = _runner(
        [
            {
                "paragraphs": [
                    {"text": "Retrieval grounds generation.", "cite_keys": ["gao2023survey"]}
                ]
            }
        ]
    )
    result = await coherence_pass(
        draft=draft,
        context=_context(),
        whitelist=WHITELIST,
        runner=runner,
    )
    # 允许集合是「本节已用的 key」，原本为空，因此新增的 key 被剔除。
    assert result.paragraphs[0]["cite_keys"] == []


# ---- 大纲 ----


async def test_outline_strips_keys_outside_whitelist() -> None:
    runner, _provider = _runner(
        [
            {
                "sections": [
                    {
                        "title": "Foundations",
                        "summary": "s",
                        "argument_points": ["p"],
                        "cite_keys": ["lewis2020retrieval", "fabricated2029key"],
                    }
                ]
            }
        ]
    )
    outcome = await generate_outline(
        topic="RAG",
        research_question="How?",
        cards=[CardBrief(cite_key=key, title=key) for key in WHITELIST],
        whitelist=WHITELIST,
        runner=runner,
    )
    body = [s for s in outcome.tree["sections"] if s["kind"] == "body"]
    assigned = {key for s in body for key in s["cite_keys"]}
    assert "fabricated2029key" not in assigned
    assert assigned <= WHITELIST


async def test_outline_reclaims_orphan_works() -> None:
    """检索来的文献不该白花力气：没分配到的补进最后一个主体章节。"""
    runner, _provider = _runner(
        [
            {
                "sections": [
                    {"title": "Foundations", "cite_keys": ["lewis2020retrieval"]},
                ]
            }
        ]
    )
    outcome = await generate_outline(
        topic="RAG",
        research_question="How?",
        cards=[CardBrief(cite_key=key, title=key) for key in WHITELIST],
        whitelist=WHITELIST,
        runner=runner,
    )
    assigned = {key for s in outcome.tree["sections"] for key in s["cite_keys"]}
    assert assigned == WHITELIST
    assert outcome.orphan_key_count == 0


async def test_outline_includes_frame_sections_in_order() -> None:
    outcome = await generate_outline(
        topic="RAG",
        research_question="How?",
        cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG", year=2020)],
        whitelist=WHITELIST,
        runner=None,
    )
    keys = [s["key"] for s in outcome.tree["sections"]]
    assert keys[0] == "abstract"
    assert keys[1] == "introduction"
    assert keys[-1] == "conclusion"
    assert outcome.generator == "deterministic"


async def test_outline_falls_back_when_llm_output_invalid() -> None:
    runner, _provider = _runner(["<html>"])
    outcome = await generate_outline(
        topic="RAG",
        research_question="How?",
        cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG", year=2020)],
        whitelist=WHITELIST,
        runner=runner,
    )
    assert outcome.generator == "deterministic_fallback"
    assert outcome.section_count >= 3


async def test_original_paper_uses_imrad_template() -> None:
    outcome = await generate_outline(
        topic="A new method",
        research_question="Does it work?",
        cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG")],
        whitelist=WHITELIST,
        paper_type="original",
        runner=None,
    )
    titles = [s["title"] for s in outcome.tree["sections"]]
    assert "Related Work" in titles
    assert "Method" in titles
    assert outcome.generator == "imrad_template"


def test_imrad_sections_only_ground_related_work_in_library() -> None:
    sections = imrad_body_sections(["lewis2020retrieval"], language="en")
    related = sections[0]
    method = sections[1]
    assert related["cite_keys"] == ["lewis2020retrieval"]
    # Method/Experiments 由用户素材接地，不该默认带文献（M4 数字红线的前提）。
    assert method["cite_keys"] == []
    assert method["grounding"] == "user_asset"


def test_deterministic_body_sections_cover_every_card() -> None:
    cards = [
        CardBrief(cite_key="a", title="A", year=2016),
        CardBrief(cite_key="b", title="B", year=2024),
    ]
    sections = deterministic_body_sections(cards, language="en")
    assigned = {key for s in sections for key in s["cite_keys"]}
    assert assigned == {"a", "b"}


async def test_review_outline_adds_comparison_limitations_and_conflict_synthesis() -> None:
    cards = [
        CardBrief(
            cite_key="a",
            title="Study A",
            year=2018,
            methods=("field experiment",),
            fulltext_used=True,
        ),
        CardBrief(cite_key="b", title="Study B", year=2024, methods=("simulation",)),
    ]
    outcome = await generate_outline(
        topic="Evidence synthesis",
        research_question="What agrees?",
        cards=cards,
        whitelist={"a", "b"},
        runner=None,
    )

    synthesis = next(
        section
        for section in outcome.tree["sections"]
        if section.get("synthesis_kind") == "comparison_limitations_conflicts"
    )
    assert synthesis["cite_keys"] == ["a", "b"]
    assert synthesis["inline_tables"][0]["rows"][0][-1] == "Located full text"


async def test_review_synthesis_keeps_complete_titles_and_removes_source_markup() -> None:
    title = (
        "AI-assisted isolation of bioactive Dipyrimicins from "
        "<i>Amycolatopsis azurea</i> and identification of their complete activity profile"
    )
    method = "A deliberately detailed method description " * 5
    outcome = await generate_outline(
        topic="Evidence synthesis",
        research_question="What agrees?",
        cards=[
            CardBrief(
                cite_key="a",
                title=title,
                year=2025,
                methods=(method,),
                fulltext_used=True,
            ),
            CardBrief(cite_key="b", title="Comparison study", year=2024),
        ],
        whitelist={"a", "b"},
        runner=None,
    )

    synthesis = next(
        section
        for section in outcome.tree["sections"]
        if section.get("synthesis_kind") == "comparison_limitations_conflicts"
    )
    row = synthesis["inline_tables"][0]["rows"][0]
    assert row[0] == title.replace("<i>", "").replace("</i>", "")
    assert row[1] == "2025"
    assert row[2] == method.strip()


async def test_review_synthesis_table_becomes_inline_table_ir() -> None:
    section = {
        "key": "synthesis",
        "title": "Cross-study synthesis",
        "cite_keys": [],
        "inline_tables": [
            {
                "caption": "Evidence matrix",
                "label": "tab:evidence",
                "headers": ["Study", "Evidence"],
                "rows": [["A", "Full text"]],
            }
        ],
    }
    draft = await write_section(
        section=section,
        cards={},
        whitelist=set(),
        context=WritingContext(outline={"sections": [section]}),
        runner=None,
    )
    table = draft.to_ir_section().blocks[-1]
    assert table.type == "table"
    assert table.source.kind == "inline"
    assert table.source.data == {"headers": ["Study", "Evidence"], "rows": [["A", "Full text"]]}


# ---- 工具函数 ----


def test_normalize_paragraphs_filters_and_dedupes_keys() -> None:
    paragraphs = normalize_paragraphs(
        [
            {"text": " spaced  text ", "cite_keys": ["gao2023survey", "gao2023survey", "nope"]},
            {"text": "", "cite_keys": []},
            "plain string paragraph",
        ],
        allowed={"gao2023survey"},
    )
    assert paragraphs[0]["text"] == "spaced text"
    assert paragraphs[0]["cite_keys"] == ["gao2023survey"]
    assert len(paragraphs) == 2


def test_sentence_level_citations_are_interleaved_in_ir() -> None:
    paragraphs = normalize_paragraphs(
        [
            {
                "sentences": [
                    {"text": "Background context is established.", "cite_keys": []},
                    {
                        "text": "Retrieval improves grounding.",
                        "cite_keys": ["lewis2020retrieval"],
                    },
                ]
            }
        ],
        allowed={"lewis2020retrieval"},
    )
    draft = SectionDraft(section_key="s1", title="Evidence", paragraphs=paragraphs)
    runs = draft.to_ir_section().blocks[0].runs
    assert [run.t for run in runs] == ["text", "text", "text", "cite"]
    assert runs[-1].keys == ["lewis2020retrieval"]
    assert paragraphs[0]["cite_keys"] == ["lewis2020retrieval"]


def test_count_words_handles_chinese_and_english() -> None:
    assert count_words("检索增强生成") == 6
    assert count_words("retrieval augmented generation") == 3


def test_extract_numbers_finds_percentages() -> None:
    assert extract_numbers("accuracy rose to 92.5% from 80%") == {"92.5%", "80%"}


def test_summarize_paragraphs_takes_first_sentences() -> None:
    summary = summarize_paragraphs(
        [{"text": "First claim. Supporting detail."}, {"text": "Second claim. More."}]
    )
    assert "First claim." in summary
    assert "Second claim." in summary
    assert "Supporting detail" not in summary


def test_deterministic_paragraphs_never_invent_content() -> None:
    paragraphs = deterministic_paragraphs(SECTION, CARDS, {"lewis2020retrieval"})
    texts = " ".join(p["text"] for p in paragraphs)
    assert "Introduces RAG combining a retriever with a generator." in texts
    assert "gao2023survey" not in texts


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_section_prompt_language_switches(language: str) -> None:
    runner, provider = _runner([{"paragraphs": [{"text": "x", "cite_keys": []}]}])
    context = WritingContext(outline={"sections": [SECTION]}, language=language)
    await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=context,
        runner=runner,
    )
    assert ("只输出 JSON" in provider.requests[0].system_prompt) is (language == "zh")
