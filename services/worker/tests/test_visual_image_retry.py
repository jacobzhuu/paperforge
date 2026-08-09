from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from paperforge_worker.pipelines.visuals import _generate_image_with_compliance_retry
from visuals import ImageProviderCapabilities, ImageProviderError, ImageRequest, ImageResult


@dataclass
class _LLMResult:
    value: Any

    @property
    def ok(self) -> bool:
        return self.value is not None


class _Runner:
    enabled = True

    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls = 0

    async def agenerate_json(self, *_args: Any, **_kwargs: Any) -> _LLMResult:
        self.calls += 1
        return _LLMResult(self.value)


class _Provider:
    configured = True
    capabilities = ImageProviderCapabilities(provider="stub", model="stub")

    def __init__(self, results: list[ImageResult | Exception]) -> None:
        self.results = list(results)
        self.requests: list[ImageRequest] = []

    def generate(self, request: ImageRequest) -> ImageResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        pass


def _rejected(request_id: str) -> ImageProviderError:
    return ImageProviderError(
        "image request rejected",
        code="content_rejected",
        retryable=False,
        request_id=request_id,
    )


def _image() -> ImageResult:
    return ImageResult(
        data=b"\x89PNG\r\n\x1a\ncontent",
        media_type="image/png",
        provider="stub",
        model="stub",
        request_id="retry-2",
    )


def _safe_rewrite() -> dict[str, Any]:
    return {
        "can_retry": True,
        "prompt": (
            "A neutral academic illustration of a clinical workflow using abstract "
            "silhouettes, restrained colors, and a clean publication-ready layout."
        ),
        "reason": "Replaced ambiguous wording with neutral clinical language.",
    }


@pytest.mark.asyncio
async def test_content_rejection_gets_exactly_one_compliance_retry() -> None:
    provider = _Provider([_rejected("first-1"), _image()])
    runner = _Runner(_safe_rewrite())

    outcome = await _generate_image_with_compliance_retry(
        provider,
        ImageRequest(prompt="A graphic clinical workflow for an academic paper."),
        runner=runner,
    )

    assert outcome.generated is not None
    assert outcome.error is None
    assert len(provider.requests) == 2
    assert runner.calls == 1
    assert outcome.compliance_audit == {
        "original_prompt": "A graphic clinical workflow for an academic paper.",
        "rewritten_prompt": _safe_rewrite()["prompt"],
        "original_error_code": "content_rejected",
        "original_error_reason": "image request rejected",
        "original_request_id": "first-1",
        "retry_count": 1,
        "max_retries": 1,
        "rewrite_reason": "Replaced ambiguous wording with neutral clinical language.",
    }


@pytest.mark.asyncio
async def test_second_content_rejection_is_not_rewritten_or_retried_again() -> None:
    provider = _Provider([_rejected("first-1"), _rejected("second-2")])
    runner = _Runner(_safe_rewrite())

    outcome = await _generate_image_with_compliance_retry(
        provider,
        ImageRequest(prompt="A graphic clinical workflow for an academic paper."),
        runner=runner,
    )

    assert isinstance(outcome.error, ImageProviderError)
    assert outcome.error.request_id == "second-2"
    assert len(provider.requests) == 2
    assert runner.calls == 1
    assert outcome.compliance_audit is not None
    assert outcome.compliance_audit["retry_count"] == 1


@pytest.mark.asyncio
async def test_unsafe_or_unusable_rewrite_stops_after_the_original_rejection() -> None:
    provider = _Provider([_rejected("first-1")])
    runner = _Runner({"can_retry": False, "prompt": "", "reason": "unsafe intent"})

    outcome = await _generate_image_with_compliance_retry(
        provider,
        ImageRequest(prompt="A request rejected by the image safety policy."),
        runner=runner,
    )

    assert isinstance(outcome.error, ImageProviderError)
    assert len(provider.requests) == 1
    assert runner.calls == 1
    assert outcome.compliance_audit is not None
    assert outcome.compliance_audit["retry_count"] == 0
