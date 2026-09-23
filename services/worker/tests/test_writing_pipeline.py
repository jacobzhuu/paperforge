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
    FRAME_SECTION_BRIEFS,
    CardBrief,
    _section_heading,
    deterministic_body_sections,
    generate_outline,
    imrad_body_sections,
    question_driven_sections,
    review_synthesis_section,
)
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    coherence_pass,
    count_words,
    deterministic_paragraphs,
    enforce_sentence_evidence_rules,
    enforce_sentence_grounding_rules,
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


def test_evidence_downgrade_keeps_audit_reason_out_of_body_prose() -> None:
    paragraphs = [
        {
            "sentences": [
                {
                    "text": "The method improves all recommender systems by 30%.",
                    "cite_keys": ["lewis2020retrieval"],
                    "evidence_ids": [],
                }
            ]
        }
    ]
    result = enforce_sentence_evidence_rules(paragraphs, evidence_by_id={}, language="zh")
    assert result[0]["sentences"] == []
    assert result[0]["downgraded_sentences"][0]["downgraded_reason"] == "R6_locator_missing"
    assert result[0]["text"] == ""


def test_a_non_comparable_comparison_is_dropped_not_replaced_with_boilerplate() -> None:
    """R5 也不许把判定说明写进正文。

    此前不可比的比较句会被替换成「这些证据采用不同的任务、数据集、指标或划分，
    结果应分别陈述。」——那是写给系统看的判词，出现在成稿里就是一行模板。实测它
    出现在项目 6a6bbf18 的 s4 正文中段，夹在两段正常论述之间。
    """
    paragraphs = [
        {
            "sentences": [
                {
                    "text": "方法 A 在准确率上优于方法 B。",
                    "cite_keys": ["lewis2020retrieval"],
                    "evidence_ids": ["e-1", "e-2"],
                }
            ]
        }
    ]
    evidence_by_id = {
        "e-1": {
            "grade": "A_located_structured",
            "page": 3,
            "measurements": [{"comparability_key": "task=qa|dataset=nq"}],
        },
        "e-2": {
            "grade": "A_located_structured",
            "page": 7,
            "measurements": [{"comparability_key": "task=summarisation|dataset=cnn"}],
        },
    }
    result = enforce_sentence_evidence_rules(
        paragraphs,
        evidence_by_id=evidence_by_id,
        language="zh",
    )

    body = " ".join(paragraph.get("text", "") for paragraph in result)
    assert "结果应分别陈述" not in body
    assert "不同的任务、数据集" not in body
    # 整段被删空时会被移出返回值，审计记录留在原段落对象上（原地改写）。
    downgraded = [
        sentence
        for paragraph in paragraphs
        for sentence in paragraph.get("downgraded_sentences") or []
    ]
    assert [item["downgraded_reason"] for item in downgraded] == ["R5_not_comparable"]
    # 判定理由必须留在审计记录里——删掉正文不等于删掉证据。
    assert downgraded[0]["text"] == ""


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


async def test_write_failure_never_pastes_evidence_text_into_the_body() -> None:
    """降级不得把证据原文当正文交出去。

    回归自项目 6a6bbf18（2026-08-14）：writer 两次都 `output_truncated`，降级路径
    把大纲提示语和一整段英文 PDF 原文（连原文献自己的 (Zipfel, 2014) 标注一起）
    写成了中文综述的正文段落。这是逐字复制他人正文，不只是质量差。
    """
    verbatim = (
        "Given that P. polymyxa could favorably alter the soil microbiome ( 34 ), "
        "we investigated whether the AIP QS autoinducers could shape the rhizosphere "
        "microbiome as reported by Zipfel, 2014."
    )
    section = {
        **SECTION,
        "question_id": "q-1",
        "summary": "回答该子问题；当前证据状态：一致。",
    }
    outline = {
        "sections": [section],
        "sub_question_bundles": [
            {
                "question_id": "q-1",
                "evidence": [
                    {
                        "evidence_id": "e-1",
                        "cite_key": "lewis2020retrieval",
                        "text": verbatim,
                        "grade": "B_located_prose",
                    }
                ],
            }
        ],
    }
    runner, _provider = _runner(["not json"])
    draft = await write_section(
        section=section,
        cards=CARDS,
        whitelist=WHITELIST,
        context=WritingContext(outline=outline, language="zh"),
        runner=runner,
    )

    assert draft.generator == "deterministic_fallback"
    body = " ".join(paragraph["text"] for paragraph in draft.paragraphs)
    assert verbatim not in body
    assert "P. polymyxa" not in body
    assert "Zipfel" not in body
    # 大纲 summary 是写给模型的指令，同样不是正文。
    assert section["summary"] not in body
    assert draft.paragraphs[0]["needs_rewrite"] is True


