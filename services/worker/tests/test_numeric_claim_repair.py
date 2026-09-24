"""没有出处的数字必须在交付前从正文里消失。

此前 NUMLINT 只报数：``unsourced_number_count: 2`` 写进 outcome，稿子照常交付。
一个查不到出处的「超过 200 种化合物」读起来和有出处的数据一模一样，而审稿人只会
当它是编的——检测到却不处理，等于把问题转嫁给读者。

阶梯：改写成定性表述（保留论断、去掉数字）→ 改不动就删句 → 引用绑定全程不动。
"""

from __future__ import annotations

import json
from typing import Any

from llm_runtime import LLMConfig, LLMResponse, LLMRunner
from paperforge_worker.pipelines.writing import SectionDraft, repair_unsourced_numbers


class _Provider:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def generate(self, request):
        self.prompts.append(request.user_prompt)
        payload = self.payloads.pop(0) if self.payloads else {}
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(text=text, model="stub-model", provider="stub")


def _runner(payloads: list[Any]) -> tuple[LLMRunner, _Provider]:
    provider = _Provider(payloads)
    return (
        LLMRunner(
            LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
            provider=provider,
        ),
        provider,
    )


def _draft() -> SectionDraft:
    draft = SectionDraft(section_key="s1", title="根系分泌物")
    draft.paragraphs = [
        {
            "text": "已鉴定出超过200种化合物。根系分泌物塑造根际群落。",
            "cite_keys": ["chen2025"],
            "sentences": [
                {
                    "text": "已鉴定出超过200种化合物。",
                    "cite_keys": ["chen2025"],
                    "evidence_ids": ["e-1"],
                },
                {
                    "text": "根系分泌物塑造根际群落。",
                    "cite_keys": ["chen2025"],
                    "evidence_ids": ["e-1"],
                },
            ],
        }
    ]
    draft.generator = "llm:stub"
    return draft


async def test_an_unsourced_number_is_rewritten_qualitatively() -> None:
    """首选是保住论断：把数字换成定性说法，而不是删掉整句。"""
    runner, provider = _runner(
        [{"sentences": [{"index": 0, "text": "已鉴定出大量具有生态功能的化合物。"}]}]
    )
    draft, stats = await repair_unsourced_numbers(
        draft=_draft(),
        values={"200"},
        language="zh",
        runner=runner,
    )

    body = " ".join(p["text"] for p in draft.paragraphs)
    assert "200" not in body
    assert "大量具有生态功能的化合物" in body
    assert "根系分泌物塑造根际群落。" in body, "没问题的句子不许被牵连"
    assert stats == {"rewritten": 1, "removed": 0, "kept": 0}
    assert "200" in provider.prompts[0], "改写请求要指名是哪个数值"


async def test_a_rewrite_that_smuggles_in_another_number_is_rejected() -> None:
    """改写不能「换一个数字」——校验按「一个数字都不许剩」执行。"""
    runner, _provider = _runner(
        [{"sentences": [{"index": 0, "text": "已鉴定出超过180种化合物。"}]}]
    )
    draft, stats = await repair_unsourced_numbers(
        draft=_draft(),
        values={"200"},
        language="zh",
        runner=runner,
    )

    body = " ".join(p["text"] for p in draft.paragraphs)
    assert "180" not in body and "200" not in body
    assert stats == {"rewritten": 0, "removed": 1, "kept": 0}


async def test_the_sentence_is_removed_when_the_model_cannot_repair_it() -> None:
    runner, _provider = _runner(["not json"])
    draft, stats = await repair_unsourced_numbers(
        draft=_draft(),
        values={"200"},
        language="zh",
        runner=runner,
    )

    body = " ".join(p["text"] for p in draft.paragraphs)
    assert "200" not in body
    assert "根系分泌物塑造根际群落。" in body
    assert stats["removed"] == 1
    downgraded = [
        item
        for paragraph in draft.paragraphs
        for item in paragraph.get("downgraded_sentences") or []
    ]
    assert [item["downgraded_reason"] for item in downgraded] == ["numlint_unsourced_number"]


async def test_citation_bindings_survive_the_repair() -> None:
    """修复不得动引用与证据绑定——这是全篇溯源的底线。"""
    runner, _provider = _runner(
        [{"sentences": [{"index": 0, "text": "已鉴定出大量具有生态功能的化合物。"}]}]
    )
    draft, _stats = await repair_unsourced_numbers(
        draft=_draft(),
        values={"200"},
        language="zh",
        runner=runner,
    )

    sentences = [item for p in draft.paragraphs for item in p.get("sentences") or []]
    assert all(item["cite_keys"] == ["chen2025"] for item in sentences)
    assert all(item["evidence_ids"] == ["e-1"] for item in sentences)


async def test_a_section_without_flagged_numbers_is_untouched() -> None:
    runner, provider = _runner([])
    original = _draft()
    before = json.dumps(original.paragraphs, ensure_ascii=False)
    draft, stats = await repair_unsourced_numbers(
        draft=original,
        values=set(),
        language="zh",
        runner=runner,
    )
    assert json.dumps(draft.paragraphs, ensure_ascii=False) == before
    assert stats == {"rewritten": 0, "removed": 0, "kept": 0}
    assert provider.prompts == [], "没有待修数值时不许发调用"
