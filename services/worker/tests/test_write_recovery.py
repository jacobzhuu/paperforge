"""写作失败必须在章节内部恢复，而不是留个占位符继续往下走。

回归自项目 6a6bbf18（2026-08-14）：writer 反复 `output_truncated`，11 节里 5 节
掉进降级路径。当时的行为是「记下来、继续跑、照常交付」——本文件锁住相反的行为：
先重试，重试要换策略，救不回来的稿子不许算交付。

这里覆盖的四种失败都取自那次真实事故或它暴露的邻近风险：
零内容返回（推理吃光预算）、整段照抄证据原文、中途切成英文、只写了个开头。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from llm_runtime import LLMConfig, LLMResponse, LLMRunner
from paperforge_worker.pipelines.document import WriteOutcome, _write_one
from paperforge_worker.pipelines.writing import (
    SectionDraft,
    WritingContext,
    inspect_section_draft,
)
from test_document_persistence import _context, _seed

CITE_KEY = "lovelace2025poison"
S2_TITLE = "微生物信号如何调控植物免疫"
#: 段落里写哪个 evidence id 由用例决定；单元级用例用 e-1，文档级用例两节各有各的。
EVIDENCE_IDS = ["e-1"]
EVIDENCE_TEXT = (
    "Given that P. polymyxa could favorably alter the soil microbiome, we investigated "
    "whether the AIP quorum sensing autoinducers could shape the natural rhizosphere "
    "microbiome of Arabidopsis thaliana grown in a tropical forest soil."
)
SECTION = {
    "key": "s1",
    "kind": "body",
    "title": "天然产物信号如何影响植物微生物组结构",
    "summary": "回答该子问题；当前证据状态：一致。",
    "cite_keys": [CITE_KEY],
    "question_id": "q-1",
}
OUTLINE = {
    "topic": "植物—微生物互作",
    "research_question": "天然产物如何介导化学信号？",
    "sections": [SECTION],
    "sub_question_bundles": [
        {
            "question_id": "q-1",
            "evidence": [
                {
                    "evidence_id": "e-1",
                    "cite_key": CITE_KEY,
                    "text": EVIDENCE_TEXT,
                    "grade": "B_located_prose",
                }
            ],
        }
    ],
}


def _zh_paragraph(sentence_count: int = 6, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    sentence = (
        "群体感应自诱导肽重塑了根际群落的组成，说明信号分子的作用范围并不局限于"
        "产生菌自身，而是延伸到周边微生物的装配过程。"
    )
    return {
        "stance_summary": "consistent",
        "sentences": [
            {
                "text": sentence,
                "cite_keys": [CITE_KEY],
                "evidence_ids": list(EVIDENCE_IDS if evidence_ids is None else evidence_ids),
            }
            for _ in range(sentence_count)
        ],
    }


def _good_payload(evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "paragraphs": [_zh_paragraph(evidence_ids=evidence_ids) for _ in range(2)],
        "terms": [],
    }


def _frame_payload() -> dict[str, Any]:
    """摘要/引言这类框架章节：背景陈述，不带证据绑定。

    带因果或结论措辞而又没有 evidence_ids 的句子会被句级证据规则剥掉——这是产品
    的正确行为，替身也必须照着这个契约写，否则测的就成了规则本身。
    """
    sentence = (
        "本文综述了植物与微生物之间由天然产物介导的化学信号研究进展，"
        "内容涵盖根系分泌物、微生物挥发性有机物与共生信号分子等来源。"
    )
    return {
        "paragraphs": [
            {
                "stance_summary": "background",
                "sentences": [
                    {"text": sentence, "cite_keys": [], "evidence_ids": []} for _ in range(4)
                ],
            }
        ],
        "terms": [],
    }


def _verbatim_payload() -> dict[str, Any]:
    return {
        "paragraphs": [
            {
                "stance_summary": "consistent",
                "sentences": [
                    {
                        "text": EVIDENCE_TEXT,
                        "cite_keys": [CITE_KEY],
                        "evidence_ids": EVIDENCE_IDS,
                    }
                ],
            }
        ],
        "terms": [],
    }


def _english_payload() -> dict[str, Any]:
    sentence = (
        "Autoinducing peptides reshape the rhizosphere community composition, showing that "
        "signalling molecules act well beyond the producing strain and reach the assembly of "
        "neighbouring microbial taxa in the root zone."
    )
    return {
        "paragraphs": [
            {
                "stance_summary": "consistent",
                "sentences": [
                    {"text": sentence, "cite_keys": [CITE_KEY], "evidence_ids": EVIDENCE_IDS}
                    for _ in range(4)
                ],
            }
        ],
        "terms": [],
    }


class _ScriptedProvider:
    """按脚本回答，并记下每一次实际发出的 prompt（用来断言重试确实换了要法）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(request.user_prompt)
        payload = self.script.pop(0) if self.script else _good_payload()
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(text=text, model="stub-model", provider="stub")