async def test_without_runner_uses_deterministic_paragraphs() -> None:
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=WHITELIST,
        context=_context(),
        runner=None,
    )
    assert draft.generator == "deterministic"
    # 没有写作模型就没有正文——留一句「待重写」，而不是拿大纲提示语和素材凑一段。
    assert len(draft.paragraphs) == 1
    assert draft.paragraphs[0]["needs_rewrite"] is True
    assert SECTION["summary"] not in draft.paragraphs[0]["text"]


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


async def test_question_driven_outline_uses_subquestions_not_publication_years() -> None:
    bundles = [
        {
            "question_id": "q-1",
            "question": "Under which datasets does the effect hold?",
            "answer_status": "answered",
            "stance_summary": "consistent",
            "evidence": [
                {"evidence_id": "e-1", "cite_key": "a"},
                {"evidence_id": "e-2", "cite_key": "b"},
            ],
            "comparison_clusters": [
                {
                    "comparability_key": "same-key",
                    "classification": "consistent",
                    "evidence_ids": ["e-1", "e-2"],
                }
            ],
            "not_comparable_groups": [],
            "evidence_gap": None,
        }
    ]
    outcome = await generate_outline(
        topic="Evidence synthesis",
        research_question="What agrees?",
        cards=[
            CardBrief(cite_key="a", title="Old study", year=1999),
            CardBrief(cite_key="b", title="New study", year=2026),
        ],
        whitelist={"a", "b"},
        runner=None,
        sub_question_bundles=bundles,
    )
    body = [section for section in outcome.tree["sections"] if section.get("question_id") == "q-1"][
        0
    ]
    # 小节由子问题驱动这一点没变；变的是**标题不再是问题原文**——那会把问号和举例
    # 括号印到 PDF 的目录里。问题原文改挂在 question / summary 上。
    assert body["question"] == bundles[0]["question"]
    assert bundles[0]["question"] in body["summary"]
    assert body["title"] == _section_heading(bundles[0]["question"], language="en")
    assert body["stance_summary"] == "consistent"
    assert body["evidence_ids"] == ["e-1", "e-2"]
    assert outcome.generator == "question_evidence_matrix"
    assert all("Earlier" not in section["title"] for section in outcome.tree["sections"])


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
    assert synthesis["cite_keys"] == []
    # Prose, not a bool: a bool reached the writer prompt as the literal
    # line "[EVIDENCE GAP] True".
    assert isinstance(synthesis["evidence_gap"], str)
    assert synthesis["evidence_gap"]
    assert synthesis["inline_tables"] == []


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
    assert synthesis["inline_tables"] == []
    assert synthesis["cite_keys"] == []


def _synthesis_bundles(*, metric: bool) -> list[dict]:
    measurements = [{"task": "5-way 1-shot", "dataset": "miniImageNet",
                     "metric_name": "acc", "value": 62.1, "unit": "%"}] if metric else []
    return [
        {
            "evidence": [
                {
                    "evidence_id": f"e{index}",
                    "cite_key": "a",
                    "title": (
                        "Multi-stage fusion of local and global features "
                        "for few-shot image classification"
                    ),
                    "year": 2025,
                    "grade": "B_located_prose",
                    "measurements": measurements,
                    "section_path": "Related work",
                    "paragraph_index": 1,
                }
                for index in range(3)
            ]
        }
    ]


def test_literature_matrix_drops_columns_no_study_reported() -> None:
    """整列都是「未报告」的列只会和有内容的列平分版面宽度。

    实测：16 行 × 6 列的矩阵排了 5 页，其中「任务/数据集」与「关键指标与数值」
    两列每一行都是占位符。省略它们是排版决定，但必须**写进 caption**——
    静默删列会让读者以为这些维度从没被考察过。
    """
    cards = [
        CardBrief(
            cite_key="a", title="Study A", year=2025, methods=("contrastive pre-training",)
        )
    ]
    section = review_synthesis_section(
        cards, language="zh", sub_question_bundles=_synthesis_bundles(metric=False)
    )
    table = section["inline_tables"][0]
    assert "任务/数据集" not in table["headers"]
    assert "关键指标与数值" not in table["headers"]
    assert table["headers"][0] == "研究"
    assert all(len(row) == len(table["headers"]) for row in table["rows"])
    assert "已略去相应列" in table["caption"]
    assert "任务/数据集" in table["caption"]


