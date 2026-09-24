"""Cross-process provider admission. Held by the actual synchronous HTTP thread."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager

from llm_runtime import fair_admission
from llm_runtime.telemetry import (
    add_timing,
    admission_event,
    check_admission_cancelled,
    request_metadata,
)

_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 60000)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
if tonumber(ARGV[3]) > 0 and redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], now + 120000, ARGV[1])
redis.call('ZADD', KEYS[2], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 180000)
redis.call('PEXPIRE', KEYS[2], 120000)
return 1
"""
_RENEW = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local until_at = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not until_at or tonumber(until_at) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + 120000, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 180000)
for i=2,#KEYS do redis.call('PEXPIRE', KEYS[i], 180000) end
return 1
"""


@contextmanager
def provider_slot(config):
    url = os.environ.get("LLM_LIMIT_REDIS_URL", "")
    if not url or config.provider == "noop":
        yield
        return
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(url)
    url = urlunsplit(parsed._replace(path="/0"))
    from redis import Redis

    limit = int(os.environ.get("LLM_GLOBAL_CONCURRENCY", "6"))
    if limit < 1:
        raise ValueError("LLM_GLOBAL_CONCURRENCY must be positive")
    rpm = 0  # Actual HTTP attempts are metered by reserve_attempt.
    digest = hashlib.sha256(f"{config.base_url}|{config.api_key}".encode()).hexdigest()
    key = f"paperforge:provider:{digest}"
    token = uuid.uuid4().hex
    client = Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    stop = threading.Event()
    lost = threading.Event()
    deadline = time.monotonic() + float(os.environ.get("LLM_ADMISSION_TIMEOUT", "300"))
    acquired = False
    fair = os.environ.get("LLM_FAIR_ADMISSION_ENABLED", "false").lower() == "true"
    metadata = request_metadata()
    job = str(metadata.get("scheduler_job_id") or "anonymous")
    priority = 1 if metadata.get("scheduler_priority") == "shadow" else 0
    fair_keys = fair_admission.keys(key)

    def renew():
        while not stop.wait(20):
            try:
                renewal_keys = [key, fair_keys[4], fair_keys[5], fair_keys[6]] if fair else [key]
                if not client.eval(_RENEW, len(renewal_keys), *renewal_keys, token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    thread = None
    waiting_at = time.monotonic()
    try:
        admission_event(
            "queued", job=job, priority=priority, policy="fair-v1" if fair else "legacy"
        )
        while not acquired:
            check_admission_cancelled()
            if fair:
                decision = client.eval(
                    fair_admission.ACQUIRE, len(fair_keys), *fair_keys, token, limit, job, priority
                )
                acquired = bool(decision[0])
            else:
                acquired = bool(client.eval(_ACQUIRE, 2, key, key + ":rpm", token, limit, rpm))
            if not acquired:
                if time.monotonic() >= deadline:
                    raise TimeoutError("provider admission timeout")
                time.sleep(0.2)
        add_timing("provider_slot_wait_ms", int((time.monotonic() - waiting_at) * 1000))
        admission_event(
            "granted",
            job=job,
            wait_ms=int((time.monotonic() - waiting_at) * 1000),
            job_inflight=int(decision[1]) if fair else None,
            total_inflight=int(decision[2]) if fair else None,
            queued_requests=int(decision[3]) if fair else None,
            reason="least_occupied_then_oldest_grant" if fair else "available_slot",
        )
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        check_admission_cancelled()
        yield
        if lost.is_set():
            raise RuntimeError("provider admission lease lost; response not accepted")
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=4)
        try:
            if fair:
                client.eval(fair_admission.RELEASE, len(fair_keys), *fair_keys, token)
            elif acquired:
                client.zrem(key, token)
            admission_event("released" if acquired else "withdrawn", job=job)
        finally:
            client.close()


_ATTEMPT = """
local now = redis.call('TIME')[1]
for i,key in ipairs(KEYS) do
 redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 60)
 local members = redis.call('ZRANGE', key, 0, -1)
 local total = 0
 for _,v in ipairs(members) do total = total + tonumber(string.match(v, ':(%d+)$')) end
 local rpm = tonumber(ARGV[2*i+1])
 local tpm = tonumber(ARGV[2*i+2])
 if rpm > 0 and #members >= rpm then return 0 end
 if tpm > 0 and total + tonumber(ARGV[2]) > tpm then return 0 end
end
for _,key in ipairs(KEYS) do
 redis.call('ZADD', key, now, ARGV[1] .. ':' .. ARGV[2])
 redis.call('EXPIRE', key, 120)
end
return 1
"""


def reserve_attempt(provider, payload):
    """Every HTTP attempt consumes a sliding-window request/token reservation.

    UTF-8 byte count plus output allowance is a conservative estimate, not
    provider-reported token usage. Failed attempts are deliberately not refunded.
    """
    import json
    from urllib.parse import urlsplit, urlunsplit

    url = os.environ.get("LLM_LIMIT_REDIS_URL", "")
    if not url:
        return
    limits = json.loads(os.environ.get("LLM_MODEL_LIMITS", "{}"))
    model = str(payload.get("model", ""))
    budget = limits.get(model, {})
    budgets = [
        (
            "account",
            int(os.environ.get("LLM_GLOBAL_RPM", "0")),
            int(os.environ.get("LLM_GLOBAL_TPM", "0")),
        ),
        (
            hashlib.sha256(model.encode()).hexdigest(),
            int(budget.get("rpm", 0)),
            int(budget.get("tpm", 0)),
        ),
    ]
    if any(rpm < 0 or tpm < 0 for _, rpm, tpm in budgets):
        raise ValueError("provider budgets cannot be negative")
    budgets = [entry for entry in budgets if entry[1] or entry[2]]
    if not budgets:
        return
    from redis import Redis

    estimated = (
        len(json.dumps(payload, ensure_ascii=False).encode())
        + int(payload.get("max_tokens", payload.get("max_completion_tokens", 4096)))
        + 256
    )
    if any(tpm and estimated > tpm for _, _, tpm in budgets):
        raise ValueError("request exceeds configured conservative token budget")
    account = hashlib.sha256(f"{provider.base_url}|{provider.api_key}".encode()).hexdigest()
    keys = [f"paperforge:attempts:{account}:{scope}" for scope, _, _ in budgets]
    arguments = [value for _, rpm, tpm in budgets for value in (rpm, tpm)]
    url = urlunsplit(urlsplit(url)._replace(path="/0"))
    client = Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    deadline = time.monotonic() + float(os.environ.get("LLM_ADMISSION_TIMEOUT", "300"))
    waiting_at = time.monotonic()
    try:
        check_admission_cancelled()
        while not client.eval(_ATTEMPT, len(keys), *keys, uuid.uuid4().hex, estimated, *arguments):
            check_admission_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("provider rate budget wait exceeded")
            time.sleep(0.2)
    finally:
        add_timing("rate_limit_wait_ms", int((time.monotonic() - waiting_at) * 1000))
        client.close()
