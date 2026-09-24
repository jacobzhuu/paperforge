"""被规则删掉的句子必须留下痕迹，并且跨过 Draft→IR 那道坎。

此前 ``enforce_sentence_evidence_rules`` 确实把弃用句收进
``paragraph["downgraded_sentences"]``，但 ``to_ir_section()`` 只读
``paragraph["sentences"]``——线索死在草稿变 IR 的那一步。生产库 434 个已交付
章节里，**没有一个**带删除记录，所以「这篇稿子删了几句、为什么删」这个问题
在系统里根本问不出口。资产接地规则更彻底：直接 ``continue``，连收都没收。
"""

from __future__ import annotations

import uuid
from typing import Any

from paperforge_worker.pipelines.writing import (
    SectionDraft,
    enforce_sentence_evidence_rules,
    enforce_sentence_grounding_rules,
    sentence_downgrade_events,
)


def _unit(**fields: Any) -> dict[str, Any]:
    base = {
        "evidence_id": "e-1",
        "cite_key": "k1",
        "grade": "B_located_prose",
        "page": None,
        "object_ref": None,
        "section_path": None,
        "paragraph_index": None,
    }
    return {**base, **fields}


NUMERIC = "The hit rate rose from 0.7% to 14.2% under a 0.5% injection ratio."


def _paragraphs(text: str = NUMERIC, evidence_ids: list[str] | None = None):
    return [
        {
            "sentences": [
                {
                    "text": text,
                    "cite_keys": ["k1"],
                    "evidence_ids": evidence_ids if evidence_ids is not None else ["e-1"],
                }
            ]
        }
    ]


def _draft(paragraphs) -> SectionDraft:
    return SectionDraft(section_key="s2", title="t", paragraphs=paragraphs)


# --- 捕获：清空之前把原句留下 --------------------------------------------------


def test_the_original_sentence_survives_the_rule_that_blanks_it():
    """降级分支紧接着就清空 text/cite_keys/evidence_ids，副本必须在那之前取。"""
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(), evidence_by_id={"e-1": _unit()}, language="en"
    )

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["rule"] == "R6_locator_missing"
    assert event["outcome"] == "removed"
    assert event["text"] == NUMERIC, "原句必须原样留下，而不是清空后的空壳"
    assert event["cite_keys"] == ["k1"]
    assert event["evidence_ids"] == ["e-1"]


def test_the_event_records_where_the_sentence_was():
    paragraphs = [
        {"sentences": [{"text": "Background prose.", "cite_keys": [], "evidence_ids": []}]},
        _paragraphs()[0],
    ]
    paragraphs = enforce_sentence_evidence_rules(
        paragraphs, evidence_by_id={"e-1": _unit()}, language="en"
    )

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["section_key"] == "s2"
    assert event["paragraph_index"] == 2, "段落位置要能定位到第几段"
    assert event["sentence_index"] == 1


def test_the_event_records_the_locator_status_at_deletion_time():
    """删的时候这句绑的证据到底能不能查证——事后无法重建，只能当场记。"""
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(), evidence_by_id={"e-1": _unit(grade="D_abstract_only")}, language="en"
    )

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["locator_status"] == "unlocated"
    assert event["locators"] == [
        {"evidence_id": "e-1", "grade": "D_abstract_only", "located": False, "display": None}
    ]


def test_a_sentence_with_no_evidence_at_all_is_marked_as_such():
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(evidence_ids=[]), evidence_by_id={}, language="en"
    )

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["locator_status"] == "no_evidence"
    assert event["locators"] == []


def test_a_located_unit_that_failed_a_different_rule_is_not_blamed_on_the_locator():
    """R4（证据等级不够）与 R6（无从定位）必须分得开。"""
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(),
        evidence_by_id={"e-1": _unit(grade="C_fulltext_unlocated", section_path="Results")},
        language="en",
    )

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["rule"] == "R4_grade_missing"
    assert event["locator_status"] == "located"
    assert event["locators"][0]["display"] == "§Results"


def test_a_rewritten_sentence_is_recorded_as_rewritten_not_removed():
    """R4 归因是改写：句子还在正文里，但同样是规则动过的，要能查。"""
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(), evidence_by_id={"e-1": _unit(grade="D_abstract_only")}, language="en"
    )
    # 摘要级证据 + 未超归因配额 -> 改写
    kept = [s for p in paragraphs for s in p.get("sentences") or []]
    assert len(kept) == 1 and kept[0]["text"].startswith("The cited work reports")

    [event] = sentence_downgrade_events(_draft(paragraphs))
    assert event["rule"] == "R4_abstract_attribution"
    assert event["outcome"] == "rewritten"
    assert event["text"] == NUMERIC, "记的仍是模型原本写的那句"


def test_the_asset_grounding_rule_no_longer_drops_sentences_on_the_floor():
    """此前这条规则直接 `continue`，句子连 downgraded_sentences 都没进过。"""
    paragraphs = enforce_sentence_grounding_rules(
        _paragraphs(),
        assets_by_ref={},
        require_grounding=True,
    )

    events = sentence_downgrade_events(_draft(paragraphs))
    assert [e["rule"] for e in events] == ["asset_grounding_missing"]
    assert events[0]["text"] == NUMERIC


def test_an_untouched_draft_produces_no_events():
    draft = _draft([{"sentences": [{"text": "Fine.", "cite_keys": [], "evidence_ids": []}]}])
    assert sentence_downgrade_events(draft) == []


# --- 跨 Draft→IR：这正是它此前消失的那一步 ------------------------------------


