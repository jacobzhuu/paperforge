from __future__ import annotations

import base64
import json

import httpx
import pytest
from pydantic import ValidationError
from visuals import (
    AIImageSemantics,
    AIImageSpec,
    ChartSpec,
    CloudflareWorkersAIImageProvider,
    DiagramSpec,
    ImageProviderConfig,
    ImageProviderError,
    ImageRequest,
    OpenAIImageProvider,
    YunwuImageProvider,
    available_image_providers,
    create_image_provider,
    image_provider_configured,
    register_image_provider,
)
from visuals import provider as provider_module


def test_chart_spec_only_accepts_source_columns_not_inline_data_or_code() -> None:
    with pytest.raises(ValidationError):
        ChartSpec.model_validate(
            {
                "kind": "chart",
                "chart_type": "line",
                "source_asset_ref": "ua_deadbeef",
                "x": "epoch",
                "y": ["accuracy"],
                "data": [{"epoch": 1, "accuracy": 0.9}],
            }
        )


def test_chart_error_bounds_reject_ambiguous_aggregation() -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        ChartSpec(
            chart_type="bar",
            source_asset_ref="ua_deadbeef",
            x="method",
            y=["score"],
            error_lower="low",
            error_upper="high",
            aggregation="mean",
        )
    with pytest.raises(ValidationError):
        ChartSpec(
            chart_type="line",
            source_asset_ref="ua_deadbeef",
            x="https://example.com/data.csv",
            y=["accuracy"],
        )


def test_diagram_rejects_duplicate_nodes_and_dangling_edges() -> None:
    with pytest.raises(ValidationError, match="unique"):
        DiagramSpec(nodes=[{"id": "n1", "label": "A"}, {"id": "n1", "label": "B"}])
    with pytest.raises(ValidationError, match="existing nodes"):
        DiagramSpec(
            nodes=[{"id": "n1", "label": "A"}],
            edges=[{"source": "n1", "target": "n2"}],
        )


def test_ai_image_rejects_injection_but_not_subject_matter() -> None:
    """校验层只挡注入内容。

    题材黑名单（准确率 / 坐标轴 / 柱状图 …）已经去掉：它按关键字工作，
    连「解释准确率概念」这类正当描述一起挡了。该画什么由规划提示词与人工审核决定。
    """
    with pytest.raises(ValidationError, match="URLs or executable content"):
        AIImageSpec(prompt="Illustrate the pipeline described at https://example.com/paper")
    assert AIImageSpec(prompt="A conceptual view of how accuracy improves over training").kind == (
        "ai_image"
    )


def test_render_prompt_uses_the_refined_prompt_verbatim() -> None:
    """润色后的提示词必须原样下发，不能再被拼接逻辑改写一遍。

    否则确认框展示的 `resolved_prompt` 与实际发送的字符串会分叉——
    而两者同出于 `render_prompt()` 正是为了防这件事。
    """
    refined = (
        "A wide conceptual illustration of a root system anchoring a soil slope, "
        "warm neutral palette, soft directional light, publication quality."
    )
    spec = AIImageSpec(
        prompt="root system. composition: cross-section",
        semantics=AIImageSemantics(subject="root system", text_policy="none"),
        refined_prompt=refined,
    )
    assert spec.render_prompt() == refined


def test_manual_prompt_override_has_explicit_highest_precedence() -> None:
    spec = AIImageSpec(
        prompt="A sufficiently long fallback image prompt",
        semantics=AIImageSemantics(
            subject="structured subject",
            elements=["first element", "second element"],
        ),
        refined_prompt="A sufficiently long model-refined image prompt.",
        prompt_override="A sufficiently long user-authored final image prompt.",
        negative_prompt="watermark, signature",
        seed=42,
    )
    assert spec.render_prompt() == "A sufficiently long user-authored final image prompt."
    assert spec.negative_prompt == "watermark, signature"
    assert spec.seed == 42


def test_render_prompt_falls_back_to_assembly_without_a_refined_prompt() -> None:
    spec = AIImageSpec(
        prompt="unused once semantics exist",
        semantics=AIImageSemantics(subject="root system", composition="cross-section"),
    )
    rendered = spec.render_prompt()
    assert rendered.startswith("root system. composition: cross-section")
    # text_policy 默认 auto：不再无条件附加禁字指令。
    assert "no text" not in rendered