def test_literature_matrix_keeps_columns_that_carry_data() -> None:
    cards = [
        CardBrief(
            cite_key="a", title="Study A", year=2025, methods=("contrastive pre-training",)
        )
    ]
    section = review_synthesis_section(
        cards, language="zh", sub_question_bundles=_synthesis_bundles(metric=True)
    )
    table = section["inline_tables"][0]
    assert table["headers"] == ["研究", "任务/数据集", "方法", "关键指标与数值", "证据等级", "定位"]
    assert "已略去相应列" not in table["caption"]


def test_literature_matrix_clips_long_cells_at_a_word_boundary() -> None:
    """窄 ``p{}`` 列里一段 200 字的方法描述会把整行撑成十几行高。

    硬切会留下半个单词（真实产物里出现过 ``unseen ta``），所以退到词边界并补省略号。
    """
    method = (
        "Supervised <i>contrastive</i> pre-training of the encoder followed by a "
        "nearest centroid classifier trained on the few-shot split"
    )
    cards = [CardBrief(cite_key="a", title="Study A", year=2025, methods=(method,))]
    section = review_synthesis_section(
        cards, language="en", sub_question_bundles=_synthesis_bundles(metric=True)
    )
    row = section["inline_tables"][0]["rows"][0]
    study, _task, rendered_method = row[0], row[1], row[2]
    # 截断长度按去标记后的可见文本算，标记本身不进 PDF。
    assert "<i>" not in rendered_method
    plain = method.replace("<i>", "").replace("</i>", "")
    assert rendered_method.endswith("…")
    assert len(rendered_method) <= 91
    head = rendered_method[:-1]
    assert plain.startswith(head)
    # 词边界：截断点后面一个字符必须是空格，否则就切在了单词中间。
    assert plain[len(head)] == " "
    assert study.endswith("(2025)")
    assert len(study) <= 48


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


async def test_evidence_ledger_is_appendix_only_and_deduplicates_units() -> None:
    bundles = [
        {
            "evidence": [
                {
                    "evidence_id": "e-1",
                    "cite_key": "a",
                    "title": "Study A",
                    "year": 2024,
                    "text": "Located result.",
                    "task_id": "bgc.identification",
                    "grade": "B_located_prose",
                    "locator_display": "p.2",
                },
                {"evidence_id": "e-1", "cite_key": "a", "text": "duplicate"},
            ]
        }
    ]
    outcome = await generate_outline(
        topic="BGC",
        research_question="What works?",
        cards=[CardBrief(cite_key="a", title="Study A"), CardBrief(cite_key="b", title="Study B")],
        whitelist={"a", "b"},
        runner=None,
        sub_question_bundles=bundles,
    )
    ledger = next(
        section
        for section in outcome.tree["sections"]
        if section.get("synthesis_kind") == "evidence_ledger"
    )
    assert ledger["appendix"] is True
    assert ledger["cite_keys"] == ["a"]
    assert len(ledger["inline_tables"][0]["rows"]) == 1


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


def test_original_sentence_grounding_is_interleaved_in_ir() -> None:
    paragraphs = normalize_paragraphs(
        [
            {
                "sentences": [
                    {
                        "text": "The recorded accuracy was 92.5%.",
                        "cite_keys": [],
                        "source_refs": ["ua_12345678"],
                    }
                ]
            }
        ],
        allowed=set(),
        allowed_source_refs={"ua_12345678"},
    )
    draft = SectionDraft(section_key="s4", title="Results", paragraphs=paragraphs)
    runs = draft.to_ir_section().blocks[0].runs
    assert [run.t for run in runs] == ["text", "grounding"]
    assert runs[-1].source_refs == ["ua_12345678"]


