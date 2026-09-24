"""Durable semantic-verdict cache contracts (real PostgreSQL)."""

from __future__ import annotations

import uuid

import pytest
from db import get_claim_entailment_cache, store_claim_entailment_cache


def _entry(**overrides):
    return {
        "cache_key": "a" * 64,
        "claim_hash": "b" * 64,
        "evidence_hash": "c" * 64,
        "claim_kind": "effect",
        "verifier_version": "claim_evidence_v1",
        "model": "deepseek-v4-flash",
        "verdict": "supported",
        "confidence": 0.97,
        "reason": "The located excerpt directly entails the complete claim.",
        **overrides,
    }


async def test_project_cache_does_not_read_legacy_or_other_projects_and_cascades(session):
    from db import create_project, create_user
    from db.models.paper import ClaimEntailmentCache, PaperProject
    from sqlalchemy import delete, select

    user = await create_user(
        session, email=f"{uuid.uuid4()}@example.test", password_hash="!test", verified=True
    )
    projects = [
        await create_project(
            session, title=f"P{i}", paper_type="review", language="en", owner_id=user.id
        )
        for i in range(2)
    ]
    key = "a" * 64
    await store_claim_entailment_cache(session, [_entry()])
    assert await get_claim_entailment_cache(session, {key}, project_id=projects[0].id) == {}
    await store_claim_entailment_cache(session, [_entry()], project_id=projects[0].id)
    assert key in await get_claim_entailment_cache(session, {key}, project_id=projects[0].id)
    assert await get_claim_entailment_cache(session, {key}, project_id=projects[1].id) == {}
    deleted_id = projects[0].id
    await session.execute(delete(PaperProject).where(PaperProject.id == deleted_id))
    assert not list(
        (
            await session.scalars(
                select(ClaimEntailmentCache).where(ClaimEntailmentCache.project_id == deleted_id)
            )
        ).all()
    )
    assert key in await get_claim_entailment_cache(session, {key})  # draining legacy namespace


@pytest.mark.asyncio
async def test_cache_survives_sessions_and_is_immutable_on_conflict(session_factory):
    async with session_factory() as session:
        assert await store_claim_entailment_cache(session, [_entry()]) == 1
        await session.commit()

    async with session_factory() as session:
        cached = await get_claim_entailment_cache(session, {"a" * 64})
        assert cached["a" * 64]["verdict"] == "supported"
        assert cached["a" * 64]["cache_scope"] == "persistent"
        assert cached["a" * 64]["model"] == "deepseek-v4-flash"
        assert (
            await store_claim_entailment_cache(
                session,
                [_entry(verdict="unsupported", confidence=0.99)],
            )
            == 0
        )
        await session.commit()

    async with session_factory() as session:
        cached = await get_claim_entailment_cache(session, {"a" * 64})
        assert cached["a" * 64]["verdict"] == "supported"


@pytest.mark.asyncio
async def test_empty_cache_operations_are_noops(session):
    assert await get_claim_entailment_cache(session, set()) == {}
    assert await store_claim_entailment_cache(session, []) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "banana"},
        {"verdict": ""},
        {"confidence": float("nan")},
        {"confidence": None},
        {"confidence": "not-a-number"},
        {"claim_kind": "k" * 40},
        {"verifier_version": "v" * 40},
        {"reason": ""},
    ],
    ids=[
        "verdict-outside-closed-set",
        "verdict-empty",
        "confidence-nan",
        "confidence-none",
        "confidence-unparseable",
        "claim-kind-wider-than-column",
        "verifier-version-wider-than-column",
        "reason-empty",
    ],
)
async def test_malformed_verdicts_are_dropped_without_losing_the_batch(session, bad):
    """Rows are immutable and have no TTL, so a bad one must never be written — nor take the
    batch down with it: an oversized value raises DataError for the whole multi-row INSERT."""
    good = _entry(cache_key="d" * 64)

    stored = await store_claim_entailment_cache(
        session,
        [_entry(cache_key="e" * 64, **bad), good],
    )

    assert stored == 1
    cached = await get_claim_entailment_cache(session, {"d" * 64, "e" * 64})
    assert set(cached) == {"d" * 64}


@pytest.mark.asyncio
async def test_oversized_model_is_clipped_instead_of_failing_the_insert(session):
    """cache_key already hashes the full model string, so clipping the readable copy to its
    column width keeps the row unambiguous — and keeps one long model name from raising
    DataError and discarding every verdict in the batch."""
    assert (
        await store_claim_entailment_cache(session, [_entry(cache_key="1" * 64, model="m" * 200)])
        == 1
    )

    cached = await get_claim_entailment_cache(session, {"1" * 64})
    assert cached["1" * 64]["model"] == "m" * 128