def test_render_prompt_still_honours_an_explicit_no_text_policy() -> None:
    spec = AIImageSpec(
        prompt="unused once semantics exist",
        semantics=AIImageSemantics(subject="root system", text_policy="none"),
    )
    assert "no text, no labels, no numerals" in spec.render_prompt()


def test_openai_provider_parses_base64_and_request_id() -> None:
    png = b"\x89PNG\r\n\x1a\ncontent"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            headers={"x-request-id": "req_1"},
            json={"data": [{"b64_json": base64.b64encode(png).decode()}], "usage": {"images": 1}},
        )

    provider = OpenAIImageProvider(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert result.data == png
    assert result.request_id == "req_1"
    assert result.model == "gpt-image-2"


def test_yunwu_provider_accepts_url_response_without_forwarding_api_key() -> None:
    jpeg = b"\xff\xd8\xffyunwu-image"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/v1/images/generations"
            assert request.headers["authorization"] == "Bearer yunwu-secret"
            assert json.loads(request.read()) == {
                "model": "gpt-image-1",
                "prompt": "A conceptual scientific illustration",
                "size": "1536x1024",
                "quality": "high",
                "n": 1,
            }
            return httpx.Response(
                200,
                headers={"x-request-id": "yunwu_req_1"},
                json={
                    "data": [{"url": "https://cdn.example.com/generated.jpeg"}],
                    "usage": {"images": 1},
                },
            )
        assert request.method == "GET"
        assert request.url == "https://cdn.example.com/generated.jpeg"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})

    provider = YunwuImageProvider(
        api_key="yunwu-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(
        ImageRequest(
            prompt="A conceptual scientific illustration",
            size="1536x1024",
            quality="high",
        )
    )
    assert result.data == jpeg
    assert result.media_type == "image/jpeg"
    assert result.provider == "yunwu"
    assert result.model == "gpt-image-1"
    assert result.request_id == "yunwu_req_1"
    assert result.usage == {"images": 1}


def test_yunwu_provider_accepts_base64_and_rejects_unsafe_image_url() -> None:
    png = b"\x89PNG\r\n\x1a\nyunwu"
    base64_provider = YunwuImageProvider(
        api_key="yunwu-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": [{"b64_json": base64.b64encode(png).decode()}]},
                )
            )
        ),
    )
    assert (
        base64_provider.generate(ImageRequest(prompt="A conceptual scientific illustration")).data
        == png
    )

    unsafe_provider = YunwuImageProvider(
        api_key="yunwu-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"url": "http://127.0.0.1/internal"}]},
                    request=request,
                )
            )
        ),
    )
    with pytest.raises(ImageProviderError) as captured:
        unsafe_provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "invalid_image"


@pytest.mark.parametrize(
    "image_request",
    [
        ImageRequest(prompt="A conceptual scientific illustration", seed=42),
        ImageRequest(
            prompt="A conceptual scientific illustration",
            negative_prompt="watermark",
        ),
    ],
)
def test_yunwu_rejects_unadvertised_optional_parameters_without_network(
    image_request: ImageRequest,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("unsupported options must fail before network I/O")

    provider = YunwuImageProvider(
        api_key="yunwu-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert not provider.capabilities.supports_negative_prompt
    assert not provider.capabilities.supports_seed
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(image_request)
    assert captured.value.code == "invalid_request"
    assert calls == 0


def test_capability_enabled_openai_compatible_provider_maps_optional_parameters() -> None:
    png = b"\x89PNG\r\n\x1a\ncontent"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["negative_prompt"] == "watermark"
        assert payload["seed"] == 17
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(png).decode()}]},
        )

    provider = OpenAIImageProvider(
        api_key="secret",
        provider_name="compatible-test",
        supports_negative_prompt=True,
        supports_seed=True,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.capabilities.supports_negative_prompt
    assert provider.capabilities.supports_seed
    provider.generate(
        ImageRequest(
            prompt="A conceptual scientific illustration",
            negative_prompt="watermark",
            seed=17,
        )
    )


@pytest.mark.parametrize("status_code", [401, 403, 400, 422])
def test_non_retryable_provider_failures_are_called_once(status_code: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="moderation blocked")

    provider = OpenAIImageProvider(
        api_key="secret",
        max_retries=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert not captured.value.retryable
    assert calls == 1


def test_invalid_base64_is_rejected() -> None:
    provider = OpenAIImageProvider(
        api_key="secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"data": [{"b64_json": "!!!"}]})
            )
        ),
    )
    with pytest.raises(ImageProviderError, match="invalid image provider payload"):
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))