def _runner(script: list[Any]) -> tuple[LLMRunner, _ScriptedProvider]:
    provider = _ScriptedProvider(script)
    return (
        LLMRunner(
            LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
            provider=provider,
        ),
        provider,
    )


async def _run(script: list[Any], session_factory) -> tuple[SectionDraft, WriteOutcome, Any]:
    project_id, _cite_key = await _seed(session_factory)
    runner, provider = _runner(script)
    outcome = WriteOutcome()
    draft = await _write_one(
        section=SECTION,
        cards={CITE_KEY: {"title": "Autoinducing peptides", "year": 2025}},
        whitelist={CITE_KEY},
        writing_context=WritingContext(outline=OUTLINE, language="zh"),
        runner=runner,
        context=_context(project_id, session_factory),
        outcome=outcome,
    )
    return draft, outcome, provider


async def test_a_section_that_comes_back_empty_is_retried_with_a_smaller_ask(
    session_factory,
) -> None:
    """零内容返回不是「写得不对」，是要的太多——重试必须降低需求。

    deepseek 系的输出上限被 clamp 在 8192，加预算没有余量；唯一能变的是让这一节
    少写一点。所以第二次请求里必须出现紧凑档的指令。
    """
    draft, outcome, provider = await _run(["", _good_payload()], session_factory)

    assert draft.generator.startswith("llm")
    assert draft.attempts == 2
    assert inspect_section_draft(draft, language="zh", evidence=[]) == []
    assert [item["section"] for item in outcome.recovered_sections] == ["s1"]
    assert "最多写" in provider.prompts[1], "紧凑档指令没有进第二次 prompt"
    assert "最多写" not in provider.prompts[0]


async def test_a_section_that_copies_evidence_verbatim_is_rewritten(session_factory) -> None:
    """整段照抄要靠**告诉模型它抄了**来修，而不是原样再问一遍。"""
    draft, outcome, provider = await _run([_verbatim_payload(), _good_payload()], session_factory)

    assert draft.attempts == 2
    body = " ".join(p["text"] for p in draft.paragraphs)
    assert EVIDENCE_TEXT not in body
    assert "照抄" in provider.prompts[1]
    assert outcome.recovered_sections


async def test_a_section_written_in_the_wrong_language_is_rewritten(session_factory) -> None:
    draft, _outcome, provider = await _run([_english_payload(), _good_payload()], session_factory)

    assert draft.attempts == 2
    assert "中文" in provider.prompts[1]
    assert inspect_section_draft(draft, language="zh", evidence=[]) == []


async def test_a_section_that_is_far_too_short_is_developed_further(session_factory) -> None:
    stub = {
        "paragraphs": [
            {
                "stance_summary": "consistent",
                "sentences": [
                    {
                        "text": "本节讨论信号分子。",
                        "cite_keys": [CITE_KEY],
                        "evidence_ids": EVIDENCE_IDS,
                    }
                ],
            }
        ],
        "terms": [],
    }
    draft, _outcome, provider = await _run([stub, _good_payload()], session_factory)

    assert draft.attempts == 2
    assert "篇幅" in provider.prompts[1]


async def test_retries_are_bounded_and_the_best_attempt_survives(session_factory) -> None:
    """全部重试都失败时，留住最好的一版，并明确记为没写成。

    「有正文但有瑕疵」严格好于「一句占位符」——把前者换成后者是在丢掉已经付过钱
    的内容，而这正是事故当天发生的事。
    """
    draft, outcome, provider = await _run(
        [_verbatim_payload(), _verbatim_payload(), _verbatim_payload()],
        session_factory,
    )

    assert len(provider.prompts) == 3, "重试次数必须有上限"
    assert draft.has_body, "最后留下的应是模型正文，不是降级占位"
    assert any(item["reason"] == "section_not_matured" for item in outcome.warnings)
    defects = {
        defect.code
        for defect in inspect_section_draft(
            draft,
            language="zh",
            evidence=OUTLINE["sub_question_bundles"][0]["evidence"],
        )
    }
    # 照抄进来的那一段同时也是英文、也过短——三条判据各自独立地抓到了它。
    assert "verbatim_evidence_copy" in defects
    assert defects == {"verbatim_evidence_copy", "language_mismatch", "too_short"}


