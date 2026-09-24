import uuid

import pytest
from db import create_project, create_user
from db.models.library import EvidenceUnit, LibraryEntry, ScholarlyWork
from db.models.retrieval import EvidenceEmbedding
from retrieval import models
from retrieval.ranking import cosine, lexical_rank, rrf
from retrieval.search import content_hash, search


def test_bilingual_lexical_and_rrf_deterministic():
    assert lexical_rank("证据检索", {"a": "基于证据检索的研究", "b": "完全不相关"})[0][0] == "a"
    assert rrf([("a", 1), ("b", 0.9)], [("b", 1)])[0][0] == "b"
    with pytest.raises(ValueError):
        cosine([1], [1, 2])


async def test_filter_private_unselected_retracted_and_stale_vectors(session):
    user = await create_user(
        session, email=f"{uuid.uuid4()}@example.test", password_hash="!", verified=True
    )
    projects = [
        await create_project(
            session, title="test", paper_type="review", language="en", owner_id=user.id
        )
        for _ in range(2)
    ]
    work = ScholarlyWork(canonical_title="Retrieval")
    session.add(work)
    await session.flush()
    session.add_all(
        [
            LibraryEntry(project_id=p.id, work_id=work.id, status="selected", added_via="manual")
            for p in projects
        ]
    )
    units = [
        EvidenceUnit(
            work_id=work.id,
            project_id=p.id,
            kind="finding",
            grade="B_located_prose",
            text="retrieval evidence " + str(i),
            text_hash=str(i) * 64,
        )
        for i, p in enumerate(projects)
    ]
    session.add_all(units)
    await session.flush()
    session.add(
        EvidenceEmbedding(
            project_id=projects[0].id,
            evidence_id=units[0].id,
            model=models.MODEL,
            content_hash=content_hash(units[0].text),
            embedding=[1.0, 0.0],
        )
    )
    await session.flush()
    result = await search(session, projects[0].id, "retrieval", embed=lambda _: [[1.0, 0.0]])
    assert [r["evidence_id"] for r in result["results"]] == [str(units[0].id)]
    assert result["indexed_count"] == 1
    assert result["results"][0]["verification_status"] == "not_assessed"
    def unavailable(_):
        raise LookupError("simulated model backend failure")

    fallback = await search(session, projects[0].id, "retrieval", embed=unavailable)
    assert fallback["warnings"] == ["LookupError:embedding_unavailable"]
    assert fallback["results"][0]["evidence_id"] == str(units[0].id)
    units[0].text = "updated retrieval text"
    await session.flush()
    stale = await search(session, projects[0].id, "retrieval", embed=lambda _: [[1.0, 0.0]])
    assert stale["indexed_count"] == 0
    work.is_retracted = True
    await session.flush()
    assert (await search(session, projects[0].id, "retrieval"))["results"] == []