def test_to_ir_section_still_drops_the_trail_so_it_is_read_from_the_draft():
    """钉住这个事实本身。

    IR 是渲染器消费的东西，审计线索不该混进去；解法是在**草稿**上取，与正文
    同一事务落库。这条用例的作用是：若哪天有人改成把线索塞进 IR，这里会提醒
    落库路径也要跟着改。
    """
    paragraphs = enforce_sentence_evidence_rules(
        _paragraphs(), evidence_by_id={"e-1": _unit()}, language="en"
    )
    draft = _draft(paragraphs)
    assert sentence_downgrade_events(draft), "草稿上取得到"

    ir = draft.to_ir_section().model_dump(mode="json")
    assert "downgraded" not in str(ir), "IR 里没有，所以必须从草稿取"


# --- 落库与汇总（真实 Postgres） ------------------------------------------------


async def _seed_document(session_factory):
    from db import create_document, create_outline, create_project, create_user

    async with session_factory() as session:
        owner = await create_user(
            session,
            email=f"{uuid.uuid4()}@example.test",
            password_hash="!test-only",
            verified=True,
        )
        project = await create_project(
            session, title="downgrade trail", paper_type="review", owner_id=owner.id
        )
        outline = await create_outline(
            session, project_id=project.id, tree={"sections": [{"key": "s1"}, {"key": "s2"}]}
        )
        document = await create_document(
            session, project_id=project.id, outline_id=outline.id
        )
        await session.commit()
        return project.id, document.id


async def test_events_persist_and_aggregate_by_section_and_document(session_factory):
    from db import (
        replace_sentence_downgrades,
        sentence_downgrade_summary,
        upsert_section,
    )

    project_id, document_id = await _seed_document(session_factory)

    async with session_factory() as session:
        rows = {}
        for order_no, key in enumerate(["s1", "s2"]):
            rows[key] = await upsert_section(
                session, document_id=document_id, section_key=key,
                title=key, order_no=order_no, body_ir={"key": key, "title": key, "blocks": []},
                cite_keys=[], asset_refs=[], status="generated",
            )
        await replace_sentence_downgrades(
            session, project_id=project_id, job_id=None, document_id=document_id,
            section_id=rows["s1"].id, section_key="s1",
            events=[
                {"paragraph_index": 1, "sentence_index": 1, "rule": "R6_locator_missing",
                 "outcome": "removed", "text": "a", "cite_keys": ["k"], "evidence_ids": ["e"],
                 "locator_status": "unlocated", "locators": []},
                {"paragraph_index": 2, "sentence_index": 1, "rule": "R6_locator_missing",
                 "outcome": "removed", "text": "b", "cite_keys": [], "evidence_ids": [],
                 "locator_status": "no_evidence", "locators": []},
            ],
        )
        await replace_sentence_downgrades(
            session, project_id=project_id, job_id=None, document_id=document_id,
            section_id=rows["s2"].id, section_key="s2",
            events=[
                {"paragraph_index": 1, "sentence_index": 1, "rule": "R4_abstract_attribution",
                 "outcome": "rewritten", "text": "c", "cite_keys": [], "evidence_ids": [],
                 "locator_status": "unlocated", "locators": []},
            ],
        )
        await session.commit()

    async with session_factory() as session:
        summary = await sentence_downgrade_summary(session, document_id=document_id)

    assert summary["total"] == 3
    assert summary["removed"] == 2
    assert summary["rewritten"] == 1
    assert summary["by_rule"] == {"R6_locator_missing": 2, "R4_abstract_attribution": 1}
    assert summary["by_locator_status"] == {"unlocated": 2, "no_evidence": 1}
    by_section = {item["section_key"]: item for item in summary["by_section"]}
    assert by_section["s1"]["total"] == 2
    assert by_section["s2"]["by_rule"] == {"R4_abstract_attribution": 1}


async def test_rewriting_a_section_replaces_its_events_instead_of_adding_to_them(session_factory):
    """修复轮会重写章节。计数不能随轮次累加。"""
    from db import replace_sentence_downgrades, sentence_downgrade_summary, upsert_section

    project_id, document_id = await _seed_document(session_factory)
    event = {
        "paragraph_index": 1, "sentence_index": 1, "rule": "R6_locator_missing",
        "outcome": "removed", "text": "a", "cite_keys": [], "evidence_ids": [],
        "locator_status": "unlocated", "locators": [],
    }
    async with session_factory() as session:
        row = await upsert_section(
            session, document_id=document_id, section_key="s1", title="s1", order_no=0,
            body_ir={"key": "s1", "title": "s1", "blocks": []}, cite_keys=[], asset_refs=[],
            status="generated",
        )
        for _ in range(3):  # three repair rounds writing the same section
            await replace_sentence_downgrades(
                session, project_id=project_id, job_id=None, document_id=document_id,
                section_id=row.id, section_key="s1", events=[event],
            )
        await session.commit()

    async with session_factory() as session:
        summary = await sentence_downgrade_summary(session, document_id=document_id)
    assert summary["total"] == 1, "三轮重写之后仍是一条，不是三条"


async def test_a_document_with_no_downgrades_summarises_as_zero(session_factory):
    from db import sentence_downgrade_summary

    _project_id, document_id = await _seed_document(session_factory)
    async with session_factory() as session:
        summary = await sentence_downgrade_summary(session, document_id=document_id)
    assert summary["total"] == 0
    assert summary["by_rule"] == {}
    assert summary["by_section"] == []
