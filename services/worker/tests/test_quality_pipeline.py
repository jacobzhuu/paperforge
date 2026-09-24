"""M5 测试：语义软校验、覆盖建议、质量评分。

三者都必须是**提示**而非门槛：低分引用不会被删除，覆盖不足不会阻断交付
（设计 §3.4：formal completion 的 12 项硬门槛 → 质量评分报告，仅提示）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from llm_runtime import DecisionResult, LLMConfig, LLMRunner
from llm_runtime.types import LLMResponse
from paperforge_worker.pipelines.quality import (
    CLAIM_DEMOTION_CONFIDENCE,
    CROSS_LANGUAGE_SUPPORT_CONFIDENCE,
    MAX_CLAIM_EVIDENCE_CHECKS,
    SOFT_CHECK_THRESHOLD,
    SoftCheckFinding,
    build_quality_report,
    claim_verification_cache_key,
    count_words,
    coverage_hints,
    preview_citations,
    soft_check_citations,
    verify_claim_evidence,
    verify_cross_language_claim_evidence,
    zh_language_mismatches,
)

NOW = datetime(2026, 7, 25, tzinfo=UTC)


class _Stub:
    def __init__(self, payload: Any, calls: list[Any] | None = None) -> None:
        self.payload = payload
        self.calls = calls

    def generate(self, request):
        if self.calls is not None:
            self.calls.append(request)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return LLMResponse(text=text, model="stub", provider="stub")


class _SequenceStub:
    def __init__(self, payloads: list[Any], calls: list[Any]) -> None:
        self.payloads = list(payloads)
        self.calls = calls

    def generate(self, request):
        self.calls.append(request)
        payload = self.payloads.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(text=text, model="stub", provider="stub")


def _runner(payload: Any, *, calls: list[Any] | None = None) -> LLMRunner:
    return LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
        provider=_Stub(payload, calls),
    )


def _sequence_runner(payloads: list[Any], *, calls: list[Any]) -> LLMRunner:
    return LLMRunner(
        LLMConfig(provider="openai", base_url="http://stub", api_key="k"),
        provider=_SequenceStub(payloads, calls),
    )


def _sections(**overrides: Any) -> list[dict[str, Any]]:
    base = [
        {
            "section_key": "s1",
            "title": "技术基础",
            "word_count": 1200,
            "cite_keys": ["a2020x", "b2021y"],
            "kind": "body",
        },
        {
            "section_key": "s2",
            "title": "评测方法",
            "word_count": 900,
            "cite_keys": [],
            "kind": "body",
        },
        {
            "section_key": "abstract",
            "title": "摘要",
            "word_count": 200,
            "cite_keys": [],
            "kind": "frame",
        },
    ]
    base[0].update(overrides)
    return base


# ---- 语义软校验 ----


async def test_soft_check_flags_weak_citations_without_removing_them() -> None:
    findings = await soft_check_citations(
        usages=[
            {"cite_key": "a2020x", "section_key": "s1", "context_snippet": "关于检索器的论述"},
            {"cite_key": "b2021y", "section_key": "s1", "context_snippet": "关于评测的论述"},
        ],
        abstracts={"a2020x": "本文提出稠密检索器。", "b2021y": "本文研究蛋白质折叠。"},
        runner=_runner(
            {
                "judgements": [
                    {"index": 0, "score": 0.92, "reason": "主题一致"},
                    {"index": 1, "score": 0.12, "reason": "主题不相关"},
                ]
            }
        ),
    )
    assert len(findings) == 2
    weak = [f for f in findings if f.weak]
    assert len(weak) == 1
    assert weak[0].cite_key == "b2021y"
    # 只出提示：软校验不返回任何「删除」指令。
    assert all(hasattr(f, "score") for f in findings)


async def test_soft_check_ignores_out_of_range_indexes() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a2020x", "section_key": "s1", "context_snippet": "ctx"}],
        abstracts={"a2020x": "abs"},
        runner=_runner({"judgements": [{"index": 99, "score": 0.1}]}),
    )
    assert len(findings) == 1
    assert findings[0].status == "unverified"
    assert not findings[0].weak


async def test_soft_check_without_runner_is_empty_not_failing() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a", "section_key": "s", "context_snippet": "c"}],
        abstracts={"a": "abs"},
        runner=None,
    )
    assert len(findings) == 1
    assert findings[0].status == "unverified"
    assert not findings[0].weak


async def test_soft_check_survives_unparsable_output() -> None:
    findings = await soft_check_citations(
        usages=[{"cite_key": "a", "section_key": "s", "context_snippet": "c"}],
        abstracts={"a": "abs"},
        runner=_runner("<html>not json</html>"),
    )
    assert len(findings) == 1
    assert findings[0].status == "unverified"
    assert not findings[0].weak


class _DecisionStub:
    enabled = True

    async def decide(self, *, state, questions, metadata=None):
        return DecisionResult(
            answers={
                key: {
                    "type": "score",
                    "score": 4.0 if key.endswith("0") else 1.0,
                    "confidence": 0.97,
                    "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.0, "4": 1.0},
                }
                for key in questions
            },
            model="jev-1.13.0",
            usage={"input_tokens": 20, "output_tokens": 4},
        )


async def test_soft_check_jev_on_uses_typed_scores_without_llm_fallback() -> None:
    findings = await soft_check_citations(
        usages=[
            {"cite_key": "a", "section_key": "s", "context_snippet": "supported"},
            {"cite_key": "b", "section_key": "s", "context_snippet": "weak"},
        ],
        abstracts={"a": "evidence a", "b": "evidence b"},
        runner=None,
        decision_runner=_DecisionStub(),
        decision_mode="on",
    )
    assert [item.cite_key for item in findings] == ["a", "b"]
    assert findings[0].score == 1.0
    assert findings[1].weak


async def test_jev_shadow_preserves_duplicate_citations_and_binds_each_question():
    class RecordingDecision(_DecisionStub):
        async def decide(self, *, state, questions, metadata=None):
            assert "pairs[0].context" in questions["item_0"]["instructions"]
            assert "pairs[1].evidence" in questions["item_1"]["instructions"]
            result = await super().decide(state=state, questions=questions, metadata=metadata)
            result.answers["item_1"]["confidence"] = 0.0
            return result

    class Trace:
        events = []

        async def emit(self, event, payload, **kwargs):
            self.events.append((event, payload))

    trace = Trace()
    findings = await soft_check_citations(
        usages=[{"cite_key": "same", "context_snippet": text} for text in ["first", "second"]],
        abstracts={"same": "source"},
        runner=_runner('{"judgements":[{"index":0,"score":1},{"index":1,"score":0.1}]}'),
        decision_runner=RecordingDecision(),
        decision_mode="shadow",
        trace_context=trace,
    )
    assert [f.score for f in findings] == [1.0, 0.1]
    event = trace.events[-1][1]
    assert event["comparable_count"] == 2
    assert event["weak_agreement_count"] == 2
    assert not event["accepted"]
    assert event["items"][1]["confidence"] == 0
    assert event["confidence_summary"]["zero_count"] == 1
    assert event["items"][0]["pair_hash"] != event["items"][1]["pair_hash"]
    assert "context" not in event["items"][0]


async def test_jev_failure_does_not_change_shadow_baseline():
    class FailedDecision:
        enabled = True

        async def decide(self, **kwargs):
            return DecisionResult(error="timeout")

    findings = await soft_check_citations(
        usages=[{"cite_key": "x", "context_snippet": "a"}],
        abstracts={"x": "b"},
        runner=_runner('{"judgements":[{"index":0,"score":0.2}]}'),
        decision_runner=FailedDecision(),
        decision_mode="shadow",
    )
    assert len(findings) == 1 and findings[0].score == 0.2


# ---- 跨语言硬证据核验 ----


def _bilingual_anchor(**overrides: Any) -> dict[str, Any]:
    anchor: dict[str, Any] = {
        "claim_hash": "claim-1",
        "claim_text": "狄利克雷邻域采样能够降低污染节点对目标物品表示的影响。",
        "claim_kind": "effect",
        "cite_key": "yue2022defending",
        "is_core": True,
        "source_kind": "fulltext",
        "source_section": "Methodology",
        "source_page": None,
        "source_paragraph": 2,
        "evidence_excerpt": (
            "Dirichlet neighborhood sampling reduces the influence of polluted nodes "
            "on the target-item representation."
        ),
        "support_status": "insufficient_support",
        "support_score": 0.0,
        "grade_ok": True,
        "comparability_ok": None,
        "manual_status": "unreviewed",
    }
    anchor.update(overrides)
    return anchor


async def test_cross_language_verifier_promotes_only_confident_direct_support() -> None:
    anchors = [_bilingual_anchor()]
    summary = await verify_cross_language_claim_evidence(
        anchors=anchors,
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "supported",
                        "confidence": CROSS_LANGUAGE_SUPPORT_CONFIDENCE,
                        "reason": "The excerpt directly states the same effect.",
                    }
                ]
            }
        ),
    )

    assert summary["status"] == "completed"
    assert summary["promoted_count"] == 1
    assert anchors[0]["support_status"] == "supported"
    assert anchors[0]["support_score"] == CROSS_LANGUAGE_SUPPORT_CONFIDENCE


async def test_claim_verifier_enforce_rejects_contradiction_and_accepts_paraphrase() -> None:
    """Shared words select an excerpt; they must never decide scholarly support."""
    contradiction = _bilingual_anchor(
        claim_hash="contradiction",
        claim_text="Treatment A significantly improved survival compared with control.",
        evidence_excerpt=(
            "Treatment A did not significantly improve survival compared with control."
        ),
        support_status="supported",
        support_score=0.92,
    )
    paraphrase = _bilingual_anchor(
        claim_hash="paraphrase",
        claim_text="Treatment A lowered the risk of relapse.",
        evidence_excerpt="Participants receiving A were less likely to experience recurrence.",
    )
    summary = await verify_claim_evidence(
        anchors=[contradiction, paraphrase],
        mode="enforce",
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "supported",
                        "confidence": 0.96,
                        "reason": (
                            "Relapse risk and recurrence likelihood express the same outcome."
                        ),
                    },
                    {
                        "index": 1,
                        "verdict": "contradicted",
                        "confidence": 0.99,
                        "reason": "The excerpt explicitly negates the claimed improvement.",
                    },
                ]
            }
        ),
    )

    assert summary["status"] == "completed"
    assert summary["demoted_count"] == 1
    assert summary["promoted_count"] == 1
    assert contradiction["support_status"] == "insufficient_support"
    assert contradiction["support_score"] is None
    assert paraphrase["support_status"] == "supported"
    assert paraphrase["support_score"] == 0.96


async def test_claim_verifier_default_records_negative_without_demoting() -> None:
    """The safe default must not let an unmeasured model newly block a paper."""
    anchor = _bilingual_anchor(
        claim_hash="promotion-only-contradiction",
        support_status="supported",
        support_score=0.92,
    )

    summary = await verify_claim_evidence(
        anchors=[anchor],
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "contradicted",
                        "confidence": 0.99,
                        "reason": "The excerpt explicitly states the opposite direction.",
                    }
                ]
            }
        ),
    )

    assert summary["mode"] == "promote_only"
    assert summary["would_demote_count"] == 1
    assert summary["demoted_count"] == 0
    assert anchor["support_status"] == "supported"
    assert anchor["support_score"] == 0.92


async def test_claim_verifier_shadow_and_off_never_change_support_status() -> None:
    payload = {
        "judgements": [
            {
                "index": 0,
                "verdict": "supported",
                "confidence": 0.99,
                "reason": "The excerpt directly entails the complete claim.",
            }
        ]
    }
    shadow = _bilingual_anchor(claim_hash="shadow")
    shadow_summary = await verify_claim_evidence(
        anchors=[shadow],
        mode="shadow",
        runner=_runner(payload),
    )

    calls: list[Any] = []
    disabled = _bilingual_anchor(claim_hash="disabled")
    off_summary = await verify_claim_evidence(
        anchors=[disabled],
        mode="off",
        runner=_runner(payload, calls=calls),
    )

    assert shadow_summary["status"] == "completed"
    assert shadow_summary["would_promote_count"] == 1
    assert shadow_summary["promoted_count"] == 0
    assert shadow["support_status"] == "insufficient_support"
    assert off_summary["status"] == "disabled"
    assert off_summary["scheduled_count"] == 0
    assert off_summary["failed_count"] == 0
    assert disabled["support_status"] == "insufficient_support"
    assert calls == []


async def test_claim_verifier_outage_preserves_provisional_status() -> None:
    provisional = _bilingual_anchor(
        claim_text="The treatment increased recovery by 27%.",
        evidence_excerpt="The treatment increased recovery by 27%.",
        support_status="supported",
        support_score=1.0,
    )

    summary = await verify_claim_evidence(anchors=[provisional], runner=None)

    assert summary["status"] == "unavailable"
    assert summary["demoted_count"] == 0
    assert summary["failed_count"] == 1
    assert provisional["support_status"] == "supported"
    assert provisional["support_score"] == 1.0


async def test_claim_verifier_cap_preserves_unverified_overflow_status() -> None:
    anchors = [
        _bilingual_anchor(
            claim_hash=f"claim-{index}",
            support_status="supported",
            support_score=1.0,
        )
        for index in range(MAX_CLAIM_EVIDENCE_CHECKS + 1)
    ]
    summary = await verify_claim_evidence(
        anchors=anchors,
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": index,
                        "verdict": "supported",
                        "confidence": 0.99,
                        "reason": "The excerpt directly entails the claim.",
                    }
                    for index in range(MAX_CLAIM_EVIDENCE_CHECKS)
                ]
            }
        ),
    )

    assert summary["status"] == "partial"
    assert summary["candidate_count"] == MAX_CLAIM_EVIDENCE_CHECKS + 1
    assert summary["scheduled_count"] == MAX_CLAIM_EVIDENCE_CHECKS
    assert summary["checked_count"] == MAX_CLAIM_EVIDENCE_CHECKS
    assert summary["failed_count"] == 1
    assert anchors[-1]["support_status"] == "supported"
    assert anchors[-1]["support_score"] == 1.0


async def test_claim_verifier_does_not_demote_on_partial_or_low_confidence_negative() -> None:
    partial = _bilingual_anchor(
        claim_hash="partial-supported",
        support_status="supported",
        support_score=0.8,
    )
    uncertain_negative = _bilingual_anchor(
        claim_hash="low-confidence-negative",
        support_status="supported",
        support_score=0.75,
    )

    summary = await verify_claim_evidence(
        anchors=[partial, uncertain_negative],
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "partial",
                        "confidence": 0.99,
                        "reason": "Only one part of the compound claim is entailed.",
                    },
                    {
                        "index": 1,
                        "verdict": "contradicted",
                        "confidence": CLAIM_DEMOTION_CONFIDENCE - 0.01,
                        "reason": "The direction may differ, but confidence is below the gate.",
                    },
                ]
            }
        ),
    )

    assert summary["status"] == "completed"
    assert summary["demoted_count"] == 0
    assert partial["support_status"] == "supported"
    assert partial["support_score"] == 0.8
    assert uncertain_negative["support_status"] == "supported"
    assert uncertain_negative["support_score"] == 0.75


async def test_claim_verifier_reuses_job_cache_for_an_unchanged_pair() -> None:
    calls: list[Any] = []
    runner = _runner(
        {
            "judgements": [
                {
                    "index": 0,
                    "verdict": "supported",
                    "confidence": 0.98,
                    "reason": "The excerpt directly entails the complete claim.",
                }
            ]
        },
        calls=calls,
    )
    cache: dict[str, dict[str, Any]] = {}
    first = _bilingual_anchor()
    second = _bilingual_anchor()

    first_summary = await verify_claim_evidence(
        anchors=[first],
        runner=runner,
        cache=cache,
    )
    second_summary = await verify_claim_evidence(
        anchors=[second],
        runner=runner,
        cache=cache,
    )

    assert len(calls) == 1
    assert first_summary["model_checked_count"] == 1
    assert first_summary["cache_hit_count"] == 0
    assert second_summary["model_checked_count"] == 0
    assert second_summary["cache_hit_count"] == 1
    assert second_summary["status"] == "completed"
    assert second["support_status"] == "supported"


async def test_claim_verifier_reuses_a_persistent_model_bound_cache_entry() -> None:
    calls: list[Any] = []
    runner = _runner({}, calls=calls)
    anchor = _bilingual_anchor()
    cache_key = claim_verification_cache_key(anchor, model=runner.model_for("verifier"))
    cache = {
        cache_key: {
            "cache_key": cache_key,
            "verdict": "supported",
            "confidence": 0.98,
            "reason": "The excerpt directly entails the complete claim.",
            "model": runner.model_for("verifier"),
            "cache_scope": "persistent",
        }
    }

    summary = await verify_claim_evidence(
        anchors=[anchor],
        runner=runner,
        cache=cache,
        mode="shadow",
    )

    assert calls == []
    assert summary["persistent_cache_hit_count"] == 1
    assert summary["job_cache_hit_count"] == 0
    assert anchor["entailment_cached"] is True
    assert anchor["entailment_verdict"] == "supported"


def test_claim_verification_cache_key_changes_with_the_configured_model() -> None:
    anchor = _bilingual_anchor()

    assert claim_verification_cache_key(
        anchor, model="deepseek-v4-flash"
    ) != claim_verification_cache_key(anchor, model="deepseek-v4-pro")


def test_claim_entailment_verdicts_match_the_persistence_layer() -> None:
    """Drift between the two copies is silent: the cache would just stop storing that verdict."""
    from db.repositories.quality import CLAIM_ENTAILMENT_VERDICTS as PERSISTED_VERDICTS
    from paperforge_worker.pipelines.quality import CLAIM_ENTAILMENT_VERDICTS

    assert CLAIM_ENTAILMENT_VERDICTS == PERSISTED_VERDICTS


def test_claim_verification_cache_key_is_invalidated_by_a_prompt_edit(monkeypatch) -> None:
    """Cached verdicts are permanent, so editing the prompt must not silently reuse them."""
    import hashlib

    from paperforge_worker.pipelines import quality

    # The fingerprint must actually derive from the prompt, or editing the prompt changes nothing.
    assert (
        quality._CLAIM_EVIDENCE_PROMPT_FINGERPRINT
        == hashlib.sha256(quality._CLAIM_EVIDENCE_PROMPT.encode("utf-8")).hexdigest()[:16]
    )

    anchor = _bilingual_anchor()
    before = claim_verification_cache_key(anchor, model="deepseek-v4-flash")
    monkeypatch.setattr(quality, "_CLAIM_EVIDENCE_PROMPT_FINGERPRINT", "0" * 16)

    assert claim_verification_cache_key(anchor, model="deepseek-v4-flash") != before


async def test_demotion_without_any_alternative_is_recorded_as_unconfirmed() -> None:
    """Absence of an alternative excerpt still demotes, but must not read as a reviewed demotion."""
    anchor = _bilingual_anchor(
        claim_hash="no-alternative-available",
        support_status="supported",
        support_score=0.4,
        evidence_excerpt="A figure overview mentions the treatment.",
    )

    summary = await verify_claim_evidence(
        anchors=[anchor],
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "unsupported",
                        "confidence": 0.97,
                        "reason": "The overview does not report the claimed result.",
                    }
                ]
            }
        ),
        cache={},
        mode="enforce",
    )

    assert summary["demotion_unconfirmed_count"] == 1
    assert summary["unsafe_demotion_avoided_count"] == 0
    # Behaviour is deliberately unchanged: no alternative is not evidence of support.
    assert summary["demoted_count"] == 1
    assert anchor["support_status"] == "insufficient_support"
    assert anchor["entailment_review_json"]["status"] == "no_alternative_available"


async def test_negative_primary_does_not_demote_when_an_alternative_is_not_negative() -> None:
    anchor = _bilingual_anchor(
        claim_hash="unsafe-selection",
        support_status="supported",
        support_score=0.4,
        evidence_excerpt="A figure overview mentions the treatment.",
        _verification_alternatives=[
            {
                "evidence_excerpt": "The treatment significantly improved recovery.",
                "evidence_hash": "alternative-evidence",
                "evidence_unit_id": "result-unit",
                "source_section": "Results",
                "source_paragraph": 4,
            }
        ],
    )
    calls: list[Any] = []
    runner = _sequence_runner(
        [
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "unsupported",
                        "confidence": 0.97,
                        "reason": "The overview does not report the claimed result.",
                    }
                ]
            },
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "supported",
                        "confidence": 0.99,
                        "reason": "The alternative result passage directly states the claim.",
                    }
                ]
            },
        ],
        calls=calls,
    )

    summary = await verify_claim_evidence(
        anchors=[anchor],
        runner=runner,
        cache={},
        mode="enforce",
    )

    assert len(calls) == 2
    assert summary["raw_would_demote_count"] == 1
    assert summary["would_demote_count"] == 0
    assert summary["unsafe_demotion_avoided_count"] == 1
    assert summary["alternative_checked_count"] == 1
    assert anchor["support_status"] == "supported"
    assert anchor["entailment_review_json"]["status"] == "alternative_not_negative"


async def test_incomplete_alternative_review_fails_safe_without_demotion() -> None:
    anchor = _bilingual_anchor(
        claim_hash="incomplete-selection-review",
        support_status="supported",
        support_score=0.4,
        evidence_excerpt="A figure overview mentions the treatment.",
        _verification_alternatives=[
            {
                "evidence_excerpt": "A separate located result passage.",
                "evidence_hash": "alternative-evidence",
                "source_section": "Results",
            }
        ],
    )
    calls: list[Any] = []
    runner = _sequence_runner(
        [
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "unsupported",
                        "confidence": 0.97,
                        "reason": "The overview does not report the claimed result.",
                    }
                ]
            },
            "not-json",
        ],
        calls=calls,
    )

    summary = await verify_claim_evidence(
        anchors=[anchor],
        runner=runner,
        cache={},
        mode="enforce",
    )

    assert summary["status"] == "partial"
    assert summary["review_incomplete_count"] == 1
    assert summary["would_demote_count"] == 0
    assert anchor["support_status"] == "supported"


async def test_cross_language_verifier_does_not_promote_partial_or_low_confidence_support() -> None:
    partial = _bilingual_anchor(claim_hash="partial")
    uncertain = _bilingual_anchor(claim_hash="uncertain")
    summary = await verify_cross_language_claim_evidence(
        anchors=[partial, uncertain],
        runner=_runner(
            {
                "judgements": [
                    {
                        "index": 0,
                        "verdict": "partial",
                        "confidence": 0.99,
                        "reason": "Only the method name overlaps.",
                    },
                    {
                        "index": 1,
                        "verdict": "supported",
                        "confidence": CROSS_LANGUAGE_SUPPORT_CONFIDENCE - 0.01,
                        "reason": "Support is not sufficiently certain.",
                    },
                ]
            }
        ),
    )

    assert summary["checked_count"] == 2
    assert summary["promoted_count"] == 0
    assert partial["support_status"] == "insufficient_support"
    assert uncertain["support_status"] == "insufficient_support"


async def test_cross_language_verifier_fails_closed_and_preserves_other_hard_rules() -> None:
    unavailable = _bilingual_anchor()
    summary = await verify_cross_language_claim_evidence(
        anchors=[unavailable],
        runner=_runner("<html>not json</html>"),
    )
    assert summary["status"] == "unavailable"
    assert summary["failed_count"] == 1
    assert unavailable["support_status"] == "insufficient_support"

    numeric_without_locator = _bilingual_anchor(
        claim_kind="numeric",
        support_status="numeric_locator_missing",
    )
    same_language = _bilingual_anchor(
        claim_text="Dirichlet sampling reduced polluted-node influence.",
    )
    same_language_summary = await verify_cross_language_claim_evidence(
        anchors=[numeric_without_locator, same_language],
        runner=_runner({"judgements": []}),
    )
    assert same_language_summary["status"] == "unavailable"
    assert same_language_summary["candidate_count"] == 1
    assert same_language_summary["failed_count"] == 1


# ---- 覆盖建议 ----


def test_section_without_citations_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2024, 2025],
        now=NOW,
    )
    kinds = {hint["kind"] for hint in hints}
    assert "no_citations" in kinds
    assert any(hint.get("section_key") == "s2" for hint in hints)


def test_unused_library_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=10,
        used_keys={"a2020x"},
        publication_years=[2025],
        now=NOW,
    )
    assert any(hint["kind"] == "unused_library" for hint in hints)


def test_stale_library_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2001, 2003, 2005, 2007],
        now=NOW,
    )
    assert any(hint["kind"] == "stale_library" for hint in hints)


def test_uncovered_subtopic_is_hinted() -> None:
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2025],
        scope={"topic": "检索增强生成", "subtopics": ["多模态检索"]},
        now=NOW,
    )
    assert any(hint["kind"] == "uncovered_subtopic" for hint in hints)


def test_subtopics_that_are_just_topic_words_do_not_spam_hints() -> None:
    """确定性回退的 subtopics 就是主题切词——逐词提示只会刷屏。"""
    hints = coverage_hints(
        sections=_sections(),
        whitelist_size=2,
        used_keys={"a2020x", "b2021y"},
        publication_years=[2025],
        scope={
            "topic": "retrieval augmented generation for science",
            "subtopics": ["retrieval", "augmented", "generation"],
        },
        now=NOW,
    )
    assert not [hint for hint in hints if hint["kind"] == "uncovered_subtopic"]


def test_frame_sections_are_not_hinted_for_missing_citations() -> None:
    hints = coverage_hints(
        sections=[
            {
                "section_key": "abstract",
                "title": "摘要",
                "word_count": 1000,
                "cite_keys": [],
                "kind": "frame",
            }
        ],
        whitelist_size=1,
        used_keys={"a"},
        publication_years=[2025],
        now=NOW,
    )
    # 摘要/引言/结论不带引用是正常的，不该刷提示。
    assert not [hint for hint in hints if hint["kind"] == "no_citations"]


# ---- 质量评分 ----


def test_zh_language_check_flags_a_full_english_prose_paragraph() -> None:
    row = SimpleNamespace(
        section_key="s1",
        body_ir_json={
            "blocks": [
                {
                    "runs": [
                        {
                            "t": "text",
                            "v": "This is a complete English paragraph with enough words to "
                            "trigger the Chinese manuscript language quality check reliably and "
                            "ensure that an untranslated generated discussion cannot pass export.",
                        }
                    ]
                }
            ]
        },
    )
    assert zh_language_mismatches([row])[0]["section_key"] == "s1"


def test_zh_language_check_allows_chinese_prose_with_english_terms() -> None:
    row = SimpleNamespace(
        section_key="s1",
        body_ir_json={
            "blocks": [
                {
                    "runs": [
                        {
                            "t": "text",
                            "v": (
                                "该方法在 MIBiG 数据集上使用 Transformer 模型进行"
                                "生物合成基因簇识别。"
                            ),
                        }
                    ]
                }
            ]
        },
    )
    assert zh_language_mismatches([row]) == []


def test_quality_report_computes_density_and_coverage() -> None:
    report = build_quality_report(
        sections=_sections(),
        whitelist_size=4,
        publication_years=[2024, 2025, 2019, 2018],
        fulltext_coverage=0.5,
        soft_check=[
            SoftCheckFinding(cite_key="b2021y", section_key="s1", score=0.2, reason="不相关")
        ],
        now=NOW,
    )
    payload = report.to_payload()
    assert payload["word_count"] == 2300
    assert payload["unique_cite_count"] == 2
    assert payload["library_coverage"] == 0.5
    assert payload["citation_density"] > 0
    assert payload["fulltext_coverage"] == 0.5
    assert payload["sections_without_citations"] == ["s2"]
    # 只有弱引用进报告，强引用不刷屏。
    assert len(payload["soft_check"]) == 1
    assert payload["soft_check"][0]["score"] < SOFT_CHECK_THRESHOLD


def test_quality_report_never_blocks_on_empty_document() -> None:
    report = build_quality_report(
        sections=[],
        whitelist_size=0,
        publication_years=[],
        now=NOW,
    )
    payload = report.to_payload()
    # 空文稿也返回结构化报告，而不是抛错或拒绝（draft-first）。
    assert payload["section_count"] == 0
    assert payload["citation_density"] == 0.0


def test_count_words_matches_writing_pipeline() -> None:
    assert count_words("检索增强生成") == 6
    assert count_words("retrieval augmented generation") == 3


async def test_soft_check_retries_only_missing_and_rejects_duplicate_indexes():
    calls = []
    runner = _sequence_runner(
        [
            {
                "judgements": [
                    {"index": 0, "score": 0.9},
                    {"index": 1, "score": 0.1},
                    {"index": 1, "score": 0.9},
                ]
            },
            {"judgements": [{"index": 0, "score": 0.2}]},
        ],
        calls=calls,
    )
    findings = await soft_check_citations(
        usages=[
            {"cite_key": "a", "context_snippet": "first"},
            {"cite_key": "b", "context_snippet": "second"},
        ],
        abstracts={"a": "alpha", "b": "beta"},
        runner=runner,
    )
    assert len(calls) == 2
    assert "first" not in calls[1].user_prompt
    assert [f.score for f in findings] == [0.9, 0.2]
    assert all(f.status == "completed" for f in findings)


async def test_production_shadow_does_not_call_jev_inline(monkeypatch):
    from paperforge_worker import citation_shadow

    queued = []

    async def enqueue(context, pairs, observation):
        queued.append((pairs, observation))

    monkeypatch.setattr(citation_shadow, "enqueue", enqueue)

    class Trace:
        session_factory = True

        async def emit(self, *args, **kwargs):
            pass

    class Never:
        enabled = True

        async def decide(self, **kwargs):
            raise AssertionError("foreground must not call Jev")

    findings = await soft_check_citations(
        usages=[{"cite_key": "a", "context_snippet": "first"}],
        abstracts={"a": "alpha"},
        runner=_runner({"judgements": [{"index": 0, "score": 0.9}]}),
        decision_runner=Never(),
        decision_mode="shadow",
        trace_context=Trace(),
    )
    assert findings[0].score == 0.9
    assert len(queued) == 1


async def test_fast_draft_preview_keeps_low_confidence_as_preliminary():
    class Jev:
        enabled = True

        async def decide(self, **kwargs):
            assert "pairs[0].context" in kwargs["questions"]["item_0"]["instructions"]
            return SimpleNamespace(
                ok=True,
                answers={"item_0": {"score": 1, "confidence": 0.02}},
                model="jev-test",
                latency_ms=1300,
            )

    result = await preview_citations(
        usages=[{"cite_key": "a", "context_snippet": "claim"}],
        sources={"a": "excerpt"},
        decision_runner=Jev(),
    )
    assert result["status"] == "complete"
    assert result["items"][0]["status"] == "preliminary"
    assert result["items"][0]["weak"] is True
    assert result["items"][0]["confidence"] == 0.02


async def test_fast_draft_preview_failure_never_marks_a_citation_verified():
    class Jev:
        enabled = True

        async def decide(self, **_kwargs):
            return SimpleNamespace(
                ok=False, answers=None, model="jev-test", latency_ms=2000, error="timeout"
            )

    result = await preview_citations(
        usages=[{"cite_key": "a", "context_snippet": "claim"}],
        sources={"a": "excerpt"},
        decision_runner=Jev(),
    )
    assert result["status"] == "unavailable"
    assert result["items"][0]["status"] == "unverified"