@pytest.mark.asyncio
async def test_out_of_range_confidence_is_clamped_rather_than_stored_raw(session):
    await store_claim_entailment_cache(
        session,
        [
            _entry(cache_key="f" * 64, confidence=42.0),
            _entry(cache_key="0" * 64, confidence=-3.0),
        ],
    )

    cached = await get_claim_entailment_cache(session, {"f" * 64, "0" * 64})
    assert cached["f" * 64]["confidence"] == 1.0
    assert cached["0" * 64]["confidence"] == 0.0


async def _project_with_anchor(session, *, claim_hash: str, evidence_hash: str | None):
    """Minimal project → document → section → report → anchor chain."""
    from db import create_project, create_user
    from db.models.paper import (
        ClaimEvidenceAnchor,
        PaperDocument,
        PaperSection,
        QualityReportRecord,
    )

    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    project = await create_project(session, title="Purge", paper_type="review", owner_id=owner.id)
    document = PaperDocument(project_id=project.id, version=1, status="draft")
    session.add(document)
    await session.flush()
    section = PaperSection(
        document_id=document.id,
        section_key="introduction",
        order_no=1,
        title="Introduction",
        status="generated",
    )
    session.add(section)
    await session.flush()
    report = QualityReportRecord(
        project_id=project.id,
        document_id=document.id,
        document_version=1,
        paper_snapshot_hash=f"snapshot-{uuid.uuid4()}",
        quality_profile="scholarly",
        review_style="narrative",
        readiness_status="needs_revision",
    )
    session.add(report)
    await session.flush()
    session.add(
        ClaimEvidenceAnchor(
            quality_report_id=report.id,
            project_id=project.id,
            document_id=document.id,
            section_id=section.id,
            section_key=section.section_key,
            claim_hash=claim_hash,
            claim_text="A core claim",
            claim_kind="factual",
            is_core=True,
            cite_key="ref2025",
            source_key="cite:ref2025",
            source_kind="fulltext",
            evidence_excerpt="The exact supporting passage.",
            evidence_hash=evidence_hash,
            support_status="insufficient_support",
            manual_status="unreviewed",
        )
    )
    await session.flush()
    return project


@pytest.mark.asyncio
async def test_purging_a_project_removes_the_verdicts_only_its_claims_reached(session):
    """The cache has no project_id, so CASCADE cannot reach it — but `reason` is model-authored
    prose paraphrasing the manuscript claim, so a purge must not leave it behind."""
    from db import purge_project

    claim, evidence = "purge-claim", "purge-evidence"
    project = await _project_with_anchor(session, claim_hash=claim, evidence_hash=evidence)
    await store_claim_entailment_cache(
        session,
        [_entry(cache_key="2" * 64, claim_hash=claim, evidence_hash=evidence)],
    )

    await purge_project(session, project)

    assert await get_claim_entailment_cache(session, {"2" * 64}) == {}


@pytest.mark.asyncio
async def test_a_verdict_another_project_still_anchors_survives_the_purge(session):
    """The cache is shared by design: a pair another project still holds must not be collateral."""
    from db import purge_project

    claim, evidence = "shared-claim", "shared-evidence"
    purged = await _project_with_anchor(session, claim_hash=claim, evidence_hash=evidence)
    await _project_with_anchor(session, claim_hash=claim, evidence_hash=evidence)
    await store_claim_entailment_cache(
        session,
        [_entry(cache_key="3" * 64, claim_hash=claim, evidence_hash=evidence)],
    )

    await purge_project(session, purged)

    assert set(await get_claim_entailment_cache(session, {"3" * 64})) == {"3" * 64}


@pytest.mark.asyncio
async def test_a_null_evidence_hash_elsewhere_cannot_block_the_purge(session):
    """Guards the NOT EXISTS in `_purge_orphaned_claim_entailment_cache` against a NOT IN rewrite.

    `claim_evidence_anchor.evidence_hash` is nullable. Postgres row-constructor comparison
    short-circuits to FALSE when any element differs, so a NULL only propagates when the other
    element matches — i.e. when a surviving project holds an anchor for the SAME claim whose
    excerpt was never located. Under `NOT IN` that single NULL makes the predicate UNKNOWN for
    every row and the purge silently deletes nothing.
    """
    from db import purge_project

    claim, evidence = "nullsafe-claim", "nullsafe-evidence"
    purged = await _project_with_anchor(session, claim_hash=claim, evidence_hash=evidence)
    # Same claim text in a surviving project, but its anchor located no excerpt.
    await _project_with_anchor(session, claim_hash=claim, evidence_hash=None)
    await store_claim_entailment_cache(
        session,
        [_entry(cache_key="9" * 64, claim_hash=claim, evidence_hash=evidence)],
    )

    await purge_project(session, purged)

    assert await get_claim_entailment_cache(session, {"9" * 64}) == {}