def _no_prose_survived(paragraphs) -> bool:
    """规则拿掉每一句之后，正文里什么都不剩。

    从前这些用例断言返回的段落列表为空。现在被删的句子会留在
    ``downgraded_sentences`` 里供审计（Step 2：删除事件要能追踪），所以段落对象
    本身还在——但它一个字的正文都没有，``to_ir_section()`` 的 ``if not runs:
    continue`` 也不会为它生成任何 IR 块。要断言的是后者。
    """
    return all(
        not str(p.get("text") or "").strip()
        and not [s for s in p.get("sentences") or [] if str(s.get("text") or "").strip()]
        for p in paragraphs
    )


def test_original_result_grounding_requires_a_value_from_the_bound_table() -> None:
    assets = {
        "ua_12345678": {
            "type": "table",
            "numeric_cells": {"model::accuracy": "92.5%"},
            "numbers": ["92.5%"],
        }
    }
    qualitative = enforce_sentence_grounding_rules(
        [
            {
                "sentences": [
                    {
                        "text": "The proposed method clearly outperformed the baseline.",
                        "source_refs": ["ua_12345678"],
                    }
                ]
            }
        ],
        assets_by_ref=assets,
        require_grounding=True,
        require_numeric=True,
    )
    assert _no_prose_survived(qualitative), "数值型断言必须有表格里的值支撑"
    assert [
        s.get("downgraded_reason")
        for p in qualitative
        for s in p.get("downgraded_sentences") or []
    ], "删除要留痕"

    mislabeled = enforce_sentence_grounding_rules(
        [
            {
                "sentences": [
                    {
                        "text": "The recorded recall was 92.5%.",
                        "source_refs": ["ua_12345678"],
                    }
                ]
            }
        ],
        assets_by_ref=assets,
        require_grounding=True,
        require_numeric=True,
    )
    assert _no_prose_survived(mislabeled), "标错来源的数字同样留不下"
    assert [
        s.get("downgraded_reason")
        for p in mislabeled
        for s in p.get("downgraded_sentences") or []
    ], "删除要留痕"

    grounded = enforce_sentence_grounding_rules(
        [
            {
                "sentences": [
                    {
                        "text": "The recorded accuracy was 92.5%.",
                        "source_refs": ["ua_12345678"],
                    }
                ]
            }
        ],
        assets_by_ref=assets,
        require_grounding=True,
        require_numeric=True,
    )
    assert grounded[0]["sentences"][0]["source_refs"] == ["ua_12345678"]


def test_original_method_grounding_requires_content_from_the_bound_note() -> None:
    assets = {
        "ua_method": {
            "type": "note",
            "text": "Samples were normalized before model training and evaluation.",
            "numbers": [],
        }
    }
    unsupported = enforce_sentence_grounding_rules(
        [
            {
                "sentences": [
                    {
                        "text": "A proprietary optimizer selected every hyperparameter.",
                        "source_refs": ["ua_method"],
                    }
                ]
            }
        ],
        assets_by_ref=assets,
        require_grounding=True,
    )
    assert _no_prose_survived(unsupported), "笔记里没有的内容不许当作已接地"
    assert [
        s.get("downgraded_reason")
        for p in unsupported
        for s in p.get("downgraded_sentences") or []
    ], "删除要留痕"

    supported = enforce_sentence_grounding_rules(
        [
            {
                "sentences": [
                    {
                        "text": "Samples were normalized before model training.",
                        "source_refs": ["ua_method"],
                    }
                ]
            }
        ],
        assets_by_ref=assets,
        require_grounding=True,
    )
    assert supported[0]["sentences"]


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


def test_deterministic_paragraphs_without_evidence_never_reuses_unscoped_cards() -> None:
    paragraphs = deterministic_paragraphs(SECTION)
    texts = " ".join(p["text"] for p in paragraphs)
    assert "no evidence meeting" in texts
    assert "Introduces RAG combining a retriever with a generator." not in texts
    assert "gao2023survey" not in texts


def test_deterministic_paragraphs_without_evidence_exposes_a_gap_not_library_cards() -> None:
    paragraphs = deterministic_paragraphs({"title": "跨研究比较"}, language="zh")
    texts = " ".join(p["text"] for p in paragraphs)
    assert "尚无满足定位与可比性要求的证据" in texts
    assert "Introduces RAG" not in texts


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


