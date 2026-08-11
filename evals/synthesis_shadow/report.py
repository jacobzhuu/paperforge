"""影子评估的统计与呈现（纯函数，可直接断言）。

分开放在这里，是因为「跑一次真实调用」和「怎么解读结果」不该纠缠：解读逻辑要能
在没有数据库、没有 LLM 的情况下被测试。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

ENTRY_KINDS = ("agreement", "conditional", "conflict", "gap")


@dataclass
class BundleObservation:
    """一个子问题的影子结果。"""

    project_id: str
    question_id: str
    question: str
    answer_status: str
    evidence_units: int
    fulltext_units: int
    distinct_works: int
    comparison_clusters: int
    # 跳过时后面的字段保持初值——「没花钱」和「花了钱但一条没留下」必须分得开。
    called: bool = False
    parsed: bool = False
    skip_reason: str | None = None
    accepted: dict[str, int] = field(default_factory=dict)
    rejected: Counter[str] = field(default_factory=Counter)
    claim_kept: bool = False
    claim: str | None = None
    # 通过校验的条目原文。人工复核靠的是这个，不是计数——「冲突是不是真冲突」
    # 只能读出来判断。
    entries: list[dict[str, Any]] = field(default_factory=list)
    # 被拒条目的原文与被拒的那个值："维度不认识" 要能追到是哪个维度，
    # 否则分不清该扩本体还是该改提示词。
    rejections: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "question_id": self.question_id,
            "question": self.question,
            "answer_status": self.answer_status,
            "evidence_units": self.evidence_units,
            "fulltext_units": self.fulltext_units,
            "distinct_works": self.distinct_works,
            "comparison_clusters": self.comparison_clusters,
            "called": self.called,
            "parsed": self.parsed,
            "skip_reason": self.skip_reason,
            "claim": self.claim,
            "accepted": self.accepted,
            "rejected": dict(self.rejected),
            "entries": self.entries,
            "rejections": self.rejections,
        }

    @property
    def accepted_total(self) -> int:
        return sum(self.accepted.values())

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())


@dataclass
class ShadowReport:
    observations: list[BundleObservation] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    unpriced_calls: int = 0
    failed_calls: int = 0
    # 失败原因分布。没有它，「6 个问题只有 2 个出结果」这种结果无法解读。
    failure_reasons: Counter[str] = field(default_factory=Counter)
    model: str = ""

    def to_payload(self) -> dict[str, Any]:
        called = [item for item in self.observations if item.called]
        parsed = [item for item in called if item.parsed]
        rejected: Counter[str] = Counter()
        accepted: Counter[str] = Counter()
        for item in parsed:
            rejected.update(item.rejected)
            accepted.update(item.accepted)
        proposed = sum(accepted.values()) + sum(rejected.values())
        clusters = sum(item.comparison_clusters for item in self.observations)
        return {
            "bundles": len(self.observations),
            "skipped": {
                reason: count
                for reason, count in Counter(
                    item.skip_reason for item in self.observations if item.skip_reason
                ).items()
            },
            "calls": len(called),
            "responses_parsed": len(parsed),
            "entries_proposed": proposed,
            "entries_accepted": sum(accepted.values()),
            "acceptance_rate": (sum(accepted.values()) / proposed) if proposed else None,
            "accepted_by_kind": {kind: accepted.get(kind, 0) for kind in ENTRY_KINDS},
            "rejected_by_reason": dict(rejected.most_common()),
            "rejected_values": dict(
                Counter(
                    f"{item['reason']}={item['value']}"
                    for observation in parsed
                    for item in observation.rejections
                    if item.get("value")
                ).most_common()
            ),
            "claims_kept": sum(1 for item in parsed if item.claim_kept),
            "bundles_with_nothing_accepted": sum(
                1 for item in parsed if item.accepted_total == 0
            ),
            # 冲突判定的前提条件。为 0 时规则 2 结构上不可能通过——那不是模型的问题。
            "comparison_clusters_available": clusters,
            "bundles_detail": [item.to_payload() for item in self.observations],
            "cost": {
                "model": self.model,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimate": self.cost,
                "unpriced_calls": self.unpriced_calls,
                "failed_calls": self.failed_calls,
                "failure_reasons": dict(self.failure_reasons.most_common()),
            },
        }


def render(payload: dict[str, Any]) -> str:
    """给人看的摘要。数字之外，重要的是把"不能测"和"测出来是零"分开。"""
    lines = [
        "PaperForge Phase 4 — synthesis shadow evaluation",
        "",
        f"  bundles seen              {payload['bundles']}",
    ]
    for reason, count in sorted(payload["skipped"].items()):
        lines.append(f"    skipped ({reason:<22}) {count}")
    lines += [
        f"  LLM calls                 {payload['calls']}",
        f"  responses parsed          {payload['responses_parsed']}",
        "",
        f"  entries proposed          {payload['entries_proposed']}",
        f"  entries accepted          {payload['entries_accepted']}",
    ]
    rate = payload["acceptance_rate"]
    lines.append(f"  acceptance rate           {'n/a' if rate is None else format(rate, '.1%')}")
    lines.append("")
    for kind, count in payload["accepted_by_kind"].items():
        lines.append(f"    accepted {kind:<14} {count}")
    if payload["rejected_by_reason"]:
        lines.append("")
        for reason, count in payload["rejected_by_reason"].items():
            lines.append(f"    rejected {reason:<24} {count}")
    lines += [
        "",
        f"  claims kept               {payload['claims_kept']}/{payload['responses_parsed']}",
        f"  bundles with nothing kept {payload['bundles_with_nothing_accepted']}",
        f"  comparison clusters       {payload['comparison_clusters_available']}",
    ]
    if payload["comparison_clusters_available"] == 0:
        lines.append(
            "    ^ no cluster exists in this data, so a `conflict` entry cannot pass rule 2\n"
            "      no matter what the model says. Conflict soundness is UNTESTED here, not clean."
        )
    cost = payload["cost"]
    lines += [
        "",
        f"  model                     {cost['model'] or '(unknown)'}",
        f"  tokens                    {cost['input_tokens']} in / {cost['output_tokens']} out",
    ]
    if cost["estimate"] is None:
        lines.append(
            f"  cost                      unpriced ({cost['unpriced_calls']} call(s));"
            " set LLM_MODEL_PRICES"
        )
    else:
        bound = "" if not cost["unpriced_calls"] else "≥ "
        lines.append(f"  cost                      {bound}${cost['estimate']:.4f}")
    if cost["failed_calls"]:
        lines.append(f"  failed calls              {cost['failed_calls']}")
        for reason, count in cost.get("failure_reasons", {}).items():
            lines.append(f"    {reason:<24} {count}")
    return "\n".join(lines)


__all__ = ["ENTRY_KINDS", "BundleObservation", "ShadowReport", "render"]