def test_retryable_provider_failures_use_bounded_retry(monkeypatch) -> None:
    statuses = iter([429, 503, 200])
    calls = 0
    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status = next(statuses)
        if status == 200:
            png = b"\x89PNG\r\n\x1a\ncontent"
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]}
            )
        return httpx.Response(status)

    provider = OpenAIImageProvider(
        api_key="secret",
        max_retries=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.generate(ImageRequest(prompt="A conceptual scientific illustration")).data
    assert calls == 3


def test_network_timeout_is_retryable_and_bounded(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    provider = OpenAIImageProvider(
        api_key="secret",
        max_retries=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "network_timeout"
    assert captured.value.retryable
    assert calls == 3


def test_provider_rejects_oversize_and_forged_png(monkeypatch) -> None:
    monkeypatch.setattr(provider_module, "MAX_PROVIDER_IMAGE_BYTES", 12)

    def response_for(data: bytes) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"data": [{"b64_json": base64.b64encode(data).decode()}]},
                )
            )
        )

    oversized = OpenAIImageProvider(api_key="secret", client=response_for(b"x" * 13))
    with pytest.raises(ImageProviderError) as captured:
        oversized.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "invalid_image"

    forged = OpenAIImageProvider(api_key="secret", client=response_for(b"%PDF-1.7"))
    with pytest.raises(ImageProviderError) as captured:
        forged.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "invalid_image"


def test_cloudflare_flux_provider_uses_official_rest_contract() -> None:
    jpeg = b"\xff\xd8\xffconceptual-image"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/client/v4/accounts/0123456789abcdef0123456789abcdef/ai/run/"
            "@cf/black-forest-labs/flux-1-schnell"
        )
        assert request.headers["authorization"] == "Bearer cf-secret"
        assert json.loads(request.read()) == {
            "prompt": "A conceptual scientific illustration",
            "steps": 8,
            "seed": 123,
        }
        return httpx.Response(
            200,
            headers={"cf-ray": "ray_1"},
            json={
                "success": True,
                "result": {
                    "image": base64.b64encode(jpeg).decode(),
                    "usage": {"requests": 1},
                },
            },
        )

    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(
        ImageRequest(
            prompt="A conceptual scientific illustration",
            size="1536x1024",
            quality="high",
            seed=123,
        )
    )
    assert result.data == jpeg
    assert result.media_type == "image/jpeg"
    assert result.provider == "cloudflare"
    assert result.model == "@cf/black-forest-labs/flux-1-schnell"
    assert result.request_id == "ray_1"
    assert result.usage == {
        "steps": 8,
        "requested_quality": "high",
        "requested_size": "1536x1024",
        "seed": 123,
        "requests": 1,
    }
    assert provider.capabilities.supports_seed
    assert not provider.capabilities.supports_negative_prompt


def test_cloudflare_provider_accepts_binary_png_response() -> None:
    png = b"\x89PNG\r\n\x1a\ncontent"
    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, content=png, headers={"content-type": "image/png"}
                )
            )
        ),
    )
    result = provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert result.data == png
    assert result.media_type == "image/png"


def test_cloudflare_provider_uses_magic_bytes_instead_of_declared_mime() -> None:
    jpeg = b"\xff\xd8\xffcontent"
    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, content=jpeg, headers={"content-type": "image/png"}
                )
            )
        ),
    )
    result = provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert result.media_type == "image/jpeg"


def test_cloudflare_provider_requires_token_and_account_without_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("configuration errors must not make a network request")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for account_id, api_key, code in (
        ("0123456789abcdef0123456789abcdef", "", "provider_not_configured"),
        ("", "cf-secret", "provider_not_configured"),
    ):
        provider = CloudflareWorkersAIImageProvider(
            account_id=account_id,
            api_key=api_key,
            client=client,
        )
        with pytest.raises(ImageProviderError) as captured:
            provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
        assert captured.value.code == code
        assert not captured.value.retryable
    assert calls == 0


