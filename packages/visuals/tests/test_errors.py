"""统一错误词表的行为契约。

这些断言守的是一条产品红线：**裸的异常类名不许流到用户面前**。图表与示意图的
失败（visuald 不可达、源素材解析不唯一）占建议的大多数，它们此前全部以
`ValueError` / `ConnectError` 这类字符串落库，用户既看不懂也不知道能不能重试。
"""

from __future__ import annotations

import httpx
import pytest
from visuals import ImageProviderError, VisualdError, classify_visual_error
from visuals.errors import (
    INTERNAL_ERROR,
    LEGACY_CODE_ALIASES,
    NORMALIZATION_FAILED,
    SOURCE_UNRESOLVED,
    VISUAL_ERROR_CODES,
    VISUAL_PREFLIGHT_FAILED,
    VISUALD_UNAVAILABLE,
    is_retryable,
    normalize_code,
)


def test_every_legacy_code_maps_into_the_vocabulary() -> None:
    """历史行里的旧 code 必须都有归宿，否则界面会遇到自己不认识的值。"""
    for legacy, canonical in LEGACY_CODE_ALIASES.items():
        assert canonical in VISUAL_ERROR_CODES, legacy
        assert normalize_code(legacy) == canonical


def test_unknown_and_bare_class_names_fall_back_to_internal_error() -> None:
    # 这正是历史数据里那些裸类名的样子。
    assert normalize_code("ValueError") == INTERNAL_ERROR
    assert normalize_code("ConnectError") == INTERNAL_ERROR
    assert normalize_code(None) == INTERNAL_ERROR
    assert normalize_code("") == INTERNAL_ERROR


def test_canonical_codes_pass_through_unchanged() -> None:
    for code in VISUAL_ERROR_CODES:
        assert normalize_code(code) == code


def test_provider_errors_keep_their_request_id() -> None:
    error = ImageProviderError(
        "rejected", code="content_rejected", retryable=False, request_id="cf-ray-1"
    )
    info = classify_visual_error(error)
    assert info.code == "content_rejected"
    assert info.request_id == "cf-ray-1"
    assert not info.retryable
    # 面向用户的文案，不是厂商英文串的转述。
    assert "内容检查" in info.message


def test_visuald_failures_are_not_reported_as_bare_class_names() -> None:
    info = classify_visual_error(VisualdError("visuald unavailable: ConnectError"))
    assert info.code == VISUALD_UNAVAILABLE
    assert info.retryable
    # 技术细节仍然留痕，只是不当作主文案。
    assert "VisualdError" in (info.detail or "")


def test_normalization_failures_are_distinguished_from_renderer_outage() -> None:
    """归一化失败与「visuald 整个挂了」对用户的含义不同，不能混成一类。"""
    info = classify_visual_error(VisualdError("visuald HTTP 500: /normalize failed"))
    assert info.code == NORMALIZATION_FAILED


def test_chart_source_failures_are_actionable() -> None:
    error = ValueError("chart source asset does not resolve uniquely in this project")
    info = classify_visual_error(error)
    assert info.code == SOURCE_UNRESOLVED
    assert "素材" in info.message


def test_visual_preflight_failure_is_actionable_and_not_blindly_retryable() -> None:
    info = classify_visual_error(ValueError("visual preflight failed: visual_resolution_too_small"))
    assert info.code == VISUAL_PREFLIGHT_FAILED
    assert "画布尺寸或版式" in info.message
    assert not info.retryable


def test_arbitrary_exceptions_never_leak_their_class_name_as_the_code() -> None:
    for error in (
        RuntimeError("boom"),
        httpx.ConnectError("nope"),
        KeyError("missing"),
    ):
        info = classify_visual_error(error)
        assert info.code in VISUAL_ERROR_CODES
        assert info.code == INTERNAL_ERROR


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("rate_limited", True),
        ("provider_unavailable", True),
        ("network_timeout", True),
        ("content_rejected", False),
        ("authentication_failed", False),
        ("provider_not_configured", False),
    ],
)
def test_retryability_drives_the_advice_shown_to_users(code: str, expected: bool) -> None:
    """界面按这个判断给「重试」还是给「先改提示词」。"""
    assert is_retryable(code) is expected
