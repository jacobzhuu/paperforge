from concurrent.futures import ThreadPoolExecutor

import pytest
from llm_runtime.config import LLMConfig, ModelPrice
from llm_runtime.experiment_budget import ExperimentBudget, ExperimentBudgetExceeded
from llm_runtime.types import LLMRequest


def test_parallel_budget_reservations_and_unknown_price(tmp_path):
    ledger = ExperimentBudget(tmp_path / "budget.sqlite", "calibration")
    request = LLMRequest("", "", "priced", 1000)
    config = LLMConfig(max_retries=0, model_prices={"priced": ModelPrice(0, 10_000)})

    def attempt(_):
        try:
            ledger.reserve(request, config)
            return True
        except ExperimentBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(attempt, range(12))) == 5
    assert ledger.report()["calibration"]["reserved_cny"] == 50
    with pytest.raises(ExperimentBudgetExceeded):
        ledger.reserve(request, LLMConfig(model_prices={}))
    with pytest.raises(ValueError):
        ExperimentBudget(tmp_path / "budget.sqlite", "calibration", currency="USD")
