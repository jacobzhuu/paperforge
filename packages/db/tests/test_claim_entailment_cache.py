"""Durable semantic-verdict cache contracts (real PostgreSQL)."""

from __future__ import annotations

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
        assert await store_claim_entailment_cache(
            session,
            [_entry(verdict="unsupported", confidence=0.99)],
        ) == 0
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