async def test_a_section_the_model_never_writes_is_reported_as_incomplete(
    session_factory,
) -> None:
    """三次都拿不到内容：不留假正文，且必须能被上层看见。"""
    draft, outcome, _provider = await _run(["", "", ""], session_factory)

    assert not draft.has_body
    assert draft.generator == "deterministic_fallback"
    body = " ".join(p["text"] for p in draft.paragraphs)
    assert EVIDENCE_TEXT not in body, "降级文案不得包含证据原文"
    assert SECTION["summary"] not in body, "降级文案不得把大纲提示语当正文"
    assert any(item["reason"] == "section_not_matured" for item in outcome.warnings)


REVIEW_OUTLINE = {
    "topic": "植物—微生物互作中的天然产物信号",
    "research_question": "天然产物如何介导植物与微生物之间的化学信号？",
    "language": "zh",
    "sections": [
        {"key": "abstract", "kind": "frame", "level": 1, "title": "摘要"},
        {
            "key": "s1",
            "kind": "body",
            "level": 1,
            "title": "植物释放哪些天然产物作为信号",
            "summary": "回答该子问题；当前证据状态：一致。",
            "cite_keys": ["lovelace2025poison"],
            "question_id": "q-1",
        },
        {
            "key": "s2",
            "kind": "body",
            "level": 1,
            "title": "微生物信号如何调控植物免疫",
            "summary": "回答该子问题；当前证据状态：一致。",
            "cite_keys": ["lovelace2025poison"],
            "question_id": "q-2",
        },
    ],
    "sub_question_bundles": [
        {
            "question_id": "q-1",
            "evidence": [
                {
                    "evidence_id": "e-1",
                    "cite_key": "lovelace2025poison",
                    "text": EVIDENCE_TEXT,
                    "grade": "B_located_prose",
                    "page": 3,
                }
            ],
        },
        {
            "question_id": "q-2",
            "evidence": [
                {
                    "evidence_id": "e-2",
                    "cite_key": "lovelace2025poison",
                    "text": "Chitin perception through CERK1 primes systemic resistance.",
                    "grade": "B_located_prose",
                    "page": 5,
                }
            ],
        },
    ],
}


async def _seed_review(session_factory) -> uuid.UUID:
    """一个**有引用契约**的中文综述项目：章节带 cite_keys，子问题带证据。

    没有引用契约的章节写作器根本不会调用（R15：空白名单不许自由写作），拿那种
    大纲测重试等于什么都没测。
    """
    from db import create_outline, create_project, create_user, upsert_entry, upsert_work
    from test_document_persistence import _Candidate

    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="天然产物信号综述",
            paper_type="review",
            language="zh",
            topic="植物微生物互作",
            owner_id=owner.id,
        )
        work, _ = await upsert_work(session, _Candidate())
        entry, _ = await upsert_entry(
            session,
            project_id=project.id,
            work_id=work.id,
            added_via="search",
            status="selected",
            verified=True,
        )
        entry.bibtex_key = "lovelace2025poison"
        await create_outline(session, project_id=project.id, tree=REVIEW_OUTLINE)
        await session.commit()
        return project.id


class _PerSectionProvider:
    """按章节标题走脚本；``None`` 表示「正常写出来」。

    正常应答是**照着 prompt 里给的 EVIDENCE_ID 生成的**——真实模型也只能引用发给它的
    那几条证据，写死一个 id 会被句级证据规则剥掉，测出来的失败就不是被测的那个失败。
    """

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: dict[str, int] = {}

    def generate(self, request):
        prompt = request.user_prompt
        key = prompt.split("Write section: ")[1].split("\n")[0].strip()
        self.calls[key] = self.calls.get(key, 0) + 1
        queue = self.script.get(key)
        payload = queue.pop(0) if queue else None
        if payload is None:
            evidence_ids = re.findall(r"EVIDENCE_ID=(\S+)", prompt) or []
            # 框架章节（摘要）没有分配证据，正常应答是背景陈述而不是带绑定的论断。
            payload = _good_payload(evidence_ids[:1]) if evidence_ids else _frame_payload()
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(text=text, model="stub-model", provider="stub")