@pytest.mark.parametrize("status_code", [401, 403, 400, 422])
def test_cloudflare_non_retryable_failures_are_called_once(status_code: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="safety moderation blocked")

    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        max_retries=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert not captured.value.retryable
    assert calls == 1


def test_cloudflare_retryable_failures_are_bounded(monkeypatch) -> None:
    statuses = iter([429, 503, 200])
    calls = 0
    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status = next(statuses)
        if status == 200:
            jpeg = b"\xff\xd8\xffcontent"
            return httpx.Response(
                200,
                json={"result": {"image": base64.b64encode(jpeg).decode()}},
            )
        return httpx.Response(status)

    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        max_retries=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.generate(ImageRequest(prompt="A conceptual scientific illustration")).data
    assert calls == 3


def test_cloudflare_network_timeout_is_retryable_and_bounded(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr(provider_module.time, "sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        max_retries=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "network_timeout"
    assert captured.value.retryable
    assert calls == 3


def test_cloudflare_rejects_invalid_payload_and_long_prompt() -> None:
    provider = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"result": {"image": "!!!"}})
            )
        ),
    )
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "invalid_image"
    with pytest.raises(ImageProviderError) as captured:
        provider.generate(ImageRequest(prompt="x" * 2049))
    assert captured.value.code == "invalid_request"

    unsuccessful = CloudflareWorkersAIImageProvider(
        account_id="0123456789abcdef0123456789abcdef",
        api_key="cf-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"success": False, "errors": [{"message": "rejected"}]},
                )
            )
        ),
    )
    with pytest.raises(ImageProviderError) as captured:
        unsuccessful.generate(ImageRequest(prompt="A conceptual scientific illustration"))
    assert captured.value.code == "provider_unavailable"


def test_image_provider_factory_is_extensible_without_worker_changes() -> None:
    assert {"cloudflare", "openai", "yunwu"} <= set(available_image_providers())
    cloudflare = ImageProviderConfig(
        provider="cloudflare",
        api_key="cf-secret",
        account_id="0123456789abcdef0123456789abcdef",
    )
    assert isinstance(create_image_provider(cloudflare), CloudflareWorkersAIImageProvider)
    assert image_provider_configured(cloudflare)
    assert not image_provider_configured(ImageProviderConfig(provider="cloudflare"))
    assert isinstance(
        create_image_provider(ImageProviderConfig(provider="openai", api_key="secret")),
        OpenAIImageProvider,
    )
    assert image_provider_configured(ImageProviderConfig(provider="openai", api_key="secret"))
    yunwu = ImageProviderConfig(
        provider="yunwu",
        api_key="yunwu-secret",
        base_url="https://yunwu.ai/v1",
        model="gpt-image-1",
    )
    assert isinstance(create_image_provider(yunwu), YunwuImageProvider)
    assert image_provider_configured(yunwu)

    class LocalProvider:
        configured = True

        def generate(self, request: ImageRequest):
            return request

        def close(self) -> None:
            pass

    register_image_provider("test-local", lambda _config, _client: LocalProvider())
    local = create_image_provider(ImageProviderConfig(provider="test-local"))
    assert local.configured


def test_axis_labels_are_validated_like_column_names() -> None:
    """轴标签与单位不能是校验口径上的例外。

    x/y/series 一直过 `_REMOTE_OR_CODE`，x_label/y_label/unit 却是唯一放行的自由文本：
    同一个 `https://…` 写进节点标签被拒、写进 y_label 却通过。
    """
    base = {
        "kind": "chart",
        "chart_type": "bar",
        "source_asset_ref": "ua_deadbeef",
        "x": "method",
        "y": ["score"],
    }
    # 正常的中英文标签仍然要放行。
    ok = ChartSpec.model_validate({**base, "x_label": "方法", "y_label": "Accuracy", "unit": "%"})
    assert ok.unit == "%"

    for field, bad in (
        ("x_label", "see https://evil.example/x"),
        ("y_label", "<script>alert(1)</script>"),
        ("unit", "eval(1)"),
    ):
        with pytest.raises(ValidationError):
            ChartSpec.model_validate({**base, field: bad})
