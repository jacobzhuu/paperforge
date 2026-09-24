import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from llm_runtime.limits import reserve_attempt
from redis import Redis


@pytest.fixture
def redis_limits(monkeypatch):
    url = os.environ.get("CAPACITY_TEST_REDIS_URL")
    if not url:
        pytest.skip("set CAPACITY_TEST_REDIS_URL to an isolated Redis")
    assert "127.0.0.1:26379" in url
    monkeypatch.setenv("LLM_LIMIT_REDIS_URL", url)
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "2")
    monkeypatch.setenv("LLM_GLOBAL_RPM", "0")
    client = Redis.from_url("redis://127.0.0.1:26379/0")
    return client


def test_processes_share_one_concurrency_budget(redis_limits):
    import uuid

    marker = uuid.uuid4().hex
    code = """
import time,sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from redis import Redis
from llm_runtime.limits import provider_slot
marker=sys.argv[1]
r=Redis.from_url('redis://127.0.0.1:26379/0')
def work(_):
 with provider_slot(SimpleNamespace(provider='openai',base_url=marker,api_key='fixture')):
  n=r.incr(marker)
  r.rpush(marker+':peaks',n)
  time.sleep(.1)
  r.decr(marker)
with ThreadPoolExecutor(max_workers=4) as pool:
 list(pool.map(work,range(8)))
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, marker]) for _ in range(2)]
    for process in processes:
        assert process.wait(timeout=20) == 0
    peaks = [int(value) for value in redis_limits.lrange(marker + ":peaks", 0, -1)]
    assert len(peaks) == 16
    assert max(peaks) == 2
    redis_limits.delete(marker, marker + ":peaks")


def test_http_attempts_have_a_bounded_rate_wait(redis_limits, monkeypatch):
    import uuid

    provider = SimpleNamespace(base_url=uuid.uuid4().hex, api_key="fixture")
    monkeypatch.setenv("LLM_GLOBAL_RPM", "2")
    monkeypatch.setenv("LLM_ADMISSION_TIMEOUT", "0.01")
    payload = {"model": "fixture", "max_tokens": 10}
    reserve_attempt(provider, payload)
    reserve_attempt(provider, payload)
    with pytest.raises(TimeoutError):
        reserve_attempt(provider, payload)


def test_model_budget_does_not_bypass_account_budget(redis_limits, monkeypatch):
    import uuid

    provider = SimpleNamespace(base_url=uuid.uuid4().hex, api_key="fixture")
    monkeypatch.setenv("LLM_GLOBAL_RPM", "1")
    monkeypatch.setenv("LLM_MODEL_LIMITS", '{"fixture":{"rpm":10}}')
    monkeypatch.setenv("LLM_ADMISSION_TIMEOUT", "0.01")
    reserve_attempt(provider, {"model": "fixture", "max_tokens": 10})
    with pytest.raises(TimeoutError):
        reserve_attempt(provider, {"model": "another-model", "max_tokens": 10})
