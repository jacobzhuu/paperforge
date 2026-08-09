"""Pre-writing evidence readiness gate.

Quality checks after prose exists are still necessary, but they cannot repair a
corpus that does not answer the research questions.  This module evaluates the
question/evidence closure before OUTLINE/WRITE.

**分级门禁（不是一刀切阻断）。** 早先这里的每一条问题都是硬阻断，结果是：五个
子问题里有一个没凑够第二篇独立文献，整篇稿子就一个字都产不出来——而 draft-first
的产品承诺是「降级而不阻断」。所以问题分成两级：

* ``blocking``：语料根本不成立，写出来的东西没有意义。只有两条——没有任何子问题，
  以及全项目可用全文证据凑不出两个独立来源。**「至少两个独立来源」不放宽**，它是
  防止单源综述的唯一防线。
* ``degradable``：覆盖率没达标、或者没有一个问题被完整回答。这时照常出稿，缺证的
  子问题由 ``writing`` 的 ``evidence_gap_skeleton`` 写成显式的「现有证据不足以
  回答」小节，而不是编一段流畅的空话。

**严格模式不降级。** ``review_style="systematic"`` 与 ``quality_profile="submission"``
是用户显式要求的严格产出，覆盖率缺口在那里意味着选择偏差，只能阻断。降级出稿是
draft-first 对叙述性综述的承诺，不是对系统综述的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

ELIGIBLE_GRADES = frozenset({"A_located_structured", "B_located_prose", "C_fulltext_unlocated"})
ANSWERABLE_STATUSES = frozenset({"answered", "partial", "contested"})
# 语料不成立，出稿没有意义。
BLOCKING_CODES = frozenset({"research_questions_missing", "evidence_source_diversity_low"})
# 严格模式下额外阻断：系统综述/投稿稿的覆盖率缺口就是选择偏差。
STRICT_BLOCKING_CODES = frozenset(
    {"question_evidence_coverage_low", "no_fully_synthesized_question"}
)


@dataclass
class EvidenceReadinessReport:
    readiness_status: str = "evidence_insufficient"
    blockers: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    total_questions: int = 0
    ready_questions: int = 0
    answerable_questions: int = 0
    strong_questions: int = 0
    insufficient_questions: int = 0
    linked_evidence: int = 0
    distinct_works: int = 0
    required_coverage: float = 0.8
    question_details: list[dict[str, Any]] = field(default_factory=list)
    report_id: str | None = None
    # 系统综述 / 投稿稿：用户显式要了严格产出，覆盖率缺口不允许降级。
    strict: bool = False

    @property
    def ready(self) -> bool:
        """能否进入正文写作。降级项不影响这个判断，只影响写成什么样。"""
        return not self.blockers

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)

    @property
    def coverage(self) -> float:
        return self.ready_questions / self.total_questions if self.total_questions else 0.0

    @property
    def unready_question_ids(self) -> list[str]:
        """证据没凑齐的子问题：正文里应写成显式证据缺口，而不是硬凑结论。"""
        return [
            str(detail.get("question_id") or "")
            for detail in self.question_details
            if not detail.get("ready") and detail.get("question_id")
        ]

    def to_payload(self) -> dict[str, Any]:
        return {
            "readiness_status": self.readiness_status,
            "ready": self.ready,
            "degraded": self.degraded,
            "blockers": self.blockers,
            "degradations": self.degradations,
            "warnings": self.warnings,
            "total_questions": self.total_questions,
            "ready_questions": self.ready_questions,
            "answerable_questions": self.answerable_questions,
            "strong_questions": self.strong_questions,
            "insufficient_questions": self.insufficient_questions,
            "linked_evidence": self.linked_evidence,
            "distinct_works": self.distinct_works,
            "coverage": round(self.coverage, 4),
            "required_coverage": self.required_coverage,
            "strict": self.strict,
            "question_details": self.question_details,
            "unready_question_ids": self.unready_question_ids,
        }

    def record(self, issue: dict[str, Any]) -> None:
        """按 code 与严格模式把问题投递到阻断级或降级级。"""
        blocking = issue["code"] in BLOCKING_CODES or (
            self.strict and issue["code"] in STRICT_BLOCKING_CODES
        )
        issue["severity"] = "blocking" if blocking else "degradable"
        (self.blockers if blocking else self.degradations).append(issue)


def evaluate_evidence_readiness(
    bundles: list[dict[str, Any]],
    *,
    matrix_payload: dict[str, Any] | None = None,
    quality_profile: str = "scholarly",
    review_style: str = "narrative",
) -> EvidenceReadinessReport:
    """Return a deterministic, explainable pre-writing decision.

    Narrative scholarly reviews may carry one explicit gap in a five-question
    decomposition.  Systematic/submission work requires closure for every
    question.  A question is ready only when it has at least two eligible
    evidence units from two distinct works; a single paper is useful but not a
    sufficient review synthesis base.
    """
    strict = review_style == "systematic" or quality_profile == "submission"
    required_coverage = 1.0 if strict else 0.8
    report = EvidenceReadinessReport(
        total_questions=len(bundles),
        required_coverage=required_coverage,
        strict=strict,
    )
    all_works: set[str] = set()
    for bundle in bundles:
        eligible = [
            item
            for item in bundle.get("evidence") or []
            if str(item.get("grade") or "") in ELIGIBLE_GRADES
        ]
        works = {str(item.get("work_id")) for item in eligible if item.get("work_id")}
        status = str(bundle.get("answer_status") or "insufficient_evidence")
        answerable = status in ANSWERABLE_STATUSES and bool(eligible)
        strong = status in {"answered", "contested"}
        # Submission/systematic output must not turn a matrix of merely partial
        # answers into a confident manuscript. Narrative reviews may retain an
        # explicitly qualified partial answer when it is supported independently.
        status_ready = strong or (
            status == "partial" and quality_profile != "submission" and review_style != "systematic"
        )
        ready = status_ready and len(eligible) >= 2 and len(works) >= 2
        report.answerable_questions += int(answerable)
        report.strong_questions += int(strong)
        report.ready_questions += int(ready)
        report.insufficient_questions += int(status == "insufficient_evidence")
        report.linked_evidence += len(eligible)
        all_works.update(works)
        report.question_details.append(
            {
                "question_id": str(bundle.get("question_id") or ""),
                "answer_status": status,
                "eligible_evidence_count": len(eligible),
                "distinct_work_count": len(works),
                "ready": ready,
                "strong": strong,
            }
        )
    report.distinct_works = len(all_works)

    if not bundles:
        report.record(
            {
                "code": "research_questions_missing",
                "message": "尚未形成可评估的研究子问题，不能进入正文写作",
            }
        )
    else:
        required_count = math.ceil(len(bundles) * required_coverage)
        if report.ready_questions < required_count:
            report.record(
                {
                    "code": "question_evidence_coverage_low",
                    "message": (
                        f"仅 {report.ready_questions}/{len(bundles)} 个子问题具备至少两篇"
                        "文献的可用全文证据，其余将写成显式证据缺口"
                    ),
                    "count": len(bundles) - report.ready_questions,
                    "required": required_count,
                }
            )
        if report.strong_questions == 0:
            report.record(
                {
                    "code": "no_fully_synthesized_question",
                    "message": "没有任何子问题被完整回答，正文只能给出有保留的阶段性结论",
                }
            )
        if report.distinct_works < 2:
            report.record(
                {
                    "code": "evidence_source_diversity_low",
                    "message": "可回答问题的全文证据不足两个独立文献来源",
                    "count": report.distinct_works,
                }
            )

    diagnostics = list((matrix_payload or {}).get("diagnostics") or [])
    rejected = sum(item.get("no_link_reason") == "classifier_rejected_all" for item in diagnostics)
    if diagnostics and rejected * 2 >= len(diagnostics):
        issue = {
            "code": "evidence_classifier_rejected_majority",
            "message": f"证据分类器拒绝了 {rejected}/{len(diagnostics)} 个子问题的全部候选",
            "count": rejected,
            "severity": "degradable",
        }
        # A stochastic rerun can reject candidates while the monotonic matrix
        # correctly retains an already sufficient durable result. In that case
        # surface the anomaly without degrading sound prior evidence.
        #
        # 注意这条**不表示语料回答不了问题**：候选是通过了词法/任务排序之后才被
        # 分类器逐条否掉的，更可能是提示过严或跨语言桥接退化。因此它绝不能当作
        # 「问题不可研究」的证据去触发问题改写——那正是问题漂移的来源。
        required_count = math.ceil(len(bundles) * required_coverage) if bundles else 0
        if report.ready_questions < required_count:
            report.degradations.append(issue)
        else:
            issue["severity"] = "warning"
            report.warnings.append(issue)

    if report.blockers:
        report.readiness_status = "evidence_insufficient"
    elif report.degradations:
        report.readiness_status = "evidence_degraded"
    else:
        report.readiness_status = "evidence_ready"
    return report


__all__ = [
    "ANSWERABLE_STATUSES",
    "BLOCKING_CODES",
    "ELIGIBLE_GRADES",
    "STRICT_BLOCKING_CODES",
    "EvidenceReadinessReport",
    "evaluate_evidence_readiness",
]