async def _write_manuscript(script: dict[str, list[Any]], session_factory, monkeypatch):
    from paperforge_worker.context import JobContext
    from paperforge_worker.pipelines.document import write_document

    project_id = await _seed_review(session_factory)
    provider = _PerSectionProvider(script)
    runner = LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
        provider=provider,
    )
    monkeypatch.setattr(JobContext, "llm_runner", lambda self: runner)
    outcome = await write_document(
        _context(project_id, session_factory),
        language="zh",
        title="综述",
        coherence=False,
    )
    return outcome, provider


async def test_a_section_that_failed_the_first_pass_is_recovered_before_delivery(
    session_factory, monkeypatch
) -> None:
    """交付前的补写扫描要真的把稿子补完整。

    主循环里 s2 用尽三次重试仍然没写出来；补写扫描此时全篇摘要已经建好，再写一次
    就成了。这一轮的价值不在「多试一次」，而在**上下文变了**。
    """
    outcome, provider = await _write_manuscript(
        {S2_TITLE: ["", "", ""]},
        session_factory,
        monkeypatch,
    )

    assert provider.calls[S2_TITLE] == 4, "主循环三次 + 补写一次"
    assert outcome.incomplete_sections == []
    assert outcome.complete
    assert outcome.to_payload()["complete"] is True


async def test_a_manuscript_with_an_unrecoverable_section_is_not_complete(
    session_factory, monkeypatch
) -> None:
    """救不回来就不许算写完——产物照旧留库可查，但完整性判定必须是 False。"""
    outcome, _provider = await _write_manuscript(
        {S2_TITLE: ["", "", "", "", ""]},
        session_factory,
        monkeypatch,
    )

    assert not outcome.complete
    assert [item["section"] for item in outcome.incomplete_sections] == ["s2"]
    assert outcome.incomplete_sections[0]["defects"] == ["not_generated"]
    assert outcome.to_payload()["complete"] is False


def test_a_deterministic_search_log_is_not_judged_by_prose_length() -> None:
    """检索方法节是**故意**确定性生成的检索日志，不是论证。

    拿正文的篇幅线去量它，会把一节正确的产物判成没写成，然后反复重写——而重写只会
    再确定性地生成同一段文字，稿子还会被判定为不完整、卡住交付。
    """
    draft = SectionDraft(section_key="search_methods", title="检索方法")
    draft.generator = "deterministic_search_log"
    draft.paragraphs = [
        {"text": "检索式：natural product AND signalling；共 3 个来源。", "cite_keys": []}
    ]

    assert draft.has_body
    assert inspect_section_draft(draft, language="zh") == []

    empty = SectionDraft(section_key="search_methods", title="检索方法")
    empty.generator = "deterministic_search_log"
    empty.paragraphs = [{"text": "   ", "cite_keys": []}]
    assert [defect.code for defect in inspect_section_draft(empty, language="zh")] == [
        "not_generated"
    ]


def test_write_outcome_is_only_complete_when_every_section_has_prose() -> None:
    outcome = WriteOutcome()
    outcome.section_count = 7
    assert outcome.complete
    outcome.incomplete_sections = [{"section": "s3", "defects": ["not_generated"]}]
    assert not outcome.complete


@pytest.mark.parametrize("generator", ["deterministic_fallback", "evidence_gap_skeleton", "failed"])
def test_placeholder_generators_never_count_as_body(generator: str) -> None:
    draft = SectionDraft(section_key="s1", title="s1")
    draft.generator = generator
    draft.paragraphs = [{"text": "本节尚未生成：没有可用的写作模型输出，待重写。", "cite_keys": []}]
    assert not draft.has_body
    assert [defect.code for defect in inspect_section_draft(draft, language="zh")] == [
        "not_generated"
    ]


def test_the_same_evidence_text_is_shown_to_the_writer_only_once() -> None:
    """同一篇文献切出的重复条目会挤占上下文预算，也让「有多少证据没用上」虚高。

    实测项目 6a6bbf18：s3/s5/s6 的证据池里各有 3 条是完全相同的正文，s1/s2 各 2 条。
    去重要按正文而不是按 ID——重复的正是不同 ID、同一段字。
    """
    from paperforge_worker.pipelines.writing import dedupe_evidence_by_text

    repeated = "Auxins are pivotal hormones that significantly influence root exudation."
    kept = dedupe_evidence_by_text(
        [
            {"evidence_id": "e-1", "grade": "A_located_structured", "text": repeated},
            {"evidence_id": "e-2", "grade": "B_located_prose", "text": "另一段证据。"},
            {"evidence_id": "e-3", "grade": "B_located_prose", "text": f"  {repeated}\n"},
            {"evidence_id": "e-4", "grade": "B_located_prose", "text": ""},
            {"evidence_id": "e-5", "grade": "B_located_prose", "text": ""},
        ]
    )

    # 先出现的那条留下（上游已按等级排过序），后面重复的丢掉。
    assert [item["evidence_id"] for item in kept] == ["e-1", "e-2", "e-4", "e-5"]


