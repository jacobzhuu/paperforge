from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from paperforge_worker.pipelines.intake import material_context, understand


def output(**changes):
    return {
        "paper_type": "original",
        "language": "en",
        "language_explicit": True,
        "summary": "根据实验材料整理研究问题",
        "next_step": "先提出研究范围，待用户确认结论",
        "scope": {
            "research_question": "Does A improve B?",
            "keyword_groups": [{"name": "method", "keywords": ["method A"]}],
        },
        **changes,
    }


def runner(value):
    return SimpleNamespace(
        agenerate_json=AsyncMock(return_value=SimpleNamespace(ok=True, value=value, model="test"))
    )


async def test_one_call_understands_explicit_language_and_plans():
    model = runner(output())
    result = await understand(
        model, topic="写一篇英文研究型论文", overrides={}, answers=[], assets=[]
    )
    assert result["language"] == "en"
    assert result["paper_type"] == "original"
    assert result["scope"]["generator"] == "llm:intake:test"
    assert result["scope"]["language"] == "en"
    model.agenerate_json.assert_awaited_once()


async def test_manual_preferences_win_and_chinese_followup_cannot_change_english():
    result = await understand(
        runner(output(language="zh", paper_type="review")),
        topic="补充需求",
        overrides={"language": "en", "paper_type": "original"},
        answers=["先整理实验结果"],
        assets=[],
    )
    assert (result["language"], result["paper_type"]) == ("en", "original")
    assert result["sources"] == {"language": "user", "paper_type": "user"}


async def test_unspecified_language_uses_chinese_and_ambiguity_has_no_fake_ready_type():
    result = await understand(
        runner(
            output(
                paper_type=None,
                language_explicit=False,
                questions=[{"question": "整理现有结果还是制定方案？"}],
            )
        ),
        topic="帮我写篇论文",
        overrides={},
        answers=[],
        assets=[],
    )
    assert result["language"] == "zh"
    assert result["sources"]["language"] == "default"
    assert result["paper_type"] is None
    assert result["questions"]


async def test_english_input_without_explicit_language_can_be_inferred():
    result = await understand(
        runner(output(language_explicit=False, language_inferred=True)),
        topic="Survey methods for measuring factual consistency in scientific writing",
        overrides={},
        answers=[],
        assets=[],
    )
    assert result["language"] == "en"
    assert result["scope"]["language"] == "en"
    assert result["sources"]["language"] == "inferred"


async def test_invalid_or_failed_model_does_not_become_successful_fallback():
    with pytest.raises(ValueError):
        await understand(runner({}), topic="test", overrides={}, answers=[], assets=[])
    model = SimpleNamespace(agenerate_json=AsyncMock(return_value=SimpleNamespace(ok=False)))
    with pytest.raises(ValueError, match="需求理解暂未完成"):
        await understand(model, topic="test", overrides={}, answers=[], assets=[])


def test_material_excerpts_are_bounded_and_not_claimed_as_full_analysis():
    assets = [
        SimpleNamespace(
            id=str(i), title=f"data{i}", kind="dataset", parsed_json={"text": "x" * 10000}
        )
        for i in range(8)
    ]
    assets.append(SimpleNamespace(id="empty", title="broken", parsed_json=None))
    excerpts, records = material_context(assets)
    assert sum(len(item["excerpt"]) for item in excerpts) <= 24000
    assert all(item["truncated"] for item in records[:8])
    assert records[7]["used"] is False
    assert records[8]["parsed"] is False


def test_parser_warning_only_is_not_parsed_or_used():
    asset = SimpleNamespace(
        id="broken",
        title="broken.pdf",
        kind="method_note",
        parsed_json={"warnings": ["parse failed"]},
    )
    excerpts, records = material_context([asset])
    assert excerpts == []
    assert records[0]["parsed"] is False
    assert records[0]["used"] is False
    assert records[0]["warnings"] == ["parse failed"]


async def test_scope_only_response_is_rejected_and_outer_contract_is_last():
    model = runner(output()["scope"])
    with pytest.raises(ValueError):
        await understand(model, topic="研究目标", overrides={}, answers=[], assets=[])
    prompt = model.agenerate_json.call_args.kwargs["system_prompt"]
    assert prompt.index("</scope_reference>") < prompt.index("最终任务与输出契约")
    assert '"required": ["summary", "next_step"]' in prompt


async def test_unresolved_direction_without_questions_is_rejected():
    with pytest.raises(ValueError):
        await understand(
            runner(output(paper_type=None, questions=[])),
            topic="帮我写论文",
            overrides={},
            answers=[],
            assets=[],
        )
