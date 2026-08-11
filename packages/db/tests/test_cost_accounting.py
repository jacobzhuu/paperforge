"""成本聚合必须把「不知道」和「零」分开（P1-4，真实 Postgres）。

`cost_estimate` 之前从来没被写过，于是 `project_llm_cost` 永远返回 0.0，界面上
一次几百万 token 的运行看起来是免费的。修好定价之后，剩下的风险换了个形状：
**部分定价**。只要还有一个模型没配价格，金额就只是下界，聚合结果必须自己说出这一点。
"""

from __future__ import annotations

import uuid

import pytest
from db import create_project, create_user, project_llm_cost, record_llm_call


async def _project(session):
    owner = await create_user(
        session,
        email=f"{uuid.uuid4()}@example.test",
        password_hash="!test-only",
        verified=True,
    )
    return await create_project(
        session,
        title="Cost accounting",
        paper_type="review",
        owner_id=owner.id,
    )


async def _call(session, project, **kwargs):
    defaults = {
        "role": "writer",
        "model": "priced-model",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_estimate": 0.25,
    }
    return await record_llm_call(
        session, project_id=project.id, job_id=None, **{**defaults, **kwargs}
    )


@pytest.mark.asyncio
async def test_a_fully_priced_project_reports_a_complete_total(session):
    project = await _project(session)
    await _call(session, project)
    await _call(session, project, cost_estimate=0.75)

    totals = await project_llm_cost(session, project.id)

    assert totals["cost_estimate"] == pytest.approx(1.0)
    assert totals["priced_call_count"] == 2
    assert totals["unpriced_call_count"] == 0
    assert totals["cost_complete"] is True


@pytest.mark.asyncio
async def test_one_unpriced_model_makes_the_total_a_lower_bound(session):
    """这是最危险的一种：金额非零、看起来权威，实际漏掉了整整一个模型。"""
    project = await _project(session)
    await _call(session, project)
    await _call(session, project, model="unpriced-model", cost_estimate=None)

    totals = await project_llm_cost(session, project.id)

    assert totals["cost_estimate"] == pytest.approx(0.25)
    assert totals["priced_call_count"] == 1
    assert totals["unpriced_call_count"] == 1
    assert totals["cost_complete"] is False


@pytest.mark.asyncio
async def test_a_provider_that_returns_no_usage_also_counts_as_unpriced(session):
    project = await _project(session)
    await _call(session, project, input_tokens=None, output_tokens=None, cost_estimate=None)

    totals = await project_llm_cost(session, project.id)

    assert totals["unpriced_call_count"] == 1
    assert totals["cost_complete"] is False


@pytest.mark.asyncio
async def test_a_failed_call_is_not_counted_as_unpriced(session):
    """失败调用没有用量也不该有费用；把它算成"未定价"会让面板永远报不完整。"""
    project = await _project(session)
    await _call(session, project)
    await _call(
        session,
        project,
        input_tokens=None,
        output_tokens=None,
        cost_estimate=None,
        error_code="LLMError",
    )

    totals = await project_llm_cost(session, project.id)

    assert totals["failed_call_count"] == 1
    assert totals["unpriced_call_count"] == 0
    assert totals["cost_complete"] is True


@pytest.mark.asyncio
async def test_a_genuine_zero_cost_call_is_priced_not_unknown(session):
    """自托管模型可以真的是 0 元；那是"已定价"，与"未定价"不能混为一谈。"""
    project = await _project(session)
    await _call(session, project, cost_estimate=0.0)

    totals = await project_llm_cost(session, project.id)

    assert totals["cost_estimate"] == 0.0
    assert totals["priced_call_count"] == 1
    assert totals["cost_complete"] is True


@pytest.mark.asyncio
async def test_a_project_with_no_calls_is_complete_and_zero(session):
    project = await _project(session)

    totals = await project_llm_cost(session, project.id)

    assert totals == {
        "call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_estimate": 0.0,
        "failed_call_count": 0,
        "priced_call_count": 0,
        "unpriced_call_count": 0,
        "cost_complete": True,
    }


@pytest.mark.asyncio
async def test_another_project_s_calls_are_not_counted(session):
    project = await _project(session)
    other = await _project(session)
    await _call(session, other, cost_estimate=99.0)

    assert (await project_llm_cost(session, project.id))["call_count"] == 0