def test_single_source_section_is_flagged_and_written_under_an_explicit_limit() -> None:
    """分级门禁放行的稿子里，只有一个来源的子问题不能写成综述结论。"""
    from paperforge_worker.pipelines.outline import question_driven_sections
    from paperforge_worker.pipelines.writing import _evidence_limitation_line

    one_source = {
        "question_id": "q-one",
        "question": "Does X hold?",
        "answer_status": "partial",
        "evidence": [
            {"evidence_id": "e1", "work_id": "w1", "cite_key": "a2024"},
            {"evidence_id": "e2", "work_id": "w1", "cite_key": "a2024"},
        ],
    }
    two_sources = {
        "question_id": "q-two",
        "question": "Does Y hold?",
        "answer_status": "answered",
        "evidence": [
            {"evidence_id": "e3", "work_id": "w1", "cite_key": "a2024"},
            {"evidence_id": "e4", "work_id": "w2", "cite_key": "b2024"},
        ],
    }
    sections = question_driven_sections(
        [one_source, two_sources],
        language="zh",
        allowed={"a2024", "b2024"},
    )
    assert sections[0]["evidence_limited"] is True
    assert sections[0]["distinct_source_count"] == 1
    assert sections[1]["evidence_limited"] is False

    limited = _evidence_limitation_line(sections[0], language="zh")
    assert "仅来自 1 篇独立文献" in limited
    assert "不得推广" in limited
    assert _evidence_limitation_line(sections[1], language="zh") == ""


def test_synthesis_block_guides_argument_without_relaxing_binding():
    """[SYNTHESIS] 是"怎么论证"的指引；句子级 evidence_ids 仍然强制。"""
    from paperforge_worker.pipelines.writing import _evidence_context_block

    section = {
        "title": "Does X hold?",
        "stance_summary": "conflicting",
        "comparison_clusters": [
            {
                "comparability_key": "k1",
                "classification": "conflicting",
                "evidence_ids": ["e1", "e2"],
            }
        ],
        "synthesis": {
            "claim": "The effect reverses on the larger split.",
            "agreement": [{"statement": "Both report a gain.", "evidence_ids": ["e1", "e2"]}],
            "conditional": [
                {
                    "dimension": "split",
                    "statement": "Only under leave-one-out.",
                    "evidence_ids": ["e1"],
                }
            ],
            "conflict": [
                {
                    "statement": "Directions disagree.",
                    "evidence_ids": ["e1", "e2"],
                    "comparability_key": "k1",
                }
            ],
            "gap": [{"statement": "No study covers the cold-start case."}],
        },
    }
    evidence = [
        {"evidence_id": "e1", "cite_key": "a2024", "grade": "B_located_prose", "text": "t1"},
        {"evidence_id": "e2", "cite_key": "b2024", "grade": "B_located_prose", "text": "t2"},
    ]

    block = _evidence_context_block(section=section, evidence=evidence, language="en")

    assert "[SYNTHESIS claim] The effect reverses on the larger split." in block
    assert "[SYNTHESIS agreement] Both report a gain. (EVIDENCE_ID=e1, e2)" in block
    assert "[SYNTHESIS conditional dimension=split]" in block
    assert "[SYNTHESIS conflict key=k1]" in block
    assert "[SYNTHESIS gap] No study covers the cold-start case." in block


def test_the_prompt_is_unchanged_when_no_synthesis_is_attached():
    """关掉该特性时 section 里没有这个键，提示词必须逐字节不变。"""
    from paperforge_worker.pipelines.writing import _evidence_context_block

    section = {"title": "Does X hold?", "stance_summary": "consistent"}
    evidence = [{"evidence_id": "e1", "cite_key": "a2024", "grade": "B_located_prose", "text": "t"}]

    assert "SYNTHESIS" not in _evidence_context_block(
        section=section, evidence=evidence, language="en"
    )


# ---- 小标题：印在论文上的东西不能是子问题原文 -------------------------------------

BUNDLES = [
    {
        "question_id": "q-1",
        "question": "CRISPR-Cas 在作物抗病性改良中面临哪些技术挑战（如脱靶效应、递送方法）？",
        "evidence": [
            {"cite_key": "lewis2020retrieval", "work_id": "w-1", "evidence_id": "e-1"},
            {"cite_key": "gao2023survey", "work_id": "w-2", "evidence_id": "e-2"},
        ],
        "stance_summary": "consistent",
    }
]


