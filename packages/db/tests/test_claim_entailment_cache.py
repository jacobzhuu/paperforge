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
