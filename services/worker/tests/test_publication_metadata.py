from __future__ import annotations

import json
from typing import Any

from llm_runtime import LLMConfig, LLMRunner
from llm_runtime.types import LLMResponse
from paperforge_worker.pipelines.publication_metadata import (
    deterministic_publication_metadata,
    generate_publication_metadata,
)


class _StubProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[Any] = []

    def generate(self, request):
        self.requests.append(request)
        return LLMResponse(
            text=json.dumps(self.payload, ensure_ascii=False),
            model="stub-metadata-model",
            provider="stub",
            finish_reason="stop",
        )


def _runner(payload: dict[str, Any]) -> tuple[LLMRunner, _StubProvider]:
    provider = _StubProvider(payload)
    return (
        LLMRunner(
            LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
            provider=provider,
        ),
        provider,
    )


def test_deterministic_metadata_uses_only_scope_terms_present_in_abstract() -> None:
    result = deterministic_publication_metadata(
        abstract="本文综述序列推荐系统中的投毒攻击，并讨论鲁棒防御方法。",
        project_title="序列推荐安全研究",
        scope={
            "topic": "序列推荐",
            "subtopics": ["投毒攻击", "不存在于摘要的成员推断"],
            "keyword_groups": [{"name": "鲁棒防御", "keywords": ["防御方法"]}],
        },
        language="zh",
    )
    assert result.title == "序列推荐安全研究"
    assert result.keywords == ["序列推荐", "投毒攻击", "鲁棒防御", "防御方法"]
    assert "成员推断" not in result.keywords


async def test_llm_metadata_comes_from_final_abstract_and_drops_year_range_keyword() -> None:
    runner, provider = _runner(
        {
            "title": "序列推荐系统投毒攻击与防御研究综述",
            "keywords": ["2022–2026 年序列推荐系统投毒攻击研究综述", "序列推荐", "投毒攻击"],
        }
    )
    result = await generate_publication_metadata(
        abstract="本文系统梳理序列推荐中的投毒攻击及其防御方法。",
        project_title="工作题名",
        scope={"topic": "序列推荐"},
        language="zh",
        runner=runner,
    )

    assert result.title == "序列推荐系统投毒攻击与防御研究综述"
    assert result.keywords == ["序列推荐", "投毒攻击"]
    assert result.generator == "llm:stub-metadata-model"
    assert "最终摘要" in provider.requests[0].user_prompt