def test_a_section_at_a_quarter_of_its_target_is_not_accepted() -> None:
    """验收线此前与篇幅目标完全脱钩：正文 180 字、框架 80 字，而目标是 1200 / 350–800。

    实测（项目 ff6b9983 第 3 版）十节里五节首稿只有 264–583 字，一次 too_short 重写
    都没触发。现在验收线跟着本节自己的目标走。
    """
    from paperforge_worker.pipelines.writing import section_minimum_words

    body = {"key": "s1", "target_words": None}
    assert section_minimum_words(body, language="zh", is_frame=False) == 600
    intro = {"key": "introduction", "target_words": 800}
    assert section_minimum_words(intro, language="zh", is_frame=True) == 400
    # 没有声明目标的框架章节（旧大纲）沿用绝对下限，而不是拿正文的 1200 字去量摘要。
    from paperforge_worker.pipelines.writing import MIN_FRAME_WORDS_ZH

    assert section_minimum_words({}, language="zh", is_frame=True) == MIN_FRAME_WORDS_ZH
    abstract = {"key": "abstract", "target_words": 350}
    assert section_minimum_words(abstract, language="zh", is_frame=True) == 175


def test_a_section_whose_evidence_is_narrow_is_not_pushed_to_write_longer() -> None:
    """证据只有一个来源时抬验收线就是逼灌水，而灌水正是要防的东西。"""
    from paperforge_worker.pipelines.writing import MIN_BODY_WORDS_ZH, section_minimum_words

    thin = {"key": "s4", "evidence_limited": True, "distinct_source_count": 1}
    assert section_minimum_words(thin, language="zh", is_frame=False) == MIN_BODY_WORDS_ZH


def test_a_thin_section_is_rewritten_but_never_reported_as_missing_prose() -> None:
    """「写薄了」和「没写出来」是两件事。

    抬高验收线之后交付判定一度说出「6 个章节在重试与自动修复之后仍然没有正文」——
    而那 6 节里引言有 515 字、s6 有 649 字（项目 ff6b9983 第 4 版实测）。短要驱动
    重写，但不能否决交付，否则系统会用一句与事实相反的话拒绝自己的产物。
    """
    from paperforge_worker.pipelines.document import NON_BLOCKING_SECTION_DEFECTS
    from paperforge_worker.pipelines.writing import (
        SectionDraft,
        inspect_section_draft,
        section_absolute_minimum,
    )

    section = {"key": "introduction", "kind": "frame", "target_words": 800}
    thin = SectionDraft(
        section_key="introduction",
        title="引言",
        paragraphs=[{"text": "引" * 300, "cite_keys": []}],
        generator="llm:stub",
    )
    codes = [
        defect.code
        for defect in inspect_section_draft(
            thin, language="zh", is_frame=True, section=section
        )
    ]
    assert codes == ["below_target"]
    assert set(codes) <= NON_BLOCKING_SECTION_DEFECTS

    stub = SectionDraft(
        section_key="introduction",
        title="引言",
        paragraphs=[{"text": "引" * 10, "cite_keys": []}],
        generator="llm:stub",
    )
    stub_codes = [
        defect.code
        for defect in inspect_section_draft(
            stub, language="zh", is_frame=True, section=section
        )
    ]
    assert stub_codes == ["too_short"]
    assert not set(stub_codes) <= NON_BLOCKING_SECTION_DEFECTS
    assert section_absolute_minimum(language="zh", is_frame=True) < 300


def test_an_appendix_table_is_not_measured_against_a_prose_length() -> None:
    """证据台账的正文是一张表加两句说明。拿正文的篇幅线去量它，会把一节正确的产物
    判成没写成——实测台账 192 字被报成「1 个章节仍然没有正文」。语种和逐字照抄这些
    检查照旧生效，只是不量篇幅。"""
    from paperforge_worker.pipelines.writing import SectionDraft, inspect_section_draft

    ledger = SectionDraft(
        section_key="evidence_ledger",
        title="附录：证据台账",
        appendix=True,
        paragraphs=[{"text": "下表按证据单元逐条列出出处与定位。", "cite_keys": []}],
        generator="llm:stub",
    )
    assert inspect_section_draft(ledger, language="zh", section={"appendix": True}) == []
