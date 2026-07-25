from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# 迁移自 DeepSearch packages/observability/metrics.py。
# 改动：保留通用 HTTP + 渲染指标；丢弃 DeepSearch OSINT 专属计数器
# （browser_fallback / fetch / crawler 等），改为 PaperForge 管线维度指标；指标前缀 paperforge_*。

_HTTP_REQUESTS_TOTAL = Counter(
    "paperforge_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "path", "status_code"),
)
_HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "paperforge_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path"),
)
_JOB_STAGE_TOTAL = Counter(
    "paperforge_job_stage_total",
    "Generation job stage completions by kind/stage/status.",
    labelnames=("kind", "stage", "status"),
)
_LLM_CALLS_TOTAL = Counter(
    "paperforge_llm_calls_total",
    "LLM calls by role and outcome.",
    labelnames=("role", "outcome"),
)
_SCHOLAR_PROVIDER_TOTAL = Counter(
    "paperforge_scholar_provider_total",
    "Scholar provider requests by provider and outcome.",
    labelnames=("provider", "outcome"),
)


def observe_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    _HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status_code=str(status_code)).inc()
    _HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration_seconds)


def record_job_stage(kind: str, stage: str, status: str) -> None:
    _JOB_STAGE_TOTAL.labels(kind=kind, stage=stage, status=status).inc()


def record_llm_call(role: str, outcome: str) -> None:
    _LLM_CALLS_TOTAL.labels(role=role, outcome=outcome).inc()


def record_scholar_provider(provider: str, outcome: str) -> None:
    _SCHOLAR_PROVIDER_TOTAL.labels(provider=provider, outcome=outcome).inc()


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
