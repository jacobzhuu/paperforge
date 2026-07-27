"""结构化视觉规划器的行为契约。

守的是三件事：

1. 规划**从不**调用 ImageProvider——生图只能由用户显式确认后触发；
2. 文本模型不可用 / 输出不合法时，确定性回退接手，`visual_plan` 永远有产物；
3. 模型编造的章节键、悬空的边、量化描述都在入库前被挡掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from paperforge_worker.pipelines.visual_planner import (
    MAX_AI_IMAGES,
    SectionBrief,
    plan_visuals,
)


@dataclass
class _Result:
    ok: bool
    value: Any
    model: str = "stub-model"


class _Runner:
    """最小的 LLMRunner 替身；记录被问了什么。"""

    def __init__(self, value: Any, *, ok: bool = True, enabled: bool = True) -> None:
        self._result = _Result(ok=ok, value=value)
        self.enabled = enabled
        self.calls: list[dict[str, Any]] = []

    async def agenerate_json(self, role: str, **kwargs: Any) -> _Result:
        self.calls.append({"role": role, **kwargs})
        return self._result


SECTIONS = [
    SectionBrief(key="introduction", title="引言", excerpt="推荐系统在交互序列上排序。"),
    SectionBrief(key="method", title="方法", excerpt="模型先编码交互，再打分。"),
]


def _diagram_proposal(**overrides: Any) -> dict[str, Any]:
    payload = {
        "kind": "diagram",
        "reason": "引言描述了一条连续过程",
        "source_section_keys": ["introduction"],
        "target_section_key": "introduction",
        "title": "流程",
        "caption": "从交互到排序的过程",
        "alt_text": "从左到右的过程示意图",
        "diagram": {
            "direction": "LR",
            "nodes": [{"id": "n1", "label": "交互"}, {"id": "n2", "label": "排序"}],
            "edges": [{"source": "n1", "target": "n2"}],
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_planner_records_reason_and_source_sections() -> None:
    runner = _Runner({"proposals": [_diagram_proposal()]})
    planned, generator = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=False)
    assert generator.startswith("llm:")
    assert len(planned) == 1
    assert planned[0].kind == "diagram"
    # 「为什么建议这张图」是新增的上下文，卡片要展示它。
    assert planned[0].reason
    assert planned[0].source_section_keys == ["introduction"]
    # 用的是 planner 角色，调用会进 llm_call_log。
    assert runner.calls[0]["role"] == "planner"
    assert runner.calls[0]["metadata"]["stage"] == "visual_plan"


@pytest.mark.asyncio
async def test_planner_falls_back_when_the_text_model_is_unavailable() -> None:
    """模型不可用不是错误路径：调用方接手用确定性建议，不抛异常。"""
    planned, generator = await plan_visuals(sections=SECTIONS, runner=None, allow_ai_images=True)
    assert planned == []
    assert generator == "deterministic"

    disabled, generator = await plan_visuals(
        sections=SECTIONS, runner=_Runner(None, enabled=False), allow_ai_images=True
    )
    assert disabled == []
    assert generator == "deterministic"


@pytest.mark.asyncio
async def test_planner_falls_back_on_malformed_output() -> None:
    for value in (None, {"proposals": "not a list"}, {"proposals": []}, {}):
        planned, generator = await plan_visuals(
            sections=SECTIONS, runner=_Runner(value), allow_ai_images=True
        )
        assert planned == []
        assert generator == "deterministic_fallback"


@pytest.mark.asyncio
async def test_invented_section_keys_are_dropped() -> None:
    """编出来的章节键会让批准时找不到目标章节，必须在入库前挡掉。"""
    runner = _Runner(
        {"proposals": [_diagram_proposal(target_section_key="conclusions_that_do_not_exist")]}
    )
    planned, generator = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=False)
    assert planned == []
    assert generator == "deterministic_fallback"


@pytest.mark.asyncio
async def test_dangling_edges_are_dropped_instead_of_killing_the_proposal() -> None:
    """悬空边会让 DiagramSpec 校验整条建议失败——丢边比丢整张图好。"""
    runner = _Runner(
        {
            "proposals": [
                _diagram_proposal(
                    diagram={
                        "direction": "LR",
                        "nodes": [{"id": "n1", "label": "A"}, {"id": "n2", "label": "B"}],
                        "edges": [
                            {"source": "n1", "target": "n2"},
                            {"source": "n1", "target": "ghost"},
                        ],
                    }
                )
            ]
        }
    )
    planned, _ = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=False)
    assert len(planned) == 1
    assert len(planned[0].spec["edges"]) == 1


@pytest.mark.asyncio
async def test_charts_are_never_taken_from_the_model() -> None:
    """图表的每个数字都必须能追回某份素材，模型不得凭空造数据图。"""
    runner = _Runner(
        {
            "proposals": [
                {
                    "kind": "chart",
                    "target_section_key": "method",
                    "caption": "编造的图表",
                    "alt_text": "编造的图表",
                }
            ]
        }
    )
    planned, _ = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=True)
    assert planned == []


@pytest.mark.asyncio
async def test_ai_images_respect_the_opt_in_and_the_cap() -> None:
    proposal = {
        "kind": "ai_image",
        "target_section_key": "method",
        "caption": "概念插图",
        "alt_text": "概念插图的替代文本",
        "ai_image": {
            "subject": "sequential recommendation robustness",
            "composition": "left-to-right conceptual process",
            "elements": ["interactions", "model", "ranking"],
        },
    }
    # 关闭 AI 图时一条都不提。
    blocked, _ = await plan_visuals(
        sections=SECTIONS,
        runner=_Runner({"proposals": [proposal]}),
        allow_ai_images=False,
    )
    assert blocked == []

    # 开启后也有上限——插图是唯一会花钱的一类。
    many = _Runner({"proposals": [dict(proposal) for _ in range(5)]})
    planned, _ = await plan_visuals(sections=SECTIONS, runner=many, allow_ai_images=True)
    assert len([item for item in planned if item.kind == "ai_image"]) <= MAX_AI_IMAGES


@pytest.mark.asyncio
async def test_ai_image_semantics_are_carried_into_the_spec() -> None:
    runner = _Runner(
        {
            "proposals": [
                {
                    "kind": "ai_image",
                    "target_section_key": "method",
                    "caption": "概念插图",
                    "alt_text": "概念插图的替代文本",
                    "ai_image": {
                        "subject": "sequential recommendation robustness",
                        "composition": "left-to-right conceptual process",
                        "elements": ["interactions", "ranking"],
                    },
                }
            ]
        }
    )
    planned, _ = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=True)
    spec = planned[0].spec
    # 语义层是提供商无关的描述，适配器负责转成厂商请求。
    assert spec["semantics"]["subject"] == "sequential recommendation robustness"
    assert spec["semantics"]["text_policy"] == "none"
    # prompt 仍然写满：下游（历史数据、导出）都还依赖它。
    assert "sequential recommendation robustness" in spec["prompt"]


@pytest.mark.asyncio
async def test_proposals_missing_caption_or_alt_text_are_dropped() -> None:
    """caption/alt_text 是批准接口的硬性要求，缺一条这条建议就是死的。"""
    runner = _Runner({"proposals": [_diagram_proposal(caption="", alt_text="")]})
    planned, _ = await plan_visuals(sections=SECTIONS, runner=runner, allow_ai_images=False)
    assert planned == []


def test_deterministic_fallback_respects_the_ai_image_switch() -> None:
    """AI 生图关闭时，回退路径也不能提插图建议。

    否则用户拿到的是一张**永远点不动**的卡片：生成按钮被禁用，却看不出这是
    功能没开还是坏了。规划器那条路早就按开关过滤，回退这条不能自成一套。
    """
    from paperforge_worker.pipelines.visuals import _fallback_proposals

    class _Row:
        def __init__(self, key: str, title: str) -> None:
            self.section_key = key
            self.title = title

    rows = [_Row("introduction", "引言"), _Row("method", "方法")]

    off = _fallback_proposals(rows, "根系损伤检测", allow_ai_images=False)
    assert [item.kind for item in off] == ["diagram"]

    on = _fallback_proposals(rows, "根系损伤检测", allow_ai_images=True)
    assert [item.kind for item in on] == ["diagram", "ai_image"]


def test_visual_planner_excerpt_reads_canonical_text_run_value() -> None:
    from types import SimpleNamespace

    from paperforge_worker.pipelines.visuals import _section_excerpt

    row = SimpleNamespace(
        body_ir_json={
            "blocks": [{"type": "paragraph", "runs": [{"t": "text", "v": "Canonical IR text"}]}]
        }
    )
    assert _section_excerpt(row) == "Canonical IR text"


def test_short_review_detection_suppresses_generic_visual_fallbacks() -> None:
    from paperforge_worker.pipelines.visual_planner import SectionBrief
    from paperforge_worker.pipelines.visuals import _is_short_review

    short = [SectionBrief(key="s1", title="Synthesis", excerpt="brief review text")]
    long = [SectionBrief(key="s1", title="Synthesis", excerpt="word " * 3000)]

    assert _is_short_review("review", short) is True
    assert _is_short_review("review", long) is False
    assert _is_short_review("original", short) is False
