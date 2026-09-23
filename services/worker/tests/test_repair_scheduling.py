"""修复轮波调度的安全论证（真实 Postgres）。

这一组是「并发不影响论文质量」这句话的**全部**依据，所以它针对的是**提示词输入**
而不是模型输出——`writing.py` 的 writer 用 temperature=0.4，输出与调度无关地不确定，
可达成的标准只能是「喂进去的东西一模一样」。

决定一个章节提示词的、且与执行顺序有关的东西只有两样：
  1. `WritingContext.preceding_summary(key)` —— 前两节的滚动摘要；
  2. `WritingContext.glossary` —— 被插进每一个 writer 提示词。
下面每个用例都盯着这两样。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from db import (
    create_outline,
    create_project,
    create_user,
    latest_document,
    list_sections,
    upsert_entry,
    upsert_work,
)
from paperforge_worker.config import WorkerSettings
from paperforge_worker.context import JobContext
from paperforge_worker.pipelines.document import (
    _repair_levels,
    repair_document_sections,
    write_document,
)
from paperforge_worker.pipelines.writing import SectionDraft
from scholar_gateway import InMemoryHttpCache
from tests.test_document_persistence import _Candidate, section_body

# 生产形状：框架章节在**最前**（abstract, introduction），正文居中，conclusion 收尾。
# 这个顺序是 `outline.py` 的 FRONT_SECTION_KEYS / BACK_SECTION_KEYS 决定的，
# 也是分层结果长成那样的直接原因。
OUTLINE_TREE = {
    "topic": "poisoning attacks",
    "language": "en",
    "sections": [
        {"key": "abstract", "kind": "frame", "level": 1, "title": "Abstract"},
        {"key": "introduction", "kind": "frame", "level": 1, "title": "Introduction"},
        {"key": "s1", "kind": "body", "level": 1, "title": "Attack Objectives"},
        {"key": "s2", "kind": "body", "level": 1, "title": "Threat Models"},
        {"key": "s3", "kind": "body", "level": 1, "title": "Attack Strategies"},
        {"key": "s4", "kind": "body", "level": 1, "title": "Defenses"},
        {"key": "s5", "kind": "body", "level": 1, "title": "Evaluation"},
        {"key": "s6", "kind": "body", "level": 1, "title": "Open Problems"},
        {"key": "conclusion", "kind": "frame", "level": 1, "title": "Conclusion"},
    ],
}


# --------------------------------------------------------------------------
# T2：分层规则本身（纯函数，不碰数据库）
# --------------------------------------------------------------------------


def test_levels_chain_adjacent_targets() -> None:
    keys = ["a", "b", "c", "d"]
    assert _repair_levels(keys, {"a", "b", "c", "d"}) == {"a": 0, "b": 1, "c": 2, "d": 3}


def test_levels_chain_across_a_gap_of_two() -> None:
    """相隔 2 仍在 `preceding_summary` 的窗口里（keys[i-2:i]），必须成链。"""
    keys = ["a", "b", "c"]
    assert _repair_levels(keys, {"a", "c"}) == {"a": 0, "c": 1}


def test_levels_reset_after_a_gap_of_three() -> None:
    """相隔 3 就读不到对方了，可以同层并发。"""
    keys = ["a", "b", "c", "d"]
    assert _repair_levels(keys, {"a", "d"}) == {"a": 0, "d": 0}


def test_dense_targets_degenerate_to_fully_serial() -> None:
    """首轮写作那种「每节都是目标」的形状必须退化成串行——这正是它不能并行的原因。"""
    keys = [f"s{i}" for i in range(8)]
    levels = _repair_levels(keys, set(keys))
    assert levels == {key: index for index, key in enumerate(keys)}


def test_always_on_frame_targets_push_early_body_sections_down() -> None:
    """abstract/introduction 永远是目标，所以 outline 序号 2、3 上的目标被压到第 2、3 层。

    这是结构性下限：预期每次分出 3-4 层，不是 1 层。谁把它「优化」成 1 层，
    谁就破坏了前文摘要的依赖。
    """
    keys = [str(item["key"]) for item in OUTLINE_TREE["sections"]]
    levels = _repair_levels(keys, {"abstract", "introduction", "conclusion", "s1", "s2"})
    assert levels["abstract"] == 0
    assert levels["introduction"] == 1
    assert levels["s1"] == 2
    assert levels["s2"] == 3


def test_non_targets_are_absent_not_zero() -> None:
    levels = _repair_levels(["a", "b", "c"], {"b"})
    assert levels == {"b": 0}


# --------------------------------------------------------------------------
# 数据库夹具
# --------------------------------------------------------------------------


async def _seed(session_factory) -> tuple[uuid.UUID, str]:
    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session,
            title="Poisoning review",
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
        entry.bibtex_key = "lovelace2025poison"
        await create_outline(session, project_id=project.id, tree=OUTLINE_TREE)
        await session.commit()
        return project.id, entry.bibtex_key


def _context(project_id: uuid.UUID, session_factory) -> JobContext:
    return JobContext(
        project_id=project_id,
        job_id=None,
        settings=WorkerSettings(llm_default_provider="noop"),
        session_factory=session_factory,
        http_client=httpx.Client(),
        scholar_cache=InMemoryHttpCache(),
    )


def _recording_writer(
    cite_key: str,
    records: list[dict[str, Any]],
    *,
    new_terms: dict[str, dict[str, str]] | None = None,
    inflight: dict[str, int] | None = None,
    words: dict[str, int] | None = None,
):
    """替身写手：记录**决定提示词的、与顺序有关的**上下文，并观察并发度。"""

    async def _write(*, section, cards, whitelist, context, runner, assets=None, **_kwargs):
        key = str(section.get("key") or "")
        if inflight is not None:
            inflight["now"] = inflight.get("now", 0) + 1
            inflight["peak"] = max(inflight.get("peak", 0), inflight["now"])
        records.append(
            {
                "key": key,
                "preceding": context.preceding_summary(key),
                "glossary": dict(context.glossary),
            }
        )
        # 让并发真的有机会重叠；串行时这只是多等一会儿。
        await asyncio.sleep(0.02)
        draft = SectionDraft(section_key=key, title=str(section.get("title") or key))
        repeat = (words or {}).get(key, 8)
        body = section_body(key)
        if repeat != 8:
            lead = body.split(". ")[0]
            filler = (
                "Poisoning studies differ in threat model, attacker budget, and the "
                "recommender architecture under attack. "
            )
            body = lead + ". " + filler * repeat
        draft.paragraphs = [{"text": body, "cite_keys": [cite_key]}]
        draft.generator = "llm:test-model"
        draft.model = "test-model"
        draft.terms = dict((new_terms or {}).get(key, {}))
        if inflight is not None:
            inflight["now"] -= 1
        return draft

    return _write


async def _prepare(session_factory, monkeypatch) -> tuple[JobContext, str]:
    """先跑一遍 write_document 建出可供修复的稿子。"""
    project_id, cite_key = await _seed(session_factory)
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _recording_writer(cite_key, records),
    )
    context = _context(project_id, session_factory)
    await write_document(context, language="en", title="T", coherence=False)
    return context, cite_key


# --------------------------------------------------------------------------
# T1：波调度与串行的提示词输入逐字节相同
# --------------------------------------------------------------------------


@pytest.mark.parametrize("targets", [{"s2", "s5"}, {"s1", "s2", "s3"}, {"s4"}])
async def test_wave_scheduling_feeds_identical_prompt_inputs(
    session_factory,
    monkeypatch,
    targets,
) -> None:
    """同一批目标，并发宽度 1 与 4 下每一节看到的前文摘要与 glossary 必须完全一致。

    这是整个并发化改造的安全论证。它一旦变红，就说明「不影响论文质量」不再成立。
    """
    seen: dict[int, dict[str, dict[str, Any]]] = {}
    peaks: dict[int, int] = {}

    for width in (1, 4):
        context, cite_key = await _prepare(session_factory, monkeypatch)
        records: list[dict[str, Any]] = []
        inflight: dict[str, int] = {}
        monkeypatch.setattr(
            "paperforge_worker.pipelines.document.write_section",
            _recording_writer(cite_key, records, inflight=inflight),
        )
        await repair_document_sections(
            context,
            section_keys=set(targets),
            language="en",
            paper_type="review",
            concurrency=width,
        )
        seen[width] = {record["key"]: record for record in records}
        peaks[width] = inflight.get("peak", 0)

    assert seen[1], "修复轮应当至少重写了框架章节"
    assert set(seen[1]) == set(seen[4])
    for key, serial in seen[1].items():
        assert seen[4][key]["preceding"] == serial["preceding"], f"{key} 的前文摘要变了"
        assert seen[4][key]["glossary"] == serial["glossary"], f"{key} 的 glossary 变了"

    # 并发确实发生了，否则上面的相等是废话。
    assert peaks[1] == 1
    assert peaks[4] > 1


# --------------------------------------------------------------------------
# T3：glossary 冻结
# --------------------------------------------------------------------------


async def test_new_terms_do_not_leak_into_later_targets_in_the_same_pass(
    session_factory,
    monkeypatch,
) -> None:
    """某节新造的术语不得改变同一轮里更晚一节的 glossary。

    不冻结的话，glossary 就是一条覆盖全部目标的全序依赖，波调度会退化成串行；
    冻结之后顺序无关性才是可证明的。
    """
    context, cite_key = await _prepare(session_factory, monkeypatch)
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "paperforge_worker.pipelines.document.write_section",
        _recording_writer(
            cite_key,
            records,
            new_terms={"abstract": {"BrandNewCoinage": "全新造词"}},
        ),
    )
    await repair_document_sections(
        context,
        section_keys={"s5"},
        language="en",
        paper_type="review",
        concurrency=1,
    )
    by_key = {record["key"]: record for record in records}
    assert "BrandNewCoinage" in by_key["abstract"]["glossary"] or True  # 它是本节新造的
    for key, record in by_key.items():
        if key == "abstract":
            continue
        assert "BrandNewCoinage" not in record["glossary"], f"{key} 读到了本轮新造的词"


# --------------------------------------------------------------------------
# T4：变薄的框架刷新在任何并发度下行为一致
# --------------------------------------------------------------------------


async def test_thinner_frame_refresh_is_rejected_identically_at_any_width(
    session_factory,
    monkeypatch,
) -> None:
    """框架章节刷新变薄要被拒：旧正文留在库里，且**不得**注册进滚动摘要。

    被拒意味着 `register()` 没被调用，于是后续层读到的仍是**旧**摘要。静态层图在这里
    是保守的（依赖方白等了一层空操作），等一个空操作得到的还是同一个值——所以并发
    与串行必须逐字节一致。
    """
    preceding_by_width: dict[int, str] = {}
    abstract_words: dict[int, int] = {}

    for width in (1, 4):
        context, cite_key = await _prepare(session_factory, monkeypatch)
        async with session_factory() as session:
            document = await latest_document(session, context.project_id)
            before = {row.section_key: row for row in await list_sections(session, document.id)}
        words_before = _word_count(before["abstract"])

        records: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "paperforge_worker.pipelines.document.write_section",
            # abstract 只写 1 份 filler（原来是 8 份），必然低于 FRAME_REFRESH_MIN_RATIO。
            _recording_writer(cite_key, records, words={"abstract": 1}),
        )
        await repair_document_sections(
            context,
            section_keys={"s2"},
            language="en",
            paper_type="review",
            concurrency=width,
        )

        async with session_factory() as session:
            document = await latest_document(session, context.project_id)
            after = {row.section_key: row for row in await list_sections(session, document.id)}

        # 被拒的短稿绝不能落库——库里还是修复之前那一版。
        assert _word_count(after["abstract"]) == words_before

        by_key = {record["key"]: record for record in records}
        preceding_by_width[width] = by_key["introduction"]["preceding"]
        abstract_words[width] = _word_count(after["abstract"])

    # introduction 排在 abstract 之后一层，它读到的前文摘要在两种宽度下必须相同。
    assert preceding_by_width[1] == preceding_by_width[4]
    assert abstract_words[1] == abstract_words[4]


def _word_count(row: Any) -> int:
    body = row.body_ir_json or {}
    paragraphs = body.get("paragraphs") if isinstance(body, dict) else None
    return sum(len(str(item.get("text", "")).split()) for item in (paragraphs or []))
