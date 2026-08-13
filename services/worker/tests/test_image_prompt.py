"""提示词润色的行为契约。

守的是两件事：

1. 交互式生图上下文有硬上限，且超长文稿仍保留分节覆盖和末尾结论；
2. 模型给不出可用结果时返回 None，由调用方阻止未经分析的提示词进入生图服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from paperforge_worker.pipelines.image_prompt import (
    MAX_PAPER_CONTEXT_CHARS,
    MAX_PROMPT_CHARS,
    analyze_image_prompt,
    build_paper_context,
    refine_image_prompt,
    rewrite_rejected_image_prompt,
)

_REFINED = (
    "A wide conceptual illustration of a root system anchoring a soil slope, seen in "
    "cross-section, with fine roots threading through layered earth. Restrained earth "
    "palette, soft directional light, publication quality."
)


@dataclass
class _Result:
    ok: bool
    value: Any
    model: str = "stub-model"


class _Runner:
    def __init__(self, value: Any, *, ok: bool = True, enabled: bool = True) -> None:
        self._result = _Result(ok=ok, value=value)
        self.enabled = enabled
        self.calls: list[dict[str, Any]] = []

    async def agenerate_json(self, role: str, **kwargs: Any) -> _Result:
        self.calls.append({"role": role, **kwargs})
        return self._result


def _spec(**overrides: Any) -> dict[str, Any]:
    spec = {
        "kind": "ai_image",
        "prompt": "root system. composition: cross-section",
        "size": "1536x1024",
        "quality": "medium",
        "style": "clean academic conceptual illustration",
        "semantics": {
            "subject": "root system anchoring a soil slope",
            "composition": "cross-section, roots threading through layered earth",
            "elements": ["fine roots", "soil layers"],
            "text_policy": "auto",
        },
    }
    spec.update(overrides)
    return spec


@pytest.mark.asyncio
async def test_refined_prompt_replaces_the_assembled_one() -> None:
    runner = _Runner({"prompt": _REFINED})
    assert await refine_image_prompt(_spec(), runner=runner) == _REFINED


@pytest.mark.asyncio
async def test_brief_carries_semantics_and_supplied_paper_context() -> None:
    runner = _Runner({"prompt": _REFINED})
    await refine_image_prompt(_spec(), runner=runner, context="边坡稳定性; 根系锚固示意")

    brief = runner.calls[0]["user_prompt"]
    assert "root system anchoring a soil slope" in brief
    assert "fine roots" in brief
    # 画幅要让模型知道，否则它会按方图构图。
    assert "3:2" in brief
    assert "边坡稳定性" in brief


@pytest.mark.asyncio
async def test_full_paper_analysis_bounds_context_without_losing_the_conclusion() -> None:
    final_prompt = (
        "Create a wide journal graphical abstract that integrates the paper's taxonomy, "
        "mechanisms, defenses, and open questions. Arrange four connected scientific panels "
        "around one central theme with a restrained palette and crisp publication-ready detail. "
        "Preserve correct spelling and include no text beyond the requested short labels."
    )
    runner = _Runner(
        {
            "title": "全文综述图",
            "caption": "全文核心主题与研究缺口的图形摘要。",
            "alt_text": "横向图形摘要连接分类、机制、防御与开放问题。",
            "subject": "全文研究主题的综合图形摘要",
            "composition": "中心主题连接分类、机制、防御和开放问题四个板块",
            "elements": ["分类", "机制", "防御", "开放问题"],
            "text_policy": "auto",
            "prompt": final_prompt,
        }
    )
    sentinel = "结论部分末尾的唯一标记-DO-NOT-TRUNCATE"
    paper = "引言正文-BEGIN" + ("跨章节证据。" * 10_000) + sentinel
    analysis = await analyze_image_prompt(
        user_intent="生成全文综述图",
        full_paper=paper,
        runner=runner,
        current_spec=_spec(),
    )

    assert analysis is not None
    assert analysis.prompt == final_prompt
    submitted = runner.calls[0]["user_prompt"].split("SECTION-BALANCED PAPER CONTEXT:\n", 1)[1]
    assert len(submitted) == MAX_PAPER_CONTEXT_CHARS
    assert submitted.startswith("引言正文-BEGIN")
    assert sentinel in submitted
    assert "middle content omitted" in submitted
    assert runner.calls[0]["metadata"]["stage"] == "image_prompt_full_paper"


def test_structured_paper_context_preserves_balanced_section_head_and_tail_coverage() -> None:
    sections = [
        (
            f"s{index}",
            f"Section {index}",
            f"SECTION-{index}-BEGIN " + (character * 30_000) + f" SECTION-{index}-END",
        )
        for index, character in enumerate("ABC", start=1)
    ]

    context = build_paper_context("A very long paper", sections)

    assert len(context) == MAX_PAPER_CONTEXT_CHARS
    for index in range(1, 4):
        assert f"[s{index}] Section {index}" in context
        assert f"SECTION-{index}-BEGIN" in context
        assert f"SECTION-{index}-END" in context
    assert context.count("middle content omitted") == 3


@pytest.mark.asyncio
async def test_text_policy_is_passed_through_instead_of_being_hardcoded() -> None:
    """默认不再一律禁字；显式要求无字时才把这条约束交代给模型。"""
    permissive = _Runner({"prompt": _REFINED})
    await refine_image_prompt(_spec(), runner=permissive)
    assert "no lettering at all" not in permissive.calls[0]["user_prompt"]

    strict = _Runner({"prompt": _REFINED})
    spec = _spec()
    spec["semantics"]["text_policy"] = "none"
    await refine_image_prompt(spec, runner=strict)
    assert "no lettering at all" in strict.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_disabled_or_failed_model_falls_back_silently() -> None:
    assert await refine_image_prompt(_spec(), runner=None) is None
    assert await refine_image_prompt(_spec(), runner=_Runner(None, enabled=False)) is None
    assert await refine_image_prompt(_spec(), runner=_Runner(None, ok=False)) is None
    # 模型答非所问（漏了 prompt 键）同样退回拼接式提示词。
    assert await refine_image_prompt(_spec(), runner=_Runner({"image": "..."})) is None


@pytest.mark.asyncio
async def test_unusable_results_are_rejected() -> None:
    # 太短：多半是原样回抄而不是真的润色过。
    assert await refine_image_prompt(_spec(), runner=_Runner({"prompt": "an image"})) is None
    # 带 URL：spec 校验会拒，不能等到生图时才发现。
    injected = _Runner({"prompt": f"{_REFINED} see https://example.com/reference.png"})
    assert await refine_image_prompt(_spec(), runner=injected) is None


@pytest.mark.asyncio
async def test_overlong_results_are_cut_at_a_sentence_boundary() -> None:
    long_prompt = " ".join([_REFINED] * 20)
    result = await refine_image_prompt(_spec(), runner=_Runner({"prompt": long_prompt}))
    assert result is not None
    assert len(result) <= MAX_PROMPT_CHARS
    assert result.endswith(".")


@pytest.mark.asyncio
async def test_legacy_specs_without_semantics_still_get_refined() -> None:
    """语义层是后加的；只有一句 prompt 的历史 spec 也要能润色。"""
    runner = _Runner({"prompt": _REFINED})
    legacy = {"kind": "ai_image", "prompt": "A clean academic illustration of soil erosion"}
    assert await refine_image_prompt(legacy, runner=runner) == _REFINED
    assert "soil erosion" in runner.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_content_rejection_rewrite_requires_an_explicit_safe_decision() -> None:
    rewritten = (
        "A neutral medical textbook illustration showing a clinical care workflow, "
        "with abstract human silhouettes and non-graphic anatomical context. Use a "
        "restrained palette and a clean publication-ready layout."
    )
    runner = _Runner(
        {
            "can_retry": True,
            "prompt": rewritten,
            "reason": "Removed ambiguous graphic wording while retaining clinical context.",
        }
    )
    result = await rewrite_rejected_image_prompt(
        "A graphic view of the clinical workflow for an academic paper.",
        runner=runner,
    )
    assert result is not None
    assert result.prompt == rewritten
    assert result.reason.startswith("Removed ambiguous")
    assert runner.calls[0]["metadata"]["stage"] == "image_prompt_compliance_rewrite"
    assert "Never disguise" in runner.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_content_rejection_rewrite_never_retries_without_a_safe_changed_prompt() -> None:
    original = "A neutral academic illustration of a clinical workflow with a restrained palette."
    assert (
        await rewrite_rejected_image_prompt(
            original,
            runner=_Runner({"can_retry": False, "prompt": "", "reason": "Intent may be unsafe."}),
        )
        is None
    )
    assert (
        await rewrite_rejected_image_prompt(
            original,
            runner=_Runner(
                {"can_retry": True, "prompt": original, "reason": "No meaningful change."}
            ),
        )
        is None
    )
    assert await rewrite_rejected_image_prompt(original, runner=None) is None
