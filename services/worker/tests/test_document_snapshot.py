"""整篇手稿快照：从大纲到 Markdown，一次跑完，逐字比对。

章节级快照（``test_writing_snapshot``）钉住的是一节内部的句级规则。这一层钉的是
它们**装配起来**之后的产物：章节顺序、框架章节拿到的引用、跨章的引用去重、
参考文献段——以及 ``build_markdown``，仓库里此前**没有任何测试调用过**它，
而它正是用户在预览里看到的东西。

需要真实 Postgres（``write_document`` 有约二十处 DB 调用），不需要 Redis、
对象存储或 HTTP：``job_id=None`` 时 ``emit`` 只打日志。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from db import create_outline, create_project, create_user, upsert_entry, upsert_work
from llm_runtime import LLMConfig, LLMRunner
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.document import build_markdown, write_document
from snapshot_support import RecordedProvider, assert_matches_snapshot
from test_document_persistence import _Candidate, _context

SCENARIO = Path(__file__).parent / "snapshots" / "document_en"

CITE_KEY = "lovelace2025poison"

#: 两节正文各绑一个子问题，证据单元用真实字段（``page`` / ``object_ref``）定位，
#: 否则 R6 会把数字句从正文里删掉——那是章节级快照已经覆盖过的事，这一层要看的是
#: 装配，不该再被同一条规则支配。
OUTLINE_TREE: dict[str, Any] = {
    "topic": "poisoning attacks on sequential recommenders",
    "language": "en",
    "research_question": "How do poisoning attacks affect sequential recommenders?",
    "sections": [
        {"key": "abstract", "kind": "frame", "level": 1, "title": "Abstract"},
        {
            "key": "s1",
            "kind": "body",
            "level": 1,
            "title": "Attack Objectives",
            "question_id": "q-1",
            "summary": "What attackers try to achieve and at what cost.",
            "argument_points": ["a small injection budget already moves target rankings"],
            "cite_keys": [CITE_KEY],
            "target_words": 300,
            "stance_summary": "supported",
        },
        {
            "key": "s2",
            "kind": "body",
            "level": 1,
            "title": "Detection Limits",
            "question_id": "q-2",
            "summary": "Where entropy-based detection stops working.",
            "argument_points": ["detection recall collapses below a low injection ratio"],
            "cite_keys": [CITE_KEY],
            "target_words": 300,
            "stance_summary": "supported",
        },
    ],
    "sub_question_bundles": [
        {
            "question_id": "q-1",
            "evidence": [
                {
                    "evidence_id": "e-1",
                    "cite_key": CITE_KEY,
                    "text": "Injecting 0.5% synthetic profiles raised the target item's hit "
                    "rate from 0.7% to 14.2% on MovieLens-1M.",
                    "grade": "A_located_structured",
                    "object_ref": "table:3",
                }
            ],
        },
        {
            "question_id": "q-2",
            "evidence": [
                {
                    "evidence_id": "e-2",
                    "cite_key": CITE_KEY,
                    "text": "The entropy detector recalls 91% of injected profiles at a 3% "
                    "false-positive rate, falling to 38% below a 0.2% injection ratio.",
                    "grade": "A_located_structured",
                    "page": 5,
                }
            ],
        },
    ],
}


async def _seed(session_factory) -> uuid.UUID:
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Poisoning attacks on sequential recommenders",
            paper_type="review",
            language="en",
            topic="poisoning",
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
        entry.bibtex_key = CITE_KEY
        await create_outline(session, project_id=project.id, tree=OUTLINE_TREE)
        await session.commit()
        return project.id


def _recorded_runner() -> LLMRunner:
    provider = RecordedProvider.from_file(SCENARIO / "calls.jsonl")
    return LLMRunner(
        LLMConfig(
            provider="recorded",
            role_models={"writer": "deepseek-v4-pro", "polisher": "deepseek-v4-pro"},
        ),
        provider=provider,
    )


@pytest.mark.skipif(
    not (SCENARIO / "calls.jsonl").exists(),
    reason="no recording yet; run scripts/record_writing_snapshot.py document_en",
)
async def test_a_whole_manuscript_matches_its_snapshot(session_factory, monkeypatch) -> None:
    project_id = await _seed(session_factory)
    context = _context(project_id, session_factory)
    # `JobContext.llm_runner` 每次都新建一个带记账回调的 runner，没有自己的注入点，
    # 所以只能在类上换掉它（先例：test_write_recovery.py 的恢复用例）。
    monkeypatch.setattr(JobContext, "llm_runner", lambda self: _recorded_runner())
    # This archived recording predates full-body frame context. Keep its exact
    # prompt contract to test IR assembly without inventing a new model response.
    # V2 frame input and serial/parallel identity are covered in test_agent_writing.
    from paperforge_worker.pipelines.writing import WritingContext

    monkeypatch.setattr(WritingContext, "section_summary", WritingContext.preceding_summary)

    outcome = await write_document(
        context,
        language="en",
        title="Poisoning attacks on sequential recommenders",
        paper_type="review",
        # 连贯性 pass 每节还要再问一次模型；这一层考的是装配，不是润色。
        coherence=False,
    )

    assert outcome.section_count > 0
    markdown = await build_markdown(context)
    assert_matches_snapshot(_normalize(markdown), "document_en/expected.md")


def _normalize(markdown: str) -> str:
    """抹掉每次运行都会变的东西，其余逐字比对。

    只归一化真正非确定的部分（UUID）。日期、版本号这类一旦出现也应该在 diff 里
    被看见，而不是被悄悄抹平。
    """
    import re

    return re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", markdown
    )