def test_a_section_heading_is_not_the_sub_question_verbatim() -> None:
    """实测（项目 ff6b9983 第 2 版）导出的 PDF 里五个正文小标题全是子问题原文，
    带问号、带举例括号，最长一条 44 个字。印在论文上的东西不能长这样。"""
    sections = question_driven_sections(BUNDLES, language="zh", allowed=set(WHITELIST))
    title = sections[0]["title"]
    assert "？" not in title
    assert "（如" not in title
    assert "哪些" not in title
    # 问题原文不能丢：写作器要靠它知道该答什么，语义评审也要拿它做判据。
    assert sections[0]["question"] == BUNDLES[0]["question"]
    assert BUNDLES[0]["question"] in sections[0]["summary"]


def test_a_heading_that_cannot_be_shortened_keeps_the_original_words() -> None:
    """删不动时保留原文：啰嗦但准确，好过截断到看不懂。"""
    assert _section_heading("光合作用", language="zh") == "光合作用"
    assert _section_heading("", language="zh") == "子问题"
    assert _section_heading("What are the limits of RAG?", language="en") == "limits of RAG"


async def test_generated_headings_replace_the_deterministic_ones() -> None:
    runner, _provider = _runner([{"headings": ["抗病改良的技术挑战"]}])
    outcome = await generate_outline(
        topic="CRISPR",
        research_question="How?",
        cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG", year=2020)],
        whitelist=WHITELIST,
        language="zh",
        runner=runner,
        sub_question_bundles=BUNDLES,
    )
    body = [s for s in outcome.tree["sections"] if s.get("question")]
    assert [s["title"] for s in body] == ["抗病改良的技术挑战"]


async def test_a_bad_heading_response_leaves_the_deterministic_titles_alone() -> None:
    """标题拟不好是遗憾，数量对不上是事故——数量/问号不合规就整组不采用。"""
    payloads: list[dict[str, Any]] = [
        {"headings": ["一", "二"]},
        {"headings": ["这是什么？"]},
        {"headings": []},
    ]
    for payload in payloads:
        runner, _provider = _runner([payload])
        outcome = await generate_outline(
            topic="CRISPR",
            research_question="How?",
            cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG", year=2020)],
            whitelist=WHITELIST,
            language="zh",
            runner=runner,
            sub_question_bundles=BUNDLES,
        )
        body = [s for s in outcome.tree["sections"] if s.get("question")]
        assert body[0]["title"] == _section_heading(BUNDLES[0]["question"], language="zh")


# ---- 框架章节：没有交代就写不出东西 -----------------------------------------------


async def test_frame_sections_carry_a_brief_and_their_own_length() -> None:
    """此前 abstract/introduction/conclusion 的 summary 是空字符串，写作提示词里
    「Section goal:」后面什么都没有。实测引言只有 171–274 字，而质量修复每轮重写
    这三节、指令又是纯减法，于是结论 351 → 136 → 137 字。"""
    outcome = await generate_outline(
        topic="RAG",
        research_question="How?",
        cards=[CardBrief(cite_key="lewis2020retrieval", title="RAG", year=2020)],
        whitelist=WHITELIST,
        language="zh",
        runner=None,
    )
    frames = {s["key"]: s for s in outcome.tree["sections"] if s.get("kind") == "frame"}
    assert set(frames) == {"abstract", "introduction", "conclusion"}
    for key, section in frames.items():
        assert section["summary"].strip(), key
        assert section["target_words"] == FRAME_SECTION_BRIEFS[key]["target_zh"]
    # 摘要不该按正文小节的篇幅写，引言和结论也各有各的量。
    assert frames["abstract"]["target_words"] < frames["conclusion"]["target_words"]
    assert frames["conclusion"]["target_words"] < frames["introduction"]["target_words"]


def _synthesis_bundle(agreements: int = 6, *, gap: str = "证据未在同一基准上比较这些方法。"):
    return {
        "question_id": "q-1",
        "question": "有哪些攻击方法？",
        "stance_summary": "consistent",
        "answer_status": "answered",
        "evidence": [
            {"evidence_id": f"e{i}", "cite_key": "a", "work_id": f"w{i % 3}"} for i in range(6)
        ],
        "synthesis": {
            "claim": "攻击方法分为投毒与模型提取两类。",
            "agreement": [
                {
                    "statement": f"发现 {i}：这一类方法依赖不同的攻击者知识。",
                    "evidence_ids": [f"e{i}"],
                }
                for i in range(agreements)
            ],
            "conditional": [],
            "conflict": [],
            "gap": [{"statement": gap, "evidence_ids": []}],
        },
    }


