"""Real Redis admission, including multi-process callers; no paid provider traffic."""

import asyncio
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from llm_runtime import fair_admission
from llm_runtime.limits import provider_slot
from llm_runtime.telemetry import admission_cancellation, request_metrics
from redis import Redis


@pytest.fixture
def redis_fair(monkeypatch):
    url = os.environ.get("CAPACITY_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("requires isolated Redis at 127.0.0.1:26379")
    assert "127.0.0.1:26379" in url
    monkeypatch.setenv("LLM_LIMIT_REDIS_URL", url)
    monkeypatch.setenv("LLM_FAIR_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "6")
    client = Redis.from_url("redis://127.0.0.1:26379/0")
    keys = fair_admission.keys("test:fair:" + uuid.uuid4().hex)
    yield client, keys
    client.delete(*keys)
    client.close()


def acquire(r, keys, token, job, priority=0, cap=6):
    return r.eval(fair_admission.ACQUIRE, len(keys), *keys, token, cap, job, priority)


def release(r, keys, token):
    r.eval(fair_admission.RELEASE, len(keys), *keys, token)


def fill_legacy(r, keys):
    now = r.time()[0] * 1000
    r.zadd(keys[0], {f"legacy{i}": now + 120000 for i in range(6)})


def test_equal_jobs_fifo_and_new_arrival_receive_released_capacity(redis_fair):
    r, keys = redis_fair
    fill_legacy(r, keys)
    for job in ["A", "B"]:
        for n in range(4):
            assert acquire(r, keys, f"{job}{n}", job)[0] == 0
    r.delete(keys[0])
    assert acquire(r, keys, "A1", "A")[0] == 0, "per-job FIFO"
    for token in ["A0", "B0", "A1", "B1", "A2", "B2"]:
        decision = acquire(r, keys, token, token[0])
        assert decision[0] == 1
    assert acquire(r, keys, "C0", "C")[0] == 0
    release(r, keys, "A0")
    assert acquire(r, keys, "A3", "A")[0] == 0
    assert acquire(r, keys, "C0", "C")[0] == 1
    assert r.zcard(keys[0]) == 6


def test_shadow_waits_for_foreground_and_expired_waiters_do_not_block(redis_fair):
    r, keys = redis_fair
    fill_legacy(r, keys)
    acquire(r, keys, "s", "shadow", 1)
    acquire(r, keys, "f", "foreground")
    release(r, keys, "legacy0")
    assert acquire(r, keys, "s", "shadow", 1)[0] == 0
    assert acquire(r, keys, "f", "foreground")[0] == 1
    release(r, keys, "legacy1")
    assert acquire(r, keys, "s", "shadow", 1)[0] == 1
    acquire(r, keys, "dead", "dead-job")
    r.zadd(keys[1], {"dead": 0})
    release(r, keys, "legacy2")
    assert acquire(r, keys, "live", "live-job")[0] == 1
    assert r.hget(keys[3], "dead") is None


def test_expired_leases_and_old_worker_releases_are_cleaned(redis_fair):
    r, keys = redis_fair
    assert acquire(r, keys, "a", "A")[0] == 1
    r.zadd(keys[0], {"a": 0})
    assert acquire(r, keys, "b", "B")[0] == 1
    assert r.hget(keys[4], "a") is None
    r.zrem(keys[0], "b")
    acquire(r, keys, "c", "C")
    assert r.hget(keys[4], "b") is None


def test_timed_out_and_cancelled_waiters_are_removed_and_traced(redis_fair, monkeypatch):
    import hashlib

    r, _ = redis_fair
    config = SimpleNamespace(provider="test", base_url=uuid.uuid4().hex, api_key="fixture")
    key = "paperforge:provider:" + hashlib.sha256(f"{config.base_url}|fixture".encode()).hexdigest()
    keys = fair_admission.keys(key)
    fill_legacy(r, keys)
    metadata = {"scheduler_job_id": "cancelled"}
    event = threading.Event()

    def call():
        with request_metrics(metadata), admission_cancellation(event), provider_slot(config):
            raise AssertionError("cancelled request must not reach HTTP")

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(call)
        deadline = time.monotonic() + 3
        while not r.zcard(keys[1]) and time.monotonic() < deadline:
            time.sleep(0.01)
        event.set()
        with pytest.raises(CancelledError):
            task.result(timeout=3)
    assert r.zcard(keys[1]) == 0
    assert metadata["provider_admission_events"][-1]["event"] == "withdrawn"
    monkeypatch.setenv("LLM_ADMISSION_TIMEOUT", ".01")
    with pytest.raises(TimeoutError):
        with provider_slot(config):
            pass
    assert r.zcard(keys[1]) == 0
    r.delete(*keys)


def test_processes_share_global_cap_and_all_jobs_finish(redis_fair):
    r, _ = redis_fair
    marker = "test:fair:process:" + uuid.uuid4().hex
    code = """
import sys,time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from redis import Redis
from llm_runtime.limits import provider_slot
from llm_runtime.telemetry import request_metrics
marker,job=sys.argv[1:]
r=Redis.from_url('redis://127.0.0.1:26379/0')
def work(i):
 metadata={'scheduler_job_id':job}
 config=SimpleNamespace(provider='test',base_url=marker,api_key='test')
 with request_metrics(metadata), provider_slot(config):
  n=r.incr(marker+':active'); r.rpush(marker+':peaks',n)
  j=r.incr(marker+':'+job); r.rpush(marker+':jobpeaks',j)
  time.sleep(.08 if i % 2 else .15)
  r.decr(marker+':'+job); r.decr(marker+':active')
 r.rpush(marker+':done',job)
with ThreadPoolExecutor(max_workers=4) as pool:
 list(pool.map(work,range(8)))
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, marker, str(n)]) for n in range(3)]
    for p in processes:
        assert p.wait(timeout=30) == 0
    assert r.llen(marker + ":done") == 24
    assert max(map(int, r.lrange(marker + ":peaks", 0, -1))) <= 6
    assert max(map(int, r.lrange(marker + ":jobpeaks", 0, -1))) <= 4
    r.delete(
        *(
            marker + suffix
            for suffix in [":active", ":peaks", ":jobpeaks", ":done", ":0", ":1", ":2"]
        )
    )


async def test_async_cancellation_signals_waiting_thread_without_losing_paid_lease(redis_fair):
    from llm_runtime import LLMConfig, LLMRunner
    from llm_runtime.telemetry import check_admission_cancelled

    started, finished = threading.Event(), threading.Event()

    class Waiting:
        def generate(self, request):
            started.set()
            try:
                while True:
                    check_admission_cancelled()
                    time.sleep(0.01)
            finally:
                finished.set()

    runner = LLMRunner(LLMConfig(provider="openai", enabled=True))
    runner._provider = Waiting()
    task = asyncio.create_task(runner.agenerate("writer", system_prompt="s", user_prompt="u"))
    await asyncio.to_thread(started.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 3)


async def test_cancelling_paid_call_does_not_release_lease_until_response(redis_fair, monkeypatch):
    import hashlib

    from llm_runtime import LLMConfig, LLMResponse, LLMRunner

    r, _ = redis_fair
    monkeypatch.setenv("LLM_GLOBAL_CONCURRENCY", "1")
    config = SimpleNamespace(provider="test", base_url=uuid.uuid4().hex, api_key="fixture")
    key = "paperforge:provider:" + hashlib.sha256(f"{config.base_url}|fixture".encode()).hexdigest()
    entered, finish_http, recorded = threading.Event(), threading.Event(), threading.Event()
    records = []

    class Paid:
        def generate(self, request):
            with provider_slot(config):
                entered.set()
                assert finish_http.wait(5)
                return LLMResponse(text="paid result", model="test", provider="test")

    def save(record):
        records.append(record)
        recorded.set()

    runner = LLMRunner(LLMConfig(provider="openai", enabled=True), on_call=save)
    runner._provider = Paid()
    task = asyncio.create_task(runner.agenerate("writer", system_prompt="s", user_prompt="u"))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    assert r.zcard(key) == 1
    finish_http.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded.is_set()
    assert r.zcard(key) == 0
    assert records[0].error_code is None
    assert records[0].metadata["provider_admission_events"][-1]["event"] == "released"
    r.delete(*fair_admission.keys(key))


def test_renewal_retains_lease_and_redis_failure_does_not_admit(redis_fair, monkeypatch):
    from llm_runtime.limits import _RENEW

    r, keys = redis_fair
    acquire(r, keys, "live", "A")
    r.zadd(keys[0], {"live": r.time()[0] * 1000 + 10000})
    previous = r.zscore(keys[0], "live")
    assert r.eval(_RENEW, 1, keys[0], "live") == 1
    assert r.zscore(keys[0], "live") > previous
    r.zadd(keys[0], {"dead": 0})
    assert r.eval(_RENEW, 1, keys[0], "dead") == 0

    class Broken:
        def eval(self, *args):
            raise ConnectionError("Redis unavailable")

        def close(self):
            pass

    monkeypatch.setattr(Redis, "from_url", lambda *args, **kwargs: Broken())
    with pytest.raises(ConnectionError):
        with provider_slot(SimpleNamespace(provider="test", base_url="test", api_key="fixture")):
            pytest.fail("must not bypass Redis on failure")


def test_more_jobs_than_capacity_rotate_without_starvation(redis_fair):
    r, keys = redis_fair
    fill_legacy(r, keys)
    for n in range(7):
        acquire(r, keys, f"{n}-first", str(n))
        acquire(r, keys, f"{n}-second", str(n))
    r.delete(keys[0])
    for n in range(6):
        assert acquire(r, keys, f"{n}-first", str(n))[0] == 1
    release(r, keys, "0-first")
    assert acquire(r, keys, "0-second", "0")[0] == 0
    assert acquire(r, keys, "6-first", "6")[0] == 1


def test_long_request_renewal_retains_fairness_sequence(redis_fair):
    from llm_runtime.limits import _RENEW

    r, keys = redis_fair
    acquire(r, keys, "a", "A")
    for key in (keys[4], keys[5], keys[6]):
        r.pexpire(key, 1000)
    renewal_keys = [keys[0], keys[4], keys[5], keys[6]]
    assert r.eval(_RENEW, len(renewal_keys), *renewal_keys, "a") == 1
    assert all(r.pttl(k) > 120000 for k in renewal_keys)
