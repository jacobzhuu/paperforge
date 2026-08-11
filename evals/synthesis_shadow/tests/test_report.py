"""影子评估的解读逻辑。

最关键的一条不是算数，而是措辞：``comparison_clusters == 0`` 时规则 2 结构上
不可能通过，报告必须说这是"未被检验"，而不是"没有发现问题"。把两者混为一谈，
正是"零告警"被当成"质量合格"的经典误读。
"""

from __future__ import annotations

from collections import Counter

from evals.synthesis_shadow.report import BundleObservation, ShadowReport, render


def _observation(**kwargs) -> BundleObservation:
    defaults = {
        "project_id": "p1",
        "question_id": "q1",
        "question": "Does it hold?",
        "answer_status": "answered",
        "evidence_units": 4,
        "fulltext_units": 4,
        "distinct_works": 2,
        "comparison_clusters": 1,
    }
    return BundleObservation(**{**defaults, **kwargs})


def test_a_skipped_bundle_is_not_counted_as_a_call():
    report = ShadowReport(
        observations=[_observation(skip_reason="below_evidence_floor"), _observation()]
    )
    report.observations[1].called = True
    report.observations[1].parsed = True
    report.observations[1].accepted = {"agreement": 2}

    payload = report.to_payload()

    assert payload["bundles"] == 2
    assert payload["calls"] == 1
    assert payload["skipped"] == {"below_evidence_floor": 1}


def test_the_acceptance_rate_counts_proposals_not_bundles():
    observation = _observation(called=True, parsed=True)
    observation.accepted = {"agreement": 3}
    observation.rejected = Counter({"unsourced_number": 1})

    payload = ShadowReport(observations=[observation]).to_payload()

    assert payload["entries_proposed"] == 4
    assert payload["entries_accepted"] == 3
    assert payload["acceptance_rate"] == 0.75


def test_a_call_that_kept_nothing_is_reported_separately():
    """花了钱、一条没留下，和"没花钱"是两件事，不能都算成零。"""
    observation = _observation(called=True, parsed=True)
    observation.rejected = Counter({"no_bundle_evidence": 2})

    payload = ShadowReport(observations=[observation]).to_payload()

    assert payload["calls"] == 1
    assert payload["entries_accepted"] == 0
    assert payload["bundles_with_nothing_accepted"] == 1


def test_no_cluster_is_reported_as_untested_not_as_clean():
    report = ShadowReport(observations=[_observation(comparison_clusters=0, called=True)])

    text = render(report.to_payload())

    assert "UNTESTED here, not clean" in text


def test_a_cluster_being_available_drops_the_caveat():
    report = ShadowReport(observations=[_observation(comparison_clusters=2, called=True)])

    assert "UNTESTED" not in render(report.to_payload())


def test_an_unpriced_run_says_so_instead_of_printing_zero():
    """P1-4 的同一条纪律：算不出金额就说算不出，不要打印 $0.0000。"""
    report = ShadowReport(
        observations=[_observation(called=True)], input_tokens=10, unpriced_calls=1, cost=None
    )

    text = render(report.to_payload())

    assert "unpriced (1 call(s))" in text
    assert "$0.0000" not in text


def test_a_partially_priced_run_marks_the_total_as_a_lower_bound():
    report = ShadowReport(
        observations=[_observation(called=True)], cost=0.5, unpriced_calls=2, model="m"
    )

    assert "≥ $0.5000" in render(report.to_payload())


def test_an_empty_run_does_not_divide_by_zero():
    payload = ShadowReport().to_payload()

    assert payload["acceptance_rate"] is None
    assert "n/a" in render(payload)


def test_a_disabled_runner_is_a_skip_not_a_silent_zero():
    """noop 自检不能伪装成"调用了但什么都没留下"。"""
    report = ShadowReport(observations=[_observation(skip_reason="runner_disabled")])

    payload = report.to_payload()

    assert payload["calls"] == 0
    assert payload["skipped"] == {"runner_disabled": 1}