def test_argument_points_come_from_synthesis_not_a_generic_fallback() -> None:
    """一条方法论套话撑不起一节。

    实测：每个 body 章节只拿到 1 条 `argument_points`（「按证据等级陈述现有发现与
    适用边界」），因为要点只从 `comparison_clusters` 生成，而它几乎永远是空的；
    与此同时 SYNTH 的 8-13 条具体结论完全没被用作章节议程。
    """
    from paperforge_worker.pipelines.outline import _synthesis_argument_points

    points = _synthesis_argument_points(_synthesis_bundle(), language="zh")
    assert len(points) >= 5
    assert "按证据等级陈述现有发现与适用边界" not in points
    assert any("发现 0" in point for point in points)


def test_only_one_gap_reaches_the_agenda() -> None:
    """SYNTH 每个问题能给 1-4 条缺口；全放进议程等于把综述写成检索报告。"""
    from paperforge_worker.pipelines.outline import _synthesis_argument_points

    bundle = _synthesis_bundle()
    bundle["synthesis"]["gap"] = [
        {"statement": f"缺口 {i}", "evidence_ids": []} for i in range(4)
    ]
    points = _synthesis_argument_points(bundle, language="zh")
    assert sum(1 for point in points if "尚未解决" in point) == 1


def test_a_section_without_synthesis_keeps_the_old_fallback() -> None:
    from paperforge_worker.pipelines.outline import _synthesis_argument_points

    points = _synthesis_argument_points({"question_id": "q"}, language="zh")
    assert points == ["按证据等级陈述现有发现与适用边界"]


def test_subsections_are_numbered_under_their_parent() -> None:
    import asyncio
    from types import SimpleNamespace

    from paperforge_worker.pipelines.outline import (
        _headline_subsections,
        _with_frame_sections,
        question_driven_sections,
    )

    class _Runner:
        enabled = True

        async def agenerate_json(self, role, *, system_prompt, user_prompt, **kwargs):
            count = user_prompt.count("第 ")
            return SimpleNamespace(
                ok=True,
                value={"headings": [f"小标题{i}" for i in range(count)]},
                model="stub",
                error=None,
            )

    body = question_driven_sections([_synthesis_bundle()], language="zh", allowed={"a"})
    body = asyncio.run(_headline_subsections(body, language="zh", runner=_Runner()))
    sections = _with_frame_sections(body, language="zh", paper_type="review")
    children = [s for s in sections if s.get("level") == 2]
    assert children, "6 条要点应当拆出二级小节"
    assert all(child["parent_key"] == "s1" for child in children)
    assert [child["key"] for child in children] == [
        f"s1-{i + 1}" for i in range(len(children))
    ]
    parent = next(s for s in sections if s["key"] == "s1")
    assert parent["has_children"] is True
    # 母节只写引入段，子节各自成篇。
    assert parent["target_words"] < children[0]["target_words"]


def test_subsections_are_dropped_when_they_cannot_be_named() -> None:
    """二级标题是 PDF 里最显眼的东西之一，宁可没有也不能是半截短语。"""
    import asyncio
    from types import SimpleNamespace

    from paperforge_worker.pipelines.outline import (
        _headline_subsections,
        _with_frame_sections,
        question_driven_sections,
    )

    class _BrokenRunner:
        enabled = True

        async def agenerate_json(self, *args, **kwargs):
            return SimpleNamespace(ok=False, value=None, model="stub", error="boom")

    body = question_driven_sections([_synthesis_bundle()], language="zh", allowed={"a"})
    body = asyncio.run(_headline_subsections(body, language="zh", runner=_BrokenRunner()))
    sections = _with_frame_sections(body, language="zh", paper_type="review")
    assert not [s for s in sections if s.get("level") == 2]
    parent = next(s for s in sections if s["key"] == "s1")
    assert "has_children" not in parent
    # 撤掉小节不能损失内容：要点仍然全在母节上。
    assert len(parent["argument_points"]) >= 5
    # 也不能留下引入段的篇幅目标，否则母节会被按 250 字验收。
    assert "target_words" not in parent


def test_a_thin_section_stays_flat() -> None:
    from paperforge_worker.pipelines.outline import question_driven_sections

    body = question_driven_sections(
        [_synthesis_bundle(agreements=2)], language="zh", allowed={"a"}
    )
    assert [s.get("level", 1) for s in body] == [1]
