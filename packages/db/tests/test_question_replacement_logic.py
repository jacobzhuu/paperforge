from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from db.models.paper import ResearchQuestion
from db.repositories.questions import replace_research_questions


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _RecordingSession:
    """够用的假 session：记录 delete/clear 语句，不需要真数据库。"""

    def __init__(self, rows, manual_link=None):
        self._rows = rows
        self._manual_link = manual_link
        self.added = []
        self.executed = 0

    async def scalars(self, statement):
        return _ScalarRows(self._rows)

    async def scalar(self, statement):
        return self._manual_link

    async def execute(self, statement):
        self.executed += 1
        return SimpleNamespace(rowcount=0)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


async def test_identical_question_tree_reuses_ids_and_preserves_answer_status() -> None:
    project_id = uuid4()
    core = ResearchQuestion(
        id=uuid4(),
        project_id=project_id,
        text="Core question",
        kind="core",
        order_index=0,
        answer_status="partial",
    )
    sub = ResearchQuestion(
        id=uuid4(),
        project_id=project_id,
        parent_id=core.id,
        text="Sub question",
        kind="sub",
        order_index=1,
        answer_status="answered",
    )

    class _Session:
        async def scalars(self, statement):
            return _ScalarRows([core, sub])

        async def flush(self):
            return None

    rows = await replace_research_questions(
        _Session(),  # type: ignore[arg-type]
        project_id=project_id,
        core_text="  Core   question ",
        sub_questions=[
            {
                "text": "Sub question",
                "search_query": "sub evidence",
                "expected_evidence_kinds": ["experimental_fact"],
            }
        ],
        generator="llm:new",
    )

    assert [row.id for row in rows] == [core.id, sub.id]
    assert [row.answer_status for row in rows] == ["partial", "answered"]
    assert sub.search_query == "sub evidence"


async def test_changed_tree_refuses_to_delete_manual_evidence_links() -> None:
    project_id = uuid4()
    core = ResearchQuestion(
        id=uuid4(),
        project_id=project_id,
        text="Old core",
        kind="core",
        order_index=0,
        answer_status="partial",
    )

    class _Session:
        async def scalars(self, statement):
            return _ScalarRows([core])

        async def scalar(self, statement):
            return uuid4()

    with pytest.raises(
        ValueError,
        match="question_decomposition_conflicts_with_manual_evidence_links",
    ):
        await replace_research_questions(
            _Session(),  # type: ignore[arg-type]
            project_id=project_id,
            core_text="New core",
            sub_questions=[],
        )


def _tree(project_id, sub_texts, *, locked_indices=()):
    core = ResearchQuestion(
        id=uuid4(),
        project_id=project_id,
        text="Core question",
        kind="core",
        order_index=0,
        answer_status="partial",
        locked=False,
        origin="auto",
    )
    subs = [
        ResearchQuestion(
            id=uuid4(),
            project_id=project_id,
            parent_id=core.id,
            text=text,
            kind="sub",
            order_index=index + 1,
            answer_status="answered",
            locked=index in locked_indices,
            origin="user" if index in locked_indices else "auto",
        )
        for index, text in enumerate(sub_texts)
    ]
    return core, subs


async def test_locked_question_survives_regeneration_at_its_position() -> None:
    """用户在问题工作台改过的问题，不能被下一次 QDECOMP 悄悄删掉重建。"""
    project_id = uuid4()
    core, subs = _tree(project_id, ["Auto one", "User edited"], locked_indices={1})
    session = _RecordingSession([core, *subs])

    rows = await replace_research_questions(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        core_text="Core question",
        sub_questions=[
            {"text": "Auto one rewritten", "search_query": "one"},
            {"text": "Auto two", "search_query": "two"},
        ],
        generator="llm:new",
    )

    assert [row.id for row in rows] == [core.id, subs[0].id, subs[1].id]
    assert subs[0].text == "Auto one rewritten"
    # 锁定行保留用户文本，并吃掉这个位置上自动生成的替代问题。
    assert subs[1].text == "User edited"
    assert subs[1].origin == "user"
    assert subs[1].answer_status == "answered"


async def test_rewritten_question_loses_its_stale_automatic_links() -> None:
    project_id = uuid4()
    core, subs = _tree(project_id, ["Old question"])
    session = _RecordingSession([core, *subs])

    await replace_research_questions(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        core_text="Core question",
        sub_questions=[{"text": "Completely different question", "search_query": "q"}],
        generator="llm:new",
    )

    assert subs[0].answer_status == "insufficient_evidence"
    # 清自动链接走的是 execute(delete(...))。
    assert session.executed == 1


async def test_surplus_auto_questions_are_deleted_but_locked_ones_are_not() -> None:
    project_id = uuid4()
    core, subs = _tree(project_id, ["Auto one", "Auto two", "User kept"], locked_indices={2})
    session = _RecordingSession([core, *subs])

    rows = await replace_research_questions(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        core_text="Core question",
        sub_questions=[{"text": "Auto one", "search_query": "one"}],
        generator="llm:new",
    )

    assert [row.id for row in rows] == [core.id, subs[0].id, subs[2].id]
