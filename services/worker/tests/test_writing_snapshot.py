"""章节级快照：一整节写出来的东西，逐字比对。

单元测试断言的是「这一条规则做了它该做的」。R4/R5/R6 句级证据规则合起来会**改写
甚至清空**句子，而每一条单独看都对——上一次就是这样：跨研究小节拿不到证据台账，
`_normalize_sentences` 丢掉了模型给出的全部绑定，规则随即把非背景句全部抹白，
测试一条没红。只有把整节产物钉住才看得见。

这一层不需要 Postgres：`writing.py` 只 import `re` / `dataclasses` / `typing` /
`LLMRunner` / `paper_ir`，`write_section` 的入参全是普通 dict/set，它已经是一个
冻结输入的纯函数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from llm_runtime import LLMConfig, LLMRunner
from paperforge_worker.pipelines.writing import (
    SECTION_MAX_OUTPUT_TOKENS,
    WritingContext,
    write_section,
)
from snapshot_support import RecordedProvider, assert_matches_snapshot, prompt_key

SCENARIO = Path(__file__).parent / "snapshots" / "section_zh"

WHITELIST = {"chen2023poison", "liu2024defense", "wang2022seqrec"}

CARDS: dict[str, dict[str, Any]] = {
    "chen2023poison": {
        "title": "Poisoning Attacks on Sequential Recommenders",
        "year": 2023,
        "summary": "Injects crafted interaction sequences to shift next-item predictions.",
        "contributions": ["profile injection attack", "evaluation on three datasets"],
    },
    "liu2024defense": {
        "title": "Detecting Injected Profiles in Recommendation Logs",
        "year": 2024,
        "summary": "Flags synthetic user profiles by their interaction-entropy signature.",
        "contributions": ["entropy detector"],
    },
    "wang2022seqrec": {
        "title": "A Survey of Sequential Recommendation",
        "year": 2022,
        "summary": "Reviews sequential recommendation architectures and benchmarks.",
    },
}

SECTION: dict[str, Any] = {
    "key": "s2",
    "title": "投毒攻击对序列推荐的影响",
    "question_id": "q-1",
    "summary": "说明注入式投毒如何改变下一项预测，以及现有检测手段的边界。",
    "argument_points": [
        "注入伪造交互序列可以系统性地抬高目标物品的排名",
        "检测方法依赖交互熵特征，对低频注入不敏感",
    ],
    "cite_keys": sorted(WHITELIST),
    "target_words": 700,
    "stance_summary": "supported",
}

OUTLINE: dict[str, Any] = {
    "research_question": "投毒攻击如何影响序列推荐系统，现有防御到什么程度",
    "sections": [SECTION],
    "sub_question_bundles": [
        {
            "question_id": "q-1",
            "evidence": [
                {
                    "evidence_id": "e-1",
                    "cite_key": "chen2023poison",
                    "text": "Injecting 0.5% synthetic profiles raised the target item's "
                    "hit rate from 0.7% to 14.2% on MovieLens-1M.",
                    "grade": "A_located_structured",
                    # 真实证据单元用 `page` / `object_ref` 定位，没有 `locator` 这个键。
                    # R6 要求数字句的证据单元至少有其中之一，否则整句从正文里删掉。
                    # e-1 只给 object_ref、e-2 只给 page：两个字段各自承重，
                    # 任一半失效都会让对应段落的数字句从快照里消失。
                    "object_ref": "table:3",
                },
                {
                    "evidence_id": "e-2",
                    "cite_key": "liu2024defense",
                    "text": "The entropy detector recalls 91% of injected profiles at a 3% "
                    "false-positive rate, but recall falls to 38% below a 0.2% injection ratio.",
                    "grade": "A_located_structured",
                    "page": 5,
                },
                {
                    "evidence_id": "e-3",
                    "cite_key": "wang2022seqrec",
                    "text": "Sequential recommenders are typically evaluated with "
                    "leave-one-out splits over user interaction histories.",
                    "grade": "B_located_prose",
                },
            ],
        }
    ],
}


def _runner() -> LLMRunner:
    provider = RecordedProvider.from_file(SCENARIO / "calls.jsonl")
    return LLMRunner(
        LLMConfig(provider="recorded", role_models={"writer": "deepseek-v4-pro"}),
        provider=provider,
    )


@pytest.mark.skipif(
    not (SCENARIO / "calls.jsonl").exists(),
    reason="no recording yet; run scripts/record_writing_snapshot.py",
)
async def test_a_written_section_matches_its_snapshot() -> None:
    draft = await write_section(
        section=SECTION,
        cards=CARDS,
        whitelist=set(WHITELIST),
        context=WritingContext(outline=OUTLINE, language="zh", paper_type="review"),
        runner=_runner(),
    )

    assert_matches_snapshot(_render(draft), "section_zh/expected.md")


def _render(draft: Any) -> str:
    """把草稿摊平成可读的 diff 目标。

    句子和它的 cite_keys / evidence_ids 一起打印：一条规则把绑定清空时，diff 要
    直接指出是哪一句，而不是只显示段落长度变了。
    """
    lines = [f"# {draft.title}", f"key={draft.section_key} level={draft.level}", ""]
    for index, paragraph in enumerate(draft.paragraphs, start=1):
        lines.append(f"## paragraph {index}")
        for sentence in paragraph.get("sentences") or []:
            lines.append(f"- {sentence.get('text', '')}")
            lines.append(f"  cite_keys={sorted(sentence.get('cite_keys') or [])}")
            lines.append(f"  evidence_ids={sorted(sentence.get('evidence_ids') or [])}")
            if sentence.get("downgraded_reason"):
                lines.append(f"  downgraded={sentence['downgraded_reason']}")
        lines.append("")
    lines.append(f"word_count={draft.word_count}")
    lines.append(f"citation_warnings={json.dumps(draft.citation_warnings, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def test_the_recording_key_matches_the_ledger_digest() -> None:
    """回放键与 `llm_call_log.prompt_sha256` 同口径，一次真实运行才能直接对上录制。"""
    from llm_runtime.runner import _prompt_digest

    digest, _ = _prompt_digest("sys", "user")
    assert prompt_key("writer", "sys", "user") == f"writer:{digest}"


def test_an_unrecorded_prompt_fails_loudly_instead_of_being_invented() -> None:
    """替身编一个「看起来对」的回复，等于把提示词漂移一起签收了。"""
    from llm_runtime import LLMRequest
    from snapshot_support import MissingRecording

    provider = RecordedProvider({})
    with pytest.raises(MissingRecording):
        provider.generate(
            LLMRequest(
                system_prompt="s",
                user_prompt="u",
                model="m",
                max_output_tokens=SECTION_MAX_OUTPUT_TOKENS,
                metadata={"role": "writer"},
            )
        )
